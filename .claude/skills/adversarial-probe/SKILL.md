---
name: adversarial-probe
description: >-
  Systematically attack QueryGate's request boundary to find ways a caller could
  bypass the guardrails, then codify each vector as a regression test in the
  security suite. Use when hardening the AST/policy boundary, after adding a new
  AST field/endpoint/MCP tool, or when asked to "try to break it", "find a bypass",
  or extend the adversarial suite. Builds on TODO.md item 36's boundary fuzzing.
---

# adversarial-probe — try to break the boundary, then lock it

Purpose: think like an attacker against the core invariant — **no
caller-controlled raw SQL reaches the database** — and against every policy cap,
then turn each finding into a permanent test in `make test-security`.

This is authorized security testing of QueryGate itself. The output is defensive:
regression tests and, where a real gap is found, a fix.

## Attack surface to sweep

Work through each transport (REST `api/routes.py`, MCP `mcp/tools/*.py`) and the
shared `StructuredQueryService`. For every attack, the expected result is a clean
rejection (`QueryValidationError` / 4xx), **never** a 500, a leaked driver
string, or the operation succeeding.

1. **Raw-SQL smuggling** — attempt to inject SQL fragments through *every*
   string-bearing AST field: identifiers (table/column/alias), literal values,
   function names, ORDER BY, CASE expressions, delimiters, template parameter
   values. Try comment sequences, stacked statements (`;`), quote-breaking,
   `--`, `/* */`, backtick/bracket tricks per dialect.
2. **Policy-cap bypass** — exceed max joins / select width / where-depth /
   group-by / top_n; reference a denied table/column somewhere *other than*
   `select` (nested where, join condition, order by, having) to test that policy
   checks every column reference, not just projected ones.
3. **Cross-connection escapes** — a join's own `connection` field pointing at a
   connection outside the `join_group`; mismatched dialects.
4. **Schema-validation evasion** — non-existent tables/columns, case-tricks,
   Unicode homoglyphs, whitespace/zero-width in identifiers.
5. **Malformed / hostile input** — oversized payloads, deep nesting, huge string
   lengths, null bytes, invalid enum values, type confusion (list where scalar
   expected), duplicate keys.
6. **Catalog-as-execution** — attempt to use catalog search/draft paths to reach
   row values or execute a query (the catalog must stay descriptive-only).
7. **Credential / info leak** — confirm no error path returns a connection
   string, driver exception text, or internal path.

## Procedure

1. Enumerate candidate vectors for the surface you're targeting (use the list
   above as a checklist; note which are new since item 36).
2. For each, write a focused test asserting the clean-rejection contract. Follow
   the existing style in the security suite (`tests/` security-marked tests;
   `make test-security`).
3. Run `make test-security`. If any probe *succeeds* (a real bypass), that's a
   security bug — stop and fix the guardrail (or open a TODO item with the
   reproduction), then re-run. Never leave a passing bypass documented as
   "known".
4. Cross-check `docs/THREAT_MODEL.md` — add/adjust a QG entry if the vector is a
   newly-covered class.

## Report

List each vector tried, its result (rejected as expected / bug found), the tests
added, and any THREAT_MODEL update. If a real bypass was found, describe the fix
and confirm the regression test now fails without it.
