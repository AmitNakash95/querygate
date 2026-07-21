"""Unit tests for the admin observability aggregator (TODO.md item 44, phase 1).

`build_overview` is pure over the registry it's given, so these tests drive a
fresh `CollectorRegistry` with the same metric names/labels as
`querygate/metrics.py` and assert the aggregation — no app, no global state.
"""

from __future__ import annotations

import pytest
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from querygate.admin.observability import build_overview

pytestmark = pytest.mark.unit


def _fresh_registry() -> dict:
    reg = CollectorRegistry()
    m = {
        "queries": Counter("querygate_queries_total", "", ["connection", "status"], registry=reg),
        "rejected": Counter(
            "querygate_queries_rejected_total", "", ["connection", "reason"], registry=reg
        ),
        "duration": Histogram("querygate_query_duration_seconds", "", ["connection"], registry=reg),
        "in_use": Gauge("querygate_concurrency_in_use", "", ["connection"], registry=reg),
        "max": Gauge("querygate_concurrency_max", "", ["connection"], registry=reg),
        "queue_depth": Gauge("querygate_queue_depth", "", ["connection"], registry=reg),
        "queue_wait": Histogram(
            "querygate_queue_wait_seconds", "", ["connection", "outcome"], registry=reg
        ),
        "quota": Counter(
            "querygate_query_quota_rejections_total",
            "",
            ["connection", "quota_kind"],
            registry=reg,
        ),
        "cost_attempts": Counter(
            "querygate_cost_estimation_attempts_total", "", ["connection"], registry=reg
        ),
        "cost_unavailable": Counter(
            "querygate_cost_estimation_unavailable_total",
            "",
            ["connection", "reason"],
            registry=reg,
        ),
        "cost_would_reject": Counter(
            "querygate_cost_estimation_would_reject_total", "", ["connection"], registry=reg
        ),
    }
    m["_registry"] = reg
    return m


def test_empty_registry_is_all_zeros_but_honestly_labeled():
    ov = build_overview(_fresh_registry()["_registry"])
    assert ov.queries_total == 0
    assert ov.queries_success == 0
    assert ov.queries_rejected == 0
    assert ov.rejections_by_reason == {}
    assert ov.by_connection == []
    assert ov.duration.avg_seconds is None
    assert ov.concurrency_utilization is None
    assert ov.cost_estimation.fail_open_rate is None
    # The snapshot never pretends to be durable history.
    assert ov.source == "process_snapshot"
    assert ov.durable is False
    assert ov.since  # an ISO timestamp string
    assert "not durable history" in ov.note


def test_volume_and_rejection_categories_roll_up_and_break_down():
    m = _fresh_registry()
    m["queries"].labels("demo", "success").inc(7)
    m["queries"].labels("demo", "rejected").inc(3)
    m["queries"].labels("reporting", "success").inc(5)
    m["rejected"].labels("demo", "policy").inc(2)
    m["rejected"].labels("demo", "schema").inc(1)

    ov = build_overview(m["_registry"])

    assert ov.queries_total == 15
    assert ov.queries_success == 12
    assert ov.queries_rejected == 3
    assert ov.rejections_by_reason == {"policy": 2, "schema": 1}

    by_conn = {c.connection: c for c in ov.by_connection}
    assert set(by_conn) == {"demo", "reporting"}
    assert by_conn["demo"].queries_total == 10
    assert by_conn["demo"].queries_rejected == 3
    assert by_conn["demo"].rejections_by_reason == {"policy": 2, "schema": 1}
    assert by_conn["reporting"].queries_total == 5
    assert by_conn["reporting"].rejections_by_reason == {}
    # by_connection is sorted by connection id.
    assert [c.connection for c in ov.by_connection] == ["demo", "reporting"]


def test_duration_average_and_concurrency_utilization():
    m = _fresh_registry()
    m["duration"].labels("demo").observe(0.4)
    m["duration"].labels("demo").observe(0.6)
    m["in_use"].labels("demo").set(2)
    m["max"].labels("demo").set(8)

    ov = build_overview(m["_registry"])
    assert ov.duration.count == 2
    assert ov.duration.avg_seconds == pytest.approx(0.5)
    assert ov.concurrency_in_use_total == 2
    assert ov.concurrency_max_total == 8
    assert ov.concurrency_utilization == pytest.approx(0.25)
    assert ov.by_connection[0].concurrency_utilization == pytest.approx(0.25)


def test_concurrency_utilization_none_when_max_zero():
    m = _fresh_registry()
    m["in_use"].labels("demo").set(0)
    m["max"].labels("demo").set(0)
    ov = build_overview(m["_registry"])
    assert ov.concurrency_utilization is None


def test_queue_wait_split_by_outcome():
    m = _fresh_registry()
    m["queue_wait"].labels("demo", "completed").observe(0.1)
    m["queue_wait"].labels("demo", "completed").observe(0.3)
    m["queue_wait"].labels("demo", "capacity_timeout").observe(2.0)
    m["queue_depth"].labels("demo").set(4)

    ov = build_overview(m["_registry"])
    assert ov.queue_depth_total == 4
    assert ov.queue_wait_by_outcome["completed"].count == 2
    assert ov.queue_wait_by_outcome["completed"].avg_seconds == pytest.approx(0.2)
    assert ov.queue_wait_by_outcome["capacity_timeout"].count == 1
    assert ov.queue_wait_by_outcome["capacity_timeout"].avg_seconds == pytest.approx(2.0)


def test_cost_estimation_fail_open_rate():
    m = _fresh_registry()
    m["cost_attempts"].labels("demo").inc(10)
    m["cost_unavailable"].labels("demo", "explain_failed").inc(2)
    m["cost_unavailable"].labels("demo", "compile_failed").inc(1)
    m["cost_would_reject"].labels("demo").inc(4)

    ov = build_overview(m["_registry"])
    assert ov.cost_estimation.attempts == 10
    assert ov.cost_estimation.unavailable == 3
    assert ov.cost_estimation.unavailable_by_reason == {
        "explain_failed": 2,
        "compile_failed": 1,
    }
    assert ov.cost_estimation.would_reject == 4
    assert ov.cost_estimation.fail_open_rate == pytest.approx(0.3)


def test_quota_rejections_by_kind():
    m = _fresh_registry()
    m["quota"].labels("demo", "requests").inc(3)
    m["quota"].labels("demo", "bytes").inc(1)
    ov = build_overview(m["_registry"])
    assert ov.quota_rejections_by_kind == {"requests": 3, "bytes": 1}
    assert ov.by_connection[0].quota_rejections_by_kind == {"requests": 3, "bytes": 1}
