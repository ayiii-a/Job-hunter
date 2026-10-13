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

# 面试分很多轮，状态只说得出「在面试」，漏斗要知道走到第几轮。
# 邀请事件算一轮；完成事件不另算——它完成的是已经邀请过的那轮
ROUND_INVITES: frozenset[str] = frozenset({"phone_screen_invite", "interview_invite", "onsite_invite"})
ROUND_DONE: frozenset[str] = frozenset({"phone_screen_completed", "interview_completed", "onsite_completed"})
# 人工更正轮数：payload {"rounds": n}。自动算错了（比如提醒信被当成新一轮）就追加一条
ROUNDS_SET = "interview_rounds_set"
MAX_ROUND = 10

KNOWN_EVENT_TYPES: frozenset[str] = (
    frozenset(EVENT_TO_STAGE) | TERMINAL_EVENTS | NEUTRAL_EVENTS | {OVERRIDE_EVENT, ROUNDS_SET}
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
        # 你事后更正轮数不算对方有动静
        last = next((e for e in reversed(evs) if e.type != ROUNDS_SET), evs[-1])
        idle = now - last.occurred_at
        if idle >= timedelta(days=ghost_after_days):
            return "ghosted"

    return stage


def _round_no(e: Event) -> int:
    n = (e.payload or {}).get("round")
    return n if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= MAX_ROUND else 0


def interview_rounds(events: Iterable[Event]) -> int:
    """走到过第几轮面试。

    同一轮常常不止一封信：邀请、提醒、改期、确认时间。分类器给每封面试邀请标两样东西
    （写在事件 payload 里）：round（信里写明的第几轮）和 followup（是不是对已有邀请的跟进）。

      写明了第几轮的        按编号去重，「Round 2」就意味着至少两轮
      没写编号的新邀请      各算一轮
      没写编号的跟进信      不算——除非这是唯一的证据（漏收了原邀请），那也至少一轮

    人工更正（ROUNDS_SET）优先。
    """
    evs = sorted(events, key=lambda e: (e.occurred_at, e.id if e.id is not None else 0))
    fixed = [e for e in evs if e.type == ROUNDS_SET]
    if fixed:
        n = (fixed[-1].payload or {}).get("rounds")
        if isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= MAX_ROUND:
            return n
    invites = [e for e in evs if e.type in ROUND_INVITES]
    fresh = [e for e in invites if not (e.payload or {}).get("followup")]
    numbered = {_round_no(e) for e in invites} - {0}
    count = len({_round_no(e) for e in fresh} - {0}) + sum(1 for e in fresh if not _round_no(e))
    n = max(max(numbered, default=0), count)
    if not n and (invites or any(e.type in ROUND_DONE for e in evs)):
        n = 1
    return n


# 漏斗：走到过的最远一级。后一级一定包含在前一级里（拿到 offer 也算过了面试）
FUNNEL: tuple[str, ...] = ("投递", "过简历关", "面试 1 轮", "面试 2 轮", "面试 3 轮+", "offer")

OUTCOME_LABEL = {"rejected": "被拒", "withdrawn": "撤回", "ghosted": "无回音", "offer": "offer"}


@dataclass(frozen=True)
class Progress:
    """一条投递在漏斗里走到了哪。全部由事件推出来，和 status 同源。"""

    status: str | None
    oa: bool = False
    rounds: int = 0
    offer: bool = False

    @property
    def outcome(self) -> str:
        """进行中 / 被拒 / 撤回 / 无回音 / offer"""
        return OUTCOME_LABEL.get(self.status or "", "进行中")

    @property
    def level(self) -> int:
        """FUNNEL 里的下标"""
        if self.offer:
            return 5
        if self.rounds:
            return 1 + min(self.rounds, 3)
        return 1 if self.oa else 0

    @property
    def stage(self) -> str:
        """走到哪：已投递 / OA / 面试第 n 轮 / offer"""
        if self.offer:
            return "offer"
        if self.rounds:
            return f"面试第 {self.rounds} 轮"
        return "OA" if self.oa else "已投递"


def progress(
    events: Sequence[Event] | Iterable[Event], *, now: datetime | None = None, ghost_after_days: int = 30,
) -> Progress:
    evs = list(events)
    status = derive_status(evs, now=now, ghost_after_days=ghost_after_days)
    types = {e.type for e in evs}
    rounds = interview_rounds(evs)
    if not rounds and status in ("phone_screen", "interview_loop", "onsite"):
        rounds = 1                      # 人工把状态改成了面试，却没有面试事件
    return Progress(
        status=status,
        oa=bool(types & {"oa_invite", "oa_completed"}) or status == "oa",
        rounds=rounds,
        offer="offer_received" in types or status == "offer",
    )


def unknown_event_types(events: Iterable[Event]) -> set[str]:
    """挑出没见过的事件类型。

    加新事件类型时忘了在这里登记，它就会被 derive_status 静默忽略——
    状态看起来没错，实际漏了一次推进。所以 rebuild 时顺手报一下。
    """
    return {e.type for e in events if e.type not in KNOWN_EVENT_TYPES}
