# Distributing QueryGate to a customer — a decision guide

*A plain-language reference to re-read while deciding how you want to ship
QueryGate. It captures the delivery options and their honest trade-offs.*

> **Read this first — the framing below is superseded, and kept for reference
> only.** This document was written to answer *"how do I keep the source from
> being exposed?"*. Under the intended Business Source License 1.1 that
> question is closed: the source is **published on purpose**. The whole first
> half — the three delivery models compared on "source exposure", and the
> obfuscation survey — survives only as a record of why source-hiding was
> abandoned, not as a live decision.
>
> The questions that actually matter now are different ones:
>
> | Old question | The question that replaced it |
> |---|---|
> | How do I stop the customer reading the code? | Nothing does; the licence is published so they can. |
> | Which delivery model hides the source best? | Which delivery model is **easiest to adopt**? (Container image from GHCR, wheel from PyPI — see [`RELEASING.md`](RELEASING.md).) |
> | How do I make a licence check bite? | It deliberately does not — enforcement is **soft, always**. See [`LICENSE_ENFORCEMENT.md`](LICENSE_ENFORCEMENT.md)'s superseding banner. |
> | What does the licence forbid? | Only offering QueryGate *itself* as a hosted/managed service. Internal production use is unlimited. See [`LICENSING_FAQ.md`](LICENSING_FAQ.md). |
> | How does a customer know the artifact is genuine? | Cosign keyless signature + SLSA provenance, both consumer-verifiable — see [`RELEASING.md`](RELEASING.md). |
>
> **A separately negotiated commercial agreement is still a real thing** — for
> support, indemnification, and a paid tier — and the "Do you need a license
> agreement?" section below is still current on that point. It is an *addition*
> to the public licence, never a replacement for it. Nothing here is legal
> advice, and [`../LICENSE`](../LICENSE) is a draft that is not yet in force.

---

## The core reality (read this first)

> **Python source cannot be meaningfully hidden from someone who controls the
> machine it runs on.**

`.pyc` bytecode decompiles trivially. Even compiled-to-native tools (Nuitka,
Cython) can be reverse-engineered with effort. If a customer can *run* the
software, a determined customer can *read* it.

So the real question is **not** "how do I make the code unreadable?" It's
**"which delivery model matches how much I actually care about exposure?"**
There are three, and the thing that actually protects you in the on-prem cases
is a **license agreement** (see the last section), not obfuscation.

---

## The three delivery models

| Model | What the customer receives | Source exposure | Who operates it | Effort for you |
|---|---|---|---|---|
| **1. SaaS / hosted** | An API endpoint + credentials | **None** — code never leaves your infrastructure | You | You run and scale it |
| **2. On-prem Docker image** ⭐ | A versioned container image pulled from your registry (or a `docker save` tarball for air-gapped sites) | Low-friction, but *technically* extractable from the image | The customer | **You're basically ready today** |
| **3. Source / wheel install** | The `pip`-installable package | Fully visible | The customer | Already possible, least protection |

### Model 1 — SaaS / hosted
**The only airtight answer if source must *never* be reachable.** You run
QueryGate yourself; the customer only ever gets API/MCP access. Because
QueryGate is a *gateway that sits in front of databases*, the open question is
network access: your hosted instance needs to reach the customer's databases,
which usually means a VPN/peering/tunnel arrangement. On-prem sidesteps that
entirely.

### Model 2 — On-prem Docker image ⭐ (recommended for a first pilot)
This is what enterprise on-prem software does. The customer pulls a versioned,
signed image from your registry, drops in their own
`connections.yaml` / `policy.yaml` / `.env`, and runs it with either the
Compose stack or the Helm chart you already ship in [`deploy/`](../deploy/).
They never see a git repo. The source *is* inside the image, but the delivery
model is "an opaque artifact you run," and the **license/contract** is what
legally prevents reverse-engineering.

### Model 3 — Source / wheel install
`make release-check` already builds a wheel + sdist. Fine for internal use or a
highly trusted partner; offers essentially no protection against reading the
code. Not recommended for arm's-length customers.

---

## What you already have (more than you'd think)

You are **~80% of the way to Model 2** already:

- **Hardened production image** — [`Dockerfile`](../Dockerfile): multi-stage,
  runs as a non-root `querygate` user, build tooling and CVE-bearing packages
  stripped out of the runtime.
- **Two ways for a customer to run it** — [`deploy/`](../deploy/) has a
  production Docker Compose stack **and** a Helm chart (for Kubernetes).
- **Release gates** — `make release-check` (lockfile, metadata, formatting,
  tests, config validation, wheel/sdist build, artifact inspection, SBOM +
  dependency vulnerability audit) and `make release-smoke` (builds the image
  and runs a real structured query against Postgres). See
  [`docs/RELEASING.md`](RELEASING.md).
- **Supply-chain evidence** — a CycloneDX SBOM, a deny-by-default `pip-audit`
  gate, and `dist/SHA256SUMS` so a downloaded artifact set can be verified.
- **Security scanning in CI** — Trivy (image), gitleaks (secrets), Bandit +
  Semgrep (SAST), Schemathesis (DAST).

### The gap this work closes
CI only ran on push to `main` and **nothing published a release image** — tags
and artifacts stayed local (RELEASING.md says exactly this). The new
[`.github/workflows/release.yml`](../.github/workflows/release.yml) is the
missing CD half.

