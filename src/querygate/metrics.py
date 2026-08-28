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
    AdHocQueryNotPermittedError,
    ConcurrencyLimitError,
    ApprovalRequiredError,
    CostEstimateExceededError,
    PolicyViolationError,
    QueueFullError,
    QuotaExceededError,
    SubscriptionExpiredError,
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
    # pre-execution plan-estimate cost check (TODO.md item 26; Postgres EXPLAIN
    # or MSSQL SHOWPLAN_XML) — broken
    # out from the coarser `policy` bucket so operators can tell threshold
    # tuning apart from allow/deny rules. quota: rejected before execution by a
    # per-principal rate/byte quota (TODO.md item 50) — broken out from `policy`
    # so operators can tell cost/rate-budget throttling apart from allow/deny
    # rules; see also querygate_query_quota_rejections_total for the
    # requests-vs-bytes breakdown. db_error: everything else,
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

SUBSCRIPTION_WOULD_BLOCK_TOTAL = Counter(
    "querygate_subscription_would_block",
    "Requests an enforcing subscription gate would have refused, by funnel " "(observe mode only).",
    ["funnel"],
    registry=REGISTRY,
)

QUERY_QUOTA_REJECTIONS_TOTAL = Counter(
    "querygate_query_quota_rejections_total",
    "Execution attempts refused before running by a per-principal quota "
    "(TODO.md item 50), by connection and which cap tripped. quota_kind: "
    "requests (rolling-window request-count cap) | bytes (rolling-window "
    "response-byte cap). The default in-process quota window is per-replica, "
    "like the in-process concurrency limiter, so this counter is "
    "single-instance visibility only under it; with CONCURRENCY_BACKEND=redis "
    "the RedisQuotaLimiter (item 50 phase 2) makes the window a true "
    "cross-replica budget, though each replica still exports its own counter.",
    ["connection", "quota_kind"],
    registry=REGISTRY,
)

DISCLOSURE_BUDGET_REJECTIONS_TOTAL = Counter(
    "querygate_disclosure_budget_rejections_total",
    "Aggregate queries refused before running by a cumulative disclosure "
    "budget (TODO.md item 179), by connection and which cap tripped. "
    "budget_kind: disclosure_shape (one query shape re-run past its per-window "
    "cap — the multi-query differencing signature) | disclosure_table (too "
    "many aggregate queries against one table however the shape varied). "
    "Deliberately NOT labeled by table or principal: which table a prober is "
    "working is exactly the disclosure this guardrail exists to withhold, and "
    "principal is unbounded cardinality. Use the audit stream for attribution.",
    ["connection", "budget_kind"],
    registry=REGISTRY,
)

