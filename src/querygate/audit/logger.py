"""Structured audit records.

Every query attempt — successful or rejected — is logged with the compiled
SQL text, the caller's intent, timing, and outcome. Deliberately excludes
connection strings (never available at this layer — see connections/models.py)
and full row payloads (only row_count), so an audit trail can be retained and
shared without becoming a data-exfiltration surface itself.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from querygate.audit.events import (
    AuditDecision,
    AuditEvent,
    AuditSurface,
    CatalogGovernanceAction,
    CatalogGovernanceEvent,
    ConfigChangeAction,
    ConfigChangeEvent,
    ConnectionProbeEvent,
)
from querygate.audit.sinks import get_audit_sink
from querygate.core.logging import get_logger


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
) -> None:
    log = get_logger()
    event = AuditEvent(
        correlation_id=log.extra.get("request_id"),
        surface=surface,
        operation=operation,
        principal_id=principal,
        auth_method=auth_method,
        principal_scopes=principal_scopes or [],
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
    )
    log.info(
        "audit.query",
        audit_event_id=event.event_id,
        connection=connection_id,
        principal=principal,
        principal_scopes=principal_scopes,
        auth_method=auth_method,
        surface=surface,
        sql=sql,
        params=params,
        intent=intent,
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
    try:
        get_audit_sink().emit(event)
    except Exception as exc:
        # A persistence outage must be visible but cannot turn a successfully
        # executed read into a misleading client error after the DB work has
        # already happened. Operators should alert on this log event.
        log.error(
            "audit.sink.write_failed",
            audit_event_id=event.event_id,
            sink_type=type(get_audit_sink()).__name__,
            error=f"{type(exc).__name__}: {exc}",
        )


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
    try:
        get_audit_sink().emit(event)
    except Exception as exc:
        log.error(
            "audit.sink.write_failed",
            audit_event_id=event.event_id,
            sink_type=type(get_audit_sink()).__name__,
            error=f"{type(exc).__name__}: {exc}",
        )


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
    try:
        get_audit_sink().emit(event)
    except Exception as exc:
        log.error(
            "audit.sink.write_failed",
            audit_event_id=event.event_id,
            sink_type=type(get_audit_sink()).__name__,
            error=f"{type(exc).__name__}: {exc}",
        )


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
    try:
        get_audit_sink().emit(event)
    except Exception as exc:
        log.error(
            "audit.sink.write_failed",
            audit_event_id=event.event_id,
            sink_type=type(get_audit_sink()).__name__,
            error=f"{type(exc).__name__}: {exc}",
        )
