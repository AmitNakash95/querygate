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

**Bounded by construction.** The reader (`audit.file_reader.iter_lines_reverse`,
TODO.md item 138) reads the file tail-first and stops once either the
retained-event cap (`max_events_scanned`) or a hard lines-read cap
(`max_lines_read`) is hit, so a long-running deployment's entire audit
history can never make one request allocate unbounded memory or do
unbounded work; the report also caps the number of principals and the
per-principal new-connection list, and flags `truncated` when either cap was
hit. TODO.md item 141 added a THIRD stop condition
(`max_consecutive_out_of_window`, see its own docstring) that does NOT set
`truncated` — it is a heuristic "the window has genuinely ended" exit, not a
resource bound, and it relies on an assumption (physical write order tracks
`occurred_at` order) that a merged/restored/multi-writer audit file can
violate. See `docs/THREAT_MODEL.md` QG-43 for the residual risk this leaves.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Literal, Optional, Protocol, Set, Tuple

import pydantic as pyd

from querygate.audit.events import AuditEvent
from querygate.audit.file_reader import AuditFileReadBounded, iter_lines_reverse
from querygate.audit.ledger import unwrap_envelope, verify_envelope_hash

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
    # Memory bound on how many in-window events one report retains.
    max_events_scanned: int = pyd.Field(default=200_000, ge=1)
    # TODO.md item 138: hard bound on total *lines read from disk*, independent
    # of how many are retained — bounds worst-case parse/validate work on an
    # oversized or adversarial file. The reader scans tail-first (newest
    # physical line first, since the sink only appends), so this cap is hit
    # only after every genuinely recent line has already been seen; it never
    # trades away the window's correctness the way capping a forward scan
    # from the start of the file would. Either cap can fire first — there's
    # no requirement that one exceed the other. Defaulted to match
    # `max_events_scanned` rather than far above it (an earlier default of
    # 2,000,000 was measured, per a 2026-08-01 security review, at up to
    # ~1.6 GB read and 10-40s of blocking work per request at realistic audit
    # line sizes — not a meaningful bound in practice); `iter_lines_reverse`'s
    # own `max_line_bytes`/`max_total_bytes` are the hard backstop beneath
    # this line-count budget.
    max_lines_read: int = pyd.Field(default=200_000, ge=1)
    # TODO.md item 141 (PRODUCT_GUIDE Decision Log): `audit/logger.py`'s
    # `_persist` now stamps `occurred_at` immediately before calling the
    # sink's `emit()` — NOT under the sink's own write lock (that stronger
    # version was considered and rejected, since it would break the audit
    # test suite's established pattern of seeding synthetic history via a
    # direct `sink.emit()` call with a controlled `occurred_at`). This closes
    # the dominant source of drift (arbitrary work — a `log.info` call, any
    # future code — between event construction and the durable write) for
    # every production caller, which today all run synchronously on the
    # single asyncio event loop with no `await` between construction and
    # `_persist`. The residual is bounded by however long a competing writer
    # holds the sink's lock (`os.open`/`os.write`/optional `os.fsync`), not
    # sub-microsecond jitter — negligible today only because of that
    # single-event-loop shape, not because of anything this tolerance itself
    # guarantees. Once this many consecutive `query.execution` lines in a row
    # are all at-or-before `window_start`, the scan stops WITHOUT setting
    # `truncated` — this is a heuristic "the window probably ended" exit, not
    # a proof: a merged/restored/multi-writer audit file (e.g. concatenated
    # replica ledgers, a restored WORM segment, `num_of_workers > 1` sharing
    # one file with no cross-process lock) can make physical order disagree
    # with `occurred_at` order at scale, in which case this exit can return a
    # confidently-wrong, undisclosed `truncated=False`. See
    # `docs/THREAT_MODEL.md` QG-43. Lines of other event types (config/
    # catalog governance, probes) don't reset or advance this counter; they
    # say nothing about this stream's recency.
    max_consecutive_out_of_window: int = pyd.Field(default=5_000, ge=1)
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

    # "jsonl"/"jsonl_chained": read the persisted stream under the actually
    # configured backend (0 events is still a "jsonl*" source, not an error;
    # TODO.md item 137 disclosed which backend, previously always "jsonl").
    # "disabled": no persisted sink is configured, nothing to read.
    source: Literal["jsonl", "jsonl_chained", "disabled"] = "jsonl"
    generated_at: str
    recent_window_seconds: float
    baseline_window_seconds: float
    events_scanned: int = 0
    malformed: int = 0
    # True when a resource bound (max_events_scanned, max_lines_read, or
    # max_principals_reported) was hit, so the report is a bounded view
    # rather than the whole picture. Does NOT cover the third, heuristic
    # early-exit (`max_consecutive_out_of_window`) — that one can end the
    # scan without setting this flag even when its ordering assumption is
    # violated; see `AnomalyThresholds.max_consecutive_out_of_window` and
    # `docs/THREAT_MODEL.md` QG-43.
    truncated: bool = False
    # TODO.md item 171: True when the scan stopped on the heuristic
    # `max_consecutive_out_of_window` early-exit (item 141) rather than
    # reaching the true start of the window — distinguishes "genuinely
    # complete" from "heuristically stopped early" for a caller-facing
    # consumer. Independent of `truncated`: a scan can end on this heuristic
    # without ever hitting a resource bound, or vice versa. See
    # `docs/THREAT_MODEL.md` QG-43 for the merged/multi-writer risk this
    # discloses.
    scan_ended_on_out_of_window_run: bool = False
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
    ) -> Tuple[List[AuditEvent], int, bool, bool]:
        """Return (events, malformed_line_count, truncated,
        scan_ended_on_out_of_window_run). `truncated` is True when the scan
        stopped on a resource bound — either `max_events_scanned` in-window
        events were already found, or `max_lines_read` lines were read —
        before it could be SURE no more recent-window events remained.
        `scan_ended_on_out_of_window_run` (TODO.md item 171) is a DIFFERENT,
        independent way the scan can end early: `thresholds.
        max_consecutive_out_of_window` (TODO.md item 141) is a heuristic exit
        that stops on the assumption the window has genuinely ended — an
        assumption a merged/restored/multi-writer audit file can violate. See
        `docs/THREAT_MODEL.md` QG-43."""
        ...


