"""The gate: what it refuses, what it never refuses, and what it sends.

Two things here are the load-bearing answers to the two objections the product
has to survive, so they are asserted rather than described:

* **A refresh failure never blocks a query.** EULA §17.2 and
  `docs/LICENSING_FAQ.md` both promise it; the design puts refresh health on a
  separate axis to make it true by construction, and this pins that.
* **The refresh body is exactly the four disclosed fields.** Built in the shape
  of `test_credential_redaction.py` — against the object that actually goes on
  the wire, not a mock-call assertion — because that sentence is the load-bearing
  answer to the phone-home objection.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from querygate.core.exceptions import SubscriptionExpiredError, public_error_message
from querygate.execution.subscription_gate import (
    check_read_funnel,
    check_schema_discovery_funnel,
    check_write_funnel,
)
from querygate.metrics import SUBSCRIPTION_WOULD_BLOCK_TOTAL
from querygate.subscription.gate import require_active_subscription
from querygate.subscription.models import (
    PAYLOAD_FIELDS,
    EnforcementMode,
    Entitlement,
    EntitlementState,
    RefreshHealth,
    evaluate,
)
from querygate.subscription.sources import RefreshPayload
from querygate.subscription.state import subscription_state

pytestmark = [pytest.mark.security, pytest.mark.unit]

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _entitlement(*, expires_in_days: int, grace_days: int = 14, enforce: bool = True):
    expires = NOW + timedelta(days=expires_in_days)
    return Entitlement(
        schema_version=1,
        org_id="org",
        deployment_id="dep",
        serial=1,
        issued_at=NOW - timedelta(days=1),
        expires_at=expires,
        grace_expires_at=expires + timedelta(days=grace_days),
        enforcement=EnforcementMode.ENFORCE if enforce else EnforcementMode.OBSERVE,
        plan="team",
        max_connections=None,
        max_seats=None,
        renewal_url="https://example.invalid/renew",
    )


def _publish(status) -> None:
    subscription_state().publish(status)


# --- what the gate refuses, and what it must never refuse ---------------------


def test_a_valid_term_allows_queries():
    _publish(evaluate(_entitlement(expires_in_days=10), now=NOW))
    check_read_funnel()
    check_write_funnel()
    check_schema_discovery_funnel()


def test_the_grace_window_still_allows_everything():
    """Between the term ending and grace ending, the product works normally.
    `docs/LICENSING_FAQ.md`'s lapse table says so in the customer's words."""
    status = evaluate(_entitlement(expires_in_days=-1), now=NOW)
    assert status.entitlement is EntitlementState.GRACE
    _publish(status)
    check_read_funnel()


@pytest.mark.parametrize(
    "funnel", [check_read_funnel, check_write_funnel, check_schema_discovery_funnel]
)
def test_every_funnel_refuses_once_term_and_grace_have_elapsed(funnel):
    """Three funnels, not two. Schema discovery carries no AST and so passes
    through neither of the other two — an expired deployment enumerates nothing."""
    _publish(evaluate(_entitlement(expires_in_days=-30), now=NOW))
    with pytest.raises(SubscriptionExpiredError):
        funnel()


def test_a_failing_refresh_never_blocks_a_query():
    """The promise EULA §17.2 makes, asserted rather than described.

    Ten consecutive refresh failures, health FAILING — and the gate still
    allows, because `allows_queries` reads the entitlement axis alone.
    """
    status = evaluate(_entitlement(expires_in_days=10), now=NOW, consecutive_failures=10)
    assert status.refresh is RefreshHealth.FAILING
    _publish(status)
    check_read_funnel()
    check_write_funnel()


def test_a_cold_start_with_no_entitlement_fails_open():
    """A fresh install, a DR failover into an isolated network, or a scaled-out
    replica has nothing to be valid *from*. Failing closed there means our
    outage is the customer's outage on day one."""
    status = evaluate(None, now=NOW, first_boot=True)
    assert status.entitlement is EntitlementState.GRACE
    _publish(status)
    check_read_funnel()


def test_a_regressed_clock_enters_grace_not_expiry():
    """Trusting a provably wrong clock in the direction that blocks queries is
    the one error mode with no recovery path for the customer."""
    entitlement = _entitlement(expires_in_days=-30)
    status = evaluate(entitlement, now=NOW, high_water_mark=NOW + timedelta(days=90))
    assert status.entitlement is EntitlementState.GRACE
    assert status.reason == "clock-regressed"
    _publish(status)
    check_read_funnel()


# --- observe mode -------------------------------------------------------------


