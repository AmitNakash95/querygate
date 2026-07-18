<p align="center">
  <img src="public/images/QueryGate_logo_transparent.png" alt="QueryGate" width="640">
</p>

**QueryGate is an agent-safe database access gateway.** It lets you expose a
Postgres or MSSQL database to AI agents over MCP and REST — without ever
letting them run raw SQL.

> **QueryGate runs inside your infrastructure.** It dynamically discovers
> your schema, exposes policy-controlled MCP and REST tools, limits query
> complexity and database load, and keeps credentials and data inside your
> network.

Agents submit a structured, schema-checked query plan (a JSON AST), not a SQL
string. QueryGate validates every table and column against the live
reflected schema, enforces a per-connection policy (allow/deny lists,
complexity caps, row limits, timeouts), compiles the plan to parameterized
SQL through SQLAlchemy Core, executes it under a concurrency guardrail, and
returns a bounded result set. There is no code path — REST or MCP — that
accepts a SQL string.

For commercial positioning, target buyers, pilot structure, packaging, and
go-to-market notes, see the concise
[QueryGate business brief](docs/business/GO_TO_MARKET.md).

## Why raw SQL for agents is dangerous

Handing an LLM a `run_sql(query: str)` tool means trusting a probabilistic
text generator to never produce `DROP TABLE`, never wander into a table it
shouldn't see, never write an unbounded cross join that takes your database
down, and never leak a credential in a stack trace. Prompt injection makes
this worse — a malicious document an agent reads can suggest SQL for it to
run just as easily as a user can. None of the usual mitigations (asking the
model nicely, read-only DB users, query timeouts alone) are structural
guarantees:

- A read-only DB user still lets an agent read every table you didn't mean
  to expose, run an unindexed 12-way join, or exfiltrate an entire table in
  one `SELECT *`.
- Prompt-level instructions ("only query the `orders` table") are guidance,
  not enforcement — nothing stops the next prompt, the next model, or an
  injected instruction from ignoring them.
- A query timeout limits *duration*, not *scope* — it doesn't stop a query
  from touching a table or column it should never have reached at all.

QueryGate's structural guarantee: **the only thing an agent can submit is a
`StructuredQuery` object.** It's a Pydantic model with a fixed shape — no
`sql` field exists anywhere in the schema, so there's no field to inject
into. Every table/column reference in it is checked against the real,
live-reflected schema and an explicit policy before a single SQL statement
is compiled. A bad query gets a validation error before it ever reaches the
database, not a raw error message from the database itself.

## How QueryGate differs from generic MCP SQL connectors

A lot of "MCP + database" integrations are a thin wrapper around
`cursor.execute(model_generated_sql)`, sometimes with a read-only role and a
row cap tacked on. QueryGate is structurally different:

| | Generic MCP SQL connector | QueryGate |
|---|---|---|
| What the agent submits | A SQL string | A validated AST (`StructuredQuery`) |
| Table/column safety | Whatever the DB role allows | Explicit allow/deny policy, checked before compilation |
| Query shape limits | Usually none | Max joins, where-depth, select width, group-by, top-N — policy-enforced |
| Row limits | Often just `LIMIT` appended, sometimes bypassable | Server-clamped, tiered by query shape (aggregate vs. row select) |
| Multi-database support | One connection string, hardcoded | Dynamic connection registry, credential-isolated from schema/tool responses |
| Concurrency/load control | Rare | Per-connection concurrency semaphore + execution timeout |
| Multi-tenant scoping | DIY | Policy-level `mandatory_row_filters` |
| Audit trail | Rare | Every query logged plus an optional persisted, redaction-safe JSONL event |

## Quickstart

```bash
poetry install
cp .env.example .env
docker compose up -d          # starts demo Postgres and the Redis limiter
poetry run python -m querygate.run
```

The example environment selects `CONCURRENCY_BACKEND=redis` and connects to
the Compose service at `redis://localhost:6379/0`, so concurrency limits are
shared across multiple local QueryGate processes. If you intentionally run
without Compose, set `CONCURRENCY_BACKEND=in_process`; that mode is suitable
for a single QueryGate process only.

For development with automatic reload, use `make dev` (or its longer alias,
`make run-dev`). `make run dev` is interpreted by Make as two separate
targets and is not the development-server command.

## Persisted audit events

The example environment enables an append-only JSONL sink at
`var/audit/querygate-audit.jsonl`. Each versioned event includes correlation
id, REST/MCP surface, principal and authentication method, connection,
normalized query shape, policy decision, outcome, timing, row/byte counts,
truncation, and a fixed error category.

Persisted events deliberately exclude SQL and parameters, predicate values,
natural-language intent, exception text, connection strings, and returned
rows. Structured stdout logs remain available for diagnostics and may contain
redacted SQL; literal SQL appears there only when a policy explicitly enables
`log_query_literals`.

In production, mount `AUDIT_JSONL_PATH` on persistent storage and configure
retention/collection with the customer's log agent, SIEM, or `logrotate`.
QueryGate reopens the file for each append so rename-and-recreate rotation
works without a process signal. Files are created with mode `0600`.
`AUDIT_JSONL_FSYNC=true` requests an `fsync` per event for stronger crash
durability at the cost of latency. A sink-write failure emits
`audit.sink.write_failed` to stdout but does not report a successfully executed
database read as failed after the fact.

