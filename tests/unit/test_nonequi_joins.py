"""Unit tests for item 103: non-equi/range join conditions + FULL OUTER / CROSS.

Four axes, because four different things can go wrong when a join stops being an
equality pair:

1. **AST** — `on` (sugar) and `condition` (the general tree) are mutually
   exclusive, and a `cross` join carries neither. A join with both forms, or
   with neither, is a shape whose precedence the compiler would have to invent.
2. **Graph** — a `condition` may name more than the joined pair, so the
   connectivity rule generalizes: it must reference the table being joined, must
   connect to something already known, and must NOT forward-reference a table
   joined later (which SQLAlchemy renders as a broken/implicitly-cartesian FROM).
3. **Caps** — a join condition is the fourth position a predicate tree can sit in
   (after WHERE, HAVING and a searched-CASE `when`). Every shape cap that bounds
   those has to reach it too, or the ON clause is the position that escaped them.
4. **Dialect** — FULL OUTER and non-equi joins are universal on PG/MSSQL, so
   this is mechanical translation, not a reject-or-emulate call. The rendering is
   asserted here; the live PG-vs-MSSQL row comparison is in
   `tests/integration/test_cross_dialect_differential.py`.

The policy gate on `cross` and the denied/masked-column rules are exercised from
the attacker's side in `tests/security/test_adversarial_security.py`.
"""

from __future__ import annotations

import json
from typing import Dict

import pydantic
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mssql, postgresql, sqlite

from querygate.audit.events import normalize_query_shape
from querygate.compiler.sqlalchemy_compiler import compile_structured_query
from querygate.core.exceptions import PolicyViolationError, QueryValidationError
from querygate.policy.models import Policy
from querygate.query_ast.models import JoinSpec, Predicate, StructuredQuery, WhereGroup
from querygate.validation.policy_validation import validate_policy
from querygate.validation.schema_validation import _validate_join_graph

pytestmark = pytest.mark.unit

_DIALECTS = {
    "postgresql": postgresql.dialect(),
    "mssql": mssql.dialect(),
    "sqlite": sqlite.dialect(),
}


def _tables() -> Dict[str, sa.Table]:
    metadata = sa.MetaData()
    products = sa.Table(
        "products",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(100)),
        sa.Column("price", sa.Numeric(10, 2)),
        sa.Column("created_at", sa.DateTime),
    )
    bands = sa.Table(
        "bands",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("label", sa.String(50)),
        sa.Column("lo", sa.Numeric(10, 2)),
        sa.Column("hi", sa.Numeric(10, 2)),
        sa.Column("created_at", sa.DateTime),
    )
    regions = sa.Table(
        "regions",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("band_id", sa.Integer),
        sa.Column("product_id", sa.Integer),
    )
    return {"products": products, "bands": bands, "regions": regions}


def _price_band_condition() -> WhereGroup:
    """`ON products.price BETWEEN bands.lo AND bands.hi` — regression-bar row 16."""
    return WhereGroup(
        and_terms=[
            Predicate(col="products.price", op="gte", value_col="bands.lo"),
            Predicate(col="products.price", op="lte", value_col="bands.hi"),
        ]
    )


def _sql(query: StructuredQuery, dialect: str, policy: Policy = None) -> str:
    stmt, _limit = compile_structured_query(
        query, _tables(), policy or Policy(allow_cross_join=True), dialect=dialect
    )
    return str(stmt.compile(dialect=_DIALECTS[dialect])).replace("\n", " ")


# --- 1. AST: the two condition forms are mutually exclusive -----------------


def test_condition_and_on_together_are_rejected():
    """Two spellings of one clause would leave precedence to the compiler."""
    with pytest.raises(pydantic.ValidationError, match="exactly one of"):
        JoinSpec(
            table="bands",
            on=["products.id", "bands.id"],
            condition=_price_band_condition(),
        )


def test_join_with_neither_on_nor_condition_is_rejected():
    with pytest.raises(pydantic.ValidationError, match="exactly one of"):
        JoinSpec(table="bands")


