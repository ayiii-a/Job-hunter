"""发件人验证（DMARC）和公司发件域名的自动登记。

From 头谁都能写。记下一个域名 = 以后这个域名的来信直接认作这家公司、算可信发件方，
所以只记收信服务器验过 DMARC、而且确实是这家公司自己的域名。
"""

import email
import email.policy

import pytest

from jha import db
from jha.mail import pipeline, prefilter
from jha.mail.imap import MailReader, parse_message, sender_auth
from mailfix import FakeImap, FakeReader, eml


def signed(raw: bytes, *results: str) -> bytes:
    """在信的最上面加 Authentication-Results（第一个参数在最上面，就是收信服务器写的那条）。"""
    return b"".join(f"Authentication-Results: {r}\n".encode() for r in results) + raw


def auth(frm: str, *results: str) -> str:
    msg = email.message_from_bytes(signed(eml(frm=frm, dmarc=None), *results), policy=email.policy.default)
    return sender_auth(msg, frm.rsplit("@", 1)[-1].rstrip(">"))


@pytest.mark.parametrize("frm, results, want", [
    ("Stripe <jobs@stripe.com>", ["mx.google.com; dkim=pass header.i=@stripe.com; dmarc=pass (p=REJECT) header.from=stripe.com"], "pass"),
    ("Roblox <x@email.roblox.com>", ["mx.google.com; dmarc=pass (p=NONE) header.from=email.roblox.com"], "pass"),
    ("Stripe <jobs@stripe.com>", ["mx.google.com; dmarc=fail (p=REJECT) header.from=stripe.com"], "fail"),
    ("Stripe <jobs@stripe.com>", [], "none"),
    # 验的不是 From 里这个域名
    ("Stripe <jobs@stripe.com>", ["mx.google.com; dmarc=pass header.from=evil.io"], "unaligned"),
    # 发件人自己塞的假头在下面，收信服务器写的在最上面
    ("Stripe <jobs@stripe.com>", ["mx.google.com; dmarc=fail header.from=stripe.com",
                                  "mx.google.com; dmarc=pass header.from=stripe.com"], "fail"),
    # 最上面那条不是我们的收信服务器写的
    ("Stripe <jobs@stripe.com>", ["evil.io; dmarc=pass header.from=stripe.com"], "untrusted"),
    # 信封发件人的本地部分能写进注释和引号里，冒充 dmarc=pass
    ("Stripe <jobs@stripe.com>", ['mx.google.com; spf=pass (google.com: domain of dmarc=pass@evil.io) '
                                  'smtp.mailfrom=evil.io; dmarc=fail header.from=stripe.com'], "fail"),
    ("Stripe <jobs@stripe.com>", ['mx.google.com; spf=pass smtp.mailfrom="x;dmarc=pass header.from=stripe.com"@evil.io'],
     "none"),
])
def test_sender_auth(frm, results, want):
    assert auth(frm, *results) == want


def test_parse_message_records_the_verdict():
    raw = signed(eml(frm="Stripe <jobs@stripe.com>", dmarc=None), "mx.google.com; dmarc=pass header.from=stripe.com")
    assert parse_message(raw, "77:1").dmarc == "pass"


# ---------------------------------------------------------------------------
# 学域名
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


class Fake:
    model = "claude-haiku-5-5"

    def __init__(self, **cls):
        self.cls = cls

    def structured(self, **kw):
        return {"confidence": 0.95, "summary": "收到申请", **self.cls}


PASS = "mx.google.com; dmarc=pass header.from={}"


def sweep(conn, client, *, frm, verdict=PASS, subject="Thank you for applying", body="We received your application."):
    domain = frm.rsplit("@", 1)[-1].rstrip(">")
    raw = eml(frm=frm, subject=subject, body=body, dmarc=None)
    if verdict:
        raw = signed(raw, verdict.format(domain))
    n = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    pipeline.ingest(conn, FakeReader([parse_message(raw, f"77:{n + 1}")]))
    return pipeline.process_pending(conn, client=client)


def verified(conn, name):
    return db.load_json(conn.execute("SELECT verified_domains_json FROM companies WHERE name = ?",
                                     (name,)).fetchone()[0])


DELOITTE = dict(frm="Deloitte Careers <careers@mail.deloitte.com>", body="Deloitte received your application for AI Engineer.")


def test_verified_company_domain_is_learned_and_trusted_next_time(conn):
    rep = sweep(conn, Fake(type="confirmation", company="Deloitte", role_hint="AI Engineer"), **DELOITTE)
    assert rep.created == 1 and rep.learned == ["deloitte.com"]
    assert verified(conn, "Deloitte") == ["deloitte.com"], "记注册域名，子域名都算它的"
    res = prefilter.screen({"from_domain": "talent.deloitte.com", "subject": "hello"}, prefilter.company_domain_map(conn))
    assert res.channel == "company", "以后这个域名的来信直接认作 Deloitte"


