"""Integration tests for the admin observability overview API (item 44, phase 1):
GET /api/v1/admin/observability/overview — scope enforcement, honest snapshot
labeling, and that it reflects real metric activity without leaking values."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from querygate import metrics
from querygate.api.app import create_app
from querygate.core.config import AppConfig

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"
_ADMIN_KEY = "obs-admin-key"
_OBS_SCOPE = "admin:observability:read"
# A connection id unique to this module so assertions on its row are isolated
# from metric samples any other test in the same process may have produced.
_CONN = "obs_test_conn"


def _settings(scopes) -> AppConfig:
    return AppConfig(
        environment="localhost",
        mcp_enabled=False,
        audit_sink_backend="none",
        api_keys=[_ADMIN_KEY],
        api_key_scopes=list(scopes),
    )


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


async def _get_overview(app, key=_ADMIN_KEY):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        return await client.get("/api/v1/admin/observability/overview", headers=_auth(key))


@pytest.mark.asyncio
async def test_overview_requires_the_observability_scope():
    app = create_app(_settings(("admin:connections:read",)))  # wrong scope
    resp = await _get_overview(app)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_overview_is_returned_with_the_scope_and_labeled_non_durable():
    app = create_app(_settings((_OBS_SCOPE,)))
    resp = await _get_overview(app)
    assert resp.status_code == 200
    body = resp.json()
    # Honest snapshot: never implies durable history.
    assert body["source"] == "process_snapshot"
    assert body["durable"] is False
    assert body["since"]
    assert "not durable history" in body["note"]


@pytest.mark.asyncio
async def test_overview_reflects_real_metric_activity():
    app = create_app(_settings((_OBS_SCOPE,)))

    before = (await _get_overview(app)).json()
    before_by = {c["connection"]: c for c in before["by_connection"]}
    base_total = before_by.get(_CONN, {}).get("queries_total", 0)
    base_policy = before_by.get(_CONN, {}).get("rejections_by_reason", {}).get("policy", 0)

    # Drive the live registry the endpoint reads.
    metrics.QUERIES_TOTAL.labels(connection=_CONN, status="success").inc(2)
    metrics.QUERIES_TOTAL.labels(connection=_CONN, status="rejected").inc(1)
    metrics.QUERIES_REJECTED_TOTAL.labels(connection=_CONN, reason="policy").inc(1)

    after = (await _get_overview(app)).json()
    after_by = {c["connection"]: c for c in after["by_connection"]}

    assert _CONN in after_by
    assert after_by[_CONN]["queries_total"] == base_total + 3
    assert after_by[_CONN]["queries_success"] >= 2
    assert after_by[_CONN]["rejections_by_reason"]["policy"] == base_policy + 1
    # Global rollup moved too.
    assert after["queries_total"] >= before["queries_total"] + 3


@pytest.mark.asyncio
async def test_overview_carries_only_low_cardinality_aggregates_no_values():
    app = create_app(_settings((_OBS_SCOPE,)))
    metrics.QUERIES_TOTAL.labels(connection=_CONN, status="success").inc(1)
    resp = await _get_overview(app)
    body = resp.json()

    # Top-level shape is a fixed aggregate schema — no free-form value fields.
    allowed_top = {
        "source",
        "durable",
        "since",
        "note",
        "queries_total",
        "queries_success",
        "queries_rejected",
        "rejections_by_reason",
        "duration",
        "queue_depth_total",
        "queue_wait_by_outcome",
        "concurrency_in_use_total",
        "concurrency_max_total",
        "concurrency_utilization",
        "quota_rejections_by_kind",
        "cost_estimation",
        "by_connection",
    }
    assert set(body) == allowed_top
    # Every connection row is keyed by a plain connection id (already public),
    # and rejection reasons are the fixed low-cardinality buckets from metrics.py.
    for conn in body["by_connection"]:
        assert isinstance(conn["connection"], str)
        for reason in conn["rejections_by_reason"]:
            assert reason in {
                "policy",
                "schema",
                "concurrency",
                "queue_full",
                "cost_estimate",
                "quota",
                "db_error",
            }
