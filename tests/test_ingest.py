"""抓取管线的测试。

重点是两个顺序问题和一个省钱机制：
  1. 规则初筛必须跑在详情抓取之前（否则 870 个岗位全要拉全文）
  2. 过期检测比对接口全集，不是初筛后的子集（否则改一次过滤条件就把库弄脏）
  3. Greenhouse 的 updated_at 没变就不该再打详情请求
"""

from datetime import datetime, timedelta

import httpx
import pytest

from jha import db, ingest
from jha.sources import RawJob
from jha.sources.base import Adapter

T0 = datetime(2026, 9, 1, 9, 0, 0)

TARGET = {
    "titles_include": ["AI Engineer", "Software Engineer"],
    "titles_exclude": ["Senior"],
    "title_tiers": {"tier1": ["AI Engineer"]},
    "locations": ["United States", "Remote"],
    "locations_exclude": ["India"],
    "remote_ok": True,
}


class FakeAdapter(Adapter):
    """可控的假适配器，用来精确断言调用次数。"""

    name = "greenhouse"          # 借用注册名，好让 fetch_company 找得到

    def __init__(self, jobs, *, provides_jd=False, incremental=True, fail=None):
        self._jobs = jobs
        self.provides_jd_in_list = provides_jd
        self.supports_incremental = incremental
        self.detail_calls: list[str] = []
        self._fail = fail

    def list_jobs(self, client, token):
        if self._fail:
            raise self._fail
        return list(self._jobs)

    def fetch_detail(self, client, token, job):
        self.detail_calls.append(job.external_id)
        return job.with_jd(f"JD for {job.external_id}")


def mkjob(ext_id, title="AI Engineer", location="Boston, United States", updated="u1", **kw):
    return RawJob(
        source="greenhouse", external_id=ext_id, title=title, company_name="Acme",
        url=f"https://x/{ext_id}", location=location, source_updated_at=updated, **kw
    )


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    c.execute("INSERT INTO companies (name, ats_type, board_token) VALUES ('Acme','greenhouse','acme')")
    c.commit()
    yield c
    c.close()


@pytest.fixture
def company(conn):
    return conn.execute("SELECT * FROM companies WHERE name='Acme'").fetchone()


def run(conn, company, adapter, monkeypatch, **kw):
    monkeypatch.setattr(ingest, "get_adapter", lambda _: adapter)
    kw.setdefault("delay", 0.0)
    kw.setdefault("now", T0)
    return ingest.fetch_company(
        conn, company, TARGET, client=httpx.Client(), **kw
    )


# ---------------------------------------------------------------------------
# 初筛在详情之前
# ---------------------------------------------------------------------------

def test_filter_runs_before_detail_fetch(conn, company, monkeypatch):
    # 3 个岗位只有 1 个能过初筛 —— 详情请求就该只有 1 次，不是 3 次。
    # 这条是整个 Phase 1 最重要的省钱机制
    a = FakeAdapter([
        mkjob("1", title="AI Engineer"),
        mkjob("2", title="Senior AI Engineer"),
        mkjob("3", location="Bengaluru, India"),
    ])
    rep = run(conn, company, a, monkeypatch)
    assert rep.listed == 3
    assert rep.kept == 1
    assert a.detail_calls == ["1"]


def test_no_detail_flag_skips_all_detail_requests(conn, company, monkeypatch):
    a = FakeAdapter([mkjob("1")])
    rep = run(conn, company, a, monkeypatch, fetch_details=False)
    assert a.detail_calls == []
    assert rep.new == 1


def test_adapter_that_provides_jd_never_fetches_detail(conn, company, monkeypatch):
    # Lever / Ashby 列表里就带 JD，一次详情请求都不该发
    a = FakeAdapter([mkjob("1", jd_text="already here")], provides_jd=True, incremental=False)
    rep = run(conn, company, a, monkeypatch)
    assert a.detail_calls == []
    assert rep.detail_fetches == 0
    assert conn.execute("SELECT jd_text FROM jobs").fetchone()["jd_text"] == "already here"


# ---------------------------------------------------------------------------
# 增量：updated_at 没变就复用
# ---------------------------------------------------------------------------

