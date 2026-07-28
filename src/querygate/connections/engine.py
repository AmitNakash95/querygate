"""Async engine/session lifecycle, keyed by connection id.

Engines, sessionmakers, and reflection metadata are process-wide singletons
per connection id (mirrors SQLAlchemy's own "one engine per database"
guidance) — created lazily on first use so an unreachable/unconfigured
connection only fails when something actually queries it.
"""

from __future__ import annotations

import contextlib
from typing import AsyncGenerator, Callable, Optional

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from querygate.connections.dialects import (
    apply_session_guardrails,
    build_connect_args,
    build_engine_url,
    capture_session_identifier,
    register_query_timeout,
)
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import get_registry
from querygate.core.config import config as app_config
from querygate.policy.models import Policy

ENGINES: dict[str, AsyncEngine] = {}
SESSIONMAKERS: dict[str, async_sessionmaker] = {}
METADATAS: dict[str, sa.MetaData] = {}


def _profile(connection_id: str) -> ConnectionProfile:
    return get_registry().get(connection_id)


def physical_db_name(connection_id: str) -> str:
    """Real database name parsed from the connection string — used to build
    same-instance cross-database schema qualifiers for cross-connection joins
    (see validation/schema_validation.py).
    """
    return sa.engine.url.make_url(_profile(connection_id).connection_string).database


def init_engine(connection_id: str) -> AsyncEngine:
    from querygate.policy.loader import get_policy

    profile = _profile(connection_id)
    if not profile.connection_string:
        raise ValueError(f"Connection string for {connection_id!r} is not set.")
    policy = get_policy(connection_id)
    engine = create_async_engine(
        url=build_engine_url(profile),
        pool_size=app_config.pool_size,
        max_overflow=app_config.conn_max_overflow,
        pool_timeout=app_config.pool_timeout,
        pool_recycle=app_config.pool_recycle,
        pool_pre_ping=True,
        isolation_level="READ COMMITTED",
        connect_args=build_connect_args(profile, policy.timeout_seconds),
    )
    register_query_timeout(engine, profile.dialect, policy.timeout_seconds)
    return engine


def get_engine(connection_id: str) -> AsyncEngine:
    if connection_id not in ENGINES:
        ENGINES[connection_id] = init_engine(connection_id)
    return ENGINES[connection_id]


def get_sessionmaker(connection_id: str) -> async_sessionmaker:
    if connection_id not in SESSIONMAKERS:
        SESSIONMAKERS[connection_id] = async_sessionmaker(
            get_engine(connection_id), class_=AsyncSession, expire_on_commit=False
        )
    return SESSIONMAKERS[connection_id]


def get_metadata(connection_id: str) -> sa.MetaData:
    if connection_id not in METADATAS:
        METADATAS[connection_id] = sa.MetaData()
    return METADATAS[connection_id]


@contextlib.asynccontextmanager
async def session_scope(
    connection_id: str,
    policy: Optional[Policy] = None,
    *,
    session_identifier_sink: Optional[Callable[[str], None]] = None,
) -> AsyncGenerator[AsyncSession, None]:
    """`session_identifier_sink`, when given, is called once with this
    session's dialect-captured backend/process identifier (TODO.md item 35
    phase 3) — right after guardrails, before the caller's query ever runs —
    so a later, separate cancellation call can target it. Skipped by default
    (an extra round-trip on every query otherwise): only the async execution
    lifecycle that can actually be cancelled passes this.
    """
    profile = _profile(connection_id)
    if policy is None:
        from querygate.policy.loader import get_policy

        policy = get_policy(connection_id)
    async with get_sessionmaker(connection_id)() as session:
        try:
            await session.begin()
            await apply_session_guardrails(
                session,
                profile.dialect,
                lock_timeout_seconds=min(policy.timeout_seconds, 30),
                statement_timeout_seconds=policy.timeout_seconds,
            )
            if session_identifier_sink is not None:
                identifier = await capture_session_identifier(session, profile.dialect)
                session_identifier_sink(identifier)
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


def reset_engines() -> None:
    """Drop all cached engines/sessionmakers/metadata — used by tests."""
    ENGINES.clear()
    SESSIONMAKERS.clear()
    METADATAS.clear()


async def dispose_engine(connection_id: str) -> None:
    """Tear down one connection's cached engine (config reload — see
    querygate/config_reload.py). A request already holding a checked-out
    connection from this engine isn't interrupted: `AsyncEngine.dispose()`
    only closes idle pooled connections; a connection currently checked out
    finishes its work normally and is then discarded rather than returned to
    the (now-disposed) pool for reuse. The next `get_engine()` call lazily
    creates a fresh engine, same as at startup.
    """
    engine = ENGINES.pop(connection_id, None)
    SESSIONMAKERS.pop(connection_id, None)
    METADATAS.pop(connection_id, None)
    if engine is not None:
        await engine.dispose()
