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
| 26 | ✅ Query-cost estimation before execution (phase 1: Postgres `EXPLAIN`; phase 2: MSSQL `SHOWPLAN_XML`) | L | 2, 3, 15 |
| 27 | ✅ Semantic schema catalog and sensitivity metadata | L | 6, 16 |
| 28 | ✅ Threat model + adversarial security test suite | M | 1, 6, 8, 10, 11 |
| 29 | ✅ Production deployment reference stack | M | 4, 9, 12, 13, 14 |
| 30 | ✅ Distribution, SBOM, and signed release artifacts (phase 1: SBOM + audit; phase 2: GHCR publish + cosign + SLSA provenance shipped — first executed release + Python package-index remain maintainer-gated) | M | 4, 14 |
| 31 | ✅ Admin UI / policy designer | XL | 25 |
| 32 | ✅ Governed adaptive semantic memory for agents (32A ✅; 32B ✅; 32C ✅) | XL | 23, 25, 27, 28 |
| 33 | ✅ Permission-aware QueryGate product guide and configuration assistant | M–L | 8, 10, 21, 22, 25 |
| 34 | ✅ Interactive mocked HTML product sandbox | M | — |
| 35 | ✅ Agent-visible capacity waiting, progress, and cancellation (phases 1–3: caller-tunable queue_mode/wait_timeout_seconds/async, admission id, metrics/audit, queue-depth caps + Redis-backed cross-replica admission state, MCP progress notifications, REST 202+poll+cancel, dialect-level cancellation, 429+Retry-After) | L | 9, 12, 15, 20 |
| 36 | ✅ Extensive production-grade QA project / edge-case test suite (all phases: policy-cap boundary tests + Hypothesis property-based compiler fuzzing, REST/MCP malformed-input fuzzing, cross-dialect differential execution tests) | L | 15, 28 |
| 37 | ✅ Automated end-to-end proof of adaptive semantic learning | M–L | 23, 25, 27, 28, 32B, 32C |
| 38 | ✅ Admin UI catalog-governance workspace (phase 1: core review/approve/reject/publish/rollback loop; phase 2: bulk ops, export/import UI, generation triggers, review_history, usage-signal browsing) | L | 27, 31, 32B |
| 39 | ✅ Draft-aware policy simulation before staging | M–L | 6, 17, 25, 31 |
| 40 | ✅ Semantic access diff for config changes (phase 1: connection-baseline diff + REST; phase 2 per-principal resolution covered by item 41) | L | 6, 25, 31, 39 |
| 41 | ✅ Policy-change blast-radius analysis (phase 1: bounded synchronous aggregation + ranking; phase 2: stateless paginated evaluation) | M–L | 22, 25, 31, 40 |
| 42 | ✅ Four-eyes config approval and separation of duties | XL | 10, 23, 25, 31 |
| 43 | ✅ Admin connection-operations and health workspace (phase 1: admin connection-status API; phase 2a: "test now" probe; phase 2b: browser workspace) | L | 7, 12, 31 |
| 44 | ✅ Admin observability and rejection-trend dashboard (phase 1: admin-scoped aggregated overview API + read-only browser cards panel; phase 2: time-window charts, config/catalog-change trend, external metrics backend not started) | L | 12, 23, 31, 35 |
| 45 | ✅ Dedicated non-admin "My access" portal (phase 1: identity, guardrails, mandatory-filter readiness, schema browser; phase 2: personal denial history not started) | M | 22, 31, 33 |
| 46 | ✅ Validated policy templates and safe-start presets | M | 17, 25, 31, 39 |
| 47 | ✅ Safe draft recovery plus config export/import UX (phase 1: change-set export/import + policy-only local recovery; phase 2: server-side encrypted draft store not started) | M | 13, 25, 31 |
| 48 | ✅ Pre-defined, admin-approved query templates ("Toolbox"-style curated tools) (phase 1: file-configured invocable templates + REST/MCP; phase 2: governed authoring via the config-versioning plane) | L | 6, 22, 25, 32B |
| 49 | ✅ Column-value masking/tokenization (not just allow/deny) | L | 6, 27 |
| 50 | ✅ Per-principal rate limits / query quotas over time (phase 1: in-process rolling-window request/byte quota; phase 2: Redis-backed cross-replica quota) | M | 9, 25 |
| 51 | ✅ Typed client-side query-builder SDK (phase 1: Python builder; phase 2a: TypeScript builder; phase 2b: standalone dependency-light distribution not started) | M (per language) | 20 |
| 52 | ✅ Multi-framework agent integration examples (LangChain, LlamaIndex, OpenAI) | S (per framework) | 20 |
| 53 | Independent third-party security audit + published report | S* | 28 |
| 54 | ✅ Compliance control mapping (SOC 2 / ISO 27001 readiness) | L | 23, 25, 28 |
| 55 | ✅ Inference/transitive-exposure adversarial test suite | M | 28 |
| 56 | ✅ HA / multi-region reference deployment + DR runbook | L | 29 |
| 57 | ✅ Pluggable dialect-adapter architecture | L | 2, 19 |
| 58 | ✅ Published adversarial benchmark vs. raw-SQL agent and Google Toolbox (phase 1: offline deterministic corpus + CLI + published report; phase 2: external LLM/Toolbox harness not started) | M | 28, 36 |
| 59 | ✅ Read-only behavioral anomaly surfacing on the audit stream | M | 32C, 44 |
| 60 | ✅ Bug bounty / responsible disclosure program | S | 53 |
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
| 92 | ✅ In-query human-in-the-loop approval for sensitive/expensive reads (MCP elicitation step-up) | L | 26, 90, 91 |
| 93 | ✅ Governed Writes — structured, bounded, previewable, governed agent mutations (governance tier shipped: preview/diff, gated execution, approval, batch, upserts; reversibility/undo REMOVED 2026-07-23; `release-smoke` write round-trip open) | XL | 25, 48, 90, 91 |
| 94 | ✅ Verify/enable prepared-statement plan reuse for template execution | S | 48 |
| 95 | ✅ Discoverable scope catalog + recommended role bundles for IdP integration | S | 10, 90 |
| 96 | ✅ Unify the AST reference-walk into a single canonical visitor | M | — |
| 97 | ✅ Bounded nested subqueries (phase 1: `IN (subquery)`/`NOT IN`, tree-wide caps; phase 2: `FROM (subquery)` derived table not started) | L | 96 |
| 99 | ✅ ★ `HAVING` as `WhereNode` + searched `CASE` condition | S | 96 |
| 100 | ✅ ★ Bounded scalar `Expression` substrate (arithmetic, conditional aggregation, nested fns, expression-CASE) | XL | 96, 99 |
| 101 | ✅ ★ General window functions (`WindowSelectItem`: OVER, LAG/LEAD, frames) | L | 96, 100 (windowed exprs) |
| 102 | ✅ ★ `EXTRACT`/date_part + relative-date/interval helpers | M | 100 |
| 103 | ✅ ★ Non-equi/range joins + FULL OUTER / CROSS | M | 96, 99 |
| 104 | ✅ ★ Set operations (UNION / INTERSECT / EXCEPT) | L | 96, 97 |
| 105 | ✅ ★ CTE / derived table in FROM (non-recursive) | XL | 96, 97, 104 |
| 106 | ✅ ★ Correlated / EXISTS / scalar subqueries | XL | 96, 97, 105 |
| 107 | ✅ Batch query execution double-reserves quota on an approval retry | S | — |
| 108 | ✅ Write-preview diff runs the full DML before the affected-row cap is checked | S | — |
| 109 | ✅ MCP `run_structured_writes` has no batch-size cap | S | — |
| 110 | ✅ `value_subquery` in a write's WHERE is validated at the wrong layer | XS | — |
| 111 | ✅ Duplicated WHERE-predicate tree walk across four validators | S | — |
| 112 | ✅ No scheduled (cron) CI run — dependency/security scans only fire on push/PR | S | — |
| 113 | ✅ OBSOLETE — metrics for the removed write-undo / compensation store | — | — |
| 114 | ✅  Write tool MCP schema advertises read-only predicate fields it rejects | M | 93 |
| 115 | ✅  Guardrail-field lists in admin/help have drifted from `Policy`'s caps | S | — |
| 116 | ✅  A write's WHERE is exempt from every shape cap the read path enforces | S | — |
| 117 | ✅  `date_bucket` over a non-temporal column diverges across dialects | S | 102 |
| 118 | ✅ `min_group_size` was defeated by any fan-out join | M | — |
| 119 | ✅ `top_n` mis-resolves and DROPS a column on a duplicate output name | S | — |
| 120 | ✅ Audit shape records nothing for a nested `IN (subquery)` | S | — |
| 121 | ✅ Report-only surfaces still assume a query has one scope | S | 104 |
| 122 | ✅ `_unique_column_sets` crashed on a non-`Table` FROM element | S | 118 |
| 123 | ✅ A select-item `CASE`'s conditions are absent from the audit shape | S | 120 |
| 124 | ✅ Most of `tests/unit/` is not selected by `pytest -m unit` | S | — |
| 125 | ✅ ★ A window function as an `Expression` operand (bar row 15 → 16/16) | XL | 100, 101 |
| 126 | No per-caller rate limit on `GET /help/my-recent-denials` | S | 45 |

