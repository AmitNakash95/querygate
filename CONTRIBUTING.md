# Contributing to QueryGate

> **DRAFT — the licensing and CLA sections describe terms that have not yet been
> settled by a lawyer, and are NOT LEGAL ADVICE.** The CLA is drafted but not
> enabled: no bot is running, and no contribution is currently being asked to
> sign anything. See [`.github/cla/README.md`](.github/cla/README.md) for the
> exact state.

QueryGate is an agent-safe database access gateway. The one thing a caller can
ever submit is a validated `StructuredQuery` JSON AST or a write AST — there is
no raw-SQL field or endpoint anywhere in the codebase, and there never will be.
That is not a coding convention, it is the product. Read
[`CLAUDE.md`](CLAUDE.md)'s "Non-negotiables" section before you write anything;
a change that weakens one of the eight invariants will be rejected on principle
regardless of how good the code is.

## Before you open a pull request

**Talk to us first for anything non-trivial.** Open an issue describing the
problem and the shape of the fix. QueryGate has a maintained worklist
([`TODO.md`](TODO.md), sequenced by [`ROADMAP.md`](ROADMAP.md)) and a lot of
design that is deliberate rather than accidental; a large PR that arrives
unannounced is likely to collide with something. Small, obvious bug fixes and
documentation corrections do not need this.

**Security issues do not go in a pull request.** See
[`SECURITY.md`](SECURITY.md) for the private reporting path. A PR that fixes a
vulnerability is a public disclosure of that vulnerability.

## Local setup

```bash
poetry install          # install dependencies (Python >=3.11,<4.0, Poetry-managed)
cp .env.example .env    # local env config
make install-hooks      # enable the committed git hooks — run once per clone
```

`make install-hooks` points `core.hooksPath` at `.githooks/`. The pre-commit
hook is the floor, not the ceiling: it blocks merge-conflict markers,
credential-shaped strings, oversized blobs, `black --check src/ tests/`
failures, and a failing `pytest -m "unit and not real_db"`. It is bypassable
with `--no-verify`; please do not make a habit of it.

## The checks

| Command | What it does |
|---|---|
| `make format` | Format with Black (`src/ tests/ examples/ scripts/`). |
| `make format-check` | Check formatting without writing. Must be clean. |
| `make test-unit` | `pytest -m unit` — the minimum bar for any change. |
| `make test-integration` | `pytest -m integration`. |
| `make test-security` | The adversarial boundary suite. Run it if you touched the AST, policy, or validation. |
| `make worklist-check` | Verifies `TODO.md` / `ROADMAP.md` / `docs/TODO_ARCHIVE.md` reconcile. |
| `make license-check` | Gates every locked dependency's licence, deny-by-default. Run it after any `poetry add` / `poetry update`. |
| `make release-check` | The full deterministic source/package gate. Not needed per-PR. |

Test tiers are pytest markers, declared in `pyproject.toml` under
`[tool.pytest.ini_options] markers`: `unit`, `integration`, `security`,
`verification`, `load`, and `real_db` (plus `postgres_live` / `mssql_live` /
`mysql_live`). The default run excludes `real_db`, so you do not need a database
to contribute. If you want one:

```bash
docker compose up -d          # demo Postgres on localhost:5433, auto-seeded
make test-postgres-live
```

Note that a bare `pytest -m integration` **overrides** the default `real_db`
exclusion. Use `-m "integration and not real_db"` if you have no database
running.

## What a good pull request looks like

- **One focused change**, with a commit message that says what and why.
- **A test that fails without your change.** For anything that adds an
  enforcement point — a cap, a rejection, a validation branch — break it
  deliberately once and confirm a test fails *for that reason* before you send
  it. A green suite over an unguarded line is the failure mode this project
  cares about most.
- **`make format-check` and `make test-unit` green**, with the real output, not
  an assumption.
- **Docs updated if the change is docs-worthy** — new architecture, a deliberate
  tradeoff, a new term or tool, or a customer-facing capability goes in
  `docs/PRODUCT_GUIDE.md` and its Decision Log. Pure bug fixes, no-behaviour
  refactors, and test-only changes are exempt.
