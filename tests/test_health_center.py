from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from chaierww.domain.accounts.repository import AccountRepository
from chaierww.domain.activities import RESULT_FAILED
from chaierww.domain.health_center import HealthCenterError, HealthCenterService
from chaierww.infra.db import open_database
from chaierww.infra.secrets import InMemoryBox


class FakeCompat:
    def __init__(self, items: list[dict[str, object]] | None = None) -> None:
        self.items = list(items or [])
        self.confirmed: list[str] = []

    def unconfirmed(self) -> list[dict[str, object]]:
        return list(self.items)

    def confirm(self, scope: str) -> dict[str, object]:
        self.confirmed.append(scope)
        self.items = [item for item in self.items if item.get("scope") != scope]
        return {"scope": scope}

    def probe(self, manual: bool = False) -> dict[str, object]:
        return {"changed": []}


class FakePoolHealth:
    def __init__(self, alerts: list[dict[str, object]] | None = None) -> None:
        self.alerts = list(alerts or [])

    def health(self) -> dict[str, object]:
        return {
            "healthyAccounts": 1,
            "creditsDays": 2.0,
            "thresholds": {"minHealthyAccounts": 2, "minCreditsDays": 3},
            "alerts": list(self.alerts),
        }


class FakeLoginProbes:
    def __init__(self, state: str = "valid") -> None:
        self.state = state
        self.calls: list[list[str] | None] = []

    def probe(self, account_ids: list[str] | None = None) -> dict[str, object]:
        self.calls.append(account_ids)
        return {"items": [{"accountId": (account_ids or [""])[0], "state": self.state}]}


class FakeLifecycle:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def sweep(self, accounts: list[object], mode: str = "due") -> list[object]:
        self.calls.append(mode)
        return [SimpleNamespace(status="ok")]

    def describe(self, results: list[object], mode: str) -> str:
        return "续期完成"


class FakeActivities:
    def __init__(self) -> None:
        self.pairs: list[tuple[str, str]] = []

    def rerun_failed(self, accounts: list[object], pairs: list[tuple[str, str]] | None = None):
        self.pairs = list(pairs or [])
        return {"summary": {"ok": 1, "failed": 0}}


class FakeScheduler:
    def __init__(self, tasks: list[object] | None = None) -> None:
        self.tasks = list(tasks or [])
        self.ran: list[str] = []

    def list(self) -> list[object]:
        return list(self.tasks)

    def run_now(self, key: str) -> dict[str, object]:
        self.ran.append(key)
        return {"result": "已重试"}


def _compat_item(scope: str = "workbuddy-cn") -> dict[str, object]:
    return {
        "platform": "workbuddy",
        "scope": scope,
        "clientVersion": "6.0.0",
        "fingerprint": "abcdef0123456789",
        "firstSeenAt": "2026-11-16T00:00:00+00:00",
    }


def _service(
    tmp_path,
    *,
    compat=None,
    pool=None,
    login=None,
    lifecycle=None,
    activities=None,
    scheduler=None,
    now=None,
    clock=None,
) -> tuple[HealthCenterService, AccountRepository, sqlite3.Connection]:
    conn = open_database(tmp_path / "health.db")
    repo = AccountRepository(conn, InMemoryBox())
    service = HealthCenterService(
        conn,
        repo,
        compat=compat or FakeCompat(),
        pool_health=pool or FakePoolHealth(),
        login_probes=login or FakeLoginProbes(),
        lifecycle=lifecycle or FakeLifecycle(),
        activities=activities or FakeActivities(),
        scheduler=scheduler or FakeScheduler(),
        now=now or (lambda: datetime(2026, 11, 16, 9, 0, tzinfo=UTC)),
        clock=clock or (lambda: 0.0),
    )
    return service, repo, conn


def _account(repo: AccountRepository, name: str = "甲"):
    result = repo.upsert(
        scope="workbuddy-cn",
        external_id=f"u-{name}",
        display_name=name,
        credential={"access_token": "t"},
    )
    return repo.get(result.account.id)


