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
| 19 | ✅ Additional dialects (MySQL live-verified; Snowflake/BigQuery rendering-only, not live-verified) | L–XL (per remaining dialect) | 2 (do MSSQL first) |
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
| 45 | ✅ Dedicated non-admin "My access" portal (phase 1: identity, guardrails, mandatory-filter readiness, schema browser; phase 2: personal denial history via `GET /help/my-recent-denials`) | M | 22, 31, 33 |
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
| 93 | ✅ Governed Writes — structured, bounded, previewable, governed agent mutations (governance tier shipped: preview/diff, gated execution, approval, batch, upserts; reversibility/undo REMOVED 2026-07-23; `release-smoke` write round-trip shipped) | XL | 25, 48, 90, 91 |
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
| 126 | ✅ No per-caller rate limit on `GET /help/my-recent-denials` | S | 45 |
| 127 | ✅ Reject an MCP request whose routing headers disagree with its body | S–M | 86 |
| 128 | ✅ Conform to the final MCP `2026-07-28` protocol revision | L | 90, 92, 93 |
| 129 | ✅ Never advertise a principal-varying MCP result as shared-cacheable | S | 128 |
| 130 | ✅ Annotate `connection` with `x-mcp-header` for gateway-native authorization | S | 127, 128 |
| 131 | ✅ Publish the StructuredQuery AST as a namespaced MCP extension | M | 128 |
| 132 | ✅ Reconcile stale shipped-status claims left behind by items 90–93 | S | — |
| 133 | ✅ Caller-facing quota-metered verdict endpoint (play P4) — reuses 31/39's decision logic | M–L | 26, 31, 39, 45, 121 |
| 134 | ✅ Compliance-grade (WORM) audit retention + managed search | L | 91, 136 |
| 135 | ✅ Automatic (TTL/lease-driven) credential re-resolution, without an operator reload | M | 13 |
| 136 | ✅ `jsonl_chained` audit backend silently disables four shipped read surfaces | S–M | 91 |
| 137 | ✅ Audit read surfaces neither verify nor disclose hash-chain integrity | S–M | 91, 136 |
| 138 | ✅ Audit read surfaces scan the entire persisted file on every request, unbounded by lines read | S–M | — |
| 139 | ✅ Bound audit-line size at the source (AST list caps + audit/sinks.py's own unbounded-read defect) | M | 138 |
| 140 | ✅ `_audit_page` pagination can still materialize ~1M dicts per request | S–M | 138 |
| 141 | ✅ Convert audit-reader line caps into practically-tight window-based early exits | S | 138 |
| 142 | ✅ `docs/THREAT_MODEL.md` uses the ID `QG-32` for two unrelated threats | XS | — |
| 143 | ✅ `cryptography` 49.0.0 has an unreviewed CVE, blocking `make release-check`'s SBOM step | XS–S | — |
| 144 | ✅ `verdict()` emits no query metrics, and `/metrics` is unauthenticated | S | — |
| 145 | ✅ Purpose-bound access: enforce the declared `intent`, don't just log it (feature F7) | M | — |
| 146 | ✅ "5-minute first governed query" quickstart — close the named Toolbox onboarding gap | S–M | 48, 51 |
| 147 | ✅ Self-serve procurement evidence page | S | 54, 58, 60 |
| 148 | ✅ `admin/access_diff.py` never diffs `column_masks` at all | S | — |
| 149 | ✅ `Policy`'s case-insensitive table-key lookups disagree on `casefold()` vs `lower()` | S–M | — |
| 150 | ✅ `compiler/sqlalchemy_compiler.py`'s `mandatory_row_filters` matching uses `.lower()` vs `schema_validation.py`'s consistent subsystem | M | — |
| 151 | ✅ Bind the in-query approval gate's token to a connection and principal, not just an AST fingerprint | M | 92, 128 |
| 152 | ✅ Sales/landing pages don't reflect items 19 (MySQL)/134 (WORM retention) shipping | S | 19, 134 |
| 153 | ✅ `CHANGELOG.md` has no `[Unreleased]` entry for items 19 (MySQL) or 134 (WORM retention) | S | 19, 134 |
| 154 | ✅ WORM archive segments are unenveloped, so managed search cannot verify a segment was actually written by QueryGate | M | 91, 134 |
| 155 | ✅ `sensitivity_approval_reasons` looks up every table in the query's top-level connection's catalog, never a cross-connection join's own connection | M | 151 |
| 156 | ✅ A cross-connection join's joined table is governed only by the primary connection's Policy — masks/row filters/deny-lists never apply from the joined connection's own Policy | M/L | 155 |
| 157 | Snowflake live-server verification and deeper feature parity (item 19 phase 2 residual) | L–XL | 19 |
| 158 | ✅ `ConnectionProfile` never validates `dialect` agrees with `connection_string`'s actual backend | S–M | — |
| 159 | ✅ Cross-connection schema reflection can pick the wrong connection when a join's alias casing differs from a column ref's casing | S | — |
| 160 | ✅ Item 156 follow-up: harden the connection-resolution edge cases a full security-invariant audit surfaced (findings 1, 2, 4 fixed; finding 3 open — needs a maintainer decision) | M | 156 |
| 161 | BigQuery live-server verification and deeper feature parity (item 19 phase 3 residual) | L–XL | 19 |
| 162 | Dialects beyond MySQL/Snowflake/BigQuery (item 19's open-ended "…" scope) | unscoped | — |
| 163 | ✅ A not-connectable dialect (Snowflake/BigQuery) as the SECONDARY side of a cross-connection join never reaches the `is_connectable()` guard | S–M | — |
| 164 | ✅ `column_mask`'s HASH branch is an implicit `else`, not an exhaustive match, on all five `DialectAdapter`s | S | — |
| 165 | ✅ `/admin/reload-config`'s generic exception handler can leak a live credential in its HTTP 400 body | S | — |
| 166 | ✅ Cross-connection self-join reflects both aliases against ONE connection — the `physical_tables` reflection memo ignores which connection a name resolves to | S–M | 159 |
| 167 | ✅ A case-different column ref to a joined alias leaves a phantom second `sa.Table` alias that a mandatory row filter turns into an implicit cross join (confirmed) | S | 159 |
| 168 | ✅ Config-governance dry-run's credential-safety net is a post-hoc regex scrub, not structural, and the validate-config CLI's stderr isn't scrubbed at all | S–M | 165 |
| 169 | ✅ A correlated subquery's `correlate` ref binds to a phantom alias object by exact dict index, which can silently turn an EXISTS/scalar subquery into an unfiltered scan | S–M | 106, 167 |
| 170 | ✅ Cross-connection joins are reflected as if both connections are always on the same physical server instance, with nothing that actually checks it | S–M | — |
| 171 | ✅ The audit windowed early-exit (item 141) can silently under-report on a merged/multi-writer file, with no disclosure field or way to tell caller-facing consumers apart | M | 141 |
| 172 | ✅ WORM archive segment verification checks each record's own hash but never the chain's linkage within a segment | M | 154 |
| 173 | ✅ Cross-connection connection-resolution is unmemoized per join, redone on every call site that self-derives | S | 160 |
| 174 | ✅ A cross-connection join's secondary-connection schema qualifier is a hardcoded MSSQL `.dbo` idiom, with no dialect dispatch | S | 163 |
| 175 | ✅ `test_mssql_write_execution.py` leaks real aioodbc connections across tests, intermittently failing CI with "Connection is busy with results for another command" | S | 2 |
| 176 | ✅ Three claim-accuracy drifts found while fixing the item-134 stale WORM-search line: `sales/index.html`'s guardrails still forbid claiming managed search, `CUSTOMER_README.md` flatly denies it exists, and `TODO.md`'s own Quick-scan row for item 134 says phase 2 "not started" | S | 134 |
| 177 | ✅ `WormSearchResult.chain_breaks`/`unverified` have no Prometheus counter, so the strongest WORM-archive tamper signal isn't alertable — only visible to whoever happens to run an ad-hoc search over the right window | S | 172 |
| 178 | ✅ A hash-verified WORM record with a non-int `seq` (type-confused, not corrupt) raises `TypeError` instead of being counted `unverified` | S | 172 |
| 179 | ✅ Cumulative disclosure budget: bound multi-query differencing per purpose — the one structural form of "governing intent" | L | 88, 145 |
| 180 | Escalate an exhausted disclosure budget into the item-92 approval gate instead of rejecting | M | 92, 179 |
| 181 | `redis_quota.py`'s `Retry-After` is always the full window — the Lua indexes a nested `WITHSCORES` reply | S | 50 |
| 182 | Observe mode for the disclosure budget — measure before you enforce | S–M | 179, 26 |
| 183 | Suggest a disclosure-budget threshold from observed behavior, for human approval | M | 182 |
| 184 | ✅ A day holding more segments than `max_objects_scanned` returns a cursor that never advances, so part of the WORM archive is unreachable, a good-faith pager loops forever, and item 177's integrity counters inflate without bound | M | 134 |
| 185 | ✅ `AUDIT_WORM_SEARCH_REQUESTS_TOTAL{outcome="rejected"}` is unreachable for the bound rejections its own comment claims to count, because `build_worm_search_result` validates before calling `search_worm_archive` | S | 134 |
| 186 | The disclosure budget's per-shape cap is evadable — a select-alias *reference* mints a fresh shape bucket per probe (measured: 20 probes, 20 fingerprints), so `max_shape_repeats_per_window` never trips; CTE-rename, nested-alias and list-order vectors share the root cause | M | 179 |
| 187 | A disclosure-budget refusal tells the caller which cap tripped, its configured value and the window length, contradicting the exception's own stated contract and handing over item 186's evasion strategy | S | 179 |
| 188 | A principal policy override can fail `Policy` validation at request time and 500 every query for that principal, after `validate-config` accepted it | S | 179 |
| 189 | Test-contract gaps in the item-179 disclosure budget and item-177 WORM counters: charge weight, 3 of 5 volatile shape keys, Redis-limiter wiring, observability aggregation, and five more | M | 177, 179 |
| 190 | Four claim drifts on outward-facing surfaces unrelated to WORM search: MSSQL cost estimation, four-eyes approval + admin UI, a README self-contradiction, and the unconditional "resumable page" claim | S | — |
| 191 | `write_preview` is an unfloored, unbudgeted exact-count oracle, so a write-scoped caller can difference around items 88 and 179 | S–M | 93, 179 |
| 192 | The disclosure budget's Redis script passes multiple KEYS, which fails CROSSSLOT on Redis Cluster — turning a fail-closed control into an outage on the queries it protects | S | 179 |
| 193 | ✅ `docs/product-guide.html` has no freshness gate against `docs/PRODUCT_GUIDE.md`, so the generated copy most likely to be shared goes stale silently | S | — |
| 194 | ✅ Three crafted-or-corrupt WORM lines still escape `search_worm_archive` as a masked 500: a non-ASCII `hash` (reachable by ordinary corruption) and two unbounded recursions | S–M | 134 |
| 195 | ✅ Narrow a principal from the general query surface to reviewed templates: `Policy.templates_only` enforcement plus an opt-in, redaction-safe observed-shape recorder (in-process + Redis-backed) that drafts a template from real traffic, with an admin-UI promotion panel | M–L | 48 |
| 196 | ✅ The container image's non-Python layers have never been licence-assessed: `docs/THIRD_PARTY_LICENSES.md` covers `poetry.lock` only, while the shipped image also carries a Debian `bookworm` userland and Microsoft's `msodbcsql18` under `ACCEPT_EULA=Y` | M | — |
| 197 | Offline entitlement token for the paid tier (not-before-customers) | S | — |
| 198 | QueryGate Notary — append-only transparency log for the audit ledger's chain head (not-before-customers) | M | — |
| 199 | ✅ Human SSO: OIDC authorization-code sign-in for the browser surfaces across eleven provider presets, a built-in local identity provider (scrypt + TOTP) for air-gapped and break-glass use, a deny-by-default file-configured claim→scope mapping, CSRF-bound sessions, and an RFC 8628 device grant for CLI callers | L | 10, 90, 95 |
| 200 | ✅ Per-surface credential-type policy: an allowlist over `Principal.auth_method` for the console / REST / MCP surfaces, whose default closes the admin control plane to static API keys the moment SSO is enabled | S–M | 199 |
| 201 | ✅ The WORM archive tier is reachable only against AWS S3 (`endpoint_url` is never set), so on-prem/air-gapped deployments cannot have the immutable copy at all | S | 134 |
| 202 | The `digests_equal` non-ASCII hazard is unfixed at six `hmac.compare_digest` sites outside `audit/ledger.py` (TOTP code, OIDC state/nonce, CSRF token, PKCE challenge), turning a clean 401/403/422 into a masked 500 | S | 194, 199 |
| 203 | An unparseable predecessor exempts one chain link on a resumed page (accept-as-given), while the blank-run walk fails closed for the same threat shape | S | 172, 194 |
| 210 | ✅ Proprietary licence transition: retire the BSL apparatus (LICENSE body, EULA becomes the licence of record with subscription clauses, Change-Date targets + script + test + both CI call sites, and ~30 documents asserting a source-available future) | M | — |
| 211 | ✅ Subscription layer — the entitlement gate: two enforcement funnels (reads via `_validate_and_compile`, writes at the three write *service* entry points), 402 registered in seven registries, observe/enforce as a signed field, wall-clock high-water mark and serial floor | L | 210, 212 |
| 212 | Control plane — accounts, Stripe subscriptions, KMS-signed entitlement issuance, enrolment, append-only issuance ledger, in `control-plane/` with its own lockfile and mirror CI gates | XL | — |
| 213 | Activation — bind a deployment to a subscription via OAuth2 (Google/Microsoft) + MFA, browser and device flows, control-plane-assigned `deployment_id`, unactivated deployments inert | L | 199, 212 |
| 214 | Single obfuscated compiled binary — Nuitka feasibility spike first (pydantic-core, SQLAlchemy dispatch, MCP annotation resolution), then reproducible build preserving cosign + SLSA provenance | XL | — |
| 215 | One-command install and first-boot self-configuration — no operator-authored file needed to reach activation; safe-by-default starter policy; first connection added through the UI | M | 213 |
| 216 | Renewal countdown and lapse UX — banner with day countdown in both UIs under 30 days when auto-renew is off, email, coarse health field, metric, CLI line; accessible by construction | M | 211 |
| 217 | Customer portal — OAuth2 signup, Stripe Checkout, subscription and deployment management, cancellation flow stating the no-refund terms before confirming, downloads and docs | XL | 212 |
| 218 | Setup guides and quickstart docs for the SaaS motion — one-screen quickstart, per-target deploy guides, air-gapped guide, troubleshooting, rewritten `CUSTOMER_README.md` and landing/sales copy | M | 215 |
| 219 | Pre-launch codebase cleanup pass — `repo-audit`, `dep-audit`, `test-gap`, `claim-verify`, `security-invariant-check`; delete BSL dead code; close open defects 192 and 194; full CI matrix green | L | 210 |
| 222 | The product-guide HTML generator emits a document fragment — no doctype, `lang`, `charset` or viewport meta, so the generated customer-facing page fails WCAG 3.1.1 and its own mobile breakpoint never fires; plus a missing skip link, a split Decision Log list, and the phone-home scan not covering the two shipped `.js` files (load-bearing at item 216) | S | — |
| 221 | Move validator bodies out of the model classes into compilable sibling modules — 30 validators / 602 lines of enforcement logic (join form, window scope, CTE names, set ops, credential shape) currently ship readable because a module defining `BaseModel` cannot be Cython-compiled | M | 214 |
| 220 | Deny-by-default at table and column granularity: a `Policy` allow-list with no allow-all fallback, so an empty `allowed_tables` denies instead of allowing. Opt-in (default off) so no existing deployment changes behaviour; the starter policy turns it on | M | — |

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
`SHA256SUMS` checksum manifest covering the wheel, sdist, SBOM, and (since the GTM
WP1 licence gate) `THIRD_PARTY_LICENSES.md`.

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

### 44. Admin observability and rejection-trend dashboard ✅ DONE

Aggregated operational-trend API + admin-UI panel over the in-process metrics
registry (phase 1), a durable config/catalog change-velocity trend card over
the persisted audit stream (phase 2 slice), and real time-window trend charts
from an operator-configured external metrics backend (Prometheus HTTP API,
phase 2 remainder). **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 44).

### 45. Dedicated non-admin "My access" portal ✅ DONE

A separate, dependency-free `/access/` static page showing identity, visible connections, effective guardrails, mandatory-filter readiness, and policy-filtered schema (phase 1); phase 2 added safe, principal-scoped explanations of the caller's own recent denials (`GET /help/my-recent-denials`). **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 45).

### 46. Validated policy templates and safe-start presets ✅ DONE

Five fixed, code-reviewed presets (`querygate/admin/templates.py`): `deny-by-default`, `reporting-only`, `customer-support`, `tenant-isolated`, `bounded-analytics`. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 46).

### 47. Safe draft recovery plus config export/import UX ✅ DONE

A portable change-set bundle for export/import + policy-only local recovery (phase 1); phase 2 added a server-side, encrypted-at-rest draft store with per-principal ownership, retention, and deletion controls. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 47).

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

**Scoping/design proposal (2026-08-11, not yet approved for implementation):**
[docs/STORED_PROCEDURE_CATALOG_PLAN.md](docs/STORED_PROCEDURE_CATALOG_PLAN.md)
— why this can't reuse `catalog/`'s reflection-backed pattern, a proposed
model/registry shape mirroring `connections/`+`policy/`, a proposed 3-4 phase
breakdown, and five explicit open decisions (risk classification granularity,
approval-token reuse, whether any preview/dry-run concept is safe for an
arbitrary procedure, whether the declaration itself needs a review gate,
multi-result-set procedures) that need resolving before implementation
starts, not defaults to reach for mid-build.

### 19. Additional dialects (MySQL, Snowflake, BigQuery) ✅ DONE

MySQL shipped fully live-verified (phase 1); Snowflake and BigQuery shipped
rendering/compilation-only, explicitly NOT live-verified (phases 2–3) —
follow-up items 157 (Snowflake) and 161 (BigQuery) track closing that gap,
and item 162 tracks any dialect beyond these three.
**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 19).

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
quota per-replica unless the Redis quota backend is configured (item 50 phase
2 shipped the mechanism, opt-in); config/audit per-replica unless
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
**16/16 (100%)** structural boundary attacks vs. a structurally-modeled
raw-SQL-passthrough baseline at **0/16 (0%)**, with **2** documented
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
  `not_a_server_error` + `negative_data_rejection`. Latest: 1755/1755 checks
  across 72 operations, 0 server errors, 0 accepted malformed payloads (see
  `docs/SECURITY_POSTURE.md`'s "Dynamic analysis (DAST)" section for the
  current figure — this number drifts as the API surface grows, don't quote
  it from memory). The recursive
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
real Postgres + real MSSQL + the shipped image + a concurrency load gate**. One
**reasoned deferral** remains (not "not started" — deliberate, recorded):
approval-binds-to-diff-hash (over-engineering vs the current fingerprint
binding). (An earlier "upsert-undo" deferral is moot: reversibility/undo was
removed entirely on 2026-07-23 — see below — so there is no undo mechanism
left for upserts to be a special case of.)

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

**Phase 2b–3 planning note — superseded, kept for history only.** This
paragraph originally scoped phase 2b/3 as "not started." Every item it listed
has since shipped (row-level diff preview + approval, the MCP
`run_structured_writes` execute tool, the adversarial write security suite,
the `release-smoke` write round-trip, the write concurrency load gate,
upserts, atomic batch, MSSQL parity, constraint pre-validation) or was
deliberately removed (reversibility/compensation — see the 2026-07-23 removal
note above). See "Comprehensively shipped" at the top of this item and the
phase 2a/2b sections above for what actually shipped.

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
9. **`execution/compensation.py` — bounded compensation/undo — REMOVED
   2026-07-23, do not point an implementer here.** This was originally planned
   (and briefly shipped, see the Phase 3a/3b history below) as: before a gated
   mutation commits, capture a redaction-aware **pre-image snapshot** of the
   affected rows (bounded by policy rows/bytes/TTL) and emit a governed rollback
   operation that re-applies the pre-image under the same pipeline. The module,
   `execution/redis_compensation.py`, `POST /write/undo`, and the MCP
   `undo_structured_write` tool were all deleted in the 2026-07-23 reversibility
   removal (Decision Log, `docs/PRODUCT_GUIDE.md`) — undo forced a second copy
   of real row values outside the customer's DB, against the data-never-leaves
   North Star. There is no file at this path today.
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

