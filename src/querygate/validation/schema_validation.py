"""Schema-truth validation: every identifier a query references must exist.

Complexity caps and table/column allow-deny policy are checked separately in
validation/policy_validation.py, before this module ever reflects anything.
"""

from __future__ import annotations

import datetime as dt
import decimal

import enum
from typing import Callable, Dict, FrozenSet, Iterator, NamedTuple, Optional, Set, Tuple

import sqlalchemy as sa

from querygate.core.auth import Principal
from querygate.connections.engine import get_engine, physical_db_name
from querygate.connections.models import ConnectionProfile
from querygate.connections.visibility import resolve_visible_connection
from querygate.core.exceptions import QueryValidationError
from querygate.policy.models import Policy
from querygate.query_ast.models import (
    AggregateSelectItem,
    ArrayAggSelectItem,
    BinaryOpExpr,
    CaseExpr,
    CaseSelectItem,
    CastExpr,
    ColArg,
    ColumnExpr,
    DateAddExpr,
    DateBucketSelectItem,
    Expression,
    ExpressionSelectItem,
    ExtractExpr,
    FunctionExpr,
    JoinSpec,
    LiteralExpr,
    NowExpr,
    PercentileContSelectItem,
    Predicate,
    ScalarFunctionSelectItem,
    CteSpec,
    SelectItem,
    StringAggSelectItem,
    StructuredQuery,
    WhereNode,
    WindowSelectItem,
    _AGGREGATE_SELECT_ITEM_TYPES,
)
from querygate.schema.reflection import get_table_schema, sanitize_table_name

ConnectionResolver = Callable[[str, Optional[Principal]], Tuple[ConnectionProfile, Policy]]


def parse_column_ref(col_ref: str) -> Tuple[str, str]:
    """Parse 'Table.Column' into (table, column). Bare names are rejected."""
    if "." not in col_ref:
        raise QueryValidationError(f"Column reference must be 'Table.Column', got {col_ref!r}")
    table, column = col_ref.split(".", 1)
    table = table.strip()
    column = column.strip()
    if not table or not column:
        raise QueryValidationError(f"Invalid column reference: {col_ref!r}")
    sanitize_table_name(table)
    return table, column


def effective_name_map(query: StructuredQuery) -> Dict[str, str]:
    """Map every declared effective name (an alias, or the physical table
    name itself when no alias is given), case-insensitively, to its physical
    table name (original casing). `StructuredQuery`'s own validator already
    guarantees effective names are unique and that a physical table repeated
    across from/joins (a self-join) always carries an explicit alias on
    every occurrence, so this mapping is always well-defined for any AST
    that passed Pydantic validation. Shared by policy validation (map an
    effective name back to the physical table/column policy checks must
    apply to) and schema validation/compilation (which physical table to
    reflect, and which occurrences need a SQL alias).
    """
    mapping: Dict[str, str] = {(query.from_alias or query.from_table).lower(): query.from_table}
    for join in query.joins:
        mapping[(join.alias or join.table).lower()] = join.table
    return mapping


class ExpressionPart(NamedTuple):
    """One element yielded by `iter_expression_parts`, the single canonical walk
    over an `Expression` tree.

    `node` is an `Expression` node, unless `is_condition` — in which case it is a
    `CaseExpr` branch's `when`, a `WhereNode` rather than an expression. That
    distinction is the reason this walk yields a tagged part instead of a plain
    node: collecting column refs has to cross out of the expression union into
    the boolean-condition layer, while the node/depth caps must not count the
    condition tree as expression structure.
    """

    depth: int
    node: object
    is_condition: bool


def iter_expression_parts(expr: Expression, _depth: int = 1) -> Iterator[ExpressionPart]:
    """THE single recursion over the closed `Expression` union (item 100).

    Everything else that needs to traverse an expression — column-ref collection
    (`expression_column_refs`), the node-count and depth caps
    (`iter_expression_nodes` / `expression_depth`), and the nested-CASE rules
    (`iter_expression_case_conditions`) — is a *filter* over this one walk rather
    than its own recursion. That matters because those four questions are all
    load-bearing for safety, and four hand-maintained walks over one union is
    exactly the drift failure class items 96 and 111 exist to prevent: a new
    union member added to three of four walks opens a silent hole in the fourth.

    It descends into expressions reachable ONLY through a `CaseExpr` branch's
    condition predicates (`CASE WHEN a > b * 2 THEN …`) — the deepest position
    the grammar allows.

    **Fails closed on an unknown node.** A future union member that reaches here
    without a branch raises rather than being silently yielded childless, which
    would contribute neither its refs (a policy/mask bypass) nor its size (a cap
    bypass). The closed union plus Pydantic validation means a caller cannot
    trigger this; only a code change can, and the tests below catch it.
    """
    yield ExpressionPart(_depth, expr, False)
    # `NowExpr` is a leaf like the other two: a clock reading has no operand and
    # carries no column ref, so there is nothing below it to visit.
    if isinstance(expr, (ColumnExpr, LiteralExpr, NowExpr)):
        return  # leaves
    if isinstance(expr, BinaryOpExpr):
        yield from iter_expression_parts(expr.left, _depth + 1)
        yield from iter_expression_parts(expr.right, _depth + 1)
        return
    if isinstance(expr, FunctionExpr):
        for arg in expr.args:
            yield from iter_expression_parts(arg, _depth + 1)
        return
    if isinstance(expr, CastExpr):
        yield from iter_expression_parts(expr.cast, _depth + 1)
        return
    if isinstance(expr, ExtractExpr):
        yield from iter_expression_parts(expr.extract, _depth + 1)
        return
    if isinstance(expr, DateAddExpr):
        yield from iter_expression_parts(expr.date_add, _depth + 1)
        return
    if isinstance(expr, CaseExpr):
        for branch in expr.when:
            yield ExpressionPart(_depth, branch.when, True)
            for pred in iter_where_predicates(branch.when):
                for nested in predicate_expressions(pred):
                    yield from iter_expression_parts(nested, _depth + 1)
            yield from iter_expression_parts(branch.then, _depth + 1)
        if expr.else_ is not None:
            yield from iter_expression_parts(expr.else_, _depth + 1)
        return
    raise QueryValidationError(
        f"Unsupported expression node {type(expr).__name__} — it was added to the "
        "Expression union without being taught to iter_expression_parts"
    )


def iter_expression_nodes(expr: Expression) -> Iterator[Expression]:
    """Every `Expression` node in a tree (a `CaseExpr` condition is a `WhereNode`,
    not an expression node, so it is excluded). Feeds `max_expression_nodes` and
    the per-`CaseExpr` branch cap."""
    return (part.node for part in iter_expression_parts(expr) if not part.is_condition)


def iter_expression_case_conditions(expr: Expression) -> Iterator[WhereNode]:
    """Every `CaseExpr` branch condition inside an expression tree, at any depth."""
    return (part.node for part in iter_expression_parts(expr) if part.is_condition)


def expression_column_refs(expr: Expression) -> Iterator[str]:
    """Every Table.Column ref anywhere inside an `Expression` tree — including
    the refs a nested `CaseExpr` branch's CONDITION carries, which are not
    `ColumnExpr` nodes at all but ordinary `Predicate` refs (`col`, `col_fn`
    args, `value_col`). An unvisited ref here is a silent policy AND masking
    bypass, the single most important rule in
    docs/ENGINE_EXPRESSIVENESS_PLAN.md §1.

    A condition contributes only its predicates' *direct* refs: any expression
    those predicates carry is descended into by the walk itself, so taking the
    full `predicate_column_refs` here would double-report them.
    """
    for part in iter_expression_parts(expr):
        if part.is_condition:
            for pred in iter_where_predicates(part.node):
                yield from predicate_direct_column_refs(pred)
        elif isinstance(part.node, ColumnExpr):
            yield part.node.col


def expression_depth(expr: Expression) -> int:
    """Nesting depth of an `Expression` tree, counting a `CaseExpr` branch's
    condition-predicate expressions as children too — so `max_expression_depth`
    bounds every path a caller can build, not just the arithmetic one."""
    return max(part.depth for part in iter_expression_parts(expr) if not part.is_condition)


