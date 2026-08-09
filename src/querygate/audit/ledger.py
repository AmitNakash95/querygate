"""Tamper-evident hash-chained audit ledger + per-query compliance receipts.

TODO.md item 91 (F5). The persisted audit sink (`audit/sinks.py`, item 23)
already writes redaction-safe events. This module adds a *tamper-evident*
layer on top of it **without changing the event body at all**: each persisted
event is wrapped in a chain envelope that links it to the hash of the previous
record, so any later edit, deletion, reordering, or insertion is detectable.

The chaining is deliberately:

- **Verify-only.** Nothing in the request pipeline reads the chain; it exists so
  an operator/auditor can *prove* the trail is intact after the fact
  (`querygate-audit verify`).
- **Envelope-level, never event-level.** The `event` embedded in each record is
  exactly the same `model_dump(mode="json", exclude_none=True)` the plain JSONL
  sink writes — no SQL, predicate values, rows, or credentials. The chain adds
  only a sequence number and two hashes, none of which is customer data. The
  audit-redaction guarantee is therefore preserved by construction.

Integrity model, stated honestly:

- **Unkeyed (SHA-256) chain** detects accidental corruption, truncation in the
  middle, reordering, and insertion. It detects *tampering* only relative to a
  trusted external anchor of the head hash (an attacker who can rewrite the
  whole file can recompute every SHA-256). Pass the last-known-good head to
  ``verify_chain(..., expected_head=...)`` to also catch tail truncation.
- **Keyed (HMAC-SHA256) chain** — set a ledger key — additionally makes forgery
  infeasible for anyone without the key: a rewritten file cannot produce a
  matching HMAC, so tampering is detectable from the ledger alone.

Single-writer assumption: a hash chain has one head, so one logical writer must
own it. Within a process the sink serializes writes with a lock; across replicas
each writer needs its own ledger file (documented in THREAT_MODEL.md).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Literal, Optional

import pydantic as pyd

# prev_hash of the genesis record (there is no predecessor). 64 hex zeros to
# match the width of a SHA-256/HMAC-SHA256 hex digest.
GENESIS_PREV_HASH = "0" * 64

# Algorithm tags recorded on receipts so a verifier knows which digest to
# recompute without being told out of band.
ALGO_SHA256 = "sha256"
ALGO_HMAC_SHA256 = "hmac-sha256"


class LedgerRecord(pyd.BaseModel):
    """One line of the hash-chained ledger: a chain envelope around one event.

    ``event`` is the unmodified redaction-safe audit event body. ``hash`` is the
    digest over ``{seq, prev_hash, event}`` in canonical form; ``prev_hash`` is
    the previous record's ``hash`` (or ``GENESIS_PREV_HASH`` for the first).

    ``seq``/``prev_hash`` continuity is owned by the WRITER, not by this
    envelope shape — the same shape is written by two independent owners with
    different continuity guarantees: ``audit/sinks.py``'s
    ``HashChainedAuditSink`` carries ``seq``/``prev_hash`` across every write
    to one file, forever; ``audit/worm_sink.py``'s ``WormFlushMonitor``
    deliberately restarts at ``seq=0``/``GENESIS_PREV_HASH`` on every flush
    (TODO.md item 154 — a per-segment, not cross-segment, chain). Do not
    assume ``seq`` is a global counter across files/segments without checking
    which writer produced them.
    """

    seq: int = pyd.Field(ge=0)
    prev_hash: str
    event: Dict[str, Any]
    hash: str

    model_config = pyd.ConfigDict(extra="forbid")


def _canonical_bytes(obj: Any) -> bytes:
    """Deterministic JSON encoding used for every hash input.

    Sorted keys + compact separators mean the same logical content always hashes
    identically, on write and on later verification.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def compute_record_hash(
    seq: int, prev_hash: str, event: Dict[str, Any], *, key: Optional[bytes] = None
) -> str:
    """Digest binding a record's position, its predecessor, and its event body.

    HMAC-SHA256 when a key is given (tamper-evident against a writer with file
    access), plain SHA-256 otherwise.
    """
    payload = _canonical_bytes({"seq": seq, "prev_hash": prev_hash, "event": event})
    if key:
        return hmac.new(key, payload, hashlib.sha256).hexdigest()
    return hashlib.sha256(payload).hexdigest()


def make_record(
    seq: int, prev_hash: str, event: Dict[str, Any], *, key: Optional[bytes] = None
) -> LedgerRecord:
    """Build the next chain record for ``event`` given the current head."""
    digest = compute_record_hash(seq, prev_hash, event, key=key)
    return LedgerRecord(seq=seq, prev_hash=prev_hash, event=event, hash=digest)


