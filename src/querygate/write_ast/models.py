"""Pydantic AST for governed writes (TODO.md item 93, Phase 1).

The write sibling of `query_ast/models.py`: the ONLY shape in which a mutation
can be *proposed*. There is **no raw-DML field of any kind** — an `UPDATE`/
`DELETE` is a typed structure with a required filter tree (`WritePredicate` /
`WriteWhereGroup`, a *narrowing* of the read predicate — see item 114 below;
it converts to the read `WhereNode` at the validation boundary, so there is still
only one predicate-enumerating walk and one compiler), and an `INSERT` is a list
of typed rows. Every
statement is later checked against the live schema
(`validation/write_schema_validation.py`) and the active `WritePolicy`
(`validation/write_policy_validation.py`) before it is ever compiled — and in
Phase 1 it is only ever *previewed* (compiled, run in a transaction, diffed, and
rolled back), never committed.

Structural guarantees encoded here, not by lint:
- `UpdateStatement`/`DeleteStatement` **require** a `where` — an unqualified
  mutation cannot even be constructed.
- Single-table only in Phase 1 (no joins in a write target).
- Values are literal scalars in Phase 1 (scalar-function/CASE SET-values reuse
  the read surface in a later slice); no expression grammar is invented here.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

import pydantic as pyd

from querygate.core.exceptions import QueryValidationError
from querygate.query_ast.models import (
    CompareOp,
    Predicate,
    ScalarFunctionCall,
    WhereGroup,
    WhereNode,
    _require_column_ref,
)

WriteOp = Literal["insert", "update", "delete", "upsert"]


# ---------------------------------------------------------------------------
# The write filter tree (TODO.md item 114).
#
# A write's WHERE used to reuse the READ `Predicate` verbatim, which was correct
# at runtime — `validate_write_policy` rejects `expr`/`value_expr` (item 100) and
# `value_subquery` (item 110) — but wrong as a CONTRACT: the agent-facing JSON
# schema advertised all three, dragging the entire `Expression` union and the
# whole read `StructuredQuery` definition into the write tool. That is ~10K
# characters of every MCP session spent describing fields the server refuses,
# and, worse, an invitation to build a write that will be denied — the "hit a
# wall, route around the gate" failure the engine plan exists to prevent.
#
# These types carry exactly what a write accepts. They are a NARROWING of the
# read predicate, not a parallel grammar: `to_read_where` converts at the
# validation boundary, so the canonical predicate walk (item 111), the compiler,
# and every downstream guarantee remain the single read implementation. The
# value-shape rules are not re-stated either — `WritePredicate` validates by
# constructing the read `Predicate`, so there is one rule set, enforced at parse
# time. The runtime rejections stay as defence in depth.
# ---------------------------------------------------------------------------


# The read predicate's alternatives-lists name fields a write does not have.
# Rewriting them keeps the error channel inside the write contract; the mapping is
# explicit (not a regex over field names) so it is reviewable, and a test asserts
# no removed field name survives into any write error message.
_WRITE_VOCABULARY = (
    (
        "requires a value, value_col, value_expr, or value_subquery",
        "requires a value or value_col",
    ),
    (
        "at most one of 'value', 'value_col', 'value_expr', or 'value_subquery'",
        "at most one of 'value' or 'value_col'",
    ),
    ("exactly one of 'col', 'col_fn', or 'expr'", "exactly one of 'col' or 'col_fn'"),
)


def _write_vocabulary(exc: pyd.ValidationError) -> str:
    message = "; ".join(error["msg"].removeprefix("Value error, ") for error in exc.errors())
    for read_phrasing, write_phrasing in _WRITE_VOCABULARY:
        message = message.replace(read_phrasing, write_phrasing)
    return message


class WritePredicate(pyd.BaseModel):
    """One filter condition on a write's target rows: a column compared against a
    literal, a list, or another column. Deliberately narrower than the read
    predicate — no computed expressions and no subqueries in a write filter."""

    col: Optional[str] = pyd.Field(
        default=None,
        description="Table.Column being compared. Exactly one of col/col_fn is required.",
    )
    col_fn: Optional[ScalarFunctionCall] = pyd.Field(
        default=None,
        description=(
            "A whitelisted scalar function applied to a column instead of a bare "
            "Table.Column, e.g. {fn: lower, args: [{col: Customer.Name}]}. "
            "Mutually exclusive with col."
        ),
    )
    op: CompareOp
    value: Optional[Any] = pyd.Field(
        default=None,
        description=(
            "A literal to compare against. Required for every op except is_null/"
            "is_not_null. between: two-element [low, high]. in/not_in: non-empty "
            "list. Everything else: a single scalar. Mutually exclusive with value_col."
        ),
    )
    value_col: Optional[str] = pyd.Field(
        default=None,
        description=(
            "Compare against another Table.Column instead of a literal. Only valid "
            "for eq/neq/lt/lte/gt/gte. Mutually exclusive with value."
        ),
    )

    # `frozen`: the read predicate below is cached, and a mutated field would leave
    # the cache stale — which would split `write_fingerprint` (hashed from the
    # FIELDS) from the compiled DML (built from the CACHE), so an approval token
    # could bind to a write that is not the one executed. Nothing mutates a write
    # predicate today; freezing makes it impossible to start.
    model_config = pyd.ConfigDict(extra="forbid", frozen=True)

    # The validated read `Predicate` this narrowing corresponds to, built ONCE at
    # parse time. `as_read_predicate()` returns it rather than re-validating at
    # each of the six conversion sites — the same reasoning (and the same
    # `model_construct` idiom) as `CaseSelectItem.as_expression()`. It also means
    # a conversion of a VALIDATED instance cannot raise inside
    # `WriteExecutionService.execute`'s audited try-block, where a Pydantic message
    # would carry predicate values into the operator log. (A `model_construct`ed
    # instance rebuilds lazily; see `as_read_predicate`, which raises the typed
    # error rather than a bare `ValueError`.)
    _read_predicate: Optional[Predicate] = pyd.PrivateAttr(default=None)

    @pyd.model_validator(mode="after")
    def _validate_via_the_read_predicate(self) -> "WritePredicate":
        """The OPERATOR/VALUE rules are the read `Predicate`'s, not a second copy:
        building one here runs every one of them, so the two cannot disagree about
        what `between` or `in` means.

        Two rules are write-only and deliberately stricter — `col` and `value_col`
        must be dotted `Table.Column` refs, which the read predicate defers to
        `parse_column_ref` because a bare `col` is legal there (a HAVING clause may
        name a select alias). The two are not equivalent gaps: a bare **`col`** was
        *skipped* by `predicate_direct_column_refs`, so it bypassed the allow/deny
        and masking walk entirely and only failed later in the compiler — that is a
        real latent gap this closes. A bare **`value_col`** was always yielded by
        that walk and already rejected by `parse_column_ref`; requiring it here only
        moves the same rejection to parse time.
        """
        for field, ref in (("col", self.col), ("value_col", self.value_col)):
            if ref is not None:
                _require_column_ref(ref, field)
        self._read_predicate = self._build_read_predicate()
        return self

    def _build_read_predicate(self) -> Predicate:
        try:
            return Predicate(
                col=self.col,
                col_fn=self.col_fn,
                op=self.op,
                value=self.value,
                value_col=self.value_col,
            )
        except pyd.ValidationError as exc:
            # Re-raise in the WRITE vocabulary. The read messages name
            # `value_expr`/`value_subquery`/`expr` as alternatives — the exact
            # fields item 114 removed from this contract — so passing them through
            # would move the "advertise a field the server refuses" defect out of
            # the schema and into the error channel.
            raise ValueError(_write_vocabulary(exc)) from None

    def as_read_predicate(self) -> Predicate:
        """The equivalent read `Predicate`. Built during validation; rebuilt only
        for an instance that skipped validation (`model_construct`).

        That rebuild is the one path that can fail outside a Pydantic validator —
        i.e. inside `WriteExecutionService.execute`'s audited try-block — so it
        raises the TYPED error, which the transports map to a 422. A bare
        `ValueError` there would be masked to a 500 instead.
        """
        if self._read_predicate is None:
            try:
                self._read_predicate = self._build_read_predicate()
            except ValueError as exc:
                raise QueryValidationError(str(exc)) from None
        return self._read_predicate


class WriteWhereGroup(pyd.BaseModel):
    """Boolean group over write predicates: set exactly one of `and`/`or`/`not`,
    the same shape as a read query's WhereGroup."""

    and_terms: Optional[List["WriteWhereNode"]] = pyd.Field(
        default=None,
        validation_alias=pyd.AliasChoices("and", "and_terms"),
        serialization_alias="and",
    )
    or_terms: Optional[List["WriteWhereNode"]] = pyd.Field(
        default=None,
        validation_alias=pyd.AliasChoices("or", "or_terms"),
        serialization_alias="or",
    )
    not_terms: Optional["WriteWhereNode"] = pyd.Field(
        default=None,
        validation_alias=pyd.AliasChoices("not", "not_terms"),
        serialization_alias="not",
    )

    model_config = pyd.ConfigDict(populate_by_name=True, extra="forbid")

    @pyd.model_validator(mode="after")
    def _exactly_one_boolean(self) -> "WriteWhereGroup":
        set_count = sum([bool(self.and_terms), bool(self.or_terms), self.not_terms is not None])
        if set_count != 1:
            raise ValueError("Where group must have exactly one of 'and', 'or', or 'not'")
        return self


