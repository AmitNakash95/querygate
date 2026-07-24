# ROADMAP.md — the order to tackle TODO.md items

This file is the **execution order** for QueryGate development. `TODO.md` says
*what* each item is and whether it shipped; **this file says what order to do
them in, and why that order maximizes product growth and ROI.**

## How this file relates to TODO.md (read before editing either)

- **`TODO.md` is the authority for item content and done-status.** An item is
  "done" iff its `###` heading in `TODO.md` ends in exactly `✅ DONE` (no
  trailing qualifier like `(phase 1)`). Never track completion anywhere else.
- **This file is the authority for order only.** It never restates an item's
  body — it lists the item number, a one-line ROI rationale, and its position.
- The `[ ]` / `[x]` checkboxes below are a **convenience mirror** of TODO.md's
  `✅ DONE`, not a second source of truth. If they ever disagree, TODO.md wins;
  reconcile the checkbox to it.
- Item numbers are permanent and file-global (see `CLAUDE.md`). This file only
  references them; it never renumbers.

## The strategy this order encodes

**This order serves the North Star (`docs/business/NORTH_STAR.md`).** Every item
here advances one of QueryGate's three pillars — **Structural** (no raw SQL/DML +
query-shape policy), **Reach** (live operational DB, self-hosted, data never
leaves), **Proof** (per-human attribution + tamper-evident audit) — or closes a
table-stakes gap (delegated identity — item 90, now shipped; governed writes —
item 93), or moves the one success metric closer: **a paid design partner passing a security review
no competitor's passes.** A re-sequence must cite which pillar or gap it serves.
An item that advances none of them is a signal to question the item, not the
order.

The 2026-07-22 competitive scan (`docs/business/MARKET_DOMINATION_ANALYSIS.md`,
`docs/business/COMPETITORS.md`) reached one conclusion that drives the whole
sequence: **QueryGate's white space is real, but the thing that converts
"promising architecture" into "must-buy" is proof, not more features — and the
gate on everything is landing one paid design partner.**

So the order is not "ascending item number" and not "biggest feature first."
It is:

1. **Build the moat you demo** (delegated-identity attribution + tamper-evident
   receipts + a published safety benchmark) — the combination no competitor,
   including Cube, occupies.
2. **Make it deployable and trustable** enough for one design partner (signed
   delivery, a real Helm path, hardening).
3. **Unlock enterprise procurement** only when a partner's security team pulls
   for it (compliance mapping, third-party audit, disclosure program).
4. **Deepen governance/safety** (approval gates, config-change safety).
5. **Grow adoption and breadth** once product-market fit is proven (SDKs, DX,
   dialect breadth via the adapter architecture).
6. **Catalog/observability depth** last — lowest marginal ROI per unit effort.

Decision-gated items (governed writes, NL→StructuredQuery, the open AST
standard) are **excluded from the automated order** — they need an explicit
maintainer product decision first and are listed separately at the end.

---

## The ordered roadmap

Work top-to-bottom. Within a phase, order is also intentional. An item is
eligible only when its TODO.md "Depends on" (if any) is satisfied.

### Phase 0 — Moat & proof (highest ROI: wins the security review)

- [x] **90** — Delegated agent identity into policy + dual-identity audit (F1).
  ✅ **Shipped** (RFC 8693 actor→policy, dual-identity audit, MCP OAuth
  resource-server conformance RFC 9728/8707/6750). The attribution half of the
  Proof pillar is now realized — advertise it; complete the pillar with item 91.*
- [x] **91** — Tamper-evident hash-chained audit ledger + per-query receipts
  (F5). ✅ **Shipped** (`AUDIT_SINK_BACKEND=jsonl_chained`, SHA-256/HMAC chain,
  `querygate-audit verify`/`receipt`). *Pairs with 90 to produce the "prove
  exactly what every agent did, on whose behalf, under which policy" artifact —
  the literal buying question for the fintech/healthcare ICP. The Proof pillar
  is now realized end-to-end.*
- [ ] **58** — Published adversarial benchmark vs. raw-SQL agent / Google
  Toolbox. *The proof artifact that ends "why not Cube/Toolbox?" with evidence
  instead of assertion. High trust-per-effort.* **Phase 1 shipped** (offline
  reproducible corpus + `querygate-security-benchmark` CLI + published report,
  100% catch vs. 0% modeled raw-SQL baseline); box stays `[ ]` until phase 2's
  live LLM/Toolbox run (needs external infra).

