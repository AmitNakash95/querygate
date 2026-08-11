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

**Partially fixed (TODO.md item 154), honestly scoped against
`docs/THREAT_MODEL.md` QG-40:** WORM segments are now enveloped and
per-segment hash-chained (`audit/worm_sink.py`'s `_build_segment_body`), the
same `LedgerRecord` envelope the local `HashChainedAuditSink` uses. This
reader verifies each record's own hash before ever unwrapping it
(`verify_envelope_hash`, `unwrap_envelope`) — a line that isn't
envelope-shaped at all is `malformed`; an envelope-shaped line whose hash
doesn't recompute is `unverified` (kept distinct from `malformed`, since the
far more likely cause in practice is a rotated/mismatched
`AUDIT_LEDGER_HMAC_KEY`, not tampering — disclosed in the response `note`).
**Forgery-resistant only when a key is configured** — the default unkeyed
chain is a public SHA-256 function anyone with `s3:PutObject` can compute,
so it catches corruption and careless forgery, not a deliberate one (see
`audit/worm_sink.py`'s module docstring for the full statement). **One
residual, not closed by this control:** a genuine segment can still be
silently withheld from a listing (left to the existing S3-listing/Object-Lock
posture). **Partially fixed further (TODO.md item 172):** this reader now
also verifies each segment's internal CHAIN linkage (`seq`/`prev_hash`
continuity across consumed records), not just each record's own hash — a
record dropped from or reordered within the middle of a segment breaks the
chain and stops that segment's scan there, counted both in `unverified` and
in a distinct `chain_breaks` field (a stronger signal than an ordinary hash
mismatch, since the record itself is otherwise self-consistent). **Still
open (interior omission only, not every omission):** a genuine,
individually-valid, fully-intact segment can still be COPIED WHOLESALE to a
second S3 key and returned twice — undetectable by a linkage check alone,
since a full copy is itself a valid chain; closing this needs a further
design decision (binding a segment to its own object key) recorded as its
own follow-up. Records dropped from the TAIL of a segment (not the middle)
are ALSO still undetectable: the surviving prefix is a perfectly valid
chain on its own (`seq 0..k-1`, every link intact), and — unlike the local
`audit/ledger.py` reader's `verify_chain`, which has an `expected_head`
parameter specifically to catch records dropped from the end of a file —
no per-segment head anchor exists outside the object itself to compare
against (WS-172-3, security-invariant-reviewer, 2026-08-10). **No
legacy-segment migration question**:
this feature has no production deployment predating
this fix, so the reader requires an envelope unconditionally rather than
supporting both shapes indefinitely — a bare line is treated as `malformed`,
matching this module's existing "reject unenveloped/unverifiable, never
leak" posture for every other forgery class below.

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
  checked between object fetches and day-prefix listings, AND periodically
  (every 1,000 lines) inside a single object's own line loop (TODO.md item
  154, security-invariant-reviewer WS-154-4 — per-line envelope verification
  raised the cost of that loop enough that a single anomalous object at the
  line cap could otherwise block uninterrupted past this bound), so a
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
from querygate.audit.ledger import (
    GENESIS_PREV_HASH,
    resolve_ledger_key,
    unwrap_envelope,
    verify_envelope_hash,
)
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
    # Not JSON, not envelope-shaped at all, or the unwrapped body fails
    # PersistableEvent schema validation — garbage/corruption, independent
    # of any key.
    malformed: int = 0
    # TODO.md item 154: envelope-shaped (has seq/prev_hash/event/hash) but
    # the hash does not recompute under the configured ledger_key — either
    # genuine tampering, or (the far more likely cause in practice) the
    # archive was written under a DIFFERENT AUDIT_LEDGER_HMAC_KEY than the
    # one this search is verifying against (a rotated or newly-set key).
    # Kept distinct from `malformed` so a caller isn't left reading a
    # silently-empty, "nothing happened"-looking result when the real cause
    # is a key mismatch — see the `note` field, which names this explicitly
    # when non-zero. TODO.md item 172: ALSO includes the one breaking line
    # of each chain-break counted in `chain_breaks` below — that line's own
    # hash DID recompute, so `unverified - chain_breaks` (not this field
    # alone) is the count actually likely to be a key mismatch; see the
    # `note` field's two separate sentences.
    unverified: int = 0
    # TODO.md item 172: a genuine segment can still be copied to a second S3
    # key (returned twice) or have interior records dropped/reordered
    # without EITHER of the two counts above ever firing — each surviving
    # record's own hash still recomputes on its own, since neither field
    # checks a record's LINK to its predecessor within the segment (`seq`
    # continuity, `prev_hash` continuity). This counts SEGMENTS (not lines)
    # where that link broke within the scanned window: the breaking line
    # itself is also counted in `unverified` above (kept a real signal, not
    # silently dropped), but unlike an ordinary hash mismatch — usually a
    # rotated `AUDIT_LEDGER_HMAC_KEY`, see `unverified`'s own comment — a
    # broken LINK on an otherwise self-consistent record is a stronger
    # tamper/omission signal, so it is disclosed separately rather than
    # folded into the same "probably just a key rotation" explanation. No
    # further records from a broken segment are returned past the break
    # (see the `note` field, and `docs/THREAT_MODEL.md` QG-40 for the
    # residual this does NOT close: detecting a whole segment duplicated to
    # a second key, which still needs a segment-to-object-key binding —
    # deliberately out of scope here, see the item-172 Decision Log entry).
    chain_breaks: int = 0
    # TODO.md item 172 follow-up (WS-172-2, security-invariant-reviewer,
    # 2026-08-10): fail-closed on a broken chain means every remaining line
    # in the SAME object past the break is never read, however many genuine,
    # individually hash-verifiable records they might hold — the safer
    # posture (a record's claimed position past a break can't be trusted),
    # but leaving the SCALE of that undisclosed would let a single corrupt
    # or key-mismatched line silently cost far more than itself. This sums,
    # across every broken object in the scan, the number of raw lines
    # between the break and that object's own EFFECTIVE line cap
    # (`_MAX_LINES_PER_OBJECT` or the object's true length, whichever is
    # smaller) that were never read as a result. Not a precise count of
    # suppressed RECORDS (some of those lines could themselves be blank or
    # malformed), and — the one case where even this line count can
    # undercount — an object whose true length exceeds
    # `_MAX_LINES_PER_OBJECT` may hold further lines past the cap that
    # neither this field nor a break accounts for; that gap is the
    # pre-existing, unrelated line-cap truncation this field does not
    # attempt to describe.
    lines_skipped_after_chain_break: int = 0
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


