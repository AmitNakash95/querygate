"""Policy enforcement: caps + allow/deny lists, checked before compilation.

This runs BEFORE validation/schema_validation.py reflects anything, so a
disabled connection or an over-cap query never even touches the database.
"""

from __future__ import annotations

from typing import Iterator, List, Set

from querygate.core.exceptions import PolicyViolationError
from querygate.policy.models import Policy
from querygate.query_ast.models import CaseSelectItem, Predicate, StructuredQuery, WhereNode
from querygate.validation.schema_validation import (
    RefPosition,
    effective_name_map,
    iter_column_refs,
    parse_column_ref,
    where_depth,
)


def _iter_where_predicates(node: WhereNode) -> Iterator[Predicate]:
    """Enumerate the Predicate leaves of a WHERE tree — a different axis from
    the reference visitor (predicates, for count/in-list caps, not column
    references), so it stays here rather than folding into `iter_column_refs`.
    """
    if isinstance(node, Predicate):
        yield node
        return
    if node.not_terms is not None:
        yield from _iter_where_predicates(node.not_terms)
        return
    for child in node.and_terms or node.or_terms or []:
        yield from _iter_where_predicates(child)


def referenced_tables(query: StructuredQuery) -> Set[str]:
    """Return every PHYSICAL table touched by a query: the structural from/join
    tables, plus the physical table behind every column reference the canonical
    visitor (`iter_column_refs`) finds. Each ref's effective/alias name is
    mapped back through `effective_name_map`, so table-level policy is checked
    against the real table, never an alias.

    Candidate simulation uses this to scope mandatory-filter readiness to the
    same query graph that policy validation sees, without inspecting predicate
    values or compiling SQL.
    """
    name_to_physical = effective_name_map(query)
    tables = {query.from_table, *(join.table for join in query.joins)}
    for column_ref in iter_column_refs(query):
        table, _column = parse_column_ref(column_ref.ref)
        tables.add(name_to_physical.get(table.lower(), table))
    return tables


def validate_policy(query: StructuredQuery, policy: Policy, connection_id: str) -> None:
    if not policy.enabled:
        raise PolicyViolationError(f"Connection {connection_id!r} is disabled by policy")

    if len(query.select) > policy.max_select_columns:
        raise PolicyViolationError(f"select exceeds max of {policy.max_select_columns} items")
    for item in query.select:
        if isinstance(item, CaseSelectItem) and len(item.when) > policy.max_case_branches:
            raise PolicyViolationError(
                f"case when branches exceeds max of {policy.max_case_branches}"
            )
    if len(query.joins) > policy.max_joins:
        raise PolicyViolationError(f"joins exceeds max of {policy.max_joins}")
    if len(query.group_by) > policy.max_group_by:
        raise PolicyViolationError(f"group_by exceeds max of {policy.max_group_by} columns")
    if query.where is not None:
        depth = where_depth(query.where)
        if depth > policy.max_where_depth:
            raise PolicyViolationError(
                f"where nesting depth {depth} exceeds max {policy.max_where_depth}"
            )
    if query.top_n is not None:
        if len(query.top_n.partition_by) > policy.max_partition_by:
            raise PolicyViolationError(
                f"top_n.partition_by exceeds max of {policy.max_partition_by} columns"
            )
        if query.top_n.n > policy.max_top_n:
            raise PolicyViolationError(f"top_n.n exceeds max of {policy.max_top_n}")

    if query.where is not None:
        where_predicate_count = sum(1 for _ in _iter_where_predicates(query.where))
        if where_predicate_count > policy.max_where_predicates:
            raise PolicyViolationError(
                f"where predicate count {where_predicate_count} exceeds max of "
                f"{policy.max_where_predicates}"
            )
    if len(query.having) > policy.max_where_predicates:
        raise PolicyViolationError(
            f"having predicate count {len(query.having)} exceeds max of "
            f"{policy.max_where_predicates}"
        )

    all_predicates: List[Predicate] = list(query.having)
    if query.where is not None:
        all_predicates.extend(_iter_where_predicates(query.where))
    for pred in all_predicates:
        if pred.op in ("in", "not_in") and len(pred.value) > policy.max_in_list_size:
            target = pred.col if pred.col is not None else f"{pred.col_fn.fn}(...)"
            raise PolicyViolationError(
                f"{pred.op} list for {target!r} exceeds max_in_list_size of "
                f"{policy.max_in_list_size} items"
            )

    # One walk of the canonical visitor feeds both the table/column allow-deny
    # checks and the masked-column rule below — no second parallel enumeration.
    all_refs = list(iter_column_refs(query))
    tables = referenced_tables(query)
    name_to_physical = effective_name_map(query)

    for table in tables:
        if not policy.table_allowed(table):
            raise PolicyViolationError(f"Table {table!r} is not accessible under the active policy")

    for column_ref in all_refs:
        t, c = parse_column_ref(column_ref.ref)
        # t is the ref's effective name (an alias, or the table name itself)
        # — always resolve to the PHYSICAL table before checking column
        # policy, so an alias can never be used to dodge a denied column.
        physical_t = name_to_physical.get(t.lower(), t)
        if not policy.column_allowed(physical_t, c):
            raise PolicyViolationError(
                f"Column {column_ref.ref!r} is not accessible under the active policy"
            )

    # A masked column may only appear as a bare SELECT projection item —
    # anywhere else (filter/join/order/group, or nested in a function/CASE/
    # aggregate) an unmasked reference would leak the real value via inference,
    # so it's rejected rather than silently masked-in-place (TODO.md item 49).
    for column_ref in all_refs:
        if column_ref.position is RefPosition.SELECT_PROJECTION_BARE:
            continue
        t, c = parse_column_ref(column_ref.ref)
        physical_t = name_to_physical.get(t.lower(), t)
        if policy.column_mask(physical_t, c) is not None:
            raise PolicyViolationError(
                f"Column {column_ref.ref!r} is masked by policy and can only appear in the select "
                "projection, not in filters, joins, ordering, grouping, or nested in a function"
            )


def validate_batch_size(count: int, policy: Policy) -> None:
    if count > policy.max_batch_size:
        raise PolicyViolationError(f"batch size {count} exceeds max of {policy.max_batch_size}")