✅ = done (see item body below for exactly what shipped and what, if
anything, was intentionally left out of scope); a parenthesized phase note
means the item is only partially shipped and still carries open work.
★ = the flagship Expressive Query Engine pillar (items 99–106; spec in
[docs/ENGINE_EXPRESSIVENESS_PLAN.md](docs/ENGINE_EXPRESSIVENESS_PLAN.md)).
There is no item 98 — the number was skipped when that pillar was allocated
and stays unused, since item numbers are permanent and never reused.

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

Items 99–106 are the flagship Expressive Query Engine pillar (★) — one
coordinated initiative taking the READ engine to 10/10 expressiveness with no
safety regression; build in the listed order, per
[docs/ENGINE_EXPRESSIVENESS_PLAN.md](docs/ENGINE_EXPRESSIVENESS_PLAN.md).
Items 107–113 came out of the 2026-07-23 repo-wide technical/product review
(`TECHNICAL_REVIEW.md`) — narrow correctness, guardrail, and maintainability
gaps, sequenced in ROADMAP.md's "Technical and Product Improvement Plan".

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

### 26. Query-cost estimation before execution ✅ DONE

Proactive pre-execution cost gate: plan (never run) the compiled query with the DB's own planner and reject it if the estimated rows/cost exceed policy. Phase 1 Postgres `EXPLAIN (FORMAT JSON)` (in-session, fail-open, OBSERVE/ENFORCE modes + metrics); phase 2 MSSQL `SET SHOWPLAN_XML ON` on a dedicated connection — both dispatched by `StructuredQueryService._estimate_cost`. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 26).

### 27. Semantic schema catalog and sensitivity metadata ✅ DONE

A new `querygate/catalog/` module (`models.py` + `loader.py`) mirroring the existing `policy/` module's shape: an optional, versioned, YAML-file-configured overlay… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 27).

### 28. Threat model + adversarial security test suite ✅ DONE

Added `docs/THREAT_MODEL.md`, covering assets, trust boundaries, attacker capabilities, twelve concrete threat classes, implemented controls, deployment requirements,… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 28).

### 29. Production deployment reference stack ✅ DONE

A `deploy/` directory with two verified reference stacks — a production-ish Docker Compose file and a Helm chart — sharing the same shape: app + Redis (distributed concurrency,… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 29).

### 30. Distribution, SBOM, and signed release artifacts ✅ DONE (phase 1); phase 2 signing/provenance mechanism shipped, first signed release + package-index remain maintainer-gated

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

### 35. Agent-visible capacity waiting, progress, and cancellation ✅ DONE

