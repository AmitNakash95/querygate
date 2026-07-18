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
from typing import Literal, Optional

import pydantic as pyd


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
