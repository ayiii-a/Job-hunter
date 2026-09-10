"""SQLite 连接、建库、以及 status 缓存的重算。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from . import config
from .status import Event, derive_status, unknown_event_types

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """打开连接。外键约束默认是关的，必须显式打开。"""
    target = Path(path) if path is not None else config.db_path()
    if str(target) != ":memory:":
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


SCHEMA_VERSION = 4

#: 后加的列。schema.sql 里已经有它们（新库直接建好），这份清单是给**已存在的库**
#: 升级用的——`CREATE TABLE IF NOT EXISTS` 不会给旧表补列，跑起来只会在
#: 第一次 INSERT 时炸 "no such column"。按列是否存在来决定加不加，天然幂等。
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("jobs", "miss_count", "INTEGER NOT NULL DEFAULT 0"),
    ("jobs", "screen_tier", "TEXT"),
    ("emails", "message_id", "TEXT"),
    ("emails", "from_domain", "TEXT"),
    ("emails", "role_hint", "TEXT"),
    ("emails", "summary", "TEXT"),
    ("emails", "dates_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("emails", "links_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("emails", "action_required", "INTEGER"),
    ("emails", "policy", "TEXT"),
    ("emails", "review_status", "TEXT"),
    ("emails", "reason", "TEXT"),
    ("emails", "event_id", "INTEGER"),
    ("emails", "classifier_version", "TEXT"),
)


#: 建在「后加的列」上的索引。不能写进 schema.sql——对已存在的库，
#: schema.sql 先执行，那时这些列还没补上，建索引会直接报 no such column。
_ADDED_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_emails_policy ON emails(policy)",
    "CREATE INDEX IF NOT EXISTS idx_emails_review ON emails(review_status)",
    "CREATE INDEX IF NOT EXISTS idx_emails_msgid  ON emails(message_id)",
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def init_db(conn: sqlite3.Connection) -> list[str]:
    """建表并补列。schema.sql 全程 IF NOT EXISTS，重复跑是安全的。

    返回这次实际补上的列，方便 CLI 告诉你库被升级了。
    """
    conn.executescript(config.read_text(SCHEMA_PATH))

    added: list[str] = []
    existing_tables = set(table_names(conn))
    for table, column, ddl in _ADDED_COLUMNS:
        if table not in existing_tables:
            continue
        if column not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            added.append(f"{table}.{column}")

    for ddl in _ADDED_INDEXES:
        conn.execute(ddl)

    conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()
    return added


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


# ---------------------------------------------------------------------------
# JSON 列的读写帮手（SQLite 没有数组类型，一律存 JSON TEXT）
# ---------------------------------------------------------------------------

def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def load_json(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return default if default is not None else []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default if default is not None else []


# ---------------------------------------------------------------------------
# events -> status 缓存重算
# ---------------------------------------------------------------------------

def _parse_dt(raw: str) -> datetime:
    """容忍几种常见写法：ISO、带 Z、以及 SQLite 的 'YYYY-MM-DD HH:MM:SS'。"""
    text = raw.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")


def load_events(conn: sqlite3.Connection, application_id: int) -> list[Event]:
    rows = conn.execute(
        "SELECT id, type, occurred_at, source, payload_json FROM events "
        "WHERE application_id = ? ORDER BY occurred_at, id",
        (application_id,),
    ).fetchall()
    return [
        Event(
            id=r["id"],
            type=r["type"],
            occurred_at=_parse_dt(r["occurred_at"]),
            source=r["source"],
            payload=load_json(r["payload_json"], {}),
        )
        for r in rows
    ]


def append_event(
    conn: sqlite3.Connection,
    application_id: int,
    type: str,
    *,
    occurred_at: datetime | None = None,
    source: str = "agent",
    raw_ref: str | None = None,
    payload: dict[str, Any] | None = None,
    rebuild: bool = True,
    now: datetime | None = None,
) -> int:
    """追加一条事件，并（默认）立刻重算这条 application 的 status 缓存。

    events 表上有触发器禁止 UPDATE/DELETE，所以这是唯一改变状态的通道。

    now 是重算 status 时的参照时刻（只影响 ghosted 判定），默认取当前时间。
    补录历史投递时按批次传一个固定的 now，结果才可复现——这跟 derive_status
    把时钟交给调用方是同一个理由。
    """
    ts = (occurred_at or datetime.now()).isoformat(sep=" ", timespec="seconds")
    cur = conn.execute(
        "INSERT INTO events (application_id, type, occurred_at, source, raw_ref, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (application_id, type, ts, source, raw_ref, dump_json(payload or {})),
    )
    if rebuild:
        rebuild_status(conn, application_id, now=now)
    conn.commit()
    return int(cur.lastrowid)


def rebuild_status(
    conn: sqlite3.Connection,
    application_id: int,
    *,
    now: datetime | None = None,
) -> str | None:
    """按 events 重算一条 application 的 status 缓存。"""
    events = load_events(conn, application_id)
    status = derive_status(
        events,
        now=now or datetime.now(),
        ghost_after_days=config.ghost_after_days(),
    )
    conn.execute("UPDATE applications SET status = ? WHERE id = ?", (status, application_id))
    return status


def rebuild_all_statuses(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> dict[str, Any]:
    """全量重算。误判之后的兜底手段：不改历史，重放事件。"""
    now = now or datetime.now()
    ids = [r["id"] for r in conn.execute("SELECT id FROM applications ORDER BY id")]
    changed: list[tuple[int, str | None, str | None]] = []
    orphans: list[int] = []
    unknown: set[str] = set()

    for app_id in ids:
        before = conn.execute(
            "SELECT status FROM applications WHERE id = ?", (app_id,)
        ).fetchone()["status"]
        events = load_events(conn, app_id)
        unknown |= unknown_event_types(events)
        if not events:
            orphans.append(app_id)
        after = rebuild_status(conn, app_id, now=now)
        if before != after:
            changed.append((app_id, before, after))

    conn.commit()
    return {
        "total": len(ids),
        "changed": changed,
        "orphans": orphans,          # 有 application 却没有任何事件 = 数据错误
        "unknown_event_types": sorted(unknown),
    }
