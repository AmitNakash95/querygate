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
import sqlalchemy as sa


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

    `SNOWFLAKE` (TODO.md item 19 phase 2) is accepted here and has a real
    `SnowflakeDialectAdapter`/`SnowflakeSessionAdapter`, but — unlike the other
    three members — `connections/engine.py`'s `init_engine` refuses to actually
    open a connection for it: `snowflake-sqlalchemy`'s DBAPI has no async
    driver, so `create_async_engine` cannot be used the way it is for every
    other dialect here. This member exists so the compiler/session adapters
    can be built and rendering-tested now; see `init_engine`'s docstring and
    TODO.md item 19 for the live-async-execution gap that blocks actually
    running a query against Snowflake.
    """

    POSTGRESQL = "postgresql"
    MSSQL = "mssql"
    MYSQL = "mysql"
    SNOWFLAKE = "snowflake"


_VALID_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")

# `sqlalchemy.engine.url.URL.get_backend_name()` happens to return exactly
# these four strings for every driver combination this codebase uses
# (postgresql+asyncpg -> "postgresql", mssql+aioodbc -> "mssql",
# mysql+asyncmy/mysql+aiomysql -> "mysql", snowflake -> "snowflake" —
# verified directly against `make_url` rather than assumed), i.e. identical
# to `DatabaseDialect`'s own values. This mapping is spelled out explicitly
# anyway (item 158) rather than doing `DatabaseDialect(backend_name)`
# directly, so a backend name SQLAlchemy resolves that QueryGate doesn't
# support at all (e.g. "oracle", "sqlite") fails with this module's own
# clear error instead of a bare `ValueError: 'oracle' is not a valid
# DatabaseDialect`.
_BACKEND_NAME_TO_DIALECT: dict[str, "DatabaseDialect"] = {
    "postgresql": DatabaseDialect.POSTGRESQL,
    "mssql": DatabaseDialect.MSSQL,
    "mysql": DatabaseDialect.MYSQL,
    "snowflake": DatabaseDialect.SNOWFLAKE,
}

# Drift guard: this dict is maintained by hand (deliberately, so an
# unsupported-but-SQLAlchemy-known backend like "oracle"/"sqlite" gets this
# module's own clear rejection instead of a bare enum ValueError — see the
# comment above), which means nothing else forces it to stay a superset of
# `DatabaseDialect`. Fail loudly at import time, not by silently mis-
# rejecting every profile for a newly-added dialect at request time.
assert set(DatabaseDialect) <= set(_BACKEND_NAME_TO_DIALECT.values()), (
    "_BACKEND_NAME_TO_DIALECT is missing an entry for a DatabaseDialect member "
    "— add its make_url() backend name before shipping the new dialect."
)


class ConnectionProfile(pyd.BaseModel):
    id: str
    # `connection_string` is declared before `dialect` deliberately: the
    # `_dialect_matches_connection_string` validator below is a
    # `field_validator("dialect")` (not a whole-model validator) precisely so
    # pydantic's `ValidationError` scopes its `input_value` to `dialect`'s
    # own (credential-free) value rather than the whole input dict — see
    # that validator's docstring. `info.data` only contains fields validated
    # *before* the one currently being validated, so `connection_string`
    # must come first for that lookup to see it.
    connection_string: str = pyd.Field(repr=False)
    dialect: DatabaseDialect
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

    @pyd.field_validator("dialect")
    @classmethod
    def _dialect_matches_connection_string(
        cls, value: DatabaseDialect, info: pyd.ValidationInfo
    ) -> DatabaseDialect:
        """TODO.md item 158: nothing previously checked that the declared
        `dialect` agrees with the backend actually named in
        `connection_string`'s URL scheme. A mismatch (e.g. `dialect:
        postgresql` over a `mysql+asyncmy://...` string) used to reach
        `connections/engine.py` untouched — `session_scope` then applies the
        *declared* dialect's session guardrails to the real (URL-resolved)
        backend, so the wrong guardrail SQL text gets sent — and it also
        meant `SessionDialectAdapter.is_connectable()`'s Snowflake guard
        (which also keys on the declared dialect) couldn't be trusted to
        catch a misdeclared Snowflake connection either. Checked here, once,
        rather than teaching `init_engine` a second, narrower parse-based
        check.

        Deliberately a `field_validator("dialect")`, not a whole-model
        `model_validator` — this class's own docstring is explicit that
        `connection_string` must never end up "embedded in a validation
        error", and pydantic v2 populates a raised `ValueError`'s
        `input_value` from whatever the validator is scoped to: a
        model-level validator dumps the *entire* input dict (confirmed
        directly — `connection_string`, credential and all, appears in both
        `str(ValidationError)` and `.errors()[0]["input"]`), and even a
        `field_validator("connection_string")` would set `input_value` to
        the connection string itself, since that field's own raw value IS
        the credential. Scoping to `dialect` instead means `input_value` is
        always just the declared dialect string (e.g. `"postgresql"`) — see
        `api/routes.py`'s `reload_config_endpoint`, which stringifies any
        exception straight into an HTTP 400 `detail`, for a concrete path
        this would otherwise leak through.

        `connection_string` can still be an entirely-unresolved `${ENV_VAR}`
        placeholder when this runs — confirmed against real usage, not
        assumed: `examples/connections.example.yaml`'s own entries (the
        default `AppConfig.connections_file`) write the *whole* value as a
        single `${QUERYGATE_DEMO_DB_URL}`-shaped reference, no literal
        scheme at all, and `help/service.py`'s `_redacted_connection`
        deliberately constructs a `ConnectionProfile` straight from a stored
        config version's raw, never-interpolated YAML text (it only wants
        typed access to `dialect`/`id`/`known_tables`/`join_group` for an
        admin summary — it never needs, and never resolves, the real
        secret). A `connection_string` in that shape fails to parse as a URL
        at all, so it's indistinguishable here from a typo — in both cases
        this validator can't determine an actual backend to compare against,
        so it skips the check rather than rejecting. This isn't a live
        bypass: the file-driven path (`ConnectionRegistry.from_file`/
        `from_entries`, used by `connections/engine.py`'s `init_engine`,
        `config_reload.py`, and `cli.py`'s config validation — every route
        that can actually open a connection) always interpolates
        `connection_string` before constructing `ConnectionProfile`, so by
        the time a real engine gets built, this check runs against the
        fully-resolved URL, same as any other mismatch. (A caller that
        constructs `ConnectionRegistry` directly from an already-built
        `dict[str, ConnectionProfile]` — `catalog/
        adaptive_learning_benchmark.py` and test fixtures — bypasses
        `from_entries` entirely, but every `ConnectionProfile` it holds was
        still fully validated at construction time; there's no unvalidated
        profile anywhere in this registry, interpolated or not.)
        """
        connection_string = info.data.get("connection_string")
        if connection_string is None:
            # connection_string itself failed validation (or is missing) —
            # pydantic already raises its own error for that field; nothing
            # more to check here.
            return value
        try:
            backend_name = sa.engine.url.make_url(connection_string).get_backend_name()
        except (sa.exc.ArgumentError, ValueError):
            # Can't parse a backend out of it at all (typically a still-
            # templated `${VAR}` placeholder standing in for the whole
            # string, not just its credentials — see docstring above) — no
            # basis to assert a mismatch either way. `make_url` raises
            # `sa.exc.ArgumentError` when the string doesn't look like a URL
            # at all, but a plain `ValueError` when it parses far enough to
            # try coercing a component (confirmed directly: a templated
            # port, e.g. `...@${DB_HOST}:${DB_PORT}/...`, fails with
            # `ValueError: invalid literal for int() with base 10:
            # '${DB_PORT}'` — the same "still templated" shape as a
            # templated user/pass/host, just caught differently inside
            # SQLAlchemy). Both mean the same thing here: no backend to
            # compare against, so skip rather than reject.
            return value
        actual_dialect = _BACKEND_NAME_TO_DIALECT.get(backend_name)
        if actual_dialect is None:
            raise ValueError(
                f"connection_string's backend {backend_name!r} is not a dialect "
                f"QueryGate supports (declared dialect: {value.value!r})."
            )
        if actual_dialect != value:
            raise ValueError(
                f"declared dialect {value.value!r} does not match connection_string's "
                f"actual backend {actual_dialect.value!r} — check for a copy-paste error "
                "in either the dialect or the connection string."
            )
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
