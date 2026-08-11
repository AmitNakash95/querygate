"""Tests for the load benchmark's non-DB, non-subprocess surface.

The benchmark itself needs a real Postgres instance and spawns real
`uvicorn` subprocesses (see ``tests/integration/test_load_benchmark_live.py``),
but the concurrency bounding, error tolerance, result math, and CLI wiring
are pure and worth locking in here so a bad edit is caught without
`make compose-up`.
"""

from __future__ import annotations

import argparse
import asyncio
import socket
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

import querygate.load_benchmark
from querygate.load_benchmark import (
    DEFAULT_CONNECTION_STRING,
    _ADMIN_ENGINE_RESERVE,
    _CONNECTION_BUDGET,
    _MAX_CONCURRENCY,
    _MAX_WORKERS,
    ConcurrencyLevelResult,
    LoadBenchmarkReport,
    TierResult,
    WorkerSweepResult,
    _free_port,
    _pool_sizing_for_workers,
    _run_concurrent_batch,
    _run_level,
    _start_server,
    _stop_server,
    _wait_ready,
    run_load_benchmark,
)
from querygate.load_benchmark_cli import (
    DEFAULT_MARKDOWN_PATH,
    _cmd_run,
    _handle_termination_signal,
    _render_markdown,
    _scaling_caution,
    build_parser,
)
from querygate.performance_benchmark import LatencyStats

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_active_server_procs():
    """`_ACTIVE_SERVER_PROCS` is module-level singleton state backing the
    atexit orphan-cleanup safety net (see load_benchmark.py). A test that
    populates it and fails before its own manual cleanup line would
    otherwise leak a stale fake proc into every later test and into the
    real atexit handler at interpreter shutdown (2026-08-11
    test-contract-reviewer finding) — clear it before and after every test
    here, the same precedent tests/conftest.py's autouse fixture already
    sets for in_process_limiter()."""
    querygate.load_benchmark._ACTIVE_SERVER_PROCS.clear()
    yield
    querygate.load_benchmark._ACTIVE_SERVER_PROCS.clear()


def _stats(mean_ms: float) -> LatencyStats:
    return LatencyStats.from_samples([mean_ms])


def _tier(mean_ms: float, throughput_rps: float = 100.0, errors: int = 0) -> TierResult:
    return TierResult(throughput_rps=throughput_rps, latency=_stats(mean_ms), errors=errors)


def test_concurrency_level_result_overhead_and_throughput_ratio():
    level = ConcurrencyLevelResult(
        concurrency=10,
        requests=40,
        baseline=_tier(2.0, throughput_rps=100.0),
        rest=_tier(5.0, throughput_rps=60.0),
    )
    assert level.rest_overhead_ms == pytest.approx(3.0)
    assert level.throughput_ratio == pytest.approx(0.6)


def test_throughput_ratio_handles_zero_baseline_throughput():
    level = ConcurrencyLevelResult(
        concurrency=1,
        requests=1,
        baseline=_tier(1.0, throughput_rps=0.0),
        rest=_tier(2.0, throughput_rps=5.0),
    )
    assert level.throughput_ratio == 0.0


def test_latency_stats_handles_an_all_failed_batch():
    """load_benchmark tolerates per-request failures — an all-error batch
    must still produce a reportable (zeroed) stat, not raise (2026-08-11
    finding: statistics.fmean/IndexError on an empty sample list)."""
    stats = LatencyStats.from_samples([])
    assert stats.samples == 0
    assert stats.mean_ms == 0.0
    assert stats.p50_ms == 0.0
    assert stats.max_ms == 0.0


@pytest.mark.asyncio
async def test_run_concurrent_batch_bounds_in_flight_count():
    """The whole benchmark's throughput number is only meaningful if
    `concurrency` in-flight requests is actually enforced, not just
    requested — this proves the `asyncio.Semaphore` bound is real by
    tracking simultaneous in-flight calls directly, no DB/subprocess
    involved."""
    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def factory(index: int) -> float:
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        async with lock:
            in_flight -= 1
        return float(index)

    latencies, errors, elapsed = await _run_concurrent_batch(factory, concurrency=3, count=12)
    assert max_in_flight == 3
    assert errors == 0
    assert sorted(latencies) == [float(i) for i in range(12)]
    assert elapsed > 0.0


