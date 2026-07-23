"""Bounded reversibility for governed writes (TODO.md item 93 phase 3a).

QueryGate captures a bounded pre-image of a gated write into its **own** store
(never a shadow table in the customer's operational database — see the Decision
Log) and returns a `compensation_id`. `POST /write/undo` later re-applies the
inverse *through the governed write pipeline*, so an undo is itself a validated,
capped, audited write with no new privilege.

The store necessarily holds real row values (you cannot restore what you
redact), so it is a distinct, short-lived, access-controlled store — the
redaction-safe audit stream carries only the `compensation_id` + counts. Records
are bounded (a write over `max_compensation_rows` is executed *without* a
compensation record rather than snapshotting an unbounded set) and expire after
a TTL.

**Single-process limitation (loud, by design in phase 3a).** The default store is
**process-local**: a `compensation_id` minted in one worker/replica is invisible
to another, so `POST /write/undo` only succeeds on the same process that made the
write. Under more than one replica, undo therefore needs session affinity to that
replica, exactly like the per-replica audit ledger (see `deploy/HA_DR.md`). The
`CompensationStore` protocol is the seam for a durable cross-replica backend
(phase 3b), mirroring how `ConcurrencyLimiter`/quota gained a Redis variant — but
that also has to treat the pre-image values as sensitive at rest, so it is its own
piece of work, not a silent default. Until then: keep reversibility to
single-replica or affinity-routed deployments, or treat undo as best-effort.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol


@dataclass
class CompensationRecord:
    """Everything needed to undo one committed write by re-applying the inverse
    as a governed write. `pre_image` holds the affected rows' full pre-state
    (UPDATE/DELETE); `inserted_keys` holds the primary-key values added (INSERT)."""

    compensation_id: str
    connection_id: str
    table: str
    op: str  # the original op: insert | update | delete
    pk_column: str
    pre_image: List[Dict[str, Any]] = field(default_factory=list)
    inserted_keys: List[Any] = field(default_factory=list)
    # For UPDATE: exactly the columns the original write changed (its `set`
    # keys). Undo restores *only* these — never columns the write never touched —
    # so an undo's blast radius never exceeds the change it reverses.
    changed_columns: List[str] = field(default_factory=list)
    expires_at: float = 0.0
    consumed: bool = False


class CompensationStore(Protocol):
    """Async so a durable backend (Redis) can await its client — the in-process
    default is trivially async. Mirrors the concurrency/quota limiter seams."""

    async def put(self, record: CompensationRecord) -> None: ...

    async def get(self, compensation_id: str) -> Optional[CompensationRecord]: ...

    async def consume(self, compensation_id: str) -> None: ...


class InMemoryCompensationStore:
    """Process-local store. `get` returns None for a missing, expired, or already
    consumed record, so a stale or replayed undo is a clean no-op-with-error.
    Process-local, so undo needs single-replica/affinity — the durable
    cross-replica sibling is `RedisCompensationStore`."""

    def __init__(self) -> None:
        self._records: Dict[str, CompensationRecord] = {}

    async def put(self, record: CompensationRecord) -> None:
        self._records[record.compensation_id] = record

    async def get(self, compensation_id: str) -> Optional[CompensationRecord]:
        record = self._records.get(compensation_id)
        if record is None or record.consumed or record.expires_at < time.time():
            return None
        return record

    async def consume(self, compensation_id: str) -> None:
        record = self._records.get(compensation_id)
        if record is not None:
            record.consumed = True

    def clear(self) -> None:
        self._records.clear()


_active_store: CompensationStore = InMemoryCompensationStore()


def get_compensation_store() -> CompensationStore:
    return _active_store


def init_compensation_store(store: CompensationStore) -> None:
    """Install the active compensation store (e.g. Redis when the Redis backend
    is selected), mirroring `concurrency.init_redis_limiter`."""
    global _active_store
    _active_store = store


def reset_compensation_store() -> None:
    """Revert to a fresh in-process store (test teardown / shutdown)."""
    global _active_store
    _active_store = InMemoryCompensationStore()


def new_compensation_id() -> str:
    return uuid.uuid4().hex


def compensation_expiry(ttl_seconds: int, now: Optional[float] = None) -> float:
    return (time.time() if now is None else now) + ttl_seconds
