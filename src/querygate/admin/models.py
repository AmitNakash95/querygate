"""Config-governance version model.

A `ConfigVersion` is a complete, immutable snapshot of connections.yaml +
policy.yaml + an optional catalog.yaml, submitted through the admin API
rather than edited on disk. History is never rewritten — applying an older
version ("rollback") creates no new snapshot, it just moves the "active"
pointer, so every version that ever existed remains inspectable.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
import math
from typing import Any, Dict, Literal, Optional

import pydantic as pyd

from querygate.policy.models import CostEstimationMode
from querygate.query_ast.models import StructuredQuery


class ConfigVersionStatus(str, Enum):
    STAGED = "staged"
    ACTIVE = "active"
    INACTIVE = "inactive"


class ConfigVersion(pyd.BaseModel):
    id: str
    status: ConfigVersionStatus
    created_at: datetime
    created_by: str
    description: Optional[str] = None

    # Full source YAML, never resolved. Operators should use ${...} references,
    # but the model deliberately treats this as privileged content because a
    # submitted file can still contain a literal connection string. Guide and
    # audit projections therefore never serialize these fields.
    connections_yaml: str
    policy_yaml: str
    catalog_yaml: Optional[str] = None

    applied_at: Optional[datetime] = None
    applied_by: Optional[str] = None
    # The version that was active immediately before this one, recorded the
    # moment this version became active — lets an operator trace exactly
    # what a given apply/rollback replaced.
    previous_active_version_id: Optional[str] = None

    model_config = pyd.ConfigDict(extra="forbid")


class ConfigDocumentPreview(pyd.BaseModel):
    """Content-free change signal safe for a config writer without read scope."""

    document: Literal["connections", "policy", "catalog"]
    change: Literal["changed", "unchanged", "submitted", "inherited"]

    model_config = pyd.ConfigDict(extra="forbid")


class ConfigPreview(pyd.BaseModel):
    valid: bool
    errors: list[str] = pyd.Field(default_factory=list)
    documents: list[ConfigDocumentPreview]
    ready_to_stage: bool
    redactions: list[str] = pyd.Field(
        default_factory=lambda: [
            "configuration contents",
            "connection strings and secret values/references",
            "principal, table, column, and catalog identifiers",
        ]
    )

    model_config = pyd.ConfigDict(extra="forbid")


class CandidatePolicySimulationRequest(pyd.BaseModel):
    """An isolated policy decision against uncommitted config documents.

    Unset documents inherit from the active governance version (or directly
    from the deployment files before governance has been bootstrapped). The
    target principal is deliberately separate from the authenticated admin
    actor making the request.
    """

    connections_yaml: Optional[str] = None
    policy_yaml: Optional[str] = None
    catalog_yaml: Optional[str] = None
    principal: str = pyd.Field(min_length=1, max_length=256)
    claims: Dict[str, Any] = pyd.Field(default_factory=dict, max_length=100)
    connection: str = pyd.Field(min_length=1, max_length=256)
    table: Optional[str] = pyd.Field(default=None, max_length=256)
    columns: list[str] = pyd.Field(default_factory=list, max_length=100)
    query: Optional[StructuredQuery] = None

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.field_validator("principal", "connection")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @pyd.field_validator("table")
    @classmethod
    def _strip_optional(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return value.strip() or None

    @pyd.field_validator("columns")
    @classmethod
    def _normalize_columns(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values if value.strip()]
        if any(len(value) > 256 for value in normalized):
            raise ValueError("column names must be at most 256 characters")
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("columns must not contain duplicates")
        return normalized

    @pyd.field_validator("claims")
    @classmethod
    def _scalar_claims_only(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        for name, claim in value.items():
            if not name.strip() or len(name) > 256:
                raise ValueError("claim names must be between 1 and 256 characters")
            if claim is not None and not isinstance(claim, (str, int, float, bool)):
                raise ValueError("claim values must be scalar")
            if isinstance(claim, str) and len(claim) > 4096:
                raise ValueError("string claim values must be at most 4096 characters")
            if isinstance(claim, float) and not math.isfinite(claim):
                raise ValueError("numeric claim values must be finite")
        return value

    @pyd.model_validator(mode="after")
    def _columns_need_table(self) -> "CandidatePolicySimulationRequest":
        if self.columns and self.table is None:
            raise ValueError("table is required when columns are supplied")
        return self


class CandidateColumnDecision(pyd.BaseModel):
    column: str
    allowed: bool

    model_config = pyd.ConfigDict(extra="forbid")


class MandatoryFilterReadiness(pyd.BaseModel):
    table: str
    column: str
    source: Literal["claim", "configured_literal"]
    claim: Optional[str] = None
    ready: bool

    model_config = pyd.ConfigDict(extra="forbid")


class EffectiveGuardrails(pyd.BaseModel):
    max_joins: int
    max_select_columns: int
    max_where_depth: int
    max_group_by: int
    max_limit: int
    max_limit_aggregate: int
    default_limit: int
    max_top_n: int
    max_partition_by: int
    max_batch_size: int
    max_response_bytes: int
    timeout_seconds: int
    max_concurrency: int
    concurrency_wait_seconds: float
    max_queue_depth: Optional[int]
    max_queue_depth_per_principal: Optional[int]
    max_estimated_rows: Optional[int]
    max_estimated_cost: Optional[float]
    cost_estimation_mode: CostEstimationMode

    model_config = pyd.ConfigDict(extra="forbid")


CandidateSimulationReasonCode = Literal[
    "connection_not_visible",
    "table_denied",
    "column_denied",
    "mandatory_claim_missing",
    "query_policy_denied",
    "query_connection_denied",
    "allowed",
]


class CandidateSimulationReason(pyd.BaseModel):
    code: CandidateSimulationReasonCode
    message: str

    model_config = pyd.ConfigDict(extra="forbid")


class CandidatePolicySimulation(pyd.BaseModel):
    decision: Literal["allow", "deny"]
    principal: str
    connection: str
    table: Optional[str]
    columns: list[CandidateColumnDecision] = pyd.Field(default_factory=list)
    query_evaluated: bool
    query_allowed: Optional[bool] = None
    mandatory_filters: list[MandatoryFilterReadiness] = pyd.Field(default_factory=list)
    guardrails: Optional[EffectiveGuardrails] = None
    reasons: list[CandidateSimulationReason]
    evaluation_scope: Literal["candidate_policy_and_visibility"] = "candidate_policy_and_visibility"
    redactions: list[str] = pyd.Field(
        default_factory=lambda: [
            "candidate configuration documents",
            "connection strings, resolved secret values, and secret references",
            "static mandatory-filter values and supplied claim values",
            "query predicate values, natural-language intent, and compiled SQL",
            "policy-hidden identifiers outside the caller-supplied target",
        ]
    )

    model_config = pyd.ConfigDict(extra="forbid")


class ConfigSemanticDiffRequest(pyd.BaseModel):
    """A semantic (resolved-behavior) diff of a candidate against the active version.

    Unset documents inherit unchanged from the active governance version (or
    directly from the deployment files before governance has been
    bootstrapped) — send only the file(s) that actually change, exactly like
    the staging/validate requests. There is no target principal: this diff
    resolves the default and per-connection policy layers at the connection
    baseline (see `SemanticAccessDiff.evaluation_scope`).
    """

    connections_yaml: Optional[str] = None
    policy_yaml: Optional[str] = None
    catalog_yaml: Optional[str] = None

    model_config = pyd.ConfigDict(extra="forbid")


SemanticChangeCategory = Literal[
    "connection_visibility",
    "guardrail",
    "table_access",
    "column_access",
    "mandatory_filter",
    "join_group",
]

SemanticChangeDirection = Literal["tightening", "loosening", "neutral"]

SemanticChangeType = Literal["added", "removed", "modified"]


class SemanticAccessChange(pyd.BaseModel):
    """One resolved-behavior change between the active and candidate configs.

    `before`/`after` only ever carry non-sensitive resolved values — a
    guardrail number, a visibility state (`visible`/`hidden`/`absent`), a join
    group name, or a mandatory-filter source kind. Static mandatory-filter
    values, resolved secrets, connection strings, and query predicate values
    are structurally never placed here (see `SemanticAccessDiff.redactions`).
    """

    category: SemanticChangeCategory
    connection: str
    # Table name, `Table.Column`, guardrail field name, mandatory-filter
    # `Table.Column`, or None for a whole-connection visibility change.
    object: Optional[str] = None
    change_type: SemanticChangeType
    direction: SemanticChangeDirection
    before: Optional[str] = None
    after: Optional[str] = None
    detail: str

    model_config = pyd.ConfigDict(extra="forbid")


class SemanticDiffSummary(pyd.BaseModel):
    total: int = 0
    loosening: int = 0
    tightening: int = 0
    neutral: int = 0

    model_config = pyd.ConfigDict(extra="forbid")


class SemanticAccessDiff(pyd.BaseModel):
    """Server-derived, authorization-aware diff of *resolved* access — not a
    line diff of YAML — between the active config version and a candidate.

    Phase 1 (`evaluation_scope="connection_baseline"`) resolves the default
    and per-connection policy layers with no principal applied. When the
    per-principal override layer itself changes, or an allow-list toggles
    between "restricted" and "unrestricted" (so objects the policy never names
    may also be affected), or the change list is truncated, `analysis_incomplete`
    is set with a human-readable reason rather than silently under-reporting.
    """

    changes: list[SemanticAccessChange] = pyd.Field(default_factory=list)
    summary: SemanticDiffSummary = pyd.Field(default_factory=SemanticDiffSummary)
    analysis_incomplete: bool = False
    incomplete_reasons: list[str] = pyd.Field(default_factory=list)
    truncated: bool = False
    evaluation_scope: Literal["connection_baseline"] = "connection_baseline"
    redactions: list[str] = pyd.Field(
        default_factory=lambda: [
            "candidate configuration documents and raw YAML",
            "connection strings, resolved secret values, and secret references",
            "static mandatory-filter values and supplied claim values",
            "query predicate values and compiled SQL",
        ]
    )

    model_config = pyd.ConfigDict(extra="forbid")


class BlastRadiusPrincipalImpact(pyd.BaseModel):
    """The resolved-access diff for one explicitly configured principal.

    Built with the exact same classification logic as `SemanticAccessDiff`
    (`admin/access_diff.compute_access_diff`, called with that principal
    resolved instead of the connection baseline) — only the resolution layer
    differs, never the change taxonomy.
    """

    principal: str
    changes: list[SemanticAccessChange] = pyd.Field(default_factory=list)
    summary: SemanticDiffSummary = pyd.Field(default_factory=SemanticDiffSummary)
    analysis_incomplete: bool = False
    incomplete_reasons: list[str] = pyd.Field(default_factory=list)
    truncated: bool = False

    model_config = pyd.ConfigDict(extra="forbid")


class RankedBlastRadiusChange(pyd.BaseModel):
    """One access-expanding change, ranked by risk, with the scope it applies
    at: `"baseline"` means every principal without an override is affected
    (fleet-wide); `"principal"` means only the named principal is affected.
    """

    scope: Literal["baseline", "principal"]
    principal: Optional[str] = None
    change: SemanticAccessChange

    model_config = pyd.ConfigDict(extra="forbid")


class PolicyBlastRadiusReport(pyd.BaseModel):
    """Aggregates the resolved-access diff across the default/connection
    baseline plus every principal with an explicit `principals:` override, so
    a reviewer can tell a targeted change from a fleet-wide access expansion
    or guardrail relaxation before staging it (TODO item 41).

    Every principal *not* itemized in `principal_impacts` behaves exactly
    like `baseline` — they inherit the default/connection layers unmodified
    by any principal override — stated explicitly rather than left for a
    reviewer to infer from an empty list. `highest_risk` ranks only
    access-expanding changes (mandatory-row-filter removal ranked above newly
    visible connections/tables/columns, ranked above loosened guardrails),
    since a syntactically tiny change can matter far more than a large one —
    tightening/neutral changes remain visible in full in `baseline` and each
    principal's own `changes` list, just not in this risk-priority view. Work
    is bounded: at most `principals_evaluated` (of `principals_configured`)
    principals are individually diffed, and each principal's own change list
    is separately capped — either cap being reached sets
    `analysis_incomplete` with a specific reason rather than silently
    omitting impact.
    """

    baseline: SemanticAccessDiff
    principal_impacts: list[BlastRadiusPrincipalImpact] = pyd.Field(default_factory=list)
    principals_configured: int = 0
    principals_evaluated: int = 0
    principals_affected: int = 0
    highest_risk: list[RankedBlastRadiusChange] = pyd.Field(default_factory=list)
    analysis_incomplete: bool = False
    incomplete_reasons: list[str] = pyd.Field(default_factory=list)
    evaluation_scope: Literal["connection_baseline_plus_configured_principals"] = (
        "connection_baseline_plus_configured_principals"
    )
    redactions: list[str] = pyd.Field(
        default_factory=lambda: [
            "candidate configuration documents and raw YAML",
            "connection strings, resolved secret values, and secret references",
            "static mandatory-filter values and supplied claim values",
            "query predicate values and compiled SQL",
        ]
    )

    model_config = pyd.ConfigDict(extra="forbid")


class TemplateParameter(pyd.BaseModel):
    """One typed input a policy template (TODO.md item 46) needs rendered.

    Templates never infer parameters from live schema/table names and never
    carry credentials or tenant values — every value here comes from the
    caller filling in the template's own declared, reviewed parameter list.
    """

    name: str
    label: str
    type: Literal["string", "integer", "string_list"]
    required: bool = True
    default: Optional[Any] = None
    description: str

    model_config = pyd.ConfigDict(extra="forbid")


class PolicyTemplateSummary(pyd.BaseModel):
    id: str
    name: str
    description: str
    parameters: list[TemplateParameter] = pyd.Field(default_factory=list)

    model_config = pyd.ConfigDict(extra="forbid")


class PolicyTemplateRenderRequest(pyd.BaseModel):
    template_id: str = pyd.Field(min_length=1, max_length=100)
    params: Dict[str, Any] = pyd.Field(default_factory=dict, max_length=50)
    # The caller's current local, uncommitted policy draft to merge into.
    # Omitted/empty starts from an empty document.
    policy_yaml: Optional[str] = pyd.Field(default=None, max_length=200_000)

    model_config = pyd.ConfigDict(extra="forbid")


class PolicyTemplateRenderResult(pyd.BaseModel):
    """A template applied to the caller's local draft via a monotonically
    restrictive merge: it can only add or tighten a restriction already
    present in that draft, never loosen or remove one. `rules` is a plain-
    English preview of every rule the merged document now enforces so an
    administrator can review before staging — normal validation (`/validate`)
    plus item 39's candidate simulation still run against the result exactly
    like any other hand-edited draft; this endpoint persists nothing.
    """

    policy_yaml: str
    rules: list[str]
    warnings: list[str] = pyd.Field(default_factory=list)

    model_config = pyd.ConfigDict(extra="forbid")
