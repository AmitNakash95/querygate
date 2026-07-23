#!/usr/bin/env python3
"""Dynamic application security testing (DAST) for QueryGate's REST surface.

Boots the real ASGI app in-process and turns Schemathesis loose on its live
OpenAPI schema, fuzzing every documented endpoint with malformed, boundary, and
schema-violating payloads. This is the machine-checkable proof of QueryGate's
central guarantee: the *only* input a caller can submit is a validated
`StructuredQuery` AST — there is no raw-SQL field or endpoint — and adversarial
input is rejected at the validation boundary, never a server error and never
executed against a database.

Two checks are gated (deny-by-default; the run fails on any violation):

  * ``not_a_server_error``      — no fuzzed request may produce a 5xx. A 5xx
    means input reached past validation into an unhandled code path.
  * ``negative_data_rejection`` — data that violates the schema MUST be rejected
    with a client error (4xx), proving the validation boundary holds and that
    no malformed AST slips through to compilation/execution.

Complements — does not replace — the hand-written adversarial suite
(``make test-security``): that suite encodes specific known bypass classes;
this fuzzes the whole documented surface for unknown ones. See TODO.md item 89
and docs/SECURITY_POSTURE.md.

Schemathesis runs from its **official pinned Docker image**, not as a Python
dependency: its transitive pins (starlette<1 on 3.x, pytest>=8 on 4.x) conflict
with both QueryGate's runtime security fixes and its pinned test stack, so — like
Trivy/gitleaks/Semgrep — a dev testing tool is kept entirely out of the shipped
dependency graph. The app is booted here in the Poetry venv and the containerized
scanner reaches it over the Docker host gateway.

Run: ``make test-dast`` (or ``poetry run python scripts/run_dast.py``). Requires
Docker.

No database is required or contacted: with no connections registered, every
query endpoint rejects at validation (unknown connection / invalid AST) before
any DB touch — which is exactly the boundary under test.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The app binds all interfaces so the containerized scanner can reach it over the
# Docker host gateway; readiness is polled from the host over loopback.
BIND_HOST = "0.0.0.0"  # nosec B104 — ephemeral local DAST server, torn down at end of run
LOOPBACK = "127.0.0.1"
# Resolvable from inside a container to the host on both Docker Desktop (macOS)
# and Linux (via --add-host=...:host-gateway below).
CONTAINER_HOST = "host.docker.internal"
API_PREFIX = "/api/v1"
# Pinned 3.x image so the CLI flags below stay valid (4.x rewrote the CLI).
SCHEMATHESIS_IMAGE = os.environ.get(
    "QUERYGATE_SCHEMATHESIS_IMAGE", "schemathesis/schemathesis:3.39.16"
)
# Kept modest so the gate stays fast enough for every CI run; bump locally via
# QUERYGATE_DAST_MAX_EXAMPLES for a heavier sweep.
MAX_EXAMPLES = os.environ.get("QUERYGATE_DAST_MAX_EXAMPLES", "25")
READINESS_TIMEOUT_S = 30


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((LOOPBACK, 0))
        return s.getsockname()[1]


def _wait_for_health(base: str, timeout_s: int) -> None:
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"{base}/health", timeout=2
            ) as resp:  # nosec B310 — fixed loopback URL to our own test server
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_err = exc
            time.sleep(0.25)
    raise RuntimeError(f"app did not become healthy within {timeout_s}s: {last_err}")


def main() -> int:
    # Force a local, dev-auth-bypass posture so the OpenAPI schema is served and
    # endpoints are reachable without configured credentials — the schema itself
    # is only exposed when is_local is true (see core/config.py). No real
    # connections are registered, so no database is ever contacted.
    env = os.environ.copy()
    env["QUERYGATE_ENVIRONMENT"] = "development"
    env.setdefault("API_KEYS", "[]")
    env.setdefault("MCP_API_KEYS", "[]")
    # Boot against an empty connections file so no profile is registered — the
    # boundary under test. The default (examples/connections.example.yaml)
    # references ${QUERYGATE_DEMO_DB_URL}, which is intentionally unset here, so
    # loading it at startup (HealthMonitor priming) would fail the whole run.
    env.setdefault("CONNECTIONS_FILE", str(ROOT / "scripts" / "dast_connections.yaml"))
    os.environ.update(env)

    port = _free_port()
    health_base = f"http://{LOOPBACK}:{port}"
    # URLs the *container* uses to reach the host app.
    schema_url = f"http://{CONTAINER_HOST}:{port}{API_PREFIX}/openapi.json"
    container_base = f"http://{CONTAINER_HOST}:{port}"

    # Import lazily, after env is set, so AppConfig reads the intended values.
    import uvicorn

    from querygate.api.app import app

    server = uvicorn.Server(
        uvicorn.Config(app, host=BIND_HOST, port=port, log_level="warning", lifespan="on")
    )
    thread = threading.Thread(target=server.run, name="dast-uvicorn", daemon=True)
    thread.start()

    try:
        _wait_for_health(health_base, READINESS_TIMEOUT_S)

        cmd = [
            "docker",
            "run",
            "--rm",
            # Make the host reachable as host.docker.internal on Linux CI too
            # (Docker Desktop already provides it; this is harmless there).
            "--add-host=host.docker.internal:host-gateway",
            SCHEMATHESIS_IMAGE,
            "run",
            schema_url,
            # FastAPI's documented operation paths already include the /api/v1
            # prefix, so the base URL is the server root — not the prefix, or
            # every request double-prefixes to /api/v1/api/v1/... and 404s.
            "--base-url",
            container_base,
            # FastAPI emits OpenAPI 3.1; Schemathesis 3.x needs this opt-in.
            "--experimental=openapi-3.1",
            # Every endpoint that accepts a *recursive* AST is excluded here
            # because Schemathesis cannot auto-generate data for recursive
            # references (schemathesis/schemathesis#947):
            #   * the read StructuredQuery AST (joins/subqueries nest) —
            #     query/explain/batch/approve, template-run, admin query-simulate;
            #   * the write AST (nested WhereGroup predicates, subqueries) —
            #     write/preview, write/execute, write/approve.
            # That is not a coverage gap: those exact operations get deeper,
            # purpose-built adversarial coverage in tests/security/ — the read
            # surface in test_malformed_input_fuzzing.py (item 36 phase 2a, REST
            # *and* MCP) and the write surface in test_write_boundary.py (item 93)
            # — which assert no 5xx, no internal leak, and that malformed input
            # never reaches compilation/execution. Schemathesis owns the other
            # ~60 documented operations (admin config/catalog, help, templates,
            # connections). NOTE: only a single --exclude-path-regex takes effect,
            # so all patterns are ORed into one alternation here.
            "--exclude-path-regex",
            r"(/query(/explain|/batch|/approve)?|/write/(preview|execute|approve)|/query-templates/[^/]+/run|/admin/config/simulate)$",
            "--checks",
            "not_a_server_error",
            "--checks",
            "negative_data_rejection",
            "--hypothesis-max-examples",
            str(MAX_EXAMPLES),
            "--request-timeout",
            "10000",
            "--fixups",
            "fast_api",
        ]
        print(f"[dast] fuzzing {container_base}{API_PREFIX} (max-examples={MAX_EXAMPLES})\n")
        result = subprocess.run(cmd, cwd=ROOT, env=env)  # nosec B603 — fixed argv, no shell
        if result.returncode == 0:
            print(
                "\n[dast] PASS — no server errors and all schema-violating input "
                "was rejected at the validation boundary."
            )
        else:
            print(
                "\n[dast] FAIL — Schemathesis found a server error or an accepted "
                "malformed payload. See the report above.",
                file=sys.stderr,
            )
        return result.returncode
    finally:
        server.should_exit = True
        thread.join(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
