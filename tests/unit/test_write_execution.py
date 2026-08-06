"""Governed-writes gated execution unit checks (TODO.md item 93 Phase 2a).

Focused on the two pieces the real-DB end-to-end test can't isolate cheaply: the
write approval gate's fingerprint binding (a token can't be replayed onto a
different write) and the temporal value coercion the compiler applies so a JSON
write value binds to a typed column.
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

from querygate.audit.sinks import reset_audit_sink, set_audit_sink
from querygate.compiler.write_compiler import _coerce_write_value
from querygate.core.auth import Principal
from querygate.core.exceptions import ApprovalRequiredError, PolicyViolationError
from querygate.execution import write_execution as wx
from querygate.execution.approval import issue_approval_token, write_fingerprint
from querygate.execution.write_execution import WriteExecutionService
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy, WritePolicy
from querygate.write_ast.models import DeleteStatement, WritePredicate

pytestmark = pytest.mark.unit

_KEY = "write-approval-key"


class _CapturingSink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)

    def close(self):
        pass


def _delete(value: int) -> DeleteStatement:
    # WritePredicate, not the read Predicate: since item 114 a write's filter has
    # its own narrowed type. The wire payload ({col, op, value}) is identical.
    return DeleteStatement(
        table="orders", where=WritePredicate(col="orders.id", op="eq", value=value)
    )


def _wp(require_approval_over_rows=0) -> WritePolicy:
    return WritePolicy(
        enabled=True,
        allowed_tables=["orders"],
        allowed_operations=["delete"],
        require_approval_over_rows=require_approval_over_rows,
    )


def test_write_approval_gate_admits_only_its_own_write(monkeypatch):
    monkeypatch.setattr(wx.app_config, "approval_token_hmac_key", _KEY)
    approved = _delete(1)
    other = _delete(2)
    token = issue_approval_token(
        fingerprint=write_fingerprint(approved),
        approver_subject="human",
        key=_KEY,
        connection_id=None,
        principal_subject=None,
    )
    svc = WriteExecutionService("demo")
    # The token is bound to `approved` — admits it (no raise)...
    svc._enforce_write_approval_gate(approved, 5, _wp(), token)
    # ...and cannot be replayed onto a different write.
    with pytest.raises(ApprovalRequiredError):
        svc._enforce_write_approval_gate(other, 5, _wp(), token)


def test_write_approval_gate_raises_without_a_token(monkeypatch):
    monkeypatch.setattr(wx.app_config, "approval_token_hmac_key", _KEY)
    with pytest.raises(ApprovalRequiredError) as ei:
        WriteExecutionService("demo")._enforce_write_approval_gate(_delete(1), 5, _wp(), None)
    assert ei.value.fingerprint == write_fingerprint(_delete(1))
    assert ei.value.reasons


def test_write_approval_gate_noop_under_threshold_or_disabled(monkeypatch):
    monkeypatch.setattr(wx.app_config, "approval_token_hmac_key", _KEY)
    svc = WriteExecutionService("demo")
    # Affected within the threshold -> no approval needed.
    svc._enforce_write_approval_gate(_delete(1), 5, _wp(require_approval_over_rows=10), None)
    # Threshold None -> gate off entirely, even for a huge write.
    svc._enforce_write_approval_gate(_delete(1), 10_000, _wp(require_approval_over_rows=None), None)


def test_write_approval_gate_rejects_a_token_minted_for_a_different_connection(monkeypatch):
    """TODO.md item 151, write-side sibling of the read gate's connection-
    binding test: a write approval minted for connection A must not admit the
    byte-identical write on connection B."""
    monkeypatch.setattr(wx.app_config, "approval_token_hmac_key", _KEY)
    write = _delete(1)
    token = issue_approval_token(
        fingerprint=write_fingerprint(write),
        approver_subject="human",
        key=_KEY,
        connection_id="staging",
        principal_subject=None,
    )
    with pytest.raises(ApprovalRequiredError):
        WriteExecutionService("prod")._enforce_write_approval_gate(write, 5, _wp(), token)
    # ...but it does admit on the connection it was actually minted for.
    WriteExecutionService("staging")._enforce_write_approval_gate(write, 5, _wp(), token)


def test_write_approval_gate_rejects_a_token_minted_for_a_different_principal(monkeypatch):
    """The principal-binding sibling: a write approval bound to principal X at
    issue time must not admit principal Y's identical retry."""
    monkeypatch.setattr(wx.app_config, "approval_token_hmac_key", _KEY)
    write = _delete(1)
    token = issue_approval_token(
        fingerprint=write_fingerprint(write),
        approver_subject="alice",
        key=_KEY,
        connection_id="demo",
        principal_subject="alice",
    )
    svc_as_bob = WriteExecutionService("demo", principal=Principal(subject="bob"))
    with pytest.raises(ApprovalRequiredError):
        svc_as_bob._enforce_write_approval_gate(write, 5, _wp(), token)
    # ...but it does admit when redeemed by the principal it was bound to.
    svc_as_alice = WriteExecutionService("demo", principal=Principal(subject="alice"))
    svc_as_alice._enforce_write_approval_gate(write, 5, _wp(), token)