Then, in another terminal:

```bash
curl http://localhost:8000/api/v1/connections
curl http://localhost:8000/api/v1/demo/tables
curl http://localhost:8000/api/v1/demo/tables/orders
```

See [`examples/rest_calls.md`](examples/rest_calls.md) and
[`examples/mcp_calls.md`](examples/mcp_calls.md) for full request/response
examples, including `query`, `query/explain`, and `query/batch`. For wiring
QueryGate into an agent framework directly instead of raw JSON-RPC/curl, see
[`examples/claude_agent_sdk_integration.py`](examples/claude_agent_sdk_integration.py)
(Claude Agent SDK, `pip install claude-agent-sdk`).

To try it without Docker, seed a local SQLite file instead:

```bash
poetry run python examples/demo_db/seed.py
```

(SQLite is used for examples/tests only — see
["Current limitations"](#current-limitations); production connections are
Postgres or MSSQL.)

## Example connection config

Connections are file-configured, not hardcoded. Connection strings are never
inlined — `${VAR}` is resolved from the environment at load time, so the
file itself is safe to commit:

```yaml
# examples/connections.example.yaml
connections:
  - id: demo
    dialect: postgresql
    connection_string: ${QUERYGATE_DEMO_DB_URL}
    description: "Example demo database — customers/orders/order_items"
    enabled: true
    known_tables: [customers, orders, order_items]
```

Point `CONNECTIONS_FILE` at your own copy. `list_connections` (MCP tool) and
`GET /api/v1/connections` (REST) only ever return a credential-free
projection — `id`, `dialect`, `enabled`, `description` — and only for
connections visible to the authenticated principal. Visibility requires
both the connection profile and that principal's resolved policy to have
`enabled: true`. A hidden connection also behaves like an unknown connection
when addressed directly, so schema/query calls cannot be used to enumerate
internal database names.

## Example policy config

```yaml
# examples/policy.example.yaml
default:
  max_joins: 4
  max_select_columns: 20
  max_where_depth: 4
  max_limit: 100
  max_limit_aggregate: 1000
  timeout_seconds: 30
  max_concurrency: 8

connections:
  demo:
    allowed_tables: [customers, orders, order_items]
    denied_columns:
      customers: [email]
    max_limit: 200
    mandatory_row_filters: []   # e.g. [{table: orders, column: tenant_id, value: 42}]
```

Every table and column reference anywhere in a query — select, join keys,
where, group_by, having, order_by, top_n — is checked against this policy
*before* compilation. A denied column can't be used to filter or sort on
even if it's never selected.

For production, the safest connection-discovery posture is deny by default:

```yaml
default:
  enabled: false

principals:
  reporting-agent:
    analytics:
      enabled: true
  support-agent:
    customer-support:
      enabled: true
```

Unknown principals then see no connections. The same visibility decision is
used by REST, MCP, direct schema/query calls, and cross-connection joins.
Deployment-level `enabled: false` in `connections.yaml` always wins and
cannot be re-enabled by principal policy.

## Example StructuredQuery payload

```json
{
  "from": "orders",
  "select": [
    "orders.customer_id",
    { "fn": "sum", "col": "orders.total_amount", "as": "total_spent" }
  ],
  "group_by": ["orders.customer_id"],
  "order_by": [{ "col": "total_spent", "dir": "desc" }],
  "limit": 5,
  "intent": "top customers by total spend"
}
```

`POST /api/v1/demo/query` with that body — or `execute_structured_query`
over MCP with `connection: "demo"` and the same `query` object — runs it.
Supported: multi-column select, inner/left joins (including cross-connection
joins within a policy `join_group`), nested and/or `where`, `group_by` /
`having`, `order_by`, `limit`/`offset`, aggregate and date-bucket select
items, and `top_n` per-partition ranking (top-N-per-group).

## Example MCP usage

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "execute_structured_query",
    "arguments": {
      "connection": "demo",
      "query": {
        "from": "orders",
        "select": ["orders.id", "orders.status", "orders.total_amount"],
        "where": { "col": "orders.status", "op": "eq", "value": "completed" },
        "limit": 10
      }
    }
  }
}
```

POST this to `/mcp` (Streamable HTTP) with `MCP_ENABLED=true`. Tools:
`list_connections`, `list_tables`, `describe_table`,
`explain_structured_query`, `execute_structured_query`,
`execute_structured_queries` (batch). More examples in
[`examples/mcp_calls.md`](examples/mcp_calls.md).

## Architecture overview

```
Agent (MCP) / Client (REST)
        │  StructuredQuery JSON — never SQL
        ▼
