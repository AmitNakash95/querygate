# QueryGate commercial subscription — enforcement and control plane

*QueryGate as a proprietary, closed-source, self-hosted subscription product: a
signed entitlement the deployment renews, two enforcement funnels, and the
vendor control plane that sells and issues it.*

> **STATUS: PROPOSAL.** Owner decisions are in §1; everything else awaits
> approval. **No code in this plan has been written.** §3–§6 describe a design
> *to be built* — read every indicative sentence there as "will".
>
> **Revision 4 (2026-08-23)** — supersedes revs 1–3. Rev 3 was audited by four
> reviewers before any code was written; it contained three defects that would
> have shipped a subscription product that **did not stop writes**, a key cap
> that **contained nothing**, and a clock mark that **expires every deployment
> on first restart**. §13 lists what changed and why.

---

## 1. Decisions

| # | Decision |
|---|---|
| 1 | Hosted plane scope: **licensing + Notary on one control plane** |
| 2 | Key custody: **KMS-held issuing key, sign-only; offline root** |
| 3 | Sales motion: **Stripe self-serve**, plus sales-assisted enterprise |
| 4 | Code location: **a subdirectory of this repository** (`control-plane/`) |
| 5 | **Hard subscription enforcement.** Pay monthly or for a declared term; non-payment stops the product. |
| 6 | **Proprietary and closed-source.** No BSL flip; repo stays private; distribution is the signed image. |

All six dated 2026-08-23. **Decisions 5 and 6 overturn recorded owner decisions
of 2026-08-21** (Shape A, soft-enforcement-only, the permanent rejection of
phone-home, the BSL flip). They need a `docs/PRODUCT_GUIDE.md` Decision Log
entry **and** a `NORTH_STAR.md` amendment — Phase 0 work. This document is not a
substitute for either.

### What decision 5 necessarily implies

Monthly enforcement requires the deployment to check in: an offline token can
only expire on a schedule fixed at signing, so enforcing *monthly* means
delivering a fresh one monthly, and any automation that fetches it is a network
call. There is no design that avoids this.

- `tests/security/test_no_phone_home.py` is **narrowed, not deleted** (§7.5).
- *"It makes no outbound calls to us, ever"* comes out of `LICENSING_FAQ.md`.
- **The load-bearing half survives exactly:** no credential, query, row, schema,
  principal, policy, or connection identifier ever leaves. §10 states what the
  exchange *does* expose, because "nothing else" would be false.

---

## 2. The licence: replace, don't delete

`LICENSE`'s banner already says QueryGate *"remains proprietary and all rights
are reserved … The repository is private."* So the product is proprietary
**today**; decision 6 means the banner never comes off.

- **`docs/legal/EULA.{en,he}.md` becomes the licence of record.** It already
  covers *"source and/or object (container image, package, or binary) form."*
- **`LICENSE` must keep existing** — `pyproject.toml`'s `license-files`,
  `check_release_artifacts.py`'s wheel/sdist/image assertions, and the
  Dockerfile's `COPY LICENSE` all require it. **Replace its BSL body with the
  proprietary notice; do not delete the file.**

**EULA clauses the subscription needs** (all to counsel, both languages):

> ✅ **All of the below landed in both languages with item 210 (2026-08-27),
> plus a third-party pass-through clause in §2 that was not on this list.** The
> table is now the record of *what was asked for and why*, not open work — read
> the "Why" column, ignore the present-tense "currently"/"does not exist"
> phrasing in it. The text is still a draft pending counsel. Section map:
> fees/renewal/tax §14 · non-payment §14.4 · no-refund §14.3 · suspension vs
> termination §7.4 · cure period §7.2 · anti-circumvention §3(h) and §15.5 ·
> technical-enforcement notice §15.1-15.4 · transmission disclosure §16 ·
> service availability §17 · post-termination audit retrieval §18.

| Clause | Why |
|---|---|
| Term, renewal, fees, invoicing, tax | §13 currently defers fees entirely to "the Order" |
| Effect of non-payment; **suspension** distinct from termination | §7 offers only termination; §3's 402 state is suspension |
| **Cure period matching §3.1's grace** | §7 permits termination *"immediately upon written notice"* — flatly inconsistent with a 14-day grace and a 45-day warning ramp |
| **Anti-circumvention** | §3 prohibits reverse engineering but **not** disabling or patching the entitlement check, and does not make it a material breach. §6's whole argument assumes this clause exists. It does not. |
| **Notice of technical enforcement** | A product that silently stops is an exposure; one whose licence says it will is an accepted term |
| **Licence-data transmission disclosure** | §10 "Data and Security" is one-directional. Nothing discloses what the deployment transmits to us, how often, or how long we keep it |
| **Licence-service availability** | The customer's operation now depends on our service while §8 disclaims all warranties. Either that disclaimer narrows or §3.1's promise does |
| **Post-termination audit carve-out** | §7 ends all licences on termination, but §3.2 deliberately keeps `querygate-audit` working. Without a surviving perpetual licence to export their own records, the product permits an act its own licence forbids — worst in exactly the regulated sectors that need it |

