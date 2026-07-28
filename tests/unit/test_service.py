"""Unit tests for StructuredQueryService — the seam every REST route and MCP
tool goes through. Real schema validation is bypassed via a patched
validate_schema; policy validation runs for real against a permissive Policy.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa

from querygate.catalog.loader import CatalogStore, set_catalog_store
from querygate.core.auth import Principal
from sqlalchemy.exc import ProgrammingError

from querygate.core.exceptions import (
    CapacityTimeoutError,
    CostEstimateExceededError,
    QueryValidationError,
    QueueFullError,
    public_error_message,
)
from querygate.execution import concurrency as cc
from querygate.execution import service as svc
from querygate.execution.admission import QueueMode
from querygate.execution.cost_estimation import QueryCostEstimate
from querygate.execution.service import (
    ExplainResult,
    StructuredQueryResult,
    StructuredQueryService,
    TableDescription,
    _cap_response_bytes,
)
from querygate.metrics import REGISTRY
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import CostEstimationMode, MandatoryRowFilter, Policy
from querygate.query_ast.models import Predicate, StructuredQuery


def _sample(name: str, labels: dict) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _company_table() -> sa.Table:
    return sa.Table(
        "customers",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(50)),
    )


@pytest.mark.asyncio
async def test_execute_returns_result():
    table = _company_table()
    query = StructuredQuery(
        from_table="customers", select=["customers.id", "customers.name"], limit=10
    )

    row = {"id": 1, "name": "Ada "}
    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = [row]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
    ):
        service = StructuredQueryService(connection_id="demo")
        result = await service.execute(query)

    assert isinstance(result, StructuredQueryResult)
    assert result.row_count == 1
    assert result.rows[0]["name"] == "Ada"  # whitespace-stripped
    assert result.limit == 10


@pytest.mark.asyncio
async def test_execute_increments_success_metrics():
    table = _company_table()
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)

    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = [{"id": 1}]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    before = _sample("querygate_queries_total", {"connection": "demo", "status": "success"})
    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
    ):
        service = StructuredQueryService(connection_id="demo")
        await service.execute(query)
    after = _sample("querygate_queries_total", {"connection": "demo", "status": "success"})

    assert after == before + 1


@pytest.mark.asyncio
async def test_execute_increments_rejection_metrics_with_reason():
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)

    before_total = _sample("querygate_queries_total", {"connection": "demo", "status": "rejected"})
    before_reason = _sample(
        "querygate_queries_rejected_total", {"connection": "demo", "reason": "schema"}
    )
    with patch.object(svc, "validate_schema", AsyncMock(side_effect=ValueError("bad ref"))):
        service = StructuredQueryService(connection_id="demo")
        with pytest.raises(ValueError):
            await service.execute(query)
    after_total = _sample("querygate_queries_total", {"connection": "demo", "status": "rejected"})
    after_reason = _sample(
        "querygate_queries_rejected_total", {"connection": "demo", "reason": "schema"}
    )

    assert after_total == before_total + 1
    assert after_reason == before_reason + 1


@pytest.mark.asyncio
async def test_validate_schema_receives_principal_context():
    table = _company_table()
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)
    principal = Principal(subject="agent-a")
    validate_schema = AsyncMock(return_value={"customers": table})
    mock_engine = MagicMock()
    mock_engine.dialect.name = "sqlite"

    with (
        patch.object(svc, "validate_schema", validate_schema),
        patch.object(svc, "get_engine", return_value=mock_engine),
    ):
        service = StructuredQueryService(connection_id="demo", principal=principal)
        await service.explain(query)

    # scope_tables (item 97) is a fresh per-call dict threaded to the compiler for
    # IN (subquery) rendering; the principal context still flows through unchanged.
    validate_schema.assert_awaited_once_with(
        query, connection_id="demo", principal=principal, scope_tables=ANY
    )


@pytest.mark.asyncio
async def test_execute_redacts_where_literals_in_audit_log_by_default():
    """P0: a WHERE-clause literal (e.g. an email) must never end up verbatim
    in the audit log — SQL must be parameterized and params redacted unless
    the policy explicitly opts into log_query_literals.
    """
    table = _company_table()
    query = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        where=Predicate(col="customers.name", op="eq", value="jane@example.com"),
        limit=10,
    )

    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = []
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
        patch.object(svc, "audit_query") as mock_audit,
    ):
        service = StructuredQueryService(connection_id="demo")
        await service.execute(query)

    logged_sql = mock_audit.call_args.kwargs["sql"]
    logged_params = mock_audit.call_args.kwargs["params"]
    assert "jane@example.com" not in logged_sql
    assert "jane@example.com" not in (logged_params or "")
    assert "<redacted>" in logged_params


@pytest.mark.asyncio
async def test_execute_logs_literals_when_policy_opts_in():
    table = _company_table()
    query = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        where=Predicate(col="customers.name", op="eq", value="jane@example.com"),
        limit=10,
    )

    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = []
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    set_policy_store(PolicyStore(default=Policy(log_query_literals=True), overrides={}))
    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
        patch.object(svc, "audit_query") as mock_audit,
    ):
        service = StructuredQueryService(connection_id="demo")
        await service.execute(query)

    assert "jane@example.com" in mock_audit.call_args.kwargs["sql"]


@pytest.mark.asyncio
async def test_explain_redacts_where_literals_by_default():
    table = _company_table()
    query = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        where=Predicate(col="customers.name", op="eq", value="jane@example.com"),
        limit=5,
    )
    with patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})):
        service = StructuredQueryService(connection_id="demo")
        result = await service.explain(query)

    assert "jane@example.com" not in result.sql
    assert "jane@example.com" not in (result.params or "")
    assert "<redacted>" in result.params


@pytest.mark.asyncio
async def test_execute_logs_principal_subject_and_scopes():
    table = _company_table()
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)

    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = []
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    principal = Principal(subject="agent-a", scopes=frozenset({"read:orders"}))
    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
        patch.object(svc, "audit_query") as mock_audit,
    ):
        service = StructuredQueryService(connection_id="demo", principal=principal)
        await service.execute(query)

    assert mock_audit.call_args.kwargs["principal"] == "agent-a"
    assert mock_audit.call_args.kwargs["principal_scopes"] == ["read:orders"]


@pytest.mark.asyncio
async def test_execute_logs_principal_scopes_on_rejection():
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)
    principal = Principal(subject="agent-a", scopes=frozenset({"read:orders"}))
    with (
        patch.object(svc, "validate_schema", AsyncMock(side_effect=ValueError("schema boom"))),
        patch.object(svc, "audit_query") as mock_audit,
    ):
        service = StructuredQueryService(connection_id="demo", principal=principal)
        with pytest.raises(ValueError):
            await service.execute(query)

    assert mock_audit.call_args.kwargs["principal"] == "agent-a"
    assert mock_audit.call_args.kwargs["principal_scopes"] == ["read:orders"]
    assert mock_audit.call_args.kwargs["rejected"] is True


@pytest.mark.asyncio
async def test_execute_skips_cost_estimation_when_disabled_by_default():
    """Policy() leaves max_estimated_rows/max_estimated_cost unset — must be
    zero behavior change: no EXPLAIN round-trip at all.
    """
    table = _company_table()
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)

    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = [{"id": 1}]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
        patch.object(svc, "estimate_postgres_query_cost", AsyncMock()) as mock_estimate,
    ):
        service = StructuredQueryService(connection_id="demo")
        await service.execute(query)

    mock_estimate.assert_not_called()


@pytest.mark.asyncio
async def test_execute_allows_query_within_cost_estimate_threshold():
    table = _company_table()
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)

    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = [{"id": 1}]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    set_policy_store(PolicyStore(default=Policy(max_estimated_rows=1000), overrides={}))
    estimate = QueryCostEstimate(estimated_rows=10, estimated_total_cost=None)
    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
        patch.object(svc, "estimate_postgres_query_cost", AsyncMock(return_value=estimate)),
    ):
        service = StructuredQueryService(connection_id="demo")
        result = await service.execute(query)

    assert result.row_count == 1
    mock_session.execute.assert_awaited_once()  # the real query still ran


@pytest.mark.asyncio
async def test_execute_rejects_query_over_cost_estimate_threshold():
    table = _company_table()
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)
    mock_session = AsyncMock()

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    set_policy_store(PolicyStore(default=Policy(max_estimated_rows=1000), overrides={}))
    estimate = QueryCostEstimate(estimated_rows=999_999, estimated_total_cost=None)
    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
        patch.object(svc, "estimate_postgres_query_cost", AsyncMock(return_value=estimate)),
    ):
        service = StructuredQueryService(connection_id="demo")
        with pytest.raises(CostEstimateExceededError, match="estimated rows"):
            await service.execute(query)

    mock_session.execute.assert_not_called()  # rejected before the real query ran


@pytest.mark.asyncio
async def test_execute_skips_cost_estimation_for_non_postgres_dialect():
    """max_estimated_rows/max_estimated_cost are accepted for any dialect
    but only enforced for Postgres — see TODO.md item 26 phase 2 for MSSQL.
    """
    table = _company_table()
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)

    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = [{"id": 1}]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    mock_engine = MagicMock()
    mock_engine.dialect.name = "mssql"

    set_policy_store(PolicyStore(default=Policy(max_estimated_rows=1), overrides={}))
    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
        patch.object(svc, "get_engine", return_value=mock_engine),
        patch.object(svc, "estimate_postgres_query_cost", AsyncMock()) as mock_estimate,
    ):
        service = StructuredQueryService(connection_id="demo")
        result = await service.execute(query)

    mock_estimate.assert_not_called()
    assert result.row_count == 1


@pytest.mark.asyncio
async def test_execute_in_observe_mode_runs_the_query_instead_of_rejecting():
    """CostEstimationMode.OBSERVE must not block the query — it's a
    calibration aid, not a second enforcement path.
    """
    table = _company_table()
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)

    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = [{"id": 1}]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    set_policy_store(
        PolicyStore(
            default=Policy(
                max_estimated_rows=1000, cost_estimation_mode=CostEstimationMode.OBSERVE
            ),
            overrides={},
        )
    )
    estimate = QueryCostEstimate(estimated_rows=999_999, estimated_total_cost=None)
    before = _sample("querygate_cost_estimation_would_reject_total", {"connection": "demo"})
    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
        patch.object(svc, "estimate_postgres_query_cost", AsyncMock(return_value=estimate)),
    ):
        service = StructuredQueryService(connection_id="demo")
        result = await service.execute(query)  # must not raise

    assert result.row_count == 1
    mock_session.execute.assert_awaited_once()
    after = _sample("querygate_cost_estimation_would_reject_total", {"connection": "demo"})
    assert after == before + 1


@pytest.mark.asyncio
async def test_execute_in_observe_mode_does_not_flag_a_query_within_threshold():
    table = _company_table()
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)

    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = [{"id": 1}]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    set_policy_store(
        PolicyStore(
            default=Policy(
                max_estimated_rows=1000, cost_estimation_mode=CostEstimationMode.OBSERVE
            ),
            overrides={},
        )
    )
    estimate = QueryCostEstimate(estimated_rows=10, estimated_total_cost=None)
    before = _sample("querygate_cost_estimation_would_reject_total", {"connection": "demo"})
    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
        patch.object(svc, "estimate_postgres_query_cost", AsyncMock(return_value=estimate)),
    ):
        service = StructuredQueryService(connection_id="demo")
        await service.execute(query)

    after = _sample("querygate_cost_estimation_would_reject_total", {"connection": "demo"})
    assert after == before


def test_cap_response_bytes_keeps_all_rows_under_cap():
    rows = [{"id": i} for i in range(5)]
    kept, hit = _cap_response_bytes(rows, max_bytes=10_000)
    assert kept == rows
    assert hit is False


def test_cap_response_bytes_truncates_past_cap():
    rows = [{"blob": "x" * 100} for _ in range(10)]
    kept, hit = _cap_response_bytes(rows, max_bytes=250)
    assert hit is True
    assert 0 < len(kept) < len(rows)


def test_cap_response_bytes_omits_first_oversized_row():
    rows = [{"blob": "x" * 1000}]
    kept, hit = _cap_response_bytes(rows, max_bytes=10)
    assert kept == []
    assert hit is True


def test_cap_response_bytes_disabled_when_non_positive():
    rows = [{"blob": "x" * 1000}]
    kept, hit = _cap_response_bytes(rows, max_bytes=0)
    assert kept == rows
    assert hit is False


@pytest.mark.asyncio
async def test_execute_truncates_when_response_exceeds_byte_cap():
    table = _company_table()
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=100)

    rows = [{"id": i, "blob": "x" * 200} for i in range(20)]
    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = rows
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    set_policy_store(PolicyStore(default=Policy(max_response_bytes=1000), overrides={}))
    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
    ):
        service = StructuredQueryService(connection_id="demo")
        result = await service.execute(query)

    assert result.truncated is True
    assert result.row_count < len(rows)


@pytest.mark.asyncio
async def test_execute_many_partial_failure():
    query_ok = StructuredQuery(from_table="customers", select=["customers.id"], limit=5)
    query_bad = StructuredQuery(from_table="customers", select=["customers.id"], limit=5)
    ok_result = StructuredQueryResult(
        rows=[{"id": 1}], row_count=1, truncated=False, limit=5, offset=0
    )

    service = StructuredQueryService(connection_id="demo")
    with patch.object(
        service,
        "execute",
        AsyncMock(side_effect=[ok_result, QueryValidationError("boom")]),
    ):
        results = await service.execute_many([query_ok, query_bad])

    assert len(results) == 2
    assert results[0].error is None
    assert results[0].row_count == 1
    assert results[1].error == "boom"


@pytest.mark.asyncio
async def test_explain_many_partial_failure():
    """Mirrors test_execute_many_partial_failure — TODO.md item 61's
    run_structured_queries(mode="explain") tool relies on the same per-item
    error isolation as execute_many, not a top-level failure.
    """
    query_ok = StructuredQuery(from_table="customers", select=["customers.id"], limit=5)
    query_bad = StructuredQuery(from_table="customers", select=["customers.id"], limit=5)
    ok_result = ExplainResult(
        sql="SELECT customers.id FROM customers", tables=["customers"], limit=5
    )

    service = StructuredQueryService(connection_id="demo")
    with patch.object(
        service,
        "explain",
        AsyncMock(side_effect=[ok_result, QueryValidationError("boom")]),
    ):
        results = await service.explain_many([query_ok, query_bad])

    assert len(results) == 2
    assert results[0].error is None
    assert results[0].sql == "SELECT customers.id FROM customers"
    assert results[1].error == "boom"
    assert results[1].sql is None


@pytest.mark.asyncio
async def test_explain_does_not_open_a_db_session():
    table = _company_table()
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=5)

    mock_scope = MagicMock(side_effect=AssertionError("explain() must not open a DB session"))
    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", mock_scope),
    ):
        service = StructuredQueryService(connection_id="demo")
        result = await service.explain(query)

    mock_scope.assert_not_called()
    assert "customers" in result.sql
    assert result.tables == ["customers"]
    assert result.limit == 5


@pytest.mark.asyncio
async def test_list_tables_includes_known_tables():
    service = StructuredQueryService(connection_id="demo")
    with patch.object(svc, "get_metadata") as mock_meta:
        meta = MagicMock()
        meta.tables = {}
        mock_meta.return_value = meta
        tables = await service.list_tables()
    # "demo" connection's known_tables (set in conftest.make_demo_registry)
    assert set(tables) == {"customers", "orders", "order_items"}


@pytest.mark.asyncio
async def test_list_tables_falls_back_to_live_query_without_known_tables():
    from querygate.connections.models import ConnectionProfile
    from querygate.connections.registry import ConnectionRegistry, set_registry

    profile = ConnectionProfile(
        id="demo", dialect="postgresql", connection_string="postgresql+asyncpg://x/y"
    )
    set_registry(ConnectionRegistry({"demo": profile}))

    service = StructuredQueryService(connection_id="demo")
    with (
        patch.object(svc, "get_metadata") as mock_meta,
        patch.object(svc, "list_live_tables", AsyncMock(return_value=["LiveOnlyTable"])),
    ):
        meta = MagicMock()
        meta.tables = {}
        mock_meta.return_value = meta
        tables = await service.list_tables()
    assert tables == ["LiveOnlyTable"]


@pytest.mark.asyncio
async def test_list_tables_filters_denied_tables():
    set_policy_store(PolicyStore(default=Policy(denied_tables=["order_items"]), overrides={}))
    service = StructuredQueryService(connection_id="demo")
    with patch.object(svc, "get_metadata") as mock_meta:
        meta = MagicMock()
        meta.tables = {}
        mock_meta.return_value = meta
        tables = await service.list_tables()
    assert "order_items" not in tables
    assert "customers" in tables


@pytest.mark.asyncio
async def test_describe_table():
    table = sa.Table(
        "customers",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(50), nullable=True),
    )
    service = StructuredQueryService(connection_id="demo")
    with (
        patch.object(svc, "get_engine", return_value=MagicMock()),
        patch.object(svc, "get_table_schema", AsyncMock(return_value=table)),
    ):
        desc = await service.describe_table("customers")
    assert isinstance(desc, TableDescription)
    assert desc.name == "customers"
    assert {c.name for c in desc.columns} == {"id", "name"}


@pytest.mark.asyncio
async def test_describe_table_renders_unmapped_column_types_meaningfully():
    """MSSQL's geography/geometry columns reflect to SQLAlchemy's NullType
    (confirmed against a live server — see tests/integration/test_mssql_live.py) —
    describe_table must not report that as the misleading literal "NULL".
    """
    table = sa.Table(
        "store_locations",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("geo", sa.types.NullType()),
    )
    service = StructuredQueryService(connection_id="demo")
    with (
        patch.object(svc, "get_engine", return_value=MagicMock()),
        patch.object(svc, "get_table_schema", AsyncMock(return_value=table)),
    ):
        desc = await service.describe_table("store_locations")
    geo_column = next(c for c in desc.columns if c.name == "geo")
    assert geo_column.type != "NULL"
    assert "unsupported" in geo_column.type


@pytest.mark.asyncio
async def test_describe_table_hides_denied_columns():
    table = sa.Table(
        "customers",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(200)),
    )
    set_policy_store(
        PolicyStore(default=Policy(denied_columns={"customers": ["email"]}), overrides={})
    )
    service = StructuredQueryService(connection_id="demo")
    with (
        patch.object(svc, "get_engine", return_value=MagicMock()),
        patch.object(svc, "get_table_schema", AsyncMock(return_value=table)),
    ):
        desc = await service.describe_table("customers")
    assert {c.name for c in desc.columns} == {"id"}


@pytest.mark.asyncio
async def test_describe_table_rejects_denied_table():
    set_policy_store(PolicyStore(default=Policy(denied_tables=["customers"]), overrides={}))
    service = StructuredQueryService(connection_id="demo")
    with pytest.raises(Exception, match="not accessible"):
        await service.describe_table("customers")


@pytest.mark.asyncio
async def test_describe_table_per_principal_policy_differs_on_same_connection():
    """Two principals hitting the same connection get different column
    visibility — the whole point of item 6 (per-principal policy).
    """
    table = sa.Table(
        "customers",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(200)),
    )
    set_policy_store(
        PolicyStore.from_dict(
            {
                "default": {},
                "principals": {"agent-a": {"demo": {"denied_columns": {"customers": ["email"]}}}},
            }
        )
    )

    with (
        patch.object(svc, "get_engine", return_value=MagicMock()),
        patch.object(svc, "get_table_schema", AsyncMock(return_value=table)),
    ):
        agent_a = StructuredQueryService(
            connection_id="demo", principal=Principal(subject="agent-a")
        )
        desc_a = await agent_a.describe_table("customers")

        agent_b = StructuredQueryService(
            connection_id="demo", principal=Principal(subject="agent-b")
        )
        desc_b = await agent_b.describe_table("customers")

    assert {c.name for c in desc_a.columns} == {"id"}
    assert {c.name for c in desc_b.columns} == {"id", "email"}


@pytest.mark.asyncio
async def test_describe_table_with_no_catalog_configured_has_no_catalog_fields():
    table = sa.Table("customers", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True))
    service = StructuredQueryService(connection_id="demo")
    with (
        patch.object(svc, "get_engine", return_value=MagicMock()),
        patch.object(svc, "get_table_schema", AsyncMock(return_value=table)),
    ):
        desc = await service.describe_table("customers")
    assert desc.catalog is None
    assert desc.columns[0].catalog is None


@pytest.mark.asyncio
async def test_describe_table_merges_catalog_metadata():
    table = sa.Table(
        "customers",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(200)),
    )
    set_catalog_store(
        CatalogStore.from_dict(
            {
                "connections": {
                    "demo": {
                        "tables": {
                            "customers": {
                                "description": "One row per customer.",
                                "sensitivity": "internal",
                                "provenance": {"created_by": "private-admin-subject"},
                                "relationships": [
                                    {
                                        "to_table": "orders",
                                        "column": "id",
                                        "to_column": "customer_id",
                                    }
                                ],
                                "columns": {
                                    "email": {"description": "Email", "sensitivity": "pii"}
                                },
                            }
                        }
                    }
                }
            }
        )
    )
    service = StructuredQueryService(connection_id="demo")
    with (
        patch.object(svc, "get_engine", return_value=MagicMock()),
        patch.object(svc, "get_table_schema", AsyncMock(return_value=table)),
    ):
        desc = await service.describe_table("customers", verbose_provenance=True)

    assert desc.catalog.description == "One row per customer."
    assert desc.catalog.sensitivity == "internal"
    assert desc.catalog.relationships[0].to_table == "orders"
    assert desc.catalog.provenance.status == "verified"
    assert desc.catalog.provenance.freshness == "untracked"
    email_col = next(c for c in desc.columns if c.name == "email")
    assert email_col.catalog.description == "Email"
    assert email_col.catalog.sensitivity == "pii"
    id_col = next(c for c in desc.columns if c.name == "id")
    assert id_col.catalog is None  # no catalog entry for this column
    assert "private-admin-subject" not in json.dumps(desc.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_describe_table_catalog_provenance_is_compact_by_default():
    """TODO.md item 64: full CatalogCitation (entry_id, source_evidence,
    catalog_version, schema_fingerprint, ...) is opt-in via
    verbose_provenance — the default response carries only status +
    precedence, smaller but still enough to judge trust.
    """
    table = sa.Table(
        "customers",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(200)),
    )
    set_catalog_store(
        CatalogStore.from_dict(
            {
                "connections": {
                    "demo": {
                        "tables": {
                            "customers": {
                                "description": "One row per customer.",
                                "columns": {"email": {"description": "Email"}},
                            }
                        }
                    }
                }
            }
        )
    )
    service = StructuredQueryService(connection_id="demo")
    with (
        patch.object(svc, "get_engine", return_value=MagicMock()),
        patch.object(svc, "get_table_schema", AsyncMock(return_value=table)),
    ):
        compact = await service.describe_table("customers")
        verbose = await service.describe_table("customers", verbose_provenance=True)

    assert compact.catalog.provenance.model_dump() == {
        "status": "verified",
        "precedence": verbose.catalog.provenance.precedence,
    }
    assert not hasattr(compact.catalog.provenance, "entry_id")
    assert verbose.catalog.provenance.entry_id
    assert verbose.catalog.provenance.status == "verified"
    email_col = next(c for c in compact.columns if c.name == "email")
    assert email_col.catalog.provenance.model_dump() == {
        "status": "verified",
        "precedence": email_col.catalog.provenance.precedence,
    }
    compact_bytes = len(json.dumps(compact.model_dump(mode="json")))
    verbose_bytes = len(json.dumps(verbose.model_dump(mode="json")))
    assert compact_bytes < verbose_bytes


@pytest.mark.asyncio
async def test_describe_table_catalog_hides_relationship_to_denied_table():
    """A curated relationship hint toward a table the caller's resolved
    policy denies must not leak that table's existence — the same
    non-enumeration property policy enforces for the query AST itself.
    """
    table = sa.Table("orders", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True))
    set_catalog_store(
        CatalogStore.from_dict(
            {
                "connections": {
                    "demo": {
                        "tables": {
                            "orders": {
                                "relationships": [
                                    {
                                        "to_table": "customers",
                                        "column": "customer_id",
                                        "to_column": "id",
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        )
    )
    set_policy_store(PolicyStore(default=Policy(denied_tables=["customers"]), overrides={}))
    service = StructuredQueryService(connection_id="demo")
    with (
        patch.object(svc, "get_engine", return_value=MagicMock()),
        patch.object(svc, "get_table_schema", AsyncMock(return_value=table)),
    ):
        desc = await service.describe_table("orders")

    assert desc.catalog.relationships == []


@pytest.mark.asyncio
async def test_execute_mandatory_row_filter_resolved_from_principal_claim():
    table = _company_table()
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)

    captured_stmt = {}
    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = []

    async def _execute(stmt):
        captured_stmt["stmt"] = stmt
        return mock_result

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=_execute)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    set_policy_store(
        PolicyStore(
            default=Policy(
                mandatory_row_filters=[
                    MandatoryRowFilter(table="customers", column="name", from_claim="tenant_name")
                ]
            ),
            overrides={},
        )
    )
    principal = Principal(subject="agent-a", claims={"tenant_name": "Ada"})
    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
    ):
        service = StructuredQueryService(connection_id="demo", principal=principal)
        await service.execute(query)

    compiled = str(captured_stmt["stmt"].compile(compile_kwargs={"literal_binds": True}))
    assert "Ada" in compiled


# --- Agent-visible capacity waiting (TODO.md item 35 phase 1) --------------


@pytest.mark.asyncio
async def test_execute_returns_admission_id_and_queue_wait_ms_on_success():
    table = _company_table()
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)

    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = [{"id": 1}]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
    ):
        service = StructuredQueryService(connection_id="demo")
        result = await service.execute(query)

    assert result.admission_id
    assert result.queue_wait_ms is not None and result.queue_wait_ms >= 0


@pytest.mark.asyncio
async def test_execute_two_calls_get_different_admission_ids():
    table = _company_table()
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)

    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = []
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
    ):
        service = StructuredQueryService(connection_id="demo")
        first = await service.execute(query)
        second = await service.execute(query)

    assert first.admission_id != second.admission_id


@pytest.mark.asyncio
async def test_execute_calls_on_wait_start_and_on_admitted_hooks():
    """TODO.md item 35 phase 3: the two-point progress signal MCP's
    Context.report_progress and the async execution lifecycle both build on.
    Ordinary synchronous callers pass neither hook (see the many tests above
    with no on_wait_start/on_admitted) — this pins the opt-in shape."""
    table = _company_table()
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)

    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = []
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    wait_start_calls = []
    admitted_calls = []

    async def on_wait_start(wait_seconds: float) -> None:
        wait_start_calls.append(wait_seconds)

    async def on_admitted(queue_wait_ms: int) -> None:
        admitted_calls.append(queue_wait_ms)

    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
    ):
        service = StructuredQueryService(connection_id="demo")
        await service.execute(query, on_wait_start=on_wait_start, on_admitted=on_admitted)

    assert len(wait_start_calls) == 1
    assert len(admitted_calls) == 1
    assert admitted_calls[0] >= 0


@pytest.mark.asyncio
async def test_execute_calls_on_session_identifier_hook_via_session_scope():
    """The hook is forwarded to `session_scope` as `session_identifier_sink`
    unchanged — proven here against the real `session_scope` contract rather
    than a bespoke test double, since `connections/engine.py`'s own tests
    cover the capture logic itself."""
    table = _company_table()
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)

    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = []
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    captured_kwargs = {}

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        captured_kwargs.update(kwargs)
        sink = kwargs.get("session_identifier_sink")
        if sink is not None:
            sink("4242")
        yield mock_session

    identifiers = []

    def on_session_identifier(identifier: str) -> None:
        identifiers.append(identifier)

    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
    ):
        service = StructuredQueryService(connection_id="demo")
        await service.execute(query, on_session_identifier=on_session_identifier)

    assert identifiers == ["4242"]
    assert captured_kwargs["session_identifier_sink"] is on_session_identifier


@pytest.mark.asyncio
async def test_execute_fail_fast_raises_capacity_timeout_without_waiting():
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)
    set_policy_store(
        PolicyStore(default=Policy(max_concurrency=1, concurrency_wait_seconds=5), overrides={})
    )
    await cc.in_process_limiter().semaphore("demo", 1).acquire()  # occupy the only slot
    validate_schema = AsyncMock()

    with patch.object(svc, "validate_schema", validate_schema):
        service = StructuredQueryService(connection_id="demo")
        start = time.monotonic()
        with pytest.raises(CapacityTimeoutError) as exc_info:
            await service.execute(query, queue_mode=QueueMode.FAIL_FAST)
        elapsed = time.monotonic() - start

    # fail_fast must not wait anywhere near the 5s policy ceiling.
    assert elapsed < 1.0
    assert exc_info.value.admission_id
    assert exc_info.value.queue_wait_ms is not None
    # Never reached schema validation — the slot was never acquired.
    validate_schema.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_wait_timeout_seconds_cannot_exceed_policy_ceiling():
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)
    set_policy_store(
        PolicyStore(default=Policy(max_concurrency=1, concurrency_wait_seconds=0.1), overrides={})
    )
    await cc.in_process_limiter().semaphore(
        "demo", 1
    ).acquire()  # occupy the only slot, never released

    service = StructuredQueryService(connection_id="demo")
    start = time.monotonic()
    with pytest.raises(CapacityTimeoutError):
        # A caller cannot extend the operator's 0.1s ceiling by asking for 999s.
        await service.execute(query, queue_mode=QueueMode.WAIT, wait_timeout_seconds=999.0)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_execute_capacity_timeout_is_audited_with_admission_fields():
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)
    set_policy_store(
        PolicyStore(default=Policy(max_concurrency=1, concurrency_wait_seconds=5), overrides={})
    )
    await cc.in_process_limiter().semaphore("demo", 1).acquire()  # occupy the only slot

    audit_query = MagicMock()
    with patch.object(svc, "audit_query", audit_query):
        service = StructuredQueryService(connection_id="demo")
        with pytest.raises(CapacityTimeoutError):
            await service.execute(query, queue_mode=QueueMode.FAIL_FAST)

    audit_query.assert_called_once()
    kwargs = audit_query.call_args.kwargs
    assert kwargs["admission_id"]
    assert kwargs["queue_wait_ms"] is not None
    assert kwargs["admission_state"] == "capacity_timeout"
    assert kwargs["rejected"] is True


# --- Queue-depth pressure controls (TODO.md item 35 phase 2) ---------------


def _queued_execute_kwargs():
    """A schema-mocked execute() body, used by the tests below so a released
    waiter can run to a real success instead of hitting unrelated
    NoSuchTableError noise once its concurrency slot is finally granted.
    """
    table = _company_table()
    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = []
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    return table, _scope


@pytest.mark.asyncio
async def test_execute_raises_queue_full_error_once_max_queue_depth_is_met():
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)
    set_policy_store(
        PolicyStore(
            default=Policy(max_concurrency=1, concurrency_wait_seconds=5, max_queue_depth=1),
            overrides={},
        )
    )
    await cc.in_process_limiter().semaphore("demo", 1).acquire()  # occupy the only slot

    table, _scope = _queued_execute_kwargs()
    service = StructuredQueryService(connection_id="demo")

    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
    ):
        task = asyncio.create_task(
            service.execute(query, queue_mode=QueueMode.WAIT, wait_timeout_seconds=5)
        )
        await asyncio.sleep(0.02)  # let it actually start waiting (queue depth == 1)

        with pytest.raises(QueueFullError) as exc_info:
            await service.execute(query, queue_mode=QueueMode.WAIT, wait_timeout_seconds=5)

        assert exc_info.value.admission_id
        assert exc_info.value.queue_wait_ms == 0
        assert exc_info.value.admission_state == "queue_full"

        cc.in_process_limiter().semaphore("demo", 1).release()
        await task  # the first (legitimate) waiter still succeeds normally


@pytest.mark.asyncio
async def test_execute_queue_full_is_audited_with_queue_full_admission_state():
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)
    set_policy_store(
        PolicyStore(
            default=Policy(max_concurrency=1, concurrency_wait_seconds=5, max_queue_depth=1),
            overrides={},
        )
    )
    await cc.in_process_limiter().semaphore("demo", 1).acquire()  # occupy the only slot

    table, _scope = _queued_execute_kwargs()
    service = StructuredQueryService(connection_id="demo")

    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
    ):
        task = asyncio.create_task(
            service.execute(query, queue_mode=QueueMode.WAIT, wait_timeout_seconds=5)
        )
        await asyncio.sleep(0.02)

        audit_query = MagicMock()
        with patch.object(svc, "audit_query", audit_query):
            with pytest.raises(QueueFullError):
                await service.execute(query, queue_mode=QueueMode.WAIT, wait_timeout_seconds=5)

        audit_query.assert_called_once()
        kwargs = audit_query.call_args.kwargs
        assert kwargs["admission_id"]
        assert kwargs["queue_wait_ms"] == 0
        assert kwargs["admission_state"] == "queue_full"
        assert kwargs["rejected"] is True

        cc.in_process_limiter().semaphore("demo", 1).release()
        await task


@pytest.mark.asyncio
async def test_execute_max_queue_depth_per_principal_does_not_affect_other_principals():
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)
    set_policy_store(
        PolicyStore(
            default=Policy(
                max_concurrency=1, concurrency_wait_seconds=5, max_queue_depth_per_principal=1
            ),
            overrides={},
        )
    )
    await cc.in_process_limiter().semaphore("demo", 1).acquire()  # occupy the only slot

    table, _scope = _queued_execute_kwargs()
    noisy_service = StructuredQueryService(
        connection_id="demo", principal=Principal(subject="noisy-agent")
    )
    other_service = StructuredQueryService(
        connection_id="demo", principal=Principal(subject="other-agent")
    )

    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
    ):
        noisy_task = asyncio.create_task(
            noisy_service.execute(query, queue_mode=QueueMode.WAIT, wait_timeout_seconds=5)
        )
        await asyncio.sleep(0.02)

        # Same noisy principal, queue already has one of theirs waiting -> queue_full.
        with pytest.raises(QueueFullError):
            await noisy_service.execute(query, queue_mode=QueueMode.WAIT, wait_timeout_seconds=5)

        # A different principal must not be affected by noisy-agent's cap.
        other_task = asyncio.create_task(
            other_service.execute(query, queue_mode=QueueMode.WAIT, wait_timeout_seconds=5)
        )
        await asyncio.sleep(0.02)

        cc.in_process_limiter().semaphore("demo", 1).release()
        await noisy_task
        await other_task


# --- TODO item 32C: best-effort usage-signal emission on the execute() path -


def _orders_and_customers() -> tuple[sa.Table, sa.Table]:
    metadata = sa.MetaData()
    customers = sa.Table("customers", metadata, sa.Column("id", sa.Integer, primary_key=True))
    orders = sa.Table(
        "orders",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("customer_id", sa.Integer),
    )
    return orders, customers


async def _execute_join_query(*, principal=None, joins=None):
    from querygate.catalog.schema_memory import ObservedSchemaSnapshot

    orders, customers = _orders_and_customers()
    snapshot = ObservedSchemaSnapshot.from_tables("demo", [orders, customers])
    set_catalog_store(
        CatalogStore.from_dict(
            {"version": 2, "schema_snapshots": {"demo": snapshot.model_dump(mode="json")}}
        )
    )

    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        joins=joins or [{"table": "customers", "on": ["orders.customer_id", "customers.id"]}],
        limit=10,
    )
    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = [{"id": 1}]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    with (
        patch.object(
            svc,
            "validate_schema",
            AsyncMock(return_value={"orders": orders, "customers": customers}),
        ),
        patch.object(svc, "session_scope", _scope),
    ):
        service = StructuredQueryService(connection_id="demo", principal=principal)
        await service.execute(query)


@pytest.mark.asyncio
async def test_execute_emits_table_and_relationship_usage_signals_when_enabled(monkeypatch):
    from querygate.catalog.usage import get_usage_signal_buffer

    monkeypatch.setattr(svc.app_config, "semantic_memory_usage_signals_enabled", True)
    monkeypatch.setattr(svc.app_config, "catalog_file", "catalog.yaml")

    await _execute_join_query(principal=Principal(subject="alice"))

    signals = get_usage_signal_buffer().drain("demo")
    kinds = {signal.kind.value for signal in signals}
    assert kinds == {"table_used", "relationship_used"}
    relationship_signal = next(s for s in signals if s.kind.value == "relationship_used")
    assert relationship_signal.target.table == "orders"
    assert relationship_signal.target.column == "customer_id"
    assert relationship_signal.target.to_table == "customers"
    assert relationship_signal.target.to_column == "id"
    # Never the raw principal subject.
    assert "alice" not in relationship_signal.principal_partition


@pytest.mark.asyncio
async def test_execute_emits_no_usage_signals_when_disabled_by_default(monkeypatch):
    from querygate.catalog.usage import get_usage_signal_buffer

    assert svc.app_config.semantic_memory_usage_signals_enabled is False
    await _execute_join_query()
    assert get_usage_signal_buffer().drain("demo") == []


@pytest.mark.asyncio
async def test_execute_suppresses_usage_signal_for_an_unverified_catalog_guess(monkeypatch):
    """Anti-feedback-loop rule: if the only catalog knowledge behind a table
    is our own unreviewed draft/stale guess, using it must not be recorded
    as validating evidence for that guess.
    """
    from querygate.catalog.usage import get_usage_signal_buffer

    monkeypatch.setattr(svc.app_config, "semantic_memory_usage_signals_enabled", True)
    monkeypatch.setattr(svc.app_config, "catalog_file", "catalog.yaml")

    from querygate.catalog.schema_memory import ObservedSchemaSnapshot

    orders, customers = _orders_and_customers()
    snapshot = ObservedSchemaSnapshot.from_tables("demo", [orders, customers])
    set_catalog_store(
        CatalogStore.from_dict(
            {
                "version": 2,
                "schema_snapshots": {"demo": snapshot.model_dump(mode="json")},
                "connections": {
                    "demo": {
                        "tables": {
                            "orders": {
                                "description": "unreviewed guess",
                                "provenance": {
                                    "status": "draft",
                                    "source_class": "inferred",
                                    "confidence": 0.4,
                                },
                            }
                        }
                    }
                },
            }
        )
    )

    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        joins=[{"table": "customers", "on": ["orders.customer_id", "customers.id"]}],
        limit=10,
    )
    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = [{"id": 1}]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    with (
        patch.object(
            svc,
            "validate_schema",
            AsyncMock(return_value={"orders": orders, "customers": customers}),
        ),
        patch.object(svc, "session_scope", _scope),
    ):
        service = StructuredQueryService(connection_id="demo")
        await service.execute(query)

    signals = get_usage_signal_buffer().drain("demo")
    # "orders" (table_used) is suppressed (unverified guess); "customers"
    # (table_used, no catalog entry at all) and the relationship (no
    # relationship entry exists) are still organic, legitimate evidence.
    orders_table_signals = [
        s for s in signals if s.kind.value == "table_used" and s.target.table == "orders"
    ]
    assert orders_table_signals == []
    assert any(s.kind.value == "table_used" and s.target.table == "customers" for s in signals)
    assert any(s.kind.value == "relationship_used" for s in signals)


@pytest.mark.asyncio
async def test_execute_usage_signal_emission_never_raises_on_failure(monkeypatch):
    """Best-effort by construction: an internal failure while building/
    enqueuing usage signals must never surface as a query failure.
    """
    monkeypatch.setattr(svc.app_config, "semantic_memory_usage_signals_enabled", True)
    monkeypatch.setattr(svc.app_config, "catalog_file", "catalog.yaml")
    with patch.object(svc, "get_catalog_store", side_effect=RuntimeError("boom")):
        await _execute_join_query()  # must not raise


@pytest.mark.asyncio
async def test_database_type_error_becomes_a_clean_typed_validation_error():
    """A statement the database itself refuses — a type/operator mismatch, which
    item 100's arithmetic makes an ordinary caller mistake — must surface as a
    typed 4xx, not a 500, and must not echo the driver's message.

    The driver text is the leak risk: `psycopg` reports the failing operator and
    the real column types, so returning it (or storing it as the audit rejection
    reason) would disclose schema. Mirrors the write path's constraint-error
    mapping in `write_execution.py`.
    """
    table = _company_table()
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)
    mock_session = AsyncMock()
    leaky = ProgrammingError(
        "SELECT customers.secret_salary * 'x'",
        {},
        Exception('operator does not exist: numeric * text HINT: column "secret_salary"'),
    )
    mock_session.execute.side_effect = leaky

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    set_policy_store(PolicyStore(default=Policy(), overrides={}))
    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
    ):
        service = StructuredQueryService(connection_id="demo")
        with pytest.raises(QueryValidationError) as excinfo:
            await service.execute(query)

    message = str(excinfo.value)
    assert "not valid for the referenced columns" in message
    for leak in ("secret_salary", "operator does not exist", "HINT"):
        assert leak not in message, leak
    # The client-safe projection of this exception is the message itself (it is
    # a QueryValidationError), so the same non-leak guarantee holds on the wire.
    assert public_error_message(excinfo.value) == message


@pytest.mark.asyncio
async def test_window_type_error_becomes_a_clean_typed_validation_error():
    """The same mapping must cover item 101: `SUM(<text column>) OVER (...)` is a
    statement the database refuses, and its driver message names the real column
    and types. A window is not a second execution path — this pins that it is
    covered by the one mapping rather than assumed to be.
    """
    table = _company_table()
    query = StructuredQuery.model_validate(
        {
            "from": "customers",
            "select": [
                "customers.id",
                {
                    "fn": "sum",
                    "arg": {"col": "customers.name"},
                    "over": {"order_by": [{"col": "customers.id"}]},
                    "as": "running",
                },
            ],
            "limit": 10,
        }
    )
    mock_session = AsyncMock()
    mock_session.execute.side_effect = ProgrammingError(
        "SELECT sum(customers.secret_salary) OVER (ORDER BY customers.id)",
        {},
        Exception('function sum(text) does not exist HINT: column "secret_salary"'),
    )

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    set_policy_store(PolicyStore(default=Policy(), overrides={}))
    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
    ):
        service = StructuredQueryService(connection_id="demo")
        with pytest.raises(QueryValidationError) as excinfo:
            await service.execute(query)

    message = str(excinfo.value)
    assert "not valid for the referenced columns" in message
    for leak in ("secret_salary", "does not exist", "HINT"):
        assert leak not in message, leak
    assert public_error_message(excinfo.value) == message


@pytest.mark.asyncio
async def test_usage_signals_survive_a_range_join_that_carries_no_on_pair(monkeypatch):
    """A range/cross join (item 103) sets `JoinSpec.on` to None, and
    `_usage_signal_targets` used to unpack it unconditionally.

    The failure was invisible: `_emit_usage_signals` swallows every exception, and
    the target list is built EAGERLY — so one un-unpackable join discarded the
    whole batch, including the `table_used` signals computed before it. Item 32C
    adaptive learning went blind for any query using a range join, leaving only a
    warning line. Asserted from the outside (which signals actually land), not by
    calling the private builder.
    """
    from querygate.catalog.usage import get_usage_signal_buffer

    monkeypatch.setattr(svc.app_config, "semantic_memory_usage_signals_enabled", True)
    monkeypatch.setattr(svc.app_config, "catalog_file", "catalog.yaml")

    await _execute_join_query(
        principal=Principal(subject="alice"),
        joins=[
            {
                "table": "customers",
                "condition": {
                    "col": "orders.customer_id",
                    "op": "gte",
                    "value_col": "customers.id",
                },
            }
        ],
    )

    signals = get_usage_signal_buffer().drain("demo")
    kinds = {signal.kind.value for signal in signals}
    # Both tables are still recorded as used. No relationship signal, deliberately:
    # a RANGE join asserts no single [Left.Col, Right.Col] pair to record.
    assert kinds == {"table_used"}
    assert {s.target.table for s in signals} == {"orders", "customers"}


@pytest.mark.asyncio
async def test_usage_signals_survive_a_cross_join(monkeypatch):
    """The other `on is None` shape — same eager-build failure, same blast radius."""
    from querygate.catalog.usage import get_usage_signal_buffer

    from querygate.policy.loader import PolicyStore, set_policy_store
    from querygate.policy.models import Policy

    monkeypatch.setattr(svc.app_config, "semantic_memory_usage_signals_enabled", True)
    monkeypatch.setattr(svc.app_config, "catalog_file", "catalog.yaml")
    set_policy_store(PolicyStore(default=Policy(allow_cross_join=True), overrides={}))

    await _execute_join_query(
        principal=Principal(subject="alice"),
        joins=[{"table": "customers", "type": "cross"}],
    )

    signals = get_usage_signal_buffer().drain("demo")
    assert {s.kind.value for s in signals} == {"table_used"}
    assert {s.target.table for s in signals} == {"orders", "customers"}


@pytest.mark.asyncio
async def test_an_equality_condition_join_emits_the_same_relationship_as_the_on_form(
    monkeypatch,
):
    """Item 103 made `condition` a second way to write `ON a.x = b.y`. The two
    spellings describe the identical relationship, so the catalog must learn the
    identical thing from both — otherwise the newer spelling silently teaches it
    nothing. Same reasoning as `value_column` in the audit shape, one layer over.
    """
    from querygate.catalog.usage import get_usage_signal_buffer

    monkeypatch.setattr(svc.app_config, "semantic_memory_usage_signals_enabled", True)
    monkeypatch.setattr(svc.app_config, "catalog_file", "catalog.yaml")

    await _execute_join_query(
        principal=Principal(subject="alice"),
        joins=[
            {
                "table": "customers",
                "condition": {
                    "col": "orders.customer_id",
                    "op": "eq",
                    "value_col": "customers.id",
                },
            }
        ],
    )

    signals = get_usage_signal_buffer().drain("demo")
    relationship = next(s for s in signals if s.kind.value == "relationship_used")
    assert relationship.target.table == "orders"
    assert relationship.target.column == "customer_id"
    assert relationship.target.to_table == "customers"
    assert relationship.target.to_column == "id"
