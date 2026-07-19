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


# Stable, credential-free failure categories exposed by the admin
# connection-status API (TODO.md item 43). Deliberately coarse: the raw driver
# exception can contain the host, port, database name, or username being
# connected to, so it is only ever logged to stdout — never surfaced through an
# API. A category is derived from the exception *type*, never its message text.
FAILURE_CATEGORY_AUTHENTICATION = "authentication"
FAILURE_CATEGORY_UNREACHABLE = "unreachable"
FAILURE_CATEGORY_TIMEOUT = "timeout"
FAILURE_CATEGORY_ERROR = "error"


def classify_failure(exc: BaseException) -> str:
    """Map a ping failure to a stable, redaction-safe category.

    Classification uses the exception type (stdlib isinstance plus driver
    class-name matching), never the message, so a driver error that embeds a
    hostname/username/password can never leak through the category.
    """
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return FAILURE_CATEGORY_TIMEOUT
    if isinstance(exc, (ConnectionRefusedError, ConnectionError, OSError)):
        # OSError covers refused connections, DNS failures (socket.gaierror),
        # and network-unreachable — all "can't reach the server".
        return FAILURE_CATEGORY_UNREACHABLE
    type_name = type(exc).__name__.lower()
    if any(token in type_name for token in ("password", "authoriz", "auth", "login")):
        return FAILURE_CATEGORY_AUTHENTICATION
    if "timeout" in type_name:
        return FAILURE_CATEGORY_TIMEOUT
    if any(token in type_name for token in ("connect", "network", "unreach", "dns")):
        return FAILURE_CATEGORY_UNREACHABLE
    return FAILURE_CATEGORY_ERROR


@dataclass(frozen=True)
class ConnectionHealth:
    connection_id: str
    healthy: Optional[bool]  # None: not checked yet (e.g. right after startup)
    last_checked: Optional[float]  # time.time(), or None if never checked
    # Wall-clock time of the last *successful* ping — persists across a later
    # failure so an operator can see "reachable until 3 minutes ago".
    last_success: Optional[float] = None
    # Round-trip latency of the last successful ping, in milliseconds.
    latency_ms: Optional[float] = None
    # Stable, redaction-safe failure category (see classify_failure); None while
    # healthy or not-yet-checked.
    failure_category: Optional[str] = None
    # Raw driver error — for stdout logging only. NEVER returned by any API; the
    # admin status endpoint exposes failure_category instead.
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
        previous = self._status.get(connection_id)
        started = time.perf_counter()
        try:
            await _ping(connection_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._status[connection_id] = ConnectionHealth(
                connection_id=connection_id,
                healthy=False,
                last_checked=time.time(),
                # Preserve the last known-good timestamp/latency across a failure
                # so the admin view can show "reachable until <time>".
                last_success=previous.last_success if previous else None,
                latency_ms=previous.latency_ms if previous else None,
                failure_category=classify_failure(exc),
                error=str(exc),
            )
            get_logger().warning("health.check.failed", connection=connection_id, error=str(exc))
        else:
            now = time.time()
            self._status[connection_id] = ConnectionHealth(
                connection_id=connection_id,
                healthy=True,
                last_checked=now,
                last_success=now,
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
            )
