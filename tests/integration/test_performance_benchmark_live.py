"""Real-Postgres smoke test for the performance benchmark (see
``querygate.performance_benchmark`` and ``docs/business/PERFORMANCE_BENCHMARK.md``).

Run with ``make compose-up`` then ``poetry run pytest -m postgres_live
tests/integration/test_performance_benchmark_live.py``, or via
``make test-postgres-live``. This does not assert on absolute latency
numbers (hardware-dependent, and the point of the benchmark is to report
them, not gate on them) — it asserts the harness produces a well-formed
report against a real database and cleans up its own fixture table.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from querygate.audit.sinks import get_audit_sink
from querygate.connections.registry import get_registry
from querygate.performance_benchmark import DEFAULT_CONNECTION_STRING, run_performance_benchmark
from querygate.policy.loader import get_policy_store

pytestmark = [pytest.mark.integration, pytest.mark.real_db, pytest.mark.postgres_live]

_CONNECTION_STRING = os.environ.get("QUERYGATE_TEST_POSTGRES_URL", DEFAULT_CONNECTION_STRING)


@pytest.mark.asyncio
async def test_benchmark_report_is_well_formed_against_real_postgres():
    # 8 iterations, not 3: the REST tier must never average faster than the
    # raw-SQL baseline (it does strictly more work for the same query), but
    # a too-small sample gives pure timer/scheduling noise enough leverage to
    # flip a mean on a busy box (2026-08-11 test-contract-reviewer finding).
    report = await run_performance_benchmark(
        connection_string=_CONNECTION_STRING, rows=200, iterations=8, warmup=2
    )

    assert report.seeded_rows == 200
    assert report.dialect == "postgresql"
    assert len(report.scenarios) == 3
    for scenario in report.scenarios:
        assert scenario.iterations == 8
        assert scenario.baseline.samples == 8
        assert scenario.pipeline.samples == 8
        assert scenario.rest.samples == 8
        # Every tier did real work and took a non-negative, finite amount of time.
        assert scenario.baseline.mean_ms >= 0.0
        assert scenario.pipeline.mean_ms >= 0.0
        assert scenario.rest.mean_ms >= 0.0
        # The REST tier goes through strictly more machinery than raw SQL
        # (validation, reflection, compilation, HTTP) — it must never be
        # meaningfully faster on average, or the benchmark would be
        # measuring nothing. A small epsilon absorbs pure timer noise
        # without weakening the intent of the check.
        assert scenario.rest.mean_ms >= scenario.baseline.mean_ms - 0.5


@pytest.mark.asyncio
async def test_benchmark_drops_its_fixture_table_on_success():
    engine = create_async_engine(_CONNECTION_STRING, pool_pre_ping=True)
    try:
        before = await _bench_table_count(engine)
        await run_performance_benchmark(
            connection_string=_CONNECTION_STRING, rows=50, iterations=1, warmup=0
        )
        after = await _bench_table_count(engine)
        assert after == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_benchmark_drops_its_fixture_table_even_on_a_mid_run_failure():
    """The `finally` block's table drop must not depend on the run
    completing successfully (2026-08-11 test-contract-reviewer /
    claim-reviewer finding: the module docstring and published docs both
    claim cleanup happens "regardless of outcome," but only the success path
    was ever exercised)."""
    engine = create_async_engine(_CONNECTION_STRING, pool_pre_ping=True)
    try:
        before = await _bench_table_count(engine)
        with patch(
            "querygate.performance_benchmark._time_baseline",
            side_effect=RuntimeError("forced failure for cleanup test"),
        ):
            with pytest.raises(RuntimeError, match="forced failure"):
                await run_performance_benchmark(
                    connection_string=_CONNECTION_STRING, rows=50, iterations=1, warmup=0
                )
        after = await _bench_table_count(engine)
        assert after == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_benchmark_restores_registry_policy_store_and_audit_sink_afterward():
    """2026-08-11 architecture-boundary-reviewer / security-invariant-reviewer
    finding: earlier versions permanently overwrote the process-global
    connection registry, policy store, and audit sink for the rest of the
    process's life — invisible only because the CLI always exits right
    after. Asserts identity (`is`, not just "non-empty"), so a fix that
    restores a fresh-but-different object wouldn't pass this by accident."""
    sentinel_registry = get_registry()
    sentinel_policy_store = get_policy_store()
    sentinel_audit_sink = get_audit_sink()

    await run_performance_benchmark(
        connection_string=_CONNECTION_STRING, rows=50, iterations=1, warmup=0
    )

    assert get_registry() is sentinel_registry
    assert get_policy_store() is sentinel_policy_store
    assert get_audit_sink() is sentinel_audit_sink


async def _bench_table_count(engine) -> int:
    async with engine.connect() as conn:
        result = await conn.execute(
            sa.text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name LIKE 'querygate_perf_bench_%'"
            )
        )
        return int(result.scalar_one())
