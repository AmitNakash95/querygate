"""Connection profiles — the only place a real connection string is allowed to exist.

`ConnectionProfile` carries credentials and must never be returned from an
API/MCP response, logged, or embedded in a validation error. Anything
agent/client-facing uses `PublicConnectionInfo` instead.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Optional

import pydantic as pyd


class DatabaseDialect(StrEnum):
    """The only dialects `ConnectionRegistry` accepts — enforced everywhere
    a dialect is compared instead of a literal string (connections/dialects.py,
    compiler/sqlalchemy_compiler.py, execution/service.py). `StrEnum` (not a
    hand-rolled `str, Enum` mixin) so f-strings, `==` against a plain string,
    YAML/JSON round-tripping, and pydantic validation all behave exactly like
    a plain string — every existing `dialect == "postgresql"`-shaped
    comparison and log line keeps working unchanged, just without the magic
    string.

    SQLite is deliberately not a member: per CLAUDE.md, it's used internally
    for tests/examples by monkeypatching `connections.engine.get_engine`/
    `session_scope` directly, never through the registry — see
    `execution/service.py`'s `dialect` (derived from the live SQLAlchemy
    engine, not `ConnectionProfile.dialect`) for where that string can still
    legitimately be `"sqlite"` and stays a plain `str`.
    """

    POSTGRESQL = "postgresql"
    MSSQL = "mssql"


_VALID_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")


class ConnectionProfile(pyd.BaseModel):
    id: str
    dialect: DatabaseDialect
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
    dialect: DatabaseDialect
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