def resolve_ledger_key(raw: str) -> Optional[bytes]:
    """Turn a config-supplied HMAC key string into bytes, or `None` if unset.

    Shared by the sink (`audit/sinks.py`, which HMACs on write) and every
    reader that needs the same key to verify on read (TODO.md item 137) — one
    conversion, not one per call site.
    """
    return raw.encode("utf-8") if raw.strip() else None


def verify_envelope_hash(raw: Any, *, key: Optional[bytes] = None) -> Optional[bool]:
    """Check a hash-chained ledger envelope's own hash against its contents,
    without requiring the rest of the chain (TODO.md item 137).

    Returns `None` when `raw` isn't a chain envelope at all (a plain `jsonl`
    line) — there is nothing to verify, so a caller keeps treating those
    exactly as before. Returns `True`/`False` for an envelope depending on
    whether `hash` recomputes over `{seq, prev_hash, event}`: this is
    self-consistency only, not full chain linkage (a windowed/reverse-order
    scan never walks the whole file), but it is enough to catch a forged or
    edited record whose author didn't also recompute a correct digest — e.g.
    the `{"hash": "anything"}` fabrication this item's report describes.
    Uses the same constant-time `hmac.compare_digest` `verify_chain` does.
    """
    if not (
        isinstance(raw, dict)
        and raw.keys() >= {"seq", "prev_hash", "event", "hash"}
        and isinstance(raw["event"], dict)
    ):
        return None
    try:
        record = LedgerRecord.model_validate(raw)
    except pyd.ValidationError:
        return False
    expected = compute_record_hash(record.seq, record.prev_hash, record.event, key=key)
    return hmac.compare_digest(expected, record.hash)


def unwrap_envelope(raw: Any) -> Any:
    """Transparently unwrap a hash-chained ledger envelope (TODO.md item 91).

    A `jsonl_chained` record wraps the same redaction-safe event body a plain
    `jsonl` sink would write under an `"event"` key, alongside chain metadata
    (`seq`/`prev_hash`/`hash`). Every reader of the persisted audit stream
    that also has to accept the plain backend calls this first so it sees one
    shape regardless of which backend wrote the file. Returns `raw` unchanged
    if it doesn't look like an envelope (a plain `jsonl` line, or anything
    malformed — the caller's own validation reports that). Requires all four
    `LedgerRecord` keys, not just `event`/`hash`, so a plain event body that
    happens to carry same-named fields of its own is never mistaken for an
    envelope and silently unwrapped into something else."""
    if (
        isinstance(raw, dict)
        and raw.keys() >= {"seq", "prev_hash", "event", "hash"}
        and isinstance(raw["event"], dict)
    ):
        return raw["event"]
    return raw


@dataclass(frozen=True)
class ChainVerificationResult:
    """Outcome of verifying a hash-chained ledger.

    ``ok`` is the single question a gate cares about. ``broken_at_seq`` /
    ``reason`` localize the first problem for an investigator; ``head_hash`` is
    the last valid record's hash (useful to anchor the next verification).
    """

    ok: bool
    records_checked: int
    head_hash: Optional[str]
    broken_at_seq: Optional[int] = None
    broken_at_line: Optional[int] = None
    reason: Optional[str] = None


def _fail(
    records_checked: int,
    head_hash: Optional[str],
    *,
    seq: Optional[int],
    line: Optional[int],
    reason: str,
) -> ChainVerificationResult:
    return ChainVerificationResult(
        ok=False,
        records_checked=records_checked,
        head_hash=head_hash,
        broken_at_seq=seq,
        broken_at_line=line,
        reason=reason,
    )