def test_observe_mode_counts_instead_of_refusing():
    """Go-live is a control-plane action — issuing `enforcement: enforce` — not a
    code change, so the enforce path runs in production long before it can
    refuse anything."""
    _publish(evaluate(_entitlement(expires_in_days=-30, enforce=False), now=NOW))
    check_read_funnel()  # must not raise
    assert SUBSCRIPTION_WOULD_BLOCK_TOTAL.labels(funnel="read")._value.get() == 1.0


def test_observe_mode_labels_the_funnel_that_would_have_refused():
    _publish(evaluate(_entitlement(expires_in_days=-30, enforce=False), now=NOW))
    check_write_funnel()
    check_schema_discovery_funnel()
    assert SUBSCRIPTION_WOULD_BLOCK_TOTAL.labels(funnel="write")._value.get() == 1.0
    assert SUBSCRIPTION_WOULD_BLOCK_TOTAL.labels(funnel="schema_discovery")._value.get() == 1.0
    assert SUBSCRIPTION_WOULD_BLOCK_TOTAL.labels(funnel="read")._value.get() == 0.0


# --- the message a customer actually sees -------------------------------------


def test_the_public_message_names_the_renewal_url():
    """Item 216 depends on this, and `public_error_message` is a closed
    isinstance allow-list — without an entry there the 402 body would read
    "An unexpected error occurred."."""
    exc = SubscriptionExpiredError(renewal_url="https://example.invalid/renew")
    message = public_error_message(exc)
    assert "https://example.invalid/renew" in message
    assert "unexpected error" not in message.lower()


def test_the_message_is_still_useful_without_a_renewal_url():
    assert "administrator" in public_error_message(SubscriptionExpiredError()).lower()


def test_the_error_is_not_a_policy_violation():
    """If it subclassed `ValueError`/`PolicyViolationError`, existing handling
    would map it to 422 and label it `policy` — telling the customer their own
    policy refused a query it actually permits, and writing that into the
    tamper-evident ledger."""
    from querygate.core.exceptions import PolicyViolationError

    exc = SubscriptionExpiredError()
    assert not isinstance(exc, (ValueError, PolicyViolationError))


# --- the four disclosed fields, asserted against the wire object --------------


def test_the_refresh_body_is_exactly_the_disclosed_fields():
    """The load-bearing answer to the phone-home objection.

    Asserted against the object that is actually serialised — not a mock-call
    assertion — in the shape of `test_credential_redaction.py`.
    """
    payload = RefreshPayload(org_id="org", deployment_id="dep", connection_count=3, seat_count=7)
    body = payload.as_dict()
    assert (
        set(body)
        == set(PAYLOAD_FIELDS)
        == {
            "org_id",
            "deployment_id",
            "connection_count",
            "seat_count",
        }
    )
    assert body == {
        "connection_count": 3,
        "deployment_id": "dep",
        "org_id": "org",
        "seat_count": 7,
    }


def test_the_refresh_body_carries_no_customer_data():
    """Named negatives, because the EULA §16.2 promise is a list of absences."""
    body = RefreshPayload(
        org_id="org", deployment_id="dep", connection_count=1, seat_count=1
    ).as_dict()
    serialised = repr(body).lower()
    for forbidden in ("password", "dsn", "connection_string", "sql", "query", "schema", "table"):
        assert forbidden not in serialised


def test_adding_an_attribute_to_the_payload_cannot_reach_the_wire():
    """`as_dict` iterates `PAYLOAD_FIELDS`, so a field added to the class but not
    to the disclosure is simply not emitted — the disclosure leads the code."""
    payload = RefreshPayload(org_id="o", deployment_id="d", connection_count=0, seat_count=0)
    assert "hostname" not in payload.as_dict()


# --- the gate never reads a store --------------------------------------------


def test_the_gate_is_synchronous_and_reads_no_store():
    """The one deliberate sync exception in a package whose Protocols are all
    `async def`. It is justified only while it reads an already-evaluated
    in-memory verdict; the moment it touches `cache.py` it acquires an I/O
    failure mode on every query."""
    import inspect

    assert not inspect.iscoroutinefunction(require_active_subscription)
    source = inspect.getsource(require_active_subscription)
    assert "cache" not in source
    assert "await" not in source


# --- the CALL SITES, not just the helper --------------------------------------
#
# Everything above exercises `check_*_funnel()` directly, which verifies the
# helper and nothing else. Measured: deleting the `check_read_funnel()` call
# from `StructuredQueryService.execute` left all 3,474 tests green. These go
# through the real service entry points, so the *wiring* is what fails when the
# wiring breaks — the distinction this repo has been bitten by before.


