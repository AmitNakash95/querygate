# TODO: QueryGate → production-grade

Action items to take QueryGate from "working, tested prototype" to a
ready-to-ship commercial product. Grouped by priority. Each item explains
*why* it matters, not just what to build — treat the "why" as the acceptance
criteria. Items marked with a file path point at where the current
(incomplete) implementation lives.

Cross-reference: `README.md` → "Current limitations" covers the active gaps
more briefly; historical extraction reports live under `archive/extraction/`.
This file is the actionable breakdown.

## Effort scale

Rough solo-engineer time, assuming familiarity with this codebase already.
Design + implementation + tests, not just a first draft. Real infra
(databases, cloud accounts) and unfamiliar external systems (JWKS, Vault)
push estimates toward the high end of their range — treat these as
order-of-magnitude, not commitments.

| Size | Time |
|---|---|
| XS | a few hours |
| S | 0.5–1 day |
| M | 2–3 days |
| L | 3–5 days |
| XL | 1–3+ weeks |

## Quick-scan summary

| # | Item | Effort | Depends on |
|---|---|---|---|
| 1 | ✅ Audit logs leak filter literals | S | — |
| 2 | ✅ MSSQL untested against a real server | L | — |
| 3 | ✅ Query timeout not proven to cancel | M | — |
| 4 | ✅ No CI pipeline | S | — |
| 5 | ✅ Config can't reload without restart | M | — |
| 6 | ✅ No per-principal policy | L | 8 |
| 7 | ✅ Health checks are fake | S | — |
| 8 | ✅ `Principal` has no scopes/claims | S | — |
| 9 | ✅ Concurrency limiter is single-instance only | L | — |
| 10 | ✅ No OAuth/JWT | M | 8 (helps) |
| 11 | ✅ No response byte-size cap | S | — |
| 12 | ✅ No metrics/observability | M | — |
| 13 | ✅ No secrets-manager integration | M–L | 5 (pairs well) |
| 14 | ✅ Docker image never built/run (found + fixed as a side effect of item 4) | S* | — |
| 15 | ✅ No load/soak testing | M | — |
| 16 | ✅ Column policy case-sensitivity gap | XS | — |
| 17 | ✅ No config validation CLI | S | — |
| 18 | Stored-procedure catalog | XL | — |
| 19 | Additional dialects | M–XL (per dialect) | 2 (do MSSQL first) |
| 20 | ✅ Client SDK / integration examples | S | — |
| 21 | ✅ Principal policy must apply to every MCP/config surface | S | 6, 8, 10 |
| 22 | ✅ Principal-aware connection/tool visibility | S–M | 6, 8 |
| 23 | ✅ Persisted audit/event sink | M | 1, 12 |
| 24 | ✅ Release hygiene and reproducible v0.1.0 cut | S–M | 4, 14, 17 |
| 25 | ✅ Admin/config governance plane | L | 5, 6, 10, 13 |
| 26 | ✅ Query-cost estimation before execution (phase 1: Postgres EXPLAIN; phase 2: MSSQL not started) | L | 2, 3, 15 |
| 27 | ✅ Semantic schema catalog and sensitivity metadata | L | 6, 16 |
| 28 | ✅ Threat model + adversarial security test suite | M | 1, 6, 8, 10, 11 |
| 29 | ✅ Production deployment reference stack | M | 4, 9, 12, 13, 14 |
| 30 | ✅ Distribution, SBOM, and signed release artifacts (phase 1: SBOM + audit; phase 2: publishing + signing not started) | M | 4, 14 |
| 31 | ✅ Admin UI / policy designer | XL | 25 |
| 32 | ✅ Governed adaptive semantic memory for agents (32A ✅; 32B ✅; 32C ✅) | XL | 23, 25, 27, 28 |
| 33 | ✅ Permission-aware QueryGate product guide and configuration assistant | M–L | 8, 10, 21, 22, 25 |
| 34 | ✅ Interactive mocked HTML product sandbox | M | — |
| 35 | ✅ Agent-visible capacity waiting, progress, and cancellation (phase 1: caller-tunable queue_mode/wait_timeout_seconds, admission id, metrics/audit; phase 2: queue-depth caps + Redis-backed cross-replica admission state; phase 3: progress notifications, REST 202+cancel, mid-queue cancellation, 429 evaluation not started) | L | 9, 12, 15, 20 |
| 36 | ✅ Extensive production-grade QA project / edge-case test suite (phase 1: policy-cap boundary tests + Hypothesis property-based compiler fuzzing; phase 2: cross-dialect differential tests + REST/MCP malformed-input fuzzing not started) | L | 15, 28 |
| 37 | ✅ Automated end-to-end proof of adaptive semantic learning | M–L | 23, 25, 27, 28, 32B, 32C |
| 38 | ✅ Admin UI catalog-governance workspace (phase 1: core review/approve/reject/publish/rollback loop; phase 2: bulk ops, export/import UI, generation triggers not started) | L | 27, 31, 32B |
| 39 | ✅ Draft-aware policy simulation before staging | M–L | 6, 17, 25, 31 |
| 40 | ✅ Semantic access diff for config changes (phase 1: connection-baseline diff + REST; phase 2: per-principal resolution not started) | L | 6, 25, 31, 39 |
| 41 | ✅ Policy-change blast-radius analysis (phase 1: bounded synchronous aggregation + ranking; phase 2: async/paginated evaluation for very large principal counts not started) | M–L | 22, 25, 31, 40 |
| 42 | Four-eyes config approval and separation of duties | XL | 10, 23, 25, 31 |
| 43 | ✅ Admin connection-operations and health workspace (phase 1: admin connection-status API; phase 2a: "test now" probe; phase 2b: browser workspace) | L | 7, 12, 31 |
| 44 | Admin observability and rejection-trend dashboard | L | 12, 23, 31, 35 |
| 45 | Dedicated non-admin “My access” portal | M | 22, 31, 33 |
| 46 | Validated policy templates and safe-start presets | M | 17, 25, 31, 39 |
| 47 | Safe draft recovery plus config export/import UX | M | 13, 25, 31 |
| 48 | Pre-defined, admin-approved query templates ("Toolbox"-style curated tools) | L | 6, 22, 25, 32B |
| 49 | Column-value masking/tokenization (not just allow/deny) | L | 6, 27 |
| 50 | Per-principal rate limits / query quotas over time | M | 9, 25 |
| 51 | Typed client-side query-builder SDK (Python + TypeScript) | M (per language) | 20 |
| 52 | Multi-framework agent integration examples (LangChain, LlamaIndex, OpenAI) | S (per framework) | 20 |
| 53 | Independent third-party security audit + published report | S* | 28 |
| 54 | Compliance control mapping (SOC 2 / ISO 27001 readiness) | L | 23, 25, 28 |
| 55 | Inference/transitive-exposure adversarial test suite | M | 28 |
| 56 | HA / multi-region reference deployment + DR runbook | L | 29 |
| 57 | Pluggable dialect-adapter architecture | L | 2, 19 |
| 58 | Published adversarial benchmark vs. raw-SQL agent and Google Toolbox | M | 28, 36 |
| 59 | Read-only behavioral anomaly surfacing on the audit stream | M | 32C, 44 |
| 60 | Bug bounty / responsible disclosure program | S | 53 |
| 61 | ✅ Deduplicate the StructuredQuery JSON Schema across execute/explain/batch tools | S–M | — |
| 62 | ✅ Consolidate redundant instructional prose into one source of truth | M | 61 (pairs well) |
| 63 | ✅ Scope-gate admin-only tool schemas out of non-admin sessions | M | 8, 22 |
| 64 | ✅ Make full catalog provenance opt-in on describe_table/search_catalog | S–M | 27 |
| 65 | ✅ Add a response-size cap to get_querygate_guide_topic | XS–S | — |
| 66 | ✅ CI/test guardrail on total MCP schema+instructions size | S | 61, 62, 63, 64, 65 |

✅ = done (see item body below for exactly what shipped and what, if
anything, was intentionally left out of scope).

Items 21–47 are the next quality tranche from the current repo scan: mostly
security consistency, enterprise operability, and product polish — the
areas that move QueryGate from "strong engineering prototype" toward a
credible 10/10 commercial infrastructure product.

Items 49–60 are proposed, not yet triaged into a priority tranche — added
from an explicit competitive-gap analysis against Google's Gen AI Toolbox,
Hasura, and Immuta/Privacera-class governance products. They live in their
own "P4" section below until reviewed and pulled forward into P2/P3.

Items 61–67 are all shipped — added from an explicit MCP token/context-
efficiency audit against DBHub (bytebase/dbhub) and Google's MCP Toolbox
for Databases, both much leaner MCP surfaces, and implemented immediately
after triage rather than sitting in a holding tranche. Item 61
(cross-tool JSON Schema duplication) was investigated first and initially
deferred — the MCP protocol has no mechanism for schema-sharing across
separate tools, so the only fix was a breaking tool-surface change
(merging three tools into one) — then implemented once that tradeoff was
explicitly accepted; see its own entry. Combined effect measured in
`tests/unit/test_mcp_token_budget.py`: 53,841 chars (~13,460 tokens) per
MCP session, down from the original pre-tranche 67,338. They live in their
own "P5" section below.

\*\* item 53's effort is engineering coordination and remediation only; the
audit itself is an external vendor engagement and calendar-time cost, not
solo-engineer effort — same caveat as item 14's footnote below, for a
different reason.

\* item 14's estimate assumes the Dockerfile mostly works; the MSSQL ODBC
apt-install step is the likeliest place for it to balloon past the estimate
if Microsoft's repo/package layout has drifted.

---

## P0 — blockers before any production traffic

### 1. Audit logs currently leak filter literals ✅ DONE

**Shipped:** SQL is now always compiled with bind placeholders for audit
logging and `explain_structured_query`; parameter values are redacted
(`<redacted>`) by default. Added `Policy.log_query_literals` (default
`False`) as the per-connection opt-in for deployments that want full
literal SQL for debugging — option (c) from the choices below, combined
with (a). See `execution/service.py`'s `_compile_to_text` and
`tests/unit/test_service.py`.

**Effort: S (0.5–1 day).** Contained to `_compile_to_text`/`audit_query` and
the `explain` response — no new subsystem, just a behavior change plus a
config knob and a test proving values no longer appear in logs.

