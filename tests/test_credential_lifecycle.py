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
    _refresh_window,
    expiry_text,
    refreshable,
    token_expiry,
)
from chaierww.domain.credential_refresh import (
    STATUS_REFRESHED,
    STATUS_RELOGIN_REQUIRED,
    STATUS_TRANSIENT,
    STATUS_UNSUPPORTED,
    RefreshOutcome,
    TraeRefresher,
    _outcome_from_response,
)
from chaierww.domain.scheduler import _default_tasks
from chaierww.domain.trae_crypto import DeviceKeyPair
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


def _jwt_with_iat(exp: int, iat: int) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp, "iat": iat, "userId": "u-1"}).encode()
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


def test_opaque_raw_file_envelope_has_no_expiry():
    raw = {"format": "raw-file", "encoding": "base64", "data": "eyJ9"}
    assert token_expiry("session", raw) is None
    assert expiry_text("session", raw) is None


def _trae_raw(*, expired_at: str, refresh_token: str = "rt-old", device_id: str = "dev-1"):
    """造一份 Trae 客户端信封（cloudide + 设备 key），用于过期/续期判定测试。"""
    from chaierww.domain.trae_crypto import tc_encrypt

    cloudide = {
        "token": "jwt-old",
        "refreshToken": refresh_token,
        "expiredAt": expired_at,
        "userId": "u-1",
        "host": "https://api.trae.cn",
    }
    auth = {
        f"iCubeAuthInfo://icube-dc:{device_id}": tc_encrypt(
            json.dumps({"publicKeyPEM": "pub", "privateKeyPEM": "priv"})
        ),
        "iCubeAuthInfo://icube.cloudide": tc_encrypt(json.dumps(cloudide)),
    }
    return {
        "format": "raw-file",
        "encoding": "base64",
        "data": base64.b64encode(json.dumps(auth).encode()).decode(),
    }


def test_trae_raw_file_expiry_and_refreshable():
    raw = _trae_raw(expired_at="2030-01-01T00:00:00.000Z")
    moment = token_expiry("session", raw)
    assert moment is not None and moment.year == 2030
    assert expiry_text("session", raw) == "2030-01-01T00:00:00+00:00"
    # 信封能解出 refreshToken → 可自动续期（设备私钥也在信封里）。
    assert refreshable("session", raw) is True


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


def test_trae_repair_device_keys_merges_only_matching_device(monkeypatch):
    """缺私钥的老扫码凭据按 deviceId 匹配本机后补齐，其余情况一概不动。"""
    local = DeviceKeyPair("dev-1", "m-1", "pub-pem", "priv-pem")
    monkeypatch.setattr(
        "chaierww.domain.credential_refresh._local_device_keypair", lambda: local
    )
    base = {
        "format": "oauth-token",
        "deviceId": "dev-1",
        "refreshToken": "r-1",
        "accessToken": "a-1",
    }
    repaired = TraeRefresher().repair_device_keys(base)
    assert repaired is not None
    assert repaired["devicePrivateKey"] == "priv-pem"
    assert repaired["devicePublicKey"] == "pub-pem"
    assert repaired["deviceMachineId"] == "m-1"
    assert "devicePrivateKey" not in base
    assert TraeRefresher().repair_device_keys({**base, "deviceId": "dev-2"}) is None
    # 旧导出包常常 deviceId 为空：按本机兜底补齐，并把本机 deviceId 落库。
    missing_id = {key: value for key, value in base.items() if key != "deviceId"}
    adopted = TraeRefresher().repair_device_keys(missing_id)
    assert adopted is not None
    assert adopted["deviceId"] == "dev-1"
    assert adopted["devicePrivateKey"] == "priv-pem"
    assert (
        TraeRefresher().repair_device_keys(
            {**base, "devicePrivateKey": "p", "devicePublicKey": "q"}
        )
        is None
    )
    assert TraeRefresher().repair_device_keys({**base, "refreshToken": ""}) is None


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


class _RepairingRefresher(_StubRefresher):
    """模拟 Trae 续期器：续期前先做设备密钥补齐。"""

    def __init__(self, outcome: RefreshOutcome, keys: dict[str, str]) -> None:
        super().__init__(outcome)
        self.keys = keys
        self.seen: list[dict] = []

    def repair_device_keys(self, credential):
        return {**credential, **self.keys}

    def refresh(self, credential):
        self.seen.append(credential)
        return super().refresh(credential)


