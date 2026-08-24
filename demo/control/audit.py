"""Real reads of QueryGate's own audit ledger, for the partner-demo control
backend's audit panel.

demo/config/env.demo runs QueryGate with AUDIT_SINK_BACKEND=jsonl_chained, so
every MCP `run_structured_queries` call it serves appends a real,
hash-chained `LedgerRecord` to demo/config/var/querygate-pitch-ledger.jsonl
(see src/querygate/audit/ledger.py for the format this module reads).
Nothing in this module simulates, guesses, or reconstructs a record — every
one shown by the UI is parsed from a line QueryGate itself wrote.

demo/ must never import from src/querygate/ (demo/SPEC.md: "Nothing in
demo/ may import from or modify src/querygate/"). The hashing/verification
logic below is therefore a small, DELIBERATE REIMPLEMENTATION of the same
algorithm as src/querygate/audit/ledger.py's
`compute_record_hash`/`verify_chain` — not an import of it. It must stay
byte-for-byte compatible with that module's canonical JSON encoding (sorted
keys, compact separators, UTF-8, no ASCII escaping) and its exact envelope
shape (`{seq, prev_hash, event, hash}` and nothing else —
`LedgerRecord.model_config = ConfigDict(extra="forbid")`) or every hash
recomputed here will legitimately fail to match a genuine, untampered
ledger. If that module's envelope shape or canonical encoding ever changes,
this file has to be updated by hand to match — there is no shared import to
keep the two in sync automatically, which is the accepted cost of the
one-way boundary. That boundary is enforced, not just asserted in prose, by
`test_audit_logic.py`'s `test_no_module_in_demo_imports_querygate` — an
AST-based sweep of every `.py` file under `demo/` for an `Import`/
`ImportFrom` node naming `querygate`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from . import config

logger = logging.getLogger("querygate.demo.control.audit")

# prev_hash of the genesis record — must match
# src/querygate/audit/ledger.py's GENESIS_PREV_HASH exactly (64 hex zeros,
# the width of a SHA-256/HMAC-SHA256 hex digest).
GENESIS_PREV_HASH = "0" * 64

# The exact envelope shape src/querygate/audit/ledger.py's LedgerRecord
# writes — and, since that model is `extra="forbid"`, the exact shape a real
# record can EVER have. Item A3 (partner-demo audit-panel fix session):
# this used to be a `>=` superset check, which meant a line with one extra
# injected field would pass THIS reader while the product's own model would
# reject it outright — an overclaiming divergence in exactly the direction
# a tamper-evidence panel must never have. Exact equality only.
_ENVELOPE_KEYS = {"seq", "prev_hash", "event", "hash"}

# How many of a run's real new records to embed in the RunResult for
# display. The full count of new lines that parsed as valid envelopes is
# always reported separately (RunResult.audit.count) so a run that appended
# more than this never silently looks smaller than it was. NOTE: `count` is
# the count of NEW LINES THAT PARSED, not a raw line count or byte count —
# see read_new_records' docstring.
RECORDS_PREVIEW_LIMIT = 5


def _canonical_bytes(obj: Any) -> bytes:
    """Same canonical JSON encoding as
    src/querygate/audit/ledger.py's `_canonical_bytes`: sorted keys, compact
    separators, no ASCII escaping. Must match exactly — this is the input to
    every hash below."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _compute_record_hash(seq: int, prev_hash: str, event: dict, *, key: Optional[bytes]) -> str:
    payload = _canonical_bytes({"seq": seq, "prev_hash": prev_hash, "event": event})
    if key:
        return hmac.new(key, payload, hashlib.sha256).hexdigest()
    return hashlib.sha256(payload).hexdigest()


def _ledger_key() -> Optional[bytes]:
    raw = config.AUDIT_LEDGER_HMAC_KEY
    return raw.encode("utf-8") if raw and raw.strip() else None


def snapshot_ledger_offset() -> int:
    """Real, live byte length of the ledger file right now — the tail
    marker used to find exactly the records a scenario's MCP call(s)
    appended, without reading the whole file for every run. Mirrors
    db.snapshot_agent_ro_calls' before/after bracketing pattern for
    pg_stat_statements (demo/SPEC.md: "never inferred from the outcome").

    Returns 0 if the file doesn't exist yet — the honest answer, since "the
    whole file so far" is empty; a subsequent read_new_records(0) then
    correctly reads the file from its start once it's created."""
    try:
        return config.AUDIT_LEDGER_PATH.stat().st_size
    except OSError:
        return 0


