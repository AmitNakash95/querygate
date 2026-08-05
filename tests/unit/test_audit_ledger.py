"""Tamper-evident hash-chained audit ledger + receipts (TODO.md item 91, F5).

Proves the actual security property — that any edit, deletion, reordering,
insertion, or (with an anchored head) tail truncation of the ledger is
detectable, and that a keyed chain is unforgeable without the key — not merely
that a happy-path chain verifies. Also proves the redaction invariant: the chain
envelope adds only a sequence number and hashes, never any new event data.
"""

from __future__ import annotations

import json

import pytest

from querygate.audit.events import AuditEvent
from querygate.audit.ledger import (
    ALGO_HMAC_SHA256,
    ALGO_SHA256,
    GENESIS_PREV_HASH,
    LedgerRecord,
    build_receipt,
    compute_record_hash,
    extract_receipt_for_event_id,
    make_record,
    resolve_ledger_key,
    unwrap_envelope,
    verify_chain,
    verify_envelope_hash,
    verify_receipt,
)
from querygate.audit.sinks import (
    HashChainedAuditSink,
    configure_audit_sink,
    get_audit_sink,
    reset_audit_sink,
)


@pytest.fixture(autouse=True)
def _isolated_sink():
    reset_audit_sink()
    yield
    reset_audit_sink()


def _event(event_id: str, connection_id: str = "c1") -> dict:
    return {
        "event_id": event_id,
        "event_type": "query.execution",
        "connection_id": connection_id,
        "policy_decision": "allowed",
        "outcome": "success",
        "query_shape": {"from": "orders"},
        "duration_ms": 3,
    }


def _chain(events, *, key=None):
    lines = []
    prev = GENESIS_PREV_HASH
    for i, ev in enumerate(events):
        rec = make_record(i, prev, ev, key=key)
        lines.append(rec.model_dump_json())
        prev = rec.hash
    return lines


# --- hashing primitives -------------------------------------------------------


def test_hash_is_deterministic_and_order_independent_for_keys():
    ev = {"b": 2, "a": 1, "nested": {"y": 1, "x": 2}}
    h1 = compute_record_hash(0, GENESIS_PREV_HASH, ev)
    h2 = compute_record_hash(0, GENESIS_PREV_HASH, dict(reversed(list(ev.items()))))
    assert h1 == h2  # canonical (sorted-key) form, not insertion order


def test_keyed_and_unkeyed_hashes_differ():
    ev = _event("a")
    assert compute_record_hash(0, GENESIS_PREV_HASH, ev) != compute_record_hash(
        0, GENESIS_PREV_HASH, ev, key=b"secret"
    )


# --- clean chains -------------------------------------------------------------


@pytest.mark.parametrize("key", [None, b"ledger-secret"])
def test_intact_chain_verifies(key):
    lines = _chain([_event("a"), _event("b"), _event("c")], key=key)
    result = verify_chain(lines, key=key)
    assert result.ok
    assert result.records_checked == 3
    assert result.head_hash == json.loads(lines[-1])["hash"]


def test_empty_and_blank_lines_are_skipped():
    lines = _chain([_event("a"), _event("b")])
    interleaved = [lines[0], "", "   ", lines[1], ""]
    assert verify_chain(interleaved).ok


def test_genesis_record_must_link_to_genesis_hash():
    ev = _event("a")
    rec = make_record(0, "f" * 64, ev)  # wrong prev_hash for seq 0
    result = verify_chain([rec.model_dump_json()])
    assert not result.ok
    assert "genesis" in result.reason


# --- tamper detection ---------------------------------------------------------


def test_edited_event_body_is_detected():
    lines = _chain([_event("a"), _event("b"), _event("c")])
    d = json.loads(lines[1])
    d["event"]["connection_id"] = "attacker-controlled"
    tampered = [lines[0], json.dumps(d), lines[2]]
    result = verify_chain(tampered)
    assert not result.ok
    assert result.broken_at_seq == 1
    assert "does not match its contents" in result.reason


