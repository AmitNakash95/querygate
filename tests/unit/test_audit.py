"""Persisted audit schema and JSONL sink tests."""

from __future__ import annotations

import json
import stat
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa

import asyncio

from querygate.audit.events import AuditEvent, normalize_query_shape
from querygate.audit.logger import audit_connection_probe, audit_query
from querygate.audit.sinks import (
    JsonlAuditSink,
    NullAuditSink,
    get_audit_sink,
    reset_audit_sink,
    set_audit_sink,
)
from querygate.api.app import create_app
from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.exceptions import PolicyViolationError
from querygate.core.logging import ContextLogger, context_logger
from querygate.execution import concurrency as cc
from querygate.execution import service as svc
from querygate.execution.admission import QueueMode
from querygate.execution.service import StructuredQueryService
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.query_ast.models import (
    ArrayAggSelectItem,
    CaseSelectItem,
    PercentileContSelectItem,
    Predicate,
    ScalarFunctionSelectItem,
    StringAggSelectItem,
    StructuredQuery,
)


@pytest.fixture(autouse=True)
def _isolated_sink():
    reset_audit_sink()
    yield
    reset_audit_sink()


def _query_with_sensitive_literals() -> StructuredQuery:
    return StructuredQuery(
        from_table="customers",
        select=["customers.id", "customers.email"],
        where=Predicate(col="customers.email", op="eq", value="secret@example.com"),
        limit=10,
        intent="Find secret@example.com for an executive request",
    )


def test_normalized_query_shape_excludes_literals_and_intent():
    serialized = json.dumps(normalize_query_shape(_query_with_sensitive_literals()))

    assert "customers.email" in serialized
    assert '"operator": "eq"' in serialized
    assert "secret@example.com" not in serialized
    assert "executive request" not in serialized


def test_normalized_query_shape_handles_string_agg_select_item():
    query = StructuredQuery(
        from_table="customers",
        select=[
            "customers.country",
            StringAggSelectItem(col="customers.email", delimiter=", ", alias="emails"),
        ],
        group_by=["customers.country"],
        limit=10,
    )
    shape = normalize_query_shape(query)
    serialized = json.dumps(shape)
    assert "customers.email" in serialized
    assert '"kind": "string_agg"' in serialized


def test_normalized_query_shape_handles_array_agg_select_item():
    query = StructuredQuery(
        from_table="customers",
        select=[
            "customers.country",
            ArrayAggSelectItem(col="customers.email", alias="emails"),
        ],
        group_by=["customers.country"],
        limit=10,
    )
    shape = normalize_query_shape(query)
    serialized = json.dumps(shape)
    assert "customers.email" in serialized
    assert '"kind": "array_agg"' in serialized


def test_normalized_query_shape_handles_percentile_cont_select_item():
    query = StructuredQuery(
        from_table="orders",
        select=[
            "orders.customer_id",
            PercentileContSelectItem(col="orders.total_amount", fraction=0.5, alias="median"),
        ],
        group_by=["orders.customer_id"],
        limit=10,
    )
    shape = normalize_query_shape(query)
    serialized = json.dumps(shape)
    assert "orders.total_amount" in serialized
    assert '"kind": "percentile_cont"' in serialized


def test_normalized_query_shape_handles_scalar_function_select_item():
    """Pre-existing gap fixed alongside item 80: _select_shape previously
    raised TypeError for any select item it didn't explicitly recognize,
    which meant any real query selecting a scalar function crashed
    execute() entirely (normalize_query_shape is called unconditionally,
    unguarded, at the top of StructuredQueryService.execute)."""
    query = StructuredQuery(
        from_table="customers",
        select=[ScalarFunctionSelectItem(fn="lower", args=[{"col": "customers.email"}])],
        limit=10,
    )
    shape = normalize_query_shape(query)
    serialized = json.dumps(shape)
    assert "customers.email" in serialized
    assert '"kind": "scalar_fn"' in serialized


