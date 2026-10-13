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

import email.utils
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlsplit

from .. import db
from ..filters import _pattern

SHARED_ATS_DOMAINS: frozenset[str] = frozenset({
    "greenhouse-mail.io", "greenhouse.io", "hire.lever.co", "lever.co", "ashbyhq.com",
    "myworkday.com", "workday.com", "smartrecruiters.com", "icims.com", "jobvite.com",
    "taleo.net", "successfactors.com", "workable.com", "workablemail.com",
    "bamboohr.com", "rippling.com", "gem.com", "calendly.com", "goodtime.io",
    "hackerrank.com", "codesignal.com", "codility.com",
    # 实测队列里被拦下的招聘系统 / HR 平台 / 测评平台
    "paylocity.com", "applytojob.com", "jazzhr.com", "avature.net", "teamtailor.com",
    "teamtailor-mail.com", "polymer.co", "personio.de", "personio.com", "saashr.com", "ukg.net",
    "ultipro.com", "hirebridge.com", "hirebridgemail.com", "applicantemails.com",
    "applyresponse.com", "oraclecloud.com", "successfactors.eu", "adp.com", "deel.com",
    "myworkdayjobs.com", "breezy.hr", "recruitee.com", "pinpointhq.com", "paycomonline.net",
    "hackerrankforwork.com", "coderpad.io",
    # 实测待办邮件里的测评 / 招聘平台
    "wellsuited.com", "coderbyte.com", "fountain.com", "hirevue.com", "karat.com",
    "testgorilla.com", "pymetrics.ai",
})

#: 求职平台。它们发的「申请已发送」是可信的确认信，但也发大量推荐和动态——
#: 所以不像 ATS 域名那样整域放行，只在主题像求职邮件时才放进来
JOB_BOARD_DOMAINS: frozenset[str] = frozenset({
    "linkedin.com", "indeed.com", "indeedemail.com", "joinhandshake.com",
    "wellfound.com", "glassdoor.com", "ziprecruiter.com", "ycombinator.com",
})

#: 个人邮箱。招聘方个人用它发信很常见，但它不是任何一家公司的域名
FREE_MAIL_DOMAINS: frozenset[str] = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com",
    "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "qq.com", "163.com", "126.com",
})


def shared_sender(domain: str) -> bool:
    """多家公司共用、或者谁都能注册的发信域名：招聘系统、求职平台、跳转服务、个人邮箱。"""
    shared = SHARED_ATS_DOMAINS | JOB_BOARD_DOMAINS | TRACKING_DOMAINS | FREE_MAIL_DOMAINS
    return any(domain_matches(domain, d) for d in shared)


#: co.uk、com.au 这类二级后缀：注册名在它左边
_SECOND_LEVEL = frozenset({"co", "com", "org", "net", "ac", "gov", "edu"})

#: 公司名里的法律后缀和通用词，比对时去掉（McKinsey & Company → mckinsey）
_CORP_WORDS = frozenset({
    "the", "inc", "llc", "ltd", "corp", "corporation", "co", "company", "group", "gmbh", "plc",
    "lp", "llp",
})

#: 首字母缩写里不算的虚词（Massachusetts Institute of Technology → mit）
_STOP_WORDS = frozenset({"of", "and", "for", "at", "in", "on"})


def domain_belongs_to(sender_domain: str, company: str) -> bool:
    """发件域名是不是这家公司的：careerhub.newyorklife.com ↔ New York Life、email.roblox.com ↔ Roblox。

    给没登记过的公司建档用：分类器说是哪家公司，这里用确定性规则核对发件域名——
    信任不交给模型，因为钓鱼信就是写来骗读信的人的，模型也是读信的人。

    只比**注册名**（顶级域名左边那一段：careerhub.newyorklife.com 的 newyorklife）。子域名谁都能起——
    拥有 evil.io 的人可以发自 stripe.com.evil.io。
    而且只认**完全相等**：注册名 = 公司名去掉空格标点后的前几个词连起来，
    或者是公司名的首字母缩写（hpe.com ↔ Hewlett Packard Enterprise、bah.com ↔ Booz Allen Hamilton，至少三个字母）。
    stripe-careers.com、stripecareers.com 冒充 Stripe 都对不上——钓鱼域名最爱在公司名后面加 careers。
    代价是 metlifecareers.com、skyworksinc.com 这类真域名也对不上，它们还是进队列。

    挡不住换顶级域名的仿冒（stripe.xyz）。能接受是因为这里只决定「直接建档」还是「进队列」：
    面试邀请不管哪种都会推送，offer 永远人工确认——漏过去的代价是表里多一条假记录。
    ponytail: 没用公共后缀表，只认几种常见二级后缀（co.uk、com.au）
    """
    words = [w for w in re.findall(r"[a-z0-9]+", (company or "").lower()) if w not in _CORP_WORDS]
    names = {"".join(words[:k]) for k in range(1, len(words) + 1)}
    initials = "".join(w[0] for w in words if w not in _STOP_WORDS)
    if len(initials) >= 3:
        names.add(initials)
    label = registered_domain(sender_domain).split(".")[0]
    return len(label) >= 3 and label in names


