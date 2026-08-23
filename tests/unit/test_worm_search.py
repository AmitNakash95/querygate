"""Unit tests for TODO.md item 134 phase 2: managed search over the WORM
S3 archive (`audit/worm_search.py`). Exercised against moto's S3 emulation
(matching phase 1's own test approach — no real AWS credentials are
available in this environment), covering real search/filter/pagination
behavior, not just that the function is callable.
"""

from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import time
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import boto3
import pydantic as pyd
import pytest
from moto import mock_aws

from querygate.audit import worm_search as worm_search_module
from querygate.audit.events import AuditEvent, ConnectionProbeEvent
from querygate.audit.ledger import GENESIS_PREV_HASH, make_record, verify_envelope_hash
from querygate.audit.worm_search import (
    WormSearchBounds,
    build_worm_search_result,
    search_worm_archive,
)
from querygate.core.config import AppConfig
from querygate.core.exceptions import QueryValidationError
from querygate.metrics import (
    AUDIT_WORM_SEARCH_CHAIN_BREAKS_TOTAL,
    AUDIT_WORM_SEARCH_UNVERIFIED_TOTAL,
    REGISTRY,
)

pytestmark = pytest.mark.unit

_BUCKET = "querygate-worm-search-test"
_PREFIX = "querygate-audit/"


def _sample(name: str, labels: dict) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _bounds(**overrides) -> WormSearchBounds:
    kwargs = dict(
        max_window_seconds=86400 * 30,
        max_objects_scanned=100,
        default_limit=50,
        max_limit=500,
        request_timeout_seconds=20.0,
    )
    kwargs.update(overrides)
    return WormSearchBounds(**kwargs)


def _event(connection_id: str = "demo", *, minute: int = 0, principal: str = None) -> AuditEvent:
    return AuditEvent(
        connection_id=connection_id,
        principal_id=principal,
        policy_decision="allowed",
        outcome="success",
        query_shape={"from": "customers"},
        duration_ms=1,
        occurred_at=datetime(2026, 3, 15, 12, minute, tzinfo=timezone.utc),
    )


def _put_segment(client, key: str, lines: list) -> None:
    body = ("\n".join(lines) + "\n").encode("utf-8")
    client.put_object(
        Bucket=_BUCKET,
        Key=key,
        Body=body,
        ObjectLockMode="COMPLIANCE",
        ObjectLockRetainUntilDate=datetime.now(timezone.utc) + timedelta(days=1),
    )


def _chain_lines(events: list, *, key: bytes = None) -> list:
    # TODO.md item 154: the same enveloped, per-segment-chained shape a real
    # WormFlushMonitor.flush_once() writes (audit/worm_sink.py's
    # _build_segment_body) — every test fixture that plants a "legitimate"
    # segment must match this shape now that the reader verifies it.
    prev_hash = GENESIS_PREV_HASH
    lines = []
    for seq, event in enumerate(events):
        body = event.model_dump(mode="json", exclude_none=True)
        record = make_record(seq, prev_hash, body, key=key)
        lines.append(record.model_dump_json())
        prev_hash = record.hash
    return lines


def _put_events(client, key: str, events: list) -> None:
    _put_segment(client, key, _chain_lines(events))


def _retype_seq(line: str, raw_seq) -> str:
    """TODO.md item 178: rewrite one chained line's RAW `seq` to a non-int
    JSON value, leaving its `hash` byte-for-byte untouched.

    The resulting line still passes `verify_envelope_hash`, because that
    function parses through `LedgerRecord.model_validate` first and
    pydantic's lax coercion turns `"3"` / `3.0` / `True` back into an `int`.
    That is the whole point: the hash verifying proves nothing about the raw
    value's TYPE, which is what the chain-linkage arithmetic downstream then
    operates on.

    The caller must pass a value that coerces to THIS record's own `seq` —
    the digest is recomputed over the coerced value, so `_retype_seq(line,
    "3")` on a seq-0 record fails verification for an ordinary hash mismatch
    and tests nothing. Every test using this asserts the line still verifies,
    which converts that mistake (and any future change to the coercion rules)
    into an obvious failed premise instead of a confusing downstream count
    mismatch."""
    parsed = json.loads(line)
    parsed["seq"] = raw_seq
    return json.dumps(parsed)


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=_BUCKET, ObjectLockEnabledForBucket=True)
        yield client


class TestSearchAndFilter:
    async def test_finds_events_across_multiple_segments_in_the_window(self, s3):
        _put_events(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [_event("a", minute=0), _event("b", minute=1)],
        )
        _put_events(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120200-000001.jsonl",
            [_event("a", minute=2), _event("c", minute=3)],
        )

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )
        assert result.source == "s3_worm"
        assert len(result.events) == 4
        assert result.objects_scanned == 2
        assert result.truncated is False
        assert result.next_cursor is None

    async def test_connection_id_filter_narrows_results(self, s3):
        _put_events(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [_event("a", minute=0), _event("b", minute=1), _event("a", minute=2)],
        )

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            connection_id="a",
            bounds=_bounds(),
        )
        assert len(result.events) == 2
        assert all(e.connection_id == "a" for e in result.events)

    async def test_principal_id_filter_narrows_results(self, s3):
        _put_events(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [
                _event("a", minute=0, principal="alice"),
                _event("a", minute=1, principal="bob"),
            ],
        )
        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            principal_id="alice",
            bounds=_bounds(),
        )
        assert len(result.events) == 1
        assert result.events[0].principal_id == "alice"

    async def test_event_type_filter_only_matches_declared_type(self, s3):
        probe = ConnectionProbeEvent(
            connection_id="demo",
            outcome="success",
            occurred_at=datetime(2026, 3, 15, 12, 5, tzinfo=timezone.utc),
        )
        _put_events(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [_event("demo", minute=0), probe],
        )
        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            event_type="connection.probe",
            bounds=_bounds(),
        )
        assert len(result.events) == 1
        assert result.events[0].event_type == "connection.probe"

    async def test_events_outside_the_time_window_are_excluded_even_within_a_scanned_segment(
        self, s3
    ):
        _put_events(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [_event("a", minute=0), _event("b", minute=59)],
        )
        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 15, 12, 30, tzinfo=timezone.utc),
            bounds=_bounds(),
        )
        assert len(result.events) == 1
        assert result.events[0].connection_id == "a"

    async def test_empty_archive_returns_no_events_not_an_error(self, s3):
        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )
        assert result.events == []
        assert result.truncated is False

    async def test_malformed_json_line_is_counted_and_skipped(self, s3):
        _put_segment(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [*_chain_lines([_event("a", minute=0)]), "{not valid json"],
        )
        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )
        assert len(result.events) == 1
        assert result.malformed == 1

    async def test_day_boundary_padding_finds_an_event_flushed_into_the_next_day(self, s3):
        """An event just before midnight can flush into the NEXT day's key
        prefix (WormFlushMonitor keys by flush time, not occurred_at). The
        search must still find it even when the requested window's end_time
        stays strictly WITHIN the event's own day (never touching the 16th
        itself) — end_time here is 23:59:59 on the 15th, not midnight, so
        end_day only naturally covers the 15th; only the flush-interval
        padding reaches into the 16th's directory where the segment actually
        landed. (A prior version of this test used end_time=midnight of the
        16th, which trivially made end_day already cover the 16th on its
        own — passing even with padding disabled, and so not actually
        exercising the padding logic at all.)"""
        near_midnight = AuditEvent(
            connection_id="a",
            policy_decision="allowed",
            outcome="success",
            query_shape={},
            duration_ms=1,
            occurred_at=datetime(2026, 3, 15, 23, 59, 50, tzinfo=timezone.utc),
        )
        # Segment flushed a few seconds later, into 2026/03/16's directory.
        _put_events(s3, f"{_PREFIX}2026/03/16/20260316T000005-000001.jsonl", [near_midnight])

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 15, 23, 59, 59, tzinfo=timezone.utc),
            bounds=_bounds(),
        )
        assert len(result.events) == 1


class TestForgedOrUnenvelopedSegmentsAreRejected:
    """TODO.md item 154, docs/THREAT_MODEL.md QG-40: the actual threat this
    item closes — a segment planted by anyone with `s3:PutObject` on the
    archive prefix (necessarily including QueryGate's own AWS role) that
    isn't a genuine QueryGate-written, correctly-chained segment must be
    rejected as malformed, never returned indistinguishably from a real one.
    `TestTamperedSegmentIsRejectedNotLeaked` in
    tests/security/test_worm_search_redaction.py covers the deeper,
    schema-level (`extra="forbid"`) gate for a VALIDLY-enveloped forgery;
    these tests cover the new, shallower gate item 154 actually adds: the
    envelope itself."""

    async def test_a_bare_unenveloped_line_is_rejected_as_malformed(self, s3):
        # The pre-item-154 shape (a bare event body, no LedgerRecord
        # wrapper) — what every WORM segment looked like before this item,
        # and what a forger with no knowledge of the envelope format at all
        # would produce. No legacy-segment tolerance (recorded decision,
        # module docstring): this is malformed now, not a weaker-but-
        # accepted read.
        event = _event("a", minute=0)
        _put_segment(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [event.model_dump_json(exclude_none=True)],
        )
        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )
        assert result.events == []
        assert result.malformed == 1

    async def test_an_envelope_whose_hash_does_not_match_its_contents_is_rejected(self, s3):
        # A forger who understands the envelope SHAPE (seq/prev_hash/event/
        # hash) but cannot compute a correct hash — e.g. edited an event's
        # content after copying a real envelope's structure, or simply
        # supplied a plausible-looking `hash` without recomputing it. Chain
        # linkage alone would never catch this (a forger can supply a
        # plausible prev_hash too) — only recomputing this record's own
        # hash does, mirroring the local reader's identical
        # test_jsonl_source_rejects_a_forged_chain_record.
        from querygate.audit.ledger import LedgerRecord

        forged = LedgerRecord(
            seq=0,
            prev_hash=GENESIS_PREV_HASH,
            event=_event("a", minute=0).model_dump(mode="json", exclude_none=True),
            hash="anything",
        )
        _put_segment(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [forged.model_dump_json()],
        )
        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )
        assert result.events == []
        # TODO.md item 154 (security-invariant-reviewer, WS-154-2): an
        # envelope-shaped-but-hash-mismatched line is `unverified`, not
        # `malformed` — it's structurally a real envelope, just not one
        # that verifies under the configured key.
        assert result.unverified == 1
        assert result.malformed == 0

    async def test_a_genuine_segment_still_verifies_and_is_returned(self, s3):
        # The control: the identical shape the two tests above attack, but
        # with a REAL matching hash, must still verify and return the event
        # — proving the rejection above is about the hash mismatch/missing
        # envelope specifically, not some other accidental difference.
        _put_events(
            s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", [_event("a", minute=0)]
        )
        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )
        assert len(result.events) == 1
        assert result.malformed == 0

    async def test_an_unkeyed_forged_segment_still_verifies_documented_residual(self, s3):
        # TODO.md item 154 / docs/THREAT_MODEL.md QG-40 (security-invariant-
        # reviewer, WS-154-1): pins the residual explicitly so a future doc
        # edit can't quietly re-claim unconditional closure. An unkeyed
        # (SHA-256) chain is a PUBLIC function — a forger with no HMAC key
        # can compute a self-consistent envelope exactly like a genuine
        # writer would, using nothing but the same public `make_record`.
        # This is NOT a bug: it's the same limitation `audit/ledger.py`'s
        # own docstring states for the local ledger's unkeyed mode.
        forged_body = {
            "connection_id": "attacker-forged",
            "policy_decision": "allowed",
            "outcome": "success",
            "event_type": "query.execution",
            "query_shape": {},
            "duration_ms": 1,
            "occurred_at": "2026-03-15T12:00:00+00:00",
            "schema_version": "1",
            "event_id": "11111111-1111-1111-1111-111111111111",
        }
        forger_record = make_record(0, GENESIS_PREV_HASH, forged_body)  # no key: public compute
        _put_segment(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [forger_record.model_dump_json()],
        )
        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
            # ledger_key intentionally omitted — the shipped default.
        )
        assert len(result.events) == 1
        assert result.events[0].connection_id == "attacker-forged"
        assert result.malformed == 0
        assert result.unverified == 0

    async def test_unverified_lines_are_disclosed_in_the_note(self, s3):
        # TODO.md item 154 (security-invariant-reviewer, WS-154-2): a
        # non-zero `unverified` count must be named in the response `note`,
        # not just a bare, unexplained number — the far more likely cause in
        # practice is a rotated/mismatched key, not tampering.
        _put_segment(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            _chain_lines([_event("a", minute=0)], key=b"a-different-key"),
        )
        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )
        assert result.unverified == 1
        assert "key" in result.note.lower()

    async def test_a_keyed_chain_is_rejected_when_the_reader_has_no_key(self, s3):
        # An unkeyed reader (ledger_key=None, the default) recomputes plain
        # SHA-256 — a segment that was actually HMAC-signed with a key the
        # reader doesn't have must not verify under the wrong algorithm.
        _put_segment(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            _chain_lines([_event("a", minute=0)], key=b"secret-key"),
        )
        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
            # ledger_key intentionally omitted (defaults to None)
        )
        assert result.events == []
        assert result.unverified == 1
        assert result.malformed == 0

    async def test_a_keyed_chain_verifies_with_the_matching_key(self, s3):
        _put_segment(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            _chain_lines([_event("a", minute=0)], key=b"secret-key"),
        )
        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
            ledger_key=b"secret-key",
        )
        assert len(result.events) == 1
        assert result.malformed == 0


