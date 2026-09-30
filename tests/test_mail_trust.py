"""发件方可信不可信、拒信要不要复核。

实测队列里 111 封有 95 封卡在「发件方不可信」：多半是公司自己的域名（公司不在你的列表里），
或者名单里漏了的招聘系统。拒信那 7 封命中的「邀请信号」基本是拒信的套话。
"""

import pytest

from jha import db
from jha.mail import classify, pipeline, policy, prefilter
from jha.mail.imap import parse_message
from mailfix import FakeReader, eml


# ---------------------------------------------------------------------------
# 名单：实测漏掉的招聘系统、求职平台
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sender", [
    "mail.paylocity.com", "m.personio.de", "notifications.ukg.net", "successfactors.eu",
    "workflow.email.us-ashburn-1.ocs.oraclecloud.com", "hackerrankforwork.com", "coderpad.io",
    "terros.na.teamtailor-mail.com", "applytojob.com",
])
def test_recruiting_platforms_seen_in_the_queue_are_recognized(sender):
    res = prefilter.screen({"from_domain": sender, "subject": "hello"}, {})
    assert res.passed and res.channel == "ats"


def test_work_at_a_startup_is_a_job_board():
    res = prefilter.screen({"from_domain": "ycombinator.com", "subject": "Thanks for applying to PostHog"}, {})
    assert res.channel == "board"


# ---------------------------------------------------------------------------
# 发件域名是不是这家公司的
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sender, company", [
    ("deloitte.com", "Deloitte"),
    ("careerhub.newyorklife.com", "New York Life"),
    ("email.roblox.com", "Roblox"),
    ("recruitment.americanexpress.com", "American Express"),
    ("workday.cigna.com", "The Cigna Group"),
    ("mckinsey.com", "McKinsey & Company"),
    ("copperstate.com", "Copper State Bolt & Nut Co."),
    ("careers.tiktokusds.com", "TikTok USDS JV"),
    ("salk.edu", "Salk Institute for Biological Studies"),
])
def test_company_own_domains_are_recognized(sender, company):
    assert prefilter.domain_belongs_to(sender, company)


@pytest.mark.parametrize("sender, company", [
    ("stripe-careers.com", "Stripe"),       # 冒充：公司名后面加 careers
    ("stripecareers.com", "Stripe"),
    ("stripe.com.evil.io", "Stripe"),     # 子域名谁都能起：只看注册名
    ("gmail.com", "Google"),
    ("careers-stripe.xyz", "Stripe"),
    ("bah.com", "Booz Allen"),             # 缩写对不上：宁可进队列
    ("co.uk", "Co"),
])
def test_lookalikes_and_unverifiable_domains_are_not(sender, company):
    assert not prefilter.domain_belongs_to(sender, company)


def test_second_level_suffixes_use_the_registered_name():
    assert prefilter.domain_belongs_to("careers.acme.co.uk", "Acme")


def test_known_gap_other_tlds_pass():
    """已知挡不住：换顶级域名的仿冒。代价只是多一条记录——面试照样推送，offer 照样人工确认。
    哪天要堵，这条测试会提醒你改的是有意为之的行为。"""
    assert prefilter.domain_belongs_to("stripe.xyz", "Stripe")


# ---------------------------------------------------------------------------
# 端到端
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    c.execute("""INSERT INTO companies (name, email_domains_json) VALUES ('Ramp', '["ramp.com"]')""")
    c.execute("INSERT INTO jobs (company_id, source, external_id, title) VALUES (1,'ashby','r1','Applied AI Engineer')")
    c.execute("INSERT INTO applications (job_id, applied_at) VALUES (1, '2026-09-01 10:00:00')")
    c.commit()
    yield c
    c.close()


class Fake:
    """分类和复核各返回各的，记下每次调用用的是哪个 schema、哪个模型。"""

    model = "claude-haiku-4-5"

    def __init__(self, cls, review=None, review_error=None):
        self.cls, self.review, self.review_error, self.calls = cls, review, review_error, []

    def structured(self, *, schema_name, **kw):
        self.calls.append((schema_name, kw.get("model")))
        if schema_name == "email_classification":
            return {"confidence": 0.95, "summary": "摘要", **self.cls}
        if self.review_error:
            raise self.review_error
        return dict(self.review)


