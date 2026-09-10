"""IMAP 读取的测试。

「邮件只读」在这里是**被断言的属性**，不是一句说明。
IMAP 里读信本身就可能是写操作——FETCH BODY[] 会让服务器自动打上 \\Seen——
所以假服务器模拟了这个行为，测试断言整个读取流程下来一封信都没被标成已读。
"""

import inspect
from datetime import date, datetime, timezone
from email.message import EmailMessage

import pytest

from jha import db
from jha.mail import imap
from jha.mail.imap import (
    MailReader, MailWriteForbidden, ReadOnlyImap, fetch_spec_sets_seen, imap_date,
    parse_message,
)
from mailfix import FakeImap, eml


# ---------------------------------------------------------------------------
# 守卫
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["store", "copy", "expunge", "append", "delete", "xatom"])
def test_write_methods_are_blocked_before_reaching_the_server(method):
    fake = FakeImap({})
    with pytest.raises(MailWriteForbidden):
        getattr(ReadOnlyImap(fake), method)("1", "+FLAGS", "(\\Seen)")
    assert not any(c[0] == method for c in fake.calls), "写命令已经发到服务器了"


def test_private_command_escape_hatch_is_blocked():
    """imaplib 的 _command 能发任意命令，等于后门。"""
    fake = FakeImap({})
    with pytest.raises(MailWriteForbidden):
        ReadOnlyImap(fake)._command("STORE", "1", "+FLAGS", "(\\Seen)")
    assert fake.calls == []


@pytest.mark.parametrize("verb", ["STORE", "COPY", "MOVE", "EXPUNGE", "THREAD", "SORT"])
def test_uid_is_a_whitelist_not_a_blacklist(verb):
    """只放行 SEARCH / FETCH。

    THREAD / SORT 其实是读命令，也被拦——这是白名单的代价，而且是值得付的：
    黑名单漏列一个写命令就破了，白名单漏列一个读命令只是少个功能。
    """
    fake = FakeImap({})
    with pytest.raises(MailWriteForbidden):
        ReadOnlyImap(fake).uid(verb, "1", "+FLAGS", "(\\Seen)")
    assert not any(c[:2] == ("uid", verb) for c in fake.calls)


def test_select_must_be_readonly():
    fake = FakeImap({})
    with pytest.raises(MailWriteForbidden):
        ReadOnlyImap(fake).select("INBOX")
    assert fake.calls == []


@pytest.mark.parametrize("spec", [
    "(RFC822)", "(BODY[])", "(BODY[TEXT])", "(RFC822.TEXT)", "(BODY[HEADER])",
])
def test_fetch_specs_that_would_mark_mail_read_are_blocked(spec):
    assert fetch_spec_sets_seen(spec)
    fake = FakeImap({"1": eml()})
    with pytest.raises(MailWriteForbidden, match="PEEK"):
        ReadOnlyImap(fake).uid("FETCH", "1", spec)
    assert fake.marked_seen == set()


@pytest.mark.parametrize("spec", [
    "(BODY.PEEK[])", "(BODY.PEEK[HEADER])", "(RFC822.HEADER)", "(RFC822.SIZE)",
    "(FLAGS)", "(UID)",
])
def test_fetch_specs_that_leave_mail_unread_are_allowed(spec):
    assert not fetch_spec_sets_seen(spec)


def test_plain_fetch_is_guarded_too():
    with pytest.raises(MailWriteForbidden):
        ReadOnlyImap(FakeImap({"1": eml()})).fetch("1", "(RFC822)")


# ---------------------------------------------------------------------------
# 读取全流程
# ---------------------------------------------------------------------------

def reader_for(messages):
    fake = FakeImap(messages)
    return MailReader(user="me@x.com", password="app-pass", connect=lambda: fake), fake


def test_reading_mail_never_marks_anything_as_read():
    """「邮件只读」原则在执行层面的锚点。

    读完之后，服务器上没有一封信的状态被改变。
    """
    r, fake = reader_for({"1": eml(), "2": eml(subject="Interview")})
    out = r.fetch_since(date(2026, 9, 1))
    assert len(out) == 2
    assert fake.marked_seen == set(), "有邮件被标成已读了"
    assert fake.selected_readonly is True
    assert {c[1] for c in fake.calls if c[0] == "uid"} <= {"SEARCH", "FETCH"}
    assert not any(c[0] in imap.FORBIDDEN_METHODS for c in fake.calls)


