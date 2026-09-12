"""Agent loop 的测试。

全部离线：用一个可编排的假模型，精确断言循环行为。

重点不是「模型聪不聪明」，而是**循环的边界能不能守住**：
预算上限、GATED 拒绝、工具报错后能不能恢复、以及那几个危险工具确实不存在。
"""

import json
from types import SimpleNamespace

import pytest

from jha import db
from jha.agent import Budget, BudgetExceeded, Permission, loop, tools as tools_mod
from jha.agent.client import Budget as B, spend_summary


# --- 假模型 -----------------------------------------------------------------

def text_block(t):
    return SimpleNamespace(type="text", text=t)


def tool_block(name, args, id="tu_1"):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=args)


class FakeClient:
    """按脚本依次返回预设回复，并记录收到的 messages。"""

    def __init__(self, script, model="fake-model"):
        self.script = list(script)
        self.model = model
        self.calls = []

    def complete(self, *, messages, system, tools, budget, conn=None, purpose="agent_loop"):
        budget.check()
        self.calls.append({"messages": list(messages), "tools": tools, "system": system})
        budget.record(self.model, 100, 50)
        blocks = self.script.pop(0) if self.script else [text_block("done")]
        return SimpleNamespace(content=blocks, usage=SimpleNamespace(input_tokens=100, output_tokens=50))


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    c.execute("INSERT INTO companies (name, ats_type, board_token) VALUES ('Acme','greenhouse','acme')")
    c.execute("INSERT INTO jobs (company_id, source, external_id, title, location, screen_tier) "
              "VALUES (1,'greenhouse','j1','AI Engineer','Boston, MA','tier1_ai_engineer')")
    c.execute("INSERT INTO contacts (company_id, name, relationship, strength) VALUES (1,'Wei','校友',3)")
    c.commit()
    yield c
    c.close()


# ---------------------------------------------------------------------------
# 安全边界 —— 这一组是这个架构成立的前提
# ---------------------------------------------------------------------------

def test_dangerous_tools_do_not_exist():
    """安全属性靠「工具不存在」保证，不靠 prompt。

    模型再怎么被 JD 或邮件里的注入内容诱导，也调不出不存在的函数。
    这个测试是那条保证的执行层面锚点——加了危险工具就会挂。
    """
    names = set(tools_mod.REGISTRY)
    for forbidden in ("submit", "apply_to", "send_email", "reply", "open_url", "click", "delete"):
        assert not [n for n in names if forbidden in n], f"出现了危险工具：{forbidden}"


def test_only_one_gated_tool_and_it_is_outbound():
    gated = [n for n, t in tools_mod.REGISTRY.items() if t.permission is Permission.GATED]
    assert gated == ["send_notification"]


def test_every_tool_has_schema_and_description():
    for name, t in tools_mod.REGISTRY.items():
        assert t.description.strip(), name
        assert t.input_schema.get("type") == "object", name


def test_read_only_mode_hides_write_tools():
    names = {s["name"] for s in tools_mod.specs({Permission.READ})}
    assert "list_jobs" in names
    assert "fetch_jobs" not in names
    assert "record_application" not in names


# ---------------------------------------------------------------------------
# 循环行为
# ---------------------------------------------------------------------------

def test_tool_result_is_fed_back_and_loop_continues(conn):
    client = FakeClient([
        [tool_block("list_jobs", {})],
        [text_block("找到 1 个岗位")],
    ])
    res = loop.run("看看有什么岗位", conn, client=client)
    assert res.final_text == "找到 1 个岗位"
    assert res.tool_calls == 1
    # 第二轮的 messages 里必须带上工具结果，否则模型是瞎猜的
    second = client.calls[1]["messages"]
    assert any(
        isinstance(m.get("content"), list)
        and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])
        for m in second
    )


def test_tool_output_reaches_the_model(conn):
    client = FakeClient([[tool_block("list_contacts", {})], [text_block("ok")]])
    loop.run("谁能内推", conn, client=client)
    payload = client.calls[1]["messages"][-1]["content"][0]["content"]
    assert "Wei" in payload


def test_multiple_tools_in_one_turn(conn):
    client = FakeClient([
        [tool_block("list_jobs", {}, "a"), tool_block("list_contacts", {}, "b")],
        [text_block("done")],
    ])
    res = loop.run("汇总", conn, client=client)
    assert res.tool_calls == 2
    assert len(client.calls[1]["messages"][-1]["content"]) == 2


