"""OA、面试这类待办的截止时间，以及按截止时间排的提醒。

sweep 只管分类和记表；这里管「还有什么要你做、什么时候截止」：

    extract_pending  对需要你动手的邮件（OA、面试邀请、约时间、offer）单独调一次模型，
                     抽出任务名和每个相关时间（换算成绝对时间）。提醒时才抽，旧邮件也就一并补上了
    verify_due       每个时间逐条确定性核对：原文确实在邮件里、能解析、月日和星期几跟原文对得上、
                     离收信时间不离谱。核对不过的只留原文、不倒计时——算错的倒计时比没有倒计时更害人
    pending          还没做完、没过截止时间、投递也没结束的，按截止时间排
    reminder_text    推送文本

推送里有从邮件派生的文字（摘要、任务名、时间原文）——这是有意的，你要看。
链接和邮箱地址先剥掉、长度截断；notify 那边 @ 和链接预览都关着。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .. import db
from ..agent.client import AgentClient, Budget, BudgetExceeded, MissingAPIKey
from . import classify
from .pipeline import _received, clean_name
from .policy import ACTION_TYPES

TYPE_LABEL = {"oa_invite": "OA", "interview_invite": "面试", "scheduling": "约时间", "offer": "offer"}
KIND_LABEL = {"deadline": "截止", "event": "时间", "other": "日期"}

#: 没写截止时间的待办，收到多少天后不再提醒（做完了可以提前 remind done）
NO_DEADLINE_DAYS = 14

#: 投递已经走到这些状态，这类待办就算过去了
_PAST: dict[str, frozenset[str]] = {
    "oa_invite": frozenset({"phone_screen", "interview_loop", "onsite", "offer", "rejected", "withdrawn"}),
    "interview_invite": frozenset({"offer", "rejected", "withdrawn"}),
    "scheduling": frozenset({"rejected", "withdrawn"}),
    "offer": frozenset({"rejected", "withdrawn"}),
}

#: 做完之后追加到投递上的事件
_DONE_EVENT = {"oa_invite": "oa_completed", "interview_invite": "interview_completed"}

_WEEKDAY_CN = "一二三四五六日"
_MONTHS = "(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug|" \
          "september|sept|sep|october|oct|november|nov|december|dec)"
_MONTH_NO = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_MONTH_DAY = re.compile(r"\b" + _MONTHS + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b")
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTHS + r"\b")
_NUMERIC = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/\d{2,4})?\b")
_WEEKDAY = re.compile(r"\b(monday|mon|tuesday|tues|tue|wednesday|wed|thursday|thurs|thu|"
                      r"friday|fri|saturday|sat|sunday|sun)\b")
_WEEKDAY_NO = {w: i for i, w in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))}
_LINKS = re.compile(r"(https?://|www\.)\S+|\S+@\S+\.\w+", re.I)


def _clip(text: Any, n: int) -> str:
    """推送前的清理：剥掉链接和邮箱地址、压空白、截断。"""
    s = " ".join(_LINKS.sub("", str(text or "")).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def verify_due(
    original: Any, iso: Any, email_text: str, received: datetime | None, *, kind: str = "other",
) -> dict[str, Any] | None:
    """核对模型换算出来的一个时间。返回 {text, kind, due_at, date_only}；原文不在邮件里就返回 None。

    due_at 为 None 表示「原文可信、换算不可信」：只显示原文，不倒计时。
    """
    text = " ".join(str(original or "").split())
    if not 2 <= len(text) <= 150 or text.lower() not in " ".join((email_text or "").split()).lower():
        return None                                        # 邮件里没有这句：编的
    item = {"text": _clip(text, 100), "kind": kind if kind in KIND_LABEL else "other",
            "due_at": None, "date_only": False}
    raw = str(iso or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return item
    date_only = len(raw) <= 10
    if date_only:
        parsed = parsed.replace(hour=23, minute=59)       # 只写了日期：当天结束前

    # 原文写了几月几号、星期几的，换算结果必须对得上（按原文的时区比）
    low = text.lower()
    days = {(_MONTH_NO[m[:3]], int(d)) for m, d in _MONTH_DAY.findall(low)}
    days |= {(_MONTH_NO[m[:3]], int(d)) for d, m in _DAY_MONTH.findall(low)}
    days |= {(int(m), int(d)) for m, d in _NUMERIC.findall(low)}
    if days and (parsed.month, parsed.day) not in days:
        return item
    weekdays = {_WEEKDAY_NO[w[:3]] for w in _WEEKDAY.findall(low)}
    if weekdays and parsed.weekday() not in weekdays:
        return item

    due = parsed if parsed.tzinfo else parsed.astimezone()  # 没写时区按本机时区
    if received is not None:
        ref = received if received.tzinfo else received.astimezone()
        if not ref - timedelta(days=1) <= due <= ref + timedelta(days=180):
            return item                                    # 年份写错这类：离收信时间太远
    item.update(due_at=due.isoformat(timespec="minutes"), date_only=date_only)
    return item


def _as_list(value: Any, key: str) -> list[Any]:
    """模型有时把数组、甚至整个对象当成 JSON 字符串塞进来（实测）。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if isinstance(value, dict):
        value = value.get(key) or []
    return value if isinstance(value, list) else []