@pytest.mark.asyncio
async def test_run_concurrent_batch_tolerates_failures_instead_of_aborting():
    """A real load tool reports an error count, it doesn't abort the whole
    batch on the first failure — proves both halves: failures are counted,
    AND every other call in the batch still completes (2026-08-11 design
    change from the prior version, which used a plain `asyncio.gather` that
    would propagate the first exception and orphan in-flight siblings)."""

    async def factory(index: int) -> float:
        if index % 3 == 0:
            raise RuntimeError("simulated failure")
        return float(index)

    latencies, errors, elapsed = await _run_concurrent_batch(factory, concurrency=4, count=9)
    # indices 0, 3, 6 fail -> 3 errors, 6 successes
    assert errors == 3
    assert sorted(latencies) == [1.0, 2.0, 4.0, 5.0, 7.0, 8.0]


@pytest.mark.asyncio
async def test_run_concurrent_batch_all_failures_reports_zero_latencies():
    async def factory(index: int) -> float:
        raise RuntimeError("always fails")

    latencies, errors, elapsed = await _run_concurrent_batch(factory, concurrency=2, count=5)
    assert errors == 5
    assert latencies == []


def test_free_port_returns_a_bindable_port():
    port = _free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))  # raises OSError if not actually free


def test_start_server_neutralizes_dangerous_inherited_env(tmp_path, monkeypatch):
    """The spawned server must always be the bare, anonymous-auth, no-catalog
    config this benchmark was designed against — regardless of what the
    invoking shell/CI environment happens to export. A caller with
    JWT_ENABLED=true or a live CATALOG_FILE set in their own shell must not
    leak either into the benchmark's throwaway server."""
    monkeypatch.setenv("JWT_ENABLED", "true")
    monkeypatch.setenv("MCP_ENABLED", "true")
    monkeypatch.setenv("CATALOG_FILE", "/some/real/catalog.yaml")
    monkeypatch.setenv("TEMPLATES_FILE", "/some/real/templates.yaml")
    monkeypatch.setenv("SEMANTIC_MEMORY_REFRESH_ENABLED", "true")
    monkeypatch.setenv("SEMANTIC_MEMORY_LEARNING_ENABLED", "true")
    monkeypatch.setenv("SEMANTIC_MEMORY_USAGE_SIGNALS_ENABLED", "true")
    monkeypatch.setenv("CREDENTIAL_LEASE_REFRESH_ENABLED", "true")
    monkeypatch.setenv("CONCURRENCY_BACKEND", "redis")
    monkeypatch.setenv("METRICS_REQUIRE_AUTH", "false")
    monkeypatch.setenv("VAULT_ENABLED", "true")
    monkeypatch.setenv("VAULT_TOKEN", "s.somerealtoken")
    monkeypatch.setenv("API_V1_PREFIX", "/some/other/prefix")

    captured = {}

    class _FakeProc:
        pid = 1234

    def _fake_popen(argv, **kwargs):
        captured["env"] = kwargs["env"]
        return _FakeProc()

    with patch("subprocess.Popen", _fake_popen):
        _start_server(
            connection_string="postgresql://x",
            config_dir=tmp_path,
            port=59999,
            workers=1,
            pool_size=10,
            max_overflow=5,
        )
    # (cleanup of the tracked fake proc is handled by the autouse
    # _reset_active_server_procs fixture above)

    env = captured["env"]
    assert env["JWT_ENABLED"] == "false"
    assert env["MCP_ENABLED"] == "false"
    assert env["CATALOG_FILE"] == ""
    assert env["TEMPLATES_FILE"] == ""
    assert env["SEMANTIC_MEMORY_REFRESH_ENABLED"] == "false"
    assert env["SEMANTIC_MEMORY_LEARNING_ENABLED"] == "false"
    assert env["SEMANTIC_MEMORY_USAGE_SIGNALS_ENABLED"] == "false"
    assert env["CREDENTIAL_LEASE_REFRESH_ENABLED"] == "false"
    assert env["CONCURRENCY_BACKEND"] == "in_process"
    assert env["METRICS_REQUIRE_AUTH"] == "true"
    assert env["VAULT_ENABLED"] == "false"
    assert env["VAULT_TOKEN"] == ""
    assert env["API_V1_PREFIX"] == "/api/v1"


