# Migration report: il-backoffice-api → QueryGate

This documents what was preserved, removed, and generalized when extracting
QueryGate from the `il-backoffice-api` prototype, and what's recommended
before a commercial release.

## What was preserved (the real product core)

The prototype's `shared/grammar/structured/*` subsystem was genuinely
well-built and generic underneath its ShanyContent-specific naming. It ported
with only mechanical renames, not a rewrite:

- **The query AST** (`shared/grammar/structured/models.py` →
  `query_ast/models.py`): `Predicate`/`WhereGroup`/`JoinSpec`/
  `AggregateSelectItem`/`DateBucketSelectItem`/`TopNSpec`/`OrderBySpec` all
  carried over as-is. The `order_by.dir` exact-string validation, the
  `WhereGroup` exactly-one-of-and/or invariant, and the `Predicate` value-shape
  validation (e.g. `between` requiring a 2-element list) are all unchanged —
  they were already correct, defensive design.
- **The SQLAlchemy compiler** (`compiler.py` → `compiler/sqlalchemy_compiler.py`):
  the top-N ranking logic (materializing an aggregate subquery before
  `OVER(...)` when combined with `group_by`, since most dialects reject a
  same-statement SELECT-list alias inside a window function), the alias
  resolution for group_by/order_by against select-list labels, and the
  join-graph compilation all ported verbatim.
- **The validator's structural checks** (table/column existence, join-graph
  connectivity, `having` requiring `group_by`/aggregates, top_n reference
  rules under aggregation) — ported into `validation/schema_validation.py`
  unchanged in logic.
- **Schema reflection + metadata caching** (`shared/schemas/__init__.py` →
  `schema/reflection.py`): the reflect-once-and-cache pattern with a
  double-checked lock, and the dual-schema-try fallback (`[None, "dbo"]`) for
  MSSQL's default schema, both carried over.
- **Concurrency guardrail** (`shared/utils/db.py`'s `CONCURRENCY_SEMAPHORES` →
  `execution/concurrency.py`): per-key semaphore with a bounded wait and a
  clear timeout error, unchanged in behavior, just re-keyed from a `DataBase`
  enum member to a connection-id string.
- **Session guardrails** (Postgres `SET LOCAL lock_timeout`/`statement_timeout`,
  MSSQL `SET LOCK_TIMEOUT`/`XACT_ABORT`/`DEADLOCK_PRIORITY`) — ported
  unchanged into `connections/dialects.py`.
- **The MCP auth pattern** (bearer-token API-key match via `hmac.compare_digest`,
  a context-var-scoped caller identity, an anonymous-dev-bypass outside
  production) — preserved, and generalized into `core/auth.py` so REST now
  shares the exact same mechanism (previously REST had no auth at all).
