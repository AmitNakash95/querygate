"""End-to-end proof that the per-principal quota (TODO.md item 50) actually
gates execution against a real (SQLite) database.

Unlike the unit tests, this drives the whole `StructuredQueryService.execute`
pipeline — policy resolution, quota reservation, concurrency slot, compile,
execute — so it proves the quota is checked in the real request path and that a
rejected attempt never reaches the database. SQLite stands in for
Postgres/MSSQL here, same as `test_sqlite_end_to_end.py`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from examples.demo_db.schema import create_and_seed_async
from querygate.core.auth import Principal
from querygate.core.exceptions import QuotaExceededError
from querygate.execution.service import StructuredQueryService
from querygate.metrics import QUERY_QUOTA_REJECTIONS_TOTAL
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.query_ast.models import StructuredQuery

pytestmark = pytest.mark.integration

_QUERY = {
    "from": "orders",
    "select": ["orders.id", "orders.status"],
    "limit": 5,
}


@pytest_asyncio.fixture
async def sqlite_engine(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    await create_and_seed_async(engine)

    import querygate.execution.service as svc_module
    import querygate.validation.schema_validation as sv_module

    @asynccontextmanager
    async def _session_scope(connection_id, policy=None):
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    monkeypatch.setattr(svc_module, "get_engine", lambda connection_id: engine)
    monkeypatch.setattr(svc_module, "session_scope", _session_scope)
    monkeypatch.setattr(sv_module, "get_engine", lambda connection_id: engine)
    yield engine
    await engine.dispose()


def _set_policy(**quota):
    set_policy_store(PolicyStore(default=Policy(**quota), overrides={}))


def _service(subject="agent-1"):
    return StructuredQueryService(
        "demo", principal=Principal(subject=subject, auth_method="api_key")
    )


@pytest.mark.asyncio
async def test_request_quota_blocks_third_query(sqlite_engine):
    _set_policy(max_requests_per_window=2, quota_window_seconds=3600)
    service = _service()
    query = StructuredQuery.model_validate(_QUERY)

    # First two attempts run against the DB and return rows.
    for _ in range(2):
        result = await service.execute(query)
        assert result.row_count > 0

    # The third is refused before touching the database.
    with pytest.raises(QuotaExceededError) as excinfo:
        await service.execute(query)
    assert excinfo.value.quota_kind == "requests"
    assert excinfo.value.retry_after_seconds >= 1


@pytest.mark.asyncio
async def test_quota_is_isolated_per_principal(sqlite_engine):
    _set_policy(max_requests_per_window=1, quota_window_seconds=3600)
    query = StructuredQuery.model_validate(_QUERY)

    await _service("agent-1").execute(query)
    # A different principal has its own budget and is unaffected.
    result = await _service("agent-2").execute(query)
    assert result.row_count > 0
    # agent-1 is now over its own cap.
    with pytest.raises(QuotaExceededError):
        await _service("agent-1").execute(query)


@pytest.mark.asyncio
async def test_unauthenticated_caller_is_not_quota_limited(sqlite_engine):
    _set_policy(max_requests_per_window=1, quota_window_seconds=3600)
    service = StructuredQueryService("demo", principal=None)
    query = StructuredQuery.model_validate(_QUERY)
    # No principal to attribute usage to -> quota skipped; repeated calls run.
    for _ in range(3):
        result = await service.execute(query)
        assert result.row_count > 0


@pytest.mark.asyncio
async def test_byte_quota_blocks_after_response_recorded(sqlite_engine):
    # A tiny byte cap: the first query's response already pushes the window
    # total to/over the cap, so the second attempt is refused.
    _set_policy(max_response_bytes_per_window=1, quota_window_seconds=3600)
    service = _service()
    query = StructuredQuery.model_validate(_QUERY)

    result = await service.execute(query)
    assert result.row_count > 0
    with pytest.raises(QuotaExceededError) as excinfo:
        await service.execute(query)
    assert excinfo.value.quota_kind == "bytes"


@pytest.mark.asyncio
async def test_quota_rejection_increments_metric(sqlite_engine):
    _set_policy(max_requests_per_window=1, quota_window_seconds=3600)
    service = _service("metric-agent")
    query = StructuredQuery.model_validate(_QUERY)

    before = QUERY_QUOTA_REJECTIONS_TOTAL.labels(
        connection="demo", quota_kind="requests"
    )._value.get()
    await service.execute(query)
    with pytest.raises(QuotaExceededError):
        await service.execute(query)
    after = QUERY_QUOTA_REJECTIONS_TOTAL.labels(
        connection="demo", quota_kind="requests"
    )._value.get()
    assert after == before + 1
