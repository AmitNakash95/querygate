"""Integration tests for TODO.md item 134 phase 2's REST surface:
GET /api/v1/admin/observability/worm-search — scope enforcement (its own
dedicated scope, distinct from admin:observability:read), honest "disabled"
when the backend isn't jsonl_chained_s3_worm, a real search against a
moto-mocked S3 archive, and rejection of out-of-bound requests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import boto3
import pytest
from httpx import ASGITransport, AsyncClient
from moto import mock_aws

from querygate.api.app import create_app
from querygate.audit.events import AuditEvent
from querygate.core.config import AppConfig

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"
_ADMIN_KEY = "worm-search-admin-key"
_WORM_SEARCH_SCOPE = "admin:audit:worm-search"
_OBS_SCOPE = "admin:observability:read"
_BUCKET = "querygate-worm-search-integration-test"
_PREFIX = "querygate-audit/"
_URL = "/api/v1/admin/observability/worm-search"


def _settings(scopes, *, backend="none", **overrides) -> AppConfig:
    kwargs = dict(
        environment="localhost",
        mcp_enabled=False,
        audit_sink_backend=backend,
        audit_jsonl_path="var/audit/chain.jsonl",
        audit_worm_s3_bucket=_BUCKET,
        audit_worm_s3_prefix=_PREFIX,
        audit_worm_s3_region="us-east-1",
        api_keys=[_ADMIN_KEY],
        api_key_scopes=list(scopes),
    )
    kwargs.update(overrides)
    return AppConfig(**kwargs)


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


async def _get(app, params=None, key=_ADMIN_KEY):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        return await client.get(_URL, headers=_auth(key), params=params or {})


def _put_event(client, key: str, event: AuditEvent) -> None:
    body = (event.model_dump_json(exclude_none=True) + "\n").encode("utf-8")
    client.put_object(
        Bucket=_BUCKET,
        Key=key,
        Body=body,
        ObjectLockMode="COMPLIANCE",
        ObjectLockRetainUntilDate=datetime.now(timezone.utc) + timedelta(days=1),
    )


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=_BUCKET, ObjectLockEnabledForBucket=True)
        yield client


@pytest.mark.asyncio
async def test_requires_a_scope_at_all():
    app = create_app(_settings((), backend="jsonl_chained_s3_worm"))
    resp = await _get(
        app,
        {
            "start_time": "2026-03-15T00:00:00+00:00",
            "end_time": "2026-03-16T00:00:00+00:00",
        },
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_the_observability_read_scope_alone_is_not_sufficient():
    """The dedicated worm-search scope must NOT be implied by
    admin:observability:read — the whole point of giving it its own scope
    (core/scopes.py's comment on ADMIN_AUDIT_WORM_SEARCH_SCOPE)."""
    app = create_app(_settings((_OBS_SCOPE,), backend="jsonl_chained_s3_worm"))
    resp = await _get(
        app,
        {
            "start_time": "2026-03-15T00:00:00+00:00",
            "end_time": "2026-03-16T00:00:00+00:00",
        },
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_the_dedicated_scope_is_sufficient_on_its_own():
    app = create_app(_settings((_WORM_SEARCH_SCOPE,), backend="jsonl_chained_s3_worm"))
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=_BUCKET, ObjectLockEnabledForBucket=True)
        resp = await _get(
            app,
            {
                "start_time": "2026-03-15T00:00:00+00:00",
                "end_time": "2026-03-16T00:00:00+00:00",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["source"] == "s3_worm"


@pytest.mark.asyncio
async def test_disabled_without_the_worm_backend_configured():
    app = create_app(_settings((_WORM_SEARCH_SCOPE,), backend="jsonl_chained"))
    resp = await _get(
        app,
        {
            "start_time": "2026-03-15T00:00:00+00:00",
            "end_time": "2026-03-16T00:00:00+00:00",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "disabled"
    assert body["events"] == []


@pytest.mark.asyncio
async def test_disabled_backend_still_rejects_a_missing_time_range_with_422():
    """'start_time/end_time required on every request' must hold even when
    the backend is disabled — not relax into an unvalidated 200 just
    because there's nothing to search (claim-reviewer, 2026-08-06)."""
    app = create_app(_settings((_WORM_SEARCH_SCOPE,), backend="jsonl_chained"))
    resp = await _get(app, {})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_missing_time_range_is_rejected_with_422():
    app = create_app(_settings((_WORM_SEARCH_SCOPE,), backend="jsonl_chained_s3_worm"))
    resp = await _get(app, {})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_too_wide_a_window_is_rejected_with_422():
    app = create_app(_settings((_WORM_SEARCH_SCOPE,), backend="jsonl_chained_s3_worm"))
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=_BUCKET, ObjectLockEnabledForBucket=True)
        resp = await _get(
            app,
            {
                "start_time": "2000-01-01T00:00:00+00:00",
                "end_time": "2026-01-01T00:00:00+00:00",
            },
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_a_real_search_returns_matching_events_and_redacts_the_rest(s3):
    matching = AuditEvent(
        connection_id="pii_customers",
        policy_decision="allowed",
        outcome="success",
        query_shape={"from": "pii_customers"},
        duration_ms=3,
        occurred_at=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
    )
    other = AuditEvent(
        connection_id="other_db",
        policy_decision="allowed",
        outcome="success",
        query_shape={"from": "other_table"},
        duration_ms=3,
        occurred_at=datetime(2026, 3, 15, 12, 1, tzinfo=timezone.utc),
    )
    _put_event(s3, f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl", matching)
    _put_event(s3, f"{_PREFIX}2026/03/15/20260315T120100-000001.jsonl", other)

    app = create_app(_settings((_WORM_SEARCH_SCOPE,), backend="jsonl_chained_s3_worm"))
    resp = await _get(
        app,
        {
            "start_time": "2026-03-15T00:00:00+00:00",
            "end_time": "2026-03-16T00:00:00+00:00",
            "connection_id": "pii_customers",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["events"]) == 1
    assert body["events"][0]["connection_id"] == "pii_customers"
    assert "other_db" not in resp.text


@pytest.mark.asyncio
async def test_pagination_cursor_round_trips_through_the_route(s3):
    for i in range(2):
        event = AuditEvent(
            connection_id=f"conn-{i}",
            policy_decision="allowed",
            outcome="success",
            query_shape={},
            duration_ms=1,
            occurred_at=datetime(2026, 3, 15, 12, i, tzinfo=timezone.utc),
        )
        _put_event(s3, f"{_PREFIX}2026/03/15/20260315T12000{i}-000001.jsonl", event)

    app = create_app(_settings((_WORM_SEARCH_SCOPE,), backend="jsonl_chained_s3_worm"))
    resp1 = await _get(
        app,
        {
            "start_time": "2026-03-15T00:00:00+00:00",
            "end_time": "2026-03-16T00:00:00+00:00",
            "limit": 1,
        },
    )
    assert resp1.status_code == 200
    body1 = resp1.json()
    assert len(body1["events"]) == 1
    assert body1["truncated"] is True
    assert body1["next_cursor"]

    resp2 = await _get(
        app,
        {
            "start_time": "2026-03-15T00:00:00+00:00",
            "end_time": "2026-03-16T00:00:00+00:00",
            "limit": 1,
            "cursor": body1["next_cursor"],
        },
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert len(body2["events"]) == 1
    assert body1["events"][0]["connection_id"] != body2["events"][0]["connection_id"]
