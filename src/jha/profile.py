"""母简历 / 目标画像的加载与校验。

为什么值得在 Phase 0 就写这个校验器：
Phase 3 的防幻觉设计是「LLM 只输出 bullet ID，不输出文本」。这个设计成立的
前提是 ID 唯一、技能引用能解析、story 关联的 bullet 真的存在。这些前提一旦
破了，LLM 选出来的 ID 会静默指向错误的内容——而那正是最难发现的一类错误。

所以这里宁可啰嗦：能在写母简历的阶段就报出来的问题，绝不留到投递前。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import config


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def load_yaml(path: str | Path) -> dict[str, Any]:
    data = yaml.safe_load(config.read_text(path))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} 顶层应该是一个映射，实际是 {type(data).__name__}")
    return data


# config/master_profile.yaml 里出现过的示例值。留着就说明还没填完。
_TEMPLATE_MARKERS = (
    "Your Name",
    "you@example.com",
    "yourname",
    "Acme Corp",
    "Globex",
    "State University",
    "+1-555-000-0000",
    "$X–$Y",
)


def _template_leftovers(data: Any) -> set[str]:
    """把整棵 YAML 拍平成文本，扫一遍模板占位符。"""
    blob = yaml.safe_dump(data, allow_unicode=True, default_flow_style=False)
    return {m for m in _TEMPLATE_MARKERS if m in blob}


def _has_metrics(bullet: dict[str, Any]) -> bool:
    """这条 bullet 有没有量化结果。

    `metrics` 接受三种写法：
        false      —— 没有
        true       —— 有，数字已经写进 text 了
        "<字符串>" —— 有，而且这里存着原始测量值

    第三种是最有用的：它把【证据】和【文案】分开存。
      · Phase 3 的确定性校验器需要一份「合法数字白名单」来判断改写有没有编造
        新数字——这些字符串就是那份白名单的来源
      · Phase 6 面试模拟追问「这个数据怎么来的」时，原始值在这里
      · 半年后你自己也还记得 0.936 是 Pearson r 不是准确率
    """
    value = bullet.get("metrics")
    if value is True:
        return True
    return isinstance(value, str) and bool(value.strip())


def metric_evidence(bullet: dict[str, Any]) -> str | None:
    """取出 bullet 的原始测量值（如果以字符串形式记了的话）。"""
    value = bullet.get("metrics")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _iter_bullets(entries: list[dict[str, Any]], kind: str):
    """遍历 experiences / projects 下的所有 bullet，带上归属信息。"""
    for entry in entries or []:
        owner = entry.get("id") or entry.get("company") or entry.get("name") or "<未命名>"
        for bullet in entry.get("bullets") or []:
            yield kind, owner, bullet


def validate_master_profile(data: dict[str, Any]) -> ValidationReport:
    rep = ValidationReport()

    # --- basics ---
    basics = data.get("basics") or {}
    for key in ("name", "email", "location", "work_authorization"):
        if not basics.get(key):
            rep.errors.append(f"basics.{key} 缺失或为空")

    # --- skills：ID 唯一 ---
    skills = data.get("skills") or []
    skill_ids: set[str] = set()
    for i, sk in enumerate(skills):
        sid = sk.get("id")
        if not sid:
            rep.errors.append(f"skills[{i}] 没有 id")
            continue
        if sid in skill_ids:
            rep.errors.append(f"skill id 重复：{sid}")
        skill_ids.add(sid)
        if not sk.get("name"):
            rep.warnings.append(f"skill {sid} 没有 name")

    # --- experiences / projects：bullet ID 全局唯一 + 技能引用可解析 ---
    bullet_ids: set[str] = set()
    no_metrics: list[str] = []
    all_entries = [
        ("experience", data.get("experiences") or []),
        ("project", data.get("projects") or []),
    ]

    for kind, entries in all_entries:
        for entry in entries:
            owner = entry.get("id") or entry.get("company") or entry.get("name") or "<未命名>"
            if not entry.get("id"):
                rep.errors.append(f"{kind} 「{owner}」没有 id")
            for ref in entry.get("tech") or []:
                if ref not in skill_ids:
                    rep.errors.append(
                        f"{kind} 「{owner}」的 tech 引用了不存在的 skill：{ref}"
                    )
            if not (entry.get("bullets") or []):
                rep.warnings.append(f"{kind} 「{owner}」一条 bullet 都没有")

    for kind, owner, bullet in (
        list(_iter_bullets(data.get("experiences") or [], "experience"))
        + list(_iter_bullets(data.get("projects") or [], "project"))
    ):
        bid = bullet.get("id")
        if not bid:
            rep.errors.append(f"{kind} 「{owner}」下有 bullet 没有 id")
            continue
        if bid in bullet_ids:
            # 这条最致命：LLM 选中这个 ID 时，渲染器不知道该取哪一条
            rep.errors.append(f"bullet id 重复：{bid}（在 {kind} 「{owner}」）")
        bullet_ids.add(bid)

        if not (bullet.get("text") or "").strip():
            rep.errors.append(f"bullet {bid} 的 text 为空")
        if not _has_metrics(bullet):
            no_metrics.append(bid)
        for tag_ref in bullet.get("skills") or []:
            if tag_ref not in skill_ids:
                rep.errors.append(f"bullet {bid} 的 skills 引用了不存在的 skill：{tag_ref}")

    # --- story_bank：关联的 bullet 必须存在 ---
    stories = data.get("story_bank") or []
    story_ids: set[str] = set()
    for i, st in enumerate(stories):
        sid = st.get("id")
        if not sid:
            rep.errors.append(f"story_bank[{i}] 没有 id")
            continue
        if sid in story_ids:
            rep.errors.append(f"story id 重复：{sid}")
        story_ids.add(sid)
        linked = st.get("linked_bullets") or []
        if not linked:
            rep.warnings.append(f"story {sid} 没有关联任何 bullet（面试模拟时接不回简历）")
        for ref in linked:
            if ref not in bullet_ids:
                rep.errors.append(f"story {sid} 关联了不存在的 bullet：{ref}")
        for part in ("situation", "task", "action", "result"):
            if not st.get(part):
                rep.warnings.append(f"story {sid} 缺 {part}")

    # --- qa_bank：投递表单要用，缺哪条后面就得手填 ---
    qa = data.get("qa_bank") or {}
    for key in ("work_authorization", "salary_expectation", "why_company_template"):
        if not qa.get(key):
            rep.warnings.append(f"qa_bank.{key} 未填——投递表单遇到这题只能留空手填")

    # --- 模板残留 ---
    # 模板里的示例内容是能通过全部结构校验的（否则你没法先跑通再填）。
    # 代价是：忘了替换也不会报错，最后会生成一份写着 Acme Corp 的简历投出去。
    # 所以单独查一遍占位符。
    leftovers = _template_leftovers(data)
    if leftovers:
        rep.warnings.append(
            "母简历里还留着模板的示例内容："
            + "、".join(sorted(leftovers)[:6])
            + "。真投之前必须全部换成你自己的"
        )

    # --- 汇总 ---
    rep.stats = {
        "skills": len(skill_ids),
        "experiences": len(data.get("experiences") or []),
        "projects": len(data.get("projects") or []),
        "bullets": len(bullet_ids),
        "bullets_without_metrics": len(no_metrics),
        "stories": len(story_ids),
    }

    if bullet_ids and len(no_metrics) / len(bullet_ids) > 0.5:
        rep.warnings.append(
            f"{len(no_metrics)}/{len(bullet_ids)} 条 bullet 没有量化结果。"
            "定制简历时会优先选有数据的，比例太低会限制选材空间"
        )

    return rep


def validate_target_profile(data: dict[str, Any]) -> ValidationReport:
    rep = ValidationReport()

    include = data.get("titles_include") or []
    if not include:
        rep.errors.append("titles_include 为空——规则初筛没有正向关键词就等于不过滤")
    if not (data.get("titles_exclude") or []):
        rep.warnings.append(
            "titles_exclude 为空。排除项比包含项更省时间，建议至少写上不想要的方向"
        )
    if not (data.get("locations") or []) and not data.get("remote_ok"):
        rep.errors.append(
            "locations 为空且 remote_ok 未开——地点是规则初筛的第一道闸，"
            "不设的话单家公司就可能涌进上百个不相关地点的岗位"
        )

    # titles_include 是硬过滤，title_tiers 只排优先级。
    # 在 tier 里加了岗位却忘了加进 titles_include，那个岗位会被第一道闸直接
    # 滤掉——你以为把它排进了主攻方向，实际它根本不会出现。
    tiers = data.get("title_tiers") or {}
    included = {t.lower() for t in include}
    for tier_name, titles in tiers.items():
        for title in titles or []:
            if title.lower() not in included:
                rep.errors.append(
                    f"title_tiers.{tier_name} 里的「{title}」不在 titles_include 中，"
                    "会被规则初筛直接滤掉"
                )

    visa = data.get("visa") or {}
    if visa.get("stem_opt_eligible") == "unverified":
        rep.warnings.append(
            "visa.stem_opt_eligible 还没确认。12 个月和 36 个月是两种求职策略——"
            "只有 12 个月的话第一年就得盯能办 H-1B 的公司，去 ISSO 问一下 CIP code"
        )
    if visa.get("needs_sponsorship_eventually") and not (visa.get("hard_fail_phrases") or []):
        rep.warnings.append(
            "需要担保却没设 hard_fail_phrases——明说不担保的岗位会照常推给你，白花时间"
        )

    rep.stats = {
        "titles_include": len(include),
        "titles_exclude": len(data.get("titles_exclude") or []),
        "tiers": len(tiers),
        "locations": len(data.get("locations") or []),
        "locations_exclude": len(data.get("locations_exclude") or []),
        "deal_breakers": len(data.get("deal_breakers") or []),
    }
    return rep


def load_master_profile() -> dict[str, Any]:
    return load_yaml(config.MASTER_PROFILE_PATH)


def load_target_profile() -> dict[str, Any]:
    return load_yaml(config.TARGET_PROFILE_PATH)