┌───────────────────────────────────────────────────────────────┐
│ querygate/execution/service.py  (StructuredQueryService)       │
│                                                                  │
│  1. validation/policy_validation.py  — caps + allow/deny        │
│  2. validation/schema_validation.py  — reflect + verify exists  │
│  3. compiler/sqlalchemy_compiler.py  — AST → SQLAlchemy Select  │
│  4. execution/concurrency.py         — per-connection semaphore │
│  5. connections/engine.py            — session + guardrails     │
│  6. audit/                           — stdout + persisted JSONL  │
│                                         event, timing, outcome    │
└───────────────────────────────────────────────────────────────┘
        │
        ▼
  Real Postgres / MSSQL database
```

- **`connections/`** — `ConnectionProfile` registry loaded from YAML,
  `${VAR}`-interpolated connection strings, per-dialect engine/session
  lifecycle (`dialects.py` is the only place Postgres/MSSQL-specific SQL
  lives).
- **`schema/`** — table/column reflection with a per-connection metadata
  cache (reflect once, reuse).
- **`query_ast/`** — the `StructuredQuery` Pydantic model. This is the
  entire agent-facing input surface.
- **`policy/`** — `Policy` model + YAML loader (default + per-connection
  overrides).
- **`validation/`** — schema-truth checks (does this table/column exist?)
  and policy checks (is it allowed? within caps?) — deliberately separate
  modules, run in that order, both before compilation.
- **`compiler/`** — turns a validated AST + policy into a SQLAlchemy Core
  `Select`, including dialect-aware date bucketing and top-N ranking.
- **`execution/`** — concurrency guardrail + the `StructuredQueryService`
  that ties validation → compilation → execution → result shaping together.
- **`audit/`** — structured stdout auditing plus a versioned, redaction-safe,
  append-only JSONL event sink.
- **`api/`** and **`mcp/`** — thin transport layers over the same
  `StructuredQueryService`; neither has its own query logic.

## Security model

- **No raw SQL, anywhere.** `StructuredQuery` has no `sql`/`query`-string
  field and rejects unknown fields (`extra="forbid"`) — there's no field to
  smuggle SQL into, and no endpoint that would accept it if there were.
- **Credentials never leave `connections/`.** `ConnectionProfile.connection_string`
  is never returned by any API/MCP response — `list_connections` and
  `GET /api/v1/connections` return `PublicConnectionInfo`, a separate model
  with no such field. This is asserted directly by
  `tests/unit/test_credential_redaction.py` against the live OpenAPI schema
  and MCP tool schemas, not just by convention.
- **Policy is enforced before compilation**, not as a post-hoc filter — a
  denied table/column, an over-cap query, or a disabled connection is
  rejected before a single line of SQL is built.
- **Every identifier is schema-checked**, not agent-asserted — a
  `Table.Column` reference that doesn't exist in the live reflected schema
  is rejected, regardless of what the AST claims.
- **Bounded execution** — every query runs under a per-connection
  concurrency semaphore and a policy-configured timeout; row counts are
  clamped server-side (tiered: lower for row selects, higher for
  aggregates), not left to the caller's `limit`.
- **Auth is pluggable.** `core/auth.py` defines an `Authenticator` interface;
  static API keys and JWKS-verified OAuth/JWT bearer tokens are shared by REST
  and MCP without transport-specific authorization logic.
- **Audit trail** — every query attempt, successful or rejected, is logged
  to stdout and can be persisted as a narrow JSONL event. The persisted event
  contains identity, surface, normalized query shape, policy decision, timing,
  row/byte counts, and error category—never row payloads or query literals.

## Current limitations

Being upfront about what's not done yet:

- **Cross-connection joins** only make sense when both connections are
  visible through one physical database engine (e.g. two MSSQL databases on
  the same server) — the `join_group` policy mechanism gates *intent*, but
  can't make a genuinely separate database server joinable in one SQL
  statement.
- **No RBAC beyond scopes/per-principal policy overrides** — `Principal`
  carries scopes/claims and `policy.yaml`'s optional `principals:` section
  can vary caps/allow-deny/mandatory-row-filters per caller, but there's no
  role hierarchy or admin UI for managing this beyond hand-editing YAML.
- **No stored-procedure catalog** — deliberately out of scope for this
  version; exposing stored procedures safely needs its own cataloging and
  policy-approval mechanism, not a generic pass-through.
- **Audit retention is operator-managed** — QueryGate provides append-only
  JSONL persistence and rotation-friendly writes, but not a WORM store,
  retention scheduler, search UI, or built-in SIEM exporter yet.
- **No write operations** — by design. QueryGate is read-only; there is no
  insert/update/delete path anywhere in the AST or compiler.
- **Distributed concurrency enforcement (Redis-backed) is opt-in** — the
  default is an in-process semaphore, correct for a single instance only;
  set `concurrency_backend: redis` for multi-instance deployments.

MSSQL support (including the query-execution-timeout guardrail) and the
Postgres statement-timeout guardrail are both verified against real
servers, not just unit-tested SQL text — see
`tests/integration/test_mssql_live.py` and
`tests/integration/test_postgres_timeout.py`. OAuth/JWT is implemented
(`core/jwt_auth.py`) alongside static API keys.

See `MIGRATION_REPORT.md` for what was preserved, generalized, or removed
from the internal prototype this was extracted from, and what's recommended
before a commercial release.
