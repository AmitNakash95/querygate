# TODO: QueryGate → production-grade

Action items to take QueryGate from "working, tested prototype" to a
ready-to-ship commercial product. Grouped by priority. Each item explains
*why* it matters, not just what to build — treat the "why" as the acceptance
criteria. Items marked with a file path point at where the current
(incomplete) implementation lives.

Cross-reference: `README.md` → "Current limitations" covers the active gaps
more briefly; historical extraction reports live under `archive/extraction/`.
This file is the actionable breakdown.

**Completed items are stubs here.** Each fully-shipped ✅ DONE item keeps its heading, a one-line summary of what shipped, and a pointer; the full write-up (Shipped / Coverage / Decision Log / Why it matters) lives in [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md), keyed by the same item number. This keeps routine reads cheap while preserving every "item N" cross-reference in the repo. Open and partially-done items (e.g. `✅ DONE (phase 1)`) keep their full body inline below.

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
| 30 | ✅ Distribution, SBOM, and signed release artifacts (phase 1: SBOM + audit; phase 2: GHCR publish + cosign + SLSA provenance shipped — first executed release + Python package-index remain maintainer-gated) | M | 4, 14 |
| 31 | ✅ Admin UI / policy designer | XL | 25 |
| 32 | ✅ Governed adaptive semantic memory for agents (32A ✅; 32B ✅; 32C ✅) | XL | 23, 25, 27, 28 |
| 33 | ✅ Permission-aware QueryGate product guide and configuration assistant | M–L | 8, 10, 21, 22, 25 |
| 34 | ✅ Interactive mocked HTML product sandbox | M | — |
| 35 | ✅ Agent-visible capacity waiting, progress, and cancellation (phase 1: caller-tunable queue_mode/wait_timeout_seconds, admission id, metrics/audit; phase 2: queue-depth caps + Redis-backed cross-replica admission state; phase 3: progress notifications, REST 202+cancel, mid-queue cancellation, 429 evaluation not started) | L | 9, 12, 15, 20 |
| 36 | ✅ Extensive production-grade QA project / edge-case test suite (phase 1: policy-cap boundary tests + Hypothesis property-based compiler fuzzing; phase 2a: REST/MCP malformed-input fuzzing; phase 2b: cross-dialect differential tests not started) | L | 15, 28 |
| 37 | ✅ Automated end-to-end proof of adaptive semantic learning | M–L | 23, 25, 27, 28, 32B, 32C |
| 38 | ✅ Admin UI catalog-governance workspace (phase 1: core review/approve/reject/publish/rollback loop; phase 2: bulk ops, export/import UI, generation triggers not started) | L | 27, 31, 32B |
| 39 | ✅ Draft-aware policy simulation before staging | M–L | 6, 17, 25, 31 |
| 40 | ✅ Semantic access diff for config changes (phase 1: connection-baseline diff + REST; phase 2: per-principal resolution not started) | L | 6, 25, 31, 39 |
| 41 | ✅ Policy-change blast-radius analysis (phase 1: bounded synchronous aggregation + ranking; phase 2: async/paginated evaluation for very large principal counts not started) | M–L | 22, 25, 31, 40 |
| 42 | Four-eyes config approval and separation of duties | XL | 10, 23, 25, 31 |
| 43 | ✅ Admin connection-operations and health workspace (phase 1: admin connection-status API; phase 2a: "test now" probe; phase 2b: browser workspace) | L | 7, 12, 31 |
| 44 | ✅ Admin observability and rejection-trend dashboard (phase 1: admin-scoped aggregated overview API + read-only browser cards panel; phase 2: time-window charts, config/catalog-change trend, external metrics backend not started) | L | 12, 23, 31, 35 |
| 45 | ✅ Dedicated non-admin "My access" portal (phase 1: identity, guardrails, mandatory-filter readiness, schema browser; phase 2: personal denial history not started) | M | 22, 31, 33 |
| 46 | ✅ Validated policy templates and safe-start presets | M | 17, 25, 31, 39 |
| 47 | ✅ Safe draft recovery plus config export/import UX (phase 1: change-set export/import + policy-only local recovery; phase 2: server-side encrypted draft store not started) | M | 13, 25, 31 |
| 48 | ✅ Pre-defined, admin-approved query templates ("Toolbox"-style curated tools) (phase 1: file-configured invocable templates + REST/MCP; phase 2: governed authoring via the config-versioning plane) | L | 6, 22, 25, 32B |
| 49 | ✅ Column-value masking/tokenization (not just allow/deny) | L | 6, 27 |
| 50 | ✅ Per-principal rate limits / query quotas over time (phase 1: in-process rolling-window request/byte quota; phase 2: Redis-backed cross-replica quota not started) | M | 9, 25 |
| 51 | ✅ Typed client-side query-builder SDK (phase 1: Python builder; phase 2: TypeScript + standalone dependency-light distribution not started) | M (per language) | 20 |
| 52 | ✅ Multi-framework agent integration examples (LangChain, LlamaIndex, OpenAI) | S (per framework) | 20 |
| 53 | Independent third-party security audit + published report | S* | 28 |
| 54 | Compliance control mapping (SOC 2 / ISO 27001 readiness) | L | 23, 25, 28 |
| 55 | ✅ Inference/transitive-exposure adversarial test suite | M | 28 |
| 56 | HA / multi-region reference deployment + DR runbook | L | 29 |
| 57 | Pluggable dialect-adapter architecture | L | 2, 19 |
| 58 | Published adversarial benchmark vs. raw-SQL agent and Google Toolbox | M | 28, 36 |
| 59 | ✅ Read-only behavioral anomaly surfacing on the audit stream | M | 32C, 44 |
| 60 | Bug bounty / responsible disclosure program | S | 53 |
| 61 | ✅ Deduplicate the StructuredQuery JSON Schema across execute/explain/batch tools | S–M | — |
| 62 | ✅ Consolidate redundant instructional prose into one source of truth | M | 61 (pairs well) |
| 63 | ✅ Scope-gate admin-only tool schemas out of non-admin sessions | M | 8, 22 |
| 64 | ✅ Make full catalog provenance opt-in on describe_table/search_catalog | S–M | 27 |
| 65 | ✅ Add a response-size cap to get_querygate_guide_topic | XS–S | — |
| 66 | ✅ CI/test guardrail on total MCP schema+instructions size | S | 61, 62, 63, 64, 65 |
| 67 | ✅ Restore StructuredQuery field descriptions items 62/64/65 assumed existed | XS | 62, 64, 65 |
| 68 | ✅ WHERE/HAVING resource-exhaustion guardrail caps | S | — |
| 69 | ✅ DISTINCT / COUNT(DISTINCT) | S | — |
| 70 | ✅ Table aliases and self-joins | S | — |
| 71 | ✅ NOT groups and column-to-column WHERE comparisons | S | — |
| 72 | ✅ Whitelisted scalar functions and CASE in select (SELECT-only) | S–M | — |
| 73 | ✅ `DialectAdapter` abstraction (compiler-scoped slice of item 57) | S | 57 |
| 74 | ✅ NULLS FIRST/LAST ordering | XS | 73 |
| 75 | ✅ `stddev`/`variance` aggregate functions | XS | 73 |
| 76 | ✅ Composite (multi-column) join keys | XS | 70 |
| 77 | ✅ Scalar functions in WHERE/HAVING predicates (`Predicate.col_fn`) | S | 72 |
| 78 | ✅ Cross-dialect rendering verification pass for items 68–77 | S | 68–77 |
| 79 | ✅ Extend the property-based fuzzer to the item 68–77 AST surface | S | 36, 68–77 |
| 80 | ✅ `string_agg` aggregate function | S | 73 |
| 81 | ✅ `array_agg` aggregate function | S | 73, 80 |
| 82 | ✅ `percentile_cont` aggregate function | S | 73 |
| 83 | ✅ Query-template authoring UX (slot self-consistency, dry-run, live-schema check) | M | 48 |
| 84 | ✅ Structured catalog authoring UI (human-curated entries via governance queue) | M | 27, 31, 32B |
| 85 | ✅ Domain-separated admin UI (Policy / Catalog / Templates / Connections / Releases) | S–M | 31, 38 |
| 86 | ✅ MCP transport request-body size/depth guard | S | 36 |
| 87 | ✅ Extend structured authoring to Policy and Templates | M | 31, 46, 48, 85 |
| 88 | ✅ Minimum aggregation group size (k-anonymity guardrail) | S–M | 55 |
| 89 | ✅ Open-source security validation gates + trust posture (phase 1 ✅; phase 2 signed delivery not started) | L | 28, 30 |
| 90 | ✅ Delegated agent identity (on-behalf-of) into policy + dual-identity audit | M | 8, 10, 23 |
| 91 | ✅ Tamper-evident hash-chained audit ledger + per-query compliance receipts | M | 23 |
| 92 | In-query human-in-the-loop approval for sensitive/expensive reads (MCP elicitation step-up) | L | 26, 90, 91 |
| 93 | Governed Writes — structured, bounded, previewable, reversible agent mutations (decision-gated) | XL | 25, 48, 90, 91 |
| 94 | Verify/enable prepared-statement plan reuse for template execution | S | 48 |

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

SQL is now always compiled with bind placeholders for audit logging and `explain_structured_query`; parameter values are redacted (`<redacted>`) by default. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 1).

