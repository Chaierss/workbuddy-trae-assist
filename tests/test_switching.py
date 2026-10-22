from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from chaierww.adapters.base import (
    AdapterError,
    Detection,
    ImportedAccount,
    PlannedFile,
    SwitchPlan,
)
from chaierww.domain.accounts.repository import AccountRepository
from chaierww.domain.backup import BackupService
from chaierww.domain.switching import SwitchEngine, encode_payload, encode_token_payload
from chaierww.infra.db import open_database
from chaierww.infra.secrets import InMemoryBox

SCOPE = "workbuddy-cn"
INFO = "workbuddy-desktop.info"


def info_bytes(uid: str, uin: str = "10001") -> bytes:
    return json.dumps(
        {
            "account": {"uid": uid, "uin": uin, "type": "personal"},
            "auth": {
                "accessToken": {"$wbEncrypted": 1, "envelope": "A" * 200},
                "refreshToken": {"$wbEncrypted": 1, "envelope": "B" * 100},
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")


@dataclass
class FakeAdapter:
    """只操作用临时目录，行为与 WorkBuddyAdapter 一致，但可注入故障。"""

    scope: str = SCOPE
    running: bool = False
    fail_write: bool = False
    fail_restore: bool = False
    verify_returns_false_for: str | None = None
    unsupported_token: bool = False

    def __post_init__(self) -> None:
        self.root = Path()
        self.launched = False
        self.terminated = False

    def bind(self, root: Path) -> FakeAdapter:
        self.root = root
        return self

    @property
    def state_file(self) -> Path:
        return self.root / INFO

    def state_dir(self) -> Path:
        return self.root

    def detect(self) -> Detection:
        return Detection(
            installed=True,
            running=self.running,
            logged_in=self.state_file.exists(),
            state_dir=self.root,
            app_path=None,
        )

    def import_current(self) -> ImportedAccount:
        raw = self.state_file.read_bytes()
        account = json.loads(raw.decode("utf-8"))["account"]
        return ImportedAccount(
            external_id=account["uid"],
            display_name=f"用户 {account['uin']}",
            payload=raw,
        )

    def plan_switch(self, payload: bytes) -> SwitchPlan:
        return SwitchPlan(
            files=(
                PlannedFile(
                    path=self.state_file,
                    action="override" if self.state_file.exists() else "create",
                    size=self.state_file.stat().st_size if self.state_file.exists() else 0,
                ),
            ),
            requires_app_exit=self.running,
            warnings=(),
        )

    def plan_token_switch(self, account: dict) -> SwitchPlan:
        if self.unsupported_token:
            raise AdapterError("该平台明文账号不支持切换客户端")
        return SwitchPlan(files=(), requires_app_exit=self.running, warnings=())

    def apply_switch(self, payload: bytes) -> None:
        if self.fail_write:
            raise AdapterError("注入写入失败")
        if self.running:
            raise AdapterError("客户端仍在运行")
        tmp = self.state_file.with_name(INFO + ".tmp")
        tmp.write_bytes(payload)
        tmp.replace(self.state_file)

    def verify_switch(self, expected_external_id: str) -> bool:
        if self.verify_returns_false_for == expected_external_id:
            return False
        try:
            raw = self.state_file.read_bytes()
            return json.loads(raw.decode("utf-8"))["account"]["uid"] == expected_external_id
        except (OSError, ValueError, KeyError):
            return False

    def is_running(self) -> bool:
        return self.running

    def launch(self) -> bool:
        self.launched = True
        self.running = True
        return True

    def terminate(self) -> bool:
        self.terminated = True
        was_running = self.running
        self.running = False  # 模拟进程退出
        return was_running

    def wait_exit(self, timeout: float = 5.0) -> bool:
        return not self.running

    def restore_from(self, source_dir: Path) -> None:
        if self.fail_restore:
            raise AdapterError("注入回滚失败")
        for item in list(self.root.iterdir()):
            if item.is_file() and item.name != "manifest.json":
                item.unlink()
        for item in source_dir.iterdir():
            if item.is_file() and item.name != "manifest.json":
                shutil.copy2(item, self.root / item.name)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def world(tmp_path):
    conn = open_database(tmp_path / "t.db")
    repo = AccountRepository(conn, InMemoryBox())
    backups = BackupService(conn, tmp_path / "backups")
    root = tmp_path / "auth"
    root.mkdir()
    (root / INFO).write_bytes(info_bytes("uid-A"))
    adapter = FakeAdapter().bind(root)
    engine = SwitchEngine(repo, backups, {SCOPE: adapter}, conn, keep_snapshots=5)

    def add_account(uid: str, payload: bytes | None = None):
        return repo.upsert(
            scope=SCOPE,
            external_id=uid,
            display_name=f"用户 {uid}",
            credential=encode_payload(payload or info_bytes(uid)),
        ).account

    def add_token_account(uid: str):
        return repo.upsert(
            scope=SCOPE,
            external_id=uid,
            display_name=f"用户 {uid}",
            credential=encode_token_payload({"uid": uid, "accessToken": "jwt-plain"}),
        ).account

    yield {
        "conn": conn,
        "repo": repo,
        "backups": backups,
        "adapter": adapter,
        "engine": engine,
        "root": root,
        "add": add_account,
        "add_token": add_token_account,
    }
    conn.close()


# ---------- 干跑 ----------


def test_dry_run_does_not_touch_any_file(world):
    account = world["add"]("uid-B")
    before = file_hash(world["root"] / INFO)

    report = world["engine"].dry_run(SCOPE, account.id)

    assert report.state == "planned"
    assert report.dry_run is True
    assert file_hash(world["root"] / INFO) == before
    assert world["backups"].list(SCOPE) == []


def test_dry_run_reports_app_exit_requirement(world):
    account = world["add"]("uid-B")
    world["adapter"].running = True

    report = world["engine"].dry_run(SCOPE, account.id)

    assert report.plan is not None
    assert report.plan.requires_app_exit is True
    # 客户端运行中不再阻塞预演：这一步恒为 ok，只作提示。
    assert all(step.ok for step in report.steps)


# ---------- 正常切换 ----------


def test_switch_replaces_state_and_records_snapshot(world):
    account = world["add"]("uid-B")

    report = world["engine"].execute(SCOPE, account.id)

    assert report.state == "succeeded"
    assert report.snapshot_id is not None
    assert world["adapter"].verify_switch("uid-B")
    # 切换成功后，pre-switch 临时备份立即删除，不堆积常驻快照。
    assert len(world["backups"].list(SCOPE)) == 0

    jobs = world["engine"].history(SCOPE)
    assert jobs[0]["state"] == "succeeded"


def test_switch_updates_last_used(world):
    account = world["add"]("uid-B")
    world["engine"].execute(SCOPE, account.id)
    assert world["repo"].get(account.id).last_used_at is not None


def test_switch_can_relaunch(world):
    account = world["add"]("uid-B")
    world["engine"].execute(SCOPE, account.id, relaunch=True)
    assert world["adapter"].launched is True


# ---------- 运行中先退出再写 ----------


def test_switch_terminates_client_before_write(world):
    """客户端运行中：引擎先退出客户端再写登录态，避免文件锁导致写入/回滚失败。"""
    account = world["add"]("uid-B")
    world["adapter"].running = True

    report = world["engine"].execute(SCOPE, account.id)

    # 切换成功，且切换过程中退出了客户端、写入了新登录态
    assert report.state == "succeeded"
    assert world["adapter"].terminated is True
    assert world["adapter"].verify_switch("uid-B") is True


# ---------- 故障与回滚 ----------


def test_verify_failure_triggers_rollback(world):
    account = world["add"]("uid-B")
    before = file_hash(world["root"] / INFO)
    world["adapter"].verify_returns_false_for = "uid-B"

    report = world["engine"].execute(SCOPE, account.id)

    assert report.state == "rolled_back"
    assert report.rollback_failed is False
    assert file_hash(world["root"] / INFO) == before
    assert world["adapter"].verify_switch("uid-A")


def test_write_failure_triggers_rollback(world):
    account = world["add"]("uid-B")
    before = file_hash(world["root"] / INFO)
    world["adapter"].fail_write = True

    report = world["engine"].execute(SCOPE, account.id)

    assert report.state == "rolled_back"
    assert file_hash(world["root"] / INFO) == before


def test_rollback_failure_is_reported_loudly(world):
    account = world["add"]("uid-B")
    world["adapter"].verify_returns_false_for = "uid-B"
    world["adapter"].fail_restore = True

    report = world["engine"].execute(SCOPE, account.id)

    assert report.state == "rollback_failed"
    assert report.rollback_failed is True
    assert report.snapshot_id
    assert "手动恢复" in (report.error or "")


def test_backup_failure_aborts_without_writing(world):
    account = world["add"]("uid-B")
    before = file_hash(world["root"] / INFO)

    def boom(**kwargs):
        from chaierww.domain.backup import BackupError

        raise BackupError("注入备份失败")

    world["backups"].create = boom  # type: ignore[method-assign]

    report = world["engine"].execute(SCOPE, account.id)

    assert report.state == "failed_backup"
    assert file_hash(world["root"] / INFO) == before


# ---------- 目标校验 ----------


def test_deleted_account_cannot_be_switched_to(world):
    account = world["add"]("uid-B")
    world["repo"].soft_delete(account.id)

    with pytest.raises(Exception, match="回收站"):
        world["engine"].execute(SCOPE, account.id)


def test_scope_mismatch_rejected(world):
    account = world["add"]("uid-B")
    with pytest.raises(Exception, match="作用域"):
        world["engine"].execute("trae-cn", account.id)


# ---------- 凭据形态预检 ----------


def test_unsupported_token_switch_is_blocked_before_backup(world):
    """明文 token 账号在平台不支持时，应在备份/写入前就被拦截，不产生备份点、不碰文件。"""
    account = world["add_token"]("uid-T")
    world["adapter"].unsupported_token = True
    before = file_hash(world["root"] / INFO)

    report = world["engine"].execute(SCOPE, account.id)

    assert report.state == "unsupported"
    assert report.succeeded is False
    assert "不支持切换" in (report.error or "")
    # 关键：没有创建任何备份点（不应有 pre-switch 快照）
    snapshots = world["backups"].list(SCOPE)
    assert snapshots == []
    # 文件未被触碰
    assert file_hash(world["root"] / INFO) == before
