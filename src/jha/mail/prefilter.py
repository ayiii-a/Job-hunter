"""确定性预过滤。不过 LLM。

目的是省掉绝大多数不相关邮件的分类调用。宁可多放进来几封（后面的分类会判成 other），
也不要漏掉一封面试邀请——所以这里是**召回优先**的。

一个必须处理的坑：**共享 ATS 域名不能用来识别公司。**
`greenhouse-mail.io` 是所有用 Greenhouse 的公司共用的发信域名。如果某家公司的
`email_domains` 里写了它，照字面匹配会把**每一封** Greenhouse 邮件都认成那家公司。
所以共享域名只用来判断「这是招聘系统发的」，认公司要看发件人显示名和正文。

发件方分四种（PrefilterResult.channel）：登记过的公司域名、招聘系统、求职平台、只是主题像求职邮件。
前三种算可信的发件方，只有它们的邮件能直接新建投递记录（见 policy.py）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .. import db

SHARED_ATS_DOMAINS: frozenset[str] = frozenset({
    "greenhouse-mail.io", "greenhouse.io", "hire.lever.co", "lever.co", "ashbyhq.com",
    "myworkday.com", "workday.com", "smartrecruiters.com", "icims.com", "jobvite.com",
    "taleo.net", "successfactors.com", "workable.com", "workablemail.com",
    "bamboohr.com", "rippling.com", "gem.com", "calendly.com", "goodtime.io",
    "hackerrank.com", "codesignal.com", "codility.com",
})

#: 求职平台。它们发的「申请已发送」是可信的确认信，但也发大量推荐和动态——
#: 所以不像 ATS 域名那样整域放行，只在主题像求职邮件时才放进来
JOB_BOARD_DOMAINS: frozenset[str] = frozenset({
    "linkedin.com", "indeed.com", "indeedemail.com", "joinhandshake.com",
    "wellfound.com", "glassdoor.com", "ziprecruiter.com",
})

SUBJECT_KEYWORDS: tuple[str, ...] = (
    "application", "applied", "applying", "interview", "assessment", "offer",
    "unfortunately", "next steps", "candidacy", "thank you for your interest",
    "coding challenge", "online assessment", "phone screen", "hackerrank", "codesignal",
)


@dataclass
class PrefilterResult:
    passed: bool
    reason: str
    company_id: int | None = None
    via_ats: bool = False
    #: 发件方是谁：company（登记过的公司域名）| ats（招聘系统）| board（求职平台）| keyword（只是主题像）
    channel: str = ""


def _get(obj: Any, key: str) -> str:
    try:
        return str(obj[key] or "")
    except (KeyError, IndexError, TypeError):
        return str(getattr(obj, key, "") or "")


def domain_matches(sender: str, domain: str) -> bool:
    """`mail.acme.com` 算 `acme.com`；`notacme.com` 和 `acme.com.evil.io` 不算。

    和规则初筛里 India / Indianapolis 是同一类问题：裸子串匹配会误伤。
    这里的边界是「.」。
    """
    s = (sender or "").lower().strip().strip(".")
    d = (domain or "").lower().strip().strip(".")
    return bool(d) and (s == d or s.endswith("." + d))


def company_domain_map(conn: sqlite3.Connection) -> dict[str, int]:
    """公司自有域名 → company_id。**共享 ATS 域名被排除在外。**"""
    out: dict[str, int] = {}
    for r in conn.execute("SELECT id, email_domains_json FROM companies"):
        for d in db.load_json(r["email_domains_json"]):
            d = str(d).lower().strip()
            if d and not any(domain_matches(d, s) for s in SHARED_ATS_DOMAINS):
                out[d] = r["id"]
    return dict(sorted(out.items(), key=lambda kv: -len(kv[0])))


def screen(email_obj: Any, domain_map: dict[str, int]) -> PrefilterResult:
    sender = _get(email_obj, "from_domain")
    subject = _get(email_obj, "subject").lower()

    for d, cid in domain_map.items():
        if domain_matches(sender, d):
            return PrefilterResult(True, f"已投公司域名：{d}", company_id=cid, channel="company")
    for d in SHARED_ATS_DOMAINS:
        if domain_matches(sender, d):
            return PrefilterResult(True, f"招聘系统发信域名：{d}", via_ats=True, channel="ats")
    for kw in SUBJECT_KEYWORDS:
        if kw in subject:
            board = next((d for d in JOB_BOARD_DOMAINS if domain_matches(sender, d)), None)
            if board:
                return PrefilterResult(True, f"求职平台 {board}，主题关键词：{kw}", channel="board")
            return PrefilterResult(True, f"主题关键词：{kw}", channel="keyword")
    return PrefilterResult(False, "既不是已投公司、也不是招聘系统，主题也不像求职邮件")
