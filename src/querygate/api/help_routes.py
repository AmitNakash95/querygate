"""REST endpoints for the offline product guide and scoped diagnostics."""

from __future__ import annotations

from typing import Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.exceptions import AuthorizationError, NotFoundError, QueryValidationError
from querygate.help.models import (
    AccessSummary,
    ConfigFieldExplanation,
    ErrorExplanation,
    GuideSearchResponse,
    GuideTopicResponse,
    RedactedConfiguration,
    SetupChecklistResponse,
)
from querygate.help.service import get_guide_service


def _not_found(exc: NotFoundError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


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
        try:
            return get_guide_service().search(q, limit=limit)
        except QueryValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    @router.get("/topics/{topic_id}", response_model=GuideTopicResponse)
    async def get_guide_topic(topic_id: str):
        try:
            return get_guide_service().topic(topic_id)
        except NotFoundError as exc:
            raise _not_found(exc)

    @router.get("/setup-checklist", response_model=SetupChecklistResponse)
    async def get_setup_checklist(
        profile: Literal["local", "container", "production"] = "local",
    ):
        return get_guide_service().setup_checklist(profile)

    @router.get("/config-fields/{model}/{field}", response_model=ConfigFieldExplanation)
    async def explain_config_field(model: str, field: str):
        try:
            return get_guide_service().explain_config_field(model, field)
        except NotFoundError as exc:
            raise _not_found(exc)

    @router.get("/errors/{error_code}", response_model=ErrorExplanation)
    async def explain_error(error_code: str):
        try:
            return get_guide_service().explain_error(error_code)
        except NotFoundError as exc:
            raise _not_found(exc)

    # Live endpoints authenticate before their context is assembled. They do
    # not participate in the static search index or its process-wide cache.
    @router.get("/my-access", response_model=AccessSummary)
    async def describe_my_access(principal: Principal = Depends(get_principal)):
        return get_guide_service().access_summary(principal)

    @router.get("/configuration", response_model=RedactedConfiguration)
    async def inspect_configuration(principal: Principal = Depends(get_principal)):
        try:
            return get_guide_service().redacted_configuration(cfg, principal)
        except AuthorizationError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))

    return router
