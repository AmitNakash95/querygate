# demo/control — the partner-demo control UI + backend

Not part of the QueryGate product. This is the presenter-facing surface for
the live partner demo (`demo/SPEC.md`): a FastAPI backend on
`127.0.0.1:8900` serving `demo/control/static/` (finished, untouched by this
backend) and implementing the HTTP contract in `demo/SPEC.md`.

## What it does

Every `POST /api/run` performs a **real MCP `tools/call`** over HTTP against
either the baseline (unsafe) MCP server (`demo/baseline_mcp/`, gate OFF) or
QueryGate's own MCP server (gate ON) — nothing is simulated, stubbed, or
replayed. `db.touched` is derived from a real `pg_stat_statements` delta for
the `agent_ro` role, snapshotted immediately before and after each scenario's
call(s). The `/api/probe` SSE stream runs a real application-shaped query
against Postgres roughly every 400ms, on its own connection as `probe_user`,
so probe traffic is never confused with agent traffic.

## Files

- `app.py` — FastAPI app; the six `/api/*` routes from `demo/SPEC.md`, plus
  static file serving.
- `scenarios.py` — the six scenarios (`recon`, `pii_table`, `pii_column`,
  `bulk_export`, `overload`, `legit`), each gate OFF/ON.
- `mcp_client.py` — shared `tools/call` HTTP client; parses both plain JSON
  and Server-Sent Events responses (the two demo MCP servers differ on this).
- `db.py` — real Postgres helpers: `pg_stat_statements` snapshots,
  `pg_terminate_backend` for the overload kill + `/api/reset`, and the
  background probe broadcaster (SSE fan-out to every connected browser tab).
- `policy_lines.py` — locates a named rule's line numbers in
  `policy.demo.yaml` live, by searching the file, not by hardcoded numbers.
- `config.py` — ports, DSNs, and other constants (all overridable by env var;
  defaults match the throwaway literals already committed elsewhere in
  `demo/`).

## Running it

Bring up the three dependencies first (each has its own README):

```bash
docker compose -f demo/db/docker-compose.demo.yml up -d   # Postgres, :5544
demo/baseline_mcp/run.sh                                   # :8811
demo/config/run_querygate.sh                                # :8010
```

Then:

```bash
demo/control/run.sh          # :8900
```

Open `http://127.0.0.1:8900/`. `/api/health` reports which of the three
upstream services are actually reachable — a scenario against a downed
server returns an honest HTTP error, never a fabricated success.

## Known, disclosed simplifications

- **Baseline responses with no `LIMIT`** (`pii_column`, `bulk_export` on the
  gate-OFF path) genuinely fetch the full, real result from Postgres (up to
  250,000 rows) so the true `row_count` is real — but the `rows` preview
  returned to the browser, and the `mcp.response` shown in the payload
  drawer, are truncated to the first 20 rows (`_truncated_baseline_response`
  in `scenarios.py`). This is disclosed via an explicit `_display_note` field
  rather than done silently; the true `row_count` is always the untruncated
  total. Without this, one `pii_column` run measured a ~40MB response and one
  `bulk_export` run measured ~90MB — real, but unusable to hand a browser's
  JSON viewer.
- **`legit` runs 30 real MCP calls per click** (15 baseline + 15 QueryGate,
  per `demo/SPEC.md`'s own requirement that the overhead be measured live,
  not quoted) — expect roughly 2-3 seconds for that scenario specifically.
- **`overload` (gate OFF) always takes the full 15-second wall-clock bound.**
  A cross join over 250k × 600k × 1.2M rows does not finish in 15s on the
  demo's 2 vCPU instance, so the scenario reliably hits AMENDMENT 1's bound
  and returns `outcome: "killed"` every time, by design.
