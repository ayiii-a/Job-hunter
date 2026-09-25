"""把邮件匹配到投递记录。确定性。

顺序按路线图 Phase 5 第 4 点：先认公司，再在这家公司的投递里认岗位。
分不出来就报 ambiguous，**不猜**——猜错了会把一封面试邀请记到另一个岗位上。

认公司时先看发件人显示名和主题，最后才看正文。正文里「ramp up quickly」
这种话会让 Ramp 被误认出来，显示名和主题里很少出现这种巧合。

认岗位要照顾「表里的投递不一定都是 agent 投的」：邮件写明的岗位对不上这家公司的
任何一条投递时，报 no_application（是一条新的投递），而不是硬套到已有的那条上。
只有一条投递时也一样——你在 LinkedIn 投了同一家公司的另一个岗位，那封确认信不该记到这条上。

已知局限：jobs 表没存 requisition id，所以一家公司同名岗位投了两个时只能报 ambiguous；
邮件里的岗位写法和表里差得太多（缩写、改名）时，会被当成新的一条。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from ..filters import _pattern

#: 岗位名短于这个长度就不拿来比——「Engineer」这种词哪条投递都沾边
MIN_ROLE_LEN = 6


@dataclass
class MatchResult:
    application_id: int | None = None
    company_id: int | None = None
    status: str = "none"          # exact | ambiguous | no_application | none
    candidates: list[int] = field(default_factory=list)
    reason: str = ""              # 进 agent 看得到的队列——只写我们自己的字，不拼邮件内容


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


def _role_score(title: str, hay: str, role: str) -> int:
    """这条投递的岗位和邮件对得上多少：命中片段的最长长度，0 = 对不上。

    两个方向都看：表里的岗位名（或它的主干）出现在邮件里；邮件写的岗位名是表里岗位名的一部分
    （表里「AI Engineer - FDE (Forward Deployed Engineer)」，邮件只写 Forward Deployed Engineer）。
    """
    score = max((len(v) for v in _title_variants(title) if v in hay), default=0)
    if len(role) >= MIN_ROLE_LEN and role in _norm(title):
        score = max(score, len(role))
    return score


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

    role = _norm(role_hint)
    names_a_role = len(role) >= MIN_ROLE_LEN
    hay = _norm(f"{subject}\n{role_hint}\n{(body or '')[:4000]}")
    scored = sorted(((_role_score(a["title"], hay, role), a["id"]) for a in apps), reverse=True)
    ids = [i for _, i in scored]

    if len(apps) == 1:
        # 表里岗位名太短、比不了的（没有可比的主干），只能照旧算它
        if names_a_role and scored[0][0] == 0 and _title_variants(apps[0]["title"]):
            return MatchResult(company_id=company_id, status="no_application", candidates=ids,
                               reason="这家公司只有一条投递，但邮件里写的岗位对不上它——当成新的一条")
        return MatchResult(application_id=apps[0]["id"], company_id=company_id,
                           status="exact", candidates=ids, reason="这家公司只有一条投递")

    best, runner_up = scored[0], scored[1]
    if best[0] > 0 and best[0] > runner_up[0]:
        return MatchResult(application_id=best[1], company_id=company_id, status="exact",
                           candidates=ids, reason="按岗位名在邮件里定位到")
    if best[0] == 0 and names_a_role:
        return MatchResult(company_id=company_id, status="no_application", candidates=ids,
                           reason=f"这家公司的 {len(apps)} 条投递都对不上邮件里写的岗位——当成新的一条")
    return MatchResult(company_id=company_id, status="ambiguous", candidates=ids,
                       reason=f"这家公司有 {len(apps)} 条投递，邮件里分不出是哪一条")