class TestChainLinkageVerification:
    """TODO.md item 172: `TestForgedOrUnenvelopedSegmentsAreRejected` above
    covers a record's OWN hash (item 154) — this class covers the gap item
    154 explicitly left open: a segment's own record-to-record LINKAGE
    (`seq` continuity, `prev_hash` continuity), which a per-record hash
    check alone can never catch (a dropped or reordered record's SURVIVING
    neighbors each still verify individually). Duplicating a whole genuine
    segment to a second key is a separate, still-open residual — it isn't
    detectable by a linkage check at all, since a full copy is itself a
    valid chain — and needs its own design decision (see the item-172
    Decision Log entry and docs/THREAT_MODEL.md QG-40); not covered here."""

    async def test_records_dropped_from_the_end_of_a_segment_are_not_detected_yet(self, s3):
        # Documents a known, disclosed residual (WS-172-3, 2026-08-10
        # security-invariant-reviewer), not a defect in this item: item 172
        # closes INTERIOR omission (a record dropped from the middle breaks
        # the surviving records' linkage), but a record dropped from the
        # END of a segment leaves a perfectly valid, self-contained prefix
        # chain -- there is nothing past it to fail a continuity check
        # against. Unlike the local audit/ledger.py reader's
        # verify_chain(expected_head=...), no per-segment head anchor
        # exists outside the object to compare against. If tail-truncation
        # detection is ever added, THIS test must be updated to assert
        # detection, not left passing by accident.
        lines = _chain_lines([_event("a", minute=0), _event("b", minute=1), _event("c", minute=2)])
        del lines[2:]  # drop the last record; "a" and "b" remain a valid prefix
        _put_segment(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", lines)
        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )
        assert {e.connection_id for e in result.events} == {"a", "b"}
        assert result.chain_breaks == 0
        assert result.unverified == 0

    async def test_a_record_dropped_from_the_middle_of_a_segment_is_detected(self, s3):
        # Three genuinely chained records, then the middle line is removed
        # from the OUTPUT before writing — simulating an operator (or an
        # attacker with s3:PutObject) editing a segment object in place to
        # remove one record. The third record's own hash is still perfectly
        # self-consistent (item 154's check alone would pass it) but its
        # prev_hash now points at a hash the reader never saw.
        lines = _chain_lines([_event("a", minute=0), _event("b", minute=1), _event("c", minute=2)])
        del lines[1]
        _put_segment(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", lines)
        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )
        # The first record (genuinely first in the chain) is returned; the
        # break is detected on the third record, so it — and anything that
        # might follow it — is never returned.
        assert len(result.events) == 1
        assert result.events[0].connection_id == "a"
        assert result.chain_breaks == 1
        assert result.unverified == 1
        # Nothing was left in the object past the breaking line itself here
        # (2 lines total, break on the 2nd) — the non-trivial count is
        # pinned separately below.
        assert result.lines_skipped_after_chain_break == 0
        assert result.malformed == 0
        assert "broken internal chain link" in result.note

    async def test_a_segment_whose_first_record_is_not_the_genuine_genesis_is_detected(self, s3):
        # A single-record segment whose seq/prev_hash don't match the
        # genuine genesis (seq=0, prev_hash=GENESIS_PREV_HASH) — e.g. a
        # forger who has an HMAC key (or an unkeyed archive) and can produce
        # a self-consistent record, but doesn't control what a genuine
        # WormFlushMonitor.flush_once() would have started this segment
        # with.
        from querygate.audit.ledger import make_record

        forged_first = make_record(
            seq=1,  # not 0
            prev_hash="a" * 64,  # not GENESIS_PREV_HASH
            event=_event("a", minute=0).model_dump(mode="json", exclude_none=True),
        )
        _put_segment(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [forged_first.model_dump_json()],
        )
        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )
        assert result.events == []
        assert result.chain_breaks == 1
        assert result.unverified == 1

    async def test_a_genuinely_intact_chain_across_multiple_segments_reports_no_breaks(self, s3):
        # The control: real, unmodified multi-record, multi-segment chains
        # (each segment independently starting its own genesis, per
        # WormFlushMonitor's per-segment-not-cross-segment design) must
        # report zero chain_breaks — proving the checks above are actually
        # selective, not just always firing.
        _put_events(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [_event("a", minute=0), _event("b", minute=1), _event("c", minute=2)],
        )
        _put_events(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120500-000001.jsonl",
            [_event("d", minute=5), _event("e", minute=6)],
        )
        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )
        assert len(result.events) == 5
        assert result.chain_breaks == 0
        assert result.unverified == 0

    async def test_resuming_mid_segment_does_not_false_positive_on_the_first_consumed_line(
        self, s3
    ):
        # A cursor legitimately resumes partway through one object — this
        # scan never read the predecessor line, so it cannot (and must not)
        # demand that the first CONSUMED record be the genuine genesis; it
        # accepts that record's incoming link as given, the same way a
        # rotated/truncated local ledger file's first record is accepted
        # (audit/ledger.py's verify_chain has the identical carve-out).
        # Every record AFTER the resume point must still be linkage-checked.
        _put_events(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [_event("a", minute=0), _event("b", minute=1), _event("c", minute=2)],
        )
        first = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
            limit=1,
        )
        assert len(first.events) == 1
        assert first.next_cursor is not None

        second = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
            cursor=first.next_cursor,
        )
        # The resumed scan starts mid-segment (not at seq=0) but its own
        # incoming link is genuinely intact (the real predecessor really is
        # "b"'s prev_hash), so no false positive fires, and both remaining
        # events are returned.
        assert {e.connection_id for e in second.events} == {"b", "c"}
        assert second.chain_breaks == 0
        assert second.unverified == 0

    async def test_resuming_exactly_at_a_break_still_detects_it(self, s3):
        # TODO.md item 172 follow-up (2026-08-10 security-invariant-reviewer,
        # WS-172-1): the predecessor line of a resumed page is NOT in a
        # different file the reader lacks — the whole object was already
        # fetched — so a record dropped exactly at a page boundary must
        # still be caught, the same as it would be on a fresh (unpaged)
        # scan of the same object. Without seeding the chain state from the
        # nearest preceding verified line, every ordinary server-issued
        # page boundary would silently exempt one link — this is the
        # concrete case that gap would miss.
        lines = _chain_lines([_event("a", minute=0), _event("b", minute=1), _event("c", minute=2)])
        del lines[1]  # "b" dropped; "c"'s prev_hash now points at it
        _put_segment(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", lines)

        first = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
            limit=1,
        )
        # Page 1 returns only "a" — genuinely the segment genesis.
        assert [e.connection_id for e in first.events] == ["a"]
        assert first.next_cursor is not None

        second = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
            cursor=first.next_cursor,
        )
        # Page 2 resumes exactly at the dropped record's successor ("c").
        # The break must be caught on THIS page, not silently accepted
        # because it's the first record consumed on a resumed page.
        assert second.events == []
        assert second.chain_breaks == 1

    async def test_metrics_and_note_still_disclose_key_rotation_separately(self, s3):
        # A hash mismatch (item 154's own gap — likely a rotated key, not a
        # linkage break) must not be misreported as a chain break: the two
        # failure classes carry genuinely different implications for an
        # operator and must stay distinguishable in the response.
        _put_segment(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            _chain_lines([_event("a", minute=0)], key=b"a-different-key"),
        )
        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )
        assert result.unverified == 1
        assert result.chain_breaks == 0
        assert "broken internal chain link" not in result.note

    async def test_a_chain_break_below_the_line_cap_is_not_reported_as_a_capacity_truncation(
        self, s3, monkeypatch
    ):
        # A deliberate "stop consuming this broken object" must never be
        # confused with the genuine resource-bound truncation
        # test_an_object_past_the_per_object_line_cap_truncates_instead_of_skipping
        # (below) pins — those are two different reasons to stop scanning
        # early, and a cursor that resumed back into an already-broken
        # chain would just re-encounter the identical break. Lowers
        # _MAX_LINES_PER_OBJECT so the object genuinely has more raw lines
        # than the cap, with the chain break landing well BEFORE the
        # (lowered) cap — proving the object-level `broke_chain` guard, not
        # just the line-level break detection this class's other tests
        # already cover.
        monkeypatch.setattr(worm_search_module, "_MAX_LINES_PER_OBJECT", 3)
        lines = _chain_lines(
            [
                _event("a", minute=0),
                _event("b", minute=1),
                _event("c", minute=2),
                _event("d", minute=3),
                _event("e", minute=4),
            ]
        )
        del lines[1]  # break the chain at the 2nd consumed line, before the cap
        _put_segment(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", lines)

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )
        assert [e.connection_id for e in result.events] == ["a"]
        assert result.chain_breaks == 1
        # The whole request completed (this was the only segment) — a
        # capacity-bound stop would instead have set truncated=True with a
        # resumable next_cursor.
        assert result.truncated is False
        assert result.next_cursor is None

    async def test_the_note_discloses_how_many_lines_a_break_skipped(self, s3):
        # TODO.md item 172 follow-up (WS-172-2, security-invariant-reviewer,
        # 2026-08-10): a break early in a large segment fails closed on
        # everything after it — the RESPONSE must disclose the scale of
        # that, not just a bare "chain_breaks: 1" a caller has no way to
        # size. A 6-record segment with the break on the 3rd record leaves
        # 3 further lines never read.
        lines = _chain_lines(
            [
                _event("a", minute=0),
                _event("b", minute=1),
                _event("c", minute=2),
                _event("d", minute=3),
                _event("e", minute=4),
                _event("f", minute=5),
            ]
        )
        del lines[1]  # break lands on what is now the 2nd consumed line
        _put_segment(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", lines)

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )
        # 6 lines - 1 deleted = 5 remaining; break on the 2nd (index 1) of
        # those 5 leaves 3 further lines (indices 2, 3, 4) never read.
        assert [e.connection_id for e in result.events] == ["a"]
        assert result.chain_breaks == 1
        assert result.lines_skipped_after_chain_break == 3
        assert "3 further line(s)" in result.note

    async def test_the_key_rotation_sentence_excludes_chain_break_lines(self, s3):
        # TODO.md item 172 follow-up (WS-172-4, security-invariant-reviewer,
        # 2026-08-10): a chain-break line's own hash DID verify — it is not
        # a key-mismatch candidate — so it must not inflate the "usually a
        # key rotation" sentence's count, and that sentence must not appear
        # for a request with a chain break but no genuine hash mismatch.
        lines = _chain_lines([_event("a", minute=0), _event("b", minute=1), _event("c", minute=2)])
        del lines[1]
        _put_segment(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", lines)

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )
        assert result.unverified == 1
        assert result.chain_breaks == 1
        # The chain-break sentence appears...
        assert "broken internal chain link" in result.note
        assert "not a key mismatch" in result.note
        # ...but the key-rotation sentence, which would otherwise fire on
        # any unverified > 0, must NOT — every unverified line here is
        # accounted for by the chain break, not a genuine hash mismatch.
        assert "AUDIT_LEDGER_HMAC_KEY" not in result.note

    async def test_the_resumed_page_seed_walk_does_not_scan_an_unbounded_blank_run(self, s3):
        # TODO.md item 172 follow-up (WS-172-7 secondary point,
        # security-invariant-reviewer, 2026-08-10): a long blank run between
        # two real records must not make resuming mid-object expensive —
        # this is the integration-level correctness check that a resume
        # landing immediately after a real record (the ordinary case: a
        # genuine segment has ZERO blank lines at all — see
        # `_seed_chain_state_from_predecessor`'s docstring — so any
        # cursor position issued against a real archive, however it was
        # issued, is always distance 1 from its predecessor, making the
        # seed walk's own predecessor lookup a single, trivial step) still
        # returns the right event once the FORWARD loop has skipped a large
        # blank run to reach it, with the true, intact chain correctly
        # confirmed (no false-positive break). This shape does not drive the
        # seed walk anywhere near its own step bound — see
        # `TestSeedChainStateFromPredecessor` below for that, including the
        # WS-172-8 fail-closed behavior when the bound genuinely is
        # exhausted.
        chained = _chain_lines([_event("a", minute=0), _event("b", minute=1)])
        # 5,000 blank lines between "a" and "b" — exercises the FORWARD
        # loop's blank-skipping cost, not the backward seed walk (whose own
        # bound is pinned directly, not indirectly through this fixture).
        lines = [chained[0]] + [""] * 5000 + [chained[1]]
        _put_segment(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", lines)

        first = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            limit=1,
            bounds=_bounds(default_limit=1),
        )
        assert [e.connection_id for e in first.events] == ["a"]
        assert first.next_cursor is not None

        # Resume right after "a": consume_from's immediate predecessor IS
        # "a" (distance 1), so the seed walk finds it trivially regardless
        # of the 5,000 blanks that follow — those are consumed by the
        # forward loop as it advances from "a" to "b", not walked backward.
        second = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            cursor=first.next_cursor,
            bounds=_bounds(),
        )
        assert [e.connection_id for e in second.events] == ["b"]
        assert second.chain_breaks == 0

    async def test_resuming_deep_inside_a_blank_run_past_the_walk_bound_fails_closed(self, s3):
        # TODO.md item 172 follow-up (WS-172-8, security-invariant-reviewer,
        # 2026-08-11): an end-to-end reproduction of the seed walk's OWN
        # step bound actually firing — unlike the adjacent test above (whose
        # cursor always lands one step after a real record, so the seed
        # walk never travels far), this cursor is built directly to resume
        # from deep inside a long blank run: more than
        # `_SEED_WALK_MAX_STEPS` lines from the nearest real record in
        # either direction it has already looked. A cursor landing here in
        # production could only come from a crafted/corrupted object (a
        # genuine segment has zero blank lines — see
        # `_seed_chain_state_from_predecessor`'s docstring); this test
        # proves that when it does happen, the resumed page fails closed
        # (reports a chain break) instead of silently accepting an
        # unverifiable incoming link.
        max_steps = worm_search_module._SEED_WALK_MAX_STEPS
        chained = _chain_lines([_event("a", minute=0), _event("b", minute=1)])
        blank_run = max_steps + 2000
        lines = [chained[0]] + [""] * blank_run + [chained[1]]
        key = f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl"
        _put_segment(s3, key, lines)

        start_time = datetime(2026, 3, 15, tzinfo=timezone.utc)
        end_time = datetime(2026, 3, 16, tzinfo=timezone.utc)
        fingerprint = worm_search_module._filters_fingerprint(
            start_time, end_time, None, None, None
        )
        # Resume line 2500: more than max_steps back from "a" (index 0),
        # and every line in between is blank — the walk cannot reach "a"
        # (start=2499, floor=2499-max_steps=1475, all indices 2499..1476
        # visited are blank), so it must exhaust its bound.
        cursor = worm_search_module._encode_cursor(date(2026, 3, 15), key, 2500, fingerprint)

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=start_time,
            end_time=end_time,
            cursor=cursor,
            bounds=_bounds(),
        )
        # "b"'s own hash verifies, but its incoming link could not be
        # confirmed within the walk's bound, so it is NOT returned as an
        # event — the page fails closed and discloses a chain break rather
        # than silently accepting "b" as given.
        assert result.events == []
        assert result.chain_breaks == 1


