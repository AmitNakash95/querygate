"""Governed writes — AST + policy + compiler unit tests (TODO.md item 93, Phase 1).

Covers the structural guarantees: no raw-DML field anywhere, an UPDATE/DELETE
cannot be constructed without a WHERE, writes are deny-by-default, and the write
compiler emits bound-parameter Core DML (never literals).
"""

from __future__ import annotations

import pydantic
import sqlalchemy as sa
import pytest

from querygate.compiler.write_compiler import compile_write
from querygate.core.exceptions import PolicyViolationError, QueryValidationError
from querygate.policy.models import ColumnMask, ColumnMaskKind, Policy, WritePolicy
from querygate.query_ast.models import Predicate, StructuredQuery, WhereGroup
from querygate.validation.policy_validation import validate_policy
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
    # Match the column-check's own message, not just "not writable" -- that
    # substring also matches `table_writable`'s rejection, so a match this
    # loose can't tell the two checks apart (found by `test-contract-reviewer`
    # while reviewing item 149's fix).
    with pytest.raises(PolicyViolationError, match=r"Column orders\.status"):
        validate_write_policy(_update(), policy, _CONN)


def test_denied_write_column_still_applies_when_the_configured_table_key_casing_differs():
    # `WritePolicy.write_column_allowed` previously looked up `denied_write_columns`
    # by an exact-match `.get(table_name.lower(), [])`, so a table key configured
    # with any casing other than all-lowercase (e.g. "Orders", the same casing
    # `allowed_tables` legitimately uses elsewhere in the same policy) never
    # matched at all -- the deny list was silently inert regardless of the
    # statement's own table casing. Found while fixing TODO.md item 149.
    wp = WritePolicy(
        enabled=True,
        allowed_tables=["orders"],
        allowed_operations=["update"],
        denied_write_columns={"Orders": ["status"]},
    )
    policy = Policy(write=wp)
    with pytest.raises(PolicyViolationError, match=r"Column orders\.status"):
        validate_write_policy(_update(), policy, _CONN)


def test_denied_write_column_wildcard_entry_applies_to_every_table():
    # The "*" wildcard term in `write_column_allowed` (mirroring the read-side
    # `denied_columns` convention) had no test at all -- deleting it left the
    # suite green (found by `test-contract-reviewer` while reviewing item
    # 149's fix).
    wp = WritePolicy(
        enabled=True,
        allowed_tables=["orders"],
        allowed_operations=["update"],
        denied_write_columns={"*": ["status"]},
    )
    policy = Policy(write=wp)
    with pytest.raises(PolicyViolationError, match=r"Column orders\.status"):
        validate_write_policy(_update(), policy, _CONN)


def test_table_writable_is_case_insensitive():
    # `WritePolicy.table_writable`'s own case-insensitivity (independent of
    # `write_column_allowed`) had no direct test -- replacing `.casefold()`
    # with a bare `==` there would still pass every other test in this file
    # (found by `test-contract-reviewer` while reviewing item 149's fix).
    wp = WritePolicy(enabled=True, allowed_tables=["Orders"], allowed_operations=["update"])
    assert wp.table_writable("orders") is True
    assert wp.table_writable("ORDERS") is True
    assert wp.table_writable("customers") is False


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


# --------------------------------------------------------------------------- #
# Read-only predicate capabilities in a write WHERE. Since item 114 the write    #
# AST has its own narrowed predicate types, so `expr`/`value_expr` (item 100)    #
# and `value_subquery` (item 97/110) are refused at PARSE time — they are not    #
# in the schema at all. The validation-layer rejections stay as defence in       #
# depth for a read node reaching the service some other way, and are still       #
# tested (via `model_construct`, which is the only way to build that state).     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "rejected_field",
    [
        {"expr": {"col": "orders.id"}},
        {"value_expr": {"col": "orders.id"}},
        {"value_subquery": {"from": "customers", "select": ["customers.id"]}},
    ],
)
def test_read_only_predicate_fields_are_not_in_the_write_contract(rejected_field):
    """The item-114 narrowing: these are refused by the schema, so an agent is
    never invited to build a write the server would deny."""
    payload = {
        "op": "update",
        "table": "orders",
        "set": {"status": "shipped"},
        "where": {"col": "orders.customer_id", "op": "in", "value": [1], **rejected_field},
    }
    with pytest.raises(pydantic.ValidationError, match="extra_forbidden|Extra inputs"):
        UpdateStatement.model_validate(payload)