---

## The release flow, end to end

**Tags, not a `release/` branch, are the trigger.** That matches
[`docs/RELEASING.md`](RELEASING.md) already. A `release/x.y` branch only earns
its keep later, when you must ship hotfixes to an *old* version while `main`
moves ahead — you don't need that with one active version yet.

```
1. Bump the version in pyproject.toml and src/querygate/__init__.py
2. Add the changes to CHANGELOG.md
3. make release-check     # deterministic source/package gate
4. make release-smoke     # container + real-Postgres gate
5. git tag -a v0.1.0 -m "QueryGate v0.1.0"
6. git push origin v0.1.0
        │
        └─▶ .github/workflows/release.yml fires automatically:
              a. verifies the tag matches the package version
              b. builds the production image
              c. Trivy-scans it  ── fails the release if a HIGH/CRITICAL slips in
              d. pushes to ghcr.io/agitmit/querygate:0.1.0  (+ :latest)
              e. signs it with cosign (keyless / Sigstore) — no key to manage
```

The customer then pulls and runs it:

```bash
# The customer's machine
docker pull ghcr.io/agitmit/querygate:0.1.0

# (optional) verify it genuinely came from your release pipeline
cosign verify ghcr.io/agitmit/querygate:0.1.0 \
  --certificate-identity-regexp 'https://github.com/AGitmit/QueryGate/.github/workflows/release.yml@.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

# then run it with their own config via the deploy/ Compose stack or Helm chart
```

**Registry choice:** the workflow uses **GHCR** (GitHub Container Registry) —
free, no extra account, and it inherits your repo's identity for keyless
signing. For a private customer image, keep the GHCR package **private** and
grant the customer a read-only pull token, **or** hand them a
`docker save`-ed tarball for fully air-gapped installs. Switching to another
registry (Docker Hub, AWS ECR, Azure ACR) later is a one-line change to the
`IMAGE` env in the workflow plus the login step.

---

## What is a "license agreement"?

A **license agreement** (often an EULA — End User License Agreement, or a
commercial software license) is a **legal contract** between you (the vendor)
and the customer that says what they are and aren't allowed to do with the
software you hand them. It is the layer that actually protects you when the
code physically sits on someone else's machine — because, as noted at the top,
technical obfuscation can't.

A typical commercial license for on-prem software grants a **limited right to
use** and explicitly **forbids**:

- **Reverse-engineering, decompiling, or disassembling** the software.
- **Copying, redistributing, or reselling** it.
- **Modifying** it or creating derivative works.
- Using it beyond the **agreed scope** (e.g. number of seats/servers, a time
  limit, specific environments).

…and it usually also covers **ownership** (you keep all IP — they get a license,
not the software itself), **warranty/liability limits**, **support/update
terms**, and **termination** (what happens when the contract ends).

**Why it matters here:** with the Docker-image model, the source is reachable
by a truly determined party. The license is what makes doing so a **breach of
contract** you could act on — it turns "technically possible" into "legally
prohibited, with consequences." Enterprise software runs on exactly this
combination: a reasonable technical barrier (an opaque, signed image) plus a
contract that forbids crossing it.

Practically, you have a few options, roughly in order of cost:

1. **A LICENSE file + short commercial terms** in the delivered artifact
   (a `LICENSE`/`EULA.txt` the image ships with, or terms the customer accepts
   at download).
2. **A proper commercial license agreement / MSA** (Master Services
   Agreement) signed per customer — standard for B2B deals.
3. **A lawyer-drafted EULA** once real money or regulated data is involved.

> ⚠️ **This is general guidance, not legal advice.** Before a paid customer,
> have an actual lawyer draft or review the agreement — it's inexpensive
> relative to what it protects, and the wording (especially the
> reverse-engineering and liability clauses) is what makes it enforceable.

Note this is *separate* from the public-licence question. The repo's top-level
[`LICENSE`](../LICENSE) now holds a **draft** of the intended Business Source
License 1.1 terms, behind a banner saying it is not yet in force; until that
banner is removed, QueryGate remains proprietary and all rights are reserved.
See [`LICENSING_FAQ.md`](LICENSING_FAQ.md) for what the intended grant means in
practice and [`LICENSE_NOTES.md`](LICENSE_NOTES.md) for the drafting notes
prepared for counsel. Whichever grant is in force at a given moment, a
deliberate *commercial* agreement (the numbered options above) is a separate
document you would still attach to a paid customer delivery — it buys support
and indemnification, which no public licence provides.

---

## Recommendation, in one line

**For a first customer pilot: ship the signed on-prem Docker image (Model 2) —
you're ready — and pair it with a written commercial license that forbids
reverse-engineering and redistribution.** Revisit **SaaS (Model 1)** if you
later decide the source must be *provably* unreachable and you're willing to
operate the service and solve the database-network-access question.

---

## Open decision (still yours to make)

- [ ] **Which model?** SaaS vs. on-prem image (the `release.yml` work supports
      the on-prem path today and is a prerequisite for it either way).
- [ ] **Registry + visibility** — GHCR private with pull tokens, vs. air-gapped
      tarball hand-off.
- [ ] **License** — start with a LICENSE/EULA file, or go straight to a
      lawyer-reviewed commercial agreement.
