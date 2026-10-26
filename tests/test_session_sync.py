"""多账号会话同步：采集 / 落地 / 稳定性 / 冲突 / 能力边界。

这里用真实的 SQLite（账号库 + workbuddy 会话库）跑，只把「路径」与「备份」
换成临时目录 —— 同步逻辑最容易出错的地方正是路径与事务，不该被 mock 掉。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import chaierww.domain.session_migration as sm
import chaierww.domain.sessions as sessions_mod
import chaierww.domain.sync.engine as engine_mod
from chaierww.domain.accounts.repository import AccountRepository
from chaierww.domain.sync import SyncEngine
from chaierww.domain.sync.canonical import canonicalize, localize, stable_key
from chaierww.domain.sync.repository import SyncRepository
from chaierww.domain.sync.store import BlobStore
from chaierww.infra.db import open_database
from chaierww.infra.secrets import InMemoryBox

SCOPE = "workbuddy-cn"


class FakeAdapter:
    def __init__(self, running: bool = False) -> None:
        self.running = running

    def is_running(self) -> bool:
        return self.running


@pytest.fixture
def wb(tmp_path, monkeypatch):
    """最小 workbuddy 数据目录：sessions 表 + edge-sync 映射表 + projects/。"""
    data = tmp_path / "workbuddy"
    data.mkdir()
    projects = data / "projects"
    projects.mkdir()
    db = data / "workbuddy.db"

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

    edge = data / "edge-sync-mapping-v4.db"
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

    for module in (sessions_mod, sm):
        monkeypatch.setattr(module, "workbuddy_data_dir", lambda: data)
        monkeypatch.setattr(module, "workbuddy_db_path", lambda: db)
        monkeypatch.setattr(module, "projects_dir", lambda: projects)
    monkeypatch.setattr(engine_mod, "workbuddy_db_path", lambda: db)
    monkeypatch.setattr(engine_mod, "projects_dir", lambda: projects)
    monkeypatch.setattr(engine_mod, "backup_session_db", lambda: str(tmp_path / "bak.db"))

    return data


@pytest.fixture
def env(tmp_path, wb):
    conn = open_database(tmp_path / "pool.db")
    repo = AccountRepository(conn, InMemoryBox())
    adapter = FakeAdapter()
    engine = SyncEngine(
        repo,
        SyncRepository(conn, repo.lock),
        tmp_path,
        {SCOPE: adapter, "trae-cn": adapter},
    )

    def make(external_id: str, name: str):
        result = repo.upsert(
            scope=SCOPE,
            external_id=external_id,
            display_name=name,
            credential={"access_token": f"tok-{external_id}", "refresh_token": "r"},
        )
        return result.account

    yield {
        "conn": conn,
        "repo": repo,
        "engine": engine,
        "adapter": adapter,
        "make": make,
        "projects": wb / "projects",
        "db": wb / "workbuddy.db",
    }
    conn.close()


def _seed_session(db: Path, cid: str, uid: str, title: str, cwd: str = "/ws") -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO sessions (id, cwd, user_id, title, created_at, updated_at, deleted_at) "
        "VALUES (?, ?, ?, ?, 1000, 1000, NULL)",
        (cid, cwd, uid, title),
    )
    conn.commit()
    conn.close()


def _write_jsonl(projects: Path, workspace: str, cid: str, lines: list[dict]) -> Path:
    folder = projects / workspace
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{cid}.jsonl"
    path.write_text(
        "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines),
        encoding="utf-8",
    )
    return path


def _rows(db: Path, uid: str) -> list[tuple]:
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT id, user_id, title, deleted_at FROM sessions WHERE user_id = ?", (uid,)
    ).fetchall()
    conn.close()
    return rows


# ---------- 纯函数 ----------


def test_canonical_roundtrip_and_stable_key():
    cid = "00000000-0000-0000-0000-000000000001"
    key = stable_key(SCOPE, "uid-a", cid)
    text = json.dumps({"sessionId": cid, "text": "你好"}) + "\n"
    canonical = canonicalize(text, cid, key)
    assert cid not in canonical
    assert localize(canonical, key, "new-cid") == text.replace(cid, "new-cid")
    # 同一血统在任何账号下都得到同一个键。
    assert stable_key(SCOPE, "uid-a", cid) == stable_key(SCOPE, "uid-a", cid)
    assert stable_key("trae-cn", "uid-a", cid) != key


def test_blob_store_dedupes_and_verifies(tmp_path):
    store = BlobStore(tmp_path)
    sha1, size1 = store.put("hello")
    sha2, size2 = store.put("hello")
    assert (sha1, size1) == (sha2, size2)
    assert store.get(sha1) == "hello"
    # 篡改后 get 必须拒绝返回内容，而不是返回可疑文本。
    path = store._path(sha1)
    path.write_text("tampered", encoding="utf-8")
    assert store.get(sha1) is None


# ---------- 采集 / 落地 ----------


def test_capture_then_apply_creates_conversation(env):
    a = env["make"]("uid-a", "账号甲")
    b = env["make"]("uid-b", "账号乙")
    cid = "11111111-1111-1111-1111-111111111111"
    _seed_session(env["db"], cid, "uid-a", "登录态回写")
    _write_jsonl(
        env["projects"],
        "ws-1",
        cid,
        [{"sessionId": cid, "type": "user", "message": "hi"}],
    )

    captured = env["engine"].capture(SCOPE, a.id)
    assert captured.state == "ok"
    assert captured.counts["captured"] == 1

    # 目标账号此刻已经有「待应用」记录（采集时已分发）。
    pending = env["engine"].items(SCOPE, b.id)
    assert len(pending) == 1 and pending[0]["pending"] is True

    applied = env["engine"].apply(SCOPE, b.id)
    assert applied.state == "ok"
    assert applied.counts["created"] == 1

    rows = _rows(env["db"], "uid-b")
    assert len(rows) == 1
    new_cid = rows[0][0]
    assert new_cid != cid
    body = (env["projects"] / "ws-1" / f"{new_cid}.jsonl").read_text(encoding="utf-8")
    assert cid not in body and new_cid in body

    # 幂等：再应用一次不新增。
    again = env["engine"].apply(SCOPE, b.id)
    assert again.counts["created"] == 0
    assert len(_rows(env["db"], "uid-b")) == 1


def test_apply_blocked_when_client_running(env):
    a = env["make"]("uid-a", "账号甲")
    b = env["make"]("uid-b", "账号乙")
    cid = "22222222-2222-2222-2222-222222222222"
    _seed_session(env["db"], cid, "uid-a", "阻塞测试")
    _write_jsonl(env["projects"], "ws-1", cid, [{"sessionId": cid, "n": 1}])

    env["engine"].capture(SCOPE, a.id)
    env["adapter"].running = True
    report = env["engine"].apply(SCOPE, b.id)
    assert report.state == "blocked"
    assert _rows(env["db"], "uid-b") == []


def test_apply_dry_run_plans_without_writing(env):
    a = env["make"]("uid-a", "账号甲")
    b = env["make"]("uid-b", "账号乙")
    cid = "33333333-3333-3333-3333-333333333333"
    _seed_session(env["db"], cid, "uid-a", "预演")
    _write_jsonl(env["projects"], "ws-1", cid, [{"sessionId": cid}])
    env["engine"].capture(SCOPE, a.id)

    report = env["engine"].apply(SCOPE, b.id, dry_run=True)
    assert report.state == "planned"
    assert [entry["action"] for entry in report.plan] == ["create"]
    assert _rows(env["db"], "uid-b") == []


def test_append_tail_when_source_continues(env):
    a = env["make"]("uid-a", "账号甲")
    b = env["make"]("uid-b", "账号乙")
    cid = "44444444-4444-4444-4444-444444444444"
    _seed_session(env["db"], cid, "uid-a", "追加")
    _write_jsonl(env["projects"], "ws-1", cid, [{"sessionId": cid, "n": 1}])
    env["engine"].capture(SCOPE, a.id)
    env["engine"].apply(SCOPE, b.id)
    b_cid = _rows(env["db"], "uid-b")[0][0]
    b_path = env["projects"] / "ws-1" / f"{b_cid}.jsonl"

    # 源账号继续写两行，重新采集后再切回目标账号：只追加尾段，不新建会话。
    _write_jsonl(
        env["projects"],
        "ws-1",
        cid,
        [{"sessionId": cid, "n": 1}, {"sessionId": cid, "n": 2}, {"sessionId": cid, "n": 3}],
    )
    env["engine"].capture(SCOPE, a.id)
    report = env["engine"].apply(SCOPE, b.id)
    assert report.counts["appended"] == 1
    assert report.counts["created"] == 0
    assert len(b_path.read_text(encoding="utf-8").splitlines()) == 3
    assert len(_rows(env["db"], "uid-b")) == 1


def test_conflict_keeps_local_and_creates_copy(env):
    a = env["make"]("uid-a", "账号甲")
    b = env["make"]("uid-b", "账号乙")
    cid = "55555555-5555-5555-5555-555555555555"
    _seed_session(env["db"], cid, "uid-a", "分叉")
    _write_jsonl(env["projects"], "ws-1", cid, [{"sessionId": cid, "n": 1}])
    env["engine"].capture(SCOPE, a.id)
    env["engine"].apply(SCOPE, b.id)
    b_cid = _rows(env["db"], "uid-b")[0][0]
    b_path = env["projects"] / "ws-1" / f"{b_cid}.jsonl"

    # 目标账号本地被改写（模拟用户在 B 上编辑/继续聊天，且内容与源端分叉）。
    b_path.write_text(
        json.dumps({"sessionId": b_cid, "n": 99}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    report = env["engine"].apply(SCOPE, b.id)
    assert report.counts["conflicts"] == 1
    # 本地保留，另存一份冲突副本 → 目标账号下两条会话。
    assert b_path.is_file()
    assert len(_rows(env["db"], "uid-b")) == 2
    titles = [row[2] for row in _rows(env["db"], "uid-b")]
    assert any("冲突副本" in title for title in titles)


def test_tombstone_soft_deletes_target(env):
    a = env["make"]("uid-a", "账号甲")
    b = env["make"]("uid-b", "账号乙")
    cid = "66666666-6666-6666-6666-666666666666"
    _seed_session(env["db"], cid, "uid-a", "删除")
    _write_jsonl(env["projects"], "ws-1", cid, [{"sessionId": cid}])
    env["engine"].capture(SCOPE, a.id)
    env["engine"].apply(SCOPE, b.id)

    conn = sqlite3.connect(str(env["db"]))
    conn.execute("UPDATE sessions SET deleted_at = 2000 WHERE id = ?", (cid,))
    conn.commit()
    conn.close()

    env["engine"].capture(SCOPE, a.id)
    report = env["engine"].apply(SCOPE, b.id)
    assert report.counts["removed"] == 1
    assert _rows(env["db"], "uid-b")[0][3] is not None


def test_trae_is_unavailable_and_writes_nothing(env):
    trae = env["repo"].upsert(
        scope="trae-cn",
        external_id="trae-1",
        display_name="Trae 账号",
        credential={"blob": "x"},
    ).account
    report = env["engine"].capture("trae-cn", trae.id)
    assert report.state == "unavailable"
    assert env["engine"].apply("trae-cn", trae.id).state == "unavailable"


def test_forget_account_removes_items_and_garbage_collects(env):
    """永久删号后镜像必须真的消失：行清空、失去引用的正文回收。"""
    a = env["make"]("uid-a", "账号甲")
    cid = "77777777-7777-7777-7777-777777777777"
    _seed_session(env["db"], cid, "uid-a", "待删")
    _write_jsonl(env["projects"], "ws-1", cid, [{"sessionId": cid}])

    env["engine"].capture(SCOPE, a.id)
    assert env["engine"].overview(SCOPE)["blobBytes"] > 0

    env["engine"].forget_account(SCOPE, a.id)

    assert env["engine"].items(SCOPE, a.id) == []
    assert env["engine"].overview(SCOPE)["blobBytes"] == 0


def _make_conflict(env) -> dict:
    """采 A → 铺 B → 在 B 本地改成另一版，制造一条冲突镜像。"""
    a = env["make"]("uid-a", "账号甲")
    b = env["make"]("uid-b", "账号乙")
    cid = "88888888-8888-8888-8888-888888888888"
    _seed_session(env["db"], cid, "uid-a", "分叉")
    _write_jsonl(env["projects"], "ws-1", cid, [{"sessionId": cid, "n": 1}])
    env["engine"].capture(SCOPE, a.id)
    env["engine"].apply(SCOPE, b.id)
    b_cid = _rows(env["db"], "uid-b")[0][0]
    b_path = env["projects"] / "ws-1" / f"{b_cid}.jsonl"
    b_path.write_text(json.dumps({"sessionId": b_cid, "n": 99}) + "\n", encoding="utf-8")

    report = env["engine"].apply(SCOPE, b.id)
    assert report.counts["conflicts"] == 1
    conflicted = [item for item in env["engine"].items(SCOPE, b.id) if item["conflict"]]
    assert len(conflicted) == 1
    return {"account": b, "cid": b_cid, "path": b_path, "item": conflicted[0]}


def test_resolve_conflict_keep_mirror_overwrites_local(env):
    case = _make_conflict(env)
    report = env["engine"].resolve_conflict(
        SCOPE, case["account"].id, case["item"]["stableKey"], "keepMirror"
    )
    assert report.state == "ok"
    body = case["path"].read_text(encoding="utf-8")
    assert '"n": 1' in body
    assert '"n": 99' not in body
    assert all(not item["conflict"] for item in env["engine"].items(SCOPE, case["account"].id))


def test_resolve_conflict_keep_source_keeps_local(env):
    case = _make_conflict(env)
    report = env["engine"].resolve_conflict(
        SCOPE, case["account"].id, case["item"]["stableKey"], "keepSource"
    )
    assert report.state == "ok"
    assert '"n": 99' in case["path"].read_text(encoding="utf-8")
    assert all(not item["conflict"] for item in env["engine"].items(SCOPE, case["account"].id))


def test_resolve_conflict_keep_both_clears_flag(env):
    case = _make_conflict(env)
    before = len(_rows(env["db"], "uid-b"))
    report = env["engine"].resolve_conflict(
        SCOPE, case["account"].id, case["item"]["stableKey"], "keepBoth"
    )
    assert report.state == "ok"
    assert len(_rows(env["db"], "uid-b")) >= before
    assert all(not item["conflict"] for item in env["engine"].items(SCOPE, case["account"].id))

