"""What item 101's two cost levers actually cost, measured on real Postgres.

`Policy.max_window_specs` (default 5) and `Policy.max_window_frame_offset`
(default 1000) bound the two unbounded magnitudes a window AST can carry. They
were originally chosen by judgment; this module is the measurement that either
justifies them or would have corrected them, and pins the findings as
regressions.

Measured against the 25,000-row `web_events` table, twice, before and after
autovacuum refreshed the planner statistics:

| lever                        | run A            | run B            |
| ---------------------------- | ---------------- | ---------------- |
| 1 window                     | 12 ms            | 12 ms            |
| 5 windows (the default cap)  | 44 ms (14.4x)    | 41 ms (14.4x)    |
| 20 windows                   | 218 ms (71x)     | 349 ms (122x)    |
| `MAX` over 10 preceding      | 12 ms            | 12 ms            |
| `MAX` over 1,000 preceding   | **574 ms**       | **~570 ms**      |
| `MAX` over 10,000 preceding  | **3,451 ms**     | **~3,400 ms**    |
| `SUM` over any offset        | ~6 ms — flat     | ~6 ms — flat     |
| planner cost, 5 windows      | 4,914 (15.4x)    | 13,984 (29.3x)   |

Five things follow. The tests below pin the order-of-magnitude findings — the
ratios and the planner's blindness — **not** the individual timings: the bands are
wide (see each assertion's own comment), nothing measures the 10,000-row frame's
runtime, and nothing varies the row count.

1. **The default `max_window_specs=5` keeps a windowed query inside ~15x a plain
   scan** — the one ratio that held across both runs. Per-window cost also rose
   with count: on run B, (wN-w1)/(N-1) gives 7.3 ms/window at 5 and 17.7 at 20.
   That trend is the shape argument for a cap, but it is an observation here, not
   an assertion — and three data points cannot locate a knee, so 5 is a judgement
   inside the measured range rather than a derived optimum.

2. **The frame cost belongs to aggregates the engine cannot compute with inverse
   transitions.** Postgres supplies one for `SUM`/`AVG` over
   int2/int4/int8/numeric/money/interval **only** — over `real`/`double
   precision` they rescan exactly like `MIN`/`MAX`. The flat `SUM` row above is an
   integer column (`web_events.account_id`), i.e. the best case; a float metric
   column pays the `MAX` cost.

3. **These are unlimited-scan numbers, and a compiled read's LIMIT does not
   reliably bound them.** `_framed_sql` runs raw SQL with no LIMIT; a compiled
   top-level read is always clamped (`clamp_limit`; default 50, max 100 for a
   non-aggregate). That clamp bounds the frame rescan only while the query's
   `order_by` is already satisfied by the window's own ordering — otherwise the
   plan is `Limit -> Sort -> WindowAgg` and the blocking sort runs the window over
   every row (0.7 ms vs 583 ms at `LIMIT 50`, pinned by
   `test_an_unrelated_outer_order_by_defeats_the_limit_bound`). The full cost is
   therefore reachable at the top level, and always inside an `IN (subquery)`
   whose LIMIT is stripped. `_n_window_sql` also gives each window a *different*
   `PARTITION BY`, forcing N separate sorts where Postgres would reuse one for
   identical OVER clauses: the deliberate worst case for a cap.

4. **Postgres does not price the frame at all** — identical `EXPLAIN` cost for a
   10-row and a 10,000-row frame. So item 26's cost-estimation gate is
   structurally blind to it *there*, and `max_window_frame_offset` is not a
   redundant belt-and-braces cap: it is the only guardrail that sees the
   difference, with `timeout_seconds` as the backstop. MSSQL's `SHOWPLAN_XML`
   estimator is **unmeasured** for this.

5. **Planner cost is not a stable assertion basis; the 5-window execution ratio
   was.** The same query's planner cost moved 4,914 -> 13,984 between the two runs
   purely from refreshed statistics (+185%, or 15.4x -> 29.3x relative to the
   baseline scan), while its 14.4x wall-clock ratio was identical. (The 20-window
   row moved too — 71x -> 122x — so "execution ratios are stable" holds for the
   cap-sized query, not universally.) Assertions
   therefore use wall-clock ratios with wide margins, and read planner cost only
   for what it answers deterministically: whether the frame is priced at all, and
   direction.

**Data.** These numbers were measured against `web_events` in `querygate_stress`
at 25,000 rows — which is what `test_large_domain_stress.py`'s session fixture
seeds (`LARGE_STRESS_SCALE`, default 0.1, of a 250,000-row table). This module
does NOT seed it: it asserts the precondition and skips with the real command if
it is absent. Note that `make seed-large` is NOT that command — it seeds
`querygate_demo` at scale 1.0, which would both miss this database and break
`test_postgres_schema_discovery`'s "exactly five demo tables". Excluded from the
default run; the four wall-clock tests are additionally `load`-marked, so the CI
`postgres_live and not load` job stays deterministic (`make test-postgres-live`
does not filter `load` and still runs them).
"""

