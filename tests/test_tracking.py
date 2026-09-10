"""Phase 4：投递追踪的测试。

三块，**全部是确定性规则**（§0「能确定性判定的不交给模型」）：
确认邮件告警、下一步建议、每日上限。这些完全由状态和时间算得出来，
让模型来做既贵又不可复现。
"""

import json
from datetime import datetime, timedelta

import pytest

from jha import config, db, tracking
from jha.agent import tools as tools_mod

T0 = datetime(2026, 9, 1, 9, 0, 0)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    c.execute("INSERT INTO companies (name) VALUES ('Ramp')")
    c.execute("INSERT INTO jobs (company_id, source, external_id, title, url) "
              "VALUES (1,'ashby','a1','Applied AI Engineer','https://x/1')")
    c.execute("INSERT INTO jobs (company_id, source, external_id, title) "
              "VALUES (1,'ashby','a2','ML Engineer')")
    c.execute("INSERT INTO contacts (company_id, name, relationship, strength) "
              "VALUES (1,'Wei','校友',3)")
    c.commit()
    yield c
    c.close()


def apply_at(conn, job_id, when, *, via="ats_direct", contact=None, status="applied"):
    cur = conn.execute(
        "INSERT INTO applications (job_id, applied_at, applied_via, "
        "referred_by_contact_id, status) VALUES (?,?,?,?,?)",
        (job_id, when.isoformat(sep=" ", timespec="seconds"), via, contact, status),
    )
    conn.commit()
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# 确认邮件告警
# ---------------------------------------------------------------------------

def test_alerts_after_24h_without_confirmation(conn):
    """确认邮件是「申请真的进系统了」的唯一地面真相。

    风险表里「申请被 ATS 静默丢弃」原本只有预防没有检测——这条补上检测。
    """
    apply_at(conn, 1, T0)
    assert tracking.missing_confirmations(conn, now=T0 + timedelta(hours=23)) == []
    alerts = tracking.missing_confirmations(conn, now=T0 + timedelta(hours=25))
    assert len(alerts) == 1
    assert alerts[0]["hours_since"] == 25
    assert alerts[0]["url"] == "https://x/1", "告警要带链接，方便你直接去查"


def test_confirmed_applications_do_not_alert(conn):
    app_id = apply_at(conn, 1, T0)
    tracking.mark_confirmed(conn, app_id, when=T0 + timedelta(hours=2))
    assert tracking.missing_confirmations(conn, now=T0 + timedelta(days=3)) == []


def test_confirmation_writes_an_event_too(conn):
    # status 由 events 推导，所以确认也要落成事件，不能只改缓存字段
    app_id = apply_at(conn, 1, T0)
    tracking.mark_confirmed(conn, app_id, when=T0 + timedelta(hours=2))
    types = [r["type"] for r in conn.execute("SELECT type FROM events")]
    assert "confirmation_received" in types


def test_advanced_applications_stop_alerting(conn):
    """已经进面试了，确认邮件早就无关紧要——别再吵。"""
    apply_at(conn, 1, T0, status="interview_loop")
    assert tracking.missing_confirmations(conn, now=T0 + timedelta(days=5)) == []


def test_mark_confirmed_rejects_unknown_id(conn):
    with pytest.raises(ValueError, match="没有 id"):
        tracking.mark_confirmed(conn, 999)


# ---------------------------------------------------------------------------
# 下一步：纯规则
# ---------------------------------------------------------------------------

def step(**kw):
    base = dict(status="applied", applied_at=T0, last_event_at=None,
                confirmation_seen=True, now=T0 + timedelta(hours=1))
    return tracking.next_step(**{**base, **kw})


def test_missing_confirmation_outranks_everything_else():
    """一条根本没进 ATS 的申请，追它的进度没有意义——所以这条先报。"""
    out = step(confirmation_seen=False, now=T0 + timedelta(days=20))
    assert "确认邮件" in out


def test_follow_up_after_two_weeks():
    out = step(now=T0 + timedelta(days=15))
    assert "15 天没动静" in out


def test_no_nagging_before_two_weeks():
    assert step(now=T0 + timedelta(days=10)) == ""


