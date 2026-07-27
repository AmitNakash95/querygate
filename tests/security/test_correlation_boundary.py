"""Adversarial boundary tests for correlated / EXISTS / scalar subqueries (item 106).

This item deliberately REMOVES an invariant the rest of the engine was built on —
that every scope resolves against its own tables and nothing else — so its threats
are not variations on the earlier containers' threats. They are specific to
correlation:

* an outer column reached WITHOUT declaring it (the pre-106 behaviour must survive
  as the default, or correlation becomes ambient scope);
* a declared ref pointing at a table/column the OUTER policy denies or masks —
  the check that must run against the parent's name map, not the subquery's;
* correlation used to smuggle a value past a mandatory row filter;
* the caps, which now bound a construct whose cost is per-outer-row.

Sibling of `test_subquery_boundary.py` (97), `test_set_operation_boundary.py` (104)
and `test_cte_boundary.py` (105).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

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
    }


def _q(**kwargs) -> StructuredQuery:
    return StructuredQuery.model_validate(kwargs)


def _exists_on(correlate, where, op="exists") -> dict:
    return {
        "op": op,
        "exists_subquery": {
            "from": "orders",
            "select": ["orders.id"],
            **({"correlate": correlate} if correlate else {}),
            "where": where,
        },
    }


_MATCH = {"col": "orders.customer_id", "op": "eq", "value_col": "customers.id"}


async def _run(query: StructuredQuery, policy: Policy):
    """Real pipeline order — policy, then schema, then compile."""
    validate_policy(query, policy, _CONN)
    tables = _tables()

    async def _fake(connection_id, table_name, table_connection):
        return tables[table_name.lower()]

    scope_tables: dict = {}
    with patch.object(sv, "_load_table", AsyncMock(side_effect=_fake)):
        outer = await sv.validate_schema(query, _CONN, scope_tables=scope_tables)
    return compile_structured_query(
        query, outer, policy, dialect="postgresql", subquery_tables=scope_tables
    )


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


_DEEP = Policy(max_subquery_depth=2)


# --------------------------------------------------------------------------- #
# The core threat: correlation must not become ambient scope                   #
# --------------------------------------------------------------------------- #


async def test_an_outer_column_cannot_be_reached_without_declaring_it():
    """THE property this item rests on. SQL would resolve `customers.id` inside the
    subquery implicitly; QueryGate must not, because an undeclared ref has no
    position the enforcement layer inspected. The pre-106 rejection is the default
    and correlation is opt-in per subquery."""
    query = _q(
        **{"from": "customers", "select": ["customers.name"]},
        where=_exists_on(None, _MATCH),
    )
    with pytest.raises(QueryValidationError, match="undeclared"):
        await _run(query, _DEEP)


async def test_declaring_the_ref_is_what_makes_it_resolve():
    """The positive control for the test above — without it, that test could pass
    because correlation is broken outright rather than because it is gated."""
    query = _q(
        **{"from": "customers", "select": ["customers.name"]},
        where=_exists_on(["customers.id"], _MATCH),
    )
    stmt, _limit = await _run(query, _DEEP)
    sql = _sql(stmt)
    assert "EXISTS (SELECT" in sql
    assert "orders.customer_id = customers.id" in sql


async def test_correlation_cannot_reach_two_levels_up():
    """Correlation reaches exactly ONE level, to the immediately enclosing scope.

    Here the innermost subquery declares `customers.id` — a GRANDPARENT column. Its
    actual parent is the middle subquery over `orders`, whose name map has no
    `customers`, so the declaration is refused there. Rejecting at declaration is
    what keeps the model checkable: a scope stack would be a second resolution
    order to keep correct forever.
    """
    innermost = {
        "from": "orders",
        "from_alias": "o2",
        "select": ["o2.id"],
        "correlate": ["customers.id"],  # the GRANDPARENT, not the parent
        "where": {"col": "o2.customer_id", "op": "eq", "value_col": "customers.id"},
    }
    middle = {
        "from": "orders",
        "select": ["orders.id"],
        "correlate": ["customers.id"],
        "where": {
            "and": [
                _MATCH,
                {"op": "exists", "exists_subquery": innermost},
            ]
        },
    }
    query = _q(
        **{"from": "customers", "select": ["customers.name"]},
        where={"op": "exists", "exists_subquery": middle},
    )
    with pytest.raises(QueryValidationError, match="does not name a table of the enclosing"):
        await _run(query, Policy(max_subquery_depth=3, max_correlated_refs=5))


# --------------------------------------------------------------------------- #
# The declared ref is checked against the OUTER scope                          #
# --------------------------------------------------------------------------- #


def test_a_correlated_ref_to_a_denied_column_is_rejected():
    """The check must run against the PARENT's name map. Resolving it against the
    subquery's would test a table the subquery never declared and pass."""
    query = _q(
        **{"from": "customers", "select": ["customers.name"]},
        where=_exists_on(["customers.id"], _MATCH),
    )
    with pytest.raises(PolicyViolationError, match="customers.id"):
        validate_policy(
            query, Policy(denied_columns={"customers": ["id"]}, max_subquery_depth=2), _CONN
        )


