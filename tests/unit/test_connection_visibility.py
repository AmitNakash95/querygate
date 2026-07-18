"""Unit coverage for principal-aware connection visibility."""

from __future__ import annotations

import pytest

from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.connections.visibility import list_visible_connections, resolve_visible_connection
from querygate.core.auth import Principal
from querygate.core.exceptions import NotFoundError
from querygate.policy.loader import PolicyStore, set_policy_store


def _set_two_connections(*, second_profile_enabled: bool = True) -> None:
    set_registry(
        ConnectionRegistry(
            {
                "public": ConnectionProfile(
                    id="public",
                    dialect="postgresql",
                    connection_string="postgresql+asyncpg://user:pass@localhost/public",
                ),
                "private": ConnectionProfile(
                    id="private",
                    dialect="postgresql",
                    connection_string="postgresql+asyncpg://user:pass@localhost/private",
                    enabled=second_profile_enabled,
                ),
            }
        )
    )


def test_deny_by_default_policy_exposes_only_explicit_principal_grants():
    _set_two_connections()
    set_policy_store(
        PolicyStore.from_dict(
            {
                "default": {"enabled": False},
                "principals": {"agent-a": {"public": {"enabled": True}}},
            }
        )
    )

    visible = list_visible_connections(Principal(subject="agent-a"))

    assert [connection.id for connection in visible] == ["public"]


def test_unknown_principal_inherits_deny_by_default_policy():
    _set_two_connections()
    set_policy_store(
        PolicyStore.from_dict(
            {
                "default": {"enabled": False},
                "principals": {"agent-a": {"public": {"enabled": True}}},
            }
        )
    )

    assert list_visible_connections(Principal(subject="unknown-agent")) == []


def test_deployment_disabled_connection_cannot_be_reenabled_by_principal_policy():
    _set_two_connections(second_profile_enabled=False)
    set_policy_store(
        PolicyStore.from_dict(
            {
                "default": {"enabled": True},
                "principals": {"agent-a": {"private": {"enabled": True}}},
            }
        )
    )

    visible = list_visible_connections(Principal(subject="agent-a"))

    assert [connection.id for connection in visible] == ["public"]


@pytest.mark.parametrize("connection_id", ["private", "does_not_exist"])
def test_hidden_and_unknown_connections_return_same_error(connection_id):
    _set_two_connections()
    set_policy_store(PolicyStore.from_dict({"default": {"enabled": False}}))

    with pytest.raises(NotFoundError) as exc_info:
        resolve_visible_connection(connection_id, Principal(subject="agent-a"))

    assert str(exc_info.value) == f"Unknown connection: {connection_id!r}"
