<p align="center">
  <img src="landing/assets/logo-wordmark.svg" alt="QueryGate" width="520">
</p>

**QueryGate is an agent-safe database access gateway.** It lets you expose a
Postgres, MSSQL, or MySQL database to AI agents over MCP and REST — without
ever letting them run raw SQL.

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
| Rate limits / cost budget | DIY | Per-principal rolling-window request & response-byte quotas (429 + `Retry-After`) |
| Multi-tenant scoping | DIY | Policy-level `mandatory_row_filters` |
| Column-level masking | DIY (or none) | Per-principal `column_masks` (hash/null/last-N/bucket), applied in the compiled SQL |
| Audit trail | Rare | Every query logged plus an optional persisted, redaction-safe JSONL event — optionally a tamper-evident hash-chained ledger with per-query receipts |

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

With the server running, get your first governed query in under 5 minutes:

```bash
export QUERYGATE_URL=http://localhost:8000
export QUERYGATE_TOKEN=<a bearer token from your .env's API_KEYS>
querygate-quickstart demo
```

`querygate-quickstart` (TODO.md item 146) reflects a connection's schema and
prints 3 ready-to-run example queries — a plain select, a filtered select,
and an aggregate — scoped to columns the catalog doesn't mark sensitive, each
with a REST `curl`, an MCP tool-call, and a Python SDK snippet. It composes
existing read-only discovery routes; it never writes anything.

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

### Tamper-evident ledger + per-query receipts

Set `AUDIT_SINK_BACKEND=jsonl_chained` for a **tamper-evident hash-chained
ledger**: each event is wrapped in a chain envelope linking it to the previous
record's hash, so any later edit, deletion, reordering, or insertion is
detectable. The embedded event body is identical and just as redaction-safe —
the chain adds only a sequence number and hashes.

```bash
querygate-audit verify var/audit/querygate-audit.jsonl        # 0 iff intact
querygate-audit receipt var/audit/querygate-audit.jsonl <event_id>   # portable compliance receipt
```

Set `AUDIT_LEDGER_HMAC_KEY` to make the chain HMAC-SHA256 (unforgeable by anyone
with file access but not the key; pass the same key to `verify --hmac-key-env`).
Left unset the chain is SHA-256 — it still catches corruption, reordering, and
mid-file deletion, with full tamper-evidence resting on externally anchoring the
head hash (`verify --expected-head`, which also catches records dropped from the
end). One logical writer owns the chain head, so run a single replica or give
each replica its own ledger file. This complements — never replaces — shipping
events to retained/WORM storage or a SIEM.

### Compliance-grade WORM retention (S3 Object Lock)

