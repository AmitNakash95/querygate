"""Integration tests for the config/catalog change-trend API (TODO.md item 44,
phase 2 slice): GET /api/v1/admin/observability/config-changes — scope
enforcement, honest "disabled" when no persisted sink, and a real trend
surfaced from a JSONL file without leaking version/proposal content."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from querygate.api.app import create_app
from querygate.audit.events import CatalogGovernanceEvent, ConfigChangeEvent
from querygate.core.config import AppConfig

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"
_ADMIN_KEY = "change-trend-admin-key"
_OBS_SCOPE = "admin:observability:read"


def _settings(scopes, *, backend="none", jsonl_path="", **trend) -> AppConfig:
    return AppConfig(
        environment="localhost",
        mcp_enabled=False,
        audit_sink_backend=backend,
        audit_jsonl_path=jsonl_path or "var/audit/querygate-audit.jsonl",
        api_keys=[_ADMIN_KEY],
        api_key_scopes=list(scopes),
        **trend,
    )


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


async def _get(app, key=_ADMIN_KEY):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        return await client.get("/api/v1/admin/observability/config-changes", headers=_auth(key))


def _write_events(path):
    now = datetime.now(timezone.utc)
    lines = []
    # 10 baseline config-governance events (window (now-7200, now-3600]) + 40
    # recent (now-3600, now]) -> a clean 4x ratio, plus a few catalog events.
    for i in range(10):
        lines.append(
            ConfigChangeEvent(
                occurred_at=now - timedelta(seconds=3600 + 60 * (i + 1)),
                action="stage",
                outcome="success",
                description="a proposed change",
            )
        )
    for i in range(40):
        lines.append(
            ConfigChangeEvent(
                occurred_at=now - timedelta(seconds=60 * (i + 1)),
                action="apply",
                outcome="success",
                description="a proposed change",
            )
        )
    for i in range(3):
        lines.append(
            CatalogGovernanceEvent(
                occurred_at=now - timedelta(seconds=60 * (i + 1)),
                action="publish",
                outcome="success",
                connection_id="demo",
            )
        )
    with open(path, "w", encoding="utf-8") as handle:
        for event in lines:
            handle.write(event.model_dump_json(exclude_none=True) + "\n")


@pytest.mark.asyncio
async def test_config_changes_requires_the_observability_scope():
    app = create_app(_settings(("admin:connections:read",)))
    resp = await _get(app)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_config_changes_disabled_without_a_persisted_sink():
    app = create_app(_settings((_OBS_SCOPE,), backend="none"))
    resp = await _get(app)
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "disabled"
    assert body["config_recent"]["total"] == 0


@pytest.mark.asyncio
async def test_config_changes_surface_a_real_trend_from_the_jsonl_stream(tmp_path):
    path = tmp_path / "audit.jsonl"
    app = create_app(
        _settings(
            (_OBS_SCOPE,),
            backend="jsonl",
            jsonl_path=str(path),
            # Equal windows keep the ratio arithmetic obvious.
            change_trend_recent_window_seconds=3600.0,
            change_trend_baseline_window_seconds=3600.0,
        )
    )
    _write_events(path)

    resp = await _get(app)
    assert resp.status_code == 200
    body = resp.json()

    assert body["source"] == "jsonl"
    assert body["events_scanned"] == 53
    assert body["config_recent"]["total"] == 40
    assert body["config_baseline"]["total"] == 10
    assert body["config_volume_ratio"] == pytest.approx(4.0)
    assert body["catalog_recent"]["total"] == 3
    action_names = {a["action"] for a in body["config_recent"]["by_action"]}
    assert action_names == {"apply"}

    # Redaction: the surfaced report never carries version/proposal content.
    blob = resp.text
    assert "a proposed change" not in blob
    assert "description" not in blob


@pytest.mark.asyncio
async def test_config_changes_surface_a_real_trend_from_a_hash_chained_ledger(tmp_path):
    """TODO.md item 136: AUDIT_SINK_BACKEND=jsonl_chained used to be refused at
    this route's gate entirely (source="disabled"), even though the underlying
    reader already unwrapped the chain envelope — this is the full HTTP-level
    regression, not just the unit-level `_change_trend_source` helper test."""
    from querygate.audit.ledger import GENESIS_PREV_HASH, make_record

    path = tmp_path / "audit.jsonl"
    now = datetime.now(timezone.utc)
    events = [
        ConfigChangeEvent(
            occurred_at=now - timedelta(seconds=3600 + 60 * (i + 1)),
            action="stage",
            outcome="success",
            description="a proposed change",
        )
        for i in range(10)
    ] + [
        ConfigChangeEvent(
            occurred_at=now - timedelta(seconds=60 * (i + 1)),
            action="apply",
            outcome="success",
            description="a proposed change",
        )
        for i in range(40)
    ]
    prev = GENESIS_PREV_HASH
    with open(path, "w", encoding="utf-8") as handle:
        for i, event in enumerate(events):
            record = make_record(i, prev, event.model_dump(mode="json", exclude_none=True))
            handle.write(record.model_dump_json() + "\n")
            prev = record.hash

    app = create_app(
        _settings(
            (_OBS_SCOPE,),
            backend="jsonl_chained",
            jsonl_path=str(path),
            change_trend_recent_window_seconds=3600.0,
            change_trend_baseline_window_seconds=3600.0,
        )
    )

    resp = await _get(app)
    assert resp.status_code == 200
    body = resp.json()

    assert body["source"] == "jsonl_chained"
    assert body["events_scanned"] == 50
    assert body["config_recent"]["total"] == 40
    assert body["config_baseline"]["total"] == 10
    assert body["config_volume_ratio"] == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_config_changes_bounded_by_configured_scan_cap(tmp_path):
    path = tmp_path / "audit.jsonl"
    app = create_app(
        _settings(
            (_OBS_SCOPE,),
            backend="jsonl",
            jsonl_path=str(path),
            change_trend_max_events_scanned=5,
        )
    )
    _write_events(path)

    resp = await _get(app)
    assert resp.status_code == 200
    body = resp.json()
    assert body["truncated"] is True
    assert body["events_scanned"] == 5
