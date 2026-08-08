"""Aggregates the resolved-access diff (admin/access_diff.py) across every
explicitly configured principal, to answer "how many callers, and how
severely, does this candidate change affect?" before it's staged.

Every principal without an explicit `principals:` override inherits the
default/connection layers unmodified, so the connection-baseline diff already
covers them by construction — only principals with an explicit override in
either the active or candidate policy can possibly diverge from that
baseline, and those are exactly the ones itemized here individually. This is
the "later phase" item 40's own diff module said per-principal resolution
would need, and item 41's own scope bounds it deliberately: bounded work
(a capped number of principals, each with its own capped change list) rather
than unbounded fan-out over every conceivable subject.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from querygate.admin.access_diff import compute_access_diff
from querygate.admin.models import (
    BlastRadiusPrincipalImpact,
    PolicyBlastRadiusReport,
    RankedBlastRadiusChange,
    SemanticAccessChange,
)
from querygate.core.auth import Principal

if TYPE_CHECKING:
    from querygate.cli import LoadedConfigContext

DEFAULT_MAX_PRINCIPALS = 100
DEFAULT_MAX_CHANGES_PER_PRINCIPAL = 100
DEFAULT_MAX_HIGHEST_RISK = 25

# Only loosening changes are risk-ranked at all — a tightening or neutral
# change is not a blast-radius concern. Lower number == higher priority.
#
# Every `SemanticChangeCategory` literal must have an entry here — a category
# with no entry silently falls to `_risk_key`'s `.get(..., 4)` default,
# ranking it below every named category regardless of how severe a loosening
# in it actually is. `test_blast_radius.py::test_every_semantic_change_category_has_a_risk_priority`
# fails until a new category is added below (found missing for `column_mask`
# by `architecture-boundary-reviewer`, 2026-08-05, item 148 self-review;
# `purpose_access` was independently found missing the same way while fixing it).
_RISK_CATEGORY_PRIORITY = {
    "mandatory_filter": 0,
    # A purpose-gate loosening (e.g. disabling the gate entirely) can silently
    # switch off every purpose-scoped narrowing rule for a connection at
    # once — the same fleet-wide severity as removing a mandatory filter.
    "purpose_access": 0,
    "connection_visibility": 1,
    "table_access": 1,
    "column_access": 1,
    # A mask removal reveals a real column value, the same severity class as
    # an outright column-access grant.
    "column_mask": 1,
    "guardrail": 2,
    "join_group": 3,
}


def _risk_key(item: RankedBlastRadiusChange) -> tuple:
    change = item.change
    return (
        _RISK_CATEGORY_PRIORITY.get(change.category, 4),
        # Fleet-wide (baseline) changes rank ahead of a same-category change
        # scoped to a single principal, since they affect strictly more callers.
        0 if item.scope == "baseline" else 1,
        change.connection.casefold(),
        (item.principal or "").casefold(),
        (change.object or "").casefold(),
    )


def _ranked(scope, principal, changes: list[SemanticAccessChange]) -> list[RankedBlastRadiusChange]:
    return [
        RankedBlastRadiusChange(scope=scope, principal=principal, change=change)
        for change in changes
        if change.direction == "loosening"
    ]


def compute_blast_radius_report(
    active: "LoadedConfigContext",
    candidate: "LoadedConfigContext",
    *,
    principal_offset: int = 0,
    max_principals: int = DEFAULT_MAX_PRINCIPALS,
    max_changes_per_principal: int = DEFAULT_MAX_CHANGES_PER_PRINCIPAL,
    max_highest_risk: int = DEFAULT_MAX_HIGHEST_RISK,
) -> PolicyBlastRadiusReport:
    baseline = compute_access_diff(active, candidate)

    configured = sorted(
        set(active.policy_store.principal_override_map())
        | set(candidate.policy_store.principal_override_map())
    )
    # Phase 2 (TODO item 41): deterministically-sorted configured principals are
    # evaluated one PAGE at a time. `principal_offset` is the cursor into that
    # list and `max_principals` the page size; `next_principal_offset` (below)
    # tells a caller with more configured principals than fit in one page how to
    # fetch the next page — so a large deployment can cover every principal across
    # requests instead of the first page being silently dropped as "incomplete".
    offset = max(principal_offset, 0)
    evaluated_subjects = configured[offset : offset + max_principals]
    next_offset = offset + max_principals
    next_principal_offset = next_offset if next_offset < len(configured) else None

    incomplete_reasons = list(baseline.incomplete_reasons)

    impacts: list[BlastRadiusPrincipalImpact] = []
    ranked = _ranked("baseline", None, baseline.changes)

    for subject in evaluated_subjects:
        principal = Principal(subject=subject, auth_method="admin_blast_radius_analysis")
        principal_diff = compute_access_diff(
            active, candidate, principal=principal, max_changes=max_changes_per_principal
        )
        if principal_diff.changes or principal_diff.analysis_incomplete:
            impacts.append(
                BlastRadiusPrincipalImpact(
                    principal=subject,
                    changes=principal_diff.changes,
                    summary=principal_diff.summary,
                    analysis_incomplete=principal_diff.analysis_incomplete,
                    incomplete_reasons=principal_diff.incomplete_reasons,
                    truncated=principal_diff.truncated,
                )
            )
        ranked.extend(_ranked("principal", subject, principal_diff.changes))

    ranked.sort(key=_risk_key)
    highest_risk = ranked[:max_highest_risk]
    if len(ranked) > max_highest_risk:
        incomplete_reasons.append(
            f"{len(ranked) - max_highest_risk} additional access-expanding change(s) were "
            f"ranked below the top {max_highest_risk} shown in highest_risk; the full set "
            "remains visible in baseline and principal_impacts."
        )

    return PolicyBlastRadiusReport(
        baseline=baseline,
        principal_impacts=impacts,
        principals_configured=len(configured),
        principals_evaluated=len(evaluated_subjects),
        principals_affected=len(impacts),
        highest_risk=highest_risk,
        analysis_incomplete=bool(incomplete_reasons),
        incomplete_reasons=incomplete_reasons,
        principal_offset=offset,
        next_principal_offset=next_principal_offset,
    )
