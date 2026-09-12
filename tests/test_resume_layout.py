"""简历版面与内容：时间倒序、条目排版、技能分类、经历每段最多砍一条、补满一页、按 JD 改写关键词。"""

import json
import re

import pytest

from jha import render, tailor
from jha.agent.client import Budget
from test_tailor import FakeSelector, conn  # noqa: F401  （conn 是 fixture）
from test_tailor import MASTER as TAILOR_MASTER

MASTER = {
    "basics": {"name": "Zijiang Zhao", "email": "a@b.c", "location": "Boston, MA", "links": {}},
    "skills": [
        {"id": "sk_py", "name": "Python", "category": "Languages"},
        {"id": "sk_torch", "name": "PyTorch", "category": "Machine Learning"},
        {"id": "sk_llm", "name": "LLMs", "category": "AI & LLMs"},
        {"id": "sk_cpp", "name": "C++", "category": "Languages"},
    ],
    "experiences": [
        {"id": "e_ta", "company": "UConn", "title": "TA", "period": "2024-01 ~ 2024-05",
         "bullets": [{"id": "ta1", "text": "Held office hours.", "metrics": False}]},
        {"id": "e_ra", "company": "BU Lab", "title": "Research Assistant", "period": "Jun 2026 ~ Aug 2026",
         "bullets": [{"id": "ra1", "text": "Designed pipeline contracts.", "metrics": False},
                     {"id": "ra2", "text": "Grew tests from 71 to 246.", "metrics": True}]},
        {"id": "e_oem", "company": "OEM Control", "title": "Engineer", "period": "2023-09 ~ 2024-05",
         "bullets": [{"id": "oem1", "text": "Built a robot arm rig.", "metrics": False}]},
    ],
    "projects": [
        {"id": "p_old", "name": "Old Project", "period": "2026-01 ~ 2026-05",
         "bullets": [{"id": "po1", "text": "Trained a BiLSTM.", "metrics": False}]},
        {"id": "p_new", "name": "Job Agent", "period": "2026-09 ~ present",
         "url": "https://github.com/x/y",
         "bullets": [{"id": "pn1", "text": "Built an agent loop.", "metrics": False}]},
    ],
    "education": [
        {"id": "ed_bs", "school": "UConn", "degree": "B.S.", "period": "2020 ~ 2024"},
        {"id": "ed_ms", "school": "Boston University", "degree": "M.S.", "period": "2025-08 ~ 2026-12"},
    ],
}

LONG = "Lorem ipsum dolor sit amet consectetur adipiscing. " * 5


# ---------------------------------------------------------------------------
# 时间倒序
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("newer, older", [
    ("2026-09 ~ present", "Jun 2026 ~ Aug 2026"),
    ("Jun 2026 ~ Aug 2026", "2026-04"),
    ("2024-01 ~ 2024-05", "2023-09 ~ 2024-05"),     # 同样结束，开始晚的在前
    ("2025-08 ~ 2026-12", "2020 ~ 2024"),
    ("2020 ~ 2024", "not a date"),
])
def test_period_key_orders_most_recent_first(newer, older):
    assert render.period_key(newer) > render.period_key(older)


def test_every_section_is_reverse_chronological():
    text = render.html_to_plain(render.build_html(MASTER, ["ta1", "ra1", "oem1", "po1", "pn1"]))
    assert text.index("M.S.") < text.index("B.S.")
    exp = text.index("Experience")
    assert text.index("BU Lab", exp) < text.index("UConn", exp) < text.index("OEM Control", exp)
    assert text.index("Job Agent") < text.index("Old Project")


# ---------------------------------------------------------------------------
# 条目排版：公司一行（地点靠右），职位一行（时间靠右，时间那号字）
# ---------------------------------------------------------------------------

def test_entry_is_company_and_location_then_title_and_period():
    exp = {**MASTER["experiences"][1], "location": "Boston, MA"}
    markup = render.build_html({**MASTER, "experiences": [exp]}, ["ra1"])
    assert ('<div class="entry-head"><span class="entry-title">BU Lab</span>'
            '<span class="entry-meta">Boston, MA</span></div>'
            '<div class="entry-sub"><span>Research Assistant</span>'
            '<span class="entry-meta">Jun 2026 ~ Aug 2026</span></div>') in markup


