# Releasing QueryGate

This document defines the reproducible release process for the self-hosted QueryGate
source package and container. Version `0.1.0` is a release candidate suitable for
controlled pilots; it is not a claim of third-party certification or general availability.

## Release boundary

Included runtime surfaces:

- the `querygate` Python package and CLI;
- bundled example connection/policy files used by the default configuration;
- the production Docker image; and
- the Postgres/Redis Compose stack used for local verification.

The landing page, business material, tests, documentation, `deploy/`, and `archive/` are
repository assets but are excluded from the container. `deploy/` (production Docker
Compose and Helm chart references, see `deploy/README.md`) is used directly from a
checkout, not bundled into the package or image — operators clone or copy it, they don't
`pip install` it. Historical extraction reports live under `archive/extraction/`.
Generated databases, local environment files, coverage output, and build artifacts must
never be tracked.

## Prerequisites

- Python 3.11 and Poetry 2.4.1;
- Docker with Compose v2;
- `curl`; and
- a clean Git worktree containing the intended release changes.

## Versioning

Update the version in both `pyproject.toml` and `src/querygate/__init__.py`. Runtime
configuration derives its default version from the package, and `scripts/check_release.py`
rejects mismatches. Add the user-visible changes to `CHANGELOG.md` before building.

## Required gates

Run the deterministic source/package gate:

```bash
make release-check
```

It verifies the lock file and release metadata, rejects generated or legacy product files,
checks formatting, runs the default test suite, validates bundled configuration from the
installed CLI path, runs the fixed-threshold offline semantic-memory benchmark, builds
both wheel and source distribution into `dist/`, inspects their members for generated,
secret, archived, or test-only files, and generates a software bill of materials and
dependency vulnerability report (below).

Then run the container/infrastructure gate:

```bash
make release-smoke
```

The smoke test uses an isolated Compose project, waits for Postgres and Redis health,
builds the production image, starts QueryGate on port `18080`, checks readiness and
connection discovery, and executes a real structured query against the seeded Postgres
database. Its dedicated containers and volumes are removed on exit.

## Artifact inspection and installation smoke

Before tagging, inspect `dist/`. `make release-check`'s final step (`make sbom`, below)
already installs the built wheel into a fresh temporary environment and confirms it imports
cleanly, in addition to generating the SBOM; the container smoke covers the server entry
point end to end. The release check ensures generated `.db` files and historical identities
are absent from tracked product inputs.

## Software bill of materials and dependency audit

`make release-check`'s last step runs `make sbom` (`scripts/generate_sbom.py`), which:

1. Reads `poetry.lock`'s `main` dependency group — the exact locked set QueryGate ships,
   not whatever an unpinned `pip install` would resolve today — and installs those pins,
   `--no-deps`, plus the built wheel, into a throwaway virtual environment.
2. Generates a CycloneDX 1.6 SBOM from that environment: `dist/querygate-<version>.cdx.json`.
3. Runs `pip-audit` against the same environment and fails the release (deny-by-default) on
   any known vulnerability that is not a reviewed entry in
   `security/dependency-audit-allowlist.json`. Each allowlist entry records the advisory id,
   the affected package, and a specific reason the finding doesn't reach QueryGate's actual
   code paths (e.g. an unused transport, a function QueryGate never calls) — a new vulnerable
   dependency fails the build until it is either upgraded or explicitly reviewed and added
   there, not silently ignored.
4. Writes `dist/SHA256SUMS` — checksums for the wheel, the source distribution, and the SBOM
   itself — so a downloaded artifact set can be verified against what this repository's CI
   produced:

   ```bash
   shasum -a 256 -c dist/SHA256SUMS
   ```

`make verify-release` (`scripts/verify_release.py`) is the gating, cross-platform
counterpart to that snippet: it re-computes each artifact's SHA-256 and fails closed on a
missing manifest, a missing artifact, a digest mismatch, or a malformed manifest — the
integrity check a consumer runs after downloading a release bundle. It checks *integrity*
(the bytes are the ones this repo produced), not *authenticity*; authenticity of the
published container image comes from the signature and provenance described next.

