"""Compile a validated StructuredQuery + policy into a SQLAlchemy Select.

Called only after validation/policy_validation.py and validation/
schema_validation.py have both passed — every identifier here is already
known to exist and be policy-permitted.
"""

from __future__ import annotations

import operator
from typing import Any, Dict, List, NamedTuple, Optional, Tuple, get_args

import sqlalchemy as sa

from querygate.compiler.dialect_adapters import get_dialect_adapter
from querygate.connections.models import DatabaseDialect
from querygate.core.auth import Principal
from querygate.core.exceptions import QueryValidationError
from querygate.policy.models import Policy
from querygate.query_ast.models import (
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
    ExprFn,
    ExtractExpr,
    FunctionExpr,
    LiteralExpr,
    NowExpr,
    PercentileContSelectItem,
    Predicate,
    ScalarFunctionArg,
    ScalarFunctionSelectItem,
    StringAggSelectItem,
    StructuredQuery,
    WhereNode,
    WindowSelectItem,
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

# Window functions (item 101). Every one of these is spelled identically on
# Postgres, MSSQL and SQLite — the genuine per-dialect variance is in the FRAME
# grammar, which is why `DialectAdapter.window_frame` exists and this stays a
# flat dict (the right-weight version of the registry pattern, per CLAUDE.md's
# "don't over-apply this" note). Ranking fns are reused from `_RANK_FNS`, so
# `top_n` and a window projection can never drift on what `rank` means.
_WINDOW_FNS = {
    **_AGG_FNS,
    **_RANK_FNS,
    "ntile": sa.func.ntile,
    "lag": sa.func.lag,
    "lead": sa.func.lead,
    "first_value": sa.func.first_value,
    "last_value": sa.func.last_value,
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


# Dialect-universal expression functions stay OFF the DialectAdapter, exactly as
# its docstring requires. The four that genuinely diverge live on the adapter's
# `scalar_function` (CEILING vs ceil, LEN vs LENGTH, ROUND's argument rules,
# substr vs SUBSTRING).
_UNIVERSAL_EXPR_FNS = {
    "coalesce": sa.func.coalesce,
    "lower": sa.func.lower,
    "upper": sa.func.upper,
    "trim": sa.func.trim,
    "concat": sa.func.concat,
    "abs": sa.func.abs,
    "floor": sa.func.floor,
    "nullif": sa.func.nullif,
    "replace": sa.func.replace,
}

_CAST_TYPES = {
    # `Unicode`, not `Text` — and the reason is DATA CORRUPTION, not syntax.
    # Verified against a real SQL Server 2022: `Text` renders `VARCHAR(max)`
    # there (a *connected* MSSQL dialect sets `deprecate_large_types` after
    # checking the server version, so it does NOT emit the deprecated `TEXT`
    # that an unconnected `mssql.dialect()` shows — a rendering-only assertion
    # is actively misleading here). `VARCHAR` is codepage-limited: under the
    # default SQL_Latin1_General_CP1_CI_AS collation, casting 'δ-λ' through it
    # silently yields 'd-?'. `Unicode` renders `NVARCHAR(max)` and round-trips
    # intact, and is the same type `MSSQLDialectAdapter.column_mask` already
    # casts through. On Postgres/SQLite both spellings render VARCHAR, so this
    # choice costs nothing there. Pinned by
    # tests/integration/test_mssql_expression_substrate.py, which asserts the
    # non-ASCII round-trip rather than the SQL text.
    "text": sa.Unicode,
    "integer": sa.Integer,
    "numeric": sa.Numeric,
    "boolean": sa.Boolean,
    "date": sa.Date,
    "timestamp": sa.DateTime,
}

# The expression functions whose SQL genuinely differs per dialect — derived,
# not hand-listed, so it cannot fall out of step with `_UNIVERSAL_EXPR_FNS`.
#
# This is exported because the real-database suites are PARAMETERIZED over it:
# `tests/integration/test_postgres_expression_substrate.py` and
# `test_mssql_expression_substrate.py` each assert their live-execution cases
# cover exactly this set. Adding a function here without a real-DB case fails
# those tests, which is deliberate — item 100 shipped a `CAST(x AS text)`
# rationale that was simply WRONG ("T-SQL's deprecated TEXT is not comparable")
# because it was backed only by an assertion on generated SQL text. A connected
# SQL Server renders that spelling as VARCHAR(max), which does not error at
# all; it silently mangles non-ASCII. Rendering assertions cannot tell a correct
# rendering from one that merely looks correct (the items 75/82 trap), so
# per-dialect behavior must be proven by EXECUTING and asserting a value.
DIALECT_ROUTED_EXPR_FNS = frozenset(get_args(ExprFn)) - frozenset(_UNIVERSAL_EXPR_FNS)

# Cast targets are the same class of claim, for the same reason — see the
# `_CAST_TYPES["text"]` comment above, which is the case that proved it.
CAST_TARGETS = frozenset(_CAST_TYPES)

_BINARY_OPS = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
}


def _compile_expression(expr: Expression, tables: Dict[str, sa.Table], dialect: str) -> Any:
    """Compile one bounded scalar `Expression` (item 100) into a SQLAlchemy Core
    construct. Mirrors `_compile_where`'s shape: one recursive function that is
    the ONLY place an expression becomes SQL, so every position that accepts an
    expression (a projection, an aggregate argument, a predicate's two sides)
    renders identically and inherits every guarantee at once.

    Nothing here interpolates a string: a `ColumnExpr` resolves to a reflected
    `sa.Column`, a `LiteralExpr` binds as a parameter, and a `FunctionExpr`'s
    name comes from a closed enum — non-goal #1 is untouched by the added
    expressiveness. Depth is already bounded by `max_expression_depth` at
    validation time, so this recursion cannot be driven arbitrarily deep.
    """
    if isinstance(expr, ColumnExpr):
        return _column(tables, expr.col)
    if isinstance(expr, LiteralExpr):
        # sa.literal(), not the raw Python value: two literal operands must be
        # combined by the DATABASE, not by Python's own operators — otherwise
        # {"op": "*", "left": {"literal": "a"}, "right": {"literal": 2}} would
        # quietly evaluate to "aa" in the compiler instead of being the type
        # error the database should reject it as.
        return sa.null() if expr.literal is None else sa.literal(expr.literal)
    if isinstance(expr, BinaryOpExpr):
        left = _compile_expression(expr.left, tables, dialect)
        right = _compile_expression(expr.right, tables, dialect)
        if expr.op == "/":
            # GUARDED division (2026-07-25 Decision Log): a zero denominator
            # yields NULL on every dialect rather than inheriting Postgres's
            # hard error and MSSQL's ARITHABORT/XACT_ABORT-dependent behavior,
            # under which one bad row would abort the whole transaction.
            return left / sa.func.nullif(right, 0)
        return _BINARY_OPS[expr.op](left, right)
    if isinstance(expr, FunctionExpr):
        args = [_compile_expression(arg, tables, dialect) for arg in expr.args]
        universal = _UNIVERSAL_EXPR_FNS.get(expr.fn)
        if universal is not None:
            return universal(*args)
        return get_dialect_adapter(dialect).scalar_function(expr.fn, args)
    if isinstance(expr, CastExpr):
        # SQLAlchemy renders the per-dialect type name itself (Text -> VARCHAR(max)
        # on MSSQL, Boolean -> BIT), so a cast needs no adapter method.
        return sa.cast(_compile_expression(expr.cast, tables, dialect), _CAST_TYPES[expr.to]())
    # The three item-102 date/time nodes route entirely through the adapter:
    # unlike a cast, every one of them differs per dialect in FUNCTION NAME,
    # ARGUMENT ORDER and — for dayofweek/week — the returned VALUE, so there is
    # no dialect-universal form to share here.
    if isinstance(expr, ExtractExpr):
        operand = _compile_expression(expr.extract, tables, dialect)
        return get_dialect_adapter(dialect).extract_part(expr.part, operand)
    if isinstance(expr, NowExpr):
        return get_dialect_adapter(dialect).current_timestamp(expr.now)
    if isinstance(expr, DateAddExpr):
        operand = _compile_expression(expr.date_add, tables, dialect)
        return get_dialect_adapter(dialect).date_add(operand, expr.unit, expr.amount)
    if isinstance(expr, CaseExpr):
        whens = [
            # alias_map={} — a CASE condition can't reference a peer select
            # alias; ctx=None keeps `value_subquery` out (rejected in policy
            # validation for every CASE condition, wherever nested).
            (
                _compile_where(branch.when, tables, {}, dialect),
                _compile_expression(branch.then, tables, dialect),
            )
            for branch in expr.when
        ]
        else_value = (
            _compile_expression(expr.else_, tables, dialect) if expr.else_ is not None else None
        )
        return sa.case(*whens, else_=else_value)
    raise QueryValidationError(f"Unsupported expression node {type(expr).__name__}")


def _compile_window(item: WindowSelectItem, tables: Dict[str, sa.Table], dialect: str) -> Any:
    """Compile one item-101 `WindowSelectItem` into `fn(...) OVER (...)`.

    Every reference resolves to a real reflected column: no dialect lets an OVER
    clause reference a peer SELECT alias, so the AST requires dotted Table.Column
    refs here and this function deliberately has no `alias_map` to fall back on —
    the same rule `_apply_top_n` works around by materializing an aggregation as a
    subquery first.

    The window's own `arg` goes through `_compile_expression`, so an aggregate
    window over a computed value (`SUM(qty * price) OVER (...)`) and the guarded
    division, cap, and visitor guarantees of item 100 all apply unchanged.
    """
    adapter = get_dialect_adapter(dialect)
    args: List[Any] = []
    if item.arg is not None:
        args.append(_compile_expression(item.arg, tables, dialect))
    # `sa.literal(..., type_=Integer)`, never `literal_column`: a bucket count and
    # a lag/lead offset are caller input, and caller input binds as a typed
    # parameter rather than reaching SQL as text (plan §1 invariant 1). Explicit
    # typing keeps Postgres's function resolution unambiguous.
    if item.fn == "ntile":
        args.append(sa.literal(item.buckets, type_=sa.Integer))
    if item.offset is not None:
        args.append(sa.literal(item.offset, type_=sa.Integer))

    frame_kwargs: Dict[str, Any] = {}
    frame = item.over.frame
    if frame is not None:
        # Per-dialect frame grammar (MSSQL rejects a numeric RANGE offset).
        frame_kwargs = adapter.window_frame(
            frame.mode, frame.start.sqlalchemy_bound(), frame.end.sqlalchemy_bound()
        )
    # No frame given -> no ROWS/RANGE clause is synthesized; the dialect's
    # SQL-standard default frame applies (2026-07-26 Decision Log).

    partition_cols = [_column(tables, ref) for ref in item.over.partition_by] or None
    order_terms = [
        term
        for order in item.over.order_by
        for term in adapter.order_by_terms(_column(tables, order.col), order.dir, order.nulls)
    ] or None
    return _WINDOW_FNS[item.fn](*args).over(
        partition_by=partition_cols, order_by=order_terms, **frame_kwargs
    )


class _WhereCtx(NamedTuple):
    """Everything `_compile_where` needs to render a nested `IN (subquery)`
    (item 97): the policy/dialect/principal to compile the subquery through the
    same path, and the per-subquery reflected tables from schema validation
    (keyed by the subquery node's id)."""

    policy: Policy
    dialect: str
    principal: Optional[Principal]
    subquery_tables: Optional[Dict[int, Dict[str, sa.Table]]]


def _compile_in_subquery(pred: Predicate, ctx: Optional["_WhereCtx"]) -> sa.Select:
    """Compile a Predicate.value_subquery (item 97) into the SELECT that feeds an
    `IN (...)`. Compiled through the SAME `compile_structured_query` path as any
    query — so the subquery's tables get their mandatory row filters, min-group
    guardrail, etc. — using the subquery's OWN reflected tables (an independent,
    uncorrelated scope). The final LIMIT is stripped (`.limit(None)`): an IN value
    set must be complete or membership is wrong; the subquery is bounded by its
    filters and the tree-wide caps, not by a row limit."""
    if ctx is None or ctx.subquery_tables is None:
        raise QueryValidationError("IN (subquery) is only supported in a WHERE clause")
    subq_tables = ctx.subquery_tables.get(id(pred.value_subquery))
    if subq_tables is None:
        raise QueryValidationError("Subquery was not schema-validated")
    stmt, _limit = compile_structured_query(
        pred.value_subquery,
        subq_tables,
        ctx.policy,
        dialect=ctx.dialect,
        principal=ctx.principal,
        subquery_tables=ctx.subquery_tables,
    )
    return stmt.limit(None)


def _apply_predicate(
    col: Any,
    pred: Predicate,
    tables: Dict[str, sa.Table],
    dialect: str,
    ctx: Optional["_WhereCtx"] = None,
) -> Any:
    op = pred.op
    if pred.value_subquery is not None:
        # IN (subquery) / NOT IN (subquery) — item 97.
        subselect = _compile_in_subquery(pred, ctx)
        return col.in_(subselect) if op == "in" else ~col.in_(subselect)
    # value_col/value_expr are only valid for eq/neq/lt/lte/gt/gte (enforced at
    # the AST layer), so every other op below always sees pred.value here.
    if pred.value_col is not None:
        val = _column(tables, pred.value_col)
    elif pred.value_expr is not None:
        val = _compile_expression(pred.value_expr, tables, dialect)
    else:
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
    pred: Predicate, tables: Dict[str, sa.Table], alias_map: Dict[str, Any], dialect: str
) -> Any:
    if pred.expr is not None:
        return _compile_expression(pred.expr, tables, dialect)
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


