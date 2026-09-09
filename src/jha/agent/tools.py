"""工具注册表 —— 同时也是这个 agent 的安全边界。

改成 agent loop 之后，路线图那几条原则（提交由人点、邮件只读、不点链接）
不能再靠 prompt 里写一句话来保证——模型可以忽略 prompt。它们现在靠
**这里不存在对应的工具**来保证。

## 刻意不存在的工具

    submit_application  —— 没有。提交永远由你在浏览器里按
    send_email / reply  —— 没有。邮件模块只会实现读取路径
    open_url / click    —— 没有。邮件里的链接原样呈现给你，agent 不点
    delete_* / update_event —— 没有。events 表连数据库层面都禁止改删

模型再怎么被 JD 或邮件正文里的注入内容诱导，也调不出不存在的函数。
**加工具之前先问一句：这个能力被滥用的最坏后果是什么。**

## 三档权限

    READ   只读本地数据，随便调
    WRITE  写本地数据库。可以自动执行——因为 events 是追加式的，误写能靠
           追加更正事件来修，代价低
    GATED  外发或不可逆。必须人工批准，未批准时返回「需要批准」给模型，
           让它自己决定是问你还是换条路
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable

from .. import db, ingest, notify, profile


class Permission(str, Enum):
    READ = "read"
    WRITE = "write"
    GATED = "gated"


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    permission: Permission
    fn: Callable[..., Any]

    def spec(self) -> dict[str, Any]:
        """Anthropic Messages API 的 tools 格式。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, permission: Permission, schema: dict[str, Any]):
    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        REGISTRY[name] = Tool(name, description, schema, permission, fn)
        return fn

    return wrap


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


STR = {"type": "string"}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}


# ---------------------------------------------------------------------------
# READ
# ---------------------------------------------------------------------------

@tool(
    "list_companies",
    "列出目标公司清单，含 ATS 类型、board_token、邮件域名和优先级。",
    Permission.READ,
    _obj({"active_only": BOOL}),
)
def list_companies(conn: sqlite3.Connection, active_only: bool = True) -> list[dict]:
    sql = "SELECT id, name, ats_type, board_token, email_domains_json, priority, is_active FROM companies"
    if active_only:
        sql += " WHERE is_active = 1"
    return [
        {**dict(r), "email_domains": db.load_json(r["email_domains_json"])}
        for r in conn.execute(sql + " ORDER BY priority, name")
    ]


@tool(
    "list_jobs",
    "列出库里的岗位。可按公司、初筛档位（tier1_ai_engineer 等）过滤。"
    "不返回 JD 全文——要看全文用 get_job。",
    Permission.READ,
    _obj({"company": STR, "tier": STR, "active_only": BOOL, "limit": INT}),
)
def list_jobs(
    conn: sqlite3.Connection,
    company: str | None = None,
    tier: str | None = None,
    active_only: bool = True,
    limit: int = 50,
) -> list[dict]:
    sql = (
        "SELECT j.id, c.name AS company, j.title, j.location, j.remote_type, "
        "j.salary_raw, j.screen_tier, j.url, j.posted_at, j.first_seen_at, j.is_active, "
        "j.is_shortlisted, length(j.jd_text) AS jd_chars "
        "FROM jobs j LEFT JOIN companies c ON c.id = j.company_id WHERE 1=1 "
    )
    params: list[Any] = []
    if active_only:
        sql += "AND j.is_active = 1 "
    if company:
        sql += "AND lower(c.name) = lower(?) "
        params.append(company)
    if tier:
        sql += "AND j.screen_tier = ? "
        params.append(tier)
    sql += "ORDER BY j.screen_tier, j.first_seen_at DESC LIMIT ?"
    params.append(min(int(limit), 200))
    return [dict(r) for r in conn.execute(sql, params)]


