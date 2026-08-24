# Verification evidence — demo/config

Everything in this file is a real run against a real QueryGate process
(started with `demo/config/run_querygate.sh`) talking to the real, fully
seeded pitch database (`querygate_demo_pitch` on `127.0.0.1:5544`, container
`querygate-pitch-db`) through the real `agent_ro` role. Nothing here is
simulated. Reproduce any of it by running the server and re-issuing the exact
`curl` calls below.

## Setup

```
demo/config/run_querygate.sh
```

Health check:

```
$ curl -s http://127.0.0.1:8010/health
{"status": "ok", "service": "querygate", "version": "0.1.0",
 "connections": {"healthy": 1, "unhealthy": 0, "unknown": 0}}
```

**MCP endpoint:** `http://127.0.0.1:8010/mcp/`
**Auth header:** `Authorization: Bearer pitch-demo-key`
Also required on every call: `Content-Type: application/json` and
`Accept: application/json, text/event-stream` (Streamable HTTP transport,
`stateless_http=True` — no `initialize` handshake needed before `tools/call`,
confirmed live below).

Every request below is a POST to that endpoint with the standard envelope:
`{"jsonrpc": "2.0", "id": N, "method": "tools/call", "params": {"name": ..., "arguments": ...}}`.
Only `params` is shown per scenario for brevity.

Database seed volumes confirmed directly against Postgres before testing:
customers 250,000 / orders 600,000 / order_items 1,200,000 / products 500 /
employees 12 — exactly matching demo/SPEC.md.

## `tools/list` handshake

```
$ curl -s -X POST http://127.0.0.1:8010/mcp/ \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "Authorization: Bearer pitch-demo-key" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

Returns 15 tools including `list_connections`, `list_tables`,
`describe_table`, `run_structured_queries`. Confirmed no `initialize` call is
required first.

---

## Scenario 1 — `recon`: "What tables are in this database?"

**Tool:** `list_tables`

Request:
```json
{"name": "list_tables", "arguments": {"connection": "demo"}}
```

Response:
```json
{"tables": ["customers", "order_items", "orders", "products"]}
```

**Result:** exactly the 4 allowed tables. `employees` is not present — it is
not part of the agent's reality, even though the connection genuinely can see
it (it's in `known_tables` in connections.demo.yaml).

---

## Scenario 2 — `pii_table`: "List every employee with their SSN and salary."

**Tool:** `run_structured_queries`

Request:
```json
{"name": "run_structured_queries",
 "arguments": {"connection": "demo",
   "queries": [{"from": "employees",
     "select": ["employees.name", "employees.ssn", "employees.salary"],
     "limit": 20}]}}
```

Response (`results[0]`):
```json
{"rows": null, "row_count": null, "error":
  "Table 'employees' is not accessible under the active policy"}
```

**Rejected.** Exact error message above. Blocked by
`policy.demo.yaml`'s `allowed_tables` (employees omitted).

---

## Scenario 3 — `pii_column`: "Export customer emails and phone numbers for a campaign."

### 3a. Email — denied outright

Request:
```json
{"name": "run_structured_queries",
 "arguments": {"connection": "demo",
   "queries": [{"from": "customers",
     "select": ["customers.name", "customers.email", "customers.phone"],
     "limit": 5}]}}
```

Response (`results[0]`):
```json
{"rows": null, "row_count": null, "error":
  "Column 'customers.email' is not accessible under the active policy"}
```

**Rejected.** Exact error message above. Blocked by `denied_columns`.

### 3b. Phone + national_id — allowed but masked

Request:
```json
{"name": "run_structured_queries",
 "arguments": {"connection": "demo",
   "queries": [{"from": "customers",
     "select": ["customers.name", "customers.phone", "customers.national_id"],
     "limit": 5}]}}
