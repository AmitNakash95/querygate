"""Adversarial write boundary suite (TODO.md item 93, governed writes).

Runs under `make test-security`. Proves the *structural* write guarantees hold
under attack — the class of catastrophic write the whole market retreated from
read-only to avoid. Every check here is enforced before a write is ever
compiled or reaches a database, so an injected or hostile write can only ever
*propose* a validated structure that is then re-validated and capped.

Execution-time guarantees that genuinely need a database (the in-transaction row
cap rolls back, no partial write) are proved against real SQLite in
`tests/integration/test_write_execution_end_to_end.py`, dual-marked `security`.
"""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa

from querygate.compiler.write_compiler import compile_write
from querygate.core.exceptions import ApprovalRequiredError, PolicyViolationError
from querygate.execution.approval import issue_approval_token, write_fingerprint
from querygate.execution.write_execution import WriteExecutionService
from querygate.policy.models import Policy, WritePolicy
from querygate.query_ast.models import Predicate
from querygate.write_ast.models import (
    DeleteStatement,
    InsertStatement,
    UpdateStatement,
    UpsertStatement,
)

pytestmark = pytest.mark.security

_ATTACK = "x'; DROP TABLE orders; --"
_RAW_DML_FIELDS = {"sql", "raw_sql", "execute_sql", "dml", "raw", "query", "statement_text"}


def _all_property_keys(schema) -> set:
    """Every property name anywhere in a JSON schema (walks nested/union defs)."""
    keys: set = set()
    if isinstance(schema, dict):
        for name, sub in schema.get("properties", {}).items():
            keys.add(name)
            keys |= _all_property_keys(sub)
        for key, sub in schema.items():
            if key != "properties":
                keys |= _all_property_keys(sub)
    elif isinstance(schema, list):
        for item in schema:
            keys |= _all_property_keys(item)
    return keys


def _orders_table() -> sa.Table:
    return sa.Table(
        "orders",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("customer_id", sa.Integer),
        sa.Column("status", sa.String(20)),
        sa.Column("created_at", sa.DateTime),
    )


def _writable(*, policy_kwargs=None, **write_overrides) -> Policy:
    wp = dict(
        enabled=True,
        allowed_tables=["orders"],
        allowed_operations=["insert", "update", "delete"],
    )
    wp.update(write_overrides)
    return Policy(write=WritePolicy(**wp), **(policy_kwargs or {}))


# --------------------------------------------------------------------------- #
# No raw DML — structurally impossible to express                              #
# --------------------------------------------------------------------------- #


def test_write_statement_json_schemas_expose_no_raw_dml_field_and_forbid_extras():
    # Schema-level sibling of test_credential_redaction: the live write-statement
    # schemas (what REST OpenAPI is generated from) expose no raw-SQL/DML property
    # and forbid smuggled fields — the no-raw-DML invariant, asserted structurally.
    for cls in (InsertStatement, UpdateStatement, DeleteStatement, UpsertStatement):
        schema = cls.model_json_schema()
        props = _all_property_keys(schema)
        assert not (_RAW_DML_FIELDS & props), f"{cls.__name__} exposes a raw-DML field: {props}"
        assert schema.get("additionalProperties") is False  # extra="forbid"


def test_mcp_write_tool_schema_exposes_no_raw_dml_field():
    import querygate.mcp.tools.write  # noqa: F401 — ensure the tool is registered
    from querygate.mcp.server import create_mcp_server

    server = create_mcp_server()
    tool = server._tool_manager._tools["run_structured_writes"]
    schema = json.loads(json.dumps(tool.parameters))  # plain dict
    assert not (_RAW_DML_FIELDS & _all_property_keys(schema))


@pytest.mark.parametrize("field", ["sql", "raw_sql", "execute_sql", "query", "dml"])
def test_no_raw_dml_field_on_any_write_statement(field):
    # extra="forbid" — a smuggled raw-SQL field is rejected at parse time, so a
    # raw-DML channel cannot be added by a caller's payload.
    for cls, base in (
        (InsertStatement, {"table": "orders", "rows": [{"id": 1}]}),
        (
            UpdateStatement,
            {"table": "orders", "set": {"status": "x"}, "where": _eq()},
        ),
        (DeleteStatement, {"table": "orders", "where": _eq()}),
    ):
        with pytest.raises(Exception):
            cls(**{**base, field: "DELETE FROM orders"})


def _eq() -> Predicate:
    return Predicate(col="orders.id", op="eq", value=1)


def test_unqualified_update_and_delete_cannot_be_constructed():
    # The AST makes an unqualified UPDATE/DELETE inexpressible — `where` is a
    # required field, so there is no "forgot the WHERE" catastrophe to guard.
    with pytest.raises(Exception):
        UpdateStatement(table="orders", set={"status": "x"})  # no where
    with pytest.raises(Exception):
        DeleteStatement(table="orders")  # no where


# --------------------------------------------------------------------------- #
# Injected values are bound data, never executable SQL                          #
# --------------------------------------------------------------------------- #


def test_insert_value_is_bound_data_not_sql():
    stmt = compile_write(
        InsertStatement(table="orders", rows=[{"id": 1, "status": _ATTACK}]), _orders_table()
    )
    compiled = stmt.compile()
    assert _ATTACK not in str(compiled)
    assert _ATTACK in compiled.params.values()


def test_update_set_value_is_bound_data_not_sql():
    stmt = compile_write(
        UpdateStatement(table="orders", set={"status": _ATTACK}, where=_eq()), _orders_table()
    )
    compiled = stmt.compile()
    assert _ATTACK not in str(compiled)
    assert _ATTACK in compiled.params.values()


