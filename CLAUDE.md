# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

QueryGate is an agent-safe database access gateway: it exposes Postgres/MSSQL
databases to AI agents over MCP and REST, but the only thing a caller can ever
submit is a validated `StructuredQuery` JSON AST — there is no raw-SQL field
or endpoint anywhere in the codebase. See `README.md` for the product pitch,
`docs/RELEASING.md` for release gates, and `archive/extraction/` only when
historical extraction context is explicitly needed.

`docs/PRODUCT_GUIDE.md` is a plain-language, human-facing explainer of the
product's architecture, terms, and technical decisions (also used to answer
marketing/positioning questions). After finishing any non-trivial task,
check whether it introduced or changed something worth recording there —
new architecture, a deliberate tradeoff, a new term/tool, a customer-facing
capability — and update the relevant section (and its Decision Log) if so.
Skip this for pure bug fixes, refactors with no behavioral change, or
test-only changes. See that file's own "Maintenance protocol" section for
the exact rule.

## Commands

```bash
poetry install                          # install deps (into .venv if configured in-project)
cp .env.example .env                    # local env config

# Run the app
poetry run python -m querygate.run      # or: make run
poetry run uvicorn querygate.api.app:app --reload   # or: make run-dev

# Tests
poetry run pytest                       # full suite (unit + integration)
poetry run pytest -m unit               # unit only
poetry run pytest -m integration        # integration only
poetry run pytest tests/unit/test_compiler.py                              # one file
poetry run pytest tests/unit/test_compiler.py::TestCompiler::test_simple_select_sql  # one test
poetry run pytest --cov=src --cov-report=term-missing                      # coverage
make test-security                     # adversarial boundary suite
make test-postgres-live                # requires compose-up
make test-load                         # bounded real-Postgres concurrency load gate
make test-soak SOAK_ROUNDS=100         # repeated guardrail load scenarios

# Formatting
poetry run black --check src/ tests/    # or: make format-check
poetry run black src/ tests/            # or: make format

# Demo database (for manual/local verification against a real Postgres)
docker compose up -d                    # starts querygate-demo-db on localhost:5433, auto-seeded
make release-check                      # source, package, formatting, tests, config
make release-smoke                      # image + real structured Postgres query
poetry run querygate-semantic-memory evaluate  # fixed offline 32A benchmark
```

The default suite excludes tests marked `real_db`; CI also runs dedicated
Postgres and MSSQL jobs. `test_sqlite_end_to_end.py` uses SQLite internally
as a compiler/execution test, but SQLite is not a supported registry dialect.

## Architecture

### The one request pipeline

Every REST route (`api/routes.py`) and every MCP tool (`mcp/tools/*.py`) is a
thin transport wrapper around a single `StructuredQueryService`
(`execution/service.py`). There is no other path to a database. The pipeline,
in order, for `execute`/`explain`:

1. **`validation/policy_validation.py`** — checks the AST against the
   connection's `Policy` (caps: max joins/select/where-depth/group-by/top_n;
   table/column allow-deny checked against *every* column reference in the
   query, not just `select`). Runs first, before any DB touch.
