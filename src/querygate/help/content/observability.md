---
id: operations.observability
title: Operate and observe QueryGate
summary: Use health, metrics, structured logs, persisted audit events, timeouts, and concurrency controls to operate QueryGate safely.
tags: [operations, health, readiness, metrics, prometheus, logs, audit, timeout, concurrency, redis]
next_actions:
  - Monitor health, rejection counters, latency, truncation, and audit-sink failures.
---
`/health` reports service version and aggregate connection health without
revealing connection identifiers. `/metrics` provides Prometheus-format
operational metrics. Structured application logs carry request correlation
IDs, while persisted audit events record normalized query shape, caller,
surface, outcome, timing, and bounded result metadata without predicate
literals or returned rows.

Timeout and concurrency settings are policy guardrails. In-process concurrency
is suitable for a single instance; multiple instances should share the Redis
backend when a global cap is required. Choose fail-open or fail-closed Redis
behavior according to whether availability or strict database protection is
the higher priority.

Alert on unhealthy connections, sustained rejection changes, audit-sink write
failures, unusually high truncation, and latency approaching configured
timeouts. Preserve the `X-Request-ID` when troubleshooting masked failures.