- **New behaviour that varies by dialect, backend, or strategy goes behind a
  Protocol plus one class per variant plus a registry** — never an inline
  `if dialect == ...` at a call site. There are five established precedents;
  follow the nearest one.

## Worklist discipline

If your change corresponds to a numbered item in `TODO.md`, say so in the PR.
**Item numbers are permanent and file-global — never renumber or reuse one.** A
new item takes the next unused number. `make worklist-check` enforces the
reconciliation rules between `TODO.md`, `ROADMAP.md`, and
`docs/TODO_ARCHIVE.md`, and it runs in the unit suite and in the pre-commit
hook, so drift fails fast.

## Licence and the Contributor Licence Agreement

QueryGate is licensed under the **Business Source License 1.1** — see
[`LICENSE`](LICENSE) and the plain-language
[licensing FAQ](docs/LICENSING_FAQ.md). Source-available, not open source; each
version converts to Apache-2.0 four years after it ships.

**Contributions will require a lightweight click-through Contributor Licence
Agreement.** The reason is specific and worth stating plainly rather than hiding
behind boilerplate: QueryGate is sold under commercial licences alongside BSL.
Under a bare Developer Certificate of Origin an outside contributor licenses
their patch under BSL only, and the project then cannot grant a paying customer
rights above BSL over that code. A CLA keeps that possible. It does not assign
your copyright — you keep it, and you grant a licence broad enough to relicense.

The draft agreement is [`.github/cla/CLA.md`](.github/cla/CLA.md). **It is a
draft, it has not been reviewed by a lawyer, and nothing is enforcing it yet.**

## For maintainers: the first-outside-PR runbook

Three things must be decided *before* the first external pull request arrives,
not while one is sitting open. None is done yet.

### 1. Turn the CLA bot on

`.github/cla/` holds the configuration and a **disabled** workflow file. Nothing
is active. See [`.github/cla/README.md`](.github/cla/README.md) for the exact
enabling steps, the signature-store decision, and what must be settled by
counsel first.

### 2. Decide what CI does for a fork PR

`PRE_BSL_CLEANUP_PLAN.md` Phase 4 raises this as "secrets-dependent jobs (live
MSSQL/MySQL/Snowflake) will fail for an outside contributor". **Checked against
the workflows as they stand today, that premise does not hold, and the decision
is smaller than it looks:**

- `.github/workflows/ci.yml` consumes **no GitHub Actions secrets at all**. The
  only `secrets.` references anywhere in `.github/workflows/` are a Helm
  `--set secrets.create=true` chart value (not a repository secret) and
  `secrets.GITHUB_TOKEN` in the tag-triggered `release.yml`, which a pull
  request never runs.
- The `postgres-live`, `mssql-live`, `mysql-live`, and
  `cross-dialect-differential` jobs use ephemeral GitHub Actions **service
  containers** from public images (`postgres:15.1`,
  `mcr.microsoft.com/mssql/server:2022-latest`, `mysql:8.4`). No credentials, no
  external server.
- **There is no Snowflake CI job.** `snowflake` is an accepted registry dialect
  that `connections/engine.py` refuses to actually connect for; nothing in CI
  reaches a real Snowflake.

What genuinely remains to decide, then, is narrower: a fork PR gets a read-only
`GITHUB_TOKEN`, and the `docker`, `secret-scan`, `sast`, and `dast` jobs each
pull images or rule packs from the network (Trivy's vulnerability database,
`zricethezav/gitleaks`, Semgrep's `p/...` packs). Decide whether those are
allowed to run untrusted-fork code on `pull_request`, or move to
`pull_request_target` with an explicit approval gate, or require a maintainer to
re-run them from a branch in the base repo — **and document whichever you pick
in this file**, so a first-time contributor whose PR shows red checks knows
whether that is their fault.

### 3. Decide who reviews, and say so

Today there is one maintainer. A PR that sits unacknowledged for two weeks costs
more credibility than a PR that is politely declined on day one. Publish a
target first-response time you can actually meet, and put a `CODEOWNERS` file in
`.github/` if that helps route it. Neither exists yet.

Reviews should check, in this order: the eight non-negotiables in `CLAUDE.md`;
that a test would genuinely fail if the change's guarantee broke; that
dialect/backend variation went through a registry rather than an inline branch;
and only then style.
