"""Managed search over the WORM (S3 Object Lock) audit archive (TODO.md item
134 phase 2).

**Why this exists.** Phase 1 (`audit/worm_sink.py`) gives QueryGate a durable,
tamper-evident, long-retention copy of every audit event in S3 — but until
now the only way to *read* it back was reaching into the bucket directly with
an AWS credential outside QueryGate entirely. This module is the missing
QueryGate-native read: "show me every query against `pii_customers` in the
last 18 months" for a compliance/security reviewer, answerable even after the
*local* hash-chained file (`admin/anomaly.py`'s `JsonlAuditEventSource` and
friends) has long since rotated that window out. The local file and the WORM
archive answer genuinely different questions — recent operational signal vs.
a compliance-grade, multi-year retrievability guarantee — so this is a
distinct read path, not a rewrite of the existing one.

**What's searchable.** Every persisted event type (`PersistableEvent` in
`audit/events.py`: `query.execution`, `config.governance`,
`catalog.governance`, `connection.probe`) — the WORM archive holds whatever
the composite sink fanned into it, not only query executions. A request
filters on `event_type`, `connection_id`, and `principal_id` — the same
fields `admin/anomaly.py`'s `JsonlAuditEventSource` already filters on, kept
consistent so an operator moving between the local and archival readers finds
the same vocabulary. `start_time`/`end_time` are REQUIRED on every request —
there is no "search everything" mode.

**What this is not.** Not a query execution path, not a second way to reach
a customer database, not a catalog/schema surface (non-negotiable #4: one
database path) — it searches AUDIT EVENTS about queries, never row data. It
never decrypts, reconstructs, or exposes SQL text, predicate/literal values,
result rows, or credentials: every event returned is validated against the
exact same redaction-safe `AuditEvent`/`ConfigChangeEvent`/
`CatalogGovernanceEvent`/`ConnectionProbeEvent` models (`extra="forbid"`) the
local sinks already write — a returned event's TOP-LEVEL shape can never
carry more content than a local audit-browser read already could. This
module additionally screens each event's `query_shape` (a `Dict[str, Any]`
`extra="forbid"` cannot constrain) for the same forbidden-content markers
the top-level check catches, so a nested forgery is rejected the same way a
top-level one is — see `_contains_forbidden_content` below.

**Residual, not closed by this control (recorded 2026-08-06 by
`security-invariant-reviewer`, TODO.md item 154):** unlike the LOCAL
hash-chained sink, WORM segments are written unenveloped (`audit/worm_sink.py`
serializes the bare `PersistableEvent` body, not a `LedgerRecord`), so this
reader has no hash-chain to verify against — it can confirm a line matches a
known REDACTION-SAFE SHAPE, but not that QueryGate itself actually wrote it.
S3 Object Lock (COMPLIANCE mode) prevents deleting or overwriting an EXISTING
object; it does not prevent adding a NEW, schema-valid one. A principal
holding `s3:PutObject` on the archive prefix — necessarily including
QueryGate's own AWS role, since `WormFlushMonitor` needs that permission to
archive at all — could plant a fabricated, schema-valid segment that this
reader would return indistinguishably from a real one. Closing this fully
means enveloping/hash-chaining WORM segments the way the local sink already
does, which is a phase-1 WRITE-FORMAT change with a migration question for
already-archived segments — an explicit design decision, not something this
read-side module can decide unilaterally. Tracked as TODO.md item 154.

**Bounds — enforced, not advisory (`WormSearchBounds`).** A request outside
these is REJECTED (422, `QueryValidationError`) before any S3 call is made;
a request inside them that still can't finish scanning within one call is
TRUNCATED with a resumable `next_cursor` — the same "stop and disclose
honestly, never silently serve past a bound" posture
`admin/anomaly.py`'s `max_lines_read`/`max_events_scanned` already
established for the local reader:

- `max_window_seconds` — the requested `[start_time, end_time]` (closed —
  both ends inclusive, see `_matches`) cannot be wider than this (default
  ~2 years/730 days — deliberately wide enough to cover the "18 months
  back" scenario this feature exists for). Both a missing `start_time`/
  `end_time` and an over-wide window are rejected here, never silently
  narrowed or silently allowed through.
- `max_objects_scanned` — hard per-request cap on S3 `GetObject` calls, the
  real cost driver of a scan (each is a network round trip against a segment
  up to `AUDIT_WORM_MAX_BUFFERED_EVENTS` events large). Hit mid-scan, the
  response is truncated with a cursor to resume from exactly where it
  stopped — never a full-archive linear scan in one request.
- `request_timeout_seconds` — wall-clock budget for one request's S3 work,
  checked between (never mid-) object fetches and day-prefix listings, so a
  request degrades to a truncated, resumable page rather than hanging.
- `max_limit`/`default_limit` — page-size bounds on returned events.

**How the scan stays bounded without a full-bucket listing.** `WormFlushMonitor`
(`audit/worm_sink.py`'s `_segment_key`) names every segment
`<prefix>/YYYY/MM/DD/<timestamp>-<microseconds>.jsonl` — timestamp-first, so
S3's own lexicographic key ordering is chronological within a day. This
module exploits exactly that: it enumerates one `ListObjectsV2` call per
CALENDAR DAY in the requested (padded) window, using the day directory as the
`Prefix`, rather than one unbounded listing over the whole bucket/prefix. The
window is padded by slightly more than one flush interval on each side
(`flush_interval_seconds`) because a segment's key reflects when it was
FLUSHED, not each event's own `occurred_at` — an event just before midnight
can flush into the next day's directory. Padding only widens which day
directories get listed; every individual event is still filtered against the
caller's exact `[start_time, end_time]` by its own `occurred_at`; the
padding cannot leak an out-of-window event into the result.

**Cursor.** A self-describing resumption token (`day`/`key`/`line` + a
fingerprint of the request's own filters, base64-encoded, NOT a bearer
capability or a signed/encrypted value — a holder of the search scope can
already read everything a cursor could ever point at), so replaying a cursor
against DIFFERENT filters is rejected (422) rather than silently returning a
mismatched page. It cannot be used to escape the ISSUING request's own
declared bounds: `day` is re-validated against the current request's
(padded) window, `key` is only ever matched against a freshly-listed S3
prefix (never used as a raw key path), and every event is still re-filtered
by its own `occurred_at` against the caller's exact window regardless of
what the cursor claims — so a forged or hand-edited cursor can widen
nothing. It is a resumption hint, not a promise the archive is unchanged: if
the exact segment a cursor points at is gone (e.g. an operator's own S3
lifecycle policy expired it after the cursor was issued — WORM's Object Lock
prevents *deletion before* the retention date, not lifecycle expiry *after*
it), the scan resumes at the next key that sorts after it rather than
erroring.

**Scope.** Gated by `ADMIN_AUDIT_WORM_SEARCH_SCOPE` (`core/scopes.py`) — its
OWN scope, not `ADMIN_OBSERVABILITY_READ_SCOPE`. The WORM archive is at least
as sensitive as the local anomaly/observability reads (same redaction-safe
event fields), arguably more so: it is the durable, potentially multi-year
compliance copy, so a principal that can read today's in-process aggregates
should not automatically be able to search years of retained history. See
`core/scopes.py`'s comment on `ADMIN_AUDIT_WORM_SEARCH_SCOPE` and the
"Compliance Auditor" role bundle.

**Read-only, 32C-boundary-safe**, the same posture every other admin
observability read in this codebase holds: this module computes nothing that
feeds back into enforcement — no policy edit, no throttle, no block. It only
ever issues `ListObjectsV2`/`GetObject` against the configured bucket/prefix;
it has no S3 write permission requirement and performs no write.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import List, Literal, Optional, Tuple, get_args

import pydantic as pyd

from querygate.audit.events import PersistableEvent
from querygate.core.exceptions import QueryValidationError
from querygate.metrics import (
    AUDIT_WORM_SEARCH_OBJECTS_SCANNED_TOTAL,
    AUDIT_WORM_SEARCH_REQUESTS_TOTAL,
)


def _persistable_event_types() -> Tuple[str, ...]:
    """The `event_type` discriminator value each `PersistableEvent` member
    declares, read off the canonical union itself rather than hand-typed
    here — so this filter's allowed values can never silently drift behind
    a new member. `test_worm_search_event_type_covers_every_persistable_
    event_type` (tests/unit/test_worm_search.py) fails if they ever
    disagree; a new `PersistableEvent` variant automatically both parses
    (see `_EVENT_ADAPTER` below) and becomes filterable with zero edits
    here."""
    return tuple(
        get_args(member.model_fields["event_type"].annotation)[0]
        for member in get_args(PersistableEvent)
    )


WormSearchEventType = Literal[_persistable_event_types()]

# Parses each archived line into whichever PersistableEvent member it
# actually is — the SAME canonical union `audit/events.py` already defines
# and every local sink already writes, never a second, hand-duplicated
# union this module could let drift (api/admin_ui_routes.py's own
# `_AUDIT_EVENT_ADAPTER` reuses this identical union for the identical
# reason: one place decides which event shapes exist, not two). Pydantic
# v2's smart-mode union validation (the default for a plain, non-
# discriminated `Union`) tries each member in turn; every member's
# `extra="forbid"` means a line with an unrecognized `event_type` OR an
# extra/forbidden field matches none of them and the whole line is rejected
# — proven in tests/security/test_worm_search_redaction.py, not just
# asserted here.
_EVENT_ADAPTER: pyd.TypeAdapter = pyd.TypeAdapter(PersistableEvent)

# Defensive bound on lines read from a SINGLE object. QueryGate's own writer
# never produces a segment this large (bounded by AUDIT_WORM_MAX_BUFFERED_
# EVENTS, default 5,000) — this only guards worst-case work against a
# corrupted or adversarially large object reachable in the bucket. Hitting it
# TRUNCATES the result (see search_worm_archive) rather than silently
# dropping the remainder — an object this anomalous is exactly the case
# where an honest "did not finish reading this" matters most.
_MAX_LINES_PER_OBJECT = 500_000

# Defensive bound on a SINGLE object's byte size, checked via a lightweight
# HeadObject BEFORE the body is ever read into memory (unlike
# _MAX_LINES_PER_OBJECT above, which only bounds work AFTER the fetch). A
# legitimate segment is bounded by AUDIT_WORM_MAX_BUFFERED_EVENTS (default
# 5,000 events, each at most a few KB of JSON) — orders of magnitude below
# this. Guards against a corrupted or adversarially oversized object in the
# bucket causing an unbounded-memory read (security-invariant-reviewer,
# 2026-08-06, WS-5).
_MAX_OBJECT_BYTES = 64 * 1024 * 1024

# Keys `normalize_query_shape` (audit/events.py) never emits for a
# legitimately constructed event — `AuditEvent.query_shape` is a plain
# `Dict[str, Any]`, the one field on any PersistableEvent member whose
# INTERIOR `extra="forbid"` cannot constrain. A forged or corrupted line
# carrying one of these keys anywhere inside query_shape is rejected the
# same way a forbidden TOP-LEVEL field already is (security-invariant-
# reviewer, 2026-08-06, WS-2) — see `_contains_forbidden_content`.
_FORBIDDEN_QUERY_SHAPE_KEYS = frozenset(
    {
        "sql",
        "row_data",
        "rows",
        "connection_string",
        "password",
        "credential",
        "credentials",
        "secret",
        "value",
        "values",
        "literal",
        "intent",
        "params",
    }
)


def _contains_forbidden_content(node: object) -> bool:
    """Recursively screens a parsed `query_shape` for a denylisted key.
    `normalize_query_shape` never emits any of these keys for a
    legitimately constructed event, so this can only ever fire on a forged
    or corrupted line."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key.lower() in _FORBIDDEN_QUERY_SHAPE_KEYS:
                return True
            if _contains_forbidden_content(value):
                return True
        return False
    if isinstance(node, list):
        return any(_contains_forbidden_content(item) for item in node)
    return False


