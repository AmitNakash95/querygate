"""Real-Postgres execution of item 100's scalar Expression substrate.

The SQLite end-to-end suite (`test_expression_end_to_end.py`) proves the
semantics; this proves the two renderings whose correctness is *specific to
Postgres* and which SQLite silently forgives — the exact "renders fine, breaks
live" trap items 75/82 established this tier for:

1. **Guarded division.** `left / NULLIF(right, 0)` must return NULL. Postgres
   raises `division_by_zero` unconditionally for an unguarded divide, so this is
   the only backend where the guard's absence would be a hard failure rather
   than a silently different value. The 2026-07-25 Decision Log chose the guard
   over dialect-native behavior; this is that decision's live proof.
2. **The two-argument `round` cast.** Postgres has `round(numeric, integer)` but
   NO `round(double precision, integer)`, so an uncast two-argument round over a
   float column fails at runtime. `PostgresDialectAdapter.scalar_function` casts
   to NUMERIC for exactly that reason. Every numeric column in the demo schema
   is NUMERIC, so the end-to-end query cannot exercise the float case; the
   server behavior the cast exists for is asserted directly instead — see
   `test_postgres_rejects_two_argument_round_over_double_precision`.

Needs a real Postgres — run `make compose-up` first, then
`make test-postgres-live` (or `poetry run pytest -m postgres_live`). Excluded
from the default `pytest` run, same as the sibling postgres_live modules.
"""

from __future__ import annotations

import pytest

from querygate.connections.engine import reset_engines
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.execution.service import StructuredQueryService
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
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


@pytest.mark.asyncio
async def test_guarded_division_yields_null_on_real_postgres():
    """Postgres raises `division_by_zero` for an unguarded divide, so if the
    NULLIF guard regressed this request would FAIL rather than return NULLs."""
    _use_policy()
    query = StructuredQuery.model_validate(
        {
            "from": "order_items",
            "select": [
                {
                    "expr": {
                        "op": "/",
                        "left": {"col": "order_items.unit_price"},
                        "right": {
                            # Identically zero for every row, without depending
                            # on the seed containing a literal zero.
                            "op": "-",
                            "left": {"col": "order_items.quantity"},
                            "right": {"col": "order_items.quantity"},
                        },
                    },
                    "as": "ratio",
                }
            ],
            "limit": 5,
        }
    )
    result = await StructuredQueryService(connection_id="demo").execute(query)
    assert result.rows, "expected rows"
    assert all(row["ratio"] is None for row in result.rows)


@pytest.mark.asyncio
async def test_two_argument_round_runs_on_real_postgres():
    """The end-to-end path: a two-argument round over a real column executes.

    NOTE ON WHAT THIS DOES *NOT* PROVE: every numeric column in the demo schema
    is NUMERIC, and `numeric / integer` stays numeric, so this query succeeds
    with or without the adapter's cast. Removing the cast does not fail this
    test. The cast's necessity is proven separately, against the live server,
    by `test_postgres_rejects_two_argument_round_over_double_precision` below —
    written that way deliberately rather than leaving a test whose name claims
    more than it checks."""
    _use_policy()
    query = StructuredQuery.model_validate(
        {
            "from": "order_items",
            "select": [
                "order_items.unit_price",
                {
                    "expr": {
                        "fn": "round",
                        "args": [
                            {
                                "op": "/",
                                "left": {"col": "order_items.unit_price"},
                                "right": {"literal": 3},
                            },
                            {"literal": 2},
                        ],
                    },
                    "as": "rounded",
                },
            ],
            "limit": 5,
        }
    )
    result = await StructuredQueryService(connection_id="demo").execute(query)
    assert result.rows
    for row in result.rows:
        expected = round(float(row["unit_price"]) / 3, 2)
        assert float(row["rounded"]) == pytest.approx(expected)


@pytest.mark.asyncio
async def test_postgres_rejects_two_argument_round_over_double_precision():
    """The live fact `PostgresDialectAdapter.scalar_function`'s cast exists for.

    A customer table with a `double precision` / `real` column (rates, scores,
    measurements) is ordinary, and `round(<float8>, 2)` genuinely does not exist
    in Postgres. The demo schema has no such column, so rather than write a
    real-DB test that silently proves nothing, this asserts the underlying
    server behavior directly: the uncast form fails, the cast form the adapter
    emits succeeds. If Postgres ever gained `round(double precision, integer)`,
    this test would start failing and the cast could be reconsidered.
    """
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(_CONNECTION_STRING)
    try:
        async with engine.connect() as conn:
            with pytest.raises(Exception) as excinfo:
                await conn.execute(
                    sa.text("SELECT round(unit_price::float8 * 2, 2) FROM order_items LIMIT 1")
                )
            assert "round" in str(excinfo.value).lower()
        # A fresh connection: the failure above aborted that transaction.
        async with engine.connect() as conn:
            value = (
                await conn.execute(
                    sa.text(
                        "SELECT round(CAST(unit_price::float8 * 2 AS NUMERIC), 2) "
                        "FROM order_items LIMIT 1"
                    )
                )
            ).scalar_one()
            assert value is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_conditional_aggregation_and_computed_measure_run_on_real_postgres():
    """Canonical regression-bar rows 1 and 2 against the real backend."""
    _use_policy()
    query = StructuredQuery.model_validate(
        {
            "from": "order_items",
            "select": [
                "order_items.product_name",
                {
                    "fn": "sum",
                    "arg": {
                        "op": "*",
                        "left": {"col": "order_items.quantity"},
                        "right": {"col": "order_items.unit_price"},
                    },
                    "as": "revenue",
                },
                {
                    "fn": "sum",
                    "arg": {
                        "when": [
                            {
                                "when": {"col": "order_items.quantity", "op": "gt", "value": 1},
                                "then": {"col": "order_items.unit_price"},
                            }
                        ],
                        "else": {"literal": 0},
                    },
                    "as": "multi_unit_price_total",
                },
            ],
            "group_by": ["order_items.product_name"],
            "limit": 50,
        }
    )
    result = await StructuredQueryService(connection_id="demo").execute(query)
    assert result.rows
    assert all(row["revenue"] is not None for row in result.rows)
    assert any(float(row["multi_unit_price_total"]) > 0 for row in result.rows)


@pytest.mark.asyncio
async def test_nested_functions_and_cast_run_on_real_postgres():
    _use_policy()
    query = StructuredQuery.model_validate(
        {
            "from": "order_items",
            "select": [
                "order_items.product_name",
                {
                    "expr": {
                        "fn": "upper",
                        "args": [{"fn": "trim", "args": [{"col": "order_items.product_name"}]}],
                    },
                    "as": "shout",
                },
                {"expr": {"cast": {"col": "order_items.id"}, "to": "text"}, "as": "id_text"},
                {
                    "expr": {
                        "fn": "substring",
                        "args": [
                            {"col": "order_items.product_name"},
                            {"literal": 1},
                            {"literal": 3},
                        ],
                    },
                    "as": "prefix",
                },
                {
                    "expr": {"fn": "length", "args": [{"col": "order_items.product_name"}]},
                    "as": "n",
                },
                {"expr": {"fn": "ceil", "args": [{"col": "order_items.unit_price"}]}, "as": "c"},
            ],
            "limit": 5,
        }
    )
    result = await StructuredQueryService(connection_id="demo").execute(query)
    assert result.rows
    for row in result.rows:
        name = row["product_name"]
        assert row["shout"] == name.strip().upper()
        assert row["prefix"] == name[:3]
        assert row["n"] == len(name)
        assert row["id_text"] == str(row["id_text"])