def test_unchanged_job_reuses_stored_jd(conn, company, monkeypatch):
    a1 = FakeAdapter([mkjob("1", updated="u1")])
    run(conn, company, a1, monkeypatch)
    assert a1.detail_calls == ["1"]

    a2 = FakeAdapter([mkjob("1", updated="u1")])       # 时间戳没变
    rep = run(conn, company, a2, monkeypatch)
    assert a2.detail_calls == []                        # 不再打详情
    assert rep.detail_fetches == 0
    assert rep.new == 0 and rep.updated == 1


def test_changed_updated_at_triggers_refetch(conn, company, monkeypatch):
    run(conn, company, FakeAdapter([mkjob("1", updated="u1")]), monkeypatch)
    a2 = FakeAdapter([mkjob("1", updated="u2")])
    run(conn, company, a2, monkeypatch)
    assert a2.detail_calls == ["1"]


def test_missing_jd_forces_refetch_even_if_timestamp_matches(conn, company, monkeypatch):
    # 上次在详情阶段挂了，库里有行但 jd_text 是空的。
    # 光比时间戳会永远跳过它，那条岗位的 JD 就永远补不上
    run(conn, company, FakeAdapter([mkjob("1", updated="u1")]), monkeypatch, fetch_details=False)
    assert conn.execute("SELECT jd_text FROM jobs").fetchone()["jd_text"] is None

    a2 = FakeAdapter([mkjob("1", updated="u1")])
    run(conn, company, a2, monkeypatch)
    assert a2.detail_calls == ["1"]


def test_non_incremental_adapter_always_refetches(conn, company, monkeypatch):
    a1 = FakeAdapter([mkjob("1")], incremental=False)
    run(conn, company, a1, monkeypatch)
    a2 = FakeAdapter([mkjob("1")], incremental=False)
    run(conn, company, a2, monkeypatch)
    assert a2.detail_calls == ["1"]


# ---------------------------------------------------------------------------
# 过期检测
# ---------------------------------------------------------------------------

def _active(conn, ext):
    return conn.execute("SELECT is_active FROM jobs WHERE external_id=?", (ext,)).fetchone()["is_active"]


def test_job_deactivated_after_two_consecutive_misses(conn, company, monkeypatch):
    run(conn, company, FakeAdapter([mkjob("1"), mkjob("2")]), monkeypatch)
    assert _active(conn, "2") == 1

    run(conn, company, FakeAdapter([mkjob("1")]), monkeypatch)      # 第一次没出现
    assert _active(conn, "2") == 1                                  # 容忍一次

    run(conn, company, FakeAdapter([mkjob("1")]), monkeypatch)      # 第二次
    assert _active(conn, "2") == 0


def test_reappearing_job_resets_miss_count_and_revives(conn, company, monkeypatch):
    run(conn, company, FakeAdapter([mkjob("1"), mkjob("2")]), monkeypatch)
    run(conn, company, FakeAdapter([mkjob("1")]), monkeypatch)
    run(conn, company, FakeAdapter([mkjob("1")]), monkeypatch)
    assert _active(conn, "2") == 0

    run(conn, company, FakeAdapter([mkjob("1"), mkjob("2")]), monkeypatch)
    assert _active(conn, "2") == 1
    assert conn.execute("SELECT miss_count FROM jobs WHERE external_id='2'").fetchone()["miss_count"] == 0


def test_expiry_compares_against_full_listing_not_filtered_subset(conn, company, monkeypatch):
    # 关键正确性点：岗位还在招，只是不再匹配你的过滤条件（比如你改了 target_profile）。
    # 拿初筛后的子集做比对，会把它误判成下架，数据就脏了
    run(conn, company, FakeAdapter([mkjob("1"), mkjob("2")]), monkeypatch)
    assert _active(conn, "2") == 1

    still_listed_but_filtered = [mkjob("1"), mkjob("2", title="Senior AI Engineer")]
    run(conn, company, FakeAdapter(still_listed_but_filtered), monkeypatch)
    run(conn, company, FakeAdapter(still_listed_but_filtered), monkeypatch)
    assert _active(conn, "2") == 1, "岗位仍在接口返回里，不该被判下架"