def _compile_where(
    node: WhereNode,
    tables: Dict[str, sa.Table],
    alias_map: Dict[str, Any],
    dialect: str,
    ctx: Optional["_WhereCtx"] = None,
) -> Any:
    if isinstance(node, Predicate):
        target = _resolve_predicate_target(node, tables, alias_map, dialect)
        return _apply_predicate(target, node, tables, dialect, ctx)

    if node.not_terms is not None:
        return sa.not_(_compile_where(node.not_terms, tables, alias_map, dialect, ctx))

    children = node.and_terms or node.or_terms or []
    compiled = [_compile_where(child, tables, alias_map, dialect, ctx) for child in children]
    if node.and_terms is not None:
        return sa.and_(*compiled)
    return sa.or_(*compiled)


def _mask_for_select_ref(
    ref: str, policy: Policy, name_to_physical: Dict[str, str]
) -> Optional[Any]:
    """The ColumnMask configured for a bare projection ref, resolved against
    the physical table (an alias can never dodge a mask), or None."""
    table, column = parse_column_ref(ref)
    physical = name_to_physical.get(table.lower(), table)
    return policy.column_mask(physical, column)


def _build_select_columns(
    query: StructuredQuery,
    tables: Dict[str, sa.Table],
    dialect: str,
    policy: Policy,
    name_to_physical: Dict[str, str],
) -> Tuple[List[Any], Dict[str, Any]]:
    columns: List[Any] = []
    alias_map: Dict[str, Any] = {}

    for item in query.select:
        if isinstance(item, str):
            col = _column(tables, item)
            mask = _mask_for_select_ref(item, policy, name_to_physical)
            if mask is not None:
                # Masked in the compiled Select (validation guarantees this is
                # the only place a masked column can appear). Keep the original
                # output name so the response shape is unchanged.
                labeled = get_dialect_adapter(dialect).column_mask(col, mask).label(col.name)
                columns.append(labeled)
                alias_map[col.name] = labeled
                alias_map[item] = labeled
                continue
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

        if isinstance(item, WindowSelectItem):
            labeled = _compile_window(item, tables, dialect).label(item.alias)
            columns.append(labeled)
            # In alias_map so the query's own ORDER BY may sort by the window's
            # output name. It is deliberately NOT in `_select_aliases`, so
            # `top_n` can't reference it — that would nest one window inside
            # another's OVER clause, which no dialect allows.
            alias_map[item.alias] = labeled
            continue

        if isinstance(item, (CaseSelectItem, ExpressionSelectItem)):
            # A CaseSelectItem is exactly a CaseExpr plus a required alias, so
            # both projections go through the one `_compile_expression` path —
            # searched-CASE branch conditions included (item 99/100).
            source = item.expr if isinstance(item, ExpressionSelectItem) else item.as_expression()
            labeled = _compile_expression(source, tables, dialect).label(item.alias)
            columns.append(labeled)
            alias_map[item.alias] = labeled
            continue

        fn = _aggregate_fn(item.fn, dialect)
        if item.arg is None:
            if item.fn != "count":
                raise QueryValidationError("Only count(*) is allowed as a star aggregate")
            expr = fn()
        else:
            arg = _compile_expression(item.arg, tables, dialect)
            expr = fn(arg.distinct()) if item.distinct else fn(arg)

        alias = item.alias
        if not alias:
            if item.arg is None:
                alias = f"{item.fn}_all"
            else:
                # An aggregate with no alias always has a bare-column `arg` (the
                # `col` sugar) — the AST layer requires an explicit alias for any
                # computed argument, since there is no sensible default name.
                _, col_name = parse_column_ref(item.arg.col)
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


