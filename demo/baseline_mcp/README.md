# baseline_mcp — the "gate OFF" control condition

> **This is not QueryGate. It is the thing QueryGate exists to replace.**
> A deliberately unsafe, generic "Postgres MCP server" with an `execute_sql`
> tool — raw SQL, no policy, no limits, no auth, no redaction — built only to
> stand next to the real product in a live A/B demo (`demo/SPEC.md`, Act 1).
> **Never deploy this. Never import it from `src/querygate/`. Never point it
> at anything but the throwaway `querygate_demo_pitch` database.**

## What it is

`server.py` is a real MCP server (Streamable HTTP transport, `mcp` SDK v2,
the same `MCPServer` class QueryGate's own server uses) exposing three tools
against Postgres, connected as the genuinely read-only `agent_ro` role:

- `execute_sql(sql: str)` — runs arbitrary SQL, returns `columns`, `rows`,
  `row_count`. This is the whole point: it is exactly the capability every
  "connect an agent to your database" MCP server on the market ships today.
- `list_tables()` — every table in the `public` schema, no filtering.
- `describe_table(table: str)` — every column of a table, no filtering.

It is genuinely functional, not a strawman: real SQL execution, real
Postgres errors surfaced verbatim (never swallowed or reshaped — the `mcp`
SDK itself turns any exception a tool raises into
`CallToolResult(is_error=true, ...)`, preserving the original message inside
it), real column/row data back over real Streamable HTTP JSON-RPC.

Every corner it cuts is intentional and commented in `server.py` with
`DELIBERATELY NAIVE: ...`, naming the specific QueryGate mechanism that
exists instead:

| Cut corner | What QueryGate does instead |
|---|---|
| No statement timeout | `connections/dialects.py` session guardrails (`SET LOCAL statement_timeout`) |
| No row cap | `Policy.max_limit`, enforced by construction in the compiled query |
| No table/column allow-deny list | `validation/policy_validation.py`, checked before any DB touch |
| No auth | `MCPAuthMiddleware` (API keys / JWKS-verified JWTs) in front of every tool |
| No redaction | Policy bounds what's reachable at all; audit events never carry SQL/rows/predicates |

Read-only at the database-role level (`agent_ro` has
`default_transaction_read_only = on`) — and that is precisely the demo's
point: identical database privileges to QueryGate's own connection, but with
no policy gate in front, `execute_sql` still hands back every row of
`employees.ssn`/`employees.salary`, and a cross-join scenario can still peg
the database's CPU. Read-only stops nothing here.

## Why it exists

The partner demo (`demo/SPEC.md`) is a three-act A/B comparison: identical
agent intent, run once through this server ("gate OFF") and once through
QueryGate's real MCP server ("gate ON"). This is the control condition — it
has to be real for the comparison to mean anything, and it has to be
impossible to mistake for the product.

## Running it

```bash
demo/baseline_mcp/run.sh
```

which sets the required opt-in env var and starts the server on
`http://127.0.0.1:8811/mcp`. Equivalent manual invocation:

```bash
QUERYGATE_DEMO_UNSAFE_BASELINE=i-understand poetry run python demo/baseline_mcp/server.py
```

It refuses to start (non-zero exit, specific stderr message) unless **all**
of the following hold — the startup guard in `server.py`'s
`check_startup_guard()`:

1. `QUERYGATE_DEMO_UNSAFE_BASELINE=i-understand` is set.
2. The DSN host (from `PITCH_AGENT_RO_DSN`, or the local default) resolves
   to `localhost`/`127.0.0.1`, and is a single host — not a libpq
   comma-separated host list, which could fail over to a non-local host.
3. The DSN database name is exactly `querygate_demo_pitch`.
4. The DSN scheme is `postgresql`/`postgres` — including the SQLAlchemy-style
   `postgresql+asyncpg://` form `demo/config/env.demo` sets the same
   `PITCH_AGENT_RO_DSN` var to for QueryGate's own connection loader; this
   server normalizes that down to the plain form `asyncpg` expects.

By default it connects to `postgresql://agent_ro:agent_ro_demo_pw_only@127.0.0.1:5544/querygate_demo_pitch`
(matching `demo/db/02_roles.sql` and `demo/db/docker-compose.demo.yml`).
Override with `PITCH_AGENT_RO_DSN` if your local `demo/db` stack differs — the
same env var `demo/config/env.demo` sets for QueryGate itself.

Requires the `demo/db` Postgres stack running and seeded
(`docker compose -f demo/db/docker-compose.demo.yml up -d`).

## Testing the guard

```bash
poetry run pytest demo/baseline_mcp/test_guard.py -v
```

This file is intentionally **outside** `tests/` — `pyproject.toml` sets
`testpaths = ["tests"]`, so it is never collected by a bare `pytest` or
`pytest -m unit` run at the repo root; it only runs when invoked directly.

## Packaging / import boundary

`demo/` is excluded from the wheel, sdist, and container image, and nothing
under `src/querygate/` imports anything from `demo/`. This directory has no
`__init__.py` and is not a package — it is a standalone script meant to be
run, never imported by product code.
