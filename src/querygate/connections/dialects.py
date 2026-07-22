"""Dialect-specific engine URL building, connect args, and session guardrails.

Everything Postgres/MSSQL-specific lives in this one module (goal: "keep
dialect-specific code isolated") — adding a third dialect means extending the
three functions here, not touching connections/engine.py or execution code.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from querygate.connections.models import ConnectionProfile, DatabaseDialect
from querygate.core.config import config as app_config


def build_engine_url(profile: ConnectionProfile) -> str:
    if profile.dialect == DatabaseDialect.POSTGRESQL:
        return profile.connection_string
    if profile.dialect == DatabaseDialect.MSSQL:
        cert = "&TrustServerCertificate=Yes" if app_config.db_trust_server_certificate else ""
        return f"{profile.connection_string}?driver={app_config.odbc_driver}{cert}"
    raise ValueError(f"Unsupported dialect: {profile.dialect}")


def build_connect_args(profile: ConnectionProfile, timeout_seconds: int) -> dict:
    if profile.dialect == DatabaseDialect.MSSQL:
        # This is pyodbc's *login* timeout (SQL_ATTR_LOGIN_TIMEOUT) — despite
        # its name, `timeout=` in pyodbc.connect() does NOT bound query
        # execution (confirmed against a live server: a WAITFOR DELAY well
        # past this value ran to completion uncancelled — TODO.md item 3).
        # Real query-execution timeout is register_query_timeout() below,
        # which has to be set as a post-connect attribute, not a connect
        # kwarg. Kept here anyway — bounding how long a connection attempt
        # itself can hang is still a real, separate guardrail worth having.
        return {"timeout": timeout_seconds}
    return {}


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


def register_query_timeout(
    engine: AsyncEngine, dialect: DatabaseDialect, timeout_seconds: int
) -> None:
    """Bind pyodbc's actual query-execution timeout (SQL_ATTR_QUERY_TIMEOUT).

    Must be set as an attribute on the raw DBAPI connection after it's
    opened — pyodbc has no connect-time keyword for it, unlike login
    timeout (see build_connect_args). Hooks SQLAlchemy's pool `connect`
    event so every physical connection this engine ever opens (not just the
    first) gets it applied, since the pool can silently create new
    connections later (e.g. after one is recycled or dropped).
    """
    if dialect != DatabaseDialect.MSSQL:
        return

    @event.listens_for(engine.sync_engine, "connect")
    def _set_query_timeout(dbapi_connection, connection_record) -> None:
        _raw_pyodbc_connection(dbapi_connection).timeout = timeout_seconds


async def apply_session_guardrails(
    session: AsyncSession,
    dialect: DatabaseDialect,
    *,
    lock_timeout_seconds: int,
    statement_timeout_seconds: int,
) -> None:
    # The interpolated values are Pydantic-validated ints from Policy
    # (timeout_seconds), never caller input, and `SET LOCAL` / `SET LOCK_TIMEOUT`
    # take no bind parameters — string interpolation is required here, not a SQL
    # injection vector. Semgrep's avoid-sqlalchemy-text rule is suppressed inline
    # per-site with that justification (reviewed 2026-07-22, TODO item 89). The
    # `# fmt: off` region keeps each `# nosemgrep` on the same line as its
    # `sa.text(...)` match, which Black would otherwise split apart.
    # fmt: off
    if dialect == DatabaseDialect.POSTGRESQL:
        await session.execute(sa.text(f"SET LOCAL lock_timeout = '{lock_timeout_seconds}s'"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        await session.execute(sa.text(f"SET LOCAL statement_timeout = '{statement_timeout_seconds}s'"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
    elif dialect == DatabaseDialect.MSSQL:
        # LOCK_TIMEOUT is in milliseconds.
        await session.execute(sa.text(f"SET LOCK_TIMEOUT {lock_timeout_seconds * 1000}"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        await session.execute(sa.text("SET XACT_ABORT ON"))
        await session.execute(sa.text("SET DEADLOCK_PRIORITY LOW"))
    # fmt: on
