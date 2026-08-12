"""Policy enforcement: caps + allow/deny lists, checked before compilation.

This runs BEFORE validation/schema_validation.py reflects anything, so a
disabled connection or an over-cap query never even touches the database.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from querygate.core.auth import Principal
from querygate.core.exceptions import PolicyViolationError
from querygate.policy.models import ColumnMask, Policy
from querygate.query_ast.models import (
    CaseExpr,
    DateAddExpr,
    Predicate,
    StructuredQuery,
    WhereNode,
    WindowExpr,
    WindowSelectItem,
    interval_magnitude_days,
    is_aggregate_scope,
)
from querygate.validation.schema_validation import (
    ConnectionResolver,
    RefPosition,
    cte_source_names,
    iter_correlations,
    declared_cte_names,
    effective_name_map,
    expression_depth,
    iter_column_refs,
    iter_expression_nodes,
    iter_join_condition_predicates,
    resolve_scope_connections,
    iter_join_conditions,
    iter_query_scopes,
    iter_scope_case_conditions,
    iter_scope_expressions,
    iter_scope_expressions_by_position,
    iter_where_predicates,
    parse_column_ref,
    resolve_table_policies,
    select_item_column_refs,
    where_depth,
)


def referenced_tables(query: StructuredQuery, cte_names: Optional[Set[str]] = None) -> Set[str]:
    """Return every PHYSICAL table touched by a query: the structural from/join
    tables, plus the physical table behind every column reference the canonical
    visitor (`iter_column_refs`) finds. Each ref's effective/alias name is
    mapped back through `effective_name_map`, so table-level policy is checked
    against the real table, never an alias.

    Candidate simulation uses this to scope mandatory-filter readiness to the
    same query graph that policy validation sees, without inspecting predicate
    values or compiling SQL.

    ``cte_names`` (item 105) are excluded, because a cte is not a table: there is
    nothing to allow, deny or filter at that name. Its *body* is a scope of its
    own, so the physical tables it reads are checked there, at the source, with the
    full treatment — which is why omitting the name here removes no enforcement.
    Passing them matters in both directions: an allow-list policy would otherwise
    reject every cte reference as an unknown table, and a report of "tables read"
    would name something that does not exist in the database.

    Omitting it derives the names from `query` itself rather than defaulting to
    "none" — deliberately, because the two differ exactly when it matters. A
    default of `frozenset()` is fail-OPEN: a future caller who forgets the
    argument silently gets pre-cte behavior and reports a block's name as a table.
    Self-defaulting makes the forgetful call correct for a root query and no worse
    than before for any other. `_validate_scope` still passes the ROOT's names
    explicitly, because a nested scope declares none of its own yet must know
    which of ITS names denote the statement's blocks.
    """
    if cte_names is None:
        cte_names = declared_cte_names(query)
    name_to_physical = effective_name_map(query)
    tables = {query.from_table, *(join.table for join in query.joins)}
    for column_ref in iter_column_refs(query):
        table, _column = parse_column_ref(column_ref.ref)
        tables.add(name_to_physical.get(table.casefold(), table))
    return {table for table in tables if table.casefold() not in cte_names}


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
    cte_names = declared_cte_names(query)
    tables: Set[str] = set()
    for _depth, scope in iter_query_scopes(query):
        tables |= referenced_tables(scope, cte_names)
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


def _reject_windows_outside_projections(query: StructuredQuery) -> None:
    """THE item-125 rule: a `WindowExpr` is legal in a projection and nowhere else.

    `WindowExpr` is the one `Expression` member that is not legal everywhere a
    scalar is expected, which is the property the rest of the item-100 substrate
    relies on. That exception is deliberate and recorded in
    `docs/PRODUCT_GUIDE.md`'s Decision Log (2026-07-27); this function is the
    single place it is enforced, so the substrate stays reviewable in ONE site
    even though the type no longer encodes the restriction.

    Three things are rejected, and all three are queries SQL itself refuses — so
    this converts a database error (or, worse, a dialect-dependent acceptance)
    into one typed rejection with no DB touch:

    1. a window anywhere other than a projection — WHERE/HAVING, a join
       condition, or an aggregate's argument (`SUM(SUM(x) OVER ())`);
    2. a window nested inside another window's `arg`;
    3. a window combined with `group_by` or an aggregate select item — the same
       incompatibility the AST layer already enforces for `WindowSelectItem`,
       carried over because a window is evaluated AFTER grouping, over columns
       that no longer exist per row.

    Matching on `position == "projection"` rather than on a denylist of the
    illegal positions is what makes it fail CLOSED: a position added to
    `iter_scope_expressions_by_position` later rejects windows by default instead
    of silently becoming a place one is allowed.
    """
    has_window = False
    for position, expr in iter_scope_expressions_by_position(query):
        windows = [node for node in iter_expression_nodes(expr) if isinstance(node, WindowExpr)]
        if not windows:
            continue
        has_window = True
        if position != "projection":
            raise PolicyViolationError(
                f"a window function is not allowed in a {position.replace('_', ' ')} — "
                "a window is evaluated after WHERE/GROUP BY, so SQL permits one only "
                "in a projection; filter on it by projecting it in one query and "
                "filtering that result in a second"
            )
        for window in windows:
            if window.arg is None:
                continue
            if any(isinstance(node, WindowExpr) for node in iter_expression_nodes(window.arg)):
                raise PolicyViolationError(
                    "a window function cannot be nested inside another window's "
                    "argument — no dialect allows it; project the inner window in "
                    "one query and window over that result in a second"
                )
    if has_window and is_aggregate_scope(query):
        raise PolicyViolationError(
            "a window function cannot be combined with group_by or aggregate "
            "select items — a window projects a value per row, so aggregate in "
            "one query and window over that result in a second query"
        )


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


def _table_connection_id(
    table_connection: Dict[str, str], effective_name: str, connection_id: str
) -> str:
    """The connection one effective (alias-or-table) name resolves to in a
    given scope's table_connection map, falling back to the query's primary
    connection when the map is empty (no `scope_connections` was threaded in
    at all — every pre-item-156 caller) or the name isn't a key in it (a
    same-connection table always seeds its own entry — see
    `resolve_scope_connections` — so "absent" only ever means the caller
    passed no map)."""
    return table_connection.get(effective_name.casefold(), connection_id)


def _table_allowed_everywhere(table: str, candidates: List[Policy]) -> bool:
    """A table is accessible only if EVERY candidate Policy allows it (item
    156): denied under either connection's Policy denies the query — the
    fail-closed direction, matching `sensitivity_approval_reasons`'s posture
    of "over-triggering ... is the safe direction; under-triggering ... is
    not." For a single-connection table `candidates` is `[policy]`, so this
    collapses to the original one-Policy check exactly."""
    return all(candidate.table_allowed(table) for candidate in candidates)


def _column_allowed_everywhere(table: str, column: str, candidates: List[Policy]) -> bool:
    """Column-level sibling of `_table_allowed_everywhere` — see its docstring."""
    return all(candidate.column_allowed(table, column) for candidate in candidates)


def _column_mask_anywhere(
    table: str, column: str, candidates: List[Policy]
) -> Optional[ColumnMask]:
    """The first configured `ColumnMask` across `candidates`, or None if none of
    them mask this column (item 156). Governed under EITHER connection's Policy
    means the mask applies — inverted from `_table_allowed_everywhere`'s AND: a
    mask is something that activates when any policy says so, not something
    every policy must agree on to avoid.

    `candidates` is ordered PRIMARY-connection-first (see
    `resolve_table_policies`'s docstring for why this order is load-bearing,
    not cosmetic) — so a mask configured only on the joined connection's own
    Policy is still found (the primary's `column_mask` returns `None`, so the
    walk falls through), but when BOTH connections configure a DIFFERENT mask
    on the same column, the PRIMARY connection's own mask is the one applied —
    matching the pre-item-156 default (which only ever consulted the primary)
    rather than letting a cross-connection join silently weaken an
    already-masked column's protection."""
    for candidate in candidates:
        mask = candidate.column_mask(table, column)
        if mask is not None:
            return mask
    return None


