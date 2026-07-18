"""Structured audit records.

Every query attempt — successful or rejected — is logged with the compiled
SQL text, the caller's intent, timing, and outcome. Deliberately excludes
connection strings (never available at this layer — see connections/models.py)
and full row payloads (only row_count), so an audit trail can be retained and
shared without becoming a data-exfiltration surface itself.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from querygate.audit.events import AuditDecision, AuditEvent, AuditSurface
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
