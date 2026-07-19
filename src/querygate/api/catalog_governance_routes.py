"""REST admin API for catalog governance (TODO.md item 32B-1).

Privileged review/edit/approve/reject/publish/rollback workflow for quarantined
inferred draft proposals (32A-2). Agent-facing catalog retrieval
(`GET /{connection}/catalog/search`, `describe_table`) stays read-only and
principal-filtered in `api/routes.py` — this router never changes that
surface, it only lets an authorized reviewer move a proposal through its
state machine and, on publish, merge it into the catalog every caller
already reads from.

Every mutation goes through the same `CatalogFileRepository` lock 32A's
refresh/generate-drafts already use (see `catalog/governance.py`) — there is
no second catalog file or mutation path here.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Callable, List, Optional

import pydantic as pyd
from fastapi import APIRouter, Depends, HTTPException, Query, status

from querygate.audit.logger import audit_catalog_governance
from querygate.catalog import governance
from querygate.catalog.generation import generate_catalog_drafts
from querygate.catalog.loader import get_catalog_store
from querygate.catalog.models import (
    CatalogDraftContent,
    CatalogDraftProposal,
    CatalogDraftTarget,
    CatalogVersionRecord,
    ProposalReviewStatus,
)
from querygate.catalog.providers import (
    ManualDraftBatch,
    ManualSemanticMemoryProvider,
    SemanticGenerationRequest,
)
from querygate.catalog.repository import CatalogFileRepository, CatalogFileUpdate
from querygate.connections.registry import get_registry
from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.exceptions import CatalogGovernanceError, NotFoundError
from querygate.core.scopes import (
    CATALOG_APPROVE_SCOPE,
    CATALOG_EDIT_SCOPE,
    CATALOG_GENERATE_SCOPE,
    CATALOG_PUBLISH_SCOPE,
    CATALOG_REJECT_SCOPE,
    CATALOG_REVIEW_SCOPE,
    CATALOG_ROLLBACK_SCOPE,
)
from querygate.policy.loader import get_policy


def _require_scope(principal: Principal, scope: str) -> None:
    if scope not in principal.scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=f"Missing required scope: {scope!r}"
        )


def _require_known_connection(connection_id: str) -> None:
    try:
        get_registry().get(connection_id)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown connection: {connection_id!r}"
        )


def _repository(cfg: AppConfig) -> CatalogFileRepository:
    if not cfg.catalog_file:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Catalog governance requires CATALOG_FILE to be configured.",
        )
    return CatalogFileRepository(cfg.catalog_file)


async def _apply(repository: CatalogFileRepository, mutator) -> governance.CatalogGovernanceUpdate:
    """Run a governance mutation off the event loop.

    `CatalogFileRepository` uses a blocking cross-process file lock — the
    same constraint `catalog_cli.py` already works under as a synchronous
    CLI, just wrapped here so a FastAPI async handler never blocks the loop.
    """

    def _run() -> governance.CatalogGovernanceUpdate:
        def _wrapped(store):
            result = mutator(store)
            return CatalogFileUpdate(result.store, result)

        return repository.update(_wrapped)

    return await asyncio.to_thread(_run)


class ProposalListItem(pyd.BaseModel):
    proposal_id: str
    generation_id: str
    target: CatalogDraftTarget
    content: CatalogDraftContent
    review_status: ProposalReviewStatus
    schema_status: str
    published_entry_id: Optional[str] = None
    published_version_id: Optional[str] = None

    model_config = pyd.ConfigDict(extra="forbid")

    @classmethod
    def from_proposal(cls, proposal: CatalogDraftProposal) -> "ProposalListItem":
        return cls(
            proposal_id=proposal.proposal_id,
            generation_id=proposal.generation_id,
            target=proposal.target,
            content=proposal.content,
            review_status=proposal.review_status,
            schema_status=proposal.provenance.status.value,
            published_entry_id=proposal.published_entry_id,
            published_version_id=proposal.published_version_id,
        )


class EditProposalRequest(pyd.BaseModel):
    content: CatalogDraftContent


class RejectRequest(pyd.BaseModel):
    reason: str = pyd.Field(min_length=1, max_length=500)


class GenerateDraftsRequest(pyd.BaseModel):
    batch: ManualDraftBatch
    purpose: str = "onboarding"
    max_proposals: int = pyd.Field(default=100, ge=1, le=200)


class GenerateDraftsResult(pyd.BaseModel):
    generation_id: str
    outcome: str
    added_proposal_count: int


class BulkProposalRequest(pyd.BaseModel):
    proposal_ids: List[str] = pyd.Field(min_length=1, max_length=50)


class BulkRejectRequest(BulkProposalRequest):
    reason: str = pyd.Field(min_length=1, max_length=500)


class BulkResult(pyd.BaseModel):
    outcome: str
    proposal_ids: List[str]


class GovernanceActionResult(pyd.BaseModel):
    outcome: str
    proposal_id: Optional[str] = None
    version_id: Optional[str] = None
    entry_id: Optional[str] = None
    rolled_back_version_id: Optional[str] = None


class CatalogVersionSummary(pyd.BaseModel):
    """Metadata-only projection — field names touched, never their content."""

    version_id: str
    action: str
    status: str
    actor: str
    occurred_at: datetime
    proposal_id: Optional[str] = None
    rolled_back_version_id: Optional[str] = None
    object_type: str
    table: str
    column: Optional[str] = None
    to_table: Optional[str] = None
    to_column: Optional[str] = None
    fields_changed: List[str]

    @classmethod
    def from_record(cls, record: CatalogVersionRecord) -> "CatalogVersionSummary":
        change = record.changes[0]
        content = change.after or change.before
        fields: List[str] = []
        if content is not None:
            if content.description is not None:
                fields.append("description")
            if content.aliases:
                fields.append("aliases")
            if content.default_aggregation is not None:
                fields.append("default_aggregation")
        return cls(
            version_id=record.version_id,
            action=record.action.value,
            status=record.status.value,
            actor=record.actor,
            occurred_at=record.occurred_at,
            proposal_id=record.proposal_id,
            rolled_back_version_id=record.rolled_back_version_id,
            object_type=change.target.object_type.value,
            table=change.target.table,
            column=change.target.column,
            to_table=change.target.to_table,
            to_column=change.target.to_column,
            fields_changed=fields,
        )


def build_catalog_governance_router(
    get_principal: Callable[..., Principal], cfg: AppConfig, prefix: str = "/api/v1"
) -> APIRouter:
    router = APIRouter(prefix=f"{prefix}/admin/catalog")

    async def _run_mutation(
        *,
        action: str,
        scope: str,
        connection: str,
        principal: Principal,
        proposal_id: Optional[str],
        mutator,
    ) -> governance.CatalogGovernanceUpdate:
        _require_scope(principal, scope)
        _require_known_connection(connection)
        start = time.monotonic()
        repository = _repository(cfg)
        try:
            update = await _apply(repository, mutator)
        except (NotFoundError, CatalogGovernanceError) as exc:
            audit_catalog_governance(
                action=action,
                outcome="rejected",
                principal=principal.subject,
                principal_scopes=sorted(principal.scopes),
                auth_method=principal.auth_method,
                connection_id=connection,
                proposal_id=proposal_id,
                error_category="not_found" if isinstance(exc, NotFoundError) else "validation",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
            status_code = (
                status.HTTP_404_NOT_FOUND
                if isinstance(exc, NotFoundError)
                else status.HTTP_409_CONFLICT
            )
            raise HTTPException(status_code=status_code, detail=str(exc))
        audit_catalog_governance(
            action=action,
            outcome="success",
            principal=principal.subject,
            principal_scopes=sorted(principal.scopes),
            auth_method=principal.auth_method,
            connection_id=connection,
            proposal_id=update.proposal_id or proposal_id,
            proposal_count=len(update.proposal_ids) if update.proposal_ids else None,
            version_id=update.version_id,
            entry_id=update.entry_id,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        return update

    @router.post(
        "/{connection}/generate-drafts",
        response_model=GenerateDraftsResult,
        status_code=status.HTTP_201_CREATED,
    )
    async def generate_drafts_endpoint(
        connection: str,
        request: GenerateDraftsRequest,
        principal: Principal = Depends(get_principal),
    ):
        _require_scope(principal, CATALOG_GENERATE_SCOPE)
        _require_known_connection(connection)
        if request.batch.connection_id != connection:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="batch.connection_id must match the connection in the URL",
            )
        start = time.monotonic()
        repository = _repository(cfg)

        def _generate(store):
            snapshot = store.get_schema_snapshot(connection)
            if snapshot is None:
                raise CatalogGovernanceError(
                    "draft generation requires a persisted schema snapshot; run refresh first"
                )
            gen_request = SemanticGenerationRequest(
                generation_id=request.batch.generation_id,
                connection_id=connection,
                snapshot=snapshot,
                purpose=request.purpose,
                max_proposals=request.max_proposals,
            )
            return generate_catalog_drafts(
                store, request=gen_request, provider=ManualSemanticMemoryProvider(request.batch)
            )

        try:
            update = await _apply(repository, _generate)
        except (ValueError, CatalogGovernanceError) as exc:
            audit_catalog_governance(
                action="generate",
                outcome="rejected",
                principal=principal.subject,
                principal_scopes=sorted(principal.scopes),
                auth_method=principal.auth_method,
                connection_id=connection,
                error_category="validation",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

        audit_catalog_governance(
            action="generate",
            outcome="success",
            principal=principal.subject,
            principal_scopes=sorted(principal.scopes),
            auth_method=principal.auth_method,
            connection_id=connection,
            proposal_count=update.added_count,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        return GenerateDraftsResult(
            generation_id=update.generation_id,
            outcome=update.outcome,
            added_proposal_count=update.added_count,
        )

    @router.get("/{connection}/proposals", response_model=List[ProposalListItem])
    async def list_proposals_endpoint(
        connection: str,
        review_status: Optional[ProposalReviewStatus] = Query(default=None),
        principal: Principal = Depends(get_principal),
    ):
        _require_scope(principal, CATALOG_REVIEW_SCOPE)
        _require_known_connection(connection)
        store = get_catalog_store()
        proposals = list(store.iter_draft_proposals(connection))
        if review_status is not None:
            proposals = [p for p in proposals if p.review_status == review_status]
        return [ProposalListItem.from_proposal(p) for p in proposals]

    @router.get("/{connection}/proposals/{proposal_id}", response_model=ProposalListItem)
    async def get_proposal_endpoint(
        connection: str, proposal_id: str, principal: Principal = Depends(get_principal)
    ):
        _require_scope(principal, CATALOG_REVIEW_SCOPE)
        _require_known_connection(connection)
        store = get_catalog_store()
        proposal = store.get_draft_proposal(proposal_id)
        if proposal is None or proposal.target.connection_id != connection:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown catalog proposal: {proposal_id!r}",
            )
        return ProposalListItem.from_proposal(proposal)

    @router.get(
        "/{connection}/proposals/{proposal_id}/preview",
        response_model=governance.ProposalPublishPreview,
    )
    async def preview_publish_endpoint(
        connection: str,
        proposal_id: str,
        principal_subject: str = Query(min_length=1, max_length=200),
        principal: Principal = Depends(get_principal),
    ):
        _require_scope(principal, CATALOG_REVIEW_SCOPE)
        _require_known_connection(connection)
        store = get_catalog_store()
        test_policy = get_policy(connection, principal=Principal(subject=principal_subject))
        try:
            return governance.preview_publish(
                store, proposal_id=proposal_id, connection_id=connection, policy=test_policy
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    @router.patch("/{connection}/proposals/{proposal_id}", response_model=GovernanceActionResult)
    async def edit_proposal_endpoint(
        connection: str,
        proposal_id: str,
        request: EditProposalRequest,
        principal: Principal = Depends(get_principal),
    ):
        update = await _run_mutation(
            action="edit",
            scope=CATALOG_EDIT_SCOPE,
            connection=connection,
            principal=principal,
            proposal_id=proposal_id,
            mutator=lambda store: governance.edit_proposal(
                store,
                proposal_id=proposal_id,
                connection_id=connection,
                actor=principal.subject,
                content=request.content,
            ),
        )
        return GovernanceActionResult(outcome=update.outcome, proposal_id=update.proposal_id)

    @router.post(
        "/{connection}/proposals/{proposal_id}/approve", response_model=GovernanceActionResult
    )
    async def approve_proposal_endpoint(
        connection: str, proposal_id: str, principal: Principal = Depends(get_principal)
    ):
        update = await _run_mutation(
            action="approve",
            scope=CATALOG_APPROVE_SCOPE,
            connection=connection,
            principal=principal,
            proposal_id=proposal_id,
            mutator=lambda store: governance.approve_proposal(
                store, proposal_id=proposal_id, connection_id=connection, actor=principal.subject
            ),
        )
        return GovernanceActionResult(outcome=update.outcome, proposal_id=update.proposal_id)

    @router.post(
        "/{connection}/proposals/{proposal_id}/reject", response_model=GovernanceActionResult
    )
    async def reject_proposal_endpoint(
        connection: str,
        proposal_id: str,
        request: RejectRequest,
        principal: Principal = Depends(get_principal),
    ):
        update = await _run_mutation(
            action="reject",
            scope=CATALOG_REJECT_SCOPE,
            connection=connection,
            principal=principal,
            proposal_id=proposal_id,
            mutator=lambda store: governance.reject_proposal(
                store,
                proposal_id=proposal_id,
                connection_id=connection,
                actor=principal.subject,
                reason=request.reason,
            ),
        )
        return GovernanceActionResult(outcome=update.outcome, proposal_id=update.proposal_id)

    @router.post("/{connection}/proposals/bulk-approve", response_model=BulkResult)
    async def bulk_approve_endpoint(
        connection: str,
        request: BulkProposalRequest,
        principal: Principal = Depends(get_principal),
    ):
        proposal_ids = tuple(request.proposal_ids)
        update = await _run_mutation(
            action="bulk_approve",
            scope=CATALOG_APPROVE_SCOPE,
            connection=connection,
            principal=principal,
            proposal_id=None,
            mutator=lambda store: governance.bulk_approve_proposals(
                store, proposal_ids=proposal_ids, connection_id=connection, actor=principal.subject
            ),
        )
        return BulkResult(outcome=update.outcome, proposal_ids=list(update.proposal_ids))

    @router.post("/{connection}/proposals/bulk-reject", response_model=BulkResult)
    async def bulk_reject_endpoint(
        connection: str,
        request: BulkRejectRequest,
        principal: Principal = Depends(get_principal),
    ):
        proposal_ids = tuple(request.proposal_ids)
        update = await _run_mutation(
            action="bulk_reject",
            scope=CATALOG_REJECT_SCOPE,
            connection=connection,
            principal=principal,
            proposal_id=None,
            mutator=lambda store: governance.bulk_reject_proposals(
                store,
                proposal_ids=proposal_ids,
                connection_id=connection,
                actor=principal.subject,
                reason=request.reason,
            ),
        )
        return BulkResult(outcome=update.outcome, proposal_ids=list(update.proposal_ids))

    @router.post(
        "/{connection}/proposals/{proposal_id}/publish", response_model=GovernanceActionResult
    )
    async def publish_proposal_endpoint(
        connection: str, proposal_id: str, principal: Principal = Depends(get_principal)
    ):
        update = await _run_mutation(
            action="publish",
            scope=CATALOG_PUBLISH_SCOPE,
            connection=connection,
            principal=principal,
            proposal_id=proposal_id,
            mutator=lambda store: governance.publish_proposal(
                store, proposal_id=proposal_id, connection_id=connection, actor=principal.subject
            ),
        )
        return GovernanceActionResult(
            outcome=update.outcome,
            proposal_id=update.proposal_id,
            version_id=update.version_id,
            entry_id=update.entry_id,
        )

    @router.get("/{connection}/versions", response_model=List[CatalogVersionSummary])
    async def list_versions_endpoint(
        connection: str, principal: Principal = Depends(get_principal)
    ):
        _require_scope(principal, CATALOG_REVIEW_SCOPE)
        _require_known_connection(connection)
        store = get_catalog_store()
        return [
            CatalogVersionSummary.from_record(r) for r in store.iter_version_history(connection)
        ]

    @router.get("/{connection}/versions/{version_id}", response_model=CatalogVersionRecord)
    async def get_version_endpoint(
        connection: str, version_id: str, principal: Principal = Depends(get_principal)
    ):
        _require_scope(principal, CATALOG_REVIEW_SCOPE)
        _require_known_connection(connection)
        store = get_catalog_store()
        record = store.get_version_record(version_id)
        if record is None or record.changes[0].target.connection_id != connection:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown catalog version: {version_id!r}",
            )
        return record

    @router.post(
        "/{connection}/versions/{version_id}/rollback", response_model=GovernanceActionResult
    )
    async def rollback_version_endpoint(
        connection: str, version_id: str, principal: Principal = Depends(get_principal)
    ):
        update = await _run_mutation(
            action="rollback",
            scope=CATALOG_ROLLBACK_SCOPE,
            connection=connection,
            principal=principal,
            proposal_id=None,
            mutator=lambda store: governance.rollback_version(
                store, version_id=version_id, connection_id=connection, actor=principal.subject
            ),
        )
        return GovernanceActionResult(
            outcome=update.outcome,
            version_id=update.version_id,
            rolled_back_version_id=update.rolled_back_version_id,
        )

    return router
