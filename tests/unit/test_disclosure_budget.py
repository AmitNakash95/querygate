"""Cumulative disclosure budget (TODO.md item 179).

Covers the shape fingerprint's defining property (a differencing probe is ONE
shape re-sent, not N shapes), which scopes/tables get charged, the policy
resolution coupling to `min_group_size`, the in-process limiter's window and
all-or-nothing semantics, and the enforce helper's skip conditions and key
isolation. The `execute()`-path enforcement — including the full differencing
scenario against a real database — is proven end-to-end in
`tests/integration/test_disclosure_budget_e2e.py`.
"""

from __future__ import annotations

import pydantic as pyd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette import status

from querygate.api._errors import install_exception_handlers
from querygate.core.exceptions import (
    DisclosureBudgetExceededError,
    PolicyViolationError,
    QuotaExceededError,
)
from querygate.execution.disclosure_budget import (
    KIND_SHAPE,
    KIND_TABLE,
    InProcessDisclosureBudgetLimiter,
    budgeted_occurrences,
    enforce_disclosure_budget,
    resolve_disclosure_budget,
    shape_fingerprint,
)
from querygate.mcp.exceptions import _error_code_from_exception
from querygate.metrics import classify_rejection
from querygate.policy.models import Policy
from querygate.query_ast.models import StructuredQuery

pytestmark = pytest.mark.unit


def _agg(where_value: int = 40, **overrides) -> StructuredQuery:
    """An aggregate query on `employees`, filtered by a single constant."""
    body = {
        "from": "employees",
        "select": [{"fn": "count", "col": "employees.id", "as": "n"}],
        "group_by": ["employees.department"],
        "where": {"col": "employees.age", "op": "gt", "value": where_value},
    }
    body.update(overrides)
    return StructuredQuery.model_validate(body)


def _tables(query) -> set:
    """The distinct physical tables `budgeted_occurrences` charges."""
    return {table for table, _fingerprint in budgeted_occurrences(query)}


def _budget_policy(**overrides) -> Policy:
    base = {"min_group_size": 5, "max_shape_repeats_per_window": 3}
    base.update(overrides)
    return Policy(**base)


# ---------------------------------------------------------------------------
# shape_fingerprint — the property the whole design rests on
# ---------------------------------------------------------------------------


def test_same_shape_with_different_literals_is_one_fingerprint():
    """THE defining property (TODO.md item 179). A differencing probe walks a
    constant — `age > 40`, `> 41`, `> 42` — and every one of those is the same
    normalized shape. If this ever became false, the per-shape cap would count
    each probe in its own bucket and never trip, i.e. the guardrail would be
    silently inert against the exact attack it exists for.
    """
    fingerprints = {shape_fingerprint(_agg(value)) for value in range(40, 60)}
    assert len(fingerprints) == 1


def test_genuinely_different_shapes_differ():
    assert shape_fingerprint(_agg()) != shape_fingerprint(_agg(group_by=["employees.title"]))
    assert shape_fingerprint(_agg()) != shape_fingerprint(
        _agg(where={"col": "employees.age", "op": "lt", "value": 40})
    )
    assert shape_fingerprint(_agg()) != shape_fingerprint(
        _agg(**{"from": "orders", "group_by": ["orders.status"], "where": None})
    )


def test_walking_limit_or_offset_cannot_mint_a_fresh_shape_bucket():
    """`normalize_query_shape` legitimately retains `requested_limit`/`offset`,
    so without stripping them a prober could evade the per-shape cap entirely by
    incrementing `limit` on each probe."""
    baseline = shape_fingerprint(_agg())
    assert shape_fingerprint(_agg(limit=10)) == baseline
    assert shape_fingerprint(_agg(limit=999)) == baseline
    assert shape_fingerprint(_agg(offset=7)) == baseline


def test_identifier_case_cannot_mint_a_fresh_shape_bucket():
    """`normalize_query_shape` keeps the caller's own casing, but the database
    resolves `Employees.Id` and `employees.id` to the same column and every
    other identifier comparison in the policy layer is case-insensitive (items
    149/150). Without folding, alternating case would hand a prober unlimited
    fresh buckets."""
    upper = StructuredQuery.model_validate(
        {
            "from": "Employees",
            "select": [{"fn": "count", "col": "Employees.Id", "as": "n"}],
            "group_by": ["Employees.Department"],
            "where": {"col": "Employees.Age", "op": "gt", "value": 40},
        }
    )
    assert shape_fingerprint(_agg()) == shape_fingerprint(upper)


