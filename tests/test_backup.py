"""备份：作用域快照 + 全局分区快照（阶段六 / G9）。

全局备份的重点是**默认不含凭据**：`accounts` / `gateway` 两个分区必须显式
二次确认才会写进快照，这一点在接口与界面两端都要成立（见 routes.py 的
`confirmCredentials` 与 BackupTab 的确认态）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from chaierww.domain.backup import (
    CREDENTIAL_PARTS,
    DEFAULT_GLOBAL_PARTS,
    GLOBAL_PARTS,
    GLOBAL_SCOPE,
    MANIFEST_NAME,
    BackupError,
    BackupService,
)
from chaierww.infra.db import open_database


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / "preferences.json").write_text('{"keepalive": true}', encoding="utf-8")
    (home / "schedules.json").write_text('{"tasks": []}', encoding="utf-8")
    (home / "gateway.json").write_text('{"enabled": false}', encoding="utf-8")
    (home / "gateway-keys.json").write_text('{"keys": []}', encoding="utf-8")
    return home


@pytest.fixture()
def conn(home: Path) -> Iterator[sqlite3.Connection]:
    connection = open_database(home / "chaierww.db")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def service(conn: sqlite3.Connection, home: Path) -> BackupService:
    return BackupService(conn, home / "backups")


def _manifest(snapshot) -> dict[str, object]:  # noqa: ANN001 - 只在本文件内用
    raw = (snapshot.directory / MANIFEST_NAME).read_text(encoding="utf-8")
    return json.loads(raw)


def test_default_parts_exclude_credentials() -> None:
    """默认勾选里不能出现任何含凭据的分区 —— 这是「默认不含凭据」的根。"""
    assert not set(DEFAULT_GLOBAL_PARTS) & CREDENTIAL_PARTS
    assert set(DEFAULT_GLOBAL_PARTS) <= set(GLOBAL_PARTS)
    assert CREDENTIAL_PARTS.issubset(GLOBAL_PARTS)


def test_config_part_copies_only_registered_files(service: BackupService, home: Path) -> None:
    snapshot = service.create_global(home, ["config"], note="手动备份")

    assert snapshot.scope == GLOBAL_SCOPE
    assert snapshot.reason == "manual"
    assert snapshot.verified is True
    assert snapshot.file_count == 2
    names = sorted(path.name for path in snapshot.directory.iterdir())
    assert names == [MANIFEST_NAME, "preferences.json", "schedules.json"]

    manifest = _manifest(snapshot)
    assert manifest["parts"] == ["config"]
    assert sorted(entry["name"] for entry in manifest["files"]) == [
        "preferences.json",
        "schedules.json",
    ]


def test_missing_part_files_are_skipped(service: BackupService, home: Path) -> None:
    """`gateway-pool.json` 在夹具里不存在：跳过快照、不报错。"""
    snapshot = service.create_global(home, ["gateway"], confirm_credentials=True)
    names = sorted(path.name for path in snapshot.directory.iterdir())
    assert names == ["gateway-keys.json", "gateway.json", MANIFEST_NAME]


def test_duplicate_parts_are_deduped(service: BackupService, home: Path) -> None:
    snapshot = service.create_global(home, ["config", "config"])
    manifest = _manifest(snapshot)
    assert manifest["parts"] == ["config"]
    assert snapshot.file_count == 2


@pytest.mark.parametrize("part", sorted(CREDENTIAL_PARTS))
def test_credential_parts_require_confirmation(
    service: BackupService, home: Path, part: str
) -> None:
    with pytest.raises(ValueError, match="二次确认"):
        service.create_global(home, [part])
    # 拒绝时一个字节都不该落盘。
    assert not (home / "backups").exists()


def test_unknown_part_is_rejected(service: BackupService, home: Path) -> None:
    with pytest.raises(ValueError, match="未知分区"):
        service.create_global(home, ["nope"])


def test_empty_parts_are_rejected(service: BackupService, home: Path) -> None:
    with pytest.raises(ValueError, match="至少要勾选"):
        service.create_global(home, [])


def test_accounts_part_is_copied_online(
    service: BackupService, conn: sqlite3.Connection, home: Path
) -> None:
    """账号库是 WAL 模式的活库：备份走 SQLite 在线备份，要能读到已写入的行。"""
    conn.execute(
        "INSERT INTO accounts (id, scope, external_id, display_name, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            "a1",
            "workbuddy-cn",
            "u1",
            "测试账号",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )

    snapshot = service.create_global(home, ["accounts"], confirm_credentials=True)
    copy = snapshot.directory / "chaierww.db"
    assert copy.is_file()

    with sqlite3.connect(str(copy)) as check:
        tables = {
            row[0]
            for row in check.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"accounts", "credentials", "snapshots"} <= tables
        assert check.execute("SELECT display_name FROM accounts").fetchone()[0] == "测试账号"


def test_parts_without_any_file_fail(service: BackupService, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(BackupError, match="没什么可备份"):
        service.create_global(empty, ["config"])
    # 失败不留半成品：分区目录里不能有残留快照。
    scope_dir = empty / "backups" / GLOBAL_SCOPE
    assert not scope_dir.exists() or not list(scope_dir.iterdir())


def test_verify_detects_tampering(service: BackupService, home: Path) -> None:
    snapshot = service.create_global(home, ["config"])
    assert service.verify(snapshot.id) is True
    (snapshot.directory / "preferences.json").write_text('{"keepalive": false}', encoding="utf-8")
    assert service.verify(snapshot.id) is False


def test_global_snapshots_are_listed(service: BackupService, home: Path) -> None:
    first = service.create_global(home, ["config"])
    second = service.create_global(home, ["config"])

    listed = [item.id for item in service.list(GLOBAL_SCOPE)]
    assert set(listed) == {first.id, second.id}


def test_scope_backup_still_works(service: BackupService, home: Path) -> None:
    """回归：`create()`（备份客户端登录态目录）在重构后行为不变。"""
    source = home / "client-state"
    source.mkdir()
    (source / "workbuddy-desktop.info").write_text("uid", encoding="utf-8")

    snapshot = service.create(scope="workbuddy-cn", source_dir=source, reason="manual")
    assert snapshot.scope == "workbuddy-cn"
    assert snapshot.file_count == 1
    assert service.verify(snapshot.id) is True


def test_keep_latest_snapshots_by_count_regardless_of_reason(
    service: BackupService, home: Path
) -> None:
    """按份数滚动：自动、切换前、手动一起排队，只留最近 7 份，最旧的先删。"""
    source = home / "client-state"
    source.mkdir()
    (source / "state.json").write_text("{}", encoding="utf-8")

    reasons = [
        "auto",
        "manual",
        "preswitch",
        "manual",
        "auto",
        "preswitch",
        "manual",
        "auto",
        "manual",
    ]
    created = [
        service.create(scope="workbuddy-cn", source_dir=source, reason=reason)
        for reason in reasons
    ]

    # create() 自己就会滚动，所以 9 次创建之后只剩 7 份。
    remaining = service.list("workbuddy-cn", limit=50)
    assert len(remaining) == 7
    remaining_ids = {item.id for item in remaining}
    assert remaining_ids == {item.id for item in created[2:]}
    # 被删掉的既包含自动也包含手动：没有哪一类能永久占位。
    assert created[0].reason == "auto"
    assert created[1].reason == "manual"
    assert not Path(created[0].directory).exists()
    assert not Path(created[1].directory).exists()

    # 显式调用仍然按份数：keep=1 只留最新一份。
    assert service.prune("workbuddy-cn", 1) == 6
    assert [item.id for item in service.list("workbuddy-cn")] == [created[-1].id]


def test_global_snapshots_roll_by_count(service: BackupService, home: Path) -> None:
    """全局备份同样只留最近 7 份（它们都是 manual，说明手动也参与滚动）。"""
    ids = [service.create_global(home, ["config"]).id for _ in range(9)]

    listed = [item.id for item in service.list(GLOBAL_SCOPE)]
    assert len(listed) == 7
    assert set(listed) == set(ids[2:])


def test_auto_backup_runs_once_per_day_and_respects_switch(tmp_path: Path) -> None:
    """自动备份：开关关着不动；当天已有 auto 快照就不再重复；目录不存在直接跳过。"""
    from chaierww.domain.auto_backup import AUTO_REASON, AutoBackupRunner

    home = tmp_path / "home"
    home.mkdir()
    conn = open_database(home / "chaierww.db")
    service = BackupService(conn, home / "backups")
    source = home / "client-state"
    source.mkdir()
    (source / "state.json").write_text("{}", encoding="utf-8")

    enabled = [True]
    runner = AutoBackupRunner(
        lambda: enabled[0],
        lambda: [("workbuddy-cn", source), ("trae-cn", home / "missing")],
        service,
        interval_seconds=24 * 3600,
        keep=7,
    )

    try:
        assert runner.tick() == 1
        # 同一天再跑：已有 auto 快照，不重复。
        assert runner.tick() == 0
        auto_count = len(
            [item for item in service.list("workbuddy-cn") if item.reason == AUTO_REASON]
        )
        assert auto_count == 1

        enabled[0] = False
        assert runner.tick() == 0
    finally:
        conn.close()


def test_auto_backup_prunes_to_latest_seven_even_without_client_dir(tmp_path: Path) -> None:
    """客户端目录被卸载后，旧快照照样按份数淘汰：不留孤儿。"""
    from chaierww.domain.auto_backup import AutoBackupRunner

    home = tmp_path / "home"
    home.mkdir()
    conn = open_database(home / "chaierww.db")
    service = BackupService(conn, home / "backups")
    # 直接灌 9 份历史快照（绕过 create 的自动滚动，模拟旧版本留下的存量）。
    for index in range(9):
        conn.execute(
            "INSERT INTO snapshots (id, scope, created_at, reason, account_id, directory, "
            "total_bytes, file_count, verified, note) "
            "VALUES (?, 'trae-cn', ?, 'auto', NULL, ?, 1, 1, 1, '')",
            (f"old-{index}", f"2026-09-0{index + 1}T00:00:00+00:00", str(home / f"old-{index}")),
        )
    conn.commit()

    runner = AutoBackupRunner(
        lambda: True,
        lambda: [("trae-cn", home / "missing")],
        service,
        interval_seconds=24 * 3600,
        keep=7,
    )
    try:
        assert runner.tick() == 0
        assert len(service.list("trae-cn", limit=50)) == 7
    finally:
        conn.close()


def test_auto_global_backup_once_per_day_and_respects_switch(
    service: BackupService, home: Path
) -> None:
    """全局备份的自动档：一天一份、开关关掉就不再产生、默认分区不含凭据。"""
    from chaierww.domain.auto_backup import AUTO_REASON, AutoBackupRunner

    enabled = [True]
    runner = AutoBackupRunner(
        lambda: False,  # 客户端快照那条先关掉，只观察全局这一条
        lambda: [],
        service,
        global_enabled=lambda: enabled[0],
        global_home=lambda: home,
        interval_seconds=24 * 3600,
        keep=7,
    )

    assert runner.tick() == 1
    items = service.list(GLOBAL_SCOPE)
    assert len(items) == 1
    assert items[0].reason == AUTO_REASON
    assert items[0].verified is True

    # 当天再跑：已有 auto 全局备份，不重复。
    assert runner.tick() == 0
    assert len(service.list(GLOBAL_SCOPE)) == 1

    # 自动备份里绝不能出现凭据文件 —— 无人值守的备份不弹二次确认。
    names = sorted(entry["name"] for entry in _manifest(items[0])["files"])
    assert names == ["preferences.json", "schedules.json"]
    assert _manifest(items[0])["parts"] == list(DEFAULT_GLOBAL_PARTS)

    # 开关关掉：一份都不加。
    enabled[0] = False
    assert runner.tick() == 0
    assert len(service.list(GLOBAL_SCOPE)) == 1


def test_auto_global_backup_follows_dynamic_parts(service: BackupService, home: Path) -> None:
    """自动备份分区随偏好现读：勾选含凭据的 gateway 后，自动备份也会收录它。"""
    from chaierww.domain.auto_backup import AUTO_REASON, AutoBackupRunner

    parts = [["config"]]  # 模拟偏好里的 autoGlobalParts，先默认后加含凭据分区
    runner = AutoBackupRunner(
        lambda: False,
        lambda: [],
        service,
        global_enabled=lambda: True,
        global_home=lambda: home,
        global_parts=lambda: tuple(parts[0]),
        interval_seconds=24 * 3600,
        keep=7,
    )

    # 默认只有 config：不含 gateway 文件
    assert runner.tick() == 1
    first = service.list(GLOBAL_SCOPE)[0]
    assert _manifest(first)["parts"] == ["config"]

    # 用户勾选了 gateway（含凭据）并确认 → 偏好更新，下次自动备份带上它
    parts[0] = ["config", "gateway"]
    # 重置「今天已备」的间隔，让下一轮能再落一份
    runner._interval_seconds = 0  # noqa: SLF001 - 测试里强制让 age 判定过期
    assert runner.tick() == 1
    # 两份快照 created_at 可能落在同一秒，排序不可靠，按 parts 定位带凭据的那份
    snapshots = service.list(GLOBAL_SCOPE)
    gateway_snap = next(
        s for s in snapshots if _manifest(s)["parts"] == ["config", "gateway"]
    )
    names = sorted(entry["name"] for entry in _manifest(gateway_snap)["files"])
    assert "gateway.json" in names
    assert "gateway-keys.json" in names
    assert _manifest(gateway_snap)["parts"] == ["config", "gateway"]
    assert gateway_snap.reason == AUTO_REASON


def test_auto_global_backup_skips_when_no_source_files(tmp_path: Path) -> None:
    """全新安装、默认分区一个文件都没有时静默跳过，不刷失败日志。"""
    from chaierww.domain.auto_backup import AutoBackupRunner

    empty = tmp_path / "fresh"
    empty.mkdir()
    conn = open_database(empty / "chaierww.db")
    service = BackupService(conn, empty / "backups")
    runner = AutoBackupRunner(
        lambda: False,
        lambda: [],
        service,
        global_enabled=lambda: True,
        global_home=lambda: empty,
    )
    try:
        assert runner.tick() == 0
        assert service.list(GLOBAL_SCOPE) == []
    finally:
        conn.close()


def test_create_global_reason_is_recorded(service: BackupService, home: Path) -> None:
    """`reason` 由调用方给：后台自动备份靠它判断「今天备过没有」。"""
    manual = service.create_global(home, ["config"])
    auto = service.create_global(home, ["config"], reason="auto")

    assert manual.reason == "manual"
    assert auto.reason == "auto"
    latest = service.latest(GLOBAL_SCOPE, "auto")
    assert latest is not None
    assert latest.id == auto.id