def test_normalized_query_shape_handles_case_select_item():
    """Same pre-existing crash-on-execute gap as the scalar_fn case above."""
    query = StructuredQuery(
        from_table="customers",
        select=[
            CaseSelectItem(
                when=[
                    {
                        "when": {"col": "customers.email", "op": "eq", "value": "secret@x.com"},
                        "then": {"literal": "redacted"},
                    }
                ],
                else_={"literal": "visible"},
                alias="label",
            )
        ],
        limit=10,
    )
    shape = normalize_query_shape(query)
    serialized = json.dumps(shape)
    assert "customers.email" in serialized
    assert '"kind": "case"' in serialized
    assert "secret@x.com" not in serialized
    assert "redacted" not in serialized


def test_normalized_query_shape_handles_predicate_col_fn_in_having():
    """Predicate.col_fn (item 77) previously fell through _predicate_shape
    as {"column": None, ...}, silently losing which column a HAVING clause
    actually filtered on."""
    query = StructuredQuery(
        from_table="orders",
        select=["orders.status", {"fn": "count", "col": "*", "as": "n"}],
        group_by=["orders.status"],
        having=Predicate(
            col_fn={"fn": "coalesce", "args": [{"col": "orders.total_amount"}, {"literal": 0}]},
            op="gt",
            value=0,
        ),
        limit=10,
    )
    shape = normalize_query_shape(query)
    serialized = json.dumps(shape)
    assert "orders.total_amount" in serialized
    assert '"function": "coalesce"' in serialized


def test_searched_having_and_case_shapes_carry_structure_but_no_literals():
    """item 99: HAVING and a CASE `when` are now WhereNode trees. The audit shape
    must record their boolean structure and columns while leaking no predicate
    literal — the redaction rule the new positions inherit from `where`."""
    query = StructuredQuery(
        from_table="orders",
        select=[
            "orders.status",
            {"fn": "count", "col": "*", "as": "n"},
            {
                "when": [
                    {
                        "when": {
                            "not": {
                                "col": "orders.status",
                                "op": "eq",
                                "value": "case-secret-literal",
                            }
                        },
                        "then": {"literal": "case-then-literal"},
                    }
                ],
                "as": "label",
            },
        ],
        group_by=["orders.status"],
        having={
            "or": [
                {"col": "n", "op": "gt", "value": 4242},
                {"col": "orders.status", "op": "eq", "value": "having-secret-literal"},
            ]
        },
        limit=10,
    )
    serialized = json.dumps(normalize_query_shape(query))

    # Structure is preserved: the OR group and its operators are recorded.
    assert '"or"' in serialized
    assert '"operator": "gt"' in serialized
    # No predicate literal from any of the new positions reaches the event.
    assert "having-secret-literal" not in serialized
    assert "case-secret-literal" not in serialized
    assert "4242" not in serialized


# These cover the redaction contract (non-negotiable 3), so they belong in the
# tier the pre-commit gate runs. Without this the whole file is deselected by
# `-m unit` and only the full-suite run reaches it. The same is true of most of
# tests/unit/ — tracked as item 124.
pytestmark = pytest.mark.unit


_SCALAR_EMPLOYEE_SUBQUERY = {
    "from": "employees",
    "select": [{"fn": "count", "col": "employees.id", "as": "n"}],
}


