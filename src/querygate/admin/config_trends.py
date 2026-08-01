"""Config/catalog-change trend surfacing (TODO.md item 44, phase 2 slice).

Item 44 phase 1 aggregates the in-process Prometheus registry into query
volume/rejection/queue/cost-estimate trends — but it deliberately deferred a
second trend question: "is config-governance or catalog-governance change
*volume* rising, and by which action?" Those events (`ConfigChangeEvent`/
`CatalogGovernanceEvent` in `audit/events.py`) live in the persisted audit
stream, not the metrics registry, so this is a distinct read path — mirroring
item 59's `admin/anomaly.py` (`AuditEventSource` protocol +
`JsonlAuditEventSource`) rather than `admin/observability.py`'s registry read.

**Read-only, 32C-boundary-safe.** Same posture as `admin/anomaly.py`: a pure
aggregation over already-persisted, already-redaction-safe event fields
(`action`, `outcome`, counts) — nothing here feeds back into enforcement, and
it never edits a policy, config version, or catalog entry.

**Recent-vs-baseline, not "since process start".** Unlike item 44 phase 1's
metrics-registry snapshot (which resets on restart), the audit JSONL stream is
durable, so a real two-window rate comparison is meaningful here — the same
recent/baseline shape item 59 uses per-principal, applied fleet-wide to
change-event volume instead of per-caller query behavior.

**Redaction posture.** Every field surfaced (`action`, `outcome`, counts,
rates) is already present on `ConfigChangeEvent`/`CatalogGovernanceEvent` and
already browsable through item 31's admin audit viewer — this module carries
no version content, proposal text, or raw YAML beyond what those event
schemas (themselves redaction-safe by construction) already hold.

**Bounded by construction.** The file is streamed once into a bounded deque,
so a long-running deployment's entire audit history can never make one
request allocate unbounded memory; `truncated` flags when that cap was hit.

Deliberately still deferred (item 44's remaining phase-2 scope, unchanged by
this slice): time-window trend *charts* over stored history, and querying an
operator-configured external metrics backend. Both need a durable
time-series store QueryGate does not own; this slice needs none, because the
audit JSONL file already is durable history.
"""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Deque, Dict, List, Literal, Optional, Protocol, Tuple, Union

import pydantic as pyd

from querygate.audit.events import CatalogGovernanceEvent, ConfigChangeEvent
from querygate.audit.ledger import unwrap_envelope

ChangeEvent = Union[ConfigChangeEvent, CatalogGovernanceEvent]

_REPORT_NOTE = (
    "Fleet-wide config/catalog-governance change volume, read from the "
    "persisted audit stream: the recent window is compared against the "
    "immediately preceding baseline window. A read-only trend signal for a "
    "human admin — never wired into enforcement."
)


class ChangeTrendThresholds(pyd.BaseModel):
    """Tunable window sizes. Same recent/baseline shape as
    `admin.anomaly.AnomalyThresholds`, kept as its own model because this
    aggregation has no significance thresholds to tune — only window sizes
    and the scan cap."""

    recent_window_seconds: float = pyd.Field(default=3600.0, gt=0)
    baseline_window_seconds: float = pyd.Field(default=86400.0, gt=0)
    max_events_scanned: int = pyd.Field(default=200_000, ge=1)

    model_config = pyd.ConfigDict(extra="forbid")


class ActionCount(pyd.BaseModel):
    """One action bucket. Never anything beyond a stable action name + counts
    — the same redaction posture as the underlying event (no version content,
    no proposal text, no YAML)."""

    action: str
    success: int = 0
    rejected: int = 0

    model_config = pyd.ConfigDict(extra="forbid")


class ChangeWindowStat(pyd.BaseModel):
    total: int = 0
    rejected: int = 0
    rate_per_min: float = 0.0
    by_action: List[ActionCount] = pyd.Field(default_factory=list)

    model_config = pyd.ConfigDict(extra="forbid")


class ConfigCatalogChangeTrend(pyd.BaseModel):
    """Bounded, redaction-safe change-volume trend over the audit stream."""

    # "jsonl": read the persisted stream (0 events is still "jsonl", not an
    # error). "disabled": no persisted sink is configured, nothing to read.
    source: Literal["jsonl", "disabled"] = "jsonl"
    generated_at: str
    recent_window_seconds: float
    baseline_window_seconds: float
    events_scanned: int = 0
    malformed: int = 0
    truncated: bool = False
    note: str = _REPORT_NOTE

    config_recent: ChangeWindowStat = pyd.Field(default_factory=ChangeWindowStat)
    config_baseline: ChangeWindowStat = pyd.Field(default_factory=ChangeWindowStat)
    # recent per-second rate / baseline per-second rate; None when the
    # baseline window has no events to compare against.
    config_volume_ratio: Optional[float] = None

    catalog_recent: ChangeWindowStat = pyd.Field(default_factory=ChangeWindowStat)
    catalog_baseline: ChangeWindowStat = pyd.Field(default_factory=ChangeWindowStat)
    catalog_volume_ratio: Optional[float] = None

    model_config = pyd.ConfigDict(extra="forbid")


class _WindowAcc:
    __slots__ = ("total", "rejected", "by_action_success", "by_action_rejected")

    def __init__(self) -> None:
        self.total = 0
        self.rejected = 0
        self.by_action_success: Dict[str, int] = {}
        self.by_action_rejected: Dict[str, int] = {}

    def add(self, action: str, outcome: str) -> None:
        self.total += 1
        if outcome == "rejected":
            self.rejected += 1
            self.by_action_rejected[action] = self.by_action_rejected.get(action, 0) + 1
        else:
            self.by_action_success[action] = self.by_action_success.get(action, 0) + 1


