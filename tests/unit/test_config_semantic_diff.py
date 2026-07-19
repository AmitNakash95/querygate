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
from querygate.policy.models import MandatoryRowFilter, Policy


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