_NOTE = (
    "Managed search over the durable WORM (S3 Object Lock) audit archive — a "
    "distinct, long-retention copy from the local hash-chained file other "
    "admin observability reads use. Every returned event is the same "
    "redaction-safe body the local sinks write: no SQL, predicate value, "
    "row, or credential ever appears here."
)


class WormSearchBounds(pyd.BaseModel):
    """Server-enforced ceilings a request cannot exceed. A request outside
    these is REJECTED before any S3 call; a request inside them that can't
    finish in one call is TRUNCATED with a resumable cursor."""

    max_window_seconds: float = pyd.Field(gt=0)
    max_objects_scanned: int = pyd.Field(ge=1)
    default_limit: int = pyd.Field(ge=1)
    max_limit: int = pyd.Field(ge=1)
    request_timeout_seconds: float = pyd.Field(gt=0)

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _default_within_max(self) -> "WormSearchBounds":
        # A misconfigured default above the documented ceiling would silently
        # exceed the enforced page-size cap on every request that omits
        # `limit` explicitly, while still rejecting an explicit `limit` above
        # the same ceiling — an inconsistent, self-contradicting bound
        # (security-invariant-reviewer, 2026-08-06, WS-4).
        if self.default_limit > self.max_limit:
            raise ValueError(
                f"default_limit ({self.default_limit}) must not exceed max_limit "
                f"({self.max_limit})."
            )
        return self