def applied_column_masks(query: StructuredQuery, policy: Policy) -> List[str]:
    """The output column names that would be masked when this query is compiled
    for this policy — bare projection columns whose physical column has a mask.
    Shares `policy.column_mask` with the compiler, so the two can't drift.

    Used by the audit trail to distinguish "masked" from "denied" access
    (never the pre-mask value). Names, not `table.column` refs, so it matches
    the response column names a masked caller actually sees.
    """
    name_to_physical = effective_name_map(query)
    masked: List[str] = []
    for item in query.select:
        if not isinstance(item, str):
            continue
        if _mask_for_select_ref(item, policy, name_to_physical) is not None:
            _table, column = parse_column_ref(item)
            masked.append(column)
    return masked


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
    subquery_tables: Optional[Dict[int, Dict[str, sa.Table]]] = None,
) -> Tuple[sa.Select, int]:
    """Compile AST + reflected tables + policy into a Select.

    Returns (statement, effective_limit). `subquery_tables` (item 97) maps each
    nested value_subquery node's id to its own reflected tables, so an
    `IN (subquery)` in the WHERE clause compiles through this same path recursively.
    """
    where_ctx = _WhereCtx(
        policy=policy, dialect=dialect, principal=principal, subquery_tables=subquery_tables
    )
    name_to_physical = effective_name_map(query)
    select_cols, alias_map = _build_select_columns(query, tables, dialect, policy, name_to_physical)
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

    stmt = _apply_mandatory_row_filters(stmt, policy, tables, name_to_physical, principal)

    if query.where is not None:
        stmt = stmt.where(_compile_where(query.where, tables, {}, dialect, ctx=where_ctx))

    if query.group_by:
        stmt = stmt.group_by(
            *[
                _resolve_output_ref(ref, tables, alias_map, allow_table_fallback=True)
                for ref in query.group_by
            ]
        )

    if query.having is not None:
        # HAVING is a full WhereNode (item 99), compiled through the same
        # `_compile_where` machinery as WHERE. alias_map is threaded so a HAVING
        # predicate can reference a select alias (e.g. an aggregate's `as`);
        # ctx=None keeps `value_subquery` out (WHERE-only, item 97).
        stmt = stmt.having(_compile_where(query.having, tables, alias_map, dialect))

    is_aggregate = bool(query.group_by) or any(
        isinstance(i, _AGGREGATE_SELECT_ITEM_TYPES) for i in query.select
    )

    # k-anonymity guardrail (TODO.md item 88): on an aggregate query, suppress
    # any result group backed by fewer than policy.min_group_size underlying
    # rows, so a caller can't single out an individual by aggregating over a
    # razor-thin filter. Injected like a mandatory row filter — policy-driven
    # and non-removable — and only on aggregate queries (plain row reads are
    # governed by mandatory row filters, not group size). `count()` with no
    # argument is COUNT(*), counting rows per group (or the single implicit
    # group when there's no GROUP BY), ANDed with any caller HAVING above.
    if is_aggregate and policy.min_group_size is not None:
        stmt = stmt.having(sa.func.count() >= policy.min_group_size)

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
