"""Unit tests for the per-connection concurrency guardrail."""

from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest

from querygate.execution import concurrency as cc
from querygate.execution.redis_concurrency import RedisConcurrencyLimiter
from querygate.metrics import REGISTRY


def _gauge(name: str, labels: dict) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.mark.asyncio
async def test_concurrency_slot_updates_in_use_gauge():
    cc.SEMAPHORES.pop("demo", None)
    assert _gauge("querygate_concurrency_in_use", {"connection": "demo"}) == 0.0

    async with cc.concurrency_slot("demo", max_concurrency=2, wait_seconds=1):
        assert _gauge("querygate_concurrency_in_use", {"connection": "demo"}) == 1.0

    assert _gauge("querygate_concurrency_in_use", {"connection": "demo"}) == 0.0
    assert _gauge("querygate_concurrency_max", {"connection": "demo"}) == 2.0


@pytest.mark.asyncio
async def test_concurrency_cap_serializes_in_flight_calls():
    cc.SEMAPHORES.pop("demo", None)
    in_flight = 0
    max_in_flight = 0

    async def work():
        nonlocal in_flight, max_in_flight
        async with cc.concurrency_slot("demo", max_concurrency=1, wait_seconds=1):
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.02)
            in_flight -= 1

    await asyncio.gather(work(), work(), work())
    assert max_in_flight == 1


@pytest.mark.asyncio
async def test_concurrency_cap_raises_when_wait_exceeded():
    cc.SEMAPHORES["demo"] = asyncio.Semaphore(1)
    await cc.SEMAPHORES["demo"].acquire()  # occupy the only slot

    with pytest.raises(ValueError, match="too many concurrent"):
        async with cc.concurrency_slot("demo", max_concurrency=1, wait_seconds=0.05):
            pass  # pragma: no cover


@pytest.mark.asyncio
async def test_concurrency_caps_are_independent_per_connection():
    cc.SEMAPHORES["demo"] = asyncio.Semaphore(1)
    await cc.SEMAPHORES["demo"].acquire()
    cc.SEMAPHORES.pop("other", None)

    # Must not block on "demo"'s exhausted slot.
    async with cc.concurrency_slot("other", max_concurrency=1, wait_seconds=1):
        pass


@pytest.mark.asyncio
async def test_concurrency_slot_tracks_queue_depth_while_waiting():
    cc.SEMAPHORES["demo"] = asyncio.Semaphore(1)
    await cc.SEMAPHORES["demo"].acquire()  # occupy the only slot
    assert _gauge("querygate_queue_depth", {"connection": "demo"}) == 0.0

    async def _waiter():
        async with cc.concurrency_slot("demo", max_concurrency=1, wait_seconds=1):
            pass

    task = asyncio.create_task(_waiter())
    await asyncio.sleep(0.02)  # let the waiter actually start blocking on acquire
    assert _gauge("querygate_queue_depth", {"connection": "demo"}) == 1.0

    cc.SEMAPHORES["demo"].release()  # frees the slot the waiter is queued behind
    await task
    assert _gauge("querygate_queue_depth", {"connection": "demo"}) == 0.0


@pytest.mark.asyncio
async def test_concurrency_slot_releases_queue_depth_after_a_capacity_timeout():
    cc.SEMAPHORES["demo"] = asyncio.Semaphore(1)
    await cc.SEMAPHORES["demo"].acquire()  # occupy the only slot

    with pytest.raises(ValueError, match="too many concurrent"):
        async with cc.concurrency_slot("demo", max_concurrency=1, wait_seconds=0.05):
            pass  # pragma: no cover
    # Depth must be released even though the acquire ultimately timed out.
    assert _gauge("querygate_queue_depth", {"connection": "demo"}) == 0.0


@pytest.mark.asyncio
async def test_concurrency_slot_dispatches_to_redis_limiter_when_configured():
    limiter = RedisConcurrencyLimiter(fakeredis.aioredis.FakeRedis(), lease_seconds=30)
    cc.init_redis_limiter(limiter)
    try:
        # SEMAPHORES must stay untouched — proves the in-process path was
        # never exercised for this call.
        cc.SEMAPHORES.pop("demo", None)
        async with cc.concurrency_slot("demo", max_concurrency=1, wait_seconds=1):
            assert "demo" not in cc.SEMAPHORES
    finally:
        cc.clear_redis_limiter()


@pytest.mark.asyncio
async def test_clear_redis_limiter_reverts_to_in_process():
    limiter = RedisConcurrencyLimiter(fakeredis.aioredis.FakeRedis(), lease_seconds=30)
    cc.init_redis_limiter(limiter)
    cc.clear_redis_limiter()

    cc.SEMAPHORES.pop("demo", None)
    async with cc.concurrency_slot("demo", max_concurrency=1, wait_seconds=1):
        assert "demo" in cc.SEMAPHORES


# --- Queue-depth pressure controls (TODO.md item 35 phase 2) ---------------


@pytest.mark.asyncio
async def test_max_queue_depth_rejects_once_local_queue_is_full():
    cc.clear_local_queue_state()
    cc.SEMAPHORES["demo"] = asyncio.Semaphore(1)
    await cc.SEMAPHORES["demo"].acquire()  # occupy the only slot

    async def _waiter():
        async with cc.concurrency_slot(
            "demo", max_concurrency=1, wait_seconds=1, max_queue_depth=1
        ):
            pass  # pragma: no cover

    task = asyncio.create_task(_waiter())
    await asyncio.sleep(0.02)  # let it actually start waiting (queue depth == 1)

    with pytest.raises(
        cc.QueueDepthExceededError, match="queue is already at its configured depth"
    ):
        async with cc.concurrency_slot(
            "demo", max_concurrency=1, wait_seconds=1, max_queue_depth=1
        ):
            pass  # pragma: no cover

    cc.SEMAPHORES["demo"].release()
    await task


