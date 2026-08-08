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


async def test_a_mandatory_row_filter_on_the_outer_correlated_table_applies_once_at_the_outer_scope():
    """The companion case to the test above, and the one item 167's fix to
    `_apply_mandatory_row_filters` actually turns on. That fix narrowed the
    walk inside a scope's own compile pass to ONLY its declared FROM/JOIN
    occurrences — deliberately excluding a table the scope merely reaches via
    a declared `correlate` name — on the documented theory that the ENCLOSING
    scope's own compile pass already applies that table's mandatory filters,
    so re-applying them inside the correlated subquery too would be pure
    redundancy (AND with itself), never a way to see MORE rows.

    This test is what actually backs that theory for the case it's making a
    claim about: a `mandatory_row_filter` on `customers` (the OUTER table),
    reached inside the EXISTS ONLY via `correlate=["customers.id"]`, never
    declared as a FROM/JOIN table of the subquery's own scope. If a future
    change broke how the outer scope's own filter coverage works — the thing
    item 167's docstring is trusting to still be true — the filter would just
    silently vanish for `customers` inside this correlated-EXISTS shape, and
    nothing else in this file would catch it (the sibling test above only
    covers a filter on the SUBQUERY's own declared table, `orders`)."""
    policy = Policy(
        mandatory_row_filters=[
            MandatoryRowFilter(table="customers", column="tenant_id", value="t1")
        ],
        max_subquery_depth=2,
    )
    query = _q(
        **{"from": "customers", "select": ["customers.name"]},
        where=_exists_on(["customers.id"], _MATCH),
    )
    stmt, _limit = await _run(query, policy)
    sql = _sql(stmt)
    # Applied -- not silently lost because it's only reached via `correlate`.
    assert "tenant_id = 't1'" in sql, sql
    # Applied exactly ONCE -- not redundantly re-applied inside the EXISTS too.
    assert sql.count("tenant_id = 't1'") == 1, sql
    # And specifically at the OUTER scope: the filter must sit outside the
    # EXISTS subquery's own body, not inside it.
    exists_body = sql.split("EXISTS (SELECT", 1)[1].rsplit(")", 1)[0]
    assert "tenant_id = 't1'" not in exists_body, sql


# --------------------------------------------------------------------------- #
# Item 169: a phantom, differently-cased alias object must not break identity #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("insertion_order", [("C", "c"), ("c", "C")])
async def test_correlate_resolves_to_the_same_object_the_from_join_clause_uses(insertion_order):
    """Item 169: a sibling of item 167's phantom-alias bug, in a DIFFERENT
    consumer of the same duplicated-alias `tables` dict.

    Here the parent scope declares `JOIN customers alias="C"` but ALSO
    references the same table elsewhere with different case ("c.name" in
    SELECT) -- `_reflect_and_validate_scope`'s `needed` union (item 167's own
    mechanism) leaves TWO distinct `sa.Table.alias(...)` objects in the
    parent's own reflected `tables` dict, `tables["C"]` and `tables["c"]`,
    both wrapping the same physical `customers` table. A child EXISTS
    correlates against it using the PHANTOM spelling, `correlate=["c.id"]`.

    `validate_schema` used to resolve that ref with an EXACT `scoped_tables.
    get("c")` index -- always `tables["c"]`, regardless of which object the
    compiler's own FROM/JOIN construction (`table_by_name`, case-insensitive,
    first-match-in-iteration-order) actually places in the parent's compiled
    FROM/JOIN clause. Confirmed by compiling this exact shape (see TODO.md
    item 169): under the `("C", "c")` insertion order, the FROM/JOIN clause
    used `tables["C"]` while the correlate ref bound to `tables["c"]` --
    SQLAlchemy's auto-correlation matches by object identity, so those don't
    match and the EXISTS silently compiled as an INDEPENDENT, UNCORRELATED,
    full-table scan of `customers` -- one that never picked up the
    `mandatory_row_filter` on `customers` either, since that filter only
    applies to whichever object the outer scope's own FROM/JOIN actually
    uses. That is a cross-tenant EXISTS boolean oracle.

    `_reflect_and_validate_scope`'s `needed` is a Python `set`, so which of
    "C"/"c" a real query's own reflection would insert first is hash-order
    dependent (the same nondeterminism item 167's own regression test dealt
    with) -- so this test forces BOTH possible orders directly, by replacing
    `_reflect_and_validate_scope` with a stand-in that returns a `tables`
    dict built in the given order for the parent scope, while leaving
    `validate_schema`'s own correlate-ref resolution loop -- the actual code
    under test -- completely real.
    """
    metadata = sa.MetaData()
    orders = sa.Table(
        "orders",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("customer_id", sa.Integer),
    )
    customers = sa.Table(
        "customers",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String),
        sa.Column("tenant_id", sa.String),
    )
    parent_tables = {"orders": orders}
    for name in insertion_order:
        parent_tables[name] = customers.alias(name)

    query = _q(
        **{
            "from": "orders",
            "select": ["orders.id", "c.name"],  # phantom lowercase ref
            "joins": [{"table": "customers", "alias": "C", "on": ["orders.customer_id", "C.id"]}],
        },
        where={
            "op": "exists",
            "exists_subquery": {
                "from": "orders",
                "from_alias": "o2",
                "select": ["o2.id"],
                "correlate": ["c.id"],  # phantom spelling, not the declared "C"
                "where": {"col": "o2.customer_id", "op": "eq", "value_col": "c.id"},
            },
        },
    )

    received_correlated_tables = {}

    async def _fake_reflect(
        scope,
        connection_id,
        principal=None,
        cte_tables=None,
        correlated_tables=None,
        *,
        table_connection=None,
    ):
        if scope is query:
            return parent_tables
        # the EXISTS subquery's own scope
        received_correlated_tables.update(correlated_tables or {})
        tables = {"o2": orders.alias("o2")}
        tables.update(correlated_tables or {})
        return tables

    policy = Policy(
        mandatory_row_filters=[
            MandatoryRowFilter(table="customers", column="tenant_id", value="t1")
        ],
        max_subquery_depth=2,
    )
    scope_tables: dict = {}
    with patch.object(sv, "_reflect_and_validate_scope", AsyncMock(side_effect=_fake_reflect)):
        outer = await sv.validate_schema(query, _CONN, scope_tables=scope_tables)

    # The core identity property: whatever object validate_schema resolved
    # the correlate ref to must be the SAME object `table_by_name` -- the
    # compiler's own FROM/JOIN lookup -- would pick for the join's declared
    # spelling, regardless of which of "C"/"c" landed in `parent_tables`
    # first.
    expected = sv.table_by_name_or_none(parent_tables, "C")
    assert expected is not None
    assert received_correlated_tables.get("c") is expected, (
        f"correlate bound to a different object than the compiler's FROM/JOIN "
        f"clause will use (insertion_order={insertion_order})"
    )

    stmt, _limit = compile_structured_query(
        query, outer, policy, dialect="postgresql", subquery_tables=scope_tables
    )
    sql = _sql(stmt)

    assert "EXISTS (SELECT" in sql, sql
    exists_body = sql.split("EXISTS (SELECT", 1)[1].rsplit(")", 1)[0]
    # No independent scan of customers inside the EXISTS -- it must be a real
    # correlated reference to the outer join's own alias, not a second,
    # unconditioned FROM entry.
    assert "customers" not in exists_body, sql
    # The mandatory row filter on customers must actually be reachable from
    # inside the EXISTS's own WHERE (via correlation to the filtered outer
    # object) -- i.e. the filter is not silently bypassed by scanning an
    # unfiltered independent copy of customers.
    assert sql.count("tenant_id = 't1'") == 1, sql
    assert "tenant_id = 't1'" not in exists_body, sql


