---
name: test-gap
description: >-
  Find under-tested code and missing coverage classes, then propose (or write) the
  specific tests to close the gaps. Use when asked to "improve test coverage",
  "find untested code", after adding a feature, or before a release. Enforces the
  unit/integration/security/real-db coverage expectation.
---

# test-gap — find and close coverage gaps

Not just a line-coverage number — the goal is the *right kinds* of tests for a
security-critical data gateway.

## Measure

```bash
poetry run pytest --cov=src --cov-report=term-missing
```

Note the low-coverage modules and, more importantly, the *uncovered branches* in
security-sensitive code (validation, compiler, execution, auth, catalog
governance, audit).

## Gap classes to check (beyond line coverage)

1. **Security-path branches** — every rejection path in
   `validation/policy_validation.py` and `validation/schema_validation.py` should
   have a test proving it rejects. Uncovered rejection branches are the dangerous
   ones. (See the `adversarial-probe` skill for boundary attacks.)
2. **Dialect parity** — for each `DialectAdapter` method, is there a test per
   dialect asserting the rendered SQL *and* a test that the unsupported-on-a-
   dialect path raises `QueryValidationError`? Missing dialect tests are a common
   gap.
3. **Real-DB coverage** — behavior that only manifests against a live engine
   (timeouts, concurrency, MSSQL-specific SQL) needs `real_db`/`postgres_live`/
   MSSQL tests, not just SQLite/mocked ones. These are excluded from the default
   run — check they exist and run in the dedicated jobs.
4. **Redaction/credential invariants** — new returned fields or audit fields need
   a test proving they don't leak (extend `test_credential_redaction.py` /
   audit-sink tests rather than trusting convention).
5. **Error contracts** — new endpoints/tools: a test that malformed input yields
   a clean 4xx/`QueryValidationError`, never a 500 or leaked driver string.

## Procedure

1. Rank gaps by risk (security path > correctness > cosmetics), not by raw % .
2. Propose the concrete tests to add, with file + what each asserts. If asked to
   write them, follow `tests/conftest.py` conventions (fresh demo registry,
   `in_process_limiter().clear()`, patch `_load_table` to unit-test schema
   validation, monkeypatch `get_engine`/`session_scope` for SQLite paths).
3. Run the suite; confirm new tests pass and actually fail when the behavior is
   broken (a test that can't fail proves nothing).

## Report

Ranked gap list (risk-ordered) with file:line of the uncovered path, the test
proposed/added for each, and the before/after coverage on the touched modules.
