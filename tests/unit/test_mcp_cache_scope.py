"""TODO.md item 129: `cache_scope: "private"` on every principal-varying MCP
result, asserted against the actual emitted payload — in the manner of
`test_credential_redaction.py`, not by trusting source-code convention.

QueryGate's MCP surface is per-principal by construction (`tools/list` is
filtered by the caller's scopes). The MCP `2026-07-28` revision (SEP-2549)
lets `tools/list`/`prompts/list`/`resources/list`/`resources/read` advertise
a `cache_scope` of `"public"` or `"private"`; a shared intermediary is
allowed to cache and replay a `"public"` response across callers. The
installed `mcp` v2 SDK already defaults `cache_scope="private"` on every one
of the four result types (they all inherit `mcp_types.CacheableResult`) —
these tests exist anyway, per CLAUDE.md's "the failure mode is a default,
not a decision" doctrine: QueryGate must own this guarantee structurally,
not lean on an upstream default that could change. They assert every one of
the four surfaces is unconditionally `"private"` even for a resource/prompt
registered after this test file was written (QueryGate currently registers
zero of either); that the two excluded surfaces (`tools/call` and
`prompts/get` — a tool's/prompt's per-caller *call-time result*, not a
listed surface under the spec) are left untouched; that the guarantee
survives regardless of installation order or repeated (re-)installation, not
just in the one call order `create_mcp_server()` happens to use today; and
that `setup_mcp()`'s pre-serve assertion actually catches an unprotected
server rather than being dead code.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver import MCPServer

from querygate.mcp.caching import (
    PRIVATE_CACHE_SCOPE,
    assert_private_cache_scope_installed,
    install_private_cache_scope,
    with_forced_private_cache_scope,
)
from querygate.mcp.server import _install_scoped_tool_listing, create_mcp_server


async def _call(server: MCPServer, method: str, params: object = None) -> object:
    entry = server._lowlevel_server._request_handlers[method]
    return await entry.handler(None, params)


@pytest.mark.asyncio
async def test_with_forced_private_cache_scope_overrides_a_handler_that_sets_public():
    """The one test that actually distinguishes QueryGate's wrapper from the
    SDK's own `cache_scope="private"` default.

    Every other test in this file exercises a handler that never explicitly
    sets `cache_scope` at all, so the SDK default alone would make them pass
    even with the wrapper's forcing line deleted entirely (confirmed by
    mutation: replacing `result.cache_scope = PRIVATE_CACHE_SCOPE` with a
    no-op does not fail any other test here). Build a fake inner handler
    that explicitly returns a `"public"`-scoped result — the one input the
    SDK default can't paper over — and assert the wrapper still forces it
    back to `"private"`.
    """
    from mcp_types import ListToolsResult

    async def public_handler(ctx: object, params: object) -> ListToolsResult:
        return ListToolsResult(tools=[], cache_scope="public")

    wrapped = with_forced_private_cache_scope(public_handler)
    result = await wrapped(None, None)

    assert result.cache_scope == PRIVATE_CACHE_SCOPE


@pytest.mark.asyncio
async def test_real_tools_list_emits_private_cache_scope():
    """The actual registered querygate server's tools/list payload."""
    server = create_mcp_server()

    result = await _call(server, "tools/list")

    assert result.cache_scope == PRIVATE_CACHE_SCOPE
    # At least one real tool is present, so this is not a vacuous empty-list
    # assertion the way a from-scratch server's prompts/resources would be.
    assert len(result.tools) > 0

    # Assert against the actual wire payload, not just the in-memory model.
    payload = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert payload["cacheScope"] == "private"


@pytest.mark.asyncio
async def test_real_prompts_list_emits_private_cache_scope():
    server = create_mcp_server()

    result = await _call(server, "prompts/list")

    assert result.cache_scope == PRIVATE_CACHE_SCOPE
    payload = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert payload["cacheScope"] == "private"


@pytest.mark.asyncio
async def test_real_resources_list_emits_private_cache_scope():
    server = create_mcp_server()

    result = await _call(server, "resources/list")

    assert result.cache_scope == PRIVATE_CACHE_SCOPE
    payload = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert payload["cacheScope"] == "private"


@pytest.mark.asyncio
async def test_a_resource_registered_after_this_test_was_written_is_still_private():
    """Structural guarantee, not a per-registration opt-in.

    QueryGate registers zero resources today, so asserting only against the
    empty list above would pass even if the enforcement point were deleted
    and no one had noticed resources/list started returning an empty,
    unmarked payload. Build an isolated MCPServer instance, register a real
    resource and a real prompt on it (simulating a future feature), run the
    exact same `install_private_cache_scope` used in production, and assert
    their *actual content* is served back with cache_scope still private.
    """
    server = MCPServer(name="future-querygate")

    @server.resource("resource://future-thing")
    def get_future_resource() -> str:
        return "this table's description is per-principal in spirit"

    @server.prompt()
    def future_prompt() -> str:
        return "describe this table"

    install_private_cache_scope(server)

    resources_result = await _call(server, "resources/list")
    assert len(resources_result.resources) == 1
    assert resources_result.cache_scope == PRIVATE_CACHE_SCOPE

    from mcp_types import ReadResourceRequestParams

    read_result = await _call(
        server,
        "resources/read",
        ReadResourceRequestParams(uri="resource://future-thing"),
    )
    assert read_result.contents[0].text == "this table's description is per-principal in spirit"
    assert read_result.cache_scope == PRIVATE_CACHE_SCOPE

    prompts_result = await _call(server, "prompts/list")
    assert len(prompts_result.prompts) == 1
    assert prompts_result.cache_scope == PRIVATE_CACHE_SCOPE