class TestSeedChainStateFromPredecessor:
    """TODO.md item 172 follow-up (WS-172-7, security-invariant-reviewer,
    2026-08-10): direct, deterministic unit tests of the extracted
    `_seed_chain_state_from_predecessor` helper — precise about the exact
    step bound, which the integration-level test above can only observe
    indirectly (via the graceful-degradation behavior, not an iteration
    count)."""

    def test_a_deeply_nested_predecessor_line_is_not_raised(self):
        """TODO.md item 194 defect (2) on the SEED WALK specifically. The
        line loop's own handler does not cover this path: the seed walk has
        its own `json.loads`, reached when a resumed page seeds its chain
        state from a preceding line. A RecursionError here escaped
        `search_worm_archive` exactly like the line loop's did."""
        good = _chain_lines([_event("a", minute=0)])[0]
        nested = "[" * 1000 + "]" * 1000
        # See the line-loop test: a successful parse returns the identical
        # tuple this asserts, so without pinning the premise this test goes
        # vacuous the moment the recursion limit changes.
        with pytest.raises(RecursionError):
            json.loads(nested)

        prev_hash, prev_seq, exhausted = worm_search_module._seed_chain_state_from_predecessor(
            [nested, good], 1, ledger_key=None
        )

        assert (prev_hash, prev_seq, exhausted) == (None, None, False)

    def test_finds_the_predecessor_within_the_bound(self):
        events = _chain_lines([_event("a", minute=0), _event("b", minute=1)])
        prev_hash, prev_seq, exhausted = worm_search_module._seed_chain_state_from_predecessor(
            events, 1, ledger_key=None
        )
        parsed_a = json.loads(events[0])
        assert prev_hash == parsed_a["hash"]
        assert prev_seq == parsed_a["seq"]
        assert exhausted is False

    def test_skips_blank_lines_within_the_bound(self):
        # lines = [a, "", "", b]; consume_from=3 means "b" (index 3) is the
        # line about to be consumed, so its PREDECESSOR "a" (index 0) is
        # what the walk must find, skipping the two blanks in between.
        events = _chain_lines([_event("a", minute=0), _event("b", minute=1)])
        lines = [events[0], "", "", events[1]]
        prev_hash, prev_seq, exhausted = worm_search_module._seed_chain_state_from_predecessor(
            lines, 3, ledger_key=None
        )
        parsed_a = json.loads(events[0])
        assert prev_hash == parsed_a["hash"]
        assert prev_seq == parsed_a["seq"]
        assert exhausted is False

    def test_gives_up_exactly_at_the_step_bound_not_one_past_it(self):
        # "a" sits at index 0; the line being resumed sits at index
        # max_steps + 1, one step beyond what a max_steps-length walk
        # starting at index max_steps (max_steps+1 - 1) can reach (it visits
        # indices max_steps..1, never reaching index 0) — must NOT be found,
        # and the walk genuinely used its whole budget doing so (WS-172-8).
        events = _chain_lines([_event("a", minute=0), _event("b", minute=1)])
        max_steps = worm_search_module._SEED_WALK_MAX_STEPS
        resume_at = max_steps + 1
        lines = [events[0]] + [""] * (resume_at - 1) + [events[1]]
        prev_hash, prev_seq, exhausted = worm_search_module._seed_chain_state_from_predecessor(
            lines, resume_at, ledger_key=None
        )
        assert (prev_hash, prev_seq) == (None, None)
        assert exhausted is True

    def test_finds_the_predecessor_exactly_at_the_step_bound(self):
        # Same shape, resumed one position earlier: the walk starts at
        # index max_steps - 1 and visits exactly max_steps indices down to
        # (and including) index 0 — "a" is the last one checked, and must
        # be found.
        events = _chain_lines([_event("a", minute=0), _event("b", minute=1)])
        max_steps = worm_search_module._SEED_WALK_MAX_STEPS
        resume_at = max_steps
        lines = [events[0]] + [""] * (resume_at - 1) + [events[1]]
        prev_hash, prev_seq, exhausted = worm_search_module._seed_chain_state_from_predecessor(
            lines, resume_at, ledger_key=None
        )
        parsed_a = json.loads(events[0])
        assert prev_hash == parsed_a["hash"]
        assert prev_seq == parsed_a["seq"]
        assert exhausted is False

    def test_the_ledger_key_is_actually_passed_through_to_verification(self):
        # test-contract-reviewer, 2026-08-11: every other test in this class
        # uses ledger_key=None (matching the shipped default), which cannot
        # catch a swapped/dropped `key=` argument in the extracted helper —
        # a broken passthrough would silently misclassify every resumed page
        # on an HMAC-keyed deployment as a chain break. Pin the keyed case
        # directly: seeding with the SAME key the chain was written with
        # must find the predecessor; the WRONG key must not.
        key = b"a-real-hmac-key"
        events = _chain_lines([_event("a", minute=0), _event("b", minute=1)], key=key)
        parsed_a = json.loads(events[0])

        prev_hash, prev_seq, exhausted = worm_search_module._seed_chain_state_from_predecessor(
            events, 1, ledger_key=key
        )
        assert prev_hash == parsed_a["hash"]
        assert prev_seq == parsed_a["seq"]
        assert exhausted is False

        prev_hash, prev_seq, exhausted = worm_search_module._seed_chain_state_from_predecessor(
            events, 1, ledger_key=b"the-wrong-key"
        )
        assert (prev_hash, prev_seq) == (None, None)
        assert exhausted is False

    def test_a_predecessor_that_does_not_verify_yields_nothing_to_seed_from(self):
        events = _chain_lines([_event("a", minute=0)], key=b"a-different-key")
        prev_hash, prev_seq, exhausted = worm_search_module._seed_chain_state_from_predecessor(
            events, 1, ledger_key=None
        )
        # A predecessor that doesn't verify is a DIFFERENT case than the
        # bound firing (WS-172-8): the caller's existing accept-as-given
        # carve-out still applies here, not the new fail-closed path — the
        # walk learned something concrete (this line is untrustworthy), it
        # didn't merely run out of budget.
        assert (prev_hash, prev_seq) == (None, None)
        assert exhausted is False

    def test_does_not_step_past_a_non_verifying_line_to_seed_from_a_stale_one_behind_it(self):
        # A non-verifying predecessor must stop the walk right there, not
        # skip over it looking for an older line that DOES verify — that
        # older line is not actually this record's true predecessor.
        stale = _chain_lines([_event("a", minute=0)])
        tampered = json.loads(stale[0])
        tampered["hash"] = "0" * 64  # corrupt: no longer recomputes
        lines = [stale[0], json.dumps(tampered)]
        prev_hash, prev_seq, exhausted = worm_search_module._seed_chain_state_from_predecessor(
            lines, 2, ledger_key=None
        )
        assert (prev_hash, prev_seq) == (None, None)
        assert exhausted is False

    def test_a_corrupt_json_predecessor_yields_nothing_to_seed_from(self):
        lines = ["{not valid json"]
        prev_hash, prev_seq, exhausted = worm_search_module._seed_chain_state_from_predecessor(
            lines, 1, ledger_key=None
        )
        assert (prev_hash, prev_seq) == (None, None)
        assert exhausted is False

    def test_consume_from_past_the_end_of_lines_does_not_crash(self):
        # WS-172-1's own regression (an object shorter than a stale cursor
        # expected) — clamped via min(consume_from, len(lines)), not an
        # IndexError.
        events = _chain_lines([_event("a", minute=0)])
        prev_hash, prev_seq, exhausted = worm_search_module._seed_chain_state_from_predecessor(
            events, 10_000, ledger_key=None
        )
        parsed_a = json.loads(events[0])
        assert prev_hash == parsed_a["hash"]
        assert prev_seq == parsed_a["seq"]
        assert exhausted is False

    def test_empty_lines_does_not_crash(self):
        prev_hash, prev_seq, exhausted = worm_search_module._seed_chain_state_from_predecessor(
            [], 5, ledger_key=None
        )
        assert (prev_hash, prev_seq) == (None, None)
        assert exhausted is False

    def test_a_predecessor_whose_seq_is_type_confused_fails_closed(self):
        # TODO.md item 178: the predecessor's own hash verifies, so the
        # pre-fix code seeded `prev_seq` with the raw string and the very
        # next `seq == prev_seq + 1` raised TypeError. It must instead yield
        # NO seed at all AND fail closed — returning the hash without a
        # position would just move the same TypeError one branch along.
        chained = _chain_lines([_event("a", minute=0), _event("b", minute=1)])
        lines = [_retype_seq(chained[0], "0"), chained[1]]
        # Premise: the crafted predecessor genuinely still verifies.
        assert verify_envelope_hash(json.loads(lines[0]), key=None) is True

        prev_hash, prev_seq, fail_closed = worm_search_module._seed_chain_state_from_predecessor(
            lines, 1, ledger_key=None
        )
        assert (prev_hash, prev_seq) == (None, None)
        assert fail_closed is True


