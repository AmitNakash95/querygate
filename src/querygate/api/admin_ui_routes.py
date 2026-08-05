"""Support APIs for the browser-based admin control plane.

The UI deliberately builds on the existing config-governance and schema
routes.  This module only supplies the capabilities that did not already
exist: parsing/rendering a policy document for the visual designer,
principal-safe policy simulation, and bounded browsing of the redaction-safe
persisted audit stream.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional

import pydantic as pyd
import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, status

from querygate.api._errors import require_scope
from querygate.audit.events import PersistableEvent
from querygate.audit.file_reader import AuditFileReadBounded, iter_lines_reverse
from querygate.audit.ledger import resolve_ledger_key, unwrap_envelope, verify_envelope_hash
from querygate.connections.registry import get_registry
from querygate.core.auth import Principal
from querygate.core.config import AppConfig, AuditSinkBackend
from querygate.core.exceptions import PolicyViolationError
from querygate.core.scopes import ADMIN_CONFIG_READ_SCOPE, ADMIN_CONFIG_WRITE_SCOPE
from querygate.policy.loader import PolicyStore, get_policy_store
from querygate.policy.models import GUARDRAIL_FIELDS
from querygate.templates.models import QueryTemplateFile

_AUDIT_EVENT_ADAPTER = pyd.TypeAdapter(PersistableEvent)
_AUDIT_EVENT_TYPES = frozenset(
    {"query.execution", "config.governance", "catalog.governance", "connection.probe"}
)


class PolicyDocumentRequest(pyd.BaseModel):
    policy_yaml: str

    model_config = pyd.ConfigDict(extra="forbid")


class PolicyDocumentResponse(pyd.BaseModel):
    document: Dict[str, Any]

    model_config = pyd.ConfigDict(extra="forbid")


class PolicyRenderRequest(pyd.BaseModel):
    document: Dict[str, Any]

    model_config = pyd.ConfigDict(extra="forbid")


class PolicyRenderResponse(pyd.BaseModel):
    policy_yaml: str

    model_config = pyd.ConfigDict(extra="forbid")


# TODO.md item 87: structured query-template authoring. Mirrors the policy
# parse/render pair above — the Templates domain form composes a validated
# QueryTemplate and merges it into the draft templates.yaml change-set
# document, which then flows through the same validate → stage → apply →
# rollback as any other config document (no new store).
class TemplateDocumentRequest(pyd.BaseModel):
    templates_yaml: str

    model_config = pyd.ConfigDict(extra="forbid")


class TemplateDocumentResponse(pyd.BaseModel):
    document: Dict[str, Any]

    model_config = pyd.ConfigDict(extra="forbid")


class TemplateRenderRequest(pyd.BaseModel):
    document: Dict[str, Any]

    model_config = pyd.ConfigDict(extra="forbid")


class TemplateRenderResponse(pyd.BaseModel):
    templates_yaml: str

    model_config = pyd.ConfigDict(extra="forbid")


class PolicyTestRequest(pyd.BaseModel):
    principal: str = pyd.Field(min_length=1, max_length=256)
    connection: str = pyd.Field(min_length=1, max_length=256)
    table: Optional[str] = pyd.Field(default=None, max_length=256)
    columns: List[str] = pyd.Field(default_factory=list, max_length=100)
    claims: Dict[str, Any] = pyd.Field(default_factory=dict)

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.field_validator("principal", "connection")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @pyd.field_validator("table")
    @classmethod
    def _strip_optional(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return value.strip() or None

    @pyd.field_validator("columns")
    @classmethod
    def _normalize_columns(cls, values: List[str]) -> List[str]:
        normalized = [value.strip() for value in values if value.strip()]
        if len(set(value.casefold() for value in normalized)) != len(normalized):
            raise ValueError("columns must not contain duplicates")
        return normalized

    @pyd.field_validator("claims")
    @classmethod
    def _scalar_claims_only(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        for name, claim in value.items():
            if not name.strip():
                raise ValueError("claim names must not be blank")
            if isinstance(claim, (dict, list)):
                raise ValueError("claim values must be scalar")
        return value


class PolicyColumnDecision(pyd.BaseModel):
    column: str
    allowed: bool

    model_config = pyd.ConfigDict(extra="forbid")


class MandatoryFilterDecision(pyd.BaseModel):
    table: str
    column: str
    source: str
    satisfied: bool

    model_config = pyd.ConfigDict(extra="forbid")


class PolicyTestResponse(pyd.BaseModel):
    principal: str
    connection: str
    table: Optional[str]
    allowed: bool
    profile_enabled: bool
    policy_enabled: bool
    table_allowed: Optional[bool]
    columns: List[PolicyColumnDecision]
    mandatory_filters: List[MandatoryFilterDecision]
    reasons: List[str]
    guardrails: Dict[str, Any]

    model_config = pyd.ConfigDict(extra="forbid")


class AuditEventPage(pyd.BaseModel):
    # TODO.md item 137: "jsonl"/"jsonl_chained" discloses the actually
    # configured backend rather than always reporting "jsonl".
    source: Literal["jsonl", "jsonl_chained", "disabled", "empty"]
    events: List[Dict[str, Any]]
    total: int = pyd.Field(ge=0)
    malformed: int = pyd.Field(ge=0)
    next_cursor: Optional[int] = pyd.Field(default=None, ge=0)
    # TODO.md item 138: True when the underlying scan stopped at its
    # `audit_page_max_lines_read` bound before it could be sure no more
    # matching lines remained — `total` (and therefore pagination) may be
    # incomplete as a result.
    truncated: bool = False

    model_config = pyd.ConfigDict(extra="forbid")


def _require_any_scope(principal: Principal, *scopes: str) -> None:
    if not any(scope in principal.scopes for scope in scopes):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing required scope; expected one of: {', '.join(repr(s) for s in scopes)}",
        )


def _policy_document(policy_yaml: str) -> Dict[str, Any]:
    try:
        raw = yaml.safe_load(policy_yaml) or {}
    except yaml.YAMLError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid policy YAML: {exc}",
        ) from exc
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Policy document root must be a mapping.",
        )
    try:
        PolicyStore.from_dict(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid policy document: {exc}",
        ) from exc
    return raw


def _template_document(templates_yaml: str) -> Dict[str, Any]:
    """Parse + validate a templates.yaml body against `QueryTemplateFile`.

    Returns the raw (validated) mapping unchanged — the schema authority is the
    same model the loader/dry-run uses, so a template that passes here passes
    staging. Mirrors `_policy_document`.
    """
    try:
        raw = yaml.safe_load(templates_yaml) or {}
    except yaml.YAMLError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid templates YAML: {exc}",
        ) from exc
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Templates document root must be a mapping.",
        )
    try:
        QueryTemplateFile.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid templates document: {exc}",
        ) from exc
    return raw


def _guardrail_summary(policy: Any) -> Dict[str, Any]:
    # Derived from Policy (TODO.md item 115), not hand-listed: this panel had
    # the shortest of the four copies, missing even max_top_n/max_partition_by.
    fields = GUARDRAIL_FIELDS
    return {
        field: (
            getattr(policy, field).value
            if hasattr(getattr(policy, field), "value")
            else getattr(policy, field)
        )
        for field in fields
    }


def _test_policy(request: PolicyTestRequest) -> PolicyTestResponse:
    registry = get_registry()
    try:
        profile = registry.get(request.connection)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown connection: {request.connection!r}",
        ) from exc

    simulated = Principal(
        subject=request.principal,
        claims=request.claims,
        auth_method="admin_simulation",
    )
    policy = get_policy_store().get(request.connection, principal=simulated)
    reasons: List[str] = []
    if not profile.enabled:
        reasons.append("The connection profile is disabled.")
    if not policy.enabled:
        reasons.append("The effective principal policy disables this connection.")

    table_allowed: Optional[bool] = None
    if request.table is not None:
        table_allowed = policy.table_allowed(request.table)
        if not table_allowed:
            reasons.append(f"Table {request.table!r} is denied by the effective policy.")

    columns: List[PolicyColumnDecision] = []
    if request.columns and request.table is None:
        reasons.append("Choose a table before evaluating columns.")
    for column in request.columns:
        allowed = request.table is not None and policy.column_allowed(request.table, column)
        columns.append(PolicyColumnDecision(column=column, allowed=allowed))
        if request.table is not None and not allowed:
            reasons.append(f"Column {request.table}.{column} is denied by the effective policy.")

    filter_decisions: List[MandatoryFilterDecision] = []
    for row_filter in policy.mandatory_row_filters:
        # `.casefold()`, not `.lower()` (TODO.md item 150): `policy.table_allowed`/
        # `column_allowed` a few lines above already casefold, so this simulator's
        # mandatory-filter match must too, or it can report a simulated `allowed=True`
        # verdict for a table/filter pair real execution would actually reject.
        if request.table is not None and row_filter.table.casefold() != request.table.casefold():
            continue
        satisfied = True
        try:
            row_filter.resolve(simulated)
        except PolicyViolationError:
            satisfied = False
            reasons.append(
                f"Mandatory filter {row_filter.table}.{row_filter.column} requires claim "
                f"{row_filter.from_claim!r}."
            )
        filter_decisions.append(
            MandatoryFilterDecision(
                table=row_filter.table,
                column=row_filter.column,
                source=(
                    f"claim:{row_filter.from_claim}"
                    if row_filter.from_claim is not None
                    else "configured_literal:redacted"
                ),
                satisfied=satisfied,
            )
        )

    allowed = (
        profile.enabled
        and policy.enabled
        and (table_allowed is not False)
        and (not request.columns or request.table is not None)
        and all(item.allowed for item in columns)
        and all(item.satisfied for item in filter_decisions)
    )
    if allowed:
        reasons.append("The active policy permits this simulated access.")

    return PolicyTestResponse(
        principal=request.principal,
        connection=request.connection,
        table=request.table,
        allowed=allowed,
        profile_enabled=profile.enabled,
        policy_enabled=policy.enabled,
        table_allowed=table_allowed,
        columns=columns,
        mandatory_filters=filter_decisions,
        reasons=reasons,
        guardrails=_guardrail_summary(policy),
    )


def _audit_page(
    cfg: AppConfig,
    *,
    cursor: int,
    limit: int,
    event_type: Optional[str],
    outcome: Optional[str],
    principal: Optional[str],
    connection: Optional[str],
    action: Optional[str],
) -> AuditEventPage:
    if not cfg.audit_sink_backend.is_locally_readable():
        return AuditEventPage(source="disabled", events=[], total=0, malformed=0)

    path = Path(cfg.audit_jsonl_path)
    if not path.exists():
        return AuditEventPage(source="empty", events=[], total=0, malformed=0)

    # Scan tail-first (TODO.md item 138): since the sink only ever appends,
    # the physically newest lines are what a newest-first page needs, so a
    # hard cap on lines read (`audit_page_max_lines_read`) bounds worst-case
    # parse/validate work without risking never reaching a recent page the
    # way capping a forward scan from the start of the file would. Only the
    # first `cursor + limit` matches are retained — enough to serve this
    # page — but every match within the line-read bound is still counted
    # toward `total`.
    ledger_key = resolve_ledger_key(cfg.audit_ledger_hmac_key)
    # TODO.md item 137: on jsonl_chained, every persisted line MUST be a
    # chain envelope, so a bare (non-enveloped) line is itself evidence of
    # tampering/corruption, not a legitimate plain-jsonl line (found by
    # `security-invariant-reviewer`, 2026-08-05).
    require_envelope = cfg.audit_sink_backend == AuditSinkBackend.JSONL_CHAINED
    matches: List[Dict[str, Any]] = []
    total = 0
    malformed = 0
    lines_read = 0
    truncated = False
    try:
        for line in iter_lines_reverse(path):
            if lines_read >= cfg.audit_page_max_lines_read:
                truncated = True
                break
            lines_read += 1
            try:
                raw = json.loads(line)
                verified = verify_envelope_hash(raw, key=ledger_key)
                if verified is False or (verified is None and require_envelope):
                    # TODO.md item 137: a chain envelope whose own hash
                    # doesn't match its contents, or (require_envelope) a
                    # bare line on a backend where every line must be
                    # enveloped — never display either as clean.
                    malformed += 1
                    continue
                raw = unwrap_envelope(raw)
                event = _AUDIT_EVENT_ADAPTER.validate_python(raw)
                item = event.model_dump(mode="json", exclude_none=True)
            except (json.JSONDecodeError, pyd.ValidationError, TypeError):
                malformed += 1
                continue
            if event_type is not None and item.get("event_type") != event_type:
                continue
            if outcome is not None and item.get("outcome") != outcome:
                continue
            if principal is not None and item.get("principal_id") != principal:
                continue
            if connection is not None and item.get("connection_id") != connection:
                continue
            if action is not None and item.get("action") != action:
                continue
            total += 1
            if len(matches) < cursor + limit:
                matches.append(item)
    except AuditFileReadBounded:
        truncated = True

    events = matches[cursor : cursor + limit]
    next_cursor = cursor + len(events) if total > cursor + len(events) else None
    return AuditEventPage(
        source=cfg.audit_sink_backend.value,
        events=events,
        total=total,
        malformed=malformed,
        next_cursor=next_cursor,
        truncated=truncated,
    )


def build_admin_ui_router(
    get_principal: Callable[..., Principal], cfg: AppConfig, prefix: str = "/api/v1"
) -> APIRouter:
    router = APIRouter(prefix=f"{prefix}/admin/ui", tags=["admin-ui"])

    @router.post("/policy/parse", response_model=PolicyDocumentResponse)
    async def parse_policy(
        request: PolicyDocumentRequest,
        principal: Principal = Depends(get_principal),
    ):
        # A read-only administrator may inspect a document they are already
        # authorized to fetch; a write-only administrator may parse a
        # proposal they supplied.  The endpoint never loads current content
        # on behalf of either caller.
        _require_any_scope(principal, ADMIN_CONFIG_READ_SCOPE, ADMIN_CONFIG_WRITE_SCOPE)
        return PolicyDocumentResponse(document=_policy_document(request.policy_yaml))

    @router.post("/policy/render", response_model=PolicyRenderResponse)
    async def render_policy(
        request: PolicyRenderRequest,
        principal: Principal = Depends(get_principal),
    ):
        require_scope(principal, ADMIN_CONFIG_WRITE_SCOPE)
        document = _policy_document(
            yaml.safe_dump(request.document, sort_keys=False, allow_unicode=True)
        )
        return PolicyRenderResponse(
            policy_yaml=yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
        )

    @router.post("/templates/parse", response_model=TemplateDocumentResponse)
    async def parse_templates(
        request: TemplateDocumentRequest,
        principal: Principal = Depends(get_principal),
    ):
        # Same read-or-write posture as policy/parse: parsing a document the
        # caller supplied never loads current content on their behalf.
        _require_any_scope(principal, ADMIN_CONFIG_READ_SCOPE, ADMIN_CONFIG_WRITE_SCOPE)
        return TemplateDocumentResponse(document=_template_document(request.templates_yaml))

    @router.post("/templates/render", response_model=TemplateRenderResponse)
    async def render_templates(
        request: TemplateRenderRequest,
        principal: Principal = Depends(get_principal),
    ):
        require_scope(principal, ADMIN_CONFIG_WRITE_SCOPE)
        document = _template_document(
            yaml.safe_dump(request.document, sort_keys=False, allow_unicode=True)
        )
        return TemplateRenderResponse(
            templates_yaml=yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
        )

    @router.post("/policy/test", response_model=PolicyTestResponse)
    async def test_policy(
        request: PolicyTestRequest,
        principal: Principal = Depends(get_principal),
    ):
        require_scope(principal, ADMIN_CONFIG_READ_SCOPE)
        return _test_policy(request)

    @router.get("/audit/events", response_model=AuditEventPage)
    async def browse_audit_events(
        # TODO.md item 140: the old ceiling (1_000_000) let one request
        # allocate on the order of a gigabyte of fully-parsed event dicts
        # before `_audit_page` ever slices its response page. Lowered rather
        # than redesigning the pagination shape (recorded in
        # docs/PRODUCT_GUIDE.md's Decision Log) — 5,000 pages of `limit=50`
        # covers far deeper manual "load more" paging than any real admin UI
        # session reaches, while bounding worst-case retained dicts per
        # request to cursor + limit (~5,100), several orders of magnitude
        # below the old bound.
        cursor: int = Query(default=0, ge=0, le=5_000),
        limit: int = Query(default=50, ge=1, le=100),
        event_type: Optional[str] = Query(default=None),
        outcome: Optional[Literal["success", "rejected"]] = Query(default=None),
        principal_id: Optional[str] = Query(default=None, max_length=256),
        connection_id: Optional[str] = Query(default=None, max_length=256),
        action: Optional[str] = Query(default=None, max_length=64),
        principal: Principal = Depends(get_principal),
    ):
        require_scope(principal, ADMIN_CONFIG_READ_SCOPE)
        if event_type is not None and event_type not in _AUDIT_EVENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unsupported audit event type: {event_type!r}",
            )
        return _audit_page(
            cfg,
            cursor=cursor,
            limit=limit,
            event_type=event_type,
            outcome=outcome,
            principal=principal_id,
            connection=connection_id,
            action=action,
        )

    return router