def _parse_envelope_line(line: str) -> Optional[dict]:
    """Parse one ledger line as a chain envelope. Returns None for a blank
    line, invalid JSON, or a JSON value that isn't exactly the
    `{seq, prev_hash, event, hash}` shape (see `_ENVELOPE_KEYS`) — callers
    that need "does this whole ledger verify" (verify_chain, below) treat a
    None here as a hard verification failure naming the line; callers doing
    a best-effort tail read for the preview list (read_new_records) skip
    it. Never raises — a corrupt line is data to report, not an exception to
    propagate through /api/run and 500 an otherwise-successful scenario
    run."""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        logger.warning("ledger line failed to parse as JSON")
        return None
    if not (
        isinstance(obj, dict)
        and set(obj.keys()) == _ENVELOPE_KEYS
        and isinstance(obj["event"], dict)
    ):
        logger.warning("ledger line parsed but isn't exactly a chain envelope")
        return None
    return obj


def _read_ledger_text(path) -> str:
    """Read the whole ledger file leniently: binary mode + `errors="replace"`
    on decode, so a single non-UTF-8 byte anywhere in the file can never
    raise `UnicodeDecodeError` here (item D2, partner-demo audit-panel fix
    session). `audit_block_for_run`'s docstring promises it never raises —
    before this fix that promise was false for exactly one corrupt byte,
    which would propagate a `ValueError` straight through `scenarios.py`
    into app.py's generic exception handler and turn every subsequent
    `/api/run` into a 500, with no results at all, for a problem confined to
    the audit panel. A replaced byte still makes its line fail to parse as
    JSON in `_parse_envelope_line`, which is the correct, already-handled
    outcome (an unparseable line is a named verification failure, not a
    crash) — replacement doesn't hide the corruption, it just routes it
    through the normal reporting path instead of an exception."""
    return path.read_bytes().decode("utf-8", errors="replace")


def read_new_records(byte_offset_before: int) -> list[dict]:
    """The real records appended to the ledger strictly after
    byte_offset_before — a tail-only read: seeks straight to the offset
    recorded immediately before a scenario's MCP call(s) instead of reading
    the whole file, so this stays cheap no matter how large the ledger has
    grown over the course of a demo day. Returns [] (never fabricates a
    record) if the file is missing or unreadable right now.

    Reads in BINARY mode and seeks by raw byte offset — text-mode `seek()`
    with a cookie that didn't come from an earlier `tell()` is not something
    Python's `io` docs guarantee is a byte offset (CPython happens to treat
    it that way today); binary mode makes the seek correct by construction,
    and also lets `_read_ledger_text`'s lenient `errors="replace"` decode
    apply to the tail the same way it does to a full-file read (D2)."""
    path = config.AUDIT_LEDGER_PATH
    try:
        with path.open("rb") as f:
            f.seek(byte_offset_before)
            tail_bytes = f.read()
    except (OSError, ValueError):
        return []
    tail = tail_bytes.decode("utf-8", errors="replace")
    records = []
    for line in tail.splitlines():
        rec = _parse_envelope_line(line)
        if rec is not None:
            records.append(rec)
    return records


@dataclass(frozen=True)
class ChainStatus:
    """Outcome of re-verifying the WHOLE ledger from scratch. Unlike
    read_new_records above (deliberately tail-only), this genuinely has to
    walk every record to confirm nothing earlier in the file was altered,
    deleted, or reordered — this demo's ledger stays small (tens to low
    hundreds of lines across a full day of scenario runs), so that's not the
    "avoid reading the whole file" case the tail-only read exists for.

    `status` is the machine-readable tri-state the UI badge renders from
    (item A1): "ok" (the ledger, empty or not, verifies), "not_yet_initialized"
    (no ledger file exists yet — nothing has been written, which is not a
    finding), or "broken" (a genuine verification failure). `verified` is
    kept for backward compatibility with anything reading only the boolean;
    it is True for both "ok" and "not_yet_initialized" (there is nothing
    broken in either case) and False only for "broken". No caller should add
    a *new* read of `verified` alone going forward — read `status`."""

    verified: bool
    head_hash: Optional[str]
    records_checked: int
    reason: Optional[str]
    status: str  # "ok" | "not_yet_initialized" | "broken"


