from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from chaierww.domain.accounts.models import CredentialState
from chaierww.domain.accounts.repository import AccountRepository
from chaierww.domain.credential_lifecycle import (
    MODE_CHECK,
    MODE_DUE,
    MODE_FORCE,
    RESULT_FAILED,
    RESULT_NOOP,
    RESULT_OK,
    RESULT_REFRESHED,
    RESULT_RELOGIN_REQUIRED,
    RESULT_UNSUPPORTED,
    CredentialLifecycleService,
    expiry_text,
    refreshable,
    token_expiry,
)
from chaierww.domain.credential_refresh import (
    STATUS_REFRESHED,
    STATUS_RELOGIN_REQUIRED,
    STATUS_TRANSIENT,
    RefreshOutcome,
    _outcome_from_response,
)
from chaierww.domain.scheduler import _default_tasks
from chaierww.infra.db import open_database
from chaierww.infra.secrets import InMemoryBox

SCOPE = "workbuddy-cn"
TRAE_SCOPE = "trae-cn"


@pytest.fixture
def repo(tmp_path):
    connection = open_database(tmp_path / "test.db")
    yield AccountRepository(connection, InMemoryBox())
    connection.close()


def _jwt(exp: int) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp, "userId": "u-1"}).encode()
    ).decode()
    return f"header.{payload}.signature"


def _oauth_account(
    repo: AccountRepository,
    *,
    scope: str = SCOPE,
    external_id: str = "u-1",
    name: str = "账号甲",
    expires_at: str | None = None,
    refresh_token: str = "refresh-secret",
    access_token: str = "access-secret",
):
    credential = {
        "format": "oauth-token",
        "uid": external_id,
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "domain": "www.workbuddy.cn",
    }
    return repo.upsert(
        scope=scope,
        external_id=external_id,
        display_name=name,
        credential=credential,
        credential_kind="oauth_token",
        expires_at=expires_at,
    ).account


def _raw_account(repo: AccountRepository):
    return repo.upsert(
        scope=SCOPE,
        external_id="raw-1",
        display_name="信封号",
        credential={"format": "raw-file", "encoding": "base64", "data": "eyJ9"},
        credential_kind="session",
    ).account


# ---------- 过期解析 ----------


def test_trae_expiry_comes_from_jwt_exp():
    exp = int((datetime.now(UTC) + timedelta(hours=2)).timestamp())
    credential = {"format": "oauth-token", "accessToken": _jwt(exp)}
    moment = token_expiry("oauth_token", credential)
    assert moment is not None
    assert int(moment.timestamp()) == exp


def test_workbuddy_expiry_accepts_seconds_millis_and_iso():
    moment = token_expiry("oauth_token", {"format": "oauth-token", "expiresAt": 1_700_000_000})
    assert moment is not None and int(moment.timestamp()) == 1_700_000_000

    millis = token_expiry(
        "oauth_token", {"format": "oauth-token", "expiresAt": 1_700_000_000_000}
    )
    assert millis is not None and int(millis.timestamp()) == 1_700_000_000

    iso = token_expiry(
        "oauth_token", {"format": "oauth-token", "expires_at": "2030-01-01T00:00:00+00:00"}
    )
    assert iso is not None and iso.year == 2030


def test_raw_file_envelope_has_no_expiry():
    raw = {"format": "raw-file", "encoding": "base64", "data": "eyJ9"}
    assert token_expiry("session", raw) is None
    assert expiry_text("session", raw) is None


def test_refreshable_requires_oauth_and_refresh_token():
    assert refreshable("oauth_token", {"format": "oauth-token", "refreshToken": "r"}) is True
    assert refreshable("oauth_token", {"format": "oauth-token"}) is False
    assert refreshable("session", {"format": "oauth-token", "refreshToken": "r"}) is False
    assert (
        refreshable("oauth_token", {"format": "raw-file", "refreshToken": "r"}) is False
    )


# ---------- 响应分类 ----------


def test_wb_waf_403_is_transient_not_relogin():
    resp = httpx.Response(403, json={"code": 10085, "msg": "forbidden"})
    outcome = _outcome_from_response(resp, {}, endpoint="/x")
    assert outcome.status == STATUS_TRANSIENT


