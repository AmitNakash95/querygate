"""Stress + correctness tests against the large, real-world-shaped demo domain.

These exercise QueryGate the way the MCP playground
(examples/MCP_PLAYGROUND.md) does — at volume, over the ``retail`` and
``analytics`` connections — but assert automatically. Four classes:

1. ``TestDifferentialCorrectness`` — the high-leverage one. Because
   ``examples/demo_db/generate_large.generate`` is pure, deterministic Python,
   the same in-memory rows that seed Postgres are the *oracle*: each query's
   expected answer is computed in plain Python over those dicts and compared
   against what QueryGate returns through the full validate/compile/execute
   pipeline. Catches compiler/validation regressions that a 20-row fixture
   would sail past.

2. ``TestSecurityInvariantsAtVolume`` — the walls hold across *thousands* of
   rows, not one: PII masking (hash + last-4), column denial, aggregate
   k-anonymity, the mandatory active-only row filter, and credential
   redaction.

3. ``TestConcurrentBurst`` (``load``) — a burst of genuinely heavy analytical
   queries stays correct and stable, never exceeds the concurrency cap at the
   database boundary, keeps p95 latency under a loose budget, and leaks no
   pooled connections.

4. ``TestSoak`` (``load``) — repeats the burst for ``QUERYGATE_LOAD_ROUNDS``
   rounds, asserting the guarantees hold every round with no pool-leak drift.

Needs a real Postgres. Run ``make compose-up`` first, then::

    make test-stress          # classes 1-2 (fast, no timing sensitivity)
    make test-load            # + class 3
    make test-soak SOAK_ROUNDS=200   # + class 4, many rounds

All are excluded from the default suite (see pyproject.toml's ``addopts``).
The session fixture seeds its own copy of the large domain (drop + recreate at
``LARGE_STRESS_SCALE``, default 0.1), so it does not depend on ``make
seed-large`` having been run — but note it DOES replace whatever the large
tables currently hold. Determinism means the oracle assertions are exact.
"""

from __future__ import annotations

import asyncio
import math
import os
import statistics
import time
from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, List

import json

import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from loguru import logger as loguru_logger
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from examples.demo_db.generate_large import _seed_engine, generate
from querygate.api.app import create_app
from querygate.connections.engine import ENGINES, dispose_engine, get_engine, reset_engines
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.core.config import AppConfig
from querygate.execution.concurrency import in_process_limiter
from querygate.execution.service import StructuredQueryService
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.query_ast.models import StructuredQuery

pytestmark = [pytest.mark.integration, pytest.mark.real_db, pytest.mark.postgres_live]

_CONNECTION_STRING = os.environ.get(
    "QUERYGATE_TEST_POSTGRES_URL",
    os.environ.get(
        "QUERYGATE_DEMO_DB_URL",
        "postgresql+asyncpg://querygate:querygate@localhost:5433/querygate_demo",
    ),
)
_SCALE = float(os.environ.get("LARGE_STRESS_SCALE", "0.1"))
_EXAMPLES = "examples"


# --------------------------------------------------------------------------
# Fixtures: seed once, apply the shipped example config, hand tests the oracle
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def large_domain() -> Dict[str, List[dict]]:
    """Deterministically generate + seed the large domain into Postgres once,
    and return the in-memory rows to act as the oracle for every test.

    Synchronous (seeds via its own throwaway event loop) so the session scope
    doesn't collide with pytest-asyncio's per-test function-scoped loop.
    """
    data = generate(_SCALE)
    asyncio.run(_seed_engine(_CONNECTION_STRING, data, drop=True))
    return data