---

## 3. Enforcement architecture

A short-lived **signed entitlement**, renewed daily, cached, verified offline.

```
   CONTROL PLANE                                CUSTOMER DEPLOYMENT
   ┌───────────────┐                            ┌────────────────────────────┐
   │ Stripe        │                            │ SubscriptionManager        │
   │    ↓ webhook  │                            │  (per worker process)      │
   │ Entitlement   │◀── GET /v1/entitlement ────│  · refresh daily + jitter  │
   │    ↓          │    (per-deployment key)    │  · verify EVERY read       │
   │ KMS sign  ────┼──▶ signed entitlement ────▶│  · wall-clock mark         │
   └───────────────┘                            └──────────┬─────────────────┘
                                                sync, in-memory│
                            ┌───────────────────────────────┴──────────┐
                            ▼                                          ▼
          StructuredQueryService._validate_and_compile      validate_write_policy
                    (reads: execute/explain/verdict/batch)   (writes: all 3 paths)
```

### 3.1 The state machine

Two **orthogonal axes**, not five flat states. Modelling them separately is what
structurally prevents the gate from ever depending on refresh health:

```python
class EntitlementState(StrEnum):   # the ONLY axis the gate reads
    VALID = "valid"; GRACE = "grace"; EXPIRED = "expired"

class RefreshHealth(StrEnum):      # observability; the gate never reads this
    OK = "ok"; DEGRADED = "degraded"; FAILING = "failing"

class SubscriptionStatus(pyd.BaseModel, frozen=True):
    entitlement: EntitlementState
    refresh: RefreshHealth
    clock_regressed: bool
    expires_at: datetime          # aware UTC
    grace_expires_at: datetime
    consecutive_refresh_failures: int
    @property
    def allows_queries(self) -> bool:
        return self.entitlement is not EntitlementState.EXPIRED
```

One pure `evaluate(entitlement, *, now, high_water_mark) -> SubscriptionStatus`,
cached on the module singleton, recomputed by the timer. The gate reads
`.allows_queries`; health reads `.entitlement`; the CLI renders the model.
**Nobody else compares a datetime** — three independent comparisons is how the
CLI ends up reporting "valid" while the gate returns 402.

| Condition | Behaviour |
|---|---|
| Valid, refresh succeeding | Normal. `expires_at` rolls forward ~30 days. |
| Valid, **refresh failing** | **Fully operational** to `expires_at` (~30 days tolerance). `WARN` from the third *consecutive* failure; **a success resets the counter to zero**. |
| Expired, inside grace (+14d) | **Fully operational.** Warnings; health `degraded`. |
| Past grace | **Blocked** (§3.2). |
| Clock regressed beyond tolerance | Enters **grace**, never expiry directly (§3.5). |
| **Cold start: no cached entitlement, no network** | **Fails open, in grace, for a bounded first-boot window.** A fresh install, a DR failover into an isolated network, or a scaled-out replica has nothing to be "valid" from; failing closed there means our outage *is* their outage on day one. |

**The load-bearing property: a failed refresh is never fail-closed.** Only a
server-confirmed non-renewal is, after ~45 days of visible warnings.

### 3.2 Two funnels, not one

Rev 3 claimed one gate in `StructuredQueryService`, deriving it from
non-negotiable 4. **That derivation was invalid** — non-negotiable 4 means one
*AST-validated* path, never one class. `WriteExecutionService` and
`WritePreviewService` are separate classes whose own docstring says
*"Deliberately a separate class,"* and `_execute_many_atomically` opens its own
`session_scope` and commits DML without calling `self.execute()`. Gated as rev 3
specified, **a lapsed deployment would still commit INSERT/UPDATE/DELETE.**

| Funnel | Covers |
|---|---|
| `StructuredQueryService._validate_and_compile` (`execution/service.py:406`) | `execute`, `explain`, `verdict`, and batch — its own comment already names this as *"the four ways an ad-hoc AST reaches a database"* |
| `validation/write_policy_validation.validate_write_policy` | the only function all three write entry points call |

**Not `connections/engine.session_scope`** — the true single seam, but it would
also stop `CatalogRefreshMonitor` and all schema reflection, and push a
`connections/` → `licensing/` edge a layer below `execution/`.

**Every DB-touching path, classified. An unclassified path is an unenforced one:**

