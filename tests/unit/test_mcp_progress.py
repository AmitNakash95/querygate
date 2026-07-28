"""MCP progress notifications during a concurrency-slot wait (TODO.md item 35
phase 3): `run_structured_queries` builds `on_wait_start`/`on_admitted` hooks
that call `Context.report_progress` only when a `Context` was actually
supplied — unconditional and additive, never a new required capability.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.mcp.tools import query as qtool
from querygate.policy.models import Policy
from querygate.query_ast.models import StructuredQuery

pytestmark = pytest.mark.unit

_CALLER = Principal(subject="agent-1")
_QUERY = StructuredQuery(from_table="orders", select=["orders.id"])


def _config() -> AppConfig:
    return AppConfig(environment="localhost", mcp_elicitation_approval_enabled=False)


def _ctx_with_progress() -> MagicMock:
    ctx = MagicMock()
    ctx.report_progress = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_hooks_are_built_only_when_ctx_is_present(monkeypatch):
    monkeypatch.setattr(qtool, "get_mcp_caller", lambda: _CALLER)
    monkeypatch.setattr(qtool, "validate_batch_size", lambda *a, **k: None)
    monkeypatch.setattr(qtool, "get_policy", lambda *a, **k: Policy())
    monkeypatch.setattr(qtool, "get_mcp_config", _config)

    captured = {}

    async def _fake_execute_many(queries, **kwargs):
        captured["on_wait_start"] = kwargs.get("on_wait_start")
        captured["on_admitted"] = kwargs.get("on_admitted")
        return []

    fake_service = MagicMock()
    fake_service.execute_many = _fake_execute_many

    with patch.object(qtool, "_service", return_value=fake_service):
        await qtool.run_structured_queries("demo", [_QUERY], ctx=_ctx_with_progress())
        assert captured["on_wait_start"] is not None
        assert captured["on_admitted"] is not None

        await qtool.run_structured_queries("demo", [_QUERY], ctx=None)
        assert captured["on_wait_start"] is None
        assert captured["on_admitted"] is None


@pytest.mark.asyncio
async def test_on_wait_start_reports_progress_with_a_waiting_message(monkeypatch):
    monkeypatch.setattr(qtool, "get_mcp_caller", lambda: _CALLER)
    monkeypatch.setattr(qtool, "validate_batch_size", lambda *a, **k: None)
    monkeypatch.setattr(qtool, "get_policy", lambda *a, **k: Policy())
    monkeypatch.setattr(qtool, "get_mcp_config", _config)

    captured = {}

    async def _fake_execute_many(queries, **kwargs):
        captured["on_wait_start"] = kwargs["on_wait_start"]
        captured["on_admitted"] = kwargs["on_admitted"]
        return []

    fake_service = MagicMock()
    fake_service.execute_many = _fake_execute_many
    ctx = _ctx_with_progress()

    with patch.object(qtool, "_service", return_value=fake_service):
        await qtool.run_structured_queries("demo", [_QUERY], ctx=ctx)

    await captured["on_wait_start"](8.0)
    ctx.report_progress.assert_awaited_once()
    progress, total, message = ctx.report_progress.await_args.args
    assert progress == 0
    assert total == 8.0
    assert "waiting" in message

    ctx.report_progress.reset_mock()
    await captured["on_admitted"](150)
    ctx.report_progress.assert_awaited_once()
    progress, total, message = ctx.report_progress.await_args.args
    assert progress == pytest.approx(0.15)
    assert "admitted" in message


@pytest.mark.asyncio
async def test_run_structured_queries_rejects_async_queue_mode(monkeypatch):
    """Audit fix (item 35 phase 3 re-audit): the 202/poll/cancel async
    admission lifecycle is REST-only, implemented only by POST .../query.
    Without this rejection, queue_mode=async here would silently execute
    synchronously instead of surfacing an error, since resolve_wait_seconds
    treats async identically to wait for the wait-ceiling calculation."""
    monkeypatch.setattr(qtool, "get_mcp_caller", lambda: _CALLER)
    monkeypatch.setattr(qtool, "validate_batch_size", lambda *a, **k: None)
    monkeypatch.setattr(qtool, "get_policy", lambda *a, **k: Policy())
    monkeypatch.setattr(qtool, "get_mcp_config", _config)

    fake_service = MagicMock()
    fake_service.execute_many = AsyncMock()

    with patch.object(qtool, "_service", return_value=fake_service):
        result = await qtool.run_structured_queries("demo", [_QUERY], queue_mode="async")

    assert result.success is False
    assert result.error_code == "VALIDATION"
    assert "only supported on POST /{connection}/query" in result.error_message
    fake_service.execute_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_wait_start_reports_submitting_when_wait_is_zero(monkeypatch):
    """`queue_mode=fail_fast` (or an otherwise-empty queue) resolves to a
    0-second wait — the message should say "submitting", not imply an actual
    wait is about to happen."""
    monkeypatch.setattr(qtool, "get_mcp_caller", lambda: _CALLER)
    monkeypatch.setattr(qtool, "validate_batch_size", lambda *a, **k: None)
    monkeypatch.setattr(qtool, "get_policy", lambda *a, **k: Policy())
    monkeypatch.setattr(qtool, "get_mcp_config", _config)

    captured = {}

    async def _fake_execute_many(queries, **kwargs):
        captured["on_wait_start"] = kwargs["on_wait_start"]
        return []

    fake_service = MagicMock()
    fake_service.execute_many = _fake_execute_many
    ctx = _ctx_with_progress()

    with patch.object(qtool, "_service", return_value=fake_service):
        await qtool.run_structured_queries("demo", [_QUERY], ctx=ctx)

    await captured["on_wait_start"](0.0)
    _, _, message = ctx.report_progress.await_args.args
    assert "submitting" in message
