"""TODO.md item 129: `cacheScope: "private"` on every principal-varying MCP
result, asserted against the actual emitted payload — in the manner of
`test_credential_redaction.py`, not by trusting source-code convention.

QueryGate's MCP surface is per-principal by construction (`tools/list` is
filtered by the caller's scopes). The MCP `2026-07-28` revision (SEP-2549)
lets `tools/list`/`prompts/list`/`resources/list`/`resources/read` advertise
a `cacheScope` of `"public"` or `"private"`; a shared intermediary is allowed
to cache and replay a `"public"` response across callers. If any of those
four surfaces were ever left unmarked or marked `"public"`, a caching
gateway could serve one principal's visible tool surface to another. These
tests assert every one of the four surfaces is unconditionally `"private"`,
including for a resource/prompt registered after this test file was written
(query gate currently registers zero of either); that the two excluded
surfaces (`tools/call` and `prompts/get` — a tool's/prompt's per-caller
*call-time result*, not a listed surface under the spec) are left
untouched; that the guarantee survives regardless of installation order or
repeated (re-)installation, not just in the one call order
`create_mcp_server()` happens to use today; and that `setup_mcp()`'s
pre-serve assertion actually catches an unprotected server rather than
being dead code.
"""

from __future__ import annotations

import pytest
from mcp import types as mcp_types
from mcp.server.fastmcp import FastMCP

from querygate.mcp.caching import (
    PRIVATE_CACHE_SCOPE,
    assert_private_cache_scope_installed,
    install_private_cache_scope,
)
from querygate.mcp.server import _install_scoped_tool_listing, create_mcp_server


def _server_result_root(server_result: mcp_types.ServerResult):
    return server_result.root


@pytest.mark.asyncio
async def test_real_tools_list_emits_private_cache_scope():
    """The actual registered querygate server's tools/list payload."""
    server = create_mcp_server()
    handler = server._mcp_server.request_handlers[mcp_types.ListToolsRequest]

    result = await handler(mcp_types.ListToolsRequest(method="tools/list"))
    root = _server_result_root(result)

    assert root.cacheScope == PRIVATE_CACHE_SCOPE
    # At least one real tool is present, so this is not a vacuous empty-list
    # assertion the way a from-scratch server's prompts/resources would be.
    assert len(root.tools) > 0

    # Assert against the actual wire payload, not just the in-memory model.
    payload = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert payload["cacheScope"] == "private"


@pytest.mark.asyncio
async def test_real_prompts_list_emits_private_cache_scope():
    server = create_mcp_server()
    handler = server._mcp_server.request_handlers[mcp_types.ListPromptsRequest]

    result = await handler(mcp_types.ListPromptsRequest(method="prompts/list"))
    root = _server_result_root(result)

    assert root.cacheScope == PRIVATE_CACHE_SCOPE
    payload = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert payload["cacheScope"] == "private"


@pytest.mark.asyncio
async def test_real_resources_list_emits_private_cache_scope():
    server = create_mcp_server()
    handler = server._mcp_server.request_handlers[mcp_types.ListResourcesRequest]

    result = await handler(mcp_types.ListResourcesRequest(method="resources/list"))
    root = _server_result_root(result)

    assert root.cacheScope == PRIVATE_CACHE_SCOPE
    payload = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert payload["cacheScope"] == "private"