def predicate_expressions(pred: Predicate) -> Iterator[Expression]:
    """The `Expression` trees a single predicate carries directly (item 100) —
    its computed left side and its computed right side."""
    if pred.expr is not None:
        yield pred.expr
    if pred.value_expr is not None:
        yield pred.value_expr


def select_item_expressions(item: SelectItem) -> Iterator[Expression]:
    """The `Expression` trees a single select item carries directly.
    `CaseSelectItem` is projected through `as_expression()` so CASE logic is
    walked by exactly one code path (see that method)."""
    if isinstance(item, ExpressionSelectItem):
        yield item.expr
    elif isinstance(item, CaseSelectItem):
        yield item.as_expression()
    elif isinstance(item, (AggregateSelectItem, WindowSelectItem)) and item.arg is not None:
        # A window's `arg` is an ordinary Expression (item 101), so the expression
        # depth/node caps, the nested-CASE rules, and the ref walk all reach into
        # a window argument with no extra wiring.
        yield item.arg


def iter_join_conditions(query: StructuredQuery) -> Iterator[Tuple[JoinSpec, WhereNode]]:
    """Every `(join, condition)` pair in one scope (TODO.md item 103).

    A join `condition` is a full `WhereNode`, so it is the fourth position — after
    WHERE, HAVING and a searched-CASE `when` — where a predicate tree can appear.
    This is the one place that set is enumerated; the shape caps, the expression
    caps, the column-ref visitor and the no-subquery rule all reach it through
    here rather than each re-walking `query.joins`.
    """
    for join in query.joins:
        if join.condition is not None:
            yield join, join.condition


def iter_join_condition_predicates(query: StructuredQuery) -> Iterator[Predicate]:
    """Every predicate leaf across every join condition in one scope (item 103)."""
    for _join, condition in iter_join_conditions(query):
        yield from iter_where_predicates(condition)


def iter_scope_expressions(query: StructuredQuery) -> Iterator[Expression]:
    """Every top-level `Expression` tree in ONE query scope: those carried by
    its select items and by the predicates of its WHERE/HAVING trees and of its
    join conditions (item 103). Not recursive into nested scopes
    (`value_subquery` is its own scope, walked by `iter_query_scopes`), and not
    recursive into the expressions themselves — callers compose this with
    `iter_expression_nodes` for the full walk.

    Join conditions belong here rather than in a parallel walk because every
    consumer of this function is a rule that must hold wherever an expression
    sits: the depth/node budgets, the `date_add` interval cap, the date-operand
    type check (item 117), and — via `iter_scope_case_conditions` — the CASE
    branch and condition-predicate budgets. Threading them in at the one shared
    source is what stops "a range join whose bound is a 200-node arithmetic tree"
    from being the position that escaped every one of them.
    """
    for item in query.select:
        yield from select_item_expressions(item)
    for pred in iter_where_and_having_predicates(query):
        yield from predicate_expressions(pred)
    for pred in iter_join_condition_predicates(query):
        yield from predicate_expressions(pred)


def iter_scope_case_conditions(query: StructuredQuery) -> Iterator[WhereNode]:
    """Every searched-CASE condition tree reachable in one scope, wherever the
    CASE sits — a `CaseSelectItem`, a `CaseExpr` inside an aggregate argument,
    or one buried in a WHERE predicate's arithmetic. The caps that bound a CASE
    condition (`max_where_depth`, the case-condition predicate budget,
    `max_in_list_size`) and the no-subquery-in-a-CASE rule all walk this, so a
    condition cannot escape them by being nested one level deeper.
    """
    for expr in iter_scope_expressions(query):
        yield from iter_expression_case_conditions(expr)


def select_item_column_refs(item: SelectItem) -> Iterator[str]:
    """Every Table.Column ref a single select item touches, across every
    variant — a bare string, an aggregate/date_bucket's `.col`, a scalar
    function's column-typed args, a CASE expression's when/then/else, any
    `Expression` the item carries (item 100), or a window's arg/partition/order
    refs (item 101). Shared by schema validation
    (which tables/columns to reflect/resolve) and policy validation (which
    refs column-level policy must check).
    """
    if isinstance(item, str):
        yield item
        return
    if isinstance(item, ScalarFunctionSelectItem):
        for arg in item.args:
            if isinstance(arg, ColArg):
                yield arg.col
        return
    if isinstance(item, WindowSelectItem):
        # A window carries refs in THREE places (item 101) — its `arg` expression,
        # its PARTITION BY, and its ORDER BY — and all three must be yielded or a
        # denied/masked column could ride into the query inside an OVER clause.
        # Must come before the Expression branch below, which would return early
        # after the `arg` alone.
        if item.arg is not None:
            yield from expression_column_refs(item.arg)
        yield from item.over.partition_by
        for order in item.over.order_by:
            yield order.col
        return
    # ExpressionSelectItem / CaseSelectItem / an aggregate with an `arg` are all
    # Expression-carrying; one walk covers every ref at any depth, including the
    # ones inside a CASE branch's condition (item 99's WhereNode).
    expressions = list(select_item_expressions(item))
    if expressions:
        for expr in expressions:
            yield from expression_column_refs(expr)
        return
    # DateBucketSelectItem / StringAggSelectItem / ArrayAggSelectItem /
    # PercentileContSelectItem, plus count(*)'s star form.
    if item.col != "*":
        yield item.col


def predicate_direct_column_refs(pred: Predicate) -> Iterator[str]:
    """The refs a Predicate carries WITHOUT descending into its expressions:
    `col` if dotted, each `ColArg` in `col_fn.args`, and `value_col`.

    Split out from `predicate_column_refs` for one caller —
    `expression_column_refs`, whose walk already descends into `expr`/
    `value_expr` itself and would otherwise report those refs twice.
    """
    if pred.col is not None:
        if "." in pred.col:
            yield pred.col
    elif pred.col_fn is not None:
        for arg in pred.col_fn.args:
            if isinstance(arg, ColArg):
                yield arg.col
    if pred.value_col is not None:
        yield pred.value_col


def predicate_column_refs(pred: Predicate) -> Iterator[str]:
    """Every Table.Column ref a Predicate touches: its direct refs (see above —
    a bare alias in `col`, valid only in HAVING, is skipped; enforcing that
    strictly is `_validate_predicate_columns`'s job, not this collector's) plus
    every ref inside a computed `expr`/`value_expr`. Single source of truth
    shared by policy validation's ref walk and this module's own
    table-collection/reflection logic.
    """
    yield from predicate_direct_column_refs(pred)
    for expression in predicate_expressions(pred):
        yield from expression_column_refs(expression)


class RefPosition(enum.Enum):
    """Where in a `StructuredQuery` a Table.Column reference appears.

    Rich enough to preserve the one distinction enforcement actually branches
    on: a *bare* top-level select projection item is the only position a
    masked column (TODO.md item 49) is allowed to appear — every other
    position would leak the raw value. `SELECT_NESTED` is a column inside a
    scalar-fn/CASE/aggregate/window select item, which is NOT a bare projection
    and so is subject to the masked-column rule like any other non-projection
    ref. A window's `arg`/PARTITION BY/ORDER BY refs (item 101) all land in
    `SELECT_NESTED` rather than taking positions of their own: every one of them
    is a non-projection use inside a select item, which is the only distinction
    enforcement branches on, and inventing three more members that no rule reads
    would be taxonomy without enforcement.
    """

    SELECT_PROJECTION_BARE = "select_projection_bare"
    SELECT_NESTED = "select_nested"
    JOIN_ON = "join_on"
    JOIN_EXTRA_ON = "join_extra_on"
    JOIN_CONDITION = "join_condition"
    WHERE = "where"
    GROUP_BY = "group_by"
    HAVING = "having"
    ORDER_BY = "order_by"
    TOP_N_PARTITION = "top_n_partition"
    TOP_N_ORDER = "top_n_order"


class ColumnRef(NamedTuple):
    position: RefPosition
    ref: str  # the 'Table.Column' string as written (effective/alias name . column)


