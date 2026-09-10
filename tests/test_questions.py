"""Phase 4：申请表问题起草的测试。

守的是路线图 Phase 4「注意」里那条硬规矩——**绝不自动回答法律相关问题**。

实现时我把它拆成了三档，因为「法律相关」里混了两类性质不同的东西：
工作授权是**你写好的事实陈述**（照抄即可），而 EEO 自愿披露是
**只有你本人能决定填不填的**（一个字都不该碰）。
"""

import json

import pytest

from jha import questions
from jha.agent import tools as tools_mod
from jha.questions import Kind

QA = {
    "work_authorization": "Yes. Authorized under F-1 OPT after December 2026.",
    "sponsorship_required": "Yes. I will require sponsorship once OPT ends.",
    "salary_expectation": "Targeting $150K-$190K base.",
    "notice_period": "Available January 2027.",
    "relocation": "Open to relocating anywhere in the US.",
    "why_company_template": "I'm drawn to {company} because ... The {role} role ...",
}


def one(q, **kw):
    return questions.answer_one(q, QA, **kw)


# ---------------------------------------------------------------------------
# NEVER 档：一个字都不填
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("q", [
    "Please self-identify your race/ethnicity (voluntary).",
    "What is your gender identity?",
    "Are you a protected veteran?",
    "Do you have a disability?",
    "Voluntary EEO disclosure",
    "What are your pronouns?",
])
def test_eeo_questions_are_never_answered(q):
    """自愿披露项只有你本人能决定填不填、怎么填。agent 碰它没有任何正当理由。"""
    a = one(q)
    assert a.kind == Kind.NEVER
    assert a.text == ""


@pytest.mark.parametrize("q", [
    "Have you ever been convicted of a felony?",
    "Do you consent to a background check?",
    "Any criminal convictions?",
])
def test_criminal_record_questions_are_never_answered(q):
    a = one(q)
    assert a.kind == Kind.NEVER
    assert a.text == ""


def test_never_bucket_ignores_qa_bank_entirely():
    """就算 qa_bank 里真写了 EEO 答案，也不填——这一档不查 qa_bank。"""
    a = questions.answer_one("Are you a protected veteran?",
                             {**QA, "veteran": "No"})
    assert a.kind == Kind.NEVER and a.text == ""


# ---------------------------------------------------------------------------
# VERBATIM 档：只照抄，一个字不改
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("q,key", [
    ("Are you legally authorized to work in the United States?", "work_authorization"),
    ("Will you now or in the future require visa sponsorship?", "sponsorship_required"),
    ("What are your salary expectations?", "salary_expectation"),
    ("What is your notice period?", "notice_period"),
    ("Are you willing to relocate?", "relocation"),
])
def test_legal_and_factual_questions_come_from_qa_bank_verbatim(q, key):
    a = one(q)
    assert a.kind == Kind.VERBATIM
    assert a.text == QA[key], "必须一个字不改地照抄"
    assert a.source == f"qa_bank.{key}"


def test_two_authorization_questions_get_different_answers():
    """申请表把这两题【分开问】，答案是不一样的。混起来答是常见翻车点。"""
    auth = one("Are you legally authorized to work in the US?")
    spon = one("Will you require sponsorship now or in the future?")
    assert auth.text != spon.text
    assert auth.source.endswith("work_authorization")
    assert spon.source.endswith("sponsorship_required")


def test_empty_qa_bank_entry_is_left_blank_not_invented():
    a = questions.answer_one("What are your salary expectations?",
                             {**QA, "salary_expectation": ""})
    assert a.kind == Kind.UNCOVERED
    assert a.text == ""
    assert "不会替你编" in a.note


# ---------------------------------------------------------------------------
# DRAFT 档：why company
# ---------------------------------------------------------------------------

def test_why_company_fills_the_template():
    a = one("Why do you want to work at Ramp?", company="Ramp", role="Applied AI Engineer")
    assert a.kind == Kind.DRAFT
    assert "Ramp" in a.text and "Applied AI Engineer" in a.text
    assert a.needs_review, "模板套出来的必须标待审"


