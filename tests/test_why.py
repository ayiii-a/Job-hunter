"""「为什么想来」按岗位起草：材料以你的备注为主，数字和专名只能来自材料，没过校验就退回模板。"""

import pytest

from jha import why
from jha.agent.client import Budget, MissingAPIKey
from jha.questions import Kind
from test_tailor import MASTER as BASE, conn  # noqa: F401  （conn 是 fixture）

MASTER = {**BASE, "qa_bank": {
    "why_company_template": "I want to join {company} because {why}. The {role} role fits my work.",
}}
Q = "Why are you excited to join us at Acme?"


class Fake:
    model = "claude-sonnet-5"

    def __init__(self, answer="", exc=None):
        self.answer, self.exc, self.users = answer, exc, []

    def structured(self, *, user, **kw):
        self.users.append(user)
        if self.exc:
            raise self.exc
        return {"answer": self.answer}


@pytest.fixture
def note(monkeypatch):
    def set_note(text):
        monkeypatch.setattr(why, "company_note", lambda company: text)
    set_note("")
    return set_note


def test_draft_is_built_from_your_note_and_marked_for_review(conn, note):
    note("Acme ships PyTorch inference tooling for hospitals.")
    client = Fake("I want to join Acme because it ships PyTorch inference tooling. "
                  "I built a PyTorch inference pipeline.")
    a = why.draft(conn, 1, Q, master=MASTER, client=client, budget=Budget())
    assert a.kind == Kind.DRAFT and a.needs_review
    assert a.text == client.answer
    assert "Acme ships PyTorch inference tooling" in client.users[0]
    assert "<untrusted-job-description>" in client.users[0], "JD 是不可信输入，要围起来"


@pytest.mark.parametrize("bad, reason", [
    ("I want to join Acme because of its Kubernetes platform.", "Kubernetes"),
    ("Acme grew revenue 300% last year, and I built a PyTorch inference pipeline.", "300"),
    ("I want to join {company} because it is great.", "留着占位符"),
    ("I built things. " * 60, "超过"),
])
def test_unverified_draft_falls_back_to_the_template(conn, note, bad, reason):
    a = why.draft(conn, 1, Q, master=MASTER, client=Fake(bad), budget=Budget())
    assert a.text.startswith("I want to join Acme because {why}"), "没过校验的文本不给你看"
    assert reason in a.note and "退回模板" in a.note


def test_missing_api_key_falls_back_to_the_template(conn, note):
    a = why.draft(conn, 1, Q, master=MASTER, client=Fake(exc=MissingAPIKey("no key")), budget=Budget())
    assert "{why}" in a.text and "ANTHROPIC_API_KEY" in a.note


def test_no_material_means_no_model_call(conn, note):
    """没有这家公司的任何材料，起草出来只会是通用话——不花这个钱。"""
    conn.execute("UPDATE jobs SET jd_text = '' WHERE id = 1")
    conn.commit()
    client = Fake("anything")
    a = why.draft(conn, 1, Q, master=MASTER, client=client, budget=Budget())
    assert client.users == [] and "{why}" in a.text


def test_sentence_words_are_not_mistaken_for_invented_names():
    assert why.check("At Acme, I would build what the team ships.", MASTER, ["Acme"]) is None


def test_company_note_comes_from_companies_yaml(tmp_path, monkeypatch):
    path = tmp_path / "companies.yaml"
    path.write_text("companies:\n  - name: Acme\n    why_note: Ships inference tooling.\n", encoding="utf-8")
    monkeypatch.setattr(why.config, "COMPANIES_PATH", path)
    assert why.company_note("acme") == "Ships inference tooling."
    assert why.company_note("Other") == ""
