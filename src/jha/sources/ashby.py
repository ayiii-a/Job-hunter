"""Ashby 适配器。

一次调用拿全，且**加一个参数就能拿到结构化薪资**：
`?includeCompensation=true` 会返回形如 `$211.4K – $290.6K • Offers Equity`
的现成字符串。路线图的原则——能从 API 直接拿到的字段一律不过 LLM——
在这里最省钱：Phase 2 不用再让模型从 JD 里猜薪资范围。

两个实测发现的坑：
  1. 标题可能带前导空格（实测 Ramp 的 " Security Engineer, Cloud"）
  2. 远程岗位常常主地点写着总部，"Remote (US)" 躲在 secondaryLocations 里。
     只看 location 字段会把一批远程岗位漏掉。
"""

from __future__ import annotations

from typing import Any

import httpx

from .base import Adapter, RawJob

API = "https://api.ashbyhq.com/posting-api/job-board"


class AshbyAdapter(Adapter):
    name = "ashby"
    provides_jd_in_list = True
    supports_incremental = False     # publishedAt 是发布时间，不是更新时间

    def list_jobs(self, client: httpx.Client, token: str) -> list[RawJob]:
        r = client.get(f"{API}/{token}", params={"includeCompensation": "true"})
        r.raise_for_status()
        jobs = (r.json() or {}).get("jobs") or []
        # isListed=False 是已下架但还留在接口里的
        return [self._normalize(token, j) for j in jobs if j.get("isListed", True)]

    # -----------------------------------------------------------------
    def _normalize(self, token: str, j: dict[str, Any]) -> RawJob:
        secondary = tuple(
            (s.get("location") or "").strip()
            for s in (j.get("secondaryLocations") or [])
            if (s.get("location") or "").strip()
        )
        comp = j.get("compensation") or {}
        salary = (
            comp.get("compensationTierSummary")
            or comp.get("scrapeableCompensationSalarySummary")
            or None
        )
        remote = (j.get("workplaceType") or "").strip()
        if not remote and j.get("isRemote"):
            remote = "Remote"

        return RawJob(
            source=self.name,
            external_id=str(j.get("id")),
            title=(j.get("title") or "").strip(),     # 实测有前导空格
            company_name=token,
            url=j.get("jobUrl") or j.get("applyUrl") or "",
            location=(j.get("location") or "").strip(),
            all_locations=secondary,
            remote_type=remote,
            jd_text=(j.get("descriptionPlain") or "").strip() or None,
            salary_raw=salary,
            posted_at=j.get("publishedAt"),
            source_updated_at=None,
            extra={
                "department": j.get("department"),
                "team": j.get("team"),
                "employment_type": j.get("employmentType"),
            },
        )
