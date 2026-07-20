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
    """Aggregate projection, e.g. COUNT(*) AS order_count."""

    fn: AggregateFn
    col: str = pyd.Field(description="Column ref (Table.Col) or '*' for count")
    distinct: bool = pyd.Field(
        default=False,
        description="Aggregate only over distinct values of col, e.g. COUNT(DISTINCT col).",
    )
    alias: Optional[str] = pyd.Field(
        default=None,
        validation_alias=pyd.AliasChoices("as", "alias"),
        serialization_alias="as",
    )

    model_config = pyd.ConfigDict(populate_by_name=True, extra="forbid")

    @pyd.model_validator(mode="after")
    def _validate_distinct(self) -> "AggregateSelectItem":
        if self.distinct and self.col == "*":
            raise ValueError("distinct is not valid with count(*) — give a real column")
        if self.distinct and self.fn in _NO_DISTINCT_AGG_FNS:
            raise ValueError(
                f"distinct is not valid with {self.fn} — MSSQL's STDEV/VAR don't accept "
                "DISTINCT, so this stays rejected on every dialect rather than working on "
                "Postgres and breaking on MSSQL"
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


class ColArg(pyd.BaseModel):
    """A scalar-function argument that is a column reference, not a literal."""

    col: str = pyd.Field(description="Table.Column (or Alias.Column) ref supplying this argument.")

    model_config = pyd.ConfigDict(extra="forbid")


class LiteralArg(pyd.BaseModel):
    """A scalar-function argument that is a literal value, not a column."""

    literal: Any = pyd.Field(description="A literal scalar value (string/number/bool/null).")

    model_config = pyd.ConfigDict(extra="forbid")


ScalarFunctionArg = Union[ColArg, LiteralArg]

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


class CaseWhen(pyd.BaseModel):
    """One CASE WHEN branch: a single Predicate condition (not a full
    WhereNode — a deliberate v1 simplification covering the common
    `CASE WHEN col = x THEN ...` shape without full boolean nesting inside a
    select item) and the value to project when it's true.
    """

    when: Predicate
    then: ScalarFunctionArg

    model_config = pyd.ConfigDict(extra="forbid")


class CaseSelectItem(pyd.BaseModel):
    """CASE WHEN ... THEN ... [ELSE ...] END, evaluated top-to-bottom —
    the first matching `when` wins. `alias` is REQUIRED (unlike aggregates/
    date_bucket) since there's no sensible default output name for a
    conditional expression.
    """

    when: List[CaseWhen] = pyd.Field(min_length=1)
    else_: Optional[ScalarFunctionArg] = pyd.Field(
        default=None,
        validation_alias=pyd.AliasChoices("else", "else_"),
        serialization_alias="else",
    )
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
        description="Table.Column being compared. Exactly one of col/col_fn is required.",
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

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _validate_col_shape(self) -> "Predicate":
        if (self.col is None) == (self.col_fn is None):
            raise ValueError("Predicate must set exactly one of 'col' or 'col_fn'")
        return self

    @pyd.model_validator(mode="after")
    def _validate_value_shape(self) -> "Predicate":
        if self.op in ("is_null", "is_not_null"):
            if self.value is not None or self.value_col is not None:
                raise ValueError(f"Operator {self.op!r} must not include a value or value_col")
            return self
        if self.value is not None and self.value_col is not None:
            raise ValueError("Predicate must not set both 'value' and 'value_col'")
        if self.value is None and self.value_col is None:
            raise ValueError(f"Operator {self.op!r} requires a value or value_col")
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
WhereGroup.model_rebuild()


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
    having: List[Predicate] = pyd.Field(
        default_factory=list,
        description=(
            "Predicates evaluated after group_by/aggregation, same shape as a where "
            "Predicate. The list is AND-combined (no nested and/or here) — for OR logic "
            "on grouped values, filter in `where` before grouping instead."
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