def company_key(name: str) -> str:
    """比较公司名用：小写、去标点和 Inc / Company 这类后缀（SS&C Technologies Inc → ssctechnologies）。"""
    return "".join(w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in _CORP_WORDS)


def registered_domain(domain: str) -> str:
    """注册名加后缀：careers.acme.com → acme.com，jobs.acme.co.uk → acme.co.uk。子域名谁都能起，只比这一段。"""
    parts = (domain or "").lower().strip(".").split(".")
    if len(parts) < 2:
        return ""
    n = 3 if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL else 2
    return ".".join(parts[-n:])


#: 邮件服务商的点击跟踪跳转（实测：HackerRank 走 Postmark，Roblox 走 SendGrid）。
#: 跳到哪由发件方决定，所以只在发件方本身可信时放行
TRACKING_DOMAINS: frozenset[str] = frozenset({
    "pstmrk.it", "sendgrid.net", "mandrillapp.com", "mailgun.org", "list-manage.com", "hubspotlinks.com",
})

_NOT_ACTION = re.compile(r"(?i)unsubscribe|opt-?out|preferences|privacy")

#: 推送的一行放得下的长度（Discord 单条 2000 字）。点击跟踪链接常常一千多字（实测 HPE 的 SendGrid 链接 1323）
MAX_LINK_LEN = 1900


def link_allowed(
    url: str, *, sender_domain: str, company: str, company_domains: Iterable[str] = (),
) -> bool:
    """邮件里的一个链接能不能推给你。

    推送出现在你信任的频道里，旁边还写着公司名和「OA」——钓鱼链接混进来比在邮箱里更像真的。
    所以只放三种：招聘 / 测评平台上的；这家公司自己域名上的（含你在 companies.yaml 登记的）；
    点击跟踪跳转，而且发件方本身是前两种。退订、隐私这类链接一律不推。
    和 domain_belongs_to 一样挡不住换顶级域名的仿冒（stripe.xyz）。
    """
    url = str(url or "")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not host or len(url) > MAX_LINK_LEN or _NOT_ACTION.search(url):
        return False
    platforms = SHARED_ATS_DOMAINS | JOB_BOARD_DOMAINS
    registered = {registered_domain(d) for d in company_domains if d} - {""}

    def ours(h: str) -> bool:
        return (any(domain_matches(h, d) for d in platforms) or domain_belongs_to(h, company)
                or registered_domain(h) in registered)

    if ours(host):
        return True
    return any(domain_matches(host, d) for d in TRACKING_DOMAINS) and ours(sender_domain or "")

SUBJECT_KEYWORDS: tuple[str, ...] = (
    "application", "applied", "applying", "interview", "assessment", "offer",
    "unfortunately", "next steps", "candidacy", "thank you for your interest",
    "coding challenge", "online assessment", "phone screen", "hackerrank", "codesignal",
    # 实测漏掉的：HPE「Congratulations! You are invited to our screening process」
    "screening", "invited", "invitation",
)


@dataclass
class PrefilterResult:
    passed: bool
    reason: str
    company_id: int | None = None
    via_ats: bool = False
    #: 发件方是谁：company（登记过的公司域名）| ats（招聘系统）| board（求职平台）| keyword（只是主题像）
    #: | named（发件人显示名是你投过的公司）
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


def company_domains(row: Any) -> list[str]:
    """一家公司的发信域名：你登记的（email_domains）加上从验过 DMARC 的邮件学到的（verified_domains）。"""
    return [*db.load_json(row["email_domains_json"]), *db.load_json(row["verified_domains_json"])]


def company_domain_map(conn: sqlite3.Connection) -> dict[str, int]:
    """公司自有域名 → company_id。**共享 ATS 域名被排除在外。**"""
    out: dict[str, int] = {}
    for r in conn.execute("SELECT id, email_domains_json, verified_domains_json FROM companies"):
        for d in company_domains(r):
            d = str(d).lower().strip()
            if d and not any(domain_matches(d, s) for s in SHARED_ATS_DOMAINS):
                out[d] = r["id"]
    return dict(sorted(out.items(), key=lambda kv: -len(kv[0])))


def applied_company_names(conn: sqlite3.Connection) -> list[str]:
    """投过的公司（不算合并掉、作废的投递）。"""
    rows = conn.execute(
        "SELECT DISTINCT c.name FROM companies c JOIN jobs j ON j.company_id = c.id "
        "JOIN applications a ON a.job_id = j.id WHERE a.merged_into IS NULL AND a.voided_at IS NULL"
    )
    return [r["name"] for r in rows if len((r["name"] or "").strip()) >= 3]


def screen(email_obj: Any, domain_map: dict[str, int], applied_names: Iterable[str] = ()) -> PrefilterResult:
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
    # 投过的公司用自己没登记的域名来信、主题又没有关键词（HPE 的筛选邀请就是这样漏的）。
    # 显示名谁都能写——这里只决定花不花一次分类，不算可信发件方
    display = email.utils.parseaddr(_get(email_obj, "from_addr"))[0]
    for name in applied_names:
        if display and _pattern(name).search(display):
            return PrefilterResult(True, f"发件人显示名是投过的公司：{name}", channel="named")
    return PrefilterResult(False, "既不是已投公司、也不是招聘系统，主题也不像求职邮件")