from __future__ import annotations

import json
import os
import statistics
import time

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from querygate.connections.engine import reset_engines
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.core.exceptions import CostEstimateExceededError
from querygate.execution.service import StructuredQueryService
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.query_ast.models import StructuredQuery

pytestmark = [pytest.mark.integration, pytest.mark.real_db, pytest.mark.postgres_live]

# The SAME env var the suite that seeds this data honours — with two names, an
# operator redirecting the stress database moved one module and not the other.
_STRESS_URL = os.environ.get(
    "QUERYGATE_TEST_STRESS_URL",
    "postgresql+asyncpg://querygate:querygate@localhost:5433/querygate_stress",
)
# The row count the ratios below were measured at. A different scale changes them,
# so the band is asserted rather than assumed (a 2x window keeps the ratios valid
# while tolerating a re-seed).
_MEASURED_ROWS = 25_000
# The frame offset the ~48x ratio was measured at; asserted against the live
# default so lowering the cap re-measures instead of silently weakening the test.
_MEASURED_FRAME_OFFSET = 1000
_SEED_HINT = (
    "querygate_stress.web_events is missing or a different size. It is seeded by "
    "tests/integration/test_large_domain_stress.py's session fixture "
    "(LARGE_STRESS_SCALE=0.1); to seed it directly: "
    "SEED_SCALE=0.1 QUERYGATE_DEMO_DB_URL=<stress-url> poetry run python -m "
    "examples.demo_db.generate_large --postgres --drop"
)
_TABLE = "web_events"
_ORDER = "event_id"
_VALUE = "account_id"


@pytest_asyncio.fixture(autouse=True)
async def require_seeded_stress_data():
    """Autouse so EVERY test in the module skips when the data is absent — the two
    service-level tests build their own registry and would otherwise error inside
    reflection instead of skipping, which is the failure mode this guard exists to
    close."""
    engine = create_async_engine(_STRESS_URL)
    try:
        try:
            async with engine.connect() as conn:
                rows = (await conn.execute(sa.text(f"SELECT count(*) FROM {_TABLE}"))).scalar_one()
        except Exception as exc:  # missing database or table -> skip, don't error
            pytest.skip(f"{_SEED_HINT} ({type(exc).__name__})")
        if not _MEASURED_ROWS // 2 <= rows <= _MEASURED_ROWS * 2:
            pytest.skip(f"{_TABLE} has {rows:,} rows, not ~{_MEASURED_ROWS:,}. {_SEED_HINT}")
    finally:
        await engine.dispose()
    yield


@pytest_asyncio.fixture
async def engine():
    # Function-scoped and disposed per test: an async engine's pool binds to the
    # event loop that created it, and pytest-asyncio gives each test its own —
    # a module-scoped engine passes in isolation and fails in the module (the
    # same loop-affinity trap tests/conftest.py documents for semaphores).
    engine = create_async_engine(_STRESS_URL)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _planner_cost(engine, sql: str) -> float:
    async with engine.connect() as conn:
        return await _planner_cost_on(conn, sql)


async def _planner_cost_on(conn, sql: str) -> float:
    """Cost from an EXISTING connection, so two costs being compared come from one
    statistics snapshot. Planner cost moves with autoanalyze (see the docstring),
    which is precisely why the comparison must not straddle two connections."""
    plan = (await conn.execute(sa.text(f"EXPLAIN (FORMAT JSON) {sql}"))).scalar_one()
    if isinstance(plan, str):
        plan = json.loads(plan)
    return float(plan[0]["Plan"]["Total Cost"])


async def _median_seconds(engine, sql: str, runs: int = 3) -> float:
    samples = []
    async with engine.connect() as conn:
        for _ in range(runs):
            start = time.perf_counter()
            await conn.exec_driver_sql(sql)
            samples.append(time.perf_counter() - start)
    return statistics.median(samples)


def _n_window_sql(n: int) -> str:
    windows = ", ".join(
        f"sum({_VALUE}) OVER (PARTITION BY {_VALUE} % {i + 2} ORDER BY {_ORDER}) AS w{i}"
        for i in range(n)
    )
    return f"SELECT {_ORDER}{', ' + windows if n else ''} FROM {_TABLE}"


def _framed_sql(fn: str, offset: int) -> str:
    return (
        f"SELECT {fn}({_VALUE}) OVER (ORDER BY {_ORDER} "
        f"ROWS BETWEEN {offset} PRECEDING AND CURRENT ROW) FROM {_TABLE}"
    )


