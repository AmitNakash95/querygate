"""Policy enforcement: caps + allow/deny lists, checked before compilation.

This runs BEFORE validation/schema_validation.py reflects anything, so a
disabled connection or an over-cap query never even touches the database.
"""

from __future__ import annotations

from typing import Iterator, Set

from querygate.core.exceptions import PolicyViolationError
from querygate.policy.models import Policy
from querygate.query_ast.models import Predicate, StructuredQuery, WhereNode
from querygate.validation.schema_validation import parse_column_ref, where_depth


def _collect_referenced_tables(query: StructuredQuery) -> Set[str]:
    tables = {query.from_table}
    for join in query.joins:
        tables.add(join.table)
    for item in query.select:
        if isinstance(item, str):
            t, _ = parse_column_ref(item)
            tables.add(t)
        elif item.col != "*":
            t, _ = parse_column_ref(item.col)
            tables.add(t)
    return tables


def _where_column_refs(node: WhereNode) -> Iterator[str]:
    if isinstance(node, Predicate):
        if "." in node.col:
            yield node.col
        return
    for child in node.and_terms or node.or_terms or []:
        yield from _where_column_refs(child)


def _iter_column_refs(query: StructuredQuery) -> Iterator[str]:
    """Every Table.Column reference anywhere in the query — select, join
    keys, where, group_by, having, order_by, top_n — so column-level policy
    can't be bypassed by filtering/sorting/grouping on a denied column
    without ever selecting it.
    """
    for item in query.select:
        if isinstance(item, str):
            yield item
        elif item.col != "*":
            yield item.col
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
    """Return every table touched by a query using the production policy walk.

    Candidate simulation uses this to scope mandatory-filter readiness to the
    same query graph that policy validation sees, without inspecting predicate
    values or compiling SQL.
    """
    tables = _collect_referenced_tables(query)
    for ref in _iter_column_refs(query):
        table, _column = parse_column_ref(ref)
        tables.add(table)
    return tables


def validate_policy(query: StructuredQuery, policy: Policy, connection_id: str) -> None:
    if not policy.enabled:
        raise PolicyViolationError(f"Connection {connection_id!r} is disabled by policy")

    if len(query.select) > policy.max_select_columns:
        raise PolicyViolationError(f"select exceeds max of {policy.max_select_columns} items")
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

    column_refs = list(_iter_column_refs(query))
    tables = referenced_tables(query)

    for table in tables:
        if not policy.table_allowed(table):
            raise PolicyViolationError(f"Table {table!r} is not accessible under the active policy")

    for ref in column_refs:
        t, c = parse_column_ref(ref)
        if not policy.column_allowed(t, c):
            raise PolicyViolationError(f"Column {ref!r} is not accessible under the active policy")


def validate_batch_size(count: int, policy: Policy) -> None:
    if count > policy.max_batch_size:
        raise PolicyViolationError(f"batch size {count} exceeds max of {policy.max_batch_size}")
