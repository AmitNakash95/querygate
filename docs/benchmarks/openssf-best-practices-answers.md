# OpenSSF Best Practices — self-assessment

> **What this is.** A self-assessment of QueryGate against the
> [OpenSSF Best Practices Badge](https://www.bestpractices.dev/) *passing*-level
> criteria. It is **not** a claim that QueryGate holds the badge: that badge is
> awarded only to open-source (FLOSS) projects with a **public** repository, and
> QueryGate's source is intentionally private (it ships as a distributed image).
>
> This document serves two real purposes:
> 1. It is a maturity artifact a prospect's security team can read directly — the
>    same criteria, honestly self-scored.
> 2. It is a ready-to-submit answer set the day any QueryGate component is
>    open-sourced.
>
> Status values: **Met** · **Met (private-repo equivalent)** · **N/A (needs
> public repo)** · **Unmet**. Keep this in sync with `docs/SECURITY_POSTURE.md`.

## Basics

| Criterion | Status | Evidence / justification |
|---|---|---|
| Project description / what it does | Met | `README.md`, `docs/PRODUCT_GUIDE.md` |
| Public project homepage | N/A (needs public repo) | Private commercial product; landing page under `landing/` |
| Version-controlled source | Met | Git; conventional history, `main` + feature branches |
| Unique version numbering | Met | Semver image tags; `pyproject.toml` version |
| Release notes per release | Met | `CHANGELOG.md`, `docs/RELEASING.md` |

## Change control

| Criterion | Status | Evidence / justification |
|---|---|---|
| Public VCS repository | N/A (needs public repo) | Private by design |
| Tracked issues / worklist | Met (private-repo equivalent) | `TODO.md` + `docs/TODO_ARCHIVE.md` (permanent item numbering) |
| Unique version identifiers | Met | Semver + image digests |

## Reporting

| Criterion | Status | Evidence / justification |
|---|---|---|
| Vulnerability reporting process documented | Met | `SECURITY.md` (private disclosure channel, SLAs) |
| Private vulnerability reporting | Met | Email channel + GitHub private security advisories |
| Bug reporting process | Met (private-repo equivalent) | Maintainer channel; internal worklist |

## Quality

| Criterion | Status | Evidence / justification |
|---|---|---|
| Working build system | Met | Poetry; `make`, `Dockerfile`, Helm chart |
| Automated test suite | Met | `pytest` (unit/integration/security), real Postgres + MSSQL CI jobs |
| Tests run on every change (CI) | Met | `.github/workflows/ci.yml` (push + PR) |
| New-functionality tests policy | Met (private-repo equivalent) | Every shipped TODO item lands with tests; release gates enforce |
| Coding standards / style enforced | Met | `black --check` in CI |
| Warning flags / linters enabled | Met | Bandit + Semgrep (SAST) gate CI |

## Security

| Criterion | Status | Evidence / justification |
|---|---|---|
| Developers know secure design basics | Met | `docs/THREAT_MODEL.md`; structured-AST-only design |
| Use of good cryptography (no home-grown) | Met | JWT via `pyjwt[crypto]`; TLS at deployment; no custom crypto |
| Secrets kept out of source | Met | `${ENV_VAR}` resolution; gitleaks full-history gate (`.gitleaks.toml`) |
| Delivery protects against MITM | Met | cosign keyless signature + SLSA build-provenance attestation on the published image, both digest-bound (`.github/workflows/release.yml`); consumer verifies with `cosign verify` / `gh attestation verify` (see `docs/RELEASING.md`) |
| Publicly-known vulnerabilities patched | Met | pip-audit deny-by-default (`security/dependency-audit-allowlist.json`), Trivy image gate |
| No leaked credentials in releases | Met | gitleaks history scan; credential-redaction unit test vs live schemas |

## Analysis

| Criterion | Status | Evidence / justification |
|---|---|---|
| Static analysis applied | Met | Bandit + Semgrep OSS (`make sast`) |
| Static analysis run on every change | Met | `sast` CI job |
| Dynamic analysis applied | Met | Schemathesis OpenAPI fuzzing (`make test-dast`) + adversarial suite |
| Fuzzing / property testing | Met | Schemathesis + Hypothesis-based malformed-input fuzzer (item 36) |
| Dependency vulnerability scanning | Met | pip-audit + Trivy, both deny-by-default |

## Summary

Every **Quality**, **Security**, and **Analysis** criterion is **Met** by a real,
CI-enforced gate — the substance the badge is meant to signal. The only unmet
items are the ones that *require a public repository* (public homepage, public
VCS, public issue tracker), which are a deliberate product decision, not a
security gap.

**Signed delivery — the previously-noted gap — is now closed at the mechanism
level:** the release workflow signs the published image with cosign keyless
(Sigstore) and attaches a SLSA build-provenance attestation, both digest-bound
and consumer-verifiable (`cosign verify` / `gh attestation verify`). What remains
is operational, not a capability gap: a maintainer cutting the *first* signed
release (a deliberate tag push) and choosing a Python package-index — tracked in
TODO.md item 30/89 phase 2.

*Last reviewed: 2026-07-23.*