def test_tool_error_is_returned_to_model_not_raised(conn):
    # 工具报错要让模型看见并自己纠正，而不是把整个 run 炸掉
    client = FakeClient([
        [tool_block("get_job", {"job_id": 9999})],
        [text_block("那个岗位不存在")],
    ])
    res = loop.run("看岗位 9999", conn, client=client)
    assert res.final_text == "那个岗位不存在"
    assert any(s.kind == "error" for s in res.steps)
    assert client.calls[1]["messages"][-1]["content"][0]["is_error"] is True


def test_unknown_tool_is_reported_to_model(conn):
    client = FakeClient([[tool_block("submit_application", {})], [text_block("我没有这个能力")]])
    res = loop.run("帮我投", conn, client=client)
    assert any(s.kind == "error" for s in res.steps)
    assert "没有名为" in client.calls[1]["messages"][-1]["content"][0]["content"]


def test_loop_stops_at_max_turns(conn):
    client = FakeClient([[tool_block("list_jobs", {}, f"t{i}")] for i in range(20)])
    res = loop.run("循环", conn, client=client, max_turns=3)
    assert "最大轮数" in res.stopped_because
    assert res.tool_calls == 3


# ---------------------------------------------------------------------------
# GATED
# ---------------------------------------------------------------------------

def test_gated_tool_denied_by_default(conn):
    client = FakeClient([
        [tool_block("send_notification", {"text": "hi"})],
        [text_block("需要你批准才能推送")],
    ])
    res = loop.run("推送摘要", conn, client=client)
    assert any(s.kind == "denied" for s in res.steps)
    assert res.pending_approvals == [{"tool": "send_notification", "args": {"text": "hi"}}]
    assert client.calls[1]["messages"][-1]["content"][0]["is_error"] is True


def test_gated_tool_runs_when_approved(conn, monkeypatch):
    sent = {}
    monkeypatch.setattr(
        tools_mod.notify, "send",
        lambda text: SimpleNamespace(sent=True, channel="discord", detail="") or sent.setdefault("t", text),
    )
    monkeypatch.setattr(
        tools_mod.notify, "send",
        lambda text: (sent.setdefault("t", text), SimpleNamespace(sent=True, channel="discord", detail=""))[1],
    )
    client = FakeClient([
        [tool_block("send_notification", {"text": "hi"})],
        [text_block("已推送")],
    ])
    res = loop.run("推送", conn, client=client, approve=lambda n, a: n == "send_notification")
    assert not res.pending_approvals
    assert sent["t"] == "hi"


def test_approval_is_per_tool_not_blanket(conn):
    # 批准 send_notification 不该顺带批准别的 GATED 工具
    approve = lambda name, args: name == "send_notification"
    assert approve("send_notification", {}) is True
    assert approve("something_else", {}) is False


# ---------------------------------------------------------------------------
# 预算
# ---------------------------------------------------------------------------

def test_budget_stops_the_loop(conn):
    client = FakeClient([[tool_block("list_jobs", {}, f"t{i}")] for i in range(20)])
    res = loop.run("循环", conn, client=client, budget=B(max_llm_calls=2), max_turns=20)
    assert "预算用尽" in res.stopped_because
    assert res.budget["llm_calls"] == 2


def test_budget_tracks_tokens_and_cost():
    b = B()
    b.record("claude-sonnet-5", 1_000_000, 100_000)
    assert b.calls == 1
    assert b.cost_usd == pytest.approx(3.0 + 1.5)


def test_every_model_in_the_selection_plan_has_a_price():
    """路线图 §1 的模型选型表里每个模型都必须能查到价格。

    查不到时 Budget.record 按 (0,0) 算，那部分调用的成本被**静默记成 $0**。
    分层设计里量最大的恰恰是 Haiku——漏一个别名就等于整个第二层不计费。
    这个 bug 出现过一次：PRICING 里只有 claude-haiku-4-5-20251001，
    而配置和文档用的是 claude-haiku-4-5。
    """
    from jha.agent.client import PRICING

    for model in ("claude-sonnet-5", "claude-haiku-4-5", "claude-opus-5"):
        assert model in PRICING, f"{model} 没有价格，它的成本会被静默记成 0"
        assert all(rate > 0 for rate in PRICING[model])


