"""Phase 4：申请表自定义问题的起草。

路线图 Phase 4「注意」里的硬规矩：

    绝不自动回答工作授权、是否需要签证、犯罪记录、EEO 等法律相关问题——
    预填也只填 qa_bank 里你亲自写好的固定答案。

我把它拆成了**三档**，因为「法律相关」这四个字里其实混了两类性质不同的问题：

    VERBATIM  工作授权、签证、薪资期望、到岗时间
              → 只回 qa_bank 里你亲手写的原文，一个字不改，永不过 LLM
              这些是事实陈述，你已经写好了，照抄即可

    NEVER     EEO 自愿披露：种族、性别、退伍军人身份、残障状况
              → **一个字都不填**，连 qa_bank 都不查
              这些是自愿披露项，只能你本人决定填不填、怎么填。
              agent 碰它没有任何正当理由

    DRAFT     其余开放题（为什么想来、最有挑战的项目……）
              → 可以从 qa_bank / story_bank 的材料起草，但一律标成待审

分档是**确定性**的（正则匹配），不问 LLM——判断"这是不是 EEO 问题"
本身就不该交给一个可能判错的组件（§0「能确定性判定的不交给模型」）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import profile


class Kind:
    VERBATIM = "verbatim"     # 只回 qa_bank 原文
    NEVER = "never"           # 一个字都不填
    DRAFT = "draft"           # 可起草，需人工审
    UNCOVERED = "uncovered"   # 材料里没有，留空标红


#: EEO 自愿披露。命中就完全不碰。
#:
#: 注意这里的 `\w*`：要做**前缀匹配**的词后面绝不能加 `\b`。
#: `\bdisabilit\b` 匹配不上 "disability"（后面是 y，还是单词字符），
#: 于是残障问题会掉进 UNCOVERED 而不是 NEVER。现在两档都是留空，
#: 但分类错了——将来若给 UNCOVERED 加上起草能力，这题就会被起草。
#: 词边界这东西两个方向都能咬人：太松会误伤（India 匹配 Indianapolis），
#: 太紧会漏网（disabilit 漏掉 disability）。
_NEVER = re.compile(
    r"(?i)(\brace\b|\bethnic\w*|\bgender\b|\bsexual orientation\b|\bveteran\w*|"
    r"\bdisabilit\w*|\bdisabled\b|\bpronoun\w*|\bEEO\b|\bequal employment\b|"
    r"\bself[- ]?identif\w*|\bnational origin\b|\bmarital status\b)"
)

#: 犯罪记录 / 背景调查。属于法律敏感，也不代填。
_NEVER_LEGAL = re.compile(
    r"(?i)(\bcriminal\w*|\bconvict\w*|\bfelon\w*|\bmisdemeanor\w*|"
    r"\bbackground check\b|\bdrug (test|screen)\w*)"
)

#: 问题模式 → qa_bank 的键。命中就照抄原文。
_VERBATIM: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(require|need).{0,30}sponsor|sponsorship"), "sponsorship_required"),
    (re.compile(r"(?i)(legally )?authoriz(ed|ation) to work|work authorization|"
                r"right to work|eligible to work"), "work_authorization"),
    (re.compile(r"(?i)visa status|immigration status|work permit"), "work_authorization"),
    (re.compile(r"(?i)salary|compensation expectation|desired pay|pay expectation"),
     "salary_expectation"),
    (re.compile(r"(?i)notice period|when can you start|start date|availability"),
     "notice_period"),
    (re.compile(r"(?i)relocat|willing to move"), "relocation"),
)

#: "Why this company" 单独拎出来：它出现在很大一部分 Greenhouse / Lever 表单上，
#: 是真正的每份申请时间成本所在——比表单预填重要得多（路线图 Phase 4）。
_WHY_COMPANY = re.compile(
    r"(?i)why (do you want to |are you interested in )?(work|join|us|our|this|"
    r"[a-z]+\?)|what (draws|attracts|excites) you"
)


@dataclass
class Answer:
    question: str
    kind: str
    text: str = ""
    source: str = ""
    needs_review: bool = False
    note: str = ""

    @property
    def filled(self) -> bool:
        return bool(self.text.strip())

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question[:160], "kind": self.kind,
            "answer": self.text, "source": self.source,
            "needs_review": self.needs_review, "note": self.note,
        }


def classify(question: str) -> tuple[str, str | None]:
    """给一个问题分档。返回 (档位, qa_bank 键)。"""
    q = question or ""
    if _NEVER.search(q) or _NEVER_LEGAL.search(q):
        return Kind.NEVER, None
    for pattern, key in _VERBATIM:
        if pattern.search(q):
            return Kind.VERBATIM, key
    if _WHY_COMPANY.search(q):
        return Kind.DRAFT, "why_company_template"
    return Kind.DRAFT, None


def answer_one(
    question: str, qa_bank: dict[str, Any], *, company: str = "", role: str = ""
) -> Answer:
    kind, key = classify(question)

    if kind == Kind.NEVER:
        return Answer(question, Kind.NEVER, note=(
            "自愿披露 / 法律敏感项 —— agent 不填。只有你本人能决定填不填、怎么填"
        ))

    if kind == Kind.VERBATIM:
        text = (qa_bank.get(key) or "").strip() if key else ""
        if not text:
            return Answer(question, Kind.UNCOVERED, source=f"qa_bank.{key}", note=(
                f"qa_bank.{key} 是空的 —— 这题必须你亲自填，agent 不会替你编"
            ))
        return Answer(question, Kind.VERBATIM, text=text, source=f"qa_bank.{key}",
                      note="照抄你写好的原文，一个字没改")

    # DRAFT
    if key == "why_company_template":
        tmpl = (qa_bank.get("why_company_template") or "").strip()
        if not tmpl:
            return Answer(question, Kind.UNCOVERED, source="qa_bank.why_company_template",
                          note="这题值得单独花力气写好——它出现在很大一部分表单上")
        text = tmpl.replace("{company}", company or "{company}").replace(
            "{role}", role or "{role}")
        unfilled = "{" in text
        return Answer(
            question, Kind.DRAFT, text=text, source="qa_bank.why_company_template",
            needs_review=True,
            note=("模板里还有没替换的占位符，必须补完再投" if unfilled
                  else "模板套出来的——**通用的「为什么想来」比不写还差**，投之前改成这家公司特有的理由"),
        )

    return Answer(question, Kind.UNCOVERED, note=(
        "qa_bank 里没有覆盖这题。agent 不会凭空起草——留空由你填"
    ))


def answer_all(
    questions: list[str], *, company: str = "", role: str = "",
    master: dict[str, Any] | None = None,
) -> list[Answer]:
    qa = (master or profile.load_master_profile()).get("qa_bank") or {}
    return [answer_one(q, qa, company=company, role=role) for q in questions]


def summarize(answers: list[Answer]) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    for a in answers:
        by_kind[a.kind] = by_kind.get(a.kind, 0) + 1
    todo = [a for a in answers if not a.filled or a.needs_review]
    return {
        "total": len(answers),
        "by_kind": by_kind,
        "ready_to_paste": sum(1 for a in answers if a.filled and not a.needs_review),
        "needs_you": len(todo),
        "answers": [a.as_dict() for a in answers],
    }
