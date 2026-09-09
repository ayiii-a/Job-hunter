"""规则初筛的测试。

这道过滤是承重墙：单家公司 870 个岗位、178 个地点，它决定 Phase 2 的成本。
但它也是最容易**静默出错**的地方——被误杀的岗位不入库，你永远不会知道
自己漏了什么。所以这里把每条误杀路径都钉死。
"""

import pytest

from jha.filters import screen, screen_all
from jha.sources import RawJob

TARGET = {
    "titles_include": ["AI Engineer", "Machine Learning Engineer", "Software Engineer"],
    "titles_exclude": ["Senior", "Staff", "Principal", "Manager", "Intern"],
    "title_tiers": {
        "tier1_ai": ["AI Engineer"],
        "tier2_mle": ["Machine Learning Engineer"],
        "tier3_swe": ["Software Engineer"],
    },
    "locations": ["United States", "USA", "US-", "Remote"],
    "locations_exclude": ["India", "Canada", "Japan", "United Kingdom"],
    "remote_ok": True,
}


def job(title="AI Engineer", location="Boston, MA, United States", **kw):
    return RawJob(
        source="greenhouse", external_id="1", title=title,
        company_name="Acme", url="", location=location, **kw
    )


# ---------------------------------------------------------------------------
# 词边界 —— 这一组是最重要的
# ---------------------------------------------------------------------------

def test_india_does_not_match_indianapolis():
    # 裸子串匹配会把 Indianapolis 当成 India 排掉，而且因为是排除项，
    # 你永远不会发现自己漏了整个印第安纳州的岗位
    res = screen(job(location="Indianapolis, IN, United States"), TARGET)
    assert res.passed, res.reason


def test_india_does_not_match_indiana():
    assert screen(job(location="Indiana, United States"), TARGET).passed


def test_india_still_excluded():
    res = screen(job(location="Bengaluru, India"), TARGET)
    assert res.rejected and "India" in res.reason


def test_intern_does_not_match_internal():
    # "Internal Tools Engineer" 不该被 Intern 排除
    res = screen(job(title="Internal Tools Software Engineer"), TARGET)
    assert res.passed, res.reason


def test_intern_does_not_match_international():
    assert screen(job(title="International Software Engineer"), TARGET).passed


def test_intern_still_excluded():
    res = screen(job(title="Software Engineer Intern"), TARGET)
    assert res.rejected and "Intern" in res.reason


def test_us_dash_pattern_matches():
    # "US-" 以连字符结尾，尾部不能加 \b，否则永远匹配不上
    assert screen(job(location="US-Remote"), TARGET).passed


# ---------------------------------------------------------------------------
# 地点：排除必须先于 remote_ok
# ---------------------------------------------------------------------------

def test_remote_canada_is_excluded_not_admitted_by_remote_ok():
    # 顺序反了的话，remote_ok 会把加拿大远程岗位放进来——而你没有加拿大工作许可
    res = screen(job(location="Remote (Canada)"), TARGET)
    assert res.rejected and "Canada" in res.reason


def test_remote_us_passes():
    assert screen(job(location="Remote (US)"), TARGET).passed


def test_one_excluded_location_does_not_kill_a_valid_one():
    # 实测 bug：Ramp 的纽约岗位同时提供 Remote (Canada)，
    # 拿所有地点拼成一串去匹配排除项，整批纽约岗位被 Canada 杀掉了。
    # 正确语义是「只要有一个地点可接受，岗位就可接受」
    res = screen(
        job(
            location="New York, NY (HQ)",
            all_locations=("Remote (Canada)", "Remote (US)"),
        ),
        TARGET,
    )
    assert res.passed, res.reason


def test_all_locations_excluded_still_rejects():
    res = screen(
        job(location="Toronto, Canada", all_locations=("Bengaluru, India",)), TARGET
    )
    assert res.rejected and "地点排除" in res.reason


def test_remote_type_counts_as_a_location_candidate():
    # 有些源把远程只写在 workplaceType 里
    res = screen(job(location="", all_locations=(), remote_type="Remote"), TARGET)
    assert res.passed