def test_renaming_an_alias_cannot_mint_a_fresh_shape_bucket():
    """Column references carry the *effective* name, so an alias rename would
    otherwise change the shape without changing the query."""
    fingerprints = set()
    for alias in ("e", "x", "q1"):
        fingerprints.add(
            shape_fingerprint(
                StructuredQuery.model_validate(
                    {
                        "from": "employees",
                        "from_alias": alias,
                        "select": [{"fn": "count", "col": f"{alias}.id", "as": "n"}],
                        "group_by": [f"{alias}.department"],
                        "where": {"col": f"{alias}.age", "op": "gt", "value": 40},
                    }
                )
            )
        )
    assert len(fingerprints) == 1
    # ...and it is the same bucket as the un-aliased spelling of that query.
    assert fingerprints == {shape_fingerprint(_agg())}


def test_fingerprint_is_stable_across_calls():
    assert shape_fingerprint(_agg()) == shape_fingerprint(_agg())


# ---------------------------------------------------------------------------
# budgeted_tables — which scopes and tables get charged
# ---------------------------------------------------------------------------


def test_non_aggregate_query_is_never_budgeted():
    plain = StructuredQuery.model_validate(
        {"from": "employees", "select": ["employees.id"], "limit": 5}
    )
    assert budgeted_occurrences(plain) == {}


def test_aggregate_query_charges_its_table():
    assert _tables(_agg()) == {"employees"}


def test_join_charges_both_tables():
    query = StructuredQuery.model_validate(
        {
            "from": "orders",
            "joins": [
                {
                    "table": "customers",
                    "type": "inner",
                    "on": ["orders.customer_id", "customers.id"],
                }
            ],
            "select": [{"fn": "count", "col": "orders.id", "as": "n"}],
            "group_by": ["customers.name"],
        }
    )
    assert _tables(query) == {"orders", "customers"}


def test_a_cte_name_is_not_charged_but_its_underlying_table_is():
    """A cte is not a physical table. The tables behind it are charged when the
    cte body's own scope is walked, so the budget can't be dodged by wrapping an
    aggregate in a cte and selecting from the cte name."""
    query = StructuredQuery.model_validate(
        {
            "ctes": [
                {
                    "name": "per_dept",
                    "query": {
                        "from": "employees",
                        "select": [{"fn": "count", "col": "employees.id", "as": "n"}],
                        "group_by": ["employees.department"],
                    },
                }
            ],
            "from": "per_dept",
            "select": ["per_dept.n"],
        }
    )
    charged = _tables(query)
    assert "per_dept" not in charged
    assert "employees" in charged


def test_an_aggregate_over_a_cte_name_still_never_charges_the_cte():
    """The case that actually exercises the cte exclusion. Above, the outer
    scope doesn't aggregate, so it is skipped before the cte name is ever
    considered — this one aggregates *over* the cte, so `per_dept` reaches the
    table loop and must still be excluded as a non-physical name.
    """
    query = StructuredQuery.model_validate(
        {
            "ctes": [
                {
                    "name": "per_dept",
                    "query": {
                        "from": "employees",
                        "select": [
                            "employees.department",
                            {"fn": "count", "col": "employees.id", "as": "n"},
                        ],
                        "group_by": ["employees.department"],
                    },
                }
            ],
            "from": "per_dept",
            "select": [{"fn": "sum", "col": "per_dept.n", "as": "total"}],
            "group_by": ["per_dept.department"],
        }
    )
    charged = _tables(query)
    assert "per_dept" not in charged
    assert charged == {"employees"}


