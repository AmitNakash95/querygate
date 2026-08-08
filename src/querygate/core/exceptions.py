"""Shared exception types used across validation, execution, and the API/MCP layers."""

from __future__ import annotations

import pydantic as pyd
import yaml

PUBLIC_INTERNAL_ERROR = "An unexpected error occurred."


class NotFoundError(Exception):
    """Raised when a requested resource (connection, table) doesn't exist."""


class ServiceDisabledError(Exception):
    """Raised when an optional subsystem isn't configured on this deployment
    (e.g. the item 47 phase 2 draft store with no encryption key set) — a
    deployment/configuration condition, not a caller input error, so it maps
    to 503 rather than a 4xx at the route layer."""


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
        retry_after_seconds: int,
        admission_state: str = "capacity_timeout",
    ) -> None:
        super().__init__(message)
        self.admission_id = admission_id
        self.queue_wait_ms = queue_wait_ms
        # A conservative hint (the connection's own concurrency_wait_seconds
        # ceiling), surfaced as REST's Retry-After (TODO.md item 35 phase 3,
        # migrated from 422 to 429 — same mechanism QuotaExceededError
        # already uses, 2026-07-28 Decision Log).
        self.retry_after_seconds = retry_after_seconds
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

    def __init__(self, message: str, *, admission_id: str, retry_after_seconds: int) -> None:
        super().__init__(
            message,
            admission_id=admission_id,
            queue_wait_ms=0,
            retry_after_seconds=retry_after_seconds,
            admission_state="queue_full",
        )


class QueryCancellationNotEnabledError(PolicyViolationError):
    """A cancel request for a RUNNING async query (TODO.md item 35 phase 3),
    rejected because the connection's `Policy.allow_query_cancellation` is not
    set. Its own type (rather than a bare `PolicyViolationError`) so the REST
    edge can map it to `403` specifically — the operator has deliberately not
    enabled this capability, distinct from every other `422` policy rejection.
    """


class QueryCancellationNotReadyError(ValueError):
    """A cancel request for a query that is RUNNING but hasn't yet opened its
    database session (a narrow timing window right after admission, before
    `session_scope` captures the dialect session identifier) — there is
    nothing to cancel yet. Retry-able: the caller should poll status and
    cancel again shortly. Maps to REST `409`, distinct from the `403` of
    `QueryCancellationNotEnabledError` (a policy decision, not a timing one).
    """


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


class ApprovalRequiredError(PolicyViolationError):
    """A read tripped the in-query human-in-the-loop approval gate (TODO.md item
    92): its pre-execution estimate exceeded a policy approval threshold and no
    valid approval token was supplied, so execution is paused pending a human
    grant. Subclasses `PolicyViolationError` so existing
    `except ValueError`/`except PolicyViolationError` handling still treats it as
    a client-actionable rejection; it is its own type so the REST edge can map it
    to a distinct `428 Precondition Required` carrying the `fingerprint` (which
    an approver signs) and the `reasons`, and so `metrics.classify_rejection` can
    report a dedicated `approval_required` reason.
    """

    def __init__(self, message: str, *, fingerprint: str, reasons: list[str]) -> None:
        super().__init__(message)
        self.fingerprint = fingerprint
        self.reasons = reasons
        # The approval gate runs *after* the per-principal quota is reserved, so
        # by the time this is raised one quota unit is already spent for this
        # logical query. When an in-session batch retry obtains a token and calls
        # `execute()` again, it reuses this reservation instead of reserving a
        # second unit (TODO.md item 107). Typed loosely to avoid a core->execution
        # import; only `execution/service.py` sets or reads it.
        self.quota_reservation: object | None = None


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
            ServiceDisabledError,
        ),
    ):
        return str(exc)
    return PUBLIC_INTERNAL_ERROR


def safe_pydantic_error_lines(exc: pyd.ValidationError) -> list[str]:
    """Turn a `pydantic.ValidationError` into safe, human-readable lines built
    strictly from each error's `loc`/`msg` -- never `input`/`input_value`.

    Any caller that validates a model built from already-`${...}`-interpolated
    config -- e.g. `ConnectionProfile.model_validate()` on a `connections.yaml`
    entry -- must never surface `str(exc)` or `error["input"]`/
    `error["input_value"]` directly: those can embed the *entire* validated
    object, including a live credential, for certain pydantic error kinds
    (e.g. a `missing`-type error). The actual guarantee is that this function
    only ever reads `loc`/`msg` from each error dict -- `include_input=False`
    is a belt-and-braces request to pydantic to not even materialize
    `input`/`input_value` in the first place, on top of (not instead of) that.

    Neutral module (item 168): shared by `admin/service.py` (item 165's
    `/admin/reload-config` and config-governance dry-run fix) and `cli.py`
    (`load_config_context`, item 168's fix for the same gap in the
    `querygate-validate-config` CLI and its callers) -- `admin/service.py`
    imports from `cli.py`, so this can't live in either module without a
    cycle.
    """
    lines: list[str] = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        loc = ".".join(str(part) for part in error.get("loc", ()))
        msg = error.get("msg", "")
        lines.append(f"{loc}: {msg}" if loc else msg)
    return lines


def safe_yaml_error_detail(exc: yaml.YAMLError) -> str:
    """Turn a `yaml.YAMLError` into a safe, human-readable summary that never
    calls `str()` on the exception itself, or on its `.problem_mark`/
    `.context_mark`.

    PyYAML's `Mark.__str__` embeds `get_snippet()` -- the literal offending
    SOURCE LINE -- which for a `connections.yaml` parse failure can be a line
    containing a live credential (either an already-interpolated `${...}`
    reference, or a literal credential written directly -- config-governance
    drafts explicitly permit both). Only the plain string fields
    (`.context`/`.problem`/`.note`) and the mark's integer `.line`/`.column`
    are used -- never the mark's own `__str__`, and never `.name` (which can
    be a caller-controlled stream/file label). See `safe_pydantic_error_lines`
    above for the module-placement rationale.
    """
    parts: list[str] = []
    context = getattr(exc, "context", None)
    if context:
        parts.append(str(context))
    problem = getattr(exc, "problem", None)
    if problem:
        parts.append(str(problem))
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    if mark is not None:
        parts.append(f"at line {mark.line + 1}, column {mark.column + 1}")
    note = getattr(exc, "note", None)
    if note:
        parts.append(str(note))
    if not parts:
        parts.append("Invalid YAML syntax")
    return "; ".join(parts)
