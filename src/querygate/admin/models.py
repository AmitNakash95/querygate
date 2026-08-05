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
from typing import Any, Dict, List, Literal, Optional

import pydantic as pyd

from querygate.policy.models import GUARDRAIL_FIELDS, CostEstimationMode, Policy
from querygate.query_ast.models import StructuredQuery


class ConfigVersionStatus(str, Enum):
    STAGED = "staged"
    ACTIVE = "active"
    INACTIVE = "inactive"


class ConfigApprovalDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class ConfigApprovalRecord(pyd.BaseModel):
    """A durable four-eyes review decision on a staged config version (item 42).

    Bound to the version's immutable content fingerprint at decision time, so a
    decision can never silently apply to different content, and attributed to the
    reviewer (who must not be the version's author). Notes are bounded so a
    review can't be used to smuggle large/free-form content into the audit trail.
    """

    approver: str
    decision: ConfigApprovalDecision
    at: datetime
    content_fingerprint: str
    note: Optional[str] = pyd.Field(default=None, max_length=500)

    model_config = pyd.ConfigDict(extra="forbid")


class ConfigVersion(pyd.BaseModel):
    id: str
    status: ConfigVersionStatus
    created_at: datetime
    created_by: str
    description: Optional[str] = None

    # Four-eyes review decisions (item 42). Empty for single-administrator mode
    # (AppConfig.require_config_approvals == 0) and for versions staged before
    # this field existed — a missing field defaults to [] on load, so old
    # manifests remain readable unchanged.
    approvals: list[ConfigApprovalRecord] = pyd.Field(default_factory=list)

    # Full source YAML, never resolved. Operators should use ${...} references,
    # but the model deliberately treats this as privileged content because a
    # submitted file can still contain a literal connection string. Guide and
    # audit projections therefore never serialize these fields.
    connections_yaml: str
    policy_yaml: str
    catalog_yaml: Optional[str] = None
    templates_yaml: Optional[str] = None

    applied_at: Optional[datetime] = None
    applied_by: Optional[str] = None
    # The version that was active immediately before this one, recorded the
    # moment this version became active — lets an operator trace exactly
    # what a given apply/rollback replaced.
    previous_active_version_id: Optional[str] = None

    model_config = pyd.ConfigDict(extra="forbid")


class ConfigDocumentPreview(pyd.BaseModel):
    """Content-free change signal safe for a config writer without read scope."""

    document: Literal["connections", "policy", "catalog", "templates"]
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


CONFIG_CHANGE_SET_FORMAT = "querygate.config-change-set/1"


class ConfigChangeSetBundle(pyd.BaseModel):
    """A portable, self-contained config change set (item 47).

    Carries only the document deltas an admin actually submitted (each of
    connections/policy/catalog/templates is present only when it was part of
    the change), the id and content fingerprint of the base version those
    deltas were composed against, and a description — enough to move a
    reviewed change between environments or recover a lost draft, and to
    detect on re-import that the target's active version has drifted from the
    base.

    This exact model is also the payload the phase-2 encrypted server-side
    draft store (`admin/draft_store.py`) persists — saving/loading a draft
    there is not a second document shape, just an alternative to downloading/
    uploading the same bundle. Either way, importing/loading one validates it
    and hands the deltas back through the existing validate/stage flow — it
    never becomes an ungoverned shadow config store. A bundle can legitimately
    contain a `connections` document with a literal credential (that is the
    caller's own submitted content), which is exactly why `contains_connections`
    is surfaced — and why the phase-1 browser flow never writes this document
    to `localStorage`, and why the phase-2 server-side store encrypts it at
    rest rather than persisting it as plain YAML.
    """

    bundle_format: Literal["querygate.config-change-set/1"] = CONFIG_CHANGE_SET_FORMAT
    base_version_id: Optional[str] = None
    base_fingerprint: Optional[str] = None
    created_at: datetime
    description: Optional[str] = None
    # Only the documents the change actually touched. Keys are constrained to
    # the four governed document names; an empty mapping means "no delta".
    documents: Dict[Literal["connections", "policy", "catalog", "templates"], str] = pyd.Field(
        default_factory=dict
    )

    model_config = pyd.ConfigDict(extra="forbid")

    @property
    def contains_connections(self) -> bool:
        return "connections" in self.documents


class DraftSummary(pyd.BaseModel):
    """Metadata for one server-side stored draft (item 47 phase 2) —
    everything the "my drafts" list needs, and nothing from the bundle's
    actual document content. `contains_connections` mirrors
    `ConfigChangeSetBundle.contains_connections` without requiring the
    caller's own encrypted content to be decrypted just to list it.
    """

    id: str
    description: Optional[str] = None
    created_at: datetime
    expires_at: datetime
    contains_connections: bool = False

    model_config = pyd.ConfigDict(extra="forbid")


