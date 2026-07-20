"""Proves TODO.md item 82 (percentile_cont) actually works against a real
Postgres — not just a compile-level rendering assertion. percentile_cont
has no MSSQL/SQLite equivalent usable as a plain GROUP BY aggregate (see
compiler/dialect_adapters.py), so like item 81's array_agg it can't get
end-to-end coverage via the internal SQLite test/example path — this is
the only place percentile_cont gets genuine execution coverage.

Needs a real Postgres — run `make compose-up` first, then
`make test-postgres-live` (or `poetry run pytest -m postgres_live`).
Excluded from the default `pytest` run (see pyproject.toml's
`addopts`/`markers`), same as tests/integration/test_postgres_cost_estimation.py.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from querygate.connections.engine import reset_engines
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.execution.service import StructuredQueryService
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.query_ast.models import PercentileContSelectItem, StructuredQuery

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


@pytest.mark.asyncio
async def test_percentile_cont_end_to_end():
    """Group demo orders by customer, percentile_cont(0.5) on total_amount,
    and confirm customer 2's median matches a hand-computed value. Customer
    2 has exactly 3 orders seeded in examples/demo_db/init_postgres.sql
    (75.00, 249.00, 310.25) — never modify their values. An odd count means
    the continuous-interpolation median lands exactly on the middle sorted
    value (249.00), with no floating-point interpolation between two rows
    to account for."""
    _use_policy()
    service = StructuredQueryService(connection_id="demo")
    query = StructuredQuery(
        from_table="orders",
        select=[
            "orders.customer_id",
            PercentileContSelectItem(col="orders.total_amount", fraction=0.5, alias="median"),
        ],
        group_by=["orders.customer_id"],
        limit=50,
    )
    result = await service.execute(query)

    by_customer = {row["customer_id"]: row["median"] for row in result.rows}
    assert by_customer[2] == Decimal("249.00")
