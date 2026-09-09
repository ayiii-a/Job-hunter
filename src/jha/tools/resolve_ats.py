"""从 careers 页反查 ATS 类型和 board_token，并打真实接口验证。

为什么需要这个工具：
路线图 Phase 0 让你「打开 careers 页看 URL」判断 ATS，听起来是 30 秒的事。
实际上 board_token 经常不等于公司名（Databricks 的板子未必叫 databricks），
而且很多公司把 job board 用 iframe / JS 嵌在自己域名下，地址栏上什么都看不出来。
50–80 家纯手工排查是好几个小时的枯燥活。

策略：先从最终 URL 和页面 HTML 里正则捞候选 token，再补上按公司名猜的几个变体，
最后逐个打真实 API 验证——**只有真的返回了岗位才算数**，不猜。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Iterable

import httpx

UA = "job-hunting-agent/0.1 (personal job search tool)"
TIMEOUT = httpx.Timeout(25.0)

# 这些路径段是 ATS 自己的固定结构，不是公司 token
_NOISE = {
    "embed", "js", "job_board", "jobs", "posting-api", "v1", "v0",
    "boards", "postings", "api", "search", "www", "job", "static",
}

_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "greenhouse": (
        re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.I),
        re.compile(r"(?:job-)?boards\.greenhouse\.io/embed/job_board[^\"'\s]*?[?&]for=([A-Za-z0-9_-]+)", re.I),
        re.compile(r"greenhouse\.io/embed/job_board/js[^\"'\s]*?[?&]for=([A-Za-z0-9_-]+)", re.I),
        re.compile(r"(?:job-)?boards\.greenhouse\.io/([A-Za-z0-9_-]+)", re.I),
    ),
    "lever": (
        re.compile(r"api\.lever\.co/v0/postings/([A-Za-z0-9_-]+)", re.I),
        re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_-]+)", re.I),
    ),
    "ashby": (
        re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_.-]+)", re.I),
        re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)", re.I),
    ),
    "workday": (
        re.compile(r"([A-Za-z0-9_-]+)\.(?:wd\d+)\.myworkdayjobs\.com", re.I),
    ),
}


@dataclass
class Candidate:
    ats_type: str
    token: str
    origin: str          # 这个候选是怎么来的：url / html / guess
    verified: bool = False
    job_count: int | None = None
    detail: str = ""


# ---------------------------------------------------------------------------
# 验证：只有真的返回岗位才算数
# ---------------------------------------------------------------------------

def _verify_greenhouse(client: httpx.Client, token: str) -> tuple[bool, int | None, str]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    r = client.get(url)
    if r.status_code != 200:
        return False, None, f"HTTP {r.status_code}"
    jobs = (r.json() or {}).get("jobs") or []
    return bool(jobs), len(jobs), "ok" if jobs else "接口通但没有岗位"


def _verify_lever(client: httpx.Client, token: str) -> tuple[bool, int | None, str]:
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    r = client.get(url)
    if r.status_code != 200:
        return False, None, f"HTTP {r.status_code}"
    data = r.json()
    jobs = data if isinstance(data, list) else []
    return bool(jobs), len(jobs), "ok" if jobs else "接口通但没有岗位"


def _verify_ashby(client: httpx.Client, token: str) -> tuple[bool, int | None, str]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
    r = client.get(url)
    if r.status_code != 200:
        return False, None, f"HTTP {r.status_code}"
    jobs = (r.json() or {}).get("jobs") or []
    return bool(jobs), len(jobs), "ok" if jobs else "接口通但没有岗位"


_VERIFIERS = {
    "greenhouse": _verify_greenhouse,
    "lever": _verify_lever,
    "ashby": _verify_ashby,
}


# ---------------------------------------------------------------------------
# 候选收集
# ---------------------------------------------------------------------------

def _extract(text: str, origin: str) -> list[Candidate]:
    found: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    for ats, patterns in _PATTERNS.items():
        for pat in patterns:
            for m in pat.finditer(text):
                token = m.group(1)
                if token.lower() in _NOISE or len(token) < 2:
                    continue
                key = (ats, token.lower())
                if key in seen:
                    continue
                seen.add(key)
                found.append(Candidate(ats_type=ats, token=token, origin=origin))
    return found


def guess_tokens(name: str) -> list[str]:
    """按公司名猜几个常见变体。命中率不高，但成本是零。"""
    base = name.strip().lower()
    compact = re.sub(r"[^a-z0-9]+", "", base)
    dashed = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    out = []
    for t in (compact, dashed, compact.removesuffix("inc"), compact.removesuffix("labs")):
        if t and len(t) >= 2 and t not in out:
            out.append(t)
    return out


def fetch_page(client: httpx.Client, url: str) -> tuple[str, str]:
    """抓 careers 页，跟随重定向。

    跟随重定向是必须的：boards.greenhouse.io 现在 301 到 job-boards.greenhouse.io，
    只看你手里那个旧链接会认错。
    """
    r = client.get(url, follow_redirects=True)
    return str(r.url), r.text


_ORIGIN_RANK = {"url": 0, "html": 1, "guess": 2}


def _merge(into: list[Candidate], found: Iterable[Candidate]) -> None:
    """按 (ats, token) 去重，保留来源最可信的那条。

    同一个 token 常常在最终 URL 和页面 HTML 里各出现一次，不去重的话
    列表里全是重复项，也会把同一个接口验证两遍。
    """
    index = {(c.ats_type, c.token.lower()): c for c in into}
    for cand in found:
        key = (cand.ats_type, cand.token.lower())
        existing = index.get(key)
        if existing is None:
            into.append(cand)
            index[key] = cand
        elif _ORIGIN_RANK[cand.origin] < _ORIGIN_RANK[existing.origin]:
            existing.origin = cand.origin


def resolve(
    target: str,
    *,
    name: str | None = None,
    verify: bool = True,
) -> list[Candidate]:
    """target 可以是 careers URL，也可以直接是公司名。"""
    is_url = target.startswith("http://") or target.startswith("https://")
    company = name or (target if not is_url else "")

    candidates: list[Candidate] = []
    with httpx.Client(headers={"User-Agent": UA}, timeout=TIMEOUT) as client:
        if is_url:
            # 先从【用户给的那个 URL】提取，再去抓页面。
            # 顺序很重要：ATS 的板子地址常常 302 跳回公司自己的 careers 页
            # （job-boards.greenhouse.io/databricks -> databricks.com/...），
            # 只看跳转后的最终地址，反而会把地址栏里现成的 token 弄丢。
            _merge(candidates, _extract(target, "url"))
            try:
                final_url, html = fetch_page(client, target)
                _merge(candidates, _extract(final_url, "url"))
                _merge(candidates, _extract(html, "html"))
            except httpx.HTTPError as exc:
                print(f"  ! 抓取 careers 页失败：{exc}", file=sys.stderr)

        if company:
            _merge(
                candidates,
                [
                    Candidate(ats, token, "guess")
                    for token in guess_tokens(company)
                    for ats in ("greenhouse", "lever", "ashby")
                ],
            )

        if verify:
            for c in candidates:
                verifier = _VERIFIERS.get(c.ats_type)
                if verifier is None:
                    c.detail = "Workday 没有稳定的公开接口，需要人工确认"
                    continue
                try:
                    c.verified, c.job_count, c.detail = verifier(client, c.token)
                except (httpx.HTTPError, ValueError) as exc:
                    c.verified, c.detail = False, f"验证失败：{type(exc).__name__}"

    # 验证通过的排前面，其次按来源可信度（页面里捞到的优于猜的），再按岗位数
    candidates.sort(
        key=lambda c: (not c.verified, _ORIGIN_RANK.get(c.origin, 9), -(c.job_count or 0))
    )
    return candidates


def format_yaml_entry(name: str, c: Candidate, careers_url: str = "") -> str:
    """输出可以直接粘进 config/companies.yaml 的片段。"""
    lines = [
        f"  - name: {name}",
        f"    ats_type: {c.ats_type}",
        f"    board_token: {c.token}",
    ]
    if careers_url:
        lines.append(f"    careers_url: {careers_url}")
    lines.append("    email_domains: []   # 填上！Phase 5 靠它把邮件匹配回投递记录")
    lines.append("    priority: 3")
    return "\n".join(lines)
