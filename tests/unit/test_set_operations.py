"""Unit coverage for set operations (TODO.md item 104).

Three groups, matching the three places a new scope container has to be taught
about — the AST's own structural rules, the per-dialect availability of each
operator, and the consumers that used to assume a query has exactly one SELECT
(the audit shape, the applied-mask list, the approval gate, the catalog usage
signals). That last group is the one the plan's own frontier notes call the
recurring miss, so each of its members gets an explicit test here.
"""

from __future__ import annotations

import json
import typing

import pytest

from querygate.audit.events import normalize_query_shape
from querygate.catalog.models import SensitivityClass
from querygate.compiler.dialect_adapters import get_dialect_adapter
from querygate.compiler.sqlalchemy_compiler import applied_column_masks
from querygate.core.exceptions import QueryValidationError
from querygate.execution.approval import query_fingerprint, sensitivity_approval_reasons
from querygate.policy.models import ColumnMask, ColumnMaskKind, Policy
from querygate.query_ast.models import Predicate, SetOpKind, SetOpSpec, StructuredQuery
from querygate.validation.schema_validation import iter_query_scopes, iter_set_op_arms

_COVERED_OPS = {"union", "intersect", "except"}
_PII = SensitivityClass.PII

pytestmark = pytest.mark.unit


def _arm(table: str, *columns: str) -> StructuredQuery:
    return StructuredQuery(from_table=table, select=list(columns) or [f"{table}.id"])


def _union(*arms: StructuredQuery, op: str = "union", all_: bool = False) -> StructuredQuery:
    """Rebuild arm 1 through the CONSTRUCTOR, never `model_copy(update=...)`:
    `model_copy` skips validators in Pydantic v2, so a helper built that way would
    have silently exempted every structural rule this file exists to test."""
    first, *rest = arms
    return StructuredQuery(
        **first.model_dump(exclude={"set_op"}),
        set_op=SetOpSpec(op=op, all_=all_, arms=list(rest)),
    )


# --------------------------------------------------------------------------- #
# AST structural rules                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "arm_overrides,expected",
    [
        ({"order_by": [{"col": "customers.id"}]}, "order_by"),
        ({"limit": 5}, "limit"),
        ({"offset": 5}, "offset"),
        ({"top_n": {"order_by": [{"col": "customers.id"}], "n": 1}}, "top_n"),
    ],
)
def test_an_arm_may_not_carry_a_statement_level_clause(arm_overrides, expected):
    arm = _arm("customers").model_copy(update={}).model_dump(by_alias=True)
    arm.update(arm_overrides)
    with pytest.raises(ValueError, match=expected):
        StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": ["orders.id"],
                "set_op": {"op": "union", "arms": [arm]},
            }
        )


def test_arms_must_project_the_same_number_of_columns():
    with pytest.raises(ValueError, match="same number of columns"):
        _union(_arm("orders", "orders.id"), _arm("customers", "customers.id", "customers.name"))


def test_an_arm_may_not_nest_its_own_set_operation():
    with pytest.raises(ValueError, match="may not carry its own set_op"):
        _union(_arm("orders"), _union(_arm("customers"), _arm("products")))


def test_top_n_cannot_be_combined_with_a_set_operation():
    with pytest.raises(ValueError, match="top_n cannot be combined"):
        StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            top_n={"order_by": [{"col": "orders.id"}], "n": 1},
            set_op=SetOpSpec(op="union", arms=[_arm("customers")]),
        )


# --------------------------------------------------------------------------- #
# Scope enumeration                                                            #
# --------------------------------------------------------------------------- #


def test_arms_are_scopes_at_the_same_depth_as_the_carrying_query():
    """Depth is `max_subquery_depth`'s currency, and an arm is a sibling SELECT —
    not a nesting level. If an arm were yielded at depth+1, a two-arm union would
    consume the subquery-depth budget it has nothing to do with."""
    query = _union(_arm("orders"), _arm("customers"), _arm("products"))
    assert [depth for depth, _ in iter_query_scopes(query)] == [0, 0, 0]
    assert [scope.from_table for _, scope in iter_query_scopes(query)] == [
        "orders",
        "customers",
        "products",
    ]


def test_a_subquery_inside_an_arm_still_gets_its_own_depth():
    nested = _arm("customers")
    arm = StructuredQuery(
        from_table="products",
        select=["products.id"],
        where=Predicate(col="products.id", op="in", value_subquery=nested),
    )
    query = _union(_arm("orders"), arm)
    assert sorted(depth for depth, _ in iter_query_scopes(query)) == [0, 0, 1]