def _where_column_refs(node: WhereNode) -> Iterator[str]:
    """Every Table.Column ref inside a (possibly nested) WHERE tree, in
    document order — layered on the single canonical `iter_where_predicates`
    walk (item 111) rather than re-recursing the boolean tree itself, so a new
    `WhereGroup` combinator is handled in exactly one place. The visitor below
    is the only caller.
    """
    for pred in iter_where_predicates(node):
        yield from predicate_column_refs(pred)


def iter_where_and_having_predicates(query: StructuredQuery) -> Iterator[Predicate]:
    """Every Predicate in a query's own WHERE tree and HAVING list (this scope
    only — does NOT descend into a predicate's `value_subquery`, which is a
    separate scope). The one place a `value_subquery` (item 97) can be attached."""
    if query.where is not None:
        yield from iter_where_predicates(query.where)
    if query.having is not None:
        yield from iter_where_predicates(query.having)


def iter_where_predicates(node: WhereNode) -> Iterator[Predicate]:
    """Every `Predicate` leaf in a (possibly nested) WHERE/HAVING/CASE boolean
    tree, in document order. This is the single canonical WHERE-predicate walk —
    the read policy/schema and write policy/schema validators all call it, so a
    new `WhereGroup` combinator (item 96's failure class) is handled in exactly
    one place instead of four hand-rolled copies. It does NOT descend into a
    predicate's `value_subquery` — that is a separate scope (see
    `iter_query_scopes`)."""
    if isinstance(node, Predicate):
        yield node
        return
    if node.not_terms is not None:
        yield from iter_where_predicates(node.not_terms)
        return
    for child in node.and_terms or node.or_terms or []:
        yield from iter_where_predicates(child)


def iter_set_op_arms(query: StructuredQuery) -> Iterator[StructuredQuery]:
    """This query and every further arm of its set operation (item 104), in the
    order they are combined. Just `[query]` when there is no `set_op`.

    Flat by construction: the AST forbids an arm from carrying its own `set_op`,
    so a set operation is always one N-ary list and this never needs to recurse.
    The one place "which SELECTs make up this statement's output" is answered, so
    the compiler, the audit shape and the applied-mask list cannot disagree on it.
    """
    yield query
    if query.set_op is not None:
        yield from query.set_op.arms


class Correlation(NamedTuple):
    """One declared correlated reference (item 106): `ref` is an outer column that
    `child` may read, and `parent` is the scope it must resolve against.

    Carrying the parent explicitly is the whole point. Correlation is the one place
    in the engine where a reference does NOT resolve against the scope it is written
    in, so every consumer has to be handed the right name map rather than reaching
    for the nearest one — which is precisely the mistake that would silently check a
    correlated column against the subquery's policy instead of the outer query's.
    """

    parent: StructuredQuery
    child: StructuredQuery
    ref: str


def iter_correlations(query: StructuredQuery) -> Iterator[Correlation]:
    """Every declared correlation in the query tree, paired with the scope it
    resolves against (item 106).

    THE single authority on the parent/child relationship. `iter_query_scopes`
    deliberately does not model it — it answers "what are the scopes", flattening
    the tree, and correlation is the one question whose answer depends on which
    scope contains which. Rather than give that walker a second meaning, this is a
    separate walk over the same structure, and everything that needs to enforce a
    correlated ref (policy allow/deny, the mask rule, schema resolution) consumes
    it instead of re-deriving parenthood.

    Only descends where a subquery can be attached, so a `correlate` list on a
    scope that is not a subquery is never yielded — `_validate_correlation` rejects
    that separately rather than letting it be silently inert.
    """
    for _depth, scope in iter_query_scopes(query):
        for pred in iter_where_and_having_predicates(scope):
            for nested in (pred.value_subquery, pred.exists_subquery):
                if nested is None:
                    continue
                for ref in nested.correlate:
                    yield Correlation(parent=scope, child=nested, ref=ref)


def declared_cte_names(query: StructuredQuery) -> FrozenSet[str]:
    """Lowercased names of every cte this query declares (item 105).

    Only the ROOT query may declare ctes — `_validate_cte_constraints` enforces
    that — so this is the whole namespace for the query tree, and the callers that
    need to tell "this from/join name is a cte" from "this is a physical table"
    take this one frozenset rather than re-deriving it per scope.
    """
    return frozenset(spec.name.lower() for spec in query.ctes)


def cte_source_names(query: StructuredQuery) -> Set[str]:
    """Every lowercased from/join *source* name used anywhere in this query's own
    scope tree — i.e. the names that could be resolving to a cte.

    Deliberately the `from`/`table` name and NOT the alias: `{"from": "daily",
    "from_alias": "d"}` reads the block called `daily` and calls it `d` here, so
    `daily` is the reference and `d` is local naming.
    """
    names: Set[str] = set()
    for _depth, scope in iter_query_scopes(query):
        names.add(scope.from_table.lower())
        names.update(join.table.lower() for join in scope.joins)
    return names


def cte_chain_depths(query: StructuredQuery) -> Dict[str, int]:
    """How deep each cte sits in the *reference chain*, by lowercased name — 1 for
    a block reading only physical tables, 2 for one reading a 1, and so on.

    This is what `max_subquery_depth` is charged for a cte, and it makes the
    default cap of 1 deny a cte-reading-a-cte until an operator raises it. Chained
    stages are real nesting: the database cannot start stage 2 until stage 1 has
    produced rows, exactly like an `IN (subquery)`.

    Declaration order is relied upon as topological order, which holds ONLY because
    `_validate_cte_constraints` rejects a forward reference. If that check were
    removed, a forward-referenced block would not yet be in `depths` and would be
    charged as though it read a physical table — so the two belong together, and
    the mutation test on the forward-reference rule covers this line too.
    """
    depths: Dict[str, int] = {}
    for spec in query.ctes:
        referenced = cte_source_names(spec.query) & set(depths)
        depths[spec.name.lower()] = 1 + max((depths[name] for name in referenced), default=0)
    return depths


def iter_query_scopes(
    query: StructuredQuery, _depth: int = 0
) -> Iterator[Tuple[int, StructuredQuery]]:
    """Yield `(depth, query)` for the outer query (depth 0), every cte body (item
    105), every set-operation arm (item 104), and every nested `value_subquery`
    (item 97), depth-first. Each
    yielded query is an INDEPENDENT validation scope: its column references resolve
    against its own from/join tables (never an outer scope's), which is exactly what
    makes an `IN (subquery)` structurally uncorrelated. Policy and schema validation
    walk these scopes so a subquery gets the full allow/deny + cap treatment, and so
    caps can be summed tree-wide (never per-level) to stop nesting being a
    cap-multiplier bypass.

    A set-op arm is yielded at the SAME depth as the query carrying the set
    operation, not one deeper: `max_subquery_depth` bounds caller-authored
    *nesting*, and an arm is a sibling SELECT, not a nesting level. Keeping the
    depth flat is also what stops an arm from being mistaken for an
    `IN (subquery)` scope by the `depth > 0` rules in `validate_schema` and
    `_validate_subquery_constraints` — while an arm of a set operation that sits
    INSIDE a subquery correctly inherits that subquery's depth and does get them.

    A cte body IS charged depth, unlike an arm: it is a stage the database must
    finish before the query reading it can start, which is the same cost shape as
    an `IN (subquery)` and not the sibling-SELECT shape of an arm. Its depth is its
    position in the reference chain (`cte_chain_depths`), so two INDEPENDENT ctes
    both cost 1 rather than the second being punished for being written second.
    """
    yield (_depth, query)
    if query.ctes:
        depths = cte_chain_depths(query)
        for spec in query.ctes:
            yield from iter_query_scopes(spec.query, _depth + depths[spec.name.lower()])
    if query.set_op is not None:
        for arm in query.set_op.arms:
            yield from iter_query_scopes(arm, _depth)
    for pred in iter_where_and_having_predicates(query):
        # Both subquery-bearing predicate shapes (items 97 and 106). Written as one
        # loop over both fields rather than two walks, so a third subquery-bearing
        # field cannot be added to one and forgotten in the other — the drift class
        # items 96 and 111 exist to prevent.
        for nested in (pred.value_subquery, pred.exists_subquery):
            if nested is not None:
                yield from iter_query_scopes(nested, _depth + 1)