WriteWhereNode = Union[WritePredicate, WriteWhereGroup]


def to_read_where(node: Union["WriteWhereNode", WhereNode]) -> WhereNode:
    """Convert a write filter tree into the read `WhereNode` every downstream
    walker and the compiler already understand.

    **A read node at the root passes through unchanged** (by identity),
    deliberately: `validate_write_policy`'s
    rejections of `expr`/`value_expr`/`value_subquery` are defence in depth for a
    read predicate reaching the service some other way, and raising here would turn
    a would-be typed 4xx into a 500. It must not launder one into something else
    either.

    **Anything else fails closed**, matching `iter_expression_parts`'s posture
    (item 100) rather than opposing it. The permissive `return node` this replaced
    had a verified silent-corruption path: a future `WriteWhereGroup` combinator
    added alongside `and_terms` would have produced a `WhereGroup` with the new
    terms *dropped* — a compiled DELETE/UPDATE matching MORE rows than the caller
    asked for, agreed on by the preview, the diff and the execution because all
    three re-derive from this one conversion.
    """
    if isinstance(node, WritePredicate):
        return node.as_read_predicate()
    if isinstance(node, WriteWhereGroup):
        # `model_construct`, not `WhereGroup(...)`: the terms are already validated,
        # and re-validating them re-runs each nested leaf's validators. That is the
        # `CaseSelectItem.as_expression()` precedent, and here it also closes a leak
        # — rebuilding a group around a `model_construct`ed read `Predicate` raised a
        # Pydantic error whose text carried the predicate's repr (and re-offered the
        # removed field names) into the operator log.
        if node.not_terms is not None:
            return WhereGroup.model_construct(not_terms=to_read_where(node.not_terms))
        if node.and_terms is not None:
            return WhereGroup.model_construct(
                and_terms=[to_read_where(term) for term in node.and_terms]
            )
        if node.or_terms is not None:
            return WhereGroup.model_construct(
                or_terms=[to_read_where(term) for term in node.or_terms]
            )
        raise QueryValidationError(  # only reachable via WriteWhereGroup.model_construct
            "a write filter group set no boolean combinator"
        )
    if isinstance(node, (Predicate, WhereGroup)):
        return node
    raise QueryValidationError(
        f"Unsupported write filter node {type(node).__name__} — it was added to "
        "WriteWhereNode without being taught to to_read_where"
    )