def test_iter_set_op_arms_is_just_the_query_when_there_is_no_set_operation():
    query = _arm("orders")
    assert list(iter_set_op_arms(query)) == [query]


# --------------------------------------------------------------------------- #
# Per-dialect availability                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("dialect", ["postgresql", "mssql", "sqlite"])
@pytest.mark.parametrize("op", ["union", "intersect", "except"])
def test_every_operator_without_all_is_available_on_every_dialect(dialect, op):
    import sqlalchemy as sa

    selects = [sa.select(sa.literal(1)), sa.select(sa.literal(2))]
    compound = get_dialect_adapter(dialect).set_operation(op, False, selects)
    assert op.replace("except", "EXCEPT").upper() in str(compound).upper()


@pytest.mark.parametrize(
    "dialect,op,supported",
    [
        ("postgresql", "union", True),
        ("postgresql", "intersect", True),
        ("postgresql", "except", True),
        ("mssql", "union", True),
        ("mssql", "intersect", False),
        ("mssql", "except", False),
        ("sqlite", "union", True),
        ("sqlite", "intersect", False),
        ("sqlite", "except", False),
    ],
)
def test_the_all_variant_is_postgres_only_for_intersect_and_except(dialect, op, supported):
    """Postgres has all six combinations; T-SQL and SQLite have no `INTERSECT ALL`
    or `EXCEPT ALL` at all. Both render without complaint and fail on the live
    server, so the adapter is the only thing that can catch it."""
    import sqlalchemy as sa

    selects = [sa.select(sa.literal(1)), sa.select(sa.literal(2))]
    adapter = get_dialect_adapter(dialect)
    if supported:
        assert adapter.set_operation(op, True, selects) is not None
    else:
        with pytest.raises(QueryValidationError, match=f"{op.upper()} ALL is not supported"):
            adapter.set_operation(op, True, selects)


def test_every_set_op_kind_is_covered_by_a_per_dialect_case():
    """The exhaustiveness gate every other closed union in this repo has
    (`WindowFn`, `JoinType`, `DatePart`, `IntervalUnit`). A fourth operator added
    to `SetOpKind` must be given a constructor AND a per-dialect case here, or it
    ships renderable-but-untested and `_compound` raises at request time. The
    `_SET_OPERATIONS` map's own comment claims it is exhaustive "for the reason
    the date-part maps are" — this is what makes that claim true."""
    from querygate.compiler.dialect_adapters import _SET_OPERATIONS

    declared = set(typing.get_args(SetOpKind))
    assert declared == set(_SET_OPERATIONS), "an operator has no constructor"
    assert declared == _COVERED_OPS, "an operator has no per-dialect test case above"


# --------------------------------------------------------------------------- #
# The consumers that used to assume one SELECT                                 #
# --------------------------------------------------------------------------- #


def test_the_audit_shape_records_every_arm_and_still_no_literals():
    query = _union(
        StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            where=Predicate(col="orders.status", op="eq", value="paid"),
        ),
        StructuredQuery(
            from_table="customers",
            select=["customers.id"],
            where=Predicate(col="customers.email", op="eq", value="secret@example.com"),
        ),
        all_=True,
    )
    shape = normalize_query_shape(query)
    serialized = json.dumps(shape)

    assert shape["set_op"]["op"] == "union"
    assert shape["set_op"]["all"] is True
    # The arm's own table and predicate structure are there — an audit trail that
    # recorded only "from: orders" would understate what the query read.
    assert shape["set_op"]["arms"][0]["from"] == "customers"
    assert "customers.email" in serialized
    # ...and the arm's literals are redacted exactly as the outer query's are.
    assert "secret@example.com" not in serialized
    assert "paid" not in serialized


def test_applied_column_masks_reports_masks_from_every_arm():
    """The masked-column list feeds the audit trail's "masked, not denied"
    distinction. Reading only the carrying query would say a response was
    unmasked when the compiler had masked half of it."""
    policy = Policy(
        column_masks={
            "customers": [ColumnMask(column="email", kind=ColumnMaskKind.HASH)],
            "employees": [ColumnMask(column="ssn", kind=ColumnMaskKind.LAST, length=4)],
        }
    )
    # Arm 1 masks position 1 (`customers.email`); arm 2 masks position 0
    # (`employees.ssn`). Both are reported, each under ARM 1's output name at that
    # position — so arm 2's masked `ssn` surfaces as the response key "name".
    query = _union(
        _arm("customers", "customers.name", "customers.email"),
        _arm("employees", "employees.ssn", "employees.id"),
    )
    assert applied_column_masks(query, policy) == ["email", "name"]


