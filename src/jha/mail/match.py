"""把邮件匹配到投递记录。确定性。

顺序按路线图 Phase 5 第 4 点：先认公司，再在这家公司的投递里认岗位。
分不出来就报 ambiguous，**不猜**——猜错了会把一封面试邀请记到另一个岗位上。

认公司时先看发件人显示名和主题，最后才看正文。正文里「ramp up quickly」
这种话会让 Ramp 被误认出来，显示名和主题里很少出现这种巧合。

已知局限：jobs 表没存 requisition id，所以一家公司同名岗位投了两个时只能报 ambiguous。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from ..filters import _pattern


@dataclass
class MatchResult:
    application_id: int | None = None
    company_id: int | None = None
    status: str = "none"          # exact | ambiguous | no_application | none
    candidates: list[int] = field(default_factory=list)
    reason: str = ""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _companies_named_in(conn: sqlite3.Connection, text: str) -> list[int]:
    found = []
    for r in conn.execute("SELECT id, name FROM companies"):
        name = (r["name"] or "").strip()
        if len(name) >= 2 and _pattern(name).search(text or ""):
            found.append(r["id"])
    return found


def _title_variants(title: str) -> list[str]:
    """「AI Engineer - FDE (Forward Deployed Engineer)」→ 全称 + 「ai engineer」。"""
    t = _norm(title)
    parts = {t}
    for sep in (" - ", " — ", " – ", ", ", " | ", " ("):
        if sep in t:
            parts.add(t.split(sep)[0].strip())
    return [p for p in parts if len(p) >= 6]


def match(
    conn: sqlite3.Connection, *, from_addr: str, subject: str, body: str,
    company_id: int | None = None, role_hint: str = "",
) -> MatchResult:
    if company_id is None:
        head = _companies_named_in(conn, f"{from_addr}\n{subject}")
        found = head or _companies_named_in(conn, (body or "")[:2000])
        if len(found) > 1:
            return MatchResult(status="ambiguous", reason="邮件里同时出现了多家目标公司")
        if not found:
            return MatchResult(status="none", reason="认不出是哪家公司")
        company_id = found[0]

    apps = conn.execute(
        "SELECT a.id, j.title FROM applications a JOIN jobs j ON j.id = a.job_id "
        "WHERE j.company_id = ? ORDER BY a.applied_at DESC", (company_id,),
    ).fetchall()
    if not apps:
        return MatchResult(company_id=company_id, status="no_application",
                           reason="这家公司没有投递记录")
    if len(apps) == 1:
        return MatchResult(application_id=apps[0]["id"], company_id=company_id,
                           status="exact", candidates=[apps[0]["id"]],
                           reason="这家公司只有一条投递")

    hay = _norm(f"{subject}\n{role_hint}\n{(body or '')[:4000]}")
    scored = sorted(
        ((max((len(v) for v in _title_variants(a["title"]) if v in hay), default=0), a["id"])
         for a in apps),
        reverse=True,
    )
    best, runner_up = scored[0], scored[1]
    if best[0] > 0 and best[0] > runner_up[0]:
        return MatchResult(application_id=best[1], company_id=company_id, status="exact",
                           candidates=[i for _, i in scored], reason="按岗位名在邮件里定位到")
    return MatchResult(company_id=company_id, status="ambiguous",
                       candidates=[i for _, i in scored],
                       reason=f"这家公司有 {len(apps)} 条投递，邮件里分不出是哪一条")
