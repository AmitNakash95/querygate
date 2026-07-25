"""Real-MSSQL execution of item 100's scalar Expression substrate.

Every T-SQL-specific rendering decision in
`MSSQLDialectAdapter.scalar_function` and `_CAST_TYPES` was, until this suite,
backed only by an assertion on generated SQL *text*. That is precisely the
"renders fine, breaks live" trap items 75 and 82 established this tier for —
SQLAlchemy will happily compile T-SQL-invalid SQL against the mssql dialect
(the `within_group()` case item 82 found), so text assertions cannot tell a
correct rendering from one that only *looks* correct. Each test below therefore
asserts a computed VALUE, not a string.

The four decisions under test:

1. `ceil` → `CEILING` — T-SQL has no `CEIL`.
2. `length` → `LEN` — T-SQL has no `LENGTH`.
3. `round` always gets a length argument — T-SQL's `ROUND` requires it, so a
   one-argument `round` must render `ROUND(x, 0)` or fail.
4. `CAST(x AS text)` maps to `Unicode` → `NVARCHAR(max)`, not `Text`. The
   danger there is silent DATA CORRUPTION rather than an error: a connected
   MSSQL dialect renders `Text` as `VARCHAR(max)` (it sets
   `deprecate_large_types` once it knows the server version, so it does *not*
   emit the deprecated `TEXT` an unconnected dialect shows), and `VARCHAR` is
   codepage-limited — under the default collation 'δ-λ' comes back as 'd-?'.
   That is exactly why this suite asserts round-tripped VALUES: a rendering
   assertion would have "passed" on the broken spelling.

Plus the guarded division, whose whole justification is MSSQL-specific: this
adapter sets `XACT_ABORT ON`, under which an unguarded divide-by-zero aborts the
entire transaction rather than failing one row.

Needs a real MSSQL server seeded by `tests/integration/setup_mssql_test_db.py`
— see `test_mssql_live.py`'s module docstring. Run via `pytest -m mssql_live`.
"""

from __future__ import annotations

import os
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from querygate.api.app import create_app
from querygate.connections.engine import reset_engines
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.core.config import AppConfig
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy

pytestmark = [pytest.mark.integration, pytest.mark.real_db, pytest.mark.mssql_live]

_BASE_URL = "http://localhost"

_HOST = os.environ.get("QUERYGATE_TEST_MSSQL_HOST", "localhost")
_PORT = os.environ.get("QUERYGATE_TEST_MSSQL_PORT", "14330")
_SA_PASSWORD = os.environ.get("QUERYGATE_TEST_MSSQL_SA_PASSWORD", "QueryGate_Test_Pw1!")
_ODBC_DRIVER = os.environ.get("QUERYGATE_TEST_MSSQL_ODBC_DRIVER", "ODBC Driver 18 for SQL Server")

_DEMO_URL = f"mssql+aioodbc://sa:{_SA_PASSWORD}@{_HOST}:{_PORT}/querygate_demo"


@pytest_asyncio.fixture
async def mssql_app():
    # Same import-time binding caveat as test_mssql_live.py's fixture: mutate
    # the shared config object rather than rebinding the name.
    from querygate.core.config import config as shared_config

    shared_config.odbc_driver = _ODBC_DRIVER.replace(" ", "+")
    shared_config.db_trust_server_certificate = True

    set_registry(
        ConnectionRegistry(
            {
                "mssql_demo": ConnectionProfile(
                    id="mssql_demo",
                    dialect="mssql",
                    connection_string=_DEMO_URL,
                    known_tables=["customers", "orders", "order_items"],
                )
            }
        )
    )
    set_policy_store(PolicyStore(default=Policy(), overrides={}))
    reset_engines()

    app = create_app(
        AppConfig(environment="localhost", mcp_enabled=False, audit_sink_backend="none")
    )
    yield app

    from querygate.connections.engine import ENGINES

    for engine in list(ENGINES.values()):
        await engine.dispose()
    reset_engines()


async def _rows(app, body):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post("/api/v1/mssql_demo/query", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["rows"]


@pytest.mark.asyncio
async def test_arithmetic_and_conditional_aggregation_run_on_mssql(mssql_app):
    """Canonical regression-bar rows 1 and 2 on the second supported dialect."""
    rows = await _rows(
        mssql_app,
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
                    "as": "multi_unit_total",
                },
            ],
            "group_by": ["order_items.product_name"],
            "limit": 50,
        },
    )
    assert rows
    assert all(row["revenue"] is not None for row in rows)
    assert any(float(row["multi_unit_total"]) > 0 for row in rows)