@pytest.mark.asyncio
async def test_a_resource_registered_after_this_test_was_written_is_still_private():
    """Structural guarantee, not a per-registration opt-in.

    QueryGate registers zero resources today, so asserting only against the
    empty list above would pass even if the enforcement point were deleted
    and no one had noticed resources/list started returning an empty,
    unmarked payload. Build an isolated FastMCP instance, register a real
    resource and a real prompt on it (simulating a future feature), run the
    exact same `install_private_cache_scope` used in production, and assert
    their *actual content* is served back with cacheScope still private.
    """
    server = FastMCP(name="future-querygate")

    @server.resource("resource://future-thing")
    def get_future_resource() -> str:
        return "this table's description is per-principal in spirit"

    @server.prompt()
    def future_prompt() -> str:
        return "describe this table"

    install_private_cache_scope(server)

    resources_handler = server._mcp_server.request_handlers[mcp_types.ListResourcesRequest]
    resources_result = await resources_handler(
        mcp_types.ListResourcesRequest(method="resources/list")
    )
    resources_root = _server_result_root(resources_result)
    assert len(resources_root.resources) == 1
    assert resources_root.cacheScope == PRIVATE_CACHE_SCOPE

    read_handler = server._mcp_server.request_handlers[mcp_types.ReadResourceRequest]
    read_result = await read_handler(
        mcp_types.ReadResourceRequest(
            method="resources/read",
            params=mcp_types.ReadResourceRequestParams(uri="resource://future-thing"),
        )
    )
    read_root = _server_result_root(read_result)
    assert read_root.contents[0].text == "this table's description is per-principal in spirit"
    assert read_root.cacheScope == PRIVATE_CACHE_SCOPE

    prompts_handler = server._mcp_server.request_handlers[mcp_types.ListPromptsRequest]
    prompts_result = await prompts_handler(mcp_types.ListPromptsRequest(method="prompts/list"))
    prompts_root = _server_result_root(prompts_result)
    assert len(prompts_root.prompts) == 1
    assert prompts_root.cacheScope == PRIVATE_CACHE_SCOPE


@pytest.mark.asyncio
async def test_resources_list_without_the_enforcement_point_is_not_marked():
    """Negative control: an untouched FastMCP server (no `install_private_cache_scope`
    applied) does NOT carry cacheScope — proving the assertions above test the
    enforcement point QueryGate adds, not a default the SDK already provides.
    """
    server = FastMCP(name="unprotected")
    handler = server._mcp_server.request_handlers[mcp_types.ListResourcesRequest]

    result = await handler(mcp_types.ListResourcesRequest(method="resources/list"))
    root = _server_result_root(result)

    assert getattr(root, "cacheScope", None) is None
    payload = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert "cacheScope" not in payload


@pytest.mark.asyncio
async def test_get_prompt_result_is_not_annotated_with_cache_scope():
    """`prompts/get` is the other surface explicitly excluded alongside
    `tools/call` (a prompt's per-caller *call-time result*, not a listed
    surface) — sibling to the tools/call check above, closing the gap where
    only one of the two exclusions had a negative test.
    """
    server = FastMCP(name="get-prompt-check")

    @server.prompt()
    def greeting() -> str:
        return "hello"

    install_private_cache_scope(server)

    get_prompt_handler = server._mcp_server.request_handlers[mcp_types.GetPromptRequest]
    result = await get_prompt_handler(
        mcp_types.GetPromptRequest(
            method="prompts/get",
            params=mcp_types.GetPromptRequestParams(name="greeting"),
        )
    )
    root = _server_result_root(result)

    assert getattr(root, "cacheScope", None) is None
    payload = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert "cacheScope" not in payload


@pytest.mark.asyncio
async def test_cache_scope_survives_reinstallation_in_either_order():
    """Ordering-independence, not ordering-discipline.

    A naive fix (wrap whichever handler happens to be registered right now)
    only works if `install_private_cache_scope` always runs LAST, after
    every other installer (e.g. `_install_scoped_tool_listing`) — a
    convention enforced only by a comment, easy to invert by accident, and
    invisible to every other test in this file since they all go through
    the correctly-ordered `create_mcp_server()`. Prove the actual
    implementation does not depend on getting that order right: install
    cache-scope enforcement FIRST (reversed from production order), then
    run the real `_install_scoped_tool_listing` afterward, then re-run it a
    second time (simulating a hypothetical future config-reload path that
    re-registers tools/list without re-running cache-scope installation) —
    cacheScope must still be private after both.
    """
    server = FastMCP(name="reordering-check")

    @server.tool()
    def echo(value: str) -> str:
        return value

    install_private_cache_scope(server)  # deliberately first
    _install_scoped_tool_listing(server)  # then the real item-63 installer
    _install_scoped_tool_listing(server)  # simulated re-registration/reload

    handler = server._mcp_server.request_handlers[mcp_types.ListToolsRequest]
    result = await handler(mcp_types.ListToolsRequest(method="tools/list"))
    root = _server_result_root(result)

    assert root.cacheScope == PRIVATE_CACHE_SCOPE
    assert len(root.tools) == 1


