# Enforcing time-limited licenses on a self-hosted product

*A plain-language reference on how to make a leased/subscription license
actually expire when the product runs on the **customer's** infrastructure —
where you don't control the machine, the clock, or the network. Companion to
[docs/DISTRIBUTION_STRATEGY.md](DISTRIBUTION_STRATEGY.md).*

---

## TL;DR

- **Any license check on the customer's hardware is a speed bump, not a wall.**
  The goal isn't to beat a determined thief (that's the **contract's** job) —
  it's to make honest expiry automatic and make bypassing it a deliberate,
  contract-breaching act.
- **Six models:** (1) **signed offline license token** ⭐, (2) online
  activation + heartbeat, (3) floating license server, (4) node/environment
  binding, (5) hybrid crown-jewel-server-side (the only *airtight* one), (6)
  legal-only (the baseline under all of them).
- **Recommended stack for you, in order:** contract first (you have the EULA) →
  **signed offline token** with an `expiry`, a grace period, and clock-tamper
  detection → add a heartbeat later *only* if customers allow outbound network.
  Skip the heavy stuff (#3/#4/#5) for a first pilot.
- **Key design call — fail mode:** enforce a *definite signed expiry date*
  (safe to fail-closed after a grace period), but **never brick a paying
  customer just because you couldn't reach them right now** (fail-open when the
  check is merely *unreachable*, act only on an affirmative "revoked").
- **QueryGate already has the machinery:** a license token is just another
  signed JWT ([core/auth.py](../src/querygate/core/auth.py)), enforced by one
  guard in front of `StructuredQueryService`, with clock-tamper detection
  anchored in the audit stream — no new architecture needed.

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

| # | Model | How expiry is enforced | Works air-gapped? | Strength vs. tampering | Fit for you |
|---|---|---|---|---|---|
| **1** | **Signed offline license token** ⭐ | A cryptographically signed file with an `expiry` date the product verifies at startup + periodically | ✅ Yes | Medium (can't forge a date; *can* patch the check) | **Best default** |
| **2** | **Online activation + heartbeat** | Product phones home to your license server to confirm the subscription is active; degrades after a grace period if it can't | ❌ Needs egress | Medium-high (adds revocation) | Great add-on to #1 |
| **3** | **Floating license server** | A separate license-manager component (yours or hosted) hands out time/seat-limited leases the app checks out | ⚠️ On-prem server | Medium | Overkill until you sell seats |
| **4** | **Node / environment binding** | License is locked to a machine fingerprint (hostname, cloud instance id, MAC) so a valid token can't be copied to other machines | ✅ Yes | Medium (anti-copy, not anti-patch) | A *modifier* on #1/#2 |
| **5** | **Hybrid: keep a crown-jewel server-side** | An essential capability lives in *your* infra; no subscription → you cut it off | ❌ By design | **High** — real teeth | The airtight option |
| **6** | **Legal-only** | No technical check; the written term + audit rights govern | ✅ Yes | None technically | The baseline under all of the above |

### 1. Signed offline license token ⭐ (recommended default)
You issue the customer a signed license file — think a JWT-style token whose
payload carries `customer`, `issued_at`, `expiry`, and `scope/features`, signed
with **your private key**. The product ships with your **public key embedded**
and verifies the token's signature + expiry at startup and on a timer.

- **Why it's the right default:** it needs no network (works in air-gapped
  enterprise environments — a common on-prem requirement), the customer can't
  forge a later expiry without your private key, and renewal is just "issue a
  new token." It cleanly automates the honest non-renewal case.
- **Its limit:** the *check* runs client-side, so it can be patched out. That's
  the speed-bump reality above — acceptable, because it's paired with the
  contract.

### 2. Online activation + heartbeat (phone-home)
The product periodically calls your license API to confirm the subscription is
still active, and degrades after a **grace period** if it can't confirm or is
told the license was revoked.

