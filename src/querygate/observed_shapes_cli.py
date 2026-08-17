"""`querygate-shapes` — inspect a deployment's recorded query shapes and draft
a curated template from one (TODO.md item 195).

**It talks to a running QueryGate over the admin API**, it does not read a
local store. The first version of this CLI read the in-process
`observed_shape_store()` directly, which was useless in practice and quietly
so: the CLI is a different OS process from the server even in a single-replica
deployment, so it always found an empty store and printed "No query shapes
recorded yet." — the one answer an operator must never be given wrongly, since
it reads as "this agent runs nothing" and invites narrowing a connection to an
empty template set. (Found by `claim-reviewer`, 2026-08-17.)

So: point it at the server with `--url` and a bearer credential holding
`admin:shapes:read`.

    querygate-shapes --url https://qg.internal --token "$QG_ADMIN_TOKEN" list
    querygate-shapes --url https://qg.internal --token "$QG_ADMIN_TOKEN" \\
        draft <hash> --template-id orders_by_status > draft.yaml

**It never installs anything.** `draft` writes YAML to stdout (or `--output`);
adding it to the templates file stays a deliberate human edit — the same
quarantined-draft posture `catalog/governance.py` takes. There is no `--apply`,
on purpose.

The window's scope depends on the deployment: with `CONCURRENCY_BACKEND=redis`
it is shared across every replica and survives a restart; otherwise it is per
serving process and volatile, so a multi-replica deployment must be queried per
replica and a restart resets it. The `scope` field says which, and this CLI
prints it in *every* branch — including "nothing recorded yet", where knowing
which guarantee you have matters most.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import httpx
import yaml

_PATH = "/api/v1/admin/observability/observed-shapes"


def _client(args: argparse.Namespace) -> httpx.Client:
    token = args.token or os.environ.get("QUERYGATE_ADMIN_TOKEN")
    if not token:
        raise SystemExit(
            "A bearer credential holding 'admin:shapes:read' is required: pass "
            "--token or set QUERYGATE_ADMIN_TOKEN."
        )
    return httpx.Client(
        base_url=args.url.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=args.timeout,
    )


def _request(args: argparse.Namespace, path: str, params: Dict[str, Any]) -> Any:
    with _client(args) as client:
        response = client.get(path, params={k: v for k, v in params.items() if v is not None})
    if response.status_code == 403:
        raise SystemExit("Forbidden — the credential lacks the 'admin:shapes:read' scope.")
    if response.status_code == 404:
        raise SystemExit(f"Not found: {response.text.strip()}")
    if response.status_code >= 400:
        raise SystemExit(f"Request failed ({response.status_code}): {response.text.strip()}")
    return response.json()


def _list(args: argparse.Namespace) -> int:
    report = _request(
        args, _PATH, {"connection_id": args.connection, "principal_id": args.principal}
    )
    if args.json:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
        return 0
    if not report.get("enabled"):
        # Distinguished from "nothing ran" deliberately: an operator who reads
        # an empty list and concludes their agent is idle, when in fact
        # recording was never enabled, would narrow a connection to an empty
        # template set and break it.
        print(
            "Observed-shape recording is DISABLED on this deployment "
            "(set OBSERVED_SHAPES_ENABLED=true and restart).",
            file=sys.stderr,
        )
        _print_bounds(report)
        return 0
    if report.get("backend_healthy") is False:
        # The third way an empty list happens, and the nastiest: fail-open means
        # an unreachable Redis renders exactly like an idle agent. Narrowing from
        # that list narrows to nothing.
        print(
            "WARNING: the shape store's backend is UNREACHABLE — this list is "
            "empty because it could not be read, NOT because nothing ran. Do "
            "not narrow a connection from it.",
            file=sys.stderr,
        )
        _print_bounds(report)
        return 0
    shapes = report.get("shapes", [])
    if not shapes:
        print("Recording is enabled, but no query shapes have been seen yet.", file=sys.stderr)
        _print_bounds(report)
        return 0
    print(f"{'HASH':<34}{'OCCURS':>8}  {'CONNECTION':<20}{'PRINCIPAL':<24}TABLE")
    for shape in shapes:
        skeleton = shape.get("skeleton", {})
        table = skeleton.get("from_table") or skeleton.get("from") or "?"
        print(
            f"{shape['shape_hash']:<34}{shape['occurrences']:>8}  "
            f"{shape['connection_id']:<20}{(shape.get('principal_id') or '-'):<24}{table}"
        )
    _print_bounds(report)
    return 0


def _print_bounds(report: Dict[str, Any]) -> None:
    """Every bound the operator needs in order to read the list honestly.

    Printed in EVERY branch, not only when shapes were listed: on an empty
    window the scope line is what tells an operator whether they are looking at
    the whole fleet or one replica, which is exactly when they are most likely to
    misread the emptiness.
    """
    scope = report.get("scope")
    scope_note = (
        "shared across replicas, survives a restart"
        if scope == "shared-durable"
        else "this process only, discarded on restart — collect per replica"
    )
    print(
        f"\nscope={scope} ({scope_note}) · max_entries={report.get('max_entries')} · "
        f"evicted={report.get('evicted_total')} · "
        f"skeletonization_failures={report.get('skeletonization_failures')}",
        file=sys.stderr,
    )
    if report.get("evicted_total"):
        print(
            "WARNING: shapes were evicted — this list is incomplete. Raise "
            "OBSERVED_SHAPES_MAX_ENTRIES and re-run the discovery window. Note "
            "the bound shown is the one the last writing replica used, which in "
            "a fleet that disagrees is the smallest configured value.",
            file=sys.stderr,
        )
    if report.get("skeletonization_failures"):
        print(
            "WARNING: some queries could not be reduced to a template skeleton "
            "and were NOT recorded. Narrowing on this list alone may break them.",
            file=sys.stderr,
        )


def _draft(args: argparse.Namespace) -> int:
    template = _request(
        args,
        f"{_PATH}/{args.shape_hash}/template-draft",
        {
            "template_id": args.template_id,
            "description": args.description,
            "connection_id": args.connection,
            "principal_id": args.principal,
        },
    )
    document = yaml.safe_dump(
        {"templates": [{k: v for k, v in template.items() if v is not None}]},
        sort_keys=False,
        allow_unicode=True,
    )
    if args.output is not None:
        args.output.write_text(document, encoding="utf-8")
        print(f"wrote template draft -> {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(document)
    print(
        "Review this draft before adding it to your templates file; it is NOT "
        "installed. Every caller-supplied value became a required parameter — "
        "hard-code the ones that should not vary, and tighten the slots "
        "(allowed_values, min/max) before publishing.",
        file=sys.stderr,
    )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="querygate-shapes",
        description="Inspect a running QueryGate's recorded query shapes and "
        "draft a curated query template from one (TODO.md item 195). Reads the "
        "admin API; never installs a template.",
    )
    parser.add_argument("--url", default=os.environ.get("QUERYGATE_URL", "http://localhost:8000"))
    parser.add_argument(
        "--token",
        default=None,
        help="Bearer credential with admin:shapes:read (or QUERYGATE_ADMIN_TOKEN).",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List recorded query shapes.")
    list_parser.add_argument("--connection", default=None, help="Filter by connection id.")
    list_parser.add_argument("--principal", default=None, help="Filter by principal subject.")
    list_parser.add_argument("--json", action="store_true", help="Emit raw JSON.")
    list_parser.set_defaults(func=_list)

    draft_parser = subparsers.add_parser(
        "draft", help="Render one recorded shape as a reviewable template."
    )
    draft_parser.add_argument("shape_hash", help="Shape hash from `querygate-shapes list`.")
    draft_parser.add_argument(
        "--template-id", required=True, help="Identifier for the drafted template."
    )
    draft_parser.add_argument("--description", default=None, help="Template description.")
    # A hash is not unique on its own — the same shape run by two principals is
    # two records — so these disambiguate which one to draft from.
    draft_parser.add_argument("--connection", default=None, help="Disambiguate by connection id.")
    draft_parser.add_argument("--principal", default=None, help="Disambiguate by principal.")
    draft_parser.add_argument(
        "-o", "--output", type=Path, default=None, help="Write YAML here instead of stdout."
    )
    draft_parser.set_defaults(func=_draft)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
