"""Compile a validated StructuredQuery + policy into a SQLAlchemy Select.

Called only after validation/policy_validation.py and validation/
schema_validation.py have both passed — every identifier here is already
known to exist and be policy-permitted.
"""

from __future__ import annotations

import operator
from typing import Any, Dict, FrozenSet, List, NamedTuple, Optional, Tuple, get_args

import sqlalchemy as sa

from querygate.compiler.dialect_adapters import get_dialect_adapter
from querygate.connections.models import DatabaseDialect
from querygate.core.auth import Principal
from querygate.core.exceptions import PolicyViolationError, QueryValidationError
from querygate.policy.models import Policy
from querygate.query_ast.models import (
    ArrayAggSelectItem,
    BinaryOpExpr,
    CaseExpr,
    CaseSelectItem,
    CastExpr,
    ColArg,
    ColumnExpr,
    CteSpec,
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
    iter_set_op_arms,
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
    # Item 105 — carried so an IN (subquery) whose own FROM names a cte binds to
    # the compiled block, exactly like any other scope.
    cte_objects: Optional[Dict[str, Any]] = None


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
        cte_objects=ctx.cte_objects,
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


def _equality_bound_columns(join: Any) -> Optional[set]:
    """The joined table's columns pinned by equality to another table's column,
    lowercased — or None if this join's shape is not a pure equality conjunction.

    Both spellings are read: `on`/`extra_on` pairs, and a `condition` that is a
    `Predicate` or an all-`and` tree of `eq` predicates comparing `col` to
    `value_col` (item 103's general form can still express plain equality, and a
    caller who writes it that way must not be treated as if they had written a
    range join). Anything else — an inequality, an OR/NOT tree, a computed
    operand, a cross join — returns None, meaning "cannot be shown to be an
    equality join".
    """
    if join.type == "cross":
        return None

    joined = (join.alias or join.table).lower()
    bound: set = set()

    def _take(left_ref: str, right_ref: str) -> bool:
        for ref, other in ((left_ref, right_ref), (right_ref, left_ref)):
            table, column = parse_column_ref(ref)
            other_table, _ = parse_column_ref(other)
            # Only a comparison against a DIFFERENT table constrains the join's
            # grain; `a.x = a.y` says nothing about how many right rows match.
            if table.lower() == joined and other_table.lower() != joined:
                bound.add(column.lower())
                return True
        return False

    if join.on is not None:
        for pair in [join.on, *join.extra_on]:
            _take(pair[0], pair[1])
        return bound

    def _walk(node: Any) -> None:
        """Collect the equalities that hold unconditionally for every matched row.

        Only top-level conjuncts qualify, and a non-qualifying conjunct is skipped
        rather than failing the walk — because *narrowing* a condition can never
        make it match more rows. If one conjunct pins a unique key, at most one
        right-hand row satisfies it, so the whole (more restrictive) condition
        matches at most one too: `pk = x AND price BETWEEN lo AND hi` cannot fan
        out, and neither can `pk = x AND (a OR b)`.

        An OR/NOT subtree is simply not descended into: an equality inside one
        holds on only some branches, so it constrains nothing. That is what makes
        a bare `a = b OR c = d` fall through with nothing pinned, and be refused.
        """
        if isinstance(node, Predicate):
            if node.op == "eq" and node.col is not None and node.value_col is not None:
                _take(node.col, node.value_col)
            return
        for term in node.and_terms or []:
            _walk(term)

    if join.condition is None:
        return None
    _walk(join.condition)
    return bound


def _unique_column_sets(source: Any) -> List[set]:
    """Every set of column names that is unique in `source`, lowercased —
    its primary key, plus every unique constraint and unique index reflected
    from the database (backends surface these differently: SQLite reports a
    `UniqueConstraint`, Postgres typically a unique `Index`, so both are read).

    **Only a `Table` carries that metadata**, and everything else must be handled
    rather than assumed away — this used to take `sa.Table` and reach straight for
    `.primary_key.columns`, which raises `AttributeError` on every other FROM
    element, because `Alias`/`Subquery`/`CTE` expose `.primary_key` as a bare
    `ColumnSet` with no `.columns`. That was a live crash on a shipped feature
    (TODO.md item 122): item 118's k-anonymity fan-out check runs on the joined
    table, so `min_group_size` plus ANY join carrying an `alias` raised instead of
    deciding — a 500 where a policy answer belonged, and no cte required to reach
    it. Measured 2026-07-27 while wiring item 105, which joins onto a `CTE` and hit
    the same line from the new direction.

    An alias of a table keeps that table's uniqueness (same rows, new name), so it
    looks through to the element. A cte or a subquery has **no declared
    uniqueness**, so it returns empty — which makes `_join_can_fan_out` answer
    "yes, this can fan out", the fail-closed direction the k-anonymity floor
    requires: a computed stage may legitimately hold several rows per join key, and
    assuming otherwise would silently reopen exactly the leak item 118 closed.
    """
    table = source
    while isinstance(table, sa.Alias):
        table = table.element
    if not isinstance(table, sa.Table):
        return []
    sets: List[set] = []
    pk = {c.name.lower() for c in table.primary_key.columns}
    if pk:
        sets.append(pk)
    for constraint in table.constraints:
        if isinstance(constraint, sa.UniqueConstraint) and len(constraint.columns) > 0:
            sets.append({c.name.lower() for c in constraint.columns})
    for index in table.indexes:
        if index.unique and len(index.columns) > 0:
            sets.append({c.name.lower() for c in index.columns})
    return sets


def _join_can_fan_out(join: Any, tables: Dict[str, sa.Table]) -> bool:
    """Can this join match MORE than one right-hand row per left-hand row?

    It cannot, iff the join pins a set of the joined table's columns by equality
    and that set covers one of the table's unique keys — the ordinary
    join-to-a-dimension-on-its-primary-key shape. Everything else is assumed to
    fan out, which is the fail-closed direction: an unreflected uniqueness
    constraint costs a rejection, a missed fan-out would cost the guarantee.
    """
    bound = _equality_bound_columns(join)
    if bound is None:
        return True
    table = _table_by_name(tables, join.alias or join.table)
    return not any(unique <= bound for unique in _unique_column_sets(table))


def _apply_mandatory_row_filters(
    stmt: sa.Select,
    policy: Policy,
    tables: Dict[str, sa.Table],
    name_to_physical: Dict[str, str],
    principal: Optional[Principal],
    cte_names: FrozenSet[str],
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

    `cte_names` (item 105) are skipped: a filter names a TABLE, and a block that
    happens to share that name is a different thing entirely — applying the filter
    to its output would filter the wrong rows, or raise if the block projects no
    column by that name. Policy validation already refuses a cte named after a
    filtered table, so this is defence in depth on the compile side rather than the
    only guard. The filter is NOT lost: the block's body is compiled through this
    same function against its own real tables, which is where those rows are read.
    """
    for row_filter in policy.mandatory_row_filters:
        matches = [
            key
            for key in tables
            if name_to_physical.get(key.lower(), key).lower() == row_filter.table.lower()
            and name_to_physical.get(key.lower(), key).lower() not in cte_names
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

    Every set-operation arm contributes (item 104), because every arm contributes
    rows to the one response — reading only the carrying query would under-report
    a mask applied to arm 2 and make the audit trail say less than the compiler
    actually did. A nested `value_subquery` deliberately does NOT contribute: its
    columns feed an `IN` comparison rather than the response, and a masked column
    is rejected there outright.

    A cte body (item 105) does not contribute either, for that same second reason
    and with the same rejection behind it: `_validate_cte_constraints` refuses a
    masked column as a block's projection, so no mask can originate inside one. The
    outer query's `daily.total` ref then resolves to a cte OUTPUT name, which has no
    physical (table, column) pair for `policy.column_mask` to match — correctly,
    since the block it came from could not have carried a masked value. That makes
    the empty contribution here a consequence of the validation rule rather than an
    omission; if that rule were ever relaxed, this function would have to walk cte
    bodies, and `test_cte.py` pins the pairing.

    A later arm's mask is reported under **arm 1's** output name at the same
    position, because that is the name the response actually carries: a compound
    SELECT takes its column names from its first arm. Reporting the masked arm's
    own column name would name a key the caller never receives — which is exactly
    what this function's "matches the response column names" contract forbids.

    **The set-op semantics are a UNION across arms, and that is deliberate.** An
    output column is listed when *at least one* arm masks it, so for a set
    operation this reads "this response column carries masked values for some of
    its rows", not "for all of them" — a column masked in arm 2 but not arm 1
    genuinely contains both. Union is the right direction for an audit field whose
    job is to distinguish masked from denied: it over-states protection rather than
    under-stating it, so it can never claim a raw value was masked when the
    opposite is what happened. Reporting per-arm instead would need a shape change
    to the persisted event, which is not worth it for this distinction.
    """
    arms = list(iter_set_op_arms(query))
    output_names = [_projection_output_name(item) for item in arms[0].select]
    masked: List[str] = []
    for arm in arms:
        name_to_physical = effective_name_map(arm)
        for index, item in enumerate(arm.select):
            if not isinstance(item, str):
                continue
            if _mask_for_select_ref(item, policy, name_to_physical) is None:
                continue
            # Falls back to this arm's own name only when arm 1 projects something
            # with no statically-known output name at that position (an unaliased
            # aggregate); every other shape resolves exactly.
            column = output_names[index] or parse_column_ref(item)[1]
            if column not in masked:
                masked.append(column)
    return masked


def _projection_output_name(item: Any) -> Optional[str]:
    """The response key a select item produces, where that is knowable without
    reflected tables: a bare `Table.Column`'s column name, or an explicit alias."""
    if isinstance(item, str):
        return parse_column_ref(item)[1]
    return getattr(item, "alias", None)


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
    cte_objects: Optional[Dict[str, Any]] = None,
) -> Tuple[sa.Select, int]:
    """Compile AST + reflected tables + policy into a Select.

    Returns (statement, effective_limit). `subquery_tables` (items 97 and 104) maps
    each nested value_subquery node's id — and each set-operation arm's — to its own
    reflected tables, so an `IN (subquery)` in the WHERE clause and every set-op arm
    compile through this same path recursively.

    `cte_objects` (item 105) maps each lowercased cte name to the compiled `WITH`
    block, so a scope referencing one by name binds to the real construct rather
    than to the typeless placeholder schema validation resolved its columns
    against. It is built here on the way in and threaded down, never rebuilt.
    """
    if cte_objects is None and query.ctes:
        cte_objects = {}
        for spec in query.ctes:
            # Compiled in declaration order, which the no-forward-reference rule
            # makes dependency order, so a block reading an earlier block finds it
            # already in `cte_objects` below.
            cte_objects[spec.name.lower()] = _compile_cte(
                spec, policy, dialect, principal, subquery_tables, cte_objects
            )

    if query.set_op is not None:
        return _compile_set_operation(
            query, tables, policy, dialect, principal, subquery_tables, cte_objects
        )
    stmt, alias_map, is_aggregate = _compile_scope_body(
        query, tables, policy, dialect, principal, subquery_tables, cte_objects
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


def _compile_cte(
    spec: CteSpec,
    policy: Policy,
    dialect: str,
    principal: Optional[Principal],
    subquery_tables: Optional[Dict[int, Dict[str, sa.Table]]],
    cte_objects: Dict[str, Any],
) -> Any:
    """Compile one named `WITH` block (item 105) through the SAME
    `compile_structured_query` path any query takes — so the block inherits its
    mandatory row filters, its column masks, its `min_group_size` floor and item
    118's fan-out refusal. There is deliberately no reduced compile path for a cte,
    for the reason item 104 gave for arms: a second path is somewhere a filter can
    be forgotten.

    **The row cap is stripped when the caller set no `limit`, and that is a
    correctness decision rather than a relaxation.** `clamp_limit` would otherwise
    apply `default_limit`/`max_limit` to a stage whose rows are *input* to a join
    or an aggregate, silently truncating the population a total is computed over —
    a wrong answer, which items 102 and 117 established this project treats as
    worse than a rejection. Item 97's `_compile_in_subquery` strips it for the same
    reason (an incomplete `IN` list is a wrong membership test). An EXPLICIT limit
    is kept and stays clamped, because that is the caller asking for "the top 100",
    not a guardrail. What bounds a block instead: `timeout_seconds`, the tree-wide
    caps every scope shares, and `max_cte_count`.
    """
    body_tables = (subquery_tables or {}).get(id(spec.query))
    if body_tables is None:
        # Same fail-closed posture as `_arm_tables`: compiling a block against
        # another scope's tables would be a silent cross-scope resolution.
        raise QueryValidationError(f"cte {spec.name!r} was not schema-validated")
    stmt, _limit = compile_structured_query(
        spec.query,
        body_tables,
        policy,
        dialect=dialect,
        principal=principal,
        subquery_tables=subquery_tables,
        cte_objects=cte_objects,
    )
    if spec.query.limit is None:
        stmt = stmt.limit(None)
    return stmt.cte(name=spec.name)


def _resolve_cte_references(
    query: StructuredQuery,
    tables: Dict[str, sa.Table],
    cte_objects: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Swap each cte reference's validation placeholder for the compiled `WITH`
    block (item 105).

    Schema validation resolves `daily.n` against a typeless `sa.Table` standing in
    for the block's projection, which is the right shape for answering "does this
    block project that name?" but would render as a reference to a table called
    `daily` that does not exist. Substituting here — once, at the entry to the
    scope body — means every downstream site (`_table_by_name`, `_column`, the join
    loop, mandatory filters) keeps working on whatever it is handed, with no
    "is this a cte?" branch of its own.
    """
    if not cte_objects:
        return tables
    name_to_physical = effective_name_map(query)
    resolved: Dict[str, Any] = dict(tables)
    for name in tables:
        source_name = name_to_physical.get(name.lower(), name).lower()
        cte = cte_objects.get(source_name)
        if cte is not None:
            resolved[name] = cte if name.lower() == source_name else cte.alias(name)
    return resolved


def _arm_tables(
    arm: StructuredQuery, subquery_tables: Optional[Dict[int, Dict[str, sa.Table]]]
) -> Dict[str, sa.Table]:
    """One set-operation arm's own reflected tables, from the map schema
    validation populated. Fails closed with a typed error rather than compiling an
    arm against the wrong scope's tables — the same guard `_compile_in_subquery`
    applies to a nested subquery, for the same reason."""
    scoped = (subquery_tables or {}).get(id(arm))
    if scoped is None:
        raise QueryValidationError("Set-operation arm was not schema-validated")
    return scoped


def _compile_set_operation(
    query: StructuredQuery,
    tables: Dict[str, sa.Table],
    policy: Policy,
    dialect: str,
    principal: Optional[Principal],
    subquery_tables: Optional[Dict[int, Dict[str, sa.Table]]],
    cte_objects: Optional[Dict[str, Any]] = None,
) -> Tuple[sa.Select, int]:
    """Compile one item-104 set operation: every arm through the SAME
    `_compile_scope_body` any single query uses, combined by the dialect adapter,
    then wrapped in a plain SELECT that carries the shared ORDER BY / LIMIT / OFFSET.

    Two things here are load-bearing rather than stylistic.

    **Every arm goes through `_compile_scope_body`, not a reduced path.** That is
    what makes each arm inherit its own mandatory row filters, its own column
    masks, and its own `min_group_size` floor — including item 118's refusal of a
    fan-out join under that floor. A set operation must not be a channel to dodge a
    per-table filter, and the way to guarantee that is to have no second compile
    path where one could be forgotten.

    **The compound is wrapped in a derived table before LIMIT is applied, and that
    is not cosmetic.** SQLAlchemy's MSSQL dialect SILENTLY DROPS `.limit()` on a
    `CompoundSelect` (measured: `SELECT ... UNION SELECT ...` renders with no TOP
    and no FETCH, while Postgres renders `LIMIT`). `clamp_limit` is a policy
    guardrail, so a dropped LIMIT is an unbounded response on one dialect only.
    Wrapping makes the limit a limit on a plain SELECT, which both dialects render
    correctly (`TOP n` / `LIMIT n`), and gives ORDER BY real derived-table columns
    to resolve against instead of a bare output name.
    """
    spec = query.set_op
    assert spec is not None  # nosec B101 — only reached from the branch above
    adapter = get_dialect_adapter(dialect)

    arm_statements: List[sa.Select] = []
    arm_aggregates: List[bool] = []
    for arm in iter_set_op_arms(query):
        arm_stmt, _arm_alias_map, arm_is_aggregate = _compile_scope_body(
            arm,
            # Identity, not a list index — the same rule `validate_schema` uses to
            # pick the outer scope's tables, so the two cannot disagree about which
            # query is the carrier if `iter_set_op_arms`'s ordering ever changes.
            tables if arm is query else _arm_tables(arm, subquery_tables),
            policy,
            dialect,
            principal,
            subquery_tables,
            cte_objects,
        )
        arm_aggregates.append(arm_is_aggregate)
        arm_statements.append(arm_stmt)

    derived = adapter.set_operation(spec.op, spec.all_, arm_statements).subquery()
    output_columns = list(derived.c)
    stmt = sa.select(*output_columns).select_from(derived)

    # ORDER BY on a set operation resolves against the OUTPUT of the combined
    # result, never a table column — `ORDER BY Customer.id` after a UNION is not
    # valid SQL on any backend. So `allow_table_fallback=False`, and the alias map
    # is built from the derived table.
    #
    # The written-ref half of that map is POSITIONAL, and that is load-bearing
    # rather than stylistic. `output_columns` is arm 1's select list in order, so
    # index i is the column item i produced. Keying it by *name* instead — via the
    # arm's own alias map — silently misbinds whenever two projections share a base
    # name: SQLAlchemy disambiguates the derived table's keys (`id`, `id_1`) but
    # both refs still report `.name == "id"`, so `ORDER BY orders.id` bound to
    # `customers.id`. Measured before the fix: the caller's requested ordering was
    # replaced by a different column's, and because ORDER BY feeds LIMIT that
    # returns a different ROW SET, silently, on every dialect. (`_apply_top_n` has
    # the same name-keyed shape and a worse version of the bug — TODO.md item 119.)
    alias_map: Dict[str, Any] = {column.name: column for column in output_columns}
    for item, column in zip(query.select, output_columns):
        if isinstance(item, str):
            alias_map.setdefault(item, column)

    for order in query.order_by:
        col = _resolve_output_ref(order.col, tables, alias_map, allow_table_fallback=False)
        stmt = stmt.order_by(*adapter.order_by_terms(col, order.dir, order.nulls))

    # The higher aggregate ceiling applies only when EVERY arm is aggregated. One
    # raw-row arm means the response contains raw rows, which is what
    # `max_limit` (not `max_limit_aggregate`) is sized for.
    limit = clamp_limit(query.limit, policy, is_aggregate=all(arm_aggregates))
    stmt = stmt.limit(limit)
    if query.offset:
        stmt = stmt.offset(query.offset)
    return stmt, limit


def _compile_scope_body(
    query: StructuredQuery,
    tables: Dict[str, sa.Table],
    policy: Policy,
    dialect: str,
    principal: Optional[Principal],
    subquery_tables: Optional[Dict[int, Dict[str, sa.Table]]],
    cte_objects: Optional[Dict[str, Any]] = None,
) -> Tuple[sa.Select, Dict[str, Any], bool]:
    """Everything a single SELECT is made of — projection, FROM, joins, mandatory
    row filters, WHERE, GROUP BY, HAVING and the k-anonymity floor — with no
    ORDER BY / LIMIT / OFFSET / top_n.

    Split out because those trailing clauses belong to the STATEMENT while
    everything above belongs to the SELECT: a set-operation arm (item 104) needs
    exactly this much and no more. Returns the statement, its alias map, and
    whether it aggregates.
    """
    where_ctx = _WhereCtx(
        policy=policy,
        dialect=dialect,
        principal=principal,
        subquery_tables=subquery_tables,
        cte_objects=cte_objects,
    )
    # One substitution point for the whole scope (item 105) — every use of
    # `tables` below is a cte reference or a real table without needing to know.
    tables = _resolve_cte_references(query, tables, cte_objects)
    name_to_physical = effective_name_map(query)
    select_cols, alias_map = _build_select_columns(query, tables, dialect, policy, name_to_physical)
    base = _table_by_name(tables, query.from_alias or query.from_table)
    stmt = sa.select(*select_cols).select_from(base)
    if query.distinct:
        stmt = stmt.distinct()

    for join in query.joins:
        right = _table_by_name(tables, join.alias or join.table)
        if join.type == "cross":
            # SQLAlchemy Core has no cross-join constructor on Select, and a
            # comma-separated FROM is the older implicit spelling. `ON true`
            # (rendered `ON 1 = 1` where a dialect has no boolean literal) is the
            # explicit, portable form and is the same cartesian product to every
            # planner — no caller-derived content is involved.
            stmt = stmt.join(right, sa.true())
            continue
        if join.condition is not None:
            # A general join condition (item 103) is compiled by the SAME
            # `_compile_where` the WHERE clause uses — no second predicate
            # compiler to drift. `alias_map={}` because a join is evaluated before
            # the projection exists, and `ctx` is left at its default None so a
            # `value_subquery` fails closed here too (policy validation already
            # rejects one with a typed error; this is the defence in depth).
            condition = _compile_where(join.condition, tables, {}, dialect)
        else:
            conditions = []
            for left_ref, right_ref in [join.on, *join.extra_on]:
                left_t, left_c = parse_column_ref(left_ref)
                right_t, right_c = parse_column_ref(right_ref)
                left_col = resolve_column(_table_by_name(tables, left_t), left_c)
                right_col = resolve_column(_table_by_name(tables, right_t), right_c)
                conditions.append(left_col == right_col)
            condition = sa.and_(*conditions) if len(conditions) > 1 else conditions[0]
        stmt = stmt.join(right, condition, isouter=join.type == "left", full=join.type == "full")

    stmt = _apply_mandatory_row_filters(
        stmt, policy, tables, name_to_physical, principal, frozenset(cte_objects or {})
    )

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
        # The floor counts JOINED rows, so a join that matches many right-hand
        # rows per left-hand row multiplies a group's count and can lift a
        # single-row group above k — the guarantee silently fails (TODO.md item
        # 118, measured 2026-07-27: with k=5, a lone person joined to a 10-row
        # table on a shared non-unique column was returned).
        #
        # Rejected rather than silently exempted, the same posture item 101 took
        # for aggregate windows under this floor: when the floor cannot be
        # enforced correctly, the query fails closed instead of returning an
        # answer the policy believes is protected. The rejection is scoped to the
        # shapes that actually break it — a join onto a unique key (the ordinary
        # join-to-a-dimension shape) matches at most one row, cannot inflate a
        # count, and stays allowed.
        fanning = [
            join.alias or join.table for join in query.joins if _join_can_fan_out(join, tables)
        ]
        if fanning:
            raise PolicyViolationError(
                f"join to {fanning[0]!r} can match more than one row per row of the "
                f"query's other tables, which would inflate the count that "
                f"min_group_size ({policy.min_group_size}) floors — so the k-anonymity "
                "guarantee cannot hold for this query. Join on the target table's "
                "primary key or a unique column, or aggregate without the join and "
                "combine results client-side."
            )
        stmt = stmt.having(sa.func.count() >= policy.min_group_size)

    return stmt, alias_map, is_aggregate
