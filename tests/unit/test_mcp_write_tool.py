"""MCP governed-writes tool + write batch execution (TODO.md item 93).

Covers the new `WriteExecutionService.execute_many` resolver/retry seam (the
elicitation-approval plug-in point) and the `run_structured_writes` tool's
preview-vs-execute routing and resolver wiring.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.exceptions import ApprovalRequiredError
from querygate.execution.write_execution import WriteExecutionService, WriteResult
from querygate.mcp.tools import write as wtool
from querygate.query_ast.models import Predicate
from querygate.write_ast.models import DeleteStatement

pytestmark = pytest.mark.unit

_CALLER = Principal(subject="agent-1")


def _delete(value: int) -> DeleteStatement:
    return DeleteStatement(table="orders", where=Predicate(col="orders.id", op="eq", value=value))


@pytest.mark.asyncio
async def test_execute_many_admits_after_resolver_grants_and_errors_on_decline():
    svc = WriteExecutionService("demo")
    seen_tokens = []

    async def _fake_execute(statement, *, approval_token=None):
        seen_tokens.append(approval_token)
        if approval_token is None:  # first attempt trips the gate
            raise ApprovalRequiredError("needs approval", fingerprint="fp", reasons=["big write"])
        return WriteResult(operation="delete", table="orders", affected_rows=3)

    svc.execute = _fake_execute  # type: ignore[assignment]

    async def _granting(statement, exc):
        return "a-valid-token"

    granted = await svc.execute_many([_delete(1)], approval_resolver=_granting)
    assert granted[0].executed is True and granted[0].affected_rows == 3
    assert seen_tokens == [None, "a-valid-token"]  # tripped, then retried with the token

    async def _declining(statement, exc):
        return None

    declined = await svc.execute_many([_delete(2)], approval_resolver=_declining)
    assert declined[0].error is not None and declined[0].executed is False


@pytest.mark.asyncio
async def test_execute_many_one_failure_does_not_drop_the_rest():
    svc = WriteExecutionService("demo")

    async def _fake_execute(statement, *, approval_token=None):
        if statement.where.value == 1:
            raise ValueError("boom")
        return WriteResult(operation="delete", table="orders", affected_rows=1)

    svc.execute = _fake_execute  # type: ignore[assignment]
    results = await svc.execute_many([_delete(1), _delete(2)])
    assert results[0].error is not None
    assert results[1].executed is True and results[1].error is None


@pytest.mark.asyncio
async def test_tool_preview_mode_never_executes(monkeypatch):
    monkeypatch.setattr(wtool, "get_mcp_caller", lambda: _CALLER)
    preview_service = MagicMock()
    preview_service.preview = AsyncMock(return_value=MagicMock())
    exec_service = MagicMock()
    exec_service.execute_many = AsyncMock(return_value=[])

    with (
        patch.object(wtool, "WritePreviewService", return_value=preview_service),
        patch.object(wtool, "WriteExecutionService", return_value=exec_service) as exec_cls,
    ):
        await wtool.run_structured_writes("demo", [_delete(1)], mode="preview", ctx=None)

    preview_service.preview.assert_awaited_once()
    exec_cls.assert_not_called()  # preview never touches the execution service


@pytest.mark.asyncio
async def test_tool_execute_mode_passes_resolver_only_when_enabled(monkeypatch):
    monkeypatch.setattr(wtool, "get_mcp_caller", lambda: _CALLER)
    captured = {}

    async def _fake_execute_many(writes, **kwargs):
        captured["resolver"] = kwargs.get("approval_resolver")
        return []

    exec_service = MagicMock()
    exec_service.execute_many = _fake_execute_many

    def _config(enabled):
        return AppConfig(
            environment="localhost",
            mcp_elicitation_approval_enabled=enabled,
            approval_token_hmac_key="k",
        )

    with patch.object(wtool, "WriteExecutionService", return_value=exec_service):
        # ctx present + operator opted in -> a resolver is built and passed.
        monkeypatch.setattr(wtool, "get_mcp_config", lambda: _config(True))
        await wtool.run_structured_writes("demo", [_delete(1)], mode="execute", ctx=MagicMock())
        assert captured["resolver"] is not None

        # Operator opted out -> no resolver (gated writes stay fail-closed).
        monkeypatch.setattr(wtool, "get_mcp_config", lambda: _config(False))
        await wtool.run_structured_writes("demo", [_delete(1)], mode="execute", ctx=MagicMock())
        assert captured["resolver"] is None

        # No ctx (client without an interactive channel) -> no resolver.
        monkeypatch.setattr(wtool, "get_mcp_config", lambda: _config(True))
        await wtool.run_structured_writes("demo", [_delete(1)], mode="execute", ctx=None)
        assert captured["resolver"] is None


@pytest.mark.asyncio
async def test_undo_tool_calls_service_undo(monkeypatch):
    from querygate.execution.write_execution import WriteResult

    monkeypatch.setattr(wtool, "get_mcp_caller", lambda: _CALLER)
    exec_service = MagicMock()
    exec_service.undo = AsyncMock(
        return_value=WriteResult(operation="undo_delete", table="orders", affected_rows=2)
    )
    with patch.object(wtool, "WriteExecutionService", return_value=exec_service):
        result = await wtool.undo_structured_write("demo", "cid-123")
    exec_service.undo.assert_awaited_once_with("cid-123")
    assert result.operation == "undo_delete" and result.affected_rows == 2
