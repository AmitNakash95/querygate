<p align="center">
  <img src="landing/assets/logo-wordmark.svg" alt="QueryGate" width="520">
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

The seed script under `examples/demo_db/` can generate a SQLite fixture for
tests and data inspection, but SQLite is not a supported QueryGate connection.
The runnable quickstart deliberately exercises the same Postgres path used in
production.

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

### Secret-backed connection strings (Vault)

`${VAR}` (a bare environment variable name) always resolves from the
environment, unchanged. A `${scheme:reference}` reference resolves through
a pluggable secret backend instead — today `vault`, reading a HashiCorp
Vault KV v2 secret:

```yaml
connections:
  - id: demo
    dialect: postgresql
    connection_string: ${vault:querygate/demo-db#connection_string}
```

`path` (`querygate/demo-db`) is the KV v2 secret path under
`VAULT_KV_MOUNT` (default `secret`); `field` (`connection_string`) is the
key within that secret's data. Enable it with:

```bash
VAULT_ENABLED=true
VAULT_ADDR=https://vault.internal:8200
VAULT_TOKEN=...
VAULT_KV_MOUNT=secret
VAULT_NAMESPACE=            # Vault Enterprise only, optional
```

Token auth only for this first backend. Resolution is pluggable
(`querygate/secrets/resolvers.py`'s `SecretResolver` protocol — the same
one-method-interface shape as `Authenticator` and `AuditSink`): a future
backend (AWS Secrets Manager, GCP Secret Manager, ...) is a new resolver
class plus one registration line, never a change to how
`connections.yaml` is loaded, to `config_reload`, or to
`querygate-validate-config`. `${...}` references resolve fresh on every
load — including a hot reload via `POST /api/v1/admin/reload-config` — so a
rotated Vault secret takes effect on the next reload without a restart, no
env var/process restart required the way a bare `${VAR}` reference does.

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

## Config-governance API (staged versions, apply, rollback)

`POST /api/v1/admin/reload-config` (above) reloads whatever
`connections.yaml`/`policy.yaml`/`catalog.yaml` currently contain on disk —
the right fit for infra-as-code deployments that edit those files directly.
For teams that want to submit config changes over the API instead — with
validation, staged review, full version history, and rollback — a separate
`/api/v1/admin/config/*` surface layers on top of the same reload mechanism,
never bypassing it:

```bash
# See what's currently active
curl -H "Authorization: Bearer $KEY" $HOST/api/v1/admin/config/current

# Dry-run a candidate without persisting anything
curl -X POST -H "Authorization: Bearer $KEY" $HOST/api/v1/admin/config/validate \
  -d '{"policy_yaml": "default:\n  enabled: true\n  max_joins: 2\n"}'

# Preview the submitted configuration documents. A caller who also has read
# scope sees changed/unchanged; write-only sees submitted/inherited so the
# preview cannot be used as an equality oracle. No content is returned.
curl -X POST -H "Authorization: Bearer $KEY" $HOST/api/v1/admin/config/preview \
  -d '{"policy_yaml": "default:\n  enabled: true\n  max_joins: 2\n"}'

# Stage it as a new version (only the fields you send change; everything
# else inherits from the current active version)
curl -X POST -H "Authorization: Bearer $KEY" $HOST/api/v1/admin/config/versions \
  -d '{"policy_yaml": "...", "description": "tighten max_joins for pilot customer X"}'
# -> {"id": "7", "status": "staged", ...}

# Apply it — validates once more, then reloads exactly like
# /admin/reload-config does, and records the new active version
curl -X POST -H "Authorization: Bearer $KEY" $HOST/api/v1/admin/config/versions/7/apply

# Roll back — the same "apply" endpoint, targeting an older version id.
# QueryGate never rewrites history: every version that ever existed stays
# inspectable via GET /admin/config/versions/{id}.
curl -X POST -H "Authorization: Bearer $KEY" $HOST/api/v1/admin/config/versions/1/apply
```

Every validate/preview/stage/apply/rollback is attributed to the calling principal
and recorded in the same audit trail as query execution (a `config.governance`
event — action, version id, outcome, actor — never the YAML content itself,
which stays only in the version store). The first call to any `/admin/config/*`
endpoint bootstraps version `"1"` from whatever `connections.yaml`/
`policy.yaml`/`catalog.yaml` the deployment started with, so "current active
version" always means something. Gated behind two scopes, matching the
read/write split most admin APIs use: `admin:config:read` (list/inspect
versions) and `admin:config:write` (validate/preview/stage/apply/rollback).

## Example schema catalog (optional)

Raw reflection tells an agent that `customers.email` exists and is a
`VARCHAR`; it doesn't say what the table represents, how it joins to other
tables, or that the column is sensitive. A curated schema catalog fills that
gap:

