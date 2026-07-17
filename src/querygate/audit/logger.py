"""Structured audit records.

Every query attempt — successful or rejected — is logged with the compiled
SQL text, the caller's intent, timing, and outcome. Deliberately excludes
connection strings (never available at this layer — see connections/models.py)
and full row payloads (only row_count), so an audit trail can be retained and
shared without becoming a data-exfiltration surface itself.
"""

from __future__ import annotations

from typing import Optional

from querygate.core.logging import get_logger


def audit_query(
    *,
    connection_id: str,
    sql: str,
    intent: Optional[str] = None,
    row_count: Optional[int] = None,
    duration_ms: Optional[int] = None,
    principal: Optional[str] = None,
    rejected: bool = False,
    rejection_reason: Optional[str] = None,
) -> None:
    get_logger().info(
        "audit.query",
        connection=connection_id,
        principal=principal,
        sql=sql,
        intent=intent,
        row_count=row_count,
        duration_ms=duration_ms,
        rejected=rejected,
        rejection_reason=rejection_reason,
    )