### 2. MSSQL support is untested against a real server ✅ DONE

`tests/integration/test_mssql_live.py` (12 tests) + its setup companion `tests/integration/setup_mssql_test_db.py`, run against a real MSSQL 2022 container. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 2).

### 3. Query timeout is a connect-time setting, not a guaranteed cancellation ✅ DONE

`tests/integration/test_postgres_timeout.py`, run against a real Postgres (`make compose-up` + `make test-postgres-live`, or `pytest -m postgres_live`; wired into CI as a… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 3).

### 4. No CI pipeline ✅ DONE

`.github/workflows/ci.yml` — a `test` job (`poetry install`, `black --check`, full `pytest` suite, `querygate-validate-config` against the example configs) and a `docker` job… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 4).

### 21. Principal policy must apply to every MCP/config surface ✅ DONE

MCP schema tools now build `StructuredQueryService` with the authenticated MCP caller (`get_mcp_caller()`), so `list_tables` and `describe_table` return the same… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 21).

## P1 — needed before general availability

### 5. Policy and connections can't be updated without a process restart ✅ DONE

`querygate/config_reload.py`'s `reload_config()` rebuilds the registry and policy store from disk and swaps them in — a single reference assignment… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 5).

### 6. Per-principal policy doesn't exist — only per-connection ✅ DONE

`policy.yaml` gained an optional `principals:` section, keyed by `Principal.subject`, then by connection id (or `"*"` for every connection) — merged on top of whichever of… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 6).

### 7. Health/readiness checks are fake ✅ DONE

`querygate/health.py`'s `HealthMonitor` pings every enabled connection (`SELECT 1`) on a background interval (`AppConfig.health_check_interval_seconds`, default 30s) and caches… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 7).

### 8. `Principal` is just a bearer-token subject string — no scopes/claims ✅ DONE

`Principal` now carries `scopes: frozenset[str]` and `claims: Mapping[str, Any]`, threaded through `ApiKeyAuthenticator` (via new… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 8).

### 9. Concurrency limiter doesn't work across multiple instances ✅ DONE

built the distributed version, not just the documentation minimum bar. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 9).

### 10. No OAuth/JWT — only static API keys ✅ DONE

`core/jwt_auth.py`'s `JwtAuthenticator` verifies a bearer token's signature against a JWKS endpoint (`jwt.PyJWKClient`, in-process cache, default 5-minute lifespan — the JWKS… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 10).

### 11. Result size isn't actually bounded — only row *count* is ✅ DONE

Added `Policy.max_response_bytes` (default 10MB). **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 11).

### 22. Principal-aware connection/tool visibility ✅ DONE

Added one shared visibility rule in `connections/visibility.py`: a connection is reachable only when both its deployment profile and the caller's resolved principal policy have… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 22).

### 23. Persisted audit/event sink ✅ DONE

Added a versioned `AuditEvent` schema and pluggable `AuditSink` interface under `querygate/audit/`, with an append-only JSONL implementation enabled by `.env.example`. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 23).

### 24. Release hygiene and reproducible v0.1.0 cut ✅ DONE

QueryGate now has one repeatable source/package gate (`make release-check`) and one isolated container/infrastructure gate (`make release-smoke`). **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 24).

### 25. Admin/config governance plane ✅ DONE

A REST-only admin API (`/api/v1/admin/config/*`, `api/admin_config_routes.py`) layered on top of the existing hot-reload mechanism (item 5) — never a parallel implementation of it. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 25).

### 95. Discoverable scope catalog + recommended role bundles for IdP integration ✅ DONE

Shipped both parts: (1) RFC 9728 `scopes_supported` (`mcp/oauth_metadata.py`)
now advertises the **entire** vocabulary (`core/scopes.py`'s `ALL_SCOPES`,
unioned with any custom `mcp_required_scopes`), kept distinct from the unchanged
required-scope access gate; (2) `core/scopes.py` gained a structured
`SCOPE_CATALOG` + advisory `ROLE_BUNDLES` (Analyst/Operator/Config Governor/
Catalog Author/Catalog Admin/Catalog Data Steward), from which
`querygate-scope-catalog` (`make scope-catalog`) generates `docs/SCOPE_CATALOG.md`
— drift-tested so it can't diverge, and completeness-tested so every scope
constant is catalogued and covered by a bundle. No auth-model change; data-access
grants stay in `policy.yaml` keyed by `sub`/claim (an Analyst carries no scope).

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 95).

## P2 — hardening and scale

### 12. No metrics/observability beyond structured logs ✅ DONE

`querygate/metrics.py`, exposed via `GET /metrics` (Prometheus text format, own `CollectorRegistry` — not the global default). **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 12).

### 13. No secrets-manager integration for connection strings ✅ DONE

A new `querygate/secrets/` module defines a `SecretResolver` protocol — one method, `resolve(reference: str) -> str` — mirroring the `Authenticator` (`core/auth.py`) and… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 13).

### 14. Docker image has never been built or run in this session ✅ DONE

the estimate's own warning was correct — the MSSQL ODBC apt-install step had rotted (`python:3.11-slim` floated to Debian 13/ trixie; Microsoft's trixie apt repo is signed with… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 14).

### 15. No load/soak testing of the concurrency and timeout guardrails ✅ DONE

`tests/integration/test_postgres_load_guardrails.py` is a repeatable asyncio/HTTPX load harness that drives simultaneous structured queries through the real REST application,… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 15).

### 16. Column-level policy is case-sensitive and untested cross-dialect ✅ DONE

`Policy.column_allowed`'s `denied_columns`/`allowed_columns` dict lookups are now case-insensitive (`Policy._ci_lookup` in `policy/models.py`) — `table_allowed` and the… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 16).

### 17. No policy/connections file validation tooling ✅ DONE

`querygate-validate-config` (registered as a poetry script, `make validate-config`) loads both files through the same Pydantic validation the app uses at runtime and… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 17).

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

A new `querygate/catalog/` module (`models.py` + `loader.py`) mirroring the existing `policy/` module's shape: an optional, versioned, YAML-file-configured overlay… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 27).

### 28. Threat model + adversarial security test suite ✅ DONE

Added `docs/THREAT_MODEL.md`, covering assets, trust boundaries, attacker capabilities, twelve concrete threat classes, implemented controls, deployment requirements,… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 28).

### 29. Production deployment reference stack ✅ DONE

A `deploy/` directory with two verified reference stacks — a production-ish Docker Compose file and a Helm chart — sharing the same shape: app + Redis (distributed concurrency,… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 29).

### 30. Distribution, SBOM, and signed release artifacts

**Phase 1 (SBOM + dependency audit + checksums) ✅ DONE.** **Phase 2 —
signing + provenance mechanism now shipped; two operational bits remain
maintainer-gated (first executed signed release + Python package-index).** The
container-image half of "signed release artifacts" is implemented:
`.github/workflows/release.yml` pushes the image to GHCR, signs it with cosign
keyless (Sigstore), and attaches a SLSA build-provenance attestation
(`actions/attest-build-provenance`), both bound to the image digest and
consumer-verifiable (`cosign verify` / `gh attestation verify` — see
`docs/RELEASING.md`). Offline artifact integrity is checkable with `make
verify-release` (`scripts/verify_release.py`) against `dist/SHA256SUMS`. What
stays open (why this is not fully `✅ DONE`): (a) the *first* signed+attested
release is only produced when a maintainer deliberately pushes a version tag —
publishing is never automatic; (b) **Python package-index (PyPI/private)
publishing** has no index chosen yet — the wheel/sdist are verified by
`SHA256SUMS` until one is. Both are maintainer decisions, not code work.

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

This originally surfaced 13 real advisories across 5 production-reachable
dependencies — `click`, `idna`, `mcp`, `python-dotenv`, `starlette` — each
reviewed and temporarily allowlisted with a specific non-applicability reason.
**Phase 2's dependency-remediation half is now done (under item 89):** all of
those were fixed by upgrading to patched versions (fastapi 0.115→0.139 +
starlette 0.46→1.3.1, mcp 1.12→1.28.1, python-dotenv/click/idna), so the
`security/dependency-audit-allowlist.json` is now **empty** — findings fixed,
not accepted. `setuptools`/`pip`/`wheel` findings were previously excluded as
`ensurepip` bootstrap packages; they are now also **stripped from the runtime
container image** (Dockerfile) since a running service never installs packages,
so the Trivy image scan is likewise clean with no exceptions. The
**registry-publish + cryptographic signing + SLSA provenance** infrastructure
that was open here is now shipped (GHCR + cosign keyless + SLSA attestation in
`release.yml`, tracked alongside item 89 phase 2); see the phase-2 status note
at the top of this item for the two operational bits (first executed release +
package-index) that remain maintainer decisions.

**Was out of scope for phase 1; status now:**

- **Publishing to a registry.** ✅ Container registry chosen and wired: GHCR
  (`ghcr.io/${{ github.repository }}`), pushed by `release.yml` on a `v*` tag
  after a pre-publish Trivy gate. Pushing still requires an explicit maintainer
  tag push — never automatic on a `main` commit. **A Python package index
  (PyPI/private) is still not chosen** — the only remaining publish gap.
- **Cryptographic signing + provenance.** ✅ Shipped. cosign keyless signature
  (Sigstore, OIDC identity, transparency log) plus a SLSA build-provenance
  attestation, both digest-bound, produced by `release.yml`. (The earlier
  "security theater until there's something to sign" concern is resolved now
  that GHCR is the real published artifact.)
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

