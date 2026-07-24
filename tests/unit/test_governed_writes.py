"""Governed writes — AST + policy + compiler unit tests (TODO.md item 93, Phase 1).

Covers the structural guarantees: no raw-DML field anywhere, an UPDATE/DELETE
cannot be constructed without a WHERE, writes are deny-by-default, and the write
compiler emits bound-parameter Core DML (never literals).
"""

from __future__ import annotations

import sqlalchemy as sa
import pytest

from querygate.compiler.write_compiler import compile_write
from querygate.core.exceptions import PolicyViolationError
from querygate.policy.models import ColumnMask, ColumnMaskKind, Policy, WritePolicy
from querygate.validation.write_policy_validation import (
    validate_write_batch_size,
    validate_write_policy,
)
from querygate.write_ast.models import (
    DeleteStatement,
    InsertStatement,
    UpdateStatement,
    _WRITE_STATEMENT_TYPES,
)

pytestmark = pytest.mark.unit

_CONN = "demo"


def _writable(*, denied_write_columns=None, **read_policy_kwargs) -> Policy:
    """A policy that permits writes to `orders`. Write-policy knobs go on
    WritePolicy; read-policy knobs (denied_columns/column_masks) go on Policy."""
    wp = WritePolicy(
        enabled=True,
        allowed_tables=["orders"],
        allowed_operations=["insert", "update", "delete"],
        denied_write_columns=denied_write_columns or {},
    )
    return Policy(write=wp, **read_policy_kwargs)


# --------------------------------------------------------------------------- #
# Structural guarantees (can't even be constructed unsafely)                    #
# --------------------------------------------------------------------------- #


def test_no_raw_dml_field_on_any_write_model():
    for model in _WRITE_STATEMENT_TYPES.values():
        fields = set(model.model_fields)
        for banned in ("sql", "raw_sql", "dml", "raw", "statement", "query"):
            assert banned not in fields, f"{model.__name__} exposes a raw field {banned!r}"


def test_update_requires_where():
    with pytest.raises(Exception):
        UpdateStatement(table="orders", set={"status": "shipped"})  # no where


def test_delete_requires_where():
    with pytest.raises(Exception):
        DeleteStatement(table="orders")  # no where


def test_insert_rows_must_be_column_consistent():
    with pytest.raises(ValueError, match="same set of columns"):
        InsertStatement(table="orders", rows=[{"a": 1}, {"a": 1, "b": 2}])


# --------------------------------------------------------------------------- #
# Deny-by-default policy                                                        #
# --------------------------------------------------------------------------- #


def _update():
    return UpdateStatement(
        table="orders",
        set={"status": "shipped"},
        where={"col": "orders.id", "op": "eq", "value": 1},
    )


def test_writes_denied_by_default():
    with pytest.raises(PolicyViolationError, match="Writes are not enabled"):
        validate_write_policy(_update(), Policy(), _CONN)


def test_operation_must_be_allowed():
    policy = Policy(
        write=WritePolicy(enabled=True, allowed_tables=["orders"], allowed_operations=["insert"])
    )
    with pytest.raises(PolicyViolationError, match="operation 'update' is not allowed"):
        validate_write_policy(_update(), policy, _CONN)


def test_table_must_be_writable():
    policy = Policy(
        write=WritePolicy(enabled=True, allowed_tables=["customers"], allowed_operations=["update"])
    )
    with pytest.raises(PolicyViolationError, match="not writable"):
        validate_write_policy(_update(), policy, _CONN)


def test_written_column_must_be_write_allowed():
    policy = _writable(denied_write_columns={"orders": ["status"]})
    with pytest.raises(PolicyViolationError, match="not writable"):
        validate_write_policy(_update(), policy, _CONN)


def test_where_column_denied_by_read_policy_is_rejected():
    stmt = UpdateStatement(
        table="orders",
        set={"status": "shipped"},
        where={"col": "orders.secret", "op": "eq", "value": 1},
    )
    policy = _writable(denied_columns={"orders": ["secret"]})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_write_policy(stmt, policy, _CONN)


