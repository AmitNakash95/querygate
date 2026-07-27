"""Unit coverage for named WITH blocks — ctes (TODO.md item 105).

Grouped the way the item's risk is grouped, not the way the code is laid out:

* the structural rules that make a cte namespace unambiguous (and make a
  RECURSIVE cte inexpressible rather than merely forbidden);
* the guardrails a new scope container must not become a channel around — a
  denied table, a mandatory row filter, a masked column, a cap;
* compilation, including the two things that can only be settled by measuring —
  that the names the validator PREDICTS a block projects are the names SQLAlchemy
  actually labels, and that a block carries no row cap;
* the consumers that assumed a query has one scope, which the plan's own frontier
  notes call the recurring miss.

TODO.md item 122 also lands here (`_unique_column_sets`), because a cte join is
what surfaced it, but note it is reproducible with no cte at all.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mssql, postgresql

from querygate.audit.events import normalize_query_shape
from querygate.catalog.models import SensitivityClass
from querygate.compiler.sqlalchemy_compiler import (
    applied_column_masks,
    compile_structured_query,
    _unique_column_sets,
)
from querygate.core.exceptions import PolicyViolationError, QueryValidationError
from querygate.execution.service import StructuredQueryService
from querygate.policy.models import (
    ColumnMask,
    ColumnMaskKind,
    MandatoryRowFilter,
    Policy,
)
from querygate.query_ast.models import CteSpec, StructuredQuery
from querygate.validation import schema_validation as sv
from querygate.validation.policy_validation import (
    referenced_tables_tree_wide,
    validate_policy,
)
from querygate.validation.schema_validation import (
    _cte_projection_table,
    cte_chain_depths,
    declared_cte_names,
    iter_query_scopes,
    select_item_output_name,
)

pytestmark = pytest.mark.unit

_CONN = "demo"


def _metadata() -> dict:
    md = sa.MetaData()
    return {
        "orders": sa.Table(
            "orders",
            md,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("customer_id", sa.Integer),
            sa.Column("amount", sa.Numeric(10, 2)),
            sa.Column("status", sa.String),
            sa.Column("created_at", sa.DateTime),
        ),
        "customers": sa.Table(
            "customers",
            md,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("name", sa.String),
            sa.Column("region", sa.String),
            sa.Column("ssn", sa.String),
        ),
    }


def _totals_cte(**body) -> dict:
    """The canonical aggregate-then-join block: per-customer order totals."""
    return {
        "name": "totals",
        "query": {
            "from": "orders",
            "select": [
                "orders.customer_id",
                {"fn": "sum", "col": "orders.amount", "as": "total"},
            ],
            "group_by": ["orders.customer_id"],
            **body,
        },
    }


def _joins_totals(**outer) -> dict:
    return {
        "ctes": [_totals_cte()],
        "from": "customers",
        "joins": [{"table": "totals", "on": ["customers.id", "totals.customer_id"]}],
        "select": ["customers.name", "totals.total"],
        **outer,
    }


def _query(**kwargs) -> StructuredQuery:
    return StructuredQuery.model_validate(kwargs)


async def _validate_schema(query: StructuredQuery, scope_tables: dict | None = None):
    """Run the real `validate_schema` against the one seam that touches a database,
    per the conftest gotcha — never by mocking SQLAlchemy internals."""
    tables = _metadata()

    async def _fake_load(connection_id, table_name, table_connection):
        try:
            return tables[table_name.lower()]
        except KeyError:  # pragma: no cover - a test asking for an unknown table
            raise QueryValidationError(f"no such table {table_name!r}")

    with patch.object(sv, "_load_table", AsyncMock(side_effect=_fake_load)):
        return await sv.validate_schema(query, _CONN, scope_tables=scope_tables)


async def _compile(query: StructuredQuery, policy: Policy | None = None, dialect="postgresql"):
    """Validate then compile, exactly as `execution/service.py` sequences it — so a
    test can never accidentally prove something about a query the pipeline would
    have rejected before it ever reached the compiler."""
    policy = policy or Policy()
    validate_policy(query, policy, _CONN)
    scope_tables: dict = {}
    outer = await _validate_schema(query, scope_tables=scope_tables)
    return compile_structured_query(
        query, outer, policy, dialect=dialect, subquery_tables=scope_tables
    )


def _sql(stmt, dialect=postgresql) -> str:
    return str(stmt.compile(dialect=dialect.dialect(), compile_kwargs={"literal_binds": True}))


# --------------------------------------------------------------------------- #
# Structural rules — an unambiguous namespace                                  #
# --------------------------------------------------------------------------- #


def test_duplicate_cte_names_are_rejected_case_insensitively():
    with pytest.raises(ValueError, match="Duplicate cte name"):
        _query(
            ctes=[_totals_cte(), {**_totals_cte(), "name": "TOTALS"}],
            **{"from": "totals", "select": ["totals.total"]},
        )


@pytest.mark.parametrize("bad", ["1totals", "to-tals", "to tals", "totals;--", ""])
def test_a_cte_name_must_be_a_plain_identifier(bad):
    with pytest.raises(ValueError):
        CteSpec.model_validate({"name": bad, "query": {"from": "orders", "select": ["orders.id"]}})


@pytest.mark.parametrize(
    "container",
    [
        pytest.param("arm", id="set_op_arm"),
        pytest.param("subquery", id="in_subquery"),
        pytest.param("body", id="another_cte_body"),
    ],
)
def test_only_the_root_query_may_declare_ctes(container):
    """One declaration site. A second namespace would not be in
    `declared_cte_names(root)`, which every consumer trusts as complete."""
    inner = {"ctes": [_totals_cte()], "from": "totals", "select": ["totals.total"]}
    if container == "arm":
        query = _query(
            **{"from": "orders", "select": ["orders.id"]},
            set_op={"op": "union", "arms": [inner]},
        )
    elif container == "subquery":
        query = _query(
            **{"from": "orders", "select": ["orders.id"]},
            where={"col": "orders.customer_id", "op": "in", "value_subquery": inner},
        )
    else:
        query = _query(
            ctes=[{"name": "outer_block", "query": inner}],
            **{"from": "outer_block", "select": ["outer_block.total"]},
        )
    with pytest.raises(PolicyViolationError, match="only the top-level query may declare"):
        validate_policy(query, Policy(), _CONN)


def test_a_cte_may_not_reference_a_later_cte():
    query = _query(
        ctes=[
            {"name": "first", "query": {"from": "second", "select": ["second.id"]}},
            {"name": "second", "query": {"from": "orders", "select": ["orders.id"]}},
        ],
        **{"from": "first", "select": ["first.id"]},
    )
    with pytest.raises(PolicyViolationError, match="declared later"):
        validate_policy(query, Policy(max_subquery_depth=5), _CONN)


def test_a_cte_may_not_reference_itself_so_recursion_is_inexpressible():
    """Recursive cte is out of scope for item 105 (unbounded recursion is a real
    DoS). It is excluded *structurally* — by the earlier-only rule — rather than
    by a separate check that could be forgotten."""
    query = _query(
        ctes=[{"name": "loop", "query": {"from": "loop", "select": ["loop.id"]}}],
        **{"from": "loop", "select": ["loop.id"]},
    )
    with pytest.raises(PolicyViolationError, match="declared later or is itself"):
        validate_policy(query, Policy(max_subquery_depth=5), _CONN)


def test_a_cte_may_reference_an_earlier_cte():
    query = _query(
        ctes=[
            _totals_cte(),
            {
                "name": "big",
                "query": {
                    "from": "totals",
                    "select": ["totals.customer_id", "totals.total"],
                    "where": {"col": "totals.total", "op": "gt", "value": 100},
                },
            },
        ],
        **{"from": "big", "select": ["big.customer_id"]},
    )
    validate_policy(query, Policy(max_subquery_depth=2), _CONN)


def test_a_cte_name_may_not_shadow_a_table_the_policy_has_a_rule_for():
    """A cte name always wins name resolution, so a block called `orders` makes
    every rule the operator wrote about the TABLE `orders` unreachable here."""
    query = _query(
        ctes=[{"name": "orders", "query": {"from": "customers", "select": ["customers.id"]}}],
        **{"from": "orders", "select": ["orders.id"]},
    )
    # No rule about `orders` -> nothing to shadow, so this is allowed.
    validate_policy(query, Policy(), _CONN)
    # A policy rule about `orders` -> the block would make it unverifiable here.
    governed = Policy(
        mandatory_row_filters=[MandatoryRowFilter(table="orders", column="status", value="paid")]
    )
    with pytest.raises(PolicyViolationError, match="policy has a rule for"):
        validate_policy(query, governed, _CONN)


def test_a_declared_but_unreferenced_cte_is_rejected():
    """It would cost a scope against every tree-wide cap while SQLAlchemy renders
    nothing — so the caps would be counting structure the SQL does not contain."""
    query = _query(
        ctes=[_totals_cte()],
        **{"from": "customers", "select": ["customers.name"]},
    )
    with pytest.raises(PolicyViolationError, match="declared but never referenced"):
        validate_policy(query, Policy(), _CONN)


def test_a_cte_reference_may_not_set_a_join_connection():
    query = _query(
        **_joins_totals(),
    )
    query.joins[0].connection = "other"
    with pytest.raises(QueryValidationError, match="may not set `connection`"):
        sv.resolve_query_table_connections(query, _CONN, cte_names={"totals"})


# --------------------------------------------------------------------------- #
# Caps                                                                          #
# --------------------------------------------------------------------------- #


def test_max_cte_count_is_enforced():
    ctes = [
        {"name": f"b{i}", "query": {"from": "orders", "select": ["orders.id"]}} for i in range(4)
    ]
    query = _query(
        ctes=ctes,
        **{
            "from": "b0",
            "joins": [{"table": f"b{i}", "on": ["b0.id", f"b{i}.id"]} for i in range(1, 4)],
            "select": ["b0.id"],
        },
    )
    with pytest.raises(PolicyViolationError, match="ctes exceeds max of 3"):
        validate_policy(query, Policy(max_cte_count=3, max_joins=10), _CONN)


def test_independent_ctes_each_cost_one_level_but_a_chain_accumulates():
    """Depth is the REFERENCE CHAIN, not declaration position — writing two
    unrelated blocks must not punish the second for being written second."""
    two_independent = _query(
        ctes=[
            {"name": "a", "query": {"from": "orders", "select": ["orders.id"]}},
            {"name": "b", "query": {"from": "customers", "select": ["customers.id"]}},
        ],
        **{
            "from": "a",
            "joins": [{"table": "b", "on": ["a.id", "b.id"]}],
            "select": ["a.id"],
        },
    )
    assert cte_chain_depths(two_independent) == {"a": 1, "b": 1}
    validate_policy(two_independent, Policy(max_subquery_depth=1), _CONN)

    chained = _query(
        ctes=[
            _totals_cte(),
            {"name": "big", "query": {"from": "totals", "select": ["totals.total"]}},
        ],
        **{"from": "big", "select": ["big.total"]},
    )
    assert cte_chain_depths(chained) == {"totals": 1, "big": 2}
    with pytest.raises(PolicyViolationError, match="exceeds max_subquery_depth"):
        validate_policy(chained, Policy(max_subquery_depth=1), _CONN)


def test_count_caps_are_summed_across_cte_bodies():
    """A block must not be a fresh budget — the item-97 loophole, reopened by any
    new scope container that forgets to be counted."""
    query = _query(
        ctes=[
            {
                "name": "wide",
                "query": {
                    "from": "orders",
                    "select": ["orders.id", "orders.amount", "orders.status"],
                },
            }
        ],
        **{"from": "wide", "select": ["wide.id", "wide.amount"]},
    )
    validate_policy(query, Policy(max_select_columns=5), _CONN)
    with pytest.raises(PolicyViolationError, match="select exceeds max"):
        validate_policy(query, Policy(max_select_columns=4), _CONN)


# --------------------------------------------------------------------------- #
# A cte is not a channel around a guardrail                                    #
# --------------------------------------------------------------------------- #


def test_a_denied_table_cannot_be_reached_through_a_cte():
    query = _query(**_joins_totals())
    with pytest.raises(PolicyViolationError, match="orders"):
        validate_policy(query, Policy(denied_tables=["orders"]), _CONN)


def test_a_denied_column_cannot_be_reached_through_a_cte():
    query = _query(**_joins_totals())
    with pytest.raises(PolicyViolationError, match="orders.amount"):
        validate_policy(query, Policy(denied_columns={"orders": ["amount"]}), _CONN)


def test_a_cte_name_is_not_itself_subject_to_table_allow_deny():
    """The block's name is not a table, so an allow-list must not reject it —
    while the tables INSIDE it are still checked, at the source."""
    query = _query(**_joins_totals())
    validate_policy(query, Policy(allowed_tables=["orders", "customers"]), _CONN)
    with pytest.raises(PolicyViolationError):
        validate_policy(query, Policy(allowed_tables=["customers"]), _CONN)


def test_a_masked_column_may_not_be_projected_by_a_cte():
    """Within the block this is a bare projection, but the block's rows are an
    INPUT to another scope, where the value could be filtered or joined on."""
    query = _query(
        ctes=[{"name": "people", "query": {"from": "customers", "select": ["customers.ssn"]}}],
        **{"from": "people", "select": ["people.ssn"]},
    )
    policy = Policy(
        column_masks={"customers": [ColumnMask(column="ssn", kind=ColumnMaskKind.HASH)]}
    )
    with pytest.raises(PolicyViolationError, match="cannot be projected by cte"):
        validate_policy(query, policy, _CONN)


async def test_a_mandatory_row_filter_applies_inside_a_cte_body():
    policy = Policy(
        mandatory_row_filters=[MandatoryRowFilter(table="orders", column="status", value="paid")]
    )
    stmt, _limit = await _compile(_query(**_joins_totals()), policy)
    sql = _sql(stmt)
    # The filter must land INSIDE the WITH body, where the rows are read.
    body = sql.split("WITH totals AS")[1].split(" SELECT customers.name")[0]
    assert "status = 'paid'" in body


async def test_applied_column_masks_reports_nothing_from_a_cte_because_none_can_exist():
    """Pins the pairing documented on `applied_column_masks`: it walks no cte body
    ONLY because a masked column cannot be projected by one. If that validation
    rule were relaxed, this test and that docstring are the pointer."""
    policy = Policy(
        column_masks={"customers": [ColumnMask(column="ssn", kind=ColumnMaskKind.HASH)]}
    )
    query = _query(**_joins_totals())
    assert applied_column_masks(query, policy) == []
    masked_body = _query(
        ctes=[{"name": "people", "query": {"from": "customers", "select": ["customers.ssn"]}}],
        **{"from": "people", "select": ["people.ssn"]},
    )
    with pytest.raises(PolicyViolationError):
        validate_policy(masked_body, policy, _CONN)


# --------------------------------------------------------------------------- #
# Compilation                                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("dialect,module", [("postgresql", postgresql), ("mssql", mssql)])
async def test_a_cte_renders_as_a_real_with_clause_on_both_dialects(dialect, module):
    stmt, _limit = await _compile(_query(**_joins_totals()), dialect=dialect)
    sql = _sql(stmt, module)
    assert "WITH totals AS" in sql
    assert "JOIN totals ON" in sql


async def test_a_cte_body_carries_no_row_cap_unless_the_caller_asked_for_one():
    """`max_rows` bounds the RESPONSE; a block's rows are input to a join. Clamping
    would silently truncate the population a total is computed over — a wrong
    answer, which items 102/117 established is worse than a rejection."""
    stmt, _limit = await _compile(_query(**_joins_totals()), Policy(default_limit=7, max_limit=7))
    sql = _sql(stmt)
    body, outer = sql.split("WITH totals AS")[1].split(" SELECT customers.name")
    assert "LIMIT" not in body
    assert "LIMIT 7" in outer


async def test_an_explicit_limit_inside_a_cte_is_honoured_and_clamped():
    query = _query(
        ctes=[_totals_cte(limit=500)],
        **{
            "from": "customers",
            "joins": [{"table": "totals", "on": ["customers.id", "totals.customer_id"]}],
            "select": ["customers.name", "totals.total"],
        },
    )
    stmt, _limit = await _compile(query, Policy(max_limit_aggregate=100))
    body = _sql(stmt).split("WITH totals AS")[1].split(" SELECT customers.name")[0]
    assert "LIMIT 100" in body


async def test_a_cte_can_be_the_from_table_and_a_chained_cte_compiles():
    query = _query(
        ctes=[
            _totals_cte(),
            {
                "name": "big",
                "query": {
                    "from": "totals",
                    "select": ["totals.customer_id", "totals.total"],
                    "where": {"col": "totals.total", "op": "gt", "value": 100},
                },
            },
        ],
        **{"from": "big", "select": ["big.customer_id", "big.total"]},
    )
    stmt, _limit = await _compile(query, Policy(max_subquery_depth=2))
    sql = _sql(stmt)
    assert sql.count(" AS \n(SELECT") == 2 or sql.count("AS \n(SELECT") == 2
    assert "FROM big" in sql


@pytest.mark.parametrize(
    "item",
    [
        "orders.customer_id",
        {"fn": "sum", "col": "orders.amount", "as": "total"},
        {"fn": "sum", "col": "orders.amount"},
        {"fn": "count", "col": "*", "as": "n"},
        {"col": "orders.created_at", "granularity": "month"},
        {"col": "orders.created_at", "granularity": "month", "as": "m"},
        {"col": "orders.status", "delimiter": ","},
        {"fn": "lower", "args": [{"col": "orders.status"}]},
        {"expr": {"op": "+", "left": {"col": "orders.amount"}, "right": {"literal": 1}}, "as": "x"},
        {
            "when": [
                {"when": {"col": "orders.amount", "op": "gt", "value": 1}, "then": {"literal": 1}}
            ],
            "as": "flag",
        },
    ],
)
async def test_predicted_output_names_match_what_sqlalchemy_actually_labels(item):
    """THE drift guard for `select_item_output_name`. The validator resolves the
    outer query's `block.column` refs against the names it PREDICTS, while the
    compiler labels the real columns — so agreeing with itself is not enough, it
    has to agree with SQLAlchemy. Every select-item shape is checked, because the
    failure is silent: a ref validated against a name the SQL does not have."""
    spec = CteSpec.model_validate({"name": "block", "query": {"from": "orders", "select": [item]}})
    tables = {"orders": _metadata()["orders"]}
    predicted = [select_item_output_name(i, tables) for i in spec.query.select]

    inner, _limit = compile_structured_query(spec.query, tables, Policy(), dialect="postgresql")
    actual = list(inner.limit(None).cte(name="block").c.keys())
    assert predicted == actual


def test_select_item_output_name_fails_closed_on_an_unknown_item_type():
    class Rogue:
        pass

    with pytest.raises(QueryValidationError, match="without being given an output name"):
        select_item_output_name(Rogue(), {})


# --------------------------------------------------------------------------- #
# Consumers that used to assume one scope                                      #
# --------------------------------------------------------------------------- #


def test_iter_query_scopes_yields_every_cte_body():
    query = _query(**_joins_totals())
    scopes = [scope for _depth, scope in iter_query_scopes(query)]
    assert query.ctes[0].query in scopes
    assert declared_cte_names(query) == frozenset({"totals"})


def test_the_audit_shape_records_each_cte_body_and_leaks_no_literal():
    query = _query(
        ctes=[_totals_cte(where={"col": "orders.status", "op": "eq", "value": "SECRET-LITERAL"})],
        **{
            "from": "customers",
            "joins": [{"table": "totals", "on": ["customers.id", "totals.customer_id"]}],
            "select": ["customers.name", "totals.total"],
        },
    )
    shape = normalize_query_shape(query)
    assert [block["name"] for block in shape["ctes"]] == ["totals"]
    # Without this the event would claim the query read a table called `totals`
    # and nothing else — the real tables are all inside the block.
    assert shape["ctes"][0]["query"]["from"] == "orders"
    assert "SECRET-LITERAL" not in str(shape)


def test_referenced_tables_tree_wide_reports_body_tables_not_the_cte_name():
    query = _query(**_joins_totals())
    assert referenced_tables_tree_wide(query) == {"customers", "orders"}


def test_the_catalog_usage_signals_do_not_learn_a_phantom_table():
    """A cte name is a stage this statement computes. Teaching 32C a table by that
    name would put a phantom into the catalog no refresh could reconcile."""
    query = _query(**_joins_totals())
    targets = StructuredQueryService(_CONN)._usage_signal_targets(query)
    assert {target.table for _kind, target in targets} == {"customers", "orders"}


def test_the_sensitivity_approval_gate_sees_a_labelled_column_inside_a_cte(monkeypatch):
    from querygate.execution import approval as approval_module

    class _Column:
        sensitivity = SensitivityClass.PII

    class _Entry:
        sensitivity = SensitivityClass.NONE

        def column(self, name):
            return _Column() if name == "ssn" else None

    class _Store:
        def get_table(self, connection_id, table):
            return _Entry() if table == "customers" else None

    monkeypatch.setattr(approval_module, "get_catalog_store", lambda: _Store())
    query = _query(
        ctes=[
            {
                "name": "people",
                "query": {
                    "from": "customers",
                    "select": ["customers.id"],
                    "where": {"col": "customers.ssn", "op": "eq", "value": "x"},
                },
            }
        ],
        **{"from": "people", "select": ["people.id"]},
    )
    reasons = approval_module.sensitivity_approval_reasons(
        query, Policy(approval_sensitivities=[SensitivityClass.PII]), _CONN
    )
    assert reasons and "customers.ssn" in reasons[0]


def test_the_approval_gate_does_not_report_the_cte_name_as_a_labelled_table(monkeypatch):
    """A catalog entry sharing the block's name must not produce a hit naming a
    table the query never read."""
    from querygate.execution import approval as approval_module

    class _Entry:
        sensitivity = SensitivityClass.PII

        def column(self, name):
            return None

    class _Store:
        def get_table(self, connection_id, table):
            return _Entry() if table == "totals" else None

    monkeypatch.setattr(approval_module, "get_catalog_store", lambda: _Store())
    reasons = approval_module.sensitivity_approval_reasons(
        _query(**_joins_totals()), Policy(approval_sensitivities=[SensitivityClass.PII]), _CONN
    )
    assert reasons == []


# --------------------------------------------------------------------------- #
# TODO.md item 122 — _unique_column_sets on a non-Table FROM element           #
# --------------------------------------------------------------------------- #


def test_unique_column_sets_handles_every_from_element_shape():
    """Only a `Table` carries reflected uniqueness metadata. `Alias`/`Subquery`/
    `CTE` expose `.primary_key` as a bare `ColumnSet` with no `.columns`, which
    used to raise `AttributeError` here."""
    orders = _metadata()["orders"]
    assert _unique_column_sets(orders) == [{"id"}]
    # An alias is the same rows under a new name, so uniqueness is preserved.
    assert _unique_column_sets(orders.alias("o")) == [{"id"}]
    # A computed stage has no declared uniqueness -> "can fan out" (fail closed).
    assert _unique_column_sets(sa.select(orders.c.id).subquery()) == []


async def test_min_group_size_with_an_aliased_join_decides_instead_of_crashing():
    """TODO.md item 122, reproducible with NO cte: before the fix this raised
    `AttributeError` — a 500 where a policy answer belonged."""
    onto_pk = _query(
        **{
            "from": "customers",
            "joins": [{"table": "orders", "alias": "o", "on": ["customers.id", "o.id"]}],
            "select": ["customers.region", {"fn": "count", "col": "*", "as": "n"}],
            "group_by": ["customers.region"],
        }
    )
    stmt, _limit = await _compile(onto_pk, Policy(min_group_size=5))
    assert "count(*) >= 5" in _sql(stmt)

    fans_out = _query(
        **{
            "from": "customers",
            "joins": [{"table": "orders", "alias": "o", "on": ["customers.id", "o.customer_id"]}],
            "select": ["customers.region", {"fn": "count", "col": "*", "as": "n"}],
            "group_by": ["customers.region"],
        }
    )
    with pytest.raises(PolicyViolationError, match="can match more than one row"):
        await _compile(fans_out, Policy(min_group_size=5))


async def test_a_join_onto_a_cte_is_refused_under_the_k_anonymity_floor():
    """A computed stage may hold several rows per join key, and item 118's floor
    counts JOINED rows — so assuming uniqueness would reopen exactly that leak."""
    with pytest.raises(PolicyViolationError, match="can match more than one row"):
        await _compile(
            _query(
                ctes=[_totals_cte()],
                **{
                    "from": "customers",
                    "joins": [{"table": "totals", "on": ["customers.id", "totals.customer_id"]}],
                    "select": ["customers.region", {"fn": "count", "col": "*", "as": "n"}],
                    "group_by": ["customers.region"],
                },
            ),
            Policy(min_group_size=5),
        )


# --------------------------------------------------------------------------- #
# A cte OUTPUT name is not a physical column                                   #
# --------------------------------------------------------------------------- #
#
# These four cases were added because mutation testing found the guards they
# cover were unpinned: breaking each left the suite green. A WILDCARD policy rule
# (`{"*": [...]}`) is what makes the difference observable — with a table-scoped
# rule the lookup misses either way, so only a wildcard proves the skip is load-
# bearing rather than incidental.


def test_a_wildcard_column_deny_does_not_fire_on_a_cte_output_name():
    """`denied_columns {"*": ["total"]}` denies a physical column called `total`.
    A block's `total` is a computed value that never existed as a column, and the
    real column it came from (`orders.amount`) was checked inside the block."""
    query = _query(**_joins_totals(order_by=[{"col": "totals.total", "dir": "desc"}]))
    validate_policy(query, Policy(denied_columns={"*": ["total"]}), _CONN)
    # ...while the physical column the block actually reads is still denied.
    with pytest.raises(PolicyViolationError, match="orders.amount"):
        validate_policy(query, Policy(denied_columns={"*": ["amount"]}), _CONN)


def test_a_wildcard_mask_does_not_fire_on_a_cte_output_name():
    """Same shape for item 49's rule: a masked column may only surface as a bare
    projection, so an ORDER BY on one is rejected — but a block's output name is
    not that column."""
    query = _query(**_joins_totals(order_by=[{"col": "totals.total", "dir": "desc"}]))
    wildcard = Policy(column_masks={"*": [ColumnMask(column="total", kind=ColumnMaskKind.HASH)]})
    validate_policy(query, wildcard, _CONN)
    # ...while a masked column inside the block is still refused as its output.
    masked_source = Policy(
        column_masks={"*": [ColumnMask(column="amount", kind=ColumnMaskKind.HASH)]}
    )
    with pytest.raises(PolicyViolationError, match="cannot be projected by cte"):
        validate_policy(query, masked_source, _CONN)


async def test_the_compiler_skips_a_mandatory_filter_named_after_a_cte_even_unvalidated():
    """Defence in depth, tested by DELIBERATELY bypassing `validate_policy` — which
    would have rejected this query under the governed-name rule. Without the
    compile-side skip, the filter would be applied to the BLOCK's output: wrong
    rows if it projects a column of that name, a compiler-internal error if not.
    """
    query = _query(
        ctes=[{"name": "orders", "query": {"from": "customers", "select": ["customers.id"]}}],
        **{"from": "orders", "select": ["orders.id"]},
    )
    policy = Policy(
        mandatory_row_filters=[MandatoryRowFilter(table="orders", column="status", value="paid")]
    )
    scope_tables: dict = {}
    outer = await _validate_schema(query, scope_tables=scope_tables)
    stmt, _limit = compile_structured_query(
        query, outer, policy, dialect="postgresql", subquery_tables=scope_tables
    )
    sql = _sql(stmt)
    assert "status" not in sql


def test_the_catalog_usage_signals_skip_a_cte_used_as_the_from_table():
    """Companion to the join-reference case: the `from` branch needs its own
    coverage, which mutation testing showed it did not have."""
    query = _query(
        ctes=[_totals_cte()],
        **{"from": "totals", "select": ["totals.total"]},
    )
    targets = StructuredQueryService(_CONN)._usage_signal_targets(query)
    assert {target.table for _kind, target in targets} == {"orders"}


# --------------------------------------------------------------------------- #
# Findings from this item's own completion audit                               #
# --------------------------------------------------------------------------- #


async def test_a_cte_projecting_two_columns_of_the_same_name_is_refused_cleanly():
    """A block's columns are referred to BY NAME, so two called `id` leave
    `block.id` with no defined meaning. SQLAlchemy would quietly rename the second
    to `id_1` — item 119's exact failure, where the caller asked for two columns
    and one silently becomes unreachable under the name they used.

    Before the fix this escaped as a raw `sqlalchemy.exc.DuplicateColumnError`
    from inside validation: a 500 rather than a typed 4xx.
    """
    query = _query(
        ctes=[
            {
                "name": "pairs",
                "query": {
                    "from": "orders",
                    "joins": [{"table": "customers", "on": ["orders.customer_id", "customers.id"]}],
                    "select": ["orders.id", "customers.id"],
                },
            }
        ],
        **{"from": "pairs", "select": ["pairs.id"]},
    )
    with pytest.raises(QueryValidationError, match="more than one column named"):
        await _validate_schema(query)

    # Aliasing one of them resolves the ambiguity — the primitive the message
    # points at, so the rejection is a redirect rather than a wall.
    fixed = _query(
        ctes=[
            {
                "name": "pairs",
                "query": {
                    "from": "orders",
                    "joins": [{"table": "customers", "on": ["orders.customer_id", "customers.id"]}],
                    "select": ["orders.id", {"expr": {"col": "customers.id"}, "as": "cust_id"}],
                },
            }
        ],
        **{"from": "pairs", "select": ["pairs.id", "pairs.cust_id"]},
    )
    await _validate_schema(fixed)


def test_max_cte_count_is_checked_before_the_per_block_scope_walks():
    """The cap is the only O(1) rule in `_validate_cte_constraints`; rule 2 walks
    every block's scope tree. Checking the cap last let a caller drive O(N x tree)
    validation work with N far above the cap before being told the cap existed.

    Asserted by giving the over-cap query a SECOND violation that a later rule
    would report: the cap message must win, which it only does if it runs first.
    """
    ctes = [
        # Every one of these also violates the never-referenced rule (rule 4).
        {"name": f"b{i}", "query": {"from": "orders", "select": ["orders.id"]}}
        for i in range(9)
    ]
    query = _query(ctes=ctes, **{"from": "b0", "select": ["b0.id"]})
    with pytest.raises(PolicyViolationError, match="ctes exceeds max of 3"):
        validate_policy(query, Policy(max_cte_count=3), _CONN)


@pytest.mark.parametrize(
    "container",
    [
        pytest.param("arm", id="set_op_arm_reads_a_block"),
        pytest.param("subquery", id="in_subquery_reads_a_block"),
    ],
)
async def test_a_nested_scope_can_READ_a_block_even_though_it_cannot_declare_one(container):
    """The other half of the one-declaration-site rule, and the half that was
    working but unproven: a block is visible to the WHOLE statement, so an arm or
    an `IN (subquery)` may reference one — it just may not declare its own.

    This pins the `cte_objects` threading through `_compile_set_operation` and
    `_WhereCtx`. Without it a nested scope would resolve the name against the
    validation placeholder and emit a reference to a table that does not exist.
    """
    block = {"name": "block", "query": {"from": "orders", "select": ["orders.customer_id"]}}
    if container == "arm":
        query = _query(
            ctes=[block],
            **{"from": "block", "select": ["block.customer_id"]},
            set_op={"op": "union", "arms": [{"from": "orders", "select": ["orders.customer_id"]}]},
        )
    else:
        # The block is referenced ONLY from inside the subquery — the outer query
        # never names it. That is what makes this case discriminating: nothing
        # else can put the WITH clause into the statement, so if `_WhereCtx` did
        # not carry `cte_objects`, the subquery would bind the validation
        # placeholder and emit `FROM block` with no block defined anywhere.
        # (An earlier version of this test ALSO joined the block in the outer
        # query, which rendered identically either way and proved nothing — the
        # mutation survived it.)
        query = _query(
            ctes=[block],
            **{"from": "customers", "select": ["customers.name"]},
            where={
                "col": "customers.id",
                "op": "in",
                "value_subquery": {"from": "block", "select": ["block.customer_id"]},
            },
        )
    stmt, _limit = await _compile(query, Policy(max_subquery_depth=3))
    sql = _sql(stmt)
    assert "WITH block AS" in sql
    # Exactly one WITH clause, however many scopes reference it — the block is
    # compiled once and referenced by name, which is the whole point of the shape.
    assert sql.count("WITH block AS") == 1
