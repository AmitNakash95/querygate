"""Centralized REST error handling: scope checks, admission headers, and the
canonical domain-exception → HTTP-status map.

The MCP transport already centralizes the identical domain-exception →
client-error mapping in `mcp/exceptions.py` (`safe_mcp_tool` +
`_error_code_from_exception`). This module is the REST counterpart, so neither
transport re-inlines that map per route (CLAUDE.md, "Composable single-purpose
interfaces").

Two mechanisms, split deliberately:

* `install_exception_handlers(app)` registers the domain-exception → status map
  once. Those are non-500 handlers, so they run in Starlette's
  `ExceptionMiddleware` and behave identically whether or not the app is in
  debug mode. A handler fires only for an exception a route lets *propagate*,
  so a route needing a context-specific status the global map doesn't express
  (catalog governance's 404-vs-409 on `CatalogGovernanceError`, admin config's
  400 on a failed reload, admin_ui's 422 on a YAML parse error) still adds its
  own local `try/except` and is unaffected.

* `mask_unexpected()` is the shared replacement for the copy-pasted
  `except Exception -> 500 (PUBLIC_INTERNAL_ERROR)` guard. Masking must happen
  in-process (raised as an `HTTPException`), *not* as an app-level `Exception`
  handler: under `debug=True` (the default locally) Starlette's
  `ServerErrorMiddleware` renders a traceback and never calls a registered 500
  handler, which would leak driver text / SQL / bind values. Domain exceptions
  and `HTTPException` pass straight through to the handlers above.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette import status

from querygate.core.auth import Principal
from querygate.core.exceptions import (
    PUBLIC_INTERNAL_ERROR,
    AuthorizationError,
    CapacityTimeoutError,
    ConcurrencyLimitError,
    ConfigValidationError,
    NotFoundError,
    PolicyViolationError,
    QueryValidationError,
    public_error_message,
)
from querygate.core.logging import get_logger

log = get_logger()


def require_scope(principal: Principal, scope: str) -> None:
    """Raise 403 if the principal lacks `scope`.

    Shared by every admin/catalog REST router so the check and its message live
    in exactly one place.
    """
    if scope not in principal.scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing required scope: {scope!r}",
        )


# Agent-visible admission info (TODO.md item 35 phase 1) travels as response
# headers so the existing `{"detail": ...}` 422 body callers already parse never
# changes. Both the success path (routes.py) and the CapacityTimeoutError
# handler below build headers from `admission_headers`.
_ADMISSION_ID_HEADER = "X-QueryGate-Admission-Id"
_ADMISSION_STATE_HEADER = "X-QueryGate-Admission-State"
_QUEUE_WAIT_HEADER = "X-QueryGate-Queue-Wait-Ms"


def admission_headers(
    *, admission_id: Optional[str], state: str, queue_wait_ms: Optional[int]
) -> dict:
    headers = {_ADMISSION_STATE_HEADER: state}
    if admission_id is not None:
        headers[_ADMISSION_ID_HEADER] = admission_id
    if queue_wait_ms is not None:
        headers[_QUEUE_WAIT_HEADER] = str(queue_wait_ms)
    return headers


# Client-actionable domain failures: their message is safe to surface and they
# carry their own status via the handlers below, so `mask_unexpected` lets them
# propagate untouched. Everything else is masked.
_ACTIONABLE = (
    NotFoundError,
    PolicyViolationError,
    ConcurrencyLimitError,
    QueryValidationError,
    ConfigValidationError,
    AuthorizationError,
)


@contextmanager
def mask_unexpected() -> Iterator[None]:
    """Mask any non-actionable exception raised inside the block to a generic
    500, so driver details / SQL / bind values / paths never reach the client.

    Actionable domain exceptions and `HTTPException` pass through unchanged (the
    former are mapped by the app-level handlers). See the module docstring for
    why this is a context manager rather than an app-level 500 handler.
    """
    try:
        yield
    except (HTTPException, *_ACTIONABLE):
        raise
    except Exception:
        log.exception("rest.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=PUBLIC_INTERNAL_ERROR,
        )


def _response(exc: Exception, status_code: int, *, headers: Optional[dict] = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": public_error_message(exc)},
        headers=headers,
    )


def install_exception_handlers(app: FastAPI) -> None:
    """Register the canonical domain-exception → HTTP-status map on `app`.

    Only fires for exceptions a route lets propagate; routes needing a
    context-specific status keep their own local handling (see module docstring).
    """

    @app.exception_handler(NotFoundError)
    async def _not_found(_request: Request, exc: NotFoundError) -> JSONResponse:
        return _response(exc, status.HTTP_404_NOT_FOUND)

    # Registered separately from (and more specifically than)
    # ConcurrencyLimitError: a capacity timeout / queue-full additionally
    # carries admission headers.
    @app.exception_handler(CapacityTimeoutError)
    async def _capacity_timeout(_request: Request, exc: CapacityTimeoutError) -> JSONResponse:
        return _response(
            exc,
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            headers=admission_headers(
                admission_id=exc.admission_id,
                state=exc.admission_state,
                queue_wait_ms=exc.queue_wait_ms,
            ),
        )

    @app.exception_handler(ConcurrencyLimitError)
    async def _concurrency(_request: Request, exc: ConcurrencyLimitError) -> JSONResponse:
        return _response(exc, status.HTTP_422_UNPROCESSABLE_ENTITY)

    @app.exception_handler(PolicyViolationError)
    async def _policy(_request: Request, exc: PolicyViolationError) -> JSONResponse:
        return _response(exc, status.HTTP_422_UNPROCESSABLE_ENTITY)

    @app.exception_handler(QueryValidationError)
    async def _query_validation(_request: Request, exc: QueryValidationError) -> JSONResponse:
        return _response(exc, status.HTTP_422_UNPROCESSABLE_ENTITY)

    @app.exception_handler(ConfigValidationError)
    async def _config_validation(_request: Request, exc: ConfigValidationError) -> JSONResponse:
        return _response(exc, status.HTTP_422_UNPROCESSABLE_ENTITY)

    @app.exception_handler(AuthorizationError)
    async def _authorization(_request: Request, exc: AuthorizationError) -> JSONResponse:
        return _response(exc, status.HTTP_403_FORBIDDEN)