def test_value_subquery_in_write_where_is_rejected_at_validation():
    """Defence in depth: `model_construct` skips validation to simulate a read
    predicate reaching the write validator anyway. The write compiler passes
    ctx=None and could not render it, so it must fail cleanly here."""
    sub = StructuredQuery(from_table="customers", select=["customers.id"])
    stmt = UpdateStatement.model_construct(
        op="update",
        table="orders",
        set={"status": "shipped"},
        where=Predicate(col="orders.customer_id", op="in", value_subquery=sub),
    )
    with pytest.raises(QueryValidationError, match="subquery predicate"):
        validate_write_policy(stmt, _writable(), _CONN)


def test_value_subquery_nested_in_a_boolean_group_is_still_rejected():
    # The rejection must walk the whole WHERE tree, not just inspect the top
    # node — a subquery hidden one AND-level down must be caught too.
    sub = StructuredQuery(from_table="customers", select=["customers.id"])
    stmt = DeleteStatement.model_construct(
        op="delete",
        table="orders",
        where=WhereGroup(
            and_terms=[
                Predicate(col="orders.id", op="gt", value=0),
                Predicate(col="orders.customer_id", op="in", value_subquery=sub),
            ]
        ),
    )
    with pytest.raises(QueryValidationError, match="subquery predicate"):
        validate_write_policy(stmt, _writable(), _CONN)


def test_computed_expression_in_write_where_is_rejected_at_validation():
    """The same defence in depth for item 100's `expr` (the read Expression),
    which item 114 also removed from the write contract."""
    stmt = DeleteStatement.model_construct(
        op="delete",
        table="orders",
        where=Predicate(
            expr={"op": "*", "left": {"col": "orders.id"}, "right": {"literal": 2}},
            op="gt",
            value=1,
        ),
    )
    with pytest.raises(QueryValidationError, match="computed expression predicate"):
        validate_write_policy(stmt, _writable(), _CONN)


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


# --------------------------------------------------------------------------- #
# `to_read_where` (item 114) — the translation layer in front of item 111's one
# canonical walk. Its boolean branches shipped with NO test: swap and/or and a
# `DELETE ... WHERE a OR b` executes as `a AND b`, agreed on by the preview, the
# diff and the execution because all three re-derive from this one conversion.
# --------------------------------------------------------------------------- #
def _compiled_delete_where(where: dict) -> str:
    stmt = DeleteStatement.model_validate({"op": "delete", "table": "orders", "where": where})
    dml = compile_write(stmt, _orders_table(), "postgresql")
    return str(dml.compile(compile_kwargs={"literal_binds": True}))


def test_and_group_converts_to_a_conjunction():
    sql = _compiled_delete_where(
        {
            "and": [
                {"col": "orders.id", "op": "gt", "value": 1},
                {"col": "orders.status", "op": "eq", "value": "draft"},
            ]
        }
    )
    assert "orders.id > 1 AND orders.status = 'draft'" in sql


def test_or_group_converts_to_a_disjunction_not_a_conjunction():
    sql = _compiled_delete_where(
        {
            "or": [
                {"col": "orders.id", "op": "gt", "value": 1},
                {"col": "orders.status", "op": "eq", "value": "draft"},
            ]
        }
    )
    assert "orders.id > 1 OR orders.status = 'draft'" in sql
    assert " AND " not in sql