def test_reader_logs_out_even_when_something_breaks():
    fake = FakeImap({"1": eml()})

    def boom(*a):
        raise RuntimeError("connection dropped")

    fake.uid = boom
    r = MailReader(user="u", password="p", connect=lambda: fake)
    with pytest.raises(RuntimeError):
        r.fetch_since(date(2026, 9, 1))
    assert ("logout",) in fake.calls


def test_uid_carries_uidvalidity():
    # UIDVALIDITY 一变，UID 会重排——不带上它，去重会把两封不同的信当成同一封
    r, _ = reader_for({"42": eml()})
    assert r.fetch_since(date(2026, 9, 1))[0].uid == "77:42"


def test_imap_date_uses_english_months_regardless_of_locale():
    """IMAP 日期必须是英文月份。strftime('%b') 跟 locale 走，中文 Windows 上会变成「9月」。"""
    assert imap_date(date(2026, 9, 1)) == "01-Sep-2026"
    assert imap_date(date(2026, 12, 31)) == "31-Dec-2026"
    assert "%b" not in inspect.getsource(imap.imap_date)


def test_search_criteria_is_well_formed():
    r, fake = reader_for({"1": eml()})
    r.fetch_since(date(2026, 9, 1))
    search = next(c for c in fake.calls if c[:2] == ("uid", "SEARCH"))
    assert "SINCE 01-Sep-2026" in search[-1]


def test_missing_credentials_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(imap.config, "env", lambda k, d=None: None)
    with pytest.raises(imap.MailNotConfigured, match="应用专用密码"):
        MailReader().fetch_since(date(2026, 9, 1))


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def test_parse_plain_message():
    e = parse_message(eml(frm="Jane <Jane@Ramp.com>", subject="Next steps",
                          body="Please pick a time"), "1:1")
    assert e.from_domain == "ramp.com"
    assert e.subject == "Next steps"
    assert "pick a time" in e.body_text


def test_parse_html_only_message():
    m = EmailMessage()
    m["From"], m["Subject"] = "a@acme.com", "x"
    m["Date"] = "Fri, 05 Sep 2026 15:00:00 +0000"
    m.set_content('<p>Book here: <a href="https://calendly.com/acme/30min">schedule</a></p>',
                  subtype="html")
    e = parse_message(m.as_bytes(), "1:1")
    assert "<p>" not in e.body_text and "schedule" in e.body_text
    assert "https://calendly.com/acme/30min" in e.links


def test_links_are_extracted_from_html_even_when_plain_is_preferred():
    """「请点击确认」类链接常常只在 HTML 部分里。它们原文呈现给你，agent 不打开。"""
    raw = eml(body="See portal",
              html='<a href="https://boards.greenhouse.io/x/confirm">confirm</a>')
    e = parse_message(raw, "1:1")
    assert e.body_text.startswith("See portal")
    assert "https://boards.greenhouse.io/x/confirm" in e.links


def test_attachments_are_never_read_into_the_body():
    e = parse_message(eml(body="Offer letter attached", attachment=b"%PDF-1.4 SECRETPDF"), "1:1")
    assert "SECRETPDF" not in e.body_text


def test_non_ascii_subject_is_decoded():
    assert parse_message(eml(subject="面试邀请 — Ramp"), "1:1").subject == "面试邀请 — Ramp"


def test_received_at_is_local_naive_so_events_can_be_sorted():
    """邮件头时间带时区，手工记的事件不带。

    两种混进 events 表，derive_status 排序时会直接抛
    「can't compare offset-naive and offset-aware datetimes」。
    """
    e = parse_message(eml(date=datetime(2026, 9, 5, 15, 0, tzinfo=timezone.utc)), "1:1")
    assert e.received_at and "+" not in e.received_at
    assert db._parse_dt(e.received_at).tzinfo is None