| Path | Verdict |
|---|---|
| `execute`, `execute_many`, `explain`, `explain_many` | **Blocked** |
| Write execute / execute_many / `_execute_many_atomically` / preview | **Blocked** |
| `verdict`, `verdict_many` | **Blocked** — but must report a reason *distinct from* `"not-available-to-you"`, or a 45-day-late invoice presents as a policy denial |
| `list_tables`, `describe_table`, `search_catalog` | **Blocked.** They are product. |
| `CatalogRefreshMonitor` (`catalog/refresh.py:249`) | **Stops.** A background timer re-reflecting the live schema unpaid is product. |
| In-flight async executions admitted before the lapse | **Complete.** Bounded, minutes-wide, stated rather than unexamined. |
| Health ping (`health.py`), connection-test probe, query cancellation | **Exempt** — `SELECT 1`-class diagnostics and stopping work already running |
| Startup | **Exempt.** Boots, serves 402s, says exactly what is wrong. Refusing to start leaves an operator with no diagnostics at 3am. |
| `querygate-audit verify`/`receipt`, the `/observability/worm-search` route | **Exempt.** Their compliance record is their data; withholding it is a fight not worth having and arguably unlawful in regulated sectors. Verified not to be a data-exfiltration path: events carry `query_shape` — structure and names, never literals — and it is a read over already-recorded events. |
| Admin UI, metrics, config reload, `querygate-license status` | **Exempt** |

### 3.3 The error must exist in four registries or it is a 500

Rev 3 said "`SubscriptionExpiredError`, HTTP 402, and the MCP equivalent" as if
naming it were enough. There is **no 402 anywhere in this codebase today**, and
an unregistered exception is masked to a generic 500.

- **Define it in `core/exceptions.py`, not in `licensing/`** — `api/_errors.py`
  and `mcp/exceptions.py` import only from `core.exceptions`; putting it in
  `licensing/` plants that import into both transports.
- **It must not subclass `ValueError`/`PolicyViolationError`**, or existing
  handling maps it to 422 and labels it `"policy"`.
- Register in **all four**: `_ACTIONABLE` + a 402 handler (`api/_errors.py`,
  using `starlette.status`, never a raw int); a named code before the `INTERNAL`
  fall-through (`mcp/exceptions.py`, which otherwise also logs a full traceback
  on every tool call); and its own label in `classify_rejection`
  (`metrics.py`) — which fixes the metric **and** the audit `error_category` in
  one place.

> Why that last one matters most: `classify_rejection` falls through to
> `"db_error"`, and that value is written into the **hash-chained, WORM-archived**
> audit event. Unfixed, every non-payment block is recorded in the customer's
> compliance ledger as a database error — a falsehood in the artifact that *is*
> the Proof pillar, and the record a regulator reads.

**Batch surfaces must re-raise.** `_execute_batch_item`, `explain_many` and
`WriteExecutionService._batch_error` each catch bare `Exception` and return a
per-item error string inside an **HTTP 200**. A caller that always batches would
never see a 402, and dunning automation keyed on 402 would see nothing. Add an
explicit re-raise *before* the generic handler, and check the gate **once per
request before fan-out** at the four transport call sites. This is the one place
"no second check in any transport" bends; it bends deliberately, with the
service-level checks kept as the fail-closed backstop.

### 3.4 The signed payload

`{ schema_version, kid, manifest_serial, serial, purpose, enforcement, org_id,
deployment_id, plan, features[], issued_at, expires_at, grace_expires_at,
renewal }`

- **`schema_version` is mandatory, and strict parse is version-scoped.** Rev 3
  paired "reject unknown fields" with no version field. Those two together mean
  the payload can never be extended: the day the control plane adds a field,
  **every deployed verifier rejects every entitlement** — a simultaneous,
  fleet-wide outage of a fail-closed gate, caused by a vendor change customers
  cannot roll back. Rule: reject unknown fields *at a known `schema_version`*;
  reject an unknown `schema_version` with its own distinct reason. Migration is
  "ship the verifier first, then issue the new version."
- **All times aware UTC**, `AwareDatetime`, naive rejected as a typed failure.
  A naive value compared against an aware `now` raises `TypeError` inside a
  fail-open path that swallows it.
- **`features[]` is the only capability authority.** `plan` is a display and
  billing label, **never branched on** — pinned by a source-level test. Two
  authorities for "what may this deployment do" means a mis-issued entitlement
  resolves differently at every call site; under-grant is a support ticket,
  over-grant is unbilled revenue.
- **`enforcement: observe | enforce`** — see §3.6.

### 3.5 Verification, and the bounds rev 3 was missing

Strict parse (unknown fields, duplicate JSON keys via `object_pairs_hook`,
base64 `validate=True`); verify over the **exact decoded bytes** then parse;
domain-separated signatures; `manifest_serial` binding; **reject duplicate
`kid`s**; key window checked against `issued_at`; typed `LicenseFailure` per
rule; **the disk cache holds the signed bytes and is re-verified on every read,
never a parsed structure** — otherwise an operator edits `expires_at` in a JSON
file and the subscription is defeated, and no conformance vector would notice
because vectors test the verifier, not the cache path.

**Two bounds rev 3 lacked, without which §4's key cap contains nothing.**
Because the key window is checked against `issued_at`, and `issued_at` is chosen
by whoever signs, a leaked key mints entitlements backdated inside the window
with `expires_at` in 2099 — valid forever, on unlimited installs, after the key
has rotated. Add, each with its own failure reason:

- **Freshness:** reject if `now - issued_at > lease_window + grace + skew`, and
  reject if `issued_at > now + skew`.
