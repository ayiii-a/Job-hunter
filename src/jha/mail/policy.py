"""状态更新策略：**按误判代价分级，不是按分类难度。** 纯函数。

    confirmation / rejection        自动写 —— events 追加式，误判可以追加更正事件修回来
    oa_invite / interview_invite    自动写，并且推送提醒 —— 状态记错了能纠正，要紧的是你别错过
    scheduling                      记一条中性事件，推送（要你回复）
    offer                           **人工确认** —— 量少、误判代价高，而且 offer 诈骗专挑应届生

## 表里没有这条投递时：新建

你在 LinkedIn、公司官网投的岗位也会来信，表里要有它们。所以确认信、拒信、面试、OA
对不上任何投递时，会新建一条。但凭一封邮件建档，比更新已有记录多两个条件：

  1. 发件方可信：登记过的公司域名、招聘系统（Greenhouse 等）、求职平台（LinkedIn 等）。
     发件域名随便什么的，建档前要你看一眼——钓鱼信最爱冒充招聘方
  2. 认得出是哪家公司

两个条件的默认值都是 False：调用方不说清楚，就不建档。

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

#: 需要你动手的类型：不管最后怎么处理，都推送提醒
ACTION_TYPES = frozenset({"oa_invite", "interview_invite", "offer", "scheduling"})

#: 表里对不上投递时，可以凭这封邮件新建一条的类型
CREATABLE = frozenset({"confirmation", "rejection", "oa_invite", "interview_invite"})

#: 永远人工确认。offer 诈骗专挑应届生和 F-1：假 offer 会要你交钱、先买设备、报 SSN
ALWAYS_HUMAN = frozenset({"offer"})

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
    action: str               # auto_apply | create | queue | record_only | ignore
    event_type: str | None
    reason: str
    alert: bool = False


def decide(
    *, ctype: str, confidence: float, match_status: str, text: str,
    trusted_sender: bool = False, company_known: bool = False,
) -> Decision:
    """reason 会进 agent 看得到的队列——只能是我们自己写的字，不能拼邮件里的内容。"""
    if ctype == "other":
        return Decision("ignore", None, "和求职无关")
    if ctype == "recruiter_outreach":
        return Decision("record_only", None, "招聘方主动联系，不改任何状态")

    event = STATUS_EVENT.get(ctype)
    alert = ctype in ACTION_TYPES
    if event is None:
        return Decision("queue", None, "未归类的情况，交给人", alert=alert)

    if ctype in ALWAYS_HUMAN:
        return Decision("queue", event, "offer 永远人工确认：量少、误判代价高，而且 offer 诈骗专挑应届生",
                        alert=True)

    if ctype == "rejection":
        hit = INVITE_SIGNALS.search(text or "")
        if hit:
            # 命中的只可能是上面正则里的固定词，不会把邮件里的任意文字带进 reason
            return Decision(
                "queue", event,
                f"判成了拒信，但正文里有邀请信号「{hit.group(0)}」——"
                "宁可你看一眼，也不能把面试邀请当拒信",
                alert=True,
            )

    if confidence < AUTO_CONFIDENCE:
        return Decision("queue", event, f"置信度 {confidence:.2f} 低于 {AUTO_CONFIDENCE}", alert=alert)

    if match_status == "exact":
        if ctype == "scheduling":
            return Decision("auto_apply", event, "排期邮件：记一条中性事件、不改状态，但需要你回复",
                            alert=True)
        if ctype in ("oa_invite", "interview_invite"):
            return Decision("auto_apply", event, "已更新投递状态并推送提醒；记错了可以追加更正事件",
                            alert=True)
        return Decision("auto_apply", event, "低代价、可追加事件纠正——自动写入，事后抽查")

    if match_status == "ambiguous":
        return Decision("queue", event, "这家公司有几条投递，分不出是哪一条", alert=alert)

    # 表里对不上任何投递（none / no_application）
    if ctype not in CREATABLE:
        return Decision("queue", event, "对不上任何投递记录", alert=alert)
    if not company_known:
        return Decision("queue", event, "认不出是哪家公司，没法建档", alert=alert)
    if not trusted_sender:
        return Decision(
            "queue", event,
            "发件方不是登记过的公司域名、招聘系统或求职平台——钓鱼信最爱冒充招聘方，你确认后再建档",
            alert=alert,
        )
    return Decision("create", event, "表里还没有这条投递，新建一条（你在别处投的也记进来）", alert=alert)
