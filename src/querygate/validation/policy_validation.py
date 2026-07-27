"""Policy enforcement: caps + allow/deny lists, checked before compilation.

This runs BEFORE validation/schema_validation.py reflects anything, so a
disabled connection or an over-cap query never even touches the database.
"""

from __future__ import annotations

from typing import List, Optional, Set

from querygate.core.exceptions import PolicyViolationError
from querygate.policy.models import Policy
from querygate.query_ast.models import (
    CaseExpr,
    DateAddExpr,
    Predicate,
    StructuredQuery,
    WhereNode,
    WindowSelectItem,
    interval_magnitude_days,
)
from querygate.validation.schema_validation import (
    RefPosition,
    effective_name_map,
    expression_depth,
    iter_column_refs,
    iter_expression_nodes,
    iter_join_condition_predicates,
    iter_join_conditions,
    iter_query_scopes,
    iter_scope_case_conditions,
    iter_scope_expressions,
    iter_where_predicates,
    parse_column_ref,
    select_item_column_refs,
    where_depth,
)


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


def referenced_tables_tree_wide(query: StructuredQuery) -> Set[str]:
    """Every physical table the query touches across EVERY scope — the outer
    query, each set-operation arm (item 104) and each nested `value_subquery`
    (item 97).

    Deliberately a separate function rather than a change to `referenced_tables`:
    that one is called PER SCOPE by `_validate_scope`, where scope-local is the
    correct and load-bearing behavior (a scope's aliases mean nothing outside it).
    This one exists for the *reporting* surfaces that describe a whole request —
    where answering for the outer scope alone under-reports what will actually be
    read (TODO.md item 121).
    """
    tables: Set[str] = set()
    for _depth, scope in iter_query_scopes(query):
        tables |= referenced_tables(scope)
    return tables


def _scope_where_predicate_count(query: StructuredQuery) -> int:
    return sum(1 for _ in iter_where_predicates(query.where)) if query.where is not None else 0


def _scope_having_predicate_count(query: StructuredQuery) -> int:
    """HAVING is a WhereNode (item 99); count every predicate in its boolean
    tree, not a flat list length."""
    return sum(1 for _ in iter_where_predicates(query.having)) if query.having is not None else 0


def _scope_join_condition_predicate_count(query: StructuredQuery) -> int:
    """Every predicate across every join `condition` (item 103) in this scope.
    A range join's condition is a WhereNode like any other, so its boolean breadth
    is budgeted rather than left unbounded — otherwise `max_where_predicates`
    would bound the WHERE clause while a 500-predicate ON clause sailed past."""
    return sum(1 for _ in iter_join_condition_predicates(query))


def _scope_case_condition_predicate_count(query: StructuredQuery) -> int:
    """Every predicate across every searched-CASE condition (item 99) reachable
    in this scope — a `when` is a WhereNode, so its boolean breadth is counted
    and bounded like WHERE/HAVING, not left unbounded. Since item 100 a CASE can
    sit anywhere an Expression can (inside an aggregate argument, inside
    arithmetic, inside a WHERE predicate), so this walks
    `iter_scope_case_conditions` rather than only the select list — otherwise
    burying the CASE one level deeper would dodge the budget entirely."""
    return sum(1 for cond in iter_scope_case_conditions(query) for _ in iter_where_predicates(cond))


def _scope_windows(query: StructuredQuery) -> List[WindowSelectItem]:
    """Every window projection in one scope (item 101)."""
    return [item for item in query.select if isinstance(item, WindowSelectItem)]


