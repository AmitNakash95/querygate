"""Unit tests for the per-connection concurrency guardrail."""

from __future__ import annotations

import asyncio

import pytest

from querygate.execution import concurrency as cc


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
