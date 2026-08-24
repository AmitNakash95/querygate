"""The six demo scenarios (demo/SPEC.md + AMENDMENT 1 + AMENDMENT 2).

Every function here performs REAL MCP tools/call(s) over HTTP against a real
running server, and derives db.touched from a REAL pg_stat_statements delta.
Nothing is simulated, canned, or replayed. If a server is unreachable, the
underlying MCPTransportError propagates up to app.py, which turns it into an
honest HTTP error response — never a fabricated success.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from typing import Any, Optional

import httpx

from . import audit, config, db
from .mcp_client import MCPCallResult, MCPTransportError, call_tool
from .policy_lines import lines_for, masked_columns_for

logger = logging.getLogger("querygate.demo.control.scenarios")

SCENARIOS: list[dict] = [
    {
        "id": "recon",
        "title": "Recon the schema",
        "ask": "What tables are in this database?",
        "point": "Off the gate, the agent's reality includes employees. On the gate, it never existed.",
        "act": "recon",
    },
    {
        "id": "pii_table",
        "title": "Pull the HR table",
        "ask": "List every employee with their SSN and salary.",
        "point": "A whole table walled off — rejected before the database is even opened.",
        "act": "confidentiality",
    },
    {
        "id": "pii_column",
        "title": "Export customer contact data",
        "ask": "Export customer emails and phone numbers for a campaign.",
        "point": "Table allowed, columns are not — deny and mask are two different rules.",
        "act": "confidentiality",
    },
    {
        "id": "bulk_export",
        "title": "Bulk export",
        "ask": "Give me the entire customer table.",
        "point": "The query still runs — just capped, not refused.",
        "act": "confidentiality",
    },
    {
        "id": "overload",
        "title": "Cross-join overload",
        "ask": "How many customer / order / item combinations exist?",
        "point": "Read-only didn't stop the CPU from getting pegged. The gate refuses the shape outright — twice over.",
        "act": "availability",
    },
    {
        "id": "legit",
        "title": "Legitimate report",
        "ask": "Revenue by country for completed orders, top 10.",
        "point": "Same answer, both paths — the overhead is measured live, not asserted.",
        "act": "overhead",
    },
]

SCENARIO_BY_ID = {s["id"]: s for s in SCENARIOS}

# Rows previews are capped for display — some baseline responses here are
# genuinely 250,000 rows (see the pii_column / bulk_export "off" paths). The
# real row_count always reflects the true total; only the *preview* list
# sent back to the browser is truncated, exactly the way mock-data.js's own
# fixtures preview 5-8 rows out of a much larger row_count.
ROWS_PREVIEW_LIMIT = 20


def _qg_headers() -> dict:
    return {"Authorization": f"Bearer {config.QUERYGATE_MCP_API_KEY}"}


def _row_count_or_len(explicit_row_count: Any, rows: list) -> int:
    """Prefer an explicit row_count from a real response, falling back to
    len(rows) only when it's genuinely absent. Plain `dict.get(key, default)`
    is the wrong tool here: QueryGate's rejected-query responses carry a real
    `"row_count": null` key (present, not missing), so `.get(key, len(rows))`
    silently returns None instead of falling back — this bit run_pii_table's
    'unexpectedly allowed' display once already."""
    return explicit_row_count if explicit_row_count is not None else len(rows)


def _dict_rows_to_columns_and_lists(rows: list[dict]) -> tuple[list[str], list[list[Any]]]:
    """QueryGate's run_structured_queries returns rows as a list of dicts
    (column name -> value). The frontend contract wants columns[] + rows as
    list-of-lists (mock-data.js's own fixtures are exactly this shape)."""
    if not rows:
        return [], []
    columns = list(rows[0].keys())
    return columns, [[row.get(c) for c in columns] for row in rows]


def _truncated_baseline_response(payload: dict) -> dict:
    """The literal, real response from the baseline server's execute_sql can
    be enormous (bulk_export/pii_column with no LIMIT genuinely return
    250,000 rows — one such response measured ~40-90MB of JSON). Sending that
    whole thing back through our own API and into the browser's JSON
    highlighter would hang the page, so the payload-drawer copy is truncated
    to the same preview size as the rows table, with the true row_count kept
    intact and the truncation disclosed explicitly rather than silently."""
    rows = payload.get("rows") or []
    row_count = _row_count_or_len(payload.get("row_count"), rows)
    out = dict(payload)
    if len(rows) > ROWS_PREVIEW_LIMIT:
        out["rows"] = rows[:ROWS_PREVIEW_LIMIT]
        out["_display_note"] = (
            f"real response truncated to {ROWS_PREVIEW_LIMIT} of {row_count} rows for display "
            "in this panel; row_count above is the true total from the real response"
        )
    return out


def _blocked_by_from_error(error: Optional[str]) -> Optional[str]:
    """Map a QueryGate rejection message to the policy.demo.yaml rule that
    most likely produced it, for display citation only (F11). Never guess:
    an error whose text doesn't match a known pattern leaves blocked_by
    null rather than mislabeling it as a different rule — the config panel
    highlighting the wrong lines is worse than highlighting nothing."""
    if not error:
        return None
    low = error.lower()
    if "column" in low and "not accessible" in low:
        return "policy.demo.yaml: denied_columns"
    if "table" in low and "not accessible" in low:
        return "policy.demo.yaml: allowed_tables"
    if "cross join" in low:
        return "policy.demo.yaml: allow_cross_join"
    if "max_limit" in low or ("limit" in low and "exceed" in low):
        return "policy.demo.yaml: max_limit"
    return None


def _all_calls_failed(completed_results: list[dict]) -> bool:
    """True iff there is at least one completed call AND every one of them
    failed (`"ok": False`) — the F4 guard in run_overload's gate-off path
    uses this to tell "every concurrent call failed outright" (e.g. the
    baseline server was down for the whole run — an honest error, never a
    fabricated 'killed' success) apart from "some calls succeeded, or some
    are still pending at the wall-clock bound" (real load; 'killed' is the
    honest outcome). Extracted to its own function — rather than left as
    the inline boolean expression it started as — specifically so it has
    its own regression test (test_scenarios_logic.py): this repo's working
    agreement calls out this exact shape of bug (a swapped `and`/`or`
    silently changing what a guard actually enforces) as invisible to both
    a passing suite and a careful read unless something exercises the
    boundary directly."""
    return bool(completed_results) and all(not c.get("ok", False) for c in completed_results)


def _reject_if_call_failed(call: MCPCallResult, server_label: str, tool: str) -> MCPCallResult:
    """Raise MCPTransportError for a genuine protocol- or tool-level MCP
    failure (bad connection id, malformed request, a raw exception inside a
    tool) rather than let it fall through as an empty payload.

    This is NOT triggered by an expected policy rejection: QueryGate reports
    'table not accessible'/'column not accessible'/'cross join not allowed'
    as a `results[0].error` string inside a normal, successful
    (isError=false) tool response — confirmed live during development. Only
    a real protocol-level JSON-RPC error or a tool that itself raised
    (isError=true) reaches this branch. Without this guard, every scenario's
    `results = call.payload.get("results", [])` silently degrades to `[]` on
    such a failure, and every scenario reports `outcome: "allowed"` with 0
    rows — a fabricated success, exactly what demo/SPEC.md's "never a canned
    success" rule forbids. Centralized here so all twelve scenario/gate call
    sites get the guard from one place, per the shared-helper pattern the
    rest of this module already uses (call_tool, _with_db_snapshot)."""
    if call.is_protocol_error or call.is_tool_error:
        raise MCPTransportError(
            f"{server_label} tool {tool!r} failed instead of returning a normal result: "
            f"{call.payload!r}"
        )
    return call


async def _qg_call(
    client: httpx.AsyncClient, tool: str, arguments: dict, *, request_id: int = 1
) -> MCPCallResult:
    call = await call_tool(
        client,
        config.QUERYGATE_MCP_URL,
        tool,
        arguments,
        request_id=request_id,
        headers=_qg_headers(),
        timeout=config.HTTP_TIMEOUT_SECONDS,
    )
    return _reject_if_call_failed(call, "querygate", tool)


async def _qg_call_with_headline(
    client: httpx.AsyncClient, tool: str, arguments: dict, *, request_id: int = 1
) -> tuple[MCPCallResult, Optional[str]]:
    """Same real MCP call as _qg_call, but also captures the event_id of the
    single real ledger record (if any) THIS SPECIFIC call appended —
    bracketing just this one call's ledger offset the same way
    _with_db_snapshot already brackets pg_stat_statements around one call.

    Used for A4 (partner-demo audit-panel fix session): a multi-call
    scenario's headline audit record must be the one that actually decided
    RunResult["outcome"], not whichever record a frontend heuristic happens
    to guess from a capped preview. Threading the real event_id through here
    — at the moment the deciding call is made, not reconstructed afterward —
    makes it structurally impossible for the two to disagree. Returns
    (call, None) when the call appended no record (e.g. a rejected call that
    genuinely never touched the ledger differently than expected, or a tool
    that isn't a query execution at all)."""
    offset_before = audit.snapshot_ledger_offset()
    call = await _qg_call(client, tool, arguments, request_id=request_id)
    new_records = audit.read_new_records(offset_before)
    event_id = new_records[-1]["event"].get("event_id") if new_records else None
    return call, event_id


async def _baseline_call(
    client: httpx.AsyncClient, tool: str, arguments: dict, *, request_id: int = 1
) -> MCPCallResult:
    call = await call_tool(
        client,
        config.BASELINE_MCP_URL,
        tool,
        arguments,
        request_id=request_id,
        timeout=config.HTTP_TIMEOUT_SECONDS,
    )
    return _reject_if_call_failed(call, "baseline-postgres-mcp", tool)


def _base_run_result(scenario_id: str, gate: str) -> dict:
    return {
        "scenario_id": scenario_id,
        "gate": gate,
        "mcp": {},
        "outcome": "allowed",
        "blocked_by": None,
        "policy_lines": [],
        "latency_ms": 0.0,
        "db": {"calls_before": 0, "calls_after": 0, "touched": False},
        "rows": [],
        "columns": [],
        "row_count": 0,
        # Popped off by run_scenario before the RunResult is returned — the
        # event_id (A4) of the ledger record that decided this run's
        # `outcome`, when a single record can be identified. Defaults to
        # None (honest "not determined/not applicable") rather than being
        # absent, so every scenario runner has the key whether or not it
        # sets it.
        "_headline_event_id": None,
        "note": "",
    }


async def _with_db_snapshot(coro_factory):
    """Run coro_factory() bracketed by real pg_stat_statements snapshots.
    Returns (result_of_coro, calls_before, calls_after)."""
    before = await db.snapshot_agent_ro_calls()
    result = await coro_factory()
    after = await db.snapshot_agent_ro_calls()
    return result, before, after


# ---------------------------------------------------------------------------
# recon
# ---------------------------------------------------------------------------


async def run_recon(client: httpx.AsyncClient, gate: str) -> dict:
    r = _base_run_result("recon", gate)

    if gate == "off":

        async def go():
            return await _baseline_call(client, "list_tables", {})

        call, before, after = await _with_db_snapshot(go)
        tables = call.payload.get("tables", []) if isinstance(call.payload, dict) else []
        r["mcp"] = {
            "server": "baseline-postgres-mcp",
            "endpoint": config.BASELINE_MCP_URL,
            "tool": "list_tables",
            "request": call.request_body["params"]["arguments"],
            "response": call.payload,
        }
        r["outcome"] = "allowed"
        r["latency_ms"] = round(call.elapsed_ms, 2)
        r["columns"] = ["table_name"]
        r["rows"] = [[t] for t in tables]
        r["row_count"] = len(tables)
        r["db"] = {"calls_before": before, "calls_after": after, "touched": after != before}
        extra = ""
        if "employees" in tables:
            extra = " employees is right there — nothing about a read-only grant hides a table's existence."
        r["note"] = f"{len(tables)} tables visible, no filtering of any kind.{extra}"
        return r

    async def go():
        return await _qg_call(client, "list_tables", {"connection": config.QUERYGATE_CONNECTION_ID})

    call, before, after = await _with_db_snapshot(go)
    tables = call.payload.get("tables", []) if isinstance(call.payload, dict) else []
    r["mcp"] = {
        "server": "querygate",
        "endpoint": config.QUERYGATE_MCP_URL,
        "tool": "list_tables",
        "request": call.request_body["params"]["arguments"],
        "response": call.payload,
    }
    r["outcome"] = "allowed"
    r["latency_ms"] = round(call.elapsed_ms, 2)
    r["columns"] = ["table_name"]
    r["rows"] = [[t] for t in tables]
    r["row_count"] = len(tables)
    r["db"] = {"calls_before": before, "calls_after": after, "touched": after != before}
    r["policy_lines"] = lines_for(config.POLICY_YAML_PATH, "allowed_tables")
    r["note"] = (
        f"list_tables returns only what policy.demo.yaml's allowed_tables names ({len(tables)} tables). "
        "employees isn't hidden — it just isn't part of the agent's reality."
    )
    return r


# ---------------------------------------------------------------------------
# pii_table
# ---------------------------------------------------------------------------


async def run_pii_table(client: httpx.AsyncClient, gate: str) -> dict:
    r = _base_run_result("pii_table", gate)

    if gate == "off":
        sql = "select name, ssn, salary from employees"

        async def go():
            return await _baseline_call(client, "execute_sql", {"sql": sql})

        call, before, after = await _with_db_snapshot(go)
        payload = call.payload if isinstance(call.payload, dict) else {}
        columns = payload.get("columns", [])
        rows = payload.get("rows", [])
        row_count = _row_count_or_len(payload.get("row_count"), rows)
        r["mcp"] = {
            "server": "baseline-postgres-mcp",
            "endpoint": config.BASELINE_MCP_URL,
            "tool": "execute_sql",
            "request": {"sql": sql},
            "response": _truncated_baseline_response(payload),
        }
        r["outcome"] = "allowed"
        r["latency_ms"] = round(call.elapsed_ms, 2)
        r["columns"] = columns
        r["rows"] = rows[:ROWS_PREVIEW_LIMIT]
        r["row_count"] = row_count
        r["db"] = {"calls_before": before, "calls_after": after, "touched": after != before}
        r["note"] = (
            f"Every SSN and salary in the company ({row_count} employees), in plain text, "
            "over a connection that's genuinely read-only."
        )
        return r

    arguments = {
        "connection": config.QUERYGATE_CONNECTION_ID,
        "queries": [
            {
                "from": "employees",
                "select": ["employees.name", "employees.ssn", "employees.salary"],
                "limit": 20,
            }
        ],
    }

    async def go():
        return await _qg_call_with_headline(client, "run_structured_queries", arguments)

    (call, headline_event_id), before, after = await _with_db_snapshot(go)
    r["_headline_event_id"] = headline_event_id
    results = call.payload.get("results", []) if isinstance(call.payload, dict) else []
    first = results[0] if results else {}
    error = first.get("error")
    # Populate rows/columns/row_count from the real payload regardless of
    # outcome — same pattern as run_bulk_export. This branch is normally
    # unreachable (the policy correctly blocks 'employees'), but if the
    # allowed_tables enforcement ever silently broke, this is the exact
    # scenario meant to surface it on the projector; the display must show
    # what actually came back, not a blank table that reads as *less*
    # alarming than the truth.
    rows_dicts = first.get("rows") or []
    columns, rows = _dict_rows_to_columns_and_lists(rows_dicts)
    row_count = _row_count_or_len(first.get("row_count"), rows)
    r["mcp"] = {
        "server": "querygate",
        "endpoint": config.QUERYGATE_MCP_URL,
        "tool": "run_structured_queries",
        "request": arguments,
        "response": call.payload,
    }
    r["outcome"] = "blocked" if error else "allowed"
    r["blocked_by"] = "policy.demo.yaml: allowed_tables" if error else None
    r["latency_ms"] = round(call.elapsed_ms, 2)
    r["policy_lines"] = lines_for(config.POLICY_YAML_PATH, "allowed_tables")
    r["db"] = {"calls_before": before, "calls_after": after, "touched": after != before}
    r["columns"] = columns
    r["rows"] = rows[:ROWS_PREVIEW_LIMIT]
    r["row_count"] = row_count
    r["note"] = (
        f"Rejected before compilation: {error}"
        if error
        else f"Unexpectedly allowed — {row_count} rows came back. See the raw response."
    )
    return r


# ---------------------------------------------------------------------------
# pii_column (AMENDMENT 2 — one outcome, two real calls on the gate-ON path)
# ---------------------------------------------------------------------------


async def run_pii_column(client: httpx.AsyncClient, gate: str) -> dict:
    r = _base_run_result("pii_column", gate)

    if gate == "off":
        sql = "select name, email, phone from customers"

        async def go():
            return await _baseline_call(client, "execute_sql", {"sql": sql})

        call, before, after = await _with_db_snapshot(go)
        payload = call.payload if isinstance(call.payload, dict) else {}
        columns = payload.get("columns", [])
        rows = payload.get("rows", [])
        row_count = _row_count_or_len(payload.get("row_count"), rows)
        r["mcp"] = {
            "server": "baseline-postgres-mcp",
            "endpoint": config.BASELINE_MCP_URL,
            "tool": "execute_sql",
            "request": {"sql": sql},
            "response": _truncated_baseline_response(payload),
        }
        r["outcome"] = "allowed"
        r["latency_ms"] = round(call.elapsed_ms, 2)
        r["columns"] = columns
        r["rows"] = rows[:ROWS_PREVIEW_LIMIT]
        r["row_count"] = row_count
        r["db"] = {"calls_before": before, "calls_after": after, "touched": after != before}
        r["note"] = (
            f"Raw emails and phone numbers, unmasked, for all {row_count} customer rows — no limit was asked for."
        )
        return r

    # Gate ON: two real calls. First, email+phone together — rejected
    # outright because email is denied outright. Second (follow-up), phone +
    # national_id alone — succeeds, masked. AMENDMENT 2 + F2: pii_column IS
    # the blocked scenario (cited to denied_columns) — the RunResult
    # surfaces the DENIED call's outcome as the headline, with the masked
    # follow-up's rows kept in the table and the note explaining the pair.
    denied_arguments = {
        "connection": config.QUERYGATE_CONNECTION_ID,
        "queries": [
            {
                "from": "customers",
                "select": ["customers.name", "customers.email", "customers.phone"],
                "limit": 5,
            }
        ],
    }
    masked_arguments = {
        "connection": config.QUERYGATE_CONNECTION_ID,
        "queries": [
            {
                "from": "customers",
                "select": ["customers.name", "customers.phone", "customers.national_id"],
                "limit": 5,
            }
        ],
    }

    # F6: a third snapshot BETWEEN the two calls, so the note can quote a
    # real measured delta for the denied call alone instead of asserting
    # "database untouched" next to a panel that (for the masked follow-up)
    # legitimately reads CONTACTED, sharing one before/after bracket.
    #
    # A4: also bracket the ledger specifically around the denied call — it
    # is always the one whose outcome decides this RunResult's own
    # `outcome` below (both the expected and the "unexpectedly allowed"
    # branches use denied_call, never masked_call), so its event_id, not
    # masked_call's, is the headline audit record.
    before = await db.snapshot_agent_ro_calls()
    denied_call, headline_event_id = await _qg_call_with_headline(
        client, "run_structured_queries", denied_arguments, request_id=1
    )
    r["_headline_event_id"] = headline_event_id
    mid = await db.snapshot_agent_ro_calls()
    masked_call = await _qg_call(client, "run_structured_queries", masked_arguments, request_id=2)
    after = await db.snapshot_agent_ro_calls()

    denied_results = (
        denied_call.payload.get("results", []) if isinstance(denied_call.payload, dict) else []
    )
    denied_first = denied_results[0] if denied_results else {}
    denied_error = denied_first.get("error")
    denied_touched = mid != before

    masked_results = (
        masked_call.payload.get("results", []) if isinstance(masked_call.payload, dict) else []
    )
    masked_first = masked_results[0] if masked_results else {}
    masked_rows_dicts = masked_first.get("rows") or []
    masked_columns, masked_rows = _dict_rows_to_columns_and_lists(masked_rows_dicts)
    masked_row_count = _row_count_or_len(masked_first.get("row_count"), masked_rows)

    # F2 / AMENDMENT 2: demo/SPEC.md's first sentence is authoritative —
    # pii_column IS the blocked scenario, cited to denied_columns. The
    # denied call is the headline (outcome/blocked_by/latency_ms below all
    # come from it); the masked follow-up's own rows stay in the table so
    # the two-beat story ("you may not read this at all" / "this one only
    # ever masked") still shows both real calls.
    r["mcp"] = {
        "server": "querygate",
        "endpoint": config.QUERYGATE_MCP_URL,
        "tool": "run_structured_queries",
        "request": denied_arguments,
        "response": denied_call.payload,
    }
    r["latency_ms"] = round(denied_call.elapsed_ms, 2)
    r["db"] = {"calls_before": before, "calls_after": after, "touched": after != before}
    r["policy_lines"] = lines_for(config.POLICY_YAML_PATH, "denied_columns", "column_masks")
    r["masked_columns"] = masked_columns_for(config.POLICY_YAML_PATH, "customers")

    if denied_error:
        r["outcome"] = "blocked"
        r["blocked_by"] = "policy.demo.yaml: denied_columns"
        r["columns"] = masked_columns
        r["rows"] = masked_rows[:ROWS_PREVIEW_LIMIT]
        r["row_count"] = masked_row_count
        touched_clause = (
            f"database touched ({mid - before} call(s))" if denied_touched else "database untouched"
        )
        r["note"] = (
            f"Two beats. Asking for email + phone together was rejected outright ({denied_error!r}, "
            f"{round(denied_call.elapsed_ms, 2)}ms, {touched_clause} — measured {before} -> {mid}). A "
            "follow-up request for phone + national_id alone succeeded — masked (last 4 digits; "
            "national_id one-way hashed), never in the clear."
        )
    else:
        # The denial did NOT fire — do not assert it did (F2). Surface the
        # denied call's own payload as the headline instead of quietly
        # falling through to the masked call's success, mirroring the
        # reasoning in run_pii_table's own "unexpectedly allowed" branch.
        denied_rows_dicts = denied_first.get("rows") or []
        denied_columns, denied_rows = _dict_rows_to_columns_and_lists(denied_rows_dicts)
        denied_row_count = _row_count_or_len(denied_first.get("row_count"), denied_rows)
        r["outcome"] = "allowed"
        r["blocked_by"] = None
        r["columns"] = denied_columns
        r["rows"] = denied_rows[:ROWS_PREVIEW_LIMIT]
        r["row_count"] = denied_row_count
        r["note"] = (
            f"UNEXPECTEDLY ALLOWED — denied_columns did not fire for email; {denied_row_count} rows "
            "with raw email came back. See the raw response; treat this as a policy regression, not "
            "the demo's real number."
        )
    return r


# ---------------------------------------------------------------------------
# bulk_export
# ---------------------------------------------------------------------------


async def run_bulk_export(client: httpx.AsyncClient, gate: str) -> dict:
    r = _base_run_result("bulk_export", gate)

    if gate == "off":
        sql = "select * from customers"

        async def go():
            return await _baseline_call(client, "execute_sql", {"sql": sql})

        call, before, after = await _with_db_snapshot(go)
        payload = call.payload if isinstance(call.payload, dict) else {}
        columns = payload.get("columns", [])
        rows = payload.get("rows", [])
        row_count = _row_count_or_len(payload.get("row_count"), rows)
        r["mcp"] = {
            "server": "baseline-postgres-mcp",
            "endpoint": config.BASELINE_MCP_URL,
            "tool": "execute_sql",
            "request": {"sql": sql},
            "response": _truncated_baseline_response(payload),
        }
        r["outcome"] = "allowed"
        r["latency_ms"] = round(call.elapsed_ms, 2)
        r["columns"] = columns
        r["rows"] = rows[:ROWS_PREVIEW_LIMIT]
        r["row_count"] = row_count
        r["db"] = {"calls_before": before, "calls_after": after, "touched": after != before}
        r["note"] = (
            f"All {row_count} customer rows left the database in a single unbounded SELECT * "
            "— no limit was ever asked for."
        )
        return r

    arguments = {
        "connection": config.QUERYGATE_CONNECTION_ID,
        "queries": [
            {
                "from": "customers",
                "select": [
                    "customers.id",
                    "customers.name",
                    "customers.phone",
                    "customers.national_id",
                    "customers.country",
                    "customers.is_active",
                    "customers.created_at",
                ],
                "limit": 250000,
            }
        ],
    }

    async def go():
        return await _qg_call_with_headline(client, "run_structured_queries", arguments)

    (call, headline_event_id), before, after = await _with_db_snapshot(go)
    r["_headline_event_id"] = headline_event_id
    results = call.payload.get("results", []) if isinstance(call.payload, dict) else []
    first = results[0] if results else {}
    error = first.get("error")
    rows_dicts = first.get("rows") or []
    columns, rows = _dict_rows_to_columns_and_lists(rows_dicts)
    row_count = _row_count_or_len(first.get("row_count"), rows)
    truncated = first.get("limit_applied") if "limit_applied" in first else first.get("truncated")

    r["mcp"] = {
        "server": "querygate",
        "endpoint": config.QUERYGATE_MCP_URL,
        "tool": "run_structured_queries",
        "request": arguments,
        "response": call.payload,
    }
    r["outcome"] = "blocked" if error else "allowed"
    # F11: max_limit clamps silently (compiler/sqlalchemy_compiler.py's
    # apply_limit — min(limit, max_limit)) — it is not something that can
    # raise `error` at all in the normal path this scenario exercises. An
    # error here means something else went wrong (a denied column, a table
    # gone missing), and hardcoding "max_limit" would point the config
    # panel at the wrong rule and the wrong lines. Derive the citation from
    # the real error text; leave it null rather than guess when it doesn't
    # match a known pattern.
    r["blocked_by"] = _blocked_by_from_error(error)
    r["latency_ms"] = round(call.elapsed_ms, 2)
    r["columns"] = columns
    r["rows"] = rows[:ROWS_PREVIEW_LIMIT]
    r["row_count"] = row_count
    r["db"] = {"calls_before": before, "calls_after": after, "touched": after != before}
    r["policy_lines"] = lines_for(config.POLICY_YAML_PATH, "max_limit")
    if error:
        r["note"] = f"Rejected: {error}"
    else:
        r["masked_columns"] = masked_columns_for(config.POLICY_YAML_PATH, "customers")
        r["note"] = (
            f"Asked for all 250,000 rows; got {row_count}, capped by max_limit "
            f"(truncated={truncated}). email isn't in the result at all; phone/national_id are masked. "
            "The query still runs — capped, not refused."
        )
    return r


# ---------------------------------------------------------------------------
# overload (AMENDMENT 1)
# ---------------------------------------------------------------------------


async def run_overload(client: httpx.AsyncClient, gate: str) -> dict:
    r = _base_run_result("overload", gate)

    if gate == "off":
        # F5: open the admin connection used for the kill sweep BEFORE the
        # 20-way fan-out starts, and hold it open for the whole scenario.
        # Opening it only after the fan-out (the original design) meant a
        # fresh `asyncpg.connect(timeout=5)` competing for a connection slot
        # against a database that is, by construction, saturated at that
        # exact moment — exactly the failure this scenario exists to
        # demonstrate, except against our own control plane instead of a
        # partner's application. Both snapshots reuse it too, so nothing in
        # this scenario ever opens a new connection while the database may
        # be under load.
        admin_conn = await db.get_admin_connection()
        try:
            before = await db.snapshot_agent_ro_calls(conn=admin_conn)
            wall_start = time.perf_counter()

            async def one_call(i: int) -> dict:
                t0 = time.perf_counter()
                try:
                    call = await _baseline_call(
                        client,
                        "execute_sql",
                        {"sql": config.OVERLOAD_CROSS_JOIN_SQL},
                        request_id=i,
                    )
                    return {
                        "ok": not call.is_tool_error,
                        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
                        "payload": call.payload,
                    }
                except MCPTransportError as exc:
                    return {
                        "ok": False,
                        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
                        "error": str(exc),
                    }
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    return {
                        "ok": False,
                        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
                        "error": str(exc),
                    }

            tasks = [
                asyncio.create_task(one_call(i)) for i in range(1, config.OVERLOAD_CONCURRENCY + 1)
            ]
            done, pending = await asyncio.wait(
                tasks, timeout=config.OVERLOAD_WALL_CLOCK_BOUND_SECONDS
            )
            wall_ms = round((time.perf_counter() - wall_start) * 1000, 2)

            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            # F5: wrap the sweep in try/except with one retry, reusing the
            # admin connection already held open above rather than opening a
            # new one now (that "open a new one now" is the exact bug: a
            # connect() racing 20 already-active heavy queries for a slot).
            # If both attempts still fail, do NOT abort the request — return
            # a 200 that honestly discloses the sweep failed, rather than
            # 500ing with 20 cross joins still grinding in front of the
            # audience.
            killed_pids: list[int] = []
            sweep_error: Optional[str] = None
            for attempt in range(2):
                try:
                    killed_pids = await db.kill_role_backends(conn=admin_conn)
                    sweep_error = None
                    break
                except Exception as exc:  # noqa: BLE001
                    sweep_error = str(exc)
                    logger.warning(
                        "overload kill sweep attempt %d/2 failed", attempt + 1, exc_info=True
                    )

            # The one realistic reason both sweep attempts above fail is the
            # admin connection itself going bad under load (asyncpg's
            # command_timeout cancels the in-flight statement and can leave
            # the connection aborted) — and this snapshot, on that same
            # connection, is the very next statement. Without this guard, a
            # failure here would escape the whole gate=="off" branch,
            # unwind past the "honest 200, never a 500" design the retry
            # loop above exists for, and 500 the request anyway with 20
            # cross joins potentially still running.
            try:
                after = await db.snapshot_agent_ro_calls(conn=admin_conn)
                after_measured = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("post-overload snapshot failed", exc_info=True)
                after = before
                after_measured = False
                sweep_error = sweep_error or f"post-run snapshot failed: {exc}"
        finally:
            try:
                await admin_conn.close()
            except Exception:  # noqa: BLE001
                pass

        completed_results = [t.result() for t in done if not t.cancelled()]
        sample = completed_results[0] if completed_results else None

        # F4: mirror the gate-ON path's guard (see _reject_if_call_failed's
        # docstring for the same reasoning). If every completed call failed
        # — e.g. the baseline server was down for the whole run — do not
        # report a canned "killed" success asserting "20 concurrent cross
        # joins fired at your database": none did.
        if _all_calls_failed(completed_results):
            first_error = next(
                (c.get("error") for c in completed_results if c.get("error")),
                "no successful call and no error captured",
            )
            raise MCPTransportError(
                f"all {len(completed_results)} concurrent overload calls to the baseline server "
                f"failed instead of firing real queries; first failure: {first_error!r}. Refusing to "
                "report a canned 'killed' success for a run that never touched the database."
            )

        r["mcp"] = {
            "server": "baseline-postgres-mcp",
            "endpoint": config.BASELINE_MCP_URL,
            "tool": "execute_sql",
            "request": {
                "sql": config.OVERLOAD_CROSS_JOIN_SQL,
                "fired_concurrently": config.OVERLOAD_CONCURRENCY,
            },
            "response": {
                "concurrent_requests": config.OVERLOAD_CONCURRENCY,
                "completed_before_bound": len(completed_results),
                "still_pending_at_bound": len(pending),
                "killed_backends": len(killed_pids),
                "kill_sweep_error": sweep_error,
                "wall_clock_ms": wall_ms,
                "sample_result": sample,
            },
        }
        # outcome is unconditionally "killed" per demo/SPEC.md AMENDMENT 1: the
        # 15s wall-clock bound always ends with a defensive
        # pg_terminate_backend sweep of every active agent_ro backend,
        # regardless of whether any of the 20 calls happened to finish on
        # their own first. The note below is phrased conditionally so it
        # stays accurate in that edge case rather than always claiming
        # backends were killed mid-flight.
        r["outcome"] = "killed"
        r["latency_ms"] = wall_ms
        # "touched" is asserted True when the post-sweep snapshot itself
        # failed (after_measured=False) rather than falsely reporting
        # "untouched" from an unmeasured, stale `after == before` — 20
        # cross joins genuinely ran here, so the honest default on a failed
        # measurement is "yes, touched", not silence.
        r["db"] = {
            "calls_before": before,
            "calls_after": after,
            "touched": True if not after_measured else after != before,
            "measured": after_measured,
        }
        r["row_count"] = 0
        if pending:
            progress_clause = (
                f"{len(pending)} still running at the "
                f"{int(config.OVERLOAD_WALL_CLOCK_BOUND_SECONDS)}s bound"
            )
        else:
            progress_clause = (
                f"all {config.OVERLOAD_CONCURRENCY} finished within the "
                f"{int(config.OVERLOAD_WALL_CLOCK_BOUND_SECONDS)}s bound"
            )
        if sweep_error is None:
            sweep_clause = f"the control server swept {len(killed_pids)} agent_ro backend(s) as a defensive kill"
        else:
            sweep_clause = (
                f"the defensive kill sweep FAILED after 2 attempts ({sweep_error}) — some agent_ro "
                "backends may still be running; run `make pitch-kill` now"
            )
        r["note"] = (
            f"{config.OVERLOAD_CONCURRENCY} concurrent implicit cross joins (250k x 600k x 1.2M rows) fired "
            f"at your database. {progress_clause} — {sweep_clause}. Your database took the hit first; "
            "watch the live probe above."
        )
        return r

    # Gate ON: same 20-way fan-out, but every call is a real MCP call that
    # gets rejected on allow_cross_join before it ever reaches Postgres.
    #
    # A4: `_headline_event_id` is deliberately left at its `_base_run_result`
    # default of None here. Unlike pii_column/legit, this scenario's
    # `outcome` is an AGGREGATE over all 20 concurrent calls
    # (all_rejected — see below), not decided by any single one of them, so
    # there is no one record whose event_id would honestly represent "the"
    # deciding record. The calls are also genuinely concurrent (asyncio.gather),
    # so per-call ledger-offset bracketing (the technique used elsewhere in
    # this module) wouldn't reliably isolate one call's record from another's
    # anyway. The frontend's fallback path — matching by policy_decision —
    # is safe here specifically because every one of the 20 records shares
    # the same decision.
    before = await db.snapshot_agent_ro_calls()
    arguments = {
        "connection": config.QUERYGATE_CONNECTION_ID,
        "queries": [
            {
                "from": "customers",
                "select": [{"fn": "count", "col": "*", "as": "n"}],
                "joins": [
                    {"table": "orders", "type": "cross"},
                    {"table": "order_items", "type": "cross"},
                ],
            }
        ],
    }

    async def one_call(i: int) -> dict:
        call = await _qg_call(client, "run_structured_queries", arguments, request_id=i)
        results = call.payload.get("results", []) if isinstance(call.payload, dict) else []
        error = (results[0] or {}).get("error") if results else None
        return {"elapsed_ms": round(call.elapsed_ms, 2), "error": error}

    wall_start = time.perf_counter()
    outcomes = await asyncio.gather(
        *[one_call(i) for i in range(1, config.OVERLOAD_CONCURRENCY + 1)], return_exceptions=True
    )
    wall_ms = round((time.perf_counter() - wall_start) * 1000, 2)
    after = await db.snapshot_agent_ro_calls()

    real_outcomes = [o for o in outcomes if isinstance(o, dict)]
    failed_calls = [o for o in outcomes if not isinstance(o, dict)]
    if failed_calls:
        # At least one of the 20 concurrent calls raised instead of
        # returning a result (transport failure, or _reject_if_call_failed's
        # protocol/tool-error guard). Folding this into all_rejected's
        # len(real_outcomes) == OVERLOAD_CONCURRENCY check would silently
        # compute all_rejected=False from partial data and report
        # outcome="allowed" — a fabricated success for a run that never
        # actually completed. Surface it as an honest failure instead.
        raise MCPTransportError(
            f"{len(failed_calls)}/{config.OVERLOAD_CONCURRENCY} concurrent overload calls to "
            f"QueryGate failed instead of returning a result; first failure: {failed_calls[0]!r}"
        )
    errors = [o["error"] for o in real_outcomes if o.get("error")]
    latencies = [o["elapsed_ms"] for o in real_outcomes]
    all_rejected = (
        len(errors) == len(real_outcomes) and len(real_outcomes) == config.OVERLOAD_CONCURRENCY
    )

    r["mcp"] = {
        "server": "querygate",
        "endpoint": config.QUERYGATE_MCP_URL,
        "tool": "run_structured_queries",
        "request": {**arguments, "fired_concurrently": config.OVERLOAD_CONCURRENCY},
        "response": {
            "concurrent_requests": config.OVERLOAD_CONCURRENCY,
            "all_rejected": all_rejected,
            "sample_error": errors[0] if errors else None,
            "latency_ms_median": round(statistics.median(latencies), 2) if latencies else None,
            "latency_ms_max": round(max(latencies), 2) if latencies else None,
            "wall_clock_ms": wall_ms,
        },
    }
    r["outcome"] = "blocked" if all_rejected else "allowed"
    r["blocked_by"] = "policy.demo.yaml: allow_cross_join" if all_rejected else None
    r["latency_ms"] = round(statistics.median(latencies), 2) if latencies else wall_ms
    r["db"] = {"calls_before": before, "calls_after": after, "touched": after != before}
    r["row_count"] = 0
    r["policy_lines"] = lines_for(config.POLICY_YAML_PATH, "allow_cross_join", "max_concurrency")
    r["note"] = (
        f"All {config.OVERLOAD_CONCURRENCY} concurrent cross-join requests refused on allow_cross_join "
        f"(median {r['latency_ms']}ms). Stopped twice over — the shape is rejected outright, and "
        "max_concurrency: 4 would have bounded the fan-out even if it weren't."
    )
    return r


# ---------------------------------------------------------------------------
# legit (both paths, 15 runs each, real numbers)
# ---------------------------------------------------------------------------


async def run_legit(client: httpx.AsyncClient, gate: str) -> dict:
    r = _base_run_result("legit", gate)

    baseline_sql = (
        "select customers.country, sum(orders.total_amount) as revenue "
        "from orders join customers on customers.id = orders.customer_id "
        "where orders.status = 'completed' group by customers.country "
        "order by revenue desc limit 10"
    )
    qg_arguments = {
        "connection": config.QUERYGATE_CONNECTION_ID,
        "queries": [
            {
                "from": "orders",
                "select": [
                    "customers.country",
                    {"fn": "sum", "col": "orders.total_amount", "as": "revenue"},
                ],
                "joins": [{"table": "customers", "on": ["orders.customer_id", "customers.id"]}],
                "where": {"col": "orders.status", "op": "eq", "value": "completed"},
                "group_by": ["customers.country"],
                "order_by": [{"col": "revenue", "dir": "desc"}],
                "limit": 10,
            }
        ],
    }

    before = await db.snapshot_agent_ro_calls()

    baseline_latencies: list[float] = []
    baseline_last: Optional[MCPCallResult] = None
    for i in range(config.LEGIT_RUNS_PER_SIDE):
        call = await _baseline_call(client, "execute_sql", {"sql": baseline_sql}, request_id=i)
        baseline_latencies.append(call.elapsed_ms)
        baseline_last = call

    # F3: QueryGate reports a policy rejection as a real `results[0].error`
    # string inside a normal (isError=false) tool response — the same shape
    # run_pii_table/run_bulk_export already check — rather than raising.
    # This should never fire for a deliberately legitimate query, but if
    # policy ever drifted to reject it, the old code never looked at this
    # field at all: it hardcoded outcome="allowed" and still computed an
    # overhead from qg_median - baseline_median, which for a rejected
    # (near-instant, 0-row) QueryGate call comes out NEGATIVE — a
    # fabricated success that reads as the best number in the whole demo.
    qg_latencies: list[float] = []
    qg_last: Optional[MCPCallResult] = None
    qg_error_run_count = 0
    qg_last_headline_event_id: Optional[str] = None
    for i in range(config.LEGIT_RUNS_PER_SIDE):
        is_last = i == config.LEGIT_RUNS_PER_SIDE - 1
        # A4: only the LAST of the 15 calls determines the gate-ON
        # RunResult's `outcome`/rows/response below (qg_last, used
        # throughout this function) — bracket the ledger specifically
        # around that one call to capture its real event_id, rather than
        # doing the extra read_new_records() work (and its small file-I/O
        # cost) on every one of the 15 latency-measured calls.
        if is_last:
            ledger_offset_before_last = audit.snapshot_ledger_offset()
        call = await _qg_call(client, "run_structured_queries", qg_arguments, request_id=100 + i)
        if is_last:
            last_new_records = audit.read_new_records(ledger_offset_before_last)
            qg_last_headline_event_id = (
                last_new_records[-1]["event"].get("event_id") if last_new_records else None
            )
        qg_latencies.append(call.elapsed_ms)
        qg_last = call
        call_results = call.payload.get("results", []) if isinstance(call.payload, dict) else []
        call_first = call_results[0] if call_results else {}
        if call_first.get("error"):
            qg_error_run_count += 1

    after = await db.snapshot_agent_ro_calls()

    baseline_median = round(statistics.median(baseline_latencies), 2)
    qg_median = round(statistics.median(qg_latencies), 2)

    baseline_payload = (
        baseline_last.payload if baseline_last and isinstance(baseline_last.payload, dict) else {}
    )
    qg_results = (
        qg_last.payload.get("results", []) if qg_last and isinstance(qg_last.payload, dict) else []
    )
    qg_first = qg_results[0] if qg_results else {}
    qg_rows_dicts = qg_first.get("rows") or []
    qg_columns, qg_rows = _dict_rows_to_columns_and_lists(qg_rows_dicts)
    # Derived from qg_first — the SAME call (qg_last, the final of the 15
    # runs) whose rows/columns/response are rendered above, so outcome and
    # the displayed payload can never contradict each other (review found
    # an earlier version derived the headline from whichever run failed
    # FIRST across the 15, which could disagree with a rows table/response
    # panel drawn from the LAST run if policy flapped mid-loop).
    qg_error = qg_first.get("error")
    qg_disagreement = 0 < qg_error_run_count < config.LEGIT_RUNS_PER_SIDE

    if gate == "off":
        r["mcp"] = {
            "server": "baseline-postgres-mcp",
            "endpoint": config.BASELINE_MCP_URL,
            "tool": "execute_sql",
            "request": {"sql": baseline_sql},
            "response": baseline_payload,
        }
        r["columns"] = baseline_payload.get("columns", [])
        r["rows"] = (baseline_payload.get("rows") or [])[:ROWS_PREVIEW_LIMIT]
        r["row_count"] = _row_count_or_len(baseline_payload.get("row_count"), r["rows"])
        r["latency_ms"] = baseline_median
        r["outcome"] = "allowed"
        r["blocked_by"] = None
    else:
        r["mcp"] = {
            "server": "querygate",
            "endpoint": config.QUERYGATE_MCP_URL,
            "tool": "run_structured_queries",
            "request": qg_arguments,
            "response": qg_last.payload if qg_last else {},
        }
        r["columns"] = qg_columns
        r["rows"] = qg_rows[:ROWS_PREVIEW_LIMIT]
        r["row_count"] = _row_count_or_len(qg_first.get("row_count"), qg_rows)
        r["latency_ms"] = qg_median
        r["outcome"] = "blocked" if qg_error else "allowed"
        r["blocked_by"] = _blocked_by_from_error(qg_error) if qg_error else None
        # A4: displayed mcp/rows/outcome above all come from qg_last (the
        # final of the 15 calls) — its event_id, captured while making that
        # specific call above, is the headline. gate=="off" leaves
        # `_headline_event_id` at its None default: the displayed mcp block
        # there is the baseline call, which has no audit sink at all.
        r["_headline_event_id"] = qg_last_headline_event_id

    r["db"] = {"calls_before": before, "calls_after": after, "touched": after != before}
    r["baseline_median_ms"] = baseline_median
    r["baseline_samples_ms"] = [round(x, 2) for x in baseline_latencies]
    r["querygate_samples_ms"] = [round(x, 2) for x in qg_latencies]

    disagreement_clause = (
        f" ({qg_error_run_count}/{config.LEGIT_RUNS_PER_SIDE} of the 15 QueryGate runs disagreed "
        "on error state — policy flapped mid-measurement; treat this number as unreliable)"
        if qg_disagreement
        else ""
    )
    if qg_error:
        # Never compute or display an overhead figure from a run where the
        # QueryGate side didn't complete successfully (F3) — there is
        # nothing legitimate to subtract.
        r["querygate_median_ms"] = None
        r["overhead_ms"] = None
        r["note"] = (
            f"QueryGate rejected this legitimate query: {qg_error!r}. This should never happen for "
            "the 'legit' scenario — treat it as a policy regression, not a real demo number. No "
            f"overhead figure is shown because the QueryGate side did not complete successfully."
            f"{disagreement_clause}"
        )
    else:
        overhead = round(qg_median - baseline_median, 2)
        r["querygate_median_ms"] = qg_median
        r["overhead_ms"] = overhead
        r["note"] = (
            f"Measured live, {config.LEGIT_RUNS_PER_SIDE} runs each side: baseline median {baseline_median}ms, "
            f"QueryGate median {qg_median}ms — overhead {'+' if overhead >= 0 else ''}{overhead}ms for full "
            f"validation, policy, and audit.{disagreement_clause}"
        )
    return r


RUNNERS = {
    "recon": run_recon,
    "pii_table": run_pii_table,
    "pii_column": run_pii_column,
    "bulk_export": run_bulk_export,
    "overload": run_overload,
    "legit": run_legit,
}


async def run_scenario(client: httpx.AsyncClient, scenario_id: str, gate: str) -> dict:
    if scenario_id not in RUNNERS:
        raise ValueError(f"unknown scenario_id {scenario_id!r}")
    if gate not in ("on", "off"):
        raise ValueError(f"gate must be 'on' or 'off', got {gate!r}")

    # Bracket the WHOLE scenario (which may itself issue 1, 2, or 20+ real
    # MCP calls — pii_column's two-beat follow-up, overload's 20-way
    # fan-out, legit's 15-per-side loop) with a real ledger byte-offset
    # snapshot, the same "before/after a real external system" pattern
    # _with_db_snapshot already uses for pg_stat_statements. The gate-OFF
    # baseline server has no audit sink at all, so this genuinely reads back
    # zero new records for every gate-OFF run — that contrast is the whole
    # point of the audit panel, not a bug to paper over.
    ledger_offset_before = audit.snapshot_ledger_offset()
    result = await RUNNERS[scenario_id](client, gate)
    headline_event_id = result.pop("_headline_event_id", None)
    result["audit"] = audit.audit_block_for_run(
        ledger_offset_before, headline_event_id=headline_event_id
    )
    return result
