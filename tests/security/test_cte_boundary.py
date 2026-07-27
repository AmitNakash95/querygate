"""Adversarial boundary tests for named WITH blocks — ctes (TODO.md item 105).

A cte is a new scope container, so it is a new place every guardrail could be
forgotten. Each attack it enables is codified here rather than left to the unit
suite's happy-path coverage: using blocks to multiply a cap, wrapping a table in a
block to shed its mandatory row filter, laundering a denied or masked column
through a projection, correlating a block to an outer row, recursing, or
shadowing a name the policy has a rule for.

Sibling of `test_subquery_boundary.py` (item 97) and `test_set_operation_boundary.py`
(item 104) — the same threats, asked of the third container.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa

from querygate.compiler.sqlalchemy_compiler import compile_structured_query
from querygate.core.exceptions import PolicyViolationError, QueryValidationError
from querygate.policy.models import (
    ColumnMask,
    ColumnMaskKind,
    MandatoryRowFilter,
    Policy,
)
from querygate.query_ast.models import StructuredQuery
from querygate.validation import schema_validation as sv
from querygate.validation.policy_validation import validate_policy

pytestmark = pytest.mark.security

_CONN = "demo"


def _tables() -> dict:
    md = sa.MetaData()
    return {
        "orders": sa.Table(
            "orders",
            md,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("customer_id", sa.Integer),
            sa.Column("amount", sa.Numeric(10, 2)),
            sa.Column("tenant_id", sa.String),
        ),
        "customers": sa.Table(
            "customers",
            md,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("name", sa.String),
            sa.Column("ssn", sa.String),
            sa.Column("tenant_id", sa.String),
        ),
        "salaries": sa.Table(
            "salaries",
            md,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("amount", sa.Numeric(10, 2)),
        ),
    }


def _q(**kwargs) -> StructuredQuery:
    return StructuredQuery.model_validate(kwargs)


async def _run(query: StructuredQuery, policy: Policy, dialect: str = "postgresql"):
    """The real pipeline order — policy, then schema, then compile. A boundary test
    that skipped straight to the compiler could 'prove' safety for a query the
    gate would never have compiled."""
    validate_policy(query, policy, _CONN)
    tables = _tables()

    async def _fake_load(connection_id, table_name, table_connection):
        try:
            return tables[table_name.lower()]
        except KeyError:
            raise QueryValidationError(f"no such table {table_name!r}")

    scope_tables: dict = {}
    with patch.object(sv, "_load_table", AsyncMock(side_effect=_fake_load)):
        outer = await sv.validate_schema(query, _CONN, scope_tables=scope_tables)
    return compile_structured_query(
        query, outer, policy, dialect=dialect, subquery_tables=scope_tables
    )


def _sql(stmt) -> str:
    from sqlalchemy.dialects import postgresql

    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


# --------------------------------------------------------------------------- #
# The core threat: a block as a fresh budget                                   #
# --------------------------------------------------------------------------- #


def test_joins_are_summed_across_cte_bodies_not_given_a_fresh_budget():
    """3 joins outside + 3 inside a block = 6 > max_joins(5), though each scope is
    individually within cap. This is the item-97 loophole, asked of item 105."""

    def _joins(n, prefix, base):
        return [
            {"table": f"{prefix}{i}", "on": [f"{base}.id", f"{prefix}{i}.id"]} for i in range(n)
        ]

    query = _q(
        ctes=[
            {
                "name": "block",
                "query": {
                    "from": "orders",
                    "select": ["orders.id"],
                    "joins": _joins(3, "b", "orders"),
                },
            }
        ],
        **{
            "from": "block",
            "select": ["block.id"],
            "joins": _joins(3, "c", "block"),
        },
    )
    with pytest.raises(PolicyViolationError, match="joins exceeds max"):
        validate_policy(query, Policy(max_joins=5), _CONN)


def test_where_predicates_are_summed_across_cte_bodies():
    def _preds(n, col):
        return {"and": [{"col": col, "op": "eq", "value": i} for i in range(n)]}

    query = _q(
        ctes=[
            {
                "name": "block",
                "query": {
                    "from": "orders",
                    "select": ["orders.id"],
                    "where": _preds(6, "orders.id"),
                },
            }
        ],
        **{"from": "block", "select": ["block.id"], "where": _preds(6, "block.id")},
    )
    with pytest.raises(PolicyViolationError, match="where"):
        validate_policy(query, Policy(max_where_predicates=10), _CONN)


def test_a_chain_of_blocks_cannot_exceed_the_depth_cap():
    query = _q(
        ctes=[
            {"name": "a", "query": {"from": "orders", "select": ["orders.id"]}},
            {"name": "b", "query": {"from": "a", "select": ["a.id"]}},
            {"name": "c", "query": {"from": "b", "select": ["b.id"]}},
        ],
        **{"from": "c", "select": ["c.id"]},
    )
    with pytest.raises(PolicyViolationError, match="exceeds max_subquery_depth"):
        validate_policy(query, Policy(max_subquery_depth=2, max_cte_count=5), _CONN)


def test_unreferenced_blocks_cannot_be_used_to_pad_a_query():
    query = _q(
        ctes=[
            {"name": "used", "query": {"from": "orders", "select": ["orders.id"]}},
            {"name": "dead", "query": {"from": "salaries", "select": ["salaries.amount"]}},
        ],
        **{"from": "used", "select": ["used.id"]},
    )
    with pytest.raises(PolicyViolationError, match="never referenced"):
        validate_policy(query, Policy(max_cte_count=5), _CONN)


# --------------------------------------------------------------------------- #
# Laundering a denied / masked / filtered column through a block               #
# --------------------------------------------------------------------------- #


def test_a_denied_table_cannot_be_laundered_through_a_block():
    query = _q(
        ctes=[{"name": "pay", "query": {"from": "salaries", "select": ["salaries.amount"]}}],
        **{"from": "pay", "select": ["pay.amount"]},
    )
    with pytest.raises(PolicyViolationError, match="salaries"):
        validate_policy(query, Policy(denied_tables=["salaries"]), _CONN)


def test_a_denied_column_cannot_be_laundered_through_a_block():
    query = _q(
        ctes=[{"name": "pay", "query": {"from": "salaries", "select": ["salaries.amount"]}}],
        **{"from": "pay", "select": ["pay.amount"]},
    )
    with pytest.raises(PolicyViolationError, match="salaries.amount"):
        validate_policy(query, Policy(denied_columns={"salaries": ["amount"]}), _CONN)


@pytest.mark.parametrize(
    "item",
    [
        pytest.param("customers.ssn", id="bare_projection"),
        pytest.param({"expr": {"col": "customers.ssn"}, "as": "s"}, id="inside_an_expression"),
        pytest.param(
            {
                "expr": {
                    "op": "+",
                    "left": {"fn": "length", "args": [{"col": "customers.ssn"}]},
                    "right": {"literal": 1},
                },
                "as": "s",
            },
            id="buried_in_nested_arithmetic",
        ),
        pytest.param({"fn": "count", "col": "customers.ssn", "as": "n"}, id="inside_an_aggregate"),
        pytest.param(
            {
                "when": [
                    {
                        "when": {"col": "customers.ssn", "op": "eq", "value": "x"},
                        "then": {"literal": 1},
                    }
                ],
                "as": "flag",
            },
            id="inside_a_case_condition",
        ),
    ],
)
def test_a_masked_column_cannot_reach_a_blocks_projection_at_any_depth(item):
    """The DoD's "denied column buried in the deepest position the node allows"
    case. The check walks `select_item_column_refs` — the canonical visitor — so
    burying the reference must not shake it off."""
    query = _q(
        ctes=[{"name": "people", "query": {"from": "customers", "select": [item]}}],
        **{"from": "people", "select": ["people.s"]},
    )
    policy = Policy(
        column_masks={"customers": [ColumnMask(column="ssn", kind=ColumnMaskKind.HASH)]}
    )
    with pytest.raises(PolicyViolationError, match="masked by policy"):
        validate_policy(query, policy, _CONN)


async def test_wrapping_a_table_in_a_block_does_not_shed_its_mandatory_row_filter():
    """The exfiltration shape this rule exists to stop: hide the filtered table one
    level down and read the block unfiltered."""
    policy = Policy(
        mandatory_row_filters=[MandatoryRowFilter(table="orders", column="tenant_id", value="t1")]
    )
    query = _q(
        ctes=[
            {
                "name": "block",
                "query": {"from": "orders", "select": ["orders.id", "orders.amount"]},
            }
        ],
        **{"from": "block", "select": ["block.id", "block.amount"]},
    )
    stmt, _limit = await _run(query, policy)
    assert "tenant_id = 't1'" in _sql(stmt)


def test_a_block_may_not_be_named_after_a_table_the_policy_governs():
    """Shadowing would make the operator's own rule unreachable in this query, and
    would have applied a mandatory filter to the BLOCK's output columns."""
    query = _q(
        ctes=[{"name": "orders", "query": {"from": "customers", "select": ["customers.id"]}}],
        **{"from": "orders", "select": ["orders.id"]},
    )
    policy = Policy(
        mandatory_row_filters=[MandatoryRowFilter(table="orders", column="tenant_id", value="t1")]
    )
    with pytest.raises(PolicyViolationError, match="policy has a rule for"):
        validate_policy(query, policy, _CONN)