Set `AUDIT_SINK_BACKEND=jsonl_chained_s3_worm` to additionally archive the
same redaction-safe events to S3 Object Lock — genuinely undeletable for the
configured retention window, including by the AWS account root under
`AUDIT_WORM_RETENTION_MODE=COMPLIANCE` (the default). It **composes with**
the hash-chained ledger above, never replaces it: the local file is
unaffected, and the two controls answer different questions — the chain
proves nobody edited what was kept, WORM proves you can *produce* it on
demand for the retention window even if the local file is later rotated or
lost. Requires `AUDIT_WORM_S3_BUCKET` (with Object Lock enabled on the
bucket — an S3 prerequisite this feature can't turn on for you) and
`AUDIT_WORM_S3_REGION`.

Archival is buffered and flushed off the request path (`AUDIT_WORM_FLUSH_INTERVAL_SECONDS`,
default 60s) as one batched object per flush, never one object per event.
It fails open by design: a flush failure never blocks or fails the query
that triggered the event — the local chain already captured it — but the
batch is re-queued for retry (not dropped) and increments
`querygate_audit_worm_flush_failures_total`, which you should alert on. Only
a sustained outage past `AUDIT_WORM_MAX_BUFFERED_EVENTS` drops the oldest
buffered events, visibly, via `querygate_audit_worm_buffer_dropped_total`.

`GET /api/v1/admin/observability/worm-search` (`admin:audit:worm-search`
scope — deliberately separate from `admin:observability:read`) is the
QueryGate-native managed search over that archive: bounded time-range,
`event_type`/`connection_id`/`principal_id` filters, and cursor-based
pagination, so "every query against `pii_customers` in the last 18 months"
is answerable even after the local hash-chained file has long since rotated
that window out. `start_time`/`end_time` are required on every request (no
"search everything" mode), the window is capped
(`AUDIT_WORM_SEARCH_MAX_WINDOW_DAYS`, default 730), and one request's S3
scan is bounded by `AUDIT_WORM_SEARCH_MAX_OBJECTS_SCANNED` and
`AUDIT_WORM_SEARCH_REQUEST_TIMEOUT_SECONDS` — an over-wide/missing range is
rejected outright, a bound hit mid-scan degrades to a truncated, resumable
page rather than a slow or unbounded scan.

### Prove the boundary: the adversarial security benchmark

A fixed, versioned attack corpus, run against the real request-pipeline
guardrails entirely offline (no DB, no network, no LLM) — the reproducible,
publishable form of the adversarial "five-minute demo". It reports QueryGate's
catch rate next to a structurally-modeled raw-SQL-passthrough baseline and
discloses the documented inference residuals it does *not* block.

```bash
querygate-security-benchmark run          # human-readable report (exit 0 iff clean)
querygate-security-benchmark run --json    # machine-readable report
querygate-security-benchmark list          # list the corpus cases
```

Methodology, results, and the factual Google MCP Toolbox comparison live in
[`docs/business/SECURITY_BENCHMARK.md`](docs/business/SECURITY_BENCHMARK.md).

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
the runnable integration examples — each registers QueryGate as a
Streamable-HTTP MCP server and lets the framework's agent discover and call the
tools itself:

- [`examples/claude_agent_sdk_integration.py`](examples/claude_agent_sdk_integration.py) — Claude Agent SDK (`pip install claude-agent-sdk`)
- [`examples/langchain_integration.py`](examples/langchain_integration.py) — LangChain / LangGraph (`pip install langchain-mcp-adapters langgraph`)
- [`examples/llamaindex_integration.py`](examples/llamaindex_integration.py) — LlamaIndex (`pip install llama-index-tools-mcp`)
- [`examples/openai_function_calling_integration.py`](examples/openai_function_calling_integration.py) — OpenAI Chat Completions function-calling (`pip install openai`)

### Typed Python query builder

Rather than hand-writing the `StructuredQuery` JSON above, construct it with a
typed, autocompleting builder shipped in the package
(`from querygate.client import Query`):

```python
from querygate.client import Query, agg, col, desc

body = (
    Query.from_("orders")
    .join("customers", on=("orders.customer_id", "customers.id"))
    .select("customers.name", agg.count("*", as_="order_count"))
    .where(col("customers.country") == "GB")
    .where(col("orders.status") == "completed")
    .group_by("customers.name")
    .order_by("order_count", desc=True)
    .limit(5)
    .to_dict()          # -> the exact JSON to POST to /api/v1/<connection>/query
)
```

The builder is a **pure client-side convenience**: it constructs the same
Pydantic models the server validates, so an illegal query (a `count(*)` with
`distinct`, a self-join missing an alias) raises locally with the same error
the server would return — and whatever it emits is still fully policy-,
schema-, and guardrail-checked server-side before any row is touched. It adds
no trust and cannot bypass any guardrail. Predicate operators (`==`, `>`,
`.in_`, `.between`, `.is_null`), boolean groups (`and_`/`or_`/`not_`),
aggregates (`agg.*`), `date_bucket`, `string_agg`/`array_agg`,
`percentile_cont`, scalar functions (`fn`/`fn_select`), `case`/`when`,
computed expressions (`expr`/`expr_fn`/`cast` and Python `+ - * /` over
columns; item 100), window functions (`window`/`frame`; item 101), `top_n`,
set operations (`.union()`/`.intersect()`/`.except_()`; item 104),
and a bounded `IN (subquery)` (a predicate's `value_subquery` — a
nested `StructuredQuery`, not raw SQL; uncorrelated, single-connection,
depth-capped, with all caps summed tree-wide; item 97) cover the AST.
Runnable end-to-end demo:
[`examples/client_sdk_python.py`](examples/client_sdk_python.py)
(`python examples/client_sdk_python.py` to print bodies; add `--send` with
`QUERYGATE_API_KEY` set to run them against a local server). A TypeScript
sibling and a standalone dependency-light distribution are planned (TODO item
51 phase 2).

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

That reload is normally operator-triggered. Set
`CREDENTIAL_LEASE_REFRESH_ENABLED=true` (requires `VAULT_ENABLED=true`) to
make it automatic instead: a background monitor polls every `${vault:...}`
reference for a reported lease expiry and proactively reloads before it runs
out, closing the outage window a short-TTL dynamic credential would
otherwise fall into between reloads. It only ever triggers the same
reload/dispose path above, earlier — see `CREDENTIAL_LEASE_CHECK_INTERVAL_SECONDS`
and `CREDENTIAL_LEASE_REFRESH_MARGIN_SECONDS` in `.env.example`.

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
    min_group_size: 5           # k-anonymity: suppress aggregate groups < 5 rows
```

Every table and column reference anywhere in a query — select, join keys,
where, group_by, having, order_by, top_n — is checked against this policy
*before* compilation. A denied column can't be used to filter or sort on
even if it's never selected. See [docs/INFERENCE_RISKS.md](docs/INFERENCE_RISKS.md)
for the full analysis of what this closes and what stays a residual.

`min_group_size` is an optional **k-anonymity guardrail** (off by default):
when set, the compiler injects `HAVING count(*) >= k` into every aggregate
query, so a caller can't single out an individual by aggregating over a
razor-thin filter — a `count(*)` over a group backed by fewer than *k* rows is
suppressed rather than returned. It is the aggregate analog of a mandatory row
filter (policy-driven, injected, non-removable) and applies only to aggregate
queries; it closes single-query singling-out, not multi-query differencing.
Because the floor counts *joined* rows, a join that can match many rows per row
would inflate the count — so while `min_group_size` is set, such a join is refused
on an aggregate query rather than silently answered (item 118). Joining onto the
target table's primary key or a unique column matches at most one row and is
unaffected.

### Column-value masking (not just allow/deny)

Allow/deny is binary — a column is either fully readable or fully hidden.
Some columns need to stay *usable* without exposing the raw value: show the
last four digits of a card, bucket an age, hash an identifier so it joins
consistently but never reveals itself. `Policy.column_masks` adds an optional
per-connection **`column_mask`** primitive that transforms a value **in the
compiled `SELECT`**, resolved per principal through the same policy merge — so
one caller can see raw values while another sees them masked on the very same
connection:

```yaml
connections:
  demo:
    column_masks:
      customers:
        - {column: national_id, kind: hash}       # deterministic one-way hash
        - {column: phone, kind: last, length: 4}  # reveal trailing 4 chars, mask the rest
      orders:
        - {column: total_amount, kind: bucket, bucket_size: 100}  # round down to a 100-wide bucket
```

Four kinds ship: `hash`, `null` (fully blanked but still selectable, unlike a
denied column), `last` (reveal trailing `length` chars), and `bucket` (round
a numeric value down to a `bucket_size` multiple). The transform is rendered
by each dialect's `DialectAdapter.column_mask` (Postgres `md5`/`right`, MSSQL
`HASHBYTES`/`RIGHT`, `null`/`bucket` dialect-universal) and labeled with the
original output name, so the response shape is unchanged.

A masked column may appear **only as a bare `select` item**. Using it in a
`where`, `join`, `order_by`, or `group_by` position — or nesting it inside a
function/CASE/aggregate — is **rejected**, not silently unmasked: projection-only
masking would otherwise leave an inference channel (`where ssn = '<guess>'` and
watch whether a row comes back), the same side-channel the allow/deny walk
already closes. Masking is audited distinctly from denial — the success event
carries `masked_columns` (output names only, never the pre-mask value) so
operators can tell "masked" access apart from "denied" in the one stream.

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
item 26). `run_structured_queries(mode="explain")` (MCP) and
`POST .../query/explain` (REST) never open a database session at all (by
design — it stays a pure, always-cheap compile preview), so this check
runs only on `mode="execute"` (the default)/`POST .../query`, not
`mode="explain"`.

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

### Caller-facing verdict — would this be allowed?

`POST .../query/verdict` (REST) and `run_structured_queries(mode="verdict")`
(MCP) answer one question — *"would this `StructuredQuery` be allowed for me,
right now?"* — without executing it and without the debug-level detail
`mode="explain"` gives. It's built for a low-trust caller that needs a yes/no,
not a schema tour: an MCP gateway deciding whether to forward a request, a
proxy, or a CI check validating a query before it ships (TODO.md item 133).

```bash
curl -X POST $HOST/api/v1/demo/query/verdict \
  -H "Authorization: Bearer $KEY" \
  -d '{"from": "customers", "select": ["customers.id"], "limit": 10}'
# {"allowed": true, "reason": null, "message": null, "plan": null}
```

A denial always reports the same generic `reason: "not-available-to-you"` —
it never distinguishes "on your policy's deny list" from "doesn't exist in
the schema" from "references a join connection you can't see". Policy is
checked before schema on every request, so returning either failure's real
message (or even just which one fired) would let a caller enumerate
identifiers and reconstruct both the schema and the policy boundary one query
at a time, the same discovery-oracle class `docs/THREAT_MODEL.md` QG-19/QG-24
already close on the admin simulation and "my access" surfaces.
`mode="explain"` is deliberately left as-is (it still echoes the real
validation message) — it's a debugging tool for a caller who already has
identifier-level access, a different posture than this endpoint's "may I"
question. The response *bodies* are identical across every denial cause;
response *timing* is not — schema validation does strictly more work than
policy validation, so a sophisticated caller could in principle time the
difference. Closing that would need a constant-time response floor, which
this endpoint doesn't implement.

A verdict answers policy-and-schema shape only, up through the same
`_validate_and_compile` seam `execute`/`explain` share — it does not evaluate
the approval gate (item 92) or the cost-estimation gate. A query reported
`allowed: true` can still be paused for human approval, or refused by a
cost-estimate cap, when actually executed.

The compiled plan (SQL + touched tables) is omitted by default for the same
reason — set `verdict_include_plan: true` on a connection's policy to opt in:

```yaml
policy:
  verdict_include_plan: true   # off by default — the plan itself is a discovery channel
```

Unlike `explain` (deliberately free — a pure, always-cheap compile preview),
a verdict call still reflects the schema (a cold-cache reflection is a real DB
round trip) and is audited unconditionally — never silently skipped, the same
posture `execute` takes (the audit event just omits execution-only fields
like row/byte counts, since no rows are ever returned). It also consumes one
unit of the caller's query quota **when the connection's policy configures
one** (`max_requests_per_window`/`max_response_bytes_per_window`, both off by
default, same as every other read) — under the default policy it is not
metered, the same as `execute` would be.

### In-query human-in-the-loop approval

Some reads shouldn't run unattended just because they pass policy — a query
whose pre-execution estimate is very large is the exfiltration leg of the
"lethal trifecta". QueryGate can **pause** such a read and require a human's
approval before it executes, gating on *what the query would actually touch*
rather than after the fact (TODO.md item 92). Opt-in per policy, off by default:

```yaml
policy:
  approval_max_estimated_rows: 100000    # softer than max_estimated_rows above
  approval_max_estimated_cost: 50000     # Postgres planner-cost units
  approval_sensitivities: [pii]          # or internal/confidential — see below
```

There are two triggers, and either fires the gate:

- **Cost/size** (`approval_max_estimated_*`): set *below* the hard
  `max_estimated_*` caps to mean "ask a human" rather than "refuse". Postgres
  only (reuses the same estimate as cost estimation).
- **Sensitivity** (`approval_sensitivities`): a query that references a column —
  or its table — carrying one of these catalog sensitivity labels (`pii`,
  `confidential`, `internal`) requires approval *regardless of size*, on any
  dialect. It reads only the descriptive catalog's static label (never a row
  value) and checks **every** referenced column (a sensitive column used in a
  `WHERE`/join/grouping trips it too, not just one you `select`).

When a query trips either trigger, execution is paused with
`428 Precondition Required` carrying a query **fingerprint** and the **reasons**.
An approver holding the `query:approve` scope (deliberately *not* the querying
agent — see `docs/SCOPE_CATALOG.md`'s "Query Approver" role) grants a token:

```bash
# 1. execution pauses -> 428 { "fingerprint": "...", "reasons": [...] }
# 2. approver (query:approve) mints a short-lived, query-bound token:
curl -X POST -H "Authorization: Bearer $APPROVER_KEY" \
  $HOST/api/v1/<connection>/query/approve -d '<the exact same StructuredQuery JSON>'
# -> { "fingerprint": "...", "approval_token": "..." }
# 3. caller re-submits the identical query with the token:
curl -X POST -H "Authorization: Bearer $CALLER_KEY" \
  -H "X-QueryGate-Approval: <approval_token>" \
  $HOST/api/v1/<connection>/query -d '<the exact same StructuredQuery JSON>'
```

The token is a stateless HMAC (set `APPROVAL_TOKEN_HMAC_KEY`) bound to the exact
query fingerprint and a short expiry — it can't be forged, can't be replayed
against a *different* query, and can't be replayed indefinitely; any
missing-key/forged/expired/mismatched token fails closed. An interactive **MCP
elicitation** approval channel also ships — an MCP caller can approve inside the
same session (`Context.elicit`) instead of the REST round-trip. A deployment
that sets no approval thresholds or sensitivities is completely unaffected.

### Governed writes — preview, execute, approve, diff

QueryGate is read-only until an operator explicitly turns writes on: every
deployment starts with `WritePolicy.enabled=false`, and there is still no
`sql`/raw-DML field anywhere in either transport. When writes are enabled, a
caller can only ever submit a typed `InsertStatement`/`UpdateStatement`/
`DeleteStatement` — never a SQL string — and an `UPDATE`/`DELETE` *cannot be
constructed without a `WHERE`* (an unqualified mutation is impossible by
construction, not just discouraged). Every write is checked against the same
deny-by-default `WritePolicy` used for reads (`allowed_tables`,
`allowed_operations`, `denied_write_columns`, `max_affected_rows`) before it
is compiled to bound-parameter SQLAlchemy Core DML.

```bash
# 1. Preview: compiles, but never mutates. include_diff=true adds the exact
#    before/after row values (computed inside a transaction that always rolls
#    back), bounded by max_diff_rows and masking-aware.
curl -X POST -H "Authorization: Bearer $CALLER_KEY" \
  "$HOST/api/v1/<connection>/write/preview?include_diff=true" \
  -d '{"op":"update","table":"orders","set":{"status":"shipped"},"where":{"col":"orders.id","op":"eq","value":42}}'
# -> { "executed": false, "affected_rows": 1, "sql": "...", "diff": {"before":[...],"after":[...]} }

# 2. Execute: one transaction — counts matched rows, aborts before mutating if
#    over max_affected_rows, runs the approval gate if the row count crosses
#    approval_max_affected_rows, commits, and rolls back whole on any error.
curl -X POST -H "Authorization: Bearer $CALLER_KEY" \
  "$HOST/api/v1/<connection>/write/execute" -d '<the same write JSON>'
# -> { "executed": true, "affected_rows": 1 }
```

**No undo — by design.** QueryGate deliberately does not snapshot rows to offer
a rollback. Keeping a second copy of your data outside its source of truth is
exactly the footprint an operational-database gateway should not add. The safety
story is *prevention*, not reversal: the diff preview and the approval gate put a
human in front of the exact change before it commits, deny-by-default and the
affected-row cap make a catastrophic-shape write impossible, and every write is
attributed and audited.

**Both transports, same guarantees.** REST is the routes above; MCP has one
`run_structured_writes` tool (`mode=preview|execute`, batch, `include_diff`,
plus in-session elicitation approval instead of the REST 428/approve
round-trip). Every write is audited exactly like a read — redaction-safe,
dual-identity (on-behalf-of), tamper-evident when chained audit is enabled —
recording the operation, table, and affected-row count, **never** a value or
row. A deployment that never sets `WritePolicy.enabled=true` is unaffected:
every write endpoint returns a clean policy rejection, and the database stays
read-only in practice as well as by default.

The claim is deliberately **governed**, not "safe autonomous writes": what's
guaranteed by construction (no raw DML, every target policy-checked, no
unqualified UPDATE/DELETE, bounded rows) rules out the catastrophic-shape
class; preview and approval are what make the remaining question — "did the
agent intend *this* change" — reviewable and attributable rather than
unattended.

### Per-principal rate limits / query quotas

`max_concurrency` bounds how many queries a principal can have *in flight at
once*; it says nothing about how many it can run *over time*. A well-behaved
agent that never exceeds its concurrency limit can still fire tens of
thousands of sequential queries an hour, exhausting a database's capacity or a
customer's cost budget. `Policy.max_requests_per_window` /
`max_response_bytes_per_window` (both unset/disabled by default) add the
missing *rate* dimension — a rolling-window cap on request count and on total
response bytes, scoped per principal per connection:

```yaml
default:
  max_requests_per_window: 600           # at most 600 queries …
  max_response_bytes_per_window: 104857600  # … and 100 MiB of results …
  quota_window_seconds: 60               # … per principal per 60s window
```

The quota is checked **before** a query is queued or touches the database, so
a rate-limited caller never consumes a concurrency slot or a DB session. Only
authenticated callers are throttled — an anonymous request has no principal to
attribute usage to, so it's skipped rather than sharing one bucket. Because
per-principal overrides merge over the default, you can throttle a single
noisy agent without touching everyone else:

```yaml
principals:
  batch-agent:
    "*":
      max_requests_per_window: 60
      quota_window_seconds: 60
```

A caller past either cap gets a distinct, contextful rejection — REST answers
**429** with a `Retry-After` header; MCP returns a **`RATE_LIMITED`** error —
naming which cap tripped and roughly when to retry. Quota rejections are
audited exactly like other policy denials (`policy_decision: denied`) and
counted under `querygate_queries_rejected_total{reason="quota"}` plus a
dedicated `querygate_query_quota_rejections_total{connection,quota_kind}`
(`requests` / `bytes`). Like `max_concurrency`, enforcement is in-process:
correct for a single instance, but the window is per-replica under a load
balancer (a Redis-backed cross-replica quota is TODO.md item 50 phase 2).
`explain` is never quota-gated — it compiles a preview without executing.

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

### Delegated agent identity (on-behalf-of)

When an agent queries *for a specific human*, "who did this?" has two answers:
the agent that made the call and the person it acted for. QueryGate carries
both. A verified JWT's RFC 8693 `act` claim is mapped into a delegation chain
on the `Principal` — `subject` is the human, `actor` is the agent (with a
nested `delegated_by` chain for multi-hop delegation). Because per-principal
policy resolution already keys off `Principal.subject`, **the human's** policy
and `mandatory_row_filters` apply automatically — an agent acting for a
support rep is bound by that rep's access, not the agent's own. Every audit
event records both identities (`actor_id` + `delegation_chain`, identities
only — the redaction guarantee is unchanged), so the trail reads "Agent A, on
behalf of User Z, under User Z's policy." This is opt-in via `jwt_act_claim`
and off unless a token actually carries an `act` claim.

The mounted MCP surface can additionally run as an **OAuth 2.0 resource
server** (`mcp_oauth_resource_server_enabled`, requires `jwt_enabled`, off by
default). It publishes RFC 9728 protected-resource metadata at
`/.well-known/oauth-protected-resource<MCP_MOUNT_PATH>`, enforces RFC 8707
audience binding (a token's `aud` must include `mcp_resource_identifier`,
blocking confused-deputy reuse of a token minted for another audience), and
answers a missing/insufficient credential with an RFC 6750
`WWW-Authenticate: Bearer …, resource_metadata="…"` challenge so a client can
run the token exchange / scope step-up. Static API keys still work and skip
audience binding (an out-of-band trust with no `aud`) but still pass the scope
gate.

The metadata's RFC 9728 `scopes_supported` advertises QueryGate's **entire**
scope vocabulary (not just the MCP access gate), so an IdP can import it and
mint usable tokens with no manual typing. For humans, `docs/SCOPE_CATALOG.md`
(generated from `core/scopes.py` by `make scope-catalog`, drift-tested) lists
every scope, the action it gates, and **recommended role bundles**
(Analyst / Operator / Config Governor / Catalog Author / Catalog Admin /
Catalog Data Steward) to paste into your IdP's role definitions. Data-access
grants stay in `policy.yaml` keyed by `sub`/claim — never scopes — so
provisioning a user reduces to role assignment.

## Config-governance API (staged versions, apply, rollback)

`POST /api/v1/admin/reload-config` (above) reloads whatever
`connections.yaml`/`policy.yaml`/`catalog.yaml`/`templates.yaml` currently
contain on disk — the right fit for infra-as-code deployments that edit those
files directly.
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

# See what a draft would actually change about resolved access — not a YAML
# line diff, but typed tightening/loosening/neutral changes to connection
# visibility, guardrail caps, table/column access, mandatory-filter
# requirements, and join groups. Static filter values, secrets, and predicate
# values are never included. Requires both config scopes, like simulate.
curl -X POST -H "Authorization: Bearer $KEY" $HOST/api/v1/admin/config/diff \
  -d '{"policy_yaml": "default:\n  enabled: true\n  max_joins: 2\n"}'

# Same candidate-document shape as /diff, but aggregated across every
# principal configured in policy.yaml's `principals:` section, not just the
# default/connection baseline — a syntactically tiny per-principal override
# can matter more than a large default-policy edit, and vice versa. Ranks
# access-expanding changes (a removed mandatory row filter ranked above a
# newly visible table/column, ranked above a loosened guardrail) so a
# reviewer sees the riskiest, most-affected-caller changes first, and tags
# each one "baseline" (affects every principal without an override) or
# "principal" (affects only that one caller). Bounded work: evaluates up to
# 100 configured principals and reports `analysis_incomplete` with a specific
# reason if that cap — or the top-25 highest-risk cap — is reached, rather
# than silently omitting impact. Requires both config scopes, like diff.
curl -X POST -H "Authorization: Bearer $KEY" $HOST/api/v1/admin/config/blast-radius \
  -d '{"policy_yaml": "default:\n  enabled: true\n  max_limit: 50\n"}'

# Optional, on-demand: check that query templates' tables/columns actually
# exist in the live database (the fast dry-run above is offline and doesn't).
# Best-effort per template: ok / issues / connection_unavailable / unreachable —
# an unreachable database is reported, never a hard failure. Requires both
# config scopes, like /simulate and /diff.
curl -X POST -H "Authorization: Bearer $KEY" $HOST/api/v1/admin/config/check-template-schema \
  -d '{"templates_yaml": "templates:\n  - id: ...\n    ..."}'

# Stage it as a new version (only the fields you send change; everything
# else inherits from the current active version). templates_yaml is a governed
# document too (item 48 phase 2): stage a curated query-template change here and
# it goes live only on apply — the same validate/stage/apply/rollback path,
# never a direct template-mutation endpoint.
curl -X POST -H "Authorization: Bearer $KEY" $HOST/api/v1/admin/config/versions \
  -d '{"templates_yaml": "templates:\n  - id: orders_for_customer\n    ...",
       "description": "add the orders_for_customer curated template"}'
# -> {"id": "7", "status": "staged", ...}

# Apply it — validates once more, then reloads exactly like
# /admin/reload-config does, and records the new active version
curl -X POST -H "Authorization: Bearer $KEY" $HOST/api/v1/admin/config/versions/7/apply

# Roll back — the same "apply" endpoint, targeting an older version id.
# QueryGate never rewrites history: every version that ever existed stays
# inspectable via GET /admin/config/versions/{id}.
curl -X POST -H "Authorization: Bearer $KEY" $HOST/api/v1/admin/config/versions/1/apply
```

Every validate/preview/simulate/diff/blast-radius/stage/apply/rollback is
attributed to the calling
principal and recorded in the same audit trail as query execution (a
`config.governance` event — action, version id, outcome, actor — never the
YAML content itself, which stays only in the version store). The first call to
any `/admin/config/*` endpoint bootstraps version `"1"` from whatever
`connections.yaml`/`policy.yaml`/`catalog.yaml`/`templates.yaml` the deployment
started with, so "current active version" always means something. Gated behind two scopes,
matching the read/write split most admin APIs use: `admin:config:read`
(list/inspect versions) and `admin:config:write` (validate/preview/stage/
apply/rollback). `simulate`, `diff`, and `blast-radius` each require *both*
scopes at once — they echo back
semantic policy detail like a read endpoint, but also resolve caller-supplied
config/secret references like a write endpoint, so a read-only principal can't
turn any of them into a secret-existence oracle. All three load the candidate
documents into an isolated, temporary registry/policy/catalog context (reusing
the real loaders and validation logic) and never touch the live
registry/policy singletons or the version store, so concurrent production
requests and other simulations can't see or affect them. `diff` reports what a
candidate would actually *change* about resolved access — typed
tightening/loosening/neutral changes to connection visibility, guardrail caps,
table/column access, mandatory-filter requirements, and join groups — rather
than leaving a reviewer to mentally execute the default→connection policy merge
from a YAML line diff. It resolves the default and per-connection layers at the
connection baseline; when a change lives purely in a `principals:` override it
is flagged as `analysis_incomplete` (per-principal resolution is `blast-radius`,
below) rather than silently omitted. `blast-radius` builds directly on `diff`'s
own change classification — same categories, same tightening/loosening/neutral
taxonomy — evaluated once at the connection baseline and once more per
explicitly configured principal, then ranks the access-expanding results so a
reviewer sees whether a change is fleet-wide or targeted at a specific caller
before it's staged.

**Four-eyes approval (optional).** By default one `admin:config:write` principal
can stage and apply a change (single-administrator mode). Set
`require_config_approvals` to N ≥ 1 and a staged version cannot be applied until
N **distinct** reviewers holding the separate `admin:config:approve` scope have
approved it via `POST /admin/config/versions/{id}/approve` (or `/reject`, with an
optional bounded note) — and **the version's author can never approve their own
change**. Enforcement is entirely server-side (the store refuses an author's own
review and `apply` refuses an under-approved version), so it can't be bypassed by
talking to the API directly rather than the UI. Approvals bind to the version's
content fingerprint, and rollback to a previously-active version stays exempt so
disaster recovery is never blocked. Every approve/reject and every
insufficient-approvals rejection is in the audit trail. See
`docs/SCOPE_CATALOG.md`'s "Config Approver" role. Reviewers can act from the
`/admin/` Releases view (Approve/Reject buttons on a staged version) or the
`querygate-config` CLI (`querygate-config versions` / `approve <id>` /
`reject <id>`, authenticating with their own token).

### Validated policy templates and safe-start presets

Common access shapes — deny-by-default, reporting-only, customer-support,
tenant-isolated, bounded-analytics — otherwise require an admin to know every
relevant `policy.yaml` field. A small, fixed, code-reviewed set of templates
renders one of those shapes from a few typed parameters and merges it into
the caller's own local draft — never a live mutation, and never inferred
from live schema/table names or embedding credentials/tenant values:

```bash
# List the shipped templates and their typed parameters
curl -H "Authorization: Bearer $KEY" $HOST/api/v1/admin/config/templates

# Render one against the caller's current draft (nothing persisted)
curl -X POST -H "Authorization: Bearer $KEY" $HOST/api/v1/admin/config/templates/render \
  -d '{"template_id": "reporting-only",
       "params": {"connection": "demo", "allowed_tables": ["orders"], "max_limit": 20},
       "policy_yaml": "<the caller'"'"'s current draft policy.yaml, or omit for a fresh one>"}'
# -> {"policy_yaml": "...", "rules": ["Joins capped at 2, ...", ...], "warnings": []}
```

The merge is monotonically restrictive: applying a template can only add a
new restriction or tighten one already in the draft (a stricter existing
`max_joins`, a narrower existing `allowed_tables`, an existing
`mandatory_row_filters` entry) — it can never loosen or remove one, so a
template is safe to apply on top of policy an administrator has already
hand-edited. `rules` is a plain-English preview of every rule the merged
document now enforces, including when the merge kept an existing, stricter
value instead of the template's own. The rendered document is not a special
code path — it is plain `policy_yaml` text that goes through the exact same
`/validate`, `/simulate`, `/diff`, and `/versions` (stage) endpoints as a
hand-edited draft. `GET /templates` requires `admin:config:read`;
`POST /templates/render` requires `admin:config:write`, matching `/validate`
and `/preview` — it also resolves caller-supplied content and is meant to be
staged. The browser control plane's Policy designer exposes this as a
"Safe-start templates" panel: pick a template, fill in its parameters,
preview the resulting rules, and apply the rendered document to the local
draft with one click.

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
- filtered, newest-first browsing of persisted JSONL query/config/catalog/
  connection-probe audit events (when `AUDIT_SINK_BACKEND=jsonl` or
  `jsonl_chained`);
- a connection health workspace — the same credential-free per-connection
  status the admin API above returns, plus a "Test now" button per
  connection (gated on the separate `admin:connections:test` scope) that
  triggers an immediate, rate-limited re-check and updates that row in
  place.

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

### Non-admin "my access" portal

`/admin/` is built for operators — it needs an admin scope to see anything
useful and fills the page with controls a regular caller cannot use.
[`http://localhost:8000/access/`](http://localhost:8000/access/) is a
separate, dependency-free, read-only page any authenticated principal can
open to answer "what can I actually query?" without an admin scope:
identity/auth method/scopes, visible connections, effective per-connection
query guardrails (`max_joins`, `max_limit`, `timeout_seconds`, ...),
mandatory row-filter claim readiness, and a policy-filtered schema browser.
It calls the same `GET /api/v1/help/my-access` and `list_tables`/
`describe_table` endpoints any caller already has, so it discloses nothing
beyond what that principal's own policy already allows — never a filter or
claim *value*, another principal, raw YAML, version history, or audit
browsing; those stay `/admin/`-only. Served same-origin with the same
restrictive Content Security Policy, no-referrer/nosniff headers, and
no-store HTML as `/admin/`.

Recent-personal-denial history ("here's what was rejected for you this week")
is a second, separate self-service endpoint: `GET /api/v1/help/my-recent-denials`
(also authentication-only, no admin scope). It answers *why* your own recent
queries were rejected — a stable reason label (`policy`, `schema`, `quota`,
`cost_estimate`, ...) plus a human-readable explanation per denial, filtered to
the caller's own `principal_id` before any response is built, over a
configurable lookback window. Never another principal's activity, a query
value, or a table/column identifier beyond what the caller's own request
already referenced (TODO item 45 phase 2).

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
reading or changing what QueryGate connects to.

`POST /api/v1/admin/connections/{id}/test`, gated by its own
`admin:connections:test` scope, triggers an immediate out-of-band re-check of
one connection instead of waiting for the next background interval — useful
right after rotating a credential or changing network access. It reuses the
exact same ping/classification path (and non-disclosure posture) as the
background monitor, updates the shared cached status so a following `GET`
reflects it too, and returns the connection's fresh `ConnectionStatus`:

```bash
curl -X POST -H "Authorization: Bearer $KEY" \
  $HOST/api/v1/admin/connections/demo/test
```

An unknown connection is `404`; a deployment-disabled one is `409`. Each
connection can be manually probed at most once per
`admin_connection_test_cooldown_seconds` (default 10s, single-process only) —
a second request inside that window gets `429` with a `Retry-After` header
instead of opening another real connection to the target database. Every
probe — successful or rate-limited — is recorded as its own redaction-safe
`connection.probe` audit event (connection id, actor, probe result/failure
category, never a raw driver error or connection string). The browser
control plane's "Connection health" tab (below) renders this same status
view and exposes "Test now" as a button, per connection.

### Admin observability overview API

`GET /api/v1/admin/observability/overview`, gated by a dedicated
`admin:observability:read` scope, aggregates the in-process Prometheus metrics
into one operational snapshot so an administrator can see trends — *which
reason rejects the most queries*, *whether queue pressure is rising*, *whether
the cost-estimate gate is silently failing open* — without scraping raw
`/metrics` or parsing logs:

```bash
curl -H "Authorization: Bearer $KEY" $HOST/api/v1/admin/observability/overview
# -> {"source": "process_snapshot", "durable": false, "since": "...",
#     "note": "Current-process snapshot: ... This is not durable history.",
#     "queries_total": 128, "queries_success": 120, "queries_rejected": 8,
#     "rejections_by_reason": {"policy": 5, "schema": 3},
#     "concurrency_utilization": 0.25,
#     "cost_estimation": {"attempts": 40, "unavailable": 1, "would_reject": 2,
#                         "fail_open_rate": 0.025}, ...,
#     "by_connection": [{"connection": "demo", "queries_total": 128, ...}]}
```

It reports both a global rollup and a per-connection breakdown of query volume,
success/rejection categories, average duration, queue depth and wait-by-outcome,
concurrency in-use/max/utilization, per-principal quota rejections by kind, and
cost-estimation health. It is deliberately an **honest current-process
snapshot**, not durable history: the response says so (`source`, `durable:
false`, `since`, `note`), because counters are cumulative since process start
and reset on restart, and under the default in-process backends everything is
per-replica. Output is built only from already-public, low-cardinality metric
labels (connection ids and fixed reason/outcome buckets) — never a query,
value, principal, table, or column — and `admin:observability:read` gates the
whole overview, including the per-connection breakdown. The browser control
plane renders this as a read-only "Observability" section (overview cards + a
per-connection table + a banner echoing the snapshot's honesty note).
A config/catalog-change trend card, real time-window trend charts, and
querying an operator-configured external metrics backend for durable
cross-replica history have since shipped — see below.

### Per-principal behavioral anomaly surfacing

The overview above answers a *fleet* question from live Prometheus counters.
`GET /api/v1/admin/observability/anomalies` (same `admin:observability:read`
scope) answers a *per-principal* one from the durable audit stream: is one
caller's recent behavior unusual versus its own preceding baseline — even
among queries policy *allowed*? A read-only detector
(`querygate/admin/anomaly.py`) compares each principal's recent window against
its equal-or-longer baseline window and flags three signals: a `volume_spike`
(recent per-second rate far above baseline), a rejection-rate jump, and a
newly-touched connection the caller hadn't used before.

```bash
curl -H "Authorization: Bearer $KEY" $HOST/api/v1/admin/observability/anomalies
```

Detection is a pure function over audit events plus a fixed `now` and tunable
thresholds — no clock, file, or global state — so it stays fully testable, and
the report is bounded and redaction-safe (identities and typed signal kinds,
never a query, value, table, or column). It is strictly a read-only *surfacing*
of the existing stream, within item 32C's read-only boundary — it never blocks,
throttles, or edits policy. The browser control plane renders it as a
"Behavioral anomalies" panel in the Observability view.

### Config/catalog change-velocity trend

`GET /api/v1/admin/observability/config-changes` (same
`admin:observability:read` scope) answers a question the overview above
can't: is config-governance or catalog-governance change *volume* rising, and
by which action? Unlike the process-snapshot overview, this reads the durable
persisted audit stream, so it's a real recent-vs-baseline rate comparison (the
same two-window shape the anomaly detector uses per-principal, applied
fleet-wide to change-event volume) rather than a since-process-start counter.
Bounded by a configurable scan cap, it honestly reports `source="disabled"`
without the JSONL audit sink enabled, and carries only action names/outcomes/
counts — never version content, proposal text, or raw YAML. Rendered as a
"Change velocity" subsection in the same Observability view.

### Real time-window trend charts (external metrics backend)

`GET /api/v1/admin/observability/history` (same `admin:observability:read`
scope) closes the one gap the process-snapshot overview can't: a real trend
*line*, not just a current-value card. Since QueryGate doesn't own a
time-series store of its own, it reads an **operator-configured**
Prometheus-compatible HTTP API — one already scraping this deployment's own
`/metrics` — for five fixed named series (successful/rejected query rate, avg
duration, queue depth, concurrency utilization). QueryGate only ever queries
this backend, never writes to it, and every query sent is one of five fixed
PromQL templates the admin dashboard composes itself — never anything a
caller can shape. With no backend configured (the default) it honestly
reports `source="disabled"`; an unreachable or erroring backend is reported as
a stable `backend_error` category rather than failing the whole request.
Rendered as a "Trend charts" subsection with a dependency-free inline-SVG
sparkline per series — no charting library.

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

`POST /api/v1/demo/query` with that body — or `run_structured_queries` over
MCP with `connection: "demo"` and `queries: [<that object>]` — runs it.
Supported: multi-column select, inner/left/full/cross joins with either an
equality `on` pair or a general `condition` predicate tree for range/temporal
joins (including cross-connection
joins within a policy `join_group`), nested and/or `where`, `group_by` /
`having`, `order_by`, `limit`/`offset`, aggregate and date-bucket select
items, `top_n` per-partition ranking (top-N-per-group), computed **expressions**
anywhere a scalar belongs — arithmetic, nested functions, casts, `CASE`,
including inside an aggregate, so `SUM(quantity * unit_price)` and conditional
aggregation are expressible (item 100), **window functions** for running
totals, moving averages, rank-in-place and lag/lead (item 101), and **set
operations** — `UNION`/`INTERSECT`/`EXCEPT` combining several queries into one
statement, each arm independently policy-checked and independently filtered
(item 104). Every one of
those is a typed AST node checked against the policy and the live schema; none
of them is a SQL string.

## Example MCP usage

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "run_structured_queries",
    "arguments": {
      "connection": "demo",
      "queries": [
        {
          "from": "orders",
          "select": ["orders.id", "orders.status", "orders.total_amount"],
          "where": { "col": "orders.status", "op": "eq", "value": "completed" },
          "limit": 10
        }
      ]
    }
  }
}
```

POST this to `/mcp` (Streamable HTTP) with `MCP_ENABLED=true`. Tools:
`list_connections`, `list_tables`, `describe_table`, `search_catalog`,
`run_structured_queries` (execute, dry-run/explain via `mode="explain"`,
and single-or-batch — `queries` is always a list — all in one tool),
`list_query_templates`/`run_query_template` (curated templates, below), plus
the product-guide and access tools described below. More examples in
[`examples/mcp_calls.md`](examples/mcp_calls.md).

## Curated query templates (optional)

Instead of (or alongside) letting an agent compose an arbitrary
`StructuredQuery` within policy caps, an admin can pre-define a fixed set of
**named, parameterized queries** — `get_orders_for_customer(customer_id)`,
`top_products(category, n)` — and agents invoke one of *those* by name with
typed parameters. This shrinks the effective surface to a finite, reviewed set
of query shapes ("these are the twelve things this agent may ask") and gives
non-technical stakeholders something concrete to sign off on. It is **additive
to QueryGate's core guarantee, not a new one**: a template is just a stored
`StructuredQuery` AST — there is still no raw-SQL field anywhere.

Templates are file-configured (`TEMPLATES_FILE`, unset by default — a
deployment with no templates behaves identically), hot-reloadable via the same
`POST /api/v1/admin/reload-config` endpoint, and validated by
`querygate-validate-config --template-file`:

```yaml
# examples/templates.example.yaml
templates:
  - id: orders_for_customer
    connection: demo
    description: A customer's most recent orders, newest first.
    parameters:
      - {name: customer_id, type: integer, required: true}
      - {name: limit, type: integer, required: false, default: 10, min: 1, max: 100}
    query:
      from: orders
      select: [orders.id, orders.status, orders.total_amount]
      where: {col: orders.customer_id, op: eq, value: {param: customer_id}}
      order_by: [{col: orders.id, dir: desc}]
      limit: {param: limit}