def _expire() -> None:
    _publish(evaluate(_entitlement(expires_in_days=-30), now=NOW))


def _read_query():
    from querygate.query_ast.models import StructuredQuery

    return StructuredQuery(from_table="customers", select=["customers.id"], limit=10)


def _write_statement():
    from querygate.write_ast.models import DeleteStatement, WritePredicate

    return DeleteStatement(table="orders", where=WritePredicate(col="orders.id", op="eq", value=1))


@pytest.mark.asyncio
async def test_the_read_service_entry_point_refuses_when_expired():
    from querygate.execution.service import StructuredQueryService

    _expire()
    query = _read_query()
    with pytest.raises(SubscriptionExpiredError):
        await StructuredQueryService(connection_id="demo").execute(query)


@pytest.mark.asyncio
async def test_the_read_gate_runs_before_quota_and_the_concurrency_slot():
    """An expired deployment must not spend a caller's quota or hold a slot for
    a request it is going to refuse. Asserted by the fact that the refusal
    happens with no connection configured to reserve against at all."""
    from querygate.execution.service import StructuredQueryService

    _expire()
    query = _read_query()
    # `no-such-connection` would raise NotFoundError from `_get_policy` if the
    # gate ran after policy resolution; SubscriptionExpiredError proves it is first.
    with pytest.raises(SubscriptionExpiredError):
        await StructuredQueryService(connection_id="no-such-connection").execute(query)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["list_tables", "describe_table", "search_catalog"])
async def test_every_schema_discovery_entry_point_refuses_when_expired(method):
    """The third funnel. These reach the live database through reflection and
    carry no AST, so they pass through neither of the other two."""
    from querygate.execution.service import StructuredQueryService

    _expire()
    service = StructuredQueryService(connection_id="demo")
    argument = {"list_tables": (), "describe_table": ("users",), "search_catalog": ("users",)}[
        method
    ]
    with pytest.raises(SubscriptionExpiredError):
        await getattr(service, method)(*argument)


@pytest.mark.asyncio
async def test_the_write_execute_entry_point_refuses_when_expired():
    from querygate.execution.write_execution import WriteExecutionService

    _expire()
    statement = _write_statement()
    with pytest.raises(SubscriptionExpiredError):
        await WriteExecutionService("demo").execute(statement)


@pytest.mark.asyncio
async def test_the_atomic_batch_entry_point_refuses_when_expired():
    """`_execute_many_atomically` opens its own `session_scope`, so it needs its
    own call — a gate only in `execute` would leave the atomic path running."""
    from querygate.execution.write_execution import WriteExecutionService

    _expire()
    statement = _write_statement()
    with pytest.raises(SubscriptionExpiredError):
        await WriteExecutionService("demo")._execute_many_atomically([statement])


@pytest.mark.asyncio
async def test_the_write_preview_entry_point_refuses_when_expired():
    """A separate class from `WriteExecutionService`, so a separate call site."""
    from querygate.execution.write_preview import WritePreviewService

    _expire()
    statement = _write_statement()
    with pytest.raises(SubscriptionExpiredError):
        await WritePreviewService("demo").preview(statement)


@pytest.mark.asyncio
async def test_a_batch_does_not_swallow_the_gate_into_a_two_hundred():
    """`_execute_batch_item`'s catch-all turns any exception into a per-item
    error string inside an HTTP 200. A deployment-wide billing state must
    re-raise instead, or the caller sees N failures rather than one 402."""
    from querygate.execution.service import StructuredQueryService

    _expire()
    query = _read_query()
    with pytest.raises(SubscriptionExpiredError):
        await StructuredQueryService(connection_id="demo").execute_many([query, query])


@pytest.mark.asyncio
async def test_verdict_does_not_record_a_false_policy_denial():
    """The worst of the four swallowing surfaces. `verdict`'s documented
    catch-all returns HTTP 200 `{"allowed": false, "reason":
    "not-available-to-you"}` and writes `policy_decision="denied"` into the
    tamper-evident ledger — telling the customer, permanently, that their own
    policy refused a query it actually permits."""
    from querygate.execution.service import StructuredQueryService

    _expire()
    query = _read_query()
    with pytest.raises(SubscriptionExpiredError):
        await StructuredQueryService(connection_id="demo").verdict(query)


@pytest.mark.asyncio
async def test_explain_many_does_not_swallow_the_gate():
    from querygate.execution.service import StructuredQueryService

    _expire()
    query = _read_query()
    with pytest.raises(SubscriptionExpiredError):
        await StructuredQueryService(connection_id="demo").explain_many([query])
