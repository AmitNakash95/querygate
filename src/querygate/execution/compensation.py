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
a TTL. The default store is in-process; the `CompensationStore` protocol leaves
room for a durable backend later, mirroring the audit-sink pattern.
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
    expires_at: float = 0.0
    consumed: bool = False


class CompensationStore(Protocol):
    def put(self, record: CompensationRecord) -> None: ...

    def get(self, compensation_id: str) -> Optional[CompensationRecord]: ...

    def consume(self, compensation_id: str) -> None: ...


class InMemoryCompensationStore:
    """Process-local store. `get` returns None for a missing, expired, or already
    consumed record, so a stale or replayed undo is a clean no-op-with-error."""

    def __init__(self) -> None:
        self._records: Dict[str, CompensationRecord] = {}

    def put(self, record: CompensationRecord) -> None:
        self._records[record.compensation_id] = record

    def get(self, compensation_id: str) -> Optional[CompensationRecord]:
        record = self._records.get(compensation_id)
        if record is None or record.consumed or record.expires_at < time.time():
            return None
        return record

    def consume(self, compensation_id: str) -> None:
        record = self._records.get(compensation_id)
        if record is not None:
            record.consumed = True

    def clear(self) -> None:
        self._records.clear()


_STORE = InMemoryCompensationStore()


def get_compensation_store() -> InMemoryCompensationStore:
    return _STORE


def new_compensation_id() -> str:
    return uuid.uuid4().hex


def compensation_expiry(ttl_seconds: int, now: Optional[float] = None) -> float:
    return (time.time() if now is None else now) + ttl_seconds
