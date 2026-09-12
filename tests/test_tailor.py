"""Phase 3：简历定制的测试。

这一组守的是路线图称为「整个项目最重要的安全阀」的东西。所以断言写得比别处硬：
**防幻觉不是靠 prompt，是靠三道结构性保证**——

  1. 选材 schema 里根本没有文本字段，模型想输出正文也无处可放
  2. 渲染器只认 bullet id，从母简历取原文；模型给的字符串进不了成品
  3. 最终闸门查产出物：出现母简历以外的数字或专名就拒

第 2 道最硬——前两道都被绕过，渲染器也拿不出母简历里没有的句子。
"""

import json
from pathlib import Path

import pytest

from jha import db, render, tailor, verify
from jha.agent.client import Budget

MASTER = {
    "basics": {
        "name": "Zijiang Zhao", "email": "a@b.c", "phone": "959-929-5318",
        "location": "Boston, MA", "links": {"github": "https://github.com/x"},
        "work_authorization": "F-1",
    },
    "skills": [
        {"id": "sk_python", "name": "Python"},
        {"id": "sk_pytorch", "name": "PyTorch"},
        {"id": "sk_resnet", "name": "ResNet"},
    ],
    "experiences": [{
        "id": "exp_oem", "company": "OEM Control", "title": "Full Stack Engineer",
        "period": "2023-09 ~ 2024-05", "location": "Storrs, CT",
        "bullets": [
            {"id": "b1", "text": "Built a robot arm calibration rig.", "metrics": False},
            {"id": "b2", "text": "Developed a GUI to control the arm.", "metrics": False},
        ],
    }],
    "projects": [{
        "id": "proj_medai", "name": "Med-AI Hackathon",
        "period": "2026-04",
        "bullets": [
            {"id": "b3", "text": "Trained a 3D ResNet-18 cutting MAE from 19.77 to 11.73 CL.",
             "metrics": "MAE 19.77 -> 11.73"},
            {"id": "b4", "text": "Built a PyTorch inference pipeline.", "metrics": True},
        ],
    }],
    "education": [{"id": "edu", "school": "Boston University",
                   "degree": "M.S. Artificial Intelligence", "period": "~ 2026-12"}],
}


class FakeSelector:
    """假的选材模型。可以让它「作弊」，用来验证结构性保证真的挡得住。"""

    model = "claude-sonnet-5"

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def structured(self, *, system, user, schema, **kw):
        self.calls.append({"system": system, "user": user, "schema": schema})
        return dict(self.payload)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    c.execute("INSERT INTO companies (name) VALUES ('Acme')")
    c.execute("INSERT INTO jobs (company_id, source, external_id, title, jd_text) "
              "VALUES (1,'greenhouse','j1','AI Engineer','We want Python and PyTorch.')")
    c.commit()
    yield c
    c.close()


# ---------------------------------------------------------------------------
# 保证一：schema 里没有文本字段
# ---------------------------------------------------------------------------

def test_selection_schema_has_no_text_field():
    """模型只能给 id。它想输出 bullet 正文，schema 里没有地方放。"""
    props = tailor.SCHEMA["properties"]
    assert set(props) == {"selected_bullet_ids", "backup_bullet_ids", "skills_line", "rationale"}
    assert props["selected_bullet_ids"]["items"] == {"type": "string"}
    assert props["backup_bullet_ids"]["items"] == {"type": "string"}
    # rationale 是给人看的，明确说明不会进简历
    assert "不会进简历" in props["rationale"]["description"]


def test_no_bullet_text_field_anywhere_in_schema():
    blob = json.dumps(tailor.SCHEMA)
    for forbidden in ("bullet_text", "rewritten", "content", "body"):
        assert forbidden not in blob


# ---------------------------------------------------------------------------
# 保证二：渲染器只认 id（最硬的一道）
# ---------------------------------------------------------------------------

def test_renderer_ignores_model_supplied_text():
    """就算模型硬塞正文，渲染器也只按 id 从母简历取原文。"""
    markup = render.build_html(MASTER, ["b1"])
    assert "Built a robot arm calibration rig." in markup
    # 母简历里没有的句子进不去
    assert "Increased revenue by 300%" not in markup


def test_renderer_silently_ignores_unknown_ids():
    markup = render.build_html(MASTER, ["b1", "b_does_not_exist"])
    text = render.html_to_plain(markup)
    assert "robot arm" in text
    assert "b_does_not_exist" not in text


def test_renderer_orders_bullets_as_selected():
    markup = render.build_html(MASTER, ["b2", "b1"])
    text = render.html_to_plain(markup)
    assert text.index("GUI to control") < text.index("robot arm")


def test_renderer_omits_entries_with_no_selected_bullets():
    text = render.html_to_plain(render.build_html(MASTER, ["b1"]))
    assert "OEM Control" in text
    assert "Med-AI" not in text


