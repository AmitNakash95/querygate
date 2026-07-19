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

Cryptographic signing of published artifacts/images (e.g. Sigstore/cosign keyless signing)
needs a real publishing pipeline — a container registry and/or package index to attach the
signature and transparency-log entry to — which does not exist yet for this project; see
TODO.md item 30's phase 2.

## Tagging

After every gate passes and the release commit is final:

```bash
git tag -a v0.1.0 -m "QueryGate v0.1.0"
```

Tags and artifacts are local until a maintainer explicitly pushes or publishes them.
Public registry automation and image signing are intentionally deferred to TODO item 30's
phase 2; the SBOM, dependency audit, and artifact checksums described above are phase 1 and
already run as part of every `make release-check`.

## Rollback

Do not move an already published tag. Fix the issue, increment the version, rerun every
gate, and publish a new release. For an unpublished local candidate, delete and recreate
the local tag only after confirming that no remote contains it.
