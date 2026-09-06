"""Pure-logic regression tests for demo/control/audit.py — the module that
reimplements QueryGate's own tamper-evident hash-chain verification for the
partner-demo audit panel (see audit.py's own module docstring for why this
is a deliberate reimplementation, not an import, of
src/querygate/audit/ledger.py).

Not wired into the QueryGate product test suite (pyproject.toml's
`testpaths = ["tests"]` never discovers this file) — demo/ is excluded from
every release artifact and this file only needs the interpreter, never a
live Postgres/MCP server, ledger, or QueryGate process. Run it explicitly:

    poetry run pytest demo/control/test_audit_logic.py -v

Before this file, audit.py's cryptographic tamper-detection logic (per-record
hash recomputation, seq continuity, prev_hash linkage, genesis shape) had NO
automated coverage at all — demo/prove_audit.py exercises the PRODUCT's own
`querygate-audit` CLI (a different, independently-tested implementation), not
this module. A swapped `hmac.compare_digest` argument, a dropped `prev_hash`
check, or a `sort_keys`/separators drift in the canonical JSON encoding would
all have left every existing check green.

These tests build their own valid ledger fixtures using an INDEPENDENT
canonical-encoding + hash implementation (`_canonical_json`/`_record_hash`
below) rather than calling audit.py's own `_canonical_bytes`/
`_compute_record_hash` to construct them — so a bug in those functions can't
silently validate itself by also being used to build the "known good" input.
The one place this file DOES exercise audit.py's own hash function directly
is `TestCanonicalEncoding`, which pins its output against a hand-computed,
hardcoded expected byte string (not `json.dumps()` called again inside the
test) specifically to catch a `sort_keys`/separators drift.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any, Optional

import pytest

from demo.control import audit, config

TEST_KEY = "test-ledger-hmac-key-not-real"


# ---------------------------------------------------------------------------
# Independent canonical encoding + hashing, used only to BUILD fixtures.
# Deliberately does not share code with audit.py's own _canonical_bytes/
# _compute_record_hash (see module docstring).
# ---------------------------------------------------------------------------
def _canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _record_hash(seq: int, prev_hash: str, event: dict, *, key: Optional[bytes]) -> str:
    payload = _canonical_json({"seq": seq, "prev_hash": prev_hash, "event": event})
    if key:
        return hmac.new(key, payload, hashlib.sha256).hexdigest()
    return hashlib.sha256(payload).hexdigest()


def _make_chain(n: int, *, key: Optional[bytes]) -> list[dict]:
    """n valid, correctly linked records, seq 0..n-1."""
    records = []
    prev_hash = audit.GENESIS_PREV_HASH
    for seq in range(n):
        event = {
            "schema_version": "1",
            "event_id": f"event-{seq}",
            "event_type": "query.execution",
            "policy_decision": "allowed",
        }
        h = _record_hash(seq, prev_hash, event, key=key)
        records.append({"seq": seq, "prev_hash": prev_hash, "event": event, "hash": h})
        prev_hash = h
    return records


def _write_records(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


@pytest.fixture
def ledger_path(tmp_path, monkeypatch):
    """Points config.AUDIT_LEDGER_PATH/AUDIT_LEDGER_HMAC_KEY at an isolated
    tmp_path file for the duration of one test — audit.py reads both off the
    `config` module attribute at call time (never caches them at import), so
    this is enough to redirect every function under test without touching
    the real demo ledger."""
    path = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(config, "AUDIT_LEDGER_PATH", path)
    monkeypatch.setattr(config, "AUDIT_LEDGER_HMAC_KEY", TEST_KEY)
    return path


# ---------------------------------------------------------------------------
# verify_chain()
# ---------------------------------------------------------------------------
class TestVerifyChain:
    def test_valid_chain_verifies(self, ledger_path):
        records = _make_chain(6, key=TEST_KEY.encode())
        _write_records(ledger_path, records)
        status = audit.verify_chain()
        assert status.verified is True
        assert status.status == "ok"
        assert status.records_checked == 6
        assert status.reason is None
        assert status.head_hash == records[-1]["hash"]

    def test_flipped_field_without_recomputed_hash_is_tampered(self, ledger_path):
        records = _make_chain(4, key=TEST_KEY.encode())
        records[2]["event"]["policy_decision"] = "denied"  # tamper — hash NOT recomputed
        _write_records(ledger_path, records)
        status = audit.verify_chain()
        assert status.verified is False
        assert status.status == "broken"
        assert "tampered" in status.reason
        assert status.records_checked == 2  # records 0 and 1 verified before hitting record 2

    def test_removed_middle_record_is_sequence_gap(self, ledger_path):
        records = _make_chain(5, key=TEST_KEY.encode())
        del records[2]
        _write_records(ledger_path, records)
        status = audit.verify_chain()
        assert status.verified is False
        assert status.status == "broken"
        assert "sequence gap" in status.reason
        assert status.records_checked == 2

    def test_reordering_detected_via_prev_hash_mismatch(self, ledger_path):
        """A literal swap of two ADJACENT lines is caught by the sequence
        check first, since the swap always puts an out-of-order seq value
        where the next-expected one belongs — that's what
        test_removed_middle_record_is_sequence_gap's "gap" reason is; it's
        also true of the product's own verify_chain (sequence continuity is
        checked before prev_hash linkage, in both implementations). To
        exercise the prev_hash-linkage check in isolation, this builds a
        record whose seq is untouched and whose own hash correctly covers
        its contents (so the self-hash check passes) but whose prev_hash
        has been re-pointed at a record other than its real predecessor —
        the signature of a record detached and re-spliced elsewhere with
        its seq preserved, which two-reordered-records tampering reduces to
        once the attacker also fixes up the sequence numbers."""
        records = _make_chain(4, key=TEST_KEY.encode())
        wrong_prev_hash = records[3]["hash"]  # anything other than records[0]["hash"]
        seq, event = records[1]["seq"], records[1]["event"]
        retargeted_hash = _record_hash(seq, wrong_prev_hash, event, key=TEST_KEY.encode())
        records[1] = {
            "seq": seq,
            "prev_hash": wrong_prev_hash,
            "event": event,
            "hash": retargeted_hash,
        }
        _write_records(ledger_path, records)
        status = audit.verify_chain()
        assert status.verified is False
        assert status.status == "broken"
        assert "prev_hash mismatch" in status.reason
        assert status.records_checked == 1

    def test_genesis_with_wrong_prev_hash(self, ledger_path):
        event = {"event_id": "e0"}
        bogus_prev = "1" * 64
        h = _record_hash(0, bogus_prev, event, key=TEST_KEY.encode())
        _write_records(
            ledger_path, [{"seq": 0, "prev_hash": bogus_prev, "event": event, "hash": h}]
        )
        status = audit.verify_chain()
        assert status.verified is False
        assert status.status == "broken"
        assert "genesis" in status.reason

    def test_empty_but_existing_file(self, ledger_path):
        ledger_path.write_text("", encoding="utf-8")
        status = audit.verify_chain()
        assert status.verified is True
        assert status.status == "ok"
        assert status.records_checked == 0

    def test_missing_file_is_not_yet_initialized_not_broken(self, ledger_path):
        # A1: the stage-risk fix — a fresh ledger (or a genuinely absent
        # one) must never come back status="broken".
        assert not ledger_path.exists()
        status = audit.verify_chain()
        assert status.verified is True
        assert status.status == "not_yet_initialized"
        assert status.records_checked == 0

    def test_unparseable_last_line_fails_verification(self, ledger_path):
        # A3: an earlier version of this reader silently skipped an
        # unparseable line, so a truncated last line made it "verify" a
        # silently-shortened prefix of the file as a pass. It must be a
        # hard failure naming the line.
        records = _make_chain(3, key=TEST_KEY.encode())
        lines = [json.dumps(r) for r in records]
        lines[-1] = lines[-1][: len(lines[-1]) // 2]  # truncate mid-object
        ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        status = audit.verify_chain()
        assert status.verified is False
        assert status.status == "broken"
        assert "line 3" in status.reason
        assert status.records_checked == 2

    def test_extra_field_in_envelope_fails_verification(self, ledger_path):
        # A3, extended: the product's LedgerRecord is extra="forbid" — a
        # line with one injected field must FAIL here too, not silently
        # verify. A superset (`>=`) key check used to let this through,
        # which is overclaiming in exactly the wrong direction for a
        # tamper-evidence panel.
        records = _make_chain(2, key=TEST_KEY.encode())
        lines = [json.dumps(r) for r in records]
        tampered = dict(records[1])
        tampered["injected"] = "surprise"
        lines[1] = json.dumps(tampered)
        ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        status = audit.verify_chain()
        assert status.verified is False
        assert status.status == "broken"
        assert "line 2" in status.reason

    def test_ledger_overwritten_entirely_with_garbage_does_not_verify_true(self, ledger_path):
        # The A3 follow-up review: because unparseable lines used to be
        # silently skipped, a ledger overwritten entirely with junk would
        # parse to an empty record list and come back verified=True — a
        # green "CHAIN VERIFIED" badge over a destroyed ledger.
        ledger_path.write_text("not json\nalso not json\n", encoding="utf-8")
        status = audit.verify_chain()
        assert status.verified is False
        assert status.status == "broken"

    def test_read_failure_reports_generic_reason_not_raw_exception_text(
        self, ledger_path, monkeypatch
    ):
        # D4: the browser-facing reason must never leak raw OS exception
        # text or filesystem paths — only a genericized exception type
        # name. Full detail is expected to go to the server log instead
        # (not asserted here — that's app.py's/audit.py's logger call, out
        # of this pure-logic test's scope).
        ledger_path.write_bytes(b"{}")
        real_read_bytes = Path.read_bytes

        def boom(self):
            if self == ledger_path:
                raise OSError("some absolute /Users/whoever/secret/path detail")
            return real_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", boom)
        status = audit.verify_chain()
        assert status.verified is False
        assert status.status == "broken"
        assert "OSError" in status.reason
        assert "/Users/" not in status.reason
        assert "secret" not in status.reason


# ---------------------------------------------------------------------------
# Binary, lenient decoding (item D2) — a corrupt/non-UTF-8 byte must never
# raise, in either the tail read (read_new_records) or the full read
# (verify_chain).
# ---------------------------------------------------------------------------
class TestLenientDecoding:
    def test_invalid_utf8_byte_does_not_raise_in_verify_chain(self, ledger_path):
        records = _make_chain(2, key=TEST_KEY.encode())
        lines = [json.dumps(r) for r in records]
        good_prefix = ("\n".join(lines[:-1]) + "\n").encode("utf-8") if len(lines) > 1 else b""
        corrupted_last = lines[-1].encode("utf-8")
        corrupted_last = corrupted_last[:5] + b"\xff\xfe" + corrupted_last[5:]
        ledger_path.write_bytes(good_prefix + corrupted_last + b"\n")
        status = audit.verify_chain()  # must not raise UnicodeDecodeError
        assert status.status == "broken"  # the corrupted line still fails to parse as JSON
        # Specifically the CONTROLLED "line N didn't parse" path, not the
        # generic read-exception path — verify_chain's outer
        # `except (OSError, ValueError)` would ALSO turn a raised
        # UnicodeDecodeError (it's a ValueError subclass) into a non-raising
        # "broken" status, so asserting status.status == "broken" alone
        # cannot tell a lenient decode apart from a strict decode caught by
        # that outer handler. Pin the actual reason text to the lenient
        # path so a regression back to strict `read_text()` is still caught
        # (mutation-verified: reverting _read_ledger_text to
        # `path.read_text(encoding="utf-8")` makes this assertion fail,
        # even though `status.status == "broken"` alone would not).
        assert status.reason is not None
        assert "line" in status.reason
        assert "could not read ledger" not in status.reason

    def test_invalid_utf8_byte_does_not_raise_in_read_new_records(self, ledger_path):
        ledger_path.write_bytes(
            b'{"seq":0,\xff\xfe"prev_hash":"'
            + audit.GENESIS_PREV_HASH.encode("ascii")
            + b'","event":{},"hash":"x"}\n'
        )
        result = audit.read_new_records(0)  # must not raise
        assert result == []  # unparseable after replacement — skipped for the tail preview


# ---------------------------------------------------------------------------
# _parse_envelope_line
# ---------------------------------------------------------------------------
class TestParseEnvelopeLine:
    def test_blank_line_is_none(self):
        assert audit._parse_envelope_line("   ") is None

    def test_invalid_json_is_none(self):
        assert audit._parse_envelope_line("{not json") is None

    def test_missing_key_is_none(self):
        line = json.dumps({"seq": 0, "prev_hash": "a" * 64, "event": {}})
        assert audit._parse_envelope_line(line) is None

    def test_extra_key_is_none(self):
        line = json.dumps(
            {"seq": 0, "prev_hash": "a" * 64, "event": {}, "hash": "b" * 64, "extra": 1}
        )
        assert audit._parse_envelope_line(line) is None

    def test_non_object_event_is_none(self):
        line = json.dumps(
            {"seq": 0, "prev_hash": "a" * 64, "event": "not-a-dict", "hash": "b" * 64}
        )
        assert audit._parse_envelope_line(line) is None

    def test_valid_envelope_parses(self):
        obj = {"seq": 0, "prev_hash": "a" * 64, "event": {"x": 1}, "hash": "b" * 64}
        assert audit._parse_envelope_line(json.dumps(obj)) == obj


# ---------------------------------------------------------------------------
# audit_block_for_run — the RunResult["audit"] shape the frontend consumes.
# ---------------------------------------------------------------------------
class TestAuditBlockForRun:
    def test_never_raises_on_a_missing_ledger(self, ledger_path):
        block = audit.audit_block_for_run(0)
        assert block["chain_status"] == "not_yet_initialized"
        assert block["chain_verified"] is True
        assert block["records"] == []
        assert block["count"] == 0
        assert block["headline_event_id"] is None

    def test_threads_headline_event_id_through(self, ledger_path):
        block = audit.audit_block_for_run(0, headline_event_id="some-event-id")
        assert block["headline_event_id"] == "some-event-id"

    def test_ledger_path_is_not_an_absolute_filesystem_path(self, ledger_path):
        # D4: never leak the presenter's home directory onto the page.
        # ledger_path (tmp_path) is outside the repo, exercising the
        # "fall back to just the file name" branch of _display_ledger_path.
        block = audit.audit_block_for_run(0)
        assert not Path(block["ledger_path"]).is_absolute()
        assert block["ledger_path"] == ledger_path.name

    def test_records_checked_reflects_the_whole_ledger_not_just_new_records(self, ledger_path):
        records = _make_chain(4, key=TEST_KEY.encode())
        _write_records(ledger_path, records)
        # byte_offset_before=huge means read_new_records sees no NEW
        # records, but verify_chain (records_checked) still walks the
        # whole file.
        block = audit.audit_block_for_run(10**9)
        assert block["count"] == 0
        assert block["records_checked"] == 4


# ---------------------------------------------------------------------------
# Canonical hash encoding — pinned byte-for-byte, item A7's explicit
# "sort_keys/separators drift" requirement.
# ---------------------------------------------------------------------------
class TestCanonicalEncoding:
    def test_matches_a_hand_computed_hardcoded_byte_string(self):
        # The expected bytes below were computed once, by hand, outside of
        # audit.py (via json.dumps with sort_keys=True,
        # separators=(",", ":"), ensure_ascii=False on
        # {"seq": 7, "prev_hash": "b"*64, "event": {"zeta": 1,
        # "alpha": {"delta": 2, "beta": 1}, "unicode": "café"}}) and is
        # asserted here as a LITERAL, not recomputed via json.dumps() in
        # this test — a test that called json.dumps() again would only
        # prove the test agrees with itself, and would not catch
        # sort_keys or separators silently drifting in audit.py.
        seq = 7
        prev_hash = "b" * 64
        event = {"zeta": 1, "alpha": {"delta": 2, "beta": 1}, "unicode": "café"}
        expected_canonical = (
            b'{"event":{"alpha":{"beta":1,"delta":2},"unicode":"caf\xc3\xa9",'
            b'"zeta":1},"prev_hash":"' + prev_hash.encode("ascii") + b'","seq":7}'
        )
        assert (
            audit._canonical_bytes({"seq": seq, "prev_hash": prev_hash, "event": event})
            == expected_canonical
        )

        key = b"pin-test-key"
        expected_hmac = hmac.new(key, expected_canonical, hashlib.sha256).hexdigest()
        assert audit._compute_record_hash(seq, prev_hash, event, key=key) == expected_hmac

        expected_sha256 = hashlib.sha256(expected_canonical).hexdigest()
        assert audit._compute_record_hash(seq, prev_hash, event, key=None) == expected_sha256


# ---------------------------------------------------------------------------
# D5: demo/ must never import from src/querygate/. AST-based (not grep) so
# this file's own prose mentions of "querygate" never false-positive, and a
# stray in-string occurrence can't accidentally suppress or trigger it.
# ---------------------------------------------------------------------------
class TestNoQuerygateImportBoundary:
    def test_no_module_in_demo_imports_querygate(self):
        demo_dir = Path(__file__).resolve().parent.parent
        assert demo_dir.name == "demo"
        violations = []
        for py_file in demo_dir.rglob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "querygate" or alias.name.startswith("querygate."):
                            violations.append(f"{py_file}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    if node.module and (
                        node.module == "querygate" or node.module.startswith("querygate.")
                    ):
                        violations.append(f"{py_file}: from {node.module} import ...")
        assert violations == [], f"demo/ must never import from querygate: {violations}"
