"""OpenClaw 外壳：从 schedules.yaml 生成配置，并检查实际配置有没有被改松。

## 外壳是什么，不是什么

OpenClaw 补上的是**常驻运行 + 双向聊天入口**：按时把任务跑起来、结果推到手机，
你在手机上问一句它回一句。它**不是**新的安全边界——边界仍然是工具注册表，
并且在 `mcp_server.py` 里按任务再收窄一次。

## 为什么要 verify

外壳的约束全在一份 JSON 里：`tools.allow`、沙箱、heartbeat、谁能给 bot 发消息。
改松任何一项，OpenClaw 都不会报错——agent 只是悄悄多了 exec，或者陌生人
也能跟它说话。§0「沉默失败要变成响亮失败」，所以由我们来报。

verify **默认不通过**：找不到该有的键就算问题，不是「没写就当没事」。

## 配置键名

按 2026-09 的官方文档写（`agents.entries`、`bindings`、`channels.telegram`、
`openclaw cron add` 的参数）。OpenClaw 迭代很快，装好之后用 `openclaw config schema`
核对一遍；键名变了只需要改这个文件。
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

from . import config, profile, schedules
from .agent.loop import SYSTEM
from .agent.tools import REGISTRY, Permission
from .mcp_server import ScopeError, exposed_tools

PREFIX = "jha-"
DEFAULT_SETTINGS: dict[str, str] = {"mail_detect_every": "2h"}
CRON_MESSAGE = "按 AGENTS.md 里的任务执行一次，然后给我简报。"
PLACEHOLDER_TG = "<TELEGRAM_USER_ID>"

#: OpenClaw 的内置工具。jha agent 的白名单只能是我们注册表里的名字，
#: 这份清单只是为了报错时说清楚放进来的是哪一类
SHELL_CORE_TOOLS = frozenset({
    "exec", "process", "bash", "read", "write", "edit", "apply_patch",
    "browser", "canvas", "nodes", "cron", "gateway", "message",
    "sessions_spawn", "sessions_send", "sessions_list", "sessions_history",
    "subagents", "agents_list", "session_status", "web_search", "web_fetch",
    "image", "memory_search", "memory_get",
})


class ShellConfigError(ValueError):
    pass


@dataclass
class Bundle:
    config: dict[str, Any]
    cron_commands: list[str]
    agents_md: dict[str, str]
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 设置与小工具
# ---------------------------------------------------------------------------

def load_settings(path: Path | None = None) -> dict[str, str]:
    """schedules.yaml 顶层的 openclaw 块。"""
    path = path or config.SCHEDULES_PATH
    data = profile.load_yaml(path) if path.exists() else {}
    raw = data.get("openclaw") or {}
    if not isinstance(raw, dict):
        raise ShellConfigError("schedules.yaml 的顶层 openclaw 应该是一个映射")
    unknown = set(raw) - set(DEFAULT_SETTINGS)
    if unknown:
        raise ShellConfigError(f"schedules.yaml 的顶层 openclaw 有不认识的字段：{sorted(unknown)}")
    out = {**DEFAULT_SETTINGS, **{k: str(v) for k, v in raw.items()}}
    if not re.fullmatch(r"[1-9][0-9]*[mh]", out["mail_detect_every"]):
        raise ShellConfigError(
            f"openclaw.mail_detect_every 应该写成 30m、2h 这样：{out['mail_detect_every']!r}"
        )
    return out


def wsl_path(p: str | Path) -> str:
    """C:\\Projects\\x → /mnt/c/Projects/x。OpenClaw 跑在 WSL 里，看到的是这个路径。"""
    w = PureWindowsPath(str(p))
    if not w.drive:
        return str(p).replace("\\", "/")
    return f"/mnt/{w.drive[0].lower()}/" + "/".join(w.parts[1:])


def model_ref(model: str) -> str:
    return f"anthropic/{model}"


def telegram_user_id(raw: Any) -> int | None:
    if raw is None or str(raw).strip() == "":
        return None
    s = str(raw).strip()
    if not re.fullmatch(r"[0-9]+", s):
        raise ShellConfigError(
            f"Telegram 用户 id 应该是一串数字：{s!r}。负数是群聊 id——外壳只接你本人的私聊"
        )
    return int(s)


def shell_schedules(all_schedules: dict[str, schedules.Schedule]) -> dict[str, schedules.Schedule]:
    return {n: s for n, s in all_schedules.items() if s.openclaw}


def agents_md_text(s: schedules.Schedule) -> str:
    return (
        f"# {PREFIX}{s.name}\n\n"
        "> 由 `agent openclaw config` 从 config/schedules.yaml 生成。要改行为就改那份文件再重新生成——"
        "直接改这里，下次生成就被覆盖，而且没有版本历史。\n\n"
        f"{SYSTEM}\n\n## 你的任务\n\n{s.task.strip()}\n"
    )


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------

def generate(
    all_schedules: dict[str, schedules.Schedule], *, settings: dict[str, str],
    telegram_id: Any = None, python_path: str | None = None, agent_path: str | None = None,
) -> Bundle:
    """生成配置片段、cron 命令和各 agent 的 AGENTS.md。只返回，不写 ~/.openclaw。"""
    shell = shell_schedules(all_schedules)
    if not shell:
        raise ShellConfigError("schedules.yaml 里没有任何任务写了 openclaw 块，没什么可交给外壳的")
    chats = sorted(n for n, s in shell.items() if "chat" in s.openclaw)
    if len(chats) > 1:
        raise ShellConfigError(f"只能有一个聊天入口，现在有 {chats}——同一个人的私聊没法路由给两个 agent")

    warnings: list[str] = []
    tg = telegram_user_id(telegram_id)
    if tg is None:
        warnings.append("没有 Telegram 用户 id（--telegram-id 或 .env 的 TELEGRAM_CHAT_ID）。"
                        "配置里先放了占位符，verify 不会通过")
    tg_value: int | str = tg if tg is not None else PLACEHOLDER_TG

    scripts = config.ROOT / ".venv" / "Scripts"
    python = python_path or wsl_path(scripts / "python.exe")
    agent_exe = agent_path or wsl_path(scripts / "agent.exe")

    entries: dict[str, Any] = {}
    servers: dict[str, Any] = {}
    bindings: list[dict[str, Any]] = []
    crons: list[str] = []
    agents_md: dict[str, str] = {}

    for name in sorted(shell):
        s = shell[name]
        try:
            tools = sorted(exposed_tools(s))
        except ScopeError as exc:
            raise ShellConfigError(str(exc)) from exc
        aid = PREFIX + name

        servers[aid] = {
            "command": python,
            "args": ["-m", "jha.mcp_server", "--schedule", name],
            "transport": "stdio",
            "toolFilter": {"include": tools},
        }
        entries[aid] = {
            "workspace": f"~/.openclaw/workspace-{aid}",
            "model": model_ref(s.openclaw["model"]),
            "tools": {"allow": tools},
            "mcpServers": [aid],
            "sandbox": {"mode": "all"},
            "heartbeat": {"every": "0m"},
        }
        agents_md[aid] = agents_md_text(s)

        if "cron" in s.openclaw:
            crons.append(shlex.join([
                "openclaw", "cron", "add", "--name", aid, "--agent", aid,
                "--cron", s.openclaw["cron"], "--session", "isolated", "--light-context",
                "--model", model_ref(s.openclaw["model"]), "--message", CRON_MESSAGE,
                "--announce", "--channel", "telegram", "--to", str(tg_value),
            ]))
        else:
            bindings.append({
                "agentId": aid,
                "match": {"channel": s.openclaw["chat"],
                          "peer": {"kind": "direct", "id": f"tg:{tg_value}"}},
            })

    # 时间敏感的部分不经过模型：确定性的检测 + 推送，由外壳的 command 作业定时跑
    crons.append(shlex.join([
        "openclaw", "cron", "add", "--name", f"{PREFIX}mail-detect",
        "--every", settings["mail_detect_every"],
        "--command-argv", json.dumps([agent_exe, "mail", "sweep", "--push-alerts"], ensure_ascii=False),
        "--no-deliver",
    ]))

    cfg = {
        "gateway": {"bind": "loopback"},
        "agents": {
            "defaults": {"sandbox": {"mode": "all"}, "heartbeat": {"every": "0m"}},
            "entries": entries,
        },
        "mcpServers": servers,
        "bindings": bindings,
        "channels": {"telegram": {
            "enabled": True, "dmPolicy": "allowlist", "allowFrom": [tg_value],
            "groupPolicy": "allowlist", "groupAllowFrom": [],
        }},
    }
    return Bundle(cfg, crons, agents_md, warnings)


def write_bundle(bundle: Bundle, out_dir: Path) -> list[Path]:
    paths: list[Path] = []
    p = out_dir / "openclaw.fragment.json"
    config.write_text(p, json.dumps(bundle.config, ensure_ascii=False, indent=2) + "\n")
    paths.append(p)

    p = out_dir / "cron.sh"
    config.write_text(p, "#!/usr/bin/env bash\n"
                         "# 由 agent openclaw config 生成。在 WSL 里逐条确认后再跑\n"
                         "set -euo pipefail\n\n" + "\n".join(bundle.cron_commands) + "\n")
    paths.append(p)

    for aid, text in sorted(bundle.agents_md.items()):
        p = out_dir / "workspaces" / aid / "AGENTS.md"
        config.write_text(p, text)
        paths.append(p)
    return paths


# ---------------------------------------------------------------------------
# 检查
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ShellConfigError(f"找不到 {path}")
    try:
        data = json.loads(config.read_text(path))
    except json.JSONDecodeError as exc:
        raise ShellConfigError(
            f"{path} 不是严格 JSON（{exc}）。OpenClaw 允许 JSON5 写法（注释、不加引号的键），"
            "先去掉这些再检查"
        ) from exc
    if not isinstance(data, dict):
        raise ShellConfigError(f"{path} 的顶层应该是一个对象")
    return data


def _get(d: Any, *path: str, default: Any = None) -> Any:
    for key in path:
        if not isinstance(d, dict) or key not in d:
            return default
        d = d[key]
    return d


def _schedule_arg(args: Any) -> str | None:
    if not isinstance(args, list) or "--schedule" not in args:
        return None
    i = args.index("--schedule")
    return str(args[i + 1]) if i + 1 < len(args) else None


def verify(
    cfg: dict[str, Any], all_schedules: dict[str, schedules.Schedule], *, telegram_id: Any = None,
) -> list[str]:
    """返回所有不满足的地方。空列表 = 通过。"""
    problems: list[str] = []
    add = problems.append

    bind = _get(cfg, "gateway", "bind")
    if bind != "loopback":
        add(f"gateway.bind 应该是 loopback（现在是 {bind!r}）。"
            "一键 RCE 那类漏洞的前提就是 gateway 能被别的地方连到")
    if _get(cfg, "tools", "elevated", "enabled"):
        add("tools.elevated.enabled 打开了——elevated 让 exec 跑到沙箱外面")

    entries = _get(cfg, "agents", "entries")
    if not isinstance(entries, dict) or not entries:
        add("找不到 agents.entries")
        entries = {}
    defaults = _get(cfg, "agents", "defaults", default={})

    ours = {str(aid): e for aid, e in entries.items() if str(aid).startswith(PREFIX)}
    others = sorted(str(aid) for aid in entries if not str(aid).startswith(PREFIX))
    if entries and not ours:
        add("agents.entries 里没有 jha-* agent")
    if others:
        add(f"gateway 上还有别的 agent：{others}。它们不受这份检查约束，却和 jha agent 在同一台机器上——"
            "带 exec 的 agent 能直接读到 .env 里的邮箱密码。删掉，或者放到另一个 gateway")

    servers = cfg.get("mcpServers") if isinstance(cfg.get("mcpServers"), dict) else {}
    for aid, e in sorted(ours.items()):
        problems.extend(_verify_agent(aid, e if isinstance(e, dict) else {}, defaults,
                                      servers, all_schedules))

    problems.extend(_verify_telegram(cfg, telegram_id))
    problems.extend(_verify_bindings(cfg, all_schedules))
    return problems


def _verify_agent(
    aid: str, e: dict[str, Any], defaults: Any, servers: dict[str, Any],
    all_schedules: dict[str, schedules.Schedule],
) -> list[str]:
    name = aid[len(PREFIX):]
    s = all_schedules.get(name)
    if s is None or not s.openclaw:
        return [f"{aid}：schedules.yaml 里没有写了 openclaw 块的任务 {name}"]
    try:
        scope = exposed_tools(s)
    except ScopeError as exc:
        return [f"{aid}：{exc}"]

    out: list[str] = []
    allow = _get(e, "tools", "allow")
    if not isinstance(allow, list) or not allow:
        out.append(f"{aid}：没有 tools.allow 白名单——没有白名单，内置工具就全都能用")
    else:
        for t in map(str, allow):
            if t == "*" or t.startswith("group:"):
                out.append(f"{aid}：tools.allow 里有 {t!r}，会放进一整批内置工具")
            elif t in SHELL_CORE_TOOLS:
                out.append(f"{aid}：tools.allow 里有 OpenClaw 内置工具 {t!r}")
            elif t in REGISTRY and REGISTRY[t].permission is Permission.GATED:
                out.append(f"{aid}：tools.allow 里有 GATED 工具 {t!r}——外发不交给外壳")
            elif t not in scope:
                out.append(f"{aid}：tools.allow 里有 {t!r}，不在任务 {name} 的工具集里")

    mode = _get(e, "sandbox", "mode", default=_get(defaults, "sandbox", "mode"))
    if mode != "all":
        out.append(f"{aid}：sandbox.mode 应该是 all（现在是 {mode!r}）")
    every = _get(e, "heartbeat", "every", default=_get(defaults, "heartbeat", "every"))
    if str(every) not in ("0m", "0"):
        out.append(f"{aid}：heartbeat 没关（every = {every!r}）。定时交给 cron；"
                   "heartbeat 每次都带着主会话的历史，是 OpenClaw 账单失控的头号原因")

    refs = e.get("mcpServers")
    if not isinstance(refs, list) or not refs:
        out.append(f"{aid}：没有指定 mcpServers，会拿到 gateway 上所有 MCP 服务器的工具")
        return out
    for sid in refs:
        srv = servers.get(sid)
        if not isinstance(srv, dict):
            out.append(f"{aid}：引用了不存在的 MCP 服务器 {sid!r}")
            continue
        args = srv.get("args")
        if not isinstance(args, list) or "jha.mcp_server" not in args or _schedule_arg(args) != name:
            out.append(f"{aid}：MCP 服务器 {sid!r} 服务的不是任务 {name}（args = {args!r}）")
        include = _get(srv, "toolFilter", "include")
        if isinstance(include, list):
            extra = sorted(set(map(str, include)) - scope)
            if extra:
                out.append(f"{aid}：MCP 服务器 {sid!r} 的 toolFilter.include 多出了 {extra}")
    return out


def _verify_telegram(cfg: dict[str, Any], telegram_id: Any) -> list[str]:
    tg = _get(cfg, "channels", "telegram")
    if not isinstance(tg, dict):
        return ["找不到 channels.telegram——聊天入口和推送都靠它"]

    out: list[str] = []
    if tg.get("dmPolicy") != "allowlist":
        out.append(f"channels.telegram.dmPolicy 应该是 allowlist（现在是 {tg.get('dmPolicy')!r}）"
                   "——否则陌生人也能跟 agent 说话")

    allow_from = tg.get("allowFrom")
    if not isinstance(allow_from, list) or not allow_from:
        out.append("channels.telegram.allowFrom 是空的")
    else:
        bad = [x for x in allow_from if not re.fullmatch(r"[0-9]+", str(x))]
        if bad:
            out.append(f"channels.telegram.allowFrom 里有不是用户 id 的值：{bad}")
        try:
            expected = telegram_user_id(telegram_id)
        except ShellConfigError as exc:
            out.append(str(exc))
            expected = None
        if expected is not None and [str(x) for x in allow_from] != [str(expected)]:
            out.append(f"channels.telegram.allowFrom 应该只有你本人（{expected}），现在是 {allow_from}")
        elif expected is None and len(allow_from) > 1:
            out.append(f"channels.telegram.allowFrom 里不止一个人：{allow_from}")

    if tg.get("groupPolicy") not in ("allowlist", "disabled"):
        out.append(f"channels.telegram.groupPolicy 应该是 allowlist 或 disabled"
                   f"（现在是 {tg.get('groupPolicy')!r}）")
    if tg.get("groupAllowFrom"):
        out.append("channels.telegram.groupAllowFrom 不是空的——群里的人也能跟 agent 说话")
    accounts = tg.get("accounts") if isinstance(tg.get("accounts"), dict) else {}
    for acc, a in accounts.items():
        if isinstance(a, dict) and a.get("dmPolicy") not in (None, "allowlist"):
            out.append(f"channels.telegram.accounts.{acc}.dmPolicy 是 {a.get('dmPolicy')!r}，"
                       "会盖掉上面的 allowlist")
    return out


def _verify_bindings(cfg: dict[str, Any], all_schedules: dict[str, schedules.Schedule]) -> list[str]:
    bindings = cfg.get("bindings") or []
    if not isinstance(bindings, list):
        return ["bindings 应该是一个列表"]

    out: list[str] = []
    for b in bindings:
        target = str(_get(b, "agentId", default=""))
        channel = _get(b, "match", "channel")
        if not target.startswith(PREFIX):
            if channel == "telegram":
                out.append(f"bindings 把 Telegram 消息路由给了 {target!r}，不是 jha agent")
            continue
        s = all_schedules.get(target[len(PREFIX):])
        if s is None or s.openclaw.get("chat") != channel:
            out.append(f"bindings 把 {channel} 消息路由给了 {target!r}，但它不是聊天入口——"
                       "定时任务的 agent 手里有写入工具，不该能从聊天里触发")
    return out
