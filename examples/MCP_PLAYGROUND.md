# MCP playground — sit down and stress QueryGate as a user

This is a hands-on script for exercising a running QueryGate over MCP against
the **large, real-world-shaped demo domain** (see
[`demo_db/large_schema.py`](demo_db/large_schema.py)). It is organised around
four questions you'll want to answer for yourself:

- **Does security hold up?** (Section A) — the walls reject what they should.
- **Do the features hold up?** (Section B) — discovery, catalog, templates,
  explain, capacity, audit.
- **Does the StructuredQuery engine hold up?** (Section C) — joins, aggregates,
  date buckets, top-N, percentiles, CASE, NULL handling.

Every payload below is a raw MCP `tools/call` — POST it to
`http://localhost:8000/mcp` exactly like [`mcp_calls.md`](mcp_calls.md) shows
(same headers and `Authorization: Bearer <key>`). Only the `arguments` object
is shown for brevity after the first couple; wrap it in the standard
`{"jsonrpc":"2.0","id":N,"method":"tools/call","params":{"name":...,"arguments":...}}`
envelope.

There is deliberately **no raw-SQL field anywhere** in any of these — the only
thing you can ever submit is a validated StructuredQuery AST. That's the whole
product, and Section A is where you try (and fail) to get around it.

---

## 0. One-time setup

```bash
docker compose up -d            # demo Postgres + Redis
make seed-large                 # ~400k rows into the large domain (SEED_SCALE=0.1 for a fast subset)
cp .env.example .env            # then edit as below
```

In `.env`, turn on MCP and give yourself an admin key so you can also browse
audit/observability:

```dotenv
MCP_ENABLED=true
MCP_API_KEYS='["playground-mcp-key"]'

API_KEYS='["playground-admin-key"]'
API_KEY_SUBJECT=local-admin
API_KEY_SCOPES='["admin:config:read", "admin:observability:read"]'
```

```bash
make run        # http://localhost:8000  (MCP at /mcp, admin UI at /admin/)
```

Use `Authorization: Bearer playground-mcp-key` on every MCP call below.

### The three connections you're playing with

All three point at the **same** physical database but expose different
tables under different policy — this is what lets you feel "two callers of one
dataset get different access":

| connection | exposes | policy posture |
|---|---|---|
| `demo` | the original 5 tiny fixture tables | permissive (unchanged) |
| `retail` | accounts, orders_ext, order_lines, catalog_products, payments, web_events, regions, suppliers | broad; PII on `accounts` **masked**; `staff` **walled off** |
| `analytics` | regions, accounts, orders_ext, order_lines, web_events | **tighter**: PII **denied**, aggregate **k-anonymity**, low row cap, mandatory active-only filter |

Start with `list_connections` and confirm **no `connection_string` /
credential field comes back** on any of them — only id/dialect/description.
That's the first security invariant, checked live.

---

## A. Does security hold up?

Each of these **should be rejected** (or silently constrained). If any one
returns raw data it shouldn't, that's a real finding.

### A1. Walled-off table — `staff` is invisible to `retail`

`staff` holds `ssn`, `salary`, `date_of_birth`. It is known to the database
but absent from every connection's `allowed_tables`.

```jsonc
// name: run_structured_queries
{ "connection": "retail",
  "queries": [{ "from": "staff", "select": ["staff.name", "staff.salary"], "limit": 5 }] }
```
**Expect:** rejected — `staff` is not an allowed table. Try `list_tables` on
`retail` too: `staff` never appears.

### A2. Denied column — `analytics` cannot see PII at all

```jsonc
// name: run_structured_queries
{ "connection": "analytics",
  "queries": [{ "from": "accounts", "select": ["accounts.name", "accounts.email"], "limit": 5 }] }
```
**Expect:** rejected — `accounts.email` is denied on `analytics`. Same for
`national_id` and `phone`. Now run the identical query on `retail` (next).

### A3. Masked column — `retail` sees only a safe projection

```jsonc
// name: run_structured_queries
{ "connection": "retail",
  "queries": [{ "from": "accounts",
    "select": ["accounts.name", "accounts.national_id", "accounts.phone"], "limit": 5 }] }
```
**Expect:** succeeds, but `national_id` comes back **hashed** and `phone` shows
only its **last 4** — the database never returned the raw value. Now try to use
a masked column in a *filter* (inference attempt):

