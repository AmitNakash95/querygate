"""Typed, fluent builder for QueryGate's ``StructuredQuery`` AST (TODO.md item 51).

This is a pure client-side authoring convenience — it constructs the very
same ``query_ast`` Pydantic models the server validates, then serializes them
to the exact wire JSON a REST/MCP call expects. It adds **no** validation of
its own and cannot bypass any server-side check: whatever it produces is still
re-validated against the live schema and the caller's policy by
``StructuredQueryService`` before a single row is touched. Its only job is to
turn error-prone raw-JSON authoring into typed method calls with editor
autocomplete and build-time feedback.

Because ``build()`` instantiates the real models (``AggregateSelectItem``,
``Predicate``, ``StructuredQuery`` …), an illegal combination — ``count(*)``
with ``distinct``, a ``between`` without a two-element list, a self-join
missing an alias — raises here, before the request ever leaves the process,
with the identical message the server would return. Nothing is duplicated and
nothing can drift: the builder *is* a thin front-end over the canonical AST.

Example::

    from querygate.client import Query, agg, col, desc

    q = (
        Query.from_("orders")
        .join("customers", on=("orders.customer_id", "customers.id"))
        .select("customers.name", agg.sum("orders.total_amount", as_="total_spend"))
        .where(col("customers.country") == "GB")
        .where(col("orders.created_at") >= "2024-01-01")
        .group_by("customers.name")
        .order_by("total_spend", desc=True)
        .limit(10)
    )
    body = q.to_dict()   # -> the wire JSON to POST to /api/v1/<conn>/query
"""

from __future__ import annotations

from typing import Any, Iterable, List, Optional, Sequence, Tuple, Union

from querygate.query_ast.models import (
    AggregateFn,
    AggregateSelectItem,
    ArrayAggSelectItem,
    BinaryOp,
    BinaryOpExpr,
    CaseExpr,
    CaseSelectItem,
    CaseWhen,
    CastExpr,
    CastType,
    ColArg,
    ColumnExpr,
    CompareOp,
    DateBucketSelectItem,
    DateGranularity,
    Expression,
    ExpressionSelectItem,
    ExprFn,
    FunctionExpr,
    JoinSpec,
    JoinType,
    LiteralArg,
    LiteralExpr,
    OrderBySpec,
    PercentileContSelectItem,
    Predicate,
    RankFn,
    ScalarFn,
    ScalarFunctionArg,
    ScalarFunctionCall,
    ScalarFunctionSelectItem,
    SelectItem,
    StringAggSelectItem,
    StructuredQuery,
    TopNSpec,
    WhereGroup,
    WhereNode,
)

# The concrete Expression classes, for isinstance checks against an already-built
# AST node handed straight to a builder helper.
ExpressionModels = (ColumnExpr, LiteralExpr, BinaryOpExpr, FunctionExpr, CastExpr, CaseExpr)

__all__ = [
    "Query",
    "col",
    "col_fn",
    "lit",
    "fn",
    "agg",
    "date_bucket",
    "string_agg",
    "array_agg",
    "percentile_cont",
    "fn_select",
    "case",
    "when",
    "and_",
    "or_",
    "not_",
    "asc",
    "desc",
    "expr",
    "expr_fn",
    "expr_select",
    "case_expr",
    "cast",
    "Column",
    "FnColumn",
    "Literal",
    "Expr",
]


