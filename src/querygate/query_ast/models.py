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
JoinType = Literal["inner", "left", "full", "cross"]
SortDir = Literal["asc", "desc"]
RankFn = Literal["row_number", "rank", "dense_rank"]
DateGranularity = Literal["day", "week", "month", "quarter", "year"]
SetOpKind = Literal["union", "intersect", "except"]


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


# --------------------------------------------------------------------------
# Date/time primitives (TODO.md item 102). Three nodes, each its own union
# member for the SAME reason CastExpr is one: their non-expression field is a
# KEYWORD (a date part, a unit, a clock kind), not a scalar. Folding any of
# them into `FunctionExpr.args` would have required an `Any`-shaped or
# free-string argument — the escape hatch this substrate forbids.
#
# What deliberately does NOT exist here is an `interval` union member. An
# interval is not a scalar (you cannot project it on MSSQL, compare it to a
# number, or group by it), so admitting one would break the substrate's
# defining property — every `Expression` is legal everywhere a scalar is
# expected — which is exactly the trade the plan's §5 row-15 note refuses for
# windows. `DateAddExpr` carries the magnitude as a capped keyword+integer
# pair instead and yields a timestamp, so the union stays all-scalar.
#
# TIMEZONE (2026-07-26 Decision Log): every clock reading is UTC on every
# dialect, and the Postgres session is pinned to UTC in
# `connections/dialects.py` so `EXTRACT`/`date_trunc` over a `timestamptz`
# resolve there too — without that pin those are server-config dependent.
# --------------------------------------------------------------------------

# Every part returns an INTEGER on every dialect, with one definition each —
# the adapter renders that definition in its own idiom, the `date_bucket`
# category of variance. Two need stating because dialects disagree natively:
#   * `dayofweek` is 0=Sunday..6=Saturday (Postgres/SQLite numbering). T-SQL's
#     DATEPART(weekday) is 1-based AND shifts with `SET DATEFIRST`, so the
#     adapter renders the DATEFIRST-independent idiom rather than inheriting a
#     number that depends on the server's language setting.
#   * `week` is the ISO-8601 week number (Postgres `week`, T-SQL `iso_week`).
#     T-SQL's plain `week` is a different, DATEFIRST-dependent count.
DatePart = Literal[
    "year",
    "quarter",
    "month",
    "week",
    "day",
    "dayofweek",
    "dayofyear",
    "hour",
    "minute",
    "second",
]

# No sub-second unit: the cap is expressed in days, and a microsecond offset is
# not a relative-date filter — it is a rounding artifact.
IntervalUnit = Literal["year", "month", "week", "day", "hour", "minute", "second"]


class ExtractExpr(pyd.BaseModel):
    """EXTRACT one integer field from a date/timestamp expression, e.g. the hour
    of Order.CreatedAt. Evaluated in UTC. `dayofweek` is 0=Sunday..6=Saturday
    and `week` is the ISO-8601 week number, on every dialect."""

    extract: "Expression"
    part: DatePart

    model_config = pyd.ConfigDict(extra="forbid")


class NowExpr(pyd.BaseModel):
    """The current UTC time — "timestamp" for the full clock reading, "date" for
    midnight UTC today. Use it with date_add to filter relative to now instead of
    computing a timestamp literal caller-side."""

    now: Literal["timestamp", "date"]

    model_config = pyd.ConfigDict(extra="forbid")


# `amount` is signed rather than there being a separate subtract node: one node
# means one cap check, one adapter method, and one place the magnitude rule can
# be got wrong.
class DateAddExpr(pyd.BaseModel):
    """Shift a date/timestamp by a whole number of units — negative goes back in
    time, so {"date_add": {"now": "timestamp"}, "unit": "day", "amount": -7} is
    "7 days ago". The magnitude is bounded by the policy's max_interval_days."""

    date_add: "Expression"
    unit: IntervalUnit
    # Bounded to signed 32-bit independently of `max_interval_days`, because the
    # two bound DIFFERENT things and only one of them tracks the dialect's own
    # limit. `max_interval_days` bounds calendar REACH in days; this bounds the
    # NUMBER handed to the dialect. T-SQL's `DATEADD` takes an `int`, and going
    # one past it is a live server error, not a typed rejection — measured on
    # SQL Server 2022: `DATEADD(second, 2147483647, …)` succeeds, `…, 2147483648`
    # raises "Arithmetic overflow error converting expression to data type int".
    # The two only diverge once a deployment raises `max_interval_days` above
    # 24,855 (at which point a `second`-unit amount within the day-cap can still
    # exceed int32), so without this a deployment could tune itself into a driver
    # error. Deny-by-default at the narrowest limit across supported dialects.
    amount: int = pyd.Field(ge=-2_147_483_648, le=2_147_483_647)

    model_config = pyd.ConfigDict(extra="forbid")