### 126. No per-caller rate limit on `GET /help/my-recent-denials` ✅ DONE

A per-principal cooldown (`PersonalDenialsCooldown`,
`AppConfig.personal_denials_cooldown_seconds`) now bounds repeated calls,
with a metrics counter on the 429 path and disclosed per-process/shared-API-
key-bucket limitations.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 126).

### 127. Reject an MCP request whose routing headers disagree with its body (gateway confused-deputy) ✅ DONE

`mcp/transport_guard.py`'s `MCPRequestGuardMiddleware` now rejects a request
whose present `Mcp-Method`/`Mcp-Name` header disagrees with the body's
`method`/`params.name`/`params.uri` (HTTP 400 + JSON-RPC `-32020
HeaderMismatch`), closing the confused-deputy gap where a fronting gateway
authorizes on the header while QueryGate executes the body — checked strictly
after item 86's depth scan, with the spec's Base64 sentinel encoding decoded
before comparison. Validate-if-present, not required, since QueryGate speaks
protocol `2025-11-25` (item 128) which doesn't yet define these headers.
**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 127).

### 128. Conform to the final MCP `2026-07-28` protocol revision ✅ DONE

Shipped 2026-08-06: full `mcp` SDK v1 → v2 migration (`FastMCP` → `MCPServer`,
`mcp = ">=2.0.0"`) including the Multi Round-Trip Requests port of items
92/93's approval/elicitation flow, mutation-verified against a
fingerprint-swap replay attack.
**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 128).

### 129. Never advertise a principal-varying MCP result as shared-cacheable ✅ DONE

`mcp/caching.py`'s `install_private_cache_scope` forces `cacheScope: "private"`
(SEP-2549) onto `tools/list`/`prompts/list`/`resources/list`/`resources/read`
unconditionally and structurally (not a per-registration opt-in), landing
on top of item 128's `mcp` SDK v2 migration.
**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 129).

### 130. Annotate `connection` with `x-mcp-header` so a fronting gateway can authorize per-connection without parsing the body ✅ DONE

Every tool's `connection` parameter (`run_structured_queries`, `list_tables`,
`describe_table`, `search_catalog`, `run_structured_writes` — the five with a
top-level `connection` arg; `run_query_template`/`list_connections` legitimately
have none) carries the `x-mcp-header: "Connection"` JSON-schema annotation via
`Field(json_schema_extra=...)`, verified to survive FastMCP's pydantic ->
`model_json_schema()` pipeline into the emitted `inputSchema`. A conforming
`2026-07-28` client mirrors it into `Mcp-Param-Connection`, which
`mcp/transport_guard.py`'s item-127 guard now also validates against
`params.arguments.connection` (a same-day `auditors` finding: the spec's
header/body MUST-reject rule isn't scoped to `Mcp-Method`/`Mcp-Name`, so
shipping the annotation without this reopened the exact confused-deputy gap
item 127 closed). Documented in `docs/PRODUCT_GUIDE.md`'s MCP section,
README.md, a Decision Log entry, and each field's own module comment as a
routing hint only (never a substitute for `resolve_visible_connection`) that
is explicitly non-exhaustive — it mirrors `params.arguments.connection` only,
so it says nothing about a `JoinSpec`'s own `connection` for a cross-database
join, which stays bounded by `join_group` policy, and three tools take no
`connection` argument at all so emit no header. Scope held to `connection`
only; no query/write AST field is annotated (verified by walking every
`$defs` entry, not just top-level tool arguments). Ships independently of
item 128 (still open in this lineage): both the annotation and the header
check are inert `inputSchema`/dormant-header logic under the current
FastMCP/`mcp>=1.28.1` pipeline, forward-compatible with that SDK migration
rather than blocked on it.
**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 130).

### 131. Publish the StructuredQuery AST as a namespaced MCP extension ✅ DONE (internal half)

**Shipped (internal half) 2026-08-06.** The namespace is reserved
(`io.github.agitmit/structured-query-ast`, matching this project's actual
GitHub location rather than presupposing a domain QueryGate doesn't own), the
spec is written (`docs/mcp_extensions/structured_query_ast.md`, with an
explicit Non-goals section stating the hard boundary below), the schema is
generated — never hand-written — from the live Pydantic AST models
(`mcp/extensions.py`'s `generate_structured_query_ast_schema()`, covering
both the read `StructuredQuery` and the write
`Insert`/`Update`/`Delete`/`Upsert` union) into a committed, versioned file
(`docs/mcp_extensions/structured_query_ast.schema.json`, regenerated via
`make mcp-extension-schema`), and the extension is declared from the real
`MCPServer` instance's capabilities (`mcp/server.py`'s `mcp_server =
MCPServer(..., extensions=[StructuredQueryAstExtension()])`). A conformance
test (`tests/unit/test_mcp_extensions.py`) asserts all three plus the hard
boundary structurally: the committed schema matches a fresh generation
byte-for-byte (drift guard, same technique `test_credential_redaction.py`
uses), the extension is genuinely present in a real server's emitted
`ServerCapabilities.extensions`, and no JSON-RPC method is registered
anywhere under the extension's namespace. **Not done, and deliberately
gated on a maintainer decision:** actually publishing this as an adopted
external standard (registering the namespace with any outside body,
announcing it, or committing to cross-version compatibility for third
parties) — no network call or external registration was made implementing
this. See `docs/mcp_extensions/structured_query_ast.md`'s "Publication
status" section.

**Surfaced 2026-07-30 by `competitive-scan`.** The `2026-07-28` revision adds a
formal **extensions framework** with reverse-DNS namespacing — the tasks
feature moved out of experimental core into `io.modelcontextprotocol/tasks`
under it. This gives strategic play P2 ("open the contract — publish the
StructuredQuery AST as an open standard",
`docs/business/MARKET_DOMINATION_ANALYSIS.md` §7) a standards-blessed vehicle
it previously lacked: the AST can be declared as a named, versioned MCP
extension rather than a product-specific JSON schema.

**Why it matters.** The durable moat is the *contract*, not the
implementation. A named extension is citable in a security review, gives
gateway and client authors something to implement against, and makes
"structured, not raw SQL" a thing others can adopt on our terms — while every
enforcement decision stays in our pipeline. It also directly answers the
convergence risk the scan keeps flagging: Cube and Microsoft DAB now use our
messaging, so owning the *artifact* matters more than owning the phrase.

**Scope for the code half (safe to do):** author the extension specification in
`docs/`, pick and reserve the namespace, and declare it from the MCP surface's
capability/`_meta` metadata with a conformance test.

**Hard boundary — the extension declares a *contract*, never a *method*.** It
MUST NOT introduce a namespaced JSON-RPC method that accepts a query. That would
be a second query-execution path beside `tools/call` and the one pipeline —
the identical rejection class as the GraphQL decision (`docs/PRODUCT_GUIDE.md`
Decision Log, 2026-07-22) and as `execute_sql`. Note the precedent this item
cites, `io.modelcontextprotocol/tasks`, *does* define methods — so the obvious
reading of "implement an extension" is exactly the wrong one here. State the
exclusion in the spec document itself.

**Generate the published schema; do not hand-write it.** `query_ast/models.py`
is the source of truth, and `clients/typescript/src/types.ts` is already a
hand-mirrored second copy carrying known drift. A hand-authored spec would be a
third — and unlike the in-tree TS client, where drift is a local bug, drift in a
*published* standard becomes a compatibility commitment. Emit the schema from
the Pydantic models (`model_json_schema`) and have the conformance test assert
generated == published, the same live-schema technique
`tests/unit/test_credential_redaction.py` uses for the credential invariant.

**Decision-gated for the publication half:** actually publishing an external
standard is a commitment (versioning, compatibility, community process) and an
outward-facing act — maintainer's call, not an agent's. Do not publish
externally as part of implementing this; `competitive-scan` and `pitch-sync`
are draft-only by charter.

**Effort:** M (internal half). **Depends on:** 128.

### 132. Reconcile stale shipped-status claims left behind by items 90–93 ✅ DONE

Fixed the drift across `GO_TO_MARKET.md`, `README.md`, and `TODO.md` itself
left behind by items 90–93 (and, surfaced along the way, items 45/50/56/58)
describing shipped capability as open or partial; a post-build claim-reviewer
audit caught three further stale spots in the same pass.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 132).

### 133. The verdict endpoint — expose the decision without the execution (play P4) ✅ DONE

Caller-facing `POST /{connection}/query/verdict` (REST) and MCP
`run_structured_queries(mode="verdict")` answer "would this query be
allowed?" without executing it, hardened across two `auditors` rounds into a
fail-closed (not type-allow-listed) anti-oracle collapse.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 133).

### 134. Compliance-grade (WORM) audit retention + managed search ✅ DONE

Phase 1 archives redaction-safe audit events to S3 Object Lock alongside the
local hash-chained ledger (`AuditSinkBackend.JSONL_CHAINED_S3_WORM`); phase 2
adds `GET /api/v1/admin/observability/worm-search`, a bounded/filtered/
paginated search directly over that archive, gated by its own
`admin:audit:worm-search` scope.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 134).

### 135. Automatic credential re-resolution (TTL/lease-driven), without an operator-triggered reload ✅ DONE

Closed the genuine gap left after item 13 (which already made a rotated
`${vault:...}` value take effect on the next reload without a restart): the
refresh was operator-pull only, so a short-TTL leased credential could expire
into failures between reloads. `CredentialLeaseMonitor`
(`config_reload.py`) now proactively triggers the existing reload/dispose
machinery ahead of a reported lease expiry, via a new optional
`LeasedSecretResolver` protocol composed alongside (never widening)
`SecretResolver`.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 135).

### 136. The `jsonl_chained` audit backend silently disables four shipped read surfaces ✅ DONE

Fixed by replacing four scattered equality gates with one
`AuditSinkBackend.is_locally_readable()` capability lookup and giving the
admin UI audit browser the same chain-envelope unwrap the other three readers
already had (extracted once as `audit.ledger.unwrap_envelope()`).

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 136).

### 137. Audit read surfaces neither verify nor disclose hash-chain integrity ✅ DONE

All four durable audit read surfaces now disclose the actually configured
backend (`source` includes `"jsonl_chained"`, not always `"jsonl"`) and
verify each chain envelope's own hash before displaying it, via a shared
`audit/ledger.verify_envelope_hash` primitive.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 137).

### 138. Audit read surfaces scan the entire persisted file on every request, unbounded by lines read ✅ DONE

Fixed by switching all three audit-stream readers to a new shared
`audit.file_reader.iter_lines_reverse()` primitive (tail-first, byte-chunked)
with independent per-surface line/byte caps, replacing the old forward-scan +
bounded-deque approach; hardened by a second security review pass that closed
an algorithmic complexity defect in the new reader and threaded the missing
config fields.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 138).

### 139. Bound audit-line size at the source, not just at the reader ✅ DONE

Hard `max_length` caps on `StructuredQuery`'s `select`/`joins`/`group_by`/
`order_by`/`correlate`/`ctes` and `SetOpSpec.arms` (enforced at request-parse
time, before `normalize_query_shape` runs), plus `audit/sinks.py`'s startup
tail-read folded into item 138's bounded `iter_lines_reverse`.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 139).

### 140. `_audit_page` pagination can still materialize ~1M dicts per request ✅ DONE

`cursor`'s query-param ceiling lowered from 1,000,000 to 5,000, bounding
worst-case retained dicts per request to ~5,100 instead of ~1,000,050.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 140).

### 141. Convert audit-reader line caps into practically-tight window-based early exits ✅ DONE

**Shipped 2026-08-08** (maintainer-approved, PRODUCT_GUIDE Decision Log).
Investigating the original proposal (a tolerance for a bounded chance of
missing borderline events) found the real fix instead: `audit/logger.py`'s
new `_persist` helper re-stamps `occurred_at` immediately before the sink's
write call, closing the dominant source of drift (the `log.info` call and any
other work between event construction and the durable write) rather than just
tolerating it. `admin/anomaly.py`/`admin/config_trends.py` still carry a
`max_consecutive_out_of_window` tolerance (default 5,000, counted only across
the reader's own matching event type) as defense-in-depth against the
residual sub-microsecond scheduling-jitter risk under lock contention — real
in principle, negligible in practice — rather than claiming a guarantee a
misconfigured multi-worker-per-ledger-file deployment can't back. Mutation-
verified: the break condition, the counter-reset-on-in-window-event, and the
non-matching-type-doesn't-count rule each have a dedicated regression test
that fails when that specific rule is removed.
**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 141).

### 142. `docs/THREAT_MODEL.md` uses the ID `QG-32` for two unrelated threats ✅ DONE

Renamed the item-92 approval-gate row's ID to `QG-35`, keeping item-91's
audit-ledger row stable at `QG-32`; `docs/SECURITY_POSTURE.md`'s count
corrected to a clean "35 threats (QG-01…QG-35)".

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 142).

### 143. `cryptography` 49.0.0 has an unreviewed CVE, blocking `make release-check`'s SBOM step ✅ DONE

Upgraded `cryptography` 49.0.0 → 50.0.0 via `poetry update` (the existing
`>=44.0.1` constraint already permitted it) — a clean fix, no allowlist entry
needed. `make release-check` now passes fully clean end to end.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 143).

### 144. `verdict()` emits no query metrics, and `/metrics` is unauthenticated ✅ DONE

Dedicated `querygate_verdicts_total{connection,outcome}` (allowed/denied
only, never a reason) and `querygate_verdict_duration_seconds`; a
verdict-driven quota exhaustion now increments the shared
`querygate_query_quota_rejections_total`; `GET /metrics` now requires the new
`admin:metrics:read` scope by default (`AppConfig.metrics_require_auth`).

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 144).

### 145. Purpose-bound access: enforce the declared `intent`, don't just log it (feature F7) ✅ DONE

A new closed-set `StructuredQuery.purpose` field, checked against
`Policy.allowed_purposes` and narrowing the effective policy via
`Policy.purpose_policies`/`for_purpose` (deny/filter/mask-only, never
"allow" — narrows by construction) — enforced in
`validation/policy_validation.py`, propagated through to compilation, and
persisted to the audit event.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 145).

### 146. "5-minute first governed query" quickstart — close the named Toolbox onboarding gap ✅ DONE

A new `querygate-quickstart <connection>` CLI (thin authenticated HTTP client,
no new server-side authority) finds a table with non-sensitive columns and
prints a plain select, a filtered select, and a group-by aggregate, each with
a `curl`, an MCP tool-call, and a Python-SDK snippet — verified live against
a real server. The optional admin_ui "Quickstart" panel mentioned in the
item's body was not built (explicitly optional there; the CLI alone closes
the named gap).

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 146).

### 147. Self-serve procurement evidence page ✅ DONE

A generated, git-committed `docs/TRUST_EVIDENCE.md` (`make trust-page`,
`scripts/generate_trust_page.py`) composes the security posture doc,
compliance mapping, benchmark report, disclosure program, and live
dependency-audit allowlist status verbatim into one always-current artifact,
with a drift guard proving it stays current.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 147).

### 148. `admin/access_diff.py` never diffs `column_masks` at all ✅ DONE

Added `_diff_masks` to `admin/access_diff.py` (mirrors `_diff_mandatory_filters`'s
shape, case-insensitive on the table key) and a `"column_mask"`
`SemanticChangeCategory`; corrected the stale `policy/models.py` comment that
claimed this was already covered.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 148).

### 149. `Policy`'s case-insensitive table-key lookups disagree on `casefold()` vs `lower()` ✅ DONE

Standardized `Policy`/`WritePolicy`/`catalog/models.py`/`admin/templates.py` on
`.casefold()` for every case-insensitive table/column-key comparison; the
review this item's own fix went through found and fixed three sibling bugs
along the way (a write-column deny list silently inert under any non-lowercase
config key, a catalog sensitivity label unresolvable across a Unicode-casing
edge case, and a policy template's allow-list merge that could silently widen
to unrestricted). Recorded item 150 as an explicit, deliberately-deferred
follow-up for the one sibling instance (compiler/`mandatory_row_filters`
matching) that roots in a larger subsystem, not fixed here.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 149).

### 150. `compiler/sqlalchemy_compiler.py`'s `mandatory_row_filters` matching uses `.lower()` against `schema_validation.py`'s `.lower()`-consistent AST name resolution ✅ DONE

Swept `validation/schema_validation.py`'s `effective_name_map`/
`declared_cte_names`/`cte_source_names` and every function built on them
across `validation/policy_validation.py`, `execution/approval.py`,
`execution/service.py`, and `compiler/sqlalchemy_compiler.py` (~64 call sites)
from `.lower()` to `.casefold()`, closing the tenant-scoping gap
`mandatory_row_filters` had against a Unicode-casing table name.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 150).

### 151. Bind the in-query approval gate's token to a connection and principal, not just an AST fingerprint ✅ DONE

`issue_approval_token`/`verify_approval_token` (`execution/approval.py`) gained
additive `"cx"` (connection id) / `"sub_bind"` (bound principal subject)
claims — now required keyword arguments, not optional — alongside the
existing `fp`/`sub`/`exp`, plus an unconditional `"k"` (grant vs. pending)
and `"v"` (format version) claim, never folded into the fingerprint hash. A
token minted for connection A or principal X is now rejected (fail-closed)
when redeemed against a different connection or by a different principal,
closing the staging/prod cross-connection replay and the MCP MRTR
request_state session-handoff gap the `security-invariant-reviewer` audit of
items 19/128 surfaced; a *pending* MRTR elicitation token can no longer be
redeemed directly as a real grant (the `"k"` claim), and a token predating
these claims entirely can't be honored as unbound-and-permissive by a newer
pod mid rolling-deploy (the `"v"` claim) — both closed by the same review's
own follow-up findings before this landed. Wired through
`_enforce_approval_gate`/`_enforce_write_approval_gate`, REST's
`approve_query`/`approve_write`, and MCP's `build_pending_input_required`/
`resolve_approval_tokens_from_retry`. A related gap — the sensitivity
trigger resolving every table against the query's top-level connection only,
missing a joined-in connection's own `pii` labels — was filed separately as
item 155, not fixed here. See the Decision Log for the binding design
(additive claims, REST binds to the approving principal, MCP binds to the
original calling principal, no SDK `RequestStateSecurity` wiring) and why.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 151).

### 152. Sales/landing pages don't reflect items 19 (MySQL) / 134 (WORM retention) shipping ✅ DONE

Reconciled `sales/index.html`, `landing/security.html`, `landing/index.html`,
`landing/sandbox.html`, `README.md`, and `docs/business/GO_TO_MARKET.md`'s
dialect and WORM-retention claims against what items 19/134 phase 1 actually
shipped (MySQL support; S3 Object Lock WORM retention with the phase-2
managed-search caveat kept explicit). **Full write-up:**
[docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 152).

### 153. `CHANGELOG.md` has no `[Unreleased]` entry for items 19 (MySQL) or 134 (WORM retention) ✅ DONE

Added 17 `[Unreleased]` entries to `CHANGELOG.md` covering items 19, 134
(both phases), 128, 129, 135–140, 143–151 (the full gap back through item
128, not just the item's own 19/134 ask); items 89–127 and 131/133 remain a
separate, larger follow-up. **Full write-up:**
[docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 153).

### 154. WORM archive segments are unenveloped, so managed search cannot verify a segment was actually written by QueryGate ✅ DONE

**Shipped 2026-08-09** (maintainer decisions: per-segment chain, no
legacy-segment tolerance — no production deployment predates this fix).
`audit/worm_sink.py`'s `WormFlushMonitor.flush_once` now writes each
flushed batch as a fresh `LedgerRecord`-shaped chain (seq 0, `GENESIS_PREV_
HASH`), the same envelope the local `HashChainedAuditSink` uses;
`audit/worm_search.py` verifies each record's own hash before ever
unwrapping it, counting an unverifiable or unenveloped line as `malformed`.
Closes `docs/THREAT_MODEL.md` QG-40's residual (a forged, schema-valid
segment used to be returned indistinguishably from a genuine one).
**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 154).