# --------------------------------------------------------------------------- #
# Value wrappers
# --------------------------------------------------------------------------- #
class Literal:
    """An explicit literal value, disambiguating it from a column reference in
    scalar-function arguments and CASE branches (where a bare string would be
    ambiguous). Prefer the ``lit(...)`` factory."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value


def lit(value: Any) -> Literal:
    """Wrap a literal scalar (string/number/bool/None) for use as a scalar
    function argument or CASE result, e.g. ``fn("coalesce", col("a"), lit("n/a"))``."""
    return Literal(value)


class _Unset:
    __slots__ = ()


_UNSET = _Unset()


class _Comparable:
    """Shared comparison surface for a column or a scalar-function-of-a-column;
    every operator/method returns a fully-built ``Predicate``. Operator
    overloads (``==``, ``!=``, ``<`` …) are provided for ergonomics and return
    a ``Predicate`` rather than a bool — the standard query-DSL trade-off."""

    # Identity hashing: defining __eq__ would otherwise make instances
    # unhashable, and these are only ever used to *build* predicates.
    __hash__ = object.__hash__

    def _target(self) -> dict:  # pragma: no cover - overridden
        raise NotImplementedError

    def _predicate(
        self,
        op: CompareOp,
        *,
        value: Any = _UNSET,
        value_col: Optional[str] = None,
        value_expr: Optional[Expression] = None,
    ) -> Predicate:
        kwargs = dict(self._target())
        kwargs["op"] = op
        if value is not _UNSET:
            kwargs["value"] = value
        if value_col is not None:
            kwargs["value_col"] = value_col
        if value_expr is not None:
            kwargs["value_expr"] = value_expr
        return Predicate(**kwargs)

    def _cmp(self, op: CompareOp, other: Any) -> Predicate:
        if isinstance(other, Column):
            return self._predicate(op, value_col=other.name)
        if isinstance(other, (Expr, *ExpressionModels)):
            # Comparing against a computed right-hand side (item 100), e.g.
            # col("oi.price") > col("oi.cost") * 1.2.
            return self._predicate(op, value_expr=_to_expression(other))
        return self._predicate(op, value=_unwrap(other))

    # Operator overloads -------------------------------------------------- #
    def __eq__(self, other: Any) -> Predicate:  # type: ignore[override]
        return self._cmp("eq", other)

    def __ne__(self, other: Any) -> Predicate:  # type: ignore[override]
        return self._cmp("neq", other)

    def __lt__(self, other: Any) -> Predicate:
        return self._cmp("lt", other)

    def __le__(self, other: Any) -> Predicate:
        return self._cmp("lte", other)

    def __gt__(self, other: Any) -> Predicate:
        return self._cmp("gt", other)

    def __ge__(self, other: Any) -> Predicate:
        return self._cmp("gte", other)

    # Named equivalents (readable, and the only way to compare against a
    # column on some code styles that avoid operator overloading) ---------- #
    def eq(self, other: Any) -> Predicate:
        return self._cmp("eq", other)

    def neq(self, other: Any) -> Predicate:
        return self._cmp("neq", other)

    def lt(self, other: Any) -> Predicate:
        return self._cmp("lt", other)

    def lte(self, other: Any) -> Predicate:
        return self._cmp("lte", other)

    def gt(self, other: Any) -> Predicate:
        return self._cmp("gt", other)

    def gte(self, other: Any) -> Predicate:
        return self._cmp("gte", other)

    def in_(self, values: Iterable[Any]) -> Predicate:
        return self._predicate("in", value=[_unwrap(v) for v in values])

    def not_in(self, values: Iterable[Any]) -> Predicate:
        return self._predicate("not_in", value=[_unwrap(v) for v in values])

    def like(self, pattern: str) -> Predicate:
        return self._predicate("like", value=pattern)

    def between(self, low: Any, high: Any) -> Predicate:
        return self._predicate("between", value=[_unwrap(low), _unwrap(high)])

    def is_null(self) -> Predicate:
        return self._predicate("is_null")

    def is_not_null(self) -> Predicate:
        return self._predicate("is_not_null")


class Column(_Comparable):
    """A ``Table.Column`` (or ``Alias.Column``) reference. Build with ``col``.

    The arithmetic operators lift it into an ``Expr`` (item 100), so
    ``col("oi.qty") * col("oi.price")`` reads the way it would in SQL.
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def _target(self) -> dict:
        return {"col": self.name}

    def __add__(self, other: Any) -> "Expr":
        return _binary("+", self, other)

    def __radd__(self, other: Any) -> "Expr":
        return _binary("+", other, self)

    def __sub__(self, other: Any) -> "Expr":
        return _binary("-", self, other)

    def __rsub__(self, other: Any) -> "Expr":
        return _binary("-", other, self)

    def __mul__(self, other: Any) -> "Expr":
        return _binary("*", self, other)

    def __rmul__(self, other: Any) -> "Expr":
        return _binary("*", other, self)

    def __truediv__(self, other: Any) -> "Expr":
        return _binary("/", self, other)

    def __rtruediv__(self, other: Any) -> "Expr":
        return _binary("/", other, self)


