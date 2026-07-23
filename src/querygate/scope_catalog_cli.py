"""`querygate-scope-catalog` — emit the operator scope-catalog reference.

Prints the generated Markdown to stdout by default, or writes it to a path with
`--output`. `make scope-catalog` uses `--output docs/SCOPE_CATALOG.md` to
refresh the committed copy; a drift test keeps that copy honest.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from querygate.scope_catalog import render_scope_catalog_markdown


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="querygate-scope-catalog",
        description="Generate the QueryGate authorization scope-catalog reference "
        "(scopes + recommended IdP role bundles) from core/scopes.py.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Write the Markdown to this file instead of stdout.",
    )
    args = parser.parse_args(argv)

    markdown = render_scope_catalog_markdown()
    if args.output is not None:
        args.output.write_text(markdown, encoding="utf-8")
        print(f"wrote scope catalog -> {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
