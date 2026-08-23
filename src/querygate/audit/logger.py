"""Structured audit records.

Every query attempt — successful or rejected — is logged with the compiled
SQL text, the caller's intent, timing, and outcome. Deliberately excludes
connection strings (never available at this layer — see connections/models.py)
and full row payloads (only row_count), so an audit trail can be retained and
shared without becoming a data-exfiltration surface itself.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from querygate.audit.events import (
    AuditDecision,
    AuditEvent,
    AuditSurface,
    AuthenticationAction,
    AuthenticationEvent,
    CatalogGovernanceAction,
    CatalogGovernanceEvent,
    ConfigChangeAction,
    ConfigChangeEvent,
    ConnectionProbeEvent,
    PersistableEvent,
)
from querygate.audit.sinks import get_audit_sink
from querygate.core.logging import ContextLogger, get_logger


def _persist(event: PersistableEvent, log: ContextLogger) -> None:
    """Re-stamp `occurred_at` immediately before the durable write.

    Closes the gap between event construction and the sink's write-order
    lock — the dominant source of drift between physical write order and
    `occurred_at` order (TODO.md item 141, PRODUCT_GUIDE Decision Log): an
    arbitrary amount of work (structured logging, future additions here) can
    run between building the event and this call. Direct `sink.emit()`
    callers — the audit test suite's synthetic-history fixtures — are
    unaffected; only this production write path re-stamps.
    """
    event = event.model_copy(update={"occurred_at": datetime.now(timezone.utc)})
    try:
        get_audit_sink().emit(event)
    except Exception as exc:
        log.error(
            "audit.sink.write_failed",
            audit_event_id=event.event_id,
            sink_type=type(get_audit_sink()).__name__,
            error=f"{type(exc).__name__}: {exc}",
        )


def audit_query(
    *,
    connection_id: str,
    sql: str,
    params: Optional[str] = None,
    intent: Optional[str] = None,
    row_count: Optional[int] = None,
    duration_ms: Optional[int] = None,
    principal: Optional[str] = None,
    principal_scopes: Optional[List[str]] = None,
    actor: Optional[str] = None,
    delegation_chain: Optional[List[str]] = None,
    auth_method: str = "unknown",
    surface: AuditSurface = "internal",
    operation: str = "execute_structured_query",
    query_shape: Optional[Dict[str, Any]] = None,
    response_bytes: Optional[int] = None,
    truncated: Optional[bool] = None,
    error_category: Optional[str] = None,
    policy_decision: Optional[AuditDecision] = None,
    rejected: bool = False,
    rejection_reason: Optional[str] = None,
    admission_id: Optional[str] = None,
    queue_wait_ms: Optional[int] = None,
    admission_state: Optional[str] = None,
    template_id: Optional[str] = None,
    template_param_shape: Optional[List[str]] = None,
    masked_columns: Optional[List[str]] = None,
    purpose: Optional[str] = None,
) -> None:
    log = get_logger()
    event = AuditEvent(
        correlation_id=log.extra.get("request_id"),
        surface=surface,
        operation=operation,
        principal_id=principal,
        auth_method=auth_method,
        principal_scopes=principal_scopes or [],
        actor_id=actor,
        delegation_chain=delegation_chain or [],
        connection_id=connection_id,
        policy_decision=policy_decision or ("denied" if rejected else "allowed"),
        outcome="rejected" if rejected else "success",
        query_shape=query_shape or {},
        duration_ms=max(duration_ms or 0, 0),
        row_count=row_count,
        response_bytes=response_bytes,
        truncated=truncated,
        error_category=error_category,
        admission_id=admission_id,
        queue_wait_ms=queue_wait_ms,
        admission_state=admission_state,
        template_id=template_id,
        template_param_shape=template_param_shape,
        masked_columns=masked_columns or [],
        purpose=purpose,
    )
    log.info(
        "audit.query",
        audit_event_id=event.event_id,
        connection=connection_id,
        principal=principal,
        principal_scopes=principal_scopes,
        actor=actor,
        delegation_chain=delegation_chain,
        auth_method=auth_method,
        surface=surface,
        sql=sql,
        params=params,
        intent=intent,
        purpose=purpose,
        row_count=row_count,
        response_bytes=response_bytes,
        duration_ms=duration_ms,
        rejected=rejected,
        error_category=error_category,
        rejection_reason=rejection_reason,
        admission_id=admission_id,
        queue_wait_ms=queue_wait_ms,
        admission_state=admission_state,
        masked_columns=masked_columns,
    )
    # A persistence outage must be visible but cannot turn a successfully
    # executed read into a misleading client error after the DB work has
    # already happened — `_persist` logs, never raises. Operators should
    # alert on the resulting log event.
    _persist(event, log)


def audit_config_change(
    *,
    action: ConfigChangeAction,
    outcome: str,
    principal: Optional[str] = None,
    principal_scopes: Optional[List[str]] = None,
    auth_method: str = "unknown",
    surface: AuditSurface = "internal",
    version_id: Optional[str] = None,
    previous_version_id: Optional[str] = None,
    description: Optional[str] = None,
    duration_ms: Optional[int] = None,
    error_category: Optional[str] = None,
    draft_id: Optional[str] = None,
) -> None:
    """Record a config-governance action (validate/preview/simulate/diff/stage/apply/rollback) —
    same durable sink as `audit_query`, so a customer's audit trail covers
    both query attempts and who changed access to a database, when.
    """
    log = get_logger()
    event = ConfigChangeEvent(
        correlation_id=log.extra.get("request_id"),
        surface=surface,
        action=action,
        principal_id=principal,
        auth_method=auth_method,
        principal_scopes=principal_scopes or [],
        version_id=version_id,
        previous_version_id=previous_version_id,
        description=description,
        outcome="success" if outcome == "success" else "rejected",
        error_category=error_category,
        duration_ms=max(duration_ms or 0, 0),
        draft_id=draft_id,
    )
    log.info(
        "audit.config_change",
        audit_event_id=event.event_id,
        action=action,
        version_id=version_id,
        previous_version_id=previous_version_id,
        principal=principal,
        principal_scopes=principal_scopes,
        auth_method=auth_method,
        surface=surface,
        outcome=event.outcome,
        error_category=error_category,
    )
    _persist(event, log)


def audit_connection_probe(
    *,
    connection_id: str,
    outcome: str,
    principal: Optional[str] = None,
    principal_scopes: Optional[List[str]] = None,
    auth_method: str = "unknown",
    surface: AuditSurface = "internal",
    probe_healthy: Optional[bool] = None,
    failure_category: Optional[str] = None,
    latency_ms: Optional[float] = None,
    duration_ms: Optional[int] = None,
    error_category: Optional[str] = None,
) -> None:
    """Record a manual "test now" connection probe (TODO.md item 43 phase
    2) — same durable sink as `audit_query`/`audit_config_change`/
    `audit_catalog_governance`, never a raw driver error or connection
    string.
    """
    log = get_logger()
    event = ConnectionProbeEvent(
        correlation_id=log.extra.get("request_id"),
        surface=surface,
        connection_id=connection_id,
        principal_id=principal,
        auth_method=auth_method,
        principal_scopes=principal_scopes or [],
        outcome="success" if outcome == "success" else "rejected",
        probe_healthy=probe_healthy,
        failure_category=failure_category,
        latency_ms=latency_ms,
        error_category=error_category,
        duration_ms=max(duration_ms or 0, 0),
    )
    log.info(
        "audit.connection_probe",
        audit_event_id=event.event_id,
        connection_id=connection_id,
        principal=principal,
        principal_scopes=principal_scopes,
        auth_method=auth_method,
        surface=surface,
        outcome=event.outcome,
        probe_healthy=probe_healthy,
        failure_category=failure_category,
        error_category=error_category,
    )
    _persist(event, log)


def audit_catalog_governance(
    *,
    action: CatalogGovernanceAction,
    outcome: str,
    principal: Optional[str] = None,
    principal_scopes: Optional[List[str]] = None,
    auth_method: str = "unknown",
    surface: AuditSurface = "internal",
    connection_id: Optional[str] = None,
    proposal_id: Optional[str] = None,
    proposal_count: Optional[int] = None,
    version_id: Optional[str] = None,
    entry_id: Optional[str] = None,
    duration_ms: Optional[int] = None,
    error_category: Optional[str] = None,
) -> None:
    """Record a catalog-governance action — same durable sink as
    `audit_query`/`audit_config_change`, never draft/proposal text.
    """
    log = get_logger()
    event = CatalogGovernanceEvent(
        correlation_id=log.extra.get("request_id"),
        surface=surface,
        action=action,
        principal_id=principal,
        auth_method=auth_method,
        principal_scopes=principal_scopes or [],
        connection_id=connection_id,
        proposal_id=proposal_id,
        proposal_count=proposal_count,
        version_id=version_id,
        entry_id=entry_id,
        outcome="success" if outcome == "success" else "rejected",
        error_category=error_category,
        duration_ms=max(duration_ms or 0, 0),
    )
    log.info(
        "audit.catalog_governance",
        audit_event_id=event.event_id,
        action=action,
        connection_id=connection_id,
        proposal_id=proposal_id,
        version_id=version_id,
        principal=principal,
        principal_scopes=principal_scopes,
        auth_method=auth_method,
        surface=surface,
        outcome=event.outcome,
        error_category=error_category,
    )
    _persist(event, log)


def audit_authentication(
    *,
    action: AuthenticationAction,
    outcome: str,
    principal: Optional[str] = None,
    principal_scopes: Optional[List[str]] = None,
    auth_method: str = "unknown",
    surface: AuditSurface = "rest",
    provider_id: Optional[str] = None,
    mfa_used: Optional[bool] = None,
    client_name: Optional[str] = None,
    target_principal: Optional[str] = None,
    duration_ms: Optional[int] = None,
    error_category: Optional[str] = None,
) -> None:
    """Record a sign-in, sign-out, device grant, or local-account change
    (TODO.md item 199) — the same durable sink as `audit_query` and
    `audit_config_change`, so one trail answers both "who queried what" and
    "how did that person come to be trusted".

    Callers pass a stable `error_category` (`invalid_credentials`,
    `nonce_mismatch`, `locked_out`, …), never an exception string: a failure
    reason must never be able to carry a submitted username, password, code, or
    token into the audit file.
    """
    log = get_logger()
    event = AuthenticationEvent(
        correlation_id=log.extra.get("request_id"),
        surface=surface,
        action=action,
        provider_id=provider_id,
        principal_id=principal,
        auth_method=auth_method,
        principal_scopes=principal_scopes or [],
        mfa_used=mfa_used,
        client_name=client_name,
        target_principal_id=target_principal,
        outcome="success" if outcome == "success" else "rejected",
        error_category=error_category,
        duration_ms=max(duration_ms or 0, 0),
    )
    log.info(
        "audit.authentication",
        audit_event_id=event.event_id,
        action=action,
        provider=provider_id,
        principal=principal,
        principal_scopes=principal_scopes,
        auth_method=auth_method,
        surface=surface,
        outcome=event.outcome,
        mfa_used=mfa_used,
        client_name=client_name,
        target_principal=target_principal,
        error_category=error_category,
    )
    _persist(event, log)
