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
async def test_unreachable_database_is_best_effort_not_a_failure(monkeypatch):
    _patch_reflection(
        monkeypatch, raises=sa.exc.OperationalError("SELECT 1", {}, Exception("refused"))
    )
    result = await governance._check_one_template_schema(
        _template({"from": "orders", "select": ["orders.id"], "limit": 5})
    )
    assert result.status == "unreachable"
    assert "could not be reached" in result.messages[0]


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