def test_401_is_relogin_required():
    resp = httpx.Response(401, json={"message": "invalid_grant"})
    outcome = _outcome_from_response(resp, {}, endpoint="/x")
    assert outcome.status == STATUS_RELOGIN_REQUIRED


def test_404_is_unsupported():
    resp = httpx.Response(404, json={"message": "not found"})
    outcome = _outcome_from_response(resp, {}, endpoint="/x")
    assert outcome.status == "unsupported"


def test_200_without_token_is_unsupported():
    resp = httpx.Response(200, json={"ok": True})
    outcome = _outcome_from_response(resp, {}, endpoint="/x")
    assert outcome.status == "unsupported"


def test_200_keeps_old_refresh_token_when_response_omits_it():
    old = {"format": "oauth-token", "refreshToken": "old-refresh", "uid": "u-1"}
    resp = httpx.Response(
        200,
        json={"data": {"accessToken": "new-access", "expiresIn": 3600}},
    )
    outcome = _outcome_from_response(resp, old, endpoint="/x")
    assert outcome.status == STATUS_REFRESHED
    assert outcome.credential is not None
    assert outcome.credential["accessToken"] == "new-access"
    assert outcome.credential["refreshToken"] == "old-refresh"
    assert outcome.credential["uid"] == "u-1"
    assert outcome.expires_at is not None


# ---------- 仓储 ----------


def test_replace_credential_rotates_rows_without_touching_identity(repo):
    account = _oauth_account(repo)
    repo.replace_credential(
        account.id,
        credential={"format": "oauth-token", "accessToken": "new", "refreshToken": "r2"},
        kind="oauth_token",
        expires_at="2030-01-01T00:00:00+00:00",
    )
    kind, credential = repo.credential_record(account.id)
    assert kind == "oauth_token"
    assert credential["accessToken"] == "new"
    fresh = repo.get(account.id)
    assert fresh.display_name == account.display_name
    assert fresh.deleted_at is None
    assert repo.credential_expiries([account.id])[account.id] == "2030-01-01T00:00:00+00:00"

    # created_at 是秒级时间戳，同秒内插入的两行无法用时间排序，按写入顺序（rowid）断言。
    rows = repo._conn.execute(
        "SELECT is_current FROM credentials WHERE account_id = ? ORDER BY rowid",
        (account.id,),
    ).fetchall()
    assert [row["is_current"] for row in rows] == [0, 1]


# ---------- 服务：due / force / check ----------


class _StubRefresher:
    def __init__(self, outcome: RefreshOutcome) -> None:
        self.outcome = outcome
        self.calls = 0

    def refresh(self, credential):
        self.calls += 1
        return self.outcome


def _service(repo: AccountRepository) -> CredentialLifecycleService:
    return CredentialLifecycleService(repo, gap_seconds=0.0)


def test_due_mode_blind_refreshes_when_expiry_is_unknown(repo, monkeypatch):
    account = _oauth_account(repo)
    stub = _StubRefresher(
        RefreshOutcome(
            status=STATUS_REFRESHED,
            credential={"format": "oauth-token", "accessToken": "new", "refreshToken": "r2"},
            expires_at=None,
        )
    )
    monkeypatch.setattr(
        "chaierww.domain.credential_lifecycle.refresher_for", lambda platform: stub
    )
    service = _service(repo)
    monkeypatch.setattr(service, "_verify", lambda target, credential: (RESULT_OK, "登录态有效"))
    results = service.sweep([account], mode=MODE_DUE)
    # 过期时间未知不再永久跳过：第一个轮次会盲续期一次。
    assert results[0].status == RESULT_REFRESHED
    assert stub.calls == 1
    # 12 小时节流内再巡检不再打上游。
    second = service.sweep([account], mode=MODE_DUE)
    assert second[0].status == RESULT_NOOP
    assert stub.calls == 1


def test_due_mode_skips_when_far_from_expiry(repo, monkeypatch):
    expires = (datetime.now(UTC) + timedelta(hours=5)).replace(microsecond=0).isoformat()
    account = _oauth_account(repo, expires_at=expires)
    stub = _StubRefresher(
        RefreshOutcome(status=STATUS_REFRESHED, credential={}, expires_at=None)
    )
    monkeypatch.setattr(
        "chaierww.domain.credential_lifecycle.refresher_for", lambda platform: stub
    )
    results = _service(repo).sweep([account], mode=MODE_DUE)
    assert results[0].status == RESULT_NOOP
    assert stub.calls == 0