class TestTypeConfusedSeq:
    """TODO.md item 178: a WORM record whose `seq` is type-confused rather
    than corrupt — hash-valid, but not the `int` `LedgerRecord.seq` is typed
    as — must be counted like any other forgery class, never raise.

    Pre-fix, both raw `parsed.get("seq")` reads fed `seq == prev_seq + 1`
    directly, so a crafted string raised `TypeError` out of
    `search_worm_archive` and escaped the route as a generic 500.
    """

    # `str`/`float`/`bool` are the three that genuinely reach `_chain_seq` in
    # the integrated path: each coerces under `LedgerRecord.model_validate`,
    # so the digest is computed over the coerced value and the crafted line
    # VERIFIES. The rest (`null`, list, dict) fail validation earlier and are
    # already counted `unverified`, so they only pin the helper's own
    # defensive contract — do not read them as evidence about the scan path.
    @pytest.mark.parametrize(
        "raw_seq",
        ["3", 3.0, True, None, "0", [3], {"seq": 3}],
        ids=["str", "float", "bool", "null", "str-zero", "list", "dict"],
    )
    def test_a_non_int_seq_is_no_position_at_all(self, raw_seq):
        assert worm_search_module._chain_seq({"seq": raw_seq}) is None

    def test_a_genuine_int_seq_is_returned_unchanged(self):
        # The positive control, and it needs BOTH assertions: `== 0` alone
        # also passes for a `return False` mutant (`False == 0` is True), so
        # the non-zero case is what pins a real int. Without this test a
        # `_chain_seq` returning None for everything would satisfy every
        # other case in this class while disabling chain verification.
        assert worm_search_module._chain_seq({"seq": 0}) == 0
        assert worm_search_module._chain_seq({"seq": 7}) == 7

    def test_a_missing_seq_or_a_non_dict_is_no_position_at_all(self):
        # Defensive branch only — unreachable from both production call
        # sites, which run after `verify_envelope_hash` already established a
        # dict with all four envelope keys. See `_chain_seq`'s docstring.
        assert worm_search_module._chain_seq({}) is None
        assert worm_search_module._chain_seq("not a dict") is None

    @pytest.mark.parametrize("raw_seq", ["0", 0.0, False], ids=["str", "float", "bool"])
    async def test_a_type_confused_predecessor_on_a_resumed_page_is_a_chain_break(
        self, s3, raw_seq
    ):
        # Site 1 of 2 (`_seed_chain_state_from_predecessor`): the resumed
        # page's predecessor is hash-valid with a non-int `seq`, so the seed
        # carried it into the linkage arithmetic. `line` is a
        # caller-controlled cursor field (see the module docstring), and the
        # archive is the surface an attacker with `s3:PutObject` writes.
        # Parametrized over all three classes that reach here, which did NOT
        # behave alike pre-fix: a `str` raised `TypeError`, while a `float`
        # and a `bool` were silently ACCEPTED as a valid link (`1 == 0.0 + 1`
        # and `1 == False + 1` are both True) — so the crafted record was
        # returned as a genuine event. All three must now be chain breaks.
        # `False` (not `True`) is the bool that coerces to this record's own
        # seq of 0, which is what keeps the premise assertion below true.
        chained = _chain_lines([_event("a", minute=0), _event("b", minute=1)])
        lines = [_retype_seq(chained[0], raw_seq), chained[1]]
        assert verify_envelope_hash(json.loads(lines[0]), key=None) is True
        key = f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl"
        _put_segment(s3, key, lines)

        start_time = datetime(2026, 3, 15, tzinfo=timezone.utc)
        end_time = datetime(2026, 3, 16, tzinfo=timezone.utc)
        fingerprint = worm_search_module._filters_fingerprint(
            start_time, end_time, None, None, None
        )
        cursor = worm_search_module._encode_cursor(date(2026, 3, 15), key, 1, fingerprint)

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=start_time,
            end_time=end_time,
            cursor=cursor,
            bounds=_bounds(),
        )
        # "b" is not returned: its incoming link could not be confirmed
        # against a predecessor whose claimed position is unusable.
        assert result.events == []
        assert result.chain_breaks == 1
        assert result.unverified == 1
        # POSITIONAL DISCRIMINATOR (test-contract-reviewer, 2026-08-12).
        # This test and the genesis test below plant byte-identical segment
        # bytes and assert an identical count triple — only the hand-built
        # cursor's `line` makes this one exercise the SEED path. Since
        # `_decode_cursor` defaults a missing `line` to 0, a future cursor
        # reshape could silently resume at 0 and turn this into a duplicate
        # of the genesis test. Breaking on the LAST line skips nothing;
        # breaking on line 0 of 2 skips 1. That is the only observable that
        # differs between the two positions, so it is what pins the path.
        assert result.lines_skipped_after_chain_break == 0

    async def test_a_type_confused_record_accepted_as_given_does_not_poison_the_next_link(self, s3):
        # Site 2 of 2 (the main line loop's own `seq` read): the resumed
        # page's predecessor does NOT verify, so the first consumed record
        # takes the accept-as-given branch — where, pre-fix, no comparison
        # touched its `seq` at all, and the raw string was carried forward
        # into `prev_seq` and raised on the FOLLOWING line.
        chained = _chain_lines(
            [_event("a", minute=0), _event("b", minute=1), _event("c", minute=2)]
        )
        corrupt = json.loads(chained[0])
        corrupt["hash"] = "0" * 64  # no longer recomputes: nothing to seed from
        lines = [json.dumps(corrupt), _retype_seq(chained[1], "1"), chained[2]]
        assert verify_envelope_hash(json.loads(lines[1]), key=None) is True
        key = f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl"
        _put_segment(s3, key, lines)

        start_time = datetime(2026, 3, 15, tzinfo=timezone.utc)
        end_time = datetime(2026, 3, 16, tzinfo=timezone.utc)
        fingerprint = worm_search_module._filters_fingerprint(
            start_time, end_time, None, None, None
        )
        cursor = worm_search_module._encode_cursor(date(2026, 3, 15), key, 1, fingerprint)

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=start_time,
            end_time=end_time,
            cursor=cursor,
            bounds=_bounds(),
        )
        assert result.events == []
        assert result.chain_breaks == 1
        assert result.unverified == 1
        # Fail-closed stopped consumption at the crafted line, so "c" was
        # never read — and the scale of that is disclosed, not silent.
        assert result.lines_skipped_after_chain_break == 1

    async def test_a_type_confused_genesis_record_is_a_chain_break_not_an_error(self, s3):
        # The unpaged case: a crafted first line of a segment. `"0" == 0` is
        # already False so this never raised, but it must still be counted
        # (and must keep being counted once `_chain_seq` short-circuits the
        # branch) rather than quietly compared away.
        # NOTE this is a CHARACTERIZATION test — it passes against the whole
        # pre-fix module. The main-loop `seq` read is pinned as a regression
        # only by `test_a_type_confused_record_accepted_as_given_...` above;
        # the two are not interchangeable, so do not delete that one on the
        # strength of this one.
        chained = _chain_lines([_event("a", minute=0), _event("b", minute=1)])
        lines = [_retype_seq(chained[0], "0"), chained[1]]
        assert verify_envelope_hash(json.loads(lines[0]), key=None) is True
        _put_segment(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", lines)

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )
        assert result.events == []
        assert result.chain_breaks == 1
        assert result.unverified == 1
        # The other half of the positional discriminator: breaking on line 0
        # of 2 leaves exactly one line unread.
        assert result.lines_skipped_after_chain_break == 1
        # The operator-facing disclosure must name THIS cause, not just count
        # it — the pre-existing sentence explains a break as records being
        # "reordered or removed", which is the wrong investigation to send a
        # compliance reviewer on when a position field was retyped instead.
        assert "not an integer at all" in result.note