def verify_chain() -> ChainStatus:
    """Re-derive the same checks
    src/querygate/audit/ledger.py's `verify_chain` performs — per-record
    hash recomputation, seq continuity, prev_hash linkage, genesis shape —
    reading straight from the real file. Never fabricates a pass: any
    read/parse failure or a genuinely broken chain comes back with
    status="broken" and a human-readable reason, which the UI is required to
    surface rather than hide (this task's own hard rule).

    A line that fails to parse as a valid envelope (bad JSON, wrong/extra
    keys — see `_parse_envelope_line`) is itself a hard verification
    failure, naming the line (item A3): silently skipping it, as an earlier
    version of this function did via a filtering helper, meant a truncated
    or corrupted last line made this reader verify a silently-shortened
    prefix of the file and report success — exactly the failure mode this
    panel exists to catch, reproduced by the panel itself. This matches
    src/querygate/audit/ledger.py's own `verify_chain`, which returns a hard
    failure the same way when a line doesn't parse as a `LedgerRecord`."""
    path = config.AUDIT_LEDGER_PATH
    if not path.exists():
        # A1: a fresh demo box (or a ledger deliberately reset for the
        # morning) has no ledger file until QueryGate's first logged
        # query.execution. That is not a finding — it is the honest,
        # expected state of "nothing has happened yet" — and must never
        # render as the same alarmed treatment a genuinely broken chain
        # gets on stage.
        return ChainStatus(
            verified=True,
            head_hash=None,
            records_checked=0,
            reason="no ledger file yet — nothing has been written",
            status="not_yet_initialized",
        )

    try:
        lines = _read_ledger_text(path).splitlines()
    except (OSError, ValueError) as exc:
        # Full exception detail goes only to the server log, never the
        # browser (item D4 — same posture app.py's own /api/run exception
        # handler already takes for an unclassified failure).
        logger.warning("ledger read failed during verify_chain", exc_info=True)
        return ChainStatus(
            verified=False,
            head_hash=None,
            records_checked=0,
            reason=f"could not read ledger: {type(exc).__name__}",
            status="broken",
        )

    numbered_lines = [(i, ln) for i, ln in enumerate(lines, start=1) if ln.strip()]
    if not numbered_lines:
        # File exists but is genuinely empty (e.g. touched but never
        # written, or truncated to nothing) — vacuously verified, same as
        # the product's own verify_chain over zero lines. Still "ok", not
        # "not_yet_initialized": the file existing-but-empty is a different
        # state than no file at all, even though the UI treats both as
        # non-alarming (see records_checked==0 handling in app.js).
        return ChainStatus(
            verified=True, head_hash=None, records_checked=0, reason=None, status="ok"
        )

    key = _ledger_key()
    prev_seq: Optional[int] = None
    prev_hash: Optional[str] = None
    head_hash: Optional[str] = None
    checked = 0

    for line_no, raw_line in numbered_lines:
        parsed = _parse_envelope_line(raw_line)
        if parsed is None:
            return ChainStatus(
                verified=False,
                head_hash=head_hash,
                records_checked=checked,
                reason=f"line {line_no} is not a valid ledger record "
                "(unparseable JSON, or not exactly {seq, prev_hash, event, hash})",
                status="broken",
            )

        try:
            seq = int(parsed["seq"])
            rec_prev_hash = str(parsed["prev_hash"])
            event = parsed["event"]
            rec_hash = str(parsed["hash"])
        except (KeyError, TypeError, ValueError):
            return ChainStatus(
                verified=False,
                head_hash=head_hash,
                records_checked=checked,
                reason=f"record at line {line_no} is malformed",
                status="broken",
            )
        if not isinstance(event, dict):
            return ChainStatus(
                verified=False,
                head_hash=head_hash,
                records_checked=checked,
                reason=f"record at line {line_no} has a non-object event",
                status="broken",
            )

        expected = _compute_record_hash(seq, rec_prev_hash, event, key=key)
        if not hmac.compare_digest(expected, rec_hash):
            return ChainStatus(
                verified=False,
                head_hash=head_hash,
                records_checked=checked,
                reason=f"record seq={seq} (line {line_no}) hash does not match its "
                "contents (tampered)",
                status="broken",
            )

        if prev_seq is None:
            if seq == 0 and rec_prev_hash != GENESIS_PREV_HASH:
                return ChainStatus(
                    verified=False,
                    head_hash=head_hash,
                    records_checked=checked,
                    reason=f"genesis record seq={seq} (line {line_no}) does not link to "
                    "the genesis hash",
                    status="broken",
                )
        else:
            if seq != prev_seq + 1:
                return ChainStatus(
                    verified=False,
                    head_hash=head_hash,
                    records_checked=checked,
                    reason=f"sequence gap at line {line_no}: expected {prev_seq + 1}, "
                    f"found {seq}",
                    status="broken",
                )
            if prev_hash is None or not hmac.compare_digest(rec_prev_hash, prev_hash):
                return ChainStatus(
                    verified=False,
                    head_hash=head_hash,
                    records_checked=checked,
                    reason=f"prev_hash mismatch at seq={seq} (line {line_no}) — record "
                    "inserted, removed, or reordered",
                    status="broken",
                )

        prev_seq, prev_hash, head_hash = seq, rec_hash, rec_hash
        checked += 1

    return ChainStatus(
        verified=True, head_hash=head_hash, records_checked=checked, reason=None, status="ok"
    )


