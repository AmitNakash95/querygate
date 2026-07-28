"""Unit tests for the in-process async execution lifecycle store (TODO.md
item 35 phase 3): admission/state bookkeeping and cancellation orchestration,
independent of any real database (dialect cancellation is stubbed).
"""

from __future__ import annotations

import asyncio

import pytest

from querygate.core.exceptions import (
    PUBLIC_INTERNAL_ERROR,
    QueryCancellationNotEnabledError,
    QueryCancellationNotReadyError,
    QueryValidationError,
)
from querygate.execution.async_execution import (
    async_execution_store,
    request_cancel,
    start_async_execution,
)

pytestmark = pytest.mark.unit


async def _immediate_success(admission_id, on_admitted, on_session_identifier):
    await on_admitted(0)
    on_session_identifier("4242")
    return {"rows": [{"a": 1}]}


async def _never_finishes(admission_id, on_admitted, on_session_identifier):
    await asyncio.Event().wait()


async def _fails(admission_id, on_admitted, on_session_identifier):
    await on_admitted(0)
    on_session_identifier("55")
    raise RuntimeError("boom: driver detail nobody should see over REST")


async def _fails_actionably(admission_id, on_admitted, on_session_identifier):
    await on_admitted(0)
    raise QueryValidationError("max_joins exceeded")


@pytest.mark.asyncio
async def test_successful_execution_transitions_to_completed_with_the_result():
    record = start_async_execution("demo", "alice", _immediate_success)
    await record.task
    assert record.state == "completed"
    assert record.result == {"rows": [{"a": 1}]}
    assert record.session_identifier == "4242"
    assert async_execution_store().get(record.admission_id) is record


@pytest.mark.asyncio
async def test_a_failing_execution_transitions_to_failed_with_a_masked_error():
    """Mirrors the synchronous REST path's mask_unexpected(): a non-actionable
    exception (here a plain RuntimeError, standing in for a raw driver/SQL
    detail) must never reach the polled record verbatim, since GET
    .../query/{admission_id} serves error_message straight to whichever
    principal owns the admission_id."""
    record = start_async_execution("demo", "alice", _fails)
    await record.task
    assert record.state == "failed"
    assert record.error_message == PUBLIC_INTERNAL_ERROR
    assert "boom" not in record.error_message
    assert "driver detail" not in record.error_message


@pytest.mark.asyncio
async def test_a_failing_execution_with_an_actionable_error_is_not_masked():
    """The flip side of the masking test above: an actionable domain
    exception (QueryValidationError, same family the sync REST path already
    lets through unmasked) still reaches the caller with its real,
    actionable message — masking is selective, not blanket."""
    record = start_async_execution("demo", "alice", _fails_actionably)
    await record.task
    assert record.state == "failed"
    assert record.error_message == "max_joins exceeded"


@pytest.mark.asyncio
async def test_cancelling_a_still_queued_execution_marks_it_cancelled():
    record = start_async_execution("demo", "alice", _never_finishes)
    assert record.state == "queued"
    # Give the background task a real chance to start running (reach its own
    # await point inside `_never_finishes`) before cancelling it — cancelling
    # a task that has never been scheduled even once short-circuits before
    # its coroutine body (and this module's own except-clause) ever runs,
    # which is a real asyncio quirk, not the behavior a genuine caller
    # (arriving as a separate later HTTP request) would ever observe.
    await asyncio.sleep(0)
    state = await request_cancel(
        record, allow_query_cancellation=True, engine=None, dialect="postgresql"
    )
    assert state == "queued"  # returned synchronously; the task hasn't unwound yet
    await record.task  # the cancelled coroutine's except-clause swallows
    # CancelledError and returns normally, so the task itself completes
    # (rather than raising) once it does.
    assert record.state == "cancelled"


@pytest.mark.asyncio
async def test_cancelling_a_running_query_requires_policy_to_allow_it():
    """Audit fix: a mutation flipping this `not allow_query_cancellation`
    check would previously fail this test only via an unrelated AttributeError
    (falling through to a real cancel_session(engine=None, ...) call) rather
    than a clean, legible assertion failure — asserting cancel_session is
    never invoked pins the actual property under test (rejected BEFORE any DB
    call), not just "some exception happened somewhere."""
    record = start_async_execution("demo", "alice", _never_finishes)
    record.state = "running"
    record.session_identifier = "4242"

    calls = []

    async def _fake_cancel_session(engine, dialect, identifier):
        calls.append((engine, dialect, identifier))

    import querygate.execution.async_execution as mod

    original = mod.cancel_session
    mod.cancel_session = _fake_cancel_session
    try:
        with pytest.raises(QueryCancellationNotEnabledError):
            await request_cancel(
                record, allow_query_cancellation=False, engine=None, dialect="postgresql"
            )
    finally:
        mod.cancel_session = original
        record.task.cancel()
    assert record.state == "running"  # unchanged — rejected before any DB call
    assert calls == []


