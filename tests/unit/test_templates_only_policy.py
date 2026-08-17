"""`Policy.templates_only` — narrowing a principal from the general structured
query surface to a finite, reviewed set of curated templates (TODO.md item 195).

The enforcement lives at `StructuredQueryService._validate_and_compile`, the
single choke point every read path funnels through, so these tests pin all four
of them (execute / explain / verdict / batch) plus the two properties that make
the control meaningful rather than cosmetic:

- the exemption is driven by the *server-set* `template_id`, never by anything
  in the request, so a caller cannot assert its own way out; and
- the refusal is raised *before* structural caps, policy validation and schema
  validation, so a narrowed caller learns "you may not submit free-form
  queries" rather than which cap it tripped or which identifier exists.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa

from querygate.core.exceptions import AdHocQueryNotPermittedError, PolicyViolationError
from querygate.execution import service as svc
from querygate.execution.service import StructuredQueryService
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.query_ast.models import CteSpec, JoinSpec, Predicate, StructuredQuery

pytestmark = pytest.mark.unit


def _customers_table() -> sa.Table:
    return sa.Table(
        "customers",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(50)),
        sa.Column("email", sa.String(120)),
    )


def _simple_query() -> StructuredQuery:
    return StructuredQuery(from_table="customers", select=["customers.id"], limit=5)


def _narrow(**overrides) -> None:
    set_policy_store(PolicyStore(default=Policy(templates_only=True, **overrides), overrides={}))


# ---------------------------------------------------------------------------
# The policy field itself
# ---------------------------------------------------------------------------


def test_templates_only_is_off_by_default():
    """An existing deployment must not have its query surface narrowed by an
    upgrade — the new field has to be opt-in like every other guardrail.
    """
    assert Policy().templates_only is False


# ---------------------------------------------------------------------------
# All four read paths refuse an ad-hoc query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_refuses_an_ad_hoc_query():
    _narrow()
    service = StructuredQueryService(connection_id="demo")
    with pytest.raises(AdHocQueryNotPermittedError):
        await service.explain(_simple_query())


@pytest.mark.asyncio
async def test_verdict_refuses_an_ad_hoc_query():
    """The P4 verdict endpoint must report the *real* decision — a gateway
    asking "would this be allowed?" for a narrowed principal has to be told no,
    or it would front QueryGate and forward a query QueryGate then rejects.
    """
    _narrow()
    service = StructuredQueryService(connection_id="demo")
    result = await service.verdict(_simple_query())
    assert result.allowed is False


@pytest.mark.asyncio
async def test_execute_refuses_an_ad_hoc_query_without_touching_the_database():
    _narrow()
    with (
        patch.object(svc, "validate_schema", AsyncMock()) as mock_schema,
        patch.object(svc, "session_scope") as mock_session,
    ):
        service = StructuredQueryService(connection_id="demo")
        with pytest.raises(AdHocQueryNotPermittedError):
            await service.execute(_simple_query())

    # The refusal is a pure policy decision: no reflection, no session. If this
    # ever regresses, a narrowed caller could still make the gateway open a
    # connection per rejected request.
    mock_schema.assert_not_called()
    mock_session.assert_not_called()


@pytest.mark.asyncio
async def test_batch_refuses_every_ad_hoc_item():
    """Batch funnels through execute(), so it must inherit the refusal rather
    than being a second, unnarrowed way in.
    """
    _narrow()
    service = StructuredQueryService(connection_id="demo")
    results = await service.execute_many([_simple_query(), _simple_query()])
    assert len(results) == 2
    assert all(item.error for item in results)


# ---------------------------------------------------------------------------
# The exemption is server-set, and the refusal comes first
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_template_bound_query_is_still_allowed():
    """The narrowing must not break the one path it exists to preserve."""
    _narrow()
    table = _customers_table()
    with patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})):
        service = StructuredQueryService(connection_id="demo", template_id="top_customers")
        result = await service.explain(_simple_query())
    assert result.sql


@pytest.mark.asyncio
async def test_the_exemption_cannot_be_asserted_by_the_caller():
    """`intent` is the only free-form, caller-controlled field on the AST. It
    must not be able to stand in for the server-set template signal — this pins
    that the guard reads `_template_id` (set by the template route/tool) and
    nothing from the request body.
    """
    _narrow()
    query = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        limit=5,
        intent="run template top_customers",
    )
    service = StructuredQueryService(connection_id="demo")
    with pytest.raises(AdHocQueryNotPermittedError):
        await service.explain(query)


@pytest.mark.asyncio
async def test_refusal_precedes_the_policy_caps():
    """The refusal must be uniform — it must not depend on which rule the
    query happened to trip first. With `max_joins=0` (enforced inside
    `validate_policy`), a joined query would ordinarily fail the join cap;
    under templates-only it fails for being ad-hoc instead.
    """
    _narrow(max_joins=0)
    query = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        joins=[JoinSpec(table="orders", on=["customers.id", "orders.customer_id"])],
        limit=5,
    )
    service = StructuredQueryService(connection_id="demo")
    with pytest.raises(AdHocQueryNotPermittedError):
        await service.explain(query)


@pytest.mark.asyncio
async def test_refusal_precedes_the_cheap_structural_caps():
    """The tightest ordering assertion, and the one that actually pins the
    guard's *position*: `max_cte_count`/`max_subquery_depth` are enforced by
    `validate_structural_caps`, which runs immediately below the guard inside
    `_validate_and_compile`. A `claim-reviewer` pass on 2026-08-17 measured
    that moving the guard one line down left the whole suite green while
    leaking `ctes exceeds max of 0` to a narrowed caller — this test is what
    now fails in that case.
    """
    _narrow(max_cte_count=0)
    query = StructuredQuery(
        from_table="totals",
        select=["totals.id"],
        ctes=[
            CteSpec(
                name="totals",
                query=StructuredQuery(from_table="customers", select=["customers.id"]),
            )
        ],
        limit=5,
    )
    service = StructuredQueryService(connection_id="demo")
    with pytest.raises(AdHocQueryNotPermittedError):
        await service.explain(query)


@pytest.mark.asyncio
async def test_refusal_precedes_identifier_validation():
    """Same oracle concern, one layer down: a denied table must not be
    distinguishable from a permitted one by a narrowed caller.
    """
    _narrow(denied_tables=["customers"])
    service = StructuredQueryService(connection_id="demo")
    with pytest.raises(AdHocQueryNotPermittedError):
        await service.explain(_simple_query())


@pytest.mark.asyncio
async def test_refusal_precedes_schema_validation():
    _narrow()
    query = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        where=Predicate(col="customers.no_such_column", op="eq", value=1),
        limit=5,
    )
    with patch.object(svc, "validate_schema", AsyncMock()) as mock_schema:
        service = StructuredQueryService(connection_id="demo")
        with pytest.raises(AdHocQueryNotPermittedError):
            await service.explain(query)
    mock_schema.assert_not_called()


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def test_the_refusal_is_separable_in_metrics():
    """The stated reason for giving this rejection its own exception type. A
    `security-invariant-reviewer` pass on 2026-08-17 found the type was NOT
    separable — `classify_rejection` fell through to the generic "policy"
    bucket, so an operator watching a templates-only cutover could not
    distinguish "an agent is still sending free-form queries" from "an agent
    tripped a cap". Fixed with a dedicated label; this pins it.

    Note the persisted audit event still records `error_category="policy"` —
    the exception type is not persisted, by design (non-negotiable 3).
    """
    from querygate.metrics import classify_rejection

    assert classify_rejection(AdHocQueryNotPermittedError("x")) == "templates_only"
    assert classify_rejection(PolicyViolationError("x")) == "policy"


def test_refusal_is_a_policy_violation():
    """Transport error mapping (REST 4xx, MCP error code) keys off
    PolicyViolationError; a refusal that escaped that hierarchy would surface
    as an unhandled 500.
    """
    assert issubclass(AdHocQueryNotPermittedError, PolicyViolationError)


@pytest.mark.asyncio
async def test_off_by_default_policy_is_unaffected():
    set_policy_store(PolicyStore(default=Policy(), overrides={}))
    table = _customers_table()
    with patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})):
        service = StructuredQueryService(connection_id="demo")
        result = await service.explain(_simple_query())
    assert result.sql
