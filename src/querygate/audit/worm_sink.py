"""Compliance-grade WORM (Write-Once-Read-Many) audit retention (TODO.md item
134): an additional, immutable archival copy of the same redaction-safe
event bodies the local hash-chained ledger persists, targeting S3 Object
Lock.

This is a *different control* from item 91's tamper-evident local ledger,
not a replacement for it (see docs/THREAT_MODEL.md's residual on item 91):
the local chain proves nobody edited what was kept; this proves the events
can be *produced* on demand for a retention window even if the local file is
later rotated, truncated, or lost. `CompositeAuditSink` (audit/sinks.py)
composes both under one `AuditSinkBackend.JSONL_CHAINED_S3_WORM` — the local
file is completely unchanged by this module.

**Off the request path, by construction.** `S3WormAuditSink.emit()` (the
`AuditSink` Protocol method `audit/logger.py` calls synchronously from
inside `async def execute()`) only appends to an in-process buffer — it
never touches the network. `WormFlushMonitor` is a separate background task
(mirroring `catalog/usage.py`'s `CatalogUsageLearningMonitor`) that drains
the buffer on a timer and performs the real S3 PUT in a thread (boto3 has no
native async client), so a slow or unreachable S3 endpoint never adds
latency to a query.

**Fail-open, by deliberate decision (recorded in the PRODUCT_GUIDE Decision
Log), not fail-closed.** The local chain sink already captured the event
for tamper-evidence; a WORM archival failure is a compliance-retention
degradation, not a reason to make query execution depend on S3's
availability. A failed flush re-enqueues its batch (bounded by the same
buffer cap other buffered batches share, so a *sustained* outage still
eventually drops the oldest under the same visible metric every other
bounded buffer in this codebase uses) rather than silently discarding it,
and increments a dedicated metric an operator is expected to alert on.

**Object granularity is one batch ("segment") per flush, never one object
per event** (see the Decision Log for the full "why": Object Lock's
retain-until timestamp is set per PUT, so per-event objects would each
expire at a slightly different moment as they age out — segment objects
that were written and will therefore retire together keep the archive's
shape coherent instead of leaving arbitrarily scattered gaps).

**Redaction safety (non-negotiable #3) is unconditional here too**: this
module never constructs its own event body — it only ever serializes the
exact same `PersistableEvent` the local sinks already write, so anything
that would make a redaction slip permanent (WORM's whole point) is caught
by the same tests that guard the local sinks, not a second implementation
that could drift.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Deque, List, Optional

from querygate.audit.events import PersistableEvent
from querygate.core.logging import get_logger
from querygate.metrics import (
    AUDIT_WORM_BUFFER_DROPPED_TOTAL,
    AUDIT_WORM_EVENTS_ARCHIVED_TOTAL,
    AUDIT_WORM_FLUSH_FAILURES_TOTAL,
    AUDIT_WORM_FLUSHES_TOTAL,
)


class InProcessWormEventBuffer:
    """Bounded, non-blocking, single-writer-lock-free buffer. Not partitioned
    by connection (unlike `catalog/usage.py`'s buffer) — `AuditSink` is a
    single global sink shared by every connection, mirroring the local
    hash-chained file it composes with."""

    def __init__(self, *, max_size: int) -> None:
        self._max_size = max_size
        self._buffer: Deque[PersistableEvent] = deque()

    def enqueue(self, event: PersistableEvent) -> None:
        if len(self._buffer) >= self._max_size:
            self._buffer.popleft()
            AUDIT_WORM_BUFFER_DROPPED_TOTAL.inc()
        self._buffer.append(event)

    def requeue_front(self, events: List[PersistableEvent]) -> None:
        """Put a previously-drained batch back, oldest-first, for a retry on
        the next flush cycle — used when a flush fails, so a transient S3
        outage doesn't silently lose events. Still bounded by max_size, so a
        *sustained* outage degrades the same visible way every other bounded
        buffer in this codebase does (oldest dropped, metered), rather than
        growing unbounded — anything that doesn't fit back in is counted as
        a drop exactly like `enqueue`'s own eviction."""
        for event in reversed(events):
            if len(self._buffer) >= self._max_size:
                AUDIT_WORM_BUFFER_DROPPED_TOTAL.inc()
                continue
            self._buffer.appendleft(event)

    def drain(self) -> List[PersistableEvent]:
        drained = list(self._buffer)
        self._buffer.clear()
        return drained

    def size(self) -> int:
        return len(self._buffer)


_buffer: Optional[InProcessWormEventBuffer] = None


def get_worm_buffer() -> InProcessWormEventBuffer:
    global _buffer
    if _buffer is None:
        from querygate.core.config import config

        _buffer = InProcessWormEventBuffer(max_size=config.audit_worm_max_buffered_events)
    return _buffer


def reset_worm_buffer() -> None:
    """Test-only reset — mirrors `catalog/usage.py`'s identical per-test
    clearing of its own module-level buffer."""
    global _buffer
    _buffer = None


class S3WormAuditSink:
    """The `AuditSink` half — conforms to the same Protocol the local sinks
    do (`emit`/`close`), but `emit` only ever touches the in-process buffer.
    All real I/O lives in `WormFlushMonitor`, a separate object with its own
    lifecycle, the same split `CatalogUsageLearningMonitor` has from
    `enqueue_usage_signal`."""

    def __init__(self, buffer: Optional[InProcessWormEventBuffer] = None) -> None:
        # Defaults to the module-level singleton (mirrors
        # catalog/usage.py's enqueue_usage_signal()/CatalogUsageLearningMonitor
        # split — the enqueue side and the drain side reach the same buffer
        # independently, with zero explicit wiring needed by callers of
        # either) — but overridable for tests.
        self._buffer = buffer if buffer is not None else get_worm_buffer()

    def emit(self, event: PersistableEvent) -> None:
        self._buffer.enqueue(event)

    def close(self) -> None:
        # Buffer flush lifecycle is owned by WormFlushMonitor.start()/stop(),
        # not by this sink — mirrors the usage-signal buffer/monitor split.
        return None


def _segment_key(prefix: str, now: datetime) -> str:
    """A time-ordered, collision-resistant key. Timestamp-first so objects
    list in chronological order in any S3 browser/CLI; a monotonic
    microsecond suffix (not a random UUID) keeps same-instant flushes from
    two processes distinguishable while staying sortable."""
    stamp = now.strftime("%Y/%m/%d/%Y%m%dT%H%M%S")
    return f"{prefix.rstrip('/')}/{stamp}-{now.microsecond:06d}.jsonl"


class WormFlushMonitor:
    """Background job: drains the buffer on a timer and PUTs one Object-Lock
    -protected segment per non-empty drain. Independent start()/stop()
    lifecycle wired into the app's lifespan, exactly like
    `CatalogUsageLearningMonitor`/`CatalogRefreshMonitor`."""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str,
        region: str,
        retention_mode: str,
        retention_days: int,
        interval_seconds: float,
        buffer: Optional[InProcessWormEventBuffer] = None,
    ) -> None:
        if not bucket.strip():
            raise ValueError(
                "AUDIT_WORM_S3_BUCKET must be set when " "audit_sink_backend=jsonl_chained_s3_worm"
            )
        self._buffer = buffer if buffer is not None else get_worm_buffer()
        self._bucket = bucket
        self._prefix = prefix
        self._region = region or None
        self._retention_mode = retention_mode
        self._retention_days = retention_days
        self._interval = interval_seconds
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._client = None

    @property
    def is_running(self) -> bool:
        """Whether the background flush loop is actually scheduled — lets a
        caller (or a test) verify `start()` genuinely ran, as distinct from
        `flush_once()` working when called directly."""
        return self._task is not None

    def _get_client(self):
        # Constructed lazily (not in __init__) so importing this module, or
        # constructing a monitor with a fake bucket in a unit test, never
        # requires real AWS credentials to be resolvable.
        if self._client is None:
            import boto3

            self._client = boto3.client("s3", region_name=self._region)
        return self._client

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
        self._task = None
        # Best-effort final flush so a clean shutdown doesn't leave up to one
        # full interval's worth of events sitting unarchived — failure here
        # is the same fail-open posture as every other flush.
        await self.flush_once()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
            await self.flush_once()

    async def flush_once(self) -> None:
        drained = self._buffer.drain()
        if not drained:
            return
        AUDIT_WORM_FLUSHES_TOTAL.inc()
        body = ("\n".join(e.model_dump_json(exclude_none=True) for e in drained) + "\n").encode(
            "utf-8"
        )
        now = datetime.now(timezone.utc)
        key = _segment_key(self._prefix, now)
        retain_until = now + timedelta(days=self._retention_days)
        try:
            await asyncio.to_thread(
                self._get_client().put_object,
                Bucket=self._bucket,
                Key=key,
                Body=body,
                ObjectLockMode=self._retention_mode,
                ObjectLockRetainUntilDate=retain_until,
                ContentType="application/x-ndjson",
            )
            AUDIT_WORM_EVENTS_ARCHIVED_TOTAL.inc(len(drained))
        except Exception as exc:
            AUDIT_WORM_FLUSH_FAILURES_TOTAL.inc()
            get_logger().bind(func="worm_flush").error(
                "audit.worm.flush_failed",
                bucket=self._bucket,
                key=key,
                batch_size=len(drained),
                error=f"{type(exc).__name__}: {exc}",
            )
            # Fail-open (Decision Log): re-queue for a retry rather than
            # losing the batch outright on a transient S3 outage.
            self._buffer.requeue_front(drained)