def test_not_group_converts_to_a_negation():
    # SQLAlchemy folds NOT(x = 1) into `x != 1`, which is the proof: without the
    # `not_terms` branch the same payload would render `orders.id = 1`.
    sql = _compiled_delete_where({"not": {"col": "orders.id", "op": "eq", "value": 1}})
    assert "orders.id != 1" in sql
    assert "orders.id = 1" not in sql


def test_nested_groups_convert_at_every_level():
    sql = _compiled_delete_where(
        {
            "and": [
                {"col": "orders.id", "op": "gt", "value": 1},
                {
                    "or": [
                        {"col": "orders.status", "op": "eq", "value": "draft"},
                        {"not": {"col": "orders.status", "op": "eq", "value": "sent"}},
                    ]
                },
            ]
        }
    )
    assert " AND " in sql and " OR " in sql
    assert "orders.status != 'sent'" in sql  # the negated leaf, folded by SQLAlchemy


def test_denied_column_nested_in_a_write_group_is_rejected():
    """The plan §3 "denied column buried in the deepest position" box, for the new
    node: policy validation must see refs through the converted group tree."""
    stmt = DeleteStatement.model_validate(
        {
            "op": "delete",
            "table": "orders",
            "where": {
                "and": [
                    {"col": "orders.id", "op": "gt", "value": 0},
                    {"not": {"col": "orders.email", "op": "eq", "value": "x@y.z"}},
                ]
            },
        }
    )
    policy = _writable(denied_columns={"orders": ["email"]})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_write_policy(stmt, policy, _CONN)


def test_masked_column_nested_in_a_write_group_is_rejected():
    stmt = DeleteStatement.model_validate(
        {
            "op": "delete",
            "table": "orders",
            "where": {"or": [{"col": "orders.email", "op": "eq", "value": "x@y.z"}]},
        }
    )
    policy = _writable(column_masks={"orders": [ColumnMask(column="email", kind="hash")]})
    with pytest.raises(PolicyViolationError, match="masked"):
        validate_write_policy(stmt, policy, _CONN)


def test_to_read_where_passes_a_read_node_through_unchanged():
    """The pass-through is load-bearing: it is what keeps the runtime rejections
    of expr/value_expr/value_subquery reachable for a read node that arrives some
    other way. Identity, so no refactor can quietly "simplify" it into a rebuild."""
    from querygate.write_ast.models import to_read_where

    predicate = Predicate(col="orders.id", op="eq", value=1)
    group = WhereGroup(and_terms=[predicate])
    assert to_read_where(predicate) is predicate
    assert to_read_where(group) is group


def test_to_read_where_fails_closed_on_an_unknown_node():
    """A future WriteWhereNode member that reaches the conversion without a branch
    must RAISE, not be passed through — the permissive version silently dropped
    the terms of a combinator it did not know about."""
    from querygate.write_ast.models import to_read_where

    class NotAWriteNode:
        pass

    with pytest.raises(QueryValidationError, match="Unsupported write filter node"):
        to_read_where(NotAWriteNode())


def test_write_errors_never_advertise_a_read_only_field():
    """Item 114 removed expr/value_expr/value_subquery from the write CONTRACT, so
    the error channel must not re-offer them: an agent told to "try value_expr"
    hits `extra_forbidden` and is back to routing around the gate."""
    bad_shapes = [
        {"col": "orders.id", "op": "eq"},
        {"col": "orders.id", "op": "eq", "value": 1, "value_col": "orders.status"},
        {"op": "eq", "value": 1},
    ]
    for shape in bad_shapes:
        with pytest.raises(pydantic.ValidationError) as excinfo:
            DeleteStatement.model_validate({"op": "delete", "table": "orders", "where": shape})
        message = str(excinfo.value)
        for removed in ("value_expr", "value_subquery", "'expr'"):
            assert removed not in message, f"{removed} leaked into: {message}"


