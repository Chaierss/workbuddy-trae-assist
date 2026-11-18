from __future__ import annotations

from datetime import UTC, datetime, timedelta

from chaierww.domain.accounts.repository import AccountRepository
from chaierww.domain.stability import StabilityService
from chaierww.infra.db import open_database
from chaierww.infra.secrets import InMemoryBox


class FakeGateway:
    def __init__(self, entries: list[dict[str, object]] | None = None) -> None:
        self.entries = list(entries or [])

    def logs(self, limit: int = 1000) -> list[dict[str, object]]:
        return list(self.entries)


def _account(repo: AccountRepository, name: str = "甲") -> str:
    result = repo.upsert(
        scope="workbuddy-cn",
        external_id=f"u-{name}",
        display_name=name,
        credential={"access_token": "t"},
    )
    return result.account.id


def _service(conn, gateway: FakeGateway, now: datetime) -> StabilityService:
    return StabilityService(
        AccountRepository(conn, InMemoryBox()),
        gateway,
        conn,
        now=lambda: now,
    )


def test_stability_empty_data_is_unknown(tmp_path):
    conn = open_database(tmp_path / "s1.db")
    try:
        _account(AccountRepository(conn, InMemoryBox()))
        report = _service(conn, FakeGateway(), datetime.now(UTC)).report(7)
        assert len(report["items"]) == 1
        item = report["items"][0]
        assert item["switchRate"] is None
        assert item["loginInvalid"] is None
        assert item["refreshFailed"] is None
        assert item["gatewayFailed"] is None
        assert item["switchTotal"] == 0
    finally:
        conn.close()


def test_stability_counts_window_and_ignores_network_errors(tmp_path):
    conn = open_database(tmp_path / "s2.db")
    now = datetime.now(UTC)
    try:
        repo = AccountRepository(conn, InMemoryBox())
        account_id = _account(repo)
        recent = (now - timedelta(hours=2)).replace(microsecond=0).isoformat()
        old = (now - timedelta(days=10)).replace(microsecond=0).isoformat()
        for state, stamp in (("succeeded", recent), ("failed", old)):
            conn.execute(
                """
                INSERT INTO switch_jobs (
                    id, scope, source_account_id, target_account_id, snapshot_id,
                    state, dry_run, started_at, finished_at, error
                ) VALUES (?, 'workbuddy-cn', NULL, ?, NULL, ?, 0, ?, ?, '')
                """,
                (f"job-{state}", account_id, state, stamp, stamp),
            )
        repo.set_login_probe(account_id, "invalid", "令牌失效")
        repo.set_login_probe(account_id, "valid", "恢复")
        gateway = FakeGateway(
            [
                # 网络错误：不算失败
                {"accountId": account_id, "at": recent, "ok": False, "status": 0},
                # 上游 5xx：算网关失败
                {"accountId": account_id, "at": recent, "ok": False, "status": 500},
                {"accountId": account_id, "at": recent, "ok": True, "status": 200},
            ]
        )
        report = _service(conn, gateway, now).report(7)
        item = report["items"][0]
        # 10 天前的失败不在 7 天窗口里
        assert item["switchTotal"] == 1
        assert item["switchOk"] == 1
        assert item["switchRate"] == 1.0
        assert item["loginInvalid"] == 1
        assert item["refreshFailed"] == 0
        assert item["gatewayFailed"] == 1
    finally:
        conn.close()