def _todo_sql(extra: str = "") -> tuple[str, tuple[str, ...]]:
    types = tuple(sorted(ACTION_TYPES))
    return (f"e.classification IN ({','.join('?' * len(types))}) AND e.done_at IS NULL "
            f"AND COALESCE(e.review_status, '') != 'dismissed' {extra}"), types


def extract_pending(
    conn: sqlite3.Connection, *, client: AgentClient | None = None,
    budget: Budget | None = None, limit: int = 30,
) -> int:
    """给还没抽过的待办邮件抽任务名和时间。返回这次抽了几封；出错的下次再试。"""
    where, params = _todo_sql("AND e.task_json IS NULL")
    rows = conn.execute(f"SELECT e.* FROM emails e WHERE {where} ORDER BY e.received_at DESC LIMIT ?",
                        (*params, limit)).fetchall()
    if not rows:
        return 0
    client = client or AgentClient()
    budget = budget or Budget(max_llm_calls=len(rows) + 1)
    done = 0
    for r in rows:
        received = _received(r)
        try:
            data = classify.extract_task(
                client, subject=r["subject"] or "", from_addr=r["from_addr"] or "",
                body=r["body_text"] or "", budget=budget, conn=conn, email_id=r["id"],
                received_at=received.astimezone().isoformat(timespec="minutes") if received else "",
            )
        except (MissingAPIKey, BudgetExceeded):
            raise
        except Exception:
            continue
        text = f"{r['from_addr']}\n{r['subject']}\n{r['body_text']}"
        items = []
        for d in _as_list(data.get("deadlines"), "deadlines"):
            if isinstance(d, dict):
                item = verify_due(d.get("original"), d.get("iso"), text, received,
                                  kind=str(d.get("kind") or "other"))
                if item:
                    items.append(item)
        task = clean_name(data.get("task"), text, max_words=8, max_len=60)
        conn.execute("UPDATE emails SET task_json = ? WHERE id = ?",
                     (db.dump_json({"task": task, "deadlines": items}), r["id"]))
        conn.commit()
        done += 1
    return done


@dataclass
class Task:
    email_ids: list[int]
    ctype: str
    company: str
    title: str
    label: str                 # 任务名：邮件原文的关键词，比如 HackerRank assessment
    summary: str
    due: datetime | None
    due_text: str
    kind: str
    date_only: bool
    received: datetime | None


def _order(t: Task) -> tuple[int, float]:
    """有截止时间的按时间排在前；没有的按收信时间，新的在前。"""
    if t.due is not None:
        return 0, t.due.timestamp()
    return 1, -(t.received.timestamp() if t.received else 0.0)