def test_the_audit_shape_records_a_nested_in_subquery_and_leaks_no_literal():
    """item 120: a `value_subquery` had no branch in `_predicate_shape`, so
    `WHERE customer_id IN (SELECT ... FROM employees JOIN departments)` audited as a
    bare `{"operator": "in", "column": ...}` — an event naming `orders` alone, for a
    query that read three tables. The nested scope's own from/joins/filter structure
    is now recorded on the same terms a set-op arm's (item 104) and a cte body's
    (item 105) are, and its literals are redacted exactly as the outer query's."""
    query = StructuredQuery.model_validate(
        {
            "from": "orders",
            "select": ["orders.id"],
            "where": {
                "col": "orders.customer_id",
                "op": "in",
                "value_subquery": {
                    "from": "employees",
                    "select": ["employees.customer_id"],
                    "joins": [
                        {"table": "departments", "on": ["employees.dept_id", "departments.id"]}
                    ],
                    "where": {
                        "and": [
                            {"col": "employees.ssn", "op": "eq", "value": "SUBQUERY-SECRET-SSN"},
                            {"col": "departments.name", "op": "like", "value": "SUBQUERY-SECRET-%"},
                        ]
                    },
                },
            },
        }
    )
    shape = normalize_query_shape(query)
    serialized = json.dumps(shape)

    nested = shape["where"]["value_subquery"]
    assert shape["where"]["operator"] == "in"
    assert nested["from"] == "employees"
    assert [join["table"] for join in nested["joins"]] == ["departments"]
    assert nested["select"] == [{"kind": "column", "column": "employees.customer_id"}]
    # The nested boolean structure, not just its tables.
    assert nested["where"] == {
        "and": [
            {"operator": "eq", "column": "employees.ssn"},
            {"operator": "like", "column": "departments.name"},
        ]
    }
    assert "SUBQUERY-SECRET" not in serialized


def test_no_literal_escapes_a_nested_subquery_at_any_depth_or_position():
    """The redaction guarantee has to hold for every scope the new recursion
    reaches, not just the first one: a literal two levels down, one inside a nested
    CASE, and one in a nested set-op arm all travel the same walk now."""
    query = StructuredQuery.model_validate(
        {
            "from": "orders",
            "select": ["orders.id"],
            "where": {
                "col": "orders.customer_id",
                "op": "in",
                "value_subquery": {
                    "from": "customers",
                    "select": ["customers.id"],
                    "where": {
                        "not": {
                            "col": "customers.region_id",
                            "op": "not_in",
                            "value_subquery": {
                                "from": "regions",
                                "select": [
                                    {
                                        # Deliberately the EXPRESSION spelling of a
                                        # searched CASE, not `CaseSelectItem`. Only this
                                        # one routes its conditions through
                                        # `_expression_shape`; the select-item spelling
                                        # records `branch_count` alone, so a redaction
                                        # assertion written against it passes because the
                                        # subtree is discarded rather than redacted —
                                        # a vacuous test. See TODO.md item 123.
                                        "expr": {
                                            "when": [
                                                {
                                                    "when": {
                                                        "col": "regions.code",
                                                        "op": "eq",
                                                        "value": "DEPTH-2-CASE-SECRET",
                                                    },
                                                    "then": {"literal": "DEPTH-2-THEN-SECRET"},
                                                }
                                            ]
                                        },
                                        "as": "id",
                                    }
                                ],
                                "where": {
                                    "col": "regions.name",
                                    "op": "eq",
                                    "value": "DEPTH-2-WHERE-SECRET",
                                },
                                "set_op": {
                                    "op": "union",
                                    "arms": [
                                        {
                                            "from": "archived_regions",
                                            "select": ["archived_regions.id"],
                                            "where": {
                                                "col": "archived_regions.name",
                                                "op": "eq",
                                                "value": "DEPTH-2-ARM-SECRET",
                                            },
                                        }
                                    ],
                                },
                            },
                        }
                    },
                },
            },
        }
    )
    serialized = json.dumps(normalize_query_shape(query))

    for needle in (
        "DEPTH-2-CASE-SECRET",
        "DEPTH-2-THEN-SECRET",
        "DEPTH-2-WHERE-SECRET",
        "DEPTH-2-ARM-SECRET",
    ):
        assert needle not in serialized, f"{needle} leaked into the audited query shape"
    # Withholding the values must not degrade into withholding the fact that those
    # scopes existed: every table the query reads is still named.
    for table in ("orders", "customers", "regions", "archived_regions"):
        assert f'"{table}"' in serialized, f"{table} is missing from the audited query shape"
    # The literals above must be absent because the walk REDACTED them, not because
    # it never reached them. Pin that the CASE condition was actually walked, so this
    # test cannot silently go vacuous if the select-item shape changes.
    assert '"conditions": [{"operator": "eq", "column": "regions.code"}]' in serialized