Agent-visible admission (caller-tunable wait/fail-fast/async, `admission_id`/`queue_wait_ms`, Redis cross-replica queue-depth caps), MCP progress notifications via FastMCP's `report_progress`, a REST `202`+poll+cancel async lifecycle, real dialect-level cancellation (Postgres `pg_cancel_backend`/MSSQL `KILL`) gated on a deny-by-default `Policy.allow_query_cancellation` flag, and a full migration of capacity/queue rejections from REST `422` to `429`+`Retry-After`. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 35).

### 32. Governed adaptive semantic memory for agents ✅ DONE

This is the first independently deployable slice of 32B, built entirely on 32A's existing `querygate/catalog/` models, `CatalogStore`, and `CatalogFileRepository` — no second… **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 32).

### 36. Extensive production-grade QA project / edge-case test suite ✅ DONE

Phase 1 (policy-cap boundary + property-based compiler fuzzing), phase 2a (REST/MCP malformed-input fuzzing), and phase 2b (cross-dialect differential EXECUTION: same StructuredQuery run against live Postgres + MSSQL, rows asserted equal) all shipped. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 36).

### 37. Automated end-to-end proof of adaptive semantic learning ✅ DONE

`catalog/adaptive_learning_benchmark.py` + a packaged fixture drive the real persisted 32C learning lifecycle (usage signals → learned proposal → governed review/publish/rollback → agent-visible retrieval) end-to-end, with every control (below-threshold, conflict, single-principal, cross-connection, denied-object, unreviewed-guidance) and determinism/idempotency/two-worker proofs; run by `tests/integration/test_adaptive_learning_benchmark.py`. Reconciled from a shipped-but-unmarked state. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 37).

### 38. Admin UI catalog-governance workspace ✅ DONE

Full review→approve/reject→publish→rollback catalog workspace (phase 1), plus
bulk approve/reject/delete, export/import, generate-drafts/learn browser
triggers, a per-proposal review_history view, and usage-signal browsing
(phase 2) — all thin wrappers over item 32B's existing governance routes.
**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 38).

### 39. Draft-aware policy simulation before staging ✅ DONE

`POST /api/v1/admin/config/simulate` evaluates an uncommitted candidate (draft connections/policy/catalog + a target principal) in an isolated, non-persisting registry/policy/catalog context using the real production loaders and visibility/policy code, returning a redaction-safe typed allow/deny + guardrails + mandatory-filter readiness. Gated on both config scopes; threat-model QG-19. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 39).

### 40. Semantic access diff for config changes ✅ DONE

`POST /api/v1/admin/config/diff` (`admin/access_diff.py`) returns a server-derived, authorization-aware diff of *resolved* access — connection visibility, every guardrail cap, table/column access, mandatory-filter requirements, join groups — between the active config version and a caller-supplied candidate, each change classified tightening/loosening/neutral and redaction-safe; both config scopes required. Phase 2 (per-principal resolution) is COVERED by item 41's blast-radius, which reuses the same `compute_access_diff(principal=…)` engine (maintainer decision, 2026-07-23). Threat-model QG-20. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 40).

### 41. Policy-change blast-radius analysis ✅ DONE

`POST /api/v1/admin/config/blast-radius` (`admin/blast_radius.py`) reuses item 40's `compute_access_diff` — not a parallel resolution path — to evaluate every principal with an explicit `principals:` override, ranking only access-*expanding* changes in `highest_risk` and tagging each `baseline` (fleet-wide) or `principal` (targeted). Phase 2 added a stateless `principal_offset` cursor so successive pages cover every configured principal instead of the overflow being dropped as `analysis_incomplete`. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 41).

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

**Still deliberately deferred (phase 2, not a gap in this pass):**
- **Time-window trend charts.** Phase 1's endpoint is a point-in-time
  snapshot, not time-series — there is no stored history to plot, so the panel
  renders honest current-value cards rather than faking a trend line over a
  window it cannot reconstruct. Real charts depend on the external
  metrics-backend below.
- **Querying an operator-configured external metrics backend** (e.g. Prometheus
  HTTP API) for real time-windowed history and cross-replica aggregation,
  replacing the honest single-process snapshot where such a backend exists.

**Why it matters:** Item 31 can browse individual audit events, but it cannot
answer operational questions such as “Which policies reject the most
requests?”, “Is queue pressure rising?”, or “Did cost-estimation availability
regress?” Those trends are what let an administrator tune policy and capacity
proactively — and phase 1 answers them now over the API, honestly scoped to
what a single process can truthfully report.

### 45. Dedicated non-admin "My access" portal ✅ DONE

A separate, dependency-free `/access/` static page showing identity, visible connections, effective guardrails, mandatory-filter readiness, and policy-filtered schema (phase 1); phase 2 added safe, principal-scoped explanations of the caller's own recent denials (`GET /help/my-recent-denials`). **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 45).

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

Three `Policy` fields (`max_requests_per_window`, `max_response_bytes_per_window`, `quota_window_seconds`, resolved per principal through the existing `PolicyStore` merge) enforced by `execution/quota.py`'s narrow `QuotaLimiter` Protocol *before* the service queues or opens a DB session; a refused caller gets REST **429 + `Retry-After`** / MCP **`RATE_LIMITED`**. Phase 2 added `execution/redis_quota.py`'s `RedisQuotaLimiter` (one atomic Lua script, sorted set + bytes hash) so the window is a single shared budget across replicas. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 50).

### 51. Typed client-side query-builder SDK (Python + TypeScript) ✅ DONE (phase 1 — Python builder; phase 2a — TypeScript builder); phase 2b (standalone distribution) not started

**Phase 1 (Python builder) ✅ DONE. Phase 2a (TypeScript builder) ✅ DONE.**
**Phase 2b (standalone dependency-light distribution, either language) not
started — coupled to item 30 phase 2's still-unresolved registry decision.**

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