- **Max term:** reject if `expires_at - issued_at > MAX_LEASE_DAYS` for leased
  mode, or `> MAX_OFFLINE_DAYS` for offline — **compiled-in constants, never
  payload fields.**

**Anti-replay is a serial floor, not a clock.** Persist `max_seen_serial`
alongside the time mark and require strictly increasing serial per deployment. A
restored snapshot then presents an old serial and is rejected **without needing a
trustworthy clock at all** — which is the only anti-rollback control that has no
false-positive mode.

### 3.6 Observe → enforce is a signed field

The single most important structural fix in rev 4, and the repo already has the
precedent: `CostEstimationMode.OBSERVE/ENFORCE` (`policy/models.py:21-37`),
whose docstring says OBSERVE *"records what would have been rejected … without
blocking the query — use it to calibrate … before switching over to ENFORCE."*

`enforcement` is a **field in the signed payload**. Not a config flag — a
subscription gate whose off-switch is a customer-settable environment variable
is a documented bypass, and it can be half-on (enforce on the REST worker,
observe on an MCP server started with different env). Not a build flag — two
artifacts, and the enforce path would never be exercised in the image customers
actually run during Phase 1, defeating the point. Not absent code — that makes
go-live day the first-ever execution of a new hot-path call site.

Properties: the customer cannot flip it; **the call site exists and runs from
Phase 1**, with only the final `raise` conditional; the vendor rolls it forward
per deployment and can roll one customer back without shipping an image; it
cannot be half-on. Observe mode emits `querygate_subscription_would_block_total`
and a log line with the failure reason — mirroring `_observe_cost_estimate`.
**Without that counter, observe mode proves nothing**, because a false positive
never surfaces as anything an operator would notice.

### 3.7 The clock mark

Rev 3 said "monotonic" three times. **`time.monotonic()` is boot-relative with
an undefined epoch and cannot be persisted across restarts** — implemented as
written, a fresh container reads near-zero against a large persisted value and
registers a catastrophic regression, expiring **every deployment on its first
restart.**

- Persist a **wall-clock** high-water mark; `time.monotonic()` only for
  in-process interval math.
- Regression counts only beyond `CLOCK_REGRESSION_TOLERANCE` (**hours**, not
  seconds), so NTP steps and snapshot skew are absorbed.
- A regression **enters grace**, never expires directly.
- An **absent** mark is fail-open (fresh volume, first boot).
- Clamp a mark further ahead than `MAX_TRUSTED_MARK_AHEAD` rather than trusting
  it — a hostile or corrupt file set to 2099 would otherwise permanently brick a
  paying deployment, which "fail-open on I/O errors" does not cover.
- **Per-process, never shared across replicas.** A shared mark turns one bad
  clock into a fleet-wide outage; a per-pod mark limits it to that pod.
- **Monotone-max on write** — never overwrite with a lower value.
- A *persistent write failure* must be **visible** (counter, health `degraded`,
  a CLI line), not silent. Fail-open on the write is right; silently disarming
  the only anti-rollback control is not.
- Written by the timer **only** — never from `audit/logger.py` or an
  `AuditSink`, both of which run per-query. (`LICENSE_ENFORCEMENT.md:244-249`
  recommends exactly that; it must be corrected — §12.)

### 3.8 Deployment identity — honestly

**`deployment_id` is a control-plane *detection* control, not client-side
enforcement.** A local, offline-capable verifier can only compare the payload's
`deployment_id` against a locally-configured value; whoever holds the
entitlement sets their local value to match. There is no proof-of-possession,
because an offline-verifiable artifact cannot do challenge-response. What it
actually buys is that the issuance ledger records one deployment refreshing from
N sources.

**The enrolment flow rev 3 omitted, without which decision 5 has no revenue
enforcement at all:**

- The emailed credential is a **one-time enrolment token**, not a long-lived key.
- First use POSTs `/v1/enrol` and receives a **per-deployment** bearer key; the
  token cannot mint twice.
- **`deployment_id` is assigned by the control plane**, never proposed by the
  client.
- The entitlement carries a **deployment count cap**, enforced at enrolment.
- A stated rotation/revocation path for a leaked deployment key.

### 3.9 Offline mode — the least-defended corner, named

Air-gapped enterprise gets a long-dated entitlement with no refresh, same
signature format, same verifier. It must be labelled honestly: **revocation
cannot reach it.** Stopping issuance revokes a deployment that still asks; it
does not revoke a signing key, and an install that never refreshes never
receives a rotated manifest. Compensating controls: short term, a per-contract
`kid`, a named signatory, and treatment as a legal event. A technical path, if
ever wanted, is a root-signed revocation list shipped **with each release
image** and checked against `serial` — a release, not an entitlement.

### 3.10 Acquisition varies; verification does not

- **`LeaseSource` Protocol + `_LEASE_SOURCES` registry** for acquisition:
  `HttpLeaseSource` (client, URL, bearer key, refresh task) and
  `FileLeaseSource` (a path, no task, no network). Genuinely different state and
  lifecycle — the `SecretResolver`/`AuditSink` shape. **Dispatched on operator
  configuration, resolved once in the lifespan.**
