"""Unit tests for PolicyStore — connection-level and per-principal overrides."""

from __future__ import annotations

import pytest

from querygate.core.auth import Actor, Principal
from querygate.policy.loader import PolicyStore


def test_get_without_principal_returns_connection_policy():
    store = PolicyStore.from_dict(
        {
            "default": {"max_joins": 4},
            "connections": {"demo": {"max_joins": 2}},
        }
    )
    assert store.get("demo").max_joins == 2
    assert store.get("other").max_joins == 4


def test_get_with_principal_but_no_override_falls_back_to_connection_policy():
    store = PolicyStore.from_dict(
        {
            "default": {"max_joins": 4},
            "connections": {"demo": {"max_joins": 2}},
        }
    )
    principal = Principal(subject="unlisted-agent")
    assert store.get("demo", principal=principal).max_joins == 2


def test_delegated_request_resolves_the_humans_policy_not_the_agents():
    # TODO.md item 90: for an on-behalf-of request the human is Principal.subject
    # and the agent is Principal.actor. Policy resolution keys off `subject`, so
    # the *human's* override must apply — never the agent's — with no change to
    # the policy layer. This is the core attribution guarantee.
    store = PolicyStore.from_dict(
        {
            "default": {"max_limit": 100},
            "principals": {
                "user-human": {"*": {"max_limit": 5}},
                "agent-app": {"*": {"max_limit": 9999}},
            },
        }
    )
    delegated = Principal(subject="user-human", actor=Actor(subject="agent-app"))
    # The human's restrictive limit applies, not the agent's permissive one.
    assert store.get("demo", principal=delegated).max_limit == 5


def test_principal_override_merges_on_top_of_connection_policy():
    store = PolicyStore.from_dict(
        {
            "default": {"max_joins": 4, "max_limit": 100},
            "connections": {"demo": {"max_joins": 2}},
            "principals": {
                "agent-a": {"demo": {"max_limit": 10}},
            },
        }
    )
    policy = store.get("demo", principal=Principal(subject="agent-a"))
    assert policy.max_limit == 10  # from the principal override
    assert policy.max_joins == 2  # inherited from the connection override, not clobbered


def test_principal_wildcard_override_applies_to_every_connection():
    store = PolicyStore.from_dict(
        {
            "default": {"max_limit": 100},
            "principals": {"agent-a": {"*": {"max_limit": 5}}},
        }
    )
    assert store.get("demo", principal=Principal(subject="agent-a")).max_limit == 5
    assert store.get("other", principal=Principal(subject="agent-a")).max_limit == 5


def test_connection_specific_principal_override_wins_over_wildcard():
    store = PolicyStore.from_dict(
        {
            "default": {"max_limit": 100},
            "principals": {
                "agent-a": {
                    "*": {"max_limit": 5},
                    "demo": {"max_limit": 20},
                }
            },
        }
    )
    assert store.get("demo", principal=Principal(subject="agent-a")).max_limit == 20
    assert store.get("other", principal=Principal(subject="agent-a")).max_limit == 5


def test_different_principals_get_different_policy_on_same_connection():
    store = PolicyStore.from_dict(
        {
            "default": {},
            "principals": {
                "agent-a": {"demo": {"denied_tables": ["customers"]}},
                "agent-b": {"demo": {"denied_tables": []}},
            },
        }
    )
    assert not store.get("demo", principal=Principal(subject="agent-a")).table_allowed("customers")
    assert store.get("demo", principal=Principal(subject="agent-b")).table_allowed("customers")


def test_malformed_principal_override_raises_at_load_time():
    with pytest.raises(Exception):
        PolicyStore.from_dict(
            {
                "default": {},
                "principals": {"agent-a": {"demo": {"not_a_real_field": 1}}},
            }
        )


def test_override_connection_ids_unaffected_by_principals_section():
    store = PolicyStore.from_dict(
        {
            "default": {},
            "connections": {"demo": {}},
            "principals": {"agent-a": {"demo": {}}},
        }
    )
    assert store.override_connection_ids() == ["demo"]
