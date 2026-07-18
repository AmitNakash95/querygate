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
from typing import Optional

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

    # Full YAML content, never resolved — a version's connection strings are
    # always ${...} references (see querygate/secrets/), never literal
    # secret values, so persisting this content carries no credential.
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
