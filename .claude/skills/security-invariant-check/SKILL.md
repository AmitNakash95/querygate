---
name: security-invariant-check
description: >-
  Audit a change against QueryGate's non-negotiable security invariants before
  committing: no caller-controlled raw SQL path, no credential ever on a
  public/returned model, a single catalog mutation path, and redaction-safe audit
  events. Use before committing security-sensitive changes, or when touching the
  request pipeline, connections/models.py, catalog governance, audit sinks, or any
  REST/MCP surface.
---

# security-invariant-check — guardrail audit before commit

Run this against the working diff. Each invariant below has a test that asserts
it against real schemas, not convention — keep those tests meaningful if the diff
touches the code they cover.

## 1. No caller-controlled raw SQL reaches the database

- There is no `sql` field, endpoint, or MCP tool that accepts raw SQL — anywhere.
  Every REST route and MCP tool is a thin wrapper around `StructuredQueryService`;
  the only input is a validated `StructuredQuery` AST.
- Check the diff added no new path to a DB that bypasses
  `validation/policy_validation.py` → `validation/schema_validation.py` →
  `compiler/` → `execution/`.

```bash
git diff | grep -nE "text\(|execute\(.*(select|SELECT)|f\".*SELECT|\.raw" || echo "no obvious raw-SQL constructs added"
```

## 2. Credentials never leave the process

- `connections/models.py` splits `ConnectionProfile` (holds the real connection
  string) from `PublicConnectionInfo` (id/dialect/enabled/description only — **no
  credential field exists on it**). Only `PublicConnectionInfo` is returned from
  REST/MCP.
- If the diff touched either model or any response shape, confirm no credential
  field was added to a returned/public model.

```bash
poetry run pytest tests/unit/test_credential_redaction.py -q
```

This test asserts against the live OpenAPI schema and MCP tool schemas. If you
changed those models, make sure the test still actually proves the invariant.

## 3. Single catalog mutation path

- All catalog mutations (refresh, generate-drafts, governance
  review/edit/approve/reject/publish/rollback, import/export, retention) go
  through the **one** `CatalogFileRepository` lock. Do **not** add a second
  catalog file, a catalog database, or a second mutation path.
- In particular, never route catalog content through
  `admin/store.ConfigVersionStore` — it snapshots its own copy in
  `var/config_versions/` and would silently diverge from the live `CATALOG_FILE`.
- Draft proposals stay separate from published entries; never indexed/merged
  without the 32B-1 review gate (`catalog/governance.py`).
- `version_id`/`generation_id` are file-global — `import_connection` must keep
  remapping them; don't "simplify" that away.
- The catalog is descriptive: never a query-execution or row-value search path.

## 4. Redaction-safe audit events

- Persisted events (`audit/sinks.py`) never include SQL, predicate values, rows,
  exceptions, or credentials. Confirm any new logged field is safe.

## 5. Run the adversarial suite

```bash
make test-security
```

## Report

State each invariant as HOLDS / AT RISK / N/A (not touched), with the file:line
for anything at risk. Do not commit security-sensitive changes with an invariant
left AT RISK.
