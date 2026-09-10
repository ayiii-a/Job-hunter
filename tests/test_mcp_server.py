"""MCP 服务器：外壳换了，边界不能跟着换。

OpenClaw 的 tools.allow 是一行配置，改松了不会报错。这里断言的是**我们这一侧**
守住的东西：名单由定时任务决定、GATED 永远不给、名单外的调用被拒绝并留痕。
"""

from datetime import datetime, timedelta

import pytest

from jha import config, db, profile, schedules
from jha.agent import Permission, tools as tools_mod
from jha.mcp_server import IDLE_SPLIT, SHELL_MODEL, ScopeError, ToolGate, exposed_tools

DANGEROUS = ("submit", "apply_to", "send", "reply", "open_url", "click", "delete", "accept", "approve")


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    c.execute("INSERT INTO companies (name, ats_type, board_token) VALUES ('Acme','greenhouse','acme')")
    c.execute("INSERT INTO jobs (company_id, source, external_id, title) VALUES (1,'greenhouse','j1','AI Engineer')")
    c.execute("INSERT INTO applications (job_id, applied_at) VALUES (1, '2026-09-01 10:00:00')")
    c.commit()
    yield c
    c.close()


def sched(name="t", **kw):
    return schedules.Schedule(name=name, task="任务", **kw)


def shipped():
    data = profile.load_yaml(config.CONFIG_DIR / "schedules.example.yaml")
    return {n: schedules._parse_one(n, raw or {}) for n, raw in data["schedules"].items()}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 10, 9, 0)

    def __call__(self):
        return self.now


# ---------------------------------------------------------------------------
# 名单
# ---------------------------------------------------------------------------

def test_exposed_tools_are_the_schedule_minus_gated():
    s = sched(tools={"list_email_queue", "sweep_emails", "send_notification"})
    assert exposed_tools(s) == {"list_email_queue", "sweep_emails"}


def test_permission_scoped_schedule_gets_only_that_tier():
    names = exposed_tools(sched(permissions={Permission.READ}))
    assert names and all(tools_mod.REGISTRY[n].permission is Permission.READ for n in names)


def test_unscoped_schedule_is_refused():
    """不收窄就等于把全部写入工具交给一个我们管不着配置的 agent。"""
    with pytest.raises(ScopeError, match="没有收窄"):
        exposed_tools(sched())


def test_schedule_with_only_gated_tools_is_refused():
    with pytest.raises(ScopeError):
        exposed_tools(sched(tools={"send_notification"}))


def test_shipped_schedules_never_hand_gated_or_dangerous_tools_to_the_shell():
    for name, s in shipped().items():
        names = exposed_tools(s)
        assert all(tools_mod.REGISTRY[n].permission is not Permission.GATED for n in names), name
        for bad in DANGEROUS:
            assert not [n for n in names if bad in n], (name, bad)


# ---------------------------------------------------------------------------
# 调用
# ---------------------------------------------------------------------------

def test_call_inside_the_list_executes_and_is_recorded(conn):
    gate = ToolGate(conn, sched(name="phone", tools={"list_companies"}))
    text, is_error = gate.call("list_companies", {})
    assert not is_error and "Acme" in text

    run = conn.execute("SELECT * FROM agent_runs").fetchone()
    assert run["schedule_name"] == "phone" and run["model"] == SHELL_MODEL
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM agent_steps ORDER BY seq")]
    assert kinds == ["tool_use", "tool_result"]


def test_call_outside_the_list_is_refused_and_leaves_a_trace(conn):
    """外壳配置被改松、或者模型被注入说服去调名单外的工具——拒绝，并且留痕。"""
    gate = ToolGate(conn, sched(tools={"list_companies"}))
    text, is_error = gate.call("append_event", {"application_id": 1, "type": "offer_received"})

    assert is_error and "拒绝" in text
    assert conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 0
    err = conn.execute("SELECT * FROM agent_steps WHERE kind = 'error'").fetchone()
    assert err["tool_name"] == "append_event"


def test_gated_tool_is_refused_even_if_the_schedule_lists_it(conn, monkeypatch):
    sent = []
    monkeypatch.setattr(tools_mod.notify, "send", lambda text: sent.append(text))
    gate = ToolGate(conn, sched(tools={"list_companies", "send_notification"}))

    _, is_error = gate.call("send_notification", {"text": "hi"})
    assert is_error and sent == []


def test_tool_errors_are_returned_not_raised(conn):
    gate = ToolGate(conn, sched(tools={"list_companies"}))
    text, is_error = gate.call("list_companies", {"no_such_arg": 1})
    assert is_error and "TypeError" in text


# ---------------------------------------------------------------------------
# 轨迹按空闲间隔切分
# ---------------------------------------------------------------------------

def test_idle_gap_starts_a_new_run(conn):
    """外壳不告诉我们会话何时结束，按空闲间隔切分。"""
    clock = Clock()
    gate = ToolGate(conn, sched(tools={"list_companies"}), clock=clock)
    gate.call("list_companies", {})
    clock.now += IDLE_SPLIT / 2
    gate.call("list_companies", {})
    clock.now += IDLE_SPLIT + timedelta(minutes=1)
    gate.call("list_companies", {})

    runs = conn.execute("SELECT * FROM agent_runs ORDER BY id").fetchall()
    assert len(runs) == 2
    assert runs[0]["finished_at"] is not None and runs[0]["tool_calls"] == 2
    assert runs[1]["finished_at"] is None


def test_close_finishes_the_open_run(conn):
    gate = ToolGate(conn, sched(tools={"list_companies"}))
    gate.call("list_companies", {})
    gate.close()
    run = conn.execute("SELECT * FROM agent_runs").fetchone()
    assert run["finished_at"] is not None and run["ok"] == 1
    gate.close()   # 重复关闭不出错


def test_nothing_is_recorded_before_the_first_call(conn):
    ToolGate(conn, sched(tools={"list_companies"})).close()
    assert conn.execute("SELECT COUNT(*) c FROM agent_runs").fetchone()["c"] == 0


# ---------------------------------------------------------------------------
# 真实协议
# ---------------------------------------------------------------------------

def test_round_trip_through_mcp(conn):
    """用 SDK 的进程内客户端走一遍：列出来的就是名单，名单外的调用被拒。"""
    pytest.importorskip("mcp")
    import anyio
    from mcp import Client

    from jha.mcp_server import build_server

    gate = ToolGate(conn, sched(tools={"list_companies", "list_email_queue", "send_notification"}))

    async def go():
        async with Client(build_server(gate)) as client:
            listed = await client.list_tools()
            ok = await client.call_tool("list_companies", {})
            refused = await client.call_tool("append_event", {"application_id": 1, "type": "offer_received"})
            return listed, ok, refused

    listed, ok, refused = anyio.run(go)
    assert {t.name for t in listed.tools} == {"list_companies", "list_email_queue"}
    assert not ok.is_error and "Acme" in ok.content[0].text
    assert refused.is_error