- **No Protocol on the verifier or the expiry evaluator.** §3.5 specifies one
  verifier; one variant is CLAUDE.md's "Don't over-apply this" case.
- Rev 3 said the mode is "selected by a field in the signed payload." That is
  **circular** — you need a verified payload to read the field, but the mode
  determines how you *obtain* a payload. The payload's `renewal` field can only
  tell an already-verified evaluator whether to expect a refresh.

---

## 4. Trust model and keys

Offline Ed25519 root (hardware, ~2×/year) signs a key manifest
`{serial, entries:[{kid, pubkey, not_before, not_after, purpose}]}`; issuing keys
live in KMS with `sign` permission only, logged in the KMS audit trail outside
the app's blast radius. The manifest travels inside the entitlement, so rotation
costs no release.

**Containment, stated accurately:** with §3.5's freshness and max-term bounds, a
leaked issuing key is useful for at most `MAX_LEASE_DAYS` past the last honest
clock reading — *not* "until its window closes," which was rev 3's claim and was
false. Deployment revocation works for anything that still refreshes; **key
revocation remains release-recoverable only**, and does not reach offline mode
(§3.9).

> ⚠️ **Verify on the day.** Ed25519 signing: **GCP Cloud KMS** yes
> (`EC_SIGN_ED25519`); **AWS KMS no** (ECDSA/RSA only); **Azure Managed HSM
> least certain.** Not checkable from this repo; catalogues change. Re-confirm
> each with a dated note before §13.1 is decided.

---

## 5. The control plane

Stripe is the source of truth for paid status; we do not rebuild subscription
lifecycle. Six tables: `org`, `entitlement`, `deployment`, `enrolment_token`,
`entitlement_issuance` (append-only), `webhook_event` (idempotency).

- **Publicly reachable: two endpoints only** — the Stripe webhook
  (signature-verified, idempotent) and `GET /v1/entitlement` (per-deployment
  bearer key). `/v1/enrol` accepts one-time tokens. Admin endpoints that can
  mint bind to a private network behind SSO or mTLS.
- **Rate-limit per deployment, not per org** — a per-org limit is
  self-abusable: exhaust your own quota to force refresh failure and ride
  §3.1's fail-open for a free 30-day extension every cycle.
- **The control plane resolves `plan` from the entitlement, never from the
  request.** A client must not assert its own tier.
- **No customer login in v1.** Billing is Stripe's hosted portal; enrolment is
  email. No passwords, no sessions.
- **Blast radius:** org names, plans, terms, deployment ids. No payment data
  (Stripe holds it), and by construction never a credential, query, or row.

---

## 6. Tamper resistance — honestly

Closed source raises the bar; it does not make the gate unbypassable.

**What decision 6 buys:** a private repo, unpublished enforcement logic, bypass
requiring reverse engineering rather than reading a diff, and — most
importantly — circumvention as an unambiguous wilful breach of a signed EULA
*(EULA §15.5, which landed with item 210)*.

**What it does not buy:** Python in an image is readable by anyone who can
`docker pull`.

**On Nuitka — rev 3 recommended it; rev 4 does not.** Compiling
`querygate/licensing/` hardens the verifier's internals and the pinned root key.
It does **not** harden the *decision*, which is a call from plain-Python
`execution/service.py`. What stays trivial regardless: edit the one call line;
`sitecustomize.py` monkeypatching the symbol at import; or **shadowing the
compiled extension with a pure-Python module earlier on `sys.path`** — so the
compiled artifact is not even an obstacle. A real bar means compiling the call
sites too, which §6 declines on debuggability grounds. Add the unpriced cost —
a native extension complicates the SBOM and pip-audit story §7 leans on — and
the honest conclusion is that Nuitka buys roughly what shipping `.pyc` buys.
**§13.4 is reframed from "do it" to "decide whether to skip it."**

**The layer that actually holds:** customers who would patch a licence check are
not the customers who sign five-figure contracts with an indemnification clause.

---

## 7. Rebuilding the trust story without open source

1. **An independent penetration test** — already an open GTM item, already
   caveated on every outward surface. Under closed source it moves from
   nice-to-have to **load-bearing**: it becomes the primary external evidence
   that the code does what the docs say.
2. **Source escrow** for enterprise — answers "what if you disappear," which
   self-hosting used to answer for free.
3. **Auditor access under NDA** — converts the loss into a sales-qualified event.
4. **Signed, attested builds — already shipped.** `release.yml` does cosign
   keyless signing and SLSA provenance, both digest-bound. Verify once against
   the published `v0.1.0` digest and record the date before promoting it.
5. **SBOM + third-party licence inventory — already generated**, with the caveat
   that the inventory is Python-packages-only (item 196 has open phases).
6. **Narrow, don't delete, the phone-home guard** (§12). Keeping the
   module-allowlist half and asserting **exactly one** module in shipped source
   holds a vendor hostname turns "we now phone home" into a *bounded, provable*
   statement — one of the few checkable claims left when nobody can read the
   source.