def _validate_scope(
    query: StructuredQuery,
    policy: Policy,
    cte_names: Set[str],
    *,
    connection_id: str,
    principal: Optional[Principal] = None,
    scope_connections: Optional[Dict[int, Dict[str, str]]] = None,
    connection_resolver: Optional[ConnectionResolver] = None,
) -> None:
    """Per-scope checks (applied to the outer query AND each subquery
    independently): the non-summable caps and the column allow/deny + masked-
    column rule against THIS scope's own tables (item 97 — a subquery's base
    columns get the full treatment, resolved against the subquery's own name map,
    never the outer's).

    ``cte_names`` (item 105) are the statement's cte names, case-folded — the names
    in this scope that denote a computed block rather than a table."""
    # Bound every scalar Expression tree this scope carries (item 100) before
    # anything walks it: depth per tree, and WHEN-branch breadth on every
    # CaseExpr wherever it sits. The tree-wide node budget is enforced in
    # `_enforce_tree_wide_caps`; these two are per-scope structural checks.
    _reject_windows_outside_projections(query)
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
    tables = referenced_tables(query, cte_names)
    name_to_physical = effective_name_map(query)
    # This scope's own table-to-connection map (item 156) — empty for every
    # pre-item-156 caller (no `scope_connections` threaded in), which makes
    # every `_table_connection_id` lookup below fall back to `connection_id`
    # and every `resolve_table_policies` call below collapse to `[policy]`,
    # so this whole block is byte-identical to the pre-156 shape unless a
    # caller opts in.
    table_connection = (scope_connections or {}).get(id(query), {})
    # A physical table can be reached through more than one effective name in
    # this scope (most commonly one; a self-join across two connections of a
    # same-named table is the one case it's more than one), and — since item
    # 156 — those names can resolve to DIFFERENT connections. Built from the
    # from/join structure (`name_to_physical`) so a bogus/undeclared alias
    # `referenced_tables` may have pulled in via a stray column ref (never a
    # real from/join name) simply isn't in this map and falls back to
    # `connection_id` alone below — the same single-connection check as
    # before item 156.
    physical_connections: Dict[str, Set[str]] = {}
    for alias_key, physical in name_to_physical.items():
        physical_connections.setdefault(physical, set()).add(
            _table_connection_id(table_connection, alias_key, connection_id)
        )

    for table in tables:
        for table_cx in physical_connections.get(table, {connection_id}):
            candidates = resolve_table_policies(
                table_cx, connection_id, policy, principal, connection_resolver
            )
            if not _table_allowed_everywhere(table, candidates):
                raise PolicyViolationError(
                    f"Table {table!r} is not accessible under the active policy"
                )

    for column_ref in all_refs:
        t, c = parse_column_ref(column_ref.ref)
        # t is the ref's effective name (an alias, or the table name itself)
        # — always resolve to the PHYSICAL table before checking column
        # policy, so an alias can never be used to dodge a denied column.
        physical_t = name_to_physical.get(t.casefold(), t)
        # A ref into a cte names one of that block's OUTPUT columns, not a column
        # of any table, so there is no physical (table, column) pair to check here
        # (item 105). Enforcement is not skipped, only relocated to where the name
        # means something: the block's own scope, where the real column it came
        # from is subject to the identical allow/deny and mask rules below. A
        # masked column additionally may not be projected by a cte at all —
        # `_validate_cte_constraints` — because a block's projection is an input to
        # another scope rather than a result handed to the caller.
        if physical_t.casefold() in cte_names:
            continue
        column_cx = _table_connection_id(table_connection, t, connection_id)
        candidates = resolve_table_policies(
            column_cx, connection_id, policy, principal, connection_resolver
        )
        if not _column_allowed_everywhere(physical_t, c, candidates):
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
        physical_t = name_to_physical.get(t.casefold(), t)
        if physical_t.casefold() in cte_names:
            continue  # a cte output name, not a physical column — see above
        column_cx = _table_connection_id(table_connection, t, connection_id)
        candidates = resolve_table_policies(
            column_cx, connection_id, policy, principal, connection_resolver
        )
        if _column_mask_anywhere(physical_t, c, candidates) is not None:
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
                # A SCALAR subquery is allowed in HAVING (item 106): comparing an
                # aggregate against another aggregate — HAVING SUM(x) > (SELECT
                # AVG(y)) — is the shape HAVING exists for, and the single-row
                # guarantee makes it well-defined. An `IN (subquery)` stays rejected
                # (item 97's rule, deliberately not widened), and so does EXISTS,
                # which asks a per-row question that has no meaning after grouping.
                if pred.value_subquery is not None and pred.op in ("in", "not_in"):
                    raise PolicyViolationError(
                        "IN (subquery) is only supported in a WHERE clause, not HAVING (item 97)"
                    )
                if pred.exists_subquery is not None:
                    raise PolicyViolationError(
                        "EXISTS is only supported in a WHERE clause, not HAVING — it tests "
                        "rows, and HAVING filters groups (item 106)"
                    )
        # Every reachable CASE condition, not just a top-level CaseSelectItem's:
        # since item 100 a CASE can be nested inside an aggregate argument or a
        # WHERE predicate's arithmetic, and a subquery there would never be
        # enumerated by `iter_query_scopes` (which only descends WHERE/HAVING
        # predicates) — so it would reach the compiler unvalidated. Rejecting it
        # here keeps that fail-closed AND gives a clean typed error.
        for condition in iter_scope_case_conditions(scope):
            for pred in iter_where_predicates(condition):
                if pred.value_subquery is not None or pred.exists_subquery is not None:
                    raise PolicyViolationError(
                        "a subquery is only supported in a WHERE clause, not a CASE "
                        "condition (items 97/106)"
                    )
        # Same reasoning for a join condition (item 103): `iter_query_scopes`
        # descends WHERE/HAVING predicates only, so a subquery hidden in an ON
        # clause would never be enumerated as a scope and would reach the compiler
        # with no policy/schema validation of its own. Rejected here so it fails
        # closed with a typed error rather than on a compiler-internal `ctx=None`.
        for pred in iter_join_condition_predicates(scope):
            if pred.value_subquery is not None or pred.exists_subquery is not None:
                raise PolicyViolationError(
                    "a subquery is only supported in a WHERE clause, not a join "
                    "condition (items 97/106)"
                )
        if depth > 0:
            # This scope is a subquery: its single select item is the value set
            # feeding IN. A masked column may not be used there.
            name_to_physical = effective_name_map(scope)
            for ref in select_item_column_refs(scope.select[0]):
                t, c = parse_column_ref(ref)
                physical_t = name_to_physical.get(t.casefold(), t)
                if policy.column_mask(physical_t, c) is not None:
                    raise PolicyViolationError(
                        f"Column {ref!r} is masked by policy and cannot be a subquery's "
                        "IN (subquery) output"
                    )