class InsertStatement(pyd.BaseModel):
    """INSERT one or more typed rows into a single table. Each row is a mapping
    of bare column name -> literal scalar value (no expressions in Phase 1)."""

    op: Literal["insert"] = "insert"
    table: str = pyd.Field(description="Target table name (not Table.Column).")
    rows: List[Dict[str, Any]] = pyd.Field(
        min_length=1, description="Rows to insert; each maps column name -> literal value."
    )

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _validate_rows(self) -> "InsertStatement":
        if any(len(row) == 0 for row in self.rows):
            raise ValueError("Each insert row must set at least one column")
        # Every row must set the same column set — a ragged insert is almost
        # always a mistake and complicates the affected-columns policy check.
        first = set(self.rows[0])
        if any(set(row) != first for row in self.rows):
            raise ValueError("All insert rows must set the same set of columns")
        return self


class UpdateStatement(pyd.BaseModel):
    """UPDATE a single table's `set` columns for rows matching `where`. `where`
    is REQUIRED — an unqualified UPDATE cannot be expressed."""

    op: Literal["update"] = "update"
    table: str = pyd.Field(description="Target table name (not Table.Column).")
    set: Dict[str, Any] = pyd.Field(
        min_length=1, description="Column name -> new literal value. At least one."
    )
    where: WriteWhereNode = pyd.Field(
        description=("Required filter (a predicate or and/or/not group) — no unqualified UPDATE.")
    )

    model_config = pyd.ConfigDict(extra="forbid")