def test_title_line_uses_the_period_font_size():
    css = render.CSS
    sub = "".join(re.findall(r"\.entry-sub \{([^}]*)\}", css))     # 和 .entry-head 合写的那条也算
    meta = re.search(r"\.entry-meta \{([^}]*)\}", css).group(1)
    size = re.compile(r"font-size:\s*([\d.]+pt)")
    assert size.search(sub).group(1) == size.search(meta).group(1)


def test_education_follows_the_same_layout():
    markup = render.build_html(MASTER, [])
    assert ('<span class="entry-title">Boston University</span><span class="entry-meta"></span></div>'
            '<div class="entry-sub"><span>M.S.</span><span class="entry-meta">2025-08 ~ 2026-12</span>') in markup


def test_project_without_a_title_puts_its_period_on_the_first_line():
    markup = render.build_html(MASTER, ["pn1"])
    assert '<span class="entry-meta">2026-09 ~ present</span></div><ul>' in markup


def test_project_title_links_to_its_repo_but_only_for_http_urls():
    markup = render.build_html(MASTER, ["pn1"])
    assert 'href="https://github.com/x/y"' in markup
    bad = {**MASTER, "projects": [{**MASTER["projects"][1], "url": "javascript:alert(1)"}]}
    assert "href=" not in render.build_html(bad, ["pn1"])


# ---------------------------------------------------------------------------
# 技能栏
# ---------------------------------------------------------------------------

def test_skills_are_grouped_by_category_in_relevance_order():
    markup = render.build_html(MASTER, ["ra1"], skills_line=["PyTorch", "Python", "LLMs", "C++"])
    rows = re.findall(r"<p><b>(.*?):</b> (.*?)</p>", markup)
    assert rows == [("Machine Learning", "PyTorch"), ("Languages", "Python, C++"),
                    ("AI &amp; LLMs", "LLMs")]


def test_skills_without_categories_render_as_one_line():
    plain = {**MASTER, "skills": [{"id": "a", "name": "Python"}, {"id": "b", "name": "SQL"}]}
    markup = render.build_html(plain, ["ra1"], skills_line=["Python", "SQL"])
    assert "<b>" not in markup and "Python · SQL" in markup


def test_uncategorized_skills_get_no_invented_label():
    """渲染器不能往简历里塞母简历没有的词——哪怕是 "Other"。实测最终闸门就是这么拦下来的。"""
    mixed = {**MASTER, "skills": MASTER["skills"] + [{"id": "sk_git", "name": "Git"}]}
    markup = render.build_html(mixed, ["ra1"], skills_line=["Python", "Git"])
    assert "Other" not in markup and "<p>Git</p>" in markup


def test_tailor_lists_every_skill_when_the_model_gives_none(conn, monkeypatch, tmp_path):
    """技能栏宁多勿少：模型没给就全列。"""
    monkeypatch.setattr(tailor.profile, "load_master_profile", lambda: TAILOR_MASTER)
    monkeypatch.setattr(tailor.config, "DATA_DIR", tmp_path)
    res = tailor.tailor_resume(conn, 1, client=FakeSelector({"selected_bullet_ids": ["b1"], "rationale": "x"}),
                               budget=Budget())
    html_text = open(res.html_path, encoding="utf-8").read()
    for name in ("Python", "PyTorch", "ResNet"):
        assert name in html_text.split("<h2>Skills</h2>")[1]


# ---------------------------------------------------------------------------
# 经历：选中即整段放上、每段最多砍一条、至少两段
# ---------------------------------------------------------------------------

def test_selecting_one_bullet_keeps_the_whole_experience():
    units = render._experience_units(MASTER)
    assert render._expand(["ra2", "pn1"], units) == ["ra1", "ra2", "pn1"]


def test_missing_experiences_are_topped_up_from_the_most_recent():
    out, protected = render._ensure_experiences(MASTER, ["pn1", "ra2"], backup=[], minimum=2)
    assert out == ["pn1", "ra2", "ta1"] and protected == {"ra2", "ta1"}


def test_backup_choice_is_preferred_when_topping_up():
    out, _ = render._ensure_experiences(MASTER, ["pn1", "ta1"], backup=["ra2"], minimum=2)
    assert "ra2" in out and "ra1" not in out


def test_enough_experiences_means_nothing_is_added():
    out, _ = render._ensure_experiences(MASTER, ["oem1", "ra1"], backup=[], minimum=2)
    assert out == ["oem1", "ra1"]


