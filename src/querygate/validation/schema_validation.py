"""Schema-truth validation: every identifier a query references must exist.

Complexity caps and table/column allow-deny policy are checked separately in
validation/policy_validation.py, before this module ever reflects anything.
"""

from __future__ import annotations

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
    DateBucketSelectItem,
    Expression,
    ExpressionSelectItem,
    FunctionExpr,
    PercentileContSelectItem,
    Predicate,
    ScalarFunctionSelectItem,
    SelectItem,
    StringAggSelectItem,
    StructuredQuery,
    WhereNode,
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


def iter_expression_nodes(expr: Expression) -> Iterator[Expression]:
    """Every node of a bounded scalar `Expression` tree (item 100), in document
    order, INCLUDING expressions reachable only through a nested `CaseExpr`
    branch's condition predicates (`CASE WHEN a > b * 2 THEN ...`). One walk,
    used for column-ref collection, the depth/node caps, and the nested-CASE
    rules — so a new union member is taught here once, not in five places.
    """
    yield expr
    if isinstance(expr, BinaryOpExpr):
        yield from iter_expression_nodes(expr.left)
        yield from iter_expression_nodes(expr.right)
    elif isinstance(expr, FunctionExpr):
        for arg in expr.args:
            yield from iter_expression_nodes(arg)
    elif isinstance(expr, CastExpr):
        yield from iter_expression_nodes(expr.cast)
    elif isinstance(expr, CaseExpr):
        for branch in expr.when:
            for pred in iter_where_predicates(branch.when):
                for nested in predicate_expressions(pred):
                    yield from iter_expression_nodes(nested)
            yield from iter_expression_nodes(branch.then)
        if expr.else_ is not None:
            yield from iter_expression_nodes(expr.else_)


def expression_column_refs(expr: Expression) -> Iterator[str]:
    """Every Table.Column ref anywhere inside an `Expression` tree — including
    the refs a nested `CaseExpr` branch's CONDITION carries, which are not
    `ColumnExpr` nodes at all but ordinary `Predicate` refs (`col`, `col_fn`
    args, `value_col`). An unvisited ref here is a silent policy AND masking
    bypass, the single most important rule in
    docs/ENGINE_EXPRESSIVENESS_PLAN.md §1.

    This recurses the union directly rather than filtering
    `iter_expression_nodes`, because collecting refs has to cross out of the
    expression union into the boolean-condition layer and back (via
    `predicate_column_refs`, the canonical per-predicate collector, which
    recurses into a predicate's own expressions). The two walks are the closed
    union's only recursions and MUST stay in lockstep: a new `Expression`
    member is handled in both, or it silently contributes neither refs nor
    node-count. `test_reference_visitor.py` pins that with a single mixed
    expression asserting the exact ref set.
    """
    if isinstance(expr, ColumnExpr):
        yield expr.col
    elif isinstance(expr, BinaryOpExpr):
        yield from expression_column_refs(expr.left)
        yield from expression_column_refs(expr.right)
    elif isinstance(expr, FunctionExpr):
        for arg in expr.args:
            yield from expression_column_refs(arg)
    elif isinstance(expr, CastExpr):
        yield from expression_column_refs(expr.cast)
    elif isinstance(expr, CaseExpr):
        for branch in expr.when:
            yield from _where_column_refs(branch.when)
            yield from expression_column_refs(branch.then)
        if expr.else_ is not None:
            yield from expression_column_refs(expr.else_)
    # LiteralExpr carries no reference.


def expression_depth(expr: Expression, depth: int = 1) -> int:
    """Nesting depth of an `Expression` tree, counting a `CaseExpr` branch's
    condition-predicate expressions as children too — so `max_expression_depth`
    bounds every path a caller can build, not just the arithmetic one."""
    if isinstance(expr, BinaryOpExpr):
        return max(expression_depth(expr.left, depth + 1), expression_depth(expr.right, depth + 1))
    if isinstance(expr, FunctionExpr):
        return max(expression_depth(arg, depth + 1) for arg in expr.args)
    if isinstance(expr, CastExpr):
        return expression_depth(expr.cast, depth + 1)
    if isinstance(expr, CaseExpr):
        children = [expression_depth(branch.then, depth + 1) for branch in expr.when]
        children.extend(
            expression_depth(nested, depth + 1)
            for branch in expr.when
            for pred in iter_where_predicates(branch.when)
            for nested in predicate_expressions(pred)
        )
        if expr.else_ is not None:
            children.append(expression_depth(expr.else_, depth + 1))
        return max(children)
    return depth


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
    elif isinstance(item, AggregateSelectItem) and item.arg is not None:
        yield item.arg