**Phase 2a shipped:** `clients/typescript/` — a TypeScript mirror of the
Python fluent surface's *structure and behavior* (`Query.from(...)`, `col`/
`lit`/`fn`/`colFn`, `agg.*`, `dateBucket`/`stringAgg`/`arrayAgg`/
`percentileCont`/`fnSelect`, `caseSelect`/`when`, `and_`/`or_`/`not_`, `asc`/
`desc`, the item-100 scalar-expression helpers (`expr`/`exprFn`/`cast`/
`extract`/`now`/`dateAdd`/`caseExpr`/`exprSelect`), and the item-101/125
window helpers (`window`/`windowExpr`/`frame`)), producing byte-identical wire
JSON — but *not* name-for-name: every multi-word Python name is renamed
snake_case→camelCase per TS convention (`group_by`→`groupBy`, `order_by`→
`orderBy`, `top_n`→`topN`, `col_fn`→`colFn`, `date_bucket`→`dateBucket`,
`string_agg`→`stringAgg`, and so on), on top of the handful of renames
JavaScript's own grammar forces (`case`/`in` are reserved words); see
`clients/typescript/README.md`'s naming table. In-tree only
(`clients/typescript/`, not published — same registry gate as the Python
standalone distribution); build with `npm install && npm run build` inside
that directory, or `make test-ts-client`.

Since TypeScript can't import the Python Pydantic models, it can't reuse
`build()`'s "raises the server's own error" trick verbatim, so the port does
two things instead: (1) TypeScript's own type system rejects a class of
mistake the Python builder can only catch at runtime (an un-wrapped bare
scalar-function argument, a `Predicate` passed to `.select()`) at *compile*
time; (2) the handful of genuine cross-field business rules Pydantic's
`model_validator`s enforce — self-join aliasing, aggregate `distinct`
combinations, window function arity/frame/order-by rules, set-operation arm
shape, percentile range, scalar/expression-function arity — are reproduced as
explicit runtime checks with the same rejection message, in
`clients/typescript/src/builder.ts`.

`build()`/`toDict()`/`toJSON()` emit the **complete** `StructuredQuery` shape
(every field present; `null` for an unset optional, the Python-declared
default for a defaulted one) rather than mimicking Pydantic's
`exclude_none`/`exclude_defaults` trimming — see the 2026-07-28 Decision Log
entry for why a generic "strip defaults" pass was tried and rejected (it
cannot tell "never set" from "the real value is that default", concretely
`WindowFn` legitimately includes `"row_number"`). This stays fully
wire-compatible: the server's Pydantic models parse an explicit default/null
identically to an omitted field.

Covered by `clients/typescript/test/builder.test.ts` (a hand-maintained
coverage suite — TypeScript unions are erased at compile time, so there is no
`typing.get_args`-equivalent automatic "AST grew a member" trip wire) and the
cross-language parity guard: both builders must reproduce
`tests/fixtures/client_builder_kitchen_sink.json` for three representative
queries (a wide join/aggregate/CASE query, a window-function query, a
set-operation query), pinned on the Python side by
`tests/unit/test_client_builder_ts_parity.py` (using a full, unexcluded
`model_dump`) and on the TypeScript side by
`clients/typescript/test/kitchenSink.test.ts` (both wired into CI — a
`typescript-client` GitHub Actions job runs `npm ci && npm test`, alongside
the existing `pytest` job that already picks up the Python-side parity test).
Verified end-to-end against a real running server and real Postgres during
development (not an automated/CI-enforced check, matching the same posture
already recorded for phase 1's `examples/client_sdk_python.py`):
`examples/client_sdk_typescript.ts` (mirrors `examples/client_sdk_python.py`)'s
join/aggregate query and its window running-total query both returned real
rows over HTTP 200.

**No CTE/subquery builder support** (items 105/106) on either language's
builder — a real, previously-undocumented gap in the Python builder (items
105/106 shipped after item 51 phase 1, which was never revisited), now
recorded rather than silently carried forward; `StructuredQuery.correlate`/
`.ctes` are typed on the TS side and always serialize as `[]`. A real
follow-up, not in scope for this phase.

**Explicitly deferred to phase 2b:**

- **Standalone, dependency-light distribution** (a `querygate-client` package
  on PyPI / an npm package) so an adopter can install either builder without
  the full server dependency closure (`pyodbc`/`asyncpg`/`fastapi`/`redis`/…).
  This is coupled to **item 30 phase 2**: no package/registry has been chosen
  or configured, and publishing needs explicit maintainer approval — building
  a standalone dist with nowhere to publish it is premature. Until then the
  in-tree `querygate.client` / `clients/typescript/` are the shipped, tested
  surfaces for each language.

**Effort: M per language.** A thin typed wrapper around the existing
`StructuredQuery` schema — no server-side change; it mirrors a contract that
already exists.

**Why it matters:** Adopters today either hand-write `StructuredQuery` JSON
or read `examples/rest_calls.md` / `examples/mcp_calls.md`'s raw JSON-RPC
and curl examples. Google's Toolbox and most competing frameworks ship
typed SDKs in multiple languages with autocomplete and client-side
validation. This is the single largest lever on integration friction and
the most concrete ecosystem gap identified against Google's Toolbox.

**What to do (phase 2b):** Once item 30 phase 2 chooses a registry, extract
both the Python and TypeScript builders into standalone dependency-light
distributions, keeping the in-tree modules importable for existing users.
Keep both pure client-side conveniences: neither may bypass or duplicate any
server-side validation.

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

`docs/INFERENCE_RISKS.md` plus adversarial regressions in item 28's suite. The investigation found **no enforcement gap**: `test_denied_column_cannot_be_used_for_inference` is now parametrized across every column-carrying AST position, so adding a new column-carrying node without extending the shared harvesters fails the test. The residual risks identifier allow/deny structurally cannot close (R1–R4) are documented, two with demonstrating tests; R3 (no minimum group size) was later closed by item 88. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 55).

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

`Policy.min_group_size` (floor 2; `None` disables) makes the compiler inject `HAVING count(*) >= k` into every **aggregate** query — grouped or single-implicit-group — so any group backed by fewer than *k* rows is suppressed: the aggregate analog of a mandatory row filter, closing the single-query singling-out form of item 55's R3 residual. Multi-query differencing stays honestly out of scope. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 88).

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
  `p/owasp-top-ten`) as a CI job, reproducible locally with `make semgrep` (which
  falls back to the official `semgrep/semgrep` image when no binary is installed,
  the same pattern as `scan-image`/`scan-secrets`; kept out of `release-check`
  because Semgrep's version *and* its registry rulesets both float, which would
  make that gate nondeterministic and couple a release to a third-party service —
  note `release-check` is not offline either way, since `make sbom` resolves the
  locked set from PyPI and audits it against the advisory database).
  Deny-by-default; the 7 accepted Bandit findings
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

