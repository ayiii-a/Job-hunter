"""简历渲染：HTML 模板 → Playwright PDF，带一页约束的 render-measure-retry。

路线图 Phase 3 的关键工程细节：**一页约束不能靠 prompt。**

选材模型输出的是 bullet ID，它根本不知道这些 bullet 渲染出来占几行。
把「控制一页」写进 prompt 是没有任何保证的。正确做法是量出来：

    render → 数页数 → 超页则砍优先级最低的内容 → 重渲 → 直到 ≤ 上限
    还有余量 → 按备选顺序一条条往回加 → 加一条渲一次，超页就撤回 → 直到塞不下

砍谁有明确顺序：**先砍没有量化结果的**，再按选材给的排序从后往前砍。
理由是有数字的 bullet 在面试里能展开讲，没数字的替代性最强。

两条底线：
  - **experience 每段最多砍一条**：选中一段经历就把它的 bullet 全放上去，超页时每段最多砍掉一条，
    再砍就只能整段拿掉
  - **至少两段 experience**，这几段不会被整段拿掉

另一件事：**渲染器只认 bullet id**。正文要么是母简历原文，要么是这条 bullet
逐条过了确定性校验的改写（见 tailor.check_rewrite）。没过校验的文本进不了成品。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config
from .profile import _has_metrics
from .verify import bullet_index

#: 从 PDF 结构里数页数。比截图判断可靠，也不需要额外依赖。
_PDF_PAGE = re.compile(rb"/Type\s*/Page[^s]")

MAX_SHRINK_ROUNDS = 12

#: 补满页面时最多试几次（每试一次都要渲一遍 PDF）
MAX_FILL_TRIES = 30

#: 简历上至少出现几段 experience
MIN_EXPERIENCES = 2

#: 章节顺序。抽出来做常量，是因为它对应届生和有工作经验的人应该是**不一样的**：
#: 在读/应届把 Education 放最前（学位是当前最主要的资历），工作几年之后
#: 就该让 Experience 打头。改这一行就能整体调整，不用动渲染逻辑。
SECTION_ORDER: tuple[str, ...] = ("education", "experience", "projects", "skills")


class RenderUnavailable(RuntimeError):
    """没装 Playwright 或浏览器。HTML 仍然可用，只是量不了页数。"""


@dataclass
class RenderResult:
    html: str
    pdf_path: Path | None = None
    html_path: Path | None = None
    page_count: int | None = None
    selected: list[str] = field(default_factory=list)   # 最终上了简历的 bullet id
    dropped: list[str] = field(default_factory=list)    # 因为超页被砍的
    added: list[str] = field(default_factory=list)      # 为了填满页面补进来的
    rounds: int = 0
    text: str = ""          # 纯文本形态，交给 verify_rendered 做最终闸门
    error: str | None = None    # 没出 PDF 的原因（多半是没装浏览器）


# ---------------------------------------------------------------------------
# 时间顺序 —— 每个 section 内越近越靠前
# ---------------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_ONGOING = re.compile(r"(?i)present|current|至今|现在")
_RANGE_SEP = re.compile(r"\s*~\s*|\s+[-–—]\s+|\s+to\s+")


def _point(text: str) -> tuple[int, int]:
    t = text.strip()
    if _ONGOING.search(t):
        return 9999, 12
    m = re.search(r"(\d{4})[-/.](\d{1,2})", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"([A-Za-z]{3})[A-Za-z]*\.?\s+(\d{4})", t)
    if m and m.group(1).lower() in _MONTHS:
        return int(m.group(2)), _MONTHS[m.group(1).lower()]
    m = re.search(r"(\d{4})", t)
    if m:
        return int(m.group(1)), 12          # 只写了年份：按年底算
    return 0, 0


def period_key(period: Any) -> tuple[tuple[int, int], tuple[int, int]]:
    """(结束, 开始)。倒序排就是「越近越前」：先比谁结束得晚，结束一样再比谁开始得晚。

    进行中（present）排最前，解析不了的排最后。认得 "2023-09 ~ 2024-05"、
    "Jun 2026 ~ Aug 2026"、"2026-04"、"2020 ~ 2024" 这几种写法。
    """
    parts = [p for p in _RANGE_SEP.split(str(period or "")) if p.strip()]
    if not parts:
        return (0, 0), (0, 0)
    return _point(parts[-1]), _point(parts[0])


def _by_recency(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(entries, key=lambda e: period_key(e.get("period")), reverse=True)


# ---------------------------------------------------------------------------
# 模板 —— 单栏、无表格、无图形、标准字体 = ATS 友好
# ---------------------------------------------------------------------------

#: 页边距。上下压到 0.3in 给项目腾地方；CSS 的 @page 和 PDF 渲染共用这一份，免得两边对不上
MARGIN_Y = "0.3in"
MARGIN_X = "0.6in"

CSS = f"@page {{ size: Letter; margin: {MARGIN_Y} {MARGIN_X}; }}" + """
* { box-sizing: border-box; }
body { font-family: Georgia, 'Times New Roman', serif; font-size: 10.2pt;
       line-height: 1.34; color: #111; margin: 0; }
h1 { font-size: 17pt; margin: 0 0 3pt; letter-spacing: .3px; text-align: center; }
.contact { font-size: 9pt; color: #333; margin-bottom: 10pt; text-align: center; }
h2 { font-size: 10.5pt; text-transform: uppercase; letter-spacing: .8px;
     border-bottom: 1px solid #999; padding-bottom: 2pt;
     margin: 11pt 0 5pt; }
.entry { margin-bottom: 7pt; }
.entry-head, .entry-sub { display: flex; justify-content: space-between; gap: 10pt; }
.entry-title { font-weight: bold; }
.entry-title a { color: inherit; text-decoration: none; }
.entry-sub { font-size: 9pt; }
.entry-meta { font-size: 9pt; color: #444; white-space: nowrap; }
ul { margin: 3pt 0 0; padding-left: 15pt; }
li { margin-bottom: 2.5pt; }
.skills p { margin: 2pt 0; }
"""


def _esc(text: Any) -> str:
    return html.escape(str(text or ""), quote=False)


def _bullet_ids(entry: dict[str, Any]) -> list[str]:
    return [b["id"] for b in (entry.get("bullets") or []) if b.get("id")]


def build_html(
    master: dict[str, Any],
    selected_ids: list[str],
    *,
    skills_line: list[str] | None = None,
    texts: dict[str, str] | None = None,
) -> str:
    """按 **id** 组装 HTML。正文取 texts 里校验过的改写，没有就取母简历原文。

    模型给不了这里任何未经校验的文本——它只能给 id，取不到就是取不到。
    """
    index = bullet_index(master)
    chosen = [i for i in selected_ids if i in index]
    texts = texts or {}
    basics = master.get("basics") or {}

    links = basics.get("links") or {}
    contact = " · ".join(
        _esc(x) for x in (
            basics.get("email"), basics.get("phone"), basics.get("location"),
            links.get("github"), links.get("linkedin"),
        ) if x
    )

    header = [
        f"<style>{CSS}</style>",
        f"<h1>{_esc(basics.get('name'))}</h1>",
        f'<div class="contact">{contact}</div>',
    ]

    def entry_block(org: str, location: str, title: str, period: str, items: str = "") -> str:
        """第一行：公司（学校 / 项目名）靠左，地点靠右。第二行：职位靠左，时间靠右，时间那号字。

        没有职位的（多数项目）就把时间提到第一行右边，省得单独占一行。
        """
        if title:
            first_right, second = location, (title, period)
        else:
            first_right, second = location or period, (("", period) if location and period else None)
        sub = (f'<div class="entry-sub"><span>{_esc(second[0])}</span>'
               f'<span class="entry-meta">{_esc(second[1])}</span></div>') if second else ""
        body = f"<ul>{items}</ul>" if items else ""
        return (
            f'<div class="entry"><div class="entry-head">'
            f'<span class="entry-title">{org}</span>'
            f'<span class="entry-meta">{_esc(first_right)}</span></div>{sub}{body}</div>'
        )

    def bullet_section(kind: str, heading: str) -> list[str]:
        entries = _by_recency([
            e for e in (master.get(kind) or [])
            if any(b.get("id") in chosen for b in (e.get("bullets") or []))
        ])
        if not entries:
            return []
        out = [f"<h2>{heading}</h2>"]
        for entry in entries:
            org = _esc(entry.get("company") or entry.get("name"))
            url = str(entry.get("url") or "")
            if kind == "projects" and url.startswith(("https://", "http://")):
                org = f'<a href="{html.escape(url, quote=True)}">{org}</a>'
            bullets = [b for b in (entry.get("bullets") or []) if b.get("id") in chosen]
            bullets.sort(key=lambda b: chosen.index(b["id"]))
            out.append(entry_block(
                org, entry.get("location") or "",
                entry.get("title") if entry.get("company") else "", entry.get("period") or "",
                "".join(f"<li>{_esc(texts.get(b['id']) or b.get('text'))}</li>" for b in bullets),
            ))
        return out

    def education_section() -> list[str]:
        edu = _by_recency(master.get("education") or [])
        if not edu:
            return []
        out = ["<h2>Education</h2>"]
        for e in edu:
            out.append(entry_block(
                _esc(e.get("school")), e.get("location") or "",
                e.get("degree") or "", e.get("period") or "",
            ))
        return out

    def skills_section() -> list[str]:
        """按分类分组。组的先后和组内顺序都跟 skills_line 走（选材按相关度排好的）。"""
        if not skills_line:
            return []
        category = {s.get("name"): s.get("category") or "" for s in (master.get("skills") or [])}
        groups: dict[str, list[str]] = {}
        for name in skills_line:
            groups.setdefault(category.get(name) or "", []).append(name)
        if list(groups) == [""]:        # 母简历里没写分类：一行列完
            rows = f"<p>{_esc(' · '.join(skills_line))}</p>"
        else:
            # 没写分类的放最后一行、不加标签。渲染器不能往简历里塞母简历没有的词，
            # 哪怕是 "Other"——最终闸门会把它当成编造的专名拦下来（实测就是这么拦的）
            uncategorized = groups.pop("", [])
            rows = "".join(
                f"<p><b>{_esc(cat)}:</b> {_esc(', '.join(names))}</p>" for cat, names in groups.items()
            ) + (f"<p>{_esc(', '.join(uncategorized))}</p>" if uncategorized else "")
        return [f'<h2>Skills</h2><div class="skills">{rows}</div>']

    builders = {
        "education": education_section,
        "experience": lambda: bullet_section("experiences", "Experience"),
        "projects": lambda: bullet_section("projects", "Projects"),
        "skills": skills_section,
    }

    parts = list(header)
    for name in SECTION_ORDER:
        parts += builders[name]()
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
    """持有一个浏览器实例，供整个 shrink / fill 循环复用。

    每轮重开浏览器的话，砍十几轮就要跑一分钟——而这两个循环本来就要渲染多次。
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
                           margin={"top": MARGIN_Y, "bottom": MARGIN_Y,
                                   "left": MARGIN_X, "right": MARGIN_X})
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
# render → measure → shrink / fill → retry
# ---------------------------------------------------------------------------

def _drop_order(selected: list[str], index: dict[str, dict[str, Any]]) -> list[str]:
    """砍 bullet 的顺序：先砍没量化结果的，再按选材排序从后往前。

    有数字的 bullet 面试里能展开讲，没数字的替代性最强——所以先砍后者。
    """
    scored = [
        (0 if not _has_metrics(index[i]) else 1, -pos, i)
        for pos, i in enumerate(selected) if i in index
    ]
    scored.sort()
    return [i for _, _, i in scored]


def _experience_units(master: dict[str, Any]) -> dict[str, list[str]]:
    """bullet id → 它所在那段 experience 的全部 bullet id。选中其中一条就把整段放上去。"""
    units: dict[str, list[str]] = {}
    for entry in master.get("experiences") or []:
        ids = _bullet_ids(entry)
        for i in ids:
            units[i] = ids
    return units


def _expand(ids: list[str], units: dict[str, list[str]]) -> list[str]:
    """把选中的 experience bullet 展开成整段（按母简历里的顺序），项目 bullet 原样。"""
    out: list[str] = []
    for i in ids:
        for j in units.get(i, [i]):
            if j not in out:
                out.append(j)
    return out


def _ensure_experiences(
    master: dict[str, Any], current: list[str], backup: list[str], minimum: int,
) -> tuple[list[str], set[str]]:
    """保证至少 minimum 段 experience 上简历，并返回砍页时不许动的 bullet。

    选材可能觉得某段经历和岗位无关、整段不选——但只剩一段经历的简历，
    比「经历里有一条不太相关」更扣分。补的时候按时间从近到远，每段补一条：
    优先用选材给的备选，其次这段里有数字的，最后是这段的第一条。
    """
    entries = [e for e in _by_recency(master.get("experiences") or []) if _bullet_ids(e)]
    out = list(current)
    represented = [e for e in entries if any(i in out for i in _bullet_ids(e))]
    # 每段保护它在选材排序里最靠前的那条
    protected = {next(i for i in out if i in _bullet_ids(e)) for e in represented[:minimum]}

    for entry in entries:
        if len(represented) >= minimum:
            break
        if entry in represented:
            continue
        ids = _bullet_ids(entry)
        pick = next((i for i in backup if i in ids), None) or next(
            (b["id"] for b in entry["bullets"] if b.get("id") and _has_metrics(b)), ids[0]
        )
        out.append(pick)
        protected.add(pick)
        represented.append(entry)
    return out, protected


def _fill_candidates(
    master: dict[str, Any], current: list[str], dropped: list[str], backup: list[str],
) -> list[str]:
    """补满页面时往回加的顺序：先是超页被砍的（按重要性），再是选材的备选，
    最后是母简历里剩下的（按时间从近到远，有数字的优先）。"""
    seen = set(current)
    out: list[str] = []
    for i in [*reversed(dropped), *backup]:
        if i not in seen:
            out.append(i)
            seen.add(i)
    rest = [
        i for kind in ("experiences", "projects")
        for entry in _by_recency(master.get(kind) or [])
        for i in _bullet_ids(entry) if i not in seen
    ]
    index = bullet_index(master)
    rest.sort(key=lambda i: not _has_metrics(index[i]))
    return out + rest


def render_resume(
    master: dict[str, Any],
    selected_ids: list[str],
    *,
    max_pages: int = 1,
    out_dir: Path | None = None,
    basename: str = "resume",
    skills_line: list[str] | None = None,
    backup_ids: list[str] | None = None,
    min_experiences: int = MIN_EXPERIENCES,
    texts: dict[str, str] | None = None,
) -> RenderResult:
    """渲染，量页数；超页就砍最低优先级的内容，有余量就按备选往回补，直到刚好塞满 max_pages。

    砍不动了（只剩受保护的）就停，并把实际页数如实报出来——
    **宁可告诉你「压不到一页」，也不要悄悄截断内容**。
    """
    out_dir = out_dir or (config.DATA_DIR / "resumes")
    index = bullet_index(master)
    units = _experience_units(master)
    backup = [i for i in (backup_ids or []) if i in index]
    picked, keep = _ensure_experiences(
        master, [i for i in selected_ids if i in index], backup, min_experiences
    )
    current = _expand(picked, units)
    protected = set(_expand(sorted(keep), units))      # 受保护的经历不会被整段拿掉
    order = _drop_order(current, index)
    dropped: list[str] = []
    added: list[str] = []

    def build(ids: list[str]) -> str:
        return build_html(master, ids, skills_line=skills_line, texts=texts)

    markup = build(current)
    result = RenderResult(html=markup, selected=list(current))

    try:
        renderer_cm = PdfRenderer()
        renderer = renderer_cm.__enter__()
    except RenderUnavailable as exc:
        # 没有浏览器也要给出 HTML —— 它才是 diff 预览的载体。但原因必须报上去：
        # 实测吞掉原因的后果是，没 PDF、没量页数的版本一路走到了「已批准」
        result.error = (str(exc).splitlines() or ["渲染不可用"])[0]
        result.text = html_to_plain(markup)
        result.html_path = _write_html(out_dir, basename, markup)
        return result

    try:
        pdf, pages = renderer.render(markup)
        rounds = 0
        while pages > max_pages and rounds < MAX_SHRINK_ROUNDS:
            # 按超出比例批量砍，而不是一次砍一条。
            # 3 页压到 1 页时一条一条砍要几十轮；按比例估一把能几轮收敛。
            # 估多了也不怕——下面的补满环节会把多砍的加回来。
            overshoot = pages / max_pages
            want_drop = max(1, int(len(current) * (1 - 1 / overshoot) * 0.9))

            victims: list[str] = []
            for i in order:
                if len(victims) >= want_drop:
                    break
                if i in victims or i not in current:
                    continue
                if i not in units:                # 项目：按条砍
                    victims.append(i)
                    continue
                left = [u for u in units[i] if u in current and u not in victims]
                if len(left) == len(units[i]) and len(left) > 1:
                    victims.append(i)             # 这段经历还没砍过：砍这一条
                elif not protected & set(units[i]):
                    victims += left               # 已经砍过一条：只能整段拿掉（受保护的不拿）
            if not victims or len(victims) >= len(current):
                break
            for v in victims:
                current.remove(v)
                dropped.append(v)
            rounds += 1
            markup = build(current)
            pdf, pages = renderer.render(markup)

        # 有余量就补：加一次渲一次，超页就撤回，接着试下一个——长的塞不下，短的也许还能塞。
        # 实测：先试的几条是超页时被砍的长 bullet，要是连续失败就停，后面短的根本没机会
        if pages <= max_pages:
            tries = 0
            too_big: set[str] = set()       # 一段经历塞不下，就别按它的每一条 bullet 再各试一遍
            for cand in _fill_candidates(master, current, dropped, backup):
                unit = [u for u in units.get(cand, [cand]) if u not in current]
                if not unit or cand in too_big:
                    continue
                if tries >= MAX_FILL_TRIES:
                    break
                tries += 1
                trial = current + unit
                trial_markup = build(trial)
                trial_pdf, trial_pages = renderer.render(trial_markup)
                if trial_pages > max_pages:
                    too_big.update(unit)
                    continue
                current, markup, pdf, pages = trial, trial_markup, trial_pdf, trial_pages
                for u in unit:
                    if u in dropped:
                        dropped.remove(u)
                    else:
                        added.append(u)
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
        selected=current,
        dropped=dropped,
        added=added,
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
    master: dict[str, Any], selected_ids: list[str], *,
    dropped: list[str] | None = None, added: list[str] | None = None,
    rewrites: dict[str, str] | None = None,
) -> str:
    """相对母简历：选了哪些、砍了哪些、补了哪些、改写了哪些。审核门上要看的就是这个。

    改写过的条目原文和改写并排列出——校验器能保证没有凭空的数字和技术，
    保证不了意思没变，这一步要人看。
    """
    index = bullet_index(master)
    all_ids = list(index)
    chosen = [i for i in selected_ids if i in index]
    not_chosen = [i for i in all_ids if i not in chosen]
    rewrites = rewrites or {}

    lines = [f"简历上 {len(chosen)}/{len(all_ids)} 条 bullet"]
    for i in chosen:
        mark = "↳" if added and i in added else "+"
        lines.append(f"  {mark} {i}  {(index[i].get('text') or '')[:78]}")
        if i in rewrites:
            lines.append(f"      ✎ {rewrites[i]}")
    if added:
        lines.append(f"其中 {len(added)} 条（↳）是为了填满页面补进来的")
    if rewrites:
        lines.append(f"其中 {len(rewrites)} 条（✎）按 JD 关键词改写了措辞，批准前逐条对照原文，确认意思没变")
    if dropped:
        lines.append(f"因一页约束被砍 {len(dropped)} 条：")
        for i in dropped:
            lines.append(f"  ✂ {i}  {(index[i].get('text') or '')[:78]}")
    if not_chosen:
        lines.append(f"未上简历 {len(not_chosen)} 条：{', '.join(not_chosen)}")
    return "\n".join(lines)
