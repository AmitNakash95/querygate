"""Safe explanations of the caller's own recent denials (TODO.md item 45,
phase 2).

Item 45 phase 1 shipped the `/access/` "my access" portal (identity, visible
connections, effective guardrails, mandatory-filter readiness) but explicitly
deferred one thing: showing a caller *why* their own recent queries were
rejected. That data already exists — `AuditEvent` (`audit/events.py`), the
same event item 59's anomaly surfacing and item 31's admin audit viewer read —
but there was no principal-scoped read of it. This module builds that read.

**Why this reuses item 59's JSONL reader rather than writing a third one.**
`admin/anomaly.py`'s `JsonlAuditEventSource.load_query_events` already does the
exact I/O this needs: stream the audit JSONL file once into a bounded window,
transparently unwrap the hash-chained ledger envelope (item 91), tolerate
malformed lines. The *only* thing this module needs beyond that is a single
lookback window (no recent-vs-baseline comparison), so it constructs
`AnomalyThresholds` with a nominal `baseline_window_seconds` rather than
duplicating the file-parsing logic a third time (config_trends.py was the
second, for a genuinely different event pair, which justified its own reader —
this is the *same* event type and file, which does not).

**Self-service, not admin.** Unlike `admin/anomaly.py` (gated by
`admin:observability:read`), this is reached from `/help/my-recent-denials`,
which (like `/help/my-access`) requires only authentication — a caller sees
only their own denials, matched by `principal_id`, the same identity
`execution/service.py` already scopes policy resolution and delegated
attribution by. There is no path to another principal's events: the filter is
applied in-process immediately after the bounded read, before any response is
built, and the response model carries only what a caller could already infer
from having submitted the request themselves (occurred-at, connection,
surface, and a stable rejection-category label) — never another principal's
activity, a query value, or a table/column name beyond what the category
itself names generically.

**Redaction posture.** `error_category` is already a small, stable, redaction-
safe label set (`policy`/`schema`/`quota`/`cost_estimate`/`concurrency`/
`queue_full`/`approval_required`/`not_found`/`db_error`, `metrics.py`'s
`classify_rejection` plus `execution/service.py`'s `not_found` case) — the
same signal `admin/observability.py`'s `rejections_by_reason` already exposes
in aggregate to admins. This module attaches one fixed, human-readable
explanation per category and never surfaces `AuditEvent.query_shape` (which,
while already values-free, is more internal detail than a "why was this
rejected" explanation needs). `RecentDenialsReport.scan_ended_on_out_of_
window_run` (item 171) is the same category of field as the pre-existing
`truncated`: a scan-level fact about how far back the reader looked, never a
fleet-wide count or another principal's activity — safe on this no-admin-
scope surface for the same reason `truncated` already was.

**One deliberate exception: `operation="query_verdict"` events are excluded
entirely** (`_is_own_denial`), not just relabeled. `StructuredQueryService.
verdict` (TODO.md item 133) exists specifically to answer "would this be
allowed" without revealing whether a denial came from policy or schema; if
this module surfaced that same event's real `error_category` back to the
same caller, it would let the caller read back the answer verdict()'s
response deliberately withheld from them.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional, Tuple

import pydantic as pyd

from querygate.admin.anomaly import AnomalyThresholds, JsonlAuditEventSource
from querygate.audit.events import AuditEvent


class PersonalDenialsCooldown:
    """Per-principal rate limit for `GET /help/my-recent-denials` (TODO.md
    item 126). Every other caller of `admin/anomaly.py`'s
    `JsonlAuditEventSource` requires `admin:observability:read`; this
    endpoint requires only authentication, making its O(file-size) audit-JSONL
    scan the first such read triggerable by *any* authenticated caller. Mirrors
    `health.py`'s `HealthMonitor._last_manual_test`/
    `seconds_until_manual_test_allowed` cooldown shape (item 43), keyed by
    principal instead of connection. Deliberately a plain closure-scoped dict,
    not a registered variant — this is one stateless in-process cooldown, not
    a backend/dialect/strategy that varies (see CLAUDE.md's composable-
    interfaces "don't over-apply this" note).

    **Scope, disclosed (2026-08-10 `security-invariant-reviewer` finding on
    this item's own completion gate, mirroring `execution/quota.py`'s own
    disclosure for the identical shape):** correct for a single worker
    process. Under `AppConfig.num_of_workers > 1` or multiple replicas behind
    a load balancer, each process holds its own independent instance, so the
    effective ceiling is (workers × replicas) calls per window, not one — the
    same limitation `health.py`'s item-43 cooldown this mirrors already has,
    and the same one `execution/quota.py`'s in-process limiter documents and
    solves with an optional Redis-backed variant. This endpoint's traffic
    doesn't currently warrant that machinery; if it ever does, follow
    `execution/quota.py`'s `QuotaLimiter` Protocol shape rather than
    special-casing this class.

    **Bucket is `principal.subject`, not a per-human identity** (same
    finding; see `docs/THREAT_MODEL.md` QG-33, which already documents the
    identical coupling for this endpoint's *data* isolation): every
    statically configured API key maps to one shared `api_key_subject`
    (`core/auth.py`'s `ApiKeyAuthenticator`), so distinct callers sharing a
    key also share one cooldown bucket — a caller's request can 429 another
    holder of the same key. This only ever makes the limit stronger, never
    bypassable; JWT auth (a real per-caller `sub`) gets real per-caller
    granularity.
    """

    def __init__(self) -> None:
        self._last_request: Dict[str, float] = {}

    def seconds_until_allowed(self, principal_id: str, cooldown_seconds: float) -> float:
        """0 if a request is currently allowed for this principal, otherwise
        the remaining cooldown in seconds. `cooldown_seconds <= 0` always
        returns 0 (cooldown disabled)."""
        if cooldown_seconds <= 0:
            return 0.0
        last = self._last_request.get(principal_id)
        if last is None:
            return 0.0
        remaining = cooldown_seconds - (time.monotonic() - last)
        return max(0.0, remaining)

    def record_request(self, principal_id: str, cooldown_seconds: float) -> None:
        """Records now for `principal_id`, and prunes every entry whose own
        cooldown has already elapsed (TODO.md item 126 follow-up: an
        unpruned map grows one permanent entry per distinct principal ever
        seen, unbounded over the process lifetime — a real gap under a JWT
        deployment whose IdP mints many/per-session subjects). An expired
        entry is semantically identical to an absent one, so pruning here is
        behavior-preserving, not just cleanup. `cooldown_seconds <= 0`
        (disabled) records nothing at all — `seconds_until_allowed` never
        consults the map in that case, so retaining entries would only grow
        the map for no behavioral effect."""
        if cooldown_seconds <= 0:
            return
        now = time.monotonic()
        expired = [
            pid for pid, last in self._last_request.items() if now - last >= cooldown_seconds
        ]
        for pid in expired:
            del self._last_request[pid]
        self._last_request[principal_id] = now


_REPORT_NOTE = (
    "Your own recent rejected requests, read from the persisted audit stream. "
    "Never another principal's activity, a query value, or table/column "
    "content beyond what your own request already referenced."
)

_DENIAL_GUIDANCE: Dict[str, str] = {
    "policy": "A table/column/join limit in your effective policy rejected this request.",
    "schema": "The request referenced a table or column that doesn't exist, or isn't visible to you.",
    "quota": "You exceeded a configured rate or query quota.",
    "cost_estimate": "The estimated cost of this query exceeded the configured limit.",
    "concurrency": "The connection was at its concurrency limit when this request ran.",
    "queue_full": "The request queue was already at its configured depth.",
    "approval_required": "This request needs human-in-the-loop approval before it can run.",
    "not_found": "The referenced connection or resource does not exist, or isn't visible to you.",
    "db_error": "The database itself rejected or failed the request.",
}
_UNKNOWN_GUIDANCE = "This request was rejected; the specific reason wasn't recorded."


class DenialEvent(pyd.BaseModel):
    """One of the caller's own rejected requests. Never anything about any
    other principal, a query value, or a table/column identifier."""

    occurred_at: str
    connection: str
    surface: str
    reason: str
    explanation: str

    model_config = pyd.ConfigDict(extra="forbid")


class RecentDenialsReport(pyd.BaseModel):
    # "jsonl"/"jsonl_chained": read the persisted stream under the actually
    # configured backend (0 denials is still a "jsonl*" source, not an error;
    # TODO.md item 137 disclosed which backend, previously always "jsonl").
    # "disabled": no persisted sink is configured, nothing to read.
    source: Literal["jsonl", "jsonl_chained", "disabled"] = "jsonl"
    generated_at: str
    lookback_seconds: float
    # Deliberately scoped to the CALLER's own rejected requests found in the
    # window, before the `limit` cap — never a fleet-wide/cross-principal
    # count. This endpoint requires no admin scope (unlike admin/anomaly.py's
    # and admin/config_trends.py's siblings), so a raw "how many events did
    # the whole file hold" number would leak fleet-wide audit volume to every
    # authenticated caller; the underlying read is over every principal's
    # events, but nothing beyond this caller's own count is ever surfaced.
    own_denials_found: int = pyd.Field(
        default=0,
        description=(
            "Count of the caller's own rejected requests found in the scanned "
            "window, before any `limit` cap. Never a fleet-wide or another "
            "principal's count."
        ),
    )
    malformed: int = 0
    truncated: bool = pyd.Field(
        default=False,
        description=(
            "True when the underlying audit-stream scan hit its configured cap "
            "before finishing the window — this caller's own denial list may be "
            "incomplete as a result."
        ),
    )
    # TODO.md item 171: True when the scan stopped on the heuristic
    # max_consecutive_out_of_window early-exit (item 141) rather than
    # reaching the true start of the window — distinguishes "genuinely
    # complete" from "heuristically stopped early". Independent of
    # `truncated`. See `docs/THREAT_MODEL.md` QG-43.
    scan_ended_on_out_of_window_run: bool = pyd.Field(
        default=False,
        description=(
            "True when the scan stopped on a heuristic assumption that the "
            "window had genuinely ended, rather than reaching its true start "
            "— this caller's own denial list may be incomplete as a result, "
            "independent of `truncated`."
        ),
    )
    note: str = _REPORT_NOTE
    denials: List[DenialEvent] = pyd.Field(default_factory=list)

    model_config = pyd.ConfigDict(extra="forbid")


def _reason_and_explanation(error_category: Optional[str]) -> Tuple[str, str]:
    reason = error_category or "unknown"
    return reason, _DENIAL_GUIDANCE.get(reason, _UNKNOWN_GUIDANCE)


def _is_own_denial(event: AuditEvent, principal_id: str) -> bool:
    # `query_verdict` events (TODO.md item 133) are deliberately excluded:
    # `StructuredQueryService.verdict` collapses every denial into one
    # generic response specifically so a caller cannot learn whether policy
    # or schema rejected their query — surfacing this operation's own
    # `error_category` back through the caller's own denial history would
    # let the same caller reopen that exact channel by reading back the
    # probe they just submitted (see `execution/service.py`'s `verdict`
    # docstring and docs/THREAT_MODEL.md QG-34). Every other operation's
    # category is still safe to reveal here.
    return (
        event.principal_id == principal_id
        and event.outcome == "rejected"
        and event.operation != "query_verdict"
    )


def select_recent_denials(
    events: List[AuditEvent], *, principal_id: str, limit: int
) -> List[DenialEvent]:
    """Pure filter+format over already-loaded audit events: this caller's own
    rejected requests, most recent first, capped at `limit`. No file, clock,
    or global state — exhaustively unit-testable."""
    mine = [e for e in events if _is_own_denial(e, principal_id)]
    mine.sort(key=lambda e: e.occurred_at, reverse=True)
    denials: List[DenialEvent] = []
    for event in mine[:limit]:
        reason, explanation = _reason_and_explanation(event.error_category)
        denials.append(
            DenialEvent(
                occurred_at=event.occurred_at.isoformat(),
                connection=event.connection_id,
                surface=event.surface,
                reason=reason,
                explanation=explanation,
            )
        )
    return denials


def build_recent_denials_report(
    source: Optional[JsonlAuditEventSource],
    *,
    principal_id: str,
    now: Optional[datetime] = None,
    lookback_seconds: float = 86400.0,
    max_events_scanned: int = 50_000,
    max_lines_read: int = 50_000,
    max_consecutive_out_of_window: int = 5_000,
    limit: int = 20,
    backend_label: Literal["jsonl", "jsonl_chained"] = "jsonl",
) -> RecentDenialsReport:
    """Assemble a full report from a source. `source=None` means the persisted
    sink is disabled — reported honestly as `source="disabled"`, not an error.
    `backend_label` (TODO.md item 137) is the actually configured backend."""
    now = now or datetime.now(timezone.utc)
    base = dict(generated_at=now.isoformat(), lookback_seconds=lookback_seconds)
    if source is None:
        return RecentDenialsReport(source="disabled", **base)

    thresholds = AnomalyThresholds(
        # Only a single lookback window is needed here (no recent-vs-baseline
        # comparison, unlike item 59's own use of this reader), so
        # baseline_window_seconds is a nominal minimum rather than a real
        # second window — see the module docstring for why this reuses item
        # 59's reader instead of a third bespoke one.
        recent_window_seconds=lookback_seconds,
        baseline_window_seconds=1.0,
        max_events_scanned=max_events_scanned,
        # TODO.md item 138: kept independently tunable rather than inheriting
        # AnomalyThresholds' own default, since this surface is reachable
        # with authentication only, no admin scope, by design (item 45).
        max_lines_read=max_lines_read,
        # TODO.md item 141: same reasoning as max_lines_read above — kept
        # independently tunable rather than silently inheriting
        # AnomalyThresholds' own default (docs/THREAT_MODEL.md QG-43).
        max_consecutive_out_of_window=max_consecutive_out_of_window,
    )
    events, malformed, truncated, ended_on_out_of_window_run = source.load_query_events(
        now=now, thresholds=thresholds
    )
    denials = select_recent_denials(events, principal_id=principal_id, limit=limit)
    own_denials_found = sum(1 for e in events if _is_own_denial(e, principal_id))
    return RecentDenialsReport(
        source=backend_label,
        own_denials_found=own_denials_found,
        malformed=malformed,
        truncated=truncated,
        scan_ended_on_out_of_window_run=ended_on_out_of_window_run,
        denials=denials,
        **base,
    )
