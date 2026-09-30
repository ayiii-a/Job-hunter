"""截止时间提醒：时间逐条核对，按截止时间排；过期的、做完的、投递已经结束的不再提醒。

时间写法取自真实邮件：「Friday, October 2」「27 Oct 2026 01:44 AM CDT」「14 days from receipt」。
"""

import json
from datetime import datetime, timedelta

import pytest

from jha import db
from jha.mail import classify, tasks

RECEIVED = datetime(2026, 9, 25, 10, 0)          # 库里存的收信时间：本地、无时区
NOW = datetime(2026, 9, 26, 9, 0).astimezone()


# ---------------------------------------------------------------------------
# 时间核对
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("original, iso", [
    ("Friday, October 2", "2026-10-02"),
    ("27 Oct 2026 01:44 AM CDT", "2026-10-27T01:44:00-05:00"),
    ("Sep 29, 2026, 1:59 PM PDT", "2026-09-29T13:59:00-07:00"),
    ("14 days from receipt of this email", "2026-10-09T10:00:00"),
    ("by 10/3", "2026-10-03"),
])
def test_real_deadline_wordings_are_accepted(original, iso):
    item = tasks.verify_due(original, iso, f"Please finish it {original}.", RECEIVED, kind="deadline")
    assert item and item["due_at"], item


@pytest.mark.parametrize("original, iso", [
    ("Friday, October 2", "2026-10-03"),            # 月日对不上
    ("Thursday, October 2", "2026-10-02"),          # 2026-10-02 是周五
    ("October 2", "2025-10-02"),                    # 年份写错：离收信时间太远
    ("within 14 days", "not a date"),
    ("within 14 days", ""),
])
def test_wrong_conversions_keep_the_wording_but_no_countdown(original, iso):
    """算错的倒计时比没有倒计时更害人。"""
    item = tasks.verify_due(original, iso, f"Complete it {original}.", RECEIVED)
    assert item["text"] == original and item["due_at"] is None


def test_a_time_that_is_not_in_the_email_is_dropped():
    assert tasks.verify_due("next Monday at 9am", "2026-09-28T09:00:00", "Please complete it soon.", RECEIVED) is None


def test_date_without_a_time_means_end_of_that_day():
    item = tasks.verify_due("October 2", "2026-10-02", "Due October 2.", RECEIVED)
    due = datetime.fromisoformat(item["due_at"])
    assert (due.hour, due.minute) == (23, 59) and item["date_only"]


@pytest.mark.parametrize("delta, text", [
    (timedelta(days=3, hours=5), "还剩 3 天"),
    (timedelta(days=1, hours=4), "还剩 1 天 4 小时"),
    (timedelta(hours=5, minutes=3), "⚠ 还剩 5 小时 3 分"),
    (timedelta(minutes=40), "⚠ 还剩 40 分钟"),
])
def test_time_left(delta, text):
    assert tasks._left(delta) == text


# ---------------------------------------------------------------------------
# 待办列表
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    for name, title in (("Snowflake", "AI Research Scientist"), ("Akuna Capital", "Software Engineer")):
        c.execute("INSERT INTO companies (name) VALUES (?)", (name,))
        cid = c.execute("SELECT id FROM companies WHERE name = ?", (name,)).fetchone()["id"]
        c.execute("INSERT INTO jobs (company_id, source, external_id, title) VALUES (?, 'email', ?, ?)",
                  (cid, f"{cid}:x", title))
        c.execute("INSERT INTO applications (job_id) VALUES (?)", (cid,))
    c.commit()
    yield c
    c.close()


def due(days, kind="deadline"):
    return {"text": "by Friday", "kind": kind, "date_only": False,
            "due_at": (NOW + timedelta(days=days)).isoformat(timespec="minutes")}


def mail(conn, *, app=1, ctype="oa_invite", deadlines=(), task="HackerRank assessment",
         summary="完成 HackerRank 在线测评", received="2026-09-25 10:00:00", extracted=True, body="body"):
    n = conn.execute("SELECT COUNT(*) c FROM emails").fetchone()["c"] + 1
    task_json = json.dumps({"task": task, "deadlines": list(deadlines)}) if extracted else None
    conn.execute(
        "INSERT INTO emails (uid, from_addr, subject, body_text, received_at, classification, summary, "
        "matched_application_id, task_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (f"u{n}", "no-reply@hackerrankforwork.com", "Assessment", body, received, ctype, summary, app, task_json),
    )
    conn.commit()
    return n


def test_sorted_by_deadline_with_time_left(conn):
    mail(conn, app=1, deadlines=[due(5)])
    mail(conn, app=2, deadlines=[due(1)])
    items = tasks.pending(conn, now=NOW)
    assert [t.company for t in items] == ["Akuna Capital", "Snowflake"]
    text = tasks.reminder_text(items, now=NOW)
    assert text.index("Akuna") < text.index("Snowflake")
    for part in ("还剩 1 天", "还剩 5 天", "OA · HackerRank assessment", "完成 HackerRank 在线测评", "原文：by Friday"):
        assert part in text