@pytest.mark.asyncio
async def test_install_private_cache_scope_is_idempotent_no_wrapper_growth():
    """Repeated installation (e.g. `create_mcp_server()` invoked more than
    once against the shared `mcp_server` singleton, as several unit tests in
    this suite do) must not stack a new wrapper closure around
    prompts/list, resources/list, or resources/read on every call —
    otherwise a long pytest session would grow an unbounded chain. Proven by
    object identity: a second (and eleventh) install must return the exact
    same handler object, not a fresh wrapper around the previous one.
    """
    server = FastMCP(name="idempotency-check")

    install_private_cache_scope(server)
    first = server._mcp_server.request_handlers[mcp_types.ListPromptsRequest]

    for _ in range(10):
        install_private_cache_scope(server)

    second = server._mcp_server.request_handlers[mcp_types.ListPromptsRequest]
    assert first is second


def test_reassigning_an_already_wrapped_handler_does_not_double_wrap():
    """Targets `with_forced_private_cache_scope`'s own marker check directly,
    independent of `install_private_cache_scope`'s outer idempotency
    short-circuit above (which never even calls back into the wrapper on a
    second `install_private_cache_scope` call, so it alone can't prove this
    half of the guarantee). `_PrivateCacheScopeHandlers.__setitem__` fires on
    *any* assignment to a cache-scoped key, including one where the value
    being assigned is itself already a wrapped handler — e.g. code that reads
    `request_handlers[X]` and writes it straight back. That must not nest a
    second closure around it.
    """
    server = FastMCP(name="reassign-already-wrapped-check")
    install_private_cache_scope(server)

    handlers = server._mcp_server.request_handlers
    already_wrapped = handlers[mcp_types.ListPromptsRequest]
    handlers[mcp_types.ListPromptsRequest] = already_wrapped  # re-assign as-is

    assert handlers[mcp_types.ListPromptsRequest] is already_wrapped


def test_install_raises_rather_than_silently_skipping_a_missing_handler():
    """If any of the four cache-scoped request types has no handler at all,
    `install_private_cache_scope` must raise, not silently install a partial
    guarantee — a real `FastMCP` instance always has all four (registered
    unconditionally at construction time), so this only fires for a
    malformed/stub server object, and staying silent there would hide a
    real bug behind an apparently-successful install.
    """

    class _StubLowLevelServer:
        request_handlers: dict = {}

    class _StubMcpServer:
        _mcp_server = _StubLowLevelServer()

    with pytest.raises(RuntimeError):
        install_private_cache_scope(_StubMcpServer())


def test_assert_private_cache_scope_installed_raises_when_missing():
    server = FastMCP(name="assert-missing-check")
    with pytest.raises(RuntimeError):
        assert_private_cache_scope_installed(server)


def test_assert_private_cache_scope_installed_passes_once_installed():
    server = FastMCP(name="assert-present-check")
    install_private_cache_scope(server)
    assert_private_cache_scope_installed(server)  # must not raise


def test_the_real_server_passes_the_pre_serve_assertion():
    """`setup_mcp()` calls this right before mounting the ASGI app; prove the
    real, fully-wired `create_mcp_server()` singleton actually satisfies it,
    not just an isolated instance built by hand in the tests above."""
    server = create_mcp_server()
    assert_private_cache_scope_installed(server)  # must not raise


@pytest.mark.asyncio
async def test_tools_call_result_is_not_annotated_with_cache_scope():
    """`tools/call` is explicitly out of scope (SEP-2549 attaches to the list/read
    surfaces, not to a tool's per-caller result) — a `run_structured_queries`
    result must never end up carrying `cacheScope: "public"` OR `"private"`;
    it is simply not part of this spec surface. Guards against a future
    refactor accidentally widening `install_private_cache_scope` to CallToolRequest.
    """
    server = FastMCP(name="call-tool-check")

    @server.tool()
    def echo(value: str) -> str:
        return value

    install_private_cache_scope(server)

    call_handler = server._mcp_server.request_handlers[mcp_types.CallToolRequest]
    result = await call_handler(
        mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(name="echo", arguments={"value": "hi"}),
        )
    )
    root = _server_result_root(result)

    assert getattr(root, "cacheScope", None) is None
    payload = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert "cacheScope" not in payload