@pytest.mark.asyncio
async def test_cancelling_a_running_query_with_no_session_identifier_yet_is_retryable():
    record = start_async_execution("demo", "alice", _never_finishes)
    record.state = "running"
    assert record.session_identifier is None
    with pytest.raises(QueryCancellationNotReadyError):
        await request_cancel(
            record, allow_query_cancellation=True, engine=None, dialect="postgresql"
        )
    record.task.cancel()


@pytest.mark.asyncio
async def test_cancelling_a_running_query_invokes_dialect_cancellation():
    record = start_async_execution("demo", "alice", _never_finishes)
    record.state = "running"
    record.session_identifier = "4242"

    calls = []

    async def _fake_cancel_session(engine, dialect, identifier):
        calls.append((engine, dialect, identifier))

    import querygate.execution.async_execution as mod

    original = mod.cancel_session
    mod.cancel_session = _fake_cancel_session
    try:
        state = await request_cancel(
            record, allow_query_cancellation=True, engine="ENGINE", dialect="postgresql"
        )
    finally:
        mod.cancel_session = original
        record.task.cancel()
    assert state == "cancel_requested"
    assert record.state == "cancel_requested"
    assert calls == [("ENGINE", "postgresql", "4242")]


@pytest.mark.asyncio
async def test_a_query_that_completes_despite_a_cancel_request_reports_completed_not_cancelled():
    """A DB-level cancel can lose the race against the query finishing —
    report what actually happened rather than forcing "cancelled" onto a
    real result."""

    async def _finishes_after_cancel_requested(admission_id, on_admitted, on_session_identifier):
        await on_admitted(0)
        on_session_identifier("4242")
        return {"rows": []}

    record = start_async_execution("demo", "alice", _finishes_after_cancel_requested)
    record.state = "cancel_requested"  # simulate a cancel already in flight
    await record.task
    assert record.state == "completed"


@pytest.mark.asyncio
async def test_a_query_that_fails_after_a_cancel_request_reports_cancelled():
    # Doesn't call on_admitted/on_session_identifier: in the real flow those
    # already fired earlier (a cancel can only be requested once a query is
    # "running"), and calling on_admitted again here would incorrectly reset
    # the "cancel_requested" state this test simulates below back to "running".
    async def _fails_after_cancel_requested(admission_id, on_admitted, on_session_identifier):
        raise RuntimeError("canceling statement due to user request")

    record = start_async_execution("demo", "alice", _fails_after_cancel_requested)
    record.state = "cancel_requested"  # simulate: already running, cancel in flight
    await record.task
    assert record.state == "cancelled"


@pytest.mark.asyncio
async def test_cancelling_an_already_terminal_record_is_an_idempotent_no_op():
    record = start_async_execution("demo", "alice", _immediate_success)
    await record.task
    assert record.state == "completed"
    state = await request_cancel(
        record, allow_query_cancellation=True, engine=None, dialect="postgresql"
    )
    assert state == "completed"


@pytest.mark.asyncio
async def test_cancelling_a_record_already_cancel_requested_is_an_idempotent_no_op():
    """A second cancel call (e.g. a caller retrying after a slow response)
    while a first cancel is already in flight must not re-issue cancel_session
    a second time — same "fall through to the terminal no-op" path already
    proven for a completed record, but exercised directly for the
    cancel_requested state itself rather than only by proxy."""
    record = start_async_execution("demo", "alice", _never_finishes)
    record.state = "running"
    record.session_identifier = "4242"
    record.state = "cancel_requested"  # simulate: a first cancel already in flight

    calls = []

    async def _fake_cancel_session(engine, dialect, identifier):
        calls.append((engine, dialect, identifier))

    import querygate.execution.async_execution as mod

    original = mod.cancel_session
    mod.cancel_session = _fake_cancel_session
    try:
        state = await request_cancel(
            record, allow_query_cancellation=True, engine=None, dialect="postgresql"
        )
    finally:
        mod.cancel_session = original
        record.task.cancel()
    assert state == "cancel_requested"
    assert calls == []
