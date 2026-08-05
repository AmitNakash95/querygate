"""Unit tests for the semantic access diff (querygate/admin/access_diff.py).

The diff compares *resolved* access between two loaded config contexts — not
YAML text — and classifies each change as tightening/loosening/neutral. These
tests drive the pure `compute_access_diff` against constructed
registries/policy stores; the HTTP surface, scope enforcement, and audit trail
are covered in the integration and security suites.
"""

from __future__ import annotations

from querygate.admin.access_diff import compute_access_diff
from querygate.catalog.loader import CatalogStore
from querygate.cli import LoadedConfigContext
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry
from querygate.policy.loader import PolicyStore
from querygate.catalog.models import SensitivityClass
from querygate.policy.models import (
    GUARDRAIL_FIELDS,
    ColumnMask,
    ColumnMaskKind,
    CostEstimationMode,
    MandatoryRowFilter,
    Policy,
    PurposePolicyDelta,
)


def _profile(connection_id: str = "demo", **overrides) -> ConnectionProfile:
    defaults = dict(
        id=connection_id,
        dialect="postgresql",
        connection_string=f"postgresql+asyncpg://u:p@l/{connection_id}",
        known_tables=["orders", "customers"],
    )
    defaults.update(overrides)
    return ConnectionProfile(**defaults)


def _ctx(
    default_policy=None, *, overrides=None, principals=None, profiles=None
) -> LoadedConfigContext:
    profiles = profiles or {"demo": _profile()}
    registry = ConnectionRegistry(profiles)
    store = PolicyStore(
        default=default_policy or Policy(),
        overrides=overrides or {},
        principal_overrides=principals or {},
    )
    return LoadedConfigContext(
        registry=registry, policy_store=store, catalog_store=CatalogStore.empty()
    )


def _changes_by(diff, category):
    return [c for c in diff.changes if c.category == category]


def test_no_changes_is_empty_diff():
    diff = compute_access_diff(_ctx(), _ctx())
    assert diff.changes == []
    assert diff.summary.total == 0
    assert diff.analysis_incomplete is False


def test_guardrail_raise_is_loosening():
    diff = compute_access_diff(_ctx(Policy(max_limit=100)), _ctx(Policy(max_limit=500)))
    (change,) = _changes_by(diff, "guardrail")
    assert change.object == "max_limit"
    assert change.direction == "loosening"
    assert change.before == "100" and change.after == "500"
    assert diff.summary.loosening == 1


def test_guardrail_lower_is_tightening():
    diff = compute_access_diff(_ctx(Policy(max_joins=5)), _ctx(Policy(max_joins=2)))
    (change,) = _changes_by(diff, "guardrail")
    assert change.object == "max_joins"
    assert change.direction == "tightening"


def test_optional_cap_none_to_value_is_tightening():
    # None == unlimited (most permissive); setting a ceiling tightens.
    diff = compute_access_diff(
        _ctx(Policy(max_estimated_rows=None)), _ctx(Policy(max_estimated_rows=1000))
    )
    (change,) = _changes_by(diff, "guardrail")
    assert change.object == "max_estimated_rows"
    assert change.direction == "tightening"
    assert change.before == "unlimited" and change.after == "1000"


def test_optional_cap_value_to_none_is_loosening():
    diff = compute_access_diff(_ctx(Policy(max_queue_depth=10)), _ctx(Policy(max_queue_depth=None)))
    (change,) = _changes_by(diff, "guardrail")
    assert change.object == "max_queue_depth"
    assert change.direction == "loosening"
    assert change.after == "unlimited"


def test_cost_estimation_mode_observe_is_looser_than_enforce():
    diff = compute_access_diff(
        _ctx(Policy(max_estimated_rows=1000, cost_estimation_mode="enforce")),
        _ctx(Policy(max_estimated_rows=1000, cost_estimation_mode="observe")),
    )
    (change,) = _changes_by(diff, "guardrail")
    assert change.object == "cost_estimation_mode"
    assert change.direction == "loosening"


