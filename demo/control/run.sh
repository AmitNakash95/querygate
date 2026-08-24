#!/usr/bin/env bash
# Starts the QueryGate partner-demo control UI + backend on 127.0.0.1:8900
# (demo/SPEC.md). Requires the demo Postgres (127.0.0.1:5544), the baseline
# MCP server (127.0.0.1:8811), and QueryGate itself (127.0.0.1:8010) to
# already be running — see demo/db/README.md, demo/baseline_mcp/README.md,
# demo/config/run_querygate.sh. /api/health reports which of the three are
# actually reachable if one is missing.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$REPO_ROOT"
# Run as a module (not `python demo/control/app.py`) so the intra-package
# relative imports in demo/control/*.py resolve correctly.
exec poetry run uvicorn demo.control.app:app --host 127.0.0.1 --port 8900