```yaml
# examples/catalog.example.yaml
version: 1
connections:
  demo:
    tables:
      customers:
        description: "One row per registered customer."
        aliases: ["clients", "accounts"]
        default_aggregation: "count(customers.id)"
        relationships:
          - to_table: orders
            column: id
            to_column: customer_id
            description: "A customer's orders."
        columns:
          email:
            description: "Customer email address."
            sensitivity: pii
            allow_samples: false
```

Set `CATALOG_FILE` to point at your own copy (unset by default — a
deployment with no curated catalog behaves identically). `describe_table`
(MCP and REST) merges this into its response as a `catalog` object per
table and per column when configured, `null` otherwise. It is purely
descriptive: the catalog never affects query validation, compilation, or
execution, and it is filtered by the same policy as everything else —
denied columns never reach catalog lookup at all (they're already excluded
from the response), and a relationship hint pointing at a table the caller's
resolved policy denies is dropped, so curated metadata can never disclose
more than ordinary schema discovery already allows. Reloadable without a
restart via the same `POST /api/v1/admin/reload-config` endpoint used for
connections/policy; validated by `querygate-validate-config --catalog-file
...` alongside the other two files.

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
`execute_structured_queries` (batch), plus the product-guide and access tools
described below. More examples in
[`examples/mcp_calls.md`](examples/mcp_calls.md).

## Production deployment

The quickstart above is for local development. For an actual deployment,
[`deploy/`](deploy/) has two verified reference stacks — a production-ish
Docker Compose file and a Helm chart — covering app + Redis (distributed
concurrency), config/secrets mounting, Prometheus scrape config,
liveness/readiness probes, and a runbook for config reloads, secret
rotation, and rollback:

```bash
# Docker Compose
cd deploy/docker-compose && cp .env.production.example .env.production
# ...fill in .env.production, adapt config/connections.yaml and config/policy.yaml...
docker compose -f docker-compose.prod.yml --env-file .env.production up -d

# Helm
helm install querygate deploy/helm/querygate \
  --set image.repository=your-registry/querygate --set image.tag=0.1.0 \
  -f my-values.yaml
```

Both were verified against a real deployment — not just rendered — before
being committed: the Compose stack ran end-to-end against a real Postgres
(including the audit sink and the optional bundled Prometheus actually
scraping `/metrics`), and the Helm chart was installed into a real `kind`
cluster with 2 replicas, a real query, and the non-root `securityContext`
confirmed against the image's actual runtime user rather than assumed. See
[`deploy/README.md`](deploy/README.md) for the full comparison and
[`deploy/runbook.md`](deploy/runbook.md) for operational procedures.

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
  `${...}`-interpolated connection strings, per-dialect engine/session
  lifecycle (`dialects.py` is the only place Postgres/MSSQL-specific SQL
  lives).
- **`secrets/`** — pluggable `${scheme:reference}` secret resolution
  (`SecretResolver` protocol): the built-in `env` backend plus an optional
  `vault` backend, selectable per-reference without touching how
  connections are loaded or reloaded.
- **`schema/`** — table/column reflection with a per-connection metadata
  cache (reflect once, reuse).
- **`query_ast/`** — the `StructuredQuery` Pydantic model. This is the
  entire agent-facing input surface.
- **`policy/`** — `Policy` model + YAML loader (default + per-connection
  overrides).
- **`catalog/`** — optional curated schema-catalog overlay (descriptions,
  aliases, relationship hints, sensitivity labels) merged into
  `describe_table`, filtered by the same policy as everything else.
- **`validation/`** — schema-truth checks (does this table/column exist?)
  and policy checks (is it allowed? within caps?) — deliberately separate
  modules, run in that order, both before compilation.
- **`compiler/`** — turns a validated AST + policy into a SQLAlchemy Core
  `Select`, including dialect-aware date bucketing and top-N ranking.
- **`execution/`** — concurrency guardrail + the `StructuredQueryService`
  that ties validation → compilation → execution → result shaping together.
- **`audit/`** — structured stdout auditing plus a versioned, redaction-safe,
  append-only JSONL event sink, shared by both query execution and
  config-governance events.
- **`admin/`** — config-governance version store and orchestration: staging,
  applying, and rolling back connections/policy/catalog versions on top of
  `config_reload.py`'s existing swap mechanism.
- **`help/`** — packaged, versioned technical guide; deterministic offline
  search; config-field reference; caller-scoped access explanation; and
  redacted admin configuration summaries shared by REST and MCP.
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
- **Curated catalog metadata is policy-filtered too** — an optional schema
  catalog (business descriptions, relationship hints, sensitivity labels)
  never affects query enforcement, and never discloses a table/column a
  denied caller couldn't already see through ordinary schema discovery.