### 155. `sensitivity_approval_reasons` looks up every table in the query's top-level connection's catalog, never a cross-connection join's own connection ✅ DONE

Threaded schema validation's per-scope table-to-connection map through to the
catalog sensitivity-label approval trigger, so a cross-connection join's
table is now looked up in the catalog of the connection it actually resolved
to as well as the query's top-level connection (consults both, triggers on
either — a strict swap of one for the other reopened a mirror-image gap,
caught same-day and fixed).
**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 155).

### 156. A cross-connection join's joined table is governed only by the primary connection's Policy — column masks, mandatory row filters, and deny-lists never apply from the joined connection's own Policy ✅ DONE

Threaded a reflection-free sibling of item 155's per-scope table-to-connection
map (`resolve_scope_connections`, computed before policy validation so the
"policy validation runs before any DB touch" ordering is preserved) through
`validate_policy`'s table/column allow-deny and masked-column-position checks,
and through `compile_structured_query`'s column-mask and mandatory-row-filter
application. A cross-connection join's table now resolves against BOTH its
own connection's Policy and the primary connection's — never a replacement of
one for the other, the same union direction (and the same hard-won lesson)
item 155's follow-up fixed for the catalog sensitivity trigger.
**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 156).

### 157. Snowflake live-server verification and deeper feature parity (item 19 phase 2 residual)

Item 19 phase 2 (2026-08-06) shipped `SnowflakeDialectAdapter`/
`SnowflakeSessionAdapter` as **rendering-level only** — every SQL idiom is
backed by Snowflake's public docs. `SnowflakeDialectAdapter` is checked by
compiling against a real, installed `snowflake.sqlalchemy` dialect object;
`SnowflakeSessionAdapter` is checked against recording fakes asserting the
exact SQL text/params it builds, not against that real dialect object (its
statements are `sa.text(...)` directly, not compiled Core expressions).
**None of it has run against a live Snowflake account**, and
`connections/engine.py` deliberately refuses to open a Snowflake connection
at all today (`SessionDialectAdapter.is_connectable()` returns `False` for
it — `snowflake-sqlalchemy`'s driver has no async SQLAlchemy engine support
— see the 2026-08-06 Decision Log entry). This item is the honest residual
so that gap isn't silently lost.

**What to do (when prioritized), roughly in dependency order:**

1. **Async execution path.** Decide and build how Snowflake actually
   executes a query through this pipeline. `snowflake-sqlalchemy` is
   sync-only, and the pipeline (`execution/service.py`, `schema/
   reflection.py`, `execution/cost_estimation.py`, ...) is built on
   `AsyncSession`/`AsyncEngine` throughout — this needs either (a) a real
   `asyncio.to_thread`-wrapped facade that satisfies enough of the
   `AsyncSession`/`AsyncEngine` surface for every existing call site to keep
   working unchanged, or (b) confirming whether a genuinely async Snowflake
   SQLAlchemy dialect has shipped since 2026-08-06. As of this writing there
   is an open `snowflakedb/snowflake-sqlalchemy` feature request tracking
   async support (unverified at time of writing — no internet access from
   this environment to confirm the current issue number/status; re-check on
   GitHub directly before committing to either (a) or (b), don't trust this
   line). Once async support exists, remove `is_connectable`'s
   Snowflake guard.
2. **Live verification.** Once (1) lands and a real Snowflake account/
   warehouse is available (trial account or a design-partner's own),
   add `tests/integration/test_snowflake_live.py` mirroring
   `test_mysql_live.py`'s shape, and a `make test-snowflake-live` target —
   deliberately NOT added in phase 1 per this item's own scoping (no way to
   stand up a Snowflake instance in this sandboxed environment or in CI).
   Confirm each documented-but-unverified claim against real data,
   especially: `DAYOFWEEKISO`/`WEEKISO` session-independence, the explicit
   `date_bucket('week', ...)` computation actually landing on the same Monday
   `date_trunc('week', ...)` would with default `WEEK_START`, `SYSDATE()`'s
   UTC guarantee, and the RANGE-with-numeric-offset window frame support
   (GA'd 2024-08-08 — confirm the target account/edition actually has it).
3. **Cost estimation.** No Snowflake cost estimator exists (falls back to
   "any other dialect proceeds under the reactive guardrails", the same as
   MySQL). Snowflake's `EXPLAIN`/query profile API would need its own
   `execution/cost_estimation.py` function, dispatched the same way
   `estimate_postgres_query_cost`/`estimate_mssql_query_cost` are.
4. **Test-suite integration gaps**, mirroring MySQL phase 1's own honestly-
   flagged gap: `test_compiler.py`/`test_cte.py`/`test_nonequi_joins.py`/
   `test_set_operations.py`/`test_column_masking.py`/`test_date_primitives.py`
   still don't parametrize Snowflake at all (deliberately kept out of
   `test_date_primitives.py`'s shared suite in phase 1, since that file is
   coupled to the live PG/MSSQL differential infrastructure — see
   `tests/unit/test_dialect_adapters.py`'s dedicated `TestSnowflake*` classes
   for where phase 1's coverage actually lives instead).
5. **Session-parameter/auth model.** Snowflake's connection shape (account
   identifier, warehouse, role, key-pair or OAuth auth beyond a plain
   password) is more elaborate than the other three dialects' — confirm
   `examples/connections.example.yaml`'s Snowflake example URL shape and
   `secrets/resolvers.py` cover what a real deployment needs (e.g. key-pair
   auth, which doesn't fit a single `${ENV_VAR}` connection-string secret the
   way a password does).

**Effort:** L–XL (async execution path is the load-bearing unknown; the rest
is incremental once that exists). **Depends on:** item 19 phase 2 (shipped);
a real Snowflake account/credentials becoming available to this project.

### 158. `ConnectionProfile` never validates that `dialect` agrees with `connection_string`'s actual backend ✅ DONE

Added a `field_validator("dialect")` on `ConnectionProfile` that parses
`connection_string` and rejects a mismatch against the declared dialect —
also catching and fixing a credential-leak risk in the first-draft
whole-model-validator approach along the way.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 158).

### 159. Cross-connection schema reflection can pick the wrong connection when a join's alias casing differs from a column ref's casing ✅ DONE

`_reflect_and_validate_scope` now case-folds `table_connection` once up front
and uses that copy for every `_load_table` lookup, so a differently-cased
column-ref spelling of a joined alias can no longer silently fall back to the
primary connection.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 159).

### 160. Item 156 follow-up: harden the connection-resolution edge cases a full security-invariant audit surfaced ✅ DONE

Four connection-resolution edge cases from item 156's own audit, all now
shipped: a fixed-snapshot `ConnectionResolver` so audit/compile can't
disagree under a concurrent reload, restored cheap-bound-first ordering,
`scope_connections` self-derivation from a given `connection_resolver`
(closing the footgun that caused item 156's own `simulate_candidate_policy`
bug), and confirmation that purpose/k-anonymity/row-limit resolution was
never claimed to be cross-connection-resolved.
**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 160).

### 161. BigQuery live-server verification and deeper feature parity (item 19 phase 3 residual)

Item 19 phase 3 (2026-08-07) shipped `BigQueryDialectAdapter`/
`BigQuerySessionAdapter` as **rendering-level only** — every SQL idiom is
backed by Google's public BigQuery SQL reference docs.
`BigQueryDialectAdapter` is checked by compiling against a real, installed
`sqlalchemy_bigquery` dialect object; `BigQuerySessionAdapter` is checked
against recording fakes asserting the exact SQL text/params it builds, not
against that real dialect object. **None of it has run against a live
BigQuery project**, and `connections/engine.py` deliberately refuses to open
a BigQuery connection at all today (`SessionDialectAdapter.is_connectable()`
returns `False` for it — `sqlalchemy_bigquery`'s driver has no async
SQLAlchemy engine support, and separately its dialect resolves real Google
credentials and builds a live client at engine-construction time — see the
2026-08-07 Decision Log entry). This item is the honest residual so that gap
isn't silently lost, following item 157's exact template for Snowflake.

**What to do (when prioritized), roughly in dependency order:**

1. **Async execution path.** Decide and build how BigQuery actually executes
   a query through this pipeline. `sqlalchemy_bigquery` is sync-only (its
   DBAPI wraps `google.cloud.bigquery`'s own synchronous, HTTP-based REST
   client — there is no lower-level async transport to build on the way a
   raw async driver would give one), and the pipeline
   (`execution/service.py`, `schema/reflection.py`,
   `execution/cost_estimation.py`, ...) is built on `AsyncSession`/
   `AsyncEngine` throughout — this needs either (a) a real
   `asyncio.to_thread`-wrapped facade that satisfies enough of the
   `AsyncSession`/`AsyncEngine` surface for every existing call site to keep
   working unchanged, or (b) confirming whether a genuinely async BigQuery
   SQLAlchemy dialect has shipped since 2026-08-07 (unverified at time of
   writing — re-check before committing to either (a) or (b), don't trust
   this line). Once async support exists, remove `is_connectable`'s BigQuery
   guard — but also resolve finding 2 below, since credential resolution at
   engine-construction time is a SEPARATE blocker from the async-driver gap
   and would need its own fix (e.g. deferring `Client` construction, or
   accepting it as a one-time synchronous cost at connection-pool warm-up).
2. **Engine-construction-time credential resolution.** Confirmed directly
   during item 19 phase 3's own investigation:
   `sqlalchemy_bigquery.BigQueryDialect.create_connect_args` builds a real
   `google.cloud.bigquery.Client` (resolving Google Application Default
   Credentials) the moment an engine is constructed, not when a connection
   is actually opened — a materially different lifecycle from every other
   supported dialect here, where `create_engine`/`create_async_engine` is
   lazy. Decide whether this is acceptable as-is (a `ConnectionProfile`'s
   engine is already lazily constructed on first use via `get_engine`, so in
   practice this only moves the credential-resolution cost slightly earlier
   than "first query," not before) or needs its own guard/health-check
   surface (e.g. an explicit `POST /admin/connections/{id}/test` that
   surfaces a credentials problem before an agent's first real query hits
   it).
3. **Live verification.** Once (1)/(2) land and a real BigQuery project/
   credentials are available (a GCP free-tier project or a design-partner's
   own), add `tests/integration/test_bigquery_live.py` mirroring
   `test_mysql_live.py`'s shape, and a `make test-bigquery-live` target —
   deliberately NOT added in phase 3 per this item's own scoping (no way to
   stand up a BigQuery project in this sandboxed environment or in CI).
   Confirm each documented-but-unverified claim against real data,
   especially: the three-way DATE/DATETIME/TIMESTAMP dispatch actually
   picking the right function against REFLECTED (not hand-typed) column
   types from a real BigQuery table, `ISOWEEK`'s Monday-start alignment
   matching Postgres's/MSSQL's/Snowflake's ISO week for the same date,
   `CURRENT_TIMESTAMP()`'s UTC guarantee, and the RANGE-with-numeric-offset
   window frame support.
4. **Cost estimation.** No BigQuery cost estimator exists (falls back to
   "any other dialect proceeds under the reactive guardrails", the same as
   MySQL/Snowflake). BigQuery's dry-run query (`jobs.query` with
   `dryRun=true`, which returns bytes-processed without executing) would be
   the natural basis for one, dispatched the same way
   `estimate_postgres_query_cost`/`estimate_mssql_query_cost` are — and,
   unlike Postgres's/MSSQL's row/cost-based estimate, would naturally
   express BigQuery's own cost dimension (bytes scanned, which is what
   BigQuery actually bills on) rather than forcing a row-count-shaped answer
   onto a byte-based pricing model.
5. **Test-suite integration gaps.** `test_compiler.py`/`test_cte.py`/
   `test_nonequi_joins.py`/`test_set_operations.py`/`test_column_masking.py`
   still don't parametrize BigQuery at all (deliberately kept out — see
   `tests/unit/test_dialect_adapters.py`'s dedicated `TestBigQuery*` classes
   for where phase 3's coverage actually lives instead).
   `test_date_primitives.py`'s shared suite WAS extended to include BigQuery
   in phase 3 (unlike Snowflake's phase, which is item 157's own flagged
   gap) — but only for `DatePart`/`IntervalUnit` exhaustiveness, not a live
   differential.
6. **Auth/connection-shape model.** BigQuery's connection shape (GCP project
   ID, dataset, service-account JSON or ADC, optional location/region) is
   more elaborate than a plain `${ENV_VAR}` connection-string secret was
   designed for — confirm `examples/connections.example.yaml`'s BigQuery
   example URL shape and `secrets/resolvers.py` cover what a real deployment
   needs (e.g. a service-account JSON key file path or inline JSON, which
   doesn't fit a single password-shaped secret the way Postgres/MySQL/MSSQL
   do).

**Effort:** L–XL (async execution path and the engine-construction-time
credential-resolution question are the two load-bearing unknowns; the rest
is incremental once those exist). **Depends on:** item 19 phase 3 (shipped);
a real BigQuery project/credentials becoming available to this project.

### 162. Dialects beyond MySQL/Snowflake/BigQuery (item 19's open-ended "…" scope)

Item 19 originally read "Additional dialects (MySQL, Snowflake, BigQuery,
…)" — the trailing "…" always implied more dialects could be added later.
With MySQL/Snowflake/BigQuery all now shipped (2026-08-06/2026-08-07, item
19 phases 1–3) and item 19 itself closed and archived, this item exists so
that open-ended possibility has a real home instead of either being lost or
keeping item 19 open forever on the strength of an ellipsis.

**Not scoped to a specific dialect on purpose.** Candidates a future pass
might consider (no priority implied by list order, and none investigated —
this is a placeholder, not a commitment): Redshift (Postgres-wire-compatible
enough that much of `PostgresDialectAdapter` might transfer directly, unlike
Snowflake/BigQuery's from-scratch adapters), DuckDB (increasingly common as
an embedded analytics engine; has a real async story via `duckdb`'s own
Python API, unlike Snowflake/BigQuery — worth checking whether that changes
the "rendering-only phase" pattern entirely for once), Databricks/SQL
Warehouse, ClickHouse. Whichever is picked should follow the exact
`DialectAdapter`/`SessionDialectAdapter`-pair-plus-registry template item 57
established and items 19/73 have now used four times over (MySQL/Snowflake/
BigQuery, plus the original Postgres/MSSQL pair) — and should do the same
diligence this item's own precedents did before writing any adapter code:
confirm the target's actual async-driver story directly (constructing a real
engine against it, not assuming), rather than guessing from a library's
name or its sync sibling's reputation.

**Effort:** unscoped (depends entirely on which dialect and its actual
async-driver/auth-model story — could be S if a dialect turns out to have a
real async SQLAlchemy driver and Postgres-compatible wire protocol, or
L–XL if it repeats Snowflake/BigQuery's rendering-only shape). **Depends
on:** none — the template is proven; picking a dialect is a product/roadmap
decision, not a technical blocker.

### 163. A not-connectable dialect (Snowflake/BigQuery) as the SECONDARY side of a cross-connection join never reaches the `is_connectable()` guard ✅ DONE

`resolve_query_table_connections` now checks `is_connectable()` on a
cross-connection join's SECONDARY connection too, not just the primary,
rejecting with the same client-actionable `ConfigValidationError` shape
`init_engine`'s guard already gives for the primary.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 163).

### 164. `column_mask`'s HASH branch is an implicit `else`, not an exhaustive match, on all five `DialectAdapter`s ✅ DONE

All five `column_mask` implementations now raise a typed
`QueryValidationError` for any unrecognized `ColumnMaskKind` instead of
silently falling through to HASH.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 164).

### 165. `/admin/reload-config`'s generic exception handler can leak a live credential in its HTTP 400 body ✅ DONE

`reload_config_endpoint` now catches `pydantic.ValidationError` separately,
before the generic `except Exception`, and builds `detail` from a new
`safe_pydantic_error_lines` helper (`admin/service.py`) that asks pydantic
itself to never materialize `input`/`input_value` — never a credential.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 165).

### 166. Cross-connection self-join reflects both aliases against ONE connection — the `physical_tables` reflection memo is keyed by table name alone, not by which connection a name resolves to ✅ DONE

`physical_tables` is now keyed by `(table_cx, physical_key)` instead of the
physical name alone, so a cross-connection self-join reflects each alias
against its own declared connection rather than collapsing onto whichever
alias's reflection ran first.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 166).

### 167. A case-different column ref to a joined alias leaves a phantom second `sa.Table` alias that a mandatory row filter turns into an implicit cross join (confirmed by compiling the shape — row duplication, not just cost) ✅ DONE

`_apply_mandatory_row_filters` now walks only the query's own declared
FROM/JOIN occurrences, resolved through the same case-insensitive
`_table_by_name` lookup the FROM/JOIN clause itself uses (not an exact dict
index — a name-only fix alone still let the filter bind to a different alias
object than the one in the compiled statement under some `tables` dict
orderings, a bypass caught in post-ship review before this landed), so a
case-different column ref no longer leaves a phantom alias for a mandatory
row filter to turn into an unconditioned cartesian join or an unfiltered
joined alias.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 167).

### 168. Config-governance dry-run's credential-safety net (`_humanize_validation_errors`) is a post-hoc regex scrub, not a structural guarantee — and `cli.py`'s `load_config_context`/`main()` still stringify raw `ValidationError`s at the source ✅ DONE

`load_config_context` now builds its pydantic/YAML error lines through
`core/exceptions.py`'s `safe_pydantic_error_lines`/`safe_yaml_error_detail`
(moved there so both `cli.py` and `admin/service.py` can import them without
a cycle) instead of stringifying the raw exception — closes the CLI/dry-run
gap item 165 left open, plus an independently-found second leak path
(`yaml.YAMLError` on a literal-credential `connections.yaml`) item 165 didn't
cover either.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 168).

### 169. A correlated subquery's `correlate` ref binds to a phantom alias object by exact dict index, which can silently turn an EXISTS/scalar subquery into an independent, unfiltered scan of a mandatory-row-filtered table ✅ DONE

**Reproduced, then fixed** — confirmed by compiling the exact shape the item
described, and a second, closely related crash bug found in the same code
region was fixed alongside it. **Full write-up:**
[docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 169).

### 170. Cross-connection joins are reflected as if both connections are always on the same physical server instance, with nothing that actually checks it ✅ DONE

`ConnectionRegistry.from_entries` now rejects a `join_group` whose members
resolve to different hosts (or the same host with different ports) at
config-load/hot-reload time, instead of silently reading an unrelated
same-named database on the primary's host.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 170).

### 171. The audit windowed early-exit (item 141) can silently under-report on a merged/multi-writer file, with no disclosure field or way to tell caller-facing consumers apart ✅ DONE

`scan_ended_on_out_of_window_run: bool` now distinguishes "genuinely
complete" from "heuristically stopped early" on `AnomalyReport`,
`ConfigCatalogChangeTrend`, and `RecentDenialsReport`, independent of
`truncated`.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 171).

### 172. WORM archive segment verification checks each record's own hash but never the chain's linkage within a segment ✅ DONE

`search_worm_archive` now verifies `seq`/`prev_hash` continuity within a
segment, not just each record's own hash; a broken link stops consuming
that object and is counted in a new `chain_breaks` field. Segment
duplication to a second S3 key remains a separate, undecided residual.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 172).

### 173. Cross-connection connection-resolution is unmemoized per join, redone on every call site that self-derives ✅ DONE

`validate_schema` now accepts and reuses the per-request `connection_resolver`
snapshot item 160 already built, instead of re-deriving cross-connection
resolution a second time internally.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 173).

### 174. A cross-connection join's secondary-connection schema qualifier is a hardcoded MSSQL `.dbo` idiom, with no dialect dispatch ✅ DONE

`SessionDialectAdapter.cross_database_schema_qualifier` now dispatches per
dialect (MSSQL's real `<db>.dbo.<table>`; Postgres/MySQL rejected, not
emulated), replacing the hardcoded MSSQL-only f-string in `_load_table`.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 174).

### 175. `test_mssql_write_execution.py` leaks real aioodbc connections across tests, intermittently failing CI with "Connection is busy with results for another command" ✅ DONE

