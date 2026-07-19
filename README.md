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

### Pre-execution cost estimation (Postgres)

Row limits, timeouts, and concurrency caps are all reactive — they bound a
query only once it's already running. `Policy.max_estimated_rows` /
`max_estimated_cost` (both unset/disabled by default) add a proactive check
in front of that: when set, `execute()` asks Postgres's own planner to plan
— never run — the compiled query via `EXPLAIN (FORMAT JSON)`, and rejects
the query before it touches real data if the planner's row-count or cost
estimate is past the configured threshold, with a message that tells the
agent to narrow the query rather than just "denied":

```yaml
default:
  max_estimated_rows: 1000000
  max_estimated_cost: 100000   # Postgres's own arbitrary planner-cost units
  cost_estimation_mode: enforce   # or "observe" — see below
```

This is Postgres-only for now. MSSQL's estimated-plan equivalent
(`SET SHOWPLAN_XML ON`) can't be composed as a prefix on an already-compiled
statement the way Postgres's `EXPLAIN` can — it needs its own dedicated
connection lifecycle — so setting these fields on an MSSQL connection is
accepted but has no effect (see `execution/cost_estimation.py` and TODO.md
item 26). `explain_structured_query` (MCP) and `POST .../query/explain`
(REST) never open a database session at all (by design — it stays a pure,
always-cheap compile preview), so this check runs only on
`execute_structured_query`/`POST .../query`, not `explain`.

Because Postgres's planner-cost units aren't portable across schemas or
hardware, a threshold copied from documentation is a guess, not a
measurement. `cost_estimation_mode: observe` (default `enforce`) lets you
calibrate first: the estimate is still computed and compared against the
threshold, but a would-be rejection is only recorded — as a
`cost_estimation.observed_would_reject` structured log line and the
`querygate_cost_estimation_would_reject_total{connection}` counter — never
raised, so real traffic keeps flowing while you watch what the threshold
would actually do. Switch the connection to `enforce` once you're confident
in the number.

The gate is also fail-open and observable about it: an EXPLAIN that can't be
compiled, executed, or parsed degrades to "not enforced for this query"
rather than blocking traffic on an edge case, and every such case increments
`querygate_cost_estimation_unavailable_total{connection,reason}`
(`compile_failed` / `explain_failed` / `plan_parse_failed`) alongside a
warning log line — alert on that counter climbing, since it means the gate
has silently stopped evaluating queries on that connection.
`querygate_cost_estimation_attempts_total{connection}` is the matching
denominator for computing a fail-open rate.

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

# Simulate a decision against the draft before staging anything. Unset
# documents inherit from the active version; the target principal is
# independent of the caller's own identity. Returns a typed allow/deny,
# effective guardrails, and mandatory-filter readiness — never resolved
# secrets, static filter/claim values, query predicates, or compiled SQL.
curl -X POST -H "Authorization: Bearer $KEY" $HOST/api/v1/admin/config/simulate \
  -d '{"policy_yaml": "default:\n  enabled: true\n  max_joins: 2\n",
       "principal": "reporting-agent", "connection": "analytics", "table": "orders"}'

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

Every validate/preview/simulate/stage/apply/rollback is attributed to the calling
principal and recorded in the same audit trail as query execution (a
`config.governance` event — action, version id, outcome, actor — never the
YAML content itself, which stays only in the version store). The first call to
any `/admin/config/*` endpoint bootstraps version `"1"` from whatever
`connections.yaml`/`policy.yaml`/`catalog.yaml` the deployment started with, so
"current active version" always means something. Gated behind two scopes,
matching the read/write split most admin APIs use: `admin:config:read`
(list/inspect versions) and `admin:config:write` (validate/preview/stage/
apply/rollback). `simulate` requires *both* scopes at once — it echoes back
semantic policy detail like a read endpoint, but also resolves caller-supplied
config/secret references like a write endpoint, so a read-only principal can't
turn it into a secret-existence oracle. `simulate` loads the candidate
documents into an isolated, temporary registry/policy/catalog context (reusing
the real loaders and validation logic) and never touches the live
registry/policy singletons or the version store, so concurrent production
requests and other simulations can't see or affect it.

### Browser admin control plane