### Phase 1 — Pilot-readiness (let one design partner deploy & trust it)

- [ ] **30 + 89 (phase 2)** — Distribution, SBOM, and **signed release
  artifacts** (Sigstore/cosign + SLSA provenance). *One workstream — do signed
  delivery once. The one real external-attestation gap the trust
  self-assessment surfaces; cheap credibility for any security review.*
  **Mechanism shipped** (`release.yml`: GHCR push + cosign keyless + SLSA
  build-provenance attestation, both digest-bound; `make verify-release` +
  consumer `cosign verify`/`gh attestation verify` docs). Box stays `[ ]` until
  the two maintainer-gated bits — the *first* executed signed release (a
  deliberate tag push) and a chosen Python package-index — are done.
- [x] **56** — HA / multi-region reference deployment + DR runbook (start with a
  supported Helm path). *A pilot has to actually deploy; unblocks the
  "deployed in a day" pilot success criterion.* ✅ **Shipped** (zero-downtime
  rolling config reload, `values-ha.yaml` multi-zone overlay, `deploy/HA_DR.md`
  shared-state matrix + DR runbook, chart HA invariants CI-asserted; live
  failover drill is the operator's step).
- [x] **36** — Production-grade QA / edge-case test suite. *Hardening before a
  real customer's data and adversaries touch it; raises confidence for the
  pilot without new surface.* ✅ **Shipped** (phase 1 policy-cap boundary +
  compiler fuzzing, phase 2a malformed-input fuzzing, phase 2b cross-dialect
  differential *execution* — same AST run against live Postgres + MSSQL, rows
  asserted equal).
- [x] **96** — Unify the AST reference-walk into a single canonical visitor.
  *Pure refactor, no behavior change: collapses the four hand-maintained
  reference walks into one authority so a policy/schema hole can't open in a
  forgotten copy. Robustness the pilot benefits from now, and the enabler that
  makes future AST breadth safe-by-construction. **Blocks 97.*** ✅ **Shipped**
  (`iter_column_refs` canonical visitor + `RefPosition` taxonomy; four parallel
  walks removed; zero behavior change, 1402 tests pass).
- [x] **95** — Discoverable scope catalog + recommended role bundles for IdP
  integration. *Turns the shipped "bring your IdP" auth (items 10/90) into a
  turnkey wire-up: a design partner with Okta/Entra/Auth0 can register QG's
  scopes and roles without reverse-engineering `scopes.py`. Small effort,
  directly unblocks SSO-based pilot onboarding. **Depends on 10 + 90 (both
  shipped).*** ✅ **Shipped** (full-vocabulary RFC 9728 `scopes_supported` +
  generated `docs/SCOPE_CATALOG.md` with role bundles, drift-tested).

### Phase 2 — Enterprise procurement unlocks (pull-driven — do when a partner's security team engages)

- [x] **54** — Compliance control mapping (SOC 2 / ISO 27001 readiness).
  *Procurement checkbox; pairs with the audit trail from 91.* ✅ **Shipped**
  (`docs/COMPLIANCE_MAPPING.md`: SOC 2 CC1–CC9 + C/A/PI + ISO 27001 Annex A
  mapped to real artifacts, honest product/shared/org split; audit engagement =
  item 53).
- [ ] **53** — Independent third-party security audit + published report.
  *External validation enterprise buyers ask for. Needs vendor coordination —
  see "Coordination-gated" note below.*
- [x] **60** — Bug bounty / responsible disclosure program. *Cheap, durable
  trust signal; stand up after 53 clears the obvious issues.* ✅ **Shipped**
  (coordinated-disclosure program in `SECURITY.md`: recognition-only structure +
  shared remediation flow; paid-bounty tier deliberately deferred to post-item-53).

### Phase 3 — Governance & safety depth (deepen the moat)

- [x] **26** — Query-cost estimation before execution. *Underpins 92's
  cost-gating and dollar budgets; broadens an existing guardrail.* ✅ **Shipped**
  (phase 1 Postgres `EXPLAIN`; phase 2 MSSQL `SET SHOWPLAN_XML ON` on a dedicated
  connection — the cost gate now enforces on both dialects, proven on live MSSQL).
- [x] **92** — In-query human-in-the-loop approval for sensitive/expensive reads
  (F3). *Gates the exfiltration leg of the lethal trifecta on what a read would
  actually touch — nobody else can, because none knows before running it.* ✅
  **Shipped fully:** both triggers (cost/row-estimate + catalog sensitivity-label)
  + stateless HMAC approval-token grant + `query:approve` scope + REST 428/approve
  flow + per-query batch tokens + the opt-in MCP `Context.elicit` in-session
  approval channel. Opt-in and off by default throughout.
- [x] **42** — Four-eyes config approval and separation of duties. *Governance
  maturity for the config plane.* ✅ **Shipped** (server-side enforcement +
  `admin:config:approve` scope + REST approve/reject + `querygate-config` CLI +
  admin-UI review affordance).
- [x] **39** — Draft-aware policy simulation before staging. *Safer config
  changes; reduces misconfiguration risk in a security product.* ✅ **Shipped**
  (`/admin/config/simulate` — isolated, non-persisting candidate evaluation;
  reconciled from a shipped-but-unmarked state).
- [x] **40** — Semantic access diff for config changes. *Makes a policy change's
  effect legible before it ships.* ✅ **Shipped** (ph1 connection-baseline diff;
  ph2 per-principal is covered by item 41's blast-radius — maintainer decision
  2026-07-23, no new code).
- [x] **41** — Policy-change blast-radius analysis. *Completes the config-change
  safety trio (39/40/41).* ✅ **Shipped** (ph1 ranked aggregation + ph2 paginated
  per-principal evaluation via a `principal_offset`/`next_principal_offset` cursor).

### Phase 4 — Adoption & breadth (grow once PMF is proven)

- [ ] **51** — Typed client-side query-builder SDK (Python + TypeScript).
  *Lowers integration friction for the next wave of adopters.*
- [ ] **35** — Agent-visible capacity waiting, progress, and cancellation.
  *Developer-experience polish for real agent workloads.* **Phase 1 + Phase 2
  shipped** (admission info + queue modes; Redis cross-replica admission state +
  queue-depth caps). Box stays `[ ]` for **Phase 3, which is design-gated** by
  the item's own text — MCP progress-notification wire format, a REST async
  lifecycle (`202` + status/cancel), cancellation semantics (queue-only vs.
  dialect DB-cancel), and a `429`/`Retry-After` breaking-change evaluation each
  need a protocol/product decision before build.
- [ ] **94** — Verify (and, if warranted, enable) prepared-statement plan reuse
  for template execution. *Cheap, bounded perf/observability check on the
  already-shipped template path (item 48); placed here 2026-07-24 because it is
  adoption polish, not a moat or safety item. May close as "verified, no change
  warranted".* **Depends on 48.**
- [ ] **18** — Stored-procedure catalog. *Extends read coverage where customers
  already encapsulate logic in procs.*
- [x] **57** — Pluggable dialect-adapter architecture. *The enabler that turns
  each new store into an adapter (not a project) — do before 19.* ✅ **Shipped**
  (sync compiler `DialectAdapter` [item 73] + new async `SessionDialectAdapter`;
  reverses the prior inline-branching exception. Adding a dialect = implement
  both + register).
- [ ] **97** — Bounded nested subqueries (uncorrelated, single-connection,
  depth-capped). *AST expressiveness: serves the "scope a set then filter from
  it" shape as a validated node, not a raw-SQL string. Minimal-safe subset only
  (reject correlated / cross-connection / over-depth); caps summed tree-wide.
  **Depends on 96; requires a PRODUCT_GUIDE Decision Log entry before build.***
  **Phase 1 shipped** (`IN (subquery)`/`NOT IN`, tree-wide caps, full adversarial
  + e2e coverage, Decision Log recorded); box stays `[ ]` until phase 2
  (`FROM (subquery)` derived table).

#### ★ Flagship pillar — Expressive Query Engine (items 99–106)

*One coordinated initiative deepening the **Structural** pillar: take the READ
query engine to 10/10 expressiveness for a fluent SQL author with no safety
regression (the "no raw SQL, ever" bet only wins if the AST rarely walls off a
real SQL author). **Deep spec + tests + acceptance:
[docs/ENGINE_EXPRESSIVENESS_PLAN.md](docs/ENGINE_EXPRESSIVENESS_PLAN.md).** Build
in the listed order; each item's Definition of Done and the canonical regression
bar are in the plan (§3, §5). Cross-cutting rule: every new node is visited by the
item-96 canonical walker and capped summed tree-wide (item 97), or it is not done.*

- [x] **99** — `HAVING` as `WhereNode` + searched `CASE` condition. *Cheap,
  low-risk warm-up that proves the visitor/cap-expansion pattern. Depends on 96.*
  ✅ **Shipped** (both positions reuse `_compile_where` + the item-96 visitor;
  `max_where_depth`/`max_where_predicates` extended to HAVING and CASE-condition
  trees + a new tree-wide case-condition predicate budget; no new policy field, no
  dialect code. Breaking wire change: `"having": [{…}]` → `"having": {…}`).
- [ ] **100** — ★ Bounded scalar `Expression` substrate (arithmetic, conditional
  aggregation, nested fns, expression-CASE). *The centerpiece — one closed,
  depth-capped node unlocks the most walls at once. Depends on 96, 99; **requires
  a Decision Log entry (non-goal #7 boundary + division) before build.***
- [ ] **101** — ★ General window functions (`WindowSelectItem`: OVER, LAG/LEAD,
  frames). *Second expressiveness pillar; running totals / moving averages.
  Depends on 96 (100 for windowed exprs); **Decision Log entry (frames) before
  build.***
- [ ] **102** — `EXTRACT`/date_part + relative-date/interval helpers. *High
  everyday agent value. Depends on 100; **Decision Log entry (interval cap + TZ).***
- [ ] **103** — Non-equi/range joins + FULL OUTER / CROSS. *Range/temporal joins.
  Depends on 96, 99; **Decision Log entry (CROSS gating).***
- [ ] **104** — Set operations (UNION / INTERSECT / EXCEPT). *New scope container;
  caps summed across arms. Depends on 96, 97; **Decision Log entry before build.***
- [ ] **105** — CTE / derived table in FROM (non-recursive; recursive OUT of
  scope). *Multi-stage single-statement analysis. Depends on 96, 97, 104;
  **Decision Log entry before build.***
- [ ] **106** — Correlated / EXISTS / scalar subqueries. *Do last — largest safety
  surface (breaks the uncorrelated assumption). Depends on 96, 97, 105; **Decision
  Log entry (correlation scope model) before build.***
- [ ] **19** — Additional dialects (MySQL, Snowflake, BigQuery, …). *Removes the
  "QueryGate is narrow" objection. **Depends on 57.***

### Phase 5 — Catalog & observability depth (lowest marginal ROI — opportunistic)

- [x] **37** — Automated end-to-end proof of adaptive semantic learning. ✅
  **Shipped** (`catalog/adaptive_learning_benchmark.py` drives the real 32C
  learning lifecycle e2e; reconciled from a shipped-but-unmarked state).
- [ ] **38 (phase 2)** — Admin UI catalog-governance workspace.
- [ ] **44 (phase 2)** — Admin observability / rejection-trend dashboard.
- [ ] **45 (phase 2)** — Non-admin "My access" portal.
- [ ] **47 (phase 2)** — Safe draft recovery + config export/import UX.
- [x] **50 (phase 2)** — Per-principal rate limits / query quotas over time. ✅
  **Shipped** (`RedisQuotaLimiter` — cross-replica shared quota budget via Lua,
  closing the per-replica-multiplication gap).

---

## Decision-gated — NOT in the automated order

These require an explicit maintainer product decision before any
implementation (per `CLAUDE.md`). The continuation skill must **skip** them and
surface them for a human, never auto-start them.

- **P1 · Governed Writes — item 93** (`StructuredWrite` contract). The flagship
  structural expansion; crosses the read-only line. **Decision made (2026-07-23,
  maintainer-approved) and Phase 1 SHIPPED** — contract + dry-run preview,
  execution **disabled** (Decision Log recorded in `docs/PRODUCT_GUIDE.md`).
  - **Phase 1** ✅ **shipped** — `write_ast/` + `WritePolicy` + write policy/schema
    validation + write compiler + the dry-run preview + `POST /write/preview`.
    Ships on its own as a "dry-run planner"; no execution path exists.
  - **Phase 2a** ✅ **shipped (maintainer-approved 2026-07-23)** — gated
    single-statement INSERT/UPDATE/DELETE *execution*: one transaction, in-txn
    row cap, item-92 approval gate on the row count, dual-identity (90) +
    tamper-evident (91) audit, no raw DML, deny-by-default. `WriteExecutionService`
    + `POST /write/execute` + `POST /write/approve`.
  - **Phase 2b** (largely shipped) — ✅ row-level old→new **diff preview**
    (`include_diff`, bounded + masking-aware, DML rolled back), ✅ the adversarial
    **write security suite** (`make test-security`), ✅ clean typed **4xx** for
    constraint/type violations, and ✅ the **MCP `run_structured_writes` tool**
    (preview/execute, batch, elicitation approval), ✅ **real-Postgres** write
    execution coverage, and ✅ the **write concurrency load gate** (`-m load`:
    cap holds under contention, concurrent inserts commit exactly). Still open:
    only the `release-smoke` write round-trip.
  - **Phase 3a/3b — reversibility (undo): REMOVED (2026-07-23).** The compensation
    store, `POST /write/undo`, the MCP `undo_structured_write` tool, the
    `WritePolicy.compensation_*` fields, and the `compensation_id` result field
    were deleted — reversibility is no longer a QueryGate capability. Rationale
    (see `docs/PRODUCT_GUIDE.md` Decision Log + TODO.md item 93): undo forced a
    second copy of real row values outside the customer's DB — against the
    least-privilege / data-never-leaves North Star — with unresolved
    correctness/durability risk and low marginal value once preview + approval
    exist. The **governance tier is kept and shipped**: preview + diff, gated
    execution, approval, dual-identity + tamper-evident audit, deny-by-default, the
    row cap, ✅ **upserts** (Postgres/SQLite native, MSSQL reject-not-emulate), ✅
    **multi-statement atomic batch**, ✅ **MSSQL write-execution parity**, and the
    `release-smoke` write round-trip. `approval-binds-to-diff-hash` remains a
    reasoned deferral (the token binds the full write fingerprint; the trigger
    re-evaluates at execute).
- **F4 · Safe NL→StructuredQuery.** Needs a decision on model provider/posture;
  must be an isolated opt-in subsystem, never wired into the catalog/32C.
- **P2 · Open the StructuredQuery AST as a standard.** A standards-governance
  commitment, not just engineering.

## Coordination-gated (partly non-code — an agent can prep, not finish)

- **53** (third-party audit) needs an external vendor engagement.
- **60** (bug bounty) is process/policy.
- **54** (compliance mapping) is largely documentation mapped to real controls.

An agent may do the code/doc-preparable parts and clearly flag what needs a
human/vendor to complete.

---

## Frontier status (updated 2026-07-23, fourth pass) — one buildable UI slice left; everything else gated

Successive batches shipped everything buildable without a new maintainer
decision or external resource. **Done across the cycle:** 56, 96, 95, 54, 60,
**92 (full — triggers + REST token flow + batch tokens + MCP `Context.elicit`
in-session approval, maintainer-approved)**, 42 (full); reconciled 39, 40
(covered by 41), 37; and — after explicit maintainer approval — **97 ph1**
(`IN (subquery)`), **57** (session dialect adapter, reversing the prior
inline-branching decision), **93 ph1** (governed-writes dry-run preview,
execution disabled), **41 ph2** (stateless paginated blast-radius), and
**50 ph2** (`RedisQuotaLimiter` — cross-replica shared quota). The remaining
frontier is gated, with **one** live buildable thread:

- **Buildable without a decision (low ROI):** **38 ph2** — admin-UI *bulk*
  approve/reject/delete + export/import + browser-triggered generate/learn +
  `review_history` view, all over item 32B's existing scoped routes (no new
  mutation path). Frontend-only; validated by `node --check` + static-markup
  assertions (no browser automation here), Phase 5 lowest-marginal-ROI.
- **Buildable without a decision (high ROI):** the **Technical-Review Phase 1
  fixes — 107, 108, 109** (quota double-reserve on an approval retry; the
  preview diff running DML before the over-cap check; the missing MCP write
  batch-size cap). Narrow, local, correctness/DoS-relevant, no external
  dependency. **Item 99 shipped 2026-07-24** — it was the one flagship-engine
  item needing no Decision Log entry; **100–106 each require one before build**,
  so the engine pillar is decision-gated again from here.

Everything else stays gated as before:

- **93 ph2** (gated write *execution*) — depends on 90+91+92 (all shipped) but
  crosses from preview to *committing writes*: a deliberate build + product
  decision, its own PR.
- **58 ph2 / 53** — *external infra/vendor*: an external LLM/Toolbox benchmark
  harness, an external auditor. (**26 ph2** — MSSQL `SHOWPLAN_XML` cost
  estimation — and **36 ph2b** — the Postgres+MSSQL cross-dialect differential
  suite — have since shipped against live CI databases; both items are now
  fully `✅ DONE` in TODO.md.)
- **30·89 ph2** — the *maintainer's signed-release tag push* (+ package-index
  choice); the mechanism is shipped.
