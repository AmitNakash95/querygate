"""Schema-truth validation: every identifier a query references must exist.

Complexity caps and table/column allow-deny policy are checked separately in
validation/policy_validation.py, before this module ever reflects anything.
"""

from __future__ import annotations

from typing import Callable, Dict, Iterator, Optional, Set, Tuple

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
    CaseSelectItem,
    ColArg,
    DateBucketSelectItem,
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


def select_item_column_refs(item: SelectItem) -> Iterator[str]:
    """Every Table.Column ref a single select item touches, across every
    variant — a bare string, an aggregate/date_bucket's `.col`, a scalar
    function's column-typed args, or a CASE expression's when/then/else.
    Shared by schema validation (which tables/columns to reflect/resolve)
    and policy validation (which refs column-level policy must check).
    """
    if isinstance(item, str):
        yield item
        return
    if isinstance(item, ScalarFunctionSelectItem):
        for arg in item.args:
            if isinstance(arg, ColArg):
                yield arg.col
        return
    if isinstance(item, CaseSelectItem):
        for branch in item.when:
            yield from predicate_column_refs(branch.when)
            if isinstance(branch.then, ColArg):
                yield branch.then.col
        if isinstance(item.else_, ColArg):
            yield item.else_.col
        return
    # AggregateSelectItem / DateBucketSelectItem / StringAggSelectItem /
    # ArrayAggSelectItem
    if item.col != "*":
        yield item.col


def predicate_column_refs(pred: Predicate) -> Iterator[str]:
    """Every Table.Column ref a Predicate's LEFT side touches: `col` if it's
    a dotted Table.Column (a bare alias — valid only in HAVING — is skipped
    here; enforcing that strictly is `_validate_predicate_columns`'s job,
    not this collector's), else each `ColArg` in `col_fn.args` — plus
    `value_col` if set. Single source of truth shared by policy validation's
    ref walk and this module's own table-collection/reflection logic.
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
    if item.alias:
        return item.alias
    if item.col == "*":
        return f"{item.fn}_all"
    _, col_name = parse_column_ref(item.col)
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
        elif isinstance(item, ScalarFunctionSelectItem):
            aliases.add(_scalar_function_alias(item))
        elif isinstance(item, CaseSelectItem):
            aliases.add(item.alias)
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


def _collect_tables_from_where(node: WhereNode, tables: Set[str]) -> None:
    if isinstance(node, Predicate):
        for ref in predicate_column_refs(node):
            table, _ = parse_column_ref(ref)
            tables.add(table)
        return
    if node.not_terms is not None:
        _collect_tables_from_where(node.not_terms, tables)
        return
    for child in node.and_terms or node.or_terms or []:
        _collect_tables_from_where(child, tables)


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
    query: StructuredQuery, connection_id: str, principal: Optional[Principal] = None
) -> Dict[str, sa.Table]:
    """Reflect + verify every table/column the query references exists.

    Returns the reflected tables, keyed by the name the query used, for the
    compiler.
    """
    table_connection = resolve_query_table_connections(query, connection_id, principal=principal)
    _validate_join_graph(query)
    name_to_physical = effective_name_map(query)

    needed: Set[str] = {query.from_alias or query.from_table}
    for join in query.joins:
        needed.add(join.alias or join.table)
        for side in join.on:
            t, _ = parse_column_ref(side)
            needed.add(t)

    for item in query.select:
        for ref in select_item_column_refs(item):
            t, _ = parse_column_ref(ref)
            needed.add(t)

    for col_ref in query.group_by:
        # group_by may reference a date_bucket select alias (no table)
        if "." in col_ref:
            t, _ = parse_column_ref(col_ref)
            needed.add(t)

    for order in query.order_by:
        if "." in order.col:
            t, _ = parse_column_ref(order.col)
            needed.add(t)

    if query.where is not None:
        _collect_tables_from_where(query.where, needed)

    for pred in query.having:
        # having may reference select aliases (no table) or Table.Col
        for ref in predicate_column_refs(pred):
            t, _ = parse_column_ref(ref)
            needed.add(t)

    if query.top_n is not None:
        for ref in query.top_n.partition_by:
            if "." in ref:
                t, _ = parse_column_ref(ref)
                needed.add(t)
        for order in query.top_n.order_by:
            if "." in order.col:
                t, _ = parse_column_ref(order.col)
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
    if query.where is not None:
        _validate_where_columns(query.where, tables, allow_alias=False)
    for pred in query.having:
        _validate_predicate_columns(pred, tables, allow_alias=True)
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
        if isinstance(item, CaseSelectItem):
            # Same strictness as a top-level WHERE predicate (no bare-alias
            # `when` — CASE branch conditions must reference a real column,
            # same reasoning `_validate_where_columns` applies to `where`).
            for branch in item.when:
                _validate_predicate_columns(branch.when, tables, allow_alias=False)
        for ref in select_item_column_refs(item):
            t, c = parse_column_ref(ref)
            resolve_column(tables[t], c)


def _validate_join_columns(query: StructuredQuery, tables: Dict[str, sa.Table]) -> None:
    for join in query.joins:
        for side in [*join.on, *(ref for pair in join.extra_on for ref in pair)]:
            t, c = parse_column_ref(side)
            resolve_column(tables[t], c)


def _validate_group_by(query: StructuredQuery, tables: Dict[str, sa.Table]) -> None:
    bucket_aliases = {
        _date_bucket_alias(item, tables)
        for item in query.select
        if isinstance(item, DateBucketSelectItem)
    }
    for col_ref in query.group_by:
        if "." in col_ref:
            t, c = parse_column_ref(col_ref)
            resolve_column(tables[t], c)
        elif col_ref not in bucket_aliases:
            raise QueryValidationError(
                f"group_by reference {col_ref!r} is not a Table.Column or a "
                "date_bucket select alias"
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
    if isinstance(node, Predicate):
        _validate_predicate_columns(node, tables, allow_alias=allow_alias)
        return
    if node.not_terms is not None:
        _validate_where_columns(node.not_terms, tables, allow_alias=allow_alias)
        return
    for child in node.and_terms or node.or_terms or []:
        _validate_where_columns(child, tables, allow_alias=allow_alias)


def _validate_predicate_columns(
    pred: Predicate, tables: Dict[str, sa.Table], allow_alias: bool
) -> None:
    if pred.col_fn is not None:
        for arg in pred.col_fn.args:
            if isinstance(arg, ColArg):
                t, c = parse_column_ref(arg.col)
                resolve_column(tables[t], c)
    elif "." not in pred.col:
        if not allow_alias:
            raise QueryValidationError(f"Column reference must be 'Table.Column', got {pred.col!r}")
    else:
        t, c = parse_column_ref(pred.col)
        resolve_column(tables[t], c)
    if pred.value_col is not None:
        vt, vc = parse_column_ref(pred.value_col)
        resolve_column(tables[vt], vc)
