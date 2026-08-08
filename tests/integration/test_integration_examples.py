"""Model-free verification of the agent-framework integration examples (item 52).

The examples under `examples/*_integration.py` are runnable references, not a
CI-exercised code path — the live model call and the third-party framework glue
are the user's to run. But two things about them *are* QueryGate-owned and must
not silently drift:

1. Every QueryGate MCP tool an example wires up (`QUERYGATE_TOOLS`) must still be
   a tool the MCP server actually registers. Renaming or removing a tool without
   updating the examples would ship a broken quick-start.
2. The OpenAI example's `mcp_tool_to_openai_function` bridge must turn QueryGate's
   *real* MCP tool schemas into well-formed OpenAI function-calling tool schemas.

Both are checked here against the live MCP server, with no framework installed
and no model call. The example modules import their frameworks lazily (inside
`main()`), so importing them to read `QUERYGATE_TOOLS` needs nothing extra.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import types

import pytest
import pytest_asyncio
import uvicorn
from httpx import ASGITransport, AsyncClient

from examples import (
    langchain_integration,
    llamaindex_integration,
    openai_function_calling_integration as openai_integration,
)
from querygate.api.app import create_app
from querygate.core.config import AppConfig

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"
_HEADERS_JSON = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
_TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


def _reset_mcp_session_manager() -> None:
    from querygate.mcp.server import mcp_server

    mcp_server._session_manager = None  # type: ignore[assignment]


def _parse_mcp_response(resp) -> dict:
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise ValueError(f"No data line found in SSE response: {resp.text!r}")


@pytest_asyncio.fixture
async def mcp_tools():
    """The MCP server's live `tools/list` result (dev-bypass, no scopes)."""
    _reset_mcp_session_manager()
    settings = AppConfig(
        environment="localhost",
        mcp_enabled=True,
        mcp_api_keys=[],
        mcp_mount_path="/mcp",
        audit_sink_backend="none",
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url=_BASE_URL, follow_redirects=True
        ) as client,
    ):
        resp = await client.post("/mcp/", json=_TOOLS_LIST, headers=_HEADERS_JSON)
        assert resp.status_code == 200
        yield _parse_mcp_response(resp)["result"]["tools"]


@pytest.mark.parametrize(
    "example",
    [langchain_integration, llamaindex_integration, openai_integration],
    ids=["langchain", "llamaindex", "openai"],
)
@pytest.mark.asyncio
async def test_example_tool_names_are_all_registered(example, mcp_tools):
    """Each example only wires up MCP tools the server actually exposes."""
    registered = {tool["name"] for tool in mcp_tools}
    referenced = set(example.QUERYGATE_TOOLS)
    assert referenced, f"{example.__name__} declares no QUERYGATE_TOOLS"
    missing = referenced - registered
    assert (
        not missing
    ), f"{example.__name__} references MCP tools that no longer exist: {sorted(missing)}"


@pytest.mark.asyncio
async def test_openai_bridge_converts_real_tool_schemas(mcp_tools):
    """The MCP -> OpenAI function bridge produces valid schemas for real tools."""
    allowed = set(openai_integration.QUERYGATE_TOOLS)
    converted = [
        openai_integration.mcp_tool_to_openai_function(
            tool["name"], tool.get("description"), tool.get("inputSchema")
        )
        for tool in mcp_tools
        if tool["name"] in allowed
    ]
    assert converted, "no allow-listed tools were found to convert"

    for fn in converted:
        assert fn["type"] == "function"
        spec = fn["function"]
        # Required by the OpenAI function-calling schema.
        assert isinstance(spec["name"], str) and spec["name"]
        assert isinstance(spec["description"], str)  # "" is allowed, None is not
        assert isinstance(spec["parameters"], dict)
        # QueryGate's tool inputs are always JSON-Schema objects.
        assert spec["parameters"].get("type") == "object"
        # The whole schema must be JSON-serializable (what the OpenAI SDK sends).
        json.dumps(fn)


