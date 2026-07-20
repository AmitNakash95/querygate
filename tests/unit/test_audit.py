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
from querygate.core.logging import ContextLogger, context_logger
from querygate.execution import concurrency as cc
from querygate.execution import service as svc
from querygate.execution.admission import QueueMode
from querygate.execution.service import StructuredQueryService
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.query_ast.models import Predicate, StructuredQuery


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
async def test_capacity_timeout_event_carries_admission_fields(tmp_path):
    path = tmp_path / "capacity.jsonl"
    set_audit_sink(JsonlAuditSink(str(path)))
    set_policy_store(
        PolicyStore(default=Policy(max_concurrency=1, concurrency_wait_seconds=5), overrides={})
    )
    cc.SEMAPHORES["demo"] = asyncio.Semaphore(1)
    await cc.SEMAPHORES["demo"].acquire()  # occupy the only slot

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
