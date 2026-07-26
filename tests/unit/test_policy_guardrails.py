"""The drift guard for `Policy`'s guardrail field set (TODO.md item 115).

Four surfaces answer "which caps are in force" — the semantic access diff, the
effective-guardrails view, the help API's policy summary, and the admin UI's
policy panel. Each used to hand-list the fields, nothing checked them against
`Policy`, and all four had rotted: nine caps added by items 68-72, 88, 97, 100
and 101 were missing from at least one, so a config change loosening one was
reported to a reviewer as **no guardrail change**.

These tests are the missing guard, not a restatement of the fix. They fail when
a future item adds a cap and forgets it — which is the only reason the drift
happened in the first place.
"""

from __future__ import annotations

import typing

import pytest

from querygate.policy.models import (
    _NON_GUARDRAIL_POLICY_FIELDS,
    GUARDRAIL_FIELDS,
    INVERTED_GUARDRAIL_FIELDS,
    CostEstimationMode,
    Policy,
)

pytestmark = pytest.mark.unit

_SCALAR_TYPES = (int, float, bool, str)


def _scalar(annotation: object) -> bool:
    """Whether a field annotation is a scalar cap (possibly Optional)."""
    if annotation in _SCALAR_TYPES or annotation is CostEstimationMode:
        return True
    args = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
    return bool(args) and all(_scalar(arg) for arg in args)


def test_every_policy_field_is_either_a_guardrail_or_deliberately_excluded():
    """The partition must be exhaustive. A new `Policy` field is a guardrail by
    default — being excluded takes a deliberate edit with a stated reason."""
    assert set(GUARDRAIL_FIELDS) | _NON_GUARDRAIL_POLICY_FIELDS == set(Policy.model_fields)
    assert not set(GUARDRAIL_FIELDS) & _NON_GUARDRAIL_POLICY_FIELDS


def test_every_excluded_field_still_exists_on_policy():
    """A stale exclusion would silently re-open the hole for a renamed field."""
    unknown = sorted(_NON_GUARDRAIL_POLICY_FIELDS - set(Policy.model_fields))
    assert not unknown, f"exclusion list names fields Policy no longer has: {unknown}"


def test_every_guardrail_field_is_a_scalar_cap():
    """The permissiveness comparator compares numbers. A collection-typed field
    that nobody excluded would be compared as one and silently mis-reported, so
    it must fail here instead — this is what catches the next
    `approval_sensitivities`-shaped field."""
    non_scalar = [
        name for name in GUARDRAIL_FIELDS if not _scalar(Policy.model_fields[name].annotation)
    ]
    assert not non_scalar, (
        f"{non_scalar} are not scalar caps — either exclude them in "
        "_NON_GUARDRAIL_POLICY_FIELDS with a reason and report them explicitly "
        "(see approval_sensitivities in access_diff), or make them scalar"
    )


def test_inverted_fields_are_real_guardrails():
    assert INVERTED_GUARDRAIL_FIELDS <= set(GUARDRAIL_FIELDS)


@pytest.mark.parametrize(
    "cap",
    [
        # items 68-72
        "max_where_predicates",
        "max_in_list_size",
        "max_case_branches",
        # item 88
        "min_group_size",
        # item 97
        "max_subquery_depth",
        # item 100
        "max_expression_depth",
        "max_expression_nodes",
        # item 101
        "max_window_specs",
        "max_window_frame_offset",
    ],
)
def test_the_caps_that_had_rotted_out_are_covered(cap):
    """Named explicitly, so this reads as the regression it is: each of these
    shipped with an item and was then invisible on at least one surface."""
    assert cap in GUARDRAIL_FIELDS


def test_all_four_surfaces_report_the_identical_field_set():
    """THE anti-rot test. Four consumers, one derivation — if any of them grows a
    list of its own again, this fails."""
    from querygate.admin.access_diff import _GUARDRAIL_FIELDS as diff_fields
    from querygate.admin.models import EffectiveGuardrails
    from querygate.api.admin_ui_routes import _guardrail_summary
    from querygate.help.service import _GUARDRAIL_FIELDS as help_fields

    assert tuple(diff_fields) == GUARDRAIL_FIELDS
    assert tuple(help_fields) == GUARDRAIL_FIELDS
    assert tuple(EffectiveGuardrails.model_fields) == GUARDRAIL_FIELDS
    assert tuple(_guardrail_summary(Policy())) == GUARDRAIL_FIELDS


def test_effective_guardrails_types_match_policys_own():
    """Generated from `Policy`, so a cap's type/optionality cannot diverge between
    the enforcement model and the model an operator reads."""
    from querygate.admin.models import EffectiveGuardrails

    for name in GUARDRAIL_FIELDS:
        assert (
            EffectiveGuardrails.model_fields[name].annotation
            == Policy.model_fields[name].annotation
        ), name


def test_effective_guardrails_round_trips_a_real_policy():
    from querygate.admin.models import EffectiveGuardrails

    policy = Policy(max_window_specs=3, min_group_size=5, max_expression_nodes=42)
    view = EffectiveGuardrails.model_validate(
        policy.model_dump(include=set(EffectiveGuardrails.model_fields))
    )
    assert view.max_window_specs == 3
    assert view.min_group_size == 5
    assert view.max_expression_nodes == 42


def test_every_guardrail_direction_is_either_obvious_or_reviewed():
    """Deriving the field set fixes "a new cap is invisible"; this fixes the layer
    below it — a new cap diffed in the WRONG direction. A `max_*` name says which
    way it runs; anything else must appear in `_DIRECTION_REVIEWED_GUARDRAILS`,
    so the author has to decide rather than inherit a default that may be
    backwards (as it would be for a second `min_group_size`)."""
    from querygate.policy.models import _DIRECTION_REVIEWED_GUARDRAILS

    unreviewed = [
        name
        for name in GUARDRAIL_FIELDS
        if not name.startswith("max_") and name not in _DIRECTION_REVIEWED_GUARDRAILS
    ]
    assert not unreviewed, (
        f"{unreviewed}: this cap's name does not say whether a higher value is "
        "looser or stricter. Decide, then add it to _DIRECTION_REVIEWED_GUARDRAILS "
        "(and to INVERTED_GUARDRAIL_FIELDS if higher means MORE restrictive)."
    )


def test_no_max_prefixed_cap_is_secretly_inverted():
    """The naming convention the test above leans on, asserted rather than
    assumed: if a `max_*` cap ever needs inverting, the convention is broken and
    the exemption must be reconsidered, not quietly special-cased."""
    from querygate.policy.models import _DIRECTION_REVIEWED_GUARDRAILS

    surprising = sorted(
        name
        for name in INVERTED_GUARDRAIL_FIELDS
        if name.startswith("max_") and name not in _DIRECTION_REVIEWED_GUARDRAILS
    )
    assert not surprising, surprising