def test_extra_on_cannot_be_combined_with_condition():
    """`extra_on` is additional pairs for `on`; a condition expresses them itself."""
    with pytest.raises(pydantic.ValidationError, match="extra_on"):
        JoinSpec(
            table="bands",
            condition=_price_band_condition(),
            extra_on=[["products.id", "bands.id"]],
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"on": ["products.id", "bands.id"]},
        {"condition": _price_band_condition()},
        {"extra_on": [["products.id", "bands.id"]]},
    ],
    ids=["on", "condition", "extra_on"],
)
def test_cross_join_takes_no_condition(kwargs):
    """Silently ignoring a condition on a cross join answers a different question
    than the caller asked, so it is rejected rather than dropped."""
    with pytest.raises(pydantic.ValidationError, match="takes no condition"):
        JoinSpec(table="bands", type="cross", **kwargs)


def test_cross_join_with_no_condition_is_accepted():
    join = JoinSpec(table="bands", type="cross")
    assert join.on is None and join.condition is None


def test_equality_on_still_builds_unchanged():
    """The pre-item-103 form is untouched — `on` stays the common-case sugar."""
    join = JoinSpec(table="bands", on=["products.id", "bands.id"])
    assert join.on == ["products.id", "bands.id"]
    assert join.condition is None
    assert join.type == "inner"


# --- 2. Graph: connectivity generalizes to a multi-table condition ----------


def _query(joins, **kwargs) -> StructuredQuery:
    return StructuredQuery(from_table="products", select=["products.name"], joins=joins, **kwargs)


def test_condition_must_reference_the_table_being_joined():
    query = _query(
        [
            JoinSpec(
                table="bands",
                condition=Predicate(col="products.id", op="gt", value_col="products.price"),
            )
        ]
    )
    with pytest.raises(QueryValidationError, match="must reference that table"):
        _validate_join_graph(query)


def test_condition_must_connect_to_the_known_graph():
    query = _query(
        [
            JoinSpec(
                table="bands",
                condition=Predicate(col="bands.lo", op="gt", value_col="regions.band_id"),
            )
        ]
    )
    with pytest.raises(QueryValidationError, match="does not connect"):
        _validate_join_graph(query)


def test_condition_may_reference_a_third_already_joined_table():
    """`JOIN regions ON regions.band_id = bands.id AND regions.product_id =
    products.id` is legal SQL and legal here — the pair restriction that
    `extra_on` carries is a property of that sugar, not of a join condition."""
    query = _query(
        [
            JoinSpec(table="bands", on=["products.id", "bands.id"]),
            JoinSpec(
                table="regions",
                condition=WhereGroup(
                    and_terms=[
                        Predicate(col="regions.band_id", op="eq", value_col="bands.id"),
                        Predicate(col="regions.product_id", op="eq", value_col="products.id"),
                    ]
                ),
            ),
        ]
    )
    _validate_join_graph(query)  # does not raise


def test_condition_cannot_forward_reference_a_later_join():
    """`regions` is joined AFTER `bands`, so `bands`' condition cannot name it —
    SQLAlchemy would render that as a broken/implicitly-cartesian FROM."""
    query = _query(
        [
            JoinSpec(
                table="bands",
                condition=WhereGroup(
                    and_terms=[
                        Predicate(col="bands.id", op="eq", value_col="products.id"),
                        Predicate(col="bands.lo", op="eq", value_col="regions.band_id"),
                    ]
                ),
            ),
            JoinSpec(table="regions", on=["regions.product_id", "products.id"]),
        ]
    )
    with pytest.raises(QueryValidationError, match="before it is joined"):
        _validate_join_graph(query)


def test_cross_join_is_exempt_from_connectivity_and_adds_its_table():
    """A cartesian product asserts there is no relationship, so requiring one
    would make the type unusable — and the joined table still becomes 'known',
    so a LATER join may reference it."""
    query = _query(
        [
            JoinSpec(table="bands", type="cross"),
            JoinSpec(table="regions", on=["regions.band_id", "bands.id"]),
        ]
    )
    _validate_join_graph(query)  # does not raise


