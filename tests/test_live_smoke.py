"""冒烟测试：打真实 ATS 接口。

    pytest --live -k smoke

路线图 Phase 1 的「建议」：每个适配器写一个冒烟测试，拉一家已知公司、
断言字段非空。理由是这些公开接口偶尔会改格式或下线，而抓取器**静默失败**
是最危险的失败模式——你会以为最近没什么新岗位，实际是适配器挂了两周。

默认跳过，因为单元测试必须离线且确定。这些是给你手动跑、或放进
每周一次的定时任务里用的。
"""

import pytest

from jha.sources import http_client
from jha.sources.ashby import AshbyAdapter
from jha.sources.greenhouse import GreenhouseAdapter
from jha.sources.lever import LeverAdapter

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def client():
    with http_client() as c:
        yield c


def _assert_sane(jobs, source):
    assert jobs, f"{source} 一个岗位都没返回"
    for j in jobs[:20]:
        assert j.source == source
        assert j.external_id, "external_id 为空——去重会整个失效"
        assert j.title.strip(), "title 为空"
        assert j.url, "url 为空——你点不进去"
        assert j.title == j.title.strip(), "标题有首尾空格，归一化没做干净"


def test_greenhouse_smoke(client):
    jobs = GreenhouseAdapter().list_jobs(client, "databricks")
    _assert_sane(jobs, "greenhouse")
    # 增量抓取完全依赖这个字段，没了就退化成每次全量拉 JD
    assert all(j.source_updated_at for j in jobs[:20]), "updated_at 不见了"
    assert all(j.jd_text is None for j in jobs[:20]), "列表阶段不该带 JD"


def test_greenhouse_detail_smoke(client):
    a = GreenhouseAdapter()
    jobs = a.list_jobs(client, "databricks")
    filled = a.fetch_detail(client, "databricks", jobs[0])
    assert filled.jd_text and len(filled.jd_text) > 200
    assert "&lt;" not in filled.jd_text, "HTML 实体没解开"


def test_lever_smoke(client):
    jobs = LeverAdapter().list_jobs(client, "leverdemo")
    _assert_sane(jobs, "lever")
    # 真实板子上确实存在描述为空的占位岗位，所以断言「大部分有」而不是「全都有」
    with_jd = [j for j in jobs if j.jd_text]
    assert len(with_jd) >= len(jobs) // 2, "Lever 应该在列表里就带 JD"
    # 拼接 bug 的回归防线：正文不该出现两遍
    for j in with_jd[:10]:
        head = j.jd_text[:80]
        assert j.jd_text.count(head) == 1, f"JD 重复了：{j.title}"
    for j in jobs[:10]:
        if j.posted_at:
            assert j.posted_at.startswith("20"), f"epoch 毫秒没转换：{j.posted_at}"


def test_ashby_smoke(client):
    jobs = AshbyAdapter().list_jobs(client, "ramp")
    _assert_sane(jobs, "ashby")
    with_jd = [j for j in jobs if j.jd_text]
    assert len(with_jd) >= len(jobs) // 2, "Ashby 应该在列表里就带 JD"
    assert any(j.salary_raw for j in jobs), "includeCompensation 没生效——薪资要退回给 LLM 猜了"


def test_greenhouse_list_is_much_smaller_without_content(client):
    """确认两段抓取的前提还成立。

    实测 745KB vs 9.5MB。哪天 Greenhouse 把 JD 塞进列表接口，
    这个测试会挂——那时增量策略就该重新设计了。
    """
    small = client.get("https://boards-api.greenhouse.io/v1/boards/databricks/jobs")
    assert small.status_code == 200
    assert len(small.content) < 3_000_000, "列表接口变大了，两段抓取的前提要重新评估"