def test_terminal_states_produce_no_next_step():
    for s in ("rejected", "withdrawn"):
        assert step(status=s, now=T0 + timedelta(days=90)) == ""


def test_interview_states_point_at_prep():
    for s in ("phone_screen", "interview_loop", "onsite"):
        assert "面试" in step(status=s)


def test_offer_points_at_the_deadline():
    assert "offer" in step(status="offer").lower()


def test_last_event_resets_the_follow_up_clock():
    # 最近有动静就不该催
    out = step(last_event_at=T0 + timedelta(days=18), now=T0 + timedelta(days=20))
    assert out == ""


# ---------------------------------------------------------------------------
# 每日上限
# ---------------------------------------------------------------------------

def test_daily_limit_counts_only_today(conn):
    apply_at(conn, 1, T0 - timedelta(days=1))
    apply_at(conn, 2, T0)
    assert tracking.applied_today(conn, now=T0) == 1


def test_agent_cannot_exceed_the_daily_limit(conn, monkeypatch):
    """上限的用途不是省力，是逼你投得准。

    让 agent 自己决定要不要超，等于这条限制不存在。
    人可以用 CLI 的 --force 突破，agent 不行。
    """
    monkeypatch.setattr(config, "daily_apply_limit", lambda: 1)
    tools_mod.execute("record_application", {"job_id": 1, "applied_via": "other"}, conn)
    with pytest.raises(ValueError, match="上限"):
        tools_mod.execute("record_application", {"job_id": 2, "applied_via": "other"}, conn)


def test_limit_error_points_at_the_human_override(conn, monkeypatch):
    monkeypatch.setattr(config, "daily_apply_limit", lambda: 0)
    with pytest.raises(ValueError, match="--force"):
        tools_mod.execute("record_application", {"job_id": 1, "applied_via": "other"}, conn)


# ---------------------------------------------------------------------------
# 视图与导出
# ---------------------------------------------------------------------------

def test_tracking_row_carries_the_referral(conn):
    apply_at(conn, 1, T0, via="referral", contact=1)
    row = tracking.tracking_rows(conn, now=T0)[0]
    assert row.referral == "Wei"
    assert row.applied_via == "referral"


def test_unapproved_resume_is_flagged_in_the_view(conn):
    conn.execute("INSERT INTO resume_versions (generated_for_job_id) VALUES (1)")
    app_id = apply_at(conn, 1, T0)
    conn.execute("UPDATE applications SET resume_version_id = 1 WHERE id = ?", (app_id,))
    conn.commit()
    assert "未审核" in tracking.tracking_rows(conn, now=T0)[0].resume_version


def test_export_is_tab_separated_by_default(conn, tmp_path):
    apply_at(conn, 1, T0, via="referral", contact=1)
    path = tracking.export_file(conn, tmp_path / "t.tsv", now=T0)
    body = config.read_text(path)
    assert body.splitlines()[0].split("\t")[0] == "公司"
    assert "\t" in body.splitlines()[1]


def test_export_handles_commas_in_titles(conn, tmp_path):
    """岗位标题里带逗号是常态（"Software Engineer, Frontend"）。

    默认用制表符就是为了这个——CSV 会被它搞乱，而且贴进 Sheet 还要走导入向导。
    """
    conn.execute("UPDATE jobs SET title = 'Engineer, Frontend, Growth' WHERE id = 1")
    conn.commit()
    apply_at(conn, 1, T0)
    body = config.read_text(tracking.export_file(conn, tmp_path / "t.tsv", now=T0))
    assert "Engineer, Frontend, Growth" in body
    assert len(body.splitlines()[1].split("\t")) == len(tracking.COLUMNS)


def test_sheet_sync_reports_missing_config_instead_of_crashing(conn, monkeypatch):
    monkeypatch.setattr(config, "env", lambda k, d=None: None)
    out = tracking.sync_to_sheet(conn)
    assert out["synced"] is False
    assert "agent export" in out["reason"]


def test_tracking_tool_exposes_next_step(conn):
    apply_at(conn, 1, T0 - timedelta(days=30))
    out = json.loads(tools_mod.execute("get_tracking", {}, conn))
    assert out["count"] == 1
    assert out["rows"][0]["next_step"]
    assert "daily_limit" in out