# ---------------------------------------------------------------------------
# 保证三：最终闸门
# ---------------------------------------------------------------------------

def test_verifier_passes_on_honest_render():
    text = render.html_to_plain(render.build_html(MASTER, ["b1", "b2", "b3", "b4"]))
    assert verify.verify_rendered(text, MASTER).ok


def test_verifier_catches_fabricated_numbers():
    text = render.html_to_plain(render.build_html(MASTER, ["b1"]))
    rep = verify.verify_rendered(text + " Reduced latency by 47% across 9 regions.", MASTER)
    assert not rep.ok
    assert "47%" in rep.novel_numbers


def test_verifier_catches_fabricated_tech_names():
    text = render.html_to_plain(render.build_html(MASTER, ["b1"]))
    rep = verify.verify_rendered(text + " Expert in Kubernetes and Terraform.", MASTER)
    assert not rep.ok
    assert "Kubernetes" in rep.novel_entities


def test_verifier_does_not_cry_wolf_on_dates():
    """日期不能报假阳性。

    校验器一旦开始狼来了，你就不再看它了——那它等于不存在。
    这个 bug 出现过：`\\s*%?` 吃掉换行，把「2024-05\\n」抽成 "05\\n"。
    """
    text = render.html_to_plain(render.build_html(MASTER, ["b1", "b2", "b3", "b4"]))
    rep = verify.verify_rendered(text, MASTER)
    assert rep.ok, f"日期误报：{rep.novel_numbers}"


def test_whitelist_is_global_not_per_bullet():
    # 某个数字在母简历任何地方出现过，就允许出现在简历任何位置
    nums, _ = verify.build_whitelist(MASTER)
    assert "19.77" in nums and "11.73" in nums


# ---------------------------------------------------------------------------
# 选材完整性
# ---------------------------------------------------------------------------

def test_hallucinated_id_is_an_error_not_silently_dropped():
    """幻觉出的 id 必须报错。

    静默丢弃会让简历悄悄少一条，你到面试时才发现对方手里那份
    跟你以为的不一样。
    """
    rep = verify.verify_selection(["b1", "b_fake"], MASTER)
    assert not rep.ok
    assert "b_fake" in rep.problems[0]


def test_duplicate_selection_is_caught():
    rep = verify.verify_selection(["b1", "b1"], MASTER)
    assert not rep.ok and "重复" in rep.problems[0]


def test_empty_selection_is_caught():
    assert not verify.verify_selection([], MASTER).ok


def test_tailor_stops_before_rendering_when_ids_are_bad(conn, monkeypatch):
    monkeypatch.setattr(tailor.profile, "load_master_profile", lambda: MASTER)
    client = FakeSelector({"selected_bullet_ids": ["b1", "b_ghost"], "rationale": "x"})
    res = tailor.tailor_resume(conn, 1, client=client, budget=Budget())
    assert res.verify_ok is False
    assert res.pdf_path is None, "校验没过就不该渲染，更不该留下 PDF"
    assert res.resume_version_id is None


# ---------------------------------------------------------------------------
# 一页约束：量出来的，不是 prompt 说的
# ---------------------------------------------------------------------------

def test_drop_order_prefers_cutting_bullets_without_metrics():
    """先砍没数字的——有数字的面试里能展开讲，没数字的替代性最强。"""
    index = verify.bullet_index(MASTER)
    order = render._drop_order(["b3", "b1", "b4", "b2"], index)
    # b1 / b2 没有 metrics，应该排在被砍队列前面
    assert set(order[:2]) == {"b1", "b2"}


def test_drop_order_is_reverse_of_selection_within_same_metrics_class():
    index = verify.bullet_index(MASTER)
    order = render._drop_order(["b1", "b2"], index)
    assert order == ["b2", "b1"], "同类里从选材排序的末尾开始砍"


def test_page_count_comes_from_the_pdf_not_a_guess():
    pdf, pages = render.render_pdf("<p>" + ("x " * 20) + "</p>")
    assert pages == 1
    pdf2, pages2 = render.render_pdf("<p>" + ("Lorem ipsum dolor sit amet. " * 900) + "</p>")
    assert pages2 > 1


def test_render_shrinks_until_it_fits(tmp_path):
    # 用项目：经历每段最多砍一条，40 条压不下来
    fat = {
        **MASTER,
        "experiences": [],
        "projects": [{
            "id": "e", "name": "Acme", "period": "2020",
            "bullets": [
                {"id": f"x{i}", "text": "Lorem ipsum dolor sit amet consectetur. " * 12,
                 "metrics": False}
                for i in range(40)
            ],
        }],
    }
    res = render.render_resume(fat, [f"x{i}" for i in range(40)],
                               max_pages=1, out_dir=tmp_path, basename="fat")
    assert res.page_count == 1
    assert res.dropped, "超页却一条都没砍"
    assert res.rounds > 0


