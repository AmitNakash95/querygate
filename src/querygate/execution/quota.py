"""Per-principal request/byte quota over a rolling time window (TODO.md item 50).

`execution/concurrency.py` bounds how many queries a principal can have
*in flight at once*; it says nothing about how many it can run *over time*. A
well-behaved agent that never exceeds its concurrency limit can still fire tens
of thousands of sequential queries an hour, exhausting a database's capacity or
a customer's cost budget. This module adds the missing *rate* dimension: a
rolling-window cap on request count and on total response bytes, resolved per
principal (and, because the window key includes the connection id, per
connection) through the same `Policy` this caller already resolves.

Enforcement shape mirrors the concurrency guard:

* A narrow `QuotaLimiter` Protocol (CLAUDE.md's "Composable single-purpose
  interfaces" section) with two implementations, dispatched through whichever
  instance is currently active. The default `InProcessQuotaLimiter` is correct
  for a single instance — like the default in-process concurrency limiter, its
  window is silently per-replica under a load balancer. The cross-replica
  sibling that makes the quota a true shared budget is
  `execution/redis_quota.py`'s `RedisQuotaLimiter` (TODO.md item 50 phase 2,
  shipped); it sits behind this same Protocol and touches no call site. Note
  there is no separate quota backend switch — `init_redis_quota_limiter` is
  installed by `api/app.py` only when `CONCURRENCY_BACKEND=redis`.
* `reserve()` is called *before* execution and atomically prunes the window,
  checks both caps, and — if admitted — records the attempt, so N concurrent
  in-flight queries can't each slip past a check-then-act race. A rejected
  caller raises `QuotaExceededError` (a `PolicyViolationError`) and never
  touches the database.
* Response bytes aren't known until the query has run, so a reservation counts
  as one request immediately and its byte weight is filled in by
  `record_bytes()` once the response size is known. A query that fails after
  admission still counts as one request (0 bytes) — quota is anti-abuse, so an
  admitted attempt is spent whatever its downstream outcome.

`explain()` is deliberately *not* quota-gated: it compiles a preview and never
executes against the database, the same reason it skips the cost-estimation
gate.
"""

from __future__ import annotations

import math
import time
from typing import Dict, List, Optional, Protocol, Tuple

from querygate.core.exceptions import QuotaExceededError
from querygate.policy.models import Policy

# (connection_id, principal_subject) — a principal gets an independent budget
# per connection, which is the "per-principal (and optionally per-connection)"
# scope TODO.md item 50 asks for.
QuotaKey = Tuple[str, str]


class QuotaReservation:
    """Opaque token returned by `QuotaLimiter.reserve()` on admission, passed
    back to the same limiter's `record_bytes()`.

    Carries whichever backend's handle the reserving limiter needs to attribute
    the response size later: the in-process limiter stores the in-window `_entry`
    object; the Redis limiter stores the `(key, member)` of the sorted-set entry
    it added. If the entry has already aged out of the window by the time bytes
    are recorded, updating it is a harmless no-op (it no longer counts toward any
    total).
    """

    __slots__ = ("_entry", "_redis_key", "_redis_member")

    def __init__(
        self,
        entry: "Optional[_WindowEntry]" = None,
        *,
        redis_key: Optional[str] = None,
        redis_member: Optional[str] = None,
    ) -> None:
        self._entry = entry
        self._redis_key = redis_key
        self._redis_member = redis_member


class _WindowEntry:
    __slots__ = ("ts", "response_bytes")

    def __init__(self, ts: float) -> None:
        self.ts = ts
        self.response_bytes = 0


class QuotaLimiter(Protocol):
    """One reserve/record pair per operation `enforce_query_quota` needs.
    Implemented by `InProcessQuotaLimiter` (single-process) and
    `RedisQuotaLimiter` (`execution/redis_quota.py`, cross-replica — item 50
    phase 2), dispatched behind this one interface exactly like the concurrency
    limiter. Both methods are async so the Redis backend can await its client;
    the in-process backend just doesn't await anything.
    """

    async def reserve(
        self,
        key: QuotaKey,
        *,
        max_requests: Optional[int],
        max_response_bytes: Optional[int],
        window_seconds: int,
        now: Optional[float] = None,
    ) -> QuotaReservation:
        """Admit one attempt or raise `QuotaExceededError`. On admission the
        attempt is recorded immediately (counts as one request) so concurrent
        callers can't race past the caps.
        """
        ...

    async def record_bytes(self, reservation: QuotaReservation, response_bytes: int) -> None:
        """Attribute a completed response's byte size to its reservation."""
        ...