@pytest.mark.parametrize("verdict", [None, "mx.google.com; dmarc=fail header.from={}",
                                     "mx.google.com; dmarc=pass header.from=other.com"])
def test_nothing_is_learned_without_a_clean_dmarc_pass(conn, verdict):
    """没验过的新域名不直接建档、不记域名：进队列，你确认了才算。"""
    rep = sweep(conn, Fake(type="confirmation", company="Deloitte", role_hint="AI Engineer"), verdict=verdict, **DELOITTE)
    assert (rep.created, rep.queued) == (0, 1)
    assert rep.learned == []
    assert conn.execute("SELECT COUNT(*) FROM companies WHERE verified_domains_json != '[]'").fetchone()[0] == 0
    assert "DMARC" in conn.execute("SELECT reason FROM emails").fetchone()[0]


def test_shared_platform_domains_are_never_learned(conn):
    rep = sweep(conn, Fake(type="confirmation", company="Ramp", role_hint="AI Engineer"),
                frm="Ramp <no-reply@us.greenhouse-mail.io>", body="Ramp received your application for AI Engineer.")
    assert rep.created == 1 and rep.learned == []


def two_applications(conn, company):
    """同一家公司两条投递：信里没写岗位，分不出是哪条，进人工队列。"""
    conn.execute("INSERT INTO companies (name) VALUES (?)", (company,))
    for t in ("AI Engineer", "ML Engineer"):
        conn.execute("INSERT INTO jobs (company_id, source, external_id, title) VALUES (1, 'manual', ?, ?)", (t, t))
    conn.execute("INSERT INTO applications (job_id) VALUES (1)")
    conn.execute("INSERT INTO applications (job_id) VALUES (2)")
    conn.commit()


def test_human_confirmation_teaches_a_domain_whose_name_does_not_match(conn):
    two_applications(conn, "Booz Allen Hamilton")
    rep = sweep(conn, Fake(type="interview_invite", company="Booz Allen Hamilton"),
                frm="Recruiting <talent@bah.com>", subject="Interview with Booz Allen Hamilton",
                body="Booz Allen Hamilton would like to schedule an interview.")
    assert rep.queued == 1 and verified(conn, "Booz Allen Hamilton") == []
    pipeline.accept(conn, 1, application_id=1)
    assert verified(conn, "Booz Allen Hamilton") == ["bah.com"]


def test_a_personal_mailbox_is_not_learned_even_when_you_confirm(conn):
    two_applications(conn, "Tiny Startup")
    sweep(conn, Fake(type="interview_invite", company="Tiny Startup"), frm="Founder <founder@gmail.com>",
          subject="Interview at Tiny Startup", body="Tiny Startup would like to interview you.")
    pipeline.accept(conn, 1, application_id=1)
    assert verified(conn, "Tiny Startup") == []


def test_a_domain_already_owned_by_another_company_is_not_moved(conn):
    conn.execute("""INSERT INTO companies (name, email_domains_json) VALUES ('Capital One', '["capital.com"]')""")
    conn.commit()
    rep = sweep(conn, Fake(type="confirmation", company="Capital", role_hint="Analyst"),
                frm="Capital <jobs@capital.com>", body="Capital received your application for Analyst.")
    assert rep.learned == []


# ---------------------------------------------------------------------------
# 存量邮件补查
# ---------------------------------------------------------------------------

def test_verify_senders_backfills_old_mail_without_marking_it_read(conn):
    raw = signed(eml(**DELOITTE, dmarc=None), PASS.format("mail.deloitte.com"))
    rep = sweep(conn, Fake(type="confirmation", company="Deloitte", role_hint="AI Engineer"), **DELOITTE)
    assert rep.created == 1
    # 退回到还没有 DMARC 检查时的样子：邮件没查过，域名也没记
    conn.execute("UPDATE emails SET dmarc = NULL")
    conn.execute("UPDATE companies SET verified_domains_json = '[]'")
    conn.commit()

    imap = FakeImap({"1": raw})
    out = pipeline.verify_senders(conn, MailReader(connect=lambda: imap))
    assert out["checked"] == 1 and out["learned"] == ["deloitte.com"]
    assert conn.execute("SELECT dmarc FROM emails").fetchone()[0] == "pass"
    assert imap.marked_seen == set(), "只读：BODY.PEEK"


def test_mail_from_a_changed_uidvalidity_is_skipped(conn):
    sweep(conn, Fake(type="confirmation", company="Deloitte", role_hint="AI Engineer"), verdict=None, **DELOITTE)
    conn.execute("UPDATE emails SET dmarc = NULL, uid = '99:1'")
    conn.commit()
    out = pipeline.verify_senders(conn, MailReader(connect=lambda: FakeImap({"1": eml()})))
    assert out["checked"] == 0 and out["unreachable"] == 1
