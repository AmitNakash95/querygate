"""Pydantic AST for read-only structured queries.

This is the ONLY shape an agent can submit — there is no raw-SQL entry point
anywhere in QueryGate. Every field here is later checked against the live
reflected schema (validation/schema_validation.py) and the active policy
(validation/policy_validation.py) before it is ever compiled to SQL.
"""

from __future__ import annotations

from typing import Any, List, Literal, Optional, Union

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
AggregateFn = Literal["count", "sum", "avg", "min", "max"]
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
    def _distinct_requires_real_column(self) -> "AggregateSelectItem":
        if self.distinct and self.col == "*":
            raise ValueError("distinct is not valid with count(*) — give a real column")
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


SelectItem = Union[str, AggregateSelectItem, DateBucketSelectItem]


class JoinSpec(pyd.BaseModel):
    table: str
    type: JoinType = "inner"
    on: List[str] = pyd.Field(
        min_length=2,
        max_length=2,
        description="Equality join: [LeftTable.Col, RightTable.Col]",
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


class Predicate(pyd.BaseModel):
    col: str
    op: CompareOp
    value: Optional[Any] = pyd.Field(
        default=None,
        description=(
            "Required for every op except is_null/is_not_null (omit value for those two). "
            "between: two-element [low, high] list. in/not_in: non-empty list. "
            "Everything else: a single scalar."
        ),
    )

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _validate_value_shape(self) -> "Predicate":
        if self.op in ("is_null", "is_not_null"):
            if self.value is not None:
                raise ValueError(f"Operator {self.op!r} must not include a value")
            return self
        if self.value is None:
            raise ValueError(f"Operator {self.op!r} requires a value")
        if self.op == "between":
            if not isinstance(self.value, (list, tuple)) or len(self.value) != 2:
                raise ValueError("Operator 'between' requires a two-element list [low, high]")
        if self.op in ("in", "not_in"):
            if not isinstance(self.value, (list, tuple)) or len(self.value) == 0:
                raise ValueError(f"Operator {self.op!r} requires a non-empty list")
        return self


class WhereGroup(pyd.BaseModel):
    """Boolean group: set exactly one of `and`/`or` (not both, not neither) to a
    list of terms, where each term is itself a Predicate or a nested WhereGroup
    — nest freely to express arbitrary boolean logic.
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

    model_config = pyd.ConfigDict(populate_by_name=True, extra="forbid")

    @pyd.model_validator(mode="after")
    def _exactly_one_boolean(self) -> "WhereGroup":
        has_and = bool(self.and_terms)
        has_or = bool(self.or_terms)
        if has_and == has_or:
            raise ValueError("Where group must have exactly one of 'and' or 'or'")
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
    top_n.partition_by) must be "Table.Column" (e.g. "Customer.Name"), or a
    select item's own alias where noted below — never a bare column name.
    """

    from_table: str = pyd.Field(
        validation_alias=pyd.AliasChoices("from", "from_table"),
        serialization_alias="from",
        description='Root table name (not Table.Column) — e.g. "Customer".',
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
