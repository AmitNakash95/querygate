"""Reproducible throughput/latency-under-concurrency benchmark — driven over
a REAL socket against a REAL `uvicorn` server, with a worker-count sweep.

The sibling of `performance_benchmark.py`, which answers "how much does one
request cost." This answers the second half of the same question a design
partner asks: *"does that cost hold up under concurrent traffic, and does
adding capacity actually fix it?"* — with real numbers, not an estimate.

**Methodology note (2026-08-11): why this drives a real socket, not
`ASGITransport`.** An earlier version of this benchmark reused
`performance_benchmark.py`'s in-process `_benchmark_environment` (the same
`httpx.ASGITransport` trick the single-request benchmark correctly uses).
That is wrong for a *concurrency* benchmark specifically: it puts the test
client and the server being measured in the same Python process, on the
same event loop. At high concurrency the client's own coroutine bookkeeping
competes with QueryGate's CPU-bound request handling for the same CPU — a
confound no real deployment has, since a real client is always a separate
process from the server. Measured directly: the same concurrency sweep, run
first in-process and then over a real socket against a real single-worker
server, showed the same *shape* (throughput rises, peaks, then falls off)
but a dramatically different *magnitude* — the in-process version showed
REST throughput collapsing to roughly a tenth of the raw-SQL baseline at the
highest concurrency tested; the real-socket version showed a much gentler
dip from its own peak (see `docs/benchmarks/LOAD_BENCHMARK.md` for the fuller
writeup — deliberately not restated here with specific numbers, since a
number from one machine at one moment isn't a reproducible claim). Same
underlying single-process ceiling, but the in-process harness's
self-competition made it look far worse than a real deployment would ever
experience. This version fixes that by always measuring the real app over a
real socket, and adds a `worker_counts` sweep so "does adding workers fix
it" is a measured answer, not an assertion.

**Not the same tool as `docs/LOAD_TESTING.md` / `make test-load` /
`make test-soak`.** Those prove *correctness* of the concurrency/timeout
guardrails — that a configured cap of N never lets more than N queries run
at once — using a small, deliberately tight cap (2) so rejection/queueing
behavior is directly observable. This benchmark *measures performance*
(informational, not pass/fail): for a policy cap raised high enough to never
bind, how do latency and achieved throughput behave as concurrency and
worker count both vary, compared to raw SQL under the same concurrency.

Methodology:

- **Same fixture, same scenarios.** Reuses `performance_benchmark.py`'s
  `SCENARIOS`, `_create_and_seed_table`/`_drop_table`, and `_time_baseline`
  — the identical seeded table and query definitions, round-robined across
  the sweep, so numbers are comparable to the single-request benchmark.
- **The REST tier is a real, separate OS process.** For each worker count
  tested, a real `querygate.api.app:app` is launched via
  `python -m uvicorn ... --workers N` bound to a real localhost TCP port,
  with its own throwaway `connections.yaml`/`policy.yaml` (env-var
  interpolated, no process-global state shared with this benchmark's own
  process — unlike `performance_benchmark.py`'s in-process tiers, this one
  needs no registry/policy/audit-sink snapshot-and-restore or re-entrancy
  guard, because it never touches this process's globals at all).
- **Two tiers: `baseline` (raw SQL, this process) and `rest` (the real
  server, a separate process).** No `pipeline` (no-HTTP) tier here — that
  tier exists in the single-request benchmark to attribute overhead to a
  layer; it has no meaning once the REST tier is a separate process.
- **Per-request failures are tolerated, not fatal.** A batch reports an
  error count alongside its latency distribution rather than aborting on
  the first non-2xx response or connection error — the same posture any
  real load-testing tool (`wrk`, `hey`, `ab`) takes.
- **The concurrency cap and both engines' connection pools are sized to fit
  the sweep, not to hide it** — see `run_load_benchmark`'s pool-sizing
  comments. A rejected/queued request here would be a benchmark-setup bug,
  not a measurement.
- **Throughput is wall-clock over the whole batch's *successful* requests**,
  not the reciprocal of one request's latency and not the attempted count —
  `successful_requests / elapsed_wall_clock_seconds`. Failures return fast
  (a rejection, a refused connection), so counting them in the numerator
  would inflate throughput exactly when a run is least trustworthy.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import signal
import socket
import subprocess  # nosec B404 — used only to launch this benchmark's own `python -m uvicorn` with locally-built, non-attacker-controlled args (see _start_server)
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import List, Optional, Sequence, Set, Tuple

import httpx
import pydantic as pyd
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from querygate.performance_benchmark import (
    DEFAULT_CONNECTION_STRING,
    SCENARIOS,
    LatencyStats,
    _create_and_seed_table,
    _drop_table,
    _time_baseline,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONNECTION_ID = "load_bench"

DEFAULT_CONCURRENCY_LEVELS: Tuple[int, ...] = (1, 5, 15, 30)
DEFAULT_WORKER_COUNTS: Tuple[int, ...] = (1, 2, 4)
DEFAULT_REQUESTS_PER_SLOT = 10

# Sanity ceilings on caller-supplied CLI input — this is operator-controlled,
# not caller-reachable, but an unbounded --workers/--concurrency/--rows can
# self-DoS the operator's own machine or database (e.g. --workers 200 forks
# 200 uvicorn processes; a high enough workers*concurrency combination can
# exceed Postgres's own max_connections even with generous per-worker pool
# sizing) — 2026-08-11 security-invariant-reviewer finding.
_MAX_WORKERS = 16
_MAX_CONCURRENCY = 200
_MAX_ROWS = 1_000_000

_READY_TIMEOUT_SECONDS = 30.0
_SHUTDOWN_TIMEOUT_SECONDS = 10.0


class TierResult(pyd.BaseModel):
    throughput_rps: float
    latency: LatencyStats
    errors: int


class ConcurrencyLevelResult(pyd.BaseModel):
    concurrency: int
    requests: int
    baseline: TierResult
    rest: TierResult

    @property
    def rest_overhead_ms(self) -> float:
        """What REST costs over raw SQL at THIS concurrency level — compare
        across levels to see whether overhead grows with load."""
        return self.rest.latency.mean_ms - self.baseline.latency.mean_ms

    @property
    def throughput_ratio(self) -> float:
        """rest throughput as a fraction of baseline throughput at the same
        concurrency — 1.0 would mean QueryGate sustains identical req/s to
        raw SQL under the same load."""
        if self.baseline.throughput_rps <= 0:
            return 0.0
        return self.rest.throughput_rps / self.baseline.throughput_rps


class WorkerSweepResult(pyd.BaseModel):
    workers: int
    levels: List[ConcurrencyLevelResult]


class LoadBenchmarkReport(pyd.BaseModel):
    dialect: str
    seeded_rows: int
    requests_per_slot: int
    # The policy concurrency cap actually configured for every server in
    # this run — exposed so "the cap never binds" is independently
    # checkable from the report, not just asserted (2026-08-11
    # test-contract-reviewer finding on the prior version of this module).
    max_concurrency: int
    sweeps: List[WorkerSweepResult]


def _scenario_for(index: int):
    return SCENARIOS[index % len(SCENARIOS)]


def _free_port() -> int:
    """Ask the OS for an unused localhost port. Small TOCTOU race between
    this and uvicorn's own bind — acceptable for local benchmark tooling; a
    collision surfaces as a clear `_wait_ready` timeout, not silent
    corruption."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _run_concurrent_batch(
    coro_factory,
    *,
    concurrency: int,
    count: int,
) -> Tuple[List[float], int, float]:
    """Fire `count` calls to `coro_factory(index)` with at most `concurrency`
    in flight at once. Returns (latency_ms of successful calls, error count,
    total wall-clock seconds for the whole batch). A failing call does not
    abort the batch — its slot is simply counted as an error, the same
    posture any real load-testing tool takes."""
    semaphore = asyncio.Semaphore(concurrency)
    results: List[Optional[float]] = [None] * count

    async def _one(index: int) -> None:
        async with semaphore:
            try:
                results[index] = await coro_factory(index)
            except Exception:
                results[index] = None

    start = time.perf_counter()
    await asyncio.gather(*(_one(i) for i in range(count)))
    elapsed_seconds = time.perf_counter() - start
    latencies = [value for value in results if value is not None]
    errors = count - len(latencies)
    return latencies, errors, elapsed_seconds


