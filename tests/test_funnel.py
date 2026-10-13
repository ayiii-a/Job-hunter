"""漏斗式 tracking：同一条投递走到了哪（OA、面试第几轮、offer），以及转化率。

面试分很多轮，而且同一轮会来好几封信（邀请、提醒、改期、确认时间）——
轮数按分类器标的 round / followup 去重，算错了可以人工更正。
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from jha import db, tracking
from jha.agent import tools as tools_mod
from jha.mail import pipeline
from jha.mail.imap import parse_message
from jha.status import FUNNEL, ROUNDS_SET, Event, Progress, interview_rounds, progress
from mailfix import FakeReader, eml

T0 = datetime(2026, 9, 1, 10, 0)
#: 端到端测试用相对时间：get_tracking 按当前时间判 ghosted，写死日期过一个月就会挂
APPLIED = (datetime.now() - timedelta(days=10)).replace(microsecond=0)


def ev(type_, day, **payload):
    return Event(type=type_, occurred_at=T0 + timedelta(days=day), id=day, payload=payload)


# ---------------------------------------------------------------------------
# 面试轮数
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("events, rounds", [
    ([], 0),
    ([ev("interview_invite", 1)], 1),
    # 写明了第几轮：按编号去重，提醒信不另算
    ([ev("interview_invite", 1, round=1), ev("interview_invite", 2, round=1, followup=True),
      ev("interview_invite", 9, round=2)], 2),
    # 没写编号的新邀请各算一轮
    ([ev("interview_invite", 1), ev("interview_invite", 9)], 2),
    # 没写编号的跟进信不算新一轮
    ([ev("interview_invite", 1), ev("interview_invite", 3, followup=True)], 1),
    # 先来没编号的邀请，后来的确认信写了 Round 1：还是一轮
    ([ev("interview_invite", 1), ev("interview_invite", 3, round=1, followup=True)], 1),
    # 「Round 3」就意味着至少走到第三轮（前面的信可能投到别的邮箱了）
    ([ev("interview_invite", 1, round=3)], 3),
    # 只剩跟进信、或者只有你手动记的「面完了」：至少一轮
    ([ev("interview_invite", 1, followup=True)], 1),
    ([ev("interview_completed", 1)], 1),
    # 电话面、onsite 都算轮次
    ([ev("phone_screen_invite", 1), ev("onsite_invite", 9)], 2),
    # 胡乱的 round 值不收
    ([ev("interview_invite", 1, round=True), ev("interview_invite", 9, round=99)], 2),
])
def test_interview_rounds(events, rounds):
    assert interview_rounds(events) == rounds


def test_manual_correction_wins_and_does_not_count_as_activity():
    evs = [ev("applied", 0), ev("interview_invite", 1), ev("interview_invite", 2), ev(ROUNDS_SET, 60, rounds=1)]
    assert interview_rounds(evs) == 1
    p = progress(evs, now=T0 + timedelta(days=61), ghost_after_days=30)
    assert p.outcome == "无回音", "你事后更正轮数，不算对方有动静"


# ---------------------------------------------------------------------------
# 走到哪、结果
# ---------------------------------------------------------------------------

def test_rejected_after_oa():
    p = progress([ev("applied", 0), ev("oa_invite", 3), ev("rejected", 10)])
    assert (p.oa, p.rounds, p.offer, p.outcome, p.stage, p.level) == (True, 0, False, "被拒", "OA", 1)


def test_offer_counts_as_having_passed_every_stage():
    p = progress([ev("applied", 0), ev("offer_received", 20)])
    assert p.level == len(FUNNEL) - 1 and p.stage == "offer" and p.outcome == "offer"


def test_status_override_to_interview_shows_at_least_one_round():
    p = progress([ev("applied", 0), ev("status_override", 5, status="interview_loop")])
    assert p.rounds == 1 and p.outcome == "进行中"


# ---------------------------------------------------------------------------
# 转化率
# ---------------------------------------------------------------------------

def row(i, *, oa=False, rounds=0, offer=False, status="applied", confirmed=True, source="greenhouse", resume=""):
    return tracking.TrackingRow(application_id=i, confirmed=confirmed, job_source=source, resume_version=resume,
                                started_at=f"2026-0{8 + i % 2}-15",
                                progress=Progress(status=status, oa=oa, rounds=rounds, offer=offer))


ROWS = [
    row(1), row(2, status="rejected"), row(3, status="ghosted", confirmed=False),
    row(4, oa=True, status="rejected"),
    row(5, oa=True, rounds=1),
    row(6, rounds=2, status="rejected", source="email"),
    row(7, oa=True, rounds=3, offer=True, status="offer", resume="#3"),
]


def test_funnel_counts_how_far_each_application_got():
    st = tracking.funnel(ROWS)[0]
    assert dict(zip(FUNNEL, st.reached)) == {
        "投递": 7, "过简历关": 4, "面试 1 轮": 3, "面试 2 轮": 2, "面试 3 轮+": 1, "offer": 1}
    assert all(a >= b for a, b in zip(st.reached, st.reached[1:])), "漏斗的后一级不会比前一级多"
    assert (st.confirmed, st.oa, st.oa_to_interview) == (6, 3, 2)
    assert st.outcomes == {"进行中": 2, "被拒": 3, "无回音": 1, "offer": 1}
    assert st.rejected_at == {"简历关": 1, "OA 后": 1, "面试后": 1}


def test_funnel_groups():
    by_source = {g.label: g.total for g in tracking.funnel(ROWS, by="source")}
    assert by_source == {"抓取到的岗位": 6, "别处投的（邮件建档）": 1}
    by_resume = {g.label: g.reached[-1] for g in tracking.funnel(ROWS, by="resume")}
    assert by_resume == {"定制简历": 1, "没有定制简历": 0}
    assert [g.label for g in tracking.funnel(ROWS, by="month")] == ["2026-09", "2026-08"], "新的月份在前"


# ---------------------------------------------------------------------------
# 端到端：邮件 → 事件 → 投递表的漏斗列
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    c.execute("""INSERT INTO companies (name, email_domains_json) VALUES ('Raydar', '["raydar.xyz"]')""")
    c.execute("INSERT INTO jobs (company_id, source, external_id, title) VALUES (1, 'ashby', 'r1', 'Full Stack AI Engineer')")
    c.execute("INSERT INTO applications (job_id, applied_at) VALUES (1, ?)", (APPLIED.isoformat(sep=" "),))
    c.commit()
    db.append_event(c, 1, "applied", occurred_at=APPLIED)
    yield c
    c.close()


class Fake:
    model = "claude-haiku-5-5"

    def __init__(self, *answers):
        self.answers = list(answers)

    def structured(self, **kw):
        return {"confidence": 0.95, "summary": "约面试", "company": "Raydar", **self.answers.pop(0)}


def sweep(conn, client, *subjects):
    raws = [parse_message(eml(frm="Raydar <hiring@raydar.xyz>", subject=s, body=s,
                              date=(APPLIED + timedelta(days=i)).astimezone(timezone.utc)), f"77:{i}")
            for i, s in enumerate(subjects, 1)]
    pipeline.ingest(conn, FakeReader(raws))
    return pipeline.process_pending(conn, client=client)


def test_rounds_flow_from_mail_into_the_tracking_table(conn):
    sweep(conn, Fake({"type": "interview_invite", "round": 1, "followup": False},
                     {"type": "interview_invite", "round": 1, "followup": True},
                     {"type": "interview_invite", "round": 2, "followup": False}),
          "Raydar - 1st Round Interview", "Reminder: your 1st round tomorrow", "Raydar - 2nd Round Interview")
    payloads = [json.loads(r["payload_json"]) for r in conn.execute(
        "SELECT payload_json FROM events WHERE type = 'interview_invite' ORDER BY id")]
    assert [(p.get("round"), p.get("followup", False)) for p in payloads] == [(1, False), (1, True), (2, False)]

    r = tracking.tracking_rows(conn)[0]
    assert (r.progress.rounds, r.progress.stage, r.progress.outcome) == (2, "面试第 2 轮", "进行中")
    cells = dict(zip(tracking.COLUMNS, r.as_cells()))
    assert (cells["面试轮数"], cells["OA"], cells["结果"], cells["投递日"]) == ("2", "", "进行中", APPLIED.date().isoformat())


def test_manual_rounds_fix(conn):
    sweep(conn, Fake({"type": "interview_invite"}, {"type": "interview_invite"}), "Interview", "Interview again")
    assert tracking.tracking_rows(conn)[0].progress.rounds == 2
    tracking.set_interview_rounds(conn, 1, 1)
    assert tracking.tracking_rows(conn)[0].progress.rounds == 1
    with pytest.raises(ValueError):
        tracking.set_interview_rounds(conn, 1, 11)


def test_agent_sees_the_funnel_fields(conn):
    sweep(conn, Fake({"type": "oa_invite"}), "Raydar assessment")
    out = json.loads(tools_mod.execute("get_tracking", {}, conn))["rows"][0]
    assert (out["oa"], out["interview_rounds"], out["offer"], out["outcome"]) == (True, 0, False, "进行中")


def test_agent_cannot_change_round_counts():
    assert not [n for n in tools_mod.REGISTRY if "round" in n]
