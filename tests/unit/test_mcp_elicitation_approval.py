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
    assert build_pending_input_required(items=items, config=_config(enabled=False)) is None


def test_build_pending_none_when_no_signing_key():
    items = [("q0", query_fingerprint(_QUERY), ["big estimate"])]
    assert build_pending_input_required(items=items, config=_config(key="")) is None


def test_build_pending_none_when_no_items():
    assert build_pending_input_required(items=[], config=_config()) is None


def test_build_pending_shapes_one_input_request_per_item():
    fp = query_fingerprint(_QUERY)
    result = build_pending_input_required(
        items=[("q0", fp, ["estimate too large"])], config=_config()
    )
    assert isinstance(result, InputRequiredResult)
    assert set(result.input_requests) == {"q0"}
    assert "estimate too large" in result.input_requests["q0"].params.message
    # request_state bundles a signed pending token per key, verifiable against
    # exactly the fingerprint it was built for.
    pending = json.loads(result.request_state)
    assert set(pending) == {"q0"}
    assert verify_approval_token(pending["q0"], fingerprint=fp, key=_KEY)


def test_build_pending_covers_multiple_batch_items_in_one_round_trip():
    """TODO.md item 128: execute_many fires per batch item, so multiple gated
    queries in the same batch must surface as multiple keys in ONE
    InputRequiredResult, not one round trip per item."""
    fp0, fp1 = query_fingerprint(_QUERY), query_fingerprint(_OTHER_QUERY)
    result = build_pending_input_required(
        items=[("q0", fp0, ["r0"]), ("q2", fp1, ["r2"])], config=_config()
    )
    assert set(result.input_requests) == {"q0", "q2"}
    pending = json.loads(result.request_state)
    assert verify_approval_token(pending["q0"], fingerprint=fp0, key=_KEY)
    assert verify_approval_token(pending["q2"], fingerprint=fp1, key=_KEY)
    # Cross-key verification must fail — q0's pending token is bound to fp0 only.
    assert not verify_approval_token(pending["q0"], fingerprint=fp1, key=_KEY)


# --------------------------------------------------------------------------- #
# resolve_approval_tokens_from_retry
# --------------------------------------------------------------------------- #


def _pending_state(fp: str, *, key: str = _KEY) -> str:
    return json.dumps(
        {
            "q0": issue_approval_token(
                fingerprint=fp, approver_subject="mcp:pending-elicitation", key=key
            )
        }
    )


def test_resolve_empty_when_channel_disabled():
    fp = query_fingerprint(_QUERY)
    ctx = _ctx(request_state=_pending_state(fp), responses={"q0": _accept(True)})
    resolved = resolve_approval_tokens_from_retry(
        ctx=ctx, fingerprints_by_key={"q0": fp}, caller=_CALLER, config=_config(enabled=False)
    )
    assert resolved == {}


def test_resolve_empty_when_no_prior_state():
    fp = query_fingerprint(_QUERY)
    ctx = _ctx()  # first call: no request_state, no input_responses
    resolved = resolve_approval_tokens_from_retry(
        ctx=ctx, fingerprints_by_key={"q0": fp}, caller=_CALLER, config=_config()
    )
    assert resolved == {}


def test_resolve_mints_bound_token_on_approval():
    fp = query_fingerprint(_QUERY)
    ctx = _ctx(request_state=_pending_state(fp), responses={"q0": _accept(True)})
    resolved = resolve_approval_tokens_from_retry(
        ctx=ctx, fingerprints_by_key={"q0": fp}, caller=_CALLER, config=_config()
    )
    assert fp in resolved
    assert verify_approval_token(resolved[fp], fingerprint=fp, key=_KEY)
    # ...and not for a different query.
    other_fp = query_fingerprint(_OTHER_QUERY)
    assert not verify_approval_token(resolved[fp], fingerprint=other_fp, key=_KEY)


def test_resolve_empty_on_decline():
    fp = query_fingerprint(_QUERY)
    ctx = _ctx(request_state=_pending_state(fp), responses={"q0": _decline()})
    resolved = resolve_approval_tokens_from_retry(
        ctx=ctx, fingerprints_by_key={"q0": fp}, caller=_CALLER, config=_config()
    )
    assert resolved == {}


def test_resolve_empty_when_accepted_but_not_approved():
    fp = query_fingerprint(_QUERY)
    ctx = _ctx(request_state=_pending_state(fp), responses={"q0": _accept(False)})
    resolved = resolve_approval_tokens_from_retry(
        ctx=ctx, fingerprints_by_key={"q0": fp}, caller=_CALLER, config=_config()
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
        ctx=ctx, fingerprints_by_key={"q0": fp_b}, caller=_CALLER, config=_config()
    )
    assert resolved == {}


def test_resolve_fails_closed_on_malformed_request_state():
    ctx = _ctx(request_state="not valid json", responses={"q0": _accept(True)})
    resolved = resolve_approval_tokens_from_retry(
        ctx=ctx,
        fingerprints_by_key={"q0": query_fingerprint(_QUERY)},
        caller=_CALLER,
        config=_config(),
    )
    assert resolved == {}


def test_resolve_fails_closed_on_forged_pending_token():
    fp = query_fingerprint(_QUERY)
    forged = json.dumps(
        {"q0": issue_approval_token(fingerprint=fp, approver_subject="x", key="wrong-key")}
    )
    ctx = _ctx(request_state=forged, responses={"q0": _accept(True)})
    resolved = resolve_approval_tokens_from_retry(
        ctx=ctx, fingerprints_by_key={"q0": fp}, caller=_CALLER, config=_config()
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
                ttl_seconds=1,
                now=1000.0,
            )
        }
    )
    ctx = _ctx(request_state=expired, responses={"q0": _accept(True)})
    resolved = resolve_approval_tokens_from_retry(
        ctx=ctx, fingerprints_by_key={"q0": fp}, caller=_CALLER, config=_config()
    )
    assert resolved == {}


def test_resolve_mints_separately_attributed_grants_for_a_multi_item_batch():
    fp0, fp1 = query_fingerprint(_QUERY), query_fingerprint(_OTHER_QUERY)
    state = json.dumps(
        {
            "q0": issue_approval_token(
                fingerprint=fp0, approver_subject="mcp:pending-elicitation", key=_KEY
            ),
            "q2": issue_approval_token(
                fingerprint=fp1, approver_subject="mcp:pending-elicitation", key=_KEY
            ),
        }
    )
    ctx = _ctx(request_state=state, responses={"q0": _accept(True), "q2": _decline()})
    resolved = resolve_approval_tokens_from_retry(
        ctx=ctx, fingerprints_by_key={"q0": fp0, "q2": fp1}, caller=_CALLER, config=_config()
    )
    assert fp0 in resolved and fp1 not in resolved


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
            fingerprint=fp, approver_subject="mcp-elicitation:agent-1", key=_KEY
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
        # The retry must carry a real, verifiable grant for this fingerprint.
        token = kwargs.get("approval_tokens", {}).get(fp)
        assert token is not None
        assert verify_approval_token(token, fingerprint=fp, key=_KEY)
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