def iter_column_refs(query: StructuredQuery) -> Iterator[ColumnRef]:
    """THE canonical reference visitor: yield one `ColumnRef(position, ref)` for
    every genuine Table.Column reference a query contains, across every position
    where one can appear — select (bare vs. nested), join `on`/`extra_on`, a join
    `condition` (item 103), where, group_by, having, order_by, and top_n
    partition/order.

    This is the single authority `CLAUDE.md`'s composable-interface doctrine
    asks for: policy validation (column allow/deny + the item-49 masked-column
    rule), `referenced_tables`, and schema validation's table-collection all
    consume this instead of each hand-maintaining its own parallel walk, so a
    new AST reference position is taught here once and enforced everywhere by
    construction (this is what makes item 97's nested subqueries safe to add).

    Positions that may legitimately reference a *select alias* rather than a
    real column (group_by, order_by, top_n) yield only their dotted
    `Table.Column` entries — a bare alias is not a column reference and is
    filtered here, exactly as the superseded walks did.
    """
    for item in query.select:
        if isinstance(item, str):
            yield ColumnRef(RefPosition.SELECT_PROJECTION_BARE, item)
        else:
            for ref in select_item_column_refs(item):
                yield ColumnRef(RefPosition.SELECT_NESTED, ref)
    for join in query.joins:
        for side in join.on or []:
            yield ColumnRef(RefPosition.JOIN_ON, side)
        for pair in join.extra_on:
            for side in pair:
                yield ColumnRef(RefPosition.JOIN_EXTRA_ON, side)
        if join.condition is not None:
            for ref in _where_column_refs(join.condition):
                yield ColumnRef(RefPosition.JOIN_CONDITION, ref)
    if query.where is not None:
        for ref in _where_column_refs(query.where):
            yield ColumnRef(RefPosition.WHERE, ref)
    for col_ref in query.group_by:
        if "." in col_ref:
            yield ColumnRef(RefPosition.GROUP_BY, col_ref)
    if query.having is not None:
        for ref in _where_column_refs(query.having):
            yield ColumnRef(RefPosition.HAVING, ref)
    for order in query.order_by:
        if "." in order.col:
            yield ColumnRef(RefPosition.ORDER_BY, order.col)
    if query.top_n is not None:
        for ref in query.top_n.partition_by:
            if "." in ref:
                yield ColumnRef(RefPosition.TOP_N_PARTITION, ref)
        for order in query.top_n.order_by:
            if "." in order.col:
                yield ColumnRef(RefPosition.TOP_N_ORDER, order.col)


def resolve_column(table: sa.Table, column_name: str) -> sa.Column:
    col_map = {c.name.lower(): c for c in table.c}
    key = column_name.lower()
    if key not in col_map:
        raise QueryValidationError(f"Column '{column_name}' not found in table '{table.name}'")
    return col_map[key]


def _date_bucket_alias(item: DateBucketSelectItem, tables: Dict[str, sa.Table]) -> str:
    if item.alias:
        return item.alias
    t, c = parse_column_ref(item.col)
    col = resolve_column(tables[t], c)
    return f"bucket_{col.name}_{item.granularity}"


def _aggregate_alias(item: AggregateSelectItem) -> str:
    """The output name an aggregate will carry. MUST stay identical to the
    compiler's own default-alias logic in `_build_select_columns` — `top_n`
    resolves its refs against the names this returns, so a divergence would
    reject a valid top_n (or accept one the compiler then can't resolve)."""
    if item.alias:
        return item.alias
    if item.arg is None:  # count(*)
        return f"{item.fn}_all"
    # A computed argument always carries an explicit alias (the AST requires
    # it), so an unaliased aggregate here is always over a bare column.
    _, col_name = parse_column_ref(item.arg.col)
    return f"{item.fn}_{col_name}"


def _string_agg_alias(item: StringAggSelectItem) -> str:
    if item.alias:
        return item.alias
    _, col_name = parse_column_ref(item.col)
    return f"string_agg_{col_name}"


def _array_agg_alias(item: ArrayAggSelectItem) -> str:
    if item.alias:
        return item.alias
    _, col_name = parse_column_ref(item.col)
    return f"array_agg_{col_name}"


def _percentile_cont_alias(item: PercentileContSelectItem) -> str:
    if item.alias:
        return item.alias
    _, col_name = parse_column_ref(item.col)
    return f"percentile_cont_{col_name}"


def _scalar_function_alias(item: ScalarFunctionSelectItem) -> str:
    if item.alias:
        return item.alias
    for arg in item.args:
        if isinstance(arg, ColArg):
            _, col_name = parse_column_ref(arg.col)
            return f"{item.fn}_{col_name}"
    return f"{item.fn}_result"


def select_item_output_name(item: SelectItem, tables: Dict[str, sa.Table]) -> str:
    """THE name one select item produces in the result — the single authority on
    select-item naming (item 105).

    It existed in two hand-maintained copies before this: the helpers above, and
    the `alias = item.alias or f"..."` lines inlined in the compiler's
    `_build_select_columns`. That was survivable while the only consumers were
    `top_n`'s rank targets and GROUP BY aliases, because a disagreement between
    them merely rejected a valid query. A cte makes it load-bearing: the validator
    resolves the OUTER query's `cte.column` refs against the names it predicts this
    block will project, while the compiler labels the real columns — so a
    divergence is either a valid query rejected or, worse, a ref validated against
    a name the compiled SQL does not have. One function removes the possibility;
    `test_cte.py` additionally asserts these names equal the compiled
    `CTE.c.keys()` for every select-item type, because agreeing with itself is not
    the same as agreeing with SQLAlchemy.

    A bare "Table.Column" resolves through the reflected table so the result
    carries the column's REAL casing, which is what the database returns.
    """
    if isinstance(item, str):
        table_name, column_name = parse_column_ref(item)
        return resolve_column(tables[table_name], column_name).name
    if isinstance(item, AggregateSelectItem):
        return _aggregate_alias(item)
    if isinstance(item, DateBucketSelectItem):
        return _date_bucket_alias(item, tables)
    if isinstance(item, StringAggSelectItem):
        return _string_agg_alias(item)
    if isinstance(item, ArrayAggSelectItem):
        return _array_agg_alias(item)
    if isinstance(item, PercentileContSelectItem):
        return _percentile_cont_alias(item)
    if isinstance(item, ScalarFunctionSelectItem):
        return _scalar_function_alias(item)
    if isinstance(item, (CaseSelectItem, ExpressionSelectItem, WindowSelectItem)):
        return item.alias
    raise QueryValidationError(
        f"Unsupported select item {type(item).__name__} — it was added to the "
        "SelectItem union without being given an output name here (item 105)"
    )


def _select_aliases(query: StructuredQuery, tables: Dict[str, sa.Table]) -> Set[str]:
    """Select-item output names `top_n` may rank by. A `WindowSelectItem`'s alias
    is deliberately absent (item 101): `top_n`'s rank is computed in the same
    SELECT, so ranking by a window's output would nest one window inside another's
    OVER clause, which no dialect allows. A bare-string item is absent for a
    different reason — `top_n` resolves those as real table columns, not aliases."""
    return {
        select_item_output_name(item, tables)
        for item in query.select
        if not isinstance(item, (str, WindowSelectItem))
    }


def groupable_select_aliases(query: StructuredQuery, tables: Dict[str, sa.Table]) -> Set[str]:
    """Select-item aliases a GROUP BY may legitimately reference: computed,
    NON-aggregate values. A date_bucket alias has always been groupable; item
    100 adds `ExpressionSelectItem`, which is what makes `GROUP BY <a CASE
    bucket>` expressible — the agent projects the CASE as an expression item
    and groups by its alias, rather than the engine growing a second inline
    grammar for group keys. Aggregate aliases stay excluded (grouping by an
    aggregate is not valid SQL)."""
    aliases = {
        _date_bucket_alias(item, tables)
        for item in query.select
        if isinstance(item, DateBucketSelectItem)
    }
    aliases |= {item.alias for item in query.select if isinstance(item, ExpressionSelectItem)}
    return aliases


