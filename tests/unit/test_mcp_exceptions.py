"""Regression for TODO.md item 163's audit finding: `mcp/exceptions.py`'s
`_error_code_from_exception` had no branch for `ConfigValidationError` — a
client-actionable, explained exception (raised by `connections/engine.py`'s
`init_engine` and, as of item 163, `validation/schema_validation.py`'s
cross-connection `is_connectable()` check) that REST already maps to a plain
422 via `api/_errors.py`'s `_ACTIONABLE` tuple. On the MCP transport it fell
through the last `if` in `_error_code_from_exception` all the way to the
`"INTERNAL"` default, and `safe_mcp_tool`'s wrapper treats `"INTERNAL"` as an
unexpected fault worth a full `log.exception()` traceback — so a routine,
client-triggerable config/validation condition was reported to an MCP caller
and logged exactly like a genuine internal bug.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from querygate.core.exceptions import ConfigValidationError, QueryValidationError
from querygate.mcp.exceptions import _error_code_from_exception, safe_mcp_tool

pytestmark = pytest.mark.unit


def test_config_validation_error_maps_to_validation_not_internal():
    exc = ConfigValidationError("connection 'other' is dialect 'snowflake', not connectable")
    error_code, message = _error_code_from_exception(exc)
    assert error_code == "VALIDATION"
    assert message == str(exc)


def test_config_validation_error_and_query_validation_error_share_the_same_code():
    """Both are the identical 'client-actionable, explained rejection' shape
    (see api/_errors.py's _ACTIONABLE tuple, which maps both to REST 422) —
    the MCP mapping must not distinguish between them either."""
    config_code, _ = _error_code_from_exception(ConfigValidationError("bad config"))
    query_code, _ = _error_code_from_exception(QueryValidationError("bad query"))
    assert config_code == query_code == "VALIDATION"


@pytest.mark.asyncio
async def test_safe_mcp_tool_does_not_log_exception_for_config_validation_error(monkeypatch):
    """The behavioral half of the regression: a ConfigValidationError raised
    inside a wrapped MCP tool must produce error_code != "INTERNAL" and must
    NOT trigger safe_mcp_tool's log.exception() traceback path — that path is
    reserved for genuinely unexpected faults."""
    fake_log = MagicMock()

    class _FakeContextLogger:
        def contextualize(self, **extra):
            class _CM:
                def __enter__(self_inner):
                    return fake_log

                def __exit__(self_inner, *exc_info):
                    return False

            return _CM()

    monkeypatch.setattr("querygate.mcp.exceptions.get_logger", lambda: _FakeContextLogger())

    @safe_mcp_tool
    async def _tool():
        raise ConfigValidationError("connection 'other' is dialect 'snowflake', not connectable")

    result = await _tool()

    assert result.success is False
    assert result.error_code == "VALIDATION"
    assert "snowflake" in result.error_message
    fake_log.exception.assert_not_called()
    fake_log.error.assert_called_once()
    assert fake_log.error.call_args.kwargs.get("error_code") == "VALIDATION"


@pytest.mark.asyncio
async def test_safe_mcp_tool_still_logs_exception_for_a_genuine_internal_error(monkeypatch):
    """Sibling to the fix above: a real, unclassified exception must still
    hit the log.exception() path and report "INTERNAL" — the fix must not
    have widened the VALIDATION bucket to swallow real faults."""
    fake_log = MagicMock()

    class _FakeContextLogger:
        def contextualize(self, **extra):
            class _CM:
                def __enter__(self_inner):
                    return fake_log

                def __exit__(self_inner, *exc_info):
                    return False

            return _CM()

    monkeypatch.setattr("querygate.mcp.exceptions.get_logger", lambda: _FakeContextLogger())

    @safe_mcp_tool
    async def _tool():
        raise RuntimeError("genuinely unexpected")

    result = await _tool()

    assert result.success is False
    assert result.error_code == "INTERNAL"
    fake_log.exception.assert_called_once()
    fake_log.error.assert_not_called()