def test_log_query_literals_enabled_is_loosening():
    diff = compute_access_diff(
        _ctx(Policy(log_query_literals=False)), _ctx(Policy(log_query_literals=True))
    )
    (change,) = _changes_by(diff, "guardrail")
    assert change.object == "log_query_literals"
    assert change.direction == "loosening"
    assert change.before == "false" and change.after == "true"


def test_table_access_added_is_loosening_removed_is_tightening():
    added = compute_access_diff(
        _ctx(Policy(allowed_tables=["orders"])),
        _ctx(Policy(allowed_tables=["orders", "customers"])),
    )
    (change,) = _changes_by(added, "table_access")
    assert change.object == "customers" and change.direction == "loosening"
    assert change.change_type == "added"

    removed = compute_access_diff(
        _ctx(Policy(allowed_tables=["orders", "customers"])),
        _ctx(Policy(allowed_tables=["orders"])),
    )
    (change,) = _changes_by(removed, "table_access")
    assert change.object == "customers" and change.direction == "tightening"


def test_column_denied_is_tightening_undenied_is_loosening():
    tightened = compute_access_diff(
        _ctx(Policy()), _ctx(Policy(denied_columns={"orders": ["secret"]}))
    )
    (change,) = _changes_by(tightened, "column_access")
    assert change.object == "orders.secret" and change.direction == "tightening"

    loosened = compute_access_diff(
        _ctx(Policy(denied_columns={"orders": ["secret"]})), _ctx(Policy())
    )
    (change,) = _changes_by(loosened, "column_access")
    assert change.object == "orders.secret" and change.direction == "loosening"


def test_mandatory_filter_added_is_tightening_removed_is_loosening_without_values():
    row_filter = MandatoryRowFilter(table="orders", column="tenant_id", value=42)
    added = compute_access_diff(_ctx(Policy()), _ctx(Policy(mandatory_row_filters=[row_filter])))
    (change,) = _changes_by(added, "mandatory_filter")
    assert change.change_type == "added" and change.direction == "tightening"
    assert change.object == "orders.tenant_id"

    removed = compute_access_diff(_ctx(Policy(mandatory_row_filters=[row_filter])), _ctx(Policy()))
    (change,) = _changes_by(removed, "mandatory_filter")
    assert change.change_type == "removed" and change.direction == "loosening"

    # The static filter value must never appear anywhere in the output.
    assert "42" not in removed.model_dump_json()
    assert "42" not in added.model_dump_json()


def test_mandatory_filter_source_change_is_neutral_and_shows_claim_name_not_value():
    before = MandatoryRowFilter(table="orders", column="tenant_id", value=42)
    after = MandatoryRowFilter(table="orders", column="tenant_id", from_claim="tenant")
    diff = compute_access_diff(
        _ctx(Policy(mandatory_row_filters=[before])),
        _ctx(Policy(mandatory_row_filters=[after])),
    )
    (change,) = _changes_by(diff, "mandatory_filter")
    assert change.change_type == "modified" and change.direction == "neutral"
    assert change.after == "claim:tenant"
    assert "42" not in diff.model_dump_json()


def test_mandatory_filter_static_value_change_is_reported_without_the_value():
    before = MandatoryRowFilter(table="orders", column="tenant_id", value=42)
    after = MandatoryRowFilter(table="orders", column="tenant_id", value=99)
    diff = compute_access_diff(
        _ctx(Policy(mandatory_row_filters=[before])),
        _ctx(Policy(mandatory_row_filters=[after])),
    )
    (change,) = _changes_by(diff, "mandatory_filter")
    assert change.change_type == "modified" and change.direction == "neutral"
    assert "scoping value" in change.detail
    serialized = diff.model_dump_json()
    assert "42" not in serialized and "99" not in serialized