def test_a_non_aggregating_cte_wrapper_still_charges_the_base_table():
    """A complete bypass if this is wrong, and it silently was.

    Wrap the target in a cte that does NOT aggregate, then aggregate over the
    cte in the outer scope. The cte body contributes nothing (it isn't an
    aggregate) and the outer scope's only table is the cte name — so an earlier
    draft charged the query *nothing at all*, while the compiler still applied
    the `min_group_size` floor to the outer scope and returned a real answer.
    The caller could then difference forever against a fully-budgeted table.
    """
    query = StructuredQuery.model_validate(
        {
            "ctes": [
                {
                    "name": "src",
                    "query": {
                        "from": "employees",
                        "select": ["employees.department", "employees.salary"],
                        "where": {"col": "employees.salary", "op": "gt", "value": 50000},
                    },
                }
            ],
            "from": "src",
            "select": [{"fn": "count", "col": "src.salary", "as": "n"}],
            "group_by": ["src.department"],
        }
    )
    assert _tables(query) == {"employees"}


def test_a_cte_chain_resolves_transitively_to_its_base_table():
    """A cte reading an earlier cte must still resolve to the physical table,
    or the wrapper bypass just needs one more layer of indirection."""
    query = StructuredQuery.model_validate(
        {
            "ctes": [
                {
                    "name": "a",
                    "query": {
                        "from": "employees",
                        "select": ["employees.department", "employees.salary"],
                    },
                },
                {
                    "name": "b",
                    "query": {"from": "a", "select": ["a.department", "a.salary"]},
                },
            ],
            "from": "b",
            "select": [{"fn": "count", "col": "b.salary", "as": "n"}],
            "group_by": ["b.department"],
        }
    )
    assert _tables(query) == {"employees"}


def test_each_aggregating_scope_costs_its_own_unit():
    """One statement must not buy several probe answers. A UNION of three
    aggregates over one table with three different constants returns three
    answers, so it must cost three units — otherwise `max_set_op_arms` silently
    multiplies whatever bound the operator configured."""
    arms = [
        {
            "from": "employees",
            "select": [{"fn": "count", "col": "employees.id", "as": "n"}],
            "group_by": ["employees.department"],
            "where": {"col": "employees.salary", "op": "gt", "value": value},
        }
        for value in (60000, 70000)
    ]
    query = StructuredQuery.model_validate(
        {
            "from": "employees",
            "select": [{"fn": "count", "col": "employees.id", "as": "n"}],
            "group_by": ["employees.department"],
            "where": {"col": "employees.salary", "op": "gt", "value": 50000},
            "set_op": {"op": "union", "all": True, "arms": arms},
        }
    )
    occurrences = budgeted_occurrences(query)
    # Three aggregating scopes over `employees` -> three units, which is what
    # the per-table cap sees. (They are not all one *shape* key: the outer
    # scope's shape includes the set_op itself, while the two arms share a
    # fingerprint with each other — the literal that distinguishes them is
    # stripped. That split is irrelevant to the cost, which is the point here.)
    assert sum(occurrences.values()) == 3
    assert {table for table, _fp in occurrences} == {"employees"}


def test_a_set_operation_arm_is_charged_on_its_own_terms():
    query = StructuredQuery.model_validate(
        {
            "from": "employees",
            "select": [{"fn": "count", "col": "employees.id", "as": "n"}],
            "group_by": ["employees.department"],
            "set_op": {
                "op": "union",
                "arms": [
                    {
                        "from": "customers",
                        "select": [{"fn": "count", "col": "customers.id", "as": "n"}],
                        "group_by": ["customers.name"],
                    }
                ],
            },
        }
    )
    assert _tables(query) == {"employees", "customers"}


def test_one_query_charges_a_repeated_table_only_once():
    query = StructuredQuery.model_validate(
        {
            "from": "employees",
            "from_alias": "emp",
            "joins": [
                {
                    "table": "employees",
                    "alias": "mgr",
                    "type": "inner",
                    "on": ["emp.manager_id", "mgr.id"],
                }
            ],
            "select": [{"fn": "count", "col": "emp.id", "as": "n"}],
            "group_by": ["emp.department"],
        }
    )
    assert _tables(query) == {"employees"}


# ---------------------------------------------------------------------------
# resolve_disclosure_budget — the min_group_size coupling
# ---------------------------------------------------------------------------


def test_budget_disabled_by_default():
    assert Policy().disclosure_budget_enabled is False
    assert resolve_disclosure_budget(Policy()) is None