a new `querygate/help/` product-knowledge boundary with ten canonical Markdown topics packaged in the wheel and tied to the installed QueryGate version. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 33).

### 34. Interactive mocked HTML product sandbox ✅ DONE

`landing/sandbox.html` — a single self-contained, static HTML page (fonts via Google Fonts CDN, everything else inline, no build step, no network calls after load) linked from… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 34).

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

This is the first independently deployable slice of 32B, built entirely on 32A's existing `querygate/catalog/` models, `CatalogStore`, and `CatalogFileRepository` — no second… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 32).

### 36. Extensive production-grade QA project / edge-case test suite

**Phase 1 (policy-cap boundary tests + property-based compiler fuzzing) ✅
DONE.** **Phase 2a (REST/MCP malformed-input fuzzing) ✅ DONE.** **Phase 2b
(cross-dialect differential tests) not started — split out below because it
needs a live/mocked second-dialect comparison harness, not just more
Hypothesis strategies on the existing compiler tests.** Phase 2 was split
into 2a/2b because the two halves have unrelated infrastructure: malformed-
input fuzzing is a fully in-process JSON-boundary sweep, while cross-dialect
differential *execution* comparison needs both a live Postgres and a live
MSSQL to compare real results — a heavy dual-DB harness. (Compile-time
cross-dialect *rendering* was already covered by item 78.)

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

`catalog/adaptive_learning_benchmark.py` + a packaged fixture drive the real persisted 32C learning lifecycle (usage signals → learned proposal → governed review/publish/rollback → agent-visible retrieval) end-to-end, with every control (below-threshold, conflict, single-principal, cross-connection, denied-object, unreviewed-guidance) and determinism/idempotency/two-worker proofs; run by `tests/integration/test_adaptive_learning_benchmark.py`. Reconciled from a shipped-but-unmarked state. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 37).

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

### 39. Draft-aware policy simulation before staging ✅ DONE

`POST /api/v1/admin/config/simulate` evaluates an uncommitted candidate (draft connections/policy/catalog + a target principal) in an isolated, non-persisting registry/policy/catalog context using the real production loaders and visibility/policy code, returning a redaction-safe typed allow/deny + guardrails + mandatory-filter readiness. Gated on both config scopes; threat-model QG-19. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 39).

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

Server-side separation of duties on the config plane: durable per-version `ConfigApprovalRecord`s, author≠approver enforced in the store, an `AppConfig.require_config_approvals` apply-gate (single-admin mode when 0; backward-compatible), the `admin:config:approve` scope + REST approve/reject, the `querygate-config` CLI, and an admin-UI review affordance. Every decision is audited. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 42).

### 43. Admin connection-operations and health workspace ✅ DONE

`GET /api/v1/admin/connections` (`api/admin_connections_routes.py`) returns a credential-free, per-connection operational status built from the same `HealthMonitor` snapshot… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 43).

### 44. Admin observability and rejection-trend dashboard ✅ DONE (phase 1)

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

**Deliberately deferred (phase 2, not a gap in this pass):**
- **Time-window trend charts.** Phase 1's endpoint is a point-in-time
  snapshot, not time-series — there is no stored history to plot, so the panel
  renders honest current-value cards rather than faking a trend line over a
  window it cannot reconstruct. Real charts depend on the external
  metrics-backend below.
- A **config/catalog-change trend card** ("which connection changed after the
  last rollout?"). Those events live in the audit stream, not the metrics
  registry — surfacing them safely needs an audit-read aggregation path, not a
  metrics read, so it's its own slice.
- **Querying an operator-configured external metrics backend** (e.g. Prometheus
  HTTP API) for real time-windowed history and cross-replica aggregation,
  replacing the honest single-process snapshot where such a backend exists.

**Why it matters:** Item 31 can browse individual audit events, but it cannot
answer operational questions such as “Which policies reject the most
requests?”, “Is queue pressure rising?”, or “Did cost-estimation availability
regress?” Those trends are what let an administrator tune policy and capacity
proactively — and phase 1 answers them now over the API, honestly scoped to
what a single process can truthfully report.

### 45. Dedicated non-admin "My access" portal ✅ DONE (phase 1)

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

**Deliberately deferred (phase 2, not a gap in this pass):** "safe
explanations of recent personal denials." There is no principal-scoped
audit-read path today — existing audit browsing
(`api/admin_ui_routes.py`'s audit endpoints) requires `admin:config:read`
and is global, not filtered to the caller's own events. Building one safely
(bounded, redaction-safe, provably incapable of leaking another principal's
rejection detail) is independent scope deserving its own review, not a UI
bolt-on onto this pass.

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

Five fixed, code-reviewed presets (`querygate/admin/templates.py`): `deny-by-default`, `reporting-only`, `customer-support`, `tenant-isolated`, `bounded-analytics`. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 46).

### 47. Safe draft recovery plus config export/import UX ✅ DONE (phase 1)

**Phase 1 (portable change-set bundle export/import + policy-only local
recovery) ✅ DONE.** **Phase 2 (a server-side, authorized, encrypted-at-rest
draft store with retention/deletion controls) not started — split out below
because it is a distinct persistence subsystem with its own
encryption/retention/audit design, and the file-based bundle already delivers
cross-environment move and full-config recovery without it.**

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

**Effort: M (2–3 days).** Basic download/upload is small, but safe recovery
must handle sensitive connection documents, version/fingerprint metadata,
schema validation, stale-base conflicts, size limits, and browser-storage
rules without creating an ungoverned shadow config store.

**Why it matters:** Item 31 warns before abandoning an in-memory draft, but a
tab crash or browser restart still loses work. Administrators also need a
convenient way to move a reviewed change between environments while preserving
the YAML/CLI path rather than copying text fields by hand.

**What to do (phase 2):** Add a server-side, authorized, encrypted-at-rest
draft store with retention/deletion controls and audit events, for
full-config recovery that survives a lost download and works across devices —
the heavier alternative this item's original scope named alongside the
downloaded file. Keep it a governed store with its own retention/deletion and
audit design; do not let it become a second config-mutation path around the
existing validate → stage → apply flow.

### 48. Pre-defined, admin-approved query templates ("Toolbox"-style curated tools) ✅ DONE

Named, parameterized `StructuredQuery` templates: file-configured + invocable over REST/MCP (phase 1), with authoring now a governed, versioned, rollbackable change — `templates.yaml` is a fourth document in the item 25 config-versioning plane (validate/preview/stage/apply/rollback + an admin-UI config-editor pane), phase 2. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 48).

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

`examples/claude_agent_sdk_integration.py` — a runnable script registering QueryGate as an HTTP MCP server in the Claude Agent SDK (`ClaudeAgentOptions(mcp_servers=...)`) and… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 20).

### 31. Admin UI / policy designer ✅ DONE

an integrated, dependency-free control plane at `/admin/`, served by the existing FastAPI process rather than deployed as a second service. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 31).

## P4 — proposed: competitive parity (not yet triaged)

Items 49–60 come from an explicit gap analysis against the strongest
adjacent products (Google's Gen AI Toolbox for Databases, Hasura, and
Immuta/Privacera-class data-governance platforms), not from a repo scan.
None of them are a gap in v1's own stated scope — they're additive ground
to close QueryGate's remaining deficits in masking granularity, cost/quota
governance, ecosystem reach, and external trust signals. Triage into P2/P3
(or drop) once prioritized; until then this section is a holding area.

### 49. Column-value masking/tokenization (not just allow/deny) ✅ DONE

a `column_mask` policy primitive (`policy/models.py`: `ColumnMask`/`ColumnMaskKind`, field `Policy.column_masks` keyed by table with `"*"` wildcard, resolver… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 49).

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

### 51. Typed client-side query-builder SDK (Python + TypeScript)

**Phase 1 (Python builder) ✅ DONE.** **Phase 2 (TypeScript sibling +
standalone dependency-light distribution) not started — split out below
because the standalone-distribution half is coupled to item 30 phase 2's
still-unresolved registry decision, and TypeScript is a genuinely separate
language implementation, not more of the Python work.**

**Phase 1 shipped:** `querygate/client/` — a fluent, typed builder that
constructs the *same* `query_ast` Pydantic models the server validates, then
serializes them to the exact REST/MCP wire JSON. Public surface
(`querygate.client.builder`, re-exported from `querygate.client`):

- `Query.from_(...)` with chainable `.select/.distinct/.join/.where/.group_by/
  .having/.order_by/.limit/.offset/.top_n/.intent`, terminating in
  `.build()` (a validated `StructuredQuery`), `.to_dict()`, or `.to_json()`.
- A predicate DSL: `col("Table.Col")` with Python comparison operators
  (`==`/`!=`/`<`/`<=`/`>`/`>=`, plus named `.eq/.neq/...`), `.in_/.not_in/
  .like/.between/.is_null/.is_not_null`, column-to-column comparison
  (`col("a") > col("b")` → `value_col`), and boolean groups `and_/or_/not_`.
