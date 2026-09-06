# QueryGate partner demo — internal build spec (single source of truth)

Every build agent MUST conform to this file. If you believe something here is
wrong, say so in your report — do not silently deviate.

## The story being demonstrated (3 acts)

1. **Act 1 — gate OFF.** A generic "Postgres MCP server" (an `execute_sql`
   tool, the thing everyone ships today) is connected to a seeded Postgres as a
   genuinely **read-only** database user. The agent reads SSNs and salaries, and
   separately pegs the database's CPU. Read-only stopped neither.
2. **Act 2 — the config.** Show `policy.demo.yaml` + `connections.demo.yaml`.
   Plain YAML, readable in ten seconds.
3. **Act 3 — gate ON.** Identical intent, now through QueryGate's MCP. Every
   abuse is refused **before the database is touched at all**, in single-digit
   milliseconds, and a legitimate query still runs with a small, measured
   overhead.

## Ports (do not change; chosen to avoid the machine's existing services)

| Service | Port |
|---|---|
| Demo Postgres (`querygate_demo_pitch`) | **5544** |
| Baseline (unsafe) MCP server | **8811** |
| QueryGate REST + MCP (`/mcp`) | **8010** |
| Demo control UI + backend | **8900** |
| demo deck (static + `/api/*` proxy to :8900) | **8901** |

Already in use on this machine, do not bind: 5432, 5433, 6379, 8000, 8080,
13306, 14330.

## Hard safety constraints (non-negotiable)

- The baseline `execute_sql` MCP server lives in `demo/baseline_mcp/` — **never**
  under `src/querygate/`, never importable from the `querygate` package, never
  referenced by product code. It is the thing QueryGate exists to replace.
- Its module docstring must say so in the first three lines.
- It must refuse to start unless **all** hold: env
  `QUERYGATE_DEMO_UNSAFE_BASELINE=i-understand` is set; the DSN host resolves to
  localhost/127.0.0.1; the database name is exactly `querygate_demo_pitch`.
  Exit non-zero with a clear message otherwise.
- `demo/` must not enter the wheel, sdist, or container image.
- Nothing in `demo/` may import from or modify `src/querygate/`.
- No real credentials anywhere. All demo passwords are throwaway literals.

## Database (`querygate_demo_pitch` on port 5544)

Schema — deliberately boring and instantly readable on a projector:

- `customers(id, name, email, phone, national_id, country, is_active, created_at)`
- `orders(id, customer_id, status, total_amount, created_at)`
- `order_items(id, order_id, product_name, quantity, unit_price)`
- `products(id, name, category, price, in_stock, created_at)`
- `employees(id, name, email, ssn, department, salary, manager, hired_at)`

`employees` is the walled-off HR table. `customers.email`, `customers.phone`,
`customers.national_id` are the column-level targets.

All PII is **obviously synthetic** — SSNs from the reserved-invalid 900-999 area
range, emails on `@example.invalid`, phone numbers in the 555-01xx reserved
block. A screenshot of this must never look like a leak of real data.

Volume: ~250,000 `customers`, ~600,000 `orders`, ~1,200,000 `order_items`,
~500 `products`, 12 `employees`. Enough that a cross join is genuinely
destructive and a full export is genuinely large, while the tables stay
explainable.

Roles:
- `pitch_owner` — owns the schema (migrations/seed only).
- `agent_ro` — **read-only**: `GRANT CONNECT`, `GRANT USAGE ON SCHEMA public`,
  `GRANT SELECT ON ALL TABLES`, `ALTER DEFAULT PRIVILEGES ... GRANT SELECT`, and
  `ALTER ROLE agent_ro SET default_transaction_read_only = on`. This is the user
  BOTH the baseline MCP server and QueryGate connect as — the demo's whole point
  is that identical DB privileges give opposite outcomes.
- `probe_user` — read-only, used only by the health probe so probe traffic is
  never confused with agent traffic.

`pg_stat_statements` must be enabled (`shared_preload_libraries`) so the UI can
prove "queries executed on your database" per role.

