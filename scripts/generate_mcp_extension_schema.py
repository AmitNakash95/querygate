#!/usr/bin/env python3
"""Regenerate `docs/mcp_extensions/structured_query_ast.schema.json` (TODO.md
item 131) from the live Pydantic AST models — never hand-edit that file.

`querygate.mcp.extensions.generate_structured_query_ast_schema()` is the
single source of truth; this script just serializes its output to disk. Run
after any change to `query_ast/models.py` or `write_ast/models.py`:

    poetry run python scripts/generate_mcp_extension_schema.py   # or: make mcp-extension-schema

`tests/unit/test_mcp_extensions.py::test_generated_schema_matches_published_file`
fails CI if the committed file drifts from a fresh generation, the same
drift-guard discipline `scripts/generate_trust_page.py` and
`scripts/generate_sbom.py` use for their own generated artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path

from querygate.mcp.extensions import (
    STRUCTURED_QUERY_AST_SCHEMA_PATH,
    generate_structured_query_ast_schema,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / STRUCTURED_QUERY_AST_SCHEMA_PATH


def main() -> int:
    schema = generate_structured_query_ast_schema()
    OUTPUT.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
