---
id: operations.observability
title: Operate and observe QueryGate
summary: Use health, metrics, structured logs, persisted audit events, timeouts, and concurrency controls to operate QueryGate safely.
tags: [operations, health, readiness, metrics, prometheus, logs, audit, timeout, concurrency, redis, connections]
next_actions:
  - Monitor health, rejection counters, latency, truncation, and audit-sink failures.
---
`/health` reports service version and aggregate connection health without
revealing connection identifiers. For per-connection operational detail,
`GET /api/v1/admin/connections` (scope `admin:connections:read`) returns a
credential-free status for each configured connection — dialect, enabled state,
healthy/degraded/disabled/unknown status, last check and last success, latency,
schema-reflected state, and a stable redacted failure category — never a
connection string or raw driver error. `POST
/api/v1/admin/connections/{id}/test` (its own scope, `admin:connections:test`,
independent of the read scope) triggers an immediate re-check of one
connection instead of waiting for the next background interval, rate-limited
to one manual probe per connection per cooldown window. `/metrics` provides Prometheus-format
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
