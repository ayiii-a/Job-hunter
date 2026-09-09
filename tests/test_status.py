"""derive_status 的单元测试。

这是纯函数，不碰数据库、不读时钟，所以可以把状态机的每条边都钉死。
路线图把「events 是真相源，status 是缓存」立为架构原则——这些测试就是那条原则
的执行层面保证。
"""

from datetime import datetime, timedelta

import pytest

from jha.status import Event, derive_status, unknown_event_types

T0 = datetime(2026, 3, 1, 9, 0, 0)


def ev(type_, days=0, **payload):
    return Event(type=type_, occurred_at=T0 + timedelta(days=days), payload=payload)


def test_no_events_returns_none():
    # 有 application 却没有事件是数据错误，不能静默当成 applied
    assert derive_status([]) is None


def test_single_applied():
    assert derive_status([ev("applied")]) == "applied"


def test_confirmation_does_not_advance_stage():
    # 收到确认邮件只是说明申请进系统了，不代表进入下一轮
    events = [ev("applied"), ev("confirmation_received", 1)]
    assert derive_status(events) == "applied"


def test_progresses_to_furthest_stage():
    events = [
        ev("applied"),
        ev("confirmation_received", 1),
        ev("oa_invite", 3),
        ev("phone_screen_invite", 10),
    ]
    assert derive_status(events) == "phone_screen"


def test_out_of_order_events_still_take_furthest():
    # 邮件是乱序到的，推导结果不能依赖插入顺序
    events = [ev("onsite_invite", 20), ev("applied"), ev("oa_invite", 3)]
    assert derive_status(events) == "onsite"


def test_rejection_beats_progress():
    events = [ev("applied"), ev("interview_invite", 5), ev("rejected", 9)]
    assert derive_status(events) == "rejected"


def test_rejection_beats_progress_even_if_logged_earlier():
    # 终态盖过推进型状态，与两者的时间先后无关
    events = [ev("rejected", 9), ev("interview_invite", 12)]
    assert derive_status(events) == "rejected"


def test_withdrawn():
    assert derive_status([ev("applied"), ev("withdrawn", 2)]) == "withdrawn"


def test_offer():
    events = [ev("applied"), ev("onsite_completed", 20), ev("offer_received", 25)]
    assert derive_status(events) == "offer"


# --- 人工更正通道 ---------------------------------------------------------

def test_override_wins_over_everything():
    # 邮件分类把面试邀请误判成拒信之后，靠追加一条事件来修，而不是改历史
    events = [
        ev("applied"),
        ev("rejected", 5),
        ev("status_override", 6, status="interview_loop"),
    ]
    assert derive_status(events) == "interview_loop"


def test_latest_override_wins():
    events = [
        ev("applied"),
        ev("status_override", 2, status="rejected"),
        ev("status_override", 4, status="phone_screen"),
    ]
    assert derive_status(events) == "phone_screen"


def test_override_with_invalid_status_is_ignored():
    # 拼错的状态名不能把 status 写成垃圾值
    events = [ev("applied"), ev("oa_invite", 1), ev("status_override", 2, status="typo")]
    assert derive_status(events) == "oa"


# --- ghosted --------------------------------------------------------------

def test_ghosted_after_silence():
    events = [ev("applied")]
    assert derive_status(events, now=T0 + timedelta(days=31)) == "ghosted"


def test_not_ghosted_before_threshold():
    events = [ev("applied")]
    assert derive_status(events, now=T0 + timedelta(days=29)) == "applied"


def test_ghosted_applies_mid_funnel_too():
    # 面试之后被晾着也是 ghosted，不只是投递之后
    events = [ev("applied"), ev("interview_completed", 5)]
    assert derive_status(events, now=T0 + timedelta(days=40)) == "ghosted"


def test_terminal_state_never_becomes_ghosted():
    events = [ev("applied"), ev("rejected", 3)]
    assert derive_status(events, now=T0 + timedelta(days=300)) == "rejected"


def test_offer_never_becomes_ghosted():
    events = [ev("applied"), ev("offer_received", 20)]
    assert derive_status(events, now=T0 + timedelta(days=300)) == "offer"


def test_without_now_ghosting_is_skipped():
    # now=None 时完全不依赖时钟，纯状态推导
    assert derive_status([ev("applied")]) == "applied"


def test_custom_ghost_threshold():
    events = [ev("applied")]
    assert derive_status(events, now=T0 + timedelta(days=15), ghost_after_days=14) == "ghosted"


# --- 事件类型登记 ---------------------------------------------------------

def test_unknown_event_types_are_reported():
    # 加了新事件类型却忘了登记，会被静默忽略——所以要能报出来
    events = [ev("applied"), ev("coffee_chat", 2)]
    assert unknown_event_types(events) == {"coffee_chat"}
    assert derive_status(events) == "applied"


def test_neutral_events_do_not_advance():
    events = [ev("applied"), ev("follow_up_sent", 5), ev("referral_requested", 6)]
    assert derive_status(events) == "applied"
    assert unknown_event_types(events) == set()


@pytest.mark.parametrize(
    "event_type,expected",
    [
        ("oa_invite", "oa"),
        ("phone_screen_invite", "phone_screen"),
        ("interview_invite", "interview_loop"),
        ("onsite_invite", "onsite"),
        ("offer_received", "offer"),
    ],
)
def test_each_stage_reachable(event_type, expected):
    assert derive_status([ev("applied"), ev(event_type, 1)]) == expected