def test_where_shape_records_a_not_group_rather_than_an_empty_or():
    """Regression: `_where_shape` used to fall through a `not` group to
    `{"or": []}`, silently misreporting the predicate shape."""
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        where={"not": {"col": "orders.status", "op": "eq", "value": "x"}},
    )
    assert normalize_query_shape(query)["where"] == {
        "not": {"operator": "eq", "column": "orders.status"}
    }


def test_jsonl_sink_appends_versioned_events_with_private_file_mode(tmp_path):
    path = tmp_path / "audit" / "events.jsonl"
    sink = JsonlAuditSink(str(path), fsync=True)
    for connection_id in ("first", "second"):
        sink.emit(
            AuditEvent(
                connection_id=connection_id,
                policy_decision="allowed",
                outcome="success",
                query_shape={"from": "customers"},
                duration_ms=3,
            )
        )

    lines = path.read_text().splitlines()
    assert [json.loads(line)["connection_id"] for line in lines] == ["first", "second"]
    assert all(json.loads(line)["schema_version"] == "1" for line in lines)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_audit_connection_probe_persists_redacted_event(tmp_path):
    path = tmp_path / "events.jsonl"
    set_audit_sink(JsonlAuditSink(str(path)))
    audit_connection_probe(
        connection_id="demo",
        outcome="success",
        principal="agent-1",
        principal_scopes=["admin:connections:test"],
        auth_method="api_key",
        probe_healthy=False,
        failure_category="unreachable",
        latency_ms=12.5,
        duration_ms=42,
    )

    lines = path.read_text().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event_type"] == "connection.probe"
    assert event["schema_version"] == "1"
    assert event["connection_id"] == "demo"
    assert event["outcome"] == "success"
    assert event["probe_healthy"] is False
    assert event["failure_category"] == "unreachable"
    assert event["latency_ms"] == 12.5
    text = path.read_text()
    for leak in ("password", "connection_string", "hunter2"):
        assert leak not in text


def test_audit_connection_probe_rejected_carries_error_category_not_exception(tmp_path):
    path = tmp_path / "events.jsonl"
    set_audit_sink(JsonlAuditSink(str(path)))
    audit_connection_probe(
        connection_id="demo",
        outcome="rejected",
        principal="agent-1",
        principal_scopes=[],
        auth_method="api_key",
        error_category="rate_limited",
    )

    event = json.loads(path.read_text().splitlines()[0])
    assert event["outcome"] == "rejected"
    assert event["error_category"] == "rate_limited"
    # Fields left unset (exclude_none) rather than serialized as null.
    assert "probe_healthy" not in event


@pytest.mark.asyncio
async def test_service_persists_redacted_event_with_identity_surface_and_request_id(tmp_path):
    path = tmp_path / "events.jsonl"
    set_audit_sink(JsonlAuditSink(str(path)))
    table = sa.Table(
        "customers",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(200)),
    )
    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = [
        {"id": 1, "email": "returned@example.com"}
    ]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    token = context_logger.set(ContextLogger(request_id="req-audit-123"))
    try:
        with (
            patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
            patch.object(svc, "session_scope", _scope),
        ):
            service = StructuredQueryService(
                connection_id="demo",
                principal=Principal(
                    subject="agent-a",
                    scopes=frozenset({"read:customers"}),
                    auth_method="api_key",
                ),
                surface="rest",
            )
            await service.execute(_query_with_sensitive_literals())
    finally:
        context_logger.reset(token)

    raw = path.read_text()
    event = json.loads(raw)
    assert event["correlation_id"] == "req-audit-123"
    assert event["surface"] == "rest"
    assert event["principal_id"] == "agent-a"
    assert event["auth_method"] == "api_key"
    assert event["principal_scopes"] == ["read:customers"]
    assert event["policy_decision"] == "allowed"
    assert event["outcome"] == "success"
    assert event["row_count"] == 1
    assert event["response_bytes"] > 0
    assert "secret@example.com" not in raw
    assert "returned@example.com" not in raw
    assert "executive request" not in raw
    assert "sql" not in event
    assert "params" not in event
    assert event["admission_id"]
    assert event["queue_wait_ms"] is not None
    assert event["admission_state"] == "completed"