# --- 3. Caps: the ON clause is not the position that escaped them -----------


def _wide_condition(n: int) -> WhereGroup:
    """A join condition with `n` predicate leaves, all graph-legal."""
    return WhereGroup(
        and_terms=[Predicate(col="products.id", op="eq", value_col="bands.id")]
        + [Predicate(col="products.price", op="gte", value_col="bands.lo") for _ in range(n - 1)]
    )


def test_join_condition_predicates_are_bounded_by_max_where_predicates():
    query = _query([JoinSpec(table="bands", condition=_wide_condition(6))])
    with pytest.raises(PolicyViolationError, match="join condition predicate count"):
        validate_policy(query, Policy(max_where_predicates=5), "demo")


def test_join_condition_predicate_budget_is_summed_tree_wide():
    """Two joins of 3 predicates each is 6 — a per-join budget would pass both."""
    query = _query(
        [
            JoinSpec(table="bands", condition=_wide_condition(3)),
            JoinSpec(
                table="regions",
                condition=WhereGroup(
                    and_terms=[
                        Predicate(col="regions.band_id", op="eq", value_col="bands.id"),
                        Predicate(col="regions.product_id", op="eq", value_col="products.id"),
                        Predicate(col="regions.id", op="gt", value_col="products.id"),
                    ]
                ),
            ),
        ]
    )
    with pytest.raises(PolicyViolationError, match="join condition predicate count 6"):
        validate_policy(query, Policy(max_where_predicates=5, max_joins=5), "demo")


def test_join_condition_depth_is_bounded_by_max_where_depth():
    node = Predicate(col="products.id", op="eq", value_col="bands.id")
    for _ in range(6):
        node = WhereGroup(and_terms=[node])
    query = _query([JoinSpec(table="bands", condition=node)])
    with pytest.raises(PolicyViolationError, match="join condition nesting depth"):
        validate_policy(query, Policy(max_where_depth=5), "demo")


def test_join_condition_in_list_is_bounded_by_max_in_list_size():
    query = _query(
        [
            JoinSpec(
                table="bands",
                condition=WhereGroup(
                    and_terms=[
                        Predicate(col="products.id", op="eq", value_col="bands.id"),
                        Predicate(col="bands.label", op="in", value=["a", "b", "c"]),
                    ]
                ),
            )
        ]
    )
    with pytest.raises(PolicyViolationError, match="max_in_list_size"):
        validate_policy(query, Policy(max_in_list_size=2), "demo")


def test_expression_in_a_join_condition_is_bounded_by_max_expression_nodes():
    """An arithmetic bound inside an ON clause counts against the same expression
    budget a WHERE clause's does — `iter_scope_expressions` is the one source."""
    expr = {"col": "bands.lo"}
    for _ in range(4):
        expr = {"left": expr, "op": "+", "right": {"literal": 1}}
    query = StructuredQuery.model_validate(
        {
            "from": "products",
            "select": ["products.name"],
            "joins": [
                {
                    "table": "bands",
                    "condition": {
                        "col": "products.price",
                        "op": "gte",
                        "value_expr": expr,
                    },
                }
            ],
        }
    )
    with pytest.raises(PolicyViolationError, match="expression node count"):
        validate_policy(query, Policy(max_expression_nodes=5), "demo")


def test_cross_join_counts_against_max_joins():
    query = _query([JoinSpec(table="bands", type="cross"), JoinSpec(table="regions", type="cross")])
    with pytest.raises(PolicyViolationError, match="joins exceeds max"):
        validate_policy(query, Policy(max_joins=1, allow_cross_join=True), "demo")


# --- 3b. The cross-join policy gate ----------------------------------------


def test_cross_join_is_denied_by_default():
    query = _query([JoinSpec(table="bands", type="cross")])
    with pytest.raises(PolicyViolationError, match="allow_cross_join is off"):
        validate_policy(query, Policy(), "demo")