- Select-item helpers for every non-string member of the `SelectItem` union:
  `agg.{count,sum,avg,min,max,stddev,variance}`, `date_bucket`, `string_agg`,
  `array_agg`, `percentile_cont`, `fn_select` (scalar function projection),
  and `case`/`when`; scalar-function predicate targets via `fn`/`col_fn`;
  `asc`/`desc` ordering helpers. Scalar-function args and CASE results must be
  wrapped `col(...)`/`lit(...)` (bare values are rejected as ambiguous), and
  a predicate helper handed to `.select()` raises a message pointing at
  `fn_select`.

**No server change, and no duplicated validation** (the CLAUDE.md invariant):
because `build()` instantiates the real models, an illegal shape (`count(*)`
with `distinct`, a self-join missing an alias, an out-of-range percentile,
`between` without two values) raises client-side with the *same* error the
server would return — and whatever it emits is still fully policy/schema/
guardrail-checked by `StructuredQueryService` before any row is touched.
The builder can never drift ahead of or behind the AST because it *is* a
thin front-end over it.

Covered by `tests/unit/test_client_builder.py` (23 tests): fidelity to
hand-written wire JSON, round-trip back through the real model, operator/
value_col/boolean/CASE/top_n/self-join/composite-join coverage, validation
propagation, and three **drift guards** that fail if the AST grows a
`StructuredQuery` field, a `SelectItem` variant, or a `CompareOp`/
`AggregateFn`/`ScalarFn` the builder can't express — the "kept in sync via a
schema test" acceptance criterion. `examples/client_sdk_python.py` is a
runnable script (offline build + optional `--send`); its three queries were
executed end-to-end against a real demo Postgres (HTTP 200) during
development, and a masked-column variant was confirmed to still get a policy
`422`, proving the builder adds no trust.

**Ships inside the `querygate` package** for phase 1 — `import` it from an
installed wheel (`from querygate.client import Query`), and since
`query_ast/models.py` and `querygate/__init__.py` are pydantic-only the
import stays light. It is not yet a *separate* dependency-light distribution.

**Explicitly deferred to phase 2:**

- **TypeScript builder.** The same contract in TS with a compile-time-typed
  `StructuredQuery` — a separate language implementation with its own
  sync-test strategy (it can't reuse the Python Pydantic models), not more of
  the Python work.
- **Standalone, dependency-light distribution** (a `querygate-client` package
  on PyPI / an npm package) so an adopter can install the builder without the
  full server dependency closure (`pyodbc`/`asyncpg`/`fastapi`/`redis`/…).
  This is coupled to **item 30 phase 2**: no package/registry has been chosen
  or configured, and publishing needs explicit maintainer approval — building
  a standalone dist with nowhere to publish it is premature. Until then the
  in-tree `querygate.client` is the shipped, importable, tested surface.

**Effort: M per language.** A thin typed wrapper around the existing
`StructuredQuery` schema — no server-side change; it mirrors a contract that
already exists.

**Why it matters:** Adopters today either hand-write `StructuredQuery` JSON
or read `examples/rest_calls.md` / `examples/mcp_calls.md`'s raw JSON-RPC
and curl examples. Google's Toolbox and most competing frameworks ship
typed SDKs in multiple languages with autocomplete and client-side
validation. This is the single largest lever on integration friction and
the most concrete ecosystem gap identified against Google's Toolbox.

**What to do (phase 2):** Add the TypeScript builder mirroring phase 1's
Python surface with its own schema-sync test, and — once item 30 phase 2
chooses a registry — extract the Python builder into a standalone
dependency-light `querygate-client` distribution, keeping the in-tree
`querygate.client` importable for existing users. Keep it a pure client-side
convenience: it must not bypass or duplicate any server-side validation.

### 52. Multi-framework agent integration examples (LangChain, LlamaIndex, OpenAI function-calling) ✅ DONE

Three runnable MCP integration examples (LangChain/LangGraph, LlamaIndex, OpenAI function-calling) mirroring item 20's shape, with a model-free drift/bridge test and no new runtime dependency. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 52).

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

### 54. Compliance control mapping (SOC 2 / ISO 27001 readiness) ✅ DONE

Shipped `docs/COMPLIANCE_MAPPING.md`: a control-by-control map of QueryGate's
product controls to the SOC 2 Common Criteria (CC1–CC9) + Confidentiality/
Availability/Processing-Integrity series and ISO 27001:2022 Annex A, each row
backed by a concrete code/test/doc artifact, with an explicit
product-provided / shared-responsibility / customer-org responsibility split.
Includes an honest gap analysis (the audit engagement itself, org-level
controls like HR/physical/IR-process, access-review formalization, and the
not-yet-shipped config-SoD items 39–42) — real gaps, no process theater.
Cross-linked from `docs/SECURITY_POSTURE.md`. The coordination-gated remainder
(the independent audit) is item 53; org-process standup is the deploying org's.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 54).

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

Shipped: a zero-downtime Helm chart (`updateStrategy.maxUnavailable: 0` +
`checksum/config` rolling-restart on config change), an `values-ha.yaml`
multi-zone overlay (autoscaling floor 3, PDB, zone/host topology spread,
Redis-shared concurrency), an optional RWX config-governance PVC, and
`deploy/HA_DR.md` — the shared-state correctness matrix (concurrency shared;
quota still per-replica until item 50 phase 2; config/audit per-replica unless
shared), the multi-replica zero-downtime config-reload contract, multi-zone/
multi-region topology, and a backup/restore + RTO/RPO DR procedure. Chart HA
invariants are asserted against `helm template` in
`tests/unit/test_helm_ha_deployment.py`. GO_TO_MARKET claims reconciled. The
live multi-region failover *drill* is the operator's step (checklist in HA_DR).

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 56).

### 57. Pluggable dialect-adapter architecture ✅ DONE

Dialect-specific behavior is behind two registry-dispatched abstract bases: the
sync compiler `DialectAdapter` (item 73) and a new async `SessionDialectAdapter`
(`connections/dialects.py` — engine-URL/connect-args/timeout/guardrails, one
class per dialect), reversing the prior inline-branching exception. Adding a
dialect (item 19) = implement both + register. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 57).

### 58. Published adversarial benchmark vs. raw-SQL agent and Google Toolbox ✅ DONE (phase 1)

**Phase 1 shipped** (offline, deterministic, reproducible): a fixed,
versioned attack corpus (`benchmarks/security_boundary_v1.yaml`), a runner
that drives the **real** request-pipeline guardrails (policy validation +
compiler parameter binding + the AST-only no-raw-SQL structural invariant)
with no DB/network/LLM, the `querygate-security-benchmark` CLI (`run`/`list`,
`--json`, exit-0-iff-clean so it can gate CI), and the published report
`docs/business/SECURITY_BENCHMARK.md`. Current corpus: QueryGate blocks
**14/14 (100%)** structural boundary attacks vs. a structurally-modeled
raw-SQL-passthrough baseline at **0/14 (0%)**, with **2** documented
inference residuals disclosed (never counted as catches) and sub-millisecond
per-query guardrail overhead. Tests: `tests/unit/test_security_benchmark.py`.
The baseline is a declared *structural model* of a naive SQL-forwarding
gateway, not a live competitor run; the Google MCP Toolbox comparison is
capability-level (from documented design), kept factual per this file's
external-market-reference instruction.

**Phase 2 — not started (needs external infrastructure).** A *live* baseline:
run the same corpus against (a) a real LLM composing raw SQL against a seeded
database and (b) a comparably configured Google MCP Toolbox deployment, and
publish measured catch-rate/latency for both. Requires a model-provider
decision + a GCP/Toolbox environment, so it is out of the offline phase-1
scope. Until it ships, this item is not fully `✅ DONE` and stays inline here.

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

### 59. Read-only behavioral anomaly surfacing on the audit stream ✅ DONE

Read-only per-principal anomaly detector over the persisted audit stream (`querygate/admin/anomaly.py`) — volume spikes, rejection-rate jumps, and newly-touched connections vs. each caller's own baseline — exposed via `GET /api/v1/admin/observability/anomalies` and a "Behavioral anomalies" panel in the admin Observability view; strictly within the 32C read-only boundary. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 59).

### 60. Bug bounty / responsible disclosure program ✅ DONE

Shipped the stage-appropriate coordinated-disclosure program in `SECURITY.md`:
private reporting channel (GitHub private advisory + maintainer contact), in/out
scope tied to the core guarantees, acknowledgement/triage SLAs, supported-version
policy, an explicit **recognition-only reward structure** (deliberately no
monetary bounty at this stage — the paid tier is a documented post-audit
escalation), and a **single remediation process** every report (researcher,
internal adversarial-suite, or item-53 audit) flows through: triage →
regression-lock in `tests/security/` → fix + release gates → release & coordinated
disclosure. The paid-bounty-platform activation remains gated on item 53's audit,
as this item's own sequencing requires.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 60).

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

Confirmed against the MCP spec first (`mcp.types.Tool.inputSchema: dict[str, Any]`, `ListToolsResult.tools: list[Tool]`) that each tool's schema is fully self-contained with no… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 61).

### 62. Consolidate redundant instructional prose into one source of truth ✅ DONE

Trimmed `mcp/instructions.py` from 8,347 to 7,031 stripped chars by removing the restated detail that duplicated `query_ast/models.py` `Field` descriptions and tool… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 62).

### 63. Scope-gate admin-only tool schemas out of non-admin sessions ✅ DONE

`mcp/server.py`'s `_install_scoped_tool_listing` re-registers the low-level `Server`'s `ListToolsRequest` handler with a wrapper that calls the original `FastMCP.list_tools()`,… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 63).