```jsonc
// name: run_structured_queries
{ "connection": "retail",
  "queries": [{ "from": "accounts", "select": ["accounts.name"],
    "where": { "col": "accounts.national_id", "op": "eq", "value": "123-45-6789" }, "limit": 5 }] }
```
**Expect:** rejected — a masked column may only appear as a bare SELECT item;
filtering/joining/sorting on it would leak the real value by inference.

### A4. k-anonymity — `analytics` suppresses razor-thin aggregate groups

```jsonc
// name: run_structured_queries
{ "connection": "analytics",
  "queries": [{ "from": "orders_ext",
    "select": ["orders_ext.account_id", { "fn": "sum", "col": "orders_ext.order_total", "as": "spend" }],
    "group_by": ["orders_ext.account_id"],
    "order_by": [{ "col": "spend", "dir": "desc" }], "limit": 20 }] }
```
**Expect:** any per-account group backed by fewer than 5 rows is **dropped**
(a `HAVING count(*) >= 5` is injected) — you can't single out an individual by
aggregating over a thin slice. Run the same on `retail` and the thin groups
reappear.

### A5. Mandatory row filter — `analytics` silently scopes to active accounts

```jsonc
// name: run_structured_queries
{ "connection": "analytics",
  "queries": [{ "from": "accounts", "select": ["accounts.status", { "fn": "count", "col": "*", "as": "n" }],
    "group_by": ["accounts.status"] }] }
```
**Expect:** only `status = active` rows are counted — a mandatory
`accounts.is_active = true` filter is AND-ed into every `analytics` query
touching `accounts`, so `churned`/`suspended` never appear even though you
didn't filter them out. On `retail` all three statuses show up.

### A6. Policy caps — resource-exhaustion guardrails

Try to blow past each cap on `retail`/`demo` and watch it get refused before
touching the database:

- **Too many joins** (`max_joins: 4`): a query with 5+ joins → rejected.
- **Row cap** (`retail max_limit: 500`): `"limit": 100000` → clamped/rejected.
- **`in`-list size** (`max_in_list_size: 1000`): an `in` predicate with a
  2000-element list → rejected.