def test_render_reports_honestly_when_it_cannot_fit(tmp_path):
    """砍无可砍时如实报页数，**不要悄悄截断内容**。"""
    huge = {
        **MASTER,
        "experiences": [{
            "id": "e", "company": "Acme", "title": "Engineer", "period": "2020",
            "bullets": [{"id": "only", "text": "Lorem ipsum dolor sit amet. " * 900,
                         "metrics": False}],
        }],
        "projects": [],
    }
    res = render.render_resume(huge, ["only"], max_pages=1, out_dir=tmp_path, basename="huge")
    assert res.page_count > 1
    assert res.dropped == []       # 只剩一条就不再砍
    text = render.html_to_plain(res.html)
    assert text.count("Lorem ipsum") > 100, "内容被悄悄截断了"


def test_filename_follows_the_convention():
    name = render.safe_filename("Zijiang Zhao", "Databricks", "AI Engineer - FDE")
    assert name == "ZijiangZhao_Resume_Databricks_AiEngineerFde"


# ---------------------------------------------------------------------------
# 审核门
# ---------------------------------------------------------------------------

def test_generated_version_starts_unapproved(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(tailor.profile, "load_master_profile", lambda: MASTER)
    monkeypatch.setattr(tailor.config, "DATA_DIR", tmp_path)
    client = FakeSelector({"selected_bullet_ids": ["b1", "b3"], "rationale": "相关"})
    res = tailor.tailor_resume(conn, 1, client=client, budget=Budget())
    assert res.resume_version_id
    row = tailor.get_version(conn, res.resume_version_id)
    assert row["approved_at"] is None
    step = res.compact()["next_step"]
    assert "未审核" in step
    # 不能指向一个 agent 调不出来的工具——那只会让它白试一轮
    assert "approve_resume" not in step
    assert "agent resume approve" in step


def test_approve_sets_the_timestamp(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(tailor.profile, "load_master_profile", lambda: MASTER)
    monkeypatch.setattr(tailor.config, "DATA_DIR", tmp_path)
    client = FakeSelector({"selected_bullet_ids": ["b1"], "rationale": "x"})
    res = tailor.tailor_resume(conn, 1, client=client, budget=Budget())
    tailor.approve(conn, res.resume_version_id)
    assert tailor.get_version(conn, res.resume_version_id)["approved_at"]


def _no_browser(monkeypatch):
    def unavailable(self):
        raise render.RenderUnavailable("启动 Chromium 失败：Executable doesn't exist\n╔══ playwright install ══╗")
    monkeypatch.setattr(render.PdfRenderer, "__enter__", unavailable)


def _tailor(conn, monkeypatch, tmp_path, ids):
    monkeypatch.setattr(tailor.profile, "load_master_profile", lambda: MASTER)
    monkeypatch.setattr(tailor.config, "DATA_DIR", tmp_path)
    client = FakeSelector({"selected_bullet_ids": ids, "rationale": "x"})
    return tailor.tailor_resume(conn, 1, client=client, budget=Budget())


def test_missing_browser_is_reported_not_swallowed(conn, monkeypatch, tmp_path):
    """实测：原因被吞掉，没 PDF、没量页数的版本一路走到了「已批准」。"""
    _no_browser(monkeypatch)
    res = _tailor(conn, monkeypatch, tmp_path, ["b1"])
    assert res.pdf_path is None and res.html_path
    assert res.render_error == "启动 Chromium 失败：Executable doesn't exist"
    assert res.compact()["render_error"] == res.render_error


def test_approve_refuses_a_version_without_a_pdf(conn, monkeypatch, tmp_path):
    _no_browser(monkeypatch)
    res = _tailor(conn, monkeypatch, tmp_path, ["b1"])
    with pytest.raises(ValueError, match="没有 PDF"):
        tailor.approve(conn, res.resume_version_id)
    assert tailor.get_version(conn, res.resume_version_id)["approved_at"] is None


def test_approve_refuses_when_the_pdf_file_is_gone(conn, monkeypatch, tmp_path):
    res = _tailor(conn, monkeypatch, tmp_path, ["b1"])
    Path(res.pdf_path).unlink()
    with pytest.raises(ValueError, match="不存在"):
        tailor.approve(conn, res.resume_version_id)


def test_versions_of_the_same_job_do_not_overwrite_each_other(conn, monkeypatch, tmp_path):
    one = _tailor(conn, monkeypatch, tmp_path, ["b1"])
    two = _tailor(conn, monkeypatch, tmp_path, ["b3"])
    assert one.pdf_path != two.pdf_path
    assert Path(one.pdf_path).is_file() and Path(two.pdf_path).is_file()
    assert Path(one.pdf_path).parent.name == f"v{one.resume_version_id}"
    assert Path(one.pdf_path).name == Path(two.pdf_path).name, "文件名招聘方看得到，不带版本号"
    assert tailor.get_version(conn, one.resume_version_id)["rendered_pdf_path"] == one.pdf_path


def test_compact_has_no_bullet_text(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(tailor.profile, "load_master_profile", lambda: MASTER)
    monkeypatch.setattr(tailor.config, "DATA_DIR", tmp_path)
    client = FakeSelector({"selected_bullet_ids": ["b1", "b3"], "rationale": "x"})
    blob = json.dumps(
        tailor.tailor_resume(conn, 1, client=client, budget=Budget()).compact(),
        ensure_ascii=False,
    )
    assert "robot arm" not in blob and "ResNet" not in blob


# ---------------------------------------------------------------------------
# 上下文：给结构化分析，不给 JD 全文
# ---------------------------------------------------------------------------

def test_uses_structured_analysis_instead_of_raw_jd(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(tailor.profile, "load_master_profile", lambda: MASTER)
    monkeypatch.setattr(tailor.config, "DATA_DIR", tmp_path)
    conn.execute(
        "INSERT INTO job_analysis (job_id, required_skills_json, jd_summary_plain, "
        "verdict, scorer_version) VALUES (1, '[\"PyTorch\"]', '做模型推理', 'apply', 'v1')"
    )
    conn.commit()
    client = FakeSelector({"selected_bullet_ids": ["b1"], "rationale": "x"})
    tailor.tailor_resume(conn, 1, client=client, budget=Budget())
    user = client.calls[0]["user"]
    assert "做模型推理" in user
    assert "<untrusted-job-description>" not in user, "有分析结果就不该再塞 JD 原文"


def test_falls_back_to_fenced_jd_when_no_analysis(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(tailor.profile, "load_master_profile", lambda: MASTER)
    monkeypatch.setattr(tailor.config, "DATA_DIR", tmp_path)
    client = FakeSelector({"selected_bullet_ids": ["b1"], "rationale": "x"})
    tailor.tailor_resume(conn, 1, client=client, budget=Budget())
    user = client.calls[0]["user"]
    assert "<untrusted-job-description>" in user


def test_catalog_marks_which_bullets_have_metrics(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(tailor.profile, "load_master_profile", lambda: MASTER)
    monkeypatch.setattr(tailor.config, "DATA_DIR", tmp_path)
    client = FakeSelector({"selected_bullet_ids": ["b1"], "rationale": "x"})
    tailor.tailor_resume(conn, 1, client=client, budget=Budget())
    user = client.calls[0]["user"]
    assert "[有数字]" in user and "[无数字]" in user


# ---------------------------------------------------------------------------
# 改写路径（默认关）的校验器
# ---------------------------------------------------------------------------

def test_rewrite_rejects_new_numbers():
    rep = verify.verify_rewrite(
        "Built a GUI to control the arm.",
        "Built a GUI that cut test time by 40%.",
        MASTER,
    )
    assert not rep.ok and "40%" in rep.novel_numbers


def test_rewrite_rejects_altered_numbers():
    # 19.77 改成 19.7 不是「新增」，但同样是编造
    rep = verify.verify_rewrite(
        "Cut MAE from 19.77 to 11.73 CL.", "Cut MAE from 19.7 to 11.7 CL.", MASTER
    )
    assert not rep.ok
    assert any("丢失" in p for p in rep.problems)


def test_rewrite_allows_pure_wording_change():
    rep = verify.verify_rewrite(
        "Built a GUI to control the arm.",
        "Developed a GUI for controlling the arm.",
        MASTER,
    )
    assert rep.ok, rep.problems


# ---------------------------------------------------------------------------
# 章节顺序与页眉
# ---------------------------------------------------------------------------

def test_section_order_is_education_experience_projects_skills():
    import re

    markup = render.build_html(MASTER, ["b1", "b3"], skills_line=["Python"])
    assert re.findall(r"<h2>(\w+)", markup) == [
        "Education", "Experience", "Projects", "Skills"
    ]


def test_section_order_is_a_single_constant():
    """顺序抽成常量，因为它对应届生和有工作经验的人应该不一样。

    在读/应届把 Education 放最前；工作几年之后该让 Experience 打头。
    改一行就能整体调整。
    """
    assert render.SECTION_ORDER == ("education", "experience", "projects", "skills")


def test_name_and_contact_are_centered():
    markup = render.build_html(MASTER, ["b1"])
    css = markup[: markup.index("</style>")]
    assert "h1 {" in css and "text-align: center" in css
    assert ".contact {" in css
    # 两处各自居中
    assert css.count("text-align: center") >= 2


def test_skills_section_omitted_when_empty():
    import re

    markup = render.build_html(MASTER, ["b1"])
    assert "Skills" not in re.findall(r"<h2>(\w+)", markup)