def _display_ledger_path() -> str:
    """A safe-to-show ledger path for the browser (item D4): never leak the
    presenter's absolute filesystem/home-directory path onto a page shown on
    a projector. Prefer a path relative to the repo root (matches the style
    already used by demo/control/static/mock-data.js's own LEDGER_PATH
    fixture constant); if the configured path genuinely isn't inside the
    repo (e.g. QG_DEMO_AUDIT_LEDGER_PATH points somewhere else for local
    testing), fall back to just the file name rather than the full absolute
    path."""
    path = config.AUDIT_LEDGER_PATH
    repo_root = config.DEMO_DIR.parent
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return path.name


def _preview_including_headline(
    records: List[Dict[str, Any]], headline_event_id: Optional[str]
) -> List[Dict[str, Any]]:
    """The capped preview, guaranteed to contain the headline record.

    A plain positional slice is wrong here. `run_legit` appends 15 records and
    its outcome is decided by the LAST one, so `records[:5]` never contains the
    headline and the panel falls through to its honest "not in preview" empty
    state -- on the one gate-ON scenario that succeeds, which is the closing
    beat of the demo. Reserving the final slot for the headline keeps the cap,
    keeps `count` the true total, and fabricates nothing: every record here is
    still a real record this run appended.
    """
    preview = records[:RECORDS_PREVIEW_LIMIT]
    if not headline_event_id:
        return preview
    if any((r.get("event") or {}).get("event_id") == headline_event_id for r in preview):
        return preview
    headline = next(
        (
            r
            for r in reversed(records)
            if (r.get("event") or {}).get("event_id") == headline_event_id
        ),
        None,
    )
    if headline is None:
        # Genuinely absent from what this run appended -- let the UI say so
        # rather than substituting some other record.
        return preview
    return preview[: RECORDS_PREVIEW_LIMIT - 1] + [headline]


def audit_block_for_run(
    byte_offset_before: int, *, headline_event_id: Optional[str] = None
) -> dict:
    """Build RunResult["audit"] for one scenario run: the real ledger
    records appended during it (capped at RECORDS_PREVIEW_LIMIT for
    display; `count` is the full number of NEW LINES THAT PARSED as valid
    envelopes since byte_offset_before — not a raw line count or byte
    count, and not necessarily every line if the tail happened to contain
    something unparseable), and whether the chain still verifies afterward.
    Called once per /api/run, bracketing the ENTIRE scenario (which may
    itself make 1, 2, or 20+ real MCP calls) rather than each individual
    call — matching how `note`/`db` already summarize a whole scenario, not
    each call inside it.

    `headline_event_id` (item A4), when the caller can identify it, is the
    `event.event_id` of the specific ledger record that actually determined
    the RunResult's own `outcome` field — e.g. for `pii_column` that's
    always the denied call's record, never the masked follow-up's, even
    though both get real records; for `legit` it's the LAST of the 15 calls,
    matching how `outcome` itself is derived. It's None when no single
    record decided the outcome (e.g. `overload`'s aggregate "all 20
    rejected" decision, or a scenario/path that appends no records at all).
    Threading this through — rather than leaving the frontend to guess which
    of a multi-record preview is "the" headline — is what makes it
    impossible for the displayed headline record to disagree with the
    outcome badge next to it.

    Deliberately never raises: a ledger read/parse problem is folded into
    chain_status="broken" + chain_error rather than turning into a 500 that
    would hide a run's real MCP result behind an unrelated audit-panel
    failure."""
    new_records = read_new_records(byte_offset_before)
    status = verify_chain()
    return {
        "records": _preview_including_headline(new_records, headline_event_id),
        "count": len(new_records),
        "headline_event_id": headline_event_id,
        "chain_verified": status.verified,
        "chain_status": status.status,
        "records_checked": status.records_checked,
        "chain_head": status.head_hash,
        "chain_error": status.reason,
        "ledger_path": _display_ledger_path(),
    }
