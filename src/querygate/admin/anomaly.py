"""Read-only behavioral anomaly surfacing on the audit stream (TODO.md item 59).

Item 44's observability overview aggregates the in-process Prometheus registry
into fleet counters — "which reason rejects the most queries?". This module
answers a *different, per-principal* question from the durable audit stream
(item 23's persisted `query.execution` events): is one caller's recent behavior
unusual compared to its own recent baseline — a sudden order-of-magnitude
volume spike, a jump in its rejection rate, or reaching a connection it has
never touched before — even among queries policy *allowed*.

**Strictly within the 32C boundary (`CLAUDE.md`).** This is a read-only signal
for a human admin to look at. It computes nothing that feeds back into
enforcement: it never edits a policy, throttles, blocks, or influences query
execution. `detect_anomalies` is a pure function over a list of events; the
route that calls it only *reads* the audit file. There is no write path here at
all.

**Redaction posture.** Every field surfaced (`principal_id`, `connection_id`,
counts, rates, ratios) is already present on the persisted `AuditEvent` and
already browsable through item 31's admin audit viewer — this module carries no
SQL, predicate value, row, table, or column beyond what the event schema
(`audit/events.py`, itself redaction-safe by construction) already holds. It
reads only `event_type == "query.execution"` events and ignores their
`query_shape`.

**Bounded by construction.** The file is streamed once into a bounded deque, so
a long-running deployment's entire audit history can never make one request
allocate unbounded memory; the report caps the number of principals and the
per-principal new-connection list, and flags `truncated` when a cap was hit.
"""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Deque, Dict, List, Literal, Optional, Protocol, Set, Tuple

import pydantic as pyd

from querygate.audit.events import AuditEvent
from querygate.audit.ledger import unwrap_envelope

AnomalyKind = Literal["volume_spike", "rejection_rate_spike", "new_connection_access"]

_UNAUTHENTICATED = "<unauthenticated>"

_REPORT_NOTE = (
    "Per-principal read-only signal derived from the persisted audit stream. "
    "Each principal's recent window is compared against its own immediately "
    "preceding baseline window; this is a hint for a human to investigate, "
    "never an automatic throttle, block, or policy change. Rates are "
    "per-minute over each window. With a single-replica/local audit file this "
    "reflects only events this deployment persisted."
)


class AnomalyThresholds(pyd.BaseModel):
    """Tunable detection parameters. Defaults are deliberately conservative so
    the signal points at genuinely unusual behavior, not normal fluctuation."""

    # Recent window: the "now" being judged. Baseline window: the equal-or-
    # longer stretch immediately before it that "normal" is measured from.
    recent_window_seconds: float = pyd.Field(default=3600.0, gt=0)
    baseline_window_seconds: float = pyd.Field(default=86400.0, gt=0)
    # A principal needs at least this much baseline history before any signal
    # fires — otherwise a brand-new or barely-active caller trivially "spikes".
    min_baseline_events: int = pyd.Field(default=20, ge=1)
    # ...and at least this many recent events, so one or two queries never trip it.
    min_recent_events: int = pyd.Field(default=5, ge=1)
    # Recent per-second rate this many times the baseline rate = volume spike.
    volume_spike_ratio: float = pyd.Field(default=3.0, gt=1)
    # Absolute increase in rejection fraction (0..1) that counts as a spike.
    rejection_rate_delta: float = pyd.Field(default=0.3, gt=0, le=1)
    # Memory/time bound on how many in-window events one report will scan.
    max_events_scanned: int = pyd.Field(default=200_000, ge=1)
    # Report caps — keep one response bounded regardless of principal count.
    max_principals_reported: int = pyd.Field(default=100, ge=1)
    max_new_connections_per_principal: int = pyd.Field(default=10, ge=1)

    model_config = pyd.ConfigDict(extra="forbid")


class AnomalySignal(pyd.BaseModel):
    """One flagged deviation for a principal. Only aggregate counts/rates — never
    a query, value, table, or column."""

    kind: AnomalyKind
    recent_count: int
    baseline_count: int
    recent_rate_per_min: float
    baseline_rate_per_min: float
    # volume_spike: recent per-second rate / baseline per-second rate.
    ratio: Optional[float] = None
    # rejection_rate_spike: the two rejection fractions (0..1) being compared.
    recent_rejection_rate: Optional[float] = None
    baseline_rejection_rate: Optional[float] = None
    # new_connection_access: the connection id reached in the recent window that
    # the principal never touched in its baseline window.
    connection: Optional[str] = None

    model_config = pyd.ConfigDict(extra="forbid")


class PrincipalAnomaly(pyd.BaseModel):
    """A principal with at least one flagged signal, plus its window totals."""

    principal_id: str
    recent_events: int
    baseline_events: int
    signals: List[AnomalySignal]

    model_config = pyd.ConfigDict(extra="forbid")


