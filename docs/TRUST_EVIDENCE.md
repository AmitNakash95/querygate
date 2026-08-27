<!-- GENERATED FILE — do not hand-edit. Regenerate with `make trust-page` (scripts/generate_trust_page.py). -->
# QueryGate — Trust & Evidence Packet

*Generated 2026-08-27 for QueryGate 0.1.0. Composed, read-only, from the checked-in docs and dependency-audit results below (TODO.md item 147) — this file asserts no claim of its own; every statement here is backed by the cited source doc and, where named, a reproducible `make` command. It does not imply any control, certification, or third-party attestation that isn't explicitly stated in a source doc.*

## Current dependency audit status

**1 reviewed allowlist entries** (deny-by-default — every other known finding fails the release gate):

- `PYSEC-2026-286` (asyncmy) — SQL injection (CVE-2025-65896, GHSA-qhqw-rrw9-25rm, CVSS 9.8) in asyncmy's converters.pyx escape_dict(): only dict VALUES are escaped, not dict KEYS, so a caller that passes cursor.execute(query, {attacker_controlled_key: value}) with a pyformat/dict-shaped parameter mapping can inject via the key. No fixed version exists yet upstream. Measured, not assumed (2026-08-06, TODO.md item 19): QueryGate never calls asyncmy's cursor directly — every MySQL query goes through SQLAlchemy Core via the async engine. SQLAlchemy's DBAPI execution layer shapes the parameters it hands a cursor according to the dialect's declared `paramstyle`; the mysql+asyncmy dialect declares 'format' (positional %s), which SQLAlchemy always passes as a tuple/sequence, never a dict — regardless of whether the query is built via sa.text() or Core Select. escape_dict()'s vulnerable dict-KEYS codepath is only reached under a dict-shaped paramstyle (pyformat/named), so it is never reached by this dialect's usage pattern. Guarded going forward by a static regression test (tests/unit/test_dialect_adapters.py::TestMySQLAsyncmyParamstyleStaysPositional) asserting the dialect's declared paramstyle stays out of the dict-shaped set — it fails loudly if a future SQLAlchemy release ever changes that, which is when this entry needs re-review, not a live-server trace on every audit pass. (tracking: TODO.md item 19 phase 1; re-review if asyncmy's paramstyle ever changes or a raw cursor call is ever introduced, and re-check on every dep-audit pass for an upstream fix version.)

## Security & reliability posture

*Source: [`docs/SECURITY_POSTURE.md`](SECURITY_POSTURE.md).*

# QueryGate — Security & Reliability Posture

*A verifiable summary of how QueryGate is tested and hardened, for security
teams and prospective buyers evaluating the product.*

QueryGate is an **agent-safe database access gateway**: it exposes Postgres and
MSSQL to AI agents over MCP and REST, but the only thing a caller can ever submit
is a validated `StructuredQuery` JSON AST — there is no raw-SQL field or endpoint
anywhere in the product. It runs **inside your own infrastructure**; your data
and credentials never leave your network.

Every claim below is backed by code and by a check that runs in continuous
integration on every change — not by assertion. Each row names the command you
can run to reproduce it yourself. Because these gates run in CI, a regression
that weakened any of them would fail the build.

> **Repository note.** QueryGate's source is private (it ships as a distributed
> container image). The badges here are therefore a **status table backed by
> real CI gates and reproducible commands**, not public shields — a security
> reviewer under NDA can run every command in a checkout and see the same
> result CI does.

## At a glance