def test_cross_join_is_allowed_when_the_policy_opts_in():
    query = _query([JoinSpec(table="bands", type="cross")])
    validate_policy(query, Policy(allow_cross_join=True), "demo")  # does not raise


def test_allow_cross_join_defaults_to_off():
    assert Policy().allow_cross_join is False


def test_full_and_inner_joins_are_unaffected_by_the_cross_gate():
    for join_type in ("inner", "left", "full"):
        query = _query([JoinSpec(table="bands", type=join_type, on=["products.id", "bands.id"])])
        validate_policy(query, Policy(), "demo")  # does not raise


def test_subquery_in_a_join_condition_is_rejected():
    """`iter_query_scopes` descends WHERE/HAVING only, so a subquery in an ON
    clause would never be validated as a scope — it fails closed with a typed
    error instead of on a compiler-internal `ctx=None`."""
    query = StructuredQuery.model_validate(
        {
            "from": "products",
            "select": ["products.name"],
            "joins": [
                {
                    "table": "bands",
                    "condition": {
                        "col": "bands.id",
                        "op": "in",
                        "value_subquery": {"from": "products", "select": ["products.id"]},
                    },
                }
            ],
        }
    )
    with pytest.raises(PolicyViolationError, match="not a join condition"):
        validate_policy(query, Policy(), "demo")


# --- 4. Dialect: mechanical translation, asserted per backend ---------------


@pytest.mark.parametrize("dialect", ["postgresql", "mssql", "sqlite"])
def test_range_join_renders_the_inequalities_on_every_dialect(dialect):
    """Regression-bar row 16. Non-equi joins are universal, so all three render
    the same ON clause — no adapter method, no rejection."""
    query = _query([JoinSpec(table="bands", condition=_price_band_condition())])
    sql = _sql(query, dialect)
    assert "JOIN bands ON products.price >= bands.lo AND products.price <= bands.hi" in sql


@pytest.mark.parametrize("dialect", ["postgresql", "mssql", "sqlite"])
def test_full_outer_join_renders_as_full_outer(dialect):
    query = _query([JoinSpec(table="bands", type="full", on=["products.id", "bands.id"])])
    assert "FULL OUTER JOIN bands ON products.id = bands.id" in _sql(query, dialect)


def test_cross_join_renders_an_always_true_condition_per_dialect():
    """SQLAlchemy Core has no cross-join constructor on Select; `ON true` is the
    explicit portable spelling and is the same cartesian product to every
    planner. Postgres has a boolean literal, the other two render `1 = 1` — a
    difference SQLAlchemy makes, not a branch QueryGate takes."""
    query = _query([JoinSpec(table="bands", type="cross")])
    assert "JOIN bands ON true" in _sql(query, "postgresql")
    assert "JOIN bands ON 1 = 1" in _sql(query, "mssql")
    assert "JOIN bands ON 1 = 1" in _sql(query, "sqlite")


@pytest.mark.parametrize("dialect", ["postgresql", "mssql", "sqlite"])
def test_equality_join_rendering_is_unchanged(dialect):
    """The sugar path must be byte-identical to pre-item-103 output."""
    query = _query([JoinSpec(table="bands", on=["products.id", "bands.id"])])
    sql = _sql(query, dialect)
    assert "JOIN bands ON products.id = bands.id" in sql
    assert "FULL" not in sql and "ON true" not in sql


def test_a_range_join_binds_no_literal_and_reaches_no_raw_sql():
    """The whole point of the AST: a range join is column-to-column, so the
    rendered ON clause carries identifiers only — no caller string is spliced."""
    query = _query([JoinSpec(table="bands", condition=_price_band_condition())])
    stmt, _limit = compile_structured_query(query, _tables(), Policy(), dialect="postgresql")
    compiled = stmt.compile(dialect=_DIALECTS["postgresql"])
    # The only bound parameter is the LIMIT the policy applies.
    assert set(compiled.params) == {"param_1"}


