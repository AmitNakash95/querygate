"""Unit tests for policy/models.py's MandatoryRowFilter — static value vs
per-principal from_claim resolution.
"""

from __future__ import annotations

import pytest

from querygate.core.auth import Principal
from querygate.core.exceptions import PolicyViolationError
from querygate.policy.models import MandatoryRowFilter


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
