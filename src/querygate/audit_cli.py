"""`querygate-audit` — verify a tamper-evident audit ledger and extract receipts.

TODO.md item 91 (F5). Verify-only tooling over the hash-chained JSONL ledger
written by `audit_sink_backend=jsonl_chained`. Two subcommands:

- ``verify`` — walk the whole ledger and confirm every record links to its
  predecessor and hashes to its contents; detects edits, deletions, reordering,
  and insertion. With ``--expected-head`` it also detects records dropped from
  the end. Exit code is 0 iff the chain is intact, so it can gate a release or
  a periodic integrity job.
- ``receipt`` — emit the portable, self-contained compliance receipt for a
  single event id (the artifact an auditor verifies for one query).

The HMAC key (for a keyed ledger) is read from ``--hmac-key-env`` (an env var
name) so the secret never lands in shell history or a process-listing argument.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

from querygate.audit.ledger import (
    extract_receipt_for_event_id,
    verify_chain,
)


def _resolve_key(hmac_key_env: Optional[str]) -> Optional[bytes]:
    if not hmac_key_env:
        return None
    value = os.environ.get(hmac_key_env, "")
    if not value.strip():
        raise SystemExit(
            f"--hmac-key-env {hmac_key_env!r} is set but that environment variable "
            "is empty; export the same key the ledger was written with."
        )
    return value.encode("utf-8")


def _iter_lines(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            yield line


def _cmd_verify(args: argparse.Namespace) -> int:
    path = Path(args.ledger)
    if not path.exists():
        print(f"Ledger file not found: {path}", file=sys.stderr)
        return 2
    key = _resolve_key(args.hmac_key_env)
    result = verify_chain(_iter_lines(path), key=key, expected_head=args.expected_head)
    if result.ok:
        print(
            f"Ledger OK: {result.records_checked} record(s) verified, " f"head={result.head_hash}"
        )
        return 0
    location = ""
    if result.broken_at_seq is not None:
        location = f" at seq {result.broken_at_seq}"
    if result.broken_at_line is not None:
        location += f" (line {result.broken_at_line})"
    print(
        f"Ledger BROKEN{location}: {result.reason} "
        f"[{result.records_checked} record(s) verified before the break]",
        file=sys.stderr,
    )
    return 1


def _cmd_receipt(args: argparse.Namespace) -> int:
    path = Path(args.ledger)
    if not path.exists():
        print(f"Ledger file not found: {path}", file=sys.stderr)
        return 2
    keyed = bool(args.hmac_key_env)
    # Validate the key exists even though the receipt itself embeds the hash;
    # this keeps behavior consistent with `verify` and catches a typo'd env name.
    _resolve_key(args.hmac_key_env)
    receipt = extract_receipt_for_event_id(_iter_lines(path), args.event_id, keyed=keyed)
    if receipt is None:
        print(
            f"No ledger record found for event id {args.event_id!r} in {path}",
            file=sys.stderr,
        )
        return 1
    print(receipt.model_dump_json(indent=2))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="querygate-audit",
        description="Verify QueryGate's tamper-evident audit ledger and extract receipts.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    verify = sub.add_parser("verify", help="Verify a hash-chained audit ledger's integrity.")
    verify.add_argument("ledger", help="Path to the JSONL ledger file.")
    verify.add_argument(
        "--hmac-key-env",
        default=None,
        help="Name of an env var holding the ledger HMAC key (for a keyed ledger).",
    )
    verify.add_argument(
        "--expected-head",
        default=None,
        help="Last-known-good head hash; also detects records dropped from the end.",
    )
    verify.set_defaults(func=_cmd_verify)

    receipt = sub.add_parser(
        "receipt", help="Emit the portable compliance receipt for one event id."
    )
    receipt.add_argument("ledger", help="Path to the JSONL ledger file.")
    receipt.add_argument("event_id", help="The audit event_id to build a receipt for.")
    receipt.add_argument(
        "--hmac-key-env",
        default=None,
        help="Name of an env var holding the ledger HMAC key (for a keyed ledger).",
    )
    receipt.set_defaults(func=_cmd_receipt)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
