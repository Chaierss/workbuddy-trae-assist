from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx

from chaierww.domain.accounts.repository import AccountRepository
from chaierww.domain.activities import (
    RESULT_FAILED,
    RESULT_UNKNOWN,
    ActivitiesService,
    RunResult,
    _root_key,
    _summarize_results,
)
from chaierww.domain.compat import (
    COMPAT_CHANGED,
    COMPAT_COMPATIBLE,
    COMPAT_MISSING,
    ClientProbe,
    CompatService,
    fingerprint_of,
    structure_paths,
)
from chaierww.domain.login_health import (
    LOGIN_INVALID,
    LOGIN_UNKNOWN,
    LOGIN_VALID,
    expiring_soon,
    probe_trae_login,
    probe_workbuddy_login,
)
from chaierww.domain.pool_health import PoolHealthService, SupplyThresholds, credits_days
from chaierww.domain.preferences import Preferences
from chaierww.infra.db import open_database
from chaierww.infra.secrets import InMemoryBox

# ---------- A. 结构指纹 ----------


def test_fingerprint_ignores_values_but_tracks_structure():
    first = {"account": {"uid": "u-1", "nickname": "甲"}, "auth": {"token": "a"}}
    second = {"account": {"uid": "u-2", "nickname": "乙"}, "auth": {"token": "b"}}
    assert fingerprint_of(first) == fingerprint_of(second)

    changed = {"account": {"uid": "u-1"}, "auth": {"token": "a"}}
    assert fingerprint_of(first) != fingerprint_of(changed)


def test_structure_paths_never_include_values():
    paths = structure_paths({"account": {"uid": "u-1", "nickname": "甲"}})
    joined = "\n".join(paths)
    assert "u-1" not in joined
    assert "甲" not in joined
    assert "account.uid" in joined


def test_structure_paths_list_does_not_change_with_length():
    one = fingerprint_of({"accounts": [{"uid": "a"}]})
    many = fingerprint_of({"accounts": [{"uid": "a"}, {"uid": "b"}]})
    assert one == many


class _ProbeAdapter:
    scope = "workbuddy-cn"

    def __init__(self, probe: ClientProbe) -> None:
        self._probe = probe

    def probe_client(self) -> ClientProbe:
        return self._probe


def test_compat_probe_marks_new_structure_then_compatible(tmp_path):
    conn = open_database(tmp_path / "compat.db")
    adapter = _ProbeAdapter(
        ClientProbe(
            platform="workbuddy",
            scope="workbuddy-cn",
            client_version="5.9.2",
            fingerprint=fingerprint_of({"auth": {"token": "x"}}),
            state_paths_ok=True,
        )
    )
    service = CompatService(conn, {"workbuddy-cn": adapter})
    try:
        first = service.probe()
        assert first["items"][0]["state"] == COMPAT_CHANGED
        second = service.probe()
        assert second["items"][0]["state"] == COMPAT_COMPATIBLE
        assert second["changed"] == []
    finally:
        conn.close()


def test_compat_reports_missing_paths_without_fingerprint(tmp_path):
    conn = open_database(tmp_path / "compat.db")
    adapter = _ProbeAdapter(
        ClientProbe(
            platform="trae",
            scope="trae-cn",
            state_paths_ok=False,
            missing_paths=("/tmp/nope/storage.json",),
        )
    )
    service = CompatService(conn, {"trae-cn": adapter})
    try:
        payload = service.probe()
        assert payload["items"][0]["state"] == COMPAT_MISSING
        assert payload["items"][0]["current"]["fingerprint"] == ""
        # 结构指纹为空时不落库，避免把「读不到」写成一次结构变化。
        assert service.history("trae", "trae-cn") == []
    finally:
        conn.close()


def test_compat_manual_probe_is_deduplicated(tmp_path):
    conn = open_database(tmp_path / "compat.db")
    adapter = _ProbeAdapter(
        ClientProbe(
            platform="workbuddy",
            scope="workbuddy-cn",
            fingerprint="abcdef0123456789",
            state_paths_ok=True,
        )
    )
    service = CompatService(conn, {"workbuddy-cn": adapter})
    try:
        service.probe(manual=True)
        again = service.probe(manual=True)
        assert again["deduped"] is True
        assert again["probed"] is False
    finally:
        conn.close()


