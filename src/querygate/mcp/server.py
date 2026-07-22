"""MCP server factory and ASGI integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from querygate.core.logging import get_logger
from querygate.core.scopes import ADMIN_CONFIG_READ_SCOPE
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

mcp_server: FastMCP = FastMCP(
    name="querygate",
    instructions=MCP_INSTRUCTIONS,
    streamable_http_path="/",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "localhost",
            "localhost:*",
            "127.0.0.1",
            "127.0.0.1:*",
            "[::1]",
            "[::1]:*",
        ],
    ),
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


def _install_scoped_tool_listing(server: FastMCP) -> None:
    """Filter tools/list to what the caller's scopes can actually call.

    Token-savings/defense-in-depth only: an admin-only tool's ~6KB schema
    stops being sent to sessions that can never call it. The real
    authorization boundary stays each tool's own call-time scope check
    (e.g. help/service.py's redacted_configuration) — this must never be
    the only thing standing between a caller and a scope-gated tool.
    """
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

    # Re-registering overwrites the low-level Server's ListToolsRequest
    # handler (a plain dict assignment) — safe to call repeatedly, and
    # `unfiltered_list_tools` above always closes over the true unfiltered
    # FastMCP.list_tools, never a previously-installed wrapper.
    server._mcp_server.list_tools()(scoped_list_tools)


def create_mcp_server() -> FastMCP:
    """Import tool modules and return the shared FastMCP instance."""
    from querygate.mcp.tools import discover_and_register_tools

    discover_and_register_tools()
    _install_scoped_tool_listing(mcp_server)
    return mcp_server


def setup_mcp(app: "FastAPI", cfg: "AppConfig") -> None:
    """Mount the MCP Streamable HTTP ASGI app under cfg.mcp_mount_path."""
    if not cfg.mcp_enabled:
        return

    from querygate.mcp.auth import MCPAuthMiddleware
    from querygate.mcp.transport_guard import MCPRequestGuardMiddleware

    server = create_mcp_server()
    server.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=cfg.mcp_dns_rebinding_protection,
        allowed_hosts=cfg.mcp_allowed_hosts,
        allowed_origins=cfg.mcp_allowed_origins,
    )
    mcp_asgi = server.streamable_http_app()
    authed_mcp = MCPAuthMiddleware(app=mcp_asgi, settings=cfg)
    # Guard the body (size/depth) outermost, before auth and before the
    # transport's json.loads — TODO.md item 86.
    guarded_mcp = MCPRequestGuardMiddleware(app=authed_mcp, settings=cfg)
    app.mount(cfg.mcp_mount_path, guarded_mcp)

    logger = get_logger()
    logger.info(
        "mcp.server.mounted",
        mount_path=cfg.mcp_mount_path,
        tool_count=len(server._tool_manager._tools),
        auth_mode="api_keys" if cfg.mcp_api_keys else "dev_bypass",
        oauth_resource_server=cfg.mcp_oauth_resource_server_enabled,
        dns_rebinding_protection=cfg.mcp_dns_rebinding_protection,
    )