@pytest.mark.load
@pytest.mark.asyncio
async def test_the_default_window_cap_keeps_a_query_near_a_plain_scan(engine):
    """Why `max_window_specs` defaults to 5: at the cap a windowed query costs
    ~15x a plain scan of the same table (measured 14.4x in both runs), and the
    marginal cost per window rises beyond it. Asserted on timing ratios with a
    wide margin — planner cost swings with statistics (see the module docstring),
    timing ratios did not."""
    baseline = await _median_seconds(engine, _n_window_sql(0))
    at_cap = await _median_seconds(engine, _n_window_sql(Policy().max_window_specs))
    over_cap = await _median_seconds(engine, _n_window_sql(20))

    assert at_cap < baseline * 40, (
        f"{Policy().max_window_specs} windows now costs {at_cap / baseline:.1f}x a plain "
        "scan (measured 14.4x when the default was chosen)"
    )
    # A window is a real per-window cost, not free: 4x the windows costs at least
    # 3x the time. (Measured 5-8x — it is mildly superlinear, which is the
    # argument for capping rather than merely counting.)
    assert over_cap > at_cap * 3


@pytest.mark.asyncio
async def test_planner_cost_grows_monotonically_with_window_count(engine):
    """The one thing planner cost answers stably: direction. Its absolute
    multiplier moved 47% between runs on identical data, so nothing here depends
    on the magnitude."""
    costs = [await _planner_cost(engine, _n_window_sql(n)) for n in (0, 1, 5, 10)]
    assert costs == sorted(costs) and costs[0] < costs[-1]


@pytest.mark.asyncio
async def test_the_planner_is_blind_to_frame_size(engine):
    """THE finding that justifies `max_window_frame_offset` existing at all.

    A 10,000-row frame executes ~280x slower than a 10-row one (see the table
    above; no test times that frame), yet
    `EXPLAIN` reports the SAME total cost — so item 26's cost-estimation gate
    cannot see it, no matter how the thresholds are tuned. If this assertion ever
    fails because Postgres started pricing frames, the cap could be reconsidered
    in favour of the cost gate; until then it is the only guardrail that sees it.
    """
    async with engine.connect() as conn:  # one snapshot for both plans
        small = await _planner_cost_on(conn, _framed_sql("max", 10))
        huge = await _planner_cost_on(conn, _framed_sql("max", 10_000))
    # A 1% band, not exact equality: the point is that a 1000x wider frame is not
    # *priced*, and a future Postgres that priced it even slightly should fail here.
    assert abs(huge - small) <= small * 0.01, (
        f"planner now prices the frame ({small} vs {huge}) — the cost gate may be "
        "able to see frame cost; revisit max_window_frame_offset's rationale"
    )


@pytest.mark.load
@pytest.mark.asyncio
async def test_a_large_frame_is_genuinely_expensive_for_a_non_invertible_aggregate(engine):
    """What the cap protects: `MAX` cannot use inverse transitions, so the work is
    O(rows x frame). Generous margin — the measured ratio is ~48x, this asserts
    only that it is unmistakably superlinear rather than pinning a timing."""
    assert Policy().max_window_frame_offset == _MEASURED_FRAME_OFFSET, (
        "the default moved; re-measure before trusting the ratio below "
        "(at an offset of ~100 the real ratio is ~5x and this assertion is a coin flip)"
    )
    small = await _median_seconds(engine, _framed_sql("max", 10))
    at_cap = await _median_seconds(engine, _framed_sql("max", _MEASURED_FRAME_OFFSET))
    assert at_cap > small * 5, (
        f"a {_MEASURED_FRAME_OFFSET}-row MAX frame took {at_cap * 1000:.0f} ms vs "
        f"{small * 1000:.0f} ms for 10 rows — if this is no longer expensive, "
        "max_window_frame_offset's default could be relaxed"
    )


@pytest.mark.load
@pytest.mark.asyncio
async def test_an_invertible_aggregate_is_flat_across_frame_sizes(engine):
    """The other half of the finding, so the cap's cost is understood rather than
    assumed uniform: `SUM` subtracts the row leaving the frame, so frame size is
    free. A deployment doing wide `SUM` windows is not paying for this cap."""
    small = await _median_seconds(engine, _framed_sql("sum", 10))
    huge = await _median_seconds(engine, _framed_sql("sum", 10_000))
    assert huge < small * 3, (
        f"SUM over a 10,000-row frame took {huge * 1000:.0f} ms vs "
        f"{small * 1000:.0f} ms over 10 — inverse transitions may no longer apply"
    )


