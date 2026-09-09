"""通知。Telegram 优先，没配就退回控制台。

设计上通知**永远不能让抓取失败**：推送挂了顶多是你少看一条消息，
而抓取结果已经落库了。所以这里所有异常都吞掉、只回报状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import httpx

from . import config

API = "https://api.telegram.org"
MAX_LEN = 3900          # Telegram 单条上限 4096，留点余量


@dataclass
class NotifyResult:
    sent: bool
    channel: str
    detail: str = ""


def configured() -> bool:
    return bool(config.env("TELEGRAM_BOT_TOKEN") and config.env("TELEGRAM_CHAT_ID"))


def send(text: str) -> NotifyResult:
    if not configured():
        return NotifyResult(False, "none", "没配 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    token = config.env("TELEGRAM_BOT_TOKEN")
    chat_id = config.env("TELEGRAM_CHAT_ID")
    try:
        r = httpx.post(
            f"{API}/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text[:MAX_LEN],
                "disable_web_page_preview": True,
            },
            timeout=20.0,
        )
        if r.status_code != 200:
            return NotifyResult(False, "telegram", f"HTTP {r.status_code}: {r.text[:120]}")
        return NotifyResult(True, "telegram")
    except httpx.HTTPError as exc:
        return NotifyResult(False, "telegram", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 消息拼装
# ---------------------------------------------------------------------------

def format_digest(
    new_jobs: Sequence[dict[str, Any]],
    failures: Sequence[Any] = (),
    *,
    limit: int = 25,
) -> str:
    """把新岗位拼成一条摘要。

    有内推线索的岗位排在最前面，并且第一句话就是「先找 X 要内推」——
    位置很重要：内推提示如果排在岗位列表下面，你的第一反应还是去点投递链接。
    """
    lines: list[str] = []

    if failures:
        lines.append("⚠️ 抓取失败：")
        for f in failures:
            lines.append(f"  · {f['name']}（{f['source']}）：{(f['error'] or '')[:90]}")
        lines.append("")

    if not new_jobs:
        lines.append("没有新岗位。")
        return "\n".join(lines)

    with_ref = [j for j in new_jobs if j.get("contacts")]
    without = [j for j in new_jobs if not j.get("contacts")]

    lines.append(f"新岗位 {len(new_jobs)} 个")

    if with_ref:
        lines.append("")
        lines.append(f"★ 这 {len(with_ref)} 个你认识人 —— 先要内推，别直接投：")
        for job in with_ref[:limit]:
            names = "、".join(c["name"] for c in job["contacts"][:3])
            lines.append(f"  · {job['company']} — {job['title']}")
            lines.append(f"    找 {names}")
            if job.get("url"):
                lines.append(f"    {job['url']}")

    if without:
        lines.append("")
        lines.append("其余：")
        for job in without[: max(0, limit - len(with_ref))]:
            tier = f" [{job['tier']}]" if job.get("tier") else ""
            salary = f" · {job['salary']}" if job.get("salary") else ""
            lines.append(f"  · {job['company']} — {job['title']}{tier}")
            lines.append(f"    {job.get('location') or ''}{salary}")
            if job.get("url"):
                lines.append(f"    {job['url']}")

    shown = min(len(new_jobs), limit)
    if len(new_jobs) > shown:
        lines.append("")
        lines.append(f"（还有 {len(new_jobs) - shown} 个，用 agent jobs list 看全部）")

    return "\n".join(lines)
