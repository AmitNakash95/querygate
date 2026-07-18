"""REST admin API for the config-governance plane (querygate/admin/).

Separate from `POST /admin/reload-config` (api/routes.py) on purpose: that
endpoint keeps reloading whatever `AppConfig.connections_file`/`policy_file`/
`catalog_file` currently point at on disk — the right fit for infra-as-code
deployments that edit those files directly. This router instead lets a
caller submit new connections/policy/catalog content over HTTP, validate it,
stage it, apply it, and roll back to an earlier version — with every action
attributed to the calling principal and recorded in the audit trail.
"""

from __future__ import annotations

from typing import Callable, List, Optional

import pydantic as pyd
from fastapi import APIRouter, Depends, HTTPException, status

from querygate.admin import service as governance
from querygate.admin.models import ConfigPreview, ConfigVersion
from querygate.config_reload import ReloadResult
from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.exceptions import ConfigValidationError, NotFoundError
from querygate.core.scopes import ADMIN_CONFIG_READ_SCOPE, ADMIN_CONFIG_WRITE_SCOPE


class ConfigChangeRequest(pyd.BaseModel):
    """A field left unset inherits unchanged from the current active version
    — only send the file(s) that actually change.
    """

    connections_yaml: Optional[str] = None
    policy_yaml: Optional[str] = None
    catalog_yaml: Optional[str] = None
    description: Optional[str] = None


class ValidationResult(pyd.BaseModel):
    valid: bool
    errors: List[str] = pyd.Field(default_factory=list)


class ApplyResult(pyd.BaseModel):
    version: ConfigVersion
    reload: ReloadResult


def _require_scope(principal: Principal, scope: str) -> None:
    if scope not in principal.scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=f"Missing required scope: {scope!r}"
        )


def build_admin_config_router(
    get_principal: Callable[..., Principal], cfg: AppConfig, prefix: str = "/api/v1"
) -> APIRouter:
    router = APIRouter(prefix=f"{prefix}/admin/config")

    @router.post("/validate", response_model=ValidationResult)
    async def validate_endpoint(
        request: ConfigChangeRequest, principal: Principal = Depends(get_principal)
    ):
        _require_scope(principal, ADMIN_CONFIG_WRITE_SCOPE)
        errors = governance.validate(
            cfg,
            principal,
            connections_yaml=request.connections_yaml,
            policy_yaml=request.policy_yaml,
            catalog_yaml=request.catalog_yaml,
        )
        return ValidationResult(valid=not errors, errors=errors)

    @router.post("/preview", response_model=ConfigPreview)
    async def preview_endpoint(
        request: ConfigChangeRequest, principal: Principal = Depends(get_principal)
    ):
        _require_scope(principal, ADMIN_CONFIG_WRITE_SCOPE)
        return governance.preview(
            cfg,
            principal,
            connections_yaml=request.connections_yaml,
            policy_yaml=request.policy_yaml,
            catalog_yaml=request.catalog_yaml,
        )

    @router.post("/versions", response_model=ConfigVersion, status_code=status.HTTP_201_CREATED)
    async def stage_endpoint(
        request: ConfigChangeRequest, principal: Principal = Depends(get_principal)
    ):
        _require_scope(principal, ADMIN_CONFIG_WRITE_SCOPE)
        try:
            return governance.stage(
                cfg,
                principal,
                connections_yaml=request.connections_yaml,
                policy_yaml=request.policy_yaml,
                catalog_yaml=request.catalog_yaml,
                description=request.description,
            )
        except ConfigValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    @router.get("/versions", response_model=List[ConfigVersion])
    async def list_versions_endpoint(principal: Principal = Depends(get_principal)):
        _require_scope(principal, ADMIN_CONFIG_READ_SCOPE)
        return governance.list_versions(cfg)

    @router.get("/versions/{version_id}", response_model=ConfigVersion)
    async def get_version_endpoint(version_id: str, principal: Principal = Depends(get_principal)):
        _require_scope(principal, ADMIN_CONFIG_READ_SCOPE)
        try:
            return governance.get_version(version_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    @router.get("/current", response_model=ConfigVersion)
    async def current_endpoint(principal: Principal = Depends(get_principal)):
        _require_scope(principal, ADMIN_CONFIG_READ_SCOPE)
        return governance.get_current(cfg)

    @router.post("/versions/{version_id}/apply", response_model=ApplyResult)
    async def apply_endpoint(version_id: str, principal: Principal = Depends(get_principal)):
        _require_scope(principal, ADMIN_CONFIG_WRITE_SCOPE)
        try:
            version, reload_result = await governance.apply(cfg, principal, version_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
        except ConfigValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        return ApplyResult(version=version, reload=reload_result)

    return router
