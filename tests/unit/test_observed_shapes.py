"""Observed query shapes — the discovery half of TODO.md item 195.

Two properties carry the weight here and everything else supports them:

1. **A recorded shape holds no data.** Every literal-bearing position is a
   parameter placeholder and free text is dropped, so the store inherits the
   persisted-audit redaction guarantee (CLAUDE.md non-negotiable 3). This is
   pinned both directly and by reflecting over the AST models, so the day a new
   literal-bearing field is added and nobody updates `_LITERAL_KEYS`, the
   reflection test fails rather than a value silently reaching the store.
2. **A recorded shape is actually promotable.** A drafted template must bind
   and validate as a real `StructuredQuery` — a draft that looks right and
   cannot be invoked would be discovered by the operator at agent run time.
"""

from __future__ import annotations

import typing
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pydantic as pyd
import pytest
import sqlalchemy as sa

from querygate.admin.observed_shapes import (
    _DROPPED_KEYS,
    _LITERAL_KEYS,
    _STRUCTURAL_SCALAR_KEYS,
    _assert_no_literal_survived,
    InProcessObservedShapeStore,
    ObservedShape,
    SkeletonizationError,
    build_observed_shape_report,
    observed_shape_store,
    shape_hash,
    skeletonize,
)
from querygate.core.exceptions import PolicyViolationError
from querygate.execution import service as svc
from querygate.execution.service import StructuredQueryService
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.query_ast import models as ast
from querygate.query_ast.models import (
    CaseSelectItem,
    CaseWhen,
    ColumnExpr,
    DateAddExpr,
    ExpressionSelectItem,
    Predicate,
    StringAggSelectItem,
    StructuredQuery,
    WhereGroup,
)
from querygate.templates.binding import bind_template
from querygate.templates.models import QueryTemplate

pytestmark = pytest.mark.unit


def _query(**overrides) -> StructuredQuery:
    base = dict(
        from_table="orders",
        select=["orders.id"],
        where=Predicate(col="orders.status", op="eq", value="completed"),
        limit=5,
    )
    base.update(overrides)
    return StructuredQuery(**base)


