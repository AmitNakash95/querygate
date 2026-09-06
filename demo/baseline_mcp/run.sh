#!/usr/bin/env bash
# Starts the deliberately unsafe baseline "generic Postgres MCP server" on
# 127.0.0.1:8811 (mount path /mcp). DEMO-ONLY — see server.py's module
# docstring and README.md before running this anywhere but a local machine
# against the throwaway querygate_demo_pitch database.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# The explicit, hard-to-fat-finger opt-in the startup guard requires
# (demo/SPEC.md hard safety constraint). Do not default this on anywhere
# else — it exists so this can never start by accident.
export QUERYGATE_DEMO_UNSAFE_BASELINE=i-understand

# Override PITCH_AGENT_RO_DSN yourself if your local demo/db stack differs;
# server.py's DEFAULT_DSN matches demo/db/02_roles.sql's agent_ro role
# against demo/db/docker-compose.demo.yml's 127.0.0.1:5544 mapping.
cd "$REPO_ROOT"
exec poetry run python demo/baseline_mcp/server.py
