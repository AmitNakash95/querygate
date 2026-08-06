"""MCP server factory and ASGI integration.

Built on `mcp` SDK v2 (TODO.md item 128 — the `2026-07-28` protocol
revision). `MCPServer` (formerly `FastMCP`) no longer takes transport
settings in its constructor — those move to `streamable_http_app()`/`run()`
— and the low-level tool-registration decorator API `_install_scoped_tool_listing`
used in v1 is gone; `list_tools` is now a plain overridable instance method
(see that function's own docstring for why a direct attribute override is
the correct v2-idiomatic replacement, not a hack).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from querygate.core.logging import get_logger
from querygate.core.scopes import ADMIN_CONFIG_READ_SCOPE
from querygate.mcp.caching import (
    assert_private_cache_scope_installed,
    install_private_cache_scope,
)
from querygate.mcp.instructions import MCP_INSTRUCTIONS

if TYPE_CHECKING:
    from fastapi import FastAPI

    from querygate.core.auth import Principal
    from querygate.core.config import AppConfig

# TODO.md item 63: tool name -> scope required to see it in tools/list.
# Visibility only — the actual authorization boundary is each tool's own
# call-time scope check (e.g. help/service.py's redacted_configuration);
# this dict must never become a substitute for that check.
_SCOPE_GATED_TOOLS: dict[str, str] = {
    "inspect_querygate_configuration": ADMIN_CONFIG_READ_SCOPE,
}

mcp_server: MCPServer = MCPServer(
    name="querygate",
    instructions=MCP_INSTRUCTIONS,
)


def _current_principal_or_none() -> Optional["Principal"]:
    from querygate.mcp.auth import MCPAuthenticationError, get_mcp_caller

    try:
        return get_mcp_caller()
    except MCPAuthenticationError:
        # No request context (e.g. a direct/offline call, not a real MCP
        # session) — fail open on visibility only. Every scope-gated tool
        # still enforces its own check at call time regardless.
        return None


def _install_scoped_tool_listing(server: MCPServer) -> None:
    """Filter tools/list to what the caller's scopes can actually call.

    Token-savings/defense-in-depth only: an admin-only tool's ~6KB schema
    stops being sent to sessions that can never call it. The real
    authorization boundary stays each tool's own call-time scope check
    (e.g. help/service.py's redacted_configuration) — this must never be
    the only thing standing between a caller and a scope-gated tool.

    v1's `FastMCP` exposed a decorator-based low-level registration
    (`server._mcp_server.list_tools()(fn)`) to install this; v2's
    `MCPServer.list_tools` is a plain overridable `async def` method — both
    `MCPServer.list_tools()` (called by any direct API consumer) and the
    dispatcher's own `_handle_list_tools` (`return ListToolsResult(tools=await
    self.list_tools())`) read the SAME bound attribute, so a plain instance-
    attribute assignment is the correct, minimal v2-idiomatic replacement —
    verified directly against the installed SDK, not assumed: overriding
    `server.list_tools` this way is observed by `_handle_list_tools` too.

    Idempotent by construction (guarded by `_scope_filter_installed`):
    `create_mcp_server()` runs once per `setup_mcp()`/`create_app()` call,
    and `create_app()` legitimately runs more than once in the same process
    (every test in this suite; any production hot-reload/multi-instantiation
    path). Without the guard, each call would close over the *current*
    `server.list_tools` — already `scoped_list_tools` from a prior call, not
    the original method — and wrap it again, growing an unbounded closure
    chain and adding one extra async hop per `tools/list` request per call.
    """
    if getattr(server, "_scope_filter_installed", False):
        return
    unfiltered_list_tools = server.list_tools

    async def scoped_list_tools():
        tools = await unfiltered_list_tools()
        principal = _current_principal_or_none()
        if principal is None:
            return tools
        return [
            tool
            for tool in tools
            if _SCOPE_GATED_TOOLS.get(tool.name) is None
            or _SCOPE_GATED_TOOLS[tool.name] in principal.scopes
        ]

    server.list_tools = scoped_list_tools
    server._scope_filter_installed = True


def create_mcp_server() -> MCPServer:
    """Import tool modules and return the shared MCPServer instance."""
    from querygate.mcp.tools import discover_and_register_tools

    discover_and_register_tools()
    _install_scoped_tool_listing(mcp_server)
    # TODO.md item 129: tools/list, prompts/list, resources/list, and
    # resources/read all vary by caller (scoped tool visibility today; any
    # future resource/prompt tomorrow). Installs a self-enforcing handler
    # dict (`_PrivateCacheScopeHandlers`), so — unlike a one-shot wrap of
    # whatever's currently registered — this is NOT order-dependent on
    # running after `_install_scoped_tool_listing`: any handler installed
    # for these four request types, now or later, gets wrapped at write
    # time regardless of call order. `setup_mcp` below asserts this is
    # actually installed before the app is ever mounted.
    install_private_cache_scope(mcp_server)
    return mcp_server


def setup_mcp(app: "FastAPI", cfg: "AppConfig") -> None:
    """Mount the MCP Streamable HTTP ASGI app under cfg.mcp_mount_path."""
    if not cfg.mcp_enabled:
        return

    from querygate.mcp.auth import MCPAuthMiddleware
    from querygate.mcp.transport_guard import MCPRequestGuardMiddleware

    server = create_mcp_server()
    # TODO.md item 129: fail loudly here rather than silently serving a
    # tools/list (etc.) response with no cacheScope, if a future refactor
    # ever skips or breaks install_private_cache_scope upstream.
    assert_private_cache_scope_installed(server)
    # Transport settings move to streamable_http_app() in v2 (no longer a
    # constructor arg / server.settings.transport_security mutation).
    mcp_asgi = server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=cfg.mcp_dns_rebinding_protection,
            allowed_hosts=cfg.mcp_allowed_hosts,
            allowed_origins=cfg.mcp_allowed_origins,
        ),
    )
    authed_mcp = MCPAuthMiddleware(app=mcp_asgi, settings=cfg)
    # Guard the body (size/depth) outermost, before auth and before the
    # transport's json.loads — TODO.md item 86.
    guarded_mcp = MCPRequestGuardMiddleware(app=authed_mcp, settings=cfg)
    app.mount(cfg.mcp_mount_path, guarded_mcp)

    logger = get_logger()
    logger.info(
        "mcp.server.mounted",
        mount_path=cfg.mcp_mount_path,
        tool_count=len(server._tool_manager.list_tools()),
        auth_mode="api_keys" if cfg.mcp_api_keys else "dev_bypass",
        oauth_resource_server=cfg.mcp_oauth_resource_server_enabled,
        dns_rebinding_protection=cfg.mcp_dns_rebinding_protection,
    )