def test_sink_failure_is_logged_but_does_not_raise():
    class FailingSink:
        def emit(self, event):
            raise OSError("disk full")

        def close(self):
            return None

    set_audit_sink(FailingSink())

    audit_query(
        connection_id="demo",
        sql="SELECT 1",
        query_shape={"from": "customers"},
        duration_ms=1,
    )


@pytest.mark.asyncio
async def test_delegated_request_audits_both_human_and_agent(tmp_path):
    # TODO.md item 90: an on-behalf-of query must record the human
    # (`principal_id`) *and* the agent chain (`actor_id`/`delegation_chain`) —
    # "Agent A on behalf of User Z" — while staying redaction-safe.
    from querygate.core.auth import Actor

    path = tmp_path / "delegated.jsonl"
    set_audit_sink(JsonlAuditSink(str(path)))
    table = sa.Table(
        "customers",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(200)),
    )
    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = [{"id": 1, "email": "x@example.com"}]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
    ):
        service = StructuredQueryService(
            connection_id="demo",
            principal=Principal(
                subject="user-human",
                auth_method="jwt",
                actor=Actor(subject="agent-app", delegated_by=Actor(subject="service-s")),
            ),
            surface="mcp",
        )
        await service.execute(_query_with_sensitive_literals())

    event = json.loads(path.read_text())
    assert event["principal_id"] == "user-human"  # the human whose policy applied
    assert event["actor_id"] == "agent-app"  # the immediate agent
    assert event["delegation_chain"] == ["agent-app", "service-s"]
    assert event["outcome"] == "success"


@pytest.mark.asyncio
async def test_non_delegated_request_has_no_actor_fields(tmp_path):
    path = tmp_path / "direct.jsonl"
    set_audit_sink(JsonlAuditSink(str(path)))
    audit_query(
        connection_id="demo",
        sql="SELECT 1",
        query_shape={"from": "customers"},
        duration_ms=1,
        principal="agent-a",
    )
    event = json.loads(path.read_text())
    assert event["principal_id"] == "agent-a"
    # The JSONL sink serializes with exclude_none=True, so a non-delegated
    # request carries no actor_id at all (never a misleading empty attribution).
    assert "actor_id" not in event
    assert event["delegation_chain"] == []


@pytest.mark.asyncio
async def test_rejected_event_has_category_without_exception_or_literals(tmp_path):
    path = tmp_path / "rejected.jsonl"
    set_audit_sink(JsonlAuditSink(str(path)))
    query = _query_with_sensitive_literals()
    with patch.object(svc, "validate_schema", AsyncMock(side_effect=ValueError("secret failure"))):
        service = StructuredQueryService(connection_id="demo", surface="mcp")
        with pytest.raises(ValueError, match="secret failure"):
            await service.execute(query)

    raw = path.read_text()
    event = json.loads(raw)
    assert event["surface"] == "mcp"
    assert event["outcome"] == "rejected"
    assert event["policy_decision"] == "denied"
    assert event["error_category"] == "schema"
    assert "secret@example.com" not in raw
    assert "secret failure" not in raw


@pytest.mark.asyncio
async def test_persisted_event_names_the_subquerys_tables_without_its_literals(tmp_path):
    """The item-120 criterion is about the PERSISTED event, not just the
    normalizer: the JSONL line an investigator actually reads must name every
    table the attempt would have read — `orders` alone was the bug — while
    carrying no literal from the nested scope."""
    path = tmp_path / "subquery.jsonl"
    set_audit_sink(JsonlAuditSink(str(path)))
    query = StructuredQuery.model_validate(
        {
            "from": "orders",
            "select": ["orders.id"],
            "where": {
                "col": "orders.customer_id",
                "op": "in",
                "value_subquery": {
                    "from": "employees",
                    "select": ["employees.customer_id"],
                    "where": {"col": "employees.ssn", "op": "eq", "value": "PERSISTED-SECRET"},
                },
            },
        }
    )
    with patch.object(svc, "validate_schema", AsyncMock(side_effect=ValueError("stop here"))):
        service = StructuredQueryService(connection_id="demo")
        with pytest.raises(ValueError, match="stop here"):
            await service.execute(query)

    raw = path.read_text()
    event = json.loads(raw)
    assert event["query_shape"]["where"]["value_subquery"]["from"] == "employees"
    assert "employees.ssn" in raw
    assert "PERSISTED-SECRET" not in raw