class TestPagination:
    async def test_a_full_page_sets_truncated_and_a_resumable_cursor(self, s3):
        _put_events(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [_event("a", minute=0), _event("b", minute=1)],
        )
        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            limit=1,
            bounds=_bounds(default_limit=1),
        )
        assert len(result.events) == 1
        assert result.truncated is True
        assert result.next_cursor is not None

    async def test_a_cursor_pointing_past_the_end_of_an_object_does_not_crash(self, s3):
        # TODO.md item 172 follow-up (WS-172-7, security-invariant-reviewer,
        # 2026-08-10): the WS-172-1 fix added a backward seed-walk indexed
        # by the cursor's own `line` field — a value the module's own
        # documented cursor contract already treats as untrustworthy ("a
        # resumption hint, not a promise the archive is unchanged"). A
        # cursor whose `line` exceeds the object's actual current length
        # (a hand-edited cursor, or an object legitimately replaced by a
        # shorter one since the cursor was issued — Object Lock prevents
        # deleting a version, not PUTting a new one) must degrade
        # gracefully, not raise IndexError.
        _put_events(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [_event("a", minute=0), _event("b", minute=1)],
        )
        first = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            limit=1,
            bounds=_bounds(default_limit=1),
        )
        assert first.next_cursor is not None
        payload = json.loads(base64.urlsafe_b64decode(first.next_cursor))
        payload["line"] = 10_000  # the real object only has 2 lines
        forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            cursor=forged,
            bounds=_bounds(default_limit=1),
        )
        assert result.events == []

    async def test_paging_through_a_full_cursor_chain_yields_every_event_exactly_once(self, s3):
        _put_events(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [_event("a", minute=0), _event("b", minute=1)],
        )
        _put_events(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120200-000001.jsonl",
            [_event("c", minute=2), _event("d", minute=3)],
        )
        bounds = _bounds(default_limit=1)
        seen = []
        cursor = None
        for _ in range(10):
            result = await search_worm_archive(
                bucket=_BUCKET,
                prefix=_PREFIX,
                region="us-east-1",
                flush_interval_seconds=60,
                start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
                end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
                cursor=cursor,
                bounds=bounds,
            )
            seen.extend(e.connection_id for e in result.events)
            cursor = result.next_cursor
            if cursor is None:
                break
        assert sorted(seen) == ["a", "b", "c", "d"]

    async def test_request_timeout_cap_truncates_mid_scan(self, s3, monkeypatch):
        """The wall-clock bound (request_timeout_seconds) must truncate a
        scan exactly like the objects-scanned cap does — mirrors
        test_max_objects_scanned_cap_truncates_mid_scan's shape, but drives
        the OTHER bound. Deliberately mutation-checked: prior to this test
        being added, deleting the `deadline = ...`/`>= deadline` checks left
        every test in this file green (found by `test-contract-reviewer`,
        2026-08-06).

        A naive fake `time.monotonic()` keyed on raw call COUNT is flaky
        here: asyncio's own event-loop scheduling calls `time.monotonic()`
        internally (it backs `loop.time()` on most platforms), so the
        number of calls between two `await` points is an implementation
        detail of the event loop, not of this module's own logic — a prior
        version of this test counted calls and was intermittently wrong
        for exactly that reason. Instead, the fake is keyed on a genuine
        application-level event (one object actually fetched via
        `_object_size`, monkeypatched to bump a counter), decoupling
        "expire the deadline" from unrelated asyncio internals entirely."""
        _put_events(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", [_event("a")])
        _put_events(s3, f"{_PREFIX}2026/03/15/20260315T120100-000001.jsonl", [_event("b")])

        real_monotonic = time.monotonic()
        state = {"objects_fetched": 0}
        real_object_size = worm_search_module._object_size

        async def counting_object_size(client, bucket, key):
            size = await real_object_size(client, bucket, key)
            state["objects_fetched"] += 1
            return size

        def fake_monotonic():
            # "Expires" only once at least one object has genuinely been
            # fetched — never before, regardless of how many unrelated
            # asyncio-internal calls land in between.
            return real_monotonic + 10_000 if state["objects_fetched"] >= 1 else real_monotonic

        monkeypatch.setattr(worm_search_module, "_object_size", counting_object_size)
        monkeypatch.setattr(time, "monotonic", fake_monotonic)

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(request_timeout_seconds=1000.0),
        )
        assert result.objects_scanned == 1
        assert result.truncated is True
        assert result.next_cursor is not None

    async def test_a_single_oversized_object_truncates_mid_object_on_the_deadline(
        self, s3, monkeypatch
    ):
        # TODO.md item 154 (security-invariant-reviewer, WS-154-4): per-line
        # envelope verification made this loop's body meaningfully more
        # expensive, and — pre-existing, merely amplified — nothing inside a
        # SINGLE object's line loop ever checked the wall-clock deadline
        # before this fix; the two existing deadline tests above both fire
        # BETWEEN objects. A single anomalous object at the line cap could
        # block uninterrupted past request_timeout_seconds entirely. Proven
        # with a real multi-thousand-line single object and a deadline that
        # "expires" only after genuine per-line verification work has
        # started (same decoupling-from-asyncio-internals technique as
        # test_request_timeout_cap_truncates_mid_scan just above).
        events = [_event("a", minute=(i % 60)) for i in range(2500)]
        _put_events(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", events)

        real_monotonic = time.monotonic()
        state = {"verified": 0}
        real_verify = worm_search_module.verify_envelope_hash

        def counting_verify(parsed, *, key=None):
            state["verified"] += 1
            return real_verify(parsed, key=key)

        def fake_monotonic():
            return real_monotonic + 10_000 if state["verified"] >= 10 else real_monotonic

        monkeypatch.setattr(worm_search_module, "verify_envelope_hash", counting_verify)
        monkeypatch.setattr(time, "monotonic", fake_monotonic)

        # default_limit/max_limit raised well past 2500 (the default is 50)
        # so the pre-existing PAGE-SIZE cap can't be what truncates this —
        # isolating the new mid-object DEADLINE check as the only possible
        # cause. (Confirmed by mutation: without this override, deleting the
        # new deadline check left this test passing for the wrong reason —
        # the page filled at the default limit=50 regardless.)
        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(default_limit=5000, max_limit=5000),
        )
        assert result.truncated is True
        assert result.next_cursor is not None
        # Truncated INSIDE the object, not just "ran out of objects" — only
        # a fraction of the 2500 events made it into the page. The deadline
        # check fires at line_no == 1000 (the first periodic checkpoint
        # after the 10th verification call expires the fake clock), so
        # exactly the first 1000 lines were consumed.
        assert len(result.events) == 1000
        assert result.malformed == 0
        assert result.unverified == 0

    async def test_max_objects_scanned_cap_truncates_mid_scan(self, s3):
        _put_events(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", [_event("a")])
        _put_events(s3, f"{_PREFIX}2026/03/15/20260315T120100-000001.jsonl", [_event("b")])
        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(max_objects_scanned=1),
        )
        assert result.objects_scanned == 1
        assert result.truncated is True
        assert result.next_cursor is not None

    async def test_a_cursor_pointing_at_a_since_deleted_segment_resumes_gracefully(self, s3):
        """A cursor is a resumption hint, not a promise the archive is
        unchanged (e.g. an operator's own lifecycle policy expired a segment
        after the cursor was issued).

        To actually exercise the `except ValueError` fallback in
        `search_worm_archive`'s `keys.index(resume_key)` lookup, the object
        deleted between the two calls must be the one the CURSOR ITSELF
        points at (key2/"b" — `_next_position_cursor` advances the cursor to
        the NEXT key once the current one is exhausted), not the
        already-fully-consumed key1/"a". Deleting key1 instead (a prior
        version of this test did) never exercises the fallback at all:
        `keys.index("...key2...")` still succeeds normally against a listing
        that still contains key2, so the test passed for an unrelated
        reason (found by `test-contract-reviewer`, 2026-08-06)."""
        _put_events(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", [_event("a")])
        _put_events(s3, f"{_PREFIX}2026/03/15/20260315T120100-000001.jsonl", [_event("b")])
        _put_events(s3, f"{_PREFIX}2026/03/15/20260315T120200-000001.jsonl", [_event("c")])
        bounds = _bounds(default_limit=1)
        first = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=bounds,
        )
        assert len(first.events) == 1
        assert first.events[0].connection_id == "a"

        # first.next_cursor points at the "b" segment (the next key after the
        # one just consumed) — delete THAT object, not the already-consumed
        # "a" one, to genuinely simulate "the cursor's target is gone".
        s3.delete_object(Bucket=_BUCKET, Key=f"{_PREFIX}2026/03/15/20260315T120100-000001.jsonl")

        second = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            cursor=first.next_cursor,
            bounds=bounds,
        )
        # The deleted "b" segment is skipped gracefully; resumption lands on
        # "c", the next key that sorts after the missing one.
        assert len(second.events) == 1
        assert second.events[0].connection_id == "c"

    async def test_an_object_past_the_per_object_line_cap_truncates_instead_of_skipping(
        self, s3, monkeypatch
    ):
        """_MAX_LINES_PER_OBJECT must TRUNCATE (disclose) rather than
        silently drop content past the cap — an object this anomalous is
        exactly the case where an honest "did not finish reading this"
        matters most (security-invariant-reviewer, 2026-08-06, WS-3)."""
        monkeypatch.setattr(worm_search_module, "_MAX_LINES_PER_OBJECT", 3)
        events = [_event("a", minute=0), _event("b", minute=1), _event("c", minute=2)]
        events += [_event("d", minute=3), _event("e", minute=4)]
        _put_events(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", events)

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )
        assert len(result.events) == 3
        assert [e.connection_id for e in result.events] == ["a", "b", "c"]
        assert result.truncated is True
        assert result.next_cursor is not None

    async def test_an_oversized_object_is_skipped_before_being_fully_read(self, s3, monkeypatch):
        """_MAX_OBJECT_BYTES must be checked via a lightweight size probe
        BEFORE the object body is read into memory — a corrupted or
        adversarially oversized object must never be fully buffered
        (security-invariant-reviewer, 2026-08-06, WS-5)."""
        monkeypatch.setattr(worm_search_module, "_MAX_OBJECT_BYTES", 10)
        fetched = {"called": False}
        real_get_text = worm_search_module._get_object_text

        async def tracking_get_object_text(client, bucket, key):
            fetched["called"] = True
            return await real_get_text(client, bucket, key)

        monkeypatch.setattr(worm_search_module, "_get_object_text", tracking_get_object_text)
        _put_events(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", [_event("a")])

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )
        assert fetched["called"] is False
        assert result.events == []
        assert result.objects_scanned == 1
        assert result.truncated is True

    async def test_a_day_with_more_keys_than_the_object_budget_stops_listing_early(self, s3):
        """_list_day_keys's own max_keys bound (WS-6) must stop accumulating
        keys once the request's object budget is exhausted, even when a
        SINGLE S3 page already returns more than that budget in one call —
        MaxKeys is passed to list_objects_v2 specifically so one oversized
        page can't slip past the pre-fetch check."""
        for minute in range(3):
            _put_events(
                s3,
                f"{_PREFIX}2026/03/15/20260315T12000{minute}-000001.jsonl",
                [_event(f"c{minute}", minute=minute)],
            )
        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(max_objects_scanned=2),
        )
        assert result.objects_scanned == 2
        assert result.truncated is True
        assert result.next_cursor is not None


