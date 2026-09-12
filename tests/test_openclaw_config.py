"""OpenClaw 外壳配置：生成出来的要通过检查，改松任何一处都要被查出来。

外壳的约束全在一份 JSON 里，改松了 OpenClaw 不会报错。这组测试守的是
「verify 真的有牙齿」——每一种改松的方式都对应一条会失败的断言。
"""

import copy
import json
import shlex
from pathlib import PureWindowsPath

import pytest

from jha import config, openclaw, profile, schedules
from jha.mcp_server import exposed_tools

UID = "123456789012345678"


def shipped():
    data = profile.load_yaml(config.CONFIG_DIR / "schedules.example.yaml")
    return {n: schedules._parse_one(n, raw or {}) for n, raw in data["schedules"].items()}


def settings():
    return dict(openclaw.DEFAULT_SETTINGS)


@pytest.fixture
def bundle():
    return openclaw.generate(shipped(), settings=settings(), discord_id=UID)


@pytest.fixture
def cfg(bundle):
    return copy.deepcopy(bundle.config)


def problems(cfg):
    return openclaw.verify(cfg, shipped(), discord_id=UID)


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------

def test_generated_config_passes_verify(bundle):
    assert problems(bundle.config) == []
    assert bundle.warnings == []


def test_every_shell_agent_gets_exactly_its_schedule_tools(bundle):
    entries = bundle.config["agents"]["entries"]
    for name, s in shipped().items():
        if not s.openclaw:
            assert f"jha-{name}" not in entries
            continue
        e = entries[f"jha-{name}"]
        assert set(e["tools"]["allow"]) == exposed_tools(s)
        assert "send_notification" not in e["tools"]["allow"]
        assert bundle.config["mcpServers"][f"jha-{name}"]["args"] == [
            "-m", "jha.mcp_server", "--schedule", name]


def test_only_the_chat_agent_is_reachable_from_discord(bundle):
    assert {b["agentId"] for b in bundle.config["bindings"]} == {"jha-phone-query"}


def test_cron_jobs_are_isolated_light_and_announce_to_you(bundle):
    agent_jobs = [c for c in bundle.cron_commands if "--agent" in c]
    assert len(agent_jobs) == 2
    for c in agent_jobs:
        argv = shlex.split(c)
        assert argv[:3] == ["openclaw", "cron", "add"]
        assert argv[argv.index("--session") + 1] == "isolated"
        assert "--light-context" in argv and "--announce" in argv
        assert argv[argv.index("--model") + 1].startswith("anthropic/claude-")
        assert argv[argv.index("--to") + 1] == f"user:{UID}"


def test_mail_detect_is_a_command_job_not_an_agent(bundle):
    """时间敏感的部分不经过模型。"""
    job = next(c for c in bundle.cron_commands if "jha-mail-detect" in c)
    argv = shlex.split(job)
    assert "--agent" not in argv and "--model" not in argv
    command = json.loads(argv[argv.index("--command-argv") + 1])
    assert command[1:] == ["mail", "sweep", "--push-alerts"]
    assert command[0].startswith("/mnt/")


def test_agents_md_carries_the_task_and_the_rules(bundle):
    md = bundle.agents_md["jha-email-sweep"]
    assert "schedules.yaml" in md and "不可信输入" in md
    assert shipped()["email-sweep"].task.strip() in md


def test_wsl_path():
    p = PureWindowsPath("C:/Projects/Job Hunting Agent/.venv/Scripts/python.exe")
    assert openclaw.wsl_path(p) == "/mnt/c/Projects/Job Hunting Agent/.venv/Scripts/python.exe"


def test_missing_discord_id_is_loud():
    b = openclaw.generate(shipped(), settings=settings(), discord_id=None)
    assert b.warnings
    assert any("allowFrom" in p for p in openclaw.verify(b.config, shipped(), discord_id=None))


def test_malformed_user_id_is_refused():
    with pytest.raises(openclaw.ShellConfigError, match="数字"):
        openclaw.generate(shipped(), settings=settings(), discord_id="@zijiang")


def test_two_chat_entries_are_refused():
    s = shipped()
    s["phone-2"] = schedules._parse_one("phone-2", {
        "task": "t", "tools": ["list_jobs"],
        "openclaw": {"chat": "discord", "model": "claude-haiku-4-5"},
    })
    with pytest.raises(openclaw.ShellConfigError, match="聊天入口"):
        openclaw.generate(s, settings=settings(), discord_id=UID)


