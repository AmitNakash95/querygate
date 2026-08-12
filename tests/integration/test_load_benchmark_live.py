"""Real-Postgres, real-subprocess smoke test for the load benchmark (see
``querygate.load_benchmark`` and ``docs/business/LOAD_BENCHMARK.md``).

Run with ``make compose-up`` then ``poetry run pytest -m postgres_live
tests/integration/test_load_benchmark_live.py``, or via
``make test-postgres-live``. Unlike the single-request benchmark's live
test, this one spawns real `uvicorn` subprocesses bound to real localhost
ports — kept deliberately small (few workers, few concurrency levels, few
rows) to keep CI runtime bounded. Does not assert on absolute throughput/
latency numbers (hardware-dependent) — it asserts the harness produces a
well-formed report against a real database and a real server, actually
bounds concurrency, and cleans up its table/config/subprocess regardless of
outcome.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from querygate.load_benchmark import DEFAULT_CONNECTION_STRING, run_load_benchmark

pytestmark = [pytest.mark.integration, pytest.mark.real_db, pytest.mark.postgres_live]

_CONNECTION_STRING = os.environ.get("QUERYGATE_TEST_POSTGRES_URL", DEFAULT_CONNECTION_STRING)


@pytest.mark.asyncio
async def test_load_benchmark_report_is_well_formed_against_a_real_server():
    report = await run_load_benchmark(
        connection_string=_CONNECTION_STRING,
        rows=50,
        concurrency_levels=[1, 4],
        worker_counts=[1],
        requests_per_slot=3,
    )

    assert report.dialect == "postgresql"
    assert report.seeded_rows == 50
    # Proves the policy concurrency cap was actually raised above the peak
    # level tested (4), not left at Policy's own default of 8.
    assert report.max_concurrency > 4
    assert [sweep.workers for sweep in report.sweeps] == [1]

    sweep = report.sweeps[0]
    assert [level.concurrency for level in sweep.levels] == [1, 4]
    for level in sweep.levels:
        expected_requests = level.concurrency * 3
        assert level.requests == expected_requests
        # A clean run against a real server: no admission rejections, no
        # connection errors (the cap and pools are sized to guarantee this).
        assert level.baseline.errors == 0
        assert level.rest.errors == 0
        assert level.baseline.latency.samples == expected_requests
        assert level.rest.latency.samples == expected_requests
        assert level.baseline.throughput_rps > 0.0
        assert level.rest.throughput_rps > 0.0


@pytest.mark.asyncio
async def test_load_benchmark_sweeps_multiple_worker_counts_in_order():
    report = await run_load_benchmark(
        connection_string=_CONNECTION_STRING,
        rows=50,
        concurrency_levels=[1, 2],
        worker_counts=[2, 1],
        requests_per_slot=2,
    )
    # Deduplicated and sorted, same convention as concurrency_levels.
    assert [sweep.workers for sweep in report.sweeps] == [1, 2]
    for sweep in report.sweeps:
        assert [level.concurrency for level in sweep.levels] == [1, 2]


@pytest.mark.asyncio
async def test_load_benchmark_drops_its_fixture_table_on_success():
    engine = create_async_engine(_CONNECTION_STRING, pool_pre_ping=True)
    try:
        before = await _bench_table_count(engine)
        await run_load_benchmark(
            connection_string=_CONNECTION_STRING,
            rows=50,
            concurrency_levels=[1],
            worker_counts=[1],
            requests_per_slot=2,
        )
        after = await _bench_table_count(engine)
        assert after == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_load_benchmark_drops_its_fixture_table_even_when_the_server_never_starts():
    """The `finally` block's table drop and temp-config cleanup must not
    depend on the server successfully starting — forces `_wait_ready` to
    fail (a real, reachable failure mode: a misbehaving app, a port fight,
    a bad config) after the table already exists, and confirms cleanup
    still happens."""
    from unittest.mock import patch

    engine = create_async_engine(_CONNECTION_STRING, pool_pre_ping=True)
    try:
        before = await _bench_table_count(engine)
        with patch(
            "querygate.load_benchmark._wait_ready",
            side_effect=RuntimeError("forced failure for cleanup test"),
        ):
            with pytest.raises(RuntimeError, match="forced failure"):
                await run_load_benchmark(
                    connection_string=_CONNECTION_STRING,
                    rows=50,
                    concurrency_levels=[1],
                    worker_counts=[1],
                    requests_per_slot=2,
                )
        after = await _bench_table_count(engine)
        assert after == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_load_benchmark_leaves_no_server_process_or_bound_port_behind():
    """A leaked `uvicorn` subprocess (or a port left bound) would silently
    accumulate across repeated benchmark runs — this proves teardown
    actually kills the process group, not just that the Python call
    returned."""
    if shutil.which("pgrep") is None:
        pytest.skip("pgrep not available on this system")
    before_pids = _uvicorn_pids()
    await run_load_benchmark(
        connection_string=_CONNECTION_STRING,
        rows=50,
        concurrency_levels=[1],
        worker_counts=[1],
        requests_per_slot=2,
    )
    after_pids = _uvicorn_pids()
    assert after_pids <= before_pids


def _uvicorn_pids() -> set[str]:
    result = subprocess.run(
        ["pgrep", "-f", "uvicorn querygate.api.app:app"],
        capture_output=True,
        text=True,
    )
    return set(result.stdout.split())


async def _bench_table_count(engine) -> int:
    async with engine.connect() as conn:
        result = await conn.execute(
            sa.text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name LIKE 'querygate_perf_bench_%'"
            )
        )
        return int(result.scalar_one())