def _where_depth(node: WhereNode, depth: int = 1) -> int:
    if isinstance(node, Predicate):
        return depth
    if node.not_terms is not None:
        return _where_depth(node.not_terms, depth + 1)
    children = node.and_terms or node.or_terms or []
    if not children:
        return depth
    return max(_where_depth(child, depth + 1) for child in children)


def where_depth(node: WhereNode) -> int:
    return _where_depth(node)


async def _load_table(connection_id: str, table_name: str, table_connection: str) -> sa.Table:
    """Reflect one table. Broken out so tests can monkeypatch this single seam
    instead of standing up a real database.
    """
    sanitize_table_name(table_name)
    engine = get_engine(connection_id)
    if table_connection == connection_id:
        return await get_table_schema(table_name, connection_id, engine)
    schema = f"{physical_db_name(table_connection)}.dbo"
    return await get_table_schema(table_name, connection_id, engine, schema=schema)


def resolve_query_table_connections(
    query: StructuredQuery,
    connection_id: str,
    principal: Optional[Principal] = None,
    connection_resolver: Optional[ConnectionResolver] = None,
    cte_names: Optional[Set[str]] = None,
) -> Dict[str, str]:
    """Resolve which connection each table belongs to, rejecting any join
    whose connection isn't in the same policy join_group as the primary.

    ``connection_resolver`` lets the config simulator run this exact
    production visibility/join-group rule against isolated candidate stores.
    Normal query execution leaves it unset and therefore uses the live stores.

    ``cte_names`` (item 105) are the statement's cte names, lowercased. A cte is
    computed by this statement rather than living in a database, so naming one as a
    cross-connection join target is rejected rather than quietly resolved against
    the primary connection — silently ignoring the field would tell an operator
    reading the query that a second connection was involved when it was not.
    """
    resolver = connection_resolver or (
        lambda target, actor: resolve_visible_connection(target, principal=actor)
    )
    primary, policy = resolver(connection_id, principal)
    primary_group = policy.join_group or primary.effective_join_group()

    known_ctes = cte_names or set()
    table_connection: Dict[str, str] = {(query.from_alias or query.from_table): connection_id}
    for join in query.joins:
        join_connection_id = join.connection or connection_id
        if join.connection is not None and join.table.lower() in known_ctes:
            raise QueryValidationError(
                f"join to cte {join.table!r} may not set `connection` — a cte is computed "
                "by this query, not read from another connection."
            )
        if join_connection_id != connection_id:
            other, other_policy = resolver(join_connection_id, principal)
            other_group = other_policy.join_group or other.effective_join_group()
            if primary_group != other_group:
                raise QueryValidationError(
                    f"cross-connection join: {join.table!r} is in connection "
                    f"{join_connection_id!r}, which is not in the same join_group as the "
                    f"primary connection {connection_id!r} — run a separate query per "
                    "connection and combine results instead."
                )
        table_connection[join.alias or join.table] = join_connection_id
    return table_connection


def _validate_join_graph(query: StructuredQuery) -> None:
    """Require each declared join to connect exactly one new table (by its
    effective name — alias if given, else its own table name) to the graph.

    A `cross` join is the deliberate exception (item 103): a cartesian product
    asserts there is no relationship, so demanding one would make the join type
    unusable. It is gated by `Policy.allow_cross_join` in policy validation
    instead, which the execution pipeline runs first.

    Do not read that ordering as a universal guarantee: this function is
    deliberately policy-free and is ALSO reachable from the policy-free template
    diagnostic (`admin/service.py`'s `_check_one_template_schema`, which calls
    `validate_schema` with no `validate_policy`). Nothing compiles or executes on
    that path, so it is not a bypass — but the cross gate must stay where it is
    rather than being "consolidated" here on the assumption policy always ran.
    """
    known = {(query.from_alias or query.from_table).lower()}
    for join in query.joins:
        joined_table = (join.alias or join.table).lower()
        if join.type == "cross":
            known.add(joined_table)
            continue

        if join.on is not None:
            left_t, _ = parse_column_ref(join.on[0])
            right_t, _ = parse_column_ref(join.on[1])
            sides = {left_t.lower(), right_t.lower()}
        else:
            # A general `condition` may legitimately be about more than the joined
            # pair (`JOIN c ON c.x = a.x AND c.y = b.y`), so the rule generalizes to
            # the set of tables it references rather than a fixed pair.
            assert join.condition is not None  # nosec B101 — the AST guarantees one form
            sides = {parse_column_ref(ref)[0].lower() for ref in _where_column_refs(join.condition)}

        if joined_table not in sides:
            raise QueryValidationError(
                f"Join condition for {join.table!r} must reference that table "
                f"(as {joined_table!r})"
            )
        # Every OTHER table the condition names must already be in the graph. The
        # first check is the original one — it is all an `on` pair can ever reach,
        # since that form names exactly one other side, so its message is
        # unchanged. The second is new and only a general `condition` can trigger
        # it: a multi-table condition that connects to the graph AND forward-
        # references a table joined later, which SQLAlchemy would render as a
        # broken or implicitly cartesian FROM rather than the join that was asked
        # for.
        others = sides - {joined_table}
        if not others & known:
            raise QueryValidationError(
                f"Join to {join.table!r} does not connect to the query table graph"
            )
        unknown = sorted(others - known)
        if unknown:
            raise QueryValidationError(
                f"Join to {join.table!r} references {unknown} before it is joined — a "
                "join condition may only reference the from table or an EARLIER join"
            )
        known.add(joined_table)

        for pair in join.extra_on:
            extra_t0, _ = parse_column_ref(pair[0])
            extra_t1, _ = parse_column_ref(pair[1])
            if {extra_t0.lower(), extra_t1.lower()} != sides:
                raise QueryValidationError(
                    f"extra_on pair {pair!r} for join to {join.table!r} must reference "
                    "the same two tables as `on` — a join's condition is always about "
                    "the one pair of tables it joins"
                )


