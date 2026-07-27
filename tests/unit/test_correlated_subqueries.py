"""Unit coverage for correlated / EXISTS / scalar subqueries (TODO.md item 106).

The adversarial cases live in `tests/security/test_correlation_boundary.py`; this
file covers the three groups that file does not:

* the AST's own structural rules for the new operator and the new declaration;
* **the consumers that assume a query has one scope** — the audit shape, the
  catalog usage signals, the item-92 approval gate and `referenced_tables`. Item
  106 adds a scope container, and the plan's frontier notes call this group the
  recurring miss; item 105 was tested for it and this item initially was not;
* composition with the OTHER scope containers (a cte, a set-operation arm), which
  is where item 104's hardest work actually was.
"""

from __future__ import annotations

import pytest

from querygate.audit.events import normalize_query_shape
from querygate.catalog.models import SensitivityClass
from querygate.core.exceptions import PolicyViolationError
from querygate.execution.service import StructuredQueryService
from querygate.policy.models import Policy
from querygate.query_ast.models import EXISTS_OPS, Predicate, StructuredQuery
from querygate.validation.policy_validation import (
    referenced_tables_tree_wide,
    validate_policy,
)
from querygate.validation.schema_validation import iter_correlations, iter_query_scopes

pytestmark = pytest.mark.unit

_CONN = "demo"
_DEEP = Policy(max_subquery_depth=2)


def _q(**kwargs) -> StructuredQuery:
    return StructuredQuery.model_validate(kwargs)


def _correlated_exists(op: str = "exists", *, where: dict | None = None) -> dict:
    return {
        "op": op,
        "exists_subquery": {
            "from": "orders",
            "select": ["orders.id"],
            "correlate": ["customers.id"],
            "where": where
            or {"col": "orders.customer_id", "op": "eq", "value_col": "customers.id"},
        },
    }


def _customers_with_orders(op: str = "exists") -> StructuredQuery:
    return _q(
        **{"from": "customers", "select": ["customers.name"]},
        where=_correlated_exists(op),
    )


# --------------------------------------------------------------------------- #
# The declaration and the operator                                             #
# --------------------------------------------------------------------------- #


def test_iter_correlations_pairs_each_ref_with_the_scope_it_resolves_against():
    """The parent is carried explicitly because correlation is the ONE place a
    reference does not resolve against the scope it is written in."""
    query = _customers_with_orders()
    correlations = list(iter_correlations(query))
    assert len(correlations) == 1
    correlation = correlations[0]
    assert correlation.ref == "customers.id"
    assert correlation.parent is query
    assert correlation.child is query.where.exists_subquery


def test_an_exists_subquery_is_enumerated_as_its_own_scope():
    query = _customers_with_orders()
    scopes = [(depth, scope) for depth, scope in iter_query_scopes(query)]
    assert scopes[0] == (0, query)
    assert (1, query.where.exists_subquery) in scopes


def test_a_declared_but_unused_correlate_is_rejected():
    """Dead structure: it widens the nested scope's namespace and spends
    `max_correlated_refs` while contributing nothing to the SQL. Item 105 rejects
    an unreferenced cte for the same reason, and the two containers should not
    disagree about whether a declaration that does nothing is acceptable."""
    query = _q(
        **{"from": "customers", "select": ["customers.name"]},
        where=_correlated_exists(where={"col": "orders.id", "op": "gt", "value": 0}),
    )
    with pytest.raises(PolicyViolationError, match="declared but never referenced"):
        validate_policy(query, _DEEP, _CONN)


def test_the_exists_operators_are_named_once_and_agree_with_the_predicate_rules():
    """`EXISTS_OPS` is the single definition three validators and the compiler read;
    a member added to the operator literal but not here would silently take the
    left-hand-side path and fail on an AST the model permits."""
    for op in EXISTS_OPS:
        predicate = Predicate.model_validate(
            {"op": op, "exists_subquery": {"from": "orders", "select": ["orders.id"]}}
        )
        assert predicate.col is None and predicate.value is None


# --------------------------------------------------------------------------- #
# Consumers that assume one scope                                              #
# --------------------------------------------------------------------------- #


def test_the_audit_shape_records_the_exists_scope_and_leaks_no_literal():
    """The Proof-pillar surface. Without this the event says the query read
    `customers` when it also read `orders` — the reasoning item 104 used for arms
    and item 105 for cte bodies, applied to the third container.

    The audit normalizer is the touchpoint the plan's frontier note calls the
    recurring miss (items 102 and 103 both missed it), so it gets its own test
    rather than riding on an end-to-end assertion.
    """
    query = _q(
        **{"from": "customers", "select": ["customers.name"]},
        where=_correlated_exists(
            where={
                "and": [
                    {"col": "orders.customer_id", "op": "eq", "value_col": "customers.id"},
                    {"col": "orders.status", "op": "eq", "value": "SECRET-LITERAL"},
                ]
            }
        ),
    )
    shape = normalize_query_shape(query)
    nested = shape["where"]["exists_subquery"]
    assert shape["where"]["operator"] == "exists"
    assert nested["from"] == "orders"
    assert "SECRET-LITERAL" not in str(shape)


