"""母简历校验器的测试。

Phase 3 的防幻觉设计建立在「LLM 只输出 bullet ID」之上。这些测试守的就是
那个前提：ID 唯一、引用可解析。ID 一旦重复或悬空，选出来的内容会静默错位。
"""

import pytest

from jha import config, profile

MASTER_EXAMPLE, TARGET_EXAMPLE, COMPANIES_EXAMPLE = (t for t, _ in config.CONFIG_TEMPLATES)


def minimal():
    return {
        "basics": {
            "name": "A",
            "email": "a@b.c",
            "location": "Seattle",
            "work_authorization": "Authorized",
        },
        "skills": [{"id": "sk_py", "name": "Python"}],
        "experiences": [
            {
                "id": "exp_1",
                "company": "Acme",
                "tech": ["sk_py"],
                "bullets": [
                    {"id": "b_1", "text": "Did a thing", "skills": ["sk_py"], "metrics": True}
                ],
            }
        ],
        "qa_bank": {
            "work_authorization": "x",
            "salary_expectation": "y",
            "why_company_template": "z",
        },
        "story_bank": [
            {
                "id": "st_1",
                "title": "T",
                "linked_bullets": ["b_1"],
                "situation": "s", "task": "t", "action": "a", "result": "r",
            }
        ],
    }


def test_minimal_profile_is_valid():
    rep = profile.validate_master_profile(minimal())
    assert rep.ok, rep.errors
    assert rep.warnings == []


def test_duplicate_bullet_id_is_an_error():
    # 最致命的一种：LLM 选中这个 ID 时渲染器不知道该取哪条
    data = minimal()
    data["experiences"].append(
        {
            "id": "exp_2",
            "company": "Globex",
            "bullets": [{"id": "b_1", "text": "Other thing", "metrics": True}],
        }
    )
    rep = profile.validate_master_profile(data)
    assert not rep.ok
    assert any("bullet id 重复：b_1" in e for e in rep.errors)


def test_duplicate_bullet_id_detected_across_experiences_and_projects():
    data = minimal()
    data["projects"] = [
        {"id": "proj_1", "name": "P", "bullets": [{"id": "b_1", "text": "x", "metrics": True}]}
    ]
    rep = profile.validate_master_profile(data)
    assert any("bullet id 重复" in e for e in rep.errors)


def test_dangling_skill_reference_in_bullet():
    data = minimal()
    data["experiences"][0]["bullets"][0]["skills"] = ["sk_nope"]
    rep = profile.validate_master_profile(data)
    assert any("sk_nope" in e for e in rep.errors)


def test_dangling_skill_reference_in_experience_tech():
    data = minimal()
    data["experiences"][0]["tech"] = ["sk_ghost"]
    rep = profile.validate_master_profile(data)
    assert any("sk_ghost" in e for e in rep.errors)


def test_story_linking_to_missing_bullet():
    data = minimal()
    data["story_bank"][0]["linked_bullets"] = ["b_missing"]
    rep = profile.validate_master_profile(data)
    assert any("b_missing" in e for e in rep.errors)


def test_missing_bullet_id():
    data = minimal()
    del data["experiences"][0]["bullets"][0]["id"]
    rep = profile.validate_master_profile(data)
    assert any("没有 id" in e for e in rep.errors)


def test_empty_bullet_text():
    data = minimal()
    data["experiences"][0]["bullets"][0]["text"] = "   "
    rep = profile.validate_master_profile(data)
    assert any("text 为空" in e for e in rep.errors)


def test_missing_basics_fields():
    data = minimal()
    del data["basics"]["work_authorization"]
    rep = profile.validate_master_profile(data)
    assert any("work_authorization" in e for e in rep.errors)


def test_duplicate_skill_id():
    data = minimal()
    data["skills"].append({"id": "sk_py", "name": "Python again"})
    rep = profile.validate_master_profile(data)
    assert any("skill id 重复" in e for e in rep.errors)


