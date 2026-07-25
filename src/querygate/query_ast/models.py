"""Pydantic AST for read-only structured queries.

This is the ONLY shape an agent can submit — there is no raw-SQL entry point
anywhere in QueryGate. Every field here is later checked against the live
reflected schema (validation/schema_validation.py) and the active policy
(validation/policy_validation.py) before it is ever compiled to SQL.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Set, Union

import pydantic as pyd

CompareOp = Literal[
    "eq",
    "neq",
    "lt",
    "lte",
    "gt",
    "gte",
    "in",
    "not_in",
    "like",
    "between",
    "is_null",
    "is_not_null",
]
AggregateFn = Literal["count", "sum", "avg", "min", "max", "stddev", "variance"]
_NO_DISTINCT_AGG_FNS = frozenset({"stddev", "variance"})
JoinType = Literal["inner", "left"]
SortDir = Literal["asc", "desc"]
RankFn = Literal["row_number", "rank", "dense_rank"]
DateGranularity = Literal["day", "week", "month", "quarter", "year"]


class AggregateSelectItem(pyd.BaseModel):
    """Aggregate projection, e.g. COUNT(*) AS order_count, or an aggregate over
    a computed expression: SUM(quantity * unit_price), or the conditional
    aggregation SUM(CASE WHEN status='paid' THEN amount ELSE 0 END).
    """

    # Two spellings reach this model and exactly ONE reaches everything
    # downstream. `col: "Table.Column"` is kept permanently as sugar (2026-07-25
    # Decision Log): the `mode="before"` validator normalizes it to
    # `arg=ColumnExpr(col=...)`, so the compiler, the caps, and the item-96
    # canonical visitor only ever see `arg`. `col: "*"` is not an expression at
    # all — it is COUNT(*)'s star form and stays on `col`.

    fn: AggregateFn
    col: Optional[str] = pyd.Field(
        default=None,
        description=(
            "Sugar for arg={'col': ...}: a Table.Col ref, or '*' for count(*). "
            "Mutually exclusive with arg; `arg` is the canonical form."
        ),
    )
    arg: Optional["Expression"] = pyd.Field(
        default=None,
        description=(
            "The scalar expression to aggregate — a column, arithmetic, a nested "
            "function, or a CASE. Mutually exclusive with col."
        ),
    )
    distinct: bool = pyd.Field(
        default=False,
        description="Aggregate only over distinct values, e.g. COUNT(DISTINCT col).",
    )
    alias: Optional[str] = pyd.Field(
        default=None,
        validation_alias=pyd.AliasChoices("as", "alias"),
        serialization_alias="as",
    )

    model_config = pyd.ConfigDict(populate_by_name=True, extra="forbid")

    @pyd.model_validator(mode="before")
    @classmethod
    def _normalize_col_sugar(cls, data: Any) -> Any:
        """Rewrite the `col` spelling into the canonical `arg` before any other
        validation runs, so exactly one shape exists from here on."""
        if not isinstance(data, dict):
            return data
        col = data.get("col")
        if not isinstance(col, str) or col == "*":
            return data
        if data.get("arg") is not None:
            raise ValueError("Set exactly one of 'col' or 'arg' on an aggregate, not both")
        normalized = dict(data)
        normalized.pop("col")
        normalized["arg"] = ColumnExpr(col=col)
        return normalized

    @pyd.model_validator(mode="after")
    def _validate_shape(self) -> "AggregateSelectItem":
        if (self.col is None) == (self.arg is None):
            raise ValueError("Aggregate must set exactly one of 'col' or 'arg'")
        if self.col is not None and self.col != "*":  # pragma: no cover - normalized above
            raise ValueError("Aggregate 'col' must be a Table.Column ref or '*'")
        if self.distinct and self.col == "*":
            raise ValueError("distinct is not valid with count(*) — give a real column")
        if self.distinct and self.fn in _NO_DISTINCT_AGG_FNS:
            raise ValueError(
                f"distinct is not valid with {self.fn} — MSSQL's STDEV/VAR don't accept "
                "DISTINCT, so this stays rejected on every dialect rather than working on "
                "Postgres and breaking on MSSQL"
            )
        if self.alias is None and self.arg is not None and not isinstance(self.arg, ColumnExpr):
            # A bare column aggregate has an obvious default name (sum_amount);
            # SUM(quantity * unit_price) does not, and inventing one would
            # silently collide between two computed aggregates. Same reasoning
            # as CaseSelectItem/ExpressionSelectItem requiring an alias.
            raise ValueError(
                "an aggregate over a computed expression requires an explicit 'as' alias"
            )
        return self


class DateBucketSelectItem(pyd.BaseModel):
    """Date-truncation projection, e.g. month bucket of Order.CreatedAt."""

    col: str = pyd.Field(description="Datetime column ref (Table.Col)")
    granularity: DateGranularity
    alias: Optional[str] = pyd.Field(
        default=None,
        validation_alias=pyd.AliasChoices("as", "alias"),
        serialization_alias="as",
    )

    model_config = pyd.ConfigDict(populate_by_name=True, extra="forbid")


class StringAggSelectItem(pyd.BaseModel):
    """GROUP BY aggregate concatenating col's grouped values into one
    delimiter-separated string, e.g. STRING_AGG(Customer.Email, ', ').
    Its own shape (not a distinct field on AggregateSelectItem) since it
    needs a delimiter that {fn, col} has no room for — see
    DateBucketSelectItem/ScalarFunctionSelectItem for the same "sibling
    type" pattern. Deliberately no ORDER BY-within-the-call (real on
    Postgres, absent from MSSQL 2017+'s STRING_AGG) and no `distinct`
    (Postgres supports it, T-SQL's STRING_AGG does not) — a v1 bound, not
    an oversight (TODO.md item 80).
    """

    col: str = pyd.Field(description="Column ref (Table.Col) to concatenate.")
    delimiter: str = pyd.Field(description="Separator string between concatenated values.")
    alias: Optional[str] = pyd.Field(
        default=None,
        validation_alias=pyd.AliasChoices("as", "alias"),
        serialization_alias="as",
    )

    model_config = pyd.ConfigDict(populate_by_name=True, extra="forbid")

    @pyd.model_validator(mode="after")
    def _validate_col(self) -> "StringAggSelectItem":
        if self.col == "*":
            raise ValueError("string_agg requires a real column, not '*'")
        return self


class ArrayAggSelectItem(pyd.BaseModel):
    """GROUP BY aggregate collecting col's grouped values into a real array,
    e.g. ARRAY_AGG(OrderItem.Sku). Its own sibling shape (not a field on
    AggregateSelectItem), matching StringAggSelectItem/DateBucketSelectItem
    — but unlike string_agg there's no delimiter, since an array result
    doesn't need one. Deliberately no ORDER BY-within-the-call and no
    `distinct`, same v1 bound string_agg used (TODO.md item 81).
    """

    col: str = pyd.Field(description="Column ref (Table.Col) to collect.")
    alias: Optional[str] = pyd.Field(
        default=None,
        validation_alias=pyd.AliasChoices("as", "alias"),
        serialization_alias="as",
    )

    model_config = pyd.ConfigDict(populate_by_name=True, extra="forbid")

    @pyd.model_validator(mode="after")
    def _validate_col(self) -> "ArrayAggSelectItem":
        if self.col == "*":
            raise ValueError("array_agg requires a real column, not '*'")
        return self


class PercentileContSelectItem(pyd.BaseModel):
    """GROUP BY ordered-set aggregate computing a continuous-interpolation
    percentile of col's grouped values, e.g.
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY Order.TotalAmount) for the
    median. Its own sibling shape (not a field on AggregateSelectItem)
    since WITHIN GROUP (ORDER BY ...) is a structurally different aggregate
    form entirely, not an {fn, col} variant. Deliberately single-column/
    single-fraction/always-ascending for v1 — no descending option, no
    Postgres's multi-fraction array form, no PARTITION BY — same
    "separate future item, not an oversight" reasoning as string_agg/
    array_agg's deferred features (TODO.md item 82).
    """

    col: str = pyd.Field(description="Column ref (Table.Col) to compute the percentile of.")
    fraction: float = pyd.Field(
        description="Percentile as a fraction in [0.0, 1.0], e.g. 0.5 for the median."
    )
    alias: Optional[str] = pyd.Field(
        default=None,
        validation_alias=pyd.AliasChoices("as", "alias"),
        serialization_alias="as",
    )

    model_config = pyd.ConfigDict(populate_by_name=True, extra="forbid")

    @pyd.model_validator(mode="after")
    def _validate(self) -> "PercentileContSelectItem":
        if self.col == "*":
            raise ValueError("percentile_cont requires a real column, not '*'")
        if not 0.0 <= self.fraction <= 1.0:
            raise ValueError("percentile_cont fraction must be between 0.0 and 1.0")
        return self


def _require_column_ref(value: str, field: str) -> str:
    """Every column leaf must be a dotted Table.Column (or Alias.Column) ref —
    a column leaf can never reference a bare select alias, unlike a HAVING
    predicate's `col`. Enforced here at the AST layer so the failure is a clean
    pre-DB validation error rather than one raised later by `parse_column_ref`.
    """
    table, _, column = value.partition(".")
    if not table.strip() or not column.strip():
        raise ValueError(f"{field} must be a 'Table.Column' reference, got {value!r}")
    return value


# Also the column-typed argument of a `ScalarFunctionCall` and of a CASE
# `then`/`else` — those were once a separate `ColArg` class with the identical
# {"col": ...} wire shape, and collapsing them means the enforcement layer sees
# exactly ONE column-leaf type instead of two kept in lockstep by hand.
class ColumnExpr(pyd.BaseModel):
    """A column reference."""

    col: str = pyd.Field(description="Table.Column (or Alias.Column) ref.")

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.field_validator("col")
    @classmethod
    def _dotted(cls, value: str) -> str:
        return _require_column_ref(value, "col")


# Typed (string/number/bool/null) rather than `Any`: the value binds as a
# parameter, never interpolated, and an unmodelled shape (a dict, a list) is
# rejected at the AST layer instead of failing opaquely at bind time.
class LiteralExpr(pyd.BaseModel):
    """A literal scalar value."""

    literal: Optional[Union[bool, int, float, str]] = pyd.Field(
        description="A literal scalar value (string/number/bool/null)."
    )

    model_config = pyd.ConfigDict(extra="forbid")


# Backwards-compatible names for the two leaves above, kept because they read
# better at a scalar-function call site (an "argument", not an "expression").
# They are the SAME classes, not siblings — see ColumnExpr's docstring.
ColArg = ColumnExpr
LiteralArg = LiteralExpr

ScalarFunctionArg = Union[ColumnExpr, LiteralExpr]

ScalarFn = Literal["coalesce", "lower", "upper", "trim", "concat"]


class ScalarFunctionCall(pyd.BaseModel):
    """A whitelisted scalar function call: fn + args. lower/upper/trim take
    exactly one column argument ({"col": "Table.Column"}); coalesce/concat
    take 2+ arguments, each either {"col": ...} or {"literal": ...}.
    **No nesting** — args are always ColArg/LiteralArg, never another
    ScalarFunctionCall — a deliberate bound keeping this from becoming an
    open-ended expression grammar. Used both as a SELECT projection
    (`ScalarFunctionSelectItem`, which adds an alias) and as a WHERE/HAVING
    predicate target (`Predicate.col_fn`, which has none).
    """

    fn: ScalarFn
    args: List[ScalarFunctionArg] = pyd.Field(min_length=1)

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _validate_args(self) -> "ScalarFunctionCall":
        if self.fn in ("lower", "upper", "trim"):
            if len(self.args) != 1 or not isinstance(self.args[0], ColArg):
                raise ValueError(f"{self.fn} takes exactly one column argument")
        elif len(self.args) < 2:
            raise ValueError(f"{self.fn} requires at least 2 arguments")
        return self


class ScalarFunctionSelectItem(ScalarFunctionCall):
    """SELECT-projection use of a `ScalarFunctionCall` — see that class for
    the fn/args shape.
    """

    alias: Optional[str] = pyd.Field(
        default=None,
        validation_alias=pyd.AliasChoices("as", "alias"),
        serialization_alias="as",
    )

    model_config = pyd.ConfigDict(populate_by_name=True, extra="forbid")


# --------------------------------------------------------------------------
# The bounded scalar Expression substrate (TODO.md item 100).
#
# One CLOSED, depth-capped recursive union used everywhere a scalar value is
# expected. This is deliberately NOT the "open-ended expression grammar"
# non-goal #7 forbids, and the boundary is recorded in docs/PRODUCT_GUIDE.md's
# Decision Log (2026-07-25):
#   1. the operator set is fixed and finite (+ - * /) — not a parser;
#   2. `FunctionExpr.fn` is an enum, never a free string — no UDFs, no procs;
#   3. nesting is capped (Policy.max_expression_depth / max_expression_nodes,
#      the latter summed tree-wide exactly as item 97 requires);
#   4. every leaf is still a typed identifier or literal — `ColumnExpr.col` is
#      resolved and policy-checked like any other ref, `LiteralExpr` binds as a
#      parameter, so no string is ever interpolated into SQL (non-goal #1);
#   5. the item-96 canonical visitor recurses through every node, so a column
#      buried in a BinaryOpExpr cannot bypass allow/deny or masking.
# --------------------------------------------------------------------------

BinaryOp = Literal["+", "-", "*", "/"]

ExprFn = Literal[
    "coalesce",
    "lower",
    "upper",
    "trim",
    "concat",
    "abs",
    "ceil",
    "floor",
    "round",
    "length",
    "nullif",
    "replace",
    "substring",
]

# (min_args, max_args) per function — max None means variadic. Arity is checked
# at the AST layer so it is dialect-independent and pre-DB. Where dialects
# disagree on an OPTIONAL argument the stricter arity wins (`substring` is
# exactly 3 because T-SQL's SUBSTRING has no two-argument form, so a 2-arg call
# would render fine on Postgres and break live on MSSQL — items 75/82's trap).
_EXPR_FN_ARITY: Dict[str, tuple] = {
    "coalesce": (2, None),
    "concat": (2, None),
    "lower": (1, 1),
    "upper": (1, 1),
    "trim": (1, 1),
    "abs": (1, 1),
    "ceil": (1, 1),
    "floor": (1, 1),
    "length": (1, 1),
    "round": (1, 2),
    "nullif": (2, 2),
    "replace": (3, 3),
    "substring": (3, 3),
}

CastType = Literal["text", "integer", "numeric", "boolean", "date", "timestamp"]


# The operator set is fixed and finite — not an operator table a caller can
# extend. Division renders GUARDED (`left / NULLIF(right, 0)`) on every dialect;
# see the 2026-07-25 Decision Log for why that beats inheriting Postgres's hard
# error and MSSQL's `ARITHABORT`-dependent behavior.
class BinaryOpExpr(pyd.BaseModel):
    """Arithmetic (+ - * /) over two expressions. Division by zero yields NULL."""

    op: BinaryOp
    left: "Expression"
    right: "Expression"

    model_config = pyd.ConfigDict(extra="forbid")


# `fn` is an ENUM, never a free string: a caller cannot name a UDF, a stored
# procedure, or any function not listed. Adding one is a code change with a
# dialect decision, not caller input (non-goal #7 boundary 2).
class FunctionExpr(pyd.BaseModel):
    """A whitelisted scalar function whose arguments may nest, e.g. lower(trim(x))."""

    fn: ExprFn
    args: List["Expression"] = pyd.Field(min_length=1)

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _validate_arity(self) -> "FunctionExpr":
        low, high = _EXPR_FN_ARITY[self.fn]
        count = len(self.args)
        if count < low or (high is not None and count > high):
            expected = (
                f"exactly {low}"
                if low == high
                else (f"at least {low}" if high is None else f"{low}-{high}")
            )
            raise ValueError(f"{self.fn} takes {expected} argument(s), got {count}")
        return self


# Its own union member rather than a `FunctionExpr` because a cast target is a
# TYPE name, not an expression — folding it into `args` would have meant an
# `Any`-shaped argument, exactly the escape hatch this substrate forbids.
class CastExpr(pyd.BaseModel):
    """CAST(expr AS type) over a closed set of target types."""

    cast: "Expression"
    to: CastType

    model_config = pyd.ConfigDict(extra="forbid")


class CaseWhen(pyd.BaseModel):
    """One CASE WHEN branch: a `when` condition and the value to project when
    it's true. `when` is a full `WhereNode` (a single Predicate OR a boolean
    `and`/`or`/`not` group — a *searched* CASE, `CASE WHEN a > 0 AND b < 5
    THEN ...`), reusing the exact same predicate machinery, visitor, and caps
    as `where`/`having` (TODO.md item 99). Bounded by `max_where_depth` /
    `max_where_predicates` / `max_case_branches` like any other predicate tree.

    `then` is a full `Expression` (item 100), widened from the old
    column-or-literal argument shape — a wire-compatible superset.
    """

    when: WhereNode
    then: "Expression"

    model_config = pyd.ConfigDict(extra="forbid")


class CaseExpr(pyd.BaseModel):
    """Searched CASE usable anywhere a scalar is expected — inside an aggregate
    argument, arithmetic, or another function. This is what makes conditional
    aggregation expressible."""

    when: List[CaseWhen] = pyd.Field(min_length=1)
    else_: Optional["Expression"] = pyd.Field(
        default=None,
        validation_alias=pyd.AliasChoices("else", "else_"),
        serialization_alias="else",
    )

    model_config = pyd.ConfigDict(populate_by_name=True, extra="forbid")


# The union is CLOSED: adding a member is a deliberate code change, reviewed
# against the five boundaries above. Members are structurally distinguishable
# by their required field names (col / literal / op / fn / cast / when) and
# every one forbids extras, so Pydantic resolves the union unambiguously —
# the same approach `ScalarFunctionArg` already uses.
Expression = Union[
    ColumnExpr,
    LiteralExpr,
    BinaryOpExpr,
    FunctionExpr,
    CastExpr,
    CaseExpr,
]


class CaseSelectItem(pyd.BaseModel):
    """CASE WHEN ... THEN ... [ELSE ...] END, evaluated top-to-bottom —
    the first matching `when` wins. `alias` is REQUIRED (unlike aggregates/
    date_bucket) since there's no sensible default output name for a
    conditional expression.

    Since item 100 this is exactly a `CaseExpr` plus a required alias — it is
    the top-level-projection SPELLING of that expression node, not a second
    structural shape. `as_expression()` is the single conversion every walker,
    cap, and compile path goes through, so CASE logic is implemented once.
    """

    when: List[CaseWhen] = pyd.Field(min_length=1)
    else_: Optional[Expression] = pyd.Field(
        default=None,
        validation_alias=pyd.AliasChoices("else", "else_"),
        serialization_alias="else",
    )
    alias: str = pyd.Field(
        validation_alias=pyd.AliasChoices("as", "alias"),
        serialization_alias="as",
    )

    model_config = pyd.ConfigDict(populate_by_name=True, extra="forbid")

    def as_expression(self) -> CaseExpr:
        """This item's equivalent `CaseExpr`. `model_construct` skips
        re-validation: the branches/else were already validated on this
        instance, and re-running validation here would re-walk the whole
        subtree every time a visitor or cap check touched the item."""
        return CaseExpr.model_construct(when=self.when, else_=self.else_)


# `alias` is REQUIRED for the same reason CaseSelectItem's is: a computed value
# has no sensible default output name, and inventing one would silently collide
# across two items.
class ExpressionSelectItem(pyd.BaseModel):
    """Project a computed scalar expression, e.g. quantity * unit_price. GROUP BY
    may reference this item's alias, which is how a computed group key (such as a
    CASE bucket) is expressed."""

    expr: Expression
    alias: str = pyd.Field(
        validation_alias=pyd.AliasChoices("as", "alias"),
        serialization_alias="as",
    )

    model_config = pyd.ConfigDict(populate_by_name=True, extra="forbid")


SelectItem = Union[
    str,
    AggregateSelectItem,
    DateBucketSelectItem,
    StringAggSelectItem,
    ArrayAggSelectItem,
    PercentileContSelectItem,
    ScalarFunctionSelectItem,
    CaseSelectItem,
    ExpressionSelectItem,
]

# Select item types that make a query an aggregate query for is_aggregate/
# has_aggregate purposes (clamp_limit's aggregate cap, top_n eligibility,
# the having-without-group_by rule) — shared by sqlalchemy_compiler.py and
# schema_validation.py's two has_aggregate checks so a fourth aggregate
# type never has to be added to three places by hand (TODO.md item 81).
_AGGREGATE_SELECT_ITEM_TYPES = (
    AggregateSelectItem,
    StringAggSelectItem,
    ArrayAggSelectItem,
    PercentileContSelectItem,
)


class JoinSpec(pyd.BaseModel):
    table: str
    alias: Optional[str] = pyd.Field(
        default=None,
        description=(
            "Optional name this occurrence of `table` is referred to by everywhere else "
            "in the query (on/select/where/group_by/having/order_by/top_n use "
            "Alias.Column instead of Table.Column once set). REQUIRED when the same "
            "physical table appears more than once in one query (a self-join) — every "
            "occurrence of a repeated table must carry its own alias, e.g. joining "
            "Employee to itself as `m` to look up each row's manager."
        ),
    )
    type: JoinType = "inner"
    on: List[str] = pyd.Field(
        min_length=2,
        max_length=2,
        description="Equality join: [LeftTable.Col, RightTable.Col]",
    )
    extra_on: List[List[str]] = pyd.Field(
        default_factory=list,
        description=(
            "Additional equality pairs ANDed with `on`, for composite/multi-column join "
            "keys — each entry is a two-element [LeftTable.Col, RightTable.Col] pair "
            "referencing the SAME two tables/aliases as `on` (a join's condition is "
            "always about the one pair of tables it joins, never a third)."
        ),
    )
    connection: Optional[str] = pyd.Field(
        default=None,
        description=(
            "Only set when this join's table lives in a DIFFERENT connection than the "
            "query's primary connection — e.g. a same-instance cross-database join. Omit "
            "for joins within the primary connection. Only permitted when both connections "
            "share the same policy-declared join_group; see list_connections."
        ),
    )

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _validate_extra_on_pairs(self) -> "JoinSpec":
        for pair in self.extra_on:
            if len(pair) != 2:
                raise ValueError(
                    "Each extra_on entry must be a two-element [LeftTable.Col, "
                    f"RightTable.Col] pair, got {pair!r}"
                )
        return self


class Predicate(pyd.BaseModel):
    col: Optional[str] = pyd.Field(
        default=None,
        description="Table.Column being compared. Exactly one of col/col_fn/expr is required.",
    )
    expr: Optional["Expression"] = pyd.Field(
        default=None,
        description=(
            "Compare a computed scalar expression instead of a bare column, e.g. "
            "OrderItem.Qty * OrderItem.Price > 100. The general form of col_fn. "
            "Mutually exclusive with col/col_fn."
        ),
    )
    col_fn: Optional[ScalarFunctionCall] = pyd.Field(
        default=None,
        description=(
            "A whitelisted scalar function applied to a column instead of a bare "
            "Table.Column, e.g. {fn: lower, args: [{col: Customer.Name}]} for "
            "lower(Customer.Name) = ... — same fn/args shape as a select item's scalar "
            "function. Mutually exclusive with col. No function nesting (see "
            "ScalarFunctionCall)."
        ),
    )
    op: CompareOp
    value: Optional[Any] = pyd.Field(
        default=None,
        description=(
            "A literal to compare col against. Exactly one of value/value_col is required "
            "for every op except is_null/is_not_null (omit both for those two). "
            "between: two-element [low, high] list. in/not_in: non-empty list. "
            "Everything else: a single scalar. Mutually exclusive with value_col."
        ),
    )
    value_col: Optional[str] = pyd.Field(
        default=None,
        description=(
            "Compare col against another Table.Column instead of a literal, e.g. "
            "OrderItem.Price > OrderItem.Cost. Only valid for eq/neq/lt/lte/gt/gte — "
            "in/not_in/between/like need a literal list/pattern, not a column. "
            "Mutually exclusive with value."
        ),
    )
    value_expr: Optional["Expression"] = pyd.Field(
        default=None,
        description=(
            "Compare against a computed scalar expression, e.g. "
            "OrderItem.Price > OrderItem.Cost * 1.2. Only valid for eq/neq/lt/lte/"
            "gt/gte. Mutually exclusive with the other value sources."
        ),
    )
    value_subquery: Optional["StructuredQuery"] = pyd.Field(
        default=None,
        description=(
            "For in/not_in only: compare col against the value set produced by a "
            "nested StructuredQuery (an uncorrelated `IN (subquery)`, TODO.md item 97) "
            "instead of a literal list. The subquery is itself a fully validated AST — "
            "never raw SQL — must select exactly one column, must resolve entirely "
            "against its own from/join tables (uncorrelated), and must stay on the same "
            "connection. Mutually exclusive with value/value_col. All policy caps apply "
            "summed across the whole query tree; nesting is bounded by "
            "Policy.max_subquery_depth."
        ),
    )

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _validate_col_shape(self) -> "Predicate":
        targets = [self.col is not None, self.col_fn is not None, self.expr is not None]
        if sum(targets) != 1:
            raise ValueError("Predicate must set exactly one of 'col', 'col_fn', or 'expr'")
        return self

    @pyd.model_validator(mode="after")
    def _validate_value_shape(self) -> "Predicate":
        if self.op in ("is_null", "is_not_null"):
            if (
                self.value is not None
                or self.value_col is not None
                or self.value_expr is not None
                or self.value_subquery is not None
            ):
                raise ValueError(f"Operator {self.op!r} must not include a value or value_col")
            return self
        # value_subquery and value_expr are further mutually-exclusive value
        # sources; value_subquery is valid only for in/not_in (a set-membership
        # test), value_expr only for the scalar comparisons value_col allows.
        sources = [
            self.value is not None,
            self.value_col is not None,
            self.value_expr is not None,
            self.value_subquery is not None,
        ]
        if sum(sources) > 1:
            raise ValueError(
                "Predicate must set at most one of 'value', 'value_col', 'value_expr', "
                "or 'value_subquery'"
            )
        if not any(sources):
            raise ValueError(
                f"Operator {self.op!r} requires a value, value_col, value_expr, or value_subquery"
            )
        if self.value_expr is not None and self.op not in ("eq", "neq", "lt", "lte", "gt", "gte"):
            raise ValueError(f"value_expr is not valid with operator {self.op!r}")
        if self.value_subquery is not None:
            if self.op not in ("in", "not_in"):
                raise ValueError(
                    f"value_subquery (IN (subquery)) is only valid with in/not_in, not {self.op!r}"
                )
            if len(self.value_subquery.select) != 1:
                raise ValueError("An IN (subquery) must select exactly one column (the value set)")
            return self
        if self.value_col is not None and self.op not in ("eq", "neq", "lt", "lte", "gt", "gte"):
            raise ValueError(f"value_col is not valid with operator {self.op!r}")
        if self.op == "between":
            if not isinstance(self.value, (list, tuple)) or len(self.value) != 2:
                raise ValueError("Operator 'between' requires a two-element list [low, high]")
        if self.op in ("in", "not_in"):
            if not isinstance(self.value, (list, tuple)) or len(self.value) == 0:
                raise ValueError(f"Operator {self.op!r} requires a non-empty list")
        return self


class WhereGroup(pyd.BaseModel):
    """Boolean group: set exactly one of `and`/`or`/`not`. `and`/`or` take a list
    of terms (each a Predicate or nested WhereGroup) — nest freely to express
    arbitrary boolean logic. `not` takes a SINGLE term (a Predicate or a nested
    WhereGroup) to negate, e.g. {"not": {"and": [...]}} for NOT (A AND B).
    """

    and_terms: Optional[List["WhereNode"]] = pyd.Field(
        default=None,
        validation_alias=pyd.AliasChoices("and", "and_terms"),
        serialization_alias="and",
    )
    or_terms: Optional[List["WhereNode"]] = pyd.Field(
        default=None,
        validation_alias=pyd.AliasChoices("or", "or_terms"),
        serialization_alias="or",
    )
    not_terms: Optional["WhereNode"] = pyd.Field(
        default=None,
        validation_alias=pyd.AliasChoices("not", "not_terms"),
        serialization_alias="not",
    )

    model_config = pyd.ConfigDict(populate_by_name=True, extra="forbid")

    @pyd.model_validator(mode="after")
    def _exactly_one_boolean(self) -> "WhereGroup":
        set_count = sum([bool(self.and_terms), bool(self.or_terms), self.not_terms is not None])
        if set_count != 1:
            raise ValueError("Where group must have exactly one of 'and', 'or', or 'not'")
        return self


WhereNode = Union[Predicate, WhereGroup]
# NOTE: model_rebuild for WhereGroup/Predicate/StructuredQuery is deferred to the
# end of the module — Predicate now references StructuredQuery (value_subquery,
# item 97), which isn't defined until below, so the whole recursive cycle
# (StructuredQuery → WhereNode → Predicate → StructuredQuery) can only be resolved
# once every model in it exists.


class OrderBySpec(pyd.BaseModel):
    """Sort key. dir must be the exact string "asc" or "desc" — other spellings
    (e.g. "direction", "sort", a boolean desc flag) are rejected, not silently
    defaulted to ascending.
    """

    col: str
    dir: SortDir = "asc"
    nulls: Optional[Literal["first", "last"]] = pyd.Field(
        default=None,
        description=(
            "Where NULLs sort relative to non-NULL values — omit for each dialect's "
            "own default ordering. Applied identically across dialects even though "
            "MSSQL has no native NULLS FIRST/LAST syntax (emulated internally)."
        ),
    )

    model_config = pyd.ConfigDict(extra="forbid")


class TopNSpec(pyd.BaseModel):
    """Rank rows within partitions and keep the top n per partition.

    partition_by may be empty — this ranks across all rows/groups as one
    partition (i.e. an overall top-N, not a per-group top-N).

    Without group_by/aggregates: partition_by/order_by may reference any
    Table.Col in the query graph, or a date_bucket select alias.
    WITH group_by/aggregates: ranking runs over the grouped result (one row
    per group), so partition_by/order_by must reference a group_by column or
    a select alias (aggregate or date_bucket) — not a raw table column.
    """

    partition_by: List[str] = pyd.Field(default_factory=list)
    order_by: List[OrderBySpec] = pyd.Field(min_length=1)
    n: int = pyd.Field(ge=1)
    fn: RankFn = "row_number"

    model_config = pyd.ConfigDict(extra="forbid")


class StructuredQuery(pyd.BaseModel):
    """Read-only structured query. No raw SQL — every field is a validated,
    schema-checked identifier or literal. Every column reference anywhere in
    this AST (select strings, where/having col, group_by, order_by, joins.on,
    top_n.partition_by) must be "Table.Column" (e.g. "Customer.Name") — or
    "Alias.Column" once from_alias/JoinSpec.alias is set for that table — or
    a select item's own alias where noted below — never a bare column name.
    """

    from_table: str = pyd.Field(
        validation_alias=pyd.AliasChoices("from", "from_table"),
        serialization_alias="from",
        description='Root table name (not Table.Column) — e.g. "Customer".',
    )
    from_alias: Optional[str] = pyd.Field(
        default=None,
        description=(
            "Optional name the root table is referred to by everywhere else in the "
            "query, in place of from_table — same rule as JoinSpec.alias, including "
            "being required if from_table is also used as a join table (self-join)."
        ),
    )
    select: List[SelectItem] = pyd.Field(
        min_length=1,
        description=(
            'Each item is EITHER a bare "Table.Column" string, OR an object: '
            "{fn, col, as} for an aggregate, or {col, granularity, as} for a date_bucket."
        ),
    )
    distinct: bool = pyd.Field(
        default=False,
        description="De-duplicate result rows (SELECT DISTINCT) across the full select list.",
    )
    joins: List[JoinSpec] = pyd.Field(default_factory=list)
    where: Optional[WhereNode] = pyd.Field(
        default=None,
        description=(
            "A single Predicate ({col, op, value}), or a WhereGroup for boolean "
            "nesting — see each type's own fields for its exact shape."
        ),
    )
    group_by: List[str] = pyd.Field(
        default_factory=list,
        description="Table.Column refs, or a date_bucket select item's alias.",
    )
    having: Optional[WhereNode] = pyd.Field(
        default=None,
        description=(
            "Filter on grouped/aggregated results — the same shape as `where`: a single "
            "Predicate ({col, op, value}) or a WhereGroup for boolean nesting "
            "(and/or/not), so OR-logic over aggregate conditions "
            "(HAVING SUM(x) > 10 OR COUNT(*) < 3) is expressible. A `having` "
            "Predicate may reference a select item's alias (e.g. an aggregate's `as`) "
            "in its `col`, unlike `where`. Requires group_by or aggregate select items."
        ),
    )
    order_by: List[OrderBySpec] = pyd.Field(
        default_factory=list,
        description="May reference a Table.Column or a select item's alias.",
    )
    limit: Optional[int] = pyd.Field(default=None, ge=1)
    offset: int = pyd.Field(default=0, ge=0)
    top_n: Optional[TopNSpec] = pyd.Field(
        default=None,
        description="Top/bottom N rows per partition — see TopNSpec's own fields.",
    )
    intent: Optional[str] = pyd.Field(
        default=None,
        description=(
            "Brief natural-language summary of the ask that led to this query — logged "
            "with the compiled SQL for audit/debugging, never returned to the caller."
        ),
    )

    model_config = pyd.ConfigDict(populate_by_name=True, extra="forbid")

    @pyd.model_validator(mode="after")
    def _validate_table_aliases(self) -> "StructuredQuery":
        """Every from/join occurrence has an *effective name* (its alias if
        given, else its own table name) — the name every Table.Column ref
        elsewhere in the query must use. Effective names must be unique, and
        a physical table used more than once (a self-join) must carry an
        explicit alias on EVERY occurrence, so there's never an implicit
        "first occurrence wins" ambiguity. This runs at the AST layer, before
        any DB touch, same as every other structural StructuredQuery check.
        """
        occurrences = [(self.from_table, self.from_alias)] + [
            (j.table, j.alias) for j in self.joins
        ]
        physical_counts: Dict[str, int] = {}
        for physical, _alias in occurrences:
            key = physical.lower()
            physical_counts[key] = physical_counts.get(key, 0) + 1

        seen: Set[str] = set()
        for physical, alias in occurrences:
            effective = (alias or physical).lower()
            if effective in seen:
                raise ValueError(
                    f"Duplicate table/alias {effective!r} — every from/join effective "
                    "name (its alias if given, else its table name) must be unique"
                )
            seen.add(effective)
            if physical_counts[physical.lower()] > 1 and alias is None:
                raise ValueError(
                    f"Table {physical!r} is used more than once in this query "
                    "(a self-join) — every occurrence must have an explicit alias, "
                    "including this one"
                )
        return self


# StructuredQuery references Predicate (via WhereNode) and Predicate now references
# StructuredQuery (value_subquery) — a recursive cycle (TODO.md item 97). CaseWhen
# also joins the cycle now that its `when` is a WhereNode (item 99), and item 100's
# Expression union closes a second cycle through it (Expression -> CaseExpr ->
# CaseWhen -> WhereNode -> Predicate -> Expression). Rebuild every model in both
# cycles once all names are defined so the forward refs resolve. Order matters
# least once all names exist, but do the leaf types first.
BinaryOpExpr.model_rebuild()
FunctionExpr.model_rebuild()
CastExpr.model_rebuild()
CaseExpr.model_rebuild()
AggregateSelectItem.model_rebuild()
ExpressionSelectItem.model_rebuild()
Predicate.model_rebuild()
WhereGroup.model_rebuild()
CaseWhen.model_rebuild()
CaseSelectItem.model_rebuild()
StructuredQuery.model_rebuild()
