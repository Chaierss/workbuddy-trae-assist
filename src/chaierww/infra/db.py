from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 3


def now_iso() -> str:
    """统一用 UTC ISO8601 存时间，展示层再做本地化，避免时区混用。"""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False：FastAPI 的同步端点在线程池里执行，连接必须可跨线程使用。
    # 并发安全由仓储层的串行化锁保证（事务边界不能交错），不能只靠 SQLite 自身。
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # 多个服务共用同一连接时，写锁竞争应当等待而不是立刻报错。
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


_SCHEMA_V1 = """
CREATE TABLE accounts (
    id               TEXT PRIMARY KEY,
    scope            TEXT NOT NULL,
    external_id      TEXT NOT NULL,
    display_name     TEXT NOT NULL,
    note             TEXT NOT NULL DEFAULT '',
    avatar_path      TEXT,
    source           TEXT NOT NULL DEFAULT 'local_import',
    credential_state TEXT NOT NULL DEFAULT 'unverified',
    last_used_at     TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    deleted_at       TEXT,
    UNIQUE (scope, external_id)
);

CREATE INDEX idx_accounts_scope ON accounts (scope, deleted_at);

CREATE TABLE credentials (
    id         TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    sealed     BLOB NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    is_current INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX idx_credentials_account ON credentials (account_id, created_at DESC);

CREATE TABLE snapshots (
    id            TEXT PRIMARY KEY,
    scope         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    reason        TEXT NOT NULL,
    account_id    TEXT,
    directory     TEXT NOT NULL,
    total_bytes   INTEGER NOT NULL DEFAULT 0,
    file_count    INTEGER NOT NULL DEFAULT 0,
    verified      INTEGER NOT NULL DEFAULT 0,
    note          TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_snapshots_scope ON snapshots (scope, created_at DESC);

CREATE TABLE switch_jobs (
    id                TEXT PRIMARY KEY,
    scope             TEXT NOT NULL,
    source_account_id TEXT,
    target_account_id TEXT NOT NULL,
    snapshot_id       TEXT,
    state             TEXT NOT NULL,
    dry_run           INTEGER NOT NULL DEFAULT 0,
    failed_step       TEXT,
    error             TEXT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT
);

CREATE INDEX idx_switch_jobs_scope ON switch_jobs (scope, started_at DESC);
"""


def _migrate_v1(conn: sqlite3.Connection) -> None:
    # executescript 会先提交已开启的事务，因此这里把 BEGIN/COMMIT 写进脚本本身，
    # 保证建表要么全成、要么全不成。
    conn.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA_V1 + "\nCOMMIT;")


#: v2：凭据探测记录（过期时间未知时的盲续期节流）+ 活动执行流水。
#: 两张表都只追加，不参与任何账号状态判定以外的读放大。
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS credential_probes (
    account_id TEXT PRIMARY KEY REFERENCES accounts (id) ON DELETE CASCADE,
    probed_at  TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS activity_runs (
    id           TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    activity_key TEXT NOT NULL,
    day          TEXT NOT NULL,
    attempt      INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL,
    message      TEXT NOT NULL DEFAULT '',
    reward       REAL,
    at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_activity_runs_day ON activity_runs (day, account_id);
CREATE INDEX IF NOT EXISTS idx_activity_runs_account ON activity_runs (account_id, at DESC);
"""


def _migrate_v2(conn: sqlite3.Connection) -> None:
    conn.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA_V2 + "\nCOMMIT;")


#: v3：凭据探测的连续认证失败次数。连续 3 次 401/403 后停止自动续期，
#: 避免对已确认失效的号反复打上游（重新扫码登录后计数清零）。
_SCHEMA_V3 = """
ALTER TABLE credential_probes ADD COLUMN failures INTEGER NOT NULL DEFAULT 0;
"""


def _migrate_v3(conn: sqlite3.Connection) -> None:
    conn.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA_V3 + "\nCOMMIT;")


def migrate(conn: sqlite3.Connection) -> int:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return version

    if version < 1:
        _migrate_v1(conn)
    if version < 2:
        _migrate_v2(conn)
    if version < 3:
        _migrate_v3(conn)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return SCHEMA_VERSION


def open_database(path: Path) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn
