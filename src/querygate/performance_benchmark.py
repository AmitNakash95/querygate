"""Reproducible request-overhead benchmark: what does QueryGate add per query?

Answers the question a design partner actually asks — "how much time does
QueryGate take, and add, on each request, and will it slow down our database
access?" — with real numbers against a real Postgres instance, not a modeled
estimate (contrast ``security_benchmark.py``, which is deliberately offline).

Three tiers are measured for the same logical query, so the overhead can be
attributed rather than just quoted as one number:

- **``baseline_ms``** — the query executed as raw SQL directly against
  Postgres through a plain SQLAlchemy async engine. This is the number an
  agent with a bare database connection would see: no QueryGate anywhere in
  the path.
- **``pipeline_ms``** — the same logical query executed through the real
  ``StructuredQueryService`` (policy validation, cached schema validation,
  compilation, session guardrails, execution, audit) with **no HTTP** in the
  loop. Isolates what the request pipeline itself costs.
- **``rest_ms``** — the same query as a full REST round trip
  (``POST /{connection}/query``) through the real FastAPI app via an
  in-process ASGI transport (the same fidelity ``tests/integration/
  test_postgres_load_guardrails.py`` uses — no real socket/TLS, but every
  other layer: routing, auth dependency, request/response (de)serialization).
  This is the closest number to what a real client experiences short of an
  actual network hop.

``rest_ms - baseline_ms`` is "how much QueryGate adds to a request" the way a
customer means it; ``pipeline_ms - baseline_ms`` isolates the guardrail/compile
cost from HTTP/JSON overhead. Both are reported per scenario, plus a query mix
across scenarios of differing shape (point lookup, filtered scan, aggregation,
sorted page) so a single easy case can't flatter the number.

Design constraints (mirrors ``security_benchmark.py``'s honesty posture):

- **Drives the real pipeline, not a mock.** Every timed call goes through the
  genuine ``StructuredQueryService`` / ``create_app`` production code paths.
- **Self-contained fixture data.** The benchmark creates and seeds its own
  table (``querygate_perf_bench_<run_id>``), so results don't depend on
  whatever happens to be loaded into the shared demo database, and drops it
  in a ``finally`` regardless of outcome.
- **Informational, not pass/fail.** Unlike the security benchmark there is no
  ground truth for "correct" latency — hardware, container overhead, and
  Postgres warmth all vary run to run. This module reports distributions
  (mean/p50/p95/p99) and never asserts a threshold; a caller wanting a CI
  regression gate can pass ``--max-overhead-ms`` to the CLI for an informal
  sanity check, off by default.
"""

from __future__ import annotations

import os
import random
import statistics
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List

import pydantic as pyd
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import querygate.audit.sinks as _audit_sinks_module
import querygate.connections.registry as _registry_module
import querygate.policy.loader as _policy_module
from querygate.api.app import create_app
from querygate.audit.sinks import AuditSink, NullAuditSink, get_audit_sink
from querygate.connections.engine import dispose_engine
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.logging import get_logger
from querygate.execution.concurrency import in_process_limiter
from querygate.execution.service import StructuredQueryService
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.query_ast.models import StructuredQuery

# Overridable so a working demo-database credential never has to be the ONLY
# literal shipped in `src/querygate/` (this one is public/dev-only, matching
# docker-compose.yml, but scanners flag it regardless of context).
DEFAULT_CONNECTION_STRING = os.environ.get(
    "QUERYGATE_DEMO_URL", "postgresql+asyncpg://querygate:querygate@localhost:5433/querygate_demo"
)
_CONNECTION_ID = "perf_bench"
# Mirrors load_benchmark.py's identical guard: a larger value materializes
# the whole seed batch in memory before any database round trip — an
# operator-triggerable but still unwise self-DoS input.
_MAX_ROWS = 1_000_000

# Re-entrancy guard for _benchmark_environment — run_performance_benchmark
# takes over process-global state for its duration and cannot run
# concurrently with itself (2026-08-11 security-invariant-reviewer finding).
_benchmark_active = False


class LatencyStats(pyd.BaseModel):
    """Distribution of one tier's per-request latency, in milliseconds."""

    samples: int
    mean_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    min_ms: float
    max_ms: float

    @classmethod
    def from_samples(cls, values: List[float]) -> "LatencyStats":
        ordered = sorted(values)
        if not ordered:
            # A real load run tolerates per-request failures rather than
            # aborting the whole sweep (load_benchmark.py) — an all-failed
            # batch must still produce a reportable (zeroed) stat, not raise
            # statistics.fmean's/IndexError on an empty list.
            return cls(
                samples=0, mean_ms=0.0, p50_ms=0.0, p95_ms=0.0, p99_ms=0.0, min_ms=0.0, max_ms=0.0
            )
        return cls(
            samples=len(ordered),
            mean_ms=statistics.fmean(ordered),
            p50_ms=_percentile(ordered, 0.50),
            p95_ms=_percentile(ordered, 0.95),
            p99_ms=_percentile(ordered, 0.99),
            min_ms=ordered[0],
            max_ms=ordered[-1],
        )


def _percentile(ordered: List[float], fraction: float) -> float:
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


class ScenarioResult(pyd.BaseModel):
    name: str
    description: str
    iterations: int
    baseline: LatencyStats
    pipeline: LatencyStats
    rest: LatencyStats

    @property
    def pipeline_overhead_ms(self) -> float:
        """Guardrail/compile cost over raw SQL, HTTP excluded."""
        return self.pipeline.mean_ms - self.baseline.mean_ms

    @property
    def rest_overhead_ms(self) -> float:
        """What a real REST client experiences beyond raw SQL access."""
        return self.rest.mean_ms - self.baseline.mean_ms

    @property
    def rest_overhead_pct(self) -> float:
        if self.baseline.mean_ms <= 0:
            return 0.0
        return (self.rest_overhead_ms / self.baseline.mean_ms) * 100.0


class PerformanceBenchmarkReport(pyd.BaseModel):
    dialect: str = "postgresql"
    seeded_rows: int
    iterations_per_scenario: int
    warmup_per_scenario: int
    scenarios: List[ScenarioResult]

    @property
    def mean_rest_overhead_ms(self) -> float:
        if not self.scenarios:
            return 0.0
        return statistics.fmean(s.rest_overhead_ms for s in self.scenarios)

    @property
    def mean_pipeline_overhead_ms(self) -> float:
        if not self.scenarios:
            return 0.0
        return statistics.fmean(s.pipeline_overhead_ms for s in self.scenarios)


# --------------------------------------------------------------------------- #
# Scenarios — each produces the same logical query as a raw SQL statement and
# an equivalent StructuredQuery payload, over the benchmark's own seeded
# table. `values()` is the SINGLE place each scenario's literals are computed;
# `raw_sql()`/`structured_query()` both take that same dict rather than each
# re-deriving it from `seed`, so the two representations cannot silently drift
# into comparing different queries (a 2026-08-11 test-contract-reviewer
# finding on the first version of this file, which had each method compute
# `seed % 10` independently). A fresh literal per iteration (drawn from the
# seeded id range) avoids a single cached plan/row flattering one tier over
# another.
# --------------------------------------------------------------------------- #


