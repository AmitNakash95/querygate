# Old-identity cleanup report

Verification that no product-facing surface still reads as
`il-backoffice-api` / IL BackOffice / Shany-specific.

## Search performed

```bash
rg -i "il-backoffice|backoffice|shany|duns|dbconst|interop|dun.?and.?bradstreet" \
   src/ examples/ tests/ README.md pyproject.toml Dockerfile docker-compose.yml \
   Makefile scripts/ .env.example
```

**Result: 0 matches.** (`rg` exit code 1 — no matches found.)

A broader repo-wide sweep (same pattern, plus `il_backoffice_api`,
`grammarql`, `\bduns\b`), excluding only `.venv/`, `.git/`, `archive/`, and
`poetry.lock`:

```bash
rg -i "il-backoffice|il_backoffice|backoffice|shany|\bduns\b|dbconst|interop|dun.?and.?bradstreet|grammarql" \
   --glob '!.venv' --glob '!.git' --glob '!archive' --glob '!poetry.lock' -l
```

**Result: 0 matches.**

`poetry.lock` checked separately for the old package name:

```bash
rg -i "il-backoffice|il_backoffice" poetry.lock
```

**Result: 0 matches** (and `poetry.lock` was regenerated fresh from the new
`pyproject.toml`, so it only reflects `querygate`'s actual dependency set).

## What's intentionally exempted

`archive/legacy-project-docs/` — the old project's development-workflow
scaffolding (`memory/`, `agent_state/`, `journal/`, `workflows/`, `skills/`,
`templates/`, `AGENT.md`, `CONSTRAINTS.md`) — still contains old-identity
references throughout, by design. It was moved there rather than deleted
(reversible, and explicitly permitted by the task's "unless retained under
an archive/ note" clause) and is not part of the product surface: it's not
imported by any code, not referenced by `pyproject.toml`, not built into the
Docker image (`Dockerfile` only copies `src/` and `examples/`), and not
served by the running application. Confirmed present (sanity check that the
exclusion is actually doing something, not just absent by coincidence):

```bash
$ rg -il "backoffice|shany" archive/ | head -5
archive/legacy-project-docs/AGENT.md
archive/legacy-project-docs/journal/daily_log.md
archive/legacy-project-docs/agent_state/next_actions.md
archive/legacy-project-docs/agent_state/context_snapshot.md
archive/legacy-project-docs/CONSTRAINTS.md
```

## Renames applied

| Old | New |
|---|---|
| Package/module `il_backoffice_api` | `querygate` |
| Poetry project name `il-backoffice-api` | `querygate` |
| FastAPI `title="il-backoffice-api"` | `title="QueryGate"` |
| MCP server `name="il-backoffice-api"` | `name="querygate"` |
| `DataBase`/`DataBaseName` enum (`ShanyContent`/`Db`/`DbConst`/`InterOp`) | Dynamic `ConnectionRegistry` (file-configured, deployment-specific ids) |
| `poetry run start` script → `il_backoffice_api.run:main` | `querygate` script → `querygate.run:main` |
| Docker base image `${IL_AWS_PRD_ECR}/il-devops-files` (private registry) | `python:3.11-slim` (public) |
| `docker-compose.yml` service `postgres` / db `backoffice` | service `querygate-demo-db` / db `querygate_demo` |
| `.env` vars `SHANY_REST_API_*`, `*_DB_CONNECTION_STRING` (×5, hardcoded) | `CONNECTIONS_FILE`, `POLICY_FILE`, `API_KEYS`, `MCP_*` |
| README ("IL-BackOffice-API Service", GrammarQL template docs) | Full QueryGate product README |

## Deliberately not renamed

Nothing — `git log` on this repository has a single "Initial commit" that
predates this migration and was never pushed/shared (see `git status` at
migration start: everything was untracked). There is no historical commit
carrying the old name that needed a "renamed from" note; the working tree
itself is the full history here.
