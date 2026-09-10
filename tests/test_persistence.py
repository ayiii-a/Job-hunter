"""agent 执行轨迹持久化的测试。

无人值守每天自动跑、还允许写库，却查不到它到底干了什么——这个组合不能上线。
所以这里守的是：**跑完必定留痕**，而且**留痕失败不能拖垮 run**。
"""

import json

import pytest

from jha import db
from jha.agent import approval_counts, cost_by_schedule, loop, recent_runs, run_steps
from jha.agent.client import Budget
from jha.agent.persistence import RunRecorder

from test_agent import FakeClient, text_block, tool_block


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    c.execute("INSERT INTO companies (name) VALUES ('Acme')")
    c.execute("INSERT INTO jobs (company_id, source, external_id, title) "
              "VALUES (1,'greenhouse','j1','AI Engineer')")
    c.commit()
    yield c
    c.close()


# ---------------------------------------------------------------------------
# 跑完必定留痕
# ---------------------------------------------------------------------------

def test_run_is_recorded_by_default(conn):
    client = FakeClient([[tool_block("list_jobs", {})], [text_block("找到 1 个")]])
    res = loop.run("看岗位", conn, client=client)
    assert res.run_id is not None

    row = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (res.run_id,)).fetchone()
    assert row["task"] == "看岗位"
    assert row["finished_at"] and row["ok"] == 1
    assert row["tool_calls"] == 1
    assert row["llm_calls"] == 2
    assert row["final_text"] == "找到 1 个"


def test_steps_are_recorded_in_order(conn):
    client = FakeClient([
        [tool_block("list_jobs", {}, "a"), tool_block("list_contacts", {}, "b")],
        [text_block("done")],
    ])
    res = loop.run("汇总", conn, client=client)
    steps = run_steps(conn, res.run_id)
    assert [s["seq"] for s in steps] == list(range(1, len(steps) + 1))
    used = [s["tool_name"] for s in steps if s["kind"] == "tool_use"]
    assert used == ["list_jobs", "list_contacts"]


def test_tool_args_are_recorded_for_forensics(conn):
    # 复盘「那条状态为什么被改了」全靠这个
    client = FakeClient([[tool_block("shortlist_job", {"job_id": 1})], [text_block("ok")]])
    res = loop.run("标记", conn, client=client)
    steps = run_steps(conn, res.run_id)
    args = [s["args_json"] for s in steps if s["kind"] == "tool_use"][0]
    assert "job_id" in args and "1" in args


def test_denied_and_error_steps_are_recorded(conn):
    client = FakeClient([
        [tool_block("send_notification", {"text": "x"}, "a")],
        [tool_block("nonexistent_tool", {}, "b")],
        [text_block("done")],
    ])
    res = loop.run("混合", conn, client=client)
    kinds = {s["kind"] for s in run_steps(conn, res.run_id)}
    assert "denied" in kinds and "error" in kinds


def test_stopped_because_is_recorded(conn):
    client = FakeClient([[tool_block("list_jobs", {}, f"t{i}")] for i in range(9)])
    res = loop.run("循环", conn, client=client, budget=Budget(max_llm_calls=2), max_turns=9)
    row = conn.execute("SELECT * FROM agent_runs WHERE id=?", (res.run_id,)).fetchone()
    assert row["ok"] == 0
    assert "预算" in row["stopped_because"]


def test_pending_approvals_are_persisted(conn):
    client = FakeClient([[tool_block("send_notification", {"text": "摘要"})], [text_block("ok")]])
    res = loop.run("推送", conn, client=client)
    row = conn.execute("SELECT * FROM agent_runs WHERE id=?", (res.run_id,)).fetchone()
    saved = json.loads(row["pending_approvals_json"])
    assert saved[0]["tool"] == "send_notification"


def test_record_false_writes_nothing(conn):
    client = FakeClient([[text_block("ok")]])
    res = loop.run("x", conn, client=client, record=False)
    assert res.run_id is None
    assert conn.execute("SELECT COUNT(*) c FROM agent_runs").fetchone()["c"] == 0


# ---------------------------------------------------------------------------
# 留痕失败不能拖垮 run
# ---------------------------------------------------------------------------

def test_broken_logging_does_not_break_the_run(conn):
    """日志挂了顶多少一条记录，agent 已经做完的事是真的做了。"""
    conn.execute("DROP TABLE agent_steps")
    conn.commit()
    client = FakeClient([[tool_block("shortlist_job", {"job_id": 1})], [text_block("ok")]])
    res = loop.run("标记", conn, client=client)
    assert res.final_text == "ok"
    # 副作用照样生效
    assert conn.execute("SELECT is_shortlisted FROM jobs WHERE id=1").fetchone()["is_shortlisted"] == 1


def test_recorder_disables_itself_after_failure(conn):
    r = RunRecorder(conn)
    r.start("t")
    conn.execute("DROP TABLE agent_steps")
    conn.commit()
    r.step(type("S", (), {"kind": "text", "name": "", "detail": "x"})())
    assert r.enabled is False


# ---------------------------------------------------------------------------
# 按任务拆账 / 权限毕业计数
# ---------------------------------------------------------------------------

def test_cost_is_attributed_to_the_schedule(conn):
    # purpose 只说得出花在哪类调用上，说不出是哪个定时任务触发的
    for name in ("daily-jobs", "daily-jobs", "weekly-review"):
        loop.run("t", conn, client=FakeClient([[text_block("ok")]]), schedule_name=name)
    loop.run("手动跑的", conn, client=FakeClient([[text_block("ok")]]))

    rows = {r["name"]: r for r in cost_by_schedule(conn)}
    assert rows["daily-jobs"]["runs"] == 2
    assert rows["weekly-review"]["runs"] == 1
    assert rows["(手动)"]["runs"] == 1


def test_approval_counts_back_the_permission_graduation_rule(conn):
    """§0「权限毕业」：批准约 20 次没出事就可以降到 WRITE。

    有了这张表，这个次数是数出来的，不是凭感觉。
    """
    for _ in range(3):
        loop.run("推送", conn, client=FakeClient([
            [tool_block("send_notification", {"text": "x"})], [text_block("ok")],
        ]))
    for _ in range(2):
        loop.run("推送", conn, client=FakeClient([
            [tool_block("send_notification", {"text": "x"})], [text_block("ok")],
        ]), approve=lambda n, a: True)

    counts = {c["tool_name"]: c for c in approval_counts(conn)}
    assert counts["send_notification"]["denied"] == 3
    assert counts["send_notification"]["executed"] == 2


def test_recent_runs_can_filter_by_schedule(conn):
    loop.run("a", conn, client=FakeClient([[text_block("ok")]]), schedule_name="daily-jobs")
    loop.run("b", conn, client=FakeClient([[text_block("ok")]]), schedule_name="weekly-review")
    assert len(recent_runs(conn, schedule="daily-jobs")) == 1
    assert len(recent_runs(conn)) == 2


def test_long_output_is_truncated_not_stored_whole(conn):
    # 轨迹表不该变成 JD 的第二个副本
    huge = "X" * 50000
    client = FakeClient([[text_block(huge)]])
    res = loop.run("t", conn, client=client)
    row = conn.execute("SELECT final_text FROM agent_runs WHERE id=?", (res.run_id,)).fetchone()
    assert len(row["final_text"]) <= 2000