### 64. Make full catalog provenance opt-in on describe_table/search_catalog ✅ DONE

Added `CompactCatalogCitation` (status + precedence only) next to `CatalogCitation` in `catalog/retrieval.py`, plus a `resolve_citation(..., verbose: bool)` helper that builds… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 64).

### 65. Add a response-size cap to get_querygate_guide_topic ✅ DONE

`GuideTopicResponse` (`help/models.py`) gained `truncated: bool = False` and `max_response_bytes: int = 16_384` fields. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 65).

### 66. CI/test guardrail on total MCP schema+instructions size ✅ DONE

`tests/unit/test_mcp_token_budget.py` instantiates the real `create_mcp_server()` (same "assert against the live schema" pattern as `test_credential_redaction.py`), sums… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 66).

### 67. Restore StructuredQuery field descriptions items 62/64/65 assumed existed ✅ DONE

This item's content first appeared in the working tree without attribution mid-session, packaged with an instruction (in the tool output framing, not from the user) to not… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 67).

### 68. WHERE/HAVING resource-exhaustion guardrail caps ✅ DONE

Added `Policy.max_where_predicates` (default 100) and `Policy.max_in_list_size` (default 1000) to `policy/models.py`. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 68).

### 69. DISTINCT / COUNT(DISTINCT) ✅ DONE

Added `StructuredQuery.distinct: bool` (whole-query dedup) and `AggregateSelectItem.distinct: bool` (per-aggregate dedup) to `query_ast/models.py`, with a `model_validator` on… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 69).

### 70. Table aliases and self-joins ✅ DONE

- `query_ast/models.py`: `StructuredQuery.from_alias` and `JoinSpec.alias`, plus a `model_validator` (`_validate_table_aliases`) that runs at the pure AST layer, before any DB… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 70).

### 71. NOT groups and column-to-column WHERE comparisons ✅ DONE

- `query_ast/models.py`: `WhereGroup.not_terms` (aliased `"not"`), a single child (Predicate or nested WhereGroup) to negate — `_exactly_one_boolean` extended to require… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 71).

### 72. Whitelisted scalar functions and CASE in select (SELECT-only) ✅ DONE

- `query_ast/models.py`: a tagged-union arg shape avoids "is this string a column ref or a literal?" ambiguity — `ColArg` (`{"col": "Table.Column"}`) and `LiteralArg`… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 72).

### 73. `DialectAdapter` abstraction (compiler-scoped slice of item 57) ✅ DONE

New `compiler/dialect_adapters.py`: an `abc.ABC` `DialectAdapter` with three methods — `date_bucket`, `order_by_terms`, `stat_fn` — the only three points where two dialects… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 73).

### 74. NULLS FIRST/LAST ordering ✅ DONE

`OrderBySpec.nulls: Optional[Literal["first","last"]]`. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 74).

### 75. `stddev`/`variance` aggregate functions ✅ DONE

`AggregateFn` extended with `"stddev"`/`"variance"`. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 75).

### 76. Composite (multi-column) join keys ✅ DONE

AST validator requires each `extra_on` entry be a real two-element pair. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 76).

### 77. Scalar functions in WHERE/HAVING predicates (`Predicate.col_fn`) ✅ DONE

`validation/schema_validation.py`: new `predicate_column_refs( pred) -> Iterator[str]` — single source of truth (col if dotted, else each `col_fn.args`' `ColArg`, plus… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 77).

### 78. Cross-dialect rendering verification pass for items 68–77 ✅ DONE

every item-68–77 compiler code path that previously only had a Postgres-default (or single-dialect) render test now also has an explicit `dialect="mssql"` one, closing the gap… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 78).

### 79. Extend the property-based fuzzer to the item 68–77 AST surface ✅ DONE

Shipped, in `test_compiler_properties.py`:** - `_predicates()` now draws a mix of plain literal predicates (still the majority, preserving the historically covered shape),… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 79).

### 80. `string_agg` aggregate function ✅ DONE

Shipped.** New sibling AST type `StringAggSelectItem` (`col`, `delimiter`, optional `alias`) added to the `SelectItem` union — not a field bolted onto `AggregateSelectItem`,… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 80).

### 81. `array_agg` aggregate function ✅ DONE

Shipped.** New sibling AST type `ArrayAggSelectItem` (`col`, optional `alias` — no `delimiter`, since an array result doesn't need a separator) added to the `SelectItem` union,… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 81).

### 82. `percentile_cont` aggregate function ✅ DONE

New sibling AST type `PercentileContSelectItem` (`col`, `fraction: float`, optional `alias`), added to the `SelectItem` union. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 82).

### 83. Query-template authoring UX: slot self-consistency, readable dry-run, and on-demand live-schema check ✅ DONE

Parameter-slot self-consistency (a slot's `allowed_values`/`default` must match its declared `type`/bounds, sharing one `scalar_type_error` primitive with runtime binding so they can't drift); attributed, plain-language config dry-run errors (temp paths and pydantic boilerplate stripped, `templates.yaml: …`); and a separate best-effort on-demand live-schema check (`POST /admin/config/check-template-schema` + the admin-UI "Check templates vs. schema" button) that reflects the currently-live connections and reports per-template `ok`/`issues`/`connection_unavailable`/`unreachable` — the column/table existence the offline dry-run deliberately skips. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 83).

### 84. Structured catalog authoring UI (human-curated entries through the governance queue) ✅ DONE

A guided admin-UI "Curate" panel and a `catalog:author`-gated `POST /{connection}/proposals` endpoint compose a human-authored catalog entry into a validated draft carrying the new `source_class = manual`, routed through the existing catalog governance queue (`CatalogFileRepository`) — not the change-set/`ConfigVersionStore` catalog.yaml tab — so it gets the same quarantine → review → publish (as `verified`) → rollback safety and actor-attributed audit. Separation of duties is scope-based, not identity-based: a principal holding `catalog:review` may approve/publish its own manual proposal (no author≠approver check). **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 84).

### 85. Domain-separated admin UI (group the 9 flat views into Policy / Catalog / Templates / Connections / Releases) ✅ DONE

The flat admin nav was regrouped into seven domains (Overview / Connections / Policy / Catalog / Templates / Releases / Audit) with a secondary tab bar per multi-view domain — a nav+layout refactor that reuses every per-view render fn and scope gate unchanged, keeps the deep-linkable `/admin/#<view>` hash, folds the item-84 Curate panel into the Catalog domain, and surfaces the shared-release (Connections/Policy/Templates → bundled `ConfigVersionStore`) vs. self-contained-catalog (`CatalogFileRepository`) split as a per-domain release signal. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 85).

### 87. Extend structured authoring to Policy and Templates (same form→validated-YAML→staged pattern) ✅ DONE

The Templates domain gained a guided authoring form (id/connection/description + a parameter-slot builder + a validated JSON `StructuredQuery` skeleton) that composes a `QueryTemplate` and merges it by id into the draft `templates.yaml` via new `/admin/ui/templates/parse` + `/templates/render` endpoints, flowing through the shared validate → stage → apply → rollback (Releases) — no per-domain publish. Policy already had this via the visual designer, so templates was the deliverable; raw-YAML editing stays as the escape hatch and the shared `QueryTemplateFile` model rejects bad slots at compose time. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 87).

### 86. MCP transport request-body size/depth guard ✅ DONE

`mcp/transport_guard.py`'s `MCPRequestGuardMiddleware` wraps the MCP mount outside auth and rejects an oversized body (`413`) or one nested past a cheap O(n) structural-depth scan (`400`) *before* the transport's `json.loads` — closing the REST/MCP asymmetry where a deeply-nested MCP body used to surface as a handled HTTP 500. Thresholds are configurable (`mcp_max_request_bytes` default 4 MiB, `mcp_max_request_depth` default 100), far above any legitimate batch; the phase-2a deep-body test was flipped from "handled 500" to a clean 4xx and a byte-cap regression added. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 86).

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

### 89. Open-source security validation gates + customer-facing trust posture ✅ DONE (phase 1); phase 2 (signed delivery) mechanism shipped, first release + package-index remain maintainer-gated

**Why:** QueryGate is sold as a private, closed-source image customers pull and
run in their own infrastructure. Every enterprise security review asks "what
SAST/dependency/container/dynamic testing do you run, and can you prove it?" The
substance was already strong (pip-audit + CycloneDX SBOM deny-by-default, the
`-m security` adversarial suite, real Postgres/MSSQL + soak/load gates) but there
was no SAST, container scan, secret scan, API DAST, disclosure policy, or single
buyer-facing artifact packaging the posture. This item adds industry-standard,
open-source security validation as enforced CI gates and surfaces it as
verifiable evidence — the scanning tooling itself is entirely dev/CI-only (adds
no runtime dependency) and the security invariants are untouched. Where the new
gates found real CVEs in the shipped dependency set, they were remediated by
upgrade rather than accepted (see the CVE-remediation bullet).

**Shipped (phase 1):**
- **SAST** — **Bandit** (`[tool.bandit]` in `pyproject.toml`, `make sast`, in
  `release-check`) + **Semgrep OSS** (`p/python`, `p/security-audit`,
  `p/owasp-top-ten`) as a CI job. Deny-by-default; the 5 accepted Bandit findings
  are annotated inline with justified `# nosec <id>` (intentional in-container
  `0.0.0.0` bind, internal invariants/sentinels), never blanket-suppressed.
  CodeQL noted as the paid-GHAS upgrade (free only on public repos).