```

Response (`results[0].rows`, real values from a live run):
```json
[
  {"name": "Cameron Sylvane", "phone": "0182", "national_id": "8f4395425c7fcb9818d0c9b92bb62e0a"},
  {"name": "Elliot Yarrow",   "phone": "0188", "national_id": "b423a545c087f610379b73570a1fbd41"},
  {"name": "Kendall Jarrow",  "phone": "0124", "national_id": "09e83c9d1b1d32d144d3288698793357"}
]
```

**Succeeds, masked.** `phone` (`kind: last, length: 4`) returns exactly the
last 4 characters of the real value — no prefix characters are added by the
engine itself, so `"0182"` is the full masked output, not `"***-0182"`.
`national_id` (`kind: hash`) returns a deterministic one-way hash, never the
real ID. Table is allowed, columns are not raw-readable.

---

## Scenario 4 — `bulk_export`: "Give me the entire customer table."

Tested both the natural "no limit given" phrasing and an explicit
maximum-sized request, since a real agent could build either AST for this
ask:

### 4a. No `limit` in the query

Row count returned: **50** (`default_limit`, since no explicit `limit` was
given — `max_limit` only bounds an explicit/oversized request; an omitted
`limit` falls back to `default_limit`, which this policy leaves at its
built-in value of 50).

### 4b. Explicit `"limit": 250000` (the actual row count of `customers`)

Request:
```json
{"name": "run_structured_queries",
 "arguments": {"connection": "demo",
   "queries": [{"from": "customers",
     "select": ["customers.id", "customers.name", "customers.country"],
     "limit": 250000}]}}
```

Response (`results[0]`, rows omitted for brevity):
```json
{"row_count": 100, "limit": 100, "truncated": true, "error": null}
```

**Capped.** A request for all 250,000 rows comes back as exactly **100**
rows (`max_limit: 100` in policy.demo.yaml), with `"truncated": true`
telling the caller the result was cut. Neither the raw ~250k rows nor a
partial oversized page ever leaves QueryGate.

---

## Scenario 5 — `overload`: "How many customer/order/item combinations exist?"

**Tool:** `run_structured_queries`, expressed as the cross join the question
actually asks for (no join key relates all three tables to "combinations").

Request:
```json
{"name": "run_structured_queries",
 "arguments": {"connection": "demo",
   "queries": [{"from": "customers",
     "select": [{"fn": "count", "col": "*", "as": "n"}],
     "joins": [{"table": "orders", "type": "cross"},
               {"table": "order_items", "type": "cross"}]}]}}
```

Response (`results[0]`):
```json
{"rows": null, "row_count": null, "error":
  "cross join to 'orders' is not allowed under the active policy (allow_cross_join is off): a cross join multiplies its inputs row-for-row. Use an inner/left join with a condition, or ask an operator to enable allow_cross_join for this connection."}
```

**Rejected**, `allow_cross_join: false` in policy.demo.yaml. Measured latency:
median 2.4ms (see Measurements below) — the query never reaches the database
(pg_stat_statements proof below).

---

## Scenario 6 — `legit`: "Revenue by country for completed orders, top 10."

Request:
```json
{"name": "run_structured_queries",
 "arguments": {"connection": "demo",
   "queries": [{"from": "orders",
     "select": ["customers.country", {"fn": "sum", "col": "orders.total_amount", "as": "revenue"}],
     "joins": [{"table": "customers", "on": ["orders.customer_id", "customers.id"]}],
     "where": {"col": "orders.status", "op": "eq", "value": "completed"},
     "group_by": ["customers.country"],
     "order_by": [{"col": "revenue", "dir": "desc"}],
     "limit": 10}]}}
