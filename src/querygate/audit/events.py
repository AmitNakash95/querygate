"""Stable, redaction-safe event schema for persisted query auditing."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Union

import pydantic as pyd

from querygate.query_ast.models import (
    AggregateSelectItem,
    ArrayAggSelectItem,
    CaseSelectItem,
    ColArg,
    DateBucketSelectItem,
    PercentileContSelectItem,
    Predicate,
    ScalarFunctionSelectItem,
    StringAggSelectItem,
    StructuredQuery,
    WhereGroup,
    WhereNode,
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
    "apply",
    "rollback",
    "export",
    "import",
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
        shape: Dict[str, Any] = {"kind": "aggregate", "function": item.fn, "column": item.col}
        if item.alias is not None:
            shape["alias"] = item.alias
        return shape
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
        }
    raise TypeError(f"Unsupported select item: {type(item).__name__}")


def _predicate_shape(predicate: Predicate) -> Dict[str, Any]:
    shape: Dict[str, Any] = {"operator": predicate.op}
    if predicate.col is not None:
        shape["column"] = predicate.col
    else:
        assert predicate.col_fn is not None
        shape["function"] = predicate.col_fn.fn
        shape["columns"] = [arg.col for arg in predicate.col_fn.args if isinstance(arg, ColArg)]
    return shape


def _where_shape(node: WhereNode) -> Dict[str, Any]:
    if isinstance(node, Predicate):
        return _predicate_shape(node)
    if isinstance(node, WhereGroup):
        if node.and_terms:
            return {"and": [_where_shape(term) for term in node.and_terms]}
        return {"or": [_where_shape(term) for term in node.or_terms or []]}
    raise TypeError(f"Unsupported where node: {type(node).__name__}")


def normalize_query_shape(query: StructuredQuery) -> Dict[str, Any]:
    """Return useful query structure without literals or natural-language intent."""
    shape: Dict[str, Any] = {
        "from": query.from_table,
        "select": [_select_shape(item) for item in query.select],
        "joins": [
            {
                "table": join.table,
                "type": join.type,
                "on": list(join.on),
                **({"connection": join.connection} if join.connection else {}),
            }
            for join in query.joins
        ],
        "group_by": list(query.group_by),
        "having": [_predicate_shape(predicate) for predicate in query.having],
        "order_by": [order.model_dump() for order in query.order_by],
        "offset": query.offset,
    }
    if query.where is not None:
        shape["where"] = _where_shape(query.where)
    if query.limit is not None:
        shape["requested_limit"] = query.limit
    if query.top_n is not None:
        shape["top_n"] = {
            "partition_by": list(query.top_n.partition_by),
            "order_by": [order.model_dump() for order in query.top_n.order_by],
            "n": query.top_n.n,
            "function": query.top_n.fn,
        }
    return shape
