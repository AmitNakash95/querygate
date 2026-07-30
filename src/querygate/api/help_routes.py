"""REST endpoints for the offline product guide and scoped diagnostics."""

from __future__ import annotations

from typing import Callable, Literal

from fastapi import APIRouter, Depends, Query

from querygate.admin.anomaly import JsonlAuditEventSource
from querygate.core.auth import Principal
from querygate.core.config import AppConfig, AuditSinkBackend
from querygate.help.models import (
    AccessSummary,
    ConfigFieldExplanation,
    ErrorExplanation,
    GuideSearchResponse,
    GuideTopicResponse,
    RedactedConfiguration,
    SetupChecklistResponse,
)
from querygate.help.personal_denials import RecentDenialsReport, build_recent_denials_report
from querygate.help.service import get_guide_service


def build_help_router(
    get_principal: Callable[..., Principal], cfg: AppConfig, prefix: str = "/api/v1"
) -> APIRouter:
    router = APIRouter(prefix=f"{prefix}/help", tags=["product-guide"])

    # Static product guidance is intentionally public. It contains only the
    # packaged corpus and never touches deployment-specific state.
    @router.get("/search", response_model=GuideSearchResponse)
    async def search_guide(
        q: str = Query(min_length=1, max_length=200),
        limit: int = Query(default=5, ge=1, le=10),
    ):
        return get_guide_service().search(q, limit=limit)

    @router.get("/topics/{topic_id}", response_model=GuideTopicResponse)
    async def get_guide_topic(
        topic_id: str,
        max_response_bytes: int = Query(
            default=16_384,
            ge=512,
            description="Byte cap on the serialized response; truncates content.",
        ),
    ):
        return get_guide_service().topic(topic_id, max_response_bytes=max_response_bytes)

    @router.get("/setup-checklist", response_model=SetupChecklistResponse)
    async def get_setup_checklist(
        profile: Literal["local", "container", "production"] = "local",
    ):
        return get_guide_service().setup_checklist(profile)

    @router.get("/config-fields/{model}/{field}", response_model=ConfigFieldExplanation)
    async def explain_config_field(model: str, field: str):
        return get_guide_service().explain_config_field(model, field)

    @router.get("/errors/{error_code}", response_model=ErrorExplanation)
    async def explain_error(error_code: str):
        return get_guide_service().explain_error(error_code)

    # Live endpoints authenticate before their context is assembled. They do
    # not participate in the static search index or its process-wide cache.
    @router.get("/my-access", response_model=AccessSummary)
    async def describe_my_access(principal: Principal = Depends(get_principal)):
        return get_guide_service().access_summary(principal)

    @router.get("/my-recent-denials", response_model=RecentDenialsReport)
    async def describe_my_recent_denials(principal: Principal = Depends(get_principal)):
        source = None
        if cfg.audit_sink_backend == AuditSinkBackend.JSONL:
            source = JsonlAuditEventSource(cfg.audit_jsonl_path)
        return build_recent_denials_report(
            source,
            principal_id=principal.subject,
            lookback_seconds=cfg.personal_denials_lookback_seconds,
            max_events_scanned=cfg.personal_denials_max_events_scanned,
            limit=cfg.personal_denials_limit,
        )

    @router.get("/configuration", response_model=RedactedConfiguration)
    async def inspect_configuration(principal: Principal = Depends(get_principal)):
        return get_guide_service().redacted_configuration(cfg, principal)

    return router
