# Security Policy

QueryGate is a security product: an agent-safe database access gateway whose
entire value is that an AI agent can never submit raw SQL, never see a table or
column its policy forbids, and never take the database down. We treat security
reports with corresponding seriousness.

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Report privately to the maintainers:

- Email: **security@querygate.invalid** (subject line prefixed `[QueryGate Security]`)
- Or, if you have repository access, open a
  [GitHub private security advisory](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability).

> Maintainer note: replace the address above with a dedicated
> `security@<your-domain>` alias before wider distribution.

Please include:

- A description of the issue and the impact you believe it has.
- Steps to reproduce (a minimal `StructuredQuery` AST, request, or config that
  triggers it is ideal).
- The QueryGate version / image digest, dialect (Postgres or MSSQL), and any
  relevant policy configuration.

### What to expect

- **Acknowledgement** within 3 business days.
- An initial **assessment and severity triage** within 10 business days.
- Coordinated disclosure: we will agree a disclosure timeline with you and
  credit you (if you wish) once a fix is available.

## Scope

In scope — anything that undermines a core guarantee:

- A path that lets a caller submit or cause execution of raw SQL (bypassing the
  `StructuredQuery` AST).
- A policy bypass: reading a table/column, exceeding a complexity/row cap, or
  reaching a connection the caller's policy forbids.
- Credential or connection-string exposure through any REST/MCP response, log,
  audit event, or error message.
- A denial-of-service that evades the concurrency/timeout/cost guardrails.
- Any leak of another tenant's/connection's data or catalog history.

Out of scope:

- Vulnerabilities in a customer's own database, network, or reverse proxy.
- Findings that require the attacker to already hold valid admin credentials
  and the intended admin scope for the action.
- Denial-of-service by sheer request volume against an unprotected deployment
  (deploy behind the documented rate limits and auth).

## Recognition and reward structure

QueryGate runs a **coordinated-disclosure program with public recognition**,
sized to the project's current stage:

- **Recognition, not cash (today).** Valid, in-scope reports earn public credit
  in the release notes / a `SECURITY-HALL-OF-FAME` acknowledgement (with your
  consent) and coordinated-disclosure handling. There is **no monetary bounty at
  this stage** — a deliberate decision, not an oversight.
- **Why staged this way.** A paid bug-bounty program is only stood up *after* an
  initial independent third-party audit (TODO.md item 53) has cleared the obvious
  issues — paying for findings a scheduled audit would have caught is poor use of
  a bounty, and an unaudited surface invites noise. Until then, coordinated
  disclosure + recognition is the stage-appropriate structure.
- **Escalation path.** When item 53's audit completes and the surface is
  hardened, this section is the single place the reward structure changes
  (e.g. a hosted program with monetary tiers). The reporting channel, scope, and
  remediation process below do **not** change when that happens.

## How we handle a report (remediation process)

Every report — from a researcher here, an internal adversarial-suite finding, or
an external audit (item 53) — flows through the **same** path, so nothing is
triaged twice or lost:

1. **Acknowledge & triage** — confirm receipt, reproduce, and assign a severity
   (impact × exploitability against the core guarantees above).
2. **Regression-lock first.** Before or alongside the fix, the issue is captured
   as a failing test in the adversarial security suite (`tests/security/`) so the
   exact vector can never silently reopen — the same bar every shipped guardrail
   is held to (`make test-security`).
3. **Fix & gate** — remediate, then pass the full release gates
   (`make release-check`, and `release-smoke` when the change touches
   packaging/DB execution) before release.
4. **Release & disclose** — ship the fix in a new versioned image and agree a
   coordinated public-disclosure timeline with the reporter, crediting them if
   they wish.

## Supported versions

QueryGate is distributed as a versioned container image. Security fixes are
issued against the **latest released image**; customers are expected to track
current releases. If you run an older pinned digest, note the fix will land in a
new release rather than a backport unless otherwise agreed.

## How we back these guarantees

The claims above are enforced by code and tested in CI, not asserted by
convention. See [docs/SECURITY_POSTURE.md](docs/SECURITY_POSTURE.md) for the
full, verifiable security posture (SAST, dependency/SBOM audit, container and
secret scanning, OpenAPI fuzzing, the adversarial test suite, and the threat
model), and [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) for the threat-by-threat
control and test mapping.