---

## 8. Packaging and the funnel

Losing "free, unlimited, forever" removes the distribution engine the GTM plan
ran on. Replacement: a **30-day full-featured trial**, same machinery,
`plan="trial"`, no special-casing. Paid tiers: Team (Stripe self-serve) and
Enterprise (invoiced, offline mode, indemnification, SLA, escrow, Notary).
**Two Enterprise goods do not exist yet and must not be sold until they do:**
the penetration test and Notary.

---

## 9. Process and worker topology

`api/app.py`'s lifespan runs **once per uvicorn worker**, and `num_of_workers`
is operator-settable. Rev 3 never mentioned workers or replicas in 432 lines.
At 4 workers: 4 concurrent daily refreshes against a per-deployment rate limit
(our own limiter becomes the outage), 4 unsynchronised writers to one cache
file, and 4 racing high-water marks — a lagging worker overwriting a leading one
arms the regression trigger against the deployment's own processes.

Required: **advisory file locking** (the `catalog_process_lock` pattern),
**monotone-max** on the mark, **jittered or leader-elected refresh**, and an
explicit statement that per-worker duplication is accepted with the rate limit
sized for it. The repo already documents this hazard class for a cooldown in
`core/config.py`.

**Naming:** call it `SubscriptionManager`, not `LeaseManager` —
`CredentialLeaseMonitor` already exists, refreshes a time-bounded grant before
expiry, and emits `action="lease_refresh"` into the same audit stream.

**Precedent to copy: `CredentialLeaseMonitor`** (`config_reload.py:246`), not
`WormFlushMonitor` and not `HealthMonitor`. All four of its properties: network
I/O off the event loop, fail-open per iteration, **only the exception type
logged, never its message** (a licence-server error body could echo an org id or
a deployment key), and a `system:` principal for the automatic action.

**State lifecycle:** the store/cache Protocol is **`async` from the first
commit** (the `admin/observed_shapes.py` precedent — a sync-then-widened
Protocol is exactly what broke the compensation store, and for a fail-open disk
write an unawaited coroutine produces no error at all, just a permanently empty
cache). The **verdict read the gate calls stays sync** — pure in-memory
comparison, no `await`, no I/O, on the hot path.

**Ten pieces of process-wide state need `tests/conftest.py` reset in both
halves:** the verified entitlement and computed verdict, the in-memory mark and
its file, the cache path, the manager instance, its `asyncio.Task` and
`asyncio.Event` (both loop-bound — the `in_process_limiter()` gotcha exactly),
the `httpx.AsyncClient` (loop-bound; closed in the lifespan `finally`), the
consecutive-failure counter, the parsed manifest cache, and
`app.state.subscription_manager`. Rev 3 never mentioned conftest.

---

## 10. What the exchange exposes

The request body carries an **org id and a deployment id**. No query, row,
schema, principal, policy, credential, or connection identifier ever leaves.

The exchange additionally exposes what any HTTPS call exposes and we must say
so, because a reviewer who finds it after reading "nothing else" is a lost deal:
**source IP** (often the egress of the gateway fronting their production
database), **product version**, **refresh timing** — a daily liveness heartbeat,
so restart storms and shutdowns are visible to us — and **deployment count**.
The EULA must disclose this and state retention (§2).

Also: `querygate/licensing/client.py` opens an outbound connection *from the
gateway process*. §9's Reach reformulation must not split "data plane" from
"the same process's licence thread" — the network does not show that
distinction. Say "the gateway makes one outbound call, for licensing only,
carrying no data," or make the client a sidecar so the sentence is literally
true.

**Health disclosure:** unauthenticated `/health` carries **at most the existing
coarse `status`** — no org, plan, dates, `kid`, or failure counts. That endpoint
is deliberately unauthenticated and its own comment restricts it to aggregate
counts; a `grace_expires_at` there tells any scanner the exact date this
customer's gateway stops working. Detail goes behind a scope on the admin
observability router (registered in `SCOPE_CATALOG.md`) and in the CLI. The
`Warning` header attaches only to authenticated `/api/v1/*` responses — it is a
middleware concern and middleware runs on `/health` and `/metrics` too. The 402
body and the exception's `__str__` carry a **fixed** operator-facing string: no
org id, deployment id, serial, plan, or date.

---

## 11. Build phases