async def _baseline_call(engine: AsyncEngine, table: str, index: int) -> float:
    (stmt, params), _payload = _scenario_for(index).for_seed(table, index)
    return await _time_baseline(engine, stmt, params)


async def _rest_call(client: httpx.AsyncClient, table: str, index: int) -> float:
    _raw, payload = _scenario_for(index).for_seed(table, index)
    start = time.perf_counter()
    response = await client.post(f"/api/v1/{_CONNECTION_ID}/query", json=payload)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    response.raise_for_status()
    return elapsed_ms


async def _run_level(
    *,
    table: str,
    baseline_engine: AsyncEngine,
    client: httpx.AsyncClient,
    concurrency: int,
    requests_per_slot: int,
) -> ConcurrencyLevelResult:
    count = concurrency * requests_per_slot

    baseline_latencies, baseline_errors, baseline_elapsed = await _run_concurrent_batch(
        lambda index: _baseline_call(baseline_engine, table, index),
        concurrency=concurrency,
        count=count,
    )
    rest_latencies, rest_errors, rest_elapsed = await _run_concurrent_batch(
        lambda index: _rest_call(client, table, index), concurrency=concurrency, count=count
    )

    return ConcurrencyLevelResult(
        concurrency=concurrency,
        requests=count,
        baseline=TierResult(
            # Numerator is successful requests, not `count` — a batch with
            # failures (which fail fast: a 401, a refused connection) would
            # otherwise shrink `elapsed` while the numerator stayed fixed,
            # inflating throughput exactly when the run is least trustworthy
            # (2026-08-11 security-invariant-reviewer finding: a fully-401ing
            # REST tier would have reported the HIGHEST throughput in the
            # whole sweep).
            throughput_rps=(
                (len(baseline_latencies) / baseline_elapsed) if baseline_elapsed > 0 else 0.0
            ),
            latency=LatencyStats.from_samples(baseline_latencies),
            errors=baseline_errors,
        ),
        rest=TierResult(
            throughput_rps=(len(rest_latencies) / rest_elapsed) if rest_elapsed > 0 else 0.0,
            latency=LatencyStats.from_samples(rest_latencies),
            errors=rest_errors,
        ),
    )


