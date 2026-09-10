"""简历渲染：HTML 模板 → Playwright PDF，带一页约束的 render-measure-retry。

路线图 Phase 3 的关键工程细节：**一页约束不能靠 prompt。**

选材模型输出的是 bullet ID，它根本不知道这些 bullet 渲染出来占几行。
把「控制一页」写进 prompt 是没有任何保证的。正确做法是量出来：

    render → 数页数 → 超页则砍优先级最低的 bullet → 重渲 → 直到 ≤ 上限

砍谁有明确顺序：**先砍没有量化结果的**，再按选材给的排序从后往前砍。
理由是有数字的 bullet 在面试里能展开讲，没数字的替代性最强。

另一件事：**渲染器只认 bullet id**。它从母简历里按 id 取原文，
模型给的任何文本都不会出现在成品里——这是「不编造」原则的执行点，
不是 prompt 里的一句话。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config
from .verify import bullet_index

#: 从 PDF 结构里数页数。比截图判断可靠，也不需要额外依赖。
_PDF_PAGE = re.compile(rb"/Type\s*/Page[^s]")

MAX_SHRINK_ROUNDS = 12


class RenderUnavailable(RuntimeError):
    """没装 Playwright 或浏览器。HTML 仍然可用，只是量不了页数。"""


@dataclass
class RenderResult:
    html: str
    pdf_path: Path | None = None
    html_path: Path | None = None
    page_count: int | None = None
    dropped: list[str] = field(default_factory=list)
    rounds: int = 0
    text: str = ""          # 纯文本形态，交给 verify_rendered 做最终闸门


# ---------------------------------------------------------------------------
# 模板 —— 单栏、无表格、无图形、标准字体 = ATS 友好
# ---------------------------------------------------------------------------

CSS = """
@page { size: Letter; margin: 0.5in 0.6in; }
* { box-sizing: border-box; }
body { font-family: Georgia, 'Times New Roman', serif; font-size: 10.2pt;
       line-height: 1.34; color: #111; margin: 0; }
h1 { font-size: 17pt; margin: 0 0 2pt; letter-spacing: .3px; }
.contact { font-size: 9pt; color: #333; margin-bottom: 9pt; }
h2 { font-size: 10.5pt; text-transform: uppercase; letter-spacing: .8px;
     border-bottom: 1px solid #999; padding-bottom: 2pt;
     margin: 11pt 0 5pt; }
.entry { margin-bottom: 7pt; }
.entry-head { display: flex; justify-content: space-between; gap: 10pt; }
.entry-title { font-weight: bold; }
.entry-meta { font-size: 9pt; color: #444; white-space: nowrap; }
ul { margin: 3pt 0 0; padding-left: 15pt; }
li { margin-bottom: 2.5pt; }
.skills p { margin: 2pt 0; }
"""


def _esc(text: Any) -> str:
    return html.escape(str(text or ""), quote=False)


def build_html(
    master: dict[str, Any],
    selected_ids: list[str],
    *,
    skills_line: list[str] | None = None,
) -> str:
    """按 **id** 从母简历取原文组装 HTML。

    模型给不了这里任何文本——它只能给 id，取不到就是取不到。
    """
    index = bullet_index(master)
    chosen = [i for i in selected_ids if i in index]
    basics = master.get("basics") or {}

    links = basics.get("links") or {}
    contact = " · ".join(
        _esc(x) for x in (
            basics.get("email"), basics.get("phone"), basics.get("location"),
            links.get("github"), links.get("linkedin"),
        ) if x
    )

    parts = [
        f"<style>{CSS}</style>",
        f"<h1>{_esc(basics.get('name'))}</h1>",
        f'<div class="contact">{contact}</div>',
    ]

    if skills_line:
        parts.append('<h2>Skills</h2><div class="skills"><p>'
                     + _esc(" · ".join(skills_line)) + "</p></div>")

    for kind, heading in (("experiences", "Experience"), ("projects", "Projects")):
        entries = [
            e for e in (master.get(kind) or [])
            if any(b.get("id") in chosen for b in (e.get("bullets") or []))
        ]
        if not entries:
            continue
        parts.append(f"<h2>{heading}</h2>")
        for entry in entries:
            title = entry.get("title") or entry.get("name") or ""
            org = entry.get("company") or ""
            head = " — ".join(_esc(x) for x in (org, title) if x)
            meta = " · ".join(_esc(x) for x in (entry.get("location"), entry.get("period")) if x)
            bullets = [b for b in (entry.get("bullets") or []) if b.get("id") in chosen]
            bullets.sort(key=lambda b: chosen.index(b["id"]))
            items = "".join(f"<li>{_esc(b.get('text'))}</li>" for b in bullets)
            parts.append(
                f'<div class="entry"><div class="entry-head">'
                f'<span class="entry-title">{head}</span>'
                f'<span class="entry-meta">{meta}</span></div>'
                f"<ul>{items}</ul></div>"
            )

    edu = master.get("education") or []
    if edu:
        parts.append("<h2>Education</h2>")
        for e in edu:
            head = " — ".join(_esc(x) for x in (e.get("school"), e.get("degree")) if x)
            meta = " · ".join(_esc(x) for x in (e.get("location"), e.get("period")) if x)
            parts.append(
                f'<div class="entry"><div class="entry-head">'
                f'<span class="entry-title">{head}</span>'
                f'<span class="entry-meta">{meta}</span></div></div>'
            )

    return "\n".join(parts)


def html_to_plain(markup: str) -> str:
    """给最终闸门用的纯文本形态。"""
    text = re.sub(r"(?is)<style.*?</style>", " ", markup)
    text = re.sub(r"(?i)</(p|div|li|h[12])\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", text).strip()


# ---------------------------------------------------------------------------
# PDF 与页数
# ---------------------------------------------------------------------------

class PdfRenderer:
    """持有一个浏览器实例，供整个 shrink 循环复用。

    每轮重开浏览器的话，砍十几轮就要跑一分钟——而 shrink 循环本来就要渲染多次。
    开一次、渲多次，把耗时从「启动次数 × 1.5 秒」降到一次启动。
    """

    def __enter__(self) -> "PdfRenderer":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - 取决于安装
            raise RenderUnavailable(
                "没装 Playwright。pip install playwright && python -m playwright install chromium"
            ) from exc
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch()
        except Exception as exc:  # 浏览器没下载
            self._pw.stop()
            raise RenderUnavailable(f"启动 Chromium 失败：{exc}") from exc
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            self._browser.close()
        finally:
            self._pw.stop()

    def render(self, markup: str) -> tuple[bytes, int]:
        page = self._browser.new_page()
        try:
            page.set_content(markup, wait_until="load")
            pdf = page.pdf(format="Letter", print_background=True,
                           margin={"top": "0.5in", "bottom": "0.5in",
                                   "left": "0.6in", "right": "0.6in"})
        finally:
            page.close()
        return pdf, count_pages(pdf)


def render_pdf(markup: str, out_path: Path | None = None) -> tuple[bytes, int]:
    """渲染一次 PDF 并返回 (字节, 页数)。多次渲染请直接用 PdfRenderer。"""
    with PdfRenderer() as r:
        pdf, pages = r.render(markup)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(pdf)
    return pdf, pages


def count_pages(pdf_bytes: bytes) -> int:
    return max(1, len(_PDF_PAGE.findall(pdf_bytes)))


# ---------------------------------------------------------------------------
# render → measure → shrink → retry
# ---------------------------------------------------------------------------

def _drop_order(selected: list[str], index: dict[str, dict[str, Any]]) -> list[str]:
    """砍 bullet 的顺序：先砍没量化结果的，再按选材排序从后往前。

    有数字的 bullet 面试里能展开讲，没数字的替代性最强——所以先砍后者。
    """
    from .profile import _has_metrics

    scored = [
        (0 if not _has_metrics(index[i]) else 1, -pos, i)
        for pos, i in enumerate(selected) if i in index
    ]
    scored.sort()
    return [i for _, _, i in scored]


def render_resume(
    master: dict[str, Any],
    selected_ids: list[str],
    *,
    max_pages: int = 1,
    out_dir: Path | None = None,
    basename: str = "resume",
    skills_line: list[str] | None = None,
) -> RenderResult:
    """渲染，量页数，超页就砍最低优先级的 bullet 再渲，直到 ≤ max_pages。

    砍不动了（只剩一条）就停，并把实际页数如实报出来——
    **宁可告诉你「压不到一页」，也不要悄悄截断内容**。
    """
    out_dir = out_dir or (config.DATA_DIR / "resumes")
    index = bullet_index(master)
    current = [i for i in selected_ids if i in index]
    order = _drop_order(current, index)
    dropped: list[str] = []

    markup = build_html(master, current, skills_line=skills_line)
    result = RenderResult(html=markup)

    try:
        renderer_cm = PdfRenderer()
        renderer = renderer_cm.__enter__()
    except RenderUnavailable:
        # 没有浏览器也要给出 HTML —— 它才是 diff 预览的载体
        result.text = html_to_plain(markup)
        result.html_path = _write_html(out_dir, basename, markup)
        return result

    try:
        pdf, pages = renderer.render(markup)
        rounds = 0
        while pages > max_pages and len(current) > 1 and rounds < MAX_SHRINK_ROUNDS:
            # 按超出比例批量砍，而不是一次砍一条。
            # 3 页压到 1 页时一条一条砍要几十轮；按比例估一把能几轮收敛。
            # 估多了也不怕——下一轮页数够了就停，不会过度删减。
            overshoot = pages / max_pages
            want_drop = max(1, int(len(current) * (1 - 1 / overshoot) * 0.9))
            want_drop = min(want_drop, len(current) - 1)

            victims = [i for i in order if i in current][:want_drop]
            if not victims:
                break
            for v in victims:
                current.remove(v)
                dropped.append(v)
            rounds += 1
            markup = build_html(master, current, skills_line=skills_line)
            pdf, pages = renderer.render(markup)
    finally:
        renderer_cm.__exit__(None, None, None)

    pdf_path = out_dir / f"{basename}.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(pdf)

    return RenderResult(
        html=markup,
        pdf_path=pdf_path,
        html_path=_write_html(out_dir, basename, markup),
        page_count=pages,
        dropped=dropped,
        rounds=rounds,
        text=html_to_plain(markup),
    )


def _write_html(out_dir: Path, basename: str, markup: str) -> Path:
    path = out_dir / f"{basename}.html"
    config.write_text(path, markup)
    return path


def safe_filename(name: str, company: str, role: str) -> str:
    """`{Name}_Resume_{Company}_{Role}` —— 路线图 Phase 3 第 6 点。"""
    def clean(x: str) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "", (x or "").title())[:28] or "X"
    return f"{clean(name)}_Resume_{clean(company)}_{clean(role)}"


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

def diff_summary(
    master: dict[str, Any], selected_ids: list[str], *, dropped: list[str] | None = None
) -> str:
    """相对母简历：选了哪些、砍了哪些。审核门上要看的就是这个。"""
    index = bullet_index(master)
    all_ids = list(index)
    chosen = [i for i in selected_ids if i in index]
    not_chosen = [i for i in all_ids if i not in chosen]

    lines = [f"选中 {len(chosen)}/{len(all_ids)} 条 bullet"]
    for i in chosen:
        lines.append(f"  + {i}  {(index[i].get('text') or '')[:78]}")
    if dropped:
        lines.append(f"因一页约束被砍 {len(dropped)} 条：")
        for i in dropped:
            lines.append(f"  ✂ {i}  {(index[i].get('text') or '')[:78]}")
    if not_chosen:
        lines.append(f"未选中 {len(not_chosen)} 条：{', '.join(not_chosen)}")
    return "\n".join(lines)
