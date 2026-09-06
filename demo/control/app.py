"""QueryGate partner-demo control backend (demo/SPEC.md).

FastAPI app on 127.0.0.1:8900. Serves demo/control/static/ at "/" and
implements the HTTP contract in demo/SPEC.md: /api/health, /api/state,
/api/gate, /api/scenarios, /api/config, /api/run, /api/probe (SSE),
/api/reset.

Not part of the QueryGate product. Nothing under src/querygate/ imports this
module, and demo/ is excluded from every release artifact. Run with:

    demo/control/run.sh

Every /api/run performs a REAL MCP tools/call over HTTP against a real
running server (demo/control/scenarios.py + mcp_client.py). If a server is
down, the failure propagates as an honest HTTP error — never a canned
success (demo/SPEC.md's own rule, restated in every scenario module).
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import config, db, scenarios
from .mcp_client import MCPTransportError, list_tools

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("querygate.demo.control")

# ---------------------------------------------------------------------------
# Process-wide state. This is a single-operator demo control surface (one
# presenter, one browser tab) — a plain module-level dict plus a couple of
# locks is the right-weight amount of state, not a database.
# ---------------------------------------------------------------------------

_state = {"gate": "off"}
_probe = db.ProbeBroadcaster()
_run_lock = asyncio.Lock()  # serializes /api/run so two scenarios (esp. two
# overloads) can never stack against the 2 vCPU demo database.
_http_client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client
    _http_client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=32)
    )
    _probe.start()
    logger.info("control backend starting: probe running, http client ready")
    try:
        yield
    finally:
        await _probe.stop()
        await _http_client.aclose()
        _http_client = None
        logger.info("control backend shut down cleanly")


app = FastAPI(title="QueryGate demo control", lifespan=lifespan)

# F12: DNS-rebinding defence. This control server binds 127.0.0.1 and holds
# no auth of its own (single-operator, one presenter, one browser tab) — a
# page loaded in the presenter's browser from any other origin could
# otherwise resolve a hostname to 127.0.0.1 and POST /api/reset or /api/run
# mid-demo purely by the browser sending a same-origin-looking request with
# a Host header this process would otherwise accept unconditionally.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


def _client() -> httpx.AsyncClient:
    assert _http_client is not None, "http client not initialized (lifespan not started?)"
    return _http_client


def _cross_site_block(request: Request) -> Optional[JSONResponse]:
    """Reject a state-changing request whose Fetch Metadata says it did NOT
    originate from this page — the CSRF gap TrustedHostMiddleware alone
    leaves open on /api/reset, /api/gate, and /api/run (found in review of
    F12): a hostile page open in the presenter's browser doesn't need to
    spoof the Host header at all, since a plain
    `<form action="http://127.0.0.1:8900/api/reset" method="POST">` already
    resolves correctly and needs no preflight (bodyless POST is a CORS
    "simple request").

    `Sec-Fetch-Site` is a Fetch Metadata header the BROWSER itself sets on
    every request — including a plain HTML form submit, not just
    fetch()/XHR — and page JavaScript cannot forge or suppress it, unlike a
    custom header or hidden form field. Every evergreen browser (Chrome/
    Edge/Firefox since 2021, Safari 16.4+) sends it. If it's absent (an
    older browser, or a deliberate non-browser client such as curl/this
    review's own verification), stay permissive rather than lock the demo
    itself out over a header some legitimate client might not send.
    """
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        return JSONResponse(
            status_code=403,
            content={"detail": f"cross-site request rejected (Sec-Fetch-Site: {site})"},
        )
    return None


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------


async def _check_querygate() -> bool:
    try:
        resp = await _client().get(config.QUERYGATE_HEALTH_URL, timeout=3)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def _check_baseline_mcp() -> bool:
    # F1: a protocol-level tools/list call — never tools/call list_tables,
    # which the baseline server serves by running a real information_schema
    # query as agent_ro. /api/health is polled by the browser every 4s, so a
    # DB-touching liveness check would itself pollute the "queries executed"
    # headline this demo's whole confidentiality act rests on. Mirrors
    # demo/Makefile.include's pitch-wait probe of :8811.
    try:
        result = await list_tools(_client(), config.BASELINE_MCP_URL, timeout=3)
        return not result.is_protocol_error
    except MCPTransportError:
        return False


@app.get("/api/health")
async def api_health():
    db_ok, baseline_ok, qg_ok = await asyncio.gather(
        db.health_check_db(), _check_baseline_mcp(), _check_querygate()
    )
    return {"db": db_ok, "baseline_mcp": baseline_ok, "querygate": qg_ok}


# ---------------------------------------------------------------------------
# GET /api/state
# ---------------------------------------------------------------------------


@app.get("/api/state")
async def api_state():
    calls, active = await asyncio.gather(
        db.snapshot_agent_ro_calls(), db.count_active_role_backends()
    )
    probe_ms = _probe.latest.ms if _probe.latest else None
    return {
        "gate": _state["gate"],
        # agent_ro_active: how many agent_ro backends are running RIGHT NOW
        # (as opposed to agent_ro's cumulative pg_stat_statements call
        # count). demo/selfcheck.py's pre-flight check reads this key —
        # keep it present even when 0, not absent, so `.get(key, 0)` there
        # reflects a real measurement rather than a default.
        "db_calls": {"agent_ro": calls, "agent_ro_active": active},
        "probe_ms": probe_ms,
    }


# ---------------------------------------------------------------------------
# POST /api/gate
# ---------------------------------------------------------------------------


class GateBody(BaseModel):
    gate: str


@app.post("/api/gate")
async def api_gate(request: Request, body: GateBody):
    blocked = _cross_site_block(request)
    if blocked is not None:
        return blocked
    if body.gate not in ("on", "off"):
        return JSONResponse(status_code=400, content={"detail": "gate must be 'on' or 'off'"})
    _state["gate"] = body.gate
    return {"gate": _state["gate"]}


# ---------------------------------------------------------------------------
# GET /api/scenarios
# ---------------------------------------------------------------------------


@app.get("/api/scenarios")
async def api_scenarios():
    return scenarios.SCENARIOS


# ---------------------------------------------------------------------------
# GET /api/config
# ---------------------------------------------------------------------------

# Matches any scheme://[user[:password]]@host[:port]/... DSN (not just
# postgresql:// / postgresql+driver://) so a literal secret can never leave
# this endpoint even if someone later inlines one where
# connections.demo.yaml currently only has a ${VAR} placeholder, and even
# for a connection profile using one of QueryGate's other supported dialects
# (mssql, mysql, snowflake, bigquery). Also covers a DSN that carries its
# credential as a query-string parameter (?password=...) instead of
# userinfo.
#
# F9: an earlier version of this redaction used a single regex whose
# password group was `[^@\s]+` — a character class that, by construction,
# cannot match '@' at all. Despite this module's own comment claiming the
# match was "greedy up to the LAST '@' before the host", a class that
# excludes '@' can only ever stop at the FIRST '@' it meets, so
# `postgresql://u:p@ssw0rd@h/db` redacted only up to that first '@' and
# left `ssw0rd@h/db` — part of the real password — in plain text. Matching
# the DSN as one token and handing it to `urlsplit` is what the comment
# described: `urlsplit`/`.password` correctly treat the LAST '@' in the
# netloc as the userinfo/host separator (this is RFC 3986 authority
# parsing, not a regex heuristic), so a password containing '@' is
# redacted in full rather than partially.
#
# Follow-up found in review: the first fix still bounded the matched token
# at `[^\s"']+`, so a password containing a literal quote (legal inside a
# YAML double/single-quoted scalar) truncated the match BEFORE the '@' —
# e.g. `"postgresql://u:pa'ss@h/db"` only matched `postgresql://u:pa`, so
# urlsplit never even saw the '@', `.password` came back None, and the
# whole original string (password included) was returned untouched. Now
# the token stops only at whitespace, so the '@' is always inside the
# match; `_redact_one_dsn` fails CLOSED (redacts the whole userinfo span)
# rather than open if urlsplit/`.port` still can't parse what's left (e.g.
# a bare `#` before the `@`, which urlsplit treats as a fragment
# delimiter, or a non-numeric port). A literal unescaped space inside an
# unquoted password is not valid DSN syntax in the first place (RFC 3986
# requires percent-encoding) and remains a known, accepted residual.
_DSN_URL_RE = re.compile(r"[a-zA-Z][\w+.\-]*://\S+")
_DSN_QUERY_PARAM_RE = re.compile(r"((?:password|pwd|sslpassword)=)([^&\s\"']+)", re.IGNORECASE)


def _redact_one_dsn(match: "re.Match[str]") -> str:
    url = match.group(0)
    try:
        parts = urlsplit(url)
        password, username = parts.password, parts.username
        hostname, port = parts.hostname, parts.port  # .port is lazy — can raise ValueError
        scheme, path, query, fragment = parts.scheme, parts.path, parts.query, parts.fragment
    except ValueError:
        # Something about the userinfo/host couldn't be parsed cleanly
        # (e.g. an unescaped '#' before the real '@', or a non-numeric
        # port). Fail closed: if there's a credential-shaped '@' anywhere
        # in the token, redact everything up to and including the LAST
        # one rather than risk returning the original text with a
        # password still in it.
        scheme, sep, rest = url.partition("://")
        if sep and "@" in rest:
            return f"{scheme}{sep}***REDACTED***@{rest.rsplit('@', 1)[1]}"
        return url
    if not password:
        return url  # no credential in the userinfo — nothing to redact here
    host = f"[{hostname}]" if hostname and ":" in hostname else (hostname or "")
    netloc = f"{host}:{port}" if port else host
    netloc = f"{username or ''}:***REDACTED***@{netloc}"
    return urlunsplit((scheme, netloc, path, query, fragment))


def _redact_dsn(text: str) -> str:
    text = _DSN_URL_RE.sub(_redact_one_dsn, text)
    return _DSN_QUERY_PARAM_RE.sub(r"\1***REDACTED***", text)


@app.get("/api/config")
async def api_config():
    # Redact both files, not just connections.demo.yaml — policy.demo.yaml
    # has no credential-bearing field today, but this makes that a property
    # of the redaction covering every file this endpoint returns, not an
    # assumption about the one file that currently needs it.
    policy_yaml = _redact_dsn(config.POLICY_YAML_PATH.read_text())
    connections_yaml = _redact_dsn(config.CONNECTIONS_YAML_PATH.read_text())
    return {"policy_yaml": policy_yaml, "connections_yaml": connections_yaml}


# ---------------------------------------------------------------------------
# POST /api/run
# ---------------------------------------------------------------------------


class RunBody(BaseModel):
    scenario_id: str
    gate: str


@app.post("/api/run")
async def api_run(request: Request, body: RunBody):
    blocked = _cross_site_block(request)
    if blocked is not None:
        return blocked
    if body.scenario_id not in scenarios.SCENARIO_BY_ID:
        return JSONResponse(
            status_code=404, content={"detail": f"unknown scenario_id {body.scenario_id!r}"}
        )
    if body.gate not in ("on", "off"):
        return JSONResponse(status_code=400, content={"detail": "gate must be 'on' or 'off'"})

    if _run_lock.locked():
        return JSONResponse(
            status_code=409,
            content={"detail": "another scenario is already running — wait for it to finish"},
        )

    async with _run_lock:
        try:
            result = await scenarios.run_scenario(_client(), body.scenario_id, body.gate)
        except MCPTransportError as exc:
            # Redact: exc's text can embed an upstream endpoint URL (from
            # config.QUERYGATE_MCP_URL/BASELINE_MCP_URL, both env-overridable)
            # or upstream response body — belt-and-suspenders alongside
            # /api/config's redaction, in case either URL is ever pointed at
            # something carrying userinfo.
            return JSONResponse(
                status_code=502,
                content={
                    "detail": f"real MCP call failed, no result to show: {_redact_dsn(str(exc))}"
                },
            )
        except Exception as exc:  # noqa: BLE001
            # Full detail (may include driver internals, paths, etc.) goes
            # only to the server log, never to the browser — same posture
            # src/querygate/core/exceptions.py takes with an unclassified
            # exception, applied here to this demo-only surface too.
            logger.exception("scenario %s (gate=%s) failed", body.scenario_id, body.gate)
            return JSONResponse(
                status_code=500,
                content={
                    "detail": f"scenario failed: {type(exc).__name__} "
                    "(see the control server log for detail)"
                },
            )

    return result


# ---------------------------------------------------------------------------
# GET /api/probe (SSE)
# ---------------------------------------------------------------------------


@app.get("/api/probe")
async def api_probe(request: Request):
    async def gen():
        async for frame in db.sse_stream(_probe):
            if await request.is_disconnected():
                break
            yield frame

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# POST /api/reset
# ---------------------------------------------------------------------------


@app.post("/api/reset")
async def api_reset(request: Request):
    blocked = _cross_site_block(request)
    if blocked is not None:
        return blocked
    if _run_lock.locked():
        # A scenario is mid-flight — killing backends or resetting counters
        # underneath it would corrupt the real pg_stat_statements delta that
        # scenario is about to report (SPEC.md: "never inferred from the
        # outcome"). Serialize under the same lock /api/run already uses.
        return JSONResponse(
            status_code=409,
            content={"detail": "a scenario is running — wait for it to finish before resetting"},
        )
    async with _run_lock:
        killed = await db.kill_role_backends()
        # F8: SPEC.md says /api/reset "clears counters"; it previously only
        # freed stray backends. Reset pg_stat_statements (as pitch_owner) so
        # a reset before each act shows a small, credible starting number
        # instead of a four-figure accumulated one from earlier runs/health
        # polls.
        await db.reset_query_stats()
    return {"ok": True, "killed_backends": len(killed)}


# ---------------------------------------------------------------------------
# Static frontend — mounted last so it doesn't shadow the /api/* routes
# above. demo/control/static/ is finished, untouched by this backend.
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=str(config.STATIC_DIR), html=True), name="static")