def test_write_bundle(tmp_path, bundle):
    openclaw.write_bundle(bundle, tmp_path)
    fragment = json.loads((tmp_path / "openclaw.fragment.json").read_text(encoding="utf-8"))
    assert fragment == bundle.config
    assert (tmp_path / "workspaces" / "jha-phone-query" / "AGENTS.md").exists()
    assert "jha-mail-detect" in (tmp_path / "cron.sh").read_text(encoding="utf-8")


def test_settings_are_validated(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("openclaw:\n  mail_detect_every: often\n", encoding="utf-8")
    with pytest.raises(openclaw.ShellConfigError, match="2h"):
        openclaw.load_settings(p)


# ---------------------------------------------------------------------------
# 检查：每一种改松都要被查出来
# ---------------------------------------------------------------------------

def _agent(c, name="email-sweep"):
    return c["agents"]["entries"][f"jha-{name}"]


LOOSENINGS = {
    "exec 进了白名单": lambda c: _agent(c)["tools"]["allow"].append("exec"),
    "白名单写成 *": lambda c: _agent(c)["tools"].__setitem__("allow", ["*"]),
    "白名单里有工具组": lambda c: _agent(c)["tools"]["allow"].append("group:fs"),
    "白名单没了": lambda c: _agent(c).pop("tools"),
    "GATED 工具进了白名单": lambda c: _agent(c)["tools"]["allow"].append("send_notification"),
    "任务外的写入工具": lambda c: _agent(c)["tools"]["allow"].append("append_event"),
    "聊天入口拿到写入工具": lambda c: _agent(c, "phone-query")["tools"]["allow"].append("sweep_emails"),
    "沙箱不是 all": lambda c: _agent(c)["sandbox"].__setitem__("mode", "non-main"),
    "heartbeat 打开": lambda c: _agent(c)["heartbeat"].__setitem__("every", "30m"),
    "gateway 对外": lambda c: c["gateway"].__setitem__("bind", "lan"),
    "陌生人能私聊": lambda c: c["channels"]["discord"].__setitem__("dmPolicy", "open"),
    "allowFrom 多了人": lambda c: c["channels"]["discord"]["allowFrom"].append("987654321098765432"),
    "id 写成了数字": lambda c: c["channels"]["discord"].__setitem__("allowFrom", [int(UID)]),
    "服务器放开": lambda c: c["channels"]["discord"].__setitem__("guilds", {"111111111111111111": {}}),
    "账号级 dmPolicy 盖掉 allowlist": lambda c: c["channels"]["discord"].__setitem__(
        "accounts", {"default": {"dmPolicy": "pairing"}}),
    "多了一个带 exec 的 agent": lambda c: c["agents"]["entries"].__setitem__(
        "main", {"tools": {"allow": ["exec"]}}),
    "Discord 路由给了定时任务的 agent": lambda c: c["bindings"].append(
        {"agentId": "jha-daily-jobs", "match": {"channel": "discord"}}),
    "Discord 路由给了别的 agent": lambda c: c["bindings"].append(
        {"agentId": "main", "match": {"channel": "discord"}}),
    "MCP 服务器换了任务": lambda c: c["mcpServers"]["jha-email-sweep"].__setitem__(
        "args", ["-m", "jha.mcp_server", "--schedule", "daily-jobs"]),
    "toolFilter 多放了工具": lambda c: c["mcpServers"]["jha-email-sweep"]["toolFilter"]["include"].append(
        "append_event"),
    "没指定 MCP 服务器": lambda c: _agent(c).pop("mcpServers"),
    "elevated 打开": lambda c: c.setdefault("tools", {}).__setitem__("elevated", {"enabled": True}),
}


@pytest.mark.parametrize("how", sorted(LOOSENINGS))
def test_every_loosening_is_caught(cfg, how):
    LOOSENINGS[how](cfg)
    assert problems(cfg), f"没查出来：{how}"


def test_missing_keys_fail_closed():
    """找不到该有的键就算问题——不是「没写就当没事」。"""
    found = openclaw.verify({}, shipped(), discord_id=UID)
    assert any("gateway.bind" in p for p in found)
    assert any("agents.entries" in p for p in found)
    assert any("channels.discord" in p for p in found)


def test_defaults_are_inherited(cfg):
    for e in cfg["agents"]["entries"].values():
        e.pop("sandbox")
        e.pop("heartbeat")
    assert problems(cfg) == []


def test_json5_config_gets_a_clear_error(tmp_path):
    p = tmp_path / "openclaw.json"
    p.write_text("{ // 注释\n  gateway: { bind: 'loopback' } }", encoding="utf-8")
    with pytest.raises(openclaw.ShellConfigError, match="JSON5"):
        openclaw.load_config(p)
