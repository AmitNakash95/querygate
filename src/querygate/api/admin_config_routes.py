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
from starlette.concurrency import run_in_threadpool

from querygate.api._errors import require_scope
from querygate.admin import service as governance
from querygate.admin import templates as policy_templates
from querygate.admin.models import (
    CandidatePolicySimulation,
    CandidatePolicySimulationRequest,
    ConfigApprovalDecision,
    ConfigChangeSetBundle,
    ConfigChangeSetImportCheck,
    ConfigPreview,
    ConfigSemanticDiffRequest,
    ConfigVersion,
    DraftSummary,
    PolicyBlastRadiusReport,
    PolicyTemplateRenderRequest,
    PolicyTemplateRenderResult,
    PolicyTemplateSummary,
    TemplateSchemaCheckResult,
    SemanticAccessDiff,
)
from querygate.config_reload import ReloadResult
from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.exceptions import (
    ConfigValidationError,
    NotFoundError,
    PolicyViolationError,
    ServiceDisabledError,
)
from querygate.core.scopes import (
    ADMIN_CONFIG_APPROVE_SCOPE,
    ADMIN_CONFIG_READ_SCOPE,
    ADMIN_CONFIG_WRITE_SCOPE,
)


class ConfigChangeRequest(pyd.BaseModel):
    """A field left unset inherits unchanged from the current active version
    — only send the file(s) that actually change.
    """

    connections_yaml: Optional[str] = None
    policy_yaml: Optional[str] = None
    catalog_yaml: Optional[str] = None
    templates_yaml: Optional[str] = None
    description: Optional[str] = None


class TemplateSchemaCheckRequest(pyd.BaseModel):
    """Draft templates to check against live schema; unset inherits the active
    version's templates."""

    templates_yaml: Optional[str] = None


class ValidationResult(pyd.BaseModel):
    valid: bool
    errors: List[str] = pyd.Field(default_factory=list)


class ApplyResult(pyd.BaseModel):
    version: ConfigVersion
    reload: ReloadResult


class ReviewRequest(pyd.BaseModel):
    """Body for a four-eyes approve/reject (item 42). Only a bounded free-text
    note — the decision itself is the endpoint."""

    note: Optional[str] = pyd.Field(default=None, max_length=500)

    model_config = pyd.ConfigDict(extra="forbid")