async def validate_schema(
    query: StructuredQuery,
    connection_id: str,
    principal: Optional[Principal] = None,
    *,
    scope_tables: Optional[Dict[int, Dict[str, sa.Table]]] = None,
) -> Dict[str, sa.Table]:
    """Reflect + verify every table/column the query — every nested
    value_subquery (item 97) and every set-operation arm (item 104) — references
    exists.

    Returns the OUTER query's reflected tables, keyed by the name the query used,
    for the compiler. If `scope_tables` is provided, it is populated with each
    scope's reflected tables keyed by that scope query's `id`, so the compiler can
    recursively render `IN (subquery)` and each set-operation arm. Each subquery is
    validated as an
    independent scope (its refs resolve to its own tables — undeclared-table
    rejection is exactly what makes a correlated reference to an outer table fail),
    and a subquery is required to stay single-connection (cross-connection nesting
    is rejected, per item 97's minimal-safe subset)."""
    outer_tables: Optional[Dict[str, sa.Table]] = None
    # Always collected, even when the caller passes no `scope_tables`: the
    # cross-arm type check below compares scopes against EACH OTHER, so it needs
    # every scope's reflection regardless of whether a compiler wanted them.
    reflected: Dict[int, Dict[str, sa.Table]] = {}

    # Each cte becomes a table-like value keyed by the names it PROJECTS, so every
    # existing column-resolution path (`tables[name]`, `resolve_column`,
    # `_validate_select_columns`, …) works on a cte reference unchanged instead of
    # growing a parallel "is this a cte?" branch at each site. Built in declaration
    # order, which `_validate_cte_constraints`'s no-forward-reference rule makes
    # dependency order, so a block reading an earlier block finds it already here.
    cte_tables: Dict[str, sa.Table] = {}
    for spec in query.ctes:
        _reject_cross_connection_nesting(spec.query, connection_id, f"cte {spec.name!r}")
        body_tables = await _reflect_and_validate_scope(
            spec.query, connection_id, principal, cte_tables
        )
        reflected[id(spec.query)] = body_tables
        if scope_tables is not None:
            scope_tables[id(spec.query)] = body_tables
        cte_tables[spec.name.lower()] = _cte_projection_table(spec, body_tables)

    # A correlated subquery resolves its declared outer refs against the PARENT's
    # reflected tables, so a parent must be reflected before its children. The scope
    # walk is depth-first from the root, which is already that order.
    correlated: Dict[int, Dict[str, sa.Table]] = {}

    for depth, scope in iter_query_scopes(query):
        if id(scope) in reflected:
            continue  # a cte body, already validated above in dependency order
        if depth > 0:
            # Not "a nested IN (subquery)": this loop also reaches a set-operation
            # arm nested inside a subquery OR inside a cte body, and naming the
            # wrong container tells an operator they wrote a shape they did not.
            # The cte body itself is rejected above, where its name is known exactly.
            _reject_cross_connection_nesting(
                scope,
                connection_id,
                "a nested scope (an IN (subquery), or a set-operation arm within one)",
            )
        scoped_tables = await _reflect_and_validate_scope(
            scope, connection_id, principal, cte_tables, correlated.get(id(scope))
        )
        reflected[id(scope)] = scoped_tables
        if scope_tables is not None:
            scope_tables[id(scope)] = scoped_tables
        # Hand each of THIS scope's correlated children the outer tables they
        # declared, resolved here where the parent's name map is in hand. A ref that
        # does not resolve in the parent is rejected now, before the child is ever
        # validated, so the error names the scope the caller got wrong.
        for pred in iter_where_and_having_predicates(scope):
            for nested in (pred.value_subquery, pred.exists_subquery):
                if nested is None or not nested.correlate:
                    continue
                # The parent's OWN from/join/cte names — deliberately not
                # `scoped_tables`, which also holds the tables the PARENT itself
                # correlated to. Resolving against that made correlation
                # TRANSITIVE: a child could reach a grandparent's column simply
                # because its parent had declared it, so "one level" held in name
                # only. Measured 2026-07-27 by the grandparent case in
                # `test_correlation_boundary.py`, which this line is what fails.
                own_names = {n.lower() for n in effective_name_map(scope)}
                visible: Dict[str, sa.Table] = {}
                for ref in nested.correlate:
                    table_name, column_name = parse_column_ref(ref)
                    outer = scoped_tables.get(table_name)
                    if outer is not None and table_name.lower() not in own_names:
                        outer = None  # inherited by the parent, not the parent's own
                    if outer is None:
                        raise QueryValidationError(
                            f"correlate {ref!r} does not name a table of the enclosing "
                            f"query — a correlated reference reaches exactly one level "
                            "up, to the query containing this subquery."
                        )
                    resolve_column(outer, column_name)
                    visible[table_name] = outer
                correlated[id(nested)] = visible
        # Identity, not `depth == 0`: since item 104 a set-operation arm is also a
        # depth-0 scope, so a depth test would hand the compiler the LAST arm's
        # reflected tables as if they were the outer query's.
        if scope is query:
            outer_tables = scoped_tables
    if outer_tables is None:  # unreachable: iter_query_scopes always yields depth 0
        raise QueryValidationError("internal error: query had no top-level scope to validate")
    _validate_set_op_arm_types(query, reflected)
    return outer_tables


# The type FAMILY each `CastExpr.to` target produces. Kept beside the families
# below rather than derived from the compiler's `_CAST_TYPES`, because the two
# answer different questions (which SQLAlchemy type to emit vs. what a caller can
# union it with) — `test_set_operations.py` asserts the two keysets stay equal, so
# a new cast target cannot be added to one without the other.
_CAST_TARGET_FAMILIES: Dict[str, str] = {
    "text": "text",
    "integer": "numeric",
    "numeric": "numeric",
    "boolean": "boolean",
    "date": "temporal",
    "timestamp": "temporal",
}


def _python_type_family(resolved: type) -> Optional[str]:
    """Group a driver-reported Python type into the coarse family that decides
    whether two columns can be combined by a set operation.

    Coarse deliberately: `integer` vs `numeric` and `date` vs `timestamp` union
    fine on both backends, so splitting them would reject valid queries. `bool` is
    tested FIRST because it is a subclass of `int` in Python, and a boolean column
    unioned with an integer one is an error on Postgres.
    """
    if issubclass(resolved, bool):
        return "boolean"
    if issubclass(resolved, (int, float, decimal.Decimal)):
        return "numeric"
    if issubclass(resolved, str):
        return "text"
    if issubclass(resolved, (dt.date, dt.time)):
        return "temporal"
    return None


def _column_ref_type_family(ref: str, tables: Dict[str, sa.Table]) -> Optional[str]:
    table_name, column_name = parse_column_ref(ref)
    table = tables.get(table_name)
    if table is None:
        return None
    try:
        resolved = resolve_column(table, column_name).type.python_type
    except (NotImplementedError, AttributeError, QueryValidationError):
        return None  # unknown mapping: allow through rather than guess
    return _python_type_family(resolved) if isinstance(resolved, type) else None


def _expression_type_family(expr: Expression, tables: Dict[str, sa.Table]) -> Optional[str]:
    """Only the three expression shapes whose result type is knowable without
    reproducing each backend's type-promotion rules. Arithmetic, functions and
    CASE return None (unknown) and are left to the database — the same
    reject-only-what-is-known-wrong posture `_validate_date_operands` takes."""
    if isinstance(expr, CastExpr):
        return _CAST_TARGET_FAMILIES.get(expr.to)
    if isinstance(expr, LiteralExpr):
        if expr.literal is None:
            return None
        return _python_type_family(type(expr.literal))
    if isinstance(expr, ColumnExpr):
        return _column_ref_type_family(expr.col, tables)
    return None


def select_item_type_family(item: SelectItem, tables: Dict[str, sa.Table]) -> Optional[str]:
    """The coarse type family one select item projects, or None when it is not
    statically knowable. `None` always means "allow" — this rejects what is
    known-wrong, never what is merely unrecognized."""
    if isinstance(item, str):
        return _column_ref_type_family(item, tables)
    if isinstance(item, AggregateSelectItem) and item.fn == "count":
        return "numeric"
    if isinstance(item, DateBucketSelectItem):
        return "temporal"
    if isinstance(item, StringAggSelectItem):
        return "text"
    if isinstance(item, ExpressionSelectItem):
        return _expression_type_family(item.expr, tables)
    return None


def _validate_set_op_arm_types(
    query: StructuredQuery, reflected: Dict[int, Dict[str, sa.Table]]
) -> None:
    """Reject a set operation whose arms project incompatible types at the same
    position (TODO.md item 104).

    This closes a MEASURED cross-dialect divergence, not a hypothetical one. With
    arm 1 projecting an integer column and arm 2 a text CAST of it:

    * Postgres **errors** (`UNION types integer and text cannot be matched`);
    * SQL Server **succeeds**, applying data-type precedence to convert the
      varchar side back to int and returning rows.

    So the identical AST is a hard failure on one backend and an answer on the
    other — the items 75/82 class this project treats as a defect rather than a
    quirk. Catching it here makes it one typed pre-database rejection everywhere.

    Deliberately narrow, exactly like `_validate_date_operands`: only positions
    where **two or more arms** have a statically-knowable family are compared, and
    the families are coarse (integer/numeric agree; date/timestamp agree), so a
    valid query is never rejected for a difference the backends accept.
    """
    for scope in (q for _depth, q in iter_query_scopes(query) if q.set_op is not None):
        arms = list(iter_set_op_arms(scope))
        arm_tables = [reflected.get(id(arm), {}) for arm in arms]
        for position in range(len(arms[0].select)):
            seen: Dict[str, str] = {}  # family -> the ref/description that produced it
            for arm, tables in zip(arms, arm_tables):
                family = select_item_type_family(arm.select[position], tables)
                if family is None:
                    continue
                seen.setdefault(family, arm.from_table)
                if len(seen) > 1:
                    families = sorted(seen)
                    # Worded to avoid the SQL keywords `select`/`from`: bandit's
                    # B608 pattern-matches an f-string carrying both and flags this
                    # prose as a possible injection vector. Suppressing the rule
                    # would blunt it repo-wide for a real finding later, so the
                    # message says "projection"/"arm over" instead. No caller value
                    # reaches SQL from here — this string is an error, not a query.
                    raise QueryValidationError(
                        f"set operation arms disagree on the type of projection "
                        f"{position + 1}: {families[0]} (arm over "
                        f"{seen[families[0]]!r}) vs {families[1]} (arm over "
                        f"{seen[families[1]]!r}). Postgres rejects a mismatched union "
                        "outright while SQL Server may silently convert one side, so "
                        "this is refused on every dialect. Cast both sides to the same "
                        "type if the combination is intended."
                    )