class WormSearchResult(pyd.BaseModel):
    """Bounded, redaction-safe page of WORM-archived events."""

    # "disabled": the configured audit_sink_backend does not archive to S3 at
    # all — reported honestly, mirroring admin/anomaly.py's AnomalyReport,
    # never a hard error just because the feature isn't turned on here.
    source: Literal["s3_worm", "disabled"] = "s3_worm"
    generated_at: str
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    objects_scanned: int = 0
    events_scanned: int = 0
    malformed: int = 0
    # True whenever next_cursor is set — the scan of the requested window is
    # NOT yet complete (a safety bound fired, or the page simply filled),
    # exactly the "next_cursor implies truncated" invariant this module
    # maintains throughout.
    truncated: bool = False
    next_cursor: Optional[str] = None
    note: str = _NOTE
    events: List[PersistableEvent] = pyd.Field(default_factory=list)

    model_config = pyd.ConfigDict(extra="forbid")


@dataclass(frozen=True)
class _CursorState:
    day: date
    key: Optional[str]
    line: int


def _filters_fingerprint(
    start_time: datetime,
    end_time: datetime,
    event_type: Optional[str],
    connection_id: Optional[str],
    principal_id: Optional[str],
) -> str:
    payload = "|".join(
        [
            start_time.astimezone(timezone.utc).isoformat(),
            end_time.astimezone(timezone.utc).isoformat(),
            event_type or "",
            connection_id or "",
            principal_id or "",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _encode_cursor(day: date, key: Optional[str], line: int, fingerprint: str) -> str:
    payload = {"day": day.isoformat(), "key": key, "line": line, "fp": fingerprint}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str, fingerprint: str) -> _CursorState:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(raw)
        if payload.get("fp") != fingerprint:
            raise ValueError("cursor fingerprint does not match the current filters")
        day = date.fromisoformat(payload["day"])
        key = payload.get("key")
        line = int(payload.get("line", 0))
        if line < 0:
            raise ValueError("negative line offset")
        return _CursorState(day=day, key=key, line=line)
    except QueryValidationError:
        raise
    except Exception as exc:
        raise QueryValidationError(
            "Invalid search cursor: it does not match the current filters/time range, or the "
            "archive changed (e.g. a lifecycle policy expired a segment) since it was issued."
        ) from exc


def _validate_window(
    start_time: Optional[datetime], end_time: Optional[datetime], bounds: WormSearchBounds
) -> tuple[datetime, datetime]:
    if start_time is None or end_time is None:
        raise QueryValidationError(
            "WORM archive search requires both start_time and end_time — there is no "
            "unbounded 'search everything' mode."
        )
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)
    if end_time.tzinfo is None:
        end_time = end_time.replace(tzinfo=timezone.utc)
    if end_time <= start_time:
        raise QueryValidationError("end_time must be after start_time.")
    window_seconds = (end_time - start_time).total_seconds()
    if window_seconds > bounds.max_window_seconds:
        max_days = bounds.max_window_seconds / 86400.0
        raise QueryValidationError(
            f"Requested time range ({window_seconds / 86400.0:.1f} days) exceeds the maximum "
            f"searchable window of {max_days:.0f} days."
        )
    return start_time, end_time