- **35 ph3** — *design-gated* (agent-visible progress/cancellation posture);
  phases 1+2 shipped.
- **18 / 51 ph2 / 19** — *large standalone*: stored-proc subsystem (needs a real
  security review — procedures have side effects); the TypeScript SDK +
  standalone distribution (coupled to 30 ph2, not locally validatable); new
  dialects (need live DBs, now unblocked *architecturally* by 57).
- **44·45·47 ph2** — *admin-UI / durable-infra phase-2s* needing external metrics
  history (44), more UI (45), or an encrypted-at-rest draft store (47).
- **F4 / P2** — *decision-gated*: NL→StructuredQuery (model-provider/posture
  decision) and opening the AST as a standard (governance commitment).

`roadmap-next` should surface the two live threads (build 38 ph2 if the
maintainer wants the low-ROI UI slice; get a decision on 92's elicitation SoD
posture) rather than force a gated/entangled change. Trim this note as the
maintainer re-prioritizes and the frontier moves.

---

## Technical and Product Improvement Plan (2026-07-23 review)

A full repo-wide due-diligence pass (`TECHNICAL_REVIEW.md`) found this codebase
to be unusually mature and self-consistent for its stage — the non-negotiables
hold, the security/auth/catalog boundary checked out clean, and doc claims are
almost entirely backed by real code and tests. It surfaced one live regression
in an in-flight change plus a handful of narrow, real gaps, tracked as `TODO.md`
items 107–113 (the item-93 compensation-store regression is tracked inline in
item 93's own Phase 3b note, since it's the same feature, not a new item). This
section sequences that work; it supplements, not replaces, the phase ordering
above — none of these items change the Phase 0–5 execution order for the
already-planned initiatives.

