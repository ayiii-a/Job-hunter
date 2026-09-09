"""数据库骨架的测试。

重点验两件事：
  1. events 表的「永不删改」是数据库强制的，不是口头约定
  2. status 缓存能从 events 完整重建 —— 这是误判之后的兜底手段
"""

import sqlite3
from datetime import datetime, timedelta

import pytest

from jha import db

T0 = datetime(2026, 3, 1, 9, 0, 0)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture
def app_id(conn):
    conn.execute("INSERT INTO companies (name) VALUES ('Acme')")
    conn.execute(
        "INSERT INTO jobs (company_id, source, external_id, title) VALUES (1, 'greenhouse', 'j1', 'SWE')"
    )
    cur = conn.execute(
        "INSERT INTO applications (job_id, applied_at, applied_via) VALUES (1, ?, 'referral')",
        (T0.isoformat(sep=" "),),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_all_tables_created(conn):
    expected = {
        "companies", "contacts", "jobs", "job_analysis", "resume_versions",
        "applications", "events", "emails", "llm_calls", "schema_version",
    }
    assert expected.issubset(set(db.table_names(conn)))


def test_init_db_is_idempotent(conn):
    db.init_db(conn)
    db.init_db(conn)
    assert "events" in db.table_names(conn)


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO jobs (company_id, source, external_id, title) "
            "VALUES (9999, 'greenhouse', 'x', 'T')"
        )


def test_job_uniqueness_per_source(conn):
    conn.execute("INSERT INTO jobs (source, external_id, title) VALUES ('lever', 'a', 'T')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO jobs (source, external_id, title) VALUES ('lever', 'a', 'T2')")


# --- 追加式日志：靠触发器强制 ---------------------------------------------

def test_events_cannot_be_updated(conn, app_id):
    db.append_event(conn, app_id, "applied", occurred_at=T0, now=T0)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE events SET type = 'rejected' WHERE application_id = ?", (app_id,))


def test_events_cannot_be_deleted(conn, app_id):
    db.append_event(conn, app_id, "applied", occurred_at=T0, now=T0)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM events WHERE application_id = ?", (app_id,))


def test_correction_goes_through_a_new_event(conn, app_id):
    # 改不了历史，就只能追加更正事件 —— 这正是设计意图
    db.append_event(conn, app_id, "applied", occurred_at=T0, now=T0)
    db.append_event(
        conn, app_id, "rejected",
        occurred_at=T0 + timedelta(days=3), now=T0 + timedelta(days=3),
    )
    assert _status(conn, app_id) == "rejected"

    db.append_event(
        conn, app_id, "status_override",
        occurred_at=T0 + timedelta(days=4),
        payload={"status": "interview_loop"},
        source="manual",
        now=T0 + timedelta(days=4),
    )
    assert _status(conn, app_id) == "interview_loop"
    # 原始事件仍然在，历史没有被抹掉
    n = conn.execute("SELECT COUNT(*) c FROM events WHERE application_id=?", (app_id,)).fetchone()["c"]
    assert n == 3


# --- status 缓存重建 -------------------------------------------------------

def _status(conn, app_id):
    return conn.execute("SELECT status FROM applications WHERE id=?", (app_id,)).fetchone()["status"]


def test_append_event_updates_cached_status(conn, app_id):
    # now 钉在事件附近：这里测的是缓存有没有跟着事件走，不是 ghosted 规则。
    # 不钉的话这个测试的结果会随着「今天是哪天」而变。
    db.append_event(conn, app_id, "applied", occurred_at=T0, now=T0)
    assert _status(conn, app_id) == "applied"
    db.append_event(
        conn, app_id, "oa_invite",
        occurred_at=T0 + timedelta(days=2), now=T0 + timedelta(days=2),
    )
    assert _status(conn, app_id) == "oa"


def test_rebuild_repairs_a_corrupted_cache(conn, app_id):
    db.append_event(conn, app_id, "applied", occurred_at=T0, now=T0)
    db.append_event(conn, app_id, "interview_invite", occurred_at=T0 + timedelta(days=5), now=T0 + timedelta(days=5))

    # 手工把缓存写坏（模拟 bug 或误操作）
    conn.execute("UPDATE applications SET status='offer' WHERE id=?", (app_id,))
    conn.commit()

    result = db.rebuild_all_statuses(conn, now=T0 + timedelta(days=6))
    assert _status(conn, app_id) == "interview_loop"
    assert (app_id, "offer", "interview_loop") in result["changed"]


def test_rebuild_flags_applications_without_events(conn, app_id):
    result = db.rebuild_all_statuses(conn, now=T0)
    assert result["orphans"] == [app_id]


def test_rebuild_reports_unregistered_event_types(conn, app_id):
    db.append_event(conn, app_id, "applied", occurred_at=T0, now=T0)
    db.append_event(conn, app_id, "coffee_chat", occurred_at=T0 + timedelta(days=1), now=T0 + timedelta(days=1))
    result = db.rebuild_all_statuses(conn, now=T0 + timedelta(days=2))
    assert "coffee_chat" in result["unknown_event_types"]


def test_ghosted_is_derived_not_stored(conn, app_id):
    db.append_event(conn, app_id, "applied", occurred_at=T0, now=T0)
    db.rebuild_all_statuses(conn, now=T0 + timedelta(days=45))
    assert _status(conn, app_id) == "ghosted"
    # 库里没有任何 ghosted 事件，它纯粹是规则推出来的
    types = [r["type"] for r in conn.execute("SELECT type FROM events")]
    assert "ghosted" not in types


def test_json_helpers_roundtrip_non_ascii():
    # JD 里全是非 ASCII，序列化不能把它们转义成 \uXXXX 之后读不回来
    value = ["ソリューションアーキテクト", "Café", "—"]
    assert db.load_json(db.dump_json(value)) == value


def test_load_json_tolerates_garbage():
    assert db.load_json("not json at all") == []
    assert db.load_json(None) == []