def _scope_expression_node_count(query: StructuredQuery) -> int:
    """Total `Expression` nodes this scope carries (item 100), across select
    items and predicates, counting every node of every tree."""
    return sum(1 for expr in iter_scope_expressions(query) for _ in iter_expression_nodes(expr))


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
    # Counted tree-wide (item 97), then handed to the SHARED rule so the read and
    # write paths cannot disagree on the limit or the message — only on the scope
    # they count over, which differs deliberately (a write has no subqueries).
    _check_predicate_count(sum(_scope_where_predicate_count(q) for q in scopes), policy, "where")
    _check_predicate_count(sum(_scope_having_predicate_count(q) for q in scopes), policy, "having")
    _check_predicate_count(
        sum(_scope_case_condition_predicate_count(q) for q in scopes), policy, "case condition"
    )
    _check_predicate_count(
        sum(_scope_join_condition_predicate_count(q) for q in scopes), policy, "join condition"
    )
    total = sum(_scope_expression_node_count(q) for q in scopes)
    if total > policy.max_expression_nodes:
        raise PolicyViolationError(
            f"expression node count {total} exceeds max of {policy.max_expression_nodes}"
            f"{_suffix(total, nested)}"
        )
    windows = [window for q in scopes for window in _scope_windows(q)]
    total = len(windows)
    if total > policy.max_window_specs:
        raise PolicyViolationError(
            f"window function count {total} exceeds max_window_specs of "
            f"{policy.max_window_specs}{_suffix(total, nested)}"
        )
    # One budget for every PARTITION BY in the query tree, whether it came from
    # `top_n` or from a window projection — they are the same cost (a partitioned
    # sort), so they share the cap rather than each getting their own N.
    total = sum(len(q.top_n.partition_by) for q in scopes if q.top_n is not None) + sum(
        len(window.over.partition_by) for window in windows
    )
    if total > policy.max_partition_by:
        raise PolicyViolationError(
            f"partition_by exceeds max of {policy.max_partition_by} columns"
            f"{_suffix(total, nested)}"
        )
    total = sum(q.top_n.n for q in scopes if q.top_n is not None)
    if total > policy.max_top_n:
        raise PolicyViolationError(
            f"top_n.n exceeds max of {policy.max_top_n}{_suffix(total, nested)}"
        )
    # Set-operation arms (item 104), counting the carrying query as arm 1. Summed
    # across scopes like every other count cap, so a second set operation inside an
    # `IN (subquery)` shares the one budget rather than getting its own. An arm is
    # itself a scope with `set_op is None`, so it contributes 0 and is never
    # double-counted; a query with no set operation contributes 0 too, which is why
    # this cannot reject anything that was legal before.
    total = sum(1 + len(q.set_op.arms) for q in scopes if q.set_op is not None)
    if total > policy.max_set_op_arms:
        raise PolicyViolationError(
            f"set operation combines {total} arms, exceeding max_set_op_arms of "
            f"{policy.max_set_op_arms}"
            + (
                " (set operations are disabled for this connection)"
                if policy.max_set_op_arms < 2
                else _suffix(total, nested)
            )
        )


def _check_where_depth(node: Optional[WhereNode], policy: Policy, label: str) -> None:
    """Enforce `max_where_depth` on any predicate tree (WHERE, HAVING, or a
    searched-CASE condition — item 99). A `None` node (unset clause) is a no-op."""
    if node is None:
        return
    depth = where_depth(node)
    if depth > policy.max_where_depth:
        raise PolicyViolationError(
            f"{label} nesting depth {depth} exceeds max {policy.max_where_depth}"
        )


def _check_in_list_size(pred: Predicate, policy: Policy) -> None:
    """Bound one `in`/`not_in` predicate's literal list. A value_subquery predicate
    has no literal list to size (it is validated as its own scope), so only literal
    lists have a `max_in_list_size`."""
    if pred.op not in ("in", "not_in") or pred.value is None:
        return
    if len(pred.value) <= policy.max_in_list_size:
        return
    if pred.col is not None:
        target = pred.col
    elif pred.col_fn is not None:
        target = f"{pred.col_fn.fn}(...)"
    else:
        target = "expression"
    raise PolicyViolationError(
        f"{pred.op} list for {target!r} exceeds max_in_list_size of "
        f"{policy.max_in_list_size} items"
    )


def _check_predicate_count(total: int, policy: Policy, label: str) -> None:
    """Bound how many predicate leaves a filter carries. Split out so the read path
    (which counts tree-wide across scopes, item 97) and the write path (one tree)
    share the rule and the message while counting over different scopes."""
    if total > policy.max_where_predicates:
        raise PolicyViolationError(
            f"{label} predicate count {total} exceeds max of {policy.max_where_predicates}"
        )


