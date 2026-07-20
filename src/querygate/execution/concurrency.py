"""Per-connection concurrency guardrail.

Every read against a connection — structured query, batch item, schema
lookup — shares one budget per connection id, so callers contend for one
concurrency limit per database rather than each mechanism having its own.

Default backend is an in-process `InProcessConcurrencyLimiter` per connection
id — correct for a single instance, but silently multiplied by instance count
under a load balancer (see `redis_concurrency.py`). Set
`AppConfig.concurrency_backend = "redis"` for a distributed limiter shared
across instances; `init_redis_limiter`/`clear_redis_limiter` (called from
`api/app.py`'s lifespan) swap it in — backend selection is a process-startup
decision, not something that changes mid-flight, so there's no handling here
for a slot acquired under one backend being released under the other.

`InProcessConcurrencyLimiter` and `RedisConcurrencyLimiter` both implement
`ConcurrencyLimiter` (CLAUDE.md's "Composable single-purpose interfaces"
section) — one Protocol, dispatched through whichever instance is currently
active, rather than an `if backend == "redis"` check repeated at every call
site.

`max_queue_depth`/`max_queue_depth_per_principal` (TODO.md item 35 phase 2,
both optional on `Policy`) cap how many callers may be *waiting* for a slot
at once, so an unbounded `queue_mode=wait` queue can't itself become a
resource-exhaustion vector. Enforcement and the `querygate_queue_depth`
gauge both route through the Redis-backed sorted sets in
`redis_concurrency.py` whenever the Redis backend is selected, so the cap
(and the gauge) reflect the true cross-replica count, not just this
process's own waiters; the in-process fallback only ever sees this
process's waiters, same as before phase 2.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, AsyncGenerator, Dict, Optional, Protocol, Tuple

from querygate.core.exceptions import ConcurrencyLimitError, QueueDepthExceededError
from querygate.execution.redis_concurrency import RedisConcurrencyLimiter
from querygate.metrics import CONCURRENCY_IN_USE, CONCURRENCY_MAX, QUEUE_DEPTH


class ConcurrencyLimiter(Protocol):
    """One method per operation `concurrency_slot` needs — implemented by
    `InProcessConcurrencyLimiter` (below) and `RedisConcurrencyLimiter`
    (`redis_concurrency.py`, unchanged, already satisfies this shape).
    """

    async def acquire(self, connection_id: str, max_concurrency: int, wait_seconds: float) -> Any:
        """Return an opaque token on success; raise `TimeoutError` (or
        `asyncio.TimeoutError`) if `wait_seconds` elapses first —
        `concurrency_slot` translates that into `ConcurrencyLimitError`.
        """
        ...

    async def release(self, connection_id: str, token: Any) -> None: ...

    async def enter_queue(
        self,
        connection_id: str,
        *,
        principal_subject: Optional[str],
        max_queue_depth: Optional[int],
        max_queue_depth_per_principal: Optional[int],
    ) -> Optional[Tuple[Any, int]]:
        """Admit one waiter into queue-depth tracking. Returns `(token,
        depth_after_admission)` on success. On rejection, either returns
        `None` (the caller raises a generic `QueueDepthExceededError` —
        `RedisConcurrencyLimiter`'s Lua script can't cheaply tell which cap
        tripped) or raises `QueueDepthExceededError` directly with a more
        specific message (`InProcessConcurrencyLimiter` can tell which cap
        tripped, so it does) — both are valid.
        """
        ...

    async def leave_queue(
        self, connection_id: str, *, principal_subject: Optional[str], token: Any
    ) -> int:
        """Release a waiter admitted by `enter_queue`; returns the depth
        after removal, or a negative sentinel if unknown."""
        ...


class InProcessConcurrencyLimiter:
    """Single-process concurrency limiter — the default, correct only for
    one instance (see module docstring). A thin class around exactly the
    same semaphore/queue-depth bookkeeping this module used to keep as bare
    module-level dicts and free functions; wrapping it lets
    `execution/concurrency.py` dispatch through one `ConcurrencyLimiter`
    variable instead of an inline backend check.
    """

    def __init__(self) -> None:
        self._semaphores: Dict[str, asyncio.Semaphore] = {}
        self._queue_depth: Dict[str, int] = {}
        self._queue_depth_by_principal: Dict[Tuple[str, str], int] = {}

    def semaphore(self, connection_id: str, max_concurrency: int) -> asyncio.Semaphore:
        """Get-or-create the semaphore for `connection_id` — public (not
        `_private`) so tests can pre-seed/pre-acquire a slot for a specific
        scenario, and so `config_reload.py`/`reset_semaphore` have a real
        object to invalidate. `max_concurrency` is only used to construct a
        *new* semaphore; it's ignored on a cache hit, same as before.
        """
        sem = self._semaphores.get(connection_id)
        if sem is None:
            sem = asyncio.Semaphore(max_concurrency)
            self._semaphores[connection_id] = sem
        return sem

    def reset_semaphore(self, connection_id: str) -> None:
        """Drop the cached semaphore so the next `acquire()` creates a fresh
        one — used by `config_reload.py` on hot-reload (every connection's
        cap may have changed) and by tests needing a clean slot.
        """
        self._semaphores.pop(connection_id, None)

    def has_semaphore(self, connection_id: str) -> bool:
        """Whether a semaphore has been created for `connection_id` yet —
        used by tests proving the in-process path was never touched (e.g.
        because the Redis backend handled a call instead).
        """
        return connection_id in self._semaphores

    def clear(self) -> None:
        """Reset ALL in-process state — `asyncio.Semaphore` objects are
        bound to the event loop that created them, and pytest-asyncio gives
        each test its own loop, so a semaphore (or queue-depth count) cached
        from a previous test/loop must never leak into the next one. Call
        this once per test instead of clearing semaphores and queue-depth
        state separately.
        """
        self._semaphores.clear()
        self._queue_depth.clear()
        self._queue_depth_by_principal.clear()

    async def acquire(
        self, connection_id: str, max_concurrency: int, wait_seconds: float
    ) -> asyncio.Semaphore:
        sem = self.semaphore(connection_id, max_concurrency)
        await asyncio.wait_for(sem.acquire(), timeout=wait_seconds)
        return sem

    async def release(self, connection_id: str, token: asyncio.Semaphore) -> None:
        token.release()

    async def enter_queue(
        self,
        connection_id: str,
        *,
        principal_subject: Optional[str],
        max_queue_depth: Optional[int],
        max_queue_depth_per_principal: Optional[int],
    ) -> Tuple[Tuple[str, str], int]:
        global_depth = self._queue_depth.get(connection_id, 0)
        if max_queue_depth is not None and global_depth >= max_queue_depth:
            raise QueueDepthExceededError(
                f"connection {connection_id!r} queue is already at its configured depth "
                f"({max_queue_depth}), try again shortly"
            )
        principal_key = (connection_id, principal_subject or "")
        principal_depth = self._queue_depth_by_principal.get(principal_key, 0)
        if (
            principal_subject is not None
            and max_queue_depth_per_principal is not None
            and principal_depth >= max_queue_depth_per_principal
        ):
            raise QueueDepthExceededError(
                f"principal {principal_subject!r} already has {max_queue_depth_per_principal} "
                f"queries queued on connection {connection_id!r}, try again shortly"
            )
        self._queue_depth[connection_id] = global_depth + 1
        if principal_subject is not None:
            self._queue_depth_by_principal[principal_key] = principal_depth + 1
        return (connection_id, principal_subject or ""), global_depth + 1

    async def leave_queue(
        self, connection_id: str, *, principal_subject: Optional[str], token: Tuple[str, str]
    ) -> int:
        self._queue_depth[connection_id] = max(0, self._queue_depth.get(connection_id, 0) - 1)
        if principal_subject is not None:
            principal_key = (connection_id, principal_subject)
            self._queue_depth_by_principal[principal_key] = max(
                0, self._queue_depth_by_principal.get(principal_key, 0) - 1
            )
        return self._queue_depth[connection_id]


_in_process_limiter = InProcessConcurrencyLimiter()
_active_limiter: ConcurrencyLimiter = _in_process_limiter


def in_process_limiter() -> InProcessConcurrencyLimiter:
    """The persistent in-process limiter instance — used by tests to
    pre-seed/reset semaphore or queue-depth state, and by `config_reload.py`
    to invalidate a connection's cached semaphore, regardless of whether the
    Redis backend is currently active.
    """
    return _in_process_limiter


def init_redis_limiter(limiter: RedisConcurrencyLimiter) -> None:
    global _active_limiter
    _active_limiter = limiter


def clear_redis_limiter() -> None:
    """Revert to the persistent in-process limiter — the same instance
    `in_process_limiter()` returns, not a fresh one, so any state it already
    held is preserved across a Redis-backend detour.
    """
    global _active_limiter
    _active_limiter = _in_process_limiter


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
    limiter = _active_limiter

    entered = await limiter.enter_queue(
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

    try:
        try:
            token = await limiter.acquire(connection_id, max_concurrency, wait_seconds)
        except (asyncio.TimeoutError, TimeoutError):
            raise ConcurrencyLimitError(
                f"too many concurrent {connection_id!r} queries in flight, try again shortly"
            ) from None
    finally:
        depth_after = await limiter.leave_queue(
            connection_id, principal_subject=principal_subject, token=queue_token
        )
        if depth_after >= 0:
            QUEUE_DEPTH.labels(connection=connection_id).set(depth_after)

    CONCURRENCY_IN_USE.labels(connection=connection_id).inc()
    try:
        yield
    finally:
        await limiter.release(connection_id, token)
        CONCURRENCY_IN_USE.labels(connection=connection_id).dec()
