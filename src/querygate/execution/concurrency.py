"""Per-connection concurrency guardrail.

Every read against a connection — structured query, batch item, schema
lookup — shares one budget per connection id, so callers contend for one
concurrency limit per database rather than each mechanism having its own.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import AsyncGenerator

SEMAPHORES: dict[str, asyncio.Semaphore] = {}


def _get_or_create_semaphore(connection_id: str, max_concurrency: int) -> asyncio.Semaphore:
    sem = SEMAPHORES.get(connection_id)
    if sem is None:
        sem = asyncio.Semaphore(max_concurrency)
        SEMAPHORES[connection_id] = sem
    return sem


@contextlib.asynccontextmanager
async def concurrency_slot(
    connection_id: str, max_concurrency: int, wait_seconds: float
) -> AsyncGenerator[None, None]:
    sem = _get_or_create_semaphore(connection_id, max_concurrency)
    try:
        await asyncio.wait_for(sem.acquire(), timeout=wait_seconds)
    except asyncio.TimeoutError:
        raise ValueError(
            f"too many concurrent {connection_id!r} queries in flight, try again shortly"
        ) from None
    try:
        yield
    finally:
        sem.release()
