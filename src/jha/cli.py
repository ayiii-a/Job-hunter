"""命令行入口。

Phase 0 只有骨架相关的命令：建库、校验母简历、同步公司清单、反查 ATS、重算状态。
抓取 / 分析 / 投递等命令随后面的 Phase 加进来。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config, db, ingest, notify, profile
from .tools import resolve_ats

config.force_utf8_stdio()


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _warn(msg: str) -> None:
    print(f"  ! {msg}")


def _err(msg: str) -> None:
    print(f"  ✗ {msg}")


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    path = config.db_path()
    existed = path.exists()
    conn = db.connect(path)
    db.init_db(conn)
    tables = db.table_names(conn)
    conn.close()

    print(f"数据库：{path}" + ("（已存在，已应用缺失的表）" if existed else "（新建）"))
    _ok(f"{len(tables)} 张表：{', '.join(tables)}")

    created = config.bootstrap_configs()
    if created:
        _ok(f"从模板生成了配置文件：{', '.join(p.name for p in created)}")
        _warn("这些文件不进 git（里面会有你的个人信息），内容全部要换成你自己的")

    if not (config.ROOT / ".env").exists():
        _warn("还没有 .env。复制一份：cp .env.example .env")
    return 0


# ---------------------------------------------------------------------------
# profile check
# ---------------------------------------------------------------------------

def _print_report(title: str, rep: profile.ValidationReport) -> None:
    print(f"\n{title}")
    if rep.stats:
        print("  " + "  ".join(f"{k}={v}" for k, v in rep.stats.items()))
    for e in rep.errors:
        _err(e)
    for w in rep.warnings:
        _warn(w)
    if rep.ok and not rep.warnings:
        _ok("没有问题")
    elif rep.ok:
        _ok(f"没有错误（{len(rep.warnings)} 条提醒）")


def cmd_profile_check(args: argparse.Namespace) -> int:
    failed = False

    if config.MASTER_PROFILE_PATH.exists():
        rep = profile.validate_master_profile(profile.load_master_profile())
        _print_report(f"母简历  {config.MASTER_PROFILE_PATH.name}", rep)
        failed |= not rep.ok
    else:
        _err(f"找不到 {config.MASTER_PROFILE_PATH}——跑一次 agent init 从模板生成")
        failed = True

    if config.TARGET_PROFILE_PATH.exists():
        rep = profile.validate_target_profile(profile.load_target_profile())
        _print_report(f"目标画像  {config.TARGET_PROFILE_PATH.name}", rep)
        failed |= not rep.ok
    else:
        _err(f"找不到 {config.TARGET_PROFILE_PATH}——跑一次 agent init 从模板生成")
        failed = True

    # 结构校验查不出「一个字没改」——模板本身是合法的
    stale = config.unchanged_from_template()
    if stale:
        print()
        for p in stale:
            _warn(f"{p.name} 和模板一字不差，还没改过")
        _warn("模板里的默认值多半和你的实际情况相反，会让筛选反着工作")

    return 1 if failed else 0


# ---------------------------------------------------------------------------
# companies sync
# ---------------------------------------------------------------------------

def cmd_companies_sync(args: argparse.Namespace) -> int:
    if not config.COMPANIES_PATH.exists():
        _err(f"找不到 {config.COMPANIES_PATH}——跑一次 agent init 从模板生成")
        return 1

    data = profile.load_yaml(config.COMPANIES_PATH)
    entries: list[dict[str, Any]] = data.get("companies") or []
    if not entries:
        _warn("companies.yaml 里一家公司都没有")
        return 0

    conn = db.connect()
    db.init_db(conn)
    inserted = updated = 0
    missing_domains: list[str] = []

    for e in entries:
        name = (e.get("name") or "").strip()
        if not name:
            _err(f"有一条记录没有 name，跳过：{e}")
            continue
        domains = e.get("email_domains") or []
        if not domains:
            missing_domains.append(name)

        row = conn.execute("SELECT id FROM companies WHERE name = ?", (name,)).fetchone()
        fields = (
            e.get("ats_type"),
            e.get("board_token"),
            e.get("careers_url"),
            db.dump_json(domains),
            int(e.get("priority") or 3),
            e.get("notes"),
            1 if e.get("is_active", True) else 0,
        )
        if row:
            conn.execute(
                "UPDATE companies SET ats_type=?, board_token=?, careers_url=?, "
                "email_domains_json=?, priority=?, notes=?, is_active=? WHERE id=?",
                (*fields, row["id"]),
            )
            updated += 1
        else:
            conn.execute(
                "INSERT INTO companies (ats_type, board_token, careers_url, "
                "email_domains_json, priority, notes, is_active, name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (*fields, name),
            )
            inserted += 1

    conn.commit()
    total = conn.execute("SELECT COUNT(*) c FROM companies").fetchone()["c"]
    conn.close()

    _ok(f"新增 {inserted}，更新 {updated}，库里共 {total} 家")
    if missing_domains:
        _warn(
            f"{len(missing_domains)} 家没填 email_domains："
            f"{', '.join(missing_domains[:8])}{' ...' if len(missing_domains) > 8 else ''}"
        )
        _warn("Phase 5 靠这个字段把邮件匹配回投递记录，空着的话那家公司的邮件会全部落到人工队列")
    return 0


# ---------------------------------------------------------------------------
# resolve-ats
# ---------------------------------------------------------------------------

def cmd_resolve_ats(args: argparse.Namespace) -> int:
    print(f"排查：{args.target}")
    candidates = resolve_ats.resolve(
        args.target, name=args.name, verify=not args.no_verify
    )
    if not candidates:
        _err("没有找到任何候选。可能是 job board 由 JS 动态加载——"
             "手动打开 careers 页点进一个岗位，看那个岗位详情页的地址")
        return 1

    verified = [c for c in candidates if c.verified]
    print()
    for c in candidates[:12]:
        mark = "✓" if c.verified else " "
        count = f"{c.job_count} 个岗位" if c.job_count is not None else ""
        print(f"  {mark} {c.ats_type:<11} {c.token:<28} [{c.origin}] {count} {c.detail}")

    if verified:
        best = verified[0]
        name = args.name or (best.token if not args.target.startswith("http") else args.target)
        careers = args.target if args.target.startswith("http") else ""
        print("\n粘进 config/companies.yaml：\n")
        print(resolve_ats.format_yaml_entry(name, best, careers))
        return 0

    _warn("有候选但没有一个验证通过。上面的 detail 列说明了原因")
    return 1


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

def cmd_fetch(args: argparse.Namespace) -> int:
    if args.push_recommended and not args.analyze:
        _err("--push-recommended 要和 --analyze N 一起用：先分析，才知道哪些值得推")
        return 1
    if not config.TARGET_PROFILE_PATH.exists():
        _err("找不到 target_profile.yaml——规则初筛没有它就等于不过滤")
        return 1
    target = profile.load_target_profile()

    conn = db.connect()
    db.init_db(conn)
    reports = ingest.fetch_all(
        conn, target,
        only=args.company,
        dry_run=args.dry_run,
        fetch_details=not args.no_detail,
        delay=0.0 if args.fast else ingest.DETAIL_DELAY_SECONDS,
    )
    if not reports:
        _warn("没有可抓的公司。先 agent companies sync")
        conn.close()
        return 1

    print()
    for r in reports:
        (_ok if r.ok else _err)(r.headline)
        if args.explain and r.screen_reasons:
            for reason, n in list(r.screen_reasons.items())[:6]:
                print(f"      初筛丢弃 · {reason}: {n}")

    if args.explain:
        _print_dropped_samples(reports)

    new_jobs = [j for r in reports for j in r.new_jobs]
    failures = ingest.failing_sources(conn)

    if failures:
        print()
        _err(f"{len(failures)} 家最近一次抓取是失败的：")
        for f in failures:
            print(f"      {f['name']}（{f['source']}）：{(f['error'] or '')[:100]}")

    if args.analyze and not args.dry_run:
        try:
            return _analyze_and_push(conn, args, failures)
        finally:
            conn.close()
    conn.close()

    print()
    digest = notify.format_digest(new_jobs, failures)
    if args.notify:
        res = notify.send(digest)
        (_ok if res.sent else _warn)(
            f"推送到 {res.channel}" if res.sent else f"推送未发出：{res.detail}"
        )
        if not res.sent:
            print("\n" + digest)
    else:
        print(digest)
        if not args.dry_run and notify.configured():
            print("\n（加 --notify 可以推到 Discord）")
    return 0


#: 推送给你的档位
RECOMMENDED = ["strong_apply", "apply"]


def _analyze_and_push(conn: Any, args: argparse.Namespace, failures: list[Any]) -> int:
    """抓完之后：按 analyze_first 关键词排队分析一批，把新判为推荐投递的按推荐顺序推出去。

    整条路不经过 agent：分析在第二层单次调用里做，排序复用 rank_jobs（档位优先，
    同档有内推的在前，再按分数），推送文本只含结构化字段。
    """
    from . import analyze as analyze_mod
    from .agent import MissingAPIKey
    from .agent import tools as tools_mod

    try:
        results = analyze_mod.analyze_jobs(conn, limit=args.analyze)
    except MissingAPIKey as exc:
        _err(str(exc))
        return 1
    fresh = [r.job_id for r in results if r.verdict in RECOMMENDED]
    ranked = tools_mod.rank_jobs(conn, verdicts=RECOMMENDED, job_ids=fresh, limit=100) if fresh else []

    print()
    _ok(f"分析 {len(results)} 个，新增推荐投递 {len(ranked)} 个")
    text = notify.format_recommended(ranked, analyzed=len(results), failures=failures)
    if not args.push_recommended:
        if text:
            print()
            print(text)
        return 0
    if not text:
        _ok("没有新增推荐，也没有抓取失败——不推送")
        return 0
    res = notify.send(text)
    if not res.sent:
        _err(f"推送失败：{res.detail}")
        return 1
    _ok("已推送到 Discord")
    return 0


def _print_dropped_samples(reports: list[ingest.FetchReport]) -> None:
    """抽样展示被初筛丢掉的岗位。

    路线图要求前两周每天看漏报和误报来调过滤条件。被丢弃的岗位不入库
    （不然 5,000+ 条噪音会淹掉数据库），所以要在这里当场看。
    """
    for r in reports:
        if not r.dropped:
            continue
        print(f"\n  {r.company} 丢弃样本：")
        for job, res in r.dropped[:8]:
            print(f"      {job.title[:52]:<52} | {job.location[:22]:<22} | {res.reason}")


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------

def cmd_jobs_list(args: argparse.Namespace) -> int:
    conn = db.connect()
    db.init_db(conn)
    sql = (
        "SELECT j.id, c.name company, j.title, j.location, j.salary_raw, "
        "j.screen_tier, j.is_active, j.first_seen_at "
        "FROM jobs j LEFT JOIN companies c ON c.id = j.company_id "
    )
    sql += "WHERE 1=1 " if args.all else "WHERE j.is_active = 1 AND j.screened_out_at IS NULL "
    params: list[Any] = []
    if args.company:
        sql += "AND lower(c.name) = lower(?) "
        params.append(args.company)
    if args.tier:
        sql += "AND j.screen_tier = ? "
        params.append(args.tier)
    sql += "ORDER BY j.screen_tier, j.first_seen_at DESC LIMIT ?"
    params.append(args.limit)

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    if not rows:
        _warn("库里没有匹配的岗位。先跑 agent fetch")
        return 0

    for r in rows:
        flag = "" if r["is_active"] else " (已下架)"
        tier = f"[{r['screen_tier']}] " if r["screen_tier"] else ""
        print(f"#{r['id']:<5} {tier}{r['company']} — {r['title']}{flag}")
        extra = " · ".join(x for x in (r["location"], r["salary_raw"]) if x)
        if extra:
            print(f"       {extra}")
    print(f"\n共 {len(rows)} 条")
    return 0


def cmd_jobs_show(args: argparse.Namespace) -> int:
    conn = db.connect()
    db.init_db(conn)
    row = conn.execute(
        "SELECT j.*, c.name company FROM jobs j "
        "LEFT JOIN companies c ON c.id = j.company_id WHERE j.id = ?",
        (args.job_id,),
    ).fetchone()
    if row is None:
        conn.close()
        _err(f"没有 id 为 {args.job_id} 的岗位")
        return 1

    contacts = conn.execute(
        "SELECT name, relationship, strength FROM contacts WHERE company_id = ? "
        "ORDER BY strength DESC",
        (row["company_id"],),
    ).fetchall()
    conn.close()

    print(f"#{row['id']}  {row['company']} — {row['title']}")
    for label, value in (
        ("地点", row["location"]), ("远程", row["remote_type"]),
        ("薪资", row["salary_raw"]), ("档位", row["screen_tier"]),
        ("发布", row["posted_at"]), ("首见", row["first_seen_at"]),
        ("最近可见", row["last_seen_at"]), ("链接", row["url"]),
    ):
        if value:
            print(f"  {label}：{value}")
    if not row["is_active"]:
        _warn("这个岗位已经下架了（JD 全文仍然留着，面试时还用得上）")

    if contacts:
        print(f"\n  ★ 这家你认识人 —— 先要内推，别直接投：")
        for c in contacts:
            rel = f"（{c['relationship']}）" if c["relationship"] else ""
            print(f"      {c['name']}{rel}  关系强度 {c['strength']}")

    jd = row["jd_text"] or ""
    print(f"\n  JD（{len(jd)} 字）")
    print("  " + "-" * 60)
    body = jd if args.full else jd[:1500]
    for line in body.splitlines():
        print("  " + line)
    if not args.full and len(jd) > 1500:
        print(f"\n  …… 还有 {len(jd) - 1500} 字，加 --full 看全文")
    return 0


# ---------------------------------------------------------------------------
# agent
# ---------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> int:
    from . import schedules as sched_mod
    from .agent import AgentClient, Budget, MissingAPIKey, Permission, run as agent_run

    task = args.task
    tool_names: set[str] | None = None
    allow: set[Permission] | None = None
    max_turns, max_calls = args.max_turns, args.max_calls
    allow_notify, schedule_name = args.allow_notify, None

    if args.schedule:
        try:
            s = sched_mod.load(args.schedule)
        except sched_mod.ScheduleError as exc:
            _err(str(exc))
            return 1
        schedule_name = s.name
        task = s.task
        tool_names, allow = s.tools, s.permissions
        max_turns, max_calls = s.max_turns, s.max_llm_calls
        # 定义里授权了才放行；命令行的 --allow-notify 仍可单次追加
        allow_notify = allow_notify or s.allow_notify
        print(f"定时任务：{s.name}   {s.summary()}\n")
    elif not task:
        _err("要么给一句任务，要么用 --schedule <名字>。看有哪些：agent schedules")
        return 1

    conn = db.connect()
    db.init_db(conn)

    if args.read_only:
        allow, tool_names = {Permission.READ}, None
    # GATED 工具默认一律拒绝。放行是【单次】的，不延续到下一次 run ——
    # 外发动作不该因为你上次同意过就自动发生。
    approve = (lambda name, a: name == "send_notification") if allow_notify else None

    print(f"任务：{task}\n")
    try:
        result = agent_run(
            task,
            conn,
            client=AgentClient(model=args.model),
            budget=Budget(max_llm_calls=max_calls),
            max_turns=max_turns,
            allow=allow,
            tool_names=tool_names,
            approve=approve,
            schedule_name=schedule_name,
            on_step=lambda st: print(st.render()),
        )
    except MissingAPIKey as exc:
        _err(str(exc))
        return 1
    finally:
        conn.close()

    print("\n" + "─" * 62)
    if result.final_text:
        print(result.final_text)
    b = result.budget
    print(
        f"\n{result.stopped_because} · 工具调用 {result.tool_calls} 次 · "
        f"LLM {b['llm_calls']} 次 · {b['input_tokens']}/{b['output_tokens']} tokens · "
        f"约 ${b['cost_usd']}"
    )
    if result.pending_approvals:
        _warn(f"{len(result.pending_approvals)} 个外发动作没有执行（需要你批准）：")
        for p in result.pending_approvals:
            print(f"      {p['tool']}  {json.dumps(p['args'], ensure_ascii=False)[:120]}")
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    from .agent import REGISTRY, Permission

    labels = {
        Permission.READ: "只读本地数据，随便调",
        Permission.WRITE: "写本地库；events 可追加纠错，所以允许自动执行",
        Permission.GATED: "外发或不可逆，必须单次批准",
    }
    for perm in Permission:
        items = [(n, t) for n, t in REGISTRY.items() if t.permission is perm]
        print(f"\n{perm.value.upper()}  —— {labels[perm]}")
        for name, t in items:
            print(f"  {name:<22} {t.description.splitlines()[0][:64]}")

    print("\n刻意不存在的工具（这是安全边界，不是待办）：")
    for name, why in (
        ("submit_application", "提交永远由你在浏览器里按"),
        ("send_email / reply", "邮件只读，不实现写路径"),
        ("open_url / click_link", "邮件里的链接原样给你，agent 不点"),
        ("delete_* / update_event", "events 追加式，数据库层面也禁止改删"),
    ):
        print(f"  {name:<22} {why}")
    return 0


def cmd_spend(args: argparse.Namespace) -> int:
    from .agent import UNPRICED_MODELS, approval_counts, cost_by_schedule, spend_summary

    conn = db.connect()
    db.init_db(conn)
    rows = spend_summary(conn, days=args.days)
    by_task = cost_by_schedule(conn, days=args.days)
    approvals = approval_counts(conn)
    conn.close()

    if not rows and not by_task:
        _warn(f"最近 {args.days} 天没有 LLM 调用记录")
        return 0

    if rows:
        print(f"最近 {args.days} 天 · 按用途\n")
        print(f"  {'用途':<20} {'次数':>6} {'输入':>10} {'输出':>10} {'成本':>10}")
        for r in rows:
            print(
                f"  {r['purpose']:<20} {r['calls']:>6} {r['inp'] or 0:>10} "
                f"{r['out'] or 0:>10} {'$' + str(r['cost'] or 0):>10}"
            )
        print(f"\n  合计 ${round(sum(r['cost'] or 0 for r in rows), 4)}")

    if by_task:
        # 定时任务跑起来之后，这才是「钱花在哪」的正确视图——
        # purpose 只能说明花在哪类调用上，说不出是哪个任务触发的
        print(f"\n最近 {args.days} 天 · 按任务\n")
        print(f"  {'任务':<20} {'run':>5} {'LLM':>6} {'工具':>6} {'成本':>10}")
        for r in by_task:
            print(
                f"  {r['name']:<20} {r['runs']:>5} {r['calls'] or 0:>6} "
                f"{r['tools'] or 0:>6} {'$' + str(r['cost'] or 0):>10}"
            )

    gated = [a for a in approvals if a["denied"] or a["executed"]]
    if gated:
        print("\n工具调用次数（GATED 工具的「权限毕业」依据）\n")
        for a in gated:
            print(f"  {a['tool_name']:<22} 执行 {a['executed']:>4}  被拒 {a['denied']:>4}")

    if UNPRICED_MODELS:
        _warn(f"这些模型没有价格表，成本被记成 0：{sorted(UNPRICED_MODELS)}")
    return 0


# ---------------------------------------------------------------------------
# runs —— agent 干过什么
# ---------------------------------------------------------------------------

def cmd_runs(args: argparse.Namespace) -> int:
    from .agent import recent_runs, run_steps

    conn = db.connect()
    db.init_db(conn)

    if args.show:
        row = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (args.show,)).fetchone()
        if row is None:
            conn.close()
            _err(f"没有 id 为 {args.show} 的 run")
            return 1
        steps = run_steps(conn, args.show)
        conn.close()
        print(f"#{row['id']}  {row['task']}")
        print(f"  {row['started_at']} → {row['finished_at'] or '(未结束)'}  {row['stopped_because'] or ''}")
        print(f"  {row['model']} · LLM {row['llm_calls']} 次 · 工具 {row['tool_calls']} 次 · ${row['cost_usd']}")
        print("\n  轨迹")
        for s in steps:
            mark = {"tool_use": "→", "denied": "⛔", "error": "✗", "text": " "}.get(s["kind"], " ")
            body = s["args_json"] or s["error"] or s["result_summary"] or ""
            print(f"   {s['seq']:>3} {mark} {(s['tool_name'] or s['kind']):<20} {body[:90]}")
        if row["final_text"]:
            print(f"\n  结论\n   {row['final_text'][:600]}")
        return 0

    runs = recent_runs(conn, limit=args.limit, schedule=args.schedule)
    conn.close()
    if not runs:
        _warn("还没有 agent run 记录")
        return 0
    print(f"{'id':>5}  {'开始':<20} {'任务':<38} {'工具':>5} {'成本':>9}")
    for r in runs:
        flag = "" if r["ok"] else " !"
        print(
            f"{r['id']:>5}  {(r['started_at'] or '')[:19]:<20} {r['task'][:36]:<38} "
            f"{r['tool_calls']:>5} {'$' + str(round(r['cost_usd'] or 0, 4)):>9}{flag}"
        )
    print(f"\n看某次的完整轨迹：agent runs --show <id>")
    return 0


# ---------------------------------------------------------------------------
# analyze —— 不经过 agent loop，直接跑第二层分析
# ---------------------------------------------------------------------------

def cmd_analyze(args: argparse.Namespace) -> int:
    from . import analyze as analyze_mod
    from .agent import MissingAPIKey

    conn = db.connect()
    db.init_db(conn)
    pending = analyze_mod.pending_jobs(
        conn, limit=args.limit,
        priority_terms=profile.load_target_profile().get("analyze_first") or (),
    )
    if not pending:
        conn.close()
        _ok("没有需要分析的岗位（都分析过了）")
        return 0

    print(f"待分析 {len(pending)} 个岗位，每个一次独立调用（{analyze_mod.ANALYZER_MODEL}）\n")
    try:
        results = analyze_mod.analyze_jobs(conn, limit=args.limit)
    except MissingAPIKey as exc:
        conn.close()
        _err(str(exc))
        return 1
    conn.close()

    by_verdict: dict[str, int] = {}
    for r in results:
        by_verdict[r.verdict] = by_verdict.get(r.verdict, 0) + 1
        mark = {"strong_apply": "★", "apply": "+", "stretch": "~", "skip": "-"}.get(r.verdict, " ")
        extra = f" 硬性排除：{r.hard_fail}" if r.hard_fail else ""
        if r.error:
            extra = f" ✗ {r.error}"
        print(f"  {mark} #{r.job_id:<5} {r.verdict:<13} {(r.rationale or '')[:60]}{extra}")
    print(f"\n  {by_verdict}")
    return 0


def cmd_schedules(args: argparse.Namespace) -> int:
    from . import schedules as sched_mod

    try:
        all_ = sched_mod.load_all()
    except sched_mod.ScheduleError as exc:
        _err(str(exc))
        return 1
    if not all_:
        _warn(f"还没有定时任务。跑 agent init 从模板生成 {config.SCHEDULES_PATH.name}")
        return 0

    for s in all_.values():
        print(f"\n  {s.summary()}")
        if s.notes:
            print(f"    {s.notes}")
        if s.tools:
            print(f"    工具：{', '.join(sorted(s.tools))}")
        first = next((l for l in s.task.splitlines() if l.strip()), "")
        print(f"    任务：{first[:66]}...")

    print(f"\n跑一个：agent run --schedule <名字>")
    print(f"定义在：{config.SCHEDULES_PATH}")
    return 0


# ---------------------------------------------------------------------------
# resume —— 定制、看 diff、审核门
# ---------------------------------------------------------------------------

def cmd_tailor(args: argparse.Namespace) -> int:
    from . import tailor as tailor_mod
    from .agent import MissingAPIKey

    conn = db.connect()
    db.init_db(conn)
    try:
        res = tailor_mod.tailor_resume(conn, args.job_id, max_pages=args.max_pages,
                                       rewrite=not args.no_rewrite)
    except (MissingAPIKey, ValueError) as exc:
        conn.close()
        _err(str(exc))
        return 1
    conn.close()

    if res.error:
        _err(res.error)
        return 1

    print(f"岗位 #{res.job_id} → 简历版本 #{res.resume_version_id}\n")
    print(res.diff)
    print()
    if res.rationale:
        print(f"选材理由：{res.rationale}\n")

    if res.page_count:
        fits = res.page_count <= args.max_pages
        (_ok if fits else _warn)(
            f"{res.page_count} 页" + ("" if fits else f"（压不到 {args.max_pages} 页，砍无可砍）")
        )
    if res.dropped_for_length:
        _warn(f"为压页数砍掉 {len(res.dropped_for_length)} 条：{', '.join(res.dropped_for_length)}")
    if res.rewrites:
        _warn(f"{len(res.rewrites)} 条按 JD 关键词改写了措辞（diff 里 ✎ 那几行）——批准前逐条对照原文，确认意思没变")
    if res.rewrite_rejected:
        _warn(f"{len(res.rewrite_rejected)} 条改写没过校验，用了原文")

    if res.verify_ok:
        _ok("幻觉校验通过：产出物里没有母简历以外的数字或专名")
    else:
        _err("幻觉校验未通过——**不要投这一版**")
        for p in res.verify_problems:
            print(f"      {p}")

    if res.pdf_path:
        print(f"\n  PDF   {res.pdf_path}")
    if res.html_path:
        print(f"  HTML  {res.html_path}")

    if not res.pdf_path:
        # 不能只是少打一行 PDF——实测这样没人发现，没 PDF、没量页数的版本一路批准了
        _err(f"没生成 PDF，页数也没量，这一版批准不了：{res.render_error or '原因不明'}")
        print("      多半是这个终端里没装浏览器：./.venv/Scripts/python.exe -m playwright install chromium")
        print("      装好后重新 agent tailor")
        return 1

    print(f"\n这一版**还没过审核门**。看完 diff 和 PDF 之后：")
    print(f"  agent resume approve {res.resume_version_id}")
    return 0


def cmd_resume_approve(args: argparse.Namespace) -> int:
    from . import tailor as tailor_mod

    conn = db.connect()
    db.init_db(conn)
    row = tailor_mod.get_version(conn, args.resume_version_id)
    if row is None:
        conn.close()
        _err(f"没有 id 为 {args.resume_version_id} 的简历版本")
        return 1
    if row.get("approved_at"):
        conn.close()
        _ok(f"#{args.resume_version_id} 早就批准过了（{row['approved_at']}）")
        return 0

    print(row.get("diff_summary") or "")
    print(f"\n  PDF {row.get('rendered_pdf_path') or '(无)'}  ·  {row.get('page_count')} 页")
    try:
        tailor_mod.approve(conn, args.resume_version_id)
    except ValueError as exc:
        _err(str(exc))
        return 1
    finally:
        conn.close()
    _ok(f"#{args.resume_version_id} 已批准，可以拿去投了")
    return 0


def cmd_resume_list(args: argparse.Namespace) -> int:
    conn = db.connect()
    db.init_db(conn)
    rows = conn.execute(
        "SELECT rv.id, rv.page_count, rv.approved_at, rv.created_at, "
        "j.title, c.name AS company FROM resume_versions rv "
        "LEFT JOIN jobs j ON j.id = rv.generated_for_job_id "
        "LEFT JOIN companies c ON c.id = j.company_id "
        "ORDER BY rv.created_at DESC LIMIT ?", (args.limit,)
    ).fetchall()
    conn.close()
    if not rows:
        _warn("还没有生成过简历版本。先 agent tailor <job_id>")
        return 0
    for r in rows:
        mark = "✓" if r["approved_at"] else "·"
        state = "已批准" if r["approved_at"] else "待审核"
        print(f" {mark} #{r['id']:<4} {state}  {r['page_count'] or '?'} 页  "
              f"{(r['company'] or ''):<12} {(r['title'] or '')[:44]}")
    return 0


# ---------------------------------------------------------------------------
# Phase 4：投递记录与追踪
# ---------------------------------------------------------------------------

def cmd_applied(args: argparse.Namespace) -> int:
    """记录一次【你已经手动投完】的投递。这个命令不会替你提交任何表单。"""
    from . import tracking

    conn = db.connect()
    db.init_db(conn)

    job = conn.execute(
        "SELECT j.*, c.name AS company FROM jobs j "
        "LEFT JOIN companies c ON c.id = j.company_id WHERE j.id = ?", (args.job_id,)
    ).fetchone()
    if job is None:
        conn.close()
        _err(f"没有 id 为 {args.job_id} 的岗位")
        return 1
    if conn.execute("SELECT 1 FROM applications WHERE job_id = ?", (args.job_id,)).fetchone():
        conn.close()
        _err(f"岗位 {args.job_id} 已经有投递记录了")
        return 1

    limit = tracking.daily_limit_status(conn)
    if limit["exceeded"] and not args.force:
        conn.close()
        _err(f"今天已投 {limit['used']} 家，达到上限 {limit['limit']}")
        _warn("上限的用途不是省力，是逼你投得准——确实要多投加 --force")
        return 1

    if args.resume_version:
        rv = conn.execute(
            "SELECT approved_at FROM resume_versions WHERE id = ?", (args.resume_version,)
        ).fetchone()
        if rv is None:
            conn.close()
            _err(f"没有 id 为 {args.resume_version} 的简历版本")
            return 1
        if not rv["approved_at"]:
            conn.close()
            _err(f"简历版本 {args.resume_version} 还没过审核门：agent resume approve {args.resume_version}")
            return 1

    contact_id = None
    if args.referral:
        row = conn.execute(
            "SELECT id FROM contacts WHERE lower(name) = lower(?) AND company_id = ?",
            (args.referral, job["company_id"]),
        ).fetchone()
        if row is None:
            conn.close()
            _err(f"{job['company']} 下没有叫「{args.referral}」的联系人")
            return 1
        contact_id = row["id"]

    ts = datetime.now().isoformat(sep=" ", timespec="seconds")
    cur = conn.execute(
        "INSERT INTO applications (job_id, resume_version_id, referred_by_contact_id, "
        "applied_at, applied_via, notes) VALUES (?,?,?,?,?,?)",
        (args.job_id, args.resume_version, contact_id, ts, args.via, args.notes),
    )
    app_id = int(cur.lastrowid)
    db.append_event(conn, app_id, "applied", source="manual")
    after = tracking.daily_limit_status(conn)
    conn.close()

    _ok(f"投递 #{app_id}：{job['company']} — {job['title']}（{args.via}）")
    if contact_id:
        _ok(f"内推人：{args.referral}")
    print(f"      今天 {after['used']}/{after['limit']}")
    _warn("24 小时内没收到确认邮件的话，去 ATS 查一下是不是没投成功："
          f"agent confirm {app_id}")
    return 0


def cmd_confirm(args: argparse.Namespace) -> int:
    from . import tracking

    conn = db.connect()
    db.init_db(conn)
    try:
        out = tracking.mark_confirmed(conn, args.application_id)
    except ValueError as exc:
        conn.close()
        _err(str(exc))
        return 1
    conn.close()
    _ok(f"投递 #{out['application_id']} 已记录确认邮件（{out['confirmation_seen_at']}）")
    return 0


def cmd_board(args: argparse.Namespace) -> int:
    from . import tracking

    conn = db.connect()
    db.init_db(conn)
    rows = tracking.tracking_rows(conn)
    missing = tracking.missing_confirmations(conn)
    limit = tracking.daily_limit_status(conn)
    conn.close()

    if not rows:
        _warn("还没有投递记录。投完之后：agent applied <job_id> --via referral")
        return 0

    print(f"{'id':>4}  {'公司':<14}{'岗位':<34}{'状态':<15}{'投递日':<12}{'渠道':<14}内推")
    for r in rows:
        print(f"{r.application_id:>4}  {r.company[:13]:<14}{r.title[:33]:<34}"
              f"{(r.status or ''):<15}{(r.applied_at or '')[:10]:<12}"
              f"{r.applied_via:<14}{r.referral}")

    todo = [r for r in rows if r.next_step]
    if todo:
        print("\n下一步（规则算的，不是猜的）")
        for r in todo:
            print(f"  #{r.application_id} {r.company} — {r.next_step}")

    if missing:
        print()
        _err(f"{len(missing)} 条投出去超过 24h 还没收到确认邮件：")
        for m in missing:
            print(f"      #{m['application_id']} {m['company']} — {m['title'][:40]}"
                  f"（{m['hours_since']}h）")
        print("      确认邮件是「申请真的进系统了」的唯一地面真相")

    print(f"\n共 {len(rows)} 条 · 今天 {limit['used']}/{limit['limit']}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from . import tracking

    conn = db.connect()
    db.init_db(conn)
    if args.sheet:
        out = tracking.sync_to_sheet(conn)
        conn.close()
        if out.get("synced"):
            _ok(f"已写入 Google Sheet：{out['rows']} 行")
            return 0
        _err(out.get("reason", "同步失败"))
        return 1

    path = tracking.export_file(conn, Path(args.out) if args.out else None,
                                delimiter="," if args.csv else "\t")
    n = len(tracking.tracking_rows(conn))
    conn.close()
    _ok(f"{n} 行 → {path}")
    if not args.csv:
        print("      TSV 可以直接全选复制、粘进 Google Sheet 自动分列")
    print("      数据库是真相源，Sheet 只是只读视图——改 Sheet 不会回写")
    return 0


def cmd_answers(args: argparse.Namespace) -> int:
    from . import questions

    conn = db.connect()
    db.init_db(conn)
    job = conn.execute(
        "SELECT j.title, c.name AS company FROM jobs j "
        "LEFT JOIN companies c ON c.id = j.company_id WHERE j.id = ?", (args.job_id,)
    ).fetchone()
    conn.close()
    if job is None:
        _err(f"没有 id 为 {args.job_id} 的岗位")
        return 1

    qs = [q.strip() for q in config.read_text(args.questions).splitlines() if q.strip()] \
        if args.questions else list(args.question or [])
    if not qs:
        _err("给几个问题：--question '...' 可以重复，或 --questions <每行一题的文件>")
        return 1

    answers = questions.answer_all(qs, company=job["company"] or "", role=job["title"] or "")
    marks = {"verbatim": "✓", "never": "⛔", "draft": "~", "uncovered": "✗"}
    for a in answers:
        print(f"\n{marks[a.kind]} {a.question}")
        if a.text:
            for line in a.text.splitlines():
                print(f"    {line}")
        if a.note:
            print(f"    · {a.note}")

    s = questions.summarize(answers)
    print(f"\n可直接粘贴 {s['ready_to_paste']} 题 · 需要你处理 {s['needs_you']} 题")
    _warn("⛔ 那几题 agent 一个字都不填——自愿披露和法律敏感项只能你本人决定")
    return 0


# ---------------------------------------------------------------------------
# OpenClaw 外壳
# ---------------------------------------------------------------------------

def cmd_openclaw_config(args: argparse.Namespace) -> int:
    from . import openclaw
    from . import schedules as sched_mod

    try:
        bundle = openclaw.generate(
            sched_mod.load_all(), settings=openclaw.load_settings(),
            discord_id=args.discord_id or config.env("DISCORD_USER_ID"),
            python_path=args.python, agent_path=args.agent,
        )
    except (sched_mod.ScheduleError, openclaw.ShellConfigError) as exc:
        _err(str(exc))
        return 1

    for w in bundle.warnings:
        _warn(w)
    if args.out:
        for p in openclaw.write_bundle(bundle, Path(args.out)):
            _ok(f"写入 {p}")
    else:
        print(json.dumps(bundle.config, ensure_ascii=False, indent=2))
        print()
        print("# cron 作业")
        for c in bundle.cron_commands:
            print(c)
        for aid, text in sorted(bundle.agents_md.items()):
            print()
            print(f"# {aid}/AGENTS.md")
            print(text)

    print()
    print("下一步由你来做（这里不会碰 ~/.openclaw）：")
    print("  1. 在 WSL 里跑 openclaw config schema，核对上面的键名——OpenClaw 迭代快")
    print("  2. 把片段合进 openclaw.json，AGENTS.md 放进对应的 workspace")
    print("  3. agent openclaw verify <openclaw.json 的路径>")
    print("  4. 逐条确认后再跑 cron 命令")
    return 0


def cmd_openclaw_verify(args: argparse.Namespace) -> int:
    from . import openclaw
    from . import schedules as sched_mod

    try:
        cfg = openclaw.load_config(Path(args.path))
        problems = openclaw.verify(cfg, sched_mod.load_all(),
                                   discord_id=config.env("DISCORD_USER_ID"))
    except (sched_mod.ScheduleError, openclaw.ShellConfigError) as exc:
        _err(str(exc))
        return 1

    if problems:
        for p in problems:
            _err(p)
        print()
        print(f"{len(problems)} 处不满足。外壳的边界是配置，改松了它不会报错——所以这里报。")
        return 1
    _ok("外壳配置通过检查")
    _warn("配置文件里查不到的几件事要你自己确认：没装 ClawHub 上的 skill、OpenClaw 版本已固定、WSL 保活已配")
    return 0


# ---------------------------------------------------------------------------
# Phase 5：邮件
# ---------------------------------------------------------------------------

def cmd_mail_sweep(args: argparse.Namespace) -> int:
    from .agent import MissingAPIKey
    from .mail import pipeline as mp
    from .mail.imap import MailError, MailReader

    conn = db.connect()
    db.init_db(conn)
    fetched = new = 0
    try:
        if not args.no_fetch:
            fetched, new = mp.ingest(conn, MailReader(), since_days=args.since_days)
            _ok(f"拉取 {fetched} 封，新增 {new} 封（只读：EXAMINE + BODY.PEEK，不会把邮件标成已读）")
        rep = mp.process_pending(conn, limit=args.limit)
    except (MailError, MissingAPIKey) as exc:
        if not args.no_fetch:
            mp.record_sweep(conn, fetched=fetched, new=new, error=str(exc))
        conn.close()
        _err(str(exc))
        return 1
    if not args.no_fetch:
        # 只有真的去邮箱拉过才算一次检测——--no-fetch 不能让「检测已过期」的告警闭嘴
        mp.record_sweep(conn, fetched=fetched, new=new, rep=rep)
    conn.close()

    print(f"  预过滤丢弃 {rep.filtered} · 分类 {rep.classified} · 自动写入 {rep.auto_applied}"
          f" · 进人工队列 {rep.queued} · 忽略 {rep.ignored}")
    for e in rep.errors:
        _warn(e)
    if rep.alerts:
        print()
        _err(f"{len(rep.alerts)} 封需要你尽快处理：")
        for a in rep.alerts:
            where = f"{a.get('company') or '?'} — {a.get('job_title') or '未匹配'}"
            print(f"      #{a['email_id']} [{a.get('type')}] {where}")
        if args.push_alerts:
            # 确定性推送：不经过模型，文本只含库里的字段（mail/pipeline.py::alert_text）
            res = notify.send(mp.alert_text(rep.alerts))
            if not res.sent:
                _err(f"推送失败：{res.detail}")
                return 1
            _ok("已推送到 Discord")
    if rep.queued:
        print("\n  看队列：agent mail queue")
    return 0


def cmd_mail_queue(args: argparse.Namespace) -> int:
    from .mail import pipeline as mp

    conn = db.connect()
    db.init_db(conn)
    rows = mp.queue_for_human(conn)
    conn.close()
    if not rows:
        _ok("人工确认队列是空的")
        return 0
    for r in rows:
        where = f"{r['company']} — {r['job_title']}" if r["company"] else "未匹配到投递"
        print(f"\n#{r['id']}  [{r['classification']}]  置信度 {round(r['confidence'] or 0, 2)}  {where}")
        print(f"    主题：{(r['subject'] or '')[:90]}")
        print(f"    发件：{(r['from_addr'] or '')[:70]}")
        if r["summary"]:
            print(f"    摘要：{r['summary'][:100]}")
        for d in db.load_json(r["dates_json"]):
            print(f"    日期原文：「{d.get('original')}」{('— ' + d['meaning']) if d.get('meaning') else ''}")
        print(f"    为什么进队列：{r['reason']}")
    print("\n确认：agent mail accept <id> [--application <投递id>]   驳回：agent mail dismiss <id>")
    print("看链接和全文：agent mail show <id>")
    return 0


def cmd_mail_show(args: argparse.Namespace) -> int:
    conn = db.connect()
    db.init_db(conn)
    r = conn.execute("SELECT * FROM emails WHERE id = ?", (args.email_id,)).fetchone()
    conn.close()
    if r is None:
        _err(f"没有 id 为 {args.email_id} 的邮件")
        return 1
    print(f"#{r['id']}  {r['received_at'] or ''}")
    print(f"发件：{r['from_addr']}\n主题：{r['subject']}")
    print(f"分类：{r['classification'] or '(未分类)'}  置信度 {r['confidence']}  处理：{r['policy']}  "
          f"队列：{r['review_status'] or '-'}")
    if r["reason"]:
        print(f"原因：{r['reason']}")
    for d in db.load_json(r["dates_json"]):
        print(f"日期原文：「{d.get('original')}」 {d.get('meaning') or ''}")
    links = db.load_json(r["links_json"])
    if links:
        print("\n链接（原文。agent 不会打开它们——先确认是正规招聘域名再点）：")
        for u in links:
            print(f"  {u}")
    print("\n" + "-" * 60)
    print((r["body_text"] or "")[:3000])
    return 0


def cmd_mail_accept(args: argparse.Namespace) -> int:
    from .mail import pipeline as mp

    conn = db.connect()
    db.init_db(conn)
    try:
        out = mp.accept(conn, args.email_id, application_id=args.application)
    except ValueError as exc:
        conn.close()
        _err(str(exc))
        return 1
    conn.close()
    _ok(f"邮件 #{out['email_id']} → 投递 #{out['application_id']} 追加 {out['event']}，"
        f"当前状态 {out['status']}")
    if out["event"] in ("interview_invite", "oa_invite", "offer_received"):
        print(f"      面试准备材料：agent prep {out['application_id']}")
    return 0


def cmd_mail_dismiss(args: argparse.Namespace) -> int:
    from .mail import pipeline as mp

    conn = db.connect()
    db.init_db(conn)
    try:
        mp.dismiss(conn, args.email_id, note=args.note or "")
    except ValueError as exc:
        conn.close()
        _err(str(exc))
        return 1
    conn.close()
    _ok(f"邮件 #{args.email_id} 已驳回，不改任何状态")
    return 0


def cmd_prep(args: argparse.Namespace) -> int:
    from . import prep

    conn = db.connect()
    db.init_db(conn)
    try:
        path = prep.write(conn, args.application_id)
    except ValueError as exc:
        conn.close()
        _err(str(exc))
        return 1
    conn.close()
    text = config.read_text(path)
    print(text)
    _ok(f"已写入 {path}")
    return 0


# ---------------------------------------------------------------------------
# status rebuild
# ---------------------------------------------------------------------------

def cmd_status_rebuild(args: argparse.Namespace) -> int:
    conn = db.connect()
    db.init_db(conn)
    result = db.rebuild_all_statuses(conn)
    conn.close()

    print(f"重算了 {result['total']} 条 application")
    for app_id, before, after in result["changed"]:
        print(f"  #{app_id}: {before} -> {after}")
    if not result["changed"]:
        _ok("没有变化")
    if result["orphans"]:
        _err(f"这些 application 一条事件都没有（数据错误）：{result['orphans']}")
    if result["unknown_event_types"]:
        _err(
            "遇到未登记的事件类型，它们被静默忽略了，请到 status.py 里登记："
            f"{result['unknown_event_types']}"
        )
    return 0


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

def cmd_stats(args: argparse.Namespace) -> int:
    conn = db.connect()
    db.init_db(conn)
    print(f"数据库：{config.db_path()}\n")
    for table in db.table_names(conn):
        if table == "schema_version":
            continue
        n = conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        print(f"  {table:<18} {n}")

    rows = conn.execute(
        "SELECT status, COUNT(*) c FROM applications GROUP BY status ORDER BY c DESC"
    ).fetchall()
    if rows:
        print("\n  投递状态分布")
        for r in rows:
            print(f"    {str(r['status']):<16} {r['c']}")
    conn.close()
    return 0


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent", description="Job Hunting Agent")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="建库/补表").set_defaults(func=cmd_init)
    sub.add_parser("stats", help="看各表行数和投递状态分布").set_defaults(func=cmd_stats)

    pr = sub.add_parser("profile", help="母简历与目标画像")
    prsub = pr.add_subparsers(dest="sub", required=True)
    prsub.add_parser("check", help="校验 ID 唯一性、引用完整性").set_defaults(
        func=cmd_profile_check
    )

    co = sub.add_parser("companies", help="目标公司清单")
    cosub = co.add_subparsers(dest="sub", required=True)
    cosub.add_parser("sync", help="companies.yaml -> 数据库").set_defaults(
        func=cmd_companies_sync
    )

    st = sub.add_parser("status", help="投递状态")
    stsub = st.add_subparsers(dest="sub", required=True)
    stsub.add_parser("rebuild", help="按 events 全量重算 status 缓存").set_defaults(
        func=cmd_status_rebuild
    )

    ru = sub.add_parser("run", help="跑 agent loop：模型自己决定调哪些工具")
    ru.add_argument("task", nargs="?", help="要 agent 做的事；用 --schedule 时可省略")
    ru.add_argument("--schedule", help="跑 config/schedules.yaml 里定义的具名任务")
    ru.add_argument("--max-turns", type=int, default=12, help="最多几轮工具往返")
    ru.add_argument("--max-calls", type=int, default=25, help="最多几次 LLM 调用")
    ru.add_argument("--read-only", action="store_true", help="只给只读工具，用于巡检")
    ru.add_argument("--allow-notify", action="store_true",
                    help="本次允许推送到 Discord（外发动作，默认拒绝，不延续到下次）")
    ru.add_argument("--model", help="覆盖模型，默认 claude-sonnet-5")
    ru.set_defaults(func=cmd_run)

    sub.add_parser("tools", help="列出 agent 能调用的工具和权限档").set_defaults(func=cmd_tools)
    sub.add_parser("schedules", help="列出定时任务定义").set_defaults(func=cmd_schedules)

    sp = sub.add_parser("spend", help="按用途和任务拆 LLM 成本")
    sp.add_argument("--days", type=int, default=30)
    sp.set_defaults(func=cmd_spend)

    rs = sub.add_parser("runs", help="看 agent 干过什么（轨迹留痕）")
    rs.add_argument("--limit", type=int, default=20)
    rs.add_argument("--schedule", help="只看某个定时任务")
    rs.add_argument("--show", type=int, metavar="ID", help="展开某次 run 的完整轨迹")
    rs.set_defaults(func=cmd_runs)

    ap = sub.add_parser("applied", help="记录一次【你已手动投完】的投递")
    ap.add_argument("job_id", type=int)
    ap.add_argument("--via", default="ats_direct",
                    choices=["referral", "company_site", "ats_direct", "recruiter", "other"])
    ap.add_argument("--referral", help="内推人姓名（要在 contacts 里存在）")
    ap.add_argument("--resume-version", type=int, help="绑定的简历版本，必须已过审核门")
    ap.add_argument("--notes")
    ap.add_argument("--force", action="store_true", help="突破每日上限")
    ap.set_defaults(func=cmd_applied)

    cf = sub.add_parser("confirm", help="记下某条投递收到了确认邮件")
    cf.add_argument("application_id", type=int)
    cf.set_defaults(func=cmd_confirm)

    sub.add_parser("board", help="投递追踪表 + 下一步建议 + 确认邮件告警").set_defaults(func=cmd_board)

    ex = sub.add_parser("export", help="导出追踪表（TSV 可直接粘进 Google Sheet）")
    ex.add_argument("--out", help="输出路径")
    ex.add_argument("--csv", action="store_true", help="用逗号分隔（默认制表符）")
    ex.add_argument("--sheet", action="store_true", help="直接写 Google Sheet（需先配服务账号）")
    ex.set_defaults(func=cmd_export)

    aw = sub.add_parser("answers", help="给申请表自定义问题起草答案")
    aw.add_argument("job_id", type=int)
    aw.add_argument("--question", action="append", help="一题，可重复")
    aw.add_argument("--questions", help="每行一题的文件")
    aw.set_defaults(func=cmd_answers)

    ml = sub.add_parser("mail", help="邮件：拉取、分类、人工确认队列（只读）")
    mlsub = ml.add_subparsers(dest="sub", required=True)
    msw = mlsub.add_parser("sweep", help="拉取新邮件并处理")
    msw.add_argument("--since-days", type=int, default=14)
    msw.add_argument("--limit", type=int, default=60, help="本次最多分类几封（控制成本）")
    msw.add_argument("--no-fetch", action="store_true", help="不拉新邮件，只处理库里还没处理的")
    msw.add_argument("--push-alerts", action="store_true",
                     help="有面试邀请 / OA / offer 时推到 Discord。确定性推送，不经过模型；给定时任务用")
    msw.set_defaults(func=cmd_mail_sweep)
    mlsub.add_parser("queue", help="人工确认队列").set_defaults(func=cmd_mail_queue)
    msh = mlsub.add_parser("show", help="看一封邮件的全文和链接")
    msh.add_argument("email_id", type=int)
    msh.set_defaults(func=cmd_mail_show)
    mac = mlsub.add_parser("accept", help="确认队列里的一封（面试邀请等只有你能确认）")
    mac.add_argument("email_id", type=int)
    mac.add_argument("--application", type=int, help="没匹配上时手动指定投递 id")
    mac.set_defaults(func=cmd_mail_accept)
    mdi = mlsub.add_parser("dismiss", help="驳回队列里的一封")
    mdi.add_argument("email_id", type=int)
    mdi.add_argument("--note")
    mdi.set_defaults(func=cmd_mail_dismiss)

    pp = sub.add_parser("prep", help="为某条投递生成面试准备材料")
    pp.add_argument("application_id", type=int)
    pp.set_defaults(func=cmd_prep)

    oc = sub.add_parser("openclaw", help="OpenClaw 外壳：生成配置、检查配置有没有被改松")
    ocsub = oc.add_subparsers(dest="sub", required=True)
    occ = ocsub.add_parser("config", help="从 schedules.yaml 生成 OpenClaw 配置片段（不碰 ~/.openclaw）")
    occ.add_argument("--discord-id", help="你的 Discord 用户 id（默认取 .env 的 DISCORD_USER_ID）")
    occ.add_argument("--out", help="写到这个目录（建议 data/openclaw）：配置片段、cron 命令、各 agent 的 AGENTS.md")
    occ.add_argument("--python", help="WSL 里看到的 venv python 路径（默认按项目位置推算）")
    occ.add_argument("--agent", help="WSL 里看到的 agent.exe 路径（默认按项目位置推算）")
    occ.set_defaults(func=cmd_openclaw_config)
    ocv = ocsub.add_parser("verify", help="检查一份 openclaw.json：工具白名单、沙箱、heartbeat、谁能发消息")
    ocv.add_argument("path")
    ocv.set_defaults(func=cmd_openclaw_verify)

    ta = sub.add_parser("tailor", help="为某个岗位定制简历（选材 + 渲染 + 幻觉校验）")
    ta.add_argument("job_id", type=int)
    ta.add_argument("--max-pages", type=int, default=1)
    ta.add_argument("--no-rewrite", action="store_true", help="不按 JD 改写措辞，全部用母简历原文")
    ta.set_defaults(func=cmd_tailor)

    re_ = sub.add_parser("resume", help="简历版本与审核门")
    resub = re_.add_subparsers(dest="sub", required=True)
    rl = resub.add_parser("list", help="列出已生成的版本")
    rl.add_argument("--limit", type=int, default=20)
    rl.set_defaults(func=cmd_resume_list)
    rap = resub.add_parser("approve", help="批准一个版本（审核门；agent 没有这个能力）")
    rap.add_argument("resume_version_id", type=int)
    rap.set_defaults(func=cmd_resume_approve)

    an = sub.add_parser("analyze", help="直接跑第二层 JD 分析（不经过 agent loop）")
    an.add_argument("--limit", type=int, default=25)
    an.set_defaults(func=cmd_analyze)

    fe = sub.add_parser("fetch", help="抓取目标公司的新岗位")
    fe.add_argument("--company", help="只抓这一家")
    fe.add_argument("--dry-run", action="store_true", help="只跑不写库")
    fe.add_argument("--no-detail", action="store_true", help="跳过 JD 全文抓取（只看有哪些岗位）")
    fe.add_argument("--explain", action="store_true", help="显示初筛丢弃原因和样本，用来调过滤条件")
    fe.add_argument("--notify", action="store_true", help="把摘要推到 Discord")
    fe.add_argument("--analyze", type=int, default=0, metavar="N",
                    help="抓完后按 target_profile 的 analyze_first 排队，分析最多 N 个还没分析的岗位")
    fe.add_argument("--push-recommended", action="store_true",
                    help="把这次新判为推荐投递的岗位按推荐顺序推到 Discord（要和 --analyze 一起用）")
    fe.add_argument("--fast", action="store_true", help="取消请求间隔（只在自己调试时用）")
    fe.set_defaults(func=cmd_fetch)

    jo = sub.add_parser("jobs", help="看抓到的岗位")
    josub = jo.add_subparsers(dest="sub", required=True)
    jl = josub.add_parser("list", help="列出岗位")
    jl.add_argument("--company")
    jl.add_argument("--tier", help="只看某一档，如 tier1_ai_engineer")
    jl.add_argument("--limit", type=int, default=40)
    jl.add_argument("--all", action="store_true", help="包括已下架的、以及不再通过初筛的")
    jl.set_defaults(func=cmd_jobs_list)
    js = josub.add_parser("show", help="看单个岗位和 JD 全文")
    js.add_argument("job_id", type=int)
    js.add_argument("--full", action="store_true", help="打印完整 JD")
    js.set_defaults(func=cmd_jobs_show)

    ra = sub.add_parser("resolve-ats", help="从 careers 页反查 ATS 类型和 board_token")
    ra.add_argument("target", help="careers 页 URL，或直接给公司名")
    ra.add_argument("--name", help="公司名（target 是 URL 时用来猜 token）")
    ra.add_argument("--no-verify", action="store_true", help="只提取候选，不打接口验证")
    ra.set_defaults(func=cmd_resolve_ats)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
