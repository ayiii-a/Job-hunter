"""Phase 3：简历定制。**第二层，不在 agent loop 里。**

路线图称之为「关键设计，直接决定会不会幻觉」的两步法：

    第一步 选材   LLM 输入 = JD 分析 + 母简历（带 id）
                 LLM 输出 = **只有 bullet id**，没有文本
    第二步 改写   默认关。开了也要过确定性校验器（见 verify.py）

## 保证在哪

不在 prompt 里。在三个地方：

  1. **输出 schema 里根本没有文本字段。** 模型想输出 bullet 正文也无处可放。
  2. **渲染器只认 id。** `render.build_html` 从母简历按 id 取原文，
     模型给的任何字符串都不会进入成品。
  3. **最终闸门查产出物。** 渲染完的纯文本再过一次 `verify_rendered`，
     出现母简历里没有的数字或专名就拒绝。

三道里第 2 道最硬：就算前两道都被绕过，渲染器也拿不出母简历里没有的句子。

## 为什么不给它 JD 全文

`job_analysis` 已经是结构化的了（required_skills / gaps / seniority）。
选材需要的是「JD 要什么」，不是 JD 原文——传结构化结果既省钱又更聚焦。
JD 全文只在没有分析结果时才作为兜底传入，且截断。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import analyze, config, db, profile, render, verify
from .agent.client import AgentClient, Budget, BudgetExceeded, MissingAPIKey

#: 改 prompt 或 schema 就 bump
TAILOR_VERSION = "v1"

#: 选材量小、质量要求高——这是防幻觉的关键环节，用好模型（路线图 §1 选型表）
TAILOR_MODEL = "claude-sonnet-5"

#: 输出 schema：**只有 id，没有任何文本字段**。这是第一道保证。
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "selected_bullet_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "选中的 bullet id，按你希望它们在简历上出现的顺序排列。"
                           "只能填给定清单里存在的 id。",
        },
        "skills_line": {
            "type": "array",
            "items": {"type": "string"},
            "description": "技能栏要展示哪些技能，填 skills 清单里的 name 原文",
        },
        "rationale": {
            "type": "string",
            "description": "为什么这样选，两三句。这段只给人看，不会进简历",
        },
    },
    "required": ["selected_bullet_ids", "rationale"],
}

SYSTEM = """你在为一个具体岗位挑选简历内容。

你的输出**只有 bullet 的 id**，没有正文。简历正文由渲染器从母简历里按 id 逐字取出——
你写的任何句子都不会出现在成品简历上，所以不要尝试改写或润色。

怎么选：
- 优先选和这个岗位的核心要求直接相关的
- **同等相关时优先选有量化结果的**（清单里标了 has_metrics）——面试时能展开讲
- 每段经历留 2–4 条，最相关的排前面
- 总量控制在 12–16 条左右；渲染器会量实际页数，超了会从你排序的末尾往前砍，
  所以**把最想保留的排在前面**
- 不相关的经历整段不选也可以

