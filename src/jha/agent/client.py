"""Anthropic 客户端封装：每次调用都记账，并强制预算上限。

改成 agent loop 之后，调用次数由模型决定而不是由代码决定——所以
**记账和预算不再是可选项**。没有它们，一次跑偏的循环可以在你不知情的
情况下烧掉一天的额度。

llm_calls 表 Phase 0 就建好了，这里只是把它用起来。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .. import config

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_TOKENS = 4096

#: 每百万 token 的价格（美元），仅用于本地估算，不是账单。
#: 换模型时记得更新，否则成本统计会误导你。
#:
#: 别名要一起写进来：查不到价格时 `Budget.record` 会按 (0, 0) 算，
#: 于是那部分调用的成本被**静默记成 $0**。分层设计里量最大的恰恰是 Haiku，
#: 漏一个别名就等于整个第二层不计费——账面好看，实际不知道钱花在哪。
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    # ponytail: 提示超过 10 万 token 时是 $0.50 / $2.50，这里的调用都是单封邮件、单个 JD，到不了
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}


#: 第二层里开思考的模型。思考 token 按输出计费，Haiku 5.5 的输出 $0.50 / 百万，开得起
THINKING_MODELS = frozenset({"claude-haiku-5-5"})
THINKING_EFFORT = "high"            # medium（Haiku 5.5 默认）实测对邮件根本不思考；high 才真的想
THINKING_MAX_TOKENS = 16000


def strict_schema(schema: Any) -> Any:
    """structured outputs 要求每个 object 都写 additionalProperties: false。返回补好的副本，不改原 schema。"""
    if isinstance(schema, dict):
        out = {k: strict_schema(v) for k, v in schema.items()}
        if out.get("type") == "object":
            out["additionalProperties"] = False
        return out
    if isinstance(schema, list):
        return [strict_schema(v) for v in schema]
    return schema


def price_of(model: str) -> tuple[float, float]:
    """取价格；未知模型返回 (0,0) 但**留下痕迹**，不静默吞掉。"""
    if model not in PRICING:
        UNPRICED_MODELS.add(model)
    return PRICING.get(model, (0.0, 0.0))


#: 跑过但没有价格表的模型。`agent spend` 会把它列出来提醒你补。
UNPRICED_MODELS: set[str] = set()


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
        rate_in, rate_out = price_of(model)
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


    def structured(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str = "result",
        budget: Budget | None = None,
        conn: sqlite3.Connection | None = None,
        purpose: str = "structured",
        model: str | None = None,
        ref_type: str | None = None,
        ref_id: int | None = None,
    ) -> dict[str, Any]:
        """**第二层调用**：单条、独立上下文、不累积、拿结构化结果就走。

        这是 §1.1 分层设计里的下层，用来处理 JD 全文、邮件正文这类大块不可信文本。

        关于「第二层没有工具」和这里传了 `tools=` 的关系——**不矛盾，但值得讲清楚**：
        这里的 schema 只是**输出模具**，用来强制模型按 JSON 结构作答。
        它不经过 `tools_mod.execute`，没有任何东西会被执行，模型也拿不到
        数据库或网络。所以注入内容即使说服了这一层，它也无处可施。

        真正的区别在于：第一层的工具调用会**路由到执行器**，这一层不会。
        """
        budget = budget or Budget()
        budget.check()
        use_model = model or self.model
        thinks = use_model in THINKING_MODELS
        if thinks:
            # 强制 tool_choice 会让 Haiku 5.5 跳过思考，所以改用 JSON schema 约束输出，一样保证结构
            shape: dict[str, Any] = {
                "max_tokens": max(self.max_tokens, THINKING_MAX_TOKENS),   # 思考也占 max_tokens
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": THINKING_EFFORT,
                                  "format": {"type": "json_schema", "schema": strict_schema(schema)}},
            }
        else:
            shape = {
                "max_tokens": self.max_tokens,
                "tools": [{"name": schema_name, "description": "按这个结构返回结果", "input_schema": schema}],
                "tool_choice": {"type": "tool", "name": schema_name},
            }
        resp = self._ensure().messages.create(
            model=use_model, system=system, messages=[{"role": "user", "content": user}], **shape,
        )
        usage = getattr(resp, "usage", None)
        inp = getattr(usage, "input_tokens", 0) or 0
        out = getattr(usage, "output_tokens", 0) or 0
        cost = budget.record(use_model, inp, out)
        if conn is not None:
            log_call(conn, purpose=purpose, model=use_model, inp=inp, out=out,
                     cost=cost, ref_type=ref_type, ref_id=ref_id)

        stop = getattr(resp, "stop_reason", None)
        if stop == "refusal":
            # Haiku 5.5 起有安全分类器，被拒没有服务端兜底模型——让调用方按「没结果」处理，别当成结论
            raise ValueError("模型拒绝了这次请求（stop_reason=refusal）")
        if stop == "max_tokens":
            raise ValueError("输出到了 max_tokens 上限，结果不完整")
        for block in getattr(resp, "content", []) or []:
            kind = getattr(block, "type", None)
            if thinks and kind == "text":
                data = json.loads(block.text)      # 回答前面可能有 thinking 块，按类型取
                if isinstance(data, dict):
                    return data
            elif not thinks and kind == "tool_use":
                return dict(getattr(block, "input", {}) or {})
        raise ValueError("模型没有按 schema 返回结果")


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