### Review Phase 1 — Correctness, security, and production risk

- [x] **Item 93 (Phase 3b) regression, part 1** ✅ — fixed the
  `CompensationStore` async/await mismatch (`execution/write_execution.py`
  now awaits the `async` `get`/`put`/`consume`; the affected unit test was
  converted to `async def` to match). `pytest -m unit` passes 230/230 with no
  coroutine-never-awaited warnings.
- [x] **Item 93 (Phase 3b) regression, part 2 — OBSOLETE (2026-07-23).** The
  consumed-record eviction leak is moot: the compensation store was removed
  entirely along with the undo mechanism (see item 93). No code remains to leak.
- [ ] **107** — Batch query execution double-reserves quota on an approval
  retry.
  - Why: an MCP batch item that needs interactive approval consumes two
    quota units for one logical query, silently halving effective throughput
    for approval-gated callers.
  - Scope: `execution/service.py` (`_execute_batch_item`, `enforce_query_quota`).
  - Acceptance criteria:
    - A test asserts exactly one quota unit is consumed across an
      approval-required-then-resolved batch item.
- [x] **108** — Write-preview diff runs the full DML before the
  `max_affected_rows` cap is checked. ✅ **Shipped** (`_mutation_diff` takes a
  `within_cap` flag and falls back to the existing Python-applied-SET path, so an
  over-cap preview issues no DML but still returns a bounded diff; DELETE was
  never affected. Proven by hooking `before_cursor_execute` and asserting on the
  statements issued, with a within-cap positive control so the guard can't
  degrade into disabling the feature).
