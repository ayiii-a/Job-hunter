"""规则初筛 —— Phase 1 的承重墙。

实测：Databricks 一家公司 870 个在招岗位、178 个不同地点，只有约三分之一
在美国。80 家目标公司很可能意味着 5,000–15,000 个开放岗位。没有这道过滤，
Phase 2 第一天就会被成本和噪音双杀。

顺序按路线图：**地点先过**，然后才是标题排除、标题包含。
地点放第一是因为它砍掉的量级最大，而且完全不需要理解岗位内容。

全部不过 LLM。

关于匹配方式：用**词边界**而不是裸子串。这不是洁癖，是必需的——
    "India"  裸子串会匹配 "Indianapolis"、"Indiana"
    "Intern" 裸子串会匹配 "Internal Tools Engineer"、"International"
两个都会静默滤掉本该看到的岗位，而且因为是排除项，你永远不会发现。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable, Sequence

from .sources import RawJob


@dataclass(frozen=True)
class FilterResult:
    passed: bool
    reason: str
    tier: str | None = None      # 命中 title_tiers 的哪一档

    @property
    def rejected(self) -> bool:
        return not self.passed


_US_STATE_NAMES = (
    "Alabama Alaska Arizona Arkansas California Colorado Connecticut Delaware Florida "
    "Georgia Hawaii Idaho Illinois Indiana Iowa Kansas Kentucky Louisiana Maine Maryland "
    "Massachusetts Michigan Minnesota Mississippi Missouri Montana Nebraska Nevada Ohio "
    "Oklahoma Oregon Pennsylvania Tennessee Texas Utah Vermont Virginia Washington "
    "Wisconsin Wyoming"
).split() + [
    "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina",
    "North Dakota", "Rhode Island", "South Carolina", "South Dakota", "West Virginia",
    "District of Columbia",
]

_US_STATE_CODES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE "
    "NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()

#: 预设词表。在 target_profile 的 locations / locations_exclude 里写 `preset:us` 就能引用。
#:
#: 州名缩写为什么要写成 ", CA" 而不是 "CA"：匹配是不区分大小写的词边界匹配，
#: 裸的两字母缩写会撞上英文单词——`\bIN\b` 匹配 "in"、`\bOR\b` 匹配 "or"、
#: `\bME\b` 匹配 "me"、`\bOK\b` 匹配 "ok"。加个逗号前缀锚在
#: "City, ST" 这个真实写法上，就不会误伤。
#:
#: non_us 故意不收和美国地名撞车的词：Georgia（州）、Mexico（New Mexico）、
#: Jersey（New Jersey）、Lebanon（新罕布什尔和宾州都有）。
PRESETS: dict[str, tuple[str, ...]] = {
    "us": tuple(
        ["United States", "USA", "U.S.", "US-", "Remote - US", "Remote (US)"]
        + _US_STATE_NAMES
        + [f", {code}" for code in _US_STATE_CODES]
    ),
    # ponytail: 只列了招聘量大的国家和地区，没列到的国家写成「Remote Xxx」仍会漏进来
    "non_us": (
        "Canada", "Mexico City", "Brazil", "Argentina", "Chile", "Colombia", "Costa Rica",
        "United Kingdom", "UK", "England", "Scotland", "Ireland", "Germany", "France", "Spain",
        "Portugal", "Netherlands", "Belgium", "Switzerland", "Austria", "Italy", "Poland",
        "Czechia", "Czech Republic", "Romania", "Ukraine", "Sweden", "Norway", "Denmark",
        "Finland", "Estonia", "Lithuania", "Latvia", "Serbia", "Croatia", "Greece", "Turkey",
        "Israel", "United Arab Emirates", "UAE", "Dubai", "Saudi Arabia", "Egypt", "Nigeria",
        "Kenya", "South Africa", "India", "Pakistan", "Bangladesh", "Sri Lanka", "Philippines",
        "Vietnam", "Thailand", "Malaysia", "Indonesia", "Singapore", "China", "Hong Kong",
        "Taiwan", "Japan", "Korea", "Australia", "New Zealand",
        "Europe", "EMEA", "APAC", "LATAM", "Latin America", "South America", "Asia",
        "Middle East", "Africa",
    ),
}

#: AI 公司常用的通用职位名，不代表资深。里面的 "Staff" 不该触发标题排除——
#: 实测 Perplexity 43 个、xAI 13 个这类岗位被整批挡掉，其中有
#: "Member of Technical Staff (Machine Learning Engineer, Search)"。
_GENERIC_TITLE = re.compile(r"\bmember of (the )?technical staff\b", re.IGNORECASE)

#: 位置里明确写了美国。"US" 区分大小写，免得撞上英文单词 "us"
_US_WORD = re.compile(r"\bUS\b")


def expand_terms(terms: Sequence[str] | None) -> list[str]:
    """把 `preset:xxx` 展开成实际词表。未知的预设名原样留着，好让你一眼看出写错了。"""
    out: list[str] = []
    for term in terms or []:
        if isinstance(term, str) and term.startswith("preset:"):
            out.extend(PRESETS.get(term[7:].strip().lower(), (term,)))
        else:
            out.append(term)
    return out


@lru_cache(maxsize=4096)
def _pattern(term: str) -> re.Pattern[str]:
    """把一个关键词编译成带词边界的正则。

    首尾是字母数字才加 \\b —— 像 "US-" 这种以连字符结尾的词，
    后面加 \\b 会让它永远匹配不上。
    """
    term = term.strip()
    esc = re.escape(term)
    prefix = r"\b" if term[:1].isalnum() else ""
    suffix = r"\b" if term[-1:].isalnum() else ""
    return re.compile(prefix + esc + suffix, re.IGNORECASE)


def _first_match(haystack: str, terms: Iterable[str]) -> str | None:
    if not haystack:
        return None
    for term in terms:
        if term and _pattern(term).search(haystack):
            return term
    return None


def _tier_of(title: str, tiers: dict[str, Sequence[str]]) -> tuple[str | None, str | None]:
    """岗位标题落在哪一档。按 tier 名字排序保证结果稳定。"""
    for tier_name in sorted(tiers or {}):
        hit = _first_match(title, tiers[tier_name] or [])
        if hit:
            return tier_name, hit
    return None, None


def _remote_abroad(loc: str) -> str | None:
    """「Remote Spain」「Remote - EMEA」：写着 remote，限定的却是美国以外的地方。

    locations 里的 Remote 和 remote_ok 都会把它放进来——实测 Affirm 的
    「Remote Spain」就是这样进的库。同时写了美国的（「Remote (US or Canada)」）不算。
    """
    if not _pattern("remote").search(loc):
        return None
    if _US_WORD.search(loc) or _first_match(loc, PRESETS["us"]):
        return None
    return _first_match(loc, PRESETS["non_us"])


def _location_ok(job: RawJob, target: dict[str, Any]) -> tuple[bool, str]:
    """地点判定：**逐个地点单独判**，不是把所有地点拼成一串判。

    这个区别不是细节。一个岗位常常同时挂多个地点，比如
    ["New York, NY (HQ)", "Remote (Canada)", "Remote (US)"]。
    拿整串去匹配排除项，Canada 会把整个岗位杀掉——可它明明是个有效的纽约岗位。
    实测 Ramp 的纽约岗位就是这样被整批误杀的。

    正确语义：**只要有一个地点可接受，这个岗位就可接受。**

    remote_type 只说「这是远程岗」，不说在哪，所以只在没有任何具体地点时才算一个候选。
    实测 28 个岗位靠它混进来（ElevenLabs 的 Germany + Remote、Sierra 的 Munich + Remote……），
    全在美国以外，没有一个是美国岗位。
    """
    concrete = [loc.strip() for loc in (job.location, *job.all_locations) if loc and loc.strip()]
    remote_type = (job.remote_type or "").strip()
    candidates = concrete or ([remote_type] if remote_type else [])
    if not candidates:
        return False, "地点为空，无法判断"

    excludes = expand_terms(target.get("locations_exclude"))
    locations = expand_terms(target.get("locations"))
    remote_ok = bool(target.get("remote_ok"))

    blocked: list[str] = []
    for loc in candidates:
        bad = _first_match(loc, excludes) or _remote_abroad(loc)
        if bad:
            blocked.append(bad)
            continue
        hit = _first_match(loc, locations)
        if hit:
            return True, f"地点命中：{hit}"
        if remote_ok and _pattern("remote").search(loc):
            return True, "远程"

    if blocked and len(blocked) == len(candidates):
        return False, f"地点排除：{blocked[0]}"
    shown = job.location or candidates[0]
    return False, f"地点不匹配：{shown[:40]}"


def screen(job: RawJob, target: dict[str, Any]) -> FilterResult:
    """对单个岗位跑规则初筛。"""
    blob = job.location_blob
    title = job.title

    # ---- 第一道：地点 ----------------------------------------------
    ok, reason = _location_ok(job, target)
    if not ok:
        return FilterResult(False, reason)

    if target.get("remote_only") and not (_pattern("remote").search(blob) if blob else False):
        return FilterResult(False, "只要远程，此岗非远程")

    # ---- 第二道：标题排除 ------------------------------------------
    bad = _first_match(_GENERIC_TITLE.sub(" ", title), expand_terms(target.get("titles_exclude")))
    if bad:
        return FilterResult(False, f"标题排除：{bad}")

    # ---- 第三道：标题包含 ------------------------------------------
    good = _first_match(title, expand_terms(target.get("titles_include")))
    if not good:
        return FilterResult(False, "标题不在目标范围内")

    tier, _ = _tier_of(title, target.get("title_tiers") or {})
    return FilterResult(True, f"命中：{good}", tier=tier)


@dataclass
class ScreenSummary:
    kept: list[tuple[RawJob, FilterResult]]
    dropped: list[tuple[RawJob, FilterResult]]

    @property
    def reasons(self) -> dict[str, int]:
        """丢弃原因的分布。

        校准全靠它：路线图要求前两周每天看漏报和误报，
        没有这个统计就只能凭感觉调过滤条件。
        """
        counts: dict[str, int] = {}
        for _, res in self.dropped:
            key = res.reason.split("：")[0]
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def screen_all(jobs: Iterable[RawJob], target: dict[str, Any]) -> ScreenSummary:
    kept: list[tuple[RawJob, FilterResult]] = []
    dropped: list[tuple[RawJob, FilterResult]] = []
    for job in jobs:
        res = screen(job, target)
        (kept if res.passed else dropped).append((job, res))
    return ScreenSummary(kept=kept, dropped=dropped)
