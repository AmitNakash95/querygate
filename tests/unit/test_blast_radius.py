"""Unit tests for policy-change blast-radius aggregation
(querygate/admin/blast_radius.py).

Drives the pure `compute_blast_radius_report` against constructed
registries/policy stores, mirroring tests/unit/test_config_semantic_diff.py's
approach for the underlying per-connection diff it reuses. The HTTP surface,
scope enforcement, and audit trail are covered in the integration and
security suites.
"""

from __future__ import annotations

from typing import get_args

from querygate.admin.blast_radius import _RISK_CATEGORY_PRIORITY, compute_blast_radius_report
from querygate.admin.models import SemanticChangeCategory
from querygate.catalog.loader import CatalogStore
from querygate.cli import LoadedConfigContext
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry
from querygate.policy.loader import PolicyStore
from querygate.policy.models import ColumnMask, ColumnMaskKind, MandatoryRowFilter, Policy


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


def test_no_changes_is_empty_report():
    report = compute_blast_radius_report(_ctx(), _ctx())
    assert report.baseline.changes == []
    assert report.principal_impacts == []
    assert report.principals_configured == 0
    assert report.highest_risk == []
    assert report.analysis_incomplete is False


def test_baseline_only_change_is_fleet_wide_and_highest_risk():
    active = _ctx(Policy(allowed_tables=["orders"]))
    candidate = _ctx(Policy(allowed_tables=["orders", "customers"]))

    report = compute_blast_radius_report(active, candidate)

    assert report.principals_configured == 0
    (risk,) = report.highest_risk
    assert risk.scope == "baseline"
    assert risk.principal is None
    assert risk.change.category == "table_access"
    assert risk.change.direction == "loosening"


def test_principal_only_override_change_is_scoped_to_that_principal():
    active = _ctx(principals={"agent-a": {"demo": {"denied_tables": ["customers"]}}})
    candidate = _ctx(principals={"agent-a": {"demo": {}}})

    report = compute_blast_radius_report(active, candidate)

    # No baseline-level change: default/connection layers are identical.
    assert report.baseline.changes == []
    assert report.principals_configured == 1
    assert report.principals_evaluated == 1
    assert report.principals_affected == 1
    (impact,) = report.principal_impacts
    assert impact.principal == "agent-a"
    (change,) = impact.changes
    assert change.category == "table_access" and change.direction == "loosening"

    (risk,) = report.highest_risk
    assert risk.scope == "principal" and risk.principal == "agent-a"


def test_principal_shielded_by_its_own_override_is_not_listed_in_impacts():
    # The base guardrail tightens (5 -> 2), but agent-a explicitly pins
    # max_joins to 10 in its override in *both* snapshots, so its own
    # resolved policy is unaffected by the base change.
    active = _ctx(Policy(max_joins=5), principals={"agent-a": {"demo": {"max_joins": 10}}})
    candidate = _ctx(Policy(max_joins=2), principals={"agent-a": {"demo": {"max_joins": 10}}})

    report = compute_blast_radius_report(active, candidate)

    assert report.principals_configured == 1
    assert report.principals_evaluated == 1
    assert report.principals_affected == 0
    assert report.principal_impacts == []
    # The baseline change (affecting every principal without an override) is
    # still reported in full.
    assert any(c.object == "max_joins" for c in report.baseline.changes)


def test_mandatory_filter_removal_ranks_above_table_access_and_guardrail():
    row_filter = MandatoryRowFilter(table="orders", column="tenant_id", value=42)
    active = _ctx(
        Policy(
            allowed_tables=["orders"],
            max_limit=100,
            mandatory_row_filters=[row_filter],
        )
    )
    candidate = _ctx(
        Policy(allowed_tables=["orders", "customers"], max_limit=500)
        # mandatory filter removed entirely
    )

    report = compute_blast_radius_report(active, candidate)

    categories_in_order = [item.change.category for item in report.highest_risk]
    assert categories_in_order[0] == "mandatory_filter"
    assert "table_access" in categories_in_order
    assert "guardrail" in categories_in_order
    assert categories_in_order.index("mandatory_filter") < categories_in_order.index("table_access")
    assert categories_in_order.index("table_access") < categories_in_order.index("guardrail")
    # Static filter value never leaks anywhere in the aggregated report.
    assert "42" not in report.model_dump_json()