```

`query` is a normal `StructuredQuery` skeleton with `{param: <name>}`
placeholders in value positions. At invocation the caller's parameters are
type/constraint-checked against the slots, bound in, and the **resulting**
`StructuredQuery` runs through the unchanged validate → policy → schema →
compile → execute pipeline — so a bound template inherits every policy cap,
allow/deny list, and mandatory row filter an ad-hoc query does, and a parameter
gets **no exemption** from policy.

```bash
# List the templates this principal may invoke (filtered to visible connections)
curl -H "Authorization: Bearer $KEY" $HOST/api/v1/query-templates

# Invoke one with typed parameters
curl -X POST -H "Authorization: Bearer $KEY" \
  $HOST/api/v1/query-templates/orders_for_customer/run \
  -d '{"parameters": {"customer_id": 42, "limit": 5}}'
```

The MCP equivalents are `list_query_templates` and
`run_query_template(template_id, parameters)`, and the browser control plane at
`/admin/` has a read-only **Query templates** panel that lists the callable
templates and their parameter signatures (filtered by the same per-connection
visibility, no special scope required). `querygate-validate-config
--template-file` structurally validates each template's query skeleton at
deploy time — a malformed template is caught before deploy, not at first
invocation. A template is visible/invocable
only if its target connection is visible to the caller (same rule as
`list_connections`); an unknown template and one on a hidden connection return
the same non-enumerating not-found. Invocations are audited distinctly
(`operation="run_query_template"`, the template id, and the parameter *names* —
never parameter values, which are stripped from the query shape like any
literal). The governed create/edit/approve/publish/rollback workflow for
templates (reusing the catalog-governance state machine) is a later phase; for
now templates are declarative config, exactly as safe as `policy.yaml`.

## Agent-visible capacity waiting

`Policy.max_concurrency` and `concurrency_wait_seconds` already bound how
many queries run at once per connection and how long an over-cap request
waits for a slot before it's rejected. `queue_mode` and `wait_timeout_seconds`
(both optional, request-level — REST query params on `POST .../query` and
`.../query/batch`; extra MCP tool arguments on `run_structured_queries`,
ignored in `mode="explain"`) make that existing wait caller-tunable:

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
`completed`, `capacity_timeout`, or `queue_full` — and
`X-QueryGate-Queue-Wait-Ms`) so the `{"detail": "too many concurrent ..."}`
rejection body never changes shape; a capacity rejection is `429` with a
`Retry-After` header (migrated from `422`, TODO.md item 35 phase 3). A
successful `StructuredQueryResult`/`BatchQueryItemResult` also carries
`admission_id`/`queue_wait_ms` fields directly. MCP's `MCPErrorResult` gains
the same `admission_id`/`admission_state`/`queue_wait_ms` fields for a
capacity rejection (`RATE_LIMITED` error code). `querygate_queue_depth`
(current waiters, single-process visibility) and
`querygate_queue_wait_seconds` (histogram, by outcome) are exported
alongside the existing concurrency metrics.

This started as phase 1 of TODO.md item 35 — a synchronous, caller-tunable
version of the wait that already existed; phase 3 below adds progress,
asynchronous execution, and cancellation.

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

### Progress, asynchronous execution, and cancellation

TODO.md item 35 phase 3 closes out the rest of the contract: a progress
signal while queued, a non-blocking REST lifecycle, and real cancellation.

**MCP progress.** `run_structured_queries` reports two progress notifications
per query to any client that supports MCP's standard
`notifications/progress` (via `Context.report_progress` — silently a no-op
otherwise, so this is purely additive): once when the wait for a
concurrency slot begins, once when the slot is acquired and execution
starts. Not a continuous mid-wait tick — that would mean slicing the
underlying wait into shorter repeated attempts, risking the "a caller
cannot extend its wait past the operator's ceiling" guarantee for a
DX-only feature.

**REST asynchronous execution.** `queue_mode=async` on `POST .../query`
returns `202` immediately instead of blocking:

```bash
curl -X POST "http://localhost:8000/api/v1/demo/query?queue_mode=async" \
  -H "Content-Type: application/json" \
  -d '{"from": "orders", "select": ["orders.id"], "limit": 10}'
