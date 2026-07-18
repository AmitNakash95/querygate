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

The landing page, business material, tests, documentation, and `archive/` are repository
assets but are excluded from the container. Historical extraction reports live under
`archive/extraction/`. Generated databases, local environment files, coverage output,
and build artifacts must never be tracked.

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
installed CLI path, builds both wheel and source distribution into `dist/`, and inspects
their members for generated, secret, archived, or test-only files.

Then run the container/infrastructure gate:

```bash
make release-smoke
```

The smoke test uses an isolated Compose project, waits for Postgres and Redis health,
builds the production image, starts QueryGate on port `18080`, checks readiness and
connection discovery, and executes a real structured query against the seeded Postgres
database. Its dedicated containers and volumes are removed on exit.

## Artifact inspection and installation smoke

Before tagging, inspect `dist/` and install the wheel into a fresh temporary environment.
Confirm that the package version imports and `querygate-validate-config` works outside the
repository directory; the container smoke covers the server entry point. The release check
ensures generated `.db` files and historical identities are absent from tracked product
inputs.

## Tagging

After every gate passes and the release commit is final:

```bash
git tag -a v0.1.0 -m "QueryGate v0.1.0"
```

Tags and artifacts are local until a maintainer explicitly pushes or publishes them.
Public registry automation, image signing, provenance, and SBOMs are intentionally deferred
to TODO item 30.

## Rollback

Do not move an already published tag. Fix the issue, increment the version, rerun every
gate, and publish a new release. For an unpublished local candidate, delete and recreate
the local tag only after confirming that no remote contains it.