def test_masked_column_cannot_filter_a_write():
    stmt = UpdateStatement(
        table="orders",
        set={"status": "shipped"},
        where={"col": "orders.email", "op": "eq", "value": "x@y.z"},
    )
    policy = _writable(
        column_masks={"orders": [ColumnMask(column="email", kind=ColumnMaskKind.HASH)]}
    )
    with pytest.raises(PolicyViolationError, match="masked"):
        validate_write_policy(stmt, policy, _CONN)


def test_valid_write_passes_policy():
    validate_write_policy(_update(), _writable(), _CONN)  # no raise


# --------------------------------------------------------------------------- #
# Compiler emits bound-parameter DML                                           #
# --------------------------------------------------------------------------- #


def _orders_table() -> sa.Table:
    return sa.Table(
        "orders",
        sa.MetaData(),
        sa.Column("id", sa.Integer),
        sa.Column("status", sa.String(20)),
    )


def test_update_compiles_to_parameterized_dml():
    dml = compile_write(_update(), _orders_table())
    sql = str(dml.compile(compile_kwargs={"literal_binds": False}))
    assert sql.upper().startswith("UPDATE ORDERS SET")
    assert "WHERE" in sql.upper()
    assert "shipped" not in sql  # value is a bind param, not a literal


def test_delete_compiles_with_where():
    stmt = DeleteStatement(table="orders", where={"col": "orders.id", "op": "eq", "value": 9})
    dml = compile_write(stmt, _orders_table())
    sql = str(dml.compile(compile_kwargs={"literal_binds": False})).upper()
    assert sql.startswith("DELETE FROM ORDERS") and "WHERE" in sql


def test_insert_compiles():
    stmt = InsertStatement(table="orders", rows=[{"status": "new"}])
    dml = compile_write(stmt, _orders_table())
    sql = str(dml.compile(compile_kwargs={"literal_binds": False})).upper()
    assert sql.startswith("INSERT INTO ORDERS")
    assert "NEW" not in sql  # bound, not literal


# --------------------------------------------------------------------------- #
# Batch sizing (TODO.md item 109) — the write sibling of the read path's        #
# `validate_batch_size`. `max_affected_rows` bounds ONE statement's blast       #
# radius; this bounds how many statements ride along with it.                   #
# --------------------------------------------------------------------------- #


def _batch(n: int) -> list:
    return [UpdateStatement(**_update().model_dump()) for _ in range(n)]


def test_write_batch_size_cap_defaults_to_the_read_path_value():
    """The write cap deliberately mirrors `Policy.max_batch_size` rather than
    inventing a different number — the write path was modeled on the read path."""
    assert WritePolicy().max_batch_size == Policy().max_batch_size


def test_write_batch_over_cap_is_rejected():
    policy = _writable()
    policy.write.max_batch_size = 3
    with pytest.raises(PolicyViolationError, match="write batch size 4 exceeds max of 3"):
        validate_write_batch_size(4, policy)


def test_write_batch_at_cap_passes():
    """Boundary: exactly at the cap is allowed, one over is not (item 109)."""
    policy = _writable()
    policy.write.max_batch_size = 3
    validate_write_batch_size(3, policy)  # must not raise
    with pytest.raises(PolicyViolationError):
        validate_write_batch_size(4, policy)


def test_write_batch_size_cap_must_be_at_least_one():
    with pytest.raises(Exception):
        WritePolicy(max_batch_size=0)


@pytest.mark.asyncio
async def test_execute_many_rejects_an_over_size_batch_before_running_anything():
    """The cap is enforced at the service layer, not only at the MCP transport,
    and *before* any statement is validated/compiled/executed — so an over-size
    batch costs nothing rather than failing partway through."""
    from querygate.execution.write_execution import WriteExecutionService
    from querygate.policy.loader import PolicyStore, set_policy_store

    policy = _writable()
    policy.write.max_batch_size = 2
    set_policy_store(PolicyStore(default=policy, overrides={}))

    service = WriteExecutionService(connection_id=_CONN, surface="rest")
    with pytest.raises(PolicyViolationError, match="write batch size 3 exceeds max of 2"):
        await service.execute_many(_batch(3))