### 93. Governed Writes — structured, bounded, previewable, governed agent mutations ✅ DONE (governance tier); reversibility/undo REMOVED 2026-07-23

**⚠️ 2026-07-23 — reversibility/undo REMOVED** (maintainer decision, recorded in
`docs/PRODUCT_GUIDE.md` Decision Log). Deleted: `execution/compensation.py` +
`execution/redis_compensation.py`, `POST /{connection}/write/undo`, the MCP
`undo_structured_write` tool, `WritePolicy.compensation_*`, and the
`compensation_id` result field. **Why:** undo was the only feature that forced a
second copy of real row values outside the customer's DB (against the
least-privilege / data-never-leaves North Star) and carried unresolved
correctness/durability risk, while adding little value once preview + approval
exist. **Governed writes keep the governance tier** — preview + diff, gated
execution, approval, dual-identity + tamper-evident audit, deny-by-default, the
row cap, upserts, atomic batch. The phase-3a/3b undo narrative below is retained
as history but no longer describes shipping behavior.

**Comprehensively shipped:** the write sibling of the read pipeline for all four
operations (INSERT/UPDATE/DELETE/UPSERT), *bounded* (deny-by-default, mandatory
WHERE, in-txn cap, atomic — single or all-or-nothing batch), *previewed* (dry-run
+ bounded masking-aware old→new diff), *approved* (REST token + MCP elicitation),
*attributed* (dual-identity, tamper-evident, redaction-safe audit). REST + MCP surfaces,
clean typed errors, an adversarial security suite, and proven on **SQLite +
real Postgres + real MSSQL + the shipped image + a concurrency load gate**. Only
two **reasoned deferrals** remain (not "not started" — deliberate, recorded):
upsert-undo (per-row insert-or-update is ambiguous to reverse) and
approval-binds-to-diff-hash (over-engineering vs the current fingerprint binding).

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
`WriteExecutionService.execute_many` is the batch/resolver seam), and the
**write concurrency load gate** (`tests/integration/test_postgres_write_load.py`,
`-m load`: 8 concurrent over-cap writes all reject and change nothing; 12
concurrent within-cap inserts commit exactly once each). **Still open in 2b:**
only the `release-smoke` write round-trip (extend the smoke image test with a
real capped write). (Approval still binds to the write fingerprint, not the diff
hash — a phase-3 refinement.) Write execution is also proved against a **real
Postgres**
(`tests/integration/test_postgres_write_execution.py`, `real_db`/`postgres_live`:
self-cleaning insert→verify→update→verify→delete→verify round-trip + over-cap
rollback), not just SQLite. Also shipped in 2b:
**constraint handling** — an INSERT missing a NOT NULL column is caught with a
precise pre-DB validation error, and any DB constraint/type violation
(NOT NULL/FK/unique/mistyped) maps to a clean typed 422 (rolled back, no raw
driver text leaked) instead of a masked 500.

