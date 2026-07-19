"""TODO item 32C: redaction-safe usage-signal recording, an in-process
buffer that keeps signal emission off the query execution path, and a
background monitor that batches buffered signals into the catalog file and
runs the usage learner (``catalog/learning.py``).

Recording (``record_usage_signals``) is a pure ``(CatalogStore) ->
CatalogStore`` transform, exactly like ``refresh_catalog_schema``/
``generate_catalog_drafts`` — callers persist the result through the same
``CatalogFileRepository`` lock. It is deliberately never called directly
from a request handler: ``CatalogFileRepository`` uses a blocking
cross-process ``flock`` + fsync, which is fine for infrequent governance
actions but would be a serious latency/contention regression on every
query. Instead, ``execution/service.py`` only ever calls
``enqueue_usage_signal`` (non-blocking, in-memory, best-effort), and
``CatalogUsageLearningMonitor`` periodically drains the buffer in one
batched write per connection per cycle.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from querygate.catalog.learning import generate_learned_relationship_proposals
from querygate.catalog.loader import CatalogStore
from querygate.catalog.models import (
    CatalogDraftObjectType,
    CatalogDraftTarget,
    CatalogEntryStatus,
    CatalogEvidence,
    CatalogEvidenceKind,
    CatalogUsageSignal,
    CatalogUsageSignalKind,
)
from querygate.catalog.repository import CatalogFileRepository, CatalogFileUpdate
from querygate.connections.registry import get_registry
from querygate.core.logging import get_logger
from querygate.metrics import (
    LEARNED_PROPOSALS_GENERATED_TOTAL,
    USAGE_SIGNAL_BUFFER_DROPPED_TOTAL,
    USAGE_SIGNALS_BUFFERED_TOTAL,
    USAGE_SIGNALS_RECORDED_TOTAL,
)

_MAX_USAGE_SIGNALS = 50_000  # mirrors SchemaCatalog.usage_signals' max_length


def hash_principal_partition(subject: Optional[str]) -> str:
    """Stable per-caller partition key, never the raw principal identity —
    the durable catalog file must not store caller identity even though the
    in-process audit log already does (``AuditEvent.principal_id``).
    """

    basis = subject if subject else "anonymous"
    return "sha256:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()


def build_usage_signal(
    *,
    connection_id: str,
    principal_subject: Optional[str],
    target: CatalogDraftTarget,
    kind: CatalogUsageSignalKind,
    schema_fingerprint: str,
    evidence_reference: str,
    observed_at: Optional[datetime] = None,
) -> CatalogUsageSignal:
    """Construct a validated signal with a deterministic id derived from the
    caller-supplied ``evidence_reference`` (e.g. a query's admission id) plus
    target/kind/principal identity — so replaying the *same* evidence
    reference is idempotent (see ``record_usage_signals``), while every
    distinct real occurrence (a fresh admission id per query) still counts
    as independent evidence for the learner.
    """

    principal_partition = hash_principal_partition(principal_subject)
    identity = "\x1f".join(
        [
            connection_id,
            principal_partition,
            kind.value,
            target.table,
            target.column or "",
            target.to_table or "",
            target.to_column or "",
            evidence_reference,
        ]
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return CatalogUsageSignal(
        signal_id=f"urn:querygate:catalog:usage:{digest}",
        connection_id=connection_id,
        principal_partition=principal_partition,
        target=target,
        kind=kind,
        schema_fingerprint=schema_fingerprint,
        observed_at=observed_at or datetime.now(timezone.utc),
        evidence=CatalogEvidence(kind=CatalogEvidenceKind.USAGE, reference=evidence_reference),
    )


def should_emit_signal(
    store: CatalogStore, *, connection_id: str, target: CatalogDraftTarget
) -> bool:
    """Anti-feedback-loop gate (TODO item 32C non-negotiable): only emit a
    usage signal when the target's *current* catalog knowledge is either
    absent (organic discovery) or human-verified. If the only reason an
    agent chose this table/relationship is our own unreviewed draft/stale
    guess, using it must never count as evidence that validates that guess
    — otherwise the learner could "confirm" its own unverified suggestions
    purely because an agent followed them.
    """

    table = store.get_table(connection_id, target.table)
    if table is None:
        return True

    if target.object_type == CatalogDraftObjectType.TABLE:
        provenance = table.provenance
    elif target.object_type == CatalogDraftObjectType.COLUMN:
        column = table.column(target.column) if target.column else None
        if column is None:
            return True
        provenance = column.provenance
    else:
        relationship = None
        for candidate in table.relationships:
            if (
                candidate.column.casefold() == (target.column or "").casefold()
                and candidate.to_table.casefold() == (target.to_table or "").casefold()
                and candidate.to_column.casefold() == (target.to_column or "").casefold()
            ):
                relationship = candidate
                break
        if relationship is None:
            return True
        provenance = relationship.provenance

    return provenance.status == CatalogEntryStatus.VERIFIED


@dataclass(frozen=True)
class CatalogUsageRecordUpdate:
    store: CatalogStore
    outcome: str  # "recorded" | "no_op"
    recorded_signal_ids: tuple[str, ...] = ()
    duplicate_signal_ids: tuple[str, ...] = ()


def record_usage_signals(
    store: CatalogStore, *, signals: Sequence[CatalogUsageSignal]
) -> CatalogUsageRecordUpdate:
    """Idempotently append usage signals (append-if-absent by ``signal_id``),
    evicting the oldest signals FIFO once at the bounded cap. Batches an
    entire drained buffer into one validated round trip rather than one
    per signal, so a periodic flush of many buffered signals costs one
    ``CatalogStore`` rebuild, not one per signal.
    """

    if not signals:
        return CatalogUsageRecordUpdate(store=store, outcome="no_op")

    raw = store.to_dict()
    signals_raw = raw.setdefault("usage_signals", [])
    existing_ids = {entry["signal_id"] for entry in signals_raw}

    recorded: list[str] = []
    duplicates: list[str] = []
    for signal in signals:
        if signal.signal_id in existing_ids:
            duplicates.append(signal.signal_id)
            continue
        signals_raw.append(signal.model_dump(mode="json", exclude_none=True))
        existing_ids.add(signal.signal_id)
        recorded.append(signal.signal_id)

    if len(signals_raw) > _MAX_USAGE_SIGNALS:
        del signals_raw[: len(signals_raw) - _MAX_USAGE_SIGNALS]

    if not recorded:
        return CatalogUsageRecordUpdate(
            store=store, outcome="no_op", duplicate_signal_ids=tuple(duplicates)
        )
    return CatalogUsageRecordUpdate(
        store=store.replace(raw),
        outcome="recorded",
        recorded_signal_ids=tuple(recorded),
        duplicate_signal_ids=tuple(duplicates),
    )


class UsageSignalSummary:
    """Bounded, aggregated-by-target projection — never a raw per-signal
    dump — used by both the REST introspection endpoint and the CLI.
    """

    __slots__ = (
        "kind",
        "target",
        "support",
        "signal_count",
        "first_observed_at",
        "last_observed_at",
    )

    def __init__(
        self,
        *,
        kind: CatalogUsageSignalKind,
        target: CatalogDraftTarget,
        support: int,
        signal_count: int,
        first_observed_at: datetime,
        last_observed_at: datetime,
    ) -> None:
        self.kind = kind
        self.target = target
        self.support = support
        self.signal_count = signal_count
        self.first_observed_at = first_observed_at
        self.last_observed_at = last_observed_at

    def as_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "connection_id": self.target.connection_id,
            "object_type": self.target.object_type.value,
            "table": self.target.table,
            "column": self.target.column,
            "to_table": self.target.to_table,
            "to_column": self.target.to_column,
            "support": self.support,
            "signal_count": self.signal_count,
            "first_observed_at": self.first_observed_at.isoformat(),
            "last_observed_at": self.last_observed_at.isoformat(),
        }


def summarize_usage_signals(store: CatalogStore, connection_id: str) -> list[UsageSignalSummary]:
    groups: dict[tuple, list[CatalogUsageSignal]] = defaultdict(list)
    for signal in store.iter_usage_signals(connection_id):
        key = (
            signal.kind,
            signal.target.object_type,
            signal.target.table.casefold(),
            (signal.target.column or "").casefold(),
            (signal.target.to_table or "").casefold(),
            (signal.target.to_column or "").casefold(),
        )
        groups[key].append(signal)

    summaries = [
        UsageSignalSummary(
            kind=signals[0].kind,
            target=signals[0].target,
            support=len({signal.principal_partition for signal in signals}),
            signal_count=len(signals),
            first_observed_at=min(signal.observed_at for signal in signals),
            last_observed_at=max(signal.observed_at for signal in signals),
        )
        for signals in groups.values()
    ]
    summaries.sort(
        key=lambda summary: (
            -summary.support,
            summary.kind.value,
            summary.target.table.casefold(),
        )
    )
    return summaries


class InProcessUsageSignalBuffer:
    """Bounded, non-blocking, per-connection-partitioned in-process buffer.

    Never touches the catalog file lock. Partitioning by connection here
    (not just in the persisted ``connection_id`` field) is what lets
    ``CatalogUsageLearningMonitor`` run one independent drain-and-flush task
    per connection, exactly like ``CatalogRefreshMonitor``, without one
    connection's drain racing another's.
    """

    def __init__(self, *, max_size_per_connection: int) -> None:
        self._max_size = max_size_per_connection
        self._by_connection: dict[str, deque] = defaultdict(deque)

    def enqueue(self, signal: CatalogUsageSignal) -> None:
        bucket = self._by_connection[signal.connection_id]
        if len(bucket) >= self._max_size:
            bucket.popleft()
            USAGE_SIGNAL_BUFFER_DROPPED_TOTAL.labels(connection=signal.connection_id).inc()
        bucket.append(signal)
        USAGE_SIGNALS_BUFFERED_TOTAL.labels(
            connection=signal.connection_id, kind=signal.kind.value
        ).inc()

    def drain(self, connection_id: str) -> list[CatalogUsageSignal]:
        bucket = self._by_connection.get(connection_id)
        if not bucket:
            return []
        drained = list(bucket)
        bucket.clear()
        return drained

    def size(self, connection_id: str) -> int:
        return len(self._by_connection.get(connection_id, ()))


_buffer: Optional[InProcessUsageSignalBuffer] = None


def get_usage_signal_buffer() -> InProcessUsageSignalBuffer:
    global _buffer
    if _buffer is None:
        from querygate.core.config import config

        _buffer = InProcessUsageSignalBuffer(
            max_size_per_connection=config.semantic_memory_usage_signal_buffer_size
        )
    return _buffer


def reset_usage_signal_buffer() -> None:
    """Test-only reset — mirrors ``execution/concurrency.py``'s per-test
    clearing of its own module-level state.
    """

    global _buffer
    _buffer = None


def enqueue_usage_signal(signal: CatalogUsageSignal) -> None:
    get_usage_signal_buffer().enqueue(signal)


class CatalogUsageLearningMonitor:
    """Independent, fail-open background job (TODO item 32C). Each cycle,
    per enabled connection: drain that connection's buffered signals and
    persist them in one batched write, then separately run the learner —
    two independent ``CatalogFileRepository`` writes rather than one, so a
    learner bug can never cause already-drained evidence to be lost (if the
    learner step fails, the persisted signals survive for the next cycle).
    """

    def __init__(self, *, catalog_file: str, interval_seconds: float) -> None:
        self._repository = CatalogFileRepository(catalog_file)
        self._interval = interval_seconds
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        connection_ids = [
            connection.id for connection in get_registry().list_public() if connection.enabled
        ]
        self._tasks = [
            asyncio.create_task(self._run_connection(connection_id))
            for connection_id in connection_ids
        ]

    async def stop(self) -> None:
        self._stop.set()
        if self._tasks:
            await asyncio.gather(*self._tasks)
        self._tasks = []

    async def run_once(self, connection_id: str):
        drained = get_usage_signal_buffer().drain(connection_id)
        if drained:

            async def _persist(store: CatalogStore) -> CatalogFileUpdate[CatalogUsageRecordUpdate]:
                update = record_usage_signals(store, signals=drained)
                return CatalogFileUpdate(update.store, update)

            record_update = await self._repository.update_async(_persist)
            if record_update.recorded_signal_ids:
                USAGE_SIGNALS_RECORDED_TOTAL.labels(
                    connection=connection_id, outcome="recorded"
                ).inc(len(record_update.recorded_signal_ids))
            if record_update.duplicate_signal_ids:
                USAGE_SIGNALS_RECORDED_TOTAL.labels(
                    connection=connection_id, outcome="duplicate"
                ).inc(len(record_update.duplicate_signal_ids))

        async def _learn(store: CatalogStore) -> CatalogFileUpdate:
            update = generate_learned_relationship_proposals(
                store, connection_id=connection_id, now=datetime.now(timezone.utc)
            )
            return CatalogFileUpdate(update.store, update)

        return await self._repository.update_async(_learn)

    async def _run_connection(self, connection_id: str) -> None:
        log = get_logger()
        while not self._stop.is_set():
            try:
                update = await self.run_once(connection_id)
                LEARNED_PROPOSALS_GENERATED_TOTAL.labels(
                    connection=connection_id, outcome=update.outcome
                ).inc()
                log.info(
                    "semantic_memory.usage_learning",
                    connection=connection_id,
                    outcome=update.outcome,
                    added_proposal_count=update.added_count,
                )
            except Exception as exc:
                # Schema-reflection/driver errors can contain credentials or
                # raw server text — retain only the exception type.
                log.error(
                    "semantic_memory.usage_learning_failed",
                    connection=connection_id,
                    error_type=type(exc).__name__,
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