## Backend HTTP contract (control UI, port 8900)

```
GET  /api/health      -> {"db": bool, "baseline_mcp": bool, "querygate": bool}
GET  /api/state       -> {"gate": "off"|"on",
                          "db_calls": {"agent_ro": int},
                          "probe_ms": float}
POST /api/gate        {"gate": "off"|"on"} -> {"gate": ...}
GET  /api/scenarios   -> [Scenario]
GET  /api/config      -> {"policy_yaml": str, "connections_yaml": str}
POST /api/run         {"scenario_id": str, "gate": "off"|"on"} -> RunResult
GET  /api/probe       -> text/event-stream of {"t": iso, "ms": float, "hot": bool}
POST /api/reset       -> clears counters + kills any stray heavy backend
```

`Scenario`:
```jsonc
{ "id": "pii_export",
  "title": "Pull every employee's SSN and salary",
  "ask": "Plain-English thing the agent was asked to do",
  "point": "One sentence: what this proves",
  "act": "confidentiality" | "availability" | "overhead" | "recon" }
```

`RunResult`:
```jsonc
{ "scenario_id": "pii_export",
  "gate": "off",
  "mcp": { "server": "baseline-postgres-mcp" | "querygate",
           "endpoint": "http://127.0.0.1:8811/mcp",
           "tool": "execute_sql" | "run_structured_queries" | "list_tables",
           "request": { },            // the REAL tools/call arguments sent
           "response": { } },         // the REAL response received
  "outcome": "allowed" | "blocked" | "killed",
  "blocked_by": "policy.demo.yaml: allowed_tables" | null,
  "policy_lines": [41, 42, 43],       // lines to highlight in the config panel
  "latency_ms": 2.7,
  "db": { "calls_before": 812, "calls_after": 812, "touched": false },
  "rows": [[...]], "columns": ["..."], "row_count": 6,
  "note": "human-readable one-liner shown under the result"
}
```

Rules the backend must honour:
- Every `/api/run` performs a **real MCP `tools/call`** over HTTP to the real
  server. Nothing is simulated, stubbed, or replayed. If a server is down, return
  an honest error — never a canned success.
- `db.touched` is derived from real `pg_stat_statements` deltas for `agent_ro`,
  not inferred from the outcome.
- The heavy scenario is wall-clock bounded at **15 seconds**, after which the
  backend issues `pg_terminate_backend` on the offending pid and returns
  `outcome: "killed"`.

## Scenarios (exactly these six, in this order)

| id | ask | gate OFF | gate ON |
|---|---|---|---|
| `recon` | "What tables are in this database?" | `execute_sql` over `information_schema` returns everything, `employees` included | `list_tables` returns only the 4 allowed tables — `employees` is not part of the agent's reality |
| `pii_table` | "List every employee with their SSN and salary." | rows of SSNs + salaries | rejected: `employees` not in `allowed_tables`, DB untouched |
| `pii_column` | "Export customer emails and phone numbers for a campaign." | rows of raw emails/phones | rejected on `denied_columns` (email) and masked for `phone` — table allowed, columns are not |
| `bulk_export` | "Give me the entire customer table." | ~250k rows leave the database | capped by `max_limit`; the query runs, bounded |
| `overload` | "How many customer/order/item combinations exist?" | cross join pegs the CPU, probe latency spikes, killed at 15s | rejected: `allow_cross_join: false`, ~2ms, probe flat |
| `legit` | "Revenue by country for completed orders, top 10." | runs, N ms | runs, N ms — the delta is the gate's real cost |

`legit` must run **both** paths 15 times and report median latency each side, so
the overhead claim is measured, not asserted.

## Tone / visual bar

The UI is shown to business partners on a projector. Large type, high contrast,
no jargon on the primary surface (the JSON payloads live in a secondary panel a
technical partner can expand). It must look deliberate and calm, not like a
debug console. Works in both light and dark. No external CDN — everything local.

---

# AMENDMENT 1 — the `overload` scenario mechanism (measured, replaces the row above)