def test_the_audit_shape_records_the_correlate_list():
    """The exact set of outer columns a nested scope was permitted to see is the
    most security-relevant fact about a correlated query — column identifiers only,
    never values, so non-negotiable 3 is untouched."""
    shape = normalize_query_shape(_customers_with_orders())
    assert shape["where"]["exists_subquery"]["correlate"] == ["customers.id"]


def test_referenced_tables_tree_wide_sees_the_exists_subquerys_tables():
    assert referenced_tables_tree_wide(_customers_with_orders()) == {"customers", "orders"}


def test_the_catalog_usage_signals_learn_the_exists_subquerys_tables():
    targets = StructuredQueryService(_CONN)._usage_signal_targets(_customers_with_orders())
    assert {target.table for _kind, target in targets} == {"customers", "orders"}


def test_the_sensitivity_approval_gate_sees_a_labelled_column_inside_an_exists(monkeypatch):
    """A labelled column reachable only from inside an EXISTS must still trigger
    the item-92 gate. Reading the outer scope alone would let a caller test a
    sensitive column with the approval gate never firing — the hole that was live
    for `value_subquery` from item 97 until item 104 closed it."""
    from querygate.execution import approval as approval_module

    class _Column:
        sensitivity = SensitivityClass.PII

    class _Entry:
        sensitivity = SensitivityClass.NONE

        def column(self, name):
            return _Column() if name == "status" else None

    class _Store:
        def get_table(self, connection_id, table):
            return _Entry() if table == "orders" else None

    monkeypatch.setattr(approval_module, "get_catalog_store", lambda: _Store())
    query = _q(
        **{"from": "customers", "select": ["customers.name"]},
        where=_correlated_exists(
            where={
                "and": [
                    {"col": "orders.customer_id", "op": "eq", "value_col": "customers.id"},
                    {"col": "orders.status", "op": "eq", "value": "x"},
                ]
            }
        ),
    )
    reasons = approval_module.sensitivity_approval_reasons(
        query, Policy(approval_sensitivities=[SensitivityClass.PII]), _CONN
    )
    assert reasons and "orders.status" in reasons[0]


# --------------------------------------------------------------------------- #
# Composition with the other scope containers                                  #
# --------------------------------------------------------------------------- #


def test_an_exists_may_live_inside_a_cte_body():
    """Item 105's container holding item 106's. The block is a scope, and the
    EXISTS inside it is a scope of that — so the correlated ref resolves against
    the BLOCK, not the top-level query."""
    query = _q(
        ctes=[
            {
                "name": "block",
                "query": {
                    "from": "customers",
                    "select": ["customers.id"],
                    "where": _correlated_exists(),
                },
            }
        ],
        **{"from": "block", "select": ["block.id"]},
    )
    validate_policy(query, Policy(max_subquery_depth=3), _CONN)
    correlations = list(iter_correlations(query))
    assert len(correlations) == 1
    assert correlations[0].parent is query.ctes[0].query


def test_an_exists_may_live_inside_a_set_operation_arm():
    """Item 104's container holding item 106's. An arm is a scope at the same
    depth as its carrier, so the correlated ref resolves against the ARM."""
    arm = {"from": "customers", "select": ["customers.name"], "where": _correlated_exists()}
    query = _q(
        **{"from": "customers", "select": ["customers.name"]},
        set_op={"op": "union", "arms": [arm]},
    )
    validate_policy(query, _DEEP, _CONN)
    correlations = list(iter_correlations(query))
    assert len(correlations) == 1
    assert correlations[0].parent is query.set_op.arms[0]


def test_correlated_refs_from_every_container_share_one_budget():
    """Tree-wide, like every other count cap — a second container must not be a
    second budget, which is the item-97 loophole restated for correlation."""
    arm = {"from": "customers", "select": ["customers.name"], "where": _correlated_exists()}
    query = _q(
        **{"from": "customers", "select": ["customers.name"]},
        where=_correlated_exists(),
        set_op={"op": "union", "arms": [arm]},
    )
    validate_policy(query, Policy(max_subquery_depth=2, max_correlated_refs=2), _CONN)
    with pytest.raises(PolicyViolationError, match="correlated references exceed max"):
        validate_policy(query, Policy(max_subquery_depth=2, max_correlated_refs=1), _CONN)