| Phase | Scope | Ships |
|---|---|---|
| **0 — Decisions & demolition** | Decision Log + NORTH_STAR amendments; replace `LICENSE`'s body; EULA clauses to counsel; rewrite `LICENSING_FAQ.md`; the demolition and guard work in §14; renumber from 200 | No product behaviour. Unblocks everything. |
| **1 — Verifier + client, shipped in `observe`** | `querygate/licensing/`: verification, `SubscriptionManager`, disk cache, wall-clock mark, `querygate-license status`, and **the gate call sites in both funnels** — running, evaluating, counting `would_block`, never raising | Safe on live deployments. The enforce path exists and is exercised. |
| **2 — Control plane core** | Domain model + migrations, KMS signing, `/v1/enrol` + `GET /v1/entitlement`, Stripe webhooks, append-only issuance ledger, admin console | A working entitlement factory. Enterprise deals sellable. |
| **3 — Enforcement on** | Issue `enforcement: enforce` per deployment, gradually. **A control-plane change, not a code change.** | Enforcement live, rollback-able per customer without shipping an image. |
| **4 — Self-serve** | Stripe Checkout, trial flow, lifecycle → entitlement | Self-serve funnel. Blocked on the legal entity. |
| **5 — Notary** | Item 198, as tenant #2 | Proof-pillar revenue. |
| **6 — Hardening** | Tenancy isolation, rate limits, backup/restore drill, pentest, escrow process | Survives the security review. |

Phase 1 before Phase 3 is load-bearing, and §3.6's signed field is what makes it
real: the machinery runs on real customers, in the artifact they actually run,
with the enforce path exercised rather than inserted on go-live day. Every
false-positive class — clock skew, cache corruption, a signature edge case —
surfaces as a counter before it can stop a query.

**Exit criterion for Phase 3:** N consecutive days across every observed
deployment with zero `would_block` events attributable to anything other than a
genuinely lapsed entitlement. Without a stated criterion, "observe first" is a
gesture.

**State written in Phase 1 is not authoritative in Phase 3.** The cache and the
clock mark carry a `state_version`; a Phase-3 binary reading a Phase-1 version
discards it and starts fresh. A mark written by an earlier build — including one
written during a clock-correction event or a since-fixed bug — must never become
a live expiry trigger.

---

## 12. Notary (item 198) — deferred, with one unresolved problem recorded

Notary stays tenant #2 and Phase 5. One design gap must be solved **before** it
is built, not after: an append-only log served solely by the control plane and
signed by a key the control plane holds is verifiable only for *internal
consistency*. Resisting a **split view** — a compromised plane showing different
histories to different parties — requires independent witnesses or mirrors. The
pitch says "verifiable by a third party without trusting the control plane";
nothing in the design delivers that yet. Its constraints are otherwise unchanged:
hash bytes only, asynchronous, never in the request path, operator-configured
URL, fixed cadence.

---

## 13. Open decisions

1. **KMS provider vs. algorithm** (§4) — Ed25519/GCP recommended; re-verify all
   three vendor facts on the day.
2. **Legal entity + insurance** — prerequisite for Stripe, the EULA's Licensor,
   and indemnification.
3. **Windows:** 30-day entitlement, 14-day grace, daily refresh, and the
   first-boot window (§3.1).
4. **Nuitka: decide whether to skip it** (§6) — the analysis now argues against.
5. **Source escrow** — Enterprise by default, or on request?
6. **Control-plane brand name.** Not "QueryGate Cloud" (implies hosted
   execution, still a non-goal).
7. **Trial length; card required at signup?**
8. **`MAX_LEASE_DAYS` / `MAX_OFFLINE_DAYS` / `CLOCK_REGRESSION_TOLERANCE` /
   `MAX_TRUSTED_MARK_AHEAD`** — the four compiled-in constants.

---

## 14. Everything that must change

**Legal.** Replace `LICENSE`'s BSL body with the proprietary notice (keep the
file). Add §2's eight clause groups to `EULA.{en,he}.md` — both languages; the
Hebrew is shorter and needs the same review. Rewrite `LICENSING_FAQ.md`: **~13
claims go false, not 2.** The highest-exposure one is **lines 158-161** — *"no
kill switch, no time bomb, and no check that can refuse to start or block a
query … it will never interrupt data access"* — a forward **promise** that is
the exact inverse of decision 5, and a different repair job from a statement of
fact. Also: the "no asterisk / free forever / no licence key" headline, the BSL
and Change-Date sections, the MSP carve-out, "the entire source is public,
readable, buildable, forkable", "BSL restricts resale, never capability:
everything is in the box" (contradicted by `features[]`), the `grep` self-check
invitation, "the paid tier adds… not software features", and the contribute/
open-an-issue answers. `LICENSE_NOTES.md`.

**Strategy.** `NORTH_STAR.md` Reach amendment. `PRODUCT_GUIDE.md` Decision Log.
`GTM_EXECUTION_PLAN.md` §2, **§3 Layer 2's anti-pattern list and §7's repeat**,
§6's flip sequence, and the **"Owner decisions — 2026-08-21" block**, which must
carry a superseded-by note. `LICENSE_ENFORCEMENT.md` — reverse the
soft-enforcement decision, un-reject models #2/#5, and correct **both** of its
wrong implementation recommendations: the per-request `StructuredQueryService`
placement *and* anchoring the clock mark "via the audit stream." Plus
`DISTRIBUTION_STRATEGY.md`, `MARKET_DOMINATION_ANALYSIS.md`,
`PRE_BSL_CLEANUP_PLAN.md`, `BSL_EXECUTION_PROMPT.md`, `GTM_EXECUTION_PROMPT.md`,
`COMPARISON_GOOGLE_MCP_TOOLBOX.md`'s licence row, `docs/README.md` rows 40/54/56,
`docs/CONTAINER_IMAGE_LICENCES.md` (a second copy of the phone-home claim),
`CONTRIBUTING.md` + `.github/cla/` (the CLA's entire rationale is outside
contributors to a public repo), `README.md`, `CUSTOMER_README.md`, `landing/`,
and `sales/index.html:476` — the objection script that tells a salesperson to
say "the source is public under BSL — read it."

