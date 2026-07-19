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

`max_queue_depth`/`max_queue_depth_per_principal` (TODO.md item 35 phase 2,
both optional on `Policy`) cap how many callers may be *waiting* for a slot
at once, so an unbounded `queue_mode=wait` queue can't itself become a
resource-exhaustion vector. Enforcement and the `querygate_queue_depth`
gauge both route through the Redis-backed sorted sets in
`redis_concurrency.py` whenever the Redis backend is selected, so the cap
(and the gauge) reflect the true cross-replica count, not just this
process's own waiters; the in-process fallback below only ever sees this
process's waiters, same as before phase 2.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, AsyncGenerator, Optional

from querygate.core.exceptions import ConcurrencyLimitError, QueueDepthExceededError
from querygate.execution.redis_concurrency import RedisConcurrencyLimiter
from querygate.metrics import CONCURRENCY_IN_USE, CONCURRENCY_MAX, QUEUE_DEPTH

SEMAPHORES: dict[str, asyncio.Semaphore] = {}

_REDIS_LIMITER: Optional[RedisConcurrencyLimiter] = None

# In-process queue-depth tracking, single-process like SEMAPHORES above.
# Plain dicts rather than reading the Prometheus gauge back (it has no
# public "current value for these labels" API) so max_queue_depth /
# max_queue_depth_per_principal have something to compare against.
_LOCAL_QUEUE_DEPTH: dict[str, int] = {}
_LOCAL_QUEUE_DEPTH_BY_PRINCIPAL: dict[tuple[str, str], int] = {}


def init_redis_limiter(limiter: RedisConcurrencyLimiter) -> None:
    global _REDIS_LIMITER
    _REDIS_LIMITER = limiter


def clear_redis_limiter() -> None:
    global _REDIS_LIMITER
    _REDIS_LIMITER = None


def clear_local_queue_state() -> None:
    """Reset in-process queue-depth tracking — call this alongside
    `SEMAPHORES.clear()` between tests/event loops, for the same reason
    (stale counts from a previous run must never leak into the next one).
    """
    _LOCAL_QUEUE_DEPTH.clear()
    _LOCAL_QUEUE_DEPTH_BY_PRINCIPAL.clear()


def _get_or_create_semaphore(connection_id: str, max_concurrency: int) -> asyncio.Semaphore:
    sem = SEMAPHORES.get(connection_id)
    if sem is None:
        sem = asyncio.Semaphore(max_concurrency)
        SEMAPHORES[connection_id] = sem
    return sem


def _enter_local_queue(
    connection_id: str,
    *,
    principal_subject: Optional[str],
    max_queue_depth: Optional[int],
    max_queue_depth_per_principal: Optional[int],
) -> None:
    global_depth = _LOCAL_QUEUE_DEPTH.get(connection_id, 0)
    if max_queue_depth is not None and global_depth >= max_queue_depth:
        raise QueueDepthExceededError(
            f"connection {connection_id!r} queue is already at its configured depth "
            f"({max_queue_depth}), try again shortly"
        )
    principal_key = (connection_id, principal_subject or "")
    principal_depth = _LOCAL_QUEUE_DEPTH_BY_PRINCIPAL.get(principal_key, 0)
    if (
        principal_subject is not None
        and max_queue_depth_per_principal is not None
        and principal_depth >= max_queue_depth_per_principal
    ):
        raise QueueDepthExceededError(
            f"principal {principal_subject!r} already has {max_queue_depth_per_principal} "
            f"queries queued on connection {connection_id!r}, try again shortly"
        )
    _LOCAL_QUEUE_DEPTH[connection_id] = global_depth + 1
    if principal_subject is not None:
        _LOCAL_QUEUE_DEPTH_BY_PRINCIPAL[principal_key] = principal_depth + 1


def _leave_local_queue(connection_id: str, principal_subject: Optional[str]) -> None:
    _LOCAL_QUEUE_DEPTH[connection_id] = max(0, _LOCAL_QUEUE_DEPTH.get(connection_id, 0) - 1)
    if principal_subject is not None:
        principal_key = (connection_id, principal_subject)
        _LOCAL_QUEUE_DEPTH_BY_PRINCIPAL[principal_key] = max(
            0, _LOCAL_QUEUE_DEPTH_BY_PRINCIPAL.get(principal_key, 0) - 1
        )


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
    connection_id: str,
    max_concurrency: int,
    wait_seconds: float,
    *,
    principal_subject: Optional[str] = None,
    max_queue_depth: Optional[int] = None,
    max_queue_depth_per_principal: Optional[int] = None,
) -> AsyncGenerator[None, None]:
    CONCURRENCY_MAX.labels(connection=connection_id).set(max_concurrency)

    if _REDIS_LIMITER is not None:
        entered = await _REDIS_LIMITER.enter_queue(
            connection_id,
            principal_subject=principal_subject,
            max_queue_depth=max_queue_depth,
            max_queue_depth_per_principal=max_queue_depth_per_principal,
        )
        if entered is None:
            raise QueueDepthExceededError(
                f"connection {connection_id!r} queue is already at its configured depth, "
                "try again shortly"
            )
        queue_token, queue_depth = entered
        if queue_depth >= 0:
            QUEUE_DEPTH.labels(connection=connection_id).set(queue_depth)
    else:
        _enter_local_queue(
            connection_id,
            principal_subject=principal_subject,
            max_queue_depth=max_queue_depth,
            max_queue_depth_per_principal=max_queue_depth_per_principal,
        )
        QUEUE_DEPTH.labels(connection=connection_id).inc()

    try:
        token = await _acquire(connection_id, max_concurrency, wait_seconds)
    finally:
        if _REDIS_LIMITER is not None:
            depth_after = await _REDIS_LIMITER.leave_queue(
                connection_id, principal_subject=principal_subject, token=queue_token
            )
            if depth_after >= 0:
                QUEUE_DEPTH.labels(connection=connection_id).set(depth_after)
        else:
            _leave_local_queue(connection_id, principal_subject)
            QUEUE_DEPTH.labels(connection=connection_id).dec()
    CONCURRENCY_IN_USE.labels(connection=connection_id).inc()
    try:
        yield
    finally:
        await _release(connection_id, token)
        CONCURRENCY_IN_USE.labels(connection=connection_id).dec()
