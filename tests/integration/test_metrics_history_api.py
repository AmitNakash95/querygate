"""Integration tests for the metrics-history API (TODO.md item 44, phase 2
remainder): GET /api/v1/admin/observability/history — scope enforcement,
honest "disabled" with no backend configured, a real report when a Prometheus
backend is configured (HTTP call monkeypatched, no real network), and honest
degradation when that backend can't be reached."""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from querygate.api.app import create_app
from querygate.core.config import AppConfig

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"
_ADMIN_KEY = "metrics-history-admin-key"
_OBS_SCOPE = "admin:observability:read"


def _settings(scopes, **overrides) -> AppConfig:
    return AppConfig(
        environment="localhost",
        mcp_enabled=False,
        audit_sink_backend="none",
        api_keys=[_ADMIN_KEY],
        api_key_scopes=list(scopes),
        **overrides,
    )


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


async def _get(app, key=_ADMIN_KEY):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        return await client.get("/api/v1/admin/observability/history", headers=_auth(key))


@pytest.mark.asyncio
async def test_history_requires_the_observability_scope():
    app = create_app(_settings(("admin:connections:read",)))
    resp = await _get(app)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_history_disabled_without_a_configured_backend():
    app = create_app(_settings((_OBS_SCOPE,)))
    resp = await _get(app)
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "disabled"
    assert body["series"] == []


@pytest.mark.asyncio
async def test_history_disabled_when_backend_set_without_a_url():
    # A backend chosen without a URL is treated as unconfigured, not a crash.
    app = create_app(_settings((_OBS_SCOPE,), metrics_history_backend="prometheus"))
    resp = await _get(app)
    assert resp.status_code == 200
    assert resp.json()["source"] == "disabled"


@pytest.mark.asyncio
async def test_history_returns_a_real_series_from_a_configured_prometheus_backend(monkeypatch):
    _original_get = httpx.AsyncClient.get
    captured_params = []

    async def _get_mock(self, url, *args, **kwargs):
        # The prometheus backend's own client is the only caller passing a
        # full http(s) URL with query_range params; the test's own ASGI
        # client calls with a relative app path and must pass through
        # unmocked, or this patch would also break the request to our app.
        if not str(url).endswith("/api/v1/query_range"):
            return await _original_get(self, url, *args, **kwargs)
        captured_params.append(kwargs.get("params"))
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [{"metric": {}, "values": [[1780000000, "3.0"], [1780000060, "4.0"]]}]
                },
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", _get_mock)
    app = create_app(
        _settings(
            (_OBS_SCOPE,),
            metrics_history_backend="prometheus",
            metrics_history_prometheus_url="http://prom.local:9090",
            metrics_history_window_seconds=3600,
            metrics_history_step_seconds=60,
        )
    )
    resp = await _get(app)
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "prometheus"
    assert body["backend_error"] is None
    assert len(body["series"]) == 5
    assert body["series"][0]["points"][0]["value"] == pytest.approx(3.0)

    # The configured window/step actually reach both the response shape and
    # the real outgoing request — not just an internal object the mock
    # ignores (params is asserted per-call below, not just once).
    assert body["window_seconds"] == 3600
    assert body["step_seconds"] == 60
    assert len(captured_params) == 5  # one call per fixed series
    for params in captured_params:
        assert params["step"] == 60
        assert params["end"] - params["start"] == pytest.approx(3600)


@pytest.mark.asyncio
async def test_history_reports_backend_error_when_prometheus_is_unreachable(monkeypatch):
    _original_get = httpx.AsyncClient.get

    async def _get_mock(self, url, *args, **kwargs):
        if not str(url).endswith("/api/v1/query_range"):
            return await _original_get(self, url, *args, **kwargs)
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "get", _get_mock)
    app = create_app(
        _settings(
            (_OBS_SCOPE,),
            metrics_history_backend="prometheus",
            metrics_history_prometheus_url="http://prom.local:9090",
        )
    )
    resp = await _get(app)
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "prometheus"
    # A stable category only — never the raw httpx exception text, which
    # would otherwise embed the configured backend's internal host/port.
    assert body["backend_error"] == "unreachable"
    assert "prom.local" not in resp.text
    assert body["series"] == []
