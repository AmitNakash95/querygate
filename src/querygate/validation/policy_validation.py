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
    iter_query_scopes,
    parse_column_ref,
    select_item_column_refs,
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


def _scope_where_predicate_count(query: StructuredQuery) -> int:
    return sum(1 for _ in _iter_where_predicates(query.where)) if query.where is not None else 0


def _enforce_tree_wide_caps(scopes: List[StructuredQuery], policy: Policy) -> None:
    """The count-based caps are enforced on the SUM across every scope in the
    query tree (TODO.md item 97), never per-level — otherwise a caller could put
    N joins in the outer query and N more in a subquery, doing 2N joins of work
    while each level is individually "within cap". For a non-nested query (one
    scope) each sum equals that query's own count, so behavior — including the
    error messages — is unchanged; the `(total N across ...)` suffix only appears
    once nesting is involved."""

    def _suffix(total: int, nested: bool) -> str:
        return f" (total {total} across the query and its subqueries)" if nested else ""

    nested = len(scopes) > 1

    total = sum(len(q.select) for q in scopes)
    if total > policy.max_select_columns:
        raise PolicyViolationError(
            f"select exceeds max of {policy.max_select_columns} items{_suffix(total, nested)}"
        )
    total = sum(len(q.joins) for q in scopes)
    if total > policy.max_joins:
        raise PolicyViolationError(
            f"joins exceeds max of {policy.max_joins}{_suffix(total, nested)}"
        )
    total = sum(len(q.group_by) for q in scopes)
    if total > policy.max_group_by:
        raise PolicyViolationError(
            f"group_by exceeds max of {policy.max_group_by} columns{_suffix(total, nested)}"
        )
    total = sum(_scope_where_predicate_count(q) for q in scopes)
    if total > policy.max_where_predicates:
        raise PolicyViolationError(
            f"where predicate count {total} exceeds max of {policy.max_where_predicates}"
        )
    total = sum(len(q.having) for q in scopes)
    if total > policy.max_where_predicates:
        raise PolicyViolationError(
            f"having predicate count {total} exceeds max of {policy.max_where_predicates}"
        )
    total = sum(len(q.top_n.partition_by) for q in scopes if q.top_n is not None)
    if total > policy.max_partition_by:
        raise PolicyViolationError(
            f"top_n.partition_by exceeds max of {policy.max_partition_by} columns"
            f"{_suffix(total, nested)}"
        )
    total = sum(q.top_n.n for q in scopes if q.top_n is not None)
    if total > policy.max_top_n:
        raise PolicyViolationError(
            f"top_n.n exceeds max of {policy.max_top_n}{_suffix(total, nested)}"
        )


def _validate_scope(query: StructuredQuery, policy: Policy) -> None:
    """Per-scope checks (applied to the outer query AND each subquery
    independently): the non-summable caps and the column allow/deny + masked-
    column rule against THIS scope's own tables (item 97 — a subquery's base
    columns get the full treatment, resolved against the subquery's own name map,
    never the outer's)."""
    for item in query.select:
        if isinstance(item, CaseSelectItem) and len(item.when) > policy.max_case_branches:
            raise PolicyViolationError(
                f"case when branches exceeds max of {policy.max_case_branches}"
            )
    if query.where is not None:
        depth = where_depth(query.where)
        if depth > policy.max_where_depth:
            raise PolicyViolationError(
                f"where nesting depth {depth} exceeds max {policy.max_where_depth}"
            )

    all_predicates: List[Predicate] = list(query.having)
    if query.where is not None:
        all_predicates.extend(_iter_where_predicates(query.where))
    for pred in all_predicates:
        # A value_subquery predicate has no literal list to size; it's validated
        # as its own scope. Only literal in/not_in lists have a max_in_list_size.
        if pred.op in ("in", "not_in") and pred.value is not None:
            if len(pred.value) > policy.max_in_list_size:
                target = pred.col if pred.col is not None else f"{pred.col_fn.fn}(...)"
                raise PolicyViolationError(
                    f"{pred.op} list for {target!r} exceeds max_in_list_size of "
                    f"{policy.max_in_list_size} items"
                )

    # One walk of the canonical visitor (this scope only — it does not descend
    # into value_subquery) feeds both the table/column allow-deny checks and the
    # masked-column rule — no second parallel enumeration.
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
    # Applied per scope, so a masked column can't hide inside a subquery either.
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


def _validate_subquery_constraints(scoped: List, policy: Policy) -> None:
    """Item 97 minimal-safe subset: `value_subquery` (IN (subquery)) is allowed
    ONLY as a WHERE predicate, and its single output column must not be a masked
    column (the subquery output feeds an IN comparison — a non-projection use — so
    the item-49 masked-column rule applies to it, even though it is a bare
    projection *within* the subquery scope)."""
    for depth, scope in scoped:
        for pred in scope.having:
            if pred.value_subquery is not None:
                raise PolicyViolationError(
                    "IN (subquery) is only supported in a WHERE clause, not HAVING (item 97)"
                )
        for item in scope.select:
            if isinstance(item, CaseSelectItem):
                for branch in item.when:
                    if branch.when.value_subquery is not None:
                        raise PolicyViolationError(
                            "IN (subquery) is only supported in a WHERE clause, not a CASE "
                            "condition (item 97)"
                        )
        if depth > 0:
            # This scope is a subquery: its single select item is the value set
            # feeding IN. A masked column may not be used there.
            name_to_physical = effective_name_map(scope)
            for ref in select_item_column_refs(scope.select[0]):
                t, c = parse_column_ref(ref)
                physical_t = name_to_physical.get(t.lower(), t)
                if policy.column_mask(physical_t, c) is not None:
                    raise PolicyViolationError(
                        f"Column {ref!r} is masked by policy and cannot be a subquery's "
                        "IN (subquery) output"
                    )


def validate_policy(query: StructuredQuery, policy: Policy, connection_id: str) -> None:
    if not policy.enabled:
        raise PolicyViolationError(f"Connection {connection_id!r} is disabled by policy")

    # Enumerate the query and every nested value_subquery (item 97) as independent
    # scopes. For a non-nested query this is just [query].
    scoped = list(iter_query_scopes(query))
    max_depth = max(depth for depth, _ in scoped)
    if max_depth > policy.max_subquery_depth:
        raise PolicyViolationError(
            f"subquery nesting depth {max_depth} exceeds max_subquery_depth of "
            f"{policy.max_subquery_depth}"
        )
    _validate_subquery_constraints(scoped, policy)
    scopes = [q for _, q in scoped]

    # Count caps summed tree-wide, then per-scope semantics for each scope.
    _enforce_tree_wide_caps(scopes, policy)
    for scope in scopes:
        _validate_scope(scope, policy)


def validate_batch_size(count: int, policy: Policy) -> None:
    if count > policy.max_batch_size:
        raise PolicyViolationError(f"batch size {count} exceeds max of {policy.max_batch_size}")
