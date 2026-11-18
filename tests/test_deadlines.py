from __future__ import annotations

from types import SimpleNamespace

from chaierww.domain.accounts.repository import AccountRepository
from chaierww.domain.deadlines import deadlines, record_snapshots
from chaierww.infra.db import open_database
from chaierww.infra.secrets import InMemoryBox


def _account(repo: AccountRepository, name: str) -> str:
    result = repo.upsert(
        scope="workbuddy-cn",
        external_id=f"u-{name}",
        display_name=name,
        credential={"access_token": "t"},
    )
    return result.account.id


def _detected(account_id: str, items: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(account_id=account_id, detectable=True, items=items)


def test_deadline_aggregates_pending_accounts(tmp_path):
    conn = open_database(tmp_path / "deadline.db")
    try:
        repo = AccountRepository(conn, InMemoryBox())
        first = _account(repo, "甲")
        second = _account(repo, "乙")
        record_snapshots(
            conn,
            [
                _detected(
                    first,
                    [
                        SimpleNamespace(
                            key="wb_season",
                            state="todo",
                            period_end="2026-11-20",
                        ),
                        SimpleNamespace(
                            key="wb_daily",
                            state="todo",
                            period_end=None,
                        ),
                    ],
                ),
                _detected(
                    second,
                    [
                        SimpleNamespace(
                            key="wb_season",
                            state="done",
                            period_end="2026-11-20",
                        )
                    ],
                ),
            ],
        )
        report = deadlines(conn, days=3, now=_now(2026, 11, 18))
        assert len(report["items"]) == 1
        item = report["items"][0]
        assert item["key"] == "wb_season"
        assert item["daysLeft"] == 2
        assert item["totalAccounts"] == 2
        assert item["pending"] == 1
        assert item["pendingAccounts"][0]["displayName"] == "甲"
        assert item["stale"] is False
    finally:
        conn.close()


def test_deadline_skips_done_and_missing_period(tmp_path):
    conn = open_database(tmp_path / "deadline2.db")
    try:
        repo = AccountRepository(conn, InMemoryBox())
        first = _account(repo, "甲")
        record_snapshots(
            conn,
            [
                _detected(
                    first,
                    [
                        SimpleNamespace(
                            key="wb_season", state="done", period_end="2026-11-20"
                        ),
                        SimpleNamespace(
                            key="wb_daily", state="todo", period_end=None
                        ),
                    ],
                )
            ],
        )
        report = deadlines(conn, now=_now(2026, 11, 18))
        assert report["items"] == []
    finally:
        conn.close()


def test_deadline_marks_stale_snapshot(tmp_path):
    conn = open_database(tmp_path / "deadline3.db")
    try:
        repo = AccountRepository(conn, InMemoryBox())
        first = _account(repo, "甲")
        record_snapshots(
            conn,
            [
                _detected(
                    first,
                    [SimpleNamespace(key="wb_season", state="todo", period_end="2026-11-20")],
                )
            ],
        )
        conn.execute(
            "UPDATE activity_snapshots SET detected_at = ? WHERE account_id = ?",
            ("2026-11-16T00:00:00+00:00", first),
        )
        report = deadlines(conn, now=_now(2026, 11, 18))
        assert report["items"][0]["stale"] is True
    finally:
        conn.close()


def _now(year: int, month: int, day: int):
    from datetime import UTC, datetime

    return datetime(year, month, day, 9, 0, tzinfo=UTC)
