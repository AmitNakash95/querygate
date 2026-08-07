"""Unit tests for the on-demand live-schema check of query templates
(admin.service._check_one_template_schema) — the existence check the offline
dry-run deliberately skips.

The per-template classifier is exercised directly (no config-plane file
machinery) with the schema reflection seam patched, per the conftest gotcha:
patch the module-level `schema_validation._load_table` rather than mocking
SQLAlchemy internals. Every test relies on the autouse "demo" registry.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from querygate.admin import service as governance
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.templates.models import QueryTemplate
from querygate.validation import schema_validation

pytestmark = pytest.mark.unit


def _orders_table() -> sa.Table:
    md = sa.MetaData()
    return sa.Table(
        "orders",
        md,
        sa.Column("id", sa.Integer),
        sa.Column("total_amount", sa.Numeric),
    )


def _template(query: dict, *, connection: str = "demo") -> QueryTemplate:
    return QueryTemplate(id="t", connection=connection, parameters=[], query=query)


def _patch_reflection(monkeypatch, *, raises: Exception | None = None) -> None:
    async def fake_load_table(connection_id, table_name, table_connection):
        if raises is not None:
            raise raises
        if table_name.lower() == "orders":
            return _orders_table()
        raise sa.exc.NoSuchTableError(table_name)

    monkeypatch.setattr(schema_validation, "_load_table", fake_load_table)


@pytest.mark.asyncio
async def test_ok_when_every_table_and_column_exists(monkeypatch):
    _patch_reflection(monkeypatch)
    result = await governance._check_one_template_schema(
        _template({"from": "orders", "select": ["orders.id", "orders.total_amount"], "limit": 5})
    )
    assert result.status == "ok"
    assert result.messages == []


@pytest.mark.asyncio
async def test_missing_column_is_reported_as_issues(monkeypatch):
    _patch_reflection(monkeypatch)
    result = await governance._check_one_template_schema(
        _template({"from": "orders", "select": ["orders.ghost_col"], "limit": 5})
    )
    assert result.status == "issues"
    assert any("ghost_col" in m for m in result.messages)


@pytest.mark.asyncio
async def test_missing_table_is_reported_as_issues(monkeypatch):
    _patch_reflection(monkeypatch)
    result = await governance._check_one_template_schema(
        _template({"from": "ghosts", "select": ["ghosts.id"], "limit": 5})
    )
    assert result.status == "issues"
    assert any("does not exist" in m for m in result.messages)


@pytest.mark.asyncio
async def test_unreachable_database_is_best_effort_and_never_leaks_the_driver_error(monkeypatch):
    # A real connection failure embeds host/credentials/SQL in the driver error.
    # The result must report only a generic status/message — never that text.
    driver_error = sa.exc.OperationalError(
        "SELECT * FROM orders  -- statement text",
        {},
        Exception("connection refused: password=SUPERSECRET host=db.internal port=5432"),
    )
    _patch_reflection(monkeypatch, raises=driver_error)
    result = await governance._check_one_template_schema(
        _template({"from": "orders", "select": ["orders.id"], "limit": 5})
    )
    assert result.status == "unreachable"
    assert result.messages == [
        "schema not checked — the connection's database could not be reached"
    ]
    blob = " ".join(result.messages)
    for leak in ("SUPERSECRET", "password=", "db.internal", "5432", "statement text", "SELECT"):
        assert leak not in blob


@pytest.mark.asyncio
async def test_unknown_connection_is_connection_unavailable(monkeypatch):
    _patch_reflection(monkeypatch)
    result = await governance._check_one_template_schema(
        _template({"from": "orders", "select": ["orders.id"], "limit": 5}, connection="not-a-conn")
    )
    assert result.status == "connection_unavailable"


@pytest.mark.asyncio
async def test_structurally_invalid_skeleton_is_flagged_separately(monkeypatch):
    _patch_reflection(monkeypatch)
    # empty select is not a valid StructuredQuery — dummy binding fails before
    # any reflection happens.
    result = await governance._check_one_template_schema(
        _template({"from": "orders", "select": [], "limit": 5})
    )
    assert result.status == "structural_error"


@pytest.mark.asyncio
async def test_not_connectable_secondary_join_is_reported_as_issues_not_raised(monkeypatch):
    """Post-ship audit finding on TODO.md item 163's own fix
    (`security-invariant-reviewer`, 2026-08-07): `validate_schema`'s
    `resolve_query_table_connections` now raises `ConfigValidationError` for a
    cross-connection join whose SECONDARY connection is a not-yet-connectable
    dialect (Snowflake/BigQuery). `_check_one_template_schema` had no handler
    for that exception type — it escaped past this function (and past
    `check_template_schema`, which has no wrapping try either) instead of
    being reported as a per-template `issues` result, contradicting this
    module's own "a connection that can't be reached never blocks staging"
    contract (`check_template_schema`'s docstring). `_patch_reflection`'s
    default fake still raises `NoSuchTableError` for any table other than
    "orders" (see its definition above) — if this guard ever stopped firing
    before reflection, the resulting message would be about the "customers"
    table not existing, not the dialect, so the "snowflake" assertion below
    only passes for the right reason.
    """
    demo = ConnectionProfile(
        id="demo",
        dialect="postgresql",
        connection_string="postgresql+asyncpg://user:pass@localhost/demo",
        join_group="shared",
    )
    other = ConnectionProfile(
        id="other",
        dialect="snowflake",
        connection_string="snowflake://user:pass@account/db",
        join_group="shared",
    )
    set_registry(ConnectionRegistry({"demo": demo, "other": other}))
    _patch_reflection(monkeypatch)
    result = await governance._check_one_template_schema(
        _template(
            {
                "from": "orders",
                "select": ["orders.id"],
                "joins": [
                    {
                        "table": "customers",
                        "on": ["orders.customer_id", "customers.id"],
                        "connection": "other",
                    }
                ],
                "limit": 5,
            }
        )
    )
    assert result.status == "issues"
    assert any("snowflake" in m for m in result.messages)