| Area | Gate | Tooling (open-source, industry-standard) | Status | Reproduce |
|---|---|---|---|---|
| **Core guarantee** | No raw-SQL path; validated-AST-only | Structured AST + Pydantic `forbid`; enforced by tests | ✅ Enforced | `make test-security` |
| **SAST** | Static security analysis of source | **Bandit** + **Semgrep OSS** (`p/python`, `p/security-audit`, `p/owasp-top-ten`) | ✅ Clean (deny-by-default) | `make sast` + `make semgrep` |
| **Dependencies** | Known-CVE audit of the exact shipped set | **pip-audit** against `poetry.lock` `main` group | ✅ Clean — **1 reviewed allowlist entry** (asyncmy, unreachable codepath) | `make sbom` |
| **SBOM** | Software bill of materials | **CycloneDX** | ✅ Generated per release | `make sbom` |
| **Licences** | Third-party licence inventory of the locked **Python** packages ([report](THIRD_PARTY_LICENSES.md)) | Deny-by-default tier policy + individually recorded exceptions | 🟡 No GPL/AGPL among locked Python packages, in either group; **1 weak-copyleft (MPL-2.0 `certifi`) in the redistributed set, review pending**. Python packages only — the image's Debian/`msodbcsql18` layers are unassessed (TODO item 196) | `make license-check` |
| **Container image** | OS + library CVEs, secrets, misconfig | **Trivy** on the shipped image | ✅ **0 HIGH/CRITICAL** (no exceptions) | `make scan-image` |
| **Secrets** | No credential ever committed | **gitleaks** over full git history | ✅ Clean | `make scan-secrets` |
| **DAST** | Fuzz the API for validation bypass / crashes | **Schemathesis** against the live OpenAPI schema | ✅ 0 server errors, 0 bypass | `make test-dast` |
| **Adversarial suite** | Known bypass classes as regressions | Purpose-built pytest suite (`-m security`) | ✅ 656 tests (655 run, 1 skipped pending item 211) | `make test-security` |
| **Reliability** | Guardrails hold under real concurrent load | Real-Postgres soak/load gates | ✅ Enforced in CI | `make test-load` / `make test-soak` |
| **Credential isolation** | No secret on any returned model | Asserted against live OpenAPI + MCP schemas | ✅ Enforced | `pytest tests/unit/test_credential_redaction.py` |
| **Best-practices self-assessment** | OpenSSF criteria maturity | **OpenSSF Best Practices** criteria (self-assessed) | 🟡 Self-assessed | see [below](#external-attestations) |

Legend: ✅ implemented and gating CI · 🟡 self-assessed / not yet an external
award, **or** gating but with an open finding recorded in the row.

---

## The core guarantee: no raw SQL, ever

QueryGate's central security property is structural, not behavioral: there is no
code path — REST or MCP — that accepts a SQL string. A caller submits a
`StructuredQuery` AST; Pydantic rejects any unknown field (`extra="forbid"`);
identifiers resolve to reflected SQLAlchemy objects; and predicate values are
always bound parameters, never concatenated into SQL.

- **Pipeline:** policy validation → schema validation → SQLAlchemy Core
  compilation → concurrency-guarded execution → redaction-safe audit
  (see the README "Architecture overview").
- **Proof:** `test_raw_sql_rejected_on_every_query_endpoint`,
  `test_predicate_payload_is_bound_data_not_executable_sql`, and the malformed
  input fuzzer `tests/security/test_malformed_input_fuzzing.py` (item 36 phase
  2a), which proves a smuggled `sql`/`raw_sql`/`query` field — and every other
  malformed shape — is rejected *before* execution.

Maps to threat **QG-01** in [THREAT_MODEL.md](THREAT_MODEL.md).

## Static analysis (SAST)

Two complementary open-source static analyzers run on every push:

- **Bandit** — Python-specific security linter, scanning the shipped source
  (`src/`). Configuration lives in `pyproject.toml` (`[tool.bandit]`). The gate
  is **deny-by-default**: any finding fails CI. The handful of accepted findings
  are annotated inline with `# nosec <id>` and a written justification (e.g. the
  intentional in-container `0.0.0.0` bind) — reviewed, never blanket-suppressed.
- **Semgrep OSS** — community rulesets `p/python`, `p/security-audit`, and
  `p/owasp-top-ten`. Free and works on a private repository (no login/token
  required for the public rule packs). Same **deny-by-default** posture and same
  inline-allowlist discipline as Bandit: any finding fails CI, and the accepted
  ones are annotated `# nosemgrep: <rule-id>` with a written justification. There
  are currently twelve, all the same rule (`avoid-sqlalchemy-text`) on
  session-guardrail/`EXPLAIN` statements that must interpolate an integer
  timeout or a fixed, dialect-controlled interval keyword —
  `connections/dialects.py` (×9), `compiler/dialect_adapters.py` (×2), and
  `execution/cost_estimation.py` (×1). None takes caller input; see the
  rationale comments at those lines.

> CodeQL is the natural upgrade if GitHub Advanced Security is adopted; it is
> free only on public repositories. Bandit + Semgrep cover the same
> static-analysis requirement today at no cost.

Reproduce: `make sast` (Bandit) and `make semgrep` (Semgrep OSS) — or both at once
via `make security-scan`. Semgrep needs no *install* of its own: the target uses an
installed `semgrep` binary when present and otherwise falls back to the official
`semgrep/semgrep` image, the same pattern as the Trivy and gitleaks targets. Two
prerequisites either way: **network egress** (both paths fetch the `p/...` rule
packs from the Semgrep registry at run time) and **Docker** on the fallback path.
Note Semgrep scans **git-tracked files**, so `git add` a new source file before
relying on a local pass — CI scans a full checkout, where everything is tracked.
CI job: **SAST (Bandit + Semgrep OSS)**.

## Supply chain: dependency audit + SBOM

- **pip-audit** checks the *exact* production dependency set QueryGate ships —
  the `main` group of `poetry.lock`, reproduced in a scratch venv — against the
  vulnerability database. The gate is **deny-by-default**: any known
  vulnerability without a reviewed entry in
  `security/dependency-audit-allowlist.json` fails the build. That allowlist
  currently holds **one entry**: `PYSEC-2026-286` (asyncmy, the MySQL driver
  added by item 19) — a SQL-injection CVE in a codepath (dict-keyed pyformat
  parameters) SQLAlchemy's `mysql+asyncmy` dialect never reaches, since it
  always hands the driver positional parameters; see the allowlist file's own
  entry for the full reasoning and the regression test that guards it. Every
  other previously-known CVE in the shipped set was *remediated by upgrading
  to a fixed version* (fastapi/starlette, mcp, python-dotenv, click, idna),
  not accepted with a compensating control.
- **CycloneDX SBOM** is generated for that same set, alongside a SHA-256
  manifest of the built artifacts.

Reproduce: `make sbom` (see `scripts/generate_sbom.py`). CI: the **Generate SBOM
and audit locked dependencies** step. Maps to **QG** supply-chain controls in the
threat model.

## Container image scanning

QueryGate ships as a container image you pull and run. That image is scanned in
CI with **Trivy** for OS-package and library CVEs, embedded secrets, and
misconfiguration. The gate fails on any **HIGH/CRITICAL** finding that is not a
reviewed exception in `.trivyignore` (deny-by-default, `--ignore-unfixed` so the
gate stays actionable). The current result is **0 HIGH/CRITICAL with no
exceptions**: vulnerable dependencies were upgraded to fixed versions, and
build/install tooling (`pip`/`setuptools`/`wheel`, including the CVE-bearing
copies setuptools vendors internally) is **stripped from the runtime image** in
the Dockerfile — a running service never installs packages — rather than
allowlisted. Trivy consumes the same dependency reality as the SBOM, so the
image scan and the dependency audit agree.

Reproduce: `make scan-image`. CI: the **Trivy scan of the shipped image** step in
the Docker release job.

## Secret scanning

**gitleaks** scans the working tree *and the full git history* on every push, so
we can affirmatively prove no database connection string, API key, or signing key
was ever committed — the concrete backing for QueryGate's credential-isolation
invariant. Reviewed dev-only placeholders (example env files, the throwaway
MSSQL test password) are allowlisted in `.gitleaks.toml`; everything else is
deny-by-default.

Reproduce: `make scan-secrets`. CI job: **Secret scan (gitleaks, full history)**.

## Dynamic analysis (DAST)

**Schemathesis** reads QueryGate's live OpenAPI schema and property-fuzzes every
documented REST endpoint with malformed, boundary, and schema-violating payloads.
Two checks gate the run (deny-by-default):

- `not_a_server_error` — no fuzzed request may produce a 5xx (a 5xx would mean
  input reached an unhandled code path).
- `negative_data_rejection` — schema-violating input **must** be rejected with a
  client error, proving the validation boundary holds and no malformed AST slips
  through to compilation or execution.

Latest run: **1755/1755 checks passed across 72 operations, zero server
errors, zero accepted malformed payloads.** The recursive query-executing endpoints
(which take the nested `StructuredQuery` AST) are covered *more deeply* by the
dedicated malformed-input fuzzer above rather than by generic schema fuzzing.

Reproduce: `make test-dast` (see `scripts/run_dast.py`). CI job: **DAST
(Schemathesis OpenAPI fuzzing)**. This complements — does not replace — the
hand-written adversarial suite.

## Adversarial regression suite

Beyond automated fuzzing, QueryGate carries a purpose-built adversarial suite
(656 tests, `pytest -m security`) encoding specific known bypass classes:
denied-column inference, undeclared-table smuggling, predicate-as-SQL,
schema-discovery leaks, policy-cap boundary breaches, and audit no-leak checks.
New attack vectors are added here as regressions (see the `adversarial-probe`
workflow). Reproduce: `make test-security`.

## Reliability under load

Security guarantees must hold under real concurrency, not just in isolation.
Against a real Postgres, QueryGate runs concurrency-guardrail, heavy-burst, and
repeated **soak** scenarios that assert caps are never breached and no connection
or slot leaks. Reproduce: `make test-load` (bounded) / `make test-soak
SOAK_ROUNDS=100`. CI: the real-Postgres load/soak job.

## Credential isolation

`ConnectionProfile` (which carries the resolved connection string) is split from
`PublicConnectionInfo` (id/dialect/enabled/description only — no credential field
exists on it). Only the public model is ever returned from REST/MCP.
`tests/unit/test_credential_redaction.py` asserts this against the **live OpenAPI
schema and MCP tool schemas**, so it catches drift, not just convention.

## Threat model

[docs/THREAT_MODEL.md](THREAT_MODEL.md) enumerates 43 threats (QG-01…QG-43),
each mapped to its compensating control and the test(s) that enforce it. The
gates on this page are the automated, continuously-run backbone of that model.

## External attestations

Being honest about what a **private, closed-source** product can and cannot
claim here matters more than badge-count:

- **OpenSSF Best Practices** (bestpractices.dev) — this badge is awarded only to
  **open-source (FLOSS) projects with a public repository**, so QueryGate cannot
  be *awarded* it while the source is private. What we do maintain is a
  **self-assessment against its criteria** — a genuine maturity artifact you can
  attach to a security questionnaire, and a ready-to-submit application the day
  any component is open-sourced. See
  [docs/business/openssf-best-practices-answers.md](business/openssf-best-practices-answers.md).
- **OpenSSF Scorecard** — likewise oriented at public repositories
  (branch-protection introspection, public badge serving). Available to enable
  as an internal metric if/when QueryGate is open-sourced; intentionally not run
  as a private-repo CI gate today because most checks are not meaningful without
  a public repo.
- **Signed images + build provenance — implemented.** Because QueryGate is
  distributed as an image customers pull, **Sigstore/cosign image signing + SLSA
  build provenance** is the external attestation that actually fits this product:
  a customer can cryptographically verify the image they run was built by us from
  this source, untampered. The release workflow (`.github/workflows/release.yml`)
  signs the pushed image with cosign keyless (Sigstore, OIDC identity,
  transparency log) and attaches an `actions/attest-build-provenance` SLSA
  attestation, both bound to the image's immutable digest. A consumer verifies
  with `cosign verify` and `gh attestation verify` — the exact commands are in
  [docs/RELEASING.md](RELEASING.md). Artifact *integrity* is separately checkable
  offline with `make verify-release` against `dist/SHA256SUMS`. What remains a
  maintainer step (TODO.md item 30/89 phase 2) is cutting the *first* signed
  release (a deliberate tag push, never automatic) and choosing a Python
  package-index; the signing/provenance mechanism itself is in place.
- **Independent penetration test / third-party audit — none yet.** Every result
  in this document is produced by *our own* gates: our test suite, our
  adversarial regression suite, and industry-standard scanners we run ourselves.
  That is real evidence and it is reproducible by anyone with a checkout
  (see [Reproduce the whole posture](#reproduce-the-whole-posture)), but it is
  **self-attested**. No external party has yet tried to break QueryGate under
  contract. State this plainly in a security review rather than letting
  "adversarial suite" be heard as "pentested" — and treat commissioning one as
  the single cheapest upgrade to this document's standing.
- **Tested is not proven.** The no-raw-SQL and column-policy guarantees rest on
  the correctness of our validators, exercised by the suite — not on formal
  verification or a machine-checked proof, and we do not claim otherwise. What
  the architecture buys is *drift resistance*: because every column reference
  flows through one canonical visitor
  (`validation/schema_validation.py`'s `iter_column_refs`, consumed by policy
  and schema validation alike), a new AST feature is policed in one place rather
  than a dozen — the failure mode where eleven of twelve checks get updated and
  the twelfth silently becomes a bypass. That materially lowers the odds of a
  gap; it does not reduce them to zero.
- **Audit persistence is opt-in, and the tamper-evident ledger is a further
  opt-in.** Be precise, because this is three settings and not one:
  audit logging **to stdout is always on**; `AUDIT_SINK_BACKEND` defaults to
  `none`, so nothing is *persisted* until an operator sets `jsonl`; and the
  hash-chained envelope requires `jsonl_chained` on top of that. Left unkeyed the chain is SHA-256, so
  tamper-*evidence* depends on externally anchoring the head hash
  (`querygate-audit verify --expected-head`); `AUDIT_LEDGER_HMAC_KEY` makes it
  unforgeable without the key. Describe it as "available and independently
  verifiable," never as "on by default."
- **SOC 2 / ISO 27001 control mapping — available.** A control-by-control map of
  QueryGate's product controls to the SOC 2 Trust Service Criteria and ISO 27001
  Annex A, each row backed by a concrete code/test/doc artifact, with an honest
  product-vs-shared-vs-organization responsibility split, is in
  [docs/COMPLIANCE_MAPPING.md](COMPLIANCE_MAPPING.md) (TODO.md item 54). It is a
  readiness baseline for a customer's audit — not a claim that the product is
  itself certified; the audit engagement (item 53) and org-level controls remain
  the deploying organization's.

## Reproduce the whole posture

```bash
make sast            # Bandit static analysis
make semgrep         # Semgrep OSS rulesets (uses the official image if not installed)
make scan-secrets    # gitleaks over full history
make sbom            # CycloneDX SBOM + pip-audit dependency gate
make license-check   # third-party licence inventory gate (docs/THIRD_PARTY_LICENSES.md)
make test-dast       # Schemathesis OpenAPI fuzzing
make test-security   # adversarial regression suite
make scan-image      # Trivy scan of the built container image
make security-scan   # sast + semgrep + scan-secrets + sbom + test-dast in one shot
                     # (test-security and scan-image stay separate)
```

*Last reviewed: 2026-08-11 — every row in the table above **except Licences**
was independently re-run against the tree at that commit, not carried forward
from a prior CI result: `make sast` (Bandit, clean), `make semgrep` (0 findings), `make
scan-secrets` (gitleaks, 352 commits scanned, no leaks), `poetry build` +
`make sbom` (pip-audit clean, 1 allowlisted entry as documented), `make
scan-image` (Trivy, debian 12.15 base + 57 Python packages, 0
vulnerabilities), `make test-security` (green; the suite has grown since, and its
current size is the CI-gated figure in the Adversarial-suite row above rather
than a number frozen into this paragraph), and `make test-dast`
(1755/1755 checks, 72 operations). The **Licences** row was added later, on
2026-08-21, and re-run that day (`make license-check`: 138 locked packages, 60
redistributed, report current); the other rows were not re-run then and still
carry their 2026-08-11 evidence. (Bump this date whenever a row changes.)
Keep this page honest with the `claim-verify` workflow — every row must
point at a gate that exists and passes.*

## Compliance control mapping

*Source: [`docs/COMPLIANCE_MAPPING.md`](COMPLIANCE_MAPPING.md).*

# Compliance control mapping (SOC 2 / ISO 27001 readiness)

This document maps QueryGate's **product controls** to the SOC 2 Trust Service
Criteria (2017 TSC, Common Criteria) and the corresponding ISO/IEC 27001:2022
Annex A controls, with a pointer to the concrete artifact — code, test, CI gate,
or doc — that evidences each one. It exists so a regulated-industry buyer's
security team can answer "does this satisfy control X?" against real evidence
instead of a marketing claim (TODO.md item 54).

**Read the scope boundary first — it is the honest part.** QueryGate is a
**self-hosted product component**, not a SaaS. A SOC 2 report or ISO 27001
certificate is issued to an *organization* for a *system operated over a period*,
not to a piece of software. So this mapping is deliberately split:

- **Product-provided** — a control QueryGate implements and evidences in this
  repository. These are the rows a customer can inherit or point to.
- **Shared responsibility** — QueryGate provides the mechanism; the deploying
  organization must configure and operate it (e.g. point audit at durable
  storage, set the least-privileged DB account).
- **Customer/organization responsibility** — a control that is entirely the
  deploying org's to run (HR screening, physical security, an incident-response
  *process*, the audit engagement itself). QueryGate can't and shouldn't claim
  these; they are listed so nothing is silently missing.

Nothing here asserts QueryGate "is SOC 2 certified" or "is ISO 27001 certified."
It asserts which controls the product *supports with evidence* so the
certification effort starts from a mapped baseline rather than a blank page.

---

## SOC 2 Common Criteria mapping

### CC1 — Control Environment (governance, roles)

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| CC1.1–1.4 Integrity, board oversight, org structure, competence | Org-level — QueryGate is a component, not the operating entity. | — | Customer/org |
| CC1.3 Roles & responsibilities for the system | Least-privilege authorization scopes with recommended role bundles (Analyst/Operator/Config Governor/Catalog Author/Catalog Admin/Data Steward). | `core/scopes.py`, `docs/SCOPE_CATALOG.md` (item 95) | Product-provided |

### CC2 — Communication & Information

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| CC2.1 Quality information (audit trail) | Redaction-safe structured audit log + append-only JSONL sink; every request attempt is recorded. | `audit/logger.py`, `audit/sinks.py`; `tests/unit/test_credential_redaction.py` (item 23) | Product-provided |
| CC2.2/2.3 Internal & external communication of responsibilities | Threat model documents the deployment trust boundary and the operator's obligations. | `docs/THREAT_MODEL.md` §7, `deploy/README.md` | Product-provided |

### CC3 — Risk Assessment

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| CC3.1–3.4 Identify & assess risks, including fraud & change | First-party threat model with assets, attacker capabilities, threats→controls, residual risks, and review triggers. | `docs/THREAT_MODEL.md` | Product-provided |
| CC3.4 Assess changes | Governed config-change management: every change is staged, validated against the same runtime rules, versioned, attributed to the calling principal, and roll-back-able. | `admin/service.py`, `admin/store.py` (item 25) | Product-provided |

### CC4 — Monitoring Activities

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| CC4.1 Ongoing evaluation | Adversarial security regression suite + boundary fuzzing run in CI on every change; published safety benchmark. | `tests/security/`, `make test-security` (items 28/36/55); `querygate-security-benchmark` (item 58) | Product-provided |
| CC4.1 Operational monitoring | Prometheus metrics + aggregated observability endpoint (query volume, rejection categories, queue/concurrency pressure). | `metrics.py`, `GET /metrics`, `admin/observability.py` (items 12/44) | Shared responsibility |

### CC5 — Control Activities

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| CC5.1–5.3 Control selection, technology controls, policies | The entire request pipeline is a deny-by-default control: policy caps + allow/deny, schema validation, no raw-SQL path, per-dialect session guardrails. | `validation/policy_validation.py`, `validation/schema_validation.py`, `compiler/`, `connections/dialects.py` | Product-provided |

### CC6 — Logical & Physical Access Controls

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| CC6.1 Logical access — identity | Pluggable authN: static API keys + JWKS-verified JWT, composable; delegated (on-behalf-of) identity via RFC 8693. | `core/auth.py` (items 8/10/90) | Product-provided |
| CC6.1 Logical access — authorization | Per-principal, per-connection table/column policy keyed by `sub`/claim; scope-gated admin operations; mandatory row filters. | `policy/models.py`, `policy/loader.py`, `core/scopes.py` | Product-provided |
| CC6.1 Least privilege (data) | Column masking/tokenization; deny-by-default policy; least-privileged read-only DB account required by deployment. | item 49 masking; `docs/THREAT_MODEL.md` §7 | Product + Shared |
| CC6.2/6.3 Provisioning & de-provisioning | Identity lifecycle is the IdP's; QueryGate consumes it. Scope→role catalog makes provisioning a role assignment. | `docs/SCOPE_CATALOG.md` (item 95) | Shared responsibility |
| CC6.6 Boundary protection | No raw-SQL/DML input path anywhere; TLS terminated at the operator's proxy; admin/metrics endpoints restricted at the network layer. | `README.md`, `docs/THREAT_MODEL.md` §7; `tests/unit/test_credential_redaction.py` | Product + Shared |
| CC6.7 Restrict data transmission | Credentials never returned on any public model; audit events never carry SQL/values/rows/credentials. | `connections/models.py` (`PublicConnectionInfo`), `audit/sinks.py`; `test_credential_redaction.py` | Product-provided |
| CC6.8 Malicious software / integrity | Signed, provenance-attested release artifacts (cosign keyless + SLSA build provenance, digest-bound); SBOM; dependency & image scanning. | `.github/workflows/release.yml`, `make verify-release` (items 30/89); `docs/SECURITY_POSTURE.md` | Product-provided |
| CC6.4/6.5 Physical access & disposal | Deploying org / cloud provider. | — | Customer/org |

### CC7 — System Operations

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| CC7.1 Vulnerability detection | SAST (Bandit + Semgrep OSS), dependency audit (deny-by-default allowlist), container image scan, secret scanning, DAST — all CI gates. | `docs/SECURITY_POSTURE.md`, `make sast` + `make semgrep`/dep-audit (item 30) | Product-provided |
| CC7.2 Monitoring for anomalies | Read-only behavioral anomaly surfacing on the audit stream. | `admin/anomaly.py` (item 59) | Product-provided |
| CC7.3/7.4 Incident response | Tamper-evident, hash-chained audit ledger + per-query receipts give the forensic record; the IR *process* is the org's. | `audit/ledger.py`, `querygate-audit verify` (item 91) | Product + Org |
| CC7.5 Recovery | HA/DR reference deployment + backup/restore runbook with RTO/RPO. | `deploy/HA_DR.md` (item 56) | Product + Shared |

### CC8 — Change Management

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| CC8.1 Authorized, tested, approved changes | Release gates (`make release-check`/`release-smoke`) for product changes; governed config changes with validation, versioning, attribution, and rollback for runtime changes. Four-eyes approval (item 42) is opt-in via `require_config_approvals` (default 0) and gates a staged version's **first activation only** — rollback to a previously-active version is exempt by design; policy simulation (item 39), semantic access diff (item 40) and blast-radius analysis (item 41) have shipped. | `docs/RELEASING.md` (item 24); `admin/service.py` (item 25); `admin/service.py` `apply()` four-eyes gate + `tests/unit/test_config_approval.py` (item 42) | Product-provided |

### CC9 — Risk Mitigation

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| CC9.1 Risk mitigation | Concurrency limits, timeouts, result-size caps, per-principal quotas, query-cost gating protect the operational DB. | `execution/concurrency.py`, `execution/quota.py`, `execution/cost_estimation.py` | Product-provided |
| CC9.2 Vendor & third-party management | Dependency allowlist + SBOM + third-party licence inventory is the software-supply-chain half; vendor-management *process* is the org's. | `docs/SECURITY_POSTURE.md`; `docs/THIRD_PARTY_LICENSES.md` (`make license-check`); dep-audit | Product + Org |

## Confidentiality (C-series)

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| C1.1 Identify & protect confidential data | Column masking/tokenization, deny-by-default column policy, redaction-safe audit. | item 49; `audit/sinks.py` | Product-provided |
| C1.2 Disposal / retention | Catalog retention/deletion + export (least-privilege scopes); audit retention has an opt-in native WORM path (S3 Object Lock COMPLIANCE mode) or remains operator-configured storage otherwise. | items 32B-2 (`catalog:delete`/`catalog:export`); item 134 (`AUDIT_SINK_BACKEND=jsonl_chained_s3_worm`); `deploy/HA_DR.md` (audit storage) | Product + Shared |

## Availability (A-series)

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| A1.1/1.2 Capacity & recovery | Multi-replica HA deployment, autoscaling, PDB, zero-downtime rolling updates, DR runbook. | `deploy/helm/querygate/`, `deploy/HA_DR.md` (item 56) | Product + Shared |
| A1.3 Recovery testing | Failover-drill checklist provided; the drill is operator-run against their cluster. | `deploy/HA_DR.md` §5 | Shared responsibility |

## Processing Integrity (PI-series)

| Criterion | QueryGate control | Evidence | Status |
|---|---|---|---|
| PI1.1–1.3 Inputs/processing are complete, valid, accurate | Every input is a validated `StructuredQuery` AST against policy + live schema before any DB touch; no raw-SQL path; property-based compiler fuzzing. | request pipeline; `tests/unit/test_compiler_properties.py` (item 36) | Product-provided |

---

## ISO/IEC 27001:2022 Annex A cross-reference (key domains)

| Annex A control | QueryGate evidence |
|---|---|
| A.5.15 Access control | `policy/`, `core/scopes.py`, `docs/SCOPE_CATALOG.md` |
| A.5.16 Identity management | `core/auth.py` (JWT/JWKS, delegated identity) |
| A.5.17 Authentication information | Secrets via env/Vault resolver, never on public models (`secrets/`, `connections/models.py`) |
| A.8.2/8.3 Privileged access & information access restriction | Scope-gated admin ops; per-principal column/row policy |
| A.8.8 Management of technical vulnerabilities | SAST, dep-audit, image scan, secret scan, DAST (`docs/SECURITY_POSTURE.md`) |
| A.8.15 Logging | Redaction-safe structured audit + hash-chained ledger (`audit/`) |
| A.8.16 Monitoring activities | Metrics, observability endpoint, anomaly surfacing |
| A.8.24 Use of cryptography | HMAC-SHA256 audit chaining; cosign-signed releases; TLS at the operator's edge |
| A.8.28 Secure coding | Deny-by-default pipeline, no raw SQL, adversarial regression suite |
| A.8.31 Separation of environments | Config governance staging vs. live; four-eyes approval |
| A.5.7 Threat intelligence / A.5.24-5.28 Incident management | Anomaly surfacing + tamper-evident ledger feed the org's IR process |

---

## Genuine gaps (real, not process theater)

Per this item's rule — close only real gaps, don't manufacture process around
controls that already exist — the following are the honest remainder. They are
**organizational**, not product code, so QueryGate cannot "implement" them; they
are what a deploying org completes to reach an actual report/certificate:

1. **The audit engagement itself.** A SOC 2 Type II report or ISO 27001
   certificate requires an independent auditor and a control-operating period.
   This is coordination-gated (see also item 53, third-party security audit). No
   code closes it.
2. **Organizational controls (CC1, CC6.4/6.5, parts of CC7.3/CC9.2):** HR
   screening/onboarding, physical/environmental security, the incident-response
   *process and on-call*, vendor-management *process*, and a formal access-review
   *cadence*. QueryGate provides the technical evidence these processes act on
   (audit trail, scope catalog, anomaly stream) but the recurring human process
   is the deploying org's.
3. **Access-review evidence formalization.** QueryGate exposes the inputs
   (scope catalog, per-principal policy, audit of admin actions); turning those
   into a scheduled, signed-off access review is an org process to stand up.
4. **Config-change separation-of-duties (shipped; one residual).** Four-eyes
   config approval (item 42), draft-aware policy simulation (item 39), semantic
   access diff (item 40), and blast-radius analysis (item 41) have all shipped
   and back CC8.1 change management alongside release gates and the governed
   config plane (item 25). The approval gate is **opt-in**
   (`require_config_approvals`, default 0) and covers a staged version's first
   activation only — **rollback to a previously-active version is exempt by
   design**, so an org requiring approval on every activation must add that in
   its own change-management process.

The product-side of item 54 — the mapping and the honest gap analysis above — is
complete. Items 53 (auditor) and any org-process standup are the human/vendor
remainder.

## Adversarial benchmark report

*Source: [`docs/business/SECURITY_BENCHMARK.md`](business/SECURITY_BENCHMARK.md).*

# QueryGate adversarial security benchmark

**A reproducible, versioned measurement of how QueryGate's structural
guardrails behave against a corpus of boundary attacks — and how a naive
raw-SQL-forwarding gateway behaves against the same attacks.**

This document turns the adversarial "five-minute demo" from
the go-to-market plan (internal) — historically performed live by a
salesperson — into a numbers-on-the-page artifact anyone can regenerate. It is
the evidence behind the claim that structurally forbidding raw SQL is a
categorically different posture from filtering or trusting a model-generated
SQL string.

> TL;DR of the current corpus (`security_boundary_v1`): QueryGate blocks
> **16/16 (100%)** of the structural boundary attacks; the modeled raw-SQL
> passthrough baseline blocks **0/16 (0%)**; **2** documented inference
> residuals are disclosed (not counted as catches); guardrail overhead is
> **sub-millisecond per query, before any database round-trip.** Regenerate the
> exact current numbers with the command below — never quote these from memory.

## How to run it

```bash
make security-benchmark                                # human-readable report
make security-benchmark ARGS="--json"                  # machine-readable report
make security-benchmark ARGS=list                      # list the corpus cases

# ...or call the CLI directly:
poetry run querygate-security-benchmark run
poetry run querygate-security-benchmark run --json
poetry run querygate-security-benchmark list
```

Exit code is `0` iff the run is clean (every attack caught, no regressions), so
the same command gates CI and a periodic integrity job. The corpus lives in
[`benchmarks/security_boundary_v1.yaml`](../benchmarks/security_boundary_v1.yaml);
the runner is `querygate.security_benchmark`; the harness is regression-locked
by `tests/unit/test_security_benchmark.py`.

## Methodology (why the numbers are trustworthy)

1. **It drives the real guardrails, not a mock.** Every case is run through the
   genuine production code paths — policy validation
   (`validation/policy_validation.py`) and the SQLAlchemy compiler's parameter
   binding (`compiler/sqlalchemy_compiler.py`). A 100% catch rate means the
   shipping enforcement caught the attack, not that a test double returned
   "blocked."
2. **It is offline and deterministic.** The three guardrails this benchmark
   exercises — the AST-only structural shape, policy allow/deny checked over
   *every* column reference in the query (not just `SELECT`), and compiler
   parameter binding — all run *before any database touch*. So the benchmark
   needs no database, no network, and no LLM, and its pass/fail is fully
   reproducible by a third party. (Only the latency figures vary run to run and
   are reported as informational.)
3. **It is honest by construction.** Documented inference residuals
   ([`docs/INFERENCE_RISKS.md`](INFERENCE_RISKS.md)) are carried in the
   corpus and reported in their own line — the benchmark discloses what
   QueryGate does *not* block rather than cherry-picking only its wins. The
   `test_every_attack_case_declares_a_vulnerable_baseline` test prevents the
   comparison from being inflated.

## The attack corpus (`security_boundary_v1`)

| Category | What it probes | Example case |
| --- | --- | --- |
| `sql_injection` | Injection payloads in predicate values are carried as **bound parameters**, absent from the compiled SQL text — across dialects (Postgres, MSSQL) | `sqli-predicate-eq-value` |
| `policy_denied_table` | A denied table cannot be smuggled in through `WHERE`, `ORDER BY`, or any non-`SELECT` clause | `denied-table-in-where` |
| `policy_denied_column` | A denied column cannot be referenced anywhere — `SELECT`, `WHERE` (even when never projected), `GROUP BY`, `HAVING`, `ORDER BY`, or a window partition | `denied-column-in-where-not-selected` |
| `complexity_cap` | Query-shape budgets (max joins, max select columns, …) reject over-broad queries | `joins-exceed-cap` |
| `structural_invariant` | **No field anywhere in the request AST accepts a raw SQL string** — a structural regression lock | `no-raw-sql-escape-hatch` |
| `documented_residual` | Inference risks column policy cannot close (semantic correlation, single-row aggregates) — **allowed by design, disclosed** | `residual-single-row-aggregate` |

The corpus is versioned. A change to the guardrails that would alter a verdict
is a deliberate, reviewed edit (new `corpus_id` version), never silent drift.

## The raw-SQL passthrough baseline — what it is and is not

The baseline column is a **structural model**, declared per case, of a gateway
whose interface is a model-generated SQL string forwarded to the database with
no AST contract. Its verdicts are grounded in a structural fact, not an
assumption: such a gateway has

- **no per-query table/column policy** — it cannot check identifiers it never
  parses into a validated shape, so denied-table/denied-column smuggling
  through any clause lands; and
- **no parameter-binding contract** — a string-concatenating passthrough
  executes an injected statement rather than binding it as data.

This is **not** a live run of any specific competitor. It is the honest lower
bound for "just let the agent send SQL." The point of the comparison is
categorical: the attacks QueryGate stops are ones that a raw-SQL interface has
no structural mechanism to stop, no matter how the SQL was generated or
reviewed.

## Google MCP Toolbox comparison (capability-level, not a live run)

[Google's MCP Toolbox for Databases](https://github.com/googleapis/mcp-toolbox)
is the closest widely-known comparator, so a fair, factual comparison matters.
Based on Toolbox's **documented design** (not a benchmark run against it):

- Toolbox exposes database access as developer-authored *tools*, commonly
  parameterized SQL statements. Where a tool is a fixed parameterized statement,
  its parameters are bound — so the SQL-injection class is **not** where
  QueryGate differentiates against a well-authored Toolbox tool.
- The differentiation is **structural governance of agent-composed queries**:
  QueryGate's caller submits a validated `StructuredQuery` AST and *cannot*
  submit raw SQL at all, and every query is checked against per-principal
  table/column policy and query-shape caps over every clause — by construction,
  before compilation. Toolbox's model centers on the tools a developer decides
  to publish; it does not impose an AST-only contract or a per-query
  column-policy check across every clause on arbitrary agent-composed queries.

We deliberately do **not** publish head-to-head catch-rate numbers against
Toolbox in phase 1, because a fair live comparison requires a comparably
configured Toolbox deployment and would otherwise risk misrepresenting a
documented competitor capability — which this project forbids. That live
comparison is scoped as phase 2 below.

## Scope and phases

- **Phase 1 (this document).** The versioned attack corpus, the runner, the CLI,
  QueryGate's measured catch rate, the structurally-modeled raw-SQL baseline, and
  the capability-level Toolbox comparison. Fully offline, deterministic, and
  reproducible.
- **Phase 2 (not started — needs external infrastructure).** A *live* baseline:
  execute the same corpus against (a) a real LLM composing raw SQL against a
  seeded database and (b) a comparably configured Google MCP Toolbox deployment,
  and publish measured catch-rate and latency numbers for both. This needs a
  model-provider decision and a GCP/Toolbox environment, so it is intentionally
  out of phase 1's offline scope. See TODO.md item 58.

## Relationship to the rest of the security posture

This benchmark is the **publishable subset** of QueryGate's adversarial QA. The
full guarantee set — including error-masking, credential redaction, catalog
disclosure, concurrency/DoS, and config-governance authorization — is
regression-locked in [`tests/security/`](../tests/security/) (TODO.md item
28) and described in [`docs/THREAT_MODEL.md`](THREAT_MODEL.md). The benchmark
exists to make the structural core of that posture *legible and reproducible to
an outside reviewer*, not to replace the full suite.

## Responsible disclosure program

*Source: [`SECURITY.md`](../SECURITY.md).*

# Security Policy

QueryGate is a security product: an agent-safe database access gateway whose
entire value is that an AI agent can never submit raw SQL, never see a table or
column its policy forbids, and never take the database down. We treat security
reports with corresponding seriousness.

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

**Preferred channel — GitHub private vulnerability reporting.** Open a
[private security advisory](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository. It is private to you and the maintainers, it threads the
whole report and fix in one place, and it is the channel we monitor.

**Email — not yet available.** A dedicated
`security@<DOMAIN — to be registered>` alias is the intended second channel and
**does not exist yet**; there is no legal entity or domain behind QueryGate at
the time of writing. Until this placeholder is replaced with a real address,
the GitHub advisory above is the only reporting channel, and we would rather
say that plainly than publish an address that nobody answers. A personal
mailbox previously stood here; it was removed deliberately, because "who
answers a vulnerability report, and how" is a property of the project, not of
one person's inbox.

> Maintainer checklist before wider distribution: register the domain, stand up
> the `security@` alias with a monitored inbox and a documented on-call owner,
> replace the placeholder above, enable **Settings → Code security → Private
> vulnerability reporting** on the public repository, and re-run
> `make trust-page` so `docs/TRUST_EVIDENCE.md` picks up the change.

Please include:

- A description of the issue and the impact you believe it has.
- Steps to reproduce (a minimal `StructuredQuery` AST, request, or config that
  triggers it is ideal).
- The QueryGate version / image digest, dialect (Postgres, MSSQL, or MySQL),
  and any relevant policy configuration.

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
convention. See [docs/SECURITY_POSTURE.md](SECURITY_POSTURE.md) for the
full, verifiable security posture (SAST, dependency/SBOM audit, container and
secret scanning, OpenAPI fuzzing, the adversarial test suite, and the threat
model), and [docs/THREAT_MODEL.md](THREAT_MODEL.md) for the threat-by-threat
control and test mapping.