def test_budget_enabled_by_either_cap():
    assert (
        Policy(min_group_size=5, max_shape_repeats_per_window=3).disclosure_budget_enabled is True
    )
    assert (
        Policy(min_group_size=5, max_aggregate_queries_per_window=3).disclosure_budget_enabled
        is True
    )


def test_a_budget_without_a_k_anonymity_floor_is_rejected_at_load_time():
    """Deliberate coupling: with no `min_group_size` there is no aggregate-only
    disclosure to difference around — the caller could read the rows directly —
    so the budget would protect nothing.

    It fails loudly rather than silently: a config that loads clean, shows the
    caps in the admin guardrails view, and enforces nothing is the worst version
    of this coupling.
    """
    with pytest.raises(pyd.ValidationError, match="min_group_size"):
        Policy(max_shape_repeats_per_window=3, min_group_size=None)
    with pytest.raises(pyd.ValidationError, match="min_group_size"):
        Policy(max_aggregate_queries_per_window=3, min_group_size=None)


def test_resolve_still_defends_against_a_floorless_budget_in_process():
    """Defence in depth for a `Policy` built in-process rather than loaded."""
    policy = Policy(min_group_size=5, max_shape_repeats_per_window=3)
    object.__setattr__(policy, "min_group_size", None)
    assert policy.disclosure_budget_enabled is True
    assert resolve_disclosure_budget(policy) is None


def test_resolve_returns_both_caps_and_the_window():
    policy = _budget_policy(
        max_aggregate_queries_per_window=9, disclosure_budget_window_seconds=1800
    )
    assert resolve_disclosure_budget(policy) == (3, 9, 1800)


def test_caps_and_window_have_positive_lower_bounds():
    for field in (
        "max_shape_repeats_per_window",
        "max_aggregate_queries_per_window",
        "disclosure_budget_window_seconds",
    ):
        with pytest.raises(pyd.ValidationError) as excinfo:
            Policy(min_group_size=5, **{field: 0})
        # Assert on the offending field, not just "some ValidationError" — a
        # bare `pytest.raises(ValueError)` here would also pass for a typo'd
        # field name under `extra="forbid"`.
        assert excinfo.value.errors()[0]["loc"] == (field,)
        assert "greater than or equal to 1" in excinfo.value.errors()[0]["msg"]


# ---------------------------------------------------------------------------
# InProcessDisclosureBudgetLimiter
# ---------------------------------------------------------------------------


KEY_A = ("demo", "agent", "", "employees", "shape-a")
KEY_B = ("demo", "agent", "", "employees", "")


@pytest.mark.asyncio
async def test_admits_up_to_the_cap_then_rejects():
    lim = InProcessDisclosureBudgetLimiter()
    for _ in range(2):
        await lim.reserve([(KEY_A, 2, KIND_SHAPE, 1)], window_seconds=600, now=100.0)
    with pytest.raises(DisclosureBudgetExceededError) as excinfo:
        await lim.reserve([(KEY_A, 2, KIND_SHAPE, 1)], window_seconds=600, now=100.0)
    assert excinfo.value.quota_kind == KIND_SHAPE
    assert excinfo.value.retry_after_seconds == 600


@pytest.mark.asyncio
async def test_window_rolls_forward():
    lim = InProcessDisclosureBudgetLimiter()
    await lim.reserve([(KEY_A, 1, KIND_SHAPE, 1)], window_seconds=600, now=100.0)
    with pytest.raises(DisclosureBudgetExceededError):
        await lim.reserve([(KEY_A, 1, KIND_SHAPE, 1)], window_seconds=600, now=200.0)
    # Once the first charge ages out of the window, capacity returns.
    await lim.reserve([(KEY_A, 1, KIND_SHAPE, 1)], window_seconds=600, now=701.0)


@pytest.mark.asyncio
async def test_distinct_keys_have_independent_budgets():
    lim = InProcessDisclosureBudgetLimiter()
    await lim.reserve([(KEY_A, 1, KIND_SHAPE, 1)], window_seconds=600, now=100.0)
    await lim.reserve([(KEY_B, 1, KIND_TABLE, 1)], window_seconds=600, now=100.0)