class ConfigChangeSetImportCheck(pyd.BaseModel):
    """Result of validating an uploaded change-set bundle before staging.

    Content-free by construction — like `ConfigPreview`, it reports whether
    the resulting candidate is valid, a per-document change signal, and
    whether the target's active version has drifted from the bundle's base
    (`stale_base` + the specific documents that changed underneath), but never
    echoes YAML, credentials, or identifiers. The browser already holds the
    uploaded bundle locally, so it populates its editors from that rather than
    from this response.
    """

    valid: bool
    errors: List[str] = pyd.Field(default_factory=list)
    documents: List["ConfigDocumentPreview"] = pyd.Field(default_factory=list)
    # True when the active version's content differs from the fingerprint the
    # bundle was composed against — the caller is importing onto a moved base.
    stale_base: bool = False
    # Which base documents changed since the bundle was created (only
    # meaningful when stale_base is True). Content-free document names only.
    base_conflict_documents: List[Literal["connections", "policy", "catalog", "templates"]] = (
        pyd.Field(default_factory=list)
    )
    # True when the bundle includes a connections document (may carry a
    # literal credential) — a signal for the UI to warn and to keep it out of
    # browser storage, not a disclosure of the content itself.
    contains_connections: bool = False
    ready_to_stage: bool = False
    warnings: List[str] = pyd.Field(default_factory=list)

    model_config = pyd.ConfigDict(extra="forbid")


class TemplateSchemaCheck(pyd.BaseModel):
    """Result of checking one query template's referenced tables/columns against
    a live connection's reflected schema (the on-demand check the offline
    dry-run deliberately skips)."""

    template_id: str
    connection: str
    # ok: every table/column exists. issues: something is missing/invalid.
    # connection_unavailable: the target connection isn't a live, enabled
    # connection to reflect. unreachable: the database couldn't be reached, so
    # existence was not verified (best-effort, never a hard failure).
    # structural_error: the skeleton isn't a valid query (fix in the dry-run first).
    status: Literal["ok", "issues", "connection_unavailable", "unreachable", "structural_error"]
    messages: List[str] = pyd.Field(default_factory=list)

    model_config = pyd.ConfigDict(extra="forbid")


class TemplateSchemaCheckResult(pyd.BaseModel):
    """Per-template results of the on-demand live-schema check. `checked` is
    False when there are no templates to check or the document couldn't be
    parsed (see `note`); a per-connection reflection failure is reported as an
    `unreachable` result, not an error, so a down database never blocks staging."""

    checked: bool
    results: List[TemplateSchemaCheck] = pyd.Field(default_factory=list)
    note: Optional[str] = None

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


# Built FROM `Policy` rather than hand-listed (TODO.md item 115). The previous
# hand-written version had silently fallen nine caps behind — items 68-72's
# WHERE/CASE guardrails, 88's k-anonymity floor, 97's subquery depth, 100's
# expression bounds and 101's window bounds were all absent, so an operator
# asking "what are my limits" got an answer that omitted them. Generating the
# model keeps every field's type and optionality identical to Policy's own, so
# the two cannot disagree either. Widening a response model is additive:
# existing consumers are unaffected.
EffectiveGuardrails = pyd.create_model(
    "EffectiveGuardrails",
    __config__=pyd.ConfigDict(extra="forbid"),
    __doc__=(
        "Every scalar guardrail in force for a connection, derived from `Policy` "
        "so a newly added cap is reported here automatically."
    ),
    **{name: (Policy.model_fields[name].annotation, ...) for name in GUARDRAIL_FIELDS},
)


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
    # Blast-radius pagination cursor (TODO item 41 phase 2); ignored by /diff.
    # Page through configured principals when there are more than one page's
    # worth — use the response's `next_principal_offset` for the next request.
    principal_offset: int = pyd.Field(default=0, ge=0)

    model_config = pyd.ConfigDict(extra="forbid")


SemanticChangeCategory = Literal[
    "connection_visibility",
    "guardrail",
    "table_access",
    "column_access",
    "mandatory_filter",
    "column_mask",
    "join_group",
    "purpose_access",
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
    # Pagination cursor (TODO item 41 phase 2): the offset this page started at,
    # and the offset to request for the next page (None when the last page of
    # configured principals has been returned). `principals_evaluated` is this
    # page's size; `principals_configured` is the total across all pages.
    principal_offset: int = 0
    next_principal_offset: Optional[int] = None
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