# Upper bounds, not averages: a cap must never be *under*-counted by a unit
# whose real length varies (a year can be 366 days, a month 31), or "31 months"
# would slip past a cap that "944 days" would not.
_UNIT_MAX_SECONDS: Dict[str, int] = {
    "year": 366 * 86_400,
    "month": 31 * 86_400,
    "week": 7 * 86_400,
    "day": 86_400,
    "hour": 3_600,
    "minute": 60,
    "second": 1,
}


def interval_magnitude_days(node: DateAddExpr) -> int:
    """`node`'s absolute shift in whole days, rounded UP — the quantity
    `Policy.max_interval_days` bounds. Rounding up keeps the cap from being a
    fraction under-counted (23 hours is not "0 days" of reach)."""
    seconds = abs(node.amount) * _UNIT_MAX_SECONDS[node.unit]
    return -(-seconds // 86_400)  # ceil division, integer-exact


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
# by their required field names (col / literal / op / fn / cast / when /
# extract / now / date_add) and every one forbids extras, so Pydantic resolves
# the union unambiguously — the same approach `ScalarFunctionArg` already uses.
Expression = Union[
    ColumnExpr,
    LiteralExpr,
    BinaryOpExpr,
    FunctionExpr,
    CastExpr,
    CaseExpr,
    ExtractExpr,
    NowExpr,
    DateAddExpr,
]


# Defined here rather than beside TopNSpec below because WindowSelectItem's
# `over.order_by` needs it; TopNSpec and StructuredQuery use the same class.
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
            "own default ordering. Not supported on MSSQL (T-SQL has no NULLS "
            "FIRST/LAST syntax); order by a CASE 0/1 'is null' bucket first instead."
        ),
    )

    model_config = pyd.ConfigDict(extra="forbid")


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


# --------------------------------------------------------------------------
# General window functions (TODO.md item 101), the second expressiveness
# pillar of docs/ENGINE_EXPRESSIVENESS_PLAN.md.
#
# `top_n` was previously the ONLY OVER() surface, hard-wired to
# rank-and-filter-top-N. A `WindowSelectItem` only *projects* a window value
# (running totals, moving averages, rank-in-place, lag/lead gap analysis); the
# two stay separate types deliberately — see TopNSpec.
#
# Every bound below is either a portability requirement or a cost cap, and each
# is recorded in the 2026-07-26 Decision Log entry in docs/PRODUCT_GUIDE.md:
#   * a window is a PROJECTION, not an `Expression` operand — so it cannot be
#     nested inside arithmetic/aggregates, which is also why it cannot appear in
#     WHERE/HAVING or as a group key;
#   * `over` is REQUIRED (possibly empty) — it is what structurally distinguishes
#     `SUM(x) OVER ()` from the plain `SUM(x)` aggregate, which otherwise share
#     the {fn, arg, as} shape and would resolve ambiguously in the SelectItem
#     union;
#   * refs inside `over` must be real Table.Columns: no dialect lets an OVER
#     clause reference a peer SELECT alias;
#   * ORDER BY inside OVER is required wherever T-SQL requires it, on every
#     dialect, so one AST cannot render fine on Postgres and break live on MSSQL;
#   * frames are only accepted where T-SQL accepts them, and no frame is
#     synthesized when `frame` is omitted (the SQL-standard default applies).
# --------------------------------------------------------------------------

WindowFn = Literal[
    "sum",
    "avg",
    "min",
    "max",
    "count",
    "row_number",
    "rank",
    "dense_rank",
    "ntile",
    "lag",
    "lead",
    "first_value",
    "last_value",
]