- **Where-predicate count** (`max_where_predicates: 100`): a single `or` with
  hundreds of terms → rejected (depth checks alone wouldn't catch this).
- **CASE branches** (`max_case_branches: 10`): a CASE with 11 `when`s →
  rejected.

```jsonc
// name: run_structured_queries — in-list bomb
{ "connection": "retail",
  "queries": [{ "from": "orders_ext", "select": ["orders_ext.order_id"],
    "where": { "col": "orders_ext.account_id", "op": "in",
               "value": [1,2,3, "... paste 2000 ids ..."] }, "limit": 10 }] }
```

### A7. Cross-connection join is refused across join_groups

`retail` (join_group `commerce`) and `analytics` (join_group `reporting`) don't
share a group, so a join across them is rejected at validation:

```jsonc
// name: run_structured_queries
{ "connection": "retail",
  "queries": [{ "from": "orders_ext", "select": ["orders_ext.order_id"],
    "joins": [{ "table": "accounts", "connection": "analytics",
                "on": ["orders_ext.account_id", "accounts.account_id"] }], "limit": 5 }] }
```
**Expect:** rejected — join_group mismatch. (Note: a *successful* cross-database
join compiles to three-part `db.dbo.table` names and only executes on MSSQL;
on Postgres this path is validated but not runnable. The rejection above is
fully exercisable on Postgres.)

### A8. Credential redaction is structural, not conventional

`list_connections` and `describe_table` never carry a connection string. Try
to find one anywhere in any MCP response — you won't, because
`PublicConnectionInfo` has no such field. The audit log (Section B4) likewise
never records SQL text, predicate values, or rows.

---

## B. Do the features hold up?

### B1. Schema discovery + catalog overlay

```jsonc
// name: describe_table
{ "connection": "retail", "table_name": "orders_ext" }
```
**Expect:** columns plus a `catalog` object per table/column — business
descriptions, aliases (`orders`/`purchases`/`sales`), relationship hints to
`accounts`/`order_lines`/`payments`, and sensitivity labels — all filtered by
your policy (a hint pointing at a denied table/column is dropped).

### B2. Semantic catalog search (metadata only, never row values)

```jsonc
// name: search_catalog
{ "connection": "retail", "query": "customer lifetime revenue", "limit": 5 }
```
**Expect:** ranked tables/columns from curated metadata + reflected schema —
`accounts.lifetime_value`, `orders_ext.order_total`, etc. It never reads a
single database row.

### B3. Explain mode — see the SQL without running it

Append `"mode": "explain"` to any `run_structured_queries` call to get the
compiled SQL + bind params back instead of rows, without touching the DB or the
concurrency limiter. Great for confirming *exactly* what a mask/mandatory
filter/k-anonymity rule injected — diff the explained SQL on `retail` vs
`analytics` for the same query.

### B4. Audit + observability

Every attempt (allowed or rejected) is logged. Open the admin UI at
`http://localhost:8000/admin/` with your admin key, or hit
`GET /api/v1/admin/observability/overview` — watch query volume and
**rejection categories** climb as you run Section A. Confirm the persisted
JSONL audit events contain no SQL, no predicate values, no rows.

### B5. Capacity behavior under load

`make test-load` / `make test-soak SOAK_ROUNDS=200` drive real concurrent
Postgres load against these tables and assert the concurrency/timeout
guardrails hold. Interactively, add `"queue_mode": "fail_fast"` (or `"wait"`
with `"wait_timeout_seconds"`) to a `run_structured_queries` call and fire a
burst — capacity rejections come back per-item with `admission_state`, never
as a crash.

---

## C. Does the StructuredQuery engine hold up?

This is where the volume pays off — every query below returns something
meaningful because there are ~4 years and hundreds of thousands of rows behind
it. All on `retail` (Postgres) unless noted.

### C1. Multi-hop join + aggregate + group_by

Revenue by region and product category (order_lines → orders_ext → regions,
and order_lines → catalog_products):

```jsonc
// name: run_structured_queries
{ "connection": "retail",
  "queries": [{
    "from": "order_lines",
    "select": ["regions.name", "catalog_products.category",
               { "fn": "sum", "col": "order_lines.line_total", "as": "revenue" },
               { "fn": "count", "col": "*", "as": "lines" }],
    "joins": [
      { "table": "orders_ext", "on": ["order_lines.order_id", "orders_ext.order_id"] },
      { "table": "regions", "on": ["orders_ext.region_id", "regions.region_id"] },
      { "table": "catalog_products", "on": ["order_lines.product_id", "catalog_products.product_id"] }
    ],
    "group_by": ["regions.name", "catalog_products.category"],
    "order_by": [{ "col": "revenue", "dir": "desc" }],
    "limit": 25,
    "intent": "revenue by region and category" }] }
```

### C2. Date bucketing at every granularity

Swap `granularity` through `day`/`week`/`month`/`quarter`/`year` — the ~4-year
window means each returns a real trend:

```jsonc
// name: run_structured_queries
{ "connection": "retail",
  "queries": [{
    "from": "orders_ext",
    "select": [{ "col": "orders_ext.placed_at", "granularity": "month", "as": "bucket" },
               { "fn": "sum", "col": "orders_ext.order_total", "as": "revenue" }],
    "group_by": ["bucket"],
    "order_by": [{ "col": "bucket", "dir": "asc" }],
    "limit": 60 }] }
```

### C3. Top-N per partition — top 3 accounts by spend per region

```jsonc
// name: run_structured_queries
{ "connection": "retail",
  "queries": [{
    "from": "orders_ext",
    "select": ["orders_ext.region_id", "orders_ext.account_id",
               { "fn": "sum", "col": "orders_ext.order_total", "as": "spend" }],
    "group_by": ["orders_ext.region_id", "orders_ext.account_id"],
    "top_n": { "partition_by": ["orders_ext.region_id"],
               "order_by": [{ "col": "spend", "dir": "desc" }], "n": 3 },
    "limit": 50 }] }
```

### C4. Percentile + spread stats (median, stddev, variance)

```jsonc
// name: run_structured_queries
{ "connection": "retail",
  "queries": [{
    "from": "orders_ext",
    "select": ["orders_ext.channel",
               { "col": "orders_ext.order_total", "fraction": 0.5, "as": "median_total" },
               { "fn": "stddev", "col": "orders_ext.order_total", "as": "sd" },
               { "fn": "avg", "col": "orders_ext.order_total", "as": "mean" }],
    "group_by": ["orders_ext.channel"] }] }
```

### C5. NULL handling — COALESCE, NULLS ordering, is_null

`accounts.last_login_at` is ~20% NULL and `catalog_products.discontinued_at` is
NULL for still-sold products. Exercise all three NULL surfaces:

```jsonc
// name: run_structured_queries
{ "connection": "retail",
  "queries": [{
    "from": "accounts",
    "select": ["accounts.name", "accounts.last_login_at"],
    "where": { "col": "accounts.last_login_at", "op": "is_null" },
    "order_by": [{ "col": "accounts.signup_at", "dir": "desc", "nulls": "last" }],
    "limit": 10 }] }
```
Then a `coalesce` scalar projection to substitute a default for the NULL, and an
`order_by` with `"nulls": "first"` to see the ordering flip.

### C6. CASE + scalar functions

Bucket products into price bands with CASE, and normalize text with
`lower`/`concat`:

```jsonc
// name: run_structured_queries
{ "connection": "retail",
  "queries": [{
    "from": "catalog_products",
    "select": ["catalog_products.name",
      { "when": [
          { "when": { "col": "catalog_products.list_price", "op": "lt", "value": 50 }, "then": { "literal": "budget" } },
          { "when": { "col": "catalog_products.list_price", "op": "lt", "value": 200 }, "then": { "literal": "mid" } }
        ], "else": { "literal": "premium" }, "as": "price_band" }],
    "limit": 20 }] }
```

### C7. array_agg / string_agg (Postgres)

Collect the SKUs on each order into an array (retail is Postgres, so
`array_agg` works — on MSSQL the adapter would reject it, by design):

```jsonc
// name: run_structured_queries
{ "connection": "retail",
  "queries": [{
    "from": "order_lines",
    "select": ["order_lines.order_id",
               { "col": "catalog_products.sku", "as": "skus" }],
    "joins": [{ "table": "catalog_products",
                "on": ["order_lines.product_id", "catalog_products.product_id"] }],
    "group_by": ["order_lines.order_id"],
    "limit": 10 }] }
```
(Use the `array_agg`/`string_agg` select-item shape — `{col, as}` and
`{col, delimiter, as}` respectively.)

### C8. Column-vs-column predicate + negative money

Find loss-making lines (sold below cost) and refund payments:

```jsonc
// name: run_structured_queries
{ "connection": "retail",
  "queries": [{
    "from": "order_lines",
    "select": ["order_lines.line_id", "order_lines.unit_price", "catalog_products.cost"],
    "joins": [{ "table": "catalog_products",
                "on": ["order_lines.product_id", "catalog_products.product_id"] }],
    "where": { "col": "order_lines.unit_price", "op": "lt", "value_col": "catalog_products.cost" },
    "limit": 20 }] }
```
And `payments` where `amount < 0` (refunds) — negative and zero amounts are
seeded on purpose.

### C9. Self-join (optional toggle)

The manager chain lives on `staff.manager_id → staff.staff_id`, but `staff` is
walled off by default. To try a self-join, **temporarily** add `staff` to
`retail.allowed_tables` in `policy.example.yaml` and reload — which itself
demonstrates a policy change opening access — then:

```jsonc
// name: run_structured_queries
{ "connection": "retail",
  "queries": [{
    "from": "staff", "from_alias": "e",
    "select": ["e.name", "m.name"],
    "joins": [{ "table": "staff", "alias": "m", "type": "left",
                "on": ["e.manager_id", "m.staff_id"] }],
    "limit": 20 }] }
```
Put the wall back when you're done.

### C10. Batch — many queries in one call, isolated failures

Pass several objects in `queries`; results come back in order and one failing
query never fails the others. Mix a valid query with a deliberately walled one
(`from: staff`) and confirm only that item carries an `error`.

---

## What "tight" looks like

By the end you should have seen, with your own eyes:

- the **only** input is a StructuredQuery AST — no raw-SQL escape hatch exists;
- walls (table/column/mask/k-anon/mandatory-filter/caps/join_group) reject the
  right things **before** touching data, and the *same* query gets *different*
  answers on `retail` vs `analytics`;
- credentials never appear on any returned model or audit event;
- the engine expresses real analytical SQL — multi-hop and self joins,
  aggregates, percentiles, date buckets, top-N-per-group, CASE, NULL handling —
  bounded only by policy and by what the dialect genuinely supports.

If something here *doesn't* behave as described, that's exactly the kind of gap
worth turning into a TODO item or a security regression test (see
`make test-security` and the `adversarial-probe` skill).
