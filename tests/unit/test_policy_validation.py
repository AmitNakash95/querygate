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