# --------------------------------------------------------------------------- #
# Correlation, recursion, and second namespaces                                #
# --------------------------------------------------------------------------- #


async def test_a_block_cannot_correlate_to_an_outer_table():
    """A block is an INDEPENDENT scope — its refs resolve against its own
    from/joins only. Correlation is item 106's problem, deliberately not item
    105's, and until then it must fail closed rather than resolve."""
    query = _q(
        ctes=[
            {
                "name": "block",
                "query": {
                    "from": "orders",
                    "select": ["orders.id"],
                    # `customers` is the OUTER query's table, not this block's.
                    "where": {"col": "customers.id", "op": "eq", "value": 1},
                },
            }
        ],
        **{
            "from": "customers",
            "joins": [{"table": "block", "on": ["customers.id", "block.id"]}],
            "select": ["customers.name"],
        },
    )
    with pytest.raises(QueryValidationError, match="undeclared"):
        await _run(query, Policy())


def test_a_recursive_block_is_structurally_inexpressible():
    """Recursive cte is out of scope (unbounded recursion is a real DoS). It is
    excluded by the earlier-only reference rule, not by a bolt-on check."""
    query = _q(
        ctes=[{"name": "loop", "query": {"from": "loop", "select": ["loop.id"]}}],
        **{"from": "loop", "select": ["loop.id"]},
    )
    with pytest.raises(PolicyViolationError, match="declared later or is itself"):
        validate_policy(query, Policy(max_subquery_depth=9), _CONN)


