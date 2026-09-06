"""Unit tests for PolicyStore — connection-level and per-principal overrides."""

from __future__ import annotations

import pytest

from pydantic import ValidationError

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


# ---------------------------------------------------------------------------
# TODO.md item 188 — a principal override must be validated against every merge
# base it can actually land on, not just the default.
# ---------------------------------------------------------------------------


def _three_layer_raw() -> dict:
    """The exact acceptance case from item 188.

    Each layer is individually valid. `default:` sets a k-anonymity floor;
    `connections.foo:` removes it; `principals.alice.foo:` sets a disclosure cap
    that `Policy._disclosure_budget_needs_a_k_floor` requires a floor for. The
    only invalid thing is the *combination* the request path actually builds.
    """
    return {
        "default": {"min_group_size": 5},
        "connections": {"foo": {"min_group_size": None}},
        "principals": {"alice": {"foo": {"max_shape_repeats_per_window": 10}}},
    }


def test_a_principal_override_invalid_against_its_connection_base_fails_at_load():
    """Before item 188 this loaded clean and `validate-config` passed, then the
    first query alice issued on `foo` raised inside
    `StructuredQueryService._get_policy()` and `mask_unexpected` turned it into a
    generic 500 — one principal fully offline on a config the CLI accepted."""
    with pytest.raises(ValidationError):
        PolicyStore.from_dict(_three_layer_raw())


def test_the_same_override_still_loads_when_its_connection_base_keeps_the_floor():
    """The control: the failure must come from the real merge base, not from
    rejecting the field outright. `foo` keeps the inherited floor here."""
    raw = _three_layer_raw()
    raw["connections"]["foo"] = {"max_joins": 2}
    store = PolicyStore.from_dict(raw)
    policy = store.get("foo", principal=Principal(subject="alice"))
    assert policy.max_shape_repeats_per_window == 10
    assert policy.min_group_size == 5


def test_a_wildcard_override_is_checked_against_every_connection_base():
    """A `"*"` entry lands on every connection, so one floorless connection is
    enough to make it invalid — checking it against the default alone would miss
    exactly the connection that breaks."""
    raw = {
        "default": {"min_group_size": 5},
        "connections": {"floorless": {"min_group_size": None}},
        "principals": {"alice": {"*": {"max_shape_repeats_per_window": 10}}},
    }
    with pytest.raises(ValidationError):
        PolicyStore.from_dict(raw)


def test_a_wildcard_override_loads_when_every_connection_base_is_compatible():
    raw = {
        "default": {"min_group_size": 5},
        "connections": {"other": {"max_joins": 2}},
        "principals": {"alice": {"*": {"max_shape_repeats_per_window": 10}}},
    }
    store = PolicyStore.from_dict(raw)
    assert store.get("other", principal=Principal(subject="alice")).min_group_size == 5


def test_a_typo_in_a_principal_override_still_fails_at_load():
    """The check item 188 replaced existed to catch a misspelled field name.
    That must survive the change — `Policy` is `extra="forbid"`, and the merge
    base does not affect it."""
    with pytest.raises(ValidationError):
        PolicyStore.from_dict({"principals": {"alice": {"*": {"max_joinz": 3}}}})


def test_every_connection_base_that_get_could_resolve_is_checked():
    """`get()` falls back to the bare default for a connection with no
    `connections:` entry, so a principal override naming such a connection is
    checked against the default — not skipped for want of an override entry."""
    raw = {
        "default": {"min_group_size": None},
        "connections": {"floored": {"min_group_size": 5}},
        "principals": {"alice": {"unlisted": {"max_shape_repeats_per_window": 10}}},
    }
    with pytest.raises(ValidationError):
        PolicyStore.from_dict(raw)
