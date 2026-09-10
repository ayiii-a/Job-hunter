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
    "最近事件", "简历版本", "匹配档位", "下一步", "备注",
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
    notes: str = ""

    def as_cells(self) -> list[str]:
        return [
            self.company, self.title, self.status, (self.applied_at or "")[:10],
            self.applied_via, self.referral, self.last_event,
            self.resume_version, self.verdict, self.next_step, self.notes,
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
                 WHERE e.application_id = a.id ORDER BY e.occurred_at DESC, e.id DESC LIMIT 1) AS last_event_at
        FROM applications a
        JOIN jobs j ON j.id = a.job_id
        LEFT JOIN companies c ON c.id = j.company_id
        LEFT JOIN contacts ct ON ct.id = a.referred_by_contact_id
        LEFT JOIN job_analysis an ON an.job_id = j.id
        LEFT JOIN resume_versions rv ON rv.id = a.resume_version_id
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
# 每日上限
# ---------------------------------------------------------------------------

def applied_today(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    day = (now or datetime.now()).strftime("%Y-%m-%d")
    return conn.execute(
        "SELECT COUNT(*) c FROM applications WHERE substr(applied_at, 1, 10) = ?", (day,)
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


def export_file(
    conn: sqlite3.Connection, path: Path | None = None, *,
    delimiter: str = "\t", now: datetime | None = None,
) -> Path:
    """导出成 Sheet 可直接粘贴的 TSV（默认）或 CSV。

    默认用制表符：直接贴进 Google Sheet 会自动分列，不用走导入向导，
    也不会被岗位标题里的逗号搞乱。
    """
    path = path or (config.DATA_DIR / "tracking.tsv")
    config.write_text(path, to_delimited(tracking_rows(conn, now=now), delimiter=delimiter))
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