- **Container scanning** — **Trivy** step in the `docker` CI job scans the
  shipped image (vuln+secret+misconfig, HIGH/CRITICAL gate, `--ignore-unfixed`);
  `make scan-image` for local runs. Result: **0 HIGH/CRITICAL, `.trivyignore`
  empty** (findings fixed, not accepted — see the remediation bullet).
- **Secret scanning** — **gitleaks** over full git history as the `secret-scan`
  CI job + `make scan-secrets`; reviewed dev-only placeholders allowlisted in
  `.gitleaks.toml`. Backs the credential-isolation invariant. Verified: no leaks.
- **DAST** — **Schemathesis** fuzzes the live OpenAPI surface via
  `scripts/run_dast.py` (`make test-dast`, `dast` CI job), gating
  `not_a_server_error` + `negative_data_rejection`. Latest: 678/678 checks across
  59 operations, 0 server errors, 0 accepted malformed payloads. The recursive
  query-executing endpoints are excluded (Schemathesis #947 recursion limit) —
  not a gap, they get deeper coverage from `test_malformed_input_fuzzing.py`
  (item 36 phase 2a). Schemathesis runs from its **pinned Docker image**, not a
  Poetry dep: its transitive pins (starlette<1 on 3.x, pytest>=8 on 4.x) conflict
  with both the runtime security fixes and the pinned test stack, so a dev tool
  never constrains the shipped graph (like Trivy/gitleaks/Semgrep). The script is
  hermetic — no DB, no autouse fixtures — and reaches the app over the Docker
  host gateway.
- **CVE remediation (completes the dependency work item 30 phase 2 deferred).**
  The new dependency + container scans immediately surfaced real known CVEs in
  the shipped set. Rather than allowlist them, they were **fixed by upgrade**:
  fastapi 0.115→0.139 + starlette 0.46→1.3.1 (major, gated by fastapi), mcp
  1.12→1.28.1, python-dotenv→1.2.2, click→8.4.2, idna→3.18 (pydantic followed to
  2.13.4). Build/install tooling (`pip`/`setuptools`/`wheel`, incl. the copies
  setuptools vendors internally) is **stripped from the runtime image** in the
  Dockerfile (a service never installs packages). Net result: the pip-audit
  allowlist (`security/dependency-audit-allowlist.json`) and the Trivy exception
  list are both **empty**, and `make release-smoke` still executes a real
  structured Postgres query. Starlette 1.x's renamed status constants
  (`HTTP_422_UNPROCESSABLE_ENTITY`→`_CONTENT`, `HTTP_413_*`) were updated across
  the REST/MCP surface (25 sites) to clear the deprecation warnings. *Deferred,
  dev-only:* `black` (25→26 would reformat the whole tree) and `pytest` (7→9 test-
  framework migration) still carry CVEs but never ship in the image and run only
  on first-party code — tracked for a separate, isolated bump.
- **Trust surface** — `SECURITY.md` (disclosure policy + SLAs),
  `docs/SECURITY_POSTURE.md` (customer-facing status table + reproduce-it
  commands + threat-model mapping; the questionnaire artifact), and
  `docs/business/openssf-best-practices-answers.md` (honest OpenSSF criteria
  self-assessment). README "Security model" gained a posture pointer.

**Honest scope note:** the **OpenSSF Best Practices badge is FLOSS/public-repo
only**, so a private product cannot be *awarded* it — we keep a self-assessment
(all Quality/Security/Analysis criteria Met by real gates) instead. OpenSSF
Scorecard is likewise public-repo oriented and intentionally not run as a
private-repo gate. Both are documented as available on open-sourcing.

**Phase 2 — signed delivery / build provenance: mechanism shipped.** The
genuinely earnable external attestation for a self-hosted image product is
**Sigstore/cosign image signing + SLSA build provenance**, letting a customer
cryptographically verify the image they run was built by us from this source,
untampered. `.github/workflows/release.yml` now does both on a `v*` tag push:
after a pre-publish Trivy gate it pushes to GHCR, signs with cosign keyless
(Sigstore, OIDC identity, transparency log), and attaches a SLSA build-provenance
attestation (`actions/attest-build-provenance`, `push-to-registry: true`), both
bound to the image digest. Consumer verification (`cosign verify` /
`gh attestation verify`) and offline artifact-integrity checking (`make
verify-release` over `dist/SHA256SUMS`, `scripts/verify_release.py`) are
documented in `docs/RELEASING.md`; `docs/SECURITY_POSTURE.md` and the OpenSSF
self-assessment now record signed delivery as implemented. **Still not fully
`✅ DONE`** (why this item and item 30 stay phased): the *first* signed+attested
release is only produced when a maintainer deliberately pushes a version tag
(publishing is never automatic), and a Python package-index (PyPI/private) has
not been chosen — the container image is the signed, provenance-attested
distribution channel today. Both are maintainer decisions, not code work.

## P6 — Category-defining moats (from competitive-scan, 2026-07-22)

Items 90–92 promote three of the "breakout four" features from
`docs/business/MARKET_DOMINATION_ANALYSIS.md` (F1/F5/F3) from strategy prose
into scoped worklist items. The fourth, F2 (minimum-group-size), already
shipped as item 88. F4 (safe NL→StructuredQuery) is deliberately **not** an
item yet: it requires an explicit maintainer decision on model provider/posture
before any build, per `CLAUDE.md`'s catalog/LLM boundary — kept as a strategy
proposal, not an approved task. Each item below preserves every core invariant
(no caller-controlled raw SQL, catalog stays descriptive, no autonomous LLM
call or policy self-edit); they *strengthen* attribution and governance rather
than widen the input surface.

### 90. Delegated agent identity (on-behalf-of) carried into policy + dual-identity audit ✅ DONE

Delegated-identity attribution (RFC 8693 `act` → `Principal.actor`, the human's policy applies, both identities audited) plus the MCP surface as an opt-in OAuth 2.0 resource server (RFC 9728 metadata, RFC 8707 audience binding, RFC 6750 `WWW-Authenticate` scope/step-up challenges).
**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 90).

### 91. Tamper-evident hash-chained audit ledger + per-query compliance receipts ✅ DONE

Optional `AUDIT_SINK_BACKEND=jsonl_chained` wraps every redaction-safe event in a hash-chain envelope (SHA-256, or HMAC-SHA256 with `AUDIT_LEDGER_HMAC_KEY`) so edits/deletions/reordering/insertion are detectable; `querygate-audit verify` validates a ledger and `querygate-audit receipt` emits a portable per-query compliance receipt. Chaining is envelope-level (no new event data) and verify-only. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 91).

### 92. In-query human-in-the-loop approval for sensitive/expensive reads (MCP elicitation step-up) ✅ DONE

Cost/row-estimate and catalog-sensitivity triggers pause a gated read; approval is a stateless, fingerprint-bound, short-lived HMAC token via a scope-separated REST flow (428 → `query:approve` → resubmit), threaded per-query through batches, and — opt-in, off by default — obtainable in-session over MCP via `Context.elicit`. Read-only, AST-only, opt-in throughout. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 92).

## P7 — Governed Writes (flagship structural expansion — decision-gated)

Item 93 is the largest scope expansion on the roadmap and the market's biggest
unsolved problem (see `docs/business/MARKET_DOMINATION_ANALYSIS.md` §P1). It
**crosses the product's read-only line** and therefore **requires an explicit
maintainer product decision before any implementation** — the write-up below is
a complete design and worklist, not an approved commitment. It preserves every
core invariant: no caller-controlled raw DML ever reaches a database (a write
is a validated structure, exactly as a read is), the catalog stays descriptive,
audit stays redaction-safe. The `roadmap-next` automation must **not** auto-start
it; a human decides first.

### 93. Governed Writes — structured, bounded, previewable, reversible agent mutations ✅ DONE (phase 1 — contract + dry-run preview; phase 2a — gated single-statement execution); phase 2b + phase 3 not started

**Phase 2a shipped (gated write EXECUTION, REST; maintainer-approved, Decision
Log recorded).** `execution/write_execution.py`'s `WriteExecutionService.execute()`
actually commits a single-table INSERT/UPDATE/DELETE — but only via the *same*
validated `write_ast` → `compile_write` Core statement the preview compiles (no
raw-DML path), and only when in policy and within cap. One transaction (the one
`session_scope` opens): count matched rows in-txn → reject if over
`max_affected_rows` *before* mutating → item-92 approval gate (new
`WritePolicy.require_approval_over_rows`, fingerprint-bound token via
`write_fingerprint`) → execute → re-check the statement's own rowcount against the
cap → commit; any raise rolls the whole thing back (no partial write). Runs
through the concurrency limiter. Redaction-safe, dual-identity (item 90),
tamper-evident (item 91) audit by reusing `audit_query` (operation
`execute_structured_write`) — op/table/affected-count/parameterized-SQL only,
never a value or row. REST `POST /{connection}/write/execute` (+ 428→approval)
and `POST /{connection}/write/approve` (`query:approve`-scoped, `write_fingerprint`
token). `compiler/write_compiler.py` now coerces a JSON temporal string to the
column's Python `datetime`/`date`/`time` so an INSERT/UPDATE of a typed column
binds. Covered by `tests/integration/test_write_execution_end_to_end.py` (4:
real-SQLite insert→verify→update→verify→delete→verify round-trip commits;
over-cap rolls back and changes nothing; deny-by-default; approval pause→admit)
+ `tests/unit/test_write_execution.py` (5: approval fingerprint binding/replay,
threshold off/under, temporal coercion). Invariant preserved: no raw DML,
deny-by-default, opt-in.