skills_line 从技能清单里挑和岗位相关的，按相关度排序，10–18 个。"""


@dataclass
class TailorResult:
    job_id: int
    selected_ids: list[str] = field(default_factory=list)
    dropped_for_length: list[str] = field(default_factory=list)
    page_count: int | None = None
    pdf_path: str | None = None
    html_path: str | None = None
    diff: str = ""
    rationale: str = ""
    resume_version_id: int | None = None
    verify_ok: bool = True
    verify_problems: list[str] = field(default_factory=list)
    error: str | None = None

    def compact(self) -> dict[str, Any]:
        """返回给 agent 的形式——不含 bullet 正文，只有 id 和计数。"""
        out: dict[str, Any] = {
            "job_id": self.job_id,
            "resume_version_id": self.resume_version_id,
            "selected_count": len(self.selected_ids),
            "selected_bullet_ids": self.selected_ids,
            "page_count": self.page_count,
            "verify_ok": self.verify_ok,
        }
        if self.dropped_for_length:
            out["dropped_for_length"] = self.dropped_for_length
        if self.verify_problems:
            out["verify_problems"] = self.verify_problems
        if self.pdf_path:
            out["pdf_path"] = self.pdf_path
        if self.error:
            out["error"] = self.error
        # 别写成「调 approve_resume」—— 那个工具对 agent 不存在（审核门就是这么设计的）。
        # 指向一个调不出来的能力，只会让模型白试一轮。
        out["next_step"] = (
            "未审核。你没有批准的能力——请把 diff 和 PDF 路径给用户，"
            "让他自己跑 `agent resume approve %s`"
            % (self.resume_version_id if self.resume_version_id else "<id>")
        )
        return out


def _bullet_catalog(master: dict[str, Any]) -> list[dict[str, Any]]:
    """给模型看的 bullet 清单。带 id、正文、技能标签、有没有数字。"""
    out = []
    for kind in ("experiences", "projects"):
        for entry in master.get(kind) or []:
            for b in entry.get("bullets") or []:
                if not b.get("id"):
                    continue
                out.append({
                    "id": b["id"],
                    "belongs_to": f"{entry.get('company') or entry.get('name')} "
                                  f"({entry.get('period') or ''})",
                    "text": b.get("text"),
                    "skills": b.get("skills") or [],
                    "has_metrics": profile._has_metrics(b),
                })
    return out


def _job_context(conn: sqlite3.Connection, job_id: int) -> tuple[sqlite3.Row, str]:
    row = conn.execute(
        "SELECT j.*, c.name AS company FROM jobs j "
        "LEFT JOIN companies c ON c.id = j.company_id WHERE j.id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"没有 id 为 {job_id} 的岗位")

    a = analyze.get_analysis(conn, job_id)
    if a:
        # 有结构化分析就用它——比 JD 原文更聚焦，也更省
        ctx = (
            f"岗位：{row['title']}｜{row['company'] or ''}\n"
            f"这个岗位在做什么：{a.get('jd_summary_plain') or ''}\n"
            f"核心要求：{', '.join(a.get('required_skills') or [])}\n"
            f"加分项：{', '.join(a.get('nice_to_have') or [])}\n"
            f"职级：{a.get('seniority') or ''}　年限：{a.get('years_required') or ''}\n"
            f"已知 gap：{', '.join(a.get('gaps') or [])}"
        )
    else:
        ctx = (
            f"岗位：{row['title']}｜{row['company'] or ''}\n"
            "（这个岗位还没跑过分析，下面是 JD 节选）\n"
            f"<untrusted-job-description>\n{(row['jd_text'] or '')[:6000]}\n"
            "</untrusted-job-description>"
        )
    return row, ctx


# ---------------------------------------------------------------------------

def tailor_resume(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    client: AgentClient | None = None,
    budget: Budget | None = None,
    max_pages: int = 1,
) -> TailorResult:
    master = profile.load_master_profile()
    catalog = _bullet_catalog(master)
    if not catalog:
        return TailorResult(job_id, error="母简历里一条 bullet 都没有")

    row, ctx = _job_context(conn, job_id)
    client = client or AgentClient()
    budget = budget or Budget(max_llm_calls=4)

    skills = [s.get("name") for s in (master.get("skills") or []) if s.get("name")]
    user = (
        f"{ctx}\n\n可选的 bullet（**只能从这里挑 id**）：\n"
        + "\n".join(
            f"  {b['id']}  [{'有数字' if b['has_metrics'] else '无数字'}] "
            f"{b['belongs_to']} — {b['text']}"
            for b in catalog
        )
        + f"\n\n技能清单：{', '.join(skills)}"
    )

    try:
        data = client.structured(
            system=SYSTEM, user=user, schema=SCHEMA, schema_name="resume_selection",
            budget=budget, conn=conn, purpose="resume_select",
            model=TAILOR_MODEL, ref_type="job", ref_id=job_id,
        )
    except (MissingAPIKey, BudgetExceeded):
        raise
    except Exception as exc:
        return TailorResult(job_id, error=f"{type(exc).__name__}: {exc}")

    selected = [str(i) for i in (data.get("selected_bullet_ids") or [])]

    # 一、选材完整性。幻觉出的 id 必须报错，不能静默丢弃
    sel_report = verify.verify_selection(selected, master)
    if not sel_report.ok:
        return TailorResult(
            job_id, selected_ids=selected, verify_ok=False,
            verify_problems=sel_report.problems, rationale=data.get("rationale") or "",
        )

    # 二、渲染 + 一页约束（量出来的，不是 prompt 说的）
    basics = master.get("basics") or {}
    basename = render.safe_filename(
        basics.get("name") or "resume", row["company"] or "", row["title"] or ""
    )
    rendered = render.render_resume(
        master, selected, max_pages=max_pages, basename=basename,
        skills_line=[s for s in (data.get("skills_line") or []) if s in skills] or None,
    )

    # 三、最终闸门：产出物里不得有母简历没有的数字或专名
    final = verify.verify_rendered(rendered.text, master) if rendered.text else verify.VerifyReport(True)

    result = TailorResult(
        job_id=job_id,
        selected_ids=[i for i in selected if i not in rendered.dropped],
        dropped_for_length=rendered.dropped,
        page_count=rendered.page_count,
        pdf_path=str(rendered.pdf_path) if rendered.pdf_path else None,
        html_path=str(rendered.html_path) if rendered.html_path else None,
        diff=render.diff_summary(master, selected, dropped=rendered.dropped),
        rationale=data.get("rationale") or "",
        verify_ok=final.ok,
        verify_problems=final.problems,
    )
    result.resume_version_id = _store(conn, result)
    return result


def _store(conn: sqlite3.Connection, r: TailorResult) -> int:
    """写 resume_versions。**approved_at 留空**——审核门在 approve_resume。"""
    cur = conn.execute(
        "INSERT INTO resume_versions (generated_for_job_id, selected_bullet_ids_json, "
        "rendered_pdf_path, rendered_html_path, page_count, diff_summary) "
        "VALUES (?,?,?,?,?,?)",
        (r.job_id, db.dump_json(r.selected_ids), r.pdf_path, r.html_path,
         r.page_count, r.diff),
    )
    conn.commit()
    return int(cur.lastrowid)


def approve(conn: sqlite3.Connection, resume_version_id: int) -> dict[str, Any]:
    """审核门。**只有人能过这道门**——所以它不是 agent 的工具。"""
    row = conn.execute(
        "SELECT * FROM resume_versions WHERE id = ?", (resume_version_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"没有 id 为 {resume_version_id} 的简历版本")
    conn.execute(
        "UPDATE resume_versions SET approved_at = datetime('now') WHERE id = ?",
        (resume_version_id,),
    )
    conn.commit()
    return {"resume_version_id": resume_version_id, "approved": True}


def get_version(conn: sqlite3.Connection, resume_version_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM resume_versions WHERE id = ?", (resume_version_id,)
    ).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["selected_bullet_ids"] = db.load_json(out.pop("selected_bullet_ids_json", None))
    return out
