# Technical and Product Review

**Date:** 2026-07-23. **Reviewer scope:** full-repository due diligence — architecture,
security boundary, reliability, tests/CI, and documentation-vs-implementation
cross-check. This document is a point-in-time assessment; see `ROADMAP.md`'s
"Technical and Product Improvement Plan" section and `TODO.md` items 107–113
(plus the item-93 Phase 3b note) for the tracked follow-up work.

## Executive Summary

QueryGate is a mature, disciplined codebase, not an early-stage prototype —
106 tracked TODO items (most shipped), ~150 source modules, ~1,630 collected
tests across unit/integration/security tiers, and process docs
(`docs/PRODUCT_GUIDE.md`, `docs/business/NORTH_STAR.md`,
`docs/ENGINE_EXPRESSIVENESS_PLAN.md`) that are unusually well-reconciled to the
actual code. The core non-negotiables — no caller-controlled raw SQL, no
credential ever on a returned model, redaction-safe audit, one catalog
mutation path — all hold under direct inspection, not just by convention.

The review found **one broken feature in an in-flight, uncommitted change**
(governed-write undo/rollback was non-functional, not just under multi-replica
HA as documented — see Finding 1) — **fixed during this review** (the
production call sites were completed and the affected unit test corrected;
`pytest -m unit` now passes 230/230) — plus a small number of narrow, real
gaps in the newer write-execution surface, batch/quota interaction, and CI
scheduling, still open. It found **no new security-boundary defect** — auth,
credential redaction, catalog governance, and the AST/policy boundary all
checked out clean against direct code inspection, which is itself a
meaningful positive signal given how much of the product's value proposition
rests on that boundary holding.

Net assessment: **production-credible core.** The one release-blocking
regression found is now fixed and verified; what remains is a handful of
small, well-scoped hardening items (Findings 2–5 below, tracked as `TODO.md`
items 107–113). Nothing found here calls for architectural rework.

## Product Understanding

- **Primary users:** teams operating AI agents (via MCP or REST) that need
  read/write access to live operational Postgres/MSSQL databases without
  handing the agent a raw-SQL surface. The buyer is typically a platform or
  security team gating agent-database access, not an end user directly.
- **Core workflows:** (1) an agent submits a validated `StructuredQuery` AST,
  which is policy-checked, schema-checked, compiled to SQLAlchemy Core, and
  executed with concurrency/quota/audit guardrails; (2) the same shape for
  governed writes (`write_ast`), gated by approval and boundable/reversible;
  (3) an admin/operator configures connections, policy, and the semantic
  catalog through a single governed mutation path with review/rollback.
- **Current maturity:** post-MVP, pre-first-paying-design-partner, per
  `ROADMAP.md`'s own framing ("the gate on everything is landing one paid
  design partner"). The read path is deep and hardened (item 96's canonical
  visitor, item 97's bounded subqueries, extensive adversarial coverage); the
  write path (item 93) is newer and comparatively thinner, which the findings
  below reflect.
- **Assumptions this review makes, labeled as such:** that the working-tree
  diff to `execution/compensation.py` (uncommitted at review time) represents
  genuine in-progress work toward the already-tracked "durable cross-replica
  compensation store" item, not an abandoned experiment — the finding stands
  either way, but the recommended fix assumes the direction is worth
  finishing rather than reverting.

## Strong Foundations

- **The canonical reference visitor (item 96) is real, not aspirational.**
  Every read-side AST node that carries a column reference — select variants,
  joins, where/having, group/order, top_n — routes through `iter_column_refs`
  or its siblings; there is no second, forgotten walk that could let a
  column reference dodge policy allow/deny or masking.
- **Credential and audit redaction hold under direct inspection.**
  `PublicConnectionInfo` structurally has no credential field;
  `tests/unit/test_credential_redaction.py` asserts this against the live
  OpenAPI/MCP schemas, not just by convention. Persisted `AuditEvent` types
  structurally exclude SQL/params/rows/exceptions.
- **Catalog governance's single-mutation-path invariant holds.** Draft
  proposals are provenance-validated to never reach `search_catalog`/
  `agent_visible` without going through `publish_proposal`'s `verified` status.
  32C's adaptive-learning signals are typed, redaction-safe, and cannot
  self-publish.
- **JWT/auth verification has no obvious bypass.** The `algorithms` allowlist
  is explicit (never derived from the token header), audience/issuer are
  enforced when configured, and failures are fail-closed without leaking the
  token into logs.