def _validate_cte_constraints(
    query: StructuredQuery, scoped: List, policy: Policy, cte_names: Set[str]
) -> None:
    """Every cte rule that needs to see the query's SCOPE TREE rather than one
    query object (item 105). Kept here, beside the `value_subquery` constraints it
    mirrors, so both scope-container rule sets read against the one
    `iter_query_scopes` authority instead of a second traversal in the AST layer.

    Ordered deliberately, and the order was CORRECTED during this item's own audit:
    `max_cte_count` is checked FIRST because it is the only O(1) rule here, while
    rule 2 walks every block's scope tree. Checking it last — the original order,
    chosen so a malformed block got a message naming its real problem — meant a
    caller could make the validator do O(N x tree) work with N far above the cap
    before being told the cap existed. Cheap bound first is the right shape for a
    guardrail, and it costs nothing in message quality: when a query IS over the
    cap, the cap message is the accurate one.
    """
    if len(query.ctes) > policy.max_cte_count:
        raise PolicyViolationError(f"ctes exceeds max of {policy.max_cte_count}")

    # 1. One declaration site. A cte declared on an arm, inside an IN (subquery) or
    #    inside another cte's body would be a second namespace that
    #    `declared_cte_names(root)` — which every consumer trusts as complete —
    #    does not contain, so those references would resolve against nothing.
    #    Written as "any scope that is not the root" rather than by enumerating the
    #    three containers, so a fourth container added later is covered by default.
    for scope in (q for _depth, q in scoped if q is not query):
        if scope.ctes:
            raise PolicyViolationError(
                "only the top-level query may declare `ctes` — a set_op arm, an "
                "IN (subquery) and another cte's query may not. Declare every block "
                "once at the top level; each is visible to the whole statement."
            )

    if not cte_names:
        return

    # 2. No forward or self reference. Declaration order is therefore dependency
    #    order, which `cte_chain_depths` relies on to charge max_subquery_depth,
    #    and which makes a RECURSIVE cte structurally inexpressible rather than
    #    merely forbidden — deliberately, since unbounded recursion is a real DoS
    #    and re-admitting it needs a hard iteration cap recorded separately.
    declared_so_far: Set[str] = set()
    for spec in query.ctes:
        name = spec.name.casefold()
        for source in sorted(cte_source_names(spec.query) & cte_names):
            if source not in declared_so_far:
                raise PolicyViolationError(
                    f"cte {spec.name!r} references {source!r}, which is declared later "
                    f"or is itself — a cte may only read an EARLIER one, so a recursive "
                    "cte is not expressible."
                )
        declared_so_far.add(name)

    # 3. No shadowing a physical table. SQL would let the cte win. That is not a
    #    policy bypass — a block's body is a full scope, so its tables are checked
    #    at the source. What it DOES break is the operator's ability to reason
    #    about their own policy: a cte name always wins name resolution, so a block
    #    called `orders` makes every rule the operator wrote about the table
    #    `orders` unreachable in this query, silently.
    #
    #    Measured while writing this rule's test: the concrete damage is not
    #    theoretical. `_apply_mandatory_row_filters` matches a filter by physical
    #    name, so a block named after a filtered table would have had that filter
    #    applied to the BLOCK'S OUTPUT — filtering the wrong rows if the block
    #    happens to project a column of that name, and raising a
    #    compiler-internal error if it does not.
    #
    #    So the rule is scoped to names the policy has an opinion about rather than
    #    to "any real table": that is the set where shadowing changes enforcement,
    #    it needs no reflection to check, and it cannot be written as "collides
    #    with a table used in this query" — the reference to the block IS such a
    #    use, which makes that phrasing circular (it silently never fires).
    #    `_apply_mandatory_row_filters` skips cte names too, as defence in depth.
    governed: Set[str] = {
        *(row_filter.table.casefold() for row_filter in policy.mandatory_row_filters),
        *(table.casefold() for table in policy.column_masks),
        *(table.casefold() for table in policy.denied_tables),
        *(table.casefold() for table in policy.allowed_tables),
        *(table.casefold() for table in policy.denied_columns),
        *(table.casefold() for table in policy.allowed_columns),
    }
    for spec in query.ctes:
        if spec.name.casefold() in governed:
            raise PolicyViolationError(
                f"cte name {spec.name!r} is also the name of a table this connection's "
                "policy has a rule for — a cte shadows that name, which would make the "
                "rule unverifiable here. Rename the cte."
            )

    # 4. No dead blocks. An unreferenced cte costs a scope against every tree-wide
    #    cap while contributing nothing, and SQLAlchemy would not render it — so
    #    accepting one means the caps count structure the SQL does not contain.
    referenced: Set[str] = set()
    for _depth, scope in scoped:
        referenced |= {
            scope.from_table.casefold(),
            *(join.table.casefold() for join in scope.joins),
        }
    for spec in query.ctes:
        if spec.name.casefold() not in referenced:
            raise PolicyViolationError(
                f"cte {spec.name!r} is declared but never referenced — reference it in "
                "`from` or a join's `table`, or remove it."
            )

    # 5. A masked column may not be a cte's output. Same rule and same reason as
    #    item 97's IN (subquery) output check directly above: within the block this
    #    is a bare projection, but the block's rows are an INPUT to another scope,
    #    where the value could be filtered, joined or ordered on — every position
    #    item 49 exists to keep an unmasked value out of.
    for spec in query.ctes:
        name_to_physical = effective_name_map(spec.query)
        for item in spec.query.select:
            for ref in select_item_column_refs(item):
                t, c = parse_column_ref(ref)
                physical_t = name_to_physical.get(t.casefold(), t)
                if policy.column_mask(physical_t, c) is not None:
                    raise PolicyViolationError(
                        f"Column {ref!r} is masked by policy and cannot be projected by "
                        f"cte {spec.name!r} — a cte's rows feed the rest of the query, "
                        "where a masked value could be filtered or joined on."
                    )