# Aggregate-style windows compute an aggregate over the frame. Called out as a
# set because `Policy.min_group_size` (the item-88 k-anonymity floor) has to
# reject exactly these: the floor is a HAVING on grouped results, and a window
# aggregate produces no group to filter (see policy_validation).
_WINDOW_AGGREGATE_FNS = frozenset({"sum", "avg", "min", "max", "count"})
# Ranking functions take no argument at all.
_WINDOW_NO_ARG_FNS = frozenset({"row_number", "rank", "dense_rank", "ntile"})
# T-SQL REQUIRES ORDER BY inside OVER for these; Postgres/SQLite merely make
# them meaningless without it. Required everywhere — the stricter rule wins, the
# same reasoning that pins `substring` to exactly 3 arguments.
_WINDOW_ORDERED_FNS = _WINDOW_NO_ARG_FNS | {"lag", "lead", "first_value", "last_value"}
# A ROWS/RANGE frame is only accepted by T-SQL for aggregate windows and
# FIRST_VALUE/LAST_VALUE, so it is rejected elsewhere rather than silently
# ignored (Postgres tolerates more).
_WINDOW_FRAMEABLE_FNS = _WINDOW_AGGREGATE_FNS | {"first_value", "last_value"}
_WINDOW_OFFSET_FNS = frozenset({"lag", "lead"})

WindowBoundKind = Literal[
    "unbounded_preceding",
    "preceding",
    "current_row",
    "following",
    "unbounded_following",
]
WindowFrameMode = Literal["rows", "range"]


class WindowBound(pyd.BaseModel):
    """One end of a window frame, e.g. {"bound": "preceding", "offset": 6}."""

    bound: WindowBoundKind
    offset: Optional[int] = pyd.Field(
        default=None,
        ge=1,
        description=(
            "Distance from the current row — required for preceding/following, "
            "forbidden for the unbounded/current_row kinds. Capped by "
            "Policy.max_window_frame_offset."
        ),
    )

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _validate_offset(self) -> "WindowBound":
        needs_offset = self.bound in ("preceding", "following")
        if needs_offset and self.offset is None:
            raise ValueError(f"window frame bound {self.bound!r} requires an offset")
        if not needs_offset and self.offset is not None:
            raise ValueError(f"window frame bound {self.bound!r} takes no offset")
        return self

    # SQLAlchemy's frame encoding: None = unbounded (which end is implied by the
    # bound's position, and WindowFrame rejects the two nonsensical positions),
    # 0 = current row, -n = n preceding, +n = n following.
    def sqlalchemy_bound(self) -> Optional[int]:
        if self.bound in ("unbounded_preceding", "unbounded_following"):
            return None
        if self.bound == "current_row":
            return 0
        return -self.offset if self.bound == "preceding" else self.offset

    # Ordering position on the frame's number line, used only to reject an
    # inverted frame (start after end) at the AST layer.
    def _position(self) -> float:
        if self.bound == "unbounded_preceding":
            return float("-inf")
        if self.bound == "unbounded_following":
            return float("inf")
        return float(self.sqlalchemy_bound())


class WindowFrame(pyd.BaseModel):
    """Which rows around the current one the window covers, e.g. the last 7 rows:
    {"mode": "rows", "start": {"bound": "preceding", "offset": 6},
     "end": {"bound": "current_row"}}. Omit `frame` entirely for the SQL default.
    `mode` "range" only accepts unbounded/current_row bounds on MSSQL (T-SQL has
    no numeric RANGE offsets) — use "rows" for an N-row window.
    """

    mode: WindowFrameMode
    start: WindowBound
    end: WindowBound

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _validate_bounds(self) -> "WindowFrame":
        if self.start.bound == "unbounded_following":
            raise ValueError("a window frame cannot start at 'unbounded_following'")
        if self.end.bound == "unbounded_preceding":
            raise ValueError("a window frame cannot end at 'unbounded_preceding'")
        if self.start._position() > self.end._position():
            raise ValueError("a window frame's start must not come after its end")
        return self


