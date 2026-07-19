"""Unit tests for the agent-visible admission helpers (TODO.md item 35 phase 1)."""

from __future__ import annotations

import uuid

from querygate.execution.admission import QueueMode, new_admission_id, resolve_wait_seconds


def test_new_admission_id_is_a_uuid():
    admission_id = new_admission_id()
    assert uuid.UUID(admission_id)


def test_new_admission_id_is_unique_per_call():
    assert new_admission_id() != new_admission_id()


def test_resolve_wait_seconds_defaults_to_policy_ceiling():
    # Omitting both queue_mode and a requested wait must reproduce the exact
    # pre-item-35 behavior: wait up to the policy's own ceiling.
    assert (
        resolve_wait_seconds(
            queue_mode=None, requested_wait_seconds=None, policy_ceiling_seconds=10.0
        )
        == 10.0
    )


def test_resolve_wait_seconds_fail_fast_never_waits():
    assert (
        resolve_wait_seconds(
            queue_mode=QueueMode.FAIL_FAST,
            requested_wait_seconds=99.0,
            policy_ceiling_seconds=10.0,
        )
        == 0.0
    )


def test_resolve_wait_seconds_honors_a_shorter_caller_request():
    assert (
        resolve_wait_seconds(
            queue_mode=QueueMode.WAIT, requested_wait_seconds=2.0, policy_ceiling_seconds=10.0
        )
        == 2.0
    )


def test_resolve_wait_seconds_clamps_a_longer_caller_request_to_the_ceiling():
    # The security property: a caller can shorten the wait, never lengthen it
    # past what the operator configured.
    assert (
        resolve_wait_seconds(
            queue_mode=QueueMode.WAIT, requested_wait_seconds=999.0, policy_ceiling_seconds=10.0
        )
        == 10.0
    )


def test_resolve_wait_seconds_clamps_a_negative_caller_request_to_zero():
    assert (
        resolve_wait_seconds(
            queue_mode=QueueMode.WAIT, requested_wait_seconds=-5.0, policy_ceiling_seconds=10.0
        )
        == 0.0
    )
