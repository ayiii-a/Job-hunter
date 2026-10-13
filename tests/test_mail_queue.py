"""什么都不悄悄丢、没见过的公司域名怎么处理、队列的批量操作。

打算丢掉的邮件（预过滤判无关、分类器判无关、招聘方主动联系）都进队列，你确认了才真的丢。
没见过的公司域名：验过 DMARC、对得上公司名或首字母缩写，放行后自动记下来；可疑的进队列。
"""

import json

import pytest

from jha import cli, db
from jha.mail import pipeline, prefilter
from jha.mail.imap import parse_message
from mailfix import FakeReader, eml


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    c.execute("""INSERT INTO companies (name, email_domains_json) VALUES ('Stripe', '["stripe.com"]')""")
    c.execute("INSERT INTO companies (name) VALUES ('Hewlett Packard Enterprise')")
    c.execute("INSERT INTO jobs (company_id, source, external_id, title) VALUES (1, 'manual', 's', 'AI Engineer')")
    c.execute("INSERT INTO jobs (company_id, source, external_id, title) VALUES (2, 'manual', 'h', 'ML Engineer')")
    c.execute("INSERT INTO applications (job_id) VALUES (1)")
    c.execute("INSERT INTO applications (job_id) VALUES (2)")
    c.commit()
    yield c
    c.close()


class Fake:
    model = "claude-haiku-5-5"

    def __init__(self, **cls):
        self.cls = cls

    def structured(self, **kw):
        return {"confidence": 0.95, "summary": "摘要", **self.cls}


def sweep(conn, client, **mail):
    n = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    pipeline.ingest(conn, FakeReader([parse_message(eml(**mail), f"77:{n + 1}")]))
    return pipeline.process_pending(conn, client=client)


def email(conn, i=1):
    return conn.execute("SELECT * FROM emails WHERE id = ?", (i,)).fetchone()


# ---------------------------------------------------------------------------
# 不悄悄丢
# ---------------------------------------------------------------------------

def test_prefilter_drops_wait_for_you(conn):
    rep = sweep(conn, Fake(type="other"), frm="News <news@somewhere.com>", subject="Weekly digest")
    assert rep.filtered == 1 and rep.classified == 0, "预过滤照样不花钱"
    assert (email(conn)["policy"], email(conn)["review_status"]) == ("filtered", "pending")
    assert pipeline.drop_candidates(conn) == 1
    assert pipeline.queue_for_agent(conn) == [], "给 agent 的队列只放要判断的，打算丢掉的只报个数"
    assert not rep.alerts


@pytest.mark.parametrize("ctype, policy", [("other", "ignore"), ("recruiter_outreach", "record_only")])
def test_unrelated_or_no_op_mail_waits_for_you(conn, ctype, policy):
    rep = sweep(conn, Fake(type=ctype), frm="Ann <ann@stripe.com>", subject="Your application")
    assert (email(conn)["policy"], email(conn)["review_status"]) == (policy, "pending")
    assert not rep.alerts


# ---------------------------------------------------------------------------
# 首字母缩写、没见过的域名
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sender, company, ok", [
    ("jobalerts.careers.hpe.com", "Hewlett Packard Enterprise", True),
    ("bah.com", "Booz Allen Hamilton", True),
    ("mit.edu", "Massachusetts Institute of Technology", True),     # of 不算
    ("ge.com", "General Electric", False),                         # 两个字母太短，撞名太多
    ("hpe.com", "Hewlett Packard", False),
])
def test_acronym_domains(sender, company, ok):
    assert prefilter.domain_belongs_to(sender, company) is ok


HPE = dict(frm="Hewlett Packard Enterprise <recruiting@jobalerts.careers.hpe.com>",
           subject="Congratulations! You are invited to our screening process",
           body="Hewlett Packard Enterprise invites you to a screening for ML Engineer.")


def test_a_verified_new_domain_is_recorded_after_it_passes(conn):
    rep = sweep(conn, Fake(type="oa_invite"), **HPE)
    assert rep.auto_applied == 1 and rep.learned == ["hpe.com"]
    res = prefilter.screen({"from_domain": "hpe.com", "subject": "hi"}, prefilter.company_domain_map(conn))
    assert res.channel == "company", "以后直接认得"
    # HPE 的测评链接走的是 SendGrid 跳转：发件方认得了，链接就能推
    assert prefilter.link_allowed("https://u266.ct.sendgrid.net/ls/click?x", sender_domain="jobalerts.careers.hpe.com",
                                  company="Hewlett Packard Enterprise")


@pytest.mark.parametrize("frm, dmarc, why", [
    ("Stripe <jobs@stripe.xyz>", "pass", "后缀不同"),                 # stripe.com 登记过
    ("Stripe Recruiting <jobs@talentstripe.com>", "pass", "对不上"),
    ("Hewlett Packard Enterprise <recruiting@hpe.com>", "none", "DMARC"),
])
def test_suspicious_new_domains_wait_for_you(conn, frm, dmarc, why):
    company = "Stripe" if "Stripe" in frm else "Hewlett Packard Enterprise"
    rep = sweep(conn, Fake(type="interview_invite"), frm=frm, subject=f"Interview with {company}",
                body=f"{company} would like to interview you.", dmarc=dmarc)
    assert rep.auto_applied == 0 and rep.queued == 1 and rep.learned == []
    assert rep.alerts, "面试邀请照样推送，只是先不记"
    assert why in email(conn)["reason"]
    assert "stripe.xyz" not in email(conn)["reason"], "理由里不写邮件里的字"