@tool(
    "get_job",
    "取单个岗位的完整信息，包含 JD 全文，以及这家公司有没有可以内推的人。",
    Permission.READ,
    _obj({"job_id": INT}, ["job_id"]),
)
def get_job(conn: sqlite3.Connection, job_id: int) -> dict:
    row = conn.execute(
        "SELECT j.*, c.name AS company FROM jobs j "
        "LEFT JOIN companies c ON c.id = j.company_id WHERE j.id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"没有 id 为 {job_id} 的岗位")
    out = {k: row[k] for k in row.keys() if k != "company_id"}
    out["contacts"] = [
        dict(c)
        for c in conn.execute(
            "SELECT name, relationship, strength, last_contacted_at FROM contacts "
            "WHERE company_id = ? ORDER BY strength DESC",
            (row["company_id"],),
        )
    ]
    return out


@tool(
    "list_contacts",
    "列出内推线索。任何岗位在建议「去投」之前都该先查这个——"
    "内推的面试转化率显著高于冷投。",
    Permission.READ,
    _obj({"company": STR}),
)
def list_contacts(conn: sqlite3.Connection, company: str | None = None) -> list[dict]:
    sql = (
        "SELECT ct.id, c.name AS company, ct.name, ct.relationship, ct.strength, "
        "ct.last_contacted_at, ct.notes FROM contacts ct "
        "LEFT JOIN companies c ON c.id = ct.company_id WHERE 1=1 "
    )
    params: list[Any] = []
    if company:
        sql += "AND lower(c.name) = lower(?) "
        params.append(company)
    return [dict(r) for r in conn.execute(sql + "ORDER BY ct.strength DESC", params)]


@tool(
    "get_target_profile",
    "取目标画像：岗位方向分档、地点、签证情况、deal-breakers。",
    Permission.READ,
    _obj({}),
)
def get_target_profile(conn: sqlite3.Connection) -> dict:
    return profile.load_target_profile()


@tool(
    "get_master_profile",
    "取母简历。bullets 带全局唯一 id —— 定制简历时只能引用这些 id，"
    "绝不能自己写新的 bullet 文本。",
    Permission.READ,
    _obj({"section": {"type": "string", "enum": ["all", "bullets", "qa_bank", "story_bank"]}}),
)
def get_master_profile(conn: sqlite3.Connection, section: str = "all") -> Any:
    data = profile.load_master_profile()
    if section == "qa_bank":
        return data.get("qa_bank") or {}
    if section == "story_bank":
        return data.get("story_bank") or []
    if section == "bullets":
        out = []
        for kind in ("experiences", "projects"):
            for entry in data.get(kind) or []:
                for b in entry.get("bullets") or []:
                    out.append(
                        {
                            "id": b.get("id"),
                            "owner": entry.get("id"),
                            "text": b.get("text"),
                            "skills": b.get("skills") or [],
                            "has_metrics": profile._has_metrics(b),
                            "metric_evidence": profile.metric_evidence(b),
                        }
                    )
        return out
    return data


@tool(
    "list_applications",
    "列出投递记录及当前状态。status 是由 events 推导出来的缓存。",
    Permission.READ,
    _obj({"status": STR}),
)
def list_applications(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    sql = (
        "SELECT a.id, c.name AS company, j.title, a.status, a.applied_at, a.applied_via, "
        "a.confirmation_seen_at, a.notes FROM applications a "
        "JOIN jobs j ON j.id = a.job_id LEFT JOIN companies c ON c.id = j.company_id WHERE 1=1 "
    )
    params: list[Any] = []
    if status:
        sql += "AND a.status = ? "
        params.append(status)
    return [dict(r) for r in conn.execute(sql + "ORDER BY a.applied_at DESC", params)]


@tool(
    "get_fetch_health",
    "看抓取器健康状况：最近一次抓取失败的公司。"
    "抓取静默失败是最危险的失败模式——你会以为最近没新岗位，实际适配器挂了两周。",
    Permission.READ,
    _obj({}),
)
def get_fetch_health(conn: sqlite3.Connection) -> dict:
    failures = [dict(r) for r in ingest.failing_sources(conn)]
    recent = [
        dict(r)
        for r in conn.execute(
            "SELECT r.source, c.name AS company, r.started_at, r.ok, r.listed_count, "
            "r.kept_count, r.new_count, r.detail_fetches, r.error "
            "FROM fetch_runs r LEFT JOIN companies c ON c.id = r.company_id "
            "ORDER BY r.started_at DESC, r.id DESC LIMIT 10"
        )
    ]
    return {"failing": failures, "recent_runs": recent}


# ---------------------------------------------------------------------------
# WRITE —— 只动本地数据库
# ---------------------------------------------------------------------------

@tool(
    "fetch_jobs",
    "跑抓取管线：拉取 → 规则初筛 → 增量补 JD 全文 → 入库 → 过期检测。"
    "不传 company 就抓全部启用的公司。返回每家的统计。",
    Permission.WRITE,
    _obj({"company": STR, "dry_run": BOOL}),
)
def fetch_jobs(
    conn: sqlite3.Connection, company: str | None = None, dry_run: bool = False
) -> list[dict]:
    target = profile.load_target_profile()
    reports = ingest.fetch_all(conn, target, only=company, dry_run=dry_run)
    return [
        {
            "company": r.company,
            "source": r.source,
            "ok": r.ok,
            "listed": r.listed,
            "kept_after_screening": r.kept,
            "new": r.new,
            "updated": r.updated,
            "detail_fetches": r.detail_fetches,
            "deactivated": r.deactivated,
            "screen_reasons": r.screen_reasons,
            "error": r.error,
        }
        for r in reports
    ]


@tool(
    "shortlist_job",
    "把岗位标记为「感兴趣但还没投」。注意这【不是】投递——"
    "还没投就不建 application 记录，否则投递数统计会被污染。",
    Permission.WRITE,
    _obj({"job_id": INT, "shortlisted": BOOL}, ["job_id"]),
)
def shortlist_job(conn: sqlite3.Connection, job_id: int, shortlisted: bool = True) -> dict:
    cur = conn.execute(
        "UPDATE jobs SET is_shortlisted = ? WHERE id = ?", (1 if shortlisted else 0, job_id)
    )
    if cur.rowcount == 0:
        raise ValueError(f"没有 id 为 {job_id} 的岗位")
    conn.commit()
    return {"job_id": job_id, "is_shortlisted": shortlisted}


@tool(
    "record_application",
    "记录一次【已经由人手动完成】的投递。这个工具不会替你提交任何表单——"
    "它只是把你投过的事实落库，并写一条 applied 事件。",
    Permission.WRITE,
    _obj(
        {
            "job_id": INT,
            "applied_via": {
                "type": "string",
                "enum": ["referral", "company_site", "ats_direct", "recruiter", "other"],
            },
            "applied_at": STR,
            "notes": STR,
        },
        ["job_id", "applied_via"],
    ),
)
def record_application(
    conn: sqlite3.Connection,
    job_id: int,
    applied_via: str,
    applied_at: str | None = None,
    notes: str | None = None,
) -> dict:
    if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
        raise ValueError(f"没有 id 为 {job_id} 的岗位")
    if conn.execute("SELECT 1 FROM applications WHERE job_id = ?", (job_id,)).fetchone():
        raise ValueError(f"岗位 {job_id} 已经有投递记录了")

    ts = applied_at or datetime.now().isoformat(sep=" ", timespec="seconds")
    cur = conn.execute(
        "INSERT INTO applications (job_id, applied_at, applied_via, notes) VALUES (?,?,?,?)",
        (job_id, ts, applied_via, notes),
    )
    app_id = int(cur.lastrowid)
    db.append_event(conn, app_id, "applied", occurred_at=datetime.fromisoformat(ts), source="agent")
    return {"application_id": app_id, "job_id": job_id, "status": "applied"}


@tool(
    "append_event",
    "给投递记录追加一条事件，status 会自动重算。"
    "events 是追加式的：要更正之前的误判，追加 status_override 事件（payload 带 status），"
    "而不是修改历史——数据库层面也不允许修改。",
    Permission.WRITE,
    _obj(
        {
            "application_id": INT,
            "type": STR,
            "occurred_at": STR,
            "payload": {"type": "object"},
            "source": {"type": "string", "enum": ["email", "manual", "agent"]},
        },
        ["application_id", "type"],
    ),
)
def append_event(
    conn: sqlite3.Connection,
    application_id: int,
    type: str,
    occurred_at: str | None = None,
    payload: dict | None = None,
    source: str = "agent",
) -> dict:
    from ..status import KNOWN_EVENT_TYPES

    if type not in KNOWN_EVENT_TYPES:
        raise ValueError(
            f"未登记的事件类型 {type!r}。已登记的：{sorted(KNOWN_EVENT_TYPES)}"
        )
    when = datetime.fromisoformat(occurred_at) if occurred_at else None
    db.append_event(conn, application_id, type, occurred_at=when, source=source, payload=payload)
    status = conn.execute(
        "SELECT status FROM applications WHERE id = ?", (application_id,)
    ).fetchone()
    return {"application_id": application_id, "event": type, "status": status["status"] if status else None}


# ---------------------------------------------------------------------------
# GATED —— 外发，必须人工批准
# ---------------------------------------------------------------------------

@tool(
    "send_notification",
    "把一条摘要推送到你配置的 Telegram。这是外发动作，需要人工批准。",
    Permission.GATED,
    _obj({"text": STR}, ["text"]),
)
def send_notification(conn: sqlite3.Connection, text: str) -> dict:
    res = notify.send(text)
    return {"sent": res.sent, "channel": res.channel, "detail": res.detail}


# ---------------------------------------------------------------------------

def specs(allow: set[Permission] | None = None) -> list[dict[str, Any]]:
    """给 Messages API 的 tools 参数。allow 可以进一步收窄可用工具。"""
    return [
        t.spec()
        for t in REGISTRY.values()
        if allow is None or t.permission in allow
    ]


def execute(name: str, args: dict[str, Any], conn: sqlite3.Connection) -> str:
    """执行一个工具，把结果序列化成给模型看的字符串。"""
    t = REGISTRY.get(name)
    if t is None:
        raise KeyError(f"没有名为 {name} 的工具")
    result = t.fn(conn, **args)
    return json.dumps(result, ensure_ascii=False, default=str)