@pytest.mark.asyncio
async def test_max_queue_depth_none_never_rejects():
    cc.clear_local_queue_state()
    cc.SEMAPHORES["demo"] = asyncio.Semaphore(2)

    async def _hold():
        async with cc.concurrency_slot("demo", max_concurrency=2, wait_seconds=1):
            await asyncio.sleep(0.05)

    # Several concurrent waiters, no max_queue_depth configured (the
    # default) — none of them should ever see QueueDepthExceededError.
    await asyncio.gather(*(_hold() for _ in range(5)))


@pytest.mark.asyncio
async def test_max_queue_depth_per_principal_isolates_noisy_caller():
    cc.clear_local_queue_state()
    cc.SEMAPHORES["demo"] = asyncio.Semaphore(1)
    await cc.SEMAPHORES["demo"].acquire()

    async def _waiter(principal: str):
        async with cc.concurrency_slot(
            "demo",
            max_concurrency=1,
            wait_seconds=1,
            principal_subject=principal,
            max_queue_depth_per_principal=1,
        ):
            pass  # pragma: no cover

    task = asyncio.create_task(_waiter("noisy-agent"))
    await asyncio.sleep(0.02)

    # A second wait from the same principal is rejected...
    with pytest.raises(cc.QueueDepthExceededError, match="noisy-agent"):
        async with cc.concurrency_slot(
            "demo",
            max_concurrency=1,
            wait_seconds=1,
            principal_subject="noisy-agent",
            max_queue_depth_per_principal=1,
        ):
            pass  # pragma: no cover

    # ...but a different principal is unaffected.
    other_task = asyncio.create_task(_waiter_for_other())
    await asyncio.sleep(0.02)

    # One release is enough: it frees `task`, which then releases the slot
    # again on its own way out, in turn freeing `other_task`.
    cc.SEMAPHORES["demo"].release()
    await task
    await other_task


async def _waiter_for_other():
    async with cc.concurrency_slot(
        "demo",
        max_concurrency=1,
        wait_seconds=1,
        principal_subject="other-agent",
        max_queue_depth_per_principal=1,
    ):
        pass  # pragma: no cover


@pytest.mark.asyncio
async def test_max_queue_depth_releases_slot_on_success_not_just_on_error():
    cc.clear_local_queue_state()
    cc.SEMAPHORES.pop("demo", None)

    async with cc.concurrency_slot("demo", max_concurrency=1, wait_seconds=1, max_queue_depth=1):
        pass

    # The depth slot from the completed call above must have been released,
    # so a fresh call with the same depth-1 cap must not be rejected.
    async with cc.concurrency_slot("demo", max_concurrency=1, wait_seconds=1, max_queue_depth=1):
        pass


@pytest.mark.asyncio
async def test_redis_backed_max_queue_depth_rejects_across_separate_limiter_instances():
    """Two limiters sharing one Redis stand in for two QueryGate replicas —
    the queue-depth cap must be enforced against the shared cross-replica
    count, not each replica's own local tally.
    """
    redis_client = fakeredis.aioredis.FakeRedis()
    limiter_a = RedisConcurrencyLimiter(redis_client, lease_seconds=30)
    limiter_b = RedisConcurrencyLimiter(redis_client, lease_seconds=30)

    # Occupy the only concurrency slot via limiter_a so the next caller must
    # queue rather than run immediately.
    holder_token = await limiter_a.acquire("demo", max_concurrency=1, wait_seconds=1)

    cc.init_redis_limiter(limiter_a)
    try:

        async def _waiter():
            async with cc.concurrency_slot(
                "demo", max_concurrency=1, wait_seconds=1, max_queue_depth=1
            ):
                pass  # pragma: no cover

        task = asyncio.create_task(_waiter())
        await asyncio.sleep(0.05)  # let it register itself in Redis as waiting

        # Switch to limiter_b (a separate process's limiter object, same
        # Redis) to prove the cap is enforced cross-replica.
        cc.init_redis_limiter(limiter_b)
        with pytest.raises(cc.QueueDepthExceededError):
            async with cc.concurrency_slot(
                "demo", max_concurrency=1, wait_seconds=1, max_queue_depth=1
            ):
                pass  # pragma: no cover

        cc.init_redis_limiter(limiter_a)
        await limiter_a.release("demo", holder_token)
        await task
    finally:
        cc.clear_redis_limiter()


@pytest.mark.asyncio
async def test_redis_backed_queue_depth_gauge_reflects_cross_replica_count():
    redis_client = fakeredis.aioredis.FakeRedis()
    limiter = RedisConcurrencyLimiter(redis_client, lease_seconds=30)
    cc.init_redis_limiter(limiter)
    try:
        holder_token = await limiter.acquire("demo-gauge", max_concurrency=1, wait_seconds=1)

        async def _waiter():
            async with cc.concurrency_slot("demo-gauge", max_concurrency=1, wait_seconds=1):
                pass

        task = asyncio.create_task(_waiter())
        await asyncio.sleep(0.05)
        assert _gauge("querygate_queue_depth", {"connection": "demo-gauge"}) == 1.0

        await limiter.release("demo-gauge", holder_token)
        await task
        assert _gauge("querygate_queue_depth", {"connection": "demo-gauge"}) == 0.0
    finally:
        cc.clear_redis_limiter()