class TestDayListingTruncationIsResumable:
    """TODO.md item 184. When one calendar day holds more segments than
    `max_objects_scanned`, `_list_day_keys` returns the first N with
    `stopped_early=True`. The day-truncation cursor must then resume
    EXCLUSIVELY after the last key actually consumed — a cursor pointing back
    at the start of the same day makes every segment past key N permanently
    unreachable and turns a good-faith pager into an infinite loop."""

    @staticmethod
    async def _drain(*, bounds_kwargs: dict, max_laps: int = 25):
        """Follow next_cursor to exhaustion, asserting the chain terminates
        and that no cursor is ever reissued (the infinite-loop symptom)."""
        seen_cursors = set()
        connections = []
        laps = 0
        cursor = None
        while True:
            laps += 1
            assert (
                laps <= max_laps
            ), f"cursor chain did not terminate within {max_laps} laps — collected {connections}"
            result = await search_worm_archive(
                bucket=_BUCKET,
                prefix=_PREFIX,
                region="us-east-1",
                flush_interval_seconds=60,
                start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
                end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
                cursor=cursor,
                bounds=_bounds(**bounds_kwargs),
            )
            connections.extend(e.connection_id for e in result.events)
            if result.next_cursor is None:
                assert result.truncated is False
                return connections, laps
            assert result.next_cursor not in seen_cursors, (
                "next_cursor repeated — the pager would loop forever "
                f"(lap {laps}, cursor {result.next_cursor!r})"
            )
            seen_cursors.add(result.next_cursor)
            cursor = result.next_cursor

    @pytest.mark.parametrize("default_limit", [50, 2, 1])
    async def test_a_day_truncated_by_the_object_budget_pages_to_exhaustion(
        self, s3, default_limit
    ):
        """Item 184's first acceptance criterion: 5 single-event segments in
        one day with max_objects_scanned=2, followed to exhaustion, yield all
        5 events exactly once with no cursor repeating.

        Parametrized over `default_limit` because the original fix — and the
        original version of this test — only ever exercised the ONE exit where
        the day's key loop runs to completion and falls through to
        `if listing_truncated:`. At `default_limit=1` the page fills on the
        last LISTED key instead, taking `_next_position_cursor`, which knew
        nothing about the truncated listing and advanced to the next day:
        three of five records were silently unreachable and the final page
        reported `truncated=False`. All four reviewers found this
        independently; it reproduced as `['c0','c1']` before the follow-up
        fix. A single un-parametrized value is why a green suite hid it."""
        for minute in range(5):
            _put_events(
                s3,
                f"{_PREFIX}2026/03/15/20260315T12000{minute}-000001.jsonl",
                [_event(f"c{minute}", minute=minute)],
            )

        connections, _ = await self._drain(
            bounds_kwargs={"max_objects_scanned": 2, "default_limit": default_limit}
        )

        assert sorted(connections) == ["c0", "c1", "c2", "c3", "c4"]

    @pytest.mark.parametrize("default_limit", [3, 2])
    async def test_multi_event_segments_behind_a_truncated_listing_page_exactly_once(
        self, s3, default_limit
    ):
        """The architecture reviewer's Variant B: multi-event segments make a
        page fill MID-object as well as at an object boundary, so the
        mid-object line cursor and the page-filled cursor both have to carry
        the day's `after` marker. Without it a resumed page re-lists the day
        from its start, `keys.index(resume_key)` misses, no object is
        consumed, and the scan oscillates between two cursors re-delivering
        the events between them."""
        for minute in range(5):
            _put_events(
                s3,
                f"{_PREFIX}2026/03/15/20260315T12000{minute}-000001.jsonl",
                [_event(f"c{minute}a", minute=minute), _event(f"c{minute}b", minute=minute)],
            )

        connections, _ = await self._drain(
            bounds_kwargs={"max_objects_scanned": 2, "default_limit": default_limit}
        )

        assert sorted(connections) == sorted(
            [f"c{m}{half}" for m in range(5) for half in ("a", "b")]
        )

    async def test_an_oversized_object_behind_a_truncated_listing_stays_on_its_day(
        self, s3, monkeypatch
    ):
        """The oversized-object exit is a third way control leaves a day whose
        listing was cut short. It must not advance to the next day either — an
        object over `_MAX_OBJECT_BYTES` is skipped, but the segments the
        listing never reached are still owed to the caller.

        The oversized object is deliberately the LAST key the truncated
        listing returns. An earlier version of this test placed it third,
        where `max_objects_scanned=2` never listed it, so the assertion was
        satisfied by the ordinary fall-through and the exit was untested."""
        # Above a one-event chained segment (~700 B) and below the planted body,
        # so only the intended object trips the cap.
        monkeypatch.setattr(worm_search_module, "_MAX_OBJECT_BYTES", 4096)
        _put_events(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [_event("c0", minute=0)],
        )
        oversized_key = f"{_PREFIX}2026/03/15/20260315T120000-000002.jsonl"
        _put_segment(s3, oversized_key, ["x" * 8192])
        for minute in (1, 2):
            _put_events(
                s3,
                f"{_PREFIX}2026/03/15/20260315T12000{minute}-000001.jsonl",
                [_event(f"c{minute}", minute=minute)],
            )

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(max_objects_scanned=2),
        )

        assert result.truncated is True
        payload = json.loads(base64.urlsafe_b64decode(result.next_cursor))
        assert payload["day"] == "2026-03-15", "the scan abandoned a day it had not finished"
        assert payload["key"] is None
        assert payload["after"] == oversized_key, "the day resumed before what it already read"

    async def test_the_chain_break_counter_counts_one_break_once_across_a_cursor_chain(self, s3):
        """Item 184's second acceptance criterion: one broken segment behind a
        day-truncating object budget must move
        querygate_audit_worm_search_chain_breaks_total by exactly 1 across the
        WHOLE cursor chain. Re-listing the day from its start on every lap
        re-counts the same break once per lap, so the counter's magnitude would
        track how long the pager ran rather than how many segments broke."""
        broken = _chain_lines(
            [_event("b0", minute=0), _event("b1", minute=1), _event("b2", minute=2)]
        )
        del broken[1]
        _put_segment(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", broken)
        for minute in range(1, 5):
            _put_events(
                s3,
                f"{_PREFIX}2026/03/15/20260315T12000{minute}-000001.jsonl",
                [_event(f"c{minute}", minute=minute)],
            )

        before_breaks = _sample("querygate_audit_worm_search_chain_breaks_total", {})

        await self._drain(bounds_kwargs={"max_objects_scanned": 2})

        assert _sample("querygate_audit_worm_search_chain_breaks_total", {}) == before_breaks + 1

    async def test_a_day_truncation_cursor_resumes_after_the_last_consumed_key(self, s3):
        """The cursor's own payload, not just the paging outcome: a
        day-truncated page must carry an exclusive `after` marker naming the
        last key it consumed, and no `key`. Asserted directly so the encoding
        contract cannot regress silently behind a still-passing pager."""
        for minute in range(4):
            _put_events(
                s3,
                f"{_PREFIX}2026/03/15/20260315T12000{minute}-000001.jsonl",
                [_event(f"c{minute}", minute=minute)],
            )

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(max_objects_scanned=2),
        )

        assert result.truncated is True
        payload = json.loads(base64.urlsafe_b64decode(result.next_cursor))
        assert payload["key"] is None
        assert payload["after"] == f"{_PREFIX}2026/03/15/20260315T120001-000001.jsonl"

    async def test_a_resumed_day_advances_its_marker_strictly_forward(self, s3):
        """Consecutive laps over one over-budget day must move the marker
        strictly forward. Going backwards re-delivers segments and re-counts
        their integrity findings, which is symptom 3 of item 184's write-up.

        Named for what it covers: the object-budget disjunct at the top of the
        key loop is NOT reachable within the marker-carrying day, because
        `_list_day_keys` is capped at `max_objects_scanned`, so the counter can
        only reach the cap after the last listed key is consumed and the loop
        has already ended. An earlier version of this test claimed that exit."""
        for minute in range(5):
            _put_events(
                s3,
                f"{_PREFIX}2026/03/15/20260315T12000{minute}-000001.jsonl",
                [_event(f"c{minute}", minute=minute)],
            )
        first = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(max_objects_scanned=2),
        )
        marker = json.loads(base64.urlsafe_b64decode(first.next_cursor))["after"]

        second = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            cursor=first.next_cursor,
            bounds=_bounds(max_objects_scanned=2),
        )

        payload = json.loads(base64.urlsafe_b64decode(second.next_cursor))
        assert payload["after"] is not None
        assert payload["after"] > marker, "the resumed page went backwards"

    async def test_an_over_long_after_marker_is_rejected_not_handed_to_s3(self, s3):
        """`after` is the only cursor field that reaches the AWS wire, so it
        is held to S3's own key ceiling rather than trusted (WS-194-1)."""
        forged = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "day": "2026-03-15",
                    "key": None,
                    "line": 0,
                    "after": "k" * 2000,
                    "fp": worm_search_module._filters_fingerprint(
                        datetime(2026, 3, 15, tzinfo=timezone.utc),
                        datetime(2026, 3, 16, tzinfo=timezone.utc),
                        None,
                        None,
                        None,
                    ),
                }
            ).encode()
        ).decode()

        with pytest.raises(QueryValidationError) as excinfo:
            await search_worm_archive(
                bucket=_BUCKET,
                prefix=_PREFIX,
                region="us-east-1",
                flush_interval_seconds=60,
                start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
                end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
                cursor=forged,
                bounds=_bounds(),
            )

        # Every decode failure collapses into one generic public message, so
        # without checking the cause this would also pass if the cursor were
        # rejected for an unrelated reason (a fingerprint or `day` change).
        assert "S3 key length" in str(excinfo.value.__cause__)

    async def test_a_surrogate_after_marker_is_a_422_not_a_masked_500(self, s3):
        """A lone surrogate survives `json.loads` as a `str`, so an
        `isinstance` check passes it — then botocore's percent-encoding
        raises `UnicodeEncodeError`, which escapes as the masked 500 item 194
        exists to eliminate AND lets any caller move the `error` counter."""
        before_error = _sample("querygate_audit_worm_search_requests_total", {"outcome": "error"})
        forged = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "day": "2026-03-15",
                    "key": None,
                    "line": 0,
                    "after": "\ud800",
                    "fp": worm_search_module._filters_fingerprint(
                        datetime(2026, 3, 15, tzinfo=timezone.utc),
                        datetime(2026, 3, 16, tzinfo=timezone.utc),
                        None,
                        None,
                        None,
                    ),
                }
            ).encode()
        ).decode()

        with pytest.raises(QueryValidationError) as excinfo:
            await search_worm_archive(
                bucket=_BUCKET,
                prefix=_PREFIX,
                region="us-east-1",
                flush_interval_seconds=60,
                start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
                end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
                cursor=forged,
                bounds=_bounds(),
            )

        assert "not UTF-8 encodable" in str(excinfo.value.__cause__)
        assert (
            _sample("querygate_audit_worm_search_requests_total", {"outcome": "error"})
            == before_error
        ), "a forged cursor moved the S3-failure counter"

    async def test_an_object_past_the_per_object_line_cap_pages_to_exhaustion(
        self, s3, monkeypatch
    ):
        """WS-134-1. `last_line` used to be `min(len(lines), cap)` — an
        ABSOLUTE ceiling rather than a per-page budget. A resumed page whose
        `consume_from` already equalled the cap recomputed the same
        `last_line`, consumed nothing, and re-emitted a byte-identical cursor
        forever, making every line past the cap and every later object and day
        permanently unreachable. Reachable with no forgery by raising
        AUDIT_WORM_MAX_BUFFERED_EVENTS above the cap, and by anyone holding
        s3:PutObject with a ~1 MB body of newlines.

        The pre-existing test for this exit asserted only that the FIRST page
        truncates; it never followed the cursor, which is why a
        non-advancing cursor sat here undetected."""
        monkeypatch.setattr(worm_search_module, "_MAX_LINES_PER_OBJECT", 2)
        _put_events(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [_event(f"c{i}", minute=i) for i in range(5)],
        )

        connections, _ = await self._drain(bounds_kwargs={})

        assert sorted(connections) == ["c0", "c1", "c2", "c3", "c4"]

    async def test_a_resumed_page_keeps_its_marker_when_the_deadline_stops_it_mid_object(
        self, s3, monkeypatch
    ):
        """WS-184-2 / TCR-13. The line-loop deadline exit was the one in-day
        cursor still emitted without the day's `after` marker, so a resumed
        page re-listed the day from its start, could not find its own key, and
        walked BACKWARD to the start of the previous window — re-delivering
        records and, with a consistently exhausted budget, cycling between two
        cursors forever. Both other reviewers found it independently."""
        for minute in range(5):
            _put_events(
                s3,
                f"{_PREFIX}2026/03/15/20260315T12000{minute}-000001.jsonl",
                [_event(f"c{minute}", minute=minute)],
            )
        window = dict(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
        )
        first = await search_worm_archive(**window, bounds=_bounds(max_objects_scanned=2))
        marker = json.loads(base64.urlsafe_b64decode(first.next_cursor))["after"]
        assert marker is not None

        # Expire the clock the moment the resumed page opens its first object,
        # so the deadline fires inside the LINE loop rather than between
        # objects. Keyed to an application event, not a call count.
        real_get = worm_search_module._get_object_text
        clock = {"now": time.monotonic()}

        async def expiring_get(*args, **kwargs):
            result = await real_get(*args, **kwargs)
            clock["now"] += 10_000
            return result

        monkeypatch.setattr(worm_search_module, "_get_object_text", expiring_get)
        monkeypatch.setattr(worm_search_module.time, "monotonic", lambda: clock["now"])

        second = await search_worm_archive(
            **window, cursor=first.next_cursor, bounds=_bounds(max_objects_scanned=2)
        )

        assert second.truncated is True
        payload = json.loads(base64.urlsafe_b64decode(second.next_cursor))
        assert payload["after"] == marker, "the resumed page lost its listing window"

    async def test_a_pre_item_184_cursor_without_an_after_marker_still_decodes(self, s3):
        """Backward compatibility: a cursor issued before item 184 has no
        `after` field at all. It must still decode and resume at its day's
        start rather than being rejected as malformed."""
        _put_events(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [_event("a", minute=0)],
        )
        legacy = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "day": "2026-03-15",
                    "key": None,
                    "line": 0,
                    "fp": worm_search_module._filters_fingerprint(
                        datetime(2026, 3, 15, tzinfo=timezone.utc),
                        datetime(2026, 3, 16, tzinfo=timezone.utc),
                        None,
                        None,
                        None,
                    ),
                }
            ).encode()
        ).decode()

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            cursor=legacy,
            bounds=_bounds(),
        )

        assert [e.connection_id for e in result.events] == ["a"]

    async def test_a_non_string_after_marker_is_rejected_not_handed_to_s3(self, s3):
        """`after` is attacker-reachable in a hand-crafted cursor and flows
        straight into a `StartAfter` listing argument. A non-string must be
        rejected as an invalid cursor (4xx) rather than reaching boto3 and
        surfacing as a masked 500."""
        forged = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "day": "2026-03-15",
                    "key": None,
                    "line": 0,
                    "after": {"not": "a string"},
                    "fp": worm_search_module._filters_fingerprint(
                        datetime(2026, 3, 15, tzinfo=timezone.utc),
                        datetime(2026, 3, 16, tzinfo=timezone.utc),
                        None,
                        None,
                        None,
                    ),
                }
            ).encode()
        ).decode()

        with pytest.raises(QueryValidationError):
            await search_worm_archive(
                bucket=_BUCKET,
                prefix=_PREFIX,
                region="us-east-1",
                flush_interval_seconds=60,
                start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
                end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
                cursor=forged,
                bounds=_bounds(),
            )


class TestCorruptLinesAreCountedNotRaised:
    """TODO.md item 194 defects (1) and (2). Each of these lines is supposed
    to be COUNTED (`malformed`/`unverified`) and skipped. Before the fix each
    instead raised out of `search_worm_archive`, hit the route's
    `mask_unexpected()` and became a generic HTTP 500 — costing availability
    plus every genuine record the page had already accumulated. Because a
    WORM object is immutable, one bad object poisoned every future search
    whose window covered that day, permanently."""

    async def test_a_non_ascii_hash_is_counted_unverified_not_raised(self, s3):
        """Defect (1), the one that needs no attacker: `_get_object_text`
        decodes with errors="replace", so a single corrupted byte inside a
        genuine segment's `hash` becomes U+FFFD, and `hmac.compare_digest`
        raises TypeError on a non-ASCII str."""
        line = json.loads(_chain_lines([_event("a", minute=0)])[0])
        line["hash"] = line["hash"][:-1] + "\ufffd"
        _put_segment(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", [json.dumps(line)])

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )

        assert result.unverified == 1
        assert result.events == []

    async def test_a_deeply_nested_line_is_counted_malformed_not_raised(self, s3):
        """Defect (2): `json.loads` raises RecursionError (a RuntimeError, so
        NOT caught by `except json.JSONDecodeError`) on a deeply-nested line.
        1,000 nested arrays is a ~2 KB line — four orders of magnitude under
        _MAX_OBJECT_BYTES, so the byte bounds are no defence at all."""
        nested = "[" * 1000 + "]" * 1000
        # Pin the premise: if the recursion limit ever rises, `json.loads`
        # SUCCEEDS, returns a list, and the line is still counted `malformed`
        # (not envelope-shaped) — so every assertion below would pass while
        # covering nothing. Fail loudly on a changed premise instead.
        with pytest.raises(RecursionError):
            json.loads(nested)
        _put_segment(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", [nested])

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )

        assert result.malformed == 1
        assert result.events == []

    async def test_a_deeply_nested_line_does_not_discard_the_rest_of_the_page(self, s3):
        """The availability half of the defect: a genuine record sharing the
        segment with a poisoned line must still be returned. A raised
        RecursionError discarded the whole page, so this fails differently
        from the counting test above and is worth its own case."""
        nested = "[" * 1000 + "]" * 1000
        with pytest.raises(RecursionError):
            json.loads(nested)
        good = _chain_lines([_event("survivor", minute=0)])
        _put_segment(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", [nested] + good)

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )

        assert result.malformed == 1
        assert [e.connection_id for e in result.events] == ["survivor"]


