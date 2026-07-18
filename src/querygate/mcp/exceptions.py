"""MCP error handling helpers."""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel

from querygate.core.exceptions import (
    AuthorizationError,
    ConcurrencyLimitError,
    NotFoundError,
    PolicyViolationError,
    QueryValidationError,
    public_error_message,
)
from querygate.core.logging import get_logger


class MCPErrorResult(BaseModel):
    success: bool = False
    error_code: str
    error_message: str


def _error_code_from_exception(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, NotFoundError):
        return "NOT_FOUND", str(exc)
    if isinstance(exc, HTTPException):
        detail = exc.detail
        message = str(detail.get("msg", detail)) if isinstance(detail, dict) else str(detail)
        return f"HTTP_{exc.status_code}", message
    if isinstance(exc, AuthorizationError):
        return "FORBIDDEN", public_error_message(exc)
    if isinstance(exc, (PolicyViolationError, QueryValidationError, ConcurrencyLimitError)):
        return "VALIDATION", public_error_message(exc)
    return "INTERNAL", public_error_message(exc)


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
                return MCPErrorResult(error_code=error_code, error_message=error_message)

    return wrapper