def test_shrinking_cuts_projects_but_at_most_one_bullet_per_experience(tmp_path):
    fat = {**MASTER, "experiences": [
        {"id": "a", "company": "Alpha", "title": "E", "period": "2026-01 ~ 2026-06",
         "bullets": [{"id": f"a{i}", "text": LONG, "metrics": True} for i in range(3)]},
        {"id": "b", "company": "Beta", "title": "E", "period": "2024-01 ~ 2024-06",
         "bullets": [{"id": f"b{i}", "text": LONG, "metrics": False} for i in range(3)]},
    ], "projects": [
        {"id": "p", "name": "Proj", "period": "2025-01 ~ 2025-06",
         "bullets": [{"id": f"p{i}", "text": LONG, "metrics": True} for i in range(20)]},
    ]}
    res = render.render_resume(fat, [f"p{i}" for i in range(20)] + ["b0", "a0"],
                               out_dir=tmp_path, basename="fat2")
    assert res.page_count == 1
    for e in "ab":
        assert len({f"{e}{i}" for i in range(3)} & set(res.selected)) >= 2, "每段经历最多砍一条"
    assert any(d.startswith("p") for d in res.dropped)


def test_experiences_lose_at_most_one_bullet_even_when_the_page_overflows(tmp_path):
    """宁可如实报超页，也不从一段经历里砍第二条。"""
    exps = [
        {"id": n, "company": n.title(), "title": "E", "period": p,
         "bullets": [{"id": f"{n}{i}", "text": LONG * 3, "metrics": False} for i in range(8)]}
        for n, p in (("alpha", "2026-01 ~ 2026-06"), ("beta", "2025-01 ~ 2025-06"))
    ]
    res = render.render_resume({**MASTER, "projects": [], "experiences": exps}, ["alpha0", "beta0"],
                               out_dir=tmp_path, basename="over")
    assert res.page_count > 1
    assert sorted(res.dropped) == ["alpha7", "beta7"]


def test_an_unprotected_experience_loses_one_bullet_or_goes_whole(tmp_path):
    exps = [
        {"id": n, "company": n.title(), "title": "E", "period": p,
         "bullets": [{"id": f"{n}{i}", "text": LONG, "metrics": False} for i in range(k)]}
        for n, p, k in (("alpha", "2026-01 ~ 2026-06", 3), ("beta", "2025-01 ~ 2025-06", 3),
                        ("gamma", "2024-01 ~ 2024-06", 12))
    ]
    res = render.render_resume({**MASTER, "projects": [], "experiences": exps},
                               ["gamma0", "alpha0", "beta0"], out_dir=tmp_path, basename="gamma")
    gamma = {f"gamma{i}" for i in range(12)}
    assert res.page_count == 1
    assert len(gamma & set(res.selected)) in (0, 11, 12), "最多砍一条，再砍就整段拿掉"
    for n in ("alpha", "beta"):         # 受保护的两段
        assert len({f"{n}{i}" for i in range(3)} & set(res.selected)) >= 2


# ---------------------------------------------------------------------------
# 补满一页（项目按条补）
# ---------------------------------------------------------------------------

def _project_master(bullets):
    return {**MASTER, "experiences": [], "projects": [
        {"id": "a", "name": "Alpha", "period": "2026-01 ~ 2026-06", "bullets": bullets}]}


def test_page_is_filled_from_backups_until_nothing_more_fits(tmp_path):
    long = "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do. " * 6
    master = _project_master([{"id": f"a{i}", "text": long, "metrics": True} for i in range(20)])
    res = render.render_resume(master, ["a0"], backup_ids=[f"a{i}" for i in range(1, 20)],
                               out_dir=tmp_path, basename="roomy")
    assert res.page_count == 1
    assert res.added[:2] == ["a1", "a2"], "按备选顺序补"
    assert 3 < len(res.selected) < 20, "补了，但没有超页"


def test_fill_keeps_trying_after_long_bullets_do_not_fit(tmp_path):
    """先试的几条塞不下，后面短的也要试。"""
    huge = "Lorem ipsum dolor sit amet consectetur adipiscing elit. " * 40
    long = "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do. " * 6
    master = _project_master(
        [{"id": f"a{i}", "text": long, "metrics": True} for i in range(8)]
        + [{"id": f"h{i}", "text": huge, "metrics": True} for i in range(4)]
        + [{"id": "tiny", "text": "Wrote tests.", "metrics": True}]
    )
    res = render.render_resume(master, [f"a{i}" for i in range(8)],
                               backup_ids=["h0", "h1", "h2", "h3", "tiny"], out_dir=tmp_path, basename="t")
    assert res.page_count == 1 and "tiny" in res.selected


