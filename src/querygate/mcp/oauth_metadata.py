"""RFC 9728 OAuth 2.0 Protected Resource Metadata for the MCP surface.

TODO.md item 90 phase 2. When `mcp_oauth_resource_server_enabled` is set, the
mounted MCP surface acts as an OAuth 2.0 resource server per the MCP 2026-07-28
authorization spec. Part of being a resource server is publishing *protected
resource metadata* (RFC 9728) so a client that receives a `401` challenge can
discover which authorization server(s) issue tokens for this resource and what
audience/scope those tokens must carry.

The metadata document is served **unauthenticated** from the main application
(not from under the `/mcp` mount, which requires a token) at the RFC 9728
well-known location for the resource path: for an MCP mount at `/mcp`, that is
`/.well-known/oauth-protected-resource/mcp`. A root alias
(`/.well-known/oauth-protected-resource`) is also served for clients that omit
the resource path suffix. This module only *describes* the resource; the actual
token enforcement (audience binding + scope step-up) lives in `mcp/auth.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict

from fastapi import APIRouter

from querygate.core.scopes import ALL_SCOPES

if TYPE_CHECKING:
    from querygate.core.config import AppConfig

# The RFC 9728 well-known path prefix. The resource's own path
# (`AppConfig.mcp_mount_path`, e.g. `/mcp`) is appended to it.
WELL_KNOWN_PREFIX = "/.well-known/oauth-protected-resource"


def protected_resource_metadata_path(cfg: "AppConfig") -> str:
    """The absolute request path the metadata document is served from.

    This is the value a `WWW-Authenticate: Bearer resource_metadata=...`
    challenge points a client at, so it must match the route registered by
    `build_oauth_metadata_router`.
    """
    return f"{WELL_KNOWN_PREFIX}{cfg.mcp_mount_path}"


def build_protected_resource_metadata(cfg: "AppConfig") -> Dict[str, Any]:
    """Build the RFC 9728 protected-resource metadata document.

    Only identity/discovery fields are published — never a credential, a
    connection, or any query content — so this document is safe to serve
    unauthenticated to any client that will then obtain a token elsewhere.
    """
    metadata: Dict[str, Any] = {
        "resource": cfg.mcp_resource_identifier,
        "authorization_servers": list(cfg.mcp_authorization_servers),
        # RFC 6750 §2.1: this resource only accepts the token in the
        # Authorization request header, never a query/form parameter.
        "bearer_methods_supported": ["header"],
        # RFC 9728 `scopes_supported` advertises the ENTIRE scope vocabulary this
        # resource understands (TODO.md item 95) so an IdP can import it and mint
        # usable tokens with zero manual archaeology. This is deliberately NOT
        # `mcp_required_scopes` — that is the access *gate* for the MCP surface
        # (enforced in mcp/auth.py) and stays exactly as configured. A custom
        # required scope an operator set that isn't in the built-in catalog is
        # still surfaced here via the union, so discovery never hides a gate.
        "scopes_supported": sorted(set(ALL_SCOPES) | set(cfg.mcp_required_scopes)),
    }
    if cfg.mcp_resource_documentation:
        metadata["resource_documentation"] = cfg.mcp_resource_documentation
    return metadata


def build_oauth_metadata_router(cfg: "AppConfig") -> APIRouter:
    """Router exposing the RFC 9728 metadata document (unauthenticated).

    Registered on the main app only when the MCP OAuth resource server is
    enabled. Both the resource-path-suffixed well-known location (the
    canonical one referenced by the auth challenge) and the bare prefix are
    served, returning the identical document.
    """
    router = APIRouter(tags=["oauth"])
    metadata = build_protected_resource_metadata(cfg)
    suffixed_path = protected_resource_metadata_path(cfg)

    async def _metadata() -> Dict[str, Any]:
        return metadata

    router.add_api_route(suffixed_path, _metadata, methods=["GET"], include_in_schema=False)
    if suffixed_path != WELL_KNOWN_PREFIX:
        router.add_api_route(WELL_KNOWN_PREFIX, _metadata, methods=["GET"], include_in_schema=False)
    return router
