"""迁移前预检（dry-run）单元测试：只读、不产生任何写入。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import chaierww.domain.migration as mig
import chaierww.domain.session_migration as sm
from chaierww.domain.migration import MigrationSelection, preview_account_data


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """最小 workbuddy 数据目录，含记忆 / 连接器 / 个人档案 / 会话库。"""
    monkeypatch.setattr(sm, "workbuddy_data_dir", lambda: tmp_path)
    monkeypatch.setattr(sm, "workbuddy_db_path", lambda: tmp_path / "workbuddy.db")
    monkeypatch.setattr(sm, "projects_dir", lambda: tmp_path / "projects")
    monkeypatch.setattr(mig, "workbuddy_data_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "chaierww.domain.profile_migration.workbuddy_data_dir", lambda: tmp_path
    )

    memory = tmp_path / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "src_memory.md").write_text("## 偏好\n- 表格\n- 新增行\n", encoding="utf-8")
    (memory / "dst_memory.md").write_text("## 偏好\n- 表格\n", encoding="utf-8")

    conn_dir = tmp_path / "connectors" / "src"
    conn_dir.mkdir(parents=True, exist_ok=True)
    (conn_dir / "mcp.json").write_text('{"mcpServers":{"a":{}}}', encoding="utf-8")

    (tmp_path / "user-src-personal").mkdir(parents=True, exist_ok=True)
    (tmp_path / "user-src-personal" / "IDENTITY.md").write_text(
        "## 身份\n- 甲\n- 新增\n", encoding="utf-8"
    )
    (tmp_path / "user-dst-personal").mkdir(parents=True, exist_ok=True)
    (tmp_path / "user-dst-personal" / "IDENTITY.md").write_text("## 身份\n- 甲\n", encoding="utf-8")

    db = tmp_path / "workbuddy.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE sessions (id TEXT, cwd TEXT, user_id TEXT, title TEXT, "
        "custom_title TEXT, created_at INTEGER, updated_at INTEGER, deleted_at INTEGER, "
        "is_playground INTEGER)"
    )
    conn.execute(
        "INSERT INTO sessions (id, cwd, user_id, title, created_at, updated_at, deleted_at) "
        "VALUES ('cid-1', '/ws', 'src', '会话一', 1000, 1000, NULL)"
    )
    conn.commit()
    conn.close()
    return tmp_path


def _snapshot(root: Path) -> dict[str, tuple[int, float]]:
    """文件树快照：相对路径 → (内容哈希, mtime)，用于断言「零写入」。

    内容哈希用 sha256，避免只比 mtime 漏判；同时排除预检自己不该产生的文件。
    """
    import hashlib

    out: dict[str, tuple[int, float]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            out[str(path.relative_to(root))] = (digest, path.stat().st_mtime)
    return out


def test_preview_reports_all_blocks(data_dir):
    result = preview_account_data("src", "dst")

    assert result["valid"] is True
    assert result["memory"]["wouldAppend"] == 1
    assert result["connectors"]["addedKeys"] >= 1
    assert result["sessions"]["copyCount"] == 1
    assert result["profile"]["mergeCount"] >= 1
    assert result["workspaces"]["mergeCount"] == 0  # 无 workspace-state.json
    assert result["duplicates"] == []


def test_preview_writes_nothing(data_dir):
    before = _snapshot(data_dir)
    preview_account_data("src", "dst")
    after = _snapshot(data_dir)

    assert before == after, "预检不得写入任何文件"
    # 预检也不该落备份目录 / 台账
    assert not (data_dir / "backups").exists()
    assert not (data_dir / "buddy-switch-copy-ledger.json").exists()


def test_preview_honours_selection(data_dir):
    result = preview_account_data(
        "src",
        "dst",
        selection=MigrationSelection(
            memory=False,
            connectors=False,
            sessions=True,
            profile=False,
            workspaces=False,
        ),
    )
    assert result["memory"] is None
    assert result["connectors"] is None
    assert result["profile"] is None
    assert result["workspaces"] is None
    assert result["sessions"]["copyCount"] == 1


def test_preview_same_uid_is_invalid(data_dir):
    result = preview_account_data("src", "src")
    assert result["valid"] is False
    assert "相同" in result["reason"]


def test_preview_sessions_would_skip_after_copy(data_dir):
    """真实复制一次后，预检应显示该会话会被跳过（血缘 / 台账命中）。"""
    assert preview_account_data("src", "dst")["sessions"]["copyCount"] == 1
    sm.copy_sessions_for_switch("src", "dst", ["cid-1"])
    after = preview_account_data("src", "dst")
    assert after["sessions"]["copyCount"] == 0
    assert after["sessions"]["skipCount"] == 1


def test_preview_reports_existing_duplicates(data_dir):
    """目标账号里同一血统的多条会话应出现在 duplicates 里（只读识别）。"""
    conn = sqlite3.connect(str(data_dir / "workbuddy.db"))
    for cid in ("dup-a", "dup-b"):
        conn.execute(
            "INSERT INTO sessions (id, cwd, user_id, title, created_at, updated_at, deleted_at) "
            "VALUES (?, '/ws', 'dst', '重复', 1000, 1000, NULL)",
            (cid,),
        )
    conn.commit()
    conn.close()
    sm._write_ledger(
        sm.LedgerDoc(
            lineage={
                "dup-a": ("rootuid", "rootid"),
                "dup-b": ("rootuid", "rootid"),
            }
        )
    )

    result = preview_account_data("src", "dst")
    assert result["duplicates"], "应识别出目标账号内同血统的重复会话"
    group = result["duplicates"][0]
    assert group["kept"]["id"] == "dup-a"
    assert [d["id"] for d in group["dups"]] == ["dup-b"]


def test_preview_memory_json_blocks_match_apply(data_dir):
    """预检结论必须与实际执行一致（记忆追加行数）。"""
    preview = preview_account_data("src", "dst")
    report = mig.migrate_account_data(
        "src",
        "dst",
        selection=MigrationSelection(
            memory=True, connectors=False, sessions=False, profile=False, workspaces=False
        ),
    )
    assert preview["memory"]["wouldAppend"] == report.memory["appended"]


def test_preview_returns_json_serialisable(data_dir):
    result = preview_account_data("src", "dst")
    json.dumps(result)  # 不应抛 TypeError
