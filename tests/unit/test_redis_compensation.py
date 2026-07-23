"""RedisCompensationStore against fakeredis (TODO.md item 93 phase 3b).

Proves the cross-replica property that closes the self-review's #1 gap: a
compensation record put through one store instance is resolvable through a
separate instance sharing the same Redis (standing in for two replicas), plus
single-use (consume) and expiry semantics, and that a pre-image carrying
datetime/Decimal values survives the JSON round-trip.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import fakeredis.aioredis
import pytest

from querygate.execution.compensation import CompensationRecord, compensation_expiry
from querygate.execution.redis_compensation import RedisCompensationStore

pytestmark = pytest.mark.unit


@pytest.fixture
def redis_client():
    return fakeredis.aioredis.FakeRedis()


def _record(cid="c1", *, ttl=3600, pre_image=None) -> CompensationRecord:
    return CompensationRecord(
        compensation_id=cid,
        connection_id="demo",
        table="orders",
        op="delete",
        pk_column="id",
        pre_image=pre_image or [],
        expires_at=compensation_expiry(ttl),
    )


@pytest.mark.asyncio
async def test_record_is_visible_across_store_instances(redis_client):
    # Two instances, one Redis — stands in for two replicas. The record minted on
    # `a` is resolvable on `b`, which the in-process store cannot do.
    a = RedisCompensationStore(redis_client)
    b = RedisCompensationStore(redis_client)
    await a.put(_record("shared"))
    got = await b.get("shared")
    assert got is not None and got.compensation_id == "shared" and got.table == "orders"


@pytest.mark.asyncio
async def test_consume_is_single_use(redis_client):
    store = RedisCompensationStore(redis_client)
    await store.put(_record("once"))
    assert await store.get("once") is not None
    await store.consume("once")
    assert await store.get("once") is None  # replay is a clean miss


@pytest.mark.asyncio
async def test_already_expired_record_is_a_miss(redis_client):
    store = RedisCompensationStore(redis_client)
    await store.put(_record("stale", ttl=-1))  # expires_at already in the past
    assert await store.get("stale") is None


@pytest.mark.asyncio
async def test_missing_id_is_none(redis_client):
    assert await RedisCompensationStore(redis_client).get("nope") is None


@pytest.mark.asyncio
async def test_pre_image_with_datetime_and_decimal_round_trips(redis_client):
    store = RedisCompensationStore(redis_client)
    pre_image = [{"id": 1, "created_at": dt.datetime(2026, 1, 1, 9, 30), "total": Decimal("12.50")}]
    await store.put(_record("typed", pre_image=pre_image))
    got = await store.get("typed")
    row = got.pre_image[0]
    # JSON turns datetime/Decimal into strings; the write compiler's
    # _coerce_write_value restores their Python types on undo.
    assert row["created_at"] == "2026-01-01T09:30:00"
    assert row["total"] == "12.50"
    assert row["id"] == 1
