"""MCP server factory and ASGI integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from querygate.core.logging import get_logger
from querygate.mcp.instructions import MCP_INSTRUCTIONS

if TYPE_CHECKING:
    from fastapi import FastAPI

    from querygate.core.config import AppConfig

mcp_server: FastMCP = FastMCP(
    name="querygate",
    instructions=MCP_INSTRUCTIONS,
    streamable_http_path="/",
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def create_mcp_server() -> FastMCP:
    """Import tool modules and return the shared FastMCP instance."""
    from querygate.mcp.tools import discover_and_register_tools

    discover_and_register_tools()
    return mcp_server


def setup_mcp(app: "FastAPI", cfg: "AppConfig") -> None:
    """Mount the MCP Streamable HTTP ASGI app under cfg.mcp_mount_path."""
    if not cfg.mcp_enabled:
        return

    from querygate.mcp.auth import MCPAuthMiddleware

    server = create_mcp_server()
    mcp_asgi = server.streamable_http_app()
    authed_mcp = MCPAuthMiddleware(app=mcp_asgi, settings=cfg)
    app.mount(cfg.mcp_mount_path, authed_mcp)

    logger = get_logger()
    logger.info(
        "mcp.server.mounted",
        mount_path=cfg.mcp_mount_path,
        tool_count=len(server._tool_manager._tools),
        auth_mode="api_keys" if cfg.mcp_api_keys else "dev_bypass",
    )
