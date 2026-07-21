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
"""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends

from querygate.admin.observability import ObservabilityOverview, build_overview
from querygate.api._errors import require_scope
from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.scopes import ADMIN_OBSERVABILITY_READ_SCOPE


def build_admin_observability_router(
    get_principal: Callable[..., Principal], cfg: AppConfig, prefix: str = "/api/v1"
) -> APIRouter:
    router = APIRouter(prefix=f"{prefix}/admin/observability")

    @router.get("/overview", response_model=ObservabilityOverview)
    async def observability_overview(principal: Principal = Depends(get_principal)):
        require_scope(principal, ADMIN_OBSERVABILITY_READ_SCOPE)
        return build_overview()

    return router