An autouse, function-scoped fixture now disposes every cached engine after
each test, mirroring `test_mssql_live.py`'s own already-shipped fix for the
identical `reset_engines()`-doesn't-dispose gap (item 2).

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 175).

### 176. Three claim-accuracy drifts found while fixing the item-134 stale WORM-search line ✅ DONE

`sales/index.html`'s two sales-guardrail lists, `CUSTOMER_README.md`,
`docs/business/GO_TO_MARKET.md` and TODO.md's own Quick-scan row for item 134
now describe the shipped `GET /api/v1/admin/observability/worm-search` endpoint
instead of denying it exists — each scoped to what actually ships: a bounded
API over the S3 WORM archive, with no UI of its own, no server-side table
filter, and non-exhaustive paging once a day exceeds the object budget. The
local JSONL sink has its own separate, bounded admin-UI browse surface, which
an earlier version of this fix wrongly denied.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 176).

### 177. `WormSearchResult.chain_breaks`/`unverified` have no Prometheus counter, so the strongest WORM-archive tamper signal isn't alertable ✅ DONE

`querygate_audit_worm_search_chain_breaks_total` and
`..._unverified_total` (both unlabelled) now publish item 172's chain-linkage
findings on the served path *and* on a mid-scan S3 failure, so a broken
segment chain is alertable rather than only visible inside an ad-hoc search
response.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 177).

### 178. A hash-verified WORM record with a non-int `seq` (type-confused, not corrupt) raises instead of being counted `unverified` ✅ DONE

Both raw `parsed.get("seq")` reads in `audit/worm_search.py` now go through a
`_chain_seq` helper that rejects anything that is not a genuine `int`, so a
crafted-but-hash-valid line is counted like every other forgery class
(`unverified` + `chain_breaks`, segment scan stopped) instead of raising
`TypeError` out as a generic 500.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 178).

### 179. Cumulative disclosure budget: bound multi-query differencing per purpose, the one form of "governing intent" that is structural ✅ DONE

Two off-by-default `Policy` caps (`max_shape_repeats_per_window`,
`max_aggregate_queries_per_window`) that bound, per (principal, connection,
declared purpose, table) over a rolling window, how many times one *literal-free
query shape* may be re-run against a k-floored table and how many aggregate
queries may touch it at all — the multi-query counterpart to `min_group_size`.
Repetition, not variety, is the differencing signal. Rejects on exhaustion;
approval-escalation deferred to item 180. Bounds R3, does not close it.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 179).

### 180. Escalate an exhausted disclosure budget into the item-92 approval gate instead of rejecting

**Deferred deliberately from item 179 (2026-08-11), with the maintainer.** Item
179 rejects when a principal's cumulative disclosure budget is spent
(`DisclosureBudgetExceededError` → REST 429 / MCP `RATE_LIMITED`). The
alternative considered and not taken: return 428 and let a human holding
`query:approve` grant the next N queries, reusing item 92's shipped
stateless-HMAC approval-token machinery end to end.

**Why it was deferred rather than built.** It is the better UX — "you've
exhausted this purpose's budget, a human can extend it" beats a dead end — but
it carries a specific failure mode worth deciding against evidence rather than
taste: an approval gate on a *disclosure* budget can become "click here to buy
unlimited disclosure", and approvers habituate to clicking. Item 179's own
false-positive calibration is also still open (no audit-stream replay data
exists yet), so we would be tuning an escalation path before knowing how often
the budget legitimately trips.

**What to do (when prioritized):** decide first, from real usage, whether the
budget trips often enough on legitimate work to need an escape hatch at all. If
it does: raise `ApprovalRequiredError` instead of
`DisclosureBudgetExceededError` when `Policy` opts in, bound what one approval
grants (a fixed extra N, never "unlimited for the window"), make the grant
itself an audited event distinct from an ordinary query approval, and ensure an
approval cannot be replayed across purposes or tables — the token is
fingerprint-bound today, and a budget grant is a different shape of authority
from "run this specific expensive query".

**Effort:** M. **Depends on:** 92, 179 (both shipped).

### 181. `redis_quota.py`'s `Retry-After` is always the full window: the Lua reads `ZRANGE … WITHSCORES` and indexes a nested reply

**Found 2026-08-11 while building item 179's Redis sibling**, by a parity test
that rejects at a *non-zero* window age. `execution/redis_quota.py`'s
`_RESERVE_SCRIPT` computes its retry hint as:

```lua
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
local oldest_ts = oldest[2] and tonumber(oldest[2]) or now
```

Measured under `fakeredis`, the Lua bridge surfaces a `WITHSCORES` reply as a
**nested** table, so `oldest[2]` is `nil`, `oldest_ts` falls back to `now`, and
the countdown collapses to `math.ceil(window - 0)` — i.e. **every** rejection
reports the full window rather than the true time until capacity returns. Item
179's `redis_disclosure_budget.py` had the identical line and now uses a
portable `ZRANGE` + `ZSCORE` pair instead; this file was deliberately left
unchanged, since altering a shipped feature's caller-visible retry hint is its
own change.

**Why no existing test catches it.** `tests/unit/test_redis_quota.py`'s
`test_request_cap_admits_then_rejects_with_retry_after` charges and rejects at
the *same* instant (`now=100.0`), where the correct answer and the buggy answer
coincide at `window_seconds`. The same blind spot existed in item 179's first
draft and is exactly what the added non-zero-age test exposed.

**Impact:** availability/UX, not disclosure. A caller told to retry in 3600s
when capacity actually returns in 12s will back off far longer than necessary;
a well-behaved client honoring `Retry-After` is penalised most. The in-process
`InProcessQuotaLimiter` computes this correctly, so single-instance deployments
are unaffected — this is Redis-backend-only.

**What to do (when prioritized):**

1. **Verify against a REAL Redis first.** This was measured under `fakeredis`
   only. Real Redis returns a *flat* array for `ZRANGE … WITHSCORES`, in which
   case `oldest[2]` is correct there and the defect is a fakeredis artifact —
   which would mean the bug is in the *test double*, not production. Do not
   "fix" production until that is settled; the two-call form is correct under
   both, so it is the safe landing either way.
2. Apply the same `ZRANGE` + `ZSCORE` pair item 179 uses, and add a
   non-zero-age assertion to `test_redis_quota.py` (charge at `now=100.0`,
   reject at `now=400.0` with `window_seconds=600`, assert `300`).
3. Check `execution/redis_concurrency.py` for the same pattern while there.

**Effort:** S. **Depends on:** 50 (shipped).

### 182. Observe mode for the disclosure budget — measure before you enforce

**Opened 2026-08-11**, immediately after item 179 shipped, because that item
left one honest gap: **no threshold is recommended anywhere, because none has
been calibrated.** An operator enabling `max_shape_repeats_per_window` today is
choosing a number nobody has validated against real traffic, and choosing it
too low turns an analyst iterating on filters into a refused caller. This is
the piece that makes item 179 deployable rather than theoretical.

**The precedent is exact.** `CostEstimationMode.OBSERVE` (item 26) exists for
the identical problem and states the identical reasoning in its own docstring:
records what *would* have been rejected without blocking, "use it to calibrate
thresholds against real traffic before switching a connection over to ENFORCE,
since a threshold copied from documentation is a guess, not a measurement." It
ships `querygate_cost_estimation_would_reject_total` and a
`cost_estimation.observed_would_reject` log line. Follow that shape rather than
inventing a second one.

**What to do:**

1. `Policy.disclosure_budget_mode: enforce | observe`, defaulting to `enforce`
   for consistency with `cost_estimation_mode` — but with the docs saying
   plainly that a first deployment should start in `observe`. Off-by-default
   still holds either way: with no caps configured neither mode does anything.
2. In `observe`, a charge past its cap increments a new
   `querygate_disclosure_budget_would_reject_total{connection,budget_kind}` and
   emits a `disclosure_budget.observed_would_reject` log line, and the query
   **runs**.
3. **The one real design question, and it is not cosmetic.** Item 179's
   `reserve()` is all-or-nothing: a refused charge records *nothing*, so a
   naive "catch the exception and continue" observe mode would stop
   accumulating the moment the cap is first crossed — and you would measure
   only the run-up to the threshold, never how far past it real traffic goes.
   That is precisely the number needed for calibration. Observe mode must keep
   recording past the cap, which means threading the mode into
   `DisclosureBudgetLimiter.reserve` (and the Lua) rather than wrapping the
   call site. Decide this deliberately; wrapping is the tempting wrong answer.
4. Both backends, since a single-replica measurement generalises badly to the
   fleet the operator will actually enforce on.

**Effort:** S–M. **Depends on:** 179 (shipped), 26 (shipped — the precedent).

### 183. Suggest a disclosure-budget threshold from observed behavior, for human approval

**Opened 2026-08-11 (maintainer proposal).** Once item 182 is producing real
distributions, propose a threshold rather than making every operator derive one
from raw metrics. Framed as a nice-to-have: a client can use it or ignore it,
and the budget must remain fully configurable by hand.

**Stay inside the 32C boundary, which already governs exactly this.** CLAUDE.md:
typed redaction-safe signals only, per-customer/connection partitioning, no
feedback loops, and learned content "must go through the existing 32B review
path — it can never publish itself." A suggestion is a proposal, never an
applied policy. `admin/anomaly.py` is likewise committed in its own docstring to
being read-only and non-feedback — a suggestion surface may sit alongside it but
must not turn it into an enforcement path.

**The trap that makes this non-trivial — do not skip it.** If the threshold is
derived from observed behavior and a prober is *already* active during the
observation window, the recommendation is calibrated to comfortably accommodate
the attack. The baseline is poisoned by the very thing the budget exists to
catch, and the more patient the attacker, the more normal they look. Two
consequences for the design:

- **Suggest from a percentile of typical behavior, never the observed maximum.**
  The maximum is exactly where an attacker sits.
- **Present the distribution, not a number.** The approval surface should show
  the shape of observed re-run counts and its tail, so a human is approving a
  judgement they can see, not rubber-stamping an integer. An unexplained
  recommended number invites habituated clicking — the same failure mode item
  180 records for approval-gated budget grants.

**What to do (when prioritized):** read item 182's observed distribution per
(principal, connection, purpose, table); compute a percentile-based candidate
plus the tail beyond it; surface both through the 32B review path as a proposal
an operator approves, edits or discards. Never auto-apply, never on a schedule
that could apply without a human, and never suggest a *loosening* of a
threshold an operator has already set by hand without saying so explicitly.

**Effort:** M. **Depends on:** 182, 179 (shipped), 32B/32C (shipped).

### 184. A day holding more segments than `max_objects_scanned` returns a cursor that never advances, so part of the WORM archive is unreachable and both integrity counters inflate without bound ✅ DONE

The day-truncation cursor is now exclusive: it carries an `after` listing marker naming the last key consumed, so a day holding more segments than `max_objects_scanned` pages to exhaustion instead of re-listing its first N keys forever.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 184).

### 185. `AUDIT_WORM_SEARCH_REQUESTS_TOTAL{outcome="rejected"}` is unreachable for the rejections its own comment claims to count ✅ DONE

`build_worm_search_result` now counts its own bound rejections, so `querygate_audit_worm_search_requests_total{outcome="rejected"}` reaches the missing/over-wide window and out-of-range limit cases `metrics.py` always documented it as covering.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 185).

### 186. The disclosure budget's per-shape cap is evadable: a select-alias *reference* mints a fresh shape bucket per probe

**Surfaced 2026-08-12 by the `security-invariant-reviewer` and the
`architecture-boundary-reviewer` independently, auditing the merge of item
179 into `main`; each found a different evasion vector for the same root
cause.** Pre-existing in item 179 as shipped — not caused by the merge.

`shape_fingerprint` strips `_VOLATILE_SHAPE_KEYS` (which includes `alias`) and
`_canonicalize` rewrites dotted refs via `effective_name_map`. That removes an
alias *definition* and a *table* alias. It does nothing about an alias
**reference**, which `normalize_query_shape` emits as a bare string under
`order_by[].col`, `group_by[]`, `top_n.order_by[].col`, and inside
`_where_shape(having)`. `_canonicalize` only rewrites refs containing a dot
whose prefix is a declared effective name, so a bare `n7` is returned unchanged
after casefolding.

**Measured on the merged tree (not reasoned):** twenty probes differing only in
a select alias and its `order_by` reference — with the predicate literal
sliding, which is the actual differencing attack — produce **20 distinct
fingerprints**. The same query with the alias held fixed produces **1**, so the
mechanism works and this is precisely the hole:

```python
{"from": "employees",
 "select": [{"fn": "count", "col": "employees.id", "as": "n7"}],
 "group_by": ["employees.department"],
 "order_by": [{"col": "n7", "dir": "desc"}],
 "where": {"col": "employees.salary", "op": "gt", "value": 120000}}
```

Walk `n1`, `n2`, `n3`… alongside the sliding constant: every probe compiles,
passes the k-floor, returns a real answer, and lands in its own bucket, so
`max_shape_repeats_per_window` never trips at any value.

The `security-invariant-reviewer` found three further vectors from the same
root cause, not individually re-measured: a **CTE rename** (`max_cte_count`
defaults to 3, so this is on by default), a **nested-scope alias** inside a
`value_subquery`/`exists_subquery`/set-op arm (`_canonicalize` builds its alias
map from the *outermost* scope only), and **list order** (`select` items,
`and_terms`, `order_by` direction — none is in `_VOLATILE_SHAPE_KEYS` and
`_canonicalize` preserves list order).

**Why this is worse than a cap being loose.** `max_aggregate_queries_per_window`
does bound every one of these vectors — but it is a *separately optional*
field. A policy with `min_group_size` + `max_shape_repeats_per_window` and no
table cap loads clean, shows up in the admin effective-guardrails view, and
bounds essentially nothing; `docs/PRODUCT_GUIDE.md` frames the shape cap as
"the targeted probe cap" and the table cap as a "blunt backstop", which makes
shape-cap-only sound like the precise choice rather than the broken one. Item
179's own archive write-up also records this defect class as **found and
fixed**, which it is not — that claim has been corrected in
`docs/TODO_ARCHIVE.md` and a residual note added to `docs/THREAT_MODEL.md` §8.

**What to do (needs a maintainer decision on scope, hence its own item):**
1. *The cheap containment*, if the fix is not immediate: a second
   `model_validator` on `Policy` next to `_disclosure_budget_needs_a_k_floor`
   rejecting `max_shape_repeats_per_window` set without
   `max_aggregate_queries_per_window`. Same fail-at-load posture as the k-floor
   coupling. **This is a breaking config change** for anyone who set the shape
   cap alone — hence a decision, not a drive-by.
2. *The real fix*: make `shape_fingerprint` alias- and order-insensitive —
   build a map from each declared select-item output alias to a positional
   token (`select[0]`, `select[1]`, …) and rewrite bare references through it;
   thread a per-scope alias map through `_canonicalize`'s recursion instead of
   using only the outer scope's; rewrite cte references to their
   `_cte_base_tables` result; sort `select`/`and_terms`/`or_terms`; drop the
   `order_by` key. Every one of those collapses buckets, which the module
   already argues is the safe direction ("the cap trips sooner, never later").
   Note it changes existing fingerprints, so in-flight windows reset on deploy.

**Two scoping residuals to document in the same pass** (both surfaced by the
`security-invariant-reviewer`, neither a bypass on its own):
- A **cross-connection joined table** is charged under the *requesting*
  connection, so the same physical table reachable through two connections
  carries two independent budgets, and the k-floor is the primary connection's.
  Pin the current behavior with a test so a future change is deliberate.
- Under **static API-key auth** every caller sharing a key collapses to one
  `Principal.subject`, so the module docstring's "keying on principal, not
  actor, avoids one agent denying service to a fleet" argument holds only under
  JWT auth. One sentence in the docstring.

**Effort:** M. **Depends on:** 179 (shipped).

### 187. A disclosure-budget refusal tells the caller which cap tripped, its configured value, and the window length

**Surfaced 2026-08-12 by `security-invariant-reviewer` auditing the item-179
merge.** `core/exceptions.py`'s `DisclosureBudgetExceededError` docstring claims
the caller-visible contract is deliberately indistinguishable from any other
budget rejection, so as not to "tell an attacker which guardrail they tripped".
`rejection_message` (`execution/disclosure_budget.py`) does not keep that
contract: the two branches are textually distinct and self-describing, naming
the cap kind, the operator's configured limit, and the window length, plus a
precise `retry_after`.

The leak is not the table or the shape (both correctly withheld, and tested).
It is that a prober learns **which** cap it hit — which directly answers "will
varying my shape help?", i.e. the server hands over item 186's evasion strategy
instead of making it be discovered blind.

