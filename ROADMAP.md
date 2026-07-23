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
- [ ] **92** — In-query human-in-the-loop approval for sensitive/expensive reads
  (F3). *Gates the exfiltration leg of the lethal trifecta on what a read would
  actually touch — nobody else can, because none knows before running it.*
  **Shipped: both triggers** (cost/row-estimate + catalog sensitivity-label) +
  stateless HMAC approval-token grant + `query:approve` scope + REST 428/approve
  flow, opt-in; box stays `[ ]` until the MCP-elicitation approval channel + batch.
- [ ] **42** — Four-eyes config approval and separation of duties. *Governance
  maturity for the config plane.* **Phase 1 shipped** (server-side enforcement:
  durable approval records, author≠approver, `require_config_approvals` apply-gate,
  `admin:config:approve` scope + REST approve/reject); box stays `[ ]` until
  phase 2 (CLI + admin-UI review flows).
- [ ] **39** — Draft-aware policy simulation before staging. *Safer config
  changes; reduces misconfiguration risk in a security product.*
- [ ] **40** — Semantic access diff for config changes. *Makes a policy change's
  effect legible before it ships.*
- [ ] **41** — Policy-change blast-radius analysis. *Completes the config-change
  safety trio (39/40/41).*

### Phase 4 — Adoption & breadth (grow once PMF is proven)

- [ ] **51** — Typed client-side query-builder SDK (Python + TypeScript).
  *Lowers integration friction for the next wave of adopters.*
- [ ] **35** — Agent-visible capacity waiting, progress, and cancellation.
  *Developer-experience polish for real agent workloads.*
- [ ] **18** — Stored-procedure catalog. *Extends read coverage where customers
  already encapsulate logic in procs.*
- [ ] **57** — Pluggable dialect-adapter architecture. *The enabler that turns
  each new store into an adapter (not a project) — do before 19.*
- [ ] **97** — Bounded nested subqueries (uncorrelated, single-connection,
  depth-capped). *AST expressiveness: serves the "scope a set then filter from
  it" shape as a validated node, not a raw-SQL string. Minimal-safe subset only
  (reject correlated / cross-connection / over-depth); caps summed tree-wide.
  **Depends on 96; requires a PRODUCT_GUIDE Decision Log entry before build.***
- [ ] **19** — Additional dialects (MySQL, Snowflake, BigQuery, …). *Removes the
  "QueryGate is narrow" objection. **Depends on 57.***

### Phase 5 — Catalog & observability depth (lowest marginal ROI — opportunistic)

- [ ] **37** — Automated end-to-end proof of adaptive semantic learning.
- [ ] **38 (phase 2)** — Admin UI catalog-governance workspace.
- [ ] **44 (phase 2)** — Admin observability / rejection-trend dashboard.
- [ ] **45 (phase 2)** — Non-admin "My access" portal.
- [ ] **47 (phase 2)** — Safe draft recovery + config export/import UX.
- [ ] **50 (phase 2)** — Per-principal rate limits / query quotas over time.

---

## Decision-gated — NOT in the automated order

These require an explicit maintainer product decision before any
implementation (per `CLAUDE.md`). The continuation skill must **skip** them and
surface them for a human, never auto-start them.

- **P1 · Governed Writes — item 93** (`StructuredWrite` contract). The flagship
  structural expansion; crosses the read-only line, so it needs an explicit
  maintainer product decision (record it in `docs/PRODUCT_GUIDE.md`'s Decision
  Log) before any code. Fully specced and phased in TODO.md item 93. **Slotting
  once approved:**
  - **Phase 1** (contract + dry-run diff preview, execution **disabled**) is
    low-risk/zero-write-risk and depends on nothing beyond today's code — it can
    slot **immediately after Phase 0** (right after item 91) once the decision is
    made, and can even ship on its own as a "dry-run planner."
  - **Phase 2** (gated execution) **depends on items 90 + 91 + 92**, so it
    cannot precede the Phase-0 moat and the item-92 approval gate.
  - **Phase 3** (compensation/undo + upserts + batch + MSSQL parity) depends on
    Phase 2.
  Until the decision is made, `roadmap-next` must skip item 93 and surface it.
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