def iter_scope_expressions(query: StructuredQuery) -> Iterator[Expression]:
    """Every top-level `Expression` tree in ONE query scope: those carried by
    its select items and by the predicates of its WHERE/HAVING trees. Not
    recursive into nested scopes (`value_subquery` is its own scope, walked by
    `iter_query_scopes`), and not recursive into the expressions themselves —
    callers compose this with `iter_expression_nodes` for the full walk.
    """
    for item in query.select:
        yield from select_item_expressions(item)
    for pred in iter_where_and_having_predicates(query):
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
        for node in iter_expression_nodes(expr):
            if isinstance(node, CaseExpr):
                for branch in node.when:
                    yield branch.when


def select_item_column_refs(item: SelectItem) -> Iterator[str]:
    """Every Table.Column ref a single select item touches, across every
    variant — a bare string, an aggregate/date_bucket's `.col`, a scalar
    function's column-typed args, a CASE expression's when/then/else, or any
    `Expression` the item carries (item 100). Shared by schema validation
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


def predicate_column_refs(pred: Predicate) -> Iterator[str]:
    """Every Table.Column ref a Predicate touches: `col` if it's a dotted
    Table.Column (a bare alias — valid only in HAVING — is skipped here;
    enforcing that strictly is `_validate_predicate_columns`'s job, not this
    collector's), else each `ColArg` in `col_fn.args` or every ref inside a
    computed `expr` — plus `value_col`/`value_expr` if set. Single source of
    truth shared by policy validation's ref walk and this module's own
    table-collection/reflection logic.
    """
    if pred.col is not None:
        if "." in pred.col:
            yield pred.col
    elif pred.col_fn is not None:
        for arg in pred.col_fn.args:
            if isinstance(arg, ColArg):
                yield arg.col
    elif pred.expr is not None:
        yield from expression_column_refs(pred.expr)
    if pred.value_col is not None:
        yield pred.value_col
    if pred.value_expr is not None:
        yield from expression_column_refs(pred.value_expr)


class RefPosition(enum.Enum):
    """Where in a `StructuredQuery` a Table.Column reference appears.

    Rich enough to preserve the one distinction enforcement actually branches
    on: a *bare* top-level select projection item is the only position a
    masked column (TODO.md item 49) is allowed to appear — every other
    position would leak the raw value. `SELECT_NESTED` is a column inside a
    scalar-fn/CASE/aggregate select item, which is NOT a bare projection and so
    is subject to the masked-column rule like any other non-projection ref.
    """

    SELECT_PROJECTION_BARE = "select_projection_bare"
    SELECT_NESTED = "select_nested"
    JOIN_ON = "join_on"
    JOIN_EXTRA_ON = "join_extra_on"
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
    where one can appear — select (bare vs. nested), join `on`/`extra_on`,
    where, group_by, having, order_by, and top_n partition/order.

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
        for side in join.on:
            yield ColumnRef(RefPosition.JOIN_ON, side)
        for pair in join.extra_on:
            for side in pair:
                yield ColumnRef(RefPosition.JOIN_EXTRA_ON, side)
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
    """
    known = {(query.from_alias or query.from_table).lower()}
    for join in query.joins:
        left_t, _ = parse_column_ref(join.on[0])
        right_t, _ = parse_column_ref(join.on[1])
        sides = {left_t.lower(), right_t.lower()}
        joined_table = (join.alias or join.table).lower()
        if joined_table not in sides:
            raise QueryValidationError(
                f"Join condition for {join.table!r} must reference that table "
                f"(as {joined_table!r})"
            )
        if not (sides - {joined_table}) & known:
            raise QueryValidationError(
                f"Join to {join.table!r} does not connect to the query table graph"
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
        for side in [*join.on, *(ref for pair in join.extra_on for ref in pair)]:
            t, c = parse_column_ref(side)
            resolve_column(tables[t], c)


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