Start QueryGate and open [`http://localhost:8000/admin/`](http://localhost:8000/admin/).
The page is served by the same process and talks only to same-origin QueryGate
APIs; there is no separate frontend service or app-owned database. Authenticate
with a bearer token carrying `admin:config:read` to inspect versions, browse the
redaction-safe audit stream, and test the active policy as another principal.
Add `admin:config:write` to edit policy/config documents, dry-run validation,
stage a version, and activate or roll it back.

The control plane provides:

- policy-filtered connection, table, column, and catalog-sensitivity review;
- a visual default/connection/principal policy designer plus raw YAML editing;
- policy simulation for a target principal, connection, table, columns,
  scalar claims, and optionally a structured-query shape, without executing a
  query. A caller with both config scopes simulates the local *uncommitted
  draft* in an isolated context (item 39); a read-only caller simulates the
  currently active policy instead. Neither returns resolved secrets, static
  row-filter/claim values, query predicates, or compiled SQL — only a typed
  allow/deny, effective guardrails, and mandatory-filter claim readiness;
- active-versus-draft document diffs, dry-run validation and redacted preview;
- immutable version history with explicit activation and rollback confirmation;
- a catalog review workspace — connection/status/source/object-type filters over
  the quarantined proposal queue from item 32B, side-by-side proposed-versus-
  published fields (read live via the same policy-filtered `describe_table` the
  schema-review tab uses), provenance/confidence/freshness indicators,
  edit/approve/reject/publish actions gated on their own least-privilege scopes
  and only shown when the proposal's status makes that action legal, a
  publish-conflict preview, and connection-scoped catalog version rollback;
- filtered, newest-first browsing of persisted JSONL query/config/catalog audit
  events (when `AUDIT_SINK_BACKEND=jsonl`).

The UI does not create a second configuration path: every stage/apply/rollback
still goes through the config-governance API described above, and
`querygate-validate-config` plus file-based reloads remain supported for
infrastructure-as-code deployments. The catalog workspace is the same way —
every edit/approve/reject/publish/rollback goes through item 32B's existing
REST routes; bulk operations, export/import, and triggering draft generation
from the browser remain CLI/REST-only for now (TODO item 38 phase 2). A
write-only principal can validate and stage submitted content but cannot use
the UI to read the active documents; normal operation therefore grants both
config scopes to the human admin role.

### Admin connection-status API

The public `GET /health` returns only aggregate healthy/unhealthy/unknown
counts, so it can't disclose database topology to an unauthenticated
orchestrator probe. Administrators still need to know *which* connection is
failing and since when — without reading process logs or seeing a raw driver
error (which can embed the host, port, database, or username). `GET
/api/v1/admin/connections`, gated by a dedicated `admin:connections:read`
scope, returns a credential-free per-connection view built from the same
background `HealthMonitor` the readiness probe already uses:

```bash
curl -H "Authorization: Bearer $KEY" $HOST/api/v1/admin/connections
# -> [{"connection_id": "demo", "dialect": "postgresql", "enabled": true,
#      "status": "healthy", "last_checked": 1752960000.0,
#      "last_success": 1752960000.0, "latency_ms": 1.25,
#      "schema_reflected": true, "failure_category": null}, ...]
```

`status` is `healthy`/`degraded`/`disabled`/`unknown`; a failure surfaces only
as a stable `failure_category` (`authentication`/`unreachable`/`timeout`/
`error`) classified from the exception *type*, never its message — the raw
driver error stays in stdout logs only, and no connection string is ever
returned. `admin:connections:read` is intentionally separate from the config
scopes: seeing whether a database is reachable is a different privilege from
reading or changing what QueryGate connects to. The rate-limited "test now"
re-check and a browser workspace that renders this view are TODO item 43
phase 2.

## Example schema catalog (optional)

Raw reflection tells an agent that `customers.email` exists and is a
`VARCHAR`; it doesn't say what the table represents, how it joins to other
tables, or that the column is sensitive. A curated schema catalog fills that
gap:

```yaml
# examples/catalog.example.yaml
version: 2
connections:
  demo:
    tables:
      customers:
        description: "One row per registered customer."
        aliases: ["clients", "accounts"]
        provenance:
          source_class: verified
          status: verified
          source_evidence:
            - {kind: manual, reference: "catalog.yaml"}
          created_by: "data-governance"
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
from the response), and a relationship hint pointing at a denied table—or
using a denied join column on either end—is dropped.

Catalog format version 2 adds deterministic stable entry IDs and durable
provenance to every table, column, and relationship: source class/evidence,
`draft`/`verified`/`rejected`/`stale`/`archived` status, confidence, catalog
and schema version, freshness, actors/timestamps, and server-derived
precedence. Legacy version-1 files still load; omitted provenance is upgraded
in memory as manually verified content. Verified content outranks observed,
inferred, and learned proposals, and semantic-memory merging can never change
a sensitivity label.

`GET /api/v1/{connection}/catalog/search?q=customer+revenue` and the MCP
`search_catalog` tool return compact deterministic matches with those
citations. QueryGate applies the requesting principal's policy before
candidate text is tokenized, ranked, counted, traversed, or byte-limited.
Rejected/archived entries are not searchable; stale and draft entries are
labeled explicitly. Search is metadata-only, bounded to at most 20 hits and
16 KiB, and never touches database rows. Public citations hash evidence
references and omit creator/approver/model identities; the durable catalog
retains those privileged provenance fields for later governed review.

Version 2 can also persist row-free observed-schema snapshots. The
deterministic fingerprint/diff engine covers tables, columns/types/
nullability, primary keys, foreign keys, indexes, and hashed (never raw)
database comments; possible renames are reported as candidates rather than
silently asserted. Phase 32A-2 adds an opt-in background scanner that updates
those snapshots and marks only affected catalog entries and draft proposals
stale. Unaffected active entries are rebound to the new fingerprint, and a
same-schema retry is a no-op. Persistence uses an adjacent cross-process lock
and atomic replacement; refresh failure does not block schema discovery or
query execution.

Semantic-memory enrichment is disabled by default. The only provider modes
shipped in 32A are `disabled` and `manual`; neither has a network adapter or
model call. Manual structured input produces separate inferred draft
proposals, never edits/publishes the verified table/column/relationship entry,
and is not indexed or returned to agents. Proposal content structurally cannot
contain policy, mandatory-filter, sensitivity, or sampling fields. Enable the
operator workflow explicitly:

```bash
export CATALOG_FILE=/config/catalog.yaml
export SEMANTIC_MEMORY_PROVIDER=manual              # required for generate-drafts
export SEMANTIC_MEMORY_REFRESH_ENABLED=true         # optional background scanning
export SEMANTIC_MEMORY_REFRESH_INTERVAL_SECONDS=300
export SEMANTIC_MEMORY_REFRESH_MAX_TABLES=500

querygate-semantic-memory refresh --connection demo
querygate-semantic-memory generate-drafts \
  --input-file examples/catalog_drafts.example.yaml
querygate-semantic-memory evaluate
```

The manual batch must cite the exact current schema fingerprint. Generation
IDs are durable idempotency keys: an identical retry adds nothing; reuse with
different content is rejected. See `examples/catalog_drafts.example.yaml`.
The packaged version-1 benchmark fixes thresholds in code before evaluation
(>=0.85 expected-hit recall, >=0.80 relationship recall, 1.0 stale detection,
>=0.50 discovery-call reduction, zero policy violations). Its deterministic
current result is 1.0/1.0/1.0/0.75/0 and the command exits non-zero on a
regression.

Automatic refresh requires `CATALOG_FILE` to be writable and persistent. If
several QueryGate replicas refresh the same catalog they must share that file
and its adjacent `.lock`; read-only ConfigMap mounts should keep refresh
disabled and run the explicit refresh CLI against a governed writable copy.
The atomic writer emits canonical YAML and does not preserve YAML comments;
keep operational guidance in catalog fields and retain the file in normal
versioned backup/config-governance workflows.

The catalog remains reloadable without a restart through the same
`POST /api/v1/admin/reload-config` endpoint used for connections/policy and is
validated by `querygate-validate-config --catalog-file ...` alongside the
other two files. Empty/disabled semantic memory does not affect ordinary
schema validation or query execution.

### Governed catalog review, publish, and rollback

A generated draft proposal (above) is a privileged, non-agent-visible record
until an authorized reviewer moves it through a deny-by-default state
machine: `pending` → `approved` (or `rejected`, with a required reason) →
`published`. A draft can never publish itself — approval and publication are
always two separate, actor-attributed calls. Publishing merges the proposal's
description/aliases/default_aggregation into a real catalog entry using the
same precedence gate 32A-1 uses for legacy content: if the target already
carries different, human-verified content for the same field, publication is
refused as a reviewable conflict rather than silently overwritten. A draft's
content model has no sensitivity, sampling, policy, or mandatory-filter
field at all, so publishing can never touch connection access, row filters,
or sensitivity labels.

```bash
# List this connection's proposals awaiting review
curl -H "Authorization: Bearer $KEY" \
  "$HOST/api/v1/admin/catalog/demo/proposals?review_status=pending"

# Preview what publishing would look like for a specific principal, without
# persisting anything
curl -H "Authorization: Bearer $KEY" \
  "$HOST/api/v1/admin/catalog/demo/proposals/$PROPOSAL_ID/preview?principal_subject=reporting-agent"

# Approve, then publish — two separate, actor-attributed calls
curl -X POST -H "Authorization: Bearer $KEY" \
  "$HOST/api/v1/admin/catalog/demo/proposals/$PROPOSAL_ID/approve"
curl -X POST -H "Authorization: Bearer $KEY" \
  "$HOST/api/v1/admin/catalog/demo/proposals/$PROPOSAL_ID/publish"
# -> {"outcome": "published", "version_id": "1", "entry_id": "urn:querygate:catalog:..."}

# Roll back a publish — restores the prior field values (or removes an
# entry this publish created), refusing if the entry changed since
curl -X POST -H "Authorization: Bearer $KEY" \
  "$HOST/api/v1/admin/catalog/demo/versions/1/rollback"
```

The same workflow is available without any REST client via
`querygate-semantic-memory`: `list-proposals`, `show-proposal`,
`edit-proposal`, `approve-proposal`, `reject-proposal`, `publish-proposal`,
`preview-publish`, `list-versions`, `show-version`, `rollback-version`,
`export`, `import`, `delete-proposal`, and `delete-version`. Nine
independent least-privilege scopes gate the surface — `catalog:generate`,
`catalog:review` (read), `catalog:edit`, `catalog:approve`,
`catalog:reject`, `catalog:publish`, `catalog:rollback`, `catalog:export`,
and `catalog:delete` — none implied by another, matching the read/write
separation the config-governance API above already uses. Every generation
and state transition is recorded as a redaction-safe `catalog.governance`
audit event through the same sink as query execution and config governance:
stable ids, actor, outcome, and duration — never draft text, descriptions,
or raw catalog YAML. Publishing creates a durable, catalog-file-scoped
version-history record; `GET .../versions` reports a metadata-only diff
(which field names changed), and `GET .../versions/{id}` returns the full
before/after content to a `catalog:review`-scoped caller.

Bulk approve/reject/delete are bounded to 50 proposal ids per call and
validate every id before mutating any of them, so a single invalid id in a
batch fails the whole call rather than partially applying it.

### Export, import, backup/restore, and retention

`GET .../export` (`catalog:export`) returns a self-contained,
connection-scoped snapshot — published entries, quarantined proposals with
their full review history, generation records, version history, and the
schema snapshot. `POST .../import` (the same `catalog:export` scope — one
bidirectional data-portability privilege) is a full, destructive replace of
that connection's governed content from a bundle, serving both migrating
governed state between environments and restoring from a saved export.
Version and generation ids are file-global, not per-connection, so import
re-numbers/de-duplicates them against the target catalog's current content
and remaps every internal cross-reference — an operator-chosen generation id
like `"onboarding-1"` reused across two environments can never collide with,
or corrupt, an unrelated connection's history:

```bash
# Back up (or migrate) everything governed for this connection
curl -H "Authorization: Bearer $KEY" "$HOST/api/v1/admin/catalog/demo/export" > demo-backup.json

# Restore it — destructively replaces "demo"'s governed content only
curl -X POST -H "Authorization: Bearer $KEY" \
  -d @demo-backup.json "$HOST/api/v1/admin/catalog/demo/import"
```

`DELETE .../proposals/{id}` and `DELETE .../versions/{id}` (`catalog:delete`)
prune only terminal-state records: a rejected proposal deletes standalone; a
published-and-since-rolled-back proposal deletes together with its now-
reverted publish record and the rollback record that reverted it (so no
record is left referencing a deleted one); a standalone rollback record
deletes on its own. A proposal that's still pending/approved, or published
with its publish still live, can't be deleted — reject or roll it back
first — so deletion can never destroy the only record of why current
catalog content exists. `POST .../proposals/bulk-delete` is the same bounded,
atomic bulk pattern as bulk approve/reject. Version history is otherwise
bounded to 2000 entries per catalog file.

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
`list_connections`, `list_tables`, `describe_table`, `search_catalog`,
`explain_structured_query`, `execute_structured_query`,
`execute_structured_queries` (batch), plus the product-guide and access tools
described below. More examples in
[`examples/mcp_calls.md`](examples/mcp_calls.md).

## Agent-visible capacity waiting

`Policy.max_concurrency` and `concurrency_wait_seconds` already bound how
many queries run at once per connection and how long an over-cap request
waits for a slot before it's rejected. `queue_mode` and `wait_timeout_seconds`
(both optional, request-level — REST query params on `POST .../query` and
`.../query/batch`; extra MCP tool arguments on `execute_structured_query`/
`execute_structured_queries`) make that existing wait caller-tunable:

- `queue_mode=fail_fast` — reject immediately if the connection is at
  capacity, no waiting at all.
- `queue_mode=wait` (default) with `wait_timeout_seconds` — wait up to that
  many seconds for a slot. A caller may request a **shorter** wait than the
  operator's `concurrency_wait_seconds`; it can never request a longer one —
  `wait_timeout_seconds=3600` against a 5-second policy ceiling still only
  waits 5 seconds. Omitting both preserves the original behavior exactly.

```bash
curl -X POST "http://localhost:8000/api/v1/demo/query?queue_mode=fail_fast" \
  -H "Content-Type: application/json" \
  -d '{"from": "orders", "select": ["orders.id"], "limit": 10}'
```

Every call — successful or capacity-rejected — gets a stable `admission_id`
and how long it waited (`queue_wait_ms`). REST returns these as response
headers (`X-QueryGate-Admission-Id`, `X-QueryGate-Admission-State` —
`completed` or `capacity_timeout` — and `X-QueryGate-Queue-Wait-Ms`) so the
existing `422 {"detail": "too many concurrent ..."}` rejection body never
changes shape; a successful `StructuredQueryResult`/`BatchQueryItemResult`
also carries `admission_id`/`queue_wait_ms` fields directly. MCP's
`MCPErrorResult` gains the same `admission_id`/`admission_state`/
`queue_wait_ms` fields for a capacity rejection. `querygate_queue_depth`
(current waiters, single-process visibility) and
`querygate_queue_wait_seconds` (histogram, by outcome) are exported
alongside the existing concurrency metrics.

This is phase 1 of TODO.md item 35 — a synchronous, caller-tunable version of
the wait that already existed. MCP progress notifications, a REST
`202`-plus-cancel contract, mid-queue cancellation, and a `429`+`Retry-After`
evaluation are phase 3.

### Queue-depth pressure controls and cross-replica visibility

`Policy.max_queue_depth`/`max_queue_depth_per_principal` (both optional,
unset/unlimited by default — phase 2 of item 35) cap how many callers may be
*waiting* for a slot at once, separately from `max_concurrency` (which caps
how many may *run*). Without a cap, a burst of `queue_mode=wait` callers
against an already-saturated connection can itself become a resource-
exhaustion vector — each waiter parked for up to `concurrency_wait_seconds`.
A caller whose admission would exceed either cap is rejected immediately
(`queue_wait_ms: 0`), with a distinct `admission_state` of `"queue_full"` so
it can be told apart from a genuine wait-timeout (`"capacity_timeout"`) in
REST headers, MCP fields, metrics, and the audit event:

```yaml
connections:
  demo:
    max_queue_depth: 50              # this connection's whole waiting queue
    max_queue_depth_per_principal: 5 # one caller's share of it
```

When `concurrency_backend: redis` is selected, both the cap and the
`querygate_queue_depth` gauge are enforced/computed against the same Redis
every replica shares — the same sorted-set-plus-lease design
`max_concurrency`'s own Redis limiter already uses — so the depth is the
true cross-replica count, not one process's own local tally.

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
- **`catalog/`** — versioned semantic-catalog overlay (descriptions, aliases,
  relationship hints, sensitivity labels, durable provenance, schema
  fingerprints/diffs, and compact retrieval) merged into `describe_table`
  and filtered before search/ranking by the same policy as everything else.
  `governance.py` implements the deny-by-default draft review/edit/approve/
  reject/publish/rollback state machine, publish-time conflict detection
  against verified content, durable version history, connection-scoped
  export/import (backup/restore), and terminal-state-only retention/
  deletion — all through the same `CatalogFileRepository` lock as schema
  refresh and draft generation.
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
- **`admin_ui/`** — same-origin browser control plane for schema review, visual
  policy design/simulation, config diffs, version activation/rollback, and
  redaction-safe audit browsing; all mutations reuse `admin/`'s API workflow.
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
  Semantic search filters candidates—including both relationship columns—
  before ranking or counting and returns provenance/status/freshness on every
  hit.
- **Bounded execution** — every query runs under a per-connection
  concurrency semaphore and a policy-configured timeout; row counts are
  clamped server-side (tiered: lower for row selects, higher for
  aggregates), not left to the caller's `limit`.
- **Proactive cost estimation, not just reactive caps (Postgres)** — an
  optional `max_estimated_rows`/`max_estimated_cost` policy gate plans the
  compiled query with Postgres's own `EXPLAIN` before running it, and
  rejects likely full scans or join explosions before they ever touch real
  data instead of only bounding them once already running.
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
- **Supply-chain transparency for the exact dependency set shipped** — every
  `make release-check` generates a CycloneDX SBOM and a `pip-audit`
  vulnerability report scoped to `poetry.lock`'s locked `main` group (not an
  unpinned resolve), plus SHA-256 checksums for the built artifacts. Any known
  vulnerability without a reviewed, justified entry in
  `security/dependency-audit-allowlist.json` fails the release
  (deny-by-default). See [`docs/RELEASING.md`](docs/RELEASING.md#software-bill-of-materials-and-dependency-audit).

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
- **Pre-execution cost estimation is Postgres-only** — `max_estimated_rows`/
  `max_estimated_cost` (above) have no effect on an MSSQL connection yet;
  MSSQL's estimated-plan mechanism needs its own connection lifecycle that
  hasn't been built (TODO item 26 phase 2).
- **Distributed concurrency enforcement (Redis-backed) is opt-in** — the
  default is an in-process semaphore, correct for a single instance only;
  set `concurrency_backend: redis` for multi-instance deployments.
- **Agent-visible capacity waiting still has no cancellation or async
  contract** — a caller can choose `queue_mode=fail_fast`/a shorter
  `wait_timeout_seconds`, gets a stable `admission_id` back, and (with
  `concurrency_backend: redis`) a queue-depth cap and gauge that are
  accurate across replicas (above), but there's still no MCP progress
  notification, REST `202`-plus-cancel contract, mid-queue cancellation, or
  `429`+`Retry-After` evaluation (TODO item 35 phase 3). The in-process
  (non-Redis) `querygate_queue_depth` gauge remains single-process
  visibility only, like `querygate_concurrency_in_use`.
- **Semantic memory enrichment is opt-in and relationship-learning only** —
  item 32's governed adaptive loop is complete, including redaction-safe usage
  signals, confidence/decay/conflict-gated learned relationship proposals,
  background processing, and the existing review/publish/rollback workflow.
  There is still no embedding index or live model provider, and usage alone
  does not generate free-form table or column descriptions. Learned proposals
  remain hidden from agents until explicitly approved and published by an
  authorized reviewer through the workflow above.
- **Security review is first-party** — the repository includes a maintained
  threat model and adversarial regression suite, but has not yet undergone an
  independent penetration test or formal compliance certification.

MSSQL support (including the query-execution-timeout guardrail) and the
Postgres statement-timeout guardrail are both verified against real
servers, not just unit-tested SQL text — see
`tests/integration/test_mssql_live.py` and
`tests/integration/test_postgres_timeout.py`. The real-Postgres load/soak
harness also proves the observed database concurrency cap, overflow rejection,
queued completion, timeout cancellation, `queue_mode=fail_fast` never
waiting, and a caller-shortened `wait_timeout_seconds` being honored under
concurrent REST traffic; see [`docs/LOAD_TESTING.md`](docs/LOAD_TESTING.md).
OAuth/JWT is implemented (`core/jwt_auth.py`) alongside static API keys.

For the repeatable source/package and container release gates, see
[`docs/RELEASING.md`](docs/RELEASING.md). Historical extraction notes are
kept outside the product surface under `archive/extraction/`.

<p align="center">
  <img src="landing/assets/favicon.svg" alt="QueryGate app icon" width="64">
</p>