class InProcessQuotaLimiter:
    """Single-process rolling-window limiter — the default, correct only for
    one instance (see module docstring). Keeps a sliding-window log of
    per-key attempt timestamps and their response-byte weights; each
    `reserve()` first prunes entries older than `window_seconds`, so the
    window is genuinely rolling (not a coarse fixed bucket that resets on a
    boundary).
    """

    def __init__(self) -> None:
        self._windows: Dict[QuotaKey, List[_WindowEntry]] = {}

    def clear(self) -> None:
        """Reset all in-process quota state. Called once per test by the
        conftest autouse fixture (same rationale as the concurrency limiter's
        `clear()`), so a window populated by one test can't leak into the next.
        """
        self._windows.clear()

    def _prune(self, key: QuotaKey, cutoff: float) -> List[_WindowEntry]:
        entries = [e for e in self._windows.get(key, ()) if e.ts > cutoff]
        if entries:
            self._windows[key] = entries
        else:
            self._windows.pop(key, None)
        return entries

    async def reserve(
        self,
        key: QuotaKey,
        *,
        max_requests: Optional[int],
        max_response_bytes: Optional[int],
        window_seconds: int,
        now: Optional[float] = None,
    ) -> QuotaReservation:
        now = time.monotonic() if now is None else now
        entries = self._prune(key, now - window_seconds)

        def _retry_after() -> int:
            # Seconds until the oldest in-window attempt ages out and frees
            # capacity. entries is non-empty whenever a cap can trip (a
            # positive cap with a >= count/byte-total implies >= 1 entry).
            oldest = entries[0].ts if entries else now
            return max(1, math.ceil(window_seconds - (now - oldest)))

        if max_requests is not None and len(entries) >= max_requests:
            raise QuotaExceededError(
                f"request quota exceeded: at most {max_requests} queries per "
                f"{window_seconds}s window; retry in ~{_retry_after()}s",
                quota_kind="requests",
                retry_after_seconds=_retry_after(),
            )
        if max_response_bytes is not None:
            byte_total = sum(e.response_bytes for e in entries)
            if byte_total >= max_response_bytes:
                raise QuotaExceededError(
                    f"response-byte quota exceeded: at most {max_response_bytes} bytes per "
                    f"{window_seconds}s window; retry in ~{_retry_after()}s",
                    quota_kind="bytes",
                    retry_after_seconds=_retry_after(),
                )

        entry = _WindowEntry(now)
        self._windows.setdefault(key, []).append(entry)
        return QuotaReservation(entry)

    async def record_bytes(self, reservation: QuotaReservation, response_bytes: int) -> None:
        if reservation._entry is not None:
            reservation._entry.response_bytes = max(0, response_bytes)


_in_process_limiter = InProcessQuotaLimiter()
_active_limiter: QuotaLimiter = _in_process_limiter


def in_process_quota_limiter() -> InProcessQuotaLimiter:
    """The persistent in-process quota limiter — used by tests to seed/reset
    window state and by the conftest autouse fixture to clear it, regardless of
    which backend is active (mirrors `concurrency.in_process_limiter()`).
    """
    return _in_process_limiter


def init_redis_quota_limiter(limiter: QuotaLimiter) -> None:
    """Install the Redis-backed cross-replica quota limiter as active (item 50
    phase 2). Called from `create_app` when the Redis backend is selected,
    mirroring `concurrency.init_redis_limiter`."""
    global _active_limiter
    _active_limiter = limiter


def clear_redis_quota_limiter() -> None:
    """Revert to the in-process quota limiter (test teardown / shutdown)."""
    global _active_limiter
    _active_limiter = _in_process_limiter


def resolve_query_quota(policy: Policy) -> Optional[Tuple[Optional[int], Optional[int], int]]:
    """`(max_requests, max_response_bytes, window_seconds)` if this policy
    configures any quota, else `None` (feature disabled — no window is touched).
    """
    if not policy.query_quota_enabled:
        return None
    return (
        policy.max_requests_per_window,
        policy.max_response_bytes_per_window,
        policy.quota_window_seconds,
    )


async def enforce_query_quota(
    policy: Policy,
    *,
    connection_id: str,
    principal_subject: Optional[str],
) -> Optional[QuotaReservation]:
    """Reserve one quota slot before execution, or raise `QuotaExceededError`.

    Returns `None` (no reservation to record) when the quota is disabled for
    this policy, or when there's no authenticated principal to attribute usage
    to — an unattributable caller can't be rate-limited per principal, so the
    guard is skipped rather than applied to a shared anonymous bucket.
    """
    quota = resolve_query_quota(policy)
    if quota is None or principal_subject is None:
        return None
    max_requests, max_response_bytes, window_seconds = quota
    return await _active_limiter.reserve(
        (connection_id, principal_subject),
        max_requests=max_requests,
        max_response_bytes=max_response_bytes,
        window_seconds=window_seconds,
    )


async def record_query_quota_bytes(
    reservation: Optional[QuotaReservation], response_bytes: int
) -> None:
    """Attribute a completed response's byte size to its reservation; a no-op
    when the quota was disabled (`reservation is None`)."""
    if reservation is not None:
        await _active_limiter.record_bytes(reservation, response_bytes)
