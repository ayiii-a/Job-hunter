"""工具注册表 —— 同时也是这个 agent 的安全边界。

改成 agent loop 之后，路线图那几条原则（提交由人点、邮件只读、不点链接）
不能再靠 prompt 里写一句话来保证——模型可以忽略 prompt。它们现在靠
**这里不存在对应的工具**来保证。

## 刻意不存在的工具

    submit_application  —— 没有。提交永远由你在浏览器里按
    send_email / reply  —— 没有。邮件模块只会实现读取路径
    open_url / click    —— 没有。邮件里的链接原样呈现给你，agent 不点
    delete_* / update_event —— 没有。events 表连数据库层面都禁止改删
    approve_resume      —— 没有。审核门如果 agent 能自己过，那就不是门。
                          它只在 CLI 里：agent resume approve <id>
    accept_email        —— 没有。面试邀请 / OA / offer 的人工确认只在 CLI 里：
                          agent mail accept <id>。人工确认如果 agent 能做，就不是人工确认

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

from .. import analyze, db, ingest, notify, prep, profile, questions, tailor, tracking


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
    "取单个岗位的信息，以及这家公司有没有可以内推的人。"
    "默认【不返回 JD 全文】——要做匹配判断请用 analyze_jobs，它在工具内部分析，"
    "比把 JD 拉进对话便宜一个量级。只有需要引用原文时才 include_jd=true，且一次一条。",
    Permission.READ,
    _obj({"job_id": INT, "include_jd": BOOL}, ["job_id"]),
)
def get_job(conn: sqlite3.Connection, job_id: int, include_jd: bool = False) -> dict:
    row = conn.execute(
        "SELECT j.*, c.name AS company FROM jobs j "
        "LEFT JOIN companies c ON c.id = j.company_id WHERE j.id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"没有 id 为 {job_id} 的岗位")
    out = {k: row[k] for k in row.keys() if k != "company_id"}

    # JD 全文默认不给：单条约 1,620 tokens，而 agent loop 每轮都要重发
    # 完整历史，逐条拉进上下文的成本是复利的（读 20 条 8.3 倍）。见路线图 §1.1
    jd = out.pop("jd_text", None) or ""
    out["jd_chars"] = len(jd)
    if include_jd:
        out["jd_text"] = jd
    else:
        out["jd_preview"] = jd[:400]
        out["jd_hint"] = "JD 全文未返回。要判断匹配度请用 analyze_jobs；确需原文再 include_jd=true"
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
    "看抓取器和邮件检测的健康状况：最近一次抓取失败的公司；邮件检测上次成功是什么时候、"
    "是否已经过期（mail.stale）。静默失败是最危险的失败模式——你会以为最近没新岗位、"
    "没面试邀请，实际是抓取器或邮件检测挂了。",
    Permission.READ,
    _obj({}),
)
def get_fetch_health(conn: sqlite3.Connection) -> dict:
    from ..mail import pipeline as mail_pipeline

    failures = [dict(r) for r in ingest.failing_sources(conn)]
    recent = [
        dict(r)
        for r in conn.execute(
            "SELECT r.source, c.name AS company, r.started_at, r.ok, r.listed_count, "
            "r.kept_count, r.new_count, r.detail_fetches, r.error "
            "FROM fetch_runs r LEFT JOIN companies c ON c.id = r.company_id "
            "WHERE r.source != 'imap' "
            "ORDER BY r.started_at DESC, r.id DESC LIMIT 10"
        )
    ]
    return {"failing": failures, "recent_runs": recent, "mail": mail_pipeline.mail_health(conn)}


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
            "resume_version_id": INT,
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
    resume_version_id: int | None = None,
    applied_at: str | None = None,
    notes: str | None = None,
) -> dict:
    if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
        raise ValueError(f"没有 id 为 {job_id} 的岗位")
    if conn.execute("SELECT 1 FROM applications WHERE job_id = ?", (job_id,)).fetchone():
        raise ValueError(f"岗位 {job_id} 已经有投递记录了")

    # 审核门要有牙齿：未审核的简历版本不许被绑到投递记录上。
    # 否则「人工审核」就只是个没人查的字段。
    if resume_version_id is not None:
        rv = conn.execute(
            "SELECT approved_at FROM resume_versions WHERE id = ?", (resume_version_id,)
        ).fetchone()
        if rv is None:
            raise ValueError(f"没有 id 为 {resume_version_id} 的简历版本")
        if not rv["approved_at"]:
            raise ValueError(
                f"简历版本 {resume_version_id} 还没过审核门。"
                "先让用户看 diff 再 agent resume approve —— agent 不能自己批准"
            )

    # 每日上限。**agent 不能突破，人可以**（CLI 的 --force）——
    # 上限的用途不是省力，是逼你投得准：一天 8 家才有时间给每家写像样的
    # 「Why this company」、查内推、看 JD。让 agent 自己决定要不要超，
    # 等于这条限制不存在。
    limit = tracking.daily_limit_status(conn)
    if limit["exceeded"]:
        raise ValueError(
            f"今天已投 {limit['used']} 家，达到上限 {limit['limit']}。"
            "agent 不能突破这个上限——确实要多投请用 agent applied <job_id> --force"
        )

    ts = applied_at or datetime.now().isoformat(sep=" ", timespec="seconds")
    cur = conn.execute(
        "INSERT INTO applications (job_id, resume_version_id, applied_at, applied_via, notes) "
        "VALUES (?,?,?,?,?)",
        (job_id, resume_version_id, ts, applied_via, notes),
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


@tool(
    "analyze_jobs",
    "分析岗位并给出匹配判定（strong_apply / apply / stretch / skip）+ gap 列表。"
    "不传 job_ids 就分析所有还没分析过的。"
    "分析在工具内部逐条进行，只返回紧凑结论——**不要为了做匹配判断而逐条拉 JD 全文**。",
    Permission.WRITE,
    _obj({"job_ids": {"type": "array", "items": INT}, "limit": INT}),
)
def analyze_jobs(
    conn: sqlite3.Connection, job_ids: list[int] | None = None, limit: int = 25
) -> dict:
    results = analyze.analyze_jobs(conn, job_ids=job_ids, limit=min(int(limit), 60))
    if not results:
        return {"analyzed": 0, "note": "没有需要分析的岗位（可能都分析过了）"}
    by_verdict: dict[str, int] = {}
    for r in results:
        by_verdict[r.verdict] = by_verdict.get(r.verdict, 0) + 1
    return {
        "analyzed": len(results),
        "by_verdict": by_verdict,
        "results": [r.compact() for r in results],
    }


@tool(
    "get_analysis",
    "取某个岗位的完整分析结果：技能要求、gap、red flags、JD 大白话讲解。",
    Permission.READ,
    _obj({"job_id": INT}, ["job_id"]),
)
def get_analysis(conn: sqlite3.Connection, job_id: int) -> dict:
    out = analyze.get_analysis(conn, job_id)
    if out is None:
        raise ValueError(f"岗位 {job_id} 还没有分析结果，先调 analyze_jobs")
    return out


@tool(
    "rank_jobs",
    "把已分析的岗位按匹配档位和分数排序，附带该公司有没有内推线索。"
    "档位相同的，有内推路径的排前面。",
    Permission.READ,
    _obj({"verdicts": {"type": "array", "items": STR}, "limit": INT}),
)
def rank_jobs(
    conn: sqlite3.Connection, verdicts: list[str] | None = None, limit: int = 25
) -> list[dict]:
    order = {"strong_apply": 0, "apply": 1, "stretch": 2, "skip": 3}
    wanted = verdicts or ["strong_apply", "apply", "stretch"]
    rows = conn.execute(
        "SELECT a.job_id, a.verdict, a.match_score, a.gaps_json, j.title, "
        "j.location, j.salary_raw, j.url, c.name AS company, c.id AS cid "
        "FROM job_analysis a JOIN jobs j ON j.id = a.job_id "
        "LEFT JOIN companies c ON c.id = j.company_id "
        "WHERE j.is_active = 1 AND a.scorer_version = ?",
        (analyze.ANALYZER_VERSION,),
    ).fetchall()

    out = []
    for r in rows:
        if r["verdict"] not in wanted:
            continue
        contacts = [
            dict(x) for x in conn.execute(
                "SELECT name, relationship, strength FROM contacts WHERE company_id = ? "
                "ORDER BY strength DESC", (r["cid"],)
            )
        ]
        out.append({
            "job_id": r["job_id"], "company": r["company"], "title": r["title"],
            "location": r["location"], "salary": r["salary_raw"], "url": r["url"],
            "verdict": r["verdict"], "match_score": r["match_score"],
            "gaps": db.load_json(r["gaps_json"])[:3],
            "referral": [c["name"] for c in contacts] or None,
        })
    # 档位优先；同档位有内推的排前面 —— §0「内推优先」
    out.sort(key=lambda x: (order.get(x["verdict"], 9), x["referral"] is None,
                            -(x["match_score"] or 0)))
    return out[: min(int(limit), 100)]


@tool(
    "tailor_resume",
    "为某个岗位定制一份简历：从母简历按 bullet id 选材、渲染 PDF、量页数、跑幻觉校验。"
    "生成的版本**未经审核**，必须由人看过 diff 后在 CLI 里批准才能投出去。",
    Permission.WRITE,
    _obj({"job_id": INT, "max_pages": INT}, ["job_id"]),
)
def tailor_resume(conn: sqlite3.Connection, job_id: int, max_pages: int = 1) -> dict:
    return tailor.tailor_resume(conn, job_id, max_pages=max(1, min(int(max_pages), 2))).compact()


@tool(
    "get_resume_version",
    "取某个简历版本：选中了哪些 bullet、页数、diff、有没有通过审核。",
    Permission.READ,
    _obj({"resume_version_id": INT}, ["resume_version_id"]),
)
def get_resume_version(conn: sqlite3.Connection, resume_version_id: int) -> dict:
    out = tailor.get_version(conn, resume_version_id)
    if out is None:
        raise ValueError(f"没有 id 为 {resume_version_id} 的简历版本")
    out["approved"] = bool(out.get("approved_at"))
    return out


@tool(
    "list_resume_versions",
    "列出已生成的简历版本。approved=false 的还没过审核门，不能拿去投。",
    Permission.READ,
    _obj({"job_id": INT, "limit": INT}),
)
def list_resume_versions(
    conn: sqlite3.Connection, job_id: int | None = None, limit: int = 25
) -> list[dict]:
    sql = (
        "SELECT rv.id, rv.generated_for_job_id, rv.page_count, rv.approved_at, "
        "rv.created_at, j.title, c.name AS company FROM resume_versions rv "
        "LEFT JOIN jobs j ON j.id = rv.generated_for_job_id "
        "LEFT JOIN companies c ON c.id = j.company_id WHERE 1=1 "
    )
    params: list[Any] = []
    if job_id:
        sql += "AND rv.generated_for_job_id = ? "
        params.append(job_id)
    sql += "ORDER BY rv.created_at DESC LIMIT ?"
    params.append(min(int(limit), 100))
    return [
        {**dict(r), "approved": bool(r["approved_at"])}
        for r in conn.execute(sql, params)
    ]


@tool(
    "get_tracking",
    "取投递追踪表：每条投递的状态、渠道、内推人、最近事件、简历版本，"
    "以及**下一步该做什么**（由规则算出，不是猜的）。",
    Permission.READ,
    _obj({}),
)
def get_tracking(conn: sqlite3.Connection) -> dict:
    rows = tracking.tracking_rows(conn)
    return {
        "count": len(rows),
        "daily_limit": tracking.daily_limit_status(conn),
        "rows": [
            {"application_id": r.application_id, "company": r.company, "title": r.title,
             "status": r.status, "applied_at": (r.applied_at or "")[:10],
             "applied_via": r.applied_via, "referral": r.referral or None,
             "last_event": r.last_event, "verdict": r.verdict,
             "next_step": r.next_step or None}
            for r in rows
        ],
    }


@tool(
    "check_confirmations",
    "找出投出去超过 24 小时、仍没收到确认邮件的投递。"
    "确认邮件是「申请真的进系统了」的唯一地面真相——没有它就可能是白投。",
    Permission.READ,
    _obj({"hours": INT}),
)
def check_confirmations(conn: sqlite3.Connection, hours: int = 24) -> dict:
    missing = tracking.missing_confirmations(conn, hours=max(1, int(hours)))
    return {"missing": missing, "count": len(missing)}


@tool(
    "mark_confirmed",
    "记下某条投递收到了确认邮件。会同时追加一条 confirmation_received 事件。",
    Permission.WRITE,
    _obj({"application_id": INT}, ["application_id"]),
)
def mark_confirmed(conn: sqlite3.Connection, application_id: int) -> dict:
    return tracking.mark_confirmed(conn, application_id)


@tool(
    "draft_answers",
    "给申请表的自定义问题起草答案。"
    "工作授权/签证/薪资类**只照抄 qa_bank 原文**；EEO 自愿披露类（种族、性别、"
    "退伍、残障）**一个字都不填**；其余没被 qa_bank 覆盖的一律留空标红由用户填。"
    "agent 绝不凭空编造答案。",
    Permission.READ,
    _obj({"questions": {"type": "array", "items": STR}, "company": STR, "role": STR},
         ["questions"]),
)
def draft_answers(
    conn: sqlite3.Connection, questions_: list[str] | None = None,
    company: str = "", role: str = "", **kw
) -> dict:
    qs = questions_ if questions_ is not None else kw.get("questions") or []
    return questions.summarize(
        questions.answer_all(list(qs), company=company, role=role)
    )


@tool(
    "sweep_emails",
    "拉取新邮件并处理：确定性预过滤 → 分类（在工具内部逐封进行，邮件正文不会进入你的上下文）"
    "→ 匹配到投递记录 → 按误判代价分级更新。只会自动写入确认邮件和拒信（可追加事件纠正）；"
    "面试邀请、OA、offer 一律进人工确认队列——你**没有**确认它们的能力，也不需要去做。",
    Permission.WRITE,
    _obj({"since_days": INT}),
)
def sweep_emails(conn: sqlite3.Connection, since_days: int = 14) -> dict:
    from ..mail import pipeline as mail_pipeline
    from ..mail.imap import MailReader

    try:
        fetched, new = mail_pipeline.ingest(conn, MailReader(), since_days=max(1, int(since_days)))
        rep = mail_pipeline.process_pending(conn)
    except Exception as exc:
        # 失败也要留痕，否则「检测挂了」和「没有新邮件」看起来一模一样
        mail_pipeline.record_sweep(conn, error=f"{type(exc).__name__}: {exc}")
        raise
    mail_pipeline.record_sweep(conn, fetched=fetched, new=new, rep=rep)
    return {"fetched": fetched, "new": new, **rep.compact()}


@tool(
    "list_email_queue",
    "人工确认队列：需要用户亲自确认的邮件（面试邀请、OA、offer、匹配不上的、置信度低的）。"
    "只给分类结果和原因，不给邮件正文。把这些列给用户，让他自己跑 agent mail accept。",
    Permission.READ,
    _obj({}),
)
def list_email_queue(conn: sqlite3.Connection) -> dict:
    from ..mail import pipeline as mail_pipeline

    pending = mail_pipeline.queue_for_agent(conn)
    return {"count": len(pending), "pending": pending}


@tool(
    "get_prep_pack",
    "为某条投递生成面试准备材料：JD 讲解、技能对比、gap、投出去的那版简历、"
    "对得上的 STAR 故事、内推人、时间线。只汇总库里已有的事实，不编造。返回文件路径。",
    Permission.READ,
    _obj({"application_id": INT}, ["application_id"]),
)
def get_prep_pack(conn: sqlite3.Connection, application_id: int) -> dict:
    path = prep.write(conn, application_id)
    text = path.read_text(encoding="utf-8")
    return {
        "application_id": application_id, "path": str(path),
        "sections": [l[3:] for l in text.splitlines() if l.startswith("## ")],
        "todo_count": text.count("TODO") + text.count("⚠"),
    }


# ---------------------------------------------------------------------------
# GATED —— 外发，必须人工批准
# ---------------------------------------------------------------------------

@tool(
    "send_notification",
    "把一条摘要推送到你配置的 Discord 频道。这是外发动作，需要人工批准。",
    Permission.GATED,
    _obj({"text": STR}, ["text"]),
)
def send_notification(conn: sqlite3.Connection, text: str) -> dict:
    res = notify.send(text)
    return {"sent": res.sent, "channel": res.channel, "detail": res.detail}


# ---------------------------------------------------------------------------

def specs(
    allow: set[Permission] | None = None,
    names: set[str] | None = None,
) -> list[dict[str, Any]]:
    """给 Messages API 的 tools 参数。

    两种收窄方式：
        allow  按权限档（粗，省事）——例如只读巡检
        names  按工具名（细，精确）——定时任务用这个

    收窄不只是省 token（实测每轮千余个）。真正的收益是**爆炸半径**：
    抓岗位的任务在结构上够不着改投递状态的工具，哪怕它被注入内容说服了。
    这是「工具即边界」用在任务粒度上。
    """
    return [
        t.spec()
        for name, t in REGISTRY.items()
        if (allow is None or t.permission in allow)
        and (names is None or name in names)
    ]


def execute(name: str, args: dict[str, Any], conn: sqlite3.Connection) -> str:
    """执行一个工具，把结果序列化成给模型看的字符串。"""
    t = REGISTRY.get(name)
    if t is None:
        raise KeyError(f"没有名为 {name} 的工具")
    result = t.fn(conn, **args)
    return json.dumps(result, ensure_ascii=False, default=str)