def _write_config(
    config_dir: Path, table: str, *, max_concurrency: int, concurrency_wait_seconds: float
) -> None:
    (config_dir / "connections.yaml").write_text(
        "connections:\n"
        f"  - id: {_CONNECTION_ID}\n"
        "    dialect: postgresql\n"
        "    connection_string: ${QUERYGATE_LOAD_BENCH_DB_URL}\n"
        "    enabled: true\n"
        f"    known_tables: [{table}]\n"
    )
    (config_dir / "policy.yaml").write_text(
        "default:\n"
        "  enabled: true\n"
        f"  allowed_tables: [{table}]\n"
        "  max_limit: 1000\n"
        "  default_limit: 100\n"
        f"  max_concurrency: {max_concurrency}\n"
        f"  concurrency_wait_seconds: {concurrency_wait_seconds}\n"
    )


# Tracks every server subprocess this module has spawned that hasn't been
# confirmed stopped yet. run_load_benchmark()'s try/finally around
# _stop_server(proc) is the primary cleanup path; this set plus the atexit
# handler below is a belt-and-suspenders net for the narrow window between
# _start_server() returning and that try block being entered (e.g. a
# KeyboardInterrupt landing in that exact gap), so an interrupted run doesn't
# leave an orphaned uvicorn process bound to a port after the benchmark
# process itself has exited.
_ACTIVE_SERVER_PROCS: Set[subprocess.Popen] = set()


def _stop_all_active_servers() -> None:
    for proc in list(_ACTIVE_SERVER_PROCS):
        try:
            _stop_server(proc)
        except Exception:
            # nosec B110 — this is a last-resort atexit safety net; one
            # misbehaving entry must not stop the rest from being cleaned up,
            # and there is no meaningful recovery action left to take this
            # late in interpreter shutdown.
            pass


atexit.register(_stop_all_active_servers)