- [x] **109** — MCP `run_structured_writes` has no batch-size cap (the read
  path's `validate_batch_size` has no write-side equivalent). ✅ **Shipped**
  (`WritePolicy.max_batch_size`, default 10 to match the read cap and drift-tested
  against it, + `validate_write_batch_size` enforced at the MCP tool before the
  preview/execute branch *and* inside `execute_many`, so no statement is touched
  in an over-size batch and the service layer is bounded regardless of transport).

### Review Phase 2 — Reliability and workflow hardening

- [ ] **112** — Add a scheduled (cron) CI workflow for dependency/security
  scans and wire `make test-soak` into it.
  - Why: `.github/workflows/ci.yml` only triggers on `push`/`pull_request` —
    a CVE disclosed against an already-merged dependency isn't caught until
    the next incidental change, and the heavier 100-round soak test never
    runs automatically (only the 5-round `test-load` does).
  - Scope: `.github/workflows/`, `docs/RELEASING.md`.
  - Acceptance criteria:
    - A nightly/weekly workflow runs the CVE/SBOM/lockfile checks and
      `make test-soak` against `main` independent of code changes.
- [x] **113 — OBSOLETE (2026-07-23).** Metrics for the write-undo/compensation
  store are moot: the undo/compensation feature was removed (see item 93).
  Write-*execution* metrics, if wanted later, would be a fresh, separately-scoped
  item.

### Review Phase 3 — Architecture and maintainability

- [ ] **110** — Explicitly reject `value_subquery` in a write's WHERE at the
  write-validation layer instead of relying on the compiler's `ctx=None`
  default to fail it.
  - Why: not currently exploitable, but it fails at the wrong layer with a
    compiler-internal error, and is a latent trap for a future write-compiler
    change.
  - Scope: `validation/write_policy_validation.py`.
  - Acceptance criteria: a `value_subquery` in a write WHERE raises a clean
    `QueryValidationError` at validation time; regression test added.
- [ ] **111** — Consolidate the four hand-rolled WHERE-predicate tree walks
  (`policy_validation.py`, `schema_validation.py`, `write_policy_validation.py`,
  `write_schema_validation.py`) into one shared helper, mirroring how item 96
  centralized column-ref walking into `iter_column_refs`.
  - Why: all four are correct today but could silently drift the next time
    `WhereNode` grows a new combinator — the exact class of bug item 96 was
    built to prevent for column refs.
  - Scope: the four validator modules.
  - Acceptance criteria: one shared predicate-iterator helper; all four
    validators' existing test suites pass unchanged (no behavior change).

### Review Phase 4 — Performance, observability, and developer experience

- [x] **Stale security-posture numbers** — `docs/SECURITY_POSTURE.md` claimed
  "31 threats (QG-01…QG-31)" and "~191" adversarial tests; `docs/THREAT_MODEL.md`
  actually runs to QG-32 and `pytest -m security --collect-only` collects 259.
  Corrected directly in `docs/SECURITY_POSTURE.md` and `docs/PRODUCT_GUIDE.md`
  as part of this review (no TODO item needed — already fixed).
- Item 113 (metrics) and item 112 (scheduled CI) above are also this phase's
  content; not repeated here.

**Not turned into tracked items** (reviewed and deliberately left as
observations in `TECHNICAL_REVIEW.md`, not work items): a potential
`asyncio.Lock` event-loop-binding risk in `schema/reflection.py`'s
`_METADATA_LOCKS` cache (no reproduction, no reset path exists but nothing
currently triggers it — flagged for awareness, matches the documented
`in_process_limiter().clear()` pattern if it's ever needed).

---

## How to pick the next item (the algorithm the `roadmap-next` skill follows)

1. Read this file and `TODO.md`. Confirm the git worktree is clean.
2. Walk the ordered roadmap **top-to-bottom**. For each item, the authority is
   its `TODO.md` `###` heading: if it ends in exactly `✅ DONE` (no phase
   qualifier), it is complete — reconcile this file's checkbox to `[x]` and
   continue.
3. The **next item** is the first one that is *not* fully `✅ DONE`, is *not* in
   the Decision-gated list, and whose TODO.md dependencies are satisfied. Skip
   (and report) any decision-gated item reached before it.
4. Announce: the last completed item (where the previous agent left off), the
   next item, why it's next, and any items skipped and why.
5. Implement it with the full `next-item` discipline (scope → production-grade
   impl → tests → docs → release gates → one clean commit → `ship-item`).
6. On completion, tick this file's checkbox for that item in the same commit.

## Maintenance rules

- **Re-sequencing is allowed and expected** as the market and pilots teach us —
  but a re-order must be a deliberate edit with a one-line reason, not a drift.
  The competitive-scan and next-item workflows may propose re-ordering.
- When a *new* TODO item is added, place it in the correct phase here (or note
  it as unplaced) — a new item is not automatically last.
- Never move an item's *done-status* here without the matching `✅ DONE` in
  TODO.md. TODO.md leads; this file follows.
- Keep the rationale one line per item. Depth lives in TODO.md, not here.
