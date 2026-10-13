"""邮件管线：拉取 → 存正文 → 预过滤 → 分类 → 匹配 → 按误判代价更新。外加人工确认队列。

## 人工确认队列只在 CLI 里

进队列的邮件（offer、置信度低、分不清是哪条投递、发件方可疑却要建档的），
**确认它们的函数不是 agent 的工具**。
和简历的审核门同一个道理：人工确认如果 agent 能自己做，那就不是人工确认。

## 表里没有的投递会自动补上

表里的投递不一定都是 agent 投的：你在 LinkedIn、公司官网投的岗位来信时，
确认信、拒信、面试、OA 会新建一条投递记录（策略见 policy.py）。
公司名、岗位名是分类器从邮件里抽的——写进库之前过 `clean_name` 的确定性检查，
因为它们之后会出现在推送里、出现在 agent 看得到的投递表里。

## 什么都不悄悄丢

预过滤判无关的、分类器判无关的、招聘方主动联系（不改状态）的，都进人工队列当「打算丢掉」的候选，
你确认了（agent mail dismiss）才算真的丢。它们不推送、不进给 agent 的队列，只是等你批量看一眼。

## 给 agent 的东西里没有邮件原文

`queue_for_agent` 和 `SweepReport.compact` 只含分类结果、置信度、匹配到的投递、
以及公司和岗位名——后两者**来自我们自己的数据库**，不是邮件里的字。
**连主题行都不给**：主题行一样是不可信输入。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from .. import db, tracking
from .imap import MailReader, RawEmail
from . import match as match_mod
from . import policy, prefilter
from . import classify
from ..agent.client import AgentClient, Budget, BudgetExceeded, MissingAPIKey


@dataclass
class SweepReport:
    filtered: int = 0
    classified: int = 0
    auto_applied: int = 0
    created: int = 0          # 其中新建了几条投递记录（表里原来没有的）
    learned: list[str] = field(default_factory=list)   # 新记下的公司发件域名
    queued: int = 0
    ignored: int = 0
    alerts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def compact(self) -> dict[str, Any]:
        return {
            "drop_candidates_queued": self.filtered + self.ignored,
            "filtered_out": self.filtered, "classified": self.classified,
            "auto_applied": self.auto_applied, "records_created": self.created,
            "queued_for_human": self.queued, "domains_learned": len(self.learned),
            "ignored": self.ignored, "alerts": self.alerts, "errors": self.errors[:5],
        }


# ---------------------------------------------------------------------------
# 拉取与存储
# ---------------------------------------------------------------------------

def store_raw(conn: sqlite3.Connection, raw: RawEmail) -> int | None:
    """存下正文。**邮件删了就拿不回来**——没有正文就没有回归测试集。"""
    if raw.message_id and conn.execute(
        "SELECT 1 FROM emails WHERE message_id = ?", (raw.message_id,)
    ).fetchone():
        return None
    cur = conn.execute(
        "INSERT OR IGNORE INTO emails (uid, message_id, from_addr, from_domain, subject, "
        "body_text, received_at, links_json, dmarc) VALUES (?,?,?,?,?,?,?,?,?)",
        (raw.uid, raw.message_id or None, raw.from_addr, raw.from_domain, raw.subject,
         raw.body_text, raw.received_at or None, db.dump_json(raw.links), raw.dmarc or None),
    )
    conn.commit()
    return int(cur.lastrowid) if cur.rowcount else None


def ingest(
    conn: sqlite3.Connection, reader: MailReader, *,
    since_days: int = 14, limit: int = 200, now: datetime | None = None,
) -> tuple[int, int]:
    """拉取并存储。返回 (拉到几封, 新增几封)。重叠拉取靠去重兜住，所以宁可多拉一天。"""
    now = now or datetime.now()
    since: date = (now - timedelta(days=since_days)).date()
    last = conn.execute("SELECT MAX(received_at) m FROM emails").fetchone()["m"]
    if last:
        try:
            since = max(since, (db._parse_dt(last) - timedelta(days=1)).date())
        except (ValueError, TypeError):
            pass
    raws = reader.fetch_since(since, limit=limit)
    new = sum(1 for r in raws if store_raw(conn, r) is not None)
    return len(raws), new


# ---------------------------------------------------------------------------
# 处理
# ---------------------------------------------------------------------------

#: 可信的发件方：只有它们的邮件能直接新建投递记录
TRUSTED_CHANNELS = frozenset({"company", "ats", "board"})

#: 从邮件里取、要写进表的名字只能由这些字符组成：字母数字和几种常见标点（含岗位名里常见的 – —），
#: 没有链接、尖括号
_NAME_CHARS = re.compile(r"^[\w &.,'’()/+#–—-]+$")

#: 岗位名比公司名长得多。实测的真实岗位名：
#: 「Engineering Intern - Supply Chain Data, AI and Business Intelligence (Spring 2027 Co-op)」
ROLE_LIMITS = {"max_words": 16, "max_len": 120}

#: 分类器偶尔会把转发平台当成招聘公司
_PLATFORM_NAMES = frozenset({
    "linkedin", "indeed", "glassdoor", "handshake", "wellfound", "ziprecruiter",
    "greenhouse", "lever", "ashby", "workday", "smartrecruiters", "icims",
})


def clean_name(raw: Any, source_text: str | None, *, max_words: int, max_len: int = 80) -> str:
    """分类器从邮件里抽的公司名 / 岗位名能不能写进表。不能就返回空串。

    这些名字之后会出现在推送里、出现在 agent 看得到的投递表里，而它们来自不可信的邮件。
    所以三道确定性检查：字符集和长度（挡链接、挡一整句话）、不是招聘平台的名字、
    **确实是邮件原文里出现过的字**（分类器补全或编出来的不收）。
    source_text 传 None 表示是你亲手给的名字，不查最后一道。
    """
    s = " ".join(str(raw or "").split())
    if not 2 <= len(s) <= max_len or len(s.split()) > max_words or not _NAME_CHARS.match(s):
        return ""
    low = s.lower()
    if "http" in low or "www." in low or low in _PLATFORM_NAMES:
        return ""
    if source_text is not None and low not in " ".join(source_text.split()).lower():
        return ""
    return s


def _received(row: sqlite3.Row) -> datetime | None:
    try:
        return db._parse_dt(row["received_at"]) if row["received_at"] else None
    except (ValueError, TypeError):
        return None


def _company_name(conn: sqlite3.Connection, company_id: int | None) -> str:
    row = conn.execute("SELECT name FROM companies WHERE id = ?", (company_id,)).fetchone() if company_id else None
    return row["name"] if row else ""


def _rejection_cleared(
    client: AgentClient, row: sqlite3.Row, *, budget: Budget, conn: sqlite3.Connection,
) -> bool:
    """复核能不能排除「这其实是邀请」。

    只有复核明确说没有下一步（False，不是缺省）、而且它引用的那句话确实在邮件里，才算排除——
    引用对不上原文，说明它没好好读，这次的结论不收。其余一律当成可能是邀请。
    """
    subject, body = row["subject"] or "", row["body_text"] or ""
    try:
        data = classify.review_rejection(client, subject=subject, from_addr=row["from_addr"] or "",
                                         body=body, budget=budget, conn=conn, email_id=row["id"])
    except (MissingAPIKey, BudgetExceeded):
        raise
    except Exception:
        return False
    quote = " ".join(str(data.get("quote") or "").split()).lower()
    source = " ".join(f"{subject}\n{body}".split()).lower()
    return data.get("asks_for_next_step") is False and len(quote) >= 10 and quote in source


def _write_event(
    conn: sqlite3.Connection, row: sqlite3.Row, app_id: int, ctype: str, event_type: str,
    *, source: str, payload: dict[str, Any],
) -> int:
    event_id = db.append_event(conn, app_id, event_type, occurred_at=_received(row),
                               source=source, raw_ref=str(row["id"]), payload=payload)
    if ctype == "confirmation":
        conn.execute("UPDATE applications SET confirmation_seen_at = COALESCE(confirmation_seen_at, ?) "
                     "WHERE id = ?", (row["received_at"], app_id))
    return event_id


def _round_info(cls: dict[str, Any]) -> dict[str, Any]:
    """分类器给的「第几轮」「是不是跟进信」，写进事件 payload，漏斗数面试轮数用（status.interview_rounds）。"""
    out: dict[str, Any] = {}
    n = cls.get("round")
    if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= 10:
        out["round"] = n
    if cls.get("followup") is True:
        out["followup"] = True
    return out


def learn_sender_domain(
    conn: sqlite3.Connection, row: sqlite3.Row, application_id: int | None, *, confirmed_by_human: bool = False,
) -> str:
    """把验证过的公司自有发件域名记进这家公司（companies.verified_domains）。返回记下的域名，没记就是空串。

    记下之后，这个域名的来信直接认作这家公司、算可信发件方——所以条件要严，全部满足才记：
      1. 收信服务器验过 DMARC，而且验的就是 From 里这个域名（emails.dmarc = 'pass'）——From 头谁都能写。
         你在队列里亲手确认过的那封例外：只要不是明确验失败（fail）就记
      2. 不是多家共用、谁都能注册的域名：招聘系统、求职平台、跳转服务、个人邮箱
      3. 域名就是这家公司的：注册名对得上公司名或它的首字母缩写（prefilter.domain_belongs_to）；
         或者是你在人工队列里亲手确认过的这封信（bah.com ↔ Booz Allen 这种名字对不上的，靠这条）
      4. 没被别的公司占着
    记的是注册域名（careers.acme.com → acme.com）：同一家公司的子域名都算它的。
    """
    dmarc = row["dmarc"] or ""
    if not application_id or (dmarc != "pass" and not (confirmed_by_human and dmarc != "fail")):
        return ""
    domain = prefilter.registered_domain(row["from_domain"] or "")
    if not domain or prefilter.shared_sender(domain):
        return ""
    co = conn.execute(
        "SELECT c.* FROM applications a JOIN jobs j ON j.id = a.job_id JOIN companies c ON c.id = j.company_id "
        "WHERE a.id = ?", (application_id,),
    ).fetchone()
    if co is None or not (confirmed_by_human or prefilter.domain_belongs_to(domain, co["name"])):
        return ""
    owner = next((cid for d, cid in prefilter.company_domain_map(conn).items()
                  if prefilter.domain_matches(domain, d) or prefilter.domain_matches(d, domain)), None)
    if owner is not None:
        return ""                       # 已经记过（owner 是它自己），或者被别的公司占着
    learned = [*db.load_json(co["verified_domains_json"]), domain]
    conn.execute("UPDATE companies SET verified_domains_json = ? WHERE id = ?", (db.dump_json(learned), co["id"]))
    conn.commit()
    return domain


def _new_domain_problem(
    conn: sqlite3.Connection, row: sqlite3.Row, company_id: int | None, company_name: str,
    domain_map: dict[str, int],
) -> str:
    """发件方是一个没见过的公司自有域名时，它可不可疑。返回进队列的理由；不可疑、或者不适用就是空串。

    放行之后 learn_sender_domain 会把它记成这家公司的，以后这个域名的来信直接可信——所以第一次把好关：
      - 收信服务器没验过（DMARC 没通过）
      - 和公司名、公司名首字母缩写都对不上
      - 和这家公司登记过的域名同名、后缀不同（stripe.com 登记过，来了个 stripe.xyz）
    招聘系统、求职平台、个人邮箱不是哪家公司的域名，不适用。
    理由会进 agent 看得到的队列，所以不写域名本身——那是邮件里的字。
    """
    domain = prefilter.registered_domain(row["from_domain"] or "")
    if not domain or not company_name or prefilter.shared_sender(domain):
        return ""
    if any(prefilter.domain_matches(domain, d) or prefilter.domain_matches(d, domain) for d in domain_map):
        return ""
    if (row["dmarc"] or "") != "pass":
        return "发件域名第一次见，收信服务器没验过它（DMARC 没通过）——可能是仿冒，你确认后再记"
    if not prefilter.domain_belongs_to(domain, company_name):
        return "发件域名第一次见，和公司名、公司名首字母缩写都对不上——你确认后会记下来，以后就认得了"
    co = conn.execute("SELECT email_domains_json, verified_domains_json FROM companies WHERE id = ?",
                      (company_id,)).fetchone() if company_id else None
    label = domain.split(".")[0]
    if co and any(prefilter.registered_domain(d).split(".")[0] == label for d in prefilter.company_domains(co)):
        return "发件域名和这家公司登记过的域名同名、后缀不同——典型的仿冒手法，你确认后再记"
    return ""


def verify_senders(conn: sqlite3.Connection, reader: MailReader) -> dict[str, Any]:
    """存量邮件：补查 DMARC（只取邮件头），再按 learn_sender_domain 的规则把公司域名补记进 companies。

    只看已经记进投递表的邮件（自动写入、新建、或你在队列里确认过的）——这些是「验证没有问题」的。
    """
    rows = conn.execute("SELECT id, uid FROM emails WHERE dmarc IS NULL").fetchall()
    results = reader.fetch_auth([r["uid"] for r in rows]) if rows else {}
    for r in rows:
        if r["uid"] in results:
            conn.execute("UPDATE emails SET dmarc = ? WHERE id = ?", (results[r["uid"]], r["id"]))
    conn.commit()

    learned: list[str] = []
    for r in conn.execute(
        "SELECT * FROM emails WHERE dmarc = 'pass' AND matched_application_id IS NOT NULL "
        "AND (policy IN ('auto_apply', 'create') OR review_status = 'accepted') ORDER BY received_at"
    ).fetchall():
        d = learn_sender_domain(conn, r, r["matched_application_id"],
                                confirmed_by_human=r["review_status"] == "accepted")
        if d:
            learned.append(d)
    tally = dict(conn.execute("SELECT COALESCE(dmarc, '没查到'), COUNT(*) FROM emails GROUP BY 1").fetchall())
    return {"checked": len(results), "unreachable": len(rows) - len(results), "dmarc": tally, "learned": learned}


def process_pending(
    conn: sqlite3.Connection, *, client: AgentClient | None = None,
    budget: Budget | None = None, limit: int = 60, only: Iterable[int] = (),
) -> SweepReport:
    """only：只处理这几封，并且跳过预过滤——你从「打算丢掉」里放行的（release）。"""
    rep = SweepReport()
    only = frozenset(only)
    sql, params = "SELECT * FROM emails WHERE policy IS NULL", []
    if only:
        sql += f" AND id IN ({','.join('?' * len(only))})"
        params += sorted(only)
    rows = conn.execute(sql + " ORDER BY received_at LIMIT ?", (*params, limit)).fetchall()
    if not rows:
        return rep

    # 1. 确定性预过滤。没过的一次 LLM 都不花
    domain_map = prefilter.company_domain_map(conn)
    applied = prefilter.applied_company_names(conn)
    todo: list[tuple[sqlite3.Row, prefilter.PrefilterResult]] = []
    for r in rows:
        pre = (prefilter.PrefilterResult(True, "你从「打算丢掉」里放行的", channel="keyword")
               if r["id"] in only else prefilter.screen(r, domain_map, applied))
        if pre.passed:
            todo.append((r, pre))
        else:
            conn.execute("UPDATE emails SET policy = 'filtered', review_status = 'pending', reason = ? "
                         "WHERE id = ?", (pre.reason + "——你确认了才真的丢", r["id"]))
            rep.filtered += 1
    conn.commit()
    if not todo:
        return rep

    client = client or AgentClient()
    # 每封最多两次调用：分类 + 可能的拒信复核
    # 开着思考，实测每封分类输出 100–800 token
    budget = budget or Budget(max_llm_calls=2 * len(todo) + 2, max_output_tokens=3_000 * (len(todo) + 1))

    for r, pre in todo:
        subject, body, sender = r["subject"] or "", r["body_text"] or "", r["from_addr"] or ""

        # 2. 第二层分类（正文不进 agent 上下文）
        cls = classify.classify_one(client, subject=subject, from_addr=sender, body=body,
                                    budget=budget, conn=conn, email_id=r["id"])
        rep.classified += 1
        if cls.get("error"):
            rep.errors.append(f"邮件 #{r['id']}：{cls['error']}")

        # 3. 确定性匹配
        m = match_mod.match(conn, from_addr=sender, subject=subject, body=body,
                            company_id=pre.company_id, role_hint=cls.get("role_hint") or "")

        # 要从邮件里取公司名、岗位名建档时，先过确定性检查
        text = f"{sender}\n{subject}\n{body}"
        company = "" if m.company_id else clean_name(cls.get("company"), text, max_words=6)
        role = clean_name(cls.get("role_hint"), text, **ROLE_LIMITS)

        # 没登记的公司：模型说是哪家，代码核对发件域名是不是这家的
        named = company or _company_name(conn, m.company_id)
        trusted = pre.channel in TRUSTED_CHANNELS or prefilter.domain_belongs_to(r["from_domain"] or "", named)

        # 判成拒信、正文又有邀请信号的，换更强的模型复核一遍
        cleared = (cls["type"] == "rejection" and bool(policy.INVITE_SIGNALS.search(f"{subject}\n{body}"))
                   and _rejection_cleared(client, r, budget=budget, conn=conn))

        # 4. 按误判代价决定
        d = policy.decide(ctype=cls["type"], confidence=cls["confidence"],
                          match_status=m.status, text=f"{subject}\n{body}",
                          trusted_sender=trusted, company_known=bool(m.company_id or company),
                          invite_cleared=cleared)
        if cls.get("error"):
            # 没分出来（接口出错、模型拒绝）不等于「和求职无关」——判成 other 就会被忽略、再也不看。
            # 放进队列，你看一眼，或者 --requeue 重跑
            d = policy.Decision("queue", None, "没分类成功（接口出错或模型拒绝）——agent mail sweep --requeue 重跑，"
                                "或者 agent mail show 看原文")
        elif d.action in ("auto_apply", "create"):
            # 放行之后会把发件域名记成这家公司的（以后直接可信），所以没见过的域名第一次要把好关
            problem = _new_domain_problem(conn, r, m.company_id, named, domain_map)
            if problem:
                d = policy.Decision("queue", d.event_type, problem, alert=d.alert)

        app_id = m.application_id
        if d.action == "create":
            app_id = tracking.record_from_email(
                conn, company_id=m.company_id, company_name=company, title=role,
                occurred_at=_received(r), email_id=r["id"],
                applied_at=r["received_at"] if cls["type"] == "confirmation" else None,
            )
            rep.created += 1

        event_id = None
        if d.action in ("auto_apply", "create") and d.event_type and app_id:
            event_id = _write_event(conn, r, app_id, cls["type"], d.event_type, source="email",
                                    payload={"email_id": r["id"], "confidence": cls["confidence"],
                                             **_round_info(cls)})
            tracking.fill_unknown_role(conn, app_id, role)
            rep.auto_applied += 1
            learned = learn_sender_domain(conn, r, app_id)
            if learned:
                rep.learned.append(learned)
        elif d.action == "queue":
            rep.queued += 1
        else:
            rep.ignored += 1

        conn.execute(
            "UPDATE emails SET classification=?, confidence=?, role_hint=?, company_hint=?, summary=?, "
            "dates_json=?, action_required=?, matched_application_id=?, policy=?, "
            "review_status=?, reason=?, event_id=?, classifier_version=? WHERE id=?",
            (cls["type"], cls["confidence"], cls.get("role_hint"), cls.get("company"), cls.get("summary"),
             db.dump_json(cls.get("dates") or []), 1 if cls.get("action_required") else 0,
             app_id, d.action, "pending" if d.action in ("queue", *DROP_POLICIES) else None,
             d.reason + (f"；{m.reason}" if m.reason else ""), event_id,
             classify.CLASSIFIER_VERSION, r["id"]),
        )
        conn.commit()
        if d.alert:
            rep.alerts.append(_compact(conn, r["id"]))
    return rep


# ---------------------------------------------------------------------------
# 人工确认队列
# ---------------------------------------------------------------------------

_QUEUE_SQL = """
SELECT e.id, e.classification, e.confidence, e.matched_application_id, e.reason, e.policy,
       e.received_at, c.name AS company, j.title AS job_title