def test_column_mask_added_is_tightening_removed_is_loosening():
    mask = ColumnMask(column="ssn", kind=ColumnMaskKind.NULL)
    tightened = compute_access_diff(_ctx(Policy()), _ctx(Policy(column_masks={"orders": [mask]})))
    (change,) = _changes_by(tightened, "column_mask")
    assert change.object == "orders.ssn" and change.change_type == "added"
    assert change.direction == "tightening"
    assert change.after == "null"

    loosened = compute_access_diff(_ctx(Policy(column_masks={"orders": [mask]})), _ctx(Policy()))
    (change,) = _changes_by(loosened, "column_mask")
    assert change.object == "orders.ssn" and change.change_type == "removed"
    assert change.direction == "loosening"
    assert change.before == "null"


def test_column_mask_kind_change_is_reported_as_neutral_not_silently_dropped():
    before = ColumnMask(column="ssn", kind=ColumnMaskKind.NULL)
    after = ColumnMask(column="ssn", kind=ColumnMaskKind.LAST, length=4)
    diff = compute_access_diff(
        _ctx(Policy(column_masks={"orders": [before]})),
        _ctx(Policy(column_masks={"orders": [after]})),
    )
    (change,) = _changes_by(diff, "column_mask")
    assert change.change_type == "modified" and change.direction == "neutral"
    assert change.before == "null" and change.after == "last:4"


def test_column_mask_table_key_is_case_insensitive():
    mask = ColumnMask(column="ssn", kind=ColumnMaskKind.NULL)
    diff = compute_access_diff(
        _ctx(Policy(column_masks={"Orders": [mask]})),
        _ctx(Policy(column_masks={"orders": [mask]})),
    )
    assert _changes_by(diff, "column_mask") == []


def test_no_mask_change_reports_nothing():
    mask = ColumnMask(column="ssn", kind=ColumnMaskKind.NULL)
    diff = compute_access_diff(
        _ctx(Policy(column_masks={"orders": [mask]})),
        _ctx(Policy(column_masks={"orders": [mask]})),
    )
    assert _changes_by(diff, "column_mask") == []


def test_column_mask_bucket_kind_display():
    before = ColumnMask(column="salary", kind=ColumnMaskKind.NULL)
    after = ColumnMask(column="salary", kind=ColumnMaskKind.BUCKET, bucket_size=10000)
    diff = compute_access_diff(
        _ctx(Policy(column_masks={"orders": [before]})),
        _ctx(Policy(column_masks={"orders": [after]})),
    )
    (change,) = _changes_by(diff, "column_mask")
    assert change.after == "bucket:10000.0"


def test_column_mask_table_specific_entry_shadowing_a_wildcard_is_not_a_false_tightening():
    # A table-specific entry always wins over "*" (Policy.column_mask's own
    # precedence rule) — moving the SAME mask from "*" to one specific table
    # is a no-op for that table but a real loosening for every OTHER table
    # the wildcard used to cover. A diff that flattens column_masks without
    # resolving through Policy.column_mask misreports the no-op as a false
    # "tightening" on the specific table (item 148 self-review finding).
    mask = ColumnMask(column="ssn", kind=ColumnMaskKind.NULL)
    diff = compute_access_diff(
        _ctx(Policy(column_masks={"*": [mask]})),
        _ctx(Policy(column_masks={"customers": [mask]})),
    )
    changes = _changes_by(diff, "column_mask")
    # customers.ssn: masked before (via "*") AND after (via the specific
    # entry) -- net effect is no change, so it must not appear at all.
    assert not any(c.object == "customers.ssn" for c in changes)
    # orders.ssn: masked before (via "*"), unmasked after (the wildcard is
    # gone and "orders" has no entry of its own) -- a genuine loosening.
    (orders_change,) = [c for c in changes if c.object == "orders.ssn"]
    assert orders_change.change_type == "removed" and orders_change.direction == "loosening"