# ---------------------------------------------------------------------------
# 入库内容
# ---------------------------------------------------------------------------

def test_new_job_stores_tier_and_hash(conn, company, monkeypatch):
    run(conn, company, FakeAdapter([mkjob("1")]), monkeypatch)
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["screen_tier"] == "tier1"
    assert row["content_hash"]
    assert row["company_id"] == company["id"]


def test_update_does_not_wipe_jd_with_null(conn, company, monkeypatch):
    # 第二次抓取如果没带 JD（比如 --no-detail），不能把已存的全文清掉
    run(conn, company, FakeAdapter([mkjob("1")]), monkeypatch)
    assert conn.execute("SELECT jd_text FROM jobs").fetchone()["jd_text"]
    run(conn, company, FakeAdapter([mkjob("1")]), monkeypatch, fetch_details=False)
    assert conn.execute("SELECT jd_text FROM jobs").fetchone()["jd_text"]


def test_dry_run_writes_nothing(conn, company, monkeypatch):
    rep = run(conn, company, FakeAdapter([mkjob("1")]), monkeypatch, dry_run=True)
    assert rep.ok and rep.kept == 1
    assert conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM fetch_runs").fetchone()["c"] == 0


def test_new_job_summary_includes_contacts(conn, company, monkeypatch):
    # 内推提示必须和岗位一起推出来，否则你看到岗位的第一反应永远是「去投」
    conn.execute(
        "INSERT INTO contacts (company_id, name, relationship, strength) VALUES (?,?,?,?)",
        (company["id"], "Wei", "校友", 3),
    )
    conn.commit()
    rep = run(conn, company, FakeAdapter([mkjob("1")]), monkeypatch)
    assert rep.new_jobs[0]["contacts"][0]["name"] == "Wei"


# ---------------------------------------------------------------------------
# 失败处理与告警
# ---------------------------------------------------------------------------

def test_http_failure_is_recorded_not_raised(conn, company, monkeypatch):
    a = FakeAdapter([], fail=httpx.ConnectError("boom"))
    rep = run(conn, company, a, monkeypatch)
    assert rep.ok is False and "boom" in rep.error
    row = conn.execute("SELECT * FROM fetch_runs").fetchone()
    assert row["ok"] == 0 and "boom" in row["error"]


def test_failing_sources_reports_last_failed_run(conn, company, monkeypatch):
    run(conn, company, FakeAdapter([], fail=httpx.ConnectError("down")), monkeypatch)
    failures = ingest.failing_sources(conn)
    assert len(failures) == 1 and failures[0]["name"] == "Acme"


def test_recovery_clears_the_alert(conn, company, monkeypatch):
    run(conn, company, FakeAdapter([], fail=httpx.ConnectError("down")), monkeypatch)
    run(conn, company, FakeAdapter([mkjob("1")]), monkeypatch)
    assert ingest.failing_sources(conn) == []


def test_unsupported_ats_reports_clearly(conn, monkeypatch):
    conn.execute("INSERT INTO companies (name, ats_type, board_token) VALUES ('Big','workday','big')")
    conn.commit()
    row = conn.execute("SELECT * FROM companies WHERE name='Big'").fetchone()
    rep = ingest.fetch_company(conn, row, TARGET, delay=0.0)
    assert rep.ok is False and "Workday" in rep.error


def test_missing_board_token_reports_clearly(conn):
    conn.execute("INSERT INTO companies (name, ats_type) VALUES ('NoTok','greenhouse')")
    conn.commit()
    row = conn.execute("SELECT * FROM companies WHERE name='NoTok'").fetchone()
    rep = ingest.fetch_company(conn, row, TARGET, delay=0.0)
    assert rep.ok is False and "resolve-ats" in rep.error


def test_fetch_run_records_cost_visibility(conn, company, monkeypatch):
    run(conn, company, FakeAdapter([mkjob("1"), mkjob("2"), mkjob("3", location="Bengaluru, India")]), monkeypatch)
    row = conn.execute("SELECT * FROM fetch_runs").fetchone()
    assert row["listed_count"] == 3
    assert row["kept_count"] == 2
    assert row["detail_fetches"] == 2      # 只对过了初筛的抓详情