def _validate_limit(limit: Optional[int], bounds: WormSearchBounds) -> int:
    if limit is None:
        return bounds.default_limit
    if limit < 1 or limit > bounds.max_limit:
        raise QueryValidationError(f"limit must be between 1 and {bounds.max_limit} (got {limit}).")
    return limit


def _matches(
    event: PersistableEvent,
    *,
    start_time: datetime,
    end_time: datetime,
    event_type: Optional[str],
    connection_id: Optional[str],
    principal_id: Optional[str],
) -> bool:
    occurred = event.occurred_at
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)
    if not (start_time <= occurred <= end_time):
        return False
    if event_type is not None and event.event_type != event_type:
        return False
    if connection_id is not None and getattr(event, "connection_id", None) != connection_id:
        return False
    if principal_id is not None and event.principal_id != principal_id:
        return False
    return True


async def _list_day_keys(
    client, bucket: str, day_prefix: str, *, deadline: float, max_keys: int
) -> Tuple[List[str], bool]:
    """Every key under one calendar day's prefix, in S3's own lexicographic
    (here: chronological, since segment keys are timestamp-first) order.

    Bounded two ways so a single, unusually large day directory can never
    make one request accumulate an unbounded key list or paginate past its
    wall-clock budget (security-invariant-reviewer, 2026-08-06, WS-6):
    `deadline` is checked before every `ListObjectsV2` page, and listing
    stops once `max_keys` keys are collected (the caller's
    `max_objects_scanned` bound — listing more keys than a request could
    ever fetch is pointless work). Returns `(keys, stopped_early)`; the
    caller must treat `stopped_early=True` as a truncation, since more keys
    for this exact day may still exist beyond what was listed.
    """
    keys: List[str] = []
    token: Optional[str] = None
    while True:
        if time.monotonic() >= deadline or len(keys) >= max_keys:
            return keys, True
        # MaxKeys bounds THIS page itself to the remaining budget, so a
        # single S3 page (which can return up to 1,000 keys by default)
        # can never alone push the accumulated list past max_keys before
        # the check above gets a chance to fire again.
        kwargs = {
            "Bucket": bucket,
            "Prefix": day_prefix,
            "MaxKeys": max(1, max_keys - len(keys)),
        }
        if token:
            kwargs["ContinuationToken"] = token
        resp = await asyncio.to_thread(client.list_objects_v2, **kwargs)
        keys.extend(obj["Key"] for obj in resp.get("Contents", []))
        if resp.get("IsTruncated"):
            token = resp.get("NextContinuationToken")
        else:
            break
    return keys, False