def test_qa_bank_gap_is_a_warning_not_an_error():
    # 缺 qa_bank 条目不该挡住流程，只是投递时得手填
    data = minimal()
    del data["qa_bank"]["salary_expectation"]
    rep = profile.validate_master_profile(data)
    assert rep.ok
    assert any("salary_expectation" in w for w in rep.warnings)


def test_warns_when_most_bullets_lack_metrics():
    data = minimal()
    data["experiences"][0]["bullets"] += [
        {"id": "b_2", "text": "x", "metrics": False},
        {"id": "b_3", "text": "y", "metrics": False},
    ]
    rep = profile.validate_master_profile(data)
    assert rep.ok
    assert any("没有量化结果" in w for w in rep.warnings)


def test_stats_are_counted():
    rep = profile.validate_master_profile(minimal())
    assert rep.stats["bullets"] == 1
    assert rep.stats["skills"] == 1
    assert rep.stats["stories"] == 1


def test_template_leftovers_are_flagged():
    # 模板的示例内容结构上完全合法，忘了替换不会报错——
    # 最后就会生成一份写着 Acme Corp 的简历投出去。所以要单独查
    data = minimal()
    data["experiences"][0]["company"] = "Acme Corp"
    data["basics"]["name"] = "Your Name"
    rep = profile.validate_master_profile(data)
    assert rep.ok                                   # 不是错误，不该挡住流程
    assert any("模板的示例内容" in w for w in rep.warnings)


def test_no_leftover_warning_for_real_content():
    rep = profile.validate_master_profile(minimal())
    assert not any("模板的示例内容" in w for w in rep.warnings)


def test_shipped_template_is_detected_as_unfilled():
    # 随仓库发出去的那份模板，本来就该被认出来「还没填」
    rep = profile.validate_master_profile(profile.load_yaml(MASTER_EXAMPLE))
    assert any("模板的示例内容" in w for w in rep.warnings)


# --- 目标画像 --------------------------------------------------------------

def test_target_profile_requires_titles_include():
    rep = profile.validate_target_profile({"locations": ["Seattle"]})
    assert not rep.ok
    assert any("titles_include" in e for e in rep.errors)


def test_target_profile_requires_location_or_remote():
    rep = profile.validate_target_profile({"titles_include": ["SWE"]})
    assert not rep.ok
    assert any("locations" in e for e in rep.errors)


def test_tier_title_missing_from_include_is_an_error():
    # 在 tier 里加了岗位却忘了加进 titles_include，它会被第一道闸滤掉——
    # 你以为把它排进了主攻方向，实际它永远不会出现
    rep = profile.validate_target_profile(
        {
            "titles_include": ["AI Engineer"],
            "remote_ok": True,
            "title_tiers": {"tier1": ["AI Engineer", "LLM Engineer"]},
        }
    )
    assert not rep.ok
    assert any("LLM Engineer" in e for e in rep.errors)


def test_tier_titles_matching_include_are_fine():
    rep = profile.validate_target_profile(
        {
            "titles_include": ["AI Engineer", "LLM Engineer"],
            "titles_exclude": ["Senior"],
            "remote_ok": True,
            "title_tiers": {"tier1": ["AI Engineer"], "tier2": ["LLM Engineer"]},
        }
    )
    assert rep.ok
    assert rep.stats["tiers"] == 2


def test_tier_match_is_case_insensitive():
    rep = profile.validate_target_profile(
        {
            "titles_include": ["AI Engineer"],
            "remote_ok": True,
            "title_tiers": {"tier1": ["ai engineer"]},
        }
    )
    assert rep.ok


def test_unverified_stem_opt_is_flagged():
    rep = profile.validate_target_profile(
        {
            "titles_include": ["AI Engineer"],
            "titles_exclude": ["Senior"],
            "remote_ok": True,
            "visa": {"stem_opt_eligible": "unverified"},
        }
    )
    assert rep.ok
    assert any("stem_opt_eligible" in w for w in rep.warnings)


