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

**The scanner is given a PRUNED copy of the schema, not the live URL.** The
AST-accepting operations are out of scope here (see ``EXCLUDED_PATHS`` below for
why, and where they *are* covered). They used to be dropped with Schemathesis's
``--exclude-path-regex``, which skips *executing* them but still parses their
schemas — and Schemathesis canonicalises every component in the document before
it runs anything. The read AST is deeply recursive (``StructuredQuery`` →
``Predicate`` → ``value_subquery`` → ``StructuredQuery``, and since TODO.md item
100 also ``Expression`` → ``CaseExpr`` → ``CaseWhen`` → ``WhereNode`` →
``Predicate`` → ``Expression`` across a six-member union), and canonicalising
that blew the scanner's memory: every operation — including trivial ones like
``GET /help/search`` — died with SIGKILL/137 once item 100 widened the cycle.

So the paths are removed from the document itself and the now-unreachable
component schemas are garbage-collected, which makes the exclusion genuinely
exclusive rather than execution-only. Operation coverage is unchanged (the
excluded paths were never fuzzed either way) and the run is roughly twice as
fast.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
# Schemathesis exits 1 when a check fails; anything else non-zero means it did
# not run (2 = usage error, 137/143 = 128 + SIGKILL/SIGTERM, also reported as
# -9/-15 by subprocess).
_FINDINGS_EXIT_CODE = 1
_KILLED_EXIT_CODES = frozenset({137, 143, -9, -15})
_SCHEMA_REF_PREFIX = "#/components/schemas/"

# Operations that accept a *recursive* AST, removed from the schema handed to the
# scanner (see the module docstring for why removal, not --exclude-path-regex):
#   * the read StructuredQuery AST (joins/subqueries/expressions nest) —
#     query/explain/verdict/batch/approve, template-run, admin query-simulate;
#   * the write AST (nested WhereGroup predicates, subqueries) —
#     write/preview, write/execute, write/approve.
# Not a coverage gap: those exact operations get deeper, purpose-built
# adversarial coverage in tests/security/ — the read surface in
# test_malformed_input_fuzzing.py (item 36 phase 2a, REST *and* MCP) and the
# write surface in test_write_boundary.py (item 93) — which assert no 5xx, no
# internal leak, and that malformed input never reaches compilation/execution.
# Schemathesis owns the other ~60 documented operations (admin config/catalog,
# help, templates, connections).
EXCLUDED_PATHS = re.compile(
    r"(/query(/explain|/verdict|/batch|/approve)?"
    r"|/write/(preview|execute|approve)"
    r"|/query-templates/[^/]+/run"
    r"|/admin/config/simulate)$"
)


def _schema_refs(node: object, found: set) -> None:
    """Collect every ``#/components/schemas/<name>`` reference under ``node``."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_SCHEMA_REF_PREFIX):
            found.add(ref[len(_SCHEMA_REF_PREFIX) :])
        for value in node.values():
            _schema_refs(value, found)
    elif isinstance(node, list):
        for value in node:
            _schema_refs(value, found)


def _serve_schema(document: dict) -> tuple[ThreadingHTTPServer, int]:
    """Serve ``document`` as JSON on an ephemeral port, for the scanner to fetch.

    The pruned schema is handed over via HTTP rather than a Docker bind mount.
    A mount looked simpler and worked on macOS, but failed on the Linux CI
    runner with "The specified file does not exist": `tempfile` directories are
    created 0700 for the *creating* user, and the Schemathesis image runs as a
    different UID, so the container could not traverse into it. Docker Desktop
    hides that with UID translation; a Linux runner does not.

    Serving over HTTP has no filesystem, ownership, or path-translation
    semantics to get wrong, and it is how the scanner consumed the schema
    originally — the only change is that it now reads our pruned copy.
    """
    payload = json.dumps(document).encode("utf-8")

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's API
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:
            """Silence per-request logging; the scanner's own output is enough."""

    # Binds all interfaces for the same reason the app does: the scanner reaches
    # it from inside a container over the Docker host gateway.
    httpd = ThreadingHTTPServer((BIND_HOST, 0), _Handler)  # nosec B104 — ephemeral local server
    threading.Thread(target=httpd.serve_forever, name="dast-schema", daemon=True).start()
    port = httpd.server_address[1]

    # Prove it serves valid JSON before handing the URL to the scanner. Without
    # this, a broken handler surfaces as Schemathesis failing to load the schema
    # — which reads like a finding rather than a bug in this script, the same
    # confusion the unreadable bind mount caused.
    try:
        with urllib.request.urlopen(  # nosec B310 — fixed loopback URL to our own server
            f"http://{LOOPBACK}:{port}/openapi.json", timeout=10
        ) as resp:
            json.load(resp)
    except Exception:
        httpd.shutdown()
        httpd.server_close()
        raise

    return httpd, port


