# ROADMAP.md — the order to tackle TODO.md items

This file is the **execution order** for QueryGate development. `TODO.md` says
*what* each item is and whether it shipped; **this file says what order to do
them in, and why that order maximizes product growth and ROI.**

## How this file relates to TODO.md (read before editing either)

- **`TODO.md` is the authority for item content and done-status.** An item is
  "done" iff its `###` heading in `TODO.md` ends in exactly `✅ DONE` (no
  trailing qualifier like `(phase 1)`). Never track completion anywhere else.
- **This file is the authority for order and transient active claims only.** It
  never restates an item's body — it lists the item number, a one-line ROI
  rationale, its position, and (while work is active) its claim marker.
- The `[ ]` / `[x]` checkboxes below are a **convenience mirror** of TODO.md's
  `✅ DONE`, not a second source of truth. If they ever disagree, TODO.md wins;
  reconcile the checkbox to it.
- A line in the exact form
  ``🚧 **CLAIMED** — owner: `<agent/session-or-task-id>`; started:
  `<YYYY-MM-DDTHH:MMZ>` `` immediately below an item's checkbox means an agent
  is actively working on it. It is a coordination signal, not done-status.
  Other agents must leave that item alone; only the owner or maintainer clears
  the marker.
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
5. **Deepen the core engine** — take the structured query surface to 10/10
   expressiveness (the ★ flagship pillar, items 99–106). *Re-sequenced
   2026-07-25 (maintainer decision): the engine is the **Structural** pillar
   itself, not adoption polish — the "no raw SQL, ever" bet only holds if the
   AST rarely walls off a fluent SQL author, so engine depth outranks SDK/DX
   breadth. Previously buried inside Phase 4 behind items 51/35/94/18.*
6. **Grow adoption and breadth** once the engine is deep enough to adopt (SDKs,
   DX, dialect breadth via the adapter architecture).
7. **Catalog/observability depth** last — lowest marginal ROI per unit effort.

Decision-gated items (governed writes, NL→StructuredQuery, the open AST
standard) are **excluded from the automated order** — they need an explicit
maintainer product decision first and are listed separately at the end.

---

## The ordered roadmap

Work top-to-bottom. Within a phase, order is also intentional. An item is
eligible only when its TODO.md "Depends on" (if any) is satisfied.

Before changing implementation files, claim the selected item by adding the
`🚧 **CLAIMED**` line defined above and re-read the item to verify there is
still exactly one claim and it is yours. A claimed item is unavailable even if
an agent was explicitly told to work on that number. Continue to the next
independently eligible item, or stop if roadmap order/dependencies leave none.
Never steal or auto-expire a claim based on its timestamp. Remove your own
claim when the work ships or before explicitly handing the item back.

### Phase 0 — Moat & proof (highest ROI: wins the security review)

- [x] **90** — Delegated agent identity into policy + dual-identity audit (F1).
  ✅ **Shipped** (RFC 8693 actor→policy, dual-identity audit, MCP OAuth
  resource-server conformance RFC 9728/8707/6750). The attribution half of the
  Proof pillar; with item 91 also shipped, the pillar is realized end-to-end —
  advertise it.*
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
- [x] **127** — Reject an MCP request whose routing headers disagree with its
  body. *Added 2026-07-30 by `competitive-scan`. The MCP `2026-07-28` spec
  mandates this server-side check precisely because a gateway authorizing on
  `Mcp-Name` while the server executes the body is a confused deputy — and
  "QueryGate behind someone else's front door" (P4) is exactly that topology.
  Prospective, not a live bug (we speak `2025-11-25`), but it is a boundary
  bypass, it belongs in the adversarial suite, and it is deliberately
  independent of item 128 so it can ship now and fail closed the moment a
  gateway starts sending the headers.* **Depends on 86.**
- [x] **136** — The `jsonl_chained` audit backend silently disables four shipped
  read surfaces.
  *Pre-existing defect found 2026-07-30 by `auditors`. Opting
  into the tamper-evident posture item 91 shipped currently costs
  `/help/my-recent-denials`, the anomaly report, the change-trend report, and
  the admin UI audit browser. Three readers already unwrap the envelope and are
  refused at the gate; the fourth (`_audit_page`) has **no** unwrap, so a
  gate-only fix would turn it from honestly-disabled into silently-empty. Phase
  0 because it makes the stronger audit configuration worse than the weaker one
  — backwards for the security-review story.* **Depends on 91; blocks 134.**
- [ ] **137** — Audit read surfaces neither verify nor disclose hash-chain
  integrity. *Surfaced 2026-08-01 by the `security-invariant-reviewer` audit of
  item 136: the four surfaces item 136 made able to read the `jsonl_chained`
  ledger neither recompute the chain hash nor tell a caller which backend
  actually produced a `source="jsonl"` response. Needs a maintainer decision
  (disclosure-only vs. real per-request verification) recorded in the
  PRODUCT_GUIDE Decision Log as the item's own first step — same pattern as
  items 100–106 — not a reason to defer it.* **Depends on 91, 136.**