- **The documentation discipline is genuinely working.** Of ~15 concrete,
  checkable claims spot-verified across README/SECURITY_POSTURE/
  THREAT_MODEL/COMPLIANCE_MAPPING/the security benchmark, only two stale
  numbers were found (Finding 6) — everything else, including the
  reject-don't-emulate MSSQL nulls decision and the reproducibility of the
  published security benchmark, matched the code exactly.

## Highest-Priority Findings

### 1. Governed-write undo was non-functional (not just cross-replica) — FIXED during this review

- **Severity:** Critical
- **Confidence:** Confirmed (reproduced, then fixed and re-verified)
- **Effort:** Small
- **Evidence:** `execution/compensation.py`'s `CompensationStore.put/get/consume`
  were converted to `async def` in an uncommitted working-tree change (prep for
  a Redis-backed durable store), but the three call sites in
  `execution/write_execution.py` (`get(...)` ~line 207, `consume(...)` ~line
  217, `put(...)` ~line 427) were not yet updated to `await` them when first
  found. `get()` returned an unawaited coroutine object; the subsequent
  `record.connection_id` access raised `AttributeError` instead of the intended
  "unknown/expired" handling, and `put()`'s coroutine was silently discarded
  (the record was never stored), with `RuntimeWarning: coroutine ... was never
  awaited`. Originally reproduced: `poetry run pytest -m unit` → `1 failed, 229
  passed` on `test_write_execution.py::test_compensation_store_ttl_and_single_use`.
- **Impact (while broken):** the shipped "bounded reversibility (undo)"
  feature (`TODO.md` item 93 Phase 3a) did not work at all in a single process
  — a strictly worse state than the already-documented "process-local, needs
  single-replica affinity" caveat.
