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
secret, archived, or test-only files, gates the third-party licence inventory, and
generates a software bill of materials and dependency vulnerability report (both below).

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
4. Writes `dist/SHA256SUMS` — checksums for the wheel, the source distribution, the SBOM
   itself, and the third-party licence inventory — so a downloaded artifact set can be verified against what this repository's CI
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

## Dependency licence inventory

`docs/THIRD_PARTY_LICENSES.md` records the licence of **every** Python package in
`poetry.lock`, split by whether QueryGate actually redistributes it. It is gated on every release like the
SBOM — though unlike the SBOM, `make release-check` only *checks* it; you regenerate
it yourself with `make license-report`. `make sbom` then copies the checked-in report
into `dist/` and covers it with `dist/SHA256SUMS`, so a consumer who downloads and
verifies a release bundle gets the licence inventory with it rather than having to be
sent it separately. It is the standard answer to the "list your third-party components and their
licences" question on a vendor security questionnaire.

"Redistributed" means the `main` group, precisely: the published container image contains
those packages (`Dockerfile` runs `poetry install --no-root --only main`), while the wheel
and source distribution contain none of them: QueryGate declares only its **direct**
requirements and `pip` resolves the rest transitively, so a `pip install querygate`
fetches them from PyPI — subject to the same environment markers, and to pip's own
resolution against the declared version ranges rather than to this lockfile's pins. Packages carrying an environment marker
(a Windows-only wheel, for instance) are installed only where their marker applies. The
inventory covers Python packages only: the image additionally layers a Debian `bookworm`
userland and Microsoft's `msodbcsql18` driver, installed under `ACCEPT_EULA=Y` on its own
proprietary terms. Clearing the image's OS layer is separate, unfinished work.

`make release-check` runs `make license-check` (`scripts/check_licenses.py --check`), which
fails on any of:

- a **strong**-copyleft dependency (GPL, AGPL) in *either* group. This one cannot be
  waived at all: no reviewed entry makes it acceptable, and the gate does not consult one;
- a **weak**-copyleft dependency (MPL, LGPL) with no reviewed entry in
  `security/copyleft-license-allowlist.json` — deny-by-default, the same posture
  `security/dependency-audit-allowlist.json` takes for CVEs;
- a licence string the gate does not **recognise**, which is a failure rather than a guess,
  so a new copyleft licence cannot slip through on a fuzzy match. The overrides file below
  is not a way around this: an override contradicted by an unrecognised declared licence
  fails too;
- a locked package whose licence cannot be read locally and has no evidence-backed record
  in `security/third-party-license-overrides.json` — either because it cannot be installed
  here (a Windows-only wheel, or a marker that does not apply) or because it declares no
  licence metadata at all;
- an installed package whose **version** differs from the locked pin, so a licence is never
  attributed to a release it was not read from;
- a reviewed record that has gone **stale** — its package left the lock, relicensed to
  something permissive, or moved between the redistributed and dev-only sets; or
- **drift** — the checked-in report no longer matching `poetry.lock`. Regenerate it with
  `make license-report`.

Passing the gate is **not** the same as the licence questions being settled. Each reviewed
record carries a `review_status`, and `make license-check` prints a `NOTICE:` line for every
record still marked `draft` — analysis written but not confirmed by the owner. Every
record is still a draft today, including `certifi` (MPL-2.0), the one in the
redistributed set.

This follows the SBOM's design rather than a licence scanner's default: `poetry.lock` is
the authority for which packages exist and at which version, not the ambient virtualenv —
an environment scanner reports whatever happens to be installed, including stale leftovers
QueryGate does not ship. `dev`-group packages are build/test/CI tooling that is never
distributed, so their licences constrain how QueryGate is developed rather than how it may
be licensed.

The same gate runs in the default unit suite (`tests/unit/test_third_party_licenses.py`),
so a `poetry add` or `poetry update` that pulls in a copyleft dependency fails at test
time, not at release time.

Five packages cannot have their licence read locally — four are Windows-only or
marker-excluded, and one declares no licence metadata at all — so theirs are recorded
with evidence in `security/third-party-license-overrides.json`. Those records do not
rest on a hand-typed snapshot: the nightly workflow runs
`scripts/check_licenses.py --verify-overrides`, which re-reads each release's declared
licence from PyPI and fails on a mismatch. That mode needs network, which is why it is
deliberately not part of `make license-check` — the per-commit gate stays hermetic and
a flaky network blocks nobody's commit.

Each reviewed copyleft record also carries a machine-checked `facts` block — which
packages require it, whether QueryGate declares it directly, and whether any module under
`src/querygate/` imports it — and the
gate verifies all three against `poetry.lock`, `pyproject.toml`, and the source tree on
every run. The legal
reading in `reason` cannot be verified that way and is explicitly a draft; its factual
premises can be, and are.

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
  CVE gate `make release-check` runs, over the exact locked ship set — then
  `scripts/check_licenses.py --verify-overrides`, which re-reads from PyPI the five
  third-party licences that cannot be read from a local install.
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
signed+attested release is only produced when a maintainer actually pushes a version tag —
publishing is never automatic on a `main` commit.

**Python package index: public PyPI.** The index question is closed (see the go-to-market
plan's distribution decision): the Python wheel and sdist publish to **public PyPI**, and
the container image to **GHCR** as already described above. A private index was rejected —
distribution friction is adoption friction, and a source-available licence whose package
cannot be `pip install`ed defeats its own purpose.

The *decision* is made; the *mechanism* is not built. `.github/workflows/release.yml`
today builds, scans, pushes, signs, and attests the container image only — it contains no
PyPI upload step, and no index credential or trusted-publisher configuration exists yet.
Until that step lands, the wheel and sdist are distributed as release artifacts and
verified by `SHA256SUMS` / `make verify-release`. When it does land it must inherit the
same tag gate as the image: publishing is never automatic on a `main` commit.

## The EULA is the licence of record

QueryGate ships **proprietary and closed-source** under a paid monthly
subscription (`docs/business/GTM_SAAS.md`, TODO.md item 210). There is no BSL
flip and no Change Date: `docs/legal/EULA.en.md` (with the Hebrew
`EULA.he.md`) is the document a customer accepts, and `LICENSE` carries the
proprietary notice.

```bash
make eula-check            # tolerant: reports outstanding placeholders, exit 0
make eula-check-release    # pre-tag: refuses ANY unfilled placeholder
```

- **`make eula-check`** runs on every push (CI) and inside `make release-check`.
  While counsel has not settled the text it reports how many placeholders remain
  and passes, so development is never blocked by an unfinished legal document.
- **`make eula-check-release`** is the pre-tag gate, wired into
  `.github/workflows/release.yml` *before* the image is built. A tag pushed while
  the licence of record still reads `[LICENSOR LEGAL NAME]` or `[ADDRESS]` fails
  the release rather than publishing a signed image with a blank licence.
- The placeholder pattern deliberately matches **any** bracketed span, not
  `\[[A-Z_ ]+\]`. `EULA.he.md`'s placeholders are Hebrew, so an ASCII-uppercase
  pattern would give a green build on an entirely unfilled Hebrew licence.
  Markdown links are excluded. Drift-tested by `tests/unit/test_eula.py`.

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
