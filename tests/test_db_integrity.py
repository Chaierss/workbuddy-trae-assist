from __future__ import annotations

import sqlite3

import pytest

from chaierww.infra.db import (
    SCHEMA_VERSION,
    DatabaseIntegrityError,
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
