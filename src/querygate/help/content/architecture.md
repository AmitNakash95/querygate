---
id: product.how-it-works
title: How QueryGate works
summary: QueryGate accepts structured queries, validates them against schema and policy, compiles parameterized SQL, and returns bounded results.
tags: [architecture, structured-query, validation, policy, compiler, execution]
next_actions:
  - List your visible connections, then describe a table before constructing a query.
---
QueryGate sits between an agent and one or more databases. Callers never send
raw SQL. They submit a typed `StructuredQuery` containing tables, selected
columns, filters, joins, grouping, ordering, and a bounded result limit.

For every request QueryGate first resolves the authenticated principal and the
effective per-connection policy. It verifies that the connection is visible,
reflects or loads the permitted schema, rejects denied or unknown identifiers,
applies mandatory row filters, enforces complexity and execution guardrails,
and only then compiles parameterized SQL through SQLAlchemy. Query results are
bounded by both row count and serialized response size.

REST and MCP are transport adapters over the same validation, policy,
compilation, execution, visibility, audit, and catalog services. A rule is not
considered a security boundary unless both surfaces enforce it consistently.
