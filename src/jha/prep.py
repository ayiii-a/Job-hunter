"""面试准备材料（prep pack）。**确定性，只汇总库里已有的事实。**

路线图 Phase 5 第 7 点列了七项。其中五项是库里现成的事实，拼起来就行：
JD 讲解、技能对比、gap、投出去的那版简历、内推人。另外两项不是：

    可能被问到的问题  需要 LLM 基于 JD 生成
    公司近况          需要 web search —— 这是整个项目里唯一真正需要多轮 subagent 的地方

这两项**明确标 TODO，不假装做了**。宁可材料少一块，也不要塞进一段编的。

有一处比清单多做的：**把 story_bank 和投出去的 bullet 对上**。
你简历上写了哪几条，面试官就会追问哪几条——对应的 STAR 故事应该摆在你眼前。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from . import analyze, config, profile, tailor, verify


def build(conn: sqlite3.Connection, application_id: int) -> str:
    app = conn.execute(
        "SELECT a.*, j.id AS job_id, j.title, j.url, j.location, j.salary_raw, "
        "j.company_id, c.name AS company FROM applications a JOIN jobs j ON j.id = a.job_id "
        "LEFT JOIN companies c ON c.id = j.company_id WHERE a.id = ?", (application_id,),
    ).fetchone()
    if app is None:
        raise ValueError(f"没有 id 为 {application_id} 的投递记录")

    master = profile.load_master_profile()
    index = verify.bullet_index(master)
    analysis = analyze.get_analysis(conn, app["job_id"])
    selected: list[str] = []
    if app["resume_version_id"]:
        rv = tailor.get_version(conn, app["resume_version_id"])
        selected = (rv or {}).get("selected_bullet_ids") or []
    contacts = conn.execute(
        "SELECT name, relationship, strength, last_contacted_at FROM contacts "
        "WHERE company_id = ? ORDER BY strength DESC", (app["company_id"],),
    ).fetchall()
    events = conn.execute(
        "SELECT type, occurred_at, source FROM events WHERE application_id = ? "
        "ORDER BY occurred_at, id", (application_id,),
    ).fetchall()

    company, title = app["company"] or "", app["title"] or ""
    out = [
        f"# 面试准备：{company} — {title}", "",
        "> 这份材料**只汇总库里已有的事实**，不编造。标了 TODO 的需要你自己补。", "",
    ]

    # ---- 内推人放最前 ----
    if contacts:
        out += ["## ★ 先找内部的人聊 15 分钟", "",
                "面试前找内部的人聊一次，价值高于多刷两道题。", ""]
        out += [f"- **{c['name']}**（{c['relationship'] or '关系未记'}，强度 {c['strength']}）"
                for c in contacts]
    else:
        out += ["## 内部联系人", "",
                "这家公司 contacts 里没有人。LinkedIn 上找一个校友或同组的人，面试前聊 15 分钟。"]
    out.append("")

    # ---- 岗位 ----
    out += ["## 这个岗位在做什么", ""]
    if analysis and analysis.get("jd_summary_plain"):
        out.append(analysis["jd_summary_plain"])
    else:
        out.append("TODO：这个岗位还没跑过分析（`agent analyze`）")
    meta = " · ".join(x for x in (app["location"], app["salary_raw"]) if x)
    if meta:
        out += ["", meta]
    out.append("")

    # ---- 技能对比 ----
    if analysis and analysis.get("required_skills"):
        mine = {(s.get("name") or "").lower() for s in master.get("skills") or [] if s.get("name")}

        def have(req: str) -> bool:
            r = req.lower()
            return r in mine or any(len(m) >= 3 and (m in r or r in m) for m in mine)

        out += ["## 技能对比", "",
                "> 按技能名字面匹配，同义词（K8s / Kubernetes）可能认不出来。", ""]
        out += [f"- {'✓' if have(s) else '✗'} {s}" for s in analysis["required_skills"]]
        out.append("")

    # ---- gap ----
    if analysis and analysis.get("gaps"):
        out += ["## 已知 gap —— 被问到时怎么答", "",
                "别回避。承认 → 说明你怎么补 → 举一个相邻的经验。", ""]
        out += [f"- {g}" for g in analysis["gaps"]]
        out.append("")

    # ---- 投出去的简历 ----
    out += ["## 你投出去的那版简历", ""]
    if selected:
        out.append(f"简历版本 #{app['resume_version_id']}。**面试官手里就是这几条，会逐条追问**：")
        out.append("")
        out += [f"- `{i}` {index[i]['text']}" for i in selected if i in index]
    else:
        out.append("⚠ 这条投递没绑定简历版本——**你不知道对方手里那份写了什么**。"
                   "去翻当时的投递记录确认，别凭记忆去面试。")
    out.append("")

    # ---- STAR 故事 ----
    stories = master.get("story_bank") or []
    chosen = set(selected)
    relevant = [st for st in stories
                if not chosen or chosen & set(st.get("linked_bullets") or [])]
    out += ["## 对得上的 STAR 故事", ""]
    if relevant:
        for st in relevant:
            empty = [p for p in ("situation", "task", "action", "result") if not st.get(p)]
            linked = ", ".join(st.get("linked_bullets") or [])
            out.append(f"### {st.get('title') or st.get('id')}")
            out.append(f"对应 bullet：{linked}")
            if empty:
                out.append(f"⚠ 还没写：{', '.join(empty)} —— **面试前补上**，被追问时现编最容易翻车")
            else:
                for p in ("situation", "task", "action", "result"):
                    out.append(f"- **{p}**：{st[p]}")
            out.append("")
    else:
        out += ["story_bank 里没有和这版简历对得上的故事。", ""]

    # ---- 时间线 ----
    if events:
        out += ["## 时间线", ""]
        out += [f"- {e['occurred_at'][:16]}  {e['type']}（{e['source']}）" for e in events]
        out.append("")

    # ---- 明确没做的 ----
    out += [
        "## TODO —— 这份材料没有覆盖的", "",
        "- **可能被问到的问题**：需要 LLM 基于 JD 生成，还没做（Phase 6）",
        "- **公司近况**：需要 web search，还没做",
    ]
    if app["url"]:
        out.append(f"- 岗位原链接：{app['url']}")
    return "\n".join(out) + "\n"


def write(conn: sqlite3.Connection, application_id: int) -> Path:
    text = build(conn, application_id)
    first = text.splitlines()[0] if text else ""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", first.split("：", 1)[-1])[:40].strip("_") or "prep"
    path = config.DATA_DIR / "prep" / f"{application_id}_{slug}.md"
    config.write_text(path, text)
    return path
