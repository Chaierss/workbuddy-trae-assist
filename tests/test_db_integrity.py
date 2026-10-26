from __future__ import annotations

import sqlite3

import pytest

from chaierww.infra.db import (
    SCHEMA_VERSION,
    DatabaseIntegrityError,
    _migrate_v1,
    _migrate_v2,
    _migrate_v3,
    open_database,
)
from chaierww.infra.startup import (
    clear_startup_error,
    read_startup_error,
    scrub,
    write_startup_error,
)


def test_old_database_is_backed_up_before_migrate(tmp_path):
    db = tmp_path / "chaierww.db"
    conn = sqlite3.connect(db)
    # 真实 v1 库一定带 accounts 表；只造 demo 表会让后续 ALTER TABLE 无从下手。
    _migrate_v1(conn)
    conn.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO demo (name) VALUES ('kept')")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    backup_dir = tmp_path / "backups" / "pre-migrate"
    opened = open_database(db, backup_dir=backup_dir)
    try:
        assert opened.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        backups = list(backup_dir.glob("v1-*.db"))
        assert len(backups) == 1
        check = sqlite3.connect(backups[0])
        try:
            # 迁移前的库必须还能打开且数据还在。
            assert check.execute("SELECT name FROM demo").fetchone()[0] == "kept"
        finally:
            check.close()
    finally:
        opened.close()


def test_garbage_database_is_rejected(tmp_path):
    db = tmp_path / "chaierww.db"
    db.write_bytes(b"this is not a sqlite database" * 4)
    with pytest.raises(DatabaseIntegrityError) as excinfo:
        open_database(db)
    assert str(db) in str(excinfo.value)


def test_startup_error_is_scrubbed_and_removable(tmp_path):
    error = RuntimeError("failed with Bearer abcdefghijklmnop and sk-supersecret1234")
    path = write_startup_error(tmp_path, error)
    data = read_startup_error(tmp_path)
    assert data is not None
    assert data["type"] == "RuntimeError"
    assert "abcdefghijklmnop" not in data["message"]
    assert "sk-supersecret1234" not in data["message"]
    assert path.name == "startup-error.json"

    clear_startup_error(tmp_path)
    assert read_startup_error(tmp_path) is None


def test_scrub_truncates_long_messages():
    assert len(scrub("x" * 900)) <= 500


def test_v4_upgrade_adds_sync_objects_and_keeps_existing_rows(tmp_path):
    """v3 → v4：新增会话同步表与登录态代际，且老账号行必须原地保留。"""
    db = tmp_path / "v3.db"
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA foreign_keys = ON")
    _migrate_v1(raw)
    _migrate_v2(raw)
    _migrate_v3(raw)
    raw.execute(
        "INSERT INTO accounts (id, scope, external_id, display_name, credential_state,"
        " created_at, updated_at) VALUES ('a-1', 'workbuddy-cn', 'u-1', '老账号',"
        " 'usable', '2026-01-01', '2026-01-01')"
    )
    raw.execute("PRAGMA user_version = 3")
    raw.commit()
    raw.close()

    conn = open_database(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"sync_state", "sync_items", "sync_jobs"} <= tables

        columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
        assert "credential_generation" in columns

        row = conn.execute(
            "SELECT display_name, credential_generation FROM accounts WHERE id = 'a-1'"
        ).fetchone()
        assert row[0] == "老账号"
        assert row[1] == 0  # 老账号默认从第 0 代开始
    finally:
        conn.close()


def test_credential_generation_bumps_on_credential_change(tmp_path):
    from chaierww.domain.accounts.repository import AccountRepository
    from chaierww.infra.secrets import InMemoryBox

    conn = open_database(tmp_path / "gen.db")
    try:
        repo = AccountRepository(conn, InMemoryBox())
        account = repo.upsert(
            scope="workbuddy-cn",
            external_id="u-1",
            display_name="甲",
            credential={"access_token": "t1"},
        ).account
        assert repo.credential_generation(account.id) == 0

        bumped = repo.bump_credential_generation(account.id)
        assert bumped == 1

        # 重新导入同一账号（凭据换了）也要 +1：换 token 就是换了一代登录态。
        repo.upsert(
            scope="workbuddy-cn",
            external_id="u-1",
            display_name="甲",
            credential={"access_token": "t2"},
        )
        assert repo.credential_generation(account.id) == 2
    finally:
        conn.close()