@pytest.mark.parametrize("field", ["expr", "value_expr"])
def test_computed_expression_in_write_where_is_rejected_at_validation_for_both_fields(field):
    """Both disjuncts of the runtime rejection, not just `expr` — the archive
    claimed all three read-only fields were covered and `value_expr` was not."""
    computed = {"op": "*", "left": {"col": "orders.id"}, "right": {"literal": 2}}
    if field == "expr":
        kwargs = {"expr": computed, "op": "gt", "value": 1}
    else:
        kwargs = {"col": "orders.id", "op": "gt", "value_expr": computed}
    stmt = DeleteStatement.model_construct(op="delete", table="orders", where=Predicate(**kwargs))
    with pytest.raises(QueryValidationError, match="computed expression predicate"):
        validate_write_policy(stmt, _writable(), _CONN)


def test_value_col_must_be_dotted_on_a_write_predicate():
    """The write-only tightening, applied symmetrically to both column fields: a
    bare ref used to slip past the allow/deny + mask walk entirely (it is skipped
    by `predicate_direct_column_refs`) and fail later in the compiler."""
    for shape in (
        {"col": "id", "op": "eq", "value": 1},
        {"col": "orders.id", "op": "eq", "value_col": "status"},
    ):
        with pytest.raises(pydantic.ValidationError, match="must be a 'Table.Column'"):
            DeleteStatement.model_validate({"op": "delete", "table": "orders", "where": shape})


def test_to_read_where_rejects_a_group_with_no_combinator():
    """The last `to_read_where` branch. Only reachable via `model_construct`, but an
    untested unreachable branch is exactly what this item deleted
    `_reject_subquery_in_write_where` for — so it gets its one line."""
    from querygate.write_ast.models import WriteWhereGroup, to_read_where

    empty = WriteWhereGroup.model_construct(and_terms=None, or_terms=None, not_terms=None)
    with pytest.raises(QueryValidationError, match="no boolean combinator"):
        to_read_where(empty)


def test_write_predicate_is_frozen_so_its_cached_read_predicate_cannot_go_stale():
    """`write_fingerprint` hashes the FIELDS while the compiler uses the CACHED read
    predicate, so a mutated field would let an approval token bind to a write that
    is not the one executed. Freezing makes that unreachable."""
    from querygate.write_ast.models import WritePredicate

    predicate = WritePredicate(col="orders.id", op="eq", value=1)
    with pytest.raises(pydantic.ValidationError):
        predicate.value = 999
    assert predicate.as_read_predicate().value == 1


def test_a_model_constructed_write_predicate_rebuilds_with_a_typed_error():
    """The one conversion path that can fail outside a validator — inside the
    audited try-block — must raise the typed error the transports map to a 422, not
    a bare ValueError that would be masked to a 500."""
    from querygate.write_ast.models import WritePredicate

    invalid = WritePredicate.model_construct(col="orders.id", op="eq", value=None)
    with pytest.raises(QueryValidationError, match="requires a value or value_col"):
        invalid.as_read_predicate()


def test_converting_a_group_does_not_revalidate_its_leaves():
    """`model_construct` on the rebuilt group: re-validating a nested leaf raised a
    Pydantic error whose text carried the predicate's repr — and re-offered the
    removed field names — into the operator log."""
    from querygate.write_ast.models import WriteWhereGroup, to_read_where

    unvalidated_leaf = Predicate.model_construct(col="orders.email", op="eq")
    group = WriteWhereGroup.model_construct(and_terms=[unvalidated_leaf])
    converted = to_read_where(group)  # must not raise
    assert converted.and_terms[0] is unvalidated_leaf