def test_refresh_repairs_device_keys_before_asking_upstream(repo, monkeypatch):
    """补齐结果要落库，而且这次续期就用补齐后的凭据（不能只在内存里兜底）。"""
    account = _oauth_account(repo, scope=TRAE_SCOPE, external_id="t-1")
    stub = _RepairingRefresher(
        RefreshOutcome(status=STATUS_TRANSIENT, detail="网络超时"),
        {"devicePrivateKey": "priv", "devicePublicKey": "pub"},
    )
    monkeypatch.setattr(
        "chaierww.domain.credential_lifecycle.refresher_for", lambda platform: stub
    )
    results = _service(repo).sweep([account], mode=MODE_FORCE)
    assert results[0].status == RESULT_FAILED
    _kind, credential = repo.credential_record(account.id)
    assert credential["devicePrivateKey"] == "priv"
    assert credential["devicePublicKey"] == "pub"
    assert stub.seen[0]["devicePrivateKey"] == "priv"


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


def test_expired_account_without_refresh_token_marks_relogin(repo):
    """已过期又没有 refreshToken：不能再显示可用，必须如实降级为需重登。"""
    expired = (datetime.now(UTC) - timedelta(hours=2)).replace(microsecond=0).isoformat()
    credential = {"format": "oauth-token", "uid": "u-1", "accessToken": "jwt"}
    account = repo.upsert(
        scope=SCOPE,
        external_id="u-1",
        display_name="过期号",
        credential=credential,
        credential_kind="oauth_token",
        expires_at=expired,
    ).account
    results = _service(repo).sweep([account], mode=MODE_DUE)
    assert results[0].status == RESULT_RELOGIN_REQUIRED
    assert "已过期" in results[0].detail
    assert repo.get(account.id).credential_state is CredentialState.RELOGIN_REQUIRED


def test_expired_account_with_unsupported_endpoint_marks_relogin(repo, monkeypatch):
    """续期端点不支持 + 已过期：账号实际已不可用，同样要降级。"""
    expired = (datetime.now(UTC) - timedelta(hours=1)).replace(microsecond=0).isoformat()
    account = _oauth_account(repo, expires_at=expired)
    stub = _StubRefresher(RefreshOutcome(status=STATUS_UNSUPPORTED, detail="端点不存在"))
    monkeypatch.setattr(
        "chaierww.domain.credential_lifecycle.refresher_for", lambda platform: stub
    )
    results = _service(repo).sweep([account], mode=MODE_DUE)
    assert results[0].status == RESULT_RELOGIN_REQUIRED
    assert repo.get(account.id).credential_state is CredentialState.RELOGIN_REQUIRED


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


def test_trae_error_code_is_exposed_on_rejection():
    from chaierww.domain.credential_refresh import TraeRefresher

    resp = httpx.Response(
        200,
        json={
            "ResponseMetadata": {
                "Error": {"Code": "20403", "Message": "Token device not match"}
            }
        },
    )
    outcome = TraeRefresher()._interpret(resp, {}, endpoint="/x")
    assert outcome.status == STATUS_RELOGIN_REQUIRED
    assert outcome.error_code == "20403"
    assert outcome.detail == "Token device not match"


def test_probe_record_persists_detail_and_refresh_time(repo):
    account = _oauth_account(repo)
    repo.record_probe(
        account.id,
        STATUS_RELOGIN_REQUIRED,
        failed=True,
        detail="20101 refresh token is invalid",
        error_code="20101",
    )
    record = repo.probe_records([account.id])[account.id]
    assert record["detail"] == "20101 refresh token is invalid"
    assert record["lastCode"] == "20101"
    # 失败不能伪装成「刚续期过」。
    assert record["lastRefreshAt"] is None

    repo.record_probe(account.id, STATUS_REFRESHED, refreshed=True, detail="已自动续期")
    record = repo.probe_records([account.id])[account.id]
    assert record["lastRefreshAt"] is not None
    assert record["failures"] == 0


def test_sweep_records_upstream_error_code(repo, monkeypatch):
    account = _oauth_account(repo)
    stub = _StubRefresher(
        RefreshOutcome(
            status=STATUS_RELOGIN_REQUIRED,
            detail="Token device not match",
            error_code="20403",
        )
    )
    monkeypatch.setattr(
        "chaierww.domain.credential_lifecycle.refresher_for", lambda platform: stub
    )
    results = _service(repo).sweep([account], mode=MODE_FORCE)
    assert results[0].status == RESULT_RELOGIN_REQUIRED
    assert results[0].error_code == "20403"

    record = repo.probe_records([account.id])[account.id]
    assert record["lastCode"] == "20403"
    assert record["detail"] == "Token device not match"


# ---------- 调度 ----------


