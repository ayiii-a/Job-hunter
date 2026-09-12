"""邮件检测：即时提醒不经过模型，检测停了要响亮地报出来。"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from jha import cli, db, ingest, notify
from jha.agent import tools as tools_mod
from jha.mail import imap as imap_mod
from jha.mail import pipeline
from jha.mail.imap import MailNotConfigured, parse_message
from mailfix import FakeClassifier, FakeReader, eml

INVITE = dict(frm="Priya <priya@ramp.com>", subject="SECRET_SUBJECT interview",
              body="SECRET_BODY we would like to invite you to a technical interview")


def invite_rule(user):
    return {"type": "interview_invite", "confidence": 0.97, "summary": "面试邀请"}


def seed(c):
    c.execute("""INSERT INTO companies (name, email_domains_json) VALUES ('Ramp', '["ramp.com"]')""")
    c.execute("INSERT INTO jobs (company_id, source, external_id, title) VALUES (1,'ashby','r1','Applied AI Engineer')")
    c.execute("INSERT INTO applications (job_id, applied_at) VALUES (1, '2026-09-01 10:00:00')")
    c.commit()


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    seed(c)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# 推送文本
# ---------------------------------------------------------------------------

def test_alert_text_has_no_email_text(conn):
    """推送不经过模型，但它是发出去的东西——和给 agent 的输出守同一条线。"""
    pipeline.ingest(conn, FakeReader([parse_message(eml(**INVITE), "77:1")]))
    rep = pipeline.process_pending(conn, client=FakeClassifier(invite_rule))
    text = pipeline.alert_text(rep.alerts)

    assert "SECRET_SUBJECT" not in text and "SECRET_BODY" not in text
    for part in ("Ramp", "Applied AI Engineer", "interview_invite", "#1"):
        assert part in text


# ---------------------------------------------------------------------------
# 留痕与过期判定
# ---------------------------------------------------------------------------

NOW = datetime(2026, 9, 10, 14, 0)   # UTC


def _row(conn, *, hours_ago, now, ok=1):
    conn.execute("INSERT INTO fetch_runs (source, started_at, ok) VALUES ('imap', ?, ?)",
                 ((now - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S"), ok))
    conn.commit()


def test_sweep_is_recorded_without_polluting_fetcher_alerts(conn):
    pipeline.record_sweep(conn, fetched=3, new=1, rep=pipeline.SweepReport(classified=1))
    pipeline.record_sweep(conn, error="MailError: 登录失败")

    rows = conn.execute("SELECT * FROM fetch_runs WHERE source = 'imap' ORDER BY id").fetchall()
    assert [r["ok"] for r in rows] == [1, 0]
    assert rows[0]["listed_count"] == 3 and rows[0]["new_count"] == 1
    assert rows[0]["company_id"] is None and "登录失败" in rows[1]["error"]
    assert ingest.failing_sources(conn) == []


def test_never_checked_is_stale(conn):
    assert pipeline.mail_health(conn, now=NOW)["stale"] is True


def test_recent_success_is_fresh(conn):
    _row(conn, hours_ago=1, now=NOW)
    h = pipeline.mail_health(conn, now=NOW)
    assert h["stale"] is False and h["last_sweep_ok"] is True


def test_old_success_is_stale(conn):
    _row(conn, hours_ago=7, now=NOW)
    assert pipeline.mail_health(conn, now=NOW)["stale"] is True


def test_failures_do_not_count_as_checks(conn):
    """密码被撤之后每次都「跑了」，但一次都没成功——这正是要报出来的。"""
    _row(conn, hours_ago=8, now=NOW, ok=1)
    _row(conn, hours_ago=1, now=NOW, ok=0)
    h = pipeline.mail_health(conn, now=NOW)
    assert h["stale"] is True and h["last_sweep_ok"] is False


def test_default_clock_matches_sqlite_utc(conn):
    """fetch_runs 用 datetime('now')，是 UTC。拿本地时间比，过期会早报或晚报几个小时。"""
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    _row(conn, hours_ago=1, now=utc_now)
    assert pipeline.mail_health(conn)["stale"] is False
    conn.execute("DELETE FROM fetch_runs")
    _row(conn, hours_ago=7, now=utc_now)
    assert pipeline.mail_health(conn)["stale"] is True


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def test_sweep_tool_records_success(conn, monkeypatch):
    monkeypatch.setattr(imap_mod, "MailReader", lambda: FakeReader([]))
    tools_mod.execute("sweep_emails", {}, conn)
    assert conn.execute("SELECT ok FROM fetch_runs WHERE source = 'imap'").fetchone()["ok"] == 1


def test_sweep_tool_records_failure(conn, monkeypatch):
    def not_configured():
        raise MailNotConfigured("没配 IMAP_USER")

    monkeypatch.setattr(imap_mod, "MailReader", not_configured)
    with pytest.raises(MailNotConfigured):
        tools_mod.execute("sweep_emails", {}, conn)
    row = conn.execute("SELECT ok, error FROM fetch_runs WHERE source = 'imap'").fetchone()
    assert row["ok"] == 0 and "IMAP_USER" in row["error"]


def test_fetch_health_reports_mail(conn):
    out = json.loads(tools_mod.execute("get_fetch_health", {}, conn))
    assert out["mail"]["stale"] is True
    assert all(r["source"] != "imap" for r in out["recent_runs"])


# ---------------------------------------------------------------------------
# CLI：agent mail sweep --push-alerts
# ---------------------------------------------------------------------------

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    dbfile = tmp_path / "t.db"
    monkeypatch.setenv("JHA_DB_PATH", str(dbfile))
    c = db.connect(dbfile)
    db.init_db(c)
    seed(c)
    c.close()

    raw = parse_message(eml(**INVITE), "77:1")
    monkeypatch.setattr(imap_mod, "MailReader", lambda: FakeReader([raw]))
    monkeypatch.setattr(pipeline, "AgentClient", lambda: FakeClassifier(invite_rule))
    sent = []

    def fake_send(text, ok=True):
        sent.append(text)
        return notify.NotifyResult(True, "discord")

    monkeypatch.setattr(notify, "send", fake_send)
    return dbfile, sent


def test_cli_push_alerts_sends_only_db_fields(cli_env):
    dbfile, sent = cli_env
    assert cli.main(["mail", "sweep", "--push-alerts"]) == 0

    assert len(sent) == 1
    assert "SECRET_SUBJECT" not in sent[0] and "SECRET_BODY" not in sent[0]
    assert "Ramp" in sent[0]
    c = db.connect(dbfile)
    assert c.execute("SELECT ok FROM fetch_runs WHERE source = 'imap'").fetchone()["ok"] == 1
    c.close()


def test_cli_push_failure_is_loud(cli_env, monkeypatch):
    """推送失败时退出码非零——定时任务的「没收到面试提醒」不能是沉默的。"""
    monkeypatch.setattr(notify, "send", lambda text: notify.NotifyResult(False, "none", "没配 DISCORD_WEBHOOK_URL"))
    assert cli.main(["mail", "sweep", "--push-alerts"]) == 1


def test_cli_no_fetch_does_not_count_as_a_check(cli_env):
    dbfile, _ = cli_env
    assert cli.main(["mail", "sweep", "--no-fetch"]) == 0
    c = db.connect(dbfile)
    assert c.execute("SELECT COUNT(*) n FROM fetch_runs WHERE source = 'imap'").fetchone()["n"] == 0
    c.close()
