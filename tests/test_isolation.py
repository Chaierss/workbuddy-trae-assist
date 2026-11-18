from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from chaierww.domain.isolation import IsolationService
from chaierww.infra.db import open_database


class FakePreferences:
    def __init__(
        self, *, enabled: bool = True, threshold: int = 3, hours: int = 24
    ) -> None:
        self._prefs = SimpleNamespace(
            isolation_enabled=enabled,
            isolation_threshold=threshold,
            isolation_hours=hours,
        )

    def load(self) -> SimpleNamespace:
        return self._prefs


def test_isolation_triggers_after_threshold_and_expires(tmp_path):
    conn = open_database(tmp_path / "i1.db")
    clock = {"now": datetime(2026, 11, 18, 9, 0, tzinfo=UTC)}
    try:
        service = IsolationService(
            conn, FakePreferences(), now=lambda: clock["now"]
        )
        assert service.record_failure("a1", 401) is None
        assert service.record_failure("a1", 403) is None
        assert service.active_ids() == set()

        isolated = service.record_failure("a1", 401)
        assert isolated is not None
        assert isolated["failureCount"] == 3
        assert "a1" in service.active_ids()

        # 24 小时自动回归
        clock["now"] += timedelta(hours=25)
        assert service.active_ids() == set()
        assert service.items()[0]["active"] is False
    finally:
        conn.close()


def test_isolation_ignores_network_and_disabled(tmp_path):
    conn = open_database(tmp_path / "i2.db")
    try:
        enabled = IsolationService(
            conn, FakePreferences(), now=lambda: datetime.now(UTC)
        )
        for _ in range(3):
            assert enabled.record_failure("a2", 500) is None
        assert enabled.active_ids() == set()

        disabled = IsolationService(
            conn, FakePreferences(enabled=False), now=lambda: datetime.now(UTC)
        )
        for _ in range(3):
            assert disabled.record_failure("a2", 401) is None
        assert disabled.active_ids() == set()
    finally:
        conn.close()


def test_isolation_release_and_success_reset(tmp_path):
    conn = open_database(tmp_path / "i3.db")
    try:
        now = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
        service = IsolationService(conn, FakePreferences(), now=lambda: now)
        for _ in range(3):
            service.record_failure("a3", 401)
        assert "a3" in service.active_ids()

        service.release("a3")
        assert service.active_ids() == set()
        detail = service.detail("a3")
        assert detail is not None
        assert detail["failureCount"] == 0

        # 释放后重新累计，而不是一次失败就再次隔离
        assert service.record_failure("a3", 401) is None
        assert service.active_ids() == set()

        service.record_success("a3")
        assert service.detail("a3") is None
    finally:
        conn.close()
