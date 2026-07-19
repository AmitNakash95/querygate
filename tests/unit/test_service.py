"""Unit tests for StructuredQueryService — the seam every REST route and MCP
tool goes through. Real schema validation is bypassed via a patched
validate_schema; policy validation runs for real against a permissive Policy.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa

from querygate.catalog.loader import CatalogStore, set_catalog_store
from querygate.core.auth import Principal
from querygate.core.exceptions import (
    CapacityTimeoutError,
    CostEstimateExceededError,
    QueryValidationError,
)
from querygate.execution import concurrency as cc
from querygate.execution import service as svc
from querygate.execution.admission import QueueMode
from querygate.execution.cost_estimation import QueryCostEstimate
from querygate.execution.service import (
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

    validate_schema.assert_awaited_once_with(query, connection_id="demo", principal=principal)


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
        desc = await service.describe_table("customers")

    assert desc.catalog.description == "One row per customer."
    assert desc.catalog.sensitivity == "internal"
    assert desc.catalog.relationships[0].to_table == "orders"
    email_col = next(c for c in desc.columns if c.name == "email")
    assert email_col.catalog.description == "Email"
    assert email_col.catalog.sensitivity == "pii"
    id_col = next(c for c in desc.columns if c.name == "id")
    assert id_col.catalog is None  # no catalog entry for this column


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
async def test_execute_fail_fast_raises_capacity_timeout_without_waiting():
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=10)
    set_policy_store(
        PolicyStore(default=Policy(max_concurrency=1, concurrency_wait_seconds=5), overrides={})
    )
    cc.SEMAPHORES["demo"] = asyncio.Semaphore(1)
    await cc.SEMAPHORES["demo"].acquire()  # occupy the only slot
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
    cc.SEMAPHORES["demo"] = asyncio.Semaphore(1)
    await cc.SEMAPHORES["demo"].acquire()  # occupy the only slot, never released

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
    cc.SEMAPHORES["demo"] = asyncio.Semaphore(1)
    await cc.SEMAPHORES["demo"].acquire()  # occupy the only slot

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