class AnomalyReport(pyd.BaseModel):
    """Bounded, redaction-safe anomaly report over the audit stream."""

    # "jsonl": read the persisted stream (0 events is still "jsonl", not an
    # error). "disabled": no persisted sink is configured, nothing to read.
    source: Literal["jsonl", "disabled"] = "jsonl"
    generated_at: str
    recent_window_seconds: float
    baseline_window_seconds: float
    events_scanned: int = 0
    malformed: int = 0
    # True when a cap (max_events_scanned or max_principals_reported) was hit,
    # so the report is a bounded view rather than the whole picture.
    truncated: bool = False
    note: str = _REPORT_NOTE
    principals: List[PrincipalAnomaly] = pyd.Field(default_factory=list)

    model_config = pyd.ConfigDict(extra="forbid")


class _PrincipalAcc:
    """Mutable per-principal scratch over the two windows."""

    __slots__ = (
        "recent_total",
        "recent_rejected",
        "recent_connections",
        "baseline_total",
        "baseline_rejected",
        "baseline_connections",
    )

    def __init__(self) -> None:
        self.recent_total = 0
        self.recent_rejected = 0
        self.recent_connections: Set[str] = set()
        self.baseline_total = 0
        self.baseline_rejected = 0
        self.baseline_connections: Set[str] = set()


def _per_min(count: int, window_seconds: float) -> float:
    return (count / window_seconds) * 60.0 if window_seconds > 0 else 0.0


def _signal_score(signal: AnomalySignal) -> float:
    """A single comparable magnitude used only to rank which principals to keep
    when the report is capped. Not surfaced; ordering aid only."""
    if signal.kind == "volume_spike":
        return signal.ratio or 0.0
    if signal.kind == "rejection_rate_spike":
        recent = signal.recent_rejection_rate or 0.0
        baseline = signal.baseline_rejection_rate or 0.0
        return recent - baseline
    # A new connection is a categorical shape change; rank it above routine
    # fluctuation but below a large numeric spike.
    return 2.0


def detect_anomalies(
    events: List[AuditEvent],
    *,
    now: datetime,
    thresholds: AnomalyThresholds,
) -> Tuple[List[PrincipalAnomaly], bool]:
    """Pure detection over query-execution events. Returns the flagged
    principals (most-severe first) and whether the principal cap truncated them.

    Splits each principal's events into a recent window ``(now - recent, now]``
    and the baseline window immediately before it, then flags volume spikes,
    rejection-rate spikes, and newly-touched connections. Pure over its inputs —
    no file, clock, or global state — so it is exhaustively unit-testable.
    """
    recent_start = now - timedelta(seconds=thresholds.recent_window_seconds)
    baseline_start = recent_start - timedelta(seconds=thresholds.baseline_window_seconds)

    per: Dict[str, _PrincipalAcc] = {}
    for event in events:
        occurred = event.occurred_at
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
        if occurred <= baseline_start or occurred > now:
            continue
        principal = event.principal_id or _UNAUTHENTICATED
        acc = per.setdefault(principal, _PrincipalAcc())
        rejected = event.outcome == "rejected"
        if occurred > recent_start:
            acc.recent_total += 1
            if rejected:
                acc.recent_rejected += 1
            if event.connection_id:
                acc.recent_connections.add(event.connection_id)
        else:
            acc.baseline_total += 1
            if rejected:
                acc.baseline_rejected += 1
            if event.connection_id:
                acc.baseline_connections.add(event.connection_id)

    flagged: List[PrincipalAnomaly] = []
    for principal, acc in per.items():
        signals = _principal_signals(acc, thresholds)
        if not signals:
            continue
        flagged.append(
            PrincipalAnomaly(
                principal_id=principal,
                recent_events=acc.recent_total,
                baseline_events=acc.baseline_total,
                signals=signals,
            )
        )

    # Most-severe principal first; a principal's rank is its strongest signal.
    flagged.sort(
        key=lambda p: max(_signal_score(s) for s in p.signals),
        reverse=True,
    )
    truncated = len(flagged) > thresholds.max_principals_reported
    return flagged[: thresholds.max_principals_reported], truncated


