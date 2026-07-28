"""Dialect-specific engine URL building, connect args, and session guardrails.

Formalized as a `SessionDialectAdapter` interface (TODO.md item 57): one concrete
adapter class per dialect, dispatched through the `_SESSION_ADAPTERS` registry —
the engine/session-layer sibling of `compiler/dialect_adapters.py`'s (sync)
`DialectAdapter`. The two are kept as **separate** abstract bases on purpose: this
one is async (it executes `SET ...` on a live session and hooks pool events),
that one is sync (it builds SQL expressions); merging async session execution and
sync SQL-building into one interface would be awkward. Adding a dialect means
implementing both adapters and registering them — never adding an
`if dialect == ...` branch at a call site.

The module-level `build_engine_url` / `build_connect_args` /
`register_query_timeout` / `apply_session_guardrails` functions are preserved as
thin dispatchers so existing callers (`connections/engine.py`) are unchanged;
all the dialect-specific behavior now lives in the adapter classes below.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from querygate.connections.models import ConnectionProfile, DatabaseDialect
from querygate.core.config import config as app_config


def _raw_pyodbc_connection(dbapi_connection):
    """Unwrap SQLAlchemy's async adapter to the real pyodbc connection.

    Confirmed against a live server: `engine.sync_engine`'s `connect` event
    hands us `AsyncAdapt_aioodbc_connection` (aioodbc), not a raw pyodbc
    connection — it has no `.timeout` attribute of its own. The actual
    pyodbc connection is nested at `._connection._conn` (mirroring how
    SQLAlchemy's own aioodbc adapter reaches it for its `autocommit`
    setter — see sqlalchemy.connectors.aioodbc). Falls back to the object
    itself for a hypothetical sync pyodbc engine, where it would already be
    the raw connection.
    """
    inner = getattr(dbapi_connection, "_connection", None)
    return getattr(inner, "_conn", None) or inner or dbapi_connection


class SessionDialectAdapter(ABC):
    """Per-dialect engine/session behavior: how to build the engine URL and
    connect args, register a query-execution timeout, and apply per-session
    guardrails. One concrete class per dialect, dispatched via
    `get_session_adapter` — no inline `if dialect == ...` branching."""

    @abstractmethod
    def build_engine_url(self, profile: ConnectionProfile) -> str: ...

    @abstractmethod
    def build_connect_args(self, profile: ConnectionProfile, timeout_seconds: int) -> dict: ...

    @abstractmethod
    def register_query_timeout(self, engine: AsyncEngine, timeout_seconds: int) -> None: ...

    @abstractmethod
    async def apply_session_guardrails(
        self,
        session: AsyncSession,
        *,
        lock_timeout_seconds: int,
        statement_timeout_seconds: int,
    ) -> None: ...

    @abstractmethod
    async def capture_session_identifier(self, session: AsyncSession) -> str:
        """A value identifying this live session's server-side process/backend
        (Postgres backend PID, MSSQL `@@SPID`), captured once right after
        connect so a LATER, separate connection can target it for real
        dialect-level cancellation (TODO.md item 35 phase 3). Not a security
        boundary itself — `cancel_session` below is gated by
        `Policy.allow_query_cancellation`, not by this method existing."""
        ...

    @abstractmethod
    async def cancel_session(self, engine: AsyncEngine, identifier: str) -> None:
        """Cancel the query running on the session `identifier` names, from a
        NEW connection on `engine` — never the session being cancelled itself
        (it's presumably blocked executing the query this call is meant to
        stop). Each dialect gets the primitive it actually has, mechanically
        translated, not a synthesized "graceful cancel" a dialect doesn't
        offer out-of-band (2026-07-28 Decision Log): Postgres's
        `pg_cancel_backend` interrupts just the query and leaves the pooled
        connection alive; MSSQL's `KILL` is the only out-of-band primitive
        T-SQL exposes for this and terminates the whole session."""
        ...


class PostgresSessionAdapter(SessionDialectAdapter):
    def build_engine_url(self, profile: ConnectionProfile) -> str:
        return profile.connection_string

    def build_connect_args(self, profile: ConnectionProfile, timeout_seconds: int) -> dict:
        return {}

    def register_query_timeout(self, engine: AsyncEngine, timeout_seconds: int) -> None:
        # Postgres bounds execution via `SET LOCAL statement_timeout` in
        # apply_session_guardrails — no per-connection attribute to register.
        return None

    async def apply_session_guardrails(
        self,
        session: AsyncSession,
        *,
        lock_timeout_seconds: int,
        statement_timeout_seconds: int,
    ) -> None:
        # The interpolated values are Pydantic-validated ints from Policy
        # (timeout_seconds), never caller input, and `SET LOCAL` takes no bind
        # parameters — string interpolation is required here, not a SQL injection
        # vector. Semgrep's avoid-sqlalchemy-text rule is suppressed inline
        # per-site with that justification (reviewed 2026-07-22, TODO item 89).
        # fmt: off
        await session.execute(sa.text(f"SET LOCAL lock_timeout = '{lock_timeout_seconds}s'"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        await session.execute(sa.text(f"SET LOCAL statement_timeout = '{statement_timeout_seconds}s'"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        # fmt: on
        # UTC, deliberately (TODO.md item 102, 2026-07-26 Decision Log). Postgres
        # resolves `EXTRACT`, `date_trunc` and every timestamp<->timestamptz
        # conversion against the SESSION TimeZone, so without this pin the value
        # QueryGate returns for `extract(hour from a timestamptz)` or a
        # `date_bucket` depends on the server's configured zone rather than on
        # the query — the same input yielding different answers on two
        # deployments, invisible to any test that only inspects generated SQL.
        # MSSQL has no session time zone to set (its adapter reads the clock via
        # SYSUTCDATETIME instead) and SQLite's 'now' is already UTC, so this is
        # what makes all three dialects agree. Fixed literal, no interpolation.
        await session.execute(sa.text("SET LOCAL TIME ZONE 'UTC'"))

    async def capture_session_identifier(self, session: AsyncSession) -> str:
        result = await session.execute(sa.text("SELECT pg_backend_pid()"))
        return str(result.scalar_one())

    async def cancel_session(self, engine: AsyncEngine, identifier: str) -> None:
        # A bind parameter, unlike apply_session_guardrails's SET LOCAL
        # (which takes none) — pg_cancel_backend is an ordinary function call.
        # A NEW connection: `identifier`'s own session is presumably blocked
        # executing the query this call means to interrupt.
        async with engine.connect() as conn:
            await conn.execute(sa.text("SELECT pg_cancel_backend(:pid)"), {"pid": int(identifier)})


class MSSQLSessionAdapter(SessionDialectAdapter):
    def build_engine_url(self, profile: ConnectionProfile) -> str:
        cert = "&TrustServerCertificate=Yes" if app_config.db_trust_server_certificate else ""
        return f"{profile.connection_string}?driver={app_config.odbc_driver}{cert}"

    def build_connect_args(self, profile: ConnectionProfile, timeout_seconds: int) -> dict:
        # This is pyodbc's *login* timeout (SQL_ATTR_LOGIN_TIMEOUT) — despite
        # its name, `timeout=` in pyodbc.connect() does NOT bound query
        # execution (confirmed against a live server: a WAITFOR DELAY well
        # past this value ran to completion uncancelled — TODO.md item 3).
        # Real query-execution timeout is register_query_timeout below, which
        # has to be set as a post-connect attribute, not a connect kwarg. Kept
        # here anyway — bounding how long a connection attempt itself can hang
        # is still a real, separate guardrail worth having.
        return {"timeout": timeout_seconds}

    def register_query_timeout(self, engine: AsyncEngine, timeout_seconds: int) -> None:
        """Bind pyodbc's actual query-execution timeout (SQL_ATTR_QUERY_TIMEOUT).

        Must be set as an attribute on the raw DBAPI connection after it's
        opened — pyodbc has no connect-time keyword for it, unlike login timeout
        (see build_connect_args). Hooks SQLAlchemy's pool `connect` event so
        every physical connection this engine ever opens (not just the first)
        gets it applied, since the pool can silently create new connections
        later (e.g. after one is recycled or dropped).
        """

        @event.listens_for(engine.sync_engine, "connect")
        def _set_query_timeout(dbapi_connection, connection_record) -> None:
            _raw_pyodbc_connection(dbapi_connection).timeout = timeout_seconds

    async def apply_session_guardrails(
        self,
        session: AsyncSession,
        *,
        lock_timeout_seconds: int,
        statement_timeout_seconds: int,
    ) -> None:
        # See PostgresSessionAdapter for the interpolation/nosemgrep rationale.
        # fmt: off
        # LOCK_TIMEOUT is in milliseconds.
        await session.execute(sa.text(f"SET LOCK_TIMEOUT {lock_timeout_seconds * 1000}"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        await session.execute(sa.text("SET XACT_ABORT ON"))
        await session.execute(sa.text("SET DEADLOCK_PRIORITY LOW"))
        # fmt: on

    async def capture_session_identifier(self, session: AsyncSession) -> str:
        result = await session.execute(sa.text("SELECT @@SPID"))
        return str(result.scalar_one())

    async def cancel_session(self, engine: AsyncEngine, identifier: str) -> None:
        # KILL takes a literal SPID, not a bind parameter — T-SQL has no
        # parameterized form. `identifier` is a driver-returned integer this
        # adapter captured itself (never caller input), and `int(...)` below
        # both validates that and is what makes the subsequent interpolation
        # safe — the same "not a SQL injection vector" justification
        # apply_session_guardrails's SET LOCK_TIMEOUT already documents.
        spid = int(identifier)
        async with engine.connect() as conn:
            await conn.execute(
                sa.text(f"KILL {spid}")
            )  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text


_SESSION_ADAPTERS: Dict[DatabaseDialect, SessionDialectAdapter] = {
    DatabaseDialect.POSTGRESQL: PostgresSessionAdapter(),
    DatabaseDialect.MSSQL: MSSQLSessionAdapter(),
}


def get_session_adapter(dialect: DatabaseDialect) -> SessionDialectAdapter:
    adapter = _SESSION_ADAPTERS.get(dialect)
    if adapter is None:
        raise ValueError(f"Unsupported dialect: {dialect}")
    return adapter


# --- Thin dispatchers (preserve the module's public API for existing callers) ---


def build_engine_url(profile: ConnectionProfile) -> str:
    return get_session_adapter(profile.dialect).build_engine_url(profile)


def build_connect_args(profile: ConnectionProfile, timeout_seconds: int) -> dict:
    return get_session_adapter(profile.dialect).build_connect_args(profile, timeout_seconds)


def register_query_timeout(
    engine: AsyncEngine, dialect: DatabaseDialect, timeout_seconds: int
) -> None:
    get_session_adapter(dialect).register_query_timeout(engine, timeout_seconds)


async def apply_session_guardrails(
    session: AsyncSession,
    dialect: DatabaseDialect,
    *,
    lock_timeout_seconds: int,
    statement_timeout_seconds: int,
) -> None:
    await get_session_adapter(dialect).apply_session_guardrails(
        session,
        lock_timeout_seconds=lock_timeout_seconds,
        statement_timeout_seconds=statement_timeout_seconds,
    )


async def capture_session_identifier(session: AsyncSession, dialect: DatabaseDialect) -> str:
    return await get_session_adapter(dialect).capture_session_identifier(session)


async def cancel_session(engine: AsyncEngine, dialect: DatabaseDialect, identifier: str) -> None:
    await get_session_adapter(dialect).cancel_session(engine, identifier)
