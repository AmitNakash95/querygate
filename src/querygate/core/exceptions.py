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