def enforce_predicate_shape_caps(node: WhereNode, policy: Policy, *, label: str) -> None:
    """The three shape caps for ONE predicate tree: nesting depth, predicate count,
    and `in`/`not_in` list size.

    All three RULES are shared with the read path — `_check_where_depth`,
    `_check_predicate_count` and `_check_in_list_size` are the single
    implementations, and reads reach them through
    `_validate_scope`/`_enforce_tree_wide_caps`. What differs, deliberately, is the
    SCOPE counted over: reads sum predicate counts tree-wide across subqueries
    (item 97), while a write filter is one tree. This composition adds no rule of
    its own, so a fourth shape cap added to a primitive applies to both paths.

    Without it a write filter was exempt from all three, so
    `delete … where id in [<huge list>]` rendered the whole list client-side before
    `max_affected_rows` was ever consulted — measured: 100,000 values compile and
    render to a 689 KB statement in ~25 ms, and beyond Postgres's 32,767-parameter
    limit (T-SQL's 2,100) the driver refuses the statement outright. The read path
    refused the same list at `max_in_list_size` (default 1,000); the write path
    turned it into either wasted CPU or a driver-level failure.
    """
    _check_where_depth(node, policy, label)
    predicates = list(iter_where_predicates(node))
    _check_predicate_count(len(predicates), policy, label)
    for pred in predicates:
        _check_in_list_size(pred, policy)


