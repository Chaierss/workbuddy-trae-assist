from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from chaierww.domain.accounts.models import Account, CredentialState
from chaierww.domain.accounts.repository import (
    AccountNotFound,
    AccountNotInTrash,
    AccountRepository,
)
from chaierww.domain.scopes import all_scopes


class AccountInUseError(RuntimeError):
    """账号正在被客户端使用，不允许删除。"""


class AccountBusyError(RuntimeError):
    """账号正在参与切换任务，不允许删除。"""


class RuntimeProbe(Protocol):
    """运行时状态探针。

    删除规则需要知道"这个号是不是正在用"，但这个信息来自客户端登录态，
    不属于账号库自身的数据，因此用接口隔离，避免数据层依赖运行时。
    """

    def is_in_use(self, scope: str, external_id: str) -> bool: ...

    def is_switching(self, scope: str) -> bool: ...


class NullProbe:
    """M1a 未接入客户端登录态前使用；接入后替换为真实探针。"""

    def is_in_use(self, scope: str, external_id: str) -> bool:
        return False

    def is_switching(self, scope: str) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class ScopeSummary:
    scope: str
    total: int
    usable: int
    relogin_required: int
    unknown: int


class AccountService:
    def __init__(self, repository: AccountRepository, probe: RuntimeProbe | None = None) -> None:
        self._repo = repository
        self._probe = probe or NullProbe()

    def list_active(self, scope: str, query: str | None = None) -> list[Account]:
        return self._repo.list_accounts(scope, include_deleted=False, query=query)

    def list_merged(
        self, scopes: list[str] | tuple[str, ...], query: str | None = None
    ) -> list[Account]:
        """合并号池：把多个已实现作用域的账号合成一张表。

        平台不在这里合并改写，只按多 scope 取；行各自的 scope 就是平台标签。
        """
        return self._repo.list_all(scopes, include_deleted=False, query=query)

    def list_recycle_bin(self, scope: str | None = None) -> list[Account]:
        """回收站：只取已软删除的账号。

        `scope` 不传 = 合并号池口径（所有已实现作用域），因为号池页本身不区分平台。
        过滤在 SQL 里做，不再把整张表捞出来在调用方筛。
        """
        if scope:
            return self._repo.list_accounts(scope, only_deleted=True)
        implemented = [item.key for item in all_scopes() if item.implemented]
        return self._repo.list_all(implemented, only_deleted=True)

    def by_id(self, account_id: str) -> Account:
        return self._repo.get(account_id)

    def summary(self, scope: str) -> ScopeSummary:
        counts = self._repo.count_by_state(scope)
        return ScopeSummary(
            scope=scope,
            total=sum(counts.values()),
            usable=counts[CredentialState.USABLE],
            relogin_required=counts[CredentialState.RELOGIN_REQUIRED],
            unknown=counts[CredentialState.UNKNOWN],
        )

    def _guard(self, account: Account) -> None:
        """删除类操作的统一守卫：正在切换 / 正在客户端使用 → 拒绝。"""
        if self._probe.is_switching(account.scope):
            raise AccountBusyError("该作用域正在执行切换任务，请等待完成后再删除")

        if self._probe.is_in_use(account.scope, account.external_id):
            raise AccountInUseError("该账号正在客户端中使用，请先切换到其他账号再删除")

    def delete(self, account_id: str) -> None:
        """软删除。当前使用中或正在切换的账号拒绝删除，并说明原因。"""
        account = self._repo.get(account_id)
        if account.is_deleted:
            raise AccountNotFound(f"{account_id}（已在回收站）")

        self._guard(account)
        self._repo.soft_delete(account_id)

    def purge(self, account_id: str) -> Account:
        """永久删除回收站里的账号（含凭据），返回删前的行供关联清理。

        永久删除同样走使用中/切换中守卫：删掉的是不可恢复的凭据，若删的正是
        客户端当前登录的号，等 token 刷新失败就会掉线且没有补救路径；切换进行中
        时 switch_jobs 也正引用这个 id。等几秒远好过不可逆丢失。
        """
        account = self._repo.get(account_id)
        if not account.is_deleted:
            raise AccountNotInTrash(f"{account_id}（不在回收站，不能永久删除）")
        self._guard(account)
        return self._repo.purge(account_id)

    def restore(self, account_id: str) -> Account:
        return self._repo.restore(account_id)

    def purge_expired(self, days: int) -> int:
        return self._repo.purge_expired(days)

    def set_note(self, account_id: str, note: str) -> Account:
        return self._repo.update_note(account_id, note)