## Scheduled security scans and soak

`.github/workflows/ci.yml` only triggers on `push`/`pull_request`, so its SBOM/CVE
audit, image scan, and guardrail load tests run only when the repo is touched. A CVE
disclosed against an already-merged, unchanged dependency (or the shipped image's
OS/library layers) would otherwise go unnoticed until the next incidental change, and
the heavy `make test-soak` (`SOAK_ROUNDS=100`) never runs per-PR — only the lighter
5-round `test-load` does.

`.github/workflows/scheduled.yml` closes both windows. It runs nightly at 07:00 UTC
(and on-demand via `workflow_dispatch`, with an optional `soak_rounds` input),
independent of any code change, and covers three jobs:

- **`dependency-audit`** — `poetry check --lock` (lockfile drift) followed by
  `scripts/generate_sbom.py`, the same CycloneDX SBOM + `pip-audit` deny-by-default
  CVE gate `make release-check` runs, over the exact locked ship set.
- **`image-scan`** — builds the production image and Trivy-scans it for HIGH/CRITICAL
  vulnerabilities, secrets, and misconfig (`--ignore-unfixed`, exceptions in
  `.trivyignore`).
- **`soak`** — seeds the demo + stress databases and runs `make test-soak`
  (`SOAK_ROUNDS=100`), repeating the real-Postgres guardrail load scenarios far
  enough to surface a slow pool leak or a cap breach a single pass would miss.

A red nightly run means a new CVE, lockfile drift, or a guardrail regression has landed
on `main` without a code change to trigger the per-PR gates — triage it the same as a
failed release gate.

## Signed, provenance-attested container image

The published container image is cryptographically signed and carries SLSA build
provenance. Both are produced by `.github/workflows/release.yml` when a maintainer pushes a
version tag (`v*`); the workflow builds the image, Trivy-scans it (deny-by-default,
pre-publish), pushes it to GHCR, and then:

- **cosign keyless signature (Sigstore).** Signed with the workflow's OIDC identity and
  recorded in the public transparency log — no long-lived signing key to manage.
- **SLSA build-provenance attestation** (`actions/attest-build-provenance`). A signed
  in-toto attestation, bound to the same OIDC identity, stating which repo/workflow/commit
  built those exact bytes; pushed to GHCR next to the image and stored in GitHub's
  attestations API.

Both bind to the image's immutable digest, not a mutable tag. A consumer verifies before
running:

```bash
# Signature — proves the image came from this repo's release workflow.
cosign verify ghcr.io/agitmit/querygate:0.1.0 \
  --certificate-identity-regexp 'https://github.com/AGitmit/QueryGate/.github/workflows/release.yml@.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

# SLSA build provenance — proves which workflow/commit built these exact bytes.
gh attestation verify oci://ghcr.io/agitmit/querygate:0.1.0 --repo AGitmit/QueryGate
```

**Still deferred to a maintainer decision (TODO.md item 30/89 phase 2):** the *first*
signed+attested release is only produced when a maintainer actually pushes a version tag
(publishing is never automatic on a commit), and **Python package-index (PyPI/private
index) publishing** has no index chosen yet — the signing/provenance above cover the
container image, which is how QueryGate is distributed; the Python wheel/sdist are verified
by `SHA256SUMS`/`make verify-release` until an index is chosen.

## Tagging

After every gate passes and the release commit is final:

```bash
git tag -a v0.1.0 -m "QueryGate v0.1.0"
```

Local `make release-check`/`release-smoke` prove the release locally; pushing the `v0.1.0`
tag is what triggers `.github/workflows/release.yml` to build, scan, push, sign, and attest
the image. A maintainer pushes the tag deliberately — publishing is never automatic on a
`main` commit.

## Rollback

Do not move an already published tag. Fix the issue, increment the version, rerun every
gate, and publish a new release. For an unpublished local candidate, delete and recreate
the local tag only after confirming that no remote contains it.