class TestBoundsRejection:
    def test_a_default_limit_above_the_max_limit_is_a_configuration_error(self):
        """A misconfigured default above the documented ceiling would
        silently exceed the enforced page-size cap on every request that
        omits `limit`, while still rejecting an explicit `limit` above the
        same ceiling — a self-contradicting bound
        (security-invariant-reviewer, 2026-08-06, WS-4)."""
        with pytest.raises(pyd.ValidationError, match="must not exceed max_limit"):
            WormSearchBounds(
                max_window_seconds=1,
                max_objects_scanned=1,
                default_limit=1000,
                max_limit=10,
                request_timeout_seconds=1,
            )

    async def test_missing_start_time_is_rejected(self, s3):
        with pytest.raises(QueryValidationError, match="requires both start_time and end_time"):
            await search_worm_archive(
                bucket=_BUCKET,
                prefix=_PREFIX,
                region="us-east-1",
                flush_interval_seconds=60,
                start_time=None,
                end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
                bounds=_bounds(),
            )

    async def test_missing_end_time_is_rejected(self, s3):
        with pytest.raises(QueryValidationError, match="requires both start_time and end_time"):
            await search_worm_archive(
                bucket=_BUCKET,
                prefix=_PREFIX,
                region="us-east-1",
                flush_interval_seconds=60,
                start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
                end_time=None,
                bounds=_bounds(),
            )

    async def test_end_before_start_is_rejected(self, s3):
        with pytest.raises(QueryValidationError, match="end_time must be after start_time"):
            await search_worm_archive(
                bucket=_BUCKET,
                prefix=_PREFIX,
                region="us-east-1",
                flush_interval_seconds=60,
                start_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
                end_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
                bounds=_bounds(),
            )

    async def test_window_wider_than_the_cap_is_rejected(self, s3):
        with pytest.raises(QueryValidationError, match="exceeds the maximum searchable window"):
            await search_worm_archive(
                bucket=_BUCKET,
                prefix=_PREFIX,
                region="us-east-1",
                flush_interval_seconds=60,
                start_time=datetime(2020, 1, 1, tzinfo=timezone.utc),
                end_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
                bounds=_bounds(max_window_seconds=86400 * 30),
            )

    async def test_a_window_exactly_at_the_cap_is_accepted(self, s3):
        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 1, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 31, tzinfo=timezone.utc),
            bounds=_bounds(max_window_seconds=86400 * 30),
        )
        assert result.truncated is False

    async def test_limit_above_the_cap_is_rejected(self, s3):
        with pytest.raises(QueryValidationError, match="limit must be between"):
            await search_worm_archive(
                bucket=_BUCKET,
                prefix=_PREFIX,
                region="us-east-1",
                flush_interval_seconds=60,
                start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
                end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
                limit=10_000,
                bounds=_bounds(max_limit=500),
            )

    async def test_limit_below_one_is_rejected(self, s3):
        with pytest.raises(QueryValidationError, match="limit must be between"):
            await search_worm_archive(
                bucket=_BUCKET,
                prefix=_PREFIX,
                region="us-east-1",
                flush_interval_seconds=60,
                start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
                end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
                limit=0,
                bounds=_bounds(),
            )

    async def test_garbage_cursor_is_rejected(self, s3):
        with pytest.raises(QueryValidationError, match="Invalid search cursor"):
            await search_worm_archive(
                bucket=_BUCKET,
                prefix=_PREFIX,
                region="us-east-1",
                flush_interval_seconds=60,
                start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
                end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
                cursor="not-a-real-cursor",
                bounds=_bounds(),
            )

    async def test_cursor_from_a_different_filter_set_is_rejected(self, s3):
        bounds = _bounds(default_limit=1)
        first = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=bounds,
        )
        # Reuse a syntactically valid cursor shape but forge a mismatched
        # fingerprint by hand — simulates replaying a page 1 cursor against
        # different filters.
        payload = json.loads(base64.urlsafe_b64decode(first.next_cursor or _fake_cursor()))
        payload["fp"] = "0" * 16
        forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        with pytest.raises(QueryValidationError, match="Invalid search cursor"):
            await search_worm_archive(
                bucket=_BUCKET,
                prefix=_PREFIX,
                region="us-east-1",
                flush_interval_seconds=60,
                start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
                end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
                connection_id="different-filter-now",
                cursor=first.next_cursor or _fake_cursor(),
                bounds=bounds,
            )


def _fake_cursor() -> str:
    payload = {"day": "2026-03-15", "key": None, "line": 0, "fp": "0" * 16}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


