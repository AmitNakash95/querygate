"""In-process async execution lifecycle store (TODO.md item 35 phase 3).

Backs the REST `queue_mode=async` contract: `POST .../query` returns `202`
immediately with an `admission_id`, `GET .../query/{admission_id}` polls this
store for the current state/result, and `POST .../query/{admission_id}/cancel`
requests cancellation through it. A record is created and the real
`StructuredQueryService.execute()` call is scheduled as a background
`asyncio.Task` before the `202` response is even built, so the caller's
`admission_id` is known synchronously up front rather than discovered only
once the background work has started.

**Single-process only for this pass** — like admission (item 35 phase 1) and
quota (item 50), which both shipped in-process first and grew a Redis-backed
cross-replica variant separately once the in-process shape was proven. A
deployment with multiple replicas behind a load balancer that doesn't
sticky-route a caller's `GET .../query/{admission_id}` back to the replica
that started the query will get a `404` from the replica that never heard of
it — a real, documented limitation, not a silently-assumed-away one. A
Redis-backed store (mirroring `execution/redis_concurrency.py`/
`redis_quota.py`'s pattern) is a natural, separately-scoped follow-up.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Literal, Optional

from sqlalchemy.ext.asyncio import AsyncEngine

from querygate.connections.dialects import cancel_session
from querygate.connections.models import DatabaseDialect
from querygate.core.exceptions import (
    PUBLIC_INTERNAL_ERROR,
    QueryCancellationNotEnabledError,
    QueryCancellationNotReadyError,
    public_error_message,
)
from querygate.core.logging import get_logger
from querygate.execution.admission import new_admission_id

log = get_logger()

AsyncExecutionState = Literal[
    "queued", "running", "completed", "failed", "cancel_requested", "cancelled"
]

# The coroutine a caller (a REST route) supplies to actually run the query.
# Receives this execution's already-known admission_id plus the two hooks
# `StructuredQueryService.execute()` accepts for the async lifecycle
# (`on_admitted`/`on_session_identifier`) — see that method's docstring.
ExecuteCoroFactory = Callable[
    [str, Callable[[int], Awaitable[None]], Callable[[str], None]], Awaitable[Any]
]


@dataclass
class AsyncExecutionRecord:
    admission_id: str
    connection_id: str
    principal_subject: Optional[str]
    state: AsyncExecutionState = "queued"
    queue_wait_ms: Optional[int] = None
    result: Optional[Any] = None
    error_message: Optional[str] = None
    error_admission_state: Optional[str] = None
    session_identifier: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)
    task: Optional["asyncio.Task[None]"] = None


class InProcessAsyncExecutionStore:
    """Keyed by admission_id. Records are mutated in place (state
    transitions, result/error) rather than replaced, so a concurrent
    `GET .../status` read always sees a real snapshot — each attribute write
    below is a single bytecode-level operation, safe under asyncio's
    single-threaded cooperative scheduling with no lock needed."""

    def __init__(self) -> None:
        self._records: Dict[str, AsyncExecutionRecord] = {}

    def put(self, record: AsyncExecutionRecord) -> None:
        self._records[record.admission_id] = record

    def get(self, admission_id: str) -> Optional[AsyncExecutionRecord]:
        return self._records.get(admission_id)

    def clear(self) -> None:
        self._records.clear()


_store = InProcessAsyncExecutionStore()


def async_execution_store() -> InProcessAsyncExecutionStore:
    return _store


def start_async_execution(
    connection_id: str,
    principal_subject: Optional[str],
    execute_coro_factory: ExecuteCoroFactory,
) -> AsyncExecutionRecord:
    """Create a new record (state `queued`), store it, and schedule the real
    execution as a background task — returning immediately with the
    already-known `admission_id` so the caller can build its `202` response
    without waiting on any of the work below."""
    admission_id = new_admission_id()
    record = AsyncExecutionRecord(
        admission_id=admission_id, connection_id=connection_id, principal_subject=principal_subject
    )
    async_execution_store().put(record)

    async def on_admitted(queue_wait_ms: int) -> None:
        record.state = "running"
        record.queue_wait_ms = queue_wait_ms

    def on_session_identifier(identifier: str) -> None:
        record.session_identifier = identifier

    async def _run() -> None:
        try:
            record.result = await execute_coro_factory(
                admission_id, on_admitted, on_session_identifier
            )
            # A cancel may have been requested and even reached the DB, but the
            # query finished anyway (a race the DB-level cancel lost) — report
            # what actually happened rather than forcing "cancelled" onto a
            # real result.
            record.state = "completed"
        except asyncio.CancelledError:
            # Only reachable while still `queued` (see request_cancel below) —
            # the wait for a concurrency slot was interrupted before any DB
            # session ever opened.
            record.state = "cancelled"
        except Exception as exc:  # noqa: BLE001 - reported via the record, not raised
            # Mirrors the synchronous path's mask_unexpected(): a non-actionable
            # exception may carry driver details, SQL, bind values, hostnames,
            # or filesystem paths, and this record is polled over REST by
            # whichever principal owns the admission_id, so it gets the same
            # public/masked reduction the sync path gets for free from its
            # mask_unexpected() wrapper — this background task runs outside
            # that wrapper's scope entirely.
            if public_error_message(exc) == PUBLIC_INTERNAL_ERROR:
                log.exception("async_execution.unexpected_error", admission_id=admission_id)
            record.error_message = public_error_message(exc)
            record.error_admission_state = getattr(exc, "admission_state", None)
            record.state = "cancelled" if record.state == "cancel_requested" else "failed"

    record.task = asyncio.ensure_future(_run())
    return record


async def request_cancel(
    record: AsyncExecutionRecord,
    *,
    allow_query_cancellation: bool,
    engine: AsyncEngine,
    dialect: DatabaseDialect,
) -> AsyncExecutionState:
    """Request cancellation of `record`'s query. Idempotent: cancelling an
    already-terminal record is a no-op that returns its current state
    unchanged. Raises `QueryCancellationNotEnabledError`/
    `QueryCancellationNotReadyError` for the two ways a RUNNING query's
    cancel can be refused — never attempted and left to fail on a DB
    permission error (2026-07-28 Decision Log).
    """
    if record.state == "queued":
        # No `await` between this check and `.cancel()`: asyncio's
        # single-threaded cooperative scheduling makes the two atomic with
        # respect to `_run` above, so `record.state` cannot have changed to
        # "running" between them.
        if record.task is not None:
            record.task.cancel()
        return record.state
    if record.state == "running":
        if not allow_query_cancellation:
            raise QueryCancellationNotEnabledError(
                "query cancellation is not enabled for this connection — set "
                "Policy.allow_query_cancellation after granting the required "
                "DB-level permission (Postgres: GRANT pg_signal_backend; "
                "MSSQL: ALTER ANY CONNECTION)"
            )
        if record.session_identifier is None:
            raise QueryCancellationNotReadyError(
                "this query has not yet opened a database session — try " "cancelling again shortly"
            )
        record.state = "cancel_requested"
        await cancel_session(engine, dialect, record.session_identifier)
        return record.state
    # Already terminal (completed/failed/cancelled) — idempotent no-op.
    return record.state