def _validate_correlation(
    query: StructuredQuery,
    scoped: List,
    policy: Policy,
    *,
    connection_id: str,
    principal: Optional[Principal] = None,
    scope_connections: Optional[Dict[int, Dict[str, str]]] = None,
    connection_resolver: Optional[ConnectionResolver] = None,
) -> None:
    """Enforce item 106's correlation model: declared, capped, and checked against
    the ENCLOSING scope.

    This is the item's whole safety argument in one function. SQL would make every
    outer column implicitly visible to a subquery; QueryGate requires each one to be
    named, so that each has a known position the enforcement layer can reach — and
    then applies the outer scope's OWN policy to it here. Checking a correlated ref
    against the subquery's name map instead would resolve `Customer.Id` against a
    scope that never declared `Customer`, which is how a correlated reference turns
    into a hole in table/column allow-deny.
    """
    correlations = list(iter_correlations(query))

    # A `correlate` list on a scope that is not a subquery would be silently inert —
    # nothing would ever read it — so it is rejected rather than ignored. Compared
    # by identity against the scopes `iter_correlations` actually descends into.
    correlated_children = {id(c.child) for c in correlations}
    for scope in (q for _depth, q in scoped):
        if scope.correlate and id(scope) not in correlated_children:
            raise PolicyViolationError(
                "`correlate` is only meaningful on a subquery (an exists_subquery or "
                "value_subquery) — the top-level query, a cte body and a set_op arm "
                "have no enclosing scope to correlate to."
            )

    if len(correlations) > policy.max_correlated_refs:
        raise PolicyViolationError(
            f"correlated references exceed max of {policy.max_correlated_refs}"
            + (
                f" (total {len(correlations)} across the query and its subqueries)"
                if len(correlations) > 0
                else ""
            )
        )

    # A declared ref the subquery never actually uses is dead structure: it widens
    # the nested scope's namespace and spends `max_correlated_refs` while
    # contributing nothing to the SQL. Rejected for the same reason item 105
    # rejects an unreferenced cte — the two containers should not disagree about
    # whether a declaration that does nothing is acceptable.
    for correlation in correlations:
        used = {ref.ref.casefold() for ref in iter_column_refs(correlation.child)}
        if correlation.ref.casefold() not in used:
            raise PolicyViolationError(
                f"correlate {correlation.ref!r} is declared but never referenced by the "
                "subquery — use it in the subquery, or remove it."
            )

    for correlation in correlations:
        # Resolved against the PARENT's name map — the load-bearing line.
        #
        # This ref is checked TWICE, deliberately. `_validate_scope` also walks the
        # subquery and sees the same ref as an ordinary WHERE reference, so the
        # generic table/column/mask rules apply to it there too. That redundancy is
        # why disabling the mask check below still leaves the query rejected — the
        # check earns its place by naming the correlation specifically, not by being
        # the only guard. Neither layer may be removed on the assumption the other
        # covers it: this one runs against the PARENT's name map, that one against
        # the child's, and only the parent's is correct for an aliased outer table.
        name_to_physical = effective_name_map(correlation.parent)
        table, column = parse_column_ref(correlation.ref)
        physical = name_to_physical.get(table.casefold(), table)
        # The PARENT scope's own table-to-connection map (item 156) — a
        # correlated ref names one of the parent's from/join tables, which may
        # itself have been reached through a cross-connection join.
        table_connection = (scope_connections or {}).get(id(correlation.parent), {})
        table_cx = _table_connection_id(table_connection, table, connection_id)
        candidates = resolve_table_policies(
            table_cx, connection_id, policy, principal, connection_resolver
        )
        if not _table_allowed_everywhere(physical, candidates):
            raise PolicyViolationError(
                f"Table {physical!r} is not accessible under the active policy"
            )
        if not _column_allowed_everywhere(physical, column, candidates):
            raise PolicyViolationError(
                f"Column {correlation.ref!r} is not accessible under the active policy"
            )
        # A correlated ref is a non-projection use by construction — it exists to be
        # compared inside the subquery — so item 49's rule applies with no position
        # test: a masked column may only surface as a bare top-level projection, and
        # this is never that.
        if _column_mask_anywhere(physical, column, candidates) is not None:
            raise PolicyViolationError(
                f"Column {correlation.ref!r} is masked by policy and cannot be used as a "
                "correlated reference — its raw value would be compared inside the subquery"
            )