@pytest.mark.asyncio
async def test_a_refused_charge_spends_nothing_on_its_other_keys():
    """All-or-nothing. The per-table key here is already exhausted, so the
    per-shape key charged alongside it must be left untouched — otherwise a
    caller repeatedly bouncing off one cap would silently burn down the other.
    """
    lim = InProcessDisclosureBudgetLimiter()
    await lim.reserve([(KEY_B, 1, KIND_TABLE, 1)], window_seconds=600, now=100.0)
    for _ in range(5):
        with pytest.raises(DisclosureBudgetExceededError):
            await lim.reserve(
                [(KEY_A, 3, KIND_SHAPE, 1), (KEY_B, 1, KIND_TABLE, 1)],
                window_seconds=600,
                now=100.0,
            )
    # KEY_A never charged, so its full budget of 3 is still available.
    for _ in range(3):
        await lim.reserve([(KEY_A, 3, KIND_SHAPE, 1)], window_seconds=600, now=100.0)


@pytest.mark.asyncio
async def test_the_tripped_key_determines_the_reported_kind():
    """The rejection must report the kind of the cap that ACTUALLY tripped, not
    whichever charge happened to be listed first. Reporting the wrong kind would
    send an operator tuning a threshold that was never the constraint, and would
    mislabel the metrics counter.
    """
    lim = InProcessDisclosureBudgetLimiter()
    await lim.reserve([(KEY_B, 1, KIND_TABLE, 1)], window_seconds=600, now=100.0)
    with pytest.raises(DisclosureBudgetExceededError) as excinfo:
        await lim.reserve(
            [(KEY_A, 5, KIND_SHAPE, 1), (KEY_B, 1, KIND_TABLE, 1)],
            window_seconds=600,
            now=100.0,
        )
    assert excinfo.value.quota_kind == KIND_TABLE
    assert "aggregate queries" in str(excinfo.value)


@pytest.mark.asyncio
async def test_retry_after_counts_down_as_the_window_ages():
    """Pinned away from the degenerate zero-age point. Charging and rejecting at
    the same instant makes retry_after == window_seconds, which several wrong
    implementations also produce (dropping the elapsed term, reading the newest
    entry instead of the oldest, flipping the sign). Telling a caller to retry in
    an hour when capacity returns in five minutes is a real availability bug."""
    lim = InProcessDisclosureBudgetLimiter()
    await lim.reserve([(KEY_A, 1, KIND_SHAPE, 1)], window_seconds=600, now=100.0)
    with pytest.raises(DisclosureBudgetExceededError) as excinfo:
        await lim.reserve([(KEY_A, 1, KIND_SHAPE, 1)], window_seconds=600, now=400.0)
    assert excinfo.value.retry_after_seconds == 300


@pytest.mark.asyncio
async def test_an_entry_exactly_at_the_window_edge_has_aged_out():
    """Boundary case for the `>` in `_prune`: an entry exactly `window_seconds`
    old is outside the window, not inside it."""
    lim = InProcessDisclosureBudgetLimiter()
    await lim.reserve([(KEY_A, 1, KIND_SHAPE, 1)], window_seconds=600, now=100.0)
    await lim.reserve([(KEY_A, 1, KIND_SHAPE, 1)], window_seconds=600, now=700.0)


@pytest.mark.asyncio
async def test_a_weighted_charge_is_refused_when_it_would_exceed_the_cap():
    """Weight is what stops one statement buying several probe answers, so a
    charge that does not *fit* must be refused rather than admitted and then
    overshooting the cap."""
    lim = InProcessDisclosureBudgetLimiter()
    await lim.reserve([(KEY_A, 3, KIND_SHAPE, 2)], window_seconds=600, now=100.0)
    with pytest.raises(DisclosureBudgetExceededError):
        await lim.reserve([(KEY_A, 3, KIND_SHAPE, 2)], window_seconds=600, now=100.0)
    # ...but a charge that still fits exactly is admitted.
    await lim.reserve([(KEY_A, 3, KIND_SHAPE, 1)], window_seconds=600, now=100.0)