**Phase 3a shipped — bounded reversibility (undo).** `execution/compensation.py`:
a `CompensationStore` (in-memory default, TTL'd, mirroring the audit-sink
pattern) holds a bounded pre-image — NOT a shadow table in the operational DB
(Decision Log records why: minimal footprint, no extra DB privilege). When
`WritePolicy.compensation_enabled`, a gated write captures the pre-image within
its transaction (UPDATE/DELETE rows, or INSERT keys), commits, then records it and
returns a `compensation_id`. `WriteExecutionService.undo()` (REST
`POST /write/undo`) re-applies the inverse in **one transaction**
(`_apply_undo_atomically`) — re-INSERT deleted rows / DELETE inserted keys /
restore each UPDATE's *changed columns* by PK — so undo is **atomic
all-or-nothing** (a failure reverses nothing and doesn't consume the record),
bounded, capped, and audited (`undo_structured_write`). **Undo semantics
(self-review hardened, Decision Log recorded):** authorized by the single-use,
TTL'd, connection-scoped `compensation_id`, so undo deliberately bypasses the
approval gate (the forward write was already approved; the prior state is
lower-risk) and the op-allowed re-check (undoing a DELETE is an INSERT), while
still enforcing deny-by-default + cap + schema + audit; it restores **only the
columns the original write changed**, so an untouched column's concurrent change
is preserved; and its inverse writes never spawn redo records. Proven on SQLite
(`test_write_execution_end_to_end.py`: delete/insert/update → undo →
byte-identical restore; approved-write undoable without re-approval; only-changed-
columns restored; atomic-not-consumed-on-failure; no redo record; auto-PK insert
returns `compensation_id=null`) and real Postgres. **Phase 3b shipped so far:**
(a) **RETURNING capture** — a serial/identity-PK INSERT now captures its
generated key via `RETURNING` and is undoable (was `compensation_id=null`); (b)
**durable cross-replica store** — the `CompensationStore` is async + pluggable
and `RedisCompensationStore` (installed when `CONCURRENCY_BACKEND=redis`) makes a
`compensation_id` resolvable on any replica, so undo works under HA (pre-image
round-trips through JSON with type re-coercion; fakeredis + real-DB tested;
`_coerce_write_value` now also coerces Decimal); (c) **optimistic-concurrency
undo** — an UPDATE undo reads each affected row's current changed-column values
and **refuses** (422) if any drifted from what the write set (or the row is
gone), so a concurrent change since the write is never silently clobbered.
**Honest bounded limits:** cannot unwind cascading triggers/FK actions or
downstream reads; (d) **MCP undo parity** — `undo_structured_write` tool, so an
agent that wrote over MCP can reverse over MCP; (e) **`release-smoke` write** —
`make release-smoke` now proves the shipped image preview→execute→verify→undo→
verify a governed write on real Postgres (Redis backend); (f) **MSSQL parity** —
insert/update/delete + undo all proven against a live MSSQL
(`test_mssql_write_execution.py`: OUTPUT key capture for IDENTITY PKs, and
delete-undo re-inserts the original key via SQLAlchemy's SET IDENTITY_INSERT).
Writes are now proven on **all three** engines (SQLite/Postgres/MSSQL); (g)
**upserts** — `UpsertStatement` (INSERT ON CONFLICT DO UPDATE) via a per-dialect
compiler registry (`_UPSERT_COMPILERS`, composable — no inline `if dialect`),
native on Postgres/SQLite, **rejected on MSSQL** (reject-not-emulate, no
synthesized MERGE); proven on real Postgres + MSSQL. Upsert-undo is deferred
(per-row insert-or-update is ambiguous to reverse); (h) **multi-statement batch
atomicity** — `execute_many(atomic=True)` + the MCP tool's `atomic` flag: all
writes in one transaction, all-or-nothing (fail-closed on a gated write; no
per-write compensation in atomic mode). **Phase 3b deliberately deferred (not
built speculatively):** approval-binds-to-diff-hash — the token already binds to
the write's full fingerprint and the trigger is re-evaluated at execute; binding
to a computed diff-hash would force the execute path to compute the diff every
time for a TOCTOU window no pilot has asked to close. **Upsert-undo** and this
are the only open item-93 items, both reasoned deferrals.

**Phase 3b — reversibility hardening: WITHDRAWN (2026-07-23).** The undo mechanism
these items would have hardened has been removed (see the note at the top of this
item and the `docs/PRODUCT_GUIDE.md` Decision Log entry). The hardening action
list no longer applies.

**2026-07-23 review finding — in-flight regression on `compensation.py`, FIXED.**
A working-tree edit converted `CompensationStore.put/get/consume` to
`async def` (prep for the Redis-backed durable store above), but
`execution/write_execution.py`'s three call sites
(`get_compensation_store().get(...)` line ~207, `.consume(...)` line ~217,
`.put(...)` line ~427) were not yet updated to `await` them when the review
found this — `get()` returned an un-awaited coroutine so `record.connection_id`
raised `AttributeError`, and `put()`'s coroutine was silently discarded (never
stored); undo did not work at all in a single process, not just across
replicas. **Fixed:** all three call sites now `await` the store, and
`tests/unit/test_write_execution.py::test_compensation_store_ttl_and_single_use`
(which itself called the now-async store synchronously) was converted to
`async def` with matching `await`s. Re-verified: `pytest -m unit` → 230
passed, 0 failed. **Still open, separate from the above:**
`InMemoryCompensationStore.consume()` only sets `record.consumed = True` and
never removes the entry from `self._records` — every governed write with
`compensation_enabled` leaks one record for the life of the process even after
TTL expiry, since `get()` filters expired/consumed records out of *reads* but
nothing ever evicts them from the dict. Fix this eviction gap before building
the Redis-backed store.

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

### 94. Verify (and, if warranted, enable) prepared-statement plan reuse for template execution ✅ DONE

Verified live against real Postgres and MSSQL: both already get full
server-side prepared-statement/plan reuse today (asyncpg's default
`prepared_statement_cache_size=100`; SQL Server's own plan cache), because
QueryGate already compiles every query with bound, never-inlined parameters.
No config change was warranted; regression-pinned in
`tests/integration/test_prepared_statement_reuse.py`.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 94).

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

### 97. Bounded nested subqueries (uncorrelated, single-connection, depth-capped) ✅ DONE

`Predicate.value_subquery` gives `IN (subquery)`/`NOT IN`, enumerated as an
independent scope by `iter_query_scopes` with every count cap summed tree-wide.
Phase 2 (the `FROM (subquery)` derived table) was **absorbed by item 105**, which
spells it as a named `WITH` block — so the derived table exists once, not twice.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 97).

### 99. Query engine: `HAVING` as `WhereNode` + searched `CASE` condition ✅ DONE

`having` and `CaseWhen.when` are now the same `WhereNode` union as `where`, so
OR-logic over aggregate conditions and searched multi-condition CASE branches
compile through the one existing `_compile_where`, are walked by the item-96
canonical visitor, and are bounded by the existing `max_where_depth` /
`max_where_predicates` (plus a new tree-wide case-condition predicate budget) —
no new policy field and no dialect code. Breaking wire change: `"having": [{…}]`
→ `"having": {…}`.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 99).

### 100. Query engine: bounded scalar `Expression` substrate ★ ✅ DONE

One closed, depth-capped recursive `Expression` union (column | literal |
arithmetic | nested function | cast | CASE) now backs projections, aggregate
arguments, CASE results, and both sides of a predicate — unlocking
`SUM(quantity * unit_price)`, conditional aggregation, `lower(trim(x))`, and
computed group keys in one item. Capped by `max_expression_depth` /
`max_expression_nodes` (summed tree-wide), visited by the item-96 canonical
visitor at every depth, guarded division, and reject-not-emulate per dialect.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 100).

### 101. Query engine: general window functions (`WindowSelectItem`) ★ ✅ DONE

A `WindowSelectItem` select item now projects any of thirteen window functions
(`sum/avg/min/max/count`, `row_number/rank/dense_rank/ntile`,
`lag/lead/first_value/last_value`) with `PARTITION BY`, `ORDER BY`, and
`ROWS`/`RANGE` frames — running totals, moving averages, rank-in-place, lag/lead
gap analysis. `arg` reuses item 100's `Expression`; capped by `max_window_specs`
(summed tree-wide) and `max_window_frame_offset`; no frame is synthesized when
omitted; rejected with `group_by`/aggregates (needs item 105's derived table) and
under `min_group_size`; numeric `RANGE` offsets rejected on MSSQL. Every function
and frame executes against live Postgres AND live MSSQL with rows compared.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 101).

### 102. Query engine: `EXTRACT`/date_part + relative-date/interval helpers ✅ DONE

Shipped `extract`/`now`/`date_add` as three new members of item 100's
`Expression` union, bounded by `max_interval_days`, plus the UTC session pin that
makes "every date answer is UTC" true rather than server-config dependent.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 102).