@pytest.mark.asyncio
async def test_a_policy_rejected_nested_subquery_is_still_shaped_in_the_event(tmp_path):
    """Item 120's claim is that normalization runs BEFORE validation, so a
    rejected attempt stays auditable. The sibling test above proves that for a
    SCHEMA rejection; this one pins the POLICY stage, which is the earlier of the
    two and the one an operator most wants a record of. Moving
    `normalize_query_shape` below `validate_policy` would silently void the claim
    while every other test stayed green."""
    path = tmp_path / "policy-rejected.jsonl"
    set_audit_sink(JsonlAuditSink(str(path)))
    # depth 0 admits no subquery at all, so the nested scope is refused by policy.
    set_policy_store(PolicyStore(default=Policy(max_subquery_depth=0), overrides={}))
    query = StructuredQuery.model_validate(
        {
            "from": "orders",
            "select": ["orders.id"],
            "where": {
                "col": "orders.customer_id",
                "op": "in",
                "value_subquery": {
                    "from": "employees",
                    "select": ["employees.customer_id"],
                    "where": {"col": "employees.ssn", "op": "eq", "value": "REJECTED-SECRET"},
                },
            },
        }
    )
    service = StructuredQueryService(connection_id="demo")
    with pytest.raises(PolicyViolationError):
        await service.execute(query)

    raw = path.read_text()
    event = json.loads(raw)
    assert event["query_shape"]["where"]["value_subquery"]["from"] == "employees"
    assert "REJECTED-SECRET" not in raw


def test_the_nested_scope_is_recorded_in_every_predicate_position():
    """The recursion lives in `_predicate_shape`, so it reaches HAVING and join
    conditions for free. That is exactly why it must be pinned: relocating the
    branch up into `normalize_query_shape`'s `where` handling would keep the
    WHERE test green while silently dropping the other two positions."""
    having = StructuredQuery.model_validate(
        {
            "from": "orders",
            "select": [{"fn": "count", "col": "orders.id", "as": "cnt"}],
            "group_by": ["orders.customer_id"],
            "having": {"col": "cnt", "op": "gt", "value_subquery": _SCALAR_EMPLOYEE_SUBQUERY},
        }
    )
    assert normalize_query_shape(having)["having"]["value_subquery"]["from"] == "employees"

    joined = StructuredQuery.model_validate(
        {
            "from": "orders",
            "select": ["orders.id"],
            "joins": [
                {
                    "table": "customers",
                    "condition": {
                        "col": "orders.customer_id",
                        "op": "in",
                        "value_subquery": _SCALAR_EMPLOYEE_SUBQUERY,
                    },
                }
            ],
        }
    )
    join_shape = normalize_query_shape(joined)["joins"][0]
    assert join_shape["condition"]["value_subquery"]["from"] == "employees"


@pytest.mark.asyncio
async def test_capacity_timeout_event_carries_admission_fields(tmp_path):
    path = tmp_path / "capacity.jsonl"
    set_audit_sink(JsonlAuditSink(str(path)))
    set_policy_store(
        PolicyStore(default=Policy(max_concurrency=1, concurrency_wait_seconds=5), overrides={})
    )
    await cc.in_process_limiter().semaphore("demo", 1).acquire()  # occupy the only slot

    query = _query_with_sensitive_literals()
    service = StructuredQueryService(connection_id="demo")
    with pytest.raises(ValueError, match="too many concurrent"):
        await service.execute(query, queue_mode=QueueMode.FAIL_FAST)

    raw = path.read_text()
    event = json.loads(raw)
    assert event["outcome"] == "rejected"
    assert event["error_category"] == "concurrency"
    assert event["admission_id"]
    assert event["queue_wait_ms"] is not None
    assert event["admission_state"] == "capacity_timeout"
    assert "secret@example.com" not in raw


