"""In-query approval via MCP elicitation, ported to the `2026-07-28` Multi
Round-Trip Requests model (TODO.md item 92 / 128).

Two-phase design under test:
1. `build_pending_input_required` — turns one or more gated batch items into
   an `InputRequiredResult`, minting a signed "pending" token per item bound
   to its fingerprint, bundled into `request_state`.
2. `resolve_approval_tokens_from_retry` — on the client's retry (carrying
   `ctx.input_responses`/`ctx.request_state`), verifies each pending token
   against the *freshly re-derived* fingerprint of the resubmitted item and,
   only if approved, mints a real grant.

Plus the tool-layer wiring (`run_structured_queries`/`run_structured_writes`):
a first call with no prior state builds `InputRequiredResult` for gated
items; a "retry" call with `ctx.input_responses`/`ctx.request_state` set
resolves tokens and lets the query/write through.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa
from mcp.types import ElicitResult, InputRequiredResult

from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.exceptions import ApprovalRequiredError
from querygate.execution import service as svc
from querygate.execution.approval import (
    TOKEN_KIND_PENDING,
    issue_approval_token,
    query_fingerprint,
    verify_approval_token,
)
from querygate.execution.cost_estimation import QueryCostEstimate
from querygate.execution.service import StructuredQueryService
from querygate.mcp.elicitation import (
    build_pending_input_required,
    resolve_approval_tokens_from_retry,
)
from querygate.mcp.tools import query as qtool
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.query_ast.models import StructuredQuery

pytestmark = pytest.mark.unit

_KEY = "elicit-hmac-key"
_QUERY = StructuredQuery(from_table="orders", select=["orders.id"])
_OTHER_QUERY = StructuredQuery(from_table="orders", select=["orders.total"])
_CALLER = Principal(subject="agent-1")
_OTHER_CALLER = Principal(subject="agent-2")
_CONNECTION = "demo"
_OTHER_CONNECTION = "other-connection"


def _config(*, enabled: bool = True, key: str = _KEY) -> AppConfig:
    return AppConfig(
        environment="localhost",
        mcp_elicitation_approval_enabled=enabled,
        approval_token_hmac_key=key,
    )


def _ctx(*, request_state: str | None = None, responses: dict | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.request_state = request_state
    ctx.input_responses = responses or {}
    return ctx


def _accept(approve: bool) -> ElicitResult:
    return ElicitResult(action="accept", content={"approve": approve})


def _decline() -> ElicitResult:
    return ElicitResult(action="decline")


# --------------------------------------------------------------------------- #
# build_pending_input_required
# --------------------------------------------------------------------------- #


def test_build_pending_none_when_channel_disabled():
    items = [("q0", query_fingerprint(_QUERY), ["big estimate"])]
    assert (
        build_pending_input_required(
            items=items,
            config=_config(enabled=False),
            caller=_CALLER,
            connection_id=_CONNECTION,
        )
        is None
    )


def test_build_pending_none_when_no_signing_key():
    items = [("q0", query_fingerprint(_QUERY), ["big estimate"])]
    assert (
        build_pending_input_required(
            items=items, config=_config(key=""), caller=_CALLER, connection_id=_CONNECTION
        )
        is None
    )


def test_build_pending_none_when_no_items():
    assert (
        build_pending_input_required(
            items=[], config=_config(), caller=_CALLER, connection_id=_CONNECTION
        )
        is None
    )


def test_build_pending_shapes_one_input_request_per_item():
    fp = query_fingerprint(_QUERY)
    result = build_pending_input_required(
        items=[("q0", fp, ["estimate too large"])],
        config=_config(),
        caller=_CALLER,
        connection_id=_CONNECTION,
    )
    assert isinstance(result, InputRequiredResult)
    assert set(result.input_requests) == {"q0"}
    assert "estimate too large" in result.input_requests["q0"].params.message
    # request_state bundles a signed pending token per key, verifiable against
    # exactly the fingerprint it was built for, and (item 151) bound to the
    # calling principal and connection.
    pending = json.loads(result.request_state)
    assert set(pending) == {"q0"}
    assert verify_approval_token(
        pending["q0"],
        fingerprint=fp,
        key=_KEY,
        connection_id=_CONNECTION,
        principal_subject=_CALLER.subject,
        expected_kind=TOKEN_KIND_PENDING,
    )
    # A pending token is never itself a redeemable grant (item 151 F1) — the
    # default expected_kind="grant" check rejects it even with every other
    # claim matching.
    assert not verify_approval_token(
        pending["q0"],
        fingerprint=fp,
        key=_KEY,
        connection_id=_CONNECTION,
        principal_subject=_CALLER.subject,
    )


def test_build_pending_covers_multiple_batch_items_in_one_round_trip():
    """TODO.md item 128: execute_many fires per batch item, so multiple gated
    queries in the same batch must surface as multiple keys in ONE
    InputRequiredResult, not one round trip per item."""
    fp0, fp1 = query_fingerprint(_QUERY), query_fingerprint(_OTHER_QUERY)
    result = build_pending_input_required(
        items=[("q0", fp0, ["r0"]), ("q2", fp1, ["r2"])],
        config=_config(),
        caller=_CALLER,
        connection_id=_CONNECTION,
    )
    assert set(result.input_requests) == {"q0", "q2"}
    pending = json.loads(result.request_state)
    assert verify_approval_token(
        pending["q0"],
        fingerprint=fp0,
        key=_KEY,
        connection_id=_CONNECTION,
        principal_subject=_CALLER.subject,
        expected_kind=TOKEN_KIND_PENDING,
    )
    assert verify_approval_token(
        pending["q2"],
        fingerprint=fp1,
        key=_KEY,
        connection_id=_CONNECTION,
        principal_subject=_CALLER.subject,
        expected_kind=TOKEN_KIND_PENDING,
    )
    # Cross-key verification must fail — q0's pending token is bound to fp0 only.
    assert not verify_approval_token(
        pending["q0"],
        fingerprint=fp1,
        key=_KEY,
        connection_id=_CONNECTION,
        principal_subject=_CALLER.subject,
        expected_kind=TOKEN_KIND_PENDING,
    )


def test_pending_token_cannot_be_redeemed_directly_as_a_grant(monkeypatch):
    """TODO.md item 151 (F1, `security-invariant-reviewer`): a pending MRTR
    elicitation token is only an integrity-protected 'this fingerprint/
    connection/principal was asked about' marker — it carries a genuine `fp`/
    `cx`/`sub_bind`, but nobody has actually answered the elicitation prompt
    yet. Before the `"k"`/`kind` claim, `verify_approval_token` could not tell
    a pending token from a real grant, so a principal could pull the pending
    token straight off `request_state` and submit it directly as
    `X-QueryGate-Approval` (or in a batch's `approval_tokens` map) and have it
    execute — no elicitation ever answered, no `query:approve` scope needed.
    Exercises the real `StructuredQueryService._enforce_approval_gate` seam a
    REST/MCP execute call would actually go through, not just
    `verify_approval_token` directly."""
    monkeypatch.setattr(svc.app_config, "approval_token_hmac_key", _KEY)
    fp = query_fingerprint(_QUERY)
    result = build_pending_input_required(
        items=[("q0", fp, ["big estimate"])],
        config=_config(),
        caller=_CALLER,
        connection_id=_CONNECTION,
    )
    pending_token = json.loads(result.request_state)["q0"]

    policy = Policy(approval_max_estimated_rows=100)
    estimate = QueryCostEstimate(estimated_rows=500, estimated_total_cost=None)
    service = StructuredQueryService(connection_id=_CONNECTION, principal=_CALLER)
    with pytest.raises(ApprovalRequiredError):
        service._enforce_approval_gate(estimate, policy, _QUERY, pending_token)


# --------------------------------------------------------------------------- #
# resolve_approval_tokens_from_retry
# --------------------------------------------------------------------------- #


def _pending_state(
    fp: str,
    *,
    key: str = _KEY,
    connection_id: str = _CONNECTION,
    principal_subject: str = _CALLER.subject,
) -> str:
    return json.dumps(
        {
            "q0": issue_approval_token(
                fingerprint=fp,
                approver_subject="mcp:pending-elicitation",
                key=key,
                connection_id=connection_id,
                principal_subject=principal_subject,
                kind=TOKEN_KIND_PENDING,
            )
        }
    )


def test_resolve_empty_when_channel_disabled():
    fp = query_fingerprint(_QUERY)
    ctx = _ctx(request_state=_pending_state(fp), responses={"q0": _accept(True)})
    resolved = resolve_approval_tokens_from_retry(
        ctx=ctx,
        fingerprints_by_key={"q0": fp},
        caller=_CALLER,
        config=_config(enabled=False),
        connection_id=_CONNECTION,
    )
    assert resolved == {}


def test_resolve_empty_when_no_prior_state():
    fp = query_fingerprint(_QUERY)
    ctx = _ctx()  # first call: no request_state, no input_responses
    resolved = resolve_approval_tokens_from_retry(
        ctx=ctx,
        fingerprints_by_key={"q0": fp},
        caller=_CALLER,
        config=_config(),
        connection_id=_CONNECTION,
    )
    assert resolved == {}


def test_resolve_mints_bound_token_on_approval():
    fp = query_fingerprint(_QUERY)
    ctx = _ctx(request_state=_pending_state(fp), responses={"q0": _accept(True)})
    resolved = resolve_approval_tokens_from_retry(
        ctx=ctx,
        fingerprints_by_key={"q0": fp},
        caller=_CALLER,
        config=_config(),
        connection_id=_CONNECTION,
    )
    assert fp in resolved
    assert verify_approval_token(
        resolved[fp],
        fingerprint=fp,
        key=_KEY,
        connection_id=_CONNECTION,
        principal_subject=_CALLER.subject,
    )
    # ...and not for a different query.
    other_fp = query_fingerprint(_OTHER_QUERY)
    assert not verify_approval_token(
        resolved[fp],
        fingerprint=other_fp,
        key=_KEY,
        connection_id=_CONNECTION,
        principal_subject=_CALLER.subject,
    )


def test_resolve_empty_on_decline():
    fp = query_fingerprint(_QUERY)
    ctx = _ctx(request_state=_pending_state(fp), responses={"q0": _decline()})
    resolved = resolve_approval_tokens_from_retry(
        ctx=ctx,
        fingerprints_by_key={"q0": fp},
        caller=_CALLER,
        config=_config(),
        connection_id=_CONNECTION,
    )
    assert resolved == {}


def test_resolve_empty_when_accepted_but_not_approved():
    fp = query_fingerprint(_QUERY)
    ctx = _ctx(request_state=_pending_state(fp), responses={"q0": _accept(False)})
    resolved = resolve_approval_tokens_from_retry(
        ctx=ctx,
        fingerprints_by_key={"q0": fp},
        caller=_CALLER,
        config=_config(),
        connection_id=_CONNECTION,
    )
    assert resolved == {}


def test_resolve_rejects_a_fingerprint_swapped_at_retry():
    """The mutation-verified enforcement point (TODO.md item 128, trap 1): a
    pending token issued for query A's fingerprint must NOT resolve when the
    retry's freshly re-derived fingerprint at the same key is query B's —
    otherwise a caller could obtain approval for a cheap query and replay the
    granted state against an expensive one in the same slot."""
    fp_a = query_fingerprint(_QUERY)
    fp_b = query_fingerprint(_OTHER_QUERY)
    ctx = _ctx(request_state=_pending_state(fp_a), responses={"q0": _accept(True)})
    # The retry's own re-derived fingerprint at "q0" is now fp_b, not fp_a.
    resolved = resolve_approval_tokens_from_retry(
        ctx=ctx,
        fingerprints_by_key={"q0": fp_b},
        caller=_CALLER,
        config=_config(),
        connection_id=_CONNECTION,
    )
    assert resolved == {}


def test_resolve_rejects_a_request_state_handed_to_a_different_agent_session():
    """TODO.md item 151's concrete MRTR scenario: a `request_state` built for
    one agent session (bound to `_CALLER`'s subject) must not resolve when a
    DIFFERENT principal's session retries the identical tool call with it,
    even though the fingerprint still matches — this is exactly what
    `RequestStateSecurity.bind_principal` would cover in the SDK's own auth
    layer, which QueryGate cannot use (see `mcp/elicitation.py`'s docstring),
    so this claim-based check is the enforcement point instead."""
    fp = query_fingerprint(_QUERY)
    ctx = _ctx(request_state=_pending_state(fp), responses={"q0": _accept(True)})
    resolved = resolve_approval_tokens_from_retry(
        ctx=ctx,
        fingerprints_by_key={"q0": fp},
        caller=_OTHER_CALLER,  # a different principal than the pending state was bound to
        config=_config(),
        connection_id=_CONNECTION,
    )
    assert resolved == {}


def test_resolve_rejects_a_request_state_replayed_against_a_different_connection():
    """The connection-binding sibling of the session-hijack test above: a
    pending state built for one connection must not resolve when the retry
    targets a different connection with the byte-identical query."""
    fp = query_fingerprint(_QUERY)
    ctx = _ctx(request_state=_pending_state(fp), responses={"q0": _accept(True)})
    resolved = resolve_approval_tokens_from_retry(
        ctx=ctx,
        fingerprints_by_key={"q0": fp},
        caller=_CALLER,
        config=_config(),
        connection_id=_OTHER_CONNECTION,  # different connection than the pending state was bound to
    )
    assert resolved == {}


def test_resolve_fails_closed_on_malformed_request_state():
    ctx = _ctx(request_state="not valid json", responses={"q0": _accept(True)})
    resolved = resolve_approval_tokens_from_retry(
        ctx=ctx,
        fingerprints_by_key={"q0": query_fingerprint(_QUERY)},
        caller=_CALLER,
        config=_config(),
        connection_id=_CONNECTION,
    )
    assert resolved == {}


def test_resolve_fails_closed_on_forged_pending_token():
    fp = query_fingerprint(_QUERY)
    forged = json.dumps(
        {
            "q0": issue_approval_token(
                fingerprint=fp,
                approver_subject="x",
                key="wrong-key",
                connection_id=None,
                principal_subject=None,
            )
        }
    )
    ctx = _ctx(request_state=forged, responses={"q0": _accept(True)})
    resolved = resolve_approval_tokens_from_retry(
        ctx=ctx,
        fingerprints_by_key={"q0": fp},
        caller=_CALLER,
        config=_config(),
        connection_id=_CONNECTION,
    )
    assert resolved == {}


def test_resolve_fails_closed_on_expired_pending_token():
    fp = query_fingerprint(_QUERY)
    expired = json.dumps(
        {
            "q0": issue_approval_token(
                fingerprint=fp,
                approver_subject="mcp:pending-elicitation",
                key=_KEY,
                connection_id=_CONNECTION,
                principal_subject=_CALLER.subject,
                kind=TOKEN_KIND_PENDING,
                ttl_seconds=1,
                now=1000.0,
            )
        }
    )
    ctx = _ctx(request_state=expired, responses={"q0": _accept(True)})
    resolved = resolve_approval_tokens_from_retry(
        ctx=ctx,
        fingerprints_by_key={"q0": fp},
        caller=_CALLER,
        config=_config(),
        connection_id=_CONNECTION,
    )
    assert resolved == {}


def test_resolve_mints_separately_attributed_grants_for_a_multi_item_batch():
    fp0, fp1 = query_fingerprint(_QUERY), query_fingerprint(_OTHER_QUERY)
    state = json.dumps(
        {
            "q0": issue_approval_token(
                fingerprint=fp0,
                approver_subject="mcp:pending-elicitation",
                key=_KEY,
                connection_id=_CONNECTION,
                principal_subject=_CALLER.subject,
                kind=TOKEN_KIND_PENDING,
            ),
            "q2": issue_approval_token(
                fingerprint=fp1,
                approver_subject="mcp:pending-elicitation",
                key=_KEY,
                connection_id=_CONNECTION,
                principal_subject=_CALLER.subject,
                kind=TOKEN_KIND_PENDING,
            ),
        }
    )
    ctx = _ctx(request_state=state, responses={"q0": _accept(True), "q2": _decline()})
    resolved = resolve_approval_tokens_from_retry(
        ctx=ctx,
        fingerprints_by_key={"q0": fp0, "q2": fp1},
        caller=_CALLER,
        config=_config(),
        connection_id=_CONNECTION,
    )
    assert fp0 in resolved and fp1 not in resolved


def test_resolve_does_not_grant_a_declined_duplicate_slot():
    """`resolved` is keyed by fingerprint, not by slot. If the same statement
    appears twice in one batch (duplicate fingerprint at two keys) and the
    human approves one occurrence but declines the other, the decline must
    veto the fingerprint entirely — `resolved` is a fingerprint->token map
    consumed by execute_many's per-statement lookup, so a grant surviving
    here would let BOTH slots execute even though one was explicitly
    declined."""
    fp = query_fingerprint(_QUERY)
    state = json.dumps(
        {
            "q0": issue_approval_token(
                fingerprint=fp,
                approver_subject="mcp:pending-elicitation",
                key=_KEY,
                connection_id=None,
                principal_subject=None,
                kind=TOKEN_KIND_PENDING,
            ),
            "q1": issue_approval_token(
                fingerprint=fp,
                approver_subject="mcp:pending-elicitation",
                key=_KEY,
                connection_id=None,
                principal_subject=None,
                kind=TOKEN_KIND_PENDING,
            ),
        }
    )
    ctx = _ctx(request_state=state, responses={"q0": _accept(True), "q1": _decline()})
    resolved = resolve_approval_tokens_from_retry(
        ctx=ctx,
        fingerprints_by_key={"q0": fp, "q1": fp},
        caller=_CALLER,
        config=_config(),
        connection_id=_CONNECTION,
    )
    assert resolved == {}


# --------------------------------------------------------------------------- #
# The execute_many approval_tokens seam the resolved grants plug into
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_execute_many_admits_a_pre_supplied_approval_token(monkeypatch):
    monkeypatch.setattr(svc.app_config, "approval_token_hmac_key", _KEY)
    set_policy_store(PolicyStore(default=Policy(approval_max_estimated_rows=100), overrides={}))
    table = sa.Table("orders", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True))
    query = StructuredQuery(from_table="orders", select=["orders.id"], limit=10)

    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = [{"id": 1}]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    estimate = QueryCostEstimate(estimated_rows=500, estimated_total_cost=None)

    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"orders": table})),
        patch.object(svc, "session_scope", _scope),
        patch.object(svc, "estimate_postgres_query_cost", AsyncMock(return_value=estimate)),
    ):
        service = StructuredQueryService(connection_id="demo")
        no_token = await service.execute_many([query])
        fp = query_fingerprint(query)
        token = issue_approval_token(
            fingerprint=fp,
            approver_subject="mcp-elicitation:agent-1",
            key=_KEY,
            connection_id=None,
            principal_subject=None,
        )
        with_token = await service.execute_many([query], approval_tokens={fp: token})

    assert no_token[0].error is not None
    assert no_token[0].approval_fingerprint == fp
    assert no_token[0].approval_reasons
    assert with_token[0].row_count == 1 and with_token[0].error is None


# --------------------------------------------------------------------------- #
# Tool wiring: first call surfaces InputRequiredResult; retry resolves it
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tool_returns_input_required_on_first_gated_call(monkeypatch):
    monkeypatch.setattr(qtool, "get_mcp_caller", lambda: _CALLER)
    monkeypatch.setattr(qtool, "validate_batch_size", lambda *a, **k: None)
    monkeypatch.setattr(qtool, "get_policy", lambda *a, **k: Policy())
    monkeypatch.setattr(qtool, "get_mcp_config", _config)

    fp = query_fingerprint(_QUERY)
    from querygate.execution.service import BatchQueryItemResult

    gated_result = BatchQueryItemResult(
        error="approval required", approval_fingerprint=fp, approval_reasons=["big estimate"]
    )

    async def _fake_execute_many(queries, **kwargs):
        assert kwargs.get("approval_tokens") == {}  # first call: nothing resolved yet
        return [gated_result]

    fake_service = MagicMock()
    fake_service.execute_many = _fake_execute_many

    with patch.object(qtool, "_service", return_value=fake_service):
        result = await qtool.run_structured_queries("demo", [_QUERY], ctx=_ctx())

    assert isinstance(result, InputRequiredResult)
    assert "q0" in result.input_requests


@pytest.mark.asyncio
async def test_tool_admits_the_query_on_a_resolved_retry(monkeypatch):
    monkeypatch.setattr(qtool, "get_mcp_caller", lambda: _CALLER)
    monkeypatch.setattr(qtool, "validate_batch_size", lambda *a, **k: None)
    monkeypatch.setattr(qtool, "get_policy", lambda *a, **k: Policy())
    monkeypatch.setattr(qtool, "get_mcp_config", _config)

    fp = query_fingerprint(_QUERY)
    from querygate.execution.service import BatchQueryItemResult

    async def _fake_execute_many(queries, **kwargs):
        # The retry must carry a real, verifiable grant for this fingerprint,
        # bound to this connection and to the calling principal (item 151).
        token = kwargs.get("approval_tokens", {}).get(fp)
        assert token is not None
        assert verify_approval_token(
            token,
            fingerprint=fp,
            key=_KEY,
            connection_id=_CONNECTION,
            principal_subject=_CALLER.subject,
        )
        return [
            BatchQueryItemResult(rows=[{"id": 1}], row_count=1, truncated=False, limit=10, offset=0)
        ]

    fake_service = MagicMock()
    fake_service.execute_many = _fake_execute_many

    retry_ctx = _ctx(request_state=_pending_state(fp), responses={"q0": _accept(True)})
    with patch.object(qtool, "_service", return_value=fake_service):
        result = await qtool.run_structured_queries("demo", [_QUERY], ctx=retry_ctx)

    assert not isinstance(result, InputRequiredResult)
    assert result.results[0].row_count == 1


@pytest.mark.asyncio
async def test_tool_stays_fail_closed_when_channel_disabled(monkeypatch):
    monkeypatch.setattr(qtool, "get_mcp_caller", lambda: _CALLER)
    monkeypatch.setattr(qtool, "validate_batch_size", lambda *a, **k: None)
    monkeypatch.setattr(qtool, "get_policy", lambda *a, **k: Policy())
    monkeypatch.setattr(qtool, "get_mcp_config", lambda: _config(enabled=False))

    fp = query_fingerprint(_QUERY)
    from querygate.execution.service import BatchQueryItemResult

    gated_result = BatchQueryItemResult(
        error="approval required", approval_fingerprint=fp, approval_reasons=["big estimate"]
    )

    async def _fake_execute_many(queries, **kwargs):
        assert kwargs.get("approval_tokens") == {}
        return [gated_result]

    fake_service = MagicMock()
    fake_service.execute_many = _fake_execute_many

    with patch.object(qtool, "_service", return_value=fake_service):
        result = await qtool.run_structured_queries("demo", [_QUERY], ctx=_ctx())

    # Disabled: build_pending_input_required returns None, so the tool falls
    # through to the plain (fail-closed, still-erroring) batch result.
    assert not isinstance(result, InputRequiredResult)
    assert result.results[0].error is not None


@pytest.mark.asyncio
async def test_tool_never_discards_an_already_executed_item_for_input_required(monkeypatch):
    """A batch where one item already ran for real (row_count set) and another
    is gated must NOT return InputRequiredResult: since MRTR retries resubmit
    the identical `queries` argument, treating a partially-executed batch as
    'first gated call' would silently re-run (and, for writes, re-commit) the
    item that already executed. The already-executed result must survive in
    the returned batch instead."""
    monkeypatch.setattr(qtool, "get_mcp_caller", lambda: _CALLER)
    monkeypatch.setattr(qtool, "validate_batch_size", lambda *a, **k: None)
    monkeypatch.setattr(qtool, "get_policy", lambda *a, **k: Policy())
    monkeypatch.setattr(qtool, "get_mcp_config", _config)

    other_query = StructuredQuery(from_table="orders", select=["orders.status"])
    fp = query_fingerprint(other_query)
    from querygate.execution.service import BatchQueryItemResult

    executed = BatchQueryItemResult(
        rows=[{"id": 1}], row_count=1, truncated=False, limit=10, offset=0
    )
    gated = BatchQueryItemResult(
        error="approval required", approval_fingerprint=fp, approval_reasons=["big estimate"]
    )

    async def _fake_execute_many(queries, **kwargs):
        return [executed, gated]

    fake_service = MagicMock()
    fake_service.execute_many = _fake_execute_many

    with patch.object(qtool, "_service", return_value=fake_service):
        result = await qtool.run_structured_queries("demo", [_QUERY, other_query], ctx=_ctx())

    assert not isinstance(result, InputRequiredResult)
    assert result.results[0].row_count == 1
    assert result.results[1].approval_fingerprint == fp