def test_two_blocks_cannot_reference_each_other():
    query = _q(
        ctes=[
            {"name": "a", "query": {"from": "b", "select": ["b.id"]}},
            {"name": "b", "query": {"from": "a", "select": ["a.id"]}},
        ],
        **{"from": "a", "select": ["a.id"]},
    )
    with pytest.raises(PolicyViolationError, match="declared later or is itself"):
        validate_policy(query, Policy(max_subquery_depth=9, max_cte_count=5), _CONN)


def test_a_nested_scope_cannot_open_a_second_cte_namespace():
    """`declared_cte_names(root)` is trusted as complete by every consumer — the
    approval gate, the usage signals, the mandatory-filter skip. A block declared
    anywhere else would be invisible to all of them."""
    hidden = {
        "ctes": [{"name": "shadow", "query": {"from": "salaries", "select": ["salaries.amount"]}}],
        "from": "shadow",
        "select": ["shadow.amount"],
    }
    query = _q(
        **{"from": "orders", "select": ["orders.id"]},
        where={"col": "orders.id", "op": "in", "value_subquery": hidden},
    )
    with pytest.raises(PolicyViolationError, match="only the top-level query may declare"):
        validate_policy(query, Policy(denied_tables=["salaries"]), _CONN)


async def test_a_block_may_not_reach_another_connection():
    query = _q(
        ctes=[{"name": "block", "query": {"from": "orders", "select": ["orders.id"]}}],
        **{"from": "block", "select": ["block.id"]},
    )
    joined = _q(
        ctes=[{"name": "block", "query": {"from": "orders", "select": ["orders.id"]}}],
        **{
            "from": "customers",
            "joins": [{"table": "block", "on": ["customers.id", "block.id"]}],
            "select": ["customers.name"],
        },
    )
    joined.joins[0].connection = "other"
    query = joined
    with pytest.raises(QueryValidationError, match="may not set `connection`"):
        sv.resolve_query_table_connections(query, _CONN, cte_names={"block"})


# --------------------------------------------------------------------------- #
# The k-anonymity floor                                                        #
# --------------------------------------------------------------------------- #


async def test_the_k_anonymity_floor_is_not_dodged_by_joining_onto_a_block():
    """Item 118's floor counts JOINED rows, and a computed stage may hold many
    rows per key — so a block must be treated as able to fan out."""
    query = _q(
        ctes=[
            {
                "name": "block",
                "query": {"from": "orders", "select": ["orders.customer_id"]},
            }
        ],
        **{
            "from": "customers",
            "joins": [{"table": "block", "on": ["customers.id", "block.customer_id"]}],
            "select": ["customers.name", {"fn": "count", "col": "*", "as": "n"}],
            "group_by": ["customers.name"],
        },
    )
    with pytest.raises(PolicyViolationError, match="can match more than one row"):
        await _run(query, Policy(min_group_size=5))


async def test_the_k_anonymity_floor_still_applies_inside_a_block():
    query = _q(
        ctes=[
            {
                "name": "block",
                "query": {
                    "from": "customers",
                    "select": ["customers.name", {"fn": "count", "col": "*", "as": "n"}],
                    "group_by": ["customers.name"],
                },
            }
        ],
        **{"from": "block", "select": ["block.name", "block.n"]},
    )
    stmt, _limit = await _run(query, Policy(min_group_size=5))
    assert "count(*) >= 5" in _sql(stmt)
