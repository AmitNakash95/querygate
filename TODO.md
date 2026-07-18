# TODO: QueryGate → production-grade

Action items to take QueryGate from "working, tested prototype" to a
ready-to-ship commercial product. Grouped by priority. Each item explains
*why* it matters, not just what to build — treat the "why" as the acceptance
criteria. Items marked with a file path point at where the current
(incomplete) implementation lives.

Cross-reference: `README.md` → "Current limitations" and
`MIGRATION_REPORT.md` → "What still needs work" cover the same ground more
briefly; this file is the actionable breakdown.

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
| 13 | No secrets-manager integration | M–L | 5 (pairs well) |
| 14 | ✅ Docker image never built/run (found + fixed as a side effect of item 4) | S* | — |
| 15 | No load/soak testing | M | — |
| 16 | ✅ Column policy case-sensitivity gap | XS | — |
| 17 | ✅ No config validation CLI | S | — |
| 18 | Stored-procedure catalog | XL | — |
| 19 | Additional dialects | M–XL (per dialect) | 2 (do MSSQL first) |
| 20 | ✅ Client SDK / integration examples | S | — |
| 21 | ✅ Principal policy must apply to every MCP/config surface | S | 6, 8, 10 |
| 22 | ✅ Principal-aware connection/tool visibility | S–M | 6, 8 |
| 23 | Persisted audit/event sink | M | 1, 12 |
| 24 | Release hygiene and reproducible v0.1.0 cut | S–M | 4, 14, 17 |
| 25 | Admin/config governance plane | L | 5, 6, 10, 13 |
| 26 | Query-cost estimation before execution | L | 2, 3, 15 |
| 27 | Semantic schema catalog and sensitivity metadata | L | 6, 16 |
| 28 | Threat model + adversarial security test suite | M | 1, 6, 8, 10, 11 |
| 29 | Production deployment reference stack | M | 4, 9, 12, 13, 14 |
| 30 | Distribution, SBOM, and signed release artifacts | M | 4, 14 |
| 31 | Admin UI / policy designer | XL | 25 |

✅ = done (see item body below for exactly what shipped and what, if
anything, was intentionally left out of scope).

Items 21–31 are the next quality tranche from the current repo scan: mostly
security consistency, enterprise operability, and product polish — the
areas that move QueryGate from "strong engineering prototype" toward a
credible 10/10 commercial infrastructure product.

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

### 23. Persisted audit/event sink

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

### 24. Release hygiene and reproducible v0.1.0 cut

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

### 25. Admin/config governance plane

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

### 13. No secrets-manager integration for connection strings

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

### 15. No load/soak testing of the concurrency and timeout guardrails

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

**Effort: L (3–5 days for one dialect, longer cross-dialect).** The hard
part is not calling `EXPLAIN`; it is turning dialect-specific plan output
into a conservative, understandable policy decision without blocking safe
queries unnecessarily.

**Why it matters:** Row limits, timeouts, and concurrency caps are reactive
guardrails. A 10/10 gateway should also be proactive: reject or warn on
queries that are likely to full-scan huge tables, explode joins, or stress a
production database before they run. This is a differentiator against
generic MCP database connectors.

**What to do:** Add an optional policy gate that compiles the structured
query, runs a dialect-specific dry plan (`EXPLAIN`/estimated plan), extracts
estimated rows/cost where available, and rejects queries above configured
thresholds. Start with Postgres, document MSSQL differences, and include
clear denial messages that help the agent narrow the query safely.

### 27. Semantic schema catalog and sensitivity metadata

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

### 28. Threat model + adversarial security test suite

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

### 29. Production deployment reference stack

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

**Effort: M (2–3 days).** Straightforward release engineering once the
package boundary is settled.

**Why it matters:** To be taken seriously by security teams, QueryGate
should ship with repeatable artifacts and supply-chain metadata: versioned
Python package, container image tags, lockfile discipline, SBOM, and ideally
signed images/releases. This is not glamorous, but it creates a lot of
buyer confidence.

**What to do:** Define the release process for PyPI/private registry and
container images. Generate an SBOM in CI, pin and review dependencies,
publish immutable version tags, and document verification steps. Add a
release checklist so every version is cut the same way.

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

### 31. Admin UI / policy designer

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