def test_column_mask_duplicate_column_entries_resolve_first_match_like_enforcement():
    # Policy.column_mask returns the FIRST case-insensitive column match
    # within one table's list. A diff that instead flattens the list with a
    # plain dict (last write wins) would compare the wrong pair of masks and
    # miss a real enforcement-level change (item 148 self-review finding).
    before_mask = ColumnMask(column="ssn", kind=ColumnMaskKind.NULL)
    after_masks = [
        ColumnMask(column="ssn", kind=ColumnMaskKind.LAST, length=4),
        ColumnMask(column="ssn", kind=ColumnMaskKind.NULL),
    ]
    diff = compute_access_diff(
        _ctx(Policy(column_masks={"orders": [before_mask]})),
        _ctx(Policy(column_masks={"orders": after_masks})),
    )
    (change,) = _changes_by(diff, "column_mask")
    assert change.change_type == "modified" and change.direction == "neutral"
    assert change.before == "null" and change.after == "last:4"


def test_join_group_change_is_neutral():
    diff = compute_access_diff(
        _ctx(profiles={"demo": _profile(join_group=None)}),
        _ctx(profiles={"demo": _profile(join_group="shared")}),
    )
    (change,) = _changes_by(diff, "join_group")
    assert change.direction == "neutral"
    assert change.before == "demo" and change.after == "shared"


def test_connection_added_visible_is_loosening_hidden_is_neutral():
    active = _ctx(profiles={"demo": _profile()})
    visible = _ctx(profiles={"demo": _profile(), "demo2": _profile("demo2")})
    diff = compute_access_diff(active, visible)
    (change,) = _changes_by(diff, "connection_visibility")
    assert change.connection == "demo2" and change.direction == "loosening"

    hidden = _ctx(profiles={"demo": _profile(), "demo2": _profile("demo2", enabled=False)})
    diff2 = compute_access_diff(active, hidden)
    (change,) = _changes_by(diff2, "connection_visibility")
    assert change.connection == "demo2" and change.direction == "neutral"


def test_connection_removed_is_tightening():
    active = _ctx(profiles={"demo": _profile(), "demo2": _profile("demo2")})
    candidate = _ctx(profiles={"demo": _profile()})
    diff = compute_access_diff(active, candidate)
    (change,) = _changes_by(diff, "connection_visibility")
    assert change.connection == "demo2" and change.direction == "tightening"
    assert change.change_type == "removed"


def test_connection_visibility_flip_via_policy_enabled():
    active = _ctx(Policy(enabled=True))
    candidate = _ctx(Policy(enabled=False))
    diff = compute_access_diff(active, candidate)
    (change,) = _changes_by(diff, "connection_visibility")
    assert change.direction == "tightening"
    assert change.before == "visible" and change.after == "hidden"


def test_hidden_in_both_reports_no_within_connection_detail():
    active = _ctx(Policy(enabled=False, max_limit=100))
    candidate = _ctx(Policy(enabled=False, max_limit=500))
    diff = compute_access_diff(active, candidate)
    # Both hidden and enabled unchanged: the guardrail change is not surfaced
    # because the connection is unreachable in either snapshot.
    assert diff.changes == []


def test_principal_override_change_marks_analysis_incomplete():
    active = _ctx()
    candidate = _ctx(principals={"agent": {"demo": {"max_joins": 2}}})
    diff = compute_access_diff(active, candidate)
    assert diff.analysis_incomplete is True
    assert any("per-principal" in reason.lower() for reason in diff.incomplete_reasons)


def test_allowed_tables_emptiness_toggle_marks_incomplete():
    diff = compute_access_diff(
        _ctx(Policy(allowed_tables=["orders"])), _ctx(Policy(allowed_tables=[]))
    )
    assert diff.analysis_incomplete is True
    assert any("does not name" in reason for reason in diff.incomplete_reasons)


