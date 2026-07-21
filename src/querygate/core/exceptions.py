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


class QueueDepthExceededError(ConcurrencyLimitError):
    """Raised by `execution/concurrency.py` when a connection's (or a single
    principal's) waiting queue is already at `Policy.max_queue_depth` /
    `max_queue_depth_per_principal` (TODO.md item 35 phase 2) — the caller is
    rejected before it attempts to wait for a concurrency slot at all, so an
    unbounded number of parked waiters can't become its own resource-
    exhaustion vector. Plain, low-level signal — same "raise a bare
    `ConcurrencyLimitError` at the guardrail, enrich it into a public
    exception at the service boundary" split `ConcurrencyLimitError` already
    uses for a genuine wait-timeout (see `QueueFullError` below).
    """


class CapacityTimeoutError(ConcurrencyLimitError):
    """`ConcurrencyLimitError` enriched with an admission id and elapsed
    queue-wait time (TODO.md item 35 phase 1). Subclasses
    `ConcurrencyLimitError` so every existing `isinstance`/`except` site
    (REST/MCP error mapping, `metrics.classify_rejection`) keeps working
    unchanged; the extra attributes let REST/MCP additionally surface a
    stable, machine-readable `admission_state` without changing the
    existing string message contract callers already parse.
    """

    def __init__(
        self,
        message: str,
        *,
        admission_id: str,
        queue_wait_ms: int,
        admission_state: str = "capacity_timeout",
    ) -> None:
        super().__init__(message)
        self.admission_id = admission_id
        self.queue_wait_ms = queue_wait_ms
        self.admission_state = admission_state


class QueueFullError(CapacityTimeoutError):
    """`QueueDepthExceededError` enriched with an admission id at the
    `StructuredQueryService` boundary (TODO.md item 35 phase 2) — mirrors how
    `CapacityTimeoutError` enriches a plain `ConcurrencyLimitError` for a
    genuine wait-timeout. `queue_wait_ms` is always 0 (the caller never began
    waiting) and `admission_state` is `"queue_full"`, distinct from
    `"capacity_timeout"`, so REST/MCP/audit can tell "the queue itself is at
    its configured depth cap" apart from "waited and ran out of time".
    Subclasses `CapacityTimeoutError` (not `QueueDepthExceededError`) so every
    existing `except CapacityTimeoutError` site at the REST/MCP boundary
    handles it identically without a new branch.
    """

    def __init__(self, message: str, *, admission_id: str) -> None:
        super().__init__(
            message, admission_id=admission_id, queue_wait_ms=0, admission_state="queue_full"
        )


class CostEstimateExceededError(PolicyViolationError):
    """Raised when a Postgres EXPLAIN-based pre-execution cost estimate
    exceeds `Policy.max_estimated_rows`/`max_estimated_cost` (see
    execution/cost_estimation.py). Subclasses PolicyViolationError so
    existing `except ValueError`/`except PolicyViolationError` handling
    still applies unchanged, and exists as its own type — same rationale as
    ConcurrencyLimitError — so metrics classification (see
    metrics.classify_rejection) can report a dedicated `cost_estimate`
    rejection reason instead of folding it into the coarser `policy` bucket.
    """


class QuotaExceededError(PolicyViolationError):
    """A principal's request-count or response-byte quota over a rolling
    window (TODO.md item 50) is already exhausted, so this execution attempt
    is refused before it runs. Subclasses `PolicyViolationError` so every
    existing `except ValueError`/`except PolicyViolationError` site and
    `public_error_message` keep treating it as a client-actionable rejection
    with a surfaceable message; it exists as its own type so
    `metrics.classify_rejection` can report a dedicated `quota` reason and the
    REST/MCP edges can additionally surface a `Retry-After` hint without
    sniffing message text (same split `ConcurrencyLimitError`/
    `CostEstimateExceededError` already use).

    `quota_kind` is `"requests"` or `"bytes"`; `retry_after_seconds` is a
    conservative whole-second hint (when the oldest in-window attempt ages
    out) the REST 429 handler puts in a `Retry-After` header.
    """

    def __init__(self, message: str, *, quota_kind: str, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.quota_kind = quota_kind
        self.retry_after_seconds = retry_after_seconds


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


class CatalogGovernanceError(ValueError):
    """A catalog governance operation (edit/approve/reject/publish/rollback)
    was requested from an invalid state, or a publish would conflict with
    already-verified content.

    Client-actionable for the same reason as ConfigValidationError — a
    reviewer needs to see exactly why a transition was refused (wrong
    status, stale schema, a field conflict with verified content), not a
    masked internal error.
    """


class AuthorizationError(Exception):
    """The authenticated caller lacks an explicit operation scope."""

    def __init__(self, required_scope: str) -> None:
        self.required_scope = required_scope
        super().__init__(f"Missing required scope: {required_scope!r}")


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
            CatalogGovernanceError,
            AuthorizationError,
        ),
    ):
        return str(exc)
    return PUBLIC_INTERNAL_ERROR
