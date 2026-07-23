"""Shared fixtures for integration tests.

`sqlite_app` stands up a real FastAPI app wired to a real (in-memory
SQLite) database on the "demo" connection, seeded from
`examples/demo_db/schema.py` — the same seed data used by
`examples/demo_db/seed.py` and `examples/demo_db/init_postgres.sql`. SQLite
stands in for Postgres/MSSQL here since no real server is available in this
environment; see `test_sqlite_end_to_end.py`'s module docstring for the
scope of what this does and doesn't verify.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from examples.demo_db.schema import create_and_seed_async
from querygate.api.app import create_app
from querygate.core.config import AppConfig


@pytest_asyncio.fixture
async def sqlite_app(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    await create_and_seed_async(engine)

    import querygate.execution.service as svc_module
    import querygate.execution.write_execution as wx_module
    import querygate.execution.write_preview as wp_module
    import querygate.validation.schema_validation as sv_module

    @asynccontextmanager
    async def _session_scope(connection_id, policy=None):
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    # Swap the real engine in at the seams that would otherwise build a
    # Postgres/MSSQL engine from the "demo" connection profile. Session
    # guardrails (Postgres/MSSQL-only SQL) are bypassed along with them.
    monkeypatch.setattr(svc_module, "get_engine", lambda connection_id: engine)
    monkeypatch.setattr(svc_module, "session_scope", _session_scope)
    monkeypatch.setattr(sv_module, "get_engine", lambda connection_id: engine)
    # Governed-writes preview (item 93) opens its own session for the COUNT(*).
    monkeypatch.setattr(wp_module, "session_scope", _session_scope)
    # Governed-writes gated execution (item 93 phase 2) opens its own write txn.
    monkeypatch.setattr(wx_module, "session_scope", _session_scope)

    settings = AppConfig(
        environment="localhost",
        mcp_enabled=False,
        audit_sink_backend="none",
    )
    app = create_app(settings)
    yield app
    await engine.dispose()