@pytest.mark.asyncio
async def test_app_lifecycle_configures_and_resets_jsonl_sink(tmp_path):
    path = tmp_path / "lifecycle.jsonl"
    app = create_app(
        AppConfig(
            environment="localhost",
            mcp_enabled=False,
            concurrency_backend="in_process",
            audit_sink_backend="jsonl",
            audit_jsonl_path=str(path),
        )
    )
    with patch("querygate.health._ping", new_callable=AsyncMock):
        async with app.router.lifespan_context(app):
            assert isinstance(get_audit_sink(), JsonlAuditSink)
            audit_query(
                connection_id="demo",
                sql="SELECT 1",
                query_shape={"from": "customers"},
                duration_ms=1,
            )

    assert path.exists()
    assert isinstance(get_audit_sink(), NullAuditSink)


def test_normalized_query_shape_handles_window_select_item():
    """item 101: a window's audited shape names the function, the columns it
    touched, and the frame's SHAPE — never a frame offset, a lag distance, or a
    literal, which are caller values (non-negotiable 3)."""
    query = StructuredQuery.model_validate(
        {
            "from": "orders",
            "select": [
                "orders.id",
                {
                    "fn": "sum",
                    "arg": {
                        "op": "*",
                        "left": {"col": "orders.total_amount"},
                        "right": {"literal": 1.07},
                    },
                    "over": {
                        "partition_by": ["orders.status"],
                        "order_by": [{"col": "orders.created_at"}],
                        "frame": {
                            "mode": "rows",
                            "start": {"bound": "preceding", "offset": 6},
                            "end": {"bound": "current_row"},
                        },
                    },
                    "as": "trailing_total",
                },
            ],
            "limit": 10,
        }
    )
    shape = normalize_query_shape(query)
    serialized = json.dumps(shape)
    assert '"kind": "window"' in serialized
    assert '"function": "sum"' in serialized
    assert "orders.total_amount" in serialized
    assert "orders.status" in serialized
    assert "orders.created_at" in serialized
    assert '"mode": "rows"' in serialized
    assert '"start": "preceding"' in serialized
    # No values: not the arithmetic literal, and not the frame's row distance.
    assert "1.07" not in serialized
    assert "6" not in serialized.replace('"limit": 10', "")


def test_normalized_query_shape_covers_every_select_item_type():
    """`_select_shape` raises on an unknown select item, so a new AST select-item
    type would crash the audit path (which runs on EVERY request) until taught
    here. Pinned by construction rather than by remembering."""
    import typing

    from querygate.query_ast.models import SelectItem

    payloads = {
        "str": "orders.id",
        "AggregateSelectItem": {"fn": "count", "col": "*"},
        "DateBucketSelectItem": {"col": "orders.created_at", "granularity": "month"},
        "StringAggSelectItem": {"col": "orders.status", "delimiter": ","},
        "ArrayAggSelectItem": {"col": "orders.status"},
        "PercentileContSelectItem": {"col": "orders.total_amount", "fraction": 0.5},
        "ScalarFunctionSelectItem": {"fn": "upper", "args": [{"col": "orders.status"}]},
        "CaseSelectItem": {
            "when": [
                {"when": {"col": "orders.id", "op": "eq", "value": 1}, "then": {"literal": 1}}
            ],
            "as": "k",
        },
        "ExpressionSelectItem": {"expr": {"col": "orders.id"}, "as": "e"},
        "WindowSelectItem": {
            "fn": "row_number",
            "over": {"order_by": [{"col": "orders.id"}]},
            "as": "rn",
        },
    }
    members = {m.__name__ if m is not str else "str" for m in typing.get_args(SelectItem)}
    assert members == set(payloads)
    for name, payload in payloads.items():
        query = StructuredQuery.model_validate({"from": "orders", "select": [payload]})
        assert normalize_query_shape(query)["select"], name
