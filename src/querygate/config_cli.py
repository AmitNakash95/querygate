"""`querygate-config` — operate the config-governance four-eyes flow from a shell.

An authenticated thin HTTP client over the existing `/admin/config/*` REST API
(TODO.md item 42 phase 2). It adds no new authority: every action carries the
caller's own bearer token, and the server enforces the same scopes, the
author≠approver rule, and the audit trail — so a reviewer running
`querygate-config approve` here is exactly the reviewer the server sees. This
exists purely so operators and CI/CD can review staged config changes without
hand-writing curl.

    export QUERYGATE_URL=https://gateway.internal
    export QUERYGATE_TOKEN=<reviewer bearer token>
    querygate-config versions              # list versions + approval status
    querygate-config approve 7 --note ok   # approve staged version 7
    querygate-config reject 7 --note "..."  # reject it
"""

from __future__ import annotations

import argparse
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
    """Render a REST error the way this API returns it, without leaking anything
    the server chose not to (the body is already redaction-safe)."""
    try:
        detail = resp.json().get("detail", resp.text)
    except ValueError:
        detail = resp.text
    print(f"error {resp.status_code}: {detail}", file=sys.stderr)
    return 1


def _approval_summary(version: dict) -> str:
    approvals = version.get("approvals", []) or []
    approves = sorted({a["approver"] for a in approvals if a.get("decision") == "approve"})
    rejects = sorted({a["approver"] for a in approvals if a.get("decision") == "reject"})
    parts = []
    if approves:
        parts.append(f"approved by {', '.join(approves)}")
    if rejects:
        parts.append(f"rejected by {', '.join(rejects)}")
    return "; ".join(parts) if parts else "no reviews"


def _cmd_versions(args: argparse.Namespace) -> int:
    with _client(args) as client:
        resp = client.get("/api/v1/admin/config/versions")
    if resp.status_code != 200:
        return _print_error(resp)
    versions = resp.json()
    if not versions:
        print("no config versions")
        return 0
    for v in versions:
        line = f"{v['id']:>4}  {v['status']:<8}  by {v.get('created_by', '?')}"
        if v.get("status") == "staged":
            line += f"  [{_approval_summary(v)}]"
        if v.get("description"):
            line += f"  — {v['description']}"
        print(line)
    return 0


def _review(args: argparse.Namespace, decision: str) -> int:
    body = {"note": args.note} if args.note else {}
    with _client(args) as client:
        resp = client.post(f"/api/v1/admin/config/versions/{args.version_id}/{decision}", json=body)
    if resp.status_code != 200:
        return _print_error(resp)
    version = resp.json()
    print(f"{decision}d version {version['id']} — {_approval_summary(version)}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="querygate-config",
        description="Operate the config-governance four-eyes review flow (item 42).",
    )
    parser.add_argument("--url", default=None, help="QueryGate base URL (or QUERYGATE_URL).")
    parser.add_argument("--token", default=None, help="Bearer token (or QUERYGATE_TOKEN).")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout seconds.")
    sub = parser.add_subparsers(dest="command", required=True)

    versions = sub.add_parser("versions", help="List config versions and their approval status.")
    versions.set_defaults(func=_cmd_versions)

    approve = sub.add_parser("approve", help="Approve a staged config version (four-eyes).")
    approve.add_argument("version_id")
    approve.add_argument("--note", default=None, help="Optional bounded review note.")
    approve.set_defaults(func=lambda a: _review(a, "approve"))

    reject = sub.add_parser("reject", help="Reject a staged config version.")
    reject.add_argument("version_id")
    reject.add_argument("--note", default=None, help="Optional bounded review note.")
    reject.set_defaults(func=lambda a: _review(a, "reject"))

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