def _literal_values(node) -> list:
    """Every concrete value still sitting in a literal-bearing position."""
    found: list = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _LITERAL_KEYS and not (isinstance(value, dict) and set(value) == {"param"}):
                found.append(value)
            else:
                found.extend(_literal_values(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_literal_values(item))
    return found


# ---------------------------------------------------------------------------
# The redaction guarantee
# ---------------------------------------------------------------------------


def test_every_bare_scalar_ast_field_is_classified():
    """`_LITERAL_KEYS` is the single place the redaction guarantee is
    maintained, and this is its lock.

    The first version of this test only flagged fields typed `Any` or a union
    of bare scalars — which meant a plain `str` field was invisible to it, and
    it duly failed to catch `StringAggSelectItem.delimiter`, an unbounded
    caller-supplied string that reached the store and the admin API
    (found by `security-invariant-reviewer`, 2026-08-17). The lock now fails on
    **any** bare-scalar field that has not been explicitly classified as a
    value, as structure, or as dropped — so a new AST field cannot be added
    without someone deciding which it is.
    """
    scalar_types = {bool, int, float, str}
    classified = _LITERAL_KEYS | _STRUCTURAL_SCALAR_KEYS | _DROPPED_KEYS
    unclassified: list[str] = []
    for name in dir(ast):
        model = getattr(ast, name)
        if not (isinstance(model, type) and issubclass(model, pyd.BaseModel)):
            continue
        for field_name, field in model.model_fields.items():
            if field_name in classified:
                continue
            annotation = field.annotation
            args = set(typing.get_args(annotation)) or {annotation}
            args.discard(type(None))
            # Unwrap List[str]/List[int] — a list of bare scalars is as much a
            # value (or as much an identifier list) as the scalar itself.
            unwrapped = set()
            for arg in args:
                inner = set(typing.get_args(arg))
                unwrapped |= (inner - {type(None)}) if inner else {arg}
            if annotation is typing.Any or any(a is typing.Any for a in unwrapped):
                unclassified.append(f"{model.__name__}.{field_name} (Any)")
            elif unwrapped and unwrapped <= scalar_types:
                kinds = sorted(a.__name__ for a in unwrapped)
                unclassified.append(f"{model.__name__}.{field_name} ({kinds})")
    assert not unclassified, (
        "unclassified bare-scalar AST field(s): "
        f"{sorted(set(unclassified))}. Decide whether each is a caller-supplied "
        "VALUE (add to observed_shapes._LITERAL_KEYS so it becomes a parameter "
        "slot), STRUCTURE (add to _STRUCTURAL_SCALAR_KEYS with a justification), "
        "or free text to drop (_DROPPED_KEYS)."
    )


def test_a_string_agg_delimiter_is_a_value_not_structure():
    """Regression for the 2026-08-17 review finding: `delimiter` is unbounded
    caller free text, and `normalize_query_shape` drops it entirely. A record
    that kept it would be strictly leakier than the audit event this store
    claims parity with.
    """
    query = _query(
        select=[StringAggSelectItem(col="orders.id", delimiter="SSN-123-45-6789", alias="ids")]
    )
    skeleton, parameters = skeletonize(query)
    assert "SSN-123-45-6789" not in str(skeleton)
    assert skeleton["select"][0]["delimiter"] == {"param": "delimiter"}
    assert "delimiter" in [p.name for p in parameters]


def test_caller_supplied_numbers_the_audit_event_drops_are_parameterized():
    """`DateAddExpr.amount` and the window frame/ntile numbers are values —
    `audit/events.py` omits each with an explicit "'shifted by some days' is
    shape, 'shifted by 90 days' is a value" comment. Same posture here.
    """
    query = _query(
        select=[
            ExpressionSelectItem(
                expr=DateAddExpr(
                    date_add=ColumnExpr(col="orders.created_at"), unit="day", amount=-90
                ),
                alias="shifted",
            )
        ]
    )
    skeleton, parameters = skeletonize(query)
    assert "-90" not in str(skeleton) and "90" not in str(skeleton["select"])
    assert "amount" in [p.name for p in parameters]


def test_limit_and_offset_are_parameterized_so_one_shape_stays_one_entry():
    """A number left in the skeleton is part of `shape_hash`, so the same
    logical query at limit 10 and limit 100 would occupy two entries —
    fragmenting the operator's view, and letting a caller walk a number to
    evict a bounded store (2026-08-17 review, finding 2).
    """
    first, _ = skeletonize(_query(limit=10))
    second, _ = skeletonize(_query(limit=100, offset=25))
    assert shape_hash(first) == shape_hash(second)


def test_predicate_value_becomes_a_parameter():
    skeleton, parameters = skeletonize(_query())
    assert skeleton["where"]["value"] == {"param": "status"}
    assert _literal_values(skeleton) == []
    # limit/offset are parameterized too (see _LITERAL_KEYS); assert on the
    # predicate slot specifically rather than on the whole list.
    by_name = {p.name: p for p in parameters}
    assert (by_name["status"].type, by_name["status"].is_list) == ("string", False)


def test_intent_is_dropped_entirely():
    """Natural-language intent is excluded from persisted audit events; a
    shape record must not become the back door that reintroduces it.
    """
    skeleton, _ = skeletonize(_query(intent="find everyone who owes us money"))
    assert "intent" not in skeleton
    assert "money" not in str(skeleton)


def test_literals_buried_in_an_expression_are_parameterized():
    """The walk is generic over the dumped AST rather than an enumeration of
    node types, so a literal nested inside a CASE branch is caught by the same
    rule as a top-level predicate.
    """
    query = _query(
        select=[
            CaseSelectItem(
                when=[
                    CaseWhen(
                        when=Predicate(col="orders.total_amount", op="gt", value=10_000),
                        then={"literal": "large"},
                    )
                ],
                else_={"literal": "small"},
                alias="bucket",
            )
        ]
    )
    skeleton, parameters = skeletonize(query)
    assert _literal_values(skeleton) == []
    assert "large" not in str(skeleton) and "small" not in str(skeleton)
    assert {p.type for p in parameters} == {"integer", "string"}


def test_literals_inside_a_subquery_are_parameterized():
    inner = StructuredQuery(
        from_table="order_items",
        select=["order_items.order_id"],
        where=Predicate(col="order_items.sku", op="eq", value="SECRET-SKU"),
    )
    skeleton, _ = skeletonize(
        _query(where=Predicate(col="orders.id", op="in", value_subquery=inner))
    )
    assert _literal_values(skeleton) == []
    assert "SECRET-SKU" not in str(skeleton)


def test_a_valueless_predicate_yields_no_parameter():
    """`is_null` carries no value. Emitting a required slot nobody can fill
    would make the drafted template permanently unbindable.
    """
    skeleton, parameters = skeletonize(
        _query(where=Predicate(col="orders.shipped_at", op="is_null"))
    )
    assert "value" not in skeleton["where"]
    assert not [p for p in parameters if p.name.startswith("shipped_at")]


def test_list_values_become_list_parameters():
    _, parameters = skeletonize(
        _query(where=Predicate(col="orders.status", op="in", value=["a", "b"]))
    )
    by_name = {p.name: p for p in parameters}
    assert (by_name["status"].type, by_name["status"].is_list) == ("string", True)


def test_colliding_parameter_names_are_disambiguated():
    query = _query(
        where=WhereGroup(
            and_terms=[
                Predicate(col="orders.status", op="eq", value="new"),
                Predicate(col="orders.status", op="neq", value="void"),
            ]
        )
    )
    _, parameters = skeletonize(query)
    assert [p.name for p in parameters if p.name.startswith("status")] == [
        "status",
        "status_2",
    ]


def test_the_guard_rejects_a_skeleton_that_still_holds_a_literal():
    """Direct test of the fail-closed half. `_walk` is *supposed* to have
    replaced every literal; this is what happens when it did not.
    """
    with pytest.raises(SkeletonizationError):
        _assert_no_literal_survived({"where": {"col": "orders.status", "value": "leaked"}})
    with pytest.raises(SkeletonizationError):
        _assert_no_literal_survived({"intent": "free text"})


def test_skeletonize_fails_closed_if_the_walk_misses_a_literal():
    """Defence in depth, and the reason the guard is a runtime check rather
    than only a test: if a future AST change makes `_walk` miss a position,
    `skeletonize` must refuse to hand back a skeleton carrying a value rather
    than storing it and trusting this file to have caught the regression.

    Simulated by neutralizing the walk — the failure mode the guard exists for.
    """
    import querygate.admin.observed_shapes as module

    with patch.object(module, "_walk", lambda node, allocator, **kw: node):
        with pytest.raises(SkeletonizationError):
            skeletonize(_query())


def test_a_value_shape_no_template_slot_can_express_is_refused():
    """Fail loudly rather than emit a draft that cannot bind.

    `Predicate.value` is typed `Any`, so a structured value reaches the
    skeletonizer even though no `TemplateParameter` type can express it. The
    only safe answer is to refuse to draft, not to guess a type — the recording
    hook swallows this so a live query is unaffected.
    """
    with pytest.raises(SkeletonizationError):
        skeletonize(_query(where=Predicate(col="orders.meta", op="eq", value={"nested": 1})))


# ---------------------------------------------------------------------------
# Shape identity and the store
# ---------------------------------------------------------------------------


def test_the_same_shape_with_different_values_is_one_entry():
    """The point of recording shapes rather than queries: a filter walked over
    a thousand values is one promotable template, not a thousand.
    """
    store = InProcessObservedShapeStore(enabled=True)
    for value in ("completed", "pending", "cancelled"):
        store.record(
            _query(where=Predicate(col="orders.status", op="eq", value=value)),
            connection_id="demo",
            principal_id="agent",
        )
    shapes = store.list_shapes()
    assert len(shapes) == 1
    assert shapes[0].occurrences == 3


def test_a_different_shape_is_a_different_entry():
    store = InProcessObservedShapeStore(enabled=True)
    store.record(_query(), connection_id="demo", principal_id="agent")
    store.record(
        _query(select=["orders.id", "orders.status"]), connection_id="demo", principal_id="agent"
    )
    assert len(store.list_shapes()) == 2


def test_one_shape_run_by_two_principals_stays_separable():
    """Promotion is a per-principal decision ("may THIS agent be narrowed to
    these templates?"), so the records cannot be merged across principals.
    """
    store = InProcessObservedShapeStore(enabled=True)
    store.record(_query(), connection_id="demo", principal_id="agent-a")
    store.record(_query(), connection_id="demo", principal_id="agent-b")
    assert len(store.list_shapes()) == 2
    assert len(store.list_shapes(principal_id="agent-a")) == 1


def test_shape_hash_is_stable_across_recordings():
    first, _ = skeletonize(_query())
    second, _ = skeletonize(_query(where=Predicate(col="orders.status", op="eq", value="other")))
    assert shape_hash(first) == shape_hash(second)


def test_a_shape_that_cannot_be_skeletonized_is_counted_not_silently_dropped():
    """The second way the discovery list can be incomplete. It is swallowed on
    the query path by design (a query that already succeeded must not fail for
    an operator convenience), so the only way an operator learns the list is
    missing something is this counter — the same rationale `evicted_total` has.
    Silently dropping it means promoting N templates and breaking the N+1st
    query after cutover.
    """
    store = InProcessObservedShapeStore(enabled=True)
    assert store.skeletonization_failures == 0
    with pytest.raises(SkeletonizationError):
        store.record(
            _query(where=Predicate(col="orders.meta", op="eq", value={"nested": 1})),
            connection_id="demo",
            principal_id="agent",
        )
    assert store.skeletonization_failures == 1
    assert build_observed_shape_report(store=store).skeletonization_failures == 1


def test_the_store_is_bounded_and_reports_evictions():
    """Shapes are caller-authored, so an unbounded store is a memory leak an
    adversarial caller controls. The ceiling must hold and be visible.
    """
    store = InProcessObservedShapeStore(max_entries=3, enabled=True)
    # Structurally distinct shapes — varying a *value* (a limit, a predicate
    # literal) deliberately no longer produces new entries, so the bound has to
    # be exercised with real structural variety.
    for index in range(10):
        store.record(
            (
                _query(select=["orders.id"] + [f"orders.status"] * 0, group_by=[], joins=[])
                if index == 0
                else _query(select=[f"orders.c{index}"])
            ),
            connection_id="demo",
            principal_id="agent",
        )
    assert len(store.list_shapes()) <= 3
    assert store.evicted_total > 0


def test_shapes_are_ranked_most_used_first():
    store = InProcessObservedShapeStore(enabled=True)
    store.record(_query(group_by=["orders.status"]), connection_id="demo", principal_id="agent")
    for _ in range(3):
        store.record(_query(), connection_id="demo", principal_id="agent")
    assert store.list_shapes()[0].occurrences == 3


def test_report_reports_nothing_when_recording_is_disabled():
    """An operator reading an empty list must be able to tell "recording is
    off" from "nothing ran" — narrowing a connection on the strength of the
    second when it was really the first would break the agent.
    """
    store = InProcessObservedShapeStore(enabled=True)
    store.record(_query(), connection_id="demo", principal_id="agent")
    store.enabled = False
    report = build_observed_shape_report(store=store)
    assert report.enabled is False
    assert report.shapes == []


def test_report_is_honest_about_being_process_local_and_volatile():
    report = build_observed_shape_report(store=InProcessObservedShapeStore(enabled=True))
    assert report.scope == "process-local-volatile"


# ---------------------------------------------------------------------------
# Promotability
# ---------------------------------------------------------------------------


def test_a_drafted_template_binds_back_into_a_valid_query():
    """The end-to-end property the whole feature rests on: observe a query,
    draft a template from its shape, bind a value, get a real StructuredQuery
    back. If this breaks, every drafted template is a landmine an operator
    discovers in production.
    """
    store = InProcessObservedShapeStore(enabled=True)
    observed = store.record(_query(), connection_id="demo", principal_id="agent")
    assert observed is not None

    template = observed.to_template_draft("orders_by_status")
    assert isinstance(template, QueryTemplate)

    bound = bind_template(template, {"status": "shipped", "limit": 5, "offset": 0})
    assert isinstance(bound, StructuredQuery)
    assert bound.from_table == "orders"
    assert bound.where.value == "shipped"


def test_a_drafted_template_targets_the_observed_connection():
    store = InProcessObservedShapeStore(enabled=True)
    observed = store.record(_query(), connection_id="analytics", principal_id="agent")
    assert observed.to_template_draft("orders_by_status").connection == "analytics"


def test_drafting_an_invalid_template_id_is_refused():
    """A template id must be a legal identifier; refusing here keeps the
    failure at draft time instead of at templates-file load time.
    """
    shape = ObservedShape(
        shape_hash="deadbeef",
        connection_id="demo",
        skeleton={"from": "orders", "select": ["orders.id"]},
        first_seen=datetime.now(timezone.utc),
        last_seen=datetime.now(timezone.utc),
    )
    with pytest.raises(pyd.ValidationError):
        shape.to_template_draft("not a valid id")


# ---------------------------------------------------------------------------
# The recording hook on the execute() path
# ---------------------------------------------------------------------------


def _orders_table() -> sa.Table:
    return sa.Table(
        "orders",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("status", sa.String(20)),
    )


@asynccontextmanager
async def _fake_session(*args, **kwargs):
    result = MagicMock()
    result.mappings.return_value.all.return_value = []
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    yield session


async def _run(query: StructuredQuery, *, enabled: bool) -> None:
    table = _orders_table()
    with (
        patch.object(observed_shape_store(), "enabled", enabled),
        patch.object(svc, "validate_schema", AsyncMock(return_value={"orders": table})),
        patch.object(svc, "session_scope", _fake_session),
        patch.object(svc, "audit_query"),
    ):
        service = StructuredQueryService(connection_id="demo")
        await service.execute(query)


@pytest.mark.asyncio
async def test_recording_is_off_by_default():
    """Opt-in, like every other guardrail in this repo — an upgrade must not
    silently start retaining a new class of record.
    """
    await _run(_query(), enabled=False)
    assert observed_shape_store().list_shapes() == []


@pytest.mark.asyncio
async def test_an_allowed_query_is_recorded_when_enabled():
    await _run(_query(), enabled=True)
    shapes = observed_shape_store().list_shapes()
    assert len(shapes) == 1
    assert shapes[0].connection_id == "demo"


@pytest.mark.asyncio
async def test_a_rejected_query_is_not_recorded():
    """Only shapes that were actually allowed are promotable — recording a
    rejected one would put a query the policy refuses into an operator's
    "promote these to templates" list.
    """
    set_policy_store(PolicyStore(default=Policy(denied_tables=["orders"]), overrides={}))
    with pytest.raises(PolicyViolationError):
        await _run(_query(), enabled=True)
    assert observed_shape_store().list_shapes() == []


@pytest.mark.asyncio
async def test_a_recorder_failure_never_fails_the_query():
    """The query already succeeded and its rows are in hand; an operator
    convenience must not turn that into an error for the caller.
    """
    table = _orders_table()
    with (
        patch.object(observed_shape_store(), "enabled", True),
        patch.object(svc, "validate_schema", AsyncMock(return_value={"orders": table})),
        patch.object(svc, "session_scope", _fake_session),
        patch.object(svc, "audit_query"),
        patch.object(svc.observed_shape_store(), "record", side_effect=RuntimeError("boom")),
    ):
        service = StructuredQueryService(connection_id="demo")
        result = await service.execute(_query())
    assert result.row_count == 0