def test_snapshot_dedupes_and_tracks_ack(tmp_path):
    compat = FakeCompat([_compat_item()])
    service, repo, conn = _service(tmp_path, compat=compat)
    try:
        account = _account(repo)
        repo.set_login_probe(account.id, "invalid", "令牌失效")

        first = service.snapshot()
        assert len(first["items"]) == 2
        assert {item["kind"] for item in first["items"]} == {
            "compat_changed",
            "login_invalid",
        }
        assert first["summary"]["open"] == 2

        # 再刷新一次：同一个异常只更新时间，不新增事件。
        second = service.snapshot()
        assert len(second["items"]) == 2

        login_event = next(item for item in second["items"] if item["kind"] == "login_invalid")
        service.ack(login_event["id"])
        summary = service.summary()
        assert summary["acked"] == 1
        assert summary["unhandled"] == 1
    finally:
        conn.close()


def test_signal_disappears_auto_resolves(tmp_path):
    compat = FakeCompat([_compat_item()])
    login = FakeLoginProbes("valid")
    service, repo, conn = _service(tmp_path, compat=compat, login=login)
    try:
        account = _account(repo)
        repo.set_login_probe(account.id, "invalid", "令牌失效")
        assert len(service.snapshot()["items"]) == 2

        repo.set_login_probe(account.id, "valid", "恢复")
        compat.items = []
        active = service.snapshot()
        assert active["items"] == []
        assert active["summary"]["open"] == 0

        resolved = service.snapshot(state="resolved")
        assert {item["kind"] for item in resolved["items"]} == {
            "compat_changed",
            "login_invalid",
        }
        assert all(item["resolvedAt"] for item in resolved["items"])
    finally:
        conn.close()


def test_snooze_reopens_after_window(tmp_path):
    moment = [datetime(2026, 11, 16, 9, 0, tzinfo=UTC)]
    service, repo, conn = _service(tmp_path, now=lambda: moment[0])
    try:
        account = _account(repo)
        repo.set_login_probe(account.id, "invalid", "令牌失效")
        event = service.snapshot()["items"][0]

        snoozed = service.snooze(event["id"], hours=1)
        assert snoozed["state"] == "snoozed"
        assert service.summary()["snoozed"] == 1

        moment[0] = moment[0] + timedelta(hours=2)
        reopened = service.snapshot()["items"][0]
        assert reopened["state"] == "open"
        assert reopened["snoozedUntil"] is None
    finally:
        conn.close()


def test_actions_are_whitelisted_and_dispatch(tmp_path):
    login = FakeLoginProbes("valid")
    scheduler = FakeScheduler()
    service, repo, conn = _service(tmp_path, login=login, scheduler=scheduler)
    try:
        account = _account(repo)
        repo.set_login_probe(account.id, "invalid", "令牌失效")
        event = service.snapshot()["items"][0]

        result = service.act(event["id"], "recheck_login")
        assert result["ok"] is True
        assert login.calls == [[account.id]]

        with pytest.raises(HealthCenterError):
            service.act(event["id"], "run_anything")

        with pytest.raises(HealthCenterError):
            service.act(event["id"], "open_accounts")
    finally:
        conn.close()


def test_schedule_failure_event_and_retry(tmp_path):
    task = SimpleNamespace(
        key="checkin",
        enabled=True,
        history=(
            {"at": "2026-11-16T09:10:00+08:00", "result": "失败 1", "meta": {"failed": 1}},
        ),
    )
    scheduler = FakeScheduler([task])
    service, _repo, conn = _service(tmp_path, scheduler=scheduler)
    try:
        event = service.snapshot()["items"][0]
        assert event["kind"] == "schedule_failed"
        service.act(event["id"], "run_schedule")
        assert scheduler.ran == ["checkin"]
    finally:
        conn.close()


