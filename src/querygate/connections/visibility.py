"""Principal-aware connection visibility shared by REST, MCP, and execution.

A connection is reachable only when both its deployment profile and the
caller's resolved policy are enabled. Disabled and unknown connections are
reported identically so callers cannot use direct tool/API calls to infer a
hidden connection's existence.
"""

from __future__ import annotations

from typing import Optional, Tuple

from querygate.connections.models import ConnectionProfile, PublicConnectionInfo
from querygate.connections.registry import get_registry
from querygate.core.auth import Principal
from querygate.core.exceptions import NotFoundError
from querygate.policy.loader import get_policy
from querygate.policy.models import Policy


def resolve_visible_connection(
    connection_id: str, principal: Optional[Principal] = None
) -> Tuple[ConnectionProfile, Policy]:
    """Return the profile and resolved policy, or a non-enumerating 404-style error."""
    try:
        profile = get_registry().get(connection_id)
    except KeyError as exc:
        raise NotFoundError(f"Unknown connection: {connection_id!r}") from exc

    policy = get_policy(connection_id, principal=principal)
    if not profile.enabled or not policy.enabled:
        raise NotFoundError(f"Unknown connection: {connection_id!r}")
    return profile, policy


def list_visible_connections(
    principal: Optional[Principal] = None,
) -> list[PublicConnectionInfo]:
    """Credential-free connections enabled for this principal, sorted by id."""
    visible: list[PublicConnectionInfo] = []
    for info in get_registry().list_public():
        try:
            resolve_visible_connection(info.id, principal=principal)
        except NotFoundError:
            continue
        visible.append(info)
    return visible
