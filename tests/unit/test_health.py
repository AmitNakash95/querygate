"""Unit tests for HealthMonitor — background connection-health caching."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from querygate import health as health_module
from querygate.health import HealthMonitor


@pytest.mark.asyncio
async def test_start_primes_status_for_every_enabled_connection():
    with patch.object(health_module, "_ping", new_callable=AsyncMock):
        monitor = HealthMonitor(interval_seconds=1000)
        await monitor.start()
        try:
            snapshot = monitor.snapshot()
            assert "demo" in snapshot
            assert snapshot["demo"].healthy is True
            assert snapshot["demo"].last_checked is not None
            assert snapshot["demo"].error is None
        finally:
            await monitor.stop()


@pytest.mark.asyncio
async def test_start_records_failure_without_raising():
    with patch.object(
        health_module, "_ping", new_callable=AsyncMock, side_effect=ConnectionError("refused")
    ):
        monitor = HealthMonitor(interval_seconds=1000)
        await monitor.start()
        try:
            snapshot = monitor.snapshot()
            assert snapshot["demo"].healthy is False
            assert "refused" in snapshot["demo"].error
        finally:
            await monitor.stop()


@pytest.mark.asyncio
async def test_stop_cancels_background_tasks_cleanly():
    with patch.object(health_module, "_ping", new_callable=AsyncMock):
        monitor = HealthMonitor(interval_seconds=1000)
        await monitor.start()
        assert len(monitor._tasks) == 1
        await monitor.stop()
        assert monitor._tasks == []


@pytest.mark.asyncio
async def test_one_connection_failure_does_not_block_others():
    from querygate.connections.models import ConnectionProfile
    from querygate.connections.registry import ConnectionRegistry, set_registry

    set_registry(
        ConnectionRegistry(
            {
                "good": ConnectionProfile(
                    id="good", dialect="postgresql", connection_string="postgresql+asyncpg://x/y"
                ),
                "bad": ConnectionProfile(
                    id="bad", dialect="postgresql", connection_string="postgresql+asyncpg://x/z"
                ),
            }
        )
    )

    async def _ping_side_effect(connection_id: str) -> None:
        if connection_id == "bad":
            raise ConnectionError("refused")

    with patch.object(health_module, "_ping", new_callable=AsyncMock) as mock_ping:
        mock_ping.side_effect = _ping_side_effect
        monitor = HealthMonitor(interval_seconds=1000)
        await monitor.start()
        try:
            snapshot = monitor.snapshot()
            assert snapshot["good"].healthy is True
            assert snapshot["bad"].healthy is False
        finally:
            await monitor.stop()


@pytest.mark.parametrize(
    "exc,expected",
    [
        (TimeoutError(), "timeout"),
        (ConnectionRefusedError(), "unreachable"),
        (OSError("network is unreachable"), "unreachable"),
        (ConnectionError("reset"), "unreachable"),
        (type("InvalidPasswordError", (Exception,), {})(), "authentication"),
        (type("LoginTimeoutError", (Exception,), {})(), "authentication"),
        (type("SomeConnectError", (Exception,), {})(), "unreachable"),
        (type("QueryError", (Exception,), {})(), "error"),
    ],
)
def test_classify_failure_categories(exc, expected):
    assert health_module.classify_failure(exc) == expected


def test_classify_failure_never_uses_the_message():
    # A driver error embedding host/user/password must classify by type only.
    leaky = type("WeirdError", (Exception,), {})("host=db.internal user=admin password=hunter2")
    category = health_module.classify_failure(leaky)
    assert category == "error"
    for secret in ("db.internal", "admin", "hunter2"):
        assert secret not in category


@pytest.mark.asyncio
async def test_check_once_records_latency_and_last_success():
    with patch.object(health_module, "_ping", new_callable=AsyncMock):
        monitor = HealthMonitor(interval_seconds=1000)
        await monitor._check_once("demo")
    status = monitor.snapshot()["demo"]
    assert status.healthy is True
    assert status.last_success is not None
    assert status.latency_ms is not None and status.latency_ms >= 0
    assert status.failure_category is None


@pytest.mark.asyncio
async def test_last_success_persists_across_a_later_failure():
    with patch.object(health_module, "_ping", new_callable=AsyncMock):
        monitor = HealthMonitor(interval_seconds=1000)
        await monitor._check_once("demo")
    first_success = monitor.snapshot()["demo"].last_success
    assert first_success is not None

    with patch.object(
        health_module, "_ping", new_callable=AsyncMock, side_effect=ConnectionRefusedError("no")
    ):
        await monitor._check_once("demo")
    after = monitor.snapshot()["demo"]
    assert after.healthy is False
    assert after.failure_category == "unreachable"
    # The last known-good timestamp survives the failure for the admin view.
    assert after.last_success == first_success
