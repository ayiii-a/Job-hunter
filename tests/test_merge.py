"""重复的投递记录、公司和待办：合并、作废、找重复。

实测：先来一封不写岗位的 OA 建了占位记录，后来的确认信写了岗位，表里就多出一条；
同一家公司被建成两条（SS&C Technologies / SS&C Technologies Inc）；待办跟着重复。
"""

import json
from datetime import datetime, timedelta

import pytest

from jha import db, tracking
from jha.agent import tools as tools_mod
from jha.mail import match, pipeline, tasks

NOW = datetime(2026, 9, 26, 9, 0).astimezone()
CHECK = datetime(2026, 9, 26)            # 重算状态的参照时刻，免得过一个月测试就判 ghosted


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    c.execute("INSERT INTO companies (name) VALUES ('Netic')")
    c.execute("INSERT INTO jobs (company_id, source, external_id, title) VALUES (1, 'email', '1:p', ?)",
              (tracking.UNKNOWN_ROLE,))
    c.execute("INSERT INTO jobs (company_id, source, external_id, title) "
              "VALUES (1, 'email', '1:t', 'Full-Stack Software Engineer')")
    c.execute("INSERT INTO applications (job_id, applied_at) VALUES (1, '2026-09-18 15:00:00')")
    c.execute("INSERT INTO applications (job_id, applied_at, confirmation_seen_at) "
              "VALUES (2, '2026-09-19 10:00:00', '2026-09-19 10:05:00')")
    c.commit()
    db.append_event(c, 1, "applied", occurred_at=datetime(2026, 9, 18, 15), now=CHECK)
    db.append_event(c, 1, "oa_invite", occurred_at=datetime(2026, 9, 18, 15, 30), now=CHECK)
    db.append_event(c, 2, "applied", occurred_at=datetime(2026, 9, 19, 10), now=CHECK)
    db.append_event(c, 2, "confirmation_received", occurred_at=datetime(2026, 9, 19, 10, 5), now=CHECK)
    yield c
    c.close()


def oa_email(conn, app, days):
    n = conn.execute("SELECT COUNT(*) c FROM emails").fetchone()["c"] + 1
    task = {"v": tasks.TASK_VERSION, "task": "", "deadlines": [{
        "text": "x", "kind": "deadline", "date_only": False,
        "due_at": (NOW + timedelta(days=days)).isoformat(timespec="minutes")}]}
    conn.execute("INSERT INTO emails (uid, received_at, classification, summary, matched_application_id, task_json) "
                 "VALUES (?, '2026-09-25 10:00:00', 'oa_invite', 'OA', ?, ?)", (f"u{n}", app, json.dumps(task)))
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# 合并投递
# ---------------------------------------------------------------------------

def test_placeholder_is_merged_into_the_titled_record_whichever_order(conn):
    out = tracking.merge_applications(conn, 2, 1, now=CHECK)      # 参数顺序反了，也保留有岗位名的
    assert (out["kept"], out["merged"]) == (2, 1)
    assert out["status"] == "oa", "占位那条上的 OA 邀请要算进来"
    row = conn.execute("SELECT * FROM applications WHERE id = 2").fetchone()
    assert row["applied_at"] == "2026-09-18 15:00:00", "投递日取早的那个"
    assert "合并了 #1" in row["notes"]
    copied = conn.execute("SELECT type, payload_json FROM events WHERE application_id = 2 ORDER BY id").fetchall()
    assert [r["type"] for r in copied] == ["applied", "confirmation_received", "oa_invite"], "applied 不重复抄"
    assert json.loads(copied[-1]["payload_json"])["merged_from_application"] == 1
    assert conn.execute("SELECT COUNT(*) FROM events WHERE application_id = 1").fetchone()[0] == 2, "原来的历史还在"


def test_merged_records_disappear_everywhere(conn):
    tracking.merge_applications(conn, 1, 2, now=CHECK)
    assert [r.application_id for r in tracking.tracking_rows(conn)] == [2]
    assert [a["id"] for a in json.loads(tools_mod.execute("list_applications", {}, conn))] == [2]
    m = match.match(conn, from_addr="x", subject="Netic update", body="", company_id=1)
    assert m.status == "exact" and m.application_id == 2


def test_duplicate_todos_become_one_after_merging(conn):
    oa_email(conn, 1, 5)
    oa_email(conn, 2, 2)
    assert len(tasks.pending(conn, now=NOW)) == 2
    tracking.merge_applications(conn, 1, 2, now=CHECK)
    items = tasks.pending(conn, now=NOW)
    assert len(items) == 1 and items[0].email_ids == [1, 2]
    assert items[0].title == "Full-Stack Software Engineer"


def test_plan_is_read_only(conn):
    before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    p = tracking.plan_merge(conn, 1, 2)
    assert p["keep"]["id"] == 2 and [e["type"] for e in p["events"]] == ["oa_invite"]
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
    assert conn.execute("SELECT merged_into FROM applications WHERE id = 1").fetchone()[0] is None


@pytest.mark.parametrize("a, b, msg", [(1, 1, "同一条"), (1, 99, "没有 id")])
def test_bad_merges_are_refused(conn, a, b, msg):
    with pytest.raises(ValueError, match=msg):
        tracking.plan_merge(conn, a, b)


