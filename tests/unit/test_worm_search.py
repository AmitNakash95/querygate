"""Unit tests for TODO.md item 134 phase 2: managed search over the WORM
S3 archive (`audit/worm_search.py`). Exercised against moto's S3 emulation
(matching phase 1's own test approach — no real AWS credentials are
available in this environment), covering real search/filter/pagination
behavior, not just that the function is callable.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timedelta, timezone

import boto3
import pydantic as pyd
import pytest
from moto import mock_aws

from querygate.audit import worm_search as worm_search_module
from querygate.audit.events import AuditEvent, ConnectionProbeEvent
from querygate.audit.ledger import GENESIS_PREV_HASH, make_record
from querygate.audit.worm_search import (
    WormSearchBounds,
    build_worm_search_result,
    search_worm_archive,
)
from querygate.core.config import AppConfig
from querygate.core.exceptions import QueryValidationError
from querygate.metrics import REGISTRY

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
