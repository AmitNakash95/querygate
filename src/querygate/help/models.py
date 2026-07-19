"""Public response models for the QueryGate product guide.

The guide deliberately has two families of models: static product knowledge
and caller/deployment-aware summaries. Keeping them structurally separate
makes it difficult to accidentally put private deployment state into the
globally cached/searchable documentation corpus.
"""

from __future__ import annotations

from typing import Dict, List, Literal, Optional, Union

import pydantic as pyd

from querygate.connections.models import PublicConnectionInfo


class GuideCitation(pyd.BaseModel):
    source: str
    topic_id: str
    version: str

    model_config = pyd.ConfigDict(extra="forbid")


class GuideTopic(pyd.BaseModel):
    """One canonical, packaged guide topic."""

    id: str
    title: str
    summary: str
    body: str
    tags: List[str] = pyd.Field(default_factory=list)
    next_actions: List[str] = pyd.Field(default_factory=list)
    source: str

    model_config = pyd.ConfigDict(extra="forbid")


class GuideSearchHit(pyd.BaseModel):
    topic_id: str
    title: str
    summary: str
    score: float
    citation: GuideCitation
    next_actions: List[str] = pyd.Field(default_factory=list)

    model_config = pyd.ConfigDict(extra="forbid")


class GuideSearchResponse(pyd.BaseModel):
    querygate_version: str
    query: str
    results: List[GuideSearchHit]

    model_config = pyd.ConfigDict(extra="forbid")


class GuideTopicResponse(pyd.BaseModel):
    querygate_version: str
    topic_id: str
    title: str
    summary: str
    content: str
    citation: GuideCitation
    next_actions: List[str] = pyd.Field(default_factory=list)

    model_config = pyd.ConfigDict(extra="forbid")


class SetupStep(pyd.BaseModel):
    number: int
    title: str
    instruction: str
    citation: GuideCitation

    model_config = pyd.ConfigDict(extra="forbid")


class SetupChecklistResponse(pyd.BaseModel):
    querygate_version: str
    profile: Literal["local", "container", "production"]
    steps: List[SetupStep]

    model_config = pyd.ConfigDict(extra="forbid")


class ConfigFieldExplanation(pyd.BaseModel):
    querygate_version: str
    model: str
    field: str
    type: str
    required: bool
    default: Optional[object] = None
    description: str
    citation: GuideCitation

    model_config = pyd.ConfigDict(extra="forbid")


class CallerCapabilities(pyd.BaseModel):
    query_visible_connections: bool
    read_configuration: bool
    change_configuration: bool
    reload_configuration: bool

    model_config = pyd.ConfigDict(extra="forbid")


class AccessSummary(pyd.BaseModel):
    querygate_version: str
    principal: str
    auth_method: str
    scopes: List[str]
    capabilities: CallerCapabilities
    visible_connections: List[PublicConnectionInfo]
    guidance: str
    citation: GuideCitation

    model_config = pyd.ConfigDict(extra="forbid")


class ConfigVersionSummary(pyd.BaseModel):
    id: str
    status: str
    created_at: str
    created_by: str
    description: Optional[str] = None
    applied_at: Optional[str] = None
    applied_by: Optional[str] = None
    previous_active_version_id: Optional[str] = None

    model_config = pyd.ConfigDict(extra="forbid")


class RedactedConnection(pyd.BaseModel):
    id: str
    dialect: str
    enabled: bool
    description: Optional[str] = None
    credential_source: Literal["environment", "external_secret", "configured"]
    credential_configured: bool = True
    known_table_count: int = 0
    join_group_configured: bool = False

    model_config = pyd.ConfigDict(extra="forbid")


class PolicyRuleCounts(pyd.BaseModel):
    allowed_tables: int = 0
    denied_tables: int = 0
    allowed_column_groups: int = 0
    denied_column_groups: int = 0
    mandatory_row_filters: int = 0

    model_config = pyd.ConfigDict(extra="forbid")


GuardrailValue = Union[int, float, bool, str, None]


class RedactedPolicySummary(pyd.BaseModel):
    enabled: bool
    rule_counts: PolicyRuleCounts
    guardrails: Dict[str, GuardrailValue]
    join_group_configured: bool = False

    model_config = pyd.ConfigDict(extra="forbid")


class RedactedPolicyConfiguration(pyd.BaseModel):
    default: RedactedPolicySummary
    connection_overrides: Dict[str, RedactedPolicySummary]
    principal_override_count: int

    model_config = pyd.ConfigDict(extra="forbid")


class RedactedCatalogSummary(pyd.BaseModel):
    configured: bool
    version: Optional[int] = None
    connection_count: int = 0
    table_count: int = 0
    column_count: int = 0

    model_config = pyd.ConfigDict(extra="forbid")


class RedactedConfiguration(pyd.BaseModel):
    querygate_version: str
    version: ConfigVersionSummary
    connections: List[RedactedConnection]
    policy: RedactedPolicyConfiguration
    catalog: RedactedCatalogSummary
    redactions: List[str]
    citation: GuideCitation

    model_config = pyd.ConfigDict(extra="forbid")


class ErrorExplanation(pyd.BaseModel):
    querygate_version: str
    error_code: str
    title: str
    explanation: str
    next_actions: List[str]
    citation: GuideCitation

    model_config = pyd.ConfigDict(extra="forbid")
