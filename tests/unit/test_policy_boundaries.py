"""Boundary tests for every `Policy` complexity cap (TODO.md item 36 phase 1).

`test_policy_validation.py` proves each cap rejects *some* over-cap value;
this file proves the sharper claim a cap actually needs: a query at exactly
the configured limit passes, and one column/join/depth/row past it is
rejected — for every cap `validate_policy` enforces, not just the ones an
earlier hand-written test happened to exercise (`max_top_n` and
`max_partition_by` had no coverage at all before this file).
"""

from __future__ import annotations

from typing import List, Union

import pytest

from querygate.core.exceptions import PolicyViolationError
from querygate.policy.models import Policy
from querygate.query_ast.models import (
    JoinSpec,
    OrderBySpec,
    Predicate,
    StructuredQuery,
    TopNSpec,
    WhereGroup,
)
from querygate.validation.policy_validation import validate_policy


def _nested_where(depth: int) -> Union[Predicate, WhereGroup]:
    """A where tree whose `where_depth()` is exactly `depth` (a bare
    Predicate is depth 1; each wrapping WhereGroup adds one).
    """
    node: Union[Predicate, WhereGroup] = Predicate(col="orders.status", op="eq", value="completed")
    for _ in range(depth - 1):
        node = WhereGroup(and_terms=[node])
    return node


def _joins(n: int) -> List[JoinSpec]:
    # Each join aliases "customers" distinctly — StructuredQuery's own
    # validator requires an explicit alias on every occurrence once the same
    # physical table is joined more than once (self-join disambiguation),
    # and this helper only cares about exercising max_joins, not building a
    # graph-connected query (validate_policy doesn't check connectivity).
    return [
        JoinSpec(table="customers", alias=f"c{i}", on=["orders.customer_id", f"c{i}.id"])
        for i in range(n)
    ]


@pytest.mark.parametrize("n", [0, 1, 5])
def test_max_joins_at_cap_passes(n):
    query = StructuredQuery(from_table="orders", select=["orders.id"], joins=_joins(n))
    validate_policy(query, Policy(max_joins=n), connection_id="demo")  # must not raise


@pytest.mark.parametrize("n", [0, 1, 5])
def test_max_joins_one_over_cap_rejected(n):
    query = StructuredQuery(from_table="orders", select=["orders.id"], joins=_joins(n + 1))
    with pytest.raises(PolicyViolationError, match="joins exceeds"):
        validate_policy(query, Policy(max_joins=n), connection_id="demo")


@pytest.mark.parametrize("n", [1, 5, 30])
def test_max_select_columns_at_cap_passes(n):
    query = StructuredQuery(from_table="orders", select=[f"orders.col{i}" for i in range(n)])
    validate_policy(query, Policy(max_select_columns=n), connection_id="demo")


@pytest.mark.parametrize("n", [1, 5, 30])
def test_max_select_columns_one_over_cap_rejected(n):
    query = StructuredQuery(from_table="orders", select=[f"orders.col{i}" for i in range(n + 1)])
    with pytest.raises(PolicyViolationError, match="select exceeds"):
        validate_policy(query, Policy(max_select_columns=n), connection_id="demo")


@pytest.mark.parametrize("n", [0, 1, 10])
def test_max_group_by_at_cap_passes(n):
    query = StructuredQuery(
        from_table="orders", select=["orders.id"], group_by=[f"orders.col{i}" for i in range(n)]
    )
    validate_policy(query, Policy(max_group_by=n), connection_id="demo")


@pytest.mark.parametrize("n", [0, 1, 10])
def test_max_group_by_one_over_cap_rejected(n):
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        group_by=[f"orders.col{i}" for i in range(n + 1)],
    )
    with pytest.raises(PolicyViolationError, match="group_by exceeds"):
        validate_policy(query, Policy(max_group_by=n), connection_id="demo")


@pytest.mark.parametrize("depth", [1, 2, 5])
def test_max_where_depth_at_cap_passes(depth):
    query = StructuredQuery(from_table="orders", select=["orders.id"], where=_nested_where(depth))
    validate_policy(query, Policy(max_where_depth=depth), connection_id="demo")


