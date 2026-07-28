"""REST/MCP mapping for capacity/queue rejections (TODO.md item 35 phase 3,
2026-07-28): migrated from REST 422 / MCP VALIDATION to REST 429+Retry-After /
MCP RATE_LIMITED, matching item 50's quota-rejection precedent
(`tests/unit/test_query_quota.py`'s analogous tests for `QuotaExceededError`).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from querygate.api._errors import install_exception_handlers
from querygate.core.exceptions import CapacityTimeoutError, ConcurrencyLimitError, QueueFullError
from querygate.mcp.exceptions import _error_code_from_exception


def test_mcp_maps_bare_concurrency_limit_error_to_rate_limited():
    exc = ConcurrencyLimitError("too many concurrent 'demo' queries in flight")
    code, message = _error_code_from_exception(exc)
    assert code == "RATE_LIMITED"
    assert message == "too many concurrent 'demo' queries in flight"


def test_mcp_maps_capacity_timeout_to_rate_limited_with_retry_after():
    from querygate.mcp.exceptions import _admission_fields_from_exception

    exc = CapacityTimeoutError(
        "too many concurrent 'demo' queries in flight",
        admission_id="admission-1",
        queue_wait_ms=100,
        retry_after_seconds=10,
    )
    code, _ = _error_code_from_exception(exc)
    assert code == "RATE_LIMITED"
    fields = _admission_fields_from_exception(exc)
    assert fields["retry_after_seconds"] == 10
    assert fields["admission_id"] == "admission-1"


def test_rest_maps_bare_concurrency_limit_error_to_429():
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/boom")
    async def boom():
        raise ConcurrencyLimitError("too many concurrent 'demo' queries in flight")

    resp = TestClient(app).get("/boom")
    assert resp.status_code == 429


def test_rest_maps_capacity_timeout_to_429_with_retry_after():
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/boom")
    async def boom():
        raise CapacityTimeoutError(
            "too many concurrent 'demo' queries in flight",
            admission_id="admission-1",
            queue_wait_ms=100,
            retry_after_seconds=10,
        )

    resp = TestClient(app).get("/boom")
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "10"
    assert resp.headers["X-QueryGate-Admission-Id"] == "admission-1"


def test_rest_maps_queue_full_to_429_with_retry_after():
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/boom")
    async def boom():
        raise QueueFullError(
            "connection 'demo' queue is already at its configured depth",
            admission_id="admission-2",
            retry_after_seconds=15,
        )

    resp = TestClient(app).get("/boom")
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "15"
    assert resp.headers["X-QueryGate-Admission-State"] == "queue_full"
