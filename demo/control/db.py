"""Real Postgres helpers for the partner-demo control backend.

Everything here talks to the actual `querygate_demo_pitch` database (port
5544) through asyncpg — no mocking, no simulated counters. `db.touched` in
every RunResult is derived from a real pg_stat_statements delta snapshotted
immediately before and after a scenario's MCP call(s) (demo/SPEC.md rule:
"never inferred from the outcome").
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

import asyncpg

from . import config

logger = logging.getLogger("querygate.demo.control.db")

# SQL: total statement executions attributed to agent_ro across
# pg_stat_statements, regardless of which of the two MCP servers issued
# them — this is the number the "Your database" indicator shows.
_AGENT_RO_CALLS_SQL = """
    SELECT coalesce(sum(s.calls), 0)::bigint AS n
    FROM pg_stat_statements s
    JOIN pg_roles r ON r.oid = s.userid
    WHERE r.rolname = $1
"""

# Every backend belonging to the role, whatever it is doing. This is the KILL
# set, deliberately broad: an idle-in-transaction backend still holds locks, so
# the panic path should not leave one behind. QueryGate's pool sets
# pool_pre_ping=True, so terminating a pooled connection recycles transparently
# rather than erroring on the next query.
_ALL_ROLE_BACKENDS_SQL = """
    SELECT pid
    FROM pg_stat_activity
    WHERE usename = $1 AND pid <> pg_backend_pid()
"""

# Backends actually RUNNING a statement right now. This is the COUNT set, and
# it must stay narrower than the kill set: a healthy connection pool parks idle
# connections on this role between scenarios, and counting those as "active"
# made demo/selfcheck.py report a stray backend when nothing was wrong. A
# pre-flight check that cries wolf minutes before a meeting is worse than no
# check at all.
_RUNNING_BACKENDS_SQL = """
    SELECT pid
    FROM pg_stat_activity
    WHERE usename = $1 AND pid <> pg_backend_pid() AND state = 'active'
