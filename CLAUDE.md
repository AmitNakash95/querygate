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
   into a SQLAlchemy Core `Select`. Dialect-specific behavior (date bucketing,
   `ORDER BY` nulls handling, statistical/aggregate function naming) is
   isolated behind `compiler/dialect_adapters.py`'s `DialectAdapter`
   interface (TODO.md item 73) — one concrete adapter class per dialect
   (`PostgresDialectAdapter`/`MSSQLDialectAdapter`/`SQLiteDialectAdapter`),
   never an inline `if dialect == ...` branch at a call site; everything
   else is dialect-agnostic Core.
4. **`execution/concurrency.py`** — guards actual execution through either a
   single-process semaphore or a Redis-backed distributed limiter.
5. **`connections/engine.py`** — session lifecycle + dialect-specific session
   guardrails (`connections/dialects.py`: Postgres `SET LOCAL
   lock_timeout`/`statement_timeout`, MSSQL `SET LOCK_TIMEOUT`/`XACT_ABORT`).
6. **`audit/logger.py` / `audit/sinks.py`** — every attempt is logged and can
   be persisted as a versioned, redaction-safe JSONL event. Persisted events
   never include SQL, predicate values, rows, exceptions, or credentials.

### Engine philosophy: expose primitives, don't spoon-feed the agent

QueryGate's job is to expose querying that's as expressive and flexible as
real raw SQL — CASE expressions, functions, predicates, ordering, joins,
even multiple round-trip queries — bounded only by what a caller's schema
and the connection's policy allow, and by what the target dialect
*genuinely* supports. It is **not** the engine's job to pre-solve query
construction for the calling agent. The agent is expected to bring its own
reasoning about the user's goal, the schema, and the tools/AST surface
exposed to it, and compose one or more `StructuredQuery`s to get the
result it wants — the same way a human SQL author would hand-write a
workaround using real building blocks, not expect the database to have a
bespoke keyword for every scenario.

This gives a sharper test than "does every dialect support this" alone:

- **Mechanical translation of the same AST-expressed operation into each
  dialect's native syntax is fine** — that's what a compiler is for. Date
  bucketing (Postgres `date_trunc` vs. MSSQL's `DATEADD`/`DATEDIFF` idiom),
  `stddev`/`variance` naming (`STDEV`/`VAR` on MSSQL), `string_agg` vs.
  `STRING_AGG` vs. `group_concat` — every dialect answers the identical
  semantic question in its own idiom. Every `DialectAdapter` method is this
  shape.
- **Do not force feature parity when a dialect genuinely lacks the
  capability** (e.g. `array_agg` — T-SQL has no array/collection type at
  all): implement it for real where it exists, and have the adapter method
  on a dialect that can't raise `QueryValidationError` explaining the gap,
  rather than inventing an emulation.
- **Do not synthesize query structure the AST never asked for**, to paper
  over a dialect's missing keyword — that's the engine solving the
  agent's composition problem instead of exposing a primitive. Concretely
  under active reconsideration: `MSSQLDialectAdapter.order_by_terms`
  (TODO.md item 74) currently injects an *extra CASE-based sort column*
  into the query when `OrderBySpec.nulls` is set, since T-SQL has no
  `NULLS FIRST/LAST` syntax — structure the caller never expressed in the
  AST. An agent can already build that exact CASE-bucket itself with
  primitives QueryGate already exposes (`CaseSelectItem` for the bucket,
  multiple `OrderBySpec` entries for the tie-break); whether the engine
  should keep doing it automatically, or instead reject `nulls` on MSSQL
  the same way `array_agg` will be rejected there, is an open question —
  don't treat it as settled either way without asking first, and don't use
  it as precedent for a similar shortcut elsewhere.

**Exceptions are possible but must be deliberate, not assumed.** If a
specific case seems to genuinely warrant the engine doing more than
mechanical translation — synthesizing structure on the agent's behalf for
a good, concrete reason — that is a design discussion to have explicitly
(with the user, or recorded as a reasoned Decision Log entry in
`docs/PRODUCT_GUIDE.md`) before implementing it, not a default anyone
should reach for under time pressure or convenience. Treat this section's
rule as the default for all new work; an exception must be justified on
its own terms, in the open, the same way item 74's MSSQL nulls handling is
being discussed rather than silently kept or silently reverted.

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

### Composable single-purpose interfaces

Where a piece of behavior genuinely varies by backend/dialect/strategy,
prefer a narrow Protocol/ABC (one or two methods) with one concrete class per
variant, dispatched through a registry (a dict keyed by string/enum, or a
`get_x(...)` lookup function) — never `if X == ...` branching scattered
across call sites. Established precedent: `SecretResolver`
(`secrets/resolvers.py`), `DialectAdapter` (`compiler/dialect_adapters.py`),
`Authenticator` (`core/auth.py`), `AuditSink` (`audit/sinks.py`),
`ConcurrencyLimiter` (`execution/concurrency.py`). Adding a variant means
implementing the interface once and registering it — never hunting through
every call site for a place the old assumption leaked in.

This isn't just swapping one implementation for another — composition is a
first-class option. `core/auth.py`'s `CompositeAuthenticator` chains multiple
`Authenticator`s together (tries API-key, then JWT) rather than picking
exactly one at a time.

**Don't over-apply this.** When a variant is a single stateless expression
with no parameters and no internal branching, a plain dict-of-callables
(`_SCALAR_FNS`, `_RANK_FNS`, `_ADAPTERS` in `compiler/sqlalchemy_compiler.py`/
`dialect_adapters.py`, `_AGGREGATE_SELECT_ITEM_TYPES` in `query_ast/models.py`)
is the right-weight version of the same idea — wrapping a one-liner in a full
class is ceremony, not clarity.

Known, deliberate exceptions to this rule, not oversights:
`connections/dialects.py`'s inline dialect branching (its own docstring
frames centralizing dialect-specific SQL in one module as the goal — a
lighter-weight tradeoff, not a gap) and `execution/service.py`'s
Postgres-only cost-estimation gate (MSSQL's estimated-plan mechanism doesn't
exist yet — TODO.md item 26 phase 2). Don't treat either as precedent for a
new inline branch elsewhere.

### Testing gotchas (see `tests/conftest.py`)

- Every test gets a fresh in-memory "demo" `ConnectionRegistry` and a
  permissive default `Policy` via an autouse fixture — don't rely on
  `examples/*.yaml` being loaded in tests.
- `execution/concurrency.in_process_limiter()` returns the persistent
  `InProcessConcurrencyLimiter` singleton; its `semaphore(connection_id, n)`
  method is a public get-or-create used both by production code and by tests
  seeding a specific scenario (e.g. pre-acquiring a slot). `asyncio.Semaphore`
  objects are bound to the event loop that created them, and pytest-asyncio
  gives each test its own loop — the conftest fixture calls
  `in_process_limiter().clear()` every test. If you add new concurrency
  state, clear it there too.
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
