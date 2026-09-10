"""IMAP 读取 —— **只读**，三层保证。

路线图 §0「邮件只读」的落地方式是「代码里不存在写路径」。但 IMAP 有个不显眼的坑：
**读邮件本身就可能是一次写操作。**

    FETCH BODY[]   服务器会自动给这封信打上 \\Seen —— 你收件箱里的未读被悄悄标成已读
    FETCH RFC822   同上
    FETCH BODY.PEEK[]  不会

所以只读靠三层叠起来保证，任何一层单独都不够：

    1. select(readonly=True) → 协议层发 EXAMINE，服务器拒绝一切标记变更
    2. 只用 BODY.PEEK[] 取信
    3. ReadOnlyImap 守卫：写方法直接抛异常；uid() 用**白名单**只放行 SEARCH / FETCH；
       会打 \\Seen 的 FETCH 取法也拦；私有方法和 xatom（能发任意命令）也拦

第 3 层用白名单而不是黑名单：IMAP 扩展命令很多，漏列一个写命令黑名单就破了，
白名单漏列一个读命令只是功能少一个。

附件一律不下载、不保存。链接只抽取原文呈现给你，**永不打开**。
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import imaplib
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from .. import config
from ..sources.base import html_to_text


class MailError(RuntimeError):
    pass


class MailNotConfigured(MailError):
    pass


class MailWriteForbidden(MailError):
    """试图对邮箱做写操作。这是 bug，不是可恢复的错误。"""


#: 会改变服务器状态、或能发任意命令的 imaplib 方法
FORBIDDEN_METHODS = frozenset({
    "store", "copy", "move", "expunge", "append", "delete", "create", "rename",
    "setacl", "deleteacl", "setquota", "subscribe", "unsubscribe", "setannotation",
    "xatom",          # 能发任意命令，等于后门
})

#: uid() 的白名单。只读这两个就够了
ALLOWED_UID_VERBS = frozenset({"SEARCH", "FETCH"})

_BODY_SETS_SEEN = re.compile(r"\bBODY\[", re.IGNORECASE)                     # BODY.PEEK[ 不匹配
_RFC822_SETS_SEEN = re.compile(r"\bRFC822(?!\.HEADER|\.SIZE)\b", re.IGNORECASE)


def fetch_spec_sets_seen(spec: str) -> bool:
    """这个 FETCH 取法会不会让服务器把邮件标成已读。"""
    return bool(_BODY_SETS_SEEN.search(spec) or _RFC822_SETS_SEEN.search(spec))


class ReadOnlyImap:
    """包住 imaplib 连接，任何写操作在发出去之前就抛异常。"""

    def __init__(self, conn: Any):
        self._conn = conn

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name.lower() in FORBIDDEN_METHODS:
            raise MailWriteForbidden(f"邮箱是只读的：禁止调用 {name}()")
        return getattr(self._conn, name)

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> Any:
        if not readonly:
            raise MailWriteForbidden("select 必须 readonly=True（EXAMINE），否则服务器允许改标记")
        return self._conn.select(mailbox, readonly=True)

    def uid(self, command: str, *args: Any) -> Any:
        verb = str(command).upper()
        if verb not in ALLOWED_UID_VERBS:
            raise MailWriteForbidden(f"邮箱是只读的：uid {verb} 不在白名单里")
        if verb == "FETCH" and len(args) >= 2 and fetch_spec_sets_seen(str(args[1])):
            raise MailWriteForbidden(
                f"FETCH {args[1]} 会把邮件标成已读——用 BODY.PEEK[]"
            )
        return self._conn.uid(command, *args)

    def fetch(self, message_set: Any, message_parts: str) -> Any:
        if fetch_spec_sets_seen(str(message_parts)):
            raise MailWriteForbidden(f"FETCH {message_parts} 会把邮件标成已读——用 BODY.PEEK[]")
        return self._conn.fetch(message_set, message_parts)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

@dataclass
class RawEmail:
    uid: str                 # "{UIDVALIDITY}:{UID}" —— UIDVALIDITY 变了 UID 会重排
    message_id: str
    from_addr: str
    from_domain: str
    subject: str
    received_at: str         # 本地时间、无时区，见 _local_naive
    body_text: str
    links: list[str] = field(default_factory=list)


#: IMAP 的日期必须是英文月份。strftime("%b") 跟随系统 locale，
#: 中文 Windows 上会变成「9月」，SEARCH 直接报错。
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def imap_date(d: date) -> str:
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


_URL = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)
_HREF = re.compile(r"""href\s*=\s*["'](https?://[^"']+)["']""", re.IGNORECASE)


def _extract_links(text: str, html_src: str = "") -> list[str]:
    """抽取链接**原文**给你看。agent 没有打开链接的能力，也不该有。"""
    seen: set[str] = set()
    out: list[str] = []
    for u in list(_HREF.findall(html_src or "")) + _URL.findall(text or ""):
        u = u.rstrip(".,;:")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:25]


def _local_naive(raw_date: str) -> str:
    """统一转成本地时间、去掉时区。

    邮件头的时间带时区，而你手工记的事件是本地无时区时间。两种混进 events 表，
    derive_status 排序时会直接抛「can't compare offset-naive and offset-aware」。
    """
    try:
        dt = email.utils.parsedate_to_datetime(raw_date)
    except (TypeError, ValueError, IndexError):
        return ""
    if dt is None:
        return ""
    return dt.astimezone().replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


def _content(part: Any) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeError, KeyError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", "replace")


def parse_message(raw: bytes, uid: str) -> RawEmail:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    from_hdr = str(msg.get("From", "") or "")
    _, addr = email.utils.parseaddr(from_hdr)
    domain = addr.rsplit("@", 1)[-1].lower().strip() if "@" in addr else ""

    body_text, html_src = "", ""
    part = msg.get_body(preferencelist=("plain", "html"))   # 跳过附件
    if part is not None:
        content = _content(part)
        if part.get_content_type() == "text/html":
            html_src, body_text = content, html_to_text(content)
        else:
            body_text = content
    if not html_src:
        hp = msg.get_body(preferencelist=("html",))
        if hp is not None and hp is not part:
            html_src = _content(hp)

    return RawEmail(
        uid=uid,
        message_id=str(msg.get("Message-ID", "") or "").strip(),
        from_addr=from_hdr,
        from_domain=domain,
        subject=str(msg.get("Subject", "") or "").strip(),
        received_at=_local_naive(str(msg.get("Date", "") or "")),
        body_text=(body_text or "").strip(),
        links=_extract_links(body_text, html_src),
    )


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

class MailReader:
    def __init__(
        self, *, host: str | None = None, port: int | None = None,
        user: str | None = None, password: str | None = None,
        mailbox: str | None = None, connect: Callable[[], Any] | None = None,
    ):
        self.host = host or config.env("IMAP_HOST", "imap.gmail.com") or "imap.gmail.com"
        self.port = int(port or config.env("IMAP_PORT", "993") or 993)
        self.user = user if user is not None else config.env("IMAP_USER")
        self.password = password if password is not None else config.env("IMAP_APP_PASSWORD")
        self.mailbox = mailbox or config.env("IMAP_MAILBOX", "INBOX") or "INBOX"
        self._connect = connect

    def _open(self) -> ReadOnlyImap:
        if self._connect is not None:
            return ReadOnlyImap(self._connect())
        if not (self.user and self.password):
            raise MailNotConfigured(
                "没配 IMAP_USER / IMAP_APP_PASSWORD。Gmail 要先开两步验证，"
                "再到 Google 账号 → 安全性 → 应用专用密码 生成一个"
            )
        return ReadOnlyImap(imaplib.IMAP4_SSL(self.host, self.port))

    def fetch_since(self, since: date, *, limit: int = 200) -> list[RawEmail]:
        conn = self._open()
        try:
            if self.user and self.password:
                conn.login(self.user, self.password)
            typ, _ = conn.select(self.mailbox, readonly=True)
            if typ != "OK":
                raise MailError(f"打不开邮箱 {self.mailbox}")

            validity = "0"
            try:
                _, vals = conn.response("UIDVALIDITY")
                if vals and vals[0]:
                    validity = vals[0].decode() if isinstance(vals[0], bytes) else str(vals[0])
            except Exception:
                pass

            typ, data = conn.uid("SEARCH", None, f"(SINCE {imap_date(since)})")
            if typ != "OK":
                raise MailError("IMAP SEARCH 失败")
            uids = ((data or [b""])[0] or b"").split()[-limit:]

            out: list[RawEmail] = []
            for u in uids:
                u_s = u.decode() if isinstance(u, bytes) else str(u)
                typ, msg_data = conn.uid("FETCH", u_s, "(BODY.PEEK[])")
                if typ != "OK" or not msg_data:
                    continue
                raw_bytes = next(
                    (p[1] for p in msg_data if isinstance(p, tuple) and len(p) >= 2), None
                )
                if raw_bytes:
                    out.append(parse_message(raw_bytes, f"{validity}:{u_s}"))
            return out
        finally:
            try:
                conn.logout()
            except Exception:
                pass