class FnColumn(_Comparable):
    """A whitelisted scalar function applied to a column, usable as a predicate
    target (``fn("lower", col("customers.name")) == "ada"``). Build with ``fn``."""

    __slots__ = ("call",)

    def __init__(self, call: ScalarFunctionCall) -> None:
        self.call = call

    def _target(self) -> dict:
        return {"col_fn": self.call}


def col(name: str) -> Column:
    """A ``Table.Column`` reference for use in predicates, select items, or as a
    scalar-function argument."""
    return Column(name)


def _unwrap(value: Any) -> Any:
    """Normalize a comparison right-hand side to a plain literal value."""
    if isinstance(value, Literal):
        return value.value
    if isinstance(value, Column):
        raise TypeError(
            "compare against a column with the operator/method directly "
            "(e.g. col('a') == col('b')); do not wrap it"
        )
    return value


def _colname(value: Union[str, "Column"]) -> str:
    if isinstance(value, Column):
        return value.name
    if isinstance(value, str):
        return value
    raise TypeError(f"expected a column name string or col(...), got {type(value).__name__}")


# --------------------------------------------------------------------------- #
# Scalar functions
# --------------------------------------------------------------------------- #
def _to_arg(value: Any) -> ScalarFunctionArg:
    if isinstance(value, Column):
        return ColArg(col=value.name)
    if isinstance(value, Literal):
        return LiteralArg(literal=value.value)
    raise TypeError(
        "scalar-function arguments and CASE results must be wrapped explicitly "
        "as col(...) for a column or lit(...) for a literal, to avoid ambiguity; "
        f"got a bare {type(value).__name__}"
    )


# --------------------------------------------------------------------------- #
# Bounded scalar expressions (TODO.md item 100)
# --------------------------------------------------------------------------- #
class Expr(_Comparable):
    """A built scalar ``Expression``. Arithmetic operators compose it further
    (``col("a.qty") * col("a.price") + 1``) and the comparison operators turn it
    into a ``Predicate`` targeting the computed value, exactly like ``Column``.
    """

    __slots__ = ("node",)

    def __init__(self, node: Expression) -> None:
        self.node = node

    def _target(self) -> dict:
        return {"expr": self.node}

    def __add__(self, other: Any) -> "Expr":
        return _binary("+", self, other)

    def __radd__(self, other: Any) -> "Expr":
        return _binary("+", other, self)

    def __sub__(self, other: Any) -> "Expr":
        return _binary("-", self, other)

    def __rsub__(self, other: Any) -> "Expr":
        return _binary("-", other, self)

    def __mul__(self, other: Any) -> "Expr":
        return _binary("*", self, other)

    def __rmul__(self, other: Any) -> "Expr":
        return _binary("*", other, self)

    def __truediv__(self, other: Any) -> "Expr":
        return _binary("/", self, other)

    def __rtruediv__(self, other: Any) -> "Expr":
        return _binary("/", other, self)