# -> 202 {"admission_id": "...", "status_url": "/api/v1/demo/query/<id>"}

curl http://localhost:8000/api/v1/demo/query/<admission_id>
# -> {"state": "completed", "result": {...}, ...}
```

`state` is one of `queued`/`running`/`completed`/`failed`/
`cancel_requested`/`cancelled`. In-process only for this pass — a caller
polling a different replica than the one that started the query gets a
`404`; a Redis-backed cross-replica store is a natural follow-up, mirroring
how admission (phase 1) and quota both shipped in-process before growing a
cross-replica variant.

**Cancellation.** `POST .../query/<admission_id>/cancel` requests
cancellation. Cancelling your own query needs no scope; cancelling another
principal's needs the new `query:cancel` scope. Cancelling a still-`queued`
query is always free — it never touches the database. Cancelling a
`running` query invokes real dialect-level cancellation (Postgres
`pg_cancel_backend`, which interrupts just the query and leaves the pooled
connection alive; MSSQL `KILL <spid>`, T-SQL's only out-of-band primitive
for this, which terminates the whole session) — but only if the
connection's policy sets `allow_query_cancellation: true`, deny-by-default
like `allow_cross_join`:

```yaml
connections:
  demo:
    allow_query_cancellation: true  # only after granting the DB-level permission below
