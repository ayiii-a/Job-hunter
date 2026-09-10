"""简历幻觉校验器 —— 全项目最重要的安全阀，**全部是确定性代码**。

路线图 Phase 3 的注意事项写得很清楚：校验器不要用第二次 LLM 调用。
理由是它的规格本质上就是一次集合差运算——从母简历抽出全部数字 + 专名 +
技术词做白名单，对产出文本做差集，非空就拒。

确定性检查比 LLM 校验**更严格**（不会心软放过）、免费、可单元测试。
**安全阀不该建在一个会随机放水的组件上。**

三道检查，强度递减：

    verify_selection   选中的 bullet id 必须真实存在        —— 选材路径（默认）
    verify_rendered    渲染出的文本里的数字/专名必须在母简历里 —— 最终闸门
    verify_rewrite     改写后不得新增任何实体                —— 改写路径（默认关）

第一道最强也最简单：ID 选材模式下，简历文本是**逐字从母简历取出来的**，
所以「是不是原文」可以精确判定，不需要任何推断。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

#: 模板自己引入的字样。它们不来自母简历，但也不是编造。
TEMPLATE_CHROME: frozenset[str] = frozenset({
    "EXPERIENCE", "EXPERIENCES", "PROJECTS", "EDUCATION", "SKILLS",
    "Experience", "Projects", "Education", "Skills", "Present",
})

#: 数字：整数、小数、百分比（800ms / 19.77 / 0.936 / 12%）
#:
#: 百分号前只允许**空格和制表符**，不能是 `\s`——`\s` 会吃掉换行，
#: 于是「2024-05\n」被抽成 "05\n"，跟白名单里的 "05" 对不上，
#: 每个日期都报一次假阳性。校验器一旦开始狼来了，你就不再看它了，
#: 那它等于不存在。
_NUM = re.compile(r"\d+(?:[.,]\d+)*[ \t]*%?")

#: 专名与技术词：首字母大写的词、全大写缩写、带点或连字符的技术名
#: （PyTorch / ResNet-18 / MAE / ROC-AUC / Three.js / C++）
_ENTITY = re.compile(r"[A-Z][A-Za-z0-9+#]*(?:[.\-][A-Za-z0-9+#]+)*")


@dataclass
class VerifyReport:
    ok: bool
    problems: list[str] = field(default_factory=list)
    novel_numbers: list[str] = field(default_factory=list)
    novel_entities: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            return "通过"
        return "；".join(self.problems[:6])


# ---------------------------------------------------------------------------
# 从母简历建白名单
# ---------------------------------------------------------------------------

def _walk_strings(node: Any) -> Iterable[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _walk_strings(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _walk_strings(v)


def _norm_num(raw: str) -> str:
    return raw.strip().replace(" ", "").replace("	", "").replace(",", "").rstrip(".")


def numbers_in(text: str) -> set[str]:
    return {_norm_num(m.group()) for m in _NUM.finditer(text or "")}


def entities_in(text: str) -> set[str]:
    return {m.group() for m in _ENTITY.finditer(text or "")}


def build_whitelist(master: dict[str, Any]) -> tuple[set[str], set[str]]:
    """母简历里出现过的全部数字和专名。

    刻意做成**全局**白名单而不是逐条对应：某个数字只要在母简历任何地方出现过，
    就允许它出现在简历任何位置。这样宽松一点，但仍然能挡住「凭空多出来的数字」——
    而那才是真正会在面试里翻车的东西。
    """
    nums: set[str] = set()
    ents: set[str] = set()
    for text in _walk_strings(master):
        nums |= numbers_in(text)
        ents |= entities_in(text)
    return nums, ents


def bullet_index(master: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """bullet id → bullet。渲染器只认这张表，模型给不了它没有的东西。"""
    out: dict[str, dict[str, Any]] = {}
    for kind in ("experiences", "projects"):
        for entry in master.get(kind) or []:
            for b in entry.get("bullets") or []:
                if b.get("id"):
                    out[b["id"]] = {**b, "_owner": entry.get("id"), "_kind": kind}
    return out


# ---------------------------------------------------------------------------
# 一、选材完整性（默认路径）
# ---------------------------------------------------------------------------

def verify_selection(selected_ids: list[str], master: dict[str, Any]) -> VerifyReport:
    """选中的 bullet id 必须真实存在，且不重复。

    模型幻觉出一个不存在的 id 时**必须报错，不能静默丢弃**——
    静默丢弃会让简历悄悄少一条，你到面试时才发现对方手里那份跟你以为的不一样。
    """
    index = bullet_index(master)
    problems: list[str] = []

    unknown = [i for i in selected_ids if i not in index]
    if unknown:
        problems.append(f"选中了不存在的 bullet id：{unknown}")

    seen: set[str] = set()
    dupes = {i for i in selected_ids if i in seen or seen.add(i)}  # type: ignore[func-returns-value]
    if dupes:
        problems.append(f"重复选中：{sorted(dupes)}")

    if not selected_ids:
        problems.append("一条 bullet 都没选中")

    return VerifyReport(ok=not problems, problems=problems)


# ---------------------------------------------------------------------------
# 二、最终闸门：渲染结果不得含母简历里没有的事实
# ---------------------------------------------------------------------------

def verify_rendered(
    rendered_text: str,
    master: dict[str, Any],
    *,
    extra_allowed: Iterable[str] = (),
) -> VerifyReport:
    """对**渲染出来的成品**做最后一道差集检查。

    这一道是 belt-and-braces：即使选材是对的，模板 bug 或后续改动也可能
    往文档里塞进母简历没有的东西。产出物才是投出去的东西，所以查产出物。
    """
    allow_nums, allow_ents = build_whitelist(master)
    allow_ents = allow_ents | TEMPLATE_CHROME | set(extra_allowed)

    novel_nums = sorted(numbers_in(rendered_text) - allow_nums)
    novel_ents = sorted(entities_in(rendered_text) - allow_ents)

    problems: list[str] = []
    if novel_nums:
        problems.append(f"出现了母简历里没有的数字：{novel_nums[:8]}")
    if novel_ents:
        problems.append(f"出现了母简历里没有的专名/技术词：{novel_ents[:8]}")

    return VerifyReport(
        ok=not problems, problems=problems,
        novel_numbers=novel_nums, novel_entities=novel_ents,
    )


# ---------------------------------------------------------------------------
# 三、改写校验（第二步，默认关）
# ---------------------------------------------------------------------------

def verify_rewrite(original: str, rewritten: str, master: dict[str, Any]) -> VerifyReport:
    """改写后不得新增任何数字、专名或技术词。

    「宁可误杀」：同义改写只该动动词和句式，一旦冒出新实体就拒绝。
    允许的上界是**这条 bullet 的原文 + 整份母简历**——
    比如把技术名从别处搬过来是允许的，凭空造一个不行。
    """
    allow_nums, allow_ents = build_whitelist(master)
    allow_nums |= numbers_in(original)
    allow_ents |= entities_in(original) | TEMPLATE_CHROME

    novel_nums = sorted(numbers_in(rewritten) - allow_nums)
    novel_ents = sorted(entities_in(rewritten) - allow_ents)

    problems: list[str] = []
    if novel_nums:
        problems.append(f"改写新增了数字：{novel_nums}")
    if novel_ents:
        problems.append(f"改写新增了专名/技术词：{novel_ents}")

    # 数字被改掉也要拦：19.77 变成 19.7 不是「新增」，但同样是编造
    lost = sorted(numbers_in(original) - numbers_in(rewritten))
    if lost:
        problems.append(f"改写丢失了原有数字（可能被篡改）：{lost}")

    return VerifyReport(
        ok=not problems, problems=problems,
        novel_numbers=novel_nums, novel_entities=novel_ents,
    )