**Phase 2b shipped so far:** the **row-level old→new diff preview**
(`POST /write/preview?include_diff=true` → `WriteDiff`: bounded by
`WritePolicy.max_diff_rows`, masking-aware, computed by running the DML in a
rolled-back transaction — `execution/write_preview.py`) and the **adversarial
write boundary security suite** (`tests/security/test_write_boundary.py` +
dual-marked execution guarantees, under `make test-security`), and the **MCP
`run_structured_writes` tool** (`mcp/tools/write.py`: preview/execute modes,
batch, `include_diff`, and the in-session elicitation approval channel for a
gated write — the elicitation resolver was factored into shared
`mcp/elicitation.py`, now serving both the read and write tools;
`WriteExecutionService.execute_many` is the batch/resolver seam). **Still open in
2b:** the `release-smoke` write round-trip and the write concurrency load gate.
(Approval still binds to the write fingerprint, not the diff hash — a phase-3
refinement.) Write execution is also proved against a **real Postgres**
(`tests/integration/test_postgres_write_execution.py`, `real_db`/`postgres_live`:
self-cleaning insert→verify→update→verify→delete→verify round-trip + over-cap
rollback), not just SQLite. Also shipped in 2b:
**constraint handling** — an INSERT missing a NOT NULL column is caught with a
precise pre-DB validation error, and any DB constraint/type violation
(NOT NULL/FK/unique/mistyped) maps to a clean typed 422 (rolled back, no raw
driver text leaked) instead of a masked 500. **Phase 3:** compensation/undo,
upserts, multi-row batch, MSSQL execution parity, deeper FK/unique
*pre*-validation (a full pre-check needs a lookup query; today the DB enforces
them and the violation is a clean 422).

**Phase 1 shipped (maintainer-approved; Decision Log recorded).** The write
sibling of the read pipeline, preview-only — **no code path executes or commits
a write.** `write_ast/models.py`: `InsertStatement`/`UpdateStatement`/
`DeleteStatement` (discriminated union on `op`, dispatched via
`_WRITE_STATEMENT_TYPES`), **no raw-DML field anywhere**, WHERE reuses the read
`Predicate` tree; `UPDATE`/`DELETE` **require** a WHERE (structural — can't be
constructed without one). `policy/models.py` `WritePolicy` (deny-by-default:
`enabled=False`, per-table `allowed_tables`/`allowed_operations`,
`denied_write_columns`, `max_affected_rows`). `validation/write_policy_validation.py`
+ `validation/write_schema_validation.py` mirror the read validators (written
columns write-allowed + exist; a write's WHERE columns subject to the READ
allow/deny + masked-column rules; single-target-table only).
`compiler/write_compiler.py` builds Core `insert()`/`update()`/`delete()` (WHERE
via the read `_compile_where` — bound params, no raw SQL). `execution/write_preview.py`
`WritePreviewService.preview()` validates → compiles → reports a redaction-safe
`WritePreview` (op, table, affected-row count via a policy-checked `COUNT(*)`,
within-cap, **parameterized** SQL, `executed=False`) — no DML runs. REST
`POST /{connection}/write/preview` (discriminated-union body). Covered by
`tests/unit/test_governed_writes.py` (14: no-raw-DML invariant, structural
mandatory-WHERE, deny-by-default, WHERE read-policy, parameterized DML) +
`tests/integration/test_write_preview_end_to_end.py` (3: real-SQLite preview
reports the right count AND **changes nothing** — before == after — plus
deny-by-default). The `IN (subquery)` (item 97) is rejected in a write WHERE for
phase 1.

**Phase 2b–3 (not started):** phase 2a above shipped the core gated *execution*
(single transaction, in-txn row cap, item-92 approval on the row *count*,
dual-identity audit [90], tamper-evident audit [91]). Still open — **phase 2b:**
the transactional row-level old→new *diff* preview (the current preview reports
the affected *count* + parameterized SQL; the killer per-row diff runs the DML in
a rolled-back txn) and approval on that diff, the MCP `run_structured_writes`
execute tool, the adversarial write security suite, the `release-smoke` write
round-trip, and the write concurrency load gate; **phase 3:** reversibility/
compensation + upserts + batch + MSSQL parity + scalar-function/CASE SET-values +
NOT NULL/FK/unique pre-validation.

**Effort: XL (cleanly phaseable; Phase 1 is L and carries zero write risk).
Priority: flagship. Status: decision-gated (crosses read-only). Depends on:
90 (dual-identity audit) + 91 (tamper-evident receipts) + 92 (approval gate)
for Phase 2; Phase 1 (preview only, execution disabled) depends on none of
them.**

**Why it matters (competitive pressure).** The entire market retreated to
read-only-by-default because agent writes are unsolved: the Neon incident was a
read→write control-flow hijack, Supabase's fix was to remove the write channel,
AWS calls its write-blocklist "best-effort, bypassable," and every DB-vendor MCP
server ships write behind an opt-out with a disclaimer that injection is
unsolved in read-write mode. **Nobody has a structural answer** — and a follower
cannot produce one without first giving up raw SQL, which is their whole
product. QueryGate's read-only limitation is not a weakness to defend; it is the
*launchpad*: the same AST-validation spine that makes reads safe is what makes a
safe write contract possible.

**The guarantee — stated honestly (this is the whole product framing).** Reads
have a total safety story: worst case you read a row you were already allowed to
read. Writes have a failure mode reads don't: a well-formed, fully in-policy
write with the *wrong values* or the wrong (but qualified) `WHERE`. No
structural layer can make a write *semantically correct*. So QueryGate must
claim only what it can prove, and prove all of it:
- **What is guaranteed by construction:** no raw DML string anywhere; every
  target table/column policy-checked before compilation; no unqualified
  UPDATE/DELETE ever; bounded affected-row count; only allowed operations on
  allowed targets; injection can only ever *propose* a validated structure that
  is then re-validated and capped — it cannot exceed policy or reach raw DML.
  **This eliminates the catastrophic-shape class of write entirely.**
- **What is made reviewable and reversible, not impossible:** the residual
  ("did the agent intend *this specific* change") is handled by a dry-run diff
  preview, a mandatory approval gate on the diff for sensitive/large writes,
  dual-identity attribution, and a bounded compensation/undo record.
- **What is NOT claimed:** "safe autonomous agent writes" / "provably correct
  writes." The claim is **governed writes: bounded, previewed, approved,
  attributed, reversible** — a category nobody occupies. Never repeat PromptQL's
  unbackable "100%".

**System architecture (mirrors the read pipeline component-for-component).**
Every piece is a sibling of an existing read component, feeding the same
validate → policy → schema → compile → (preview) → (approve) → execute → audit
spine. No second enforcement point is invented.

1. **`write_ast/models.py`** — sibling to `query_ast/models.py`. Typed
   `InsertStatement` (single or multi-row typed rows), `UpdateStatement`,
   `DeleteStatement`, and (Phase 3) `UpsertStatement`. **No raw-DML field of any
   kind.** Set-values and predicates reuse the *existing* read AST surface
   (whitelisted scalar functions, CASE, `Predicate`/filter tree, column refs) —
   no new expression grammar, no raw fragments. A `Discriminated`-union mutation
   type, dispatched via a dict-of-types registry like the read
   `_AGGREGATE_SELECT_ITEM_TYPES`, not scattered `isinstance` branches.
2. **`policy/models.py` — write policy surface** (new `WritePolicy` block on
   `Policy`, deny-by-default): per-table allowed operations
   (insert/update/delete/upsert); write allow-deny for target tables and columns
   *separate from* read allow-deny (a principal may read a column it may not
   write); `max_affected_rows` cap per operation; `require_mandatory_where`
   (default true — reject unqualified UPDATE/DELETE); `require_approval` triggers
   (row-count threshold and/or catalog `sensitivity` label); `allow_upsert`;
   compensation/undo policy (max snapshot rows/bytes, TTL); transaction/isolation
   settings. Loaded generically from `policy.yaml` like every other cap.
3. **`validation/write_policy_validation.py`** — mirror of
   `policy_validation.py`, runs first, before any DB touch: every target
   table/column checked against write allow-deny and *every* column referenced
   in set-values and predicates (not just the target list); operation-allowed
   check; mandatory-WHERE enforcement (unqualified UPDATE/DELETE rejected here,
   structurally); policy-level row-cap declaration.
4. **`validation/write_schema_validation.py`** — mirror of
   `schema_validation.py` via cached reflection: target tables/columns exist and
   types are compatible; refuse writes to generated/identity/computed columns
   unless explicitly allowed; NOT NULL / PK / FK / unique awareness so a policy
   violation is caught before the DB rejects it; resolve cross-connection safety
   (a write targets exactly one connection — no cross-connection write).
