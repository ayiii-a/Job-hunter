"""定时任务定义的测试。

守两件事：
  1. **配置写错要报错，不能静默生效** —— 工具名打错一个字母会少给一个工具，
     agent 到时候莫名其妙做不了事，而日志看起来完全正常
  2. **收窄真的收窄了** —— 爆炸半径不是文档里的说法，是能断言的属性
"""

import pytest

from jha import config, db, schedules
from jha.agent import Permission, loop, tools as tools_mod

from test_agent import FakeClient, text_block


def write(tmp_path, monkeypatch, body: str):
    path = tmp_path / "schedules.yaml"
    config.write_text(path, body)
    monkeypatch.setattr(config, "SCHEDULES_PATH", path)
    return path


# ---------------------------------------------------------------------------
# 配置写错必须报错
# ---------------------------------------------------------------------------

def test_typo_in_tool_name_is_an_error(tmp_path, monkeypatch):
    """工具名打错一个字母不能静默通过。

    `analyze_job`（少个 s）会让 agent 少一个工具，而它不会报错，
    只会做不到你要它做的事——这类沉默失败正是这个项目一直在防的。
    """
    write(tmp_path, monkeypatch, """
schedules:
  daily:
    task: 抓岗位
    tools: [fetch_jobs, analyze_job]
""")
    with pytest.raises(schedules.ScheduleError, match="analyze_job"):
        schedules.load_all()


def test_missing_task_is_an_error(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, "schedules:\n  daily:\n    tools: [fetch_jobs]\n")
    with pytest.raises(schedules.ScheduleError, match="没有 task"):
        schedules.load_all()


def test_bad_permission_value_is_an_error(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch,
          "schedules:\n  r:\n    task: x\n    permissions: [readonly]\n")
    with pytest.raises(schedules.ScheduleError, match="permissions"):
        schedules.load_all()


def test_tools_and_permissions_together_is_an_error(tmp_path, monkeypatch):
    # 两种收窄方式混用，语义不清楚：到底是交集还是并集？直接禁掉
    write(tmp_path, monkeypatch,
          "schedules:\n  x:\n    task: t\n    tools: [fetch_jobs]\n    permissions: [read]\n")
    with pytest.raises(schedules.ScheduleError, match="二选一"):
        schedules.load_all()


def test_allow_notify_without_the_tool_is_an_error(tmp_path, monkeypatch):
    """授权了推送却没把工具放进来 —— 一定是写错了，报出来。"""
    write(tmp_path, monkeypatch, """
schedules:
  x:
    task: t
    tools: [fetch_jobs]
    allow_notify: true
""")
    with pytest.raises(schedules.ScheduleError, match="send_notification"):
        schedules.load_all()


def test_unknown_schedule_name_lists_what_exists(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, "schedules:\n  daily:\n    task: x\n")
    with pytest.raises(schedules.ScheduleError, match="daily"):
        schedules.load("dailyy")


def test_missing_file_is_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SCHEDULES_PATH", tmp_path / "nope.yaml")
    assert schedules.load_all() == {}


# ---------------------------------------------------------------------------
# 收窄真的生效
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


def tools_offered(conn, **kw) -> set[str]:
    client = FakeClient([[text_block("ok")]])
    loop.run("t", conn, client=client, max_turns=2, record=False, **kw)
    return {t["name"] for t in client.calls[0]["tools"]}


def test_no_scoping_offers_everything(conn):
    assert tools_offered(conn) == set(tools_mod.REGISTRY)


def test_name_scoping_offers_exactly_that_set(conn):
    wanted = {"fetch_jobs", "analyze_jobs", "list_contacts"}
    assert tools_offered(conn, tool_names=wanted) == wanted


def test_scoping_shrinks_the_blast_radius(conn):
    """这才是收窄的主要收益——省 token 只是顺带。

    抓岗位的任务在结构上够不着改投递状态的工具，
    哪怕它被 JD 里的注入内容说服了。
    """
    offered = tools_offered(conn, tool_names={"fetch_jobs", "analyze_jobs"})
    for unreachable in ("record_application", "append_event", "tailor_resume",
                        "send_notification"):
        assert unreachable not in offered


def test_permission_scoping_still_works(conn):
    offered = tools_offered(conn, allow={Permission.READ})
    assert "list_jobs" in offered
    assert "fetch_jobs" not in offered


