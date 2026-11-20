"""号池一键体检：登录复检 + 凭据验活 + 供给/隔离汇总 + 安全自动处置。

设计边界：

- 只做机械汇总，不产出「推荐账号」；
- ``fix`` 只触发健康中心既有的安全白名单（续期 / 补跑），不切号、不删资料；
- 每一段独立降级：登录段失败不阻断凭据段，凭据段失败不阻断供给段。
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from chaierww.domain.credential_lifecycle import (
    MODE_CHECK,
    RESULT_FAILED,
    RESULT_RELOGIN_REQUIRED,
)
from chaierww.domain.login_health import LOGIN_BLOCKED, LOGIN_INVALID

logger = logging.getLogger("chaierww")


class PoolCheckupService:
    """一次体检 = 一个快照 + 一次可选的安全修复。"""

    def __init__(
        self,
        *,
        login_probes: Any,
        lifecycle: Any,
        pool_health: Any,
        isolation: Any,
        health: Any,
    ) -> None:
        self._login_probes = login_probes
        self._lifecycle = lifecycle
        self._pool_health = pool_health
        self._isolation = isolation
        self._health = health

    def run(self, accounts: list[Any], *, fix: bool = False) -> dict[str, Any]:
        started = datetime.now(UTC).replace(microsecond=0)
        clock = time.monotonic()
        errors: dict[str, str] = {}

        login = self._login(accounts, errors)
        credential = self._credential(accounts, errors)
        supply = self._supply(errors)
        isolation = self._isolation_snapshot(errors)
        fix_result = self._fix(errors) if fix else None

        issues = self._issues(login, credential, isolation, supply)
        finished = datetime.now(UTC).replace(microsecond=0)
        return {
            "startedAt": started.isoformat(),
            "finishedAt": finished.isoformat(),
            "durationMs": int((time.monotonic() - clock) * 1000),
            "accounts": [
                {
                    "accountId": account.id,
                    "displayName": account.display_name,
                    "scope": account.scope,
                    "credentialState": getattr(
                        account.credential_state, "value", str(account.credential_state)
                    ),
                }
                for account in accounts
            ],
            "login": login,
            "credential": credential,
            "supply": supply,
            "isolation": isolation,
            "fix": fix_result,
            "issues": issues,
            "errors": errors,
        }

    # ---------- 分段采集 ----------

    def _login(self, accounts: list[Any], errors: dict[str, str]) -> dict[str, Any]:
        try:
            return self._login_probes.probe([account.id for account in accounts])
        except Exception as exc:  # noqa: BLE001 - 登录段失败不阻断其它段
            logger.exception("号池体检：登录复检失败")
            errors["login"] = f"{type(exc).__name__}：{exc}"
            return {
                "items": [],
                "total": 0,
                "valid": 0,
                "invalid": 0,
                "blocked": 0,
            }

    def _credential(self, accounts: list[Any], errors: dict[str, str]) -> dict[str, Any]:
        try:
            results = self._lifecycle.sweep(accounts, mode=MODE_CHECK)
            return {
                "mode": MODE_CHECK,
                "summary": self._lifecycle.summary(results),
                "results": [item.payload() for item in results],
            }
        except Exception as exc:  # noqa: BLE001 - 凭据段失败不阻断供给段
            logger.exception("号池体检：凭据验活失败")
            errors["credential"] = f"{type(exc).__name__}：{exc}"
            return {"mode": MODE_CHECK, "summary": {}, "results": []}

    def _supply(self, errors: dict[str, str]) -> dict[str, Any]:
        try:
            return self._pool_health.health()
        except Exception as exc:  # noqa: BLE001
            logger.exception("号池体检：供给快照失败")
            errors["supply"] = f"{type(exc).__name__}：{exc}"
            return {}

    def _isolation_snapshot(self, errors: dict[str, str]) -> dict[str, Any]:
        try:
            items = self._isolation.items()
            active = [item for item in items if item.get("active")]
            return {"active": len(active), "items": active}
        except Exception as exc:  # noqa: BLE001
            logger.exception("号池体检：隔离台账读取失败")
            errors["isolation"] = f"{type(exc).__name__}：{exc}"
            return {"active": 0, "items": []}

    def _fix(self, errors: dict[str, str]) -> dict[str, Any] | None:
        try:
            return self._health.auto_dispose(force=True)
        except Exception as exc:  # noqa: BLE001 - 修复失败不回滚体检结果
            logger.exception("号池体检：安全修复失败")
            errors["fix"] = f"{type(exc).__name__}：{exc}"
            return None

    # ---------- 机械归类 ----------

    def _issues(
        self,
        login: dict[str, Any],
        credential: dict[str, Any],
        isolation: dict[str, Any],
        supply: dict[str, Any],
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []

        for item in login.get("items", []):
            state = str(item.get("state") or "")
            if state == LOGIN_INVALID:
                issues.append(
                    {
                        "kind": "login_invalid",
                        "accountId": item.get("accountId"),
                        "displayName": item.get("displayName"),
                        "detail": item.get("detail") or "登录态已失效",
                        "next": "relogin",
                    }
                )
            elif state == LOGIN_BLOCKED:
                issues.append(
                    {
                        "kind": "login_blocked",
                        "accountId": item.get("accountId"),
                        "displayName": item.get("displayName"),
                        "detail": item.get("detail") or "设备登录被限制",
                        "next": "relogin",
                    }
                )

        for item in credential.get("results", []):
            status = str(item.get("status") or "")
            if status == RESULT_RELOGIN_REQUIRED:
                issues.append(
                    {
                        "kind": "relogin_required",
                        "accountId": item.get("accountId"),
                        "displayName": item.get("displayName"),
                        "detail": item.get("detail") or "凭据需要重新登录",
                        "next": "relogin",
                    }
                )
            elif status == RESULT_FAILED:
                issues.append(
                    {
                        "kind": "credential_failed",
                        "accountId": item.get("accountId"),
                        "displayName": item.get("displayName"),
                        "detail": item.get("detail") or "验活暂时失败",
                        "next": "diagnose",
                    }
                )

        for item in isolation.get("items", []):
            issues.append(
                {
                    "kind": "isolated",
                    "accountId": item.get("accountId"),
                    "displayName": None,
                    "detail": "网关连续认证失败，已暂时移出候选",
                    "next": "release_isolation",
                }
            )

        for alert in supply.get("alerts", []):
            issues.append(
                {
                    "kind": "supply_low",
                    "accountId": None,
                    "displayName": None,
                    "detail": alert.get("message") or "号池供给低于阈值",
                    "next": "view_health",
                }
            )

        return issues
