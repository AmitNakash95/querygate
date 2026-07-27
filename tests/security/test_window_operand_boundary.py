"""Adversarial boundary tests for a window function as an `Expression` operand
(item 125, regression-bar row 15).

`WindowExpr` is the ONE member of the closed item-100 `Expression` union that is
not legal everywhere a scalar is expected. The rest of the substrate's
reviewability rests on that property, so the exception is bought with a single
fail-closed rule — and this file is the adversarial half of that bargain: every
position SQL forbids a window in, tried through the real validation path.

A window in a filter is not merely invalid SQL. It is evaluated AFTER WHERE and
GROUP BY, so a caller who could smuggle one into a predicate would be filtering
on a value computed over rows the filter was supposed to have already removed.

Sibling of `test_correlation_boundary.py` (106), `test_subquery_boundary.py` (97)
and `test_set_operation_boundary.py` (104).
"""

from __future__ import annotations

from typing import Optional

import pytest

from querygate.core.exceptions import PolicyViolationError
from querygate.policy.models import Policy
from querygate.query_ast.models import StructuredQuery
from querygate.validation.policy_validation import validate_policy

pytestmark = pytest.mark.security

_CONN = "demo"
_WINDOW = {"fn": "sum", "over": {}, "arg": {"col": "orders.amount"}}


def _q(**kwargs) -> StructuredQuery:
    return StructuredQuery.model_validate(kwargs)


def _validate(query: StructuredQuery, policy: Optional[Policy] = None) -> None:
    validate_policy(query, policy or Policy(), _CONN)


# --------------------------------------------------------------------------- #
# THE rule: a window is legal in a projection and nowhere else                 #
# --------------------------------------------------------------------------- #


def test_the_motivating_query_is_allowed_in_a_projection():
    """The whole point of the item — each row against an aggregate over its
    partition, in ONE statement. If this ever starts failing, the feature is
    gone and every rejection below is vacuous."""
    _validate(
        _q(
            **{"from": "orders"},
            select=[
                {
                    "expr": {"op": "/", "left": {"col": "orders.amount"}, "right": _WINDOW},
                    "as": "share_of_total",
                }
            ],
        )
    )


def test_a_window_cannot_be_smuggled_into_a_where_predicate():
    """The security-relevant one. A window is evaluated after WHERE, so filtering
    on one would filter on a value computed over rows WHERE was meant to
    remove."""
    query = _q(
        **{"from": "orders", "select": ["orders.id"]},
        where={"expr": _WINDOW, "op": "gt", "value": 100},
    )
    with pytest.raises(PolicyViolationError, match="not allowed in a where or having"):
        _validate(query)


def test_a_window_cannot_be_smuggled_into_having():
    query = _q(
        **{"from": "orders", "select": ["orders.id"]},
        having={"expr": _WINDOW, "op": "gt", "value": 100},
    )
    with pytest.raises(PolicyViolationError, match="not allowed in a where or having"):
        _validate(query)


def test_a_window_cannot_be_smuggled_into_a_join_condition():
    """The position item 103 opened: a join condition is a full predicate tree,
    so it is a fourth place an expression can hide."""
    query = _q(
        **{"from": "orders", "select": ["orders.id"]},
        joins=[{"table": "customers", "condition": {"expr": _WINDOW, "op": "gt", "value": 1}}],
    )
    with pytest.raises(PolicyViolationError, match="not allowed in a join condition"):
        _validate(query)


def test_a_window_cannot_be_nested_inside_an_aggregate_argument():
    """Inside the select list yet still illegal — `SUM(SUM(x) OVER ())` is not a
    query any dialect accepts. This is exactly why `aggregate_arg` is tagged
    separately from `projection` rather than lumped in with it."""
    query = _q(**{"from": "orders"}, select=[{"fn": "sum", "arg": _WINDOW, "as": "nope"}])
    with pytest.raises(PolicyViolationError, match="not allowed in a aggregate arg"):
        _validate(query)


def test_a_window_cannot_be_nested_inside_another_window():
    """No dialect allows it, and the outer frame would be computed over values
    that do not exist yet."""
    query = _q(
        **{"from": "orders"},
        select=[{"expr": {"fn": "sum", "over": {}, "arg": _WINDOW}, "as": "nope"}],
    )
    with pytest.raises(PolicyViolationError, match="nested inside another window"):
        _validate(query)


def test_a_window_operand_cannot_be_combined_with_group_by():
    """Carried over from item 101's rule for the projection spelling: a window is
    evaluated after grouping, over columns that no longer exist per row."""
    query = _q(
        **{"from": "orders"},
        select=[{"expr": {"op": "+", "left": _WINDOW, "right": {"literal": 1}}, "as": "x"}],
        group_by=["orders.customer_id"],
    )
    with pytest.raises(PolicyViolationError, match="cannot be combined with group_by"):
        _validate(query)


def test_a_window_hidden_deep_inside_arithmetic_in_a_filter_is_still_caught():
    """The rule walks every expression node, not just the top one — burying the
    window inside a CASE inside arithmetic must not evade it."""
    query = _q(
        **{"from": "orders", "select": ["orders.id"]},
        where={
            "expr": {
                "op": "+",
                "left": {"literal": 1},
                "right": {
                    "when": [
                        {"when": {"col": "orders.id", "op": "eq", "value": 1}, "then": _WINDOW}
                    ]
                },
            },
            "op": "gt",
            "value": 1,
        },
    )
    with pytest.raises(PolicyViolationError, match="not allowed in a where or having"):
        _validate(query)


def test_the_rule_reaches_a_nested_subquery_scope():
    """Every scope is validated, so a window cannot ride in inside an
    `IN (subquery)` whose own WHERE carries one."""
    query = _q(
        **{"from": "orders", "select": ["orders.id"]},
        where={
            "col": "orders.customer_id",
            "op": "in",
            "value_subquery": {
                "from": "customers",
                "select": ["customers.id"],
                "where": {
                    "expr": {"fn": "sum", "over": {}, "arg": {"col": "customers.id"}},
                    "op": "gt",
                    "value": 1,
                },
            },
        },
    )
    with pytest.raises(PolicyViolationError, match="not allowed in a where or having"):
        _validate(query, Policy(max_subquery_depth=2))