### 103. Query engine: non-equi/range joins + FULL OUTER / CROSS ✅ DONE

Shipped `JoinSpec.condition` (a full `WhereNode`, so range/temporal joins) plus
`full` and `cross` join types, with `cross` gated by a deny-by-default
`Policy.allow_cross_join` and the ON clause held to every WHERE-clause cap.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 103).

### 104. Query engine: set operations (UNION / INTERSECT / EXCEPT) ✅ DONE

Shipped `StructuredQuery.set_op` (UNION/INTERSECT/EXCEPT, with `all`), the first
top-level scope container — every arm independently validated, compiled, filtered
and k-anon-floored, capped by a tree-wide `max_set_op_arms`.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 104).

### 105. Query engine: CTE / derived table in FROM (non-recursive) ✅ DONE

Named `WITH` blocks via an additive `StructuredQuery.ctes` list — **not** the union
on `from`/`JoinSpec.table` the plan sketched, so no existing field changed type and
a consumer that has never heard of a cte fails closed (the name reflects as a table
and is rejected) instead of silently mishandling a new union member. Absorbs item
97 phase 2: an inline derived table is written as a named block, and the derived
table is therefore implemented once. Regression bar 12/16 -> **14/16** (rows 3 and
6). Every block is a full scope — its own mandatory row filters, masks, k-anon
floor and item-118 fan-out refusal — and carries **no `max_rows` clamp**, because
truncating intermediate work is a silently wrong total. 23/23 enforcement points
mutation-verified; live Postgres **and** live MSSQL.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 105).

### 106. Query engine: correlated / EXISTS / scalar subqueries ✅ DONE

`EXISTS`/`NOT EXISTS` as a `Predicate` operator, scalar subqueries as a comparison
RHS in WHERE and HAVING, and correlation via a **declared, capped**
`StructuredQuery.correlate` list checked against the ENCLOSING scope. An undeclared
outer ref still fails exactly as before, so correlation is opt-in per subquery and
the pre-106 uncorrelated model is the default. A scalar subquery must be an
aggregate with no `group_by`, making exactly-one-row true by construction. A scalar
subquery in a SELECT **projection** is deliberately NOT part of this — it composes
from item 105's cte + LEFT JOIN, and the recipe is recorded in the Decision Log.
Closes regression bar row 11 -> **15/16**, completing Phase 4 of
`docs/ENGINE_EXPRESSIVENESS_PLAN.md`. 13/13 enforcement points mutation-verified.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 106).

---

## Findings from the 2026-07-23 technical/product review

Items 107–113 came out of a full-repo due-diligence pass (see
`TECHNICAL_REVIEW.md` for the full write-up and evidence). Item 93's own
compensation-store regression is tracked inline in item 93's Phase 3b note
above, not here, since it's the same feature. These are otherwise-solid,
narrowly-scoped fixes/hardenings the review surfaced — not a restatement of
already-tracked open work.

### 107. Batch query execution double-reserves quota on an approval retry ✅ DONE

An in-session batch approval retry now reuses the quota reservation the paused
first attempt already made instead of reserving a second unit. The live
reservation is stashed on the `ApprovalRequiredError` as it leaves `execute()`
and threaded back into the retry via a private `_reserved_quota` parameter, so
one approved query consumes exactly one quota unit.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 107).

### 108. Write-preview diff runs the full DML before the affected-row cap is checked ✅ DONE

An over-cap `include_diff=true` UPDATE preview no longer executes the real
row-locking DML: `_mutation_diff` takes a `within_cap` flag and falls back to the
existing Python-applied-SET path, so the caller still gets a bounded diff while
no DML reaches the database.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 108).

### 109. MCP `run_structured_writes` has no batch-size cap ✅ DONE

Added `WritePolicy.max_batch_size` (default 10, mirroring `Policy.max_batch_size`)
and `validate_write_batch_size`, enforced both at the MCP tool (before the
preview/execute branch, so no statement is touched) and inside
`WriteExecutionService.execute_many` (so the service layer is bounded regardless
of transport).

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 109).

### 110. `value_subquery` in a write's WHERE is validated at the wrong layer ✅ DONE

`validate_write_policy` now rejects a `value_subquery` predicate anywhere in a write's WHERE with a clean `QueryValidationError` at validation time (walking the whole boolean tree), instead of failing deep in the write compiler's `ctx=None` path.
**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 110).

### 111. Duplicated WHERE-predicate tree walk across four validators ✅ DONE

Extracted one shared `iter_where_predicates` (in `schema_validation.py`, beside item 96's `iter_column_refs`) consumed by all four read/write policy/schema validators; the four hand-rolled copies + their now-unused imports were deleted. No behavior change (full + security suites pass unchanged; two direct contract tests added).
**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 111).

### 112. No scheduled (cron) CI run — dependency/security scans only fire on push/PR ✅ DONE

Added `.github/workflows/scheduled.yml` — a nightly (07:00 UTC) + `workflow_dispatch` workflow running the CVE/SBOM/lockfile audit, a Trivy image scan, and `make test-soak` against `main` independent of code changes; cadence documented in `docs/RELEASING.md`.
**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 112).

### 113. No metrics for the write-undo / compensation-store feature ✅ OBSOLETE (2026-07-23)

**Obsolete: the write-undo/compensation store was removed** (item 93,
2026-07-23 — see the `docs/PRODUCT_GUIDE.md` Decision Log). There is no
compensation store or undo path left to instrument, so this observability gap no
longer exists. If write-*execution* metrics are wanted later, that is a fresh,
separately-scoped item (not undo-specific).


### 114. The write tool's MCP schema advertised read-only predicate fields it rejects ✅ DONE

A write's `where` reused the READ `Predicate`, so `run_structured_writes` inlined
item 100's whole `Expression` union, item 101's window nodes and the entire read
`StructuredQuery` — all rejected at runtime. The write AST now has its own
narrowed `WritePredicate`/`WriteWhereGroup`, converted to the read `WhereNode` at
the validation boundary so there is still one predicate walk and one compiler.
The MCP context budget DROPPED 19% (128,551 -> 104,042; the write tool -63%), and
a fifth hand-rolled predicate enumerator in `write_preview.py` was deleted as
unreachable.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 114).