- **Adds:** real-time **revocation** (you can kill a license mid-term for
  non-payment or breach — the offline token can't do this until it expires),
  and a signal when tampering/clock-fiddling is happening.
- **Costs:** requires **outbound network access** from the customer's
  environment, which secure/air-gapped customers often forbid; your license
  server becomes a dependency whose downtime must not brick paying customers
  (hence the grace period). Raises data-privacy questions you must document.
- **Best used as a layer on top of #1**, not instead of it: offline token is
  the source of truth; the heartbeat adds revocation *when reachable*.

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

### 5. Hybrid — keep a crown-jewel server-side (the airtight option)
The only way to make enforcement *actually* airtight on self-hosted is to make
the product genuinely depend on something only you can provide, so "stop paying
→ it stops working" isn't a check they can patch, it's a capability that's
simply gone. For QueryGate, candidate crown jewels could be a **control plane**
(config/policy/catalog distribution) or **signed update/catalog feeds** the
product needs periodically. This shades toward a partial-SaaS model and is a
product decision, not just an enforcement one — but it's the honest answer to
"how do I make it truly enforceable."

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
| **Clock rollback** — set the system clock back to before expiry | Medium | Persist a **monotonic high-water mark** of the latest time the product has ever observed (in a state file / the audit stream); refuse to run if the clock is now *before* it. Optionally trust signed time from a heartbeat (#2). |
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
  false positive (clock skew, a renewal that arrived a day late, your heartbeat
  server having an outage) **bricks a paying customer** and burns goodwill +
  support time.
- **Fail-open with loud warnings** — keep serving but log/alert/emit warnings.
  Friendlier, weaker.
- **Recommended middle path:**
  - On a **definite expiry date from a validly-signed token** → **grace period**
    (e.g. 7–14 days of degraded-but-working with escalating warnings), then
    fail-closed. This is a certain fact, not an outage, so enforcing it is safe.
  - On an **inability to reach the heartbeat** (#2) → **fail-open** within the
    grace window (never punish a customer for *your* server's downtime or their
    firewall); only act on an *affirmative* "revoked/expired" response.

Distinguishing "the token itself says expired" (safe to enforce) from "I
couldn't check right now" (don't punish) is the single most important design
decision here.

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
  narrow `LicenseValidator` protocol with concrete variants — e.g.
  `SignedTokenLicenseValidator` (#1) and, later, `HeartbeatLicenseValidator`
  (#2) that can be **composed** the same way `CompositeAuthenticator` chains
  authenticators. No `if mode == ...` branching.
- **Check at startup + on a timer**, and gate the request pipeline. The
  cleanest enforcement point is one guard in front of
  `StructuredQueryService` (the single path to a database — CLAUDE.md), so an
  expired license fails *every* query uniformly, with no second code path.
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

**Layered, in this order:**

1. **Ship the contract first** (#6) — the EULA term + termination clause. This
   is the load-bearing layer and you already have the EULA drafted.
2. **Add a signed offline license token** (#1) with `expiry`, reusing the
   existing JWT machinery, a grace period, and clock-tamper detection. This
   automates the honest non-renewal case, works air-gapped, and is a natural
   fit for the codebase.
3. **Optionally add a heartbeat** (#2) later for real-time revocation — *only*
   if your customers permit outbound network access, and always fail-open on
   unreachable-vs-fail-closed on affirmatively-revoked.
4. **Reach for hybrid/SaaS** (#5) only if a customer's value-at-risk is high
   enough to justify keeping a crown jewel off their machine.

Skip #3/#4 for a first pilot — token + contract is the right-sized starting
point.

---

## Open decisions (yours to make)

- [ ] **Enforcement posture** — token-only (#1) for the first customer, or
      token + heartbeat (#2)? (Depends on whether customers allow egress.)
- [ ] **Fail mode + grace period length** — how many days of degraded operation
      after a token expires before hard fail-closed?
- [ ] **Per-what pricing** — per-deployment (drives node binding #4), per-seat
      (drives a floating server #3, later), or per-customer flat?
- [ ] **Build vs. defer** — implement the `SignedTokenLicenseValidator` now, or
      keep licensing legal-only (#6) for the very first pilot and add the token
      before customer #2?

*If you decide to build #1, say so and I can scope it as a TODO item and
implement it against the existing auth/audit machinery — including the key
generation, the token-issuing CLI, and the pipeline guard.*
