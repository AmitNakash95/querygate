"""Unit tests for policy enforcement — caps and table/column allow-deny."""

from __future__ import annotations

import pytest

from querygate.core.exceptions import PolicyViolationError
from querygate.policy.models import Policy
from querygate.query_ast.models import JoinSpec, Predicate, StructuredQuery, WhereGroup
from querygate.validation.policy_validation import validate_batch_size, validate_policy


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
        having=[
            Predicate(
                col_fn={"fn": "coalesce", "args": [{"col": "customers.email"}, {"literal": ""}]},
                op="neq",
                value="",
            )
        ],
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
        having=[
            Predicate(col="orders.id", op="gt", value=1),
            Predicate(col="orders.id", op="lt", value=100),
        ],
    )
    with pytest.raises(PolicyViolationError, match="having predicate count"):
        validate_policy(query, Policy(max_where_predicates=1), connection_id="demo")


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
