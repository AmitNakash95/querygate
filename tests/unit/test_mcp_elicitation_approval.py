"""In-query approval via MCP elicitation (TODO.md item 92).

The interactive channel: when a query trips the approval gate over MCP, the
tool asks the client's human to approve it in-session with `Context.elicit`
instead of the out-of-band REST token flow. These tests cover the resolver's
decision logic (opt-in gating, accept/decline/error handling, token minting),
the `execute_many` retry seam it plugs into, and the tool wiring that only
builds a resolver when the operator opted in.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa

from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.exceptions import ApprovalRequiredError
from querygate.execution import service as svc
from querygate.execution.approval import query_fingerprint, verify_approval_token
from querygate.mcp.elicitation import build_elicitation_resolver
from querygate.execution.cost_estimation import QueryCostEstimate
from querygate.execution.service import StructuredQueryService
from querygate.mcp.tools import query as qtool
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.query_ast.models import StructuredQuery

pytestmark = pytest.mark.unit

_KEY = "elicit-hmac-key"
_QUERY = StructuredQuery(from_table="orders", select=["orders.id"])
_CALLER = Principal(subject="agent-1")


def _config(*, enabled: bool = True, key: str = _KEY) -> AppConfig:
    return AppConfig(
        environment="localhost",
        mcp_elicitation_approval_enabled=enabled,
        approval_token_hmac_key=key,
    )


def _elicit_result(action: str, approve: bool = False):
    result = MagicMock()
    result.action = action
    result.data = MagicMock(approve=approve)
    return result


def _ctx(action: str, approve: bool = False) -> MagicMock:
    ctx = MagicMock()
    ctx.elicit = AsyncMock(return_value=_elicit_result(action, approve))
    return ctx


def _exc() -> ApprovalRequiredError:
    return ApprovalRequiredError(
        "approval required", fingerprint=query_fingerprint(_QUERY), reasons=["estimate too large"]
    )


# --------------------------------------------------------------------------- #
# Resolver decision logic                                                       #
# --------------------------------------------------------------------------- #


def test_resolver_none_when_channel_disabled():
    assert build_elicitation_resolver(_ctx("accept", True), _CALLER, _config(enabled=False)) is None


def test_resolver_none_when_no_signing_key():
    assert build_elicitation_resolver(_ctx("accept", True), _CALLER, _config(key="")) is None


@pytest.mark.asyncio
async def test_resolver_mints_bound_token_on_approval():
    ctx = _ctx("accept", approve=True)
    resolve = build_elicitation_resolver(ctx, _CALLER, _config())
    assert resolve is not None
    token = await resolve(_QUERY, _exc())
    assert token is not None
    # The minted token verifies for exactly this query's fingerprint...
    assert verify_approval_token(token, fingerprint=query_fingerprint(_QUERY), key=_KEY)
    # ...and not for a different query.
    other = StructuredQuery(from_table="orders", select=["orders.total"])
    assert not verify_approval_token(token, fingerprint=query_fingerprint(other), key=_KEY)
    ctx.elicit.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolver_returns_none_on_decline():
    resolve = build_elicitation_resolver(_ctx("decline"), _CALLER, _config())
    assert await resolve(_QUERY, _exc()) is None


@pytest.mark.asyncio
async def test_resolver_returns_none_when_accepted_but_not_approved():
    # Client accepted the form but left approve=false — deny-by-default.
    resolve = build_elicitation_resolver(_ctx("accept", approve=False), _CALLER, _config())
    assert await resolve(_QUERY, _exc()) is None


@pytest.mark.asyncio
async def test_resolver_fails_closed_when_elicitation_raises():
    ctx = MagicMock()
    ctx.elicit = AsyncMock(side_effect=RuntimeError("client has no elicitation channel"))
    resolve = build_elicitation_resolver(ctx, _CALLER, _config())
    assert await resolve(_QUERY, _exc()) is None


# --------------------------------------------------------------------------- #
# The execute_many retry seam the resolver plugs into                           #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_execute_many_retries_after_resolver_grants_approval(monkeypatch):
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

    async def _resolver(q, exc):
        # Stand in for the human clicking "approve": mint a token bound to q.
        from querygate.execution.approval import issue_approval_token

        return issue_approval_token(fingerprint=exc.fingerprint, approver_subject="human", key=_KEY)

    declined_calls = []

    async def _declining_resolver(q, exc):
        declined_calls.append(exc.fingerprint)
        return None

    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"orders": table})),
        patch.object(svc, "session_scope", _scope),
        patch.object(svc, "estimate_postgres_query_cost", AsyncMock(return_value=estimate)),
    ):
        service = StructuredQueryService(connection_id="demo")
        granted = await service.execute_many([query], approval_resolver=_resolver)
        declined = await service.execute_many([query], approval_resolver=_declining_resolver)

    assert granted[0].row_count == 1 and granted[0].error is None
    # A resolver that declines leaves the query fail-closed as an error item.
    assert declined[0].error is not None and declined[0].row_count is None
    assert declined_calls  # the resolver was actually consulted


@pytest.mark.asyncio
async def test_approval_retry_consumes_exactly_one_quota_unit(monkeypatch):
    """TODO.md item 107: a batch item that pauses for approval and is then
    approved in-session must consume exactly ONE per-principal quota unit, not
    two. The first attempt reserves before the approval gate runs; the retry
    must reuse that reservation, not reserve again."""
    from querygate.execution.quota import in_process_quota_limiter

    monkeypatch.setattr(svc.app_config, "approval_token_hmac_key", _KEY)
    set_policy_store(
        PolicyStore(
            default=Policy(
                approval_max_estimated_rows=100,  # gate trips at estimate 500
                max_requests_per_window=5,  # request quota enabled
                quota_window_seconds=60,
            ),
            overrides={},
        )
    )
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

    async def _resolver(q, exc):
        from querygate.execution.approval import issue_approval_token

        return issue_approval_token(fingerprint=exc.fingerprint, approver_subject="human", key=_KEY)

    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"orders": table})),
        patch.object(svc, "session_scope", _scope),
        patch.object(svc, "estimate_postgres_query_cost", AsyncMock(return_value=estimate)),
    ):
        # Quota is per (connection, principal_subject) — needs an attributable caller.
        service = StructuredQueryService(connection_id="demo", principal=_CALLER)
        granted = await service.execute_many([query], approval_resolver=_resolver)

    assert granted[0].row_count == 1 and granted[0].error is None
    window = in_process_quota_limiter()._windows.get(("demo", _CALLER.subject), [])
    assert len(window) == 1, f"approval retry double-reserved quota: {len(window)} units"


# --------------------------------------------------------------------------- #
# Tool wiring: a resolver is built only when the operator opted in              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tool_passes_resolver_only_when_enabled(monkeypatch):
    monkeypatch.setattr(qtool, "get_mcp_caller", lambda: _CALLER)
    monkeypatch.setattr(qtool, "validate_batch_size", lambda *a, **k: None)
    monkeypatch.setattr(qtool, "get_policy", lambda *a, **k: Policy())

    captured = {}

    async def _fake_execute_many(queries, **kwargs):
        captured["resolver"] = kwargs.get("approval_resolver")
        return []

    fake_service = MagicMock()
    fake_service.execute_many = _fake_execute_many

    with patch.object(qtool, "_service", return_value=fake_service):
        # Enabled + ctx present -> a resolver is built and passed.
        monkeypatch.setattr(qtool, "get_mcp_config", _config)
        await qtool.run_structured_queries("demo", [_QUERY], ctx=_ctx("decline"))
        assert captured["resolver"] is not None

        # Disabled -> no resolver (fail-closed on the REST token flow).
        monkeypatch.setattr(qtool, "get_mcp_config", lambda: _config(enabled=False))
        await qtool.run_structured_queries("demo", [_QUERY], ctx=_ctx("decline"))
        assert captured["resolver"] is None

        # No ctx injected (client without a context) -> no resolver.
        monkeypatch.setattr(qtool, "get_mcp_config", _config)
        await qtool.run_structured_queries("demo", [_QUERY], ctx=None)
        assert captured["resolver"] is None