def test_refresh_window_scales_with_lifetime():
    """提前窗口 = min(6h, 寿命 × 10%)，下限 2 小时。"""
    assert _refresh_window(None) == timedelta(hours=2)
    # 2h 寿命按 10% 只有 12 分钟，抬到下限 2 小时。
    assert _refresh_window(timedelta(hours=2)) == timedelta(hours=2)
    assert _refresh_window(timedelta(hours=30)) == timedelta(hours=3)
    # 100h 寿命按 10% 是 10h，压到上限 6h。
    assert _refresh_window(timedelta(hours=100)) == timedelta(hours=6)


def test_due_window_scales_with_token_lifetime(repo, monkeypatch):
    now = datetime.now(UTC).replace(microsecond=0)
    issued = int((now - timedelta(hours=1)).timestamp())
    far = _oauth_account(
        repo,
        external_id="far",
        name="远期号",
        access_token=_jwt_with_iat(int((now + timedelta(hours=29)).timestamp()), issued),
    )
    near = _oauth_account(
        repo,
        external_id="near",
        name="临近号",
        access_token=_jwt_with_iat(
            int((now + timedelta(hours=2)).timestamp()),
            int((now - timedelta(hours=28)).timestamp()),
        ),
    )
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
    service = CredentialLifecycleService(repo, gap_seconds=0.0, now=lambda: now)
    monkeypatch.setattr(
        service, "_verify", lambda target, credential: (RESULT_OK, "登录态有效")
    )
    results = service.sweep([far, near], mode=MODE_DUE)
    # 30h 寿命 → 窗口 3h：剩 29h 不动，剩 2h 才续。
    assert results[0].status == RESULT_NOOP
    assert results[1].status == RESULT_REFRESHED
    assert stub.calls == 1


def test_failed_probe_backs_off_then_retries(repo, monkeypatch):
    """第 1 次失败后退避 1 小时；手动 force 不受退避影响。"""
    account = _oauth_account(
        repo,
        expires_at=(datetime.now(UTC) + timedelta(minutes=10)).replace(microsecond=0).isoformat(),
    )
    repo.record_probe(
        account.id, STATUS_RELOGIN_REQUIRED, failed=True, detail="拒绝", error_code="20101"
    )
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
    monkeypatch.setattr(
        service, "_verify", lambda target, credential: (RESULT_OK, "登录态有效")
    )

    blocked = service.sweep([account], mode=MODE_DUE)
    assert blocked[0].status == RESULT_NOOP
    assert "后再试" in blocked[0].detail
    assert stub.calls == 0

    forced = service.sweep([account], mode=MODE_FORCE)
    assert forced[0].status == RESULT_REFRESHED
    assert stub.calls == 1
    assert repo.probe_failures([account.id]).get(account.id, 0) == 0


def test_backoff_expires_after_wait(repo, monkeypatch):
    """退避窗口过去之后，自动巡检会重新打上游。"""
    account = _oauth_account(
        repo,
        external_id="u-2",
        name="账号乙",
        expires_at=(datetime.now(UTC) + timedelta(minutes=10)).replace(microsecond=0).isoformat(),
    )
    repo.record_probe(account.id, STATUS_RELOGIN_REQUIRED, failed=True, detail="拒绝")
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
    later = CredentialLifecycleService(
        repo, gap_seconds=0.0, now=lambda: datetime.now(UTC) + timedelta(hours=2)
    )
    monkeypatch.setattr(
        later, "_verify", lambda target, credential: (RESULT_OK, "登录态有效")
    )
    results = later.sweep([account], mode=MODE_DUE)
    assert results[0].status == RESULT_REFRESHED
    assert stub.calls == 1


# ---------- 巡检前对账 ----------


def test_reconcile_runs_before_sweep_but_not_in_check_mode(repo, monkeypatch):
    account = _oauth_account(repo)
    seen: list[list[str]] = []
    service = CredentialLifecycleService(
        repo, gap_seconds=0.0, reconcile=lambda accounts: seen.append([a.id for a in accounts])
    )
    monkeypatch.setattr(
        service, "_verify", lambda target, credential: (RESULT_OK, "登录态有效")
    )
    # check 是只读验活，不该顺手改凭据。
    service.sweep([account], mode=MODE_CHECK)
    assert seen == []

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
    service.sweep([account], mode=MODE_FORCE)
    # 对账拿到的是本次要巡检的号，不是整个号池。
    assert seen == [[account.id]]


