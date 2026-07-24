"""WritePolicy enforcement (TODO.md item 93, Phase 1) — the write sibling of
`validation/policy_validation.py`, run before a write is ever compiled/previewed.

Deny-by-default: a write is refused unless writes are enabled, the operation is
allowed, and the target table is writable. Written columns are checked against
`WritePolicy.write_column_allowed`; a write's WHERE filter reuses the READ
column allow/deny + masked-column rules (filtering on a column is a read of it),
so a denied or masked column can't be used to target rows.
"""

from __future__ import annotations

from typing import Iterator

from querygate.core.exceptions import PolicyViolationError
from querygate.policy.models import Policy
from querygate.query_ast.models import Predicate, WhereNode
from querygate.validation.schema_validation import parse_column_ref, predicate_column_refs
from querygate.write_ast.models import (
    DeleteStatement,
    InsertStatement,
    UpdateStatement,
    UpsertStatement,
    WriteStatement,
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


def _where_predicates(node: WhereNode) -> Iterator[Predicate]:
    if isinstance(node, Predicate):
        yield node
        return
    if node.not_terms is not None:
        yield from _where_predicates(node.not_terms)
        return
    for child in node.and_terms or node.or_terms or []:
        yield from _where_predicates(child)


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
        for pred in _where_predicates(where):
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
