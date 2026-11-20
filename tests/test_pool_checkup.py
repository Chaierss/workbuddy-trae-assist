"""1.12.0 号池一键体检：只做机械汇总，不做推荐，不越权写。"""

from __future__ import annotations

from types import SimpleNamespace

from chaierww.domain.credential_lifecycle import (
    RESULT_FAILED,
    RESULT_OK,
    RESULT_RELOGIN_REQUIRED,
)
from chaierww.domain.login_health import LOGIN_BLOCKED, LOGIN_INVALID, LOGIN_VALID
from chaierww.domain.pool_checkup import PoolCheckupService


class FakeLoginProbes:
    def __init__(self, items: list[dict[str, object]] | None = None, boom: bool = False) -> None:
        self.items = list(items or [])
        self.boom = boom
        self.calls: list[list[str]] = []

    def probe(self, account_ids: list[str] | None = None) -> dict[str, object]:
        self.calls.append(list(account_ids or []))
        if self.boom:
            raise RuntimeError("上游超时")
        return {
            "items": list(self.items),
            "total": len(self.items),
            "valid": sum(1 for item in self.items if item["state"] == LOGIN_VALID),
            "invalid": sum(1 for item in self.items if item["state"] == LOGIN_INVALID),
            "blocked": sum(1 for item in self.items if item["state"] == LOGIN_BLOCKED),
        }


class FakeResult:
    def __init__(self, account_id: str, status: str, detail: str = "") -> None:
        self.account_id = account_id
        self.status = status
        self.detail = detail

    def payload(self) -> dict[str, object]:
        return {
            "accountId": self.account_id,
            "displayName": self.account_id,
            "status": self.status,
            "detail": self.detail,
            "expiresAt": None,
        }


class FakeLifecycle:
    def __init__(self, results: list[FakeResult] | None = None, boom: bool = False) -> None:
        self.results = list(results or [])
        self.boom = boom
        self.calls: list[str] = []

    def sweep(self, accounts: list[object], mode: str = "due") -> list[FakeResult]:
        self.calls.append(mode)
        if self.boom:
            raise RuntimeError("续期服务不可用")
        return list(self.results)

    def summary(self, results: list[FakeResult]) -> dict[str, int]:
        return {
            "total": len(results),
            "ok": sum(1 for item in results if item.status == RESULT_OK),
            "reloginRequired": sum(
                1 for item in results if item.status == RESULT_RELOGIN_REQUIRED
            ),
            "failed": sum(1 for item in results if item.status == RESULT_FAILED),
        }


class FakePoolHealth:
    def __init__(self, alerts: list[dict[str, object]] | None = None) -> None:
        self.alerts = list(alerts or [])

    def health(self) -> dict[str, object]:
        return {
            "healthyAccounts": 1,
            "creditsDays": 2.0,
            "alerts": list(self.alerts),
        }


class FakeIsolation:
    def __init__(self, items: list[dict[str, object]] | None = None) -> None:
        self.value = list(items or [])

    def items(self) -> list[dict[str, object]]:
        return list(self.value)


class FakeHealth:
    def __init__(self) -> None:
        self.forces: list[bool] = []

    def auto_dispose(self, *, force: bool = False) -> dict[str, object]:
        self.forces.append(force)
        return {
            "enabled": True,
            "busy": False,
            "refreshed": 1,
            "rerunPairs": 0,
            "rerunSummary": None,
        }


def _account(account_id: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=account_id,
        display_name=name,
        scope="workbuddy-cn",
        credential_state=SimpleNamespace(value="usable"),
    )


def _service(
    *,
    login: FakeLoginProbes | None = None,
    lifecycle: FakeLifecycle | None = None,
    supply: FakePoolHealth | None = None,
    isolation: FakeIsolation | None = None,
    health: FakeHealth | None = None,
) -> PoolCheckupService:
    return PoolCheckupService(
        login_probes=login or FakeLoginProbes(),
        lifecycle=lifecycle or FakeLifecycle(),
        pool_health=supply or FakePoolHealth(),
        isolation=isolation or FakeIsolation(),
        health=health or FakeHealth(),
    )


def test_checkup_aggregates_mechanical_issues_without_fix():
    accounts = [_account("a1", "甲"), _account("a2", "乙"), _account("a3", "丙")]
    login = FakeLoginProbes(
        [
            {"accountId": "a1", "displayName": "甲", "state": LOGIN_VALID},
            {"accountId": "a2", "displayName": "乙", "state": LOGIN_INVALID, "detail": "已退出"},
            {
                "accountId": "a3",
                "displayName": "丙",
                "state": LOGIN_BLOCKED,
                "detail": "设备限制",
            },
        ]
    )
    lifecycle = FakeLifecycle(
        [
            FakeResult("a1", RESULT_OK),
            FakeResult("a2", RESULT_RELOGIN_REQUIRED, "需要重新登录"),
            FakeResult("a3", RESULT_FAILED, "上游 502"),
        ]
    )
    supply = FakePoolHealth([{"code": "credits_days", "message": "库存约 2 天"}])
    isolation = FakeIsolation(
        [
            {"accountId": "a2", "active": True},
            {"accountId": "a3", "active": False},
        ]
    )
    health = FakeHealth()

    report = _service(
        login=login, lifecycle=lifecycle, supply=supply, isolation=isolation, health=health
    ).run(accounts)

    assert report["fix"] is None
    assert health.forces == []
    assert report["errors"] == {}
    assert report["login"]["invalid"] == 1
    assert report["credential"]["summary"]["reloginRequired"] == 1
    assert report["isolation"]["active"] == 1
    assert {issue["kind"] for issue in report["issues"]} == {
        "login_invalid",
        "login_blocked",
        "relogin_required",
        "credential_failed",
        "isolated",
        "supply_low",
    }
    # 只做机械归类，不产出推荐字段。
    assert "recommendation" not in report
    assert all("score" not in issue for issue in report["issues"])


def test_checkup_fix_calls_safe_auto_dispose_once():
    health = FakeHealth()
    report = _service(health=health).run([_account("a1", "甲")], fix=True)

    assert health.forces == [True]
    assert report["fix"]["enabled"] is True
    assert report["fix"]["refreshed"] == 1


def test_checkup_login_failure_does_not_block_credential_section():
    login = FakeLoginProbes(boom=True)
    lifecycle = FakeLifecycle([FakeResult("a1", RESULT_RELOGIN_REQUIRED, "需要重新登录")])
    report = _service(login=login, lifecycle=lifecycle).run([_account("a1", "甲")])

    assert "login" in report["errors"]
    assert lifecycle.calls == ["check"]
    assert [issue["kind"] for issue in report["issues"]] == ["relogin_required"]


def test_checkup_credential_failure_does_not_block_supply_section():
    lifecycle = FakeLifecycle(boom=True)
    report = _service(lifecycle=lifecycle).run([_account("a1", "甲")])

    assert "credential" in report["errors"]
    assert report["supply"]["healthyAccounts"] == 1