FROM emails e
LEFT JOIN applications a ON a.id = e.matched_application_id
LEFT JOIN jobs j ON j.id = a.job_id
LEFT JOIN companies c ON c.id = j.company_id
"""

_PRIORITY = ("CASE e.classification WHEN 'offer' THEN 0 WHEN 'interview_invite' THEN 1 "
             "WHEN 'oa_invite' THEN 2 WHEN 'scheduling' THEN 3 ELSE 4 END")


def _row_for_agent(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "email_id": r["id"], "type": r["classification"],
        "confidence": round(r["confidence"] or 0.0, 2),
        "matched_application_id": r["matched_application_id"],
        "company": r["company"], "job_title": r["job_title"],   # 来自我们的库，不是邮件
        "reason": r["reason"],
        "action": r["policy"],       # 我们的枚举：auto_apply / create / queue ...
    }


def _compact(conn: sqlite3.Connection, email_id: int) -> dict[str, Any]:
    r = conn.execute(_QUEUE_SQL + " WHERE e.id = ?", (email_id,)).fetchone()
    return _row_for_agent(r) if r else {"email_id": email_id}


#: 「打算丢掉」的建议：预过滤判无关、分类器判无关、招聘方主动联系（不改状态）。你确认了才算数
DROP_POLICIES = ("filtered", "ignore", "record_only")

#: agent mail queue / accept / dismiss 的 --kind
KINDS: dict[str, tuple[str, ...]] = {
    "decide": ("queue",), "drops": DROP_POLICIES,
    "filtered": ("filtered",), "other": ("ignore",), "outreach": ("record_only",),
}


def queue_for_agent(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """只给要判断的。「打算丢掉」的候选可能有几十封，只报个数（drop_candidates）。"""
    rows = conn.execute(
        _QUEUE_SQL + f" WHERE e.review_status = 'pending' AND e.policy = 'queue' "
        f"ORDER BY {_PRIORITY}, e.received_at"
    ).fetchall()
    return [_row_for_agent(r) for r in rows]


def drop_candidates(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM emails WHERE review_status = 'pending' "
        f"AND policy IN ({','.join('?' * len(DROP_POLICIES))})", DROP_POLICIES,
    ).fetchone()[0]


def select_pending(
    conn: sqlite3.Connection, ids: Iterable[int] = (), *, kind: str | None = None,
    domain: str | None = None, ctype: str | None = None,
) -> list[sqlite3.Row]:
    """队列里的邮件，人看的版本（带主题和摘要，只给 CLI 用）。按 id、种类、发件域名、分类挑一批。"""
    ids = list(ids)
    sql = ("SELECT e.*, c.name AS company, j.title AS job_title FROM emails e "
           "LEFT JOIN applications a ON a.id = e.matched_application_id "
           "LEFT JOIN jobs j ON j.id = a.job_id LEFT JOIN companies c ON c.id = j.company_id "
           "WHERE e.review_status = 'pending'")
    params: list[Any] = []
    if ids:
        sql += f" AND e.id IN ({','.join('?' * len(ids))})"
        params += ids
    if kind:
        policies = KINDS[kind]
        sql += f" AND e.policy IN ({','.join('?' * len(policies))})"
        params += policies
    if ctype:
        sql += " AND e.classification = ?"
        params.append(ctype)
    rows = conn.execute(sql + f" ORDER BY {_PRIORITY}, e.received_at", params).fetchall()
    if domain:
        want = prefilter.registered_domain(domain) or domain.lower()
        rows = [r for r in rows if prefilter.registered_domain(r["from_domain"] or "") == want]
    return rows


def queue_for_human(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """要你判断的那部分（不含「打算丢掉」的候选）。"""
    return select_pending(conn, kind="decide")


def release(conn: sqlite3.Connection, email_ids: Iterable[int]) -> list[int]:
    """「打算丢掉」里预过滤判无关的那些，你说要留：放回待处理。调用方接着用 process_pending(only=...) 分类。"""
    ids = [int(i) for i in email_ids]
    conn.executemany("UPDATE emails SET policy = NULL, review_status = NULL, reason = NULL "
                     "WHERE id = ? AND policy = 'filtered' AND review_status = 'pending'", [(i,) for i in ids])
    conn.commit()
    return ids


def accept(
    conn: sqlite3.Connection, email_id: int, *, application_id: int | None = None,
    create: bool = False, company: str | None = None, role: str | None = None,
    as_type: str | None = None,
) -> dict[str, Any]:
    """人工确认一封邮件的建议。**不是 agent 的工具。**

    没匹配到投递时：application_id 指定已有的一条；确定是新的投递就 create=True，
    公司、岗位默认取分类器从邮件里抽的（过同样的检查），也可以自己给。
    as_type：分类器判错了，你改判（比如它判成无关的其实是拒信）。
    「打算丢掉」的候选不改判就确认 = 留着它、不改任何状态；预过滤判无关的要先分类（release）。
    """
    row = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
    if row is None:
        raise ValueError(f"没有 id 为 {email_id} 的邮件")
    if row["review_status"] != "pending":
        raise ValueError(f"邮件 #{email_id} 不在待确认队列里（{row['review_status']}）")
    if as_type and as_type not in classify.TYPES:
        raise ValueError(f"不认识的类型「{as_type}」，可选：{', '.join(classify.TYPES)}")
    if row["policy"] == "filtered" and not as_type:
        raise ValueError(f"邮件 #{email_id} 还没分类过——先放行去分类（agent mail accept 会自动做），"
                         "或者用 --as 直接给类型")

    ctype = as_type or row["classification"] or ""
    event_type = policy.STATUS_EVENT.get(ctype)
    if as_type:
        conn.execute("UPDATE emails SET classification = ?, reason = COALESCE(reason, '') || ? WHERE id = ?",
                     (as_type, f"；人工改判为 {as_type}", email_id))
    if not event_type:
        if row["policy"] in DROP_POLICIES:
            conn.execute("UPDATE emails SET review_status = 'accepted', reason = COALESCE(reason, '') || "
                         "'；你确认留着，不改状态' WHERE id = ?", (email_id,))
            conn.commit()
            return {"email_id": email_id, "application_id": None, "event": None, "status": None}
        raise ValueError(f"「{ctype}」类邮件没有对应的状态事件——要改判用 --as，不要就 dismiss")

    app_id = application_id or row["matched_application_id"]
    if app_id:
        target = conn.execute("SELECT merged_into FROM applications WHERE id = ?", (app_id,)).fetchone()
        if target is None:
            raise ValueError(f"没有 id 为 {app_id} 的投递记录")
        app_id = target["merged_into"] or app_id          # 合并过的，记到保留的那条上
    elif not create:
        raise ValueError(
            f"邮件 #{email_id} 没匹配到投递记录——用 --application <id> 指定已有的一条；"
            "确定是新的投递就加 --create（公司、岗位可以用 --company / --role 给）"
        )
    else:
        text = f"{row['from_addr']}\n{row['subject']}\n{row['body_text']}"
        name = (clean_name(company, None, max_words=6) if company
                else clean_name(row["company_hint"], text, max_words=6))
        if not name:
            raise ValueError("认不出公司名——用 --company 指定")
        title = (clean_name(role, None, **ROLE_LIMITS) if role
                 else clean_name(row["role_hint"], text, **ROLE_LIMITS))
        app_id = tracking.record_from_email(
            conn, company_id=None, company_name=name, title=title, occurred_at=_received(row),
            email_id=email_id, source="manual",
            applied_at=row["received_at"] if ctype == "confirmation" else None,
        )

    event_id = _write_event(conn, row, app_id, ctype, event_type, source="manual",
                            payload={"email_id": email_id, "accepted_from_queue": True})
    learn_sender_domain(conn, row, app_id, confirmed_by_human=True)
    conn.execute("UPDATE emails SET review_status = 'accepted', matched_application_id = ?, "
                 "event_id = ? WHERE id = ?", (app_id, event_id, email_id))
    conn.commit()
    status = conn.execute("SELECT status FROM applications WHERE id = ?", (app_id,)).fetchone()
    return {"email_id": email_id, "application_id": app_id, "event": event_type,
            "status": status["status"] if status else None}


def dismiss(conn: sqlite3.Connection, email_id: int, *, note: str = "") -> dict[str, Any]:
    cur = conn.execute(
        "UPDATE emails SET review_status = 'dismissed', "
        "reason = COALESCE(reason, '') || ? WHERE id = ? AND review_status = 'pending'",
        (f"；人工驳回：{note}" if note else "；人工驳回", email_id),
    )
    if cur.rowcount == 0:
        raise ValueError(f"邮件 #{email_id} 不在待确认队列里")
    conn.commit()
    return {"email_id": email_id, "review_status": "dismissed"}


def rescreen_filtered(conn: sqlite3.Connection) -> int:
    """预过滤规则放宽之后，把以前被它丢掉、按现在的规则能过的邮件放回待处理。

    被预过滤丢掉的邮件从来没分类过、也没写过任何事件——规则改了它们不会自己回来。
    这一步只重跑确定性的预过滤，不调模型；放回去的下次 process_pending 再分类。
    """
    domain_map, applied = prefilter.company_domain_map(conn), prefilter.applied_company_names(conn)
    ids = [(r["id"],) for r in conn.execute(
               "SELECT * FROM emails WHERE policy = 'filtered' AND COALESCE(review_status, 'pending') = 'pending'")
           if prefilter.screen(r, domain_map, applied).passed]
    conn.executemany("UPDATE emails SET policy = NULL, review_status = NULL, reason = NULL WHERE id = ?", ids)
    conn.commit()
    return len(ids)


def requeue_pending(conn: sqlite3.Connection) -> int:
    """把还在人工队列里的邮件放回待处理，下次 process_pending 按现在的规则重新分类、重新判。

    规则放宽之后（比如对不上投递的确认信现在会建档），队列里的旧邮件不会自己重新判——
    它们已经处理过了。确认过、驳回过的不动：那是你做的决定。
    队列里的邮件没写过任何事件，所以放回去不会重复记账。
    """
    cur = conn.execute("UPDATE emails SET policy = NULL, review_status = NULL, reason = NULL "
                       "WHERE review_status = 'pending'")
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# 检测留痕与即时提醒
# ---------------------------------------------------------------------------

#: 超过这么久没有一次**成功**的邮件检测，就算过期
MAIL_STALE_AFTER = timedelta(hours=6)


def record_sweep(
    conn: sqlite3.Connection, *, fetched: int = 0, new: int = 0,
    rep: SweepReport | None = None, error: str | None = None,
) -> None:
    """每次真的去邮箱拉过信，就在 fetch_runs 里留一行（source='imap'）。

    邮件检测静默停掉是这个模块最危险的失败：外壳挂了、电脑睡着了、应用专用密码被撤了，
    你看到的只是「最近没有面试邀请」。留了这一行，`mail_health` 才能把沉默变成告警。

    company_id 留空——`ingest.failing_sources` 按公司关联，这些行不会混进抓取器的告警。
    """
    conn.execute(
        "INSERT INTO fetch_runs (company_id, source, finished_at, ok, listed_count, "
        "kept_count, new_count, error) VALUES (NULL, 'imap', datetime('now'), ?, ?, ?, ?, ?)",
        (0 if error else 1, fetched, rep.classified if rep else 0, new, error),
    )
    conn.commit()


def mail_health(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    """邮件检测还活着吗。

    fetch_runs 的时间来自 SQLite 的 datetime('now')，**是 UTC**。拿本地时间去比，
    在美东会差 4–5 小时——过期要晚半天才报出来，正好是这个函数要防的那种沉默。
    失败的检测不算数：密码过期之后每次都「跑了」，但一次都没成功。
    """
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    last = conn.execute(
        "SELECT started_at, ok, error FROM fetch_runs WHERE source = 'imap' "
        "ORDER BY started_at DESC, id DESC LIMIT 1"
    ).fetchone()
    last_ok = conn.execute(
        "SELECT MAX(started_at) AS m FROM fetch_runs WHERE source = 'imap' AND ok = 1"
    ).fetchone()["m"]
    stale = last_ok is None or now - db._parse_dt(last_ok) > MAIL_STALE_AFTER
    return {
        "last_sweep_at_utc": last["started_at"] if last else None,
        "last_sweep_ok": bool(last["ok"]) if last else None,
        "last_error": last["error"] if last else None,
        "last_success_at_utc": last_ok,
        "stale": stale,
        "stale_after_hours": int(MAIL_STALE_AFTER.total_seconds() // 3600),
    }


def alert_text(alerts: list[dict[str, Any]]) -> str:
    """即时提醒的推送文本。

    这条推送不经过模型，但它是发出去的东西，所以和给 agent 的输出守同一条线：
    只拼库里的字段（公司、岗位来自我们的库，类型和处理方式是我们的枚举值），
    **不含主题行和正文**。要看原文，回电脑上跑 agent mail show。
    从邮件自动建档的公司、岗位名是分类器抽出来的，写进库之前过了 clean_name 的检查。
    """
    lines = [f"{len(alerts)} 封邮件需要你尽快处理："]
    for a in alerts:
        where = f"{a.get('company') or '?'} — {a.get('job_title') or '未匹配到投递'}"
        done = "已记进投递表" if a.get("action") in ("auto_apply", "create") else "待你确认"
        lines.append(f"#{a.get('email_id')} [{a.get('type')}] {where} · {done}")
    lines.append("在电脑上看：agent mail queue")
    return "\n".join(lines)
