"""状态更新策略：**按误判代价分级，不是按分类难度。** 纯函数。

    confirmation   自动写 —— 误判代价几乎为零
    rejection      自动写 —— events 追加式，误判可以追加更正事件修回来
    oa / interview / offer   **永远人工确认** —— 误判代价高且不可逆（错过面试）

## 最贵的那个错误单独设防

路线图风险表第二条：「邮件误判把面试邀请当拒信 → 错过面试」。
这是整个邮件模块唯一**不可挽回**的失败。所以在自动写入拒信之前，
再用确定性正则扫一遍正文里的**邀请信号**——命中就不自动写，改进人工队列。

信号刻意只选**面向未来的排期语言**（schedule / availability / calendly / next round），
不选裸的 "interview"。因为「Thank you for interviewing with us... unfortunately」
是最常见的面试后拒信——把它们全扔进人工队列，队列很快就会被你无视，
那这道防线等于不存在。（和 verify.py 日期误报是同一个教训：狼来了的检查不如没有。）
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 置信度低于这个值，任何类型都不自动写
AUTO_CONFIDENCE = 0.85

STATUS_EVENT: dict[str, str] = {
    "confirmation": "confirmation_received",
    "rejection": "rejected",
    "oa_invite": "oa_invite",
    "interview_invite": "interview_invite",
    "offer": "offer_received",
    "scheduling": "scheduling",
}

ALWAYS_HUMAN = frozenset({"oa_invite", "interview_invite", "offer"})
AUTO_OK = frozenset({"confirmation", "rejection"})

INVITE_SIGNALS = re.compile(
    r"(?i)("
    r"\bschedul(e|ing)\b|\bavailabilit(y|ies)\b|\bcalendly\b|\bbook a time\b|"
    r"\bpick a time\b|\btime slots?\b|\bnext round\b|\binvite you\b|\binvitation to\b|"
    r"\bmov(e|ing) (you|your application|your candidacy) forward\b|"
    r"\bcoding challenge\b|\bonline assessment\b|\bhackerrank\b|\bcodesignal\b|"
    r"\btake[- ]home\b"
    r")"
)


@dataclass(frozen=True)
class Decision:
    action: str               # auto_apply | queue | record_only | ignore
    event_type: str | None
    reason: str
    alert: bool = False


def decide(*, ctype: str, confidence: float, match_status: str, text: str) -> Decision:
    if ctype == "other":
        return Decision("ignore", None, "和求职无关")
    if ctype == "recruiter_outreach":
        return Decision("record_only", None, "招聘方主动联系，不改任何状态")

    event = STATUS_EVENT.get(ctype)

    if ctype in ALWAYS_HUMAN:
        return Decision("queue", event, f"{ctype} 误判代价高且不可逆——永远人工确认", alert=True)

    if ctype == "rejection":
        hit = INVITE_SIGNALS.search(text or "")
        if hit:
            return Decision(
                "queue", event,
                f"判成了拒信，但正文里有邀请信号「{hit.group(0)}」——"
                "宁可你看一眼，也不能把面试邀请当拒信",
                alert=True,
            )

    if confidence < AUTO_CONFIDENCE:
        return Decision("queue", event, f"置信度 {confidence:.2f} 低于 {AUTO_CONFIDENCE}",
                        alert=ctype == "scheduling")
    if match_status != "exact":
        return Decision("queue", event, f"匹配不到唯一的投递记录（{match_status}）",
                        alert=ctype == "scheduling")

    if ctype == "scheduling":
        return Decision("auto_apply", event, "排期邮件：记一条中性事件、不改状态，但需要你回复",
                        alert=True)
    if ctype in AUTO_OK:
        return Decision("auto_apply", event, "低代价、可追加事件纠正——自动写入，事后抽查")
    return Decision("queue", event, "未归类的情况，交给人")
