"""会话迁移单元测试：复制会话到目标账号。"""

from __future__ import annotations

import json
import sqlite3

import pytest

import chaierww.domain.session_migration as sm


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """构造一个最小 workbuddy 数据目录，并 monkeypatch 所有路径函数。"""
    monkeypatch.setattr(sm, "workbuddy_data_dir", lambda: tmp_path)
    monkeypatch.setattr(sm, "workbuddy_db_path", lambda: tmp_path / "workbuddy.db")
    monkeypatch.setattr(sm, "projects_dir", lambda: tmp_path / "projects")

    # workbuddy.db 的 sessions 表
    db = tmp_path / "workbuddy.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT, cwd TEXT, user_id TEXT, title TEXT, custom_title TEXT,
            created_at INTEGER, updated_at INTEGER, deleted_at INTEGER,
            is_playground INTEGER
        )
        """
    )
    conn.commit()
    conn.close()

    # edge-sync 库
    edge = tmp_path / "edge-sync-mapping-v4.db"
    econn = sqlite3.connect(str(edge))
    econn.execute(
        """
        CREATE TABLE edge_sync_mapping (
            session_id TEXT, conversation_id TEXT, msg_channel TEXT, created_at INTEGER
        )
        """
    )
    econn.commit()
    econn.close()

    return tmp_path


def _seed_session(data_dir, cid, uid, title, cwd="/workspace"):
    conn = sqlite3.connect(str(data_dir / "workbuddy.db"))
    conn.execute(
        "INSERT INTO sessions (id, cwd, user_id, title, created_at, updated_at, deleted_at) "
        "VALUES (?, ?, ?, ?, 1000, 1000, NULL)",
        (cid, cwd, uid, title),
    )
    conn.commit()
    conn.close()


def test_copy_session_copies_row_and_jsonl(data_dir):
    src_uid = "src-uid"
    dst_uid = "dst-uid"
    cid = "00000000-0000-0000-0000-000000000001"

    _seed_session(data_dir, cid, src_uid, "测试会话")
    # jsonl 正文
    ws = data_dir / "projects" / "ws-1"
    ws.mkdir(parents=True)
    (ws / f"{cid}.jsonl").write_text(
        json.dumps({"sessionId": cid, "type": "user", "message": "hi"}) + "\n",
        encoding="utf-8",
    )

    report = sm.copy_sessions_for_switch(src_uid, dst_uid, [cid])

    assert report.errors == []
    assert len(report.copied) == 1
    new_cid = report.copied[0]["newId"]
    assert new_cid != cid

    # jsonl 已复制且旧 cid 被替换
    new_jsonl = ws / f"{new_cid}.jsonl"
    assert new_jsonl.is_file()
    content = new_jsonl.read_text(encoding="utf-8")
    assert cid not in content
    assert new_cid in content

    # sessions 表新增行，user_id 为目标账号
    conn = sqlite3.connect(str(data_dir / "workbuddy.db"))
    row = conn.execute(
        "SELECT id, user_id, title FROM sessions WHERE id = ?", (new_cid,)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[1] == dst_uid
    assert row[2] == "测试会话"

    # edge 映射已注册
    econn = sqlite3.connect(str(data_dir / "edge-sync-mapping-v4.db"))
    edge_row = econn.execute(
        "SELECT msg_channel FROM edge_sync_mapping WHERE session_id = ?", (new_cid,)
    ).fetchone()
    econn.close()
    assert edge_row is not None
    assert edge_row[0] == f"convmsg:{dst_uid}"


def test_copy_session_deduplicates_via_ledger(data_dir):
    src_uid = "src-uid"
    dst_uid = "dst-uid"
    cid = "00000000-0000-0000-0000-000000000002"
    _seed_session(data_dir, cid, src_uid, "会话")

    # 第一次复制
    report1 = sm.copy_sessions_for_switch(src_uid, dst_uid, [cid])
    assert len(report1.copied) == 1

    # 第二次复制同一会话 → 账本命中，跳过
    report2 = sm.copy_sessions_for_switch(src_uid, dst_uid, [cid])
    assert len(report2.copied) == 0
    assert len(report2.skipped) == 1
    assert report2.skipped[0]["reason"] == "已复制过"


def test_copy_session_rejects_claw(data_dir):
    src_uid = "src-uid"
    dst_uid = "dst-uid"
    cid = "00000000-0000-0000-0000-000000000003"
    _seed_session(data_dir, cid, src_uid, "Claw 会话", cwd="/some/Claw")

    report = sm.copy_sessions_for_switch(src_uid, dst_uid, [cid])
    assert len(report.copied) == 0
    assert any("Claw" in e["error"] for e in report.errors)


def test_copy_session_rejects_same_uid(data_dir):
    with pytest.raises(sm.SessionMigrationError, match="相同"):
        sm.copy_sessions_for_switch("uid", "uid", [])


def test_copy_all_when_no_ids_given(data_dir):
    src_uid = "src-uid"
    dst_uid = "dst-uid"
    _seed_session(data_dir, "11111111-1111-1111-1111-111111111111", src_uid, "A")
    _seed_session(data_dir, "22222222-2222-2222-2222-222222222222", src_uid, "B")

    report = sm.copy_sessions_for_switch(src_uid, dst_uid, [])
    assert len(report.copied) == 2


def test_copy_session_round_trip_does_not_duplicate(data_dir):
    """A→B→A→B 往返不应让副本翻倍（血统去重）。"""
    a, b = "uid-a", "uid-b"
    cid = "00000000-0000-0000-0000-000000000010"
    _seed_session(data_dir, cid, a, "往返会话")
    ws = data_dir / "projects" / "ws-rt"
    ws.mkdir(parents=True)
    (ws / f"{cid}.jsonl").write_text("{}", encoding="utf-8")

    r1 = sm.copy_sessions_for_switch(a, b, [])
    assert len(r1.copied) == 1
    x = r1.copied[0]["newId"]
    assert x != cid  # 副本用新 id，原会话留在 A

    # B→A：X 的根就是 A 自己的会话 → 跳过
    r2 = sm.copy_sessions_for_switch(b, a, [])
    assert len(r2.copied) == 0
    assert any("本就是目标账号" in s["reason"] for s in r2.skipped)

    # A→B 再来一次：三元组键命中或血统命中，均跳过、不新增副本
    r3 = sm.copy_sessions_for_switch(a, b, [])
    assert len(r3.copied) == 0
    assert r3.skipped

    conn = sqlite3.connect(str(data_dir / "workbuddy.db"))
    total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    conn.close()
    assert total == 2  # cid(归 A) + x(归 B)，往返未翻倍


def test_received_lineage_skips_second_copy_of_same_root(data_dir):
    """目标已收到过某血统时，源头另一条同血统会话也应跳过（received 命中）。"""
    a, b = "uid-a", "uid-b"
    root = "00000000-0000-0000-0000-000000000050"
    twin = "00000000-0000-0000-0000-000000000051"
    _seed_session(data_dir, root, a, "原")
    _seed_session(data_dir, twin, a, "同血统副本")
    sm._write_ledger(sm.LedgerDoc(lineage={twin: (a, root)}))

    r1 = sm.copy_sessions_for_switch(a, b, [root])
    assert len(r1.copied) == 1

    r2 = sm.copy_sessions_for_switch(a, b, [twin])
    assert len(r2.copied) == 0
    assert any("血统" in s["reason"] for s in r2.skipped)


def test_copy_session_copies_companions(data_dir):
    src, dst = "uid-s", "uid-d"
    cid = "00000000-0000-0000-0000-000000000020"
    _seed_session(data_dir, cid, src, "带伴生")
    ws = data_dir / "projects" / "ws-c"
    ws.mkdir(parents=True)
    (ws / f"{cid}.jsonl").write_text("{}", encoding="utf-8")
    (ws / f"{cid}.meta.json").write_text(f'{{"sessionId":"{cid}"}}', encoding="utf-8")
    (ws / f"{cid}.file-rollback.ndjson").write_text('{"op":"x"}\n', encoding="utf-8")

    report = sm.copy_sessions_for_switch(src, dst, [cid])
    new = report.copied[0]["newId"]
    meta = ws / f"{new}.meta.json"
    assert meta.is_file()
    assert cid not in meta.read_text(encoding="utf-8")
    rollback = ws / f"{new}.file-rollback.ndjson"
    assert rollback.read_text(encoding="utf-8") == '{"op":"x"}\n'  # 仅改名不改内容
    assert report.copied[0]["companions"] == {"meta": True, "rollback": True}


def test_read_ledger_backward_compatible_with_flat_format(data_dir):
    (data_dir / sm.COPY_LEDGER_FILE).write_text(
        json.dumps({"a\x1fb\x1fc": "d"}), encoding="utf-8"
    )
    doc = sm._read_ledger()
    assert doc.entries == {"a\x1fb\x1fc": "d"}
    assert doc.lineage == {}


def test_ledger_versioned_roundtrip(data_dir):
    doc = sm.LedgerDoc(entries={"a\x1fb\x1fc": "d"}, lineage={"d": ("a", "c")})
    sm._write_ledger(doc)
    back = sm._read_ledger()
    assert back.entries == doc.entries
    assert back.lineage == doc.lineage


def test_find_and_cleanup_duplicates(data_dir):
    uid = "uid-dup"
    root = "00000000-0000-0000-0000-000000000030"
    dup = "00000000-0000-0000-0000-000000000031"
    _seed_session(data_dir, root, uid, "原始")
    conn = sqlite3.connect(str(data_dir / "workbuddy.db"))
    conn.execute(
        "INSERT INTO sessions (id, cwd, user_id, title, created_at, updated_at, deleted_at) "
        "VALUES (?, ?, ?, ?, 2000, 2000, NULL)",
        (dup, "/workspace", uid, "原始"),
    )
    conn.commit()
    conn.close()
    # 台账血统：dup 是 root 的副本，根归 uid
    sm._write_ledger(sm.LedgerDoc(lineage={dup: (uid, root)}))

    groups = sm.find_duplicate_copies(uid)
    assert len(groups) == 1
    assert groups[0]["kept"]["id"] == root
    assert [d["id"] for d in groups[0]["dups"]] == [dup]

    result = sm.cleanup_duplicates(uid, [dup])
    assert result["errors"] == []
    assert result["removed"][0]["softDeleted"] is True
    remaining = [s["id"] for s in sm.list_sessions_for_user(uid)]
    assert remaining == [root]
