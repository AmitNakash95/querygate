"""Unit tests for time-windowed metrics history (TODO.md item 44, phase 2
remainder): honest "disabled" with no backend configured, a real report built
from a fake `MetricsHistorySource`, points-per-series bounded by construction,
honest degradation when the backend can't be queried, and
`PrometheusMetricsHistorySource`'s own request/response handling against a
monkeypatched httpx client — no real network, no app, no clock.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List

import httpx
import pydantic as pyd
import pytest

from querygate.admin.metrics_history import (
    MetricsBackendError,
    MetricsHistoryThresholds,
    MetricsSeries,
    PrometheusMetricsHistorySource,
    build_metrics_history_report,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)


class _FakeSource:
    def __init__(self, series: List[MetricsSeries], *, error: Exception | None = None) -> None:
        self._series = series
        self._error = error
        self.calls: list = []

    async def query_history(self, *, start, end, step_seconds):
        self.calls.append((start, end, step_seconds))
        if self._error is not None:
            raise self._error
        return self._series


# --- build_metrics_history_report -------------------------------------------


@pytest.mark.asyncio
async def test_disabled_without_a_configured_source():
    report = await build_metrics_history_report(None, now=_NOW)
    assert report.source == "disabled"
    assert report.series == []
    assert report.backend_error is None


@pytest.mark.asyncio
async def test_real_report_from_a_configured_source():
    series = [MetricsSeries(id="queue_depth", label="Queue depth", unit="requests", points=[])]
    source = _FakeSource(series)
    report = await build_metrics_history_report(
        source, now=_NOW, thresholds=MetricsHistoryThresholds(window_seconds=3600, step_seconds=60)
    )
    assert report.source == "prometheus"
    assert report.series == series
    assert report.backend_error is None
    # The source is called with the exact configured window, ending at `now`.
    start, end, step = source.calls[0]
    assert end == _NOW
    assert (end - start).total_seconds() == 3600
    assert step == 60


@pytest.mark.asyncio
async def test_step_is_widened_to_bound_points_per_series():
    source = _FakeSource([])
    # A 1-day window at a 1-second step would be 86400 points; the cap forces
    # the step wider rather than ever truncating the window.
    await build_metrics_history_report(
        source,
        now=_NOW,
        thresholds=MetricsHistoryThresholds(
            window_seconds=86400, step_seconds=1, max_points_per_series=100
        ),
    )
    start, end, step = source.calls[0]
    assert (end - start).total_seconds() == 86400
    assert step == pytest.approx(864.0)


@pytest.mark.asyncio
async def test_backend_error_is_reported_not_raised():
    source = _FakeSource([], error=MetricsBackendError("boom"))
    report = await build_metrics_history_report(source, now=_NOW)
    assert report.source == "prometheus"
    assert report.series == []
    assert report.backend_error == "boom"


# --- PrometheusMetricsHistorySource ------------------------------------------


class _FakeResponse:
    def __init__(self, payload, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)  # type: ignore[arg-type]

    def json(self):
        return self._payload


def _patch_get(monkeypatch, handler):
    async def _get(self, url, params=None):
        return handler(url, params)

    monkeypatch.setattr(httpx.AsyncClient, "get", _get)


@pytest.mark.asyncio
async def test_prometheus_source_parses_a_real_matrix_result(monkeypatch):
    def handler(url, params):
        assert url.endswith("/api/v1/query_range")
        return _FakeResponse(
            {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {},
                            "values": [[1780000000, "1.5"], [1780000060, "2.0"]],
                        }
                    ],
                },
            }
        )

    _patch_get(monkeypatch, handler)
    source = PrometheusMetricsHistorySource("http://prom.local:9090")
    series = await source.query_history(
        start=datetime(2026, 7, 30, 11, 0, tzinfo=timezone.utc), end=_NOW, step_seconds=60
    )
    assert len(series) == 5  # one per _SERIES_DEFS entry
    first = series[0]
    assert len(first.points) == 2
    assert first.points[0].value == pytest.approx(1.5)
    assert first.points[1].value == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_prometheus_source_treats_empty_result_as_no_points(monkeypatch):
    _patch_get(
        monkeypatch,
        lambda url, params: _FakeResponse({"status": "success", "data": {"result": []}}),
    )
    source = PrometheusMetricsHistorySource("http://prom.local:9090")
    series = await source.query_history(
        start=datetime(2026, 7, 30, 11, 0, tzinfo=timezone.utc), end=_NOW, step_seconds=60
    )
    assert all(s.points == [] for s in series)


@pytest.mark.asyncio
async def test_prometheus_source_maps_nan_values_to_none(monkeypatch):
    _patch_get(
        monkeypatch,
        lambda url, params: _FakeResponse(
            {
                "status": "success",
                "data": {"result": [{"metric": {}, "values": [[1780000000, "NaN"]]}]},
            }
        ),
    )
    source = PrometheusMetricsHistorySource("http://prom.local:9090")
    series = await source.query_history(
        start=datetime(2026, 7, 30, 11, 0, tzinfo=timezone.utc), end=_NOW, step_seconds=60
    )
    assert series[0].points[0].value is None


@pytest.mark.asyncio
async def test_prometheus_source_raises_backend_error_on_non_success_status(monkeypatch):
    _patch_get(
        monkeypatch,
        lambda url, params: _FakeResponse({"status": "error", "error": "bad query"}),
    )
    source = PrometheusMetricsHistorySource("http://prom.local:9090")
    with pytest.raises(MetricsBackendError) as exc_info:
        await source.query_history(
            start=datetime(2026, 7, 30, 11, 0, tzinfo=timezone.utc), end=_NOW, step_seconds=60
        )
    # A stable category only — never the raw Prometheus error text (which
    # could echo back the PromQL/query content).
    assert str(exc_info.value) == "invalid_response"
    assert "bad query" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_prometheus_source_raises_backend_error_on_transport_failure(monkeypatch):
    async def _get(self, url, params=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "get", _get)
    source = PrometheusMetricsHistorySource("http://prom.local:9090")
    with pytest.raises(MetricsBackendError) as exc_info:
        await source.query_history(
            start=datetime(2026, 7, 30, 11, 0, tzinfo=timezone.utc), end=_NOW, step_seconds=60
        )
    # A stable category only — never the raw httpx exception text (which
    # embeds the request URL, including the operator's internal host/port).
    assert str(exc_info.value) == "unreachable"


@pytest.mark.asyncio
async def test_prometheus_source_error_category_never_leaks_the_backend_url(monkeypatch):
    # httpx.HTTPStatusError.__str__ embeds the full request URL (including
    # query params) on a non-2xx response — the exact leak this classify-not-
    # echo behavior exists to close. Use a real (non-2xx) httpx.Response, not
    # the hand-rolled _FakeResponse, so this genuinely exercises that path.
    async def _get(self, url, params=None):
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(500, text="internal error", request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", _get)
    source = PrometheusMetricsHistorySource("http://prom.internal.example:9090")
    with pytest.raises(MetricsBackendError) as exc_info:
        await source.query_history(
            start=datetime(2026, 7, 30, 11, 0, tzinfo=timezone.utc), end=_NOW, step_seconds=60
        )
    message = str(exc_info.value)
    assert "prom.internal.example" not in message
    assert "9090" not in message
    assert "query_range" not in message


# --- thresholds validation ---------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"window_seconds": 0},
        {"step_seconds": -1},
        {"max_points_per_series": 0},
    ],
)
def test_thresholds_reject_out_of_range_values(overrides):
    with pytest.raises(pyd.ValidationError):
        MetricsHistoryThresholds(**overrides)


# --- route helpers (config -> thresholds/source wiring) ----------------------


def test_route_helpers_map_config_to_thresholds_and_source():
    from querygate.api.admin_observability_routes import (
        _metrics_history_source,
        _metrics_history_thresholds,
    )
    from querygate.core.config import AppConfig

    cfg = AppConfig(
        environment="localhost",
        metrics_history_backend="prometheus",
        metrics_history_prometheus_url="http://prom.internal:9090",
        metrics_history_window_seconds=1800.0,
        metrics_history_step_seconds=30.0,
        metrics_history_max_points_per_series=42,
        metrics_history_request_timeout_seconds=7.5,
    )
    th = _metrics_history_thresholds(cfg)
    assert th.window_seconds == 1800.0
    assert th.step_seconds == 30.0
    assert th.max_points_per_series == 42

    source = _metrics_history_source(cfg)
    assert isinstance(source, PrometheusMetricsHistorySource)
    assert source.base_url == "http://prom.internal:9090"
    assert source.timeout_seconds == 7.5

    # Backend not "prometheus" -> no source (endpoint reports "disabled").
    cfg_none = AppConfig(environment="localhost", metrics_history_backend="none")
    assert _metrics_history_source(cfg_none) is None

    # Backend "prometheus" but no URL configured -> also no source, not a crash.
    cfg_no_url = AppConfig(environment="localhost", metrics_history_backend="prometheus")
    assert _metrics_history_source(cfg_no_url) is None
