"""Shared exception types used across validation, execution, and the API/MCP layers."""

from __future__ import annotations

PUBLIC_INTERNAL_ERROR = "An unexpected error occurred."


class NotFoundError(Exception):
    """Raised when a requested resource (connection, table) doesn't exist."""


class PolicyViolationError(ValueError):
    """Raised when a query violates the active policy: a disabled connection,
    a denied table/column, or an exceeded complexity cap. Subclasses
    ValueError so existing `except ValueError` handling in the API/MCP layers
    still maps it to a client-facing validation error.
    """


class ConcurrencyLimitError(ValueError):
    """Raised when a connection's concurrency slot can't be acquired within
    `concurrency_wait_seconds`. Subclasses ValueError for the same reason as
    PolicyViolationError (existing `except ValueError` handling still
    applies) and exists as its own type so callers — metrics classification
    in particular (see metrics.classify_rejection) — can distinguish "too
    many concurrent queries" from other rejection reasons without sniffing
    exception message text.
    """


class QueryValidationError(ValueError):
    """Client-actionable query/schema validation failure.

    The explicit type prevents an unrelated ValueError raised by a database
    driver from being mistaken for safe validation text at a transport edge.
    """


class ConfigValidationError(ValueError):
    """A staged or applied config-governance version fails validation
    (bad YAML, unresolvable secret reference, unknown connection id, ...).

    Client-actionable for the same reason as QueryValidationError — an admin
    caller needs to see exactly what's wrong with a candidate config, not a
    masked internal error.
    """


def public_error_message(exc: Exception) -> str:
    """Return a client-safe message without exposing unexpected internals.

    Validation, policy, concurrency, and not-found failures are deliberately
    actionable to callers. Everything else may contain driver details, SQL,
    bind values, hostnames, or filesystem paths and is therefore masked.
    """
    if isinstance(
        exc,
        (
            NotFoundError,
            PolicyViolationError,
            ConcurrencyLimitError,
            QueryValidationError,
            ConfigValidationError,
        ),
    ):
        return str(exc)
    return PUBLIC_INTERNAL_ERROR
