"""Proves TODO.md item 81 (array_agg) actually works against a real Postgres
— not just a compile-level rendering assertion. array_agg has no MSSQL/
SQLite equivalent (see compiler/dialect_adapters.py), so unlike item 80's
string_agg it can't get end-to-end coverage via the internal SQLite test/
example path (tests/integration/test_sqlite_end_to_end.py) — this is the
only place array_agg gets genuine execution coverage.

Needs a real Postgres — run `make compose-up` first, then
`make test-postgres-live` (or `poetry run pytest -m postgres_live`).
Excluded from the default `pytest` run (see pyproject.toml's
`addopts`/`markers`), same as tests/integration/test_postgres_cost_estimation.py.
"""

from __future__ import annotations

import pytest

from querygate.connections.engine import reset_engines
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.execution.service import StructuredQueryService
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.query_ast.models import ArrayAggSelectItem, StructuredQuery

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
async def test_array_agg_end_to_end():
    """Group demo customers by country, array_agg(name), and confirm GB's
    array is the real set of GB customer names — seeded in
    examples/demo_db/init_postgres.sql, never modify their values."""
    _use_policy()
    service = StructuredQueryService(connection_id="demo")
    query = StructuredQuery(
        from_table="customers",
        select=[
            "customers.country",
            ArrayAggSelectItem(col="customers.name", alias="names"),
        ],
        group_by=["customers.country"],
        limit=50,
    )
    result = await service.execute(query)

    by_country = {row["country"]: row["names"] for row in result.rows}
    assert "GB" in by_country
    assert set(by_country["GB"]) == {"Ada Lovelace", "Alan Turing", "Charles Babbage"}