@pytest.mark.asyncio
async def test_guarded_division_yields_null_under_xact_abort(mssql_app):
    """The MSSQL half of the guarded-division decision. `XACT_ABORT ON` (set by
    `MSSQLSessionDialectAdapter`) makes an unguarded divide-by-zero abort the
    WHOLE transaction, so without the NULLIF this request fails outright rather
    than returning a NULL cell."""
    rows = await _rows(
        mssql_app,
        {
            "from": "order_items",
            "select": [
                {
                    "expr": {
                        "op": "/",
                        "left": {"col": "order_items.unit_price"},
                        "right": {
                            "op": "-",
                            "left": {"col": "order_items.quantity"},
                            "right": {"col": "order_items.quantity"},
                        },
                    },
                    "as": "ratio",
                }
            ],
            "limit": 5,
        },
    )
    assert rows, "expected rows"
    assert all(row["ratio"] is None for row in rows)


@pytest.mark.asyncio
async def test_ceiling_length_and_round_render_in_t_sql_and_return_right_values(mssql_app):
    """`CEIL`/`LENGTH` do not exist in T-SQL and `ROUND` requires its length
    argument — all three would be a runtime syntax/function error if the adapter
    emitted the Postgres spelling. Values are asserted, not SQL text."""
    rows = await _rows(
        mssql_app,
        {
            "from": "order_items",
            "select": [
                "order_items.product_name",
                "order_items.unit_price",
                {"expr": {"fn": "ceil", "args": [{"col": "order_items.unit_price"}]}, "as": "c"},
                {
                    "expr": {"fn": "length", "args": [{"col": "order_items.product_name"}]},
                    "as": "n",
                },
                {
                    "expr": {"fn": "round", "args": [{"col": "order_items.unit_price"}]},
                    "as": "r1",
                },
                {
                    "expr": {
                        "fn": "round",
                        "args": [{"col": "order_items.unit_price"}, {"literal": 1}],
                    },
                    "as": "r2",
                },
            ],
            "limit": 10,
        },
    )
    assert rows
    for row in rows:
        price = Decimal(str(row["unit_price"]))
        assert Decimal(str(row["c"])) == price.to_integral_value(rounding=ROUND_CEILING)
        # T-SQL's LEN ignores trailing spaces; the seed data has none, so LEN
        # and Python's len agree here. (Documented on the adapter method.)
        assert row["n"] == len(row["product_name"])
        # ROUND_HALF_UP, not Python's round(): T-SQL rounds halves AWAY FROM
        # ZERO while Python uses banker's rounding, so round(310.25, 1) is
        # 310.3 on the server and 310.2 in Python. Postgres's numeric round
        # agrees with T-SQL here — which is a second reason the Postgres
        # adapter casts to NUMERIC rather than letting a float through.
        assert Decimal(str(row["r1"])) == price.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        assert Decimal(str(row["r2"])) == price.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


@pytest.mark.asyncio
async def test_substring_uses_the_t_sql_comma_form(mssql_app):
    """Postgres takes the SQL-standard `SUBSTRING(x FROM a FOR b)`; T-SQL only
    accepts the comma form. The AST requires exactly 3 arguments precisely
    because T-SQL has no 2-argument form."""
    rows = await _rows(
        mssql_app,
        {
            "from": "order_items",
            "select": [
                "order_items.product_name",
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
            ],
            "limit": 5,
        },
    )
    assert rows
    for row in rows:
        assert row["prefix"] == row["product_name"][:3]


@pytest.mark.asyncio
async def test_cast_to_text_preserves_non_ascii_on_mssql(mssql_app):
    """`to: "text"` maps to `Unicode` (→ `NVARCHAR(max)`), not SQLAlchemy's
    `Text` (→ `VARCHAR(max)` on a connected 2012+ server).

    The distinction is invisible in SQL text and invisible on ASCII data — it
    shows up only as silently corrupted characters, which is the worst failure
    mode available: no error, wrong data. Under the server's default
    SQL_Latin1_General_CP1_CI_AS collation a VARCHAR cast turns 'δ-λ' into
    'd-?'. Asserting the round-trip is what makes this test able to fail;
    an earlier version asserted the rendered type name and did NOT catch the
    wrong spelling at all.
    """
    rows = await _rows(
        mssql_app,
        {
            "from": "order_items",
            "select": [
                {"expr": {"cast": {"literal": "δ-λ"}, "to": "text"}, "as": "unicode_text"},
                {"expr": {"cast": {"col": "order_items.id"}, "to": "text"}, "as": "id_text"},
            ],
            "limit": 1,
        },
    )
    assert len(rows) == 1
    assert rows[0]["unicode_text"] == "δ-λ", "non-ASCII was mangled — VARCHAR, not NVARCHAR"
    assert rows[0]["id_text"] == str(rows[0]["id_text"])


@pytest.mark.asyncio
async def test_nested_functions_and_expression_predicate_run_on_mssql(mssql_app):
    rows = await _rows(
        mssql_app,
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
            ],
            "where": {
                "expr": {
                    "op": "*",
                    "left": {"col": "order_items.quantity"},
                    "right": {"col": "order_items.unit_price"},
                },
                "op": "gt",
                "value": 1,
            },
            "limit": 10,
        },
    )
    assert rows
    for row in rows:
        assert row["shout"] == row["product_name"].strip().upper()