def test_start_server_tracks_and_stop_server_untracks_the_process(tmp_path):
    """`_ACTIVE_SERVER_PROCS` backs the atexit safety net that stops any
    server this module spawned but never got a chance to clean up itself
    (e.g. a KeyboardInterrupt landing between `_start_server()` returning and
    `run_load_benchmark`'s try/finally being entered). If `_start_server`
    stopped registering here, or `_stop_server` stopped unregistering, an
    interrupted run would either leak a live uvicorn process past interpreter
    exit, or the atexit handler would keep trying to stop an already-reaped
    process every run."""

    class _FakeProc:
        pid = 99999
        _polled = False

        def poll(self):
            return 0 if self._polled else None

    fake = _FakeProc()

    with patch("subprocess.Popen", lambda argv, **kwargs: fake):
        returned = _start_server(
            connection_string="postgresql://x",
            config_dir=tmp_path,
            port=59998,
            workers=1,
            pool_size=10,
            max_overflow=5,
        )

    assert returned is fake
    assert fake in querygate.load_benchmark._ACTIVE_SERVER_PROCS

    fake._polled = True  # simulate the process having already exited
    _stop_server(fake)

    assert fake not in querygate.load_benchmark._ACTIVE_SERVER_PROCS


@pytest.mark.asyncio
async def test_wait_ready_fails_fast_when_the_process_has_already_exited():
    """A server that crashes on startup (bad config, port collision) must be
    reported immediately as a process exit, not spun on until the readiness
    timeout expires with a generic "did not become ready" message."""

    class _DeadProc:
        def poll(self):
            return 1

    with pytest.raises(RuntimeError, match="process exited"):
        await _wait_ready(59997, _DeadProc(), timeout=5.0)


@pytest.mark.asyncio
async def test_wait_ready_rejects_a_200_from_a_different_service():
    """A stale process left bound to this port by a prior crashed run (or an
    unrelated service) must never be mistaken for this run's QueryGate
    server just because it answers 200 — the body's `service` field must
    also match."""

    class _AliveProc:
        def poll(self):
            return None

    fake_response = httpx.Response(200, json={"status": "ok", "service": "some-other-app"})

    async def _fake_get(self, url, timeout=1.0):
        return fake_response

    with patch.object(httpx.AsyncClient, "get", _fake_get):
        with pytest.raises(RuntimeError, match="did not become ready"):
            await _wait_ready(59996, _AliveProc(), timeout=0.3)


@pytest.mark.asyncio
async def test_wait_ready_returns_when_the_service_answers_ready():
    """Happy-path counterpart to the two negative-path tests above: a live
    process answering 200 with the correct `service` field must let
    `_wait_ready` return normally on the first poll, not just avoid raising
    eventually. Exercised only indirectly via the postgres_live integration
    tier otherwise (2026-08-11 test-contract-reviewer finding) — an
    accidentally-too-strict success condition would only be caught there,
    not by the fast `pytest -m unit` tier."""

    class _AliveProc:
        def poll(self):
            return None

    fake_response = httpx.Response(200, json={"status": "ok", "service": "querygate"})
    call_count = 0

    async def _fake_get(self, url, timeout=1.0):
        nonlocal call_count
        call_count += 1
        return fake_response

    with patch.object(httpx.AsyncClient, "get", _fake_get):
        await _wait_ready(59995, _AliveProc(), timeout=5.0)

    assert call_count == 1


@pytest.mark.parametrize("peak", [1, 30, 100, _MAX_CONCURRENCY])
@pytest.mark.parametrize("workers", list(range(1, _MAX_WORKERS + 1)))
def test_pool_sizing_never_exceeds_the_connection_budget(peak, workers):
    """The whole point of this formula: `workers * (pool_size + overflow)`
    must never exceed Postgres's own connection budget (90, leaving headroom
    under the demo container's max_connections=100), for every worker count
    this tool allows. An earlier version of this formula had ZERO test
    coverage and was caught only by mutation-testing it directly against a
    real Postgres — reverting it to the buggy shape left every test,
    including the real-Postgres integration suite, green (2026-08-11
    test-contract-reviewer finding, HIGH severity)."""
    pool_size, overflow = _pool_sizing_for_workers(peak, workers)
    assert pool_size >= 1
    assert overflow >= 1
    assert workers * (pool_size + overflow) <= 90