def _apply_example_config() -> None:
    """Load the SHIPPED example connections + policy so the tests verify the
    real configuration (masks/denies/k-anonymity/mandatory-filter) end to end,
    not a reconstruction of it. Points every ${QUERYGATE_DEMO_DB_URL} at the
    test Postgres.
    """
    import querygate.schema.reflection as reflection

    os.environ["QUERYGATE_DEMO_DB_URL"] = _CONNECTION_STRING
    set_registry(ConnectionRegistry.from_file(f"{_EXAMPLES}/connections.example.yaml"))
    set_policy_store(PolicyStore.from_file(f"{_EXAMPLES}/policy.example.yaml"))
    reset_engines()
    in_process_limiter().clear()
    # reset_engines() clears the reflection metadata cache, so the next query
    # re-reflects — which acquires a per-connection asyncio.Lock. pytest-asyncio
    # gives each test its own event loop, so a lock cached from a prior test is
    # bound to a dead loop. Drop them so fresh locks bind to the current loop.
    reflection._METADATA_LOCKS.clear()


@pytest_asyncio.fixture
async def configured(large_domain):
    """Function-scoped: (re)apply the example config for a service-level test
    and dispose real engines afterward so no asyncpg connections leak.
    """
    _apply_example_config()
    yield large_domain
    for connection_id in list(ENGINES):
        await dispose_engine(connection_id)


async def _run(connection_id: str, query: dict) -> List[dict]:
    service = StructuredQueryService(connection_id=connection_id, surface="rest")
    result = await service.execute(StructuredQuery.model_validate(query))
    return result.rows


# --------------------------------------------------------------------------
# Oracles: plain Python over the generated dicts (the ground truth)
# --------------------------------------------------------------------------


