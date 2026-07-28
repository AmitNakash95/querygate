"""Unit tests for querygate.metrics — rejection classification and rendering."""

from __future__ import annotations

from querygate.core.exceptions import (
    ConcurrencyLimitError,
    CostEstimateExceededError,
    PolicyViolationError,
    QueueFullError,
)
from querygate.metrics import classify_rejection, render_latest


def test_classify_concurrency_limit_error():
    assert classify_rejection(ConcurrencyLimitError("too many")) == "concurrency"


def test_classify_policy_violation_error():
    assert classify_rejection(PolicyViolationError("denied")) == "policy"


def test_classify_cost_estimate_exceeded_error_as_its_own_reason():
    """CostEstimateExceededError subclasses PolicyViolationError but must
    report its own `cost_estimate` reason, not fall into the coarser
    `policy` bucket — see TODO.md item 26.
    """
    assert classify_rejection(CostEstimateExceededError("too expensive")) == "cost_estimate"


def test_classify_queue_full_error_as_its_own_reason():
    """QueueFullError subclasses CapacityTimeoutError (itself a
    ConcurrencyLimitError) but must report its own `queue_full` reason, not
    fall into the coarser `concurrency` bucket — see TODO.md item 35 phase 2.
    """
    exc = QueueFullError("queue is full", admission_id="admission-1", retry_after_seconds=10)
    assert classify_rejection(exc) == "queue_full"


def test_classify_plain_value_error_as_schema():
    assert classify_rejection(ValueError("bad column ref")) == "schema"


def test_classify_other_exception_as_db_error():
    assert classify_rejection(RuntimeError("connection reset")) == "db_error"


def test_render_latest_produces_prometheus_text_format():
    output = render_latest().decode("utf-8")
    assert "querygate_queries_total" in output
    assert "querygate_concurrency_in_use" in output
