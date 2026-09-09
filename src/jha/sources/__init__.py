"""ATS 适配器注册表。"""

from __future__ import annotations

from .ashby import AshbyAdapter
from .base import Adapter, RawJob, epoch_ms_to_iso, html_to_text, http_client
from .greenhouse import GreenhouseAdapter
from .lever import LeverAdapter

ADAPTERS: dict[str, Adapter] = {
    a.name: a for a in (GreenhouseAdapter(), LeverAdapter(), AshbyAdapter())
}

# Workday 没有稳定的公开接口，Phase 1 第二轮才用 Playwright 处理。
# 这里显式列出来，好让 CLI 能给出「暂不支持」而不是「未知 ATS」。
UNSUPPORTED = {"workday"}


def get_adapter(ats_type: str | None) -> Adapter | None:
    return ADAPTERS.get((ats_type or "").strip().lower())


__all__ = [
    "ADAPTERS",
    "UNSUPPORTED",
    "Adapter",
    "RawJob",
    "get_adapter",
    "html_to_text",
    "epoch_ms_to_iso",
    "http_client",
]
