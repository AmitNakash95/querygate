"""Per-connection concurrency guardrail.

Every read against a connection — structured query, batch item, schema
lookup — shares one budget per connection id, so callers contend for one
concurrency limit per database rather than each mechanism having its own.

Default backend is an in-process `asyncio.Semaphore` per connection id —
correct for a single instance, but silently multiplied by instance count
under a load balancer (see `redis_concurrency.py`). Set
`AppConfig.concurrency_backend = "redis"` for a distributed limiter shared
across instances; `init_redis_limiter`/`clear_redis_limiter` (called from
`api/app.py`'s lifespan) swap it in — backend selection is a process-startup
decision, not something that changes mid-flight, so there's no handling here
for a slot acquired under one backend being released under the other.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, AsyncGenerator, Optional

from querygate.core.exceptions import ConcurrencyLimitError
from querygate.execution.redis_concurrency import RedisConcurrencyLimiter
from querygate.metrics import CONCURRENCY_IN_USE, CONCURRENCY_MAX, QUEUE_DEPTH

SEMAPHORES: dict[str, asyncio.Semaphore] = {}

_REDIS_LIMITER: Optional[RedisConcurrencyLimiter] = None


def init_redis_limiter(limiter: RedisConcurrencyLimiter) -> None:
    global _REDIS_LIMITER
    _REDIS_LIMITER = limiter


def clear_redis_limiter() -> None:
    global _REDIS_LIMITER
    _REDIS_LIMITER = None


def _get_or_create_semaphore(connection_id: str, max_concurrency: int) -> asyncio.Semaphore:
    sem = SEMAPHORES.get(connection_id)
    if sem is None:
        sem = asyncio.Semaphore(max_concurrency)
        SEMAPHORES[connection_id] = sem
    return sem


async def _acquire(connection_id: str, max_concurrency: int, wait_seconds: float) -> Any:
    try:
        if _REDIS_LIMITER is not None:
            return await _REDIS_LIMITER.acquire(connection_id, max_concurrency, wait_seconds)
        sem = _get_or_create_semaphore(connection_id, max_concurrency)
        await asyncio.wait_for(sem.acquire(), timeout=wait_seconds)
        return sem
    except (asyncio.TimeoutError, TimeoutError):
        raise ConcurrencyLimitError(
            f"too many concurrent {connection_id!r} queries in flight, try again shortly"
        ) from None


async def _release(connection_id: str, token: Any) -> None:
    if _REDIS_LIMITER is not None:
        await _REDIS_LIMITER.release(connection_id, token)
    else:
        token.release()  # token is the asyncio.Semaphore acquired above


@contextlib.asynccontextmanager
async def concurrency_slot(
    connection_id: str, max_concurrency: int, wait_seconds: float
) -> AsyncGenerator[None, None]:
    CONCURRENCY_MAX.labels(connection=connection_id).set(max_concurrency)
    QUEUE_DEPTH.labels(connection=connection_id).inc()
    try:
        token = await _acquire(connection_id, max_concurrency, wait_seconds)
    finally:
        QUEUE_DEPTH.labels(connection=connection_id).dec()
    CONCURRENCY_IN_USE.labels(connection=connection_id).inc()
    try:
        yield
    finally:
        await _release(connection_id, token)
        CONCURRENCY_IN_USE.labels(connection=connection_id).dec()
