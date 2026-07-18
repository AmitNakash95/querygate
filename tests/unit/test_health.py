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