@pytest.mark.asyncio
async def test_idle_keys_are_swept_so_the_window_map_stays_bounded():
    """`_prune` only touches keys in the current charge, so without a sweep a
    key charged once and never charged again would live for the process's
    lifetime — and the key carries a shape fingerprint, so its cardinality grows
    with the number of distinct queries a caller has ever run."""
    lim = InProcessDisclosureBudgetLimiter()
    for index in range(500):
        key = ("demo", "agent", "", "employees", f"shape-{index}")
        await lim.reserve([(key, 5, KIND_SHAPE, 1)], window_seconds=600, now=100.0)
    assert len(lim._windows) == 500
    # Two windows later, one unrelated charge reclaims every aged-out key.
    await lim.reserve([(KEY_A, 5, KIND_SHAPE, 1)], window_seconds=600, now=2000.0)
    assert len(lim._windows) == 1


@pytest.mark.asyncio
async def test_clear_resets_all_windows():
    lim = InProcessDisclosureBudgetLimiter()
    await lim.reserve([(KEY_A, 1, KIND_SHAPE, 1)], window_seconds=600, now=100.0)
    lim.clear()
    await lim.reserve([(KEY_A, 1, KIND_SHAPE, 1)], window_seconds=600, now=100.0)


@pytest.mark.asyncio
async def test_clear_resets_the_sweep_clock_too():
    """`clear()` promises to reset *all* state, and `reserve()` takes an explicit
    `now`. If the sweep clock survived, a test sweeping at a large `now` would
    leave it ahead of every later test's smaller `now`, so
    `now - _last_sweep > window_seconds` could never hold and the sweep would
    silently stop running — order-dependent, and for a reason nobody would look
    for. Asserted on the observable consequence (the sweep reclaims aged-out
    keys) rather than on `_last_sweep` alone, so it fails if either the reset or
    the sweep itself regresses."""
    lim = InProcessDisclosureBudgetLimiter()
    await lim.reserve([(KEY_A, 5, KIND_SHAPE, 1)], window_seconds=600, now=100_000.0)
    lim.clear()
    for index in range(3):
        key = ("demo", "agent", "", "employees", f"shape-{index}")
        await lim.reserve([(key, 5, KIND_SHAPE, 1)], window_seconds=600, now=10.0)
    assert len(lim._windows) == 3
    # Two windows on from `now=10.0`, one unrelated charge must reclaim all three.
    # With `_last_sweep` left at 100_000.0 the sweep never fires and this is 4.
    await lim.reserve([(KEY_A, 5, KIND_SHAPE, 1)], window_seconds=600, now=2_000.0)
    assert len(lim._windows) == 1


# ---------------------------------------------------------------------------
# enforce_disclosure_budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enforce_is_a_noop_without_a_principal():
    """An unattributable caller can't be budgeted per principal, so the guard is
    skipped rather than applied to a shared anonymous bucket (same posture as
    `enforce_query_quota`)."""
    policy = _budget_policy(max_shape_repeats_per_window=1)
    for _ in range(5):
        await enforce_disclosure_budget(
            _agg(), policy, connection_id="demo", principal_subject=None
        )


@pytest.mark.asyncio
async def test_enforce_is_a_noop_when_the_budget_is_disabled():
    policy = Policy(min_group_size=5)
    for _ in range(5):
        await enforce_disclosure_budget(
            _agg(), policy, connection_id="demo", principal_subject="agent"
        )


@pytest.mark.asyncio
async def test_enforce_is_a_noop_for_a_non_aggregate_query():
    plain = StructuredQuery.model_validate(
        {"from": "employees", "select": ["employees.id"], "limit": 5}
    )
    policy = _budget_policy(max_shape_repeats_per_window=1)
    for _ in range(5):
        await enforce_disclosure_budget(
            plain, policy, connection_id="demo", principal_subject="agent"
        )


@pytest.mark.asyncio
async def test_enforce_rejects_a_repeated_shape_regardless_of_the_literal():
    policy = _budget_policy(max_shape_repeats_per_window=3)
    for value in (40, 41, 42):
        await enforce_disclosure_budget(
            _agg(value), policy, connection_id="demo", principal_subject="agent"
        )
    with pytest.raises(DisclosureBudgetExceededError) as excinfo:
        await enforce_disclosure_budget(
            _agg(43), policy, connection_id="demo", principal_subject="agent"
        )
    assert excinfo.value.quota_kind == KIND_SHAPE


