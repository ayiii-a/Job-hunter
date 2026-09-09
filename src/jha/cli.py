"""命令行入口。

Phase 0 只有骨架相关的命令：建库、校验母简历、同步公司清单、反查 ATS、重算状态。
抓取 / 分析 / 投递等命令随后面的 Phase 加进来。
"""

from __future__ import annotations

import argparse
import json
import sys
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
    conn.close()

    if failures:
        print()
        _err(f"{len(failures)} 家最近一次抓取是失败的：")
        for f in failures:
            print(f"      {f['name']}（{f['source']}）：{(f['error'] or '')[:100]}")

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
            print("\n（加 --notify 可以推到 Telegram）")
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
    sql += "WHERE 1=1 " if args.all else "WHERE j.is_active = 1 "
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
    from .agent import AgentClient, Budget, MissingAPIKey, Permission, run as agent_run

    conn = db.connect()
    db.init_db(conn)

    allow = {Permission.READ} if args.read_only else None
    # GATED 工具默认一律拒绝。--allow-notify 是【单次】显式放行，
    # 不会延续到下一次 run —— 外发动作不该因为你上次同意过就自动发生。
    approve = (lambda name, a: name == "send_notification") if args.allow_notify else None

    print(f"任务：{args.task}\n")
    try:
        result = agent_run(
            args.task,
            conn,
            client=AgentClient(model=args.model),
            budget=Budget(max_llm_calls=args.max_calls),
            max_turns=args.max_turns,
            allow=allow,
            approve=approve,
            on_step=lambda s: print(s.render()),
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
    from .agent import spend_summary

    conn = db.connect()
    db.init_db(conn)
    rows = spend_summary(conn, days=args.days)
    conn.close()
    if not rows:
        _warn(f"最近 {args.days} 天没有 LLM 调用记录")
        return 0
    print(f"最近 {args.days} 天\n")
    print(f"  {'用途':<20} {'次数':>6} {'输入':>10} {'输出':>10} {'成本':>10}")
    for r in rows:
        print(
            f"  {r['purpose']:<20} {r['calls']:>6} {r['inp'] or 0:>10} "
            f"{r['out'] or 0:>10} {'$' + str(r['cost'] or 0):>10}"
        )
    print(f"\n  合计 ${round(sum(r['cost'] or 0 for r in rows), 4)}")
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
    ru.add_argument("task", help="要 agent 做的事，用自然语言描述")
    ru.add_argument("--max-turns", type=int, default=12, help="最多几轮工具往返")
    ru.add_argument("--max-calls", type=int, default=25, help="最多几次 LLM 调用")
    ru.add_argument("--read-only", action="store_true", help="只给只读工具，用于巡检")
    ru.add_argument("--allow-notify", action="store_true",
                    help="本次允许推送到 Telegram（外发动作，默认拒绝，不延续到下次）")
    ru.add_argument("--model", help="覆盖模型，默认 claude-sonnet-5")
    ru.set_defaults(func=cmd_run)

    sub.add_parser("tools", help="列出 agent 能调用的工具和权限档").set_defaults(func=cmd_tools)

    sp = sub.add_parser("spend", help="按用途拆 LLM 成本")
    sp.add_argument("--days", type=int, default=30)
    sp.set_defaults(func=cmd_spend)

    fe = sub.add_parser("fetch", help="抓取目标公司的新岗位")
    fe.add_argument("--company", help="只抓这一家")
    fe.add_argument("--dry-run", action="store_true", help="只跑不写库")
    fe.add_argument("--no-detail", action="store_true", help="跳过 JD 全文抓取（只看有哪些岗位）")
    fe.add_argument("--explain", action="store_true", help="显示初筛丢弃原因和样本，用来调过滤条件")
    fe.add_argument("--notify", action="store_true", help="把摘要推到 Telegram")
    fe.add_argument("--fast", action="store_true", help="取消请求间隔（只在自己调试时用）")
    fe.set_defaults(func=cmd_fetch)

    jo = sub.add_parser("jobs", help="看抓到的岗位")
    josub = jo.add_subparsers(dest="sub", required=True)
    jl = josub.add_parser("list", help="列出岗位")
    jl.add_argument("--company")
    jl.add_argument("--tier", help="只看某一档，如 tier1_ai_engineer")
    jl.add_argument("--limit", type=int, default=40)
    jl.add_argument("--all", action="store_true", help="包括已下架的")
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
