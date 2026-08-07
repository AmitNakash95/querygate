# TODO archive — completed items

Full write-ups for every fully-shipped ✅ DONE item from [TODO.md](../TODO.md) — the "Shipped / Coverage / Decision Log / Why it matters" history moved out of the live worklist to keep routine reads cheap. Items are keyed by their original TODO.md number (search `### N.`) and ordered numerically. The one-line summary plus priority/effort for each still lives in TODO.md's Quick-scan summary and the stub under each item. Partially-done items (e.g. `✅ DONE (phase 1)`) keep their full body in TODO.md, not here, because they still carry open work.

This file is reference-only: nothing here is an open action item. Cross-references like "item 73" elsewhere in the repo resolve here.

---

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

### 26. Query-cost estimation before execution ✅ DONE

**Phase 1 (Postgres `EXPLAIN`-based estimation) ✅ DONE.** **Phase 2 (MSSQL
estimated-plan equivalent) ✅ DONE** — `estimate_mssql_query_cost` uses a
dedicated `SET SHOWPLAN_XML ON` connection (SHOWPLAN mode must own its batch, so
it can't share the query's session) and parses the estimated rows / subtree cost
from the SHOWPLAN XML; `StructuredQueryService._estimate_cost` dispatches by
dialect (Postgres inline-EXPLAIN vs MSSQL SHOWPLAN), same fail-open + observable
contract. Proven against a live MSSQL (`tests/integration/test_mssql_cost_estimation.py`:
the estimator returns rows/cost, a full scan over threshold is rejected before
execution, a selective query admits). The Postgres-only limitation the
CLAUDE.md/service noted is now closed — the cost gate enforces on both dialects.

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

### 35. Agent-visible capacity waiting, progress, and cancellation ✅ DONE

**Phase 1 shipped:** `concurrency_slot()` already waited for up to the
policy's `concurrency_wait_seconds` and raised `ConcurrencyLimitError`
(REST `429`) when that window expired — proven under real load by item 15's
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
  `X-QueryGate-Queue-Wait-Ms`) rather than folded into the existing (then-`422`,
  now-`429` — see phase 3) `{"detail": "too many concurrent..."}` body, so
  that documented string contract (`docs/LOAD_TESTING.md`) never changes.
  MCP's `MCPErrorResult` gains the same three fields for a capacity rejection.
  `core/exceptions.CapacityTimeoutError` (subclasses `ConcurrencyLimitError`)
  carries the id/elapsed-wait without touching any existing
  `isinstance`/`except ConcurrencyLimitError` call site.
  Terminal states this phase: `completed` and `capacity_timeout` — `queued`/
  `running`/`cancelled` need the asynchronous contract phase 3 adds.
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
let `QueueFullError` reuse the exact same REST rejection-plus-headers and MCP
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
REST/MCP integration tests proving the rejection/`MCPErrorResult` mapping, and
two adversarial security regressions
(`test_unbounded_waiting_queue_is_capped_not_a_dos_vector`,
`test_max_queue_depth_per_principal_prevents_one_caller_starving_another`)
proving the actual security property: a caller happy to wait indefinitely
cannot pile up an unbounded number of waiters, and one noisy principal
cannot exhaust another principal's share of the queue.

**Phase 3 shipped (2026-07-28):** the four remaining pieces, each resolved as
its own protocol/product decision recorded in `docs/PRODUCT_GUIDE.md`'s
Decision Log before build, per the maintainer's explicit sign-off:

- **MCP progress notifications** reuse FastMCP's built-in
  `Context.report_progress` — a real MCP standard message
  (`notifications/progress`) keyed on a client-supplied `progressToken`, not a
  bespoke QueryGate wire format. `run_structured_queries` (`mcp/tools/query.py`)
  builds `on_wait_start`/`on_admitted` callbacks only when `ctx is not None`
  (report_progress itself already no-ops with no token, so this stays
  unconditional and purely additive) and threads them through
  `execute_many`/`_execute_batch_item`/`execute()` — a two-point signal
  ("waiting up to Ns" then "admitted after Nms, executing"), not a continuous
  mid-wait tick: ticking would mean slicing `concurrency_slot`'s wait into
  shorter repeated attempts, risking a regression in the "a caller cannot
  extend its wait past the operator's ceiling" guarantee item 35 phase 1's own
  security regression test protects, for a DX-only feature.
- **A REST asynchronous contract**: `queue_mode=async` on `POST .../query`
  returns `202` immediately with `{admission_id, status_url}` — the
  `admission_id` is generated up front (a new `_admission_id` param on
  `StructuredQueryService.execute()`) and the real execution runs as a
  background `asyncio.Task`, so the caller never blocks on it.
  `GET .../query/{admission_id}` polls `execution/async_execution.py`'s new
  `InProcessAsyncExecutionStore` (state machine: `queued` → `running` →
  `completed`/`failed`/`cancel_requested` → `cancelled`) for the current
  state/result; `POST .../query/{admission_id}/cancel` requests cancellation.
  Both new endpoints 404 uniformly for an unknown id, a different
  connection's id, or (unless the caller holds the new `query:cancel` scope)
  a different principal's id — never an enumeration oracle, proven by a
  genuine cross-principal integration test using two JWTs with distinct `sub`
  claims (a same-anonymous-principal test would pass even with the ownership
  check deleted, confirmed by mutation-testing it during development).
  **In-process only for this pass** — like admission (phase 1) and quota
  (item 50), which both shipped in-process first; a Redis-backed cross-replica
  store is a natural, separately-scoped follow-up, documented as a real
  limitation (a caller polling a different replica than the one that started
  the query gets a `404`) rather than silently assumed away.
- **Real dialect-level cancellation**, gated on a new deny-by-default
  `Policy.allow_query_cancellation` flag (the same posture as
  `allow_cross_join`, item 103) — not queue-only cancellation, and not a
  silently-attempted best-effort call. `connections/dialects.py`'s
  `SessionDialectAdapter` gained `capture_session_identifier`/`cancel_session`:
  Postgres captures `pg_backend_pid()` and cancels via `pg_cancel_backend`
  (interrupts just the query, the pooled connection stays alive — chosen over
  `pg_terminate_backend`, which would force the pool to detect and recycle a
  dead connection for one query's cancellation); MSSQL captures `@@SPID` and
  cancels via `KILL <spid>` (T-SQL's only out-of-band primitive for this,
  which terminates the whole session — a real cross-dialect asymmetry
  recorded rather than papered over with a synthesized "graceful cancel" MSSQL
  doesn't offer). Both permission requirements (Postgres: superuser or
  `pg_signal_backend` role membership; MSSQL: `ALTER ANY CONNECTION` server
  permission or sysadmin) were discovered during design, not assumed — a
  cancel request for a RUNNING query is rejected outright, before any DB call,
  unless the policy flag is explicitly set; the operator sets it only after
  granting the permission themselves. `connections/engine.py`'s
  `session_scope()` gained an optional `session_identifier_sink` parameter
  (skipped by default — no extra round-trip on the ordinary synchronous path;
  only the async lifecycle passes it) that captures the identifier right
  after guardrails, before the caller's query runs. Cancelling a still-`queued`
  execution is cheaper: `record.task.cancel()` interrupts the wait cleanly
  (asyncio's single-threaded cooperative scheduling makes the state check and
  the cancel call atomic, so no lock is needed), no DB call needed. A query
  that completes despite a cancel request (the DB-level cancel lost the race)
  reports `completed` with the real result, not a forced `cancelled`.
- **Capacity/queue rejections migrated fully from REST `422` to `429` +
  `Retry-After`**, matching item 50's quota-rejection precedent, with no
  compatibility flag to keep emitting `422` — `CapacityTimeoutError`/
  `QueueFullError` gained a `retry_after_seconds` field (the connection's own
  `concurrency_wait_seconds` ceiling, the same conservative-hint approach
  quota uses) and MCP's error-code classification moved `ConcurrencyLimitError`
  from `VALIDATION` into the `RATE_LIMITED` bucket alongside
  `QuotaExceededError` — "at capacity, retry later" is the same semantic as
  quota exhaustion, not a structural validation error. Recorded as a
  deliberate breaking change (`CHANGELOG.md`); `docs/LOAD_TESTING.md`'s
  documented response-body string contract is unchanged, only the status code
  and the added header.

Covered by `tests/unit/test_admission.py` (async treated identically to wait
for the ceiling calculation — pinned against a literal expected value, not
just cross-checked against `wait`'s own result, so a future `async`
special-case that happened to coincidentally match `wait` for one input pair
can't hide behind it), `tests/unit/test_async_execution.py` (11 tests:
success/failure/cancellation state transitions, masking of a non-actionable
background-execution error versus an actionable one passing through
unmasked, the queued-vs-running cancel paths — the policy-gate test asserts
`cancel_session` is never invoked, not just that some exception was raised —
the not-yet-connected retry case, the completes-despite-cancel and
fails-after-cancel races, and idempotency for both a completed record and one
already `cancel_requested`), `tests/unit/test_dialects.py` (5 new tests:
Postgres/MSSQL session-identifier capture and cancellation, including a
defensive non-integer-SPID rejection test for MSSQL's un-parameterizable
`KILL`), `tests/unit/test_mcp_progress.py` (4 tests: hooks built only when
`ctx` is present, the two progress messages, and `queue_mode=async` rejected
as a validation error), `tests/unit/test_service.py` (2 new tests:
`on_wait_start`/`on_admitted` fire with the right values, `on_session_identifier`
forwards to `session_scope`), `tests/unit/test_capacity_rate_limit_mapping.py`
(5 tests: REST 429 + MCP RATE_LIMITED mapping for both the bare and enriched
exception), and `tests/integration/test_async_query_lifecycle.py` (7 tests
against a real SQLite-backed REST app: `202` → poll → `completed` with real
rows, unknown/wrong-connection admission-id 404s, idempotent
cancel-of-completed, a real driver-failure masking test, and both directions
of the genuine two-JWT cross-principal test — denied without `query:cancel`,
allowed with it). Every new enforcement point was mutation-verified: the
cross-principal ownership check, the `allow_query_cancellation` policy gate,
the window/session-identifier wiring — each was deliberately broken, confirmed
to fail for that exact reason, then
reverted.

**Deliberately out of scope for phase 3** (real, separately-scoped follow-ups,
not silently dropped): a Redis-backed cross-replica async-execution store
(single-process only, documented above); deeper Prometheus/audit-event tagging
for the async lifecycle specifically (a cancelled-while-queued execution never
reaches `classify_rejection` or increments a metric today — it's caught
entirely inside `async_execution.py`; a cancelled-while-running execution IS
audited via the normal failure path, with the real DB-driven error text, just
without a dedicated `admission_state="cancelled"` tag); and MCP-side
cancellation (scoped to REST only — an MCP tool call has no separate
"cancel a previous call" concept the way REST's dedicated cancel endpoint
does).

**Why it matters:** Before phase 3, a caller saw either a slow pending tool
call, a final result, or a final capacity error. It could not ask to fail
fast or wait for a caller-selected period (phase 1), could not distinguish
"queued behind two queries" from "the database is slow" across replicas
(phase 2), and could not present a supported cancel action while it waits or
tell a waiting agent anything mid-flight. Phase 3 closes that: an interactive
agent gets a progress signal while queued, a REST caller gets a poll-and-cancel
contract instead of a blocking call, and a genuinely stuck query can be
stopped at the database itself — deliberately, only where an operator has
opted in and granted the permission.

**Two real bugs found and fixed by an independent re-audit (2026-07-28),
not silently left broken:**

1. **Unmasked internal error text leaked via the async status poll.** The
   background execution task (`async_execution.py`'s `_run()`) stored a
   failing query's raw `str(exc)` and `GET .../query/{admission_id}` served
   it verbatim — the synchronous REST path's `mask_unexpected()` never wraps
   this background task, so a non-actionable exception (potentially driver
   details, SQL, bind values, or a filesystem path) could reach whichever
   principal owns the admission_id, breaking redaction parity with every
   other route. Fixed by routing the stored message through the same
   `public_error_message()` classifier the synchronous path uses (actionable
   exceptions like `QueryValidationError` still pass through with their real
   text; everything else reduces to the generic message), plus a
   server-side `log.exception` so the real detail isn't lost, just not
   handed to the client. Regression-tested in both directions
   (`test_a_failing_execution_transitions_to_failed_with_a_masked_error`,
   `test_a_failing_execution_with_an_actionable_error_is_not_masked`).
2. **`queue_mode=async` silently downgraded to synchronous execution on
   every endpoint except the one that implements it.** Only `POST
   .../query` builds the real `202`/poll/cancel admission lifecycle;
   `POST .../query-templates/{id}/run`, `POST .../query/batch`, and the MCP
   `run_structured_queries` tool all accepted `async` as a valid
   `queue_mode` value (their field descriptions never said otherwise) and
   simply executed synchronously with no signal that the requested mode
   wasn't honored — `resolve_wait_seconds` treats `async` identically to
   `wait` for the wait-ceiling calculation, so nothing else would have
   caught the silent fallback. Fixed with a shared
   `reject_unsupported_async_queue_mode()` helper
   (`execution/admission.py`) raising `QueryValidationError` (422/VALIDATION)
   on all three surfaces, plus updated field descriptions stating the
   restriction explicitly. Regression-tested on all three
   (`test_batch_query_rejects_async_queue_mode`,
   `test_run_rejects_async_queue_mode`,
   `test_run_structured_queries_rejects_async_queue_mode`).

Both fixes, plus three lower-severity test-hardening items from the same
re-audit (a policy-gate test that now asserts `cancel_session` is never
invoked rather than only catching *some* exception; a new idempotency test
for re-cancelling an already-`cancel_requested` record; and the positive
half of the cross-principal test — a caller *with* `query:cancel` succeeding
where one without it is denied), were each mutation-verified: deliberately
broken, confirmed to fail for the reported reason, then reverted.

### 36. Extensive production-grade QA project / edge-case test suite ✅ DONE

**Phase 1 (policy-cap boundary tests + property-based compiler fuzzing) ✅
DONE.** **Phase 2a (REST/MCP malformed-input fuzzing) ✅ DONE.** **Phase 2b
(cross-dialect differential EXECUTION tests) ✅ DONE** —
`tests/integration/test_cross_dialect_differential.py` (`real_db` +
`postgres_live` + `mssql_live`) executes the same `StructuredQuery`
(select/filter/join/group-by-aggregate) against a live Postgres AND a live MSSQL
seeded with identical demo data and asserts the returned rows are equal, so a
dialect-agnostic query behaves identically end-to-end — going beyond item 78's
compile-time rendering check. Dialect-specific behavior (date bucketing, stddev
naming, string_agg, NULLS ordering) is out of scope (the item-74/78 concern).

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

**Phase 2a shipped:** `tests/security/test_malformed_input_fuzzing.py`
(security-marked, in the default suite) sweeps the input-parsing/schema
boundary of both transports with a broad corpus of malformed-but-plausible
JSON — wrong body/field types, missing/extra fields (including the
no-raw-SQL `sql`/`raw_sql`/`query`/... smuggle set, QG-01), invalid enum
values, invalid/empty JSON, non-finite numbers, and `where` trees deep
enough to trip the JSON parser's recursion guard — and asserts, for REST
(`/query`, `/query/explain`, `/query/batch`) and MCP (`run_structured_queries`
via a real `tools/call`): the input is rejected with a clean client error
(never a 5xx crash), the error body never leaks a server internal (traceback,
file path, driver/SQLAlchemy text, `NoSuchTableError`, or a connection-string
credential — QG-07), and the `StructuredQueryService` execute/explain/batch
methods are patched and asserted *un-called* so malformed input provably
never reaches the database (QG-01). A mirror case proves an extreme-but-valid
literal is accepted (not spuriously size-rejected), so the suite tests
*malformed* shapes, not merely large ones. Policy-cap rejection (over-depth/
size/batch) is intentionally left to phase 1's `test_policy_boundaries.py`.

Two real robustness gaps the suite surfaced (per this item's "where a real
gap is found, fix it or record it — don't leave it silent" posture):

- **Fixed — non-finite numbers 500'd at the REST boundary.** `NaN`/`Infinity`
  (accepted by Python's json parser, not valid JSON) in a numeric field made
  the *validation-error* response fail to encode (Starlette's `JSONResponse`
  renders with `allow_nan=False`) and surface as a 500 — leaking a traceback
  under `debug=True`. `api/_errors.py` now registers a `RequestValidationError`
  handler that scrubs non-finite floats out of the echoed error, so it is a
  clean 422. Adversarially confirmed to 500 before the handler existed.
- **Recorded as residual risk (item 86) — MCP transport 500 on a pathological
  deep body.** REST rejects a `where` nested past the parser's recursion guard
  with a clean 400, but the upstream MCP Streamable-HTTP transport's
  `json.loads` raises `RecursionError`, which it catches and returns as a
  handled JSON-RPC internal-error (`-32603`, generic non-sensitive message) —
  an HTTP 500 rather than a 4xx. Handled and leak-free, so the test asserts
  the security property (handled + no leak + not executed) and item 86 tracks
  the transport-level body-size/depth guard that would make it a 4xx.

**Explicitly out of scope for this pass, tracked as phase 2b:**
cross-dialect differential tests (same AST compiled and *executed* against a
live Postgres and a live MSSQL, asserting equivalent results where the AST
doesn't invoke dialect-specific behavior) — needs the dual-live-DB harness
noted above.

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

### 37. Automated end-to-end proof of adaptive semantic learning ✅ DONE

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

### 38. Admin UI catalog-governance workspace ✅ DONE

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

**Shipped (phase 2):** the five features deliberately deferred out of phase 1,
all still thin wrappers over item 32B's existing governance routes — no new
mutation path, no new scope:

- **Bulk approve/reject(reason)/delete.** Checkboxes per proposal row (a
  `state.selectedProposalIds` `Set`, pruned to whatever survives the active
  filters on every re-render so a hidden proposal can't stay silently
  selected) plus a toolbar calling the existing `bulk-approve`/`bulk-reject`/
  `bulk-delete` REST endpoints, gated on the same `catalog:approve`/`reject`/
  `delete` scopes as the single-proposal actions.
- **Export/import (backup/restore)** for a connection's catalog, calling the
  existing `GET .../export` / `POST .../import` routes — download-a-file /
  upload-a-file, the same pattern `exportChangeSet`/`importChangeSet` already
  use for the shared config change-set, gated on `catalog:export`.
- **`generate-drafts`/`learn` triggers from the browser.** "Run learning" is
  a one-click POST with no body. "Generate drafts…" uploads a JSON file
  that's posted through unmodified — the exact `GenerateDraftsRequest` body
  (`batch`/`purpose`/`max_proposals`) the endpoint already accepts, since a
  real `ManualDraftBatch` needs a schema fingerprint and suggestions no
  browser form can conjure. Both gated on `catalog:generate`.
- **Per-proposal `review_history` detail view.** The one backend addition
  this phase needed: `ProposalDetail(ProposalListItem)` adds a
  `review_history: List[ProposalReviewEvent]` field, returned only by
  `GET .../proposals/{id}` (`get_proposal_endpoint`) — never by the bulk
  `GET .../proposals` list, which stays on the lean `ProposalListItem` shape.
  A list of every proposal's full actor-attributed history is unbounded
  payload no queue-scanning reviewer needs; the single-fetch this backs is
  exactly the request `selectProposal()` and `refreshSelectedProposalAfterMutation()`
  already (re-)issue. `selectProposal()` was changed to always fetch this
  single-proposal endpoint directly (rather than reusing the cached list
  item) so the history renders immediately, not only after the first
  mutation. Regression-tested directly against the model boundary in both
  directions: `test_single_proposal_fetch_includes_review_history_but_list_does_not`
  asserts the field is present after a reject action on the detail fetch and
  absent on the list fetch.
- **Usage-signal browsing.** A new "Usage signals" tab in the Catalog domain
  (`GET .../usage-signals`, already existed, `catalog:review`-scoped) renders
  the bounded, aggregated-by-target `UsageSignalSummaryItem` projection from
  item 32C — kind, object, target, support, signal count, first/last
  observed — the same "metadata projection, never a raw per-signal dump"
  posture as `CatalogVersionSummary`.

Verified during development: `node --check` on the served `app.js`, a
custom HTML tag-balance check on `index.html`, the full unit suite (1763
passed) and non-`real_db` integration suite (295 passed) green, and a new
`tests/integration/test_admin_ui.py` static-shell assertion test
(`test_catalog_governance_phase2_ui_is_wired_and_scope_gated`) confirming the
new markup, endpoints, and scope gates are actually wired into the served
page/script. Unlike phase 1's manual live-server REST exercise, this pass
relied entirely on static checks and the automated suites — no browser tool
was available in this environment to drive the new bulk/export/import/
generate/learn/usage-signal features through a real page the way phase 1's
review/publish/rollback loop was exercised.

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

### 39. Draft-aware policy simulation before staging ✅ DONE

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

### 40. Semantic access diff for config changes ✅ DONE

Phase 1 (connection-baseline semantic diff, `POST /admin/config/diff`) shipped.
**Phase 2 (per-principal resolution) is COVERED by item 41 ph1 (blast-radius)**,
which reuses the exact same `compute_access_diff(principal=...)` engine and
returns each configured principal's full itemized change list in
`principal_impacts[].changes` — the "reporting-agent gains X" statements phase 2
described — plus ranking. A distinct per-principal `/diff` would only duplicate
that. Maintainer decision (2026-07-23): mark phase 2 covered, no new code. See
item 41.

<details><summary>Original phase-1 write-up</summary>

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

</details>

### 41. Policy-change blast-radius analysis ✅ DONE

**Phase 1 (bounded, synchronous aggregation) ✅ DONE.** **Phase 2
(paginated evaluation) ✅ DONE.** Phase 2 took the *paginated-response* option
(the simpler, stateless of the two shapes the spec offered): `compute_blast_radius_report`
+ `POST /admin/config/blast-radius` accept a `principal_offset` cursor and
evaluate one deterministically-sorted **page** of configured principals per
request (page size = the existing `max_principals`), returning `principal_offset`
+ `next_principal_offset` (None on the last page). A deployment with more
principals than one page now covers *every* principal across successive requests
instead of the overflow being dropped as `analysis_incomplete`. `highest_risk`
ranks over the constant baseline + the current page. Covered by
`tests/unit/test_blast_radius.py` (page bounds + next-offset cursor; paging
covers every configured principal). A background-job/polling variant was
deliberately not built — pagination is stateless, needs no job store, and covers
the same "too many principals for one synchronous pass" case.

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

### 42. Four-eyes config approval and separation of duties ✅ DONE

**Phase 1 shipped (the server-side governance model + enforcement):** A staged
config version now carries durable `ConfigApprovalRecord`s
(`admin/models.py`: approver, decision, timestamp, content fingerprint, bounded
note). The store (`admin/store.py add_approval`) enforces the invariants
**server-side, not in the UI**: only a `staged` version can be reviewed, the
version's **author can never approve/reject their own change**, and a reviewer's
latest decision supersedes their own earlier one so *N approvals means N distinct
reviewers*. A new `AppConfig.require_config_approvals` (default `0` =
single-administrator mode, fully backward-compatible; existing manifests missing
the `approvals` field load unchanged) gates `apply()`: a staged version's **first
activation** is refused with `PolicyViolationError` until it has that many valid
approvals (bound to the version's content fingerprint) — rollback is deliberately
exempt. New `admin:config:approve` scope (distinct from `admin:config:write`; a
"Config Approver" role bundle) and REST `POST /admin/config/versions/{id}/approve`
and `/reject` (409 on an author-conflict/not-staged, 403 without the scope). Every
decision + the insufficient-approvals apply-rejection is audited (`approve`/
`reject` actions, content-free). Covered by `tests/unit/test_config_approval.py`
(9 tests: author≠approver, staged-only, distinct-reviewer accounting,
apply-blocked-until-quorum, single-admin backward-compat, endpoint scope/409).

**Phase 2 shipped (CLI + admin-UI review flows):** `querygate-config` (new
`config_cli.py`, an authenticated thin HTTP client over `/admin/config/*`) adds
`versions` (list with approval status), `approve <id>`, and `reject <id>` — the
caller's own bearer token authenticates the reviewer, so the server enforces the
same scope/author≠approver/audit; no new authority. The admin-UI Releases view
now shows each staged version's approval status (reviewers + decisions) and,
for an `admin:config:approve` holder, Approve/Reject buttons that call the
endpoints (`app.js` `canApprove`/`approvalSummary`/`reviewDecision`); the
author's own buttons are disabled client-side as a hint, with server enforcement
unchanged. "Never simulate four-eyes in the browser while the server permits
self-approval" holds by construction — enforcement is server-side (ph1).
Covered by `tests/unit/test_config_cli.py` (6: request shape, approval-status
render, 403/409 surfacing) and `tests/integration/test_admin_ui.py`
(`test_four_eyes_review_ui_is_wired_and_scope_gated`).

**Original scope (for reference):** governance-model and authorization change —
new durable states, reviewer records, scopes, invariants, audit actions,
concurrency handling, REST/CLI/UI flows, and migration/backward-compatibility for
existing staged versions.

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

### 44. Admin observability and rejection-trend dashboard ✅ DONE

**Shipped (phase 1 — the aggregation API plus a read-only browser panel):** A
new `admin:observability:read`-scoped `GET
/api/v1/admin/observability/overview` (`api/admin_observability_routes.py`)
returning a typed, redaction-safe `ObservabilityOverview`
(`admin/observability.py`) aggregated from the *existing* in-process Prometheus
registry (`metrics.py`) — query volume, success/rejection categories, average
duration, queue depth + wait-by-outcome, concurrency in-use/max/utilization,
per-principal quota rejections by kind, and cost-estimation attempts/
unavailable/would-reject with a derived `fail_open_rate` — both as a global
rollup and a per-connection breakdown.

The `/admin/` control plane renders it as an "Observability" section:
overview cards (queries, top reject reason, avg duration, concurrency
utilization, queue depth, cost-estimate fail-open rate), a per-connection
table, and a banner echoing the snapshot's `note`/`since` so the honesty
about durability is visible in the UI, not just the JSON. The panel calls the
same scoped endpoint and shows an explicit "connect with
admin:observability:read" empty state without it.

The aggregator (`build_overview(registry)`) is pure over the registry it reads,
so it's unit-tested against a fresh `CollectorRegistry`; the endpoint is
integration-tested for scope enforcement (403 without the scope), honest
snapshot labeling, real-activity reflection, and low-cardinality-only output;
the panel is asserted in the admin-UI static-shell test.

**Honesty about durability (item 44's explicit requirement):** the response is
labeled `source="process_snapshot"`, `durable=false`, `since=<process start>`,
with a `note` stating counters are cumulative-since-start, gauges are
instantaneous, and — under the default in-process backends — everything is
per-replica. It never implies a durable time-series store QueryGate does not
own. Its own least-privilege scope (distinct from config/connection scopes)
gates the whole overview, including the per-connection breakdown; output is
built only from already-public, low-cardinality metric labels (never a query,
value, principal, table, or column).

**Phase 2 slice shipped (2026-07-29) — config/catalog-change trend card:** a
third read on the same router, `GET /api/v1/admin/observability/config-changes`
(`admin/config_trends.py`), following item 59's `AuditEventSource`-protocol
shape (`ChangeEventSource`/`JsonlChangeEventSource`) rather than item 44 phase
1's Prometheus-registry read. Unlike phase 1's process snapshot, the audit
JSONL stream is durable, so this is a real recent-vs-baseline rate comparison
(same two-window shape as item 59's per-principal anomaly detection, applied
fleet-wide to `ConfigChangeEvent`/`CatalogGovernanceEvent` volume and
by-action/outcome breakdown) rather than a since-process-start counter. Bounded
by a configurable scan cap, honestly reports `source="disabled"` without the
JSONL sink, and carries only action names/outcomes/counts — never version
content, proposal text, or raw YAML. Rendered as a "Change velocity"
subsection in the admin UI's Observability panel.

**Phase 2 remainder shipped (2026-07-30) — external metrics backend + real
trend charts:** a fourth read, `GET /api/v1/admin/observability/history`
(`admin/metrics_history.py`), closes both items the phase-2 slice above
deliberately deferred. `MetricsHistorySource` is a narrow read-only Protocol
(the same composable-interface shape as `SecretResolver`/`DialectAdapter`/
`AuditSink`) with one concrete backend today, `PrometheusMetricsHistorySource`
— it queries an **operator-configured** Prometheus-compatible HTTP API (one
already scraping this deployment's own `/metrics`) for five fixed named
series (successful/rejected query rate, avg duration, queue depth,
concurrency utilization), each a fleet-wide PromQL aggregate over QueryGate's
own already-public metric labels — QueryGate only ever reads from this
backend, never writes to it, and no caller-influenced query ever reaches it.
With no backend configured (the default, `METRICS_HISTORY_BACKEND=none`) the
endpoint honestly reports `source="disabled"`; an unreachable/erroring backend
is likewise reported as `backend_error` rather than 5xx-ing the dashboard.
Points-per-series are bounded by construction (a too-fine step for the
configured window is widened, never the window truncated), mirroring
`config_trends.py`'s scan cap. Rendered as a "Trend charts" subsection in the
admin UI's Observability panel — a dependency-free inline-SVG sparkline per
series (no charting library), consistent with the rest of the control plane.

**Why it matters:** Item 31 can browse individual audit events, but it cannot
answer operational questions such as “Which policies reject the most
requests?”, “Is queue pressure rising?”, or “Did cost-estimation availability
regress?” Those trends are what let an administrator tune policy and capacity
proactively — and phase 1 answers them now over the API, honestly scoped to
what a single process can truthfully report.

### 45. Dedicated non-admin "My access" portal ✅ DONE

**Shipped:** A separate, dependency-free `/access/` static page
(`querygate/access_ui/`), mounted and CSP/security-header-protected the same
way `/admin/` is (`api/app.py`), showing the caller's identity/auth method/
scopes/capabilities, visible connections, effective per-connection query
guardrails, and mandatory row-filter claim readiness — plus a policy-filtered
schema browser reusing the existing `list_tables`/`describe_table` REST
endpoints unchanged. No new query/schema code path: the page authenticates
with the caller's own token and calls the same principal-scoped endpoints
that caller already has.

The one new backend surface is additive to the existing `AccessSummary`
model/`GET /api/v1/help/my-access` endpoint (also the MCP
`describe_my_querygate_access` tool, which returns the same model): a new
`connection_access` field lists, per visible connection, `EffectiveGuardrails`
and `MandatoryFilterReadiness` — reusing the exact typed models item 39's
candidate-policy simulation already built, now applied to the caller's own
active policy instead of an uncommitted candidate, and across every
mandatory filter on a policy-visible table rather than one requested table.
Never a filter/claim *value* — only table/column/claim-name/source/
readiness, matching that existing redaction posture. A mandatory filter on a
table the caller's policy denies is excluded entirely (mirrors QG-19's
"don't leak hidden-table filter metadata" reasoning).

Verified per-principal, not just for one caller: two JWTs with different
`sub` claims and a `principals:` policy override see different effective
`max_joins` and different mandatory-filter claim readiness through the same
`GET /help/my-access` call. See `tests/unit/test_product_guide.py`,
`tests/integration/test_product_guide_api.py`, and the new
`tests/integration/test_access_ui.py` (static-shell security headers, plus an
explicit assertion that no admin-only nav/action/endpoint string ever
appears in the shipped shell or script).

**Phase 2 shipped (2026-07-30) — safe explanations of recent personal
denials:** `GET /api/v1/help/my-recent-denials` (`api/help_routes.py`,
`help/personal_denials.py`), requiring only authentication like `/help/
my-access` — no admin scope. Reuses item 59's already-tested
`JsonlAuditEventSource` (`admin/anomaly.py`) to read the persisted
`query.execution` audit stream rather than writing a third file-parsing
implementation, then applies a pure, principal-scoped filter
(`select_recent_denials`): only the caller's own `outcome="rejected"` events,
most recent first, capped by `AppConfig.personal_denials_limit`. Each denial
carries only `occurred_at`/`connection`/`surface`/a stable `reason` label
(drawn from `AuditEvent.error_category`:
`policy`/`schema`/`quota`/`cost_estimate`/`concurrency`/`queue_full`/
`approval_required`/`not_found`/`db_error`) and one fixed, human-readable
explanation per category — never `query_shape`, another principal's activity,
or a table/column identifier beyond the category name. The report's own
`own_denials_found`/`truncated` counters are themselves caller-scoped, not a
fleet-wide scan total, even though the underlying reader scans every
principal's events — a distinction that matters here specifically because,
unlike item 59's admin-scoped anomaly report, this endpoint carries no scope
requirement at all, so a raw scan-wide count would leak cross-principal audit
volume to any authenticated caller (caught and fixed via `auditors` review
before this shipped). Honestly reports `source="disabled"` without the JSONL
sink. Verified end-to-end with two JWTs (distinct `sub` claims, since a
single API-key list maps to one shared subject): each principal sees only
their own denial, proven both by exact list contents and by asserting the
other principal's connection id/subject never appears anywhere in the
response body (`tests/integration/test_personal_denials_api.py`). Surfaced as
a "Recent denials" panel on the existing `/access/` portal.

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

### 46. Validated policy templates and safe-start presets ✅ DONE

**Shipped:** Five fixed, code-reviewed presets (`querygate/admin/templates.py`):
`deny-by-default`, `reporting-only`, `customer-support`, `tenant-isolated`,
`bounded-analytics`. Each declares a small typed parameter list
(`TemplateParameter`) and a pure `build(params) -> (scope, patch, rules)`
function — no live schema/table inference, no credential or tenant value
ever hardcoded. `GET /api/v1/admin/config/templates` lists them
(`admin:config:read`); `POST /api/v1/admin/config/templates/render` renders
one against the caller's own supplied `policy_yaml` draft
(`admin:config:write`, matching `/validate`/`/preview`'s posture since it
resolves caller-supplied content bound for staging) and returns the merged
document plus a plain-English `rules` preview — nothing is persisted, and
the live registry/policy singletons and version store are never touched.

The merge (`render_template`) is monotonically restrictive, not a blind
overwrite: numeric guardrail caps the templates set
(`max_joins`/`max_where_depth`/`max_limit`/`max_limit_aggregate`/
`timeout_seconds`) take `min(existing, template)`; `allowed_tables` only
narrows an existing non-empty allow-list (never widens it) and only adopts
the template's list outright when nothing was previously restricted;
`denied_columns` and `mandatory_row_filters` union onto what's already
there (append-only, keyed by table/column so re-applying a template is
idempotent) rather than replacing it. Every case where the merge kept an
existing, stricter value instead of the template's own is called out by
name in the returned `rules` list, not silently absorbed. A rendered
document is not a special code path — it is plain `policy_yaml` text
verified to flow through the exact same `/validate` and `/versions` (stage)
endpoints as a hand-edited draft (`test_render_template_end_to_end_through_validate_and_stage`).

The browser control plane's Policy designer (item 31) gained a "Safe-start
templates" panel: a template picker, a dynamically rendered parameter form,
"Preview template" (calls `/render` against the current local draft and
shows the rules list), and "Apply to draft" (writes the rendered document
into the existing draft/validate/stage flow — no new mutation surface).

See `tests/unit/test_policy_templates.py` (rendering, restrictive-merge,
idempotency, and no-credential/no-secret-embedding coverage) and the
template-endpoint tests added to `tests/integration/test_admin_config_governance.py`
and `tests/integration/test_admin_ui.py`.

**Deliberately out of scope for this pass:** the item's "keep generated YAML
fully editable/exportable for infrastructure-as-code users" is satisfied
structurally (the render output is ordinary `policy_yaml` text, not a
managed/opaque artifact) rather than by adding a separate export format.

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

### 47. Safe draft recovery plus config export/import UX ✅ DONE

**Phase 1 shipped:** a portable *change-set bundle* built entirely on item
25's existing governance plane (`admin/service.py`), never a shadow store.

- **Model:** `ConfigChangeSetBundle` (`admin/models.py`,
  `bundle_format="querygate.config-change-set/1"`) carries only the documents
  an admin actually submitted (a change set, not a full snapshot), plus the id
  and a sha256 content `base_fingerprint` of the base version those deltas were
  composed against, plus a description. A `connections` document may
  legitimately be present (the caller's own submitted content, on an explicit
  download), which is why `contains_connections` is surfaced.
- **Export** (`POST /api/v1/admin/config/export`, `export_change_set`) echoes
  **only** the caller-submitted deltas — an unset document is never resolved
  into the bundle — so it can never disclose the active connections/policy
  content. `admin:config:write` scoped, like `/preview` and `/versions`.
- **Import** (`POST /api/v1/admin/config/import`, `import_change_set`) is
  validation-only: it re-validates the resolved candidate through the same
  loaders `/validate` uses, detects a **stale base** via fingerprint
  (`stale_base` + the specific `base_conflict_documents` that moved), enforces
  `AppConfig.config_bundle_max_bytes` (default 1 MiB → a clean validation
  failure, never OOM), and returns a **content-free** change signal. It never
  stages or persists — staging still goes through the unchanged `/versions`
  endpoint, so there is one governed mutation path.
- **UI** (`admin_ui/`, Releases → Change set): Export/Import buttons wired to
  those endpoints (import fills the draft editors from the locally-held bundle
  and warns on drift), plus tab-scoped `localStorage` recovery of an
  in-progress **policy** draft. Only the policy document is ever written to
  browser storage; `connections.yaml` (credentials), secret references, and
  bearer tokens never are — full-config recovery uses the downloaded file.
- Audited as `export`/`import` `config.governance` actions
  (`audit/events.py`); documented as **QG-30** in `docs/THREAT_MODEL.md`.

Covered by `tests/unit/test_config_change_set.py` (11 cases: delta selection,
no-active-disclosure, fingerprint stale-base, oversized rejection,
missing-fingerprint warning, connections flag, import-never-persists),
`tests/integration/test_admin_config_governance.py` (REST round-trip → stage,
stale-base after the active moves, oversized rejection),
`tests/security/test_adversarial_security.py` (export/import require write
scope), and `tests/integration/test_admin_ui.py` (export/import shell +
policy-only-localStorage invariant).

**Phase 2 shipped (2026-07-30) — server-side encrypted-at-rest draft store:**
`admin/draft_store.py`'s `DraftStore` persists the exact same
`ConfigChangeSetBundle` phase 1 downloads, Fernet-encrypted at rest (a key
derived via SHA-256 from `AppConfig.draft_store_encryption_key`, matching the
plain-string-secret posture `audit_ledger_hmac_key`/`approval_token_hmac_key`
already established — no pre-formatted base64 key required from the
operator). `POST/GET/GET-by-id/DELETE /api/v1/admin/config/drafts[/{id}]`
(`api/admin_config_routes.py`) save/list/load/delete a caller's own drafts;
listing is metadata-only (`DraftSummary` — id/description/timestamps/
`contains_connections`, `admin:config:read`) while save/load/delete touch
actual document content and require `admin:config:write`, the same split
`/versions` already uses. Ownership is enforced per-principal: loading or
deleting another principal's draft raises the same `NotFoundError` as an
unknown id, so the endpoint can never become a draft-id enumeration or
existence oracle. **A residual `auditors` review surfaced and this pass
documents rather than silently leaving untested:** isolation is only as
fine-grained as the deployment's identity model — under static `api_keys`
every configured key shares one `api_key_subject`, so two admins each holding
their own key see and can delete each other's drafts (the same limitation
item 45's `/help/my-recent-denials` already has); JWT auth is required for
real per-admin isolation, proven by a dedicated regression test. Retention is
opportunistic (no cron): every save/list call
prunes expired drafts first (`AppConfig.draft_store_retention_seconds`,
default 7 days), and `AppConfig.draft_store_max_drafts_per_principal`
(default 20) bounds storage per admin — over the cap must delete before
saving again, rather than one draft silently evicting another. The subsystem
fails closed: an empty encryption key means every draft endpoint reports a
clean `503`, never writing plaintext. New audited actions
`save_draft`/`load_draft`/`delete_draft` (`ConfigChangeEvent.draft_id`) —
never the draft's document content, matching `export`/`import`'s existing
redaction posture. No new config-mutation path: a loaded draft still flows
through the unchanged validate/stage/apply plane. Surfaced as a "Saved
drafts" panel alongside the phase-1 export/import buttons in the admin UI.

Covered by `tests/unit/test_draft_store.py` (23 cases: encrypted round-trip,
plaintext-never-on-disk, cross-principal isolation on load/delete, per-
principal listing, retention/expiry + pruning, the per-principal cap, wrong-
key decryption failure, disabled-store handling, and the service-layer
audit-wrapped functions), `tests/integration/test_draft_store_api.py` (9
cases: scope/auth enforcement, full REST round-trip, 404s, 503-when-disabled,
the cap enforced over REST, a two-JWT cross-principal isolation proof
mirroring item 45's, and an audit-stream assertion proving `save_draft`/
`load_draft` events never carry document content), and
`tests/security/test_adversarial_security.py` (drafts folded into the
existing config-governance read/write scope-separation tests).

**Effort: M (2–3 days).** Basic download/upload is small, but safe recovery
must handle sensitive connection documents, version/fingerprint metadata,
schema validation, stale-base conflicts, size limits, and browser-storage
rules without creating an ungoverned shadow config store.

**Why it matters:** Item 31 warns before abandoning an in-memory draft, but a
tab crash or browser restart still loses work. Administrators also need a
convenient way to move a reviewed change between environments while preserving
the YAML/CLI path rather than copying text fields by hand.

### 48. Pre-defined, admin-approved query templates ("Toolbox"-style curated tools) ✅ DONE

**Phase 1 (file-configured, invocable templates) ✅ DONE. Phase 2 (governed
create/edit/publish/rollback) ✅ DONE — via the item 25 config-versioning
plane, not 32B's per-entry proposal state machine (a deliberate governance-
model decision, see below).**

**Phase 1 shipped:** A `querygate/templates/` module adds a `QueryTemplate`
model — a named, parameterized `StructuredQuery` *skeleton* (a raw dict with
`{param: name}` placeholders in value positions) plus typed parameter slots
(`type`/`required`/`default`/`min`/`max`/`max_length`/`allowed_values`/
`is_list`) — loaded from an optional `TEMPLATES_FILE` into a `TemplateStore`
singleton that mirrors `CatalogStore` (hot-reloadable via the existing
config-reload; validated by `querygate-validate-config --template-file`; cross-
checked so every template targets a real connection id). At invocation
(`templates/binding.py`) the caller's parameters are type/constraint-checked
against the slots, substituted into the skeleton, and the **result validated as
a real `StructuredQuery`** and run through the *unchanged*
`StructuredQueryService` — so a bound template inherits every policy cap,
allow/deny list, mandatory row filter, schema check, and guardrail an ad-hoc
query has, and a parameter can never smuggle SQL (there is no SQL) or exceed
policy (`tests/security/test_adversarial_security.py::
test_query_template_cannot_exceed_policy`). Surface:
`GET /api/v1/query-templates` + `POST /api/v1/query-templates/{id}/run` (REST),
`list_query_templates`/`run_query_template` (MCP), and a read-only "Query
templates" browse panel in the `/admin/` control plane — all filtered
per-principal by target-connection visibility (item 22) — an unknown template
and one on a hidden connection return the same non-enumerating 404. Invocations
audit distinctly (`operation="run_query_template"`, `template_id`, and the
parameter *names* — never values). Documented as QG-26 in
`docs/THREAT_MODEL.md`. Covered by `tests/unit/test_query_templates.py`,
`tests/integration/test_query_template_api.py`, `tests/integration/test_admin_ui.py`,
plus MCP registration + adversarial tests.

**Phase 2 shipped — governed authoring via the config-versioning plane
(item 25), not 32B:** `templates.yaml` became a fourth governed configuration
document alongside `connections.yaml`/`policy.yaml`/`catalog.yaml`. It is now
carried through the entire `ConfigVersionStore` snapshot lifecycle and the
`/api/v1/admin/config/*` governance API — `validate`, `preview` (adds a
`templates` document-change signal), `versions` (stage), `versions/{id}/apply`,
and rollback — exactly the way an optional `catalog.yaml` already was. A
version now snapshots its own `templates.yaml`; `apply`/`rollback` reload the
`TemplateStore` from that snapshot (a pre-phase-2 version with no snapshot
falls back to the deployment's static `template_file` rather than clobbering
it to empty). Templates are validated together with the rest of the config
both at stage and — crucially — re-validated at apply (QG-15's posture, now
covering templates: a version whose template stopped validating can't be
silently activated). The admin config editor gained a `templates.yaml` pane
(a fourth document tab) reusing the existing validate → preview → stage →
apply → rollback UI with no new mutation path.

**Why the config-versioning plane and not 32B's proposal state machine:** the
item's original phase-2 sketch said "route through 32B's proposal state
machine," but query templates are a configuration *document* loaded by
`config_reload` alongside connections/policy/catalog — not catalog *entries*
(which carry sensitivity/confidence/provenance/relationship-target fields that
the 32B model is built around). Routing a config document through the
catalog-entry proposal machinery would have been a second, awkward mutation
path — exactly what this repo's standing rule forbids. The config-versioning
plane already *is* "governed create/edit/publish/rollback" for config
documents (whole-document staging, validation, atomic apply, rollback, audit,
no self-publish), so templates joined it as one more document. This satisfies
the item's "why it matters" — a reviewed, versioned, rollbackable finite set
of query shapes that non-technical stakeholders can sign off on — with zero
new governance machinery. A finer-grained *per-template* proposal/approval
workflow (individual sign-off with separation of duties, mirroring catalog
governance) was consciously **not** built: it's a possible future nicety, not
required for governed authoring, and would be the "Option A" alternative in
this pass's Decision Log entry.

**Coverage (phase 2):** `tests/unit/test_admin_store.py` (templates snapshot
round-trips and appears in `file_paths`; a template-free version has none),
`tests/unit/test_admin_service.py` (preview reports the `templates` document
change), `tests/integration/test_admin_config_governance.py` (a template
authored through stage→apply becomes invocable on `/query-templates` and a
rollback removes it; a malformed template — e.g. targeting a nonexistent
connection — is rejected at stage and never persisted; a security-marked test
that a *staged* template is not live until a separate apply, proving no
self-publish), and `tests/integration/test_admin_ui.py` (the `templates.yaml`
config-editor tab is served). `docs/THREAT_MODEL.md` QG-10 now lists templates
among the governed documents.

**Why it matters:** Google's Gen AI Toolbox for Databases popularized a
pattern this project's own model is a natural fit for: instead of (or in
addition to) letting an agent compose an arbitrary `StructuredQuery` within
policy caps, an admin pre-defines a fixed set of named, parameterized queries
and agents only ever call one of *those* by name with typed parameters. This
shrinks the effective attack/error surface to a reviewed, finite set of query
shapes, gives non-technical stakeholders something concrete to sign off on,
and matches how teams already think about tool-calling for agents. It is
additive to QueryGate's existing guarantee, not a new one: a template is just
a stored, named, parameterized `StructuredQuery` AST, so it inherits every
validation and guardrail already built for ad-hoc queries — and phase 2 makes
authoring it a governed, versioned, rollbackable change rather than a
hand-edited file.

### 49. Column-value masking/tokenization (not just allow/deny) ✅ DONE

**Shipped:** a `column_mask` policy primitive (`policy/models.py`:
`ColumnMask`/`ColumnMaskKind`, field `Policy.column_masks` keyed by table with
`"*"` wildcard, resolver `Policy.column_mask`) supporting `hash`, `null`,
`last` (reveal trailing N chars), and `bucket` (round down to a width). It
resolves per principal through the existing `PolicyStore` merge — no loader
change — so one caller sees raw values and another sees them masked on the
same connection. The transform is applied **in the compiled `Select`**: the
bare-projection branch of `_build_select_columns` wraps the column via a new
`DialectAdapter.column_mask` method (Postgres `md5`/`right`, MSSQL
`HASHBYTES`/`RIGHT`, SQLite `substr`; `null`/`bucket` are dialect-universal;
SQLite `hash` rejects like `array_agg` since SQLite has no hash builtin),
labeled with the original output name so the response shape is unchanged.

**Masking scope (Decision Log, docs/PRODUCT_GUIDE.md):** a masked column may
appear **only as a bare `select` item**. Any use in a
`where`/`join`/`order_by`/`group_by` position, or nested inside a
function/CASE/aggregate, is **rejected** by `policy_validation.py`
(`_non_projection_column_refs`) — masking-in-place would silently change query
semantics and projection-only masking would leave an inference exfiltration
channel (`where ssn = 'guess'` + observe row presence), the same side-channel
the allow/deny walk already closes for denied columns. This is the
"reject and name the gap" posture, consistent with item 74 / `array_agg`.

**Audit:** the success `AuditEvent` carries `masked_columns` (output names
only, never the pre-mask value; `compiler.applied_column_masks`), so operators
distinguish "masked" from "denied" access in the one stream (item 23).

**Coverage:** `tests/unit/test_column_masking.py` (policy resolution +
precedence + per-principal override, validation rejection in every
non-projection position, compiler output-name preservation, per-dialect
rendering, SQLite end-to-end for null/last/bucket, audit `masked_columns`),
plus an example block in `examples/policy.example.yaml`.

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

### 50. Per-principal rate limits / query quotas over time ✅ DONE

**Phase 2 shipped (Redis cross-replica quota):** `execution/redis_quota.py`'s
`RedisQuotaLimiter` makes a principal's request/byte rolling-window budget a
single **shared** budget across replicas, closing the per-replica-multiplication
gap phase 1 flagged (and that `deploy/HA_DR.md`'s shared-state matrix called
out). Mirrors `redis_concurrency.py`: a per-(connection, principal) sorted set
scored by wall-clock time + a parallel bytes hash, one atomic Lua script that
prunes aged entries, checks the request-count and byte-total caps against the
true cross-replica window, and records the attempt; `record_bytes` fills in the
response size afterward (guarded so a late write can't resurrect a pruned entry);
both keys carry a window-length TTL. The `QuotaLimiter` protocol (and
`enforce_query_quota`/`record_query_quota_bytes`) went **async** so the Redis
backend can await its client; the in-process limiter is the unchanged default.
`create_app` installs it when `concurrency_backend=redis` (same client as the
concurrency limiter). Tested with fakeredis (`tests/unit/test_redis_quota.py`:
caps, rolling expiry, per-key isolation, record_bytes, and — standing in for
cross-replica — two limiter instances sharing one Redis enforcing one budget).

**Shipped (phase 1 — in-process rolling-window quota):** three `Policy`
fields (`max_requests_per_window`, `max_response_bytes_per_window`,
`quota_window_seconds`; both caps unset = disabled, identical to prior
behavior), resolved per principal through the existing `PolicyStore` merge —
so a per-principal `principals:` override can throttle one noisy caller
without a code change. Enforcement is a new `execution/quota.py` with a
narrow `QuotaLimiter` Protocol (CLAUDE.md "Composable single-purpose
interfaces") and one `InProcessQuotaLimiter` today: a sliding-window log
keyed by `(connection_id, principal_subject)`. `StructuredQueryService.execute`
calls `enforce_query_quota` **before** it queues or opens a DB session
(`reserve()` atomically prunes the window, checks both caps, and records the
attempt so concurrent in-flight callers can't race past the cap), and
`record_query_quota_bytes` attributes the response size afterward (the query
that crosses the byte ceiling completes; the next one is refused). A rejected
caller raises `QuotaExceededError` (a `PolicyViolationError`, so every
existing deny-path handler and `public_error_message` treat it as
client-actionable) and never touches the database.

Distinct, contextful rejection (not a bare 429): REST maps it to **429 with a
`Retry-After` header** (`api/_errors.py`), MCP to a **`RATE_LIMITED`** error
code (`mcp/exceptions.py`) — both carrying a message that names the cap and a
retry hint. Audited exactly like other policy denials (`policy_decision:
denied`, `error_category: quota`), with a dedicated `quota` reason in
`metrics.classify_rejection`/`querygate_queries_rejected_total` plus a
`querygate_query_quota_rejections_total{connection,quota_kind}` counter
breaking out requests-vs-bytes. `explain()` is deliberately not quota-gated
(it compiles a preview and never executes — same reason it skips
cost-estimation).

**Coverage:** `tests/unit/test_query_quota.py` (window semantics, per-
principal/per-connection isolation, byte accounting, policy validation,
classification, REST-429/MCP mapping), `tests/integration/test_query_quota_e2e.py`
(real execute pipeline against SQLite: request cap, per-principal isolation,
byte cap, unauthenticated skip, metric increment), and a
`tests/security/test_adversarial_security.py` case proving a quota-rejected
attempt is refused strictly before schema validation / the engine.

**Phase 2 — Redis-backed cross-replica quota (NOT STARTED):** the in-process
window is per-replica, so under a load balancer the effective quota is
multiplied by instance count — exactly the caveat the default in-process
concurrency limiter carries (see `execution/redis_concurrency.py`). Closing
it means a `RedisQuotaLimiter` implementing the same `QuotaLimiter` Protocol
(a per-key sorted set of attempt timestamps + byte weights, pruned by a
single atomic Lua `ZREMRANGEBYSCORE`/`ZADD` script — the exact shape item 9's
`RedisConcurrencyLimiter` already uses), selected by the same
`concurrency_backend`/startup swap `init_redis_limiter` uses, plus a
live-Redis integration gate. Split out because the in-process quota is a
complete, shippable guardrail for single-instance deployments on its own, and
the cross-replica variant needs the real-Redis test infrastructure item 9
established — the same phasing precedent as item 35 phase 2.

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

### 52. Multi-framework agent integration examples (LangChain, LlamaIndex, OpenAI function-calling) ✅ DONE

**Shipped:** three runnable integration examples mirroring item 20's Claude
Agent SDK script — `examples/langchain_integration.py` (LangChain / LangGraph
via `langchain-mcp-adapters`), `examples/llamaindex_integration.py` (LlamaIndex
via `llama-index-tools-mcp`), and `examples/openai_function_calling_integration.py`
(OpenAI Chat Completions function-calling, bridging QueryGate's MCP tool schemas
into OpenAI function schemas with a pure `mcp_tool_to_openai_function` helper,
using the already-bundled `mcp` client SDK for discovery/dispatch). No new
runtime dependency was added — the frameworks are user-installed exactly like
`claude-agent-sdk`, and each example imports its framework lazily so its
`QUERYGATE_TOOLS` allow-list stays importable without the framework present.

Following item 20's verification bar (mechanically verify what can be verified
without a live model call; state plainly what wasn't): a model-free
`tests/integration/test_integration_examples.py` asserts, against the live MCP
server's `tools/list`, that every tool each example wires up is really
registered (drift guard), and that `mcp_tool_to_openai_function` turns
QueryGate's real tool schemas into well-formed OpenAI function schemas. The
framework agent loops and the live model calls are explicitly *not* exercised in
CI. No maintained framework-specific SDK layer was added, per item 20's warning.
README's integration section and `docs/PRODUCT_GUIDE.md`'s MCP-transport section
were updated to list all four examples.

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

### 54. Compliance control mapping (SOC 2 / ISO 27001 readiness) ✅ DONE

**Effort: L (mostly documentation and gap analysis).** Coordination-gated: an
agent can produce the mapping + gap analysis (done here); the independent audit
engagement (item 53) and org-level process controls are the human/vendor
remainder, flagged explicitly in the deliverable.

**Why it mattered:** regulated-industry buyers ask "where's your SOC 2" as a
gating question before evaluating architecture. QueryGate already has most of
the underlying controls; this item maps what's built to a recognized framework
rather than building new security features.

**What shipped.** `docs/COMPLIANCE_MAPPING.md` — a control-by-control map to:

- **SOC 2 Common Criteria CC1–CC9** plus the Confidentiality, Availability, and
  Processing-Integrity series, each row citing a concrete artifact (e.g. CC6.7
  → `PublicConnectionInfo` + `test_credential_redaction.py`; CC6.8 → cosign/SLSA
  `release.yml` + `make verify-release`; CC7.3 → `audit/ledger.py` +
  `querygate-audit verify`; CC7.5/A1 → `deploy/HA_DR.md`; PI1 → the validated-AST
  pipeline + property-based compiler fuzzing).
- **ISO/IEC 27001:2022 Annex A** cross-reference for the key domains (access
  control, logging, cryptography, secure coding, vulnerability management).

Every "Product-provided" row is grounded in a real file/test/CI gate (verified
to exist before writing). The doc draws an explicit scope boundary —
product-provided vs. shared-responsibility vs. customer/organization — because
QueryGate is a self-hosted *component*, not a certified SaaS, so it never claims
to "be SOC 2 certified"; it maps which controls it *evidences*. Cross-linked
from `docs/SECURITY_POSTURE.md`'s External attestations section.

**Honest gap analysis (real gaps, not theater):** the audit engagement itself
(item 53), organizational controls (HR/physical/IR-process/vendor-management/
access-review cadence), access-review evidence formalization, and the
not-yet-shipped config separation-of-duties enhancements (items 39–42, correctly
listed as roadmap not as existing controls). No new product code was added
because the real gaps are organizational, not code — closing them with product
features would have been the process theater the item warns against.

### 55. Inference/transitive-exposure adversarial test suite ✅ DONE

**Shipped:** A new design note (`docs/INFERENCE_RISKS.md`) enumerating
inference-attack shapes against the `StructuredQuery` AST, plus adversarial
regression cases added to item 28's suite
(`tests/security/test_adversarial_security.py`).

The investigation found **no enforcement gap**: the policy column walk
(`validation/policy_validation.py`'s `_iter_column_refs` + the shared
`select_item_column_refs`/`predicate_column_refs` harvesters) already checks
every column reference in every clause, and the AST forbids nested scalar
functions, so there is no expression tree a column can hide inside. The value
of this item is therefore (a) proving that exhaustively and (b) documenting the
residual risks that identifier allow/deny structurally *cannot* close.

- **Class A — direct reference in any clause (closed, regression-locked):**
  `test_denied_column_cannot_be_used_for_inference` is now parametrized across
  every column-carrying AST position — where/group_by/having/order_by/top_n
  (partition_by + order_by)/join `on`, plus scalar-function args, `CASE`
  when/then/else, aggregate/`percentile_cont`/`string_agg` columns, predicate
  `col_fn` and `value_col`, and composite join `extra_on` keys — each asserting
  a denied column is rejected. Adding a new column-carrying AST node without
  extending the harvest fails this test.
- **Class B — residual risks (documented, not closable by allow/deny):** R1
  derived/correlated permitted columns (closed by *policy* — deny the derived
  column too), R2 underlying-data correlation (out of scope for an access
  gateway), R3 aggregate differencing / no minimum group size (accepted v1
  residual; a scoped candidate `min_group_size` guardrail is noted, not
  half-built), R4 existence/row-count probing (accepted, mitigated in depth by
  mandatory row filters, masking, quotas, and audit). R1 and R3 each carry a
  demonstrating test asserting the current allowed-by-design behavior, so the
  boundary is explicit and flips the day a closing feature lands.

**Why it matters:** Column allow/deny stops a query from directly selecting
a denied column, but "provably does not leak it *indirectly*" was previously
asserted only for a handful of clauses. This item makes that guarantee
exhaustive and regression-locked, and draws the honest line between what the
engine closes and what remains a policy-configuration or accepted residual
risk — rather than leaving the inference category silently unaddressed.

### 56. HA / multi-region reference deployment + DR runbook ✅ DONE

**Effort: L (3–5 days).** Built on item 29's reference stack and item 9's
cross-instance concurrency state; the new work was failover behavior and a
documented recovery procedure, not a new deployment topology from scratch.

**Why it mattered:** `docs/business/GO_TO_MARKET.md` explicitly said not to
claim "a production Helm/Kubernetes reference deployment" yet. Item 29's
reference stack is not the same claim as proven multi-instance failover —
enterprise buyers evaluating this for production traffic ask for an HA/DR story
specifically, not just a docker-compose file or a single Helm chart.

**What shipped.**

- **Zero-downtime rollouts + all-replica config reload.** The Helm
  `deployment.yaml` now sets a configurable `updateStrategy`
  (`RollingUpdate`, default `maxUnavailable: 0` / `maxSurge: 1`) and stamps a
  `checksum/config` annotation derived from the rendered ConfigMap onto the pod
  template. A `helm upgrade` that changes connections/policy/catalog therefore
  rolls **every** replica one at a time behind the readiness gate — the
  multi-replica-correct, zero-downtime config-reload path (item 5 + item 56).
  This closes the real HA gap that `admin/service.py`'s `apply()` reloads only
  the single replica that served the request (no cross-replica broadcast).
- **Multi-zone overlay** `deploy/helm/querygate/values-ha.yaml`: autoscaling
  floor 3, a PodDisruptionBudget, `topologySpreadConstraints` across
  `topology.kubernetes.io/zone` and `kubernetes.io/hostname`, Redis-backed
  concurrency kept on, and the chained audit sink selected.
- **Optional shared config-governance PVC** (`configGovernance.enabled`,
  `templates/configgovernance-pvc.yaml`) — ReadWriteMany, so the governance API
  (Path B) has one shared version history across replicas when needed; off by
  default because GitOps/ConfigMap (Path A) is the recommended multi-replica
  path and needs no shared volume.
- **`deploy/HA_DR.md`** — the shared-state correctness matrix (concurrency is a
  true shared cap under `CONCURRENCY_BACKEND=redis`; **per-principal quota is
  still per-replica** until item 50 phase 2 — effective budget multiplies by
  replica count; config-governance and persisted audit are per-replica unless
  deliberately shared), the zero-downtime config-reload contract, multi-zone and
  active/active-or-passive multi-region topology, a backup/restore procedure
  (Git-as-config-backup, governance PVC snapshot, audit-via-log-aggregator), and
  reference RTO (≈ minutes) / RPO (≈ zero for config) targets, plus a failover
  drill checklist for the one step only the operator can run.
- Cross-links from `deploy/README.md` and `deploy/runbook.md`; GO_TO_MARKET
  claims reconciled (HA/DR deployment now "safe to claim now" with the quota and
  live-drill caveats; only an enterprise *SLA* remains "do not claim").

**Tests.** `tests/unit/test_helm_ha_deployment.py` renders the actual chart with
`helm template` and asserts the invariants are real properties of the manifests,
not prose: zero-downtime strategy, a change-sensitive config checksum (proving a
config edit actually rolls the pods), the HA overlay's PDB + zone spread + Redis
concurrency, and the governance PVC being RWX and opt-in. Skips cleanly where
`helm` is absent; CI images that ship helm exercise it for real.

**Honest remainder (operator-run, by design).** A live multi-zone/multi-region
failover *drill* against a real cluster is the operator's step — HA_DR.md §5 is
its checklist. Everything code/chart/doc-preparable is done and tested here.

### 57. Pluggable dialect-adapter architecture ✅ DONE

**Shipped.** Dialect-specific behavior is now behind two formal, registry-
dispatched abstract bases — one concrete class per dialect, no inline
`if dialect == ...` branching:

- **Compiler (sync)** — `compiler/dialect_adapters.py`'s `DialectAdapter`
  (date-bucketing, order-by nulls, stat/string_agg/array_agg/percentile_cont
  naming, column masking) with Postgres/MSSQL/SQLite classes — shipped as the
  compiler-scoped slice, item 73.
- **Engine/session (async)** — `connections/dialects.py`'s new
  `SessionDialectAdapter` (engine-URL, connect-args, query-timeout registration,
  per-session guardrails) with `PostgresSessionAdapter`/`MSSQLSessionAdapter`,
  dispatched via `_SESSION_ADAPTERS`/`get_session_adapter`. Reverses the prior
  deliberate 'inline branching is fine here' decision (recorded in the
  PRODUCT_GUIDE Decision Log; CLAUDE.md updated). Kept a SEPARATE ABC from the
  sync compiler adapter on purpose — async session execution vs. sync SQL
  building are different execution models. Behavior-preserving (module funcs
  are thin dispatchers; `test_dialects.py` unchanged + a new registry test).

Adding a dialect (item 19) is now: implement both adapters + register. The one
remaining gap is the cost-estimation hook's MSSQL side, which is blocked on
MSSQL estimated-plan support (item 26 ph2), not on the adapter interface.

<details><summary>Original scope</summary>

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
</details>

### 59. Read-only behavioral anomaly surfacing on the audit stream ✅ DONE

**Phase 1 (detection engine + admin REST API) ✅ DONE. Phase 2 (surface the
signal in item 44's browser dashboard) ✅ DONE.**

**Phase 1 shipped:** `querygate/admin/anomaly.py` — a read-only,
per-principal anomaly detector over item 23's persisted audit stream, plus
`GET /api/v1/admin/observability/anomalies` (`admin:observability:read`,
added to the item 44 router) returning a bounded, redaction-safe
`AnomalyReport`. Distinct from item 44's overview, which aggregates the
in-process Prometheus registry into *fleet* counters — this answers a
*per-principal* question from the durable stream: is one caller's recent
behavior unusual versus its own preceding baseline, even among queries policy
*allowed*?

- **Detection (`detect_anomalies`) is a pure function** over a list of
  `AuditEvent`s + a fixed `now` + `AnomalyThresholds` — no clock, file, or
  global state — so the full signal space is unit-testable. It splits each
  principal's events into a recent window `(now - recent, now]` and the equal-
  or-longer baseline window immediately before it, then flags three signals:
  `volume_spike` (recent per-second rate ÷ baseline rate ≥ ratio),
  `rejection_rate_spike` (jump in the *fraction* of a caller's queries policy
  denied), and `new_connection_access` (a connection reached in the recent
  window the caller never touched in its baseline). Both windows must clear
  `min_baseline_events`/`min_recent_events` first, so a brand-new or barely-
  active caller never trivially "spikes."
- **Bounded by construction:** `JsonlAuditEventSource` streams the audit file
  once into a bounded deque (`max_events_scanned`), reusing item 44's audit-
  viewer read tolerance (malformed lines counted, never fatal; only
  `query.execution` events, never config/catalog governance events); the
  report caps principals (`max_principals_reported`, ranked most-severe first)
  and per-principal new connections, and sets `truncated` honestly when a cap
  is hit.
- **Redaction-safe:** every surfaced field (`principal_id`, `connection`,
  counts, per-minute rates, ratios) is already on the persisted `AuditEvent`
  and already browsable via item 31's audit viewer; the report carries no SQL,
  predicate value, row, table, or column, and the model is `extra="forbid"`
  so a leak field can't be added silently. `test_report_is_redaction_safe`
  and `test_anomalies_surface_a_spike_from_the_jsonl_stream` assert this
  against the live serialized schema.
- **Strictly within the 32C boundary (`CLAUDE.md`):** read-only. There is no
  write path in the module at all — it never edits a policy, throttles,
  blocks, or influences execution. Config lives in `AppConfig`
  (`anomaly_*` fields, all optional with conservative defaults) and
  `.env.example`; with `AUDIT_SINK_BACKEND=none` the endpoint honestly
  reports `source="disabled"`.

Covered by `tests/unit/test_anomaly.py` (each signal, the min-volume guards,
window filtering, unauthenticated grouping, principal-cap ranking/truncation,
threshold validation, exact window-boundary classification, naive-timestamp
handling, the new-connection per-principal cap, the JSONL source's malformed/
out-of-window/bound handling, the route config→thresholds/source mapping, and
redaction) and `tests/integration/test_anomaly_api.py` (scope enforcement,
disabled-without-sink, a real spike surfaced from a written JSONL file without
leaking values, and the configured principal cap enforced end-to-end).
`scripts/anomaly_ui_smoke.py` (`make anomaly-ui-smoke`) is a UI-visualization
smoke: it seeds a JSONL audit stream, computes the real `AnomalyReport`, and
renders the *actual* admin UI assets (`index.html`/`app.js`/`app.css`, only the
network stubbed) in headless Chromium — asserting the real `renderAnomalies()`
draws every principal, badge, and detail, and writing `dist/anomaly-ui-smoke.png`
for human inspection (it SKIPs cleanly when no browser is present).

**Why it matters:** Item 44 covers rejection-trend dashboards — denied
queries. This is distinct: surfacing unusual volume or shape even among
*allowed* queries per principal (e.g. a sudden order-of-magnitude spike) as
a passive alert. Must stay strictly within the 32C boundary already fixed
in `CLAUDE.md`: a read-only signal for a human admin to look at, never an
autonomous policy edit or a feedback loop back into enforcement.

**Phase 2 shipped:** A "Behavioral anomalies" subsection in the existing
Observability view of the admin UI (`admin_ui/index.html`/`app.js`/`app.css`),
rendered from the same `admin:observability:read` fetch pattern as the item 44
overview — no new backend, scope, or mutation path. `loadObservability()`
fetches `/admin/observability/anomalies` alongside the overview and
`renderAnomalies()` draws one row per (principal, signal): a color-coded kind
badge (volume spike / rejection spike / new connection), a human-readable
detail (`4.3× its baseline rate`, `81.0% rejected vs 4.0% baseline`,
`reached payroll-prod — untouched in baseline`), and recent/baseline
count·rate. `source="disabled"` and the empty case render honest guidance
strings rather than a blank panel; the report's note plus the window/scanned/
truncated summary is shown as the same honest snapshot banner the overview
uses. Asserted by `tests/integration/test_admin_ui.py` (the SPA serves the
`anomaly-table-wrap` panel, the "Behavioral anomalies" heading, and the
`/admin/observability/anomalies` fetch).

### 60. Bug bounty / responsible disclosure program ✅ DONE

**Effort: S (process and policy, not engineering).** Coordination-gated for its
*paid* tier only — that pairs with item 53 and is explicitly deferred; the
disclosure program itself is stage-appropriate to ship now.

**Why it mattered:** a public disclosure process is a cheap, durable trust
signal, and without it a researcher has no responsible channel to report.

**What shipped.** `SECURITY.md` (already carried reporting channel, in/out scope,
SLAs, supported-version policy, and links to the posture/threat-model) gained the
two remaining pieces of this item:

- **A recognition/reward structure decision appropriate to the current stage:**
  coordinated disclosure + public recognition, **no monetary bounty yet** —
  recorded as a deliberate decision, with the paid-program escalation gated on
  item 53's audit (paying for findings a first audit would catch is poor use of a
  bounty). The reporting channel/scope/process are stated to survive that
  escalation unchanged.
- **A single remediation process** all reports flow through (researcher, internal
  adversarial-suite finding, or item-53 audit finding): triage/severity →
  regression-lock as a failing test in `tests/security/` (the same bar every
  guardrail meets) → fix + release gates → release & coordinated disclosure.

This satisfies item 60's "publish SECURITY.md + decide a stage-appropriate
structure + route through a shared remediation process." The only remaining part
— standing up a *paid* bounty platform after the audit — is the deliberately
deferred escalation, not a gap.

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

### 68. WHERE/HAVING resource-exhaustion guardrail caps ✅ DONE

**Problem.** `Policy` already caps join count, select width, WHERE nesting
*depth*, and group-by width (`validation/policy_validation.py`), but nothing
capped the *total number* of predicates in a WHERE tree or the *size* of a
single `in`/`not_in` predicate's value list. A single-level
`{"or": [10,000 predicates]}` has `where_depth() == 2` and passed every
existing check while still compiling into a huge boolean expression; an
`in` predicate with an arbitrarily long value list had no cap at all.

**Shipped.** Added `Policy.max_where_predicates` (default 100) and
`Policy.max_in_list_size` (default 1000) to `policy/models.py`.
`validate_policy` in `validation/policy_validation.py` now: counts total
`Predicate` leaves in `where` via a new `_iter_where_predicates` walker and
rejects over-cap counts; separately checks `having`'s flat list length
against the same cap; and checks every `in`/`not_in` predicate's value list
(across both `where` and `having`) against `max_in_list_size`. Documented
both fields in `examples/policy.example.yaml` next to the other complexity
caps. Added boundary tests (at-cap passes, one-over-cap rejects) to
`tests/unit/test_policy_boundaries.py` following that file's existing
parametrized pattern, plus targeted rejection tests in
`test_policy_validation.py`.

**Effort: XS.** Validation-only; no AST or compiler change.

**Why it matters:** closes the last un-capped resource-exhaustion surface
in the WHERE/HAVING shape — every other structural dimension of a query
(joins, select width, nesting depth, group-by width, top_n) already had an
explicit ceiling.

### 69. DISTINCT / COUNT(DISTINCT) ✅ DONE

**Problem.** The AST had no way to de-duplicate rows — neither
`SELECT DISTINCT` nor `COUNT(DISTINCT col)` — despite both being ordinary,
frequent asks ("how many unique customers ordered this month").

**Shipped.** Added `StructuredQuery.distinct: bool` (whole-query dedup) and
`AggregateSelectItem.distinct: bool` (per-aggregate dedup) to
`query_ast/models.py`, with a `model_validator` on `AggregateSelectItem`
rejecting `distinct=True` combined with `col="*"` (`COUNT(DISTINCT *)` isn't
valid SQL — a caller gets a clear rejection instead of a DB-level error).
`compiler/sqlalchemy_compiler.py`: `compile_structured_query` calls
`stmt.distinct()` when `query.distinct`; `_build_select_columns` wraps the
resolved column with `.distinct()` before applying the aggregate fn when
`item.distinct`. No new policy cap needed — bounded by the existing
row/complexity caps. Added shape-validation tests to `test_query_ast.py`
and rendering tests to `test_compiler.py`.

**Effort: XS.** Two boolean fields, one validator, two compiler call sites.

**Why it matters:** closes a gap where an agent literally could not express
"unique X" — the request had to be either impossible or answered by
pulling more rows than needed and de-duplicating client-side, defeating the
point of a policy-enforced gateway.

### 70. Table aliases and self-joins ✅ DONE

**Problem.** `JoinSpec.table` had to be a real table name, and every
column-resolution path (schema reflection, policy checks, compilation) keyed
off that raw name — so the same physical table could never appear twice in
one query. A common ask like "for each employee, who is their manager"
(`Employee` joined to itself) was structurally impossible.

**Design.** Introduced *effective names*: every from/join occurrence has an
effective name (its alias if given, else its own table name), and every
`Table.Column` reference elsewhere in the query is qualified by effective
name. Kept strictly separate from *physical* names, which schema reflection
and policy allow/deny checks always operate on — an alias must never be a
back door around table/column policy.

**Shipped.**
- `query_ast/models.py`: `StructuredQuery.from_alias` and `JoinSpec.alias`,
  plus a `model_validator` (`_validate_table_aliases`) that runs at the pure
  AST layer, before any DB touch: effective names must be unique
  case-insensitively, and a physical table used more than once (a
  self-join) must carry an explicit alias on *every* occurrence — no
  implicit "first one wins."
- `validation/schema_validation.py`: new `effective_name_map(query)` —
  single source of truth mapping every effective name to its physical
  table, reused by policy validation and the compiler. `validate_schema`
  now reflects each distinct physical table once (a local cache keyed by
  physical name, independent of `schema/reflection.py`'s own cache) and
  wraps every aliased occurrence with SQLAlchemy's `.alias(effective_name)`;
  `resolve_query_table_connections` and `_validate_join_graph` operate on
  effective names throughout.
- `validation/policy_validation.py`: **critical fix** — every place that
  checked a `Table.Column` ref's leading component against
  `policy.table_allowed`/`column_allowed` now maps it through
  `effective_name_map` to the physical table first. Without this, aliasing
  a denied table/column would have silently bypassed column-level policy.
  Added `test_denied_column_rejected_when_referenced_through_alias` and
  `test_denied_table_rejected_when_referenced_through_join_alias` proving
  the fix.
- `compiler/sqlalchemy_compiler.py`: FROM/JOIN resolve by effective name.
  `_apply_mandatory_row_filters` now applies a matching filter to *every*
  occurrence of a physical table, not just the first dict match — a
  self-join of a mandatory-filtered table (e.g. tenant-scoped `Employee`)
  gets the filter AND-ed in on both aliases, proven by
  `test_mandatory_row_filter_applies_to_every_self_join_alias`.
- Verified end-to-end against a real in-memory SQLite engine (compiled SQL
  executed, not just rendered): a self-join of an `employees` table
  correctly returned each row's manager's name via a LEFT JOIN on two
  distinct aliases of the same table.
- `mcp/instructions.py` gained a short "Self-joins" section (aliasing is a
  required, non-obvious step the AST rejects without) alongside the
  existing "Cross-connection joins" section it sits next to.

**Effort: L.** The largest of the five items — touches AST validation,
both validation layers, and the compiler, with two separate
security-correctness invariants (policy must resolve aliases to physical
identity; mandatory filters must cover every alias) each needing their own
dedicated test, not just a happy-path check.

**Why it matters:** self-joins are an ordinary, frequent query shape
(hierarchies, before/after comparisons on the same entity) that was
previously simply unrepresentable — not capped, not restricted, entirely
absent from the grammar.

### 71. NOT groups and column-to-column WHERE comparisons ✅ DONE

**Problem.** `WhereGroup` only supported `and`/`or` — there was no way to
negate a compound condition (`NOT (A AND B)`); `neq`/`not_in` cover single
negated predicates but not a negated group. Separately, `Predicate.value`
was always a literal, so `WHERE OrderItem.Price > OrderItem.Cost` (compare
two columns on the same row) was impossible to express.

**Shipped.**
- `query_ast/models.py`: `WhereGroup.not_terms` (aliased `"not"`), a single
  child (Predicate or nested WhereGroup) to negate — `_exactly_one_boolean`
  extended to require exactly one of and/or/not, not just and/or.
  `Predicate.value_col: Optional[str]` — compares `col` against another
  Table.Column instead of a literal; `_validate_value_shape` extended so
  exactly one of value/value_col is set for every op except
  is_null/is_not_null, and `value_col` is only accepted for
  eq/neq/lt/lte/gt/gte (in/not_in/between/like need a literal
  list/pattern, not a column, so those are rejected with `value_col` set).
- `compiler/sqlalchemy_compiler.py`: `_compile_where` wraps `not_terms` in
  `sa.not_(...)`; `_apply_predicate` now takes the reflected `tables` dict
  and resolves `value_col` to a real column when set (falls back to
  `pred.value` otherwise — every other op still only ever sees a literal,
  since `value_col` is AST-rejected for them).
- `validation/schema_validation.py`: `_where_depth`,
  `_collect_tables_from_where`, `_validate_where_columns` all descend into
  `not_terms`; `_validate_predicate_columns` also resolves `value_col`
  against the reflected schema; the `having`/`needed`-table-collection loop
  in `validate_schema` adds `value_col`'s table too, so a value_col
  referencing a joined (not just the base) table gets correctly reflected.
- `validation/policy_validation.py`: **critical fix, same category as item
  70's alias fix** — `_where_column_refs` and `_iter_where_predicates` now
  descend into `not_terms` (so `max_where_depth`/`max_where_predicates`/
  `max_in_list_size` from item 68 still apply inside a negated group, and
  column policy still sees predicates hidden behind a NOT), and
  `_iter_column_refs`'s `having` loop now also yields `pred.value_col`.
  Without the latter, a denied column could be read indirectly by putting
  it on the right-hand side of a comparison instead of selecting or
  filtering on it directly — proven by
  `test_denied_column_rejected_when_only_used_as_value_col` and
  `test_denied_column_rejected_when_only_used_in_not_group`.

**Effort: M.** Two features bundled because they touch the same four files
in the same places; each has its own AST validator, compiler path, and a
dedicated policy-bypass test — not just a happy-path compile test.

**Why it matters:** closes two ordinary expressiveness gaps (negating a
compound condition; comparing two columns on the same row) while proving,
not just assuming, that both stay inside the existing policy/cap
enforcement rather than becoming a new blind spot.

### 72. Whitelisted scalar functions and CASE in select (SELECT-only) ✅ DONE

**Problem.** No `COALESCE`, `LOWER`/`UPPER`/`TRIM`, string concat, or
`CASE WHEN` existed anywhere in the AST. Ordinary asks like "treat NULL
discount as 0" or "compare names case-insensitively" were simply
unrepresentable, not just capped.

**Scope boundary (deliberate — see docs/PRODUCT_GUIDE.md Decision Log).**
SELECT projections only, not usable as a WHERE/HAVING predicate target.
Extending `Predicate.col` to accept a function-wrapped expression is a
materially bigger change (threads a new type through every
`parse_column_ref` call site); left as a separate, explicitly-scoped
future item rather than folded in here.

**Shipped.**
- `query_ast/models.py`: a tagged-union arg shape avoids "is this string a
  column ref or a literal?" ambiguity — `ColArg` (`{"col": "Table.Column"}`)
  and `LiteralArg` (`{"literal": ...}`), verified to discriminate correctly
  from plain JSON dicts via Pydantic's smart union. `ScalarFunctionSelectItem`
  (`fn`: coalesce/lower/upper/trim/concat, `args`, optional `alias`) —
  lower/upper/trim require exactly one `ColArg`; coalesce/concat require
  2+ args of either kind. `CaseSelectItem` (`when`: list of `{when:
  Predicate, then: ScalarFunctionArg}`, optional `else_`, REQUIRED `alias`
  since no sensible default name exists) — `when` is a single `Predicate`
  per branch, not a full `WhereNode`, a deliberate v1 simplification
  covering the common `CASE WHEN col = x THEN ...` shape. Both added to
  `SelectItem`'s union.
- `policy/models.py`: `max_case_branches` (default 10), following the same
  structural-size-cap philosophy as every other policy cap.
- `validation/schema_validation.py`: new `select_item_column_refs(item)` —
  single source of truth yielding every `Table.Column` ref inside any
  select item variant (bare string, aggregate/date_bucket `.col`, scalar
  function args, CASE when/then/else), replacing the old select-loop logic
  in both this module and `policy_validation.py` (which previously assumed
  every non-string select item had a `.col` attribute — true before this
  item, false for the two new variants). `_validate_select_columns` now
  resolves every item type's columns generically through it, plus a
  strict pass on each CASE branch's `when` Predicate (`allow_alias=False`,
  same reasoning as top-level `where`). `_select_aliases` extended so
  `group_by`/`having`/`order_by`/`top_n` can reference a scalar-function or
  CASE alias.
- `validation/policy_validation.py`: **same security-correctness category
  as items 70/71's fixes** — a naive extension of the old select-loop (which
  assumed every non-string select item had a `.col` attribute) would have
  left a denied column referenced only inside a `coalesce(...)` call or a
  CASE `when`/`then`/`else` completely unwalked by
  `_iter_column_refs`/`_collect_referenced_tables` — invisible to column
  policy, not merely mis-attributed. The generic `select_item_column_refs`
  rewrite closes that path from the start rather than shipping it and
  patching later. Proven by `test_denied_column_rejected_inside_coalesce`,
  `test_denied_column_rejected_inside_case_when`, and
  `test_denied_column_rejected_inside_case_then`. Also added the
  `max_case_branches` cap check.
- `compiler/sqlalchemy_compiler.py`: `_build_select_columns` gains branches
  for both item types (`sa.func.coalesce`/`concat`/`lower`/`upper`/`trim`,
  `sa.case((cond, then), ..., else_=...)` reusing the existing
  `_resolve_predicate_target`/`_apply_predicate` pair for each `when`
  condition). Verified end-to-end against a real in-memory SQLite engine
  (not just rendered SQL): `COALESCE`, `LOWER`, and `CASE WHEN...ELSE` all
  executed and returned correct values, and a CASE alias was confirmed
  usable in `GROUP BY`.
- `examples/policy.example.yaml` and `mcp/instructions.py` updated
  (the latter with the explicit SELECT-only caveat, since an agent might
  otherwise reasonably assume a function usable in SELECT is also usable
  in WHERE).
- `tests/unit/test_mcp_token_budget.py`: `_MAX_TOTAL_CHARS` bumped from
  62,000 to 66,500 (measured actual: 63,201) — the new Field descriptions
  across items 68-72 pushed past budget; bumped deliberately in this same
  commit per item 66's own rule, not silently.

**Effort: L.** The riskiest item by design (closest to a general expression
grammar) — kept bounded by the SELECT-only scope decision and an explicit
function whitelist rather than open-ended expressions.

**Why it matters:** closes the highest-value remaining expressiveness gap
identified in the original review, while the select-loop rewrite it forced
also fixed a genuine policy-bypass latent in how select items were walked
for column-level policy — a second security-correctness fix, not just a
feature add.

### 73. `DialectAdapter` abstraction (compiler-scoped slice of item 57) ✅ DONE

**Problem.** Adding NULLS FIRST/LAST ordering and `stddev`/`variance`
aggregates (items 74/75, next) each needed real per-dialect handling —
about to become the second and third ad hoc `if dialect == MSSQL: ... else:
...` branch in the compiler, joining the existing one in
`_date_bucket_expr`. Flagged directly: keep the engine dialect-agnostic at
its core, with dialect variance isolated behind an abstraction so adding or
dropping a dialect stays a contained, plug-in change. This is item 57
("Pluggable dialect-adapter architecture"), already scoped in the backlog
under P4 — this closes the compiler-scoped slice of it (not
`connections/dialects.py`'s session guardrails or
`execution/cost_estimation.py`'s Postgres-only EXPLAIN hook, which item 57
also mentions but which are separate concerns left for a future pass).

**Shipped.** New `compiler/dialect_adapters.py`: an `abc.ABC`
`DialectAdapter` with three methods — `date_bucket`, `order_by_terms`,
`stat_fn` — the only three points where two dialects render meaningfully
different SQL for the same AST concept (count/sum/avg/min/max and
coalesce/lower/upper/trim/concat are dialect-universal and deliberately
NOT on this interface — keeps it focused on what actually varies, not
padded with things that don't). `ABC` chosen over `typing.Protocol`
specifically so an incomplete new adapter fails loudly at class-definition
time, not with a confusing `AttributeError` mid-compile — proven by
`test_dialect_adapters.py::TestDialectAdapterIsAbstract`. Three concrete
adapters (`PostgresDialectAdapter`, `MSSQLDialectAdapter`,
`SQLiteDialectAdapter` for the internal-only test/example path) plus a
`get_dialect_adapter(dialect)` registry lookup falling back to SQLite for
anything unrecognized, matching `_date_bucket_expr`'s pre-existing
fallback behavior exactly.

**Ported, not rewritten:** `_date_bucket_expr`'s 3-way branch and
`_sqlite_date_bucket_expr`'s granularity switch moved into the three
adapters' `date_bucket` verbatim; `sqlalchemy_compiler.py`'s call site
became `get_dialect_adapter(dialect).date_bucket(col, item.granularity)`.
Every pre-existing date-bucket test
(`test_date_bucket_postgres_uses_date_trunc`,
`test_date_bucket_mssql_uses_dateadd_datediff`) passed unmodified after the
extraction — the regression check proving this was behavior-neutral before
items 74/75 built anything new on top of it.

**Effort: M.** New module + one call-site swap; the actual "no behavior
change" claim is enforced by tests that already existed, not asserted.

**Why it matters:** the third scattered dialect branch is exactly the
point where "two dialects supported" starts costing more than linear
effort per additional dialect (item 19) or per additional dialect-sensitive
feature (items 74/75, immediately next). Naming and isolating the
abstraction now, verified behavior-neutral against the one branch that
already existed, is what keeps that cost flat going forward.

### 74. NULLS FIRST/LAST ordering ✅ DONE

**Problem.** No control over where NULLs sort in `order_by`/`top_n`. Worse
than a missing feature: Postgres and MSSQL default NULL ordering
*differently*, so the identical query returns rows in a different order
depending only on which connection it hits — a silent, dialect-dependent
correctness gap, not just an expressiveness one.

**Verified before implementing (empirically, not assumed):** MSSQL has no
`NULLS FIRST/LAST` syntax at all — T-SQL never supported it — but
SQLAlchemy's mssql dialect will still *compile* `.nulls_last()` into the
literal (broken) clause rather than raising, so rendering alone can't catch
this; it has to be handled explicitly per dialect.

**Shipped.** `OrderBySpec.nulls: Optional[Literal["first","last"]]`. Both
`MSSQLDialectAdapter.order_by_terms` and `PostgresDialectAdapter`/
`SQLiteDialectAdapter`'s (item 73) now do real work: Postgres/SQLite use
native `.nulls_first()/.nulls_last()`; MSSQL emulates with a leading 0/1
CASE-based sort bucket (NULLs and non-NULLs into separate buckets, sorted
ascending, then the real column direction breaks ties within each bucket)
— proven to actually take that path, not silently fall through to the
broken native compile, by asserting `CASE` appears and the literal string
`NULLS` does not. `compile_structured_query`'s main `order_by` loop and
`_apply_top_n`'s window-function `order_by` (which needed `dialect` threaded
into its signature — didn't take one before) both call
`get_dialect_adapter(dialect).order_by_terms(...)` uniformly; neither call
site branches on dialect itself. Verified end-to-end against real SQLite
execution (not just rendered SQL) that NULLs actually land where requested
for both `nulls="first"` and `nulls="last"`.

**Effort: S**, on top of item 73's foundation — this is exactly the kind of
change item 73 was meant to make small: one new adapter method
implemented three times, two call sites updated to use it, zero new
dialect branches in `sqlalchemy_compiler.py` itself.

**Why it matters:** closes a real cross-dialect correctness gap, not just
an expressiveness one — the same query returning differently-ordered rows
depending on which connection answered it is exactly the kind of silent
inconsistency this project's dialect-isolation discipline exists to
prevent.

**Revised (2026-07-20) — MSSQL now rejects `nulls`, does not emulate.** The
CASE-bucket emulation above was reconsidered against the "expose primitives,
don't spoon-feed the agent" rule (CLAUDE.md, added after this item shipped):
injecting an extra sort column the AST never expressed is the engine solving
the agent's composition problem. `MSSQLDialectAdapter.order_by_terms` now
raises `QueryValidationError` when `nulls` is set — same posture as
`array_agg` (item 81) — at both the main `order_by` and `top_n` rank-ordering
call sites. Postgres/SQLite keep native `.nulls_first()/.nulls_last()`. An
agent wanting null placement on MSSQL composes it directly with primitives
already exposed (a `CaseSelectItem` 0/1 "is null" bucket + a leading
`OrderBySpec` on it). Recorded in `docs/PRODUCT_GUIDE.md`'s Decision Log.

### 75. `stddev`/`variance` aggregate functions ✅ DONE

**Problem.** No statistical aggregates existed at all.

**Verified before implementing:** `sa.func.stddev`/`sa.func.variance`
render the identical literal function name on every SQLAlchemy dialect —
but MSSQL has no functions by those names; its real ones are `STDEV`/`VAR`.
A naive addition would have compiled fine and failed at execution time
against a real SQL Server.

**Shipped.** `AggregateFn` extended with `"stddev"`/`"variance"`.
`AggregateSelectItem`'s distinct-guard validator (renamed
`_validate_distinct`, its scope now broader than the name it replaced)
also rejects `distinct=True` combined with either — T-SQL's `STDEV`/`VAR`
don't accept `DISTINCT` at all, so this is rejected uniformly across
dialects rather than working on Postgres and silently breaking on MSSQL.
`DialectAdapter.stat_fn` (item 73) resolves the two names per dialect:
Postgres/SQLite use `stddev`/`variance` directly; MSSQL uses `STDEV`/`VAR`;
SQLite's raises (no such functions exist there, and it's the internal-only
test/example dialect). `sqlalchemy_compiler.py`'s new `_aggregate_fn(name,
dialect)` helper routes `stddev`/`variance` through the adapter and
everything else through the existing dialect-universal `_AGG_FNS` dict —
no new dialect branch in the compiler itself, exactly the payoff item 73
was built for.

**Explicitly out of scope, not silently dropped:** `string_agg`/
`array_agg` (need an extra delimiter parameter — don't fit
`AggregateSelectItem`'s `{fn, col}` shape) and `percentile_cont` (needs
`WITHIN GROUP (ORDER BY ...)`, a structurally different aggregate shape,
and MSSQL has no clean equivalent). Real and useful, but each needs its
own AST shape — a separate future item, not a rushed fit into this one.

**Effort: S**, same "one adapter method, zero new compiler branches" payoff
as item 74.

**Why it matters:** real analytics asks ("how much does delivery time
vary by region") were previously impossible to express at all, not just
capped.

### 76. Composite (multi-column) join keys ✅ DONE

**Problem.** `JoinSpec.on` was hard-capped at exactly one column pair.
Composite keys (e.g. `(tenant_id, order_id)` together) could only be
half-expressed — join on one column, filter the rest in WHERE, which is
both awkward and not actually equivalent (a WHERE filter runs after the
join, not as part of the join condition itself, so it doesn't affect which
rows a LEFT JOIN's unmatched side produces).

**Design — additive, not a breaking shape change.** `JoinSpec.on` stays
exactly as it was (zero churn across ~40 existing call sites/examples).
New `JoinSpec.extra_on: List[List[str]]` — additional `[LeftTable.Col,
RightTable.Col]` pairs ANDed with the primary `on`.

**Shipped.** AST validator requires each `extra_on` entry be a real
two-element pair. `schema_validation._validate_join_graph` additionally
requires every `extra_on` pair reference the *same two tables* as the
primary `on` — a join's condition is always about the one pair of tables
it joins, never a smuggled third table's column. Compiler ANDs one
equality condition per pair (`on` plus each `extra_on` entry) before
`stmt.join(...)`. `policy_validation._iter_column_refs`'s join loop
extended to also yield `extra_on` refs — **dedicated test**
(`test_denied_column_rejected_when_only_used_in_extra_on`) proving a
denied column reachable only through a composite key's second pair is
still rejected, same category as every prior alias/value_col bypass test.
Verified end-to-end against real SQLite execution: seeded data where two
rows each satisfy only ONE of the two join conditions and one row satisfies
both, confirming the compiled join returns only the row matching both —
not a rendering-only check.

**Effort: S.** Dialect-agnostic — unaffected by items 73–75's work.

**Why it matters:** composite foreign keys are common in real schemas
(especially multi-tenant ones scoping every table by `tenant_id` alongside
its own primary key); the AST previously couldn't express that as an
actual join condition at all.

### 77. Scalar functions in WHERE/HAVING predicates (`Predicate.col_fn`) ✅ DONE

**Supersedes item 72's SELECT-only scope decision** — see the Decision Log
entry recording the reversal (docs/PRODUCT_GUIDE.md). Closes the exact gap
item 72 deferred: `WHERE lower(status) = 'active'`,
`HAVING coalesce(discount, 0) > 5` are now expressible, not just projectable.

**The "materially bigger change" item 72 predicted didn't fully
materialize.** `Predicate.col` didn't need to become `Union[str,
expression]` — instead `Predicate.col_fn: Optional[ScalarFunctionCall]`, a
sibling field to `col`, mutually exclusive with it (validated by
`_validate_col_shape`, `col` itself became `Optional[str]`). `Scalar
FunctionSelectItem`'s `fn`/`args` shape (and its arg-count validator) was
factored into a shared base `ScalarFunctionCall`, with
`ScalarFunctionSelectItem` now just `ScalarFunctionCall` + an alias — one
definition of the whitelist/shape, two use sites. **No function nesting** —
`args` stays `List[ColArg | LiteralArg]`, never another
`ScalarFunctionCall` — kept exactly as narrow as item 72's version, still
not an open-ended expression grammar. `col_fn` works with every operator
(not just eq/neq/lt/lte/gt/gte the way `value_col` is restricted) — `lower(
status) IN (...)`, `coalesce(x, 0) BETWEEN ...` are all ordinary SQL.
`CaseWhen.when: Predicate` gained `col_fn` for free, proven by a test:
`CASE WHEN lower(status) = 'x' THEN ...` now works too.

**Shipped.** `validation/schema_validation.py`: new `predicate_column_refs(
pred) -> Iterator[str]` — single source of truth (col if dotted, else each
`col_fn.args`' `ColArg`, plus `value_col`) replacing three separate ad hoc
`if "." in ...`/`if value_col` duplications
(`_where_column_refs`-equivalent walk, the `having` table-collection loop,
`_collect_tables_from_where`) — and `select_item_column_refs`'s CASE branch
now delegates to it too, closing a gap where a CASE `when` predicate's
`col_fn` would otherwise have been silently invisible to both reflection
and policy. `_validate_predicate_columns` resolves `col_fn`'s columns
against the live schema. `compiler/sqlalchemy_compiler.py`:
`_resolve_predicate_target` resolves `col_fn` via the existing `_SCALAR_FNS`/
`_resolve_scalar_arg` helpers item 72 already built.

**Critical fix, same category as items 70–72's fixes.**
`validation/policy_validation.py`'s `_where_column_refs` and the `having`
loop in `_iter_column_refs` now delegate to `predicate_column_refs` too —
closing the path where a denied column reachable only through
`lower(customers.email) = 'x'` (WHERE) or inside a `coalesce(...)` in
HAVING would otherwise never be walked by column-level policy at all.
Proven by `test_denied_column_rejected_when_only_used_inside_predicate_col_fn`
and its HAVING-variant sibling.

Verified end-to-end against real SQLite execution (not just rendered SQL):
seeded rows with mixed-case status values, confirmed
`WHERE lower(status) = 'active'` matched both `'Active'` and `'ACTIVE'`
while excluding `'inactive'`.

**Effort: L.** The architecturally central item of this round — touches
all four layers, with the same "walk every new ref site or it's a policy
bypass" discipline as every prior alias/value_col/extra_on addition.

**Why it matters:** this was the single highest-leverage gap identified
after the first round — the one place where "SELECT-only" was a real,
user-visible limitation rather than a reasonable scope boundary, and it's
now closed at no cost to the narrowness (no nesting, whitelisted functions
only) that made the original version safe.

### 78. Cross-dialect rendering verification pass for items 68–77 ✅ DONE

**Scope decision, stated rather than silently assumed.** No real MSSQL
server runs in this environment (`make test-mssql-live` needs one
provisioned via `setup_mssql_test_db.py`), and provisioning one — an image
pull, ODBC driver setup, a new persistent container — is a meaningfully
heavier, longer-running action than the code changes in this round.
`make test-mssql-live` stays available as an operator-run follow-up if
live verification is wanted; not attempted automatically here.

**What was achievable and genuinely valuable, and is what shipped:** every
item-68–77 compiler code path that previously only had a Postgres-default
(or single-dialect) render test now also has an explicit `dialect="mssql"`
one, closing the gap where a feature could render fine on Postgres and be
subtly wrong or unrenderable on MSSQL with nothing noticing —
**exactly the class of bug items 74/75 themselves ran into** (NULLS
FIRST/LAST silently compiling to broken T-SQL; `stddev`/`variance`
resolving to Postgres-only function names) before their own dialect
handling closed it. New `TestCrossDialectRendering` in `test_compiler.py`:
DISTINCT / COUNT(DISTINCT), self-joins, NOT groups, `value_col`
column-to-column comparisons, `lower`/`upper`/`trim`/`concat`, CASE,
composite (`extra_on`) join keys, and predicate `col_fn` (both WHERE and
HAVING) — 14 new tests, each asserting the MSSQL-rendered SQL text
directly, not just that compilation didn't raise.

**Honest about what this is not:** rendering-level coverage, not live
execution — it would not have caught, say, a real SQL Server rejecting a
function at parse time for a reason SQLAlchemy doesn't model. Stated
explicitly rather than implying equivalence to `test-mssql-live`.

**Effort: S.** Test-only; no production code changed.

**Why it matters:** the two real dialect bugs items 74/75 had to design
around were both things rendering-level testing — not assumption — caught.
This pass applies that same check retroactively to everything shipped
since item 68 that didn't already have it, on the theory that "renders
fine on Postgres" was never a safe proxy for "renders correctly on MSSQL"
in the first place.

### 79. Extend the property-based fuzzer to the item 68–77 AST surface ✅ DONE

**Problem.** `tests/unit/test_compiler_properties.py` (item 36 phase 1) still
only generated pre-item-68 shapes — everything since was covered by
hand-written boundary tests only, never by the combinatorial fuzzer.
Explicitly flagged as deferred, not done, when the first round shipped.

**Shipped, in `test_compiler_properties.py`:**
- `_predicates()` now draws a mix of plain literal predicates (still the
  majority, preserving the historically covered shape), `value_col`
  column-to-column comparisons (item 71), and `col_fn` scalar-function
  predicates (item 77) — biased rather than uniform, so the common case
  still dominates the generated corpus. `_where_clauses()` sometimes wraps
  the whole tree in a `not_terms` negation (item 71).
- `_row_select_queries()` gained: `distinct` (item 69) on the query itself;
  a scalar-function and/or CASE select item mixed into the projection list
  (item 72); an `extra_on` composite join-key pair on the join when one is
  present (item 76); `nulls` on every generated `OrderBySpec` (item 74).
- `_aggregate_queries()`'s function pool extended to include
  `stddev`/`variance` (item 75), with `distinct` drawn only when it's valid
  (never for `count(*)` or `stddev`/`variance` — encoded directly in the
  generator rather than filtered post hoc with `assume()`).
- New `_self_join_queries()` — structurally distinct from the other three
  strategies (needs a `tables` dict keyed by alias, not physical name), so
  it gets its own `test_compiler_never_crashes_on_self_join_shapes` and its
  own `test_compiler_respects_mandatory_row_filter_across_self_join_shapes`
  (proving item 70's "filter applies to every alias" guarantee — asserting
  the mandatory-filter marker appears exactly twice — holds across random
  self-join shapes too, not just the one hand-written case in
  `test_compiler.py`) rather than being forced into the existing shared
  `st.one_of(...)` mandatory-filter test, which only ever needs the
  original physical-name-keyed `tables` dict.
- The original `test_compiler_respects_mandatory_row_filter_across_random_shapes`
  is otherwise unchanged and automatically re-proves the mandatory-filter
  guarantee across the now-much-larger generated space for free, since it
  already wraps all three original strategies.

**Effort: M.** Test-only; no production code changed. All 6 property tests
(4 original-strategy tests + 2 new self-join ones) pass at 100 examples
each with the expanded generators.

**Why it matters:** closes the last explicitly-acknowledged gap from the
first round — hand-written tests prove specific shapes work, but only the
fuzzer proves the compiler is robust to the *combinatorics* item 36 was
written to cover, and that guarantee had silently stopped extending to
anything shipped after item 67.

---

### 80. `string_agg` aggregate function ✅ DONE

**Problem.** Item 75 shipped `stddev`/`variance` and explicitly deferred
`string_agg`/`array_agg` (need a delimiter parameter `AggregateSelectItem`'s
`{fn, col}` shape has no room for) and `percentile_cont` (needs `WITHIN
GROUP (ORDER BY ...)`, a structurally different shape). Of those,
`string_agg` was the right next item: Postgres (`string_agg`) and MSSQL
2017+ (`STRING_AGG`) both support it with the same `(expr, separator)`
shape, unlike `array_agg` (no MSSQL equivalent at all) or `percentile_cont`
(MSSQL-only-as-a-window-function).

**Shipped.** New sibling AST type `StringAggSelectItem` (`col`, `delimiter`,
optional `alias`) added to the `SelectItem` union — not a field bolted onto
`AggregateSelectItem`, matching how `DateBucketSelectItem`/
`ScalarFunctionSelectItem` are separate siblings. Its own model validator
rejects `col == "*"` up front (string_agg is never valid over `*`).
`DialectAdapter` (item 73) gained a fourth method, `string_agg(col_expr,
delimiter)`: Postgres renders `string_agg(...)`, MSSQL renders
`STRING_AGG(...)` (uppercase, matching the adapter's existing `STDEV`/`VAR`
convention). **SQLite's adapter is a real implementation, not a raise**
(unlike `stat_fn`, which has no SQLite stddev/variance equivalent at all):
`group_concat(expr, sep)` has the identical 2-arg shape as Postgres/MSSQL,
so it was mapped for real — a deliberate decision (confirmed with the user)
that gave this item genuine end-to-end execution coverage
(`tests/integration/test_sqlite_end_to_end.py::test_string_agg_end_to_end`,
seeded GB-country customers, asserts the concatenated *set* of names
matches, not an exact ordered string, since concatenation order is
implementation-defined without an `ORDER BY`-in-call). `_build_select_columns`
routes through `get_dialect_adapter(dialect).string_agg(...)`; `is_aggregate`
detection in both the compiler and `schema_validation.py`'s two
`has_aggregate` checks now treat `StringAggSelectItem` the same as
`AggregateSelectItem` (it affects `clamp_limit`'s aggregate cap, `top_n`
eligibility, and the having-without-group_by-or-aggregate rule identically).
`schema_validation._select_aliases` gained a `_string_agg_alias` branch.

**Explicitly out of scope, not silently dropped** (same reasoning item 75
used for `distinct`): `ORDER BY`-within-the-call (real on Postgres, absent
from MSSQL 2017+) and `DISTINCT` inside the call (Postgres supports it,
T-SQL's `STRING_AGG` does not) — both would need per-dialect rejection or
emulation, so v1 keeps the AST shape minimal instead. `array_agg`/
`percentile_cont` remain deferred exactly as item 75 described.

**Side-fix discovered and fixed in the same commit (confirmed with the
user):** `audit/events.py`'s `_select_shape`/`_predicate_shape` — called
unconditionally, unguarded, at the top of
`StructuredQueryService.execute()` — only handled `str`/`AggregateSelectItem`/
`DateBucketSelectItem` and raised `TypeError` for anything else. This meant
any real query selecting a `ScalarFunctionSelectItem` or `CaseSelectItem`
(items 71/72) already crashed `execute()` entirely, unnoticed because no
test exercised that path. Fixed alongside `StringAggSelectItem`'s own
branch, reusing the existing `select_item_column_refs`/`predicate_column_refs`
collectors (`validation/schema_validation.py`) as the single redaction-safe
source of which columns a select item or predicate touches, rather than
re-deriving that logic in the audit module. `_predicate_shape` also now
handles `Predicate.col_fn` (item 77) instead of silently emitting
`{"column": None, ...}` for a HAVING clause built on a scalar function.

**Effort: S**, same "one adapter method, zero new compiler branches" shape
as items 74/75, plus the audit-shape side-fix.

**Why it matters:** real reporting asks ("list every product SKU in this
order as one string") were previously impossible to express at all; the
audit-shape fix closes a real crash bug in the shared query-execution path,
not just a cosmetic logging gap.

---

### 81. `array_agg` aggregate function ✅ DONE

**Problem.** Item 80 shipped `string_agg` and explicitly deferred
`array_agg` as the harder of the two: Postgres has native `array_agg`, but
MSSQL has no MSSQL equivalent at all — T-SQL has no array/collection type
to hold the result, unlike `string_agg`'s lucky `(expr, separator)` shape
match across all three dialects. Per CLAUDE.md's "Engine philosophy: expose
primitives, don't spoon-feed the agent" section (added alongside item 80),
the correct move for a dialect that genuinely lacks a capability is to
implement it for real where it exists and reject it explicitly where it
doesn't — not synthesize an emulation the AST never asked for.

**Shipped.** New sibling AST type `ArrayAggSelectItem` (`col`, optional
`alias` — no `delimiter`, since an array result doesn't need a separator)
added to the `SelectItem` union, mirroring `StringAggSelectItem` exactly
otherwise (same `AliasChoices("as","alias")` pattern, `extra="forbid"`, a
model validator rejecting `col == "*"`). `DialectAdapter` (item 73) gained
a fifth method, `array_agg(col_expr)`: `PostgresDialectAdapter` renders
`sa.func.array_agg(col_expr)` — a real implementation, not a stub.
`MSSQLDialectAdapter.array_agg` raises `QueryValidationError` naming the
actual gap (no array/collection type in T-SQL). **This is the first
`DialectAdapter` method where a real, supported registry dialect — not
just the internal-only SQLite test/example path `stat_fn` already raised
on — rejects a capability outright.** `SQLiteDialectAdapter.array_agg`
also raises: `json_group_array()` returns a JSON-encoded string, not a
real array, so mapping it would be exactly the forced-parity emulation the
engine-philosophy section rules out, unlike `string_agg`'s genuine
`group_concat` shape match. `_build_select_columns` routes through
`get_dialect_adapter(dialect).array_agg(...)`, never an inline `if dialect
== "mssql"` branch. `is_aggregate`/`has_aggregate` detection (compiler and
both `schema_validation.py` checks) now treats `ArrayAggSelectItem` the
same as `AggregateSelectItem`/`StringAggSelectItem` — pulled the growing
3-type `isinstance` tuple into one shared `_AGGREGATE_SELECT_ITEM_TYPES`
constant in `query_ast/models.py`, reused by all three call sites, rather
than letting a third file drift with its own copy.
`schema_validation._select_aliases` gained an `_array_agg_alias` branch.
`audit/events.py`'s `_select_shape` gained its `ArrayAggSelectItem` branch
proactively in this same change (not deferred to a follow-up, per the
crash-bug lesson item 80 found and fixed for `StringAggSelectItem`/
`ScalarFunctionSelectItem`/`CaseSelectItem`).

Because SQLite can't stand in for a real array result, `array_agg` gets no
`test_sqlite_end_to_end.py` coverage the way item 80's `string_agg` did.
Instead it gets genuine coverage against a real Postgres
(`tests/integration/test_postgres_array_agg.py`, `pytest -m
postgres_live`): groups `examples/demo_db`'s seeded customers by country,
`array_agg`s their names, and asserts GB's returned array is exactly `{Ada
Lovelace, Alan Turing, Charles Babbage}` as a set (concatenation order is
implementation-defined without an `ORDER BY`-in-call, same reasoning item
80 used). `tests/unit/test_compiler.py` also gained a dedicated MSSQL
rejection test (`pytest.raises(QueryValidationError)` around
`compile_structured_query(..., dialect="mssql")`) — new for this item,
since `string_agg` never needed one (it rendered successfully on MSSQL).
`tests/unit/test_compiler_properties.py`'s `_aggregate_queries` fuzzer
strategy now draws a 3-way choice among plain aggregates/`string_agg`/
`array_agg`; confirmed first that this file only ever compiles at the
default Postgres dialect, so the fuzzer never hits the deliberate MSSQL
raise.

**Explicitly out of scope, not silently dropped** (same v1-bound reasoning
items 75/80 used): `distinct` and `ORDER BY`-within-the-call, matching
`string_agg`'s bound. `percentile_cont` remains deferred exactly as item 75
described — it needs a structurally different `WITHIN GROUP (ORDER BY
...)` shape, unrelated to this item.

**Effort: S**, same "one adapter method, mostly zero new compiler branches"
shape as items 74/75/80, plus pulling the aggregate-type tuple into one
shared constant while a third call site needed it anyway.

**Why it matters:** real reporting asks ("give me every SKU in this order
as a real list, not a string I have to re-split") are now expressible on
Postgres; equally importantly, this is the first time the `DialectAdapter`
abstraction has had to make a real, deliberate call about telling a
calling agent "no" on a supported production dialect rather than quietly
downgrading behavior — validating that item 73's abstraction and CLAUDE.md's
engine-philosophy rule both hold up under an actual forced-parity
temptation, not just a hypothetical one.

---

### 82. `percentile_cont` aggregate function ✅ DONE

**Problem.** Items 75/80/81 each explicitly deferred `percentile_cont` as
the one remaining aggregate gap, noting it needs `WITHIN GROUP (ORDER BY
...)`, a structurally different aggregate shape from `AggregateSelectItem`'s
`{fn, col}` pattern (or `StringAggSelectItem`/`ArrayAggSelectItem`'s
`{col, ...}` sibling shape).

**Verified before implementing** (same "verify before implementing" step
item 75 modeled for `stddev`/`variance`): Postgres's `percentile_cont
(fraction) WITHIN GROUP (ORDER BY expr)` is a true ordered-set *aggregate*
— usable in an ordinary `GROUP BY` query exactly like `array_agg`. MSSQL's
`PERCENTILE_CONT` is documented by Microsoft as an **analytic (window)
function only** — T-SQL requires an `OVER (...)` clause and has no
`GROUP BY`-compatible aggregate form at all. Confirmed directly with
SQLAlchemy: `sa.within_group(sa.func.percentile_cont(fraction), col_expr)`
**silently compiles identical SQL text against both the Postgres and
MSSQL dialect compilers** — it would pass compilation and only fail at
runtime against a real SQL Server, the exact "renders fine, breaks live"
trap item 75's investigation flagged for `stddev`/`variance` before
`stat_fn` existed.

**Shipped.** New sibling AST type `PercentileContSelectItem` (`col`,
`fraction: float`, optional `alias`), added to the `SelectItem` union.
Mirrors `StringAggSelectItem`/`ArrayAggSelectItem` otherwise (same
`AliasChoices("as","alias")` pattern, `extra="forbid"`, `col == "*"`
rejected) plus a new numeric-range validator rejecting `fraction` outside
`[0.0, 1.0]` — the first aggregate select item needing one.
`DialectAdapter` (item 73) gained a sixth method,
`percentile_cont(col_expr, fraction)`: `PostgresDialectAdapter` renders
`sa.within_group(sa.func.percentile_cont(fraction), col_expr)` — a real
implementation. `MSSQLDialectAdapter.percentile_cont` raises
`QueryValidationError` naming the actual structural gap (analytic/window-
function-only, no `GROUP BY` form). **This is the first `DialectAdapter`
rejection in this arc for a genuinely different reason than item 81's
`array_agg`** — not "no equivalent type exists" but "the equivalent exists
only in an incompatible structural form (window function vs. plain
aggregate)" — confirming the per-dialect-capability question in CLAUDE.md's
engine-philosophy section has more than one shape, not just a single
repeated pattern. `SQLiteDialectAdapter.percentile_cont` also raises, for
its own distinct reason: no ordered-set aggregate support at all.
`_build_select_columns` routes through
`get_dialect_adapter(dialect).percentile_cont(...)`, never an inline
`if dialect == "mssql"` branch. `PercentileContSelectItem` was added to
the shared `_AGGREGATE_SELECT_ITEM_TYPES` tuple item 81 introduced
specifically so a fourth aggregate type wouldn't require touching three
files by hand — confirming that refactor's payoff on its first real use.
`schema_validation._select_aliases` gained a `_percentile_cont_alias`
branch. `audit/events.py`'s `_select_shape` gained its
`PercentileContSelectItem` branch (`{"kind": "percentile_cont", "column":
..., "fraction": ...}`) proactively in this same change, per the item
80/81 lesson about not deferring it.

Because SQLite can't stand in for `percentile_cont` any more than it can
for `array_agg`, this gets no `test_sqlite_end_to_end.py` coverage.
Instead it gets genuine coverage against a real Postgres
(`tests/integration/test_postgres_percentile_cont.py`, `pytest -m
postgres_live`): groups `examples/demo_db`'s seeded orders by customer,
`percentile_cont(0.5)` on `total_amount`, and asserts customer 2's median
(3 orders seeded: 75.00, 249.00, 310.25 — an odd count, so the continuous-
interpolation median lands exactly on the middle sorted value with no
floating-point interpolation between two rows to account for) equals
exactly `249.00`. Verified directly that the seeded 3-order set for
customer 2 isn't one of `examples/demo_db/schema.py`'s explicitly pinned
rows (only customer 1's 2-order count is called out there) before relying
on it. `tests/unit/test_compiler.py` also gained a dedicated MSSQL
rejection test, same shape as item 81's. `tests/unit/
test_compiler_properties.py`'s `_aggregate_queries` fuzzer strategy now
draws a 4-way choice among plain aggregates/`string_agg`/`array_agg`/
`percentile_cont` (fraction drawn from `[0.0, 1.0]`); re-confirmed this
file still only ever compiles at the default Postgres dialect before
extending it, so the fuzzer never hits either deliberate MSSQL raise.

**Explicitly out of scope, not silently dropped** (same v1-bound reasoning
items 75/80/81 used): descending order inside `WITHIN GROUP`, Postgres's
multi-fraction array form (`percentile_cont(array[...])`), and
`PARTITION BY`. All reachable by a calling agent composing its own
multi-query workaround if genuinely needed — not this v1's job to expose
immediately.

**Effort: S**, same "one adapter method, mostly zero new compiler
branches" shape as items 74/75/80/81, plus one new numeric-range AST
validator and reusing the `_AGGREGATE_SELECT_ITEM_TYPES` constant item 81
had already generalized for a fourth type.

**Why it matters:** real analytics asks ("what's the median order value
per customer, not just the average") are now expressible on Postgres; this
also closes out the last aggregate function explicitly flagged as
deferred across items 75/80/81, and demonstrates that CLAUDE.md's
no-forced-parity principle produces *differentiated* reasoning per
dialect-capability gap (array/collection-type absence vs.
window-function-only restriction vs. no-ordered-set-support-at-all) rather
than a single boilerplate justification copy-pasted three times.

### 83. Query-template authoring UX: slot self-consistency, readable dry-run, and on-demand live-schema check ✅ DONE

Three refinements to query-template authoring (item 48), surfaced by reviewing
the admin config dry-run — the theme is *convenience and reliability for
whoever authors templates* (human or not), not new security or correctness (the
run-time pipeline already protected every case here).

**1. Parameter-slot self-consistency (caught at load/dry-run).** A slot whose
`allowed_values` or `default` contradicted its declared `type`/bounds (e.g.
`type: integer` with string `allowed_values`, or a `default` outside its own
`min`/`max`/`max_length`/`allowed_values`) previously passed validation, then
could never be invoked — every supplied value failed the enum. `TemplateParameter`
now rejects such slots when the model loads (so `querygate-validate-config` and
the admin dry-run both catch them). The type/bounds primitive is factored into
one `templates/models.scalar_type_error()` shared by both the model's
self-consistency check and `binding._coerce_scalar`'s run-time value check, so
the two can never disagree about what a slot accepts; binding kept its exact
runtime messages.

**2. Readable, attributed config dry-run errors.** The governance dry-run wrote
each candidate to a throwaway temp dir and surfaced validation errors prefixed
with that temp path plus raw pydantic boilerplate (the docs URL, the
`[type=..., input_value=..., input_type=...]` tail). `admin.service._humanize_
validation_errors` now re-attributes each error to its logical document name
(`templates.yaml: …`, never the temp path) and strips the noise, keeping the
human message and field path. The admin dry-run panel wraps multi-line
messages and states explicitly that column/table existence is checked
separately, so the offline dry-run's scope is no longer a surprise. (The CLI's
own verbose `validate_config` output is unchanged — this is admin-surface
only.)

**3. On-demand live-schema check.** The offline dry-run deliberately never
opens a DB session (same posture as `explain`/cost-estimation), so it can't
verify a referenced *column or table exists*. New `POST /admin/config/
check-template-schema` (+ the admin UI's "Check templates vs. schema" button,
co-located in the Change-set dry-run panel) fills that gap explicitly:
`admin.service.check_template_schema` resolves the draft templates (inheriting
the active version's like the rest of the config plane), binds each with dummy
values (`binding.dummy_bound_query`, extracted from `validate_template_structure`
— placeholder values never change which identifiers a query references), and
runs the **same** `validate_schema` the real pipeline uses (with `principal=None`,
so the connection resolves by deployment visibility, not a query-time principal
policy) against the *currently-live* registry. It returns per-template
`TemplateSchemaCheck` results: `ok`, `issues` (a missing column via
`QueryValidationError`, or a missing table via `sa.exc.NoSuchTableError`, with
the specific name), `connection_unavailable` (target isn't a live enabled
connection), or `unreachable`. It is **best-effort by design** — any
DB/reflection failure (`sa.exc.SQLAlchemyError`) becomes `unreachable`, never a
`500` and never echoing a raw driver error — so the fast dry-run stays
decoupled from database availability while authors still get pre-stage schema
feedback on demand. Gated by both `admin:config:read` + `admin:config:write`
(reveals live schema detail while resolving caller content, exactly `/simulate`
and `/diff`'s reasoning), audited as a redaction-safe `check_template_schema`
event, and documented as QG-27 in `docs/THREAT_MODEL.md`.

**Why the split (Decision Log, `docs/PRODUCT_GUIDE.md`):** the rejected
alternative was always-on live schema validation folded into the dry-run.
Declined because it would couple every config validation to database
availability (a slow/down DB would block staging otherwise-valid config), for a
marginal convenience gain over one explicit button. Column/table existence can
also change after authoring (a later schema change), so the check is
point-in-time by nature — an on-demand action fits that better than an implied
guarantee.

**Coverage.** `tests/unit/test_query_templates.py` (slot self-consistency:
`allowed_values`/`default` vs type, numeric bounds, enum membership, list-default
elements, plus a valid slot accepted); `tests/unit/test_admin_service.py`
(the humanizer strips temp paths + pydantic noise); `tests/unit/
test_template_schema_check.py` (the per-template classifier: ok / missing
column / missing table / unreachable-is-best-effort-and-never-leaks-the-driver-
error / unknown-connection / structural-error, with the reflection seam patched
per the conftest gotcha); `tests/integration/test_admin_config_governance.py`
(the `/validate` endpoint returns a clean attributed slot error; the
`/check-template-schema` endpoint flags a missing column end to end; and a
security-marked test that the endpoint returns `200` — not a `500` — and never
puts the raw driver string/host/credentials in the body when reflection raises,
backing the QG-27 claim and adversarially verified to fail if the raw error is
surfaced); `tests/integration/test_admin_ui.py` (the button + endpoint
reference are served); and both config-scope security tests now assert the new
endpoint requires read *and* write. Verified end-to-end
against a real demo Postgres during development: a valid template → `ok`, a bad
column → `Column 'ghost_amount' not found in table 'orders'`, a missing table →
`table 'ghosts' does not exist`, an unknown connection → `connection_unavailable`.

**Effort: S–M**, mostly reuse: one shared scalar validator + one error
humanizer + one endpoint that composes `dummy_bound_query` with the existing
`validate_schema`, plus the admin-UI button/panel.

### 84. Structured catalog authoring UI (human-curated entries through the governance queue) ✅ DONE

**Effort: M (2–4 days).** Vertical slice — one new backend proposal source,
one new endpoint, one new UI panel. Nearly everything is composed from bricks
that already exist (governance quarantine → approve → publish → rollback,
audit, `CatalogDraftContent` validators); do not build a second catalog file,
store, or mutation path.

**Why it matters:** catalog.yaml already carries human-curated content (the
`verified` source class), but the *only* way a human authors it today is by
hand-typing raw YAML into the Change-set panel's catalog.yaml `<textarea>`
(`admin_ui/index.html` `#document-editor`) — no field labels, no key hints,
no schema help, and it flows through `ConfigVersionStore`, whose snapshot copy
CLAUDE.md explicitly warns diverges from the live `CATALOG_FILE` that catalog
governance writes to. This item gives a human a guided "pick a table → fill in
description/aliases/relationships → submit" form whose output is routed through
the **catalog governance path** (`CatalogFileRepository`), giving it the same
staged → reviewed → published → rollback safety and actor-attributed audit the
agent-generated proposals already get. It closes the UX gap and the
correctness gap (right versioning path) at once.

**Design decisions (resolved):**
- Human-authored entries flow through the **governance queue**, NOT the
  Change-set/`ConfigVersionStore` catalog.yaml tab. This is the CLAUDE.md-
  compliant path (do not route catalog content through `admin/store`).
- Authoring is gated on a **distinct `catalog:author` scope**, separate from
  `catalog:review`, so a deployment *can* keep authoring and approving as
  different principals — but this is enforced by **scope**, not identity.
- **Self-approval is allowed.** Separation of duties is permission-based: a
  principal that holds the approve/publish scope (`catalog:review`) may approve
  and publish its own manual proposal. There is NO author≠approver identity
  check. A deployment that wants strict separation grants `catalog:author` and
  `catalog:review` to different principals; the scopes are the gate.

**What shipped:**
1. **New proposal source.** `KnowledgeSourceClass.MANUAL` (a proposal-only
   class, precedence-mapped to VERIFIED for its computed provenance field) and
   `CatalogDraftProposal._quarantine_inferred_content` extended to admit
   `source_class = manual` alongside `inferred`/`learned`. A manual proposal
   stays quarantined as DRAFT until published, at which point
   `publish_proposal` mints a fresh `verified` entry — publishable-as-verified,
   never auto-trusted or auto-indexed. The `replacement_decision` downgrade
   guard is unchanged (verified-over-verified allowed, non-verified/manual
   candidate over verified rejected).
2. **New backend mutation.** `governance.create_manual_proposal` composes a
   `CatalogDraftTarget` + `CatalogDraftContent`, validates the target against
   the current schema snapshot when one exists, and appends the proposal plus a
   single-proposal `provider_mode="manual"` generation record (the store
   invariant requires every proposal to reference a generation record) — all
   through the same `CatalogFileRepository` lock (`_apply`/`_run_mutation`). No
   new file, no new store.
3. **New endpoint** `POST /{connection}/proposals` (manual create) in
   `api/catalog_governance_routes.py`, gated on `catalog:author`, emitting a
   redaction-safe `catalog.governance` audit event (`action="manual_create"`).
   Body: `{object_type, table, column?, to_table?, to_column?, description?,
   aliases?, default_aggregation?}`. Two deliberate deviations from the item's
   sketch: `sensitivity` is *not* accepted (a draft's content model
   structurally has no sensitivity/sampling field — a verified-only,
   direct-edit concern — and accepting it would weaken the quarantine
   invariant), and relationship hints are authored as an `object_type =
   relationship` target rather than a content-embedded list.
4. **New "Curate" UI panel** (`admin_ui/index.html` + `app.js`): connection →
   object type → table → optional column → form fields with inline help, the
   currently-published entry shown beside the form via the existing compare
   pattern, submit → manual pending proposal that surfaces in the existing
   Catalog-review workbench (a `manual` source filter was added). Panel/nav
   visibility gated on `catalog:author`.
5. **No identity-based approval check** — separation of duties is purely
   scope-driven; actor attribution (`created_by`/`approved_by`) retained.
6. **Tests.** Unit (`test_catalog_governance.py`): quarantine validator admits
   manual, downgrade guard holds, create→approve→publish→verified, self-approve
   allowed, guardrails (default_aggregation-on-non-table, absent target). Route
   (`test_catalog_governance_rest.py`): `catalog:author` required to create; a
   principal holding both scopes can create AND self-approve/publish; the
   published entry lands as `verified` in the live `CATALOG_FILE` and no
   `config_versions/**/catalog.yaml` path is introduced.
7. **Scope wiring.** `catalog:author` added to `core/scopes.py`;
   `describe_my_querygate_access` reports it automatically (it lists the
   principal's scopes verbatim).

**Docs:** `docs/PRODUCT_GUIDE.md` gained a "Human-authored catalog entries"
subsection and a Decision Log entry (new `manual` source + governance-path
rationale + scope-based separation of duties).

### 85. Domain-separated admin UI (group the 9 flat views into Policy / Catalog / Templates / Connections / Releases) ✅ DONE

**Effort: M.** Nav + layout refactor of the existing single-page admin UI
(`admin_ui/index.html` + a single ~75KB `app.js`); reuses every existing
per-view render function and scope gate unchanged. Not a rewrite of view
logic. Done after item 84 so the new Curate panel folds into the Catalog
domain rather than staying a flat nav slot.

**Why it mattered:** the admin UI had grown to ten flat nav items (Overview,
Schema review, Policy designer, Change set, Versions, Audit, Catalog review,
Curate catalog, Connection health, Query templates) and would keep growing as
structured authoring lands per domain. Grouping by domain makes the surface
navigable and gives each domain a coherent home for its authoring + review +
history sub-panels.

**What shipped:**
1. **Seven domains, secondary tab bar.** The sidebar lists seven domains —
   **Overview** (standalone), **Connections** (Schema review + Connection
   health), **Policy** (Designer, with its safe-start presets and simulation
   as in-view sections), **Catalog** (Curate + Review proposals + Versions &
   rollback), **Templates** (Query templates), **Releases** (Change set +
   Versions), and **Audit** (the redaction-safe audit trail, standalone). A
   secondary tab bar (`#domain-tabs`) renders inside any domain with more than
   one view; single-tab domains show no bar. (The item's original sketch put
   Audit under Releases; it was promoted to its own top-level domain since the
   audit trail is a read-only cross-cutting view, not part of the change-set
   staging flow.)
2. **Two-level routing, unchanged render fns.** `app.js` gained an ordered
   `navModel` (domain → tabs, each tab carrying its `view`/`section`/optional
   `panel`/optional `scope`) plus `viewToTab`/`viewToDomain` reverse lookups.
   `showView(name)` was extended from a flat lookup to resolve a view's owning
   domain and section, render the domain tab bar, and toggle sub-panels — while
   keeping its signature, the deep-linkable `/admin/#<view>` hash, and every
   existing lazy-load trigger. `viewMeta` stays the view→`[kicker, title]` map.
   No per-view render function or `hasScope`/`canRead`/`canWrite` gate changed.
3. **Catalog's third tab is a sub-panel toggle, not a new view.** Because the
   proposal list (`loadCatalogProposals`) and version history
   (`loadCatalogVersions`) both read the same `#catalog-connection` selector,
   the connection selector was lifted into a shared header row and the review
   vs. versions content wrapped in `[data-catalog-panel]` blocks that the two
   tabs (`catalog` / `catalog-versions`) toggle within the one `view-catalog`
   section — giving the required three-tab Catalog with zero change to any
   render function.
4. **Scope gating preserved.** The `catalog:author`-gated Curate panel became a
   Catalog tab that `renderDomainTabs` omits when the principal lacks the
   scope (the same gate the old standalone `#nav-curate` button used); `connect`
   re-renders the active domain's tabs once access is known.
5. **The structural constraint made legible, not papered over.** Each domain's
   tab bar shows a one-line release signal: Connections/Policy/Templates read
   *"edits stage into the shared release"* (they feed the bundled
   `ConfigVersionStore` atomic version, applied in Releases); Catalog reads
   *"self-contained governance"* (its own `CatalogFileRepository` versioning,
   author → review → publish → rollback in-domain); Releases reads *"shared
   atomic version — policy, connections, catalog.yaml and templates apply
   together."*
6. **`app.js` kept as one file.** A per-domain module split was considered and
   declined: the change never touches the ~1400 lines of view-render logic, so
   splitting would add risk without reducing it.

**Coverage:** `tests/integration/test_admin_ui.py` updated for the new
nav labels (Catalog is a `data-domain`; the tab labels and the self-contained
release copy are asserted in the served `app.js`). Verified end-to-end by
driving the real `index.html`+`app.js` through a DOM harness (36 checks:
six-domain sidebar, per-domain tab bars and release notes, catalog sub-panel
toggle, `#catalog-versions` deep-link, and `catalog:author` tab gating
with/without the scope).

**Docs:** `docs/PRODUCT_GUIDE.md`'s admin-surface section gained a
"How the control plane is organized" subsection, plus a Decision Log entry on
surfacing the shared-release vs. self-contained-catalog split (and the
nav-only / single-file decisions).

### 86. MCP transport request-body size/depth guard ✅ DONE

**Effort: S.** Closes the REST/MCP status-code asymmetry surfaced by item 36
phase 2a: the REST surface rejects a malformed body (oversized, or nested past
the JSON parser's recursion guard) with a clean 4xx, but the mounted MCP
Streamable-HTTP transport's own `json.loads(body)` raised `RecursionError` and
surfaced it as a *handled but HTTP 500* JSON-RPC internal error (`-32603`,
leak-free). A robustness/consistency gap, not a disclosure — deferred by item
36 to its own deliberate pass because a body cap is a judgment call.

**What shipped:**
1. **`mcp/transport_guard.py` — `MCPRequestGuardMiddleware`.** A small ASGI
   wrapper mounted *around* the auth wrapper (outermost, so it runs before auth
   and before the transport's `json.loads`). HTTP requests are buffered up to
   the byte cap; a body past the cap is a clean `413`, a body nested past the
   depth cap is a clean `400`, and anything within both caps is replayed
   downstream unchanged. Non-HTTP scopes (lifespan, the GET SSE stream,
   websockets) and `http.disconnect` pass straight through.
2. **Cheap pre-parse depth scan.** `_structural_depth_exceeds` is an O(n) scan
   over the raw bytes that counts structural `{`/`[` nesting only — characters
   inside JSON string literals (respecting `\` escapes) are skipped, so a
   string value that merely *contains* brackets can't trip the guard — and
   short-circuits the instant the threshold is crossed, so a malicious deep
   body is rejected after a few hundred bytes rather than parsed.
3. **Two configurable `AppConfig` fields** (`mcp_max_request_bytes`, default
   4 MiB; `mcp_max_request_depth`, default 100), both far above any legitimate
   batch (policy caps `max_where_depth` at 5), documented in `.env.example`.
4. **Rejections are redaction-safe:** a generic `{"error":{"code","message"}}`
   body and a `mcp.transport.rejected` audit log carrying only the reason code
   and path — never body content.

**Coverage** (`tests/security/test_malformed_input_fuzzing.py`): the phase-2a
deep-body test was flipped from "handled 500, documents the asymmetry" to
`test_mcp_deeply_nested_argument_body_is_rejected_as_a_clean_4xx`, and a new
`test_mcp_oversized_body_is_rejected_as_413_before_execution` covers the byte
cap; both assert the service was never dispatched and nothing leaked. The
existing valid-call MCP tests exercise the replay path unchanged. Verified
end-to-end through the real mounted ASGI transport via httpx (not the tool
internals): 174 security + credential-redaction tests green.

**Docs:** `docs/THREAT_MODEL.md` §8 updated from residual-risk to resolved;
`docs/PRODUCT_GUIDE.md`'s MCP-transport section documents the outer guard
layer.

### 87. Extend structured authoring to Policy and Templates (same form→validated-YAML→staged pattern) ✅ DONE

**Effort: M–L.** Brought the item-84 "guided form instead of raw YAML" pattern
to the config documents that still lacked it, feeding the shared
`ConfigVersionStore` change-set (Releases domain) — not a per-domain governance
publish like catalog.

**Why it mattered:** authoring a query template meant hand-typing it into the
`templates.yaml` change-set `<textarea>` — knowing the exact keys and the
parameter-slot schema (type / required / default / numeric & length bounds /
`allowed_values`, with self-consistency rules). Policy already had the visual
designer for this; templates did not.

**What shipped:**
1. **Policy was already covered.** The visual policy designer
   (`applyDesigner` → `/admin/ui/policy/render`) already composes a validated
   `Policy` layer into the draft `policy.yaml`; item 85 placed it in the Policy
   domain. No new policy authoring was needed — the deliverable was templates.
2. **Two support endpoints** in `api/admin_ui_routes.py`, mirroring
   `policy/parse`+`policy/render`: `POST /admin/ui/templates/parse` (read-or-
   write scope) turns a `templates.yaml` body into a structured document, and
   `POST /admin/ui/templates/render` (write scope) validates a document against
   the shared `QueryTemplateFile` model and renders it back to YAML. The schema
   authority is the same model the loader/dry-run uses, so a bad slot (numeric
   bound on a string type, duplicate id) is a clean 422 at compose time.
3. **Templates-domain authoring form** (`admin_ui/index.html` + `app.js`):
   opened on demand from a "+ New template" button (not an always-on form),
   with template id, connection, description, a repeatable parameter-slot
   builder (name/type/required/list + min/max/max_length/allowed_values), and
   the `StructuredQuery` skeleton as validated JSON. On submit it composes a
   template, merges it by id into the current `templates.yaml` draft through the
   parse→render round-trip, marks the change-set dirty, and auto-closes/resets —
   the change then flows through the existing validate → stage → apply →
   rollback. Gated on `admin:config:write` (a read-only session sees an
   explanatory gate). Each browse card is also clickable to expand a read-only
   view of that template's query skeleton — read from the admin's own
   `templates.yaml` (the agent-facing `/query-templates` projection omits the
   AST by design). It is server-gated on `admin:config:read`/`write` (the
   `/admin/ui/templates/parse` scope, so a no-config-scope caller gets a 403
   and a read-scope prompt, never the AST), reads only content the caller
   already loaded via `admin:config:read`, is retained in memory only for
   templates in the caller's visibility-filtered browse list, and is
   HTML-escaped — no credentials/secrets/rows are ever in a query skeleton, so
   it discloses nothing beyond what config-read already exposes.
4. **The query skeleton stays JSON, deliberately** — the parameter slots are
   the schema-heavy, error-prone part and got the structured builder; a full
   visual AST builder would be a separate large surface (already served by the
   typed query-builder SDK and raw editing). Raw `templates.yaml` editing
   remains as the escape hatch; the form is additive.

**Coverage:** `tests/integration/test_admin_ui.py` gained a parse/render
round-trip + slot-validation-rejection test and a write-scope-required test.
Verified end-to-end by driving the real `index.html`+`app.js` through a DOM
harness (15 checks: form scope-gating, dynamic parameter rows, compose →
parse → merge → render with typed coercion, and the draft going dirty).

**Docs:** `docs/PRODUCT_GUIDE.md`'s query-templates section documents the
guided form; a Decision Log entry records the shared-release routing, the
JSON-skeleton scope call, and that Policy was already covered.

### 88. Minimum aggregation group size (k-anonymity guardrail) ✅ DONE

**Shipped:** A new `Policy.min_group_size` cap (`policy/models.py`) that closes
the direct, single-query form of the aggregate-differencing residual that item
55's design note flagged as R3 (`docs/INFERENCE_RISKS.md`). When set (floor 2;
`None` disables), the compiler (`compiler/sqlalchemy_compiler.py`) injects
`HAVING count(*) >= k` into every **aggregate** query — grouped or
single-implicit-group — so any result group backed by fewer than *k* underlying
rows is suppressed. A caller can no longer aggregate over a razor-thin filter to
single out an individual (`count(*) WHERE id = X` returns nothing when fewer
than *k* rows match). It is the aggregate analog of a mandatory row filter:
policy-driven, injected, non-removable, and it only touches aggregate queries —
plain row reads remain governed by mandatory row filters, not group size.

**Scope (deliberate):** this closes single-query singling-out, **not**
multi-query differencing (isolating an individual by subtracting two
independently-compliant aggregates), which needs query-set auditing or
differential privacy — out of scope and documented as still-residual in
`docs/INFERENCE_RISKS.md`. No new AST surface; `min_group_size` is a policy cap,
loaded generically from `policy.yaml` like every other cap.

**Coverage:** compiler unit tests (`test_compiler.py::TestMinGroupSize` — HAVING
injection on grouped/single-group aggregates, no-op on plain selects, combines
with caller HAVING, `None` no-op), real end-to-end suppression against SQLite
(`test_sqlite_end_to_end.py` — a single-customer country group and a
single-row filtered count are suppressed; the whole-table count is returned),
a security test tying the closure back to item 55's R3
(`test_adversarial_security.py`), and policy-model validation
(`test_policy_models.py` — default `None`, floor of 2).

**Why it matters:** item 55 proved the direct column-reference defenses are
complete and documented the residuals it couldn't close. R3 (no minimum group
size) was the one residual with a bounded, well-precedented fix — this item
builds it, turning a documented gap into an opt-in enforced guardrail without
overclaiming (multi-query differencing stays honestly out of scope).

### 90. Delegated agent identity (on-behalf-of) carried into policy + dual-identity audit ✅ DONE

**Effort: L. Priority: high (time-sensitive — see below). Feature ref: F1.**

**Status:** Phase 1 (delegated-identity attribution) and phase 2 (MCP OAuth
resource-server conformance) both shipped.
- **Phase 1 ✅ DONE — delegated-identity attribution (the moat).** `core/auth.py`
  gains an `Actor` delegation chain on `Principal` (`subject` = the human,
  `actor` = the agent, with a nested `delegated_by` chain and
  `is_delegated`/`actor_subject`/`delegation_chain` helpers). `core/jwt_auth.py`
  maps a verified token's RFC 8693 `act` claim into that chain (configurable via
  `jwt_act_claim`, depth-bounded, malformed-`act`-safe). Because per-principal
  policy resolution already keys off `Principal.subject`
  (`policy/loader.PolicyStore.get`), mapping the human to `subject` makes **the
  human's** policy and `mandatory_row_filters` apply with zero change to the
  policy layer. `audit/events.AuditEvent` gains `actor_id` + `delegation_chain`
  (identities only — redaction guarantee unchanged), threaded through
  `audit/logger.audit_query` and both `execution/service.py` audit call sites, so
  every query records "Agent A on behalf of User Z under the human's policy."
  Covered by unit tests in `test_auth.py`, `test_jwt_auth.py`,
  `test_policy_loader.py` (human's-policy-wins), and `test_audit.py`
  (dual-identity event, redaction-safe).
- **Phase 2 ✅ DONE — MCP OAuth resource-server conformance.** The mounted MCP
  surface is now an opt-in OAuth 2.0 resource server per the MCP `2026-07-28`
  spec, gated behind `mcp_oauth_resource_server_enabled` (requires `jwt_enabled`;
  off by default, so existing deployments are unchanged). `mcp/oauth_metadata.py`
  publishes RFC 9728 protected-resource metadata (`resource`,
  `authorization_servers`, `bearer_methods_supported`, optional
  `scopes_supported`/`resource_documentation`), served unauthenticated from the
  main app at `/.well-known/oauth-protected-resource<MCP_MOUNT_PATH>` (plus a
  root alias). `mcp/auth.MCPAuthMiddleware` enforces RFC 8707 audience binding on
  verified tokens (a JWT's `aud` must include `mcp_resource_identifier`, blocking
  confused-deputy reuse of a token minted for another audience — a `str` or
  array `aud` is honored, absence fails closed) and a configurable required-scope
  gate, and answers a missing/invalid credential or insufficient scope with an
  RFC 6750 `WWW-Authenticate: Bearer …, resource_metadata="…"` challenge
  (`invalid_token` 401 / `insufficient_scope` 403) so a client can perform the
  RFC 8693 token exchange / step-up. Static API keys skip audience binding (an
  out-of-band trust with no `aud`) but still pass the scope gate. New config +
  `.env.example` entries; covered by `tests/unit/test_mcp_oauth_rs.py` (metadata
  shape/route, audience binding, scope step-up, unauthenticated challenge, and
  config validation). **Not in scope / deferred:** extending actor attribution
  to the admin-surface audit events
  (`ConfigChangeEvent`/`CatalogGovernanceEvent`/`ConnectionProbeEvent`) — delegated
  *admin* actions aren't a current requirement; open a fresh item if they become
  one.

**Why it matters (competitive pressure):** The identity market has fully solved
"delegated identity → API" and is standardizing it *this month* — the MCP
`2026-07-28` spec release candidate (verified 2026-07-22: "the largest revision
of the protocol since launch") rewrites authorization around a two-identity
delegated model (the agent application **and** the human on whose behalf it
acts), RFC 8707 audience binding, and RFC 8693 token exchange. But the
independent competitive finding stands: *almost nobody carries that principal
into the database session* — only Databricks (Unity Catalog on-behalf-of) and
Snowflake (caller-rights) do, and only because they own both the gateway and
the engine. Roughly two-thirds of orgs cannot attribute an agent action to a
human. This is the single hottest enterprise requirement with the emptiest
data-layer, and the standard formalizing it lands now — QueryGate is
warehouse-agnostic, so it can deliver OBO-grade attribution against
Postgres/MSSQL where the two warehouse vendors cannot reach.

**What to do:** Accept a delegation credential (agent acting *on behalf of* a
human), resolve **both** identities, apply the **human's** resolved policy and
`mandatory_row_filters` to the query, and record **both** in the audit event
("Agent A on behalf of User Z under Policy Y"). Implement the MCP authorization
spec as a proper OAuth resource server: RFC 9728 protected-resource metadata,
mandatory RFC 8707 audience validation, `insufficient_scope` step-up. Extend
`core/auth.py`'s `Principal` to a delegation chain (actor + subject) — a natural
extension of machinery that already drives per-principal policy — and add
actor+subject fields to `audit/events.py`. Touches `core/auth.py`,
`core/jwt_auth.py`, `mcp/auth.py`, `api/auth.py`, `audit/events.py`. Extends
items 8/10/21; pairs with item 91. **Invariant:** none at risk — this
strengthens attribution.

### 91. Tamper-evident hash-chained audit ledger + per-query compliance receipts ✅ DONE

**Feature ref: F5. Completes the Proof pillar (`docs/business/NORTH_STAR.md`)
with item 90 — attribution (who, on whose behalf, under which policy) plus
tamper-evidence (and the record proving it wasn't edited).**

**Shipped.** An optional tamper-evident audit ledger that chains at the
**sink/envelope layer**, so the redaction guarantee (QG-12) is preserved by
construction — no new field is added to any event body.

- `audit/ledger.py` (new): the chain primitives and verify-only tooling.
  `LedgerRecord` is a `{seq, prev_hash, event, hash}` envelope around one
  unmodified event; `compute_record_hash` binds a record's position, its
  predecessor's hash, and its event via canonical (sorted-key) JSON, using
  **HMAC-SHA256** when a key is configured and **SHA-256** otherwise.
  `verify_chain()` walks a ledger and detects edits (record hash mismatch),
  deletion/insertion/reordering (sequence gap or `prev_hash` linkage break), a
  malformed line (fails closed), and — with `expected_head` — records dropped
  from the *end*. A rotated ledger legitimately starting past genesis is
  accepted on its incoming link but every subsequent link is still enforced.
  `Receipt` + `build_receipt`/`verify_receipt`/`extract_receipt_for_event_id`
  produce a portable, self-contained per-query compliance receipt (one event's
  chain position, re-verifiable on its own without the rest of the ledger).
- `audit/sinks.py`: `HashChainedAuditSink` appends chain-envelope JSONL with the
  same `0o600`/append-only/optional-`fsync` posture as `JsonlAuditSink`, and
  **recovers the chain head on startup** (tail-reads the last record) so a
  process restart continues the chain instead of forking it; it refuses to
  resume a corrupt ledger rather than silently forking. `configure_audit_sink`
  gained a `jsonl_chained` backend and an optional `ledger_hmac_key`.
- `core/config.py`: `AuditSinkBackend.JSONL_CHAINED` + `audit_ledger_hmac_key`
  (never logged); `api/app.py` passes the key through at startup.
- `admin/anomaly.py`: the item-59 audit reader transparently unwraps a chain
  envelope, so behavioral anomaly surfacing keeps working under
  `jsonl_chained` — one tolerant reader, both formats.
- `querygate-audit` CLI (`audit_cli.py`, new poetry script): `verify` (exit 0
  iff intact; `--hmac-key-env` reads the key from an env var so it never lands
  in shell history/process listing; `--expected-head` for tail-truncation) and
  `receipt <event_id>` (prints the portable receipt).

**Integrity model (documented honestly).** Keyed → HMAC-SHA256, unforgeable by
anyone with file access but not the key. Unkeyed → SHA-256, which still detects
corruption/reorder/mid-file deletion, with full tamper-evidence resting on
externally anchoring the head hash. One logical writer owns the chain head, so
the ledger assumes a single replica (or a per-replica ledger file) — an honest
constraint, not a distributed-consensus ledger nobody asked for. Chaining is
verify-only; nothing in the request pipeline reads it.

**Coverage.** `tests/unit/test_audit_ledger.py` (deterministic/keyed hashing;
intact keyed+unkeyed chains; genesis-link enforcement; detection of edit,
deletion, reorder, forged insertion, tail truncation via anchor, malformed
line; rotated-ledger handling; receipt round-trip keyed/unkeyed + tamper +
wrong-key/no-key; sink writes a verifiable chain; head recovery across restart;
refusal to resume a corrupt ledger; the redaction invariant that the envelope
adds only seq/prev_hash/hash; backend selection). `tests/unit/test_audit_cli.py`
(verify OK/tamper/missing-file exit codes, keyed verify from env, receipt
extraction, dispatch). `tests/unit/test_anomaly.py` gained a chained-ledger read
test. `docs/THREAT_MODEL.md` gained QG-32 and an updated audit-durability
residual-risk note; `docs/PRODUCT_GUIDE.md` gained a capability subsection and a
Decision Log entry (why chaining lives at the envelope layer); README and
`.env.example` document the backend, key, and CLI.

**Decision Log:** why chaining is at the sink/envelope layer, not on the event
model — see `docs/PRODUCT_GUIDE.md` (2026-07-22 entry).

**Why it matters:** EU AI Act Art. 12/26 (automatic, tamper-evident event
logging retained ≥6 months), DORA, and ISO 42001 A.6.2.8 all want a
tamper-evident, per-human-attributed record of agent action. Competitors offer
platform logs; none offers a tamper-evident, per-human-attributed, portable
receipt *at the query layer*. Combined with item 90 this is the "prove to your
auditor exactly what every agent did, on whose behalf, under which policy, and
that the record is intact" artifact — the literal buying question for the
fintech/healthcare ICP.

### 92. In-query human-in-the-loop approval for sensitive/expensive reads (MCP elicitation step-up) ✅ DONE

**Phase 2 sensitivity-label trigger shipped:** `Policy.approval_sensitivities`
(a list of catalog `SensitivityClass` labels, default empty/off) makes a query
that references a column — or its table — carrying one of those labels require
approval **regardless of estimated size** and **dialect-agnostically** (no cost
estimate needed, so it works on MSSQL). `execution/approval.py`'s
`sensitivity_approval_reasons` enumerates every referenced column via the single
canonical AST visitor (`iter_column_refs`, item 96 — so a sensitive column in a
`where`/join/having/etc. triggers it too, not just `select`), resolves each to
its physical table, and reads only the descriptive catalog's static label (never
a row value — the catalog stays descriptive). The gate now combines both
triggers into one decision (`_enforce_approval_gate`), so a single approval token
covers whatever tripped it; `Policy.approval_cost_gate_enabled` vs.
`approval_gate_enabled` keep the estimate needed only for the cost trigger.
Covered by 6 added tests in `tests/unit/test_approval.py`.

**Phase 1 shipped (cost/row-estimate trigger + stateless HMAC approval-token
grant, REST):** `execution/approval.py` is the gate's decision core —
`approval_required_reasons(estimate, policy)` (reusing the estimate the pipeline
already computes), `query_fingerprint(query)` (canonical SHA-256 of the AST), and
`issue_approval_token`/`verify_approval_token` (HMAC-SHA256, fail-closed on any
missing-key/forged/expired/wrong-fingerprint/malformed input, constant-time
compare). New `Policy.approval_max_estimated_rows`/`approval_max_estimated_cost`
(opt-in, default off; a softer gate *below* the hard `max_estimated_*` caps) and
`Policy.approval_gate_enabled`/`estimate_needed`. The gate runs in
`execution/service.py`'s `execute()` right after the cost estimate (Postgres
only, same estimate source), raising `ApprovalRequiredError` (a
`PolicyViolationError`; `metrics.classify_rejection` → `approval_required`) when
triggered and no valid token is supplied, admitting when a token bound to that
exact query is supplied (audited `approval.required`/`approval.granted`). REST:
`execute` accepts an `X-QueryGate-Approval` header; `ApprovalRequiredError` maps
to **428 Precondition Required** with `{fingerprint, reasons}`; a new
scope-gated `POST /{connection}/query/approve` (scope `query:approve`, in the
scope catalog + a "Query Approver" role bundle) issues the token. Requires
`AppConfig.approval_token_hmac_key` (fail-closed 503 if unset). Covered by
`tests/unit/test_approval.py` (22 tests: token forge/replay/expiry/wrong-key/
malformed, trigger boundary, gate seam, e2e pause→approve→resubmit, endpoint
scope/503). **Invariant preserved:** read-only, AST-only, opt-in — a
default-config deployment is byte-for-byte unchanged.

**Batch approval tokens shipped (REST):** `execute_many` takes an
`approval_tokens` map (query fingerprint -> the signed token from
`POST /query/approve`) and threads each query's token into its `execute()`
call, so an approval-gated query can run inside a batch. `BatchQueryRequest`
carries the map (`POST /query/batch`). A query with no matching token stays
fail-closed — its `ApprovalRequiredError` surfaces as that batch item's `error`
without dropping the rest — and each token is still verified against its own
query's fingerprint in `execute()`, so it can't be replayed onto another query
in the same batch. Covered by 3 tests (2 service-level in `test_approval.py`, 1
route-level in `test_rest_api.py`).

**MCP elicitation channel shipped (the interactive step-up):** over MCP, a query
that trips the gate is approved **in-session** via `Context.elicit` instead of
the out-of-band REST token round-trip. `mcp/tools/query.py`'s
`_elicitation_resolver` builds an `ApprovalResolver` (a narrow async callback the
transport-agnostic `execute_many` calls when a query raises
`ApprovalRequiredError` and no pre-supplied token covers it): it elicits a
one-field `approve` form from the client's human and, only on an explicit
accept+approve, mints the same fingerprint-bound short-lived HMAC token the REST
flow issues and retries the query once with it. Opt-in and **off by default**
(`AppConfig.mcp_elicitation_approval_enabled`): an elicitation response carries
no authenticated approver identity, so enabling it is a deliberate decision that
the client's human is a trusted approver — separation of duties is preserved by
the medium (the querying agent physically can't answer its own elicitation), and
the minted token records `approver_subject=mcp-elicitation:<caller>` for the
audit trail. Fails closed on a client without an elicitation channel (degrades
to the REST-token rejection) and when the signing key is unset. The resolver
seam keeps all batch/error shaping in `execute_many` while the MCP-specific
interaction stays in `mcp/`; the service never imports MCP. Covered by 8 tests
in `tests/unit/test_mcp_elicitation_approval.py` (opt-in gating, accept/decline/
accept-without-approve/elicitation-error handling, token binding, the
`execute_many` retry seam, and the tool wiring). Decision recorded in
`docs/PRODUCT_GUIDE.md`'s Decision Log (2026-07-23).

**Effort: L. Priority: medium (safety moat; sequence after 90/91). Feature ref: F3.**

**Why it mattered (competitive pressure):** MCP elicitation is the standardized
HITL primitive (and the `2026-07-28` spec revision keeps a first-class
client-interaction path), and Auth0 async-authz (CIBA), PromptQL, and others
gate *writes*. But **reads are the exfiltration leg of the lethal trifecta**
every 2025 incident exploited (Supabase, Neon), and no competitor gates *reads*
on *what the query would actually touch* — because none knows before running
it. QueryGate uniquely already holds the three signals to decide automatically
whether a read needs a human: catalog sensitivity labels (32A), the
pre-execution cost/row estimate (item 26), and the parsed AST. Distinct from
item 42 (four-eyes for *config* changes) and item 35 (capacity waiting) —
neither gates *query execution* on sensitivity/cost. **Invariant:** read-only
posture and AST-only input unchanged; this only adds a pre-execution gate.

### 94. Verify (and, if warranted, enable) prepared-statement plan reuse for template execution ✅ DONE

**Effort: S. Priority: opportunistic optimization — measure first; may close as
"no change needed." Depends on: nothing. Explicitly NOT a pivot.**

**Origin.** Fielding the "stored procedures run more efficiently — why don't we
do that?" question (see `docs/business/COMPETITOR_CUBE.md`, analytics-performance
note). Executing stored procedures is a permanent non-goal — it reopens the
raw-SQL/arbitrary-code path the whole product exists to remove. But the *durable*
SP performance benefit that isn't SP-exclusive — **cached execution plans**
(skip re-parse/re-optimize on repeated calls) — is capturable via
prepared/parameterized statements, and query templates (items 48/87) are the
ideal shape for it: fixed structure, only parameter values vary, so the DB sees
the same parameterized statement every call.

**What to do — measure before touching anything.**
1. Determine whether repeated template (and ad-hoc) reads already get
   server-side prepared-statement plan reuse *today*. This depends on the driver
   and pooling: asyncpg prepares/caches statements per connection automatically;
   psycopg3 only prepares above `prepare_threshold`. We compile with bound
   parameters already (`execution/service.py`; predicate values are never
   inlined) and use a pooled `create_async_engine` (`connections/engine.py`) with
   no explicit prepared-statement config.
2. If the measurement shows we already benefit → close the item as verified, no
   change (document the finding, done).
3. If we're leaving plan reuse on the table → enable/tune it explicitly for the
   fixed-shape template path only (e.g. driver `prepare_threshold`/prepared-stmt
   settings), guarding against known pitfalls: parameter sniffing (a cached plan
   from an unrepresentative first call), and interaction with our per-request
   session guardrails (`SET LOCAL statement_timeout`/`lock_timeout` in
   `connections/dialects.py`) and pool recycling.

**Hard boundaries.** This is NOT: a plan cache we build ourselves, a query-result
cache, pre-aggregation caching, or any SP execution path. It is turning on a
capability the DB driver already has, for queries we already compile with bound
parameters. No new caller surface, no AST change, no invariant impact. The big
analytics "pre-compute the heavy work" win lives in the customer's DB
(materialized views/indexes), which QueryGate already reads as ordinary tables —
that is documentation (the analytics-performance note), not this item.

**Verified 2026-07-28: already benefiting on both dialects, no change
needed.** Measured live against real Postgres and MSSQL (not reasoned from
docs alone):

- **Postgres (asyncpg).** SQLAlchemy 2.0's asyncpg dialect defaults
  `prepared_statement_cache_size=100` and keys its own
  `_prepared_statement_cache` (an LRU on the DBAPI connection wrapper) by exact
  SQL text (`AsyncAdapt_asyncpg_connection._prepare`,
  `sqlalchemy/dialects/postgresql/asyncpg.py`) — a repeat of the same compiled
  text with different bound values reuses the cached `PreparedStatement`
  (server round-trip skipped) rather than re-preparing. Confirmed live by
  identity, not just size: the cached `(prepared_stmt, attributes, timestamp)`
  tuple for the repeated SQL text is the *same object* after a second
  identical-shape execute (a hit returns early without touching the cache),
  and a genuinely different shape adds a new key.
- **MSSQL (pyodbc/aioodbc).** SQL Server's own plan cache reuses a single
  compiled plan across repeated executions of the same parameterized text sent
  via ODBC, independent of any client-side setting. Confirmed live via
  `sys.dm_exec_cached_plans`: three executions of the same shape with
  different bound values produced exactly one cached plan with `usecounts=3`;
  a different shape produced a second, separate plan.
- Both hold *because* QueryGate already compiles every query with bound
  parameters and never inlines predicate values — the exact precondition the
  Postgres cache keys on. **The two dialects are not symmetric evidence,
  measured not assumed:** SQL Server's own "simple parameterization"
  auto-parameterizes even a literal-inlined version of this query shape, so
  the MSSQL test pins the shape-vs-reuse contract (repeat shape -> one reused
  plan) but — unlike the Postgres test — can't by itself catch a regression
  that stopped binding parameters specifically on MSSQL. No driver/pool
  config change was warranted on either dialect.
- Regression-pinned in `tests/integration/test_prepared_statement_reuse.py`
  (`postgres_live` + `mssql_live`) so a future SQLAlchemy/driver upgrade that
  silently disables reuse fails a test instead of only showing up in
  production profiling. Mutation-verified: forcing
  `prepared_statement_cache_size=0` on the Postgres URL breaks the test as
  expected, and a same-key-overwrite mutation (simulating a disguised
  re-prepare hidden behind an unchanged cache size) was caught only after the
  assertion was strengthened from a size check to an identity check — the
  size-only version of this test would have stayed green through a real
  re-prepare-every-call regression.

### 95. Discoverable scope catalog + recommended role bundles for IdP integration ✅ DONE

**Effort: S. Priority: enterprise-SSO adoption enabler for the shipped JWT/OAuth
auth (items 8, 10, 90). Depends on: 10 (JWT), 90 (OAuth resource server). Not a
security-model change — pure discoverability/DX.**

**Origin.** With JWT/JWKS auth (item 10) and the MCP OAuth resource server (item
90) shipped, "bring your IdP" is the scalable multi-user story, but an
authorization server had to be told QueryGate's `scope` vocabulary, which lived
only as constants in `core/scopes.py` — an operator had to reverse-engineer scope
strings and hand-group them into roles.

**What shipped.**

- **`core/scopes.py` is now the single source of truth**, not just constants: a
  structured `SCOPE_CATALOG` (`ScopeInfo(scope, category, gates)` per scope) and
  advisory `ROLE_BUNDLES` (`RoleBundle(name, purpose, scopes)`), plus
  `ALL_SCOPES` derived from the catalog. Six recommended bundles — Analyst
  (no scopes; data-policy-governed), Operator, Config Governor, Catalog Author,
  Catalog Admin, Catalog Data Steward — collectively cover every scope.
- **Part 1 — machine-discoverable.** `mcp/oauth_metadata.py`'s RFC 9728
  `scopes_supported` now advertises `sorted(set(ALL_SCOPES) | mcp_required_scopes)`
  — the full vocabulary an IdP can import — instead of only echoing the required
  scopes. The `mcp_required_scopes` **access gate** (`mcp/auth.py`) is untouched;
  discovery and enforcement are kept as the two distinct concepts they are.
- **Part 2 — human-readable & generated.** `scope_catalog.py` renders a Markdown
  reference; `querygate-scope-catalog` (poetry script, `make scope-catalog`)
  writes `docs/SCOPE_CATALOG.md`. Generated from `core/scopes.py`, never
  hand-maintained.
- **Docs:** README's auth section and PRODUCT_GUIDE (auth subsection + a Decision
  Log entry recording the discovery-≠-gate and roles-are-advisory decisions).

**Tests.** `test_scope_catalog.py` asserts every `*_SCOPE` constant is catalogued
exactly once, `ALL_SCOPES` matches catalog order, bundles reference only known
scopes and cover all of them, and the committed doc matches the generator
(drift guard). `test_mcp_oauth_rs.py` updated to assert the full-vocabulary
`scopes_supported` (present even with no required scopes configured).

**Hard boundaries honored.** No auth-model change, no QG-owned identity/key
store, no new scope semantics or enforcement path — the IdP still owns
identities and QG still resolves data-access policy from `sub`/claims. Per-IdP
click-through quickstarts were explicitly deferred.

### 96. Unify the AST reference-walk into a single canonical visitor (enforcement hardening) ✅ DONE

**Effort: M. Priority: high (robustness/proof; pure refactor, no behavior
change). Depends on: nothing. Blocks: item 97.**

**Why it mattered.** The knowledge "every place in a `StructuredQuery` where a
table/column reference can appear" was duplicated across four independently
hand-maintained walks: `validation/policy_validation.py`'s `_iter_column_refs`,
`_non_projection_column_refs` (whose own docstring admitted it was
"`_iter_column_refs` minus the bare-`str` select branch" — a near-verbatim copy
kept in lockstep by hand), and `_collect_referenced_tables`, plus
`validation/schema_validation.py`'s own separate `for join…`/`for item…`
enumerations and `_collect_tables_from_where`. Every time the AST grew a field,
each walk had to be taught the new position or a policy/schema hole opened
silently in whichever one was forgotten.

**What shipped.**

- `validation/schema_validation.py` now defines the single canonical reference
  visitor: `iter_column_refs(query) -> Iterator[ColumnRef]`, where
  `ColumnRef = (position: RefPosition, ref: str)`. `RefPosition` is a 10-value
  enum (`SELECT_PROJECTION_BARE`, `SELECT_NESTED`, `JOIN_ON`, `JOIN_EXTRA_ON`,
  `WHERE`, `GROUP_BY`, `HAVING`, `ORDER_BY`, `TOP_N_PARTITION`, `TOP_N_ORDER`) —
  rich enough to preserve the one distinction enforcement branches on: a *bare*
  top-level select projection is the sole position a masked column (item 49) may
  appear. The single WHERE-tree recursion (`_where_column_refs`) is the visitor's
  only caller.
- **policy_validation.py** deleted `_iter_column_refs`,
  `_non_projection_column_refs`, `_collect_referenced_tables`, and its own
  `_where_column_refs`. `referenced_tables` now folds from/join structural tables
  with `iter_column_refs`; `validate_policy` walks the visitor once and feeds
  both the table/column allow-deny checks and the masked-column rule (the latter
  by filtering `position is RefPosition.SELECT_PROJECTION_BARE`). The
  predicate-axis walk `_iter_where_predicates` (for count/in-list caps, not
  references) deliberately stayed — a different axis.
- **schema_validation.py**'s `validate_schema` replaced its six inline
  per-position table-collection loops (and `_collect_tables_from_where`) with a
  single `for column_ref in iter_column_refs(query)`.

**Zero behavior change**, as required: the full suite (1402 passed), the
adversarial `make test-security` suite, and `test_credential_redaction.py` all
pass unchanged; no new caller surface, no AST change, no policy-semantics change.
`tests/unit/test_reference_visitor.py` (new) pins the position taxonomy and the
exact reference set per position, plus the alias→physical mapping and the
item-49 exemption, so a future AST reference position must be taught in the one
visitor (and this test) rather than in N forgotten copies — which is precisely
what makes item 97's bounded nested subqueries safe to add by construction.

**Hard boundaries honored.** Not a rewrite of policy semantics, not a change to
any cap or allow/deny rule, not a new AST field.

### 97. Bounded nested subqueries (uncorrelated, single-connection, depth-capped) ✅ DONE

**Phase 1 shipped — `IN (subquery)` / `NOT IN (subquery)`:** `Predicate.value_subquery`
is a nested `StructuredQuery` (recursive AST via `model_rebuild`), valid only for
`in`/`not_in`, mutually exclusive with value/value_col, must select exactly one
column. `Policy.max_subquery_depth` (default 1) bounds nesting. The single
canonical scope-walker `schema_validation.iter_query_scopes` enumerates the outer
query + every subquery as **independent scopes**; policy validation enforces the
count caps (select/joins/group_by/where-predicates/top_n) **summed tree-wide**
(so nesting can't multiply a cap — the core threat), plus per-scope column
allow/deny + masking (a denied/masked column can't hide one level down), and
rejects: over-depth, a masked column as the subquery's IN-output, and
`value_subquery` outside a WHERE clause (HAVING/CASE rejected). Schema validation
validates each subquery scope independently (a correlated reference to an outer
table fails as undeclared-in-scope) and rejects cross-connection subqueries. The
compiler renders `col.in_(subselect)` via the same compile path (so the subquery
gets mandatory row filters + min-group guardrail), stripping the subquery LIMIT
so IN membership is complete. Covered by `tests/security/test_subquery_boundary.py`
(13 adversarial: cap-evasion-via-nesting per cap, denied/masked-in-subquery,
correlated, cross-connection, over-depth, HAVING/CASE) + `tests/integration/
test_subquery_end_to_end.py` (real-SQLite IN/NOT-IN match an equivalent join).
Renders as standard SQL IN(subquery) on Postgres+MSSQL (no dialect-specific
code); MSSQL execution parity is CI-validated. Decision Log entry recorded.

**Phase 2 (not started):** `FROM (subquery)` — a derived table the outer query
selects *from*. Additionally needs the outer query to resolve against the inner
query's OUTPUT aliases (a virtual relation) without reaching past them into inner
base tables; deferred as a distinct, harder slice. HAVING/CASE `IN (subquery)`
also deferred.

> **Overlap with item 105 (noted 2026-07-25).** Item 105 (CTE / derived table in
> FROM) is the same capability generalized — its own text says it "generalizes
> item 97's `subquery_tables` plumbing and `effective_name_map`". ROADMAP.md
> Phase 4 now sequences 97 phase 2 immediately before 105 for that reason. Build
> them as one slice, or fold 97 phase 2 into 105 and stub it — do **not**
> implement the derived table twice. Which way to resolve it is a call to make
> when 105 is scoped, not now.

**Effort: L. Priority: medium (capability extension). Depends on: item 96.
Requires a recorded Decision Log entry in `docs/PRODUCT_GUIDE.md` before build.**

**Why it matters.** Callers naturally compose queries that scope an initial set
and filter from it (`FROM (subquery)` / `IN (subquery)`). Today the AST is
single-level: `from_table` is a table-name string and predicate `value`/
`value_col`/`in`-list are literals/columns — there is no caller-authored nested
query. Much of the real demand is already served by joins + `group_by`/`having`
(semi-joins, aggregate-filters) and by the two-round-trip pattern (query 1
returns IDs → query 2 filters with `in: [...]`), so this item must clear a
genuine-marginal-value bar, not be added reflexively. It does **not** cross any
North Star non-goal: a nested `StructuredQuery` is still a fully validated AST,
never a raw-SQL string.

**Scope — the minimal safe subset only (reject the rest, per the item-74
"reject, don't emulate" precedent):**
- ✅ **Uncorrelated** derived table (`FROM (subquery)`) and/or `IN (subquery)`.
- ❌ **Correlated** subqueries (inner references an outer row) — this is the
  sharp cliff that defeats "each query is independently bounded"; reject
  explicitly with a `QueryValidationError` pointing at joins as the primitive.
- ❌ **Cross-connection** nesting (a subquery carrying its own `connection`) —
  can't push to one DB; reject.
- New cap `max_subquery_depth` (default 1). **All existing caps (max_joins,
  max_where_depth, max_group_by, top_n, in-list size) apply summed tree-wide**,
  never per-level — otherwise nesting becomes a cap-multiplier bypass.

**What to do (once item 96 lands, this is small).**
1. Make the model recursive (e.g. `from_table: str | StructuredQuery`, and/or an
   `in`-subquery predicate variant). Pydantic recurses for free.
2. Teach the **one** canonical visitor (item 96) to descend nested queries, so
   policy + schema enforcement follow automatically. Base-table column refs
   inside a subquery get the full allow/deny + cap treatment; the outer query
   resolves against the inner query's *output aliases* (a virtual relation),
   which must NOT let the outer reach past them into inner base tables.
3. Schema validation computes the inner query's output column set and threads it
   through as a virtual relation.
4. Compiler renders via SQLAlchemy Core `.subquery()` (already used for `top_n`
   at `compiler/sqlalchemy_compiler.py`); nested scopes need their own column
   resolution frame.
5. Full `adversarial-probe` pass — every new node is a new bypass surface;
   codify each vector as a regression test (esp. cap-evasion-via-nesting and
   masked/denied column hidden in a subquery).

**Acceptance.** Bounded subset above works end-to-end on Postgres + MSSQL;
correlated/cross-connection/over-depth all rejected with clear errors; caps
proven to apply tree-wide by adversarial tests; Decision Log entry recorded.
No raw-SQL surface, no non-goal crossed.

---

## Flagship pillar — Expressive Query Engine (items 99–106)

Items 99–106 are one coordinated initiative: take the READ structured query
engine to 10/10 expressiveness for a fluent SQL author **without weakening any
safety invariant** — the deepening of the North Star **Structural** pillar (the
"no raw SQL, ever" bet only wins if the AST rarely walls off a real SQL author).
The deep, authoritative design/test/validation spec lives in
**[docs/ENGINE_EXPRESSIVENESS_PLAN.md](docs/ENGINE_EXPRESSIVENESS_PLAN.md)** — each
item below is scoped there (§4) with its AST shape, compiler seam, validation
wiring, caps, dialect handling, adversarial cases, and per-item Definition of
Done. Build them in the order 99 → 106; the plan's §3 checklist and §5 canonical
regression bar are mandatory acceptance gates for every item.

The unifying safety rule (plan §1, §3): **every new node must be wired into the
canonical reference visitor (item 96) or its column refs bypass policy allow/deny
+ masking**, and **every new cost-bearing count must be capped summed tree-wide
(item 97)**. Reject-don't-emulate (item 74) governs all per-dialect gaps.

### 99. Query engine: `HAVING` as `WhereNode` + searched `CASE` condition ✅ DONE

**Effort: S. Priority: high (flagship pillar; cheap first step). Depends on:
item 96.** Phase 0 of
[docs/ENGINE_EXPRESSIVENESS_PLAN.md](ENGINE_EXPRESSIVENESS_PLAN.md) — the
low-risk warm-up that proves the visitor/cap-expansion pattern on machinery that
already existed, before items 100+ build the `Expression` substrate on it.

**Why it mattered.** Two positions in the AST were arbitrarily weaker than
`where`, for no safety reason: `StructuredQuery.having` was a `List[Predicate]`
(implicitly AND-combined, no nesting) and `CaseWhen.when` was a single
`Predicate`. So `HAVING SUM(x) > 10 OR COUNT(*) < 3` and a searched
`CASE WHEN a > 0 AND b < 5 THEN …` were inexpressible — the caller had to
pre-filter in `where` before grouping (which changes the semantics) or give up.
Since the Structural pillar's whole bet is that the AST rarely walls off a
fluent SQL author, an artificial wall over machinery that already existed was
pure downside.

**What shipped.**

- **AST** (`query_ast/models.py`): `having: List[Predicate]` →
  `Optional[WhereNode]`; `CaseWhen.when: Predicate` → `WhereNode`. `CaseWhen`
  joins the recursive `model_rebuild()` cycle now that it references `WhereNode`.
  Both positions are now the *same* union `where` already used — this removed a
  special case rather than adding a grammar.
- **Compiler** (`compiler/sqlalchemy_compiler.py`): both positions compile
  through the one existing `_compile_where`. HAVING threads `alias_map` (so a
  HAVING predicate can still reference a select alias); a CASE condition passes
  `alias_map={}` (it can't reference a peer select alias). Both pass `ctx=None`,
  keeping `value_subquery` WHERE-only. The two hand-rolled
  `_resolve_predicate_target` + `_apply_predicate` call sites are gone.
- **Validation** — the safety-critical half. The canonical visitor (item 96) now
  walks both trees: `iter_column_refs` yields every HAVING ref via
  `_where_column_refs` (not a flat list of single predicates), and
  `select_item_column_refs` walks the whole CASE-condition tree. So column
  allow/deny **and** the item-49 masked-column rule follow automatically at any
  nesting depth — an unvisited ref buried in an OR-group would have been a silent
  policy/mask bypass, which is exactly the failure class item 96 was built to
  prevent. `iter_where_and_having_predicates` walks the HAVING tree so
  `iter_query_scopes` still finds a nested `value_subquery`, and schema
  validation validates both trees (`_validate_where_columns`, `allow_alias=True`
  for HAVING, `False` for CASE conditions).
- **Caps — no new policy field, per the plan.** `max_where_depth` now also fires
  on a deep HAVING tree and on a deep CASE condition (one shared
  `_check_where_depth` helper, labeled per position). `max_where_predicates` now
  counts HAVING predicates *across the tree* rather than a flat `len()`, and
  gained a third tree-wide budget — `case condition predicate count` — so a wide
  boolean CASE condition can't dodge the cap by breadth (depth and branch-count
  caps alone wouldn't catch `and` of N predicates in one branch). All summed
  tree-wide across subquery scopes, per item 97.
- **Item 97 boundary preserved and tightened:** `value_subquery` stays WHERE-only
  and is now rejected even when buried inside a HAVING or CASE boolean group
  (the old check only inspected a single predicate).
- **Latent gap closed on the way:** `max_in_list_size` now applies to an
  `in`/`not_in` inside a CASE condition too. The old single-predicate walk never
  size-checked CASE conditions, so an over-size `in` list there was unbounded.
- **Audit** (`audit/events.py`): `having` is shaped with `_where_shape` and
  emitted only when set, matching `where`. Fixed a real pre-existing bug in
  `_where_shape` while there — a `not` group fell through to
  `{"or": []}`, silently misreporting the predicate shape; it now emits
  `{"not": …}`. Still redaction-safe: shapes carry operator/column/function only,
  never literals.
- **Client SDK** (`client/builder.py`): `.having(*nodes)` takes full `WhereNode`s
  and AND-combines multiple nodes/calls via a shared `_and_combine` helper (so
  `.where()` and `.having()` now use identical logic); `when()` accepts a
  `WhereNode`. Builder ergonomics are unchanged for existing callers.

**Coverage.** 2 compiler tests (OR-combined HAVING clause; AND-combined CASE
condition), 9 policy tests (denied column buried in a HAVING OR-group and in a
CASE AND-condition; masked column in both; HAVING depth cap; CASE-condition depth
cap; CASE-condition predicate-count cap; in-list size inside a CASE condition),
3 client-builder tests, and a new end-to-end integration file
(`tests/integration/test_searched_having_case_end_to_end.py`) that *executes*
both against real SQLite and compares against a ground truth computed from the
same pipeline — asserting each boolean arm actually contributes, so the test
can't pass on AND/OR confusion. The item-97 subquery-in-HAVING/CASE rejections
and the property-based compiler fuzzer were updated to the new shapes. Full
suite: 1589 passed; `-m security` 258 passed.

**Accepted cost (recorded in the Decision Log):** a breaking wire-format change
— `"having": [{…}]` becomes `"having": {…}` (or `{"and": […]}`), and the audit
event's `having` is a nested shape present only when set. No JSON example in
`examples/` or the docs used `having`, so the blast radius was the test suite,
the benchmark corpus case `denied-column-in-having`, and the client builder.

**Hard boundaries honored.** No raw SQL, no new dialect code (boolean logic is
universal on both Postgres and MSSQL, so nothing belonged on `DialectAdapter`),
no new policy cap field, no new scope container, and no change to what a masked
or denied column is allowed to do.

### 100. Query engine: bounded scalar `Expression` substrate ★ ✅ DONE

**Effort: XL. Priority: high (flagship pillar — the Structural pillar itself).
Depended on: items 96, 99.** Full spec: **ENGINE_EXPRESSIVENESS_PLAN.md Phase 1.**

**Shipped.** One closed, depth-capped recursive `Expression` union now backs every
position where a scalar value is expected, replacing the flat leaf set that made
arithmetic, conditional aggregation, nested functions, expression-valued `CASE`,
and computed group keys *the same* missing feature:

```
Expression = ColumnExpr | LiteralExpr | BinaryOpExpr | FunctionExpr
           | CastExpr | CaseExpr
```

**Positions it reaches:** a new `ExpressionSelectItem` projection
(`{"expr": …, "as": …}`), `AggregateSelectItem.arg`, `CaseWhen.then` /
`CaseSelectItem.else_` (widened from column-or-literal — a wire-compatible
superset), and **both** sides of a `Predicate` (`expr` / `value_expr`). A computed
GROUP BY key needs no new field: project the expression with an alias and group by
it, the route `date_bucket` has always used.

**Functions:** `coalesce/lower/upper/trim/concat/abs/floor/nullif/replace` are
dialect-universal and stay off the adapter; `ceil`/`length`/`round`/`substring`
genuinely differ and are one new `DialectAdapter.scalar_function` method per
dialect (`CEILING`/`LEN` on T-SQL; `round`'s NUMERIC cast on Postgres, which has
no `round(double precision, integer)`; `substr` on the internal SQLite path).
`substring` requires exactly 3 arguments at the AST layer because T-SQL has no
2-argument form — a 2-arg call would render fine on Postgres and break live.

**Caps (new):** `Policy.max_expression_depth` (default 5, per expression tree) and
`Policy.max_expression_nodes` (default 200, summed **tree-wide** across the query
and every subquery per item 97). `max_case_branches`, `max_where_depth`, the
case-condition predicate budget, and `max_in_list_size` now follow a CASE wherever
item 100 lets it move (into an aggregate argument, into arithmetic, into a WHERE
predicate) instead of only seeing a top-level `CaseSelectItem`.

**Safety — the make-or-break step.** There is exactly ONE recursion over the
closed union, `iter_expression_parts`; the column-ref walk, the node-count and
depth caps, and the nested-CASE rules are all *filters* over it, so a new member
cannot be taught to three of four walks and forgotten in the fourth (the item
96/111 drift class). It **fails closed** — an unrecognized node raises rather
than being yielded childless (which would contribute neither refs nor size) —
and it *crosses into the boolean-condition layer and back* (a `CaseExpr`
branch's `when` is a `WhereNode`, so its refs are ordinary `Predicate` refs, not
`ColumnExpr` nodes). Exhaustiveness is pinned by construction: tests are
parameterized over `typing.get_args(Expression)`, and a guard test fails if a
member joins the union without a payload under test — verified by adding a
seventh member and watching it fail. Every ref is yielded through the item-96 canonical visitor at
`SELECT_NESTED`/`WHERE`/`HAVING`, so a denied or masked column buried anywhere in
an expression is rejected exactly as at the top level. No new `RefPosition` member
was needed — an expression column is never a bare projection, which is precisely
what the masked-column rule keys on.

**Decisions recorded** (`docs/PRODUCT_GUIDE.md` Decision Log, all 2026-07-25):
the five-part non-goal-#7 boundary; **guarded division** (`left / NULLIF(right,
0)`); `AggregateSelectItem.col` kept permanently as sugar normalized to `arg`;
**`ColArg`/`LiteralArg` collapsed into `ColumnExpr`/`LiteralExpr`** so exactly one
column-leaf type exists; and **writes excluded** — `expr`/`value_expr` in a write
WHERE is rejected at `validate_write_policy`, the item-110 posture.

**Two live-breakage traps caught and fixed during the build**, both of the
items 75/82 "renders fine, breaks live" class:
- `CAST(x AS text)` mapped to SQLAlchemy's `Text`, which a **connected**
  SQL Server 2012+ renders as `VARCHAR(max)` — codepage-limited, so under the
  default collation `'δ-λ'` silently comes back `'d-?'`. Now `Unicode` →
  `NVARCHAR(max)`, the type `MSSQLDialectAdapter.column_mask` already used.
  *(The first draft of this justified the change as "TEXT is deprecated and not
  comparable"; that is what an UNCONNECTED `mssql.dialect()` renders and was
  simply wrong for a real server. The real-MSSQL mutation test caught the bad
  reasoning — the rendering-only assertion had not — and the fix was to assert
  the non-ASCII round-trip instead. Recorded because the wrong reason nearly
  shipped attached to the right change.)*
- Postgres has **no `round(double precision, integer)`**; the adapter casts to
  NUMERIC. Verified against the live server, which rejects the uncast form.

**Also fixed (would have been a 500 on every expression query):**
`audit/events.py`'s `normalize_query_shape` knew none of the new nodes. It now
emits a redaction-safe expression *shape* — node kinds, operators, and column
refs, but **no literal values** — for both select items and predicates.
`execution/service.py` additionally maps a `DataError`/`ProgrammingError` from the
database into a clean typed `QueryValidationError` (arithmetic on a text column is
now an ordinary caller mistake), mirroring the write path's constraint mapping and
never echoing driver text, which carries column names and values.

**Coverage.** Unit: visitor contract per union member and per position
(`test_reference_visitor.py`), rendering + per-dialect divergence + guarded
division (`test_compiler.py`), depth/node/CASE caps including the tree-wide sum
across a subquery (`test_policy_validation.py`), the `col`↔`arg` sugar identity
and an Expression-union drift guard for the client builder
(`test_client_builder.py`), the DB-type-error mapping (`test_service.py`).
Security: denied **and** masked column buried at maximum depth in **every** one of
six expression positions, the undeclared-table check, and no-literal-in-audit
(`test_adversarial_security.py`) — verified to fail when the visitor's
CASE-condition crossing is reverted. Integration: SQLite end-to-end value checks
against ground truth computed through the same pipeline
(`test_expression_end_to_end.py`), real Postgres
(`test_postgres_expression_substrate.py`), and **real MSSQL**
(`test_mssql_expression_substrate.py`).

**Every per-dialect claim is mutation-verified against a live server**, not
asserted from SQL text — the discipline items 75/82 exist to enforce. Reverting
each rendering produces the real failure: `ceil` → *"'ceil' is not a recognized
built-in function name"*; a one-argument `ROUND` → *"The round function requires
2 to 3 arguments"*; unguarded `/` → *"Divide by zero error encountered"* on
MSSQL and `DivisionByZeroError` on Postgres.

**Canonical regression bar (plan §5):** rows 1, 2 and 7 went ✅; row 15's
arithmetic half is done and waits only on `OVER` (item 101). 5/16 → **8/16**.

**Accepted cost.** The agent-facing MCP schema grew ~14K chars (117,100 total;
`_MAX_TOTAL_CHARS` bumped deliberately to 123,000 after moving maintainer
rationale out of model docstrings — a Pydantic docstring becomes the agent-facing
schema description and costs tokens every session, a `#` comment costs nothing).
Every later engine item (101–106) now inherits `Expression` as a dependency, so a
bug in the substrate is a bug everywhere — the deliberate trade for reviewing one
node hard, once, instead of five special cases each with its own visitor wiring to
forget.

### 101. Query engine: general window functions (`WindowSelectItem`) ★ ✅ DONE

**Effort: L. Priority: high (flagship pillar; second expressiveness pillar).
Depended on: items 96, 100.** Full spec: **ENGINE_EXPRESSIVENESS_PLAN.md Phase 2.**

**Shipped.** `top_n` was the only `OVER()` surface in the product and it did
exactly one thing — rank rows within partitions and *keep the top N*. A new
`WindowSelectItem` select item projects a window value without collapsing or
filtering rows, covering all thirteen window functions:
`sum/avg/min/max/count`, `row_number/rank/dense_rank/ntile`, and
`lag/lead/first_value/last_value`, with `PARTITION BY`, `ORDER BY`, and
`ROWS`/`RANGE` frames. Running totals, moving averages, rank-in-place,
per-partition totals, and lag/lead gap analysis are expressible now.

```json
{"fn": "sum", "arg": {"col": "orders.total_amount"},
 "over": {"order_by": [{"col": "orders.id"}],
          "frame": {"mode": "rows",
                    "start": {"bound": "unbounded_preceding"},
                    "end": {"bound": "current_row"}}},
 "as": "running_total"}
```

**`over` is required, and that is load-bearing.** `{fn, arg, as}` is already the
plain aggregate; `over` (possibly `{}`, meaning `OVER ()`) is what makes the
`SelectItem` union resolve unambiguously instead of Pydantic silently preferring
`AggregateSelectItem` for a payload the caller meant as `SUM(x) OVER ()`. It also
reads like SQL. `arg` is item 100's `Expression`, so `SUM(qty * price) OVER (…)`
works and inherits every guarantee of that substrate rather than adding a parallel
scalar shape.

**Four bounds, all maintainer-ratified before build** (`docs/PRODUCT_GUIDE.md`
Decision Log, 2026-07-26; plan §8 entry 3):
1. **No default frame is synthesized** — omitting `frame` emits no `ROWS`/`RANGE`
   clause, so the dialect's SQL-standard default applies (identical on PG/MSSQL);
   inventing one is the item-74 line. A **numeric `RANGE` offset is rejected on
   MSSQL** (T-SQL's `RANGE` takes only unbounded/current-row bounds) pointing at
   `mode: "rows"`, not rewritten to `ROWS`, which treats ties differently.
2. **Unbounded frame ends are not separately gated; offsets are capped instead.**
   `UNBOUNDED PRECEDING … CURRENT ROW` is both the running-total idiom and SQL's
   own default, and unbounded-both-ends is the same whole-partition scan as no
   frame — gating it while the no-frame form stays legal would be theater. The
   genuinely unbounded magnitudes get the caps.
3. **No window with `group_by`/aggregate select items** — a window over
   *aggregated* rows needs the aggregation materialized as a derived table (item
   105); rejected at the AST layer rather than growing a second bespoke
   materialization path beside `_apply_top_n`'s.
4. **No aggregate window when `Policy.min_group_size` is set** — the item-88
   k-anonymity floor is a `HAVING count(*) >= k` on grouped results, and a window
   aggregate has no group to filter, so `COUNT(*) OVER ()` would report a
   below-floor count *with no column projected at all*. Fails closed; ranking and
   offset windows stay allowed since they only surface values the caller may
   already project bare.

**Caps (new):** `Policy.max_window_specs` (default 5, summed **tree-wide** per item
97; `0` disables windows for a connection) and `Policy.max_window_frame_offset`
(default 1000 — bounds both a frame's `N PRECEDING/FOLLOWING` distance and a
`lag`/`lead` offset, the only unbounded magnitudes in the AST). Window
`PARTITION BY` shares `max_partition_by` with `top_n` — same cost, one budget, so
the cap's message changed from `top_n.partition_by exceeds` to
`partition_by exceeds`.

**Portability enforced at the AST layer, not per dialect.** `ORDER BY` inside
`OVER` is required wherever T-SQL requires it (every ranking/offset function, and
any framed window), a frame is rejected for the functions T-SQL won't accept one
for, and `over.partition_by`/`over.order_by` refs must be dotted `Table.Column` —
no dialect lets an `OVER` clause reference a peer select alias, so the spelling is
refused up front instead of producing invalid SQL. Frame bounds are checked for
sanity (no start after end, no start at `unbounded_following`). One AST that
renders on Postgres and fails live on MSSQL is the failure mode all of this exists
to prevent.

**Safety.** All three ref-bearing positions — `arg`, `partition_by`, `order_by` —
flow through the item-96 canonical visitor at `SELECT_NESTED`, so a denied column
is rejected and a masked one cannot be ordered or partitioned by (which leaks its
value by inference). No new `RefPosition` member: every window ref is a
non-projection use inside a select item, which is the only distinction enforcement
branches on. A window's alias is deliberately **absent** from `_select_aliases`, so
`top_n` cannot rank by it — that would nest one window inside another's `OVER`
clause. `audit/events.py` records a redaction-safe window shape (function, columns,
frame mode + bound *kinds*) and no offsets, distances, or literals. The
`window_frame` adapter method is the sixth `DialectAdapter` variance point.

**Coverage.** Unit: rendering per dialect + frame forms + the MSSQL RANGE and
NULLS rejections (`test_compiler.py`, `TestWindowFunctions`), every AST shape rule
(`TestWindowAstRules`), adapter frame matrix (`test_dialect_adapters.py`), the two
new caps + the tree-wide sum across a subquery + the k-anonymity rule + **every
item-100 bound reaching inside a window argument** (depth, node budget, CASE
branches, CASE-condition depth/breadth, in-list size, no-subquery-in-a-CASE) in
`test_policy_validation.py`, ref-resolution and the undeclared-table check
(`test_schema_validation.py`), the redaction-safe audit shape (`test_audit.py`), a
database type error inside a window mapping to a clean typed 4xx with no driver
text (`test_service.py`), builder parity (`test_client_builder.py`). Security:
denied **and** masked column in each of **four** window ref positions — including
a predicate buried in a CASE condition inside the window's argument, which lives
outside the `Expression` union entirely — the k-anonymity bypass attempt, the
tree-wide cap bypass, no offsets in audit (`test_adversarial_security.py`), plus
six malformed window payloads at the REST/MCP boundary
(`test_malformed_input_fuzzing.py`) and windows folded into the property-based
compiler fuzzer (`test_compiler_properties.py`). Integration: SQLite end-to-end
value checks against ground truth computed through the same pipeline, both
published composition recipes, and the masked-column caveat
(`test_window_end_to_end.py`); and — the plan's headline requirement — **every one
of the thirteen functions plus each frame form executed against a live Postgres
AND a live SQL Server with rows asserted equal**
(`test_cross_dialect_differential.py`), with a coverage test that fails if a new
window function ships without a live both-dialects case.

**Three guards added beyond the item** (the same drift class items 96/111 exist
for): a parameterized test that the canonical visitor yields a marker column from
**every** `SelectItem` union member; one that `normalize_query_shape` handles
every member — the audit path runs on every request and previously raised on an
unknown select item, so a new type would have 500'd until noticed; and a
`WindowFn`-parameterized live case, so a new window function cannot ship without
being executed on both real backends.

**Verified by mutation, not by assertion alone**, and one mutation found a real
hole that the first round of tests did not:

- Deleting the window branch from `select_item_column_refs` fails the window ref
  tests and the `referenced_tables` test. ✅
- Deleting `WindowSelectItem` from **`select_item_expressions`** — the single line
  that makes a window's `arg` an item-100 `Expression` for enforcement purposes —
  originally failed **nothing**: all 1801 tests passed while the depth cap, the
  node budget, `max_case_branches`, the CASE-condition depth/breadth budgets,
  `max_in_list_size`, and the no-`value_subquery`-in-a-CASE rule all silently
  stopped applying inside a window argument. Six targeted tests now fail on that
  mutation. This is exactly the failure the plan's §9 warns about ("do not let the
  adversarial suite lag the capability surface") and it was invisible because the
  ref walk reaches window args through a *different* path than the caps do.
- The MSSQL `RANGE 2 PRECEDING` rejection is backed by a live-server test proving
  Postgres runs that statement and SQL Server refuses it, while SQLAlchemy
  compiles it for both without complaint.

**Composition is documented AND executed.** The plan's DoD requires that anything
deliberately left to composition ships with the recipe written down, so
`docs/PRODUCT_GUIDE.md` publishes both — the two-query moving average over daily
aggregates, and percent-of-total via a projected window total — and
`test_window_end_to_end.py` runs both verbatim, including the caveat that a
*masked* column cannot be a window argument. A recipe nothing executes is a claim,
not a primitive. (The first draft of the percent-of-total recipe used
`orders.total_amount`, which the shipped demo policy masks — a reader pasting it
would have hit a policy rejection. Caught by writing the test.)

**The shipped container runs a window**: `make release-smoke` now asserts a
running total over real Postgres from inside the release image, alongside the
existing read and governed-write round-trips.

**The two caps are backed by a measurement, not judgement** (added 2026-07-26,
`tests/integration/test_postgres_window_cost.py`): at the default
`max_window_specs=5` a windowed query costs ~15x a plain scan of the same 25,000
rows, and the per-window cost rises past that. For `max_window_frame_offset` the
finding is sharper — **Postgres does not price the frame at all** (identical
`EXPLAIN` cost for a 10-row and a 10,000-row frame) while a non-invertible
aggregate over a wide frame really is O(rows x frame) (12 ms / 574 ms / 3.4 s at
10 / 1,000 / 10,000 preceding), so item 26's cost gate is structurally blind there
and this cap is the only guardrail that sees it. Three qualifications are recorded
with it rather than glossed: `SUM`/`AVG` are only free over
integer/numeric/money/interval (a `double precision` column rescans like `MAX`),
the timings are unlimited-scan numbers whereas a top-level read is LIMIT-clamped
(the full cost is reachable inside an `IN (subquery)`, whose LIMIT is stripped),
and MSSQL's estimator is unmeasured. The test pins the order-of-magnitude
findings, not the individual timings.

**Canonical regression bar (plan §5):** row 5 (running cumulative total) went ✅.
8/16 → **9/16**. Two table corrections were recorded rather than glossed: **row 3**
(7-day moving average of *daily* orders) needs item 105's derived table, not this
item — it is a window over aggregated rows — and **row 15** (percent-of-total in
one expression) needs a window to be an `Expression` operand, which is recorded in
§5 as a wall for the maintainer to weigh as its own item rather than smuggled in
here.

**Also surfaced, not silently fixed:** three hand-maintained guardrail-field lists
(`admin/access_diff.py`, `admin/models.py`'s `EffectiveGuardrails`,
`help/service.py`) have drifted from `Policy`'s cap set — nine caps from items
68–72/88/97/100/101 are missing, so the semantic access diff can report "no
change" for a loosened cap. Recorded as **item 115** rather than fixed here: it
widens a public REST response model and wants its own PR.

**Accepted cost.** The MCP schema grew 9,600 chars (126,700 total; budget bumped to
132,000) — but only ~4,370 of that is the new capability: the identical window
definitions are inlined a *second* time into `run_structured_writes`, because a
write's `WHERE` reuses the read `Predicate` and reaches `StructuredQuery` →
`SelectItem`. **Item 114 would have absorbed this entire raise.** A k-anonymity
deployment gets no aggregate windows, and windows over grouped results wait for
item 105.

### 102. Query engine: `EXTRACT`/date_part + relative-date/interval helpers ✅ DONE

**Effort: M. Priority: medium (flagship pillar; high everyday value). Depends on:
item 100 (shipped). Decision Log entry recorded 2026-07-26 before code (plan §8
entry 4).** Full spec: **ENGINE_EXPRESSIVENESS_PLAN.md Phase 3a**. Phase 3a of the
Expressive Query Engine — the **Structural** pillar.

**What shipped.** Three new members of item 100's closed `Expression` union, so
each is legal anywhere a scalar is (projection, aggregate argument, either side of
a predicate, inside a `CASE`):

- `{"extract": <expr>, "part": …}` — one integer field of a date/timestamp, over
  `year`/`quarter`/`month`/`week`/`day`/`dayofweek`/`dayofyear`/`hour`/`minute`/`second`.
- `{"now": "timestamp"|"date"}` — the current UTC instant, or midnight UTC today.
- `{"date_add": <expr>, "unit": …, "amount": <signed int>}` — shift by whole units.

Plus `Policy.max_interval_days` (default 3,660), three `DialectAdapter` methods
(`extract_part`/`current_timestamp`/`date_add`) implemented on all three adapters,
`SET LOCAL TIME ZONE 'UTC'` in the Postgres session guardrails, `extract`/`now`/
`date_add` in the typed Python builder, and the three nodes wired into the audit
shape normalizer.

**Why each is its own union member rather than a `FunctionExpr`.** The item text
said "extends item 100's `FunctionExpr`", and that turned out to be wrong for all
three: `part`, `unit` and the clock `kind` are **keywords, not scalars**, so
folding them into `args` would have required an `Any`-shaped or free-string
argument — the exact escape hatch the substrate forbids. `CastExpr.to` set the
precedent. What deliberately does *not* exist is an `interval` union member: an
interval is not a scalar (it cannot be projected on MSSQL, compared to a number,
or grouped by), so admitting one would break the property that every `Expression`
is legal everywhere a scalar is expected — the same trade §5's row-15 note refuses
for windows. `DateAddExpr` carries the magnitude as a capped keyword+integer pair
and yields a timestamp, so the union stays all-scalar.

**The real finding — the timezone pin.** Recorded as the item's Decision Log
entry. **Postgres resolves `EXTRACT`, `date_trunc` and every
`timestamp`↔`timestamptz` conversion against the session `TimeZone`**, and
QueryGate never set one. So the *already-shipped* `date_bucket` was silently
server-config dependent: the same query on the same data returned different
answers on two deployments, with nothing in the SQL text to show it. The decision
is UTC, enforced where each dialect actually decides it — a `SET LOCAL TIME ZONE
'UTC'` for Postgres, `SYSUTCDATETIME()` (not `GETDATE()`) for MSSQL, and SQLite's
`'now'` which is already UTC. **This is a behavior change**: a deployment whose
Postgres server zone is not UTC will see different `date_bucket` and `EXTRACT`
values than before. Columns are never converted — QueryGate cannot know what a
naive `timestamp` column means, so it reads it as stored and guarantees only that
its own clock readings and field extractions are UTC.

The pin is invisible to every rendering assertion *and* to any test run against a
server that is already UTC (which the demo container is), so it is proven live in
`test_postgres_date_primitives.py` by setting the role's default zone to
`Pacific/Marquesas` — UTC−09:30, a **half-hour** offset chosen so no plausible
off-by-N bug can imitate it — and asserting a row stored at 12:00 UTC still
extracts hour 12. Removing the pin makes it 2.

**Two parts are defined, not passed through.** `dayofweek` is 0=Sunday..6=Saturday
and `week` is the ISO-8601 week, on every dialect. T-SQL disagrees natively on
both: `DATEPART(weekday)` is 1-based *and* moves with the server's `SET DATEFIRST`,
and plain `week` is a different count from ISO. The MSSQL adapter renders the
DATEFIRST-independent idiom `(DATEPART(weekday, x) + @@DATEFIRST - 1) % 7` and
`iso_week`. This is mechanical translation of a defined primitive (the
`date_bucket` category), not synthesized structure — and where a dialect genuinely
lacks the capability it still **rejects**: the internal SQLite path has no ISO-week
function (`%W` is a different count and `%V` postdates the builds CPython ships),
so `extract(week)` raises and points at `date_bucket`'s `week` granularity.

**What the cap is and is not.** `max_interval_days` is computed from the amount
with **upper-bound** unit lengths (a year counts as 366 days, a month as 31, and
sub-day units round up), so a larger unit cannot launder a bigger reach past it.
It is explicitly **not** a row-count guardrail — a caller who wants everything
omits the filter, which `max_limit` and the mandatory row filters bound. It
prevents a caller-triggerable *server-side* error (`DATEADD(year, 10000, …)`
overflows T-SQL's datetime range; Postgres raises "timestamp out of range") and
keeps a relative-date filter an honestly-bounded lookback. Default 3,660 days
(~10 years) sits above every realistic analytic window and below either dialect's
overflow point even when `max_expression_depth` nested shifts compound it.

**Five defects found by measuring rather than reading**, each of which passed a
careful reading of the diff first:

1. **Postgres `amount * interval '1 day'` was wrong.** Postgres defines only
   `interval * double precision`, so the caller's bound integer would have been
   resolved to a float by operator inference. Replaced with `make_interval`, whose
   parameters are typed integers in fixed positions.
2. **MSSQL `now: "date"` did not truncate — in the rendering tier.** The generic
   `sa.Date` renders `DATETIME` against an *unconnected* mssql dialect, so
   `now: "date"` looked like it kept the clock reading. Stated precisely (the first
   draft of this entry was not): SQLAlchemy falls back to `DATETIME` only when
   `server_version_info < (10,)`, which is true for an unconnected dialect and
   false for any connected SQL Server 2008+, so a live server would have rendered
   `DATE` correctly anyway. Naming the concrete `mssql.DATE` removes the version
   dependency and makes the rendering tier trustworthy — but this was a
   test-fidelity defect, not a live one, and counting it as a live bug would be the
   same unconnected-vs-connected confusion item 100's `CAST(x AS text)` rationale
   fell into.
3. **SQLite `quarter` returned a float.** SQLAlchemy's `/` is true division, so
   month 2 gave 1.33 rather than quarter 1.
4. **SQLite has no `weeks` date modifier.** `datetime(x, '-2 weeks')` returns
   **NULL** rather than erroring, so a week shift would have been a silently empty
   column. Expressed in days instead (a week is exactly 7 days, unlike a month).
5. **A fourth recursion over the `Expression` union had no exhaustiveness guard.**
   `audit/events.py`'s shape normalizer fails closed on an unknown node — correct —
   but the walk and the compiler both had union-parameterized guards and it did not.
   So the three new members passed the entire 2,021-test default suite (unit +
   integration) while *every query using one* raised at execution time. Only running one against a real
   database surfaced it. A guard per recursion now exists, and the compiler guard
   was widened to cover ref-free members (`LiteralExpr` was never compiler-tested
   either).

**Testing.** 82 unit tests (`test_date_primitives.py`), 24 end-to-end
(`test_date_primitives_end_to_end.py`), 9 real-Postgres
(`test_postgres_date_primitives.py`), and the cross-dialect differential suite
gained a live PG+MSSQL case per date part and per interval unit. Expected values
come from Python rather than from comparing the dialects to each other, so two
identically-wrong adapters cannot agree on a wrong answer — for the date parts,
and (after the audit) for the fixed-length interval units too; year and month are
calendar units with no fixed `timedelta`, so those rest on cross-dialect equality
plus the Postgres-side calendar assertions. A coverage test makes a live case
mandatory for every `DatePart`, and lives in the unit tier so it actually fires
outside the two-database CI job. The adversarial suite's
`_buried` helper now routes every leaf through `extract` and `date_add`, so all
ten denied/masked-column position tests cover them, plus five item-102-specific
cases (unit-laundering, subquery cap, no free strings, no interval amount in a
persisted audit event).

**Proven against a real SQL Server, not only in CI.** A live SQL Server 2022
was stood up (deliberately on a **UTC-09:30 clock**, so `SYSUTCDATETIME()` vs
`GETDATE()` is discriminating rather than vacuous) and the full differential suite
run against it plus live Postgres: 57 pass. That run **found three defects the
green suite had not**:

1. **A refactor had silently not applied.** `MSSQLDialectAdapter.extract_part` was
   still on the inline `.get(part, part)` passthrough while `_MSSQL_DATEPART_FIELDS`
   sat beside it as dead code — so the fail-open behavior was still live and a
   mutation test that edited the map proved nothing. Cause: a multi-replace script
   that asserted only "something changed". A test now perturbs each map and asserts
   the rendered SQL changes, which is the only check that couples a map to its code
   path.
2. **The differential corpus was thin on time-of-day.** Every seeded timestamp is
   midnight with no fractional part, so the *MSSQL side* of the `EXTRACT(second …)`
   rounding was invisible to the differential tier. A `date_probe` table now seeds
   year-boundary and fractional-second timestamps on **both** servers.
   *Corrected after re-review, and the correction matters more than the finding.*
   The first version of this entry also claimed the corpus could not discriminate
   ISO week from T-SQL's `week`. **Measured: it could** — 3 of the 20 seeded dates
   diverge at `DATEFIRST=7` (`2025-01-05` is ISO 1 / T-SQL 2, plus `2024-11-10` and
   `2025-06-01`). The `week` mutation survived because of defect (1) above: it
   edited a map nothing read. Blaming the corpus was a misdiagnosis of the author's
   own bug — the same "measured on the wrong tier" error this item's write-up is
   otherwise built around. The second-rounding half was likewise already pinned
   Postgres-side by `test_extract_second_truncates_rather_than_rounds`; the probe
   extends it to MSSQL, which is coverage widening rather than a missed defect. The
   probe is kept on both counts: `2024-12-30` (53 vs 1) is a far stronger
   discriminator than the incidental rows, and the fractional rows close a real
   differential-tier gap.
3. **The mutation harness itself had a false-positive bug**, matching the substring
   "error" against pytest's `RefResolutionError` warning — so *every* run reported
   "caught". Corrected to match the summary line, and every earlier result re-run.
   This is the one that should be read as a process finding rather than a code
   one: it means an earlier progress report of "16 caught, 0 missed" was made on a
   harness that could not fail.

A fourth, found by re-review rather than by the live run: the non-UTC clock the
MSSQL assertion depends on existed only on the developer's ad-hoc container, so in
CI — the only place that runs automatically — the assertion was vacuous while the
guide claimed it was not. `TZ` is now set on the compose service and both CI
service blocks, and the test skips loudly instead of passing when it detects a UTC
server. A documented vacuum is still a vacuum.

**Every new enforcement rule was mutation-verified** — each broken deliberately,
the suite re-run, and a failure confirmed *for that reason*. Two passes: 16
mutations before review (the cap itself, the upper-bound unit lengths, the
ceil-division, both MSSQL normalizations, the Postgres integer cast, the
`mssql.DATE` target, `SYSUTCDATETIME` vs `GETDATE`, the SQLite week rejection, the
SQLite quarter cast, the SQLite week→day conversion, the `make_interval` position
mapping, both visitor branches, the audit-shape omission); 8 more after the audit
covering the fixes it produced (the restored `_buried` chain against three separate
visitor branches, the `minute`/`second` multipliers, the Postgres `FLOOR`, the
corrected cap default, a swapped SQLite `%H`/`%M`); and 7 against the **live**
Postgres+SQL Server pair, of which 6 are caught and 1 is a verified no-op —
`sa.Date` vs `mssql.DATE` renders identically on a *connected* SQL Server 2022
(measured: both emit `CAST(… AS DATE)` and return a `date`), which is why that
defect is recorded above as rendering-tier only. **30 caught, 0 missed, 1 proven
un-catchable.** The re-review then added guards for the rules the first pass had
left half-covered: `date_add`'s unit is now exhaustive on all three dialects (it
had been guarded on Postgres only), the fourth dispatch map has a dead-code check,
and `_validate_date_operands` gained the unit tier it shipped without. A final round
verified those fixes by breaking each one (9 more mutations, all caught), because
the previous round's report had asserted its fixes were clean without checking —
which is how the *preceding* round's unlanded correction got through.

**Total: 39 mutations caught, 0 missed, 1 proven un-catchable** across four
harnesses (pre-review, post-audit, live PG+MSSQL, and confirmation).

**Accepted cost.** MCP schema 104,042 → **108,329** chars, **no budget bump
needed** — but headroom is now **~1.5%**, well below the ~5% the budget file
targets, so item 103 will have to raise it. Breakdown, measured rather than
attributed: ~3,400 for the three nodes, ~700 for the agent-facing instructions
section, and **~50** for the `minimum`/`maximum` keywords Pydantic emits from
`DateAddExpr.amount`'s int32 bound. An earlier draft credited that last ~160 to the
non-temporal-operand rejection *message* — wrong in a checkable way, since a
runtime error string never appears in a tool schema at all. Recorded because this
note exists to steer the next budget decision, and the wrong version would have
sent someone shortening error messages instead of auditing constraints.

**Two bounds added after the audit, both measured against the live server
first.** (1) `DateAddExpr.amount` is bounded to signed 32-bit independently of
`max_interval_days`: the two bound different things, and only this one tracks
T-SQL's limit — `DATEADD(second, 2147483647, …)` succeeds and `…, 2147483648`
raises "Arithmetic overflow error converting expression to data type int", which a
deployment raising `max_interval_days` above 24,855 could reach. (2)
`extract`/`date_add` over a **non-temporal column** is now rejected at schema
validation: with an INTEGER operand, Postgres *errors* while MSSQL silently returns
`0` and `1900-01-03` (T-SQL implicitly converts an int to a datetime from
1900-01-01) — the same AST, a hard failure on one backend and a plausible wrong
answer on the other. Only a **bare column** operand is checked, since that is the
only case with a reflected type; a computed operand (an explicit cast, a CASE) is
left to the database, and the rejection message names the cast to use.

**Regression bar 9/16 → 10/16** (row 12). Stated honestly: row 12 was 🟡, not ❌ —
relative-date filtering was always composable with a caller-computed literal, and
this is native convenience. The larger outcome of the item is the timezone pin,
which fixed a correctness gap in a capability that had already shipped.

### 103. Query engine: non-equi/range joins + FULL OUTER / CROSS ✅ DONE

Generalized `JoinSpec` from equality-pairs to a full predicate tree and added the
two missing join types, taking regression-bar row 16 (a price-band join) from
inexpressible to executed on both real backends.

**What shipped.**

- **`JoinSpec.condition: Optional[WhereNode]`** — the *same* node type `where`
  uses, so a join can be a range/temporal/inequality join
  (`ON Sale.Price BETWEEN Band.Lo AND Band.Hi`, `ON Event.At >= Window.Start`).
  The equality `on` form stays as the common-case sugar and its rendering is
  byte-identical to before. `on` and `condition` are mutually exclusive at the AST
  layer; `extra_on` stays tied to `on`.
- **`JoinType` gains `"full"` and `"cross"`.** `full` compiles to
  `stmt.join(..., full=True)`; `cross` to `stmt.join(right, sa.true())` —
  `ON true` on Postgres, `ON 1 = 1` on MSSQL/SQLite, the same cartesian product
  to every planner. A `cross` join takes no condition and is rejected (not
  silently stripped) if given one.
- **`Policy.allow_cross_join`, default off.** Deny-by-default for the one join
  type whose cost is the product of its inputs and the only one exempt from the
  graph-connectivity rule. Cross joins still count against `max_joins`.
  Registered in `_DIRECTION_REVIEWED_GUARDRAILS` (item 115), since `allow_*`
  does not read as a ceiling.
- **The ON clause inherits every WHERE cap**, wired in at the two shared walks
  rather than as parallel checks: `iter_column_refs` gains a `JOIN_CONDITION`
  position (so the denied-column and item-49 masked-column rules apply by
  construction) and `iter_scope_expressions` gains join-condition expressions (so
  `max_expression_depth`/`max_expression_nodes`, the `max_case_branches` budget,
  the `date_add` interval cap and the item-117 date-operand type check all reach
  it). Depth, tree-wide predicate count and `max_in_list_size` are enforced
  through the same shared rules WHERE/HAVING/CASE use.
- **`IN (subquery)` is rejected in a join condition**, matching HAVING and CASE:
  `iter_query_scopes` descends WHERE/HAVING only, so a subquery there would never
  be validated as its own scope. Rejected with a typed error rather than left to
  fail on a compiler-internal `ctx=None`.
- **The join-graph rule generalized rather than special-cased.** A `condition` may
  name any table *already* in the graph (`JOIN c ON c.x = a.x AND c.y = b.y` is
  ordinary SQL, and `extra_on`'s same-two-tables restriction is a property of that
  sugar), must reference the table being joined, and must **not**
  forward-reference a table joined later — which SQLAlchemy would render as a
  broken or implicitly-cartesian FROM. The `on` path can only reach the original
  check, so its message is unchanged.
- **Audit shape** records the join type plus whichever form was used, with a
  condition normalized through the same `_where_shape` as WHERE — operators and
  column names, never a literal.

**Coverage.** `tests/unit/test_nonequi_joins.py` (38 tests across AST form, graph
rules, caps, per-dialect rendering and the audit shape),
`tests/integration/test_nonequi_join_end_to_end.py` (9 tests executing every form
through the REST pipeline, including bar row 16), new `JOIN_CONDITION` cases in
the visitor contract and the adversarial suite's denied/masked expression-position
matrix, and 8 new live cases in `tests/integration/test_cross_dialect_differential.py`
run against real Postgres **and** real SQL Server. Two client-builder tests and
two `test_service.py` usage-signal tests round it out.
`test_every_join_type_has_a_live_both_dialects_case` makes a live case mandatory
for any future `JoinType` and lives in the **unit** tier, not beside the cases it
guards — the differential module is marked `postgres_live and mssql_live`, so a
gate placed there would only fire in the one CI job that has both servers. That
placement is item 102's recorded precedent, and the item-103 audit caught this
file's first draft getting it wrong.

**Mutation-verified — and the claim is written to be re-checkable rather than
taken on trust.** Each enforcement point below was broken deliberately and
confirmed to fail its guarding test *for that reason*. To re-verify any row:
break the named line, run the named test, expect a failure, revert.

| # | Enforcement point | Guarding test |
| --- | --- | --- |
| 1 | `allow_cross_join` gate (`policy_validation`) | `test_cross_join_is_denied_by_default` |
| 2 | join-condition predicate count, per scope | `test_join_condition_predicates_are_bounded_by_max_where_predicates` |
| 3 | ...summed **across** subquery scopes | `test_join_condition_predicate_budget_is_summed_across_subquery_scopes` |
| 4 | join-condition depth cap | `test_join_condition_depth_is_bounded_by_max_where_depth` |
| 5 | join-condition `max_in_list_size` | `test_join_condition_in_list_is_bounded_by_max_in_list_size` |
| 6 | `value_subquery` rejected in a join condition | `test_subquery_in_a_join_condition_is_rejected` |
| 7 | `JOIN_CONDITION` refs yielded by the item-96 visitor | `test_visitor_covers_every_position_with_expected_refs` + the adversarial denied/masked matrix |
| 8 | join-condition expressions in `iter_scope_expressions` | `test_expression_in_a_join_condition_is_bounded_by_max_expression_nodes` |
| 9 | ...carrying the `max_interval_days` cap | `test_date_add_inside_a_join_condition_is_bounded_by_max_interval_days` |
| 10 | ...carrying the `max_case_branches` cap | `test_case_branches_inside_a_join_condition_are_bounded` |
| 11 | forward-reference rejection in the join graph | `test_condition_cannot_forward_reference_a_later_join` |
| 12 | condition must reference the joined table | `test_condition_must_reference_the_table_being_joined` |
| 13 | join-condition columns resolved at schema validation | `test_join_condition_unknown_column_rejected` |
| 14 | compiler `full=` flag | `test_full_outer_join_renders_as_full_outer` |
| 15 | compiler `isouter`/`full` not swapped | `test_left_join_renders_left_outer_not_full_outer`, `test_left_join_is_not_silently_promoted_to_full` |
| 16 | AST: `on`/`condition` mutually exclusive | `test_condition_and_on_together_are_rejected` |
| 17 | AST: `cross` takes no condition | `test_cross_join_takes_no_condition` |
| 18 | audit shape records the join condition | `test_join_condition_appears_in_the_normalized_audit_shape` |
| 19 | audit shape records the join type | `test_join_shape_records_the_type_and_omits_the_form_not_used` |
| 20 | `_usage_signal_targets` survives `on is None` | `test_usage_signals_survive_a_range_join_that_carries_no_on_pair` |
| 21 | cross-joined table still reaches table policy | `test_cross_join_to_a_denied_table_is_rejected` |

**What the first pass missed, recorded because it is the useful part.** The
initial mutation run covered rows 1–2, 4–8, 11–12, 14, 16–17 and caught 13 of the
16 points it probed. Three misses were real: nothing pinned the **audit shape**
(the item-102 lesson repeating — the normalizer is a third un-foldable recursion),
nothing pinned **join-condition column resolution** at the schema layer, and one
mutation string failed to match. Rows 3, 9, 10, 13, 15, 18–21 were added
afterwards, most of them in response to the completion audit rather than the
mutation pass — which is the honest lesson: mutation-testing the rules an item
*adds* does not probe the existing consumers of a field whose **type** the item
changed. Row 20 is exactly that class, and it was a live bug.

**Regression bar 10/16 → 11/16** (row 16). Unlike row 12, this row was a genuine
❌ with no composition escape: a range join is not two queries plus a client
merge, it is a join the AST could not describe.

**A pre-existing divergence surfaced and deliberately left standing.** An outer
join can NULL out the column a query orders by, and Postgres sorts those NULLs
last on ASC while SQL Server sorts them first — the same AST, the same row set, a
different order. Measured on both live servers, this **predates item 103**: a
plain LEFT JOIN diverges identically, and item 103 only makes it reachable from a
second join type. Not fixed here, because the only in-engine fix is
`OrderBySpec.nulls`, which item 74 deliberately rejects on MSSQL rather than
emulating with a synthesized CASE sort column — changing that posture is a
maintainer decision, not a side effect of this item. Pinned by
`test_outer_join_null_ordering_diverges_and_predates_item_103` so it is recorded
rather than rediscovered.

### 104. Query engine: set operations (UNION / INTERSECT / EXCEPT) ✅ DONE

**Shipped.** `StructuredQuery.set_op` combines this query's rows with further
queries via `UNION` / `INTERSECT` / `EXCEPT` (`all: true` keeps duplicates), the
first **new scope container at the top level** and the first since item 97
phase 1's `value_subquery`. Regression bar 11/16 -> **12/16** (row 8).

**The AST shape, and why it is not the one the plan sketched.** The plan called
for "a new top-level shape wrapping N `StructuredQuery` arms". What shipped is an
optional `set_op` on `StructuredQuery` whose **carrying query is arm 1**. A second
top-level type would have turned every signature in the pipeline into a
`Union[StructuredQuery, SetOperationQuery]` — REST routes, MCP tools, templates,
audit, approval, cost estimation — or made `from`/`select` optional on every
query. The failure mode of that change is a consumer that quietly handles one
member, which is the exact class the item-103 audit found and item 102 hit five
times. One top-level type means every existing consumer keeps working, and the
ones that must now see *every arm* are found by a single question: does this walk
`iter_query_scopes`? Full reasoning in `docs/PRODUCT_GUIDE.md`'s Decision Log
entry for 2026-07-27.

Four consequences of that shape, each decided rather than inherited:

1. **Clause placement mirrors SQL.** The carrying query's `where`/`joins`/
   `group_by`/`having` describe arm 1; its `order_by`/`limit`/`offset` bound the
   combined result — exactly how `SELECT … WHERE … UNION SELECT … ORDER BY …
   LIMIT …` distributes. An arm setting any of the four is rejected at the AST
   layer with one typed message rather than producing three dialect-specific
   errors. `top_n` cannot be combined with `set_op` at all (its refs resolve
   against table columns the combined result no longer has) — the same
   reject-rather-than-grow-a-second-materialization-path posture item 101 took
   for `group_by` + window. Arity is checked at the AST layer; arms do not nest.
2. **An arm is a scope at the SAME depth as its carrier.** `max_subquery_depth`
   bounds caller-authored *nesting*, and an arm is a sibling SELECT. Depth+1
   would have charged a two-arm union against an unrelated budget and — worse —
   made an arm look like an `IN (subquery)` to the two rules that branch on
   `depth > 0`. A set operation *inside* a subquery correctly inherits that
   subquery's depth and does get them.
3. **The compound is wrapped in a derived table before `LIMIT`.** Measured, not
   assumed: SQLAlchemy's MSSQL dialect **silently drops** `.limit()` on a
   `CompoundSelect` — no `TOP`, no `FETCH`, no error — while Postgres renders
   `LIMIT` normally. `clamp_limit` is a policy guardrail, so the naive form is an
   unbounded response on one dialect only. Wrapping makes it a limit on a plain
   SELECT (both dialects render it correctly) and gives `ORDER BY` real
   derived-table columns instead of a bare output name. This is the engine
   enforcing its *own* cap, not synthesizing caller structure, so it is not the
   item-74 line.
4. **`INTERSECT ALL` / `EXCEPT ALL` are rejected on MSSQL and SQLite.** T-SQL has
   no `ALL` form of either, and dropping the flag would return *fewer* rows than
   asked with no error anywhere. SQLAlchemy renders the invalid keyword for every
   dialect with no guard of its own — verified by execution, where it is a live
   syntax error rather than a compile error. So it is a `DialectAdapter.set_operation`
   method that raises and names the primitive to use instead (the `array_agg`
   posture).

**Every arm is independently governed.** Each arm is validated as its own scope
(table/column allow-deny, the masked-column rule, schema resolution against its
own tables) and *compiled* through the same `_compile_scope_body` a single query
uses — so it carries its own mandatory row filters and its own `min_group_size`
floor, including item 118's fan-out refusal. There is deliberately no second
compile path where one could be forgotten. An arm cannot reference another arm's
table (arms are siblings, not a correlated scope), and cross-connection joins in
an arm get the outer query's `join_group` rule rather than the stricter
single-connection rule an `IN (subquery)` gets.

**Cap.** New `Policy.max_set_op_arms` (default 3, counting arm 1, summed
tree-wide; `0` disables set operations per connection). The default is a
judgement, and the honest reason it can be low is that it is not the query's cost
bound: every other count cap is already summed across the arms, so N arms share
one budget. This bounds only the extra scans plus the dedup sort.

**Two pre-existing single-scope holes closed on the way**, both named rather than
folded in silently:

- `sensitivity_approval_reasons` (the item-92 human-approval trigger) walked only
  the outer query, so a catalog-labelled sensitive column reached from inside an
  `IN (subquery)` never tripped the gate. **True since item 97**, not an item-104
  regression. It now walks `iter_query_scopes`, fixing subqueries and arms
  together.
- The 32C catalog usage signals had the same assumption, with fidelity rather
  than safety consequences, and were fixed the same way.

**Coverage.** `tests/unit/test_set_operations.py` (33 cases: AST rules, scope
enumeration, per-dialect availability, and one test per consumer that used to
assume a single SELECT — audit shape, applied masks, approval gate, fingerprint,
usage signals); `tests/security/test_set_operation_boundary.py` (20 adversarial
cases: denied table/column/mask hidden in a later arm, caps summed across arms,
the arm-count cap and its tree-wide sum, filters and the k-anon floor reaching
every compiled arm, the MSSQL limit survival, the ORDER BY table-column
fallback, an unvalidated arm failing closed); `tests/integration/
test_set_operation_end_to_end.py` (8 executed cases including regression-bar row
8); and 8 live differential cases in `tests/integration/
test_cross_dialect_differential.py` run against **real Postgres and real
MSSQL**. `tests/unit/test_client_builder.py` gains `.union()/.intersect()/
.except_()` coverage.

**Mutation-verified.** All **17** enforcement points this item adds were broken
one at a time against the suite; the first pass caught 15 and the two genuine
misses were real — the aggregate-limit ceiling using `any()` instead of `all()`
(one raw-row arm would have got the 1000-row aggregate ceiling instead of 100),
and the set-op `ORDER BY` allowing a raw table-column fallback (which SQLAlchemy
resolves by adding the table to the FROM clause — an unasked-for cartesian
product). Both got tests; the second pass caught 17/17.

**Follow-up round (same day, after the completion audit's rating review).** Three
residual gaps the audit left open were closed rather than carried:

1. **Arm select TYPES are now checked**, closing the measured divergence above —
   an integer column against a text cast of it errored on Postgres and *succeeded*
   on SQL Server. `_validate_set_op_arm_types` compares coarse type families
   (numeric / text / boolean / temporal) at each position, only where two or more
   arms have a statically-knowable one, so integer-vs-numeric and
   date-vs-timestamp stay legal and anything unknowable (arithmetic, functions,
   CASE) is left to the database. The differential test that recorded the
   divergence as an accepted residual was **inverted, not deleted** — the item-118
   precedent.
2. **`ExplainResult.tables` and the candidate simulator** now report every scope
   (item 121, closed in the same pass).
3. **The masked-column list's set-op semantics are stated rather than assumed**: it
   is a UNION across arms, so a column masked in one arm reads as masked for the
   whole output column. That over-states protection, never under-states it, which
   is the safe direction for a field distinguishing masked from denied; a per-arm
   breakdown would need an audit-event shape change. Pinned by a test.

**MCP context budget** rose a measured **1,485 chars** of tool schema plus 778 of
instructions (2,263 total; 109,252 -> 111,515, measured against base commit
e735735 in a worktree), against item 103's prediction that a new top-level shape
would breach the ceiling badly. `SetOpSpec.arms` is a `$ref` to the
`StructuredQuery` already in `$defs`, so nothing was duplicated. The ceiling was
raised 110,000 -> 116,000 after a trim pass found no maintainer rationale left in
any model docstring to move out. A first draft of this figure said 1,576 and was
wrong — it double-counted the `SetOpSpec` docstring, which is already inside the
1,145-char `$def`; the correction is recorded in the budget test's own comment.

### 105. Query engine: CTE / derived table in FROM (non-recursive) ✅ DONE

**Shape — a named `WITH` block, not a union on `from`/`JoinSpec.table`.**
`StructuredQuery.ctes` is a new ADDITIVE list of `CteSpec{name, query}`; `from_table`
and `JoinSpec.table` stay `str` and resolve to a block when one of that name is
declared. `ENGINE_EXPRESSIVENESS_PLAN.md` §4 Phase 4b had specified the union; that
is the same shape item 104 rejected one day earlier and it lost again here, so the
plan text was corrected rather than left contradicting the code. The deciding
argument is the **unaware consumer**: with a union, every site reading
`query.from_table` receives a model where it expected a string — `referenced_tables`
putting a non-string into a set of table names, `normalize_query_shape` recording it
as the table read, `policy.table_allowed` handed a model — and Pydantic cannot flag
any of it, because the field is legitimately both. With a name, a consumer that has
never heard of a cte treats `"daily"` as a table and reflection REJECTS it. The
unaware consumer fails closed. Efficiency (one block compiled once, referenced N
times) and expressiveness (a block is reusable across `from` and several joins,
which an inlined derived table is not) agreed but did not decide it.

**Absorbs item 97 phase 2.** An inline derived table is written as a named block, so
the derived table is implemented once, and item 97 is now fully `✅ DONE`.

**Structural rules** (`_validate_cte_constraints`, beside item 97's subquery rules so
both scope-container rule sets read against the one `iter_query_scopes` authority):
only the ROOT query may declare `ctes` (written as "any scope that is not the root",
so a fourth container is covered by default); a block may reference only an EARLIER
block, which makes declaration order dependency order **and makes a recursive cte
structurally inexpressible** rather than merely forbidden; a block name may not
collide with a table the policy has a rule for; a declared block must be referenced;
names are unique case-insensitively (the one purely local rule, in the AST layer).

**Caps.** New `Policy.max_cte_count` (default 3) — not summed tree-wide, and that is
a property of the shape rather than an exemption: only the root declares blocks, so
there is no second place for a count to hide. `max_subquery_depth` is charged along
the REFERENCE CHAIN (`cte_chain_depths`), so two independent blocks each cost 1 while
a block reading a block costs 2 and the default cap of 1 denies it. Every summable
cap already counts block bodies, because `iter_query_scopes` yields them.

**A block carries no `max_rows` clamp**, following item 97's `_compile_in_subquery`
for the same reason: `max_rows` bounds the RESPONSE, and a block's rows are input to
a join or an aggregate, so clamping would silently truncate the population a total is
computed over — the wrong-answer class items 102/117 established is worse than a
rejection. An explicit `limit` is honoured and clamped. What actually bounds a block:
`timeout_seconds`, `max_response_bytes`, the concurrency limiter, `max_cte_count`.

**Enforcement is per-scope, not skipped.** Every body compiles through the same
`_compile_scope_body`, inheriting mandatory row filters, column masks, the
`min_group_size` floor and item 118's fan-out refusal. A masked column may **not** be
projected by a block (its rows feed another scope, where the value could be filtered
or joined on) — the same rule and reason as item 97's `IN (subquery)` output check.

**`select_item_output_name` is now the single authority** on what a select item is
named in the result. It previously existed in two hand-maintained copies (the
`_*_alias` helpers and the compiler's inlined `alias = item.alias or ...` lines); a
cte makes it load-bearing, because the validator resolves outer `block.column` refs
against PREDICTED names while the compiler labels real ones. A test asserts the
predicted names equal the compiled `CTE.c.keys()` for every select-item shape —
agreeing with itself is not the same as agreeing with SQLAlchemy.

**Single-scope consumers** (the recurring miss the frontier notes name): the audit
shape now recurses into each body, the 32C usage signals and the sensitivity approval
gate skip the block NAME (teaching 32C a phantom table would be unreconcilable by any
refresh), and `referenced_tables` excludes block names in both directions — an
allow-list would otherwise reject every reference, and a "tables read" report would
name something that does not exist.

**Verification.** **29/29 enforcement points mutation-verified.** The first pass
killed 18/23 and each of the four survivors was a real coverage gap — a WILDCARD
column rule (`{"*": [...]}`) is what makes the "a cte output name is not a physical
column" skips observable, which is why a table-scoped rule proved nothing there. 63
unit + 20 adversarial-boundary + 6 end-to-end tests, and 4 cross-dialect differential
cases executed against **live Postgres and live SQL Server** with rows compared
equal. Regression bar 12/16 -> **14/16** (rows 3 and 6).

One test in that count is worth naming because it initially proved nothing: the
"a nested scope can READ a block" case first joined the block in the outer query
too, which rendered identically whether or not `_WhereCtx` carried `cte_objects`.
The mutation survived it. It now references the block ONLY from inside the
subquery, so nothing else can put the `WITH` clause into the statement.

**Found by this item's own completion audit and fixed before it shipped**, listed
because each is the kind of defect a green suite tolerates:
1. A block projecting two same-base-named columns escaped a raw
   `sqlalchemy.exc.DuplicateColumnError` from inside validation — a 500 where a
   typed 4xx belonged, and item 119's failure class (one column silently
   unreachable under the name the caller used). Now a typed rejection pointing at
   the `as` alias that resolves it.
2. `max_cte_count` ran AFTER the O(N x tree) structural walks it exists to bound,
   so a caller could drive that work with N far above the cap. It is now first.
3. **A cte body could join to a second connection while the identical
   `IN (subquery)` could not** — cte bodies are validated in their own
   dependency-ordered loop, which ran before the `depth > 0` branch the item-97
   check lived in. Not a bypass (the `join_group` rule still applied), but a
   guardrail that depended on which container the caller picked. The check is now
   a shared helper both containers call, and it names the real container, since
   the old message reported a set-operation arm nested in a block as an
   `IN (subquery)` the caller never wrote.
4. `referenced_tables`'s `cte_names` defaulted to `frozenset()` — fail-OPEN, since
   a caller who forgot it would report a block's name as a table that was read. It
   now derives the names from the query, so the forgetful call is correct.

**Side finding — item 122**, pre-existing and unrelated to ctes: `_unique_column_sets`
crashed on any non-`Table` FROM element, so item 118's floor raised `AttributeError`
on any aliased join. See its own entry.

### 106. Query engine: correlated / EXISTS / scalar subqueries ✅ DONE

The last item of the flagship engine pillar, and the only one that deliberately
REMOVES an invariant the rest of the engine rests on: that every scope resolves
against its own tables and nothing else.

**Correlation is declared, not ambient.** `StructuredQuery.correlate` lists the
outer columns a nested subquery may read. SQL makes every enclosing column
implicitly visible; QueryGate does not, because the enforcement chokepoint is the
canonical visitor, and a ref resolving against a scope the visitor was not looking
at is exactly the silent policy-and-mask bypass the plan's invariant 2 names as its
most important rule. Each declared ref is checked against the **enclosing** scope's
name map for table allow-deny, column allow-deny and the mask rule. An UNdeclared
outer ref is rejected exactly as it was before this item, so the pre-106 behavior is
the default and correlation is opt-in per subquery.

**Reach is one level, and that had to be enforced rather than assumed.** The first
implementation resolved a child's declared refs against the parent's already-
reflected tables — which include the tables the PARENT had correlated to. That made
correlation transitively reach a grandparent, so "one level" held in name only. Found
by the grandparent case in `test_correlation_boundary.py`; the fix resolves against
the parent's OWN from/join/cte names.

**Scalar subqueries return exactly one row by construction.** A `value_subquery` on
a scalar comparison op must be an aggregate with no `group_by`. `LIMIT 1` was
rejected because it picks an arbitrary row — a wrong answer with no error, the class
items 102/117 established is worse than a rejection — and letting the backend raise
was rejected as dialect-dependent and post-execution. The required shape is also
what the use case actually is: "> the overall average", "> this customer's own
average". A `set_op` inside a scalar subquery is refused for the same arity reason.

**`EXISTS` is an operator on `Predicate`, not a third `WhereNode` member.** A new
union member would force `iter_where_predicates`, `_compile_where`,
`predicate_column_refs`, `where_depth` and both write validators to learn a shape
whose failure mode is a consumer silently handling only what it knows — the argument
items 104 and 105 already turned on, now settled precedent. It behaves like the
`is_null` operators that take no value. The new ops live on a read-only
`ReadCompareOp`: `CompareOp` is shared with the write AST, so widening it would have
advertised `exists` in the write tool's MCP schema while the write path rejects it,
which is precisely item 114's defect. A new test pins the operator-set relationship,
because the pre-existing field-level narrowing guard cannot see an operator change.

**Placement.** WHERE takes EXISTS and both subquery shapes. HAVING takes a SCALAR
subquery (comparing an aggregate to an aggregate is what HAVING is for) but not
EXISTS (a per-row test has no meaning after grouping) and still not `IN (subquery)`
— item 97's rule, deliberately not widened. HAVING previously compiled with
`ctx=None`, which WAS the compiler-side backstop for that rule; since it now needs a
ctx, the backstop is preserved explicitly as `_WhereCtx.allow_value_set_subquery`
rather than quietly lost.

**Caps.** `max_correlated_refs` (default 2) summed tree-wide; `max_subquery_depth`
charges an EXISTS scope like any other nesting; every count cap already sums across
the new scopes because `iter_query_scopes` yields them. Stated honestly in the
field's own comment: this caps the correlation SURFACE, not its cost — a correlated
subquery is re-evaluated per candidate outer row, and what bounds that is
`timeout_seconds`, the concurrency limiter and item 26's cost gate where enabled.

**Audit.** An EXISTS scope's full shape is recorded, and so is each subquery's
`correlate` list — the exact set of outer columns a nested scope was permitted to
see is the most security-relevant fact about a correlated query, and it is column
identifiers only, never values.

**One listed capability deliberately did NOT ship: a scalar subquery in a SELECT
projection.** It is the position with the worst cost profile (once per output row)
and the widest blast radius (a new `SelectItem` member reaching output naming, the
ref-position taxonomy, set-op arity and `top_n`), and it is the one shape already
composable from exposed primitives — an aggregating cte plus a LEFT JOIN, which is
only a real recipe *because* item 105 shipped. Recorded in the Decision Log rather
than left as a silent gap between the item's write-up and its code.

**Verification.** **18/18 enforcement points mutation-verified**, across the
validators, the compiler, the audit shape and the scope walk. One survivor on the
first pass was informative rather than a gap: the masked-correlated-ref check is
already covered by the generic per-scope mask rule, so it is defence in depth — the
test now matches its specific message, which pins the layer instead of pinning
neither. 12 unit + 23 adversarial-boundary + 4 end-to-end tests, and **5 cross-dialect
differential cases executed against live Postgres AND live SQL Server** with rows
compared equal: the EXISTS/NOT EXISTS partition, the row-11 scalar comparison, a
correlated per-outer-row scalar subquery, the HAVING position, and `NOT EXISTS` over
a nullable correlated column (the classic `NOT IN`-vs-`NOT EXISTS` divergence,
recorded by execution rather than assumed).

Two test-design choices are load-bearing rather than incidental. The EXISTS cases
filter on `status='cancelled'` because every customer in the demo seed has orders —
an unfiltered EXISTS/NOT EXISTS partition is all-vs-none, which an implementation
IGNORING the correlated row reproduces exactly. And the correlated scalar case
asserts its answer DIFFERS from the same query using the global average, so a
dropped correlation cannot pass.

**Consumers of the new scope are tested, not assumed.** The audit shape (which
records the nested scope AND its `correlate` list), the 32C usage signals, the
item-92 approval gate and `referenced_tables_tree_wide` each get an explicit test
against an EXISTS scope — the group the plan's frontier note calls the recurring
miss. Composition with the other two containers is covered too: an EXISTS inside a
cte body and inside a set-operation arm, each correlating to the right parent.

**MCP cost, measured rather than assumed** (the discipline item 104 set): the two
new fields add ~67 characters to `StructuredQuery`'s JSON Schema, which sits at
37,073 against a 116,000-character total MCP budget.

### 107. Batch query execution double-reserves quota on an approval retry ✅ DONE

**Effort: S. Priority: medium (real throughput bug, narrow blast radius).
Depends on: none.** Surfaced by the 2026-07-23 technical review
(`TECHNICAL_REVIEW.md`).

**Why it mattered.** `execute()` reserves the per-principal query quota (item 50)
at the very top — deliberately before queuing or touching the DB, so a
rate-limited caller doesn't even consume a concurrency slot. The item-92 approval
gate runs *later* in the same call, after cost estimation. So when a query trips
the gate, one quota unit is already spent. Over MCP, `_execute_batch_item`
handles the resulting `ApprovalRequiredError` by asking the elicitation resolver
for a token and, if granted, calling `execute()` **again** — which reserved a
*second* unit for what is logically one approved query. A principal running
interactive-approval batches therefore burned roughly two quota units per
approved query, silently ~halving effective throughput. No test asserted quota
consumption across the approval-retry path, so it passed a casual read.

**Why the retry, specifically — and not the REST 428 flow.** The REST path
answers an over-threshold query with `428`, the caller signs a token and
*resubmits a brand-new request*; that legitimately counts as a new request
(fresh admission, fresh HTTP call). The double-count is unique to the **in-session
batch retry**, where the same logical `execute_many` item is re-driven internally
after the first attempt paused. So the fix is scoped exactly there.

**What shipped.**

- `ApprovalRequiredError` gained a `quota_reservation` attribute (default `None`,
  typed loosely to avoid a `core` -> `execution` import). `execute()` stashes its
  in-flight reservation there as the exception leaves the method.
- `execute()` gained a private `_reserved_quota: Optional[QuotaReservation]`
  parameter. When set, it reuses that reservation instead of calling
  `enforce_query_quota` again — so no second unit is counted, and the response
  bytes still attribute to the original window entry.
- `_execute_batch_item` threads `exc.quota_reservation` into the retry
  `execute(...)`.

Idempotent by construction for the disabled/unattributable cases: when quota is
off or there's no principal, the reservation is `None` on both attempts, so
reserving twice records nothing twice — the fix only changes the enabled path,
which is the only one that double-counted.

**Coverage** (`tests/unit/test_mcp_elicitation_approval.py`):
`test_approval_retry_consumes_exactly_one_quota_unit` enables a request quota,
drives an approval-required-then-approved batch item through the real
`execute_many` retry seam (same harness as the existing retry test), and asserts
the in-process quota window for `(connection, principal)` holds exactly **one**
entry. Verified it bites: with the reuse reverted, the window holds two and the
test fails `2 == 1`.

**Hard boundaries honored.** No change to what the quota caps are or when the
approval gate trips (the estimate/sensitivity trigger still re-evaluates on the
retry); audit and metrics still record the true sequence (a paused attempt then a
successful one). Purely a fix to *how many times* one logical query reserves quota.

### 108. Write-preview diff runs the full DML before the affected-row cap is checked ✅ DONE

**Effort: S. Priority: medium (resource-exhaustion / lock-contention risk on a
preview-only endpoint). Depends on: none.** Surfaced by the 2026-07-23
technical review (`TECHNICAL_REVIEW.md`).

**Why it mattered.** `WritePreviewService.preview()` computed the affected-row
count with a policy-checked `COUNT(*)`, but when `include_diff=true` it called
`_mutation_diff` unconditionally — and for an `UpdateStatement` that executes
the *real* UPDATE inside the (later-rolled-back) transaction to read back the
committed-shape old→new values. Only the rows *shown in the response* were
capped (`max_diff_rows`); the row-locking UPDATE against **every** matching row
ran first. So a caller could point `include_diff=true` at a deliberately broad
WHERE and force a full-table UPDATE — taking row locks, generating WAL/redo, and
contending with live writers — purely to preview a write that would then be
**rejected outright** as over `max_affected_rows`. All the cost of the write,
none of the authorization, on a preview-only endpoint.

**What shipped.** `_mutation_diff` gained a keyword-only `within_cap: bool`,
passed from `preview()` as `affected <= max_affected_rows`. The DML-executing
branch is now gated on it. Crucially the fix does **not** drop the feature: the
method already had a non-DML path (applying the statement's SET to the
before-rows in Python — the documented fallback for a composite/absent primary
key), so an over-cap preview reuses that and still returns a useful bounded
diff. Only the DML is skipped. The Python fallback is an approximation — it
can't reflect DB-side defaults, triggers, or type coercion — which is the right
trade for a write that will not be permitted to run anyway.

A DELETE preview was never affected: it only SELECTs the doomed rows
(`LIMIT max_diff_rows + 1`) and runs no DML, so its diff is unchanged and still
available over-cap.

**Coverage** (`tests/integration/test_write_preview_end_to_end.py`). Proving
this needs observing the SQL actually issued — the preview rolls back either
way, so the *data* is identical whether or not the DML ran; the defect is work
performed, not end state. `_record_sql` hooks SQLAlchemy's
`before_cursor_execute` on the engine the `sqlite_app` fixture exposes through
its monkeypatched `get_engine`, and the tests assert on the statements seen:

- `test_over_cap_update_diff_never_runs_the_dml` — over-cap UPDATE preview with
  `include_diff=true` issues **zero** UPDATE statements, still returns a
  populated diff with the proposed new value, reports
  `within_affected_cap: false`, and changes nothing.
- `test_within_cap_update_diff_still_runs_the_dml` — the **positive control**:
  within cap, exactly one UPDATE is issued. Without this, the fix could have
  been "disable the DML-backed diff entirely" and still looked green.
- `test_over_cap_delete_diff_lists_rows_without_running_dml` — an over-cap
  DELETE keeps its bounded diff and issues no DELETE.

Verified the regression test genuinely bites: with the `within_cap` guard
reverted, `test_over_cap_update_diff_never_runs_the_dml` fails while the
positive control still passes.

**Hard boundaries honored.** No new policy field (reuses `max_affected_rows`),
no change to what a preview may show, masking still applied to every diff row,
and the preview remains non-mutating (the rollback is untouched).

### 109. MCP `run_structured_writes` has no batch-size cap ✅ DONE

**Effort: S. Priority: medium-high (the write path had weaker sizing guardrails
than the read path it was modeled on). Depends on: none.** Surfaced by the
2026-07-23 technical review (`TECHNICAL_REVIEW.md`).

**Why it mattered.** The read path caps how many queries one call may carry —
`validate_batch_size(len(queries), policy)` against `Policy.max_batch_size`,
enforced at **both** transports (`api/routes.py`, `mcp/tools/query.py`). The
write path had no equivalent at any layer: `WritePolicy` carried no batch field
at all, and `mcp/tools/write.py` accepted an unbounded `writes: List[...]`. A
caller could submit an arbitrarily long batch of *individually legal,
individually in-cap* writes in a single MCP call, each running the full
validate→compile→execute pipeline and taking locks in sequence — multiplying
cost and lock time per call far past what the same principal was allowed on the
read side. `max_affected_rows` is no defense here: it bounds one statement's
blast radius, and every statement in the batch satisfies it.

**What shipped.**

- `WritePolicy.max_batch_size` (`policy/models.py`), `default=10, ge=1` —
  deliberately the *same* default as `Policy.max_batch_size` rather than a new
  invented number, since the write path was modeled on the read path. A drift
  test (`test_write_batch_size_cap_defaults_to_the_read_path_value`) pins the
  two together so they can't silently diverge.
- `validate_write_batch_size(count, policy)`
  (`validation/write_policy_validation.py`) — the write sibling of
  `validate_batch_size`, same shape and error style.
- Enforced at **two** layers, deliberately:
  - `mcp/tools/write.py`, before the `mode` branch — so *both* preview and
    execute batches are rejected before any statement is validated, compiled,
    previewed, or run. (MCP is currently the only batched write transport; the
    REST write endpoints are single-statement.)
  - `WriteExecutionService.execute_many` — so the service layer is bounded by
    construction for any future caller, rather than depending on each transport
    remembering to check. This mirrors the read path, which likewise checks at
    each transport rather than in one place only.

**Coverage.** `tests/unit/test_governed_writes.py`: the read-parity default,
over-cap rejection, the **boundary** case (exactly at cap passes, one over
fails), `ge=1` validation, and an `execute_many` test proving the service layer
rejects before running anything. `tests/security/test_write_boundary.py` adds
the adversarial framing — `test_unbounded_write_batch_is_capped_not_a_dos_vector`
(5000 individually-legal writes in one call) and
`test_write_batch_cap_is_enforced_before_any_statement_runs` (up-front, not
partway through — otherwise an over-size batch would still pay for, and commit,
every statement before the one that trips the cap).

Verified both new tests genuinely bite: with the `execute_many` enforcement
removed they fail; with it restored they pass.

**Not expanded.** `WritePolicy` guardrails are still outside the item-40/41
semantic-diff scope (`admin/access_diff.py` enumerates read-`Policy` fields
only). That is a pre-existing boundary, not a regression from this item; adding
write-policy diffing is its own separately-scoped piece of work.

### 110. `value_subquery` in a write's WHERE is validated at the wrong layer ✅ DONE

**Effort: XS. Priority: low-medium (defense-in-depth / clear error, not a live
bypass). Depends on: none.** Surfaced by the 2026-07-23 technical review
(`TECHNICAL_REVIEW.md`), Review Phase 3.

**Why it mattered.** `UpdateStatement`/`DeleteStatement` reuse the read
`WhereNode`, so a `Predicate.value_subquery` (item 97's `IN (subquery)`) is
structurally constructible in a write's WHERE — but neither
`write_policy_validation.py` nor `write_schema_validation.py` inspected it. It
only failed later, deep inside `compiler/write_compiler.py`'s `_compile_where`,
because the write compiler always passes `ctx=None`. Not currently exploitable
(the compiler-level failure is safe), but it failed at the *wrong layer* with a
compiler-internal error instead of a clean validation rejection, and it was a
latent trap: a future write-compiler change that ever passed a non-`None` `ctx`
(e.g. to support a write-side subquery feature) would silently reopen a bypass
this layer was never built to check.

**What shipped.** `validate_write_policy` now rejects any predicate whose
`value_subquery` is set, walking the *whole* WHERE tree via the existing
`_where_predicates` iterator (so a subquery hidden one boolean-group level down
is caught too), and raising a clean `QueryValidationError` *before* the per-column
allow/deny checks and long before the compiler. The message points the caller at
the primitives — "scope the target rows with literal or column predicates
instead" — the same reject-not-emulate posture as MSSQL `array_agg`/`NULLS`
(a genuine capability gap on the write path, not a policy denial). No compiler
change; no new policy field.

**Coverage.** `tests/unit/test_governed_writes.py` — a top-level
`value_subquery` in an UPDATE WHERE and one nested inside a DELETE's `and`-group
both raise `QueryValidationError` at `validate_write_policy` time. Both were
verified to bite: with the new check reverted they fail (validation does *not*
raise — proving the pre-fix "wrong layer" behavior), and pass with it restored.
The write + subquery security boundary suites still pass unchanged.

### 111. Duplicated WHERE-predicate tree walk across four validators ✅ DONE

**Effort: S. Priority: low (maintainability/drift-prevention, not a live bug).
Depends on: none.** Surfaced by the 2026-07-23 technical review
(`TECHNICAL_REVIEW.md`), Review Phase 3.

**Why it mattered.** `policy_validation.py`, `schema_validation.py`,
`write_policy_validation.py`, and `write_schema_validation.py` each hand-rolled
their own recursive WHERE-boolean-tree predicate enumerator — four byte-identical
copies of `if Predicate: yield; elif not_terms: recurse; else: recurse children`.
All four were correct, but the next time `WhereNode` grows a new combinator, a new
node type would need updating in four places — easy to miss one and silently open
a policy/schema hole in a forgotten copy. This is the exact class of bug item 96
was built to prevent for column refs (`iter_column_refs`), applied to the
predicate axis.

**What shipped.** One shared `iter_where_predicates(node)` in
`schema_validation.py` (beside item 96's `iter_column_refs`, the AST-walk home),
now public and documented as the single canonical WHERE-predicate walk. The other
three validators import it; their local copies (`_iter_where_predicates` /
`_where_predicates`) and the now-unused `Iterator`/`Predicate`/`WhereNode` imports
they required were deleted. `iter_where_and_having_predicates` (already in
`schema_validation.py`) now delegates to it too.

The consolidation went one level further than the four named enumerators: the two
*other* per-leaf boolean-tree recursions in `schema_validation.py` — the column-ref
walk `_where_column_refs` (feeds item 96's `iter_column_refs`) and the
column-validation walk `_validate_where_columns` — were also re-expressed as
`for pred in iter_where_predicates(node): …`, since both are purely per-Predicate
with no dependence on tree shape (provably identical output and document order).
The **only** remaining hand-rolled recursion over the boolean tree is
`_where_depth`, which genuinely needs the tree structure (nesting depth) and so
cannot be leaf-flattened — a legitimately distinct operation, not a missed
duplicate. So a new `WhereGroup` combinator is now handled in exactly one place
for every leaf-oriented traversal.

> **Correction (2026-07-26, from item 114).** That "only remaining" claim was
> wrong as written. `execution/write_preview.py` held a **fifth** leaf-oriented
> enumerator this consolidation missed (item 114 found it unreachable and deleted
> it), and `audit/events.py`'s `_where_shape` and the compiler's `_compile_where`
> are two further structure-dependent recursions that legitimately remain, in the
> same category as `_where_depth`. The accurate claim is: one shared predicate
> *enumerator*, plus a small set of deliberately structure-dependent walks.

**No behavior change**, by construction (the four walks were identical) and by
proof: the full default suite (1613) and the security suite (260) pass unchanged.
Added two direct contract tests in `tests/unit/test_reference_visitor.py` (the
item-96 visitor's test home) pinning the traversal — every Predicate leaf yielded
in document order through nested and/or/not groups, no `WhereGroup` node yielded,
and the bare-Predicate case — so a future edit to the now-single helper is caught
directly, not only transitively.

### 112. No scheduled (cron) CI run — dependency/security scans only fire on push/PR ✅ DONE

**Effort: S. Priority: medium (closes a real blind window between code changes,
cheap to add). Depends on: none.** Surfaced by the 2026-07-23 technical review
(`TECHNICAL_REVIEW.md`), Review Phase 2.

**Why it mattered.** `.github/workflows/ci.yml`'s only triggers were
`push: branches: [main]` and `pull_request`, so the SBOM/CVE audit, Trivy image
scan, secret scan, and adversarial/DAST suites ran only when someone happened to
open a PR or push to `main`. A CVE disclosed against an already-merged, unchanged
dependency (or the shipped image's OS/library layers) was not caught until the
next incidental change touched the repo. Separately, `make test-soak`
(`SOAK_ROUNDS=100`) was never invoked by CI at all — only the lighter 5-round
`test-load` ran in the `postgres-live` job — so a slow-degradation or pool-leak
regression that only surfaces after dozens of rounds passed every PR and was
caught only if a maintainer remembered to run `test-soak` manually before a
release.

**What shipped.** `.github/workflows/scheduled.yml` — a new workflow triggered on
`schedule` (daily at 07:00 UTC, off-peak) and `workflow_dispatch` (with an
optional `soak_rounds` input for on-demand runs), independent of any code change.
A non-cancelling `concurrency` group keeps two scheduled runs from overlapping;
`permissions: contents: read` is least-privilege (no write scopes); each job
carries a `timeout-minutes` bound (20/30/45) so a hung run can't burn the 360-min
GitHub default. Three jobs:

- **`dependency-audit`** — `poetry check --lock` (lockfile drift) then
  `scripts/generate_sbom.py`, the identical CycloneDX SBOM + `pip-audit`
  deny-by-default CVE gate `make release-check` runs, over the exact locked
  `main` ship set. This is the step that catches a newly-disclosed CVE against an
  unchanged dependency between code changes.
- **`image-scan`** — builds the production image and Trivy-scans it for
  HIGH/CRITICAL vulnerabilities, secrets, and misconfig (`--ignore-unfixed`,
  reviewed exceptions in `.trivyignore`), mirroring ci.yml's image-scan posture
  but on the nightly schedule.
- **`soak`** — stands up the same bare Postgres service the `postgres-live` job
  uses, seeds `querygate_demo` and creates the separate `querygate_stress`
  database the large-domain scenarios populate (both needed because
  `make test-soak` runs every `-m load` test — the concurrency-guardrail suite,
  the write-load suite, and the large-domain stress soak), then runs
  `make test-soak SOAK_ROUNDS=100` (overridable via the dispatch input).

**Docs.** `docs/RELEASING.md` gained a "Scheduled security scans and soak"
section documenting the cadence, the three jobs, and how to triage a red nightly
run (same as a failed release gate — a CVE, lockfile drift, or guardrail
regression landed on `main` without a code change to trigger the per-PR gates).

**Verification.** The workflow was linted clean with `actionlint` (which bundles
shellcheck, so the `run:` step shell was validated too) — no schema, expression,
`uses:`, or shell errors. Every job's command path was then executed locally and
proven green, not just asserted to be reused:

- **`dependency-audit`** — `poetry check --lock` → "All set!"; `poetry build` +
  `scripts/generate_sbom.py` → SBOM (47 components) written and `pip-audit`
  reported "no unreviewed known vulnerabilities (0 allowlisted)". The nightly
  will be green on day one, not red on a pre-existing finding.
- **`image-scan`** — `make scan-image` (the same Dockerfile build + Trivy
  HIGH/CRITICAL `--ignore-unfixed` deny-by-default scan the job runs via
  `aquasecurity/trivy-action`) exited 0 with no findings.
- **`soak`** — against a real Compose Postgres with `querygate_stress` created
  the same way the job seeds it, `SOAK_ROUNDS=2 make test-soak` ran the full
  `-m load` set (concurrency guardrails + large-domain stress soak + write-load),
  9/9 passed. The bounded round count proves the exact command path; the nightly
  runs the full `SOAK_ROUNDS=100`.

The cron *firing* is inherently only observable once merged and scheduled — a
config declaration, not runtime logic — so there is no in-repo test to add; the
acceptance criterion (a nightly/weekly workflow runs the CVE/SBOM/lockfile checks
and `make test-soak` against `main` independent of code changes) is satisfied by
the declared `schedule:` trigger plus every invoked command proven green above.

### 114. The write tool's MCP schema advertised read-only predicate fields it rejects ✅ DONE

**Effort: M. Priority: medium (agent-facing correctness + MCP token budget).
Depended on: item 93.** Its own PR, as the item's own text required — this is a
write-contract change, not a rider on a read-engine item.

**The defect.** `UpdateStatement`/`DeleteStatement` reused the READ `Predicate`
verbatim, so `run_structured_writes` was the largest MCP tool schema in the
product (38,964 chars — larger than the read tool). A write's `where` dragged in
`expr`/`value_expr` (item 100's entire `Expression` union), `value_subquery`
(item 110), and through that the whole read `StructuredQuery` definition —
`SelectItem`, aggregates, and after item 101 the window nodes too. **Every one of
those is rejected by `validate_write_policy` at runtime.**

**Why it mattered beyond bytes.** The schema is the agent's contract. Advertising
a field the server refuses invites the agent to build a write it will be denied —
the "hit a wall, route around the gate" failure `ENGINE_EXPRESSIVENESS_PLAN.md`
exists to prevent — and it spent that context on every single MCP session to do
it.

**The fix: narrow at the wire, converge internally.** `WritePredicate` and
`WriteWhereGroup` carry exactly what a write accepts (`col`, `col_fn`, `op`,
`value`, `value_col`, and boolean groups). They are a **narrowing**, not a
parallel grammar:

- `to_read_where` converts at the validation boundary, so the canonical predicate
  walk (item 111), `_compile_where`, and every downstream guarantee stay the
  single read implementation. There is no second compile path and no second
  predicate-*enumerating* walk: `to_read_where` is a structural conversion, the
  same legitimately-distinct category as `_where_depth` and `audit/events.py`'s
  `_where_shape`, and it fails closed on a node it does not recognise.
- The value-shape rules are **not restated**: `WritePredicate` validates by
  constructing the read `Predicate`, so one rule set is enforced (at parse time),
  and the two cannot drift on what `between` or `in` means.
- Read nodes pass through `to_read_where` unchanged, which is what keeps the
  runtime rejections reachable as **defence in depth** — they are still tested,
  now via `model_construct` (the only way to reach that state), and a new test
  asserts the parse-time refusal separately.

**Result: the MCP context budget went DOWN, not up.** **128,551 -> 104,042**
chars total (**-19%**); `run_structured_writes` 38,964 -> **14,455** (-63%).
The baseline is item 115's tree: it had added 1,851 chars over item 101's 126,700
(a wider `EffectiveGuardrails` in `describe_my_querygate_access`'s output schema)
without needing a budget bump, so quoting 126,700 understated the drop. `_MAX_TOTAL_CHARS` drops
132,000 -> 110,000 — back below item 100's 123,000, but ~2,000 **above** the
108,000 ceiling that predated item 100. (An earlier draft of this write-up
claimed the ceiling was lower than pre-item-100; it is not. The measured *total*
is what improved.)

**A fifth hand-rolled predicate enumerator found — and deleted.**
`write_preview.py`'s `_reject_subquery_in_write_where` traversed the boolean tree
with its own stack, a copy item 111's four-validator consolidation missed. Once
it was on the canonical walk the more useful fact emerged: it was **unreachable**
— all three call sites ran it immediately after `validate_write_policy`, which
rejects the same thing over the same walk (item 110) — and it had zero tests. A
layer that cannot fire and is not tested is not defence in depth, so it was
deleted; the schema refusal and the validator rejection remain.

**Coverage.** `tests/unit/test_mcp_write_tool.py`: the schema contains none of
nine read-only definitions (parameterized, so a future reuse of a read model
fails loudly), it still advertises what writes *do* accept, **18
previously-valid write payloads round-trip unchanged** through the real
discriminated union (the "no caller can break" acceptance criterion), and eight
malformed value shapes are still refused by the read predicate's own rules.
`tests/unit/test_governed_writes.py`: the parse-time refusal of
`expr`/`value_expr`/`value_subquery`, the defence-in-depth validation rejections
for all three (both `expr` and `value_expr` disjuncts, not just the first), the
write-vocabulary error property, the dotted-ref rule on both column fields, and
— the gap the audit caught — **every branch of `to_read_where`**: and/or/not
conversion, nested groups, read-node identity pass-through, fail-closed on an
unknown node, and a denied *and* masked column buried inside a group. Those
boolean branches shipped with no test at all; a swapped `and_terms`/`or_terms`
would have made `DELETE ... WHERE a OR b` execute as `a AND b`, with the preview,
the diff and the execution all agreeing on the wrong blast radius because they
re-derive from that one conversion.

**Accepted cost.** Python-level construction of a write statement now uses
`WritePredicate` instead of `Predicate` (a dozen test call sites migrated); wire
payloads are unchanged, which the round-trip test now pins field-for-field
across 18 payloads covering every `CompareOp`, both `col_fn` argument kinds and
top-level `and`/`or`/`not` groups. `col_fn` is deliberately KEPT on the write
predicate — it works on the write path today and removing it would have broken
currently-valid payloads, which the acceptance criteria forbid. One payload shape
*is* now refused earlier: a bare (non-dotted) `col`, which could never have
succeeded (it bypassed the ref walk and failed in the compiler) — recorded because
"every previously-valid payload still validates" is imprecise about it.

### 115. The hand-maintained guardrail-field lists had drifted from `Policy` ✅ DONE

**Effort: S. Priority: medium (operator-facing correctness on shipped surfaces).
Depended on: nothing.** Surfaced 2026-07-26 while shipping item 101's two caps.

**The defect.** Four surfaces answer "which caps are in force" and each kept its
own typed-out field list, with nothing checking any of them against `Policy`:
`admin/access_diff.py` (the item-40 semantic diff), `admin/models.py`'s
`EffectiveGuardrails` (the item-45 "my access" view, also served by the help
API), `help/service.py` (the queryable product guide), and
`api/admin_ui_routes.py` (the admin UI's policy panel — the shortest list of the
four, missing even `max_top_n`/`max_partition_by`). **Nine caps** added by items
68–72 (`max_where_predicates`, `max_in_list_size`, `max_case_branches`), 88
(`min_group_size`), 97 (`max_subquery_depth`), 100 (`max_expression_depth`,
`max_expression_nodes`) and 101 (`max_window_specs`,
`max_window_frame_offset`) were absent from at least one.

**Why that is more than untidy.** `/admin/config/diff` exists to make a staged
policy change legible *before* it ships. A cap it cannot see is reported to the
approving reviewer as **"no guardrail change"** — a governance surface stating the
opposite of the truth. Raising `max_window_specs` from 1 to 50 produced an empty
diff.

**The fix inverts the failure mode.** `GUARDRAIL_FIELDS` is now derived from
`Policy.model_fields` minus a small, reasoned exclusion set, the same
derived-not-listed posture as `DIALECT_ROUTED_EXPR_FNS`:

- structural allow/deny rules (tables, columns, masks, row filters) are excluded
  because `access_diff` already itemizes them properly — reporting them again as
  opaque scalars would be worse;
- the nested `WritePolicy` is excluded: its caps deserve their own change
  category, deliberately out of scope here and stated as such;
- `approval_sensitivities` is excluded because it is a *list* with no scalar
  permissiveness — and it now gets its own dedicated diff change rather than
  staying invisible, which is what the old lists did to it.

`EffectiveGuardrails` is **generated** from those fields, so the response model
cannot drift from the enforcement model in membership *or* type. It widens from
19 fields to 34 — additive for consumers, but a public response shape, which is
exactly why this was split out of item 101 rather than ridden along with it.

**The second-order trap, closed too.** Deriving the field set fixes "a new cap is
invisible" and leaves "a new cap is diffed **backwards**". Direction is not
derivable in general: a larger `min_group_size` suppresses *more* groups and the
same budget over a longer `quota_window_seconds` is a *lower* rate, so both are
tightening while every `max_*` cap loosens as it grows. A `max_*` name states its
own direction; every other guardrail must appear in
`_DIRECTION_REVIEWED_GUARDRAILS`, and a test fails until it does. **That test
caught two fields on its first run** — `approval_max_estimated_rows`/`_cost`,
whose direction had never actually been considered (they follow the normal
direction, but now that is stated rather than inherited).

**Coverage.** `tests/unit/test_policy_guardrails.py` is the guard whose absence
caused the rot: the partition over `Policy.model_fields` is exhaustive, every
excluded field still exists, every guardrail is scalar (so the next
`approval_sensitivities`-shaped field fails loudly instead of being compared as a
number), all four surfaces report the identical set, the generated model's types
match `Policy`'s, and every direction is either obvious from the name or
reviewed. `tests/unit/test_config_semantic_diff.py` adds behavior: a change to
**every** field in `GUARDRAIL_FIELDS` produces a change (driven off the constant,
so a future cap is covered automatically), the `max_window_specs` false negative
is pinned as a regression, both inverted directions are asserted in both
directions, and the `approval_sensitivities` change is asserted.

**Verified by mutation, 8 of 9 caught:** adding a collection field without
excluding it, a stale exclusion, either surface re-growing a private list, either
inverted field losing its inversion, a new ambiguously-named cap, and dropping
the `approval_sensitivities` change all fail the suite. The ninth — adding a
plain `max_*` cap — is *designed* to pass: it is auto-included everywhere and its
direction is unambiguous, so there is nothing left to forget.

### 116. A write's WHERE was exempt from every shape cap the read path enforces ✅ DONE

**Effort: S. Priority: medium (resource-exhaustion guardrail parity). Depended on:
nothing.** Surfaced 2026-07-26 by item 114's `auditors` audit — three of four
reviewers flagged it independently. Pre-existing since item 93; item 114 is what
made it salient, since the write filter then had its own type and therefore an
obvious home for the caps.

**The defect.** `validate_write_policy` enforced none of `Policy.max_where_depth`,
`max_where_predicates` or `max_in_list_size`. All three lived only in
`policy_validation.py`, which the write path never calls. The concrete one was the
in-list cap: a
`{"op":"delete","table":"orders","where":{"col":"orders.id","op":"in","value":[…1e6 ids…]}}`
compiled ~1M bind parameters and ran a `COUNT(*)` over them **before**
`max_affected_rows` was consulted — while the read path refused the same list at
1,000. Depth was at least fail-safe (~800 nested `not` levels return a clean 422,
not a `RecursionError`), but a write filter could nest ~250 levels where a read
caps at 5, and that tree is walked four times.

**The fix is one implementation of each rule, not a write-side copy.** All three
rules are now single functions in `policy_validation.py` —
`_check_where_depth`, `_check_predicate_count` and `_check_in_list_size` (the last
two extracted from inline code by this item) — and BOTH paths reach them. What
differs, deliberately, is the scope counted over: reads sum predicate counts
tree-wide across subqueries as item 97 requires, while a write filter is one tree,
so the write path composes the three through `enforce_predicate_shape_caps` and the
read path calls them from its own wider scoping. The composition adds no rule of its
own, so a fourth shape cap added to a primitive lands on both paths.

Proven by spying: three parameterized tests monkeypatch each rule function and
assert **both** `validate_policy` and `validate_write_policy` route through it. (The
first version of this test compared imported names for identity, which Python's
import semantics make true automatically — it would still have passed while the
count rule was implemented twice, which is exactly what the audit found.)

**Decision recorded: the write caps are the READ `Policy` fields, not new
`WritePolicy.max_*` ones.** A write's WHERE *reads* rows in order to select them —
the same reasoning that already applies the read allow/deny and masked-column rules
to a write filter (item 93). Splitting them would mean two numbers for one cost and
a second place to forget. The acceptance criteria asked for this to be decided
explicitly rather than defaulted.

**A batch now validates up front.** `_execute_many_atomically` validated policy
*inside* its per-statement loop inside the open session, so statement N's caps were
applied only after 1..N-1 had compiled and issued DML (rolled back — but the work
was performed, which is the defect this item is about). Policy for every statement
now runs before a concurrency slot is taken or a session opened, mirroring item
109's up-front batch-size check; schema validation stays in the loop because it
needs the connection.

**Coverage.** Unit (`test_governed_writes.py`): each of the three caps fires on a
write, each with an at-cap positive control (including the count cap, whose control
catches an off-by-one), the three spy tests above, and the `in`-list message's
target ladder (`col` / `col_fn(...)`), which nothing had asserted. Integration (`test_write_preview_end_to_end.py`): an over-size `in`
list is refused at **both** `/write/preview` and `/write/execute` with a clean 422,
and — the acceptance criterion that actually matters, proven the way item 108 proved
its own guard — `before_cursor_execute` observes that **not one statement reached
the database**, with a within-cap positive control so the guard cannot degrade into
refusing every `in` filter. Five mutations verified caught, including each rule
individually and the read path's own in-list check (so the extraction cannot have
silently weakened reads).

**Accepted cost.** A write filter that legitimately needed >1,000 `in` values, >100
predicates or >5 nesting levels now needs a policy change — the same conversation a
read of that shape has always required.

### 117. `date_bucket` over a non-temporal column diverges across dialects ✅ DONE

**Effort: S. Priority: medium (correctness; same class as a shipped guardrail).
Depends on: item 102.** Surfaced by item 102's confirmation review.

**The defect.** Item 102 added `_validate_date_operands`, rejecting `extract` and
`date_add` over a column that is not a date/time type — because the same AST was a
hard error on Postgres and a silent 1900-epoch value on MSSQL. `DateBucketSelectItem`
is the **third** date primitive and was not covered, while item 102's own
documentation described the general property ("A date primitive requires a date").
So the third one stayed broken while reading as covered — the same
documentation-outruns-code failure that item's write-up is otherwise about.

**Measured before deciding, which is what settled it.** `date_bucket` day-truncation
over an INTEGER column:

| Backend | Result |
| --- | --- |
| Postgres | `ERROR: function date_trunc(unknown, integer) does not exist` |
| SQL Server | `1900-01-02 00:00:00` |
| SQLite (internal) | `-4712-01-05` |

Three backends, three different wrong answers, none of them usable. That is what
turned this from a risky behavior change into a plain bug fix: rejecting these
queries takes nothing away from anyone, because no caller had correct behavior.
It was deliberately NOT folded into item 102 — at that point the risk was unknown,
and changing a long-shipped feature as a side effect of another item is the kind of
thing that should be a decision. The measurement made the decision easy.

**What shipped.** `_iter_date_operands` — one walk yielding every (column, label)
pair a date primitive applies to, across all three shapes — with
`_validate_date_operands` consuming it. Plus `test_every_date_primitive_is_covered_by_the_operand_rule`,
the guard that would have caught this when item 102 shipped: it enumerates the date
primitives and asserts each is reachable through the shared walk, so a fourth fails
until it is wired in.

**Testing.** Per-type unit cases for `date_bucket` (reject int/string, allow
timestamp), the coverage gate, an end-to-end rejection plus a positive control that
bucketing a real timestamp still works, and a live PG+MSSQL pair asserting both
servers now give the *same* typed rejection — a divergence is closed by making the
two agree, not by picking a winner. Mutation-verified: reverting the `date_bucket`
branch fails 4 tests, including the coverage gate.

### 118. `min_group_size` was defeated by any fan-out join ✅ DONE

The item-88 k-anonymity floor is compiled as `HAVING count(*) >= k`, which counts
**joined** rows rather than distinct underlying rows. Any join matching more than
one right-hand row per left-hand row multiplied a group's count and lifted a
single-row group above the floor, so the group was returned.

**Measured before the fix.** With `k=5`, one person at `salary=100`, five at
`salary=200`, and a 10-row table sharing a `tenant` value:

| query | result |
| --- | --- |
| no join (control) | `[200]` — floor works |
| `JOIN big ON person.tenant = big.tenant` (**equality**) | `[100, 200]` — floor defeated |
| `JOIN big ON person.id != big.id` (non-equi) | `[100, 200]` — floor defeated |

The equality row uses only the `on` form, which shipped long before item 103's
`condition` — so this was a **pre-existing** gap surfaced by item 103's audit, not
a non-equi-join regression, and a fix rejecting only inequality conditions would
have been theater.

**What shipped.** When `min_group_size` is set on an aggregate query, a join that
**can** fan out is refused with a typed `PolicyViolationError` rather than answered.
This is the posture item 101 already established for aggregate windows under this
floor: when the floor cannot be enforced correctly, fail closed instead of returning
an answer the policy believes is protected.

**The rejection is scoped, not a blanket ban** — which is the whole design, since a
deployment that sets the floor is exactly the one that still needs joins. A join
cannot fan out iff it pins a set of the joined table's columns by equality and that
set covers one of the table's unique keys, read from reflected metadata (primary
key, plus unique constraints and unique indexes — backends surface these
differently, so all three are read). Verdicts:

| join shape | verdict |
| --- | --- |
| onto the target's PRIMARY KEY (the ordinary dimension join) | allowed |
| onto a UNIQUE non-PK column | allowed |
| an equality `condition` onto the PK (item 103's spelling) | allowed |
| composite key via `extra_on` covering a composite unique key | allowed |
| onto a NON-unique column | refused |
| a range/non-equi condition | refused |
| a cross join | refused |
| an OR/NOT condition tree (pins nothing) | refused |

Both spellings of an equality join are read, so item 103's `condition` form is not
penalised for expressing the same thing a different way. The direction is
fail-closed: an unreflected uniqueness constraint costs a rejection, whereas a
missed fan-out would cost the guarantee.

**Unchanged for everyone else.** The rule only fires when `min_group_size` is set
(opt-in, off by default) and only on aggregate queries — a plain row read with a
fan-out join is governed by mandatory row filters, not group size. Both are
asserted.

**Coverage.** `tests/security/test_adversarial_security.py::test_k_anonymity_floor_cannot_be_defeated_by_a_fan_out_join`
executes against a real database and was *inverted from the test that pinned the
leak* — its earlier revision carried the instruction to do exactly that, and it
failed with that message the moment the fix landed. Eight parametrized unit cases
in `tests/unit/test_nonequi_joins.py` cover every row of the table above plus the
two "rule does not apply" cases.

**Doc reconciliation.** The disclosure added while the gap was open — in
`docs/THREAT_MODEL.md` QG-29, `docs/INFERENCE_RISKS.md` R3 (headline, gap bullet,
closing summary), `README.md`, `examples/policy.example.yaml`,
`Policy.min_group_size`'s docstring and `landing/security.html` — was replaced with
the accurate statement of what now holds.

### 119. `top_n` mis-resolves and DROPS a column when two projections share a base name ✅ DONE

**The defect.** `_apply_top_n` materializes a query in a derived table, but it
rebuilt both the outer projection and the aggregate rank targets by output
**name**. Two projections can legitimately share a base name — for example
`customers.id` and `orders.id`. SQLAlchemy disambiguates the derived table's keys,
but both columns still report `.name == "id"`, so name lookup selected
`customers.id` twice and silently dropped `orders.id`. In the grouped path the
same collision also made `ORDER BY orders.id` rank by `customers.id`, returning a
different row set without an error.

**What shipped.** `_apply_top_n` now binds all three derived-table relationships
by position, which is unambiguous because a derived table preserves its SELECT
list order:

- the grouped/aggregate materialization maps each written select reference to the
  derived column at the same position, so partition and ordering references resolve
  to the column the caller named;
- the ranked query carries every materialized column positionally; and
- the outer projection and its alias map carry those same positions, so both
  same-named columns reach the response (`id` and SQLAlchemy's stable
  disambiguated `id_1` key).

Duplicate output names were not rejected globally: ordinary non-`top_n` queries
already preserve both under disambiguated response keys, and the existing AST
lets a caller alias a computed projection when a semantic name matters. The fix
makes `top_n` behave like the ordinary path instead of turning a valid request
into a wrong answer. The obsolete name-only `_ref_output_name` helper was removed.

**Coverage.** A compiler regression test asserts grouped `top_n` orders by the
second derived `id` column, projects both columns, and resolves an outer
`ORDER BY` through the rebuilt alias map. A real SQLite integration test runs
through the REST request pipeline and proves that, for every customer that has an
order, the response contains both the customer id and the maximum order id the
caller ranked by. All three derived-table mappings were mutation-verified
independently: rebinding the rank reference, the outer projection, or the outer
alias map to the first column each fails a distinct assertion for the expected
wrong-answer reason. (The alias-map leg was added on 2026-07-27 during the
completion audit, which found that binding untested — it is only reachable when
the query carries an outer `order_by`.)

### 120. The audit shape records nothing for a nested `IN (subquery)` ✅ DONE

**The defect.** `normalize_query_shape` already recursed into set-operation arms
and CTE bodies, but `_predicate_shape` had no `value_subquery` branch. An event
for `WHERE customer_id IN (SELECT … FROM employees)` therefore named only the
outer table even though the attempted query read another governed scope. This
was an audit-fidelity gap on the Proof pillar, present since item 97.

**What shipped.** A predicate carrying `value_subquery` now records that nested
scope through the same recursive `normalize_query_shape` authority used for the
root query, set-operation arms, and CTE bodies. Because the branch lives in
`_predicate_shape`, it applies everywhere that function is reached — WHERE,
HAVING, join conditions, and a searched CASE written as an *expression*
(`CaseExpr`) — including attempts that policy or schema validation later reject.
The persisted structure includes the nested `from`, joins, select shape, boolean
predicate structure, and any deeper subquery/set-operation scopes.

One position is deliberately **not** covered: a searched CASE written as a
select-item (`CaseSelectItem`) records `branch_count` and its column refs but not
its condition subtree, so `_predicate_shape` is never reached there. That is a
pre-existing gap in `_select_shape`, not one this item introduced, and it is
tracked as item 123 rather than folded in here.

The redaction contract is unchanged: nested predicates and expressions use the
same shape walkers as the root, which record operators and column identifiers but
omit predicate values, CASE results, and other expression literals. Query-shape
normalization runs before validation so rejected attempts remain auditable;
pydantic's own recursion detection rejects a `value_subquery` chain past ~127
levels as a 422 before any walker runs, and the policy depth cap
(`max_subquery_depth`) bounds accepted subqueries.

**Coverage.** Unit tests assert the exact nested table/join/boolean shape, recurse
through a second subquery plus CASE and set-operation positions, and prove none
of their sentinel literals appears. A JSONL test exercises
`StructuredQueryService.execute` through a rejected attempt and inspects the
persisted event itself. A security-marked test pins the same no-leak property for
a policy-rejected subquery. Removing the branch, replacing the recursive walk
with a raw model dump, and breaking the persisted-event path were
mutation-verified to fail for the intended fidelity/redaction reasons.

### 121. Report-only surfaces still assume a query has one scope ✅ DONE

**Shipped.** Enforcement was always scope-correct (items 97 + 104); three
*reporting* surfaces were not, and both containers were affected.

* `ExplainResult.tables` (`execution/service.py`) reported only the outer scope's
  tables while the same response's `sql` named every arm's and every subquery's —
  two fields of one response contradicting each other. It now derives from the
  populated `scope_tables` map, so it describes the statement it returns.
* The candidate policy simulator (`admin/service.py`) built its
  mandatory-filter-readiness set from the scope-local `referenced_tables`, so a
  filter whose table appeared only in an arm or a subquery was invisible: an
  operator could be told a principal was `allow` for a request execution would
  then refuse on a missing claim. It now uses a new
  `referenced_tables_tree_wide`.

`referenced_tables` itself was deliberately **left scope-local**: `_validate_scope`
calls it per scope and must, because a scope's aliases mean nothing outside it.
The tree-wide version is a separate function for the reporting callers, not a
change to the enforcement path.

Neither was a bypass — the simulation was strictly *less* permissive than
enforcement — but a tool whose whole value is predicting enforcement should not be
wrong about it. Surfaced by the item-104 completion audit.

**Coverage.** `tests/integration/test_set_operation_end_to_end.py` asserts explain
reports every arm's tables and a nested subquery's table, in both cases checking
the `tables` field against the `sql` field of the same response.

### 122. `_unique_column_sets` crashed on any FROM element that is not a `Table` ✅ DONE

- Why: it was annotated `sa.Table` and reached straight for `.primary_key.columns`.
  Only a `Table` has that — `Alias`, `Subquery` and `CTE` expose `.primary_key` as a
  bare `ColumnSet` with no `.columns`. Item 118's k-anonymity fan-out check runs on
  the JOINED table, so **`min_group_size` plus any join carrying an `alias` raised
  `AttributeError`** — a 500 where a policy answer belonged. Measured 2026-07-27,
  reproducible with no cte involved; live since item 118 shipped, and never
  exercised because every item-118 test joined an unaliased table.
- Fixed: an alias looks through to its element (same rows, new name, so uniqueness
  is preserved); a cte or subquery has no declared uniqueness and returns empty,
  which makes `_join_can_fan_out` answer "can fan out" — the fail-closed direction
  the floor requires, since a computed stage may hold several rows per join key.
- Surfaced by the item-105 build, which joins onto a `CTE` and hit the same line
  from a new direction. It is the blind spot ROADMAP.md's frontier note names:
  mutation testing probes the rules an item *adds*, not the existing consumers of a
  value whose *type* widened.
- Regression test: `tests/unit/test_cte.py::test_unique_column_sets_handles_every_from_element_shape`
  and `::test_min_group_size_with_an_aliased_join_decides_instead_of_crashing`.

**Effort: S. Priority: high (a crash on a shipped guardrail).**

### 123. A select-item `CASE`'s condition subtree was absent from the audit shape ✅ DONE

`_select_shape`'s `CaseSelectItem` branch recorded `{kind, alias, branch_count,
columns}` and never walked `item.when[*].when` through `_where_shape`. Every other
walker reaches its predicates via `as_expression()` — which `query_ast/models.py`
documents as "the single conversion every walker, cap, and compile path goes
through" — and `_select_shape` was the one that did not. The identical CASE written
as an `ExpressionSelectItem` (`CaseExpr`) recorded its conditions in full, so two
spellings of one query produced materially different audit detail — the exact
asymmetry `_predicate_shape` calls out and fixed for joins in item 103.

**Consequence (fixed).** A nested `value_subquery` sitting in a select-item CASE
condition was invisible to the event: the attempt audited as reading only the outer
table. Policy always rejects such a query (`policy_validation.py` refuses a
subquery in that position), so this was never a policy bypass — but item 120's
whole rationale is that *rejected attempts stay auditable*, and in this one
position they did not. Proof-pillar fidelity, not enforcement.

**Found by** the item-119/120 completion audit on 2026-07-27, which also found
that the redaction test covering this position asserted only the ABSENCE of
sentinel literals — vacuously true, since the subtree was discarded rather than
redacted.

**Fixed** (`src/querygate/audit/events.py`'s `_select_shape`): the `CaseSelectItem`
branch now additively records `"conditions": [_where_shape(branch.when) for branch
in item.when]`, mirroring the `CaseExpr` branch in `_expression_shape`. Additive —
existing consumers of the shape's other keys are unaffected.

**Tests:** `tests/unit/test_audit.py::test_normalized_query_shape_handles_case_select_item`
and `::test_searched_having_and_case_shapes_carry_structure_but_no_literals` now
assert the `conditions` structure directly (previously only asserted literal
absence). A new
`::test_the_audit_shape_records_a_nested_subquery_inside_a_case_select_item_condition`
pins the consequence directly: a `value_subquery` nested in a `CaseSelectItem`
WHEN now shows up in the audit event with its own table/filter shape and no
leaked literal — mirroring item 120's WHERE-clause coverage for this position.
Mutation-verified: reverting the `conditions` line makes all three fail with
`KeyError: 'conditions'`, not a silent pass.

**Effort: S. Priority: low** (fidelity on a position that is always rejected).

### 124. Most of `tests/unit/` is not selected by `pytest -m unit` ✅ DONE

Markers were explicit with no auto-marking by directory, and only 26 of the 78
files in `tests/unit/` carried one. Measured 2026-07-27: `pytest tests/unit`
collected **1703** tests, `pytest -m "unit and not real_db"` collected only
**678** of them — ~60% of the unit directory was invisible to the tier
`CLAUDE.md` names as the minimum bar and to the `git commit` pre-commit hook,
which runs exactly that selection. Found by the item-119/120 completion audit,
when two newly added regression tests passed the file-scoped run but were
silently deselected by the gate.

**Fixed 2026-07-28** with the preferred option from this item's own write-up —
derive the tier from the directory instead of backfilling 52 files. A
`@pytest.hookimpl(tryfirst=True)` `pytest_collection_modifyitems` hook in
`tests/conftest.py` walks every collected item's path relative to `tests/`,
and adds the matching `unit`/`integration`/`security` marker whenever the item
doesn't already carry one. This must run before pytest's own `-m` deselection
— also implemented as a `pytest_collection_modifyitems` hook — or the added
markers arrive too late to affect selection. Mutation-verified, not assumed:
forcing `trylast=True` reproducibly broke `-m unit` back down to 726
collected; removing the decorator entirely (default ordering) still collected
the full 1725, meaning conftest-hook registration already happens to run
before pytest's own mark-deselection hook on this pytest version (9.1.1) —
`tryfirst` pins that ordering explicitly rather than relying on an
undocumented default. Immediately before the fix landed (726, not the 678
measured a day earlier — the unit directory grew by ~20 tests from
intervening commits, most visibly item 122's regression tests), `pytest -m
unit --collect-only` collected 726; after, it collects the full **1725**,
matching the plain `pytest tests/unit --collect-only` count exactly, with
zero live-DB leakage (`pytest -m unit` and `pytest -m "unit and not real_db"`
now collect the identical 1725, because no test under `tests/unit/` carries a
`real_db` marker — verified by grep, not assumed). `tests/integration/` and
`tests/security/` were already fully marked file-by-file (or per-test), so
their counts (489 and 410 respectively) were unchanged by this fix; the hook
makes that coverage structural for both instead of a coincidence of current
file authorship.

The hook itself is pinned by `tests/unit/test_conftest_tier_markers.py`, in
two parts matching its two independent properties. The marker-assignment
logic is pinned by calling the hook directly against fake, filesystem-free
items and asserting the marker-assignment and already-marked-is-left-alone
behavior — both mutations (emptying the tier set; dropping the idempotency
guard) were confirmed to fail the new tests before being reverted. The
`tryfirst` ordering guarantee — untestable by a direct function call, since
it's a property of how pytest calls *multiple* registered hookimpls against
each other, not of the function body — is pinned separately with a `pytester`
end-to-end test: a nested pytest process runs a marker-adding
`pytest_collection_modifyitems` hook against pytest's own `-m` deselection,
parametrized over `tryfirst=True` (the real hook's setting; asserted to win
the ordering race and get the mark selected) and `trylast=True` (asserted to
lose it and get deselected) — the same pair this session verified manually
when the fix landed, now automated instead of one-off. Flipping the two
assertions against each other was confirmed to fail both parametrized cases
before being reverted, so the test genuinely discriminates the two orderings
rather than passing vacuously.

Deliberately additive, not a mass edit: the existing 82 file-level
`pytestmark` lines (64 single-mark `pytestmark = pytest.mark.<tier>`
declarations plus 18 multi-mark `pytestmark = [...]` lists such as
`pytest.mark.integration, pytest.mark.real_db`) and 38 per-test
`@pytest.mark.<tier>` decorators were left in place rather than stripped,
since the hook already skips any item that
carries the marker explicitly — removing them was optional cleanup this item
didn't need for the acceptance criterion (every test under the three tier
directories is now selectable by its tier marker, permanently, with no
future file able to fall through the gap again).

**Effort: S. Priority: medium** (it weakened every gate the repo relies on).

---

### 125. Query engine: a window function as an `Expression` operand ★ ✅ DONE

The last red row of the canonical regression bar (`docs/ENGINE_EXPRESSIVENESS_PLAN.md`
§5 row 15) and the only thing between the engine and **16/16**. Both halves have
existed since item 101 — arithmetic from 100, `OVER` from 101 — but a window is a
select-item **projection**, never an `Expression` operand, so
`amount / SUM(amount) OVER ()` (each row against an aggregate over its partition,
in one statement) is two projected columns plus client-side division.

**Recorded as a wall, not a gap**, by item 101 and left for a maintainer decision
because the obvious fix has a real cost: adding a union member that is legal in
some positions and illegal in others breaks the "legal everywhere a scalar is
expected" property that keeps the item-100 substrate reviewable in one place.
**Maintainer approved it on 2026-07-27** — see the Decision Log entry in
`docs/PRODUCT_GUIDE.md` for the shape and why the alternative was rejected.

**What shipped.** A `WindowExpr` — `WindowSelectItem` minus `alias`, both now sharing
a `WindowCall` base so their arity/frame rules cannot drift — as a member of the
closed `Expression` union, with the positional rule enforced in ONE place
(`_reject_windows_outside_projections`) over a new position-aware walk
(`iter_scope_expressions_by_position`):

- a window may appear **only inside a select-item (projection) expression tree**;
- never inside another window's `arg` (SQL forbids nested windows);
- never inside an aggregate's argument;
- the existing `group_by`/aggregate incompatibility carries over unchanged.

Rejected alternative: a parallel projection-only expression union. It makes misuse
structurally impossible rather than merely forbidden — the posture item 105 chose
for recursive CTEs — but only by duplicating the whole recursive tree across every
walker, cap, compiler path and audit shape. That duplication is a worse
maintainability trade than the wall it removes, and the fail-closed single rule
gets the same safety with one reviewable site.

**Coverage.** `tests/security/test_window_operand_boundary.py` (9 cases) covers every
position SQL forbids a window in — WHERE, HAVING, a join condition, an aggregate
argument, nested in another window, combined with `group_by`, buried inside a CASE
inside arithmetic, and inside a nested subquery scope — plus the motivating query,
so the rejections cannot pass vacuously. `tests/integration/test_window_operand_end_to_end.py`
EXECUTES row 15 through the REST pipeline and asserts the computed answer, both
unpartitioned and partitioned; it uses subtraction rather than the plan's "share of
total" division because `NUMERIC(10,2)` rounds a quotient to 2 decimals, which would
have made the assertion a test of rounding rather than of the window.

All 4 enforcement points were mutation-verified independently: removing the rule
fails 8 cases; replacing the projection ALLOWLIST with a denylist of illegal
positions (the fail-open spelling) fails exactly the aggregate-argument case;
removing the nested-window check fails only that case; removing the `group_by`
carry-over fails only that one.

**Real backends.** Row 15 also runs in the cross-dialect differential suite against
live Postgres and live MSSQL, asserting both return identical rows
(`test_regression_bar_row_15_window_as_an_expression_operand_matches`), so it carries
the same grade of evidence as bar rows 3/6/8/12/16. That leg is load-bearing rather
than ceremonial: the operand position puts the window inside item 100's guarded
division (`NULLIF` + `CAST(... AS NUMERIC)`), and T-SQL's integer-division and
NUMERIC scale rules are not Postgres's — exactly the composition a rendering
assertion cannot validate. The case also asserts the result set is non-empty and not
all-NULL, since two empty or two all-NULL columns would compare equal while proving
the window computed nothing.

**Effort: XL. Priority: high** (closes the ★ flagship pillar's success criterion).
Depends on: items 100, 101.

### 127. Reject an MCP request whose routing headers disagree with its body (gateway confused-deputy) ✅ DONE

**Surfaced 2026-07-30 by `competitive-scan`.** The MCP `2026-07-28`
specification (final — see item 128) mirrors `method` and `params.name` into
required `Mcp-Method` / `Mcp-Name` HTTP headers so that intermediaries
"(load balancers, gateways, observability tooling) can route and inspect
requests without parsing the body." It therefore also mandates the matching
server-side defense:

> Servers that process the request body **MUST** reject requests where the
> values specified in the headers do not match the corresponding values in the
> request body. This prevents potential security vulnerabilities when different
> components in the network rely on different sources of truth (e.g., a load
> balancer routing on the header value while the MCP server executes based on
> the body value).
> — [Streamable HTTP § Server Validation](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)

**Why it matters more for QueryGate than for a typical MCP server.** The P4
leverage move (`docs/business/MARKET_DOMINATION_ANALYSIS.md` §7) is to sit
*behind* MCP gateways and proxies as the enforcement point they can't be. That
is exactly the deployment where this mismatch is a real confused-deputy: a
fronting gateway authorizes `Mcp-Name: list_tables` for a low-privilege
identity, while the body it forwards calls `run_structured_writes`. The
gateway's tool-level authorization is then silently void, and QueryGate — the
component that *did* see the body — executed the privileged operation anyway.

**Current state at build time (verified 2026-07-30, prospective not live):**
QueryGate speaks protocol `2025-11-25` (item 128), which does not define these
headers, so there was no live vulnerability — a conforming gateway would not
yet rely on them. The exposure begins the moment either side moves: a gateway
that trusts the headers, or QueryGate's own upgrade under item 128.

**What shipped.** `mcp/transport_guard.py`'s `MCPRequestGuardMiddleware` — the
same ASGI wrapper that pre-scans raw body bytes for item 86's size/depth
guards, already the one place that sees headers and body together before the
transport parses either — gained a header/body agreement check
(`_header_body_mismatch`), run strictly *after* item 86's depth scan so a
hostile deep body can't reach this check's own `json.loads` first. When
present, `Mcp-Method` must equal the body's `method` and `Mcp-Name` must equal
`params.name` / `params.uri`; a mismatch is rejected with HTTP `400` and
JSON-RPC error code `-32020` (`HeaderMismatch`), reusing the existing
`{"error": {"code", "message"}}` reject envelope rather than a second one.
Validate-if-present, not require — shippable now, independent of item 128,
and it fails closed the instant a gateway starts sending the headers.

The spec's Base64 "sentinel" header-value encoding (`=?base64?...?=`, used
when a name isn't safely representable as a plain ASCII header value) is
decoded before comparison (`_decode_sentinel_value`); a header wearing the
sentinel's markers that doesn't actually decode as base64/UTF-8 is rejected as
malformed rather than compared as literal text — fail-closed, not a silent
fallback to raw-string comparison. `Mcp-Name` is deliberately **not** checked
against the set of registered tools here: this guard runs outside
`MCPAuthMiddleware`, so that check would turn it into an unauthenticated
tool-enumeration oracle — agreement with the body is the whole job.

**Four defects found and fixed by the post-build `auditors` pass** (all four
reviewers — security-invariant, architecture-boundary, test-contract, and
claim — were run in parallel; each is recorded here with its finding ID):

1. **Unhandled parser exception on a small, well-formed-looking body
   (security-invariant QG-127-1).** `json.loads` can raise a bare `ValueError`
   (CPython's integer-string-conversion guard trips on an ~5,000-digit numeric
   literal, well under both the byte and depth caps) that the original
   `except (json.JSONDecodeError, UnicodeDecodeError)` didn't catch, so it
   propagated to an unhandled 500 pre-auth — exactly the "malformed input is a
   client error, never a 5xx" regression item 86 exists to prevent. Widened to
   `except (ValueError, UnicodeDecodeError, RecursionError)` (`JSONDecodeError`
   is a `ValueError` subclass, so this only widens, never narrows).
2. **First-match on a repeated routing header (security-invariant QG-127-2,
   independently found by architecture-boundary Finding 1).** `Mcp-Method`/
   `Mcp-Name` sent twice with disagreeing values has no single source of truth
   for an intermediary to agree with QueryGate about; taking the first
   occurrence let a caller satisfy this guard with one value while a
   differently-configured gateway authorizes on a different occurrence of the
   same header name — reproducing the exact confused-deputy shape this item
   closes. A repeated routing header is now rejected outright.
3. **`params.name`-only field selection (architecture-boundary Finding 3,
   independently found by test-contract F2).** The original
   `params.get("name", params.get("uri"))` always preferred `name`, so a
   `resources/read`-shaped body (which mirrors `params.uri`, not `params.name`,
   per the spec's own table) was never actually checked against the field the
   header is supposed to agree with — and a body carrying *both* fields was
   silently resolved by whichever key happened to be checked first. Selection
   is now driven by `method` (`resources/*` prefers `uri`), and a body carrying
   both `name` and `uri` is rejected as ambiguous rather than guessed at.
4. **Ordering test asserted only "some non-null code" (test-contract F1).**
   `test_mcp_deeply_nested_body_with_routing_header_hits_depth_guard_not_header_check`
   would have stayed green even if a future change made the header check run
   first and land on the same observable HTTP status by some other path.
   Tightened to assert the depth guard's specific `MALFORMED_REQUEST` code.

Claim-reviewer also caught this write-up's own test count drifting ("eight"
vs. the seven tests that existed at that point) and flagged that the adjacent
`docs/business/` strategy docs (`MARKET_DOMINATION_ANALYSIS.md`,
`COMPETITOR_MCP_GATEWAYS.md`, `NORTH_STAR.md`) still described item 127 as
unshipped — both corrected.

**Coverage.** 13 test functions (14 test items — one is parametrized ×2) in
`tests/security/test_malformed_input_fuzzing.py` (already the item-86 MCP
transport-guard suite): the literal confused-deputy scenario on both
`Mcp-Name` and `Mcp-Method`, matching headers passing through unaffected,
sentinel-encoded names decoding correctly, a malformed sentinel failing
closed, an unparseable body under a present header failing closed, a
non-object JSON body (e.g. a bare array) failing closed, a deeply-nested body
with a routing header present still hitting the depth guard specifically (not
the header check, and not a `RecursionError`), the integer-conversion
`ValueError` failing closed rather than 500ing, a repeated routing header
being rejected (both header names, parametrized), a `resources/read`-shaped
body matching correctly against `params.uri`, the mirror mismatch case, and a
body carrying both `params.name` and `params.uri` being rejected as
ambiguous. Every enforcement point — the original set plus all four
post-audit fixes — was mutation-verified: flipping the method/name equality
checks, letting a JSON-parse failure return "no mismatch", letting a
malformed sentinel fall back to raw-string comparison, reordering the header
check ahead of the depth scan (reproduces the exact `RecursionError` item 86
exists to prevent), narrowing the exception clause back down, dropping the
duplicate-header check, dropping the both-fields-ambiguous check, and
reverting the `uri`-preference logic each made a distinct, targeted test fail
for the expected reason. One mutation survived a first, weaker version of the
malformed-sentinel test — the header and the body's `params.name` must be set
to the *same* still-encoded sentinel text for the test to distinguish
"decoded and rejected" from "coincidentally never equal as raw strings" —
corrected before landing.

**Effort:** S–M. **Depends on:** 86 (shipped). **Does not depend on 128** —
deliberately.

**Known residual, recorded rather than fixed (security-invariant QG-127-3):**
the pre-auth `json.loads` this item adds is bounded only by the existing
`mcp_max_request_bytes` (default 4 MiB), not a smaller dedicated cap — an
unauthenticated caller can force a full parse of up to that size per request
by adding one small header, at roughly 4x the CPU and 10x the memory of the
byte/depth scan alone (measured: ~0.16s CPU / ~36MB peak heap for a 3.5MB
body). A dedicated smaller cap would reduce this, but rejecting an
over-threshold body under a present header would also reject a legitimate
large batch from a real, conformant 2026-07-28 gateway once item 128 lands —
a product tradeoff between pre-auth cost and future-client compatibility, not
a small/safe fix, and left for a maintainer decision alongside item 128.

### 128. Conform to the final MCP `2026-07-28` protocol revision ✅ DONE

**Surfaced 2026-07-30 by `competitive-scan`** (see item 127's write-up for the
full spec background and why the gap was a distribution risk, not hygiene).
QueryGate was pinned to `mcp = ">=1.28.1"` (`LATEST_PROTOCOL_VERSION`
`2025-11-25`), a full revision behind final. Shipped 2026-08-06 as a full
`mcp` SDK v1 → v2 migration (`pyproject.toml`: `mcp = ">=2.0.0"`), not a
scoped-down subset — the item's own sequencing note flagged this as gated on
SDK availability, and by build time the v2 SDK was GA.

**What shipped, by surface:**

- **Transport/server.** `mcp/server.py`: `FastMCP` → `MCPServer` (no longer
  takes transport settings in its constructor — those move to
  `streamable_http_app()`); the v1 low-level `_install_scoped_tool_listing`
  decorator registration is gone, replaced by overriding the plain
  `async def list_tools()` instance method v2 exposes directly (both
  `MCPServer.list_tools()` and the dispatcher's `_handle_list_tools` read the
  same bound attribute, so a direct override is the v2-idiomatic
  replacement, not a workaround).
- **The MRTR port of items 92/93 — the substantive work.** The protocol core
  going stateless removes server-initiated mid-call requests entirely;
  `Context.elicit` (what `mcp/elicitation.py` used to gate an expensive/
  sensitive read or write on human approval) no longer exists as a
  send-and-suspend call. Ported at the MCP tool layer, per the item's own
  design constraint that `execution/service.py` must never learn about MRTR
  (it is the transport-agnostic single pipeline shared with REST):
  `mcp/elicitation.py` was rewritten around two functions —
  `build_pending_input_required()` (catches `ApprovalRequiredError` per
  batch item and shapes an `InputRequiredResult` with one `inputRequest` per
  gated item, reusing `execution/approval.py`'s existing HMAC-signed,
  fingerprint-bound token verbatim as the opaque `requestState` — no new
  token format) and `resolve_approval_tokens_from_retry()` (reads
  `ctx.input_responses`/`ctx.request_state` on the retried call and mints
  the final granted token per item). `mcp/tools/query.py` and
  `mcp/tools/write.py` were rewritten to call these and return
  `InputRequiredResult` instead of a synchronous elicitation `await`.
  `execution/service.py`'s `BatchQueryItemResult` and
  `execution/write_execution.py`'s `WriteBatchItemResult` each gained
  `approval_fingerprint`/`approval_reasons` fields (populated by
  `_batch_error_item`/`_batch_error`) so the tool layer can shape a
  per-item `inputRequest` without the pipeline itself knowing what MRTR is;
  `execute_many` gained an `approval_tokens` map parameter matching the
  write path's existing per-batch-item shape.
- **The anti-replay property, mutation-verified.** Signing `requestState`
  satisfies the spec's "treat it as attacker-controlled" requirement but
  does not by itself stop a caller from obtaining approval for query A and
  replaying that `requestState` against a resubmitted query B — MRTR's
  retry carries its own `queries` argument, unlike the old in-process
  `Context.elicit` where the token was minted from the server's own
  validated AST and never left the process. The fix: the retry path
  re-derives each item's fingerprint from the *resubmitted* AST and compares
  it against the fingerprint embedded in the pending token **before**
  consulting the human's actual approve/decline response.
  `test_resolve_rejects_a_fingerprint_swapped_at_retry`
  (`tests/unit/test_mcp_elicitation_approval.py`) mutation-verified this —
  disabling the comparison made this test (and no other) fail for the
  expected reason.
- **Routing headers (item 127) and the OAuth hardening items (RFC 9207
  issuer validation, DCR → CIMD migration, etc.)** were confirmed, not
  reworked: item 127 shipped independently of this item by design, and the
  OAuth hardening items are client-side/authorization-server-side
  obligations outside QueryGate's resource-server-only MCP surface
  (`mcp/oauth_metadata.py`, `mcp/auth.py`) — listed in the original item
  write-up for completeness, deliberately not built here.
- **Mechanical v2 API fixes surfaced by the migration**, not incidental:
  `tool.inputSchema` → `tool.input_schema` (the Python SDK object attribute
  rename; raw JSON-RPC wire dicts stay camelCase via aliases and were left
  alone — `examples/openai_function_calling_integration.py` and its
  docstring), `streamablehttp_client` → `streamable_http_client` and its
  2-tuple (was 3-tuple) return shape
  (`tests/integration/test_integration_examples.py`), and the MCP token
  budget's `_MAX_TOTAL_CHARS` bumped 123,000 → 124,000 to absorb the v2
  tool-schema size delta (`tests/unit/test_mcp_token_budget.py`, with an
  inline comment recording why).

**Coverage.** `tests/unit/test_mcp_elicitation_approval.py` was rewritten in
full (19 tests: pending-result shaping including the multi-item-batch case,
resolve-on-approval/decline/not-yet-approved, the fingerprint-swap replay
rejection, fail-closed on a malformed/forged/expired `requestState`,
per-item grant attribution for a batch, and the tool-layer round trip from
first gated call through a resolved retry); `tests/unit/test_mcp_write_tool.py`
gained the same-shaped coverage for the write path (`w{i}` batch keys,
atomic-mode exclusion from the approval channel). `tests/integration/
test_integration_examples.py` and `docker`-free MCP transport tests exercise
the real v2 `MCPServer` end to end (`streamable_http_app`, `tools/list`,
`tools/call`) rather than only unit-level doubles.

**Docs.** `CLAUDE.md`'s testing-gotcha note and file list (3 → 6 files:
`connections, schema, query, write, help, templates`) updated for the v2
forward-ref resolution mechanics; `docs/PRODUCT_GUIDE.md`'s architecture
section, item-127/128 narrative, tools list, and Decision Log updated;
`docs/business/NORTH_STAR.md`'s "MCP protocol currency" gap marked CLOSED
2026-08-06.

**Effort:** L (as scoped). **Depends on:** 90, 92, 93 (all shipped), and the
`mcp` v2 SDK reaching GA — confirmed at build time.
### 129. Never advertise a principal-varying MCP result as shared-cacheable ✅ DONE

**Surfaced 2026-07-30 by `competitive-scan`.** The `2026-07-28` revision adds
caching metadata (SEP-2549) to `tools/list`, `prompts/list`, `resources/list`,
and `resources/read`: a `ttlMs` freshness hint and a `cacheScope` of `"public"`
or `"private"`, modelled on HTTP `Cache-Control`, where `public` permits
**shared intermediaries** to cache and reuse the response across callers.

**Aim this at `tools/list`, not at tool results.** The caching metadata attaches
to `tools/list` / `prompts/list` / `resources/list` / `resources/read` — *not*
to `tools/call` results, so `list_connections`'s per-caller output is not the
exposed surface (an easy mis-aim: it is a tool whose *result* varies, which the
spec does not make cacheable). The genuinely principal-varying **list** surface
is `tools/list`, filtered by `_install_scoped_tool_listing` in `mcp/server.py`
via `_SCOPE_GATED_TOOLS`. Note QueryGate currently registers **zero** resources
and **zero** prompts, so a test written only against those is close to vacuous —
the test must therefore also fail if a resource or prompt is ever registered
without an explicit `cacheScope`.

**Why this is a security rule for QueryGate specifically.** Our MCP surface is
per-principal by construction, and the spec explicitly blesses this ("the set
**MAY** vary by the authorization presented on the request"). But a
principal-varying result marked `cacheScope: "public"` and cached by a shared
gateway — the very intermediary the P4 play courts — would serve one
principal's visible tool surface to another, eroding the deny-by-default
posture without a single line of policy code being wrong.

**Honest severity:** this is defense-in-depth, not an authorization bypass.
`mcp/server.py` already records that scoped tool listing is
"token-savings/defense-in-depth only" and that the real boundary is each tool's
call-time scope check. Keep that framing — do not let this item's write-up imply
the tool list is a security boundary.

The failure mode is a *default*, not a decision: whichever value the SDK or a
future refactor emits when nobody thought about it. So encode it as an
invariant with a test, in the manner of `tests/unit/test_credential_redaction.py`
(which asserts the no-credential invariant against the live schemas rather
than trusting convention): **every MCP result whose content depends on the
caller must carry `cacheScope: "private"`**, asserted against the actual
emitted payloads, so adding a new per-principal tool cannot silently regress
it.

**Current state at build time:** the installed `mcp` SDK (1.28.1) reports
`LATEST_PROTOCOL_VERSION = "2025-11-25"` and defines no `cacheScope`/`ttlMs`
fields anywhere (grepped the installed package source directly, not assumed —
item 128, the full `2026-07-28` protocol/SDK migration, has not landed on this
branch). This item does not wait on that migration: SEP-2549's caching
metadata is additive to the four existing result shapes, and every one of
them (`ListToolsResult`/`ListPromptsResult`/`ListResourcesResult`/
`ReadResourceResult`, and their common `Result` base) already declares
`model_config = ConfigDict(extra="allow")`, so a caller-invisible `cacheScope`
field can be forced onto the emitted payload today without depending on the
SDK's own (not-yet-released) support for the field.

**What shipped.** `mcp/caching.py`'s `install_private_cache_scope` installs a
self-enforcing `_PrivateCacheScopeHandlers` dict as `mcp_server`'s low-level
`Server.request_handlers`: overriding `__setitem__` means ANY assignment to
one of the four SEP-2549 keys (`ListToolsRequest`/`ListPromptsRequest`/
`ListResourcesRequest`/`ReadResourceRequest`) gets wrapped to force
`.cacheScope = "private"` onto the returned `ServerResult.root` at write
time — a plain pydantic attribute assignment (every relevant `mcp.types`
result class declares `model_config = ConfigDict(extra="allow")`) that
survives the SDK's own `model_dump(by_alias=True, mode="json",
exclude_none=True)` serialization path (`shared/session.py`), verified
end-to-end against a live MCP integration test, not assumed. Deliberately
*not* `CallToolRequest`/`GetPromptRequest` (a mis-aim the item's own
write-up above warns about: a tool's or prompt's per-caller *call-time
result* is not a listed surface) and, with a recorded reasoned exclusion
rather than a guess, not `ListResourceTemplatesRequest` (no spec artifact in
this repo names it as a SEP-2549 surface, and the installed SDK predates
SEP-2549 entirely). `mcp/server.py`'s `setup_mcp` calls the companion
`assert_private_cache_scope_installed` immediately before
`streamable_http_app()`, raising loudly rather than silently serving an
unprotected app if a future refactor ever breaks the wiring.

`tests/unit/test_mcp_cache_scope.py` (14 tests) asserts this against real
emitted payloads, not source-code grepping, in the manner of
`test_credential_redaction.py`: the real `create_mcp_server()` instance's
`tools/list`/`prompts/list`/`resources/list`; a from-scratch `FastMCP`
server with a real resource and prompt registered *after*
`install_private_cache_scope` runs, proving the future-registration case
isn't vacuous; a negative control on an unprotected `FastMCP` instance;
`tools/call` and `prompts/get` both confirmed to carry no `cacheScope` at
all (guarding the "aim at lists, not results" distinction on both excluded
surfaces, not just one); installing cache-scope enforcement *before*
`_install_scoped_tool_listing` and re-running the latter twice afterward
(simulating a hypothetical future config-reload path), proving the
guarantee is order-independent rather than resting on a call-order
convention; ten repeated `install_private_cache_scope` calls and a direct
handler re-assignment both proven not to nest wrapper closures (an identity
check, not just a behavioral one); a stub server object missing a handler
proven to raise rather than silently install a partial guarantee; and both
of `assert_private_cache_scope_installed`'s raise/pass paths, including
against the real singleton.

**Four defects found and fixed by the post-build `auditors` pass**
(`architecture-boundary-reviewer`, `security-invariant-reviewer`, and
`test-contract-reviewer` run in parallel; two independently converged on the
same root cause from different angles):

1. **Installation-order dependence (architecture-boundary F1, independently
   found by security-invariant M129-1).** The first version wrapped
   whichever handler happened to be registered *right now*, which only
   works if `install_private_cache_scope` always runs after every other
   handler installer (`_install_scoped_tool_listing` included) — a
   convention enforced only by a comment, invisible to every test since
   they all went through the one correctly-ordered `create_mcp_server()`.
   Fixed by making the guarantee structural instead of order-dependent: the
   `_PrivateCacheScopeHandlers.__setitem__` override above, plus
   `assert_private_cache_scope_installed` as a fail-loud backstop at serve
   time.
2. **Unbounded wrapper-closure growth on repeated installation
   (architecture-boundary F2, security-invariant M129-2, and independently
   test-contract F2 — all three reviewers found this).** Calling
   `create_mcp_server()` more than once (several existing unit tests do,
   against the shared module-level `mcp_server` singleton) re-wrapped
   `prompts/list`/`resources/list`/`resources/read`'s already-wrapped
   handler on every call, growing one closure layer per call —
   functionally invisible today only because the sole side effect (setting
   `cacheScope` to the same value repeatedly) is itself idempotent. Fixed
   with a marker attribute (`_qg_forced_private_cache_scope`) checked
   before wrapping.
3. **Silent skip on a missing handler (security-invariant M129-1b).** The
   original loop did `if inner is None: continue`; replaced with a raise,
   since a real `FastMCP` instance always has all four handlers and staying
   silent would hide a real bug behind an apparently-successful install.
4. **Missing negative test for the second exclusion (test-contract F1).**
   The module docstring named both `CallToolRequest` and `GetPromptRequest`
   as deliberately excluded, but only the former had a regression test;
   added `test_get_prompt_result_is_not_annotated_with_cache_scope` as a
   sibling to the existing `tools/call` check.

Two purely-informational items from the same pass were recorded rather than
requiring code changes: `ListResourceTemplatesRequest`'s exclusion is now a
reasoned docstring note (security-invariant M129-3) rather than a silent
omission, and the protocol-version interop residual — the field is emitted
while the server still negotiates `2025-11-25`, working only because of
`extra="allow"` — is now one sentence in `caching.py`'s module docstring
(security-invariant M129-4) in addition to the "Honest scope note" below.
Test-contract's F3 (the future-registration test proves the helper works in
isolation, not that a real future registration through
`discover_and_register_tools()` would hit the same path) was accepted as a
low-risk residual per the reviewer's own recommendation, not fixed — tightening
it would require a throwaway registration on the real singleton for a case
that has zero present impact (QueryGate registers zero resources/prompts
today).

Every one of the four accepted fixes was mutation-verified individually
(not just re-running the full suite): reverting the `__setitem__`
auto-wrap to the original one-shot wrap made the reordering test fail for
the expected reason; removing the idempotency marker check made a
direct-reassignment test fail (the outer `install_private_cache_scope`
call-count guard alone did **not** catch this — that gap was itself found
during mutation testing and closed with a dedicated test targeting the
marker independent of the call-count guard); reverting the raise to a
silent skip made its own test fail; and neutering
`assert_private_cache_scope_installed` made its own tests fail. Full
`poetry run pytest` (2,722 tests, unit + integration, including a live-server
MCP integration test that exercises the real `setup_mcp()` path
end-to-end) passed clean on the final tree.

**Honest scope note — merge-time rework, not a clean drop-in.** Implemented
on a worktree branched before item 128's `mcp` SDK v1 → v2 migration landed
on `roadmap/auto-session-2026-08-05`; the original implementation targeted
`mcp.server.fastmcp.FastMCP` and relied on every relevant `mcp_types` result
class declaring `model_config = ConfigDict(extra="allow")` to force an
undeclared `cacheScope` field onto the response. Both assumptions were false
on the real v2 SDK this branch actually runs: `FastMCP` doesn't exist in v2
(renamed `MCPServer`, importable from `mcp.server.mcpserver`); the low-level
`Server.request_handlers` dict keyed by request-*type* became
`_request_handlers` keyed by request *method string*, storing a frozen
`HandlerEntry(params_type, handler)` instead of a bare callable; handlers are
invoked `(ctx, params) -> result`, not `(req) -> ServerResult` with a `.root`
wrapper; and v2's result models no longer declare `extra="allow"` at all —
assigning an undeclared attribute raises `ValueError`, so the original
field-forcing mechanism would not have worked even once mounted.

Separately, v2 turned out to already define native `ttl_ms`/`cache_scope`
fields on a `CacheableResult` base class that `ListToolsResult`/
`ListPromptsResult`/`ListResourcesResult`/`ReadResourceResult` all inherit,
defaulting `cache_scope="private"` — so on this branch the item's invariant
already holds by SDK default before any QueryGate code runs. The merge kept
the module anyway rather than deleting it as redundant, per CLAUDE.md's "the
failure mode is a default, not a decision" doctrine: `mcp/caching.py` was
rewritten against the real v2 shapes (`_lowlevel_server._request_handlers`,
method-string keys, `HandlerEntry`, `.cache_scope` not `.cacheScope`) so
QueryGate's own guarantee is structural and independent of the SDK's
default, not merely coincident with it. `tests/unit/test_mcp_cache_scope.py`
was rewritten to match, and gained one test
(`test_with_forced_private_cache_scope_overrides_a_handler_that_sets_public`)
that the original suite lacked even conceptually: every other test exercises
a handler that never explicitly sets `cache_scope`, so the SDK default alone
would pass them with the forcing line deleted entirely — confirmed by
mutation (replacing `result.cache_scope = PRIVATE_CACHE_SCOPE` with a no-op
left the other 14 tests green). The new test builds a fake inner handler
that explicitly returns a `"public"`-scoped result and asserts the wrapper
still forces it back — the one case that actually distinguishes QueryGate's
enforcement from the upstream default, and the one the mutation confirmed
was previously untested. `poetry run pytest -m unit` (2,094 tests) and
`black --check` both clean on the merged tree.

**Effort:** S. **Depends on:** 128 (shipped first on this branch; this item's
*intent* — own the invariant structurally rather than trust a default — is
SDK-version-agnostic, but the specific implementation required 128's v2 API
shape once merged, as the note above documents honestly rather than
asserting a clean independence that didn't hold in practice).

### 132. Reconcile stale shipped-status claims left behind by items 90–93 ✅ DONE

**Surfaced 2026-07-30 by the `auditors` claim review of the `competitive-scan`
pass; pre-existing drift, not caused by that pass.** Items 90, 91, 92, and 93
all shipped, but several surfaces still described them as open or partial.

**What shipped — every claim verified against the code before editing:**

- `docs/business/GO_TO_MARKET.md`'s "Safe to claim now" list gained four
  bullets for items 90 (delegated agent identity, dual-identity audit, MCP
  OAuth resource-server conformance RFC 9728/8707/6750), 91 (tamper-evident
  hash-chained audit ledger + per-query receipts, `AUDIT_SINK_BACKEND=jsonl_chained`),
  92 (in-query approval, both triggers), and 93 (governed writes — deny-by-default,
  preview/diff, gated/audited execution; reversibility/undo explicitly named
  as removed so it's never mis-claimed). The existing "do not claim yet →
  compliance-grade/WORM audit retention" line was left as-is (still correct —
  item 91 is chain-integrity detection, not WORM retention).
- The same file's HA/DR quota bullet and "secrets-manager rotation" bullet
  were corrected from unconditional/understated framing to the accurate
  residual: quota sharing is real only when `RedisQuotaLimiter` is configured
  (opt-in); credential rotation *without a process restart* already works
  (`POST /api/v1/admin/reload-config`, in-flight-safe engine disposal — item
  13) and the real gap is the refresh being operator-pull only, not automatic
  (item 135, still open).
- `README.md`'s "In-query human-in-the-loop approval (phase 1)" heading
  dropped its stale phase suffix (the body already documented both shipped
  triggers). Its `/access/` section's stale "personal-denial history not
  included in this first pass" paragraph was rewritten to describe the
  shipped `GET /api/v1/help/my-recent-denials` endpoint (authentication-only,
  no admin scope, `principal_id`-filtered, configurable lookback).
- `TODO.md`'s own quick-scan rows for items 45 and 93 were corrected (item 45's
  "personal denial history not started" parenthetical; item 93's
  "`release-smoke` write round-trip open" → shipped, verified directly against
  `scripts/release_smoke.sh`'s real insert→verify→delete→verify round-trip
  against real Postgres in the built image).
- Item 93's own body: a "Phase 2b–3 (not started)" planning paragraph —
  everything it listed had since shipped or been deliberately removed — was
  replaced with a superseded/history note; the numbered design-plan's
  `execution/compensation.py` entry (a file deleted 2026-07-23 with the
  write-undo feature) was marked removed so it stops sending an implementer
  hunting for nonexistent code; a now-meaningless "upsert-undo" deferral
  (undo itself was removed entirely, so there's no mechanism left for upserts
  to be a special case of) was dropped, leaving `approval-binds-to-diff-hash`
  as the sole remaining reasoned deferral.
- A benchmark-figure sweep (the original `14/14`→`16/16` fix already landed
  2026-07-30) found no further stale hard-coded figures outside
  `TECHNICAL_REVIEW.md`, which is a dated point-in-time snapshot correctly
  left unedited — it reported what was true on 2026-07-23, not a living claim.

**Post-build `claim-reviewer` audit (2026-08-01)** found three further stale
spots surfaced by, but not part of, this same reconciliation pass — all fixed
in the same commit: README's approval section still claimed MCP elicitation
approval as "the one remaining piece" three lines below a heading this item
had just marked complete, when `mcp/elicitation.py` had shipped it weeks
earlier; GO_TO_MARKET's "do not claim yet" list still said "signed
release/SBOM distribution" in blanket form when `sales/index.html` already
carried the precise, reconciled framing (mechanism built and CI-tested,
no release cut through it yet) — reused verbatim; and item 56's own HA/DR body
carried the identical stale quota phrase GO_TO_MARKET's copy had just been
corrected away from.

**Effort:** S. **Depends on:** none.

### 133. The verdict endpoint — expose the decision without the execution (play P4) ✅ DONE

**Surfaced 2026-07-30 by `competitive-scan`.** `MARKET_DOMINATION_ANALYSIS.md`
§7 names P4 as one of the two leverage moves, `NORTH_STAR.md` lists it under
"the two leverage moves", `COMPETITORS.md` tells us to build it, and
`COMPETITOR_MCP_GATEWAYS.md`'s Decision leads with it.

**Correction (2026-07-30, `auditors`): a `StructuredQuery` allow/deny verdict
already ships — twice** (item 39's admin config-simulation path and item 31's
"test as principal" path), both gated on admin scope and answering about a
*target* principal. What was genuinely unscoped was a **caller-facing,
non-admin, quota-metered** verdict about the **calling** principal — "may
**this** caller run **this** query, right now", inverted from the existing
admin surfaces.

**Shipped.** `POST /{connection}/query/verdict` (REST) and MCP
`run_structured_queries(mode="verdict")` add
`StructuredQueryService.verdict()`/`.verdict_many()`, reusing the same
`_validate_and_compile` seam `execute`/`explain` already share (non-negotiable
#4 — one path, not a second evaluator). A denial always reports the fixed
`reason="not-available-to-you"`; the plan (compiled SQL + touched tables) is
omitted by default (`Policy.verdict_include_plan`); the endpoint is
quota-metered and audited unconditionally, including a quota/concurrency
rejection.

**Anti-oracle collapse — hardened across two audit rounds, not shipped
type-by-type.** The first cut's inner except clause caught only
`(PolicyViolationError, QueryValidationError)`. A first `auditors` pass found
two more distinguishable outcomes escaping it: `NotFoundError` (a join's own
`connection` field naming a connection the caller can't see) and
`sa.exc.NoSuchTableError` (a wholly non-existent table, which reflection
raises directly — this one escaped as an uncaught 500, a fourth
distinguishable outcome alongside allowed/policy-denied/schema-denied). A
**second** `auditors` pass on that same fix then found the four-type
allow-list was itself still reachable-but-missed (a plain `ValueError` from
an unresolved `${ENV_VAR}` connection-string secret was a concrete,
non-hypothetical fifth gap). The except clause is now a deliberate
`except Exception` catch-all, not an allow-list — safe because
`_get_policy`/`enforce_query_quota`/`concurrency_slot` all run strictly
before this inner block, so nothing reaching it is a quota/concurrency system
failure, only a genuine query-shape rejection.

**The combined-surface leak.** `help/personal_denials.py`'s
`/help/my-recent-denials` persisted the real `policy`/`schema`
`error_category` for every rejected event, including `query_verdict` ones —
so the same caller who received a collapsed `verdict()` denial could
immediately read the real category back for their own just-submitted probe.
`_is_own_denial` now excludes `operation="query_verdict"` events entirely
(both from the visible list and the separate `own_denials_found` count);
every other operation's category is still surfaced there unchanged.

**Audit-trail and admission-control gaps found by the architecture/security
re-audits.** The first cut called `enforce_query_quota` outside any
try/except leading to `audit_query`, so a quota-throttled probe left zero
audit trace — fixed by wrapping quota reservation, connection resolution,
and the concurrency admission in an outer handler that audits then
re-raises, mirroring `execute`'s outer handler (its `NotFoundError` case is
also classified `"not_found"`, matching `execute`, instead of falling
through to the generic `"db_error"` label). `verdict` also now passes
`principal_subject`/`max_queue_depth`/`max_queue_depth_per_principal` to
`concurrency_slot`, the same caps `execute` already passes — the identical
gap was then found still open in `explain()` (pre-existing, not introduced
by this item, but fixed in the same pass for consistency once `verdict` no
longer had it). The plan-compile-and-success-audit block was moved inside
the outer `try` so a `_compile_to_text` failure is still audited and
reported as a real error, rather than 500ing silently with the quota unit
already spent and no trace left behind.

**Deliberate scope limits, recorded rather than silently assumed:**
- `explain` is left untouched (still echoes the real validation message) —
  tightening it was named as this item's own open question and rejected as
  unrelated scope creep. This also means `verdict` is response-shape
  hygiene, not a privilege boundary: the same principal that can call
  `verdict` can call `explain`/`query` and get the real message, since
  QueryGate has no scope today separating "may see a verdict" from "may see
  debug detail" — recorded as a residual in `docs/THREAT_MODEL.md` QG-34
  rather than left as an implied, uncovered guarantee.
- `verdict` answers policy-and-schema shape only — it does not evaluate the
  approval gate (item 92) or the cost-estimation gate, so `allowed: true`
  does not guarantee unattended execution would proceed.
- Quota-metering applies only when the connection's policy configures a
  window (`max_requests_per_window`/`max_response_bytes_per_window`, both
  off by default) — under the default policy `verdict` is unmetered, same as
  `execute` would be; the docs no longer state this unconditionally.
- A residual timing side-channel (schema validation does strictly more work
  than policy validation, so response *timing* can differ even though
  response *bodies* are byte-identical) is recorded rather than left
  implied-closed.

**Coverage.** Unit (`tests/unit/test_service.py`, ~25 verdict/explain tests
including the fail-closed collapse, the outer-handler category fix, the
plan-compile-audit fix, and per-connection/per-principal queue-depth caps),
real-SQLite end-to-end (`tests/integration/test_sqlite_end_to_end.py`,
including a three-way policy/schema/non-existent-table indistinguishability
proof and the `verdict_include_plan=True` × denied cross-product), MCP
transport-level (`tests/integration/test_mcp_server.py`, both the allowed and
denied paths through the real dispatch/serialization chain), REST
(`tests/integration/test_rest_api.py`), the malformed-input adversarial suite
(`tests/security/test_malformed_input_fuzzing.py`, `/query/verdict` added to
every REST boundary parametrization), the DAST AST-route exclusion
(`scripts/run_dast.py`, `tests/unit/test_run_dast.py`), and
`tests/unit/test_personal_denials.py` for the combined-surface exclusion.

**Follow-ups filed, not built here (each recorded as its own TODO item
rather than silently expanded into this one's scope):**
- Item 142 — `docs/THREAT_MODEL.md` uses the ID `QG-32` for two unrelated
  threats (pre-existing, surfaced while adding QG-34).
- Item 143 — `cryptography` 49.0.0 has an unreviewed CVE, blocking
  `make release-check`'s SBOM step (pre-existing, unrelated, surfaced while
  running the release gate).
- Item 144 — `verdict()` emits no query metrics, and `/metrics` is
  unauthenticated (the latter pre-existing).

**Effort:** M–L. **Depends on:** 31 and 39 (the existing verdict logic
reused), 26 (cost estimation, for the optional plan half), 45 + 121 (denial
vocabulary; scope-completeness).

### 134. Compliance-grade (WORM) audit retention + managed search ✅ DONE

**Surfaced 2026-07-30 by `competitive-scan`.** `GO_TO_MARKET.md`'s "Do not
claim yet" list had named compliance-grade/WORM audit retention and managed
search since early on. Item 91's hash-chained ledger detects tampering in
what was kept; this item closes the other half a regulated (fintech/
healthcare) buyer asks for by name: can you *produce* the records, not just
prove nobody edited them.

**Shipped (phase 1 — WORM retention).** `AuditSinkBackend.JSONL_CHAINED_S3_WORM`
composes (never replaces) the existing local hash-chained sink with an
additional `S3WormAuditSink` half, via a new `CompositeAuditSink`
(`audit/sinks.py`) — the `CompositeAuthenticator` shape, fanning one event
out to every composed sink and never letting one sink's failure suppress
another's write. `configure_audit_sink` is now dispatched through a real
`_SINK_FACTORIES` registry (mirroring `secrets/resolvers.py`'s
`build_secret_resolver_registry`) instead of the inline `if backend ==
...` chain non-negotiable #6 forbids.

`audit/worm_sink.py` is the new module: `S3WormAuditSink.emit()` only ever
appends to an in-process `InProcessWormEventBuffer` (bounded, drop-oldest,
metered) — zero network I/O on the request path, mirroring
`catalog/usage.py`'s buffered-signal/background-monitor split exactly, down
to the module-level singleton buffer both the enqueue side and the drain
side reach independently. A separate `WormFlushMonitor` background task
(wired into `app.py`'s lifespan like `CatalogUsageLearningMonitor`) drains
the buffer on a timer and `PUT`s one batched, Object-Lock-protected segment
per flush — deliberately never one object per event, since Object Lock's
retain-until timestamp is set per `PUT` and per-event objects would each
expire at a slightly different moment as they age out, leaving the
archive's shape incoherent.

**Fails open, by deliberate decision:** a flush failure never blocks or
fails the query that triggered the event (the local chain already captured
it), but the failed batch is re-queued for retry rather than silently
dropped, and `querygate_audit_worm_flush_failures_total` is a dedicated
metric an operator is expected to alert on — only a *sustained* outage past
`AUDIT_WORM_MAX_BUFFERED_EVENTS` drops the oldest events, visibly, via
`querygate_audit_worm_buffer_dropped_total`.

Item 136's capability-lookup pattern (already shipped) is what made adding
a fourth backend safe: `AuditSinkBackend` gained
`wraps_events_in_a_hash_chain_envelope()` alongside the existing
`is_locally_readable()`, replacing the four scattered `==
AuditSinkBackend.JSONL_CHAINED` equality checks in `help_routes.py`/
`admin_observability_routes.py`/`admin_ui_routes.py` — the exact "new
backend silently disables a shipped read surface" bug class item 136 exists
to prevent, now guarded by the same exhaustiveness-test pattern.

Redaction safety (non-negotiable #3) holds by construction: the WORM sink
never builds its own event body, it serializes the exact same
`PersistableEvent` the local sinks already write — proven byte-identical in
tests, not just asserted.

**Shipped (phase 2 — managed search, 2026-08-06).** `audit/worm_search.py`
adds `GET /api/v1/admin/observability/worm-search` — a bounded, filtered,
paginated search directly over the S3 WORM archive, closing the "search the
records" half phase 1 deliberately deferred. Gated by its own scope,
`admin:audit:worm-search` (`ADMIN_AUDIT_WORM_SEARCH_SCOPE`), NOT implied by
`admin:observability:read`: the WORM archive is the durable, potentially
multi-year compliance copy, so a principal that can read today's in-process
trend aggregates should not automatically gain search access to years of
retained history — proven by
`test_the_observability_read_scope_alone_is_not_sufficient`.

Every request requires an explicit `start_time`/`end_time` (no "search
everything" mode) capped at `AUDIT_WORM_SEARCH_MAX_WINDOW_DAYS` (default 730
days — wide enough for an "18 months back" compliance review, still a real
enforced ceiling); the actual S3 work is separately bounded per request by
`AUDIT_WORM_SEARCH_MAX_OBJECTS_SCANNED` and
`AUDIT_WORM_SEARCH_REQUEST_TIMEOUT_SECONDS`. An out-of-bound request is
rejected with a 422 before any S3 call is made; a bound hit mid-scan
degrades to a truncated page with a resumable, opaque cursor (encoding
`day`/`key`/`line` plus a fingerprint of the request's own filters, so
replaying a cursor against different filters is rejected rather than
silently returning a mismatched page) rather than continuing an
expensive/slow scan.

The scan itself stays bounded without a full-bucket listing by exploiting
`WormFlushMonitor`'s own segment-key structure
(`<prefix>/YYYY/MM/DD/<timestamp>.jsonl`, timestamp-first): one
`ListObjectsV2` per calendar day in the (flush-interval-padded) requested
window, not one unbounded listing over the whole prefix. Padding only
widens which day directories get listed — every individual event is still
filtered against the caller's exact window by its own `occurred_at`, so
padding cannot leak an out-of-window event into a result.

Redaction safety holds the same way phase 1's does: this module never
constructs its own event body, it validates each archived line against the
identical `extra="forbid"` `AuditEvent`/`ConfigChangeEvent`/
`CatalogGovernanceEvent`/`ConnectionProbeEvent` schemas the local sinks
already write (discriminated on `event_type`). Proven, not just asserted,
by `tests/security/test_worm_search_redaction.py`: a legitimate predicate
referencing a sensitive value never surfaces that value, and — the sharper
test — an S3 object tampered to carry a forbidden `sql`/`row_data`/
`connection_string` field alongside otherwise-valid fields is rejected
outright (counted as `malformed`, zero events returned), not silently
passed through with the extra field dropped.

Deliberately REST-only, not also an MCP tool: no sibling read on
`admin_observability_routes.py` (overview/anomalies/config-changes/history)
has an MCP counterpart either, so this stays consistent with the existing
surface rather than introducing a new REST/MCP asymmetry.

**Scope, stated honestly:** no browser UI over the search endpoint yet
(REST only); a search made during a sustained S3 outage won't find events
still sitting unflushed in the in-process buffer (the same fail-open
posture phase 1 already documents). Tested against `moto`'s S3 Object Lock
emulation, matching phase 1's own testing approach — no real AWS
credentials are available in this environment.

**Fixed by the 2026-08-06 `auditors` pass:** `WormFlushMonitor._run`'s loop
originally guarded only the S3 `PUT` itself — an exception from draining the
buffer or building the segment key propagated out of the loop uncaught,
permanently stopping WORM archival for the process (the local hash-chained
ledger still captured every event; only the S3 copy would have stopped).
Now wraps the whole per-iteration `flush_once()` call (and the final flush
in `stop()`) in a catch-all, mirroring `catalog/usage.py`'s
`CatalogUsageLearningMonitor`/`catalog/refresh.py`'s monitors, matching what
this module's docstring already claimed. Mutation-verified:
`test_a_flush_error_outside_the_put_does_not_kill_the_loop`
(`tests/unit/test_audit_worm_sink.py`) fails without the fix.

**Tested against `moto`'s S3 Object Lock emulation** (confirmed separately
to accept the same `ObjectLockMode`/`ObjectLockRetainUntilDate` parameters a
real bucket does), not a live AWS account — no real AWS credentials are
available in this environment. Every enforcement point was mutation-verified:
`CompositeAuditSink`'s "keep calling every sink even if one raises" (a naive
un-guarded loop confirmed to fail the suppression test), and `app.py`'s
lifespan actually calling `WormFlushMonitor.start()` (confirmed via a
public `is_running` property, not by reaching into a private attribute).

**Effort:** L (both phases shipped). **Depends on:** 91, 136.

### 135. Automatic credential re-resolution (TTL/lease-driven), without an operator-triggered reload ✅ DONE

**Surfaced 2026-07-30 by `competitive-scan`; scope corrected the same day by
`auditors` after an earlier draft got the current behavior wrong.**

**What already shipped (item 13 — not rebuilt here).** Rotation without a
process restart already worked: `config_reload.py` re-runs
`ConnectionRegistry.from_file(..., resolver_registry=...)`, which re-resolves
every `${vault:…}` reference; `_dispose_stale_engines` diffs
`old_profile.connection_string != new_profile.connection_string` and disposes
exactly the affected engines; `connections/engine.py`'s `dispose_engine`
already documented the in-flight-safe property. It was reachable via
`POST /api/v1/admin/reload-config`. An earlier draft of this item claimed
"every query fails until someone restarts the process" — that was false; the
genuine gap was narrower.

**The genuine gap: the refresh was operator-pull only.** No TTL, no lease
awareness, no automatic trigger — a rotation nobody followed with a reload
still opened an outage window, and short-TTL dynamic credentials (Vault's
main value proposition) could expire into failures between reloads.

**Shipped.** Added the *trigger*, not new plumbing:

- `LeasedSecretResolver` (`secrets/resolvers.py`) is a separate,
  `@runtime_checkable` Protocol — `lease_expiry(reference) ->
  Optional[datetime]` — deliberately not folded into `SecretResolver` (whose
  module docstring states its narrow one-method design on purpose). Composed
  via `isinstance` probing in `SecretResolverRegistry.soonest_lease_expiry`,
  mirroring `core/auth.py`'s `CompositeAuthenticator` "compose, don't widen"
  precedent. `EnvSecretResolver` deliberately does not implement it — it's
  leaseless (re-reads the environment on every `resolve()`), not
  un-refreshable.
- `VaultSecretResolver` implements the protocol, honestly reporting Vault's
  own `lease_duration` response field rather than inventing a TTL, capped at
  a sane bound (400 days) so an absurd server-reported value can't raise
  `OverflowError` out of the narrow `except ValueError` guarding it. The
  shipped Vault integration reads KV v2 (static secrets) only, whose
  `lease_duration` is genuinely `0`, so `lease_expiry` correctly reports
  `None` (no lease) for it today; the trigger activates automatically, with
  no further code change, the moment a registered resolver reads a reference
  that actually carries a lease. Extending Vault integration to a
  dynamic-secrets engine was explicitly out of scope for this item.
- `config_reload.CredentialLeaseMonitor` — a new independent, fail-open
  background task (disabled unless `CREDENTIAL_LEASE_REFRESH_ENABLED=true`),
  mirroring `catalog/refresh.py`'s `CatalogRefreshMonitor` shape: an
  `asyncio.Event`-gated poll loop, a catch-all around each iteration so one
  bad probe or reload never kills the loop, only the exception *type* logged
  (never its message). Each poll re-resolves the *currently live* source
  files (governance-aware — see below), scans only each connection entry's
  `connection_string` field (not the whole raw file — a `${...}`-shaped
  reference in a comment or an unrelated key is never resolved by a real
  reload, so probing it would react to something reload never reads), and —
  when the soonest reported expiry is within
  `CREDENTIAL_LEASE_REFRESH_MARGIN_SECONDS` and hasn't already been acted on
  — calls the exact same `reload_config()` the operator-triggered path
  already used, reusing `_dispose_stale_engines`/`dispose_engine` verbatim,
  and records an `audit_config_change(action="lease_refresh", ...)` event.
  Wired into `app.py`'s lifespan alongside the other background monitors.
  `AppConfig` rejects `CREDENTIAL_LEASE_REFRESH_ENABLED=true` without
  `VAULT_ENABLED=true` (no other resolver can report a lease today) or
  without a poll interval shorter than the refresh margin (otherwise a lease
  could come due and expire between two polls, unseen).

**Post-ship security review (same day) — one HIGH finding, fixed before
shipping, plus several Medium/Low hardening items:**

- **QG135-01 (HIGH): the monitor reloaded unconditionally from the raw
  `cfg.connections_file`/`cfg.policy_file`/`cfg.catalog_file` paths.**
  `admin/service.py`'s config-governance `apply()`/rollback makes a staged,
  four-eyes-approved version's *own* on-disk files (under
  `AppConfig.config_governance_dir`) the live truth, and never writes that
  content back to the plain `cfg.*` files. An unattended monitor that always
  used the plain paths would, the moment a lease came due in any deployment
  using config governance, silently revert the live registry/policy back to
  stale disk content — discarding an approved policy tightening, with zero
  audit trail (a plain loguru line, not a governance event). Fixed:
  `_current_source_paths()` prefers the active governed version's files
  (`ConfigVersionStore.file_paths`, same resolution `admin/service.py`'s
  `apply()` already does) and falls back to the plain `cfg.*` paths only
  when governance has never been used; every automatic trigger now also
  calls `audit_config_change(action="lease_refresh",
  principal="system:credential-lease-monitor", ...)`, a new
  `ConfigChangeAction` literal, so it's attributable like any other config
  change.
- **QG135-02 (Medium): synchronous Vault network I/O ran directly on the
  event loop from the async poll loop, no explicit timeout.** A
  slow/partitioned Vault could stall the whole process. Fixed: the probe
  (`_evaluate`) runs via `asyncio.to_thread`; `VaultSecretResolver` now
  constructs its `hvac.Client` with an explicit 10s timeout (vs. hvac's own
  30s default).
- **QG135-03 (Medium): probing an expiry called the same method that
  resolves the value — for a backend where "reading is issuing" (a Vault
  *dynamic* secrets engine, the feature's actual motivating case), every
  poll would mint and immediately orphan a fresh privileged credential just
  to check a timestamp.** Not exploitable today only because the shipped
  KV v2 integration never mints on read. Fixed at the protocol level before
  a second implementer could inherit the bug: `resolve_with_lease(reference)
  -> (value, expires_at)` became `lease_expiry(reference) ->
  Optional[datetime]` — no value returned, so probing can never be the thing
  that hands out a secret, and the docstring requires a future
  dynamic-secrets resolver to implement it as a non-minting introspection
  call (Vault's own `sys/leases/lookup`), not its own credential-generating
  read.
- **QG135-04 (Medium): no hysteresis — a lease whose TTL is shorter than the
  refresh margin (the normal case for a real dynamic credential) would
  reload on every single poll forever**, resetting every connection's
  concurrency semaphore each time and turning a documented "reloads are
  rare" tradeoff into a routine one. Fixed: `_last_triggered_expiry` tracks
  the expiry already acted on; the monitor only re-triggers once the
  underlying lease has actually rolled forward to a new expiry.
- **QG135-05 (Low): the `except ValueError` narrowing (added to make the
  `isinstance` guard's removal observable, see below) didn't fully hold —
  code after a successful backend read could still raise `OverflowError`
  for an absurd `lease_duration`.** Fixed by the same cap mentioned above.
- **QG135-06 (Low): the monitor scanned the whole raw connections-file
  text, not just the `connection_string` field a real reload actually
  resolves.** Fixed — see `CredentialLeaseMonitor` bullet above.
- **QG135-07 (Low): the monitor pinned one resolver registry (and Vault
  token) for its whole process lifetime; a rotated `VAULT_TOKEN` would be
  picked up by `/admin/reload-config` and governance apply but silently
  never by this monitor.** Fixed: `resolver_registry_factory` is called
  fresh on every poll, defaulting to `build_secret_resolver_registry(cfg)`.
- **QG135-08 (Low): a validator comment overclaimed what it guarantees.**
  Reworded.

**Invariant guard (non-negotiable #2/#3).** A new adversarial test
(`test_lease_driven_proactive_refresh_never_logs_the_resolved_secret`) drives
`CredentialLeaseMonitor` end to end against a fake leased Vault secret and
asserts the resolved marker appears in neither stdout nor stderr — capturing
real emitted JSON log lines, not just response bodies. It's a genuine
regression guard, not a vacuous one: a deliberately introduced leak (logging
the resolved `connection_string` alongside the existing
`disposed_connections` field) made this exact test fail before being
reverted.

**Mutation-verified before shipping** (each broken deliberately, confirmed a
specific test failed for that reason, then reverted): the trigger margin
comparison, the `isinstance(resolver, LeasedSecretResolver)` probe guard,
the `lease_duration` overflow cap, the monitor's per-iteration catch-all,
the `AppConfig` cross-field validators (vault-required and
interval-vs-margin), the governed-config-path preference
(`_current_source_paths`), the hysteresis guard
(`_last_triggered_expiry`), the `asyncio.to_thread` offload, the
`connection_string`-only text scan, and the per-poll fresh resolver
registry. The `isinstance` guard mutation initially passed silently (the
surrounding broad `except Exception` swallowed the resulting
`AttributeError`) — a genuine gap caught by this process, closed by
narrowing that except to `ValueError` (matching `lease_expiry`'s documented
contract) so the guard's removal is now observable.

**Coverage:** `tests/unit/test_secrets.py` (`LeasedSecretResolver`
composition, `VaultSecretResolver.lease_expiry` including the overflow cap
and the non-minting return shape, `iter_secret_references`,
`SecretResolverRegistry.soonest_lease_expiry` including the non-`ValueError`
propagation case, the `AppConfig` cross-field validators),
`tests/unit/test_config_reload.py` (`CredentialLeaseMonitor`
trigger/no-trigger/hysteresis/governed-path-preference/plain-path-fallback/
connection-string-only-scan/thread-offload/fresh-registry-per-poll/
iteration-failure/idempotent-stop/audit-event, plus the `app.py` lifespan
wiring gate itself), `tests/security/test_adversarial_security.py`
(`test_lease_driven_proactive_refresh_never_logs_the_resolved_secret`).

**Effort:** M. **Depends on:** 13 (which shipped the re-resolution this adds
a trigger for).

### 136. The `jsonl_chained` audit backend silently disables four shipped read surfaces ✅ DONE

**Surfaced 2026-07-30 by the `auditors` architecture review while scoping item
134; pre-existing defect, not a regression from that pass.** Four route-level
gates admitted only the *plain* backend:

- `api/help_routes.py` — `if cfg.audit_sink_backend == AuditSinkBackend.JSONL`
  (`GET /help/my-recent-denials`)
- `api/admin_observability_routes.py` (two sites) — `!= AuditSinkBackend.JSONL:
  return None` (`GET /admin/observability/anomalies`, the config change-trend
  report)
- `api/admin_ui_routes.py` — `!= AuditSinkBackend.JSONL` inside `_audit_page`
  (`GET /api/v1/admin/ui/audit/events`, the admin UI audit browser)

**Two different bugs, and conflating them would have made one surface worse.**
For the first three, the reader already handled the format — `admin/anomaly.py`
and `admin/config_trends.py` both transparently unwrapped the hash-chained
envelope — so the capability existed and was refused at the door. `_audit_page`
was different: it had no unwrap at all. It called
`_AUDIT_EVENT_ADAPTER.validate_python(raw)` directly on the raw line, and a
`LedgerRecord` (`{seq, prev_hash, event, hash}`) fails that discriminated union
and is counted as `malformed`. So a gate-only fix applied uniformly would have
turned that surface from honestly `source="disabled"` into silently empty with
a rising malformed count — strictly worse than before. That surface needed the
*reader* fix as well as the gate fix.

**The failure:** a deployment running `AUDIT_SINK_BACKEND=jsonl_chained` — the
tamper-evident configuration item 91 shipped and documents as opt-in, the one a
regulated buyer would actually enable — lost all four surfaces. Choosing the
stronger audit posture silently cost four observability features, which is
precisely backwards.

**What shipped:**

1. `core/config.py`'s `AuditSinkBackend` gained a single capability lookup,
   `is_locally_readable()` (`{JSONL, JSONL_CHAINED}`), replacing all four
   scattered equality/inequality checks — a future backend (item 134)
   declares its readability once here instead of repeating the check (and the
   bug class) at a third and fourth call site.
2. `_audit_page` gained the same envelope unwrap the other two readers had.
   That unwrap itself was duplicated inline in two readers
   (`admin/anomaly.py`, `admin/config_trends.py`); rather than adding a third
   copy, it was extracted once as `audit.ledger.unwrap_envelope()` and all
   three readers now call it. The extraction is not a pure move: the two
   inline copies it replaced matched on only two of `LedgerRecord`'s four
   keys (`event` + `hash`); the shared function requires all four
   (`seq`/`prev_hash`/`event`/`hash`), a hardening caught by the
   post-build `security-invariant-reviewer` audit that closes a latent path
   for a plain event body carrying its own same-named fields to be misread
   as a chain envelope.
3. The missing `jsonl_chained` route-helper cell was added per surface
   (`tests/unit/test_anomaly.py`, `tests/unit/test_config_trends.py`), plus
   full HTTP-level regression tests for all four surfaces
   (`tests/integration/test_admin_ui.py`,
   `tests/integration/test_personal_denials_api.py`,
   `tests/integration/test_anomaly_api.py`,
   `tests/integration/test_config_trends_api.py`), each writing a real
   `make_record`-built chained ledger and asserting the surface serves it.
4. `README.md`'s admin UI audit-browser line and the admin UI's own two
   "disabled" empty-state hint strings (`admin_ui/app.js`) were reconciled to
   name both backends; `.env.example` and `docs/PRODUCT_GUIDE.md` already
   named both correctly, which was independent corroboration the code (not
   the docs) was the defect.

**Post-build `auditors` audit (2026-08-01, all four reviewers run in
parallel — security-invariant, architecture-boundary, test-contract,
claim).** Architecture-boundary and claim came back clean bar nits (a stale
"Requires audit_sink_backend=jsonl" comment repeated in three `AppConfig`
field docstrings, fixed; a pre-existing, unrelated README claim about item 45
phase 2 being unshipped, filed under item 132 rather than fixed inline since
it's untouched by this diff). Test-contract found two real coverage gaps,
both closed: no full HTTP-level `jsonl_chained` test for the anomaly/
change-trend endpoints (only the unit-level route-helper call was tested) —
added; no negative-path test proving the admin UI audit browser's gate still
refuses `AuditSinkBackend.NONE` even when a file that would otherwise parse
exists at the configured path — added. Security-invariant confirmed the fix
introduces no bypass (envelope contents still pass through the pre-existing
`extra="forbid"` event-schema validation before reaching any response; no
`require_scope`/`Depends(get_principal)` line touched; `/help/my-recent-
denials`'s cross-principal filter untouched) and found two real but
deliberately out-of-scope gaps, filed as items 137 and 138 rather than folded
in: the four surfaces neither verify the chain nor disclose which backend
produced a `source="jsonl"` response (item 137, needs a maintainer decision on
disclosure vs. verification posture/cost), and the underlying per-request file
scan is unbounded by lines read — pre-existing for the default `jsonl` backend,
which this item's fix also made reachable under `jsonl_chained` (item 138). It
also flagged that `unwrap_envelope` duck-typed on only two of `LedgerRecord`'s
four keys; tightened to require all four as part of this item (see point 2
above), with a dedicated regression test
(`tests/unit/test_audit_ledger.py::test_unwrap_envelope_does_not_unwrap_a_partial_envelope_only_event_and_hash`)
and mutation verification recorded below.

**Mutation-verified:** reverting `is_locally_readable()` to a plain `==
AuditSinkBackend.JSONL` check made all new/extended `jsonl_chained` tests fail
for the expected reason across every surface, including the two full
HTTP-level tests added post-audit; reverting `_audit_page`'s new
`unwrap_envelope` call made its dedicated integration test fail (`total` 1 ->
0); reverting `unwrap_envelope`'s four-key check back to two keys made the
dedicated partial-envelope regression test fail. All three reverted cleanly
afterward and the full suite (1865 unit, 329 integration excluding real_db,
424 security) passed on the final tree.

**Effort:** S–M. **Depends on:** 91. **Blocks:** 134 (which must not
replicate the pattern).

### 137. Audit read surfaces neither verify nor disclose hash-chain integrity ✅ DONE

**Surfaced 2026-08-01 by the `security-invariant-reviewer` audit of item 136.**
Item 136 made the four read-only observability/help surfaces (the admin UI
audit browser, the anomaly report, the config/catalog change-trend report,
`/help/my-recent-denials`) accept `AUDIT_SINK_BACKEND=jsonl_chained` the same
way they already accepted plain `jsonl`. That fix is correct and in scope —
but it also newly makes those four surfaces reachable *readers* of the
chained-ledger file, and none of them verify the chain or say they didn't.

**The gap, precisely.** `audit/ledger.py`'s own module docstring says the
chain is "verify-only... nothing in the request pipeline reads the chain" —
`querygate-audit verify` is the only place integrity is actually checked. The
four surfaces' `unwrap_envelope` call (added by item 136) only recognizes the
envelope *shape* (all four `LedgerRecord` keys present); it never recomputes
`hash` or checks chain linkage. An actor with append access to
`AUDIT_JSONL_PATH` (compromised app user, writable log volume, a log-shipping
sidecar) can append a fabricated `{"seq":0,"prev_hash":"...","event":{...},
"hash":"anything"}` line with an arbitrary `event` body, and all four surfaces
will display it as a genuine event — the anomaly detector can be pushed over a
threshold or diluted below one, and (worst case) a forged event could be
attributed to another principal in that principal's own `/help/my-recent-denials`
view. Also, on a successful chained-backend read, all four surfaces report
`source="jsonl"` — the same literal a plain-`jsonl` read reports — so an
operator or auditor reading the API response cannot tell which backend, and
therefore which integrity posture, actually produced it.

**Why this is a new item, not folded into 136.** Fixing it changes the public
response contract (a new `source` value and/or a `chain_verified` field) and
requires a product decision on cost/posture: real per-request verification
recomputes a SHA-256/HMAC over every scanned line (cheap per-line, but adds up
over `max_events_scanned`), is only meaningful for forgery-resistance when
`AUDIT_LEDGER_HMAC_KEY` is set, and needs a decision on what an unkeyed chain's
"verified" even means to report honestly. Item 136's own scope was strictly
"restore the read access the four surfaces already had for `jsonl`"; widening
that read access's *trust model* is a distinct call.

**Decision (recorded in `docs/PRODUCT_GUIDE.md`'s Decision Log, 2026-08-05,
per the item-100–106 precedent that the Decision Log entry is the item's own
first step): both.**

1. **Disclosure.** `source` on all four response models
   (`AnomalyReport`, `ConfigCatalogChangeTrend`, `RecentDenialsReport`,
   `AuditEventPage`) widened from a `jsonl`/`disabled`(/`empty`) `Literal` to
   include `"jsonl_chained"`, and each route now passes through the actually
   configured `cfg.audit_sink_backend.value` instead of a hardcoded `"jsonl"`.
2. **Real per-record verification.** A new `audit/ledger.py` primitive,
   `verify_envelope_hash(raw, *, key=None) -> Optional[bool]`, recomputes
   `compute_record_hash` over `{seq, prev_hash, event}` and compares in
   constant time (`hmac.compare_digest`) against the record's own `hash`.
   Returns `None` for a plain (non-envelope) line — unchanged behavior — and
   `True`/`False` for an envelope. All three readers (`admin.anomaly
   .JsonlAuditEventSource`, `admin.config_trends.JsonlChangeEventSource`,
   `api.admin_ui_routes._audit_page`) call it immediately before
   `unwrap_envelope` and count a `False` result as `malformed` rather than
   displaying it — mirroring how item 136 itself consolidated the envelope
   unwrap into one shared primitive. A new `resolve_ledger_key(raw: str) ->
   Optional[bytes]` helper (also shared with `audit/sinks.py`'s write path,
   which previously inlined the identical conversion) turns
   `cfg.audit_ledger_hmac_key` into the same key bytes the sink HMACs with,
   so a keyed chain verifies correctly rather than against the wrong
   (unkeyed) assumption.

**Stated honestly, not oversold.** This is self-consistency verification
only, not full chain-linkage verification — a windowed/reverse-order scan
(the shape every one of these readers uses) never walks the whole file from
genesis, so it cannot by itself prove no record was *dropped*; that remains
`querygate-audit verify`'s job. It is real per-record forgery *detection* for
the concrete attack this item's report describes. Per `audit/ledger.py`'s own
already-documented integrity model, an unkeyed chain still only detects
tampering relative to a trusted external anchor — forgery is *infeasible*
(not just detectable) only when `AUDIT_LEDGER_HMAC_KEY` is set. Nothing here
changes that model; it only makes the four read surfaces observe it correctly
instead of trusting an envelope's shape alone.

**Coverage.** A genuine chain-valid record followed by a hand-forged one
(correct `prev_hash`/`seq` chained onto it, `hash: "anything"`, an arbitrary
`event` body) — verifying the forged record is counted `malformed` and
excluded from the response — at both the unit level (one new test per reader
in `test_anomaly.py`/`test_config_trends.py`) and the full HTTP level (a new
integration test on the admin UI audit browser); the three existing
"reads a hash-chained ledger" tests were corrected to pass the sink's own key
to the reader (they had silently been comparing against the *wrong* key,
which this item's fix would otherwise have broken) and the four existing
"reads a hash-chained ledger" `source=="jsonl"` assertions across the admin
UI, anomaly, config-trends, and personal-denials integration suites were
updated to `"jsonl_chained"`, matching the new disclosure behavior.
**Mutation-verified:** replacing `verify_envelope_hash`'s final
`hmac.compare_digest(...)` with an unconditional `True` made every new forged-
record regression test fail for the expected reason (a genuine event count
mismatch); reverted, and the full unit + integration + `make test-security`
suites pass on the final tree.

**Effort:** M (both disclosure and real verification, not just one).
**Depends on:** 91, 136.

**Post-ship hardening (2026-08-05, same-day mandatory audit).**
`security-invariant-reviewer` found the self-consistency check itself was
bypassable: a BARE (non-enveloped) forged line on a `jsonl_chained` backend
made `verify_envelope_hash` correctly return `None` ("not shaped like an
envelope" — indistinguishable from a legitimate plain-`jsonl` line) rather
than `False`, so it was never counted malformed even though every line on a
chained backend should be enveloped. Fixed with a `require_envelope: bool`
flag on both readers plus `_audit_page`, true only when the configured
backend is `jsonl_chained`; new regression tests (unit + integration) prove
a bare line is now excluded. See `docs/PRODUCT_GUIDE.md`'s Decision Log,
2026-08-05, for the full audit response.

### 138. Audit read surfaces scan the entire persisted file on every request, unbounded by lines read ✅ DONE

**Surfaced 2026-08-01 by the `security-invariant-reviewer` audit of item 136.**
`admin/anomaly.py`'s `JsonlAuditEventSource.load_query_events` and
`admin/config_trends.py`'s `JsonlChangeEventSource.load_change_events` both
streamed and `json.loads`/pydantic-validated **every line** of
`AUDIT_JSONL_PATH` on every request; `max_events_scanned` bounded only the
retained deque, not the read itself, so there was no early exit. The admin UI
audit browser (`_audit_page`) had the same shape. `/help/my-recent-denials`
reaches this path with authentication only, no admin scope, and no
rate-limit/quota middleware — by design (the response is filtered to the
caller's own `principal_id` before return) — so the least-privileged
authenticated caller could trigger a full-file scan on demand. Pre-existing
for the default `AUDIT_SINK_BACKEND=jsonl` backend; item 136 only made the
identical, already-shipped behavior reachable under `jsonl_chained` too.

**What shipped:**

1. A new shared primitive, `audit/file_reader.py`'s `iter_lines_reverse()`,
   reads the audit file backward from EOF in fixed byte chunks, yielding
   complete lines newest-physical-line-first, without loading the file into
   memory. Because the sink only ever appends, this means the data every
   caller actually wants — the recent window, the newest page — is examined
   first. **Why not just cap a forward scan:** capping lines read from the
   *start* of a large file would silently return stale or empty results,
   since the oldest lines are read first — worse than no bound at all for
   exactly the surfaces this item exists to protect.
2. All three readers (`admin/anomaly.py`, `admin/config_trends.py`,
   `api/admin_ui_routes.py`'s `_audit_page`) switched from a forward scan +
   bounded `collections.deque` to iterating `iter_lines_reverse`, each with a
   new hard cap on lines *read* (`AnomalyThresholds.max_lines_read`,
   `ChangeTrendThresholds.max_lines_read`, `AppConfig.audit_page_max_lines_read`,
   `personal_denials_max_lines_read`) — independent of how many are retained.
   The deques were removed entirely: reading tail-first, the first N matches
   encountered already *are* the newest N by construction. `AuditEventPage`
   (the admin UI audit browser's response model) gained a `truncated: bool`
   field for the same reason the other two report models already had one.
3. **Hardened same-day by a second `security-invariant-reviewer` pass**
   before this item shipped, catching real defects in the first cut: the
   `max_lines_read` fields existed on the thresholds models but were never
   threaded from `AppConfig` for two of three surfaces (an
   `architecture-boundary-reviewer` finding) — fixed. More seriously,
   `iter_lines_reverse` itself was algorithmically unsound for a single
   undelimited byte run: the carry-forward buffer is fully re-copied every
   chunk, making an unterminated "line" cost O(length²) instead of
   O(length), and nothing bounded that length since the line-count cap only
   increments once a line is actually yielded. Measured at the originally
   shipped 2,000,000-line default: ~1.6 GB read and 10-40s of blocking work
   per request on `/help/my-recent-denials` at realistic audit-line sizes —
   a bound in the formal sense, not the practical one. Fixed with two
   independent bounds inside `iter_lines_reverse`, both raising a typed
   `AuditFileReadBounded` rather than stopping silently (so `truncated`
   stays accurate rather than looking identical to reaching the start of the
   file): `max_line_bytes` (1 MiB default) aborts an undelimited run before
   it can grow past a fixed size; `max_total_bytes` (256 MiB default) bounds
   total bytes read regardless of line count, closing a companion gap where
   a file padded with enormous numbers of blank lines was never counted
   against the line cap (blank lines are filtered before a caller's own
   counter sees them). The `max_lines_read` defaults were also lowered from
   2,000,000 to 200,000 (50,000 for `personal_denials_max_lines_read`,
   defaulted tighter than the admin-scoped surfaces since it's the one
   reachable with authentication only) — now a real backstop under the
   byte-level bounds rather than the sole line of defense. A fourth finding
   — a short read during in-place file truncation could splice non-adjacent
   byte ranges into a fabricated line while still reporting
   `truncated=False` — is closed the same way: a short `handle.read` now
   raises `AuditFileReadBounded` instead of being silently concatenated.
4. Test-contract and claim-review findings from the same pass: added a
   `truncated is False` regression on `_audit_page`'s ordinary (no-cap-hit)
   path, since none of the existing tests asserted the field's default;
   clarified a test comment that overclaimed what it alone proved (an exact
   `max_events_scanned` boundary test, which cannot on its own distinguish a
   `>` vs `>=` off-by-one — that variant is covered by a sibling over-cap
   test); corrected the PRODUCT_GUIDE Decision Log entry, which had
   originally named only two of the three readers whose deques were removed.

**Deliberately not folded into this item**, filed as follow-ups instead:
bounding audit-line size at the *source* (the read-query AST's unbounded
`select`/`join`/`group_by` lists, and `audit/sinks.py`'s own
`_read_last_line` startup-path reader, which shares the pre-fix
unbounded-growth shape) — item 139; `_audit_page`'s pagination can still
materialize up to `cursor + limit` ≈ 1,000,100 dicts given the existing
`cursor` ceiling — item 140; converting the line-count cap into a
practically-tight, window-based early exit — a real further tightening, but
one that trades a small, bounded ordering-tolerance assumption for speed,
which needs a maintainer decision rather than being built under review
pressure — item 141.

**Mutation-verified:** every new enforcement point was broken deliberately,
confirmed to fail the specific test guarding it, and restored — the tail-first
iteration direction (all three readers), each line-count cap's break
condition (all three readers), `iter_lines_reverse`'s carry-forward logic,
its `max_line_bytes`/`max_total_bytes`/short-read guards, and each reader's
`AuditFileReadBounded` exception handling. Full suite (1898 unit, 333
integration excluding real_db, 424 security) passed on the final tree.

**Effort:** S–M (grew to M with the algorithmic hardening). **Depends on:**
none (touches the already-shipped `jsonl` path, item 91 for the
`jsonl_chained` share of it).

### 139. Bound audit-line size at the source, not just at the reader ✅ DONE

**Surfaced 2026-08-01 by the `security-invariant-reviewer` audit of item 138.**
Item 138 made `audit.file_reader.iter_lines_reverse` bail (raise
`AuditFileReadBounded`) on a single undelimited byte run longer than
`max_line_bytes` (default 1 MiB), which bounds the *reader's* worst case. It
does not address the two places an oversized line can originate:

1. **The read-query AST has no size limit on `select`/`joins`/`group_by`/
   `order_by`.** `query_ast/models.py`'s `StructuredQuery.select` has
   `min_length=1` and no `max_length`; `execution/service.py`'s
   `normalize_query_shape(query)` runs **before** policy validation and is
   written to the audit event even on the rejection path (`service.py:804`).
   An authenticated caller with query rights (no special privilege needed) can
   submit a `StructuredQuery` with tens of thousands of `select` entries;
   policy correctly rejects it (e.g. `max_select_columns`), but the rejection
   audit event still serializes the full oversized `query_shape` as one JSONL
   line first.
2. **`audit/sinks.py`'s `_read_last_line`** (used at process startup to
   resume a `jsonl_chained` ledger's sequence/hash) has the identical
   unbounded-expanding-read shape item 138 fixed in `iter_lines_reverse` —
   `handle.read(size - pos)` grows to the whole file if no newline is ever
   found, and it runs once at boot, so one oversized trailing line delays or
   OOMs startup rather than one request.

**Decision (recorded in `docs/PRODUCT_GUIDE.md`'s Decision Log, 2026-08-05,
per the item-100–106 precedent): a hard, non-operator-tunable `max_length`**
on the AST's list fields, enforced by Pydantic at request-parsing time —
before `normalize_query_shape` or anything else in application code ever
sees the payload — rather than reordering policy validation ahead of audit
normalization (riskier, and out of this item's "bound it at the source"
scope). Deliberately **not** the same knob as `Policy.max_select_columns`/
`max_joins`/`max_group_by` (operator-tunable, checked later in
`validation/policy_validation.py`): the AST cap is a hard ceiling sized only
to bound worst-case size, with generous headroom (10–100x) over each field's
Policy default or, for a field with no Policy cap at all, over a sane
maximum:

- `StructuredQuery.select`: `max_length=1000` (Policy default 30)
- `.joins`: `200` (Policy default 5)
- `.group_by`: `500` (Policy default 10)
- `.order_by`: `500` (no existing Policy cap)
- `.correlate`: `50` (Policy `max_correlated_refs` default 2)
- `.ctes`: `50` (Policy `max_cte_count` default 3)
- `SetOpSpec.arms`: `50` (Policy `max_set_op_arms` default 3)

The last three weren't named by this item's own report, but reading
`normalize_query_shape` end to end (`audit/events.py`) showed both
`correlate` and `ctes` — recursively, through nested CTE bodies — and
`set_op.arms` are walked into the audit shape the identical way `select`/
`joins`/`group_by`/`order_by` are, so the same gap applied to all of them;
`intent` was checked too and confirmed **already excluded** from the audit
event by design (`audit/events.py`'s own docstring), so it needed no change.

**Part 2 — `audit/sinks.py:_read_last_line` folded into item 138's bounded
reader.** Rather than hand-patch the second tail-scanner with its own bound
(which the composable-interfaces doctrine argues against — a second reader
carrying the same defect class is itself the problem, not just this one
instance of it), `_read_last_line` is now a thin wrapper over
`audit.file_reader.iter_lines_reverse`. A bound-exceeded tail (no newline
within `max_line_bytes`) now raises the same "refuse to silently fork the
chain" `ValueError` `_recover_head` already raises for an unparseable last
line — deliberately **not** treated as "file empty, start at genesis" (the
`None` case), since that would silently restart the sequence counter at 0
over a file with real prior content, a worse outcome than failing loud.

**Coverage.** Nine new unit tests (`test_query_ast.py::TestAuditLineSizeCaps`)
assert each of the seven capped fields rejects one-over-the-limit and accepts
exactly-at-the-limit; one new unit test
(`test_audit_ledger.py::test_sink_refuses_to_resume_a_ledger_whose_tail_has_no_newline_within_bounds`)
writes a 2 MiB newline-free ledger tail and asserts `HashChainedAuditSink`
raises instead of silently forking. **Mutation-verified:** raising the
`select` cap by 100x made its regression test fail for the expected reason;
short-circuiting `_read_last_line` to always return `None` made both the new
test and the pre-existing `test_sink_recovers_head_across_restart` fail (the
latter for a genuinely different, equally correct reason — it caught the
chain silently restarting at seq 0). Both reverted; full unit (1929),
integration (343 excluding `real_db`), and security (463) suites pass on the
final tree.

**Effort:** M (the AST cap needed a product decision on the right limit; the
sinks.py fold-in was S once item 138's primitive existed, as scoped).
**Depends on:** 138.

### 140. `_audit_page` pagination can still materialize ~1M dicts per request ✅ DONE

**Surfaced 2026-08-01 by the `security-invariant-reviewer` audit of item 138.**
`GET /api/v1/admin/ui/audit/events`'s `cursor` query param was
`Query(default=0, ge=0, le=1_000_000)`; `_audit_page` retains up to
`cursor + limit` matched, fully-parsed event dicts before slicing the response
page. A `cursor` near the ceiling therefore still allocated on the order of a
gigabyte for one admin-scoped request. This bound predated item 138 unchanged
(the old `deque(maxlen=cursor + limit + 1)` had the identical size), so item
138 did not introduce it — but it was the same class of defect that item
exists to fix, in the same function, and admin-scoped is not the same as
unbounded-safe.

**Decision (recorded in `docs/PRODUCT_GUIDE.md`'s Decision Log, 2026-08-05):
lower the ceiling, not a cursor-shape redesign.** The item offered both; the
lower ceiling closes the actual resource-exhaustion gap with a one-line
change and no API/response-shape change, versus an opaque file-position
cursor that's real scope growth for a marginal further improvement once the
ceiling itself is no longer six figures. `cursor`'s `Query(..., le=...)`
lowered from `1_000_000` to `5_000` — far past any real "load more" admin
session at the default `limit=50`, while bounding worst-case retained dicts
per request to ~5,100 (cursor + limit), several orders of magnitude below
the old bound. A cursor-shape redesign remains available later if 5,000
pages ever proves insufficient for a real deployment.

**Coverage.** A new integration test
(`test_admin_ui.py::test_audit_browser_rejects_a_cursor_past_the_lowered_ceiling`)
asserts `cursor=5000` is accepted (`200`) and `cursor=5001` is rejected
(`422`). **Mutation-verified:** restoring the old `1_000_000` ceiling made
the new test fail for the expected reason (`200` where `422` was asserted);
reverted, and the full admin UI integration suite (19 tests) passes on the
final tree.

**Effort:** S (lower the ceiling — the cursor-redesign alternative, M, was
not chosen). **Depends on:** 138.

### 142. `docs/THREAT_MODEL.md` uses the ID `QG-32` for two unrelated threats ✅ DONE

**Surfaced 2026-08-01/02 by the `claim-reviewer`/`security-invariant-reviewer`
audit of item 133; pre-existing, not introduced by that item.** `QG-32`
labeled both the audit-ledger tamper-evidence threat (item 91) and the
approval-token forgery threat (item 92) — `docs/THREAT_MODEL.md` had 35
distinct threat rows across only 34 unique `QG-` IDs. `TECHNICAL_REVIEW.md`
(a dated, point-in-time snapshot document, deliberately left unchanged) had
already flagged a related "QG-31-vs-QG-32" stale-count inconsistency from an
earlier pass, so this duplicate had survived at least one prior review.

**Shipped.** Renamed the item-92 approval-token-forgery row's ID from
`QG-32` to `QG-35` (the next unused ID), keeping the item-91 audit-ledger
row's ID stable at `QG-32` so every existing correct reference to it (a
second `docs/THREAT_MODEL.md` cross-reference and `docs/TODO_ARCHIVE.md`'s
own item-91 write-up) stayed correct without further edits.
`docs/SECURITY_POSTURE.md`'s summary line was simplified from item 133's
transitional "35 threats across IDs QG-01…QG-34 (QG-32 is used for two
unrelated threats...)" wording to the clean final form: "enumerates 35
threats (QG-01…QG-35)". `docs/THREAT_MODEL.md`'s own "Last reviewed" date was
bumped for the content edit.

**Verified via `claim-reviewer`** (the only applicable reviewer for a
doc-only ID rename — no code, test, or UI surface changed): row/ID counts
independently re-derived as exactly 35/35 with no gap or duplicate; both the
renamed and kept-stable rows confirmed intact (correct column count, no
truncation from the ID-only edit); every one of the five "should stay
untouched" references (the ledger cross-reference in THREAT_MODEL.md itself,
the item-91 archive write-up, TECHNICAL_REVIEW.md's point-in-time snapshot,
a historical already-completed ROADMAP.md entry, and the absence of any test
referencing either ID) independently re-verified correct; a broader
`QG-3[0-9]` sweep across README/PRODUCT_GUIDE/business docs found no missed
reference. No blocking findings.

**Effort:** XS. **Depends on:** none.

### 143. `cryptography` 49.0.0 has an unreviewed CVE, blocking `make release-check`'s SBOM step ✅ DONE

**Surfaced 2026-08-02 while running the release gate for item 133; unrelated
to that item — no dependency file was touched.** `make sbom`'s vulnerability
audit failed closed on `cryptography 49.0.0`:
`PYSEC-2026-3552`/`GHSA-g6cj-pr64-35w5`/`CVE-2026-69247`, a Bleichenbacher
padding-oracle in `pkcs7_decrypt_der`/`pkcs7_decrypt_pem`/`pkcs7_decrypt_smime`
(introduced in `cryptography` 44.0.0, fixed in 50.0.0). A quick check had
found QueryGate's own code never calls any `pkcs7_decrypt_*` function,
making a justified-allowlist entry look plausible, but the `dep-audit` skill's
own priority order puts a real upgrade first.

**Shipped.** `pyproject.toml`'s existing constraint (`cryptography >=44.0.1`)
already permitted the fix — `poetry update cryptography` resolved cleanly to
50.0.0 with zero other dependency changes, no lockfile drift
(`poetry check` clean), and the full test suite (2708 tests) passed unchanged
against the new version. `poetry run python scripts/generate_sbom.py`
confirms zero unreviewed vulnerabilities (0 allowlisted — no allowlist entry
was needed, since a real fix existed). `make release-check` now passes
completely clean end to end, including the SBOM/dep-audit step that had been
failing since item 133 surfaced this.

**Effort:** XS–S (turned out to be XS — a clean upgrade was available, no
allowlist judgment call needed). **Depends on:** none.

### 144. `verdict()` emits no query metrics, and `/metrics` is unauthenticated ✅ DONE

**Surfaced 2026-08-02 by the `security-invariant-reviewer` re-audit of item
133; two related, non-blocking observability gaps.** `verdict()` shared
`execute()`'s per-principal quota budget while emitting no metrics of its
own, so a caller could exhaust that budget through verdict calls alone with
no verdict-shaped trace for an operator's "why is this agent throttled"
debugging; separately, `GET /metrics` was unauthenticated, and already
labels rejections `"policy"` vs `"schema"` for ordinary `execute`/`explain`
traffic — the exact distinction `verdict()`'s own response body (QG-34)
collapses.

**Maintainer decision (both items this gap needed):** gate `/metrics`
outright rather than only document the exposure, and add dedicated
verdict-only counters rather than reusing `execute()`'s `reason`-labeled
rejection counter.

**Shipped.**

1. **`querygate_verdicts_total{connection,outcome}`** (`metrics.py`) —
   `outcome` restricted to `allowed`/`denied` only, deliberately never a
   `reason` breakdown, so this fix cannot reopen the exact oracle QG-34
   collapses. Incremented on `verdict()`'s allowed and denied return paths
   only — a quota/concurrency failure is a system-busy state, not a shape
   verdict (per the docstring's existing reasoning), so it does not touch
   this counter.
2. **`querygate_verdict_duration_seconds{connection}`** — observed on both
   outcomes, since a denied verdict's duration is still real
   validation/compile time, not noise (unlike `execute()`'s "successful
   queries only" convention).
3. **A verdict-driven quota exhaustion now increments the same
   `querygate_query_quota_rejections_total` counter `execute()` reports
   through** (`verdict()`'s outer `except`, gated on
   `isinstance(exc, QuotaExceededError)`), so it's visible on the operator's
   existing quota dashboard instead of invisible.
4. **`AppConfig.metrics_require_auth`** (default `True`) gates `GET
   /metrics` behind the same `Authenticator`/scope machinery as every other
   admin surface, via a new least-privilege `admin:metrics:read` scope
   (`core/scopes.py`, added to the "Operator" role bundle) — deliberately
   separate from `admin:observability:read` (a different consumer: a
   Prometheus scraper vs. an admin-UI operator reading aggregated trends).
   Setting it `false` is an explicit, documented operator opt-out for a
   deployment whose network reachability is already restricted.
   `docs/THREAT_MODEL.md` gets a new QG-36 row; the docker-compose and Helm
   reference deployments' bundled-Prometheus examples were updated to
   authenticate their scrape (a bearer-token file / `bearerTokenSecret`)
   rather than silently breaking.

**Effort:** S. **Depends on:** none.

### 145. Purpose-bound access: enforce the declared `intent`, don't just log it (feature F7) ✅ DONE

**Surfaced 2026-08-05 by `competitive-scan`.** `StructuredQuery.intent`
(`query_ast/models.py`) is logged with the compiled SQL for audit/debugging
today and never enforced. Immuta's flagship primitive — purpose-based access,
where a caller must declare *why* it needs the data from an allowed set of
purposes, and that purpose narrows what it can see — is real and, per the
2026-07-22 survey (`docs/business/MARKET_DOMINATION_ANALYSIS.md` F7), "barely
exists elsewhere": the access proxies log a justification string at best; no
MCP gateway or DB-vendor server enforces a declared purpose at all.

**Why it's newly actionable.** The AST field and the audit wiring already
exist — this is "enforce a value we already carry," not new surface area. It
composes with machinery already shipped: per-principal `Policy` resolution
(item 90's actor/subject chain), `mandatory_row_filters`, and column
masking/deny — a purpose is just another input to the same resolution, not a
new enforcement point.

**Shipped.** All four scope points, plus the propagation fix the item's own
scope implied but didn't spell out:

1. **A new field, `StructuredQuery.purpose: Optional[str]`** (max_length 200)
   — NOT a repurposed `intent`, which stays free text and stays excluded
   from the persisted audit event.
2. **`Policy.allowed_purposes: list[str]`** (empty = unrestricted, the same
   convention `allowed_tables` uses) gates every query on a connection once
   non-empty — connection-wide, not a second "which tables are
   purpose-gated" concept the item's scope didn't ask for. A missing or
   unrecognized purpose is rejected in `validation/policy_validation.py`'s
   new `resolve_purpose_policy`, before any DB touch.
3. **`Policy.purpose_policies: dict[str, PurposePolicyDelta]`** and
   `Policy.for_purpose(purpose)` apply the narrowing. `PurposePolicyDelta`
   has no "allow" field — only additional `denied_tables`/`denied_columns`/
   `mandatory_row_filters`/`column_masks`, unioned onto the base `Policy` —
   so "narrows never widens" holds by construction, not convention.
4. **The propagation fix:** `validate_policy` now RETURNS the effective
   (possibly purpose-narrowed) `Policy`, and `execution/service.py`'s
   `_validate_and_compile` rebinds its local `policy` to that return value
   before compiling. Without this, a purpose delta's `mandatory_row_filters`/
   `column_masks` would pass validation but never reach
   `compiler/sqlalchemy_compiler.py` (which reads `policy.mandatory_row_filters`/
   `policy.column_mask` directly from whatever `Policy` it's handed) —
   silently doing nothing. `explain()`/`verdict()` share the same choke
   point, so both inherit the fix.
5. **The declared purpose is persisted** (`AuditEvent.purpose`) — unlike
   `intent`, it's a fixed allow-listed token, not caller-authored prose, so
   this doesn't reopen the redaction guarantee (non-negotiable #3).

Full design rationale for all five points recorded in
`docs/PRODUCT_GUIDE.md`'s Decision Log (2026-08-05) and its new "Purpose-bound
access" section.

**Client SDK parity.** Both the Python (`client/builder.py`) and TypeScript
(`clients/typescript/src/builder.ts`) client builders got a matching
`.purpose(...)` method, and the shared cross-language kitchen-sink parity
fixture (`tests/fixtures/client_builder_kitchen_sink.json`, item 51) was
updated to include the new field — both `make test-ts-client` and the Python
parity test stayed green.

**Coverage.** Unit tests for `Policy.for_purpose` (union semantics per field,
mask precedence, the base-policy-untouched invariant), `validate_policy`'s
purpose gate (missing/unrecognized/valid purpose, narrowing applied), and an
end-to-end `StructuredQueryService.execute` test proving a purpose delta's
`mandatory_row_filters` entry reaches the COMPILED SQL, not just validation.
**Mutation-verified:** dropping the base `denied_tables` from the union (only
keeping the delta's) made the "narrows never widens" tests fail on exactly
that assertion; dropping `_validate_and_compile`'s policy reassignment made
the compiled-SQL test fail (filter never reached the SQL); disabling the
missing-purpose check made the gate tests fail with the wrong fallback error.
All three reverted; full unit (1950), integration (344, excluding `real_db`),
and security (463) suites pass on the final tree.

**Effort:** M. **Depends on:** none.

**Post-ship hardening (2026-08-05, same-day mandatory audit).** Four real
defects found by `security-invariant-reviewer`/`architecture-boundary-
reviewer`, all fixed same-day: (1) declared `purpose` was persisted to the
audit event even on a connection with `allowed_purposes` empty — reopening
`intent`'s free-text-in-audit-log channel under a different field name; now
gated on `policy.allowed_purposes` being non-empty. (2) `Policy.for_purpose`'s
`denied_columns`/`column_masks` merge used case-sensitive dict keys, so a
case-mismatched table name between base and delta could silently drop one
side's entries — for `column_masks` this was a real **unmask**, a "narrows
never widens" violation; fixed with a canonicalizing merge helper. (3)
`admin/service.py`'s candidate-policy simulation discarded the purpose-
narrowed policy, so a purpose delta's mandatory filter never appeared in
readiness reports; fixed with the same one-line reassignment `execution/
service.py` uses. (4) `admin/access_diff.py` never diffed `allowed_purposes`/
`purpose_policies` at all — an operator disabling the whole gate reported as
no access change; fixed with a new `_diff_purposes` function (surfaced a
second, pre-existing instance of the same gap for `column_masks`, filed as
item 148). See `docs/PRODUCT_GUIDE.md`'s Decision Log, 2026-08-05, for the
full audit response and every regression test/mutation-verification.

### 146. "5-minute first governed query" quickstart — close the named Toolbox onboarding gap ✅ DONE

**Surfaced 2026-08-05 by `product-scorecard`.**
`docs/business/COMPETITOR_GOOGLE_TOOLBOX.md`'s 2026-07-22 Decision commits
explicitly: "Steal the onboarding lesson, not the architecture... QueryGate's
discovery flow (list/describe/search_catalog) should be as close to
zero-friction as the guardrails allow — a '5-minute first governed query'
quickstart." That is the one dimension the brief's own scoring table hands
to a competitor outright (Onboarding / time-to-first-query: Toolbox 9,
QueryGate 6).

**Why it matters.** Today a new caller must chain `list_connections` →
`search_catalog`/`describe_schema` → hand-author a `StructuredQuery` from
raw JSON before running a first query. QueryGate already has every
ingredient — item 48's admin-approved query templates, the item-51 Python/
TypeScript client SDKs, and the catalog's `describe_schema` reflection —
but nothing composed them into a guided first-five-minutes path.

**Decision (recorded in `docs/PRODUCT_GUIDE.md`'s Decision Log, 2026-08-05):
a standalone `querygate-quickstart` CLI, not a `querygate quickstart`
subcommand.** The item's own prose showed `querygate quickstart <connection>`,
but `querygate` is the uvicorn server entrypoint with no subcommand
dispatcher — every other CLI in this repo is its own top-level `querygate-*`
script, so `querygate-quickstart` matches the established convention (and
the item's own "mirroring `querygate-config`" instruction).

**Shipped.** `src/querygate/quickstart_cli.py`: a thin, authenticated
`httpx` client (no new server-side authority) that (1) calls
`GET /{connection}/tables` then `GET /{connection}/tables/{table}` to find
the first table with ≥2 non-sensitive columns — reusing
`TableDescription.columns[].catalog.sensitivity` (item 32) directly, no
second sensitivity check invented and no extra `catalog/search` round trip;
(2) proposes exactly the three shapes the item asked for — a plain select, a
filtered select (`is_not_null`, so it's valid regardless of real data), and a
group-by aggregate (`COUNT(*)`); (3) prints, per query, a ready `curl`
command against `POST /{connection}/query`, an MCP `run_structured_queries`
tool-call JSON, and an equivalent `client/builder.py` Python-SDK snippet.
Never persists anything (item 48's template-authoring path is untouched, as
scoped). Wired as `querygate-quickstart` in `pyproject.toml`'s
`[project.scripts]`, documented in README.md's `## Quickstart` section
(exactly the section a new reader hits right after starting the server).

**Coverage.** `tests/unit/test_quickstart_cli.py`: skips a table whose
columns are all sensitive and picks the next qualifying one, never surfaces
a sensitive column in any proposed query, renders all three snippet kinds
for all three query shapes, reports a clean non-error message when no table
qualifies, and every "StructuredQuery body" block round-trips as valid JSON.
**Verified live**, not just mocked: against the real demo Postgres
(`docker compose up` + the packaged `examples/` connections/policy config),
all three generated `curl` commands were executed for real and returned
real rows, and the three printed Python SDK snippets were run directly and
produced byte-identical bodies to what the CLI displayed.
**Mutation-verified:** short-circuiting the sensitivity check to always
report "safe" made the sensitive-column regression test fail for the
expected reason (a PII column reaching a proposed query); reverted, and the
full unit (1961), integration (344, excluding `real_db`), and security (463)
suites pass on the final tree.

**Effort:** S–M. **Depends on:** 48, 51 (both shipped).

### 147. Self-serve procurement evidence page ✅ DONE

**Surfaced 2026-08-05 by `product-scorecard`.** The `trust-evidence` skill
already assembles a defensible security-posture packet — SBOM, item-54
compliance mapping, item-58 benchmark results, threat-model coverage,
credential-redaction evidence — but only ad hoc, hand-rebuilt per prospect
engagement. `docs/business/NORTH_STAR.md`'s own stated posture is "we are
not behind on capability, we are behind on evidence and market presence,"
and the one defined success metric is a design partner's security team
signing off; the artifact that shortens that review cycle doesn't persist
anywhere a prospect can be pointed at today.

**Why it matters.** Phase 2 (Enterprise procurement unlocks) already ships
items 53/54/60/134 as procurement-facing controls; this is the missing
"hand it to them" step. A live page beats a stale exported PDF because the
artifacts it cites (current SBOM, latest benchmark pass/fail, the
compliance-mapping table) drift as the codebase changes, and a hand-assembled
packet goes stale between rebuilds.

**Decision (recorded in `docs/PRODUCT_GUIDE.md`'s Decision Log, 2026-08-05):
a checked-in generated doc, not a live REST route.** `pyproject.toml`'s
`packages` list and the `Dockerfile`'s `COPY` lines both confirm `docs/` is
**not** part of the installed package or the container image — a live route
reading `docs/*.md` at request time would work in a dev checkout and fail in
the actually-shipped product. `scripts/generate_trust_page.py` (mirroring
`scripts/generate_sbom.py`'s shape) instead composes
`docs/SECURITY_POSTURE.md`, `docs/COMPLIANCE_MAPPING.md`,
`docs/business/SECURITY_BENCHMARK.md`, `SECURITY.md`'s disclosure section,
and the live `security/dependency-audit-allowlist.json` status **verbatim**
(no lossy summarization — no new evidence or claim) into one generated,
git-committed `docs/TRUST_EVIDENCE.md`, regenerated with `make trust-page`.

**Coverage.** `tests/unit/test_trust_page.py`: every named source doc exists,
`build_document` embeds every section header and the dependency-audit
summary, the empty-allowlist and populated-allowlist summary shapes, the
GitHub-flavored-markdown anchor helper, and the drift guard
(`test_committed_doc_matches_generator`) that fails if the checked-in file
and the generator disagree — extracting the committed file's own
version/date so the comparison isn't date-flaky. **Mutation-verified:**
corrupting a section heading in the committed `docs/TRUST_EVIDENCE.md` made
the drift-guard test fail for the expected reason; reverted (regenerated via
`make trust-page`) and the full unit suite passes on the final tree.

**Explicitly out of scope (per the item's own scope, honored as-is):** no
HTML/CSS marketing page — that's `pitch-sync`/`GO_TO_MARKET.md`'s job, and
`landing/security.html` already exists for that purpose; no unearned SOC 2 /
ISO / third-party-pentest claims (nothing here asserts one).

**Effort:** S. **Depends on:** 54, 58 (phase 1), 60 (all shipped).

### 148. `admin/access_diff.py` never diffs `column_masks` at all ✅ DONE

**Surfaced 2026-08-05 by `architecture-boundary-reviewer`/
`security-invariant-reviewer`, while auditing item 145's fix for the same
class of gap on `allowed_purposes`/`purpose_policies`.** `Policy.column_masks`
had been excluded from `GUARDRAIL_FIELDS` since item 49 shipped (masking),
with an inline comment claiming it was "already diffed field-by-field by
access_diff (tables, columns, masks, row filters)" — but `admin/access_diff.py`
had no `_diff_masks`/equivalent function and no `category="column_mask"`
anywhere. Real, pre-existing since item 49, not a regression from anything
shipped earlier that session.

**Shipped.** `admin/access_diff.py._diff_masks` diffs `Policy.column_masks`,
resolving through `Policy.column_mask(table, column)` itself rather than
flattening the raw `dict[table, list[ColumnMask]]` — so the diff sees exactly
what enforcement sees: a table-specific entry always wins over the `"*"`
wildcard, and within one table's list the first case-insensitive column match
wins. `_named_mask_columns_for_table` builds the per-table column universe
(named columns for that table, plus any named under `"*"`); the table
universe is `_named_tables(...)` unioned with every table keyed in either
policy's `column_masks` (excluding `"*"`); a wildcard entry on either side adds
the same "tables the policy does not name may also be affected" incompleteness
note `_diff_tables` already uses for its own allow-list-emptiness toggle.
Removing a mask is `loosening` (the real value becomes visible again); adding
one is `tightening`; changing `kind`/`length`/`bucket_size` is `modified`/
`neutral` — mirroring `_diff_mandatory_filters`'s own "value_changed → neutral"
precedent, since there is no general ordering between mask kinds. Wired into
`_diff_connection` alongside the other per-connection diffs; `"column_mask"`
added to `SemanticChangeCategory` (`admin/models.py`), to `_CATEGORY_PRIORITY`
(`admin/access_diff.py`, ranked with `column_access`), and to
`_RISK_CATEGORY_PRIORITY` (`admin/blast_radius.py`, ranked with
`column_access`) — the latter table was also missing `purpose_access`
(item 145's own category), found and fixed the same way while adding
`column_mask`. The stale `policy/models.py::_NON_GUARDRAIL_POLICY_FIELDS`
comment that made the false "already diffed" claim was corrected in the same
change.

**Caught by this item's own mandatory self-review, not shipped as first
written:** the first `_diff_masks` draft *did* flatten `column_masks` with a
plain dict keyed by `(table.casefold(), column.casefold())`, treating `"*"`
as an ordinary table name. Two independently launched reviewers
(`security-invariant-reviewer` and `architecture-boundary-reviewer`) both
found the same defect: moving an unchanged mask from `"*"` to one specific
table is a no-op for that table (the specific entry now provides what the
wildcard used to) but a real **loosening** for every other table the
wildcard used to cover — the flattened-map version reported the no-op as a
false `tightening` and missed the real loosening on the other tables
entirely. A second, related bug: two masks on the same column within one
table's list resolve first-match-wins at read time (`Policy.column_mask`)
but the flattened map kept whichever the dict-comprehension inserted last,
so a real enforcement-level mask change could be silently reported as no
change. Confirmed directly (`Policy.column_mask` evaluated against both
scenarios before any fix) before rewriting `_diff_masks` to resolve through
`Policy.column_mask` itself instead of reimplementing its precedence rules.

**Coverage.** `tests/unit/test_config_semantic_diff.py`: mask added is
tightening, mask removed is loosening (with the real mask parameters — not
just presence — asserted), a `kind`/`length` change is `modified`/`neutral`
rather than silently dropped, a `BUCKET`-kind display is asserted (not just
`NULL`/`LAST`), the table key is case-insensitive, an unchanged mask reports
nothing, a table-specific entry shadowing a wildcard is NOT a false
`tightening` (regression test for the finding above — asserts the shadowed
table reports no change while a genuinely-unmasked other table correctly
reports `loosening`), and duplicate same-column entries resolve first-match
exactly like `Policy.column_mask` (regression test for the second finding).
`tests/unit/test_blast_radius.py` gained a matching pair: a `column_mask`
loosening ranks ahead of a `guardrail` loosening, and an exhaustiveness test
(`test_every_semantic_change_category_has_a_risk_priority`, using
`typing.get_args(SemanticChangeCategory)`) that fails if any future category
is added to `SemanticChangeCategory` without a matching
`_RISK_CATEGORY_PRIORITY` entry — closing the whole class of bug, not just
this one instance. **Mutation-verified:** commenting out the `_diff_masks(...)`
call site made 5 of 7 mask tests fail for the expected reason (the two
"reports nothing" tests correctly stayed green either way); reverting
`_RISK_CATEGORY_PRIORITY` to omit `column_mask`/`purpose_access` made both new
blast-radius tests fail for the expected reason. Both reverted; full unit
(2003) and security (463) suites green on the final tree.

**Effort:** S (grew to M once the self-review findings were incorporated).
**Depends on:** none.

### 149. `Policy`'s case-insensitive table-key lookups disagree on `casefold()` vs `lower()` ✅ DONE

**Surfaced 2026-08-05 by `security-invariant-reviewer`, while reviewing item
148's `_diff_masks` fix.** `Policy._ci_lookup` (used by `column_allowed`/
`column_mask`/`table_allowed`) lowercased with `.lower()`, while
`Policy._merge_table_keyed` (item 145) and every table-keyed helper in
`admin/access_diff.py` already used `.casefold()` — the two disagree on a
handful of real Unicode identifiers (`"STRASSE".lower() != "straße".lower()`,
but `.casefold()` unifies both to `"strasse"`, confirmed empirically).

**Shipped, and grew in scope during its own mandatory self-review.**
`_ci_lookup` was extracted from a `Policy` staticmethod to a module-level
function in `policy/models.py` (its only two-line body is otherwise
duplicated), switched to `.casefold()`, and every `Policy` method that used to
inline `.lower()` (`table_allowed`, `column_allowed`, `column_mask`) now
delegates to it.

**A second, more severe bug was found and fixed in the same change:**
`WritePolicy.write_column_allowed` looked up `denied_write_columns` via a
literal `.get(table_name.lower(), [])` — a plain dict exact-key lookup, not
going through any case-insensitive helper at all. A `denied_write_columns` key
configured with any casing other than all-lowercase (e.g. `{"Orders": [...]}`
— the same casing `allowed_tables` legitimately uses elsewhere in the same
policy) **never matched, regardless of the write statement's own table
casing** — the write-column deny list was silently inert. Confirmed directly
with a Python script (`write_column_allowed("orders", "ssn")` returned `True`/
allowed when it should have denied) before fixing. `WritePolicy.table_writable`
was also switched to `.casefold()` for consistency (it was already correctly
case-insensitive, just via the wrong function).

**A third and fourth sibling bug were found by the mandatory
`security-invariant-reviewer` audit of this item's own fix, and fixed in the
same change rather than deferred:**
- `catalog/models.py`'s `TableCatalogEntry.column`/`ConnectionCatalog.table`
  used `.lower()` while this same class's own uniqueness validators
  (`_relationship_identities_are_unique`'s column-uniqueness check,
  `_table_names_are_unique`) already used `.casefold()` — so a catalog entry
  the module's own validation treats as a single unique table/column could
  fail to resolve here, and an unresolved catalog entry means a sensitivity
  label silently doesn't reach `execution/approval.py`'s human-approval gate
  (item 92 phase 2) — a fail-open approval bypass on the Unicode edge case.
  Confirmed empirically (`catalog.table("straße")` returned `None` for an
  entry keyed `"STRASSE"`) before fixing.
- `admin/templates.py`'s `_merge_allowed_tables`/`_merge_denied_columns`
  (item 46's policy templates) matched table names by plain ASCII-case-
  sensitive `in`/dict-key lookup with **no** case-folding at all — a
  materially more reachable bug than the Unicode edge case above, since it
  fails for any casing mismatch (e.g. the ordinary "Orders" vs "orders" the
  whole `_ci_lookup` mechanism exists to handle). An existing allow-list
  keyed with different casing than the template's own casing would
  intersect down to an **empty** list, which `Policy.table_allowed` reads as
  *no restriction at all* — silently inverting this module's own stated
  "monotonically restrictive" invariant. A differently-cased
  `denied_columns` table key would create a second shadow key instead of
  merging, silently dropping one side's denied columns. Fixed with a local
  case-insensitive intersection/merge (mirroring `Policy._merge_table_keyed`'s
  precedent) rather than importing that private helper across modules.

**Deliberately NOT fixed under this item — recorded as item 150 instead:** the
`security-invariant-reviewer` audit also found the identical bug class in
`compiler/sqlalchemy_compiler.py`'s `mandatory_row_filters` matching (tenant
row-scoping — higher stakes than any method fixed here) and in
`execution/approval.py`'s sensitivity-scan table resolution, but both root in
`validation/schema_validation.py`'s `effective_name_map`/`declared_cte_names`/
`cte_source_names` — a foundational, internally-self-consistent `.lower()`
subsystem used throughout AST alias resolution across policy validation,
schema validation, and compilation. Properly fixing the compiler-side
comparison requires switching that whole subsystem to `.casefold()`, not a
one-line change like the four fixed above — a larger, riskier change to the
core validation/compilation pipeline that deserves its own dedicated,
reviewed unit of work rather than a same-session patch under time pressure.

**Coverage.** `tests/unit/test_policy_models.py`: `column_allowed`/
`table_allowed`/`column_mask` each resolve a genuine `"STRASSE"`/`"straße"`
pair. `tests/unit/test_governed_writes.py`: the write-column deny check is
pinned to its own exact message (not the looser "not writable" substring
`table_writable` also raises), a casing-mismatched `denied_write_columns` key
still denies, the `"*"` wildcard term (previously untested — deleting it kept
the suite green) is exercised directly, and `table_writable`'s own case-
insensitivity (independent of the column check) is asserted directly.
`tests/unit/test_catalog.py`: a `"STRASSE"`/`"straße"` table+column pair
resolves through `CatalogStore.get_table`/`.column()`. `tests/unit/
test_policy_templates.py`: an existing "Orders" allow-list is not silently
emptied by a lowercase-cased template, and an existing "Customers"
denied-columns key merges with a lowercase-cased template patch into one key,
not two. **Mutation-verified:** every one of the eight enforcement lines
touched (both `Policy` case-fold sites, `WritePolicy`'s wildcard term and its
own case-fold, both `catalog/models.py` methods, both `admin/templates.py`
merges) was broken deliberately and confirmed to fail its own regression test
for the exact reason, then restored. Full unit (2012), integration (345), and
security (463) suites green on the final tree.

**Effort:** S as scoped; M once the four sibling bugs found by this item's own
mandatory security review were folded in.
**Depends on:** none.

### 150. `compiler/sqlalchemy_compiler.py`'s `mandatory_row_filters` matching uses `.lower()` against `schema_validation.py`'s `.lower()`-consistent AST name resolution ✅ DONE

**Surfaced 2026-08-05 by `security-invariant-reviewer`, while reviewing item
149's fix for the identical bug class.** `mandatory_row_filters` compiling
compared an AST-resolved physical table name against `MandatoryRowFilter.table`
(an operator-configured value from `Policy`, unconstrained by the AST's own
`VALID_TABLE_NAME` ASCII-only pattern) using `.lower()` on both sides — the
one instance item 149 deliberately deferred, since it roots in a subsystem
with several call sites rather than a single method.

**Shipped, as a mechanical, uniform sweep.** Confirmed first that the subsystem
in question — `validation/schema_validation.py`'s `effective_name_map`,
`declared_cte_names`, `cte_source_names`, and every function built on them
across `validation/schema_validation.py`, `validation/policy_validation.py`,
`execution/approval.py`, `execution/service.py`, and
`compiler/sqlalchemy_compiler.py` — was internally self-consistent (every one
of its own `.lower()` call sites agreed with every other), which is why it
never surfaced as a live bug against `Policy`/`catalog` (already `.casefold()`
after item 149): the mismatch only bites where a `.casefold()`-side value
(like `MandatoryRowFilter.table`) crosses into this `.lower()`-side subsystem.
Read every one of the ~64 `.lower()` call sites across the five files (table/
column/alias/cte-name comparisons, self-join detection, correlation-boundary
resolution, k-anonymity uniqueness sets, cte-projection dedup) individually
before touching anything, to confirm each one is genuinely an identifier
comparison this bug class applies to and not something unrelated — none were.
Switched every one to `.casefold()` in the same change (a 1:1 substitution,
confirmed by diff stat: only `.lower()` → `.casefold()` changed, nothing
else), plus the docstrings that explicitly described the old `.lower()`
behavior ("lowercased names" → "case-folded names").

**Why a blanket sweep was the right level, not a narrower patch.** For pure-
ASCII identifiers (the overwhelming majority of real schemas), `.lower()` and
`.casefold()` are defined to produce identical output — Python's `.casefold()`
is a strict superset of `.lower()`, more aggressive only for a handful of
non-ASCII cases (German `ß`, Turkish dotted/dotless İ/ı, certain ligatures).
Switching every self-consistent site together preserves ASCII behavior
*exactly* while closing the Unicode-casing gap everywhere at once — confirmed
by the fact that all 2012 previously-passing unit tests kept passing
unchanged after the sweep, with no test needing an update for the ASCII case.
A narrower patch touching only the flagged `mandatory_row_filters` comparison
would have left `effective_name_map`'s own dict keys on `.lower()` while
`MandatoryRowFilter.table`-adjacent code compared via `.casefold()` — the
exact kind of half-migrated inconsistency item 149 was created to close, not
reproduce one level down.

**A real regression was found by this item's own mandatory two-reviewer
self-review before landing, and fixed in the same change.** Both
`security-invariant-reviewer` and `architecture-boundary-reviewer`
independently found that the 5-file sweep above missed
`query_ast/models.py`'s own `_validate_table_aliases`/`_validate_cte_names`
validators — the AST-layer uniqueness checks `effective_name_map` relies on
as a *precondition* (its dict is only well-defined because the AST already
guarantees every effective name/cte name is unique). Those validators were
still `.lower()`-based. Confirmed directly: `StructuredQuery(from_table=
"customers", from_alias="straße", joins=[JoinSpec(table="orders",
alias="STRASSE", ...)])` was accepted as having two DISTINCT effective names
(`.lower()` keeps `"straße"` and `"strasse"` apart), while the now-`.casefold()`
`effective_name_map` collapsed both to one key — silently dropping `customers`
from the query graph entirely, a real integrity gap the sweep introduced by
fixing the consumer side without fixing the precondition the consumers
depend on. `JoinSpec.table`/`alias` and `from_table`/`from_alias` carry no
ASCII-only pattern (unlike `CteSpec.name`, which does — see below), so this
was genuinely caller-reachable. Fixed with the same mechanical substitution
in `query_ast/models.py`'s two validators; verified the reproduction is
correctly rejected after the fix.

**Two further leftover `.lower()` sites in the identical bug class, found by
the same review pass, also fixed:** `api/admin_ui_routes.py`'s policy
simulator (`_test_policy`) matched a simulated table against
`mandatory_row_filters` using `.lower()` two lines below where it calls the
now-`.casefold()` `policy.table_allowed`/`column_allowed` — a simulated
`allowed=True` verdict with no mandatory filter listed for exactly the
table/filter pair real execution would reject, reachable since both the
request's `table` field and `MandatoryRowFilter.table` are unconstrained
strings. The same file's `_normalize_columns` request-body duplicate check
was switched too, for the same reason (a lower-severity, request-payload-only
consistency gap). `validation/write_schema_validation.py`'s WHERE-ref-must-
match-target-table check was also switched — **but this one is provably
unreachable in production**, not merely low-severity: both sides of that
comparison (`ref_table` via `parse_column_ref`'s `sanitize_table_name`,
`statement.table` via `_load_table`'s `sanitize_table_name`) are already
restricted to `schema/reflection.py`'s ASCII-only `VALID_TABLE_NAME` pattern
before this comparison ever runs, so neither side can carry a non-ASCII
character to begin with. Fixed anyway for consistency (harmless, zero
behavior change for the only inputs that can ever reach it), but deliberately
left without a dedicated regression test — manufacturing one would require
also bypassing `sanitize_table_name`, which is not the shape of a genuine
test.

**Coverage.** `tests/unit/test_compiler.py::test_mandatory_row_filter_applies_despite_a_casefold_lower_disagreement_in_table_name`:
a real physical table named `"STRASSE"` (the only spelling reflectable at all
for a genuine DB table — `VALID_TABLE_NAME` rejects any non-ASCII table name
at load time, which is exactly why THIS mismatch can only ever originate on
the *policy*-configured side, never the AST/reflection side) with a
`mandatory_row_filters` entry configured `table="straße"`; asserts the filter
value still appears in the compiled statement. `tests/unit/test_query_ast.py::test_duplicate_effective_name_rejected_across_a_casefold_lower_disagreement`:
the `"straße"`/`"STRASSE"` alias-collision reproduction above, asserting
rejection with a typed error instead of silent AST/compiler disagreement
(added a CTE-name variant too, then removed it on realizing `CteSpec.name`'s
own `^[A-Za-z_][A-Za-z0-9_]*$` pattern makes a casefold/lower-disagreeing cte
name unconstructible — that specific test would have been non-vacuous but
non-discriminating, since plain ASCII case-insensitivity was already covered
by an existing test). `tests/integration/test_admin_ui.py::test_policy_simulation_mandatory_filter_match_survives_a_casefold_lower_disagreement`:
the `_test_policy` simulator fix, end-to-end through the real route.
**Mutation-verified:** every one of the four fixed comparisons (compiler's
`_apply_mandatory_row_filters`, `query_ast/models.py`'s two validators,
`admin_ui_routes.py`'s simulator match) was broken deliberately and confirmed
to fail its own regression test for the exact reported reason, then restored.
Full unit (2014), integration (346), and security (463) suites green on the
final tree.

**Effort:** M as scoped; grew similarly to items 148/149 once this item's own
mandatory review found the AST-layer precondition gap and two further
leftover sites. **Depends on:** none.

### 151. Bind the in-query approval gate's token to a connection and principal, not just an AST fingerprint ✅ DONE

**Surfaced 2026-08-06 by the `security-invariant-reviewer` audit of items
19/128.** `execution/approval.py`'s `query_fingerprint`/`write_fingerprint`
hash the AST alone. An approval token minted for query `Q` on connection
`staging` verified unchanged for the byte-identical `Q` on connection
`prod` — a realistic scenario, since staging and prod normally share
table/column names while only prod carries the catalog `sensitivity: pii`
labels or the row volumes that trip the gate. A `query:approve` holder who
reads and approves what they believe is a staging query had, in fact,
approved it everywhere the same AST is submitted. The MCP MRTR channel
(item 128's `mcp/elicitation.py`) inherited this unchanged, since it reuses
`execution/approval.py`'s token verbatim. Separately, neither the REST token
nor the MRTR pending state was bound to a QueryGate principal at all: a
`request_state` handed from one agent session to another was honored for the
second principal's identical tool call.

**Shipped: additive `"cx"`/`"sub_bind"` claims, not a fingerprint-absorbing
redesign.** `issue_approval_token`/`verify_approval_token`
(`execution/approval.py`) gained optional `connection_id`/`principal_subject`
parameters, carried as new `"cx"`/`"sub_bind"` claims in the signed payload
**alongside** the existing `fp`/`sub`/`exp` — the fingerprint computation
itself is untouched, and a token minted without the new parameters (the
pre-item-151 shape) verifies exactly as before, so every already-issued
token's shape and any doc describing "the fingerprint is a hash of the AST"
stay stable. `verify_approval_token` only enforces a claim the token actually
carries: a token minted with `cx`/`sub_bind` is rejected — fail-closed, the
same posture as a fingerprint mismatch — unless the caller supplies a
matching value (including when the caller omits it entirely). `"sub_bind"` is
a field distinct from the pre-existing `"sub"` claim (the approver's subject,
recorded for audit only, unchanged): reusing `"sub"` for enforcement would
have collided with MCP's real-grant minting, which writes an audit-distinctive
`f"mcp-elicitation:{caller.subject}"` into `"sub"` and would break an equality
check against the raw subject.

**Wired through every issue/verify call site.** `StructuredQueryService.
_enforce_approval_gate` (`execution/service.py`) and `WriteExecutionService.
_enforce_write_approval_gate` (`execution/write_execution.py`) pass their own
`self._connection_id`/`self._principal_subject` into `verify_approval_token` —
no new parameter threading needed at that layer, since both were already
instance state. The connection bound is always the query/write's own
top-level `connection_id`, never a cross-connection join's own `connection`
(`JoinSpec.connection`) — the same scoping item 130 (not yet implemented on
this branch) proposes for its own `Mcp-Param-Connection` gateway header, though
this decision was reached independently of that item. REST's `approve_query`/
`approve_write` (`api/routes.py`) mint with `connection_id=<path param>`,
`principal_subject=<the approver's own subject>` — meaning the same principal
that calls `POST /{connection}/query/approve` must be the one that redeems
the resulting token via `POST /{connection}/query`; a token handed to a
different principal to redeem is now rejected. MCP's `build_pending_input_required`/
`resolve_approval_tokens_from_retry` (`mcp/elicitation.py`) gained `caller`/
`connection_id` parameters and bind both the pending state and the resulting
real grant to the *original calling* principal's subject — the principal
whose tool call tripped the pending elicitation — closing the request_state
session-handoff gap without changing legitimate usage, since both ends of one
MRTR round trip necessarily share that principal. `mcp/tools/query.py` and
`mcp/tools/write.py` thread `caller`/`connection` into both calls.

**Deliberately not wired: MCP's own `RequestStateSecurity(bind_principal=...)`**
(`mcp/server/request_state.py`, shipped in the v2 SDK). It reads
`mcp.server.auth.middleware.auth_context.get_access_token()` — the SDK's own
auth layer — but QueryGate authenticates independently in its own
`MCPAuthMiddleware` on top of `core/auth.py` and never populates that
context, so wiring the SDK mechanism would be dead code that appears to
enforce something it structurally cannot see. The claim-based check above,
sitting below the transport layer where QueryGate's own `Principal` is
actually available, is the enforcement point instead. See the Decision Log
for the full reasoning.

**Post-implementation `auditors` review found two real, additional gaps in
the same module — both closed in the same commit, not deferred:**

**F1 (Medium): a pending MRTR elicitation token was itself a valid grant.**
`build_pending_input_required` minted a token carrying a genuine `fp`/`cx`/
`sub_bind`, but nothing distinguished "this was asked about" from "this was
approved" — `verify_approval_token` had no way to reject a pending token
presented directly as `X-QueryGate-Approval` or in a batch's
`approval_tokens` map, so a principal could pull it straight off
`request_state` and have it execute without ever answering the elicitation
prompt or holding `query:approve`. Fixed with a new `"k"` claim
(`TOKEN_KIND_GRANT`/`TOKEN_KIND_PENDING`): `issue_approval_token(..., kind=)`
defaults to `"grant"`; the elicitation pending-mint site is the only caller
that passes `"pending"`; `verify_approval_token(..., expected_kind=)` defaults
to `"grant"` and rejects any mismatch unconditionally (not just when the
claim is present, unlike `cx`/`sub_bind`'s opt-in binding). The elicitation
resolver's own pending-state check now passes `expected_kind=TOKEN_KIND_PENDING`.

**F2 (Low/Medium): `connection_id`/`principal_subject` were optional-with-
`None`-default, reopenable by a future call site simply forgetting them.**
Dropped the defaults — both are now required keyword arguments on
`issue_approval_token`/`verify_approval_token` (`TypeError` if omitted; a
call site must still explicitly pass `None` to opt out, a deliberate choice
visible at the call site rather than a silent gap). Also added an
unconditional `"v"` (format-version) claim, checked at verify time
regardless of what else the token carries — closes a rolling-deploy race
where a token minted by a pod running an older build (predating these
claims entirely) could otherwise be treated as unbound-and-therefore-
permissive by a newer pod that understands them.

Also fixed from the same review: **F4/citation** — the connection-scoping
rationale no longer cites `mcp/transport_guard.py`'s `Mcp-Param-Connection`
check as existing precedent (item 130 proposes it; it is not implemented on
this branch), stating the actual code fact instead (`self._connection_id` is
set once in `__init__`; `JoinSpec.connection` is read only locally in schema
validation). **F5** — `mcp/elicitation.py`'s `_PENDING_SUBJECT` comment
updated to point at the `"k"` claim as the real enforcement mechanism, not
the audit-only `"sub"` field. **F6** — every "session" reference in the
connection/principal-binding docs and code comments corrected to "the
calling principal's own `Principal.subject`" — `sub_bind` does not
distinguish two delegated sessions acting for the same human subject, which
is a real, narrower scope than session-binding, not what the original
wording implied. **F7** — `approve_query`/`approve_write`'s docstrings
restated accurately: since only the approving principal can redeem the
token, the only configuration that can self-approve is one principal
deliberately granted both `query:approve` and execution scope — a
deployment choice, not something these endpoints can prevent on their own.

**One finding filed as its own item, not fixed here:** the sensitivity
trigger (`sensitivity_approval_reasons`) resolves every table against the
query's single top-level `connection_id`, so a cross-connection join can
pull in a `pii`-labelled column from the *joined* connection's catalog
without tripping the gate — real, pre-existing, and out of this item's scope
(a currently connection-unaware function needs a per-table connection map
threaded through it). Filed as TODO.md item 155.

A separate `test-contract-reviewer` pass found three test-quality gaps, also
closed in the same commit: (1) every mint-site test asserted only the
positive "this token verifies with the right binding" case, which can't
distinguish a genuinely bound token from an unbound one matching by
coincidence — added the negative counterpart at each of the four production
mint call sites; (2) `POST /{connection}/write/approve` had zero test
coverage of any kind — added
`test_approve_write_endpoint_issues_verifiable_token`; (3) the security-suite
tests called `_enforce_approval_gate` directly, bypassing the REST transport/
auth layer entirely — added
`test_approve_endpoint_only_the_approving_principal_can_redeem_the_token`, a
full HTTP round trip using two genuinely distinct JWT principals (API keys
can't produce two different principals in one `AppConfig`, since
`ApiKeyAuthenticator` maps every configured key to one shared
`api_key_subject`) that approves as one principal and asserts a fresh `428`
when a different principal presents the same token, then success when the
approving principal redeems it themselves.

**Coverage.** `tests/unit/test_approval.py`: low-level `cx`/`sub_bind`/`k`/`v`
mismatch/omission round-trips on `issue_approval_token`/`verify_approval_token`
directly (`test_token_bound_to_a_connection_is_rejected_for_a_different_connection`,
`test_token_bound_to_a_connection_is_rejected_when_connection_omitted_at_verify`,
`test_token_bound_to_a_principal_is_rejected_for_a_different_principal`,
`test_token_bound_to_a_principal_is_rejected_when_principal_omitted_at_verify`,
`test_unbound_token_verifies_regardless_of_connection_or_principal_supplied`,
`test_token_bound_to_both_connection_and_principal_requires_both_to_match`,
`test_pending_kind_token_is_rejected_where_a_grant_is_expected`,
`test_grant_kind_token_is_rejected_where_a_pending_marker_is_expected`,
`test_token_missing_the_format_version_claim_is_rejected`); the service-gate
seam (`test_gate_rejects_a_token_minted_for_a_different_connection`,
`test_gate_rejects_a_token_minted_for_a_different_principal`); and the REST
mint paths, both directions
(`test_approve_endpoint_issues_verifiable_token`,
`test_approve_write_endpoint_issues_verifiable_token`, both asserting a
matching-binding pass AND a mismatched-binding fail), plus the full HTTP
round trip (`test_approve_endpoint_only_the_approving_principal_can_redeem_the_token`).
`tests/unit/test_write_execution.py`: the write-gate siblings
(`test_write_approval_gate_rejects_a_token_minted_for_a_different_connection`,
`test_write_approval_gate_rejects_a_token_minted_for_a_different_principal`).
`tests/unit/test_mcp_elicitation_approval.py`: every existing
`build_pending_input_required`/`resolve_approval_tokens_from_retry` call site
updated to thread `caller`/`connection_id`/`kind`, plus new siblings to the
existing fingerprint-swap-replay test
(`test_resolve_rejects_a_request_state_handed_to_a_different_agent_session`,
`test_resolve_rejects_a_request_state_replayed_against_a_different_connection`,
`test_pending_token_cannot_be_redeemed_directly_as_a_grant` — F1's dedicated
regression, feeding a pending token straight into
`StructuredQueryService._enforce_approval_gate`). `tests/unit/
test_mcp_write_tool.py`: its own pending-state fixture updated for the new
`kind` claim. `tests/security/test_adversarial_security.py`: the item's exact
motivating scenario end to end
(`test_approval_token_minted_on_a_low_sensitivity_connection_cannot_redeem_on_a_high_one` —
mints on an unlabelled `staging`-like connection, redeems against a `prod`-like
connection whose catalog carries a `pii` label on the same query, asserts
`ApprovalRequiredError`) and the REST-flow principal-swap sibling
(`test_approval_token_cannot_be_redeemed_by_a_different_principal`).

**Mutation-verified**, on the final tree: the `"k"`-claim check
(F1) and the `"v"`-claim check (F2) were each independently disabled and
re-verified to fail exactly the tests naming them
(`test_pending_kind_token_is_rejected_where_a_grant_is_expected`/
`test_pending_token_cannot_be_redeemed_directly_as_a_grant`/
`test_build_pending_shapes_one_input_request_per_item` for `"k"`;
`test_token_missing_the_format_version_claim_is_rejected` for `"v"`) before
being restored. The `cx`/`sub_bind` checks were re-verified the same way
against the FINAL test set (larger than the first pass, after the
test-contract fixes above): disabling `cx` fails exactly 9 tests, disabling
`sub_bind` fails exactly 10 — both counts and every failing test name
confirmed by re-running, not estimated (an earlier draft of this entry stated
7/6 from a narrower, `-k`-filtered first pass and undercounted by one test
each; a `claim-reviewer` pass caught the discrepancy before this landed).
Every mutation restored and the affected files re-verified green (253 tests
across the seven affected files) before commit. Full unit (2145) + security
(466) suites green on the final tree.

**Effort:** M, grew during self-review (F1/F2/test-contract fixes) similarly
to items 148–150's precedent of a mandatory review finding sibling gaps in
the same module. **Depends on:** 92 (shipped), 128 (shipped).

### 152. Sales/landing pages don't reflect items 19 (MySQL) / 134 (WORM retention) shipping ✅ DONE

**Surfaced 2026-08-06 by the `claim-reviewer` audit of items 19/128/134/144.**
`sales/index.html`'s "Do not claim yet" list still named compliance-grade
WORM audit retention and "additional database dialects beyond Postgres/
MSSQL" as not-yet-available, and `landing/security.html` still asserted the
audit sink "is not WORM storage and does not provide built-in retention,
managed search" and listed only Postgres/SQL Server as supported dialects —
both false as of items 19 phase 1 and 134 phase 1. Ran the `pitch-sync`
skill to reconcile every dialect/WORM mention across `sales/index.html`,
`landing/security.html`, `landing/index.html`, and `landing/sandbox.html`
against `docs/business/GO_TO_MARKET.md`'s framing.

**Correction to the item's own premise:** `GO_TO_MARKET.md` was *not*
fully correct going in — item 134's commit had updated its WORM claim
correctly, but item 19's commit never touched it, so its "safe to claim
now" list still read "Postgres and SQL Server support" with no MySQL
mention. Fixed that line too (with the Snowflake/BigQuery-still-open
caveat kept explicit) before treating it as the source of truth. Also
found and fixed the same drift in `README.md`'s opening pitch line
("Postgres or MSSQL database" — item 19's own commit had updated the
architecture-diagram mention further down but missed this one) since it's
a public-facing claim of the same kind, per the item's own instruction to
bounded-grep for it.

**What changed, precisely:**

- `sales/index.html`: qualification "strong fit" list, the "safe to claim
  now" / "do not claim yet" claim-guardrail lists — added MySQL to the
  dialect claim and the disqualifier list (`Postgres/MSSQL` →
  `Postgres/MSSQL/MySQL`), moved WORM retention from "do not claim" to
  "safe to claim now" with the S3 Object Lock mechanism, fail-open
  buffering, and no-managed-search caveat spelled out; "do not claim yet"
  now names only managed search over the WORM archive, not retention
  itself.
- `landing/security.html`: the "Writes are governed" limitation now lists
  Postgres/SQL Server/MySQL; the "Audit durability and search" limitation
  now describes the real `jsonl_chained_s3_worm` backend (Object Lock
  COMPLIANCE mode, composed with the hash-chained ledger, fail-open
  buffering) instead of flatly denying WORM storage exists, while still
  disclaiming managed search and a SIEM UI.
- `landing/index.html`: meta description/og tags, the JSON-LD feature
  list, the hero lede, architecture note, demo-request database dropdown
  (added a MySQL option), FAQ answer, and the alternate JSON copy variant
  block (`hero-lede`, `arch-db-title`, `outcome-work-copy`,
  `footer-copy`) — all updated to name MySQL alongside Postgres/SQL
  Server.
- `landing/sandbox.html`: the demo-to-real handoff line.
- `README.md`: the opening one-line pitch ("Postgres or MSSQL database" →
  "Postgres, MSSQL, or MySQL database").
- `docs/business/GO_TO_MARKET.md`: "safe to claim now" dialect line fixed
  to include MySQL (was missed by item 19's own commit).

**Verified against real code, not just plausible wording**, before
landing any claim: MySQL — `MySQLDialectAdapter`
(`compiler/dialect_adapters.py`), `MySQLSessionAdapter`
(`connections/dialects.py`), `DialectEnum.MYSQL` on `ConnectionProfile`,
`tests/integration/test_mysql_live.py` (real MySQL 8.4). WORM —
`audit/worm_sink.py`'s `S3WormAuditSink`/`WormFlushMonitor`, the
`jsonl_chained_s3_worm` backend registered in `audit/sinks.py`,
`tests/unit/test_audit_worm_sink.py`. The WORM copy keeps the phase-2
caveat explicit everywhere it appears rather than claiming the bare
capability.

**Bounded-grep sweep, not an unbounded hunt** (per the item's own scoping
instruction): searched `sales/`, `landing/`, and `README.md` for
"Postgres"+"SQL Server" co-occurrences and "WORM" mentions; found and
fixed every hit above. Left the HA/Kubernetes "do not claim yet" line in
`sales/index.html` untouched — that's a different item's (56's) drift, out
of this item's scope.

**Effort:** S. **Depends on:** 19 (phase 1 shipped), 134 (phase 1 shipped).

### 153. `CHANGELOG.md` has no `[Unreleased]` entry for items 19 (MySQL) or 134 (WORM retention) ✅ DONE

**Surfaced 2026-08-06 by `claim-reviewer` while auditing item 152's
landing/sales-copy reconciliation.** Both items shipped real, upgrade-relevant
capability (a third registry dialect; an opt-in compliance-grade audit
archival backend) but neither has an `[Unreleased]` entry in `CHANGELOG.md`,
unlike every other item of comparable weight (e.g. items 35 phase 3, 102,
136). This is a documentation gap, not a claim-accuracy defect — item 152
itself was scoped to landing/sales copy and README, not the changelog, so this
was deliberately left for its own item rather than folded in.

**Shipped.** Ran the `release-notes` skill's procedure by hand (source of
truth: `docs/TODO_ARCHIVE.md`'s full write-ups, cross-checked against actual
code and tests). Before writing anything, re-checked how far `CHANGELOG.md`'s
existing `[Unreleased]` section actually reached — it already covered
roughly item 88 and separately items 102/117, but nothing from item 89
onward otherwise, and specifically nothing from items 128–152. Rather than
stopping at the item's own literal item-19/134 ask, swept
`docs/TODO_ARCHIVE.md` item-by-item from 128 through 151 (the range the
gap actually spans) and added a customer-facing entry for every item with
genuine external-behavior impact:

- **Added:** 19 (MySQL dialect), 134 (WORM retention, both phases), 145
  (purpose-bound access), 146 (quickstart CLI), 147 (procurement evidence
  page), 135 (lease-driven credential re-resolution).
- **Changed:** 128 (MCP SDK v2 / `2026-07-28` protocol revision), 144
  (`/metrics` auth-required by default + `verdict()` metrics), 140 (admin
  audit-browser cursor ceiling lowered 1,000,000 → 5,000) — each with an
  explicit **Upgrade impact** callout, since all three are breaking for
  some caller.
- **Fixed:** 136 (chained/WORM audit backends silently disabled four read
  surfaces), 148 (`access_diff` never diffed `column_masks`).
- **Security:** 149+150 (casefold/lower policy-enforcement disagreements,
  including a real "denied-write-column silently inert for a non-lowercase
  table key" bug), 151 (approval token not bound to connection/principal),
  137 (audit read surfaces now verify + disclose hash-chain integrity),
  138+139 (bounded audit-log reads + AST list-size caps), 143
  (`cryptography` CVE upgrade), 129 (MCP `tools/list` caching marked
  non-shared-cacheable).

17 entries total, in customer-facing language (no internal item-N-only
shorthand as the lead sentence), each still citing `TODO.md item N` for
traceability per the file's existing convention. Item 142 (a
threat-model doc-only ID rename) and item 130 (not merged — still open in
`TODO.md`) were checked and correctly excluded.

**Spot-checked against real code and tests before writing**, per this
item's own "be honest" requirement, specifically for items 19 and 134 (the
item's own named ask): confirmed `MySQLDialectAdapter`
(`compiler/dialect_adapters.py`), `ConnectionProfile`'s `mysql` dialect
literal, `AuditSinkBackend.JSONL_CHAINED_S3_WORM`
(`core/config.py`), the `GET /api/v1/admin/observability/worm-search`
route (`api/admin_observability_routes.py`), and every cited test file
(`test_mysql_live.py`, `test_audit_worm_sink.py`, `test_worm_search.py`,
`test_worm_search_api.py`, `test_worm_search_redaction.py`) actually exist
in the shipped tree.

**Deliberately left out, and stated as such rather than silently
expanded:** items 89–127 (including the flagship engine-expressiveness
items 99–106 — window functions, non-equi joins, set operations, CTEs,
subqueries) and items 131/133 are also not yet reflected in
`CHANGELOG.md`. Backfilling that range is a materially larger effort than
this item's own "S" scope and this item's specific 19/134 ask — a
dedicated follow-up, not something to fold in under this item's own
low-risk-docs-task framing.

**Effort:** S (grew to M once the 128–151 sweep was included, per the
task's own instruction to check for other genuinely-missing entries).
**Depends on:** 19 (phase 1 shipped), 134 (phase 1 and 2 shipped).

### 155. `sensitivity_approval_reasons` looks up every table in the query's top-level connection's catalog, never a cross-connection join's own connection ✅ DONE

**Surfaced 2026-08-06 by `security-invariant-reviewer` while auditing item
151.** `execution/approval.py`'s `sensitivity_approval_reasons` (the catalog
`sensitivity: pii` approval trigger, item 92 phase 2) resolved every column it
walks via `store.get_table(connection_id, physical)` using a single
`connection_id` argument — the query's own top-level connection. For a
cross-connection join (`JoinSpec.connection`, gated by policy's `join_group`
rule and already shipped — see `validation/schema_validation.py`'s
`resolve_query_table_connections`), a joined table actually lives in a
*different* connection's catalog. A query joining connection `analytics`'s
`orders` to connection `crm`'s `customers`, where `customers.email` is
labelled `pii` only in `crm`'s catalog, never tripped the approval gate:
`store.get_table("analytics", "customers")` looked in the wrong connection's
catalog, found no entry, and `sensitivity_approval_reasons` silently treated
the joined column as unlabelled. Real and pre-existing (not introduced by
item 151), and separate in scope from it — item 151 stops an *already-minted*
token from being redeemed against the wrong connection; this item stops the
*trigger itself* from being blind to a joined connection's labels in the
first place, so the gate could never even ask for a token when it should
have.

**Shipped: thread schema validation's per-scope connection map through, don't
recompute it.** `validation/schema_validation.py`'s `validate_schema` gained
an optional `scope_connections: Optional[Dict[int, Dict[str, str]]]` keyword,
populated exactly like the existing `scope_tables` out-param: one entry per
scope (the outer query, every set-operation arm, every cte body, every nested
subquery), keyed by `id(scope)`, whose value is that scope's own
`resolve_query_table_connections` result — case-folded onto each table's
effective name so a later case-insensitive lookup matches the way
`effective_name_map` already does. This is the SAME map schema validation
already computed to decide which connection's schema `_load_table` reflects a
cross-connection join's table against (previously computed and discarded
inside the private `_reflect_and_validate_scope` helper on every call); the
call itself was hoisted up into `validate_schema`'s two loops (the cte loop
and the main scope loop) so the result can be captured once and handed to
both `_reflect_and_validate_scope` (as a new `table_connection` keyword,
still falls back to computing it itself when omitted, for callers that don't
need the map) and `scope_connections`, rather than the approval gate
recomputing the same resolution a second time and risking it drifting from
what schema validation actually enforced the `join_group` rule against.

`execution/approval.py`'s `sensitivity_approval_reasons` gained a matching
optional `scope_connections` parameter. For each scope it walks (via the
existing `iter_query_scopes`), it resolves a column's table to
`table_connection.get(table.casefold(), connection_id)` and then — this is the
second-pass fix below — consults that connection AND `connection_id` when
they differ, rather than replacing one with the other. `execution/service.py`'s
`_validate_and_compile` now returns a sixth tuple element (`scope_connections`,
built alongside the existing `scope_tables` local) and `_enforce_approval_gate`
gained a matching optional parameter that it forwards into
`sensitivity_approval_reasons` — wired at `execute()`'s real call site;
`explain()`/`verdict()` (which don't run the approval gate) just discard the
extra tuple element.

**Second pass, same day: replacing the lookup reopened a mirror-image gap.**
A same-day `security-invariant-reviewer` follow-up pass caught that the first
version of this fix simply replaced `connection_id` with the table's resolved
connection — correct for the reported direction (a label living only on the
*joined* connection), but wrong in the opposite direction: connection catalogs
are curated independently (item 32's per-connection scoping —
`import_connection` exists specifically because labels do not propagate
automatically between connections), so an operator may have labelled a table
sensitive under only the connection they curated *first*. Once a query reaches
that same physical table through a different, cross-joined connection, a
strict replacement stops finding that label — the same shape of bug, just on
the other side. Fixed by consulting **both** candidate connections when they
differ and triggering on either hit, matching this module's own documented
fail-closed posture ("denies rather than admits on any ambiguity"): a false
positive against an unrelated same-named table in the other catalog is the
safe direction; a false negative against a genuinely sensitive column is not.
The docstring's original "absent from the map" framing was also imprecise
(caught by `claim-reviewer`) — the outer FROM table is always present in a
real map (seeded with its own connection by `resolve_query_table_connections`)
and a cte reference never reaches the lookup at all (skipped earlier by the
`cte_names` check); only a call site that passes no map (every pre-155 caller)
genuinely collapses to a single lookup. Reworded in the docstring and here.

**Coverage (`tests/unit/test_approval.py`):** a real cross-connection-join
scenario (two `ConnectionProfile`s sharing a `join_group`, `orders` on
`primary` joined to `customers` on `other`, `customers.email` labelled `pii`
ONLY in `other`'s catalog) run through the REAL `validate_schema` (only
`_load_table` patched, per the standard schema-validation test seam) —
`test_cross_connection_join_trips_sensitivity_gate_when_scope_connections_threaded`
asserts the gate now finds the label;
`test_cross_connection_join_never_trips_the_gate_without_the_map` pins the
pre-155 fallback behavior for a call site that passes no map at all (the shape
of every call site before this item);
`test_enforce_approval_gate_uses_the_real_cross_connection_map` proves the
`_enforce_approval_gate` wiring specifically, and that omitting the map does
NOT raise; `test_execute_trips_the_gate_for_a_cross_connection_join_end_to_end`
drives the real public `execute()` entry point end to end (real
`validate_schema`, mocked engine/session only) so a wiring break at
`execute()`'s own call site — invisible to any test that calls
`_enforce_approval_gate` directly — is still caught;
`test_single_connection_sensitivity_gate_unaffected_by_scope_connections_param`
confirms the existing single-connection behavior is byte-identical whether
`scope_connections` is omitted, `None`, or an explicit map that doesn't cover
the table. Two more tests closed gaps a `test-contract-reviewer` pass found in
the first four: every one of them joined `customers` unaliased and all
lowercase, so a mutation swapping the lookup key from the alias/table token to
the physical name, or dropping either `.casefold()` call at the
`scope_connections` population sites, left all of them green.
`test_cross_connection_join_trips_the_gate_through_an_aliased_mixed_case_ref`
joins under alias `Cust` and references it in mixed case, closing that gap; and
`test_cross_connection_join_still_trips_when_the_label_lives_only_on_the_primary_side`
mirrors the original scenario with the label on `primary` instead of `other`,
covering the union-check fix.

**Mutation-verified, both passes, against the FULL test set above (an initial
narrower pass under-counted this before the alias/end-to-end tests existed —
corrected here rather than left stale):** reverting the `approval.py` lookup
to ignore the map entirely, and separately reverting `schema_validation.py`'s
main-loop map population to an empty dict, each independently fail exactly
the same four tests —
`test_cross_connection_join_trips_sensitivity_gate_when_scope_connections_threaded`,
`test_cross_connection_join_trips_the_gate_through_an_aliased_mixed_case_ref`,
`test_enforce_approval_gate_uses_the_real_cross_connection_map`,
`test_execute_trips_the_gate_for_a_cross_connection_join_end_to_end`. Reverting
the union check back to a strict replacement fails exactly
`test_cross_connection_join_still_trips_when_the_label_lives_only_on_the_primary_side`.
Swapping the lookup key from the alias/table token to the physical name, and
separately dropping either `.casefold()` call at the population sites, each
fail exactly `test_cross_connection_join_trips_the_gate_through_an_aliased_
mixed_case_ref`. Every mutation was reverted after confirming the expected
failure.

**Third pass, same day: name which connection actually produced the hit.**
An `architecture-boundary-reviewer` pass on the union-check fix pointed out
that "references pii-labelled column customers.email" reads as if it
describes the physical table the query actually reads, even when the union
check found the label on the OTHER candidate connection — misleading for an
approver reading the reason, or an auditor reading the persisted
`approval.required`/`approval.granted` log line. The reason string now
appends `(connection '<id>')` naming the connection whose catalog entry
produced the hit, but only when there were genuinely two different candidates
— a single-connection query's reason text is unchanged. Mutation-verified:
dropping the suffix unconditionally fails exactly
`test_cross_connection_join_still_trips_when_the_label_lives_only_on_the_primary_side`'s
added assertion (`"connection 'primary'" in reasons[0]`), and no other test.

**Known residual, not closed by this item:** the map lookup is keyed by
`id(scope)` on the assumption that the same `StructuredQuery` object instance
flows unmutated from `_validate_and_compile` (where the map is built) to
`_enforce_approval_gate` (where it's consumed) within one `execute()` call —
true today (verified by reading the call chain), but a future change that
re-parses or deep-copies the AST between those two points would make every
lookup miss silently, collapsing back to single-connection-only behavior
rather than raising. Low severity — no current call site does this — left
unfixed rather than adding a speculative guard against a change that hasn't
happened.

**Effort:** M. **Depends on:** 151 (shipped — same module), cross-connection
joins/`join_group` (shipped).

### 156. A cross-connection join's joined table is governed only by the primary connection's Policy — column masks, mandatory row filters, and deny-lists never apply from the joined connection's own Policy ✅ DONE

**Surfaced 2026-08-06 by `security-invariant-reviewer` while auditing item
155 (pre-existing, not introduced by that item).** `validation/policy_validation.py`'s
`validate_policy` and `compiler/sqlalchemy_compiler.py`'s `compile_structured_query`
each took a single `Policy` — the primary connection's (or the principal's
override of it) — and applied it uniformly to every table in the query,
including a table reached through a cross-connection join
(`JoinSpec.connection`, gated by policy's shared `join_group` rule,
`docs/THREAT_MODEL.md` QG-09). Item 155 fixed the catalog sensitivity-label
*approval trigger* to consult a joined table's own connection's catalog, but
`column_masks`, `mandatory_row_filters`, and the table/column allow-deny list
are a different enforcement path (policy validation + compilation, not the
approval gate) and were out of that item's scope. So before this item: a
column masked under connection B's own `Policy` was NOT masked when read via
a query whose primary connection was A joined to B, unless A's `Policy`
happened to declare the identical mask/filter for B's table — same shape for
a mandatory row filter or a deny-list entry.

**Shipped: a reflection-free sibling of item 155's per-scope connection map,
threaded through both enforcement paths, unioned rather than replaced.**
`validation/schema_validation.py` gained two new shared primitives:

- `resolve_scope_connections(query, connection_id, principal, connection_resolver)`
  — walks every scope (`iter_query_scopes`) and calls the existing
  `resolve_query_table_connections` per scope, exactly like `validate_schema`'s
  own internal loop, but touches **no database** — only the in-memory
  connection registry/policy store. This matters because
  `validation/policy_validation.py`'s own module docstring requires it to
  "run BEFORE `validation/schema_validation.py` reflects anything" — so the
  per-table connection map policy validation needs has to be computable
  *before* schema validation ever runs, not reused from its output. Kept as a
  genuinely separate walk from `validate_schema`'s (rather than factored into
  one shared function both call) because `validate_schema`'s loop also feeds
  each scope's RAW, un-casefolded `table_connection` into
  `_reflect_and_validate_scope`'s `_load_table` schema argument, where exact
  casing is load-bearing; this one only ever needs the case-insensitive
  lookup `sensitivity_approval_reasons` already relies on.
- `resolve_table_policies(table_connection_id, connection_id, policy, principal, connection_resolver)`
  — the shared "which Policy/Policies govern this table" primitive both
  enforcement paths now call: `[policy]` alone when the table resolves to the
  primary connection (the overwhelming common case, and every table in a
  single-connection query — byte-identical to the pre-156 shape), or
  `[policy, other_policy]` — the PRIMARY connection's Policy FIRST, the
  table's own connection SECOND — when they differ. Never a strict
  replacement of one for the other, learned directly from item 155's own
  follow-up correction on the identical mistake for the catalog lookup. (The
  order was corrected same-day from an initial joined-first ordering — see
  "Second pass" below; primary-first is the one that matters, since order is
  observable for a mask.)

`validation/policy_validation.py`'s `validate_policy` gained optional
`principal`/`scope_connections`/`connection_resolver` keywords (every
existing call site — `security_benchmark.py`, `catalog/
adaptive_learning_benchmark.py`, `admin/service.py`'s config simulator —
passes none of them and is unaffected). When supplied, `_validate_scope`
(table/column allow-deny, and the item-49 masked-column-position rule) and
`_validate_correlation` (a correlated ref checked against its PARENT scope's
policy) resolve each table/column's own connection via the map and check it
through `resolve_table_policies`, composed with two new tiny helpers:
`_table_allowed_everywhere`/`_column_allowed_everywhere` (`all()` across
candidates — denied under EITHER policy denies the query, the fail-closed
AND direction for a permission check) and `_column_mask_anywhere` (first
non-`None` mask across candidates, PRIMARY-connection-first — governed
under EITHER policy means the mask applies, but the primary's own mask wins
when both connections configure one). `_validate_cte_constraints`'s
and `_validate_subquery_constraints`'s own separate masked-projection checks
were deliberately left untouched: a cte body or a nested subquery is
structurally forced to stay single-connection
(`_reject_cross_connection_nesting` in schema validation forbids a join
inside either from naming a different connection), so their own table's
resolved connection can never differ from the primary — extending them would
have added surface with no reachable behavior change.

`compiler/sqlalchemy_compiler.py`'s `compile_structured_query` (and every
function in its recursive call graph — `_compile_cte`, `_compile_set_
operation`, `_compile_scope_body`, `_compile_in_subquery`, `_compile_exists`,
carried through `_WhereCtx`) gained the same optional `connection_id`/
`scope_connections`/`connection_resolver` triple, threaded down so each
scope's OWN table-to-connection map (`scope_connections.get(id(scope))`) is
available exactly where it's needed. `_mask_for_select_ref`/`_build_select_
columns` resolve a bare projection's mask through `resolve_table_policies` +
a first-match walk (primary connection first) instead of a single
`policy.column_mask(...)` call. `_apply_mandatory_row_filters` was
restructured from "for each policy-declared filter, find matching table
occurrences" to "for each table occurrence, resolve its candidate policies
and apply every one of THEIR matching filters" — a union across candidates,
not a replacement, and every set-operation arm gets its own resolution since
(unlike a cte body or nested subquery) an arm is allowed to cross-connection
join independently of its sibling arms. `applied_column_masks` (the audit
trail's masked-column reporter) gained the matching optional parameters too,
so a joined-only mask is reported, not just silently applied.
`execution/service.py`'s `_validate_and_compile` computes
`early_scope_connections` via `resolve_scope_connections` before calling
`validate_policy` (preserving the pre-schema-validation ordering), and passes
`validate_schema`'s own `scope_connections` output (already computed for
item 155's sensitivity trigger) into `compile_structured_query` and into the
`applied_column_masks` audit call.

**Coverage:** `tests/unit/test_policy_validation.py` — a cross-connection
`orders`/`customers` join (mirroring item 155's own fixture shape) with a
deny rule, a denied column, or a mask configured ONLY on the joined
connection's Policy, each proven to reject/reject/reject once the map is
threaded in and to silently pass when it's omitted (the pre-156 regression
pin); the mirror case (rule ONLY on the primary connection) for each,
proving the union direction rather than a replacement; a both-connections-
different-rules case; a single-connection regression check.
`tests/unit/test_column_masking.py::TestCrossConnectionMasking` and
`tests/unit/test_compiler.py::TestCrossConnectionMandatoryRowFilters` mirror
the same matrix at the compiled-SQL level (mask rendered/row filter present
in the literal-bound compiled statement). `tests/unit/test_service.py` adds
three end-to-end wiring tests through the real `execute()` entry point (a
real two-connection `ConnectionRegistry`/`PolicyStore`, not a hand-built
resolver): a joined-only deny rejects, neither-policy-objects proceeds
normally, and a joined-only mask is applied to the actual compiled statement
`execute()` runs — closing the "a wiring break at the real call site is
invisible to a test that only calls the helper function directly" gap item
155's own precedent called out.

**Mutation-verified:** flipping `_table_allowed_everywhere`/`_column_allowed_
everywhere` from `all()` to `any()` fails exactly the three "enforced from
X connection" tests for the reason expected (the permissive direction lets a
denied table/column through). Changing `resolve_table_policies` to drop the
primary policy entirely (a strict replacement, the EXACT mistake item 155's
own follow-up had to correct) fails exactly the two "still enforced from the
primary connection" tests — both the policy-validation deny-list one and the
compiler mask/row-filter ones — plus (added same-day, see "Second pass"
below) the correlation-specific pair. Collapsing `_column_mask_anywhere` to
only check the first candidate (`candidates[0]`) passed every existing test
**except** a test written specifically to catch it —
`test_cross_connection_join_masked_column_position_rule_still_enforced_from_
primary_connection`, added during this item's own mutation pass after the
first attempt at this check revealed the gap (a real instance of exactly the
self-review discipline CLAUDE.md's working agreement asks for: the mutation
exposed a genuine missing test, which was then closed before landing rather
than narrated). Breaking the compiler's per-table connection lookup
(`table_cx = table_connection.get(...)`) to always fall back to
`connection_id` fails exactly the two "configured only on the joined
connection" mandatory-row-filter tests. Dropping `early_scope_connections`
from `_validate_and_compile`'s call to `validate_policy` breaks the
end-to-end wiring test (surfaces as a different, later exception — real
schema validation reaching a database reflection that was never meant to
happen — rather than a clean assertion mismatch, which is itself a
confirmation that the early rejection this item adds is what stops execution
from reaching that point at all). Every mutation was reverted after
confirming the expected failure.

**Second pass, same day: a full four-reviewer `auditors` run
(`security-invariant-reviewer`, `architecture-boundary-reviewer`,
`test-contract-reviewer`, `claim-reviewer`) on the shipped diff surfaced four
confirmed defects, fixed before this item was considered complete, plus
several lower-severity findings recorded as follow-ups rather than fixed
under time pressure (items 157/158 below).**

1. **Mask ordering was a real security regression in one untested case, not
   just a documentation imprecision** (`security-invariant-reviewer`). The
   first-shipped `resolve_table_policies` ordered the JOINED connection's
   Policy first — mirroring item 155's own ordering by analogy, but item
   155's sensitivity trigger has OR/trigger semantics where order is
   cosmetic (either candidate matching produces the same outcome), while a
   mask caller PICKS one concrete answer from the first candidate with an
   opinion, where order is NOT cosmetic. Concretely: if the primary
   connection masks a column with `kind: null` and the joined connection
   independently masks the SAME column with `kind: last, length: 4`,
   joined-first ordering applies the WEAKER `last` mask — strictly worse
   protection than the pre-156 default, which only ever consulted the
   primary and would have applied `null`. No test exercised this case (every
   existing test had only ONE side configuring a real mask). Fixed:
   `resolve_table_policies` now returns `[policy, other_policy]` —
   PRIMARY-first — so the primary's own mask wins whenever it has one,
   falling through to the joined connection's mask only when the primary has
   none. Allow-checks (`all()`) and mandatory-row-filter union
   (apply-every-match) are order-independent, so this reordering changes
   ONLY mask resolution. New test:
   `test_when_both_connections_mask_the_same_column_differently_the_primary_
   wins` (`test_column_masking.py`) — mutation-verified: reverting the order
   to joined-first fails exactly this test.
2. **`admin/service.py`'s `simulate_candidate_policy` (the `/admin/config/
   simulate` policy-preview endpoint) was left calling `validate_policy`
   without the new map** (`architecture-boundary-reviewer` and
   `security-invariant-reviewer`, independently, same finding). Before this
   item, primary-only was consistent with production; after it, production
   was correct but the simulator became the ONE place still stuck on the old
   behavior — a NEW divergence this item itself created, not an old one it
   inherited. An operator staging a candidate policy that adds a deny/mask/
   filter rule to a JOINED connection's Policy would get an `allow` preview
   for a query real execution would reject or filter. Fixed: `simulate_
   candidate_policy` now computes its own `scope_connections` via
   `resolve_scope_connections` (using the simulator's existing isolated
   `candidate_resolver`, not the live registry) and threads it plus
   `connection_resolver=candidate_resolver` into `validate_policy`; a
   connection-resolution failure here is swallowed so the pre-existing
   dedicated `query_connection_denied` check (a few lines below) keeps
   owning that error shape. The mandatory-filter readiness report was
   similarly walking only the request's own connection's
   `mandatory_row_filters` — extended to also walk each distinct joined
   connection's own filters (deduplicated by object identity, matching
   `_apply_mandatory_row_filters`'s own dedup), so a missing claim on a
   JOINED connection's filter is reported instead of silently omitted. A
   related gap found during this same fix (not by a reviewer, by re-reading
   the diff against the existing "do not enumerate mandatory-filter
   identifiers for a table the target policy itself hides" guard a few lines
   below): that guard only ever checked the PRIMARY connection's `Policy`, so
   a table denied only by the joined connection's own Policy would still
   have leaked that same connection's filter column name into the report,
   even though the query as a whole was already reported `deny` for touching
   that table. Fixed by checking table visibility against every candidate
   Policy in play (the same list gathered for the filter walk itself), not
   just the primary's — coarser than per-occurrence connection attribution
   (a table reached through more than one connection in a multi-way join is
   hidden if ANY candidate denies it) but the same fail-closed direction the
   existing single-connection guard already takes. Four new tests in
   `test_admin_service.py`
   (`test_candidate_simulation_denies_a_cross_connection_query_the_joined_
   policy_denies`, its neither-side-objects mutation guard,
   `test_candidate_simulation_reports_a_mandatory_filter_declared_only_on_
   the_joined_connection`, and `test_candidate_simulation_does_not_reveal_a_
   joined_connections_filter_for_a_table_it_denies`), each mutation-verified.
3. **`_validate_correlation`'s own item-156 change shipped with zero test
   coverage, and its "checked twice" docstring claim doesn't hold for the
   cross-connection case** (`test-contract-reviewer`). A correlated reference
   into a cross-connection-joined outer table is resolved by
   `_validate_correlation` against the PARENT scope's own `table_connection`
   map — but the docstring's claimed redundant backstop (`_validate_scope`
   walking the CHILD subquery's own refs) can't actually provide that
   backstop for a cross-connection table: the outer alias is never declared
   in the child's own from/join, so the child's own `table_connection` lookup
   always falls back to `connection_id` (the primary), regardless of where
   the table really lives. `_validate_correlation`'s own resolution is
   therefore the SOLE enforcement point for this case. Fixed by adding direct
   coverage: `tests/unit/test_policy_validation.py` gained a `_cross_
   connection_correlation_query` fixture (an EXISTS subquery correlating on
   the cross-connection-joined table's column) and four tests — joined-only
   deny, the no-map regression pin, joined-only mask, and the primary-only
   mutation guard — the mask one mutation-verified to fail without `_validate_
   correlation`'s own resolution (the deny-list ones turn out to be
   independently covered too, since a correlate ref's table must also be a
   genuine join in the parent scope, which `_validate_scope`'s own per-scope
   check already catches — expected and harmless redundancy, not a gap).
4. **A weak/tautological assertion** in the joined-only-mask regression pin
   (`test-contract-reviewer`): the second assertion accepted `"phone" in
   [c.name for c in stmt.selected_columns]`, true for both the raw AND the
   masked rendering (the output column is named `"phone"` either way), so it
   caught nothing a mutation could actually fail. Replaced with `assert
   "customers.phone" in compiled` — a masked bare projection renders as
   `NULL AS phone` with no `customers.phone` reference anywhere in the
   statement, so this genuinely distinguishes the two.

Also corrected in this pass: `docs/PRODUCT_GUIDE.md`'s Decision Log entry
said row-filter composition was "first-match" like masks — it is not; every
matching filter on every candidate is applied (a union, not a pick-one), and
the entry now says so explicitly for each of the three rules separately
(`claim-reviewer`).

**Full unit (2225) and security (472) suites green** after the full pass
(including the post-audit fixes above), `poetry run black` clean.

**Known residual, not closed by this item — recorded as follow-up items:**

- `_validate_cte_constraints`'s and `_validate_subquery_constraints`'s own
  masked-projection checks stay primary-policy-only (a cte/subquery scope
  cannot itself cross-connection join, so their table's resolved connection
  can never actually differ from the primary — confirmed by reading
  `_reject_cross_connection_nesting`, not just asserted).
- **TODO.md item 159** (new, unrelated pre-existing bug the same audit pass
  found): schema reflection's `table_connection` lookup in
  `_reflect_and_validate_scope` is case-sensitive and keyed by a
  hash-ordered `set`, so a join whose alias casing differs from the casing
  used in its own column refs can silently reflect (and, per this item,
  resolve masks/filters/deny-list for) the wrong connection. Item 156 never
  touches this code path — pre-existing, filed separately.
- **TODO.md item 160** (new): four smaller connection-resolution edge cases
  in item 156's own design — an audit-vs-compiled-SQL Policy snapshot race
  under a concurrent authorized config reload, a cap-check ordering
  inversion (the new map computation now runs before the cheap structural
  caps), a fail-open-by-default parameter shape (every new keyword defaults
  to the pre-156 behavior, which is exactly how finding 2 above happened and
  could happen again at a different call site), and confirmation that
  purpose narrowing / `min_group_size` / row limits are NOT part of this
  item's cross-connection resolution (never claimed to be — recorded so it
  isn't silently assumed).

**Effort:** M. **Depends on:** cross-connection joins/`join_group` (shipped),
155 (shipped — same map-computation precedent, reused via a reflection-free
sibling rather than the original).

### 158. `ConnectionProfile` never validates that `dialect` agrees with `connection_string`'s actual backend ✅ DONE

Surfaced 2026-08-06 by the `security-invariant-reviewer` audit of item 19
phase 2 (Snowflake), but the gap is pre-existing and dialect-agnostic — it
predates Snowflake and affects Postgres/MSSQL/MySQL exactly the same way.

**The gap.** Nothing anywhere validated that `ConnectionProfile.dialect`
(the declared enum value) matched the actual backend named in
`connection_string`'s URL scheme. A profile with `dialect: postgresql` and
`connection_string: mysql+asyncmy://user:pass@host/db` (or any other
mismatched pair) was accepted by Pydantic and reached
`connections/engine.py`'s `init_engine` untouched, and every place that
branches on the *declared* dialect rather than the live engine would act on
the wrong assumption: `session_scope` (`engine.py`) dispatches
`apply_session_guardrails` on the declared dialect, so a Postgres-declared
profile pointed at a real MySQL server would send `SET LOCAL
statement_timeout` to a MySQL connection, which does not understand that
statement — the session guardrail silently fails to apply (`create_async_
engine` still resolves the actual driver from the URL, so the wire protocol
is always correct — only the guardrail is not). More concretely security-
relevant: `schema/reflection.py`'s `list_live_tables()` selects its extra
`INFORMATION_SCHEMA.TABLES` filter via `SessionDialectAdapter.
list_live_tables_extra_filter_sql()`, also keyed on the declared dialect —
MySQL's adapter appends `AND TABLE_SCHEMA = DATABASE()` specifically
because MySQL's `INFORMATION_SCHEMA.TABLES` is server-wide (spans every
database the connecting user can see), unlike Postgres/MSSQL. A profile
misdeclared `postgresql` over a real MySQL backend would skip that filter
(Postgres's adapter contributes none), letting `list_tables()` leak
*other* databases' table names to a caller whose connection was only
supposed to see one — this item closes that specific cross-database leak,
not just the guardrail-text mismatch. It also meant item 19 phase 2's
`SessionDialectAdapter.is_connectable()` Snowflake guard (which keys on the
*declared* dialect) couldn't be trusted to catch a `dialect: postgresql`
profile pointed at a real `snowflake://` URL.

**Shipped: a `field_validator("dialect")` on `ConnectionProfile`**
(`connections/models.py`), alongside the existing `_valid_id` validator,
that parses `connection_string` with `sa.engine.url.make_url(...)
.get_backend_name()` and rejects a mismatch against `dialect` with a clear
message naming both the declared dialect and the actual backend found —
closing both the general session-guardrail-mismatch case and the Snowflake
`is_connectable()` case in one place, rather than teaching `init_engine` a
second, narrower parse-based check. `get_backend_name()` was verified
directly (not assumed) to return exactly `DatabaseDialect`'s own spelling
for every driver combination this codebase uses:
`postgresql+asyncpg`/`mssql+aioodbc`/`mysql+asyncmy`/`mysql+aiomysql`/
`snowflake` all map cleanly, via an explicit `_BACKEND_NAME_TO_DIALECT`
lookup table (not `DatabaseDialect(backend_name)` directly, so an
SQLAlchemy-recognized backend QueryGate doesn't support, e.g. `sqlite` or
`oracle`, gets this module's own clear rejection instead of a bare enum
`ValueError`).

**A credential leak was caught and fixed during implementation, before
landing — not after.** The first draft used a whole-model
`model_validator(mode="after")`. Verified directly against a live
`pydantic.ValidationError`: when a `ValueError` is raised from a
model-level validator, pydantic populates the error's `input_value` (both
`str(ValidationError)` and `.errors()[0]["input"]`) with the **entire raw
input dict** — including `connection_string`, credential and all. This
class's own docstring is explicit that `connection_string` must never end
up "embedded in a validation error," and `api/routes.py`'s
`reload_config_endpoint` does `except Exception as exc: raise
HTTPException(..., detail=str(exc))` — a concrete path that would have
handed a real credential straight back in an HTTP 400 body the first time
an admin fat-fingered a reload with a mismatched dialect. Confirmed the same
leak happens even scoping to `field_validator("connection_string")`, since
that field's own raw value *is* the credential. The fix: reorder
`ConnectionProfile`'s fields so `connection_string` is declared before
`dialect`, and scope the validator to `field_validator("dialect")` instead,
reading `connection_string` via `info.data` (already-validated, since it's
declared earlier) — `dialect`'s own value (e.g. `"postgresql"`) is what ends
up in `input_value` on rejection, never the connection string. Verified
directly against a live `ValidationError` again after the fix, with a
credential marker planted in the connection string, to confirm it doesn't
appear in `str(exc)` or any `.errors()` entry.

**A second real-usage gap was caught by running the full test suite, not
assumed away.** The item's own write-up (and a first-draft assumption) held
that "the URL scheme is always literal, never templated," so a validator
could safely reject anything `make_url` can't parse. Two real integration
tests failed on that assumption:
`test_mcp_server.py::test_mcp_configuration_inspection_uses_request_application_config`
and
`test_product_guide_api.py::test_configuration_summary_is_scope_gated_and_never_returns_raw_yaml`.
Both exercise `help/service.py`'s `_redacted_connection`, which
deliberately constructs a `ConnectionProfile` straight from a stored config
version's raw, never-interpolated YAML text (for an admin summary that only
needs typed `dialect`/`id`/`known_tables`/`join_group` access — it never
resolves or needs the real secret). `examples/connections.example.yaml`
(the default `AppConfig.connections_file`) confirmed the *whole*
`connection_string` value, not just the credential portion, is commonly a
single `${QUERYGATE_DEMO_DB_URL}`-shaped reference with no literal scheme
at all. Fixed by having the validator skip the check (not reject) when
`make_url` raises `ArgumentError` — there's no basis to assert a mismatch
against a string with no parseable backend, and this isn't a live bypass:
the one path that actually opens a connection
(`ConnectionRegistry.from_entries`) always interpolates `connection_string`
before constructing `ConnectionProfile`, so the real check still runs
against the fully-resolved URL by the time a connection is ever opened.

**Two more real gaps caught by the post-implementation `auditors` review, both
fixed before landing.** The `architecture-boundary-reviewer` found: (1)
`make_url()` doesn't only raise `sa.exc.ArgumentError` on an unparseable
string — a *templated port* (`...@${DB_HOST}:${DB_PORT}/...`, the same
"still templated" shape as a templated user/pass/host, just further along
in `make_url`'s parse) raises a plain `ValueError` while coercing the port
to an int, confirmed directly (`invalid literal for int() with base 10:
'${DB_PORT}'`), which the validator's original `except sa.exc.ArgumentError:`
didn't catch — so a genuinely-matching, legitimately-templated profile
would have been incorrectly rejected. Fixed by widening the except clause
to `except (sa.exc.ArgumentError, ValueError):`, both treated identically
(skip, don't reject) since neither yields a backend to compare against. (2)
`_BACKEND_NAME_TO_DIALECT` is hand-maintained, deliberately not derived
from `DatabaseDialect` (so an unsupported-but-SQLAlchemy-known backend like
`sqlite` gets this module's own clear rejection instead of a bare enum
`ValueError`) — but nothing forced it to stay a superset of the enum, so a
fifth `DatabaseDialect` member added later without a matching entry here
would silently mis-reject every profile for that new dialect. Fixed with a
module-level `assert set(DatabaseDialect) <= set(_BACKEND_NAME_TO_DIALECT.
values())` so that failure mode is loud (import-time) instead of silent
(request-time). The same review also flagged (informational, no fix
needed) that the field-reordering `connection_string`-before-`dialect`
dependency, while real, is already adequately covered indirectly by the
existing mismatch tests, which would fail (just without saying why) if a
future reorder broke it.

A separate `test-contract-reviewer` pass on the same diff found one more
real gap: every test up to that point exercised `ConnectionProfile`
directly with already-resolved literal strings, never through
`ConnectionRegistry.from_entries` — so the write-up's own central claim
("the one path that actually opens a connection always interpolates
`connection_string` before constructing `ConnectionProfile`, so this check
still runs against the fully-resolved URL") was asserted but never pinned
by a test at the registry level. Fixed by adding
`test_from_entries_rejects_a_dialect_mismatch_only_visible_after_interpolation`
to `tests/unit/test_connections_registry.py`, which calls `from_entries`
with a declared dialect that only disagrees with the connection string
*after* `${MYSQL_URL}`-style interpolation resolves it — the one shape that
can only be caught by validating post-interpolation, exactly the order
`from_entries` uses.

**Coverage:** `tests/unit/test_connections_models.py` (new, 18 tests) —
matching profiles accepted across all four dialects' real driver schemes;
every pairwise mismatch across the four dialects rejected with both dialect
names named in the message; a partially-templated connection string
(scheme literal, credentials/host/db templated) still validates and still
catches a mismatch; a templated port validates too (the fix above); a
wholly-templated (`${VAR}`-only) and a garbage non-URL connection string
are both left unrejected (the real-usage cases above); an SQLAlchemy-known
but QueryGate-unsupported backend (`sqlite`) is rejected even though it
parses; the `_BACKEND_NAME_TO_DIALECT`/`DatabaseDialect` drift guard; and a
dedicated credential-leak test that plants a marker string in the
connection string and asserts it never appears in `str(ValidationError)`
or any `.errors()` entry. `tests/unit/test_connections_registry.py` gained
the post-interpolation registry-level test above (10 tests total in that
file). `tests/unit/test_credential_redaction.py`'s existing suite still
passes unmodified. Mutation-verified: disabling the mismatch check,
disabling the unsupported-backend check, narrowing the except clause back
to `sa.exc.ArgumentError` alone, and removing an entry from
`_BACKEND_NAME_TO_DIALECT` each caused the corresponding test to fail for
the right reason, then all four were restored. Full unit (2295), non-
`real_db` integration, and security suites all green after every fix; no
existing `ConnectionProfile` fixture anywhere in the repo needed weakening.
`make release-check`'s deterministic steps (formatting, full default suite,
config validation, catalog benchmarks, packaging, SBOM) and `make
release-smoke` (real container against real Postgres — structured query,
window function, non-equi join, governed write) both passed against the
final tree; `make release-check`'s `sast` step reports one pre-existing
bandit finding in `execution/approval.py`/`schema/reflection.py`
(unrelated files, confirmed identical on the pre-158 base commit — not
introduced by this item).

**Effort:** S–M (one validator + tests across all four dialects' URL
schemes; no runtime architecture change). **Depends on:** none — buildable
independently of item 157.