def _validate_scope(query: StructuredQuery, policy: Policy) -> None:
    """Per-scope checks (applied to the outer query AND each subquery
    independently): the non-summable caps and the column allow/deny + masked-
    column rule against THIS scope's own tables (item 97 — a subquery's base
    columns get the full treatment, resolved against the subquery's own name map,
    never the outer's)."""
    # Bound every scalar Expression tree this scope carries (item 100) before
    # anything walks it: depth per tree, and WHEN-branch breadth on every
    # CaseExpr wherever it sits. The tree-wide node budget is enforced in
    # `_enforce_tree_wide_caps`; these two are per-scope structural checks.
    for expr in iter_scope_expressions(query):
        depth = expression_depth(expr)
        if depth > policy.max_expression_depth:
            raise PolicyViolationError(
                f"expression nesting depth {depth} exceeds max " f"{policy.max_expression_depth}"
            )
        for node in iter_expression_nodes(expr):
            if isinstance(node, CaseExpr) and len(node.when) > policy.max_case_branches:
                raise PolicyViolationError(
                    f"case when branches exceeds max of {policy.max_case_branches}"
                )
            # The magnitude of one `date_add` shift (item 102) — a per-node
            # bound, like `max_window_frame_offset`, not a summed count: two
            # sibling shifts of N days are two independently bounded reaches.
            # NESTED shifts do compound (`date_add(date_add(x, -N), -N)` reaches
            # 2N), and that is deliberate: `max_expression_depth` already bounds
            # the nesting, so the worst case is depth x cap — accounted for when
            # the default was chosen (see Policy.max_interval_days).
            # Walking `iter_expression_nodes` (rather than the select list) is
            # what stops a date_add buried inside an aggregate argument, a CASE
            # branch, or a WHERE predicate's arithmetic from escaping the cap.
            if isinstance(node, DateAddExpr):
                days = interval_magnitude_days(node)
                if days > policy.max_interval_days:
                    raise PolicyViolationError(
                        f"date_add interval of {node.amount} {node.unit}(s) reaches "
                        f"{days} days, exceeding max_interval_days of "
                        f"{policy.max_interval_days}"
                    )

    for window in _scope_windows(query):
        # The only unbounded magnitudes in a window spec: a frame's N PRECEDING/
        # N FOLLOWING distance and a lag/lead row offset. Both are how far from the
        # current row the engine must reach, so one cap covers both.
        offsets = [window.offset] if window.offset is not None else []
        if window.over.frame is not None:
            offsets += [
                bound.offset
                for bound in (window.over.frame.start, window.over.frame.end)
                if bound.offset is not None
            ]
        for offset in offsets:
            if offset > policy.max_window_frame_offset:
                raise PolicyViolationError(
                    f"window row offset {offset} exceeds max_window_frame_offset of "
                    f"{policy.max_window_frame_offset}"
                )
        # k-anonymity (item 88) is enforced as `HAVING count(*) >= min_group_size`
        # on AGGREGATE queries. An aggregate window produces no group to filter —
        # `COUNT(*) OVER ()` would report a below-floor count that the aggregate
        # path suppresses, and no in-statement construct can re-impose the floor
        # without a QUALIFY/derived table. So when the floor is configured, an
        # aggregate window is rejected rather than silently exempted (maintainer
        # decision, 2026-07-26 Decision Log). Ranking/offset windows stay allowed:
        # they only surface values the caller may already project bare.
        if policy.min_group_size is not None and window.is_aggregate_window():
            raise PolicyViolationError(
                f"aggregate window function {window.fn!r} is not allowed when "
                f"min_group_size is set: the k-anonymity floor is enforced on grouped "
                "results, and a window aggregate has no group to apply it to. Use an "
                "aggregate query with group_by, or a ranking/offset window "
                "(row_number/rank/dense_rank/ntile/lag/lead/first_value/last_value)."
            )

    # A cartesian product is the one join shape whose cost is the PRODUCT of its
    # inputs rather than bounded by a key, and (item 103) the only join the graph
    # rule deliberately exempts from having to connect to anything. So it is
    # deny-by-default and must be turned on per connection.
    if not policy.allow_cross_join:
        for join in query.joins:
            if join.type == "cross":
                raise PolicyViolationError(
                    f"cross join to {join.table!r} is not allowed under the active policy "
                    "(allow_cross_join is off): a cross join multiplies its inputs "
                    "row-for-row. Use an inner/left join with a condition, or ask an "
                    "operator to enable allow_cross_join for this connection."
                )

    # In-list-size is checked on every predicate the scope can carry: WHERE tree,
    # HAVING tree, every searched-CASE condition tree (item 99, including the
    # CASEs item 100 lets sit inside an expression), and every join condition
    # (item 103).
    all_predicates: List[Predicate] = []
    for _join, condition in iter_join_conditions(query):
        # A join condition is a WhereNode — depth-bound it exactly like
        # WHERE/HAVING/CASE so an ON clause cannot be a compile-time DoS.
        _check_where_depth(condition, policy, "join condition")
        all_predicates.extend(iter_where_predicates(condition))
    for condition in iter_scope_case_conditions(query):
        # A searched-CASE condition (item 99) is a WhereNode — depth-bound it
        # exactly like WHERE/HAVING so a deeply-nested condition can't be a
        # compile-time DoS.
        _check_where_depth(condition, policy, "case condition")
        all_predicates.extend(iter_where_predicates(condition))
    _check_where_depth(query.where, policy, "where")
    _check_where_depth(query.having, policy, "having")
    for node in (query.where, query.having):
        if node is not None:
            all_predicates.extend(iter_where_predicates(node))
    for pred in all_predicates:
        _check_in_list_size(pred, policy)

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
        if scope.having is not None:
            for pred in iter_where_predicates(scope.having):
                if pred.value_subquery is not None:
                    raise PolicyViolationError(
                        "IN (subquery) is only supported in a WHERE clause, not HAVING (item 97)"
                    )
        # Every reachable CASE condition, not just a top-level CaseSelectItem's:
        # since item 100 a CASE can be nested inside an aggregate argument or a
        # WHERE predicate's arithmetic, and a subquery there would never be
        # enumerated by `iter_query_scopes` (which only descends WHERE/HAVING
        # predicates) — so it would reach the compiler unvalidated. Rejecting it
        # here keeps that fail-closed AND gives a clean typed error.
        for condition in iter_scope_case_conditions(scope):
            for pred in iter_where_predicates(condition):
                if pred.value_subquery is not None:
                    raise PolicyViolationError(
                        "IN (subquery) is only supported in a WHERE clause, not a CASE "
                        "condition (item 97)"
                    )
        # Same reasoning for a join condition (item 103): `iter_query_scopes`
        # descends WHERE/HAVING predicates only, so a subquery hidden in an ON
        # clause would never be enumerated as a scope and would reach the compiler
        # with no policy/schema validation of its own. Rejected here so it fails
        # closed with a typed error rather than on a compiler-internal `ctx=None`.
        for pred in iter_join_condition_predicates(scope):
            if pred.value_subquery is not None:
                raise PolicyViolationError(
                    "IN (subquery) is only supported in a WHERE clause, not a join "
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
