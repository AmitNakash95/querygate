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