class DeleteStatement(pyd.BaseModel):
    """DELETE rows matching `where` from a single table. `where` is REQUIRED —
    an unqualified DELETE cannot be expressed."""

    op: Literal["delete"] = "delete"
    table: str = pyd.Field(description="Target table name (not Table.Column).")
    where: WriteWhereNode = pyd.Field(
        description=("Required filter (a predicate or and/or/not group) — no unqualified DELETE.")
    )

    model_config = pyd.ConfigDict(extra="forbid")


class UpsertStatement(pyd.BaseModel):
    """INSERT rows, but on a unique/primary-key conflict on `conflict_columns`,
    UPDATE `update_columns` instead of failing (Postgres `ON CONFLICT DO UPDATE`
    / SQLite's equivalent). MSSQL has no `ON CONFLICT` and is **rejected** (per
    the reject-not-emulate doctrine — use a separate insert/update there). No raw
    DML: the conflict target and the updated columns are validated identifiers."""

    op: Literal["upsert"] = "upsert"
    table: str = pyd.Field(description="Target table name (not Table.Column).")
    rows: List[Dict[str, Any]] = pyd.Field(
        min_length=1, description="Rows to insert-or-update; each maps column -> literal value."
    )
    conflict_columns: List[str] = pyd.Field(
        min_length=1,
        description="The unique/PK column(s) whose collision triggers an update instead of insert.",
    )
    update_columns: List[str] = pyd.Field(
        min_length=1,
        description="Which columns to overwrite on conflict — a subset of the row's columns, "
        "excluding the conflict columns.",
    )

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _validate(self) -> "UpsertStatement":
        first = set(self.rows[0])
        if any(len(row) == 0 for row in self.rows) or any(set(row) != first for row in self.rows):
            raise ValueError("All upsert rows must set the same non-empty set of columns")
        missing_conflict = [c for c in self.conflict_columns if c not in first]
        if missing_conflict:
            raise ValueError(f"conflict_columns not present in the rows: {missing_conflict}")
        missing_update = [c for c in self.update_columns if c not in first]
        if missing_update:
            raise ValueError(f"update_columns not present in the rows: {missing_update}")
        overlap = set(self.update_columns) & set(self.conflict_columns)
        if overlap:
            raise ValueError(
                f"update_columns must not include a conflict column: {sorted(overlap)}"
            )
        return self


# Discriminated union dispatched on `op` (mirrors the read AST's registry-based
# select-item dispatch — no scattered isinstance branching at call sites).
WriteStatement = Union[InsertStatement, UpdateStatement, DeleteStatement, UpsertStatement]

_WRITE_STATEMENT_TYPES: Dict[str, type] = {
    "insert": InsertStatement,
    "update": UpdateStatement,
    "delete": DeleteStatement,
    "upsert": UpsertStatement,
}


def write_target_table(statement: WriteStatement) -> str:
    """The single table a write statement targets."""
    return statement.table


WriteWhereGroup.model_rebuild()
UpdateStatement.model_rebuild()
DeleteStatement.model_rebuild()