def test_a_later_arms_mask_is_reported_under_arm_ones_output_name():
    """A compound SELECT takes its column names from its FIRST arm, so a mask
    applied in arm 2 surfaces in the response under arm 1's name at that
    position. Reporting the masked arm's own column name would name a key the
    caller never receives — which is what this function's "matches the response
    column names" contract forbids."""
    policy = Policy(
        column_masks={"employees": [ColumnMask(column="ssn", kind=ColumnMaskKind.HASH)]}
    )
    query = _union(_arm("customers", "customers.email"), _arm("employees", "employees.ssn"))
    # Arm 1 projects `customers.email` (unmasked) at position 0; arm 2's masked
    # `employees.ssn` shares that position, so the response key is "email".
    assert applied_column_masks(query, policy) == ["email"]


def _catalog_with_pii_email() -> None:
    from querygate.catalog.loader import CatalogStore, set_catalog_store

    set_catalog_store(
        CatalogStore.from_dict(
            {
                "connections": {
                    "demo": {
                        "tables": {
                            "customers": {
                                "provenance": {"created_by": "admin"},
                                "columns": {"email": {"sensitivity": "pii"}},
                            }
                        }
                    }
                }
            }
        )
    )


def test_the_sensitivity_approval_gate_sees_a_labelled_column_in_a_later_arm(monkeypatch):
    """Without this, hiding a PII column in arm 2 would run with no human in the
    loop while the identical column in arm 1 required approval."""
    _catalog_with_pii_email()
    policy = Policy(approval_sensitivities=[_PII])
    hidden_in_arm_two = _union(_arm("orders"), _arm("customers", "customers.email"))
    reasons = sensitivity_approval_reasons(hidden_in_arm_two, policy, "demo")
    assert len(reasons) == 1 and "customers.email" in reasons[0]


def test_the_fingerprint_changes_when_an_arm_changes():
    """An approval token binds to the fingerprint, so two set operations that
    differ only in a later arm must not share one — otherwise approving a benign
    union would authorize a different second arm."""
    one = _union(_arm("orders"), _arm("customers"))
    two = _union(_arm("orders"), _arm("employees"))
    assert query_fingerprint(one) != query_fingerprint(two)
    assert query_fingerprint(one) != query_fingerprint(_arm("orders"))


def test_usage_signals_name_every_arms_table():
    from querygate.execution.service import StructuredQueryService

    service = StructuredQueryService("demo")
    query = _union(_arm("orders"), _arm("customers"), _arm("products"))
    tables = {target.table for _kind, target in service._usage_signal_targets(query)}
    assert tables == {"orders", "customers", "products"}


def test_the_sensitivity_gate_also_sees_a_labelled_column_in_an_in_subquery(monkeypatch):
    """The pre-existing hole item 104 closed, pinned separately from the arm case.

    `sensitivity_approval_reasons` walked only the outer query from item 92 until
    item 104, so a labelled column reached through an `IN (subquery)` never tripped
    the human-approval gate. Without this test a "simplification" from
    `iter_query_scopes` to `iter_set_op_arms` — plausible, since the sibling
    `applied_column_masks` uses exactly that walk — keeps the arm test green and
    silently re-opens the hole.
    """
    _catalog_with_pii_email()
    policy = Policy(approval_sensitivities=[_PII])
    hidden_in_subquery = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        where=Predicate(
            col="orders.customer_id",
            op="in",
            value_subquery=_arm("customers", "customers.email"),
        ),
    )
    reasons = sensitivity_approval_reasons(hidden_in_subquery, policy, "demo")
    assert len(reasons) == 1 and "customers.email" in reasons[0]


def test_usage_signals_also_name_a_subquerys_table():
    """The 32C half of the same widening — same revert risk, same one-line guard."""
    from querygate.execution.service import StructuredQueryService

    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        where=Predicate(col="orders.customer_id", op="in", value_subquery=_arm("customers")),
    )
    targets = StructuredQueryService("demo")._usage_signal_targets(query)
    assert {target.table for _kind, target in targets} == {"orders", "customers"}