**Code and gates.** Delete `make stamp-change-date` / `change-date-check` /
`change-date-check-release`, its `release-check` wiring, `scripts/check_change_date.py`,
`tests/unit/test_change_date.py`, and RELEASING.md's Change-Date sections — **and
the two CI call sites**, `ci.yml:34` (`make change-date-check`) and
`release.yml:69`, which invokes the script **directly**, so removing the Make
targets alone still breaks the tagged-release job. **Preserve two assertions
from the deleted test**: the pyproject↔dated-CHANGELOG binding (nothing else
asserts it) and the placeholder-refusal release gate, **retargeted at the EULA**,
which currently carries five unfilled `[BRACKETED]` placeholders and has **no
test, script, or CI job referencing it anywhere** — at the exact moment it
becomes load-bearing.

Narrow `test_no_phone_home.py` (§7 item 6) — and land it **before the first Phase-1
commit**, since it is `-m unit` and gates pre-commit on three assertions.
Regenerate `docs/product-guide.html` (`make product-guide-html`) and
`TRUST_EVIDENCE.md` (`make trust-page`) in the same commits as the doc edits —
both are freshness-gated in the unit suite. Update the adversarial-suite count in
**eight documents** pinned by `test_security_suite_count_claims.py`. Fix
`querygate license status` → `querygate-license status` here, in `TODO.md`, and
in GTM §3.

**Worklist.** Rewrite item 197's body (soft-warn/not-before-customers is
reversed); item 198 keeps its scope, moves to Phase 5. New items start at
**200** — 199 was allocated during this conversation. Add a `docs/README.md`
index row for this document, marked as a proposal, matching its two peers.

**Guards for `control-plane/`.** Source-level bidirectional import ban
(behavioural is impossible: `pythonpath = ["."]` makes the runtime import
succeed, so a stray cross-import passes the whole suite and CI, then
`ModuleNotFoundError`s in the wheel — a green-suite production crash found by a
customer). Frozen adversarial conformance vectors: **signed bytes in hex** so
canonicalisation is pinned, a checksum test so a format change must add a `v2`
section rather than rewrite `v1`, one negative vector per §3.5 rule asserting the
**specific** failure reason, an enum-exhaustiveness test
(`set(LicenseFailure) == {v.expected_reason for v in VECTORS}`), and a
**verifier that takes an injected `now` and `expected_deployment_id`** — without
those parameters the key-window and replay rules are structurally untestable.
Root-lockfile purity as **set disjointness**, not a hash (a hash reds on every
legitimate bump, gets rubber-stamped, then rubber-stamps the leak). Mirror CI
gates (`black`, `bandit`, semgrep, pip-audit) — dependency isolation otherwise
removes the component holding `sign` on the signing keys from **every**
supply-chain check. **Every source-level scanner asserts it walked a non-zero
file count** — the existing guard's `SOURCE_ROOT` can silently resolve to
nothing and pass with zero work done.

---

## 15. What changed in revision 4

Four reviewers audited rev 3 before any code existed. The defects that would
have shipped:

- **The enforcement did not stop writes.** Two write services bypass
  `StructuredQueryService` entirely, and the atomic batch bypasses even
  `WriteExecutionService.execute`. §3.2 now names two funnels and classifies
  every DB-touching path.
- **The 90-day key cap contained nothing**, because `issued_at` is chosen by
  whoever signs. §3.5 adds freshness and max-term bounds and a serial floor.
- **The clock mark was specified against a primitive that cannot be persisted.**
  `time.monotonic()` is boot-relative; as written every deployment expired on
  first restart. Now wall-clock, with tolerance, clamping, grace-not-expiry, and
  per-process scope.
- **The 402 would have been a 500**, and every block would have been written into
  the customer's WORM audit ledger as `db_error`. §3.3 names four registries and
  the batch re-raise.
- **Batch endpoints returned 200** with per-item errors, invisible to dunning.
- **The observe→enforce switch was unspecified**; every obvious representation
  was a bypass or a half-on state. §3.6 makes it a signed field on the
  `CostEstimationMode` precedent.
- **Multi-worker topology was never mentioned** in 432 lines, nor was
  `tests/conftest.py`.
- **`deployment_id` was presented as a cryptographic control.** §3.8 demotes it
  and adds the enrolment flow that was missing entirely.
- **Nuitka was recommended on an analysis that does not support it** (§6).
- **The doc inventory missed ~11 claims in LICENSING_FAQ, eleven surfaces, both
  CI call sites, and four gates that require `LICENSE` to exist.**
