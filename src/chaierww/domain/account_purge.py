"""永久删除回收站账号的编排层。

## 为什么单独一层

删一条账号行只涉及 ``AccountRepository``，但「彻底删除」还要顺带清掉散在别处的
引用：本机网页版登录态快照（``domain/webview_state``）、额度网关账号池的排除/指定
（``domain/gateway``）、轮换台账里指向它的记录（``domain/rotation``）。若把这些塞进
``AccountService``，账号域就会反向依赖网关与调度，层次立刻乱掉。

## 两条纪律

1. **先删库，再清外设**。反过来的话，库删失败时快照已经没了、账号还在，
   用户既丢数据又没删成，比什么都不做更糟。
2. **主操作必须成功，外设清理尽力而为**。账号删除失败 → 记进 ``failed`` 并继续
   下一条；外设清理失败 → 记进 ``warnings``，账号照样算删掉了。
   网关里残留一个永不命中的 id、台账里多一条指向已删账号的记录，都是只在界面上
   显示、不影响功能的残留，不值得为它回滚一次不可逆删除。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from chaierww.domain import webview_state
from chaierww.domain.accounts.models import Account
from chaierww.domain.accounts.repository import AccountNotFound, AccountNotInTrash
from chaierww.domain.accounts.service import (
    AccountBusyError,
    AccountInUseError,
    AccountService,
)
from chaierww.domain.gateway import GatewayStore
from chaierww.domain.rotation import RotationStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PurgedAccount:
    """一个被彻底删除的账号。"""

    account_id: str
    display_name: str
    snapshot_removed: bool


@dataclass(frozen=True, slots=True)
class PurgeFailure:
    """一个没删成的账号，`reason` 直接给人看。"""

    account_id: str
    display_name: str
    reason: str


@dataclass(frozen=True, slots=True)
class PurgeOutcome:
    """一次删除的结果。账号已删、但关联数据没清干净的情况进 `warnings`。"""

    purged: tuple[PurgedAccount, ...] = ()
    failed: tuple[PurgeFailure, ...] = ()
    warnings: tuple[str, ...] = ()


class AccountPurger:
    """把「删账号行」与「清关联引用」串起来。"""

    def __init__(
        self,
        service: AccountService,
        *,
        gateway: GatewayStore,
        rotation: RotationStore,
    ) -> None:
        self._service = service
        self._gateway = gateway
        self._rotation = rotation

    def purge(self, account_id: str) -> PurgeOutcome:
        """永久删除一个账号。账号不在回收站/正被使用时抛异常（由调用方映射状态码）。"""
        account = self._service.purge(account_id)
        purged, warnings = self._cleanup([account])
        return PurgeOutcome(purged=purged, warnings=warnings)

    def purge_all(self, scope: str | None = None) -> PurgeOutcome:
        """清空回收站（`scope` 不传 = 合并号池口径）。

        逐条删，单条失败不阻断其余：批量操作的价值就在于「能删的先删掉」，
        然后把没能删的和原因一次讲清。
        """
        purged: list[Account] = []
        failed: list[PurgeFailure] = []
        for account in self._service.list_recycle_bin(scope):
            try:
                purged.append(self._service.purge(account.id))
            except (AccountNotFound, AccountNotInTrash, AccountBusyError, AccountInUseError) as exc:
                failed.append(PurgeFailure(account.id, account.display_name, str(exc)))
            except Exception as exc:  # noqa: BLE001 - 单个号出意外不该让整批 500
                logger.exception("永久删除账号 %s 时出错", account.id)
                failed.append(
                    PurgeFailure(account.id, account.display_name, f"删除失败：{exc}")
                )
        rows, warnings = self._cleanup(purged)
        return PurgeOutcome(purged=rows, failed=tuple(failed), warnings=warnings)

    def _cleanup(
        self, accounts: list[Account]
    ) -> tuple[tuple[PurgedAccount, ...], tuple[str, ...]]:
        """账号行已删掉之后，清掉指向它们的关联数据。步骤之间互不影响。"""
        done: list[PurgedAccount] = []
        warnings: list[str] = []
        for account in accounts:
            removed = False
            try:
                removed = webview_state.drop_snapshot(account.external_id)
            except OSError as exc:
                logger.warning("删除网页版快照失败：%s", exc)
                warnings.append(
                    f"「{account.display_name}」的本机网页版登录态快照没删掉：{exc}"
                )
            try:
                self._gateway.forget_account(account.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("清理网关账号池引用失败：%s", exc)
                warnings.append(f"网关账号池里的排除/指定设置没清干净：{exc}")
            try:
                self._rotation.forget_account(account.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("清理轮换台账引用失败：%s", exc)
                warnings.append(f"轮换台账里指向它的记录没清干净：{exc}")
            done.append(PurgedAccount(account.id, account.display_name, removed))
        return tuple(done), tuple(warnings)
