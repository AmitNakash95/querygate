"""Schema-truth validation: every identifier a query references must exist.

Complexity caps and table/column allow-deny policy are checked separately in
validation/policy_validation.py, before this module ever reflects anything.
"""

from __future__ import annotations

import datetime as dt

import enum
from typing import Callable, Dict, Iterator, NamedTuple, Optional, Set, Tuple

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


def iter_query_scopes(
    query: StructuredQuery, _depth: int = 0
) -> Iterator[Tuple[int, StructuredQuery]]:
    """Yield `(depth, query)` for the outer query (depth 0) and every nested
    `value_subquery` (item 97), depth-first. Each yielded query is an INDEPENDENT
    validation scope: its column references resolve against its own from/join
    tables (never an outer scope's), which is exactly what makes an `IN (subquery)`
    structurally uncorrelated. Policy and schema validation walk these scopes so
    a subquery gets the full allow/deny + cap treatment, and so caps can be summed
    tree-wide (never per-level) to stop nesting being a cap-multiplier bypass."""
    yield (_depth, query)
    for pred in iter_where_and_having_predicates(query):
        if pred.value_subquery is not None:
            yield from iter_query_scopes(pred.value_subquery, _depth + 1)


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


def _select_aliases(query: StructuredQuery, tables: Dict[str, sa.Table]) -> Set[str]:
    """Select-item output names `top_n` may rank by. A `WindowSelectItem`'s alias
    is deliberately absent (item 101): `top_n`'s rank is computed in the same
    SELECT, so ranking by a window's output would nest one window inside another's
    OVER clause, which no dialect allows."""
    aliases: Set[str] = set()
    for item in query.select:
        if isinstance(item, AggregateSelectItem):
            aliases.add(_aggregate_alias(item))
        elif isinstance(item, DateBucketSelectItem):
            aliases.add(_date_bucket_alias(item, tables))
        elif isinstance(item, StringAggSelectItem):
            aliases.add(_string_agg_alias(item))
        elif isinstance(item, ArrayAggSelectItem):
            aliases.add(_array_agg_alias(item))
        elif isinstance(item, PercentileContSelectItem):
            aliases.add(_percentile_cont_alias(item))
        elif isinstance(item, ScalarFunctionSelectItem):
            aliases.add(_scalar_function_alias(item))
        elif isinstance(item, (CaseSelectItem, ExpressionSelectItem)):
            aliases.add(item.alias)
    return aliases


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
) -> Dict[str, str]:
    """Resolve which connection each table belongs to, rejecting any join
    whose connection isn't in the same policy join_group as the primary.

    ``connection_resolver`` lets the config simulator run this exact
    production visibility/join-group rule against isolated candidate stores.
    Normal query execution leaves it unset and therefore uses the live stores.
    """
    resolver = connection_resolver or (
        lambda target, actor: resolve_visible_connection(target, principal=actor)
    )
    primary, policy = resolver(connection_id, principal)
    primary_group = policy.join_group or primary.effective_join_group()

    table_connection: Dict[str, str] = {(query.from_alias or query.from_table): connection_id}
    for join in query.joins:
        join_connection_id = join.connection or connection_id
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
    """Reflect + verify every table/column the query — and every nested
    value_subquery (item 97) — references exists.

    Returns the OUTER query's reflected tables, keyed by the name the query used,
    for the compiler. If `scope_tables` is provided, it is populated with each
    scope's reflected tables keyed by that scope query's `id`, so the compiler can
    recursively render `IN (subquery)`. Each subquery is validated as an
    independent scope (its refs resolve to its own tables — undeclared-table
    rejection is exactly what makes a correlated reference to an outer table fail),
    and a subquery is required to stay single-connection (cross-connection nesting
    is rejected, per item 97's minimal-safe subset)."""
    outer_tables: Optional[Dict[str, sa.Table]] = None
    for depth, scope in iter_query_scopes(query):
        if depth > 0:
            for join in scope.joins:
                if join.connection is not None and join.connection != connection_id:
                    raise QueryValidationError(
                        f"cross-connection subquery: a nested IN (subquery) may not join to "
                        f"another connection ({join.connection!r}) — run a separate query per "
                        "connection and combine results instead (item 97)."
                    )
        scoped_tables = await _reflect_and_validate_scope(scope, connection_id, principal)
        if scope_tables is not None:
            scope_tables[id(scope)] = scoped_tables
        if depth == 0:
            outer_tables = scoped_tables
    if outer_tables is None:  # unreachable: iter_query_scopes always yields depth 0
        raise QueryValidationError("internal error: query had no top-level scope to validate")
    return outer_tables


async def _reflect_and_validate_scope(
    query: StructuredQuery, connection_id: str, principal: Optional[Principal] = None
) -> Dict[str, sa.Table]:
    """Reflect + verify one query scope (the outer query, or a single subquery),
    independent of any other scope — its column refs resolve only against its own
    from/join tables."""
    table_connection = resolve_query_table_connections(query, connection_id, principal=principal)
    _validate_join_graph(query)
    name_to_physical = effective_name_map(query)

    # Every effective table/alias the query needs reflected: the structural
    # from/join tables, plus the (effective) table of every column reference the
    # canonical visitor finds anywhere in the AST. One walk, one authority — see
    # `iter_column_refs`. (extra_on refs add no new tables, being the same pair
    # as `on`, but flow through the visitor harmlessly.)
    needed: Set[str] = {query.from_alias or query.from_table}
    for join in query.joins:
        needed.add(join.alias or join.table)
    for column_ref in iter_column_refs(query):
        t, _ = parse_column_ref(column_ref.ref)
        needed.add(t)

    declared_tables = set(name_to_physical)
    undeclared_tables = sorted(name for name in needed if name.lower() not in declared_tables)
    if undeclared_tables:
        raise QueryValidationError(
            "Column references may only use the query's from table/alias or an "
            f"explicitly declared join table/alias; undeclared: {undeclared_tables}"
        )

    tables: Dict[str, sa.Table] = {}
    physical_tables: Dict[str, sa.Table] = {}
    for name in needed:
        physical_name = name_to_physical[name.lower()]
        physical_key = physical_name.lower()
        if physical_key not in physical_tables:
            physical_tables[physical_key] = await _load_table(
                connection_id, physical_name, table_connection.get(name, connection_id)
            )
        physical_table = physical_tables[physical_key]
        tables[name] = (
            physical_table if name.lower() == physical_key else physical_table.alias(name)
        )

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