def test_deleted_record_breaks_linkage():
    lines = _chain([_event("a"), _event("b"), _event("c")])
    # Drop the middle record; c's prev_hash no longer matches a's hash and the
    # sequence jumps 0 -> 2.
    result = verify_chain([lines[0], lines[2]])
    assert not result.ok
    assert result.broken_at_seq == 2


def test_reordered_records_are_detected():
    lines = _chain([_event("a"), _event("b"), _event("c")])
    result = verify_chain([lines[0], lines[2], lines[1]])
    assert not result.ok


def test_inserted_forged_record_is_detected_with_a_key():
    key = b"ledger-secret"
    lines = _chain([_event("a"), _event("b")], key=key)
    # An attacker without the key forges a record to splice in. They can compute
    # a plausible SHA-256 but not a valid HMAC, so its own hash fails first.
    forged = make_record(1, json.loads(lines[0])["hash"], _event("evil"), key=None)
    result = verify_chain([lines[0], forged.model_dump_json()], key=key)
    assert not result.ok


def test_unkeyed_chain_head_anchor_detects_tail_truncation():
    lines = _chain([_event("a"), _event("b"), _event("c")])
    good_head = json.loads(lines[-1])["hash"]
    # Attacker lops off the last record and re-presents a shorter but internally
    # consistent chain. Only an externally anchored head catches it.
    truncated = lines[:-1]
    assert verify_chain(truncated).ok  # internally consistent on its own
    anchored = verify_chain(truncated, expected_head=good_head)
    assert not anchored.ok
    assert "dropped from the end" in anchored.reason


def test_malformed_ledger_line_fails_closed():
    lines = _chain([_event("a")])
    result = verify_chain([lines[0], "{not valid json"])
    assert not result.ok
    assert result.broken_at_line == 2


def test_rotated_ledger_starting_past_genesis_verifies_internally():
    # A rotated file legitimately starts at a higher seq; its incoming link is
    # accepted, but every subsequent link is still enforced.
    key = b"k"
    prev = "a" * 64
    r5 = make_record(5, prev, _event("a"), key=key)
    r6 = make_record(6, r5.hash, _event("b"), key=key)
    assert verify_chain([r5.model_dump_json(), r6.model_dump_json()], key=key).ok
    # ...but tampering with the second record is still caught.
    d = json.loads(r6.model_dump_json())
    d["event"]["outcome"] = "rejected"
    assert not verify_chain([r5.model_dump_json(), json.dumps(d)], key=key).ok


# --- receipts -----------------------------------------------------------------


def test_receipt_round_trips_unkeyed():
    lines = _chain([_event("a"), _event("b")])
    receipt = extract_receipt_for_event_id(lines, "b", keyed=False)
    assert receipt is not None
    assert receipt.algorithm == ALGO_SHA256
    assert receipt.event["event_id"] == "b"
    assert verify_receipt(receipt)


def test_receipt_round_trips_keyed_and_requires_the_key():
    key = b"ledger-secret"
    lines = _chain([_event("a"), _event("b")], key=key)
    receipt = extract_receipt_for_event_id(lines, "b", keyed=True)
    assert receipt is not None
    assert receipt.algorithm == ALGO_HMAC_SHA256
    assert verify_receipt(receipt, key=key)
    assert not verify_receipt(receipt, key=b"wrong-key")
    assert not verify_receipt(receipt, key=None)  # keyed receipt needs a key


def test_tampered_receipt_fails_verification():
    lines = _chain([_event("a")])
    receipt = build_receipt(LedgerRecord.model_validate_json(lines[0]), keyed=False)
    receipt.event["connection_id"] = "swapped"
    assert not verify_receipt(receipt)


def test_receipt_for_unknown_event_id_is_none():
    lines = _chain([_event("a")])
    assert extract_receipt_for_event_id(lines, "missing", keyed=False) is None


# --- sink integration ---------------------------------------------------------