@pytest.mark.parametrize("depth", [1, 2, 5])
def test_max_where_depth_one_over_cap_rejected(depth):
    query = StructuredQuery(
        from_table="orders", select=["orders.id"], where=_nested_where(depth + 1)
    )
    with pytest.raises(PolicyViolationError, match="where nesting depth"):
        validate_policy(query, Policy(max_where_depth=depth), connection_id="demo")


@pytest.mark.parametrize("n", [1, 5, 50])
def test_max_top_n_at_cap_passes(n):
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        top_n=TopNSpec(order_by=[OrderBySpec(col="orders.id")], n=n),
    )
    validate_policy(query, Policy(max_top_n=n), connection_id="demo")


@pytest.mark.parametrize("n", [1, 5, 50])
def test_max_top_n_one_over_cap_rejected(n):
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        top_n=TopNSpec(order_by=[OrderBySpec(col="orders.id")], n=n + 1),
    )
    with pytest.raises(PolicyViolationError, match="top_n.n exceeds"):
        validate_policy(query, Policy(max_top_n=n), connection_id="demo")


@pytest.mark.parametrize("n", [0, 1, 5])
def test_max_partition_by_at_cap_passes(n):
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        top_n=TopNSpec(
            partition_by=[f"orders.col{i}" for i in range(n)],
            order_by=[OrderBySpec(col="orders.id")],
            n=1,
        ),
    )
    validate_policy(query, Policy(max_partition_by=n), connection_id="demo")


@pytest.mark.parametrize("n", [0, 1, 5])
def test_max_partition_by_one_over_cap_rejected(n):
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        top_n=TopNSpec(
            partition_by=[f"orders.col{i}" for i in range(n + 1)],
            order_by=[OrderBySpec(col="orders.id")],
            n=1,
        ),
    )
    with pytest.raises(PolicyViolationError, match="top_n.partition_by exceeds"):
        validate_policy(query, Policy(max_partition_by=n), connection_id="demo")


def _flat_or(n: int) -> WhereGroup:
    return WhereGroup(
        or_terms=[Predicate(col="orders.status", op="eq", value=f"s{i}") for i in range(n)]
    )


@pytest.mark.parametrize("n", [1, 5, 20])
def test_max_where_predicates_at_cap_passes(n):
    query = StructuredQuery(from_table="orders", select=["orders.id"], where=_flat_or(n))
    validate_policy(query, Policy(max_where_predicates=n), connection_id="demo")


@pytest.mark.parametrize("n", [1, 5, 20])
def test_max_where_predicates_one_over_cap_rejected(n):
    query = StructuredQuery(from_table="orders", select=["orders.id"], where=_flat_or(n + 1))
    with pytest.raises(PolicyViolationError, match="where predicate count"):
        validate_policy(query, Policy(max_where_predicates=n), connection_id="demo")


@pytest.mark.parametrize("n", [1, 5, 20])
def test_max_in_list_size_at_cap_passes(n):
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        where=Predicate(col="orders.status", op="in", value=[f"s{i}" for i in range(n)]),
    )
    validate_policy(query, Policy(max_in_list_size=n), connection_id="demo")


@pytest.mark.parametrize("n", [1, 5, 20])
def test_max_in_list_size_one_over_cap_rejected(n):
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        where=Predicate(col="orders.status", op="in", value=[f"s{i}" for i in range(n + 1)]),
    )
    with pytest.raises(PolicyViolationError, match="max_in_list_size"):
        validate_policy(query, Policy(max_in_list_size=n), connection_id="demo")


@pytest.mark.parametrize("n", [1, 3, 10])
def test_max_batch_size_at_cap_passes(n):
    from querygate.validation.policy_validation import validate_batch_size

    validate_batch_size(n, Policy(max_batch_size=n))  # must not raise


@pytest.mark.parametrize("n", [1, 3, 10])
def test_max_batch_size_one_over_cap_rejected(n):
    from querygate.validation.policy_validation import validate_batch_size

    with pytest.raises(PolicyViolationError, match="batch size"):
        validate_batch_size(n + 1, Policy(max_batch_size=n))