**What to do (needs a decision):** either collapse both branches to one
kind-agnostic sentence naming neither the cap nor its value (keeping
`quota_kind` on the exception object, since that feeds the metric and the
operator-facing breakdown), **or** decide that echoing a configured cap is
consistent with the repo's existing posture for structural caps (`max_set_op_arms
of 3` is echoed today) and correct the `exceptions.py` docstring instead. Do not
leave the docstring claiming a contract the message does not keep.

**Effort:** S. **Depends on:** 179 (shipped).

### 188. A principal policy override can fail `Policy` validation at request time, 500-ing every query for that principal, after `validate-config` accepted it

**Surfaced 2026-08-12 by `security-invariant-reviewer` auditing the item-179
merge.** Item 179 added `_disclosure_budget_needs_a_k_floor`, the first
`model_validator` on `Policy` itself (the existing two are on `ColumnMask` /
`MandatoryRowFilter`) — and therefore the first that can fail on a *combination*
of two individually-valid config layers.

`policy/loader.py` validates a principal override by merging it against
`default_raw` only; its own comment already concedes "the real merge base at
request time may instead be this connection's own `connections:` override".
That was harmless while no cross-field constraint existed. Now: `default:` sets
`min_group_size: 5`; `connections.foo:` sets `min_group_size: null`;
`principals.alice.foo:` sets `max_shape_repeats_per_window: 10`. Load-time
validation passes. At request time `PolicyStore.get("foo", alice)` merges
alice's cap onto foo's floorless base, the validator raises inside
`StructuredQueryService._get_policy()`, and `mask_unexpected` turns it into a
generic **500** for every query alice issues on that connection.

Fail-closed and non-leaking, but a config the CLI accepted takes one principal
fully offline, and the 500 says nothing an operator could act on.

**What to do:** validate each principal override against **each connection's own
resolved base**, not just `default_raw` — for `connection_id != "*"`, against
`overrides.get(connection_id, default)`; for `"*"`, against every connection
base. That moves the failure to load time, which is what the existing comment
says it wanted but had no cross-field constraint to motivate.

**Acceptance criteria:** a three-layer YAML as above raises at
`PolicyStore.from_dict`; today it loads clean and only `PolicyStore.get` raises.

**Effort:** S. **Depends on:** 179 (shipped).

### 189. Test-contract gaps in the item-179 disclosure budget and the item-177 WORM counters

**Surfaced 2026-08-12 by `test-contract-reviewer` (and two items independently
by `architecture-boundary-reviewer`) auditing the merge of both branches into
`main`.** Each is a place where the enforcement is correct but nothing would
fail if it broke, so they are grouped: one commit, one review, no production
behavior change.

1. **Charge weight is unpinned end to end.** `budgeted_occurrences` returning 3
   is tested, and the limiter refusing a weight-2 charge is tested, but the line
   carrying the number between them is not: replace `weight` with `1` at both
   `charges.append` sites and the suite stays green. No unit test reaching
   `enforce_disclosure_budget` uses a multi-scope query, and no e2e test uses
   `set_op` at all — so the documented "one statement must not buy several probe
   answers" bypass is unpinned.
2. **Only 2 of the 5 `_VOLATILE_SHAPE_KEYS` are pinned** (`requested_limit`,
   `offset`). Removing `alias`, `n`, or `fraction` fails nothing. Drive the test
   off the frozenset itself so a sixth key is covered automatically. (Note the
   `alias` entry is also half-ineffective — see item 186.)
3. **Nothing tests that `create_app` installs the Redis limiter.** Delete the
   `init_redis_disclosure_budget_limiter(...)` line and the suite stays green
   while an HA deployment silently reverts to a per-replica budget that
   `deploy/HA_DR.md` says is shared. Same for the two new `clear_*` shutdown
   calls, where the leak is a fail-closed limiter left over a closed client.
4. **The admin-observability aggregation of the new counter has no test.**
   Delete the `elif name == "querygate_disclosure_budget_rejections_total"`
   branch and everything passes; the operator-facing breakdown silently reverts
   to `{}` — the exact regression the item-179 audit had just fixed.
   `test_observability.py::test_quota_rejections_by_kind` is the precedent to
   mirror.
5. **`test_a_refused_query_never_reaches_the_database` proves something
   narrower than its name.** Enforcement runs *after* `session_scope` opens
   (issuing dialect guardrail statements on Postgres/MSSQL) and after
   `_estimate_cost`'s real `EXPLAIN`; the test's SQLite fixture has neither. Either
   narrow the name/docstring to "the compiled statement is never executed", or
   assert the ordering that matters with a spy on `_estimate_cost`.
6. **`disclosure_budget_window_seconds`' inverted diff direction is unpinned**,
   unlike both its siblings — remove it from `INVERTED_GUARDRAIL_FIELDS` and
   nothing fails, so an operator *lengthening* the window (the tighter posture)
   would be shown "loosening".
7. **`test_observability_api.py`'s two new assertions are order-dependent and
   vacuous in isolation** — they iterate a dict that is only non-empty when the
   e2e disclosure test ran earlier in the same process. Have the test increment
   the counter itself, as the file already does for `QUERIES_TOTAL`.
8. **`test_the_worm_search_has_exactly_one_production_call_site` pins the wrong
   symbol.** It greps for `build_worm_search_result(`, the wrapper, while the
   load-bearing claim on four surfaces is about `search_worm_archive` — which is
   public and directly callable. Add a background verifier calling it directly
   and the test stays green while "QueryGate does not scan on a schedule" goes
   stale. It also asserts a hard-coded `file:line`; assert on file names.
9. **Two dead imports** in `tests/unit/test_worm_search.py`
   (`AUDIT_WORM_SEARCH_CHAIN_BREAKS_TOTAL`, `..._UNVERIFIED_TOTAL`), and
   `test_disclosure_budget_e2e.py` reads the private
   `labels._value.get()` where `REGISTRY.get_sample_value` is the public
   equivalent used throughout `test_worm_search.py`.

**Effort:** M. **Depends on:** 177, 179 (both shipped).

### 190. Claim drifts on outward-facing surfaces, unrelated to WORM search

**Status 2026-08-12: #1, #4, #8, #9 and #10 are DONE** — cost estimation
(README, `examples/policy.example.yaml`, five `src/` comments, THREAT_MODEL,
the PRODUCT_GUIDE Decision Log, TECHNICAL_REVIEW, two TODO_ARCHIVE write-ups
and a test docstring), the unconditional "resumable page" claim (both
`worm_search.py` bullets, its `WormSearchBounds` docstring, README, QG-40,
THREAT_MODEL, PRODUCT_GUIDE ×2, the generated HTML, CHANGELOG, TODO_ARCHIVE),
the two disclosure-budget code docs, MARKET_DOMINATION_ANALYSIS, and the
rollback-exemption test (mutation-verified: closing the exemption fails it,
and a control proves the gate is still on). Verified by re-running
`scripts/claim_drift_sites.py --all`. **Open: #2, #3, #5, #6, #7** — all four
remaining surfaces are `landing/security.html`, `README.md:1944`, and
`sales/PUBLIC_LANDING_RUNBOOK.md`, and the two landing/sales files carried
unrelated uncommitted work, so they were deliberately not edited. **#6 (the
runbook's stop-list) should be fixed first: it is the instruction sheet the
landing copy is written from, i.e. the mechanism by which #2 keeps recurring.**


**Surfaced 2026-08-12 by `claim-reviewer` auditing item 176.** Item 176 closed
the WORM-search class specifically and its sweep confirmed no fourth *WORM*
surface. These are a different class the same review turned up, filed rather
than folded in because none is about item 134:

1. **Cost estimation is described as Postgres-only across the repo.**
   `estimate_mssql_query_cost` ships (`execution/cost_estimation.py`), is
   dispatched (`execution/service.py`), and is live-tested
   (`tests/integration/test_mssql_cost_estimation.py`); item 26 is `✅ DONE`
   including phase 2 and CLAUDE.md records the exception as resolved.
   **Do not hand-assemble the site list — derive it:**
   `poetry run python scripts/claim_drift_sites.py cost-estimation`.
   Live sites include `README.md:483/501/1950-1953` (`:501` is a whole stale
   paragraph) and `README.md:609`, which is **drifted, not merely stale** —
   `Policy.estimate_needed` fires on `approval_cost_gate_enabled`, so
   `approval_max_estimated_*` does work on MSSQL; plus
   `examples/policy.example.yaml:172` (**ships to customers**),
   `src/querygate/metrics.py:53` and `:141-142`,
   `src/querygate/core/exceptions.py:127`,
   `src/querygate/execution/service.py:653` and `:947`,
   `src/querygate/compiler/dialect_adapters.py:13`,
   `src/querygate/policy/models.py:25`, `docs/THREAT_MODEL.md:162` and
   `:295-298`, `docs/PRODUCT_GUIDE.md:7505` (**re-word, don't delete**), the
   generated `docs/product-guide.html`, `TECHNICAL_REVIEW.md:249`,
   `docs/TODO_ARCHIVE.md:1240` and `:4202-4203`, and
   `tests/unit/test_service.py:387`. Re-run the scan after fixing. *A fifth
   denial class sits in `landing/security.html`, which says QueryGate does not
   estimate a plan **at all** — left only because that file carried unrelated
   uncommitted work; use `sales/index.html`'s "prevents *every* expensive plan"
   framing.*
2. **`landing/security.html` lists a four-eyes config approval workflow and an
   administration UI as not shipped.** Both ship — item 42
   (`ADMIN_CONFIG_APPROVE_SCOPE`, `require_config_approvals`, author ≠ approver
   enforced server-side) with the admin UI driving approve/reject/apply.
   *`sales/index.html`'s copy of this was corrected on 2026-08-12* — item 176's
   own follow-up added a shipped admin-UI Audit view to the "safe to claim"
   list, which made the stop-list entry a self-contradiction on the same page,
   so it could not wait. The landing-page instance is untouched and is what
   remains here. (It was left deliberately: that file carried unrelated
   uncommitted work at the time.)
3. **`README.md` contradicts itself on the admin UI** — one line calls the
   governance mutation API "REST-only for now, no admin UI", another describes
   the admin UI browser control plane in the same file.
4. **"a bound hit mid-scan degrades to a truncated, resumable page" is asserted
   unconditionally** in `audit/worm_search.py`, `README.md`, THREAT_MODEL
   QG-40, `docs/PRODUCT_GUIDE.md` (twice), the generated
   `docs/product-guide.html`, and `CHANGELOG.md`. True for every bound except
   the day-listing truncation — that is item 184. One clause naming item 184 at
   each site. **Derive the list, don't hand-assemble it:**
   `poetry run python scripts/claim_drift_sites.py worm-resumable`. Two hand
   attempts named 3 of ~14 and then 8 of ~14, and the second pointed at
   `worm_search.py:126` — the `request_timeout_seconds` bullet, the one bound
   that genuinely *does* resume — while missing `worm_search.py:117-119`, the
   `max_objects_scanned` bullet describing the exact bound item 184 breaks.
   Live sites also include `worm_search.py:104` and `:313`,
   `docs/THREAT_MODEL.md:193` **and `:415`**, `docs/PRODUCT_GUIDE.md:1304` and
   `:5779-5780`, **both** instances in `docs/product-guide.html` (436 and 780),
   `README.md:195`, `CHANGELOG.md:164`, `src/querygate/core/config.py:366-370`,
   and `docs/TODO_ARCHIVE.md:9463`. The script's `KNOWN_OK` table records why
   the timeout-bound lines must be left alone.
5. **`docs/TODO_ARCHIVE.md:1240`'s item-26 write-up still says "Phase 2 — MSSQL
   estimated-plan equivalent, not started"** while item 26 is `✅ DONE`
   including phase 2 and `estimate_mssql_query_cost` ships. Internal-only, same
   root as #1. *Found 2026-08-12 by `claim-reviewer`; previously unfiled.*
6. **`sales/PUBLIC_LANDING_RUNBOOK.md:57-60` forbids claiming four shipped
   capabilities** — four-eyes approval (item 42), an administration UI (item
   31), a WORM audit store (item 134 phase 1) and a production Helm reference
   (`deploy/helm/querygate`, asserted by `tests/unit/test_helm_ha_deployment.py`).
   **This file is the instruction sheet for landing-page copy**, so a stale
   stop-list actively reproduces #2's and #1's denials on every future edit —
   it is the mechanism by which `landing/security.html` stayed wrong, and it
   should be fixed FIRST. Left untouched here only because it carried unrelated
   uncommitted work.
7. **`landing/security.html:502` omits the item-184 non-exhaustive-paging
   caveat** that `CUSTOMER_README.md`, `sales/index.html`,
   `docs/business/GO_TO_MARKET.md` and README now carry. Omission, not a false
   statement — but the public security page is exactly who needs it.

8. **`src/querygate/execution/disclosure_budget.py:25-34` and
   `src/querygate/policy/models.py:501-510`** still call the shape cap "the
   targeted probe cap" and say `shape_fingerprint` "closes the evasion at the
   shape layer too" — the two places an engineer reads first, both wrong per
   item 186. *Found 2026-08-12 (round 5).*
9. **`docs/business/MARKET_DOMINATION_ANALYSIS.md:516-518`** still says
   multi-query differencing "stays honestly out of scope"; item 179 bounds it.
   *Found 2026-08-12 (round 5).*
10. **The four-eyes rollback exemption has no test.** Five surfaces now state it
   as a security residual, but flipping the gate to cover rollback (breaking DR)
   or dropping it entirely fails nothing. Add
   `test_rollback_to_a_previously_active_version_needs_no_approvals`.
   *Found 2026-08-12 (round 5).*

**Method note — read before fixing any of the above.** Four consecutive
`claim-reviewer` rounds (2026-08-12) each found that the *previous* round's fix
was scoped to the site list in the filing, and that every filing's list was
shorter than reality. A fifth round then found that the hand-grepped lists
written to fix exactly that were themselves short — 6 of ~17 and 8 of ~14. The
failure is the method, not the diligence, so the method is now a script:
`poetry run python scripts/claim_drift_sites.py <class>` (or `--all`). Fix every
hit that is a live claim, then re-run to confirm. Adding a class is three lines
in its `_CLASSES` table. It over-reports by design — a historical release note
and a live claim look identical to a regex — so every hit needs a human read,
and its `KNOWN_OK` table records the lookalikes a previous sweep already
cleared, with the reason, so a true statement is not "fixed" by mistake. A sweep that excludes a directory
can conclude only that the searched subset is clean, never that a class is
closed.

**Effort:** S. **Depends on:** nothing.

### 191. `write_preview` is an unfloored, unbudgeted exact-count oracle

**Surfaced 2026-08-12 by `security-invariant-reviewer` auditing the item-179
merge.** `execution/write_preview.py` runs `SELECT count(*) … WHERE <caller
predicate>` and returns the exact integer as `affected_rows`, with no
`min_group_size` floor and no disclosure charge. A caller holding write +
preview access on a k-floored table can difference through the preview path
indefinitely, unaffected by items 88 and 179.

Requires write scope, which is deny-by-default and separately granted, so this
is low severity — but `docs/INFERENCE_RISKS.md` R3/R4 discuss only the read
aggregate path, which makes the "bounded since item 179" summary read broader
than it is.

**What to do:** at minimum, add it to R3's residual list so the bound's scope is
honest. Whether the preview count should be floored or charged is a product
decision — a floored preview reports a row count the write itself will not
honor, so this is not obviously the right fix.

**Effort:** S (docs) / M (if floored or charged). **Depends on:** 93, 179.

### 192. The disclosure budget's Redis script passes multiple KEYS, which fails CROSSSLOT on Redis Cluster

**Surfaced 2026-08-12 by `test-contract-reviewer` auditing the item-179 merge.**
`RedisDisclosureBudgetLimiter` is the first limiter in the codebase to pass
**several KEYS to one Lua script** (one per charge). Under Redis Cluster those
keys hash to different slots and the call fails `CROSSSLOT` → `RedisError` →
the fail-closed branch → *every* aggregate query on a k-floored connection is
refused. `fakeredis` does not model slots, so no test can see it.

`deploy/HA_DR.md` recommends `CONCURRENCY_BACKEND=redis` whenever
`replicaCount > 1` without qualifying the topology, so an operator following it
onto a clustered Redis gets a hard outage on exactly the queries the feature was
enabled to protect.

**What to do:** either add a hash tag to `_redis_key` so one connection's keys
share a slot (`qg:disclosure:{<connection_id>}:…`), or state the
single-node/non-cluster assumption in the module docstring and `HA_DR.md`. The
hash tag is preferable and cheap; confirm it against a real clustered Redis
rather than fakeredis.

**Effort:** S. **Depends on:** 179 (shipped).

### 193. `docs/product-guide.html` has no freshness gate against `docs/PRODUCT_GUIDE.md` ✅ DONE

Shipped a drift guard that regenerates the page in memory and byte-compares it, in the default unit suite — plus a negative control proving a markdown edit changes the output, and a section-count assertion pinning the failure mode actually measured (whole sections going missing).

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 193).
### 194. Three crafted-or-corrupt WORM lines still escape `search_worm_archive` as an unhandled exception the route masks as a 500 ✅ DONE

All three crafted-or-corrupt line shapes are now counted rather than raised: a non-ASCII digest, a deeply-nested line at either `json.loads` site, and an over-deep `query_shape` (bounded by `AUDIT_WORM_SEARCH_MAX_QUERY_SHAPE_DEPTH`, default 64, fail-closed).

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 194).

### 195. The narrowing path: `Policy.templates_only` enforcement plus an observed-shape recorder that drafts a template from real traffic ✅ DONE

Shipped the discovery→narrowing bridge between the general `StructuredQuery`
surface and item 48's curated query templates: `Policy.templates_only`
(off by default) refuses an ad-hoc AST on execute/explain/verdict/batch at the
single `_validate_and_compile` choke point, exempting only a server-set
`template_id`; an opt-in, bounded, per-process recorder captures the
redaction-safe *skeleton* of each allowed query (every literal replaced by a
typed parameter slot, `intent` dropped); and a scope-gated
(`admin:shapes:read`) REST read plus a `querygate-shapes` CLI turn a recorded
shape into a reviewable `QueryTemplate` draft that is never auto-installed.
Also corrected `docs/INFERENCE_RISKS.md`'s stale Class A exhaustiveness
argument.

**Phase 2 (same day)** closed the three limits phase 1 disclosed rather than
fixed: a Redis-backed store (`admin/redis_observed_shapes.py`) makes the
discovery window shared across replicas and durable across restarts
(`scope: "shared-durable"`); the observed-shapes panel landed in the admin UI as
a read-only promotion surface with no install action; and the `between`
dry-run binder bug that made a drafted BETWEEN template fail
`querygate-validate-config` is fixed.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 195).

### 196. The container image's non-Python layers have never been licence-assessed ✅ DONE (phase 1)

**Why.** `make license-check` (`scripts/check_licenses.py`, GTM WP1, 2026-08-21)
gates every package in `poetry.lock` and emits `docs/THIRD_PARTY_LICENSES.md`.
That inventory is deliberately scoped to **Python packages**, and says so in its
own header — but the artifact QueryGate actually distributes is the container
image, and `Dockerfile` layers two things the inventory never sees:

- a full **Debian `bookworm`** userland from `python:3.11-slim-bookworm` (glibc
  under LGPL-2.1, plus the usual GPL-licensed coreutils/bash), and
- **`unixodbc`** plus Microsoft's **`msodbcsql18`**, installed with
  `ACCEPT_EULA=Y` — proprietary terms accepted at build time, in the image
  QueryGate hands to a customer.

Nothing here is presumed to be a problem: redistributing a Debian base image is
routine and the GPL components are separate programs, not linked into
QueryGate. The defect is that **it has not been looked at**, while a
customer-facing document now states a licence position for everything else. A
security or procurement reviewer who asks "and the OS layer?" currently gets an
explicit scope disclaimer rather than an answer, and the BSL flip is exactly
when that question gets asked.

**Definition of done.**

- Enumerate the image's non-Python components and their licences from the built
  image itself. Trivy already scans those layers in the `image-scan` job but
  emits only vuln/secret/misconfig findings, no SBOM and no retained artifact —
  so add `--format cyclonedx` to that existing step rather than introducing a
  second scanner.
- Establish whether `msodbcsql18`'s EULA permits redistribution inside a
  commercial product image, and whether it must be an opt-in layer instead. This
  is the one that could actually change the Dockerfile: if redistribution is not
  permitted, the ODBC driver becomes a documented operator-installed step and
  MSSQL support ships without it in the default image.
- Fold the result into `docs/THIRD_PARTY_LICENSES.md` (or a sibling document it
  links to) and delete the scope disclaimer once it is no longer true.
- Gate whatever is established, in the spirit of the Python-side gate: a new
  base-image component with an unreviewed licence should fail a build, not be
  discovered by a customer.

**Not before:** it does not block the flip on its own, but it must have an
answer before the first paid pilot's security review — the north-star metric.
Raised by the GTM WP1 licence pass, which found and scoped it rather than
silently leaving it out.

---

**Phase 1 ✅ DONE 2026-08-21** — the assessment is written up in
[`docs/CONTAINER_IMAGE_LICENCES.md`](docs/CONTAINER_IMAGE_LICENCES.md), measured
against a locally built image (124 OS packages; aarch64, so re-take the package
list on an amd64 build before quoting it to a counterparty).

Two things were found and one is fixed:

- **The driver was shipping with no licence text.** `msodbcsql18` declares
  `/usr/share/doc/msodbcsql18/LICENSE.txt`, but the slim base image
  path-excludes `/usr/share/doc/*`, so it never landed. `Dockerfile` now writes
  a `path-include` before installing, and `scripts/check_release_artifacts.py`
  fails the release if that line is ever removed while the driver is installed.
- **The Debian userland is not a problem.** GPL there is aggregation, not a
  combined work — the position every Debian-derived image relies on. The
  source-offer obligation is discharged the customary way (unmodified upstream,
  available from Debian's archives); that customary answer is the one line in
  the assessment counsel should confirm rather than take on trust.

**Phase 2 — open, and it needs counsel, not code.** Microsoft's EULA §2
expressly permits redistribution, but §2(b) attaches three conditions and
QueryGate meets one of them. §2(b)(ii) requires that end users be made to "agree
to terms that protect it and Microsoft at least as much as this agreement" —
BSL 1.1 has no pass-through clause, so a user pulling the image agrees to
nothing on Microsoft's behalf. §2(b)(iii) requires indemnifying Microsoft, which
nothing currently does. The assessment proposes a third-party-notices file plus
a pass-through clause as the ordinary fix, with making the driver an opt-in
layer as the fallback. Fold the indemnity question into the existing
indemnification/insurance conversation (`GTM_EXECUTION_PLAN.md` §3 Layer 3)
rather than running a separate one.

### 197. Offline entitlement token for the paid tier — ⛔ SUPERSEDED by items 210-213

**Do not implement this item. It is retained for history only; every design
decision in it was reversed.**

Its premise was Shape A — the BSL Additional Use Grant's unlimited free internal
production use — which left no production threshold for a licence to self-check,
so the token was to be an *entitlement marker for the paid tier*, never a gate.
That grant was cancelled on 2026-08-23 (`docs/business/GTM_SAAS.md`). QueryGate
is a paid subscription, and the entitlement now **blocks**: after the paid term
plus a grace window has fully elapsed, every governed query and write is refused
with HTTP 402.

Three specific commitments in the original body are now the opposite of the
product, which is why this item is superseded rather than merely re-scoped:

- *"Soft enforcement only … must never refuse to start and must never block or
  degrade a query"* — item 211's gate does exactly that, deliberately and
  disclosed (`docs/legal/EULA.en.md` §15).
- *"Zero network calls — no phone-home, ever"* — item 212's control plane is
  contacted on start-up and roughly daily, carrying the four fields §16.1 of the
  EULA discloses.
- *"The source is public under BSL; hiding the check would be theatre"* — the
  source is not public, and item 214 compiles the enforcement call sites into
  the shipped artifact.

**What survived**, and where it went: the Ed25519-signed, offline-verifiable,
locally-checked token shape is the design items 211-213 build on
(`docs/CONTROL_PLANE_PLAN.md` §3), and the availability objection this item
raised was answered rather than dropped — a *failed refresh* is never
fail-closed, cold start fails open, and administration, health and audit
retrieval survive a lapse. `docs/LICENSE_ENFORCEMENT.md` carries the reversal
notice and the enforcement-model survey both items rest on.

### 198. QueryGate Notary — append-only transparency log for the audit ledger's chain head — not-before-customers

**Effort: M. NOT BEFORE CUSTOMERS — do not claim or implement. Originally
logged per `docs/business/GTM_EXECUTION_PLAN.md` §4 (owner decision,
2026-08-21); pitch it now, build it when a customer asks, not before.**

**Scope unchanged by the 2026-08-23 licence decision** (`docs/business/GTM_SAAS.md`,
which supersedes that plan), unlike its sibling item 197. Two things did move
around it: `GTM_SAAS.md` §7 lists Notary among the trust-rebuilding measures that
matter *more* under closed source, and `docs/LICENSING_FAQ.md` names it as a
forward statement a prospect must not be allowed to mistake for a shipping
service.

**Why it matters:** the shipped hash-chained audit ledger (item 91) is
tamper-evident, but `docs/business/NORTH_STAR.md` and `docs/PRODUCT_GUIDE.md`
both already record the honest limit: **unkeyed, it is tamper-evident only
against an externally anchored head** — without a third party attesting to the
chain head at a point in time, an operator with write access to the ledger
file can still rewrite history and re-derive a consistent chain from genesis.
That documented dependency implies a natural product, named for the first
time in the GTM plan: a hosted service that anchors the head hash
independently of the operator who could otherwise rewrite it. A customer
cannot substitute self-hosting it — an anchor a customer runs themselves is
their own infrastructure attesting to their own logs, which proves nothing;
third-partyness is the product, the same way the Structural pillar is won by
construction rather than by policy.

**What it is, when built:**
- **Receives only chain head hashes** — no queries, rows, schema, or
  credentials ever leave the customer's network; bytes meaningless in
  isolation. This does not touch the data plane and does not weaken the
  Reach pillar.
- **Asynchronous, never in the request path.** Audit must never depend on
  Notary's uptime. **The comparison this bullet used to draw at item 197 no
  longer holds** — the subscription entitlement (items 211-213) *can* block a
  query, by design and by disclosed contract. Notary is different in kind: it
  attests to records the customer already holds, so a Notary outage must
  degrade to "not yet anchored", never to "audit unavailable". An availability
  dependency on a third-party service, for a control that buys the customer
  nothing at request time, is a security-review finding.
- **An append-only transparency log from day one, with published keys.** A
  notary able to forge or retro-edit an attestation is a security-review
  finding *about the vendor* — the one failure mode this product cannot
  afford. Signed receipts, no PII, no data-residency question.

**Definition of done, when this is picked up:** the anchoring client lives
outside `execution/service.py`'s request path entirely (a background
publisher reading committed chain heads, never awaited by a query); the
service itself (receipt issuance, key publication, the transparency log) is
out of `src/querygate/`'s request-serving surface and scoped as its own
component; `security-invariant-check` confirms no query/row/schema/credential
data is ever included in an anchored payload.

### 199. Human SSO: OIDC sign-in for the browser surfaces, a built-in local identity provider, and a file-configured claim→scope mapping ✅ DONE (phase 1)

**Effort: L.** Requested by the maintainer 2026-08-23.

**Why it mattered:** items 10 and 90 already let QueryGate *verify* an IdP's
JWT, and item 95 published the scope vocabulary to register in that IdP — so an
agent or a service could authenticate as a human. A **human** could not. Both
browser surfaces made a person paste a bearer token
(`admin_ui/app.js`'s `connect()`, `access_ui/app.js`'s), which is a credential
handling practice no enterprise security review passes: it trains operators to
copy long-lived tokens between windows, it cannot express "this person is in
the platform-oncall group", and it leaves no way to end someone's access
without rotating a key somebody else also holds. The Proof pillar claims
per-human attribution; before this, the *humans* had no first-class way in.

Three gaps, all closed in phase 1:

1. **No interactive sign-in.** No authorization-code flow, no session, no
   logout.
2. **No discovery or provider presets.** An operator hand-wrote
   `JWT_JWKS_URL`/`JWT_ISSUER`/`JWT_AUDIENCE` per IdP.
3. **No group→authority mapping.** `jwt_scopes_claim` read one claim as a
   space-delimited scope string. Entra ID emits app roles in `roles` and groups
   as opaque GUIDs in `groups`; Keycloak nests realm roles at
   `realm_access.roles`. None of those are QueryGate scopes, so "sign in with
   Entra" would have meant "sign in with no authority at all".

**Phase 1 — shipped 2026-08-23.** New `src/querygate/identity/` package:

- **One OIDC implementation, nineteen named providers plus every other one.** `identity/oidc.py` runs
  authorization-code + PKCE (S256 always — an IdP that cannot do S256 is
  *refused*, never downgraded to `plain`); `identity/presets.py` holds a
  behaviour-free `ProviderPreset` per IdP (Entra ID, Okta, Auth0, Google
  Workspace, Keycloak, authentik, PingOne, PingFederate, OneLogin, JumpCloud,
  GitLab, AD FS, Amazon Cognito, Cloudflare Access, ZITADEL, Authelia, WorkOS,
  FusionAuth, Salesforce, plus `generic`) carrying only the issuer template,
  default scopes, and claim names. Issuer templates interpolate exactly three
  values — `{tenant}`, `{domain}`, `{region}` — a deliberate ceiling: an IdP
  needing a fourth uses `generic` with an explicit issuer rather than growing
  the model a bespoke field per vendor. `identity/discovery.py` reads `.well-known/openid-configuration`,
  enforcing RFC 8414 §3.3 exact issuer equality and https-only endpoints, TTL
  cached with one outbound fetch per issuer under contention.
- **A built-in local identity provider** (`identity/local_store.py`,
  `local_auth.py`, `passwords.py`, `totp.py`) for deployments with no external
  IdP and for break-glass access when the IdP is down. `hashlib.scrypt`
  (N=2^16) rather than a new argon2 dependency; RFC 6238 TOTP against the
  stdlib; per-account lockout; a fixed dummy verifier so an unknown username
  costs the same as a real one; a bounded semaphore so parallel logins cannot
  turn a memory-hard KDF into self-inflicted exhaustion. One locked writer for
  `users.yaml` (`LocalUserFileRepository`, mirroring `CatalogFileRepository`).
- **File-configured claim→scope mapping** (`identity/mapping.py`,
  `examples/identity.example.yaml`). Deny-by-default, grant-only, order-
  independent; validated against `core/scopes.ALL_SCOPES` and `ROLE_BUNDLES` at
  load so a typo fails the reload instead of silently granting nothing. Applied
  to browser sessions **and** — via `JWT_MAPPING_PROVIDER_ID` — to bearer JWTs,
  so one human holds the same authority through either door.
- **Sessions that re-derive authority per request.** A session stores the IdP's
  *claims*, never resolved scopes, and `identity/authenticators.py` maps them
  fresh on every request — so tightening `identity.yaml` and reloading takes
  effect on the next request rather than at the next logout. The browser holds
  an opaque token; the store holds its SHA-256. Every cookie-authenticated
  request must also carry the CSRF token (`X-QueryGate-CSRF`) — on *every*
  method, not only unsafe ones, so a new read endpoint cannot become
  cross-site-reachable by someone judging it "safe".
- **RFC 8628 device grant** (`identity/device.py`) so a browserless tool gets a
  short-lived token carrying the approving human's identity, narrowed to the
  intersection of what it asked for and what the approver holds — and narrowed
  *again* against the live mapping on each request, so revoking a group
  deactivates an already-issued token without waiting for expiry.
- **Admin surface** (`api/admin_identity_routes.py`) under two new scopes,
  `admin:identity:read`/`admin:identity:write` (deliberately not folded into
  `admin:config:*`: minting an account is a different privilege from changing
  what a connection exposes) — local-account CRUD, TOTP enrolment shown exactly
  once, provider inspection with no `client_secret` field on the model at all,
  a mapping simulator, and break-glass revocation of a person's sessions and
  tokens.
- **Audit.** New `AuthenticationEvent` (`identity.authentication`) joins the
  `PersistableEvent` union; it has no field capable of holding a password,
  code, token, or claim set. Adding it also removed a real drift hazard:
  `api/admin_ui_routes.py` kept its own hand-written frozenset of event types,
  now derived from the union by `audit.events.persistable_event_types()`.
- **Both UIs** offer "Continue with <provider>" and a local sign-in form,
  resume an existing session on load, and sign out server-side. The
  paste-a-token path still works unchanged.

*Mutation-verified before landing (CLAUDE.md's rule), which paid for itself:
three enforcement points passed a green 3,289-test suite with no test actually
pinning them — single-use login flows, login-flow expiry, and store-level
session expiry. Tests added. It also showed `is_safe_relative_path`'s two
open-redirect layers are mutually redundant; kept deliberately and documented,
rather than "simplified" down to one parser's edge cases.*

**Phase 1b — a local development identity provider ✅ DONE 2026-08-23.**
Requested by the maintainer: *"add local bypass so it works full flow but
actually its local so i dont need actual SSO set up for it to work."*
`identity/dev_idp.py` serves QueryGate's own OIDC provider at
`<sso.base_url>/dev-idp` — real discovery document, real JWKS, real RS256 ID
tokens signed with a per-process key, and **real PKCE verification** on
exchange. Nothing in `identity/oidc.py` is bypassed or stubbed: the browser
takes the same redirect and the callback runs the same state/nonce/signature/
audience checks. Only *who vouches for the human* is make-believe.

Zero configuration: with no `users:` block the personas are **derived from the
mapping rules** — one per rule, named by its description, plus one matching
nothing so deny-by-default is a click away rather than something to construct.

Fenced three independent ways, because it is an authentication bypass by
definition: the config validator refuses `DEV_IDP_ENABLED=true` outside a local
`ENVIRONMENT`; `build_dev_idp_router` re-checks rather than trusting it was
reached legitimately; and the signing key is generated per process and never
written anywhere, so a leaked dev token dies at the next restart. Each fence has
its own test in `tests/security/test_sso_boundary.py`.

This also closed the honest gap phase 1 shipped with — the redirect path could
only be verified in pieces, because verifying it whole meant registering an
application with a real IdP. `tests/integration/test_dev_idp_full_flow.py` now
drives login → authorize → callback → session → scope-gated call against a real
uvicorn server, and it immediately found a real defect: `derive_personas` was
emitting a dotted claim path (`realm_access.roles`) as a **flat key**, so the
rule that inspired a persona would not fire on it. Fixed with `nest_claim`.

**Phase 2 — Redis-backed SSO state ✅ DONE 2026-08-23.**
`identity/redis_sessions.py` adds `RedisSessionStore`, `RedisLoginFlowStore`,
`RedisDeviceGrantStore`, and `RedisIssuedTokenStore`, wired in the same lifespan
block as the concurrency/quota/disclosure/observed-shape stores. The failure
mode this removes is different from theirs: a per-replica session store does not
multiply a budget, it signs people *out* at random.

**Deliberately Lua-free**, and the reason is the point: a session record is a
JSON document full of arrays (`groups`, `scopes`, whatever the IdP nested in
`claims`), which is exactly what item 195 phase 2's `cjson` round-trip trap
destroys — on real Redis, invisibly under fakeredis. So it takes CLAUDE.md's
own structural way out: the document is written once and never decoded
server-side, and `last_seen_at` — the only mutable field — lives in its own key
a plain `SET` updates. Single-use comes from `GETDEL`, every command touches
exactly one key (no `CROSSSLOT` possible), and reads **fail closed**: an
unreachable store means nobody is signed in. Guarded by AST-level assertions
that no Lua is executed and no multi-key command is issued, since no behavioural
test can catch either class.

**Phase 3 — `querygate login` CLI ✅ DONE 2026-08-23.**
`login_cli.py` (`querygate-login`). Prints the user code, opens the
verification URI, polls honouring `interval` and backing off on `slow_down`,
and distinguishes denied / expired / timed-out from "the gateway is down".
Refuses to send a credential over plain http to a non-loopback host. Writes
nothing to disk by default — it prints an `export` line; `--save` writes
`~/.querygate/token` created 0600 (via `os.open` with the mode, not a
write-then-chmod, which would leave a world-readable window). `--quiet` emits
only the token so `$(querygate-login --quiet)` works.
`tests/integration/test_login_cli.py` drives it against a real server with a
timer thread playing the human who approves.

**Phase 4 — SAML 2.0 (DECISION-GATED, do not start).** Deliberately not built:
every mainstream IdP speaks OIDC, and SAML costs an `xmlsec`/`python3-saml`
native dependency in the container image plus the XML signature-wrapping attack
surface — a poor trade against one well-tested code path. Build it only when a
design partner actually mandates SAML, and record that decision first.

### 200. Per-surface credential-type policy: enabling SSO must actually close the console to shared secrets ✅ DONE

A per-surface allowlist over `Principal.auth_method` (console / REST / MCP) whose default drops static API keys from the admin control plane the moment SSO is enabled.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 200).
### 201. The WORM archive tier is reachable only against AWS S3, so on-prem and air-gapped deployments cannot have it ✅ DONE

`AUDIT_WORM_S3_ENDPOINT_URL` points both the WORM flush monitor and the managed search at any S3-API-compatible store with Object Lock (MinIO, Ceph RGW), so on-prem and air-gapped deployments can have the immutable archive too — not AWS S3 alone.

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 201).

### 202. The `digests_equal` hazard is unfixed at six `hmac.compare_digest` sites outside `audit/ledger.py`, all comparing request-supplied strings

**Filed 2026-08-23 by `architecture-boundary-reviewer` and `claim-reviewer`
independently**, auditing items 184/194/185/201. **Pre-existing** — the six
sites landed with the human-SSO work in `620d90d`, one commit before that
range; item 194 established the repo's answer to this defect class but applied
it only inside `audit/ledger.py`.

`hmac.compare_digest` accepts two `str`s only when BOTH are ASCII-only, and
raises `TypeError` otherwise. `audit/ledger.py`'s `digests_equal` (item 194
defect 1) fixes this for the five ledger sites. Six others compare a string
that arrives from a request and have no such guard:

- `identity/totp.py:94` — **the reachable one, measured**: the guard is
  `candidate.isdigit()`, which is `True` for Arabic-Indic digits.
  `"١٢٣٤٥٦".isdigit()` is `True` with `len == 6`, so a six-character non-ASCII
  "code" passes the length/digit guard and raises out of `verify_totp`.
- `identity/oidc.py:143` (`state`, straight off the callback query string) and
  `:288` (the `nonce` ID-token claim, `isinstance(str)`-checked but not
  ASCII-checked) — raise instead of `SsoLoginError("state_mismatch"/
  "nonce_mismatch")`.
- `identity/authenticators.py:112` and `api/sso_routes.py:251` — a CSRF token
  read directly from a request header; raise instead of `CsrfMismatchError`/403.
- `identity/dev_idp.py:185` — the PKCE `code_challenge`.

**Not a bypass, and the severity should not be inflated.** Every one of these
fails CLOSED: the exception aborts the request, so the security decision is
still "reject". The cost is availability plus error-class masking — a clean
401/403/422 becomes a masked 500, and the SSO paths lose their specific
`error=` code, which is what an operator debugging a federation problem reads.

**Verified clean, do not "fix" these three:** `core/auth.py:114` and
`identity/passwords.py:118` compare `bytes` (always safe), and
`execution/approval.py:401` compares a caller-supplied `str` but already sits
inside `except (ValueError, TypeError, json.JSONDecodeError): return False`.

**What to do:** promote `digests_equal` out of `audit/ledger.py` into `core/`
— it is a general safety wrapper, not a ledger concept — and use it at the six
sites. Keep a re-export or update the ledger's imports so item 194's
`test_hmac_compare_digest_is_called_in_exactly_one_place` still holds, and
consider widening that source-level guard to the whole `src/` tree, which is
what would have caught this class in the first place.

**Acceptance criteria:** `verify_totp("١٢٣٤٥٦", secret=...)` returns
`TotpResult(valid=False)` rather than raising; a non-ASCII CSRF header yields
403 not 500; a non-ASCII `state` yields `SsoLoginError("state_mismatch")`. All
three raise today.

**Effort:** S. **Depends on:** 194 (defect 1 shipped), 199 (shipped, phase 1).

### 203. An unparseable predecessor exempts one chain link on a resumed page, while an exhausted blank-run walk fails closed — same threat shape, opposite posture

**Filed 2026-08-24 by `security-invariant-reviewer` (WS-172-9)**, re-reviewing
the items 184/194/185/201 fixes. **Pre-existing since item 172's WS-172-1
seed walk**; item 194 defect 2 only made it visible by adding `RecursionError`
alongside the `json.JSONDecodeError` sibling that already behaved this way.

`_seed_chain_state_from_predecessor` returns `(None, None, False)` — the
ACCEPT-AS-GIVEN tuple — when the predecessor line is unparseable, so a resumed
page's first incoming link is never checked. Its blank-run sibling returns
`(None, None, True)` (fail-closed) for a threat of the same shape, on the
reasoning that "a genuine segment has zero blank lines, so this only fires
against a crafted/corrupted object". **A genuine segment also has zero
unparseable lines**, so the same reasoning argues for the same posture.

**Attack, and its narrowness.** An actor with `s3:PutObject` deletes a record
from the middle of a segment (no forging, so this bites the keyed-HMAC
configuration too), which breaks linkage at the following record. Overwriting
the line immediately BEFORE that record with an unparseable blob makes the
seed walk return accept-as-given, so a page resuming exactly at the following
record reports `chain_breaks == 0`. Narrow because the attacker cannot choose
where the auditor's page boundary falls — though a caller-supplied
`limit`/`cursor` determine it — and one `malformed` count remains as a weak
signal.

**What to do:** this is a deliberate decision, not a drive-by — the comment at
the `RecursionError` handler says so explicitly. The consistent change is
`return None, None, True` at BOTH the `except (json.JSONDecodeError,
RecursionError)` branch and the pre-existing sibling, making the resumed
page's first link a counted `chain_break`. It is false-positive-free by the
same argument the blank-run case already uses. If instead the exemption is
deliberately kept, record it in the function's three-way contract docstring as
an accepted residual alongside WS-172-3 (tail truncation).

**Acceptance criteria:** a segment whose middle record is deleted and whose
preceding line is unparseable, resumed at the following record, reports
`chain_breaks == 1`. It reports 0 today.

**Effort:** S. **Depends on:** 172 (shipped), 194 defect 2 (shipped).

---

## SaaS subscription work packages (items 210–219)

Owner decision, 2026-08-23: QueryGate ships **proprietary, closed-source, as a
paid monthly subscription**. See `docs/business/GTM_SAAS.md`, which supersedes
`GTM_EXECUTION_PLAN.md` and the Shape A / BSL decision of 2026-08-21 in full.

**Numbering note:** 199–209 were reserved for the concurrent human SSO /
identity work stream (item 199 and its phases). These items start at 210.
**201, 202 and 203 have since been allocated** by the concurrent WORM
audit-archive work (201 shipped and referenced from `core/config.py`,
`audit/worm_sink.py`, tests and CHANGELOG; 202 is itself an SSO-code defect
found while auditing it). 204–209 remain free for the SSO stream
to avoid a collision. Per CLAUDE.md item numbers are permanent and file-global;
a reserved gap is intentional, like the unused 98.

**Items 197 and 198 are superseded in premise.** 197 (offline entitlement token,
soft-warn, not-before-customers) is replaced by items 210–213 — the token is no
longer a receipt that warns, it is a subscription that blocks. 198 (Notary) keeps
its scope and stays deferred. Both bodies need rewriting as part of item 210.

### 210. Proprietary licence transition — retire the BSL apparatus ✅ DONE

`LICENSE` is now a proprietary notice pointing at `docs/legal/EULA.en.md` as the
licence of record; the EULA carries the subscription's commercial clauses in both
languages; the phone-home guard was narrowed rather than deleted; and the
licensing claim sweep went from 235 sites to 35 deliberate lookalikes (part
rewritten, part recorded as whole-document exemptions — see the write-up).

**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 210).

### 211. Subscription layer — the entitlement gate ✅ DONE (phase 1)

**Effort: L.** Depends on 210 for the licence text, on 212 for real entitlements.

**Why it matters:** this is the wrapper that makes the product a subscription.
It must stop the product when unpaid without ever becoming an availability risk
when *we* fail, and without lying in the customer's audit ledger.

**What it is:**
- **⚠️ Two constraints item 210 already landed as tests. Read these before the
  first commit, because both fail loudly and neither is negotiable.**
  1. `tests/security/test_no_phone_home.py` exempts **exactly the package path
     `src/querygate/subscription/`** from the vendor-hostname and
     licence-server-vocabulary bans. Anywhere else in `src/querygate/` still
     fails the build, so the client cannot be started in
     `execution/` or `core/` "temporarily". The exemption is also **bounded at
     10 modules** (`_MAX_EXEMPT_MODULES`). The package list below is eight names
     **plus `__init__.py`, which `rglob("*.py")` counts** — nine, with one slot
     of head-room. That bound was 8 in the guard's first draft, which would have
     tripped on this item's own intended package on its first commit; a tripwire
     that fires before any drift has occurred only teaches people to raise it.
     If you genuinely need an eleventh module, raise it against a written module
     list and say why — never to get a suite green.
  2. The package must declare a module-level
     **`PAYLOAD_FIELDS`** literal naming every field it transmits, and it must
     equal `{"org_id", "deployment_id", "connection_count", "seat_count"}` —
     and the package must satisfy `payload_contract_violations()` in that same
     file — one literal declaration, no `**` spread into a dict literal, no dict
     literal passed as `json=`/`data=`/`content=`, and no literal key outside the
     disclosed set plus transport plumbing. **That checker is already
     detector-tested against planted bypasses, so it holds the moment this
     package exists.** What this item still owes is the runtime half: a
     schema-level test asserting the actual refresh request body's key set, built
     in the shape of `test_credential_redaction.py` rather than as a mock-call
     assertion —
     the four fields `docs/legal/EULA.en.md` §16.1 discloses and
     `docs/business/GTM_SAAS.md` §3 publishes. A fifth field is a **disclosure
     change before it is a code change**: amend the EULA (both languages), the
     FAQ, and `NORTH_STAR.md`'s Reach pillar first.
- **`src/querygate/subscription/`** — a peer package to `audit/` and `secrets/`:
  `models.py`, `verify.py` (pure, no I/O), `state.py` (module singleton),
  `gate.py`, `sources.py` (the registry above; the only modules doing outbound
  HTTP), `manager.py`, `cache.py`, and **`cli.py` exposed as `querygate-license`
  in `[project.scripts]`** — `status` renders `SubscriptionStatus` and is the
  only place that formats the model. That console script does not exist today and
  two documents already reference it.
  **`cache.py`'s Protocol is `async def` from the first commit**, per CLAUDE.md
  and the `admin/observed_shapes.py` precedent, even though the in-process
  implementation needs no `await` — it is exactly the shape that grows a shared
  backend under multi-replica, and a sync-then-widened Protocol is what broke the
  compensation store. **`gate.py`'s read is the one deliberate synchronous
  exception**: it reads an already-evaluated in-memory `SubscriptionStatus`,
  never a store, so it adds no await point to the request path — and it must
  never read `cache.py` directly.
- **Two enforcement funnels, not one.** Reads:
  `StructuredQueryService._validate_and_compile` — its own comment already names
  it as "the four ways an ad-hoc AST reaches a database". Writes: the three write
  **service** entry points (`WriteExecutionService.execute`,
  `_execute_many_atomically`, `WritePreviewService.preview`) via one shared
  helper in `execution/`. **`WriteExecutionService` and `WritePreviewService` are
  separate classes and `_execute_many_atomically` opens its own `session_scope`;
  a gate only in `StructuredQueryService` leaves every governed
  INSERT/UPDATE/DELETE running.**
  **Do *not* put the write gate in `validation/write_policy_validation`** — it is
  a pure function of (AST, Policy) with no I/O and no process state. Gating there
  makes every existing shape-validation unit test depend on the subscription
  singleton, and makes any non-executing caller (a linter, the admin candidate
  simulator, a dry-run) receive a 402 for asking whether a *shape* is legal.
- **Third funnel: schema discovery — decided 2026-08-23, gate it.**
  `list_tables`, `describe_table` and `search_catalog` reach the live database
  via `list_live_tables`/reflection and pass through **neither** other funnel —
  `_validate_and_compile`'s comment is precise that it covers the ways an *AST*
  reaches a database, and these carry no AST. Same for `catalog/refresh.py`'s
  background reflection, which must also stop. They are product, not
  diagnostics, so an expired deployment enumerates nothing. Note this makes
  "two funnels" three, and the source-level import guard must permit
  `subscription.gate` at all three.
- **The forbidden-edge guard is bidirectional.** Acceptable: `execution/` →
  `subscription.gate`; `api/`/`mcp/` → `core.exceptions` plus one narrow
  read-only status accessor for item 216; `subscription/` → `core/` and
  `metrics.py`; the lifespan → `subscription.manager`. **Forbidden in either
  direction:** `validation/`, `compiler/`, `connections/`, `policy/`, `catalog/`,
  `schema/`, `query_ast/`, `write_ast/` ↔ `subscription/`. Compilation and
  session guardrails must not vary by entitlement, and `policy/` importing
  `subscription/` would put observe/enforce inside the customer-reloadable
  `Policy` — the customer-settable bypass this design exists to prevent. Also
  forbid `subscription/` → `execution/`, `validation/`, `audit/`.
- **Acquisition varies; verification does not.** Two real backends are in scope —
  HTTP refresh and the air-gapped offline entitlement file — so this is a genuine
  Protocol + registry case, not ceremony: an `EntitlementSource` Protocol
  (`async def fetch() -> bytes`) with `HttpEntitlementSource` and
  `OfflineFileEntitlementSource` behind a `_SOURCES` registry keyed on the
  configured mode. Without it the offline mode lands as an inline
  `if cfg.offline_entitlement_file:` in `manager.py` and every downstream
  question (does the high-water mark apply? what is `RefreshHealth` for a file?
  how does the serial floor advance?) grows its own branch. `verify.py`'s rules
  and the serial floor then apply to both by construction.
- **Two orthogonal state axes**, so the gate can never depend on refresh health:
  `EntitlementState{VALID,GRACE,EXPIRED}` and
  `RefreshHealth{OK,DEGRADED,FAILING}`, combined in a frozen
  `SubscriptionStatus` computed by one pure `evaluate()`. The gate reads
  `.allows_queries`; health reads `.entitlement`; the CLI renders the model.
  Nobody else compares a datetime.
- **`SubscriptionExpiredError` in `core/exceptions.py`**, not in
  `subscription/` — `api/_errors.py` and `mcp/exceptions.py` import only from
  `core.exceptions`. It must not subclass `ValueError`/`PolicyViolationError`
  (existing handling would map it to 422 and label it `policy`). Register it in
  **seven** places or it degrades silently: `_ACTIONABLE` + a 402 handler
  (`api/_errors.py`); `_error_code_from_exception` before the `INTERNAL`
  fall-through (`mcp/exceptions.py`, which also logs a traceback);
  `classify_rejection` (`metrics.py`); **`public_error_message`
  (`core/exceptions.py`)** — a closed isinstance allow-list that both transports
  render every message through, so without it the 402 body is "An unexpected
  error occurred." and item 216's "the message names the renewal URL" is
  unsatisfiable; the **`policy_decision` bucket** (`execution/service.py`), or
  the persisted event records `policy_decision="unknown"`; and
  **`_DENIAL_GUIDANCE`** (`help/personal_denials.py`), or the caller's
  self-service explanation reads "the specific reason wasn't recorded". Getting
  `classify_rejection` alone fixes the metric and stops the WORM ledger blaming
  `db_error`, but stops three registries short.
- **Four surfaces swallow the gate, not three.** `_execute_batch_item`,
  `explain_many` and `WriteExecutionService._batch_error` turn it into a per-item
  string inside an HTTP 200 — **and `verdict()`/`verdict_many` are worse**: their
  deliberate, documented catch-all returns HTTP 200
  `{"allowed": false, "reason": "not-available-to-you"}` and writes
  `policy_decision="denied"` into the tamper-evident ledger, telling the customer
  their *policy* refused a query it actually permits, permanently. Re-raise
  `SubscriptionExpiredError` ahead of every one of them, and check the gate once
  per request **before** the quota reservation and the concurrency slot, so an
  expired deployment neither spends a caller's quota nor holds a slot per refused
  request.
- **Observe/enforce is a field in the signed entitlement**, on the
  `CostEstimationMode.OBSERVE/ENFORCE` precedent — not a config flag (a
  customer-settable bypass), not a build flag (the enforce path would never run
  in the shipped image), not absent code (go-live day becomes the first
  execution of a new hot-path call site). Observe emits
  `querygate_subscription_would_block_total`.
- **Wall-clock high-water mark, not monotonic.** `time.monotonic()` is
  boot-relative and cannot be persisted; a fresh container would read a
  catastrophic regression and expire on first restart. Tolerance in hours;
  regression enters **grace**, never expiry; absent mark is fail-open; clamp a
  mark implausibly far ahead; per-process, monotone-max on write; a persistent
  write failure is visible, not silent. Written by the timer only — never from
  `audit/logger.py` or an `AuditSink`, both of which run per-query.
- **A serial floor** (`max_seen_serial`, strictly increasing) is the primary
  anti-replay control: it needs no trustworthy clock and has no false-positive
  mode.
- **Verification:** strict parse (unknown fields at a known `schema_version`,
  duplicate JSON keys, base64 `validate=True`); verify over the exact decoded
  bytes then parse; domain-separated signatures; manifest binding; freshness and
  max-term bounds as compiled-in constants; typed failure reason per rule; **the
  disk cache holds signed bytes re-verified on every read**, never a parsed
  structure.
- **`SubscriptionManager`, not `LeaseManager`** — `CredentialLeaseMonitor`
  already owns that word and emits `action="lease_refresh"`. Copy its four
  properties: I/O off the loop, fail-open per iteration, **only the exception
  type logged never its message**, and a `system:` principal.

**Definition of done:** `security-invariant-check` clean; mutation verification
on each verification rule individually; `tests/conftest.py`'s autouse fixture
clears every new process-wide item (verdict, wall-clock mark and its file, the
serial floor, cache path, manager, task, event, HTTP client, failure counter,
manifest cache, `app.state`, **and the observe-mode Prometheus counter** — the
collector lives in the process-global `REGISTRY`, which conftest resets nothing
in today, so any absolute assertion on it is order-dependent); the bidirectional
import guard above; **a schema-level test asserting the refresh request body's
key set is exactly what `GTM_SAAS.md` §8 publishes** — built in the shape of
`test_credential_redaction.py`, not as a mock-call assertion, because that
sentence is the load-bearing answer to the phone-home objection and is currently
unbacked; and a test that the rendered 402 body and the MCP error message both
name the renewal URL.

### 212. Control plane — accounts, Stripe subscriptions, entitlement issuance

**Effort: XL.**

**Why it matters:** the vendor side. Without it there is no revenue and item 211
has nothing to verify.

**What it is:**
- **Lives in `control-plane/`**, its own Poetry project with its own lockfile —
  Stripe, KMS and session dependencies must not enter the product's lock, SBOM,
  or `dep-audit` scope. **Mirror CI gates are part of this item**, and the
  specific root settings matter: the Makefile's `bandit -r src/` and semgrep
  `src/` scopes mean the mirror must be **new targets, not a widened scope** (or
  control-plane findings land in the product's SAST job);
  `tests/unit/test_security_posture_commands.py` asserts **exactly one** semgrep
  invocation in `ci.yml`, so adding the mirror step fails it with a drift message
  that looks unrelated; `testpaths = ["tests"]` correctly excludes
  `control-plane/tests` from a root `pytest` and must **stay** that way, because
  the natural "fix" drags Stripe and KMS imports into the product test
  environment; add `"control-plane"` to `check_release_artifacts.py`'s
  `FORBIDDEN_PARTS` (the cheapest possible guarantee the signing plane can never
  ship in a wheel or sdist) and to `.dockerignore` (the Dockerfile's COPY
  allowlist makes the image safe today, but the ignore entry survives a Dockerfile
  edit). `[tool.poetry] packages` and `--cov=src` need **no** change — say so, so
  nobody "fixes" them.
- **Define `deployment_id` once, normatively** — it currently means three things
  across items 212, 213 and 215. One *logical* deployment holds one entitlement
  and one cap slot; an ephemeral per-replica `instance_id` rides alongside it.
  Otherwise a ten-replica Kubernetes Deployment is simultaneously "one deployment
  refreshing from N sources" (item 213's abuse signal) and ten times the
  per-deployment refresh rate against a limit sized for one — while a
  per-container id would consume N cap slots on every rolling deploy and break
  item 213's rebuild guarantee. Restate item 213's detection heuristic as "N
  *unrelated* source networks", and note that item 211's high-water mark and
  serial floor are per-process, so a fleet's floors must converge rather than
  fight.
- **Stack:** FastAPI + SQLAlchemy 2 + Alembic + Postgres; Stripe SDK; cloud KMS.
- **Stripe is the source of truth for paid status.** Do not rebuild subscription
  lifecycle, dunning, proration, tax, or invoices.
- **Six tables:** `org`, `subscription`, `entitlement`, `deployment`,
  `enrolment_token`, `entitlement_issuance` (append-only), plus `webhook_event`
  for idempotency.
- **Billing semantics:** monthly prepaid; cancellation is immediate for renewal
  purposes but the entitlement's `expires_at` stays at the end of the paid
  period; **no proration, no refund**.
- **Endpoints:** Stripe webhook (signature-verified, idempotent), `POST /v1/enrol`
  (one-time token → per-deployment key), `GET /v1/entitlement` (per-deployment
  bearer key). Admin/minting endpoints bind to a private network behind SSO or
  mTLS. **Rate-limit per deployment, not per org** — a per-org limit is
  self-abusable to force refresh failure and ride the fail-open window.
- **Keys:** offline Ed25519 root on hardware signs a key manifest; issuing keys
  in KMS with `sign` permission only. **Ed25519 signing is available on GCP
  Cloud KMS and not on AWS KMS** — verify all vendor facts with a dated note
  before committing to a provider.
- **The control plane resolves plan/features from the entitlement, never from
  the request.** A client must not assert its own tier.
- **Tier limits are enforced in software — decided 2026-08-23.** The refresh
  carries `connection_count` and `seat_count` alongside the two identifiers, and
  the control plane compares them against the entitlement. Three consequences to
  build, not assume: the counts are *operational facts about the customer's
  estate*, so item 210's EULA transmission-disclosure clause must name them and
  state retention; the counts are **reported, never trusted for authorisation** —
  they drive upgrade prompts and overage flags, and a mismatch is a commercial
  event, never a reason to block a query; and going over-limit must degrade to a
  notification and a renewal conversation, since blocking on a miscount would
  turn a billing disagreement into a production outage.

**Definition of done:** webhook replay and signature tests; enrolment token is
single-use; issuance ledger is append-only and never updated; no credential,
query, row, or schema is stored anywhere in the plane; tenancy isolation tests.

### 213. Activation — bind a deployment to a subscription via OAuth2 + MFA

**Effort: L.** Builds directly on item 199's `identity/` package.

**Why it matters:** this is the customer's first five minutes and the moment the
product becomes theirs. It is also the security boundary: an unactivated
deployment must be inert.

**Depends on item 199 (`identity/`), which is NOT in this branch** — it is
uncommitted work on the identity stream. Do not start 213 until 199 merges.

**What it is:**
- **⚠️ Activation must prove *entitlement to bind this box*, not merely a signed-in
  identity.** As first drafted this item authenticated a human and then bound the
  deployment to whatever org that human belongs to — so anyone who reaches a
  fresh container before the operator (a VPC co-tenant, someone scanning the cloud
  VM just started, a contractor on the LAN) completes OAuth with **their own**
  account and becomes administrator of a gateway inside the customer's network
  that is about to hold production database credentials. Required: the portal
  issues a **single-use ≥128-bit claim code** at checkout; the activation screen
  demands it *before* any OIDC redirect starts; the control plane refuses to bind
  to any org but the code's holder. Add a **local-presence factor** for the
  browser flow — a one-time secret printed to the container's stdout on first
  boot — so network reach alone is insufficient.
- **⚠️ "Nothing else" is unreachable with the gate an implementer will naturally
  write.** A router-level `Depends` — the pattern the whole `api/` package uses —
  does **not** cover `app.mount()`ed sub-apps, so the MCP surface and both static
  UIs stay live; and no request-scoped gate reaches the four lifespan monitors
  (`HealthMonitor`, `CatalogRefreshMonitor`, `CatalogUsageLearningMonitor`,
  `CredentialLeaseMonitor`), which dial every configured database and resolve
  secrets from the secret backend at boot, before any human has authenticated.
  Required: a **pure-ASGI middleware installed on the app**, a **default-deny
  closed allowlist** of path prefixes, and a lifespan that starts **no**
  connection-touching monitor until the activation transition.
- **Bound the unauthenticated surface.** Activation is the first unauthenticated
  REST endpoint in the product, and **no per-source rate limiter exists anywhere
  in the codebase** (the only limiter is per-principal quota, and there is no
  principal here). It needs: failure counting per source address *and* per claim
  code with lock-after-N; a slot consumed only on **successful** enrolment, never
  on attempt; a failed attempt that cannot lock out the legitimate operator; and
  an explicit `Content-Length` and structural-depth cap before parsing — reuse
  `mcp/transport_guard.py`'s `_structural_depth_exceeds`, do not write a second.
- **Reuse the identity *mechanism*, not the store.** `identity/` is
  deployment-side: its OAuth client lives in the customer's `identity.yaml`, and
  `sso_enabled` is off by default. Activation must authenticate against **the
  control plane's own** authorization server (item 212 owns the client and the
  RFC 8628 endpoints) and must work with `sso_enabled=false` and no
  `identity.yaml`. Reuse `oidc.py`'s hardened primitives — PKCE-S256, state
  binding, nonce comparison, asymmetric-only algorithms — as a library. Note
  `identity/device.py`'s device grant requires approval **in that deployment's
  admin UI**, which an unactivated deployment does not serve, so it cannot
  bootstrap activation as-is.
- **Enforce audience binding** on any token the deployment accepts, the same
  posture `core/config.py` already requires for the MCP resource server —
  otherwise a token minted for the internet-facing portal authorises
  administration of a gateway inside the customer's network. And do **not**
  register a per-deployment `redirect_uri` at Google/Microsoft: a wildcard
  covering every customer hostname makes code interception straightforward.
- **MFA is not currently verifiable on the external path.** MFA enforcement
  exists only for the built-in local IdP (`local_auth.py`'s `require_mfa`,
  `totp.py`); there is **no `amr`/`acr` check anywhere** for Google/Microsoft. So
  either request `acr_values` and verify the `amr` claim carries a second factor,
  rejecting the session otherwise — preferred, this is the activation boundary —
  or stop writing "MFA enforced" and say the customer's IdP enforces it.
- **`deployment_id` is assigned by the control plane**, never proposed by the
  client, with a **deployment cap** enforced at enrolment.
- **What binding actually buys, split honestly.** *Enforced:* enrolment count
  (single-use token means N is a hard ceiling) and per-deployment revocation
  (each deployment holds a distinct bearer key). *Only detected:* runtime
  instance count — one enrolled key copied to N containers is invisible.
- **The persisted deployment key is a credential.** Re-activation without
  consuming a slot forces it onto a mounted volume; document its location and
  file mode, state that its compromise equals entitlement theft, and forbid it
  from entering the config-version store or any audit event.
- **Emit an activation audit event** carrying org id, deployment id and operator
  subject — and **never** the enrolment token, the OIDC `id_token`, or the
  per-deployment key.
- **Re-activation and transfer**: a documented path for rebuilt hosts, restored
  backups, and blue/green deploys that does not require a support ticket.

**Definition of done:** activation is reachable and completable from a clean
`docker run` with no config file; an unactivated deployment provably refuses
every data path; device flow works on a headless host; re-activation after a
container rebuild does not consume a new deployment slot.

### 214. Single obfuscated compiled binary — spike first, then build

**Effort: XL, and the spike is the first thing done in the whole programme.**

**Why it matters:** the enforcement story depends on the gate not being one
editable Python line, and the product's IP is now the business. But this
codebase is a hostile compilation target and finding that out in month three
would invalidate the packaging plan.

**What it is:**
- **Phase 1 — the spike (XS/S, do it first).** Attempt a Nuitka build of the
  full application and answer: does `pydantic-core` (a Rust extension) survive?
  Does SQLAlchemy's dynamic dispatch? Do the six MCP tool modules still register
  — CLAUDE.md documents that `MCPServer` resolves each tool's forward references
  against the *wrapping* function's `__globals__`, which is exactly the
  introspection compilers break. Do console-script entry points and
  `importlib.metadata` still resolve? Report go/no-go with evidence before any
  further work.
- **Phase 2 — the build.** Reproducible compile in CI; the subscription layer
  and the app compiled together so enforcement call sites are inside the
  artifact; the image is the only distribution channel; cosign signature and
  SLSA provenance preserved (both already exist in `release.yml`).
- **Accept and document the costs**: stack traces become far less useful for
  support, and a native extension complicates the SBOM and pip-audit story that
  §7 of `GTM_SAAS.md` leans on. Decide how support debugging works *before*
  shipping, not after the first incident.
- **No obfuscation theatre.** Compilation raises the bar; it does not make the
  gate unbypassable, and the EULA's anti-circumvention clause (item 210) is the
  layer that actually holds.

**Definition of done:** the compiled image passes the full test suite and
`make release-smoke`; a documented support-debugging procedure exists; the spike
report is committed even if the answer is no.

### 215. One-command install and first-boot self-configuration

**Effort: M.**

**Why it matters:** "single command or action and it's automatically set up" is
the promise. Today a deployment needs two YAML files (connections, policy) plus
env configuration before it does anything.

**What it is:**
- **One copy-pasteable `docker run`** (plus an equivalent compose file and a
  Helm values snippet) shown on the checkout success page and in the portal.
- **⚠️ The image must not ship the anonymous-auth bypass. This is the single
  highest-severity item in the phase.** `api/auth.py` appends
  `AnonymousAuthenticator` whenever `cfg.is_local` and no `api_keys` and no JWT
  — and `is_local` is true for the default environment, the Dockerfile sets no
  `ENVIRONMENT`, and `_validate_production_auth` only fires on
  `environment == "production"`. So `docker run <registry>/querygate:latest`, the
  exact command this item promises, currently yields a deployment where every
  request resolves to an anonymous principal, FastAPI `debug` is on, and the
  OpenAPI schema is public — and the first connection added below is then
  queryable by anyone who can reach the port. Fix: `ENV ENVIRONMENT=production`
  in the Dockerfile, satisfied by the generated admin credential below, with
  `debug`/`openapi_url`/`docs_url` off in the shipped image regardless of
  environment.
- **First-boot self-configuration**, with the mechanics named rather than
  implied: secrets generated with `secrets.token_urlsafe`/`os.urandom` (never
  `random`, never derived from a hostname); written **once, atomically,
  create-if-absent (`O_EXCL`)** so concurrent workers and replicas cannot race
  and so a restart never regenerates; to a documented path and file mode on a
  volume the non-root `querygate` user can actually write; **never regenerated
  if present**, because silently rotating on a rebuilt image turns an upgrade
  into an unplanned re-activation and breaks item 213's re-activation guarantee.
- **Seed-once, then one writer.** First boot seeds `connections.yaml`,
  `policy.yaml` and an empty `catalog.yaml` **only when absent**, before any
  store is constructed, and never mutates an existing file. Every subsequent
  write — including "add the first connection" — goes through the existing
  `admin/service.py` stage/apply path, which works for a single operator at
  `require_config_approvals=0`. **Do not add a second connections writer**, and
  never route catalog content through `ConfigVersionStore`; catalog mutations go
  through `CatalogFileRepository`'s lock.
- **Safe-by-default starter policy — decided 2026-08-23: build the real
  guarantee (item 220).** `Policy.table_allowed` returns `True` when
  `allowed_tables` is empty — **an empty allow-list means allow-all** — and
  `denied_tables` has no wildcard, so today the only deny-all lever is
  `default: {enabled: false}`, which is all-or-nothing. Item 220 adds the
  no-allow-all-fallback `Policy` field so "enabled, zero tables" becomes
  expressible; **215 depends on it** and ships the starter policy on top of it.
  Do not ship the `enabled: false` stopgap as the final answer.
- **⚠️ "The credential goes straight to the configured secret backend" has no
  implementation today** and must not be written as if it does. On a fresh
  install the only registered backend is `env:` (Vault is off by default), which
  is not writable from a request, and `ConnectionRegistry` has no runtime
  mutation API. The only otherwise-reachable path writes the literal DSN into
  `var/config_versions/.../connections.yaml` in plaintext and returns it verbatim
  from `GET /admin/config/versions/{id}` under **read** scope — laundering a
  credential through an untyped `str` that `test_credential_redaction.py` cannot
  see, which engages non-negotiable 2. Therefore: this item ships the **write
  side** of a secret resolver as a prerequisite; the UI stores only a `${...}`
  reference in `connections_yaml`; and a literal connection string is **rejected
  at the boundary**, never persisted.
- **`querygate-quickstart` is not the bootstrap.** It is a read-only,
  authenticated HTTP client against an already-running, already-configured
  gateway that "never writes anything". It stays that way and the setup guide
  links to it as the last step; first-boot self-configuration is new server-side
  code.

**Definition of done:** a clean machine goes from the command to a governed
query in under ten minutes with no file editing; the flow works identically on
Docker Desktop, a cloud VM, and Kubernetes; `build_authenticator` under the
shipped image's own defaults contains **no** `AnonymousAuthenticator`; the
starter policy denies by the recorded mechanism; a literal (non-`${...}`)
connection string is rejected by the first-connection flow and never appears in
any config-version response; first boot never overwrites an existing config;
and a source-level test asserts `admin/service.py` is the only module that
writes `connections.yaml`.

### 216. Renewal countdown and lapse UX

**Effort: M.** Depends on 211.

**Why it matters:** a subscription that stops without warning is a support
incident and a chargeback. The countdown is the product being honest.

**What it is:**
- **Trigger:** auto-renew off (or payment failing) **and** under 30 days
  remaining.
- **Surfaces:** a persistent banner in `admin_ui` and `access_ui` with a
  **day countdown** and the exact expiry date; escalation under 7 days; an
  email; a coarse field on the health endpoint; a Prometheus gauge; a line in
  `querygate-license status`.
- **Say exactly what happens at expiry**: every query and write is refused with
  402, and audit export keeps working. Link straight to the renewal URL.
- **The unauthenticated `/health` endpoint carries only a coarse state** — no
  org, plan, dates, or counts. It is deliberately unauthenticated and returning
  aggregate detail only; publishing an expiry date there tells any scanner
  exactly when this customer's gateway stops.
- **The same constraint binds the Prometheus gauge**, which is otherwise the
  unguarded sibling: `metrics_require_auth` defaults to true but **false is
  supported**, and a `days_remaining` gauge on such a deployment discloses
  strictly more than the health field just forbade. The gauge is a coarse state
  enum; the day countdown lives only on the authenticated UI banner, the email,
  and `querygate-license status`.
- The 402 body and the exception message carry a fixed operator-facing string:
  no org id, deployment id, serial, plan, or date.
- **Accessibility is part of this item**, not a follow-up: the banner is a live
  region, dismissible without losing the information, and legible at the
  contrast the rest of the UI meets.

**Definition of done:** `ui-a11y-reviewer` clean; a test per surface asserting
the countdown appears at the right threshold and the 402 message names the
renewal URL.

### 217. Customer portal — signup, checkout, downloads, docs

**Effort: XL.** Depends on 212.

**Why it matters:** the commercial front door. Everything the customer touches
before the product itself.

**What it is:**
- **Sign-up and sign-in via OAuth2 (Google/Microsoft) with MFA** — the same
  identity the deployment activates against, so activation is a recognition
  rather than a second account.
- **Stripe Checkout** for the self-serve tiers; **Stripe's hosted billing
  portal** for payment methods, invoices and cancellation — do not rebuild it.
- **Post-checkout success page is the install page**: the one-line command, the
  compose file, the Helm snippet, and a link to the quickstart.
- **Subscription management**: plan, seats, connections, renewal date,
  auto-renew toggle, invoices, and the **cancellation flow that states plainly —
  before confirming — that there is no refund and access runs to the end of the
  paid period**.
- **Deployments view**: which deployments are enrolled, last seen, and a
  revoke/rotate action.
- **Docs and download links**, versioned, with release notes.
- **No customer secrets in the portal database** beyond what Stripe and the
  identity provider already hold.

**Definition of done:** a stranger with a card can go from landing page to a
governed query without contacting anyone; cancellation, renewal, and card
failure are all exercised end to end against Stripe test mode.

### 218. Setup guides and quickstart docs for the SaaS motion

**Effort: M.**

**Why it matters:** the existing docs are written for an operator who clones a
repo and edits YAML. Every one of them is now wrong about how the product is
obtained and started.

**What it is:** a one-screen quickstart (checkout → command → activate →
first query); per-target deploy guides (Docker, compose, Kubernetes/Helm, a
major cloud); connecting the first database; writing a first policy; connecting
an agent over MCP; the air-gapped/offline-entitlement guide; a troubleshooting
page whose first entry is "my subscription lapsed"; and a rewrite of
`CUSTOMER_README.md` and the landing/sales copy to match the new motion.

**Definition of done:** `claim-verify` clean against the new docs; the
quickstart is verified by following it on a clean machine, not by reading it.

### 219. Pre-launch codebase cleanup pass

**Effort: L, and it is a real item, not a tidy-up afterthought.**

**Why it matters:** the codebase is about to stop being a prototype with a
public future and start being a product people pay for. Everything a customer
cannot see still has to be right, because nobody outside can review it any more.

**What it is:** run the repo's own audit skills and fix what they find, rather
than improvising a definition of clean —
- **`repo-audit`** — whole-repo invariant drift.
- **`dep-audit`** — CVEs, lockfile drift, the deny-by-default allowlist.
- **`test-gap`** — untested code and missing coverage classes.
- **`claim-verify`** — every doc and marketing claim backed by code and a test.
- **`security-invariant-check`** over the subscription and activation surfaces.
- Delete dead code left by the BSL removal; reconcile `ROADMAP.md` to `TODO.md`;
  archive fully-shipped items per the `ship-item` rules; make
  `make release-check` and the full CI matrix green.
- **Resolve the two open defects already logged**: item 192 (the disclosure
  budget's Redis script passes multiple `KEYS` and fails `CROSSSLOT` on Redis
  Cluster) and item 194 (three crafted-or-corrupt WORM lines escape
  `search_worm_archive` as an unhandled exception the route masks as a 500).
- **Three defects surfaced by the 2026-08-23 review of items 210–219**, logged
  here rather than lost:
  - `classify_rejection` returns `"templates_only"` but `_DENIAL_GUIDANCE` has no
    such key, so a principal narrowed to templates who submits an ad-hoc query
    gets "the specific reason wasn't recorded" — the least actionable message for
    the most actionable rejection, and exactly what item 195's dedicated category
    existed to prevent. Green today because the lookup has a `.get` fallback.
  - **No shared type binds the rejection vocabulary.** `classify_rejection`,
    `AuditEvent.error_category`, the `policy_decision` 4-tuple, and
    `_DENIAL_GUIDANCE` are four parallel free-string maps; any new category
    silently diverges in three places. The bullet above is one instance and item
    211 will add the next. A `RejectionCategory(StrEnum)` in `core/` consumed by
    all four — worth doing **before** 211 lands its value.
  - `GET /admin/config/versions` and `/versions/{id}` return full
    `connections_yaml` (which may hold a literal credential) under **read**
    scope, while `GET /drafts/{id}` requires **write** scope for identical
    content and encrypts at rest. Raise the versions endpoints to write scope, or
    project the YAML out of the read-scoped model — the exclusion mechanism
    already exists in `admin/store.py`. Item 215 makes this materially worse by
    making literal credentials routine.

**Definition of done:** all five skills report clean or with every finding
triaged to a decision; `make release-check` and `make release-smoke` green; no
`✅ DONE` item left unarchived.

### 220. Deny-by-default at table and column granularity: a `Policy` allow-list with no allow-all fallback

**Effort: M. Blocks item 215.** Owner decision, 2026-08-23.

**Why it matters:** `Policy.table_allowed` returns `True` when `allowed_tables`
is empty, and `column_allowed` follows the same convention — **an empty
allow-list means allow-everything**. `denied_tables` has no wildcard. So the
only deny-all lever in the model today is `Policy.enabled = False`, which is
all-or-nothing: the moment an operator enables a connection to run their first
query, every table and column on it is readable up to the numeric caps. That is
the opposite of what a new install should do, and it makes the "safe-by-default
starter policy" item 215 promises literally inexpressible. It is also a poor
default for a product whose entire pitch is that it governs *what a query is
allowed to be*.

**What it is:**
- A `Policy` field with **no allow-all fallback** — working name
  `require_explicit_table_allowlist: bool = False` — under which an empty
  `allowed_tables` means *deny every table* rather than *allow every table*. The
  same treatment for `allowed_columns`.
- **Default `False`, so no existing deployment changes behaviour.** This is a
  new opt-in guarantee, not a silent tightening of everyone's policy — a
  behaviour flip on an existing security control is exactly the change that
  breaks a customer at 3am.
- The shipped starter policy (item 215) sets it `True`, so a fresh install
  denies until the operator names tables deliberately.
- **Both branches mutation-verified.** Per CLAUDE.md's working agreement:
  break the empty-allow-list branch in each direction and confirm a test fails
  *for that reason*. A swapped boolean here silently opens every table on every
  connection that opted in, with a green suite — the same class as items 101 and
  114.
- Documented in `examples/policy.example.yaml` (whose comments currently
  concede the shipped default is permissive) and in the policy section of
  `docs/PRODUCT_GUIDE.md`.

**Definition of done:** `security-invariant-check` clean; a test that an
existing policy with an empty allow-list and the flag unset still allows (no
behaviour change), and one that the same policy with the flag set denies; both
mutation-verified; `examples/policy.example.yaml` and the product guide updated.

### 221. Move validator bodies out of the model classes so the enforcement logic can be compiled

**Effort: M. Depends on the item 214 packaging decision.** Owner decision needed
before starting — this touches the AST enforcement core.

**Why it matters:** item 214's spike measured that a module defining
`pydantic.BaseModel` subclasses cannot be Cython-compiled at all (pydantic v2's
metaclass rejects `cyfunction` methods). That leaves **30 validators, 602 lines**
of real enforcement logic readable in the shipped image:

| Module | Validators | Lines |
|---|---|---|
| `query_ast/models.py` | 21 | 396 |
| `write_ast/models.py` | 4 | 50 |
| `policy/models.py` | 3 | 50 |
| `connections/models.py` | 2 | 106 |

Join-form, window-scope, CTE-name, set-op and credential-shape checks — the
Structural pillar's actual implementation. The *shape* of these models is
already public (OpenAPI publishes 215 component schemas including every AST
node), so nothing is lost by leaving the declarations interpreted. The **logic**
is a different question, and today it ships in the clear.

**What it is:** a mechanical split, per module —

```python
# query_ast/models.py — stays interpreted; it is the published contract
@pyd.model_validator(mode="after")
def _validate_join_form(self):
    return _validators.validate_join_form(self)

# query_ast/validators.py — compilable, holds the logic
def validate_join_form(q): ...
```

- One `validators.py` per model module; the model keeps only the decorator and a
  one-line delegation.
- **No behaviour change.** Same functions, same raise sites, same messages.
- The AST models' recursive-rebuild ordering must survive (see
  `mcp/tools/__init__.py`'s docstring and item 214) — validators run at
  validation time, not import time, so this should be inert, but assert it.

**Definition of done:** all four modules split; `pytest -m unit` and
`-m security` unchanged in count and outcome; each `validators.py` verified to
Cython-compile with the suite green against the `.so`; a source-level test that
no `@pyd.*validator`-decorated function in the four model modules contains more
than a delegation; `security-invariant-check` clean.

**Do not start without the owner's go-ahead** — 30 call sites on the enforcement
core is not a cleanup, and if item 214 lands on a packaging approach that does
not need it, the whole item is unnecessary.

### 222. The product-guide HTML generator emits a document fragment, not a page

**Effort: S.** Found by the `ui-a11y-reviewer` during item 210's audit; filed
rather than fixed there because it is a generator change, unrelated to
licensing, and it invalidates `docs/product-guide.html` until regenerated.

**Why it matters:** `docs/product-guide.html` is the browsable customer-facing
copy of `docs/PRODUCT_GUIDE.md` and part of what a prospect is handed.
`scripts/generate_product_guide_html.py`'s `PAGE_TEMPLATE` begins at `<title>`
and goes straight into `<style>` — **no doctype, no `<html lang>`, no
`<meta charset>`, no viewport meta**. Both hand-written siblings
(`landing/security.html`, `sales/index.html`) get all four right, so this is
drift in the generator, not a house style.

Three measured consequences:

- **No `lang`** — a WCAG 3.1.1 (Level A) failure; a screen reader announces the
  page in the user's default voice.
- **No viewport meta** — the generator's own `@media (max-width: 900px)` block
  is the only thing that unpins the 288px fixed sidebar and restores
  `margin-left: 0`. Without the meta tag it never fires on a phone: the page
  loads at the ~980px fallback viewport, zoomed out, sidebar overlaying content.
- **No `meta charset`** — the file carries raw UTF-8 em dashes and ellipses. Over
  `file://` there is no HTTP charset header, so the browser falls back to its
  locale default.

**Also in scope** (same reviewer, same surface, all pre-existing and none
caused by item 210):

- **No skip link** in the generated page, so a keyboard user tabs through all
  eight sidebar nav links before reaching `<main>`. Both hand-written siblings
  ship one.
- **The Decision Log renders as two sibling `<ul>` elements**, so a screen
  reader announces "list, 40 items" then "list, 96 items" for one chronological
  list. Root cause is `render_blocks`, which terminates a list run on any line
  that is neither a bullet nor a two-space continuation.
- **`sales/index.html`'s `summary::after` `+`/`−`** may fold into the
  disclosure's accessible name in Chrome and Firefox, duplicating the native
  expanded/collapsed announcement. `content: "+" / ""` suppresses it where
  supported.
- **`sales/index.html`'s clipboard write has no `.catch`**, so a
  permission-denied or insecure-context rejection leaves the button label
  unchanged and the `role="status"` live region silent.
- **`generate_product_guide_html.py`'s `inline()` interpolates a link href
  without quote-escaping** (`html.escape(text, quote=False)` leaves `"`
  intact). Not exploitable — the sole input is a trusted repo file and no
  operator- or database-supplied string reaches the generator — but it is a
  latent break-out in attribute context.
- **`tests/security/test_no_phone_home.py` scans only `*.py`.**
  `src/querygate/admin_ui/app.js` and `access_ui/app.js` ship in the package and
  are outside all three rules. **This becomes load-bearing at item 216**, which
  puts a renewal URL into those UIs — extend the scan to `.js` before then, or
  216 will place a vendor URL in shipped source that no guard sees.

**Definition of done:** the generated page has a doctype, `lang`, `charset` and
viewport; `make product-guide-html` regenerated and
`test_product_guide_html_freshness.py` green; a skip link; the Decision Log a
single list; the phone-home scan covering `.js`; the four smaller items above
either fixed or explicitly declined in this item's body with a reason.
