"""Unit tests for policy enforcement — caps and table/column allow-deny."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from querygate.connections.models import ConnectionProfile
from querygate.core.exceptions import PolicyViolationError
from querygate.policy.models import ColumnMask, MandatoryRowFilter, Policy, PurposePolicyDelta
from querygate.query_ast.models import JoinSpec, Predicate, StructuredQuery, WhereGroup
from querygate.validation import policy_validation
from querygate.validation.policy_validation import (
    resolve_purpose_policy,
    validate_batch_size,
    validate_policy,
    validate_structural_caps,
)

# --------------------------------------------------------------------------- #
# Cross-connection join allow-deny / mask enforcement (TODO.md item 156)      #
# --------------------------------------------------------------------------- #


def _cross_connection_query(where_customers_email: bool = False) -> StructuredQuery:
    """`orders` on the primary connection, `customers` joined in from
    connection `other` — the exact shape `resolve_query_table_connections`
    (schema validation, item 155's own precedent) resolves and enforces the
    `join_group` rule against. `customers` carries no alias, so the effective
    name used in `scope_connections` is the table name itself."""
    return StructuredQuery(
        from_table="orders",
        select=["orders.id", "customers.email"],
        where=(
            WhereGroup(and_terms=[Predicate(col="customers.email", op="eq", value="a@b.com")])
            if where_customers_email
            else None
        ),
        joins=[
            JoinSpec(
                table="customers",
                on=["orders.customer_id", "customers.id"],
                connection="other",
            )
        ],
    )


def _connection_resolver(policies: dict):
    """A minimal `ConnectionResolver` (see `schema_validation.ConnectionResolver`)
    for tests that don't need a real `ConnectionRegistry`/`PolicyStore` — just a
    fixed connection_id -> Policy mapping. The profile half of the return tuple
    is never read by `resolve_table_policies`."""

    def resolve(connection_id, principal=None):
        return None, policies[connection_id]

    return resolve


def test_cross_connection_join_denied_table_enforced_from_joined_connection_only():
    """The item's own headline scenario: `customers` is denied ONLY by the
    JOINED connection's ('other') Policy — the primary connection's Policy has
    no opinion on it at all. Must still be rejected once the per-scope
    connection map is threaded in."""
    query = _cross_connection_query()
    primary_policy = Policy()
    scope_connections = {id(query): {"customers": "other"}}
    resolver = _connection_resolver({"other": Policy(denied_tables=["customers"])})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(
            query,
            primary_policy,
            connection_id="primary",
            scope_connections=scope_connections,
            connection_resolver=resolver,
        )


def test_cross_connection_join_denied_table_never_enforced_without_the_map():
    """Pins the exact pre-156 bug as a permanent regression, mirroring item
    155's identical pin for the sensitivity trigger: omitting `scope_
    connections` — the shape of every call site before this item, and still
    the default for a caller that doesn't pass one — resolves every table
    against the query's own top-level connection, so a deny rule that lives
    only on the JOINED connection's Policy is never consulted and the query
    passes."""
    query = _cross_connection_query()
    primary_policy = Policy()
    validate_policy(query, primary_policy, connection_id="primary")  # no raise


def test_cross_connection_join_denied_table_self_derived_when_only_the_resolver_is_given():
    """TODO.md item 160 finding 3 (maintainer-approved 2026-08-09): unlike
    the test above (which passes NEITHER `scope_connections` nor
    `connection_resolver`, and correctly still doesn't raise — there is
    nothing to derive from), a caller that passes `connection_resolver` but
    forgets `scope_connections` is the actual footgun this finding closes.
    The map is now self-derived from the resolver, so the joined
    connection's deny rule is enforced exactly as if the caller had passed
    `scope_connections` explicitly."""
    query = _cross_connection_query()
    primary_policy = Policy()
    other_policy = Policy(denied_tables=["customers"])
    # Unlike _connection_resolver (whose profile half is always None, fine
    # for tests that supply scope_connections directly), self-derivation
    # exercises resolve_query_table_connections for real, which needs a
    # genuine ConnectionProfile to check join_group/dialect against.
    profiles = {
        "primary": ConnectionProfile(
            id="primary",
            dialect="postgresql",
            connection_string="postgresql+asyncpg://user:pass@host/primary_db",
            join_group="grp",
        ),
        "other": ConnectionProfile(
            id="other",
            dialect="postgresql",
            connection_string="postgresql+asyncpg://user:pass@host/other_db",
            join_group="grp",
        ),
    }
    policies = {"primary": primary_policy, "other": other_policy}

    def resolver(connection_id, principal=None):
        return profiles[connection_id], policies[connection_id]

    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(
            query,
            primary_policy,
            connection_id="primary",
            connection_resolver=resolver,
            # scope_connections intentionally omitted.
        )


def test_self_derivation_runs_after_the_cheap_structural_caps_not_before():
    """Security-invariant-reviewer, 2026-08-09 (SIR-160F3-1): the self-derive
    call in `validate_policy` must run AFTER `validate_structural_caps`
    (`max_cte_count`/`max_subquery_depth`) — the cheap, connection-registry-
    free bound — never before it, mirroring the ordering
    `test_structural_caps_reject_before_any_per_scope_policy_lookup`
    (tests/unit/test_service.py) already pins one layer up at the service
    level. That test alone doesn't cover this: on the service path
    `scope_connections` is always already resolved, so `validate_policy`'s
    OWN derive branch never executes there regardless of where it sits in
    the function body. Pinned directly here with a call-count assertion, not
    timing: an over-cap query must be rejected WITHOUT
    `resolve_scope_connections` ever being called."""
    query = StructuredQuery(
        ctes=[{"name": "totals", "query": {"from": "orders", "select": ["orders.id"]}}],
        from_table="totals",
        select=["totals.id"],
    )
    policy = Policy(max_cte_count=0)
    resolver = _connection_resolver({})  # would KeyError if ever called

    with patch.object(policy_validation, "resolve_scope_connections", MagicMock()) as mock_resolve:
        with pytest.raises(PolicyViolationError, match="ctes exceeds max of 0"):
            validate_policy(query, policy, connection_id="primary", connection_resolver=resolver)

    mock_resolve.assert_not_called()


def test_an_empty_scope_connections_map_is_not_treated_as_missing():
    """Security-invariant-reviewer, 2026-08-09 (test-contract F3): the
    self-derive guard is `scope_connections is None`, deliberately NOT a
    falsy check — `admin/service.py`'s `simulate_candidate_policy` passes
    `scope_connections={}` (not `None`) after catching a resolution failure,
    specifically so `validate_policy` runs against "no map" rather than
    re-deriving with the same resolver that already failed. A future
    simplification to `if not scope_connections and connection_resolver...`
    would silently break that call site by re-invoking the resolver. Pinned
    directly: an explicit `{}` must never reach the resolver, even though it
    is falsy exactly like `None`."""
    query = _cross_connection_query()
    policy = Policy()

    def resolver(connection_id, principal=None):
        raise AssertionError("resolver must not be called when scope_connections={} is explicit")

    # No PolicyViolationError: with an explicit (empty) map, `customers`
    # resolves against no override at all, i.e. exactly the primary policy —
    # which has no opinion on `customers`, so this must not raise, and must
    # not call the resolver either.
    validate_policy(
        query, policy, connection_id="primary", scope_connections={}, connection_resolver=resolver
    )


def test_cross_connection_join_denied_table_still_enforced_from_primary_connection():
    """The mirror case, and the mutation guard against item 155's own
    hard-won lesson: a REPLACE of the primary policy with the joined
    connection's (instead of consulting BOTH) would silently stop enforcing a
    primary-side deny rule the moment the same table is reached through a
    cross-connection join. Here 'other' has no opinion at all — only the
    primary connection denies `customers` — so this must still raise."""
    query = _cross_connection_query()
    primary_policy = Policy(denied_tables=["customers"])
    scope_connections = {id(query): {"customers": "other"}}
    resolver = _connection_resolver({"other": Policy()})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(
            query,
            primary_policy,
            connection_id="primary",
            scope_connections=scope_connections,
            connection_resolver=resolver,
        )


def test_cross_connection_join_denied_column_from_each_connection_independently():
    """Both connections have DIFFERENT rules — primary denies `orders.id`,
    'other' denies `customers.email` — and both must be enforced independently
    against the table each rule's own connection actually governs."""
    only_primary_denies = _cross_connection_query()
    scope_connections = {id(only_primary_denies): {"customers": "other"}}
    resolver = _connection_resolver({"other": Policy(denied_columns={"customers": ["email"]})})
    with pytest.raises(PolicyViolationError, match=r"customers\.email.*not accessible"):
        validate_policy(
            only_primary_denies,
            Policy(),
            connection_id="primary",
            scope_connections=scope_connections,
            connection_resolver=resolver,
        )

    only_other_denies = _cross_connection_query()
    scope_connections = {id(only_other_denies): {"customers": "other"}}
    resolver = _connection_resolver({"other": Policy()})
    with pytest.raises(PolicyViolationError, match=r"orders\.id.*not accessible"):
        validate_policy(
            only_other_denies,
            Policy(denied_columns={"orders": ["id"]}),
            connection_id="primary",
            scope_connections=scope_connections,
            connection_resolver=resolver,
        )


def test_cross_connection_join_masked_column_position_rule_from_joined_connection_only():
    """The item-49 masked-column-position rule (a masked column may only
    appear as a bare SELECT projection, never in a filter) must also consult
    the joined connection's own mask configuration — `customers.email` is
    masked ONLY on 'other', used here in a WHERE predicate."""
    query = _cross_connection_query(where_customers_email=True)
    scope_connections = {id(query): {"customers": "other"}}
    resolver = _connection_resolver(
        {"other": Policy(column_masks={"customers": [ColumnMask(column="email", kind="null")]})}
    )
    with pytest.raises(PolicyViolationError, match="masked by policy"):
        validate_policy(
            query,
            Policy(),
            connection_id="primary",
            scope_connections=scope_connections,
            connection_resolver=resolver,
        )
    # Without the map, the pre-156 shape: the joined-only mask is invisible,
    # so the WHERE-clause use of a "masked" column is never caught.
    validate_policy(query, Policy(), connection_id="primary")  # no raise


def test_cross_connection_join_masked_column_position_rule_still_enforced_from_primary_connection():
    """Mutation guard, mirroring the deny-list case: the joined connection has
    no opinion at all — only the PRIMARY connection's Policy masks
    `customers.email` — so a candidate-ordering bug that only ever checks the
    table's own (joined) connection and never the primary one (e.g. reading
    just `candidates[0]`, where item 155's own precedent orders the table's
    own connection first) would miss this and must not."""
    query = _cross_connection_query(where_customers_email=True)
    scope_connections = {id(query): {"customers": "other"}}
    resolver = _connection_resolver({"other": Policy()})
    primary_policy = Policy(column_masks={"customers": [ColumnMask(column="email", kind="null")]})
    with pytest.raises(PolicyViolationError, match="masked by policy"):
        validate_policy(
            query,
            primary_policy,
            connection_id="primary",
            scope_connections=scope_connections,
            connection_resolver=resolver,
        )


def test_single_connection_query_unaffected_by_scope_connections_threading():
    """A single-connection query must behave identically whether or not
    `scope_connections`/`connection_resolver` are supplied — every table
    resolves to `connection_id` either way, which `resolve_table_policies`
    collapses to `[policy]` alone."""
    query = StructuredQuery(from_table="orders", select=["orders.id"])
    policy = Policy(denied_columns={"orders": ["id"]})
    scope_connections = {id(query): {"orders": "primary"}}
    resolver = _connection_resolver({"primary": policy})
    for kwargs in ({}, {"scope_connections": scope_connections, "connection_resolver": resolver}):
        with pytest.raises(PolicyViolationError, match="not accessible"):
            validate_policy(query, policy, connection_id="primary", **kwargs)


def test_single_connection_query_self_derives_as_a_no_op_when_only_the_resolver_is_given(
    monkeypatch,
):
    """test-contract-reviewer, 2026-08-09 (item 160 finding-3 follow-up):
    the three pre-existing 'unaffected by threading' tests only ever call
    with `{}` or with `scope_connections`+`connection_resolver` TOGETHER —
    none exercises `connection_resolver` alone (`scope_connections` omitted)
    on a query with no cross-connection join, which is exactly the case
    `validate_policy`'s new self-derivation branch now handles. Pin that a
    single-connection query still self-derives correctly (as a no-op,
    collapsing to `[policy]`) rather than only being covered by the
    multi-connection self-derive tests elsewhere in this file."""
    query = StructuredQuery(from_table="orders", select=["orders.id"])
    # `join_group` is set so `resolve_query_table_connections`'s
    # `policy.join_group or primary.effective_join_group()` short-circuits
    # without touching the resolver's profile half, which `_connection_resolver`
    # never populates (see its own docstring) — there's no cross-connection
    # join here for the group to matter for.
    policy = Policy(denied_columns={"orders": ["id"]}, join_group="grp")
    resolver = _connection_resolver({"primary": policy})

    calls = []
    real_resolve = policy_validation.resolve_scope_connections

    def _spy(*args, **kwargs):
        calls.append(True)
        return real_resolve(*args, **kwargs)

    monkeypatch.setattr(policy_validation, "resolve_scope_connections", _spy)

    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, connection_id="primary", connection_resolver=resolver)

    assert calls, "self-derivation must run when connection_resolver is given alone"


# --------------------------------------------------------------------------- #
# Cross-connection CORRELATED reference (TODO.md item 156 follow-up finding,   #
# test-contract-reviewer 2026-08-06): `_validate_correlation` resolves a       #
# correlated ref against the PARENT scope's own table_connection map — the    #
# generic per-scope check in `_validate_scope` (run on the CHILD/subquery     #
# scope) is NOT a genuine backstop for the cross-connection case, because the #
# outer alias is never declared in the child's OWN from/join, so the child's  #
# own `table_connection` lookup always falls back to `connection_id` (the     #
# primary connection) regardless of where the table actually lives.           #
# `_validate_correlation`'s own per-connection resolution is therefore the    #
# SOLE enforcement point for a correlated reference into a cross-connection-  #
# joined outer table, and needs its own direct coverage rather than relying   #
# on `_validate_scope`'s tests to exercise it incidentally.                   #
# --------------------------------------------------------------------------- #


def _cross_connection_correlation_query() -> StructuredQuery:
    """`orders` (primary) joined to `customers` (connection `other`), with an
    EXISTS subquery correlating on `customers.email` — the cross-connection-
    joined table's column, referenced only via `correlate`, never directly in
    the outer scope's own select/where."""
    return StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        joins=[
            JoinSpec(
                table="customers",
                on=["orders.customer_id", "customers.id"],
                connection="other",
            )
        ],
        where=WhereGroup(
            and_terms=[
                Predicate(
                    op="exists",
                    exists_subquery=StructuredQuery(
                        from_table="orders",
                        select=["orders.id"],
                        correlate=["customers.email"],
                        where=Predicate(col="orders.status", op="eq", value_col="customers.email"),
                    ),
                )
            ]
        ),
    )


def test_cross_connection_correlated_ref_denied_only_by_the_joined_connection():
    """The headline scenario: `customers` is denied ONLY by the JOINED
    connection's ('other') Policy. `_validate_correlation` must still reject
    the query even though the ref reaches `customers` only through
    `correlate`, never a direct column ref in the outer scope."""
    query = _cross_connection_correlation_query()
    scope_connections = {id(query): {"customers": "other"}}
    resolver = _connection_resolver({"other": Policy(denied_tables=["customers"])})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(
            query,
            Policy(max_subquery_depth=2),
            connection_id="primary",
            scope_connections=scope_connections,
            connection_resolver=resolver,
        )


def test_cross_connection_correlated_ref_never_denied_without_the_map():
    """Regression pin: omitting the map (every pre-156 caller) reproduces the
    exact gap this finding describes — a joined-only deny rule is invisible
    to a correlated reference."""
    query = _cross_connection_correlation_query()
    validate_policy(query, Policy(max_subquery_depth=2), connection_id="primary")  # no raise


def test_cross_connection_correlated_ref_masked_only_by_the_joined_connection():
    """Same scenario for the masked-reference rule: `customers.email` is
    masked ONLY on 'other'. A correlated ref is a non-projection use by
    construction, so item 49's rule applies with no position test."""
    query = _cross_connection_correlation_query()
    scope_connections = {id(query): {"customers": "other"}}
    resolver = _connection_resolver(
        {"other": Policy(column_masks={"customers": [ColumnMask(column="email", kind="null")]})}
    )
    with pytest.raises(PolicyViolationError, match="cannot be used as a correlated reference"):
        validate_policy(
            query,
            Policy(max_subquery_depth=2),
            connection_id="primary",
            scope_connections=scope_connections,
            connection_resolver=resolver,
        )


def test_cross_connection_correlated_ref_still_denied_from_the_primary_connection():
    """Mutation guard: the joined connection has no opinion at all — only the
    PRIMARY connection's Policy denies `customers` — so a candidate-ordering
    bug that only ever checks the table's own (joined) connection would miss
    this and must not."""
    query = _cross_connection_correlation_query()
    scope_connections = {id(query): {"customers": "other"}}
    resolver = _connection_resolver({"other": Policy()})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(
            query,
            Policy(max_subquery_depth=2, denied_tables=["customers"]),
            connection_id="primary",
            scope_connections=scope_connections,
            connection_resolver=resolver,
        )


def test_disabled_connection_rejected():
    with pytest.raises(PolicyViolationError, match="disabled"):
        validate_policy(
            StructuredQuery(from_table="orders", select=["orders.id"]),
            Policy(enabled=False),
            connection_id="demo",
        )


def test_max_joins_exceeded():
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        joins=[JoinSpec(table="customers", on=["orders.customer_id", "customers.id"])],
    )
    with pytest.raises(PolicyViolationError, match="joins exceeds"):
        validate_policy(query, Policy(max_joins=0), connection_id="demo")


def test_max_select_columns_exceeded():
    query = StructuredQuery(from_table="orders", select=["orders.id", "orders.status"])
    with pytest.raises(PolicyViolationError, match="select exceeds"):
        validate_policy(query, Policy(max_select_columns=1), connection_id="demo")


def test_max_where_depth_exceeded():
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        where=WhereGroup(
            and_terms=[
                WhereGroup(or_terms=[Predicate(col="orders.status", op="eq", value="completed")])
            ]
        ),
    )
    with pytest.raises(PolicyViolationError, match="where nesting depth"):
        validate_policy(query, Policy(max_where_depth=1), connection_id="demo")


def test_denied_table_rejected():
    query = StructuredQuery(from_table="orders", select=["orders.id"])
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, Policy(denied_tables=["orders"]), connection_id="demo")


def test_allowed_tables_restricts_scope():
    query = StructuredQuery(from_table="orders", select=["orders.id"])
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, Policy(allowed_tables=["customers"]), connection_id="demo")


def test_denied_column_rejected_in_select():
    query = StructuredQuery(from_table="customers", select=["customers.email"])
    policy = Policy(denied_columns={"customers": ["email"]})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, connection_id="demo")


def test_denied_column_rejected_when_only_used_in_where():
    """A denied column can't be used to filter/bisect even if it's never
    selected — this closes an inference side-channel the original codebase
    didn't have to worry about (no column-level policy existed there).
    """
    query = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        where=Predicate(col="customers.email", op="eq", value="ada@example.com"),
    )
    policy = Policy(denied_columns={"customers": ["email"]})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, connection_id="demo")


def test_denied_column_rejected_with_mismatched_table_casing():
    """policy.yaml keys and the casing a query/reflection actually uses can
    differ (dialect-dependent) — the deny rule must still match.
    """
    query = StructuredQuery(from_table="Customers", select=["Customers.Email"])
    policy = Policy(denied_columns={"customers": ["email"]})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, connection_id="demo")


def test_allowed_columns_rejected_with_mismatched_table_casing():
    query = StructuredQuery(from_table="Customers", select=["Customers.Email"])
    policy = Policy(allowed_columns={"customers": ["id"]})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, connection_id="demo")


def test_allowed_columns_wildcard_applies_to_every_table():
    query = StructuredQuery(from_table="customers", select=["customers.id", "customers.name"])
    policy = Policy(allowed_columns={"*": ["id"]})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, connection_id="demo")


def test_permitted_query_passes():
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id", "orders.status"],
        where=Predicate(col="orders.status", op="eq", value="completed"),
        limit=10,
    )
    validate_policy(query, Policy(), connection_id="demo")  # must not raise


def test_denied_column_rejected_when_referenced_through_alias():
    """A denied column must still be caught when the table it belongs to is
    referenced through a from_alias/JoinSpec.alias, not just its own name —
    otherwise aliasing becomes a policy bypass.
    """
    query = StructuredQuery(from_table="customers", from_alias="c", select=["c.email"])
    policy = Policy(denied_columns={"customers": ["email"]})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, connection_id="demo")


def test_denied_table_rejected_when_referenced_through_join_alias():
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        joins=[JoinSpec(table="customers", alias="c", on=["orders.customer_id", "c.id"])],
    )
    policy = Policy(denied_tables=["customers"])
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, connection_id="demo")


def test_self_join_mandatory_filter_table_still_policy_checked_via_alias():
    """referenced_tables()/table_allowed must see the PHYSICAL table for
    both self-join occurrences, not the aliases 'e'/'m'.
    """
    query = StructuredQuery(
        from_table="employees",
        from_alias="e",
        select=["e.id"],
        joins=[JoinSpec(table="employees", alias="m", on=["e.manager_id", "m.id"])],
    )
    policy = Policy(denied_tables=["employees"])
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, connection_id="demo")


def test_denied_column_rejected_when_only_used_in_not_group():
    query = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        where=WhereGroup(not_terms=Predicate(col="customers.email", op="eq", value="a@b.com")),
    )
    policy = Policy(denied_columns={"customers": ["email"]})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, connection_id="demo")


def test_denied_column_rejected_when_only_used_as_value_col():
    """A denied column must still be caught when referenced as the RIGHT-hand
    side of a comparison (value_col), not just the left-hand col — otherwise
    value_col becomes a way to read a denied column's values indirectly.
    """
    query = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        where=Predicate(col="customers.id", op="gt", value_col="customers.email"),
    )
    policy = Policy(denied_columns={"customers": ["email"]})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, connection_id="demo")


def test_where_predicate_count_counts_predicates_inside_not_group():
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        where=WhereGroup(
            not_terms=WhereGroup(
                or_terms=[
                    Predicate(col="orders.status", op="eq", value="a"),
                    Predicate(col="orders.status", op="eq", value="b"),
                    Predicate(col="orders.status", op="eq", value="c"),
                ]
            )
        ),
    )
    with pytest.raises(PolicyViolationError, match="where predicate count"):
        validate_policy(query, Policy(max_where_predicates=2), connection_id="demo")


def test_denied_column_rejected_inside_coalesce():
    query = StructuredQuery(
        from_table="customers",
        select=[{"fn": "coalesce", "args": [{"col": "customers.email"}, {"literal": "n/a"}]}],
    )
    policy = Policy(denied_columns={"customers": ["email"]})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, connection_id="demo")


def test_denied_column_rejected_when_only_used_in_string_agg():
    query = StructuredQuery(
        from_table="customers",
        select=[{"col": "customers.email", "delimiter": ", ", "as": "emails"}],
    )
    policy = Policy(denied_columns={"customers": ["email"]})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, connection_id="demo")


def test_denied_column_rejected_when_only_used_in_array_agg():
    query = StructuredQuery(
        from_table="customers",
        select=[{"col": "customers.email", "as": "emails"}],
    )
    policy = Policy(denied_columns={"customers": ["email"]})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, connection_id="demo")


def test_denied_column_rejected_when_only_used_in_percentile_cont():
    query = StructuredQuery(
        from_table="orders",
        select=[{"col": "orders.total_amount", "fraction": 0.5, "as": "median"}],
    )
    policy = Policy(denied_columns={"orders": ["total_amount"]})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, connection_id="demo")


def test_denied_column_rejected_inside_case_when():
    query = StructuredQuery(
        from_table="customers",
        select=[
            {
                "when": [
                    {
                        "when": {"col": "customers.email", "op": "eq", "value": "x"},
                        "then": {"literal": "y"},
                    }
                ],
                "as": "label",
            }
        ],
    )
    policy = Policy(denied_columns={"customers": ["email"]})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, connection_id="demo")


def test_denied_column_rejected_inside_case_then():
    query = StructuredQuery(
        from_table="customers",
        select=[
            {
                "when": [
                    {
                        "when": {"col": "customers.id", "op": "gt", "value": 0},
                        "then": {"col": "customers.email"},
                    }
                ],
                "as": "label",
            }
        ],
    )
    policy = Policy(denied_columns={"customers": ["email"]})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, connection_id="demo")


def test_max_case_branches_exceeded():
    query = StructuredQuery(
        from_table="orders",
        select=[
            {
                "when": [
                    {
                        "when": {"col": "orders.status", "op": "eq", "value": "a"},
                        "then": {"literal": 1},
                    },
                    {
                        "when": {"col": "orders.status", "op": "eq", "value": "b"},
                        "then": {"literal": 2},
                    },
                    {
                        "when": {"col": "orders.status", "op": "eq", "value": "c"},
                        "then": {"literal": 3},
                    },
                ],
                "as": "label",
            }
        ],
    )
    with pytest.raises(PolicyViolationError, match="case when branches"):
        validate_policy(query, Policy(max_case_branches=2), connection_id="demo")


def test_max_case_branches_at_cap_passes():
    query = StructuredQuery(
        from_table="orders",
        select=[
            {
                "when": [
                    {
                        "when": {"col": "orders.status", "op": "eq", "value": "a"},
                        "then": {"literal": 1},
                    },
                    {
                        "when": {"col": "orders.status", "op": "eq", "value": "b"},
                        "then": {"literal": 2},
                    },
                ],
                "as": "label",
            }
        ],
    )
    validate_policy(query, Policy(max_case_branches=2), connection_id="demo")


def test_denied_column_rejected_when_only_used_in_extra_on():
    """A denied column reachable only via a composite join's extra_on pair
    must still be caught — extra_on isn't a separate, unwalked ref site.
    """
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        joins=[
            JoinSpec(
                table="customers",
                on=["orders.customer_id", "customers.id"],
                extra_on=[["orders.status", "customers.email"]],
            )
        ],
    )
    policy = Policy(denied_columns={"customers": ["email"]})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, connection_id="demo")


def test_denied_column_rejected_when_only_used_inside_predicate_col_fn():
    """A denied column reachable only through lower(customers.email) = 'x'
    (the predicate's LEFT side wrapped in a scalar function) must still be
    rejected — col_fn isn't a separate, unwalked ref site.
    """
    query = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        where=Predicate(
            col_fn={"fn": "lower", "args": [{"col": "customers.email"}]},
            op="eq",
            value="a@b.com",
        ),
    )
    policy = Policy(denied_columns={"customers": ["email"]})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, connection_id="demo")


def test_denied_column_rejected_when_only_used_inside_having_col_fn():
    query = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        group_by=["customers.id"],
        having=Predicate(
            col_fn={"fn": "coalesce", "args": [{"col": "customers.email"}, {"literal": ""}]},
            op="neq",
            value="",
        ),
    )
    policy = Policy(denied_columns={"customers": ["email"]})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, connection_id="demo")


def test_where_predicate_count_exceeded():
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        where=WhereGroup(
            or_terms=[
                Predicate(col="orders.status", op="eq", value="a"),
                Predicate(col="orders.status", op="eq", value="b"),
                Predicate(col="orders.status", op="eq", value="c"),
            ]
        ),
    )
    with pytest.raises(PolicyViolationError, match="where predicate count"):
        validate_policy(query, Policy(max_where_predicates=2), connection_id="demo")


def test_where_predicate_count_at_cap_passes():
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        where=WhereGroup(
            or_terms=[
                Predicate(col="orders.status", op="eq", value="a"),
                Predicate(col="orders.status", op="eq", value="b"),
            ]
        ),
    )
    validate_policy(query, Policy(max_where_predicates=2), connection_id="demo")


def test_having_predicate_count_exceeded():
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        group_by=["orders.id"],
        having=WhereGroup(
            and_terms=[
                Predicate(col="orders.id", op="gt", value=1),
                Predicate(col="orders.id", op="lt", value=100),
            ]
        ),
    )
    with pytest.raises(PolicyViolationError, match="having predicate count"):
        validate_policy(query, Policy(max_where_predicates=1), connection_id="demo")


# --------------------------------------------------------------------------- #
# item 99: searched HAVING (WhereNode) + searched CASE condition (WhereNode).
# The new boolean positions must get the exact same allow/deny, masking, depth,
# count, and in-list treatment as WHERE — a new position is a new bypass surface.
# --------------------------------------------------------------------------- #
def test_denied_column_buried_in_having_or_group_is_rejected():
    query = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        group_by=["customers.id"],
        having=WhereGroup(
            or_terms=[
                Predicate(col="customers.id", op="gt", value=1),
                Predicate(col="customers.email", op="eq", value="target@example.com"),
            ]
        ),
    )
    policy = Policy(denied_columns={"customers": ["email"]})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, connection_id="demo")


def test_masked_column_in_having_is_rejected():
    query = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        group_by=["customers.id"],
        having=Predicate(col="customers.email", op="eq", value="x"),
    )
    policy = Policy(column_masks={"customers": [ColumnMask(column="email", kind="hash")]})
    with pytest.raises(PolicyViolationError, match="masked by policy"):
        validate_policy(query, policy, connection_id="demo")


def test_denied_column_buried_in_searched_case_and_condition_is_rejected():
    query = StructuredQuery(
        from_table="customers",
        select=[
            {
                "when": [
                    {
                        "when": {
                            "and": [
                                {"col": "customers.id", "op": "gt", "value": 0},
                                {"col": "customers.email", "op": "eq", "value": "x"},
                            ]
                        },
                        "then": {"literal": "y"},
                    }
                ],
                "as": "label",
            }
        ],
    )
    policy = Policy(denied_columns={"customers": ["email"]})
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, connection_id="demo")


def test_masked_column_in_searched_case_condition_is_rejected():
    query = StructuredQuery(
        from_table="customers",
        select=[
            {
                "when": [
                    {
                        "when": {
                            "or": [
                                {"col": "customers.id", "op": "gt", "value": 0},
                                {"col": "customers.email", "op": "eq", "value": "x"},
                            ]
                        },
                        "then": {"literal": "y"},
                    }
                ],
                "as": "label",
            }
        ],
    )
    policy = Policy(column_masks={"customers": [ColumnMask(column="email", kind="hash")]})
    with pytest.raises(PolicyViolationError, match="masked by policy"):
        validate_policy(query, policy, connection_id="demo")


def test_having_nesting_depth_exceeded():
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        group_by=["orders.id"],
        having=WhereGroup(not_terms=Predicate(col="orders.id", op="gt", value=1)),
    )
    with pytest.raises(PolicyViolationError, match="having nesting depth"):
        validate_policy(query, Policy(max_where_depth=1), connection_id="demo")


def test_case_condition_nesting_depth_exceeded():
    query = StructuredQuery(
        from_table="orders",
        select=[
            {
                "when": [
                    {
                        "when": {"not": {"col": "orders.status", "op": "eq", "value": "a"}},
                        "then": {"literal": 1},
                    }
                ],
                "as": "label",
            }
        ],
    )
    with pytest.raises(PolicyViolationError, match="case condition nesting depth"):
        validate_policy(query, Policy(max_where_depth=1), connection_id="demo")


def test_case_condition_predicate_count_exceeded():
    query = StructuredQuery(
        from_table="orders",
        select=[
            {
                "when": [
                    {
                        "when": {
                            "and": [
                                {"col": "orders.status", "op": "eq", "value": "a"},
                                {"col": "orders.status", "op": "eq", "value": "b"},
                                {"col": "orders.status", "op": "eq", "value": "c"},
                            ]
                        },
                        "then": {"literal": 1},
                    }
                ],
                "as": "label",
            }
        ],
    )
    with pytest.raises(PolicyViolationError, match="case condition predicate count"):
        validate_policy(query, Policy(max_where_predicates=2), connection_id="demo")


def test_in_list_size_checked_inside_case_condition():
    query = StructuredQuery(
        from_table="orders",
        select=[
            {
                "when": [
                    {
                        "when": {"col": "customers.country", "op": "in", "value": ["a", "b", "c"]},
                        "then": {"literal": 1},
                    }
                ],
                "as": "label",
            }
        ],
    )
    with pytest.raises(PolicyViolationError, match="max_in_list_size"):
        validate_policy(query, Policy(max_in_list_size=2), connection_id="demo")


def test_in_list_size_exceeded():
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        where=Predicate(col="orders.status", op="in", value=["a", "b", "c"]),
    )
    with pytest.raises(PolicyViolationError, match="max_in_list_size"):
        validate_policy(query, Policy(max_in_list_size=2), connection_id="demo")


def test_in_list_size_at_cap_passes():
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        where=Predicate(col="orders.status", op="in", value=["a", "b"]),
    )
    validate_policy(query, Policy(max_in_list_size=2), connection_id="demo")


def test_batch_size_exceeded():
    with pytest.raises(PolicyViolationError, match="batch size"):
        validate_batch_size(5, Policy(max_batch_size=3))


def test_batch_size_within_limit():
    validate_batch_size(3, Policy(max_batch_size=3))  # must not raise


# --------------------------------------------------------------------------- #
# Item 100 — caps on the bounded scalar Expression substrate.
#
# "Bounded" is the whole justification for the substrate not being the
# open-ended expression grammar non-goal #7 forbids (2026-07-25 Decision Log,
# boundary 3). These pin that the bound is real.
# --------------------------------------------------------------------------- #
def _nested_arithmetic(depth: int) -> dict:
    """A left-leaning chain of `depth` BinaryOpExpr nodes over one column."""
    node = {"col": "customers.id"}
    for _ in range(depth):
        node = {"op": "+", "left": node, "right": {"literal": 1}}
    return node


def _query_with_expression(expression: dict, **extra) -> StructuredQuery:
    return StructuredQuery.model_validate(
        {"from": "customers", "select": [{"expr": expression, "as": "computed"}], **extra}
    )


def test_max_expression_depth_fires():
    query = _query_with_expression(_nested_arithmetic(6))
    with pytest.raises(PolicyViolationError, match="expression nesting depth"):
        validate_policy(query, Policy(max_expression_depth=3), connection_id="demo")


def test_max_expression_depth_at_cap_passes():
    # depth 3 = two BinaryOpExpr levels over the ColumnExpr leaf.
    query = _query_with_expression(_nested_arithmetic(2))
    validate_policy(query, Policy(max_expression_depth=3), connection_id="demo")


def test_max_expression_nodes_fires():
    query = _query_with_expression(_nested_arithmetic(10))
    with pytest.raises(PolicyViolationError, match="expression node count"):
        validate_policy(query, Policy(max_expression_nodes=5, max_expression_depth=50), "demo")


def test_expression_node_cap_is_summed_tree_wide_across_a_subquery():
    """Item 97's rule: a count-based cap is enforced on the SUM across every
    scope. Splitting an expression budget between the outer query and a
    subquery must not buy twice the budget."""
    # 3 BinaryOpExpr + 3 LiteralExpr + 1 ColumnExpr leaf = 7 nodes per scope.
    outer_expr = _nested_arithmetic(3)
    query = StructuredQuery.model_validate(
        {
            "from": "customers",
            "select": [{"expr": outer_expr, "as": "computed"}],
            "where": {
                "col": "customers.id",
                "op": "in",
                "value_subquery": {
                    "from": "orders",
                    "select": [{"expr": _nested_arithmetic(3), "as": "inner"}],
                },
            },
        }
    )
    policy = Policy(max_expression_nodes=7, max_expression_depth=50, max_subquery_depth=1)
    with pytest.raises(PolicyViolationError, match="expression node count 14"):
        validate_policy(query, policy, connection_id="demo")
    # Each scope alone is within the same cap — proving the cap is the SUM.
    validate_policy(
        _query_with_expression(outer_expr),
        policy,
        connection_id="demo",
    )


def test_case_branch_cap_applies_to_a_case_nested_inside_an_expression():
    """max_case_branches used to see only a top-level CaseSelectItem. Burying
    the CASE inside an aggregate argument must not dodge it."""
    branches = [
        {"when": {"col": "customers.id", "op": "eq", "value": n}, "then": {"literal": n}}
        for n in range(5)
    ]
    query = StructuredQuery.model_validate(
        {
            "from": "customers",
            "select": [{"fn": "sum", "arg": {"when": branches}, "as": "total"}],
        }
    )
    with pytest.raises(PolicyViolationError, match="case when branches"):
        validate_policy(query, Policy(max_case_branches=2), connection_id="demo")


def test_case_condition_predicate_budget_counts_conditions_nested_in_expressions():
    """The item-99 case-condition predicate budget must follow the CASE
    wherever item 100 lets it move."""
    condition = {"or": [{"col": "customers.id", "op": "eq", "value": n} for n in range(6)]}
    query = StructuredQuery.model_validate(
        {
            "from": "customers",
            "select": [
                {
                    "fn": "sum",
                    "arg": {"when": [{"when": condition, "then": {"literal": 1}}]},
                    "as": "total",
                }
            ],
        }
    )
    with pytest.raises(PolicyViolationError, match="case condition predicate count"):
        validate_policy(query, Policy(max_where_predicates=3), connection_id="demo")


def test_where_depth_cap_applies_to_a_case_condition_inside_an_expression():
    condition = {"and": [{"or": [{"not": {"col": "customers.id", "op": "eq", "value": 1}}]}]}
    query = StructuredQuery.model_validate(
        {
            "from": "customers",
            "select": [
                {
                    "fn": "sum",
                    "arg": {"when": [{"when": condition, "then": {"literal": 1}}]},
                    "as": "total",
                }
            ],
        }
    )
    with pytest.raises(PolicyViolationError, match="case condition nesting depth"):
        validate_policy(query, Policy(max_where_depth=2), connection_id="demo")


def test_subquery_inside_an_expression_case_condition_is_rejected():
    """A value_subquery reachable only through a CaseExpr condition is NOT
    enumerated by iter_query_scopes (which walks WHERE/HAVING predicates), so it
    would never be schema-validated. It must be rejected with a clean typed
    error rather than reaching the compiler."""
    query = StructuredQuery.model_validate(
        {
            "from": "customers",
            "select": [
                {
                    "fn": "sum",
                    "arg": {
                        "when": [
                            {
                                "when": {
                                    "col": "customers.id",
                                    "op": "in",
                                    "value_subquery": {
                                        "from": "orders",
                                        "select": ["orders.customer_id"],
                                    },
                                },
                                "then": {"literal": 1},
                            }
                        ]
                    },
                    "as": "total",
                }
            ],
        }
    )
    with pytest.raises(PolicyViolationError, match="only supported in a WHERE clause"):
        validate_policy(query, Policy(), connection_id="demo")


# --------------------------------------------------------------------------- #
# Window functions (TODO.md item 101)
# --------------------------------------------------------------------------- #
def _window_query(count: int = 1, **over) -> StructuredQuery:
    spec = over or {"order_by": [{"col": "orders.created_at"}]}
    return StructuredQuery.model_validate(
        {
            "from": "orders",
            "select": [
                {
                    "fn": "sum",
                    "arg": {"col": "orders.total_amount"},
                    "over": spec,
                    "as": f"w{i}",
                }
                for i in range(count)
            ],
        }
    )


def test_max_window_specs_caps_the_number_of_windows():
    validate_policy(_window_query(2), Policy(max_window_specs=2), connection_id="demo")
    with pytest.raises(PolicyViolationError, match="window function count 3 exceeds"):
        validate_policy(_window_query(3), Policy(max_window_specs=2), connection_id="demo")


def test_max_window_specs_zero_disables_window_functions():
    with pytest.raises(PolicyViolationError, match="window function count 1 exceeds"):
        validate_policy(_window_query(1), Policy(max_window_specs=0), connection_id="demo")


def test_max_window_specs_is_summed_across_a_subquery():
    """Item 97's rule: a count cap is the SUM across the query tree, so hiding a
    second window inside a subquery must not multiply the budget."""
    query = StructuredQuery.model_validate(
        {
            "from": "orders",
            "select": [
                "orders.id",
                {
                    "fn": "row_number",
                    "over": {"order_by": [{"col": "orders.created_at"}]},
                    "as": "rn",
                },
            ],
            "where": {
                "col": "orders.customer_id",
                "op": "in",
                "value_subquery": {
                    "from": "customers",
                    "select": [
                        {
                            "fn": "row_number",
                            "over": {"order_by": [{"col": "customers.id"}]},
                            "as": "rn2",
                        }
                    ],
                },
            },
        }
    )
    with pytest.raises(PolicyViolationError, match="window function count 2 exceeds"):
        validate_policy(query, Policy(max_window_specs=1), connection_id="demo")


def test_window_partition_by_shares_the_top_n_partition_budget():
    query = _window_query(partition_by=["orders.status", "orders.customer_id"])
    validate_policy(query, Policy(max_partition_by=2), connection_id="demo")
    with pytest.raises(PolicyViolationError, match="partition_by exceeds"):
        validate_policy(query, Policy(max_partition_by=1), connection_id="demo")


def test_max_window_frame_offset_caps_a_frame_bound():
    query = _window_query(
        order_by=[{"col": "orders.created_at"}],
        frame={
            "mode": "rows",
            "start": {"bound": "preceding", "offset": 5_000},
            "end": {"bound": "current_row"},
        },
    )
    with pytest.raises(PolicyViolationError, match="window row offset 5000 exceeds"):
        validate_policy(query, Policy(max_window_frame_offset=1000), connection_id="demo")
    validate_policy(query, Policy(max_window_frame_offset=5000), connection_id="demo")


def test_max_window_frame_offset_caps_a_lag_offset():
    query = StructuredQuery.model_validate(
        {
            "from": "orders",
            "select": [
                {
                    "fn": "lag",
                    "arg": {"col": "orders.total_amount"},
                    "offset": 2_000,
                    "over": {"order_by": [{"col": "orders.created_at"}]},
                    "as": "prev",
                }
            ],
        }
    )
    with pytest.raises(PolicyViolationError, match="window row offset 2000 exceeds"):
        validate_policy(query, Policy(max_window_frame_offset=100), connection_id="demo")


def test_aggregate_window_is_rejected_when_min_group_size_is_set():
    """The k-anonymity floor (item 88) is a HAVING on grouped results; a window
    aggregate has no group to apply it to, and `COUNT(*) OVER ()` would otherwise
    report a below-floor count the aggregate path suppresses."""
    with pytest.raises(PolicyViolationError, match="not allowed when min_group_size is set"):
        validate_policy(_window_query(1), Policy(min_group_size=5), connection_id="demo")


def test_ranking_windows_stay_allowed_when_min_group_size_is_set():
    """A ranking/offset window only surfaces values the caller may already
    project bare, so the floor has nothing to protect there."""
    query = StructuredQuery.model_validate(
        {
            "from": "orders",
            "select": [
                "orders.id",
                {
                    "fn": "row_number",
                    "over": {"order_by": [{"col": "orders.created_at"}]},
                    "as": "rn",
                },
                {
                    "fn": "lag",
                    "arg": {"col": "orders.total_amount"},
                    "over": {"order_by": [{"col": "orders.created_at"}]},
                    "as": "prev",
                },
            ],
        }
    )
    validate_policy(query, Policy(min_group_size=5), connection_id="demo")


def test_denied_column_inside_a_window_is_rejected_in_every_position():
    for over, arg in (
        ({"order_by": [{"col": "orders.created_at"}]}, {"col": "orders.total_amount"}),
        ({"partition_by": ["orders.total_amount"]}, {"col": "orders.id"}),
        ({"order_by": [{"col": "orders.total_amount"}]}, {"col": "orders.id"}),
    ):
        query = StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": [{"fn": "sum", "arg": arg, "over": over, "as": "w"}],
            }
        )
        policy = Policy(denied_columns={"orders": ["total_amount"]})
        with pytest.raises(PolicyViolationError, match="not accessible under the active policy"):
            validate_policy(query, policy, connection_id="demo")


def test_masked_column_inside_a_window_is_rejected_in_every_position():
    """A window value is a non-projection use, so surfacing a masked column
    through one would leak the real value by inference (item 49)."""
    mask = {"orders": [ColumnMask(column="total_amount", kind="hash")]}
    for over, arg in (
        ({"order_by": [{"col": "orders.created_at"}]}, {"col": "orders.total_amount"}),
        ({"partition_by": ["orders.total_amount"]}, {"col": "orders.id"}),
        ({"order_by": [{"col": "orders.total_amount"}]}, {"col": "orders.id"}),
    ):
        query = StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": [{"fn": "sum", "arg": arg, "over": over, "as": "w"}],
            }
        )
        with pytest.raises(PolicyViolationError, match="masked by policy"):
            validate_policy(query, Policy(column_masks=mask), connection_id="demo")


# --------------------------------------------------------------------------- #
# A window's `arg` is an item-100 Expression, so item 100's bounds must reach
# INSIDE a window. `select_item_expressions` is the single wiring point that
# makes that true; without these tests the whole suite passes with that wiring
# deleted (verified by mutation), which would silently exempt window arguments
# from the depth/node/CASE caps and from the no-subquery-in-a-CASE rule.
# --------------------------------------------------------------------------- #
def _window_with_arg(arg: dict) -> StructuredQuery:
    """A window whose argument is the given expression. Built on `customers` so it
    can reuse the shared `_nested_arithmetic` helper above rather than a second
    copy of it (an earlier draft of these tests defined a duplicate and silently
    shadowed it, breaking an existing test's expected node count)."""
    return StructuredQuery.model_validate(
        {
            "from": "customers",
            "select": [{"fn": "sum", "arg": arg, "over": {}, "as": "w"}],
        }
    )


def test_max_expression_depth_fires_inside_a_window_argument():
    query = _window_with_arg(_nested_arithmetic(3))
    validate_policy(query, Policy(max_expression_depth=4), connection_id="demo")
    with pytest.raises(PolicyViolationError, match="expression nesting depth 4 exceeds"):
        validate_policy(query, Policy(max_expression_depth=3), connection_id="demo")


def test_max_expression_nodes_counts_a_window_argument():
    query = _window_with_arg(_nested_arithmetic(3))
    with pytest.raises(PolicyViolationError, match="expression node count 7 exceeds"):
        validate_policy(query, Policy(max_expression_nodes=6), connection_id="demo")


def test_max_case_branches_fires_on_a_case_inside_a_window_argument():
    branch = {
        "when": {"col": "customers.country", "op": "eq", "value": "GB"},
        "then": {"literal": 1},
    }
    query = _window_with_arg({"when": [branch, branch, branch]})
    with pytest.raises(PolicyViolationError, match="case when branches exceeds"):
        validate_policy(query, Policy(max_case_branches=2), connection_id="demo")


def test_case_condition_caps_apply_inside_a_window_argument():
    """A searched-CASE condition buried in a window argument is depth- and
    breadth-bound exactly like a top-level WHERE tree."""
    deep = {"and": [{"or": [{"not": {"col": "customers.country", "op": "eq", "value": "x"}}]}]}
    query = _window_with_arg({"when": [{"when": deep, "then": {"literal": 1}}]})
    with pytest.raises(PolicyViolationError, match="case condition nesting depth"):
        validate_policy(query, Policy(max_where_depth=2), connection_id="demo")

    wide = {"or": [{"col": "orders.status", "op": "eq", "value": f"s{i}"} for i in range(4)]}
    query = _window_with_arg({"when": [{"when": wide, "then": {"literal": 1}}]})
    with pytest.raises(PolicyViolationError, match="case condition predicate count"):
        validate_policy(query, Policy(max_where_predicates=3), connection_id="demo")


def test_in_list_size_applies_inside_a_window_arguments_case_condition():
    query = _window_with_arg(
        {
            "when": [
                {
                    "when": {"col": "customers.country", "op": "in", "value": ["a", "b", "c"]},
                    "then": {"literal": 1},
                }
            ]
        }
    )
    with pytest.raises(PolicyViolationError, match="exceeds max_in_list_size"):
        validate_policy(query, Policy(max_in_list_size=2), connection_id="demo")


def test_subquery_inside_a_window_arguments_case_condition_is_rejected():
    """`iter_query_scopes` only descends WHERE/HAVING predicates, so a
    value_subquery reachable only through a window's CASE condition would never be
    schema-validated. It must fail closed with a clean typed error."""
    query = _window_with_arg(
        {
            "when": [
                {
                    "when": {
                        "col": "customers.id",
                        "op": "in",
                        "value_subquery": {"from": "orders", "select": ["orders.customer_id"]},
                    },
                    "then": {"literal": 1},
                }
            ]
        }
    )
    with pytest.raises(PolicyViolationError, match="only supported in a WHERE clause"):
        validate_policy(query, Policy(), connection_id="demo")


def test_min_group_size_rejection_applies_inside_a_subquery_scope():
    """Per-scope rules are enforced on every scope, so an aggregate window cannot
    hide from the k-anonymity floor inside a nested IN (subquery)."""
    query = StructuredQuery.model_validate(
        {
            "from": "orders",
            "select": ["orders.id"],
            "where": {
                "col": "orders.customer_id",
                "op": "in",
                "value_subquery": {
                    "from": "customers",
                    "select": [{"fn": "count", "over": {}, "as": "n"}],
                },
            },
        }
    )
    with pytest.raises(PolicyViolationError, match="not allowed when min_group_size is set"):
        validate_policy(query, Policy(min_group_size=5), connection_id="demo")


def test_masked_column_cannot_be_a_subquerys_window_output():
    """A subquery's single select item feeds an IN comparison — a non-projection
    use — so a masked column inside a window there is rejected too."""
    query = StructuredQuery.model_validate(
        {
            "from": "orders",
            "select": ["orders.id"],
            "where": {
                "col": "orders.customer_id",
                "op": "in",
                "value_subquery": {
                    "from": "customers",
                    "select": [
                        {
                            "fn": "first_value",
                            "arg": {"col": "customers.name"},
                            "over": {"order_by": [{"col": "customers.id"}]},
                            "as": "first_name",
                        }
                    ],
                },
            },
        }
    )
    policy = Policy(column_masks={"customers": [ColumnMask(column="name", kind="hash")]})
    with pytest.raises(PolicyViolationError, match="masked by policy"):
        validate_policy(query, policy, connection_id="demo")


# --- Purpose-bound access (TODO.md item 145, feature F7) -------------------


def _simple_query(**kwargs) -> StructuredQuery:
    return StructuredQuery(from_table="orders", select=["orders.id"], **kwargs)


def test_purpose_is_inert_when_allowed_purposes_is_empty():
    """The 'empty allow-list = unrestricted' convention: a connection that
    hasn't opted into purpose-gating accepts a query with no purpose, and one
    with an arbitrary purpose, identically."""
    policy = Policy()
    assert validate_policy(_simple_query(), policy, connection_id="demo") == policy
    assert (
        validate_policy(_simple_query(purpose="anything"), policy, connection_id="demo") == policy
    )


def test_missing_purpose_is_rejected_once_allowed_purposes_is_set():
    policy = Policy(allowed_purposes=["fraud_review", "support"])
    with pytest.raises(PolicyViolationError, match="requires a declared purpose"):
        validate_policy(_simple_query(), policy, connection_id="demo")


def test_unrecognized_purpose_is_rejected():
    policy = Policy(allowed_purposes=["fraud_review"])
    with pytest.raises(PolicyViolationError, match="not permitted"):
        validate_policy(_simple_query(purpose="marketing"), policy, connection_id="demo")


def test_recognized_purpose_with_no_delta_passes_through_unchanged():
    policy = Policy(allowed_purposes=["support"])
    effective = validate_policy(_simple_query(purpose="support"), policy, connection_id="demo")
    assert effective == policy


def test_purpose_delta_denies_a_table_only_for_queries_declaring_it():
    policy = Policy(
        allowed_purposes=["support"],
        purpose_policies={"support": PurposePolicyDelta(denied_tables=["orders"])},
    )
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(_simple_query(purpose="support"), policy, connection_id="demo")
    # Mutation-verify the direction: a query declaring NO purpose (inert on
    # this connection since Policy.enabled/allowed_tables don't deny `orders`
    # on their own) must NOT inherit the purpose-only restriction.
    with pytest.raises(PolicyViolationError, match="requires a declared purpose"):
        validate_policy(_simple_query(), policy, connection_id="demo")


def test_purpose_delta_adds_a_mandatory_row_filter():
    row_filter = MandatoryRowFilter(table="orders", column="region", value="us")
    policy = Policy(
        allowed_purposes=["support"],
        purpose_policies={"support": PurposePolicyDelta(mandatory_row_filters=[row_filter])},
    )
    effective = validate_policy(_simple_query(purpose="support"), policy, connection_id="demo")
    assert row_filter in effective.mandatory_row_filters
    # The base Policy object itself is never mutated by resolving a purpose.
    assert policy.mandatory_row_filters == []


def test_purpose_cannot_widen_a_base_deny():
    """The item's own stated risk: a flipped precedence would let a purpose
    grant more than the base policy allows. A delta with an EMPTY
    denied_tables list must never remove a base-level deny."""
    policy = Policy(
        denied_tables=["secrets"],
        allowed_purposes=["support"],
        purpose_policies={"support": PurposePolicyDelta()},
    )
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(
            StructuredQuery(from_table="secrets", select=["secrets.id"], purpose="support"),
            policy,
            connection_id="demo",
        )


def test_resolve_purpose_policy_is_the_shared_primitive_validate_policy_uses():
    policy = Policy(
        allowed_purposes=["support"],
        purpose_policies={"support": PurposePolicyDelta(denied_tables=["orders"])},
    )
    resolved = resolve_purpose_policy(_simple_query(purpose="support"), policy)
    assert "orders" in resolved.denied_tables


def test_structural_caps_pre_check_can_under_reject_a_purpose_narrowed_cte_shadow_but_validate_policy_still_catches_it():
    """TODO.md item 160 finding 2 follow-up (`security-invariant-reviewer`,
    2026-08-07): `validate_structural_caps` is NOT purpose-narrowing-invariant
    end to end, even though its two eponymous caps (`max_cte_count`/
    `max_subquery_depth`) are. It also runs `_validate_cte_constraints`'s
    "no cte name shadows a table the Policy has a rule for" check, which reads
    `denied_tables` (among other fields) — a field `Policy.for_purpose` DOES
    narrow. `execution/service.py`'s `_validate_and_compile` calls
    `validate_structural_caps` once, early, against the UN-narrowed Policy (a
    cheap, connection-registry-free pre-check, before `resolve_scope_
    connections` or schema validation ever run); `validate_policy` calls the
    same function again, internally, AFTER `resolve_purpose_policy` has
    narrowed the Policy.

    This pins that the two calls CAN legitimately disagree — proving the
    (corrected) claim in this function's own docstring and `execution/
    service.py`'s comment: the pre-check alone does NOT reject a cte named
    after a table only a purpose delta denies (it under-rejects — a missed
    early exit, not a missed enforcement), but the full `validate_policy`
    pipeline still correctly rejects it. This is safe only because
    `Policy.for_purpose` is additive-only (verified separately by
    `test_purpose_cannot_widen_a_base_deny` above): it can only ADD to
    `denied_tables`/`denied_columns`/`mandatory_row_filters`/`column_masks`,
    never remove from them, so the un-narrowed pre-check's governed-name set
    is always a SUBSET of the narrowed one's — under-rejection only, never
    over-rejection, and never a missed enforcement, because `validate_
    policy`'s own internal call is the one that actually enforces it and runs
    on every code path regardless of whether `_validate_and_compile`'s
    pre-check also ran. If `PurposePolicyDelta` ever gains a subtractive
    field, this monotonicity argument — and the safety of the redundant
    pre-check — breaks.
    """
    policy = Policy(
        allowed_purposes=["support"],
        purpose_policies={"support": PurposePolicyDelta(denied_tables=["totals"])},
    )
    query = StructuredQuery(
        ctes=[{"name": "totals", "query": {"from": "orders", "select": ["orders.id"]}}],
        from_table="totals",
        select=["totals.id"],
        purpose="support",
    )

    # The pre-check, run against the UN-narrowed Policy exactly as
    # `_validate_and_compile` runs it, cannot see the purpose-only deny —
    # "totals" isn't in the un-narrowed Policy's `denied_tables` yet.
    validate_structural_caps(query, policy)  # must not raise

    # The full pipeline, which purpose-narrows FIRST, still correctly
    # rejects — proving the pre-check's silence above was never a bypass.
    with pytest.raises(PolicyViolationError, match="also the name of a table"):
        validate_policy(query, policy, connection_id="demo")
