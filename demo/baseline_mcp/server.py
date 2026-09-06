"""DELIBERATELY UNSAFE DEMO ARTIFACT — not part of the QueryGate product.
NEVER deploy this, NEVER import it from product code, NEVER point it at
anything but the throwaway `querygate_demo_pitch` database on 127.0.0.1:5544.
It is the "generic Postgres MCP server" — an `execute_sql` tool with no
policy, no limits, no auth, no redaction — that QueryGate exists to replace.

Built for the partner demo (demo/SPEC.md, Act 1: "gate OFF"). It is the
control condition in an A/B comparison against QueryGate's own MCP server
(`src/querygate/mcp/server.py`). It must be real and genuinely functional —
the demo's credibility depends on it not being a strawman — while remaining
impossible to mistake for part of the shipped product.

Every corner this file deliberately cuts is called out inline with a comment
of the form "DELIBERATELY NAIVE: ..." naming what QueryGate does instead. The
one thing this server does NOT cut a corner on is honesty: it returns real
database errors to the caller rather than swallowing or reshaping them. No
`except` clause in this file catches, reshapes, or downgrades a database
exception; the `mcp` SDK wraps whatever a tool raises as
`ToolError(f"Error executing tool {name}: {e}")` (`mcp/server/mcpserver/tools/
base.py`) and `MCPServer`'s call-tool handler turns that into
`CallToolResult(is_error=True, content=[TextContent(text=str(e))])`
(`mcp/server/mcpserver/server.py`) — the original database message is
preserved verbatim inside it, just prefixed, never masked or truncated.

Uses the same `mcp` SDK v2 `MCPServer` idiom as
`src/querygate/mcp/server.py`, but stripped to the bare minimum: no auth
middleware, no scope filtering, no transport guard, no extensions. Streamable
HTTP only, bound to 127.0.0.1:8811, mounted at /mcp.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, List, Mapping, Optional

import asyncpg
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

# demo/ is deliberately NOT a Python package (no top-level __init__.py), so
# the shared DSN-validation module (demo/_dsn_guard.py) can't be reached via
# a normal package-relative import from this file, which lives one
# directory below it (demo/baseline_mcp/). Insert demo/ itself onto
# sys.path rather than adding an __init__.py anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _dsn_guard import validate_dsn  # noqa: E402

# ---------------------------------------------------------------------------
# Startup guard (demo/SPEC.md, "Hard safety constraints").
#
# The SPEC-named checks below, plus the DSN-shape checks in the shared
# demo/_dsn_guard.py module (host, port, database name, and role — see that
# module's docstring for why each exists and, for the host check
# specifically, the S1 bypass it fixes), must all pass before this process
# will bind a socket or touch a database at all. This is intentionally a
# pure function of an env mapping (not `os.environ` directly) so it is
# trivially unit-testable — see test_guard.py, which exercises every branch
# (each failure mode, plus the success case) without starting a server.
# ---------------------------------------------------------------------------

UNSAFE_BASELINE_ENV_VAR = "QUERYGATE_DEMO_UNSAFE_BASELINE"
UNSAFE_BASELINE_REQUIRED_VALUE = "i-understand"

DSN_ENV_VAR = "PITCH_AGENT_RO_DSN"
# Default matches demo/db/02_roles.sql's agent_ro role exactly (throwaway
# password, committed in plaintext there — see that file's own comment on
# why that is safe for this local-only demo database). Overridable via
# PITCH_AGENT_RO_DSN so a different local port/password can be used without
# editing this file.
DEFAULT_DSN = "postgresql://agent_ro:agent_ro_demo_pw_only@127.0.0.1:5544/querygate_demo_pitch"

# This server must connect as agent_ro specifically — see
# demo/_dsn_guard.py's module docstring ("S3") for why the role is checked
# at all, not just host/port/database name.
REQUIRED_DB_USER = "agent_ro"

HOST = "127.0.0.1"
PORT = 8811
MOUNT_PATH = "/mcp"


def _normalize_dsn_scheme(dsn: str) -> str:
    """Strip a SQLAlchemy-style '+driver' suffix from the DSN scheme.

    PITCH_AGENT_RO_DSN is shared with QueryGate's own demo config
    (demo/config/env.demo), which sets it to the SQLAlchemy form
    'postgresql+asyncpg://...' for its SQLAlchemy-based connection loader.
    This server talks to asyncpg directly, which only understands the plain
    libpq scheme 'postgresql://'. Both are legitimate values of the same
    shared env var; normalize here so either form works, rather than making
    the two components disagree on what the shared var means.
    """
    scheme, sep, rest = dsn.partition("://")
    if sep and "+" in scheme:
        return f"{scheme.split('+', 1)[0]}{sep}{rest}"
    return dsn


def resolve_dsn(env: Mapping[str, str]) -> str:
    """Return the Postgres DSN to connect as, from env or the local default."""
    return _normalize_dsn_scheme(env.get(DSN_ENV_VAR, DEFAULT_DSN))


def check_startup_guard(env: Mapping[str, str]) -> Optional[str]:
    """Return None if it is safe to start, else a specific, human-readable
    reason naming exactly which check failed.

    Two things must hold:
      1. QUERYGATE_DEMO_UNSAFE_BASELINE=i-understand is set (an explicit,
         hard-to-fat-finger opt-in — not a default). This is the one check
         specific to this server (demo/SPEC.md hard safety constraint).
      2. The DSN itself passes every check in the shared
         demo/_dsn_guard.py module: Postgres scheme, exactly one host,
         that host is localhost/127.0.0.1, the port is exactly 5544, the
         database name is exactly "querygate_demo_pitch", and the DSN's
         user is exactly "agent_ro". See that module's docstring for why
         each of these exists — in particular "S1" for the host-parsing
         bypass this replaced an inline, disagreeing-with-asyncpg check
         with, and "S3" for why port and role are checked at all.
    """
    if env.get(UNSAFE_BASELINE_ENV_VAR) != UNSAFE_BASELINE_REQUIRED_VALUE:
        return (
            f"refusing to start: env {UNSAFE_BASELINE_ENV_VAR} is not set to "
            f"'{UNSAFE_BASELINE_REQUIRED_VALUE}' (demo/SPEC.md hard safety "
            "constraint: this server must not start without an explicit, "
            "unambiguous opt-in)."
        )

    dsn = resolve_dsn(env)
    return validate_dsn(dsn, required_user=REQUIRED_DB_USER, dsn_env_var=DSN_ENV_VAR)


def _dsn() -> str:
    """Return the DSN to connect with, re-running the startup guard first.

    Tools read this at call time rather than trusting a value captured once
    at process start, so the guard's verdict can never go stale relative to
    what a tool actually connects to — including on any future path that
    imports this module and mounts/calls a tool without going through
    main() (main()'s own guard call + SystemExit remains the primary,
    fail-fast enforcement point for normal process startup).
    """
    reason = check_startup_guard(os.environ)
    if reason is not None:
        raise RuntimeError(reason)
    return resolve_dsn(os.environ)


# ---------------------------------------------------------------------------
# JSON-safety for raw row values. asyncpg returns native Python types
# (Decimal, datetime, date, bytes, ...) that are not JSON-serializable as-is;
# this is plain plumbing, not a guardrail — it does not inspect, mask, or
# drop anything, it only makes the same value transmissible.
# ---------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return value


# ---------------------------------------------------------------------------
# MCP server + tools.
#
# DELIBERATELY NAIVE: no auth at all. No token verifier, no auth server
# provider, no middleware of any kind — anyone who can reach 127.0.0.1:8811
# can call every tool. QueryGate's MCP server (src/querygate/mcp/server.py)
# always sits behind `MCPAuthMiddleware` (static API keys and/or
# JWKS-verified JWTs) before a request ever reaches a tool.
# ---------------------------------------------------------------------------

mcp_server: MCPServer = MCPServer(
    name="baseline-postgres-mcp",
    instructions=(
        "Deliberately naive demo Postgres MCP server. execute_sql runs "
        "arbitrary SQL against the querygate_demo_pitch database with no "
        "policy, no row cap, no statement timeout, no auth, and no "
        "redaction. This is the counter-example QueryGate exists to replace."
    ),
)


class SqlResult(BaseModel):
    columns: List[str]
    rows: List[List[Any]]
    row_count: int


class TableListResult(BaseModel):
    tables: List[str]


class ColumnInfo(BaseModel):
    name: str
    data_type: str
    is_nullable: bool


class TableDescription(BaseModel):
    table: str
    columns: List[ColumnInfo]


@mcp_server.tool(
    description=(
        "Run arbitrary SQL against the demo Postgres database as the "
        "genuinely read-only 'agent_ro' role and return columns + rows. No "
        "table/column restrictions, no row limit, no statement timeout. "
        "This is the whole point of this server."
    )
)
async def execute_sql(sql: str) -> SqlResult:
    # DELIBERATELY NAIVE: no allow/deny list of any kind — every table and
    # column agent_ro can SELECT (i.e. every table in the schema) is fair
    # game, including `employees` (ssn, salary). QueryGate's
    # validation/policy_validation.py checks every table/column reference
    # against the connection's Policy *before* any database is touched.
    #
    # DELIBERATELY NAIVE: no statement timeout is set on this connection.
    # QueryGate's connections/dialects.py sets `SET LOCAL statement_timeout`
    # (and lock_timeout) as a session guardrail on every connection it opens.
    #
    # DELIBERATELY NAIVE: connect-per-call, no pool, no concurrency guard.
    # QueryGate's execution/concurrency.py bounds concurrent executions
    # through a semaphore/distributed limiter before a query ever runs.
    conn = await asyncpg.connect(dsn=_dsn())
    try:
        records = await conn.fetch(sql)
    finally:
        # conn.terminate() (not conn.close()): terminate() is a fire-and-
        # forget socket close that never raises, so a real error from
        # fetch() above is never replaced by a secondary error from a
        # graceful close attempt on an already-broken connection (e.g. one
        # whose backend the control UI just pg_terminate_backend'd for the
        # `overload` scenario). Keeps the "never swallow a real error"
        # guarantee true under that failure mode too.
        conn.terminate()

    columns = list(records[0].keys()) if records else []
    # DELIBERATELY NAIVE: no row cap. QueryGate's Policy.max_limit caps rows
    # returned by construction (the compiled query itself is bounded), not
    # by truncating a result after the fact. Here, all matching rows come
    # back — for `bulk_export` that is the entire ~250k-row customers table.
    rows = [[_json_safe(v) for v in record.values()] for record in records]
    # DELIBERATELY NAIVE: no redaction. Every column value — including
    # employees.ssn and employees.salary — is returned as-is. QueryGate's
    # audit/logger.py never even logs SQL, predicate values, or rows, let
    # alone redacts them in a response; the response itself is bounded by
    # policy so PII columns are never reachable in the first place.
    return SqlResult(columns=columns, rows=rows, row_count=len(rows))


@mcp_server.tool(description="List every table in the public schema.")
async def list_tables() -> TableListResult:
    # DELIBERATELY NAIVE: returns every table with no policy filtering.
    # QueryGate's list_tables (src/querygate/mcp/tools/schema.py) filters to
    # what the connection's Policy allows before this ever leaves the
    # server — `employees` simply is not part of the agent's reality there.
    conn = await asyncpg.connect(dsn=_dsn())
    try:
        records = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        )
    finally:
        conn.terminate()  # never raises — see execute_sql's comment above
    return TableListResult(tables=[r["table_name"] for r in records])


@mcp_server.tool(description="Describe every column of a table (name, type, nullability).")
async def describe_table(table: str) -> TableDescription:
    # DELIBERATELY NAIVE: describes any table agent_ro can see, employees
    # included, with every column and no denied-column filtering.
    conn = await asyncpg.connect(dsn=_dsn())
    try:
        records = await conn.fetch(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = $1 "
            "ORDER BY ordinal_position",
            table,
        )
    finally:
        conn.terminate()  # never raises — see execute_sql's comment above
    columns = [
        ColumnInfo(
            name=r["column_name"],
            data_type=r["data_type"],
            is_nullable=(r["is_nullable"] == "YES"),
        )
        for r in records
    ]
    return TableDescription(table=table, columns=columns)


def main() -> None:
    reason = check_startup_guard(os.environ)
    if reason is not None:
        print(f"[baseline-postgres-mcp] {reason}", file=sys.stderr)
        raise SystemExit(1)

    print(
        f"[baseline-postgres-mcp] starting on http://{HOST}:{PORT}{MOUNT_PATH} "
        f"(dsn host/db validated, connecting as agent_ro) ...",
        file=sys.stderr,
    )
    # DELIBERATELY NAIVE: stateless_http=True, no TransportSecuritySettings
    # (no DNS-rebinding protection, no allowed_hosts/allowed_origins check).
    # QueryGate's own setup_mcp() (src/querygate/mcp/server.py) always
    # constructs a TransportSecuritySettings and wraps the ASGI app in
    # MCPAuthMiddleware + MCPRequestGuardMiddleware before mounting it.
    mcp_server.run(
        transport="streamable-http",
        host=HOST,
        port=PORT,
        streamable_http_path=MOUNT_PATH,
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