- **Bounded execution** — every query runs under a per-connection
  concurrency semaphore and a policy-configured timeout; row counts are
  clamped server-side (tiered: lower for row selects, higher for
  aggregates), not left to the caller's `limit`.
- **Auth is pluggable.** `core/auth.py` defines an `Authenticator` interface;
  static API keys and JWKS-verified OAuth/JWT bearer tokens are shared by REST
  and MCP without transport-specific authorization logic.
- **Secret resolution is pluggable and fails loud, not quiet.** `secrets/`
  defines a `SecretResolver` interface; a Vault error surfaces as a clear
  config-load failure that never echoes the configured Vault token or
  Vault's own response text — only what was being looked up and why it
  failed.
- **Audit trail** — every query attempt, successful or rejected, is logged
  to stdout and can be persisted as a narrow JSONL event. The persisted event
  contains identity, surface, normalized query shape, policy decision, timing,
  row/byte counts, and error category—never row payloads or query literals.
- **Config changes are versioned, attributed, and never silently applied.**
  Every `/admin/config/*` validate/preview/stage/apply/rollback is attributed to the
  calling principal, gated behind `admin:config:read`/`admin:config:write`,
  recorded as its own audit event (never the YAML content), and re-validated
  immediately before it takes effect — a version that fails validation is
  never activated, even if it validated when it was first staged.
- **Adversarially tested boundary** — denied identifiers cannot be smuggled
  through filters, joins, grouping, ordering, or ranking; undeclared tables
  cannot enter an implicit `FROM`; unexpected backend errors are masked; and
  MCP rejects unapproved Host headers. See [the threat model](docs/THREAT_MODEL.md)
  and run `make test-security`.

## Built-in product guide

QueryGate includes a canonical technical guide for the installed version. It
works offline, does not require a model provider or a healthy database, and
uses deterministic lexical retrieval rather than sending documentation or
deployment context to an external search service.

Public REST guide endpoints include:

- `GET /api/v1/help/search?q=configure+policy`
- `GET /api/v1/help/topics/{topic_id}`
- `GET /api/v1/help/setup-checklist?profile=production`
- `GET /api/v1/help/config-fields/{app|connection|policy|catalog}/{field}`
- `GET /api/v1/help/errors/{error_code}`

`GET /api/v1/help/my-access` is authenticated and returns only the current
caller's scopes, capability flags, and policy-visible connections.
`GET /api/v1/help/configuration` additionally requires
`admin:config:read`; its structured summary omits raw YAML, connection
strings, secret values/references, table/column policy identifiers, catalog
identifiers, free-form configuration descriptions, and other principal subjects.

The equivalent MCP tools are `search_querygate_guide`,
`get_querygate_guide_topic`, `get_querygate_setup_checklist`,
`explain_querygate_config_field`, `explain_querygate_error`,
`describe_my_querygate_access`, and `inspect_querygate_configuration`.
Guide responses include the applicable QueryGate version and a citation to
their packaged canonical topic. Product guidance never mutates configuration;
changes continue through the scoped validate/preview/stage/apply/rollback
governance API.

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
- **Config-governance has no approval workflow yet** — a caller with
  `admin:config:write` can stage and immediately apply a version in one
  session; there's no second-approver/four-eyes requirement, scheduled
  apply, or detailed semantic diff. The preview reports changed/unchanged only
  with read scope; write-only callers see submitted/inherited so write scope
  cannot become read scope. No admin UI either — the governance mutation API
  is REST-only for now.
- **No write operations** — by design. QueryGate is read-only; there is no
  insert/update/delete path anywhere in the AST or compiler.
- **Distributed concurrency enforcement (Redis-backed) is opt-in** — the
  default is an in-process semaphore, correct for a single instance only;
  set `concurrency_backend: redis` for multi-instance deployments.
- **Security review is first-party** — the repository includes a maintained
  threat model and adversarial regression suite, but has not yet undergone an
  independent penetration test or formal compliance certification.

MSSQL support (including the query-execution-timeout guardrail) and the
Postgres statement-timeout guardrail are both verified against real
servers, not just unit-tested SQL text — see
`tests/integration/test_mssql_live.py` and
`tests/integration/test_postgres_timeout.py`. The real-Postgres load/soak
harness also proves the observed database concurrency cap, overflow rejection,
queued completion, and timeout cancellation under concurrent REST traffic; see
[`docs/LOAD_TESTING.md`](docs/LOAD_TESTING.md). OAuth/JWT is implemented
(`core/jwt_auth.py`) alongside static API keys.

For the repeatable source/package and container release gates, see
[`docs/RELEASING.md`](docs/RELEASING.md). Historical extraction notes are
kept outside the product surface under `archive/extraction/`.

<p align="center">
  <img src="landing/assets/favicon.svg" alt="QueryGate app icon" width="64">
</p>