def test_a_correlated_ref_to_a_denied_table_is_rejected():
    query = _q(
        **{"from": "customers", "select": ["customers.name"]},
        where=_exists_on(["customers.id"], _MATCH),
    )
    with pytest.raises(PolicyViolationError, match="customers"):
        validate_policy(query, Policy(denied_tables=["customers"], max_subquery_depth=2), _CONN)


def test_a_masked_column_cannot_be_used_as_a_correlated_reference():
    """A correlated ref is a non-projection use by construction — it exists to be
    compared INSIDE the subquery — so item 49's rule applies with no position test.
    Allowing it would compare the raw value and leak it by inference.

    Matched on the CORRELATION-specific message rather than a generic "masked"
    substring, and that precision is the point: mutation testing showed the generic
    per-scope mask rule already rejects this query (the ref is also a WHERE use
    inside the subquery), so a loose match passed even with this check disabled.
    The check is therefore defence in depth rather than the only guard — worth
    keeping for the message it gives, but the test must say which layer it pins or
    it silently pins neither.
    """
    query = _q(
        **{"from": "customers", "select": ["customers.name"]},
        where=_exists_on(
            ["customers.ssn"],
            {"col": "orders.tenant_id", "op": "eq", "value_col": "customers.ssn"},
        ),
    )
    policy = Policy(
        column_masks={"customers": [ColumnMask(column="ssn", kind=ColumnMaskKind.HASH)]},
        max_subquery_depth=2,
    )
    with pytest.raises(PolicyViolationError, match="cannot be used as a correlated reference"):
        validate_policy(query, policy, _CONN)


def test_an_alias_cannot_launder_a_correlated_ref_past_column_policy():
    """The ref resolves through the parent's `effective_name_map`, so aliasing the
    outer table must not change which physical column is checked."""
    query = _q(
        **{"from": "customers", "from_alias": "c", "select": ["c.name"]},
        where=_exists_on(["c.id"], {"col": "orders.customer_id", "op": "eq", "value_col": "c.id"}),
    )
    with pytest.raises(PolicyViolationError, match="c.id"):
        validate_policy(
            query, Policy(denied_columns={"customers": ["id"]}, max_subquery_depth=2), _CONN
        )


# --------------------------------------------------------------------------- #
# Guardrails inside the correlated scope                                       #
# --------------------------------------------------------------------------- #


async def test_a_correlated_subquery_still_carries_its_mandatory_row_filter():
    """Correlation must not be a way to read a filtered table unfiltered — the
    subquery compiles through the same path as any scope."""
    policy = Policy(
        mandatory_row_filters=[MandatoryRowFilter(table="orders", column="tenant_id", value="t1")],
        max_subquery_depth=2,
    )
    query = _q(
        **{"from": "customers", "select": ["customers.name"]},
        where=_exists_on(["customers.id"], _MATCH),
    )
    stmt, _limit = await _run(query, policy)
    assert "tenant_id = 't1'" in _sql(stmt)


def test_correlated_refs_are_capped_tree_wide():
    query = _q(
        **{"from": "customers", "select": ["customers.name"]},
        where=_exists_on(
            ["customers.id", "customers.tenant_id"],
            {
                "and": [
                    _MATCH,
                    {"col": "orders.tenant_id", "op": "eq", "value_col": "customers.tenant_id"},
                ]
            },
        ),
    )
    validate_policy(query, Policy(max_correlated_refs=2, max_subquery_depth=2), _CONN)
    with pytest.raises(PolicyViolationError, match="correlated references exceed max"):
        validate_policy(query, Policy(max_correlated_refs=1, max_subquery_depth=2), _CONN)


def test_max_correlated_refs_zero_turns_correlation_off():
    """The `0 disables it` convention the other count caps use."""
    query = _q(
        **{"from": "customers", "select": ["customers.name"]},
        where=_exists_on(["customers.id"], _MATCH),
    )
    with pytest.raises(PolicyViolationError, match="correlated references exceed max of 0"):
        validate_policy(query, Policy(max_correlated_refs=0, max_subquery_depth=2), _CONN)


def test_an_exists_subquery_counts_against_the_depth_cap():
    query = _q(
        **{"from": "customers", "select": ["customers.name"]},
        where=_exists_on(["customers.id"], _MATCH),
    )
    with pytest.raises(PolicyViolationError, match="exceeds max_subquery_depth"):
        validate_policy(query, Policy(max_subquery_depth=0), _CONN)


