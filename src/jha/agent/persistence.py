"""把 agent 的执行轨迹落库。

无人值守每天自动跑、还允许写库，却查不到它到底干了什么——这个组合不能上线。
`agent_runs` / `agent_steps` 之于 agent，就是 `events` 之于投递：**追加式的事实记录**。

三个用途：
    复盘       那条状态为什么被改了 —— 翻这次 run 的 steps
    按任务拆账 哪个定时任务在烧钱 —— group by schedule_name
    权限毕业   某个 GATED 工具被批准过多少次而没出事（§0「权限逐级放开」）

设计上刻意**不让写日志的失败影响 run 本身**：日志挂了顶多少一条记录，
而 agent 已经做完的事是真的做了。所以这里所有写入都吞异常。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

MAX_SUMMARY = 2000


class RunRecorder:
    """一次 run 的记录器。用法：start() → step()… → finish()。"""

    def __init__(self, conn: sqlite3.Connection, *, enabled: bool = True):
        self.conn = conn
        self.enabled = enabled
        self.run_id: int | None = None
        self._seq = 0

    # ------------------------------------------------------------------
    def start(self, task: str, *, model: str | None = None, schedule_name: str | None = None) -> int | None:
        if not self.enabled:
            return None
        try:
            cur = self.conn.execute(
                "INSERT INTO agent_runs (task, schedule_name, model) VALUES (?,?,?)",
                (task, schedule_name, model),
            )
            self.conn.commit()
            self.run_id = int(cur.lastrowid)
        except sqlite3.Error:
            self.enabled = False        # 日志坏了不能拖垮 run
        return self.run_id

    def step(self, step: Any) -> None:
        if not (self.enabled and self.run_id):
            return
        self._seq += 1
        tool_name = getattr(step, "name", "") or None
        kind = getattr(step, "kind", "") or ""
        detail = (getattr(step, "detail", "") or "")[:MAX_SUMMARY]
        args = detail if kind == "tool_use" else None
        error = detail if kind in ("error", "denied") else None
        summary = detail if kind in ("text", "tool_result") else None
        try:
            self.conn.execute(
                "INSERT INTO agent_steps (run_id, seq, kind, tool_name, args_json, "
                "result_summary, error) VALUES (?,?,?,?,?,?,?)",
                (self.run_id, self._seq, kind, tool_name, args, summary, error),
            )
            self.conn.commit()
        except sqlite3.Error:
            self.enabled = False

    def finish(self, result: Any) -> None:
        if not (self.enabled and self.run_id):
            return
        b = getattr(result, "budget", {}) or {}
        try:
            self.conn.execute(
                "UPDATE agent_runs SET finished_at = datetime('now'), ok = ?, "
                "stopped_because = ?, llm_calls = ?, input_tokens = ?, output_tokens = ?, "
                "cost_usd = ?, tool_calls = ?, pending_approvals_json = ?, final_text = ? "
                "WHERE id = ?",
                (
                    1 if getattr(result, "stopped_because", "") == "完成" else 0,
                    getattr(result, "stopped_because", None),
                    b.get("llm_calls", 0), b.get("input_tokens", 0),
                    b.get("output_tokens", 0), b.get("cost_usd", 0.0),
                    getattr(result, "tool_calls", 0),
                    json.dumps(getattr(result, "pending_approvals", []), ensure_ascii=False),
                    (getattr(result, "final_text", "") or "")[:MAX_SUMMARY],
                    self.run_id,
                ),
            )
            self.conn.commit()
        except sqlite3.Error:
            self.enabled = False


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def recent_runs(conn: sqlite3.Connection, *, limit: int = 20, schedule: str | None = None) -> list[dict]:
    sql = "SELECT * FROM agent_runs WHERE 1=1 "
    params: list[Any] = []
    if schedule:
        sql += "AND schedule_name = ? "
        params.append(schedule)
    sql += "ORDER BY started_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params)]


def run_steps(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM agent_steps WHERE run_id = ? ORDER BY seq", (run_id,)
        )
    ]


def cost_by_schedule(conn: sqlite3.Connection, *, days: int = 30) -> list[dict]:
    """按任务拆账。定时任务跑起来之后，这才是「钱花在哪」的正确视图——
    `llm_calls.purpose` 只能告诉你花在哪类调用上，说不出是哪个任务触发的。"""
    rows = conn.execute(
        "SELECT COALESCE(schedule_name, '(手动)') AS name, COUNT(*) AS runs, "
        "SUM(llm_calls) AS calls, SUM(tool_calls) AS tools, "
        "ROUND(SUM(cost_usd), 4) AS cost "
        "FROM agent_runs WHERE started_at >= datetime('now', ?) "
        "GROUP BY name ORDER BY cost DESC",
        (f"-{int(days)} days",),
    )
    return [dict(r) for r in rows]


def approval_counts(conn: sqlite3.Connection) -> list[dict]:
    """每个 GATED 工具被批准执行过多少次、被拒过多少次。

    §0「权限逐级放开」的依据：批准过约 20 次、没有一次是它自作主张，
    就可以把这个工具从 GATED 降到 WRITE。有了这张表，这个次数是数出来的，
    不是凭感觉。
    """
    rows = conn.execute(
        "SELECT tool_name, "
        "SUM(CASE WHEN kind = 'denied' THEN 1 ELSE 0 END) AS denied, "
        "SUM(CASE WHEN kind = 'tool_result' THEN 1 ELSE 0 END) AS executed "
        "FROM agent_steps WHERE tool_name IS NOT NULL "
        "GROUP BY tool_name ORDER BY executed DESC"
    )
    return [dict(r) for r in rows]
