"""Shared exception types used across validation, execution, and the API/MCP layers."""

from __future__ import annotations


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