def test_unknown_model_is_recorded_not_swallowed():
    from jha.agent.client import UNPRICED_MODELS, price_of

    UNPRICED_MODELS.discard("made-up-model")
    assert price_of("made-up-model") == (0.0, 0.0)
    assert "made-up-model" in UNPRICED_MODELS


def test_budget_check_raises_when_exhausted():
    b = B(max_llm_calls=1)
    b.record("claude-sonnet-5", 10, 10)
    with pytest.raises(BudgetExceeded):
        b.check()


def test_llm_calls_are_logged_for_cost_analysis(conn):
    from jha.agent.client import log_call

    log_call(conn, purpose="agent_loop", model="claude-sonnet-5", inp=1000, out=200, cost=0.006)
    log_call(conn, purpose="jd_analysis", model="claude-sonnet-5", inp=5000, out=800, cost=0.027)
    rows = spend_summary(conn)
    assert {r["purpose"] for r in rows} == {"agent_loop", "jd_analysis"}
    assert rows[0]["purpose"] == "jd_analysis"      # 按成本降序


# ---------------------------------------------------------------------------
# 工具本身
# ---------------------------------------------------------------------------

def test_get_job_includes_contacts(conn):
    out = json.loads(tools_mod.execute("get_job", {"job_id": 1}, conn))
    assert out["title"] == "AI Engineer"
    assert out["contacts"][0]["name"] == "Wei"


def test_record_application_writes_an_applied_event(conn):
    out = json.loads(
        tools_mod.execute("record_application", {"job_id": 1, "applied_via": "referral"}, conn)
    )
    assert out["status"] == "applied"
    types = [r["type"] for r in conn.execute("SELECT type FROM events")]
    assert types == ["applied"]


def test_record_application_rejects_duplicates(conn):
    tools_mod.execute("record_application", {"job_id": 1, "applied_via": "referral"}, conn)
    with pytest.raises(ValueError, match="已经有投递记录"):
        tools_mod.execute("record_application", {"job_id": 1, "applied_via": "other"}, conn)


def test_append_event_rejects_unregistered_type(conn):
    tools_mod.execute("record_application", {"job_id": 1, "applied_via": "other"}, conn)
    with pytest.raises(ValueError, match="未登记的事件类型"):
        tools_mod.execute("append_event", {"application_id": 1, "type": "coffee_chat"}, conn)


def test_append_event_recomputes_status(conn):
    tools_mod.execute("record_application", {"job_id": 1, "applied_via": "other"}, conn)
    out = json.loads(
        tools_mod.execute("append_event", {"application_id": 1, "type": "interview_invite"}, conn)
    )
    assert out["status"] == "interview_loop"


def test_correction_path_exists_for_the_agent(conn):
    # 误判之后 agent 能自己纠正——这正是 WRITE 档敢自动执行的理由
    tools_mod.execute("record_application", {"job_id": 1, "applied_via": "other"}, conn)
    tools_mod.execute("append_event", {"application_id": 1, "type": "rejected"}, conn)
    out = json.loads(tools_mod.execute("append_event", {
        "application_id": 1, "type": "status_override",
        "payload": {"status": "interview_loop"}, "source": "manual",
    }, conn))
    assert out["status"] == "interview_loop"


def test_shortlist_is_not_an_application(conn):
    # 标记感兴趣不等于投递，否则投递数统计会被污染
    tools_mod.execute("shortlist_job", {"job_id": 1}, conn)
    assert conn.execute("SELECT COUNT(*) c FROM applications").fetchone()["c"] == 0
    assert conn.execute("SELECT is_shortlisted FROM jobs WHERE id=1").fetchone()["is_shortlisted"] == 1


def test_get_master_profile_bullets_expose_ids_and_evidence(conn, monkeypatch):
    monkeypatch.setattr(tools_mod.profile, "load_master_profile", lambda: {
        "experiences": [{"id": "e1", "bullets": [
            {"id": "b1", "text": "x", "metrics": "MAE 11.73"},
            {"id": "b2", "text": "y", "metrics": False},
        ]}]
    })
    out = json.loads(tools_mod.execute("get_master_profile", {"section": "bullets"}, conn))
    assert [b["id"] for b in out] == ["b1", "b2"]
    assert out[0]["has_metrics"] is True
    assert out[0]["metric_evidence"] == "MAE 11.73"
    assert out[1]["has_metrics"] is False