def test_due_mode_refreshes_inside_window(repo, monkeypatch):
    expires = (datetime.now(UTC) + timedelta(minutes=10)).replace(microsecond=0).isoformat()
    account = _oauth_account(repo, expires_at=expires)
    new_expiry = (datetime.now(UTC) + timedelta(hours=2)).replace(microsecond=0).isoformat()
    stub = _StubRefresher(
        RefreshOutcome(
            status=STATUS_REFRESHED,
            credential={
                "format": "oauth-token",
                "accessToken": "new-access",
                "refreshToken": "new-refresh",
                "uid": "u-1",
            },
            expires_at=new_expiry,
        )
    )
    monkeypatch.setattr(
        "chaierww.domain.credential_lifecycle.refresher_for", lambda platform: stub
    )
    service = _service(repo)
    monkeypatch.setattr(service, "_verify", lambda target, credential: (RESULT_OK, "登录态有效"))
    results = service.sweep([account], mode=MODE_DUE)
    assert results[0].status == RESULT_REFRESHED
    assert stub.calls == 1
    _kind, credential = repo.credential_record(account.id)
    assert credential["accessToken"] == "new-access"
    assert repo.get(account.id).credential_state is CredentialState.USABLE
    assert repo.credential_expiries([account.id])[account.id] == new_expiry


def test_force_mode_refreshes_even_without_expiry(repo, monkeypatch):
    account = _oauth_account(repo)
    stub = _StubRefresher(
        RefreshOutcome(
            status=STATUS_REFRESHED,
            credential={"format": "oauth-token", "accessToken": "new"},
            expires_at=None,
        )
    )
    monkeypatch.setattr(
        "chaierww.domain.credential_lifecycle.refresher_for", lambda platform: stub
    )
    service = _service(repo)
    monkeypatch.setattr(service, "_verify", lambda target, credential: (RESULT_OK, "登录态有效"))
    results = service.sweep([account], mode=MODE_FORCE)
    assert results[0].status == RESULT_REFRESHED
    assert stub.calls == 1


def test_auth_rejection_marks_relogin(repo, monkeypatch):
    account = _oauth_account(repo)
    stub = _StubRefresher(RefreshOutcome(status=STATUS_RELOGIN_REQUIRED, detail="拒绝"))
    monkeypatch.setattr(
        "chaierww.domain.credential_lifecycle.refresher_for", lambda platform: stub
    )
    results = _service(repo).sweep([account], mode=MODE_FORCE)
    assert results[0].status == RESULT_RELOGIN_REQUIRED
    assert repo.get(account.id).credential_state is CredentialState.RELOGIN_REQUIRED


def test_transient_failure_keeps_current_state(repo, monkeypatch):
    account = _oauth_account(repo)
    repo.set_credential_state(account.id, CredentialState.USABLE)
    stub = _StubRefresher(RefreshOutcome(status=STATUS_TRANSIENT, detail="网络"))
    monkeypatch.setattr(
        "chaierww.domain.credential_lifecycle.refresher_for", lambda platform: stub
    )
    results = _service(repo).sweep([account], mode=MODE_FORCE)
    assert results[0].status == RESULT_FAILED
    assert repo.get(account.id).credential_state is CredentialState.USABLE


def test_raw_file_account_is_unsupported_not_failed(repo):
    account = _raw_account(repo)
    results = _service(repo).sweep([account], mode=MODE_FORCE)
    assert results[0].status == RESULT_UNSUPPORTED
    assert "信封" in results[0].detail


def test_check_mode_marks_usable_and_caches(repo, monkeypatch):
    account = _oauth_account(repo)
    service = _service(repo)
    calls = {"n": 0}

    def fake_verify(target, credential):
        calls["n"] += 1
        repo.set_credential_state(target.id, CredentialState.USABLE)
        return RESULT_OK, "登录态有效"

    monkeypatch.setattr(service, "_verify", fake_verify)
    first = service.sweep([account], mode=MODE_CHECK)
    second = service.sweep([account], mode=MODE_CHECK)
    assert first[0].status == RESULT_OK
    assert second[0].status == RESULT_OK
    # 5 分钟缓存内第二次不再打上游。
    assert calls["n"] == 1