def test_confirming_a_suspicious_domain_teaches_it(conn):
    sweep(conn, Fake(type="interview_invite"), frm="Stripe Recruiting <jobs@talentstripe.com>",
          subject="Interview with Stripe", body="Stripe would like to interview you.")
    pipeline.accept(conn, 1)
    domains = db.load_json(conn.execute("SELECT verified_domains_json FROM companies WHERE id = 1").fetchone()[0])
    assert domains == ["talentstripe.com"]


def test_known_and_shared_domains_are_not_questioned(conn):
    assert sweep(conn, Fake(type="interview_invite"), frm="Stripe <jobs@stripe.com>",
                 subject="Interview", body="Stripe").auto_applied == 1
    assert sweep(conn, Fake(type="rejection"), frm="Stripe <no-reply@us.greenhouse-mail.io>",
                 subject="Your application to Stripe", body="Stripe: unfortunately not").auto_applied == 1


# ---------------------------------------------------------------------------
# 确认、改判、批量
# ---------------------------------------------------------------------------

def test_accepting_a_drop_candidate(conn):
    sweep(conn, Fake(type="other"), frm="Ann <ann@stripe.com>", subject="Your application", body="Stripe")
    with pytest.raises(ValueError, match="不认识"):
        pipeline.accept(conn, 1, as_type="nonsense")
    out = pipeline.accept(conn, 1, as_type="rejection")
    assert out["event"] == "rejected" and out["application_id"] == 1
    assert email(conn)["classification"] == "rejection" and "人工改判" in email(conn)["reason"]


def test_keeping_outreach_changes_nothing(conn):
    sweep(conn, Fake(type="recruiter_outreach"), frm="Ann <ann@stripe.com>", subject="Your application")
    assert pipeline.accept(conn, 1)["event"] is None
    assert email(conn)["review_status"] == "accepted"
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_releasing_filtered_mail_classifies_only_those(conn):
    sweep(conn, Fake(type="other"), frm="News <news@somewhere.com>", subject="Weekly digest")
    sweep(conn, Fake(type="other"), frm="Promo <p@shop.com>", subject="Sale")
    with pytest.raises(ValueError, match="还没分类过"):
        pipeline.accept(conn, 1)
    pipeline.release(conn, [1])
    rep = pipeline.process_pending(conn, client=Fake(type="other"), only=[1])
    assert rep.classified == 1 and rep.filtered == 0
    assert email(conn, 1)["policy"] == "ignore" and email(conn, 2)["policy"] == "filtered"


def test_select_by_kind_and_domain(conn):
    for frm in ("A <a@linkedin.com>", "B <b@mail.linkedin.com>", "C <c@shop.com>"):
        sweep(conn, Fake(type="other"), frm=frm, subject="Weekly")
    assert len(pipeline.select_pending(conn, kind="drops")) == 3
    assert [r["id"] for r in pipeline.select_pending(conn, kind="drops", domain="linkedin.com")] == [1, 2]
    assert pipeline.select_pending(conn, kind="decide") == []


def test_cli_batch_lists_first_and_needs_yes(conn, tmp_path, monkeypatch):
    dbfile = tmp_path / "jha.db"
    monkeypatch.setenv("JHA_DB_PATH", str(dbfile))
    c = db.connect(dbfile)
    db.init_db(c)
    for frm in ("A <a@linkedin.com>", "B <b@linkedin.com>", "C <c@shop.com>"):
        sweep(c, Fake(type="other"), frm=frm, subject="Weekly")
    c.close()

    assert cli.main(["mail", "dismiss", "--kind", "drops", "--domain", "linkedin.com"]) == 0
    c = db.connect(dbfile)
    assert c.execute("SELECT COUNT(*) FROM emails WHERE review_status = 'pending'").fetchone()[0] == 3, "没加 --yes 只列出来"
    c.close()

    assert cli.main(["mail", "dismiss", "--kind", "drops", "--domain", "linkedin.com", "--yes"]) == 0
    c = db.connect(dbfile)
    assert [r[0] for r in c.execute("SELECT review_status FROM emails ORDER BY id")] == ["dismissed", "dismissed", "pending"]
    c.close()
    assert cli.main(["mail", "dismiss"]) == 1, "什么都没给不执行"


def test_the_agent_still_cannot_accept_or_dismiss():
    from jha.agent import tools as tools_mod

    assert not [n for n in tools_mod.REGISTRY if "accept" in n or "dismiss" in n]
    out = json.loads(tools_mod.execute("list_email_queue", {}, _empty()))
    assert out["drop_candidates"] == 0


def _empty():
    c = db.connect(":memory:")
    db.init_db(c)
    return c