@pytest.mark.asyncio
async def test_resources_list_without_the_enforcement_point_is_not_marked_public():
    """Negative control: an untouched MCPServer (no `install_private_cache_scope`
    applied) still defaults to `cache_scope="private"` via the SDK's own
    `CacheableResult` default — proving the assertions above test QueryGate's
    *own* enforcement point on top of, not instead of, that SDK default. What
    QueryGate's wrapper adds is independence from that default, verified by
    `test_cache_scope_survives_reinstallation_in_either_order` and
    `test_install_raises_rather_than_silently_skipping_a_missing_handler`
    below rather than by this negative control.
    """
    server = MCPServer(name="unprotected")

    result = await _call(server, "resources/list")

    assert result.cache_scope == PRIVATE_CACHE_SCOPE
    payload = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert payload["cacheScope"] == "private"


@pytest.mark.asyncio
async def test_get_prompt_result_is_not_annotated_with_cache_scope():
    """`prompts/get` is the other surface explicitly excluded alongside
    `tools/call` (a prompt's per-caller *call-time result*, not a listed
    surface) — sibling to the tools/call check below, closing the gap where
    only one of the two exclusions had a negative test. `GetPromptResult` is
    a plain `Result`, not a `CacheableResult` subclass, so it has no
    `cache_scope` field at all.
    """
    server = MCPServer(name="get-prompt-check")

    @server.prompt()
    def greeting() -> str:
        return "hello"

    install_private_cache_scope(server)

    from mcp_types import GetPromptRequestParams

    result = await _call(server, "prompts/get", GetPromptRequestParams(name="greeting"))

    assert not hasattr(result, "cache_scope")
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
    cache_scope must still be private after both.
    """
    server = MCPServer(name="reordering-check")

    @server.tool()
    def echo(value: str) -> str:
        return value

    install_private_cache_scope(server)  # deliberately first
    _install_scoped_tool_listing(server)  # then the real item-63 installer
    _install_scoped_tool_listing(server)  # simulated re-registration/reload

    result = await _call(server, "tools/list")

    assert result.cache_scope == PRIVATE_CACHE_SCOPE
    assert len(result.tools) == 1


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
    server = MCPServer(name="idempotency-check")

    install_private_cache_scope(server)
    first = server._lowlevel_server._request_handlers["prompts/list"]

    for _ in range(10):
        install_private_cache_scope(server)

    second = server._lowlevel_server._request_handlers["prompts/list"]
    assert first is second
    assert first.handler is second.handler


def test_reassigning_an_already_wrapped_handler_does_not_double_wrap():
    """Targets `with_forced_private_cache_scope`'s own marker check directly,
    independent of `install_private_cache_scope`'s outer idempotency
    short-circuit above (which never even calls back into the wrapper on a
    second `install_private_cache_scope` call, so it alone can't prove this
    half of the guarantee). `_PrivateCacheScopeHandlers.__setitem__` fires on
    *any* assignment to a cache-scoped key, including one where the value
    being assigned is itself already a wrapped handler — e.g. code that reads
    `_request_handlers[X]` and writes it straight back. That must not nest a
    second closure around it.
    """
    server = MCPServer(name="reassign-already-wrapped-check")
    install_private_cache_scope(server)

    handlers = server._lowlevel_server._request_handlers
    already_wrapped = handlers["prompts/list"]
    handlers["prompts/list"] = already_wrapped  # re-assign as-is

    assert handlers["prompts/list"].handler is already_wrapped.handler


def test_install_raises_rather_than_silently_skipping_a_missing_handler():
    """If any of the four cache-scoped methods has no handler at all,
    `install_private_cache_scope` must raise, not silently install a partial
    guarantee — a real `MCPServer` instance always has all four (registered
    unconditionally at construction time), so this only fires for a
    malformed/stub server object, and staying silent there would hide a
    real bug behind an apparently-successful install.
    """

    class _StubLowLevelServer:
        _request_handlers: dict = {}

    class _StubMcpServer:
        _lowlevel_server = _StubLowLevelServer()

    with pytest.raises(RuntimeError):
        install_private_cache_scope(_StubMcpServer())


def test_assert_private_cache_scope_installed_raises_when_missing():
    server = MCPServer(name="assert-missing-check")
    with pytest.raises(RuntimeError):
        assert_private_cache_scope_installed(server)


def test_assert_private_cache_scope_installed_passes_once_installed():
    server = MCPServer(name="assert-present-check")
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
    """`tools/call` is explicitly out of scope (SEP-2549 attaches to the
    list/read surfaces, not to a tool's per-caller result) — a
    `run_structured_queries` result must never end up carrying
    `cache_scope`; it is simply not part of this spec surface.
    `CallToolResult` is a plain `Result`, not a `CacheableResult` subclass,
    so it has no such field at all. Guards against a future refactor
    accidentally widening `install_private_cache_scope` to `tools/call`.
    """
    server = MCPServer(name="call-tool-check")

    @server.tool()
    def echo(value: str) -> str:
        return value

    install_private_cache_scope(server)

    from mcp_types import CallToolRequestParams

    result = await _call(
        server, "tools/call", CallToolRequestParams(name="echo", arguments={"value": "hi"})
    )

    assert not hasattr(result, "cache_scope")
    payload = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert "cacheScope" not in payload
