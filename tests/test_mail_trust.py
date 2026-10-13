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

    model = "claude-haiku-5-5"

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


# ---------------------------------------------------------------------------
# 推送里的链接（实测待办邮件里的域名）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url, sender, company, domains", [
    ("https://screen-ide.coderpad.io/x", "coderpad.io", "Abridge", ()),
    ("http://url3362.raydar.xyz/ls/click?upn=x", "raydar.xyz", "Raydar", ()),
    ("https://track.pstmrk.it/3s/abc", "hackerrankforwork.com", "Snowflake", ()),
    ("https://u21660302.ct.sendgrid.net/ls/click?x", "email.roblox.com", "Roblox", ()),
    ("https://app.wellsuited.com/assess", "myworkday.com", "Houlihan Lokey", ()),
    ("https://careers.snowflake.com/x", "ashbyhq.com", "Snowflake", ()),
    ("https://hl.com/careers", "myworkday.com", "Houlihan Lokey", ("hl.com",)),
])
def test_links_on_recruiting_platforms_or_the_company_own_domain_pass(url, sender, company, domains):
    assert prefilter.link_allowed(url, sender_domain=sender, company=company, company_domains=domains)


@pytest.mark.parametrize("url, sender, company", [
    ("https://track.pstmrk.it/3s/abc", "evil.io", "Stripe"),          # 跳转链接：发件方不可信就不放
    ("https://stripe-login.com/verify", "stripe.com", "Stripe"),
    ("https://youtu.be/x", "akunacapital.com", "Akuna Capital"),
    ("https://akunacapital.teachable.com/x", "akunacapital.com", "Akuna Capital"),   # 别家平台上的子域名
    ("javascript:alert(1)", "hackerrank.com", "Snowflake"),
    ("https://www.hackerrank.com/unsubscribe?u=1", "hackerrank.com", "Snowflake"),
])
def test_other_links_do_not(url, sender, company):
    assert not prefilter.link_allowed(url, sender_domain=sender, company=company)


# ---------------------------------------------------------------------------
# 实测漏掉的：HPE 从没登记过的 jobalerts.careers.hpe.com 发来筛选邀请，主题里没有关键词表里的词
# ---------------------------------------------------------------------------

HPE = {"from_domain": "jobalerts.careers.hpe.com",
       "from_addr": "Hewlett Packard Enterprise <recruiting@jobalerts.careers.hpe.com>"}


def test_screening_invites_pass_the_prefilter():
    res = prefilter.screen({**HPE, "subject": "Congratulations! You are invited to our screening process"}, {})
    assert res.passed


def test_mail_from_a_company_you_applied_to_passes_even_without_keywords():
    mail = {**HPE, "subject": "A quick update from our team"}
    assert not prefilter.screen(mail, {}).passed
    res = prefilter.screen(mail, {}, ["Hewlett Packard Enterprise"])
    assert res.passed and res.channel == "named"
    assert res.channel not in pipeline.TRUSTED_CHANNELS, "显示名谁都能写：只换来一次分类，不算可信"


def test_only_companies_with_applications_count(conn):
    conn.execute("INSERT INTO companies (name) VALUES ('Hewlett Packard Enterprise')")   # 只在目标列表里，没投过
    conn.commit()
    assert prefilter.applied_company_names(conn) == ["Ramp"]


def test_rescreen_brings_back_mail_the_old_rules_dropped(conn):
    conn.execute("INSERT INTO companies (name) VALUES ('Hewlett Packard Enterprise')")
    conn.execute("INSERT INTO jobs (company_id, source, external_id, title) VALUES (2, 'manual', 'h', 'AI Engineer')")
    conn.execute("INSERT INTO applications (job_id) VALUES (2)")
    for uid, subject in (("a", "A quick update from our team"), ("b", "Weekly newsletter")):
        frm = HPE["from_addr"] if uid == "a" else "News <news@somewhere.com>"
        conn.execute("INSERT INTO emails (uid, from_addr, from_domain, subject, policy, reason) "
                     "VALUES (?, ?, ?, ?, 'filtered', 'old')", (uid, frm, frm.split("@")[1].rstrip(">"), subject))
    conn.commit()
    assert pipeline.rescreen_filtered(conn) == 1
    assert [r[0] for r in conn.execute("SELECT uid FROM emails WHERE policy IS NULL")] == ["a"]


def test_the_hpe_screening_invite_lands_on_the_application(conn):
    conn.execute("INSERT INTO companies (name) VALUES ('Hewlett Packard Enterprise')")
    conn.execute("INSERT INTO jobs (company_id, source, external_id, title) VALUES (2, 'email', 'h', 'AI Engineer')")
    conn.execute("INSERT INTO applications (job_id) VALUES (2)")
    conn.commit()
    rep = sweep(conn, Fake({"type": "interview_invite", "company": "Hewlett Packard Enterprise"}),
                frm=HPE["from_addr"], subject="Congratulations! You are invited to our screening process",
                body="Hewlett Packard Enterprise invites you to complete a screening.")
    assert rep.auto_applied == 1 and rep.alerts
    assert conn.execute("SELECT status FROM applications WHERE id = 2").fetchone()["status"] == "interview_loop"


def test_long_tracking_links_are_not_refused_for_their_length():
    """实测 HPE 的 SendGrid 跟踪链接 1323 个字符，原来 800 的上限把它挡掉了。"""
    url = "https://u26642440.ct.sendgrid.net/ls/click?upn=" + "a" * 1270
    assert prefilter.link_allowed(url, sender_domain="jobalerts.careers.hpe.com", company="Hewlett Packard Enterprise")
    assert not prefilter.link_allowed(url + "a" * 700, sender_domain="jobalerts.careers.hpe.com",
                                      company="Hewlett Packard Enterprise"), "超过一条推送放得下的长度"
