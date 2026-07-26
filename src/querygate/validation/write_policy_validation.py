"""WritePolicy enforcement (TODO.md item 93, Phase 1) — the write sibling of
`validation/policy_validation.py`, run before a write is ever compiled/previewed.

Deny-by-default: a write is refused unless writes are enabled, the operation is
allowed, and the target table is writable. Written columns are checked against
`WritePolicy.write_column_allowed`; a write's WHERE filter reuses the READ
column allow/deny + masked-column rules (filtering on a column is a read of it),
so a denied or masked column can't be used to target rows.
"""

from __future__ import annotations

from querygate.core.exceptions import PolicyViolationError, QueryValidationError
from querygate.policy.models import Policy
from querygate.validation.policy_validation import enforce_predicate_shape_caps
from querygate.validation.schema_validation import (
    iter_where_predicates,
    parse_column_ref,
    predicate_column_refs,
)
from querygate.write_ast.models import (
    DeleteStatement,
    InsertStatement,
    UpdateStatement,
    UpsertStatement,
    WriteStatement,
    to_read_where,
)


def validate_write_batch_size(count: int, policy: Policy) -> None:
    """Bound how many writes one batch call may carry (TODO.md item 109) — the
    write sibling of `policy_validation.validate_batch_size`.

    Called *before* any statement in the batch is validated, compiled, or run, so
    an over-size batch costs nothing. `WritePolicy.max_affected_rows` bounds a
    single statement's blast radius; this bounds how many statements ride along
    with it, which is otherwise unbounded on the batched (MCP) write path."""
    if count > policy.write.max_batch_size:
        raise PolicyViolationError(
            f"write batch size {count} exceeds max of {policy.write.max_batch_size}"
        )


def _written_columns(statement: WriteStatement) -> list[str]:
    if isinstance(statement, (InsertStatement, UpsertStatement)):
        return list(statement.rows[0].keys())  # validated identical across rows
    if isinstance(statement, UpdateStatement):
        return list(statement.set.keys())
    return []  # delete writes no columns


def validate_write_policy(statement: WriteStatement, policy: Policy, connection_id: str) -> None:
    if not policy.enabled:
        raise PolicyViolationError(f"Connection {connection_id!r} is disabled by policy")

    wp = policy.write
    if not wp.enabled:
        raise PolicyViolationError(
            f"Writes are not enabled for connection {connection_id!r} (WritePolicy.enabled=false)"
        )
    if not wp.operation_allowed(statement.op):
        raise PolicyViolationError(f"Write operation {statement.op!r} is not allowed by policy")
    if not wp.table_writable(statement.table):
        raise PolicyViolationError(
            f"Table {statement.table!r} is not writable under the active policy"
        )

    # Written columns must be write-allowed.
    for column in _written_columns(statement):
        if not wp.write_column_allowed(statement.table, column):
            raise PolicyViolationError(
                f"Column {statement.table}.{column} is not writable under the active policy"
            )

    # A write's WHERE filter reads columns to select rows — subject to the READ
    # allow/deny + masked-column rules, so a denied/masked column can't be used
    # to target a mutation.
    where = getattr(statement, "where", None)
    if where is not None:
        # Narrowed write filter -> the read WhereNode the ONE canonical predicate
        # walk understands (item 114). A read node passes through unchanged, which
        # is what keeps the rejections below reachable as defence in depth.
        read_where = to_read_where(where)
        # Shape caps FIRST (item 116): a write filter used to be exempt from
        # max_where_depth / max_where_predicates / max_in_list_size, so
        # `delete … where id in [<huge list>]` was rendered in full client-side
        # (100,000 values -> a 689 KB statement) before `max_affected_rows` was
        # consulted, and past the driver's parameter limit it failed there rather
        # than being refused cleanly here. These are the
        # READ caps by design — a write's WHERE *reads* rows in order to select
        # them, which is the same reasoning that already applies the read
        # allow/deny and masking rules to it (2026-07-26 Decision Log).
        enforce_predicate_shape_caps(read_where, policy, label="write where")
        for pred in iter_where_predicates(read_where):
            # A subquery predicate (item 97's `value_subquery` / IN (subquery)) is
            # a READ-only capability — writes never pass a compiler `ctx`, so the
            # write compiler can't render one and would fail deep inside
            # `_compile_where` with an internal error. Reject it here, at the
            # validation layer, with a clear message instead: the write path
            # genuinely lacks this capability, so this is a reject-not-emulate
            # rejection (like MSSQL `array_agg`), not a policy denial. Scope the
            # target rows with literal or column predicates instead.
            if pred.value_subquery is not None:
                raise QueryValidationError(
                    "A subquery predicate (value_subquery / IN (subquery)) is not "
                    "supported in a write's WHERE clause; scope the target rows "
                    "with literal or column predicates instead."
                )
            # Item 100's scalar Expression substrate is a READ-engine capability
            # (ENGINE_EXPRESSIVENESS_PLAN.md is scoped to the read query engine).
            # A computed predicate would compile fine here, but it has never been
            # reviewed against the write path's own guarantees — the affected-row
            # COUNT(*), the item-108 diff, and the row cap all re-derive the same
            # WHERE, so widening writes is a deliberate future item, not a
            # side-effect of widening reads. Same reject-at-validation posture as
            # value_subquery above (item 110), not a policy denial.
            if pred.expr is not None or pred.value_expr is not None:
                raise QueryValidationError(
                    "A computed expression predicate (expr / value_expr) is not "
                    "supported in a write's WHERE clause; scope the target rows "
                    "with literal or column predicates instead."
                )
            for ref in predicate_column_refs(pred):
                table, column = parse_column_ref(ref)
                if not policy.column_allowed(table, column):
                    raise PolicyViolationError(
                        f"Column {ref!r} is not accessible under the active policy"
                    )
                if policy.column_mask(table, column) is not None:
                    raise PolicyViolationError(
                        f"Column {ref!r} is masked by policy and cannot be used to filter a write"
                    )
