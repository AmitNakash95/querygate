"""Unit tests for TODO.md item 134's WORM audit retention: the in-process
buffer, the S3-backed flush monitor (against a mocked S3 via moto — real S3
Object Lock is not reachable in this test environment, so behavior is
verified against moto's own faithful Object Lock emulation, confirmed
separately to accept the same ObjectLockMode/ObjectLockRetainUntilDate
parameters a real bucket does), the composite sink that fans an event out to
both the local chain and WORM, and the registry-dispatched
configure_audit_sink.
"""

from __future__ import annotations

import asyncio
import json

import boto3
import pytest
from moto import mock_aws

from querygate.audit.events import AuditEvent
from querygate.audit.sinks import (
    CompositeAuditSink,
    HashChainedAuditSink,
    NullAuditSink,
    configure_audit_sink,
    get_audit_sink,
    reset_audit_sink,
)
from querygate.audit.worm_sink import (
    InProcessWormEventBuffer,
    S3WormAuditSink,
    WormFlushMonitor,
    get_worm_buffer,
    reset_worm_buffer,
)
from querygate.metrics import REGISTRY

pytestmark = pytest.mark.unit


def _sample(name: str, labels: dict) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _event(connection_id: str = "demo") -> AuditEvent:
    return AuditEvent(
        connection_id=connection_id,
        policy_decision="allowed",
        outcome="success",
        query_shape={"from": "customers"},
        duration_ms=3,
    )


@pytest.fixture(autouse=True)
def _reset_buffer():
    reset_worm_buffer()
    yield
    reset_worm_buffer()


class TestInProcessWormEventBuffer:
    def test_enqueue_and_drain_round_trip_in_order(self):
        buffer = InProcessWormEventBuffer(max_size=10)
        events = [_event(f"conn-{i}") for i in range(3)]
        for e in events:
            buffer.enqueue(e)
        assert buffer.size() == 3
        assert buffer.drain() == events
        assert buffer.size() == 0

    def test_drain_is_idempotently_empty_after_draining(self):
        buffer = InProcessWormEventBuffer(max_size=10)
        buffer.enqueue(_event())
        buffer.drain()
        assert buffer.drain() == []

    def test_over_capacity_drops_the_oldest(self):
        buffer = InProcessWormEventBuffer(max_size=2)
        first, second, third = _event("a"), _event("b"), _event("c")
        buffer.enqueue(first)
        buffer.enqueue(second)
        buffer.enqueue(third)  # evicts `first`
        assert buffer.size() == 2
        assert buffer.drain() == [second, third]

    def test_over_capacity_increments_the_dropped_metric(self):
        before = _sample("querygate_audit_worm_buffer_dropped_total", {})
        buffer = InProcessWormEventBuffer(max_size=1)
        buffer.enqueue(_event("a"))
        buffer.enqueue(_event("b"))
        after = _sample("querygate_audit_worm_buffer_dropped_total", {})
        assert after == before + 1

    def test_requeue_front_restores_oldest_first_order(self):
        buffer = InProcessWormEventBuffer(max_size=10)
        failed_batch = [_event("a"), _event("b")]
        arrived_during_outage = _event("c")
        buffer.enqueue(arrived_during_outage)
        buffer.requeue_front(failed_batch)
        assert buffer.drain() == [*failed_batch, arrived_during_outage]

    def test_requeue_front_over_capacity_drops_and_meters_the_overflow(self):
        buffer = InProcessWormEventBuffer(max_size=1)
        before = _sample("querygate_audit_worm_buffer_dropped_total", {})
        buffer.requeue_front([_event("a"), _event("b")])
        after = _sample("querygate_audit_worm_buffer_dropped_total", {})
        assert buffer.size() == 1
        assert after > before