def _reject_cross_connection_nesting(scope: StructuredQuery, connection_id: str, what: str) -> None:
    """A NESTED scope stays single-connection — item 97's minimal-safe subset.

    Shared by both nested containers rather than written inline for one, because
    it was inline for one: item 105 added cte bodies to the scope tree but
    validated them in their own dependency-ordered loop, which ran BEFORE the
    `depth > 0` branch this check lived in. The result was measurable and wrong —
    the identical cross-connection join was rejected inside an `IN (subquery)` and
    ACCEPTED inside a cte, so the restriction depended on which container a caller
    picked. `what` names the real container so the message cannot claim a shape
    the caller did not write.

    Not a bypass either way (`resolve_query_table_connections` still enforces the
    `join_group` rule on every scope), but a guardrail that applies to one nested
    container and not its sibling is a guardrail an operator cannot reason about.
    """
    for join in scope.joins:
        if join.connection is not None and join.connection != connection_id:
            raise QueryValidationError(
                f"cross-connection subquery: {what} may not join to another connection "
                f"({join.connection!r}) — run a separate query per connection and "
                "combine results instead (item 97)."
            )


def _cte_projection_table(spec: CteSpec, body_tables: Dict[str, sa.Table]) -> sa.Table:
    """The table-like shape a cte presents to whatever reads it: one column per
    select item, named by `select_item_output_name` (item 105).

    Typeless on purpose. This exists to answer "does `daily.n` name something this
    block projects?" pre-database, and a wrong *type* guess here would be worse
    than none — the set-operation arm check (`select_item_type_family`) already
    owns the narrow, deliberately-incomplete type question, and reproducing it
    would give a second, disagreeing answer.

    **Duplicate output names are rejected rather than disambiguated**, and that is
    a correctness rule, not a limitation. A block's columns are referred to BY NAME
    from the outer query, so `select: ["orders.id", "customers.id"]` leaves
    `block.id` with no defined meaning. SQLAlchemy would quietly rename the second
    to `id_1`, which is the *exact* failure item 119 records on `top_n`: the caller
    asked for two columns, one silently becomes unreachable under the name they
    used. Predicting that renaming instead would also put this function back in the
    business of guessing SQLAlchemy's internal naming, which is what having a single
    output-name authority exists to avoid. The caller aliases one of them with `as`
    — a primitive already exposed — and the meaning becomes explicit.
    """
    names = [select_item_output_name(item, body_tables) for item in spec.query.select]
    seen: Set[str] = set()
    for name in names:
        if name.lower() in seen:
            raise QueryValidationError(
                f"cte {spec.name!r} projects more than one column named {name!r}, so "
                f"{spec.name}.{name} would be ambiguous — give one of them a distinct "
                "`as` alias."
            )
        seen.add(name.lower())
    return sa.Table(spec.name, sa.MetaData(), *[sa.Column(name) for name in names])


async def _reflect_and_validate_scope(
    query: StructuredQuery,
    connection_id: str,
    principal: Optional[Principal] = None,
    cte_tables: Optional[Dict[str, sa.Table]] = None,
    correlated_tables: Optional[Dict[str, sa.Table]] = None,
) -> Dict[str, sa.Table]:
    """Reflect + verify one query scope (the outer query, a cte body, or a single
    subquery), independent of any other scope — its column refs resolve only
    against its own from/join tables, plus any cte declared for the whole statement
    (which is a name it may READ, never a scope it can reach into).

    ``correlated_tables`` (item 106) are the enclosing scope's tables this subquery
    DECLARED it may read, already resolved by the caller against the parent's name
    map. They are added to the resolvable set here and nowhere else, so a scope that
    declared nothing keeps the pre-106 behavior exactly: an outer reference is an
    undeclared table, and undeclared tables are rejected below."""
    table_connection = resolve_query_table_connections(
        query, connection_id, principal=principal, cte_names=set(cte_tables or {})
    )
    _validate_join_graph(query)
    name_to_physical = effective_name_map(query)

    # Every effective table/alias the query needs reflected: the structural
    # from/join tables, plus the (effective) table of every column reference the
    # canonical visitor finds anywhere in the AST. One walk, one authority — see
    # `iter_column_refs`. (extra_on refs add no new tables, being the same pair
    # as `on`, but flow through the visitor harmlessly.)
    needed: Set[str] = {query.from_alias or query.from_table}
    needed |= set(correlated_tables or {})
    for join in query.joins:
        needed.add(join.alias or join.table)
    for column_ref in iter_column_refs(query):
        t, _ = parse_column_ref(column_ref.ref)
        needed.add(t)

    declared_tables = set(name_to_physical) | {n.lower() for n in (correlated_tables or {})}
    undeclared_tables = sorted(name for name in needed if name.lower() not in declared_tables)
    if undeclared_tables:
        raise QueryValidationError(
            "Column references may only use the query's from table/alias or an "
            f"explicitly declared join table/alias; undeclared: {undeclared_tables}"
        )

    tables: Dict[str, sa.Table] = {}
    physical_tables: Dict[str, sa.Table] = {}
    for name in needed:
        # A declared correlated name binds to the PARENT's own table object, not a
        # fresh reflection of the same table. Identity is what makes the compiled
        # subquery correlate instead of silently re-scanning an independent copy.
        outer = (correlated_tables or {}).get(name)
        if outer is not None:
            tables[name] = outer
            continue
        physical_name = name_to_physical[name.lower()]
        physical_key = physical_name.lower()
        # A cte name resolves to the block's projected shape, never to reflection —
        # `_load_table` would (correctly) fail to find a table by that name. This is
        # also the only place a cte reference is turned into something columns
        # resolve against, so a scope that was handed no `cte_tables` cannot see one.
        source = (cte_tables or {}).get(physical_key)
        if source is None:
            if physical_key not in physical_tables:
                physical_tables[physical_key] = await _load_table(
                    connection_id, physical_name, table_connection.get(name, connection_id)
                )
            source = physical_tables[physical_key]
        tables[name] = source if name.lower() == physical_key else source.alias(name)

    _validate_select_columns(query, tables)
    _validate_join_columns(query, tables)
    _validate_group_by(query, tables)
    _validate_date_operands(query, tables)
    # Every searched-CASE condition in this scope, wherever the CASE sits (a
    # CaseSelectItem, an aggregate's CaseExpr argument, one nested in a WHERE
    # predicate's arithmetic), is held to the same strictness as a top-level
    # WHERE tree: no bare-alias `when` — a condition must reference a real
    # column, the same reasoning `_validate_where_columns` applies to `where`.
    for condition in iter_scope_case_conditions(query):
        _validate_where_columns(condition, tables, allow_alias=False)
    if query.where is not None:
        _validate_where_columns(query.where, tables, allow_alias=False)
    if query.having is not None:
        # allow_alias=True — a HAVING predicate may reference a select alias
        # (an aggregate's `as`), unlike WHERE. Walks the whole boolean tree.
        _validate_where_columns(query.having, tables, allow_alias=True)
    for order in query.order_by:
        if "." in order.col:
            t, c = parse_column_ref(order.col)
            resolve_column(tables[t], c)

    _validate_top_n(query, tables)

    return tables


def _validate_select_columns(query: StructuredQuery, tables: Dict[str, sa.Table]) -> None:
    for item in query.select:
        if isinstance(item, AggregateSelectItem) and item.col == "*" and item.fn != "count":
            raise QueryValidationError("Only count(*) is allowed as a star aggregate")
        for ref in select_item_column_refs(item):
            t, c = parse_column_ref(ref)
            resolve_column(tables[t], c)