"""


async def get_admin_connection(
    *, timeout: float = 5, command_timeout: float = config.ADMIN_CONN_COMMAND_TIMEOUT_SECONDS
) -> asyncpg.Connection:
    """Open one admin (pitch_owner) connection. `command_timeout` bounds
    every individual query issued on this connection (asyncpg default), so a
    caller holding this connection across a long operation still can't hang
    indefinitely on any one statement.

    F5: callers that need to act on the database at a moment it may be
    saturated (the overload scenario's kill sweep) should call this BEFORE
    the saturating work starts and hold the connection for the duration,
    rather than opening a fresh connection once saturation has already
    begun — see run_overload in scenarios.py."""
    return await asyncpg.connect(
        config.PG_ADMIN_DSN, timeout=timeout, command_timeout=command_timeout
    )


async def snapshot_agent_ro_calls(
    role: str = config.AGENT_RO_ROLE, *, conn: Optional[asyncpg.Connection] = None
) -> int:
    """Real, live pg_stat_statements sum(calls) for the given role. Cheap
    (single indexed-ish aggregate over a small catalog view) and safe to call
    immediately before and after every scenario.

    `conn`: an already-open admin connection to reuse instead of opening a
    new one (see get_admin_connection's F5 note) — optional, defaults to
    opening (and closing) its own for the common case."""
    owns_conn = conn is None
    if conn is None:
        conn = await get_admin_connection()
    try:
        row = await conn.fetchrow(_AGENT_RO_CALLS_SQL, role)
        return int(row["n"])
    finally:
        if owns_conn:
            await conn.close()


async def count_active_role_backends(
    role: str = config.AGENT_RO_ROLE, *, conn: Optional[asyncpg.Connection] = None
) -> int:
    """How many `role` backends are active right now. Wired into
    `/api/state`'s `db_calls.agent_ro_active` (app.py) — review found
    `demo/selfcheck.py`'s pre-flight go/no-go check already reads exactly
    this key and silently defaults to 0 when it's absent, which had made
    that check permanently green regardless of whether a stray backend was
    actually still running. `conn`: reuse an already-open admin connection
    (see get_admin_connection's F5 note) instead of opening a new one."""
    owns_conn = conn is None
    if conn is None:
        conn = await get_admin_connection()
    try:
        rows = await conn.fetch(_RUNNING_BACKENDS_SQL, role)
        return len(rows)
    finally:
        if owns_conn:
            await conn.close()


async def reset_query_stats() -> None:
    """SELECT pg_stat_statements_reset() as pitch_owner (SPEC.md F8: /api/reset
    "clears counters"). Independent of kill_role_backends — resetting the
    counters never terminates a running query, and terminating a query never
    resets the counters."""
    conn = await get_admin_connection()
    try:
        await conn.execute("SELECT pg_stat_statements_reset()")
    finally:
        await conn.close()


async def kill_role_backends(
    role: str = config.AGENT_RO_ROLE, *, conn: Optional[asyncpg.Connection] = None
) -> list[int]:
    """pg_terminate_backend every active backend for `role`. Used both by the
    overload scenario (post 15s wall-clock bound) and by /api/reset, so a
    stray backend can never survive between demo runs. Returns the pids that
    were targeted (best-effort — a backend that finished on its own between
    the SELECT and the terminate call is not an error).

    `conn`: an already-open admin connection to reuse (F5) — pass the
    connection opened before a saturating fan-out started, so this sweep
    never has to open a NEW connection against a database that is, by
    construction, under load at that exact moment. Defaults to opening
    (and closing) its own for callers like /api/reset that run when the
    database is otherwise idle."""
    owns_conn = conn is None
    if conn is None:
        conn = await get_admin_connection()
    try:
        rows = await conn.fetch(_ALL_ROLE_BACKENDS_SQL, role)
        pids = [r["pid"] for r in rows]
        for pid in pids:
            try:
                await conn.fetchval("SELECT pg_terminate_backend($1)", pid)
            except Exception:  # noqa: BLE001 - best-effort, log and continue
                logger.warning("pg_terminate_backend(%s) failed", pid, exc_info=True)
        return pids
    finally:
        if owns_conn:
            await conn.close()


# ---------------------------------------------------------------------------
# Background probe — its own dedicated connection, its own role
# (probe_user), so its traffic is never confused with agent_ro traffic in
# pg_stat_statements (demo/SPEC.md). Runs continuously and fans results out
# to every subscribed SSE client via per-subscriber asyncio.Queues.
# ---------------------------------------------------------------------------


@dataclass
class ProbePoint:
    t: str
    ms: float
    hot: bool

    def to_json(self) -> str:
        return json.dumps({"t": self.t, "ms": self.ms, "hot": self.hot})


class ProbeBroadcaster:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self.latest: Optional[ProbePoint] = None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=16)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _publish(self, point: ProbePoint) -> None:
        self.latest = point
        for q in list(self._subscribers):
            try:
                q.put_nowait(point)
            except asyncio.QueueFull:
                # Slow consumer — drop the oldest, keep the stream live.
                try:
                    q.get_nowait()
                    q.put_nowait(point)
                except asyncio.QueueEmpty:
                    pass

    async def run(self) -> None:
        conn: Optional[asyncpg.Connection] = None
        while not self._stop.is_set():
            try:
                if conn is None or conn.is_closed():
                    conn = await asyncpg.connect(config.PG_PROBE_DSN, timeout=5)
                t0 = time.perf_counter()
                await conn.fetchval(config.PROBE_QUERY)
                ms = (time.perf_counter() - t0) * 1000.0
                point = ProbePoint(
                    t=datetime.now(timezone.utc).isoformat(),
                    ms=round(ms, 2),
                    hot=ms > config.PROBE_HOT_THRESHOLD_MS,
                )
                self._publish(point)
            except Exception:  # noqa: BLE001
                # Never fabricate a point on failure — skip this tick, try to
                # reconnect next time, and log so it isn't silent.
                logger.warning("probe tick failed", exc_info=True)
                if conn is not None:
                    try:
                        await conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                conn = None

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=config.PROBE_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass

        if conn is not None:
            try:
                await conn.close()
            except Exception:  # noqa: BLE001
                pass

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self.run(), name="probe-broadcaster")

    async def stop(self) -> None:
        # F12: bound the shutdown wait so Ctrl-C mid-overload (the probe
        # loop is mid-await on a saturated Postgres connect/query) can't
        # make the process appear to hang forever. asyncio.wait_for's
        # timeout path does not reliably finish cancelling a real Task in
        # every asyncio version, so cancel explicitly and re-await as a
        # fallback rather than relying on wait_for alone.
        self._stop.set()
        task = self._task
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=config.PROBE_STOP_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning(
                "probe broadcaster did not stop within %ss — cancelling",
                config.PROBE_STOP_TIMEOUT_SECONDS,
            )
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            self._task = None


async def sse_stream(broadcaster: ProbeBroadcaster) -> AsyncIterator[str]:
    """Yield SSE frames for one subscriber. Frontend expects a plain
    `data: {...}` line per event (no `event:` field required)."""
    q = broadcaster.subscribe()
    try:
        if broadcaster.latest is not None:
            yield f"data: {broadcaster.latest.to_json()}\n\n"
        while True:
            point: ProbePoint = await q.get()
            yield f"data: {point.to_json()}\n\n"
    finally:
        broadcaster.unsubscribe(q)


async def health_check_db() -> bool:
    try:
        conn = await asyncpg.connect(config.PG_PROBE_DSN, timeout=3)
        try:
            await conn.fetchval("SELECT 1")
            return True
        finally:
            await conn.close()
    except Exception:  # noqa: BLE001
        return False
