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
| **SAST** | Static security analysis of source | **Bandit** + **Semgrep OSS** (`p/python`, `p/security-audit`, `p/owasp-top-ten`) | ✅ Clean (deny-by-default) | `make sast` |
| **Dependencies** | Known-CVE audit of the exact shipped set | **pip-audit** against `poetry.lock` `main` group | ✅ Clean — **0 allowlisted** (all fixed) | `make sbom` |
| **SBOM** | Software bill of materials | **CycloneDX** | ✅ Generated per release | `make sbom` |
| **Container image** | OS + library CVEs, secrets, misconfig | **Trivy** on the shipped image | ✅ **0 HIGH/CRITICAL** (no exceptions) | `make scan-image` |
| **Secrets** | No credential ever committed | **gitleaks** over full git history | ✅ Clean | `make scan-secrets` |
| **DAST** | Fuzz the API for validation bypass / crashes | **Schemathesis** against the live OpenAPI schema | ✅ 0 server errors, 0 bypass | `make test-dast` |
| **Adversarial suite** | Known bypass classes as regressions | Purpose-built pytest suite (`-m security`) | ✅ 260 tests | `make test-security` |
| **Reliability** | Guardrails hold under real concurrent load | Real-Postgres soak/load gates | ✅ Enforced in CI | `make test-load` / `make test-soak` |
| **Credential isolation** | No secret on any returned model | Asserted against live OpenAPI + MCP schemas | ✅ Enforced | `pytest tests/unit/test_credential_redaction.py` |
| **Best-practices self-assessment** | OpenSSF criteria maturity | **OpenSSF Best Practices** criteria (self-assessed) | 🟡 Self-assessed | see [below](#external-attestations) |

Legend: ✅ implemented and gating CI · 🟡 self-assessed / not yet an external award.

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
  required for the public rule packs).

> CodeQL is the natural upgrade if GitHub Advanced Security is adopted; it is
> free only on public repositories. Bandit + Semgrep cover the same
> static-analysis requirement today at no cost.

Reproduce: `make sast`. CI job: **SAST (Bandit + Semgrep OSS)**.

## Supply chain: dependency audit + SBOM

- **pip-audit** checks the *exact* production dependency set QueryGate ships —
  the `main` group of `poetry.lock`, reproduced in a scratch venv — against the
  vulnerability database. The gate is **deny-by-default**: any known
  vulnerability without a reviewed entry in
  `security/dependency-audit-allowlist.json` fails the build. That allowlist is
  currently **empty**: every previously-known CVE in the shipped set was
  *remediated by upgrading to a fixed version* (fastapi/starlette, mcp,
  python-dotenv, click, idna), not accepted with a compensating control.
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

Latest run: **846/846 checks passed across 59 operations, zero server errors,
zero accepted malformed payloads.** The recursive query-executing endpoints
(which take the nested `StructuredQuery` AST) are covered *more deeply* by the
dedicated malformed-input fuzzer above rather than by generic schema fuzzing.

Reproduce: `make test-dast` (see `scripts/run_dast.py`). CI job: **DAST
(Schemathesis OpenAPI fuzzing)**. This complements — does not replace — the
hand-written adversarial suite.

## Adversarial regression suite

Beyond automated fuzzing, QueryGate carries a purpose-built adversarial suite
(260 tests, `pytest -m security`) encoding specific known bypass classes:
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

[docs/THREAT_MODEL.md](THREAT_MODEL.md) enumerates 32 threats (QG-01…QG-32),
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
make sast            # Bandit static analysis (Semgrep runs in CI)
make scan-secrets    # gitleaks over full history
make sbom            # CycloneDX SBOM + pip-audit dependency gate
make test-dast       # Schemathesis OpenAPI fuzzing
make test-security   # adversarial regression suite
make scan-image      # Trivy scan of the built container image
make security-scan   # SAST + secrets + dependency audit + DAST in one shot
```

*Last reviewed: 2026-07-22. Keep this page honest with the `claim-verify`
workflow — every row must point at a gate that exists and passes.*