def test_expired_items_are_not_reminded(conn):
    mail(conn, deadlines=[due(-1)])
    assert tasks.pending(conn, now=NOW) == []


def test_finished_applications_are_not_reminded(conn):
    """投递已经被拒，OA 就不用做了；已经进了面试，OA 也算过去了。"""
    db.append_event(conn, 1, "rejected", occurred_at=datetime(2026, 9, 25))
    db.append_event(conn, 2, "interview_invite", occurred_at=datetime(2026, 9, 25))
    mail(conn, app=1, deadlines=[due(3)])
    mail(conn, app=2, deadlines=[due(3)])
    assert tasks.pending(conn, now=NOW) == []


def test_dismissed_emails_are_not_tasks(conn):
    eid = mail(conn, deadlines=[due(3)])
    conn.execute("UPDATE emails SET review_status = 'dismissed' WHERE id = ?", (eid,))
    conn.commit()
    assert tasks.pending(conn, now=NOW) == []


def test_items_without_a_deadline_stay_for_two_weeks(conn):
    recent = (NOW - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    old = (NOW - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S")
    mail(conn, app=1, ctype="interview_invite", received=recent)
    mail(conn, app=2, ctype="interview_invite", received=old)
    items = tasks.pending(conn, now=NOW)
    assert [t.company for t in items] == ["Snowflake"]
    assert "没写截止时间 · 收到 3 天" in tasks.reminder_text(items, now=NOW)


def test_invite_and_reminder_for_the_same_oa_are_one_item(conn):
    first = mail(conn, deadlines=[due(5)])
    second = mail(conn, deadlines=[due(2)])          # 催促邮件，截止时间更近
    items = tasks.pending(conn, now=NOW)
    assert len(items) == 1 and items[0].email_ids == [first, second]
    assert "还剩 2 天" in tasks.reminder_text(items, now=NOW)


def test_done_marks_the_group_and_records_the_completion(conn):
    first = mail(conn, deadlines=[due(5)])
    mail(conn, deadlines=[due(2)])
    out = tasks.mark_done(conn, first)
    assert out["marked"] == 2 and out["event"] == "oa_completed"
    assert tasks.pending(conn, now=NOW) == []
    types = [r["type"] for r in conn.execute("SELECT type FROM events WHERE application_id = 1")]
    assert types == ["oa_completed"]
    assert tasks.mark_done(conn, first)["marked"] == 0, "标过了就不再重复记事件"


def test_what_gets_pushed_has_no_links_or_addresses(conn):
    mail(conn, deadlines=[due(2)], summary="点这里确认 https://evil.example/confirm 或者回复 hr@evil.example")
    text = tasks.reminder_text(tasks.pending(conn, now=NOW), now=NOW)
    assert "http" not in text and "@" not in text


# ---------------------------------------------------------------------------
# 抽取
# ---------------------------------------------------------------------------

class FakeExtractor:
    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.calls = payload, error, []

    def structured(self, *, schema_name, model, **kw):
        self.calls.append((schema_name, model))
        if self.error:
            raise self.error
        return dict(self.payload)


BODY = "Please complete the HackerRank assessment by Friday, October 2."


def test_extraction_keeps_only_verified_times_and_is_not_repeated(conn):
    eid = mail(conn, extracted=False, body=BODY)
    client = FakeExtractor({"task": "HackerRank assessment", "deadlines": [
        {"original": "by Friday, October 2", "iso": "2026-10-02", "kind": "deadline"},
        {"original": "next Monday at 9am", "iso": "2026-09-28T09:00:00", "kind": "event"},   # 邮件里没有
    ]})
    assert tasks.extract_pending(conn, client=client) == 1
    assert client.calls == [("task_extraction", classify.TASK_MODEL)]
    info = json.loads(conn.execute("SELECT task_json FROM emails WHERE id = ?", (eid,)).fetchone()[0])
    assert info["task"] == "HackerRank assessment"
    assert [d["text"] for d in info["deadlines"]] == ["by Friday, October 2"]
    assert tasks.extract_pending(conn, client=client) == 0, "抽过的不再花钱"


def test_stringified_deadlines_are_parsed(conn):
    mail(conn, extracted=False, body=BODY)
    payload = {"task": "", "deadlines": json.dumps({"deadlines": [
        {"original": "by Friday, October 2", "iso": "2026-10-02", "kind": "deadline"}]})}
    tasks.extract_pending(conn, client=FakeExtractor(payload))
    info = json.loads(conn.execute("SELECT task_json FROM emails").fetchone()[0])
    assert len(info["deadlines"]) == 1


def test_failed_extraction_is_retried_next_time(conn):
    mail(conn, extracted=False, body=BODY)
    assert tasks.extract_pending(conn, client=FakeExtractor(error=RuntimeError("overloaded"))) == 0
    assert conn.execute("SELECT task_json FROM emails").fetchone()[0] is None
