"""Connection profiles — the only place a real connection string is allowed to exist.

`ConnectionProfile` carries credentials and must never be returned from an
API/MCP response, logged, or embedded in a validation error. Anything
agent/client-facing uses `PublicConnectionInfo` instead.
"""

from __future__ import annotations

import re
from typing import Literal, Optional

import pydantic as pyd

Dialect = Literal["postgresql", "mssql"]

_VALID_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")


class ConnectionProfile(pyd.BaseModel):
    id: str
    dialect: Dialect
    connection_string: str = pyd.Field(repr=False)
    enabled: bool = True
    description: Optional[str] = None
    known_tables: list[str] = pyd.Field(
        default_factory=list,
        description="Optional seed table list so list_tables() has a real catalog "
        "before anything has been reflected. Falls back to a live schema query if empty.",
    )
    join_group: Optional[str] = pyd.Field(
        default=None,
        description="Connections sharing a join_group may be joined cross-connection "
        "in one query. Defaults to this connection's own id (no cross-connection joins).",
    )

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not _VALID_ID.match(value):
            raise ValueError(f"Invalid connection id: {value!r}")
        return value

    def effective_join_group(self) -> str:
        return self.join_group or self.id


class PublicConnectionInfo(pyd.BaseModel):
    """Credential-free projection of a ConnectionProfile — safe for MCP/REST responses."""

    id: str
    dialect: Dialect
    enabled: bool
    description: Optional[str] = None

    model_config = pyd.ConfigDict(extra="forbid")

    @classmethod
    def from_profile(cls, profile: ConnectionProfile) -> "PublicConnectionInfo":
        return cls(
            id=profile.id,
            dialect=profile.dialect,
            enabled=profile.enabled,
            description=profile.description,
        )
