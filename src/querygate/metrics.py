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
    # query. concurrency: too many in-flight queries for this connection.
    # cost_estimate: rejected by a pre-execution Postgres EXPLAIN cost check
    # (TODO.md item 26) — broken out from the coarser `policy` bucket so
    # operators can tell threshold tuning apart from allow/deny rules.
    # db_error: everything else, including genuine query timeouts — see
    # TODO.md item 3, which hasn't yet established a reliable, dialect-
    # verified way to distinguish a timeout from any other DB-layer failure.
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


def classify_rejection(exc: BaseException) -> str:
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
    "COST_ESTIMATION_ATTEMPTS_TOTAL",
    "COST_ESTIMATION_UNAVAILABLE_TOTAL",
    "COST_ESTIMATION_WOULD_REJECT_TOTAL",
    "classify_rejection",
    "render_latest",
]