class _Scenario:
    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description

    def values(self, seed: int) -> Dict[str, Any]:
        raise NotImplementedError

    def raw_sql(self, table: str, values: Dict[str, Any]) -> tuple[sa.TextClause, Dict[str, Any]]:
        raise NotImplementedError

    def structured_query(self, table: str, values: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def for_seed(
        self, table: str, seed: int
    ) -> tuple[tuple[sa.TextClause, Dict[str, Any]], Dict[str, Any]]:
        """Compute `values` once and hand the identical dict to both
        representations — the structural guarantee against drift."""
        values = self.values(seed)
        return self.raw_sql(table, values), self.structured_query(table, values)


class _PointLookup(_Scenario):
    def __init__(self) -> None:
        super().__init__("point_lookup", "SELECT by primary key, LIMIT 1")

    def values(self, seed):
        return {"id": seed}

    def raw_sql(self, table, values):
        # `table` is this benchmark's own locally-generated
        # `querygate_perf_bench_<uuid>` name, never caller/AST-controlled; every
        # predicate value below is a bound parameter. Diagnostic tooling, not the
        # StructuredQuery pipeline the "no raw SQL" invariant governs — raw SQL is
        # deliberately the *baseline* this harness measures QueryGate against.
        # `# fmt: off` keeps the `# nosemgrep` on the `sa.text(...)` match line:
        # semgrep anchors a multi-line match to its FIRST line, so a suppression
        # that black wraps onto a later line silently stops applying.
        sql = f"SELECT id, category, amount FROM {table} WHERE id = :id LIMIT 1"  # nosec B608
        # fmt: off
        return sa.text(sql), {"id": values["id"]}  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        # fmt: on

    def structured_query(self, table, values):
        return {
            "from": table,
            "select": [f"{table}.id", f"{table}.category", f"{table}.amount"],
            "where": {"col": f"{table}.id", "op": "eq", "value": values["id"]},
            "limit": 1,
        }


class _FilteredScan(_Scenario):
    def __init__(self) -> None:
        super().__init__("filtered_scan", "WHERE + ORDER BY + LIMIT over an unindexed predicate")

    def values(self, seed):
        return {"category": f"cat_{seed % 10}", "amount": float(seed % 500)}

    def raw_sql(self, table, values):
        # `table` is locally generated, never caller/AST-controlled; every
        # predicate value is a bound parameter. See _PointLookup.raw_sql for the
        # full rationale and for why `# fmt: off` pins the `# nosemgrep`.
        sql = (
            f"SELECT id, category, amount, created_at FROM {table} "  # nosec B608
            f"WHERE category = :category AND amount > :amount "
            f"ORDER BY amount DESC LIMIT 50"
        )
        # fmt: off
        return sa.text(sql), {"category": values["category"], "amount": values["amount"]}  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        # fmt: on

    def structured_query(self, table, values):
        return {
            "from": table,
            "select": [
                f"{table}.id",
                f"{table}.category",
                f"{table}.amount",
                f"{table}.created_at",
            ],
            "where": {
                "and": [
                    {"col": f"{table}.category", "op": "eq", "value": values["category"]},
                    {"col": f"{table}.amount", "op": "gt", "value": values["amount"]},
                ]
            },
            "order_by": [{"col": f"{table}.amount", "dir": "desc"}],
            "limit": 50,
        }


class _Aggregation(_Scenario):
    def __init__(self) -> None:
        super().__init__("aggregation", "GROUP BY with COUNT/SUM over the full table")

    def values(self, seed):
        return {}

    def raw_sql(self, table, values):
        # Explicit LIMIT matching the pipeline tier's Policy.default_limit
        # (see run_performance_benchmark) — the AST has no `limit` field
        # either, so without this the compiled query silently carries a
        # LIMIT the raw-SQL baseline doesn't, comparing two different
        # queries even though today's 10-category seed data makes the
        # LIMIT a no-op either way (2026-08-11 security-invariant-reviewer
        # finding).
        # `table` is locally generated, never caller/AST-controlled; no predicate
        # values here at all. See _PointLookup.raw_sql for the full rationale and
        # for why `# fmt: off` pins the `# nosemgrep`.
        sql = (
            f"SELECT category, count(*) AS n, sum(amount) AS total "  # nosec B608
            f"FROM {table} GROUP BY category ORDER BY category LIMIT 100"
        )
        # fmt: off
        return sa.text(sql), {}  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        # fmt: on

    def structured_query(self, table, values):
        return {
            "from": table,
            "select": [
                f"{table}.category",
                {"fn": "count", "col": "*", "as": "n"},
                {"fn": "sum", "col": f"{table}.amount", "as": "total"},
            ],
            "group_by": [f"{table}.category"],
            "order_by": [{"col": f"{table}.category", "dir": "asc"}],
            "limit": 100,
        }


SCENARIOS: List[_Scenario] = [_PointLookup(), _FilteredScan(), _Aggregation()]


# --------------------------------------------------------------------------- #
# Fixture setup/teardown
# --------------------------------------------------------------------------- #


async def _create_and_seed_table(engine: AsyncEngine, table: str, rows: int) -> None:
    # Every statement below is benchmark fixture DDL/DML over this harness's own
    # locally-generated `querygate_perf_bench_<uuid>` table name — never
    # caller/AST-controlled, and not the StructuredQuery pipeline the "no raw SQL"
    # invariant governs. `# fmt: off` pins each `# nosemgrep` to the `sa.text(...)`
    # line semgrep anchors on (see _PointLookup.raw_sql).
    async with engine.begin() as conn:
        # fmt: off
        await conn.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        # fmt: on
        create_sql = (
            f"CREATE TABLE {table} ("
            f"id integer PRIMARY KEY, "
            f"category text NOT NULL, "
            f"amount numeric NOT NULL, "
            f"created_at timestamptz NOT NULL DEFAULT now())"
        )
        # fmt: off
        await conn.execute(sa.text(create_sql))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        # fmt: on
        rng = random.Random(
            1234
        )  # nosec B311 — deterministic fixture-data seeding, not a security/cryptographic use
        batch = [
            {"id": i, "category": f"cat_{i % 10}", "amount": round(rng.uniform(0, 1000), 2)}
            for i in range(rows)
        ]
        insert_sql = (
            f"INSERT INTO {table} (id, category, amount) "  # nosec B608
            f"VALUES (:id, :category, :amount)"
        )
        # fmt: off
        await conn.execute(sa.text(insert_sql), batch)  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        await conn.execute(sa.text(f"CREATE INDEX ON {table} (category)"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        await conn.execute(sa.text(f"ANALYZE {table}"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        # fmt: on


async def _drop_table(engine: AsyncEngine, table: str) -> None:
    # Same locally-generated table name and same `# fmt: off` rationale as
    # _create_and_seed_table above.
    async with engine.begin() as conn:
        # fmt: off
        await conn.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        # fmt: on


# --------------------------------------------------------------------------- #
# Timing
# --------------------------------------------------------------------------- #


async def _time_baseline(engine: AsyncEngine, stmt: sa.TextClause, params: Dict[str, Any]) -> float:
    start = time.perf_counter()
    async with engine.connect() as conn:
        result = await conn.execute(stmt, params)
        result.fetchall()
    return (time.perf_counter() - start) * 1000.0


async def _time_pipeline(service: StructuredQueryService, payload: Dict[str, Any]) -> float:
    query = StructuredQuery.model_validate(payload)
    start = time.perf_counter()
    await service.execute(query)
    return (time.perf_counter() - start) * 1000.0


async def _time_rest(client: AsyncClient, payload: Dict[str, Any]) -> float:
    start = time.perf_counter()
    response = await client.post(f"/api/v1/{_CONNECTION_ID}/query", json=payload)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    response.raise_for_status()
    return elapsed_ms


async def _run_scenario(
    scenario: _Scenario,
    *,
    table: str,
    rows: int,
    engine: AsyncEngine,
    service: StructuredQueryService,
    client: AsyncClient,
    iterations: int,
    warmup: int,
) -> ScenarioResult:
    rng = random.Random(  # nosec B311 — deterministic per-scenario seed selection, not a security/cryptographic use
        hash(scenario.name) & 0xFFFF
    )

    def seed() -> int:
        return rng.randint(0, max(rows - 1, 0))

    for _ in range(warmup):
        (stmt, params), payload = scenario.for_seed(table, seed())
        await _time_baseline(engine, stmt, params)
        await _time_pipeline(service, payload)
        await _time_rest(client, payload)

    baseline_samples: List[float] = []
    pipeline_samples: List[float] = []
    rest_samples: List[float] = []
    for _ in range(iterations):
        (stmt, params), payload = scenario.for_seed(table, seed())
        baseline_samples.append(await _time_baseline(engine, stmt, params))
        pipeline_samples.append(await _time_pipeline(service, payload))
        rest_samples.append(await _time_rest(client, payload))

    return ScenarioResult(
        name=scenario.name,
        description=scenario.description,
        iterations=iterations,
        baseline=LatencyStats.from_samples(baseline_samples),
        pipeline=LatencyStats.from_samples(pipeline_samples),
        rest=LatencyStats.from_samples(rest_samples),
    )


@dataclass
class BenchmarkEnvironment:
    """What every benchmark in this module needs: a real, seeded table and a
    real, driveable QueryGate stack pointed at it."""

    dialect: str
    table: str
    baseline_engine: AsyncEngine
    service: StructuredQueryService
    client: AsyncClient


@asynccontextmanager
async def _benchmark_environment(
    connection_string: str,
    *,
    rows: int,
    pool_size: int = 5,
    max_overflow: int = 5,
    max_concurrency: int = 8,
    concurrency_wait_seconds: float = 10,
) -> AsyncIterator[BenchmarkEnvironment]:
    """Shared fixture lifecycle for `run_performance_benchmark`'s three
    in-process tiers (`baseline`/`pipeline`/`rest`, all driven via
    `httpx.ASGITransport` in this same process). `load_benchmark.py`'s
    `run_load_benchmark` does NOT use this — its REST tier is a real,
    separately-spawned `uvicorn` subprocess over a real socket (see that
    module's docstring for why), so it needs none of the process-global
    snapshot/restore or re-entrancy guard this fixture exists for:

    - **Postgres-only guard**, enforced before either engine is constructed —
      also what makes the `create_async_engine` exemption for this file in
      `tests/unit/test_connections_engine.py` true (a caller-supplied string
      that can never resolve to a not-yet-connectable dialect).
    - **Self-contained fixture table**, seeded and dropped in a `finally`
      regardless of outcome (including a mid-run exception).
    - **Process-global state (connection registry, policy store, audit sink,
      concurrency semaphore) is snapshotted and restored**, not just
      overwritten — safe to call from a process that is not *concurrently*
      serving requests or running another benchmark. It still takes over
      that process-global state and suppresses audit persistence
      process-wide for the duration of the run, so it must never be wired
      into anything that shares a process with real traffic; the
      re-entrancy guard below turns a concurrent second call into a loud
      error instead of a silent state clobber (2026-08-11
      security-invariant-reviewer finding).
    - **Audit persistence is suppressed explicitly**, by direct module-
      attribute assignment rather than `set_audit_sink()` (which closes the
      sink it replaces — today harmless since every shipped sink's `close()`
      is a no-op, but this avoids ever closing an operator's real sink) —
      and not via `AppConfig(audit_sink_backend="none")` alone, since that
      field is only ever applied by `create_app`'s FastAPI `lifespan`, which
      the ASGI transport driving the `rest` tier here never runs.

    `pool_size`/`max_overflow` size both the raw baseline engine and
    QueryGate's own connection pool identically, so neither tier is
    artificially pool-starved relative to the other under concurrency.
    `max_concurrency`/`concurrency_wait_seconds` configure the policy's
    admission control; callers of this single-request benchmark leave these
    at `Policy`'s own defaults (8 / 10s) since it never drives concurrent
    traffic (see `docs/LOAD_TESTING.md`/`make test-load` for admission-
    guardrail correctness, and `load_benchmark.py` for throughput under
    concurrency, which sizes its own separately-spawned server's policy
    instead).
    """
    global _benchmark_active
    if _benchmark_active:
        raise RuntimeError(
            "a QueryGate performance benchmark is already running in this "
            "process — run_performance_benchmark takes over the "
            "process-global connection registry, policy store, and audit "
            "sink for its duration and cannot run concurrently with "
            "itself."
        )

    backend = sa.engine.url.make_url(connection_string).get_backend_name()
    if backend != "postgresql":
        raise ValueError(
            f"performance_benchmark only targets Postgres today (its seed DDL and "
            f"scenario SQL are Postgres-specific); got dialect {backend!r} from "
            f"connection_string."
        )

    # Set BEFORE the try, reset in the matching outer finally below, so a
    # failure anywhere in this function — including engine construction,
    # which can itself raise (e.g. a malformed connection_string reaching
    # create_async_engine) — always clears the flag. A previous version set
    # this flag before entering any try/finally at all: an exception raised
    # by engine construction left `_benchmark_active` stuck True forever,
    # turning every subsequent call in the same process into a false
    # "already running" report instead of the real error (2026-08-11
    # security-invariant-reviewer finding).
    _benchmark_active = True
    try:
        table = f"querygate_perf_bench_{uuid.uuid4().hex[:8]}"
        admin_engine = create_async_engine(connection_string, pool_pre_ping=True)
        baseline_engine = create_async_engine(
            connection_string, pool_pre_ping=True, pool_size=pool_size, max_overflow=max_overflow
        )
        table_created = False

        # Snapshot process-global state this call is about to overwrite, reading
        # the module attribute directly rather than through get_registry()/
        # get_policy_store() — those lazily load the operator's real config file
        # on first call, which would raise outright in a fresh checkout with no
        # connections.yaml yet (this tool's most common "just run it against the
        # demo db" use case) and would be unwanted I/O for what is only a
        # snapshot.
        previous_registry = _registry_module._registry
        previous_policy_store = _policy_module._store
        previous_audit_sink: AuditSink = get_audit_sink()
        try:
            await _create_and_seed_table(admin_engine, table, rows)
            table_created = True

            set_registry(
                ConnectionRegistry(
                    {
                        _CONNECTION_ID: ConnectionProfile(
                            id=_CONNECTION_ID,
                            dialect="postgresql",
                            connection_string=connection_string,
                            known_tables=[table],
                        )
                    }
                )
            )
            set_policy_store(
                PolicyStore(
                    default=Policy(
                        allowed_tables=[table],
                        max_limit=1000,
                        default_limit=100,
                        max_concurrency=max_concurrency,
                        concurrency_wait_seconds=concurrency_wait_seconds,
                    ),
                    overrides={},
                )
            )
            in_process_limiter().reset_semaphore(_CONNECTION_ID)
            # Direct module-attribute assignment, not set_audit_sink() — that
            # setter calls .close() on the sink it replaces (audit/sinks.py's
            # set_audit_sink), which would permanently close the operator's real
            # sink here (today harmless: every shipped sink's close() is a no-op,
            # confirmed by reading all four — but a future sink with a real
            # close() would make this benchmark silently and permanently kill
            # audit persistence for the rest of the process, surviving even the
            # restore below. 2026-08-11 security-invariant-reviewer finding).
            _audit_sinks_module._sink = NullAuditSink()

            app = create_app(
                AppConfig(
                    environment="localhost",
                    mcp_enabled=False,
                    audit_sink_backend="none",
                    # `AppConfig` reads the repo's .env by default (core/config.py's
                    # `env_file = ".env"`); an operator's local .env may set real
                    # API_KEYS for `make run-dev`. Force anonymous auth explicitly
                    # so the benchmark's REST tier is reproducible regardless of
                    # the machine it runs on, the same isolation
                    # tests/conftest.py gives the test suite.
                    api_keys=[],
                    jwt_enabled=False,
                    pool_size=pool_size,
                    conn_max_overflow=max_overflow,
                )
            )
            # This app is driven only via the in-process ASGITransport below and
            # is never bound to a socket — do not reuse this construction (an
            # anonymous, auth-disabled create_app()) anywhere that serves real
            # traffic.
            service = StructuredQueryService(
                connection_id=_CONNECTION_ID,
                principal=Principal(subject="performance-benchmark"),
                surface="internal",
            )

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://localhost") as client:
                yield BenchmarkEnvironment(
                    dialect=backend,
                    table=table,
                    baseline_engine=baseline_engine,
                    service=service,
                    client=client,
                )
        finally:
            _registry_module._registry = previous_registry
            _policy_module._store = previous_policy_store
            _audit_sinks_module._sink = previous_audit_sink
            in_process_limiter().reset_semaphore(_CONNECTION_ID)
            # dispose_engine also clears schema/reflection.py's per-connection
            # _METADATA_LOCKS entry (2026-08-11 architecture-boundary-reviewer
            # finding: that fix belongs in connections/engine.py, the module that
            # owns the engine lifecycle, not duplicated here — every caller gets
            # it for free now, not just this benchmark).
            try:
                await dispose_engine(_CONNECTION_ID)
            except Exception:
                get_logger().warning(
                    "performance_benchmark.dispose_engine_failed", connection_id=_CONNECTION_ID
                )
            try:
                if table_created:
                    await _drop_table(admin_engine, table)
            finally:
                await admin_engine.dispose()
                await baseline_engine.dispose()
    finally:
        _benchmark_active = False


async def run_performance_benchmark(
    *,
    connection_string: str = DEFAULT_CONNECTION_STRING,
    rows: int = 5000,
    iterations: int = 30,
    warmup: int = 5,
) -> PerformanceBenchmarkReport:
    """Run every scenario's three timing tiers against a real Postgres instance.

    Needs a reachable Postgres (``make compose-up``); this is a real-database
    benchmark, not an offline one — see the module docstring for why that's
    the right tradeoff here. See `_benchmark_environment` for this function's
    own fixture lifecycle and process-global state handling (NOT shared with
    `load_benchmark.py`'s `run_load_benchmark` — see that module's docstring
    for why it spawns a real subprocess instead).
    """
    if rows < 1:
        raise ValueError("rows must be at least 1")
    if rows > _MAX_ROWS:
        raise ValueError(
            f"rows must be at most {_MAX_ROWS} — a larger value materializes the whole "
            f"seed batch in memory before any database round trip (matches "
            f"load_benchmark.py's identical guard)."
        )
    async with _benchmark_environment(connection_string, rows=rows) as env:
        scenarios = [
            await _run_scenario(
                scenario,
                table=env.table,
                rows=rows,
                engine=env.baseline_engine,
                service=env.service,
                client=env.client,
                iterations=iterations,
                warmup=warmup,
            )
            for scenario in SCENARIOS
        ]
        return PerformanceBenchmarkReport(
            dialect=env.dialect,
            seeded_rows=rows,
            iterations_per_scenario=iterations,
            warmup_per_scenario=warmup,
            scenarios=scenarios,
        )