def _validate_join_columns(query: StructuredQuery, tables: Dict[str, sa.Table]) -> None:
    for join in query.joins:
        for side in [*(join.on or []), *(ref for pair in join.extra_on for ref in pair)]:
            t, c = parse_column_ref(side)
            resolve_column(tables[t], c)
    # allow_alias=False — a join condition is evaluated before the projection
    # exists, so it can never reference a select alias, the same rule WHERE gets.
    for _join, condition in iter_join_conditions(query):
        _validate_where_columns(condition, tables, allow_alias=False)


def _validate_group_by(query: StructuredQuery, tables: Dict[str, sa.Table]) -> None:
    groupable = groupable_select_aliases(query, tables)
    for col_ref in query.group_by:
        if "." in col_ref:
            t, c = parse_column_ref(col_ref)
            resolve_column(tables[t], c)
        elif col_ref not in groupable:
            raise QueryValidationError(
                f"group_by reference {col_ref!r} is not a Table.Column, a date_bucket "
                "select alias, or an expression select alias"
            )

    has_aggregate = any(isinstance(i, _AGGREGATE_SELECT_ITEM_TYPES) for i in query.select)
    if query.having and not has_aggregate and not query.group_by:
        raise QueryValidationError("having requires group_by or aggregate select items")


def _validate_top_n(query: StructuredQuery, tables: Dict[str, sa.Table]) -> None:
    spec = query.top_n
    if spec is None:
        return

    has_aggregate = any(isinstance(i, _AGGREGATE_SELECT_ITEM_TYPES) for i in query.select)
    is_aggregated = bool(query.group_by) or has_aggregate

    if is_aggregated:
        allowed_refs = set(query.group_by) | _select_aliases(query, tables)

        def _check(ref: str) -> None:
            if ref not in allowed_refs:
                raise QueryValidationError(
                    f"top_n reference {ref!r} must be a group_by column or a "
                    "select alias when combined with group_by/aggregate select items"
                )

    else:
        bucket_aliases = _select_aliases(query, tables)

        def _check(ref: str) -> None:
            if "." in ref:
                t, c = parse_column_ref(ref)
                resolve_column(tables[t], c)
            elif ref not in bucket_aliases:
                raise QueryValidationError(
                    f"top_n reference {ref!r} is not a Table.Column or a "
                    "date_bucket select alias"
                )

    for ref in spec.partition_by:
        _check(ref)
    for order in spec.order_by:
        _check(order.col)


def _iter_date_operands(query: StructuredQuery) -> Iterator[Tuple[str, str]]:
    """Every (column ref, human label) a date primitive applies to in one scope.

    All THREE date primitives, deliberately — `extract` and `date_add` from the
    `Expression` union (item 102) and the older `date_bucket` select item
    (item 117). Enumerating them in one place is the point: item 102 shipped the
    rule over two of the three, and its own documentation then described the
    general property, which is how the third stayed broken while reading as
    covered.

    Yields only **bare column** operands, since a reflected type is the only
    thing this can check.
    """
    for expr in iter_scope_expressions(query):
        for node in iter_expression_nodes(expr):
            if isinstance(node, ExtractExpr):
                operand, label = node.extract, f"extract part {node.part!r}"
            elif isinstance(node, DateAddExpr):
                operand, label = node.date_add, f"date_add by {node.unit!r}"
            else:
                continue
            if isinstance(operand, ColumnExpr):
                yield operand.col, label
    for item in query.select:
        # `date_bucket`'s `col` is a bare Table.Column by construction, so it is
        # always checkable — there is no computed-operand escape here.
        if isinstance(item, DateBucketSelectItem):
            yield item.col, f"date_bucket by {item.granularity!r}"


def _validate_date_operands(query: StructuredQuery, tables: Dict[str, sa.Table]) -> None:
    """Reject a date primitive over a column that is not a date/time type
    (TODO.md items 102 and 117).

    This closes a measured cross-dialect divergence, not a hypothetical one.
    Against a live server, with an INTEGER column as the operand:

    * `EXTRACT(hour FROM id)` — Postgres **errors**; MSSQL's `DATEPART(hour, id)`
      silently returns **0**, because T-SQL implicitly converts an int to a
      datetime counted from 1900-01-01.
    * a day shift — Postgres **errors**; MSSQL returns **1900-01-03**.
    * `date_bucket` day-truncation — Postgres **errors**; MSSQL returns
      **1900-01-02**; the internal SQLite path returns **-4712-01-05**. Three
      backends, three different wrong answers, none of them usable — which is why
      extending the rule here (item 117) is a bug fix rather than a behavior
      change: no caller has correct behavior to lose.

    So the identical AST is a hard failure on one backend and a plausible-looking
    wrong answer on the other — precisely the items 75/82 class this project
    treats as a defect rather than a quirk. Catching it here makes it one typed
    pre-database rejection on every dialect.

    Deliberately narrow: only a **bare column** operand is checked, because that
    is the only case where a reflected type is known. A computed operand (a CAST
    to date, a CASE, a function) is left to the database, and a column whose type
    the driver does not map to a Python date/time class is allowed through rather
    than guessed at — this rejects what is known-wrong, never what is merely
    unrecognized.
    """
    for column_ref, label in _iter_date_operands(query):
        table_name, column_name = parse_column_ref(column_ref)
        # No alias fallback and no None guard: `tables` is keyed by exactly the
        # strings `iter_column_refs` produced, and every ref yielded above is one
        # of them, so the lookup always hits. An earlier draft carried a
        # `name_to_physical` fallback that review proved unreachable.
        table = tables[table_name]
        column = resolve_column(table, column_name)
        try:
            # A property, and it RAISES for types with no Python mapping —
            # so it must be read inside the guard, not fetched beforehand.
            resolved = column.type.python_type
        except (NotImplementedError, AttributeError):
            continue  # unknown mapping: allow through rather than guess
        if not isinstance(resolved, type):
            continue
        # `timedelta` is included deliberately: Postgres's `interval` maps to
        # it, and `EXTRACT(hour FROM interval_col)` / `interval_col + interval`
        # are both real Postgres (measured: `EXTRACT(hour FROM INTERVAL
        # '26 hours')` = 26). Rejecting it would be this engine refusing a
        # capability the dialect genuinely has — the inversion CLAUDE.md's
        # philosophy forbids. MSSQL has no interval column type, so allowing
        # it creates no cross-dialect divergence.
        if issubclass(resolved, (dt.date, dt.time, dt.timedelta)):
            continue
        # The remedy is deliberately NOT suggested for every type. Casting a
        # STRING that holds a timestamp is correct and works. Casting an
        # INTEGER reproduces the very divergence this rule just closed —
        # Postgres errors on `CAST(int AS TIMESTAMP)` while MSSQL yields a
        # 1900-epoch datetime — so pointing an integer operand at a cast
        # would hand the caller back the bug.
        hint = (
            f' Cast it first ({{"cast": {{"col": "{column_ref}"}}, '
            '"to": "timestamp"}) if it really holds a timestamp.'
            if issubclass(resolved, str)
            else ""
        )
        raise QueryValidationError(
            f"{label} requires a date/time column, but {column_ref!r} is "
            f"{resolved.__name__}.{hint}"
        )


def _validate_where_columns(
    node: WhereNode, tables: Dict[str, sa.Table], allow_alias: bool
) -> None:
    # Per-leaf, no dependence on the tree's shape — layered on the single
    # canonical walk (item 111) instead of re-recursing the boolean tree.
    for pred in iter_where_predicates(node):
        _validate_predicate_columns(pred, tables, allow_alias=allow_alias)


def _validate_predicate_columns(
    pred: Predicate, tables: Dict[str, sa.Table], allow_alias: bool
) -> None:
    # A bare (undotted) `col` is a select-alias reference, legal only in HAVING.
    # It is the one ref `predicate_column_refs` deliberately does not yield, so
    # it is checked here and every other ref comes from that canonical collector
    # — including the ones nested inside a computed `expr`/`value_expr`.
    if pred.col is not None and "." not in pred.col and not allow_alias:
        raise QueryValidationError(f"Column reference must be 'Table.Column', got {pred.col!r}")
    for ref in predicate_column_refs(pred):
        t, c = parse_column_ref(ref)
        resolve_column(tables[t], c)