- [x] **138** — Audit read surfaces scan the entire persisted file on every
  request, unbounded by lines read. *Surfaced 2026-08-01 by the
  `security-invariant-reviewer` audit of item 136. Pre-existing for the
  default `jsonl` backend (item 136 only extended the same already-shipped
  behavior to `jsonl_chained`, which is that item's whole point) — filed
  separately because fixing it touches the default-backend read path in
  production today and deserves its own scoping/tests rather than riding in on
  a bug-fix commit.*
- [ ] **139** — Bound audit-line size at the source (AST list caps +
  `audit/sinks.py`'s own unbounded-read defect). *Surfaced 2026-08-01 by the
  `security-invariant-reviewer` audit of item 138. Needs a maintainer decision
  on the right AST list-size cap, recorded in the PRODUCT_GUIDE Decision Log
  as the item's own first step.* **Depends on 138.**
- [ ] **140** — `_audit_page` pagination can still materialize ~1M dicts per
  request. *Surfaced 2026-08-01 by the `security-invariant-reviewer` audit of
  item 138; pre-existing (the old deque had the identical bound), same class
  of defect item 138 exists to fix. Needs a maintainer decision on the
  cursor-ceiling/pagination-shape tradeoff, recorded as the item's own first
  step.* **Depends on 138.**
- [ ] **141** — Convert audit-reader line caps into practically-tight
  window-based early exits. *Surfaced 2026-08-01 by the
  `security-invariant-reviewer` audit of item 138; deliberately not built as
  part of it. Trades a bounded ordering-tolerance assumption for speed on an
  already-safe (fail-closed) bound — needs an explicit maintainer decision
  recorded in the PRODUCT_GUIDE Decision Log before building, per CLAUDE.md's
  working agreement on judgment calls.* **Depends on 138.**
- [x] **142** — `docs/THREAT_MODEL.md` uses the ID `QG-32` for two unrelated
  threats.
  *Surfaced 2026-08-01/02 by the `claim-reviewer`/
  `security-invariant-reviewer` audit of item 133; pre-existing, not
  introduced by that item. Mechanical rename + cross-reference sweep, XS
  effort — flagged separately rather than folded into item 133's diff since
  renumbering a threat-model ID other docs may reference is an identifier-
  stability change, not a drive-by.*
- [x] **143** — `cryptography` 49.0.0 has an unreviewed CVE
  (`PYSEC-2026-3552`), blocking `make release-check`'s SBOM/dep-audit step.
  *Surfaced 2026-08-02 while running the release gate for item 133; unrelated
  — no dependency file was touched. QueryGate's own code never calls the
  vulnerable `pkcs7_decrypt_*` functions, so this is likely a justified-
  allowlist case, but that's the `dep-audit` skill's call. Blocks the release
  gate for every future item until resolved.*
- [ ] **144** — `verdict()` emits no query metrics, and `/metrics` is
  unauthenticated. *Surfaced 2026-08-02 by the `security-invariant-reviewer`
  re-audit of item 133. verdict() shares execute()'s quota budget but leaves
  no metrics trail of its own; separately, /metrics being unauthenticated
  already exposes a policy-vs-schema rejection-reason label for existing
  traffic (pre-existing, unrelated). Needs a maintainer call on whether to
  gate /metrics, recorded before building.*

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
- [ ] **135** — Automatic (TTL/lease-driven) credential re-resolution, without
  an operator-triggered reload. *Added 2026-07-30 by `competitive-scan`; scope
  corrected the same day by `auditors` — an earlier draft wrongly claimed
  rotation requires a process restart. It does not: item 13 shipped
  reload-triggered re-resolution with in-flight-safe engine disposal, and
  README/PRODUCT_GUIDE/THREAT_MODEL document it correctly. The real gap is that
  the refresh is **operator-pull only** — no TTL, no lease awareness, no
  automatic trigger — so short-TTL dynamic credentials expire into failures
  between reloads. Add the trigger, not the plumbing.* **Depends on 13.**
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
  see "Coordination-gated" note below.* **Highest trust-per-effort item on the
  board:** it converts the central claim from self-asserted to
  third-party-attested, which is the objection that actually closes a security
  review.
- [ ] **134** — Compliance-grade (WORM) audit retention + managed search.
  *Added 2026-07-30 by `competitive-scan`; scope corrected the same day by
  `auditors`. Item 91's chain **does** detect in-ledger deletion; the residual
  is prevention, availability, and whole-file loss — retention is a separate
  control regulated buyers ask for by name. Note there is **no sink registry**
  today (`configure_audit_sink` is an `if`-chain), so this item owns building
  one, composing with the chain rather than replacing it, and getting a remote
  PUT off the request path.* **Depends on 91, 136.**
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

### Phase 4 — ★ Flagship pillar: Expressive Query Engine (deepen the Structural pillar)

*One coordinated initiative deepening the **Structural** pillar: take the READ
query engine to 10/10 expressiveness for a fluent SQL author with no safety
regression (the "no raw SQL, ever" bet only wins if the AST rarely walls off a
real SQL author). **Deep spec + tests + acceptance:
[docs/ENGINE_EXPRESSIVENESS_PLAN.md](docs/ENGINE_EXPRESSIVENESS_PLAN.md).** Build
in the listed order; each item's Definition of Done and the canonical regression
bar are in the plan (§3, §5). Cross-cutting rule: every new node is visited by the
item-96 canonical walker and capped summed tree-wide (item 97), or it is not done.*

**Promoted ahead of adoption/breadth on 2026-07-25 (maintainer decision):** the
engine *is* the Structural pillar, so its depth outranks SDK/DX/dialect breadth —
an agent that hits a wall routes around the gate, and the safety guarantee stops
mattering. Items **100–106 are each gated only on a recorded PRODUCT_GUIDE
Decision Log entry**, which is a maintainer paragraph, not external infra — that
gate is the *first step of the item*, not a reason to defer it.

- [x] **99** — `HAVING` as `WhereNode` + searched `CASE` condition. *Cheap,
  low-risk warm-up that proves the visitor/cap-expansion pattern. Depends on 96.*
  ✅ **Shipped** (both positions reuse `_compile_where` + the item-96 visitor;
  `max_where_depth`/`max_where_predicates` extended to HAVING and CASE-condition
  trees + a new tree-wide case-condition predicate budget; no new policy field, no
  dialect code. Breaking wire change: `"having": [{…}]` → `"having": {…}`).
- [x] **100** — ★ Bounded scalar `Expression` substrate (arithmetic, conditional
  aggregation, nested fns, expression-CASE). ✅ **Shipped** (one closed
  depth-capped union across projections/aggregate args/CASE results/both predicate
  sides; `max_expression_depth` + tree-wide `max_expression_nodes`; guarded
  division; visitor-verified at every depth. Regression bar 5/16 → **8/16**.)
  *The centerpiece — it unlocked the most walls at once, and items 101–106 now
  build on its `Expression` rather than adding parallel scalar shapes.*
- [x] **101** — ★ General window functions (`WindowSelectItem`: OVER, LAG/LEAD,
  frames). ✅ **Shipped** (all 13 window fns + `ROWS`/`RANGE` frames as a
  projection; `arg` reuses item 100's `Expression`; `max_window_specs` summed
  tree-wide + `max_window_frame_offset`; no synthesized default frame; rejected
  with `group_by`/aggregates and under `min_group_size`; every fn/frame executed
  on live Postgres **and** live MSSQL with rows compared. Regression bar 8/16 →
  **9/16**.) *Second expressiveness pillar — running totals and rank-in-place;
  §5 rows 3 and 15 were corrected to point at item 105 and a possible
  window-as-expression item rather than this one.*
- [x] **102** — `EXTRACT`/date_part + relative-date/interval helpers. ✅ **Shipped**
  (`extract`/`now`/`date_add` as three closed `Expression` members — each its own
  member because their non-scalar field is a keyword, and deliberately no
  `interval` member so the union stays all-scalar; `max_interval_days` computed
  with upper-bound unit lengths; `dayofweek`/`week` given one cross-dialect
  definition; every part and unit executed on live Postgres **and** live MSSQL).
  *Its larger outcome was correctness, not reach: building it surfaced that
  Postgres resolved `EXTRACT` **and the already-shipped `date_bucket`** against a
  session `TimeZone` QueryGate never set, so those answers followed server config.
  Sessions are now pinned to UTC — a deliberate behavior change, recorded in the
  Decision Log. Regression bar 9/16 → **10/16**.*
- [x] **103** — Non-equi/range joins + FULL OUTER / CROSS. ✅ **Shipped**
  (`JoinSpec.condition` is the same `WhereNode` as `where`, so the ON clause
  inherits every WHERE cap through the item-96 visitor and `iter_scope_expressions`
  rather than parallel checks; `full` + `cross` join types, with `cross`
  deny-by-default behind `Policy.allow_cross_join` — a flag rather than a cap
  *because* the pre-execution bounds are weaker than they look: LIMIT bounds rows
  returned, not work, and item 26's cost gate is opt-in and off by default, leaving
  `timeout_seconds` as the always-on bound; every join type executed on live
  Postgres **and** live MSSQL. Regression bar 10/16 → **11/16**.)
  *Its side finding: outer joins made the pre-existing PG-vs-MSSQL NULLS-ordering
  divergence reachable from a second join type — measured, recorded, and left
  standing, because the only fix is the `nulls` handling item 74 rejects.*
- [x] **104** — Set operations (UNION / INTERSECT / EXCEPT). ✅ **Shipped**
  (`StructuredQuery.set_op` with the carrying query as arm 1 — deliberately not
  the second top-level query type the plan sketched, because that would have made
  every pipeline signature a union whose failure mode is a consumer handling one
  member; every arm is a scope at the SAME depth, independently validated AND
  independently compiled so it carries its own mandatory filters and k-anon floor;
  new tree-wide `max_set_op_arms`; `INTERSECT ALL`/`EXCEPT ALL` rejected on MSSQL;
  17/17 enforcement points mutation-verified. Regression bar 11/16 -> **12/16**.)
  *Its side finding was a guardrail failure, not a feature gap: SQLAlchemy's MSSQL
  dialect silently DROPS `.limit()` on a compound SELECT, so `clamp_limit` would
  have been a no-op on one dialect only — the compound is now wrapped in a derived
  table before limiting. It also closed a pre-existing item-97 hole where the
  item-92 approval gate never saw a sensitive column inside an `IN (subquery)`.*
- [x] **97 (phase 2)** — `FROM (subquery)` derived table. ✅ **ABSORBED BY 105**
  (2026-07-27), which was the recorded intent of moving it here: 105 spells the
  derived table as a named `WITH` block, so it is implemented once rather than
  twice. Item 97 is now fully `✅ DONE`.
- [x] **105** — CTE / derived table in FROM (non-recursive; recursive OUT of
  scope). ✅ **Shipped** (an additive `StructuredQuery.ctes` list of named `WITH`
  blocks — deliberately NOT the union on `from`/`JoinSpec.table` the plan
  specified, because a union re-types two `str` fields read by ~a dozen consumers
  whose failure mode is silent, while an unrecognized block NAME reflects as a
  table and is rejected: the unaware consumer fails closed. Only the root declares
  blocks; a block may reference only an EARLIER one, which makes **recursive cte
  structurally inexpressible** rather than merely forbidden; new `max_cte_count`
  with `max_subquery_depth` charged along the reference chain; a block carries no
  `max_rows` clamp, since truncating intermediate work is a silently wrong total.
  23/23 enforcement points mutation-verified; live Postgres **and** live MSSQL.
  Regression bar 12/16 -> **14/16**.)
  *Its side finding was a crash on a shipped guardrail, not a feature gap: item
  118's k-anon fan-out check reached for `.primary_key.columns`, which only a
  `Table` has — so `min_group_size` plus any **aliased** join raised
  `AttributeError` rather than deciding. Reproducible with no cte at all (item 122).*
- [x] **106** — Correlated / EXISTS / scalar subqueries. ✅ **Shipped** — and with
  it **Phase 4 is complete**. (`EXISTS`/`NOT EXISTS` as a `Predicate` operator;
  scalar subqueries as a comparison RHS in WHERE and HAVING; correlation via a
  **declared, capped** `correlate` list checked against the ENCLOSING scope, so an
  undeclared outer ref still fails exactly as before and correlation is opt-in per
  subquery. A scalar subquery must be an aggregate with no `group_by`, making
  exactly-one-row true by construction rather than by a `LIMIT 1` that would pick an
  arbitrary row. New ops live on a read-only `ReadCompareOp` so the write AST's
  shared `CompareOp` is not widened — item 114's defect. Regression bar 14/16 ->
  **15/16**; 13/13 enforcement points mutation-verified.)
  *Its own build found the defect worth remembering: resolving a child's declared
  refs against the parent's already-correlated tables made correlation reach a
  GRANDPARENT, so "one level" held in name only until a test asked for it.*
- [x] **125** — ★ A window function as an `Expression` operand. ✅ **Shipped** — the
  regression bar is **16/16** and Phase 4's success criterion is met. *Phase 4's item
  list closed at bar **15/16**; this is the one red row (15) and the pillar's own
  success criterion is 100% green. Recorded as a wall by item 101 and **approved by
  the maintainer on 2026-07-27** — Decision Log entry in `docs/PRODUCT_GUIDE.md`
  precedes the build. Depends on 100, 101.*

### Phase 5 — Adoption & breadth (grow once the engine is deep enough to adopt)

*Demoted below the engine on 2026-07-25 — see the Phase 4 note. Nothing here is
wrong; it is all downstream of having a surface worth integrating against.*

**2026-07-29 maintainer decision (via `roadmap-next`):** item 19 (additional
dialects) is deliberately deprioritized to the very end of the automated
order, alongside item 18 (stored-procedure catalog, already decision-gated).
Reason: 19 is M–XL and needs new infrastructure decisions (a new driver
dependency, a docker-compose live-DB service, CI wiring) that shouldn't be
made silently mid-walk; the maintainer chose to clear the smaller,
locally-buildable Phase 6 phase-2 slices (44/45/47) first. The walk should
treat 19 as coming after every other eligible item, not in its listed
position.

- [ ] **51** — Typed client-side query-builder SDK (Python + TypeScript).
  *Lowers integration friction for the next wave of adopters.* **Phase 1
  shipped** (in-tree Python builder); **phase 2a shipped** (in-tree TypeScript
  builder, `clients/typescript/`, a structural/behavioral mirror (camelCase
  naming, not name-for-name) + cross-language kitchen-sink parity fixture,
  both wired into CI — verified end-to-end during development against a real
  running server and real Postgres). Box stays `[ ]` for phase 2b — the standalone
  dependency-light distribution for either language (**coupled to 30 phase
  2's registry choice**).
- [x] **35** — Agent-visible capacity waiting, progress, and cancellation.
  *Developer-experience polish for real agent workloads.* ✅ **Shipped in full
  (phases 1–3, 2026-07-28)** — admission info + queue modes + Redis cross-replica
  admission state and queue-depth caps (phases 1–2); MCP progress via FastMCP's
  built-in `report_progress`, a REST `202`+poll+cancel async lifecycle, real
  dialect-level cancellation (Postgres `pg_cancel_backend`, MSSQL `KILL`) gated
  on a deny-by-default `Policy.allow_query_cancellation` flag, and a full
  migration of capacity/queue rejections from `422` to `429`+`Retry-After`
  (phase 3, Decision Log in docs/PRODUCT_GUIDE.md).
- [x] **94** — Verify (and, if warranted, enable) prepared-statement plan reuse
  for template execution. *Cheap, bounded perf/observability check on the
  already-shipped template path (item 48); adoption polish, not a moat or safety
  item. May close as "verified, no change warranted".* **Depends on 48.**
  ✅ **Shipped** (verified live on both Postgres and MSSQL: both already get
  full server-side plan reuse today via existing driver/pool defaults, no
  config change needed; regression-pinned in
  `tests/integration/test_prepared_statement_reuse.py`).
- [ ] **18** — Stored-procedure catalog. *Extends read coverage where customers
  already encapsulate logic in procs.* **Moved to Decision-gated (2026-07-28):**
  conflicts with the NORTH_STAR permanent non-goal "no stored-procedure /
  arbitrary-procedural-SQL path" — see that section below. Left `[ ]` and in
  its original phase position for history; the automated walk skips it there.
- [x] **57** — Pluggable dialect-adapter architecture. *The enabler that turns
  each new store into an adapter (not a project) — do before 19.* ✅ **Shipped**
  (sync compiler `DialectAdapter` [item 73] + new async `SessionDialectAdapter`;
  reverses the prior inline-branching exception. Adding a dialect = implement
  both + register).
- [ ] **19** — Additional dialects (MySQL, Snowflake, BigQuery, …). *Removes the
  "QueryGate is narrow" objection. **Depends on 57**; also downstream of the
  engine — each new adapter must render every Phase 4 primitive.*
- [ ] **128** — Conform to the final MCP `2026-07-28` protocol revision. *Added
  2026-07-30 by `competitive-scan`; the spec went final on 2026-07-28 (the
  2026-07-22 scan saw only the RC) and we are a full revision behind on
  `2025-11-25`. This sits in Adoption, not Moat, because the sharp edge is
  **distribution**: an intermediary enforcing policy on mirrored headers is told
  to reject the request when the protocol version is older or absent, so we
  infer a conforming gateway gains a defensible reason not to front us —
  directly against the P4 play.* **Gated on Python SDK availability — track
  upstream, do not hand-roll the transport.**
- [ ] **129** — Never advertise a principal-varying MCP result as
  shared-cacheable. *The new revision's `cacheScope` lets shared intermediaries
  reuse a `tools/list`/`resources/read` response across callers; our MCP surface
  is per-principal by construction, so `"public"` on such a result would leak
  one caller's visible connection/table surface to another. Encode it as a
  tested invariant (à la `test_credential_redaction.py`), because the failure
  mode is a default nobody chose, not a policy bug.* **Depends on 128.**
- [x] **130** — Annotate `connection` with `x-mcp-header` for gateway-native
  authorization. *P4 expressed in the spec's own mechanism: a fronting gateway
  can enforce "this identity may only reach connection X" on a header without
  parsing the body, while the query-**shape** decision it structurally cannot
  make stays ours. Annotate `connection` only — mirroring AST internals into
  headers would leak query semantics to intermediaries.* **Depends on 128, 127.**
- [x] **132** — Reconcile stale shipped-status claims left behind by items
  90–93. *Surfaced by the `auditors` claim review on 2026-07-30. Cheap, and it
  is outward-facing: GO_TO_MARKET.md understates four shipped capabilities, and
  item 93's body still points an implementer at a module deleted in July.*
- [x] **133** — Caller-facing, quota-metered verdict endpoint (play P4).
  *Added 2026-07-30 by `competitive-scan`; scope corrected the same day by
  `auditors`. The decision logic already ships twice (item 31 active-policy,
  item 39 draft-aware and already accepting a `StructuredQuery`) — what is
  unscoped is the **non-admin, caller-facing, quota-metered** verdict about the
  **calling** principal, which is what a gateway needs. Filed in Adoption rather
  than Moat by the same test applied to item 128: NORTH_STAR files P4 under the
  leverage moves (turn competitors into distribution), not the three pillars,
  and its gateway value compounds once 128/130 land. Reuse the shared evaluator;
  do not extend `explain`; design it against the discovery-oracle channels
  (THREAT_MODEL QG-19/QG-24).* **Depends on 26, 31, 39, 45, 121.**
- [ ] **131** — Publish the StructuredQuery AST as a namespaced MCP extension.
  *The new extensions framework gives strategic play P2 a standards-blessed
  vehicle. Owning the citable **artifact** is the answer to Cube/DAB having
  adopted our messaging. Internal half (author the spec, reserve the namespace,
  declare + test it) is safe to build; **external publication is decision-gated**
  — a standing commitment and an outward-facing act, maintainer's call.*
  **Depends on 128.**

### Phase 6 — Catalog & observability depth (lowest marginal ROI — opportunistic)

- [x] **37** — Automated end-to-end proof of adaptive semantic learning. ✅
  **Shipped** (`catalog/adaptive_learning_benchmark.py` drives the real 32C
  learning lifecycle e2e; reconciled from a shipped-but-unmarked state).
- [x] **38** — Admin UI catalog-governance workspace. ✅ **Shipped** (phase 1
  review→approve/reject→publish→rollback workflow, plus phase 2 bulk
  actions, export/import, generate-drafts/learn triggers, review_history
  view, and usage-signal browsing).
- [x] **44** — Admin observability / rejection-trend dashboard. ✅ **Shipped**
  (phase 1: process-snapshot overview + panel; phase 2 slice, 2026-07-29:
  config/catalog change-velocity trend card over the durable audit stream;
  phase 2 remainder, 2026-07-30: real time-window trend charts from an
  operator-configured external Prometheus-compatible metrics backend).
- [x] **45** — Non-admin "My access" portal. ✅ **Shipped** (phase 1: identity/
  connections/guardrails/schema; phase 2, 2026-07-30: safe explanations of the
  caller's own recent denials via `GET /help/my-recent-denials`, reusing item
  59's `JsonlAuditEventSource` rather than a third file-parsing
  implementation).
- [x] **47** — Safe draft recovery + config export/import UX. ✅ **Shipped**
  (phase 1: portable change-set bundle export/import + policy-only local
  recovery; phase 2, 2026-07-30: server-side encrypted-at-rest draft store
  with per-principal ownership, retention, and deletion controls).
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
- **18 · Stored-procedure catalog.** Added 2026-07-28 (`roadmap-next` walk):
  `docs/business/NORTH_STAR.md` lists "No stored-procedure /
  arbitrary-procedural-SQL path" as a permanent non-goal — product identity,
  not a gap — and CLAUDE.md's non-negotiable #8 requires an explicit recorded
  NORTH_STAR decision before adding anything the non-goals list forbids.
  TODO.md item 18's own body ("when prioritized... a real security review
  given procedures can have side effects") was written before that non-goal
  was reconciled against it. Needs a maintainer decision — reverse the
  non-goal with a recorded rationale, or close item 18 as will-not-build —
  before any implementation.

## Coordination-gated (partly non-code — an agent can prep, not finish)

- **53** (third-party audit) needs an external vendor engagement.
- **60** (bug bounty) is process/policy.
- **54** (compliance mapping) is largely documentation mapped to real controls.

**Also externally blocked — skip these in the walk (added 2026-07-25).** Both sit
in Phase 0/1, *ahead* of the engine, and both had their blocker described only in
inline prose, so a fresh walk could stall on them. They are listed here so the
selector skips them deterministically, exactly like the two lists above:

- **58 phase 2** — the live LLM / Google Toolbox benchmark run needs external
  model + Toolbox infrastructure that does not exist locally. Phase 1 (offline
  corpus + CLI + published report) has shipped.
- **30 · 89 phase 2** — the signing/provenance *mechanism* has shipped; what
  remains is the **maintainer's own deliberate signed-release tag push** plus a
  Python package-index choice. An agent must never push a tag (see the tag-safety
  rule in the `roadmap-next`/`release-gate` skills).

An agent may do the code/doc-preparable parts and clearly flag what needs a
human/vendor to complete.

---

## Frontier status (updated 2026-07-25, fifth pass) — the frontier is the engine

**2026-07-25 maintainer decision:** the core engine and systems are where effort
goes. The fourth-pass note below (kept for history) read the frontier as "one
buildable UI slice left; everything else gated" — that was a *procedural* reading.
Items 100–106 are gated only on a Decision Log paragraph the maintainer writes,
which is the item's own first step, not an external blocker. Treat the engine as
the live frontier: `roadmap-next` should walk Phase 4 in order (100 → 106),
drafting each item's Decision Log entry for approval as step 1 of that item.
Adoption/breadth (Phase 5) and catalog/UI (Phase 6) wait behind it.

**Engine progress (updated 2026-07-27):** **99, 100, 101, 102, 103 and 104 have
shipped.** The `Expression` substrate every remaining engine item depends on is
real; item 101's `WindowSelectItem.arg`, item 102's three date nodes and item
103's `JoinSpec.condition` are the worked examples of extending it. **The
scope-container substrate is now real too** — `iter_query_scopes` +
`iter_set_op_arms` + the per-scope `subquery_tables` map — and item 104 is its
worked example. **105 (CTE / derived table in FROM, with item 97 phase 2 folded
in) is the next roadmap item**; it should extend that substrate rather than
introduce a third scope enumeration. Its Decision Log entry is that item's own
first step. The canonical regression bar is **12/16**
(`docs/ENGINE_EXPRESSIVENESS_PLAN.md` §5); 105 takes rows 3 and 6.

Four lessons to carry into 105. **(1) The audit normalizer is a third un-foldable
recursion** (beside the visitor and the compiler) and it keeps being the touchpoint
that gets missed: item 102 shipped three `Expression` members that passed the whole
unit suite while every query using one raised at execution, and item 103's mutation
pass found the *same* gap again. Item 104 checked it deliberately and it held — a
new scope container means checking it every time. **(2) Measure, don't read.**
Item 102 had five defects survive a careful diff read and a green suite; item 103's
`ON true`-vs-`ON 1 = 1` rendering was settled by executing against live PG and
MSSQL; and item 104's single most important finding was invisible to any rendering
assertion — SQLAlchemy's MSSQL dialect **silently drops `.limit()` on a compound
SELECT**, which would have turned `clamp_limit` into a no-op on one dialect only.
**(3) Mutation verification is not optional polish, and it has a blind spot worth
naming** — item 103's first pass caught 13 of 16; item 104's caught 15 of 17, and
in both cases every miss was a real defect. The technique probes the rules an item
*adds*; it does not probe the **existing consumers of a field whose type the item
changed**. When an item widens or nullifies an existing field, grep every consumer
as a separate step (the discipline CLAUDE.md already documents for `async def`
conversions). **(4) New for 105: enumerate the single-scope consumers explicitly.**
Item 104's real work was not the compiler — it was finding every place that assumed
a query has exactly one SELECT: the audit shape, `applied_column_masks`, the
item-92 sensitivity approval gate, and the 32C usage signals. Two of those had been
silently wrong for `value_subquery` **since item 97**, which is how long a
single-scope assumption can survive unnoticed. A CTE is another scope container;
walk that same list first, not last.
Item 101's two corrections to §5's table (row 3 needs item 105's derived table, row
15 needs a window to be an `Expression` operand) still stand as recorded walls.

### Fourth pass (2026-07-23), retained for history

Successive batches shipped everything buildable without a new maintainer
decision or external resource. **Done across the cycle:** 56, 96, 95, 54, 60,
**92 (full — triggers + REST token flow + batch tokens + MCP `Context.elicit`
in-session approval, maintainer-approved)**, 42 (full); reconciled 39, 40
(covered by 41), 37; and — after explicit maintainer approval — **97 ph1**
(`IN (subquery)`), **57** (session dialect adapter, reversing the prior
inline-branching decision), **93 ph1** (governed-writes dry-run preview,
execution disabled), **41 ph2** (stateless paginated blast-radius), and
**50 ph2** (`RedisQuotaLimiter` — cross-replica shared quota). The remaining
frontier's buildable threads:

- **Technical-Review Phases 1–3 (107, 108, 109, 110, 111, 112) — ✅ all shipped
  (107–109 on 2026-07-24; 110, 111, 112 on 2026-07-25).** The entire 2026-07-23
  technical-review queue is now closed: quota double-reserve on an approval retry
  (107), the write preview running the real DML before the over-cap check (108),
  the missing MCP write batch-size cap (109), rejecting `value_subquery` in a
  write WHERE at the validation layer (110), consolidating the four hand-rolled
  WHERE-predicate walks into one shared `iter_where_predicates` (111), and the
  scheduled/cron CI job for CVE/SBOM scans + `make test-soak` (112). No buildable
  review items remain.
- **Flagship engine:** **item 99 shipped 2026-07-24** — it was the one
  flagship-engine item needing no Decision Log entry; **100–106 each require one
  before build**. *(Superseded 2026-07-25: that Decision Log requirement is the
  first step of each item, not a gate that defers it — see the fifth-pass note
  above. The engine is now Phase 4 and is the active frontier.)*
- ~~**Admin UI (low ROI): 38 ph2**~~ — shipped in full 2026-07-28 (bulk
  approve/reject/delete, export/import, browser-triggered generate/learn,
  `review_history`, usage-signal browsing — all over item 32B's existing
  scoped routes, no new mutation path); no longer gated.

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
- ~~**35 ph3**~~ — shipped in full 2026-07-28 (progress notifications, REST
  async+cancel, dialect-level cancellation, 429 migration); no longer gated.
- **18 / 51 ph2b / 19** — *large standalone*: stored-proc subsystem (needs a
  real security review — procedures have side effects); the standalone
  dependency-light distribution for the (now-shipped, both-language) client
  SDK (coupled to 30 ph2, not locally validatable); new dialects (need live
  DBs, now unblocked *architecturally* by 57).
- **44·45·47 ph2** — *admin-UI / durable-infra phase-2s* needing external metrics
  history (44), more UI (45), or an encrypted-at-rest draft store (47).
- **F4 / P2** — *decision-gated*: NL→StructuredQuery (model-provider/posture
  decision) and opening the AST as a standard (governance commitment).

*(The fourth pass closed by telling `roadmap-next` to surface two live threads —
38 ph2's UI slice and a decision on 92's elicitation SoD posture. Both are
superseded: 92 shipped fully, the 2026-07-25 re-prioritization put the engine
ahead of the UI slice, and 38 ph2 itself has since shipped in full
(2026-07-28). Trim this note as the frontier moves.)*

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
above — none of these items change the Phase 0–6 execution order for the
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
- [x] **107** — Batch query execution double-reserves quota on an approval
  retry. ✅ **Shipped** (the paused first attempt stashes its live quota
  reservation on the `ApprovalRequiredError`; the in-session retry reuses it via a
  private `_reserved_quota` param instead of reserving again, so one approved
  query consumes exactly one unit. Test asserts a one-entry quota window across
  the approval-required-then-resolved retry; verified it fails `2==1` when reverted.
  The REST 428->resubmit flow is untouched — that's a genuinely new request).
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

- [x] **112** — Add a scheduled (cron) CI workflow for dependency/security
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

- [x] **117** — `date_bucket` over a non-temporal column diverges across dialects.
  ✅ **Shipped** (all three date primitives now share one operand walk, guarded by
  a coverage test that fails if a fourth is added without being wired in).
  *Measurement is what settled it: over an INTEGER column Postgres errors, MSSQL
  returns 1900-01-02 and SQLite returns -4712-01-05 — three different wrong
  answers, so no caller had correct behavior and the change is a bug fix rather
  than the breaking one it looked like. Surfaced by item 102's confirmation
  review.* **Depends on 102.**

- [x] **116** — A write's WHERE was exempt from `max_where_depth`,
  `max_where_predicates` and `max_in_list_size`. ✅ **Shipped** (each rule is now a
  single function both paths reach, verified by spying rather than by name identity;
  enforced on writes — and on every statement of a batch up front — before any DML
  compiles or any `COUNT(*)` runs, with a test proving no statement reaches the
  database; the read `Policy` fields are used deliberately, since a write's WHERE
  reads rows to select them). *Surfaced 2026-07-26 by the item-114 audit; a
  `delete … where id in [<huge list>]` used to be rendered in full client-side
  first — 100,000 values measured at a 689 KB statement.*

- [x] **115** — The hand-maintained guardrail-field lists had drifted from
  `Policy`'s cap set. ✅ **Shipped** (all **four** copies —
  `admin/access_diff.py`, `EffectiveGuardrails`, `help/service.py` and the admin
  UI panel — now derive from one `GUARDRAIL_FIELDS`; `EffectiveGuardrails` is
  generated from `Policy`, so it cannot drift in membership or type;
  `approval_sensitivities` gets its own diff change; and a new cap must state its
  direction or a test fails — which caught two unconsidered fields on its first
  run). *Surfaced 2026-07-26 while adding item 101's two caps: nine caps were
  invisible to at least one surface, so loosening one was diffed as "no change".*

- [x] **114** — The write tool's MCP schema advertised read-only predicate fields
  it rejects. ✅ **Shipped** (the write AST gets its own narrowed
  `WritePredicate`/`WriteWhereGroup`, converted to the read `WhereNode` at the
  validation boundary — one predicate walk, one compiler, and the runtime
  rejections kept as defence in depth. MCP context budget **down 19%**
  (128,551 -> 104,042; the write tool -63%), putting the ceiling back below item
  100's. A fifth hand-rolled predicate enumerator was found — and deleted, being
  both unreachable and untested.)

- [x] **110** — Explicitly reject `value_subquery` in a write's WHERE at the
  write-validation layer instead of relying on the compiler's `ctx=None`
  default to fail it.
  - Why: not currently exploitable, but it fails at the wrong layer with a
    compiler-internal error, and is a latent trap for a future write-compiler
    change.
  - Scope: `validation/write_policy_validation.py`.
  - Acceptance criteria: a `value_subquery` in a write WHERE raises a clean
    `QueryValidationError` at validation time; regression test added.
- [x] **111** — Consolidate the four hand-rolled WHERE-predicate tree walks
  (`policy_validation.py`, `schema_validation.py`, `write_policy_validation.py`,
  `write_schema_validation.py`) into one shared helper, mirroring how item 96
  centralized column-ref walking into `iter_column_refs`.
  - Why: all four are correct today but could silently drift the next time
    `WhereNode` grows a new combinator — the exact class of bug item 96 was
    built to prevent for column refs.
  - Scope: the four validator modules.
  - Acceptance criteria: one shared predicate-iterator helper; all four
    validators' existing test suites pass unchanged (no behavior change).

### Review Phase 5 — Findings from the 2026-07-27 item-103 audit

- [x] **118** — `min_group_size` was defeated by any fan-out join. ✅ **Shipped**
  (the floor counts JOINED rows, so a fan-out lifted a singleton group above *k* —
  a claimed guarantee, QG-29 / INFERENCE_RISKS R3, that did not hold across a join.
  Such a join is now **refused** on an aggregate query while the floor is set,
  scoped by reflected uniqueness metadata so the ordinary join-onto-a-primary-key
  shape still runs. The security test that pinned the leak was **inverted**, not
  deleted.) *Pre-existing, not an item-103 regression — measured against an
  equality join that had shipped for months, which is precisely why a fix scoped to
  non-equi conditions would have been theater. Surfaced by the item-103 completion
  audit.*

### Review Phase 7 — Findings from the 2026-07-27 item-105 build

- [x] **122** — `_unique_column_sets` crashed on any FROM element that is not a
  `Table`, so item 118's k-anonymity floor raised `AttributeError` on **any
  aliased join** instead of making a policy decision. ✅ **Shipped** (an alias
  looks through to its element; a cte/subquery reports no uniqueness and is
  treated as able to fan out — the fail-closed direction the floor requires).
  *Pre-existing and live since item 118. It is the blind spot this file's frontier
  note already named: mutation testing probes the rules an item ADDS, not the
  existing consumers of a value whose TYPE widened — here `sa.Table` -> any FROM
  element. Found by measuring, not by reading the diff.*

### Review Phase 6 — Findings from the 2026-07-27 item-104 audit

All three are **pre-existing**, surfaced by the item-104 completion audit rather
than caused by it. None is a policy bypass — enforcement is scope-correct — but
119 is a silent wrong answer on a shipped feature and should lead.

- [x] **119** — `top_n` mis-resolves and DROPS a column when two projections share
  a base name. *Item 104 hit the identical root cause on its own new path and
  fixed it positionally; the same fix shape applies here.*
- [x] **120** — The audit shape records nothing for a nested `IN (subquery)`.
  *The Proof-pillar half of the gap item 104 closed for set-op arms.*
- [x] **121** — Report-only surfaces (`ExplainResult.tables`, the candidate
  simulator's `referenced_tables`) still assume one scope. *Operator-facing
  accuracy, not enforcement.*

### Review Phase 8 — Findings from the 2026-07-27 item-119/120 audit

- [x] **124** — Most of `tests/unit/` is not selected by `pytest -m unit`, so the
  pre-commit gate silently skips ~60% of it. *Lead this phase: it weakens every
  other gate, and the fix is a collection hook rather than 52 edits.* ✅
  **Shipped** (a `tryfirst` `pytest_collection_modifyitems` hook in
  `tests/conftest.py` tags every collected test with its directory tier —
  unit/integration/security — before pytest's own `-m` filter reads it, so a
  new file can never again be silently invisible to the gate; verified live
  that `pytest -m unit` now collects the full 1725-test `tests/unit/` tree
  with zero live-DB leakage).
- [x] **123** — A select-item `CASE`'s condition subtree is absent from the audit
  shape, so two spellings of one query audit differently. *Proof-pillar fidelity
  on a position policy always rejects; the last walker not reaching its predicates
  through `as_expression()`. Low priority, small fix.* *Picked ahead of Phase 5's
  item 19 on 2026-07-28 (maintainer decision via `roadmap-next`): 19 is XL/multi-day
  (new dialect adapter + live-DB infra + re-proving every Phase-4 primitive) and
  doesn't fit a session, while 123 is small, unblocked, and closes a real
  Proof-pillar gap now.* ✅ **Shipped** (`CaseSelectItem` branch now records
  `conditions` via `_where_shape`, mirroring `CaseExpr`; regression tests
  mutation-verified).

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
3. Treat any item carrying a `🚧 **CLAIMED**` line as unavailable. Report its
   owner and continue only to an independently eligible item; never steal or
   auto-expire the claim, including when an item-number override points to it.
4. The **next item** is the first one that is *not* fully `✅ DONE`, is
   *unclaimed*, is *not* in the Decision-gated list, is *not* in the
   Coordination-gated / externally-blocked list, and whose TODO.md dependencies
   are satisfied. Skip (and report) any gated item reached before it. **As of
   2026-07-27 this resolves to item 106** — the walk skips 58 ph2 and 30·89 ph2
   (externally blocked), 53 (external vendor), and lands on Phase 4's engine
   pillar (99–105 and the item-97 phase-2 slice have shipped).
   **A required Decision Log entry is not a skip condition.** Items 100–106 each
   need one recorded in `docs/PRODUCT_GUIDE.md` before code — that is the item's
   own first step (draft it, get maintainer ratification, then build), not a
   reason to defer the item and move on.
5. Announce: the last completed item (where the previous agent left off), the
   next item, why it's next, and any items skipped and why.
6. Add the canonical `🚧 **CLAIMED**` line immediately below the selected
   item's checkbox, then re-read that roadmap entry. Begin only if exactly one
   claim is present and it is yours.
7. Implement it with the full `next-item` discipline (scope → production-grade
   impl → tests → docs → release gates → one clean commit → `ship-item`).
8. On completion, remove the claim and tick this file's checkbox for that item
   in the same commit. If handing the item back unfinished, remove the claim
   before stopping.

## Maintenance rules

- **Re-sequencing is allowed and expected** as the market and pilots teach us —
  but a re-order must be a deliberate edit with a one-line reason, not a drift.
  The competitive-scan and next-item workflows may propose re-ordering.
- When a *new* TODO item is added, place it in the correct phase here (or note
  it as unplaced) — a new item is not automatically last.
- Never move an item's *done-status* here without the matching `✅ DONE` in
  TODO.md. TODO.md leads; this file follows.
- A claim is temporary coordination state. Keep the canonical owner + UTC
  timestamp shape, never maintain claims in TODO.md, never steal or auto-expire
  another owner's claim, and remove your own claim on completion or hand-back.
  Do not create a standalone commit containing only a claim.
- Keep the rationale one line per item. Depth lives in TODO.md, not here.
