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
- [ ] **36** — Production-grade QA / edge-case test suite. *Hardening before a
  real customer's data and adversaries touch it; raises confidence for the
  pilot without new surface.*
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

- [ ] **26** — Query-cost estimation before execution (complete phase 2 / MSSQL;
  Postgres exists). *Underpins 92's cost-gating and dollar budgets; broadens
  an existing guardrail.*
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
    (preview/execute, batch, elicitation approval). Still open: `release-smoke`
    write round-trip, write concurrency load gate.
  - **Phase 3** (compensation/undo + upserts + batch + MSSQL parity) depends on
    Phase 2.
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

Everything else stays gated as before:

- **93 ph2** (gated write *execution*) — depends on 90+91+92 (all shipped) but
  crosses from preview to *committing writes*: a deliberate build + product
  decision, its own PR.
- **26 ph2 / 36 ph2b / 58 ph2 / 53** — *external infra/vendor*: live MSSQL,
  Postgres+MSSQL dual-DB CI, external LLM/Toolbox harness, an external auditor.
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