@pytest.mark.parametrize("peak", [1, 30, 100, _MAX_CONCURRENCY])
@pytest.mark.parametrize("workers", list(range(1, _MAX_WORKERS + 1)))
def test_whole_run_connection_count_never_exceeds_the_budget(peak, workers):
    """The property that actually matters end to end: baseline_engine's own
    pool (open for this whole run) plus admin_engine's default pool plus
    whichever worker-count's server pool is currently running must together
    stay under the connection budget — not just the per-worker-count sizing
    in isolation (2026-08-11 security-invariant-reviewer finding: a previous
    version sized baseline_engine as `peak_concurrency + 2` unbounded, which
    alone could reach 404 connections at this tool's own allowed ceiling,
    entirely outside what `test_pool_sizing_never_exceeds_the_connection_budget`
    above could ever catch, since that test never accounts for baseline or
    admin at all)."""
    baseline_pool, baseline_overflow = _pool_sizing_for_workers(peak, workers=1)
    reserved = baseline_pool + baseline_overflow + _ADMIN_ENGINE_RESERVE
    worker_pool, worker_overflow = _pool_sizing_for_workers(peak, workers, reserved=reserved)
    total = (
        baseline_pool
        + baseline_overflow
        + _ADMIN_ENGINE_RESERVE
        + workers * (worker_pool + worker_overflow)
    )
    assert total <= _CONNECTION_BUDGET


def test_pool_sizing_is_generous_when_it_comfortably_fits():
    """At low worker counts the formula shouldn't shrink pools just because
    it technically could — this is the specific regression that was caught:
    an earlier version divided a fixed budget by worker count even when
    plenty of headroom existed, self-starving workers=1 below the
    concurrency being tested (25 total connections available for 30
    concurrent requests)."""
    pool_size, overflow = _pool_sizing_for_workers(30, workers=1)
    assert pool_size + overflow >= 30  # must cover the full concurrency level tested
    pool_size, overflow = _pool_sizing_for_workers(30, workers=2)
    assert pool_size + overflow >= 15  # comfortably above each worker's ~fair share (15)


@pytest.mark.asyncio
async def test_throughput_is_computed_from_successful_requests_not_attempted():
    """A batch with failures must not report inflated throughput — failures
    return fast (a rejection, a refused connection), so counting them in the
    numerator would make a FAILING run look like the FASTEST one in the
    sweep (2026-08-11 security-invariant-reviewer finding: a fully-401ing
    REST tier would previously have reported the highest throughput of the
    entire report).

    Wall-clock time is faked deterministically (`time.perf_counter` patched
    with a fixed sequence) so the two possible numerators — successful count
    vs. attempted count — produce two DIFFERENT, exactly-predictable
    throughput values; a prior version of this test only asserted
    `throughput_rps > 0`, which both numerators would satisfy identically
    (2026-08-11 test-contract-reviewer finding)."""

    async def flaky_baseline(engine, table, index):
        if index % 2 == 0:
            raise RuntimeError("simulated failure")
        return 1.0

    async def flaky_rest(client, table, index):
        if index % 4 == 0:
            raise RuntimeError("simulated failure")
        return 1.0

    # _run_concurrent_batch calls time.perf_counter() twice per invocation
    # (start, then elapsed = perf_counter() - start), and _run_level invokes
    # it twice — baseline, then rest — in that fixed order: baseline spans
    # 0.0->1.0 (elapsed 1.0s), rest spans 1.0->3.0 (elapsed 2.0s).
    with (
        patch("querygate.load_benchmark._baseline_call", flaky_baseline),
        patch("querygate.load_benchmark._rest_call", flaky_rest),
        patch(
            "querygate.load_benchmark.time.perf_counter",
            side_effect=[0.0, 1.0, 1.0, 3.0],
        ),
    ):
        result = await _run_level(
            table="t",
            baseline_engine=object(),  # never touched — _baseline_call is patched
            client=object(),  # never touched — _rest_call is patched
            concurrency=4,
            requests_per_slot=5,
        )

    # 20 requests, half fail -> 10 successes over a faked 1.0s -> 10.0 req/s.
    # The rejected alternative (numerator = attempted count) would give 20.0.
    assert result.baseline.errors == 10
    assert result.baseline.latency.samples == 10
    assert result.baseline.throughput_rps == pytest.approx(10.0)
    # 20 requests, one in four fail -> 15 successes over a faked 2.0s -> 7.5 req/s.
    # The rejected alternative (numerator = attempted count) would give 10.0.
    assert result.rest.errors == 5
    assert result.rest.latency.samples == 15
    assert result.rest.throughput_rps == pytest.approx(7.5)