def _start_server(
    *,
    connection_string: str,
    config_dir: Path,
    port: int,
    workers: int,
    pool_size: int,
    max_overflow: int,
) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(
        {
            "QUERYGATE_LOAD_BENCH_DB_URL": connection_string,
            "CONNECTIONS_FILE": str(config_dir / "connections.yaml"),
            "POLICY_FILE": str(config_dir / "policy.yaml"),
            "ENVIRONMENT": "localhost",
            # JSON, not empty string: pydantic-settings parses list-typed env
            # vars as JSON, so API_KEYS="" fails config load outright rather
            # than meaning "no keys" (found running this experiment
            # manually before writing this module).
            "API_KEYS": "[]",
            "MCP_API_KEYS": "[]",
            "AUDIT_SINK_BACKEND": "none",
            "HOST_ADDRESS": "127.0.0.1",
            "PORT": str(port),
            "LOG_LEVEL": "WARNING",
            "POOL_SIZE": str(pool_size),
            "CONN_MAX_OVERFLOW": str(max_overflow),
            # Neutralize any of these an operator's own shell/CI environment
            # happens to export — the spawned server must always be the bare,
            # anonymous-auth, no-catalog config this benchmark was designed
            # against, never whatever the invoking shell inherited.
            "JWT_ENABLED": "false",
            "MCP_ENABLED": "false",
            "CATALOG_FILE": "",
            "TEMPLATES_FILE": "",
            "SEMANTIC_MEMORY_REFRESH_ENABLED": "false",
            "SEMANTIC_MEMORY_LEARNING_ENABLED": "false",
            "SEMANTIC_MEMORY_USAGE_SIGNALS_ENABLED": "false",
            "CREDENTIAL_LEASE_REFRESH_ENABLED": "false",
            "CONCURRENCY_BACKEND": "in_process",
            # AppConfig's own default is already require-auth, but force it
            # explicitly rather than trust an inherited METRICS_REQUIRE_AUTH=false
            # (2026-08-11 security-invariant-reviewer finding: without this,
            # an operator whose shell/CI/.env sets that would get a spawned
            # server whose /metrics answers 200 unauthenticated — strictly
            # MORE permissive than this function's own anonymous-caller
            # baseline, contradicting the "never inherit a more dangerous
            # configuration" intent this whole env dict exists for).
            "METRICS_REQUIRE_AUTH": "true",
            "VAULT_ENABLED": "false",
            "VAULT_TOKEN": "",  # nosec B105 — clears any inherited token to empty, not a hardcoded credential
            "API_V1_PREFIX": "/api/v1",
        }
    )
    proc = subprocess.Popen(  # nosec B603 — argv is a fixed list built from this function's own typed int params (port, workers) and sys.executable; no shell, no string interpolation, no caller/AST-controlled input reaches it
        [
            sys.executable,
            "-m",
            "uvicorn",
            "querygate.api.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            str(workers),
            "--log-level",
            "warning",
        ],
        cwd=str(_REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _ACTIVE_SERVER_PROCS.add(proc)
    return proc


async def _wait_ready(
    port: int, proc: subprocess.Popen, timeout: float = _READY_TIMEOUT_SECONDS
) -> None:
    """Poll `/health` until it answers as *this benchmark's* server, or the
    spawned process has already died, or `timeout` elapses.

    Checking `proc.poll()` first means a server that crashes on startup (a
    bad config, a port collision) fails fast with a clear "server process
    exited" error instead of spinning for the full timeout only to report a
    generic "did not become ready". Checking the response body's `service`
    field, not just a 200 status, means a stale process from a prior crashed
    run that's still bound to this port (or an unrelated service that
    happens to be listening there) is never mistaken for this run's server.
    """
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            exit_code = proc.poll()
            if exit_code is not None:
                raise RuntimeError(
                    f"QueryGate server process exited (code {exit_code}) before becoming "
                    f"ready on port {port}"
                )
            try:
                response = await client.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
                if response.status_code == 200 and response.json().get("service") == "querygate":
                    return
            except Exception:
                # nosec B110 — expected and retried, not masked: connection-refused
                # is the normal state while uvicorn is still starting up; logging
                # each retry would spam the console for the whole readiness window.
                pass
            await asyncio.sleep(0.2)
    raise RuntimeError(f"QueryGate server did not become ready on port {port} within {timeout}s")


def _stop_server(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
            proc.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        except ProcessLookupError:
            pass
    finally:
        _ACTIVE_SERVER_PROCS.discard(proc)


# Postgres's own `max_connections` (100 on the default demo container).
_CONNECTION_BUDGET = 90
# SQLAlchemy's own defaults for a plain `create_async_engine(...)` call with
# no pool_size/max_overflow override — this is what admin_engine below uses,
# and it stays open (idle, not closed) for this whole function's duration,
# so its share of the budget must be reserved too, not assumed away.
_ADMIN_ENGINE_RESERVE = 5 + 10


def _pool_sizing_for_workers(
    peak_concurrency: int, workers: int, *, reserved: int = 0
) -> Tuple[int, int]:
    """Per-worker `(pool_size, max_overflow)` for a server with `workers`
    processes handling up to `peak_concurrency` concurrent requests total.
    `reserved` is how many connections from `_CONNECTION_BUDGET` are already
    spoken for by OTHER pools open at the same time as this one (see
    `run_load_benchmark`, which reserves the baseline engine's own sizing
    plus `_ADMIN_ENGINE_RESERVE`) — this function's own `workers x pool`
    product is bounded by what's left, not the raw budget.

    Each worker process gets its own connection pool. uvicorn's SO_REUSEPORT
    load balancing spreads concurrent requests across workers on average,
    but a synchronized burst (this benchmark fires the whole batch via one
    `asyncio.gather`) is not guaranteed to land evenly instant-to-instant, so
    a per-worker pool sized to only `peak / workers` self-starves under an
    uneven burst — confirmed directly: an earlier, tighter version of this
    formula (capped at 20) gave `workers=1` a smaller pool (25 total incl.
    overflow) than the peak concurrency being thrown at it (30), making
    higher worker counts look *no better or worse* than one, which was the
    pool exhausting itself, not a real ceiling — and this specific
    regression had ZERO test coverage until it was mutation-verified
    directly against a live Postgres (2026-08-11 test-contract-reviewer
    finding: reverting to the buggy formula left every test, including the
    real-Postgres integration suite, green). Extracted to a pure function
    specifically so a future regression to that formula fails fast and
    locally, not by silently producing a misleading benchmark result someone
    has to notice by eye.

    Sized generously (comfortably above `peak_concurrency`, per worker)
    instead, with the `workers x pool` product kept under the remaining
    budget. Only one worker-count's server is ever running at a time (the
    previous one is fully stopped before the next starts), so it's this
    run's own `workers x pool` product plus `reserved` that matters, not a
    sum across the whole sweep — but `baseline`'s own pool IS open the
    entire time (a previous version of this docstring claimed otherwise;
    SQLAlchemy's async pool keeps up to `pool_size` connections open, idle,
    between batches — not closed — so it was wrong: 2026-08-11
    security-invariant-reviewer finding), which is exactly why `reserved`
    exists now instead of assuming it away.
    """
    budget = _CONNECTION_BUDGET - reserved
    generous_pool = min(30, max(10, peak_concurrency + 8))
    generous_overflow = 5
    if workers * (generous_pool + generous_overflow) <= budget:
        return generous_pool, generous_overflow
    # The generous sizing doesn't fit the remaining budget at this worker
    # count — shrink pool AND overflow together, floored at 1 each, so the
    # budget is *always* honored regardless of `workers` (2026-08-11
    # security-invariant-reviewer finding: an earlier version floored only
    # `pool_size` at 10 while leaving `overflow` fixed at 5, which stopped
    # actually bounding the total once `workers` got large enough — e.g.
    # workers=8 produced 120 total connections, over Postgres's own default
    # `max_connections` of 100). A thin per-worker pool at a high worker
    # count is an accepted, honest tradeoff; `run_load_benchmark`'s own
    # `_MAX_WORKERS` guard also caps how far this can be pushed.
    per_worker_budget = max(2, budget // workers)
    pool_size = max(1, per_worker_budget - 1)
    overflow = per_worker_budget - pool_size
    return pool_size, overflow


async def run_load_benchmark(
    *,
    connection_string: str = DEFAULT_CONNECTION_STRING,
    rows: int = 5000,
    concurrency_levels: Sequence[int] = DEFAULT_CONCURRENCY_LEVELS,
    worker_counts: Sequence[int] = DEFAULT_WORKER_COUNTS,
    requests_per_slot: int = DEFAULT_REQUESTS_PER_SLOT,
) -> LoadBenchmarkReport:
    """Sweep worker counts × concurrency levels, measuring achieved
    throughput and latency for raw SQL vs. a full QueryGate REST round trip
    (over a real socket, real server process) at each combination.

    Needs a reachable Postgres (``make compose-up``) and permission to spawn
    local subprocesses bound to localhost ports. Postgres-only: the seed DDL
    and scenario SQL are Postgres-specific.
    """
    backend = sa.engine.url.make_url(connection_string).get_backend_name()
    if backend != "postgresql":
        raise ValueError(
            f"load_benchmark only targets Postgres today (its seed DDL and scenario SQL "
            f"are Postgres-specific); got dialect {backend!r} from connection_string."
        )

    levels = sorted(set(concurrency_levels))
    if not levels:
        raise ValueError("concurrency_levels must be non-empty")
    if any(level < 1 for level in levels):
        raise ValueError("every concurrency level must be at least 1")
    if max(levels) > _MAX_CONCURRENCY:
        raise ValueError(f"concurrency levels must be at most {_MAX_CONCURRENCY}")

    workers_list = sorted(set(worker_counts))
    if not workers_list:
        raise ValueError("worker_counts must be non-empty")
    if any(workers < 1 for workers in workers_list):
        raise ValueError("every worker count must be at least 1")
    if max(workers_list) > _MAX_WORKERS:
        raise ValueError(f"worker counts must be at most {_MAX_WORKERS}")

    if requests_per_slot < 1:
        raise ValueError("requests_per_slot must be at least 1")
    if rows > _MAX_ROWS:
        raise ValueError(f"rows must be at most {_MAX_ROWS}")

    peak_concurrency = max(levels)
    # Comfortably above the peak level tested at every worker count, so the
    # admission cap never binds — this benchmark measures throughput, not
    # the guardrail (make test-load's job).
    policy_max_concurrency = peak_concurrency + 5

    table = f"querygate_perf_bench_{uuid.uuid4().hex[:8]}"
    admin_engine = create_async_engine(connection_string, pool_pre_ping=True)
    # Reuses the same bounded formula as the per-worker sizing below (as
    # "one process handling up to peak_concurrency concurrent requests",
    # which is exactly what the baseline tier is — one Python process,
    # bounded by an asyncio.Semaphore(concurrency)) instead of the previous
    # unbounded `peak_concurrency + 2`, which could reach 404 connections at
    # this tool's own allowed ceiling (--concurrency 200) — a self-inflicted
    # connection-exhaustion risk against whatever database --connection-string
    # points at (2026-08-11 security-invariant-reviewer finding).
    baseline_pool, baseline_overflow = _pool_sizing_for_workers(peak_concurrency, workers=1)
    baseline_engine = create_async_engine(
        connection_string,
        pool_pre_ping=True,
        pool_size=baseline_pool,
        max_overflow=baseline_overflow,
    )
    # baseline_engine (above) and admin_engine (SQLAlchemy's own pool_size=5/
    # max_overflow=10 defaults) both stay open for this entire function's
    # duration, concurrently with whichever worker-count's server pool is
    # currently running — reserve their share so the per-worker-count sizing
    # below bounds the WHOLE run's connection count, not just its own slice.
    reserved_for_worker_sizing = baseline_pool + baseline_overflow + _ADMIN_ENGINE_RESERVE
    config_dir = Path(tempfile.mkdtemp(prefix="querygate_load_bench_"))
    table_created = False
    try:
        await _create_and_seed_table(admin_engine, table, rows)
        table_created = True
        _write_config(
            config_dir, table, max_concurrency=policy_max_concurrency, concurrency_wait_seconds=30
        )

        sweeps: List[WorkerSweepResult] = []
        for workers in workers_list:
            port = _free_port()
            per_worker_pool, per_worker_overflow = _pool_sizing_for_workers(
                peak_concurrency, workers, reserved=reserved_for_worker_sizing
            )
            proc = _start_server(
                connection_string=connection_string,
                config_dir=config_dir,
                port=port,
                workers=workers,
                pool_size=per_worker_pool,
                max_overflow=per_worker_overflow,
            )
            try:
                await _wait_ready(port, proc)
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}", timeout=30.0
                ) as client:
                    # Warmup at peak concurrency (TCP handshake + Postgres
                    # backend process startup for every pooled connection on
                    # both engines) before any timed level — see
                    # performance_benchmark.py's identical rationale.
                    await _run_level(
                        table=table,
                        baseline_engine=baseline_engine,
                        client=client,
                        concurrency=peak_concurrency,
                        requests_per_slot=1,
                    )
                    level_results = [
                        await _run_level(
                            table=table,
                            baseline_engine=baseline_engine,
                            client=client,
                            concurrency=concurrency,
                            requests_per_slot=requests_per_slot,
                        )
                        for concurrency in levels
                    ]
                sweeps.append(WorkerSweepResult(workers=workers, levels=level_results))
            finally:
                _stop_server(proc)

        return LoadBenchmarkReport(
            dialect=backend,
            seeded_rows=rows,
            requests_per_slot=requests_per_slot,
            max_concurrency=policy_max_concurrency,
            sweeps=sweeps,
        )
    finally:
        try:
            if table_created:
                await _drop_table(admin_engine, table)
        finally:
            await admin_engine.dispose()
            await baseline_engine.dispose()
            shutil.rmtree(config_dir, ignore_errors=True)
