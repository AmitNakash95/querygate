"""Integration tests for the audit-stream anomaly API (TODO.md item 59):
GET /api/v1/admin/observability/anomalies — scope enforcement, honest
"disabled" when no persisted sink, and a real spike surfaced from a JSONL file
without leaking query values."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from querygate.api.app import create_app
from querygate.audit.events import AuditEvent
from querygate.core.config import AppConfig

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"
_ADMIN_KEY = "anomaly-admin-key"
_OBS_SCOPE = "admin:observability:read"


def _settings(scopes, *, backend="none", jsonl_path="", **anomaly) -> AppConfig:
    return AppConfig(
        environment="localhost",
        mcp_enabled=False,
        audit_sink_backend=backend,
        audit_jsonl_path=jsonl_path or "var/audit/querygate-audit.jsonl",
        api_keys=[_ADMIN_KEY],
        api_key_scopes=list(scopes),
        **anomaly,
    )


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


async def _get(app, key=_ADMIN_KEY):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        return await client.get("/api/v1/admin/observability/anomalies", headers=_auth(key))


def _write_events(path, *, principal="svc-a", connection="demo"):
    now = datetime.now(timezone.utc)
    lines = []
    # 10 baseline events (window (now-7200, now-3600]) + 40 recent (now-3600, now].
    for i in range(10):
        lines.append(now - timedelta(seconds=3600 + 60 * (i + 1)))
    for i in range(40):
        lines.append(now - timedelta(seconds=60 * (i + 1)))
    with open(path, "w", encoding="utf-8") as handle:
        for at in lines:
            event = AuditEvent(
                occurred_at=at,
                principal_id=principal,
                connection_id=connection,
                policy_decision="allowed",
                outcome="success",
                query_shape={"from": "customers", "select": [{"kind": "column", "column": "ssn"}]},
                duration_ms=2,
            )
            handle.write(event.model_dump_json(exclude_none=True) + "\n")


@pytest.mark.asyncio
async def test_anomalies_requires_the_observability_scope():
    app = create_app(_settings(("admin:connections:read",)))
    resp = await _get(app)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_anomalies_disabled_without_a_persisted_sink():
    app = create_app(_settings((_OBS_SCOPE,), backend="none"))
    resp = await _get(app)
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "disabled"
    assert body["principals"] == []


@pytest.mark.asyncio
async def test_anomalies_surface_a_spike_from_the_jsonl_stream(tmp_path):
    path = tmp_path / "audit.jsonl"
    app = create_app(
        _settings(
            (_OBS_SCOPE,),
            backend="jsonl",
            jsonl_path=str(path),
            # Equal windows keep the spike arithmetic obvious.
            anomaly_recent_window_seconds=3600.0,
            anomaly_baseline_window_seconds=3600.0,
            anomaly_min_baseline_events=10,
            anomaly_min_recent_events=3,
        )
    )
    _write_events(path)

    resp = await _get(app)
    assert resp.status_code == 200
    body = resp.json()

    assert body["source"] == "jsonl"
    assert body["events_scanned"] == 50
    assert len(body["principals"]) == 1
    principal = body["principals"][0]
    assert principal["principal_id"] == "svc-a"
    assert any(s["kind"] == "volume_spike" for s in principal["signals"])

    # Redaction: the surfaced report never carries the query shape or values.
    blob = resp.text
    assert "ssn" not in blob
    assert "query_shape" not in blob
    assert "customers" not in blob


@pytest.mark.asyncio
async def test_anomalies_surface_a_spike_from_a_hash_chained_ledger(tmp_path):
    """TODO.md item 136: AUDIT_SINK_BACKEND=jsonl_chained used to be refused at
    this route's gate entirely (source="disabled"), even though the underlying
    reader already unwrapped the chain envelope — this is the full HTTP-level
    regression, not just the unit-level `_anomaly_source` helper test."""
    from querygate.audit.ledger import GENESIS_PREV_HASH, make_record

    path = tmp_path / "audit.jsonl"
    now = datetime.now(timezone.utc)
    prev = GENESIS_PREV_HASH
    with open(path, "w", encoding="utf-8") as handle:
        for i, at in enumerate(
            [now - timedelta(seconds=3600 + 60 * (j + 1)) for j in range(10)]
            + [now - timedelta(seconds=60 * (j + 1)) for j in range(40)]
        ):
            event = AuditEvent(
                occurred_at=at,
                principal_id="svc-a",
                connection_id="demo",
                policy_decision="allowed",
                outcome="success",
                query_shape={"from": "customers"},
                duration_ms=2,
            )
            record = make_record(i, prev, event.model_dump(mode="json", exclude_none=True))
            handle.write(record.model_dump_json() + "\n")
            prev = record.hash

    app = create_app(
        _settings(
            (_OBS_SCOPE,),
            backend="jsonl_chained",
            jsonl_path=str(path),
            anomaly_recent_window_seconds=3600.0,
            anomaly_baseline_window_seconds=3600.0,
            anomaly_min_baseline_events=10,
            anomaly_min_recent_events=3,
        )
    )

    resp = await _get(app)
    assert resp.status_code == 200
    body = resp.json()

    assert body["source"] == "jsonl"
    assert body["events_scanned"] == 50
    assert len(body["principals"]) == 1
    assert body["principals"][0]["principal_id"] == "svc-a"
    assert any(s["kind"] == "volume_spike" for s in body["principals"][0]["signals"])


def _write_multi_principal(path, spikers):
    """spikers: dict of principal_id -> recent event count (baseline fixed at 10)."""
    now = datetime.now(timezone.utc)
    with open(path, "w", encoding="utf-8") as handle:
        for principal, recent in spikers.items():
            times = [now - timedelta(seconds=3600 + 60 * (i + 1)) for i in range(10)]
            times += [now - timedelta(seconds=30 + 40 * (i + 1)) for i in range(recent)]
            for at in times:
                event = AuditEvent(
                    occurred_at=at,
                    principal_id=principal,
                    connection_id="demo",
                    policy_decision="allowed",
                    outcome="success",
                    query_shape={"from": "t", "select": [{"kind": "column", "column": "c"}]},
                    duration_ms=1,
                )
                handle.write(event.model_dump_json(exclude_none=True) + "\n")


@pytest.mark.asyncio
async def test_anomalies_respect_configured_principal_cap_end_to_end(tmp_path):
    path = tmp_path / "audit.jsonl"
    app = create_app(
        _settings(
            (_OBS_SCOPE,),
            backend="jsonl",
            jsonl_path=str(path),
            anomaly_recent_window_seconds=3600.0,
            anomaly_baseline_window_seconds=3600.0,
            anomaly_min_baseline_events=10,
            anomaly_min_recent_events=3,
            anomaly_max_principals_reported=2,
        )
    )
    # Three spiking callers with increasing severity; the cap keeps the top two.
    _write_multi_principal(path, {"p-lo": 30, "p-mid": 50, "p-hi": 90})

    resp = await _get(app)
    assert resp.status_code == 200
    body = resp.json()
    assert body["truncated"] is True
    ids = [p["principal_id"] for p in body["principals"]]
    assert ids == ["p-hi", "p-mid"]  # ranked most-severe first, weakest dropped