def test_report_construction_round_trips_through_json():
    report = LoadBenchmarkReport(
        dialect="postgresql",
        seeded_rows=100,
        requests_per_slot=4,
        max_concurrency=35,
        sweeps=[
            WorkerSweepResult(
                workers=1,
                levels=[
                    ConcurrencyLevelResult(
                        concurrency=1,
                        requests=4,
                        baseline=_tier(2.0, throughput_rps=200.0),
                        rest=_tier(4.0, throughput_rps=100.0),
                    )
                ],
            )
        ],
    )
    restored = LoadBenchmarkReport.model_validate_json(report.model_dump_json())
    assert restored == report


@pytest.mark.asyncio
async def test_empty_concurrency_levels_is_rejected_before_any_engine_work():
    with pytest.raises(ValueError, match="concurrency_levels"):
        await run_load_benchmark(concurrency_levels=[])


@pytest.mark.asyncio
async def test_non_positive_concurrency_level_is_rejected_before_any_engine_work():
    with pytest.raises(ValueError, match="at least 1"):
        await run_load_benchmark(concurrency_levels=[5, 0])
    with pytest.raises(ValueError, match="at least 1"):
        await run_load_benchmark(concurrency_levels=[-1])


@pytest.mark.asyncio
async def test_empty_worker_counts_is_rejected_before_any_engine_work():
    with pytest.raises(ValueError, match="worker_counts"):
        await run_load_benchmark(worker_counts=[])


@pytest.mark.asyncio
async def test_non_positive_worker_count_is_rejected_before_any_engine_work():
    with pytest.raises(ValueError, match="worker count"):
        await run_load_benchmark(worker_counts=[0])
    with pytest.raises(ValueError, match="worker count"):
        await run_load_benchmark(worker_counts=[-2])


@pytest.mark.asyncio
async def test_zero_requests_per_slot_is_rejected_before_any_engine_work():
    with pytest.raises(ValueError, match="requests_per_slot"):
        await run_load_benchmark(requests_per_slot=0)


@pytest.mark.asyncio
async def test_excessive_worker_count_is_rejected_before_any_engine_work():
    """`--workers 200` would fork 200 real uvicorn processes — an
    operator-controlled but still unwise self-DoS input (2026-08-11
    security-invariant-reviewer finding). Patches both `create_async_engine`
    and `subprocess.Popen` and asserts neither was even called, not just that
    a ValueError was eventually raised — a guard that fired AFTER engine
    construction (harmless, since AsyncEngine() is lazy) or after the
    subprocess spawn (a real self-DoS) would both still satisfy a bare
    `pytest.raises` (2026-08-11 test-contract-reviewer finding)."""
    with (
        patch("querygate.load_benchmark.create_async_engine") as mock_engine,
        patch("subprocess.Popen") as mock_popen,
        pytest.raises(ValueError, match="at most"),
    ):
        await run_load_benchmark(worker_counts=[_MAX_WORKERS + 1])
    mock_engine.assert_not_called()
    mock_popen.assert_not_called()


@pytest.mark.asyncio
async def test_excessive_concurrency_level_is_rejected_before_any_engine_work():
    with (
        patch("querygate.load_benchmark.create_async_engine") as mock_engine,
        patch("subprocess.Popen") as mock_popen,
        pytest.raises(ValueError, match="at most"),
    ):
        await run_load_benchmark(concurrency_levels=[_MAX_CONCURRENCY + 1])
    mock_engine.assert_not_called()
    mock_popen.assert_not_called()


@pytest.mark.asyncio
async def test_excessive_rows_is_rejected_before_any_engine_work():
    with (
        patch("querygate.load_benchmark.create_async_engine") as mock_engine,
        patch("subprocess.Popen") as mock_popen,
        pytest.raises(ValueError, match="at most"),
    ):
        await run_load_benchmark(rows=10_000_000)
    mock_engine.assert_not_called()
    mock_popen.assert_not_called()