# ---------- B. 登录健康 ----------


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_workbuddy_login_probe_maps_status_codes():
    credential = {"format": "oauth-token", "accessToken": "tok", "uid": "u-1"}
    ok = _client(lambda request: httpx.Response(200, json={"code": 0, "data": {}}))
    assert probe_workbuddy_login(ok, credential).state == LOGIN_VALID

    denied = _client(lambda request: httpx.Response(401, json={"code": 401}))
    assert probe_workbuddy_login(denied, credential).state == LOGIN_INVALID

    # 10085 是网关风控拒绝，不是令牌失效：不能误杀一个好号。
    waf = _client(lambda request: httpx.Response(403, json={"code": 10085}))
    assert probe_workbuddy_login(waf, credential).state == LOGIN_UNKNOWN


def test_login_probe_network_error_keeps_unknown():
    credential = {"format": "oauth-token", "accessToken": "tok", "uid": "u-1"}

    def boom(request):
        raise httpx.ConnectError("no route")

    assert probe_workbuddy_login(_client(boom), credential).state == LOGIN_UNKNOWN


def test_trae_login_probe_maps_entitlement(monkeypatch):
    # 设备私钥是否存在取决于跑测试的机器：CI 上没有 Trae 设备密钥，
    # 会被判成「设备绑在别处」。本用例只验证状态码映射，先把设备检查固定为 False。
    monkeypatch.setattr("chaierww.domain.login_health._trae_device_foreign", lambda _c: False)
    credential = {"format": "oauth-token", "accessToken": "jwt", "uid": "u-1"}
    ok = _client(
        lambda request: httpx.Response(200, json={"user_entitlement_pack_list": []})
    )
    assert probe_trae_login(ok, credential).state == LOGIN_VALID
    bad = _client(lambda request: httpx.Response(401, json={}))
    assert probe_trae_login(bad, credential).state == LOGIN_INVALID


def test_expiring_soon_window():
    now = datetime(2026, 11, 13, tzinfo=UTC)
    assert expiring_soon((now + timedelta(hours=2)).isoformat(), now=now)
    assert not expiring_soon((now + timedelta(days=3)).isoformat(), now=now)
    assert not expiring_soon("不是时间", now=now)


# ---------- C. 供给 ----------


def test_credits_days_returns_none_without_enough_data():
    assert credits_days({}) is None
    only_one = {"a": [{"at": "2026-11-13T00:00:00+00:00", "credits": 100}]}
    assert credits_days(only_one, now=datetime(2026, 11, 13, tzinfo=UTC)) is None


def test_credits_days_uses_net_consumption():
    now = datetime(2026, 11, 13, tzinfo=UTC)
    snapshots = {
        "a": [
            {"at": (now - timedelta(days=6)).isoformat(), "credits": 700},
            {"at": now.isoformat(), "credits": 490},
        ]
    }
    # 6 天净消耗 210 → 35/天；剩余 490 → 14 天。
    assert credits_days(snapshots, now=now) == 14.0


class _Repo:
    def __init__(self, accounts, logins=None, expiries=None, failures=None) -> None:
        self.accounts = accounts
        self._logins = logins or {}
        self._expiries = expiries or {}
        self._failures = failures or {}

    def list_all(self, scopes):
        return list(self.accounts)

    def login_probes(self, ids):
        return self._logins

    def credential_expiries(self, ids):
        return self._expiries

    def probe_failures(self, ids):
        return self._failures


class _Gateway:
    def __init__(self, snapshots=None, logs=None) -> None:
        self._snapshots = snapshots or {}
        self._logs = logs or []

    def excluded_accounts(self):
        return set()

    def credit_snapshots(self, account_id):
        return self._snapshots.get(account_id, [])

    def logs(self, limit=1000):
        return list(self._logs)


class _Account:
    def __init__(self, account_id: str) -> None:
        self.id = account_id


class _Prefs:
    def __init__(self, healthy=2, days=3.0) -> None:
        self.value = Preferences(min_healthy_accounts=healthy, min_credits_days=days)

    def load(self):
        return self.value


def test_pool_health_alerts_when_not_enough_healthy_accounts():
    now = datetime.now(UTC)
    repo = _Repo(
        [_Account("a"), _Account("b")],
        logins={"a": {"state": "valid"}, "b": {"state": "invalid"}},
        expiries={"a": (now + timedelta(days=10)).isoformat()},
    )
    service = PoolHealthService(repo, _Gateway(), preferences=_Prefs(healthy=2))
    payload = service.health()
    assert payload["healthyAccounts"] == 1
    assert [item["code"] for item in payload["alerts"]] == ["min_healthy_accounts"]
    # 没有任何积分快照 / 网关请求 → null，而不是 0。
    assert payload["creditsDays"] is None
    assert payload["successRate24h"] is None


