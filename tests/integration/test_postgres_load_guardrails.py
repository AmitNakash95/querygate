"""Concurrent-load verification for QueryGate's execution guardrails.

Unlike ``tests/unit/test_concurrency.py``, this test drives simultaneous REST
requests through the real service/compiler/session path and observes the
queries PostgreSQL reports as active.  It therefore proves the configured cap
at the database boundary rather than inferring it from client task counts.

Run ``make compose-up`` first, then ``make test-load``.  ``make test-soak``
uses the same assertions for many rounds.  Both commands are excluded from
the default suite because they need the real demo PostgreSQL instance.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import Counter
from dataclasses import dataclass

import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from querygate.api.app import create_app
from querygate.connections.engine import ENGINES, dispose_engine, get_engine
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.core.config import AppConfig
from querygate.execution.concurrency import SEMAPHORES
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.schema.reflection import get_table_schema

pytestmark = [
    pytest.mark.integration,
    pytest.mark.real_db,
    pytest.mark.postgres_live,
    pytest.mark.load,
]

_BASE_URL = "http://localhost"
_CONNECTION_ID = "postgres_load"
_CONNECTION_STRING = os.environ.get(
    "QUERYGATE_TEST_POSTGRES_URL",
    "postgresql+asyncpg://querygate:querygate@localhost:5433/querygate_demo",
)
_RUN_ID = f"{os.getpid()}_{uuid.uuid4().hex[:8]}"
_SHORT_PROBE = f"querygate_load_short_{_RUN_ID}"
_TIMEOUT_PROBE = f"querygate_load_timeout_{_RUN_ID}"
_PROBE_FUNCTION = f"querygate_load_sleep_{_RUN_ID}"
_SHORT_DELAY_SECONDS = 0.35
_TIMEOUT_DELAY_SECONDS = 4
_CAP = 2
_OVERFLOW_REQUESTS = 8
_QUEUED_REQUESTS = 6


@dataclass(frozen=True)
class LoadRun:
    responses: list[Response]
    peak_database_queries: int
    elapsed_seconds: float


def _load_rounds() -> int:
    value = os.environ.get("QUERYGATE_LOAD_ROUNDS", "3")
    try:
        rounds = int(value)
    except ValueError as exc:
        raise pytest.UsageError("QUERYGATE_LOAD_ROUNDS must be a positive integer") from exc
    if rounds < 1:
        raise pytest.UsageError("QUERYGATE_LOAD_ROUNDS must be a positive integer")
    return rounds


def _set_load_policy(*, wait_seconds: float, timeout_seconds: int) -> None:
    set_policy_store(
        PolicyStore(
            default=Policy(
                allowed_tables=[_SHORT_PROBE, _TIMEOUT_PROBE],
                max_concurrency=_CAP,
                concurrency_wait_seconds=wait_seconds,
                timeout_seconds=timeout_seconds,
                max_limit=1,
                default_limit=1,
            ),
            overrides={},
        )
    )
    # Policy changes are normally applied through config_reload(), which
    # clears the cached semaphore.  The load harness swaps an in-memory store
    # directly, so mirror that part of reload semantics explicitly.
    SEMAPHORES.clear()


async def _create_probes(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(sa.text(f"DROP VIEW IF EXISTS {_SHORT_PROBE}"))
        await conn.execute(sa.text(f"DROP VIEW IF EXISTS {_TIMEOUT_PROBE}"))
        await conn.execute(
            sa.text(
                f"""
                CREATE OR REPLACE FUNCTION {_PROBE_FUNCTION}(delay_seconds double precision)
                RETURNS TABLE(id integer)
                LANGUAGE plpgsql
                VOLATILE
                AS $probe$
                BEGIN
                    PERFORM pg_sleep(delay_seconds);
                    RETURN QUERY SELECT 1;
                END;
                $probe$
                """
            )
        )
        await conn.execute(
            sa.text(
                f"CREATE VIEW {_SHORT_PROBE} AS "
                f"SELECT id FROM {_PROBE_FUNCTION}({_SHORT_DELAY_SECONDS})"
            )
        )
        await conn.execute(
            sa.text(
                f"CREATE VIEW {_TIMEOUT_PROBE} AS "
                f"SELECT id FROM {_PROBE_FUNCTION}({_TIMEOUT_DELAY_SECONDS})"
            )
        )


async def _drop_probes(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(sa.text(f"DROP VIEW IF EXISTS {_SHORT_PROBE}"))
        await conn.execute(sa.text(f"DROP VIEW IF EXISTS {_TIMEOUT_PROBE}"))
        await conn.execute(sa.text(f"DROP FUNCTION IF EXISTS {_PROBE_FUNCTION}(double precision)"))


@pytest_asyncio.fixture
async def postgres_load_app():
    admin_engine = create_async_engine(_CONNECTION_STRING, pool_pre_ping=True)
    probes_created = False
    try:
        await _create_probes(admin_engine)
        probes_created = True

        set_registry(
            ConnectionRegistry(
                {
                    _CONNECTION_ID: ConnectionProfile(
                        id=_CONNECTION_ID,
                        dialect="postgresql",
                        connection_string=_CONNECTION_STRING,
                        known_tables=[_SHORT_PROBE, _TIMEOUT_PROBE],
                    )
                }
            )
        )
        _set_load_policy(wait_seconds=0.1, timeout_seconds=5)
        app = create_app(
            AppConfig(environment="localhost", mcp_enabled=False, audit_sink_backend="none")
        )

        yield app, admin_engine
    finally:
        # Drop QueryGate's pooled connections before removing the views they
        # reflected.  reset_engines() alone only drops references and would
        # leak real asyncpg connections in this real-database suite.
        for connection_id in list(ENGINES):
            await dispose_engine(connection_id)
        try:
            if probes_created:
                await _drop_probes(admin_engine)
        finally:
            await admin_engine.dispose()


async def _active_probe_queries(conn: AsyncConnection, probe: str) -> int:
    result = await conn.execute(
        sa.text(
            """
            SELECT count(*)
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND state = 'active'
              AND pid <> pg_backend_pid()
              AND position(:probe in query) > 0
            """
        ),
        {"probe": probe},
    )
    active = int(result.scalar_one())
    # PostgreSQL caches cumulative-statistics views until the current
    # transaction ends.  Commit this read-only sample so the next poll gets a
    # fresh pg_stat_activity snapshot instead of replaying the initial zero.
    await conn.commit()
    return active


async def _observe_database_peak(
    engine: AsyncEngine,
    probe: str,
    stop: asyncio.Event,
    ready: asyncio.Event,
) -> int:
    peak = 0
    async with engine.connect() as conn:
        while not stop.is_set():
            peak = max(peak, await _active_probe_queries(conn, probe))
            ready.set()
            await asyncio.sleep(0.01)
        # One last sample closes the small race between the final loop
        # condition and the caller setting stop.
        peak = max(peak, await _active_probe_queries(conn, probe))
    return peak


def _query_payload(probe: str) -> dict:
    return {"from": probe, "select": [f"{probe}.id"], "limit": 1}


async def _run_load(
    client: AsyncClient,
    monitor_engine: AsyncEngine,
    *,
    probe: str,
    request_count: int,
) -> LoadRun:
    stop = asyncio.Event()
    ready = asyncio.Event()
    observer = asyncio.create_task(_observe_database_peak(monitor_engine, probe, stop, ready))
    await ready.wait()

    start = time.monotonic()
    try:
        responses = await asyncio.gather(
            *[
                client.post(
                    f"/api/v1/{_CONNECTION_ID}/query",
                    json=_query_payload(probe),
                )
                for _ in range(request_count)
            ]
        )
    finally:
        stop.set()
    elapsed_seconds = time.monotonic() - start
    peak = await observer
    return LoadRun(
        responses=responses,
        peak_database_queries=peak,
        elapsed_seconds=elapsed_seconds,
    )


def _status_counts(run: LoadRun) -> Counter:
    return Counter(response.status_code for response in run.responses)


def _assert_concurrency_rejections(run: LoadRun, expected: int) -> None:
    rejected = [response for response in run.responses if response.status_code == 422]
    assert len(rejected) == expected, [response.text for response in run.responses]
    assert all("too many concurrent" in response.json()["detail"] for response in rejected)


@pytest.mark.asyncio
async def test_concurrency_and_timeout_guardrails_under_load(postgres_load_app):
    app, monitor_engine = postgres_load_app
    rounds = _load_rounds()

    # Reflect both views before timing starts.  The tested requests still go
    # through schema validation, but this keeps first-use catalog I/O from
    # obscuring the execution-concurrency measurement.
    await get_table_schema(_SHORT_PROBE, _CONNECTION_ID, get_engine(_CONNECTION_ID))
    await get_table_schema(_TIMEOUT_PROBE, _CONNECTION_ID, get_engine(_CONNECTION_ID))

    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        for _ in range(rounds):
            # A short wait means only the first CAP requests may run; every
            # excess request must get QueryGate's documented 422 rejection.
            _set_load_policy(wait_seconds=0.1, timeout_seconds=5)
            overflow = await _run_load(
                client,
                monitor_engine,
                probe=_SHORT_PROBE,
                request_count=_OVERFLOW_REQUESTS,
            )
            assert _status_counts(overflow) == Counter({200: _CAP, 422: _OVERFLOW_REQUESTS - _CAP})
            _assert_concurrency_rejections(overflow, _OVERFLOW_REQUESTS - _CAP)
            assert overflow.peak_database_queries == _CAP

            # With a long wait, the same over-cap burst should queue in waves
            # and complete without ever raising PostgreSQL's observed peak.
            _set_load_policy(wait_seconds=3, timeout_seconds=5)
            queued = await _run_load(
                client,
                monitor_engine,
                probe=_SHORT_PROBE,
                request_count=_QUEUED_REQUESTS,
            )
            assert _status_counts(queued) == Counter({200: _QUEUED_REQUESTS})
            assert queued.peak_database_queries == _CAP

        # The timeout probe sleeps for four seconds, but the active policy
        # allows one.  Under the same over-cap burst, two running queries must
        # be cancelled near one second while the other two reject at the
        # concurrency boundary.  The lower bound catches unrelated immediate
        # failures; the upper bound catches a broken statement timeout.
        _set_load_policy(wait_seconds=0.1, timeout_seconds=1)
        timed_out = await _run_load(
            client,
            monitor_engine,
            probe=_TIMEOUT_PROBE,
            request_count=_CAP * 2,
        )
        assert _status_counts(timed_out) == Counter({500: _CAP, 422: _CAP})
        _assert_concurrency_rejections(timed_out, _CAP)
        assert timed_out.peak_database_queries == _CAP
        assert 0.8 <= timed_out.elapsed_seconds < (_TIMEOUT_DELAY_SECONDS - 1)

        async with monitor_engine.connect() as conn:
            assert await _active_probe_queries(conn, _TIMEOUT_PROBE) == 0


# --- Agent-visible capacity waiting (TODO.md item 35 phase 1) --------------


async def _fill_capacity(client: AsyncClient) -> list[asyncio.Task]:
    """Launch _CAP concurrent short-probe requests and give them long enough
    to actually acquire their concurrency slots before the caller proceeds.
    """
    tasks = [
        asyncio.create_task(
            client.post(f"/api/v1/{_CONNECTION_ID}/query", json=_query_payload(_SHORT_PROBE))
        )
        for _ in range(_CAP)
    ]
    await asyncio.sleep(0.05)
    return tasks


@pytest.mark.asyncio
async def test_fail_fast_never_waits_even_though_capacity_frees_up_shortly(postgres_load_app):
    """queue_mode=fail_fast must reject immediately. A long policy ceiling
    (3s) that's well past the occupiers' ~0.35s probe duration would let a
    caller that merely *waited* eventually succeed — fail_fast must not do
    that.
    """
    app, monitor_engine = postgres_load_app
    await get_table_schema(_SHORT_PROBE, _CONNECTION_ID, get_engine(_CONNECTION_ID))
    _set_load_policy(wait_seconds=3, timeout_seconds=5)

    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        occupiers = await _fill_capacity(client)

        start = time.monotonic()
        resp = await client.post(
            f"/api/v1/{_CONNECTION_ID}/query",
            json=_query_payload(_SHORT_PROBE),
            params={"queue_mode": "fail_fast"},
        )
        elapsed = time.monotonic() - start

        occupier_responses = await asyncio.gather(*occupiers)

    assert all(r.status_code == 200 for r in occupier_responses)
    assert resp.status_code == 422
    assert "too many concurrent" in resp.json()["detail"]
    assert resp.headers["X-QueryGate-Admission-State"] == "capacity_timeout"
    assert resp.headers.get("X-QueryGate-Admission-Id")
    # Well under the occupiers' ~0.35s probe duration and nowhere near the 3s
    # policy ceiling — proves fail_fast never actually waited.
    assert elapsed < 0.2


@pytest.mark.asyncio
async def test_wait_timeout_seconds_is_honored_when_shorter_than_policy_ceiling(
    postgres_load_app,
):
    """A caller-selected wait shorter than the operator's ceiling must be
    honored (rejected around the caller's own deadline), not silently
    extended to the full policy ceiling.
    """
    app, monitor_engine = postgres_load_app
    await get_table_schema(_SHORT_PROBE, _CONNECTION_ID, get_engine(_CONNECTION_ID))
    # Ceiling (3s) is long enough for the occupiers to finish (~0.35s), but
    # the caller asks for a much shorter wait.
    _set_load_policy(wait_seconds=3, timeout_seconds=5)

    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        occupiers = await _fill_capacity(client)

        start = time.monotonic()
        resp = await client.post(
            f"/api/v1/{_CONNECTION_ID}/query",
            json=_query_payload(_SHORT_PROBE),
            params={"queue_mode": "wait", "wait_timeout_seconds": "0.1"},
        )
        elapsed = time.monotonic() - start

        await asyncio.gather(*occupiers)

    assert resp.status_code == 422
    assert resp.headers["X-QueryGate-Admission-State"] == "capacity_timeout"
    # Honored the caller's shorter 0.1s wait, not the operator's 3s ceiling.
    assert 0.05 <= elapsed < 0.3


@pytest.mark.asyncio
async def test_capacity_released_before_callers_deadline_still_succeeds(postgres_load_app):
    """A caller-selected wait that's shorter than the ceiling but still long
    enough to outlast the occupiers must succeed once capacity frees up —
    proving the shortened wait still queues, it doesn't just fail_fast.
    """
    app, monitor_engine = postgres_load_app
    await get_table_schema(_SHORT_PROBE, _CONNECTION_ID, get_engine(_CONNECTION_ID))
    _set_load_policy(wait_seconds=3, timeout_seconds=5)

    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        occupiers = await _fill_capacity(client)

        resp = await client.post(
            f"/api/v1/{_CONNECTION_ID}/query",
            json=_query_payload(_SHORT_PROBE),
            params={"queue_mode": "wait", "wait_timeout_seconds": "2"},
        )

        await asyncio.gather(*occupiers)

    assert resp.status_code == 200
    assert resp.headers["X-QueryGate-Admission-State"] == "completed"
    assert int(resp.headers["X-QueryGate-Queue-Wait-Ms"]) > 0


@pytest.mark.asyncio
async def test_successful_query_carries_admission_headers_under_real_load(postgres_load_app):
    app, monitor_engine = postgres_load_app
    await get_table_schema(_SHORT_PROBE, _CONNECTION_ID, get_engine(_CONNECTION_ID))
    _set_load_policy(wait_seconds=1, timeout_seconds=5)

    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post(
            f"/api/v1/{_CONNECTION_ID}/query", json=_query_payload(_SHORT_PROBE)
        )

    assert resp.status_code == 200
    assert resp.headers["X-QueryGate-Admission-State"] == "completed"
    assert resp.headers.get("X-QueryGate-Admission-Id")
    assert resp.headers.get("X-QueryGate-Queue-Wait-Ms") is not None
    body = resp.json()
    assert body["admission_id"] == resp.headers["X-QueryGate-Admission-Id"]