def test_truncation_keeps_loosening_first_and_flags_truncated():
    # Build many guardrail loosenings plus one tightening; cap at a small max.
    base = Policy(max_limit=100, max_joins=5)
    loosened = Policy(max_limit=500, max_joins=9)
    diff = compute_access_diff(_ctx(base), _ctx(loosened), max_changes=1)
    assert diff.truncated is True
    assert diff.summary.total == 1
    assert diff.changes[0].direction == "loosening"
    assert any("truncated" in reason.lower() for reason in diff.incomplete_reasons)


# --------------------------------------------------------------------------- #
# Guardrail coverage and direction (TODO.md item 115). The diff's whole job is
# to make a policy change's effect legible before it ships; a cap it cannot see
# is reported to the reviewer as "no change", which is worse than no diff at all.
# --------------------------------------------------------------------------- #
def _guardrail_change(before: Policy, after: Policy):
    changes = _changes_by(compute_access_diff(_ctx(before), _ctx(after)), "guardrail")
    assert len(changes) == 1, changes
    return changes[0]


def test_every_guardrail_field_produces_a_change_when_it_moves():
    """The regression that motivated item 115: nine caps were missing from the
    hand-written field list, so loosening one was diffed as nothing at all. Driven
    off `GUARDRAIL_FIELDS` itself, so a cap added later is covered automatically.
    """
    # A value that differs from the default for each field's type, chosen so the
    # change is unambiguous rather than clever.
    for field in GUARDRAIL_FIELDS:
        current = getattr(Policy(), field)
        if field == "cost_estimation_mode":
            moved = CostEstimationMode.OBSERVE
        elif isinstance(current, bool):
            moved = not current
        elif current is None:
            moved = 7
        else:
            moved = type(current)(current + 1)
        change = _guardrail_change(Policy(), Policy(**{field: moved}))
        assert change.object == field, field
        assert change.direction in ("loosening", "tightening"), (field, change.direction)


def test_window_cap_change_is_reported_not_silently_dropped():
    """The concrete false negative item 115 was filed for: raising the item-101
    window budget used to produce an empty diff."""
    change = _guardrail_change(Policy(max_window_specs=1), Policy(max_window_specs=50))
    assert change.object == "max_window_specs"
    assert change.direction == "loosening"


def test_min_group_size_direction_is_inverted():
    """A LARGER k-anonymity floor suppresses more groups, so it is tightening —
    the opposite of every other numeric cap, and the trap a naive fix falls into."""
    assert _guardrail_change(Policy(min_group_size=2), Policy(min_group_size=5)).direction == (
        "tightening"
    )
    assert _guardrail_change(Policy(min_group_size=5), Policy(min_group_size=2)).direction == (
        "loosening"
    )
    # Unset means no floor at all — the most permissive setting, so turning it on
    # is tightening even though the value goes from None to a number.
    assert _guardrail_change(Policy(), Policy(min_group_size=5)).direction == "tightening"


def test_quota_window_direction_is_inverted():
    """The same request budget spread over a longer window is a lower sustained
    rate, so a longer window is tightening."""
    assert (
        _guardrail_change(
            Policy(max_requests_per_window=100, quota_window_seconds=60),
            Policy(max_requests_per_window=100, quota_window_seconds=3600),
        ).direction
        == "tightening"
    )


def test_approval_sensitivities_change_is_reported():
    """A list, so it has no scalar permissiveness and is excluded from the derived
    set — but it gates queries behind a human, so it gets its own change."""
    change = _guardrail_change(
        Policy(),
        Policy(approval_sensitivities=[SensitivityClass.PII]),
    )
    assert change.object == "approval_sensitivities"
    assert change.direction == "tightening"
    assert change.before == "none" and change.after == "pii"


# --------------------------------------------------------------------------- #
# Purpose-bound access (TODO.md item 145) — found missing entirely by
# `security-invariant-reviewer`/`architecture-boundary-reviewer` (2026-08-05):
# `allowed_purposes`/`purpose_policies` were excluded from GUARDRAIL_FIELDS
# (correctly — they're structural, not scalar) but access_diff never actually
# diffed them, so an operator disabling the whole purpose gate reported as
# "no access change". These tests are the missing guard.
# --------------------------------------------------------------------------- #


