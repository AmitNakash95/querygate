"""Agent-visible admission control layered on `concurrency_slot()` (TODO.md item 35 phase 1).

`concurrency_slot()` (`execution/concurrency.py`) already waits up to
`Policy.concurrency_wait_seconds` and raises `ConcurrencyLimitError` when that
window expires — proven under real load by item 15's harness. This module
makes that existing wait-then-fail-fast path caller-tunable without touching
the operator's ceiling: a caller may request `queue_mode="fail_fast"` (skip
waiting for a slot entirely) or a `wait_timeout_seconds` shorter than the
policy allows — never longer — and gets back a stable admission id to
correlate with metrics/audit events.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Optional

from querygate.core.exceptions import QueryValidationError


class QueueMode(StrEnum):
    """`fail_fast`: reject immediately if a concurrency slot isn't free, no
    waiting at all. `wait` (default — matches pre-item-35 behavior): wait up
    to the resolved wait-seconds ceiling before giving up. `async` (REST
    only, TODO.md item 35 phase 3): return `202` immediately with an
    `admission_id` instead of blocking the HTTP response; the query runs in
    the background exactly as `wait` would, and `GET .../query/{admission_id}`
    polls it. Only a REST-transport concept — `resolve_wait_seconds` below
    treats it identically to `wait` for the wait-ceiling calculation itself
    (the *waiting* semantics are unchanged; only whether the caller blocks on
    it is different), so `async` needs no special case there.
    """

    FAIL_FAST = "fail_fast"
    WAIT = "wait"
    ASYNC = "async"


def new_admission_id() -> str:
    return str(uuid.uuid4())


def reject_unsupported_async_queue_mode(queue_mode: Optional[QueueMode]) -> None:
    """`async` is a REST-transport concept implemented only by the single-query
    `POST .../query` route's own `202`/poll/cancel admission dance — nothing
    else builds that lifecycle. Without this check, `queue_mode="async"`
    reaching `execute()`/`execute_many()` through `run_query_template`,
    `execute_query_batch`, or the MCP `run_structured_queries` tool would
    silently execute synchronously and block instead (`resolve_wait_seconds`
    treats `async` identically to `wait` for the wait-ceiling calculation),
    with no signal to the caller that the mode they asked for wasn't
    actually honored. Reject, don't silently downgrade."""
    if queue_mode == QueueMode.ASYNC:
        raise QueryValidationError(
            "queue_mode='async' is only supported on POST /{connection}/query "
            "(single-query REST execution); query templates, batch queries, and "
            "the MCP run_structured_queries tool always execute synchronously."
        )


def resolve_wait_seconds(
    *,
    queue_mode: Optional[QueueMode],
    requested_wait_seconds: Optional[float],
    policy_ceiling_seconds: float,
) -> float:
    """Resolve how long this call should wait for a concurrency slot.

    A caller may choose to wait less than the operator's
    `Policy.concurrency_wait_seconds` (or not at all, via
    `queue_mode="fail_fast"`) but never more — `min()` below is the entire
    security property. Omitting both `queue_mode` and `requested_wait_seconds`
    preserves the exact pre-item-35 default: wait up to the policy ceiling.
    """
    if queue_mode == QueueMode.FAIL_FAST:
        return 0.0
    if requested_wait_seconds is None:
        return policy_ceiling_seconds
    return max(0.0, min(requested_wait_seconds, policy_ceiling_seconds))