def test_caps_are_summed_across_a_correlated_subquery():
    """A correlated scope must not be a fresh budget any more than an uncorrelated
    one — the item-97 loophole, asked of the last container."""
    query = _q(
        **{
            "from": "customers",
            "select": ["customers.name", "customers.id", "customers.tenant_id"],
        },
        where=_exists_on(
            ["customers.id"],
            _MATCH,
        ),
    )
    query.where.exists_subquery.select.extend(["orders.amount", "orders.tenant_id"])
    with pytest.raises(PolicyViolationError, match="select exceeds max"):
        validate_policy(query, Policy(max_select_columns=4, max_subquery_depth=2), _CONN)


# --------------------------------------------------------------------------- #
# Placement — a subquery only where it was reasoned about                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "where_it_goes",
    [
        pytest.param("having", id="in_HAVING"),
        pytest.param("join_condition", id="in_a_join_condition"),
        pytest.param("case", id="in_a_CASE_condition"),
    ],
)
def test_exists_is_rejected_outside_a_where_clause(where_it_goes):
    """EXISTS asks a per-row question. Each of these positions is either evaluated
    where that has no meaning (HAVING, after grouping) or reaches the compiler
    without having been enumerated as a scope — so each fails closed with a typed
    error rather than on a compiler-internal path."""
    exists = _exists_on(["customers.id"], _MATCH)
    if where_it_goes == "having":
        query = _q(
            **{
                "from": "customers",
                "select": ["customers.id", {"fn": "count", "col": "*", "as": "n"}],
            },
            group_by=["customers.id"],
            having=exists,
        )
    elif where_it_goes == "join_condition":
        query = _q(
            **{
                "from": "customers",
                "select": ["customers.name"],
                "joins": [{"table": "orders", "condition": exists}],
            }
        )
    else:
        query = _q(
            **{
                "from": "customers",
                "select": [
                    "customers.name",
                    {"when": [{"when": exists, "then": {"literal": 1}}], "as": "flag"},
                ],
            }
        )
    with pytest.raises(PolicyViolationError, match="only supported in a WHERE clause"):
        validate_policy(query, _DEEP, _CONN)


def test_a_value_set_subquery_is_still_rejected_in_having():
    """Item 97's WHERE-only rule for `IN (subquery)` is deliberately NOT widened by
    item 106 — only the scalar form is admitted to HAVING."""
    query = _q(
        **{
            "from": "orders",
            "select": ["orders.customer_id", {"fn": "sum", "col": "orders.amount", "as": "t"}],
        },
        group_by=["orders.customer_id"],
        having={
            "col": "t",
            "op": "in",
            "value_subquery": {"from": "orders", "select": ["orders.id"]},
        },
    )
    with pytest.raises(PolicyViolationError, match="not HAVING"):
        validate_policy(query, _DEEP, _CONN)


def test_correlate_on_a_scope_that_is_not_a_subquery_is_rejected():
    """It would be silently inert — nothing reads it — and a caller who wrote one
    believes an outer column is in scope when none is."""
    query = _q(**{"from": "customers", "select": ["customers.name"]}, correlate=["orders.id"])
    with pytest.raises(PolicyViolationError, match="only meaningful on a subquery"):
        validate_policy(query, _DEEP, _CONN)


# --------------------------------------------------------------------------- #
# Scalar arity                                                                  #
# --------------------------------------------------------------------------- #


def test_a_scalar_subquery_must_be_a_single_group_aggregate():
    """Exactly-one-row by construction. The alternatives are a silent wrong answer
    (LIMIT 1 picks an arbitrary row) or a dialect-dependent runtime error."""
    with pytest.raises(ValueError, match="must be an aggregate with no group_by"):
        _q(
            **{"from": "orders", "select": ["orders.id"]},
            where={
                "col": "orders.amount",
                "op": "gt",
                "value_subquery": {"from": "orders", "select": ["orders.amount"]},
            },
        )


def test_a_scalar_subquery_may_not_carry_a_set_operation():
    """Combining arms produces multiple rows, defeating the single-row guarantee
    the aggregate shape provides."""
    with pytest.raises(ValueError, match="may not carry a set_op"):
        _q(
            **{"from": "orders", "select": ["orders.id"]},
            where={
                "col": "orders.amount",
                "op": "gt",
                "value_subquery": {
                    "from": "orders",
                    "select": [{"fn": "avg", "col": "orders.amount", "as": "a"}],
                    "set_op": {
                        "op": "union",
                        "arms": [
                            {
                                "from": "orders",
                                "select": [{"fn": "avg", "col": "orders.amount", "as": "a"}],
                            }
                        ],
                    },
                },
            },
        )
