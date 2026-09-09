"""从 events 推导 applications.status。

路线图第 2.2 节定的规矩：**events 是唯一真相源，applications.status 只是物化缓存**。

derive_status 是纯函数——不碰数据库、不读时钟（now 由调用方传入）。
这带来两个直接好处：
  1. 它是整个项目里最好写单元测试的部分（见 tests/test_status.py）
  2. 分类误判之后不用手改数据，追加一条 status_override 事件再 rebuild 就行

事件类型是有意冗余的（invite / completed 各一条），因为 events 是追加式日志，
记得越细，后面复盘「卡在哪一轮」时能还原的信息越多。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

# 推进型状态，按先后排序。走到过的最远一档就是当前状态。
STAGE_ORDER: tuple[str, ...] = (
    "applied",
    "oa",
    "phone_screen",
    "interview_loop",
    "onsite",
    "offer",
)

# 终态。一旦发生就盖过一切推进型状态。
TERMINAL_EVENTS: frozenset[str] = frozenset({"rejected", "withdrawn"})

# 由规则推出来的状态，不由事件直接产生。
DERIVED_ONLY: frozenset[str] = frozenset({"ghosted"})

VALID_STATUSES: frozenset[str] = frozenset(STAGE_ORDER) | TERMINAL_EVENTS | DERIVED_ONLY

# 事件类型 -> 它代表推进到了哪一档
EVENT_TO_STAGE: dict[str, str] = {
    "applied": "applied",
    "confirmation_received": "applied",
    "oa_invite": "oa",
    "oa_completed": "oa",
    "phone_screen_invite": "phone_screen",
    "phone_screen_completed": "phone_screen",
    "interview_invite": "interview_loop",
    "interview_completed": "interview_loop",
    "onsite_invite": "onsite",
    "onsite_completed": "onsite",
    "offer_received": "offer",
}

# 不推进状态、但值得记的事件（follow-up、推荐人打招呼、面试排期等）
NEUTRAL_EVENTS: frozenset[str] = frozenset(
    {
        "note",
        "follow_up_sent",
        "referral_requested",
        "referral_submitted",
        "scheduling",
        "recruiter_outreach",
    }
)

# 人工更正通道。payload 里带 {"status": "..."}，优先级最高。
OVERRIDE_EVENT = "status_override"

KNOWN_EVENT_TYPES: frozenset[str] = (
    frozenset(EVENT_TO_STAGE) | TERMINAL_EVENTS | NEUTRAL_EVENTS | {OVERRIDE_EVENT}
)


@dataclass(frozen=True)
class Event:
    """一条事件。occurred_at 用 aware/naive datetime 都可以，但同一批要一致。"""

    type: str
    occurred_at: datetime
    id: int | None = None
    source: str = "agent"
    payload: dict[str, Any] = field(default_factory=dict)


def _rank(stage: str) -> int:
    return STAGE_ORDER.index(stage)


def derive_status(
    events: Sequence[Event] | Iterable[Event],
    *,
    now: datetime | None = None,
    ghost_after_days: int = 30,
) -> str | None:
    """由事件推导状态。

    返回 None 表示这条 application 一条事件都没有——那是数据错误（投递时应当
    同时写一条 applied 事件），调用方应该把它报出来而不是静默当成 applied。

    优先级：人工更正 > 终态 > 走到过的最远一档 > ghosted 规则。

    now 传 None 就不做 ghosted 判定，这样纯粹的状态推导可以完全不依赖时钟。
    """
    evs = sorted(events, key=lambda e: (e.occurred_at, e.id if e.id is not None else 0))
    if not evs:
        return None

    # 1. 人工更正优先级最高——这是误判之后的纠错通道，取最后一条
    overrides = [e for e in evs if e.type == OVERRIDE_EVENT]
    if overrides:
        target = (overrides[-1].payload or {}).get("status")
        if target in VALID_STATUSES:
            return str(target)

    # 2. 终态盖过推进型状态。同一天既有 rejected 又有 withdrawn 时，取时间靠后的
    for ev in reversed(evs):
        if ev.type in TERMINAL_EVENTS:
            return ev.type

    # 3. 走到过的最远一档
    stage = "applied"
    for ev in evs:
        mapped = EVENT_TO_STAGE.get(ev.type)
        if mapped and _rank(mapped) > _rank(stage):
            stage = mapped

    # 4. ghosted：非终态、且太久没有任何动静。
    #    offer 不判 ghosted——拿到 offer 之后没人理你是另一回事。
    if now is not None and stage != "offer":
        idle = now - evs[-1].occurred_at
        if idle >= timedelta(days=ghost_after_days):
            return "ghosted"

    return stage


def unknown_event_types(events: Iterable[Event]) -> set[str]:
    """挑出没见过的事件类型。

    加新事件类型时忘了在这里登记，它就会被 derive_status 静默忽略——
    状态看起来没错，实际漏了一次推进。所以 rebuild 时顺手报一下。
    """
    return {e.type for e in events if e.type not in KNOWN_EVENT_TYPES}