def verify_chain(
    lines: Iterable[str],
    *,
    key: Optional[bytes] = None,
    expected_head: Optional[str] = None,
) -> ChainVerificationResult:
    """Verify a hash-chained ledger streamed as raw JSONL lines.

    Checks, per record in file order: it parses as a `LedgerRecord`, its own
    ``hash`` recomputes from ``{seq, prev_hash, event}``, ``seq`` increments by
    exactly one from the previous record, and ``prev_hash`` equals the previous
    record's ``hash`` (chain linkage). The first record's ``prev_hash`` must be
    ``GENESIS_PREV_HASH`` iff its ``seq`` is 0 — a rotated ledger legitimately
    starts at a higher seq whose predecessor is in an earlier file, so its
    incoming link is accepted as given but every subsequent link is enforced.

    ``expected_head`` (a previously trusted head hash), when provided, additionally
    requires the final record's ``hash`` to match it, which is the only way to
    detect records dropped from the *end* of the file.
    """
    prev: Optional[LedgerRecord] = None
    checked = 0
    head_hash: Optional[str] = None
    line_no = 0
    for raw in lines:
        line_no += 1
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            record = LedgerRecord.model_validate_json(stripped)
        except (pyd.ValidationError, ValueError):
            return _fail(
                checked,
                head_hash,
                seq=None,
                line=line_no,
                reason="record does not parse as a ledger record",
            )

        expected = compute_record_hash(record.seq, record.prev_hash, record.event, key=key)
        if not hmac.compare_digest(expected, record.hash):
            return _fail(
                checked,
                head_hash,
                seq=record.seq,
                line=line_no,
                reason="record hash does not match its contents (event or metadata altered)",
            )

        if prev is None:
            if record.seq == 0 and record.prev_hash != GENESIS_PREV_HASH:
                return _fail(
                    checked,
                    head_hash,
                    seq=record.seq,
                    line=line_no,
                    reason="genesis record does not link to the genesis hash",
                )
        else:
            if record.seq != prev.seq + 1:
                return _fail(
                    checked,
                    head_hash,
                    seq=record.seq,
                    line=line_no,
                    reason=f"sequence gap: expected {prev.seq + 1}, found {record.seq}",
                )
            if not hmac.compare_digest(record.prev_hash, prev.hash):
                return _fail(
                    checked,
                    head_hash,
                    seq=record.seq,
                    line=line_no,
                    reason="prev_hash does not match the previous record's hash (record inserted, "
                    "removed, or reordered)",
                )

        prev = record
        head_hash = record.hash
        checked += 1

    if expected_head is not None:
        if head_hash is None or not hmac.compare_digest(head_hash, expected_head):
            return _fail(
                checked,
                head_hash,
                seq=prev.seq if prev else None,
                line=line_no,
                reason="head hash does not match the expected head (records dropped from the end)",
            )

    return ChainVerificationResult(
        ok=True, records_checked=checked, head_hash=head_hash, reason=None
    )


class Receipt(pyd.BaseModel):
    """A self-contained, portable proof that one event occupied one chain slot.

    Handed to an auditor for a single query, it proves — without the rest of the
    ledger — that this exact redaction-safe event body was recorded at position
    ``seq`` linking to ``prev_hash``, under ``algorithm``. With a keyed ledger,
    a valid receipt is unforgeable without the key. It carries no data the audit
    event itself doesn't already carry.
    """

    kind: Literal["querygate.audit.receipt"] = "querygate.audit.receipt"
    version: str = "1"
    algorithm: str
    seq: int = pyd.Field(ge=0)
    prev_hash: str
    hash: str
    event: Dict[str, Any]

    model_config = pyd.ConfigDict(extra="forbid")


def build_receipt(record: LedgerRecord, *, keyed: bool) -> Receipt:
    """Turn a ledger record into a portable receipt for its single event."""
    return Receipt(
        algorithm=ALGO_HMAC_SHA256 if keyed else ALGO_SHA256,
        seq=record.seq,
        prev_hash=record.prev_hash,
        hash=record.hash,
        event=record.event,
    )


def verify_receipt(receipt: Receipt, *, key: Optional[bytes] = None) -> bool:
    """Recompute a receipt's hash from its contents and compare in constant time.

    A keyed receipt (`algorithm == hmac-sha256`) requires the ledger key to
    verify; an unkeyed receipt verifies with no secret. A key/algorithm mismatch
    is a verification failure, not a silent pass.
    """
    if receipt.algorithm == ALGO_HMAC_SHA256:
        if key is None:
            return False
        expected = compute_record_hash(receipt.seq, receipt.prev_hash, receipt.event, key=key)
    elif receipt.algorithm == ALGO_SHA256:
        if key is not None:
            return False
        expected = compute_record_hash(receipt.seq, receipt.prev_hash, receipt.event, key=None)
    else:
        return False
    return hmac.compare_digest(expected, receipt.hash)


def extract_receipt_for_event_id(
    lines: Iterable[str], event_id: str, *, keyed: bool
) -> Optional[Receipt]:
    """Find the ledger record whose event has ``event_id`` and return its receipt."""
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            record = LedgerRecord.model_validate_json(stripped)
        except (pyd.ValidationError, ValueError):
            continue
        if record.event.get("event_id") == event_id:
            return build_receipt(record, keyed=keyed)
    return None


def load_records(lines: Iterable[str]) -> List[LedgerRecord]:
    """Parse every valid ledger record from raw JSONL lines (skips blanks)."""
    records: List[LedgerRecord] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        records.append(LedgerRecord.model_validate_json(stripped))
    return records