def pending(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[Task]:
    now = now or datetime.now().astimezone()
    where, params = _todo_sql()
    rows = conn.execute(
        "SELECT e.*, a.status AS app_status, c.name AS company, j.title AS job_title FROM emails e "
        "LEFT JOIN applications a ON a.id = e.matched_application_id "
        "LEFT JOIN jobs j ON j.id = a.job_id LEFT JOIN companies c ON c.id = j.company_id "
        f"WHERE {where} ORDER BY e.received_at", params,
    ).fetchall()

    groups: dict[tuple[Any, ...], Task] = {}
    for r in rows:
        ctype = r["classification"]
        if r["app_status"] in _PAST[ctype]:
            continue
        info = json.loads(r["task_json"]) if r["task_json"] else {}
        items = info.get("deadlines") or []
        dated = sorted(((datetime.fromisoformat(d["due_at"]), d) for d in items if d.get("due_at")),
                       key=lambda x: x[0])
        upcoming = [x for x in dated if x[0] > now]
        received = _received(r)
        if dated and not upcoming:
            continue                                      # 截止时间都过了
        if not dated and received and now - received.astimezone() > timedelta(days=NO_DEADLINE_DAYS):
            continue                                      # 没写截止时间，又放了很久
        main = [x for x in upcoming if x[1].get("kind") in ("deadline", "event")] or upcoming
        due, d = main[0] if main else (None, items[0] if items else {})
        text = f"{r['from_addr']}\n{r['subject']}\n{r['body_text']}"
        task = Task(
            email_ids=[r["id"]], ctype=ctype,
            company=r["company"] or clean_name(r["company_hint"], text, max_words=6) or "?",
            title=r["job_title"] or "未匹配到投递",
            label=info.get("task") or "", summary=_clip(r["summary"], 120),
            due=due, due_text=d.get("text") or "", kind=d.get("kind") or "other",
            date_only=bool(d.get("date_only")), received=received,
        )
        # 同一条投递的同类邮件（邀请、催促）合成一条，留截止时间最早的那封
        key = (r["matched_application_id"], ctype) if r["matched_application_id"] else ("email", r["id"])
        prev = groups.get(key)
        if prev is not None:
            keep = task if _order(task) < _order(prev) else prev
            keep.email_ids = sorted(set(prev.email_ids + task.email_ids))
            task = keep
        groups[key] = task
    return sorted(groups.values(), key=_order)


def _when(dt: datetime, date_only: bool) -> str:
    local = dt.astimezone()
    day = f"{local.month}/{local.day} 周{_WEEKDAY_CN[local.weekday()]}"
    return f"{day}（当天结束前）" if date_only else f"{day} {local:%H:%M}"


def _left(delta: timedelta) -> str:
    mins = max(0, int(delta.total_seconds() // 60))
    d, rest = divmod(mins, 1440)
    h, m = divmod(rest, 60)
    if d >= 2:
        return f"还剩 {d} 天"
    if d == 1:
        return f"还剩 1 天 {h} 小时"
    return f"⚠ 还剩 {h} 小时 {m} 分" if h else f"⚠ 还剩 {m} 分钟"


def reminder_text(tasks: list[Task], *, now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    lines = [f"⏰ {len(tasks)} 件待办，按截止时间排："]
    for n, t in enumerate(tasks, 1):
        what = TYPE_LABEL.get(t.ctype, t.ctype) + (f" · {t.label}" if t.label else "")
        ids = " ".join(f"#{i}" for i in t.email_ids)
        lines += ["", f"{n}. {t.company} — {_clip(t.title, 70)}  [{what}]  {ids}"]
        if t.due is not None:
            lines.append(f"   {KIND_LABEL.get(t.kind, '日期')} {_when(t.due, t.date_only)} · {_left(t.due - now)}"
                         + (f"（原文：{t.due_text}）" if t.due_text else ""))
        elif t.due_text:
            lines.append(f"   时间：{t.due_text}（没能换算成具体日期，自己看一眼）")
        else:
            days = f" · 收到 {(now - t.received.astimezone()).days} 天" if t.received else ""
            lines.append(f"   没写截止时间{days}")
        if t.summary:
            lines.append(f"   {t.summary}")
    lines += ["", "做完了：agent remind done <邮件编号>"]
    return "\n".join(lines)


def mark_done(conn: sqlite3.Connection, email_id: int, *, now: datetime | None = None) -> dict[str, Any]:
    """做完了。同一条投递的同类邮件（邀请、催促）一起标掉；OA、面试顺带在投递上记一条完成事件。"""
    row = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
    if row is None:
        raise ValueError(f"没有 id 为 {email_id} 的邮件")
    ctype = row["classification"]
    if ctype not in ACTION_TYPES:
        raise ValueError(f"邮件 #{email_id} 不是待办（{ctype}）")
    ts = (now or datetime.now()).isoformat(sep=" ", timespec="seconds")
    app = row["matched_application_id"]
    if app:
        cur = conn.execute("UPDATE emails SET done_at = ? WHERE matched_application_id = ? "
                           "AND classification = ? AND done_at IS NULL", (ts, app, ctype))
    else:
        cur = conn.execute("UPDATE emails SET done_at = ? WHERE id = ? AND done_at IS NULL", (ts, email_id))
    conn.commit()
    event = _DONE_EVENT.get(ctype) if app and cur.rowcount else None
    if event:
        db.append_event(conn, app, event, occurred_at=now, source="manual", raw_ref=str(email_id),
                        payload={"email_id": email_id})
    return {"email_id": email_id, "marked": cur.rowcount, "event": event}