2. **`validation/schema_validation.py`** — reflects and verifies every
   table/column the query references actually exists (via
   `schema/reflection.py`'s cached reflection). Also resolves cross-connection
   joins here (a join's own `connection` field) and enforces the
   `join_group` policy rule.
3. **`compiler/sqlalchemy_compiler.py`** — turns the validated AST + `Policy`
   into a SQLAlchemy Core `Select`. Dialect-specific behavior (date bucketing:
   Postgres `date_trunc` vs MSSQL `DATEADD`/`DATEDIFF` vs SQLite `strftime`)
   is isolated to one function here (`_date_bucket_expr`); everything else is
   dialect-agnostic Core.
4. **`execution/concurrency.py`** — guards actual execution through either a
   single-process semaphore or a Redis-backed distributed limiter.
5. **`connections/engine.py`** — session lifecycle + dialect-specific session
   guardrails (`connections/dialects.py`: Postgres `SET LOCAL
   lock_timeout`/`statement_timeout`, MSSQL `SET LOCK_TIMEOUT`/`XACT_ABORT`).
6. **`audit/logger.py` / `audit/sinks.py`** — every attempt is logged and can
   be persisted as a versioned, redaction-safe JSONL event. Persisted events
   never include SQL, predicate values, rows, exceptions, or credentials.

### Connections and policy are file-configured, not code-configured

`connections/registry.py`, `policy/loader.py`, and `catalog/loader.py` load YAML into process-wide
stores and support an authorized, atomic hot reload. There is no app-owned
configuration database yet. See `examples/connections.example.yaml` and
`examples/policy.example.yaml` for the enforcement shape and
`examples/catalog.example.yaml` for the optional versioned semantic overlay.

Catalog version 2 extends item 27 in place with stable provenance,
schema-only fingerprints/diffs, compact policy-first retrieval, quarantined
manual draft proposals, and opt-in schema refresh. Catalog search must filter
tables, columns, and both relationship endpoints before tokenization/ranking/
counting. Draft proposals stay separate from published entries and must never
be indexed or merged without going through the 32B-1 review gate
(`catalog/governance.py`). Only `disabled` and offline `manual` provider
modes exist; do not add network/provider execution to 32A. Refresh persists
atomically and must stale only affected entries while remaining independent
of query execution. The catalog is descriptive and must never become a
query execution or row-value search path.

32B is fully shipped — governed review/edit/approve/reject/publish/rollback
(32B-1) plus export/import (backup/restore) and retention/deletion (32B-2).
See TODO.md item 32 for the exact scope and `catalog/governance.py`'s module
docstring. All governance mutations go through the same
`CatalogFileRepository` lock as refresh/generation; do not add a second
catalog file, database, or mutation path (in particular, do not route
catalog content through `admin/store.ConfigVersionStore` — that store
snapshots its own copy of catalog.yaml in `var/config_versions/`, which
would silently diverge from the live `CATALOG_FILE` refresh/generate-drafts
already write to). `version_id` and `generation_id` are file-global, not
per-connection — `import_connection` must keep remapping them against the
target catalog's current content; do not "simplify" that away, it exists
specifically to stop one connection's import from corrupting another
connection's history. 32C (adaptive usage learning) has shipped
(`catalog/usage.py`, `catalog/learning.py`,
`catalog/adaptive_learning_benchmark.py`) — stay bounded by its explicit
acceptance criteria in TODO.md item 32 (typed redaction-safe signals only,
per-customer/connection partitioning, no feedback loops, learned content
must go through the existing 32B review path and can never publish itself)
when touching it. Do not add a live LLM call, embedding index, or autonomous
policy edit as a side effect of future work here.

**Security invariant**: `connections/models.py` splits `ConnectionProfile`
(carries the real connection string, resolved from `${ENV_VAR}` in the YAML
file) from `PublicConnectionInfo` (id/dialect/enabled/description only — no
credential field exists on it at all). Only `PublicConnectionInfo` is ever
returned from REST/MCP. `tests/unit/test_credential_redaction.py` asserts
this against the live OpenAPI schema and MCP tool schemas, not just by
convention — keep that test meaningful if you touch either model.

### Auth

`core/auth.py` defines the shared authenticator boundary. Static API keys and
JWKS-verified JWTs can be composed for both REST and MCP; principals carry
subject, scopes, claims, and auth method. Don't duplicate bearer-token logic
inside either transport.

### Testing gotchas (see `tests/conftest.py`)

- Every test gets a fresh in-memory "demo" `ConnectionRegistry` and a
  permissive default `Policy` via an autouse fixture — don't rely on
  `examples/*.yaml` being loaded in tests.
- `execution/concurrency.SEMAPHORES` is a module-level dict of
  `asyncio.Semaphore` keyed by connection id. Semaphores are bound to the
  event loop that created them, and pytest-asyncio gives each test its own
  loop — the conftest fixture clears this dict every test. If you add new
  concurrency state, clear it there too.
- To unit-test `schema_validation.validate_schema` without a real database,
  patch the module-level `_load_table` function (the one seam that touches a
  connection) rather than mocking SQLAlchemy internals.
- `mcp/tools/{connections,schema,query}.py` deliberately do **not** use
  `from __future__ import annotations`. FastMCP resolves each tool's forward
  references against the *wrapping* function's `__globals__` (the
  `safe_mcp_tool` decorator lives in `mcp/exceptions.py`), not the tool
  module's — stringified annotations there fail to resolve at registration
  time. Keep annotations as real objects in those three files.
- `ConnectionProfile.dialect` only accepts `"postgresql"` or `"mssql"` —
  SQLite is used internally for tests/examples by monkeypatching
  `connections.engine.get_engine`/`session_scope` directly (see
  `test_sqlite_end_to_end.py`), never through the registry.

### What's archived, not active

`archive/legacy-project-docs/` holds the previous project's own
Claude-assisted development workflow scaffolding (`memory/`, `agent_state/`,
`journal/`, `workflows/`, `skills/`, `AGENT.md`, `CONSTRAINTS.md`). It's not
imported by any code and not part of the product — ignore it unless
specifically asked to consult old project history.

`archive/extraction/` holds historical migration/cleanup reports. Neither
archive directory is included in package or container release artifacts.
