"""Auto-discovery of MCP tool modules."""

from __future__ import annotations

import importlib
from pathlib import Path


def discover_and_register_tools() -> int:
    """Import all tool modules in mcp/tools/ and return the count imported."""
    tools_dir = Path(__file__).resolve().parent
    imported = 0
    for path in sorted(tools_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        importlib.import_module(f"querygate.mcp.tools.{path.stem}")
        imported += 1
    return imported
