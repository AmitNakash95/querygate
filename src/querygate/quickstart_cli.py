"""`querygate-quickstart` — a "5-minute first governed query" walkthrough
(TODO.md item 146).

Pure composition over already-shipped **read-only** discovery surfaces —
`GET /connections`, `GET /{connection}/tables`, `GET /{connection}/tables/
{table}` (which already carries the catalog's per-column sensitivity label,
TODO.md item 32) — plus the existing `POST /{connection}/query` execute
endpoint. It proposes example queries; it never writes anything, and it
never persists a generated example as a `templates.yaml` entry (that stays
item 48's governed authoring path with its own review gate).

An authenticated thin HTTP client, mirroring `querygate-config`'s shape: it
adds no new authority, since every discovery call runs under the caller's
own bearer token and the server's existing policy/catalog checks decide what
that caller can see.

    export QUERYGATE_URL=https://gateway.internal
    export QUERYGATE_TOKEN=<caller bearer token>
    querygate-quickstart demo   # propose example queries against connection "demo"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Sequence

import httpx


def _client(args: argparse.Namespace) -> httpx.Client:
    base = args.url or os.environ.get("QUERYGATE_URL")
    token = args.token or os.environ.get("QUERYGATE_TOKEN")
    if not base:
        raise SystemExit("Set --url or QUERYGATE_URL to the QueryGate base URL.")
    if not token:
        raise SystemExit("Set --token or QUERYGATE_TOKEN to a bearer token.")
    return httpx.Client(
        base_url=base.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=args.timeout,
    )


def _print_error(resp: httpx.Response) -> int:
    try:
        detail = resp.json().get("detail", resp.text)
    except ValueError:
        detail = resp.text
    print(f"error {resp.status_code}: {detail}", file=sys.stderr)
    return 1


def _is_sensitive(column: dict) -> bool:
    """Whether the catalog LABELS this column sensitive — not a security
    boundary of its own, just deference to whatever the catalog already
    says (item 32). A connection with no catalog labels at all has every
    column pass this check, the same as it would for any other catalog-
    driven feature (found by `security-invariant-reviewer`, 2026-08-05:
    "non-sensitive" would overstate the guarantee on such a deployment)."""
    catalog = column.get("catalog")
    return bool(catalog) and catalog.get("sensitivity", "none") != "none"


def _safe_columns(table: dict, *, limit: int = 3) -> list[str]:
    return [c["name"] for c in table.get("columns", []) if not _is_sensitive(c)][:limit]


def _find_quickstart_table(
    client: httpx.Client, connection: str
) -> tuple[Optional[str], list[str]]:
    """The first table with at least two columns not labeled sensitive in the
    catalog — enough to build a plain select, a filtered select, and a
    group-by aggregate. Returns (table_name, safe_columns) or (None, []) if
    nothing qualifies."""
    resp = client.get(f"/api/v1/{connection}/tables")
    if resp.status_code != 200:
        _print_error(resp)
        return None, []
    for table_name in resp.json().get("tables", []):
        desc = client.get(f"/api/v1/{connection}/tables/{table_name}")
        if desc.status_code != 200:
            continue
        columns = _safe_columns(desc.json())
        if len(columns) >= 2:
            return table_name, columns
    return None, []


def _example_queries(table: str, columns: list[str]) -> list[tuple[str, str, dict]]:
    """Three (title, kind, StructuredQuery body) tuples scoped to already-
    confirmed non-sensitive columns: a plain select, a filtered select, and a
    group-by aggregate — the three shapes item 146 asks for. `kind` selects
    the matching Python-SDK snippet shape in `_python_sdk_snippet`."""
    select_cols = [f"{table}.{c}" for c in columns]
    first = f"{table}.{columns[0]}"
    return [
        (
            "A plain select",
            "plain",
            {"from": table, "select": select_cols, "limit": 10},
        ),
        (
            "A filtered select",
            "filtered",
            {
                "from": table,
                "select": select_cols,
                "where": {"col": first, "op": "is_not_null"},
                "limit": 10,
            },
        ),
        (
            "An aggregate (count per group)",
            "aggregate",
            {
                "from": table,
                "select": [first, {"fn": "count", "col": "*", "as": "row_count"}],
                "group_by": [first],
                "limit": 10,
            },
        ),
    ]


def _curl_snippet(connection: str, query: dict) -> str:
    body = json.dumps(query)
    return (
        f'curl -s -X POST "$QUERYGATE_URL/api/v1/{connection}/query" \\\n'
        '  -H "Authorization: Bearer $QUERYGATE_TOKEN" \\\n'
        '  -H "Content-Type: application/json" \\\n'
        f"  -d '{body}'"
    )


def _mcp_snippet(connection: str, query: dict) -> str:
    call = {
        "tool": "run_structured_queries",
        "arguments": {"connection": connection, "queries": [query], "mode": "execute"},
    }
    return json.dumps(call, indent=2)


def _python_sdk_snippet(connection: str, table: str, kind: str, columns: list[str]) -> str:
    select_args = ", ".join(f'"{table}.{c}"' for c in columns)
    if kind == "plain":
        chain = f'Query.from_("{table}").select({select_args}).limit(10)'
    elif kind == "filtered":
        chain = (
            f'Query.from_("{table}").select({select_args})\n'
            f'    .where(col("{table}.{columns[0]}").is_not_null())\n'
            "    .limit(10)"
        )
    else:
        chain = (
            f'Query.from_("{table}")\n'
            f'    .select("{table}.{columns[0]}", agg.count("*", as_="row_count"))\n'
            f'    .group_by("{table}.{columns[0]}")\n'
            "    .limit(10)"
        )
    return (
        "from querygate.client import Query, col, agg\n"
        f"query = {chain}\n"
        f'result = client.post("/api/v1/{connection}/query", json=query.to_dict())'
    )


def _print_query_block(
    index: int, title: str, connection: str, table: str, columns: list[str], kind: str, query: dict
) -> None:
    print(f"\n{index}. {title}")
    print("-" * (len(title) + len(str(index)) + 2))
    print("\nStructuredQuery body:")
    print(json.dumps(query, indent=2))
    print("\nREST (curl):")
    print(_curl_snippet(connection, query))
    print("\nMCP tool call (run_structured_queries):")
    print(_mcp_snippet(connection, query))
    print("\nPython SDK:")
    print(_python_sdk_snippet(connection, table, kind, columns))


def _cmd_quickstart(args: argparse.Namespace) -> int:
    with _client(args) as client:
        table, columns = _find_quickstart_table(client, args.connection)
        if table is None:
            print(
                f"No table on connection {args.connection!r} has at least two "
                "columns visible to you that aren't labeled sensitive in the "
                "catalog — nothing to propose. Try `GET /api/v1/{connection}/tables` "
                "yourself, or ask an operator which tables you should have access to.",
                file=sys.stderr,
            )
            return 1

    print(f"Connection {args.connection!r}: found table {table!r} with columns {columns}.")
    print("Three read-only example queries you can run right now:")
    for i, (title, kind, query) in enumerate(_example_queries(table, columns), start=1):
        _print_query_block(i, title, args.connection, table, columns, kind, query)
    print(
        "\nThese are ad hoc examples, not saved anywhere — an admin can turn one "
        "into a reusable, reviewed template via the query-templates workflow "
        "(item 48) if it's worth keeping."
    )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="querygate-quickstart",
        description=(
            "Propose 3 ready-to-run example queries for a connection, in under "
            "5 minutes (item 146) — REST curl, MCP tool-call, and Python SDK "
            "snippets for each, scoped to columns not labeled sensitive in the "
            "catalog (a connection with no catalog labels at all has none "
            "excluded this way — see the catalog docs to label sensitive columns)."
        ),
    )
    parser.add_argument("connection", help="Connection id to build examples against.")
    parser.add_argument("--url", default=None, help="QueryGate base URL (or QUERYGATE_URL).")
    parser.add_argument("--token", default=None, help="Bearer token (or QUERYGATE_TOKEN).")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout seconds.")
    parser.set_defaults(func=_cmd_quickstart)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