def test_openai_bridge_handles_a_tool_with_no_input_schema():
    """A no-argument tool becomes a valid empty-object parameter schema."""
    fn = openai_integration.mcp_tool_to_openai_function("noop", None, None)
    assert fn["function"]["parameters"] == {"type": "object", "properties": {}}
    assert fn["function"]["description"] == ""


# --- The OpenAI example's runtime path, exercised for real (no model key) ------
#
# The bridge helper above is pure. These two tests cover the rest of the OpenAI
# example's non-model logic: its MCP discovery/dispatch against a *real* running
# server (the LangChain/LlamaIndex examples can't be run without their
# frameworks + a key, but this one's transport is the bundled `mcp` client SDK),
# and its chat loop's message/tool-dispatch mechanics against a scripted client.


def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


@pytest.fixture
def live_mcp_server():
    """A real uvicorn server (own thread + event loop) serving QueryGate's MCP
    endpoint on a loopback port — so the `mcp` client SDK can connect over real
    HTTP, exactly as the example does. The process-global demo registry set by
    the autouse conftest fixture is shared with the server thread."""
    _reset_mcp_session_manager()
    port = _free_port()
    settings = AppConfig(
        environment="localhost",
        mcp_enabled=True,
        mcp_api_keys=[],
        mcp_mount_path="/mcp",
        audit_sink_backend="none",
    )
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        pytest.fail("live MCP server did not start")
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_openai_example_mcp_path_works_against_a_live_server(live_mcp_server):
    """The example's own discovery + a real tool dispatch, over real HTTP."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with streamable_http_client(live_mcp_server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # The example's discover_openai_tools(), against a real server.
            tools = await openai_integration.discover_openai_tools(session)
            names = {fn["function"]["name"] for fn in tools}
            assert names == set(openai_integration.QUERYGATE_TOOLS)
            for fn in tools:
                assert fn["type"] == "function"
                assert fn["function"]["parameters"].get("type") == "object"

            # A real tool dispatch — list_connections needs no database, and the
            # autouse fixture registers a "demo" connection.
            result = await session.call_tool("list_connections", {})
            text = openai_integration._text_from_tool_result(result)
            assert "demo" in text


# --- Scripted doubles for the chat-loop mechanics test -------------------------


class _FakeMessage:
    def __init__(self, *, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []

    def model_dump(self, exclude_none=False):
        return {"role": "assistant", "content": self.content}


def _tool_call(call_id: str, name: str, arguments: str):
    return types.SimpleNamespace(
        id=call_id, function=types.SimpleNamespace(name=name, arguments=arguments)
    )


class _ScriptedOpenAI:
    """Duck-typed stand-in for `OpenAI()`; returns each scripted message in turn."""

    def __init__(self, messages):
        self._script = list(messages)
        self.calls = []
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        message = self._script.pop(0)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class _FakeSession:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        block = types.SimpleNamespace(type="text", text=f"result-for-{name}")
        return types.SimpleNamespace(content=[block])


@pytest.mark.asyncio
async def test_openai_tool_calling_loop_dispatches_and_returns_final_answer():
    """The loop dispatches a tool call to the MCP session, feeds the result
    back with the right tool_call_id, and returns the model's final text."""
    client = _ScriptedOpenAI(
        [
            _FakeMessage(tool_calls=[_tool_call("call_1", "list_connections", '{"a": 1}')]),
            _FakeMessage(content="here is your answer"),
        ]
    )
    session = _FakeSession()
    messages = [{"role": "user", "content": "q"}]

    answer = await openai_integration.run_tool_calling_loop(client, session, messages, [])

    assert answer == "here is your answer"
    assert session.calls == [("list_connections", {"a": 1})]
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    assert tool_messages == [
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "result-for-list_connections",
        }
    ]
    assert len(client.calls) == 2  # one tool round, one final answer
