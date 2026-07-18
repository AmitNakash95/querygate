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

from querygate.core.exceptions import ConcurrencyLimitError, PolicyViolationError

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


def classify_rejection(exc: BaseException) -> str:
    if isinstance(exc, ConcurrencyLimitError):
        return "concurrency"
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
    "classify_rejection",
    "render_latest",
]
