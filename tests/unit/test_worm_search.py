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


def _put_events(client, key: str, events: list) -> None:
    _put_segment(client, key, [e.model_dump_json(exclude_none=True) for e in events])


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
            [_event("a", minute=0).model_dump_json(exclude_none=True), "{not valid json"],
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
