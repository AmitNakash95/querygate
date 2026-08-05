"""Unit tests for policy enforcement — caps and table/column allow-deny."""

from __future__ import annotations

import pytest

from querygate.core.exceptions import PolicyViolationError
from querygate.policy.models import ColumnMask, MandatoryRowFilter, Policy, PurposePolicyDelta
from querygate.query_ast.models import JoinSpec, Predicate, StructuredQuery, WhereGroup
from querygate.validation.policy_validation import (
    resolve_purpose_policy,
    validate_batch_size,
    validate_policy,
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
