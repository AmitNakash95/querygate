"""Admin observability overview (TODO.md item 44, phase 1).

Item 31's admin surface can browse *individual* audit events, but it can't
answer trend questions — "which reason rejects the most queries?", "is queue
pressure rising?", "did cost-estimation availability regress?". The raw
signals for those already exist as the in-process Prometheus metrics
(`querygate/metrics.py`); this module aggregates that registry into one typed,
redaction-safe overview an admin API can return.

**Honesty about durability (item 44's explicit requirement).** This is a
*current-process snapshot*, not durable history: counters are cumulative
since this process started and reset to zero on restart, gauges are the
instantaneous value, and — with the default in-process concurrency/quota
backends — everything is per-replica, not fleet-wide. The model says so
(`source="process_snapshot"`, `durable=False`, `since`, `note`) rather than
implying a time-series store QueryGate does not own. Querying an
operator-configured external metrics backend for real history is phase 2.

**Redaction posture.** The snapshot is built only from metric labels, which
are already low-cardinality and public by construction (`connection` is a
`PublicConnectionInfo` id; `reason`/`outcome`/`quota_kind` are fixed enum
buckets — see `metrics.py`). It carries no SQL, predicate value, row, principal,
table, or column — the same invariant as `audit/logger.py` and `metrics.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

import pydantic as pyd
from prometheus_client import CollectorRegistry

from querygate.metrics import REGISTRY

# Captured once, at import — a truthful "no data predates this" marker for the
# snapshot. Close enough to process start for an operational overview; the
# point is to label the window, not to time it to the millisecond.
_PROCESS_START = datetime.now(timezone.utc)

_SNAPSHOT_NOTE = (
    "Current-process snapshot: counters are cumulative since this process "
    "started and reset on restart; gauges are instantaneous. With the default "
    "in-process concurrency/quota backends these are per-replica, not "
    "fleet-wide. This is not durable history."
)


class WaitStat(pyd.BaseModel):
    """Aggregate of a duration/wait histogram: enough to show an average
    without exposing per-request timings."""

    count: int = 0
    total_seconds: float = 0.0
    avg_seconds: Optional[float] = None

    model_config = pyd.ConfigDict(extra="forbid")


class CostEstimationStat(pyd.BaseModel):
    """Cost-estimation gate health (TODO.md item 26). `fail_open_rate` is the
    share of attempts that could not produce a usable estimate and therefore
    let the query through unchecked — the number to alert on."""

    attempts: int = 0
    unavailable: int = 0
    unavailable_by_reason: Dict[str, int] = pyd.Field(default_factory=dict)
    would_reject: int = 0
    fail_open_rate: Optional[float] = None

    model_config = pyd.ConfigDict(extra="forbid")


class ConnectionObservability(pyd.BaseModel):
    """Per-connection rollup. Every field is an aggregate count/gauge — never a
    query, value, or principal."""

    connection: str
    queries_total: int = 0
    queries_success: int = 0
    queries_rejected: int = 0
    rejections_by_reason: Dict[str, int] = pyd.Field(default_factory=dict)
    duration: WaitStat = pyd.Field(default_factory=WaitStat)
    concurrency_in_use: float = 0.0
    concurrency_max: float = 0.0
    concurrency_utilization: Optional[float] = None
    queue_depth: float = 0.0
    queue_wait_by_outcome: Dict[str, WaitStat] = pyd.Field(default_factory=dict)
    quota_rejections_by_kind: Dict[str, int] = pyd.Field(default_factory=dict)
    cost_estimation: CostEstimationStat = pyd.Field(default_factory=CostEstimationStat)

    model_config = pyd.ConfigDict(extra="forbid")


class ObservabilityOverview(pyd.BaseModel):
    """Global overview + per-connection breakdown. Admin-scoped; see the module
    docstring for the durability and redaction posture."""

    source: str = "process_snapshot"
    durable: bool = False
    since: str
    note: str = _SNAPSHOT_NOTE

    queries_total: int = 0
    queries_success: int = 0
    queries_rejected: int = 0
    rejections_by_reason: Dict[str, int] = pyd.Field(default_factory=dict)
    duration: WaitStat = pyd.Field(default_factory=WaitStat)

    queue_depth_total: float = 0.0
    queue_wait_by_outcome: Dict[str, WaitStat] = pyd.Field(default_factory=dict)

    concurrency_in_use_total: float = 0.0
    concurrency_max_total: float = 0.0
    concurrency_utilization: Optional[float] = None

    quota_rejections_by_kind: Dict[str, int] = pyd.Field(default_factory=dict)
    cost_estimation: CostEstimationStat = pyd.Field(default_factory=CostEstimationStat)

    by_connection: List[ConnectionObservability] = pyd.Field(default_factory=list)

    model_config = pyd.ConfigDict(extra="forbid")


class _ConnAccumulator:
    """Mutable per-connection scratch, finalized into a ConnectionObservability."""

    def __init__(self) -> None:
        self.queries_success = 0
        self.queries_rejected = 0
        self.rejections_by_reason: Dict[str, int] = {}
        self.duration_count = 0
        self.duration_sum = 0.0
        self.concurrency_in_use = 0.0
        self.concurrency_max = 0.0
        self.queue_depth = 0.0
        self.wait_count: Dict[str, int] = {}
        self.wait_sum: Dict[str, float] = {}
        self.quota_by_kind: Dict[str, int] = {}
        self.cost_attempts = 0
        self.cost_unavailable_by_reason: Dict[str, int] = {}
        self.cost_would_reject = 0


def _avg(total: float, count: int) -> Optional[float]:
    return (total / count) if count else None


def _rate(part: int, whole: int) -> Optional[float]:
    return (part / whole) if whole else None


def _wait_stat(count: int, total: float) -> WaitStat:
    return WaitStat(count=count, total_seconds=total, avg_seconds=_avg(total, count))


def build_overview(registry: CollectorRegistry = REGISTRY) -> ObservabilityOverview:
    """Aggregate `registry`'s current samples into an ObservabilityOverview.

    Pure over the registry it's given (defaults to the live one) — pass a fresh
    CollectorRegistry to test the aggregation in isolation. Reads exact emitted
    sample names (`*_total` counters, `*_count`/`*_sum` histogram parts,
    plain-named gauges), so it never depends on prometheus_client's internal
    counter-naming conventions.
    """
    conns: Dict[str, _ConnAccumulator] = {}

    def acc(labels: Dict[str, str]) -> _ConnAccumulator:
        connection = labels.get("connection", "")
        return conns.setdefault(connection, _ConnAccumulator())

    for metric in registry.collect():
        for sample in metric.samples:
            name = sample.name
            labels = sample.labels
            value = sample.value

            if name == "querygate_queries_total":
                a = acc(labels)
                if labels.get("status") == "success":
                    a.queries_success += int(value)
                elif labels.get("status") == "rejected":
                    a.queries_rejected += int(value)
            elif name == "querygate_queries_rejected_total":
                a = acc(labels)
                reason = labels.get("reason", "unknown")
                a.rejections_by_reason[reason] = a.rejections_by_reason.get(reason, 0) + int(value)
            elif name == "querygate_query_duration_seconds_count":
                acc(labels).duration_count += int(value)
            elif name == "querygate_query_duration_seconds_sum":
                acc(labels).duration_sum += value
            elif name == "querygate_concurrency_in_use":
                acc(labels).concurrency_in_use += value
            elif name == "querygate_concurrency_max":
                acc(labels).concurrency_max += value
            elif name == "querygate_queue_depth":
                acc(labels).queue_depth += value
            elif name == "querygate_queue_wait_seconds_count":
                a = acc(labels)
                outcome = labels.get("outcome", "unknown")
                a.wait_count[outcome] = a.wait_count.get(outcome, 0) + int(value)
            elif name == "querygate_queue_wait_seconds_sum":
                a = acc(labels)
                outcome = labels.get("outcome", "unknown")
                a.wait_sum[outcome] = a.wait_sum.get(outcome, 0.0) + value
            elif name == "querygate_query_quota_rejections_total":
                a = acc(labels)
                kind = labels.get("quota_kind", "unknown")
                a.quota_by_kind[kind] = a.quota_by_kind.get(kind, 0) + int(value)
            elif name == "querygate_cost_estimation_attempts_total":
                acc(labels).cost_attempts += int(value)
            elif name == "querygate_cost_estimation_unavailable_total":
                a = acc(labels)
                reason = labels.get("reason", "unknown")
                a.cost_unavailable_by_reason[reason] = a.cost_unavailable_by_reason.get(
                    reason, 0
                ) + int(value)
            elif name == "querygate_cost_estimation_would_reject_total":
                acc(labels).cost_would_reject += int(value)

    overview = ObservabilityOverview(since=_PROCESS_START.isoformat())

    for connection in sorted(conns):
        a = conns[connection]
        cost_unavailable = sum(a.cost_unavailable_by_reason.values())
        conn_model = ConnectionObservability(
            connection=connection,
            queries_total=a.queries_success + a.queries_rejected,
            queries_success=a.queries_success,
            queries_rejected=a.queries_rejected,
            rejections_by_reason=dict(a.rejections_by_reason),
            duration=_wait_stat(a.duration_count, a.duration_sum),
            concurrency_in_use=a.concurrency_in_use,
            concurrency_max=a.concurrency_max,
            concurrency_utilization=_rate_float(a.concurrency_in_use, a.concurrency_max),
            queue_depth=a.queue_depth,
            queue_wait_by_outcome={
                outcome: _wait_stat(a.wait_count.get(outcome, 0), a.wait_sum.get(outcome, 0.0))
                for outcome in sorted(set(a.wait_count) | set(a.wait_sum))
            },
            quota_rejections_by_kind=dict(a.quota_by_kind),
            cost_estimation=CostEstimationStat(
                attempts=a.cost_attempts,
                unavailable=cost_unavailable,
                unavailable_by_reason=dict(a.cost_unavailable_by_reason),
                would_reject=a.cost_would_reject,
                fail_open_rate=_rate(cost_unavailable, a.cost_attempts),
            ),
        )
        overview.by_connection.append(conn_model)

        # Roll the per-connection numbers up into the global overview.
        overview.queries_success += conn_model.queries_success
        overview.queries_rejected += conn_model.queries_rejected
        _merge_counts(overview.rejections_by_reason, conn_model.rejections_by_reason)
        overview.duration.count += conn_model.duration.count
        overview.duration.total_seconds += conn_model.duration.total_seconds
        overview.queue_depth_total += conn_model.queue_depth
        for outcome, stat in conn_model.queue_wait_by_outcome.items():
            g = overview.queue_wait_by_outcome.setdefault(outcome, WaitStat())
            g.count += stat.count
            g.total_seconds += stat.total_seconds
        overview.concurrency_in_use_total += conn_model.concurrency_in_use
        overview.concurrency_max_total += conn_model.concurrency_max
        _merge_counts(overview.quota_rejections_by_kind, conn_model.quota_rejections_by_kind)
        overview.cost_estimation.attempts += conn_model.cost_estimation.attempts
        overview.cost_estimation.would_reject += conn_model.cost_estimation.would_reject
        _merge_counts(
            overview.cost_estimation.unavailable_by_reason,
            conn_model.cost_estimation.unavailable_by_reason,
        )

    overview.queries_total = overview.queries_success + overview.queries_rejected
    overview.duration.avg_seconds = _avg(overview.duration.total_seconds, overview.duration.count)
    for stat in overview.queue_wait_by_outcome.values():
        stat.avg_seconds = _avg(stat.total_seconds, stat.count)
    overview.concurrency_utilization = _rate_float(
        overview.concurrency_in_use_total, overview.concurrency_max_total
    )
    overview.cost_estimation.unavailable = sum(
        overview.cost_estimation.unavailable_by_reason.values()
    )
    overview.cost_estimation.fail_open_rate = _rate(
        overview.cost_estimation.unavailable, overview.cost_estimation.attempts
    )
    return overview


def _rate_float(part: float, whole: float) -> Optional[float]:
    return (part / whole) if whole else None


def _merge_counts(into: Dict[str, int], src: Dict[str, int]) -> None:
    for key, val in src.items():
        into[key] = into.get(key, 0) + val