def resolve_purpose_policy(query: StructuredQuery, policy: Policy) -> Policy:
    """Enforce the purpose gate (TODO.md item 145, feature F7) and return the
    effective, purpose-narrowed `Policy` for the rest of validation — and,
    through the caller's own reuse of the returned value, compilation — to
    use.

    `allowed_purposes` empty (the default) means this connection has not
    opted into purpose-gating at all: a declared `purpose` is accepted but
    has no effect, matching the "empty allow-list = unrestricted" convention
    every other `Policy` allow-list already uses. Once `allowed_purposes` is
    non-empty, every query on this connection must declare a purpose from
    that set — a missing or unrecognized purpose is rejected here, before
    any DB touch, the same posture as an unresolvable claim
    (`MandatoryRowFilter.resolve`). `Policy.for_purpose` guarantees the
    result never grants more than `policy` itself already allows.
    """
    if not policy.allowed_purposes:
        return policy
    if query.purpose is None:
        raise PolicyViolationError(
            "this connection requires a declared purpose (one of "
            f"{sorted(policy.allowed_purposes)}); none was given"
        )
    if query.purpose not in policy.allowed_purposes:
        raise PolicyViolationError(
            f"purpose {query.purpose!r} is not permitted on this connection "
            f"(allowed: {sorted(policy.allowed_purposes)})"
        )
    return policy.for_purpose(query.purpose)


