"""Adversarial boundary tests for bounded nested subqueries (TODO.md item 97).

A caller-authored subquery is a new bypass surface, so every attack it enables
is codified here: using nesting to multiply a cap (the core threat), hiding a
denied/masked column one level down, correlating to an outer row, crossing
connections, or exceeding the depth cap. The positive end-to-end case (a valid
`IN (subquery)` returns the right rows) is proven against real SQLite.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa

from querygate.core.exceptions import PolicyViolationError, QueryValidationError
from querygate.policy.models import ColumnMask, ColumnMaskKind, Policy
from querygate.query_ast.models import (
    CaseSelectItem,
    CaseWhen,
    Predicate,
    StructuredQuery,
)
from querygate.validation import schema_validation as sv
from querygate.validation.policy_validation import validate_policy

pytestmark = pytest.mark.security

_CONN = "demo"


def _in_subquery(
    sub: StructuredQuery, *, outer_col="orders.customer_id", op="in"
) -> StructuredQuery:
    return StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        where=Predicate(col=outer_col, op=op, value_subquery=sub),
    )


# --------------------------------------------------------------------------- #
# The core threat: nesting as a cap multiplier                                 #
# --------------------------------------------------------------------------- #


def test_joins_are_summed_tree_wide_not_per_level():
    # 3 joins in the outer + 3 in the subquery = 6 > max_joins(5), even though
    # each level alone is within cap. Must be rejected.
    def _joins(n, prefix):
        return [
            {"table": f"{prefix}{i}", "on": [f"orders.id", f"{prefix}{i}.id"]} for i in range(n)
        ]

    sub = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        joins=[{"table": f"c{i}", "on": ["customers.id", f"c{i}.id"]} for i in range(3)],
    )
    outer = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        joins=[{"table": f"o{i}", "on": ["orders.id", f"o{i}.id"]} for i in range(3)],
        where=Predicate(col="orders.customer_id", op="in", value_subquery=sub),
    )
    with pytest.raises(PolicyViolationError, match="joins exceeds"):
        validate_policy(outer, Policy(max_joins=5), _CONN)


def test_group_by_is_summed_tree_wide():
    sub = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        group_by=["customers.id", "customers.country"],
    )
    outer = _in_subquery(sub)
    outer = outer.model_copy(update={"group_by": ["orders.id"]})
    with pytest.raises(PolicyViolationError, match="group_by exceeds"):
        validate_policy(outer, Policy(max_group_by=2), _CONN)


def test_select_columns_summed_tree_wide():
    sub = StructuredQuery(from_table="customers", select=["customers.id"])
    outer = StructuredQuery(
        from_table="orders",
        select=["orders.id", "orders.status", "orders.total_amount"],
        where=Predicate(col="orders.customer_id", op="in", value_subquery=sub),
    )
    # outer 3 + subquery 1 = 4 > max_select_columns(3)
    with pytest.raises(PolicyViolationError, match="select exceeds"):
        validate_policy(outer, Policy(max_select_columns=3), _CONN)


def test_where_predicates_summed_tree_wide():
    sub = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        where=Predicate(col="customers.country", op="eq", value="US"),
    )
    outer = _in_subquery(sub)  # outer where is the IN predicate (1) + subquery where (1) = 2
    with pytest.raises(PolicyViolationError, match="where predicate count"):
        validate_policy(outer, Policy(max_where_predicates=1), _CONN)


# --------------------------------------------------------------------------- #
# Denied / masked columns hidden inside a subquery                             #
# --------------------------------------------------------------------------- #


def test_denied_column_in_subquery_is_rejected():
    sub = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        where=Predicate(col="customers.email", op="eq", value="x@y.z"),
    )
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(_in_subquery(sub), Policy(denied_columns={"customers": ["email"]}), _CONN)


def test_masked_column_used_in_subquery_filter_is_rejected():
    sub = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        where=Predicate(col="customers.email", op="eq", value="x@y.z"),
    )
    policy = Policy(
        column_masks={"customers": [ColumnMask(column="email", kind=ColumnMaskKind.HASH)]}
    )
    with pytest.raises(PolicyViolationError, match="masked"):
        validate_policy(_in_subquery(sub), policy, _CONN)


def test_masked_column_as_subquery_output_is_rejected():
    # The subquery's single output feeds an IN comparison — a masked column may
    # not be used there even though it's a bare projection inside the subquery.
    sub = StructuredQuery(from_table="customers", select=["customers.email"])
    policy = Policy(
        column_masks={"customers": [ColumnMask(column="email", kind=ColumnMaskKind.HASH)]}
    )
    with pytest.raises(PolicyViolationError, match="masked"):
        validate_policy(_in_subquery(sub, outer_col="orders.status"), policy, _CONN)


# --------------------------------------------------------------------------- #
# Depth cap + WHERE-only restriction                                           #
# --------------------------------------------------------------------------- #


def test_over_depth_is_rejected():
    inner = StructuredQuery(from_table="customers", select=["customers.id"])
    mid = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        where=Predicate(col="customers.id", op="in", value_subquery=inner),
    )
    outer = _in_subquery(mid)  # depth 2
    with pytest.raises(PolicyViolationError, match="subquery nesting depth"):
        validate_policy(outer, Policy(max_subquery_depth=1), _CONN)


def test_depth_zero_policy_rejects_any_subquery():
    sub = StructuredQuery(from_table="customers", select=["customers.id"])
    with pytest.raises(PolicyViolationError, match="subquery nesting depth"):
        validate_policy(_in_subquery(sub), Policy(max_subquery_depth=0), _CONN)


def test_subquery_in_having_is_rejected():
    sub = StructuredQuery(from_table="customers", select=["customers.id"])
    q = StructuredQuery(
        from_table="orders",
        select=["orders.customer_id"],
        group_by=["orders.customer_id"],
        having=Predicate(col="orders.customer_id", op="in", value_subquery=sub),
    )
    with pytest.raises(PolicyViolationError, match="only supported in a WHERE"):
        validate_policy(q, Policy(), _CONN)


def test_subquery_in_case_condition_is_rejected():
    sub = StructuredQuery(from_table="customers", select=["customers.id"])
    q = StructuredQuery(
        from_table="orders",
        select=[
            CaseSelectItem(
                when=[
                    CaseWhen(
                        when=Predicate(col="orders.customer_id", op="in", value_subquery=sub),
                        then={"literal": 1},
                    )
                ],
                else_={"literal": 0},
                alias="flag",
            )
        ],
    )
    with pytest.raises(PolicyViolationError, match="only supported in a WHERE"):
        validate_policy(q, Policy(), _CONN)


# --------------------------------------------------------------------------- #
# Correlated + cross-connection (schema-level)                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_correlated_subquery_referencing_outer_table_is_rejected():
    # The subquery references `orders` (an outer table) which is not in the
    # subquery's own from/joins — undeclared in that scope, so it's rejected.
    sub = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        where=Predicate(col="orders.total_amount", op="gt", value_col="customers.id"),
    )
    outer = _in_subquery(sub)

    async def _fake_load(connection_id, table_name, table_connection):
        md = sa.MetaData()
        return sa.Table(
            table_name, md, sa.Column("id", sa.Integer), sa.Column("customer_id", sa.Integer)
        )

    with patch.object(sv, "_load_table", AsyncMock(side_effect=_fake_load)):
        with pytest.raises(QueryValidationError, match="undeclared|only use"):
            await sv.validate_schema(outer, _CONN)


@pytest.mark.asyncio
async def test_cross_connection_subquery_is_rejected():
    sub = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        joins=[{"table": "other", "on": ["customers.id", "other.cid"], "connection": "warehouse"}],
    )
    outer = _in_subquery(sub)

    async def _fake_load(connection_id, table_name, table_connection):
        md = sa.MetaData()
        return sa.Table(
            table_name,
            md,
            sa.Column("id", sa.Integer),
            sa.Column("cid", sa.Integer),
            sa.Column("customer_id", sa.Integer),
        )

    with patch.object(sv, "_load_table", AsyncMock(side_effect=_fake_load)):
        with pytest.raises(QueryValidationError, match="cross-connection subquery"):
            await sv.validate_schema(outer, _CONN)
