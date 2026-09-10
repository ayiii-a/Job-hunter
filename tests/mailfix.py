"""邮件测试的共享夹具：造 .eml、假 IMAP 服务器。"""

from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid


def eml(
    *, frm="Recruiting <no-reply@greenhouse-mail.io>", subject="Thanks for applying",
    body="Hello", html=None, date=None, msgid=None, attachment=None,
) -> bytes:
    m = EmailMessage()
    m["From"] = frm
    m["To"] = "me@example.com"
    m["Subject"] = subject
    m["Date"] = format_datetime(date or datetime(2026, 9, 5, 15, 0, tzinfo=timezone.utc))
    m["Message-ID"] = msgid or make_msgid()
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    if attachment:
        m.add_attachment(attachment, maintype="application", subtype="pdf", filename="x.pdf")
    return m.as_bytes()


class FakeImap:
    """假 IMAP 服务器。

    模拟真实服务器的一个关键行为：**非 PEEK 的 FETCH 会把邮件标成已读**。
    这样「没有把任何邮件标成已读」就是一个能断言的属性，而不是一句说明。
    """

    def __init__(self, messages: dict[str, bytes], uidvalidity=b"77"):
        self.messages = messages
        self.uidvalidity = uidvalidity
        self.calls: list[tuple] = []
        self.marked_seen: set[str] = set()
        self.selected_readonly = None

    def login(self, user, password):
        self.calls.append(("login",))
        return "OK", [b"logged in"]

    def select(self, mailbox="INBOX", readonly=False):
        self.calls.append(("select", mailbox, readonly))
        self.selected_readonly = readonly
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [self.uidvalidity]

    def uid(self, command, *args):
        verb = command.upper()
        self.calls.append(("uid", verb, *args))
        if verb == "SEARCH":
            return "OK", [b" ".join(k.encode() for k in self.messages)]
        if verb == "FETCH":
            u, spec = args[0], args[1]
            if "PEEK" not in spec.upper():
                self.marked_seen.add(u)
            return "OK", [(f"{u} (BODY[] {{1}}".encode(), self.messages[u]), b")"]
        if verb == "STORE":
            self.marked_seen.add(args[0])
        return "OK", [b""]

    # 写方法：真实服务器上存在。守卫必须在调到这里之前就拦下
    def store(self, *a):
        self.calls.append(("store", *a))
        return "OK", [b""]

    def copy(self, *a):
        self.calls.append(("copy", *a))
        return "OK", [b""]

    def expunge(self):
        self.calls.append(("expunge",))
        return "OK", [b""]

    def append(self, *a):
        self.calls.append(("append", *a))
        return "OK", [b""]

    def delete(self, *a):
        self.calls.append(("delete", *a))
        return "OK", [b""]

    def xatom(self, *a):
        self.calls.append(("xatom", *a))
        return "OK", [b""]

    def _command(self, *a):
        self.calls.append(("_command", *a))
        return "OK"

    def logout(self):
        self.calls.append(("logout",))
        return "BYE", [b""]


class FakeReader:
    def __init__(self, raws):
        self.raws = list(raws)
        self.since_calls = []

    def fetch_since(self, since, *, limit=200):
        self.since_calls.append(since)
        return list(self.raws)


class FakeClassifier:
    """按邮件内容里的标记返回分类结果。记录每次收到的 user 内容。"""

    model = "claude-haiku-4-5"

    def __init__(self, rule):
        self.rule = rule
        self.calls = []

    def structured(self, *, system, user, schema, **kw):
        self.calls.append({"system": system, "user": user, **kw})
        return dict(self.rule(user))
