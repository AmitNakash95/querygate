"""Compile a validated StructuredQuery + policy into a SQLAlchemy Select.

Called only after validation/policy_validation.py and validation/
schema_validation.py have both passed — every identifier here is already
known to exist and be policy-permitted.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import sqlalchemy as sa

from querygate.core.auth import Principal
from querygate.core.exceptions import QueryValidationError
from querygate.policy.models import Policy
from querygate.query_ast.models import (
    AggregateSelectItem,
    DateBucketSelectItem,
    Predicate,
    StructuredQuery,
    WhereNode,
)
from querygate.validation.schema_validation import parse_column_ref, resolve_column

_AGG_FNS = {
    "count": sa.func.count,
    "sum": sa.func.sum,
    "avg": sa.func.avg,
    "min": sa.func.min,
    "max": sa.func.max,
}

_RANK_FNS = {
    "row_number": sa.func.row_number,
    "rank": sa.func.rank,
    "dense_rank": sa.func.dense_rank,
}


def _table_by_name(tables: Dict[str, sa.Table], name: str) -> sa.Table:
    for key, table in tables.items():
        if key.lower() == name.lower():
            return table
    raise QueryValidationError(f"Unknown table {name!r}")


def _column(tables: Dict[str, sa.Table], col_ref: str) -> sa.Column:
    table_name, column_name = parse_column_ref(col_ref)
    return resolve_column(_table_by_name(tables, table_name), column_name)


def _apply_predicate(col: Any, pred: Predicate) -> Any:
    op = pred.op
    val = pred.value
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


def _date_bucket_expr(col: Any, granularity: str, dialect: str) -> Any:
    """Date truncation, isolated per dialect (goal: dialect-specific code stays
    contained). Postgres has native date_trunc (which also covers "week" and
    "quarter" directly); MSSQL has no DATE_TRUNC, so it uses the DATEADD/
    DATEDIFF truncation idiom instead; SQLite (examples/tests) uses strftime.
    """
    if dialect == "postgresql":
        return sa.func.date_trunc(granularity, col)
    if dialect == "mssql":
        part = sa.literal_column(granularity)
        zero = sa.literal_column("0")
        return sa.func.dateadd(part, sa.func.datediff(part, zero, col), zero)
    return _sqlite_date_bucket_expr(col, granularity)


def _sqlite_date_bucket_expr(col: Any, granularity: str) -> Any:
    if granularity == "day":
        return sa.func.date(sa.func.strftime("%Y-%m-%d", col))
    if granularity == "month":
        return sa.func.date(sa.func.strftime("%Y-%m-01", col))
    if granularity == "year":
        return sa.func.date(sa.func.strftime("%Y-01-01", col))
    if granularity == "week":
        return sa.func.date(col, "weekday 0", "-6 days")
    if granularity == "quarter":
        month = sa.cast(sa.func.strftime("%m", col), sa.Integer)
        quarter_start_month = ((month - 1) / 3) * 3 + 1
        return sa.func.date(
            sa.func.strftime("%Y", col) + "-" + sa.func.printf("%02d", quarter_start_month) + "-01"
        )
    raise QueryValidationError(f"Unsupported date_bucket granularity: {granularity!r}")


def _compile_where(node: WhereNode, tables: Dict[str, sa.Table], alias_map: Dict[str, Any]) -> Any:
    if isinstance(node, Predicate):
        target = _resolve_predicate_target(node, tables, alias_map)
        return _apply_predicate(target, node)

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
            expr = _date_bucket_expr(col, item.granularity, dialect)
            alias = item.alias or f"bucket_{col.name}_{item.granularity}"
            labeled = expr.label(alias)
            columns.append(labeled)
            alias_map[alias] = labeled
            continue

        fn = _AGG_FNS[item.fn]
        if item.col == "*":
            if item.fn != "count":
                raise QueryValidationError("Only count(*) is allowed as a star aggregate")
            expr = fn()
        else:
            expr = fn(_column(tables, item.col))

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
    principal: Optional[Principal],
) -> sa.Select:
    """AND in every policy-declared mandatory filter whose table is actually
    part of this query's graph — silently skipped for tables outside the
    graph, rather than erroring, so unrelated queries aren't blocked.

    A filter's value is either a static literal or resolved from the
    authenticated principal's claims (`MandatoryRowFilter.resolve` raises
    `PolicyViolationError` if `from_claim` is set but the principal lacks
    that claim — a caller with no matching claim can't fall through to an
    unfiltered query).
    """
    for row_filter in policy.mandatory_row_filters:
        matches = [key for key in tables if key.lower() == row_filter.table.lower()]
        if not matches:
            continue
        table = tables[matches[0]]
        col = resolve_column(table, row_filter.column)
        stmt = stmt.where(col == row_filter.resolve(principal))
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
) -> Tuple[sa.Select, Dict[str, Any]]:
    """Wrap stmt with a rank-per-partition subquery, keeping only the top n rows."""
    spec = query.top_n
    output_names = [c.name for c in stmt.selected_columns]
    rank_fn = _RANK_FNS[spec.fn]

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
            _agg_col(o.col).asc() if o.dir == "asc" else _agg_col(o.col).desc()
            for o in spec.order_by
        ]
        rank_expr = rank_fn().over(partition_by=partition_cols, order_by=order_cols).label("__rank")
        ranked = sa.select(*[agg.c[name] for name in output_names], rank_expr).subquery()
    else:
        partition_cols = [
            _resolve_output_ref(ref, tables, alias_map, allow_table_fallback=True)
            for ref in spec.partition_by
        ] or None
        order_cols = [
            (
                _resolve_output_ref(o.col, tables, alias_map, allow_table_fallback=True).asc()
                if o.dir == "asc"
                else _resolve_output_ref(o.col, tables, alias_map, allow_table_fallback=True).desc()
            )
            for o in spec.order_by
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
    dialect: str = "postgresql",
    principal: Optional[Principal] = None,
) -> Tuple[sa.Select, int]:
    """Compile AST + reflected tables + policy into a Select.

    Returns (statement, effective_limit).
    """
    select_cols, alias_map = _build_select_columns(query, tables, dialect)
    base = _table_by_name(tables, query.from_table)
    stmt = sa.select(*select_cols).select_from(base)

    for join in query.joins:
        right = _table_by_name(tables, join.table)
        left_ref, right_ref = join.on
        left_t, left_c = parse_column_ref(left_ref)
        right_t, right_c = parse_column_ref(right_ref)
        left_col = resolve_column(_table_by_name(tables, left_t), left_c)
        right_col = resolve_column(_table_by_name(tables, right_t), right_c)
        isouter = join.type == "left"
        stmt = stmt.join(right, left_col == right_col, isouter=isouter)

    stmt = _apply_mandatory_row_filters(stmt, policy, tables, principal)

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
        stmt = stmt.having(_apply_predicate(target, pred))

    is_aggregate = bool(query.group_by) or any(
        isinstance(i, AggregateSelectItem) for i in query.select
    )

    allow_table_fallback = True
    if query.top_n is not None:
        stmt, alias_map = _apply_top_n(stmt, query, tables, alias_map, is_aggregate)
        allow_table_fallback = False

    for order in query.order_by:
        col = _resolve_output_ref(order.col, tables, alias_map, allow_table_fallback)
        stmt = stmt.order_by(col.asc() if order.dir == "asc" else col.desc())

    limit = clamp_limit(query.limit, policy, is_aggregate=is_aggregate)
    stmt = stmt.limit(limit)
    if query.offset:
        stmt = stmt.offset(query.offset)

    return stmt, limit
