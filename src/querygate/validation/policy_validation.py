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
    effective_name_map,
    parse_column_ref,
    select_item_column_refs,
    where_depth,
)


def _collect_referenced_tables(query: StructuredQuery) -> Set[str]:
    """Every PHYSICAL table this query touches — column refs are qualified by
    effective name (alias if given, else table name), so each one is mapped
    back through `effective_name_map` before being added, ensuring table-
    level policy is checked against the real table, never an alias.
    """
    name_to_physical = effective_name_map(query)
    tables = {query.from_table, *(join.table for join in query.joins)}
    for item in query.select:
        for ref in select_item_column_refs(item):
            t, _ = parse_column_ref(ref)
            tables.add(name_to_physical.get(t.lower(), t))
    return tables


def _where_column_refs(node: WhereNode) -> Iterator[str]:
    if isinstance(node, Predicate):
        if "." in node.col:
            yield node.col
        if node.value_col is not None:
            yield node.value_col
        return
    if node.not_terms is not None:
        yield from _where_column_refs(node.not_terms)
        return
    for child in node.and_terms or node.or_terms or []:
        yield from _where_column_refs(child)


def _iter_where_predicates(node: WhereNode) -> Iterator[Predicate]:
    if isinstance(node, Predicate):
        yield node
        return
    if node.not_terms is not None:
        yield from _iter_where_predicates(node.not_terms)
        return
    for child in node.and_terms or node.or_terms or []:
        yield from _iter_where_predicates(child)


def _iter_column_refs(query: StructuredQuery) -> Iterator[str]:
    """Every Table.Column reference anywhere in the query — select, join
    keys, where, group_by, having, order_by, top_n — so column-level policy
    can't be bypassed by filtering/sorting/grouping on a denied column
    without ever selecting it.
    """
    for item in query.select:
        yield from select_item_column_refs(item)
    for join in query.joins:
        yield from join.on
    if query.where is not None:
        yield from _where_column_refs(query.where)
    for col_ref in query.group_by:
        if "." in col_ref:
            yield col_ref
    for pred in query.having:
        if "." in pred.col:
            yield pred.col
        if pred.value_col is not None:
            yield pred.value_col
    for order in query.order_by:
        if "." in order.col:
            yield order.col
    if query.top_n is not None:
        for ref in query.top_n.partition_by:
            if "." in ref:
                yield ref
        for order in query.top_n.order_by:
            if "." in order.col:
                yield order.col


def referenced_tables(query: StructuredQuery) -> Set[str]:
    """Return every PHYSICAL table touched by a query using the production
    policy walk (see `_collect_referenced_tables` — every column ref's
    effective/alias name is mapped back to its physical table).

    Candidate simulation uses this to scope mandatory-filter readiness to the
    same query graph that policy validation sees, without inspecting predicate
    values or compiling SQL.
    """
    name_to_physical = effective_name_map(query)
    tables = _collect_referenced_tables(query)
    for ref in _iter_column_refs(query):
        table, _column = parse_column_ref(ref)
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
            raise PolicyViolationError(
                f"{pred.op} list for {pred.col!r} exceeds max_in_list_size of "
                f"{policy.max_in_list_size} items"
            )

    column_refs = list(_iter_column_refs(query))
    tables = referenced_tables(query)
    name_to_physical = effective_name_map(query)

    for table in tables:
        if not policy.table_allowed(table):
            raise PolicyViolationError(f"Table {table!r} is not accessible under the active policy")

    for ref in column_refs:
        t, c = parse_column_ref(ref)
        # t is the ref's effective name (an alias, or the table name itself)
        # — always resolve to the PHYSICAL table before checking column
        # policy, so an alias can never be used to dodge a denied column.
        physical_t = name_to_physical.get(t.lower(), t)
        if not policy.column_allowed(physical_t, c):
            raise PolicyViolationError(f"Column {ref!r} is not accessible under the active policy")


def validate_batch_size(count: int, policy: Policy) -> None:
    if count > policy.max_batch_size:
        raise PolicyViolationError(f"batch size {count} exceeds max of {policy.max_batch_size}")