def test_reconcile_failure_does_not_block_sweep(repo, monkeypatch):
    account = _oauth_account(repo)

    def boom(_accounts):
        raise RuntimeError("客户端目录不可读")

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
    service = CredentialLifecycleService(repo, gap_seconds=0.0, reconcile=boom)
    monkeypatch.setattr(
        service, "_verify", lambda target, credential: (RESULT_OK, "登录态有效")
    )
    results = service.sweep([account], mode=MODE_FORCE)
    assert results[0].status == RESULT_REFRESHED
    assert stub.calls == 1


def test_credential_refresh_task_is_on_by_default():
    spec = _default_tasks()["credential_refresh"]
    assert spec.enabled is True
    assert spec.times == ("08:00", "12:00", "16:00", "20:00")


# ---------- 巡检历史 / 退避时间 / 手动作废 ----------


def test_probe_log_trims_to_twenty_and_returns_newest_first(repo):
    """每号只留最近 20 条历史，读出来新的在前。"""
    account = _oauth_account(repo)
    for index in range(25):
        repo.record_probe(account.id, "ok", detail=f"第 {index} 次")

    items = repo.probe_history(account.id)
    assert len(items) == 20
    assert items[0]["detail"] == "第 24 次"
    assert items[-1]["detail"] == "第 5 次"


def test_probe_next_retry_at_is_persisted_and_surfaced(repo):
    account = _oauth_account(repo)
    retry = "2026-11-03T00:00:00+00:00"
    repo.record_probe(account.id, STATUS_TRANSIENT, failed=True, next_retry_at=retry)

    assert repo.probe_records([account.id])[account.id]["nextRetryAt"] == retry
    assert repo.probe_history(account.id)[0]["nextRetryAt"] == retry


def test_transient_failure_returns_next_retry_time(repo, monkeypatch):
    """上游临时故障：结果里要带「下次重试」，并落进汇总供卡片展示。"""
    expires = (datetime.now(UTC) + timedelta(minutes=10)).replace(microsecond=0).isoformat()
    account = _oauth_account(repo, expires_at=expires)
    stub = _StubRefresher(
        RefreshOutcome(status=STATUS_TRANSIENT, detail="网络抖动", retry_after=600)
    )
    monkeypatch.setattr(
        "chaierww.domain.credential_lifecycle.refresher_for", lambda platform: stub
    )
    results = _service(repo).sweep([account], mode=MODE_DUE)

    assert results[0].status == RESULT_FAILED
    assert results[0].next_retry_at is not None
    assert repo.probe_records([account.id])[account.id]["nextRetryAt"] == (
        results[0].next_retry_at
    )


def test_clear_failures_resets_breaker_but_keeps_history(repo, monkeypatch):
    """手动解除熔断：计数清零、状态回未验证，历史时间线仍保留。"""
    account = _oauth_account(repo)
    stub = _StubRefresher(RefreshOutcome(status=STATUS_RELOGIN_REQUIRED, detail="拒绝"))
    monkeypatch.setattr(
        "chaierww.domain.credential_lifecycle.refresher_for", lambda platform: stub
    )
    service = _service(repo)
    for _ in range(3):
        service.sweep([account], mode=MODE_FORCE)

    assert repo.probe_failures([account.id])[account.id] == 3
    assert repo.get(account.id).credential_state is CredentialState.RELOGIN_REQUIRED
    assert repo.probe_history(account.id)

    service.clear_failures(account.id)
    assert repo.probe_failures([account.id]).get(account.id, 0) == 0
    assert repo.get(account.id).credential_state is CredentialState.UNVERIFIED
    # 历史不清：用户仍能回看「什么时候失败过」。
    assert len(repo.probe_history(account.id)) == 3


def test_diagnose_reports_six_steps_and_writes_no_probe(repo, monkeypatch):
    """诊断：六步按固定顺序给出结论，且不写任何巡检记录。"""
    expires = (datetime.now(UTC) + timedelta(hours=2)).replace(microsecond=0).isoformat()
    account = _oauth_account(repo, expires_at=expires)
    repo.record_probe(account.id, STATUS_RELOGIN_REQUIRED, failed=True, detail="拒绝")
    before = repo.probe_history(account.id)

    service = _service(repo)
    monkeypatch.setattr(
        service, "_verify", lambda target, credential: (RESULT_OK, "登录态有效")
    )
    report = service.diagnose(repo.get(account.id))

    assert [step["key"] for step in report["steps"]] == [
        "readable",
        "refreshable",
        "device",
        "expiry",
        "backoff",
        "verify",
    ]
    assert all(isinstance(step["ok"], bool) for step in report["steps"])
    assert report["conclusion"]
    assert report["suggest"]
    # 诊断只读：历史一条都不能多。
    assert repo.probe_history(account.id) == before
