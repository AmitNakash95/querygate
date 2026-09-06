"""Constants for the QueryGate partner-demo control backend (demo/SPEC.md).

Not part of the QueryGate product — see demo/control's own scope note in
demo/SPEC.md. Ports and demo-only throwaway credentials mirror the literals
already committed in demo/db/02_roles.sql, demo/config/env.demo, and
demo/baseline_mcp/server.py; every value here can be overridden by an env var
of the same name for local flexibility, but the defaults are exactly the
values the rest of demo/ already uses so this component needs no extra setup.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CONTROL_DIR = Path(__file__).resolve().parent
DEMO_DIR = CONTROL_DIR.parent
STATIC_DIR = CONTROL_DIR / "static"
POLICY_YAML_PATH = DEMO_DIR / "config" / "policy.demo.yaml"
CONNECTIONS_YAML_PATH = DEMO_DIR / "config" / "connections.demo.yaml"

# ---------------------------------------------------------------------------
# Control server itself
# ---------------------------------------------------------------------------
CONTROL_HOST = os.environ.get("QG_DEMO_CONTROL_HOST", "127.0.0.1")
CONTROL_PORT = int(os.environ.get("QG_DEMO_CONTROL_PORT", "8900"))

# ---------------------------------------------------------------------------
# QueryGate (gate ON)
# ---------------------------------------------------------------------------
QUERYGATE_BASE_URL = os.environ.get("QG_DEMO_QUERYGATE_URL", "http://127.0.0.1:8010")
QUERYGATE_MCP_URL = os.environ.get("QG_DEMO_QUERYGATE_MCP_URL", f"{QUERYGATE_BASE_URL}/mcp/")
QUERYGATE_HEALTH_URL = f"{QUERYGATE_BASE_URL}/health"
QUERYGATE_MCP_API_KEY = os.environ.get("QG_DEMO_QUERYGATE_MCP_KEY", "pitch-demo-key")
QUERYGATE_CONNECTION_ID = "demo"

# ---------------------------------------------------------------------------
# Baseline (gate OFF) — deliberately unsafe demo MCP server
# ---------------------------------------------------------------------------
BASELINE_BASE_URL = os.environ.get("QG_DEMO_BASELINE_URL", "http://127.0.0.1:8811")
BASELINE_MCP_URL = os.environ.get("QG_DEMO_BASELINE_MCP_URL", f"{BASELINE_BASE_URL}/mcp")

# ---------------------------------------------------------------------------
# Postgres — throwaway demo-only credentials, safe to commit (see
# demo/db/02_roles.sql and demo/db/README.md).
# ---------------------------------------------------------------------------
PG_HOST = os.environ.get("QG_DEMO_PG_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("QG_DEMO_PG_PORT", "5544"))
PG_DATABASE = os.environ.get("QG_DEMO_PG_DATABASE", "querygate_demo_pitch")

PG_ADMIN_DSN = os.environ.get(
    "QG_DEMO_PG_ADMIN_DSN",
    f"postgresql://pitch_owner:pitch_owner_demo_pw_only@{PG_HOST}:{PG_PORT}/{PG_DATABASE}",
)
PG_PROBE_DSN = os.environ.get(
    "QG_DEMO_PG_PROBE_DSN",
    f"postgresql://probe_user:probe_user_demo_pw_only@{PG_HOST}:{PG_PORT}/{PG_DATABASE}",
)
# agent_ro's own DSN is never opened directly by this control backend — both
# MCP servers hold that credential, not us. Kept here only so /api/reset and
# the overload killer can identify agent_ro backends by role name, not DSN.
AGENT_RO_ROLE = "agent_ro"
# Per-statement timeout (asyncpg's `command_timeout`) on the admin
# connection used for snapshots, the reset-stats call, and the overload
# kill sweep (F5) — bounds any single admin statement even while it's
# talking to a saturated 2 vCPU database.
ADMIN_CONN_COMMAND_TIMEOUT_SECONDS = float(
    os.environ.get("QG_DEMO_ADMIN_COMMAND_TIMEOUT_SECONDS", "5.0")
)

# ---------------------------------------------------------------------------
# Probe (background health signal, GET /api/probe)
# ---------------------------------------------------------------------------
PROBE_INTERVAL_SECONDS = float(os.environ.get("QG_DEMO_PROBE_INTERVAL_SECONDS", "0.4"))
PROBE_QUERY = (
    "SELECT count(*) FROM orders WHERE status = 'completed' "
    "AND created_at > now() - interval '30 days'"
)
PROBE_HOT_THRESHOLD_MS = 150.0
# Bound on ProbeBroadcaster.stop()'s wait for its background task to finish
# on its own before it force-cancels (F12) — shutdown/Ctrl-C must never hang
# waiting on a probe tick stuck against a saturated database.
PROBE_STOP_TIMEOUT_SECONDS = float(os.environ.get("QG_DEMO_PROBE_STOP_TIMEOUT_SECONDS", "5.0"))

# ---------------------------------------------------------------------------
# Overload scenario (AMENDMENT 1)
# ---------------------------------------------------------------------------
OVERLOAD_CONCURRENCY = 20
OVERLOAD_WALL_CLOCK_BOUND_SECONDS = 15.0
OVERLOAD_CROSS_JOIN_SQL = "SELECT count(*) FROM customers, orders, order_items"

# ---------------------------------------------------------------------------
# Legit scenario (both paths, 15 runs each)
# ---------------------------------------------------------------------------
LEGIT_RUNS_PER_SIDE = 15

# ---------------------------------------------------------------------------
# Audit ledger (jsonl_chained) — QueryGate's own real hash-chained audit log
# for this demo. demo/config/env.demo sets AUDIT_SINK_BACKEND=jsonl_chained,
# AUDIT_JSONL_PATH=var/querygate-pitch-ledger.jsonl (resolved relative to
# demo/config/, same as POLICY_YAML_PATH/CONNECTIONS_YAML_PATH above) and
# AUDIT_LEDGER_HMAC_KEY. QueryGate itself is launched via
# demo/config/run_querygate.sh, which sources env.demo into QueryGate's own
# process — this control backend is a separate process that does NOT source
# that file.
#
# Item A2 (partner-demo audit-panel fix session): this used to repeat both
# values here as hand-maintained literals, which meant a drift between this
# file and env.demo's would make demo/control/audit.py recompute every hash
# with the wrong key and report a perfectly intact ledger as tampered — a
# false "CHAIN BROKEN" alarm on stage caused by nothing but a config typo.
# Instead, read both values straight out of env.demo at import time (a plain
# KEY=VALUE line parse — no config library, and no import from querygate:
# demo/SPEC.md's "Nothing in demo/ may import from or modify src/querygate/"
# still holds). The literals below are now only the last-resort fallback if
# env.demo is missing or unreadable, not the source of truth.
#
# Precedence, most to least authoritative: an explicit env var override (for
# local flexibility, same as every other value in this file) beats a value
# read from env.demo, which beats the hardcoded literal. The HMAC key also
# falls back through BOTH env var spellings — this control backend's own
# `QG_DEMO_AUDIT_LEDGER_HMAC_KEY` prefix, and the un-prefixed
# `AUDIT_LEDGER_HMAC_KEY` that env.demo/prove_audit.py/selfcheck.py/RUNBOOK.md
# all already use — since those two names coincided only by accident before
# this fix, not by any enforced contract.
ENV_DEMO_PATH = DEMO_DIR / "config" / "env.demo"


def _read_env_demo(key: str) -> Optional[str]:
    """Best-effort KEY=VALUE line lookup in demo/config/env.demo. Never
    raises: an unreadable file or an absent key both just fall through to
    the caller's own fallback, so a missing env.demo degrades this control
    backend to its old hardcoded-literal behavior rather than crashing it."""
    try:
        text = ENV_DEMO_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    result: Optional[str] = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        # `set -a; source env.demo` accepts an `export ` prefix; we must too,
        # or an operator who writes the more explicit form silently gets the
        # hardcoded fallback while QueryGate gets the real value.
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        found_key, _, value = line.partition("=")
        if found_key.strip() != key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            # env.demo already quotes some values (MCP_API_KEYS, API_KEYS), so
            # quoting is this file's own idiom. The shell strips them; keeping
            # them would make every recomputed hash mismatch and paint a red
            # CHAIN BROKEN over a perfectly intact ledger.
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        # Keep scanning: `source` takes the LAST assignment, and appending an
        # override block at the bottom of the file is the natural way to rotate
        # a value.
        result = value
    return result


_ENV_DEMO_LEDGER_RELATIVE_PATH = _read_env_demo("AUDIT_JSONL_PATH")
_ENV_DEMO_LEDGER_PATH = (
    str(DEMO_DIR / "config" / _ENV_DEMO_LEDGER_RELATIVE_PATH)
    if _ENV_DEMO_LEDGER_RELATIVE_PATH
    else None
)
AUDIT_LEDGER_PATH = Path(
    os.environ.get("QG_DEMO_AUDIT_LEDGER_PATH")
    or os.environ.get("AUDIT_JSONL_PATH")
    or _ENV_DEMO_LEDGER_PATH
    or str(DEMO_DIR / "config" / "var" / "querygate-pitch-ledger.jsonl")
)
AUDIT_LEDGER_HMAC_KEY = (
    os.environ.get("QG_DEMO_AUDIT_LEDGER_HMAC_KEY")
    or os.environ.get("AUDIT_LEDGER_HMAC_KEY")
    or _read_env_demo("AUDIT_LEDGER_HMAC_KEY")
    or "pitch-demo-ledger-key-not-a-real-secret"
)

# ---------------------------------------------------------------------------
# HTTP client timeouts
# ---------------------------------------------------------------------------
# Baseline's unbounded queries (bulk_export, pii_column) can legitimately take
# a couple of seconds against 250k/1.2M rows with no cap — give it real room
# rather than a tight timeout that would turn an honest slow answer into a
# fabricated error.
HTTP_TIMEOUT_SECONDS = float(os.environ.get("QG_DEMO_HTTP_TIMEOUT_SECONDS", "30.0"))
