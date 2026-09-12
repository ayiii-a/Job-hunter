"""定时任务的定义。

为什么要落成文件，而不是在 cron 命令行里写一长串 prompt：

    任务提示词是**会改变 agent 行为的东西**。散在命令行参数里，
    改一次就没有历史——你没法 diff、没法回滚，出了问题也说不清
    「上周那个结果是哪一版提示词跑出来的」。

这和 `target_profile.yaml` 决定过滤行为、`ANALYZER_VERSION` 标记打分版本
是同一条道理：行为要住在版本化的工件里。见路线图 §0「行为落成工件」。

顺带拿到的第二个好处是**爆炸半径**：抓岗位的任务够不着改投递状态的工具，
哪怕它被 JD 里的注入内容说服了。这是「工具即边界」用在任务粒度上。

每个任务还可以带一个 `openclaw` 块，说明 OpenClaw 外壳怎么调度它（见 openclaw.py）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import config, profile
from .agent.client import PRICING
from .agent.tools import REGISTRY, Permission

DEFAULT_MAX_TURNS = 8
DEFAULT_MAX_LLM_CALLS = 20

#: openclaw 块里允许的字段。说明见 schedules.example.yaml 末尾
OPENCLAW_KEYS = frozenset({"cron", "chat", "model"})
CHAT_CHANNELS = frozenset({"discord"})


@dataclass
class Schedule:
    name: str
    task: str
    tools: set[str] | None = None          # None = 不按名字收窄
    permissions: set[Permission] | None = None
    max_turns: int = DEFAULT_MAX_TURNS
    max_llm_calls: int = DEFAULT_MAX_LLM_CALLS
    allow_notify: bool = False
    notes: str = ""
    openclaw: dict[str, str] = field(default_factory=dict)   # 空 = 不交给外壳

    def summary(self) -> str:
        scope = (
            f"{len(self.tools)} 个工具" if self.tools
            else (f"{'/'.join(sorted(p.value for p in self.permissions))} 档"
                  if self.permissions else "全部工具")
        )
        shell = ""
        if self.openclaw:
            how = (f"cron {self.openclaw['cron']}" if "cron" in self.openclaw
                   else f"聊天 {self.openclaw['chat']}")
            shell = f"  外壳：{how}"
        return (f"{self.name:<16} {scope:<14} 最多 {self.max_turns} 轮 / "
                f"{self.max_llm_calls} 次调用" + ("  外发已授权" if self.allow_notify else "")
                + shell)


class ScheduleError(ValueError):
    pass


def _parse_openclaw(name: str, raw: Any) -> dict[str, str]:
    """外壳调度设置。

    写错必须报错：cron 表达式少一段，OpenClaw 那边不一定报，任务只是永远不跑；
    model 没写，外壳会用它自己的默认模型，账就对不上了。
    """
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ScheduleError(f"定时任务 {name} 的 openclaw 应该是一个映射")
    unknown = set(raw) - OPENCLAW_KEYS
    if unknown:
        raise ScheduleError(
            f"定时任务 {name} 的 openclaw 有不认识的字段：{sorted(unknown)}。"
            f"认识的：{sorted(OPENCLAW_KEYS)}"
        )

    cron, chat = raw.get("cron"), raw.get("chat")
    if bool(cron) == bool(chat):
        raise ScheduleError(
            f"定时任务 {name} 的 openclaw 要么写 cron（定时跑），要么写 chat（聊天入口），只能二选一"
        )
    if cron and len(str(cron).split()) != 5:
        raise ScheduleError(f"定时任务 {name} 的 openclaw.cron 不是五段 cron 表达式：{cron!r}")
    if chat and chat not in CHAT_CHANNELS:
        raise ScheduleError(f"定时任务 {name} 的 openclaw.chat 只支持 {sorted(CHAT_CHANNELS)}")

    model = raw.get("model")
    if not model:
        raise ScheduleError(
            f"定时任务 {name} 的 openclaw 没写 model——外壳会用它自己的默认模型，账就对不上了"
        )
    if model not in PRICING:
        raise ScheduleError(f"定时任务 {name} 的 openclaw.model 不在 PRICING 里：{model}")

    out = {"model": str(model)}
    if cron:
        out["cron"] = str(cron)
    if chat:
        out["chat"] = str(chat)
    return out


def _parse_one(name: str, raw: dict[str, Any]) -> Schedule:
    task = (raw.get("task") or "").strip()
    if not task:
        raise ScheduleError(f"定时任务 {name} 没有 task（要 agent 做什么）")

    tools = raw.get("tools")
    tool_set: set[str] | None = None
    if tools:
        # 工具名写错会【静默】少给一个工具，agent 到时候莫名其妙做不了事。
        # 这类沉默失败正是这个项目一直在防的，所以这里直接报错。
        unknown = [t for t in tools if t not in REGISTRY]
        if unknown:
            raise ScheduleError(
                f"定时任务 {name} 引用了不存在的工具：{unknown}。"
                f"可用的：{sorted(REGISTRY)}"
            )
        tool_set = set(tools)

    perms = raw.get("permissions")
    perm_set: set[Permission] | None = None
    if perms:
        try:
            perm_set = {Permission(p) for p in perms}
        except ValueError as exc:
            raise ScheduleError(f"定时任务 {name} 的 permissions 有非法值：{perms}") from exc

    if tool_set and perm_set:
        raise ScheduleError(
            f"定时任务 {name} 同时写了 tools 和 permissions。二选一——"
            "按名字收窄更精确，按权限档收窄更粗但省事"
        )

    # 外发是不可撤回的，必须在定义里显式授权，不能靠调用时忘了加参数
    allow_notify = bool(raw.get("allow_notify"))
    if allow_notify and tool_set and "send_notification" not in tool_set:
        raise ScheduleError(
            f"定时任务 {name} 授权了 allow_notify，但 tools 里没有 send_notification"
        )

    shell = _parse_openclaw(name, raw.get("openclaw"))
    if shell and not tool_set and not perm_set:
        raise ScheduleError(
            f"定时任务 {name} 要交给外壳（openclaw），但没有收窄工具。"
            "外壳的配置我们管不着，交出去的工具必须先收窄"
        )

    return Schedule(
        name=name, task=task, tools=tool_set, permissions=perm_set,
        max_turns=int(raw.get("max_turns") or DEFAULT_MAX_TURNS),
        max_llm_calls=int(raw.get("max_llm_calls") or DEFAULT_MAX_LLM_CALLS),
        allow_notify=allow_notify, notes=(raw.get("notes") or "").strip(),
        openclaw=shell,
    )


def load_all() -> dict[str, Schedule]:
    if not config.SCHEDULES_PATH.exists():
        return {}
    data = profile.load_yaml(config.SCHEDULES_PATH)
    entries = data.get("schedules") or {}
    if not isinstance(entries, dict):
        raise ScheduleError("schedules.yaml 的顶层 schedules 应该是一个映射")
    return {name: _parse_one(name, raw or {}) for name, raw in entries.items()}


def load(name: str) -> Schedule:
    all_ = load_all()
    if name not in all_:
        raise ScheduleError(
            f"没有名为 {name} 的定时任务。已定义的：{sorted(all_) or '（一个都没有）'}"
        )
    return all_[name]