class TestMetrics:
    async def test_a_served_request_increments_the_ok_outcome(self, s3):
        before = _sample("querygate_audit_worm_search_requests_total", {"outcome": "ok"})
        await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )
        after = _sample("querygate_audit_worm_search_requests_total", {"outcome": "ok"})
        assert after == before + 1

    async def test_a_rejected_request_increments_the_rejected_outcome_not_ok(self, s3):
        before_ok = _sample("querygate_audit_worm_search_requests_total", {"outcome": "ok"})
        before_rejected = _sample(
            "querygate_audit_worm_search_requests_total", {"outcome": "rejected"}
        )
        with pytest.raises(QueryValidationError):
            await search_worm_archive(
                bucket=_BUCKET,
                prefix=_PREFIX,
                region="us-east-1",
                flush_interval_seconds=60,
                start_time=None,
                end_time=None,
                bounds=_bounds(),
            )
        after_ok = _sample("querygate_audit_worm_search_requests_total", {"outcome": "ok"})
        after_rejected = _sample(
            "querygate_audit_worm_search_requests_total", {"outcome": "rejected"}
        )
        assert after_ok == before_ok
        assert after_rejected == before_rejected + 1

    async def test_objects_scanned_metric_tracks_get_object_calls(self, s3):
        _put_events(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", [_event("a")])
        _put_events(s3, f"{_PREFIX}2026/03/15/20260315T120100-000001.jsonl", [_event("b")])
        before = _sample("querygate_audit_worm_search_objects_scanned_total", {})
        await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )
        after = _sample("querygate_audit_worm_search_objects_scanned_total", {})
        assert after == before + 2

    # TODO.md item 177: the two integrity signals must be alertable, not just
    # visible to whoever runs an ad-hoc search over the right window.

    async def test_a_chain_break_increments_both_integrity_counters(self, s3):
        # The same middle-record-removed fixture TestChainLinkage uses: the
        # break is one segment (chain_breaks) whose breaking line is also
        # counted unverified, exactly mirroring the result model's own
        # relationship between the two fields.
        lines = _chain_lines([_event("a", minute=0), _event("b", minute=1), _event("c", minute=2)])
        del lines[1]
        _put_segment(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", lines)
        before_breaks = _sample("querygate_audit_worm_search_chain_breaks_total", {})
        before_unverified = _sample("querygate_audit_worm_search_unverified_total", {})

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )

        assert result.chain_breaks == 1
        assert result.unverified == 1
        # The counters must agree with the response the same request returned —
        # a counter that drifts from the disclosed result is worse than none.
        assert _sample("querygate_audit_worm_search_chain_breaks_total", {}) == (
            before_breaks + result.chain_breaks
        )
        assert _sample("querygate_audit_worm_search_unverified_total", {}) == (
            before_unverified + result.unverified
        )

    async def test_a_hash_mismatch_increments_unverified_but_not_chain_breaks(self, s3):
        # A key mismatch (the common, benign cause) must not light up the
        # chain-break counter — the whole point of publishing them separately
        # is that one is alertable and the other is usually a rotated key.
        lines = _chain_lines([_event("a", minute=0)], key=b"a-different-ledger-key")
        _put_segment(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", lines)
        before_breaks = _sample("querygate_audit_worm_search_chain_breaks_total", {})
        before_unverified = _sample("querygate_audit_worm_search_unverified_total", {})

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )

        assert result.unverified == 1
        assert result.chain_breaks == 0
        assert _sample("querygate_audit_worm_search_unverified_total", {}) == before_unverified + 1
        assert _sample("querygate_audit_worm_search_chain_breaks_total", {}) == before_breaks

    async def test_the_counters_carry_the_real_magnitude_not_just_a_boolean(self, s3):
        # Every other test in this class produces exactly 0 or 1 of each signal,
        # so `.inc(1 if chain_breaks else 0)` / `.inc(min(unverified, 1))` would
        # pass all of them AND the rest of the suite — leaving the documented
        # `unverified_total - chain_breaks_total` arithmetic and the whole
        # magnitude story resting on nothing. Two broken segments plus one
        # key-mismatched segment give chain_breaks=2, unverified=3.
        broken_a = _chain_lines(
            [_event("a", minute=0), _event("b", minute=1), _event("c", minute=2)]
        )
        del broken_a[1]
        _put_segment(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", broken_a)
        broken_b = _chain_lines(
            [_event("d", minute=3), _event("e", minute=4), _event("f", minute=5)]
        )
        del broken_b[1]
        _put_segment(s3, f"{_PREFIX}2026/03/15/20260315T120100-000001.jsonl", broken_b)
        _put_segment(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120200-000001.jsonl",
            _chain_lines([_event("g", minute=6)], key=b"a-different-ledger-key"),
        )
        before_breaks = _sample("querygate_audit_worm_search_chain_breaks_total", {})
        before_unverified = _sample("querygate_audit_worm_search_unverified_total", {})

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )

        assert result.chain_breaks == 2
        # Two chain-break lines (whose own hashes verified) + one true hash
        # mismatch — the documented `unverified ⊇ chain_breaks` relationship.
        assert result.unverified == 3
        assert _sample("querygate_audit_worm_search_chain_breaks_total", {}) == before_breaks + 2
        assert _sample("querygate_audit_worm_search_unverified_total", {}) == before_unverified + 3
        # The subtraction the metric help text and PRODUCT_GUIDE both document
        # as "the part actually likely to be a key mismatch" — asserted on the
        # COUNTER deltas, not on the two response fields above (that form would
        # be pure arithmetic on assertions already made three lines up, and
        # could not fail independently).
        breaks_delta = _sample("querygate_audit_worm_search_chain_breaks_total", {}) - before_breaks
        unverified_delta = (
            _sample("querygate_audit_worm_search_unverified_total", {}) - before_unverified
        )
        assert unverified_delta - breaks_delta == 1

    async def test_malformed_lines_move_neither_integrity_counter(self, s3):
        # `malformed` (not envelope-shaped at all — garbage/corruption) is a
        # deliberately DISTINCT class from `unverified` (item 154), because the
        # latter usually means a rotated key. `.inc(unverified + malformed)`
        # would be green everywhere else in this file, and would page an
        # operator with a "hash didn't recompute" rate driven by ordinary
        # corruption — exactly the conflation that split exists to prevent.
        _put_segment(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [json.dumps({"not": "an envelope"})],
        )
        before_breaks = _sample("querygate_audit_worm_search_chain_breaks_total", {})
        before_unverified = _sample("querygate_audit_worm_search_unverified_total", {})

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )

        assert result.malformed == 1
        assert result.unverified == 0
        assert result.chain_breaks == 0
        assert _sample("querygate_audit_worm_search_chain_breaks_total", {}) == before_breaks
        assert _sample("querygate_audit_worm_search_unverified_total", {}) == before_unverified

    async def test_the_same_break_is_counted_once_per_scan_not_once_per_segment(self, s3):
        # Pins the deliberate semantics the help text now discloses: the
        # counters count FINDINGS PER SCAN, not distinct broken segments. A WORM
        # object is immutable, so a genuine break is permanent and every later
        # search over that window re-counts it. Recorded as a test so a future
        # change to per-segment dedup has to be a conscious decision with a doc
        # update, rather than a silent "fix".
        lines = _chain_lines([_event("a", minute=0), _event("b", minute=1), _event("c", minute=2)])
        del lines[1]
        _put_segment(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", lines)
        before_breaks = _sample("querygate_audit_worm_search_chain_breaks_total", {})

        for _ in range(2):
            await search_worm_archive(
                bucket=_BUCKET,
                prefix=_PREFIX,
                region="us-east-1",
                flush_interval_seconds=60,
                start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
                end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
                bounds=_bounds(),
            )

        assert _sample("querygate_audit_worm_search_chain_breaks_total", {}) == before_breaks + 2

    async def test_an_intact_archive_leaves_both_integrity_counters_untouched(self, s3):
        # The control: without this, both assertions above would also pass if
        # the counters incremented on every request regardless of findings.
        _put_events(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [_event("a", minute=0), _event("b", minute=1)],
        )
        before_breaks = _sample("querygate_audit_worm_search_chain_breaks_total", {})
        before_unverified = _sample("querygate_audit_worm_search_unverified_total", {})

        result = await search_worm_archive(
            bucket=_BUCKET,
            prefix=_PREFIX,
            region="us-east-1",
            flush_interval_seconds=60,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            bounds=_bounds(),
        )

        assert result.chain_breaks == 0
        assert result.unverified == 0
        # `_sample` returns `or 0.0`, so an equality-to-`before` assertion alone
        # would also pass if the series did not exist at all (a renamed or
        # misspelled metric). Pin existence separately.
        assert (
            REGISTRY.get_sample_value("querygate_audit_worm_search_chain_breaks_total") is not None
        )
        assert REGISTRY.get_sample_value("querygate_audit_worm_search_unverified_total") is not None
        assert _sample("querygate_audit_worm_search_chain_breaks_total", {}) == before_breaks
        assert _sample("querygate_audit_worm_search_unverified_total", {}) == before_unverified

    def test_both_integrity_counters_carry_no_labels(self):
        # The no-label shape is a SECURITY claim (docs/THREAT_MODEL.md QG-40,
        # CHANGELOG): a `connection`/`principal` label would be caller-chosen
        # cardinality and a weak per-principal activity oracle over the audit
        # archive to anyone who can read /metrics. Until now it was backed only
        # incidentally — by the fact that the other tests sample with `{}` and
        # `.inc()` on a labelled metric raises. Assert the property directly so
        # adding a label is a deliberate, visibly-failing decision.
        #
        # Via the PUBLIC registry API rather than `Counter._labelnames`: a
        # labelled Counter has no child until `.labels()` is called and can
        # never emit a sample whose label dict is empty, so an empty-label
        # sample existing is exactly "this metric is unlabelled" — same
        # property, no private attribute to break on a library upgrade.
        assert (
            REGISTRY.get_sample_value("querygate_audit_worm_search_chain_breaks_total", {})
            is not None
        )
        assert (
            REGISTRY.get_sample_value("querygate_audit_worm_search_unverified_total", {})
            is not None
        )

    async def test_a_result_that_fails_to_build_counts_the_signals_once_not_twice(self, s3):
        # The invariant that justified DELETING the idempotence guard from
        # `_count_integrity_signals` (TODO.md item 177): the served path and the
        # error path are mutually exclusive, because `_finalize` does nothing
        # that can raise between counting and returning.
        #
        # This pins the invariant BEHAVIOURALLY rather than by inspecting source
        # shape. Make `WormSearchResult` construction itself raise — the one
        # statement inside `_finalize` that precedes the counters — and the
        # request must reach the `except Exception` handler having counted
        # ZERO times, then count exactly once. If a future edit moves
        # `_count_integrity_signals()` above the construction (the WS-7 hazard
        # the ordering comment exists to prevent), or inserts any fallible
        # statement after it, this fails with +2.
        #
        # The fixture deliberately returns via the PAGE-FILLED call site, which
        # is inside the `try` — that is the only place the double-count hazard
        # exists. (The final `return _finalize(...)` after the loop sits OUTSIDE
        # the `try`, so a construction failure there propagates having counted
        # nothing at all, not even `outcome="error"`. That asymmetry is
        # pre-existing and equally true of the request counter; it is a
        # not-counted case, never a double-counted one.) So: a broken first
        # segment (counted, then skipped) followed by an intact segment whose
        # event fills the page at limit=2.
        broken = _chain_lines([_event("a", minute=0), _event("b", minute=1), _event("c", minute=2)])
        del broken[1]
        _put_segment(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", broken)
        _put_events(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120100-000001.jsonl",
            [_event("d", minute=3), _event("e", minute=4)],
        )
        before_breaks = _sample("querygate_audit_worm_search_chain_breaks_total", {})
        before_unverified = _sample("querygate_audit_worm_search_unverified_total", {})
        before_ok = _sample("querygate_audit_worm_search_requests_total", {"outcome": "ok"})
        before_errors = _sample("querygate_audit_worm_search_requests_total", {"outcome": "error"})

        def exploding_result(**kwargs):
            raise RuntimeError("result model failed to build")

        with patch.object(worm_search_module, "WormSearchResult", exploding_result):
            with pytest.raises(RuntimeError, match="result model failed to build"):
                await search_worm_archive(
                    bucket=_BUCKET,
                    prefix=_PREFIX,
                    region="us-east-1",
                    flush_interval_seconds=60,
                    start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
                    end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
                    limit=2,
                    bounds=_bounds(),
                )

        assert _sample("querygate_audit_worm_search_chain_breaks_total", {}) == before_breaks + 1
        assert _sample("querygate_audit_worm_search_unverified_total", {}) == before_unverified + 1
        # And the pre-existing WS-7 ordering rationale, which had never had a
        # test either: a request whose model fails to build is counted "error",
        # never also "ok".
        assert _sample("querygate_audit_worm_search_requests_total", {"outcome": "ok"}) == before_ok
        assert (
            _sample("querygate_audit_worm_search_requests_total", {"outcome": "error"})
            == before_errors + 1
        )

    async def test_a_cancelled_scan_counts_nothing(self, s3):
        # README and docs/THREAT_MODEL.md QG-40 both state that a request
        # cancelled by client disconnect or shutdown counts nothing, because
        # `asyncio.CancelledError` derives from `BaseException` and the handler
        # catches `Exception`. That is a deliberate documented limit, so it
        # needs a test: widening the handler to `except BaseException:` (a
        # plausible "always record something" edit) would silently contradict
        # two shipped documents with the whole suite green.
        lines = _chain_lines([_event("a", minute=0), _event("b", minute=1), _event("c", minute=2)])
        del lines[1]
        _put_segment(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", lines)
        _put_events(s3, f"{_PREFIX}2026/03/16/20260316T120000-000001.jsonl", [_event("d")])
        before_breaks = _sample("querygate_audit_worm_search_chain_breaks_total", {})
        before_unverified = _sample("querygate_audit_worm_search_unverified_total", {})
        before_errors = _sample("querygate_audit_worm_search_requests_total", {"outcome": "error"})

        real_get = s3.get_object
        seen = {"n": 0}

        def cancelling_get_object(**kwargs):
            # Let the broken segment be read, then cancel the request.
            seen["n"] += 1
            if seen["n"] > 1:
                raise asyncio.CancelledError()
            return real_get(**kwargs)

        with patch.object(s3, "get_object", side_effect=cancelling_get_object):
            with patch("boto3.client", return_value=s3):
                with pytest.raises(asyncio.CancelledError):
                    await search_worm_archive(
                        bucket=_BUCKET,
                        prefix=_PREFIX,
                        region="us-east-1",
                        flush_interval_seconds=60,
                        start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
                        end_time=datetime(2026, 3, 17, tzinfo=timezone.utc),
                        bounds=_bounds(),
                    )

        assert _sample("querygate_audit_worm_search_chain_breaks_total", {}) == before_breaks
        assert _sample("querygate_audit_worm_search_unverified_total", {}) == before_unverified
        assert (
            _sample("querygate_audit_worm_search_requests_total", {"outcome": "error"})
            == before_errors
        )

    def test_the_worm_search_has_exactly_one_production_call_site(self):
        # The narrowed scope claim on four surfaces ("QueryGate does not scan
        # the archive on a schedule") rests entirely on there being no caller
        # other than the scope-gated REST route. Pin it, mirroring
        # test_create_async_engine_has_exactly_one_production_call_site — so
        # adding a background verifier or an MCP tool forces the disclosed
        # limitation to be revisited instead of silently going stale.
        src = pathlib.Path(worm_search_module.__file__).resolve().parents[1]
        callers = []
        for path in src.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith("#") or "def build_worm_search_result" in stripped:
                    continue
                if "build_worm_search_result(" in stripped:
                    callers.append(f"{path.name}:{lineno}")
        assert callers == ["admin_observability_routes.py:242"], callers

    async def test_a_chain_break_found_before_a_mid_scan_failure_is_still_counted(self, s3):
        # The path the ok-only placement would lose: a scan that finds a break
        # and THEN fails against S3 never reaches _finalize, so without the
        # error-path call the strongest tamper signal this surface produces
        # would be swallowed by the exception.
        lines = _chain_lines([_event("a", minute=0), _event("b", minute=1), _event("c", minute=2)])
        del lines[1]
        _put_segment(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", lines)
        _put_events(s3, f"{_PREFIX}2026/03/16/20260316T120000-000001.jsonl", [_event("d")])
        before_breaks = _sample("querygate_audit_worm_search_chain_breaks_total", {})
        before_unverified = _sample("querygate_audit_worm_search_unverified_total", {})
        before_errors = _sample("querygate_audit_worm_search_requests_total", {"outcome": "error"})

        real_get = s3.get_object
        seen = {"n": 0}

        def exploding_get_object(**kwargs):
            # Let the first (broken) segment be read, then fail the scan.
            seen["n"] += 1
            if seen["n"] > 1:
                raise RuntimeError("s3 went away mid-scan")
            return real_get(**kwargs)

        with patch.object(s3, "get_object", side_effect=exploding_get_object):
            with patch("boto3.client", return_value=s3):
                with pytest.raises(RuntimeError, match="s3 went away mid-scan"):
                    await search_worm_archive(
                        bucket=_BUCKET,
                        prefix=_PREFIX,
                        region="us-east-1",
                        flush_interval_seconds=60,
                        start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
                        end_time=datetime(2026, 3, 17, tzinfo=timezone.utc),
                        bounds=_bounds(),
                    )

        assert (
            _sample("querygate_audit_worm_search_requests_total", {"outcome": "error"})
            == before_errors + 1
        )
        assert _sample("querygate_audit_worm_search_chain_breaks_total", {}) == before_breaks + 1
        # BOTH counters must survive the failure — README/CHANGELOG claim both,
        # and the two increments only share a closure today, which is an
        # implementation detail, not the contract.
        assert _sample("querygate_audit_worm_search_unverified_total", {}) == before_unverified + 1


class TestBuildWormSearchResult:
    def _config(self, **overrides) -> AppConfig:
        kwargs = dict(
            environment="localhost",
            mcp_enabled=False,
            audit_sink_backend="jsonl_chained_s3_worm",
            audit_jsonl_path="var/audit/chain.jsonl",
            audit_worm_s3_bucket=_BUCKET,
            audit_worm_s3_prefix=_PREFIX,
            audit_worm_s3_region="us-east-1",
        )
        kwargs.update(overrides)
        return AppConfig(**kwargs)

    async def test_the_configured_s3_endpoint_reaches_the_search_client(self, s3):
        """TODO.md item 201. The flush monitor and the managed search must
        read the SAME `audit_worm_s3_endpoint_url`, or an archive would be
        written to one store and searched at another — silently returning
        empty results against a perfectly intact archive."""
        captured = {}
        real_client = boto3.client

        def spy(service, **kwargs):
            captured.update(kwargs)
            return real_client(service, **kwargs)

        cfg = self._config(audit_worm_s3_endpoint_url="https://s3.amazonaws.com")
        with patch("boto3.client", spy):
            await build_worm_search_result(
                cfg,
                start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
                end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            )

        assert captured["endpoint_url"] == "https://s3.amazonaws.com"

    async def test_no_configured_endpoint_leaves_the_search_client_on_aws(self, s3):
        """The empty default must reach boto3 as None, not "" — boto3 treats
        an empty string as a real (invalid) endpoint."""
        captured = {}
        real_client = boto3.client

        def spy(service, **kwargs):
            captured.update(kwargs)
            return real_client(service, **kwargs)

        with patch("boto3.client", spy):
            await build_worm_search_result(
                self._config(),
                start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
                end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            )

        assert captured["endpoint_url"] is None

    async def test_disabled_backend_reports_honestly_without_touching_s3(self):
        cfg = self._config(audit_sink_backend="none")
        result = await build_worm_search_result(
            cfg,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
        )
        assert result.source == "disabled"
        assert result.events == []

    async def test_disabled_backend_still_rejects_a_missing_time_range(self):
        """'start_time/end_time are required on every request' must be a
        genuine, unconditional contract — not one that quietly relaxes into
        an unvalidated 200 just because the backend happens to be disabled
        (claim-reviewer, 2026-08-06)."""
        cfg = self._config(audit_sink_backend="none")
        with pytest.raises(QueryValidationError, match="requires both start_time and end_time"):
            await build_worm_search_result(cfg, start_time=None, end_time=None)

    async def test_disabled_backend_still_rejects_an_over_wide_window(self):
        cfg = self._config(audit_sink_backend="none")
        with pytest.raises(QueryValidationError, match="exceeds the maximum searchable window"):
            await build_worm_search_result(
                cfg,
                start_time=datetime(2000, 1, 1, tzinfo=timezone.utc),
                end_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )

    async def test_enabled_backend_wires_config_through_to_a_real_search(self, s3):
        _put_events(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", [_event("a")])
        cfg = self._config()
        result = await build_worm_search_result(
            cfg,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
        )
        assert result.source == "s3_worm"
        assert len(result.events) == 1

    async def test_enabled_backend_wires_the_ledger_key_through_to_verification(self, s3):
        # TODO.md item 154: build_worm_search_result must resolve
        # cfg.audit_ledger_hmac_key and pass it through to
        # search_worm_archive's ledger_key — proven by a segment that is
        # HMAC-signed and therefore ONLY verifies under that exact key.
        _put_segment(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            _chain_lines([_event("a")], key=b"cfg-wired-key"),
        )
        cfg = self._config(audit_ledger_hmac_key="cfg-wired-key")
        result = await build_worm_search_result(
            cfg,
            start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
        )
        assert len(result.events) == 1
        assert result.malformed == 0
