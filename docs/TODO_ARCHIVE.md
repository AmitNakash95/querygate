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
