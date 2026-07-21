---
name: repo-audit
description: >-
  Whole-repository sweep for security-invariant drift — the periodic,
  codebase-wide counterpart to the diff-scoped security-invariant-check. Use for
  a scheduled/periodic health check, before a release, or when asked to "audit the
  repo", "check for drift", or "make sure the invariants still hold everywhere".
  Reports findings; does not silently fix.
---

# repo-audit — codebase-wide invariant sweep

`security-invariant-check` audits a diff. This audits the **whole tree** for the
slow drift that a per-change review can miss. Read-only inspection + report.

## Sweeps

### 1. No path to a DB except through the pipeline
Every REST route and MCP tool must be a thin wrapper around
`StructuredQueryService`. Find any direct engine/session use, `text()`, string-
built SQL, or DB call that skips `validation → compiler → execution`.

```bash
grep -rnE "\btext\(|execute\(|\.raw|f\"\"\"?\s*(SELECT|INSERT|UPDATE|DELETE)" src/ | grep -v test
grep -rn "get_engine\|session_scope" src/querygate | grep -v -E "connections/|execution/"
```

### 2. No credential on a returned/public model
`PublicConnectionInfo` must have no credential field; only it (never
`ConnectionProfile`) is returned from REST/MCP.

```bash
poetry run pytest tests/unit/test_credential_redaction.py -q
grep -rn "connection_string\|password\|dsn" src/querygate/api src/querygate/mcp
```

### 3. Single catalog mutation path
All catalog writes go through `CatalogFileRepository`'s lock. Flag any second
catalog file, catalog DB, or catalog content routed through
`admin/store.ConfigVersionStore`. Confirm draft proposals never merge/index
without the 32B-1 gate, and `import_connection` still remaps
`version_id`/`generation_id`.

```bash
grep -rn "ConfigVersionStore" src/querygate/catalog
grep -rn "open(.*CATALOG_FILE\|catalog.yaml" src/querygate | grep -v catalog/
```

### 4. Redaction-safe audit
No audit sink writes SQL, predicate values, rows, exceptions, or credentials.
Inspect `audit/sinks.py` and any new logging near the pipeline.

### 5. Dialect dispatch hygiene
No inline `if dialect ==` at compiler call sites (must go through
`get_dialect_adapter`). Known deliberate exceptions: `connections/dialects.py`,
`execution/service.py`'s Postgres-only cost gate.

```bash
grep -rnE "if .*dialect\s*==|dialect\s*==\s*[\"']" src/querygate/compiler
```

### 6. Status-code hygiene
HTTP status codes use starlette's `status` object, not raw ints (repo preference).

```bash
grep -rnE "status_code\s*=\s*[0-9]{3}|HTTPException\([0-9]{3}" src/querygate/api
```

### 7. TODO/archive integrity
No duplicate/reused item numbers; every `✅ DONE` (no qualifier) item is a stub
with an archive pointer; no open action item sits in `docs/TODO_ARCHIVE.md`.

## Report

One line per sweep: CLEAN / DRIFT (with file:line and what's wrong). For each
DRIFT, recommend the fix or the TODO item to open — but do not apply fixes as
part of the audit unless asked. A grep hit is a lead, not a verdict: open the
site and confirm before reporting it.