COST_ESTIMATION_ATTEMPTS_TOTAL = Counter(
    "querygate_cost_estimation_attempts_total",
    "Pre-execution plan-estimation attempts — execute() calls where "
    "Policy.cost_estimation_enabled is true and the dialect has an estimator "
    "(Postgres via inline EXPLAIN, MSSQL via a dedicated SHOWPLAN_XML "
    "connection; item 26 phases 1-2) — by connection. Pairs with querygate_cost_estimation_unavailable_total to "
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
    # explain_failed (the EXPLAIN/SHOWPLAN_XML itself errored) | plan_parse_failed
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

AUDIT_WORM_FLUSHES_TOTAL = Counter(
    "querygate_audit_worm_flushes_total",
    "WORM audit archival flush attempts (TODO.md item 134) — background "
    "batches drained from the in-process buffer and PUT to S3 Object Lock, "
    "whether the PUT itself succeeded or failed (see "
    "querygate_audit_worm_flush_failures_total for the failure half).",
    registry=REGISTRY,
)

AUDIT_WORM_FLUSH_FAILURES_TOTAL = Counter(
    "querygate_audit_worm_flush_failures_total",
    "WORM archival flushes whose S3 PUT failed — fails open (the query path "
    "and the local hash-chained ledger are unaffected), but this is a real "
    "compliance-retention degradation an operator is expected to alert on. "
    "The failed batch is re-queued for a retry, not lost, unless the buffer "
    "is also over capacity (see querygate_audit_worm_buffer_dropped_total).",
    registry=REGISTRY,
)

AUDIT_WORM_EVENTS_ARCHIVED_TOTAL = Counter(
    "querygate_audit_worm_events_archived_total",
    "Individual audit events successfully archived to WORM storage (a "
    "successful flush's batch size, summed).",
    registry=REGISTRY,
)

AUDIT_WORM_BUFFER_DROPPED_TOTAL = Counter(
    "querygate_audit_worm_buffer_dropped_total",
    "Audit events evicted from the WORM in-process buffer before they could "
    "be archived — the buffer filled faster than flushes (or retries after "
    "a flush failure) could drain it. A sustained non-zero rate means "
    "AUDIT_WORM_MAX_BUFFERED_EVENTS or AUDIT_WORM_FLUSH_INTERVAL_SECONDS "
    "need retuning, or the S3 endpoint is down for longer than the buffer "
    "can absorb.",
    registry=REGISTRY,
)

# Managed search over the WORM archive (TODO.md item 134 phase 2,
# audit/worm_search.py). `outcome` is one of "ok" | "rejected" | "error" |
# "disabled" — "rejected" means a bound was violated and no S3 call was made
# at all; "disabled" means the request was served honestly (200) on a
# deployment where WORM archiving is not enabled. "disabled" exists so the
# four labels sum to the request count: without it, the natural
# `rejected / total` alert read 100% on every non-WORM deployment.
# It covers all three bound classes (TODO.md item 185): a missing/over-wide
# time range and an out-of-range limit are counted by
# `build_worm_search_result`, which runs `_validate_window`/`_validate_limit`
# itself before the backend-enabled check and so never reaches
# `search_worm_archive`; cursor rejections are counted inside
# `search_worm_archive`. The first two are unconditional; the CURSOR class is
# counted only when the WORM backend is enabled, because a disabled
# deployment short-circuits at the `source="disabled"` return before the
# cursor is ever decoded. Before item 185 the wrapper's rejections were
# counted nowhere, so this label silently meant "cursor rejections only".
# "error" means S3 failed mid-scan
# (unreachable, misconfigured bucket); "ok" covers every genuinely served
# request, complete or truncated.
AUDIT_WORM_SEARCH_REQUESTS_TOTAL = Counter(
    "querygate_audit_worm_search_requests_total",
    "Managed search requests against the WORM S3 archive, by outcome.",
    ["outcome"],
    registry=REGISTRY,
)

AUDIT_WORM_SEARCH_OBJECTS_SCANNED_TOTAL = Counter(
    "querygate_audit_worm_search_objects_scanned_total",
    "S3 segment objects fetched and parsed while serving WORM search "
    "requests — the real cost driver of a search; watch this alongside "
    "querygate_audit_worm_search_requests_total for a caller repeatedly "
    "paging a wide window.",
    registry=REGISTRY,
)

# TODO.md item 177: the two integrity signals a WORM search can produce, moved
# out of the response body and onto the metrics endpoint. Before this,
# `WormSearchResult.chain_breaks`/`unverified` were visible only to whoever
# read the response of an ad-hoc
# GET /api/v1/admin/observability/worm-search over the right window.
#
# **Scope of the claim, stated precisely** (the item's own audit found the
# first draft overstated it): these make a finding REACHABLE BY ALERTING, not
# continuously monitored. Nothing in QueryGate scans the archive on a
# schedule — `search_worm_archive` has exactly one production caller, the
# scope-gated REST route — so a counter only advances while a search actually
# runs. An operator who wants real monitoring must schedule that search;
# alerting on these alone is only as live as the search cadence.
#
# No labels on either. `PERSONAL_DENIALS_RATE_LIMITED_TOTAL` (below) is the
# precedent, and `AUDIT_WORM_SEARCH_OBJECTS_SCANNED_TOTAL` (above) is the
# unlabelled sibling; the other sibling,
# `AUDIT_WORM_SEARCH_REQUESTS_TOTAL`, carries only a fixed, non-caller-chosen
# `outcome` label. A `connection`/`principal` label here would be
# caller-chosen cardinality and a weak activity oracle over the audit archive
# itself, on a surface whose whole point is that reading it is privileged.
#
# Both are incremented by the RUNNING TOTALS of a single search request, on
# both the served ("ok") and the mid-scan-failure ("error") path — a chain
# break found just before S3 died is a real discovery, and dropping it is the
# exact silent-tamper-signal failure this item exists to close.
AUDIT_WORM_SEARCH_CHAIN_BREAKS_TOTAL = Counter(
    "querygate_audit_worm_search_chain_breaks_total",
    "Chain-break FINDINGS across WORM search requests: a record whose "
    "seq/prev_hash did not continue from its predecessor while its own hash "
    "still verified — or a segment whose first record is not the genuine "
    "genesis, or a resumed page whose incoming link could not be confirmed "
    "within the seed-walk bound (fails closed), or a record whose seq is not "
    "the integer it is typed as even though its own hash recomputes. The strongest "
    "tamper/omission signal this surface "
    "produces — unlike querygate_audit_worm_search_unverified_total it is NOT "
    "explained by a rotated AUDIT_LEDGER_HMAC_KEY. Counts findings per scan, "
    "not distinct segments: a WORM object is immutable, so a real break is "
    "permanent and every later search reaching it counts again. Alert on the "
    "FIRST non-zero increase and treat it as sticky until the affected "
    "segment is triaged; do not alert on an absolute magnitude. Only advances "
    "while a search runs — QueryGate does not scan on a schedule.",
    registry=REGISTRY,
)

AUDIT_WORM_SEARCH_UNVERIFIED_TOTAL = Counter(
    "querygate_audit_worm_search_unverified_total",
    "Envelope-shaped lines found during WORM search that did not verify under "
    "the configured AUDIT_LEDGER_HMAC_KEY. Usually a rotated or mismatched "
    "key rather than tampering — treat a sustained rate as a configuration "
    "signal first. Deliberately includes the one breaking line of each chain "
    "break (whose own hash DID recompute), mirroring "
    "WormSearchResult.unverified, so unverified_total - chain_breaks_total is "
    "the part actually likely to be a key mismatch. Excludes `malformed` "
    "lines (not envelope-shaped, unparseable, rejected by the event schema, "
    "or rejected for forbidden content nested in query_shape), which are a "
    "distinct class and are NOT published as a counter at all. Counts "
    "findings per scan, not distinct lines — see the chain-breaks counter.",
    registry=REGISTRY,
)

VERDICTS_TOTAL = Counter(
    "querygate_verdicts_total",
    "Caller-facing verdict() calls (TODO.md item 133), by connection and "
    "outcome. outcome is deliberately restricted to allowed | denied — never "
    "a policy-vs-schema reason breakdown, since verdict()'s whole guarantee "
    "(docs/THREAT_MODEL.md QG-34) is that a denial never distinguishes its "
    "real cause; a reason label here would republish exactly the oracle "
    "verdict()'s response body collapses. A quota/concurrency failure is a "
    "system-busy state, not a shape verdict, so it is not counted here — see "
    "querygate_query_quota_rejections_total instead.",
    ["connection", "outcome"],  # outcome: allowed | denied
    registry=REGISTRY,
)

VERDICT_DURATION_SECONDS = Histogram(
    "querygate_verdict_duration_seconds",
    "verdict() duration in seconds, by connection (both allowed and denied "
    "outcomes — unlike querygate_query_duration_seconds, a denied verdict's "
    "duration is still real validation/compile time, not noise).",
    ["connection"],
    registry=REGISTRY,
)

PERSONAL_DENIALS_RATE_LIMITED_TOTAL = Counter(
    "querygate_personal_denials_rate_limited_total",
    "GET /help/my-recent-denials requests rejected by TODO.md item 126's "
    "per-principal cooldown. No labels — this is a self-service, "
    "no-admin-scope endpoint, so a `principal` label would be caller-chosen "
    "cardinality and, combined with a burst, a weak per-principal activity "
    "oracle to anyone who can read /metrics; a single counter still gives an "
    "operator a real signal that the rate limit item 126 added is actually "
    "engaging, without either cost. Deliberately a metric, not a persisted "
    "audit event: an audit event per 429 would let a caller inflate the same "
    "JSONL file this endpoint scans, making every principal's subsequent "
    "scan more expensive — a self-amplifying feedback loop this metric "
    "avoids by construction.",
    registry=REGISTRY,
)


def reset_subscription_metrics() -> None:
    """Clear the observe-mode counter between tests.

    The collector lives in the process-global `REGISTRY`, which `tests/conftest.py`
    resets nothing else in — so without this, any absolute assertion on the
    counter is order-dependent and passes or fails on which tests ran first.
    """
    SUBSCRIPTION_WOULD_BLOCK_TOTAL.clear()


def record_subscription_would_block(funnel: str) -> None:
    """Observe mode suppressed a refusal that enforce would have made.

    The metric an operator watches for the whole cutover: it is the difference
    between "enforcement is safe to turn on" and "turning it on will page me".
    Labelled by funnel because reads, writes and schema discovery are three
    separate enforcement points and knowing which one would have refused is the
    actionable half.
    """
    SUBSCRIPTION_WOULD_BLOCK_TOTAL.labels(funnel=funnel).inc()


def classify_rejection(exc: BaseException) -> str:
    # First: a billing state is not a policy denial. Without this branch the
    # metric blames `policy` and the tamper-evident ledger records the
    # customer's own policy refusing a query it actually permits.
    if isinstance(exc, SubscriptionExpiredError):
        return "subscription_expired"
    if isinstance(exc, QueueFullError):
        return "queue_full"
    if isinstance(exc, ConcurrencyLimitError):
        return "concurrency"
    if isinstance(exc, CostEstimateExceededError):
        return "cost_estimate"
    # Checked before PolicyViolationError: QuotaExceededError subclasses it, and
    # a rate/budget throttle is a distinct operational signal from allow/deny.
    if isinstance(exc, QuotaExceededError):
        return "quota"
    # Also before PolicyViolationError: a paused-for-approval query (item 92) is
    # a distinct signal from a hard allow/deny rejection.
    if isinstance(exc, ApprovalRequiredError):
        return "approval_required"
    # Also before PolicyViolationError (item 195): "this principal is narrowed
    # to curated templates and something submitted a free-form query" is the
    # signal an operator watches during a templates-only cutover, and it is
    # unactionable if it is indistinguishable from a denied table or an
    # exceeded cap. Low cardinality — one more fixed label value.
    if isinstance(exc, AdHocQueryNotPermittedError):
        return "templates_only"
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
    "QUERY_QUOTA_REJECTIONS_TOTAL",
    "DISCLOSURE_BUDGET_REJECTIONS_TOTAL",
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
    "AUDIT_WORM_FLUSHES_TOTAL",
    "AUDIT_WORM_FLUSH_FAILURES_TOTAL",
    "AUDIT_WORM_EVENTS_ARCHIVED_TOTAL",
    "AUDIT_WORM_BUFFER_DROPPED_TOTAL",
    "AUDIT_WORM_SEARCH_REQUESTS_TOTAL",
    "AUDIT_WORM_SEARCH_OBJECTS_SCANNED_TOTAL",
    "AUDIT_WORM_SEARCH_CHAIN_BREAKS_TOTAL",
    "AUDIT_WORM_SEARCH_UNVERIFIED_TOTAL",
    "VERDICTS_TOTAL",
    "VERDICT_DURATION_SECONDS",
    "PERSONAL_DENIALS_RATE_LIMITED_TOTAL",
    "classify_rejection",
    "render_latest",
]
