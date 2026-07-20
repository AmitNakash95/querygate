"""Compile a validated StructuredQuery + policy into a SQLAlchemy Select.

Called only after validation/policy_validation.py and validation/
schema_validation.py have both passed — every identifier here is already
known to exist and be policy-permitted.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import sqlalchemy as sa

from querygate.compiler.dialect_adapters import get_dialect_adapter
from querygate.connections.models import DatabaseDialect
from querygate.core.auth import Principal
from querygate.core.exceptions import QueryValidationError
from querygate.policy.models import Policy
from querygate.query_ast.models import (
    ArrayAggSelectItem,
    CaseSelectItem,
    ColArg,
    DateBucketSelectItem,
    PercentileContSelectItem,
    Predicate,
    ScalarFunctionArg,
    ScalarFunctionSelectItem,
    StringAggSelectItem,
    StructuredQuery,
    WhereNode,
    _AGGREGATE_SELECT_ITEM_TYPES,
)
from querygate.validation.schema_validation import (
    effective_name_map,
    parse_column_ref,
    resolve_column,
)

_AGG_FNS = {
    "count": sa.func.count,
    "sum": sa.func.sum,
    "avg": sa.func.avg,
    "min": sa.func.min,
    "max": sa.func.max,
}

_SCALAR_FNS = {
    "coalesce": sa.func.coalesce,
    "lower": sa.func.lower,
    "upper": sa.func.upper,
    # MSSQL's TRIM() requires SQL Server 2017+; not special-cased here since
    # the project already treats MSSQL as a single supported dialect version.
    "trim": sa.func.trim,
    "concat": sa.func.concat,
}

_RANK_FNS = {
    "row_number": sa.func.row_number,
    "rank": sa.func.rank,
    "dense_rank": sa.func.dense_rank,
}

_STAT_FNS = {"stddev", "variance"}


def _aggregate_fn(fn_name: str, dialect: str) -> Any:
    """count/sum/avg/min/max are dialect-universal (the flat _AGG_FNS dict).
    stddev/variance are not (MSSQL's real functions are STDEV/VAR) — routed
    through the DialectAdapter instead of a fourth ad hoc dialect branch.
    """
    if fn_name in _STAT_FNS:
        return get_dialect_adapter(dialect).stat_fn(fn_name)
    return _AGG_FNS[fn_name]


def _table_by_name(tables: Dict[str, sa.Table], name: str) -> sa.Table:
    for key, table in tables.items():
        if key.lower() == name.lower():
            return table
    raise QueryValidationError(f"Unknown table {name!r}")


def _column(tables: Dict[str, sa.Table], col_ref: str) -> sa.Column:
    table_name, column_name = parse_column_ref(col_ref)
    return resolve_column(_table_by_name(tables, table_name), column_name)


def _resolve_scalar_arg(arg: ScalarFunctionArg, tables: Dict[str, sa.Table]) -> Any:
    if isinstance(arg, ColArg):
        return _column(tables, arg.col)
    return arg.literal


def _apply_predicate(col: Any, pred: Predicate, tables: Dict[str, sa.Table]) -> Any:
    op = pred.op
    # value_col is only valid for eq/neq/lt/lte/gt/gte (enforced at the AST
    # layer), so every other op below always sees pred.value here.
    val = _column(tables, pred.value_col) if pred.value_col is not None else pred.value
    if op == "eq":
        return col == val
    if op == "neq":
        return col != val
    if op == "lt":
        return col < val
    if op == "lte":
        return col <= val
    if op == "gt":
        return col > val
    if op == "gte":
        return col >= val
    if op == "in":
        return col.in_(list(val))
    if op == "not_in":
        return ~col.in_(list(val))
    if op == "like":
        return col.like(val)
    if op == "between":
        return col.between(val[0], val[1])
    if op == "is_null":
        return col.is_(None)
    if op == "is_not_null":
        return col.is_not(None)
    raise QueryValidationError(f"Unsupported operator {op!r}")


def _resolve_predicate_target(
    pred: Predicate, tables: Dict[str, sa.Table], alias_map: Dict[str, Any]
) -> Any:
    if pred.col_fn is not None:
        scalar_fn = _SCALAR_FNS[pred.col_fn.fn]
        args = [_resolve_scalar_arg(arg, tables) for arg in pred.col_fn.args]
        return scalar_fn(*args)
    if "." in pred.col:
        return _column(tables, pred.col)
    if pred.col in alias_map:
        return alias_map[pred.col]
    raise QueryValidationError(f"Unknown column or alias {pred.col!r}")


def _resolve_output_ref(
    ref: str, tables: Dict[str, sa.Table], alias_map: Dict[str, Any], allow_table_fallback: bool
) -> Any:
    """Resolve a group_by/order_by ref: alias/select ref first, then Table.Col."""
    if ref in alias_map:
        return alias_map[ref]
    if allow_table_fallback and "." in ref:
        return _column(tables, ref)
    raise QueryValidationError(f"Unknown column or alias {ref!r}")


def _ref_output_name(ref: str, alias_map: Dict[str, Any]) -> str:
    """Map a select/group_by ref to the output column name it will have in a subquery."""
    if ref in alias_map:
        return alias_map[ref].name
    if "." in ref:
        _, col_name = parse_column_ref(ref)
        return col_name
    return ref


def _compile_where(node: WhereNode, tables: Dict[str, sa.Table], alias_map: Dict[str, Any]) -> Any:
    if isinstance(node, Predicate):
        target = _resolve_predicate_target(node, tables, alias_map)
        return _apply_predicate(target, node, tables)

    if node.not_terms is not None:
        return sa.not_(_compile_where(node.not_terms, tables, alias_map))

    children = node.and_terms or node.or_terms or []
    compiled = [_compile_where(child, tables, alias_map) for child in children]
    if node.and_terms is not None:
        return sa.and_(*compiled)
    return sa.or_(*compiled)


def _build_select_columns(
    query: StructuredQuery, tables: Dict[str, sa.Table], dialect: str
) -> Tuple[List[Any], Dict[str, Any]]:
    columns: List[Any] = []
    alias_map: Dict[str, Any] = {}

    for item in query.select:
        if isinstance(item, str):
            col = _column(tables, item)
            columns.append(col)
            alias_map[col.name] = col
            alias_map[item] = col
            continue

        if isinstance(item, DateBucketSelectItem):
            col = _column(tables, item.col)
            expr = get_dialect_adapter(dialect).date_bucket(col, item.granularity)
            alias = item.alias or f"bucket_{col.name}_{item.granularity}"
            labeled = expr.label(alias)
            columns.append(labeled)
            alias_map[alias] = labeled
            continue

        if isinstance(item, ScalarFunctionSelectItem):
            scalar_fn = _SCALAR_FNS[item.fn]
            args = [_resolve_scalar_arg(arg, tables) for arg in item.args]
            expr = scalar_fn(*args)
            alias = item.alias
            if not alias:
                first_col = next((a for a in item.args if isinstance(a, ColArg)), None)
                alias = (
                    f"{item.fn}_{parse_column_ref(first_col.col)[1]}"
                    if first_col is not None
                    else f"{item.fn}_result"
                )
            labeled = expr.label(alias)
            columns.append(labeled)
            alias_map[alias] = labeled
            continue

        if isinstance(item, StringAggSelectItem):
            col = _column(tables, item.col)
            expr = get_dialect_adapter(dialect).string_agg(col, item.delimiter)
            alias = item.alias or f"string_agg_{col.name}"
            labeled = expr.label(alias)
            columns.append(labeled)
            alias_map[alias] = labeled
            continue

        if isinstance(item, ArrayAggSelectItem):
            col = _column(tables, item.col)
            expr = get_dialect_adapter(dialect).array_agg(col)
            alias = item.alias or f"array_agg_{col.name}"
            labeled = expr.label(alias)
            columns.append(labeled)
            alias_map[alias] = labeled
            continue

        if isinstance(item, PercentileContSelectItem):
            col = _column(tables, item.col)
            expr = get_dialect_adapter(dialect).percentile_cont(col, item.fraction)
            alias = item.alias or f"percentile_cont_{col.name}"
            labeled = expr.label(alias)
            columns.append(labeled)
            alias_map[alias] = labeled
            continue

        if isinstance(item, CaseSelectItem):
            whens = []
            for branch in item.when:
                target = _resolve_predicate_target(branch.when, tables, alias_map={})
                condition = _apply_predicate(target, branch.when, tables)
                whens.append((condition, _resolve_scalar_arg(branch.then, tables)))
            else_value = _resolve_scalar_arg(item.else_, tables) if item.else_ is not None else None
            expr = sa.case(*whens, else_=else_value)
            labeled = expr.label(item.alias)
            columns.append(labeled)
            alias_map[item.alias] = labeled
            continue

        fn = _aggregate_fn(item.fn, dialect)
        if item.col == "*":
            if item.fn != "count":
                raise QueryValidationError("Only count(*) is allowed as a star aggregate")
            expr = fn()
        else:
            arg = _column(tables, item.col)
            expr = fn(arg.distinct()) if item.distinct else fn(arg)

        alias = item.alias
        if not alias:
            if item.col == "*":
                alias = f"{item.fn}_all"
            else:
                _, col_name = parse_column_ref(item.col)
                alias = f"{item.fn}_{col_name}"
        labeled = expr.label(alias)
        columns.append(labeled)
        alias_map[alias] = labeled

    return columns, alias_map


def _apply_mandatory_row_filters(
    stmt: sa.Select,
    policy: Policy,
    tables: Dict[str, sa.Table],
    name_to_physical: Dict[str, str],
    principal: Optional[Principal],
) -> sa.Select:
    """AND in every policy-declared mandatory filter whose table is actually
    part of this query's graph — silently skipped for tables outside the
    graph, rather than erroring, so unrelated queries aren't blocked.

    `tables` is keyed by effective name (alias if given, else table name);
    `name_to_physical` maps each of those back to its physical table, so a
    filter matches every OCCURRENCE of that physical table, not just one —
    a self-join of a mandatory-filtered table must be filtered on every
    alias, or one side could see rows the filter was meant to hide.

    A filter's value is either a static literal or resolved from the
    authenticated principal's claims (`MandatoryRowFilter.resolve` raises
    `PolicyViolationError` if `from_claim` is set but the principal lacks
    that claim — a caller with no matching claim can't fall through to an
    unfiltered query).
    """
    for row_filter in policy.mandatory_row_filters:
        matches = [
            key
            for key in tables
            if name_to_physical.get(key.lower(), key).lower() == row_filter.table.lower()
        ]
        if not matches:
            continue
        value = row_filter.resolve(principal)
        for key in matches:
            col = resolve_column(tables[key], row_filter.column)
            stmt = stmt.where(col == value)
    return stmt


def clamp_limit(requested: Optional[int], policy: Policy, *, is_aggregate: bool = False) -> int:
    max_limit = policy.max_limit_aggregate if is_aggregate else policy.max_limit
    limit = policy.default_limit if requested is None else requested
    if limit < 1:
        raise QueryValidationError("limit must be >= 1")
    return min(limit, max_limit)


def _apply_top_n(
    stmt: sa.Select,
    query: StructuredQuery,
    tables: Dict[str, sa.Table],
    alias_map: Dict[str, Any],
    is_aggregate: bool,
    dialect: str,
) -> Tuple[sa.Select, Dict[str, Any]]:
    """Wrap stmt with a rank-per-partition subquery, keeping only the top n rows."""
    spec = query.top_n
    output_names = [c.name for c in stmt.selected_columns]
    rank_fn = _RANK_FNS[spec.fn]
    adapter = get_dialect_adapter(dialect)

    if is_aggregate:
        # stmt already has group_by/having applied and its select list is
        # aggregate/date_bucket Labels. Most dialects (incl. MSSQL) won't let
        # OVER(PARTITION BY/ORDER BY) reference a SELECT-list alias defined in
        # the same statement, so materialize the aggregation as a subquery
        # first and rank over its real derived-table columns instead.
        agg = stmt.subquery()

        def _agg_col(ref: str) -> Any:
            return agg.c[_ref_output_name(ref, alias_map)]

        partition_cols = [_agg_col(ref) for ref in spec.partition_by] or None
        order_cols = [
            term
            for o in spec.order_by
            for term in adapter.order_by_terms(_agg_col(o.col), o.dir, o.nulls)
        ]
        rank_expr = rank_fn().over(partition_by=partition_cols, order_by=order_cols).label("__rank")
        ranked = sa.select(*[agg.c[name] for name in output_names], rank_expr).subquery()
    else:
        partition_cols = [
            _resolve_output_ref(ref, tables, alias_map, allow_table_fallback=True)
            for ref in spec.partition_by
        ] or None
        order_cols = [
            term
            for o in spec.order_by
            for term in adapter.order_by_terms(
                _resolve_output_ref(o.col, tables, alias_map, allow_table_fallback=True),
                o.dir,
                o.nulls,
            )
        ]
        rank_expr = rank_fn().over(partition_by=partition_cols, order_by=order_cols).label("__rank")
        ranked = stmt.add_columns(rank_expr).subquery()

    outer_cols = [ranked.c[name] for name in output_names]
    outer_stmt = sa.select(*outer_cols).where(ranked.c["__rank"] <= spec.n)

    outer_alias_map: Dict[str, Any] = dict(zip(output_names, outer_cols))
    for item in query.select:
        if isinstance(item, str) and item in alias_map:
            outer_alias_map.setdefault(item, ranked.c[alias_map[item].name])

    return outer_stmt, outer_alias_map


def compile_structured_query(
    query: StructuredQuery,
    tables: Dict[str, sa.Table],
    policy: Policy,
    # Not typed DatabaseDialect: the caller (execution/service.py) derives
    # this from the live SQLAlchemy engine's own dialect.name, which is a
    # wider domain than the registry's supported dialects — it's legitimately
    # "sqlite" for the internal-only SQLite test/example path (see
    # DatabaseDialect's docstring). _date_bucket_expr compares this against
    # DatabaseDialect members and falls back to SQLite bucketing otherwise.
    dialect: str = DatabaseDialect.POSTGRESQL,
    principal: Optional[Principal] = None,
) -> Tuple[sa.Select, int]:
    """Compile AST + reflected tables + policy into a Select.

    Returns (statement, effective_limit).
    """
    select_cols, alias_map = _build_select_columns(query, tables, dialect)
    base = _table_by_name(tables, query.from_alias or query.from_table)
    stmt = sa.select(*select_cols).select_from(base)
    if query.distinct:
        stmt = stmt.distinct()

    for join in query.joins:
        right = _table_by_name(tables, join.alias or join.table)
        conditions = []
        for left_ref, right_ref in [join.on, *join.extra_on]:
            left_t, left_c = parse_column_ref(left_ref)
            right_t, right_c = parse_column_ref(right_ref)
            left_col = resolve_column(_table_by_name(tables, left_t), left_c)
            right_col = resolve_column(_table_by_name(tables, right_t), right_c)
            conditions.append(left_col == right_col)
        condition = sa.and_(*conditions) if len(conditions) > 1 else conditions[0]
        isouter = join.type == "left"
        stmt = stmt.join(right, condition, isouter=isouter)

    name_to_physical = effective_name_map(query)
    stmt = _apply_mandatory_row_filters(stmt, policy, tables, name_to_physical, principal)

    if query.where is not None:
        stmt = stmt.where(_compile_where(query.where, tables, alias_map={}))

    if query.group_by:
        stmt = stmt.group_by(
            *[
                _resolve_output_ref(ref, tables, alias_map, allow_table_fallback=True)
                for ref in query.group_by
            ]
        )

    for pred in query.having:
        target = _resolve_predicate_target(pred, tables, alias_map)
        stmt = stmt.having(_apply_predicate(target, pred, tables))

    is_aggregate = bool(query.group_by) or any(
        isinstance(i, _AGGREGATE_SELECT_ITEM_TYPES) for i in query.select
    )

    allow_table_fallback = True
    if query.top_n is not None:
        stmt, alias_map = _apply_top_n(stmt, query, tables, alias_map, is_aggregate, dialect)
        allow_table_fallback = False

    adapter = get_dialect_adapter(dialect)
    for order in query.order_by:
        col = _resolve_output_ref(order.col, tables, alias_map, allow_table_fallback)
        stmt = stmt.order_by(*adapter.order_by_terms(col, order.dir, order.nulls))

    limit = clamp_limit(query.limit, policy, is_aggregate=is_aggregate)
    stmt = stmt.limit(limit)
    if query.offset:
        stmt = stmt.offset(query.offset)

    return stmt, limit
