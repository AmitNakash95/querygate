# Enforcing time-limited licenses on a self-hosted product

*A plain-language reference on how to make a leased/subscription license
actually expire when the product runs on the **customer's** infrastructure —
where you don't control the machine, the clock, or the network. Companion to
[docs/DISTRIBUTION_STRATEGY.md](DISTRIBUTION_STRATEGY.md).*

> ## ⚠️ The 2026-08-21 decision recorded here was REVERSED on 2026-08-23.
>
> **A previous banner said: "soft enforcement, always — no fail-closed mode
> exists, on any trigger, ever." That is now the opposite of the product.**
> It rested on the BSL Additional Use Grant giving unlimited free internal
> production use, which left no threshold for a licence to self-check. That
> grant was cancelled. QueryGate is a paid subscription
> ([`business/GTM_SAAS.md`](business/GTM_SAAS.md)), and after the paid term
> **plus** a grace window has fully elapsed, every governed query and write is
> refused with HTTP 402.
>
> **This document's original grace-period-then-fail-closed recommendation is
> therefore reinstated**, and its availability objection was answered rather
> than ignored — the design in
> [`CONTROL_PLANE_PLAN.md`](CONTROL_PLANE_PLAN.md) makes a *failed refresh*
> never fail-closed (only a server-confirmed non-renewal is, after ~45 days of
> visible warnings), fails open on a cold start with no network, and keeps
> health, metrics, licence status and audit retrieval working through a lapse
> (administrative and configuration functions that read live schema, test a
> connection, or change enforcement scope are suspended with the rest). Read
> that document for the posture actually adopted; read the survey below for the
> option space it was chosen from. Item 197 (the soft-warn entitlement token) is
> **superseded in premise** by items 210-213: the token now blocks rather than
> warns, and it is gating pre-launch work, not a not-before-customers nicety.

---

## TL;DR

- **Any license check on the customer's hardware is a speed bump, not a wall.**
  The goal isn't to beat a determined thief (that's the **contract's** job) —
  it's to make honest expiry visible and make bypassing it a deliberate,
  contract-breaching act.
- **Six models surveyed below:** (1) **signed offline license token** ⭐, (2)
  online activation + heartbeat, (3) floating license server, (4)
  node/environment binding, (5) hybrid crown-jewel-server-side (the only
  *airtight* one — and rejected for QueryGate, see below), (6) legal-only (the
  baseline under all of them).
- **Decided stack for QueryGate:** contract first (the licence/EULA) →
  **signed offline token** with an `expiry`, soft-warn only, and clock-tamper
  detection logged (never enforced by refusing to run). No heartbeat, no
  online activation, ever — see the anti-patterns below.
- **Key design call — fail mode, decided:** **soft enforcement only, with no
  exception.** An absent, expired, or unverifiable token produces a startup
  `WARN` log line, a field on the health endpoint, and a redaction-safe audit
  event — and **never** refuses to start and **never** blocks or degrades a
  query, on any trigger, including a definite signed expiry. There is no grace
  period because there is no fail-closed state for a grace period to lead
  into.
- **QueryGate already has the machinery:** a license token is just another
  signed JWT ([core/auth.py](../src/querygate/core/auth.py)), verified by one
  non-blocking check in front of `StructuredQueryService`, with clock-tamper
  detection anchored in the audit stream — no new architecture needed.

---

## The core reality (read this first)

> **Any license check that runs on hardware the customer controls is a speed
> bump, not a wall.** A determined customer can patch it out — and with Python
> source visible inside the image, the bar is lower than with a compiled
> binary.

This sounds discouraging, but it reframes the goal usefully. You are **not**
trying to stop a sophisticated, malicious adversary who has decided to steal —
that fight is unwinnable on their turf, and it's what the **legal contract**
(the EULA + a written term) is actually for. You are trying to:

1. **Make honest expiry automatic** — the 95% case is a normal customer whose
   term ends and who simply doesn't renew. A good license mechanism makes the
   product stop on its own, cleanly, so non-payment isn't rewarded by default
   and you don't have to chase it manually.
2. **Make circumvention a deliberate, contract-breaching act** — not something
   that happens by accident or convenience. Once bypassing the license requires
   *knowingly patching the software*, you're squarely in "breach of contract /
   willful infringement" territory, which is exactly where you want to be
   legally.

