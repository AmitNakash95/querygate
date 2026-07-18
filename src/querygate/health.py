"""Background connection-health monitoring for `/health`.

`GET /health` previously returned a static `{"status": "ok"}` regardless of
whether any configured database was actually reachable — a load
balancer/orchestrator using it for liveness/readiness would happily route
traffic to an instance that can't reach its databases at all.

Pinging every connection on every health-check request would be wasteful
(and slow the endpoint down to the speed of the slowest database), so a
`HealthMonitor` instead pings each enabled connection on a background
interval and `/health` reads the cached last-known-good result.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Dict, Optional

import sqlalchemy as sa

from querygate.connections.engine import get_engine
from querygate.connections.registry import get_registry
from querygate.core.logging import get_logger


@dataclass(frozen=True)
class ConnectionHealth:
    connection_id: str
    healthy: Optional[bool]  # None: not checked yet (e.g. right after startup)
    last_checked: Optional[float]  # time.time(), or None if never checked
    error: Optional[str] = None


async def _ping(connection_id: str) -> None:
    """The one seam that touches a real connection — patch this in tests."""
    engine = get_engine(connection_id)
    async with engine.connect() as conn:
        await conn.execute(sa.text("SELECT 1"))


class HealthMonitor:
    """Pings every enabled connection on an interval and caches the result."""

    def __init__(self, interval_seconds: float) -> None:
        self._interval_seconds = interval_seconds
        self._status: Dict[str, ConnectionHealth] = {}
        self._tasks: list[asyncio.Task] = []

    def snapshot(self) -> Dict[str, ConnectionHealth]:
        return dict(self._status)

    async def start(self) -> None:
        """Prime every connection's status before returning (bounded by each
        connection's own connect timeout), then keep refreshing in the
        background. This keeps `/health` from reporting "unknown" for up to
        a full interval right after every restart — the case that matters
        most for a readiness probe.
        """
        connection_ids = [profile.id for profile in get_registry().list_public() if profile.enabled]
        for connection_id in connection_ids:
            self._status[connection_id] = ConnectionHealth(
                connection_id=connection_id, healthy=None, last_checked=None
            )
        await asyncio.gather(
            *(self._check_once(cid) for cid in connection_ids), return_exceptions=True
        )
        for connection_id in connection_ids:
            self._tasks.append(asyncio.create_task(self._loop(connection_id)))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    async def _loop(self, connection_id: str) -> None:
        while True:
            try:
                await asyncio.sleep(self._interval_seconds)
            except asyncio.CancelledError:
                return
            await self._check_once(connection_id)

    async def _check_once(self, connection_id: str) -> None:
        try:
            await _ping(connection_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._status[connection_id] = ConnectionHealth(
                connection_id=connection_id,
                healthy=False,
                last_checked=time.time(),
                error=str(exc),
            )
            get_logger().warning("health.check.failed", connection=connection_id, error=str(exc))
        else:
            self._status[connection_id] = ConnectionHealth(
                connection_id=connection_id, healthy=True, last_checked=time.time()
            )
