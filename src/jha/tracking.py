"""Phase 4：投递追踪。

三件事，**全部是确定性的**（§0「能确定性判定的不交给模型」）：

    tracking_rows       Sheet / 终端两用的视图模型
    missing_confirmations  投出去 24h 还没收到确认邮件 → 告警
    next_step           每条投递的下一步建议

「下一步」为什么不问 LLM：它完全由状态 + 时间算得出来，规则写出来只有十几行，
而且**每次结果一样**。让模型来做既贵又不可复现，还得担心它编造。

数据库是真相源，Sheet 只是只读视图（路线图决策 #5）。所以这里只导出，不回读。
"""

from __future__ import annotations

import csv
import io as _io
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from . import config, db

#: 投出去多久没收到确认邮件就告警。
#: 确认邮件是「申请真的进系统了」的唯一地面真相——风险表里
#: 「申请被 ATS 静默丢弃」原本只有预防没有检测，这条补上检测。
CONFIRM_ALERT_HOURS = 24

#: applied 之后多久没动静就提示 follow-up
FOLLOW_UP_DAYS = 14

COLUMNS = (
    "公司", "岗位", "状态", "投递日", "渠道", "内推人",
    "最近事件", "简历版本", "匹配档位", "下一步", "最新邮件", "备注",
)


@dataclass
class TrackingRow:
    application_id: int
    company: str = ""
    title: str = ""
    status: str = ""
    applied_at: str = ""
    applied_via: str = ""
    referral: str = ""
    last_event: str = ""
    resume_version: str = ""
    verdict: str = ""
    next_step: str = ""
    #: 最近一封相关邮件的摘要。只给人看（board、导出）——摘要是从不可信邮件生成的，不进 agent 的上下文
    last_email: str = ""
    notes: str = ""

    def as_cells(self) -> list[str]:
        return [
            self.company, self.title, self.status, (self.applied_at or "")[:10],
            self.applied_via, self.referral, self.last_event,
            self.resume_version, self.verdict, self.next_step, self.last_email, self.notes,
        ]


# ---------------------------------------------------------------------------
# 下一步：纯规则
# ---------------------------------------------------------------------------

def next_step(
    *,
    status: str | None,
    applied_at: datetime | None,
    last_event_at: datetime | None,
    confirmation_seen: bool,
    now: datetime,
) -> str:
    """算出这条投递现在该做什么。

    顺序有讲究：**先报「可能没投成功」，再报别的**。
    一条根本没进 ATS 的申请，追它的进度是没有意义的。
    """
    if status in ("rejected", "withdrawn"):
        return ""
    if status == "offer":
        return "有 offer —— 确认回复期限"
    if status in ("interview_loop", "onsite", "phone_screen"):
        return "准备面试：跑 prep pack，翻出投出去的那版简历"
    if status == "oa":
        return "有 OA —— 确认截止时间"

    if applied_at and not confirmation_seen:
        idle = now - applied_at
        if idle >= timedelta(hours=CONFIRM_ALERT_HOURS):
            return (f"⚠ 投出去 {int(idle.total_seconds() // 3600)}h 还没收到确认邮件"
                    "——去 ATS 查一下是不是没投成功")

    if status == "ghosted":
        return "已判 ghosted —— 有内推路径的话可以从内部问一句"

    anchor = last_event_at or applied_at
    if anchor and (now - anchor) >= timedelta(days=FOLLOW_UP_DAYS):
        days = (now - anchor).days
        return f"{days} 天没动静 —— 考虑 follow-up，或让它自然进 ghosted"
    return ""


# ---------------------------------------------------------------------------
# 视图
# ---------------------------------------------------------------------------

def _dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return db._parse_dt(raw)
    except (ValueError, TypeError):
        return None


