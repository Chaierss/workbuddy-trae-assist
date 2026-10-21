"""切换演练：用真实登录态文件的副本，在临时目录里跑完整流程。

存在的意义：正式切换会改动真实登录态，不能随便试。
这个脚本把真实文件复制到临时目录，然后完整走一遍
备份 → 写入 → 校验 → （故障注入）回滚，全程不碰原始文件。

用法：
    python scripts/drill_switch.py
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from chaierww.adapters.workbuddy import (
    STATE_FILENAME,
    WorkBuddyAdapter,
    WorkBuddyPaths,
    default_paths,
)
from chaierww.domain.accounts.repository import AccountRepository
from chaierww.domain.backup import BackupService
from chaierww.domain.switching import SwitchEngine, encode_payload
from chaierww.infra.db import open_database
from chaierww.infra.secrets import default_box

SCOPE = "workbuddy-cn"


def make_fake_variant(raw: bytes, new_uid: str) -> bytes:
    """把真实的登录态复制一份、只改账号标识，模拟"另一个账号"的文件。"""
    data = json.loads(raw.decode("utf-8"))
    data["account"]["uid"] = new_uid
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def main() -> int:
    real = default_paths()
    if not real.state_file.is_file():
        print(f"[跳过] 找不到登录态文件：{real.state_file}")
        return 1

    workdir = Path(tempfile.mkdtemp(prefix="chaierww-drill-"))
    sandbox = workdir / "auth"
    shutil.copytree(real.state_dir, sandbox)
    print(f"[准备] 已把真实目录复制到沙箱：{sandbox}")

    adapter = WorkBuddyAdapter(
        paths=WorkBuddyPaths(
            state_dir=sandbox, state_file=sandbox / STATE_FILENAME, app_candidates=()
        )
    )
    adapter.is_running = lambda: False  # type: ignore[method-assign]

    conn = open_database(workdir / "drill.db")
    repo = AccountRepository(conn, default_box())
    backups = BackupService(conn, workdir / "backups")
    engine = SwitchEngine(repo, backups, {SCOPE: adapter}, conn)

    # 1. 导入沙箱里的当前账号
    imported = adapter.import_current()
    account_a = repo.upsert(
        scope=SCOPE,
        external_id=imported.external_id,
        display_name=imported.display_name,
        credential=encode_payload(imported.payload),
    ).account
    print(f"[导入] 当前账号 {account_a.display_name}（uid={account_a.external_id}）")

    # 2. 造一个"另一个账号"
    target_uid = "drill-0000-0000-0000-000000000001"
    account_b = repo.upsert(
        scope=SCOPE,
        external_id=target_uid,
        display_name="演练目标账号",
        credential=encode_payload(make_fake_variant(imported.payload, target_uid)),
    ).account

    # 3. 干跑
    plan = engine.dry_run(SCOPE, account_b.id)
    print(f"[干跑] 状态={plan.state} 需退出客户端={plan.plan.requires_app_exit}")
    for item in plan.plan.files[:3]:
        print(f"        {item.action:8} {item.path.name} ({item.size}B)")

    # 4. 真实切换
    report = engine.execute(SCOPE, account_b.id)
    print(f"[切换] 状态={report.state} 备份点={report.snapshot_id}")
    for step in report.steps:
        print(f"        {'OK ' if step.ok else 'ERR'} {step.step}: {step.detail}")
    assert report.state == "succeeded", "正常切换应当成功"
    assert adapter.verify_switch(target_uid), "切换后应为目标账号"
    print("[校验] 沙箱文件已是目标账号")

    # 5. 故障注入：校验失败必须回滚
    #    先把文件还原成原账号，这样"回滚成功"才有可验证的结果。
    adapter.apply_switch(imported.payload)
    assert adapter.verify_switch(imported.external_id)

    original_verify = adapter.verify_switch

    def failing_verify(expected: str) -> bool:
        if expected == target_uid:
            return False
        return original_verify(expected)

    adapter.verify_switch = failing_verify  # type: ignore[method-assign]

    rollback_report = engine.execute(SCOPE, account_b.id)
    print(f"[回滚] 状态={rollback_report.state} 回滚失败={rollback_report.rollback_failed}")
    print(f"        {rollback_report.error}")
    assert rollback_report.state == "rolled_back", "校验失败应触发回滚"
    assert adapter.verify_switch(imported.external_id), "回滚后应回到原账号"
    print("[校验] 沙箱文件已还原为原账号")

    # 6. 备份完整性
    snapshots = backups.list(SCOPE)
    print(f"[备份] 共 {len(snapshots)} 个备份点")
    for snapshot in snapshots:
        ok = backups.verify(snapshot.id)
        print(f"        {snapshot.id} 文件数={snapshot.file_count} 校验={'通过' if ok else '失败'}")
        assert ok, "备份必须通过完整性校验"

    conn.close()
    shutil.rmtree(workdir, ignore_errors=True)
    print(f"[清理] 沙箱已删除：{workdir}")
    print("\n演练全部通过：备份、写入、校验、回滚、完整性校验均正常。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
