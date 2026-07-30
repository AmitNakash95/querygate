"""Time-windowed metrics history for the observability dashboard (TODO.md
item 44, phase 2 remainder).

Item 44 phase 1's `ObservabilityOverview` (`admin/observability.py`) is an
honest current-process snapshot: counters reset on restart and there is no
stored history, so the admin panel could only show current-value cards, never
a real trend line. This module closes that gap *without* QueryGate taking on
a time-series store of its own — a store the North Star's non-goals rule out
— by querying an **operator-configured** external metrics backend the
operator already runs (a Prometheus-compatible HTTP API scraping this
deployment's own `/metrics`). With no backend configured, the endpoint
honestly reports `source="disabled"` rather than fabricating or emulating a
trend line; this is the same "ship the honest cards, not a dishonest trend
line" posture phase 1 established.

**Composable-interface shape.** `MetricsHistorySource` is a narrow read-only
Protocol so a future backend (e.g. a different TSDB) is one more concrete
class registered by `MetricsHistoryBackend`, never a branch inline at the
route — the same doctrine as `SecretResolver`/`DialectAdapter`/`AuditSink`.

**Read-only, redaction-safe.** Every series is a fleet-wide aggregate computed
by a fixed PromQL expression over the exact metric names/labels QueryGate
itself already emits (`metrics.py`) — `status`, `reason`, `connection` are
already low-cardinality and public by construction. QueryGate only ever reads
from the backend, never writes to it, and no query, value, principal, table,
or column ever appears in a series.

**Bounded by construction.** `metrics_history_max_points_per_series` widens
the effective step (never the window) so a request can never return an
unbounded number of points, mirroring `config_trends.py`'s scan cap.

**Redaction of backend failures.** A raw `httpx` exception string (or a
Prometheus JSON error body) can embed the operator's internal metrics-backend
host/port and the PromQL query text — the same class of leak `health.py`'s
`classify_failure`/`failure_category` split was built to close for database
driver errors. `backend_error` is therefore always one of `classify_failure`'s
small stable categories (or `"invalid_response"`), never the raw exception
text; the real text is logged server-side only via `get_logger()`.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import List, Literal, Optional, Protocol, Tuple

import httpx
import pydantic as pyd

from querygate.core.logging import get_logger
from querygate.health import classify_failure

_DISABLED_NOTE = (
    "Metrics-history surfacing is disabled — no external metrics backend is "
    "configured (METRICS_HISTORY_BACKEND=none, the default). Item 44 phase "
    "1's current-value cards above remain the only trend surface."
)
_ENABLED_NOTE = (
    "Real time-windowed history read from an operator-configured Prometheus-"
    "compatible HTTP API — QueryGate only queries this backend, it never "
    "writes to it. Every series is a fleet-wide aggregate of QueryGate's own "
    "already-public metric labels."
)
_ERROR_NOTE = (
    "The configured metrics backend could not be queried; see backend_error. "
    "Item 44 phase 1's current-value cards above are unaffected."
)


class MetricsHistoryThresholds(pyd.BaseModel):
    """Tunable window/step/bounds, mirroring `ChangeTrendThresholds`'s shape.

    Deliberately excludes the request timeout: that's a `MetricsHistorySource`
    *construction* parameter (threaded straight from `AppConfig` into
    `PrometheusMetricsHistorySource(timeout_seconds=...)`), not a report-shape
    parameter this model's fields describe.
    """

    window_seconds: float = pyd.Field(default=6 * 3600.0, gt=0)
    step_seconds: float = pyd.Field(default=60.0, gt=0)
    max_points_per_series: int = pyd.Field(default=500, ge=1)

    model_config = pyd.ConfigDict(extra="forbid")


class HistoryPoint(pyd.BaseModel):
    ts: str
    value: Optional[float] = None

    model_config = pyd.ConfigDict(extra="forbid")


class MetricsSeries(pyd.BaseModel):
    """One named, fixed trend series — never an arbitrary caller-supplied
    query, always one of the small fixed set `_SERIES_DEFS` declares."""

    id: str
    label: str
    unit: str
    points: List[HistoryPoint] = pyd.Field(default_factory=list)

    model_config = pyd.ConfigDict(extra="forbid")


class MetricsHistoryReport(pyd.BaseModel):
    source: Literal["disabled", "prometheus"] = "disabled"
    generated_at: str
    window_seconds: float
    step_seconds: float
    note: str
    series: List[MetricsSeries] = pyd.Field(default_factory=list)
    backend_error: Optional[str] = None

    model_config = pyd.ConfigDict(extra="forbid")


class MetricsBackendError(Exception):
    """Raised by a `MetricsHistorySource` when the external backend can't be
    queried (unreachable, non-2xx, malformed body). Caught by
    `build_metrics_history_report`, which reports it as `backend_error`
    rather than letting the admin API endpoint fail closed with a 5xx.

    The message MUST be a small, stable, redaction-safe category (see
    `classify_failure`'s categories, or `"invalid_response"`) — never the raw
    exception text or a backend-supplied error string. Both can embed the
    backend's internal host/port or the PromQL query text, the same class of
    leak `health.py`'s `classify_failure`/`failure_category` split exists to
    close. A future `MetricsHistorySource` implementation must keep this
    contract."""


class MetricsHistorySource(Protocol):
    """Narrow read-only seam over an external metrics backend — one concrete
    class per backend, dispatched by `MetricsHistoryBackend` at the route
    factory, never an inline branch at the call site."""

    async def query_history(
        self, *, start: datetime, end: datetime, step_seconds: float
    ) -> List[MetricsSeries]: ...


# (id, label, unit, PromQL template). `{step}` is substituted with a duration
# literal (e.g. "60s") matched to the caller's step, so each rate window lines
# up with the series' own resolution rather than an arbitrary fixed window.
_SERIES_DEFS: Tuple[Tuple[str, str, str, str], ...] = (
    (
        "queries_success_rate",
        "Successful queries",
        "per second",
        'sum(rate(querygate_queries_total{{status="success"}}[{step}]))',
    ),
    (
        "queries_rejected_rate",
        "Rejected queries",
        "per second",
        'sum(rate(querygate_queries_total{{status="rejected"}}[{step}]))',
    ),
    (
        "avg_duration_seconds",
        "Avg query duration",
        "seconds",
        "sum(rate(querygate_query_duration_seconds_sum[{step}])) / "
        "clamp_min(sum(rate(querygate_query_duration_seconds_count[{step}])), 1e-9)",
    ),
    (
        "queue_depth",
        "Queue depth",
        "requests",
        "avg(querygate_queue_depth)",
    ),
    (
        "concurrency_utilization",
        "Concurrency utilization",
        "ratio",
        "avg(querygate_concurrency_in_use) / clamp_min(avg(querygate_concurrency_max), 1e-9)",
    ),
)


class PrometheusMetricsHistorySource:
    """Queries a Prometheus-compatible HTTP API's `/api/v1/query_range` for
    each of `_SERIES_DEFS`'s fixed named series. Never sends anything but
    QueryGate's own previously-emitted metric names — no caller-influenced
    query ever reaches this backend."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def query_history(
        self, *, start: datetime, end: datetime, step_seconds: float
    ) -> List[MetricsSeries]:
        step_literal = f"{step_seconds:g}s"
        series: List[MetricsSeries] = []
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            for series_id, label, unit, promql_template in _SERIES_DEFS:
                promql = promql_template.format(step=step_literal)
                points = await self._query_range(client, promql, start, end, step_seconds)
                series.append(MetricsSeries(id=series_id, label=label, unit=unit, points=points))
        return series

    async def _query_range(
        self,
        client: httpx.AsyncClient,
        promql: str,
        start: datetime,
        end: datetime,
        step_seconds: float,
    ) -> List[HistoryPoint]:
        try:
            response = await client.get(
                f"{self.base_url}/api/v1/query_range",
                params={
                    "query": promql,
                    "start": start.timestamp(),
                    "end": end.timestamp(),
                    "step": step_seconds,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # The raw exception can embed the backend's host/port and the
            # PromQL text (e.g. httpx.HTTPStatusError.__str__ includes the
            # full request URL) — the same class of leak health.py's
            # classify_failure/failure_category split closes for database
            # driver errors. Log the real text server-side only; the API
            # response gets a stable, redaction-safe category.
            get_logger().warning("metrics_history.backend_query_failed", error=str(exc))
            raise MetricsBackendError(classify_failure(exc)) from exc

        if payload.get("status") != "success":
            get_logger().warning(
                "metrics_history.backend_query_failed",
                error=f"status={payload.get('status')!r} error={payload.get('error')!r}",
            )
            raise MetricsBackendError("invalid_response")

        result = payload.get("data", {}).get("result", [])
        if not result:
            return []
        values = result[0].get("values", [])
        points: List[HistoryPoint] = []
        for raw_ts, raw_value in values:
            value: Optional[float]
            try:
                value = float(raw_value)
                if math.isnan(value):
                    value = None
            except (TypeError, ValueError):
                value = None
            points.append(
                HistoryPoint(
                    ts=datetime.fromtimestamp(float(raw_ts), tz=timezone.utc).isoformat(),
                    value=value,
                )
            )
        return points


async def build_metrics_history_report(
    source: Optional[MetricsHistorySource],
    *,
    now: Optional[datetime] = None,
    thresholds: Optional[MetricsHistoryThresholds] = None,
) -> MetricsHistoryReport:
    """Assemble a full report from a source. `source=None` means no external
    backend is configured — reported honestly as `source="disabled"`, not an
    error. A backend query failure is likewise reported, never raised through
    the admin API, so an unreachable Prometheus never 5xxs the dashboard."""
    thresholds = thresholds or MetricsHistoryThresholds()
    now = now or datetime.now(timezone.utc)

    # Bound total points per series by construction: widen the step (never
    # shrink the window) if the configured window/step would exceed the cap.
    effective_step = thresholds.step_seconds
    estimated_points = thresholds.window_seconds / effective_step
    if estimated_points > thresholds.max_points_per_series:
        effective_step = thresholds.window_seconds / thresholds.max_points_per_series

    if source is None:
        return MetricsHistoryReport(
            source="disabled",
            generated_at=now.isoformat(),
            window_seconds=thresholds.window_seconds,
            step_seconds=effective_step,
            note=_DISABLED_NOTE,
        )

    start = now - timedelta(seconds=thresholds.window_seconds)
    try:
        series = await source.query_history(start=start, end=now, step_seconds=effective_step)
    except MetricsBackendError as exc:
        return MetricsHistoryReport(
            source="prometheus",
            generated_at=now.isoformat(),
            window_seconds=thresholds.window_seconds,
            step_seconds=effective_step,
            note=_ERROR_NOTE,
            backend_error=str(exc),
        )

    return MetricsHistoryReport(
        source="prometheus",
        generated_at=now.isoformat(),
        window_seconds=thresholds.window_seconds,
        step_seconds=effective_step,
        note=_ENABLED_NOTE,
        series=series,
    )