def tracking_rows(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[TrackingRow]:
    now = now or datetime.now()
    rows = conn.execute(
        """
        SELECT a.id, a.status, a.applied_at, a.applied_via, a.notes,
               a.confirmation_seen_at, a.resume_version_id,
               j.title, c.name AS company,
               ct.name AS referral,
               an.verdict,
               rv.page_count, rv.approved_at,
               (SELECT type || ' @ ' || substr(occurred_at, 1, 10) FROM events e
                 WHERE e.application_id = a.id ORDER BY e.occurred_at DESC, e.id DESC LIMIT 1) AS last_event,
               (SELECT occurred_at FROM events e
                 WHERE e.application_id = a.id ORDER BY e.occurred_at DESC, e.id DESC LIMIT 1) AS last_event_at,
               (SELECT COALESCE(substr(m.received_at, 1, 10), '') || ' ' || COALESCE(m.classification, '')
                       || '：' || m.summary FROM emails m
                 WHERE m.matched_application_id = a.id AND COALESCE(m.summary, '') != ''
                 ORDER BY m.received_at DESC, m.id DESC LIMIT 1) AS last_email
        FROM applications a
        JOIN jobs j ON j.id = a.job_id
        LEFT JOIN companies c ON c.id = j.company_id
        LEFT JOIN contacts ct ON ct.id = a.referred_by_contact_id
        LEFT JOIN job_analysis an ON an.job_id = j.id
        LEFT JOIN resume_versions rv ON rv.id = a.resume_version_id
        WHERE a.merged_into IS NULL AND a.voided_at IS NULL
        ORDER BY a.applied_at DESC, a.id DESC
        """
    ).fetchall()

    out: list[TrackingRow] = []
    for r in rows:
        resume = ""
        if r["resume_version_id"]:
            resume = f"#{r['resume_version_id']}"
            if not r["approved_at"]:
                resume += "（未审核）"
        out.append(TrackingRow(
            application_id=r["id"],
            company=r["company"] or "",
            title=r["title"] or "",
            status=r["status"] or "",
            applied_at=r["applied_at"] or "",
            applied_via=r["applied_via"] or "",
            referral=r["referral"] or "",
            last_event=r["last_event"] or "",
            resume_version=resume,
            verdict=r["verdict"] or "",
            last_email=r["last_email"] or "",
            notes=r["notes"] or "",
            next_step=next_step(
                status=r["status"],
                applied_at=_dt(r["applied_at"]),
                last_event_at=_dt(r["last_event_at"]),
                confirmation_seen=bool(r["confirmation_seen_at"]),
                now=now,
            ),
        ))
    return out


# ---------------------------------------------------------------------------
# 确认邮件告警
# ---------------------------------------------------------------------------

def missing_confirmations(
    conn: sqlite3.Connection, *, hours: int = CONFIRM_ALERT_HOURS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """投出去超过 N 小时、仍没有确认邮件的投递。

    只看还在进行中的：已经进入面试或被拒的，确认邮件早就无关紧要了。
    """
    now = now or datetime.now()
    cutoff = now - timedelta(hours=hours)
    rows = conn.execute(
        "SELECT a.id, a.applied_at, a.applied_via, j.title, j.url, c.name AS company "
        "FROM applications a JOIN jobs j ON j.id = a.job_id "
        "LEFT JOIN companies c ON c.id = j.company_id "
        "WHERE a.confirmation_seen_at IS NULL AND a.applied_at IS NOT NULL "
        "AND a.merged_into IS NULL AND a.voided_at IS NULL "
        "AND (a.status IS NULL OR a.status IN ('applied', 'ghosted')) "
        "ORDER BY a.applied_at"
    ).fetchall()

    out = []
    for r in rows:
        applied = _dt(r["applied_at"])
        if applied and applied <= cutoff:
            out.append({
                "application_id": r["id"], "company": r["company"], "title": r["title"],
                "applied_at": r["applied_at"], "applied_via": r["applied_via"],
                "hours_since": int((now - applied).total_seconds() // 3600),
                "url": r["url"],
            })
    return out


def mark_confirmed(
    conn: sqlite3.Connection, application_id: int, *, when: datetime | None = None
) -> dict[str, Any]:
    """记下确认邮件到了。同时追加一条事件——status 由 events 推导。"""
    ts = (when or datetime.now()).isoformat(sep=" ", timespec="seconds")
    cur = conn.execute(
        "UPDATE applications SET confirmation_seen_at = ? WHERE id = ?", (ts, application_id)
    )
    if cur.rowcount == 0:
        raise ValueError(f"没有 id 为 {application_id} 的投递记录")
    db.append_event(conn, application_id, "confirmation_received",
                    occurred_at=when, source="manual")
    return {"application_id": application_id, "confirmation_seen_at": ts}


# ---------------------------------------------------------------------------
# 从邮件补建投递记录
# ---------------------------------------------------------------------------

#: 邮件里没写岗位名时，自动建档用的占位岗位名
UNKNOWN_ROLE = "(岗位未写明)"


def record_from_email(
    conn: sqlite3.Connection, *, company_id: int | None, company_name: str, title: str,
    occurred_at: datetime | None, email_id: int, applied_at: str | None, source: str = "email",
) -> int:
    """凭一封邮件给投递表补一条记录（你在 LinkedIn、公司官网等别处投的）。返回 application id。

    名字由调用方先过校验（mail/pipeline.py::clean_name），这里只管落库：
      公司  按名字找，找不到就新建：不写 email_domains（域名只能来自你登记的配置，不能来自邮件），
            is_active=0（没有 ATS 信息，不参与抓取）
      岗位  这家公司抓到过同名岗位就挂在它下面（分析、匹配档位都用得上），否则新建一条 source='email'
      投递  已经有了就直接返回。投递日只在确认信时填——别的邮件看不出你哪天投的，就不编
    """
    if company_id is None:
        row = conn.execute("SELECT id FROM companies WHERE lower(name) = lower(?)",
                           (company_name,)).fetchone()
        company_id = row["id"] if row else int(conn.execute(
            "INSERT INTO companies (name, is_active, notes) VALUES (?, 0, ?)",
            (company_name, f"由邮件 #{email_id} 自动建档；没有 ATS 信息，不参与抓取"),
        ).lastrowid)

    title = title or UNKNOWN_ROLE
    job = conn.execute(
        "SELECT id FROM jobs WHERE company_id = ? AND lower(title) = lower(?) "
        "ORDER BY source = 'email', id LIMIT 1", (company_id, title),
    ).fetchone()
    job_id = job["id"] if job else int(conn.execute(
        "INSERT INTO jobs (company_id, source, external_id, title) VALUES (?, 'email', ?, ?)",
        (company_id, f"{company_id}:{title.lower()}", title),
    ).lastrowid)

    existing = conn.execute("SELECT id, merged_into, voided_at FROM applications WHERE job_id = ?",
                            (job_id,)).fetchone()
    if existing:
        if existing["merged_into"]:
            return int(existing["merged_into"])           # 合并过：记到保留的那条上
        if existing["voided_at"]:
            # 被标成「不是投递」的那条又来了相关邮件——当它是真的，恢复
            conn.execute("UPDATE applications SET voided_at = NULL WHERE id = ?", (existing["id"],))
            conn.commit()
        return int(existing["id"])
    app_id = int(conn.execute(
        "INSERT INTO applications (job_id, applied_at, notes) VALUES (?, ?, ?)",
        (job_id, applied_at, f"由邮件 #{email_id} 自动建档"),
    ).lastrowid)
    db.append_event(conn, app_id, "applied", occurred_at=occurred_at, source=source,
                    raw_ref=str(email_id), payload={"email_id": email_id, "inferred_from_email": True})
    return app_id


def fill_unknown_role(conn: sqlite3.Connection, application_id: int, title: str) -> bool:
    """占位记录等到后来的信写了岗位，就把岗位名补上。只改从邮件建档的占位岗位，抓来的岗位不碰。"""
    if not title or title == UNKNOWN_ROLE:
        return False
    job = conn.execute(
        "SELECT j.id, j.company_id FROM jobs j JOIN applications a ON a.job_id = j.id "
        "WHERE a.id = ? AND j.source = 'email' AND j.title = ?", (application_id, UNKNOWN_ROLE),
    ).fetchone()
    if job is None:
        return False
    cur = conn.execute("UPDATE OR IGNORE jobs SET title = ?, external_id = ? WHERE id = ?",
                       (title, f"{job['company_id']}:{title.lower()}", job["id"]))
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# 重复记录：合并、作废、找重复
#
# 合并撤不回——events 只能追加。所以 CLI 默认只打印计划，加 --yes 才执行；这些都不是 agent 的工具。
# ---------------------------------------------------------------------------

#: 还算数的投递：没被合并进别的，也没被标成「不是投递」
ACTIVE = "a.merged_into IS NULL AND a.voided_at IS NULL"


def _title_key(title: str) -> str:
    """「2133859 - January 2027 - Leap Software Engineer」和「2133859 January 2027 - Leap ...」算一样。"""
    return re.sub(r"[^a-z0-9]", "", (title or "").lower())


def _active_app(conn: sqlite3.Connection, app_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT a.*, j.title, j.company_id, c.name AS company FROM applications a "
        "JOIN jobs j ON j.id = a.job_id LEFT JOIN companies c ON c.id = j.company_id WHERE a.id = ?",
        (app_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"没有 id 为 {app_id} 的投递记录")
    if row["merged_into"]:
        raise ValueError(f"#{app_id} 已经合并进 #{row['merged_into']} 了")
    if row["voided_at"]:
        raise ValueError(f"#{app_id} 已经标成不是投递了（agent applications void {app_id} --undo 恢复）")
    return row


def plan_merge(conn: sqlite3.Connection, a: int, b: int) -> dict[str, Any]:
    """合并两条重复的投递：算出保留哪条、要抄哪些事件、挪几封邮件。只读。

    有一条是占位记录（邮件没写岗位）就保留有岗位名的那条；都有岗位名就保留后一个参数。
    """
    if a == b:
        raise ValueError("两个 id 是同一条")
    ra, rb = _active_app(conn, a), _active_app(conn, b)
    if ra["company_id"] != rb["company_id"]:
        raise ValueError(
            f"#{a} 和 #{b} 属于不同的公司（{ra['company']} / {rb['company']}）。"
            "确定是同一家，先合并公司：agent companies merge <重复的公司 id> <保留的公司 id>"
        )
    dup, keep = (ra, rb) if ra["title"] == UNKNOWN_ROLE or rb["title"] != UNKNOWN_ROLE else (rb, ra)
    have = {(r["type"], r["occurred_at"]) for r in conn.execute(
        "SELECT type, occurred_at FROM events WHERE application_id = ?", (keep["id"],))}
    has_applied = any(t == "applied" for t, _ in have)
    events = [
        e for e in conn.execute(
            "SELECT * FROM events WHERE application_id = ? ORDER BY occurred_at, id", (dup["id"],))
        if (e["type"], e["occurred_at"]) not in have and not (e["type"] == "applied" and has_applied)
    ]
    emails = conn.execute("SELECT COUNT(*) c FROM emails WHERE matched_application_id = ?",
                          (dup["id"],)).fetchone()["c"]
    return {"dup": dup, "keep": keep, "events": events, "emails": emails}


def merge_applications(
    conn: sqlite3.Connection, a: int, b: int, *, now: datetime | None = None,
) -> dict[str, Any]:
    """执行合并。

    重复那条的事件原样抄到保留的那条上（来源不变，payload 记上从哪条抄来的）；邮件改指过去——
    同类待办按「投递 + 类型」分组，于是自动合成一条；空着的字段从重复那条补上，投递日取早的；
    重复那条标 merged_into，各处查询都跳过它。它自己的事件原样留着，历史不丢。
    """
    p = plan_merge(conn, a, b)
    dup, keep = p["dup"], p["keep"]
    for e in p["events"]:
        payload = {**db.load_json(e["payload_json"], {}), "merged_from_application": dup["id"],
                   "copied_event_id": e["id"]}
        db.append_event(conn, keep["id"], e["type"], occurred_at=db._parse_dt(e["occurred_at"]),
                        source=e["source"], raw_ref=e["raw_ref"], payload=payload, rebuild=False)
    conn.execute("UPDATE emails SET matched_application_id = ? WHERE matched_application_id = ?",
                 (keep["id"], dup["id"]))
    # 之前并进 dup 的，也一起指向 keep
    conn.execute("UPDATE applications SET merged_into = ? WHERE merged_into = ? OR id = ?",
                 (keep["id"], dup["id"], dup["id"]))

    def earliest(x: str | None, y: str | None) -> str | None:
        vals = [v for v in (x, y) if v]
        return min(vals) if vals else None

    conn.execute(
        "UPDATE applications SET applied_at = ?, confirmation_seen_at = ?, "
        "resume_version_id = COALESCE(resume_version_id, ?), applied_via = COALESCE(applied_via, ?), "
        "referred_by_contact_id = COALESCE(referred_by_contact_id, ?), notes = ? WHERE id = ?",
        (earliest(keep["applied_at"], dup["applied_at"]),
         earliest(keep["confirmation_seen_at"], dup["confirmation_seen_at"]),
         dup["resume_version_id"], dup["applied_via"], dup["referred_by_contact_id"],
         "；".join(x for x in (keep["notes"], f"合并了 #{dup['id']}") if x), keep["id"]),
    )
    status = db.rebuild_status(conn, keep["id"], now=now)
    conn.commit()
    return {"kept": keep["id"], "merged": dup["id"], "events_copied": len(p["events"]),
            "emails_moved": p["emails"], "status": status}


def void_application(
    conn: sqlite3.Connection, app_id: int, *, reason: str = "", undo: bool = False,
) -> dict[str, Any]:
    """标成「不是投递」（比如注册人才库的邮件建出来的）。只是隐藏，undo=True 恢复。"""
    row = conn.execute("SELECT merged_into, notes FROM applications WHERE id = ?", (app_id,)).fetchone()
    if row is None:
        raise ValueError(f"没有 id 为 {app_id} 的投递记录")
    if row["merged_into"]:
        raise ValueError(f"#{app_id} 已经合并进 #{row['merged_into']} 了")
    if undo:
        conn.execute("UPDATE applications SET voided_at = NULL WHERE id = ?", (app_id,))
    else:
        note = "；".join(x for x in (row["notes"], f"不是投递：{reason}" if reason else "不是投递") if x)
        conn.execute("UPDATE applications SET voided_at = datetime('now'), notes = ? WHERE id = ?",
                     (note, app_id))
    conn.commit()
    return {"application_id": app_id, "voided": not undo}


def merge_companies(conn: sqlite3.Connection, dup_id: int, keep_id: int) -> dict[str, Any]:
    """同一家公司被建成了两条（SS&C Technologies / SS&C Technologies Inc）：
    把指向它的岗位、联系人、抓取记录挪过去，邮件域名合在一起，删掉重复的那条。

    companies.yaml 里登记过的那条（有 ATS 信息）不能当重复的删——下次 companies sync 又会建回来。
    """
    if dup_id == keep_id:
        raise ValueError("两个 id 是同一家")
    dup = conn.execute("SELECT * FROM companies WHERE id = ?", (dup_id,)).fetchone()
    keep = conn.execute("SELECT * FROM companies WHERE id = ?", (keep_id,)).fetchone()
    if dup is None or keep is None:
        raise ValueError(f"没有 id 为 {dup_id if dup is None else keep_id} 的公司")
    if dup["ats_type"] or dup["board_token"]:
        raise ValueError(f"{dup['name']}（#{dup_id}）是 companies.yaml 里登记的，删了下次同步又会建回来。"
                         f"反过来合并：agent companies merge {keep_id} {dup_id}")
    moved: dict[str, int] = {}
    for table in [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]:
        for fk in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
            if fk["table"] == "companies":
                moved[table] = conn.execute(
                    f'UPDATE {table} SET "{fk["from"]}" = ? WHERE "{fk["from"]}" = ?', (keep_id, dup_id)
                ).rowcount
    domains = sorted(set(db.load_json(keep["email_domains_json"])) | set(db.load_json(dup["email_domains_json"])))
    conn.execute("UPDATE companies SET email_domains_json = ? WHERE id = ?", (db.dump_json(domains), keep_id))
    conn.execute("DELETE FROM companies WHERE id = ?", (dup_id,))
    conn.commit()
    return {"kept": keep_id, "merged": dup_id, "moved": moved}


def duplicate_candidates(conn: sqlite3.Connection) -> dict[str, list[Any]]:
    """可能重复的东西，只给建议，合不合由你定：

      companies     名字几乎一样的公司（SS&C Technologies / SS&C Technologies Inc）
      applications  同一家公司里：占位记录配有岗位名的；岗位名去掉标点后一样的
      emails        没对上投递的待办邮件，按公司名找可能的投递
    """
    from .mail.policy import ACTION_TYPES
    from .mail.prefilter import company_key

    groups: dict[str, list[sqlite3.Row]] = {}
    for c in conn.execute("SELECT id, name, ats_type FROM companies ORDER BY id"):
        groups.setdefault(company_key(c["name"]), []).append(c)
    companies = [g for k, g in groups.items() if k and len(g) > 1]

    apps = conn.execute(
        "SELECT a.id, a.status, j.title, j.company_id, c.name AS company FROM applications a "
        f"JOIN jobs j ON j.id = a.job_id LEFT JOIN companies c ON c.id = j.company_id WHERE {ACTIVE} ORDER BY a.id"
    ).fetchall()
    per_company: dict[int, list[sqlite3.Row]] = {}
    for a in apps:
        per_company.setdefault(a["company_id"], []).append(a)
    pairs: list[dict[str, Any]] = []
    for group in per_company.values():
        titled = [a for a in group if a["title"] != UNKNOWN_ROLE]
        for p in group:
            if p["title"] == UNKNOWN_ROLE and titled:
                pairs.append({"dup": p, "keep": titled, "why": "占位记录，当时的邮件没写岗位"})
        seen: dict[str, sqlite3.Row] = {}
        for a in titled:
            k = _title_key(a["title"])
            if k in seen:
                pairs.append({"dup": seen[k], "keep": [a], "why": "岗位名去掉标点后一样"})
            else:
                seen[k] = a

    types = tuple(sorted(ACTION_TYPES))
    keys = {a["id"]: company_key(a["company"]) for a in apps}
    emails = []
    for e in conn.execute(
        f"SELECT id, classification, company_hint FROM emails WHERE classification IN ({','.join('?' * len(types))}) "
        "AND matched_application_id IS NULL AND review_status = 'pending' AND done_at IS NULL ORDER BY id", types,
    ):
        hint = company_key(e["company_hint"])
        cands = [a for a in apps if len(hint) >= 3 and len(keys[a["id"]]) >= 3
                 and (hint.startswith(keys[a["id"]]) or keys[a["id"]].startswith(hint))]
        emails.append({"email": e, "candidates": cands})
    return {"companies": companies, "applications": pairs, "emails": emails}


# ---------------------------------------------------------------------------
# 每日上限
# ---------------------------------------------------------------------------

def applied_today(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    day = (now or datetime.now()).strftime("%Y-%m-%d")
    return conn.execute(
        "SELECT COUNT(*) c FROM applications WHERE substr(applied_at, 1, 10) = ? "
        "AND merged_into IS NULL AND voided_at IS NULL", (day,)
    ).fetchone()["c"]


def daily_limit_status(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    """每日投递上限的用途不是省力，是**逼你投得准**。

    一天投 30 家和一天投 8 家，后者才有时间给每家写像样的
    「Why this company」、查内推、看 JD。
    """
    limit = config.daily_apply_limit()
    used = applied_today(conn, now=now)
    return {"limit": limit, "used": used, "remaining": max(0, limit - used),
            "exceeded": used >= limit}


# ---------------------------------------------------------------------------
# 导出：数据库是真相源，Sheet 只是只读视图
# ---------------------------------------------------------------------------

def to_delimited(rows: Iterable[TrackingRow], *, delimiter: str = "\t") -> str:
    buf = _io.StringIO()
    w = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    w.writerow(COLUMNS)
    for r in rows:
        w.writerow(r.as_cells())
    return buf.getvalue()


def to_markdown(rows: Iterable[TrackingRow]) -> str:
    """Markdown 表格，编辑器预览或 GitHub 上看。单元格里的 | 和换行会弄乱表格，转义掉。"""
    def cell(x: str) -> str:
        return str(x or "").replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    lines += ["| " + " | ".join(cell(c) for c in r.as_cells()) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def export_file(
    conn: sqlite3.Connection, path: Path | None = None, *,
    delimiter: str = "\t", now: datetime | None = None,
) -> Path:
    """导出成 Sheet 可直接粘贴的 TSV（默认）或 CSV。

    默认用制表符：直接贴进 Google Sheet 会自动分列，不用走导入向导，
    也不会被岗位标题里的逗号搞乱。

    CSV 是给 Excel 双击打开的：开头加 BOM。Excel 不带 BOM 就按系统编码读，中文全成乱码。
    TSV 不加——它是复制粘贴用的，BOM 会变成第一格里一个看不见的字符。
    """
    csv_ = delimiter == ","
    path = path or (config.DATA_DIR / ("tracking.csv" if csv_ else "tracking.tsv"))
    text = to_delimited(tracking_rows(conn, now=now), delimiter=delimiter)
    config.write_text(path, ("\ufeff" if csv_ else "") + text)
    return path


def sync_to_sheet(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    """全量写进 Google Sheet。

    **只写不读**——数据库是真相源，双向同步会成为无底洞（路线图决策 #5）。

    用 service account 而不是 OAuth：Sheets 不像 Gmail，服务账号可以直接
    访问「共享给它的」表格，没有 Testing 模式下 refresh token 每 7 天过期的问题。
    设置一次，永久有效。

    需要：
      1. GCP 建服务账号，启用 Sheets API，下载 JSON key
      2. `.env` 里 GOOGLE_SERVICE_ACCOUNT_JSON=<key 路径>、GOOGLE_SHEET_ID=<表格 id>
      3. 把表格共享给 key 里的那个 client_email（编辑权限）

    > 这段的 gspread 调用没有自动化测试覆盖（需要真凭证）。
    > 行构造逻辑是共享的、被测过的；未覆盖的只有下面几行 API 调用。
    """
    key_path = config.env("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = config.env("GOOGLE_SHEET_ID")
    if not (key_path and sheet_id):
        return {"synced": False,
                "reason": "没配 GOOGLE_SERVICE_ACCOUNT_JSON / GOOGLE_SHEET_ID，"
                          "先用 agent export 导出 TSV 手工粘贴"}
    try:
        import gspread
    except ImportError:
        return {"synced": False, "reason": "没装 gspread：pip install gspread"}

    rows = tracking_rows(conn, now=now)
    values = [list(COLUMNS)] + [r.as_cells() for r in rows]
    gc = gspread.service_account(filename=key_path)
    ws = gc.open_by_key(sheet_id).sheet1
    ws.clear()
    ws.update(values, "A1")
    return {"synced": True, "rows": len(rows), "sheet_id": sheet_id}