@pytest.mark.asyncio
async def test_the_per_table_cap_catches_a_shape_varying_prober():
    """Only the blunt cap is set here, so each probe is a *different* shape and
    the per-shape cap would never fire — this is what stops shape variation
    being a free bypass."""
    policy = Policy(
        min_group_size=5, max_aggregate_queries_per_window=3, max_shape_repeats_per_window=None
    )
    for column in ("employees.department", "employees.title", "employees.region"):
        await enforce_disclosure_budget(
            _agg(group_by=[column]), policy, connection_id="demo", principal_subject="agent"
        )
    with pytest.raises(DisclosureBudgetExceededError) as excinfo:
        await enforce_disclosure_budget(
            _agg(group_by=["employees.office"]),
            policy,
            connection_id="demo",
            principal_subject="agent",
        )
    assert excinfo.value.quota_kind == KIND_TABLE


@pytest.mark.asyncio
async def test_budgets_are_isolated_per_principal_connection_purpose_and_table():
    policy = _budget_policy(max_shape_repeats_per_window=1, allowed_purposes=["fraud_review"])
    await enforce_disclosure_budget(
        _agg(), policy, connection_id="demo", principal_subject="agent-1"
    )
    # Same query, but each of these differs in exactly one key component and so
    # carries its own independent budget.
    await enforce_disclosure_budget(
        _agg(), policy, connection_id="demo", principal_subject="agent-2"
    )
    await enforce_disclosure_budget(
        _agg(), policy, connection_id="other", principal_subject="agent-1"
    )
    await enforce_disclosure_budget(
        _agg(purpose="fraud_review"), policy, connection_id="demo", principal_subject="agent-1"
    )
    await enforce_disclosure_budget(
        _agg(**{"from": "orders", "group_by": ["orders.status"], "where": None}),
        policy,
        connection_id="demo",
        principal_subject="agent-1",
    )
    # ...and agent-1's original bucket is still the one that's exhausted.
    with pytest.raises(DisclosureBudgetExceededError):
        await enforce_disclosure_budget(
            _agg(), policy, connection_id="demo", principal_subject="agent-1"
        )


@pytest.mark.asyncio
async def test_each_declared_purpose_carries_its_own_budget():
    """What makes a purpose (item 145) bound *cumulative* disclosure rather than
    only narrowing one query at a time.

    Note the `allowed_purposes` allow-list: a purpose only partitions the budget
    when the operator declared the closed set — see the sibling test below for
    why that condition is load-bearing rather than incidental.
    """
    policy = _budget_policy(
        max_shape_repeats_per_window=1, allowed_purposes=["fraud_review", "billing"]
    )
    await enforce_disclosure_budget(
        _agg(purpose="fraud_review"), policy, connection_id="demo", principal_subject="a"
    )
    await enforce_disclosure_budget(
        _agg(purpose="billing"), policy, connection_id="demo", principal_subject="a"
    )
    with pytest.raises(DisclosureBudgetExceededError):
        await enforce_disclosure_budget(
            _agg(purpose="fraud_review"), policy, connection_id="demo", principal_subject="a"
        )


@pytest.mark.asyncio
async def test_an_undeclared_purpose_cannot_mint_a_fresh_budget():
    """A total bypass if this is wrong, and it silently was.

    With `allowed_purposes` empty (the default), `resolve_purpose_policy`
    accepts ANY purpose string a caller invents — item 145's "empty allow-list =
    unrestricted" convention. If the budget keyed on that free text, a prober
    would defeat both caps completely by sending `purpose="p1"`, `"p2"`, `"p3"`,
    each landing in a fresh bucket, while the audit event (which applies this
    same allow-list rule before persisting a purpose) recorded nothing to make
    the evasion visible afterwards.
    """
    policy = _budget_policy(max_shape_repeats_per_window=1)
    assert policy.allowed_purposes == []
    await enforce_disclosure_budget(
        _agg(purpose="p1"), policy, connection_id="demo", principal_subject="a"
    )
    for invented in ("p2", "p3", "p4"):
        with pytest.raises(DisclosureBudgetExceededError):
            await enforce_disclosure_budget(
                _agg(purpose=invented), policy, connection_id="demo", principal_subject="a"
            )


# ---------------------------------------------------------------------------
# Error contract
# ---------------------------------------------------------------------------