def test_remote_found_in_secondary_locations():
    # 主地点是总部、Remote (US) 在次要地点里 —— 这是 Ashby 的常见形态
    res = screen(
        job(location="New York, NY (HQ)", all_locations=("Remote (US)",)), TARGET
    )
    assert res.passed


def test_foreign_location_rejected():
    res = screen(job(location="Tokyo, Japan"), TARGET)
    assert res.rejected


def test_unmatched_location_rejected():
    res = screen(job(location="Zurich"), TARGET)
    assert res.rejected and "地点不匹配" in res.reason


def test_empty_location_rejected_with_clear_reason():
    res = screen(job(location=""), TARGET)
    assert res.rejected and "地点为空" in res.reason


def test_remote_only_mode():
    target = {**TARGET, "remote_only": True}
    assert screen(job(location="Boston, MA, United States"), target).rejected
    assert screen(job(location="Remote (US)"), target).passed


# ---------------------------------------------------------------------------
# 标题
# ---------------------------------------------------------------------------

def test_title_exclude_beats_include():
    res = screen(job(title="Senior AI Engineer"), TARGET)
    assert res.rejected and "Senior" in res.reason


def test_sr_abbreviation_excluded_both_spellings():
    # 实测漏网：Databricks 写 "Sr Software Engineer"（无点），
    # 只配 "Senior" 或 "Sr." 都拦不住，七个高级岗位混进了应届列表
    target = {**TARGET, "titles_exclude": [*TARGET["titles_exclude"], "Sr"]}
    for title in ("Sr Software Engineer", "Sr. Software Engineer"):
        res = screen(job(title=title), target)
        assert res.rejected, f"{title} 该被排除"


def test_sr_does_not_match_inside_words():
    target = {**TARGET, "titles_exclude": [*TARGET["titles_exclude"], "Sr"]}
    # 词边界保证 "Sr" 不会咬到别的词
    assert screen(job(title="SRE Software Engineer"), target).passed


def test_title_not_in_scope_rejected():
    res = screen(job(title="Product Designer"), TARGET)
    assert res.rejected and "标题不在目标范围内" in res.reason


def test_new_grad_variants_pass():
    # 应届岗位常见的几种写法都必须过 —— 模板原来把 New Grad 放排除项里，正好反了
    for title in (
        "Software Engineer, New Grad",
        "Software Engineer I",
        "AI Engineer - University Graduate",
        "Machine Learning Engineer (Early Career)",
    ):
        assert screen(job(title=title), TARGET).passed, title


def test_tier_is_reported():
    assert screen(job(title="AI Engineer"), TARGET).tier == "tier1_ai"
    assert screen(job(title="Machine Learning Engineer"), TARGET).tier == "tier2_mle"


def test_tier_is_none_when_no_tiers_configured():
    target = {k: v for k, v in TARGET.items() if k != "title_tiers"}
    res = screen(job(), target)
    assert res.passed and res.tier is None


def test_matching_is_case_insensitive():
    assert screen(job(title="ai engineer", location="remote (us)"), TARGET).passed


# ---------------------------------------------------------------------------
# 批量与统计
# ---------------------------------------------------------------------------

def test_screen_all_splits_and_counts_reasons():
    jobs = [
        job(title="AI Engineer"),
        job(title="Senior AI Engineer"),
        job(title="AI Engineer", location="Bengaluru, India"),
        job(title="Product Designer"),          # 既不在 include 也不在 exclude
        job(title="AI Engineer", location="Toronto, Canada"),
    ]
    s = screen_all(jobs, TARGET)
    assert len(s.kept) == 1
    assert len(s.dropped) == 4
    assert s.reasons["地点排除"] == 2
    assert s.reasons["标题排除"] == 1           # Senior AI Engineer
    assert s.reasons["标题不在目标范围内"] == 1  # Product Designer


def test_screen_all_on_empty_input():
    s = screen_all([], TARGET)
    assert s.kept == [] and s.dropped == [] and s.reasons == {}