class WindowSpec(pyd.BaseModel):
    """The OVER (...) clause: which rows the window covers and in what order.
    Every column ref here must be a real Table.Column — no dialect allows an
    OVER clause to reference another select item's alias.
    """

    partition_by: List[str] = pyd.Field(
        default_factory=list,
        description=(
            "Table.Column refs to compute the window within (omit for one "
            "partition over all rows). Counted against Policy.max_partition_by."
        ),
    )
    order_by: List[OrderBySpec] = pyd.Field(
        default_factory=list,
        description=(
            "Ordering inside each partition; each col must be a Table.Column, "
            "never a select alias. Required for the ranking/offset functions and "
            "whenever `frame` is set."
        ),
    )
    frame: Optional[WindowFrame] = pyd.Field(
        default=None,
        description=(
            "ROWS/RANGE frame. Omit for the dialect's SQL-standard default frame "
            "(with order_by: everything up to the current row's peers; without it: "
            "the whole partition). Only valid for sum/avg/min/max/count/"
            "first_value/last_value."
        ),
    )

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _validate_refs(self) -> "WindowSpec":
        for ref in self.partition_by:
            _require_column_ref(ref, "window partition_by")
        for order in self.order_by:
            _require_column_ref(order.col, "window order_by col")
        return self


class WindowSelectItem(pyd.BaseModel):
    """A window function projection — fn(arg) OVER (partition/order/frame) — for
    running totals, moving averages, rank-in-place, and lag/lead comparisons.
    Unlike an aggregate it does NOT collapse rows, and unlike `top_n` it does not
    filter them. `over` is required (use {} for OVER ()); `as` names the output.
    Cannot be combined with group_by or aggregate select items, and cannot be
    nested inside another expression.
    """

    fn: WindowFn
    over: WindowSpec = pyd.Field(
        description="The OVER (...) clause — {} for OVER (), i.e. one partition of all rows."
    )
    arg: Optional["Expression"] = pyd.Field(
        default=None,
        description=(
            "The scalar expression the function reads — required for sum/avg/min/max/"
            "lag/lead/first_value/last_value, optional for count (omit for COUNT(*)), "
            "and forbidden for row_number/rank/dense_rank/ntile."
        ),
    )
    offset: Optional[int] = pyd.Field(
        default=None,
        ge=1,
        description="lag/lead only: how many rows back/forward to read (default 1).",
    )
    buckets: Optional[int] = pyd.Field(
        default=None,
        ge=1,
        description="ntile only: how many buckets to split each partition into.",
    )
    alias: str = pyd.Field(
        validation_alias=pyd.AliasChoices("as", "alias"),
        serialization_alias="as",
    )

    model_config = pyd.ConfigDict(populate_by_name=True, extra="forbid")

    @pyd.model_validator(mode="after")
    def _validate_shape(self) -> "WindowSelectItem":
        if self.fn in _WINDOW_NO_ARG_FNS:
            if self.arg is not None:
                raise ValueError(f"window function {self.fn} takes no 'arg'")
        elif self.arg is None and self.fn != "count":
            raise ValueError(f"window function {self.fn} requires an 'arg'")
        if self.offset is not None and self.fn not in _WINDOW_OFFSET_FNS:
            raise ValueError(f"'offset' is only valid for lag/lead, not {self.fn}")
        if self.fn == "ntile" and self.buckets is None:
            raise ValueError("window function ntile requires 'buckets'")
        if self.buckets is not None and self.fn != "ntile":
            raise ValueError(f"'buckets' is only valid for ntile, not {self.fn}")
        if self.over.frame is not None and self.fn not in _WINDOW_FRAMEABLE_FNS:
            raise ValueError(
                f"a ROWS/RANGE frame is not valid for window function {self.fn} — "
                "frames apply to sum/avg/min/max/count/first_value/last_value"
            )
        if not self.over.order_by and (
            self.fn in _WINDOW_ORDERED_FNS or self.over.frame is not None
        ):
            reason = "a frame" if self.fn not in _WINDOW_ORDERED_FNS else f"{self.fn}"
            raise ValueError(f"{reason} requires over.order_by")
        return self

    def is_aggregate_window(self) -> bool:
        """Whether this window computes an aggregate over its frame — the form the
        k-anonymity floor (`Policy.min_group_size`) cannot enforce."""
        return self.fn in _WINDOW_AGGREGATE_FNS


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
    WindowSelectItem,
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
    on: Optional[List[str]] = pyd.Field(
        default=None,
        min_length=2,
        max_length=2,
        description=(
            "Equality join: [LeftTable.Col, RightTable.Col] — the common-case sugar; "
            "use `condition` for a range/inequality join. Exactly one of on/condition "
            "is required; a `cross` join takes neither."
        ),
    )
    extra_on: List[List[str]] = pyd.Field(
        default_factory=list,
        description=(
            "Additional equality pairs ANDed with `on`, for composite/multi-column join "
            "keys — each entry is a two-element [LeftTable.Col, RightTable.Col] pair "
            "referencing the SAME two tables/aliases as `on` (a join's condition is "
            "always about the one pair of tables it joins, never a third). Only valid "
            "with `on`."
        ),
    )
    condition: Optional["WhereNode"] = pyd.Field(
        default=None,
        description=(
            "General join condition — the same predicate tree `where` uses, so a join "
            "can be a range/temporal one, e.g. ON Sale.Price BETWEEN Band.Lo AND "
            "Band.Hi (gte + lte ANDed). Must reference the table this join adds, and "
            "may only reference tables ALREADY in the graph (the from table or an "
            "EARLIER join). No IN (subquery) here — that is WHERE-only."
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

    @pyd.model_validator(mode="after")
    def _validate_join_form(self) -> "JoinSpec":
        """One join carries exactly one condition form (item 103).

        `on`/`extra_on` (equality sugar) and `condition` (the general predicate
        tree) are two spellings of the same clause, so accepting both would leave
        their precedence — ANDed? overriding? — up to the compiler to invent.
        A `cross` join carries neither: an unconditioned cartesian product is the
        entire point of it, and silently ignoring a condition the caller wrote
        would answer a different question than they asked.
        """
        if self.type == "cross":
            supplied = [
                name
                for name, value in (
                    ("on", self.on),
                    ("extra_on", self.extra_on or None),
                    ("condition", self.condition),
                )
                if value is not None
            ]
            if supplied:
                raise ValueError(
                    f"a 'cross' join takes no condition, got {'/'.join(supplied)} — a "
                    "cross join is an unconditioned cartesian product; use 'inner' with "
                    "the condition instead"
                )
            return self
        if (self.on is None) == (self.condition is None):
            raise ValueError(
                "exactly one of `on` (equality sugar) or `condition` (general "
                "predicate tree) is required on a join"
            )
        if self.extra_on and self.on is None:
            raise ValueError(
                "`extra_on` is additional equality pairs for `on` and cannot be used "
                "with `condition` — express the extra pairs inside `condition` instead"
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


# Why the carrying query is arm 1 rather than a dedicated wrapper type with an
# `arms` list of its own (item 104):
#   * SQL itself works this way. `SELECT a FROM t WHERE x UNION SELECT b FROM u
#     ORDER BY 1 LIMIT 10` binds the WHERE to the first arm and the ORDER BY/LIMIT
#     to the whole statement. The apparent asymmetry here is that asymmetry,
#     not one this AST invented.
#   * A separate top-level type would make `from`/`select` optional on every
#     query in the codebase, or force `Union[StructuredQuery, SetOperationQuery]`
#     through every REST route, MCP tool, template, audit and approval call site
#     — an enormous blast radius whose failure mode is a consumer that silently
#     handles only one member. Keeping one top-level type means every existing
#     consumer keeps compiling, and the ones that must now see every arm are
#     found by walking `iter_query_scopes`, which is already the authority.
#   * MCP context cost stays small because `arms` is a `$ref` back to the
#     StructuredQuery already in `$defs`: a measured 1,485 chars of tool schema
#     (see tests/unit/test_mcp_token_budget.py for the full base-vs-tree table).
#     Stated as the measurement it is: this was NOT the deciding argument, the
#     blast radius above was.
class SetOpSpec(pyd.BaseModel):
    """Combine this query's rows with further queries: UNION / INTERSECT / EXCEPT.

    The query carrying `set_op` is the FIRST arm — its from/joins/where/group_by/
    having describe arm 1, while its order_by/limit/offset apply to the combined
    result (exactly as in SQL, where they are written once after the last arm).
    Every arm must project the same number of select items, and an arm may not
    carry its own order_by/limit/offset/top_n/set_op.
    """

    op: SetOpKind = pyd.Field(
        description=(
            "union = rows in either side; intersect = rows in both; "
            "except = rows in this query but not in the arms."
        )
    )
    all_: bool = pyd.Field(
        default=False,
        validation_alias=pyd.AliasChoices("all", "all_"),
        serialization_alias="all",
        description=(
            "Keep duplicate rows (UNION ALL). Default false de-duplicates. "
            "Only valid with 'union' outside Postgres."
        ),
    )
    arms: List["StructuredQuery"] = pyd.Field(
        min_length=1,
        description="The further queries to combine with this one, in order.",
    )

    model_config = pyd.ConfigDict(populate_by_name=True, extra="forbid")


# Item 105 — the derived table / CTE.
#
# Spelled as a named `WITH` block rather than a subquery inlined into
# `from`/`JoinSpec.table`, which is what ENGINE_EXPRESSIVENESS_PLAN.md §4 Phase 4b
# originally specified. The plan's shape would have turned two `str` fields into
# unions, and the failure mode of that is not a caller — it is an *unaware
# consumer*: every existing site reading `query.from_table` would receive a model
# where it expected a string (`referenced_tables` putting a non-string into a set
# of table names, `normalize_query_shape` recording it as the table that was read),
# with Pydantic unable to flag any of it because the field is legitimately both.
# Here `from_table` stays a `str` naming *something*, so a consumer that has never
# heard of a CTE treats the name as a table — and reflection then rejects it,
# because no such table exists. The unaware consumer fails closed. See the
# `docs/PRODUCT_GUIDE.md` Decision Log entry dated 2026-07-27.
class CteSpec(pyd.BaseModel):
    """One named `WITH` block: a complete query that later stages refer to by name.

    Reference it exactly like a table — put its `name` in `from`/`joins[].table`.
    Because it is referenced by name rather than inlined, one CTE can feed the FROM
    clause and several joins without being written (or planned) more than once.
    """

    name: str = pyd.Field(
        # Same identifier shape a physical table must have (`VALID_TABLE_NAME`);
        # restated as a pattern rather than imported so the constraint reaches the
        # JSON Schema an MCP client sees, and so this stays a pure AST-layer rule
        # with no dependency from query_ast into schema reflection.
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description=(
            "Name later stages refer to this block by, used in `from` or a join's "
            "`table` exactly like a physical table name. Must not be the name of a "
            "real table used anywhere in the query."
        ),
    )
    query: "StructuredQuery" = pyd.Field(
        description=(
            "The query this block computes. It is a full scope in its own right: its "
            "tables get the same allow/deny, mandatory row filters, column masking "
            "and k-anonymity floor as any other query. It may reference an EARLIER "
            "cte by name, never a later one and never itself."
        )
    )

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
    set_op: Optional[SetOpSpec] = pyd.Field(
        default=None,
        description=(
            "Combine this query with further queries via UNION/INTERSECT/EXCEPT. "
            "This query is the first arm — see SetOpSpec's own fields."
        ),
    )
    ctes: List[CteSpec] = pyd.Field(
        default_factory=list,
        description=(
            "Named WITH blocks computed before this query and referred to by name in "
            "`from`/`joins[].table`, for multi-stage analysis in one statement "
            "(aggregate-then-join, dedup-then-rank). Only the top-level query may "
            "declare these — not a set_op arm, an IN (subquery), or another cte's "
            "query. Each may reference an EARLIER cte, never a later one or itself "
            "(so a recursive cte is not expressible)."
        ),
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

    @pyd.model_validator(mode="after")
    def _validate_window_scope(self) -> "StructuredQuery":
        """A window function (item 101) is computed over the query's ROW scope, so
        it cannot coexist with grouping — SQL would evaluate it after the GROUP BY
        over columns that no longer exist per row, and expressing a window over
        *aggregated* values needs the aggregation materialized as a derived table
        (TODO.md item 105). Rejected here at the AST layer: no DB touch, dialect-
        independent, and the same structural class of check as the alias rule above.
        """
        if not any(isinstance(item, WindowSelectItem) for item in self.select):
            return self
        if self.group_by or any(isinstance(i, _AGGREGATE_SELECT_ITEM_TYPES) for i in self.select):
            raise ValueError(
                "a window function cannot be combined with group_by or aggregate "
                "select items — a window projects a value per row, so aggregate in "
                "one query and window over that result in a second query"
            )
        return self

    @pyd.model_validator(mode="after")
    def _validate_set_op(self) -> "StructuredQuery":
        """The structural rules of a set operation (item 104), all of them
        dialect-independent and checkable with no DB touch — the same class as the
        alias and window rules above.

        Every rule here exists because SQL puts the clause on the *statement*, not
        on an arm: a per-arm `order_by` has no meaning once the rows are combined
        (and MSSQL rejects it outright), a per-arm `limit` would silently return an
        arbitrary subset of each side, and mismatched arity is an error every
        backend reports differently. Rejecting them here means one typed message
        instead of three dialect-specific ones.
        """
        spec = self.set_op
        if spec is None:
            return self
        if self.top_n is not None:
            # `top_n` materializes the query as a ranked derived table, which has
            # no meaning over a compound: its partition_by/order_by refs resolve
            # against table columns the combined result no longer has. Reject
            # rather than grow a second materialization path (the posture item 101
            # took for group_by + window).
            raise ValueError(
                "top_n cannot be combined with set_op — rank within each arm, or "
                "order the combined result with order_by + limit"
            )
        for arm in spec.arms:
            if arm.set_op is not None:
                raise ValueError(
                    "a set_op arm may not carry its own set_op — list every arm in "
                    "one `arms` list instead of nesting them"
                )
            disallowed = [
                name
                for name, unset in (
                    ("order_by", not arm.order_by),
                    ("limit", arm.limit is None),
                    ("offset", not arm.offset),
                    ("top_n", arm.top_n is None),
                )
                if not unset
            ]
            if disallowed:
                raise ValueError(
                    f"a set_op arm may not set {disallowed} — order_by/limit/offset/"
                    "top_n apply to the combined result and belong on the query that "
                    "carries set_op"
                )
            if len(arm.select) != len(self.select):
                raise ValueError(
                    f"every set_op arm must project the same number of columns: this "
                    f"query projects {len(self.select)}, an arm projects "
                    f"{len(arm.select)}"
                )
        return self

    @pyd.model_validator(mode="after")
    def _validate_cte_names(self) -> "StructuredQuery":
        """The one cte rule that is purely LOCAL to this query — names are unique,
        case-insensitively, because a reference is by name and two blocks answering
        to one name has no defined meaning (item 105).

        The other cte rules deliberately live in `policy_validation` instead of
        here: "only the root scope declares ctes", "no forward or self reference",
        "a name may not collide with a physical table" and "a declared cte must be
        referenced" all need to walk the query's *scope tree*, and that walk has a
        single authority (`iter_query_scopes`). Reproducing it here would be a
        second copy of the traversal items 96 and 111 exist to prevent — and a
        wrong one, since this validator cannot know whether `self` is the root.
        """
        seen: Set[str] = set()
        for spec in self.ctes:
            key = spec.name.lower()
            if key in seen:
                raise ValueError(
                    f"Duplicate cte name {spec.name!r} — every cte name must be unique"
                )
            seen.add(key)
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
ExtractExpr.model_rebuild()
DateAddExpr.model_rebuild()
CaseExpr.model_rebuild()
AggregateSelectItem.model_rebuild()
ExpressionSelectItem.model_rebuild()
WindowSelectItem.model_rebuild()
Predicate.model_rebuild()
WhereGroup.model_rebuild()
CaseWhen.model_rebuild()
CaseSelectItem.model_rebuild()
# JoinSpec.condition is a WhereNode (item 103), so JoinSpec joins the cycle too —
# it is declared before Predicate/WhereGroup and would otherwise keep an
# unresolved forward ref, making every `condition` fail to validate at request time.
JoinSpec.model_rebuild()
# SetOpSpec.arms is a forward ref to StructuredQuery, which is declared after it
# (item 104) — a third cycle into the same knot, resolved the same way.
SetOpSpec.model_rebuild()
# CteSpec.query is a forward ref to StructuredQuery for the same reason (item 105).
CteSpec.model_rebuild()
StructuredQuery.model_rebuild()