### 115. The hand-maintained guardrail-field lists had drifted from `Policy` ✅ DONE

The four surfaces that report "which caps are in force" (the semantic access
diff, the effective-guardrails view, the help API, the admin UI panel) each kept
their own field list; nine caps from items 68–72/88/97/100/101 were missing from
at least one, so a config change loosening one was diffed as "no guardrail
change". They now derive from `Policy` itself, `EffectiveGuardrails` is generated
from it, `approval_sensitivities` gets its own diff change, and a new cap must
state its direction (`min_group_size` and `quota_window_seconds` run the opposite
way) or a test fails.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 115).

### 116. A write's WHERE was exempt from every shape cap the read path enforces ✅ DONE

`validate_write_policy` enforced none of `max_where_depth`,
`max_where_predicates` or `max_in_list_size`, so `delete … where id in [<huge
list>]` was rendered in full client-side (measured: 100,000 values -> a 689 KB
statement) before `max_affected_rows` was consulted — and past the driver's
parameter limit it failed in the driver rather than being refused cleanly. Each of the three rules is now a single
function both paths reach (verified by spying on each one), and the write path
applies them — to every statement of a batch up front — before any DML compiles,
proven by observing that no statement reaches the database.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 116).


### 117. `date_bucket` over a non-temporal column diverges across dialects ✅ DONE

Extended item 102's operand rule to the third date primitive, so all three go
through one shared walk with a coverage test guarding a future fourth.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 117).

### 118. `min_group_size` was defeated by any fan-out join ✅ DONE

The k-anonymity floor counted JOINED rows, so any join matching many right rows
per left row lifted a singleton group above *k*. Now refused at compile time —
precisely: a join onto the target's primary key or a unique column cannot inflate
a count and still runs.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 118).


### 119. `top_n` mis-resolves and DROPS a column when two projections share a base name ✅ DONE

`top_n` now binds derived-table projections and rank references positionally, so same-named columns remain distinct and ordering targets the column the caller named.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 119).

### 120. The audit shape records nothing for a nested `IN (subquery)` ✅ DONE

Nested `value_subquery` scopes now appear recursively in persisted redaction-safe query shapes, including their tables, joins, and predicate structure but never their literal values.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 120).

### 121. Report-only surfaces still assume a query has one scope ✅ DONE

`ExplainResult.tables` and the candidate simulator's table set now derive from
every scope (`referenced_tables_tree_wide` / the populated `scope_tables` map), so
a set-op arm's or subquery's tables no longer go unreported while the same
response's `sql` names them.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 121).

### 122. `_unique_column_sets` crashed on any FROM element that is not a `Table` ✅ DONE

Item 118's k-anonymity fan-out check ran on the joined table and reached for
`.primary_key.columns`, which only a `Table` has — so `min_group_size` plus any
join carrying an `alias` raised `AttributeError` instead of deciding. An alias now
looks through to its element; a cte/subquery reports no uniqueness and is treated
as able to fan out (fail-closed).

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 122).

### 123. A select-item `CASE`'s condition subtree is absent from the audit shape ✅ DONE

`_select_shape`'s `CaseSelectItem` branch now additively records `"conditions":
[_where_shape(branch.when) for branch in item.when]`, mirroring `CaseExpr` in
`_expression_shape`, so a nested `value_subquery` in a select-item CASE condition
is no longer invisible to the audit event. **Full write-up:**
[docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 123).

### 124. Most of `tests/unit/` is not selected by `pytest -m unit` ✅ DONE

A directory-derived `pytest_collection_modifyitems` hook now tags every test
under `tests/{unit,integration,security}/` with its tier automatically, so a
file can never again be silently invisible to `pytest -m unit` or the
pre-commit gate. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md)
(item 124).

### 125. Query engine: a window function as an `Expression` operand ★ ✅ DONE

A `WindowExpr` joins the closed `Expression` union, so `amount / SUM(amount) OVER ()`
is one statement instead of two projected columns plus client-side arithmetic. It is
the one member not legal everywhere a scalar is expected — legal in a projection and
nowhere else — enforced by a single fail-closed positional rule rather than a parallel
projection-only union. Closes regression-bar row 15 -> **16/16**, meeting the flagship
pillar's success criterion. 4/4 enforcement points mutation-verified.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 125).

### 126. No per-caller rate limit on `GET /help/my-recent-denials`

**Surfaced 2026-07-30 by the `auditors` security-invariant review of item 45
phase 2, not a regression in this pass.** `GET /api/v1/help/my-recent-denials`
requires only authentication (no scope, matching `/help/my-access`'s posture),
and its handler calls `admin/anomaly.py`'s `JsonlAuditEventSource`, which reads
and JSON-parses every line of the audit JSONL file per call (the
`max_events_scanned` cap only bounds what's *kept in memory*, not how much of
the file is scanned). Every other caller of that reader (`GET
/admin/observability/anomalies`) requires `admin:observability:read`; this is
the first time the same O(file-size) scan becomes triggerable by *any*
authenticated caller, and there is no REST-level rate limit anywhere in the
codebase to bound repeated calls.

**Why not fixed inline:** the review explicitly judged this consistent with —
not worse than — the existing posture of every other self-service endpoint
(`/help/my-access`, schema browsing), none of which are rate-limited either;
adding a new throttling mechanism is a deliberate product/design decision
(scope, default interval, whether it should be per-endpoint or
codebase-wide), not a small safe fix to make unprompted.

**What to do (when prioritized):** either (a) add a lightweight per-principal
cooldown scoped to this endpoint, mirroring item 43's existing
`admin_connection_test_cooldown_seconds` precedent (a new
`personal_denials_cooldown_seconds` config field + a 429/Retry-After response
inside the window), or (b) make a recorded decision that the existing
no-REST-rate-limiting posture is acceptable for this class of bounded local
file read and close this as will-not-build. Either resolves it; doing neither
leaves the residual undocumented.

