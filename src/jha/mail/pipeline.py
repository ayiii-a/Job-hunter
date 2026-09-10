"""邮件管线：拉取 → 存正文 → 预过滤 → 分类 → 匹配 → 按误判代价更新。外加人工确认队列。

## 人工确认队列只在 CLI 里

面试邀请、OA、offer 进队列之后，**确认它们的函数不是 agent 的工具**。
和简历的审核门同一个道理：人工确认如果 agent 能自己做，那就不是人工确认。

## 给 agent 的东西里没有邮件原文

`queue_for_agent` 和 `SweepReport.compact` 只含分类结果、置信度、匹配到的投递、
以及公司和岗位名——后两者**来自我们自己的数据库**，不是邮件里的字。
**连主题行都不给**：主题行一样是不可信输入。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .. import db
from .imap import MailReader, RawEmail
from . import match as match_mod
from . import policy, prefilter
from . import classify
from ..agent.client import AgentClient, Budget


@dataclass
class SweepReport:
    filtered: int = 0
    classified: int = 0
    auto_applied: int = 0
    queued: int = 0
    ignored: int = 0
    alerts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def compact(self) -> dict[str, Any]:
        return {
            "filtered_out": self.filtered, "classified": self.classified,
            "auto_applied": self.auto_applied, "queued_for_human": self.queued,
            "ignored": self.ignored, "alerts": self.alerts, "errors": self.errors[:5],
        }


# ---------------------------------------------------------------------------
# 拉取与存储
# ---------------------------------------------------------------------------

def store_raw(conn: sqlite3.Connection, raw: RawEmail) -> int | None:
    """存下正文。**邮件删了就拿不回来**——没有正文就没有回归测试集。"""
    if raw.message_id and conn.execute(
        "SELECT 1 FROM emails WHERE message_id = ?", (raw.message_id,)
    ).fetchone():
        return None
    cur = conn.execute(
        "INSERT OR IGNORE INTO emails (uid, message_id, from_addr, from_domain, subject, "
        "body_text, received_at, links_json) VALUES (?,?,?,?,?,?,?,?)",
        (raw.uid, raw.message_id or None, raw.from_addr, raw.from_domain, raw.subject,
         raw.body_text, raw.received_at or None, db.dump_json(raw.links)),
    )
    conn.commit()
    return int(cur.lastrowid) if cur.rowcount else None


def ingest(
    conn: sqlite3.Connection, reader: MailReader, *,
    since_days: int = 14, limit: int = 200, now: datetime | None = None,
) -> tuple[int, int]:
    """拉取并存储。返回 (拉到几封, 新增几封)。重叠拉取靠去重兜住，所以宁可多拉一天。"""
    now = now or datetime.now()
    since: date = (now - timedelta(days=since_days)).date()
    last = conn.execute("SELECT MAX(received_at) m FROM emails").fetchone()["m"]
    if last:
        try:
            since = max(since, (db._parse_dt(last) - timedelta(days=1)).date())
        except (ValueError, TypeError):
            pass
    raws = reader.fetch_since(since, limit=limit)
    new = sum(1 for r in raws if store_raw(conn, r) is not None)
    return len(raws), new


# ---------------------------------------------------------------------------
# 处理
# ---------------------------------------------------------------------------

def _received(row: sqlite3.Row) -> datetime | None:
    try:
        return db._parse_dt(row["received_at"]) if row["received_at"] else None
    except (ValueError, TypeError):
        return None


def process_pending(
    conn: sqlite3.Connection, *, client: AgentClient | None = None,
    budget: Budget | None = None, limit: int = 60,
) -> SweepReport:
    rep = SweepReport()
    rows = conn.execute(
        "SELECT * FROM emails WHERE policy IS NULL ORDER BY received_at LIMIT ?", (limit,)
    ).fetchall()
    if not rows:
        return rep

    # 1. 确定性预过滤。没过的一次 LLM 都不花
    domain_map = prefilter.company_domain_map(conn)
    todo: list[tuple[sqlite3.Row, prefilter.PrefilterResult]] = []
    for r in rows:
        pre = prefilter.screen(r, domain_map)
        if pre.passed:
            todo.append((r, pre))
        else:
            conn.execute("UPDATE emails SET policy = 'filtered', reason = ? WHERE id = ?",
                         (pre.reason, r["id"]))
            rep.filtered += 1
    conn.commit()
    if not todo:
        return rep

    client = client or AgentClient()
    budget = budget or Budget(max_llm_calls=len(todo) + 2)

    for r, pre in todo:
        subject, body, sender = r["subject"] or "", r["body_text"] or "", r["from_addr"] or ""

        # 2. 第二层分类（正文不进 agent 上下文）
        cls = classify.classify_one(client, subject=subject, from_addr=sender, body=body,
                                    budget=budget, conn=conn, email_id=r["id"])
        rep.classified += 1
        if cls.get("error"):
            rep.errors.append(f"邮件 #{r['id']}：{cls['error']}")

        # 3. 确定性匹配
        m = match_mod.match(conn, from_addr=sender, subject=subject, body=body,
                            company_id=pre.company_id, role_hint=cls.get("role_hint") or "")

        # 4. 按误判代价决定
        d = policy.decide(ctype=cls["type"], confidence=cls["confidence"],
                          match_status=m.status, text=f"{subject}\n{body}")

        event_id = None
        if d.action == "auto_apply" and d.event_type and m.application_id:
            event_id = db.append_event(
                conn, m.application_id, d.event_type, occurred_at=_received(r),
                source="email", raw_ref=str(r["id"]),
                payload={"email_id": r["id"], "confidence": cls["confidence"]},
            )
            if cls["type"] == "confirmation":
                conn.execute(
                    "UPDATE applications SET confirmation_seen_at = COALESCE(confirmation_seen_at, ?) "
                    "WHERE id = ?", (r["received_at"], m.application_id),
                )
            rep.auto_applied += 1
        elif d.action == "queue":
            rep.queued += 1
        else:
            rep.ignored += 1

        conn.execute(
            "UPDATE emails SET classification=?, confidence=?, role_hint=?, summary=?, "
            "dates_json=?, action_required=?, matched_application_id=?, policy=?, "
            "review_status=?, reason=?, event_id=?, classifier_version=? WHERE id=?",
            (cls["type"], cls["confidence"], cls.get("role_hint"), cls.get("summary"),
             db.dump_json(cls.get("dates") or []), 1 if cls.get("action_required") else 0,
             m.application_id, d.action, "pending" if d.action == "queue" else None,
             d.reason + (f"；{m.reason}" if m.reason else ""), event_id,
             classify.CLASSIFIER_VERSION, r["id"]),
        )
        conn.commit()
        if d.alert:
            rep.alerts.append(_compact(conn, r["id"]))
    return rep


# ---------------------------------------------------------------------------
# 人工确认队列
# ---------------------------------------------------------------------------

_QUEUE_SQL = """
SELECT e.id, e.classification, e.confidence, e.matched_application_id, e.reason,
       e.received_at, c.name AS company, j.title AS job_title