@pytest.mark.asyncio
async def test_non_postgres_connection_string_is_rejected_before_any_engine_work():
    with pytest.raises(ValueError, match="Postgres"):
        await run_load_benchmark(connection_string="snowflake://user:pass@acct/db")


def test_cli_run_parser_defaults():
    parser = build_parser()
    args = parser.parse_args(["run"])
    assert args.rows == 5000
    assert args.concurrency == [1, 5, 15, 30]
    assert args.workers == [1, 2, 4]
    assert args.requests_per_slot == 10
    assert args.json is False


def test_cli_run_parser_overrides():
    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--rows",
            "100",
            "--concurrency",
            "2",
            "4",
            "8",
            "--workers",
            "1",
            "3",
            "--requests-per-slot",
            "5",
            "--json",
        ]
    )
    assert args.rows == 100
    assert args.concurrency == [2, 4, 8]
    assert args.workers == [1, 3]
    assert args.requests_per_slot == 5
    assert args.json is True


def test_help_text_never_prints_the_default_connection_string():
    """The default connection string (a working credential, even if it's
    only the public dev demo one) must never appear in --help output — an
    operator who overrides QUERYGATE_DEMO_URL with a real credential to
    point this tool at a non-demo database would otherwise leak it to
    stdout/CI logs on a bare --help."""
    assert DEFAULT_CONNECTION_STRING not in build_parser().format_help()


def test_cli_markdown_out_defaults_to_the_docs_path():
    parser = build_parser()
    args = parser.parse_args(["run"])
    assert args.markdown_out == DEFAULT_MARKDOWN_PATH


def test_cli_no_markdown_disables_the_default():
    parser = build_parser()
    args = parser.parse_args(["run", "--no-markdown"])
    assert args.markdown_out is None


def test_cli_markdown_out_overrides_the_path():
    parser = build_parser()
    args = parser.parse_args(["run", "--markdown-out", "/tmp/custom.md"])
    assert args.markdown_out == Path("/tmp/custom.md")


def _sample_report() -> LoadBenchmarkReport:
    return LoadBenchmarkReport(
        dialect="postgresql",
        seeded_rows=100,
        requests_per_slot=4,
        max_concurrency=35,
        sweeps=[
            WorkerSweepResult(
                workers=1,
                levels=[
                    ConcurrencyLevelResult(
                        concurrency=1,
                        requests=4,
                        baseline=_tier(2.0, throughput_rps=200.0),
                        rest=_tier(4.0, throughput_rps=100.0),
                    )
                ],
            )
        ],
    )


def test_render_markdown_contains_the_report_data_and_not_hand_edit_banner():
    markdown = _render_markdown(_sample_report())
    assert "do not hand-edit" in markdown
    assert "workers = 1" in markdown
    assert "200.0" in markdown
    assert "100.0" in markdown
    # No machine-identifying hostname — only generic OS/CPU-count context.
    assert "Host context:" in markdown


def test_render_markdown_never_claims_to_be_analysis():
    """The auto-generated file is data only — it must point to the
    hand-maintained methodology doc rather than assert its own
    interpretation of the numbers."""
    markdown = _render_markdown(_sample_report())
    assert "LOAD_BENCHMARK.md" in markdown


def _report_with_worker_sweep(*, lowest_rps: float, highest_rps: float) -> LoadBenchmarkReport:
    return LoadBenchmarkReport(
        dialect="postgresql",
        seeded_rows=100,
        requests_per_slot=4,
        max_concurrency=35,
        sweeps=[
            WorkerSweepResult(
                workers=1,
                levels=[
                    ConcurrencyLevelResult(
                        concurrency=1,
                        requests=4,
                        baseline=_tier(2.0),
                        rest=_tier(4.0, throughput_rps=50.0),
                    ),
                    ConcurrencyLevelResult(
                        concurrency=30,
                        requests=120,
                        baseline=_tier(2.0),
                        rest=_tier(4.0, throughput_rps=lowest_rps),
                    ),
                ],
            ),
            WorkerSweepResult(
                workers=4,
                levels=[
                    ConcurrencyLevelResult(
                        concurrency=1,
                        requests=4,
                        baseline=_tier(2.0),
                        rest=_tier(4.0, throughput_rps=50.0),
                    ),
                    ConcurrencyLevelResult(
                        concurrency=30,
                        requests=120,
                        baseline=_tier(2.0),
                        rest=_tier(4.0, throughput_rps=highest_rps),
                    ),
                ],
            ),
        ],
    )