def test_merging_across_companies_or_twice_is_refused(conn):
    conn.execute("INSERT INTO companies (name) VALUES ('Other')")
    conn.execute("INSERT INTO jobs (company_id, source, external_id, title) VALUES (2, 'email', '2:x', 'X Engineer')")
    conn.execute("INSERT INTO applications (job_id) VALUES (3)")
    conn.commit()
    with pytest.raises(ValueError, match="不同的公司"):
        tracking.plan_merge(conn, 2, 3)
    tracking.merge_applications(conn, 1, 2, now=CHECK)
    with pytest.raises(ValueError, match="已经合并"):
        tracking.plan_merge(conn, 1, 2)


def test_new_email_for_a_merged_record_goes_to_the_kept_one(conn):
    tracking.merge_applications(conn, 1, 2, now=CHECK)
    again = tracking.record_from_email(conn, company_id=1, company_name="Netic", title=tracking.UNKNOWN_ROLE,
                                       occurred_at=datetime(2026, 9, 27), email_id=9, applied_at=None)
    assert again == 2


def test_accept_onto_a_merged_record_goes_to_the_kept_one(conn):
    conn.execute("INSERT INTO emails (uid, classification, review_status) VALUES ('q1', 'oa_invite', 'pending')")
    conn.commit()
    tracking.merge_applications(conn, 1, 2, now=CHECK)
    assert pipeline.accept(conn, 1, application_id=1)["application_id"] == 2


# ---------------------------------------------------------------------------
# 作废
# ---------------------------------------------------------------------------

def test_void_hides_the_record_and_can_be_undone(conn):
    tracking.void_application(conn, 1, reason="人才库注册")
    assert [r.application_id for r in tracking.tracking_rows(conn)] == [2]
    assert tracking.applied_today(conn, now=datetime(2026, 9, 18)) == 0
    oa_email(conn, 1, 3)
    assert tasks.pending(conn, now=NOW) == [], "不是投递，它的待办也不提醒"
    tracking.void_application(conn, 1, undo=True)
    assert len(tracking.tracking_rows(conn)) == 2


def test_a_voided_record_comes_back_when_real_mail_arrives(conn):
    tracking.void_application(conn, 1)
    again = tracking.record_from_email(conn, company_id=1, company_name="Netic", title=tracking.UNKNOWN_ROLE,
                                       occurred_at=datetime(2026, 9, 27), email_id=9, applied_at=None)
    assert again == 1
    assert conn.execute("SELECT voided_at FROM applications WHERE id = 1").fetchone()[0] is None


# ---------------------------------------------------------------------------
# 合并公司
# ---------------------------------------------------------------------------

def test_company_duplicates_are_merged(conn):
    conn.execute("""INSERT INTO companies (name, email_domains_json) VALUES ('Netic AI', '["netic.ai"]')""")
    conn.execute("INSERT INTO jobs (company_id, source, external_id, title) VALUES (2, 'email', '2:x', 'Data Engineer')")
    conn.execute("INSERT INTO contacts (company_id, name, relationship, strength) VALUES (2, 'Wei', '校友', 3)")
    conn.commit()
    out = tracking.merge_companies(conn, 2, 1)
    assert out["moved"]["jobs"] == 1 and out["moved"]["contacts"] == 1
    assert conn.execute("SELECT COUNT(*) FROM companies WHERE id = 2").fetchone()[0] == 0
    assert json.loads(conn.execute("SELECT email_domains_json FROM companies WHERE id = 1").fetchone()[0]) == ["netic.ai"]


def test_a_company_from_companies_yaml_is_never_the_one_deleted(conn):
    conn.execute("INSERT INTO companies (name, ats_type, board_token) VALUES ('Netic Inc', 'ashby', 'netic')")
    conn.commit()
    with pytest.raises(ValueError, match="companies.yaml"):
        tracking.merge_companies(conn, 2, 1)


# ---------------------------------------------------------------------------
# 找重复
# ---------------------------------------------------------------------------

def test_dups_suggests_placeholders_same_titles_companies_and_unmatched_todos(conn):
    # Fidelity 那种：同一个岗位编号，写法略不同
    conn.execute("INSERT INTO jobs (company_id, source, external_id, title) "
                 "VALUES (1, 'email', '1:a', '2133859 January 2027 - Leap Software Engineer')")
    conn.execute("INSERT INTO jobs (company_id, source, external_id, title) "
                 "VALUES (1, 'email', '1:b', '2133859 - January 2027 - Leap Software Engineer')")
    conn.execute("INSERT INTO applications (job_id) VALUES (3)")
    conn.execute("INSERT INTO applications (job_id) VALUES (4)")
    conn.execute("INSERT INTO companies (name) VALUES ('SS&C Technologies')")
    conn.execute("INSERT INTO companies (name) VALUES ('SS&C Technologies Inc')")
    conn.execute("INSERT INTO emails (uid, classification, company_hint, review_status) "
                 "VALUES ('q', 'oa_invite', 'Netic AI', 'pending')")
    conn.commit()
    d = tracking.duplicate_candidates(conn)
    pairs = {(p["dup"]["id"], tuple(k["id"] for k in p["keep"])) for p in d["applications"]}
    assert (1, (2, 3, 4)) in pairs, "占位记录：候选是这家公司所有有岗位名的"
    assert (3, (4,)) in pairs, "岗位名去掉标点后一样"
    assert [sorted(c["name"] for c in g) for g in d["companies"]] == [["SS&C Technologies", "SS&C Technologies Inc"]]
    assert [a["id"] for a in d["emails"][0]["candidates"]] == [1, 2, 3, 4], "Netic AI → Netic 的投递"


def test_the_agent_cannot_merge_or_void():
    """合并撤不回，作废会让记录从各处消失——只能你在命令行里做。"""
    assert not [n for n in tools_mod.REGISTRY if "merge" in n or "void" in n]