- **The test patterns**, not just the tests: mocking the validator/schema
  load at one seam instead of a real database, a real `asyncio.Semaphore` in
  the concurrency test instead of mocking asyncio itself, and the batch
  partial-failure test (one bad query doesn't drop the others) all carried
  over as the same *shape* of test against the new module paths.

## What was generalized

- **`DataBase`/`DataBaseName` enum → `ConnectionRegistry`**: the split
  between a credential-carrying internal type and a credential-free
  client-facing type was already the right idea — it just needed to become
  dynamic (file-loaded, `${VAR}`-interpolated) instead of four hardcoded enum
  members per customer database.
- **`INTERNAL_TIER`/`EXTERNAL_TIER` trust tiers → policy `join_group`**: the
  hardcoded "ShanyContent can never join Db/DbConst/InterOp" rule generalizes
  to "connections may only be joined cross-connection when they share a
  policy-declared `join_group`" — same mechanism, no hardcoded database
  names.
- **`AUTOLOAD_TABLES` hardcoded catalogs → `ConnectionProfile.known_tables`**:
  the four curated table lists (one per ShanyContent/Db/DbConst/InterOp) are
  gone; any connection can optionally declare its own seed list in the
  connections file, or fall back to a live `INFORMATION_SCHEMA` query exactly
  as before.
- **`entity_id` AST field + FK-guessing compiler logic → policy
  `mandatory_row_filters`**: the prototype's "scope every query to
  `Company.ID`, guessing the FK column by trying `companyid`/`company_id`/
  `entityid`" was a customer-specific multi-tenancy hack baked into the AST
  and the compiler. It's replaced with a policy-declared, explicit
  `{table, column, value}` filter — no guessing, no AST field, and it now
  applies uniformly to every connection that needs it rather than being
  wired to one hardcoded concept of "Company."
- **Global `conf.structured_query_*` settings → per-connection `Policy`**:
  every guardrail (max joins, max select, max where depth, row limits,
  timeout, concurrency) was one global setting shared by all four hardcoded
  databases. It's now a `Policy` object resolved per connection, with a
  `default:` section for anything unconfigured.
- **No column-level policy existed before** — table access was effectively
  "any table the DB user can see," with the customer/business-domain access
  control living implicitly in application code, not the query layer.
  QueryGate adds `allowed_columns`/`denied_columns`, checked against *every*
  column reference in a query (select, where, group_by, having, order_by,
  top_n) — not just what's selected — closing an inference side-channel that
  didn't need closing before because there was no column policy at all.
- **REST had no authentication.** The MCP surface was the only authenticated
  transport. `core/auth.py`'s shared `Authenticator` now protects both.
- **Date bucketing was MSSQL-only** (`DATEADD`/`DATEDIFF`, hardcoded — the
  prototype only ever ran against MSSQL for this path). Since QueryGate is
  Postgres-first, `compiler/sqlalchemy_compiler.py` now dispatches on dialect:
  Postgres uses native `date_trunc`, MSSQL keeps the DATEADD/DATEDIFF idiom,
  and a SQLite path was added for the example/test database.

## What was removed (not generalizable — customer business logic)

- `domains/companies/`, `domains/templates/` (+ `classifications/`),
  `domains/reports/`, `domains/views/`, `domains/shany_content/`,
  `domains/data_parts/` — the actual backoffice product surface (company
  lookups, the GrammarQL-powered report templating engine, D&B "data part"
  stored-procedure catalog). None of this is a database-gateway concern.
- `shared/grammar/{parser.py,query_builder.py,context.py,
  column_alias_mapper.py,reference_tables.py}` — the GrammarQL template
  placeholder language (`{select(...).from(...).join(...).where(...)}`
  syntax parsed out of report templates). This is a templating-engine
  feature, not a query-gateway feature, and doesn't generalize into
  QueryGate's mission.
- `shared/const/{area_code,country_code,currency,customer_number_type,
  dnb_customer,phone_type,turnover}.py` — Dun & Bradstreet-specific reference
  data (SIC codes, D&B customer number types, etc.).
- `shared/models/{phones.py,turnover.py,shany_content/}`,
  `shared/schemas/{data_parts.py,shany_content_entities.py,views/}` —
  business-domain models tied to the same removed domains.
- `clients/shany_rest_api.py`, `clients/http.py` — an HTTP client for a
  specific external company REST API.
- `alembic/` + `alembic.ini` + the `BackOffice` Postgres store — the
  prototype had its own transactional Postgres database for templates/reports
  metadata. QueryGate has no database of its own: connections and policy are
  file-configured, and audit is structured logging, so there's nothing to
  migrate. See the plan's "no app-owned database" decision.
- `mcp/tools/{companies.py,data_parts.py,templates.py}` — MCP tools over the
  removed domains. `mcp/tools/health.py` was rewritten generic (no external
  REST-API readiness check).
- The old `.env`, hardcoded per-customer database names in `mcp/instructions.py`
  (ShanyContent's `Turnovers`/`PickList` gotchas, `Db`/`DbConst`/`InterOp`
  table surveys) — replaced with generic, schema-agnostic operating
  guidelines.
- `memory/`, `agent_state/`, `journal/`, `workflows/`, `skills/`,
  `templates/*.md`, `AGENT.md`, `CONSTRAINTS.md` — the old project's own
  Claude-assisted development workflow scaffolding, unrelated to the product
  itself. **Archived, not deleted** — moved to
  `archive/legacy-project-docs/`.

## What still needs work before commercial release

- **MSSQL is implemented but not live-verified** — no MSSQL server was
  available to test against in this environment. The dialect-isolation code
  and unit tests (checking compiled SQL *text*, e.g. `DATEADD`/`DATEDIFF`
  appears correctly) give reasonable confidence, but a real integration
  pass against SQL Server is needed before calling MSSQL support production
  ready.
- **OAuth/JWT/RBAC.** Only API-key auth exists today. `core/auth.py`'s
  `Authenticator` protocol is designed so a second implementation can be
  added without touching REST/MCP transport code, but that implementation
  doesn't exist yet.
- **No persisted audit store or admin UI.** Audit records are JSON log
  lines; there's no database-backed audit trail, no policy-management UI,
  and no way to hot-reload `connections.yaml`/`policy.yaml` without a
  restart (both are loaded once, lazily, into a process-wide singleton).
- **Stored-procedure catalog** is explicitly out of scope for this version
  (see README limitations) — worth revisiting as a separately-designed,
  explicitly-cataloged, policy-gated feature, not a generic pass-through.
- **Multi-instance deployment**: the per-connection concurrency semaphore
  and metadata cache are in-process singletons — correct for a single
  instance, but multiple QueryGate instances behind a load balancer would
  each enforce their own concurrency cap independently (an *n*-instance
  deployment effectively multiplies the configured `max_concurrency` by
  *n*). Fine for v1; a shared (e.g. Redis-backed) semaphore would be needed
  for horizontally-scaled production deployments that need a hard global
  cap.
- **Column-level policy is case-sensitive** as written — doesn't normalize
  across what a given dialect reports for identifier casing.