def _finalize(acc: _WindowAcc, window_seconds: float) -> ChangeWindowStat:
    actions = sorted(set(acc.by_action_success) | set(acc.by_action_rejected))
    return ChangeWindowStat(
        total=acc.total,
        rejected=acc.rejected,
        rate_per_min=(acc.total / window_seconds) * 60.0 if window_seconds > 0 else 0.0,
        by_action=[
            ActionCount(
                action=action,
                success=acc.by_action_success.get(action, 0),
                rejected=acc.by_action_rejected.get(action, 0),
            )
            for action in actions
        ],
    )


def _volume_ratio(
    recent: ChangeWindowStat,
    baseline: ChangeWindowStat,
    thresholds: ChangeTrendThresholds,
) -> Optional[float]:
    # window_seconds is always > 0 (pydantic `gt=0`), so baseline.total == 0
    # is the only way baseline_rate lands at exactly 0.
    baseline_rate = baseline.total / thresholds.baseline_window_seconds
    if baseline_rate == 0:
        return None
    recent_rate = recent.total / thresholds.recent_window_seconds
    return recent_rate / baseline_rate


def build_change_trend(
    events: List[ChangeEvent],
    *,
    now: datetime,
    thresholds: ChangeTrendThresholds,
) -> Tuple[ChangeWindowStat, ChangeWindowStat, ChangeWindowStat, ChangeWindowStat]:
    """Pure aggregation over already-windowed events into
    ``(config_recent, config_baseline, catalog_recent, catalog_baseline)``.
    No file, clock (besides `now`), or global state — exhaustively
    unit-testable, mirroring `admin.anomaly.detect_anomalies`.
    """
    recent_start = now - timedelta(seconds=thresholds.recent_window_seconds)

    config_recent = _WindowAcc()
    config_baseline = _WindowAcc()
    catalog_recent = _WindowAcc()
    catalog_baseline = _WindowAcc()

    for event in events:
        occurred = event.occurred_at
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
        is_recent = occurred > recent_start
        if isinstance(event, ConfigChangeEvent):
            (config_recent if is_recent else config_baseline).add(event.action, event.outcome)
        else:
            (catalog_recent if is_recent else catalog_baseline).add(event.action, event.outcome)

    return (
        _finalize(config_recent, thresholds.recent_window_seconds),
        _finalize(config_baseline, thresholds.baseline_window_seconds),
        _finalize(catalog_recent, thresholds.recent_window_seconds),
        _finalize(catalog_baseline, thresholds.baseline_window_seconds),
    )


class ChangeEventSource(Protocol):
    """Narrow read-only seam over the persisted audit stream — mirrors
    `admin.anomaly.AuditEventSource`, but returns config/catalog governance
    events instead of query-execution events."""

    def load_change_events(
        self, *, now: datetime, thresholds: ChangeTrendThresholds
    ) -> Tuple[List[ChangeEvent], int, bool]:
        """Return (events, malformed_line_count, truncated). `truncated` is
        True when more in-window events existed than `max_events_scanned`."""
        ...


class JsonlChangeEventSource:
    """Reads `config.governance`/`catalog.governance` events from the JSONL
    audit sink's file. Same bounded-deque + chain-envelope-unwrap shape as
    `admin.anomaly.JsonlAuditEventSource`."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def load_change_events(
        self, *, now: datetime, thresholds: ChangeTrendThresholds
    ) -> Tuple[List[ChangeEvent], int, bool]:
        window_start = now - timedelta(
            seconds=thresholds.recent_window_seconds + thresholds.baseline_window_seconds
        )
        kept: Deque[ChangeEvent] = deque(maxlen=thresholds.max_events_scanned)
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
                if not isinstance(raw, dict):
                    continue
                event_type = raw.get("event_type")
                if event_type not in ("config.governance", "catalog.governance"):
                    continue
                try:
                    event: ChangeEvent = (
                        ConfigChangeEvent.model_validate(raw)
                        if event_type == "config.governance"
                        else CatalogGovernanceEvent.model_validate(raw)
                    )
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


def build_change_trend_report(
    source: Optional[ChangeEventSource],
    *,
    now: Optional[datetime] = None,
    thresholds: Optional[ChangeTrendThresholds] = None,
) -> ConfigCatalogChangeTrend:
    """Assemble a full report from a source. `source=None` means the persisted
    sink is disabled — reported honestly as `source="disabled"`, not an error."""
    thresholds = thresholds or ChangeTrendThresholds()
    now = now or datetime.now(timezone.utc)
    base = dict(
        generated_at=now.isoformat(),
        recent_window_seconds=thresholds.recent_window_seconds,
        baseline_window_seconds=thresholds.baseline_window_seconds,
    )
    if source is None:
        return ConfigCatalogChangeTrend(source="disabled", **base)

    events, malformed, truncated = source.load_change_events(now=now, thresholds=thresholds)
    config_recent, config_baseline, catalog_recent, catalog_baseline = build_change_trend(
        events, now=now, thresholds=thresholds
    )
    return ConfigCatalogChangeTrend(
        source="jsonl",
        events_scanned=len(events),
        malformed=malformed,
        truncated=truncated,
        config_recent=config_recent,
        config_baseline=config_baseline,
        config_volume_ratio=_volume_ratio(config_recent, config_baseline, thresholds),
        catalog_recent=catalog_recent,
        catalog_baseline=catalog_baseline,
        catalog_volume_ratio=_volume_ratio(catalog_recent, catalog_baseline, thresholds),
        **base,
    )
