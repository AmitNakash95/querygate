"""Pure resolved-behavior diff between two loaded config contexts.

No I/O, no process globals, no audit: admin/service.py loads the active and
candidate configs through the same isolated-loader path item 39's candidate
simulation uses, then hands the two `LoadedConfigContext`s here. Keeping the
classification logic pure lets it be unit-tested directly against constructed
registries/policy stores.

Phase 1 scope (`SemanticAccessDiff.evaluation_scope == "connection_baseline"`):
called with no `principal` argument (the default), the default and
per-connection policy layers are resolved with no principal applied. A change
that lives purely in a per-principal override is detected but not itemized —
`analysis_incomplete` is set instead. The diff compares *resolved behavior*,
never YAML text, and never places a static filter value, resolved secret, or
predicate value into its output.

`compute_access_diff` also accepts an explicit `principal`, which resolves
every layer (default/connection/principal) for that one caller instead of the
connection baseline — this is the primitive `admin/blast_radius.py` (item 41)
reuses per configured principal rather than re-implementing the same
classification logic against a different resolution.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Dict, Optional

from querygate.admin.models import (
    SemanticAccessChange,
    SemanticAccessDiff,
    SemanticDiffSummary,
)
from querygate.connections.models import ConnectionProfile
from querygate.core.auth import Principal
from querygate.policy.models import (
    GUARDRAIL_FIELDS,
    INVERTED_GUARDRAIL_FIELDS,
    CostEstimationMode,
    Policy,
)

if TYPE_CHECKING:
    from querygate.cli import LoadedConfigContext

DEFAULT_MAX_CHANGES = 500

# Guardrail fields come from `Policy` itself (item 115) — one derivation shared
# with the effective-guardrails view, the help API and the admin UI, so a cap
# added to Policy is diffed here automatically instead of being invisible until
# someone notices. Direction is derived from a single "permissiveness"
# comparator (see `_guardrail_perm`): a change that raises permissiveness is
# loosening, one that lowers it is tightening. For the optional caps (queue
# depth, cost-estimation thresholds), an unset value means "unlimited" and is
# therefore the most permissive.
_GUARDRAIL_FIELDS = GUARDRAIL_FIELDS


def _guardrail_perm(field: str, value: object) -> float:
    """Higher == more permissive / less restrictive."""
    if field == "log_query_literals":
        return 1.0 if value else 0.0
    if field == "cost_estimation_mode":
        # OBSERVE never blocks a query; ENFORCE can. OBSERVE is more permissive.
        return 1.0 if value == CostEstimationMode.OBSERVE else 0.0
    # Numeric caps. The optional ones use None to mean "unlimited"/"disabled",
    # which is the most permissive setting either way.
    if value is None:
        return math.inf
    # A few caps run the other way: a larger `min_group_size` suppresses MORE
    # groups, and the same request budget over a longer `quota_window_seconds`
    # is a lower sustained rate. Reporting those as "loosening" would tell a
    # reviewer the exact opposite of what the change does.
    if field in INVERTED_GUARDRAIL_FIELDS:
        return -float(value)
    return float(value)


def _guardrail_display(value: object) -> str:
    if value is None:
        return "unlimited"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, CostEstimationMode):
        return value.value
    return str(value)


def _named_tables(profile: ConnectionProfile, policy: Policy) -> set[str]:
    """Every table the profile/policy explicitly names.

    Table access can also be affected for tables nobody names (an empty
    allowed-tables list means "no restriction"); a toggle of that emptiness is
    surfaced separately as an incompleteness reason, since the affected set
    can't be enumerated without live schema reflection.
    """
    names: set[str] = set(profile.known_tables)
    names.update(policy.allowed_tables)
    names.update(policy.denied_tables)
    names.update(key for key in policy.allowed_columns if key != "*")
    names.update(key for key in policy.denied_columns if key != "*")
    names.update(row_filter.table for row_filter in policy.mandatory_row_filters)
    return names


def _named_columns_for_table(policy: Policy, table: str) -> set[str]:
    target = table.casefold()
    cols: set[str] = set()
    for mapping in (policy.allowed_columns, policy.denied_columns):
        for key, values in mapping.items():
            if key == "*" or key.casefold() == target:
                cols.update(values)
    for row_filter in policy.mandatory_row_filters:
        if row_filter.table.casefold() == target:
            cols.add(row_filter.column)
    return cols


def _dedupe_casefold(names: set[str]) -> list[str]:
    """One display form per case-insensitive identifier, sorted deterministically."""
    by_fold: Dict[str, str] = {}
    for name in names:
        by_fold.setdefault(name.casefold(), name)
    return [by_fold[key] for key in sorted(by_fold)]


class _Diff:
    def __init__(self, max_changes: int) -> None:
        self.changes: list[SemanticAccessChange] = []
        self.incomplete_reasons: list[str] = []
        self.max_changes = max_changes

    def add(self, change: SemanticAccessChange) -> None:
        self.changes.append(change)

    def note(self, reason: str) -> None:
        if reason not in self.incomplete_reasons:
            self.incomplete_reasons.append(reason)


def _baseline_visible(profile: ConnectionProfile, policy: Policy) -> bool:
    return profile.enabled and policy.enabled


def _diff_guardrails(diff: _Diff, connection: str, before: Policy, after: Policy) -> None:
    for field in _GUARDRAIL_FIELDS:
        before_val = getattr(before, field)
        after_val = getattr(after, field)
        if before_val == after_val:
            continue
        before_perm = _guardrail_perm(field, before_val)
        after_perm = _guardrail_perm(field, after_val)
        if after_perm > before_perm:
            direction = "loosening"
        elif after_perm < before_perm:
            direction = "tightening"
        else:
            direction = "neutral"
        diff.add(
            SemanticAccessChange(
                category="guardrail",
                connection=connection,
                object=field,
                change_type="modified",
                direction=direction,
                before=_guardrail_display(before_val),
                after=_guardrail_display(after_val),
                detail=(
                    f"Guardrail {field!r} on connection {connection!r} "
                    f"{direction.replace('ing', 'ed')} from "
                    f"{_guardrail_display(before_val)} to {_guardrail_display(after_val)}."
                ),
            )
        )

    # `approval_sensitivities` is a LIST of catalog labels, so it has no scalar
    # permissiveness and is excluded from the derived guardrail set — but a
    # change to it still gates queries behind a human, so it gets its own change
    # rather than being invisible (item 115).
    if set(before.approval_sensitivities) != set(after.approval_sensitivities):
        before_labels = sorted(label.value for label in before.approval_sensitivities)
        after_labels = sorted(label.value for label in after.approval_sensitivities)
        if len(after_labels) > len(before_labels):
            direction = "tightening"
        elif len(after_labels) < len(before_labels):
            direction = "loosening"
        else:
            direction = "neutral"
        diff.add(
            SemanticAccessChange(
                category="guardrail",
                connection=connection,
                object="approval_sensitivities",
                change_type="modified",
                direction=direction,
                before=", ".join(before_labels) or "none",
                after=", ".join(after_labels) or "none",
                detail=(
                    f"Approval trigger 'approval_sensitivities' on connection {connection!r} "
                    f"{direction.replace('ing', 'ed')}: queries touching these catalog "
                    "sensitivity labels require human approval."
                ),
            )
        )


def _diff_tables(
    diff: _Diff,
    connection: str,
    before_profile: ConnectionProfile,
    before: Policy,
    after_profile: ConnectionProfile,
    after: Policy,
) -> set[str]:
    """Diff table accessibility; return the set of tables allowed in both snapshots
    (the only ones for which column-level diffing is meaningful)."""
    if bool(before.allowed_tables) != bool(after.allowed_tables):
        diff.note(
            f"Connection {connection!r} changed its allowed-tables list between restricted "
            "and unrestricted; tables the policy does not name may also be affected and are "
            "not individually listed here."
        )
    universe = _named_tables(before_profile, before) | _named_tables(after_profile, after)
    allowed_in_both: set[str] = set()
    for table in _dedupe_casefold(universe):
        before_allowed = before.table_allowed(table)
        after_allowed = after.table_allowed(table)
        if before_allowed and after_allowed:
            allowed_in_both.add(table)
        if before_allowed == after_allowed:
            continue
        diff.add(
            SemanticAccessChange(
                category="table_access",
                connection=connection,
                object=table,
                change_type="added" if after_allowed else "removed",
                direction="loosening" if after_allowed else "tightening",
                before="allowed" if before_allowed else "denied",
                after="allowed" if after_allowed else "denied",
                detail=(
                    f"Table {table!r} is now accessible on connection {connection!r}."
                    if after_allowed
                    else f"Table {table!r} is no longer accessible on connection {connection!r}."
                ),
            )
        )
    return allowed_in_both


def _diff_columns(
    diff: _Diff, connection: str, tables: set[str], before: Policy, after: Policy
) -> None:
    wildcard_toggle = False
    for table in sorted(tables, key=str.casefold):
        universe = _named_columns_for_table(before, table) | _named_columns_for_table(after, table)
        for column in _dedupe_casefold(universe):
            before_allowed = before.column_allowed(table, column)
            after_allowed = after.column_allowed(table, column)
            if before_allowed == after_allowed:
                continue
            diff.add(
                SemanticAccessChange(
                    category="column_access",
                    connection=connection,
                    object=f"{table}.{column}",
                    change_type="added" if after_allowed else "removed",
                    direction="loosening" if after_allowed else "tightening",
                    before="allowed" if before_allowed else "denied",
                    after="allowed" if after_allowed else "denied",
                    detail=(
                        f"Column {table}.{column} is now accessible on connection {connection!r}."
                        if after_allowed
                        else f"Column {table}.{column} is no longer accessible on "
                        f"connection {connection!r}."
                    ),
                )
            )
    before_wild = bool(before.allowed_columns.get("*"))
    after_wild = bool(after.allowed_columns.get("*"))
    if before_wild != after_wild:
        wildcard_toggle = True
    if wildcard_toggle:
        diff.note(
            f"Connection {connection!r} changed a wildcard allowed-columns list between "
            "restricted and unrestricted; columns the policy does not name may also be "
            "affected and are not individually listed here."
        )


def _filter_key(row_filter) -> tuple[str, str]:
    return (row_filter.table.casefold(), row_filter.column.casefold())


def _filter_source(row_filter) -> str:
    # Never the static `value` — only the source *kind* and, for a claim, the
    # claim *name* (a policy identifier, not a resolved value).
    if row_filter.from_claim is not None:
        return f"claim:{row_filter.from_claim}"
    return "configured_literal"


def _diff_mandatory_filters(diff: _Diff, connection: str, before: Policy, after: Policy) -> None:
    before_map = {_filter_key(f): f for f in before.mandatory_row_filters}
    after_map = {_filter_key(f): f for f in after.mandatory_row_filters}
    for key in sorted(set(before_map) | set(after_map)):
        b = before_map.get(key)
        a = after_map.get(key)
        if b is not None and a is None:
            diff.add(
                SemanticAccessChange(
                    category="mandatory_filter",
                    connection=connection,
                    object=f"{b.table}.{b.column}",
                    change_type="removed",
                    direction="loosening",
                    before=_filter_source(b),
                    after=None,
                    detail=(
                        f"Mandatory row-filter on {b.table}.{b.column} was removed — rows "
                        "previously scoped by it are no longer filtered."
                    ),
                )
            )
        elif b is None and a is not None:
            diff.add(
                SemanticAccessChange(
                    category="mandatory_filter",
                    connection=connection,
                    object=f"{a.table}.{a.column}",
                    change_type="added",
                    direction="tightening",
                    before=None,
                    after=_filter_source(a),
                    detail=f"Mandatory row-filter on {a.table}.{a.column} was added.",
                )
            )
        elif b is not None and a is not None:
            source_changed = _filter_source(b) != _filter_source(a)
            # A static-value change (e.g. tenant_id 42 -> 99) re-scopes which
            # rows every caller sees, so it must be reported — but the values
            # themselves are compared only server-side and never emitted.
            value_changed = b.from_claim is None and a.from_claim is None and b.value != a.value
            if source_changed or value_changed:
                detail = (
                    f"Mandatory row-filter on {a.table}.{a.column} changed its value source."
                    if source_changed
                    else f"Mandatory row-filter on {a.table}.{a.column} changed its scoping value."
                )
                diff.add(
                    SemanticAccessChange(
                        category="mandatory_filter",
                        connection=connection,
                        object=f"{a.table}.{a.column}",
                        change_type="modified",
                        direction="neutral",
                        before=_filter_source(b),
                        after=_filter_source(a),
                        detail=detail,
                    )
                )


def _diff_purposes(diff: _Diff, connection: str, before: Policy, after: Policy) -> None:
    """Purpose-bound access (item 145): `allowed_purposes` empty means the
    gate itself is off for this connection — the same "empty allow-list =
    unrestricted" convention `_diff_tables` already reports a toggle for — so
    a transition to/from empty is reported as the loosest/tightest possible
    change here, not left invisible (the exact gap `security-invariant-
    reviewer` and `architecture-boundary-reviewer` found on 2026-08-05: an
    operator could otherwise delete `allowed_purposes` and have the whole
    purpose gate disappear with the diff reporting "no access change").

    A `purpose_policies` entry that's added/removed is reported the same way
    `_diff_mandatory_filters` reports a filter add/remove; one that's merely
    MODIFIED is surfaced (never silently invisible) but not sub-field-diffed
    — like `_diff_mandatory_filters`'s own "value_changed" case, direction is
    reported `neutral` rather than guessed, since classifying a delta change
    as tightening/loosening needs the same fine-grained tables/columns/masks
    comparison `_diff_tables`/`_diff_columns` already give the BASE policy;
    doing that for every purpose's delta too is tracked as a follow-up
    (TODO.md item 148), not attempted here under review pressure.
    """
    before_purposes = set(before.allowed_purposes)
    after_purposes = set(after.allowed_purposes)
    if bool(before_purposes) != bool(after_purposes):
        gate_now_on = bool(after_purposes)
        diff.add(
            SemanticAccessChange(
                category="purpose_access",
                connection=connection,
                object=None,
                change_type="modified",
                direction="tightening" if gate_now_on else "loosening",
                before="required" if before_purposes else "not required",
                after="required" if after_purposes else "not required",
                detail=(
                    f"Connection {connection!r} now requires every query to declare a purpose."
                    if gate_now_on
                    else f"Connection {connection!r} no longer requires a declared purpose — "
                    "every purpose-bound narrowing rule for this connection is now inert."
                ),
            )
        )
    for purpose in sorted(before_purposes - after_purposes):
        diff.add(
            SemanticAccessChange(
                category="purpose_access",
                connection=connection,
                object=purpose,
                change_type="removed",
                direction="tightening",
                before="allowed",
                after="not permitted",
                detail=f"Purpose {purpose!r} is no longer permitted on connection {connection!r}.",
            )
        )
    for purpose in sorted(after_purposes - before_purposes):
        diff.add(
            SemanticAccessChange(
                category="purpose_access",
                connection=connection,
                object=purpose,
                change_type="added",
                direction="loosening",
                before="not permitted",
                after="allowed",
                detail=f"Purpose {purpose!r} is now permitted on connection {connection!r}.",
            )
        )

    before_deltas = before.purpose_policies
    after_deltas = after.purpose_policies
    for purpose in sorted(set(before_deltas) | set(after_deltas)):
        b = before_deltas.get(purpose)
        a = after_deltas.get(purpose)
        if b == a:
            continue
        if b is not None and a is None:
            diff.add(
                SemanticAccessChange(
                    category="purpose_access",
                    connection=connection,
                    object=purpose,
                    change_type="removed",
                    direction="loosening",
                    before="narrowing configured",
                    after=None,
                    detail=(
                        f"The narrowing rules for purpose {purpose!r} on connection "
                        f"{connection!r} were removed — declaring this purpose no longer "
                        "narrows access beyond the base policy."
                    ),
                )
            )
        elif b is None and a is not None:
            diff.add(
                SemanticAccessChange(
                    category="purpose_access",
                    connection=connection,
                    object=purpose,
                    change_type="added",
                    direction="tightening",
                    before=None,
                    after="narrowing configured",
                    detail=(
                        f"Purpose {purpose!r} on connection {connection!r} now narrows access "
                        "beyond the base policy when declared."
                    ),
                )
            )
        else:
            diff.add(
                SemanticAccessChange(
                    category="purpose_access",
                    connection=connection,
                    object=purpose,
                    change_type="modified",
                    direction="neutral",
                    before="narrowing configured",
                    after="narrowing configured",
                    detail=(
                        f"The narrowing rules for purpose {purpose!r} on connection "
                        f"{connection!r} changed — review this connection's purpose_policies "
                        "configuration for specifics."
                    ),
                )
            )


def _diff_join_group(
    diff: _Diff, connection: str, before: ConnectionProfile, after: ConnectionProfile
) -> None:
    before_group = before.effective_join_group()
    after_group = after.effective_join_group()
    if before_group == after_group:
        return
    diff.add(
        SemanticAccessChange(
            category="join_group",
            connection=connection,
            object=None,
            change_type="modified",
            direction="neutral",
            before=before_group,
            after=after_group,
            detail=(
                f"Connection {connection!r} cross-connection join group changed from "
                f"{before_group!r} to {after_group!r}."
            ),
        )
    )


def _diff_connection(
    diff: _Diff,
    connection: str,
    active: "LoadedConfigContext",
    candidate: "LoadedConfigContext",
    principal: Optional[Principal],
) -> None:
    active_present = connection in active.registry.all_ids()
    candidate_present = connection in candidate.registry.all_ids()

    a_profile = active.registry.get(connection) if active_present else None
    c_profile = candidate.registry.get(connection) if candidate_present else None
    a_policy = active.policy_store.get(connection, principal=principal) if active_present else None
    c_policy = (
        candidate.policy_store.get(connection, principal=principal) if candidate_present else None
    )

    a_visible = _baseline_visible(a_profile, a_policy) if a_profile and a_policy else False
    c_visible = _baseline_visible(c_profile, c_policy) if c_profile and c_policy else False

    if active_present and not candidate_present:
        diff.add(
            SemanticAccessChange(
                category="connection_visibility",
                connection=connection,
                change_type="removed",
                direction="tightening",
                before="visible" if a_visible else "hidden",
                after="absent",
                detail=f"Connection {connection!r} was removed from the configuration.",
            )
        )
        return
    if candidate_present and not active_present:
        diff.add(
            SemanticAccessChange(
                category="connection_visibility",
                connection=connection,
                change_type="added",
                direction="loosening" if c_visible else "neutral",
                before="absent",
                after="visible" if c_visible else "hidden",
                detail=(
                    f"Connection {connection!r} was added and is visible to callers."
                    if c_visible
                    else f"Connection {connection!r} was added but is not visible to any caller."
                ),
            )
        )
        return

    # Present in both.
    assert (
        a_profile and a_policy and c_profile and c_policy
    )  # nosec B101 — internal invariant for the "present in both" branch, not security control flow
    if a_visible != c_visible:
        diff.add(
            SemanticAccessChange(
                category="connection_visibility",
                connection=connection,
                change_type="modified",
                direction="loosening" if c_visible else "tightening",
                before="visible" if a_visible else "hidden",
                after="visible" if c_visible else "hidden",
                detail=(
                    f"Connection {connection!r} is now visible to callers."
                    if c_visible
                    else f"Connection {connection!r} is no longer visible to callers."
                ),
            )
        )

    # Within-connection detail only matters while the connection stays
    # reachable in both snapshots — a visibility flip is the headline otherwise.
    if a_visible and c_visible:
        _diff_guardrails(diff, connection, a_policy, c_policy)
        allowed_in_both = _diff_tables(diff, connection, a_profile, a_policy, c_profile, c_policy)
        _diff_columns(diff, connection, allowed_in_both, a_policy, c_policy)
        _diff_mandatory_filters(diff, connection, a_policy, c_policy)
        _diff_purposes(diff, connection, a_policy, c_policy)
        _diff_join_group(diff, connection, a_profile, c_profile)


_DIRECTION_PRIORITY = {"loosening": 0, "tightening": 1, "neutral": 2}
_CATEGORY_PRIORITY = {
    "connection_visibility": 0,
    "mandatory_filter": 1,
    "purpose_access": 2,
    "table_access": 3,
    "column_access": 4,
    "guardrail": 5,
    "join_group": 6,
}


def compute_access_diff(
    active: "LoadedConfigContext",
    candidate: "LoadedConfigContext",
    *,
    principal: Optional[Principal] = None,
    max_changes: int = DEFAULT_MAX_CHANGES,
) -> SemanticAccessDiff:
    diff = _Diff(max_changes=max_changes)

    if principal is None and (
        active.policy_store.principal_override_map()
        != candidate.policy_store.principal_override_map()
    ):
        diff.note(
            "Per-principal policy overrides changed; per-principal impact is resolved "
            "separately by the blast-radius endpoint (TODO item 41), which evaluates each "
            "configured principal individually. Only the default and per-connection layers "
            "are analyzed here."
        )

    connections = sorted(set(active.registry.all_ids()) | set(candidate.registry.all_ids()))
    for connection in connections:
        _diff_connection(diff, connection, active, candidate, principal)

    # Loosening first (most security-relevant), so that if the list is
    # truncated the highest-risk changes survive.
    diff.changes.sort(
        key=lambda c: (
            _DIRECTION_PRIORITY[c.direction],
            c.connection.casefold(),
            _CATEGORY_PRIORITY[c.category],
            (c.object or "").casefold(),
        )
    )

    truncated = False
    if len(diff.changes) > max_changes:
        truncated = True
        diff.changes = diff.changes[:max_changes]
        diff.note(f"The change list was truncated at {max_changes} entries.")

    summary = SemanticDiffSummary(total=len(diff.changes))
    for change in diff.changes:
        setattr(summary, change.direction, getattr(summary, change.direction) + 1)

    return SemanticAccessDiff(
        changes=diff.changes,
        summary=summary,
        analysis_incomplete=bool(diff.incomplete_reasons),
        incomplete_reasons=diff.incomplete_reasons,
        truncated=truncated,
    )