So: **technical enforcement handles the honest majority and raises the bar; the
contract handles the dishonest minority.** The only *airtight* technical
enforcement is to not ship the thing you're protecting at all (Model 5 / SaaS).

---

## The enforcement models

| # | Model | How expiry is enforced | Works air-gapped? | Strength vs. tampering | Fit for QueryGate |
|---|---|---|---|---|---|
| **1** | **Signed offline license token** ⭐ | A cryptographically signed file with an `expiry` date the product verifies at startup + periodically | ✅ Yes | Medium (can't forge a date; *can* patch the check) | **Decided default** (TODO.md item 197) |
| **2** | **Online activation + heartbeat** | Product phones home to your license server to confirm the subscription is active; degrades after a grace period if it can't | ❌ Needs egress | Medium-high (adds revocation) | **Rejected, permanently.** Phone-home for licensing contradicts "credentials and data never leave your network." |
| **3** | **Floating license server** | A separate license-manager component (yours or hosted) hands out time/seat-limited leases the app checks out | ⚠️ On-prem server | Medium | Not applicable — Shape A has no seat/instance ceiling to meter |
| **4** | **Node / environment binding** | License is locked to a machine fingerprint (hostname, cloud instance id, MAC) so a valid token can't be copied to other machines | ✅ Yes | Medium (anti-copy, not anti-patch) | Not applicable — nothing to bind against under an unlimited grant |
| **5** | **Hybrid: keep a crown-jewel server-side** | An essential capability lives in *your* infra; no subscription → you cut it off | ❌ By design | **High** — real teeth | **Rejected.** Would surrender Reach (self-hosted, data never leaves) — the real answer to "how do I meter this" is QueryGate Notary (TODO.md item 198), which anchors the audit ledger's head hash rather than gating execution |
| **6** | **Legal-only** | No technical check; the written term + audit rights govern | ✅ Yes | None technically | The load-bearing layer under Shape A (`docs/business/GTM_EXECUTION_PLAN.md` §3 Layer 1) |

### 1. Signed offline license token ⭐ (decided default — Ed25519, soft-warn only)
You issue the customer a signed license file — an Ed25519-signed token whose
payload carries `customer`, `issued_at`, `expiry`, and `tier/scope`. The
product ships with your **public key embedded** and verifies the token's
signature + expiry locally, at startup and on a timer, with **zero network
calls**.

- **Why it's the right (and only) model here:** it needs no network (works in
  air-gapped enterprise environments), the customer can't forge a later expiry
  without the private key, and renewal is just "issue a new token." It
  automates the honest non-renewal case *as visibility*, not as a gate — see
  the decided fail mode above.
- **Its limit:** the *check* runs client-side, so it can be patched out. That's
  the speed-bump reality above — acceptable, because it's paired with the
  contract, and because QueryGate never asks this check to do more than warn.

### 2. Online activation + heartbeat (phone-home) — **rejected, permanently**
The product periodically calls your license API to confirm the subscription is
still active, and degrades after a **grace period** if it can't confirm or is
told the license was revoked.

- **Would add:** real-time **revocation** and a signal when tampering/
  clock-fiddling is happening.
- **Why it's rejected outright, not merely deferred:** `docs/business/GTM_EXECUTION_PLAN.md`
  §3 lists "no phone-home telemetry — not for licensing, not for analytics" as
  an absolute anti-pattern. The product's pitch is that credentials and data
  never leave the customer's network; a licensing beacon originating inside
  that network contradicts the claim on its face, and a security reviewer will
  find it and ask about it. This is not a network-availability question
  ("only if customers allow egress") — it is off the table regardless of what
  a customer would permit.

### 3. Floating license server
A dedicated license-manager component (the classic enterprise pattern —
FlexNet, Sentinel RMS) that issues time- and seat-limited leases the app checks
out and renews. Strong for **counting concurrent seats/servers**. It's real
infrastructure to build and operate — **overkill until you're selling by the
seat and need to meter it.** Note it later.

### 4. Node / environment binding
Bind the license to a machine fingerprint so a single valid token can't be
copied across many deployments. Useful *modifier* on #1/#2 when your pricing is
per-server. Caveat: cloud/containerized environments have unstable fingerprints
(autoscaling, new instance ids), so bind loosely (e.g. to a customer-set
`deployment_id` you put in the token) or you'll generate support tickets.