FROM emails e
LEFT JOIN applications a ON a.id = e.matched_application_id
LEFT JOIN jobs j ON j.id = a.job_id
LEFT JOIN companies c ON c.id = j.company_id
"""

_PRIORITY = ("CASE e.classification WHEN 'offer' THEN 0 WHEN 'interview_invite' THEN 1 "
             "WHEN 'oa_invite' THEN 2 WHEN 'scheduling' THEN 3 ELSE 4 END")


def _row_for_agent(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "email_id": r["id"], "type": r["classification"],
        "confidence": round(r["confidence"] or 0.0, 2),
        "matched_application_id": r["matched_application_id"],
        "company": r["company"], "job_title": r["job_title"],   # 来自我们的库，不是邮件
        "reason": r["reason"],
    }


def _compact(conn: sqlite3.Connection, email_id: int) -> dict[str, Any]:
    r = conn.execute(_QUEUE_SQL + " WHERE e.id = ?", (email_id,)).fetchone()
    return _row_for_agent(r) if r else {"email_id": email_id}


def queue_for_agent(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        _QUEUE_SQL + f" WHERE e.review_status = 'pending' ORDER BY {_PRIORITY}, e.received_at"
    ).fetchall()
    return [_row_for_agent(r) for r in rows]


def queue_for_human(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """人看的版本，带主题和摘要。只给 CLI 用。"""
    return conn.execute(
        "SELECT e.*, c.name AS company, j.title AS job_title FROM emails e "
        "LEFT JOIN applications a ON a.id = e.matched_application_id "
        "LEFT JOIN jobs j ON j.id = a.job_id LEFT JOIN companies c ON c.id = j.company_id "
        f"WHERE e.review_status = 'pending' ORDER BY {_PRIORITY}, e.received_at"
    ).fetchall()


def accept(conn: sqlite3.Connection, email_id: int, *, application_id: int | None = None) -> dict[str, Any]:
    """人工确认一封邮件的建议。**不是 agent 的工具。**"""
    row = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
    if row is None:
        raise ValueError(f"没有 id 为 {email_id} 的邮件")
    if row["review_status"] != "pending":
        raise ValueError(f"邮件 #{email_id} 不在待确认队列里（{row['review_status']}）")

    app_id = application_id or row["matched_application_id"]
    if not app_id:
        raise ValueError(f"邮件 #{email_id} 没匹配到投递记录——用 --application <id> 指定")
    if conn.execute("SELECT 1 FROM applications WHERE id = ?", (app_id,)).fetchone() is None:
        raise ValueError(f"没有 id 为 {app_id} 的投递记录")

    event_type = policy.STATUS_EVENT.get(row["classification"] or "")
    if not event_type:
        raise ValueError(f"「{row['classification']}」类邮件没有对应的状态事件，用 dismiss")

    event_id = db.append_event(
        conn, app_id, event_type, occurred_at=_received(row), source="manual",
        raw_ref=str(email_id), payload={"email_id": email_id, "accepted_from_queue": True},
    )
    if row["classification"] == "confirmation":
        conn.execute("UPDATE applications SET confirmation_seen_at = COALESCE(confirmation_seen_at, ?) "
                     "WHERE id = ?", (row["received_at"], app_id))
    conn.execute("UPDATE emails SET review_status = 'accepted', matched_application_id = ?, "
                 "event_id = ? WHERE id = ?", (app_id, event_id, email_id))
    conn.commit()
    status = conn.execute("SELECT status FROM applications WHERE id = ?", (app_id,)).fetchone()
    return {"email_id": email_id, "application_id": app_id, "event": event_type,
            "status": status["status"] if status else None}


def dismiss(conn: sqlite3.Connection, email_id: int, *, note: str = "") -> dict[str, Any]:
    cur = conn.execute(
        "UPDATE emails SET review_status = 'dismissed', "
        "reason = COALESCE(reason, '') || ? WHERE id = ? AND review_status = 'pending'",
        (f"；人工驳回：{note}" if note else "；人工驳回", email_id),
    )
    if cur.rowcount == 0:
        raise ValueError(f"邮件 #{email_id} 不在待确认队列里")
    conn.commit()
    return {"email_id": email_id, "review_status": "dismissed"}