# --- 5. The audit shape: the third un-foldable recursion (item 102's lesson) -


def test_join_condition_appears_in_the_normalized_audit_shape():
    """`audit/events.py`'s shape normalizer is a third recursion over the AST,
    beside the visitor and the compiler, and item 102 shipped three nodes that
    passed the whole unit suite while every query using one raised at execution
    — because nothing asserted the normalizer had been taught about them.

    A join condition is structure the audit trail must record: "joined on a range"
    versus "joined on equality" is exactly the query shape a reviewer reads.
    """
    query = _query([JoinSpec(table="bands", condition=_price_band_condition())])
    shape = normalize_query_shape(query)
    assert shape["joins"] == [
        {
            "table": "bands",
            "type": "inner",
            "condition": {
                "and": [
                    {"operator": "gte", "column": "products.price"},
                    {"operator": "lte", "column": "products.price"},
                ]
            },
        }
    ]


def test_join_shape_records_the_type_and_omits_the_form_not_used():
    """`on` and `condition` are mutually exclusive, so each key appears only for
    the form the caller actually used, and `cross` carries neither."""
    equality = normalize_query_shape(
        _query([JoinSpec(table="bands", type="full", on=["products.id", "bands.id"])])
    )["joins"][0]
    assert equality == {
        "table": "bands",
        "type": "full",
        "on": ["products.id", "bands.id"],
    }

    crossed = normalize_query_shape(_query([JoinSpec(table="bands", type="cross")]))["joins"][0]
    assert crossed == {"table": "bands", "type": "cross"}


def test_a_join_conditions_literal_never_reaches_the_audit_shape():
    """Non-negotiable 3: a persisted audit event carries shape, never values.
    A join condition is a new place a literal can sit, so it gets the same
    assertion the WHERE clause has."""
    query = StructuredQuery.model_validate(
        {
            "from": "products",
            "select": ["products.name"],
            "joins": [
                {
                    "table": "bands",
                    "condition": {
                        "and": [
                            {"col": "products.id", "op": "eq", "value_col": "bands.id"},
                            {"col": "bands.label", "op": "eq", "value": "super-secret-band"},
                        ]
                    },
                }
            ],
        }
    )
    assert "super-secret-band" not in json.dumps(normalize_query_shape(query))


# --- 6. Gaps the item-103 audit surfaced ------------------------------------


@pytest.mark.parametrize("dialect", ["postgresql", "mssql", "sqlite"])
def test_left_join_renders_left_outer_not_full_outer(dialect):
    """`isouter` and `full` are two flags on one `stmt.join(...)` call, so
    swapping them is a one-character mutation. Pinned at the SQL-text layer as
    well as behaviorally, because the end-to-end guard depends on seed data
    happening to have unmatched rows on the discriminating side."""
    query = _query([JoinSpec(table="bands", type="left", on=["products.id", "bands.id"])])
    sql = _sql(query, dialect)
    assert "LEFT OUTER JOIN bands ON products.id = bands.id" in sql
    assert "FULL" not in sql


def test_join_condition_predicate_budget_is_summed_across_subquery_scopes():
    """The plan's Definition of Done requires the cap fire on the SUM across
    scopes, not just across joins in one scope — otherwise a caller splits the
    ON clause over a `value_subquery` boundary and each level is 'within cap'."""
    inner_join = {
        "table": "bands",
        "condition": {
            "and": [
                {"col": "products.id", "op": "eq", "value_col": "bands.id"},
                {"col": "products.price", "op": "gte", "value_col": "bands.lo"},
                {"col": "products.price", "op": "lte", "value_col": "bands.hi"},
            ]
        },
    }
    query = StructuredQuery.model_validate(
        {
            "from": "products",
            "select": ["products.name"],
            "joins": [inner_join],
            "where": {
                "col": "products.id",
                "op": "in",
                "value_subquery": {
                    "from": "products",
                    "select": ["products.id"],
                    "joins": [inner_join],
                },
            },
        }
    )
    with pytest.raises(PolicyViolationError, match="join condition predicate count 6"):
        validate_policy(query, Policy(max_where_predicates=5, max_joins=5), "demo")