def test_pool_health_success_rate_uses_recent_logs():
    now = datetime.now(UTC)
    logs = [
        {"at": (now - timedelta(hours=1)).isoformat(), "ok": True},
        {"at": (now - timedelta(hours=2)).isoformat(), "ok": False},
        {"at": (now - timedelta(days=2)).isoformat(), "ok": True},
    ]
    service = PoolHealthService(
        _Repo([], logins={}, expiries={}, failures={}), _Gateway(logs=logs)
    )
    assert service.health()["successRate24h"] == 0.5


def test_pool_health_thresholds_defaults():
    assert SupplyThresholds().min_healthy_accounts == 2


# ---------- D. 活动 unknown / 重跑 ----------


def _result(status: str) -> RunResult:
    return RunResult(
        account_id="a",
        display_name="甲",
        platform="workbuddy",
        key="workbuddy.growth_tasks.x",
        title="新任务",
        status=status,
        message="",
    )


def test_summary_counts_unknown_separately_from_failed():
    summary = _summarize_results([_result(RESULT_UNKNOWN), _result(RESULT_FAILED)])
    assert summary["unknown"] == 1
    assert summary["failed"] == 1


def test_growth_rerun_key_maps_back_to_parent():
    assert _root_key("workbuddy.growth_tasks.new_code") == "workbuddy.growth_tasks"
    assert _root_key("workbuddy.daily_checkin") == "workbuddy.daily_checkin"


class _RerunRepo:
    def __init__(self, failures) -> None:
        self.failures = failures  # list[dict[str,str]]

    def activity_failures(self, day):
        return list(self.failures)

    def activity_catalog_versions(self, limit=5):
        return ["2026.09.1", "2026.08.1"]


class _RerunService(ActivitiesService):
    def __init__(self, repo) -> None:  # 不建 httpx 客户端，避免测试依赖网络栈
        self._repo = repo
        self.calls = []

    def run(self, accounts, *, keys=None, include_growth=True, retry_failed=False):
        self.calls.append(([item.id for item in accounts], set(keys or ())))
        return {"results": [], "summary": _summarize_results([])}


def test_rerun_only_accepts_previously_failed_pairs():
    repo = _RerunRepo([{"accountId": "a", "key": "workbuddy.daily_checkin"}])
    service = _RerunService(repo)
    report = service.rerun_failed(
        [_Account("a"), _Account("b")],
        [("a", "workbuddy.daily_checkin"), ("b", "workbuddy.streak")],
    )
    assert report["rerun"] == 1
    assert report["ignored"] == 1
    assert service.calls == [(["a"], {"workbuddy.daily_checkin"})]


def test_catalog_version_state_flags_change():
    service = _RerunService(_RerunRepo([]))
    state = service.catalog_version_state()
    assert state["lastSeen"] == "2026.09.1"
    assert state["changed"] is True


# ---------- v8 迁移 ----------


def test_v8_adds_login_and_catalog_columns(tmp_path):
    path = tmp_path / "v8.db"
    conn = open_database(path)
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
        assert {"login_state", "last_login_probe_at", "login_probe_detail"} <= columns
        run_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(activity_runs)")
        }
        assert "catalog_version" in run_columns
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8

        repo = AccountRepository(conn, InMemoryBox())
        repo.upsert(
            scope="workbuddy-cn",
            external_id="u-1",
            display_name="甲",
            credential={"access_token": "t"},
        )
        account = repo.list_accounts("workbuddy-cn")[0]
        repo.set_login_probe(account.id, "valid", "ok")
        assert repo.login_probes([account.id])[account.id]["state"] == "valid"
    finally:
        conn.close()

    reopened = open_database(path)
    try:
        # 幂等：再开一次不重复加列。
        columns = {row["name"] for row in reopened.execute("PRAGMA table_info(accounts)")}
        assert "login_state" in columns
    finally:
        reopened.close()


def test_fingerprint_snapshot_survives_json_roundtrip(tmp_path):
    path = tmp_path / "dump.json"
    path.write_text(json.dumps({"auth": {"token": "t"}}), encoding="utf-8")
    assert len(fingerprint_of(json.loads(path.read_text(encoding="utf-8")))) == 16