- **Resolution:** the three `write_execution.py` call sites were completed
  (now `await get_compensation_store().get/consume/put(...)`), and the test
  itself — which called the now-async store methods synchronously — was
  converted to `async def` with matching `await`s. Re-verified:
  `poetry run pytest -m unit` → **230 passed, 0 failed**. The separate,
  still-open record-eviction gap (`consume()` marks `consumed=True` but never
  removes the entry from `self._records`, so records accumulate for the
  process's lifetime even past TTL) is **not yet fixed** — tracked in
  `TODO.md` under item 93's Phase 3b note and `ROADMAP.md`'s Review Phase 1.

### 2. Batch query execution double-reserves quota on an approval retry

- **Severity:** Medium
- **Confidence:** Confirmed
- **Effort:** Small
- **Evidence:** `execution/service.py`'s `_execute_batch_item` (~line 718) calls
  `self.execute()`, which reserves per-principal quota via
  `enforce_query_quota` (~line 522) near the top of `execute()`, *before* the
  item-92 approval gate runs later in the same call. When the first attempt
  raises `ApprovalRequiredError` and an injected `approval_resolver` (MCP
  elicitation) obtains a token, the batch item retries by calling
  `self.execute()` a second time — reserving quota again.
- **Impact:** A principal running interactive-approval batches over MCP
  consumes roughly double the intended quota per approved query, silently
  halving effective throughput. No existing test (`test_admission.py`,
  `test_query_quota.py`, `test_approval.py`, `test_mcp_elicitation_approval.py`)
  asserts quota consumption across this specific retry path.
- **Recommendation:** Thread the already-obtained quota reservation through
  the retry so the second `execute()` call doesn't re-reserve; add a
  regression test. Tracked as `TODO.md` item 107.

### 3. Write-preview diff executes the full DML before the row cap is checked

- **Severity:** Medium
- **Confidence:** Confirmed
- **Effort:** Small
- **Evidence:** `execution/write_preview.py`'s `preview()` computes `affected`
  via a policy-checked `COUNT(*)`, but when `include_diff=True` it
  unconditionally calls `_mutation_diff`, which for an `UpdateStatement`
  executes the real UPDATE (`await session.execute(dml)`, ~line 203) inside
  the later-rolled-back transaction — regardless of whether `affected` already
  exceeds `WritePolicy.max_affected_rows`. Only the *rows shown* in the diff
  response are capped (`max_diff_rows`); the row-locking UPDATE itself is not
  skipped for an over-cap write.
- **Impact:** A caller can request `include_diff=true` against a broad WHERE
  clause to force a full-table UPDATE (row locks, WAL/redo activity, lock
  contention with concurrent writers) purely to preview a write that would be
  rejected outright as over-cap. This is a resource-exhaustion/lock-contention
  risk on what is meant to be a side-effect-free preview endpoint.
- **Recommendation:** Short-circuit the diff (skip `_mutation_diff`, return
  `within_affected_cap=False`) when `affected > max_affected_rows`, before
  running any DML. Tracked as `TODO.md` item 108.

### 4. MCP write batch has no size cap

- **Severity:** Medium-High
- **Confidence:** Confirmed
- **Effort:** Small
- **Evidence:** `mcp/tools/write.py` accepts an unbounded
  `writes: List[...]`; the read path's equivalent tool enforces
  `validate_batch_size(len(queries), policy)` (`mcp/tools/query.py:128`,
  backed by `policy_validation.py`), but `WritePolicy` has no
  `max_batch_size`-equivalent field, and `write_policy_validation.py` never
  checks batch size.
- **Impact:** A caller can submit an arbitrarily large batch of
  individually-in-cap writes in one MCP call, each running the full
  validate→compile→execute pipeline — the write path currently has weaker
  sizing guardrails than the read path it was explicitly modeled on.
- **Recommendation:** Add `WritePolicy.max_batch_size` and enforce it before
  processing any statement in the batch. Tracked as `TODO.md` item 109.

### 5. No scheduled (cron) CI run for dependency/security scans or the soak test

- **Severity:** Medium
- **Confidence:** Confirmed
- **Effort:** Small
- **Evidence:** `.github/workflows/ci.yml`'s only triggers are `push:
  branches: [main]` and `pull_request` — no `schedule:` trigger exists
  anywhere in `.github/workflows/`. `make test-soak` (`Makefile:112-115`,
  `SOAK_ROUNDS=100`) is never invoked by CI; only the lighter
  `QUERYGATE_LOAD_ROUNDS=5` `test-load` config runs in the `postgres-live` job.
- **Impact:** A CVE disclosed against an already-merged, unchanged dependency
  isn't caught until the next incidental PR touches the repo. A
  slow-degradation or connection-pool-leak regression that only surfaces
  after dozens of rounds passes every PR and is caught only if a maintainer
  remembers to run `test-soak` manually before a release.
- **Recommendation:** Add a nightly/weekly scheduled workflow running the
  CVE/SBOM/lockfile checks and `make test-soak` against `main`. Tracked as
  `TODO.md` item 112.

## Architecture and Maintainability

- **Duplicated WHERE-predicate tree walk across four validators** —
  `policy_validation.py`, `schema_validation.py`, `write_policy_validation.py`,
  and `write_schema_validation.py` each hand-roll their own recursive
  WHERE-boolean-tree enumerator rather than sharing one implementation the
  way item 96 centralized column-ref walking into `iter_column_refs`. All four
  are correct today; the risk is drift the next time `WhereNode` grows a new
  combinator (a new node type needs updating in four places, easy to miss
  one) — exactly the class of bug item 96 was built to prevent for column
  refs. *Low severity, Confirmed, Small effort.* Tracked as `TODO.md` item 111.
- **`value_subquery` in a write's WHERE is validated at the wrong layer** —
  `UpdateStatement`/`DeleteStatement` reuse the read `WhereNode`, so a
  `value_subquery` predicate is structurally legal there, but neither write
  validator inspects it; it only fails inside `compiler/write_compiler.py`
  because the write compiler always passes `ctx=None`. Not exploitable today,
  but it fails with a compiler-internal error instead of a clean validation
  rejection, and is a latent trap for a future write-compiler change. *Low-
  Medium severity, Confirmed, XS effort.* Tracked as `TODO.md` item 110.
- Everything else inspected in the compiler/AST/transport layer — dialect
  handling, template binding, REST/MCP error-mapping consistency, the
  read-side validators — held up well against direct review; no dead code,
  god objects, or layer violations worth flagging were found beyond the two
  items above.

## Correctness and Reliability

- Finding 1 (undo non-functional) and Finding 3 (unbounded preview DML) above
  are the two reliability-relevant findings; see those sections for detail.
- A potential, unconfirmed risk noted for awareness rather than tracked as a
  work item: `schema/reflection.py`'s `_METADATA_LOCKS` caches one
  `asyncio.Lock()` per connection for the process lifetime with no reset path,
  unlike `InProcessConcurrencyLimiter.clear()` (which exists specifically
  because `asyncio` primitives bind to their creating event loop, and
  pytest-asyncio gives each test its own loop). Nothing in the current test
  suite triggers this, so it is not a confirmed defect — flagged only so a
  future contributor recognizes the pattern if the symptom (`RuntimeError:
  ... bound to a different event loop`) ever appears.

## Security

No new security-boundary defect was found. The invariants this repo treats as
non-negotiable were checked directly against the implementation, not just the
docs, and held:

- No inline `if dialect == ...` branching outside the registered
  `DialectAdapter`/`SessionDialectAdapter` classes (the one exception at the
  time of this review, `execution/service.py`'s then-Postgres-only
  cost-estimation hook, was the existing, documented, deliberate exception —
  not a new instance). *(Since resolved: item 26 phase 2 added MSSQL
  estimation and `_estimate_cost` became a per-dialect dispatch.)*
- No SQL-injection surface in session-guardrail string interpolation
  (`connections/dialects.py`'s `SET LOCAL`/`SET LOCK_TIMEOUT` calls
  interpolate only policy-validated integers, never caller input).
- No CORS/CSRF-relevant gap (bearer-token-only auth, no cookies) and no SSRF
  surface in secret resolvers or the JWKS URL (operator-configured only).
- Approval tokens (item 92) are HMAC-signed, fingerprint-bound to the entire
  query AST, short-lived, and fail-closed on any forged/expired/mismatched
  input; issuing one requires a distinct `query:approve` scope from execution,
  so self-approval isn't possible by construction.

This is a meaningful result on its own: the security/auth/catalog-governance
surface is where a defect would matter most for this product's positioning,
and direct inspection (not just trusting the existing adversarial suite)
didn't find one.

## Performance and Scalability

- Finding 3 (unbounded write-preview diff DML) is the one performance-relevant
  finding — see above.
- No N+1 reflection pattern, unbounded read result set, or missing-pagination
  issue was found in the areas reviewed; the read path's existing caps
  (row/join/where-depth limits, `max_diff_rows`, `max_batch_size`) are
  consistently enforced except where Finding 4 shows the write side lacking a
  batch-size analogue.

## Testing and Observability

- **A named write-execution test was failing on the working tree at the start
  of this review, now fixed** (Finding 1) — this was the most important
  testing-related result: it meant the compensation-store safety property the
  test exists to lock down was not being verified, and would not have been
  caught by a plain `pytest -m unit` run unless someone read the output
  carefully (the failure was present but easy to miss among 229 otherwise-
  passing tests). Fixed and re-verified during this review; `pytest -m unit`
  is now 230/230.
- **No metrics exist for the compensation-store/undo feature.** `metrics.py`
  instruments concurrency, quota, cost-estimation, and catalog-usage-signal
  buffering, but has no counter for compensation put/get/consume or undo
  success/failure — once Finding 1 is fixed, undo failures would still be
  invisible in Prometheus. Tracked as `TODO.md` item 113.
- Everything else inspected in this area — correlation-ID propagation from
  the REST middleware into audit events, `admin/anomaly.py` and
  `catalog/usage.py`'s test depth, the conftest event-loop-binding discipline
  for `in_process_limiter()`/`in_process_quota_limiter()` — held up well. Only
  one `skipif` exists in the whole suite (a sensible `helm`-not-installed
  guard); no `xfail`s were found.

## Product and Documentation Gaps

- **Two stale numbers in the security docs** (Medium severity, Confirmed,
  fixed directly as part of this review — no `TODO.md` item needed):
  `docs/SECURITY_POSTURE.md` claimed "31 threats (QG-01…QG-31)" while
  `docs/THREAT_MODEL.md` actually runs to QG-32 (added for item 92's approval
  gate and never back-propagated to the summary count); the same doc and
  `docs/PRODUCT_GUIDE.md` cited "~191"/"191-case" for the adversarial suite
  while `pytest -m security --collect-only` collects 259 tests today. Both
  corrected in this review's diff. Neither was misleading in the dangerous
  direction (both understated current coverage), but a security reviewer
  reading closely would have caught the QG-31-vs-QG-32 inconsistency.
- No other documentation drift was found worth flagging: the published
  security benchmark's "100% catch / 0% baseline" claim was re-run directly
  and reproduces exactly; the MSSQL nulls reject-don't-emulate Decision Log
  entry still matches `dialect_adapters.py`; `ENGINE_EXPRESSIVENESS_PLAN.md`'s
  item numbering and dependencies match `TODO.md` items 99–106 exactly;
  `README.md`'s "Current limitations" section is current.

## Prioritized Execution Plan

See `ROADMAP.md`'s new **"Technical and Product Improvement Plan"** section
for the phased, acceptance-criteria-bearing breakdown. In short: fix the
item-93 compensation-store regression first (it's a currently-broken,
previously-shipped feature, not new work), then items 107–109 (quota
double-spend, unbounded preview DML, write batch cap — all Small effort,
Medium/Medium-High severity), then the CI-scheduling and observability gaps
(112, 113), then the two maintainability items (110, 111).

## Validation Performed

- `poetry run pytest -m "unit and not real_db"` — initially **229 passed, 1
  failed** (`test_write_execution.py::test_compensation_store_ttl_and_single_use`,
  Finding 1); after fixing the three `write_execution.py` call sites and the
  test itself, re-ran to **230 passed, 0 failed**. Ran directly; not
  fabricated.
- `poetry run black --check src/ tests/` — clean, 234 files unchanged
  (one environment warning about Python 3.11 vs. black's 3.15 target-version
  safety check, not a formatting failure).
- `poetry run pytest -m security --collect-only` — 259 tests collected
  (confirms Finding/Doc gap above).
- Direct code reads (not just grep) of: `execution/service.py`,
  `execution/write_execution.py`, `execution/write_preview.py`,
  `execution/compensation.py`, `execution/concurrency.py`/`redis_concurrency.py`,
  `execution/quota.py`/`redis_quota.py`, `core/auth.py`, `jwt_auth.py`,
  `connections/models.py`, `catalog/governance.py`/`repository.py`,
  `query_ast/models.py`, `write_ast/models.py`, `compiler/dialect_adapters.py`,
  `mcp/tools/write.py`/`query.py`, `.github/workflows/ci.yml`, `Makefile`,
  `docs/SECURITY_POSTURE.md`, `docs/THREAT_MODEL.md`,
  `docs/benchmarks/SECURITY_BENCHMARK.md`.
- **Not run:** `make test-postgres-live`, `make test-load`, `make test-soak`,
  `make release-smoke` (all require a running Postgres/MSSQL via `docker
  compose up -d`, not started for this review to avoid touching local
  infrastructure state beyond what was already running). The specific claims
  those would validate (e.g. real-Postgres write+undo round-trip) are
  cross-checked instead by direct code reading and by the existing recorded
  test names/assertions in `TODO.md`'s item write-ups.
- `poetry run querygate-security-benchmark run --json` — ran successfully;
  output matched `docs/benchmarks/SECURITY_BENCHMARK.md`'s published numbers
  exactly (14/14 catch, 0/14 baseline, 2 documented residual cases).

## Documentation Updated

- **`TODO.md`** — added a review-finding note inline under item 93's Phase 3b
  (the compensation-store async/await regression and the record-eviction
  leak, both directly tied to that already-tracked feature); added new items
  107–113 for the remaining, previously-untracked findings.
- **`ROADMAP.md`** — added the mandatory "Technical and Product Improvement
  Plan" section (four phases, acceptance criteria per item); updated the
  existing item-93 Phase 3b checklist with the two new blocking sub-items.
- **`CLAUDE.md`** — added one durable testing-gotcha entry describing the
  "converting a store to `async def` without updating call sites" failure
  pattern this review caught, since the same Redis-migration pattern
  (`concurrency.py`/`redis_concurrency.py`, `quota.py`/`redis_quota.py`,
  now `compensation.py`) is likely to recur for future stores.
- **`docs/SECURITY_POSTURE.md`, `docs/PRODUCT_GUIDE.md`** — corrected the
  stale QG-31→QG-32 threat count and the stale ~191→259 adversarial-test
  count (small, unambiguous factual corrections).
- **`TECHNICAL_REVIEW.md`** — this document, created new.

## Open Questions

- Is the uncommitted `execution/compensation.py` diff intended to ship as
  part of the next commit, or was it mid-flight exploratory work the
  maintainer wants reverted instead of finished? This determines whether
  Finding 1's fix is "complete the conversion" (await the call sites) or
  "revert to the synchronous interface" — both are valid resolutions; only
  the maintainer knows which direction was intended.
- What cadence is acceptable for the new scheduled CI workflow (Finding 5) —
  nightly vs. weekly — given any CI cost/budget constraints not visible from
  the repository alone?
- Does the write-batch size cap (Finding 4) need a *different* default from
  the read path's `max_batch_size`, given writes are inherently more
  expensive per statement, or should it mirror the read policy's default
  exactly? This is a product/policy-defaults decision, not purely technical.
