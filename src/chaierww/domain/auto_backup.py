"""每日自动备份（设置 → 备份与恢复 → 自动备份）。

规则（与界面上的说明一致）：
- 默认开启；对每个已实现的客户端作用域，每天落一份 `reason="auto"` 的快照；
- 每个作用域只保留最近 `AUTO_KEEP` **份**（不是天数）：正常每天一份，
  你每点一次「立即备份」也算一份，超过 7 份就从最旧的开始滚动删除；
- 手动备份同样参与这份滚动队列，不会被永久保留。

线程语义与 `CatalogRefresher` / `RotationRunner` 一致：开关与目录都从外部**现读**
（设置里一关就停），单轮失败只记日志、绝不退出线程。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from chaierww.domain.backup import BackupError, BackupService, Snapshot

logger = logging.getLogger(__name__)

#: 快照的 reason：自动备份。手动（manual）与切换前（preswitch）不在这条规则的「生成」里，
#: 但「保留份数」是所有来源共用的一条队列。
AUTO_REASON = "auto"
#: 每个作用域保留的快照份数（按次数滚动，不按天数）。
AUTO_KEEP = 7
#: 两次自动备份之间的最小间隔（秒）：每天一份。
AUTO_INTERVAL_SECONDS = 24 * 60 * 60
#: 线程检查间隔（秒）：10 分钟看一次是否到点，够用且开销可忽略。
AUTO_TICK_SECONDS = 600.0


class AutoBackupRunner:
    """后台每日自动备份的单线程。

    `enabled` 读偏好开关；`targets` 返回 `(scope, 客户端数据目录)` 列表
    （目录不存在的作用域会被跳过，不报错）。
    """

    def __init__(
        self,
        enabled: Callable[[], bool],
        targets: Callable[[], list[tuple[str, Path]]],
        backups: BackupService,
        *,
        tick_seconds: float = AUTO_TICK_SECONDS,
        interval_seconds: float = AUTO_INTERVAL_SECONDS,
        keep: int = AUTO_KEEP,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._enabled = enabled
        self._targets = targets
        self._backups = backups
        self._tick_seconds = tick_seconds
        self._interval_seconds = interval_seconds
        self._keep = keep
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="chaierww-autobackup", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)

    def _loop(self) -> None:
        # 先跑一轮再等：应用启动时就把当天缺的那份补上，不用干等一个检查周期。
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - 后台线程不能被一次异常带走
                logger.exception("自动备份：本轮失败")
            if self._stop.wait(self._tick_seconds):
                return

    def tick(self) -> int:
        """跑一轮：该备份的备份，然后按份数清理。返回本轮新建份数。"""
        if not self._enabled():
            return 0
        created = 0
        for scope, source_dir in self._targets():
            if source_dir.is_dir() and not self._is_fresh(scope):
                try:
                    self._backups.create(
                        scope=scope,
                        source_dir=source_dir,
                        reason=AUTO_REASON,
                        note="自动备份",
                    )
                    created += 1
                    logger.info("自动备份：%s 完成", scope)
                except (BackupError, OSError, ValueError) as exc:
                    logger.warning("自动备份：%s 失败（%s）", scope, exc)
            # 清理不受「今天备过没」「目录还在不在」影响：客户端卸载后旧快照也要淘汰。
            try:
                removed = self._backups.prune(scope, self._keep)
                if removed:
                    logger.info(
                        "自动备份：%s 清理 %s 份超出保留数的快照", scope, removed
                    )
            except Exception:  # noqa: BLE001 - 清理失败不影响下一轮
                logger.warning("自动备份：%s 清理失败", scope)
        return created

    def _is_fresh(self, scope: str) -> bool:
        last = self._backups.latest(scope, AUTO_REASON)
        if last is None:
            return False
        return self._age_seconds(last) < self._interval_seconds

    def _age_seconds(self, snapshot: Snapshot) -> float:
        try:
            created = datetime.fromisoformat(snapshot.created_at)
        except ValueError:
            return float("inf")
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return (self._clock() - created).total_seconds()