def test_scoping_measurably_shrinks_the_schema(conn):
    """收窄之后每轮发出去的 schema 确实小了。

    agent loop 每轮重发全部工具 schema，所以这个差值按轮数累加。
    """
    import json

    full = json.dumps(tools_mod.specs(), ensure_ascii=False)
    narrow = json.dumps(
        tools_mod.specs(names={"fetch_jobs", "analyze_jobs", "rank_jobs"}),
        ensure_ascii=False,
    )
    assert len(narrow) < len(full) / 2


# ---------------------------------------------------------------------------
# 随仓库附带的模板
# ---------------------------------------------------------------------------

def test_shipped_template_parses_and_references_real_tools():
    """模板里引用的工具必须真实存在——否则第一次跑就报错。"""
    template = config.CONFIG_DIR / "schedules.example.yaml"
    assert template.exists()
    from jha import profile

    data = profile.load_yaml(template)
    for name, raw in (data.get("schedules") or {}).items():
        parsed = schedules._parse_one(name, raw or {})
        assert parsed.task.strip()
        if parsed.tools:
            assert parsed.tools <= set(tools_mod.REGISTRY)


def test_weekly_review_is_read_only_by_design():
    """复盘的价值在于你自己看见问题，不是让 agent 顺手改掉。"""
    from jha import profile

    data = profile.load_yaml(config.CONFIG_DIR / "schedules.example.yaml")
    review = schedules._parse_one("weekly-review", data["schedules"]["weekly-review"])
    assert review.permissions == {Permission.READ}
    assert not review.allow_notify


def test_shipped_schedules_do_not_auto_send(tmp_path):
    """外发不可撤回，模板里默认一律不授权。"""
    from jha import profile

    data = profile.load_yaml(config.CONFIG_DIR / "schedules.example.yaml")
    for name, raw in (data.get("schedules") or {}).items():
        assert not schedules._parse_one(name, raw or {}).allow_notify, name


def test_phone_query_is_read_only():
    """聊天入口中间隔着一个模型——只读是刻意的，确认面试邀请这类事回电脑上做。"""
    from jha import profile

    data = profile.load_yaml(config.CONFIG_DIR / "schedules.example.yaml")
    s = schedules._parse_one("phone-query", data["schedules"]["phone-query"])
    assert s.tools and all(tools_mod.REGISTRY[t].permission is Permission.READ for t in s.tools)
    assert s.openclaw.get("chat") == "discord"


def test_email_sweep_does_not_push_through_the_agent():
    """即时提醒走确定性的 mail sweep --push-alerts，不经过模型。"""
    from jha import profile

    data = profile.load_yaml(config.CONFIG_DIR / "schedules.example.yaml")
    s = schedules._parse_one("email-sweep", data["schedules"]["email-sweep"])
    assert "send_notification" not in s.tools and not s.allow_notify
    assert "get_fetch_health" in s.tools


# ---------------------------------------------------------------------------
# openclaw 块：写错要报错，不能让任务悄悄永远不跑
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("block, match", [
    ("openclaw:\n      model: claude-haiku-4-5", "二选一"),
    ("openclaw:\n      cron: '0 9 * * *'\n      chat: discord\n      model: claude-haiku-4-5", "二选一"),
    ("openclaw:\n      cron: '0 9 * *'\n      model: claude-haiku-4-5", "五段"),
    ("openclaw:\n      chat: slack\n      model: claude-haiku-4-5", "discord"),
    ("openclaw:\n      cron: '0 9 * * *'", "model"),
    ("openclaw:\n      cron: '0 9 * * *'\n      model: gpt-9", "PRICING"),
    ("openclaw:\n      cron: '0 9 * * *'\n      model: claude-haiku-4-5\n      tools: [exec]", "不认识"),
])
def test_bad_openclaw_block_is_an_error(tmp_path, monkeypatch, block, match):
    write(tmp_path, monkeypatch, f"schedules:\n  x:\n    task: t\n    tools: [list_jobs]\n    {block}\n")
    with pytest.raises(schedules.ScheduleError, match=match):
        schedules.load_all()


def test_openclaw_block_requires_scoped_tools(tmp_path, monkeypatch):
    """外壳的配置我们管不着，交出去的工具必须先收窄。"""
    write(tmp_path, monkeypatch,
          "schedules:\n  x:\n    task: t\n    openclaw:\n      cron: '0 9 * * *'\n      model: claude-haiku-4-5\n")
    with pytest.raises(schedules.ScheduleError, match="收窄"):
        schedules.load_all()