### 5. Hybrid — keep a crown-jewel server-side (rejected)
The only way to make enforcement *actually* airtight on self-hosted is to make
the product genuinely depend on something only you can provide, so "stop paying
→ it stops working" isn't a check they can patch, it's a capability that's
simply gone. **Rejected for QueryGate**: any crown jewel gating execution
(config/policy/catalog distribution, a signed update feed the product needs
periodically) would make the product depend on a hosted component to serve a
query at all — exactly the availability dependency, and exactly the surrender
of self-hosted Reach, that the decided fail mode above exists to avoid. The
actual answer to "how do I attach revenue to something a customer can't
replace by self-hosting it" is **QueryGate Notary** (TODO.md item 198,
`docs/business/GTM_EXECUTION_PLAN.md` §4): it anchors the audit ledger's chain
head hash — asynchronously, never in the request path, no data plane
involvement — rather than gating the request path itself.

### 6. Legal-only (the baseline)
Even with zero technical checks, the EULA's **term + termination + audit-rights
clauses** make running past expiry a breach. For trusted enterprise B2B
customers this is often *sufficient* — reputable companies don't run unlicensed
software, because the liability of being caught dwarfs the license fee. Every
model above sits on top of this; none replaces it.

---

## The attacks you're actually defending against

Design against the realistic ones, in rough order of how likely they are:

| Attack | Likelihood | Defense |
|---|---|---|
| **Just not renewing** (honest lapse) | **Very high** | Signed token with `expiry` (#1) — the whole point |
| **Clock rollback** — set the system clock back to before expiry | Medium | Persist a **monotonic high-water mark** of the latest time the product has ever observed (in a state file / the audit stream); if the clock is now *before* it, emit the same soft `WARN` + health-endpoint + audit event as any other unverifiable-token case — never refuse to run. |
| **Copying one license to many deployments** | Medium | Node/deployment binding (#4) |
| **Patching out the license check** | Low, but possible | Can't be fully prevented client-side. Obfuscation/compilation (Nuitka) raises the bar; the **contract** is the real deterrent; #5 removes the check as a target entirely |

Don't over-invest against the low-likelihood, high-effort attacks at the cost
of shipping. The signed token + clock-tamper detection + contract covers the
vast majority of real-world risk.

---

## Fail-open vs. fail-closed (an important product call)

When the license is expired, missing, or unverifiable, what should the product
do?

- **Fail-closed** — refuse to serve requests. Strongest enforcement, but a
  false positive (clock skew, a renewal that arrived a day late, a customer
  who legitimately runs air-gapped) **bricks a paying customer** and burns
  goodwill + support time. **Rejected for QueryGate, unconditionally** — see
  below.
- **Fail-open with loud warnings** — keep serving but log/alert/emit warnings.
  Friendlier, weaker as a compliance lever. **This is the only mode QueryGate
  implements.**
- **Decided posture — soft enforcement only, no exception:** an absent,
  expired, or unverifiable token — including a **definite signed expiry
  date**, which an earlier draft of this document treated as "safe to
  fail-closed after a grace period" — produces exactly four things and nothing
  more: a startup `WARN` log line, a periodic `WARN` on the same timer the
  token is re-checked, a field on the health endpoint, and a redaction-safe
  audit event. **It never refuses to start and never blocks, delays, or
  degrades a query, on any trigger.** There is no grace period, because a
  grace period implies a fail-closed state waiting at the end of it, and none
  exists.

**Why "a certain fact, not an outage" is no longer the deciding line for
QueryGate.** The earlier reasoning was right that a definite expiry is
different in kind from an unreachable heartbeat — but the conclusion it drew
(safe to enforce) doesn't hold for a product that sits in the request path of
a customer's production database. QueryGate's own product identity is that it
never becomes an availability dependency the way a database-firewall-style
inspection layer would; a licence gate able to interrupt a query is exactly
that dependency, self-inflicted, and a security reviewer evaluating QueryGate
for a design partner would be right to flag it. The token's only job is
honest, self-reported visibility (to the operator, and to us via the audit
event) — never a lever we pull.

---

## What fits QueryGate specifically

The good news: **you already have most of the machinery.** A license module
would reuse existing patterns rather than introduce new ones:

- **Reuse the JWT/JWKS verification in [core/auth.py](../src/querygate/core/auth.py).**
  QueryGate already verifies signed JWTs. A **license token is just another
  signed JWT** — payload `{sub: customer, exp: <expiry>, scope: [...],
  deployment_id: ...}`, signed with your license private key, verified against a
  **public key pinned in the image**. This is architecturally consistent and
  low-risk to build.
- **Model it as a composable interface** per the repo's own convention (the
  `Authenticator` / `DialectAdapter` / `AuditSink` pattern in CLAUDE.md): a
  narrow `LicenseValidator` protocol with `SignedTokenLicenseValidator` (#1)
  as its only planned concrete variant. **No heartbeat variant is planned or
  wanted** — see the anti-patterns below; there is nothing for a
  `HeartbeatLicenseValidator` to compose with, since QueryGate makes zero
  network calls for licensing, ever.
- **Check at startup + on a timer**, non-blockingly. The cleanest place to run
  the check is alongside `StructuredQueryService` (the single path to a
  database — CLAUDE.md) so the same startup/audit machinery sees every
  request, but the check **never gates** that path — an expired or missing
  license changes only the health-endpoint field and the audit trail, never
  whether a query runs.
- **Clock-tamper detection via the audit stream.** The audit log
  ([audit/sinks.py](../src/querygate/audit/sinks.py)) already persists ordered,
  redaction-safe events — a natural place to anchor the monotonic time
  high-water mark, so a clock rollback is detectable without new storage.
- **Config-file delivery.** The license file fits the existing file-configured
  model (like connections/policy YAML) — the operator drops in a
  `license.jwt` and points the config at it; hot-reload could pick up a renewed
  token without a restart.

> ⚠️ **Do not weaken the security invariants to add this.** A license token must
> never carry or expose a credential, must never open a second path to the
> database, and its verification must be redaction-safe in the audit stream —
> the same rules the rest of the pipeline follows. If we build it, it goes
> through the normal review (`security-invariant-check`).

---

## Recommendation

**Decided, in this order:**

1. **The contract is the load-bearing layer** (#6) — the licence + EULA term.
   Under Shape A, revenue comes from the enterprise tier (indemnification +
   support + security packet + Notary), not from a technical gate, so this
   layer is doing more work than it would under a capped grant.
2. **Add a signed offline license token** (#1, TODO.md item 197) with
   `expiry`, reusing the existing JWT machinery, **soft-warn only**, and
   clock-tamper detection that logs rather than enforces. Not before a
   customer needs it; ships alongside item 198.
3. **No heartbeat, ever** (#2) — a permanent rejection, not a "for now."
4. **No hybrid/SaaS crown jewel** (#5) — a permanent rejection; **QueryGate
   Notary** (item 198) is the actual answer to "what's the unpirateable
   thing," and it anchors the audit ledger rather than gating execution.

The rejections of #2 and #5 are permanent design positions, not scheduling —
neither is being deferred, and neither is on the roadmap under any name. Item
2 (the signed token, TODO.md item 197) and item 4's Notary reference (TODO.md
item 198) *are* scheduling decisions: **not-before-customers**, meaning build
when a paying customer needs them, not before — but even then, always
soft-warn/asynchronous, never fail-closed or in the request path. That
constraint does not lift with time or customer pressure; changing it would
need a NORTH_STAR-level decision per CLAUDE.md non-negotiable #8.

---

## Open decisions — closed

Every open decision this document originally posed is now settled by the
owner (2026-08-21, `docs/business/GTM_EXECUTION_PLAN.md` §2.1/§3):

- ~~Enforcement posture — token-only or token + heartbeat?~~ **Token-only,
  permanently.** No heartbeat variant will ever be built.
- ~~Fail mode + grace period length?~~ **Soft-warn only, unconditionally — no
  grace period, because there is no fail-closed state at the end of one.**
- ~~Per-what pricing (drives node binding / a floating server)?~~ **Moot** —
  Shape A has no production ceiling to meter; revenue is the enterprise
  bundle (§3 Layer 3), not the licence.
- ~~Build vs. defer?~~ **Defer.** Logged as TODO.md item 197, explicitly
  not-before-customers; do not implement until a paying customer needs it.
