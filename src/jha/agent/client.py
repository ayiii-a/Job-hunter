"""Anthropic 客户端封装：每次调用都记账，并强制预算上限。

改成 agent loop 之后，调用次数由模型决定而不是由代码决定——所以
**记账和预算不再是可选项**。没有它们，一次跑偏的循环可以在你不知情的
情况下烧掉一天的额度。

llm_calls 表 Phase 0 就建好了，这里只是把它用起来。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .. import config

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_TOKENS = 4096

#: 每百万 token 的价格（美元），仅用于本地估算，不是账单。
#: 换模型时记得更新，否则成本统计会误导你。
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (15.0, 75.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}


class BudgetExceeded(RuntimeError):
    pass


class MissingAPIKey(RuntimeError):
    pass


@dataclass
class Budget:
    """一次 run 的上限。超了就抛异常，而不是继续烧。"""

    max_llm_calls: int = 25
    max_input_tokens: int = 400_000
    max_output_tokens: int = 60_000

    calls: int = field(default=0, init=False)
    input_tokens: int = field(default=0, init=False)
    output_tokens: int = field(default=0, init=False)
    cost_usd: float = field(default=0.0, init=False)

    def check(self) -> None:
        if self.calls >= self.max_llm_calls:
            raise BudgetExceeded(f"LLM 调用次数达到上限 {self.max_llm_calls}")
        if self.input_tokens >= self.max_input_tokens:
            raise BudgetExceeded(f"输入 token 达到上限 {self.max_input_tokens}")
        if self.output_tokens >= self.max_output_tokens:
            raise BudgetExceeded(f"输出 token 达到上限 {self.max_output_tokens}")

    def record(self, model: str, inp: int, out: int) -> float:
        self.calls += 1
        self.input_tokens += inp
        self.output_tokens += out
        rate_in, rate_out = PRICING.get(model, (0.0, 0.0))
        cost = inp / 1e6 * rate_in + out / 1e6 * rate_out
        self.cost_usd += cost
        return cost

    def summary(self) -> dict[str, Any]:
        return {
            "llm_calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 4),
        }


class AgentClient:
    """薄封装。真正的循环在 loop.py，这里只负责一次调用 + 记账。"""

    def __init__(
        self,
        *,
        model: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        api_key: str | None = None,
    ):
        self.model = model or config.env("JHA_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL
        self.max_tokens = max_tokens
        self._api_key = api_key or config.env("ANTHROPIC_API_KEY")
        self._client: Any = None

    def _ensure(self) -> Any:
        if self._client is None:
            if not self._api_key:
                raise MissingAPIKey(
                    "没有 ANTHROPIC_API_KEY。cp .env.example .env 然后填进去。"
                    "（不需要 key 也能跑的：agent tools / fetch / jobs / profile check）"
                )
            from anthropic import Anthropic

            self._client = Anthropic(api_key=self._api_key)
        return self._client

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]],
        budget: Budget,
        conn: sqlite3.Connection | None = None,
        purpose: str = "agent_loop",
    ) -> Any:
        budget.check()
        resp = self._ensure().messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
            tools=tools,
        )
        usage = getattr(resp, "usage", None)
        inp = getattr(usage, "input_tokens", 0) or 0
        out = getattr(usage, "output_tokens", 0) or 0
        cost = budget.record(self.model, inp, out)
        if conn is not None:
            log_call(conn, purpose=purpose, model=self.model, inp=inp, out=out, cost=cost)
        return resp


def log_call(
    conn: sqlite3.Connection,
    *,
    purpose: str,
    model: str,
    inp: int,
    out: int,
    cost: float,
    ref_type: str | None = None,
    ref_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO llm_calls (purpose, model, input_tokens, output_tokens, cost_usd, "
        "ref_type, ref_id) VALUES (?,?,?,?,?,?,?)",
        (purpose, model, inp, out, cost, ref_type, ref_id),
    )
    conn.commit()


def spend_summary(conn: sqlite3.Connection, *, days: int = 30) -> list[dict[str, Any]]:
    """按用途拆成本。Phase 7 要的就是这个视图。"""
    rows = conn.execute(
        "SELECT purpose, COUNT(*) AS calls, SUM(input_tokens) AS inp, "
        "SUM(output_tokens) AS out, ROUND(SUM(cost_usd), 4) AS cost "
        "FROM llm_calls WHERE called_at >= datetime('now', ?) "
        "GROUP BY purpose ORDER BY cost DESC",
        (f"-{int(days)} days",),
    )
    return [dict(r) for r in rows]
