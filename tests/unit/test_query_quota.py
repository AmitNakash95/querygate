"""Per-principal request/byte quota over a rolling window (TODO.md item 50).

Covers the in-process limiter's window semantics, the policy resolution/enforce
helpers, the distinct exception + metrics classification, and the REST 429 /
MCP RATE_LIMITED error mapping. The execute()-path enforcement is exercised
end-to-end in tests/integration/test_query_quota_e2e.py.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette import status

from querygate.api._errors import install_exception_handlers
from querygate.core.exceptions import PolicyViolationError, QuotaExceededError
from querygate.execution.quota import (
    InProcessQuotaLimiter,
    QuotaReservation,
    enforce_query_quota,
    record_query_quota_bytes,
    resolve_query_quota,
)
from querygate.mcp.exceptions import _error_code_from_exception
from querygate.metrics import classify_rejection
from querygate.policy.models import Policy

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Policy model
# ---------------------------------------------------------------------------


def test_quota_disabled_by_default():
    policy = Policy()
    assert policy.query_quota_enabled is False
    assert resolve_query_quota(policy) is None


def test_quota_enabled_by_either_cap():
    assert Policy(max_requests_per_window=5).query_quota_enabled is True
    assert Policy(max_response_bytes_per_window=1000).query_quota_enabled is True


def test_quota_window_and_caps_have_positive_lower_bounds():
    for kwargs in (
        {"max_requests_per_window": 0},
        {"max_response_bytes_per_window": 0},
        {"quota_window_seconds": 0},
    ):
        with pytest.raises(ValueError):
            Policy(**kwargs)


def test_resolve_query_quota_returns_tuple():
    policy = Policy(
        max_requests_per_window=10,
        max_response_bytes_per_window=2048,
        quota_window_seconds=120,
    )
    assert resolve_query_quota(policy) == (10, 2048, 120)


# ---------------------------------------------------------------------------
# InProcessQuotaLimiter — request-count window
# ---------------------------------------------------------------------------


async def _reserve(
    limiter, key=("demo", "agent"), *, max_requests=None, max_bytes=None, window=60, now
):
    return await limiter.reserve(
        key,
        max_requests=max_requests,
        max_response_bytes=max_bytes,
        window_seconds=window,
        now=now,
    )


@pytest.mark.asyncio
async def test_request_quota_admits_up_to_cap_then_rejects():
    limiter = InProcessQuotaLimiter()
    await _reserve(limiter, max_requests=2, window=30, now=100.0)
    await _reserve(limiter, max_requests=2, window=30, now=100.0)
    with pytest.raises(QuotaExceededError) as excinfo:
        await _reserve(limiter, max_requests=2, window=30, now=100.0)
    assert excinfo.value.quota_kind == "requests"
    assert excinfo.value.retry_after_seconds == 30


@pytest.mark.asyncio
async def test_request_quota_window_rolls_forward():
    limiter = InProcessQuotaLimiter()
    await _reserve(limiter, max_requests=1, window=30, now=100.0)
    with pytest.raises(QuotaExceededError):
        await _reserve(limiter, max_requests=1, window=30, now=110.0)
    # Once the first attempt ages past the window, capacity frees up again.
    await _reserve(limiter, max_requests=1, window=30, now=131.0)


@pytest.mark.asyncio
async def test_retry_after_reflects_oldest_entry_age():
    limiter = InProcessQuotaLimiter()
    await _reserve(limiter, max_requests=1, window=60, now=100.0)
    with pytest.raises(QuotaExceededError) as excinfo:
        await _reserve(limiter, max_requests=1, window=60, now=140.0)
    # oldest entry at t=100, window 60 -> frees at t=160, now=140 -> ~20s.
    assert excinfo.value.retry_after_seconds == 20


@pytest.mark.asyncio
async def test_principals_and_connections_have_independent_windows():
    limiter = InProcessQuotaLimiter()
    await _reserve(limiter, ("demo", "a"), max_requests=1, window=30, now=100.0)
    # Same connection, different principal: independent budget.
    await _reserve(limiter, ("demo", "b"), max_requests=1, window=30, now=100.0)
    # Same principal, different connection: independent budget.
    await _reserve(limiter, ("other", "a"), max_requests=1, window=30, now=100.0)
    with pytest.raises(QuotaExceededError):
        await _reserve(limiter, ("demo", "a"), max_requests=1, window=30, now=100.0)


# ---------------------------------------------------------------------------
# InProcessQuotaLimiter — response-byte window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_byte_quota_rejects_once_window_total_reaches_cap():
    limiter = InProcessQuotaLimiter()
    r1 = await _reserve(limiter, max_bytes=1000, window=30, now=100.0)
    await limiter.record_bytes(r1, 600)
    r2 = await _reserve(limiter, max_bytes=1000, window=30, now=100.0)  # total 600 < 1000, admitted
    await limiter.record_bytes(r2, 600)  # window now holds 1200 bytes
    with pytest.raises(QuotaExceededError) as excinfo:
        await _reserve(limiter, max_bytes=1000, window=30, now=100.0)
    assert excinfo.value.quota_kind == "bytes"


@pytest.mark.asyncio
async def test_byte_quota_window_rolls_forward():
    limiter = InProcessQuotaLimiter()
    r1 = await _reserve(limiter, max_bytes=100, window=30, now=100.0)
    await limiter.record_bytes(r1, 500)
    with pytest.raises(QuotaExceededError):
        await _reserve(limiter, max_bytes=100, window=30, now=110.0)
    # Old byte weight ages out with its entry.
    await _reserve(limiter, max_bytes=100, window=30, now=131.0)


@pytest.mark.asyncio
async def test_request_cap_checked_before_byte_cap():
    limiter = InProcessQuotaLimiter()
    r1 = await _reserve(limiter, max_requests=1, max_bytes=100, window=30, now=100.0)
    await limiter.record_bytes(r1, 999)
    # Both caps are now exceeded; the request cap wins the classification.
    with pytest.raises(QuotaExceededError) as excinfo:
        await _reserve(limiter, max_requests=1, max_bytes=100, window=30, now=100.0)
    assert excinfo.value.quota_kind == "requests"


@pytest.mark.asyncio
async def test_record_bytes_on_aged_out_reservation_is_harmless():
    limiter = InProcessQuotaLimiter()
    r1 = await _reserve(limiter, max_bytes=1000, window=30, now=100.0)
    # Advance past the window so r1's entry is pruned on the next reserve.
    await _reserve(limiter, max_bytes=1000, window=30, now=200.0)
    await limiter.record_bytes(r1, 5000)  # must not raise or resurrect the total
    await _reserve(limiter, max_bytes=1000, window=30, now=200.0)  # still admitted


# ---------------------------------------------------------------------------
# enforce_query_quota
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enforce_returns_none_when_disabled():
    assert (
        await enforce_query_quota(Policy(), connection_id="demo", principal_subject="agent") is None
    )


@pytest.mark.asyncio
async def test_enforce_skips_unauthenticated_caller():
    policy = Policy(max_requests_per_window=1)
    # No principal to attribute usage to -> quota is skipped, not applied to a
    # shared anonymous bucket. Repeated calls never reject.
    assert await enforce_query_quota(policy, connection_id="demo", principal_subject=None) is None
    assert await enforce_query_quota(policy, connection_id="demo", principal_subject=None) is None


@pytest.mark.asyncio
async def test_enforce_reserves_and_rejects_over_cap(monkeypatch):
    policy = Policy(max_requests_per_window=1, quota_window_seconds=60)
    first = await enforce_query_quota(policy, connection_id="demo", principal_subject="agent")
    assert isinstance(first, QuotaReservation)
    with pytest.raises(QuotaExceededError):
        await enforce_query_quota(policy, connection_id="demo", principal_subject="agent")


# ---------------------------------------------------------------------------
# Classification and transport error mapping
# ---------------------------------------------------------------------------


def test_quota_error_is_a_policy_violation():
    exc = QuotaExceededError("nope", quota_kind="requests", retry_after_seconds=5)
    assert isinstance(exc, PolicyViolationError)


def test_classify_rejection_reports_quota():
    exc = QuotaExceededError("nope", quota_kind="bytes", retry_after_seconds=5)
    assert classify_rejection(exc) == "quota"


def test_mcp_maps_quota_to_rate_limited():
    exc = QuotaExceededError("slow down", quota_kind="requests", retry_after_seconds=7)
    code, message = _error_code_from_exception(exc)
    assert code == "RATE_LIMITED"
    assert message == "slow down"


def test_rest_maps_quota_to_429_with_retry_after():
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/boom")
    async def _boom():
        raise QuotaExceededError("slow down", quota_kind="requests", retry_after_seconds=42)

    client = TestClient(app)
    resp = client.get("/boom")
    assert resp.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    assert resp.headers["Retry-After"] == "42"
    assert resp.json() == {"detail": "slow down"}
