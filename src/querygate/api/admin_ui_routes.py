"""Support APIs for the browser-based admin control plane.

The UI deliberately builds on the existing config-governance and schema
routes.  This module only supplies the capabilities that did not already
exist: parsing/rendering a policy document for the visual designer,
principal-safe policy simulation, and bounded browsing of the redaction-safe
persisted audit stream.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional

import pydantic as pyd
import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, status

from querygate.audit.events import PersistableEvent
from querygate.connections.registry import get_registry
from querygate.core.auth import Principal
from querygate.core.config import AppConfig, AuditSinkBackend
from querygate.core.exceptions import PolicyViolationError
from querygate.core.scopes import ADMIN_CONFIG_READ_SCOPE, ADMIN_CONFIG_WRITE_SCOPE
from querygate.policy.loader import PolicyStore, get_policy_store


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
        if len(set(value.lower() for value in normalized)) != len(normalized):
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
    source: Literal["jsonl", "disabled", "empty"]
    events: List[Dict[str, Any]]
    total: int = pyd.Field(ge=0)
    malformed: int = pyd.Field(ge=0)
    next_cursor: Optional[int] = pyd.Field(default=None, ge=0)

    model_config = pyd.ConfigDict(extra="forbid")


def _require_scope(principal: Principal, scope: str) -> None:
    if scope not in principal.scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing required scope: {scope!r}",
        )


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
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid policy YAML: {exc}",
        ) from exc
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Policy document root must be a mapping.",
        )
    try:
        PolicyStore.from_dict(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid policy document: {exc}",
        ) from exc
    return raw


def _guardrail_summary(policy: Any) -> Dict[str, Any]:
    fields = (
        "max_joins",
        "max_select_columns",
        "max_where_depth",
        "max_group_by",
        "max_limit",
        "max_limit_aggregate",
        "default_limit",
        "max_batch_size",
        "max_response_bytes",
        "timeout_seconds",
        "max_concurrency",
        "concurrency_wait_seconds",
        "max_queue_depth",
        "max_queue_depth_per_principal",
        "max_estimated_rows",
        "max_estimated_cost",
        "cost_estimation_mode",
    )
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
        if request.table is not None and row_filter.table.lower() != request.table.lower():
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
    if cfg.audit_sink_backend != AuditSinkBackend.JSONL:
        return AuditEventPage(source="disabled", events=[], total=0, malformed=0)

    path = Path(cfg.audit_jsonl_path)
    if not path.exists():
        return AuditEventPage(source="empty", events=[], total=0, malformed=0)

    # Keep only enough newest matching records to serve this page.  The file
    # is streamed once, so a long-running deployment cannot make one UI page
    # allocate memory proportional to its entire audit history.
    records: deque[Dict[str, Any]] = deque(maxlen=cursor + limit + 1)
    total = 0
    malformed = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                raw = json.loads(line)
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
            records.append(item)

    newest_first = list(reversed(records))
    events = newest_first[cursor : cursor + limit]
    next_cursor = cursor + len(events) if total > cursor + len(events) else None
    return AuditEventPage(
        source="jsonl",
        events=events,
        total=total,
        malformed=malformed,
        next_cursor=next_cursor,
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
        _require_scope(principal, ADMIN_CONFIG_WRITE_SCOPE)
        document = _policy_document(
            yaml.safe_dump(request.document, sort_keys=False, allow_unicode=True)
        )
        return PolicyRenderResponse(
            policy_yaml=yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
        )

    @router.post("/policy/test", response_model=PolicyTestResponse)
    async def test_policy(
        request: PolicyTestRequest,
        principal: Principal = Depends(get_principal),
    ):
        _require_scope(principal, ADMIN_CONFIG_READ_SCOPE)
        return _test_policy(request)

    @router.get("/audit/events", response_model=AuditEventPage)
    async def browse_audit_events(
        cursor: int = Query(default=0, ge=0, le=1_000_000),
        limit: int = Query(default=50, ge=1, le=100),
        event_type: Optional[str] = Query(default=None),
        outcome: Optional[Literal["success", "rejected"]] = Query(default=None),
        principal_id: Optional[str] = Query(default=None, max_length=256),
        connection_id: Optional[str] = Query(default=None, max_length=256),
        action: Optional[str] = Query(default=None, max_length=64),
        principal: Principal = Depends(get_principal),
    ):
        _require_scope(principal, ADMIN_CONFIG_READ_SCOPE)
        if event_type is not None and event_type not in _AUDIT_EVENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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