def sweep(conn, client, **mail):
    n = conn.execute("SELECT COUNT(*) c FROM emails").fetchone()["c"]
    pipeline.ingest(conn, FakeReader([parse_message(eml(**mail), f"77:{n + 1}")]))
    return pipeline.process_pending(conn, client=client)


def test_confirmation_from_the_company_own_domain_is_recorded(conn):
    rep = sweep(conn, Fake({"type": "confirmation", "company": "Deloitte", "role_hint": "AI Engineer"}),
                frm="Deloitte Careers <careers@deloitte.com>", subject="Thank you for applying to Deloitte",
                body="Deloitte has received your application for AI Engineer.")
    assert rep.created == 1 and rep.queued == 0


def test_lookalike_domain_is_still_queued(conn):
    rep = sweep(conn, Fake({"type": "confirmation", "company": "Stripe", "role_hint": "AI Engineer"}),
                frm="Stripe <jobs@stripe-careers.com>", subject="Your application to Stripe",
                body="Stripe received your application for AI Engineer.")
    assert rep.created == 0 and rep.queued == 1


# ---------------------------------------------------------------------------
# 拒信复核
# ---------------------------------------------------------------------------

REJECTION = dict(frm="Ramp <recruiting@ramp.com>", subject="Your application to Ramp",
                 body="We invite you to apply for future roles. "
                      "Unfortunately, we will not be moving forward with your application at this time.")
IS_REJECTION = {"type": "rejection", "company": "Ramp", "role_hint": ""}


def status(conn):
    return conn.execute("SELECT status FROM applications WHERE id = 1").fetchone()["status"]


def test_review_clears_a_rejection_that_only_sounds_like_an_invite(conn):
    client = Fake(IS_REJECTION, review={
        "asks_for_next_step": False,
        "quote": "unfortunately, we will not be moving forward with your application at this time.",
    })
    rep = sweep(conn, client, **REJECTION)
    assert rep.auto_applied == 1 and status(conn) == "rejected"
    assert ("rejection_review", classify.REVIEW_MODEL) in client.calls, "复核要用更强的模型"
    reason = conn.execute("SELECT reason FROM emails").fetchone()["reason"]
    assert "复核排除" in reason


@pytest.mark.parametrize("review, error", [
    ({"asks_for_next_step": True, "quote": "We invite you to apply for future roles."}, None),
    ({"asks_for_next_step": False, "quote": "We would love to schedule your onsite."}, None),   # 原文里没有
    ({"quote": "Unfortunately, we will not be moving forward"}, None),                          # 没给结论
    (None, RuntimeError("overloaded")),
])
def test_anything_short_of_a_clear_no_stays_in_the_queue(conn, review, error):
    """复核说有、引用对不上原文、没给结论、调用出错——都当成可能是邀请。"""
    rep = sweep(conn, Fake(IS_REJECTION, review=review, review_error=error), **REJECTION)
    assert rep.queued == 1 and rep.auto_applied == 0 and status(conn) != "rejected"
    assert rep.alerts, "进队列的要推送，别让它悄悄躺着"


def test_plain_rejections_cost_no_review(conn):
    client = Fake(IS_REJECTION)
    sweep(conn, client, frm="Ramp <recruiting@ramp.com>", subject="Your application to Ramp",
          body="Unfortunately, we have decided to move forward with other candidates.")
    assert [s for s, _ in client.calls] == ["email_classification"]
    assert status(conn) == "rejected"


def test_policy_needs_the_review_to_clear_the_signal():
    kw = dict(ctype="rejection", confidence=0.95, match_status="exact", text="We invite you to apply again.")
    assert policy.decide(**kw).action == "queue"
    assert policy.decide(**kw, invite_cleared=True).action == "auto_apply"
