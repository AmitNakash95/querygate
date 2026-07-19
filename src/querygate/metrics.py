"""Prometheus metrics for query execution — exposed via `GET /metrics`.

`core/logging.py` emits JSON lines to stdout, which is fine for log
aggregation but gives no cheap way to alert on "concurrency saturation is
climbing" or "rejection rate spiked" without parsing logs.

Label cardinality is kept deliberately low and fixed: `connection` (bounded
by the connections file, already a public identifier — see
`connections/models.PublicConnectionInfo`) and a coarse `reason` bucket for
rejections. Never raw SQL, exception text, or row data — same invariant as
`audit/logger.py`.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from querygate.core.exceptions import (
    ConcurrencyLimitError,
    CostEstimateExceededError,
    PolicyViolationError,
    QueueFullError,
)

REGISTRY = CollectorRegistry()

QUERIES_TOTAL = Counter(
    "querygate_queries_total",
    "Structured queries processed, by connection and outcome.",
    ["connection", "status"],  # status: success | rejected
    registry=REGISTRY,
)

QUERIES_REJECTED_TOTAL = Counter(
    "querygate_queries_rejected_total",
    "Rejected structured queries, by connection and reason.",
    # policy: denied table/column/cap. schema: unknown table/column/malformed
    # query. concurrency: too many in-flight queries for this connection (a
    # genuine wait-timeout). queue_full: rejected before waiting at all
    # because Policy.max_queue_depth/max_queue_depth_per_principal was
    # already met (TODO.md item 35 phase 2) — broken out from `concurrency`
    # so operators can tell "the queue's own pressure control tripped" apart
    # from "waited and ran out of time". cost_estimate: rejected by a
    # pre-execution Postgres EXPLAIN cost check (TODO.md item 26) — broken
    # out from the coarser `policy` bucket so operators can tell threshold
    # tuning apart from allow/deny rules. db_error: everything else,
    # including genuine query timeouts — see TODO.md item 3, which hasn't
    # yet established a reliable, dialect-verified way to distinguish a
    # timeout from any other DB-layer failure.
    ["connection", "reason"],
    registry=REGISTRY,
)

QUERY_DURATION_SECONDS = Histogram(
    "querygate_query_duration_seconds",
    "Structured query duration in seconds, by connection (successful queries only).",
    ["connection"],
    registry=REGISTRY,
)

CONCURRENCY_IN_USE = Gauge(
    "querygate_concurrency_in_use",
    "In-flight queries currently holding a concurrency slot, by connection.",
    ["connection"],
    registry=REGISTRY,
)

CONCURRENCY_MAX = Gauge(
    "querygate_concurrency_max",
    "Configured max_concurrency for a connection's active policy — pairs "
    "with querygate_concurrency_in_use to compute utilization.",
    ["connection"],
    registry=REGISTRY,
)

QUEUE_DEPTH = Gauge(
    "querygate_queue_depth",
    "Callers currently waiting for a concurrency slot, by connection "
    "(TODO.md item 35). Single-process visibility only when the default "
    "in-process concurrency backend is used — like querygate_concurrency_in_use, "
    "it doesn't aggregate across replicas. When concurrency_backend=redis is "
    "selected, this gauge instead reports the true cross-replica queue depth "
    "(item 35 phase 2), computed from the same Redis the concurrency slots "
    "themselves use, so it reads the same on every replica.",
    ["connection"],
    registry=REGISTRY,
)

QUEUE_WAIT_SECONDS = Histogram(
    "querygate_queue_wait_seconds",
    "Time a structured query spent waiting for a concurrency slot before "
    "running, hitting a capacity timeout, or being rejected for an "
    "already-full queue, by connection and outcome (TODO.md item 35).",
    ["connection", "outcome"],  # outcome: completed | capacity_timeout | queue_full
    registry=REGISTRY,
)

COST_ESTIMATION_ATTEMPTS_TOTAL = Counter(
    "querygate_cost_estimation_attempts_total",
    "Pre-execution Postgres cost-estimation attempts — execute() calls where "
    "Policy.cost_estimation_enabled is true and the dialect is postgresql — "
    "by connection. Pairs with querygate_cost_estimation_unavailable_total to "
    "compute a fail-open rate.",
    ["connection"],
    registry=REGISTRY,
)

COST_ESTIMATION_UNAVAILABLE_TOTAL = Counter(
    "querygate_cost_estimation_unavailable_total",
    "Cost-estimation attempts that failed to produce a usable estimate. The "
    "gate fails open (TODO.md item 26), so the query still ran, but this "
    "guardrail did not evaluate it — alert on this climbing, since it means "
    "max_estimated_rows/max_estimated_cost has silently stopped protecting "
    "this connection.",
    # reason: compile_failed (statement can't render with literal binds) |
    # explain_failed (the EXPLAIN itself errored) | plan_parse_failed
    # (unexpected plan JSON shape) — see execution/cost_estimation.py.
    ["connection", "reason"],
    registry=REGISTRY,
)

COST_ESTIMATION_WOULD_REJECT_TOTAL = Counter(
    "querygate_cost_estimation_would_reject_total",
    "Queries that would have been rejected by the cost-estimate gate while "
    "Policy.cost_estimation_mode is 'observe' — did not actually block the "
    "query. Use this (and the accompanying "
    "cost_estimation.observed_would_reject log line) to calibrate "
    "max_estimated_rows/max_estimated_cost against real traffic before "
    "switching a connection over to 'enforce'.",
    ["connection"],
    registry=REGISTRY,
)


USAGE_SIGNALS_BUFFERED_TOTAL = Counter(
    "querygate_usage_signals_buffered_total",
    "Redaction-safe usage signals (TODO.md item 32C) enqueued into the "
    "in-process buffer by successful query execution, by connection and "
    "kind. Buffering never touches the catalog file lock — see "
    "querygate_usage_signals_recorded_total for the batched flush outcome.",
    ["connection", "kind"],
    registry=REGISTRY,
)

USAGE_SIGNAL_BUFFER_DROPPED_TOTAL = Counter(
    "querygate_usage_signal_buffer_dropped_total",
    "Usage signals dropped because a connection's in-process buffer was at "
    "capacity (SEMANTIC_MEMORY_USAGE_SIGNAL_BUFFER_SIZE) before the next "
    "background flush — a sustained non-zero rate means the flush interval "
    "is too long for this connection's query volume.",
    ["connection"],
    registry=REGISTRY,
)

USAGE_SIGNALS_RECORDED_TOTAL = Counter(
    "querygate_usage_signals_recorded_total",
    "Usage signals persisted into the catalog file by the batched "
    "background flush, by connection and outcome (recorded vs. a replayed "
    "duplicate signal_id that was a no-op).",
    ["connection", "outcome"],  # outcome: recorded | duplicate
    registry=REGISTRY,
)

LEARNED_PROPOSALS_GENERATED_TOTAL = Counter(
    "querygate_learned_proposals_generated_total",
    "Background usage-learner runs, by connection and outcome (generated | "
    "idempotent | no evidence crossed the support/confidence threshold).",
    ["connection", "outcome"],
    registry=REGISTRY,
)


def classify_rejection(exc: BaseException) -> str:
    if isinstance(exc, QueueFullError):
        return "queue_full"
    if isinstance(exc, ConcurrencyLimitError):
        return "concurrency"
    if isinstance(exc, CostEstimateExceededError):
        return "cost_estimate"
    if isinstance(exc, PolicyViolationError):
        return "policy"
    if isinstance(exc, ValueError):
        return "schema"
    return "db_error"


def render_latest() -> bytes:
    return generate_latest(REGISTRY)


__all__ = [
    "CONTENT_TYPE_LATEST",
    "REGISTRY",
    "QUERIES_TOTAL",
    "QUERIES_REJECTED_TOTAL",
    "QUERY_DURATION_SECONDS",
    "CONCURRENCY_IN_USE",
    "CONCURRENCY_MAX",
    "QUEUE_DEPTH",
    "QUEUE_WAIT_SECONDS",
    "COST_ESTIMATION_ATTEMPTS_TOTAL",
    "COST_ESTIMATION_UNAVAILABLE_TOTAL",
    "COST_ESTIMATION_WOULD_REJECT_TOTAL",
    "USAGE_SIGNALS_BUFFERED_TOTAL",
    "USAGE_SIGNAL_BUFFER_DROPPED_TOTAL",
    "USAGE_SIGNALS_RECORDED_TOTAL",
    "LEARNED_PROPOSALS_GENERATED_TOTAL",
    "classify_rejection",
    "render_latest",
]