def test_activity_failure_event_and_rerun(tmp_path):
    activities = FakeActivities()
    service, repo, conn = _service(tmp_path, activities=activities)
    try:
        account = _account(repo)
        repo.record_activity_runs(
            [
                {
                    "accountId": account.id,
                    "key": "workbuddy.sign_in",
                    "day": "2026-11-16",
                    "attempt": 1,
                    "status": RESULT_FAILED,
                    "message": "上游 502",
                    "at": "2026-11-16T09:00:00+00:00",
                }
            ]
        )
        event = service.snapshot()["items"][0]
        assert event["kind"] == "activity_failed"
        assert event["evidence"]["key"] == "workbuddy.sign_in"

        service.act(event["id"], "rerun_activity")
        assert activities.pairs == [(account.id, "workbuddy.sign_in")]
    finally:
        conn.close()


def test_auto_dispose_respects_switches(tmp_path):
    class Prefs:
        def __init__(self, refresh: bool, rerun: bool) -> None:
            self.value = SimpleNamespace(
                health_auto_refresh=refresh, health_auto_rerun=rerun
            )

        def load(self):
            return self.value

    lifecycle = FakeLifecycle()
    activities = FakeActivities()
    conn = open_database(tmp_path / "auto.db")
    repo = AccountRepository(conn, InMemoryBox())
    try:
        account = repo.upsert(
            scope="workbuddy-cn",
            external_id="u-auto",
            display_name="甲",
            credential={"format": "oauth-token", "access_token": "t", "refreshToken": "r"},
            credential_kind="oauth_token",
        ).account
        repo.set_login_probe(account.id, "expiring", "24 小时内到期")
        repo.record_activity_runs(
            [
                {
                    "accountId": account.id,
                    "key": "workbuddy.sign_in",
                    "day": "2026-11-16",
                    "attempt": 1,
                    "status": RESULT_FAILED,
                    "message": "上游 502",
                    "at": "2026-11-16T09:00:00+00:00",
                }
            ]
        )
        service = HealthCenterService(
            conn,
            repo,
            login_probes=FakeLoginProbes("valid"),
            lifecycle=lifecycle,
            activities=activities,
            preferences=Prefs(True, True),
            now=lambda: datetime(2026, 11, 16, 9, 0, tzinfo=UTC),
            clock=lambda: 0.0,
        )

        outcome = service.auto_dispose()
        assert outcome["enabled"] is True
        assert lifecycle.calls == ["force"]
        assert activities.pairs == [(account.id, "workbuddy.sign_in")]

        # 同一轮事件在窗口内不再重复处置：不会一天续期两次 / 补跑两次。
        again = service.auto_dispose()
        assert again["refreshed"] == 0
        assert lifecycle.calls == ["force"]
        assert activities.pairs == [(account.id, "workbuddy.sign_in")]
    finally:
        conn.close()

    conn = open_database(tmp_path / "auto-off.db")
    repo = AccountRepository(conn, InMemoryBox())
    try:
        account = repo.upsert(
            scope="workbuddy-cn",
            external_id="u-off",
            display_name="乙",
            credential={"format": "oauth-token", "access_token": "t", "refreshToken": "r"},
            credential_kind="oauth_token",
        ).account
        repo.set_login_probe(account.id, "expiring", "24 小时内到期")
        service = HealthCenterService(
            conn,
            repo,
            lifecycle=FakeLifecycle(),
            preferences=Prefs(False, False),
            now=lambda: datetime(2026, 11, 16, 9, 0, tzinfo=UTC),
        )
        outcome = service.auto_dispose()
        assert outcome == {"enabled": False, "message": "自动处置未开启"}
    finally:
        conn.close()


def test_v9_health_tables_are_idempotent(tmp_path):
    path = tmp_path / "v9.db"
    conn = open_database(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {
            "health_events",
            "health_actions",
            "activity_snapshots",
            "account_isolation",
        } <= tables
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(compat_snapshots)")}
        assert "confirmed_at" in columns
    finally:
        conn.close()

    reopened = open_database(path)
    try:
        assert reopened.execute("PRAGMA user_version").fetchone()[0] == 10
    finally:
        reopened.close()