def _audit_event(cid: str) -> AuditEvent:
    return AuditEvent(
        connection_id=cid,
        policy_decision="allowed",
        outcome="success",
        query_shape={"from": "orders"},
        duration_ms=1,
    )


def test_sink_writes_a_verifiable_chain(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    sink = HashChainedAuditSink(str(ledger), key=b"k")
    sink.emit(_audit_event("c1"))
    sink.emit(_audit_event("c2"))
    lines = ledger.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["seq"] for x in lines] == [0, 1]
    assert verify_chain(lines, key=b"k").ok


def test_sink_recovers_head_across_restart(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    HashChainedAuditSink(str(ledger), key=b"k").emit(_audit_event("c1"))
    # New instance (process restart) must continue the chain, not fork it.
    HashChainedAuditSink(str(ledger), key=b"k").emit(_audit_event("c2"))
    lines = ledger.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["seq"] for x in lines] == [0, 1]
    assert verify_chain(lines, key=b"k").ok


def test_sink_refuses_to_resume_a_corrupt_ledger(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("not a ledger record\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Cannot resume"):
        HashChainedAuditSink(str(ledger), key=b"k")


def test_sink_refuses_to_resume_a_ledger_whose_tail_has_no_newline_within_bounds(tmp_path):
    """TODO.md item 139: `_read_last_line` used to hand-roll its own tail
    scan, growing its read window without bound for a trailing region with no
    newline at all — worst case reading the whole file into memory once at
    process startup. It now delegates to `audit.file_reader.iter_lines_reverse`
    (item 138's bounded reader), so a tail this oversized fails loud instead —
    the same "refuse to silently fork the chain" posture as an unparseable
    last line, not a resource exhaustion risk on boot."""
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_bytes(b"x" * (2 * 1024 * 1024))  # no newline anywhere, > max_line_bytes (1 MiB)
    # Matched against the BOUND-specific phrase, not just "Cannot resume" —
    # found by `test-contract-reviewer` (2026-08-05): the pre-item-139
    # hand-rolled reader would have read this whole file, then failed to
    # parse it as a LedgerRecord and raised a *different* "Cannot resume...
    # is not a valid ledger record" message, which also matches the looser
    # pattern — so reverting the item-139 fix would NOT have failed this
    # test under the old assertion.
    with pytest.raises(ValueError, match="could not be read within the bounded-read limits"):
        HashChainedAuditSink(str(ledger), key=b"k")


def test_chained_envelope_adds_no_new_event_data(tmp_path):
    # Redaction invariant: the embedded event equals the plain event dump, and
    # the envelope adds only seq/prev_hash/hash — nothing derived from values.
    ledger = tmp_path / "ledger.jsonl"
    event = _audit_event("c1")
    HashChainedAuditSink(str(ledger)).emit(event)
    record = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
    assert set(record.keys()) == {"seq", "prev_hash", "event", "hash"}
    assert record["event"] == event.model_dump(mode="json", exclude_none=True)


def test_configure_audit_sink_selects_chained_backend(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    configure_audit_sink(backend="jsonl_chained", jsonl_path=str(ledger), ledger_hmac_key="k")
    sink = get_audit_sink()
    assert isinstance(sink, HashChainedAuditSink)
    sink.emit(_audit_event("c1"))
    assert verify_chain(ledger.read_text(encoding="utf-8").splitlines(), key=b"k").ok


# --- unwrap_envelope (TODO.md item 136) ---------------------------------------
#
# The read-only surfaces that browse the persisted audit stream (the admin UI
# audit browser, the anomaly report, the config/catalog change-trend report,
# and /help/my-recent-denials) all have to accept either backend's on-disk
# shape. This is the one shared primitive they all call to normalize a
# jsonl_chained envelope back to the plain event body a jsonl sink would have
# written, before their own event-schema validation runs.


def test_unwrap_envelope_returns_the_embedded_event_for_a_real_record():
    event = _event("q1")
    record = make_record(0, GENESIS_PREV_HASH, event)
    raw = json.loads(record.model_dump_json())
    assert unwrap_envelope(raw) == event


def test_unwrap_envelope_passes_through_a_plain_jsonl_event_unchanged():
    event = _event("q1")
    assert unwrap_envelope(event) == event


@pytest.mark.parametrize(
    "raw",
    [
        "not-a-dict",
        123,
        None,
        [1, 2, 3],
        {},
        {"event": {"event_type": "query.execution"}},  # hash missing
        {"hash": "x"},  # event missing
        {"event": "not-a-dict", "hash": "x", "seq": 0, "prev_hash": GENESIS_PREV_HASH},
    ],
)
def test_unwrap_envelope_passes_through_anything_not_shaped_like_a_full_record(raw):
    assert unwrap_envelope(raw) == raw


def test_unwrap_envelope_does_not_unwrap_a_partial_envelope_only_event_and_hash():
    # TODO.md item 136 security-review finding: duck-typing on only two of the
    # four LedgerRecord keys (event + hash) would let a plain event body that
    # happens to carry its own same-named "event"/"hash" fields be silently
    # unwrapped into something else. Requiring all four keys means only a
    # genuine chain record — which always carries seq/prev_hash too — unwraps.
    partial = {"event": {"event_type": "query.execution"}, "hash": "deadbeef"}
    assert unwrap_envelope(partial) == partial


# --- verify_envelope_hash (TODO.md item 137) — direct unit tests for its
# three return states, found missing by `test-contract-reviewer` (2026-08-05):
# every reader exercises this indirectly, but nothing pinned the primitive's
# own contract, including its ValidationError branch. ------------------------


def test_verify_envelope_hash_returns_none_for_a_plain_event_not_an_envelope():
    event = _event("q1")
    assert verify_envelope_hash(event) is None


def test_verify_envelope_hash_returns_true_for_a_genuine_unkeyed_record():
    record = make_record(0, GENESIS_PREV_HASH, _event("q1"))
    raw = json.loads(record.model_dump_json())
    assert verify_envelope_hash(raw) is True


def test_verify_envelope_hash_returns_true_for_a_genuine_keyed_record():
    record = make_record(0, GENESIS_PREV_HASH, _event("q1"), key=b"k")
    raw = json.loads(record.model_dump_json())
    assert verify_envelope_hash(raw, key=b"k") is True


def test_verify_envelope_hash_returns_false_for_a_mismatched_hash():
    record = make_record(0, GENESIS_PREV_HASH, _event("q1"))
    raw = json.loads(record.model_dump_json())
    raw["hash"] = "anything"
    assert verify_envelope_hash(raw) is False


def test_verify_envelope_hash_returns_false_when_verified_with_the_wrong_key():
    record = make_record(0, GENESIS_PREV_HASH, _event("q1"), key=b"real-key")
    raw = json.loads(record.model_dump_json())
    assert verify_envelope_hash(raw, key=b"wrong-key") is False


def test_verify_envelope_hash_returns_false_for_a_shape_valid_but_type_invalid_envelope():
    """The `except pyd.ValidationError: return False` branch — a dict with
    all four envelope keys (so it passes `unwrap_envelope`'s own shape check)
    but a field of the wrong TYPE for `LedgerRecord` (a string `seq` instead
    of an int). Removing this except and letting the exception propagate
    uncaught would crash every reader's scan on one malformed line instead of
    counting it `malformed`."""
    raw = {
        "seq": "not-an-int",
        "prev_hash": GENESIS_PREV_HASH,
        "event": {"event_type": "query.execution"},
        "hash": "deadbeef",
    }
    assert verify_envelope_hash(raw) is False


def test_resolve_ledger_key_converts_a_non_blank_string_to_bytes():
    assert resolve_ledger_key("secret") == b"secret"


def test_resolve_ledger_key_treats_blank_as_no_key():
    assert resolve_ledger_key("") is None
    assert resolve_ledger_key("   ") is None