```

Both dialects' out-of-band cancellation needs a real permission grant
beyond a typical reporting connection: Postgres requires this connection's
role to be a superuser or a member of `pg_signal_backend`
(`GRANT pg_signal_backend TO <role>;`); MSSQL requires the server-level
`ALTER ANY CONNECTION` permission or sysadmin. A cancel request for a
running query is rejected outright (`403`), before any DB call, unless the
flag is set — the operator sets it only after granting the permission
themselves, so a missing grant is a deployment decision made in the open,
never a runtime permission error discovered mid-cancellation.

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
  Real Postgres / MSSQL / MySQL database
```

- **`connections/`** — `ConnectionProfile` registry loaded from YAML,
  `${...}`-interpolated connection strings, per-dialect engine/session
  lifecycle (`dialects.py` is the only place Postgres/MSSQL/MySQL-specific
  SQL lives).
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
- **`templates/`** — admin-defined, named, parameterized `StructuredQuery`
  skeletons (item 48) loaded from an optional `TEMPLATES_FILE`. `bind_template`
  type-checks the caller's parameters, substitutes them, and hands the
  resulting `StructuredQuery` to the same `StructuredQueryService` an ad-hoc
  query uses — a template inherits every policy/schema/guardrail check and
  cannot exceed policy or contain raw SQL.
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
- **Signed, provenance-attested releases.** On a maintainer-pushed version
  tag, `.github/workflows/release.yml` builds and pushes the container image to
  GHCR behind a pre-publish Trivy gate, then **signs it with cosign keyless
  (Sigstore)** and attaches a **SLSA build-provenance attestation**
  (`actions/attest-build-provenance`) — both bound to the image digest and
  consumer-verifiable (`cosign verify` / `gh attestation verify`). Offline
  artifact integrity is checkable with `make verify-release`
  (`scripts/verify_release.py`) against `dist/SHA256SUMS`. Publishing is never
  automatic — it happens only when a maintainer deliberately pushes the tag.
