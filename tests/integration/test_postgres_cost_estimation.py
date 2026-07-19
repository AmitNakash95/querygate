"""Proves TODO.md item 26 phase 1 (pre-execution cost estimation) actually
works against a real Postgres — not just against a mocked EXPLAIN response.

Needs a real Postgres — run `make compose-up` first, then
`make test-postgres-live` (or `poetry run pytest -m postgres_live`).
Excluded from the default `pytest` run (see pyproject.toml's
`addopts`/`markers`), same as tests/integration/test_postgres_timeout.py.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from querygate.connections.engine import reset_engines, session_scope
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.core.exceptions import CostEstimateExceededError
from querygate.execution.cost_estimation import estimate_postgres_query_cost
from querygate.execution.service import StructuredQueryService
from querygate.metrics import REGISTRY
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import CostEstimationMode, Policy
from querygate.query_ast.models import StructuredQuery

pytestmark = [pytest.mark.integration, pytest.mark.real_db, pytest.mark.postgres_live]

_CONNECTION_STRING = "postgresql+asyncpg://querygate:querygate@localhost:5433/querygate_demo"


def _use_policy(**policy_overrides) -> None:
    set_registry(
        ConnectionRegistry(
            {
                "demo": ConnectionProfile(
                    id="demo",
                    dialect="postgresql",
                    connection_string=_CONNECTION_STRING,
                    known_tables=["customers", "orders", "order_items"],
                )
            }
        )
    )
    set_policy_store(PolicyStore(default=Policy(**policy_overrides), overrides={}))
    reset_engines()


def _full_scan_query() -> StructuredQuery:
    return StructuredQuery(
        from_table="orders",
        select=["orders.id", "orders.status", "orders.total_amount"],
        limit=100,
    )


@pytest.mark.asyncio
async def test_estimator_returns_real_plan_numbers_from_postgres():
    """Lower-level check of the parser against a real EXPLAIN (FORMAT JSON)
    response — proves the `isinstance(raw, str)` branch and the JSON shape
    assumptions in execution/cost_estimation.py hold against the real
    asyncpg driver, not just the mocked JSON used in unit tests.
    """
    _use_policy()
    table = sa.Table(
        "orders",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("status", sa.String),
        sa.Column("total_amount", sa.Numeric),
    )
    stmt = sa.select(table.c.id, table.c.status, table.c.total_amount)

    async with session_scope("demo") as session:
        estimate = await estimate_postgres_query_cost(session, stmt, connection_id="demo")

    assert estimate is not None
    assert estimate.estimated_rows is not None and estimate.estimated_rows >= 0
    assert estimate.estimated_total_cost is not None and estimate.estimated_total_cost >= 0


@pytest.mark.asyncio
async def test_query_rejected_when_estimated_rows_exceed_threshold():
    _use_policy(max_estimated_rows=0)
    service = StructuredQueryService(connection_id="demo")
    with pytest.raises(CostEstimateExceededError, match="estimated rows"):
        await service.execute(_full_scan_query())


@pytest.mark.asyncio
async def test_query_rejected_when_estimated_cost_exceeds_threshold():
    _use_policy(max_estimated_cost=0.0)
    service = StructuredQueryService(connection_id="demo")
    with pytest.raises(CostEstimateExceededError, match="planner cost"):
        await service.execute(_full_scan_query())


@pytest.mark.asyncio
async def test_query_allowed_when_within_cost_estimate_threshold():
    _use_policy(max_estimated_rows=1_000_000, max_estimated_cost=1_000_000.0)
    service = StructuredQueryService(connection_id="demo")
    result = await service.execute(_full_scan_query())
    assert result.row_count >= 1


@pytest.mark.asyncio
async def test_disabled_by_default_never_blocks_a_full_scan():
    _use_policy()  # max_estimated_rows/max_estimated_cost both unset
    service = StructuredQueryService(connection_id="demo")
    result = await service.execute(_full_scan_query())
    assert result.row_count >= 1


@pytest.mark.asyncio
async def test_observe_mode_runs_the_query_and_records_would_reject():
    """CostEstimationMode.OBSERVE against a real over-threshold plan: the
    query must still succeed, and querygate_cost_estimation_would_reject_total
    must increment — proves the calibration path end to end, not just against
    a mocked estimate (see the mocked equivalent in tests/unit/test_service.py).
    """
    _use_policy(max_estimated_rows=0, cost_estimation_mode=CostEstimationMode.OBSERVE)
    before = (
        REGISTRY.get_sample_value(
            "querygate_cost_estimation_would_reject_total", {"connection": "demo"}
        )
        or 0.0
    )

    service = StructuredQueryService(connection_id="demo")
    result = await service.execute(_full_scan_query())  # must not raise

    assert result.row_count >= 1
    after = (
        REGISTRY.get_sample_value(
            "querygate_cost_estimation_would_reject_total", {"connection": "demo"}
        )
        or 0.0
    )
    assert after == before + 1
