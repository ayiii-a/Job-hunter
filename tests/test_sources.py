"""三个 ATS 适配器的归一化测试。

全部离线：用 tests/fixtures/ 里从真实接口抓下来的样本 + httpx.MockTransport。
真正打网络的冒烟测试在 test_live_smoke.py，默认跳过。

重点测的是三家的**差异**——那正是路线图强调「不能用一个 fetch() 糊过去」的地方：
Lever 的 epoch 毫秒、Ashby 的 secondaryLocations 和前导空格、
Greenhouse 的转义 HTML 和两段抓取。
"""

import json
from pathlib import Path

import httpx
import pytest

from jha.sources import ADAPTERS, get_adapter
from jha.sources.ashby import AshbyAdapter
from jha.sources.base import RawJob, epoch_ms_to_iso, html_to_text
from jha.sources.greenhouse import GreenhouseAdapter
from jha.sources.lever import LeverAdapter

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def client_returning(payload, *, record=None):
    """构造一个永远返回 payload 的 httpx.Client。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        body = payload(request) if callable(payload) else payload
        return httpx.Response(200, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Greenhouse
# ---------------------------------------------------------------------------

def test_greenhouse_normalizes_list():
    a = GreenhouseAdapter()
    with client_returning(load("greenhouse_list.json")) as c:
        jobs = a.list_jobs(c, "databricks")
    assert jobs
    j = jobs[0]
    assert j.source == "greenhouse"
    assert j.external_id and j.title and j.url
    assert j.source_updated_at            # 有 updated_at，能做增量
    assert j.jd_text is None              # 列表不带 JD


def test_greenhouse_declares_its_asymmetry():
    a = GreenhouseAdapter()
    assert a.provides_jd_in_list is False
    assert a.supports_incremental is True


def test_greenhouse_detail_fills_jd_and_unescapes_html():
    a = GreenhouseAdapter()
    job = RawJob(source="greenhouse", external_id="1", title="T", company_name="X", url="")
    with client_returning(load("greenhouse_detail.json")) as c:
        filled = a.fetch_detail(c, "databricks", job)
    assert filled.jd_text
    # content 是转义过的 HTML；转换后不该再看到标签或实体
    assert "&lt;" not in filled.jd_text
    assert "<p" not in filled.jd_text


def test_greenhouse_handles_non_ascii_titles():
    # 实测板子里有日文岗位标题
    with client_returning(load("greenhouse_list.json")) as c:
        jobs = GreenhouseAdapter().list_jobs(c, "databricks")
    assert any(any(ord(ch) > 127 for ch in j.title) for j in jobs)


def test_greenhouse_list_call_does_not_request_content():
    # 带 content=true 是 9.5MB，不带是 745KB。列表阶段绝不能带
    record = []
    with client_returning(load("greenhouse_list.json"), record=record) as c:
        GreenhouseAdapter().list_jobs(c, "databricks")
    assert "content" not in str(record[0].url)


# ---------------------------------------------------------------------------
# Lever
# ---------------------------------------------------------------------------

def test_lever_normalizes_and_includes_jd():
    a = LeverAdapter()
    with client_returning(load("lever_list.json")) as c:
        jobs = a.list_jobs(c, "leverdemo")
    assert jobs
    j = jobs[0]
    assert j.source == "lever"
    assert j.jd_text                      # 列表里直接带 JD，不需要详情请求
    assert a.provides_jd_in_list is True
    assert a.supports_incremental is False


def test_lever_converts_epoch_millis_to_iso():
    # createdAt 是 epoch 毫秒，不转换的话 posted_at 列里会混进 1565990241800
    with client_returning(load("lever_list.json")) as c:
        jobs = LeverAdapter().list_jobs(c, "leverdemo")
    posted = jobs[0].posted_at
    assert posted and posted.startswith("20") and "T" in posted


def test_lever_does_not_duplicate_description():
    # descriptionBodyPlain 是 descriptionPlain 的【子串】，不是补充。
    # 天真拼接会让正文出现两遍：存储翻倍事小，Phase 2 每个岗位的 LLM
    # token 多出七成才是真花钱。冒烟测试就是抓到这个 bug 的
    raw = [{
        "id": "1", "text": "Engineer", "hostedUrl": "u",
        "descriptionPlain": "INTRO\n\nBODY TEXT HERE",
        "descriptionBodyPlain": "BODY TEXT HERE",
    }]
    with client_returning(raw) as c:
        job = LeverAdapter().list_jobs(c, "leverdemo")[0]
    assert job.jd_text.count("BODY TEXT HERE") == 1
    assert job.jd_text == "INTRO\n\nBODY TEXT HERE"


def test_lever_appends_body_when_genuinely_separate():
    # 万一 Lever 改了语义、body 不再包含于 plain，就该补上而不是丢掉
    raw = [{
        "id": "1", "text": "Engineer", "hostedUrl": "u",
        "descriptionPlain": "INTRO ONLY",
        "descriptionBodyPlain": "SEPARATE BODY",
    }]
    with client_returning(raw) as c:
        job = LeverAdapter().list_jobs(c, "leverdemo")[0]
    assert "INTRO ONLY" in job.jd_text and "SEPARATE BODY" in job.jd_text


def test_lever_empty_description_yields_none():
    # 真实板子上确实有描述为空的岗位
    raw = [{"id": "1", "text": "Engineer", "hostedUrl": "u",
            "descriptionPlain": "", "descriptionBodyPlain": ""}]
    with client_returning(raw) as c:
        assert LeverAdapter().list_jobs(c, "leverdemo")[0].jd_text is None


def test_lever_keeps_all_locations():
    with client_returning(load("lever_list.json")) as c:
        jobs = LeverAdapter().list_jobs(c, "leverdemo")
    assert any(j.all_locations for j in jobs)


def test_epoch_helper_rejects_garbage():
    assert epoch_ms_to_iso(None) is None
    assert epoch_ms_to_iso("2019-01-01") is None
    assert epoch_ms_to_iso(0) is None


# ---------------------------------------------------------------------------
# Ashby
# ---------------------------------------------------------------------------

def test_ashby_requests_compensation():
    # 加个参数就有结构化薪资，Phase 2 就不用让 LLM 从 JD 里猜
    record = []
    with client_returning(load("ashby_list.json"), record=record) as c:
        AshbyAdapter().list_jobs(c, "ramp")
    assert "includeCompensation=true" in str(record[0].url)


def test_ashby_extracts_structured_salary():
    with client_returning(load("ashby_list.json")) as c:
        jobs = AshbyAdapter().list_jobs(c, "ramp")
    assert any(j.salary_raw and "$" in j.salary_raw for j in jobs)


def test_ashby_strips_leading_space_in_title():
    # 实测 Ramp 有 " Security Engineer, Cloud"
    raw = load("ashby_list.json")
    raw["jobs"][0]["title"] = "  Padded Title  "
    with client_returning(raw) as c:
        job = AshbyAdapter().list_jobs(c, "ramp")[0]
    assert job.title == "Padded Title"


def test_ashby_keeps_secondary_locations():
    # 远程岗位常常主地点写总部、"Remote (US)" 躲在 secondaryLocations 里。
    # 丢了它，地点过滤会漏掉一批远程岗位
    with client_returning(load("ashby_list.json")) as c:
        jobs = AshbyAdapter().list_jobs(c, "ramp")
    assert any(j.all_locations for j in jobs)
    assert any("Remote" in j.location_blob for j in jobs)


def test_ashby_skips_unlisted():
    raw = load("ashby_list.json")
    for j in raw["jobs"]:
        j["isListed"] = False
    with client_returning(raw) as c:
        assert AshbyAdapter().list_jobs(c, "ramp") == []


# ---------------------------------------------------------------------------
# 公共
# ---------------------------------------------------------------------------

def test_html_to_text_breaks_blocks_into_lines():
    out = html_to_text("&lt;p&gt;one&lt;/p&gt;&lt;p&gt;two&lt;/p&gt;")
    assert "one" in out and "two" in out
    assert out.count("\n") >= 1          # 不能挤成一行


def test_html_to_text_handles_entities_and_nbsp():
    assert "Tom & Jerry" in html_to_text("&lt;p&gt;Tom &amp;amp; Jerry&lt;/p&gt;")
    assert "\xa0" not in html_to_text("&lt;p&gt;a&amp;nbsp;b&lt;/p&gt;")


def test_html_to_text_empty():
    assert html_to_text(None) == ""
    assert html_to_text("") == ""


def test_content_hash_stable_and_sensitive():
    base = dict(source="x", external_id="1", title="Engineer", company_name="Acme", url="u")
    a = RawJob(**base, location="Boston", jd_text="hello world")
    b = RawJob(**base, location="Boston", jd_text="hello world")
    c = RawJob(**base, location="Boston", jd_text="something else")
    assert a.content_hash() == b.content_hash()
    assert a.content_hash() != c.content_hash()


def test_content_hash_ignores_whitespace_and_case():
    base = dict(source="x", external_id="1", company_name="Acme", url="u", location="Boston")
    a = RawJob(**base, title="Senior  Engineer")
    b = RawJob(**base, title="senior engineer")
    assert a.content_hash() == b.content_hash()


def test_location_blob_merges_all_sources():
    j = RawJob(
        source="x", external_id="1", title="T", company_name="C", url="",
        location="New York, NY (HQ)", all_locations=("Remote (US)",), remote_type="Hybrid",
    )
    assert "New York" in j.location_blob
    assert "Remote (US)" in j.location_blob
    assert "Hybrid" in j.location_blob


@pytest.mark.parametrize("name", ["greenhouse", "lever", "ashby"])
def test_registry_lookup(name):
    assert get_adapter(name) is ADAPTERS[name]
    assert get_adapter(name.upper()) is ADAPTERS[name]


def test_registry_unknown():
    assert get_adapter("workday") is None
    assert get_adapter(None) is None
