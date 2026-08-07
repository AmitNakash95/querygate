"""Transport-level request-body guards for the mounted MCP surface.

TODO.md item 86. The REST surface rejects a malformed body (oversized, or
nested past the JSON parser's recursion guard) with a clean 4xx, but the
upstream MCP Streamable-HTTP transport's own ``json.loads(body)`` raises
``RecursionError`` on a deeply-nested body and surfaces it as a handled — but
HTTP 500 — JSON-RPC internal error. This ASGI wrapper sits *around* the MCP
mount (outside auth) and rejects an oversized or absurdly deep body as a clean
client error (413/400) *before* the transport ever parses it, so the MCP
surface matches REST's "malformed input is a client error, never a 5xx"
posture. Both thresholds are configurable with generous defaults
(``AppConfig.mcp_max_request_bytes`` / ``mcp_max_request_depth``) so legitimate
large batches are unaffected.

TODO.md item 127. The MCP ``2026-07-28`` Streamable HTTP spec mirrors
``method``/``params.name``/``params.uri`` into ``Mcp-Method``/``Mcp-Name`` HTTP
headers so intermediaries (load balancers, gateways) can route on them without
parsing the body, and mandates that a server processing the body reject any
request where a present header disagrees with the body (``-32020
HeaderMismatch``) — otherwise a gateway authorizing on the header while
QueryGate executes the body is a confused deputy. QueryGate currently speaks
protocol revision ``2025-11-25`` (item 128), which does not define these
headers, so this validates **if present**, not required — shippable now,
independent of the protocol upgrade, and it fails closed the moment a fronting
gateway starts sending them.

TODO.md item 130. The same spec lets a server mark a primitive tool
*parameter* (not just ``method``/``name``) with an ``x-mcp-header`` schema
annotation, mirrored by a conforming client into ``Mcp-Param-{Name}``.
QueryGate annotates ``connection`` on every tool that takes it
(``mcp/tools/{query,schema,write}.py``), so ``Mcp-Param-Connection`` is now a
header a real client can send — and the spec's "Server Validation" section
states the header/body agreement ``MUST`` generically, not only for
``Mcp-Method``/``Mcp-Name``: *"Servers that process the request body MUST
reject requests where the values specified in the headers do not match the
corresponding values in the request body."* Extending item 127's guard to
this one header closes the same confused-deputy shape for the one parameter
QueryGate has actually started asking clients to mirror: a gateway
authorizing ``Mcp-Param-Connection: analytics`` while the body's
``params.arguments.connection`` names a different, disallowed connection.
Same posture as item 127 — validate **if present**, not required, since
QueryGate doesn't yet require ``2026-07-28``. This does not, and cannot,
cover a ``JoinSpec``'s own ``connection`` field (no header exists for it —
see item 130's write-up); that gap is bounded by ``join_group`` policy at
schema-validation time, not by anything a transport-layer header check can
see.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Iterable

from starlette import status
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from querygate.core.config import AppConfig
from querygate.core.logging import get_logger

# Raw structural characters, as byte values, for the pre-parse depth scan.
_OPENERS = frozenset((0x7B, 0x5B))  # { [
_CLOSERS = frozenset((0x7D, 0x5D))  # } ]
_QUOTE = 0x22  # "
_BACKSLASH = 0x5C  # \

_MCP_METHOD_HEADER = b"mcp-method"
_MCP_NAME_HEADER = b"mcp-name"
_MCP_PARAM_CONNECTION_HEADER = b"mcp-param-connection"
_BASE64_SENTINEL_PREFIX = "=?base64?"
_BASE64_SENTINEL_SUFFIX = "?="
_HEADER_MISMATCH_CODE = -32020


def _structural_depth_exceeds(raw: bytes, max_depth: int) -> bool:
    """Return True if ``raw`` nests structural braces/brackets past ``max_depth``.

    A cheap O(n) scan over the raw bytes that tracks structural nesting only —
    characters inside JSON string literals (respecting ``\\`` escapes) are
    skipped, so a string value that merely *contains* brackets can't trip the
    guard. It is a pre-parse safety heuristic, not a JSON validator: it exists
    solely to reject a body deep enough to trip the transport's recursive
    parser before that parser runs. Short-circuits the moment the threshold is
    crossed, so a malicious deep body is rejected after only a few hundred bytes.
    """
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == _BACKSLASH:
                escaped = True
            elif byte == _QUOTE:
                in_string = False
            continue
        if byte == _QUOTE:
            in_string = True
        elif byte in _OPENERS:
            depth += 1
            if depth > max_depth:
                return True
        elif byte in _CLOSERS:
            if depth > 0:
                depth -= 1
    return False


def _decode_sentinel_value(raw: str) -> str | None:
    """Decode the spec's ``=?base64?...?=`` header-value sentinel.

    Returns the decoded value, the original value unchanged if it isn't the
    sentinel shape, or ``None`` if it has the sentinel's markers but the
    payload doesn't actually decode as base64/UTF-8 — a malformed header
    ("contains invalid characters" in the spec's validation-failure list),
    which must fail closed rather than compare against the raw sentinel text.
    """
    if raw.startswith(_BASE64_SENTINEL_PREFIX) and raw.endswith(_BASE64_SENTINEL_SUFFIX):
        payload = raw[len(_BASE64_SENTINEL_PREFIX) : -len(_BASE64_SENTINEL_SUFFIX)]
        try:
            return base64.b64decode(payload, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None
    return raw


def _find_header_values(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> list[str]:
    return [
        value.decode("latin-1") for header_name, value in headers if header_name.lower() == name
    ]


def _clip(value: object, limit: int = 200) -> str:
    """Bound an attacker-controlled value before it's echoed into a message."""
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "...'"


def _header_body_mismatch(headers: Iterable[tuple[bytes, bytes]], raw: bytes) -> str | None:
    """Return a HeaderMismatch message if a present routing header disagrees
    with the parsed body, else ``None``.

    Deliberately validate-if-present (TODO.md item 127, extended by item 130
    to ``Mcp-Param-Connection``): QueryGate does not yet require
    ``Mcp-Method``/``Mcp-Name``/``Mcp-Param-Connection`` (they're undefined
    pre-2026-07-28, item 128), so their absence is not itself a violation.
    But once any is present, the body must be parseable and must agree — a
    hostile or malformed body under a present header fails closed rather than
    being waved through, per the spec's own validation-failure list.
    """
    method_values = _find_header_values(headers, _MCP_METHOD_HEADER)
    name_values = _find_header_values(headers, _MCP_NAME_HEADER)
    param_connection_values = _find_header_values(headers, _MCP_PARAM_CONNECTION_HEADER)
    if not method_values and not name_values and not param_connection_values:
        return None

    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        # ValueError covers json.JSONDecodeError plus the stdlib int/str
        # conversion guard on an absurdly long numeric literal; RecursionError
        # covers a body deep enough to defeat the parser if an operator ever
        # raises mcp_max_request_depth above the interpreter's own limit.
        return "Header mismatch: request body is not valid JSON."
    if not isinstance(body, dict):
        return "Header mismatch: request body is not a single JSON-RPC request object."

    if method_values:
        if len(method_values) > 1:
            return "Header mismatch: Mcp-Method header is repeated."
        decoded = _decode_sentinel_value(method_values[0])
        if decoded is None:
            return "Header mismatch: Mcp-Method header value is malformed."
        if decoded != body.get("method"):
            return (
                f"Header mismatch: Mcp-Method header value {_clip(decoded)} does not "
                f"match body value {_clip(body.get('method'))}"
            )

    if name_values:
        if len(name_values) > 1:
            return "Header mismatch: Mcp-Name header is repeated."
        decoded = _decode_sentinel_value(name_values[0])
        if decoded is None:
            return "Header mismatch: Mcp-Name header value is malformed."
        params = body.get("params")
        method = body.get("method")
        if isinstance(params, dict):
            has_name, has_uri = "name" in params, "uri" in params
            if has_name and has_uri:
                # A body carrying both mirrored fields is ambiguous about
                # which one the header is meant to agree with — reject
                # rather than guess (TODO.md item 127 constraint 2's
                # fail-closed posture, extended to this shape).
                return "Header mismatch: body carries both params.name and params.uri."
            prefer_uri = isinstance(method, str) and method.startswith("resources/")
            body_name = params.get("uri") if prefer_uri and has_uri else params.get("name")
        else:
            body_name = None
        if decoded != body_name:
            return (
                f"Header mismatch: Mcp-Name header value {_clip(decoded)} does not "
                f"match body value {_clip(body_name)}"
            )

    if param_connection_values:
        if len(param_connection_values) > 1:
            return "Header mismatch: Mcp-Param-Connection header is repeated."
        decoded = _decode_sentinel_value(param_connection_values[0])
        if decoded is None:
            return "Header mismatch: Mcp-Param-Connection header value is malformed."
        # Item 130: the annotated parameter always lives at
        # params.arguments.connection for a tools/call body (the only request
        # shape a `connection`-taking tool is ever invoked through) — never at
        # params.connection itself, which is not a JSON-RPC field QueryGate's
        # tools define.
        params = body.get("params")
        arguments = params.get("arguments") if isinstance(params, dict) else None
        body_connection = arguments.get("connection") if isinstance(arguments, dict) else None
        if decoded != body_connection:
            return (
                f"Header mismatch: Mcp-Param-Connection header value {_clip(decoded)} does "
                f"not match body value {_clip(body_connection)}"
            )

    return None


class MCPRequestGuardMiddleware:
    """Reject oversized/over-deep MCP request bodies before the transport parses.

    Wraps the (already auth-wrapped) MCP ASGI app. HTTP requests are buffered up
    to the byte cap; a body that exceeds the cap is a clean 413, and one that
    nests past the depth cap is a clean 400. Anything within both caps is
    replayed downstream unchanged, so the transport sees an identical request.
    Non-HTTP scopes (lifespan, the GET SSE stream, websockets) pass straight
    through untouched.
    """

    def __init__(self, app: ASGIApp, settings: AppConfig) -> None:
        self._app = app
        self._max_bytes = settings.mcp_max_request_bytes
        self._max_depth = settings.mcp_max_request_depth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        # Fast path: reject on a declared Content-Length before reading a byte.
        for name, value in scope.get("headers") or ():
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    break
                if declared > self._max_bytes:
                    await self._reject(
                        scope,
                        send,
                        status.HTTP_413_CONTENT_TOO_LARGE,
                        "PAYLOAD_TOO_LARGE",
                        "Request body exceeds the configured size limit.",
                        path=scope.get("path"),
                    )
                    return
                break

        # Buffer the body, enforcing the cap while streaming (a Content-Length
        # may be absent or untruthful).
        chunks: list[bytes] = []
        total = 0
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.disconnect":
                # Hand the disconnect to the app so it can unwind normally.
                await self._app(scope, _single_message(message), send)
                return
            body = message.get("body", b"")
            if body:
                total += len(body)
                if total > self._max_bytes:
                    await self._reject(
                        scope,
                        send,
                        status.HTTP_413_CONTENT_TOO_LARGE,
                        "PAYLOAD_TOO_LARGE",
                        "Request body exceeds the configured size limit.",
                        path=scope.get("path"),
                    )
                    return
                chunks.append(body)
            more_body = message.get("more_body", False)

        buffered = b"".join(chunks)
        if _structural_depth_exceeds(buffered, self._max_depth):
            await self._reject(
                scope,
                send,
                status.HTTP_400_BAD_REQUEST,
                "MALFORMED_REQUEST",
                "Request body is nested too deeply.",
                path=scope.get("path"),
            )
            return

        # Item 127: only after the depth scan has cleared the body as safe to
        # parse — parsing an attacker-deep body here would reintroduce the
        # RecursionError the scan above exists to prevent.
        mismatch = _header_body_mismatch(scope.get("headers") or (), buffered)
        if mismatch is not None:
            await self._reject(
                scope,
                send,
                status.HTTP_400_BAD_REQUEST,
                _HEADER_MISMATCH_CODE,
                mismatch,
                path=scope.get("path"),
                log_reason="HEADER_MISMATCH",
            )
            return

        await self._app(scope, _replay(buffered, receive), send)

    async def _reject(
        self,
        scope: Scope,
        send: Send,
        status_code: int,
        code: int | str,
        message: str,
        *,
        path: object,
        log_reason: str | None = None,
    ) -> None:
        get_logger().warning("mcp.transport.rejected", reason=log_reason or code, path=path)
        response = JSONResponse(
            content={"error": {"code": code, "message": message}},
            status_code=status_code,
        )
        await response(scope, _no_body_receive, send)


async def _no_body_receive() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


def _single_message(message: Message) -> Receive:
    """A receive channel that yields one already-consumed message, then idles."""
    delivered = False

    async def receive() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return message
        return {"type": "http.disconnect"}

    return receive


def _replay(body: bytes, downstream: Receive) -> Receive:
    """Replay the fully-buffered body once, then defer to the real channel."""
    replayed = False

    async def receive() -> Message:
        nonlocal replayed
        if not replayed:
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await downstream()

    return receive