def test_scaling_caution_fires_when_added_workers_show_no_benefit():
    """The exact CPU-contention symptom documented in LOAD_BENCHMARK.md:
    peak-concurrency REST throughput barely moves (or drops) between the
    lowest and highest worker count in the sweep."""
    report = _report_with_worker_sweep(lowest_rps=300.0, highest_rps=310.0)
    caution = _scaling_caution(report)
    assert caution is not None
    assert "little to no benefit from added workers" in caution
    assert "LOAD_BENCHMARK.md" in caution


def test_scaling_caution_is_silent_when_workers_clearly_help():
    report = _report_with_worker_sweep(lowest_rps=300.0, highest_rps=900.0)
    assert _scaling_caution(report) is None


def test_scaling_caution_is_silent_for_a_single_worker_count_sweep():
    """No comparison to make with only one worker count tested — must not
    false-positive just because there's nothing to compare against."""
    assert _scaling_caution(_sample_report()) is None


def test_render_markdown_includes_the_caution_when_scaling_is_flat():
    report = _report_with_worker_sweep(lowest_rps=300.0, highest_rps=310.0)
    markdown = _render_markdown(report)
    assert "little to no benefit from added workers" in markdown


def test_render_markdown_omits_the_caution_when_scaling_is_healthy():
    report = _report_with_worker_sweep(lowest_rps=300.0, highest_rps=900.0)
    markdown = _render_markdown(report)
    assert "little to no benefit from added workers" not in markdown


def _run_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        connection_string="postgresql+asyncpg://x/y",
        rows=100,
        concurrency=[1],
        workers=[1],
        requests_per_slot=4,
        json=False,
        markdown_out=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_cmd_run_actually_writes_the_markdown_file_to_disk(tmp_path):
    """`--markdown-out`'s CLI wiring, not just `_render_markdown`'s content,
    must be exercised: a bug in `_cmd_run` itself (wrong attribute name, path
    never created, write skipped) would pass every `_render_markdown`-only
    test above while never actually producing the file a user asked for."""
    out_path = tmp_path / "nested" / "results.md"

    async def _fake_run_load_benchmark(**kwargs):
        return _sample_report()

    with patch(
        "querygate.load_benchmark_cli.run_load_benchmark",
        _fake_run_load_benchmark,
    ):
        exit_code = _cmd_run(_run_args(markdown_out=out_path))

    assert exit_code == 0
    assert out_path.exists()
    assert "do not hand-edit" in out_path.read_text()


def test_cmd_run_skips_the_markdown_file_when_disabled(tmp_path):
    out_path = tmp_path / "results.md"

    async def _fake_run_load_benchmark(**kwargs):
        return _sample_report()

    with patch(
        "querygate.load_benchmark_cli.run_load_benchmark",
        _fake_run_load_benchmark,
    ):
        exit_code = _cmd_run(_run_args(markdown_out=None))

    assert exit_code == 0
    assert not out_path.exists()


def test_termination_signal_handler_stops_servers_then_reraises_default(monkeypatch):
    """SIGTERM (a CI job timeout, `kill <pid>`) does not raise a Python
    exception by default, so `run_load_benchmark`'s try/finally never runs —
    this handler is the only thing that can still stop an orphaned spawned
    server on that path (2026-08-11 security-invariant-reviewer finding).
    Patches `os.kill`/`signal.signal` so the test process itself is never
    actually signaled."""
    import signal as signal_module

    stopped = []
    monkeypatch.setattr(
        "querygate.load_benchmark_cli.load_benchmark._stop_all_active_servers",
        lambda: stopped.append(True),
    )
    killed = []
    monkeypatch.setattr("os.kill", lambda pid, signum: killed.append((pid, signum)))
    restored = []
    monkeypatch.setattr("signal.signal", lambda signum, handler: restored.append((signum, handler)))

    _handle_termination_signal(signal_module.SIGTERM, None)

    assert stopped == [True]
    assert restored == [(signal_module.SIGTERM, signal_module.SIG_DFL)]
    import os as os_module

    assert killed == [(os_module.getpid(), signal_module.SIGTERM)]
