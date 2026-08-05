"""Unit tests for policy/models.py: MandatoryRowFilter's static value vs
per-principal from_claim resolution, and Policy's cost-estimation opt-in.
"""

from __future__ import annotations

import pytest

from querygate.core.auth import Principal
from querygate.core.exceptions import PolicyViolationError
from querygate.policy.models import (
    ColumnMask,
    ColumnMaskKind,
    MandatoryRowFilter,
    Policy,
    PurposePolicyDelta,
)


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


# --- Policy.for_purpose (TODO.md item 145, feature F7) ---------------------


def test_for_purpose_none_returns_the_same_policy_unchanged():
    policy = Policy(denied_tables=["secrets"])
    assert policy.for_purpose(None) is policy


def test_for_purpose_with_no_configured_delta_returns_the_same_policy_unchanged():
    policy = Policy(allowed_purposes=["support"], denied_tables=["secrets"])
    assert policy.for_purpose("support") is policy


def test_for_purpose_unions_denied_tables():
    policy = Policy(
        denied_tables=["secrets"],
        purpose_policies={"support": PurposePolicyDelta(denied_tables=["billing"])},
    )
    narrowed = policy.for_purpose("support")
    assert set(narrowed.denied_tables) == {"secrets", "billing"}
    # The base policy itself must be untouched — for_purpose returns a new
    # object, never mutates the one it was called on.
    assert policy.denied_tables == ["secrets"]


def test_for_purpose_unions_denied_columns_per_table():
    policy = Policy(
        denied_columns={"orders": ["ssn"]},
        purpose_policies={
            "support": PurposePolicyDelta(denied_columns={"orders": ["credit_card"]})
        },
    )
    narrowed = policy.for_purpose("support")
    assert set(narrowed.denied_columns["orders"]) == {"ssn", "credit_card"}


def test_for_purpose_appends_mandatory_row_filters():
    base_filter = MandatoryRowFilter(table="orders", column="tenant_id", value="acme")
    delta_filter = MandatoryRowFilter(table="orders", column="region", value="us")
    policy = Policy(
        mandatory_row_filters=[base_filter],
        purpose_policies={"support": PurposePolicyDelta(mandatory_row_filters=[delta_filter])},
    )
    narrowed = policy.for_purpose("support")
    assert narrowed.mandatory_row_filters == [base_filter, delta_filter]


def test_for_purpose_adds_a_mask_to_a_previously_unmasked_column():
    mask = ColumnMask(column="email", kind=ColumnMaskKind.NULL)
    policy = Policy(
        purpose_policies={"support": PurposePolicyDelta(column_masks={"users": [mask]})}
    )
    narrowed = policy.for_purpose("support")
    assert narrowed.column_mask("users", "email") == mask
    # The base (unnarrowed) policy must still see the column as unmasked.
    assert policy.column_mask("users", "email") is None


def test_for_purpose_mask_wins_over_a_base_mask_on_the_same_column():
    base_mask = ColumnMask(column="ssn", kind=ColumnMaskKind.LAST, length=4)
    purpose_mask = ColumnMask(column="ssn", kind=ColumnMaskKind.NULL)
    policy = Policy(
        column_masks={"users": [base_mask]},
        purpose_policies={"support": PurposePolicyDelta(column_masks={"users": [purpose_mask]})},
    )
    narrowed = policy.for_purpose("support")
    assert narrowed.column_mask("users", "ssn") == purpose_mask


def test_for_purpose_never_removes_a_base_deny_or_mandatory_filter():
    """The 'narrows never widens' invariant, stated as a test: whatever the
    base Policy already restricts stays restricted after for_purpose."""
    base_filter = MandatoryRowFilter(table="orders", column="tenant_id", value="acme")
    policy = Policy(
        denied_tables=["secrets"],
        denied_columns={"orders": ["ssn"]},
        mandatory_row_filters=[base_filter],
        purpose_policies={"support": PurposePolicyDelta()},
    )
    narrowed = policy.for_purpose("support")
    assert "secrets" in narrowed.denied_tables
    assert "ssn" in narrowed.denied_columns["orders"]
    assert base_filter in narrowed.mandatory_row_filters


def test_for_purpose_mask_merge_is_case_insensitive_on_the_table_key():
    """Found by `security-invariant-reviewer` (2026-08-05): a plain dict merge
    keyed by literal table-name casing would let a case-mismatched delta key
    (e.g. "Customers" vs base's "customers") silently DROP the base's own
    mask for that table — `_ci_lookup`'s first-match-wins read would only
    ever see whichever key it inserted last, unmasking a column the base
    policy protects. Real column/table casing legitimately differs across
    reflection sources (`_ci_lookup`'s own docstring: "table-name casing in
    policy.yaml isn't guaranteed to match what schema reflection returns")."""
    base_mask = ColumnMask(column="email", kind=ColumnMaskKind.NULL)
    delta_mask = ColumnMask(column="phone", kind=ColumnMaskKind.NULL)
    policy = Policy(
        column_masks={"customers": [base_mask]},
        purpose_policies={"support": PurposePolicyDelta(column_masks={"Customers": [delta_mask]})},
    )
    narrowed = policy.for_purpose("support")
    # Both masks must survive under whichever casing was used — the base's
    # email mask must not vanish just because the delta spelled the table
    # differently.
    assert narrowed.column_mask("customers", "email") == base_mask
    assert narrowed.column_mask("Customers", "phone") == delta_mask


def test_for_purpose_denied_columns_merge_is_case_insensitive_on_the_table_key():
    """Same defect class as the mask test above, opposite direction: a
    case-mismatched delta key must not make the delta's own added deny
    silently fail to apply."""
    policy = Policy(
        denied_columns={"orders": ["ssn"]},
        purpose_policies={
            "support": PurposePolicyDelta(denied_columns={"Orders": ["credit_card"]})
        },
    )
    narrowed = policy.for_purpose("support")
    assert not narrowed.column_allowed("orders", "ssn")
    assert not narrowed.column_allowed("Orders", "credit_card")


def test_for_purpose_mask_merge_preserves_a_base_mask_on_a_different_column_same_table():
    """A naive `dict.update()`-style merge (replace the whole per-table list
    rather than union it) would drop this base mask just because the delta
    also touches this table — even with matching casing. Distinct from the
    case-mismatch tests above: this is the same-key merge-completeness
    check."""
    base_mask = ColumnMask(column="ssn", kind=ColumnMaskKind.NULL)
    delta_mask = ColumnMask(column="email", kind=ColumnMaskKind.NULL)
    policy = Policy(
        column_masks={"users": [base_mask]},
        purpose_policies={"support": PurposePolicyDelta(column_masks={"users": [delta_mask]})},
    )
    narrowed = policy.for_purpose("support")
    assert narrowed.column_mask("users", "ssn") == base_mask
    assert narrowed.column_mask("users", "email") == delta_mask