- **Continuously scanned, and provable.** Every change runs static analysis
  (Bandit + Semgrep OSS), full-history secret scanning (gitleaks), container
  image scanning of the shipped image (Trivy), and OpenAPI fuzzing (Schemathesis)
  — each deny-by-default. For the full, reproducible security-and-reliability
  posture (every gate, the command to run it yourself, and the threat-model
  mapping) see **[docs/SECURITY_POSTURE.md](docs/SECURITY_POSTURE.md)**. To report
  a vulnerability, see [`SECURITY.md`](SECURITY.md).

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
- **Audit retention has an opt-in native WORM path with managed search** —
  `AUDIT_SINK_BACKEND=jsonl_chained_s3_worm` (item 134) archives to S3
  Object Lock (COMPLIANCE mode) alongside the local hash-chained ledger, and
  `GET /api/v1/admin/observability/worm-search` (item 134 phase 2) searches
  that archive directly — bounded time-range, filtered, paginated — but it's
  still opt-in: the default `jsonl_chained` sink is local-file-only, not
  itself WORM, and there's no browser UI over the search endpoint yet, REST
  only.
- **Config-governance's approval workflow is opt-in, not the default** — a
  caller with `admin:config:write` can stage and immediately apply a version
  in one session unless an operator sets `require_config_approvals` above 0
  (item 42), which then requires that many distinct `admin:config:approve`
  holders — never the author themselves — before an apply proceeds; there is
  no scheduled/timed apply either way. `POST /admin/config/diff` reports a
  resolved-access semantic diff
  (typed tightening/loosening/neutral changes, not just a YAML line diff) at
  the connection baseline, and `POST /admin/config/blast-radius` aggregates
  that same diff across every principal explicitly configured in
  `policy.yaml`'s `principals:` section — ranking access-expanding changes so
  a reviewer can tell a targeted change from a fleet-wide one — but it is
  bounded to the principals a deployment actually configured (up to 100) and
  reports `analysis_incomplete` rather than resolving every conceivable
  subject; blast-radius impact analysis beyond that bound (e.g. asynchronous
  or paginated evaluation for very large principal counts) is a later phase.
  The preview reports changed/unchanged only
  with read scope; write-only callers see submitted/inherited so write scope
  cannot become read scope. No admin UI either — the governance mutation API
  is REST-only for now.