async def _object_size(client, bucket: str, key: str) -> int:
    resp = await asyncio.to_thread(client.head_object, Bucket=bucket, Key=key)
    return int(resp.get("ContentLength", 0))


async def _get_object_text(client, bucket: str, key: str) -> str:
    resp = await asyncio.to_thread(client.get_object, Bucket=bucket, Key=key)
    body = await asyncio.to_thread(resp["Body"].read)
    return body.decode("utf-8", errors="replace")


def _next_position_cursor(
    keys: List[str], idx: int, day: date, end_day: date, fingerprint: str
) -> Optional[str]:
    """Where the NEXT page should resume once object `keys[idx]` is fully
    consumed — the next key in this day if any, else the start of the next
    day if the window still covers it, else None (nothing left)."""
    if idx + 1 < len(keys):
        return _encode_cursor(day, keys[idx + 1], 0, fingerprint)
    next_day = day + timedelta(days=1)
    if next_day <= end_day:
        return _encode_cursor(next_day, None, 0, fingerprint)
    return None


async def search_worm_archive(
    *,
    bucket: str,
    prefix: str,
    region: str,
    flush_interval_seconds: float,
    start_time: Optional[datetime],
    end_time: Optional[datetime],
    event_type: Optional[WormSearchEventType] = None,
    connection_id: Optional[str] = None,
    principal_id: Optional[str] = None,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
    bounds: WormSearchBounds,
) -> WormSearchResult:
    """Search the S3 WORM archive for events matching the given filters
    within `[start_time, end_time]` (both required, inclusive). Raises
    `QueryValidationError` (422 at the transport edge) for any out-of-bound
    request; never makes an S3 call for a request it is about to reject.

    Callers should route through `build_worm_search_result` (below), which
    additionally handles the "WORM archiving isn't configured on this
    deployment at all" case honestly rather than raising.
    """
    if not bucket.strip():
        raise ValueError(
            "search_worm_archive requires a configured bucket; callers must check "
            "AuditSinkBackend.is_s3_worm_archived() first (see build_worm_search_result)."
        )

    try:
        start_time, end_time = _validate_window(start_time, end_time, bounds)
        limit = _validate_limit(limit, bounds)
        fingerprint = _filters_fingerprint(
            start_time, end_time, event_type, connection_id, principal_id
        )
        resume = _decode_cursor(cursor, fingerprint) if cursor else None

        # A segment's key reflects when it was FLUSHED, not each event's own
        # occurred_at, so pad the day range scanned by slightly more than one
        # flush interval on each side (module docstring). Every event is
        # still filtered by its own occurred_at below, so padding cannot
        # leak an out-of-window event into the result — it only widens which
        # day directories get listed.
        pad_days = max(1, math.ceil(flush_interval_seconds / 86400.0) + 1)
        start_day = start_time.astimezone(timezone.utc).date() - timedelta(days=pad_days)
        end_day = end_time.astimezone(timezone.utc).date() + timedelta(days=pad_days)

        if resume is not None:
            if resume.day < start_day or resume.day > end_day:
                raise QueryValidationError("Search cursor does not match the requested time range.")
            current_day = resume.day
            resume_key = resume.key
            resume_line = resume.line
        else:
            current_day = start_day
            resume_key = None
            resume_line = 0
    except QueryValidationError:
        AUDIT_WORM_SEARCH_REQUESTS_TOTAL.labels(outcome="rejected").inc()
        raise

    events: List[PersistableEvent] = []
    malformed = 0
    events_scanned = 0
    objects_scanned = 0
    deadline = time.monotonic() + bounds.request_timeout_seconds

    def _finalize(*, truncated: bool, next_cursor: Optional[str]) -> WormSearchResult:
        # Build the response FIRST, count the metric only once construction
        # actually succeeds — reversed order would double-count a request as
        # both "ok" and "error" if the model itself somehow failed to build
        # (security-invariant-reviewer, 2026-08-06, WS-7).
        result = WormSearchResult(
            source="s3_worm",
            generated_at=datetime.now(timezone.utc).isoformat(),
            start_time=start_time.isoformat(),
            end_time=end_time.isoformat(),
            objects_scanned=objects_scanned,
            events_scanned=events_scanned,
            malformed=malformed,
            truncated=truncated,
            next_cursor=next_cursor,
            events=events,
        )
        AUDIT_WORM_SEARCH_REQUESTS_TOTAL.labels(outcome="ok").inc()
        return result

    try:
        import boto3  # deliberately lazy — see audit/worm_sink.py's identical rationale

        client = boto3.client("s3", region_name=region or None)

        day = current_day
        while day <= end_day:
            if time.monotonic() >= deadline:
                return _finalize(
                    truncated=True, next_cursor=_encode_cursor(day, None, 0, fingerprint)
                )

            day_prefix = f"{prefix.rstrip('/')}/{day.strftime('%Y/%m/%d')}/"
            keys, listing_truncated = await _list_day_keys(
                client, bucket, day_prefix, deadline=deadline, max_keys=bounds.max_objects_scanned
            )
            # `listing_truncated` does NOT short-circuit here: the keys
            # already listed are still valid and must still be processed
            # (respecting max_objects_scanned/deadline exactly as before) —
            # only AFTER exhausting them, if the loop below didn't already
            # return for another reason, does an incomplete listing get
            # reported as truncated (below the `for` loop).

            start_index = 0
            if day == current_day and resume_key is not None:
                try:
                    start_index = keys.index(resume_key)
                except ValueError:
                    # The exact segment the cursor pointed at is gone (e.g.
                    # an operator's own S3 lifecycle policy expired it since
                    # the cursor was issued) — resume at the next key that
                    # sorts after it rather than erroring; a cursor is a
                    # resumption hint, not a promise the archive is
                    # byte-for-byte unchanged.
                    start_index = next((i for i, k in enumerate(keys) if k > resume_key), len(keys))

            for idx in range(start_index, len(keys)):
                key = keys[idx]

                if objects_scanned >= bounds.max_objects_scanned or time.monotonic() >= deadline:
                    return _finalize(
                        truncated=True, next_cursor=_encode_cursor(day, key, 0, fingerprint)
                    )

                # Size-check BEFORE the full body is ever read into memory —
                # a corrupted or adversarially oversized object is skipped,
                # not fully buffered (security-invariant-reviewer,
                # 2026-08-06, WS-5). Counted as scanned (an S3 call was
                # made) but never parsed; the scan truncates here so this
                # object's content is never silently treated as empty.
                size = await _object_size(client, bucket, key)
                objects_scanned += 1
                AUDIT_WORM_SEARCH_OBJECTS_SCANNED_TOTAL.inc()
                if size > _MAX_OBJECT_BYTES:
                    return _finalize(
                        truncated=True,
                        next_cursor=_next_position_cursor(keys, idx, day, end_day, fingerprint),
                    )

                text = await _get_object_text(client, bucket, key)
                lines = text.splitlines()

                consume_from = resume_line if (day == current_day and key == resume_key) else 0
                last_line = min(len(lines), _MAX_LINES_PER_OBJECT)
                for line_no in range(consume_from, last_line):
                    raw_line = lines[line_no]
                    if not raw_line.strip():
                        continue
                    events_scanned += 1
                    try:
                        parsed = json.loads(raw_line)
                        event = _EVENT_ADAPTER.validate_python(parsed)
                    except (json.JSONDecodeError, pyd.ValidationError):
                        malformed += 1
                        continue
                    query_shape = getattr(event, "query_shape", None)
                    if query_shape is not None and _contains_forbidden_content(query_shape):
                        # A forged/corrupted line that passed top-level
                        # schema validation but smuggles forbidden content
                        # inside query_shape (WS-2) — reject the whole line,
                        # never return it with the field merely dropped.
                        malformed += 1
                        continue
                    if not _matches(
                        event,
                        start_time=start_time,
                        end_time=end_time,
                        event_type=event_type,
                        connection_id=connection_id,
                        principal_id=principal_id,
                    ):
                        continue
                    events.append(event)
                    if len(events) >= limit:
                        resume_at = line_no + 1
                        if resume_at < len(lines):
                            next_cursor = _encode_cursor(day, key, resume_at, fingerprint)
                        else:
                            next_cursor = _next_position_cursor(
                                keys, idx, day, end_day, fingerprint
                            )
                        return _finalize(truncated=next_cursor is not None, next_cursor=next_cursor)

                if last_line < len(lines):
                    # The per-object line cap fired and the page wasn't
                    # already filled above — an anomalously large object for
                    # a legitimate segment (WS-3). Disclose the truncation
                    # honestly with a resumable cursor rather than silently
                    # dropping everything past the cap.
                    return _finalize(
                        truncated=True,
                        next_cursor=_encode_cursor(day, key, last_line, fingerprint),
                    )

            if listing_truncated:
                # Every key THIS call listed for the day was processed
                # without hitting another bound, but the listing itself was
                # cut short (WS-6) — more keys may exist for this exact day
                # beyond what was seen. Resume at the start of this day
                # rather than claiming it was fully enumerated.
                return _finalize(
                    truncated=True, next_cursor=_encode_cursor(day, None, 0, fingerprint)
                )

            day = day + timedelta(days=1)
    except Exception:
        AUDIT_WORM_SEARCH_REQUESTS_TOTAL.labels(outcome="error").inc()
        raise

    # Every day in [start_day, end_day] was scanned without hitting a
    # budget cap or filling the page — the requested window is fully
    # covered by this result; nothing left to page through.
    return _finalize(truncated=False, next_cursor=None)