def test_error_inherits_the_quota_caller_contract():
    exc = DisclosureBudgetExceededError("x", quota_kind=KIND_SHAPE, retry_after_seconds=7)
    assert isinstance(exc, QuotaExceededError)
    assert isinstance(exc, PolicyViolationError)
    assert isinstance(exc, ValueError)
    assert exc.retry_after_seconds == 7
    assert classify_rejection(exc) == "quota"


def test_mcp_maps_a_disclosure_rejection_to_rate_limited():
    """Pinned behaviorally, not by `isinstance`: the caller-visible contract is
    deliberately identical to any other budget rejection, so an attacker can't
    tell which guardrail they tripped. An inheritance-only assertion wouldn't
    fail if the MCP mapper started branching on the concrete type."""
    exc = DisclosureBudgetExceededError("slow down", quota_kind=KIND_SHAPE, retry_after_seconds=7)
    code, message = _error_code_from_exception(exc)
    assert code == "RATE_LIMITED"
    assert message == "slow down"


def test_rest_maps_a_disclosure_rejection_to_429_with_retry_after():
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/boom")
    async def _boom():
        raise DisclosureBudgetExceededError(
            "slow down", quota_kind=KIND_TABLE, retry_after_seconds=42
        )

    client = TestClient(app)
    resp = client.get("/boom")
    assert resp.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    assert resp.headers["Retry-After"] == "42"
    assert resp.json() == {"detail": "slow down"}


def test_rejection_message_names_neither_the_table_nor_the_shape():
    """Which table is close to its disclosure budget is itself a disclosure
    channel, and echoing the shape back would confirm to a prober exactly which
    variants the server considers identical."""
    lim = InProcessDisclosureBudgetLimiter()
    key = ("demo", "agent", "fraud_review", "salaries", "deadbeef")

    async def _run():
        await lim.reserve([(key, 1, KIND_SHAPE, 1)], window_seconds=600, now=100.0)
        await lim.reserve([(key, 1, KIND_SHAPE, 1)], window_seconds=600, now=100.0)

    with pytest.raises(DisclosureBudgetExceededError) as excinfo:
        import asyncio

        asyncio.run(_run())
    message = str(excinfo.value)
    assert "salaries" not in message
    assert "deadbeef" not in message
    assert "fraud_review" not in message


@pytest.mark.asyncio
async def test_the_configured_window_reaches_the_limiter(monkeypatch):
    """`disclosure_budget_window_seconds` is deliberately separate from
    `quota_window_seconds`, so hard-coding either one (or the 3600 default) at
    the call site would silently ignore the operator's configuration while every
    other test — which all use the default — stayed green."""
    import querygate.execution.disclosure_budget as module

    seen = {}

    class _Recorder:
        async def reserve(self, charges, *, window_seconds, now=None):
            seen["window_seconds"] = window_seconds
            seen["charges"] = list(charges)

    monkeypatch.setattr(module, "_active_limiter", _Recorder())
    policy = _budget_policy(max_shape_repeats_per_window=5, disclosure_budget_window_seconds=1800)
    await enforce_disclosure_budget(_agg(), policy, connection_id="demo", principal_subject="agent")
    assert seen["window_seconds"] == 1800
    # ...and the cap the operator configured is the one charged.
    assert [limit for _key, limit, _kind, _weight in seen["charges"]] == [5]


@pytest.mark.asyncio
async def test_both_caps_produce_two_independent_charges(monkeypatch):
    """With both caps set, each must produce its own charge — an `if/elif` at the
    call site would silently drop one of them."""
    import querygate.execution.disclosure_budget as module

    seen = {}

    class _Recorder:
        async def reserve(self, charges, *, window_seconds, now=None):
            seen["charges"] = list(charges)

    monkeypatch.setattr(module, "_active_limiter", _Recorder())
    policy = _budget_policy(max_shape_repeats_per_window=2, max_aggregate_queries_per_window=9)
    await enforce_disclosure_budget(_agg(), policy, connection_id="demo", principal_subject="agent")
    kinds = {kind: limit for _key, limit, kind, _weight in seen["charges"]}
    assert kinds == {KIND_TABLE: 9, KIND_SHAPE: 2}