def test_tightening_changes_are_excluded_from_highest_risk():
    active = _ctx(Policy(allowed_tables=["orders", "customers"]))
    candidate = _ctx(Policy(allowed_tables=["orders"]))  # tightened, not loosened

    report = compute_blast_radius_report(active, candidate)

    assert report.highest_risk == []
    # ...but the tightening is still visible in the full baseline detail.
    assert any(c.direction == "tightening" for c in report.baseline.changes)


def test_principal_page_bounds_work_and_exposes_next_offset():
    # Phase 2 (item 41): more principals than one page is NOT "incomplete" —
    # it's paginated. The first page evaluates `max_principals` and hands back a
    # next_principal_offset cursor; the last page's cursor is None.
    principals = {f"agent-{i}": {"demo": {"max_joins": 2}} for i in range(5)}
    active = _ctx(principals=principals)
    candidate = _ctx(principals=principals)

    page1 = compute_blast_radius_report(active, candidate, max_principals=2)
    assert page1.principals_configured == 5
    assert page1.principals_evaluated == 2
    assert page1.principal_offset == 0
    assert page1.next_principal_offset == 2
    # Pagination is not a truncation — no "configured principal not evaluated" reason.
    assert not any("were not individually evaluated" in r for r in page1.incomplete_reasons)

    page3 = compute_blast_radius_report(active, candidate, principal_offset=4, max_principals=2)
    assert page3.principal_offset == 4
    assert page3.principals_evaluated == 1  # only agent-4 left
    assert page3.next_principal_offset is None  # last page


def test_paginating_covers_every_configured_principal_exactly_once():
    principals = {f"agent-{i}": {"demo": {"max_joins": 2}} for i in range(5)}
    active = _ctx(principals=principals)
    # Loosen one principal so it produces an impact worth listing.
    cand_principals = dict(principals)
    cand_principals["agent-3"] = {"demo": {"max_joins": 9}}
    candidate = _ctx(principals=cand_principals)

    seen: set = set()
    offset = 0
    while offset is not None:
        page = compute_blast_radius_report(
            active, candidate, principal_offset=offset, max_principals=2
        )
        seen.update(i.principal for i in page.principal_impacts)
        offset = page.next_principal_offset
    assert "agent-3" in seen  # the loosened principal surfaced while paging


def test_highest_risk_cap_marks_analysis_incomplete():
    active = _ctx(Policy(allowed_tables=["orders"]))
    candidate = _ctx(
        Policy(allowed_tables=["orders", "customers"], max_limit=999, max_limit_aggregate=99999)
    )

    report = compute_blast_radius_report(active, candidate, max_highest_risk=1)

    assert len(report.highest_risk) == 1
    assert report.analysis_incomplete is True
    assert any("ranked below the top" in reason for reason in report.incomplete_reasons)


def test_every_semantic_change_category_has_a_risk_priority():
    # A category missing from `_RISK_CATEGORY_PRIORITY` silently falls to the
    # lowest-priority default in `_risk_key`, ranking a real loosening below
    # every named category regardless of severity (item 148 self-review).
    missing = set(get_args(SemanticChangeCategory)) - set(_RISK_CATEGORY_PRIORITY)
    assert missing == set()


def test_column_mask_removal_ranks_above_guardrail_loosening():
    mask = ColumnMask(column="ssn", kind=ColumnMaskKind.NULL)
    active = _ctx(Policy(allowed_tables=["orders"], max_limit=100, column_masks={"orders": [mask]}))
    candidate = _ctx(Policy(allowed_tables=["orders"], max_limit=500))

    report = compute_blast_radius_report(active, candidate)

    categories_in_order = [item.change.category for item in report.highest_risk]
    assert "column_mask" in categories_in_order
    assert "guardrail" in categories_in_order
    assert categories_in_order.index("column_mask") < categories_in_order.index("guardrail")


def test_baseline_incomplete_reason_is_preserved_when_no_principal_evaluation_needed():
    # An allow-list emptiness toggle marks the underlying diff incomplete even
    # with zero configured principals.
    active = _ctx(Policy(allowed_tables=["orders"]))
    candidate = _ctx(Policy(allowed_tables=[]))

    report = compute_blast_radius_report(active, candidate)

    assert report.analysis_incomplete is True
    assert any("does not name" in reason for reason in report.incomplete_reasons)