def validate_structural_caps(
    query: StructuredQuery, policy: Policy
) -> Tuple[List[Tuple[int, StructuredQuery]], Set[str]]:
    """The cheap, connection-registry-free caps this module's docstring
    promises run FIRST: `max_cte_count` (via `_validate_cte_constraints`) and
    `max_subquery_depth`. Neither touches `PolicyStore`/the connection
    registry — both are pure AST-and-`Policy` arithmetic — which is exactly
    why they must run before anything that does (TODO.md item 160 finding 2).

    Extracted out of `validate_policy` so `execution/service.py`'s
    `_validate_and_compile` can call this FIRST, before it computes the
    per-scope table-to-connection map (`resolve_scope_connections`, which
    calls `PolicyStore.get()` once per cross-connection join) — restoring the
    "cheap bound before O(N x tree) work" ordering `_validate_cte_constraints`
    already documents as deliberate for its own two checks. `validate_policy`
    below also calls this (it must, to stay correct for every OTHER caller
    that never runs `_validate_and_compile`'s pre-check), so on the
    `_validate_and_compile` path these two caps are checked twice.

    **The two calls are NOT guaranteed to agree, and that is safe rather than
    a bug — correcting an earlier version of this docstring
    (`security-invariant-reviewer`, 2026-08-07).** `max_cte_count`/
    `max_subquery_depth` themselves genuinely never move under purpose
    narrowing (they are pure counts with no field `Policy.for_purpose` ever
    touches), but `_validate_cte_constraints` — which this function also
    calls — has two rules that DO read purpose-narrowable fields: a cte name
    may not shadow a table `denied_tables`/`denied_columns`/
    `mandatory_row_filters`/`column_masks` has a rule for, and a masked
    column may not be a cte's projection. `execution/service.py`'s
    `_validate_and_compile` calls this function once against the UN-narrowed
    Policy (before `resolve_purpose_policy` ever runs); `validate_policy`
    calls it again, internally, AFTER narrowing. A purpose delta that adds a
    NEW `denied_tables`/`column_masks` entry can therefore make the pre-check
    pass a query the internal, narrowed call correctly rejects —
    `test_structural_caps_pre_check_can_under_reject_a_purpose_narrowed_cte_
    shadow_but_validate_policy_still_catches_it` (`tests/unit/
    test_policy_validation.py`) pins exactly this. This is safe ONLY because
    `Policy.for_purpose` is additive-only — every field it touches is unioned
    or concatenated onto the base `Policy`, never replaced or removed (see
    its own docstring and `test_purpose_cannot_widen_a_base_deny`) — so the
    un-narrowed pre-check's governed-name set is always a SUBSET of the
    narrowed one's: the pre-check can under-reject (miss an early exit it
    could have taken) but never over-reject, and it can never cause a missed
    enforcement, because `validate_policy`'s own internal call to this
    function is the actual authority and runs unconditionally on every code
    path, whether or not `_validate_and_compile`'s pre-check also ran. If
    `PurposePolicyDelta` ever gains a subtractive field, this monotonicity
    argument breaks and the pre-check's presence would need re-examining —
    it must never be treated as making `validate_policy`'s own call
    redundant enough to remove.

    Returns `(scoped, cte_names)` — the same `iter_query_scopes`/
    `declared_cte_names` outputs `validate_policy` needs next — so a caller
    that also wants them (namely `validate_policy` itself) doesn't pay for a
    second AST walk just to get back what this one already computed.
    """
    cte_names = declared_cte_names(query)
    scoped = list(iter_query_scopes(query))
    _validate_cte_constraints(query, scoped, policy, cte_names)
    max_depth = max(depth for depth, _ in scoped)
    if max_depth > policy.max_subquery_depth:
        raise PolicyViolationError(
            f"subquery nesting depth {max_depth} exceeds max_subquery_depth of "
            f"{policy.max_subquery_depth}"
        )
    return scoped, cte_names


