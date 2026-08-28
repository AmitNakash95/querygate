"""MCP error handling helpers."""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Coroutine
from typing import Any

from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel

from querygate.core.exceptions import (
    AuthorizationError,
    CapacityTimeoutError,
    ConcurrencyLimitError,
    ConfigValidationError,
    NotFoundError,
    PolicyViolationError,
    QueryValidationError,
    QuotaExceededError,
    ServiceDisabledError,
    SubscriptionExpiredError,
    public_error_message,
)
from querygate.core.logging import get_logger


class MCPErrorResult(BaseModel):
    success: bool = False
    error_code: str
    error_message: str
    # Agent-visible admission info (TODO.md item 35) — populated only when
    # the failure was a CapacityTimeoutError (or its QueueFullError
    # subclass), so a caller can tell "too many concurrent queries"/"queue
    # already full" apart from an ordinary validation error and correlate it
    # with metrics/audit events via admission_id.
    admission_id: Optional[str] = None
    admission_state: Optional[str] = None
    queue_wait_ms: Optional[int] = None
    # Mirrors REST's Retry-After header (TODO.md item 35 phase 3, migrated
    # 2026-07-28 alongside REST's 422->429 move) — set only for a
    # CapacityTimeoutError/QueueFullError, which is the one exception in this
    # family that actually carries the value today.
    retry_after_seconds: Optional[int] = None


def _error_code_from_exception(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, NotFoundError):
        return "NOT_FOUND", str(exc)
    if isinstance(exc, HTTPException):
        detail = exc.detail
        message = str(detail.get("msg", detail)) if isinstance(detail, dict) else str(detail)
        return f"HTTP_{exc.status_code}", message
    if isinstance(exc, AuthorizationError):
        return "FORBIDDEN", public_error_message(exc)
    # Before the INTERNAL fall-through: without a branch here an expired
    # subscription is logged with a full traceback as an unexpected fault, and
    # the agent is told "an internal error occurred" rather than "renew here".
    if isinstance(exc, SubscriptionExpiredError):
        return "SUBSCRIPTION_EXPIRED", public_error_message(exc)
    # Checked before PolicyViolationError (its superclass): a per-principal
    # quota rejection (TODO.md item 50) gets its own code so an agent can tell
    # "slow down / budget exhausted, retry later" apart from a structural
    # validation error it should not retry unchanged. ConcurrencyLimitError
    # (and its CapacityTimeoutError/QueueFullError enrichments) joined this
    # bucket 2026-07-28, matching the REST 422->429 migration — "at capacity,
    # retry later" is the same semantic as quota exhaustion, not a structural
    # validation error.
    if isinstance(exc, (QuotaExceededError, ConcurrencyLimitError)):
        return "RATE_LIMITED", public_error_message(exc)
    # ConfigValidationError joined this bucket 2026-08-07 (TODO.md item 163's
    # own audit): it is the identical "client-actionable, explained rejection"
    # shape as QueryValidationError (see connections/engine.py's init_engine
    # and validation/schema_validation.py's cross-connection is_connectable()
    # check, both raise it), mapped to REST 422 the same way in api/_errors.py
    # — but this MCP mapping had no branch for it at all, so it fell all the
    # way through to "INTERNAL" and a log.exception() traceback as if it were
    # a genuine unexpected fault, defeating the whole point of raising a
    # named, actionable exception instead of letting a real error escape.
    if isinstance(exc, (PolicyViolationError, QueryValidationError, ConfigValidationError)):
        return "VALIDATION", public_error_message(exc)
    # An optional subsystem isn't configured on this deployment (e.g. the
    # item 47 phase 2 draft store with no encryption key) — no MCP tool
    # wraps it today, but this keeps the vocabulary complete for the first
    # one that does, rather than silently falling through to "INTERNAL".
    if isinstance(exc, ServiceDisabledError):
        return "SERVICE_DISABLED", public_error_message(exc)
    return "INTERNAL", public_error_message(exc)


def _admission_fields_from_exception(exc: Exception) -> dict:
    if not isinstance(exc, CapacityTimeoutError):
        return {}
    return {
        "admission_id": exc.admission_id,
        "admission_state": exc.admission_state,
        "queue_wait_ms": exc.queue_wait_ms,
        "retry_after_seconds": exc.retry_after_seconds,
    }


def safe_mcp_tool(
    fn: Callable[..., Coroutine[Any, Any, Any]],
) -> Callable[..., Coroutine[Any, Any, Any]]:
    """Catch exceptions and return MCPErrorResult instead of raising."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        tool_name = fn.__name__
        start = time.monotonic()

        with get_logger().contextualize(mcp_tool=tool_name) as log:
            log.info("mcp.tool.started")
            try:
                result = await fn(*args, **kwargs)
                duration_ms = int((time.monotonic() - start) * 1000)
                log.info("mcp.tool.success", duration_ms=duration_ms)
                return result
            except Exception as exc:
                duration_ms = int((time.monotonic() - start) * 1000)
                error_code, error_message = _error_code_from_exception(exc)
                if error_code == "INTERNAL":
                    log.exception("mcp.tool.unexpected_error", duration_ms=duration_ms)
                else:
                    log.error("mcp.tool.app_error", error_code=error_code, duration_ms=duration_ms)
                return MCPErrorResult(
                    error_code=error_code,
                    error_message=error_message,
                    **_admission_fields_from_exception(exc),
                )

    return wrapper