def test_delete_where_value_is_bound_data_not_sql():
    stmt = compile_write(
        DeleteStatement(
            table="orders", where=Predicate(col="orders.status", op="eq", value=_ATTACK)
        ),
        _orders_table(),
    )
    compiled = stmt.compile()
    assert _ATTACK not in str(compiled)
    assert _ATTACK in compiled.params.values()


# --------------------------------------------------------------------------- #
# Deny-by-default and the policy allow/deny axes                                #
# --------------------------------------------------------------------------- #


def test_writes_denied_by_default():
    # A default policy has WritePolicy.enabled=False — every op is refused.
    from querygate.validation.write_policy_validation import validate_write_policy

    with pytest.raises(PolicyViolationError):
        validate_write_policy(DeleteStatement(table="orders", where=_eq()), Policy(), "demo")


def test_operation_not_allowed_is_rejected():
    from querygate.validation.write_policy_validation import validate_write_policy

    policy = _writable(allowed_operations=["insert"])  # update/delete not allowed
    with pytest.raises(PolicyViolationError):
        validate_write_policy(DeleteStatement(table="orders", where=_eq()), policy, "demo")


def test_denied_table_is_rejected():
    from querygate.validation.write_policy_validation import validate_write_policy

    policy = _writable(allowed_tables=["customers"])  # not orders
    with pytest.raises(PolicyViolationError):
        validate_write_policy(DeleteStatement(table="orders", where=_eq()), policy, "demo")


def test_denied_write_column_is_rejected():
    from querygate.validation.write_policy_validation import validate_write_policy

    policy = _writable(denied_write_columns={"orders": ["id"]})
    with pytest.raises(PolicyViolationError):
        validate_write_policy(
            UpdateStatement(table="orders", set={"id": 5}, where=_eq()), policy, "demo"
        )


def test_where_on_a_read_denied_column_cannot_target_a_write():
    from querygate.validation.write_policy_validation import validate_write_policy

    # A column readable-denied cannot be used to *select* rows to mutate.
    policy = _writable(policy_kwargs={"denied_columns": {"orders": ["customer_id"]}})
    stmt = DeleteStatement(
        table="orders", where=Predicate(col="orders.customer_id", op="eq", value=7)
    )
    with pytest.raises(PolicyViolationError):
        validate_write_policy(stmt, policy, "demo")


# --------------------------------------------------------------------------- #
# Approval gate cannot be bypassed or a token replayed onto another write       #
# --------------------------------------------------------------------------- #


def test_approval_required_write_cannot_run_without_a_token(monkeypatch):
    from querygate.execution import write_execution as wx

    monkeypatch.setattr(wx.app_config, "approval_token_hmac_key", "k")
    wp = WritePolicy(
        enabled=True,
        allowed_tables=["orders"],
        allowed_operations=["delete"],
        require_approval_over_rows=0,
    )
    with pytest.raises(ApprovalRequiredError):
        WriteExecutionService("demo")._enforce_write_approval_gate(
            DeleteStatement(table="orders", where=_eq()), 5, wp, None
        )


def test_approval_token_bound_to_one_write_is_rejected_for_another(monkeypatch):
    from querygate.execution import write_execution as wx

    monkeypatch.setattr(wx.app_config, "approval_token_hmac_key", "k")
    wp = WritePolicy(
        enabled=True,
        allowed_tables=["orders"],
        allowed_operations=["delete"],
        require_approval_over_rows=0,
    )
    approved = DeleteStatement(table="orders", where=Predicate(col="orders.id", op="eq", value=1))
    other = DeleteStatement(table="orders", where=Predicate(col="orders.id", op="eq", value=2))
    token = issue_approval_token(
        fingerprint=write_fingerprint(approved), approver_subject="a", key="k"
    )
    svc = WriteExecutionService("demo")
    svc._enforce_write_approval_gate(approved, 5, wp, token)  # admits its own write
    with pytest.raises(ApprovalRequiredError):
        svc._enforce_write_approval_gate(other, 5, wp, token)  # not another


# --------------------------------------------------------------------------- #
# Batch sizing as a DoS axis (TODO.md item 109)                                 #
# --------------------------------------------------------------------------- #


def test_unbounded_write_batch_is_capped_not_a_dos_vector():
    """The write path is batched only over MCP. Before item 109 `WritePolicy`
    had no batch cap at all, so a caller could submit an arbitrarily long list
    of individually-in-cap writes in ONE call — each running the full
    validate→compile→execute pipeline and taking locks — multiplying cost and
    lock time per call far past what the read path allows the same principal.
    `max_affected_rows` does not help: every statement is individually legal."""
    from querygate.validation.write_policy_validation import validate_write_batch_size

    policy = _writable(max_batch_size=5)
    validate_write_batch_size(5, policy)  # at the cap: allowed
    with pytest.raises(PolicyViolationError, match="write batch size"):
        validate_write_batch_size(5000, policy)


def test_write_batch_cap_is_enforced_before_any_statement_runs():
    """The rejection must happen up front, not partway through — otherwise an
    over-size batch still pays for (and commits!) the statements before the
    one that trips the cap."""
    import asyncio

    from querygate.execution.write_execution import WriteExecutionService
    from querygate.policy.loader import PolicyStore, set_policy_store

    set_policy_store(PolicyStore(default=_writable(max_batch_size=1), overrides={}))
    svc = WriteExecutionService("demo")
    # Two writes against a table that does not exist in this unit context: if the
    # cap were checked per-item instead of up front, the first would fail on
    # schema/connection resolution rather than the batch-size rule.
    statements = [
        DeleteStatement(table="orders", where=_eq()),
        DeleteStatement(table="orders", where=_eq()),
    ]
    with pytest.raises(PolicyViolationError, match="write batch size 2 exceeds max of 1"):
        asyncio.run(svc.execute_many(statements))