**Why it matters:** `execution/service.py`'s `_compile_to_text()` tries
`stmt.compile(compile_kwargs={"literal_binds": True})` first, which succeeds
for most queries — meaning the SQL text written to the audit log (and to
`explain_structured_query`'s response) has WHERE-clause *values* baked in as
literals, not `?`/`$1` placeholders. If an agent filters on
`Customer.Email == 'jane@example.com'` or `SSN == '123-45-6789'`, that value
is now sitting in a log line. This directly contradicts the README's "audit
records... never row payloads" claim for anything expressible in a `where`.

**What to do:** Decide the intended behavior — options are (a) always log
parameterized SQL + a separately-redactable params blob, (b) hash/truncate
literal values above some length, or (c) make it configurable per-policy
(some deployments may *want* full literals for debugging). Whatever is
chosen, update the `explain_structured_query` response too, since it has the
same property and is returned directly to the caller.

### 2. MSSQL support is untested against a real server ✅ DONE

**Shipped:** `tests/integration/test_mssql_live.py` (12 tests) + its setup
companion `tests/integration/setup_mssql_test_db.py`, run against a real
MSSQL 2022 container. Verified live — no monkeypatched `get_engine`/
`session_scope`, unlike `test_sqlite_end_to_end.py`, so this exercises
`connections/dialects.py`'s real engine URL building, connect args, and
session guardrails end to end: basic select/where/order_by, joins,
`DATEADD`/`DATEDIFF` date-bucketing, `OVER`/`PARTITION BY` top_n,
policy/schema rejection, limit clamping, `list_tables`/`describe_table`,
and — the two things that specifically needed a live server, not a unit
test —**cross-database joins via `join_group`** (two real databases on one
server, three-part naming, both the working case and correctly-rejected
case where `join_group`s don't match) and **`SET LOCK_TIMEOUT` actually
taking effect** (a session holding a real row lock causes a second
session's write to fail at the configured timeout, not hang).

This surfaced and fixed three real, live-server-only-detectable bugs:
1. **A genuine safety-critical bug, not just cosmetic**: MSSQL's query
   *execution* timeout didn't actually work at all. `build_connect_args`
   passed `{"timeout": timeout_seconds}` to `pyodbc.connect()` believing it
   set `SQL_ATTR_QUERY_TIMEOUT` (per its own — wrong — comment); it
   actually sets `SQL_ATTR_LOGIN_TIMEOUT` (pyodbc's documented behavior — a
   connect-time-only setting). A `WAITFOR DELAY '00:00:10'` under a
   configured 2s timeout ran to completion, uncancelled, before the fix.
   The real query timeout has to be set as a post-connect attribute on the
   raw pyodbc connection, which — a second wrinkle — is nested inside
   SQLAlchemy's async aioodbc adapter (`dbapi_connection._connection._conn`,
   confirmed by reading the adapter's own source), not the object handed
   directly to a connect-pool event. Fixed with a new
   `register_query_timeout()` in `connections/dialects.py`, hooked into
   `connections/engine.py`'s `init_engine`. This closes item 3's MSSQL half
   too — see its own entry above the docstring notes it depended on this
   item's infra. Covered by both the live suite
   (`test_query_execution_timeout_actually_cancels_a_slow_query`) and a
   mocked unit test of the unwrap logic (`tests/unit/test_dialects.py`) —
   the latter's first version tried to prove the fix via a real `connect()`
   call and discovered its own bug: a listener that raises mid-connect can
   send SQLAlchemy's pool into a silent connect-retry loop that hangs
   indefinitely rather than propagating, so the test now asserts via
   listener-count introspection instead of actually connecting.
2. **The `geography`/`geometry` ODBC limitation the item itself
   predicted** — confirmed live: those columns reflect to SQLAlchemy's
   `NullType`, and `describe_table` was reporting that as the literal
   string `"NULL"` (not a crash, but actively misleading — indistinguishable
   from an error to an agent reading the schema). Fixed with
   `_render_column_type` in `execution/service.py`, now reporting
   `"unsupported (driver could not determine this column's type — often a
   spatial/CLR type like geography or geometry)"` instead. Regression
   covered in both the live suite and a mocked unit test
   (`tests/unit/test_service.py`), so it's caught without a real server too.
3. Real `aioodbc` engines created by the live suite were leaking
   (`reset_engines()` drops references without awaiting `.dispose()` —
   harmless for the rest of the suite, which never creates real engines,
   but a genuine resource-lifecycle gap once one does). Fixed by disposing
   explicitly in the test fixture's teardown, scoped to that file rather
   than changing the shared `reset_engines()` used everywhere else.

**Known limitation, not fixed here:** pyodbc's query timeout is bound at
connect time (`register_query_timeout`'s pool `connect` event), so an
MSSQL connection pool won't pick up a changed `Policy.timeout_seconds` from
a hot reload (item 5) until the engine is disposed and a fresh physical
connection opens — unlike Postgres, where `SET LOCAL statement_timeout` is
re-applied every session regardless of pooling. Config reload only
disposes an MSSQL connection's engine when `connection_string`/`dialect`
change, not on a pure policy change — worth a follow-up if per-connection
timeout hot-reloading for MSSQL specifically ever matters in practice.

No ODBC driver was installed on this machine, so verification ran inside
Docker end to end (an MSSQL server container + a throwaway test-runner
image with `unixodbc`+`msodbcsql18`, reusing the exact driver-install steps
already fixed for item 14) rather than touching system-level packages.
Wired into CI as a dedicated `mssql-live` job (MSSQL service container +
driver install directly on the runner, since GitHub-hosted runners are
disposable) and `make test-mssql-live` locally. New `mssql_live`/
`postgres_live` markers (both also tagged `real_db`) let
`pytest -m mssql_live` / `-m postgres_live` / `-m real_db` select exactly
what's needed; `poetry run pytest`'s default run still needs no real
database at all.

**Effort: L (3–5 days).** The unknowns are what make this large, not the
code volume: real MSSQL/ODBC driver behavior in an async pool, cross-database
schema qualification, and container/CI setup are all things that only reveal
their problems once you're actually running against a live server. Budget
extra time if this surfaces real bugs in `connections/dialects.py` or
`schema/reflection.py`, which is likely.

**Why it matters:** Goal was "Postgres and MSSQL" as the two supported
dialects, but this environment had no MSSQL server. `connections/dialects.py`
(engine URL building, connect args, session guardrails) and the compiler's
`DATEADD`/`DATEDIFF` date-bucket path are covered by unit tests that check
*generated SQL text*, not by anything that ever executed against SQL Server.
Shipping "MSSQL supported" without ever having run a query against MSSQL is
a real risk (ODBC driver quirks, `pyodbc`/`aioodbc` connection pooling
behavior, and cross-database schema-qualification for `join_group` are all
unverified).

**What to do:** Stand up an MSSQL container (or Azure SQL) in CI, add an
integration test suite mirroring `tests/integration/test_sqlite_end_to_end.py`
but against real MSSQL, and specifically verify: session guardrails
(`SET LOCK_TIMEOUT`, `XACT_ABORT`) actually take effect, cross-database joins
via schema qualification (`connections/engine.py`'s `physical_db_name` +
`schema/reflection.py`'s cross-schema reflection) work end-to-end, and the
`geography`/`geometry`-typed-column ODBC limitation noted in the old
prototype's instructions doesn't silently break `describe_table`.

### 3. Query timeout is a connect-time setting, not a guaranteed cancellation ✅ DONE

**Shipped (Postgres):** `tests/integration/test_postgres_timeout.py`, run
against a real Postgres (`make compose-up` + `make test-postgres-live`, or
`pytest -m postgres_live`; wired into CI as a dedicated job with a Postgres
service container). Proves the actual claim, not a proxy for it: a
`pg_sleep(10)` under a 2s `Policy.timeout_seconds` is cancelled at ~2s (not
left running for the full 10s, and not merely rejected before starting), a
fast query still succeeds under the same short timeout, and the
cancellation point tracks the configured value (1s timeout kills a 3s
sleep; 6s timeout lets the same 3s sleep succeed) rather than some
hardcoded threshold.

**Shipped (MSSQL), as part of item 2's live-server work below:**
`test_query_execution_timeout_actually_cancels_a_slow_query` in
`tests/integration/test_mssql_live.py`. This is where "fixing it could push
this toward the top of the M range" actually happened — the guardrail
*did not work at all* before item 2's fix: `pyodbc`'s `timeout=` connect
kwarg sets login timeout, not query timeout, so a `WAITFOR DELAY` ran to
completion uncancelled. See item 2's write-up for the full bug and fix
(`connections/dialects.py`'s new `register_query_timeout`).

New `real_db`/`postgres_live`/`mssql_live` pytest markers, excluded from
the default run via `addopts` so `poetry run pytest` still needs no real
database.

**Effort: M (1–2 days).** Mostly test-writing against a real Postgres (easy
to provision via `docker compose`); MSSQL half of this rides on item 2's
infra. If the test reveals the guardrail doesn't actually cancel, fixing it
could push this toward the top of the M range.

**Why it matters:** `connections/dialects.py`'s `build_connect_args` sets
`{"timeout": timeout_seconds}` for MSSQL (pyodbc's `SQL_ATTR_QUERY_TIMEOUT`)
but Postgres gets `SET LOCAL statement_timeout` via a session guardrail —
these are two different mechanisms with different failure modes, and neither
has been verified to actually abort a genuinely long-running query mid-flight
under load (vs. just failing to *start* one). A "server-side execution
timeout" is a core safety claim in the README; it needs a test that proves a
slow query gets killed, not just a config value that's plausibly correct.

**What to do:** Add a test that runs a deliberately slow query (e.g.
`pg_sleep()` on Postgres) against a real database and asserts it's aborted
within roughly `timeout_seconds`, not left running.

### 4. No CI pipeline ✅ DONE

**Shipped:** `.github/workflows/ci.yml` — a `test` job (`poetry install`,
`black --check`, full `pytest` suite, `querygate-validate-config` against
the example configs) and a `docker` job (build + run the image, smoke-test
`/health` and `/api/v1/connections`). All steps verified locally (including
a real `docker build`/`docker run`) before being committed to the workflow;
whether the workflow itself goes green on GitHub can only be confirmed by a
push, which wasn't done as part of this session. The MSSQL integration job
called for in the original plan is deliberately not included — there's no
such suite yet (item 2).

**Side effect:** the local `docker build` failed on the first attempt —
`python:3.11-slim` had floated from Debian 12 (bookworm) to 13 (trixie),
and Microsoft's `packages.microsoft.com/debian/13` apt repo is signed with
a key their own `microsoft.asc` doesn't contain (a confirmed open upstream
issue: microsoft/linux-package-repositories#305, #253). Fixed by pinning
both Dockerfile stages to `python:3.11-slim-bookworm` and dearmoring the
Microsoft key properly instead of piping it straight into
`trusted.gpg.d/*.asc`. This is exactly the risk item 14 flagged, so item 14
is marked done too as a side effect — see its own entry below.

**Effort: S (0.5–1 day).** Mostly YAML + iterating until the workflow is
green; the steps themselves (`poetry install`, `black --check`, `pytest`)
already work locally.

**Why it matters:** There is currently no automated gate running `pytest` or
`black --check` on push/PR — every regression depends on someone remembering
to run tests locally, which is nowhere near "production grade."

**What to do:** Add GitHub Actions (or equivalent) running `poetry install`,
`black --check`, and `pytest` on every PR, plus the MSSQL integration suite
from item 2 as a separate (slower) job once it exists. Add a Docker image
build+smoke-test step given `Dockerfile` exists but has never been built in
this session.

### 21. Principal policy must apply to every MCP/config surface ✅ DONE

**Shipped:** MCP schema tools now build `StructuredQueryService` with the
authenticated MCP caller (`get_mcp_caller()`), so `list_tables` and
`describe_table` return the same principal-filtered view as query
execution. REST and MCP batch-size validation now resolve policy with the
caller principal, closing the gap where a tighter per-principal
`max_batch_size` could be bypassed before `execute_many()` ran.

Also tightened two adjacent policy-resolution seams found during the fix:
cross-connection join-group validation now uses the caller's
principal-specific policy for both the primary and joined connection, and
`session_scope()` can receive the already-resolved execution policy so
session guardrails don't silently fall back to connection-level defaults
during query execution.

Regression coverage added in `tests/integration/test_mcp_server.py`,
`tests/integration/test_rest_api.py`, `tests/unit/test_schema_validation.py`,
and `tests/unit/test_service.py`.

**Effort: S (0.5–1 day).** The policy/principal model already exists; this
is mainly plumbing `Principal` through the remaining surfaces and adding
regression tests. Scope stays small if it is treated as a security
consistency fix, not a redesign of the policy model.

**Why it matters:** The strongest product claim is "agents can query safely
without raw SQL." That promise only holds if every path uses the same
principal-aware policy. During the current scan, the MCP schema tool and
batch-size validation path looked especially important to verify: schema
discovery must not expose tables/columns a caller cannot use, and
per-principal `max_batch_size`/limits must apply identically in REST and
MCP. A single bypass is enough for a security-conscious buyer to distrust
the whole gateway.

**What to do:** Audit every REST and MCP tool entry point and make
principal propagation explicit. Add tests proving that a restricted
principal sees only allowed schema objects, cannot infer denied columns via
schema discovery, and receives the same batch/limit enforcement through
MCP as through REST. Prefer a small helper/factory for creating
principal-scoped `StructuredQueryService` instances so future tools do not
accidentally fall back to connection-level policy.

---

## P1 — needed before general availability

### 5. Policy and connections can't be updated without a process restart ✅ DONE

**Shipped:** `querygate/config_reload.py`'s `reload_config()` rebuilds the
registry and policy store from disk and swaps them in — a single reference
assignment (`set_registry`/`set_policy_store`), atomic under the GIL, so an
in-flight request never observes a half-swapped state. A connection that
was removed or whose `connection_string`/`dialect` changed has its cached
engine disposed (`connections/engine.dispose_engine`, new); an unchanged
connection keeps its pool. Every connection's concurrency semaphore is
reset unconditionally (simpler than diffing `Policy.max_concurrency`
specifically, and reloads are rare/admin-triggered, not hot-path).
Triggered via an authenticated `POST /api/v1/admin/reload-config`, gated
behind a `admin:reload-config` scope (built on item 8) rather than any
authenticated caller — not a SIGHUP handler or file-watcher, which were the
other options `what to do` listed. See `tests/unit/test_config_reload.py`
and the reload tests in `tests/integration/test_rest_api.py`.

**Effort: M (2–3 days).** The reload mechanism itself is small; the real
work is correctness under concurrency — an in-flight request must not see a
half-swapped registry, and engines/sessions tied to a removed or changed
connection need clean teardown, not just dropping the reference.

**Why it matters:** `connections/registry.py` and `policy/loader.py` both
load their YAML file once into a process-wide singleton on first access
(`get_registry()` / `get_policy_store()`). Tightening a policy in response to
an incident, or adding a connection, requires a full redeploy — unacceptable
turnaround time for a security control.

**What to do:** Add a way to reload both stores without downtime — a
SIGHUP handler, an authenticated admin endpoint, or a file-watcher — and
decide what happens to in-flight requests during a reload (should not tear
down active connections using the old registry).

### 6. Per-principal policy doesn't exist — only per-connection ✅ DONE

**Shipped:** `policy.yaml` gained an optional `principals:` section, keyed
by `Principal.subject`, then by connection id (or `"*"` for every
connection) — merged on top of whichever of `default`/`connections` applies
(`PolicyStore.get(connection_id, principal=...)` in `policy/loader.py`).
`MandatoryRowFilter` gained `from_claim: <name>` as an alternative to a
static `value`, resolved from `Principal.claims` at compile time
(`resolve()` in `policy/models.py`, wired through
`compiler/sqlalchemy_compiler.py`'s `_apply_mandatory_row_filters`) —
raises `PolicyViolationError` if the principal lacks the claim, so there's
no silent fallthrough to an unfiltered query. Every `StructuredQueryService`
call site (`execute`/`explain`/`list_tables`/`describe_table`) now resolves
policy through the principal, not just the connection id.

Verified live against a real JWKS-backed JWT flow (two distinct `sub`
claims getting different `denied_tables` results on the same connection),
which is also how a real bug got caught and fixed along the way: the
`CompositeAuthenticator` ordering let the local-dev anonymous fallback
short-circuit *before* a configured JWT was ever checked, because the old
`ApiKeyAuthenticator` matched unconditionally whenever no keys were
configured. Fixed by splitting that out into a separate
`AnonymousAuthenticator`, placed last in the chain and gated on "no real
credential scheme configured" rather than on environment alone — see
`core/auth.py` and the regression test
`test_jwt_enabled_in_local_dev_still_rejects_invalid_tokens` in
`tests/integration/test_rest_api.py`. See also `tests/unit/test_policy_loader.py`,
`tests/unit/test_policy_models.py`, and the per-principal tests in
`tests/unit/test_compiler.py`/`tests/unit/test_service.py`.

**Effort: L (3–5 days).** Touches the policy model, the loader's merge
logic, `validation/policy_validation.py`, the compiler's
`_apply_mandatory_row_filters`, and both auth layers to thread the principal
through — genuinely cross-cutting, not contained to one module. Do item 8
first; this item is much smaller once `Principal` can carry claims.

**Why it matters:** Every authenticated caller of a given connection gets
identical table/column/row access. There's no way to say "agent A can see
`orders` but not `customers.email`; agent B can see both" on the same
connection. `mandatory_row_filters` is the closest thing to per-tenant
scoping, but its `value` is a static literal in the policy file — it can't
be derived from the authenticated principal (e.g., "scope every query to
`tenant_id = <claim from the caller's JWT>`"), which is the actual shape
most multi-tenant SaaS deployments need.

**What to do:** Extend `Policy` (or add a `PrincipalPolicy` layered on top)
so caps/allow-deny/mandatory filters can vary by `Principal.subject` (or a
richer `Principal` with scopes/claims, see item 8), and make
`mandatory_row_filters.value` support a `from_claim: <name>` indirection
resolved from the authenticated principal at query time, not just a fixed
value.

### 7. Health/readiness checks are fake ✅ DONE

**Shipped:** `querygate/health.py`'s `HealthMonitor` pings every enabled
connection (`SELECT 1`) on a background interval
(`AppConfig.health_check_interval_seconds`, default 30s) and caches the
result; `/health` reads the cache rather than pinging synchronously.
`start()` primes every connection's status before returning (bounded by
each connection's own connect timeout) so `/health` never reports
"unknown" for a full interval right after a restart. Returns HTTP 503 (not
just a body flag) when any connection is unhealthy, so a load
balancer/orchestrator actually routes around it. Verified against a real
`uvicorn` process, not just mocked tests — see `tests/unit/test_health.py`
and the health tests in `tests/integration/test_rest_api.py`. Item 22 later
hardened the unauthenticated response to aggregate counts so readiness
checks do not disclose connection ids or raw driver errors.

**Effort: S (~1 day).** One background task per connection pinging on an
interval, plus wiring the cached result into `/health` — small, self
contained in `api/app.py` + a new module.

**Why it matters:** `GET /health` (`api/app.py`) returns a static
`{"status": "ok"}` regardless of whether any configured database is actually
reachable. A load balancer or orchestrator using this for liveness/readiness
will happily route traffic to an instance that can't reach its databases at
all.

**What to do:** Add a real readiness check that (optionally, since pinging
every connection on every health-check request is wasteful) verifies engine
connectivity — at minimum a cached "last known good" ping per connection,
refreshed on an interval, not synchronously per health-check call.

### 8. `Principal` is just a bearer-token subject string — no scopes/claims ✅ DONE

**Shipped:** `Principal` now carries `scopes: frozenset[str]` and
`claims: Mapping[str, Any]`, threaded through `ApiKeyAuthenticator` (via new
`AppConfig.api_key_scopes`/`mcp_api_key_scopes`), both auth layers, and
`StructuredQueryService` (which now takes a `Principal`, not a bare subject
string). `audit_query` logs `principal_scopes` on both success and
rejection. `claims` exists but nothing populates it yet — real values need a
second `Authenticator` (item 10) or per-principal policy (item 6). See
`core/auth.py`, `tests/unit/test_auth.py`, `tests/unit/test_service.py`.

**Effort: S (0.5–1 day).** A dataclass field plus threading it through
`ApiKeyAuthenticator`, `api/auth.py`, `mcp/auth.py`, and `audit_query` calls
— mechanical, low risk. Worth doing early since items 6 and 10 both build on
it.

**Why it matters:** `core/auth.py`'s `Principal` has exactly one field
(`subject`). There's no way to carry roles, scopes, or tenant claims through
to the policy layer, which blocks item 6 (per-principal policy) and any
future RBAC/OAuth work — `Authenticator.authenticate()` would need to return
something richer than a subject string for a JWT-based implementation to be
meaningful.

**What to do:** Extend `Principal` with an extensible claims/scopes field
now, even before a second `Authenticator` implementation exists, so the
interface doesn't need a breaking change later. Thread it through to
`audit_query` (already logs `principal.subject`; extend to log relevant
scopes on rejection for "principal X tried to access denied resource Y").

### 9. Concurrency limiter doesn't work across multiple instances ✅ DONE

**Shipped:** built the distributed version, not just the documentation
minimum bar. `execution/redis_concurrency.py`'s `RedisConcurrencyLimiter`
enforces `Policy.max_concurrency` across every instance sharing one Redis —
each held slot is a member of a per-connection Redis sorted set, scored by
acquisition time; acquiring is one atomic Lua script (`EVAL`, avoids a
check-then-set race between instances) that drops members older than
`concurrency_redis_lease_seconds` before checking capacity, so a slot from
a crashed instance is reclaimed automatically instead of leaking forever.
`AppConfig.concurrency_backend = "redis"` opts in;
`concurrency_redis_fail_open` (default true) decides what happens when
Redis itself is unreachable — fail open (unenforced concurrency, favors
availability) or fail closed, both implemented and tested.

The local Compose stack now includes a health-checked Redis service bound to
`127.0.0.1:6379`, and `.env.example` selects it by default. This makes the
distributed limiter exercised by the normal quickstart instead of requiring
operators to provision an undocumented dependency separately.

Tested against `fakeredis` (real Lua execution via the `lupa` extra, no
real Redis needed for the suite — `tests/unit/test_redis_concurrency.py`,
`tests/unit/test_concurrency.py`) and separately verified against a real
Redis container: two independent OS processes acquiring/releasing the same
connection's slot proved genuine cross-process mutual exclusion, and both
fail-open and fail-closed were exercised against a real unreachable Redis
port.

**Effort: L (3–5 days).** A distributed semaphore/token bucket needs a
backend (Redis is the obvious choice), a client library, failure-mode
handling (what happens when Redis is unreachable — fail open or closed?),
and tests using something like `fakeredis` plus a real-Redis integration
test. The "just document it" alternative is effectively free — the cost is
entirely in choosing to build the distributed version.

**Why it matters:** `execution/concurrency.py`'s `SEMAPHORES` dict is an
in-process `asyncio.Semaphore` per connection id. Run 3 instances behind a
load balancer with `max_concurrency: 8` and the real ceiling against that
database is 24, not 8 — silently defeating the configured guardrail at
exactly the moment it matters (a burst of traffic that triggers
autoscaling).

**What to do:** Either document this loudly as a single-instance-only
guarantee (minimum bar), or replace the in-process semaphore with a
distributed one (Redis-backed token bucket/semaphore is the standard
approach) for deployments that actually run more than one instance.

### 10. No OAuth/JWT — only static API keys ✅ DONE

**Shipped:** `core/jwt_auth.py`'s `JwtAuthenticator` verifies a bearer
token's signature against a JWKS endpoint (`jwt.PyJWKClient`, in-process
cache, default 5-minute lifespan — the JWKS endpoint is hit on a cache miss
only) and maps configurable claims (`jwt_subject_claim`, `jwt_scopes_claim`)
to a `Principal`, including arbitrary claims for per-principal policy (item
6). Selectable via `AppConfig.jwt_enabled` + `jwt_*` fields, shared by REST
and MCP (one identity provider for both). `core/auth.CompositeAuthenticator`
lets a deployment accept both a static API key and a JWT on the same
endpoint. Slotted in without touching `api/routes.py` or `mcp/server.py`,
as required — only `api/auth.py`/`mcp/auth.py` (which *build* the
authenticator) changed.

Building this surfaced a real, separate bug (see item 6's note): the
pre-existing local-dev anonymous-auth bypass could short-circuit before a
configured JWT was ever checked. Fixed as part of this item since it's the
same code path — see `core/auth.AnonymousAuthenticator`.

Tested by mocking the JWKS endpoint against a real RSA keypair and real
`jwt.encode`/`jwt.decode` (`tests/unit/test_jwt_auth.py`) — no real IdP
needed — plus a live end-to-end check against an actual local JWKS HTTP
server serving a real JWK.

**Effort: M (2–3 days).** JWKS fetching/caching and signature verification
are well-trodden (a library does the crypto), but claims-to-`Principal`
mapping, config surface, and testing against a real or mock identity
provider add up. Faster if item 8 has already landed.

**Why it matters:** `core/auth.py`'s `Authenticator` protocol was
deliberately designed to make this additive, but nothing beyond
`ApiKeyAuthenticator` exists yet. Static bearer tokens don't give you
per-caller identity beyond a single shared subject per key, expiry, or
integration with an existing identity provider — all things enterprise
buyers will ask for.

**What to do:** Implement a `JwtAuthenticator` (verify signature against a
JWKS endpoint, map claims → `Principal`) as a second `Authenticator`,
selectable via config. Should slot in without touching `api/routes.py` or
`mcp/server.py` — if it doesn't, the abstraction has a leak worth fixing
first.

### 11. Result size isn't actually bounded — only row *count* is ✅ DONE

**Shipped:** Added `Policy.max_response_bytes` (default 10MB). `execute()`
now truncates rows once their serialized size would exceed the cap
(`_cap_response_bytes` in `execution/service.py`), reusing the existing
`truncated` flag rather than a hard reject — a single row larger than the
cap is still always returned whole, since returning zero rows would look
like an error rather than a cap. See `tests/unit/test_service.py`.

**Effort: S (0.5–1 day).** A running byte-size accumulator while shaping
rows in `execution/service.py`, plus a policy field and a truncation/reject
decision — small, self-contained.

**Why it matters:** `Policy.max_limit`/`max_limit_aggregate` cap row count,
but nothing caps row *width*. A table with a large `TEXT`/`JSONB`/`BLOB`
column selected across 100 rows (a permitted, policy-compliant query) could
still return a very large response body — a resource-exhaustion vector the
row cap doesn't address.

**What to do:** Add a response byte-size cap (truncate or reject past a
configurable threshold) in `execution/service.py`'s result shaping, and
decide the failure mode (truncate with a warning flag vs. hard reject).

### 22. Principal-aware connection/tool visibility ✅ DONE

**Shipped:** Added one shared visibility rule in
`connections/visibility.py`: a connection is reachable only when both its
deployment profile and the caller's resolved principal policy have
`enabled: true`. REST and MCP `list_connections` now return only that
caller's visible connections. Direct schema/query calls and
cross-connection joins use the same resolver; hidden and nonexistent ids
both return `Unknown connection`, preventing discovery-by-probing.

The rule supports a strict, backwards-compatible deny-by-default setup:
set `default.enabled: false`, then set `enabled: true` only for explicit
principal/connection grants. Unknown principals consequently see no
connections. A profile disabled in `connections.yaml` always wins and
cannot be re-enabled by policy. This deployment pattern is documented in
the README and example policy.

Because `/health` is intentionally unauthenticated for orchestrators, it no
longer exposes connection ids or raw driver errors. It reports aggregate
healthy/unhealthy/unknown counts while preserving the existing 200/503
readiness semantics; detailed failures remain in structured logs.

Regression coverage proves REST and MCP callers can see one connection but
not another, cannot access a hidden connection directly, unknown principals
inherit deny-by-default, and deployment-disabled profiles stay hidden.

**Effort: S–M (1–2 days).** The simple version filters existing
`list_connections`/schema output based on the resolved principal. It grows
toward M if the policy model needs a first-class connection allowlist or
role-to-connection mapping.

**Why it matters:** Even if query execution is blocked later, exposing all
connection ids, display names, dialects, or schema shape can leak internal
architecture. Enterprise buyers will expect "least privilege" to apply to
discovery, not only execution.

**What to do:** Define the product rule for connection visibility
(deny-by-default for unknown principals is the safest default), then apply
it consistently to REST `list_connections`, MCP `list_connections`, schema
tools, health detail where relevant, and examples/docs. Add tests for a
principal that can see one connection but not another.

### 23. Persisted audit/event sink ✅ DONE

**Shipped:** Added a versioned `AuditEvent` schema and pluggable `AuditSink`
interface under `querygate/audit/`, with an append-only JSONL implementation
enabled by `.env.example`. Events include event/correlation ids, timestamp,
REST/MCP surface, operation, principal id/scopes, authentication method,
connection id, normalized query shape, policy decision, outcome, duration,
row count, response byte count, truncation, and a fixed error category.

The persisted schema deliberately has no SQL, params, predicate values,
natural-language intent, exception messages, connection strings, or returned
rows. Both success and rejection paths are covered, including auth-method
attribution (`api_key`, `jwt`, `anonymous`) and principal-aware REST/MCP
surface propagation.

`JsonlAuditSink` creates files with mode `0600`, uses append-only writes,
reopens the path for every event so external rename-and-recreate rotation
works, and optionally calls `fsync` per event. Sink failures produce a
distinct `audit.sink.write_failed` structured log without converting an
already-executed database read into a misleading client failure. Retention,
immutable/WORM storage, and SIEM shipping remain operator responsibilities
and are documented explicitly in the README.

Regression coverage in `tests/unit/test_audit.py` proves append behavior,
file permissions, lifecycle configuration/reset, correlation and identity
metadata, rejection categorization, and absence of predicate, intent,
exception, SQL, parameter, and returned-row data.

**Effort: M (2–3 days).** Start with one durable sink (Postgres table,
append-only JSONL file, or webhook) and a narrow event schema. Avoid trying
to build a full SIEM integration framework in the first pass.

**Why it matters:** Structured logs are useful during development, but a
commercial database-access gateway needs durable auditability: who asked
what, which policy allowed/denied it, which connection was touched, how
many rows/bytes were returned, and what correlation id ties it back to the
calling agent. Without this, QueryGate is hard to sell into regulated or
security-reviewed environments.

**What to do:** Introduce an append-only audit event interface with at least
one persisted implementation. Keep row payloads out of the event. Include
principal id, auth method, connection id, tool/API surface, normalized query
shape, policy decision, timing, row count, byte count, and error category.
Document retention and redaction behavior clearly.

### 24. Release hygiene and reproducible v0.1.0 cut ✅ DONE

**Shipped:** QueryGate now has one repeatable source/package gate
(`make release-check`) and one isolated container/infrastructure gate
(`make release-smoke`). The first validates the Poetry lock and synchronized
version metadata, rejects tracked generated/secret/legacy product files,
checks formatting, runs the default suite, validates the bundled example
configuration, builds wheel and sdist, and inspects their members. The second
uses dedicated ports and a disposable Compose project to build the production
image from the wheel, wait for Postgres and Redis, verify readiness and
connection discovery, and execute a real structured query against seeded
Postgres.

The package uses PEP 621 metadata and portable bundled example paths; runtime
version defaults derive from `querygate.__version__`. CI declares its coverage
plugin and runs both artifact checks and the real container/Postgres smoke.
Docker build context exclusions, service health checks, configurable local
ports, a changelog, and `docs/RELEASING.md` make the boundary explicit.
Historical extraction reports were archived, the generated SQLite fixture was
removed from Git, and the README/setup/examples now describe only supported
runtime behavior.

**Verified:** `make release-check` passed with 247 tests (16 explicit
`real_db` tests excluded from the default run), the exact CI coverage command
reported 90%, a fresh environment installed and validated the built wheel from
outside the repository, and `make release-smoke` queried real Postgres through
the production container. The local `v0.1.0` tag is created only after the
release commit is final.

**Effort: S–M (1–2 days).** Mostly cleanup and packaging discipline: decide
what is in the release, what remains experimental, and make the repo state
boringly reproducible.

**Why it matters:** The codebase is currently strong, but the working tree
has a lot of modified and untracked files. That is normal during a product
extraction sprint, but it makes it hard to tell what is intentionally part
of QueryGate and what is leftover migration debris. A serious buyer,
investor, or technical evaluator will judge polish as much as architecture.

**What to do:** Cleanly separate product files from scratch/migration
artifacts, ensure every intended file is tracked, remove stale project
references, verify README/TODO/examples match the actual CLI and runtime,
then tag a clear local release candidate. The release gate should be:
`black --check`, default `pytest`, config validation, Docker build/smoke
test, and at least one real Postgres run.

### 25. Admin/config governance plane ✅ DONE

**Shipped:** A REST-only admin API (`/api/v1/admin/config/*`,
`api/admin_config_routes.py`) layered on top of the existing hot-reload
mechanism (item 5) — never a parallel implementation of it. A new
`querygate/admin/` package owns the model:

- `admin/models.ConfigVersion` — a complete, immutable snapshot of
  connections.yaml + policy.yaml + an optional catalog.yaml (never a diff),
  with `status` (`staged`/`active`/`inactive`), `created_by`/`created_at`,
  `applied_by`/`applied_at`, and `previous_active_version_id` (recorded the
  moment a version becomes active, so every apply/rollback is traceable to
  exactly what it replaced).
- `admin/store.ConfigVersionStore` — a file-backed, versioned history under
  `AppConfig.config_governance_dir` (default `var/config_versions/`), the
  same "file-configured, not a database" posture as connections/policy/
  catalog themselves. History is never rewritten: rollback doesn't create a
  new snapshot, it moves the active pointer back to an older one, so every
  version that ever existed stays inspectable.
- `admin/service.py` — orchestrates validate/preview/stage/apply, reusing
  `cli.validate_config()` (item 17) and `config_reload.reload_config()`
  (item 5) directly rather than re-implementing either: staging validates
  against a scratch directory before persisting anything, and applying
  re-validates once more immediately before activating (closing a real gap
  — a version that validated when staged but stopped validating by apply
  time, e.g. an env var disappeared, must not be silently activated) then
  calls the exact same `reload_config()` swap `/admin/reload-config` uses.

Endpoints: `POST validate` (dry-run, persists nothing), `POST preview`
(added by item 33: content-free document comparison, persists nothing), `POST versions`
(stage — only submitted fields change, the rest inherit from the current
active version), `GET versions` / `GET versions/{id}` / `GET current`
(read-only history), and `POST versions/{id}/apply` — one endpoint serves
both "apply" (a staged version's first activation) and "rollback"
(reactivating a version that was active before); the two are the same
underlying operation, distinguished only by the version's status
immediately beforehand, and labeled accordingly in the audit trail.

Two new scopes, split the way most admin APIs separate visibility from
mutation: `admin:config:read` (list/inspect history) and
`admin:config:write` (validate/preview/stage/apply/rollback) — deliberately
separate from item 5's `admin:reload-config`, since "reload whatever's on
disk" (infra-as-code workflow) and "submit new content over the API"
(this item's workflow) are different operational modes that coexist
without either depending on the other.

Auditing extends the existing sink rather than adding a parallel one: a new
`ConfigChangeEvent` (`audit/events.py`) — validate/preview/stage/apply/rollback
action, version id, previous
version id, actor, outcome, never the YAML content itself — flows through
the same configured `AuditSink` (JSONL or none) as query-execution
`AuditEvent`s, so one sink configuration and one file covers "who queried
what" and "who changed access to what" together. `AuditSink.emit()`'s type
widened to a `PersistableEvent` union to carry both.

Verified live against a real running app and real Postgres, not just
mocked tests: staged a policy version that additionally denied
`customers.name`, applied it and confirmed the column was actually
rejected on a real query, then rolled back and confirmed the original
policy (and query result) was restored, with version history correctly
attributing both the stage and the two applies. See
`tests/unit/test_admin_store.py`, `tests/unit/test_admin_service.py`,
`tests/integration/test_admin_config_governance.py`, and the governance
scope/validation-integrity tests in `tests/security/test_adversarial_security.py`.

**Explicitly out of scope for this pass** (matches this item's own "can be
started as REST-only admin endpoints before adding UI" framing, not a
gap discovered late): no approval/four-eyes workflow, no scheduled apply,
no detailed semantic diff (item 33 later added a deliberately content-free
document-level preview), and no admin UI (TODO item 31, which depends on this
item's API existing first).

**Effort: L (1–2 weeks).** This is a new control-plane slice. It can be
started as REST-only admin endpoints before adding UI, but it needs careful
authorization and audit design from day one.

**Why it matters:** YAML files plus hot reload are good for a technical MVP;
they are not enough for teams that need approvals, change history,
rollback, separation of duties, and visibility into who changed access to a
database. This is where QueryGate starts feeling like an enterprise product
rather than a library.

**What to do:** Add an admin API for validating, staging, applying, and
rolling back connection/policy changes. Every change should produce an
audit event, include actor identity, support dry-run validation, and expose
the currently active config version. Keep actual secret values write-only or
externalized through item 13.

---

## P2 — hardening and scale

### 12. No metrics/observability beyond structured logs ✅ DONE

**Shipped:** `querygate/metrics.py`, exposed via `GET /metrics`
(Prometheus text format, own `CollectorRegistry` — not the global default).
`querygate_queries_total{connection,status}`,
`querygate_queries_rejected_total{connection,reason}` (reason is a fixed,
low-cardinality bucket — `policy`/`schema`/`concurrency`/`db_error`, added
a dedicated `ConcurrencyLimitError` exception type so classification
doesn't have to sniff message text), `querygate_query_duration_seconds`,
and `querygate_concurrency_in_use`/`querygate_concurrency_max` gauges.
"timeout" specifically isn't broken out as its own reason yet — item 3
hasn't established a reliable, dialect-verified way to detect a genuine
query timeout distinct from any other DB-layer failure, so it's honestly
bucketed under `db_error` rather than guessed at. Never raw SQL, exception
text, or row data in a label — same invariant as `audit/logger.py`. See
`tests/unit/test_metrics.py`, `tests/unit/test_concurrency.py`, and the
metrics assertions in `tests/unit/test_service.py`.

**Effort: M (2–3 days).** Instrumentation itself is quick per-module, but
it touches several files (concurrency, service, validation) and needs
thought about label cardinality (e.g., don't put raw SQL in a metric label)
and an exporter choice (Prometheus vs. OTel).

**Why it matters:** `core/logging.py` emits JSON lines to stdout, which is
fine for log aggregation but gives no cheap way to alert on "concurrency
saturation is climbing" or "rejection rate spiked" without parsing logs.
Production operators expect Prometheus-style metrics (or OpenTelemetry) for
this class of guardrail.

**What to do:** Add counters/histograms for: queries executed, queries
rejected (by reason: policy/schema/timeout/concurrency), query duration,
per-connection concurrency-slot utilization, and expose via `/metrics` or an
OTel exporter.

### 13. No secrets-manager integration for connection strings ✅ DONE

**Shipped:** A new `querygate/secrets/` module defines a `SecretResolver`
protocol — one method, `resolve(reference: str) -> str` — mirroring the
`Authenticator` (`core/auth.py`) and `AuditSink` (`audit/sinks.py`)
pluggable-backend shape already used elsewhere in this codebase.
`connections/registry.py`'s interpolation is now scheme-dispatched through a
`SecretResolverRegistry`: a bare identifier (`${QUERYGATE_DEMO_DB_URL}`)
still means an environment variable, unchanged; anything else must be
`${scheme:reference}` (e.g. `${vault:querygate/demo-db#connection_string}`),
routed by `scheme` to whichever resolver is registered for it. Adding a
future backend (AWS Secrets Manager, GCP Secret Manager) is a new resolver
class plus one registration line in `build_secret_resolver_registry` —
`ConnectionRegistry`, `config_reload.py`, and `querygate-validate-config`
never change.

Shipped one real backend, `VaultSecretResolver`, reading a HashiCorp Vault
KV v2 secret (`<path>#<field>`) via `hvac`, token auth only for this first
pass. Selectable via `AppConfig.vault_enabled` + `vault_addr`/`vault_token`/
`vault_kv_mount`/`vault_namespace`; validated at config-load time the same
way `jwt_enabled` requires `jwt_jwks_url`. Every `${...}` reference —
including `${vault:...}` — is re-resolved on every config load or hot
reload (item 5's `POST /api/v1/admin/reload-config`), so a rotated secret
takes effect on the next reload without a restart, closing the rotation gap
this item's own "why it matters" called out.

Error handling is deliberately conservative: a `VaultError`/network failure
is wrapped in a `ValueError` carrying only the secret path and the
exception's type name — never the configured token or Vault's own
response text, which could otherwise end up in an operator's terminal, a
CI log, or a support ticket. Covered by `tests/unit/test_secrets.py`,
`tests/unit/test_connections_registry.py`, `tests/unit/test_cli.py`,
`tests/unit/test_config_reload.py`, a REST integration test proving a
`${vault:...}`-backed connection actually resolves through the real admin
reload endpoint, and two adversarial security tests (a masked Vault error,
and a Vault-resolved secret confirmed absent from connection listing and
error responses).

**Not done in this pass** (left for a real follow-up, not silently
dropped): AppRole/Kubernetes Vault auth (token auth only), AWS/GCP Secrets
Manager backends, and rotation of `VAULT_TOKEN` itself (still a static,
env-configured credential — see `docs/THREAT_MODEL.md`'s residual risks).

**Effort: M–L (2–4 days).** One backend (e.g. Vault) is the M end; doing it
generically pluggable plus wiring in rotation (which leans on item 5) pushes
toward L. Cleanest if scoped to one backend first rather than a fully
generic abstraction up front.

**Why it matters:** `connections/registry.py`'s `${VAR}` interpolation reads
from `os.environ` only. That's fine for a single-node deployment but doesn't
integrate with Vault/AWS Secrets Manager/GCP Secret Manager rotation flows
that many enterprise deployments require, and offers no rotation story
(a changed secret needs a process restart to pick up, same root cause as
item 5).

**What to do:** Add a pluggable secret-resolution interface alongside env
var interpolation (e.g. `${vault:secret/path#field}`), and combine with the
reload mechanism from item 5 so rotated secrets don't need a restart.

### 14. Docker image has never been built or run in this session ✅ DONE

**Shipped:** the estimate's own warning was correct — the MSSQL ODBC
apt-install step had rotted (`python:3.11-slim` floated to Debian 13/
trixie; Microsoft's trixie apt repo is signed with a key not present in
their own published key file — a confirmed open upstream issue, not
something fixable from here). Fixed by pinning `Dockerfile`'s both stages
to `python:3.11-slim-bookworm` and properly dearmoring the Microsoft signing
key into a binary keyring instead of piping the ASCII-armored key straight
into `trusted.gpg.d/`. Verified with a real local `docker build` +
`docker run`, hitting `/health` and `/api/v1/connections` against the live
container. This shipped as a side effect of building item 4's CI workflow,
which embeds the same build+smoke-test as a job.

**Effort: S, but volatile (0.5–1 day if it just works; can balloon toward
M–L if the MSSQL ODBC apt-install step has rotted).** Everything else in the
Dockerfile is standard and low-risk; that one step is the entire variance.

**Why it matters:** `Dockerfile` was rewritten off a public base image as
part of the QueryGate migration but was never actually built — the MSSQL
ODBC driver installation step (`msodbcsql18` via Microsoft's apt repo) in
particular is exactly the kind of step that silently rots (URL changes,
package renames, GPG key format changes).

**What to do:** Build the image in CI (see item 4), run the container, and
hit `/health` and `/api/v1/connections` against it as a smoke test.

### 15. No load/soak testing of the concurrency and timeout guardrails ✅ DONE

**Shipped:** `tests/integration/test_postgres_load_guardrails.py` is a
repeatable asyncio/HTTPX load harness that drives simultaneous structured
queries through the real REST application, compiler, session, and Postgres
engine. It polls Postgres's own `pg_stat_activity` during every burst, so the
assertion is the database's observed in-flight query count rather than a
client-side counter. At `max_concurrency: 2`, it proves both documented modes:
a short wait admits exactly two of eight requests and rejects six with the
actionable REST `422`; a long wait queues six requests in waves and completes
all of them while the database peak stays exactly two. A separate four-second
probe under a one-second policy timeout proves cancellation still happens
under over-cap concurrent load and leaves no probe query running afterward.

The bounded three-round gate runs in the existing real-Postgres CI job and
locally via `make test-load`; `make test-soak` repeats the same machine-checked
scenarios 100 times by default (`SOAK_ROUNDS=N` is configurable). The harness
creates and tears down isolated `querygate_load_*` database objects and is
documented in `docs/LOAD_TESTING.md`. Scope is deliberately the single-process
semaphore plus real database execution: the Redis limiter's cross-instance
atomicity, lease recovery, and failure modes remain covered by item 9's Redis
suite.

**Effort: M (1–2 days).** Writing the load script is quick; the time sink is
running it against a real database repeatedly and interpreting results
(is a slowdown the guardrail working as intended, or a real bottleneck?).

**Why it matters:** The concurrency semaphore and timeout logic are
unit-tested for correctness in isolation (`tests/unit/test_concurrency.py`)
but never tested under realistic concurrent load against a real database —
which is exactly the scenario these guardrails exist for.

**What to do:** Add a load test (locust/k6/a simple asyncio script) that
fires concurrent requests past `max_concurrency` and confirms the observed
in-flight count against the real database never exceeds the configured cap,
and that rejected/queued requests behave as documented.

### 16. Column-level policy is case-sensitive and untested cross-dialect ✅ DONE

**Shipped:** `Policy.column_allowed`'s `denied_columns`/`allowed_columns`
dict lookups are now case-insensitive (`Policy._ci_lookup` in
`policy/models.py`) — `table_allowed` and the compiler's
`_apply_mandatory_row_filters` were already case-insensitive; this closed
the one remaining gap. See `tests/unit/test_policy_validation.py`.

**Effort: XS (a few hours).** A normalization pass in `policy/models.py` and
wherever table names are collected during reflection, plus one test with a
mixed-case table name — small and low-risk.

**Why it matters:** `Policy.column_allowed`/`table_allowed`
(`policy/models.py`) lowercase both sides before comparing, but the *keys*
of `allowed_columns`/`denied_columns` dicts are matched by exact table-name
string from the policy YAML — if a table name's casing differs between what
an admin wrote in `policy.yaml` and what the connection's reflection
actually returns (dialect-dependent — Postgres lowercases unquoted
identifiers, MSSQL usually preserves case), a denied-column rule can
silently fail to match.

**What to do:** Normalize table-name casing consistently (probably: always
lowercase when reflecting and when matching policy keys) and add a test
using a mixed-case table name.

### 17. No policy/connections file validation tooling ✅ DONE

**Shipped:** `querygate-validate-config` (registered as a poetry script,
`make validate-config`) loads both files through the same Pydantic
validation the app uses at runtime and cross-checks that every
`connections:` key in `policy.yaml` matches a real connection id, reporting
all problems found rather than stopping at the first. See
`src/querygate/cli.py`, `tests/unit/test_cli.py`. Not yet wired into CI
(that's item 4).

**Effort: S (0.5–1 day).** Thin wrapper around logic that already exists
(`ConnectionRegistry.from_file`, `PolicyStore.from_file` already raise on
most bad input) — mostly packaging it as a runnable CLI plus the
cross-file id check that doesn't exist yet.

**Why it matters:** A typo in `policy.yaml` or `connections.yaml` (bad YAML,
wrong type, unknown connection id in a policy override) currently only
surfaces as a runtime error the first time something touches the affected
connection — not at deploy time.

**What to do:** Add a small CLI (`querygate validate-config` or similar)
that loads both files, runs full Pydantic validation, and cross-checks that
every `connections:` key in `policy.yaml` corresponds to a real connection
id — runnable in CI/CD before a config change ships.

### 26. Query-cost estimation before execution

**Phase 1 (Postgres `EXPLAIN`-based estimation) ✅ DONE.** **Phase 2 (MSSQL
estimated-plan equivalent) not started — split out below because it needs a
different connection-lifecycle shape than phase 1's, not just more test
coverage.**

**Phase 1 shipped:** `execution/cost_estimation.py`'s
`estimate_postgres_query_cost()` plans (never runs) the already-validated,
already-compiled `Select` with `EXPLAIN (FORMAT JSON)` — the statement is
rendered once with `literal_binds=True` (the same fallback-on-failure
pattern `execution/service.py`'s `_compile_to_text` already uses) so the
whole EXPLAIN is one self-contained string; no data is exposed by doing this
since EXPLAIN never executes the statement. It reads the root plan node's
`Plan Rows`/`Total Cost` and hands them to `enforce_cost_estimate()`, which
raises a new `CostEstimateExceededError` (subclasses `PolicyViolationError`,
mirroring `ConcurrencyLimitError`'s rationale — same client-error handling,
but its own metrics reason: `querygate_queries_rejected_total{reason="cost_estimate"}`,
broken out from the coarser `policy` bucket) with a message that tells the
agent what to do next ("narrow the query with additional filters, a smaller
limit/top_n, or a more selective time range"), not just that it was denied.

New `Policy.max_estimated_rows`/`max_estimated_cost` (both `Optional`,
default `None` — unset means fully disabled, zero behavior change for
existing deployments) gate this in `StructuredQueryService.execute()`,
inside the same session/transaction already opened for the real query — one
extra round-trip, not a second connection. Deliberately **not** wired into
`explain_structured_query`: that call has an existing, tested invariant
(`test_explain_does_not_open_a_db_session`) that it never touches the
database at all, staying a pure, always-cheap compile preview; adding a live
EXPLAIN round-trip there would break that contract for a feature explicitly
scoped to gating `execute()`.

**Fail-open by design, not fail-closed:** if EXPLAIN can't be obtained or
parsed for a given query (an unusual construct that can't render with
literal binds, an unexpected plan shape), `estimate_postgres_query_cost()`
logs a warning and returns `None` rather than raising — the query proceeds
and is still bounded by every existing reactive guardrail (row caps,
timeout, concurrency, response-byte cap). This is a deliberate trade-off:
the feature adds proactive rejection of *likely* full scans/join
explosions without becoming a new way to accidentally block legitimate
traffic on an EXPLAIN edge case.

**Follow-up shipped in this same pass — fail-open observability and a
calibration mode, so the two honest caveats above ("fails open" and
"thresholds aren't portable, so they need per-deployment tuning") aren't
silent gaps:**

- `querygate_cost_estimation_attempts_total{connection}` and
  `querygate_cost_estimation_unavailable_total{connection,reason}` (reason:
  `compile_failed`/`explain_failed`/`plan_parse_failed`) make the fail-open
  path observable instead of only a stdout warning — an operator can alert
  on the unavailable counter climbing, which means the gate has silently
  stopped evaluating queries on that connection, rather than discovering it
  after the fact.
- New `Policy.cost_estimation_mode` (`CostEstimationMode`, default
  `ENFORCE`) adds `OBSERVE`: the estimate is still computed and compared
  against the threshold, but a would-be rejection is only recorded (a
  `cost_estimation.observed_would_reject` log line plus
  `querygate_cost_estimation_would_reject_total{connection}`), never
  raised. `execution/cost_estimation.py`'s `cost_estimate_violations()` is
  the single source of truth both `enforce_cost_estimate()` (ENFORCE) and
  `StructuredQueryService._observe_cost_estimate()` (OBSERVE) build on, so
  the two modes can never disagree about what counts as a violation. Lets
  an operator calibrate `max_estimated_rows`/`max_estimated_cost` against
  real production traffic before switching a connection to `ENFORCE`,
  instead of guessing a threshold from documentation on day one.

Covered by `tests/unit/test_cost_estimation.py` (attempts/unavailable
metrics per failure path, `cost_estimate_violations`/
`format_cost_estimate_violation_message`), `tests/unit/test_service.py`
(OBSERVE mode runs the query instead of rejecting; does not flag a query
within threshold), and a real-Postgres
`test_observe_mode_runs_the_query_and_records_would_reject` in
`tests/integration/test_postgres_cost_estimation.py`.

MSSQL is explicitly a no-op, not an error: a policy with these fields set on
an MSSQL connection is valid and simply has no effect there (see the phase 2
write-up below for why). Covered by `tests/unit/test_cost_estimation.py`
(estimator parsing success/failure/fail-open paths, `enforce_cost_estimate`
threshold combinations), the wiring tests in `tests/unit/test_service.py`
(estimation disabled by default, MSSQL no-op, execute rejects over threshold
while explain never opens a session), and
`tests/integration/test_postgres_cost_estimation.py` against a real
Postgres — a genuinely large sequential-scan-shaped query is rejected under
a small `max_estimated_rows`, a selective indexed query passes under the
same policy, and disabling the gate (the default) never issues an EXPLAIN at
all. `docs/THREAT_MODEL.md`'s QG-08 row and residual-risk section were
updated; `help/service.py`'s redacted policy summary now reports these new
guardrail values (including `cost_estimation_mode`) like every other cap.

**Phase 2 — MSSQL estimated-plan equivalent, not started:** SQL Server's
`SET SHOWPLAN_XML ON` can't be prefixed onto an already-compiled statement
the way Postgres's inline `EXPLAIN (FORMAT JSON) <query>` can — once
SHOWPLAN mode is set, it must be the *only* statement in its batch (the
query being planned can't run in the same batch as the `SET`), so getting an
estimated MSSQL plan needs a dedicated connection/session lifecycle (open a
connection, `SET SHOWPLAN_XML ON`, run the query text to get its plan
without execution, then discard that connection rather than reusing it for
the real query) rather than one extra statement inside the existing session.
That's a genuinely different code shape, not a bigger version of phase 1's
approach — tracked here as its own follow-up.

**Effort: L (3–5 days for one dialect, longer cross-dialect).** The hard
part is not calling `EXPLAIN`; it is turning dialect-specific plan output
into a conservative, understandable policy decision without blocking safe
queries unnecessarily.

**Why it matters:** Row limits, timeouts, and concurrency caps are reactive
guardrails. A 10/10 gateway should also be proactive: reject or warn on
queries that are likely to full-scan huge tables, explode joins, or stress a
production database before they run. This is a differentiator against
generic MCP database connectors.

**What to do (phase 2):** Add an MSSQL estimated-plan path with its own
connection lifecycle (`SET SHOWPLAN_XML ON` in a dedicated session), extract
comparable row/cost signals from the returned plan XML, and reuse the same
`Policy.max_estimated_rows`/`max_estimated_cost` gate and
`CostEstimateExceededError` phase 1 already established rather than
inventing a parallel mechanism.

### 27. Semantic schema catalog and sensitivity metadata ✅ DONE

**Shipped:** A new `querygate/catalog/` module (`models.py` + `loader.py`)
mirroring the existing `policy/` module's shape: an optional, versioned,
YAML-file-configured overlay (`CATALOG_FILE`, unset by default — a
deployment with no curated catalog behaves identically) keyed by connection
→ table → column, each entry carrying `description`, `aliases`,
`sensitivity` (`none`/`internal`/`confidential`/`pii`), and `allow_samples`,
plus table-level `default_aggregation` and `relationships` (curated,
non-enforced join hints: `to_table`/`column`/`to_column`/`description`).

`StructuredQueryService.describe_table` merges the resolved entry into a new
`catalog` object on the response — one per table and one per column — for
both MCP's `describe_table` tool and the REST `GET
/{connection}/tables/{table}` endpoint (they share the same
`TableDescription` model, so no separate REST wiring was needed).
`list_tables` is deliberately unchanged in this pass — enriching it would
break its existing `List[str]` response contract on both REST and MCP; left
for a follow-up if a lightweight per-table blurb in the list view turns out
to matter in practice.

**Principal-safe by construction**, the security-sensitive part of this
item: a denied column never reaches catalog lookup at all, because it's
already excluded from `describe_table`'s column list by the pre-existing
policy filter before catalog metadata is attached. Table-level
`relationships` needed their own explicit filter
(`catalog.models.visible_relationships`) — an admin's curated "orders joins
customers via customer_id" hint is dropped from the response when the
caller's resolved policy denies `customers`, so curated metadata can never
disclose more than ordinary schema/policy discovery already allows. Covered
by a dedicated adversarial test,
`test_catalog_relationship_hint_cannot_disclose_a_denied_table`, plus the
threat model's new QG-13 entry.

Hot-reloadable without a restart through the existing config-reload
machinery: `config_reload.reload_config()` gained an optional
`catalog_file` parameter and swaps in a fresh `CatalogStore` alongside the
registry/policy swap; the REST `POST /api/v1/admin/reload-config` endpoint
(same `admin:reload-config` scope, no new endpoint) now reports
`catalog_connection_ids` too. `querygate-validate-config` gained an optional
`--catalog-file` flag that structurally validates the file and cross-checks
its connection ids against the real registry, mirroring the existing
policy cross-check.

Bundled `examples/catalog.example.yaml` curates the demo's
`customers`/`orders`/`order_items` tables (including marking
`customers.email` as `pii`, matching `policy.example.yaml`'s existing
denied-column example) and is wired into `.env.example` via `CATALOG_FILE`
so the quickstart demo shows the feature end to end.

**Explicitly out of scope for this pass** (this is item 27's own "what to
do" scope, not item 32's — no per-entry provenance/status
(draft/verified/stale), no approval workflow, no model-generated content, no
usage-derived learning, and no row-level sample values — `allow_samples` is
captured as a metadata flag now but nothing in this repo generates or
returns samples yet). Those remain item 32's job, which depends on this
item's data model.

**Effort: L (1 week for a useful first version).** Reflection exists; the
new work is storing curated metadata and making it available to agents
without leaking sensitive samples.

**Why it matters:** Raw table/column discovery is necessary but not enough
for excellent agent behavior. Agents need business names, descriptions,
relationship hints, safe examples, sensitivity labels, and "do not use this
unless..." guidance. This is also where QueryGate can become more valuable
than a cloud vendor's generic toolbox.

**What to do:** Add a versioned schema catalog overlay that admins can
curate per connection/table/column: descriptions, aliases, relationship
hints, sensitivity class, default aggregation preference, and whether sample
values are allowed. Feed that metadata into MCP schema tools and REST
schema responses, filtered by principal policy.

### 28. Threat model + adversarial security test suite ✅ DONE

**Shipped:** Added `docs/THREAT_MODEL.md`, covering assets, trust boundaries,
attacker capabilities, twelve concrete threat classes, implemented controls,
deployment requirements, verification links, and explicit residual risks. It
is intentionally described as a first-party model—not an external penetration
test or compliance certification.

Added a dedicated `security` pytest marker, `tests/security/` adversarial
suite, and `make test-security`. The suite attacks denied-column inference
through filters/grouping/having/ordering/ranking/join keys, denied-table
smuggling, undeclared table injection, bound-value SQL injection, principal
policy bleed, tenant filters on aggregate queries, single-row output-cap
bypass, backend error disclosure, and MCP DNS rebinding.

The work closed four confirmed gaps: table policy now covers every qualified
reference rather than only projected/declared tables; schema validation
rejects undeclared tables and malformed join graphs before reflection;
unexpected REST/MCP/batch errors are client-safe; and the response byte cap
omits a first row that alone exceeds the ceiling. MCP Host/Origin validation
is also enabled by default and configurable for reverse-proxy deployments.

**Effort: M (2–4 days).** The first pass can be a written threat model plus
tests for the most likely bypass classes; it does not require a formal
third-party audit yet.

**Why it matters:** QueryGate's buyer is effectively trusting it to sit
between autonomous agents and production data. The important failure modes
are not only SQL injection; they include policy bypass through discovery,
aggregation inference, oversized outputs, prompt-injection-style tool
misuse, error-message leaks, and tenant/principal confusion.

**What to do:** Write a `docs/threat-model.md` covering assets, trust
boundaries, attacker capabilities, and mitigations. Add adversarial tests
for denied columns in filters/order/grouping, schema leakage, aggregation
edge cases, large payload attempts, invalid JWT/scopes, cross-principal
policy overrides, and sanitized DB errors.

### 29. Production deployment reference stack ✅ DONE

**Shipped:** A `deploy/` directory with two verified reference stacks — a
production-ish Docker Compose file and a Helm chart — sharing the same
shape: app + Redis (distributed concurrency, required once more than one
instance runs), config/secrets mounted separately (connections/policy/
catalog YAML is safe to commit — always `${VAR}`/`${vault:...}` references,
never literal secrets — but the resolved values live in a gitignored env
file or an operator-managed Secret), a Prometheus scrape-config example,
liveness/readiness probes against `/health`, and `deploy/runbook.md`
covering both config-reload paths (direct file edit + `/admin/reload-config`
vs. the item-25 governance API), secret rotation, and rollback (both
config-version rollback and application-version rollback).

Neither stack was shipped from rendering alone — both were verified against
a real deployment, and doing so caught a real bug before it shipped: a
fresh named Docker volume is root-owned by default, but the production
image runs as a non-root user (uid 100, gid 101 — confirmed via `docker run
--rm <image> id`, not assumed), so the audit JSONL sink failed with
`PermissionError` on first write. Fixed in the Compose stack with a
one-shot `busybox` init container that chowns the volume before `querygate`
starts (`depends_on: condition: service_completed_successfully`); the Helm
chart doesn't need the equivalent workaround because Kubernetes'
`securityContext.fsGroup` (a mechanism Compose has no equivalent of) fixes
volume ownership automatically, confirmed by inspecting the pod's actual
mounted-volume permissions after a real deploy.

Verification, concretely: the Compose stack was brought up against the real
demo Postgres, executed a real structured query, confirmed `/metrics`
populated post-query, confirmed the audit JSONL file was written with
correct ownership and mode `0600`, and confirmed the optional
`--profile monitoring` Prometheus service actually scraped `/metrics`
(`health: "up"` via Prometheus's own targets API). The Helm chart was
`helm lint`ed, rendered with every optional feature enabled
(`autoscaling`, `podDisruptionBudget`, `metrics.serviceMonitor`,
`persistence`, `secrets.create`) to exercise every template's conditional
path, then actually installed into a real local `kind` cluster — 2
replicas, a real in-cluster Postgres and Redis, a real structured query
through a port-forwarded Service, `/metrics` populated, the non-root
`securityContext` confirmed via `kubectl exec ... id`, and the audit file
confirmed written correctly inside a live pod.

**Effort: M (2–4 days).** Docker already exists; this adds the operational
wrapper that lets another team deploy it correctly without guessing.

**Why it matters:** A product-grade infrastructure component should have an
obvious path from clone to a realistic deployment: app container, Redis,
metrics scraping, health checks, config/secrets mounting, TLS/proxy
assumptions, and upgrade notes. Without this, every evaluator has to invent
their own production shape.

**What to do:** Add a `deploy/` reference stack: production-ish Docker
Compose and/or Helm chart, example secret mounting, Redis setup for
distributed concurrency, Prometheus scrape config, liveness/readiness
probes, and a short runbook for config reloads, rotations, and rollback.

### 30. Distribution, SBOM, and signed release artifacts

**Phase 1 (SBOM + dependency audit + checksums) ✅ DONE.** **Phase 2
(publishing to a real registry/index + cryptographic signing) not started —
split out below because it needs infrastructure and credentials this
environment does not have and this project does not yet operate.**

**Phase 1 shipped:** `scripts/generate_sbom.py`, run as the last step of
`make release-check` (and as its own `make sbom` target). It reads
`poetry.lock`'s `main` dependency group — the exact locked set QueryGate
ships, filtered by platform markers, not whatever an unpinned `pip install`
would resolve today — installs those pins `--no-deps` plus the built wheel
into a throwaway venv (so the repo's own dev-tooling dependencies never
pollute the SBOM or the audit), and produces three artifacts in `dist/`:
a CycloneDX 1.6 SBOM (`querygate-<version>.cdx.json`), a `pip-audit`
vulnerability report (`querygate-<version>.vuln-report.json`), and a
`SHA256SUMS` checksum manifest covering the wheel, sdist, and SBOM.

The dependency audit is a real, deny-by-default release gate, not a report
nobody reads: any known vulnerability in the locked production dependency
set fails `make release-check` unless it has a reviewed entry in
`security/dependency-audit-allowlist.json`, keyed by advisory id, recording
the affected package and — verified against this repository's actual code,
not assumed — a specific reason the finding doesn't reach a real QueryGate
code path (e.g. a deprecated transport QueryGate never mounts, a function
QueryGate never calls, a header QueryGate never uses for authorization).

Running this on `poetry.lock`'s locked versions today surfaced 13 real,
currently-unpatched advisories across 5 production-reachable dependencies —
`click`, `idna`, `mcp`, `python-dotenv`, `starlette` — each reviewed and
allowlisted with its specific non-applicability reason (see the file). One
is worth calling out explicitly: `mcp` 1.12.4 (PYSEC-2026-1617) doesn't
enable DNS-rebinding protection by default, but `mcp/server.py` already
enables it independently at the application layer (`TransportSecuritySettings(
enable_dns_rebinding_protection=True, ...)`, also covered by item 28's
adversarial suite) — a real compensating control, not just a documentation
note. `setuptools`/`pip`/`wheel` findings are excluded everywhere: they're
bootstrapped into every fresh virtualenv by `ensurepip`, not a package
QueryGate's `poetry.lock` `main` group actually declares.

**Explicitly out of scope for phase 1, tracked as phase 2:**

- **Publishing to a registry.** No package (PyPI/private index) or container
  registry has been chosen or configured; `docs/RELEASING.md` still describes
  local-only artifacts, and pushing/publishing requires explicit maintainer
  approval before it can happen at all.
- **Cryptographic signing (e.g. Sigstore/cosign keyless signing).** Signing
  is only meaningful once there's a real published artifact and registry to
  attach the signature and transparency-log entry to — building it against
  nothing to sign would be security theater, not a control.
- **Remediating the 13 allowlisted findings** — i.e. actually upgrading
  `click`/`idna`/`mcp`/`python-dotenv`/`starlette` past their locked versions.
  `mcp` in particular is a core protocol dependency threaded through every
  `mcp/tools/*.py` module and the FastMCP forward-reference-resolution
  behavior this codebase already works around (see this file's testing
  gotchas in `CLAUDE.md`) — bumping it needs its own dedicated regression
  pass against the full MCP surface, not a drive-by version bump alongside
  unrelated SBOM tooling.
- **Container-image-level SBOM** (e.g. `anchore/sbom-action` against the
  built Docker image) — phase 1 covers the Python package's dependency
  closure, which is where `poetry.lock` gives precise, lockfile-driven
  answers; the base OS image's own package inventory is a separate, later
  addition.

**Effort: M (2–3 days) for phase 1 (done); phase 2 (registry publishing +
signing) is realistically its own S–M slice once a registry is chosen, plus
whatever time the dependency remediation above needs — that's a security
regression-testing task, not a packaging one.**

**Why it matters:** To be taken seriously by security teams, QueryGate
should ship with repeatable artifacts and supply-chain metadata: versioned
Python package, container image tags, lockfile discipline, SBOM, and ideally
signed images/releases. This is not glamorous, but it creates a lot of
buyer confidence.

**What to do (phase 2):** Choose and configure a package/container registry,
wire publishing into a release workflow gated on explicit maintainer
approval (never automatic), add Sigstore/cosign keyless signing of published
images once there's something real to sign, and — separately — work through
`security/dependency-audit-allowlist.json`'s current entries, upgrading each
dependency and removing its allowlist entry once a real regression pass
against the affected surface (MCP tools for `mcp`, ASGI routing for
`starlette`) confirms nothing breaks.

### 33. Permission-aware QueryGate product guide and configuration assistant ✅ DONE

**Shipped:** a new `querygate/help/` product-knowledge boundary with ten
canonical Markdown topics packaged in the wheel and tied to the installed
QueryGate version. `GuideCorpus` loads them through `importlib.resources` and
performs deterministic weighted lexical retrieval entirely offline — no model
provider, embeddings service, customer rows, live database, or deployment
configuration enters the search index. Results and full topics carry stable
topic ids, safe next actions, source paths, and the QueryGate version they
describe. Configuration-field explanation is generated from the current
`AppConfig`, `ConnectionProfile`, `Policy`, and `SchemaCatalog` Pydantic models,
so new fields are automatically discoverable rather than relying only on
hand-maintained prose.

REST now exposes public static guidance at `/api/v1/help/search`,
`/topics/{topic_id}`, `/setup-checklist`, `/config-fields/{model}/{field}`, and
`/errors/{error_code}`. Authenticated `/help/my-access` returns only the current
principal's own subject/auth method/scopes, derived capability flags, and the
connections already filtered by the shared principal-aware visibility layer.
`/help/configuration` additionally requires `admin:config:read` and reconstructs
an allowlisted redacted projection: no raw YAML, connection string, credential
value/reference, mandatory-filter value/claim, table/column policy identifier,
catalog identifier/description, free-form configuration description, or other
principal subject is serialized.
Live context is assembled per request and is never put in the static corpus or
a cross-principal cache.

MCP has matching read-only tools: `search_querygate_guide`,
`get_querygate_guide_topic`, `get_querygate_setup_checklist`,
`explain_querygate_config_field`, `explain_querygate_error`,
`describe_my_querygate_access`, and `inspect_querygate_configuration`. Server
instructions explicitly tell agents to use the installed guide instead of
guessing from model memory and keep every mutation in the governance plane.

The existing config workflow gained `POST /api/v1/admin/config/preview`: a
config writer can validate a proposed change and see which of the three config
documents was submitted or inherited, but receives no contents, identifiers,
hashes, secret references, or line-level diff that could turn write scope into
read scope. A caller that also holds `admin:config:read` receives the actual
document-level changed/unchanged comparison.
Validate and preview actions are attributed and persisted as metadata-only
`config.governance` audit events; stage/apply/rollback still use item 25's
original version store and reload path, with no second mutation mechanism.

Release-blocking coverage now checks corpus/runtime version agreement, every
current config-model field's generated explanation, representative onboarding/
configuration/troubleshooting/operations/upgrade search tasks, REST/MCP tool
parity, role/scope behavior, document-level preview behavior and auditing,
cross-principal context isolation, hidden-name search attempts, literal/env/
Vault credential redaction, and inclusion of the full guide corpus in built
wheel artifacts. The QG-16 threat-model entry records the new guide-oracle and
cache-confusion boundary.

**Deliberately not added:** a hosted-model answer generator, embeddings/vector
database, arbitrary log/request-id inspection, mutating MCP guide tools, or a
detailed diff visible to a write-only caller. The deterministic source material
and structured responses are sufficient for any client-side model to explain
the product safely; richer approval/review UX belongs with item 31.

**Effort: M–L (3–7 days).** A useful first version is a versioned help corpus
plus a few read-only MCP/REST tools. Deployment-aware diagnostics, scoped
configuration inspection, and a strong authorization/evaluation matrix push
it toward L. Reuse item 25's governance APIs for all mutations rather than
building a second configuration path.

**Depends on:** Items 8 and 10 provide trustworthy caller scopes and claims;
items 21 and 22 establish consistent authorization and caller-visible
surfaces; item 25 supplies the scoped config read/validate/stage/apply
workflow. The static product guide can ship incrementally before every
dependency, but deployment-specific answers must not bypass them.

**Why it matters:** An agent can use QueryGate's query tools and still have no
reliable way to help a user install the product, configure a connection or
policy, understand a schema rejection, or discover the safe next step. Make
QueryGate an authoritative information center for its own setup and operation,
not only a database gateway: agents should be able to retrieve compact,
version-correct product guidance and, where authorized, explain the caller's
effective deployment configuration and permissions.

Generic documentation is not secret and should remain broadly available.
Permission checks protect deployment-specific state and actions: what is
configured here, which connections the caller can see, why this caller was
denied, and whether the caller may validate or change configuration. The help
system must never turn documentation access into an oracle for hidden
connections, principals, policies, schema objects, or secrets.

**What to do:**

- Create a canonical, versioned help corpus covering installation, first-run
  setup, connection and policy configuration, authentication/scopes, MCP and
  REST usage, common validation/rejection reasons, observability, upgrades,
  and troubleshooting. Generate examples from the actual config models/tool
  schemas where practical so documentation drift is testable.
- Expose narrow read-only MCP/REST capabilities such as help search, a guided
  setup checklist, capability discovery, configuration-field explanation,
  and "explain my access/error". Return structured, source-attributed answers
  with the QueryGate version they apply to; keep the deterministic retrieval
  layer useful even when no model provider is configured.
- Separate static product knowledge from live deployment context. A normal
  caller may see general guidance plus only their effective scopes, visible
  connections, and safely actionable denial details. Deployment configuration
  inspection requires the existing `admin:config:read` scope and must redact
  connection strings, credentials, secret values/references, raw exceptions,
  and hidden principal/policy entries.
- Route every validate, stage, apply, or rollback operation through item 25's
  existing `admin:config:write` governance workflow. Require an explicit user
  action, validation result, diff/preview, attribution, and audit event; the
  guide must never silently edit configuration or broaden its own access.
- Apply authorization before search, ranking, context assembly, caching, and
  error rendering—not as a final response filter. Protect against inference
  through search counts, suggested topics, missing-result differences, cache
  keys, diagnostics, and "nearby" configuration examples.
- Add role-based and adversarial tests for anonymous/dev, ordinary principal,
  operator, config reader, and config writer paths. Evaluate representative
  onboarding, configuration, rejected-query, and troubleshooting tasks for
  correctness, version freshness, useful next steps, and zero unauthorized
  disclosure.

**Definition of done:**

- An agent can answer common setup and usage questions with citations to the
  installed version's canonical guidance instead of guessing from its model
  memory.
- A caller can understand what they are allowed to use and receive a safe,
  actionable explanation for common failures without learning that hidden
  connections, schema objects, policies, or principals exist.
- An authorized administrator can inspect a redacted effective configuration,
  validate a proposed change, and enter the governed preview/apply flow; a
  read-only caller cannot mutate anything.
- Help retrieval works offline and without access to customer rows, database
  credentials, or a hosted model, and remains useful when every live database
  connection is unavailable.
- Documentation/config-schema drift and cross-principal leakage are release-
  blocking test failures.

### 34. Interactive mocked HTML product sandbox ✅ DONE

**Shipped:** `landing/sandbox.html` — a single self-contained, static HTML
page (fonts via Google Fonts CDN, everything else inline, no build step, no
network calls after load) linked from `landing/index.html`'s nav, footer,
and attack-demo section. It runs entirely in the browser: a scenario picker
with six curated, bounded (not free-text) agent questions, a request-pipeline
"theater" (prompt → mocked LLM tool call as a real `StructuredQuery` AST →
six pipeline gates → decision → result table or actionable rejection → an
illustrative compiled-SQL preview and audit line), and a live "policy
studio" panel (principal selector, allowed-tables/denied-column checkboxes,
a `max_limit` control, and a mandatory-row-filter toggle) that recomputes
every scenario's outcome immediately on change.

The six scenarios cover every category the item asked for: a raw-SQL attempt
rejected at the request contract (no field exists to hold it), a safe
aggregate join that passes, a denied-column rejection (`customers.email`),
a denied-table rejection (`employees`, with its PII-shaped columns visible
if a visitor deliberately re-enables it), a requested `limit: 5000` silently
clamped to policy's `max_limit` (and truly truncated if a visitor lowers the
studio's `max_limit` below the dataset size), and a mandatory row filter
that's AND-ed into the compiled SQL without ever appearing in the caller's
request. Switching the principal to `reporting-service` demonstrates a
per-principal policy override (`max_limit: 20` via a `"*"` connection
entry) that visibly overrides — and disables — the connection-level control,
mirroring `examples/policy.example.yaml`'s own commented example.

The policy-decision logic (table/column allow-deny, cap checks, limit
clamping) is a JS port of `validation/policy_validation.py` and
`compiler/sqlalchemy_compiler.clamp_limit`, not hand-typed per-scenario
outcomes; query *execution* runs a small generic join/filter/group/order/
limit engine over a fixed sample dataset seeded from
`examples/demo_db/init_postgres.sql`'s actual rows, so aggregate results
(e.g. per-country revenue) are genuinely computed, not hardcoded. Rejection
messages, the `{"detail": "..."}` error shape, and the `StructuredQuery`
JSON field names are copied from the real Pydantic models and
`validate_policy`'s actual `PolicyViolationError` strings, not invented.

**Drift guard:** `tests/unit/test_sandbox_fixtures.py` extracts the page's
embedded `#scenario-fixtures` JSON and replays every scenario through the
real `StructuredQuery`, `Policy`, `validate_policy`, and `clamp_limit` —
asserting the same pass/reject/clamp outcome the page claims, and asserting
the raw-SQL attempt really does fail Pydantic's `extra="forbid"` validation.
A scenario added to the page without a matching expectation in the test's
`EXPECTED` map fails the suite by design, closing the "quietly drifts into
demonstrating behavior QueryGate does not support" risk called out in this
item's own "what to do."

The mocked LLM tool-call JSON shows only a faded few-line peek by default,
with a "show full JSON"/"collapse" toggle to expand it — so a first-time
visitor sees the decision and result first and opens the full AST only if
they want the technical detail, rather than either hiding it entirely or
letting it dominate the pipeline view.

**Effort: M (2–3 days).** This is a polished, self-contained product story,
not a second frontend or a live integration. Most of the effort is in choosing
representative scenarios, making policy effects visually obvious, and keeping
the examples faithful to QueryGate's real configuration and behavior.

**Why it matters:** The technical demo proves QueryGate works, but it asks an
evaluator to understand configuration, database access, structured queries,
and policy enforcement at the same time. A browser-only sandbox should make
the core value intuitive first: an LLM attempts to query a database,
QueryGate evaluates the request against a visible policy, and the request is
either allowed, constrained, or rejected with a useful explanation.

**What to do:** Build a mocked, static HTML sandbox that can be opened without
a backend, credentials, model API, or database. Present a small sample
database and a chat-like interaction where the user can choose or enter from
a bounded set of realistic questions. Step through the resulting mock tool
call and show QueryGate's decision in context rather than only displaying a
final chat answer.

- Include several curated scenarios with realistic sample queries: a safe
  aggregate that passes, a request for a forbidden table or column that is
  rejected, a query whose row limit is clamped, and a query affected by a
  mandatory filter or principal-specific policy.
- Show the relevant, real-shaped QueryGate configuration beside the demo and
  make a few controls interactive (for example allowed tables/columns,
  `max_rows`, mandatory filters, or principal). Changing a control should
  immediately and predictably change the pass/reject decision and explanation.
- Visualize the request path compactly: user prompt → mocked LLM/tool request
  → QueryGate validation/policy checks → pass, constrained pass, or reject
  → safe database result or actionable error. Make clear which parts are
  mocked and which behavior mirrors the real product.
- Use representative configurations and response/error shapes derived from
  the current models and public interfaces; add a lightweight fixture or
  snapshot check so the sandbox does not quietly drift into demonstrating
  syntax or behavior QueryGate does not support.
- Keep the experience deterministic, fast, accessible, responsive, and easy
  to reset. It should work as a hosted landing-page embed and as a local static
  file, with no production data and no network dependency after load.
- End with a clear handoff to the real technical demo, documentation, or
  runnable quickstart so the sandbox explains the value without pretending to
  be execution proof.

**Definition of done:** A first-time visitor can use the sandbox in under two
minutes, explain why at least one request passed and another was rejected, and
see how one policy/configuration change alters the outcome. Every showcased
configuration, decision, and error is traceable to a tested QueryGate behavior,
and the page is explicitly labeled as an illustrative mocked experience.

### 35. Agent-visible capacity waiting, progress, and cancellation

**Phase 1 shipped:** `concurrency_slot()` already waited for up to the
policy's `concurrency_wait_seconds` and raised `ConcurrencyLimitError`
(REST `422`) when that window expired — proven under real load by item 15's
harness. Phase 1 makes that existing wait-then-fail-fast path agent-visible
and caller-tunable, without yet building the asynchronous/cross-replica
machinery below:

- `execution/admission.py` — `QueueMode` (`fail_fast`/`wait`) and
  `resolve_wait_seconds()`, the single clamp that lets a caller shorten the
  operator's `concurrency_wait_seconds` ceiling (or skip waiting entirely via
  `fail_fast`) but never lengthen it. `queue_mode`/`wait_timeout_seconds` are
  request-level options outside the `StructuredQuery` AST — REST query
  params on `POST .../query` and `.../query/batch`, extra MCP tool
  arguments on `execute_structured_query`/`execute_structured_queries`.
  Omitting both preserves the exact pre-item-35 default.
- Every `execute()` call gets a stable `admission_id` (UUID) and
  `queue_wait_ms`, added as optional fields on `StructuredQueryResult`/
  `BatchQueryItemResult` (REST/MCP) and surfaced as REST response headers
  (`X-QueryGate-Admission-Id`, `X-QueryGate-Admission-State`,
  `X-QueryGate-Queue-Wait-Ms`) rather than folded into the existing `422`
  `{"detail": "too many concurrent..."}` body, so that documented string
  contract (`docs/LOAD_TESTING.md`) never changes. MCP's `MCPErrorResult`
  gains the same three fields for a capacity rejection.
  `core/exceptions.CapacityTimeoutError` (subclasses `ConcurrencyLimitError`)
  carries the id/elapsed-wait without touching any existing
  `isinstance`/`except ConcurrencyLimitError` call site.
  Terminal states this phase: `completed` and `capacity_timeout` — `queued`/
  `running`/`cancelled` need the asynchronous contract below.
- New metrics: `querygate_queue_depth` (callers currently waiting for a slot,
  single-process visibility) and `querygate_queue_wait_seconds` (histogram,
  labeled by outcome). `AuditEvent` gained matching `admission_id`/
  `queue_wait_ms`/`admission_state` fields (schema_version unchanged, same as
  every prior additive field).
- Proven under real Postgres load
  (`tests/integration/test_postgres_load_guardrails.py`): `fail_fast` never
  waits even though capacity frees up moments later; a caller-selected wait
  shorter than the policy ceiling is honored; a caller-selected wait that
  outlasts the occupiers still queues and succeeds; successful responses
  carry the documented admission headers. Security regression
  (`test_caller_cannot_extend_the_operators_concurrency_wait_ceiling`) proves
  a caller cannot use `wait_timeout_seconds` to wait longer than the operator
  configured.

**Phase 2 shipped:** Redis-backed cross-replica admission state, plus the
queue-depth pressure controls phase 1 deliberately left out —
`Policy.max_queue_depth` (whole-connection) and
`max_queue_depth_per_principal` (one caller's share), both optional and
unset/unlimited by default. `execution/concurrency.py`'s `concurrency_slot()`
now takes `principal_subject`/`max_queue_depth`/`max_queue_depth_per_principal`
and enforces them *before* a caller starts waiting at all — a caller past
either cap is rejected immediately (`queue_wait_ms: 0`), never queued.
`core/exceptions.QueueDepthExceededError` (raised by `concurrency.py`, plain,
mirroring how a bare `ConcurrencyLimitError` signals a wait-timeout) is
enriched into `QueueFullError` (subclasses `CapacityTimeoutError`, adds
`admission_state="queue_full"`, distinct from `"capacity_timeout"`) at the
`StructuredQueryService` boundary — the same low-level/enriched split
`CapacityTimeoutError` already established. `CapacityTimeoutError` itself
gained an `admission_state` field (default `"capacity_timeout"`) so REST/MCP
read it off the exception instead of hardcoding the string, which is what
let `QueueFullError` reuse the exact same REST `422`-plus-headers and MCP
`MCPErrorResult` mapping with no new branch at either boundary.
`querygate_queue_wait_seconds`'s `outcome` label and
`querygate_queries_rejected_total`'s `reason` label both gained a `queue_full`
bucket, broken out from `capacity_timeout`/`concurrency` so operators can
tell "the queue's own pressure control tripped" apart from "waited and ran
out of time."

Cross-replica admission state: `execution/redis_concurrency.py`'s
`RedisConcurrencyLimiter` gained `enter_queue`/`leave_queue`, mirroring
`acquire`/`release`'s existing sorted-set-plus-lease design with a second
pair of per-connection (and, when a principal is given, per-connection-
per-principal) sorted sets. When `concurrency_backend: redis` is selected,
`max_queue_depth`/`max_queue_depth_per_principal` are enforced against the
true cross-replica count (one atomic Lua script checks both caps and admits
or rejects the waiter), and `querygate_queue_depth` is set from that same
count rather than one process's own local increments — closing the exact
gap phase 1 flagged ("single-process visibility only, like
querygate_concurrency_in_use"). The in-process (non-Redis) fallback keeps
its pre-existing single-process-only gauge semantics, now paired with a
plain-dict depth count purely for cap enforcement (Prometheus gauges have
no public "current value for these labels" read). Both paths fail open on a
Redis error the same way `acquire()` already does, for the same
availability-over-strict-enforcement reason.

Tested against fakeredis (`tests/unit/test_redis_concurrency.py`'s
`enter_queue`/`leave_queue` tests, including cross-limiter-instance
enforcement standing in for cross-replica), `tests/unit/test_concurrency.py`
(local-path caps, per-principal isolation, Redis-path cap enforcement and
gauge accuracy across two separate limiter instances sharing one Redis),
`tests/unit/test_service.py` (`QueueFullError` wiring, `admission_state`
audit field), `tests/unit/test_metrics.py` (`queue_full` classification),
REST/MCP integration tests proving the `422`/`MCPErrorResult` mapping, and
two adversarial security regressions
(`test_unbounded_waiting_queue_is_capped_not_a_dos_vector`,
`test_max_queue_depth_per_principal_prevents_one_caller_starving_another`)
proving the actual security property: a caller happy to wait indefinitely
cannot pile up an unbounded number of waiters, and one noisy principal
cannot exhaust another principal's share of the queue.

**Explicitly deferred to phase 3** (each remaining piece needs its own
protocol/design decision — a wire format for MCP progress notifications, a
new REST resource lifecycle for asynchronous execution plus its cancellation
semantics, and a breaking-change evaluation for HTTP status codes — that are
independent of phase 2's storage/admission-control work above and are each
easier to scope correctly on their own than bundled together):

- MCP progress notifications for a client that advertises support.
- A REST asynchronous contract (`202` + status/cancel endpoints, or a
  documented streaming endpoint) — a normal pending HTTP response can't
  notify a caller mid-wait.
- Idempotent mid-queue cancellation, including whether cancellation is
  queue-only or must invoke and verify dialect-specific database
  cancellation before reporting `cancelled`.
- Evaluate `429` + `Retry-After` for REST capacity responses without
  breaking clients that currently handle `422`.

**Why it matters:** Today a caller sees either a slow pending tool call, a
final result, or a final capacity error. It cannot ask to fail fast or wait for
a caller-selected period, cannot distinguish "queued behind two queries" from
"the database is slow," and cannot present a supported cancel action while it
waits. An interactive agent should be able to say that QueryGate is at
capacity, keep waiting within an operator-approved bound, and let the user
cancel rather than appearing hung or retrying blindly. Phase 1 answers the
first two; phase 2 makes the waiting itself bounded and cross-replica-safe;
phase 3 answers the rest.

### 32. Governed adaptive semantic memory for agents ✅ DONE

**32B-1 shipped — governed review, edit, approve/reject, publish, and
rollback.** This is the first independently deployable slice of 32B, built
entirely on 32A's existing `querygate/catalog/` models, `CatalogStore`, and
`CatalogFileRepository` — no second catalog file, database, or mutation
path:

- `catalog/governance.py` implements a deny-by-default proposal state
  machine (`pending` → `approved`/`rejected`, `approved` → `published`)
  with actor attribution, timestamps, and a full per-proposal
  `review_history`. Every transition is validated against the proposal's
  current state; invalid, repeated, or out-of-order transitions (approving
  twice, rejecting a published proposal, editing after approval) fail with
  a `CatalogGovernanceError` and mutate nothing. Rejection requires a
  non-empty reason. Bulk approve/bulk-reject are bounded to 50 ids per call
  and validate every id before mutating any of them — a single invalid id
  fails the whole batch atomically.
- Publishing merges an *approved* proposal's content into a real table/
  column/relationship catalog entry using 32A-1's own precedence gate
  (`replacement_decision`), plus one additional 32B-specific rule:
  publication never silently overwrites a field that already carries
  human-verified content and differs from the proposal — that is always a
  reviewable conflict, and the entire publish is rejected with the
  conflicting field names, mutating nothing. A draft's content model
  (`CatalogDraftContent`) has no `sensitivity`/`allow_samples`/policy/
  mandatory-filter field at all, so publication structurally cannot touch
  connection access, mandatory row filters, or sensitivity labels regardless
  of what a reviewer approves. A stale proposal (schema drift since
  generation, per 32A-2's refresh) cannot be approved or published.
- `governance.preview_publish` is a read-only, test-as-principal dry run:
  it resolves the target table/column/relationship's visibility under a
  given principal's policy exactly like `describe_table` does, and reports
  whether the object would even be visible and whether publishing would
  conflict — without persisting anything.
- Every publish creates a durable `CatalogVersionRecord` inside the same
  catalog store (`SchemaCatalog.version_history`, bounded to 2000 entries):
  actor, timestamp, the proposal that produced it, and a metadata-only
  change description (which table/column/relationship, which field names
  changed) with full before/after content available on request. Rollback
  reactivates a prior version — restoring changed fields to their prior
  value, or removing an entry this publish created — and is authorized,
  idempotent (a version can't be rolled back twice), and safety-checked: it
  refuses if the entry has changed since this publish (someone else
  changed it in the meantime) or if rolling back a table's *creation* would
  collaterally remove columns/relationships a *later* publish added to that
  same table. Rollback never restores or exposes secrets, row data,
  provider payloads, or query literals — the version record structurally
  cannot contain any of those, the same way a draft proposal can't.
- New REST surface, `api/catalog_governance_routes.py`, under
  `/api/v1/admin/catalog/{connection}/...`: generate-drafts (a REST
  equivalent of the CLI's manual batch import), list/get/edit/approve/
  reject/publish/preview proposals, bulk-approve/bulk-reject, and list/get/
  rollback versions. Seven new least-privilege scopes gate it —
  `catalog:generate`, `catalog:review` (read), `catalog:edit`,
  `catalog:approve`, `catalog:reject`, `catalog:publish`,
  `catalog:rollback` — none of them implied by any other, matching the
  read/write scope-separation pattern item 25's config-governance API
  already established. Agent-facing catalog retrieval (`GET
  /{connection}/catalog/search`, `describe_table`) is entirely unchanged by
  this item: proposals are never indexed or returned to agents regardless
  of review status, only a `published` entry (which is now a real, verified
  catalog entry) becomes visible, filtered by the same policy as everything
  else.
- `querygate-semantic-memory` gained matching CLI subcommands
  (`list-proposals`, `show-proposal`, `edit-proposal`, `approve-proposal`,
  `reject-proposal`, `publish-proposal`, `preview-publish`, `list-versions`,
  `show-version`, `rollback-version`), so the full workflow works without
  an admin UI over either surface.
- Every generation and state transition emits a new redaction-safe
  `catalog.governance` audit event (`audit/events.py`'s
  `CatalogGovernanceEvent`, persisted through the same sink as query and
  config-governance events) — action, connection/proposal/version ids,
  actor, scopes, outcome, duration; never draft text, descriptions,
  aliases, or raw catalog YAML.

Covered by `tests/unit/test_catalog_governance.py` (state machine, merge/
conflict detection, staleness gating, rollback safety checks, and a
full generate→approve→publish→rollback lifecycle through the real
`CatalogFileRepository` lock), `tests/unit/test_catalog_cli.py` (CLI
lifecycle + error mapping), `tests/integration/test_catalog_governance_rest.py`
(full REST lifecycle, bulk-reject atomicity, unknown connection/proposal
404s), and new adversarial tests in
`tests/security/test_adversarial_security.py` (each write scope is
independent of every other; `catalog:review` alone cannot mutate anything;
unauthenticated callers are rejected; a proposal cannot publish itself
before an explicit, separately-scoped approval).

**32B-2 shipped — export/import, backup/restore, and retention/deletion.**
Completes 32B (all nine catalog-governance scopes are now implemented) on
top of 32B-1's state machine, still through the same `CatalogFileRepository`
lock and no second catalog file:

- `export_connection`/`import_connection` (gated by one bidirectional
  `catalog:export` scope) are a single mechanism serving both data
  portability and disaster recovery: export produces a self-contained,
  connection-scoped `CatalogExportBundle` (published entries, quarantined
  proposals with full review history, generation records, version history,
  and the schema snapshot); import is a full, destructive replace of that
  connection's governed content from a bundle. Both `version_id`s and
  `generation_id`s are file-global, not per-connection, so import re-numbers/
  de-duplicates them against the target catalog's current content — an
  operator-chosen generation id like `"onboarding-1"` reused across two
  environments' exports, or two independently-numbered version histories,
  can never collide or corrupt an unrelated connection's history on import.
  Every internal cross-reference (`rolled_back_version_id`, a proposal's
  `published_version_id`/`generation_id`) is remapped consistently.
- `delete_proposal`/`bulk_delete_proposals`/`delete_version_record` (gated
  by `catalog:delete`) prune only terminal-state records: a rejected
  proposal deletes standalone; a published-then-rolled-back proposal is
  deleted together with its now-reverted publish record *and* the rollback
  record that reverted it (that rollback record's own
  `rolled_back_version_id` would otherwise dangle-reference a deleted
  record — the model's own referential-integrity validators catch this,
  which is how the cascade requirement was found); a standalone rollback
  record (never referenced by anything else) deletes on its own. A proposal
  that is still `pending`/`approved`, or `published` with its publish still
  live, can never be deleted — it must be rejected or rolled back first —
  so deletion can never destroy the only record of why current catalog
  content exists. Deleting a proposal also removes its id from its
  generation record's `proposal_ids` list to keep that reference valid.
  Bulk delete is bounded to 50 ids and atomic, matching 32B-1's other bulk
  operations.
- New REST endpoints under the same `/api/v1/admin/catalog/{connection}/...`
  prefix: `GET .../export`, `POST .../import`, `DELETE .../proposals/{id}`,
  `POST .../proposals/bulk-delete`, `DELETE .../versions/{id}`. New CLI
  subcommands: `export`, `import`, `delete-proposal`, `delete-version`.
  Every action emits a redaction-safe `catalog.governance` audit event
  (`export`/`import`/`delete_proposal`/`bulk_delete`/`delete_version`
  actions), never draft content or raw catalog YAML.
- "Migration" (the remaining word in the original 32B non-negotiable list)
  needed no new code: legacy version-1 catalogs already upgrade in memory
  to durably-provenanced version-2 content (`catalog/loader.py`'s
  `_normalize_catalog`, shipped with 32A-1), and every 32B-1/32B-2 field
  added to `CatalogDraftProposal`/`SchemaCatalog` has a Pydantic default, so
  a catalog file written before this item shipped still loads correctly.

Covered by new export/import/deletion sections in
`tests/unit/test_catalog_governance.py` (self-contained bundles, a
publish→export→import round trip into a fresh store, cross-connection
version/generation id collision avoidance, destructive-replace semantics,
and every deletion precondition/cascade), `tests/unit/test_catalog_cli.py`,
`tests/integration/test_catalog_governance_rest.py` (REST export/import
round trip — including that the exported response body can be POSTed
straight back to `/import` without a computed-field validation error — and
delete/bulk-delete), and new adversarial tests proving `catalog:export`/
`catalog:delete` are independent of each other and of every 32B-1 scope.

**32A-1 shipped — durable provenance, schema fingerprints/diffs, and
policy-first retrieval.** This is an independently deployable first slice of
32A, deliberately built by versioning/extending item 27's existing
`querygate/catalog/` YAML/store rather than adding a parallel catalog:

- Catalog format version 2 gives every table, column, and relationship entry a
  deterministic stable id, catalog/schema version, source class and redaction-
  safe evidence pointers, confidence, status (`draft`, `verified`, `rejected`,
  `stale`, or `archived`), actor/timestamps, optional model/prompt identifiers,
  and server-derived precedence. Version-1 catalogs remain valid and are
  upgraded in memory as manually verified entries with deterministic ids.
- The documented/tested replacement gate orders verified > observed >
  inferred > learned, prevents a non-verified source from replacing verified
  knowledge, excludes non-publishable states, and makes sensitivity labels
  immutable through semantic-memory merging. 32A-1 had no generated-content
  path; 32A-2 adds only quarantined proposals, never automatic publication,
  so generation still cannot change access, mandatory row filters, or
  sensitivity.
- `catalog/schema_memory.py` produces canonical row-free schema snapshots,
  SHA-256 fingerprints, and structured diffs for tables, columns/types/
  nullability/primary keys, foreign keys, and indexes. Database comments are
  stored only as hashes (not prompt-injectable raw text). Unique structural
  matches are reported as *possible* renames rather than asserted as truth;
  tampered snapshots are rejected.
- `catalog/retrieval.py`, `StructuredQueryService.search_catalog`, `GET
  /api/v1/{connection}/catalog/search`, and MCP `search_catalog` provide
  bounded deterministic lexical retrieval (1–20 results, 16 KiB). The
  requesting principal's policy is applied before tokenization, scoring,
  result counts, relationship traversal, or byte budgeting—including both
  join columns and the relationship target table. Every hit cites entry id,
  source plus hashed evidence references, status, confidence, precedence,
  catalog version, schema fingerprint, and `current`/`stale`/`untracked`
  freshness. Creator/approver/model identities stay in the privileged durable
  record rather than agent responses. Rejected/archived entries never enter
  search; stale/draft results are never presented without their state.
- Retrieval is metadata-only and independent of the database execution path:
  it stores/searches no rows, credentials, query literals, raw exceptions, or
  unrestricted natural-language history; an empty catalog or memory failure
  cannot weaken or block item-27 discovery, policy/schema validation, or
  ordinary structured query execution.

Covered by focused catalog/provenance, fingerprint/diff, REST/MCP integration,
and multi-principal adversarial tests, plus QG-17 in `docs/THREAT_MODEL.md`.

**32A-2 shipped — manual-only drafts, automatic refresh/invalidation, and a
predefined deterministic baseline.** This completes phase 32A without adding
a live model or a publication path:

- `catalog/providers.py` defines a provider protocol with only `disabled` and
  `manual` implementations. Disabled mode is the default and does not even
  parse provider input. Manual mode imports a strict structured batch tied to
  the current connection/schema fingerprint; there is no HTTP client, hosted
  provider adapter, prompt execution, credential, row, query-literal, or raw-
  comment field in the contract.
- `catalog/generation.py` stores output as separate inferred `draft_proposals`
  plus metadata-only `generation_records`. Proposal IDs and input fingerprints
  are deterministic; retrying the same generation is idempotent and reusing
  its key with different input is rejected. Draft content cannot carry policy,
  mandatory-filter, sensitivity, or sampling fields and is never indexed by
  `search_catalog`, merged into verified entries, or returned by agent-facing
  REST/MCP surfaces. Generation records retain provider/prompt/schema/actor
  provenance without retaining a prompt or provider payload.
- `catalog/refresh.py` provides a bounded row-free scanner and an opt-in
  background monitor (`SEMANTIC_MEMORY_REFRESH_ENABLED`, disabled by default).
  It atomically persists through an adjacent cross-process file lock. The
  structured diff marks removed/changed table/column targets and dependent
  relationship/draft targets stale while rebinding unaffected active entries
  to the new fingerprint, so unrelated verified context remains current.
  Same-snapshot retries are no-ops. Scan/provider failures stay off the query
  path and logs retain only their exception type, not raw driver text.
- `querygate-semantic-memory refresh|generate-drafts|evaluate` supplies the
  operator workflow. Manual generation additionally requires
  `SEMANTIC_MEMORY_PROVIDER=manual`; `disabled` remains the kill switch.
- The versioned `semantic_memory_v1.yaml` corpus covers unfamiliar table and
  relationship selection, stale guidance, and a policy-hidden adversarial
  object. Release thresholds are compiled constants, not fixture-tunable:
  expected-hit recall >= 0.85, relationship recall >= 0.80, stale detection =
  1.0, discovery-call reduction >= 0.50, and zero policy violations. The
  deterministic current report is 1.0/1.0/1.0/0.75/0 respectively and fails
  the command if a threshold regresses.

Covered by provider-contract, generation/idempotency, selective schema-drift,
atomic persistence, monitor, CLI, packaged-corpus, benchmark-regression, and
adversarial quarantine/error-redaction tests. At the time 32A-2 shipped, 32B
review/publication/history/rollback and 32C adaptive usage learning/hardening
remained explicitly out of scope; both have since shipped as described above
and below.

**32C shipped — redaction-safe usage signals, a usage-based learner, and
background-job resilience.** Completes item 32 on top of 32A's provenance
model (`KnowledgeSourceClass.LEARNED`, `CatalogPrecedence.LEARNED`, and
`CatalogEvidenceKind.USAGE` already existed unused in `catalog/models.py`
from 32A) and 32B's unmodified governance state machine — no second catalog
file, mutation path, or review workflow was added:

- `catalog/models.py`'s `CatalogUsageSignal` is a typed, frozen, redaction-
  safe observation (connection id, a hashed `principal_partition` — never
  the raw principal subject — a `CatalogDraftTarget`, a `table_used`/
  `column_used`/`relationship_used` kind validated against the target's own
  object type, a schema fingerprint, and a single bounded `usage` evidence
  pointer). `SchemaCatalog.usage_signals` is bounded (50k, FIFO-pruned) and
  excluded from `CatalogExportBundle`/import — it is pre-decision evidence,
  not governed content.
- The query execution hot path (`execution/service.py`'s `execute()`) never
  takes the catalog file's cross-process lock: a successful, already
  policy-validated query's `from_table`/joins are turned into signals and
  pushed into a bounded, per-connection-partitioned **in-process** buffer
  (`catalog/usage.py`'s `InProcessUsageSignalBuffer`), best-effort and
  exception-swallowed by construction. Emission is gated on a per-object
  **anti-feedback-loop rule** (`should_emit_signal`): a signal is only
  recorded when the target's current catalog knowledge is absent (organic
  discovery) or `verified` (human-confirmed) — never when the only reason
  an agent chose it was the system's own unreviewed `draft`/`stale` guess,
  so the learner can never "confirm" its own unpublished suggestions.
  Recording is opt-in (`SEMANTIC_MEMORY_USAGE_SIGNALS_ENABLED`, off by
  default) and requires `CATALOG_FILE`.
- `catalog/learning.py`'s `generate_learned_relationship_proposals` only
  turns `relationship_used` evidence into proposals — usage alone can
  honestly support "these tables are frequently joined this way", not a
  table/column description. Compiled, non-fixture-tunable thresholds gate
  it: minimum distinct-principal support, a minimum confidence derived from
  support, a 30-day evidence decay window, and a conflict rule (two
  competing join targets for the same source column are both skipped
  unless one leads the other by a required margin). A relationship already
  `verified`, or already covered by a pending/approved proposal, is never
  relearned or duplicated. The generation id is itself derived from the
  qualifying evidence snapshot, so replay against unchanged evidence — or a
  second concurrent worker — is always idempotent, independent of whatever
  review-state a prior run's proposal is currently in.
- Learned proposals are ordinary `CatalogDraftProposal`s
  (`source_class=learned`, lower precedence than `inferred`) persisted
  through the same `draft_proposals`/`generation_records` lists 32A-2's
  manual generation already uses (`provider_mode="usage-learner"`) — every
  32B review/edit/approve/reject/publish/rollback/export/delete code path
  applies to a learned proposal completely unchanged, including that it can
  never publish itself.
- `CatalogUsageLearningMonitor` (mirrors `CatalogRefreshMonitor`) is an
  opt-in (`SEMANTIC_MEMORY_LEARNING_ENABLED`, off by default) per-connection
  background job: each cycle, it drains that connection's buffered signals
  into one batched, lock-guarded write, then separately runs the learner —
  two independent writes, so a learner failure can never lose
  already-drained evidence; fail-open with exception-type-only logging,
  identical posture to schema refresh.
- New REST surface reuses existing least-privilege scopes rather than
  minting new ones: `POST /api/v1/admin/catalog/{connection}/learn`
  (`catalog:generate` — this is generation, just from a different evidence
  source) and `GET .../usage-signals` (`catalog:review`, a bounded
  aggregated-by-target summary, never a raw per-signal dump). New
  `querygate-semantic-memory` CLI subcommands: `learn`,
  `list-usage-signals`, and `submit-usage-signals` (the operator/test-
  harness path for seeding signals without a live server, mirroring manual
  draft import). New Prometheus counters
  (`querygate_usage_signals_buffered_total`,
  `querygate_usage_signal_buffer_dropped_total`,
  `querygate_usage_signals_recorded_total`,
  `querygate_learned_proposals_generated_total`) cover observability; a new
  `catalog.governance` audit action (`learn`) covers the generation step
  itself, matching `generate-drafts`'s existing audit posture.

Covered by `tests/unit/test_catalog_usage.py` (signal shape/kind-target
validation, idempotent recording with bounded FIFO eviction, the in-process
buffer's per-connection partitioning and overflow handling, the
anti-feedback-loop gate, and the monitor's drain-then-learn sequencing),
`tests/unit/test_catalog_learning.py` (support/confidence gating, decay,
schema-drift exclusion, the conflict rule, idempotent replay, dedup against
verified/open content, cross-connection and repeated-single-principal
isolation, and a learned proposal flowing through the unmodified
approve/publish state machine), `tests/unit/test_service.py` (real
emission on the execute() path, default-disabled, anti-feedback-loop
suppression, and that a failure while emitting signals never surfaces as a
query failure), `tests/integration/test_catalog_governance_rest.py` (the
full learn → approve → publish REST flow and scope/404 checks), new
`tests/unit/test_catalog_cli.py` cases, and new adversarial tests in
`tests/security/test_adversarial_security.py` (`catalog:generate`/
`catalog:review` remain independent for the new endpoints, unauthenticated
callers are rejected, and one connection's usage evidence and review-state
transitions never affect another's).

**Effort: XL (4–8+ weeks after item 27).** A useful prototype can be built
faster, but a production-grade version needs a durable knowledge model,
principal-safe retrieval, schema-change invalidation, provider isolation,
approval/version workflows, background processing, evaluation, and
adversarial testing. Provider integrations or a full review UI can extend
the estimate further.

**Depends on:** Item 27 provides the versioned semantic catalog this feature
learns into; item 25 provides governance, actor identity, approval, and
rollback; item 23 provides the redaction-safe activity signal and audit sink;
item 28 defines the security boundary the learning loop must preserve. Item
31 may later provide the ideal human review UX, but REST/CLI workflows must
work without it.

**Why it matters:** Schema reflection tells an agent what fields exist, not
what the business means. On an unfamiliar database the agent repeatedly lists
tables, describes candidates, guesses relationships and timestamps, and may
still choose a technically valid but semantically wrong query. QueryGate can
turn that repeated discovery into customer-owned, governed memory: what a
table represents, which joins are preferred, which definitions are verified,
what is sensitive, and what changed since the knowledge was produced.

This can become a defining product message:

> QueryGate does not only protect the database. It builds a governed,
> continuously updated understanding of the data each agent is allowed to use.

The marketable outcome is lower time-to-answer, fewer discovery calls, fewer
invalid or misleading queries, consistent business definitions, and safer
agent behavior that improves with verified usage—not an opaque claim that an
autonomous model "trained itself" on production data.

#### Product principle: evidence before inference

The catalog must keep four knowledge classes structurally separate and apply
an explicit precedence order:

1. **Observed facts** — deterministic schema evidence: table/column names,
   types, nullability, constraints, foreign keys, comments, and schema hash.
2. **Inferred drafts** — model-generated descriptions, relationship
   hypotheses, likely timestamp/identifier roles, metric suggestions, and
   confidence scores. These are never silently presented as verified truth.
3. **Verified knowledge** — administrator-approved or edited definitions,
   ownership, sensitivity, approved joins, business glossary mappings, and
   usage guidance. Verified content wins over inferred content.
4. **Learned usage suggestions** — recurring safe query shapes, joins,
   aggregations, and explicit user/admin corrections. These remain proposals
   until policy and confidence thresholds allow publication or an actor
   approves them.

The canonical store must be structured and versioned. Markdown "documents for
the agent" are generated views or export artifacts, not the source of truth;
runtime context is retrieved selectively under a token budget rather than
injecting an ever-growing document into every prompt.

#### Production-grade scope

**1. Durable knowledge model and provenance**

- Store entries for connections, tables, columns, relationships, glossary
  terms, metrics, temporal semantics, recommended joins, warnings, and usage
  guidance in a migration-managed durable store.
- Give every entry a stable id, catalog/schema version, source class, source
  evidence, confidence, status (`draft`, `verified`, `rejected`, `stale`, or
  `archived`), creator/approver identity, timestamps, and optional model and
  prompt-template version.
- Define deterministic precedence and merge rules. Regeneration must never
  overwrite a human-verified field; conflicting evidence creates a reviewable
  proposal.
- Support history, diff, rollback, export/import, retention, deletion, and
  backup/restore. Catalog migrations must be reversible and tested.

**2. Safe discovery and enrichment pipeline**

- Run onboarding and refresh as idempotent background jobs, not on the query
  request path. QueryGate must continue serving ordinary schema/query tools if
  enrichment is disabled, slow, rate-limited, or unavailable.
- Begin with reflection, constraints, foreign keys, indexes, and database
  comments. Do not inspect row values by default.
- Make optional statistics or representative samples a separate, explicit
  policy capability with sensitivity checks, hard size/cardinality limits,
  redaction, audit events, and a default of disabled.
- Treat database comments, identifiers, and any permitted samples as untrusted
  data—not instructions. Delimit them from model instructions and test prompt-
  injection attempts embedded in schema metadata.
- Fingerprint the observed schema and produce a structured diff. Added,
  removed, renamed, or type-changed objects must invalidate or mark affected
  knowledge stale without discarding unrelated verified context.

**3. Pluggable and privacy-aware model execution**

- Define a provider interface supporting disabled/manual-only operation,
  customer-configured hosted models, and local/on-prem models. QueryGate must
  not require a vendor-controlled model or send metadata outside the customer
  network by default.
- Before an outbound call, compute the minimum policy-approved metadata
  payload and record which provider, region/endpoint, model, and purpose will
  receive it. Never send connection strings, credentials, query literals,
  returned rows, or hidden schema objects.
- Require structured, schema-validated model output; cap tokens, cost,
  concurrency, retries, and wall time; make jobs resumable; and quarantine
  malformed or policy-invalid output rather than partially publishing it.
- Version prompts and provider settings, expose a kill switch, and document
  data-residency, retention, and "provider may train on inputs" requirements.

**4. Governance and human correction loop**

- Add least-privilege scopes for catalog read, generate, review, approve,
  reject, edit, publish, rollback, export, and delete operations.
- Support draft review, field-level edits, rejection reasons, bulk approval,
  ownership assignment, and test-as-principal previews before publication.
- Audit every generation job and every state transition without copying
  sensitive source material into the persisted event.
- Allow corrections from agent/application feedback only through an explicit
  typed channel. Feedback is attributed and rate-limited; repeated statements
  do not become truth merely through volume.
- The catalog can recommend narrower access or a sensitivity review, but it
  must never create, broaden, or silently modify connection/policy permissions.

**5. Bounded learning from real usage**

- Learn only from redaction-safe normalized query shapes, success/failure
  categories, explicit corrections, and verified outcomes—not query literals,
  natural-language secrets, row payloads, or raw database exceptions.
- Aggregate signals per customer/connection and preserve principal boundaries.
  Never train or create shared memory across customers.
- Require minimum support, confidence thresholds, recency/decay, and conflict
  detection before proposing a common join, metric, filter, or table purpose.
- Prevent feedback loops: generated guidance being followed by an agent is not
  independent evidence that the guidance was correct.
- Make learned suggestions explainable: show the non-sensitive evidence,
  sample size, confidence, first/last observation, and reason for promotion.

**6. Principal-safe retrieval and agent context**

- Apply connection/table/column policy before keyword search, ranking,
  embeddings, counts, relationship traversal, or context assembly. A caller
  must not infer hidden objects from search hits, empty-result differences,
  embedding neighbors, suggested joins, or catalog statistics.
- Enrich existing `list_tables`/`describe_table` responses and add narrow MCP
  and REST capabilities such as catalog search, business-term lookup,
  relationship paths, metric definitions, and "context for this task".
- Return compact, relevance-ranked context with citations to catalog entry id,
  provenance, verification status, confidence, schema version, and freshness.
  Enforce per-call result and token/byte budgets.
- Cache only after authorization or include the complete policy/principal
  scope in cache keys. Invalidate caches on policy, catalog, or schema changes.
- Never let semantic retrieval become a second query execution or raw-value
  search path.

**7. Operational lifecycle and resilience**

- Provide onboarding, refresh, pause, resume, cancel, retry, and rebuild
  commands/APIs with idempotency keys and multi-instance job locking.
- Define retry/backoff, dead-letter/quarantine behavior, partial-failure
  recovery, provider outage behavior, and safe reprocessing after upgrades.
- Surface freshness, stale/failed entries, last successful schema scan, queued
  review count, and provider health without leaking hidden object names.
- Add metrics for job duration/outcome, provider latency/errors, tokens/cost,
  entries by state, schema drift, approval time, retrieval latency, cache hit
  rate, and policy-filtered results. Keep labels low-cardinality.
- Include storage sizing, retention, backup/restore, disaster recovery, and
  upgrade/rollback instructions in the production runbook.

**8. Security, privacy, and abuse resistance**

- Extend `docs/THREAT_MODEL.md` for catalog poisoning, prompt injection,
  sensitive-schema inference, cross-principal retrieval, malicious feedback,
  provider exfiltration, embedding leakage, stale guidance, and model-supply-
  chain compromise.
- Encrypt the catalog and model credentials according to the deployment's
  storage/secrets posture; strictly partition customer and connection data.
- Define deletion and retention semantics for catalog versions, feedback,
  embeddings, provider requests, and derived suggestions.
- Rate-limit generation, feedback, search, and refresh operations. Protect
  against adversarial schemas that create excessive prompts, relationship
  graphs, or catalog entries.
- Do not embed or persist credentials, row values, query literals, free-form
  exceptions, or unrestricted natural-language history. Test this invariant at
  serialization, provider, logging, audit, cache, and retrieval boundaries.

**9. Evaluation before product claims**

- Build a versioned benchmark of representative unfamiliar schemas and
  business questions with expected table choices, joins, filters, metrics, and
  forbidden objects. Include ambiguous and adversarial cases.
- Compare baseline QueryGate discovery against semantic memory on: tool calls,
  tokens, time-to-answer, schema-validation failures, correct table/join/metric
  selection, stale-guidance detection, and policy/security violations.
- Define release thresholds before implementation results are known. The
  feature must demonstrate a meaningful improvement without increasing hidden-
  schema disclosure or unsafe-query rate.
- Run deterministic unit tests, store/migration tests, provider contract tests,
  REST/MCP integration tests, multi-principal adversarial tests, schema-drift
  tests, background-job failure tests, and realistic load/soak tests.
- Keep a human-reviewed evaluation report per release; do not market the
  system as "self-learning" solely because it generated descriptions.

#### Phased delivery

1. **32A-1 — Provenance, schema drift primitives, and safe retrieval ✅ DONE:**
   durable version-2 provenance on item 27's catalog, row-free schema
   fingerprints/diffs, and compact policy-first REST/MCP retrieval. See the
   shipped note above.
2. **32A-2 — Manual generation and deterministic baseline ✅ DONE:** disabled/
   manual-only provider contract, quarantined generated drafts, bounded opt-in
   schema refresh with selective stale marking, and a fixed-threshold versioned
   benchmark. No hosted/live provider integration or publication path.
3. **32B-1 — Governed review, edit, approve/reject, publish, and rollback
   ✅ DONE:** deny-by-default proposal state machine, precedence-gated
   publish with reviewable-conflict detection, durable version history +
   authorized rollback, REST + CLI surfaces, seven least-privilege scopes,
   and redaction-safe audit events. See the shipped note above.
4. **32B-2 — Catalog-governance lifecycle ✅ DONE:** export/import
   (backup/restore, one bidirectional `catalog:export` scope with
   file-global id remapping) and retention/deletion (`catalog:delete`,
   cascade-safe against every referential-integrity rule the state machine
   established). See the shipped note above. All nine 32B scopes now exist.
5. **32C — Adaptive learning and hardening ✅ DONE:** redaction-safe usage
   signals, confidence/decay/conflict-gated relationship proposals,
   background-job resilience, metrics and audit observability, and
   adversarial coverage. See the shipped note above; item 37 provides the
   deterministic end-to-end proof of the complete learning lifecycle.

Each phase must be independently deployable and fail safely. Generated or
learned content remains opt-in until its governed publish path (32B) has
authorized it.

#### Definition of done

- Observed, inferred, verified, and learned fields cannot overwrite one
  another outside documented precedence and approval rules.
- Every answer contains provenance, state, freshness/schema version, and
  confidence where applicable; stale content is never presented as current.
- All catalog surfaces and internal searches produce exactly the view allowed
  by the requesting principal, including relationship and aggregate metadata.
- No default pipeline sends or stores row values, credentials, query literals,
  hidden schema, raw exceptions, or unrestricted natural-language history.
- Provider/model failure cannot block or weaken ordinary QueryGate policy,
  schema validation, query execution, or deterministic discovery.
- No learned signal can expand access, weaken a mandatory filter, change a
  sensitivity label, or publish itself as verified truth.
- Schema changes reliably invalidate only affected entries, and refresh is
  idempotent across retries and multiple QueryGate instances.
- Review, publication, rollback, export, deletion, retention, and disaster-
  recovery workflows are implemented, authorized, audited, and documented.
- Benchmarks show a predefined material improvement in agent correctness and
  discovery efficiency with zero regression in adversarial policy-isolation
  tests.
- A clean deployment can disable all model calls and still use the manually
  curated item-27 catalog; an air-gapped deployment can use a local provider.

#### Explicit non-goals

- Training a shared/global model on customer schemas or behavior.
- Autonomously granting access or editing QueryGate policy.
- Building a generic data warehouse, lineage platform, or row-value search
  engine.
- Treating generated descriptions, frequent usage, or database comments as
  automatically correct.
- Sending production metadata to a QueryGate-operated cloud service unless a
  separately designed hosted product, customer contract, and privacy boundary
  explicitly introduce that capability.

### 36. Extensive production-grade QA project / edge-case test suite

**Phase 1 (policy-cap boundary tests + property-based compiler fuzzing) ✅
DONE.** **Phase 2 (cross-dialect differential tests + REST/MCP
malformed-input fuzzing) not started — split out below because it needs a
live/mocked second-dialect comparison harness and a JSON-boundary fuzzing
setup, not just more Hypothesis strategies on the existing compiler tests.**

**Phase 1 shipped:** `tests/unit/test_policy_boundaries.py` proves the
sharper boundary claim `tests/unit/test_policy_validation.py` didn't: for
every cap `validate_policy` enforces (`max_joins`, `max_select_columns`,
`max_group_by`, `max_where_depth`, `top_n.n`/`max_top_n`,
`top_n.partition_by`/`max_partition_by`, `max_batch_size`), a query at
exactly the configured limit passes and one unit past it is rejected — not
just "some over-cap value fails." `max_top_n` and `max_partition_by` had no
coverage at all before this file.

`tests/unit/test_compiler_properties.py` adds Hypothesis property-based
fuzzing of `compiler/sqlalchemy_compiler.py`: generated strategies produce
many random-but-valid `StructuredQuery` combinations across three shapes
(plain row-select with optional join/where/order_by, aggregate
GROUP BY/HAVING, and `top_n` per-partition ranking over either shape) and
assert the compiler never raises, always renders to valid SQL text (both the
normal bind-parameterized form and the `literal_binds=True` form the
audit/explain path uses), and always returns `limit >= 1`. A fourth property
test proves a `MandatoryRowFilter` on the `from_table` survives every random
shape — the multi-tenant isolation guarantee must never silently drop out
for an AST combination hand-written tests didn't happen to construct. Added
`hypothesis` as a dev dependency (`pyproject.toml`/`poetry.lock`).

**Explicitly out of scope for this pass, tracked as phase 2:**
cross-dialect differential tests (same AST compiled against Postgres and
MSSQL, asserting equivalent semantics where the AST doesn't invoke
dialect-specific behavior) and malformed-input fuzzing at the REST/MCP JSON
boundary (wrong types, extra fields, deeply nested `where`, huge string
literals — proving schema validation rejects cleanly rather than 500ing).

**Effort: L (3–5 days) for the full item; phase 1 above was closer to a
focused 1-day slice.** Not a new subsystem, but a wide sweep across the
whole request pipeline: it touches `tests/unit/`, `tests/integration/`, and
`tests/security/` all at once, plus potentially a new `tests/property/` or
`tests/fuzz/` directory. Sizing is closer to item 15/28 (dedicated test
tranches) than to a single-module fix — the work is breadth, not depth in
any one file.

**Why it matters:** Existing suites are strong but each targets one concern
— `tests/security/test_adversarial_security.py` (item 28) covers
authz/policy-bypass attack shapes, `tests/integration/test_postgres_load_guardrails.py`
(item 15) covers concurrency/timeout under load, and the per-module unit
suites cover correctness of one component at a time. Nothing currently
sweeps the `StructuredQuery` AST's own input space systematically — deeply
nested boolean `where` trees at/past `Policy.max_where_depth`, every
`join`/`group_by`/`top_n` combination at its cap boundary, Unicode/NULL/
empty-string/extreme-numeric literal values, empty result sets, single-row
vs. maximum-row responses, and malformed-but-schema-valid AST shapes that
existing tests haven't happened to construct. A gateway whose entire safety
argument rests on "callers can only submit a validated AST" needs the
validator itself proven against the full shape of that AST, not just the
shapes today's tests happened to write.

**What to do:** Audit `tests/unit/`, `tests/integration/`, and
`tests/security/` for gaps against the full `StructuredQuery`/`Policy` model
(`compiler/ast.py`, `policy/models.py`) rather than assuming coverage
percentage implies scenario coverage — a query that never exercises a cap
boundary can still hit a line of code. Concretely: boundary values for every
`Policy` cap (max joins/select/where-depth/group-by/top_n, batch size,
response bytes) both just-under and just-over; property-based testing
(e.g. `hypothesis`) generating random valid `StructuredQuery` ASTs to catch
compiler crashes or SQL-generation bugs that hand-written cases miss;
cross-dialect differential tests (same AST against Postgres and MSSQL,
asserting equivalent results where the AST doesn't invoke dialect-specific
behavior); and malformed-input fuzzing at the REST/MCP JSON boundary (wrong
types, extra fields, deeply nested `where`, huge string literals) to prove
schema validation rejects cleanly rather than 500ing. Track coverage
gaps explicitly rather than chasing a single aggregate `--cov` number, since
line coverage alone doesn't prove edge cases were exercised.

### 37. Automated end-to-end proof of adaptive semantic learning

**Shipped.** `querygate/catalog/adaptive_learning_benchmark.py` plus the
packaged fixture `querygate/catalog/benchmark_data/adaptive_learning_v1.yaml`
drive the real persisted components — `CatalogStore`/`CatalogFileRepository`,
`catalog.usage.record_usage_signals`/`should_emit_signal`,
`catalog.learning.generate_learned_relationship_proposals`,
`catalog.governance`'s unmodified review/publish/rollback state machine,
`catalog.retrieval.search_catalog`, `catalog.refresh.refresh_catalog_schema`,
`validation.policy_validation.validate_policy`, and a real (mocked-session)
`StructuredQueryService` — through the complete lifecycle, exactly matching
the six-step "what to build" list below:

1. The fixture's initial catalog has no relationship hint for
   `orders.customer_id -> customers.id` at all; the task query
   deterministically fails before learning (`baseline_correct=False`),
   proving the fixture was not pre-seeded with the answer.
2. Usage evidence uses fixed evidence-reference ids and a single fake-clock
   timestamp (`_FAKE_CLOCK`, never `datetime.now()`), and covers every
   required control: below-threshold support, a conflicting pair (two
   competing targets for the same source column, tied under the conflict
   margin), repeated-single-principal (20 signals, one principal, must not
   inflate support), cross-connection (the same relationship recorded under
   a second connection id), a denied-object query (proven to raise
   `PolicyViolationError` before any execution — so no signal for it can
   ever exist), and an unreviewed-guidance target (proven via the real
   `should_emit_signal` gate returning `False`).
3. The real learner produces exactly one `learned` proposal for the
   expected relationship and none for any control; a direct replay against
   byte-identical evidence resolves to `"idempotent"` through the
   deterministic generation-id path specifically (not merely avoiding a
   visible duplicate via the separate open-proposal dedup guard); two
   independent `CatalogFileRepository` handles racing the same file
   ("two-worker" execution) still serialize to exactly one proposal.
4. The pending proposal is proven not agent-visible
   (`search_catalog` still fails the task). A disposable copy of the store
   is rejected by a named reviewer actor and proven to leave behavior
   unchanged; the main store is then approved and published by a
   *different* actor than the learner, and the published entry's
   provenance/version record is checked for actor attribution rather than
   self-publication.
5. After publication, a fresh reload (`CatalogStore.from_file`, not the
   in-memory object) finds the relationship with `freshness=current`, and
   discovery-call reduction (`1 - 1/baseline_discovery_calls`) is checked
   against a compiled, non-fixture-tunable threshold. Principal-safe
   filtering is proven on the same published content: visible under the
   default policy, hidden under a policy denying the relationship's target
   table.
6. A real schema change (dropping the referenced column) is run through
   `refresh_catalog_schema` and proven to stale only the affected
   relationship while a separate manually-verified, unrelated entry stays
   `verified`. A real rollback reverts the publish and the task
   demonstrably regresses to the baseline again. A genuine learner failure
   (a store missing its schema snapshot) is proven not to block a real
   `StructuredQueryService.execute()` call. Every post-rollback assertion is
   repeated against one final fresh reload, not just in-memory state.

Every check was adversarially verified during development by deliberately
breaking one real invariant at a time (the anti-feedback-loop gate, the
conflict margin, the support threshold, rollback, policy enforcement,
cross-connection isolation, and query execution itself) and confirming the
report's `security_violations`/`rollback_correct`/
`ordinary_query_unaffected_by_learner_failure` fields actually flip — this
is not a checker that always reports success. Covered by
`tests/integration/test_adaptive_learning_benchmark.py`, including seven
tests that each break one real invariant and assert the checker notices.

**Acceptance gate — met:** `make adaptive-learning-test` (wrapping
`poetry run python -m querygate.catalog_cli adaptive-learning-test`) is a
single deterministic command, wired into `make release-check` right after
the 32A `evaluate` benchmark. It runs with no live model, external network,
wall-clock sleep (every timestamp is the fixed `_FAKE_CLOCK` constant), or
production row access, and exits non-zero on failure. Thresholds
(`MIN_DISCOVERY_CALL_REDUCTION`, `MAX_DUPLICATE_PROPOSALS`,
`MAX_SECURITY_VIOLATIONS`) are compiled constants in
`adaptive_learning_benchmark.py`, not fixture fields.

**Original scope (for reference — see above for what actually shipped):**

**Why it matters:** The shipped `semantic_memory_v1.yaml` benchmark is a
deterministic test of retrieval from a static catalog. It does not feed usage
signals, produce a learned proposal, exercise human approval/publication, or
prove that a later request benefits from earlier safe usage. It therefore must
not be cited as an automated test of "self learning." A real test needs to
prove the state transition and the behavioral improvement while also proving
that the learning path cannot become an authorization or data-exfiltration
path.

**What to build:** Add a versioned, network-free scenario fixture (for example
`benchmarks/adaptive_learning_v1.yaml`) and an integration test/runner that
drives the real persisted components through the complete lifecycle:

1. Start from a catalog that demonstrably lacks the expected business term or
   preferred relationship, then run the unfamiliar task with learning disabled
   and record the deterministic baseline result and discovery-call count.
2. Submit only typed, redaction-safe normalized usage events with fixed ids and
   a fake clock. Include enough independent successful evidence to cross the
   predeclared support/confidence threshold, plus below-threshold, conflicting,
   repeated-generated-guidance, denied-object, cross-principal, and
   cross-connection controls that must not contribute.
3. Run the real learner and assert that it creates exactly one `learned` draft
   with bounded evidence summaries, support/confidence, provenance, and no row
   values, credentials, query literals, natural-language history, raw errors,
   or policy-hidden identifiers. Replay and two-worker execution must be
   idempotent and must not double-count evidence or duplicate the proposal.
4. Prove the proposal is not agent-visible and cannot alter access, mandatory
   filters, sensitivity, or query execution before review. Exercise item 32B's
   authorized review/publish path as a separate test actor; rejection must
   leave behavior unchanged, while approval must retain provenance and an
   auditable transition rather than allowing the learner to publish itself.
5. Repeat the original task after approved publication and require the correct
   table/relationship outcome with a predeclared material reduction in
   discovery calls versus the baseline. Citations must identify the governed
   catalog version and freshness. A control run with learning disabled or
   insufficient support must show no improvement, proving the fixture was not
   simply pre-seeded with the answer.
6. Change the relevant schema and policy and assert selective staleness,
   principal-safe filtering, rollback behavior, and unchanged ordinary query
   availability when the learner fails. Restart/reload the persisted state and
   repeat the assertions so the test is not only an in-memory happy path.

**Acceptance gate:** expose one deterministic command such as
`make adaptive-learning-test`, run it from `make release-check` once 32C is
shipped, and keep its thresholds in source rather than fixture-tunable. It must
run without a live model, external network, wall-clock sleeps, or production
row access; fail on any learned-content auto-publication or policy disclosure;
and report baseline-versus-learned correctness, discovery-call reduction,
stale detection, duplicate proposals, and security violations. Do not make a
"self-learning" product claim until this test and the adversarial suite pass.

### 38. Admin UI catalog-governance workspace (phase 1 ✅; phase 2 not started)

**Shipped (phase 1):** A new "Catalog review" section in the existing admin
UI (`admin_ui/index.html`/`app.js`/`app.css`), calling only item 32B's
existing REST routes — no new mutation path. Covers the core
review-→-approve/reject-→-publish-→-rollback loop the item's own "why it
matters" identifies as the actual gap:

- Connection, status, source (`inferred`/`learned`), and object-type
  (table/column/relationship) filters over a bounded proposal queue.
- A detail panel with side-by-side proposed-versus-currently-published
  fields — the published side is read live via the same policy-filtered
  `describe_table` call the schema-review tab already uses (table
  `catalog`, per-column `catalog`, and matched-by-identity relationship
  entries), not a new comparison endpoint.
- Provenance/confidence/schema-freshness chips, sourced from three new
  fields (`source_class`, `confidence`, `created_at`) added to the existing
  `ProposalListItem` REST response — the only backend change this phase
  needed.
- Edit/approve/reject(reason)/publish actions, each independently gated on
  its own least-privilege scope (`catalog:edit`/`approve`/`reject`/
  `publish`) and only shown when the proposal's `review_status` makes that
  action legal, mirroring `catalog/governance.py`'s state machine exactly
  (edit/approve only from `pending`, reject from `pending` or `approved`,
  publish only from `approved`) rather than showing a button the backend
  would 409 on.
- A publish-conflict preview (`GET .../proposals/{id}/preview`) surfaced
  before publish, and a typed-confirmation dialog (reusing the same
  `confirmAction()` pattern as item 31's version activate/rollback) for the
  agent-visible mutations: publish and catalog-version rollback.
- Connection-scoped catalog version history with rollback, reusing the same
  table/history UI pattern as item 25's config version history.

Verified end-to-end against a real running server and real Postgres (not
just the ASGI test client): generated table/column/relationship proposals,
edited one, approved/published/rolled-back one, rejected another, and
confirmed the "currently published" comparison panel's logic against
`describe_table`'s actual response shape for all three target kinds. No
browser-automation tool was available in this environment, so this was
exercised as the exact sequence of REST calls the JS makes rather than
pixel-verified in a rendered page; the JS itself was syntax-checked
(`node --check`) and its static markup/script content is asserted in
`tests/integration/test_admin_ui.py`.

**Not done in this pass — explicit phase 2, not silently dropped:** bulk
approve/reject/delete UI, export/import UI (backup/restore), triggering
`generate-drafts`/`learn` from the browser (still CLI/REST-only), a
per-proposal `review_history` detail view, and usage-signal browsing. None
of these are required for the core review/publish/rollback loop; each is a
real but separable value-add matching this item's own "bulk operations"
callout, deferred rather than rushed into the same slice as the core
workflow.

**Two real bugs found and fixed as follow-ups, not silently left broken:**

1. **UI panel closing on approve/reject/publish.** `selectProposal()` looked
   the open proposal up in the currently *filtered* proposal list. Approving
   a proposal moves it out of the default "pending" filter, so the
   post-action refresh (which reloads that filtered list) could no longer
   find it — the panel silently reset to its empty state and the
   newly-available Publish button never appeared, breaking the very
   review-→-approve-→-publish flow this workspace exists for. Fixed by
   decoupling the open detail panel from the filtered queue: it now renders
   from a proposal fetched directly (`GET .../proposals/{id}`) after every
   mutation, regardless of whether that proposal still matches the active
   filter. Verified with a headless jsdom harness driving the real served
   `admin_ui` against a live server (still no browser tool available in this
   environment) through the full approve → preview → publish sequence.
2. **`list_live_tables()` leaked Postgres system catalog tables**
   (`schema/reflection.py`) — found via the background schema-refresh
   scanner (`SEMANTIC_MEMORY_REFRESH_ENABLED=true`,
   `catalog/refresh.py`'s `scan_connection_schema`) raising `NoSuchTableError`
   against a real live demo Postgres for tables confirmed to exist via
   `psql`. Root cause: Postgres's own `information_schema.tables` lists
   `pg_catalog`/`information_schema` system tables (`pg_type`,
   `pg_aggregate`, ...) alongside real ones when queried without a schema
   filter, unlike MSSQL's INFORMATION_SCHEMA. This is shared, foundational
   code — the same bug also leaked system table names into agent-facing
   `list_tables()`/MCP discovery for any Postgres connection without a
   `known_tables` seed, not just the catalog scanner. Fixed with a
   `TABLE_SCHEMA NOT IN ('pg_catalog', 'information_schema', 'sys')` filter;
   regression-tested against real Postgres in
   `tests/integration/test_postgres_schema_discovery.py`
   (`make test-postgres-live`).

**Original scope (for reference — see above for what actually shipped):**

**Effort: L (3–5 days).** The governed backend, scopes, proposal state
machine, version history, and REST routes already exist from item 32B, so
this is primarily a substantial UI workflow rather than a new persistence
subsystem. The effort is in presenting conflicts, provenance, and state
transitions accurately and covering every authorization boundary.

**Why it matters:** Item 31's UI can edit the published catalog YAML as part
of a config snapshot, but it does not expose item 32B's safer proposal-based
workflow. An administrator currently has to use REST or
`querygate-semantic-memory` to review generated/learned drafts, compare them
with verified content, approve or reject them, publish them, and roll them
back. That leaves one of QueryGate's most differentiated governance features
outside its primary human interface.

**What to do:** Add a catalog workspace with connection/status/source filters,
a bounded proposal queue, side-by-side proposed-versus-published fields,
provenance and schema-freshness indicators, edit/reject/approve actions,
publish conflict explanations, bulk operations, and catalog version rollback.
Call only the existing item-32B routes and honor their least-privilege scopes;
the UI must never collapse review, approval, and publication into an automatic
transition or reveal proposal content to callers with only agent-facing
catalog access.

### 39. Draft-aware policy simulation before staging

**Shipped.** `POST /api/v1/admin/config/simulate`
(`api/admin_config_routes.py`) accepts optional `connections_yaml`/
`policy_yaml`/`catalog_yaml` overrides (each unset field inherits from the
active config-governance version, or straight from the deployment files
before governance has ever been bootstrapped) plus a target `principal`,
scalar `claims`, `connection`, `table`, `columns`, and an optional structured
`query`. The target principal is deliberately independent of the calling
admin's own identity.

- **Endpoint and evaluation scope:** `admin.service.simulate_candidate_policy`
  writes the resolved candidate documents to a temporary directory and loads
  them through `cli.load_config_context` — the same loaders and
  cross-file/schema-shape validation `/admin/config/validate` and the CLI use
  — into a fresh, request-local `ConnectionRegistry`/`PolicyStore`/
  `CatalogStore` (`connections/visibility.py`'s new
  `resolve_visible_connection_from` and `schema_validation.py`'s new
  `resolve_query_table_connections(..., connection_resolver=...)` seam let the
  exact production visibility/join-group/policy code run against those
  isolated stores instead of the process-global ones). Nothing is installed
  as a singleton and nothing is written to the config-version store, so the
  live registry/policy/catalog, concurrent production requests, and any other
  in-flight simulation are provably unaffected — proven under real concurrent
  load in `test_candidate_simulation_uses_draft_without_persisting_or_changing_live_policy`
  (12 interleaved simulate + active-policy-test calls via `asyncio.gather`).
- **Authorization boundary:** `simulate` is the only `/admin/config/*` action
  gated on *both* `admin:config:read` and `admin:config:write` together —
  every other action needs only one. It echoes back semantic policy detail
  like a read endpoint but also resolves caller-supplied config/secret
  references like a write endpoint, so a read-only principal can't turn it
  into a secret-existence oracle.
- **Redactions:** the response model (`CandidatePolicySimulation`) structurally
  excludes resolved secret values, static mandatory-filter values, supplied
  claim values, query predicate values, and compiled SQL — it returns only a
  typed `allow`/`deny` decision, per-column allow/deny, effective guardrails,
  mandatory-filter claim *readiness* (never the filter's static value), and
  typed reason codes. A table the candidate policy hides from the target
  principal never contributes its mandatory-filter identifiers to the
  response, even when the caller explicitly names that table
  (`test_candidate_simulation_does_not_reveal_filters_for_denied_table`). An
  invalid candidate fails closed with a generic message pointing at
  `/validate` for detail rather than echoing the offending content
  (`test_candidate_simulation_masks_invalid_candidate_content`), and is still
  recorded as a redaction-safe `simulate` audit event
  (`audit/events.py`/`audit/logger.py` gained `"simulate"` alongside
  validate/preview/stage/apply/rollback).
- **UI behavior:** the admin UI's "Test as principal" panel
  (`admin_ui/app.js`/`index.html`) now posts to `/admin/config/simulate` with
  the current in-browser draft when the session holds both config scopes,
  falling back to the existing active-policy `/admin/ui/policy/test` endpoint
  for read-only sessions — labeled accordingly ("Simulate draft policy
  access" / "Nothing persisted" vs. the active-policy case) so an admin never
  mistakes a draft-context decision for the currently enforced one.
- **Threat-model control:** documented as QG-19 in `docs/THREAT_MODEL.md`,
  covering the oracle risk, the isolation guarantee, the redaction surface,
  and the fail-closed invalid-candidate behavior.

Covered by `tests/unit/test_admin_service.py` (isolated-context redaction,
denied-table mandatory-filter suppression, invalid-candidate masking, query
allow/deny plus missing mandatory-claim denial) and
`tests/integration/test_admin_config_governance.py` (concurrent draft vs.
active-policy isolation over real HTTP) and
`tests/security/test_adversarial_security.py` (scope enforcement).

**Original scope (for reference — see above for what actually shipped):**

**Effort: M–L (2–5 days).** The current active-policy simulator is small, but
evaluating an uncommitted candidate safely needs an isolated candidate
registry/policy/catalog context. It must reuse the real loaders and validation
logic without swapping process-global runtime state or opening a second,
behaviorally different policy engine.

**Why it matters:** Item 31's “test as principal” deliberately evaluates only
the active policy. That proves current behavior, but it cannot answer the most
important pre-change question: “Will this draft allow or deny the intended
principal after activation?” Requiring an administrator to activate first and
test afterward weakens the value of dry-run governance.

**What to do:** Extend config preview with a read-only candidate simulation
endpoint that accepts the draft documents plus a target principal, scalar
claims, connection, table, columns, and optionally a structured-query shape.
Load and cross-validate the candidate in an isolated context, then run the same
policy/visibility checks production execution uses. Return a typed allow/deny
decision, effective guardrails, mandatory-filter claim readiness, and safe
reasons—never resolved secret values, static row-filter values, compiled SQL
literals, or hidden identifiers. Prove simulation persists nothing and cannot
alter live request behavior even under concurrent use.

### 40. Semantic access diff for config changes

**Phase 1 shipped (connection-baseline layer); phase 2 (per-principal
resolution) not started.**

`POST /api/v1/admin/config/diff` (`api/admin_config_routes.py` →
`admin.service.diff_candidate_access` → `admin/access_diff.py`) returns a
server-derived, authorization-aware diff of *resolved* access — not a line
diff of YAML — between the active config-governance version (or the deployment
files before governance has been bootstrapped) and a caller-supplied candidate
(unset documents inherit from active, exactly like `/validate` and `/versions`).

- **Evaluation scope (phase 1):** `evaluation_scope="connection_baseline"`.
  Both snapshots are loaded through the same isolated candidate-context path
  item 39's `/simulate` uses (`_load_isolated_candidate_context`), so the live
  registry/policy/catalog singletons and any concurrent request are provably
  untouched. For every connection present in either snapshot, the default and
  per-connection policy layers are resolved with **no principal applied** and
  compared. Reported `SemanticAccessChange` items cover connection visibility,
  every guardrail cap, table access, column access, mandatory-filter
  requirements, and join groups, each classified `tightening`/`loosening`/
  `neutral` (guardrail direction is derived from a single permissiveness
  comparator, with an unset optional cap treated as "unlimited"). A
  `SemanticDiffSummary` counts each direction; changes are ordered loosening-
  first so a truncated list keeps the highest-risk entries.
- **Authorization:** like `/simulate`, `/diff` requires **both**
  `admin:config:read` and `admin:config:write` — it echoes resolved policy
  detail (read-like) while resolving caller-supplied config/secret references
  (write-like), so neither scope alone can turn it into a secret-existence
  oracle.
- **Redaction:** `before`/`after` only ever carry non-sensitive resolved
  values (a guardrail number, `visible`/`hidden`/`absent`, a join-group name,
  or a mandatory-filter *source kind* and claim *name*). Static
  mandatory-filter values, resolved secrets, connection strings, query
  predicate values, and raw YAML are structurally never placed in the output.
- **Honest incompleteness:** `analysis_incomplete` is set with a
  human-readable reason rather than silently under-reporting when the
  per-principal override layer itself changed (phase 2), when an allow-list
  toggled between restricted and unrestricted (objects the policy never names
  may also be affected and can't be enumerated without live schema
  reflection), or when the change list was truncated at its cap.
- **Threat-model control:** documented as QG-20 in `docs/THREAT_MODEL.md`.

Covered by `tests/unit/test_config_semantic_diff.py` (the pure classification
engine), `tests/unit/test_admin_service.py` (isolation, audit, safe
invalid-candidate masking), `tests/integration/test_admin_config_governance.py`
(the REST surface, redaction, and non-persistence), and
`tests/security/test_adversarial_security.py` (both-scope enforcement).

**Phase 2 (not started) — per-principal resolution.** Phase 1 resolves the
default and per-connection layers only; a change that lives purely in a
`principals:` override is detected and flagged as incomplete but not itemized.
Phase 2 resolves each explicitly *configured* principal (bounded by the policy
file, not by runtime traffic) so the diff can state "reporting-agent gains
`orders.total`", applying the same per-principal denied-table redaction
`/simulate` already uses. That per-principal fan-out is also the input item 41
(policy-change blast-radius) aggregates, ranks, and paginates — so phase 2 is
split out both because it is a distinct, independently useful slice and because
it is the natural foundation item 41 builds on.

**Original scope (for reference — see above for what shipped in phase 1):**

**Effort: L (3–5 days).** A trustworthy diff must compare resolved behavior,
not YAML syntax. It needs a typed diff model, policy resolution across default,
connection, and principal layers, bounded output/redaction rules, REST wiring,
and cross-checks proving its decisions match the enforcement path.

**Why it matters:** A line diff can show that `allowed_tables` changed, but not
whether the change grants access after inherited defaults, connection
overrides, principal overrides, and deny-wins rules are resolved. Reviewers
need statements such as “reporting-agent gains `orders.total`” or “the default
row limit rises from 100 to 500,” not an expectation that they mentally execute
the merge algorithm from YAML.

**What to do:** Build a server-derived, authorization-aware semantic diff
between the active and candidate snapshots. Report typed additions/removals for
connection visibility, tables, columns, mandatory-filter requirements, join
groups, and every guardrail; classify each as tightening, loosening, or neutral.
Keep raw values out of filter diffs, distinguish explicit rules from inherited
effects, cap result size, and provide stable machine-readable output for both
the UI and CI/CD review tooling.

### 41. Policy-change blast-radius analysis

**Phase 1 (bounded, synchronous aggregation) ✅ DONE.** **Phase 2
(asynchronous/paginated evaluation for deployments with enough configured
principals to exceed phase 1's bound) not started — split out below because
it needs a different execution shape (background job plus polling or a
paginated response), not just a larger cap.**

**Phase 1 shipped:** `POST /api/v1/admin/config/blast-radius`
(`api/admin_config_routes.py`) reuses item 40's semantic diff
(`admin/access_diff.compute_access_diff`) rather than a parallel resolution
path — that function gained an optional `principal` argument so the exact
same per-connection classification logic (guardrails, table/column access,
mandatory filters, join group, connection visibility) can be evaluated once
at the connection baseline (unchanged behavior, `principal=None`) and again
for one specific caller. `admin/blast_radius.py`'s
`compute_blast_radius_report()` calls it once for the baseline, then once
more for every principal with an explicit `principals:` entry in either the
active or candidate policy — a principal *without* an override is identical
to the baseline by construction, so it is never separately evaluated or
listed, closing the loop item 40's own diff left open
(`analysis_incomplete` when "per-principal impact is resolved in a later
phase").

Findings are ranked, not just listed: `highest_risk` includes only
access-*expanding* (loosening) changes — a removed mandatory row filter
ranked above a newly visible connection/table/column, ranked above a
loosened guardrail cap — with each entry tagged `scope: "baseline"` (affects
every principal without an override; fleet-wide) or `scope: "principal"`
(affects only that named caller; targeted), so a reviewer can immediately
tell a small YAML edit with a fleet-wide blast radius from a large edit that
only touches one agent's override — exactly the scenario this item's own
"why it matters" describes. Tightening/neutral changes are never hidden;
they remain in full in `baseline.changes` and each principal's own
`principal_impacts[].changes`, just excluded from the risk-priority view.

Work is bounded on every axis, each with its own cap and an honest
`analysis_incomplete` state (with a specific human-readable reason) rather
than silent under-reporting when a cap is hit: at most 100 configured
principals are individually evaluated (`principals_evaluated` vs.
`principals_configured` in the response); each principal's own change list
is capped like item 40's diff already was; and `highest_risk` itself is
capped at 25 entries. `compute_blast_radius` (`admin/service.py`) shares
`diff_candidate_access`'s isolated-context loading, redaction posture, scope
requirement (`admin:config:read` **and** `admin:config:write` together, for
the same read-detail-plus-write-resolution reasoning as `diff`/`simulate`),
and audit trail (`config.governance` event, new `"blast_radius"` action) —
never a second candidate-loading or audit path.

Covered by `tests/unit/test_blast_radius.py` (pure aggregation/ranking logic:
fleet-wide vs. targeted scoping, a principal shielded from a base-policy
change by its own override, mandatory-filter-removal ranked above
table/column access ranked above guardrails, tightening changes excluded
from `highest_risk`, both bounds triggering `analysis_incomplete`),
`tests/unit/test_admin_service.py` (isolation from live singletons and the
governance store, audit events, invalid-candidate masking), a REST
integration test proving a targeted per-principal expansion surfaces as the
top `highest_risk` finding even while the connection baseline itself
tightens, and adversarial security tests (both config scopes independently
required, matching `/diff`; a static mandatory-filter value never appears
anywhere in the aggregated response, including inside a per-principal
impact entry). See `docs/THREAT_MODEL.md`'s new QG-22 entry.

**Explicitly out of scope for this pass** (matches this item's own "high end
applies when... asynchronous or paginated analysis" framing): no
async/background evaluation and no paginated response — a deployment with
more than 100 configured principals gets `analysis_incomplete` with a count
of how many were skipped, not a way to page through the rest. No admin UI
panel either, matching item 40 phase 1's own scope (the admin UI's existing
"Change preview" panel is a client-side line diff, not wired to either
semantic endpoint).

**Why it matters:** A syntactically tiny default-policy change can affect every
principal and connection, while a large YAML edit may affect only one agent.
Without an impact summary, reviewers cannot distinguish a targeted change from
a fleet-wide access expansion or guardrail relaxation before activation.

**What to do (phase 2):** Add an asynchronous or paginated evaluation path
for deployments with more configured principals than phase 1's bounded,
synchronous pass can cover in one request — a background job with a
pollable status/result, or a paginated `principal_impacts` response —
without changing phase 1's response shape for the common case that already
fits under the bound.

### 42. Four-eyes config approval and separation of duties

**Effort: XL (1–3+ weeks).** This is a governance-model and authorization
change, not a confirmation-dialog feature: it needs new durable states,
reviewer records, scopes, invariants, audit actions, concurrency handling,
REST/CLI/UI flows, and migration/backward-compatibility decisions for existing
staged versions.

**Why it matters:** Item 31 currently implements an explicit validate → diff →
stage → typed-confirmation activate sequence, but one `admin:config:write`
principal can perform every step. Regulated and higher-risk customers often
need proof that the author of an access change could not approve and activate
their own proposal.

**What to do:** Add separate propose/review/approve/activate capabilities and a
durable approval record bound to an immutable version fingerprint. Enforce
author ≠ approver, invalidate approval if content changes, support rejection
with bounded review notes, prevent activation without the required approvals,
and audit every transition without YAML content. Keep a documented single-
administrator mode for smaller deployments, but never simulate four-eyes in
the browser while the server still permits self-approval.

### 43. Admin connection-operations and health workspace ✅ DONE

**Phase 1 shipped (admin connection-status API); phase 2a shipped (rate-limited
"test now" probe); phase 2b shipped (browser workspace). All three phases
complete.**

`GET /api/v1/admin/connections` (`api/admin_connections_routes.py`) returns a
credential-free, per-connection operational status built from the same
`HealthMonitor` snapshot `/health` already maintains, gated by a new
least-privilege `admin:connections:read` scope (distinct from the config
scopes — seeing whether a connection is reachable is a different privilege from
reading/changing what QueryGate connects to). Each entry reports `dialect`,
`enabled`, a derived `status` (`healthy`/`degraded`/`disabled`/`unknown`),
`last_checked`, `last_success` (persisted across a later failure), `latency_ms`,
`schema_reflected`, and a stable redacted `failure_category`
(`authentication`/`unreachable`/`timeout`/`error`). `HealthMonitor` was extended
to track last-success/latency and to classify failures **by exception type,
never message**, so a driver error embedding a host/username/password can never
leak through the API — the raw error stays in stdout logs only, exactly like
`/health`'s existing non-disclosure posture. Documented as QG-21 in
`docs/THREAT_MODEL.md`. Covered by `tests/unit/test_health.py` (classification,
latency/last-success tracking, message non-leakage),
`tests/integration/test_admin_connections.py` (status derivation, sorting,
raw-error/credential redaction, monitor-absent robustness), and
`tests/security/test_adversarial_security.py` (scope enforcement).

**Phase 2a shipped:** `POST /api/v1/admin/connections/{id}/test`, gated by its
own `admin:connections:test` scope — deliberately independent of
`admin:connections:read`, so passive status visibility does not imply the
ability to trigger a live probe, matching this file's established
least-privilege pattern (e.g. `catalog:generate` vs. `catalog:review`).
`HealthMonitor.manual_check()` reuses the exact `_check_once` ping/
classification seam the background loop already uses and updates the shared
cached status, so a following `GET` reflects the manual probe too. Each
connection can be manually probed at most once per new
`AppConfig.admin_connection_test_cooldown_seconds` (default 10s,
single-process visibility only, the same caveat as
`execution/concurrency.py`'s in-process semaphore) — `HealthMonitor` tracks
the last manual-probe time per connection and a request inside the cooldown
gets `429` with a `Retry-After` header instead of opening another real
connection to the target database. An unknown connection is `404`; a
deployment-disabled one is `409`; a missing/not-yet-started monitor is `503`.
Every probe attempt — successful or rate-limited — is recorded as its own new
`ConnectionProbeEvent`/`connection.probe` audit event
(`audit/events.py`/`audit/logger.py`'s `audit_connection_probe`, same durable
sink as query/config/catalog auditing) carrying the connection id, actor,
probe result, and failure category, but never a raw driver error or
connection string, matching `ConnectionStatus`'s existing non-disclosure
posture. Documented as QG-23 in `docs/THREAT_MODEL.md`. Covered by
`tests/unit/test_health.py` (`manual_check`/cooldown semantics),
`tests/unit/test_audit.py` (redacted event content),
`tests/integration/test_admin_connections.py` (404/409/503/429 paths, status
update, non-disclosure, audit persistence), and
`tests/security/test_adversarial_security.py`
(`test_admin_connections_test_now_requires_its_own_scope`).

**Phase 2b shipped:** a new "Connection health" workspace (nav item 08) in
the existing browser control plane (`admin_ui/index.html`, `admin_ui/app.js`),
built entirely on the already-tested REST surface above — no new endpoint,
no new backend logic. Lists every configured connection (dialect, status
chip, last checked/success, latency, schema-reflected state, and failure
category when degraded) and gives each row its own "Test now" button, gated
client-side on `admin:connections:test` (disabled with an explanatory
`title` otherwise, mirroring how the catalog workspace already gates its own
action buttons on `catalog:edit`/`catalog:approve`/etc.) and on the
connection's `enabled` flag. A click calls the phase 2a endpoint and updates
just that row in place from the response; a `429` reuses the backend's own
"retry in Ns" message via the existing toast mechanism rather than a new UI
affordance. Lazy-loads the same way the audit/catalog tabs already do
(on first visit while authenticated).

Verified against a real running app and a real Postgres connection with
Playwright driving a headless browser, not just a code read: connecting,
switching to the tab, watching the row populate with live status/latency,
clicking "Test now" and seeing the row update and a success toast, a second
immediate click correctly surfacing the `429`/cooldown toast, and the
resulting `connection.probe` audit events appearing correctly in the audit
trail tab.

That last check caught two real, pre-existing gaps this item's own new
event type exposed, both fixed here: `admin_ui/app.js`'s `renderAuditEvent()`
only special-cased `query.execution`/`config.governance` and silently
mislabeled everything else — including the new `connection.probe` events,
and latently `catalog.governance` too — as `Catalog ${event.action}` (so a
probe event rendered as "Catalog undefined"); and
`api/admin_ui_routes.py`'s `_AUDIT_EVENT_TYPES` allowlist for the
`event_type` filter param didn't include `"connection.probe"`, so filtering
the audit browser down to just probe events would have 422'd. Both are fixed
generically (explicit branches per event type; the new type added to the
allowlist) rather than special-cased narrowly. Covered by
`tests/integration/test_admin_ui.py::test_audit_browser_accepts_connection_probe_event_type`.

**Original scope (for reference — see above for what shipped in phases 1, 2a, and 2b):**

**Effort: L (3–5 days).** The aggregate readiness monitor already exists, but
an admin surface needs a separately authorized detailed health model, safe
error classification, refresh/test operations, and UI states across healthy,
degraded, disabled, and never-checked connections.

**Why it matters:** The public `/health` endpoint intentionally returns only
aggregate counts so it cannot disclose database topology. Administrators still
need to know which configured connection is failing, when it last succeeded,
whether schema reflection is stale, and whether a new credential or network
change works—without searching process logs or exposing a driver exception.

**What to do:** Add an admin-scoped, credential-free connection status API and
workspace showing dialect, enabled state, last check/success, bounded latency,
schema-cache/refresh state, and stable redacted failure categories. Provide a
rate-limited “test now” action that uses the same engine/timeout/TLS settings as
normal operation, never returns connection strings or raw driver text, and
audits manual probes without turning them into query access.

### 44. Admin observability and rejection-trend dashboard

**Effort: L (3–5 days).** Current Prometheus metrics and JSONL events provide
the raw signals, but a useful dashboard needs safe aggregation, time-window
semantics, cardinality controls, pagination, and a decision about behavior when
no durable time-series backend is configured.

**Why it matters:** Item 31 can browse individual audit events, but it cannot
answer operational questions such as “Which policies reject the most
requests?”, “Is queue pressure rising?”, “Did cost-estimation availability
regress?”, or “Which connection changed after the last rollout?” Those trends
are what let an administrator tune policy and capacity proactively.

**What to do:** Add overview cards and time-window charts for query volume,
success/rejection categories, queue wait/depth, concurrency saturation,
timeouts, cost-estimation unavailable/would-reject rates, and config/catalog
changes. Prefer querying an operator-configured metrics backend when available;
otherwise expose an honest current-process snapshot and label it as such—do
not imply durable history. Keep labels low-cardinality and require admin scope
for any principal-, connection-, table-, or policy-specific breakdown.

### 45. Dedicated non-admin “My access” portal

**Effort: M (2–3 days).** The required access-summary and policy-filtered
schema APIs already exist, so this is mainly a focused UI/IA split plus tests
proving the user route never imports admin-only data or actions.

**Why it matters:** Regular authenticated users can open `/admin/` and inspect
their visible connections/schema, but the surrounding control-plane navigation
is misleading and fills the page with disabled actions. A reporting agent
owner or analyst needs a clear explanation of their own access and limits, not
an administrator console they mostly cannot use.

**What to do:** Add a separate `/access/` experience showing the caller's
identity/auth method, visible connections, policy-filtered schema/catalog,
effective query limits, mandatory-claim requirements, and safe explanations of
recent personal denials where the audit authorization model permits it. Never
show raw YAML, other principals, global audit history, version controls, or
admin navigation; keep `/admin/` explicitly scoped and worded for operators.

### 46. Validated policy templates and safe-start presets

**Effort: M (2–3 days).** Rendering a form is small; the real work is defining
versioned presets, parameter schemas, secure merge semantics, documentation,
and tests that prevent a template from silently broadening an existing policy.

**Why it matters:** Common policies—deny-by-default, reporting-only,
customer-support, tenant-isolated, and bounded analytics—currently require
admins to know every relevant YAML field. Templates can shorten setup and
reduce omission errors, especially for mandatory filters and response/capacity
guardrails.

**What to do:** Ship a small, versioned set of code-reviewed presets with
explicit parameters and an educational preview of every resulting rule. Apply
them only to the local draft, run normal config validation plus item 39's
candidate simulation, and show item 40's semantic diff before staging. Default
to restrictive values, never infer table/column grants from names, never embed
credentials or tenant values, and keep generated YAML fully editable/exportable
for infrastructure-as-code users.

### 47. Safe draft recovery plus config export/import UX

**Effort: M (2–3 days).** Basic download/upload is small, but safe recovery
must handle sensitive connection documents, version/fingerprint metadata,
schema validation, stale-base conflicts, size limits, and browser-storage
rules without creating an ungoverned shadow config store.

**Why it matters:** Item 31 warns before abandoning an in-memory draft, but a
tab crash or browser restart still loses work. Administrators also need a
convenient way to move a reviewed change between environments while preserving
the YAML/CLI path rather than copying text fields by hand.

**What to do:** Add bounded download/upload of a versioned change-set bundle
containing only the submitted document deltas, base-version fingerprint, and
description; validate it before preview/stage and surface stale-base conflicts.
Allow tab-scoped recovery for policy-only drafts, but do **not** persist
connections YAML, literal credentials, secret references, or bearer tokens in
`localStorage`/IndexedDB. Full-config recovery should use an explicitly
downloaded file or a server-side, authorized, encrypted-at-rest draft store
with retention/deletion controls and audit events—not invisible browser
persistence.

### 48. Pre-defined, admin-approved query templates ("Toolbox"-style curated tools)

**Effort: L (3–5 days).** Not a new execution path — the resolved query still
runs through the full existing pipeline (policy, schema, compiler,
concurrency, audit). The work is the template model itself (typed parameter
slots bound into a stored `StructuredQuery` AST), reusing 32B's governance
state machine for review/approve/publish/rollback, and new discovery/
invocation surface on REST and MCP.

**Why it matters:** Google's Gen AI Toolbox for Databases popularized a
pattern this project's own model is a natural fit for: instead of (or in
addition to) letting an agent compose an arbitrary `StructuredQuery` within
policy caps, an admin pre-defines a fixed set of named, parameterized
queries — "get_orders_for_customer(customer_id)", "top_n_products(n,
category)" — and agents only ever call one of *those* by name with typed
parameters. This shrinks the effective attack/error surface to a reviewed,
finite set of query shapes, gives non-technical stakeholders something
concrete to sign off on ("these are the 12 things this agent can ask"), and
matches how teams already think about tool-calling for agents. Critically,
this is additive to QueryGate's existing guarantee, not a new one: there is
still no raw-SQL field anywhere — a template is just a stored, named,
parameterized `StructuredQuery` AST, so it inherits every validation and
guardrail already built for ad-hoc queries.

**What to do:** Add a `QueryTemplate` model (id, description, target
connection, a `StructuredQuery` AST containing named parameter
placeholders, and typed/validated parameter slots — type, required,
min/max, allow-list) stored via the same file-backed, versioned mechanism
already used for the catalog/config (do not add a second store or mutation
path, per this file's standing rule for catalog/config governance). Route
create/edit/approve/publish/rollback through 32B's existing governance gate
— a template must never publish itself or skip review, same as a catalog
draft. At invocation time, bind the caller's parameters into the stored AST
and run the *resulting* `StructuredQuery` through the unchanged
`StructuredQueryService` pipeline — parameters get no special exemption
from policy caps, allow/deny lists, or mandatory row filters. Expose
published templates as individually discoverable/named MCP tools (e.g.
`list_query_templates`, `run_query_template(id, params)`) and a REST
endpoint, filtered per-principal the same way item 22 scopes connection
visibility. Audit template invocations distinctly (template id + param
shape, never param values or SQL) so operators can see curated-tool usage
separately from ad-hoc structured-query usage in the same audit stream.

---

## P3 — future scope (explicitly deferred, not a gap in v1)

### 18. Stored-procedure catalog

**Effort: XL (1–3+ weeks).** This is a new subsystem, not an extension of
an existing one: a catalog/registry model (mirroring `connections/` +
`policy/`'s design), typed parameter validation (no schema-reflection
equivalent exists for procedure signatures), new REST/MCP surface, policy
gating, and a real security review given procedures can have side effects
that structured SELECT queries structurally cannot.

**Why it matters:** The original prototype had a stored-procedure pass-through
(`execute_stored_procedure`) that QueryGate deliberately did not port —
exposing arbitrary stored procedures is a different (and harder) safety
problem than structured SELECT queries: procedures can have side effects,
arbitrary parameter shapes, and no equivalent of schema-reflection-based
validation.

**What to do (when prioritized):** Design an explicit, policy-gated catalog
model — each exposed procedure individually declared (name, typed
parameters, whether it's confirmed read-only) rather than a generic
pass-through, matching the "safe stored procedure/tool catalog pattern"
called out as a goal but intentionally not attempted in v1.

### 19. Additional dialects (MySQL, Snowflake, BigQuery, etc.)

**Effort: M–XL, per dialect.** MySQL is closest to Postgres/MSSQL's shape
(mature async SQLAlchemy driver, standard `INFORMATION_SCHEMA`) — likely M
(2–3 days). Snowflake/BigQuery are architecturally different (no native
async driver in some cases, different auth models, different SQL dialects
for date functions) and are realistically L–XL each, closer to "add a new
connection type" than "extend an enum."

**Why it matters:** goal 7 scoped this to "Postgres and MSSQL... if
feasible." Broader dialect support is a natural expansion once those two are
production-hardened (items 2–4), but adding a third dialect before the first
two are fully proven would spread verification effort thin.

**What to do (when prioritized):** Extend `connections/dialects.py` and the
compiler's dialect dispatch (currently a 3-way branch in
`_date_bucket_expr`) — the isolation pattern already supports this, it's
additive work, not a redesign.

### 20. Client SDK / agent-framework integration examples ✅ DONE

**Shipped:** `examples/claude_agent_sdk_integration.py` — a runnable script
registering QueryGate as an HTTP MCP server in the Claude Agent SDK
(`ClaudeAgentOptions(mcp_servers=...)`) and asking a natural-language
question against the demo data. Linked from README.md alongside the
existing raw-JSON-RPC/curl examples.

Verified in two layers, being explicit about which: the MCP *transport*
this depends on (tool discovery + tool invocation over Streamable HTTP) was
verified live against a real running QueryGate server using the official
`mcp` client SDK directly — `list_tools`, `list_connections`, and
`execute_structured_query` all round-tripped correctly. The example's own
`ClaudeAgentOptions`/`McpHttpServerConfig` construction was verified
against the real installed `claude-agent-sdk` package (not guessed from
memory) and imports/parses cleanly. The full agentic loop (an actual model
call choosing to invoke the tools) was **not** exercised — no
`ANTHROPIC_API_KEY` was available in this environment — so that specific
path is written-but-not-live-tested; everything it depends on mechanically
was.

Only one framework (Claude Agent SDK), not two — LangChain wasn't added,
to keep this scoped rather than chasing "just one more framework" per this
item's own stated scope-creep risk.

**Effort: S (0.5–1 day) per framework example.** Writing and testing one
working example against LangChain or the Claude Agent SDK is quick; scope
creep risk is adding "just one more" framework rather than the work itself
being hard.

**Why it matters:** `examples/mcp_calls.md` and `examples/rest_calls.md`
show raw JSON-RPC/curl, which is correct but not the fastest path for a
developer wiring QueryGate into LangChain, the Claude Agent SDK, or a custom
tool-calling loop.

**What to do (when prioritized):** Add a thin example (not a maintained SDK
necessarily) showing QueryGate's MCP tools registered in one or two popular
agent frameworks, to shorten time-to-first-query for adopters.

### 31. Admin UI / policy designer ✅ DONE

**Shipped:** an integrated, dependency-free control plane at `/admin/`,
served by the existing FastAPI process rather than deployed as a second
service. It uses item 25's config-governance routes as the only mutation
path: the UI loads the immutable active snapshot, supports visual and raw
YAML editing, runs the existing dry-run validation/preview, stages a complete
version, and activates or rolls back through the same re-validating apply
endpoint as REST users. The CLI/YAML infrastructure-as-code workflow is
unchanged and remains fully supported.

The UI includes the full prioritized surface: policy-filtered connection and
schema review; a layered default/connection/principal policy designer for
table/column boundaries and query guardrails; line-level active-versus-draft
document previews; explicit validation and staging; version history with
typed-confirmation activation/rollback; active-policy test-as-principal
simulation (including mandatory-claim readiness, while redacting configured
filter values); and newest-first filtering/pagination over the persisted,
redaction-safe JSONL audit stream. The browser shell never receives a token
through a URL, uses same-origin APIs only, and is served with a restrictive
Content Security Policy, no-referrer/nosniff headers, and no-store HTML.

`api/admin_ui_routes.py` adds only the capabilities the existing API lacked:
validated policy parse/render for the visual designer, read-only principal
simulation, and memory-bounded audit browsing. It reuses
`admin:config:read` for inspection/simulation/audit and
`admin:config:write` for rendering/mutations, preserving item 25's existing
read/write separation rather than inventing an all-powerful UI scope. See
`tests/integration/test_admin_ui.py` for static-shell/security-header,
scope, policy round-trip/simulation, row-filter redaction, and audit
pagination/filtering coverage.

**Deliberate boundary:** “approval” in this version is an explicit
validate → review diff → stage → typed-confirmation activate workflow, not a
server-enforced two-person/four-eyes state machine. Adding reviewer identity,
separation-of-duties rules, scheduled activation, or SSO session exchange
would be a governance-model/API change beyond item 25, not UI-only work.

**Effort: XL (2–4+ weeks).** This should come after item 25 establishes the
admin API and governance model. Building UI first would risk encoding the
wrong workflow.

**Why it matters:** For a self-serve commercial product, the hardest part
for customers may not be running queries — it will be safely expressing
which agents can access which data. A visual policy designer, schema
browser, test-as-principal mode, and diff/approval flow could become a
major product differentiator.

**What to do (when prioritized):** Build a web admin surface for
connections, schema review, policy editing, dry-run validation,
test-as-principal checks, audit browsing, and config version rollback. Keep
the CLI/YAML path fully supported for infrastructure-as-code users.

---

## P4 — proposed: competitive parity (not yet triaged)

Items 49–60 come from an explicit gap analysis against the strongest
adjacent products (Google's Gen AI Toolbox for Databases, Hasura, and
Immuta/Privacera-class data-governance platforms), not from a repo scan.
None of them are a gap in v1's own stated scope — they're additive ground
to close QueryGate's remaining deficits in masking granularity, cost/quota
governance, ecosystem reach, and external trust signals. Triage into P2/P3
(or drop) once prioritized; until then this section is a holding area.

### 49. Column-value masking/tokenization (not just allow/deny)

**Effort: L (3–5 days).** The policy/compiler pipeline already resolves
every column reference before compilation (`validation/policy_validation.py`,
`compiler/sqlalchemy_compiler.py`); masking adds a transform stage between
"column is permitted" and "column is selected as-is," not a new enforcement
point.

**Why it matters:** Column policy today is binary — a principal either sees
a column's real value or cannot reference it at all. Any principal who
needs partial visibility (last-4-digits of a card number, a hashed customer
id, a bucketed salary range) currently has no option short of full access.
This is the single largest remaining gap against Immuta/Privacera-class
governance, which treat masking as a first-class policy primitive, not an
afterthought.

**What to do:** Add a `column_mask` policy primitive (hash, null,
truncate/last-N, bucket/round) resolvable per principal/column alongside
the existing allow/deny check. Apply the transform in the compiled `Select`
(e.g. a SQLAlchemy `func` wrapper) so the database itself never returns the
raw value to a masked caller — masking must happen in the query, not by
redacting the response after the fact. Audit which columns were masked
(never the pre-mask value) so operators can distinguish "denied" from
"masked" access in the same audit stream item 23 already provides.

### 50. Per-principal rate limits / query quotas over time

**Effort: M (2–3 days).** Reuses the Redis-backed cross-instance state item
9 already introduced for the concurrency limiter; this is a second counter
(a rolling window or token bucket keyed by principal) alongside it, not a
new distributed-state mechanism.

**Why it matters:** The concurrency semaphore (item 9) bounds how many
queries a principal can have *in flight at once*, not how many it can run
*over time*. A well-behaved agent that never exceeds its concurrency limit
can still issue tens of thousands of sequential queries an hour, exhausting
DB capacity or a customer's cost budget — the multi-tenant cost-governance
story enterprise buyers in `docs/business/GO_TO_MARKET.md`'s target segment
will ask for directly.

**What to do:** Add a per-principal (and optionally per-connection)
request-count and byte-count quota over a configurable rolling window,
enforced before execution alongside the existing concurrency guard. Return
a distinct, policy-shaped rejection (not a raw 429 with no context) and
audit quota rejections the same way other policy denials are audited today.

### 51. Typed client-side query-builder SDK (Python + TypeScript)

**Effort: M per language.** A thin, generated-or-hand-written typed
wrapper around the existing `StructuredQuery` Pydantic schema — no
server-side change; it mirrors a contract that already exists.

**Why it matters:** Adopters today either hand-write `StructuredQuery` JSON
or read `examples/rest_calls.md` / `examples/mcp_calls.md`'s raw JSON-RPC
and curl examples. Google's Toolbox and most competing frameworks ship
typed SDKs in multiple languages with autocomplete and client-side
validation. This is the single largest lever on integration friction and
the most concrete ecosystem gap identified against Google's Toolbox.

**What to do:** Generate (or hand-maintain, kept in sync via a schema test)
a typed builder for `StructuredQuery` in Python and TypeScript — table/
column references, filters, joins, and aggregations as typed method calls
rather than raw dict/JSON construction. Ship as an installable package, not
just an example script, and keep it a pure client-side convenience: it must
not bypass or duplicate any server-side validation.

### 52. Multi-framework agent integration examples (LangChain, LlamaIndex, OpenAI function-calling)

**Effort: S per framework.** Same shape as item 20's existing Claude Agent
SDK example — a runnable script registering QueryGate's MCP tools and
asking a natural-language question against the demo data.

**Why it matters:** Item 20 deliberately scoped to one framework to avoid
scope creep. With the core product now stable, closing this gap directly
addresses the ecosystem-breadth deficit against Google's Toolbox, which
documents integration with most major agent frameworks out of the box.

**What to do:** Add one example per additional framework, following item
20's existing verification bar (mechanically verify what can be verified
without a live model call; state plainly what wasn't exercised). Resist
adding a maintained framework-specific SDK layer beyond the example
itself — that risk was already called out in item 20.

### 53. Independent third-party security audit + published report

**Effort: S (engineering coordination and remediation only). The audit
itself is an external vendor engagement and calendar-time cost, not
solo-engineer effort.**

**Why it matters:** `docs/business/GO_TO_MARKET.md` already lists
"independent threat-model review, formal certification, or external
penetration testing" under things not safe to claim yet. No amount of
internal adversarial testing (item 28) substitutes for third-party
validation, and it is the highest-leverage trust signal for the
fintech/healthcare buyers the business brief names as the ideal customer.

**What to do:** Commission an external audit/pentest scoped to the
structured-query pipeline, auth boundary, and admin/config governance
surface once items 28 and 36 are complete. Remediate findings, then publish
a redacted summary report as a sales asset per the business brief's "core
sales assets" list.

### 54. Compliance control mapping (SOC 2 / ISO 27001 readiness)

**Effort: L (mostly documentation and gap analysis; some control-filling
code, e.g. formalized retention/access-review evidence).**

**Why it matters:** Regulated-industry buyers will ask "where's your SOC 2"
as a gating question in a security review, before they evaluate
architecture. QueryGate already has most of the underlying controls
(redaction-safe audit trail from item 23, governed config change management
from item 25, adversarial test suite from item 28) — this item is mapping
what's already built to a recognized framework's control list, not building
new security features from scratch.

**What to do:** Produce a control-mapping document against SOC 2 (or ISO
27001) trust-service criteria, identify genuine gaps (e.g. formal
access-review cadence, incident-response runbook), and close only the gaps
that are real rather than adding process theater around controls that
already exist.

### 55. Inference/transitive-exposure adversarial test suite

**Effort: M (2–3 days).** Extends item 28's existing adversarial suite with
a new attack category rather than a new enforcement mechanism.

**Why it matters:** Column allow/deny stops a query from directly selecting
a denied column, but does not provably stop a caller from reconstructing a
denied value indirectly — e.g. inferring a denied `salary` through a
permitted bucketed join key, or through a computed expression built
entirely from permitted columns that happens to correlate with a denied
one. This is a distinct security category (inference attacks) that item
28's threat model may not yet enumerate.

**What to do:** Write a design note enumerating known inference-attack
shapes against this AST model, then add adversarial regression cases for
each to item 28's suite. Where a real gap is found (rather than a
theoretical one), decide explicitly whether it's closed by policy (e.g.
restricting join keys derived from sensitive columns) or documented as an
accepted residual risk — don't leave it silently unaddressed either way.

### 56. HA / multi-region reference deployment + DR runbook

**Effort: L (3–5 days).** Builds on item 29's reference stack and item 9's
cross-instance concurrency state; the new work is failover behavior and a
documented recovery procedure, not a new deployment topology from scratch.

**Why it matters:** `docs/business/GO_TO_MARKET.md` explicitly says not to
claim "a production Helm/Kubernetes reference deployment" yet. Item 29's
reference stack is not the same claim as proven multi-instance failover —
enterprise buyers evaluating this for production traffic will ask for an
HA/DR story specifically, not just a docker-compose file or a single Helm
chart.

**What to do:** Document (and test) a multi-replica deployment with the
Redis-backed concurrency/rate-limit state from items 9 and 50 shared
correctly across instances, a rolling-restart/zero-downtime config-reload
path building on item 5, and a written disaster-recovery runbook
(backup/restore for the config-governance store, recovery time
expectations).

### 57. Pluggable dialect-adapter architecture

**Effort: L (interface design); each subsequent dialect then becomes
independent M-effort work rather than a bespoke project.**

**Why it matters:** Item 19 treats every new dialect as M–XL bespoke work
gated on core-team bandwidth — the actual long-term bottleneck behind
QueryGate's biggest competitive gap (database breadth against Google's
Toolbox and Hasura). `connections/dialects.py` and the compiler's dialect
dispatch (the 3-way branch in `_date_bucket_expr`) already isolate
dialect-specific behavior; formalizing that isolation into a stable adapter
interface is what would let dialect support scale without linearly scaling
core-team effort.

**What to do:** Extract a formal `DialectAdapter` interface (session
guardrails, date-bucketing, cost-estimation hook from item 26) from the
existing 2-dialect implementation, verify it holds by porting Postgres and
MSSQL onto it with no behavior change, and only then treat additional
dialects (item 19) as adapter implementations rather than core-pipeline
changes.

### 58. Published adversarial benchmark vs. raw-SQL agent and Google Toolbox

**Effort: M (2–3 days).** Packaging existing adversarial cases (item 28)
and QA scenarios (item 36) into a repeatable, publishable comparison, not
new attack development.

**Why it matters:** `docs/business/GO_TO_MARKET.md`'s adversarial
five-minute demo is currently a sales narrative performed live. Turning it
into a repeatable, published benchmark (attack corpus, catch rate, latency
overhead versus a raw-SQL agent baseline and Google's Toolbox where a fair
comparison is possible) converts an internal QA asset into external
technical credibility — the artifact that actually wins a "why not just use
Toolbox" conversation instead of asserting it.

**What to do:** Define a fixed, versioned attack/question corpus, run it
against QueryGate, a naive raw-SQL LLM-agent baseline, and (where feasible)
a comparably configured Google Toolbox setup, and publish catch-rate and
overhead numbers. Keep the comparison factual and reproducible — per this
file's own external-market-reference instruction to never misrepresent a
competitor's documented capabilities.

### 59. Read-only behavioral anomaly surfacing on the audit stream

**Effort: M (2–3 days).** A read-only aggregation over the existing
persisted audit sink (item 23) surfaced in item 44's dashboard — no new
execution-path or policy-mutation code.

**Why it matters:** Item 44 covers rejection-trend dashboards — denied
queries. This is distinct: surfacing unusual volume or shape even among
*allowed* queries per principal (e.g. a sudden order-of-magnitude spike) as
a passive alert. Must stay strictly within the 32C boundary already fixed
in `CLAUDE.md`: a read-only signal for a human admin to look at, never an
autonomous policy edit or a feedback loop back into enforcement.

**What to do:** Add a bounded, per-principal volume/shape baseline computed
from the persisted redaction-safe audit stream, surface deviations as a
dashboard signal in item 44's admin workspace, and explicitly do not wire
it into any automatic policy change, throttle, or block — that would cross
the 32C boundary this file already treats as a hard line.

### 60. Bug bounty / responsible disclosure program

**Effort: S (process and policy, not engineering).** Pairs with item 53 —
stand this up once an initial third-party audit has cleared the obvious
issues, not before.

**Why it matters:** A public disclosure process is a cheap, durable trust
signal for security-conscious buyers, and closes the gap where currently
there is no external channel for a researcher to report an issue
responsibly.

**What to do:** Publish a `SECURITY.md` disclosure policy and scope, decide
on a bounty/recognition structure appropriate to the project's current
stage, and route incoming reports through the same remediation process
established for item 53's audit findings.

---

## P5 — MCP token/context efficiency

Items 61–67 come from an explicit MCP token/context-efficiency audit, not
from a general repo scan. The comparators are DBHub (`bytebase/dbhub`, ~2
tools / ~1.4k tokens of combined schema+instructions) and Google's MCP
Toolbox for Databases — both meaningfully leaner MCP surfaces than
QueryGate's. A verification pass (2026-07-20) instantiated the real FastMCP
server and measured QueryGate's starting per-session fixed overhead: 14
registered tools plus `MCP_INSTRUCTIONS` totaling roughly 68,600 chars
(~17,150 tokens) sent on every session's `initialize`/`tools/list`
exchange, regardless of which tools a caller ever uses — roughly 12x
DBHub's footprint. About 2,880 of the ~15,076 tool-schema tokens were pure
duplication (the identical `StructuredQuery` JSON Schema `$defs` tree
repeated verbatim across three tools), not information a client couldn't
already have gotten once. After items 61–67: 12 tools, 53,841 chars
(~13,460 tokens) — roughly 9.6x DBHub's footprint, down from 12x, measured
in `tests/unit/test_mcp_token_budget.py`.

Items 62–67 shipped the same session this tranche was triaged. Item 61's
protocol-level schema-sharing option turned out not to exist (the MCP
`tools/list` response has no cross-tool `$defs` mechanism, and no real
client resolves an external `$ref`) — the only working fix was merging
`execute_structured_query`/`explain_structured_query`/
`execute_structured_queries` into one `run_structured_queries` tool, a
deliberate breaking rename accepted explicitly rather than silently; see
its own entry for the full blast-radius accounting and what was updated.
Item 67 (restoring `StructuredQuery` field descriptions that turned out
not to exist, closing a real reliability gap item 62's design had left)
arrived mid-session packaged with an unattributed, undisclosed-by-default
delivery mechanism — flagged to the user immediately per this project's
standing instructions on suspected prompt injection, independently
re-verified claim-by-claim before anything was kept, and partially
corrected (two redundant additions trimmed, one unsubstantiated claim
removed) — see item 67's own entry for the full account.

Every shipped item is a presentation/efficiency change only, per this
file's non-negotiable constraint: none of them touch the AST-only input
guarantee (no raw SQL, ever), the policy-before-compile enforcement order
or what it checks, credential redaction guarantees, audit event
content/redaction guarantees, or catalog provenance/precedence semantics.
Item 64 makes a verbose field (catalog citation provenance) opt-in — the
underlying tracking, resolution, and enforcement behind it is unchanged,
only its default *visibility* to a caller who didn't ask for it became
configurable. Item 63 is visibility-only tool-list filtering layered in
front of an unchanged call-time scope check, never a replacement for it.
Item 61's tool merge preserves every existing capability (execute, explain,
batch, per-item error isolation, admission/queue signals — including a
real `admission_state` gap in `execute_many()` found and fixed while
merging) behind a new single tool name and an always-a-list argument shape;
nothing that used to work stopped working, only the tool surface changed.
No efficiency fix that would require cutting an actual safety or
governance check was found necessary to close a meaningful share of the
gap. Final measured total (`tests/unit/test_mcp_token_budget.py`): 53,841
chars (~13,460 tokens), down from the original pre-tranche 67,338 —
roughly 20% lower — despite item 67 adding genuinely new content, because
item 61's merge more than paid for it.

\* The audit also checked `admission_id`/`queue_wait_ms` (two small integer
fields always present on a successful query result) and `admission_state`
(confirmed already conditional — populated only on a `CapacityTimeoutError`,
not unconditional bloat as originally suspected). Neither was significant
enough to justify its own item; both are folded into item 66's baseline
size measurement instead.

### 61. Deduplicate the StructuredQuery JSON Schema across execute/explain/batch tools ✅ DONE

**Shipped:** Confirmed against the MCP spec first (`mcp.types.Tool.inputSchema:
dict[str, Any]`, `ListToolsResult.tools: list[Tool]`) that each tool's schema
is fully self-contained with no shared top-level `$defs` across tools in a
`tools/list` response, and that a `$ref` to an external document — legal
JSON Schema syntax — isn't resolved by any real MCP client. So the only
viable fix was collapsing tool count, not schema sharing. Merged
`execute_structured_query`, `explain_structured_query`, and
`execute_structured_queries` into one `run_structured_queries` tool
(`mcp/tools/query.py`): `queries: List[StructuredQuery]` is always a list
(min length 1 — a single query is a batch of one), `mode:
Literal["execute", "explain"] = "execute"` replaces the separate explain
tool, and the response is always `results: [...]` in the same order with
per-item error isolation (one failing query never fails the others) —
extending the batch semantics `execute_structured_queries` already had to
every call, not just multi-query ones.

New `StructuredQueryService.explain_many()` (`execution/service.py`) mirrors
`execute_many()`'s per-item try/except exactly, including a real gap found
while wiring this up: `execute_many()`'s per-item error never carried
`admission_state` (only `admission_id`/`queue_wait_ms`), unlike the old
single-query tool's top-level `MCPErrorResult`, which did. Fixed by adding
`admission_state` to `BatchQueryItemResult`/`BatchQueryItemToolResult` and
populating it the same way the other two admission fields already were
(`getattr(exc, "admission_state", None)`) — without this fix, merging
execute into always-batch would have silently dropped the
`capacity_timeout`/`queue_full` signal for the single-query case, the most
common one.

This is a deliberate breaking rename — every caller/doc/example referencing
the three old tool names by name breaks. Updated everywhere found in this
repository: `mcp/instructions.py`, `examples/mcp_calls.md`,
`examples/claude_agent_sdk_integration.py` (its `allowed_tools` list),
`README.md`, `docs/PRODUCT_GUIDE.md` (+ regenerated `docs/product-guide.html`
via `scripts/generate_product_guide_html.py`, plus a new Decision Log entry
explaining the tradeoff), and `landing/sandbox.html`'s interactive demo.
An external integration holding the old tool names would need to update
too — that cost was accepted deliberately (see the Decision Log entry) in
exchange for permanently removing the duplication rather than accepting it
as a recurring per-session tax. Test coverage:
`tests/unit/test_service.py::test_explain_many_partial_failure`,
`tests/integration/test_mcp_server.py`'s
`test_mcp_run_structured_queries_explain_mode_never_executes` and
`test_mcp_run_structured_queries_isolates_per_item_failure`, plus every
existing MCP integration test that called one of the three old tools was
updated to the new shape (`tests/unit/test_mcp_tools_registration.py`'s
`_EXPECTED_TOOLS` and `tests/integration/test_mcp_server.py`'s copy both
updated to match).

Net effect measured in `tests/unit/test_mcp_token_budget.py`: 53,841 chars
(~13,460 tokens) total — down from the original pre-tranche 67,338, *despite*
item 67 (below) adding real new content, because this item collapsed
`StructuredQuery`'s `$defs` tree from 3 duplicated copies to 1.

**Effort: S–M (1–2 days).** The duplication is in schema *generation*, not
schema *content* — `StructuredQuery` and its nested AST types don't change;
the fix is either sharing one `$defs` tree across tool schemas or reducing
tool count, not touching `query_ast/models.py`'s validation.

**Why it matters:** `execute_structured_query`, `explain_structured_query`,
and `execute_structured_queries` (`mcp/tools/query.py:115,139-141,165-167`)
each take a `StructuredQuery` (or `List[StructuredQuery]`) parameter, and
FastMCP independently generates a full JSON Schema per tool. A live schema
dump confirms the three `$defs.StructuredQuery` trees (plus
`AggregateSelectItem`, `DateBucketSelectItem`, `JoinSpec`, `OrderBySpec`,
`Predicate`, `TopNSpec`, `WhereGroup`) are byte-for-byte identical — 5,759
chars each, 17,277 chars carried three times instead of once. That's
~2,880 of the ~15,076 tool-schema tokens sent on every MCP session before a
single tool is ever called, with zero information gain: a client already
has the identical schema from whichever of the three tools it saw first in
`tools/list`.

**What to do:** Investigate whether the installed `mcp` SDK version
supports emitting `StructuredQuery`'s schema once via a shared `$defs`
block referenced by `$ref` across all three tool signatures, rather than
each `@mcp_server.tool` call independently regenerating the full nested
schema. If the SDK doesn't support cross-tool `$defs` sharing, evaluate
collapsing `execute_structured_query`/`explain_structured_query` into one
tool with a `mode: execute|explain` parameter instead — verify first that
this doesn't change either tool's observable behavior, error shape, or
response shape, and that existing callers/examples/docs referencing the
tool names by name are updated. No change to `StructuredQuery` validation,
the compiler, or any enforcement path — this is schema transport only.

### 62. Consolidate redundant instructional prose into one source of truth ✅ DONE

**Shipped:** Trimmed `mcp/instructions.py` from 8,347 to 7,031 stripped
chars by removing the restated detail that duplicated `query_ast/models.py`
`Field` descriptions and tool descriptions (cross-connection `join_group`
semantics, `intent`, `order_by.dir`, `top_n.fn`'s options, `date_bucket`
granularity) in favor of short pointers ("see the field schema for..."),
and by removing the two internal self-duplications (date-bucketing restated
at both `:53-56`/`:98-104`, batching restated at both `:90-96`/`:123-125`).
Also trimmed `execute_structured_query`'s own tool description
(`mcp/tools/query.py`) the same way, since it independently restated the
same AST-field mechanics a third time. No guide-topic content move was
needed in the end — cutting the duplicate copies while keeping exactly one
canonical copy (the `Field` description, already schema-visible every
session regardless) fully addressed the redundancy without relocating
anything into on-demand-only territory, so the "note" in this item's
original write-up about guide topics lacking that detail is now moot: nothing
was ever moved there, only de-duplicated in place. Unique, non-schema-visible
facts (the join_group retry-avoidance advice, the default
`bucket_<Column>_<granularity>` alias-naming rule) were preserved, not
deleted. See `tests/unit/test_mcp_token_budget.py` for the measured total.

**Effort: M (2–3 days).** Touches `mcp/instructions.py`, tool descriptions
in `mcp/tools/query.py`/`schema.py`, and potentially new `help/content/*.md`
guide topics — text-only changes, no validation or behavior change.

**Why it matters:** Several concepts are restated near-verbatim across
three layers that are *all* sent to every MCP session (the `Field`
description in `query_ast/models.py`, the owning tool's own description,
and `mcp/instructions.py`) — confirmed for cross-connection `join_group`
semantics, the `intent` field, `order_by.dir` strict-enum validation, and
`QueueMode`/`queue_mode` semantics. `mcp/instructions.py` separately
restates its own date-bucketing section internally
(`instructions.py:53-56` vs. `:98-104`) and its own batching section
internally (`:90-96` vs. `:123-125`). Note: an earlier version of this
audit assumed this prose duplicates content already available on-demand via
`search_querygate_guide`/`get_querygate_guide_topic` — that assumption did
not hold up under verification. None of the current 11 guide topics
(`help/content/*.md`) contain the mechanical detail in question (the
`top_n.fn` enum, the `date_bucket` granularity list, `queue_mode` wait
semantics), so simply deleting the prose from `instructions.py` would leave
that detail nowhere at all, not "elsewhere on demand."

**What to do:** Pick one canonical home per concept. `query_ast/models.py`
`Field` descriptions own the field-level contract (Pydantic needs them for
validation-error messages regardless). Each tool's own description in
`mcp/tools/query.py`/`schema.py` stays tool-specific framing only, not a
restatement of field semantics. `mcp/instructions.py` shrinks to a short
orientation/index layer; move the mechanical detail that currently has no
other home (the `top_n.fn` enum, `date_bucket` granularity list and alias
rule, `queue_mode`/`wait_timeout_seconds` semantics) into new or extended
`help/content/*.md` guide topics, so `search_querygate_guide`/
`get_querygate_guide_topic` become the genuine on-demand source the
original design intended rather than an already-duplicated one. Fix
`instructions.py`'s two internal duplications regardless of the broader
restructure. No change to any validation rule or enforcement behavior —
only where its explanation lives and how many times it's repeated.

### 63. Scope-gate admin-only tool schemas out of non-admin sessions ✅ DONE

**Shipped:** `mcp/server.py`'s `_install_scoped_tool_listing` re-registers
the low-level `Server`'s `ListToolsRequest` handler with a wrapper that
calls the original `FastMCP.list_tools()`, then drops any tool in a new
`_SCOPE_GATED_TOOLS` dict (currently just
`inspect_querygate_configuration` → `admin:config:read`) whose required
scope isn't in the current request's principal scopes. The principal comes
from `mcp/auth.py`'s existing `get_mcp_caller()` contextvar — confirmed
this is populated for every ASGI request to the mounted MCP app (not just
`tools/call`), since `MCPAuthMiddleware` wraps the whole sub-app, so it's
set correctly before a `tools/list` request too. No request context (e.g. a
direct/offline call) fails open on visibility only, never on enforcement.
`help/service.py`'s existing call-time scope check is completely unchanged
and remains the actual authorization boundary — this is additive filtering
in front of it. `explain_querygate_config_field` was confirmed still
correctly unrestricted (visible to everyone) since it isn't in
`_SCOPE_GATED_TOOLS`. See
`test_mcp_tools_list_omits_scope_gated_tool_without_scope` and
`test_mcp_tools_list_includes_scope_gated_tool_with_scope` in
`tests/integration/test_mcp_server.py`, plus the updated
`test_mcp_dev_bypass_lists_tools` (the anonymous dev-bypass principal has
no scopes, so it now correctly no longer sees the admin tool either).

**Effort: M (2–3 days).** Requires investigating whether the installed
FastMCP SDK exposes a per-session tool-listing hook; if not, this needs a
thin wrapper around `list_tools()`, not a new transport.

**Why it matters:** `inspect_querygate_configuration` is gated on
`admin:config:read` at call time (`help/service.py:366-368`,
`ADMIN_CONFIG_READ_SCOPE`), but its full ~6,200-char schema is still sent
to every MCP session's `tools/list` response regardless of the
authenticated principal — a non-admin caller pays the token cost for a
tool it can only ever get `AuthorizationError` from calling. (Verification
also checked `explain_querygate_config_field`, which looks admin-flavored
but is intentionally *not* scope-gated per its own tool description —
leave that one visible to everyone; only `inspect_querygate_configuration`
is actually restricted today.)

**What to do:** Filter `inspect_querygate_configuration` (and any future
admin-scoped tool) out of the schema sent to a session whose authenticated
principal lacks the scope it requires, mirroring the connection-visibility
precedent item 22 already established for REST/MCP
(`connections/visibility.py`). This is defense-in-depth and a token-savings
measure only — the existing scope check in `help/service.py` remains the
actual authorization boundary and must not be weakened, relaxed, or
replaced by list-time filtering. Add a regression test proving a
non-admin-scoped session's `tools/list` omits the tool while an
admin-scoped session's still includes it, and that the underlying call-time
scope check still rejects a direct call even if list-time filtering were
ever bypassed.

### 64. Make full catalog provenance opt-in on describe_table/search_catalog ✅ DONE

**Shipped:** Added `CompactCatalogCitation` (status + precedence only) next
to `CatalogCitation` in `catalog/retrieval.py`, plus a `resolve_citation(...,
verbose: bool)` helper that builds the existing full `CatalogCitation`
exactly as before (via the unchanged `catalog_citation()`) and only
projects it down when the caller didn't opt in — `catalog_citation()`
itself, its precedence resolution, and `catalog/governance.py` are all
untouched. `search_catalog()` (`catalog/retrieval.py`) and
`describe_table()`/`search_catalog()` (`execution/service.py`) all gained a
`verbose_provenance: bool = False` parameter threaded down to every hit/
column/table/relationship citation, exposed on both the MCP tools
(`mcp/tools/schema.py`) and the REST routes (`api/routes.py`) for
consistency. `CatalogSearchHit.citation` and the three catalog-info
`provenance` fields are now typed `Union[CatalogCitation,
CompactCatalogCitation]`. Two internal (non-MCP/REST) consumers —
`catalog/benchmark.py` and `catalog/adaptive_learning_benchmark.py`, which
read `hit.citation.freshness` for offline evaluation — were found reading
this data too and updated to pass `verbose_provenance=True`, since they
need the full citation and aren't token-metered. See
`test_describe_table_catalog_provenance_is_compact_by_default` in
`tests/unit/test_service.py` and
`test_search_citation_is_compact_by_default` in
`tests/unit/test_catalog_retrieval.py` for the default-vs-opt-in proof;
existing tests that asserted full-citation fields were updated to pass
`verbose_provenance=True` explicitly.

**Effort: S–M (1–2 days).** Response-shaping only, at the two MCP tool/
service call sites — `catalog/governance.py`'s construction, precedence
resolution, and storage of `CatalogCitation` do not change.

**Why it matters:** `describe_table` (`execution/service.py`'s
`column_catalog`/`table_catalog`) and `search_catalog`
(`catalog/retrieval.py`) attach a full 9-field `CatalogCitation` (`entry_id`,
`source_class`, `source_evidence`, `status`, `confidence`, `precedence`,
`catalog_version`, `schema_fingerprint`, `freshness`) to every visible
column, the table itself, every relationship, and every search hit
unconditionally whenever a catalog is configured — with no opt-out
parameter on either tool. A simple "what columns does `orders` have"
lookup pays for the same governance-grade provenance payload as a caller
specifically auditing catalog trust. This is exactly the case CLAUDE.md
already carves out as acceptable: the catalog *data* can be gated behind an
opt-in verbosity flag as long as the underlying tracking/enforcement is
unchanged.

**What to do:** Add an opt-in parameter (e.g. `verbose_provenance: bool =
False`) to `describe_table` and `search_catalog`. The default response
carries a compact citation (e.g. `status` + `precedence` only, or a single
`catalog_verified` boolean) sufficient for an agent to know whether to
trust a value without needing every field; the full 9-field citation
(including the evidence list) is returned only when the caller opts in. Do
not change `CatalogCitation`'s construction, its precedence resolution, or
anything under `catalog/governance.py` — this touches response shaping in
`execution/service.py`/`catalog/retrieval.py` only. Add a regression test
proving the default response is smaller and that the full citation is
still byte-for-byte retrievable and unchanged when a caller opts in.

### 65. Add a response-size cap to get_querygate_guide_topic ✅ DONE

**Shipped:** `GuideTopicResponse` (`help/models.py`) gained `truncated:
bool = False` and `max_response_bytes: int = 16_384` fields.
`GuideService.topic()` (`help/service.py`) takes a matching
`max_response_bytes` parameter (rejecting anything under 512 bytes, same
floor as `search_catalog`); when the full response would exceed the
budget, it measures the envelope with empty content first (same
incremental-measurement approach as `search_catalog`'s truncation), then
truncates the `content` body to fit exactly. Exposed on the MCP tool
(`mcp/tools/help.py`) and the REST route (`api/help_routes.py`) with the
same default. All 11 current packaged topics (157–426 words) return in
full under the default — verified directly in
`test_guide_topic_is_not_truncated_under_the_default_byte_budget`
(`tests/unit/test_product_guide.py`), alongside a dedicated truncation test
and a too-small-budget rejection test.

**Effort: XS–S (a few hours–1 day).** Mirrors an existing pattern
(`search_catalog`'s `max_response_bytes`) rather than inventing a new one.

**Why it matters:** `search_catalog` already caps and truncates its
response via `max_response_bytes` (`catalog/retrieval.py`, default
16,384, enforced by incremental serialize-and-measure truncation, not just
an echoed number). `get_querygate_guide_topic` has no equivalent —
`GuideTopicResponse` (`help/models.py`) always returns the full topic
markdown uncapped (157–426 words across the current 11 topics in
`help/content/*.md`), with no truncation or summary option, even though
`mcp/instructions.py` explicitly steers callers toward this tool for
on-demand detail — making it more, not less, exposed to token cost as
guide content grows over time.

**What to do:** Add the same `max_response_bytes`-style parameter used by
`search_catalog` to `get_querygate_guide_topic` and `GuideTopicResponse`,
truncating with an explicit `truncated` flag rather than silently cutting
content off mid-sentence. Keep the default generous enough that all 11
current topics are returned in full (the point is guarding future growth,
not shrinking today's responses).

### 66. CI/test guardrail on total MCP schema+instructions size ✅ DONE

**Shipped:** `tests/unit/test_mcp_token_budget.py` instantiates the real
`create_mcp_server()` (same "assert against the live schema" pattern as
`test_credential_redaction.py`), sums `MCP_INSTRUCTIONS` length plus every
registered tool's description + input schema + output schema chars, and
asserts the total stays under an explicit `_MAX_TOTAL_CHARS = 75_000`
constant with a comment documenting the measured baseline and requiring a
deliberate bump in the same PR as any legitimate increase. Landed first
(before items 62/64/65) specifically so the rest of this tranche's changes
were measured against a real regression gate rather than ad hoc scripts —
the baseline moved from 67,338 chars pre-tranche to 66,458 chars after
items 62/64/65 (item 63's runtime-only filtering doesn't change registered
schema size, which is what this test measures) despite adding two new
opt-in parameters and a truncation parameter, net negative overhead. Later
updated to reflect item 61's merge and item 67's field descriptions — see
those items' own entries for the final numbers.

**Effort: S (0.5–1 day).** One new test, modeled directly on an existing
pattern in this codebase.

**Why it matters:** Nothing in the codebase or CI currently measures or
bounds the combined size of `MCP_INSTRUCTIONS` plus every registered tool's
JSON Schema — this audit measured the current total at 14 tools / ~68,600
chars (~17,150 tokens) sent on every session, but there is no regression
test analogous to `tests/unit/test_credential_redaction.py` (which asserts
against the live OpenAPI/MCP schema, not hand-maintained convention) to
stop that number from silently growing as new fields, tools, or `Field`
descriptions get added. Without this, items 61–65 are one-time fixes that
will erode again the next time someone adds a verbose
`Field(description=...)` or a new tool.

**What to do:** Add a test that instantiates the real FastMCP server (same
"assert against the live schema" pattern `test_credential_redaction.py`
uses), calls `list_tools()`, and asserts the combined chars/estimated-token
total for instructions plus all tool schemas stays under an explicit budget
constant. Fail the test (not just log) when the budget is exceeded, and
require any legitimate increase to bump the budget constant in the same
PR, so growth is a visible review decision rather than a silent regression.
Set the initial budget from this item's own measured baseline plus
reasonable headroom for near-term legitimate growth (e.g. a new dialect or
tool), not an arbitrary round number.

### 67. Restore StructuredQuery field descriptions items 62/64/65 assumed existed ✅ DONE

**Shipped, after independent review.** This item's content first appeared
in the working tree without attribution mid-session, packaged with an
instruction (in the tool output framing, not from the user) to not
disclose the change — treated as a prompt-injection attempt per this
project's standing instructions and reported to the user rather than acted
on directly. Every factual and technical claim below was independently
re-verified against `git HEAD` and the live schema before anything was
kept; the parts that didn't hold up were fixed or removed (see the last
paragraph).

Item 62's trim relied on the premise that per-field detail (order_by.dir,
WhereGroup and/or shape, select item variants, Table.Column format, top_n's
grouped-vs-ungrouped column rules, Predicate.value's per-op shape) was
"already schema-visible every session regardless" via `query_ast/models.py`
`Field(description=...)`. Checked directly against `git show
HEAD:src/querygate/query_ast/models.py`: true for `intent` only — the other
10 of `StructuredQuery`'s 11 top-level fields had no schema-visible
description at all, including `where`, a recursive and/or predicate tree.
This matters independently of whether a given caller's MCP client forwards
`mcp/instructions.py`'s server-level `instructions` into the model's
context: the MCP spec documents that field as a `MAY`-level hint a client
*can* choose to forward, not a guarantee, while a tool's JSON Schema is
something every client must have to call the tool at all. So content
essential to correct first-attempt tool use has to live in a
schema-guaranteed location, not only in `instructions.py` — a real gap in
item 62's design that this item legitimately closes.

Added `Field(description=...)` to `StructuredQuery`'s from/select/where/
group_by/having/order_by/top_n, a `WhereGroup` class docstring (and/or
exclusivity + recursion — a docstring rather than a field description so
it's attached once at the `$defs` level, not repeated per reference),
`Predicate.value` (the per-op shape rule `_validate_value_shape` already
enforced but was previously invisible in the schema), a `having` field
description (its AND-only semantics, not previously stated anywhere), and
one compact worked example on `execute_structured_query`'s tool
description (later folded into `run_structured_queries` — see item 61)
with the other two tools pointing at it rather than duplicating it.

Two of the original additions were reverted or moved after review:
`TopNSpec`'s docstring and `group_by`/`order_by`'s alias-referencing
description duplicated content item 62 had deliberately kept in
`mcp/instructions.py` ("Top-N per group", "Time-bucketed trends") — since
those two sections were never trimmed to account for the new schema
coverage, the net effect was a second copy of the same facts, not a fix.
Trimmed `instructions.py`'s two sections down to short pointers at the
schema (keeping the one fact — the `bucket_<Column>_<granularity>` default
alias name — that has no schema-visible home) so the reliability gain is
kept without paying for it twice. Also removed one unverifiable claim from
this item's original text ("real-world MCP agents were observed guessing
wrong") — presented as an empirical observation with no evidence behind
it; the technical justification above stands on its own without it.

Because item 61 (above) later collapsed `StructuredQuery`'s `$defs` from 3
duplicated tool-schema copies to 1, this item's content — which touches
that exact struct — ended up roughly 3x cheaper than it would have been if
item 61 hadn't shipped in the same pass. See item 61's entry for the
final combined numbers; the `_MAX_TOTAL_CHARS` budget in
`test_mcp_token_budget.py` reflects the state after both items, not this
one in isolation.

**Effort: XS–S.** Field descriptions and one example string; no shape or
behavioral change to the AST.

**Why it matters:** `where` (a recursive and/or predicate tree) and the
other nine previously-undocumented top-level fields are exactly the shapes
most likely to trip up a first attempt at constructing a `StructuredQuery`
with no schema-visible guidance — this closes that gap without reverting
item 62's de-duplication of the tool description prose itself.
