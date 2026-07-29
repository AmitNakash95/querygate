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
"""

from __future__ import annotations

from typing import Callable, Optional

from fastapi import APIRouter, Depends

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
from querygate.admin.observability import ObservabilityOverview, build_overview
from querygate.api._errors import require_scope
from querygate.core.auth import Principal
from querygate.core.config import AppConfig, AuditSinkBackend
from querygate.core.scopes import ADMIN_OBSERVABILITY_READ_SCOPE


def _change_trend_thresholds(cfg: AppConfig) -> ChangeTrendThresholds:
    return ChangeTrendThresholds(
        recent_window_seconds=cfg.change_trend_recent_window_seconds,
        baseline_window_seconds=cfg.change_trend_baseline_window_seconds,
        max_events_scanned=cfg.change_trend_max_events_scanned,
    )


def _change_trend_source(cfg: AppConfig) -> Optional[ChangeEventSource]:
    # Change-trend surfacing reads the durable audit stream; without the
    # JSONL sink there is nothing persisted to read, reported honestly as
    # source="disabled".
    if cfg.audit_sink_backend != AuditSinkBackend.JSONL:
        return None
    return JsonlChangeEventSource(cfg.audit_jsonl_path)


def _anomaly_thresholds(cfg: AppConfig) -> AnomalyThresholds:
    return AnomalyThresholds(
        recent_window_seconds=cfg.anomaly_recent_window_seconds,
        baseline_window_seconds=cfg.anomaly_baseline_window_seconds,
        min_baseline_events=cfg.anomaly_min_baseline_events,
        min_recent_events=cfg.anomaly_min_recent_events,
        volume_spike_ratio=cfg.anomaly_volume_spike_ratio,
        rejection_rate_delta=cfg.anomaly_rejection_rate_delta,
        max_events_scanned=cfg.anomaly_max_events_scanned,
        max_principals_reported=cfg.anomaly_max_principals_reported,
    )


def _anomaly_source(cfg: AppConfig) -> Optional[AuditEventSource]:
    # Anomaly surfacing reads the durable audit stream; without the JSONL sink
    # there is nothing persisted to read, reported honestly as source="disabled".
    if cfg.audit_sink_backend != AuditSinkBackend.JSONL:
        return None
    return JsonlAuditEventSource(cfg.audit_jsonl_path)


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
        return build_anomaly_report(_anomaly_source(cfg), thresholds=_anomaly_thresholds(cfg))

    @router.get("/config-changes", response_model=ConfigCatalogChangeTrend)
    async def observability_config_changes(principal: Principal = Depends(get_principal)):
        require_scope(principal, ADMIN_OBSERVABILITY_READ_SCOPE)
        return build_change_trend_report(
            _change_trend_source(cfg), thresholds=_change_trend_thresholds(cfg)
        )

    return router