def _percentile_cont(values: List[float], fraction: float) -> float:
    """Continuous-interpolation percentile matching Postgres percentile_cont:
    rank = fraction*(n-1), linear interpolation between the bracketing values.
    """
    xs = sorted(values)
    if not xs:
        return math.nan
    rank = fraction * (len(xs) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return xs[lo]
    return xs[lo] + (rank - lo) * (xs[hi] - xs[lo])


def _oracle_status_counts(data) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    for o in data["orders_ext"]:
        counts[o["status"]] += 1
    return dict(counts)


def _oracle_yearly_revenue(data) -> Dict[int, Decimal]:
    rev: Dict[int, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for o in data["orders_ext"]:
        rev[o["placed_at"].year] += o["order_total"]
    return dict(rev)


def _oracle_revenue_by_region_category(data) -> Dict[tuple, Decimal]:
    region_name = {r["region_id"]: r["name"] for r in data["regions"]}
    order_region = {o["order_id"]: o["region_id"] for o in data["orders_ext"]}
    product_cat = {p["product_id"]: p["category"] for p in data["catalog_products"]}
    rev: Dict[tuple, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for line in data["order_lines"]:
        key = (region_name[order_region[line["order_id"]]], product_cat[line["product_id"]])
        rev[key] += line["line_total"]
    return dict(rev)


def _oracle_channel_order_totals(data) -> Dict[str, List[float]]:
    by_channel: Dict[str, List[float]] = defaultdict(list)
    for o in data["orders_ext"]:
        by_channel[o["channel"]].append(float(o["order_total"]))
    return dict(by_channel)


def _oracle_spend_by_region_account(data) -> Dict[int, Dict[int, Decimal]]:
    spend: Dict[int, Dict[int, Decimal]] = defaultdict(lambda: defaultdict(lambda: Decimal("0.00")))
    for o in data["orders_ext"]:
        spend[o["region_id"]][o["account_id"]] += o["order_total"]
    return spend


# --------------------------------------------------------------------------
# 1. Differential correctness at volume
# --------------------------------------------------------------------------


class TestDifferentialCorrectness:
    @pytest.mark.asyncio
    async def test_status_counts_match_oracle(self, configured):
        rows = await _run(
            "retail",
            {
                "from": "orders_ext",
                "select": ["orders_ext.status", {"fn": "count", "col": "*", "as": "n"}],
                "group_by": ["orders_ext.status"],
                "limit": 100,
            },
        )
        got = {r["status"]: r["n"] for r in rows}
        assert got == _oracle_status_counts(configured)

    @pytest.mark.asyncio
    async def test_yearly_revenue_date_bucket_matches_oracle(self, configured):
        rows = await _run(
            "retail",
            {
                "from": "orders_ext",
                "select": [
                    {"col": "orders_ext.placed_at", "granularity": "year", "as": "bucket"},
                    {"fn": "sum", "col": "orders_ext.order_total", "as": "revenue"},
                ],
                "group_by": ["bucket"],
                "order_by": [{"col": "bucket", "dir": "asc"}],
                "limit": 100,
            },
        )
        got = {r["bucket"].year: r["revenue"] for r in rows}
        assert got == _oracle_yearly_revenue(configured)

    @pytest.mark.asyncio
    async def test_multi_hop_join_revenue_matches_oracle(self, configured):
        rows = await _run(
            "retail",
            {
                "from": "order_lines",
                "select": [
                    "regions.name",
                    "catalog_products.category",
                    {"fn": "sum", "col": "order_lines.line_total", "as": "revenue"},
                ],
                "joins": [
                    {"table": "orders_ext", "on": ["order_lines.order_id", "orders_ext.order_id"]},
                    {"table": "regions", "on": ["orders_ext.region_id", "regions.region_id"]},
                    {
                        "table": "catalog_products",
                        "on": ["order_lines.product_id", "catalog_products.product_id"],
                    },
                ],
                "group_by": ["regions.name", "catalog_products.category"],
                "limit": 500,
            },
        )
        got = {(r["name"], r["category"]): r["revenue"] for r in rows}
        assert got == _oracle_revenue_by_region_category(configured)

    @pytest.mark.asyncio
    async def test_channel_median_and_aggregates_match_oracle(self, configured):
        rows = await _run(
            "retail",
            {
                "from": "orders_ext",
                "select": [
                    "orders_ext.channel",
                    {"col": "orders_ext.order_total", "fraction": 0.5, "as": "median"},
                    {"fn": "avg", "col": "orders_ext.order_total", "as": "mean"},
                    {"fn": "min", "col": "orders_ext.order_total", "as": "lo"},
                    {"fn": "max", "col": "orders_ext.order_total", "as": "hi"},
                    {"fn": "stddev", "col": "orders_ext.order_total", "as": "sd"},
                ],
                "group_by": ["orders_ext.channel"],
                "limit": 100,
            },
        )
        oracle = _oracle_channel_order_totals(configured)
        assert set(r["channel"] for r in rows) == set(oracle)
        for r in rows:
            xs = oracle[r["channel"]]
            # abs=0.01: median of 2-decimal currency is only meaningful to the
            # cent, and even-count percentile_cont interpolates to a half-cent
            # that Postgres rounds differently than a plain float — not a bug.
            assert float(r["median"]) == pytest.approx(_percentile_cont(xs, 0.5), abs=0.01)
            assert float(r["mean"]) == pytest.approx(statistics.fmean(xs), rel=1e-6)
            assert float(r["lo"]) == pytest.approx(min(xs), rel=1e-9)
            assert float(r["hi"]) == pytest.approx(max(xs), rel=1e-9)
            assert float(r["sd"]) == pytest.approx(statistics.stdev(xs), rel=1e-6)

    @pytest.mark.asyncio
    async def test_top_n_per_region_matches_oracle(self, configured):
        rows = await _run(
            "retail",
            {
                "from": "orders_ext",
                "select": [
                    "orders_ext.region_id",
                    "orders_ext.account_id",
                    {"fn": "sum", "col": "orders_ext.order_total", "as": "spend"},
                ],
                "group_by": ["orders_ext.region_id", "orders_ext.account_id"],
                "top_n": {
                    "partition_by": ["orders_ext.region_id"],
                    "order_by": [{"col": "spend", "dir": "desc"}],
                    "n": 3,
                },
                "limit": 500,
            },
        )
        by_region: Dict[int, List[Decimal]] = defaultdict(list)
        for r in rows:
            by_region[r["region_id"]].append(r["spend"])
        oracle = _oracle_spend_by_region_account(configured)
        for region_id, spends in by_region.items():
            top = sorted(oracle[region_id].values(), reverse=True)[:3]
            # row_number can break exact-tie boundaries arbitrarily, so assert
            # the unambiguous invariants: the #1 spend and the row count.
            assert max(spends) == top[0]
            assert len(spends) == min(3, len(oracle[region_id]))

    @pytest.mark.asyncio
    async def test_null_handling_and_distinct(self, configured):
        # Count accounts that never logged in (last_login_at IS NULL).
        rows = await _run(
            "retail",
            {
                "from": "accounts",
                "select": [{"fn": "count", "col": "*", "as": "n"}],
                "where": {"col": "accounts.last_login_at", "op": "is_null"},
                "limit": 10,
            },
        )
        oracle_nulls = sum(1 for a in configured["accounts"] if a["last_login_at"] is None)
        assert rows[0]["n"] == oracle_nulls

        # DISTINCT tiers present.
        tier_rows = await _run(
            "retail",
            {
                "from": "accounts",
                "select": ["accounts.tier"],
                "distinct": True,
                "limit": 100,
            },
        )
        assert set(r["tier"] for r in tier_rows) == set(a["tier"] for a in configured["accounts"])


# --------------------------------------------------------------------------
# 2. Security invariants at volume
# --------------------------------------------------------------------------


class TestSecurityInvariantsAtVolume:
    @pytest.mark.asyncio
    async def test_retail_pii_is_masked_across_every_row(self, configured):
        rows = await _run(
            "retail",
            {
                "from": "accounts",
                "select": ["accounts.national_id", "accounts.phone"],
                "limit": 500,
            },
        )
        assert len(rows) >= 100  # genuinely at volume, not a single row
        raw_national_ids = {a["national_id"] for a in configured["accounts"]}
        for r in rows:
            # national_id is a 32-char md5 hash, never a raw SSN-shaped value.
            assert len(r["national_id"]) == 32
            assert all(c in "0123456789abcdef" for c in r["national_id"])
            assert r["national_id"] not in raw_national_ids
            # phone reveals only the last 4 chars (or is NULL).
            if r["phone"] is not None:
                assert len(r["phone"]) == 4

    @pytest.mark.asyncio
    async def test_analytics_denies_pii_columns(self, configured):
        for col in ("email", "national_id", "phone"):
            with pytest.raises(Exception) as exc:
                await _run(
                    "analytics",
                    {"from": "accounts", "select": [f"accounts.{col}"], "limit": 5},
                )
            assert "not accessible" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_analytics_k_anonymity_suppresses_thin_groups(self, configured):
        # Group by a high-cardinality column so many raw groups are < 5 rows;
        # every SURVIVING group must have count >= 5.
        rows = await _run(
            "analytics",
            {
                "from": "orders_ext",
                "select": [
                    "orders_ext.account_id",
                    {"fn": "count", "col": "*", "as": "n"},
                ],
                "group_by": ["orders_ext.account_id"],
                "limit": 100,
            },
        )
        assert rows, "expected at least one surviving group"
        assert all(r["n"] >= 5 for r in rows)

    @pytest.mark.asyncio
    async def test_analytics_mandatory_active_filter_always_applied(self, configured):
        rows = await _run(
            "analytics",
            {
                "from": "accounts",
                "select": ["accounts.status", {"fn": "count", "col": "*", "as": "n"}],
                "group_by": ["accounts.status"],
                "limit": 100,
            },
        )
        # Only active accounts are ever visible on analytics, regardless of
        # what the caller asked for.
        assert set(r["status"] for r in rows) == {"active"}
        oracle_active = sum(1 for a in configured["accounts"] if a["status"] == "active")
        assert sum(r["n"] for r in rows) == oracle_active

    @pytest.mark.asyncio
    async def test_walled_staff_table_is_unreachable(self, configured):
        for conn in ("retail", "analytics"):
            with pytest.raises(Exception) as exc:
                await _run(conn, {"from": "staff", "select": ["staff.salary"], "limit": 5})
            assert "not accessible" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_no_credential_leaks_in_public_connection_info(self, configured):
        # PublicConnectionInfo (the only thing returned to callers) carries no
        # credential field at all — assert structurally across every profile.
        from querygate.connections.registry import get_registry

        for info in get_registry().list_public():
            dumped = info.model_dump()
            assert "connection_string" not in dumped
            assert not any("://" in str(v) for v in dumped.values())


# --------------------------------------------------------------------------
# 2b. Nothing that shouldn't leak ever reaches the logs / audit trail
# --------------------------------------------------------------------------


@pytest.fixture
def captured_logs():
    """Capture every emitted log record (message + bound structured fields +
    exception text) for the duration of a test.

    QueryGate logs via loguru to a JSON-lines stdout sink (see
    core/logging.py). We add a second in-memory sink AFTER triggering the
    module's own initialization (which removes all sinks on first call), so
    ours survives, then drop it on teardown.
    """
    from querygate.core.logging import get_logger

    get_logger()  # force core-logging init so our sink isn't removed afterward
    entries: List[dict] = []

    def _sink(message) -> None:
        record = message.record
        exc = record["exception"]
        entries.append(
            {
                "message": record["message"],
                "extra": dict(record["extra"]),
                "exception": str(exc.value) if exc and exc.value else None,
            }
        )

    sink_id = loguru_logger.add(_sink, level="DEBUG")
    try:
        yield entries
    finally:
        loguru_logger.remove(sink_id)


_SENTINEL = "ZZ_SENTINEL_PREDICATE_LEAK_CHECK"


def _forbidden_values(data) -> List[str]:
    """Raw PII from the seed data + the caller-supplied predicate sentinel +
    credential fragments — none of which may ever appear in a log or audit
    record.
    """
    forbidden: List[str] = [
        _SENTINEL,
        _CONNECTION_STRING,
        "querygate:querygate@",
        "postgresql+asyncpg",
    ]
    forbidden += [a["national_id"] for a in data["accounts"][:30]]
    forbidden += [a["email"] for a in data["accounts"][:30]]
    forbidden += [a["phone"] for a in data["accounts"][:60] if a["phone"]]
    forbidden += [s["ssn"] for s in data["staff"][:30]]
    forbidden.append(data["accounts"][0]["name"])  # a returned row cell value
    return forbidden


async def _run_leak_battery(data) -> None:
    """Run queries that carry / return values which MUST NOT surface anywhere
    in the log or audit trail: a masked-PII read, a returned-row-value read,
    and a filter carrying a distinctive literal.
    """
    first = data["accounts"][0]
    await _run(
        "retail",
        {"from": "accounts", "select": ["accounts.national_id", "accounts.phone"], "limit": 200},
    )
    await _run(
        "retail",
        {
            "from": "accounts",
            "select": ["accounts.name"],
            "where": {"col": "accounts.account_id", "op": "eq", "value": first["account_id"]},
            "limit": 5,
        },
    )
    await _run(
        "retail",
        {
            "from": "accounts",
            "select": ["accounts.account_id"],
            "where": {"col": "accounts.name", "op": "eq", "value": _SENTINEL},
            "limit": 5,
        },
    )


class TestNoLeakInLogs:
    @pytest.mark.asyncio
    async def test_logs_never_leak_pii_credentials_values_or_rows(self, configured, captured_logs):
        data = configured
        await _run_leak_battery(data)

        blob = json.dumps(captured_logs, default=str)
        for value in _forbidden_values(data):
            assert value not in blob, f"forbidden value leaked into logs: {value!r}"

        # Sanity: we actually captured the audit trail, and it positively
        # redacts bind-parameter values (so the scan can't pass vacuously).
        audit = [e for e in captured_logs if e["message"] == "audit.query"]
        assert audit, "expected audit.query events to have been captured"
        redacted = [e for e in audit if "<redacted>" in str(e["extra"].get("params"))]
        assert redacted, "expected at least one audit event with redacted params"

    @pytest.mark.asyncio
    async def test_persisted_jsonl_audit_sink_never_leaks(self, configured, tmp_path):
        """Same guarantee, but against the on-disk JSONL audit sink (the
        artifact a deployment actually retains), not just the stdout log. The
        persisted AuditEvent keeps a normalized query *shape* (column names +
        operators) but deliberately no SQL, predicate literals, rows, or
        credentials — this proves that holds at volume. The sink is reset and
        the file removed on teardown.
        """
        from querygate.audit.sinks import configure_audit_sink, reset_audit_sink

        data = configured
        audit_path = tmp_path / "audit.jsonl"
        configure_audit_sink(backend="jsonl", jsonl_path=str(audit_path), fsync=True)
        try:
            await _run_leak_battery(data)

            assert audit_path.exists(), "expected the JSONL sink to have persisted events"
            persisted = audit_path.read_text()
            assert persisted.strip(), "expected at least one persisted audit line"

            for value in _forbidden_values(data):
                assert value not in persisted, f"forbidden value leaked into audit file: {value!r}"

            # Sanity: the file really is the audit trail (has connection_id and
            # a decision) and every line is well-formed JSON — so the scan is
            # meaningful, not passing over an empty/garbage file.
            lines = [line for line in persisted.splitlines() if line.strip()]
            events = [json.loads(line) for line in lines]
            assert any(e.get("connection_id") == "retail" for e in events)
            assert all("policy_decision" in e for e in events)
        finally:
            reset_audit_sink()
            audit_path.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# 3 + 4. Concurrent burst + soak
# --------------------------------------------------------------------------

# A genuinely heavy analytical query (two joins + percentile + aggregates +
# group + sort) so a concurrent burst actually contends at the database.
_HEAVY_QUERY = {
    "from": "order_lines",
    "select": [
        "catalog_products.category",
        {"col": "order_lines.line_total", "fraction": 0.9, "as": "p90"},
        {"fn": "sum", "col": "order_lines.line_total", "as": "revenue"},
        {"fn": "count", "col": "*", "as": "lines"},
    ],
    "joins": [
        {"table": "orders_ext", "on": ["order_lines.order_id", "orders_ext.order_id"]},
        {
            "table": "catalog_products",
            "on": ["order_lines.product_id", "catalog_products.product_id"],
        },
    ],
    "group_by": ["catalog_products.category"],
    "order_by": [{"col": "revenue", "dir": "desc"}],
    "limit": 50,
}
_BURST = 32
_P95_BUDGET_SECONDS = 8.0


def _load_rounds() -> int:
    try:
        rounds = int(os.environ.get("QUERYGATE_LOAD_ROUNDS", "3"))
    except ValueError as exc:
        raise pytest.UsageError("QUERYGATE_LOAD_ROUNDS must be a positive integer") from exc
    if rounds < 1:
        raise pytest.UsageError("QUERYGATE_LOAD_ROUNDS must be a positive integer")
    return rounds


@pytest_asyncio.fixture
async def burst_app(large_domain):
    _apply_example_config()
    app = create_app(
        AppConfig(environment="localhost", mcp_enabled=False, audit_sink_backend="none")
    )
    yield app
    for connection_id in list(ENGINES):
        await dispose_engine(connection_id)


async def _peak_active_heavy_queries(monitor: AsyncEngine, stop: asyncio.Event) -> int:
    """Sample pg_stat_activity for our in-flight heavy queries (identified by
    the order_lines/percentile_cont shape) and return the observed peak.
    """
    peak = 0
    async with monitor.connect() as conn:
        while not stop.is_set():
            result = await conn.execute(
                sa.text(
                    """
                    SELECT count(*) FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND state = 'active'
                      AND pid <> pg_backend_pid()
                      AND position('order_lines' in query) > 0
                      AND position('percentile_cont' in query) > 0
                    """
                )
            )
            peak = max(peak, int(result.scalar_one()))
            await conn.commit()
            await asyncio.sleep(0.01)
    return peak


async def _fire_burst(app, monitor: AsyncEngine, count: int) -> Dict[str, Any]:
    stop = asyncio.Event()
    observer = asyncio.create_task(_peak_active_heavy_queries(monitor, stop))
    transport = ASGITransport(app=app)
    timings: List[float] = []
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:

        async def one() -> int:
            start = time.monotonic()
            resp = await client.post("/api/v1/retail/query", json=_HEAVY_QUERY)
            timings.append(time.monotonic() - start)
            return resp.status_code

        try:
            statuses = await asyncio.gather(*[one() for _ in range(count)])
        finally:
            stop.set()
    peak = await observer
    timings.sort()
    p95 = timings[min(len(timings) - 1, int(len(timings) * 0.95))]
    return {"statuses": statuses, "peak": peak, "p95": p95}


class TestConcurrentBurst:
    @pytest.mark.load
    @pytest.mark.asyncio
    async def test_heavy_burst_is_correct_stable_and_capped(self, burst_app):
        cap = get_policy_max_concurrency("retail")
        monitor = create_async_engine(_CONNECTION_STRING, pool_pre_ping=True)
        try:
            outcome = await _fire_burst(burst_app, monitor, _BURST)
        finally:
            await monitor.dispose()

        # Every request completed successfully — no 500s, no crashes.
        assert all(status == 200 for status in outcome["statuses"])
        # The concurrency cap is NEVER exceeded at the database boundary. The
        # observed peak can legitimately be 0 (fast queries can slip between
        # 10ms samples) — "never exceeded" is the guardrail, and that the cap
        # actually *binds* under guaranteed contention is proven separately by
        # test_postgres_load_guardrails.py's pg_sleep probes.
        assert outcome["peak"] <= cap
        # p95 latency stays under a loose, machine-relative budget.
        assert outcome["p95"] < _P95_BUDGET_SECONDS
        # No pooled asyncpg connection leaked once the burst drained.
        assert get_engine("retail").pool.checkedout() == 0


class TestSoak:
    @pytest.mark.load
    @pytest.mark.asyncio
    async def test_repeated_bursts_stay_stable(self, burst_app):
        cap = get_policy_max_concurrency("retail")
        rounds = _load_rounds()
        monitor = create_async_engine(_CONNECTION_STRING, pool_pre_ping=True)
        max_peak = 0
        try:
            for round_number in range(rounds):
                outcome = await _fire_burst(burst_app, monitor, _BURST)
                max_peak = max(max_peak, outcome["peak"])
                assert all(s == 200 for s in outcome["statuses"]), f"round {round_number}"
                # Never exceed the cap (see the burst test for why peak may be
                # 0 in an individual round).
                assert outcome["peak"] <= cap, f"round {round_number}"
                # Pool must return to baseline every round — a leak would
                # accumulate across rounds.
                assert get_engine("retail").pool.checkedout() == 0, f"round {round_number}"
        finally:
            await monitor.dispose()
        # Across many rounds we should have observed genuine concurrency at
        # least once (robust aggregate, unlike a per-round lower bound).
        if rounds >= 3:
            assert max_peak >= 1


def get_policy_max_concurrency(connection_id: str) -> int:
    from querygate.policy.loader import get_policy

    return get_policy(connection_id).max_concurrency