def test_sponsorship_without_hard_fail_phrases_is_flagged():
    rep = profile.validate_target_profile(
        {
            "titles_include": ["AI Engineer"],
            "titles_exclude": ["Senior"],
            "remote_ok": True,
            "visa": {"needs_sponsorship_eventually": True},
        }
    )
    assert any("hard_fail_phrases" in w for w in rep.warnings)


def test_target_profile_remote_ok_satisfies_location():
    rep = profile.validate_target_profile(
        {"titles_include": ["SWE"], "remote_ok": True, "titles_exclude": ["Intern"]}
    )
    assert rep.ok


# --- 随仓库附带的模板 -------------------------------------------------------
#
# 校验的是 *.example.yaml：进 git 的是它们，真实的 config/*.yaml 里有个人信息、
# 被 .gitignore 排除，新克隆的仓库里根本不存在。

def test_shipped_master_profile_template_validates():
    rep = profile.validate_master_profile(profile.load_yaml(MASTER_EXAMPLE))
    assert rep.ok, rep.errors


def test_shipped_target_profile_template_validates():
    rep = profile.validate_target_profile(profile.load_yaml(TARGET_EXAMPLE))
    assert rep.ok, rep.errors


def test_every_template_exists():
    # 少一个模板，agent init 就没法把它生成出来，而报错会推迟到用户跑 profile check
    for template, _ in config.CONFIG_TEMPLATES:
        assert template.exists(), f"缺模板 {template}"


def test_companies_template_parses():
    data = profile.load_yaml(COMPANIES_EXAMPLE)
    assert isinstance(data.get("companies"), list) and data["companies"]


# --- bootstrap：从模板生成真实配置 ------------------------------------------

def test_bootstrap_creates_missing_configs(tmp_path, monkeypatch):
    src = config.CONFIG_DIR
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    targets = []
    templates = []
    for template, target in config.CONFIG_TEMPLATES:
        copied = tmp_path / template.name
        copied.write_text(config.read_text(template), encoding="utf-8")
        templates.append(copied)
        targets.append(tmp_path / target.name)
    monkeypatch.setattr(config, "CONFIG_TEMPLATES", tuple(zip(templates, targets)))

    created = config.bootstrap_configs()
    assert set(created) == set(targets)
    assert all(t.exists() for t in targets)
    assert config.read_text(targets[0]) == config.read_text(src / "master_profile.example.yaml")


def test_bootstrap_never_overwrites_existing(tmp_path, monkeypatch):
    # 母简历是你写了两周的东西，重跑 init 绝不能把它冲掉
    template = tmp_path / "master_profile.example.yaml"
    target = tmp_path / "master_profile.yaml"
    template.write_text("from: template", encoding="utf-8")
    target.write_text("mine: precious", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_TEMPLATES", ((template, target),))

    assert config.bootstrap_configs() == []
    assert config.read_text(target) == "mine: precious"


def test_unchanged_from_template_is_detected(tmp_path, monkeypatch):
    # 一个字没改的配置结构上完全合法，结构校验永远查不出来
    template = tmp_path / "t.example.yaml"
    target = tmp_path / "t.yaml"
    template.write_text("titles_include: [SWE]\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_TEMPLATES", ((template, target),))

    config.bootstrap_configs()
    assert config.unchanged_from_template() == [target]

    config.write_text(target, "titles_include: [ML Engineer]\n")
    assert config.unchanged_from_template() == []


def test_unchanged_from_template_ignores_missing_files(tmp_path, monkeypatch):
    template = tmp_path / "t.example.yaml"
    template.write_text("a: 1", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_TEMPLATES", ((template, tmp_path / "nope.yaml"),))
    assert config.unchanged_from_template() == []


def test_bootstrap_is_idempotent(tmp_path, monkeypatch):
    template = tmp_path / "t.example.yaml"
    target = tmp_path / "t.yaml"
    template.write_text("a: 1", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_TEMPLATES", ((template, target),))

    assert config.bootstrap_configs() == [target]
    assert config.bootstrap_configs() == []