class TestS3WormAuditSink:
    def test_emit_only_buffers_never_touches_the_network(self):
        """The whole point of the buffer/monitor split: emit() must be safe
        to call from the request path with zero I/O."""
        sink = S3WormAuditSink()
        sink.emit(_event())
        assert get_worm_buffer().size() == 1

    def test_close_does_not_drain_the_buffer(self):
        """Flush lifecycle belongs to WormFlushMonitor, not the sink."""
        sink = S3WormAuditSink()
        sink.emit(_event())
        sink.close()
        assert get_worm_buffer().size() == 1


class TestWormFlushMonitorConstruction:
    def test_empty_bucket_is_rejected(self):
        with pytest.raises(ValueError, match="AUDIT_WORM_S3_BUCKET"):
            WormFlushMonitor(
                bucket="",
                prefix="audit/",
                region="us-east-1",
                retention_mode="COMPLIANCE",
                retention_days=180,
                interval_seconds=60,
            )


class TestWormFlushMonitorFlush:
    @pytest.fixture(autouse=True)
    def _mock_s3(self):
        # A fixture, not a class/method decorator: moto's decorator form
        # doesn't compose cleanly with pytest-asyncio's coroutine detection
        # on async test methods (confirmed — decorating the class made every
        # async test below invisible to pytest-asyncio). yield-style fixture
        # enters/exits the mock around each test instead.
        with mock_aws():
            yield

    def _bucket(self, name: str = "querygate-worm-test") -> str:
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=name, ObjectLockEnabledForBucket=True)
        return name

    def _monitor(self, bucket: str, **overrides) -> WormFlushMonitor:
        kwargs = dict(
            bucket=bucket,
            prefix="querygate-audit/",
            region="us-east-1",
            retention_mode="COMPLIANCE",
            retention_days=180,
            interval_seconds=60,
        )
        kwargs.update(overrides)
        return WormFlushMonitor(**kwargs)

    async def test_flush_of_an_empty_buffer_makes_no_s3_call(self):
        bucket = self._bucket()
        monitor = self._monitor(bucket)
        await monitor.flush_once()
        client = boto3.client("s3", region_name="us-east-1")
        assert "Contents" not in client.list_objects_v2(Bucket=bucket)

    async def test_flush_writes_one_object_lock_protected_segment(self):
        bucket = self._bucket()
        buffer = InProcessWormEventBuffer(max_size=100)
        monitor = self._monitor(bucket, buffer=buffer)
        buffer.enqueue(_event("a"))
        buffer.enqueue(_event("b"))

        await monitor.flush_once()

        client = boto3.client("s3", region_name="us-east-1")
        listing = client.list_objects_v2(Bucket=bucket)
        assert listing["KeyCount"] == 1
        key = listing["Contents"][0]["Key"]
        obj = client.get_object(Bucket=bucket, Key=key)
        assert obj["ObjectLockMode"] == "COMPLIANCE"
        assert obj["ObjectLockRetainUntilDate"] is not None
        lines = obj["Body"].read().decode("utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["event"]["connection_id"] == "a"
        assert json.loads(lines[1])["event"]["connection_id"] == "b"

    async def test_flushed_segment_is_a_valid_per_segment_hash_chain(self):
        # TODO.md item 154: each segment is its own self-contained chain,
        # starting fresh at GENESIS_PREV_HASH/seq=0 — not continuing from any
        # earlier segment's chain (the module docstring's "per-segment, not
        # cross-segment" decision).
        from querygate.audit.ledger import verify_chain

        bucket = self._bucket()
        buffer = InProcessWormEventBuffer(max_size=100)
        monitor = self._monitor(bucket, buffer=buffer)
        buffer.enqueue(_event("a"))
        buffer.enqueue(_event("b"))
        buffer.enqueue(_event("c"))

        await monitor.flush_once()

        client = boto3.client("s3", region_name="us-east-1")
        key = client.list_objects_v2(Bucket=bucket)["Contents"][0]["Key"]
        lines = client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
        result = verify_chain(lines.splitlines())
        assert result.ok
        assert result.records_checked == 3

    async def test_two_flushes_each_start_their_own_chain_at_genesis(self):
        # The "per-segment, not cross-segment" decision, proven directly:
        # the second segment's first record must NOT link to the first
        # segment's last hash — it starts over at GENESIS_PREV_HASH/seq=0.
        from querygate.audit.ledger import GENESIS_PREV_HASH

        bucket = self._bucket()
        buffer = InProcessWormEventBuffer(max_size=100)
        monitor = self._monitor(bucket, buffer=buffer)
        buffer.enqueue(_event("a"))
        await monitor.flush_once()
        buffer.enqueue(_event("b"))
        await monitor.flush_once()

        client = boto3.client("s3", region_name="us-east-1")
        keys = sorted(o["Key"] for o in client.list_objects_v2(Bucket=bucket)["Contents"])
        assert len(keys) == 2
        for key in keys:
            line = client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
            record = json.loads(line.strip().splitlines()[0])
            assert record["seq"] == 0
            assert record["prev_hash"] == GENESIS_PREV_HASH

    async def test_flushed_event_body_is_byte_identical_to_the_local_chained_sink(self, tmp_path):
        """Redaction-safety (non-negotiable #3) must hold identically here:
        WORM never constructs its own event body, so the `event` embedded in
        each chain record is exactly what the local HashChainedAuditSink
        would embed for the identical event — never more, never less. (The
        WORM segment and the local chained ledger are no longer byte-
        identical as whole files since item 154 — each has its own
        independent per-segment/per-file chain state — but the embedded
        event body itself must still match exactly, the same invariant
        `test_chained_envelope_adds_no_new_event_data` pins locally.)"""
        bucket = self._bucket()
        buffer = InProcessWormEventBuffer(max_size=10)
        monitor = self._monitor(bucket, buffer=buffer)
        event = _event("redaction-check")
        buffer.enqueue(event)

        local_path = tmp_path / "local.jsonl"
        HashChainedAuditSink(str(local_path)).emit(event)

        await monitor.flush_once()

        client = boto3.client("s3", region_name="us-east-1")
        key = client.list_objects_v2(Bucket=bucket)["Contents"][0]["Key"]
        archived_record = json.loads(
            client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8").strip()
        )
        local_record = json.loads(local_path.read_text(encoding="utf-8").strip())
        assert archived_record["event"] == local_record["event"]
        assert archived_record["event"] == event.model_dump(mode="json", exclude_none=True)

    async def test_multiple_flushes_write_distinct_non_colliding_keys(self):
        bucket = self._bucket()
        buffer = InProcessWormEventBuffer(max_size=100)
        monitor = self._monitor(bucket, buffer=buffer)
        buffer.enqueue(_event("a"))
        await monitor.flush_once()
        buffer.enqueue(_event("b"))
        await monitor.flush_once()

        client = boto3.client("s3", region_name="us-east-1")
        listing = client.list_objects_v2(Bucket=bucket)
        assert listing["KeyCount"] == 2

    async def test_flush_increments_the_flushes_and_archived_counters(self):
        bucket = self._bucket()
        buffer = InProcessWormEventBuffer(max_size=100)
        monitor = self._monitor(bucket, buffer=buffer)
        buffer.enqueue(_event("a"))
        buffer.enqueue(_event("b"))

        before_flushes = _sample("querygate_audit_worm_flushes_total", {})
        before_archived = _sample("querygate_audit_worm_events_archived_total", {})
        await monitor.flush_once()
        assert _sample("querygate_audit_worm_flushes_total", {}) == before_flushes + 1
        assert _sample("querygate_audit_worm_events_archived_total", {}) == before_archived + 2

    async def test_flush_failure_fails_open_and_requeues_the_batch(self):
        """The Decision Log's fail-open rule, proven directly: a PUT failure
        (here, a bucket that genuinely doesn't exist) must not raise out of
        flush_once(), and the drained batch must not be lost."""
        buffer = InProcessWormEventBuffer(max_size=100)
        monitor = self._monitor("this-bucket-does-not-exist", buffer=buffer)
        buffer.enqueue(_event("a"))

        before = _sample("querygate_audit_worm_flush_failures_total", {})
        await monitor.flush_once()  # must not raise
        after = _sample("querygate_audit_worm_flush_failures_total", {})

        assert after == before + 1
        assert buffer.size() == 1  # re-queued, not lost

    async def test_a_flush_error_outside_the_put_does_not_kill_the_loop(self, monkeypatch):
        """flush_once()'s own try/except only covers the S3 PUT — draining the
        buffer and building the segment key happen outside it. _run()'s loop
        must survive an exception from there too (fail-open, not fail-stopped),
        or one bad flush permanently stops WORM archival for the process even
        though every later event still enters the buffer fine."""
        bucket = self._bucket()
        monitor = self._monitor(bucket, interval_seconds=0.01)

        calls = {"n": 0}
        real_drain = monitor._buffer.drain

        def _drain_raises_once():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return real_drain()

        monkeypatch.setattr(monitor._buffer, "drain", _drain_raises_once)

        await monitor.start()
        try:
            monitor._buffer.enqueue(_event("a"))  # triggers the first (raising) flush
            for _ in range(200):
                if calls["n"] >= 1 and not monitor._task.done():
                    break
                await asyncio.sleep(0.01)
            assert not monitor._task.done()  # the loop survived the RuntimeError

            monitor._buffer.enqueue(_event("b"))  # a later flush must still work
            for _ in range(200):
                client = boto3.client("s3", region_name="us-east-1")
                if client.list_objects_v2(Bucket=bucket).get("KeyCount"):
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("a later flush never archived the re-enqueued event")
        finally:
            await monitor.stop()


class TestCompositeAuditSink:
    def test_emits_to_every_sub_sink(self, tmp_path):
        path = tmp_path / "chain.jsonl"
        chain = HashChainedAuditSink(str(path))
        worm_buffer = InProcessWormEventBuffer(max_size=10)
        worm = S3WormAuditSink(worm_buffer)
        composite = CompositeAuditSink([chain, worm])

        composite.emit(_event())

        assert path.read_text().strip() != ""
        assert worm_buffer.size() == 1

    def test_one_sub_sink_failing_does_not_suppress_the_other(self, tmp_path):
        path = tmp_path / "chain.jsonl"
        chain = HashChainedAuditSink(str(path))

        class _AlwaysFails:
            def emit(self, event):
                raise RuntimeError("boom")

            def close(self):
                pass

        composite = CompositeAuditSink([_AlwaysFails(), chain])
        with pytest.raises(ExceptionGroup):
            composite.emit(_event())
        # The chain sink still ran despite the earlier sink's failure.
        assert path.read_text().strip() != ""

    def test_close_closes_every_sub_sink(self):
        closed = []

        class _Tracks:
            def emit(self, event):
                pass

            def close(self):
                closed.append(self)

        a, b = _Tracks(), _Tracks()
        CompositeAuditSink([a, b]).close()
        assert closed == [a, b]


class TestConfigureAuditSinkRegistry:
    def test_unsupported_backend_raises(self):
        with pytest.raises(ValueError, match="Unsupported audit sink backend"):
            configure_audit_sink(backend="not-a-real-backend", jsonl_path="x")
        reset_audit_sink()

    def test_none_backend_still_works_through_the_registry(self):
        configure_audit_sink(backend="none", jsonl_path="")
        assert isinstance(get_audit_sink(), NullAuditSink)
        reset_audit_sink()

    def test_jsonl_chained_s3_worm_backend_builds_a_composite_sink(self, tmp_path):
        configure_audit_sink(
            backend="jsonl_chained_s3_worm",
            jsonl_path=str(tmp_path / "chain.jsonl"),
        )
        try:
            sink = get_audit_sink()
            assert isinstance(sink, CompositeAuditSink)
            assert len(sink._sinks) == 2
            assert isinstance(sink._sinks[0], HashChainedAuditSink)
            assert isinstance(sink._sinks[1], S3WormAuditSink)
        finally:
            reset_audit_sink()
