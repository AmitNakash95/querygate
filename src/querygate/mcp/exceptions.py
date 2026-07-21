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
    NotFoundError,
    PolicyViolationError,
    QueryValidationError,
    QuotaExceededError,
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


def _error_code_from_exception(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, NotFoundError):
        return "NOT_FOUND", str(exc)
    if isinstance(exc, HTTPException):
        detail = exc.detail
        message = str(detail.get("msg", detail)) if isinstance(detail, dict) else str(detail)
        return f"HTTP_{exc.status_code}", message
    if isinstance(exc, AuthorizationError):
        return "FORBIDDEN", public_error_message(exc)
    # Checked before PolicyViolationError (its superclass): a per-principal
    # quota rejection (TODO.md item 50) gets its own code so an agent can tell
    # "slow down / budget exhausted, retry later" apart from a structural
    # validation error it should not retry unchanged.
    if isinstance(exc, QuotaExceededError):
        return "RATE_LIMITED", public_error_message(exc)
    if isinstance(exc, (PolicyViolationError, QueryValidationError, ConcurrencyLimitError)):
        return "VALIDATION", public_error_message(exc)
    return "INTERNAL", public_error_message(exc)


def _admission_fields_from_exception(exc: Exception) -> dict:
    if not isinstance(exc, CapacityTimeoutError):
        return {}
    return {
        "admission_id": exc.admission_id,
        "admission_state": exc.admission_state,
        "queue_wait_ms": exc.queue_wait_ms,
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