async def test_child_ref_to_correlated_table_in_different_case_than_correlate_does_not_crash():
    """Found while fixing item 169, in the same code region: `_reflect_and_
    validate_scope`'s own resolution of `correlated_tables` (the map built by
    `validate_schema`'s correlate loop, keyed by whichever spelling the
    `correlate` declaration used) was ALSO an exact dict index. A child
    scope's OWN body may legally reference the correlated table with
    DIFFERENT case than `correlate` declared -- `declared_tables` already
    accepts it case-insensitively -- but the exact index missed it and fell
    through to `name_to_physical[name.casefold()]`, which raises a raw,
    uncaught `KeyError` (not the usual typed `QueryValidationError`) because
    a correlated-only table was never part of the child scope's own
    from/join map. Not the item 169 cross-tenant bypass itself (no policy
    result is affected -- it fails closed, just ungracefully as an unhandled
    500 rather than a rejection), but the identical fix (`table_by_name_or_
    none` instead of an exact index) closes it too."""
    query = _q(
        **{"from": "customers", "select": ["customers.name"]},
        where=_exists_on(
            ["customers.id"],
            {
                "and": [
                    _MATCH,
                    # "Customers.id" -- different case than the correlate
                    # declaration's own "customers.id" spelling.
                    {"col": "orders.id", "op": "gt", "value_col": "Customers.id"},
                ]
            },
        ),
    )
    stmt, _limit = await _run(query, _DEEP)
    sql = _sql(stmt)
    assert "EXISTS (SELECT" in sql, sql


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


# --------------------------------------------------------------------------- #
# Interaction with top_n's derived-table materialization (items 106 x 119)     #
# --------------------------------------------------------------------------- #


async def test_top_n_materialization_preserves_a_correlated_exists():
    """Item 106 (EXISTS/correlation) and item 119 (`_apply_top_n` positional
    binding) were built on separate branches and first combined by a rebase, so
    nothing had ever compiled them together. `_apply_top_n` re-selects the whole
    statement through a derived table; the correlated EXISTS must stay INSIDE that
    derived table. Hoisted out, it would filter after ranking — a different answer
    — and the correlation would no longer resolve against the scope that declared
    it."""
    query = _q(
        **{"from": "customers", "select": ["customers.id", "customers.name"]},
        where=_exists_on(["customers.id"], _MATCH),
        top_n={
            "partition_by": ["customers.id"],
            "order_by": [{"col": "customers.name", "dir": "desc"}],
            "n": 1,
        },
    )
    stmt, _ = await _run(query, _DEEP)
    sql = _sql(stmt)

    # Everything up to the derived table's alias is its body; the outer query is
    # only the `WHERE anon_1.__rank <= n` filter after it.
    inner, _, outer = sql.partition("AS anon_1")
    assert "EXISTS" in inner.upper(), sql
    # It is still the CORRELATED form — the outer column is referenced from inside
    # the EXISTS, not re-selected into the subquery's own FROM list.
    assert "orders.customer_id = customers.id" in inner, sql
    # And the rank filter really is the only thing left outside.
    assert "EXISTS" not in outer.upper(), sql