def test_hallucinated_backup_id_is_an_error_not_silently_dropped(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(tailor.profile, "load_master_profile", lambda: TAILOR_MASTER)
    monkeypatch.setattr(tailor.config, "DATA_DIR", tmp_path)
    client = FakeSelector({"selected_bullet_ids": ["b1"], "backup_bullet_ids": ["b_ghost"],
                           "rationale": "x"})
    res = tailor.tailor_resume(conn, 1, client=client, budget=Budget())
    assert not res.verify_ok and any("b_ghost" in p for p in res.verify_problems)


def test_tailor_reports_what_was_added_for_space(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(tailor.profile, "load_master_profile", lambda: TAILOR_MASTER)
    monkeypatch.setattr(tailor.config, "DATA_DIR", tmp_path)
    client = FakeSelector({"selected_bullet_ids": ["b3"], "backup_bullet_ids": ["b4"], "rationale": "x"})
    res = tailor.tailor_resume(conn, 1, client=client, budget=Budget())
    assert res.added_for_space[0] == "b4" and "b4" in res.selected_ids
    assert "填满页面" in res.diff


# ---------------------------------------------------------------------------
# 按 JD 关键词改写：逐条确定性校验
# ---------------------------------------------------------------------------

def test_rewrite_may_use_a_jd_keyword_the_candidate_has():
    assert tailor.check_rewrite("Built an inference pipeline for batch jobs.",
                                "Built a PyTorch inference pipeline for batch jobs.",
                                TAILOR_MASTER, ["PyTorch"]) is None


def test_rewrite_may_not_add_a_capitalized_skill_the_candidate_lacks():
    reason = tailor.check_rewrite("Built a robot arm calibration rig.",
                                  "Built a React robot arm calibration rig.", TAILOR_MASTER, ["React"])
    assert reason and "React" in reason


def test_rewrite_may_not_add_a_lowercase_jd_keyword_the_candidate_lacks():
    """小写的技术词躲得过专名检查，要单独查。"""
    reason = tailor.check_rewrite("Built an inference pipeline for batch scoring jobs.",
                                  "Built an inference microservices pipeline for batch scoring jobs.",
                                  TAILOR_MASTER, ["microservices"])
    assert reason and "microservices" in reason


def test_rewrite_may_not_change_numbers():
    reason = tailor.check_rewrite("Cut MAE from 19.77 to 11.73 CL.", "Cut MAE from 19.7 to 11.7 CL.",
                                  TAILOR_MASTER, [])
    assert reason


def test_rewrite_may_not_grow_much_longer():
    reason = tailor.check_rewrite("Built a GUI.", "Built a GUI for controlling and monitoring the arm in real time.",
                                  TAILOR_MASTER, [])
    assert reason and "长" in reason


def test_renderer_uses_verified_rewrite_text():
    markup = render.build_html(MASTER, ["ra1"], texts={"ra1": "Designed ML pipeline contracts."})
    assert "Designed ML pipeline contracts." in markup and "Designed pipeline contracts." not in markup


class TwoStep:
    """选材和改写各返回各的。"""

    model = "claude-sonnet-5"

    def __init__(self, selection, rewrites):
        self.selection, self.rewrites, self.calls = selection, rewrites, []

    def structured(self, *, schema_name, **kw):
        self.calls.append(schema_name)
        return dict(self.selection) if schema_name == "resume_selection" else {"rewrites": self.rewrites}


def _with_analysis(conn):
    conn.execute("INSERT INTO job_analysis (job_id, required_skills_json, verdict, scorer_version) "
                 "VALUES (1, '[\"Python\", \"React\"]', 'apply', 'v1')")
    conn.commit()


def test_tailor_applies_verified_rewrites_and_keeps_originals_for_rejected(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(tailor.profile, "load_master_profile", lambda: TAILOR_MASTER)
    monkeypatch.setattr(tailor.config, "DATA_DIR", tmp_path)
    _with_analysis(conn)
    client = TwoStep({"selected_bullet_ids": ["b1", "b2"], "rationale": "x"}, [
        {"id": "b2", "text": "Developed a Python GUI to control the arm."},   # Python 你有
        {"id": "b1", "text": "Built a React robot arm calibration rig."},     # React 你没有
    ])
    res = tailor.tailor_resume(conn, 1, client=client, budget=Budget())

    assert client.calls == ["resume_selection", "resume_rewrite"]
    assert res.rewrites == {"b2": "Developed a Python GUI to control the arm."}
    assert "b1" in res.rewrite_rejected and "React" in res.rewrite_rejected["b1"]
    html_text = open(res.html_path, encoding="utf-8").read()
    assert "Developed a Python GUI to control the arm." in html_text
    assert "Built a robot arm calibration rig." in html_text, "没过校验的用原文"
    assert res.verify_ok
    assert "✎ Developed a Python GUI" in res.diff, "审核门上要能对照原文和改写"
    assert tailor.get_version(conn, res.resume_version_id)["rewrites"] == res.rewrites
    blob = json.dumps(res.compact(), ensure_ascii=False)
    assert "Python GUI" not in blob and "robot arm" not in blob, "给 agent 的结果里不带正文"


@pytest.mark.parametrize("wrap", [lambda items: items, lambda items: {"rewrites": items}])
def test_stringified_rewrites_are_parsed(conn, monkeypatch, tmp_path, wrap):
    """实测：模型会把数组、甚至整个 {"rewrites": [...]} 当成一个 JSON 字符串塞进 rewrites。"""
    monkeypatch.setattr(tailor.profile, "load_master_profile", lambda: TAILOR_MASTER)
    monkeypatch.setattr(tailor.config, "DATA_DIR", tmp_path)
    _with_analysis(conn)
    text = "Developed a Python GUI to control the arm."
    client = TwoStep({"selected_bullet_ids": ["b2"], "rationale": "x"},
                     json.dumps(wrap([{"id": "b2", "text": text}])))
    assert tailor.tailor_resume(conn, 1, client=client, budget=Budget()).rewrites == {"b2": text}


@pytest.mark.parametrize("junk", ["not json", [1, "b2", None], {"b2": "x"}])
def test_malformed_rewrites_fall_back_to_originals(conn, monkeypatch, tmp_path, junk):
    monkeypatch.setattr(tailor.profile, "load_master_profile", lambda: TAILOR_MASTER)
    monkeypatch.setattr(tailor.config, "DATA_DIR", tmp_path)
    _with_analysis(conn)
    client = TwoStep({"selected_bullet_ids": ["b2"], "rationale": "x"}, junk)
    res = tailor.tailor_resume(conn, 1, client=client, budget=Budget())
    assert res.rewrites == {} and res.resume_version_id and res.verify_ok


def test_jd_terms_skip_notes_that_are_not_keywords(conn):
    """实测加分项里混着中文备注，那不是 JD 关键词。"""
    conn.execute("INSERT INTO job_analysis (job_id, required_skills_json, nice_to_have_json, verdict, "
                 "scorer_version) VALUES (1, '[\"React\"]', '[\"开源贡献（Claude Agent Skill）\", \"Node.js\"]', "
                 "'apply', 'v1')")
    conn.commit()
    assert tailor._jd_terms(conn, 1) == ["React", "Node.js"]


def test_no_rewrite_without_jd_analysis(conn, monkeypatch, tmp_path):
    """没有结构化关键词，就没法确定性地检查新加的词是不是你真有的技能——干脆不改。"""
    monkeypatch.setattr(tailor.profile, "load_master_profile", lambda: TAILOR_MASTER)
    monkeypatch.setattr(tailor.config, "DATA_DIR", tmp_path)
    client = TwoStep({"selected_bullet_ids": ["b1"], "rationale": "x"}, [])
    tailor.tailor_resume(conn, 1, client=client, budget=Budget())
    assert client.calls == ["resume_selection"]


def test_rewrite_can_be_turned_off(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(tailor.profile, "load_master_profile", lambda: TAILOR_MASTER)
    monkeypatch.setattr(tailor.config, "DATA_DIR", tmp_path)
    _with_analysis(conn)
    client = TwoStep({"selected_bullet_ids": ["b1"], "rationale": "x"}, [])
    tailor.tailor_resume(conn, 1, client=client, budget=Budget(), rewrite=False)
    assert client.calls == ["resume_selection"]
