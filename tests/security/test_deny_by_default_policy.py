"""Deny-by-default at table and column granularity (TODO.md item 220).

Two properties, and they pull in opposite directions, which is exactly why both
are pinned:

* **Nothing changes for anyone who has not opted in.** `require_explicit_allowlist`
  defaults to `False`, and an empty allow-list keeps meaning "no restriction".
  Flipping the meaning of a live security control under existing deployments is
  the change that breaks a customer at 3am.
* **Opting in genuinely denies.** With the flag set, an empty allow-list denies
  every table and an absent column entry denies every column, so an operator
  names what a caller may reach rather than what it may not.

A swapped boolean in either branch silently opens every table on every connection
that opted in, with a green suite — the same failure class as items 101 and 114 —
so both directions are mutation-verified, not merely asserted.
"""

from __future__ import annotations

import pytest

from querygate.policy.models import Policy

pytestmark = [pytest.mark.security, pytest.mark.unit]


# --- the no-behaviour-change half ---------------------------------------------


def test_the_flag_is_off_by_default():
    """The whole opt-in promise rests on this one default."""
    assert Policy().require_explicit_allowlist is False


def test_an_empty_allowlist_still_allows_when_the_flag_is_unset():
    """The historical convention, preserved exactly. Every existing policy in
    every existing deployment lands here."""
    policy = Policy()
    assert policy.table_allowed("users") is True
    assert policy.column_allowed("users", "email") is True


def test_denied_tables_still_win_when_the_flag_is_unset():
    """Deny beats allow, unchanged."""
    policy = Policy(denied_tables=["secrets"])
    assert policy.table_allowed("secrets") is False
    assert policy.table_allowed("users") is True


# --- the deny-by-default half --------------------------------------------------


def test_an_empty_allowlist_denies_every_table_when_the_flag_is_set():
    policy = Policy(require_explicit_allowlist=True)
    assert policy.table_allowed("users") is False
    assert policy.table_allowed("anything_at_all") is False


def test_an_absent_column_allowlist_denies_every_column_when_the_flag_is_set():
    policy = Policy(require_explicit_allowlist=True, allowed_tables=["users"])
    assert policy.table_allowed("users") is True
    assert policy.column_allowed("users", "email") is False


def test_naming_a_table_admits_only_that_table():
    policy = Policy(require_explicit_allowlist=True, allowed_tables=["users"])
    assert policy.table_allowed("users") is True
    assert policy.table_allowed("orders") is False


def test_naming_a_column_admits_only_that_column():
    policy = Policy(
        require_explicit_allowlist=True,
        allowed_tables=["users"],
        allowed_columns={"users": ["email"]},
    )
    assert policy.column_allowed("users", "email") is True
    assert policy.column_allowed("users", "password_hash") is False


def test_a_wildcard_column_allowlist_still_works_under_the_flag():
    """`"*"` is the escape hatch for an operator who wants deny-by-default at
    the table level but not the column level. It must keep working, or the flag
    is unusable for the common case."""
    policy = Policy(
        require_explicit_allowlist=True,
        allowed_tables=["users"],
        allowed_columns={"*": ["email", "id"]},
    )
    assert policy.column_allowed("users", "email") is True
    assert policy.column_allowed("users", "password_hash") is False


def test_deny_still_beats_allow_under_the_flag():
    """Deny-by-default must not accidentally make an explicit deny weaker."""
    policy = Policy(
        require_explicit_allowlist=True, allowed_tables=["users"], denied_tables=["users"]
    )
    assert policy.table_allowed("users") is False


def test_the_starter_policy_shape_denies_until_tables_are_named():
    """The shape item 215's starter policy ships: a fresh install reaches
    nothing until the operator names a table deliberately."""
    starter = Policy(enabled=True, require_explicit_allowlist=True)
    assert starter.enabled is True
    assert starter.table_allowed("customers") is False