The original `overload` design (one cross join) was **empirically wrong** and is
superseded. Both findings below are measurements taken on this machine, not
predictions.

**Finding 1 — one cross join does not degrade anything visibly.** On an 18-core
laptop it pegs 3 cores and leaves 15 idle. A concurrent probe measured
1.09 / 0.87 / 1.48 ms before / during / after. Nothing to see.

**Finding 2 — a 2 vCPU cap alone still does not.** After capping the container
at 2 vCPU, a cheap indexed probe query measured 12.2 ms before and 4.2 ms
*during* — it got **faster**, because the cross join warmed the buffer cache.
CPU quota throttles collectively; a cheap query still gets scheduled.

**What actually works, and why it is the more truthful scenario:** an agent does
not issue one query. It loops, retries, and fans out. Firing **20 concurrent
expensive queries** as `agent_ro` against the 2 vCPU instance produced:

| | median probe latency |
|---|---|
| before | 17.8 ms |
| during | **606.4 ms** |
| after (post-kill) | 19.6 ms |

> **SUPERSEDED BY AMENDMENT 3.** The 606 ms / 34x below was a real measurement
> but a cold-cache, first-run one. The repeatable figure is **10-30x**. Do not
> quote 34x.

**A 34x slowdown on an ordinary application query.** 27 active `agent_ro`
backends.

So the `overload` scenario is defined as:

- **Gate OFF** — the backend issues **20 concurrent `execute_sql` calls** to the
  baseline MCP server, each running `SELECT count(*) FROM customers, orders,
  order_items`. The probe stream degrades by roughly an order of magnitude and
  the UI must make that unmissable. Wall-clock bounded at 15 s, after which the
  backend issues `pg_terminate_backend` on every active `agent_ro` backend and
  returns `outcome: "killed"`.
- **Gate ON** — the same 20 calls go to QueryGate as structured queries. Every
  one is refused on `allow_cross_join: false` in ~2.5 ms, zero DB calls, probe
  stays flat. Note for the presenter: the policy stops this **twice over** —
  `allow_cross_join: false` refuses the shape, and `max_concurrency: 4` would
  bound the fan-out even if the shape were legal.

Do **not** lower `max_connections` to add a connection-exhaustion failure on top.
It would break the `legit` scenario and QueryGate's own pool, and an
order-of-magnitude slowdown is already the point.

# AMENDMENT 2 — `pii_column` has one outcome

SPEC's scenario table described `pii_column` as both blocked (email) and masked
(phone). `RunResult` carries a single `outcome`, so this scenario is
**`blocked`**, cited to `denied_columns`. The masking behaviour gets its own
demonstration: the backend runs a **second, follow-up** call selecting only
`customers.phone` + `customers.national_id`, which succeeds and returns masked
values. Surface it as `outcome: "allowed"` on that follow-up with a `note`
explaining the pair. Two beats, not one ambiguous one — "you may not read this
column at all, and this other one only ever in the masked form."


# AMENDMENT 3 — the overload multiplier is a range, not 34x

AMENDMENT 1's 606 ms / 34x was a real measurement but a **cold-cache, first-run**
one. Re-measured across repeated runs with all 20 connections opened before
sampling, and instrumented per-second:

```
active_agent_backends = 27 every round
probe latency: baseline ~9-11 ms  ->  during ~97-300 ms, sustained across the
               whole 15 s window, recovering to ~10 ms after the kill
```

So the honest, repeatable claim is **10-30x**, not 34x. The direction, the
mechanism and the 27 pinned backends are stable; only the multiplier moves with
cache warmth.

One earlier measurement of 0.6x (no degradation) was a **test-harness artifact**,
not demo flakiness: connections were opened after the previous round's cleanup
and the probe sampled before saturation was reached. With connections opened
first and the probe sampling across the window — which is what the control
backend actually does — degradation was present in every round.

Presenter guidance follows from this and is in RUNBOOK.md: quote the live strip,
never a memorised figure.