def test_check_mode_401_marks_relogin(repo, monkeypatch):
    account = _oauth_account(repo)

    class _Resp:
        status_code = 401

        @staticmethod
        def json():
            return {"code": 401}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return _Resp()

    service = _service(repo)
    monkeypatch.setattr(
        "chaierww.domain.credential_lifecycle.httpx.Client", lambda **kwargs: _Client()
    )
    results = service.sweep([account], mode=MODE_CHECK)
    assert results[0].status == RESULT_RELOGIN_REQUIRED
    assert repo.get(account.id).credential_state is CredentialState.RELOGIN_REQUIRED


def test_summary_counts_each_outcome(repo, monkeypatch):
    due = _oauth_account(
        repo,
        external_id="due",
        name="到期号",
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )
    far = _oauth_account(
        repo,
        external_id="far",
        name="远期号",
        expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
    )
    stub = _StubRefresher(
        RefreshOutcome(status=STATUS_REFRESHED, credential={"format": "oauth-token"})
    )
    monkeypatch.setattr(
        "chaierww.domain.credential_lifecycle.refresher_for", lambda platform: stub
    )
    service = _service(repo)
    monkeypatch.setattr(service, "_verify", lambda target, credential: (RESULT_OK, "登录态有效"))
    results = service.sweep([due, far], mode=MODE_DUE)
    summary = service.summary(results)
    assert summary["total"] == 2
    assert summary["refreshed"] == 1
    assert summary["noop"] == 1


def test_consecutive_auth_failures_trip_the_breaker(repo, monkeypatch):
    """连续 3 次认证失败后停止自动续期；手动 force 仍然可以再试。"""
    account = _oauth_account(repo)
    stub = _StubRefresher(RefreshOutcome(status=STATUS_RELOGIN_REQUIRED, detail="拒绝"))
    monkeypatch.setattr(
        "chaierww.domain.credential_lifecycle.refresher_for", lambda platform: stub
    )
    service = _service(repo)
    for _ in range(3):
        assert service.sweep([account], mode=MODE_FORCE)[0].status == RESULT_RELOGIN_REQUIRED
    assert repo.probe_failures([account.id])[account.id] == 3

    stub.calls = 0
    results = service.sweep([account], mode=MODE_DUE)
    # 熔断生效：自动续期不再打上游。
    assert results[0].status == RESULT_RELOGIN_REQUIRED
    assert stub.calls == 0
    assert "暂停自动续期" in results[0].detail

    # 手动强制续期不受熔断限制。
    service.sweep([account], mode=MODE_FORCE)
    assert stub.calls == 1


def test_new_login_resets_probe_failures(repo, monkeypatch):
    account = _oauth_account(repo)
    repo.record_probe(account.id, STATUS_RELOGIN_REQUIRED, failed=True)
    repo.record_probe(account.id, STATUS_RELOGIN_REQUIRED, failed=True)
    assert repo.probe_failures([account.id])[account.id] == 2
    # 重新扫码 = 新凭据，失败计数清零。
    _oauth_account(repo, external_id="u-1", name="账号甲")
    assert repo.probe_failures([account.id]).get(account.id, 0) == 0


def test_expiry_from_issued_at_and_expires_in():
    issued = "2026-10-20T00:00:00+00:00"
    moment = token_expiry(
        "oauth_token", {"format": "oauth-token", "issuedAt": issued, "expiresIn": 3600}
    )
    assert moment is not None
    assert moment.isoformat().startswith("2026-10-20T01:00:00")


def test_expiry_from_nested_absolute_field():
    credential = {
        "format": "oauth-token",
        "auth": {"token": {"expiresAt": "2030-01-01T00:00:00+00:00"}},
    }
    moment = token_expiry("oauth_token", credential)
    assert moment is not None and moment.year == 2030


# ---------- 调度 ----------


def test_credential_refresh_task_is_on_by_default():
    spec = _default_tasks()["credential_refresh"]
    assert spec.enabled is True
    assert spec.times == ("08:00", "20:00")
