---
id: troubleshooting.errors
title: Understand safe errors and troubleshoot failures
summary: QueryGate returns actionable validation failures while masking raw database and internal exceptions that could expose sensitive deployment details.
tags: [troubleshooting, errors, validation, not-found, denied, timeout, concurrency, request-id]
next_actions:
  - Use the stable error code with explain_querygate_error.
  - Give operators the X-Request-ID for masked internal failures.
---
An unavailable connection, table, or column may be unknown or hidden by the
caller's effective policy. QueryGate intentionally avoids confirming which
case applies. Use connection and schema discovery, then retry only with a
returned identifier.

Validation errors are safe and actionable: correct the structured query,
narrow its shape, or fix every reported configuration error. Policy-limit
failures require a smaller request or an administrator review. A concurrency
failure should be retried once after a delay, never in a tight loop.

Unexpected database-driver and internal exceptions are replaced with a stable
public message because raw errors can contain SQL, values, hosts, paths, or
credentials. Record the response's `X-Request-ID` and timestamp so an operator
can correlate the failure with protected structured logs. Do not paste
credentials or customer rows into support prompts.