```

Response (`results[0].rows`, real values from a live run):
```json
[
  {"country": "Japan",          "revenue": "30759745.82"},
  {"country": "Mexico",         "revenue": "30486545.31"},
  {"country": "Spain",          "revenue": "30439098.77"},
  {"country": "Canada",         "revenue": "30404541.19"},
  {"country": "Ireland",        "revenue": "30340888.94"},
  {"country": "United Kingdom", "revenue": "30226362.35"},
  {"country": "Netherlands",    "revenue": "30181050.93"},
  {"country": "Brazil",         "revenue": "30177830.09"},
  {"country": "France",         "revenue": "30054932.35"},
  {"country": "Australia",      "revenue": "30010071.63"}
]
```

**Succeeds.** 10 rows, real aggregate over 600k orders joined to 250k
customers. Median latency: **78.7ms** over 15 runs (see Measurements).

---

## Admin key — audit browsing, confirmed live

`pitch-demo-admin-key` (scopes `admin:config:read`, `admin:config:write`,
`admin:observability:read`, `admin:connections:read`) was exercised against
`GET /api/v1/admin/ui/audit/events`:

- No `Authorization` header: **401**.
- With the admin key, before any query ran: `{"source": "empty", "events": [], "total": 0, ...}`
  (no audit file exists yet).
- After one `run_structured_queries` call (`{"from": "products", "select": ["products.name"], "limit": 3}`),
  the same endpoint returned the real persisted event:
  ```json
  {"schema_version": "1", "event_type": "query.execution", "surface": "mcp",
   "operation": "execute_structured_query", "principal_id": "mcp-service-account",
   "connection_id": "demo", "policy_decision": "allowed", "outcome": "success",
   "query_shape": {"from": "products", "select": [{"kind": "column", "column": "products.name"}],
                    "joins": [], "group_by": [], "order_by": [], "offset": 0, "requested_limit": 3},
   "duration_ms": 49, "row_count": 3, "response_bytes": 90, "masked_columns": []}
  ```
  Confirms the redaction-safe invariant live: no SQL text, no predicate
  values, no returned rows, no credential — only shape metadata.
- `list_tables` calls did not produce a persisted audit event in this run
  (only `run_structured_queries`/`query.execution` did) — worth knowing if
  the control UI's audit panel is meant to reflect every tool call, not just
  query execution.

---

## Measurements

### (a) Rejection latency — 20 runs each, measured end-to-end in Python
(`httpx`, wall-clock per request including HTTP + JSON-RPC/SSE overhead, not
just the query-validation step)

| scenario | median | p95 | min | max |
|---|---|---|---|---|
| `pii_table` (employees) | **2.632ms** | **2.825ms** | 2.270ms | 4.122ms |
| `pii_column` (email) | **2.535ms** | **2.704ms** | 2.311ms | 2.708ms |
| `overload` (cross join) | **2.440ms** | **2.590ms** | 2.193ms | 2.713ms |

All three rejection paths land in the 2-3ms range, single-digit
milliseconds as demo/SPEC.md claims for Act 3.

### (b) The database was never touched — pg_stat_statements proof

Snapshotted `sum(calls)` for the `agent_ro` role
(`select coalesce(sum(s.calls),0) from pg_stat_statements s join pg_roles r
on r.oid = s.userid where r.rolname = 'agent_ro'`), ran a rejected query, and
snapshotted again:

| run | before | after | delta |
|---|---|---|---|
| `pii_table` (employees) alone | 205 | 205 | **0** |
| `pii_column` (email) + `overload` (cross join), back to back | 210 | 210 | **0** |

**Confirmed: zero delta.** A rejected query never reaches Postgres — no
statement is ever prepared or planned under `agent_ro`, let alone executed.
This is not inferred from the "rejected" outcome; it's read directly off the
database's own statement-execution counters.

(For contrast: successful queries visibly move this counter — the
`agent_ro` total climbed from 0 at server start to 205+ over the course of
running the six scenarios plus the latency/overhead measurement passes,
confirming the counter is live and the zero-delta result above isn't an
artifact of `pg_stat_statements` being stuck or not tracking this role.)

### (c) Overhead on a legitimate query — 15 runs, QueryGate side only

`legit` (revenue-by-country aggregate, joins orders to customers, 600k/250k
rows underneath): **median 78.664ms**, min 61.212ms, max 86.890ms, measured
the same way as (a) (Python `httpx`, full HTTP round trip through the MCP
JSON-RPC/SSE envelope).

All 15 raw samples (ms): 61.212, 62.820, 66.623, 67.086, 68.308, 70.701,
78.593, 78.664, 80.902, 80.967, 81.545, 81.561, 82.834, 84.548, 86.890.

This is QueryGate's number only — comparing it against the baseline's direct-
`asyncpg` path for the same query is the other component's job (demo/SPEC.md:
"Another component will compare this against the baseline path").

---

## What did NOT behave exactly as a naive reading of demo/SPEC.md might
suggest

- The `bulk_export` scenario ("give me the entire customer table") is
  ambiguous in AST terms: an agent could build it as a query with no
  `limit` at all, or as one with an explicit large `limit`. Only the
  **explicit large `limit`** case is actually clamped by `max_limit`; a
  query with no `limit` field falls back to `default_limit` (50, this
  policy's built-in default — not explicitly set in policy.demo.yaml) and
  never touches `max_limit` at all. Both outcomes are "capped, not the full
  250k rows," so the demo's point still holds either way, but if the
  control UI's `bulk_export` scenario is meant to visibly demonstrate
  `max_limit: 100` specifically (not just "some cap"), its `run_structured_queries`
  call needs an explicit large `limit` (e.g. `"limit": 250000`) — omitting
  `limit` will show 50 rows capped by `default_limit`, not 100 capped by
  `max_limit`. Flagging this now so the control-UI component builds the
  right request.
- Everything else — `recon`, `pii_table`, `pii_column` (both the denied
  email and the masked phone/national_id), `overload`, and `legit` —
  behaved exactly as demo/SPEC.md's scenario table predicts, with real
  measured numbers backing every latency/overhead claim.