def _bounds_from_config(cfg) -> WormSearchBounds:
    return WormSearchBounds(
        max_window_seconds=cfg.audit_worm_search_max_window_days * 86400.0,
        max_objects_scanned=cfg.audit_worm_search_max_objects_scanned,
        default_limit=cfg.audit_worm_search_default_limit,
        max_limit=cfg.audit_worm_search_max_limit,
        request_timeout_seconds=cfg.audit_worm_search_request_timeout_seconds,
    )


async def build_worm_search_result(
    cfg,
    *,
    start_time: Optional[datetime],
    end_time: Optional[datetime],
    event_type: Optional[WormSearchEventType] = None,
    connection_id: Optional[str] = None,
    principal_id: Optional[str] = None,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
) -> WormSearchResult:
    """Thin config-wiring wrapper — the single implementation both the REST
    route and (if reachable) an MCP tool call into, mirroring how
    `admin/anomaly.py`'s `build_anomaly_report` is the one place that reads
    `AppConfig` and decides `source="disabled"` honestly rather than the
    transport layer duplicating that decision.
    """
    # Validate the request shape UNCONDITIONALLY — even when WORM archiving
    # isn't enabled on this deployment — so "start_time/end_time are
    # required on every request" is a genuine, unconditional contract
    # rather than one that quietly relaxes into an honest-but-unvalidated
    # 200 when the backend happens to be disabled (claim-reviewer,
    # 2026-08-06). The bounds come from AppConfig fields that always have a
    # value regardless of which backend is active, so this is meaningful
    # even when disabled; search_worm_archive re-validates the identical
    # bounds when the backend IS enabled below, which is redundant but
    # harmless — validation is a pure function of its inputs.
    bounds = _bounds_from_config(cfg)
    _validate_window(start_time, end_time, bounds)
    _validate_limit(limit, bounds)

    if not cfg.audit_sink_backend.is_s3_worm_archived():
        return WormSearchResult(
            source="disabled", generated_at=datetime.now(timezone.utc).isoformat()
        )
    return await search_worm_archive(
        bucket=cfg.audit_worm_s3_bucket,
        prefix=cfg.audit_worm_s3_prefix,
        region=cfg.audit_worm_s3_region,
        flush_interval_seconds=cfg.audit_worm_flush_interval_seconds,
        start_time=start_time,
        end_time=end_time,
        event_type=event_type,
        connection_id=connection_id,
        principal_id=principal_id,
        limit=limit,
        cursor=cursor,
        bounds=bounds,
    )