5. **`compiler/sqlalchemy_write_compiler.py`** — compiles the validated write
   AST + `WritePolicy` into a SQLAlchemy Core `Insert`/`Update`/`Delete`.
   Dialect-specific behavior (RETURNING support and fallback, upsert idiom —
   Postgres `ON CONFLICT` vs. MSSQL `MERGE`, identity/OUTPUT handling) lives
   behind the existing `DialectAdapter` interface (new methods), one concrete
   adapter per dialect; where a dialect genuinely lacks a capability, the adapter
   **rejects** with `QueryValidationError` and points at primitives — never
   emulates (the item-74 doctrine, `dialect-primitive` skill). Everything else is
   dialect-agnostic Core.
6. **`execution/write_preview.py` — the dry-run diff engine (killer feature).**
   Opens a transaction, resolves the exact affected-row set (via matched-row
   SELECT for UPDATE/DELETE, and RETURNING/OUTPUT where supported), computes a
   **bounded before/after diff** ("this UPDATE touches 38 rows — here they are,
   old→new"), then **rolls back**. Never commits in preview mode. Diff is
   row-capped and column-masking-aware (respects read masking on displayed
   values). This is the artifact PromptQL/Neon approximate with human clicks;
   QueryGate makes it structural.
7. **Execution-time row-cap guard** (`execution/service.py` write path) —
   belt-and-suspenders with the policy cap: count matched rows *inside the
   transaction* before mutating; if over `max_affected_rows`, abort and roll
   back. A concurrent-insert race cannot exceed the cap.
8. **Approval gate** — reuses item 92's MCP-elicitation + approval-token
   machinery: a write above the row/sensitivity threshold **pauses for human
   sign-off on the computed diff** before execution. REST degrades to a
   "requires approval" rejection carrying an approval token bound to the exact
   compiled write + diff hash. Approval, approver identity, and decision are
   audited.
9. **`execution/compensation.py` — bounded compensation/undo.** Before a gated
   mutation commits, capture a redaction-aware **pre-image snapshot** of the
   affected rows (bounded by policy rows/bytes/TTL) and emit a governed rollback
   operation that re-applies the pre-image under the same pipeline. **Honest
   limits, documented:** bounded reversibility only — cannot unwind cascading
   triggers/FK actions or side-effects, and downstream consumers may already
   have read the changed value. Sell bounded rollback, never a time machine.
10. **Transaction, concurrency, session guardrails** — writes run through
    `execution/concurrency.py` (a dedicated write limiter; a write must not be
    starved by or starve reads) and `connections/engine.py` with
    write-appropriate session guardrails per `connections/dialects.py` (Postgres
    `SET LOCAL lock_timeout`/`statement_timeout`; MSSQL `SET LOCK_TIMEOUT` +
    `XACT_ABORT ON`). One explicit transaction per write; explicit isolation;
    deadlock/lock-timeout surfaces as a clean typed error, never a partial write.
11. **`audit/events.py` — write audit event** (redaction-safe, tamper-evident
    via item 91, dual-identity via item 90): operation, target table, **affected-
    row count**, whether previewed, whether approved + approver, whether a
    compensation snapshot was taken (+ its id/hash), what was suppressed/masked.
    **Never** the set-values, predicate values, row contents, raw DML, or
    credentials — counts, shapes, and hashes only, exactly like the read event.
12. **Transport** — a new MCP tool `run_structured_writes(connection,
    writes=[...], mode=...)` mirroring `run_structured_queries`, with modes
    `preview` (dry-run diff, no execution — the Phase-1 surface) and `execute`
    (gated). REST route mirror. **No raw field on either.** Admin-scope-gated
    like other privileged tools.

**Security invariants and gates (non-negotiable, tested, not asserted).**
- A credential/raw-DML redaction test (sibling of
  `test_credential_redaction.py`) asserts against the **live OpenAPI + MCP tool
  schemas** that no write model, endpoint, or tool exposes any raw-SQL/DML field
  and that no write event body carries values/rows.
- The `make test-security` adversarial corpus (extend via the `adversarial-probe`
  skill / item 36) gains a **write boundary suite**: attempts to smuggle raw DML
  through a value/predicate; unqualified DELETE/UPDATE; over-cap write; write to
  a read-only-but-not-write-allowed column; write to a denied table; injection
  through set-values or WHERE; approval-gate bypass / token replay / token bound
  to a *different* compiled write; transaction escape / partial-commit on error;
  oversized multi-row batch; compensation/undo abuse (undo to exfiltrate a
  pre-image, or replay an undo to re-apply stale state).
- Property-based fuzzing: extend the item 79/78 fuzzer + cross-dialect
  rendering verification to the write AST — invariant checks that every
  generated write compiles or cleanly rejects, never emits raw SQL, and that the
  row cap and mandatory-WHERE guarantees hold for all inputs.

**Test coverage (comprehensive — the point of this item).**
- **Unit:** write AST models; write policy validation; write schema validation;
  write compiler per-dialect rendering (Postgres + MSSQL + SQLite-internal);
  preview/diff engine; execution row-cap guard; compensation snapshot/rollback.
- **Integration:** end-to-end against SQLite (compiler/execution seam, like
  `test_sqlite_end_to_end.py`) for preview and gated execute; dedicated real
  **Postgres** and **MSSQL** write jobs (insert→verify→update→verify→delete→
  verify→undo→verify), marked `real_db`.
- **Security:** the adversarial write suite above under `make test-security`.
- **Smoke:** extend `make release-smoke` with a real structured **write** round-
  trip against Postgres (preview a diff, execute a capped insert, verify it, then
  a governed undo) — proving the shipped image writes safely, not just reads.
- **Stress / soak:** extend `make test-load` and `make test-soak` with
  **concurrent write** scenarios — row-cap enforcement under concurrency,
  transaction isolation and no-partial-write under contention, lock-timeout /
  deadlock behavior, the approval gate under load, and compensation correctness
  under concurrent mutations to overlapping rows. A bounded real-Postgres write
  concurrency gate, matching the existing read load gate's shape.
- **Cross-dialect:** a rendering-verification pass (like item 78) covering every
  write operation on every dialect, including the reject-not-emulate cases.

**Phasing (each phase independently shippable).**
- **Phase 1 — contract + preview, execution DISABLED (L; zero write risk).**
  `write_ast/` + `WritePolicy` + write policy/schema validation + write compiler
  + the dry-run diff engine + the `preview` transport mode. No code path can
  commit a write. Ships an immediately useful "what would this change?" planning
  tool and validates the whole AST/policy/compiler design against a design
  partner before any real mutation. Includes: all unit tests, the redaction/
  no-raw-DML invariant test, the fuzzer + cross-dialect extension, and SQLite +
  real-DB *preview* integration tests. Depends on nothing beyond today's code.
- **Phase 2 — gated execution (L–XL).** Single-table insert/update/delete with
  mandatory WHERE, policy + execution row caps, the item-92 approval gate on the
  diff, one transaction, dual-identity audit (item 90), tamper-evident write
  receipt (item 91). Adds: the adversarial write security suite, the
  `release-smoke` write round-trip, and the write concurrency load gate.
  **Depends on 90 + 91 + 92.**
- **Phase 3 — reversibility + breadth (M–L).** Bounded compensation/undo
  (pre-image snapshot + governed rollback), upserts, multi-row/batch writes, and
  **MSSQL parity** for the full write surface. Adds: soak tests for compensation
  under concurrency and the full cross-dialect write verification. **Depends on
  Phase 2.**

**Product decisions to resolve before Phase 1 starts (the gate).** (a) Confirm
crossing read-only is desired now vs. later. (b) Compensation storage location
and retention (in-DB shadow table vs. sink-backed pre-image; bounded by policy)
— must stay redaction-safe. (c) Whether Phase 1's preview tool ships publicly on
its own as a "dry-run planner" ahead of any execution. (d) Approval UX for REST
(token flow) vs. MCP (elicitation) parity expectations. Record the decision as a
Decision Log entry in `docs/PRODUCT_GUIDE.md` before implementing, per CLAUDE.md.

**Invariant notes.** Preserves "no caller-controlled raw SQL/DML" absolutely — a
write is a validated structure, never a DML string. Preserves redaction-safe
audit (counts/shapes/hashes, never values/rows). Reuses, does not duplicate, the
policy/validation/compile/execute/audit spine. Uses `DialectAdapter` for all
dialect variance (reject-not-emulate where a dialect lacks a capability).

### 94. Verify (and, if warranted, enable) prepared-statement plan reuse for template execution

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

### 96. Unify the AST reference-walk into a single canonical visitor (enforcement hardening) ✅ DONE

Shipped: `validation/schema_validation.py` now defines the single canonical
`iter_column_refs(query) -> Iterator[ColumnRef]` visitor (with a `RefPosition`
taxonomy), and policy validation, `referenced_tables`, and schema validation's
table-collection all consume it. The four hand-maintained parallel walks
(`_iter_column_refs`, `_non_projection_column_refs`, `_collect_referenced_tables`,
and schema's `_collect_tables_from_where` + inline per-position loops) are gone.
Pure refactor, zero behavior change — proven by the adversarial, credential-
redaction, and full suites passing unchanged (1402 passed), plus a new
`tests/unit/test_reference_visitor.py` pinning the position taxonomy so a future
AST reference position is taught in one place. Makes item 97 safe by construction.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 96).

### 97. Bounded nested subqueries (uncorrelated, single-connection, depth-capped) ✅ DONE (phase 1 — IN (subquery)); phase 2 (FROM (subquery) derived table) not started

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