def prune_schema(spec: dict) -> dict:
    """Drop the AST-accepting paths and every component schema that becomes
    unreachable, returning a new document.

    The garbage-collection half is the part that matters: deleting the paths
    alone leaves the recursive `StructuredQuery`/`Expression` definitions sitting
    in `components.schemas`, and the scanner canonicalises those regardless of
    which operations it runs — which is the exact out-of-memory failure this
    exists to prevent. Reachability is computed transitively from the surviving
    paths, so nothing an in-scope operation needs is ever removed.
    """
    pruned = dict(spec)
    pruned["paths"] = {
        path: item
        for path, item in spec.get("paths", {}).items()
        if not EXCLUDED_PATHS.search(path)
    }

    components = spec.get("components", {}).get("schemas", {})
    if not components:
        return pruned

    reachable: set = set()
    frontier: set = set()
    _schema_refs(pruned["paths"], frontier)
    while frontier:
        name = frontier.pop()
        if name in reachable or name not in components:
            continue
        reachable.add(name)
        nested: set = set()
        _schema_refs(components[name], nested)
        frontier |= nested - reachable

    pruned["components"] = dict(spec["components"])
    pruned["components"]["schemas"] = {
        name: schema for name, schema in components.items() if name in reachable
    }
    return pruned


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
    # The schema is fetched over loopback, pruned, and mounted into the
    # container; only the base URL still points at the host app.
    schema_url = f"{health_base}{API_PREFIX}/openapi.json"
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

        with urllib.request.urlopen(  # nosec B310 — fixed loopback URL to our own test server
            schema_url, timeout=30
        ) as resp:
            spec = json.load(resp)
        pruned = prune_schema(spec)
        all_components = spec.get("components", {}).get("schemas", {})
        print(
            f"[dast] schema pruned to {len(pruned['paths'])}/{len(spec['paths'])} paths and "
            f"{len(pruned.get('components', {}).get('schemas', {}))}/{len(all_components)} "
            "component schemas (AST-accepting operations are covered by tests/security/)",
            flush=True,
        )

        schema_server, schema_port = _serve_schema(pruned)
        try:
            cmd = [
                "docker",
                "run",
                "--rm",
                # Make the host reachable as host.docker.internal on Linux CI too
                # (Docker Desktop already provides it; this is harmless there).
                "--add-host=host.docker.internal:host-gateway",
                SCHEMATHESIS_IMAGE,
                "run",
                # The pruned document, served over HTTP from this process — no
                # bind mount, so no UID/permission divergence between macOS and
                # a Linux CI runner (see _serve_schema).
                f"http://{CONTAINER_HOST}:{schema_port}/openapi.json",
                # FastAPI's documented operation paths already include the /api/v1
                # prefix, so the base URL is the server root — not the prefix, or
                # every request double-prefixes to /api/v1/api/v1/... and 404s.
                "--base-url",
                container_base,
                # FastAPI emits OpenAPI 3.1; Schemathesis 3.x needs this opt-in.
                "--experimental=openapi-3.1",
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
            print(
                f"[dast] fuzzing {container_base}{API_PREFIX} (max-examples={MAX_EXAMPLES})\n",
                flush=True,
            )
            result = subprocess.run(cmd, cwd=ROOT, env=env)  # nosec B603 — fixed argv, no shell
        finally:
            schema_server.shutdown()
            schema_server.server_close()

        if result.returncode == 0:
            print(
                "\n[dast] PASS — no server errors and all schema-violating input "
                "was rejected at the validation boundary.",
                flush=True,
            )
        elif result.returncode != _FINDINGS_EXIT_CODE:
            # Anything that is not "clean" (0) or "found something" (1) means the
            # scanner failed to run: a usage error (2), or a signal (137/143 =
            # 128 + SIGKILL/SIGTERM, typically out-of-memory). Saying "found a
            # server error" for those sends the next person hunting a 5xx that
            # never existed — which is exactly what happened twice while fixing
            # this job, first for an OOM kill and then for an unreadable schema
            # mount. Both times the misleading message cost real debugging time.
            reason = (
                "killed by a signal — usually out-of-memory while processing the schema; "
                "check whether a new recursive model reached components.schemas and "
                "whether prune_schema() still removes it (tests/unit/test_run_dast.py "
                "catches that in the fast suite)"
                if result.returncode in _KILLED_EXIT_CODES
                else "could not run — check the scanner's own error above (bad arguments, "
                "or it could not fetch the schema)"
            )
            print(
                f"\n[dast] ERROR — the scanner {reason}. It did NOT report a finding.",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(
                "\n[dast] FAIL — Schemathesis found a server error or an accepted "
                "malformed payload. See the report above.",
                file=sys.stderr,
                flush=True,
            )
        return result.returncode
    finally:
        server.should_exit = True
        thread.join(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