# TODO.md item 172 follow-up (WS-172-7 secondary point / WS-172-8,
# security-invariant-reviewer, 2026-08-10/11): bounds `_seed_chain_state_from_
# predecessor`'s backward walk — see that function's own docstring.
_SEED_WALK_MAX_STEPS = 1024


def _seed_chain_state_from_predecessor(
    lines: List[str], consume_from: int, *, ledger_key: Optional[bytes]
) -> Tuple[Optional[str], Optional[int], bool]:
    """Seed a resumed page's chain-linkage state (TODO.md item 172,
    WS-172-1) from the nearest preceding non-blank line that itself
    verifies, so the first record actually consumed on a resumed page still
    has its incoming link checked like every other record — otherwise every
    ordinary page boundary the server itself issues would silently exempt
    one link per page. `consume_from`'s predecessor line is NOT in a
    different file the reader lacks — the whole object is already in
    `lines` — unlike the rotated-LOCAL-ledger-file carve-out
    `audit/ledger.py`'s `verify_chain` genuinely needs.

    Returns `(hash, seq, exhausted_bound)`. `(None, None, False)` when a
    predecessor was found but didn't verify, or was corrupt JSON, or there
    was nothing to seed from at all (`consume_from <= 0` territory, handled
    by the caller before this is even invoked) — the caller falls back to
    accepting the resumed page's first consumed line's incoming link as
    given, same as it always has. `(None, None, True)` is a DIFFERENT case
    (WS-172-8, security-invariant-reviewer, 2026-08-11): the walk ran out of
    its own step budget (`_SEED_WALK_MAX_STEPS`) without ever reaching a
    non-blank line, so nothing about the true predecessor is known one way
    or the other — the caller must fail closed on this one, not accept-as-
    given, or a real dropped record hidden behind a long blank-padded run
    placed exactly at a page boundary would verify silently.

    `consume_from` is caller-controlled (a cursor's `line` field, only ever
    checked for `>= 0` at decode time — see the module docstring's own "not
    a promise the archive is unchanged" contract) with no upper bound
    relative to `lines`'s actual length, which can differ from what it was
    when the cursor was issued. `min(consume_from, len(lines))` clamps the
    start index so a cursor pointing past the end of a shorter-than-expected
    object degrades (nothing to seed from) rather than raising `IndexError`.

    The walk is bounded in LENGTH, not by a wall-clock deadline (unlike the
    forward line loop in `search_worm_archive`, which checks
    `time.monotonic()` every 1,000 lines — item 154/WS-154-4): a genuine
    segment never has ANY blank line at all (`worm_sink.py`'s
    `_build_segment_body` joins records with `"\n"` and appends exactly one
    trailing newline, so `str.splitlines()` on a real segment yields zero
    empty elements) — so any bound at all is behaviorally lossless against a
    real object, and capping how far back this walk looks is the cheapest
    correct bound against a crafted one. An object crafted with a long
    blank-line run immediately before `consume_from` costs at most
    `_SEED_WALK_MAX_STEPS` `continue` iterations, not the full
    (`_MAX_OBJECT_BYTES`-bounded, but still potentially large) object
    length.
    """
    start = min(consume_from, len(lines)) - 1
    floor = max(-1, start - _SEED_WALK_MAX_STEPS)
    for back in range(start, floor, -1):
        seed_raw = lines[back]
        if not seed_raw.strip():
            continue
        try:
            seed_parsed = json.loads(seed_raw)
        except json.JSONDecodeError:
            return None, None, False
        if verify_envelope_hash(seed_parsed, key=ledger_key) is True:
            return seed_parsed.get("hash"), seed_parsed.get("seq"), False
        # Whether or not the predecessor verified, it is the nearest
        # non-blank line — stop looking further back. If it did NOT verify
        # (already reported when that line was itself consumed on an
        # earlier page), there is nothing trustworthy to seed from.
        return None, None, False
    # The loop ran to completion without finding a single non-blank line.
    # `exhausted_bound` is True only when the walk actually used its full
    # step budget — distinct from simply having nowhere left to look
    # (`start < 0`, or a short object with fewer than
    # `_SEED_WALK_MAX_STEPS` lines before `start`, all genuinely blank).
    exhausted_bound = (start - floor) >= _SEED_WALK_MAX_STEPS
    return None, None, exhausted_bound


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
    ledger_key: Optional[bytes] = None,
) -> WormSearchResult:
    """Search the S3 WORM archive for events matching the given filters
    within `[start_time, end_time]` (both required, inclusive). Raises
    `QueryValidationError` (422 at the transport edge) for any out-of-bound
    request; never makes an S3 call for a request it is about to reject.

    `ledger_key` (TODO.md item 154) verifies each segment's per-segment hash
    chain — the same key `resolve_ledger_key(AUDIT_LEDGER_HMAC_KEY)` resolves
    for the local hash-chained sink/reader. `None` verifies an unkeyed
    (SHA-256) chain.

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
    unverified = 0
    chain_breaks = 0
    lines_skipped_after_chain_break = 0
    events_scanned = 0
    objects_scanned = 0
    deadline = time.monotonic() + bounds.request_timeout_seconds

    def _finalize(*, truncated: bool, next_cursor: Optional[str]) -> WormSearchResult:
        # Build the response FIRST, count the metric only once construction
        # actually succeeds — reversed order would double-count a request as
        # both "ok" and "error" if the model itself somehow failed to build
        # (security-invariant-reviewer, 2026-08-06, WS-7).
        note = _NOTE
        # TODO.md item 172 follow-up (WS-172-4, security-invariant-reviewer,
        # 2026-08-10): `unverified` counts BOTH a pure hash mismatch and a
        # chain-break line (the latter's own hash still recomputed fine —
        # see `chain_breaks`' field comment). The two have different likely
        # causes and must not share one explanation: `hash_mismatches` below
        # is the count that's actually a candidate for "probably a key
        # rotation"; a chain-break line gets its own, separate sentence
        # that explicitly says it is NOT that.
        hash_mismatches = unverified - chain_breaks
        if hash_mismatches:
            # TODO.md item 154: disclose a likely key mismatch rather than
            # leaving the caller to read a bare count with no explanation.
            note = (
                f"{_NOTE} {hash_mismatches} line(s) were envelope-shaped but did not "
                "verify under the configured AUDIT_LEDGER_HMAC_KEY — this usually means "
                "the archive was written under a different (or since-rotated) key, not "
                "necessarily tampering."
            )
        if chain_breaks:
            # TODO.md item 172: a broken internal chain link is a stronger
            # signal than an ordinary hash mismatch — records were reordered
            # or removed from the middle of a segment, not just written
            # under a different key — so it gets its own sentence rather
            # than being folded into the key-rotation explanation above.
            note = (
                f"{note} {chain_breaks} segment(s) had a broken internal chain link "
                "(a record whose seq/prev_hash did not continue from its predecessor, "
                "and whose own hash otherwise still verified — not a key mismatch) — "
                "records may have been reordered or removed from the middle of a "
                "segment, and this fails closed: "
                f"{lines_skipped_after_chain_break} further line(s) in those affected "
                "segments were never read as a result and may hold genuine records."
            )
        result = WormSearchResult(
            source="s3_worm",
            generated_at=datetime.now(timezone.utc).isoformat(),
            start_time=start_time.isoformat(),
            end_time=end_time.isoformat(),
            objects_scanned=objects_scanned,
            events_scanned=events_scanned,
            malformed=malformed,
            unverified=unverified,
            chain_breaks=chain_breaks,
            lines_skipped_after_chain_break=lines_skipped_after_chain_break,
            truncated=truncated,
            next_cursor=next_cursor,
            note=note,
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
                # TODO.md item 172: chain-linkage state, scoped to THIS
                # object/segment — `audit/worm_sink.py`'s `WormFlushMonitor`
                # restarts every segment's own chain at seq=0/GENESIS_PREV_HASH
                # (a per-segment, not cross-segment, chain — see
                # `LedgerRecord`'s docstring), so linkage is never carried
                # across objects. `None` means "no record from this object
                # has been chain-checked yet" — distinct from a real prior
                # hash, so the first CONSUMED record is handled specially
                # below.
                broke_chain = False
                # TODO.md item 172 (WS-172-1, security-invariant-reviewer,
                # 2026-08-10): seed the chain state from the nearest
                # preceding non-blank line on a resumed page, bounded in
                # length (WS-172-7) — see `_seed_chain_state_from_
                # predecessor`'s own docstring for the full rationale.
                # `None, None, False` when `consume_from == 0` (nothing to
                # seed — this object's own genesis is checked directly
                # below) or when a predecessor was found but didn't verify.
                # `None, None, True` (WS-172-8) means the walk exhausted its
                # own step bound without learning anything — the caller
                # below must fail closed on that case, not accept-as-given.
                prev_verified_hash, prev_seq, prev_seed_bound_exhausted = (
                    _seed_chain_state_from_predecessor(lines, consume_from, ledger_key=ledger_key)
                    if consume_from > 0
                    else (None, None, False)
                )
                for line_no in range(consume_from, last_line):
                    # TODO.md item 154 (security-invariant-reviewer, WS-154-4):
                    # per-line envelope verification made this loop's body
                    # meaningfully more expensive than the plain JSON-parse +
                    # schema-validate it replaced, and — pre-existing, merely
                    # amplified — nothing inside a single object's line loop
                    # ever checked the wall-clock deadline before this. A
                    # single anomalous object at the line cap could block the
                    # event loop for seconds. Checked every 1000 lines, not
                    # every line, so the check itself doesn't dominate cost.
                    if line_no % 1000 == 0 and time.monotonic() >= deadline:
                        return _finalize(
                            truncated=True,
                            next_cursor=_encode_cursor(day, key, line_no, fingerprint),
                        )
                    raw_line = lines[line_no]
                    if not raw_line.strip():
                        continue
                    events_scanned += 1
                    try:
                        parsed = json.loads(raw_line)
                    except json.JSONDecodeError:
                        malformed += 1
                        continue
                    # TODO.md item 154: every segment is enveloped and
                    # per-segment hash-chained now. `None` (not envelope-
                    # shaped at all) is garbage/corruption, counted
                    # `malformed`. `False` (envelope-shaped, hash doesn't
                    # recompute) is kept as a DISTINCT `unverified` count —
                    # it's just as likely to be a key rotation as tampering,
                    # and conflating the two with `malformed` would hide that
                    # explanation from the caller (docs/THREAT_MODEL.md QG-40).
                    verified = verify_envelope_hash(parsed, key=ledger_key)
                    if verified is None:
                        malformed += 1
                        continue
                    if verified is False:
                        unverified += 1
                        continue
                    # TODO.md item 172: the record's OWN hash just verified
                    # above — now check its LINK to the previous record
                    # consumed from this segment. This must run for every
                    # hash-verified record regardless of what happens to it
                    # afterward (filtered out below, fails schema
                    # validation, etc.) — the write-time chain sequence
                    # doesn't care whether a record matches this caller's
                    # search filter.
                    seq = parsed.get("seq")
                    prev_hash_field = parsed.get("prev_hash")
                    if prev_verified_hash is None:
                        # Either genuinely starting fresh at this object
                        # (consume_from == 0 — must be the real segment
                        # genesis), or resuming past a predecessor line that
                        # itself didn't verify (the seeding loop above found
                        # nothing trustworthy to check against — already
                        # reported when that line was itself consumed on an
                        # earlier page). A resumed page whose predecessor DID
                        # verify never reaches this branch — it was seeded
                        # above and goes through the normal `else` below.
                        # TODO.md item 172 follow-up (WS-172-8,
                        # security-invariant-reviewer, 2026-08-11): if the
                        # seed walk exhausted its own step bound rather than
                        # genuinely finding no predecessor, fail closed
                        # instead of accepting this line's incoming link as
                        # given — a genuine segment has zero blank lines (see
                        # `_seed_chain_state_from_predecessor`'s docstring),
                        # so this only fires against a crafted/corrupted
                        # object, and accepting silently there would let a
                        # real dropped record hide behind a long blank-padded
                        # run placed exactly at a page boundary.
                        chain_ok = (not prev_seed_bound_exhausted) and (
                            consume_from > 0 or (seq == 0 and prev_hash_field == GENESIS_PREV_HASH)
                        )
                    else:
                        chain_ok = seq == prev_seq + 1 and prev_hash_field == prev_verified_hash
                    if not chain_ok:
                        # Count the breaking line itself (also, still,
                        # `unverified` — it never stopped being envelope-
                        # shaped with a self-consistent hash) and stop
                        # consuming this object: once linkage has broken, a
                        # later record in the same object can no longer be
                        # trusted to be genuinely positioned where it claims
                        # to be, so nothing further from this object is
                        # returned.
                        unverified += 1
                        chain_breaks += 1
                        # TODO.md item 172 follow-up (WS-172-2): disclose
                        # the SCALE of what fail-closed just cost — every
                        # remaining raw line in this object, past the
                        # breaking one, that will now never be read.
                        lines_skipped_after_chain_break += last_line - (line_no + 1)
                        broke_chain = True
                        break
                    prev_verified_hash = parsed.get("hash")
                    prev_seq = seq
                    unwrapped = unwrap_envelope(parsed)
                    try:
                        event = _EVENT_ADAPTER.validate_python(unwrapped)
                    except pyd.ValidationError:
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

                if broke_chain:
                    # TODO.md item 172: stopped deliberately on a broken
                    # link, not because the line cap fired — move on to the
                    # next object rather than treating this as a resumable
                    # truncation (a cursor pointing back into the same
                    # broken chain would just re-encounter the same break).
                    continue

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
        # TODO.md item 154: the same key the local hash-chained sink/reader
        # resolve, so a WORM segment's chain is verified under the identical
        # trust model.
        ledger_key=resolve_ledger_key(cfg.audit_ledger_hmac_key),
    )
