from __future__ import annotations

import pytest

from chaierww.domain.accounts.models import CredentialState
from chaierww.domain.accounts.repository import AccountNotFound, AccountRepository
from chaierww.domain.accounts.service import AccountInUseError, AccountService
from chaierww.domain.scopes import ScopeNotImplemented, UnknownScope, get_scope
from chaierww.infra.db import open_database
from chaierww.infra.secrets import InMemoryBox
from chaierww.logging_setup import redact

SCOPE = "workbuddy-cn"


@pytest.fixture
def conn(tmp_path):
    connection = open_database(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def repo(conn):
    return AccountRepository(conn, InMemoryBox())


def _import(repo: AccountRepository, external_id="u-1", name="账号甲", token="tok-secret-value"):
    return repo.upsert(
        scope=SCOPE,
        external_id=external_id,
        display_name=name,
        credential={"access_token": token, "refresh_token": "refresh-secret"},
    )


# ---------- 作用域 fail-closed ----------


def test_unknown_scope_is_rejected_not_defaulted():
    with pytest.raises(UnknownScope):
        get_scope("workbuddy-xx")


def test_empty_scope_is_rejected():
    with pytest.raises(UnknownScope):
        get_scope("")
    with pytest.raises(UnknownScope):
        get_scope(None)


def test_unimplemented_scope_cannot_be_written(repo):
    with pytest.raises(ScopeNotImplemented):
        _import_into(repo, "qoder-cn")


def _import_into(repo: AccountRepository, scope: str):
    return repo.upsert(
        scope=scope,
        external_id="u-9",
        display_name="账号",
        credential={"access_token": "x"},
    )


# ---------- 判重 ----------


def test_import_creates_then_updates(repo):
    first = _import(repo)
    assert first.created is True

    second = _import(repo, name="账号甲改名", token="tok-new-value")
    assert second.created is False
    assert second.account.display_name == "账号甲改名"
    assert len(repo.list_accounts(SCOPE)) == 1


def test_dedup_is_by_external_id_not_display_name(repo):
    _import(repo, external_id="u-1", name="同名")
    _import(repo, external_id="u-2", name="同名")
    assert len(repo.list_accounts(SCOPE)) == 2


def test_same_external_id_in_different_scope_is_separate(repo):
    _import(repo)
    repo.upsert(
        scope="trae-cn",
        external_id="u-1",
        display_name="账号甲",
        credential={"access_token": "y"},
    )
    assert len(repo.list_accounts(SCOPE)) == 1
    assert len(repo.list_accounts("trae-cn")) == 1


def test_import_restores_soft_deleted_account(repo):
    result = _import(repo)
    repo.soft_delete(result.account.id)

    again = _import(repo)
    assert again.created is False
    assert again.restored is True
    assert again.account.is_deleted is False
    assert len(repo.list_accounts(SCOPE)) == 1


# ---------- 凭据 ----------


def test_credential_is_not_stored_in_plaintext(conn, repo):
    _import(repo)
    row = conn.execute("SELECT sealed FROM credentials").fetchone()
    blob = row["sealed"]
    assert b"tok-secret-value" not in blob
    assert b"refresh-secret" not in blob


def test_credential_roundtrip(repo):
    result = _import(repo)
    data = repo.current_credential(result.account.id)
    assert data["access_token"] == "tok-secret-value"


def test_previous_credential_marked_not_current(repo):
    result = _import(repo, token="old")
    _import(repo, token="new")
    data = repo.current_credential(result.account.id)
    assert data["access_token"] == "new"


def test_import_marks_state_unverified(repo):
    result = _import(repo)
    assert result.account.credential_state is CredentialState.UNVERIFIED


def test_log_redaction_hides_full_value():
    assert "secret-value" not in redact("tok-secret-value")


# ---------- 软删除与回收站 ----------


def test_soft_delete_moves_to_recycle_bin(conn, repo):
    result = _import(repo)
    repo.soft_delete(result.account.id)

    assert repo.list_accounts(SCOPE) == []
    only_row = conn.execute(
        "SELECT deleted_at FROM accounts WHERE id = ?", (result.account.id,)
    ).fetchone()
    assert only_row["deleted_at"] is not None


def test_restore_brings_account_back(repo):
    result = _import(repo)
    repo.soft_delete(result.account.id)
    restored = repo.restore(result.account.id)
    assert restored.is_deleted is False
    assert len(repo.list_accounts(SCOPE)) == 1


def test_delete_twice_raises(repo):
    result = _import(repo)
    repo.soft_delete(result.account.id)
    with pytest.raises(AccountNotFound):
        repo.soft_delete(result.account.id)


def test_purge_expired_only_removes_old_entries(conn, repo):
    old = _import(repo, external_id="u-old")
    fresh = _import(repo, external_id="u-new")
    repo.soft_delete(old.account.id)
    repo.soft_delete(fresh.account.id)

    conn.execute(
        "UPDATE accounts SET deleted_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
        (old.account.id,),
    )
    removed = repo.purge_expired(days=30)
    assert removed == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"] == 1


# ---------- 业务规则 ----------


def test_cannot_delete_account_in_use(conn, repo):
    class BusyProbe:
        def is_in_use(self, scope, external_id):
            return True

        def is_switching(self, scope):
            return False

    service = AccountService(repo, BusyProbe())
    result = _import(repo)
    with pytest.raises(AccountInUseError):
        service.delete(result.account.id)
    assert len(repo.list_accounts(SCOPE)) == 1


def test_summary_counts_states(repo):
    a = _import(repo, external_id="u-1")
    _import(repo, external_id="u-2")
    repo.set_credential_state(a.account.id, CredentialState.USABLE)

    summary = AccountService(repo).summary(SCOPE)
    assert summary.total == 2
    assert summary.usable == 1


# ---------- 持久化 ----------


def test_data_survives_reopen(tmp_path):
    path = tmp_path / "persist.db"
    first = open_database(path)
    repo = AccountRepository(first, InMemoryBox())
    _import(repo)
    first.close()

    second = open_database(path)
    reopened = AccountRepository(second, InMemoryBox())
    assert len(reopened.list_accounts(SCOPE)) == 1
    second.close()


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "migrate.db"
    conn = open_database(path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version >= 1
    open_database(path).close()
    conn.close()