def test_disabling_purpose_gate_entirely_is_reported_as_loosening():
    diff = compute_access_diff(
        _ctx(Policy(allowed_purposes=["fraud_review"])),
        _ctx(Policy(allowed_purposes=[])),
    )
    changes = _changes_by(diff, "purpose_access")
    (toggle,) = [c for c in changes if c.object is None]
    assert toggle.direction == "loosening"
    assert toggle.before == "required" and toggle.after == "not required"


def test_enabling_purpose_gate_from_scratch_is_reported_as_tightening():
    diff = compute_access_diff(
        _ctx(Policy(allowed_purposes=[])),
        _ctx(Policy(allowed_purposes=["fraud_review"])),
    )
    changes = _changes_by(diff, "purpose_access")
    (toggle,) = [c for c in changes if c.object is None]
    assert toggle.direction == "tightening"


def test_adding_a_permitted_purpose_is_reported_as_loosening():
    diff = compute_access_diff(
        _ctx(Policy(allowed_purposes=["fraud_review"])),
        _ctx(Policy(allowed_purposes=["fraud_review", "support"])),
    )
    changes = _changes_by(diff, "purpose_access")
    added = [c for c in changes if c.change_type == "added" and c.object == "support"]
    assert len(added) == 1
    assert added[0].direction == "loosening"


def test_removing_a_permitted_purpose_is_reported_as_tightening():
    diff = compute_access_diff(
        _ctx(Policy(allowed_purposes=["fraud_review", "support"])),
        _ctx(Policy(allowed_purposes=["fraud_review"])),
    )
    changes = _changes_by(diff, "purpose_access")
    removed = [c for c in changes if c.change_type == "removed" and c.object == "support"]
    assert len(removed) == 1
    assert removed[0].direction == "tightening"


def test_removing_a_purposes_narrowing_delta_is_reported_as_loosening():
    diff = compute_access_diff(
        _ctx(
            Policy(
                allowed_purposes=["support"],
                purpose_policies={"support": PurposePolicyDelta(denied_tables=["secrets"])},
            )
        ),
        _ctx(Policy(allowed_purposes=["support"])),
    )
    changes = _changes_by(diff, "purpose_access")
    removed = [c for c in changes if c.change_type == "removed" and c.object == "support"]
    assert len(removed) == 1
    assert removed[0].direction == "loosening"


def test_adding_a_purposes_narrowing_delta_is_reported_as_tightening():
    diff = compute_access_diff(
        _ctx(Policy(allowed_purposes=["support"])),
        _ctx(
            Policy(
                allowed_purposes=["support"],
                purpose_policies={"support": PurposePolicyDelta(denied_tables=["secrets"])},
            )
        ),
    )
    changes = _changes_by(diff, "purpose_access")
    added = [c for c in changes if c.change_type == "added" and c.object == "support"]
    assert len(added) == 1
    assert added[0].direction == "tightening"


def test_modifying_a_purposes_narrowing_delta_is_reported_not_silently_dropped():
    diff = compute_access_diff(
        _ctx(
            Policy(
                allowed_purposes=["support"],
                purpose_policies={"support": PurposePolicyDelta(denied_tables=["a"])},
            )
        ),
        _ctx(
            Policy(
                allowed_purposes=["support"],
                purpose_policies={"support": PurposePolicyDelta(denied_tables=["a", "b"])},
            )
        ),
    )
    changes = _changes_by(diff, "purpose_access")
    modified = [c for c in changes if c.change_type == "modified" and c.object == "support"]
    assert len(modified) == 1
    assert modified[0].direction == "neutral"


def test_no_purpose_change_reports_nothing():
    diff = compute_access_diff(
        _ctx(Policy(allowed_purposes=["support"])),
        _ctx(Policy(allowed_purposes=["support"])),
    )
    assert _changes_by(diff, "purpose_access") == []
