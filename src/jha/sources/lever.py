"""Lever 适配器。

一次调用就拿到全部内容，包括 JD 全文（`descriptionPlain`）——不需要详情请求。
代价是**没有任何更新时间戳**：`createdAt` 是发布时间，岗位改了它不动。
所以 Lever 只能靠 content_hash 判断岗位有没有变化。

另一个坑：`createdAt` 是 **epoch 毫秒**，另外两家都是 ISO8601。
归一化不处理这个差异的话，posted_at 列里会混进 1565990241800 这种值。
"""

from __future__ import annotations

from typing import Any

import httpx

from .base import Adapter, RawJob, epoch_ms_to_iso

API = "https://api.lever.co/v0/postings"


def _description(p: dict[str, Any]) -> str | None:
    """取 JD 全文。

    坑：`descriptionBodyPlain` **是 `descriptionPlain` 的子串**（实测 leverdemo
    板子上每一条都是），不是它的补充。天真地把两个拼起来会让正文出现两遍——
    存储翻倍事小，Phase 2 每个岗位喂给 LLM 的 token 直接多出七成才是真花钱。

    所以以 descriptionPlain 为准；只有当 body 确实不在里面时才补上，
    以防 Lever 哪天改了语义。
    """
    plain = (p.get("descriptionPlain") or "").strip()
    body = (p.get("descriptionBodyPlain") or "").strip()
    if body and body not in plain:
        plain = f"{plain}\n\n{body}".strip()
    return plain or None


class LeverAdapter(Adapter):
    name = "lever"
    provides_jd_in_list = True
    supports_incremental = False     # 没有 updated_at，只能靠 hash

    def list_jobs(self, client: httpx.Client, token: str) -> list[RawJob]:
        r = client.get(f"{API}/{token}", params={"mode": "json"})
        r.raise_for_status()
        data = r.json()
        return [self._normalize(token, p) for p in (data if isinstance(data, list) else [])]

    # -----------------------------------------------------------------
    def _normalize(self, token: str, p: dict[str, Any]) -> RawJob:
        cats = p.get("categories") or {}
        location = (cats.get("location") or "").strip()
        all_locations = tuple(
            loc.strip() for loc in (cats.get("allLocations") or []) if loc and loc.strip()
        )
        jd = _description(p)

        return RawJob(
            source=self.name,
            external_id=str(p.get("id")),
            title=(p.get("text") or "").strip(),
            company_name=token,          # Lever 的接口不返回公司名
            url=p.get("hostedUrl") or p.get("applyUrl") or "",
            location=location,
            all_locations=all_locations,
            remote_type=(p.get("workplaceType") or "").strip(),
            jd_text=jd or None,
            posted_at=epoch_ms_to_iso(p.get("createdAt")),
            source_updated_at=None,
            extra={"team": cats.get("team"), "country": p.get("country")},
        )
