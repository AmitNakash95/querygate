"""Shared MCP `tools/call` client for the partner-demo control backend.

Both the baseline (unsafe) MCP server and QueryGate's own MCP server speak
Streamable HTTP and can respond either as plain `application/json` or as
`text/event-stream` (Server-Sent Events) depending on server config — they
are NOT guaranteed to agree (confirmed live: QueryGate wraps its tool result
under `structuredContent.result`, the baseline server puts the tool result
directly under `structuredContent` with no `result` key). This module is the
one place that knows how to talk to either, so no other code in demo/control
special-cases transport shape.

Every call here is a REAL HTTP round trip. Nothing in this module is
simulated, stubbed, or replayed — that is the whole point of the demo
(demo/SPEC.md: "Every /api/run performs a real MCP tools/call").
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import httpx


class MCPTransportError(Exception):
    """Raised when the server could not be reached or returned something
    that isn't a parseable JSON-RPC response at all (connection refused,
    timeout, malformed body). Distinct from a tool-level or protocol-level
    error, which are legitimate MCP responses and are returned normally."""


@dataclass
class MCPCallResult:
    """The outcome of one real MCP `tools/call`, plus its measured latency."""

    request_body: dict
    rpc_response: dict
    elapsed_ms: float
    is_protocol_error: bool  # top-level JSON-RPC "error" (auth, bad method, ...)
    is_tool_error: bool  # tool ran but raised (CallToolResult.isError)
    payload: Any  # the tool's structured result, or the raw error text


def _parse_sse_or_json(response: httpx.Response) -> dict:
    """Parse a Streamable HTTP response body that is either plain JSON or
    Server-Sent Events framing (`data: {...}` lines). Handles both because
    the two demo MCP servers have been observed to differ on this."""
    content_type = response.headers.get("content-type", "")
    text = response.text
    if "text/event-stream" in content_type:
        for line in text.split("\n"):
            line = line.strip("\r")
            if line.startswith("data:"):
                data = line[len("data:") :].strip()
                if not data:
                    continue
                try:
                    return json.loads(data)
                except json.JSONDecodeError as exc:
                    raise MCPTransportError(
                        f"could not parse SSE data frame as JSON: {exc}. Frame: {data[:300]!r}"
                    ) from exc
        raise MCPTransportError(
            f"text/event-stream response had no 'data:' frame. Body (first 300 chars): {text[:300]!r}"
        )
    # Plain JSON (or something claiming to be JSON-ish).
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MCPTransportError(
            f"response was neither JSON nor SSE (content-type={content_type!r}). "
            f"Body (first 300 chars): {text[:300]!r}"
        ) from exc


def _extract_tool_payload(rpc_response: dict) -> tuple[bool, bool, Any]:
    """Return (is_protocol_error, is_tool_error, payload) from a parsed
    JSON-RPC response to a tools/call request."""
    if "error" in rpc_response:
        return True, False, rpc_response["error"]

    result = rpc_response.get("result", {})
    is_tool_error = bool(result.get("isError"))

    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        # QueryGate wraps under structuredContent.result; the baseline server
        # puts the tool's own return value directly under structuredContent.
        # Prefer the "result" key only when present so both shapes work.
        payload = structured.get("result", structured)
        return False, is_tool_error, payload

    # Fall back to the text content block (always present on both servers).
    content = result.get("content") or []
    if content and isinstance(content, list) and "text" in content[0]:
        text = content[0]["text"]
        try:
            return False, is_tool_error, json.loads(text)
        except json.JSONDecodeError:
            return False, is_tool_error, text

    return False, is_tool_error, None


async def _post_rpc(
    client: httpx.AsyncClient,
    mcp_url: str,
    request_body: dict,
    *,
    headers: Optional[Mapping[str, str]],
    timeout: Optional[float],
) -> tuple[dict, float]:
    """Shared POST + parse for one real JSON-RPC round trip. Returns
    (rpc_response, elapsed_ms). Raises MCPTransportError if the server could
    not be reached at all or returned something unparseable — callers must
    surface this as an honest error, never fabricate a result."""
    all_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if headers:
        all_headers.update(headers)

    t0 = time.perf_counter()
    try:
        response = await client.post(
            mcp_url,
            json=request_body,
            headers=all_headers,
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise MCPTransportError(f"could not reach {mcp_url}: {exc}") from exc
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    if response.status_code >= 400:
        raise MCPTransportError(
            f"{mcp_url} returned HTTP {response.status_code}: {response.text[:300]!r}"
        )

    return _parse_sse_or_json(response), elapsed_ms


async def call_tool(
    client: httpx.AsyncClient,
    mcp_url: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    request_id: int = 1,
    headers: Optional[Mapping[str, str]] = None,
    timeout: Optional[float] = None,
) -> MCPCallResult:
    """Perform one real `tools/call` over HTTP and return its parsed result.

    Raises MCPTransportError if the server could not be reached at all or
    returned something unparseable — callers must surface this as an honest
    error, never fabricate a result.
    """
    request_body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": dict(arguments)},
    }
    rpc_response, elapsed_ms = await _post_rpc(
        client, mcp_url, request_body, headers=headers, timeout=timeout
    )
    is_protocol_error, is_tool_error, payload = _extract_tool_payload(rpc_response)

    return MCPCallResult(
        request_body=request_body,
        rpc_response=rpc_response,
        elapsed_ms=elapsed_ms,
        is_protocol_error=is_protocol_error,
        is_tool_error=is_tool_error,
        payload=payload,
    )


async def list_tools(
    client: httpx.AsyncClient,
    mcp_url: str,
    *,
    request_id: int = 1,
    headers: Optional[Mapping[str, str]] = None,
    timeout: Optional[float] = None,
) -> MCPCallResult:
    """Perform a real `tools/list` JSON-RPC call — protocol-level only, opens
    no database connection.

    Exists so a liveness check can confirm the server is answering MCP
    requests without touching Postgres. Before this, `/api/health`'s
    baseline-server check called `tools/call list_tables`, which the
    baseline server serves by running a real `information_schema` query
    as `agent_ro` — the exact role the "queries executed: 0" headline
    counts. The browser polls `/api/health` every 4 seconds, so that one
    call alone kept incrementing the counter with nothing else running
    (demo/SPEC.md F1). `demo/Makefile.include`'s `pitch-wait` target
    already probes port 8811 with a bare `tools/list`; this mirrors it.
    """
    request_body = {"jsonrpc": "2.0", "id": request_id, "method": "tools/list"}
    rpc_response, elapsed_ms = await _post_rpc(
        client, mcp_url, request_body, headers=headers, timeout=timeout
    )
    is_protocol_error = "error" in rpc_response

    return MCPCallResult(
        request_body=request_body,
        rpc_response=rpc_response,
        elapsed_ms=elapsed_ms,
        is_protocol_error=is_protocol_error,
        is_tool_error=False,
        payload=rpc_response.get("result", rpc_response.get("error")),
    )
