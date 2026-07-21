---
name: release-gate
description: >-
  Run QueryGate's pre-commit/pre-release checks in the right order and decide when
  the heavier smoke test is required. Use before committing a non-trivial change,
  when asked to "run the release checks", or when preparing a release. Encodes the
  when-to-smoke rule and the tag-safety rules.
---

# release-gate — the ship checklist

See `docs/RELEASING.md` for the authoritative gate definitions; this skill is the
operational sequence.

## Always

```bash
make release-check   # deterministic source/package/format/test gates + build artifacts
```

`release-check` covers formatting (`black --check`), the full test suite, config
validation, and packaging. Fix failures before proceeding — don't commit red.

## Run smoke only when the change warrants it

```bash
make release-smoke   # builds the image + runs a real structured query against Postgres
```

Run `release-smoke` **if and only if** the change affects any of:

- packaging (pyproject, dependencies, package layout)
- containers (Dockerfile, .dockerignore, entrypoint)
- configuration defaults (env, example YAMLs, loaders)
- database execution (compiler, execution pipeline, connections, dialects)
- deployment (deploy/, compose, runtime wiring)

For test-only, docs-only, or pure internal-refactor changes with no runtime
surface, `release-check` alone is sufficient — skip smoke and say why.

## Targeted suites (when relevant to the change)

```bash
make test-security       # touched pipeline/validation/audit/catalog security surface
make compose-up && make test-postgres-live   # touched timeout/concurrency guardrails
make test-load           # touched concurrency limiter behavior
make test-soak SOAK_ROUNDS=100   # deeper guardrail soak
```

## Tag & remote safety (hard rules)

- **Never** reset, amend, delete, or recreate the local `v0.1.0` tag.
- Do **not** push, publish artifacts, move tags, or modify remotes without
  explicit user approval.

## Report

State which gates ran, pass/fail for each, whether smoke was required (and why /
why not), and confirm no red check remains before the commit.
