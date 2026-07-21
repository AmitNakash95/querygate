"""Unit tests for policy/models.py: MandatoryRowFilter's static value vs
per-principal from_claim resolution, and Policy's cost-estimation opt-in.
"""

from __future__ import annotations

import pytest

from querygate.core.auth import Principal
from querygate.core.exceptions import PolicyViolationError
from querygate.policy.models import MandatoryRowFilter, Policy


def test_requires_value_or_from_claim():
    with pytest.raises(ValueError, match="value.*from_claim|from_claim.*value"):
        MandatoryRowFilter(table="orders", column="tenant_id")


def test_static_value_resolves_regardless_of_principal():
    row_filter = MandatoryRowFilter(table="orders", column="tenant_id", value="acme")
    assert row_filter.resolve(None) == "acme"
    assert row_filter.resolve(Principal(subject="agent-a")) == "acme"


def test_from_claim_resolves_from_principal_claims():
    row_filter = MandatoryRowFilter(table="orders", column="tenant_id", from_claim="tenant_id")
    principal = Principal(subject="agent-a", claims={"tenant_id": "acme"})
    assert row_filter.resolve(principal) == "acme"


def test_from_claim_without_principal_raises():
    row_filter = MandatoryRowFilter(table="orders", column="tenant_id", from_claim="tenant_id")
    with pytest.raises(PolicyViolationError, match="tenant_id"):
        row_filter.resolve(None)


def test_from_claim_missing_from_principal_raises():
    row_filter = MandatoryRowFilter(table="orders", column="tenant_id", from_claim="tenant_id")
    principal = Principal(subject="agent-a", claims={"other_claim": "x"})
    with pytest.raises(PolicyViolationError, match="tenant_id"):
        row_filter.resolve(principal)


def test_cost_estimation_disabled_by_default():
    assert Policy().cost_estimation_enabled is False


def test_cost_estimation_enabled_by_max_estimated_rows_alone():
    assert Policy(max_estimated_rows=1000).cost_estimation_enabled is True


def test_cost_estimation_enabled_by_max_estimated_cost_alone():
    assert Policy(max_estimated_cost=1000.0).cost_estimation_enabled is True


def test_min_group_size_defaults_to_disabled():
    assert Policy().min_group_size is None


def test_min_group_size_accepts_k_of_two_or_more():
    assert Policy(min_group_size=2).min_group_size == 2
    assert Policy(min_group_size=25).min_group_size == 25


def test_min_group_size_rejects_k_below_two():
    # k=1 is no protection (every single row is its own group); the floor is 2.
    with pytest.raises(ValueError):
        Policy(min_group_size=1)
    with pytest.raises(ValueError):
        Policy(min_group_size=0)
