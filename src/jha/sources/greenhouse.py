"""Greenhouse 适配器。

三家里唯一需要两段抓取、也是唯一能做增量的。

实测数据（Databricks 板子，870 个岗位）：
    不带 content   745 KB
    content=true   9.5 MB      ← 12 倍

而不带 content 的列表里已经有 `updated_at`。所以正确策略是
**先拉便宜的列表，再只对需要的岗位单独拉全文**——单岗详情端点
`/jobs/{id}` 是存在的，不必为了几个岗位把整个板子的全文拖下来。

真正省钱的地方还在后面：规则初筛在详情抓取【之前】跑，870 个岗位过完
地点和标题过滤可能只剩十几个，实际详情请求数是两位数而不是四位数。
"""

from __future__ import annotations

from typing import Any

import httpx

from .base import Adapter, RawJob, html_to_text

API = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseAdapter(Adapter):
    name = "greenhouse"
    provides_jd_in_list = False      # 列表不带 JD，要第二次调用
    supports_incremental = True      # 有 updated_at

    def list_jobs(self, client: httpx.Client, token: str) -> list[RawJob]:
        r = client.get(f"{API}/{token}/jobs")
        r.raise_for_status()
        return [self._normalize(token, j) for j in (r.json() or {}).get("jobs") or []]

    def fetch_detail(self, client: httpx.Client, token: str, job: RawJob) -> RawJob:
        r = client.get(f"{API}/{token}/jobs/{job.external_id}")
        r.raise_for_status()
        data = r.json() or {}
        return job.with_jd(html_to_text(data.get("content")))

    # -----------------------------------------------------------------
    def _normalize(self, token: str, j: dict[str, Any]) -> RawJob:
        location = ((j.get("location") or {}).get("name") or "").strip()
        # offices 常常比 location 更全（一个岗位挂多个办公室时）
        offices = tuple(
            (o.get("location") or o.get("name") or "").strip()
            for o in (j.get("offices") or [])
            if (o.get("location") or o.get("name"))
        )
        return RawJob(
            source=self.name,
            external_id=str(j.get("id")),
            title=(j.get("title") or "").strip(),
            company_name=(j.get("company_name") or token).strip(),
            url=j.get("absolute_url") or "",
            location=location,
            all_locations=offices,
            posted_at=j.get("first_published"),
            source_updated_at=j.get("updated_at"),
            extra={"requisition_id": j.get("requisition_id")},
        )
