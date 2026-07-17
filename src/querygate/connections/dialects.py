"""Dialect-specific engine URL building, connect args, and session guardrails.

Everything Postgres/MSSQL-specific lives in this one module (goal: "keep
dialect-specific code isolated") — adding a third dialect means extending the
three functions here, not touching connections/engine.py or execution code.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from querygate.connections.models import ConnectionProfile
from querygate.core.config import config as app_config


def build_engine_url(profile: ConnectionProfile) -> str:
    if profile.dialect == "postgresql":
        return profile.connection_string
    if profile.dialect == "mssql":
        cert = "&TrustServerCertificate=Yes" if app_config.db_trust_server_certificate else ""
        return f"{profile.connection_string}?driver={app_config.odbc_driver}{cert}"
    raise ValueError(f"Unsupported dialect: {profile.dialect}")


def build_connect_args(profile: ConnectionProfile, timeout_seconds: int) -> dict:
    if profile.dialect == "mssql":
        # pyodbc query-execution timeout (SQL_ATTR_QUERY_TIMEOUT), not just a
        # connect timeout — this engine is read-only, so bounding every
        # statement is safe.
        return {"timeout": timeout_seconds}
    return {}


async def apply_session_guardrails(
    session: AsyncSession,
    dialect: str,
    *,
    lock_timeout_seconds: int,
    statement_timeout_seconds: int,
) -> None:
    if dialect == "postgresql":
        await session.execute(sa.text(f"SET LOCAL lock_timeout = '{lock_timeout_seconds}s'"))
        await session.execute(
            sa.text(f"SET LOCAL statement_timeout = '{statement_timeout_seconds}s'")
        )
    elif dialect == "mssql":
        # LOCK_TIMEOUT is in milliseconds.
        await session.execute(sa.text(f"SET LOCK_TIMEOUT {lock_timeout_seconds * 1000}"))
        await session.execute(sa.text("SET XACT_ABORT ON"))
        await session.execute(sa.text("SET DEADLOCK_PRIORITY LOW"))