- **Writes are opt-in and deny-by-default, not absent** — see "Governed
  writes" above. `WritePolicy.enabled` is `false` until an operator turns it
  on per table/operation, so a default deployment is read-only in practice;
  there is still no raw-DML string field on either transport.
- **Pre-execution cost estimation is Postgres-only** — `max_estimated_rows`/
  `max_estimated_cost` (above) have no effect on an MSSQL connection yet;
  MSSQL's estimated-plan mechanism needs its own connection lifecycle that
  hasn't been built (TODO item 26 phase 2).
- **Distributed concurrency enforcement (Redis-backed) is opt-in** — the
  default is an in-process semaphore, correct for a single instance only;
  set `concurrency_backend: redis` for multi-instance deployments.
- **The `queue_mode=async` execution store is in-process only** — a caller
  polling `GET .../query/{admission_id}` must reach the same replica that
  started the query, or gets a `404` (TODO item 35 phase 3); a Redis-backed
  cross-replica variant is a documented follow-up, mirroring how
  distributed concurrency enforcement above and per-principal quota both
  shipped in-process before growing a Redis-backed variant. The in-process
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

MSSQL support (including the query-execution-timeout guardrail), MySQL
support (item 19 phase 1), and the Postgres statement-timeout guardrail are
all verified against real servers, not just unit-tested SQL text — see
`tests/integration/test_mssql_live.py`, `tests/integration/test_mysql_live.py`,
and `tests/integration/test_postgres_timeout.py`. **Snowflake support (item 19
phase 2) is compiler/rendering-level only and is NOT live-verified**: the
`DialectAdapter` is unit-tested by compiling its output against a real
`snowflake.sqlalchemy` dialect object; the `SessionDialectAdapter` is
unit-tested against recording fakes that assert the exact SQL text/params it
builds, not against that real dialect object (its statements are built
directly with `sa.text(...)` rather than compiled expressions). Neither is
tested against a live Snowflake instance — there is none available in this
project's environment (a proprietary cloud service, unlike Postgres/MySQL/MSSQL
which run in Docker), and
`snowflake-sqlalchemy`'s driver has no async SQLAlchemy engine support, so
`connections/engine.py` refuses to actually open a Snowflake connection today
— registering one fails with a clear, explained error rather than connecting.
Do not treat Snowflake as production-ready the way the other three dialects
are; see TODO.md item 19's Snowflake live-verification follow-up. **BigQuery
support (item 19 phase 3) is the same compiler/rendering-level-only, NOT
live-verified posture**, checked the same way (its `DialectAdapter` compiles
against a real, installed `sqlalchemy_bigquery` dialect object; its
`SessionDialectAdapter` is unit-tested against recording fakes). BigQuery has
a second, independent reason beyond the missing async driver that
`connections/engine.py` refuses to open a connection for it: `sqlalchemy_
bigquery`'s DBAPI resolves real Google credentials and builds a live client
at engine-construction time, not connection time. Do not treat BigQuery as
production-ready either; see TODO.md item 19's BigQuery live-verification
follow-up. The real-Postgres load/soak
harness also proves the observed database concurrency cap, overflow rejection,
queued completion, timeout cancellation, `queue_mode=fail_fast` never
waiting, and a caller-shortened `wait_timeout_seconds` being honored under
concurrent REST traffic; see [`docs/LOAD_TESTING.md`](docs/LOAD_TESTING.md).
OAuth/JWT is implemented (`core/jwt_auth.py`) alongside static API keys.
Every `Policy` complexity cap (joins, select width, where-depth, group-by,
top-N, partition-by, batch size) is boundary-tested at exactly its configured
limit, and the SQLAlchemy compiler is fuzzed with Hypothesis-generated random
`StructuredQuery` combinations — including that a mandatory row filter
survives every generated shape — rather than only the fixed set of
hand-written cases; see `tests/unit/test_policy_boundaries.py` and
`tests/unit/test_compiler_properties.py`.

For the repeatable source/package and container release gates, see
[`docs/RELEASING.md`](docs/RELEASING.md). Historical extraction notes are
kept outside the product surface under `archive/extraction/`.

<p align="center">
  <img src="landing/assets/favicon.svg" alt="QueryGate app icon" width="64">
</p>