def validate_policy(
    query: StructuredQuery,
    policy: Policy,
    connection_id: str,
    *,
    principal: Optional[Principal] = None,
    scope_connections: Optional[Dict[int, Dict[str, str]]] = None,
    connection_resolver: Optional[ConnectionResolver] = None,
) -> Policy:
    """`scope_connections` (TODO.md item 156) is the same per-scope table-to-
    connection map `validation/schema_validation.py`'s `resolve_scope_connections`
    (or `validate_schema`'s own identically-shaped output) produces — one entry
    per scope, keyed by `id(scope)`, mapping that scope's own effective table
    names to the connection they resolve to. When given, a cross-connection
    join's table is checked against BOTH its own resolved connection's Policy
    and this `policy` (never a replacement of one for the other — see
    `resolve_table_policies`'s docstring), closing the gap where a column mask,
    mandatory row filter, or deny-list rule that exists only on the joined-in
    connection's own Policy was silently never consulted. `None` (every
    pre-item-156 caller, and every caller that never resolves a table outside
    `connection_id`) makes every check below behave exactly as it did before —
    `resolve_table_policies` collapses to `[policy]` whenever a table's
    resolved connection equals `connection_id`, which is what an empty/absent
    map always reports.
    """
    if not policy.enabled:
        raise PolicyViolationError(f"Connection {connection_id!r} is disabled by policy")

    # Purpose narrowing (item 145) runs first, before any other policy check,
    # and every check below reads the (possibly narrowed) `policy` this
    # rebinds to — the same one place the rest of the pipeline (the compiler,
    # via the caller's reuse of this function's return value) must also see,
    # so a purpose's additional deny/filter/mask rules are never checked here
    # but skipped at compile time.
    policy = resolve_purpose_policy(query, policy)

    # Enumerate the query, every cte body (item 105) and every nested
    # value_subquery (item 97) as independent scopes. For a plain query this is
    # just [query]. `validate_structural_caps` is the cheap, registry-free
    # pass (max_cte_count, max_subquery_depth) — see its own docstring for why
    # `_validate_and_compile` also calls it, earlier, on its own.
    scoped, cte_names = validate_structural_caps(query, policy)

    # TODO.md item 160 finding 3 (maintainer-approved 2026-08-09): a caller
    # that passes a `connection_resolver` (meaning it CAN resolve
    # cross-connection joins) but forgets `scope_connections` used to
    # silently fall back to primary-only enforcement — the exact shape that
    # let `admin/service.py`'s `simulate_candidate_policy` drift onto the
    # weak path before item 156 caught it. Self-deriving here closes that
    # footgun structurally rather than relying on every future call site to
    # remember. Placed AFTER `validate_structural_caps` (finding 2's
    # cheap-bound-first ordering) since `resolve_scope_connections` touches
    # the connection registry/policy store, not before it — a malformed AST
    # still gets rejected by the cheap caps before this runs. Every current
    # caller already passes both together or neither, so this is a no-op
    # change in behavior today; it only protects a future caller.
    if scope_connections is None and connection_resolver is not None:
        scope_connections = resolve_scope_connections(
            query, connection_id, principal=principal, connection_resolver=connection_resolver
        )

    _validate_subquery_constraints(scoped, policy)
    _validate_correlation(
        query,
        scoped,
        policy,
        connection_id=connection_id,
        principal=principal,
        scope_connections=scope_connections,
        connection_resolver=connection_resolver,
    )
    scopes = [q for _, q in scoped]

    # Count caps summed tree-wide, then per-scope semantics for each scope.
    _enforce_tree_wide_caps(scopes, policy)
    for scope in scopes:
        _validate_scope(
            scope,
            policy,
            cte_names,
            connection_id=connection_id,
            principal=principal,
            scope_connections=scope_connections,
            connection_resolver=connection_resolver,
        )

    return policy


def validate_batch_size(count: int, policy: Policy) -> None:
    if count > policy.max_batch_size:
        raise PolicyViolationError(f"batch size {count} exceeds max of {policy.max_batch_size}")
