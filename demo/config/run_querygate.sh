#!/usr/bin/env bash
# Starts QueryGate on port 8010 for the partner demo (Act 3 — "gate ON"),
# using demo/config/env.demo + connections.demo.yaml + policy.demo.yaml.
#
# Deliberately does NOT depend on the repo's root .env: QueryGate's
# AppConfig reads a relative ".env" (see src/querygate/core/config.py's
# `Config.env_file = ".env"`) resolved against the process's working
# directory, so this script always runs with its own directory
# (demo/config/) as cwd — there is no .env file there, so nothing can leak
# in from the repo root, and every setting comes from env.demo (sourced
# below) or from safe code defaults.
#
# Usage:
#   demo/config/run_querygate.sh
#
# Runs in the foreground; stop with Ctrl-C.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd "${SCRIPT_DIR}"

if [[ ! -f "env.demo" ]]; then
  echo "error: env.demo not found in ${SCRIPT_DIR}" >&2
  exit 1
fi

# Load env.demo into this shell. set -a exports every variable it defines
# so the child `poetry run` process inherits them.
set -a
# shellcheck disable=SC1091
source "env.demo"
set +a

mkdir -p "${SCRIPT_DIR}/var"

echo "Starting QueryGate on http://${HOST_ADDRESS}:${PORT} (MCP at ${MCP_MOUNT_PATH})"
echo "  working directory : ${SCRIPT_DIR}"
echo "  connections file   : ${SCRIPT_DIR}/${CONNECTIONS_FILE}"
echo "  policy file        : ${SCRIPT_DIR}/${POLICY_FILE}"
echo "  audit log          : ${SCRIPT_DIR}/${AUDIT_JSONL_PATH}"
echo "  database           : querygate_demo_pitch on 127.0.0.1:5544 (role agent_ro)"
echo

# `poetry run` walks up from cwd to find pyproject.toml without changing
# cwd itself, so this still finds the repo's Poetry environment even though
# cwd stays demo/config, not the repo root. Do NOT pass `poetry -C`/
# `--directory` here — that flag changes poetry's own working directory
# (and the subprocess it execs) to the given path, which would put us back
# in the repo root and reopen the root-.env leak this script exists to
# avoid.
exec poetry run python -m querygate.run