def _to_expression(value: Any) -> Expression:
    """Normalize anything usable as a scalar expression into an ``Expression``.

    A BARE Python scalar is accepted here (unlike ``_to_arg``) because an
    arithmetic operand is unambiguous — a column must be written ``col(...)``,
    so ``col("a.qty") * 2`` and ``+ "x"`` can only mean literals. Function
    arguments and CASE results keep requiring the explicit wrapper, since there
    a bare string genuinely could be either.
    """
    if isinstance(value, Expr):
        return value.node
    if isinstance(value, Column):
        return ColumnExpr(col=value.name)
    if isinstance(value, Literal):
        return LiteralExpr(literal=value.value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return LiteralExpr(literal=value)
    if isinstance(value, ExpressionModels):
        return value
    raise TypeError(
        f"cannot use {type(value).__name__} as a scalar expression — use col(...), "
        "lit(...), a bare scalar, or another expression"
    )


def _binary(op: BinaryOp, left: Any, right: Any) -> Expr:
    return Expr(BinaryOpExpr(op=op, left=_to_expression(left), right=_to_expression(right)))


def expr(value: Any) -> Expr:
    """Lift a ``col(...)``/``lit(...)``/bare scalar into an ``Expr`` so the
    arithmetic operators are available, e.g. ``expr(col("a.qty")) * 2``. A
    column already supports the operators directly; this is for the cases where
    the left operand is a plain value."""
    return Expr(_to_expression(value))


def expr_fn(name: ExprFn, *args: Any) -> Expr:
    """A whitelisted scalar function over expressions, WITH nesting —
    ``expr_fn("lower", expr_fn("trim", col("c.name")))``. Arguments follow the
    explicit-wrapper rule (``col(...)``/``lit(...)``/another expression)."""
    return Expr(FunctionExpr(fn=name, args=[_to_expression(_require_wrapped(a)) for a in args]))


def cast(value: Any, to: CastType) -> Expr:
    """``CAST(value AS type)`` over the closed target-type set."""
    return Expr(CastExpr(cast=_to_expression(_require_wrapped(value)), to=to))


def case_expr(*whens: CaseWhen, else_: Any = None) -> Expr:
    """An expression-valued ``CASE`` — the form usable INSIDE an aggregate or
    arithmetic, which is what makes conditional aggregation expressible:
    ``agg.sum(case_expr(when(col("o.status") == "paid", col("o.amount")),
    else_=lit(0)), as_="paid_total")``. Use :func:`case` for a top-level
    projection (it is the same node plus a required alias)."""
    return Expr(
        CaseExpr(
            when=list(whens),
            else_=(_to_expression(else_) if else_ is not None else None),
        )
    )


def expr_select(value: Any, *, as_: str) -> ExpressionSelectItem:
    """Project a computed expression, e.g.
    ``expr_select(col("oi.qty") * col("oi.price"), as_="line_total")``. An
    ``as_`` alias is required — a computed value has no default output name."""
    return ExpressionSelectItem(expr=_to_expression(value), alias=as_)


def _require_wrapped(value: Any) -> Any:
    """Function arguments and CASE results keep ``_to_arg``'s explicitness rule:
    a bare string there could plausibly be a column name OR a literal."""
    if isinstance(value, (Column, Literal, Expr, ExpressionModels)):
        return value
    raise TypeError(
        "expression-function arguments and CASE results must be wrapped explicitly "
        "as col(...) for a column or lit(...) for a literal, to avoid ambiguity; "
        f"got a bare {type(value).__name__}"
    )


def _scalar_call(name: ScalarFn, args: Sequence[Any]) -> ScalarFunctionCall:
    return ScalarFunctionCall(fn=name, args=[_to_arg(a) for a in args])


def fn(name: ScalarFn, *args: Any) -> FnColumn:
    """A scalar function applied to column(s)/literal(s), usable as a *predicate*
    target. ``lower``/``upper``/``trim`` take one ``col(...)``; ``coalesce``/
    ``concat`` take 2+ ``col(...)``/``lit(...)`` args. Example::

        fn("lower", col("customers.name")) == "ada"
    """
    return FnColumn(_scalar_call(name, args))


def col_fn(name: ScalarFn, *args: Any) -> FnColumn:
    """Alias of :func:`fn` — a scalar function used as a predicate target."""
    return fn(name, *args)


def fn_select(name: ScalarFn, *args: Any, as_: Optional[str] = None) -> ScalarFunctionSelectItem:
    """A scalar function as a SELECT projection, e.g.
    ``fn_select("coalesce", col("customers.name"), lit("unknown"), as_="display_name")``."""
    call = _scalar_call(name, args)
    return ScalarFunctionSelectItem(fn=call.fn, args=call.args, alias=as_)


# --------------------------------------------------------------------------- #
# Aggregate / date-bucket / ordered-set select items
# --------------------------------------------------------------------------- #
class _Agg:
    """Namespace of aggregate select-item factories, e.g. ``agg.sum(...)``."""

    @staticmethod
    def _make(
        function: AggregateFn,
        column: Union[str, Column, "Expr"],
        distinct: bool,
        as_: Optional[str],
    ) -> AggregateSelectItem:
        # A computed argument (item 100) goes to `arg`; a plain column keeps the
        # `col` sugar spelling, which the server normalizes to the same node.
        if isinstance(column, (Expr, *ExpressionModels)):
            return AggregateSelectItem(
                fn=function, arg=_to_expression(column), distinct=distinct, alias=as_
            )
        return AggregateSelectItem(fn=function, col=_colname(column), distinct=distinct, alias=as_)

    def count(
        self,
        column: Union[str, Column, "Expr"] = "*",
        *,
        distinct: bool = False,
        as_: Optional[str] = None,
    ) -> AggregateSelectItem:
        return self._make("count", column, distinct, as_)

    def sum(
        self,
        column: Union[str, Column, "Expr"],
        *,
        distinct: bool = False,
        as_: Optional[str] = None,
    ) -> AggregateSelectItem:
        return self._make("sum", column, distinct, as_)

    def avg(
        self,
        column: Union[str, Column, "Expr"],
        *,
        distinct: bool = False,
        as_: Optional[str] = None,
    ) -> AggregateSelectItem:
        return self._make("avg", column, distinct, as_)

    def min(
        self,
        column: Union[str, Column, "Expr"],
        *,
        distinct: bool = False,
        as_: Optional[str] = None,
    ) -> AggregateSelectItem:
        return self._make("min", column, distinct, as_)

    def max(
        self,
        column: Union[str, Column, "Expr"],
        *,
        distinct: bool = False,
        as_: Optional[str] = None,
    ) -> AggregateSelectItem:
        return self._make("max", column, distinct, as_)

    def stddev(
        self, column: Union[str, Column, "Expr"], *, as_: Optional[str] = None
    ) -> AggregateSelectItem:
        return self._make("stddev", column, False, as_)

    def variance(
        self, column: Union[str, Column, "Expr"], *, as_: Optional[str] = None
    ) -> AggregateSelectItem:
        return self._make("variance", column, False, as_)


agg = _Agg()


def date_bucket(
    column: Union[str, Column], granularity: DateGranularity, *, as_: Optional[str] = None
) -> DateBucketSelectItem:
    """A date-truncation projection, e.g. ``date_bucket("orders.created_at", "month")``."""
    return DateBucketSelectItem(col=_colname(column), granularity=granularity, alias=as_)


def string_agg(
    column: Union[str, Column], delimiter: str, *, as_: Optional[str] = None
) -> StringAggSelectItem:
    """Concatenate a column's grouped values into one delimited string."""
    return StringAggSelectItem(col=_colname(column), delimiter=delimiter, alias=as_)


def array_agg(column: Union[str, Column], *, as_: Optional[str] = None) -> ArrayAggSelectItem:
    """Collect a column's grouped values into an array (Postgres only server-side)."""
    return ArrayAggSelectItem(col=_colname(column), alias=as_)


def percentile_cont(
    column: Union[str, Column], fraction: float, *, as_: Optional[str] = None
) -> PercentileContSelectItem:
    """A continuous-interpolation percentile, e.g.
    ``percentile_cont("orders.total_amount", 0.5, as_="median")``."""
    return PercentileContSelectItem(col=_colname(column), fraction=fraction, alias=as_)


def when(condition: WhereNode, then: Any) -> CaseWhen:
    """One CASE branch: a condition and its result. The condition is a full
    ``WhereNode`` — a single predicate OR an ``and_(...)``/``or_(...)``/
    ``not_(...)`` group for a searched CASE (item 99). The result is any
    expression (item 100): ``col(...)``, ``lit(...)``, or something computed
    like ``col("oi.qty") * col("oi.price")``."""
    return CaseWhen(when=condition, then=_to_expression(_require_wrapped(then)))


def case(*whens: CaseWhen, else_: Any = None, as_: str) -> CaseSelectItem:
    """``CASE WHEN ... THEN ... [ELSE ...] END`` — an ``as_`` alias is required."""
    return CaseSelectItem(
        when=list(whens),
        else_=(_to_expression(_require_wrapped(else_)) if else_ is not None else None),
        alias=as_,
    )


# --------------------------------------------------------------------------- #
# Boolean groups
# --------------------------------------------------------------------------- #
def and_(*terms: WhereNode) -> WhereGroup:
    """Combine predicates/groups with AND."""
    return WhereGroup(and_terms=list(terms))


def or_(*terms: WhereNode) -> WhereGroup:
    """Combine predicates/groups with OR."""
    return WhereGroup(or_terms=list(terms))


def not_(term: WhereNode) -> WhereGroup:
    """Negate a single predicate or group: NOT term."""
    return WhereGroup(not_terms=term)


# --------------------------------------------------------------------------- #
# Ordering
# --------------------------------------------------------------------------- #
def asc(column: Union[str, Column], *, nulls: Optional[str] = None) -> OrderBySpec:
    return OrderBySpec(col=_colname(column), dir="asc", nulls=nulls)


def desc(column: Union[str, Column], *, nulls: Optional[str] = None) -> OrderBySpec:
    return OrderBySpec(col=_colname(column), dir="desc", nulls=nulls)


def _to_orderby(value: Union[str, Column, OrderBySpec]) -> OrderBySpec:
    if isinstance(value, OrderBySpec):
        return value
    return OrderBySpec(col=_colname(value), dir="asc")


# --------------------------------------------------------------------------- #
# Query builder
# --------------------------------------------------------------------------- #
class Query:
    """A fluent builder for a single ``StructuredQuery``.

    Every mutating method returns ``self`` for chaining. Terminal methods:
    :meth:`build` (a validated ``StructuredQuery``), :meth:`to_dict`, and
    :meth:`to_json` (the wire JSON to send).
    """

    def __init__(self, from_table: str, *, alias: Optional[str] = None) -> None:
        self._from_table = from_table
        self._from_alias = alias
        self._select: List[SelectItem] = []
        self._distinct = False
        self._joins: List[JoinSpec] = []
        self._where: List[WhereNode] = []
        self._group_by: List[str] = []
        self._having: List[WhereNode] = []
        self._order_by: List[OrderBySpec] = []
        self._limit: Optional[int] = None
        self._offset = 0
        self._top_n: Optional[TopNSpec] = None
        self._intent: Optional[str] = None

    @classmethod
    def from_(cls, table: str, *, alias: Optional[str] = None) -> "Query":
        """Start a query rooted at ``table`` (optionally aliased)."""
        return cls(table, alias=alias)

    def select(self, *items: Union[str, Column, SelectItem]) -> "Query":
        """Add one or more projections: bare ``"Table.Column"`` strings, ``col(...)``,
        or select-item helpers (``agg.*``, ``date_bucket``, ``case`` …)."""
        for item in items:
            self._select.append(_to_select_item(item))
        return self

    def distinct(self, value: bool = True) -> "Query":
        """SELECT DISTINCT across the whole select list."""
        self._distinct = value
        return self

    def join(
        self,
        table: str,
        on: Tuple[Union[str, Column], Union[str, Column]],
        *,
        type: JoinType = "inner",
        alias: Optional[str] = None,
        extra_on: Optional[Sequence[Tuple[Union[str, Column], Union[str, Column]]]] = None,
        connection: Optional[str] = None,
    ) -> "Query":
        """Join ``table`` on an equality pair ``(left, right)``. ``extra_on`` adds
        further ANDed pairs for composite keys; ``connection`` marks a
        cross-connection join (same join_group only)."""
        left, right = on
        self._joins.append(
            JoinSpec(
                table=table,
                alias=alias,
                type=type,
                on=[_colname(left), _colname(right)],
                extra_on=[[_colname(a), _colname(b)] for a, b in (extra_on or [])],
                connection=connection,
            )
        )
        return self

    def where(self, *nodes: WhereNode) -> "Query":
        """Add predicate(s)/group(s). Multiple nodes — across one call or several
        ``.where()`` calls — are combined with AND."""
        self._where.extend(nodes)
        return self

    def group_by(self, *columns: Union[str, Column]) -> "Query":
        self._group_by.extend(_colname(c) for c in columns)
        return self

    def having(self, *nodes: WhereNode) -> "Query":
        """Add post-aggregation predicate(s)/group(s). Multiple nodes — across
        one call or several ``.having()`` calls — are AND-combined, exactly like
        ``.where()``. Pass ``or_(...)``/``and_(...)``/``not_(...)`` for boolean
        logic over aggregate conditions (item 99)."""
        self._having.extend(nodes)
        return self

    def order_by(
        self, column: Union[str, Column], *, desc: bool = False, nulls: Optional[str] = None
    ) -> "Query":
        self._order_by.append(
            OrderBySpec(col=_colname(column), dir="desc" if desc else "asc", nulls=nulls)
        )
        return self

    def limit(self, n: int) -> "Query":
        self._limit = n
        return self

    def offset(self, n: int) -> "Query":
        self._offset = n
        return self

    def top_n(
        self,
        n: int,
        *,
        order_by: Sequence[Union[str, Column, OrderBySpec]],
        partition_by: Optional[Sequence[Union[str, Column]]] = None,
        fn: RankFn = "row_number",
    ) -> "Query":
        """Keep the top ``n`` rows per partition (an overall top-N if
        ``partition_by`` is empty)."""
        self._top_n = TopNSpec(
            partition_by=[_colname(c) for c in (partition_by or [])],
            order_by=[_to_orderby(o) for o in order_by],
            n=n,
            fn=fn,
        )
        return self

    def intent(self, text: str) -> "Query":
        """Attach a natural-language intent (logged with the compiled SQL for
        audit/debugging; never returned to the caller)."""
        self._intent = text
        return self

    # Terminals ----------------------------------------------------------- #
    @staticmethod
    def _and_combine(nodes: List[WhereNode]) -> Optional[WhereNode]:
        if not nodes:
            return None
        if len(nodes) == 1:
            return nodes[0]
        return WhereGroup(and_terms=list(nodes))

    def _where_node(self) -> Optional[WhereNode]:
        return self._and_combine(self._where)

    def build(self) -> StructuredQuery:
        """Construct and validate the ``StructuredQuery``. Raises the same
        ``pydantic.ValidationError`` the server would on an illegal shape."""
        return StructuredQuery(
            from_table=self._from_table,
            from_alias=self._from_alias,
            select=self._select,
            distinct=self._distinct,
            joins=self._joins,
            where=self._where_node(),
            group_by=self._group_by,
            having=self._and_combine(self._having),
            order_by=self._order_by,
            limit=self._limit,
            offset=self._offset,
            top_n=self._top_n,
            intent=self._intent,
        )

    def to_dict(self) -> dict:
        """The minimal wire JSON dict to send as a REST/MCP query body."""
        return self.build().model_dump(by_alias=True, exclude_none=True, exclude_defaults=True)

    def to_json(self, *, indent: Optional[int] = None) -> str:
        """The wire JSON as a string."""
        return self.build().model_dump_json(
            by_alias=True, exclude_none=True, exclude_defaults=True, indent=indent
        )


def _to_select_item(item: Union[str, Column, SelectItem]) -> SelectItem:
    if isinstance(item, Column):
        return item.name
    if isinstance(item, FnColumn):
        raise TypeError(
            "fn()/col_fn() build a scalar function as a *predicate* target — for a "
            "scalar function in select(), use fn_select(...) which takes an as_ alias"
        )
    if isinstance(item, Predicate):
        raise TypeError("a predicate belongs in where()/having(), not select()")
    return item
