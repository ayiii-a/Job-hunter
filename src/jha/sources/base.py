"""适配器的公共类型和工具。

路线图 Phase 1 的核心判断：三个 ATS 的能力**并不对称**，不能用一个
`fetch()` 糊过去。这里把差异显式建模成两个类属性：

    provides_jd_in_list —— 列表里就带 JD 全文吗？
        Lever / Ashby 是 True，Greenhouse 是 False（要第二次调用）
    supports_incremental —— 有没有可比较的更新时间戳？
        只有 Greenhouse 有 updated_at；另外两家只能靠 content_hash

把这两个写成属性而不是藏在各自的实现里，是为了让调用方（ingest）能据此
决定抓取策略，而不是对三家做同样的事。
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

import httpx

USER_AGENT = "job-hunting-agent/0.1 (personal job search tool)"
TIMEOUT = httpx.Timeout(45.0)


@dataclass(frozen=True)
class RawJob:
    """归一化之后的岗位。三家 ATS 的字段差异到这里为止。"""

    source: str
    external_id: str
    title: str
    company_name: str
    url: str
    location: str = ""
    # 一个岗位可能挂多个地点（Ashby 的 secondaryLocations、Lever 的 allLocations）。
    # 地点过滤必须看全部——远程岗位常常主地点写着总部、"Remote (US)" 躲在次要地点里
    all_locations: tuple[str, ...] = ()
    remote_type: str = ""
    jd_text: str | None = None          # None = 还没抓全文
    salary_raw: str | None = None
    posted_at: str | None = None        # ISO8601
    source_updated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    def with_jd(self, jd_text: str | None) -> RawJob:
        return replace(self, jd_text=jd_text)

    @property
    def location_blob(self) -> str:
        """所有地点拼成一串，供子串匹配用。"""
        parts = [self.location, *self.all_locations, self.remote_type]
        return " | ".join(p for p in parts if p)

    def content_hash(self) -> str:
        """内容指纹。

        按路线图：company + title + location + JD 前 500 字。
        Lever / Ashby 没有 updated_at，只能靠这个判断岗位有没有变。
        """
        basis = "\x1f".join(
            [
                _norm(self.company_name),
                _norm(self.title),
                _norm(self.location),
                _norm((self.jd_text or "")[:500]),
            ]
        )
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


class Adapter:
    """所有 ATS 适配器的基类。"""

    name: str = ""
    #: 列表调用是否已经带回 JD 全文
    provides_jd_in_list: bool = True
    #: 是否提供可比较的更新时间戳（有的话能跳过大量详情抓取）
    supports_incremental: bool = False

    def list_jobs(self, client: httpx.Client, token: str) -> list[RawJob]:
        raise NotImplementedError

    def fetch_detail(self, client: httpx.Client, token: str, job: RawJob) -> RawJob:
        """补上 JD 全文。列表已经带了的适配器直接原样返回。"""
        return job


# ---------------------------------------------------------------------------
# 解析工具
# ---------------------------------------------------------------------------

_BLOCK_BREAK = re.compile(r"(?i)</(p|div|li|h[1-6]|tr|blockquote)\s*>|<br\s*/?>")
_TAG = re.compile(r"<[^>]+>")


def html_to_text(raw: str | None) -> str:
    """把 ATS 返回的 HTML 片段转成可读、可搜索的纯文本。

    Greenhouse 的 content 字段是**转义过的 HTML**（拿到手是 `&lt;p&gt;`），
    所以要先 unescape 再去标签。先把块级结束标签换成换行，否则整篇 JD
    会挤成一行，后面读 JD 和做关键词匹配都难受。
    """
    if not raw:
        return ""
    text = html.unescape(raw)
    text = _BLOCK_BREAK.sub("\n", text)
    text = _TAG.sub("", text)
    text = html.unescape(text)          # 实体可能嵌套一层
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def epoch_ms_to_iso(value: Any) -> str | None:
    """Lever 的时间戳是 epoch 毫秒，另外两家是 ISO8601。"""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def http_client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