def _use_policy(**overrides) -> None:
    set_registry(
        ConnectionRegistry(
            {
                "stress": ConnectionProfile(
                    id="stress",
                    dialect="postgresql",
                    connection_string=_STRESS_URL,
                    known_tables=[_TABLE],
                )
            }
        )
    )
    set_policy_store(PolicyStore(default=Policy(**overrides), overrides={}))
    reset_engines()


def _window_query(offset: int = 10) -> StructuredQuery:
    return StructuredQuery.model_validate(
        {
            "from": _TABLE,
            "select": [
                f"{_TABLE}.{_ORDER}",
                {
                    "fn": "max",
                    "arg": {"col": f"{_TABLE}.{_VALUE}"},
                    "over": {
                        "order_by": [{"col": f"{_TABLE}.{_ORDER}"}],
                        "frame": {
                            "mode": "rows",
                            "start": {"bound": "preceding", "offset": offset},
                            "end": {"bound": "current_row"},
                        },
                    },
                    "as": "rolling_max",
                },
            ],
        }
    )


@pytest.mark.asyncio
async def test_cost_estimation_runs_on_a_window_query_and_can_reject_it():
    """Item 26's `EXPLAIN` gate must at least *work* on window SQL — a window is
    not a second execution path, but nothing asserted that the estimator survives
    one."""
    _use_policy(max_estimated_rows=10)
    with pytest.raises(CostEstimateExceededError):
        await StructuredQueryService(connection_id="stress").execute(_window_query())


@pytest.mark.asyncio
async def test_the_cost_gate_cannot_distinguish_a_cheap_frame_from_an_expensive_one():
    """The documented limitation, asserted through the REAL gate rather than left
    in a comment: `estimate_postgres_query_cost` — the function whose output
    `max_estimated_rows`/`max_estimated_cost` are compared against — returns the
    identical estimate for a 10-row frame and a 10,000-row frame, though the
    latter is ~280x the work. This is why the frame cap is enforced structurally
    at validation time instead of being folded into the cost estimator."""
    from querygate.connections.engine import session_scope
    from querygate.execution.cost_estimation import estimate_postgres_query_cost

    # The cap itself would refuse the expensive query, so it is raised here to
    # measure what the cost gate sees — which is the whole point.
    _use_policy(max_window_frame_offset=100_000)
    service = StructuredQueryService(connection_id="stress")
    estimates = []
    for offset in (10, 10_000):
        stmt, _limit, _tables, _dialect, _policy = await service._validate_and_compile(
            _window_query(offset=offset)
        )
        async with session_scope("stress") as session:
            estimates.append(
                await estimate_postgres_query_cost(session, stmt, connection_id="stress")
            )

    cheap, expensive = estimates
    assert cheap is not None and expensive is not None, "the estimator must run on window SQL"
    assert cheap.estimated_total_cost == expensive.estimated_total_cost
    assert cheap.estimated_rows == expensive.estimated_rows


@pytest.mark.load
@pytest.mark.asyncio
async def test_an_unrelated_outer_order_by_defeats_the_limit_bound(engine):
    """The correction that matters most for tuning `max_window_frame_offset`.

    A compiled top-level read is LIMIT-clamped, which SOUNDS like it bounds the
    frame rescan to the rows returned — and it does, but only while the outer
    `ORDER BY` is already satisfied by the window's own ordering. Order by anything
    else and Postgres plans `Limit -> Sort -> WindowAgg`; the sort is blocking, so
    the window runs over every row with the full frame despite `LIMIT 50`.

    Measured: 0.7 ms when the orderings agree, **583 ms** when they do not. So the
    full cost is reachable at the top level under default policy — no subquery and
    no raised `max_limit` required — which is why the cap carries the guardrail
    rather than the limit.
    """
    aligned = (
        f"SELECT {_ORDER}, max({_VALUE}) OVER (ORDER BY {_ORDER} "
        f"ROWS BETWEEN {_MEASURED_FRAME_OFFSET} PRECEDING AND CURRENT ROW) "
        f"FROM {_TABLE} ORDER BY {_ORDER} LIMIT 50"
    )
    unrelated = (
        f"SELECT {_ORDER}, max({_VALUE}) OVER (ORDER BY {_ORDER} "
        f"ROWS BETWEEN {_MEASURED_FRAME_OFFSET} PRECEDING AND CURRENT ROW) "
        f"FROM {_TABLE} ORDER BY {_VALUE} LIMIT 50"
    )
    bounded = await _median_seconds(engine, aligned)
    unbounded = await _median_seconds(engine, unrelated)
    assert unbounded > bounded * 20, (
        f"an unrelated outer ORDER BY took {unbounded * 1000:.0f} ms vs "
        f"{bounded * 1000:.0f} ms aligned — if LIMIT now bounds the rescan in both "
        "cases, the guardrail rationale in Policy.max_window_frame_offset can be "
        "relaxed accordingly"
    )
