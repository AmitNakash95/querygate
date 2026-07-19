# Example REST calls

Assumes QueryGate is running locally on `:8000`, `ENVIRONMENT=localhost`, and
no `API_KEYS` configured (dev bypass). Once you set `API_KEYS`, add
`-H "Authorization: Bearer <key>"` to every call below.

## List connections

```bash
curl http://localhost:8000/api/v1/connections
```

## List tables

```bash
curl http://localhost:8000/api/v1/demo/tables
```

## Describe a table

```bash
curl http://localhost:8000/api/v1/demo/tables/orders
```

## Explain a query (compile without executing)

```bash
curl -X POST http://localhost:8000/api/v1/demo/query/explain \
  -H "Content-Type: application/json" \
  -d '{
    "from": "orders",
    "select": ["orders.id", "orders.status", "orders.total_amount"],
    "where": {"col": "orders.status", "op": "eq", "value": "completed"},
    "order_by": [{"col": "orders.total_amount", "dir": "desc"}],
    "limit": 10
  }'
```

## Execute a query

```bash
curl -X POST http://localhost:8000/api/v1/demo/query \
  -H "Content-Type: application/json" \
  -d '{
    "from": "orders",
    "select": [
      "orders.customer_id",
      {"fn": "sum", "col": "orders.total_amount", "as": "total_spent"}
    ],
    "group_by": ["orders.customer_id"],
    "order_by": [{"col": "total_spent", "dir": "desc"}],
    "limit": 5,
    "intent": "top customers by total spend"
  }'
```

## Capacity waiting (queue_mode / wait_timeout_seconds)

Optional query params, outside the request body — see the README's
"Agent-visible capacity waiting" section for the full contract:

```bash
# Reject immediately if the connection is at its concurrency cap, instead of
# waiting up to the policy's concurrency_wait_seconds.
curl -X POST "http://localhost:8000/api/v1/demo/query?queue_mode=fail_fast" \
  -H "Content-Type: application/json" \
  -d '{"from": "orders", "select": ["orders.id"], "limit": 10}'

# Wait up to 2 seconds for a slot — shorter than (or equal to) the policy's
# own ceiling; a longer request is clamped down, never extended.
curl -X POST "http://localhost:8000/api/v1/demo/query?queue_mode=wait&wait_timeout_seconds=2" \
  -H "Content-Type: application/json" \
  -d '{"from": "orders", "select": ["orders.id"], "limit": 10}'
```

Every response — success or a `422` capacity rejection — carries
`X-QueryGate-Admission-Id`, `X-QueryGate-Admission-State`
(`completed`/`capacity_timeout`), and `X-QueryGate-Queue-Wait-Ms` headers. A
successful body also carries `admission_id`/`queue_wait_ms` fields directly.

## Batch queries

```bash
curl -X POST http://localhost:8000/api/v1/demo/query/batch \
  -H "Content-Type: application/json" \
  -d '{
    "queries": [
      {"from": "customers", "select": ["customers.id", "customers.name"], "limit": 5},
      {"from": "order_items", "select": [{"fn": "count", "col": "*", "as": "item_count"}]}
    ]
  }'
```

## What raw SQL looks like here

There is no field, no endpoint, and no escape hatch that accepts a SQL
string. Trying `{"sql": "SELECT * FROM customers"}` against any endpoint
above returns a 422 — `StructuredQuery` has no `sql` field, and extra
fields are rejected (`extra="forbid"`), not silently ignored.
