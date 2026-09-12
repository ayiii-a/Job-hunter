"""通知。推到 Discord 频道（webhook），没配就退回控制台。

用 webhook 不用 bot：webhook 只能往一个频道发消息，读不了、也管不了别的——
推送要的就这么多，权限给到这里为止。OpenClaw 的聊天入口另用一个 bot，两边互不依赖：
外壳挂了，面试邀请照样推得出来。

设计上通知**永远不能让抓取失败**：推送挂了顶多是你少看一条消息，
而抓取结果已经落库了。所以这里所有异常都吞掉、只回报状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import httpx

from . import config

MAX_LEN = 2000              # Discord 单条上限
SUPPRESS_EMBEDS = 1 << 2    # 不展开链接预览


@dataclass
class NotifyResult:
    sent: bool
    channel: str
    detail: str = ""


def configured() -> bool:
    return bool(config.env("DISCORD_WEBHOOK_URL"))


def chunks(text: str, size: int = MAX_LEN) -> list[str]:
    """按行切成不超过 size 的段。

    岗位摘要经常超过 2000 字。直接截断会静默丢掉后面的岗位——丢的正好是你没看到的那些。
    """
    out: list[str] = []
    cur = ""
    for line in text.splitlines():
        line = line[:size]  # ponytail: 单行超过 2000 字直接截断，摘要里不会出现这么长的行
        if cur and len(cur) + 1 + len(line) > size:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def send(text: str) -> NotifyResult:
    url = config.env("DISCORD_WEBHOOK_URL")
    if not url:
        return NotifyResult(False, "none", "没配 DISCORD_WEBHOOK_URL")
    parts = chunks(text)
    if not parts:
        return NotifyResult(False, "discord", "空消息，没发")
    try:
        for part in parts:
            r = httpx.post(
                url,
                json={
                    "content": part,
                    "flags": SUPPRESS_EMBEDS,
                    # 推送文本里可能有 JD、邮件派生出来的字。@everyone 不许真的去 @ 人
                    "allowed_mentions": {"parse": []},
                },
                timeout=20.0,
            )
            if r.status_code not in (200, 204):
                return NotifyResult(False, "discord", f"HTTP {r.status_code}: {r.text[:120]}")
        return NotifyResult(True, "discord")
    except httpx.HTTPError as exc:
        # 只报异常类型：异常信息里可能带着 webhook URL，而 URL 本身就是密钥——
        # 这条 detail 会经 send_notification 回到模型的上下文里
        return NotifyResult(False, "discord", type(exc).__name__)


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
