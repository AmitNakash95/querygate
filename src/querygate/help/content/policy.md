---
id: configuration.policy
title: Configure policies and query guardrails
summary: Policies control connection visibility, permitted schema, mandatory row filters, query complexity, execution, concurrency, and response bounds.
tags: [configuration, policy.yaml, permissions, tables, columns, row-filter, timeout, concurrency, limits]
next_actions:
  - Start production policies with enabled false and grant each principal only its intended connections.
  - Validate a policy change and inspect its preview before staging it.
---
`policy.yaml` has a `default` policy, optional per-connection overrides under
`connections`, and optional per-principal overrides under `principals`.
Overrides are resolved in that order. For production, a deny-by-default policy
is easiest to reason about: disable the default and enable only the connections
each principal needs.

Table and column allow/deny rules affect discovery as well as execution. Deny
rules win. Mandatory row filters are inserted by QueryGate and may take a
static value or resolve from a verified principal claim. Never rely on prompt
instructions as an authorization boundary.

Guardrails include join and expression complexity, row limits, maximum response
bytes, timeout, concurrency, wait duration, and batch size. QueryGate validates
identifiers and policy before compilation, uses bind parameters for values, and
truncates bounded results where documented.