@pytest.mark.asyncio
async def test_denied_by_default_write_is_rejected_and_audited():
    # A write to a connection with writes off is rejected — and the *attempt*
    # is audited (a denied write must be visible in the trail, like reads).
    set_policy_store(PolicyStore(default=Policy(write=WritePolicy(enabled=False)), overrides={}))
    sink = _CapturingSink()
    set_audit_sink(sink)
    try:
        with pytest.raises(PolicyViolationError):
            await WriteExecutionService("demo").execute(_delete(1))
    finally:
        reset_audit_sink()
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.operation == "execute_structured_write"
    assert event.outcome == "rejected"
    # Redaction: the audit shape carries the op/table, never a predicate value.
    assert "1" not in str(event.query_shape.get("columns", []))


def test_coerce_temporal_string_to_python_object():
    col = sa.Column("created_at", sa.DateTime)
    assert _coerce_write_value(col, "2026-01-01T09:30:00") == dt.datetime(2026, 1, 1, 9, 30, 0)
    date_col = sa.Column("d", sa.Date)
    assert _coerce_write_value(date_col, "2026-01-01") == dt.date(2026, 1, 1)


def test_coerce_leaves_non_temporal_and_bad_values_untouched():
    dt_col = sa.Column("created_at", sa.DateTime)
    # A non-ISO string for a temporal column is passed through (the DB surfaces a
    # clean error rather than the compiler guessing).
    assert _coerce_write_value(dt_col, "not-a-timestamp") == "not-a-timestamp"
    # Non-temporal columns bind their JSON value directly.
    assert _coerce_write_value(sa.Column("n", sa.Integer), "5") == "5"
    assert _coerce_write_value(sa.Column("s", sa.String), "hello") == "hello"
    # A non-string value is never touched.
    assert _coerce_write_value(dt_col, 12345) == 12345


def test_upsert_compiles_on_conflict_for_postgres_and_rejects_mssql():
    from sqlalchemy.dialects import postgresql

    from querygate.compiler.write_compiler import compile_write
    from querygate.core.exceptions import QueryValidationError
    from querygate.write_ast.models import UpsertStatement

    table = sa.Table(
        "orders",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("status", sa.String(20)),
    )
    stmt = UpsertStatement(
        table="orders",
        rows=[{"id": 1, "status": "x"}],
        conflict_columns=["id"],
        update_columns=["status"],
    )
    dml = compile_write(stmt, table, "postgresql")
    rendered = str(dml.compile(dialect=postgresql.dialect())).upper()
    assert "ON CONFLICT" in rendered and "DO UPDATE" in rendered

    # MSSQL has no ON CONFLICT — reject, don't emulate.
    with pytest.raises(QueryValidationError, match="no ON CONFLICT clause"):
        compile_write(stmt, table, "mssql")

    # MySQL has ON DUPLICATE KEY UPDATE but it fires on ANY unique/PK
    # collision, not a caller-named conflict target — reject with the
    # dialect-specific message, not the generic "no ON CONFLICT" one (which
    # would be factually wrong for MySQL).
    with pytest.raises(QueryValidationError, match="ON DUPLICATE KEY UPDATE"):
        compile_write(stmt, table, "mysql")

    # Snowflake has a real upsert idiom (MERGE), but it's a multi-clause
    # statement with no single-target-constraint model the way
    # conflict_columns/update_columns express one — reject rather than
    # synthesize a MERGE the AST never asked for (TODO.md item 19 phase 2).
    with pytest.raises(QueryValidationError, match="MERGE"):
        compile_write(stmt, table, "snowflake")


def test_upsert_statement_rejects_update_column_that_is_a_conflict_column():
    from querygate.write_ast.models import UpsertStatement

    with pytest.raises(Exception):
        UpsertStatement(
            table="orders",
            rows=[{"id": 1, "status": "x"}],
            conflict_columns=["id"],
            update_columns=["id"],  # cannot update the conflict key
        )
