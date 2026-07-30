"""Stable, redaction-safe event schema for persisted query auditing."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Union

import pydantic as pyd

from querygate.query_ast.models import (
    AggregateSelectItem,
    ArrayAggSelectItem,
    BinaryOpExpr,
    CaseExpr,
    CaseSelectItem,
    CastExpr,
    ColArg,
    ColumnExpr,
    DateAddExpr,
    DateBucketSelectItem,
    ExpressionSelectItem,
    ExtractExpr,
    FunctionExpr,
    LiteralExpr,
    NowExpr,
    PercentileContSelectItem,
    Predicate,
    ScalarFunctionSelectItem,
    StringAggSelectItem,
    StructuredQuery,
    WhereGroup,
    WhereNode,
    WindowExpr,
    WindowSelectItem,
)
from querygate.validation.schema_validation import select_item_column_refs

AuditDecision = Literal["allowed", "denied", "unknown"]
AuditSurface = Literal["rest", "mcp", "internal"]
ConfigChangeAction = Literal[
    "validate",
    "preview",
    "simulate",
    "diff",
    "blast_radius",
    "check_template_schema",
    "stage",
    "approve",
    "reject",
    "apply",
    "rollback",
    "export",
    "import",
    # Server-side encrypted draft store (item 47 phase 2) — a fourth pair
    # alongside export/import for the alternative recovery path.
    "save_draft",
    "load_draft",
    "delete_draft",
]
CatalogGovernanceAction = Literal[
    "generate",
    "learn",
    "manual_create",
    "edit",
    "approve",
    "reject",
    "bulk_approve",
    "bulk_reject",
    "publish",
    "rollback",
    "export",
    "import",
    "delete_proposal",
    "bulk_delete",
    "delete_version",
]


class AuditEvent(pyd.BaseModel):
    """Versioned event written to persisted audit sinks.

    This model deliberately has no SQL, parameter, intent, result-row, or
    free-form exception fields. Those values can contain customer data. The
    normalized query shape retains identifiers and operators needed for an
    investigation without retaining predicate literals.
    """

    schema_version: str = "1"
    event_id: str = pyd.Field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: datetime = pyd.Field(default_factory=lambda: datetime.now(timezone.utc))
    event_type: Literal["query.execution"] = "query.execution"
    correlation_id: Optional[str] = None
    surface: AuditSurface = "internal"
    operation: str = "execute_structured_query"
    principal_id: Optional[str] = None
    auth_method: str = "unknown"
    principal_scopes: List[str] = pyd.Field(default_factory=list)
    # Delegated on-behalf-of attribution (TODO.md item 90, F1). For a delegated
    # request `principal_id` is the human on whose behalf the agent acted (and
    # whose policy applied); `actor_id` is the immediate agent, and
    # `delegation_chain` the full agent chain (immediate first) for multi-hop
    # delegation. Both are identities, never credentials — the redaction
    # guarantee is unchanged. Absent for a non-delegated caller.
    actor_id: Optional[str] = None
    delegation_chain: List[str] = pyd.Field(default_factory=list)
    connection_id: str
    policy_decision: AuditDecision
    outcome: Literal["success", "rejected"]
    query_shape: Dict[str, Any]
    duration_ms: int = pyd.Field(ge=0)
    row_count: Optional[int] = pyd.Field(default=None, ge=0)
    response_bytes: Optional[int] = pyd.Field(default=None, ge=0)
    truncated: Optional[bool] = None
    error_category: Optional[str] = None
    # Agent-visible admission info (TODO.md item 35) — a stable id
    # correlating this attempt across REST/MCP responses, metrics, and this
    # audit event, how long it waited for a concurrency slot, and whether it
    # completed, hit a caller-visible capacity timeout, or was rejected
    # outright because the queue itself was already at its configured depth
    # (phase 2's max_queue_depth/max_queue_depth_per_principal).
    admission_id: Optional[str] = None
    queue_wait_ms: Optional[int] = pyd.Field(default=None, ge=0)
    admission_state: Optional[Literal["completed", "capacity_timeout", "queue_full"]] = None
    # Curated query-template invocation (TODO.md item 48): the template id and
    # the parameter *names* supplied — never parameter values, which are bound
    # into the query and stripped from `query_shape` like any other literal.
    # Lets operators see curated-tool usage distinctly in the same audit stream.
    template_id: Optional[str] = None
    template_param_shape: Optional[List[str]] = None
    # Output column names that policy masked in this query (TODO.md item 49) —
    # never the pre-mask value, keeping this event redaction-safe. Lets an
    # operator distinguish "masked" from "denied" access in the same stream.
    masked_columns: List[str] = pyd.Field(default_factory=list)

    model_config = pyd.ConfigDict(extra="forbid")


class ConfigChangeEvent(pyd.BaseModel):
    """Versioned event for the config-governance plane (querygate/admin/):
    validating, previewing/simulating, staging, applying, or rolling back a
    connections/policy/catalog version. Deliberately excludes raw YAML content — a version's
    files may contain secret references or an operator-submitted literal, so
    keeping this event narrow, like `AuditEvent`, means the audit trail stays
    a metadata-only record regardless.
    """

    schema_version: str = "1"
    event_id: str = pyd.Field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: datetime = pyd.Field(default_factory=lambda: datetime.now(timezone.utc))
    event_type: Literal["config.governance"] = "config.governance"
    correlation_id: Optional[str] = None
    surface: AuditSurface = "internal"
    action: ConfigChangeAction
    principal_id: Optional[str] = None
    auth_method: str = "unknown"
    principal_scopes: List[str] = pyd.Field(default_factory=list)
    version_id: Optional[str] = None
    previous_version_id: Optional[str] = None
    description: Optional[str] = None
    outcome: Literal["success", "rejected"]
    error_category: Optional[str] = None
    duration_ms: int = pyd.Field(default=0, ge=0)
    # Server-side draft store (item 47 phase 2) — the draft's opaque id for
    # save_draft/load_draft/delete_draft actions. Never the draft's content.
    draft_id: Optional[str] = None

    model_config = pyd.ConfigDict(extra="forbid")


class CatalogGovernanceEvent(pyd.BaseModel):
    """Versioned event for catalog governance (querygate/catalog/governance.py):
    generating, editing, approving, rejecting, publishing, or rolling back a
    draft proposal. Deliberately excludes draft/proposal text, descriptions,
    aliases, and raw catalog YAML — only stable ids, the connection/table/
    column identifiers involved, and the outcome are recorded, the same
    redaction posture as ``ConfigChangeEvent``.
    """

    schema_version: str = "1"
    event_id: str = pyd.Field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: datetime = pyd.Field(default_factory=lambda: datetime.now(timezone.utc))
    event_type: Literal["catalog.governance"] = "catalog.governance"
    correlation_id: Optional[str] = None
    surface: AuditSurface = "internal"
    action: CatalogGovernanceAction
    principal_id: Optional[str] = None
    auth_method: str = "unknown"
    principal_scopes: List[str] = pyd.Field(default_factory=list)
    connection_id: Optional[str] = None
    proposal_id: Optional[str] = None
    proposal_count: Optional[int] = pyd.Field(default=None, ge=0)
    version_id: Optional[str] = None
    entry_id: Optional[str] = None
    outcome: Literal["success", "rejected"]
    error_category: Optional[str] = None
    duration_ms: int = pyd.Field(default=0, ge=0)

    model_config = pyd.ConfigDict(extra="forbid")


class ConnectionProbeEvent(pyd.BaseModel):
    """Versioned event for the admin "test now" connection probe (TODO.md
    item 43 phase 2) — a manually triggered, out-of-band health check
    against one configured connection. Matches `HealthMonitor`'s own
    non-disclosure posture: no raw driver error or connection string, only
    the same stable `failure_category` the read-only status API already
    exposes.
    """

    schema_version: str = "1"
    event_id: str = pyd.Field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: datetime = pyd.Field(default_factory=lambda: datetime.now(timezone.utc))
    event_type: Literal["connection.probe"] = "connection.probe"
    correlation_id: Optional[str] = None
    surface: AuditSurface = "internal"
    connection_id: str
    principal_id: Optional[str] = None
    auth_method: str = "unknown"
    principal_scopes: List[str] = pyd.Field(default_factory=list)
    # "success"/"rejected" describes whether the probe *ran* (auth, unknown
    # connection, disabled connection, rate limit) — not whether the target
    # database itself was reachable, which is `probe_healthy` below.
    outcome: Literal["success", "rejected"]
    probe_healthy: Optional[bool] = None
    failure_category: Optional[str] = None
    latency_ms: Optional[float] = None
    error_category: Optional[str] = None
    duration_ms: int = pyd.Field(default=0, ge=0)

    model_config = pyd.ConfigDict(extra="forbid")


# Sinks (see audit/sinks.py) persist every kind of event through the same
# configured backend — one durable audit trail for query attempts,
# config-governance actions, catalog-governance actions, and connection
# probes.
PersistableEvent = Union[
    AuditEvent, ConfigChangeEvent, CatalogGovernanceEvent, ConnectionProbeEvent
]


def _select_shape(item: object) -> Dict[str, Any]:
    if isinstance(item, str):
        return {"kind": "column", "column": item}
    if isinstance(item, AggregateSelectItem):
        shape: Dict[str, Any] = {"kind": "aggregate", "function": item.fn}
        if item.arg is None:
            shape["column"] = item.col  # count(*)'s star form
        elif isinstance(item.arg, ColumnExpr):
            # The `col` sugar and the canonical bare-column `arg` must produce
            # the identical audited shape — they are one spelling of one query.
            shape["column"] = item.arg.col
        else:
            # A computed argument (item 100): record the STRUCTURE only. The
            # column set says which data was touched; `expression` names the
            # node kinds. No operator constants, function arguments, or CASE
            # literals — a persisted event never carries values (non-negotiable
            # 3), and expressiveness must not become an exfiltration channel.
            shape["expression"] = _expression_shape(item.arg)
            shape["columns"] = list(select_item_column_refs(item))
        if item.alias is not None:
            shape["alias"] = item.alias
        return shape
    if isinstance(item, ExpressionSelectItem):
        return {
            "kind": "expression",
            "alias": item.alias,
            "expression": _expression_shape(item.expr),
            "columns": list(select_item_column_refs(item)),
        }
    if isinstance(item, DateBucketSelectItem):
        shape = {
            "kind": "date_bucket",
            "column": item.col,
            "granularity": item.granularity,
        }
        if item.alias is not None:
            shape["alias"] = item.alias
        return shape
    if isinstance(item, StringAggSelectItem):
        shape = {"kind": "string_agg", "column": item.col}
        if item.alias is not None:
            shape["alias"] = item.alias
        return shape
    if isinstance(item, ArrayAggSelectItem):
        shape = {"kind": "array_agg", "column": item.col}
        if item.alias is not None:
            shape["alias"] = item.alias
        return shape
    if isinstance(item, PercentileContSelectItem):
        shape = {"kind": "percentile_cont", "column": item.col, "fraction": item.fraction}
        if item.alias is not None:
            shape["alias"] = item.alias
        return shape
    if isinstance(item, ScalarFunctionSelectItem):
        shape = {
            "kind": "scalar_fn",
            "function": item.fn,
            "columns": list(select_item_column_refs(item)),
        }
        if item.alias is not None:
            shape["alias"] = item.alias
        return shape
    if isinstance(item, CaseSelectItem):
        return {
            "kind": "case",
            "alias": item.alias,
            "branch_count": len(item.when),
            "columns": list(select_item_column_refs(item)),
            "conditions": [_where_shape(branch.when) for branch in item.when],
        }
    if isinstance(item, WindowSelectItem):
        # Structure only (item 101): which window function ran, over which
        # columns, and the frame's SHAPE. No frame offset, no lag/lead distance —
        # those are caller literals, and a persisted event carries no values
        # (non-negotiable 3), the same line `_expression_shape` holds.
        shape = {
            "kind": "window",
            "function": item.fn,
            "alias": item.alias,
            "columns": list(select_item_column_refs(item)),
        }
        if item.arg is not None:
            shape["expression"] = _expression_shape(item.arg)
        if item.over.frame is not None:
            shape["frame"] = {
                "mode": item.over.frame.mode,
                "start": item.over.frame.start.bound,
                "end": item.over.frame.end.bound,
            }
        return shape
    raise TypeError(f"Unsupported select item: {type(item).__name__}")


def _expression_shape(expr: object) -> Dict[str, Any]:
    """The redaction-safe SHAPE of a scalar Expression (item 100): which node
    kinds it is built from and which columns it reads — never a literal value,
    never an operator's operands. Recursion mirrors the union so a nested CASE
    or function is described structurally rather than flattened away.
    """
    if isinstance(expr, ColumnExpr):
        return {"node": "column", "column": expr.col}
    if isinstance(expr, LiteralExpr):
        # Deliberately no `value` key — this is the whole point.
        return {"node": "literal"}
    if isinstance(expr, BinaryOpExpr):
        return {
            "node": "binary_op",
            "operator": expr.op,
            "operands": [_expression_shape(expr.left), _expression_shape(expr.right)],
        }
    if isinstance(expr, FunctionExpr):
        return {
            "node": "function",
            "function": expr.fn,
            "args": [_expression_shape(arg) for arg in expr.args],
        }
    if isinstance(expr, CastExpr):
        return {"node": "cast", "to": expr.to, "operand": _expression_shape(expr.cast)}
    if isinstance(expr, ExtractExpr):
        # `part` is a closed enum chosen from a fixed list, not caller data —
        # the same class of structural fact as an operator or a cast target,
        # which are already recorded. It cannot carry a value.
        return {"node": "extract", "part": expr.part, "operand": _expression_shape(expr.extract)}
    if isinstance(expr, NowExpr):
        return {"node": "now", "kind": expr.now}
    if isinstance(expr, DateAddExpr):
        # `unit` is an enum, so it is recorded; `amount` is a caller-supplied
        # NUMBER and is deliberately omitted — "shifted by some days" is shape,
        # "shifted by 90 days" is a predicate value, and a persisted event
        # carries no values (non-negotiable 3). Same line `_expression_shape`
        # holds for every literal and the one item-101 frame offsets sit on.
        return {"node": "date_add", "unit": expr.unit, "operand": _expression_shape(expr.date_add)}
    if isinstance(expr, CaseExpr):
        return {
            "node": "case",
            "branch_count": len(expr.when),
            "conditions": [_where_shape(branch.when) for branch in expr.when],
            "results": [_expression_shape(branch.then) for branch in expr.when],
            **({"else": _expression_shape(expr.else_)} if expr.else_ is not None else {}),
        }
    if isinstance(expr, WindowExpr):
        # Item 125 — the operand spelling of a window, recorded with exactly the
        # posture `_select_shape` uses for the projection spelling: which function
        # ran, over which columns, and the frame's SHAPE. The partition/order refs
        # are named because they are column identifiers an investigator needs; the
        # frame's offsets, the ntile bucket count and the lag/lead distance are
        # caller NUMBERS and stay out, like every other literal (non-negotiable 3).
        shape: Dict[str, Any] = {
            "node": "window",
            "function": expr.fn,
            "partition_by": list(expr.over.partition_by),
            "order_by": [order.col for order in expr.over.order_by],
        }
        if expr.arg is not None:
            shape["operand"] = _expression_shape(expr.arg)
        if expr.over.frame is not None:
            shape["frame"] = {
                "mode": expr.over.frame.mode,
                "start": expr.over.frame.start.bound,
                "end": expr.over.frame.end.bound,
            }
        return shape
    raise TypeError(f"Unsupported expression node: {type(expr).__name__}")


def _predicate_shape(predicate: Predicate) -> Dict[str, Any]:
    shape: Dict[str, Any] = {"operator": predicate.op}
    if predicate.exists_subquery is not None:
        # An EXISTS test has no left-hand column — the subquery IS the predicate
        # (item 106) — so it is recorded before the col/col_fn/expr branch below,
        # which would otherwise assert on an AST the AST layer explicitly permits.
        #
        # The nested scope's shape is recorded in full, for the reason item 104
        # gave for set-op arms and item 105 for cte bodies: without it the event
        # says a query read `customers` when it also read `orders`. Its `correlate`
        # list is recorded too, because that is the exact set of outer columns the
        # nested scope was allowed to see — the most security-relevant fact about a
        # correlated query, and column identifiers only, never values.
        shape["exists_subquery"] = normalize_query_shape(predicate.exists_subquery)
        return shape
    if predicate.col is not None:
        shape["column"] = predicate.col
    elif predicate.col_fn is not None:
        shape["function"] = predicate.col_fn.fn
        shape["columns"] = [arg.col for arg in predicate.col_fn.args if isinstance(arg, ColArg)]
    else:
        assert (
            predicate.expr is not None
        )  # nosec B101 — the AST guarantees exactly one of col/col_fn/expr
        shape["expression"] = _expression_shape(predicate.expr)
    if predicate.value_col is not None:
        # The OTHER side of a column-to-column comparison. A column identifier,
        # never a value — the same class of content the join `on` pair has always
        # recorded, so non-negotiable 3 is untouched.
        #
        # Added with item 103, because that item made the omission consequential:
        # a join condition is now a predicate tree, so `JOIN c ON o.cid = c.id`
        # written as a `condition` audited as `{"operator": "eq", "column":
        # "o.cid"}` — dropping the join TARGET — while the identical join written
        # as `on` recorded both sides. Two spellings of one join must not produce
        # materially different audit detail.
        shape["value_column"] = predicate.value_col
    if predicate.value_expr is not None:
        shape["value_expression"] = _expression_shape(predicate.value_expr)
    if predicate.value_subquery is not None:
        # The nested scope's own shape, recursively (item 120) — for the identical
        # reason a set-op arm's (item 104) and a cte body's (item 105) are: the
        # subquery is where the query's other tables, joins and filters live, so
        # `WHERE x IN (SELECT ... FROM employees)` recorded as just an `in` operator
        # would have the audit trail claim the query read one table when it read
        # two. The nested query goes through this same redaction-safe walk, so no
        # literal escapes the inner scope either. Recursion terminates on the AST as
        # parsed: pydantic's own recursion detection rejects a chain past ~127 levels
        # as a 422 before any walker runs, so an UNVALIDATED query is bounded by the
        # parse step, and `Policy.max_subquery_depth` (default 1) bounds a validated
        # one. There is no QueryGate-authored parser depth guard on this path — the
        # bound is pydantic's. That is the same footing the `where`-group recursion
        # above has always stood on, since the shape is normalized before validation
        # so a rejected attempt is audited too.
        shape["value_subquery"] = normalize_query_shape(predicate.value_subquery)
    return shape


def _where_shape(node: WhereNode) -> Dict[str, Any]:
    if isinstance(node, Predicate):
        return _predicate_shape(node)
    if isinstance(node, WhereGroup):
        if node.and_terms:
            return {"and": [_where_shape(term) for term in node.and_terms]}
        if node.or_terms:
            return {"or": [_where_shape(term) for term in node.or_terms]}
        return {"not": _where_shape(node.not_terms)}
    raise TypeError(f"Unsupported where node: {type(node).__name__}")


def normalize_query_shape(query: StructuredQuery) -> Dict[str, Any]:
    """Return useful query structure without literals or natural-language intent."""
    shape: Dict[str, Any] = {
        "from": query.from_table,
        "select": [_select_shape(item) for item in query.select],
        # `on` and `condition` are mutually exclusive by construction (item 103),
        # and a `cross` join carries neither — so each key appears only when the
        # caller actually used that form. A condition goes through the same
        # `_where_shape` as WHERE/HAVING, which records operators and column names
        # but never a literal, so a range join stays redaction-safe.
        "joins": [
            {
                "table": join.table,
                "type": join.type,
                **({"on": list(join.on)} if join.on is not None else {}),
                **({"condition": _where_shape(join.condition)} if join.condition else {}),
                **({"connection": join.connection} if join.connection else {}),
            }
            for join in query.joins
        ],
        "group_by": list(query.group_by),
        "order_by": [order.model_dump() for order in query.order_by],
        "offset": query.offset,
    }
    if query.where is not None:
        shape["where"] = _where_shape(query.where)
    if query.having is not None:
        shape["having"] = _where_shape(query.having)
    if query.limit is not None:
        shape["requested_limit"] = query.limit
    if query.top_n is not None:
        shape["top_n"] = {
            "partition_by": list(query.top_n.partition_by),
            "order_by": [order.model_dump() for order in query.top_n.order_by],
            "n": query.top_n.n,
            "function": query.top_n.fn,
        }
    if query.set_op is not None:
        # Every arm's own shape, recursively (item 104). Recording only the
        # operator would make the audit trail claim a query read one table when it
        # read three — the arms are where the other tables, joins and filters are.
        # The recursion terminates because the AST forbids an arm from carrying its
        # own set_op, and each arm goes through this same redaction-safe walk, so
        # no literal reaches the event from an arm either.
        shape["set_op"] = {
            "op": query.set_op.op,
            "all": query.set_op.all_,
            "arms": [normalize_query_shape(arm) for arm in query.set_op.arms],
        }
    if query.correlate:
        shape["correlate"] = list(query.correlate)
    if query.ctes:
        # Every named block's own shape, recursively (item 105) — for the identical
        # reason the arms above are recorded, and against the identical failure: the
        # outer query's `from` names a cte, so an event without this would record
        # that the query read a table called `totals` and nothing else, when the
        # tables it actually read are all inside the blocks. Recursion terminates
        # because only the root may declare `ctes`, and each body goes through this
        # same redaction-safe walk, so no literal escapes a block either.
        shape["ctes"] = [
            {"name": spec.name, "query": normalize_query_shape(spec.query)} for spec in query.ctes
        ]
    return shape