# --------------------------------------------------------------------------- #
# Write-filter shape caps (TODO.md item 116). A write's WHERE used to be exempt
# from all three caps the read path enforces, so `delete … where id in [1M ids]`
# compiled ~1M bind parameters and ran a COUNT(*) over them before
# `max_affected_rows` was consulted. Same `Policy` fields as reads: a write's
# WHERE *reads* rows to select them, the same reasoning that already applies the
# read allow/deny and masking rules to it.
# --------------------------------------------------------------------------- #
def _delete_where(where: dict) -> DeleteStatement:
    return DeleteStatement.model_validate({"op": "delete", "table": "orders", "where": where})


def test_write_where_in_list_is_bounded():
    over = _delete_where({"col": "orders.id", "op": "in", "value": list(range(11))})
    with pytest.raises(PolicyViolationError, match="exceeds max_in_list_size"):
        validate_write_policy(over, _writable(max_in_list_size=10), _CONN)
    at_cap = _delete_where({"col": "orders.id", "op": "in", "value": list(range(10))})
    validate_write_policy(at_cap, _writable(max_in_list_size=10), _CONN)  # no raise


def test_write_where_nesting_depth_is_bounded():
    deep = {"not": {"not": {"not": {"col": "orders.id", "op": "eq", "value": 1}}}}
    with pytest.raises(PolicyViolationError, match="write where nesting depth"):
        validate_write_policy(_delete_where(deep), _writable(max_where_depth=3), _CONN)
    validate_write_policy(_delete_where(deep), _writable(max_where_depth=4), _CONN)


def test_write_where_predicate_count_is_bounded():
    wide = {"or": [{"col": "orders.id", "op": "eq", "value": i} for i in range(6)]}
    with pytest.raises(PolicyViolationError, match="write where predicate count 6"):
        validate_write_policy(_delete_where(wide), _writable(max_where_predicates=5), _CONN)
    # At-cap positive control, so an off-by-one (>= instead of >) fails here.
    validate_write_policy(_delete_where(wide), _writable(max_where_predicates=6), _CONN)


@pytest.mark.parametrize(
    "rule", ["_check_where_depth", "_check_predicate_count", "_check_in_list_size"]
)
def test_read_and_write_paths_route_through_the_same_shape_rules(monkeypatch, rule):
    """One implementation, not a write-side copy — proven by spying on each rule and
    seeing BOTH paths reach it. (Comparing imported names would be tautological:
    Python's import semantics make that identity hold automatically.)"""
    from querygate.validation import policy_validation as pv

    calls: list = []
    original = getattr(pv, rule)

    def _spy(*args, **kwargs):
        calls.append(rule)
        return original(*args, **kwargs)

    monkeypatch.setattr(pv, rule, _spy)

    read_query = StructuredQuery.model_validate(
        {
            "from": "orders",
            "select": ["orders.id"],
            "where": {"col": "orders.id", "op": "in", "value": [1, 2]},
        }
    )
    validate_policy(read_query, Policy(), _CONN)
    read_calls = len(calls)
    assert read_calls, f"the READ path does not use {rule}"

    validate_write_policy(
        _delete_where({"col": "orders.id", "op": "in", "value": [1, 2]}), _writable(), _CONN
    )
    assert len(calls) > read_calls, f"the WRITE path does not use {rule}"


@pytest.mark.parametrize(
    "predicate,expected_target",
    [
        ({"col": "orders.status", "op": "in", "value": ["a", "b"]}, "'orders.status'"),
        (
            {
                "col_fn": {"fn": "lower", "args": [{"col": "orders.status"}]},
                "op": "in",
                "value": ["a", "b"],
            },
            r"'lower\(\.\.\.\)'",
        ),
    ],
)
def test_in_list_rejection_names_which_target_exceeded_the_cap(predicate, expected_target):
    """The message's target ladder (`col` -> `col_fn(...)` -> expression) was
    asserted by nothing, so mutating it failed no test — and an operator reading the
    rejection needs to know WHICH filter was too wide."""
    with pytest.raises(PolicyViolationError, match=f"in list for {expected_target} exceeds"):
        validate_write_policy(_delete_where(predicate), _writable(max_in_list_size=1), _CONN)