def test_date_add_inside_a_join_condition_is_bounded_by_max_interval_days():
    """`iter_scope_expressions` is the one wiring that carries the date caps into
    an ON clause — documented as inherited, so it gets a test rather than a claim."""
    query = StructuredQuery.model_validate(
        {
            "from": "products",
            "select": ["products.name"],
            "joins": [
                {
                    "table": "bands",
                    "condition": {
                        "col": "products.created_at",
                        "op": "gte",
                        "value_expr": {
                            "date_add": {"col": "bands.created_at"},
                            "unit": "year",
                            "amount": 100_000,
                        },
                    },
                }
            ],
        }
    )
    with pytest.raises(PolicyViolationError, match="max_interval_days"):
        validate_policy(query, Policy(), "demo")


def test_case_branches_inside_a_join_condition_are_bounded():
    """A CASE nested in an ON clause reaches the same branch budget."""
    query = StructuredQuery.model_validate(
        {
            "from": "products",
            "select": ["products.name"],
            "joins": [
                {
                    "table": "bands",
                    "condition": {
                        "col": "products.price",
                        "op": "gte",
                        "value_expr": {
                            "when": [
                                {
                                    "when": {
                                        "col": "bands.id",
                                        "op": "eq",
                                        "value_col": "products.id",
                                    },
                                    "then": {"col": "bands.lo"},
                                }
                                for _ in range(4)
                            ]
                        },
                    },
                }
            ],
        }
    )
    with pytest.raises(PolicyViolationError, match="case when branches"):
        validate_policy(query, Policy(max_case_branches=3), "demo")


def test_cross_join_to_a_denied_table_is_rejected():
    """`cross` is the first join that enters the graph with ZERO column
    references, so table-level policy rests entirely on the structural table set
    that `referenced_tables` collects — not on any ref the visitor found."""
    query = _query([JoinSpec(table="bands", type="cross")])
    policy = Policy(allow_cross_join=True, denied_tables=["bands"])
    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, "demo")


def test_mandatory_row_filter_still_applies_to_a_cross_joined_table():
    """Same structural reliance, on the compiler side: a filtered table pulled in
    by a cross join must still be filtered.

    Scope, stated precisely because a mutation pass showed this test is narrower
    than its name suggests: it pins that `_apply_mandatory_row_filters` filters a
    cross-joined table *given that table is in the compiler's `tables` map*. That
    the map contains it is schema validation's `needed` set, one layer up, which
    adds `join.alias or join.table` for every join unconditionally — and which
    `test_cross_join_returns_the_cartesian_product_when_enabled` exercises
    end-to-end (a real cartesian product is impossible unless both tables were
    reflected).
    """
    from querygate.policy.models import MandatoryRowFilter

    query = _query([JoinSpec(table="bands", type="cross")])
    policy = Policy(
        allow_cross_join=True,
        mandatory_row_filters=[MandatoryRowFilter(table="bands", column="label", value="public")],
    )
    assert "bands.label = " in _sql(query, "postgresql", policy)


def test_every_join_type_has_a_live_both_dialects_case():
    """A join type that renders on both dialects but only *runs* on one is the
    items 75/82 failure mode, so a live case is mandatory for each.

    This gate lives in the UNIT tier rather than beside the cases it guards: the
    differential module is marked `postgres_live` AND `mssql_live`, so a gate
    placed there would only fire in the one CI job that has both servers — the
    check that a new join type was not forgotten would itself be skipped in every
    run a developer normally does. Same placement, and same reason, as item 102's
    DatePart gate.
    """
    import typing

    from tests.integration.test_cross_dialect_differential import _JOIN_TYPE_CASES

    from querygate.query_ast.models import JoinType

    assert set(typing.get_args(JoinType)) == set(_JOIN_TYPE_CASES)