class JsonlAuditEventSource:
    """Reads `query.execution` events from the JSONL audit sink's file.

    Reads tail-first (`audit.file_reader.iter_lines_reverse`): since the sink
    only ever appends, the physically newest lines — which is what a
    recent-vs-baseline window needs — are seen before the oldest, so a hard
    cap on lines read (`max_lines_read`) bounds worst-case work without
    risking never reaching the window at all, the way capping a forward scan
    from the beginning of a large file would. Lines that aren't a valid
    `query.execution` event are counted as `malformed` and skipped, never
    fatal — the same tolerance as item 44's audit viewer.
    """

    def __init__(
        self, path: str, *, ledger_key: Optional[bytes] = None, require_envelope: bool = False
    ) -> None:
        self.path = Path(path)
        # TODO.md item 137: the hash-chained ledger's HMAC key, when the
        # configured backend is `jsonl_chained` with a key set — lets this
        # reader recompute each envelope's own hash rather than trusting it.
        # `None` on a plain `jsonl` backend, or an unkeyed chain.
        self.ledger_key = ledger_key
        # True only when the configured backend is `jsonl_chained`: every
        # persisted line MUST be a chain envelope, so a bare (non-enveloped)
        # line is itself evidence of tampering or corruption, not a
        # legitimate plain-`jsonl` line that happens to share this file
        # (found by `security-invariant-reviewer`, 2026-08-05 — a forged
        # line with no envelope at all previously passed through untouched,
        # since `verify_envelope_hash` correctly returns `None`, not `False`,
        # for "not shaped like an envelope").
        self.require_envelope = require_envelope

    def load_query_events(
        self, *, now: datetime, thresholds: AnomalyThresholds
    ) -> Tuple[List[AuditEvent], int, bool, bool]:
        window_start = now - timedelta(
            seconds=thresholds.recent_window_seconds + thresholds.baseline_window_seconds
        )
        kept: List[AuditEvent] = []
        malformed = 0
        lines_read = 0
        stopped_early = False
        ended_on_out_of_window_run = False
        consecutive_out_of_window = 0
        if not self.path.exists():
            return [], 0, False, False
        try:
            for line in iter_lines_reverse(self.path):
                if len(kept) >= thresholds.max_events_scanned:
                    stopped_early = True
                    break
                if lines_read >= thresholds.max_lines_read:
                    stopped_early = True
                    break
                lines_read += 1
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                verified = verify_envelope_hash(raw, key=self.ledger_key)
                if verified is False or (verified is None and self.require_envelope):
                    # False: a chain envelope whose own hash doesn't match its
                    # contents — chain linkage alone wouldn't catch this
                    # (TODO.md item 137). None-but-required: this backend is
                    # jsonl_chained, so a line with no envelope at all is
                    # itself the forgery/corruption signal. Never display
                    # either as a clean event.
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
                if occurred <= window_start:
                    # TODO.md item 141: a run of this length is treated as
                    # proof the window has genuinely ended (see
                    # `max_consecutive_out_of_window`'s docstring) — stop
                    # without setting `stopped_early`/`truncated`. TODO.md
                    # item 171: this IS disclosed separately, via
                    # `ended_on_out_of_window_run`.
                    consecutive_out_of_window += 1
                    if consecutive_out_of_window >= thresholds.max_consecutive_out_of_window:
                        ended_on_out_of_window_run = True
                        break
                    continue
                consecutive_out_of_window = 0
                if occurred > now:
                    continue
                kept.append(event)
        except AuditFileReadBounded:
            # An internal safety bound (oversized line, total-bytes budget, or
            # the file changing size mid-scan) fired before the scan reached
            # the start of the file — must not be reported as complete.
            stopped_early = True
        return kept, malformed, stopped_early, ended_on_out_of_window_run


def build_anomaly_report(
    source: Optional[AuditEventSource],
    *,
    now: Optional[datetime] = None,
    thresholds: Optional[AnomalyThresholds] = None,
    backend_label: Literal["jsonl", "jsonl_chained"] = "jsonl",
) -> AnomalyReport:
    """Assemble a full report from a source. `source=None` means the persisted
    sink is disabled — reported honestly as `source="disabled"`, not an error.
    `backend_label` (TODO.md item 137) is the actually configured backend, so a
    reader can tell a tamper-evident chain read from a plain one."""
    thresholds = thresholds or AnomalyThresholds()
    now = now or datetime.now(timezone.utc)
    base = dict(
        generated_at=now.isoformat(),
        recent_window_seconds=thresholds.recent_window_seconds,
        baseline_window_seconds=thresholds.baseline_window_seconds,
    )
    if source is None:
        return AnomalyReport(source="disabled", **base)

    events, malformed, scan_truncated, ended_on_out_of_window_run = source.load_query_events(
        now=now, thresholds=thresholds
    )
    principals, principal_truncated = detect_anomalies(events, now=now, thresholds=thresholds)
    return AnomalyReport(
        source=backend_label,
        events_scanned=len(events),
        malformed=malformed,
        truncated=scan_truncated or principal_truncated,
        scan_ended_on_out_of_window_run=ended_on_out_of_window_run,
        principals=principals,
        **base,
    )