def build_admin_config_router(
    get_principal: Callable[..., Principal], cfg: AppConfig, prefix: str = "/api/v1"
) -> APIRouter:
    router = APIRouter(prefix=f"{prefix}/admin/config")

    @router.post("/validate", response_model=ValidationResult)
    async def validate_endpoint(
        request: ConfigChangeRequest, principal: Principal = Depends(get_principal)
    ):
        require_scope(principal, ADMIN_CONFIG_WRITE_SCOPE)
        errors = governance.validate(
            cfg,
            principal,
            connections_yaml=request.connections_yaml,
            policy_yaml=request.policy_yaml,
            catalog_yaml=request.catalog_yaml,
            templates_yaml=request.templates_yaml,
        )
        return ValidationResult(valid=not errors, errors=errors)

    @router.post("/preview", response_model=ConfigPreview)
    async def preview_endpoint(
        request: ConfigChangeRequest, principal: Principal = Depends(get_principal)
    ):
        require_scope(principal, ADMIN_CONFIG_WRITE_SCOPE)
        return governance.preview(
            cfg,
            principal,
            connections_yaml=request.connections_yaml,
            policy_yaml=request.policy_yaml,
            catalog_yaml=request.catalog_yaml,
            templates_yaml=request.templates_yaml,
        )

    @router.post("/check-template-schema", response_model=TemplateSchemaCheckResult)
    async def check_template_schema_endpoint(
        request: TemplateSchemaCheckRequest,
        principal: Principal = Depends(get_principal),
    ):
        # Reveals live column/table existence (read-like) while resolving
        # caller-supplied template content (write-like), so it requires both
        # config scopes — the same reasoning as /simulate and /diff.
        require_scope(principal, ADMIN_CONFIG_READ_SCOPE)
        require_scope(principal, ADMIN_CONFIG_WRITE_SCOPE)
        return await governance.check_template_schema(cfg, principal, request.templates_yaml)

    @router.post("/simulate", response_model=CandidatePolicySimulation)
    async def simulate_candidate_endpoint(
        request: CandidatePolicySimulationRequest,
        principal: Principal = Depends(get_principal),
    ):
        # Candidate simulation returns semantic policy details, so config-read
        # is required. It also resolves caller-supplied config/secret
        # references, so config-write is independently required to prevent a
        # read-only principal from turning it into a secret-existence oracle.
        require_scope(principal, ADMIN_CONFIG_READ_SCOPE)
        require_scope(principal, ADMIN_CONFIG_WRITE_SCOPE)
        try:
            return await run_in_threadpool(
                governance.simulate_candidate_policy, cfg, principal, request
            )
        except ConfigValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))

    @router.post("/diff", response_model=SemanticAccessDiff)
    async def diff_endpoint(
        request: ConfigSemanticDiffRequest,
        principal: Principal = Depends(get_principal),
    ):
        # Like /simulate, the semantic diff returns resolved policy detail
        # (config-read) while resolving caller-supplied config/secret references
        # (config-write). Requiring both prevents a read-only principal from
        # using a candidate document as a secret-existence oracle.
        require_scope(principal, ADMIN_CONFIG_READ_SCOPE)
        require_scope(principal, ADMIN_CONFIG_WRITE_SCOPE)
        try:
            return await run_in_threadpool(
                governance.diff_candidate_access, cfg, principal, request
            )
        except ConfigValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))

    @router.post("/blast-radius", response_model=PolicyBlastRadiusReport)
    async def blast_radius_endpoint(
        request: ConfigSemanticDiffRequest,
        principal: Principal = Depends(get_principal),
    ):
        # Same request shape and scope reasoning as /diff: it echoes resolved
        # policy detail (config-read) while resolving caller-supplied
        # config/secret references (config-write), now aggregated across every
        # configured principal rather than the connection baseline alone.
        require_scope(principal, ADMIN_CONFIG_READ_SCOPE)
        require_scope(principal, ADMIN_CONFIG_WRITE_SCOPE)
        try:
            return await run_in_threadpool(governance.compute_blast_radius, cfg, principal, request)
        except ConfigValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))

    @router.get("/templates", response_model=List[PolicyTemplateSummary])
    async def list_templates_endpoint(principal: Principal = Depends(get_principal)):
        require_scope(principal, ADMIN_CONFIG_READ_SCOPE)
        return policy_templates.list_templates()

    @router.post("/templates/render", response_model=PolicyTemplateRenderResult)
    async def render_template_endpoint(
        request: PolicyTemplateRenderRequest, principal: Principal = Depends(get_principal)
    ):
        # Renders a patch into the caller's own supplied draft only — it never
        # touches the live registry/policy singletons or the version store —
        # but the result is meant to be staged, so this requires write scope
        # like /validate and /preview rather than read scope like /templates.
        require_scope(principal, ADMIN_CONFIG_WRITE_SCOPE)
        try:
            return policy_templates.render_template(
                request.template_id, request.params, request.policy_yaml
            )
        except ConfigValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))

    @router.post("/export", response_model=ConfigChangeSetBundle)
    async def export_endpoint(
        request: ConfigChangeRequest, principal: Principal = Depends(get_principal)
    ):
        # The bundle echoes only documents the caller submitted (inherited
        # documents are never resolved into it), so this discloses no active
        # config content and needs only write scope, like /preview and /versions.
        require_scope(principal, ADMIN_CONFIG_WRITE_SCOPE)
        return governance.export_change_set(
            cfg,
            principal,
            connections_yaml=request.connections_yaml,
            policy_yaml=request.policy_yaml,
            catalog_yaml=request.catalog_yaml,
            templates_yaml=request.templates_yaml,
            description=request.description,
        )

    @router.post("/import", response_model=ConfigChangeSetImportCheck)
    async def import_endpoint(
        bundle: ConfigChangeSetBundle, principal: Principal = Depends(get_principal)
    ):
        # Validation + drift check only; it never stages or persists. The
        # response is content-free (a change signal, not YAML), so — like
        # /preview — write scope is sufficient. Staging still goes through
        # /versions with the bundle's documents.
        require_scope(principal, ADMIN_CONFIG_WRITE_SCOPE)
        return await run_in_threadpool(governance.import_change_set, cfg, principal, bundle)

    @router.post("/drafts", response_model=DraftSummary, status_code=status.HTTP_201_CREATED)
    async def save_draft_endpoint(
        bundle: ConfigChangeSetBundle, principal: Principal = Depends(get_principal)
    ):
        # Same sensitivity as export/import — a draft may carry a caller-
        # submitted connections document with a literal credential — so this
        # requires write scope, encrypted at rest rather than restricted to
        # what the browser is willing to hold in localStorage.
        require_scope(principal, ADMIN_CONFIG_WRITE_SCOPE)
        try:
            return await run_in_threadpool(governance.save_draft, cfg, principal, bundle)
        except ServiceDisabledError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
        except ConfigValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))

    @router.get("/drafts", response_model=List[DraftSummary])
    async def list_drafts_endpoint(principal: Principal = Depends(get_principal)):
        # Metadata only (id/description/timestamps/contains_connections) — no
        # bundle content is decrypted to serve a list, so read scope suffices,
        # matching GET /versions.
        require_scope(principal, ADMIN_CONFIG_READ_SCOPE)
        try:
            # list_for_principal does real synchronous I/O (a directory scan
            # + manifest reads, plus opportunistic expiry pruning), matching
            # its three sibling drafts endpoints in staying off the event loop.
            return await run_in_threadpool(governance.list_my_drafts, cfg, principal)
        except ServiceDisabledError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))

    @router.get("/drafts/{draft_id}", response_model=ConfigChangeSetBundle)
    async def load_draft_endpoint(draft_id: str, principal: Principal = Depends(get_principal)):
        # Decrypts and returns the caller's own submitted content — the same
        # sensitivity as export's response — so write scope, not read.
        require_scope(principal, ADMIN_CONFIG_WRITE_SCOPE)
        try:
            return await run_in_threadpool(governance.load_draft, cfg, principal, draft_id)
        except ServiceDisabledError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    @router.delete("/drafts/{draft_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_draft_endpoint(draft_id: str, principal: Principal = Depends(get_principal)):
        require_scope(principal, ADMIN_CONFIG_WRITE_SCOPE)
        try:
            await run_in_threadpool(governance.delete_draft, cfg, principal, draft_id)
        except ServiceDisabledError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    @router.post("/versions", response_model=ConfigVersion, status_code=status.HTTP_201_CREATED)
    async def stage_endpoint(
        request: ConfigChangeRequest, principal: Principal = Depends(get_principal)
    ):
        require_scope(principal, ADMIN_CONFIG_WRITE_SCOPE)
        try:
            return governance.stage(
                cfg,
                principal,
                connections_yaml=request.connections_yaml,
                policy_yaml=request.policy_yaml,
                catalog_yaml=request.catalog_yaml,
                templates_yaml=request.templates_yaml,
                description=request.description,
            )
        except ConfigValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))

    @router.get("/versions", response_model=List[ConfigVersion])
    async def list_versions_endpoint(principal: Principal = Depends(get_principal)):
        require_scope(principal, ADMIN_CONFIG_READ_SCOPE)
        return governance.list_versions(cfg)

    @router.get("/versions/{version_id}", response_model=ConfigVersion)
    async def get_version_endpoint(version_id: str, principal: Principal = Depends(get_principal)):
        require_scope(principal, ADMIN_CONFIG_READ_SCOPE)
        try:
            return governance.get_version(version_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    @router.get("/current", response_model=ConfigVersion)
    async def current_endpoint(principal: Principal = Depends(get_principal)):
        require_scope(principal, ADMIN_CONFIG_READ_SCOPE)
        return governance.get_current(cfg)

    @router.post("/versions/{version_id}/apply", response_model=ApplyResult)
    async def apply_endpoint(version_id: str, principal: Principal = Depends(get_principal)):
        require_scope(principal, ADMIN_CONFIG_WRITE_SCOPE)
        try:
            version, reload_result = await governance.apply(cfg, principal, version_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
        except ConfigValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))
        return ApplyResult(version=version, reload=reload_result)

    @router.post("/versions/{version_id}/approve", response_model=ConfigVersion)
    async def approve_endpoint(
        version_id: str,
        request: ReviewRequest = ReviewRequest(),
        principal: Principal = Depends(get_principal),
    ):
        return await _review(version_id, ConfigApprovalDecision.APPROVE, request, principal)

    @router.post("/versions/{version_id}/reject", response_model=ConfigVersion)
    async def reject_endpoint(
        version_id: str,
        request: ReviewRequest = ReviewRequest(),
        principal: Principal = Depends(get_principal),
    ):
        return await _review(version_id, ConfigApprovalDecision.REJECT, request, principal)

    async def _review(
        version_id: str,
        decision: ConfigApprovalDecision,
        request: ReviewRequest,
        principal: Principal,
    ) -> ConfigVersion:
        require_scope(principal, ADMIN_CONFIG_APPROVE_SCOPE)
        try:
            return await run_in_threadpool(
                governance.approve,
                cfg,
                principal,
                version_id,
                decision=decision,
                note=request.note,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
        except PolicyViolationError as exc:
            # author==approver, or the version isn't staged — a state/authorization
            # conflict, not a malformed request.
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    return router