def _principal_signals(acc: _PrincipalAcc, thresholds: AnomalyThresholds) -> List[AnomalySignal]:
    # Both windows must carry enough traffic for a comparison to mean anything.
    if (
        acc.baseline_total < thresholds.min_baseline_events
        or acc.recent_total < thresholds.min_recent_events
    ):
        return []

    recent_rate_min = _per_min(acc.recent_total, thresholds.recent_window_seconds)
    baseline_rate_min = _per_min(acc.baseline_total, thresholds.baseline_window_seconds)
    recent_rate_sec = acc.recent_total / thresholds.recent_window_seconds
    baseline_rate_sec = acc.baseline_total / thresholds.baseline_window_seconds

    signals: List[AnomalySignal] = []

    # Volume spike: normalized per-second rates, so unequal window lengths still
    # compare fairly. baseline_rate_sec is > 0 here (min_baseline_events >= 1).
    ratio = recent_rate_sec / baseline_rate_sec
    if ratio >= thresholds.volume_spike_ratio:
        signals.append(
            AnomalySignal(
                kind="volume_spike",
                recent_count=acc.recent_total,
                baseline_count=acc.baseline_total,
                recent_rate_per_min=recent_rate_min,
                baseline_rate_per_min=baseline_rate_min,
                ratio=ratio,
            )
        )

    # Rejection-rate spike: a jump in the *fraction* of this principal's queries
    # policy denied — probing/misconfiguration even when overall volume is flat.
    recent_rej = acc.recent_rejected / acc.recent_total
    baseline_rej = acc.baseline_rejected / acc.baseline_total
    if recent_rej - baseline_rej >= thresholds.rejection_rate_delta:
        signals.append(
            AnomalySignal(
                kind="rejection_rate_spike",
                recent_count=acc.recent_total,
                baseline_count=acc.baseline_total,
                recent_rate_per_min=recent_rate_min,
                baseline_rate_per_min=baseline_rate_min,
                recent_rejection_rate=recent_rej,
                baseline_rejection_rate=baseline_rej,
            )
        )

    # New-connection access: a shape change — the principal reached a connection
    # in the recent window it never touched across its whole baseline window.
    new_connections = sorted(acc.recent_connections - acc.baseline_connections)
    for connection in new_connections[: thresholds.max_new_connections_per_principal]:
        signals.append(
            AnomalySignal(
                kind="new_connection_access",
                recent_count=acc.recent_total,
                baseline_count=acc.baseline_total,
                recent_rate_per_min=recent_rate_min,
                baseline_rate_per_min=baseline_rate_min,
                connection=connection,
            )
        )

    return signals


class AuditEventSource(Protocol):
    """Narrow read-only seam over the persisted audit stream. Only query-
    execution events within the combined baseline+recent window are returned."""

    def load_query_events(
        self, *, now: datetime, thresholds: AnomalyThresholds
    ) -> Tuple[List[AuditEvent], int, bool]:
        """Return (events, malformed_line_count, truncated). `truncated` is True
        when more in-window events existed than `max_events_scanned`."""
        ...


class JsonlAuditEventSource:
    """Reads `query.execution` events from the JSONL audit sink's file.

    Streams the file once into a bounded deque, so a huge audit history never
    makes one report allocate memory proportional to the whole file. Lines that
    aren't a valid `query.execution` event are counted as `malformed` and
    skipped, never fatal — the same tolerance as item 44's audit viewer.
    """

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def load_query_events(
        self, *, now: datetime, thresholds: AnomalyThresholds
    ) -> Tuple[List[AuditEvent], int, bool]:
        window_start = now - timedelta(
            seconds=thresholds.recent_window_seconds + thresholds.baseline_window_seconds
        )
        kept: Deque[AuditEvent] = deque(maxlen=thresholds.max_events_scanned)
        matched = 0
        malformed = 0
        if not self.path.exists():
            return [], 0, False
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                raw = unwrap_envelope(raw)
                # Only query-execution events; other event types (config/catalog
                # governance, probes) share the file but aren't caller behavior.
                if not isinstance(raw, dict) or raw.get("event_type") != "query.execution":
                    continue
                try:
                    event = AuditEvent.model_validate(raw)
                except pyd.ValidationError:
                    malformed += 1
                    continue
                occurred = event.occurred_at
                if occurred.tzinfo is None:
                    occurred = occurred.replace(tzinfo=timezone.utc)
                if occurred <= window_start or occurred > now:
                    continue
                matched += 1
                kept.append(event)
        truncated = matched > thresholds.max_events_scanned
        return list(kept), malformed, truncated


def build_anomaly_report(
    source: Optional[AuditEventSource],
    *,
    now: Optional[datetime] = None,
    thresholds: Optional[AnomalyThresholds] = None,
) -> AnomalyReport:
    """Assemble a full report from a source. `source=None` means the persisted
    sink is disabled — reported honestly as `source="disabled"`, not an error."""
    thresholds = thresholds or AnomalyThresholds()
    now = now or datetime.now(timezone.utc)
    base = dict(
        generated_at=now.isoformat(),
        recent_window_seconds=thresholds.recent_window_seconds,
        baseline_window_seconds=thresholds.baseline_window_seconds,
    )
    if source is None:
        return AnomalyReport(source="disabled", **base)

    events, malformed, scan_truncated = source.load_query_events(now=now, thresholds=thresholds)
    principals, principal_truncated = detect_anomalies(events, now=now, thresholds=thresholds)
    return AnomalyReport(
        source="jsonl",
        events_scanned=len(events),
        malformed=malformed,
        truncated=scan_truncated or principal_truncated,
        principals=principals,
        **base,
    )
