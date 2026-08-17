"""Admin observability overview API (TODO.md item 44, phase 1).

A single `admin:observability:read`-scoped endpoint returning an aggregated,
redaction-safe operational overview built from the in-process Prometheus
registry (`querygate/metrics.py`) — query volume, success/rejection categories,
queue and concurrency pressure, and cost-estimation gate health, globally and
per connection.

The aggregation and its durability/redaction posture live in
`querygate/admin/observability.py`; this router is a thin, scope-gated
transport wrapper. The browser dashboard that renders this overview (overview
cards + time-window charts), a config/catalog-change trend card, and querying
an operator-configured external metrics backend for durable history are all
deferred to phase 2.

TODO.md item 59 adds a second, distinct read on this same router: per-principal
behavioral anomalies over the *persisted audit stream* (not the Prometheus
registry). The aggregation and its 32C read-only boundary live in
`querygate/admin/anomaly.py`; this stays a thin, scope-gated wrapper. Surfacing
it in item 44's browser dashboard is item 59 phase 2.

Item 44 phase 2 adds a third read on this router: fleet-wide config/catalog
governance change-volume trend, also over the persisted audit stream. The
aggregation lives in `querygate/admin/config_trends.py`, following the same
`*EventSource` protocol shape `admin/anomaly.py` established.

Item 44 phase 2's remainder adds a fourth read: time-windowed metrics
*history* (real trend charts, not point-in-time cards) from an
operator-configured external metrics backend. The aggregation and its
honest-disabled-by-default posture live in `querygate/admin/metrics_history.py`.

TODO.md item 134 phase 2 adds a fifth read: managed search over the durable
WORM (S3 Object Lock) audit archive — a distinct, long-retention copy from
the persisted audit stream every read above uses. The scan, its bounds, and
its honest-disabled posture live in `querygate/audit/worm_search.py`; this
stays a thin, scope-gated wrapper, gated by its OWN scope
(`ADMIN_AUDIT_WORM_SEARCH_SCOPE`) rather than `ADMIN_OBSERVABILITY_READ_SCOPE`
— see that module's docstring for why. Deliberately REST-only: no sibling
read on this router (overview/anomalies/config-changes/history) has an MCP
tool counterpart either, so this stays consistent with the existing surface
rather than introducing a new REST/MCP asymmetry among admin observability
reads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from fastapi import APIRouter, Depends, Query

from querygate.admin.anomaly import (
    AnomalyReport,
    AnomalyThresholds,
    AuditEventSource,
    JsonlAuditEventSource,
    build_anomaly_report,
)
from querygate.admin.config_trends import (
    ChangeEventSource,
    ChangeTrendThresholds,
    ConfigCatalogChangeTrend,
    JsonlChangeEventSource,
    build_change_trend_report,
)
from querygate.admin.metrics_history import (
    MetricsHistoryReport,
    MetricsHistorySource,
    MetricsHistoryThresholds,
    PrometheusMetricsHistorySource,
    build_metrics_history_report,
)
from querygate.admin.observability import ObservabilityOverview, build_overview
from querygate.admin.observed_shapes import (
    ObservedShapeReport,
    build_observed_shape_report,
    observed_shape_store,
)
from querygate.api._errors import mask_unexpected, require_scope
from querygate.audit.ledger import resolve_ledger_key
from querygate.audit.worm_search import (
    WormSearchEventType,
    WormSearchResult,
    build_worm_search_result,
)
import pydantic as pyd

from querygate.core.auth import Principal
from querygate.core.exceptions import NotFoundError, QueryValidationError
from querygate.core.config import AppConfig, MetricsHistoryBackend
from querygate.core.scopes import (
    ADMIN_AUDIT_WORM_SEARCH_SCOPE,
    ADMIN_OBSERVABILITY_READ_SCOPE,
    ADMIN_SHAPES_READ_SCOPE,
)
from querygate.templates.models import QueryTemplate


def _change_trend_thresholds(cfg: AppConfig) -> ChangeTrendThresholds:
    return ChangeTrendThresholds(
        recent_window_seconds=cfg.change_trend_recent_window_seconds,
        baseline_window_seconds=cfg.change_trend_baseline_window_seconds,
        max_events_scanned=cfg.change_trend_max_events_scanned,
        max_lines_read=cfg.change_trend_max_lines_read,
        max_consecutive_out_of_window=cfg.change_trend_max_consecutive_out_of_window,
    )


def _change_trend_source(cfg: AppConfig) -> Optional[ChangeEventSource]:
    # Change-trend surfacing reads the durable audit stream; without a
    # locally-readable audit backend there is nothing persisted to read,
    # reported honestly as source="disabled".
    if not cfg.audit_sink_backend.is_locally_readable():
        return None
    return JsonlChangeEventSource(
        cfg.audit_jsonl_path,
        ledger_key=resolve_ledger_key(cfg.audit_ledger_hmac_key),
        require_envelope=cfg.audit_sink_backend.wraps_events_in_a_hash_chain_envelope(),
    )


def _metrics_history_thresholds(cfg: AppConfig) -> MetricsHistoryThresholds:
    return MetricsHistoryThresholds(
        window_seconds=cfg.metrics_history_window_seconds,
        step_seconds=cfg.metrics_history_step_seconds,
        max_points_per_series=cfg.metrics_history_max_points_per_series,
    )


def _metrics_history_source(cfg: AppConfig) -> Optional[MetricsHistorySource]:
    # No backend configured (or configured without a URL) -> nothing to query;
    # reported honestly as source="disabled" rather than attempting a request
    # against an empty base URL.
    if cfg.metrics_history_backend != MetricsHistoryBackend.PROMETHEUS:
        return None
    if not cfg.metrics_history_prometheus_url:
        return None
    return PrometheusMetricsHistorySource(
        cfg.metrics_history_prometheus_url,
        timeout_seconds=cfg.metrics_history_request_timeout_seconds,
    )


def _anomaly_thresholds(cfg: AppConfig) -> AnomalyThresholds:
    return AnomalyThresholds(
        recent_window_seconds=cfg.anomaly_recent_window_seconds,
        baseline_window_seconds=cfg.anomaly_baseline_window_seconds,
        min_baseline_events=cfg.anomaly_min_baseline_events,
        min_recent_events=cfg.anomaly_min_recent_events,
        volume_spike_ratio=cfg.anomaly_volume_spike_ratio,
        rejection_rate_delta=cfg.anomaly_rejection_rate_delta,
        max_events_scanned=cfg.anomaly_max_events_scanned,
        max_lines_read=cfg.anomaly_max_lines_read,
        max_consecutive_out_of_window=cfg.anomaly_max_consecutive_out_of_window,
        max_principals_reported=cfg.anomaly_max_principals_reported,
    )


def _anomaly_source(cfg: AppConfig) -> Optional[AuditEventSource]:
    # Anomaly surfacing reads the durable audit stream; without a
    # locally-readable audit backend there is nothing persisted to read,
    # reported honestly as source="disabled".
    if not cfg.audit_sink_backend.is_locally_readable():
        return None
    return JsonlAuditEventSource(
        cfg.audit_jsonl_path,
        ledger_key=resolve_ledger_key(cfg.audit_ledger_hmac_key),
        require_envelope=cfg.audit_sink_backend.wraps_events_in_a_hash_chain_envelope(),
    )


def build_admin_observability_router(
    get_principal: Callable[..., Principal], cfg: AppConfig, prefix: str = "/api/v1"
) -> APIRouter:
    router = APIRouter(prefix=f"{prefix}/admin/observability")

    @router.get("/overview", response_model=ObservabilityOverview)
    async def observability_overview(principal: Principal = Depends(get_principal)):
        require_scope(principal, ADMIN_OBSERVABILITY_READ_SCOPE)
        return build_overview()

    @router.get("/anomalies", response_model=AnomalyReport)
    async def observability_anomalies(principal: Principal = Depends(get_principal)):
        require_scope(principal, ADMIN_OBSERVABILITY_READ_SCOPE)
        return build_anomaly_report(
            _anomaly_source(cfg),
            thresholds=_anomaly_thresholds(cfg),
            backend_label=cfg.audit_sink_backend.value,
        )

    @router.get("/config-changes", response_model=ConfigCatalogChangeTrend)
    async def observability_config_changes(principal: Principal = Depends(get_principal)):
        require_scope(principal, ADMIN_OBSERVABILITY_READ_SCOPE)
        return build_change_trend_report(
            _change_trend_source(cfg),
            thresholds=_change_trend_thresholds(cfg),
            backend_label=cfg.audit_sink_backend.value,
        )

    @router.get("/history", response_model=MetricsHistoryReport)
    async def observability_history(principal: Principal = Depends(get_principal)):
        require_scope(principal, ADMIN_OBSERVABILITY_READ_SCOPE)
        return await build_metrics_history_report(
            _metrics_history_source(cfg), thresholds=_metrics_history_thresholds(cfg)
        )

    @router.get("/worm-search", response_model=WormSearchResult)
    async def observability_worm_search(
        start_time: Optional[datetime] = Query(
            default=None, description="Inclusive start of the search window. Required."
        ),
        end_time: Optional[datetime] = Query(
            default=None, description="Inclusive end of the search window. Required."
        ),
        event_type: Optional[WormSearchEventType] = Query(default=None),
        connection_id: Optional[str] = Query(default=None),
        principal_id: Optional[str] = Query(default=None),
        limit: Optional[int] = Query(default=None, ge=1),
        cursor: Optional[str] = Query(
            default=None, description="Opaque resumption token from a previous page."
        ),
        principal: Principal = Depends(get_principal),
    ):
        # A dedicated scope, deliberately not ADMIN_OBSERVABILITY_READ_SCOPE
        # — see audit/worm_search.py's module docstring for why the WORM
        # archive is gated more strictly than the in-process reads above.
        require_scope(principal, ADMIN_AUDIT_WORM_SEARCH_SCOPE)
        # start_time/end_time stay optional at the FastAPI layer (rather than
        # Query(...)) so the "both required" contract is enforced once, in
        # audit/worm_search.py's own domain validation (QueryValidationError
        # -> 422), the same place every other bound on this search is
        # enforced — not duplicated as a second, framework-level rule here.
        #
        # Unlike the other four reads on this router, this one makes a real
        # network call (S3) that can fail with a raw botocore exception —
        # mask_unexpected() keeps that failure from leaking the bucket name/
        # endpoint/driver text to the client, in-process rather than as an
        # app-level 500 handler, so it still applies under debug=True (see
        # mask_unexpected's own docstring — security-invariant-reviewer,
        # 2026-08-06, WS-7).
        with mask_unexpected():
            return await build_worm_search_result(
                cfg,
                start_time=start_time,
                end_time=end_time,
                event_type=event_type,
                connection_id=connection_id,
                principal_id=principal_id,
                limit=limit,
                cursor=cursor,
            )

    @router.get("/observed-shapes", response_model=ObservedShapeReport)
    async def observability_observed_shapes(
        connection_id: Optional[str] = Query(default=None),
        principal_id: Optional[str] = Query(default=None),
        principal: Principal = Depends(get_principal),
    ):
        # Its own scope — an observed shape names one principal's exact tables,
        # columns and predicates, which is materially more specific than the
        # aggregate reads above (see core/scopes.py).
        require_scope(principal, ADMIN_SHAPES_READ_SCOPE)
        return build_observed_shape_report(connection_id=connection_id, principal_id=principal_id)

    @router.get("/observed-shapes/{shape_hash}/template-draft", response_model=QueryTemplate)
    async def observability_observed_shape_draft(
        shape_hash: str,
        template_id: str = Query(
            description="Identifier for the drafted template (a valid identifier)."
        ),
        connection_id: Optional[str] = Query(default=None),
        principal_id: Optional[str] = Query(default=None),
        description: Optional[str] = Query(default=None),
        principal: Principal = Depends(get_principal),
    ):
        require_scope(principal, ADMIN_SHAPES_READ_SCOPE)
        # Returns a draft for review; deliberately does NOT install it. Adding
        # a template stays an edit to the templates file (or a governed config
        # version), so the human review gate cannot be bypassed by calling
        # this endpoint — the same quarantined-draft posture catalog
        # governance takes for generated entries.
        store = observed_shape_store()
        if not store.enabled:
            raise NotFoundError("Observed-shape recording is disabled.")
        # A hash is not unique on its own (one shape run by two principals is
        # two entries), so the optional filters are threaded through rather
        # than drafting from whichever entry happened to be recorded first.
        shape = store.get(shape_hash, connection_id=connection_id, principal_id=principal_id)
        if shape is None:
            raise NotFoundError(f"Unknown observed shape: {shape_hash!r}")
        try:
            return shape.to_template_draft(template_id, description=description)
        except pyd.ValidationError as exc:
            raise QueryValidationError(f"Invalid template draft: {exc}") from exc

    return router