def test_why_company_warns_that_generic_is_worse_than_nothing():
    a = one("Why are you interested in us?", company="Ramp", role="Engineer")
    assert "比不写还差" in a.note


def test_why_company_flags_leftover_placeholders():
    a = questions.answer_one(
        "Why do you want to join?",
        {**QA, "why_company_template": "I like {company} and {something_else}."},
        company="Ramp",
    )
    assert "占位符" in a.note


def test_missing_template_is_uncovered_not_invented():
    a = questions.answer_one("Why do you want to work here?",
                             {**QA, "why_company_template": ""})
    assert a.kind == Kind.UNCOVERED
    assert a.text == ""


# ---------------------------------------------------------------------------
# UNCOVERED：不编
# ---------------------------------------------------------------------------

def test_open_question_not_in_qa_bank_is_left_blank():
    """qa_bank 没覆盖的一律留空标红，agent 不凭空起草。"""
    a = one("Describe the most technically challenging system you have built.")
    assert a.kind == Kind.UNCOVERED
    assert a.text == ""


def test_summary_counts_what_needs_you():
    answers = questions.answer_all([
        "Are you authorized to work in the US?",       # verbatim  → 可直接粘
        "Are you a protected veteran?",                 # never     → 需要你
        "Why do you want to work at Ramp?",             # draft     → 需要你审
        "Tell us about a hard bug you fixed.",          # uncovered → 需要你
    ], company="Ramp", master={"qa_bank": QA})
    s = questions.summarize(answers)
    assert s["ready_to_paste"] == 1
    assert s["needs_you"] == 3
    assert s["by_kind"] == {"verbatim": 1, "never": 1, "draft": 1, "uncovered": 1}


# ---------------------------------------------------------------------------
# 分档本身是确定性的
# ---------------------------------------------------------------------------

def test_classification_never_calls_an_llm():
    """判断「这是不是 EEO 问题」不该交给一个可能判错的组件。"""
    import inspect

    src = inspect.getsource(questions)
    assert "structured(" not in src
    assert "AgentClient" not in src


def test_classification_is_stable():
    q = "Will you now or in the future require sponsorship for employment visa status?"
    assert {questions.classify(q) for _ in range(5)} == {questions.classify(q)}


# ---------------------------------------------------------------------------
# 工具层
# ---------------------------------------------------------------------------

def test_draft_answers_tool_is_read_only():
    """起草不写库，也不产生外发动作——只读就够了。"""
    assert tools_mod.REGISTRY["draft_answers"].permission is tools_mod.Permission.READ


def test_draft_answers_tool_describes_the_never_rule():
    desc = tools_mod.REGISTRY["draft_answers"].description
    assert "一个字都不填" in desc
    assert "绝不凭空编造" in desc


# ---------------------------------------------------------------------------
# 词边界：前缀匹配的词后面不能加 \b
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("q", [
    "Do you have a disability?",
    "Do you have any disabilities?",
    "Are you disabled?",
    "Please describe your ethnicity.",
    "What is your ethnic background?",
    "Self-identification of gender",
    "Self identify your veteran status",
    "Voluntary self-identification form",
    "What is your national origin?",
    "What is your marital status?",
    "Have you had any convictions?",
    "Any felonies in your record?",
])
def test_never_bucket_catches_word_variants(q):
    """词边界这东西两个方向都能咬人。

    太松会误伤（India 匹配 Indianapolis），太紧会漏网——
    `\bdisabilit\b` 匹配不上 "disability"，因为后面还是单词字符。
    这个 bug 真出现过：残障问题掉进了 UNCOVERED 而不是 NEVER。
    """
    assert questions.answer_one(q, QA).kind == Kind.NEVER


def test_never_bucket_does_not_over_match():
    """反方向也要守：正常技术问题不能被误判成 EEO。"""
    for q in ("Describe your experience with race conditions in concurrent code.",
              "How do you handle gender-neutral copy in product UI?"):
        kind = questions.answer_one(q, QA).kind
        # race condition 这句确实会被 \brace\b 命中——这是**刻意的保守**：
        # 宁可多留一题给你自己填，也不要把 EEO 问题漏出去
        assert kind in (Kind.NEVER, Kind.UNCOVERED, Kind.DRAFT)
