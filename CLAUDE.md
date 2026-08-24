# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository. These
instructions override default behavior — follow them exactly.

## Non-negotiables — read first, never violate without an explicit recorded decision

These are the load-bearing invariants. Each links to the section with the full
rationale; break one only via a written decision in `docs/PRODUCT_GUIDE.md`'s
Decision Log (or, for product identity, `docs/business/NORTH_STAR.md`).

1. **No caller-controlled raw SQL, ever.** The only thing a caller submits is a
   validated `StructuredQuery` / write AST. There is no `execute_sql` field,
   endpoint, or MCP tool, and never will be without a NORTH_STAR decision.
   → [The one request pipeline](#the-one-request-pipeline), [North Star](#north-star--the-product-definition-for-success-the-anchor)
2. **No credential on any returned model.** Only `PublicConnectionInfo` leaves
   REST/MCP; `ConnectionProfile` (which holds the connection string) never does.
   `test_credential_redaction.py` enforces this against the live schemas.
   → [Security invariant](#security-invariant)
3. **Redaction-safe audit only.** Persisted audit events never include SQL,
   predicate values, rows, exceptions, or credentials. → [pipeline step 6](#the-one-request-pipeline)
4. **One database path.** Every REST route and MCP tool is a thin wrapper over
   the single `StructuredQueryService` — no second path to a database exists.
   → [The one request pipeline](#the-one-request-pipeline)
5. **One catalog mutation path.** All catalog writes go through the
   `CatalogFileRepository` lock. No second catalog file/store/DB; drafts never
   index, merge, or publish without the 32B-1 review gate. → [Catalog](#catalog-descriptive-overlay-single-mutation-path)
6. **Vary behavior by registered interface, not scattered `if`s.** Dialect /
   backend / strategy differences go behind a Protocol + one class per variant +
   a registry — never inline `if X == ...` at call sites. → [Composable interfaces](#composable-single-purpose-interfaces)
7. **Expose primitives; don't spoon-feed the agent.** Translate an AST operation
   into each dialect's idiom, but never synthesize query structure the AST
   didn't ask for, and reject (don't emulate) a capability a dialect genuinely
   lacks. → [Engine philosophy](#engine-philosophy-expose-primitives-dont-spoon-feed-the-agent)
8. **The non-goals are product identity.** No raw-SQL mode, no execution of
   model-generated code, no stored-procedure path, no mandatory semantic
   modeling step, no warehouse of our own. → [North Star](#north-star--the-product-definition-for-success-the-anchor)

## What this is

QueryGate is an agent-safe database access gateway: it exposes Postgres/MSSQL
databases to AI agents over MCP and REST, but the only thing a caller can ever
submit is a validated `StructuredQuery` JSON AST (or a write AST) — there is no
raw-SQL field or endpoint anywhere in the codebase. See `README.md` for the
product pitch, `docs/RELEASING.md` for release gates, and `archive/extraction/`
only when historical extraction context is explicitly needed.

**Key docs and their authority:**

- **`docs/PRODUCT_GUIDE.md`** — plain-language explainer of architecture, terms,
  and decisions (also answers marketing/positioning questions). After any
  non-trivial task, check whether it added/changed architecture, a deliberate
  tradeoff, a new term/tool, or a customer-facing capability, and if so update
  the right section and its Decision Log. Skip for pure bug fixes, no-behavior
  refactors, and test-only changes. Its "Maintenance protocol" section has the
  exact rule; the `product-guide-sync` skill runs it.
- **`docs/business/NORTH_STAR.md`** — canonical definition of the product and how
  it wins. Every strategy artifact reconciles to it. → [North Star](#north-star--the-product-definition-for-success-the-anchor)
- **`TODO.md`** — the live worklist (authority for item *content* and `✅ DONE`
  status). `docs/TODO_ARCHIVE.md` holds full write-ups of fully-shipped items.
  Split to keep routine reads cheap.
- **`ROADMAP.md`** — the **execution order** over `TODO.md` and the transient
  active-claim signal used to keep concurrent agents off the same item
  (authority for *order and coordination only*; never restates a body or owns
  done-status). TODO.md leads, ROADMAP.md follows.
- **`docs/ENGINE_EXPRESSIVENESS_PLAN.md`** — flagship-pillar spec for taking the
  READ query engine to 10/10 expressiveness with no safety regression (Structural
  pillar; TODO.md items 99–106, sequenced in ROADMAP.md Phase 4). Authoritative
  for those items' AST design, per-primitive Definition of Done, and the canonical
  regression bar. Consult before starting any of items 99–106.

**Item + worklist rules** (the `ship-item` and `roadmap-next`/`next-item` skills
automate these):

- **Item numbers are permanent and file-global.** Never renumber or reuse one —
  the repo has ~176 internal "item N" cross-refs plus CLAUDE.md and test
  references that must keep resolving. A new item takes the next unused number
  (check the highest `### N` heading in TODO.md — do not trust a number written
  here, it goes stale; 98 was never allocated and is deliberately left unused,
  and 199–209 are reserved for the human-SSO/identity stream while items 210–219
  carry the SaaS subscription work).
- **When an item ships fully** (its `###` heading ends in exactly `✅ DONE`, no
  trailing qualifier): move its full body to `docs/TODO_ARCHIVE.md` in numeric
  order under a `### N.` heading, and leave a stub in `TODO.md` — same heading,
  one line of what shipped, then
  `**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item N).`
- **Partially-done items stay full inline in TODO.md.** Anything marked
  `✅ DONE (phase 1)` still carries open work — stub it only once every phase is
  complete.
- **Claim an item in `ROADMAP.md` before implementation.** Immediately below
  its checkbox line, add
  ``🚧 **CLAIMED** — owner: `<agent/session-or-task-id>`; started:
  `<YYYY-MM-DDTHH:MMZ>` ``. Re-read the item after writing the marker; if
  another claim is already present or appeared concurrently, stop and choose a
  different eligible item. A claim makes the item unavailable to every other
  agent, including when an override names it. Never infer that an old-looking
  claim is abandoned — only its owner or the maintainer may clear it. The owner
  removes the marker when the item ships or before handing it back uncompleted.
  This is transient coordination state, not completion status: never put it in
  `TODO.md`, never change `[ ]` to `[x]` because of it, and do not make a
  standalone claim-only commit.
- Keep ROADMAP.md's checkboxes reconciled to TODO.md. Re-sequencing ROADMAP.md
  is allowed but must be a deliberate, one-line-reasoned edit, not drift.
- The archive is reference-only: never put an open action item there, and don't
  add a second TODO file or a competing index.
- **The archival and done-status synchronization rules are machine-enforced.**
  `scripts/check_worklist.py` (run it with `make worklist-check`) verifies done
  items archived + stubbed + pointer present, Quick-scan `✅` column and ROADMAP
  checkboxes reconciled to the headings, archive in numeric order, no dangling
  pointer, and unique numbers. It runs in the unit suite
  (`test_worklist_consistency.py`) and blocks `git commit` via the project
  pre-commit hook. The claim protocol above is procedural. The two *derived*
  mirrors (the table `✅` column and the bare ROADMAP checkboxes) are regenerated
  from the headings with `make worklist-sync` — never hand-edit them.

## Repo map

Source is `src/querygate/`; tests are `tests/{unit,integration,security}/`.

```
src/querygate/
  api/            REST transport (routes.py, app.py) — thin wrappers over the service
  mcp/            MCP transport (tools/*.py) — thin wrappers over the service
  execution/      service.py = the single pipeline entry; concurrency, quota,
                  admission, cost_estimation, approval, write_execution/preview
  validation/     policy_validation.py then schema_validation.py (run in that order)
  compiler/       sqlalchemy_compiler.py + dialect_adapters.py (DialectAdapter per dialect)
  query_ast/ write_ast/   the read AST and write/DML AST models
  connections/    registry, engine, models.py (ConnectionProfile vs PublicConnectionInfo),
                  dialects.py (SessionDialectAdapter per dialect)
  policy/ catalog/ schema/   YAML-loaded policy, semantic catalog + governance, reflection
  core/           auth.py (Authenticator boundary), shared primitives
  audit/          logger.py + sinks.py (redaction-safe JSONL events)
  secrets/        SecretResolver per backend
  admin/ client/ health.py metrics.py   ops surface
```

Python **>=3.11,<4.0**, Poetry-managed. When a piece of behavior varies by
backend/dialect/strategy, the pattern is always Protocol + one class per variant
+ registry (see the precedents in [Composable interfaces](#composable-single-purpose-interfaces)).

## Which skill for which task

Prefer these skills over improvising the workflow; they encode the repo's rules.

| When you're… | Use skill |
| --- | --- |
| Continuing development in priority order | `roadmap-next` (or `next-item` for capacity-scoped selection) |
| Marking a fully-shipped item done + archiving it | `ship-item` |
| Touching the request pipeline, connections models, catalog governance, audit, or any REST/MCP surface | `security-invariant-check` (before commit) |
| Before declaring any top-level user task complete | `auditors` — mandatory final completion gate; run it even when every reviewer is N/A |
| Adding/extending per-dialect SQL rendering | `dialect-primitive` |
| About to commit a non-trivial change / prep a release | `release-gate` |
| After a feature/design change touched docs-worthy surface | `product-guide-sync` |
| Hardening the AST/policy boundary or "try to break it" | `adversarial-probe` |
| Improving coverage / finding untested code | `test-gap` |
| Restructuring code with behavior held constant (extract/rename/consolidate) | `refactor` |
| Diagnosing and fixing a bug down to its root cause | `debug` |
| Hands-on design/UX critique of the running admin_ui/access_ui | `ui-ux-critic` |
| Checking that doc/marketing claims are backed by code+test | `claim-verify` |
| Updating landing page / sales copy after a capability ships | `pitch-sync` |
| Periodic market check / competitor comparison | `competitive-scan` |
| Rating the product itself 1-10, standalone and vs. the competition | `product-scorecard` |
| Getting a bias-free outside opinion on market viability (no repo context given to the reviewer) | `fresh-eyes` |
| Supply-chain / CVE / lockfile audit | `dep-audit` |
| Whole-repo invariant-drift sweep | `repo-audit` |
| Assembling a security-review evidence packet | `trust-evidence` |

## Working agreement — definition of done + self-review

A task isn't done when it "works."

**Mandatory final completion gate — every top-level user task:**

- Run `auditors` after implementation, docs, and relevant checks, and before
  committing (when requested) or sending the final completion response. This is
  mandatory for trivial, formatting-only, docs-only, test-only, and read-only
  work too. If no reviewer applies, the skill must record each reviewer as N/A;
  do not silently skip it.
- Capture `git status --short` before editing so the audit can separate the
  task's delta from unrelated pre-existing work.
- Do not declare completion while a reviewer is pending, a finding is
  untriaged, an accepted fix is unverified, or an owner decision is outstanding.
  If the tree changes after the audit, rerun the affected review on the final
  tree.
- Reviewer subagents launched by `auditors` are internal stages of their parent
  audit, not top-level user tasks. They return their report to the parent and
  **must not invoke `auditors` recursively**.
- State the auditors report — target, the 1–10 rating, reviewers run/N/A,
  findings and verdicts, what was fixed — **in the final chat response
  itself**, not only as an internal step. Producing the report is not the same
  as surfacing it; a clean audit or a trivial-task N/A is not a substitute for
  stating the rating.

**Definition of done for non-trivial repository changes** (skip the following
change-specific checks only for trivial one-liners, pure formatting,
read-only/report-only tasks, or explicitly-throwaway work):

- `poetry run black src/ tests/` clean, and the relevant test tier green
  (`pytest -m unit` at minimum; add integration/security/real-db per the surface
  you touched).
- `docs/PRODUCT_GUIDE.md` checked (and updated if the change warrants it).
- Any relevant gate run — use `release-gate` for the ordering and when a smoke
  test is required.
- One clean, focused commit (branch first if on `main`); commit/push only when
  asked.

**Self-review, then act on the gap:**
1. **Re-read the actual diff** (not your memory of it) against the bar this repo
   sets — the [Non-negotiables](#non-negotiables--read-first-never-violate-without-an-explicit-recorded-decision),
   the primitives doctrine, the composable-interface rule, the testing gotchas.
2. **Mutation-verify every new enforcement point *before* reporting completion.**
   For each line the change adds that enforces something — a cap, a rejection, a
   ref walk, a conversion, a dialect guard — break it deliberately, run the suite,
   and confirm a test fails *for that reason*. Revert, then move on. This is not
   optional polish: on 2026-07-26 items 101 and 114 each *first passed self-review*
   with a green suite over an unguarded enforcement line (the item-100 caps silently
   not reaching a window argument; `to_read_where`'s boolean branches, where a
   swapped `and`/`or` would have made `DELETE … WHERE a OR b` delete on `a AND b`).
   Both were invisible to 1,800 passing tests and to a careful reading of the diff,
   both were caught by this technique before landing, and both took ten minutes to
   find. Bound the effort to each enforcement *rule*, not each line.
   **Measure the shape the product actually emits**, too: a guardrail rationale
   measured on raw un-LIMITed SQL cannot be quoted as if it described a compiled
   query, and a claimed cost ("~1M bind parameters") must be measured rather than
   reasoned — the driver's parameter limit made that one wrong.
3. **Rate it honestly, 1–10**, and name the one or two things holding it back. A
   passing suite is the floor, not a 10. Look specifically for: under-testing, a
   leaked assumption, a missed edge case, dead/duplicated code, inconsistency
   with an existing pattern, a doc that now drifts from behavior.
4. **Close the gap, don't just narrate it.** If the fix is small and safe (a
   rename, an added test, a tightened type, a doc reconciliation, deleting dead
   code), **do it now** and re-check — don't ask permission to reach the bar you
   should have hit. If it's large, risky, or a judgment call (a design change, a
   new abstraction, anything touching a non-negotiable or a non-goal, or real
   scope growth), **stop and surface it** with a concrete proposal and let the
   user decide.

**Be honest, always.** The review is worthless if the rating is inflated or the
weaknesses are softened. State what you actually found — flaws you introduced,
things you're unsure about, shortcuts you took, gates you did *not* run — even
when it looks bad. Never round a 6 up to a 9, and never claim something was
verified when it wasn't. An accurate low rating with a clear reason is a good
outcome; a flattering false one is a failed task. If the work genuinely is a
9–10, say so plainly and why — honesty runs both directions and doesn't mean
manufacturing criticism.

## North Star — the product definition for success (the anchor)

`docs/business/NORTH_STAR.md` is the canonical definition of what QueryGate is
and how it wins; every strategy artifact (ROADMAP.md, the competitor briefs,
MARKET_DOMINATION_ANALYSIS.md, marketing) reconciles to it. In one line:
**QueryGate is the enforcement point that governs *what an agent's query or
write is allowed to be* — by construction, never by inspecting a string —
against your existing operational databases, self-hosted, with per-human
proof.** It wins by owning all **three pillars** no competitor combines:
**Structural** (no raw SQL/DML string + query-shape policy), **Reach** (governs
your live operational DB, self-hosted, data never leaves), **Proof**
(per-human attribution + tamper-evident audit).

The **non-goals are product identity, not gaps** — they are why followers can't
copy us; adding any needs an explicit recorded decision: no `execute_sql`/raw-SQL
mode, no execution of model-generated code, no stored-procedure path, no
mandatory semantic/entity-modeling step, no warehouse of our own. When a
feature, roadmap re-order, or pitch is unclear, check it against NORTH_STAR.md
(definition → pillars → non-goals → success metric). Success is measured by one
thing: a paid design partner passing a security review no competitor's passes at
the operational-query layer.

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
poetry run black --check src/ tests/ examples/ scripts/   # or: make format-check
poetry run black src/ tests/ examples/ scripts/           # or: make format

# Demo database (for manual/local verification against a real Postgres)
docker compose up -d                    # starts querygate-demo-db on localhost:5433, auto-seeded
make release-check                      # source, package, formatting, tests, config
make release-smoke                      # image + real structured Postgres query
poetry run querygate-semantic-memory evaluate  # fixed offline 32A benchmark

# TypeScript client builder (item 51 phase 2, in clients/typescript/ — not a published package)
cd clients/typescript && npm install     # once
make test-ts-client                      # or: cd clients/typescript && npm test
```

The default suite excludes tests marked `real_db`; CI also runs dedicated
Postgres, MSSQL, and MySQL jobs (`postgres-live` / `mssql-live` / `mysql-live`),
so all three *supported* registry dialects are live-verified against a real engine. `test_sqlite_end_to_end.py` uses SQLite internally
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

Writes/DML follow the same shape through the write AST (`write_ast/`) and
`execution/write_execution.py`/`write_preview.py`, gated by approval
(`execution/approval.py`) — still no raw-SQL string anywhere.

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
  agent's composition problem instead of exposing a primitive. Settled
  precedent (TODO.md item 74, Decision Log): `MSSQLDialectAdapter.order_by_terms`
  used to inject an *extra CASE-based sort column* when `OrderBySpec.nulls`
  was set, since T-SQL has no `NULLS FIRST/LAST` syntax — structure the
  caller never expressed in the AST. It now **rejects** `nulls` on MSSQL
  with `QueryValidationError`, the same posture as `array_agg`, because an
  agent can build that exact CASE-bucket itself with primitives QueryGate
  already exposes (`CaseSelectItem` for the bucket, multiple `OrderBySpec`
  entries for the tie-break). Use this as the reference for how a
  missing-keyword gap is resolved — reject and point at the primitives, do
  not emulate.

**Exceptions are possible but must be deliberate, not assumed.** If a
specific case seems to genuinely warrant the engine doing more than
mechanical translation — synthesizing structure on the agent's behalf for
a good, concrete reason — that is a design discussion to have explicitly
(with the user, or recorded as a reasoned Decision Log entry in
`docs/PRODUCT_GUIDE.md`) before implementing it, not a default anyone
should reach for under time pressure or convenience. Treat this section's
rule as the default for all new work; an exception must be justified on
its own terms, in the open, and recorded — the same way item 74's MSSQL
nulls handling was decided in the open (reject, not emulate) rather than
silently kept or silently reverted.

### Connections and policy are file-configured, not code-configured

`connections/registry.py`, `policy/loader.py`, and `catalog/loader.py` load YAML
into process-wide stores and support an authorized, atomic hot reload. There is
no app-owned configuration database yet. See `examples/connections.example.yaml`
and `examples/policy.example.yaml` for the enforcement shape and
`examples/catalog.example.yaml` for the optional versioned semantic overlay.

#### Security invariant

`connections/models.py` splits `ConnectionProfile` (carries the real connection
string, resolved from `${ENV_VAR}` in the YAML file) from `PublicConnectionInfo`
(id/dialect/enabled/description only — no credential field exists on it at all).
Only `PublicConnectionInfo` is ever returned from REST/MCP.
`tests/unit/test_credential_redaction.py` asserts this against the live OpenAPI
schema and MCP tool schemas, not just by convention — keep that test meaningful
if you touch either model.

### Catalog: descriptive overlay, single mutation path

The catalog is a descriptive semantic overlay — it **must never** become a query
execution or row-value search path. Full scope is TODO.md item 32 and
`catalog/governance.py`'s module docstring; the rules that must hold:

- **Single mutation path.** All catalog writes (refresh, generate, governance
  edits) go through the same `CatalogFileRepository` lock. Do not add a second
  catalog file, database, or mutation path. In particular, do **not** route
  catalog content through `admin/store.ConfigVersionStore` — it snapshots its
  own copy of catalog.yaml in `var/config_versions/`, which would silently
  diverge from the live `CATALOG_FILE`.
- **Drafts are quarantined.** Manual draft proposals stay separate from
  published entries and must never be indexed or merged without the 32B-1
  review gate. Search must filter tables, columns, and *both* relationship
  endpoints before tokenization/ranking/counting.
- **No network in 32A.** Only `disabled` and offline `manual` provider modes
  exist. Do not add network/provider execution, a live LLM call, an embedding
  index, or an autonomous policy edit.
- **Refresh is atomic and independent** of query execution; it stales only
  affected entries.
- **`version_id`/`generation_id` are file-global, not per-connection.**
  `import_connection` must keep remapping them against the target catalog's
  current content — do not "simplify" that away; it exists specifically to stop
  one connection's import from corrupting another's history.
- **32C (adaptive usage learning) has shipped** (`catalog/usage.py`,
  `catalog/learning.py`, `catalog/adaptive_learning_benchmark.py`). Stay inside
  its acceptance criteria: typed redaction-safe signals only,
  per-customer/connection partitioning, no feedback loops, and learned content
  must go through the existing 32B review path — it can never publish itself.

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

`connections/dialects.py` was formerly a known exception (inline dialect
branching centralized in one module); as of TODO.md item 57 it is now a
`SessionDialectAdapter` — one concrete class per dialect dispatched through the
`_SESSION_ADAPTERS` registry, the async engine/session sibling of the sync
compiler `DialectAdapter`. So the composable-interface doctrine now holds there
too; follow it (implement + register an adapter, no inline branch).

The former Postgres-only cost-estimation exception is resolved (TODO.md item 26
phase 2): `execution/cost_estimation.py` now has both `estimate_postgres_query_cost`
(inline `EXPLAIN` in-session) and `estimate_mssql_query_cost` (dedicated
`SET SHOWPLAN_XML ON` connection), dispatched by `StructuredQueryService._estimate_cost`
— a per-dialect method, not scattered `if dialect ==` at call sites. A dialect
without an estimator returns None (proceeds under the reactive guardrails).

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
- `mcp/tools/{connections,schema,query,write,help,templates}.py` deliberately
  do **not** use `from __future__ import annotations`. `MCPServer` (`mcp`
  SDK v2, formerly `FastMCP`) resolves each tool's forward references
  against the *wrapping* function's `__globals__` (the `safe_mcp_tool`
  decorator lives in `mcp/exceptions.py`), not the tool module's —
  stringified annotations there fail to resolve at registration time. Keep
  annotations as real objects in all six files.
- `ConnectionProfile.dialect` accepts `"postgresql"`, `"mssql"`, `"mysql"`
  (item 19 phase 1 — **live-verified for reads, dialect idioms, and session
  guardrails**: `tests/integration/test_mysql_live.py` runs against a real MySQL
  8.4 in the dedicated `mysql-live` CI job, marker `mysql_live`. Governed-write
  *execution* is live-proven on Postgres/MSSQL only — MySQL has no
  insert/update/delete round-trip test), or `"snowflake"`/`"bigquery"` (item 19 phases 2/3 —
  both accepted, but `connections/engine.py`'s `init_engine` refuses to
  actually connect for either; rendering-level only, not live-verified) —
  SQLite is used internally for tests/examples by monkeypatching
  `connections.engine.get_engine`/`session_scope` directly (see
  `test_sqlite_end_to_end.py`), never through the registry.
- **Converting an in-process store's interface to `async def` (to prep for a
  future Redis-backed variant, the pattern already used by
  `execution/concurrency.py`/`redis_concurrency.py` and
  `execution/quota.py`/`redis_quota.py`) is a two-part change, not one.** A
  2026-07-23 review caught a now-removed `CompensationStore` (the compensation
  store was later deleted along with the write-undo feature) converted to
  `async def` while its call sites in `execution/write_execution.py` still called
  it synchronously — every call silently returned an unawaited coroutine instead
  of raising, so it passed a casual read and only broke a targeted unit test.
  When you make a store's Protocol methods `async`, grep every call site in
  the same commit, run `pytest -m unit` (a `RuntimeWarning: coroutine ... was
  never awaited` means you missed one), and extend the `tests/conftest.py`
  reset fixture to clear the new state — the same discipline the
  `in_process_limiter()` gotcha above documents for concurrency.
  **Better still: define a new store's Protocol `async` from the start**, even
  when the in-process implementation needs no `await` — then there is no later
  conversion to get wrong. `admin/observed_shapes.py` (item 195) does this
  deliberately and says so in its docstring.
- **`ZPOPMIN`'s Lua reply shape is not portable — do not use it in a Redis
  script.** Real Redis returns a flat `{member, score}` (so `reply[1]` is the
  member); fakeredis returns a *nested* table, so `reply[1]` is a table and any
  `HDEL`/`ZREM` keyed off it raises `Lua redis lib command arguments must be
  strings or integers` mid-script — which a **fail-open** wrapper swallows, so it
  presents as "the bound is cosmetic and every eviction is lost" rather than as an
  error. (In a fail-closed script, e.g. the disclosure budget's, the same bug
  would surface as a hard error instead — do not go looking for a silent
  failure.) Item 195 phase 2 hit this building the observed-shape store's
  eviction loop: measured 10 entries retained against a bound of 3, zero
  evictions counted.
  Use `ZRANGE key 0 0` followed by `ZREM` — both have stable flat replies on
  every implementation. `lupa` must be installed for fakeredis to execute Lua at
  all (it is a dev dependency); without it every scripted test fails with
  `unknown command 'evalsha'`, which reads like a store bug and is not one.
- **A Redis Lua script must declare every key it touches in `KEYS`, and those
  keys must share a hash slot.** Reaching a key built inside the script via
  `redis.call` is what Redis Cluster forbids — it is not a way to avoid
  `CROSSSLOT`, it is a worse version of the same bug. **Neither fakeredis nor a
  single-node Redis can detect it** — fakeredis executes undeclared-key access
  happily and models no slots at all (which is what item 192's own write-up
  says), so no behavioural test will catch this class. That is precisely why the
  guard is a *source-level* assertion:
  `test_every_key_the_script_touches_shares_one_hash_slot`. Give every key one
  shared hash tag (`admin/redis_observed_shapes.py` uses `{qgshapes}`) and
  declare them all. `TODO.md` item 192 is this defect still open in the
  disclosure budget's script; item 195 phase 2 nearly repeated it, and was caught
  by review, not by a test.
- **Never round-trip a JSON document through Lua `cjson` in a Redis script.**
  Lua has one table type, so `cjson.decode` + `cjson.encode` turns every empty
  JSON **array** into an empty **object** — on real Redis but *not* under
  fakeredis, whose Lua bridge preserves the distinction. Item 195 phase 2 shipped
  this into review: every `joins: []`/`group_by: []`/`ctes: []` in a stored query
  skeleton came back as `{}`, so every draft built from the durable backend failed
  validation with five `Input should be a valid list` errors, with a fully green
  suite. It is the mirror image of the `ZPOPMIN` entry above — same
  fakeredis/real-Redis divergence class, opposite direction. Structure around it
  rather than encoding carefully: keep the document **opaque** (write once with
  `HSETNX`, never decode) and hold every mutable field in its own Redis structure
  the script can update with an integer/string primitive (`HINCRBY`, `HSETNX`, a
  ZSET score).

### What's archived, not active

`archive/legacy-project-docs/` holds the previous project's own
Claude-assisted development workflow scaffolding (`memory/`, `agent_state/`,
`journal/`, `workflows/`, `skills/`, `AGENT.md`, `CONSTRAINTS.md`). It's not
imported by any code and not part of the product — ignore it unless
specifically asked to consult old project history.

`archive/extraction/` holds historical migration/cleanup reports. Neither
archive directory is included in package or container release artifacts.
