"""抓取管线：拉取 → 规则初筛 → 增量补全文 → 入库 → 过期检测。

两个顺序问题决定这条管线是省钱还是烧钱：

1. **规则初筛跑在详情抓取之前。**
   Databricks 板子 870 个岗位，过完地点和标题过滤可能只剩十几个。
   先筛后抓，详情请求数是两位数；反过来是四位数。这比列表本身
   745KB→9.5MB 的 12 倍差距更值钱。

2. **过期检测比对的是【接口返回的全集】，不是初筛之后的子集。**
   否则你改一次 target_profile，一批仍然在招的岗位会因为不再匹配
   而被误判成下架——数据就脏了，而且很难发现。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

import httpx

from . import db
from .filters import FilterResult, screen_all
from .sources import UNSUPPORTED, RawJob, get_adapter, http_client

#: 每次详情请求之间的间隔。路线图要求对每个域名限速 1–2 秒。
DETAIL_DELAY_SECONDS = 1.0

#: 连续几次抓取没出现就判下架
MISS_LIMIT = 2


@dataclass
class FetchReport:
    company: str
    source: str
    ok: bool = False
    listed: int = 0
    kept: int = 0
    new: int = 0
    updated: int = 0
    detail_fetches: int = 0
    deactivated: int = 0
    error: str | None = None
    new_jobs: list[dict[str, Any]] = field(default_factory=list)
    screen_reasons: dict[str, int] = field(default_factory=dict)
    dropped: list[tuple[RawJob, FilterResult]] = field(default_factory=list)

    @property
    def headline(self) -> str:
        if not self.ok:
            return f"{self.company}: 失败 —— {self.error}"
        return (
            f"{self.company}: 接口 {self.listed} 个 → 初筛留下 {self.kept} 个"
            f" → 新增 {self.new}，更新 {self.updated}"
            f"（详情请求 {self.detail_fetches} 次，下架 {self.deactivated}）"
        )


# ---------------------------------------------------------------------------
# 单个公司
# ---------------------------------------------------------------------------

def fetch_company(
    conn: sqlite3.Connection,
    company: sqlite3.Row,
    target: dict[str, Any],
    *,
    client: httpx.Client | None = None,
    dry_run: bool = False,
    fetch_details: bool = True,
    delay: float = DETAIL_DELAY_SECONDS,
    now: datetime | None = None,
) -> FetchReport:
    name = company["name"]
    ats = (company["ats_type"] or "").strip().lower()
    token = (company["board_token"] or "").strip()
    report = FetchReport(company=name, source=ats)

    adapter = get_adapter(ats)
    if adapter is None:
        report.error = (
            f"{ats} 暂不支持（Workday 没有稳定公开接口，第二轮用 Playwright 处理）"
            if ats in UNSUPPORTED
            else f"未知的 ats_type: {ats!r}"
        )
        _record_run(conn, company, report, dry_run=dry_run)
        return report
    if not token:
        report.error = "没有 board_token，跑 agent resolve-ats 查一个"
        _record_run(conn, company, report, dry_run=dry_run)
        return report

    owns_client = client is None
    client = client or http_client()
    try:
        listed = adapter.list_jobs(client, token)
        report.listed = len(listed)

        # --- 1. 规则初筛（在详情抓取之前）------------------------------
        summary = screen_all(listed, target)
        report.kept = len(summary.kept)
        report.screen_reasons = summary.reasons
        report.dropped = summary.dropped

        # --- 2. 增量补全文 --------------------------------------------
        prepared: list[tuple[RawJob, FilterResult]] = []
        for job, res in summary.kept:
            existing = _existing(conn, job)
            if not adapter.provides_jd_in_list and fetch_details:
                if _can_reuse_jd(adapter.supports_incremental, existing, job):
                    job = job.with_jd(existing["jd_text"])
                else:
                    if report.detail_fetches and delay:
                        time.sleep(delay)
                    job = adapter.fetch_detail(client, token, job)
                    report.detail_fetches += 1
            prepared.append((job, res))

        # --- 3. 入库 ---------------------------------------------------
        if not dry_run:
            for job, res in prepared:
                created = _upsert(conn, company, job, res, now=now)
                if created:
                    report.new += 1
                    report.new_jobs.append(_summarize(conn, company, job, res))
                else:
                    report.updated += 1

            # --- 4. 过期检测：比对接口返回的【全集】--------------------
            report.deactivated = _mark_missing(
                conn, company, {j.external_id for j in listed}
            )
            conn.commit()
        else:
            report.new = sum(1 for j, _ in prepared if _existing(conn, j) is None)
            report.updated = report.kept - report.new

        report.ok = True
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        report.error = f"{type(exc).__name__}: {exc}"
    finally:
        if owns_client:
            client.close()

    _record_run(conn, company, report, dry_run=dry_run)
    return report


def fetch_all(
    conn: sqlite3.Connection,
    target: dict[str, Any],
    *,
    only: str | None = None,
    dry_run: bool = False,
    fetch_details: bool = True,
    delay: float = DETAIL_DELAY_SECONDS,
) -> list[FetchReport]:
    sql = "SELECT * FROM companies WHERE is_active = 1"
    params: tuple[Any, ...] = ()
    if only:
        sql += " AND lower(name) = lower(?)"
        params = (only,)
    sql += " ORDER BY priority, name"
    companies = conn.execute(sql, params).fetchall()

    reports: list[FetchReport] = []
    with http_client() as client:
        for company in companies:
            reports.append(
                fetch_company(
                    conn, company, target,
                    client=client, dry_run=dry_run,
                    fetch_details=fetch_details, delay=delay,
                )
            )
    return reports


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------

def _existing(conn: sqlite3.Connection, job: RawJob) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM jobs WHERE source = ? AND external_id = ?",
        (job.source, job.external_id),
    ).fetchone()


def _can_reuse_jd(
    supports_incremental: bool, existing: sqlite3.Row | None, job: RawJob
) -> bool:
    """能不能跳过这次详情请求，直接用库里已有的 JD 全文。

    只有当适配器提供了可比较的更新时间戳、库里那条的时间戳和这次拿到的一致、
    且 JD 全文确实已经存下来了，才敢复用。三个条件缺一不可——
    尤其是最后一条：上次抓取如果在详情阶段失败，库里会有行但 jd_text 是空的。
    """
    if not supports_incremental or existing is None:
        return False
    if not existing["jd_text"]:
        return False
    return bool(job.source_updated_at) and existing["source_updated_at"] == job.source_updated_at


def _upsert(
    conn: sqlite3.Connection,
    company: sqlite3.Row,
    job: RawJob,
    res: FilterResult,
    *,
    now: datetime | None = None,
) -> bool:
    """写入或更新一个岗位。返回 True 表示是新岗位。"""
    ts = (now or datetime.now()).isoformat(sep=" ", timespec="seconds")
    existing = _existing(conn, job)
    location = job.location or (job.all_locations[0] if job.all_locations else "")

    if existing is None:
        conn.execute(
            "INSERT INTO jobs (company_id, source, external_id, title, location, "
            "remote_type, url, jd_text, salary_raw, posted_at, first_seen_at, "
            "last_seen_at, is_active, miss_count, content_hash, source_updated_at, screen_tier) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,0,?,?,?)",
            (
                company["id"], job.source, job.external_id, job.title, location,
                job.remote_type, job.url, job.jd_text, job.salary_raw, job.posted_at,
                ts, ts, job.content_hash(), job.source_updated_at, res.tier,
            ),
        )
        return True

    # 岗位还在：重置缺席计数，必要时复活
    conn.execute(
        "UPDATE jobs SET title=?, location=?, remote_type=?, url=?, "
        "jd_text=COALESCE(?, jd_text), salary_raw=?, posted_at=?, last_seen_at=?, "
        "is_active=1, miss_count=0, content_hash=?, source_updated_at=?, screen_tier=? "
        "WHERE id=?",
        (
            job.title, location, job.remote_type, job.url, job.jd_text,
            job.salary_raw, job.posted_at, ts, job.content_hash(),
            job.source_updated_at, res.tier, existing["id"],
        ),
    )
    return False


def _mark_missing(
    conn: sqlite3.Connection, company: sqlite3.Row, seen_ids: set[str]
) -> int:
    """这次抓取没出现的岗位，缺席计数 +1；连续两次没出现就判下架。

    用计数而不是「一次没看到就下架」，是为了容忍接口偶发的不完整返回。
    """
    rows = conn.execute(
        "SELECT id, external_id FROM jobs WHERE company_id = ? AND is_active = 1",
        (company["id"],),
    ).fetchall()

    deactivated = 0
    for row in rows:
        if row["external_id"] in seen_ids:
            continue
        conn.execute("UPDATE jobs SET miss_count = miss_count + 1 WHERE id = ?", (row["id"],))
        miss = conn.execute(
            "SELECT miss_count FROM jobs WHERE id = ?", (row["id"],)
        ).fetchone()["miss_count"]
        if miss >= MISS_LIMIT:
            conn.execute("UPDATE jobs SET is_active = 0 WHERE id = ?", (row["id"],))
            deactivated += 1
    return deactivated


def _summarize(
    conn: sqlite3.Connection, company: sqlite3.Row, job: RawJob, res: FilterResult
) -> dict[str, Any]:
    """给推送用的摘要，带上这家公司的内推线索。

    路线图第 8 点：认识人就先要内推，别直接投。所以内推人要和岗位一起推给你，
    否则你看到岗位的第一反应永远是「去投」。
    """
    contacts = conn.execute(
        "SELECT name, relationship, strength FROM contacts WHERE company_id = ? "
        "ORDER BY strength DESC, name",
        (company["id"],),
    ).fetchall()
    return {
        "company": company["name"],
        "title": job.title,
        "location": job.location or (job.all_locations[0] if job.all_locations else ""),
        "url": job.url,
        "salary": job.salary_raw,
        "tier": res.tier,
        "contacts": [dict(c) for c in contacts],
    }


def _record_run(
    conn: sqlite3.Connection, company: sqlite3.Row, report: FetchReport, *, dry_run: bool
) -> None:
    if dry_run:
        return
    conn.execute(
        "INSERT INTO fetch_runs (company_id, source, finished_at, ok, listed_count, "
        "kept_count, new_count, updated_count, detail_fetches, deactivated, error) "
        "VALUES (?,?,datetime('now'),?,?,?,?,?,?,?,?)",
        (
            company["id"], report.source, 1 if report.ok else 0, report.listed,
            report.kept, report.new, report.updated, report.detail_fetches,
            report.deactivated, report.error,
        ),
    )
    conn.commit()


def failing_sources(conn: sqlite3.Connection, *, limit: int = 20) -> list[sqlite3.Row]:
    """最近一次抓取失败的公司。

    抓取器静默失败是最危险的失败模式：你会以为「最近没什么新岗位」，
    实际是适配器挂了两周。所以每次 fetch 结束都要报一次。
    """
    return conn.execute(
        """
        SELECT c.name, r.source, r.error, r.started_at
        FROM companies c
        JOIN fetch_runs r ON r.id = (
            SELECT id FROM fetch_runs WHERE company_id = c.id ORDER BY started_at DESC, id DESC LIMIT 1
        )
        WHERE r.ok = 0
        ORDER BY r.started_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
