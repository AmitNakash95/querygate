"""MCP governed-writes tool + write batch execution (TODO.md item 93).

Covers the new `WriteExecutionService.execute_many` resolver/retry seam (the
elicitation-approval plug-in point) and the `run_structured_writes` tool's
preview-vs-execute routing and resolver wiring.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import ElicitResult, InputRequiredResult

from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.exceptions import ApprovalRequiredError
from querygate.execution.approval import (
    TOKEN_KIND_PENDING,
    issue_approval_token,
    write_fingerprint,
)
from querygate.execution.write_execution import (
    WriteBatchItemResult,
    WriteExecutionService,
    WriteResult,
)
from querygate.mcp.tools import write as wtool
from querygate.write_ast.models import DeleteStatement, WritePredicate

pytestmark = pytest.mark.unit

_CALLER = Principal(subject="agent-1")


def _delete(value: int) -> DeleteStatement:
    return DeleteStatement(
        table="orders", where=WritePredicate(col="orders.id", op="eq", value=value)
    )


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


def _config(enabled: bool) -> AppConfig:
    return AppConfig(
        environment="localhost",
        mcp_elicitation_approval_enabled=enabled,
        approval_token_hmac_key="k",
    )


def _ctx(*, request_state=None, responses=None) -> MagicMock:
    ctx = MagicMock()
    ctx.request_state = request_state
    ctx.input_responses = responses or {}
    return ctx


@pytest.mark.asyncio
async def test_tool_returns_input_required_on_first_gated_call(monkeypatch):
    """TODO.md item 128: a gated write's first call (no prior request_state)
    must surface InputRequiredResult, not a bare error, when the operator
    opted in and a real interactive ctx is present."""
    monkeypatch.setattr(wtool, "get_mcp_caller", lambda: _CALLER)
    fp = write_fingerprint(_delete(1))
    gated = WriteBatchItemResult(
        error="needs approval", approval_fingerprint=fp, approval_reasons=["big write"]
    )

    async def _fake_execute_many(writes, **kwargs):
        assert kwargs.get("approval_tokens") == {}
        return [gated]

    exec_service = MagicMock()
    exec_service.execute_many = _fake_execute_many

    with patch.object(wtool, "WriteExecutionService", return_value=exec_service):
        monkeypatch.setattr(wtool, "get_mcp_config", lambda: _config(True))
        result = await wtool.run_structured_writes("demo", [_delete(1)], mode="execute", ctx=_ctx())

    assert isinstance(result, InputRequiredResult)
    assert "w0" in result.input_requests


@pytest.mark.asyncio
async def test_tool_admits_the_write_on_a_resolved_retry(monkeypatch):
    monkeypatch.setattr(wtool, "get_mcp_caller", lambda: _CALLER)
    fp = write_fingerprint(_delete(1))
    ok = WriteBatchItemResult(operation="delete", table="orders", affected_rows=1, executed=True)

    async def _fake_execute_many(writes, **kwargs):
        token = kwargs.get("approval_tokens", {}).get(fp)
        assert token is not None
        return [ok]

    exec_service = MagicMock()
    exec_service.execute_many = _fake_execute_many

    pending = json.dumps(
        {
            "w0": issue_approval_token(
                fingerprint=fp,
                approver_subject="mcp:pending-elicitation",
                key="k",
                connection_id=None,
                principal_subject=None,
                kind=TOKEN_KIND_PENDING,
            )
        }
    )
    retry_ctx = _ctx(
        request_state=pending,
        responses={"w0": ElicitResult(action="accept", content={"approve": True})},
    )

    with patch.object(wtool, "WriteExecutionService", return_value=exec_service):
        monkeypatch.setattr(wtool, "get_mcp_config", lambda: _config(True))
        result = await wtool.run_structured_writes(
            "demo", [_delete(1)], mode="execute", ctx=retry_ctx
        )

    assert not isinstance(result, InputRequiredResult)
    assert result.results[0].executed is True


@pytest.mark.asyncio
async def test_tool_stays_fail_closed_when_channel_disabled(monkeypatch):
    monkeypatch.setattr(wtool, "get_mcp_caller", lambda: _CALLER)
    fp = write_fingerprint(_delete(1))
    gated = WriteBatchItemResult(
        error="needs approval", approval_fingerprint=fp, approval_reasons=["big write"]
    )

    async def _fake_execute_many(writes, **kwargs):
        assert kwargs.get("approval_tokens") == {}
        return [gated]

    exec_service = MagicMock()
    exec_service.execute_many = _fake_execute_many

    with patch.object(wtool, "WriteExecutionService", return_value=exec_service):
        # Operator opted out -> stays fail-closed, no InputRequiredResult.
        monkeypatch.setattr(wtool, "get_mcp_config", lambda: _config(False))
        result = await wtool.run_structured_writes("demo", [_delete(1)], mode="execute", ctx=_ctx())
        assert not isinstance(result, InputRequiredResult)
        assert result.results[0].error is not None

        # No ctx (client without an interactive channel) -> same fail-closed shape.
        monkeypatch.setattr(wtool, "get_mcp_config", lambda: _config(True))
        result = await wtool.run_structured_writes("demo", [_delete(1)], mode="execute", ctx=None)
        assert not isinstance(result, InputRequiredResult)
        assert result.results[0].error is not None


@pytest.mark.asyncio
async def test_atomic_mode_never_builds_input_required(monkeypatch):
    """Atomic writes have no per-item token channel — fails closed on a gated
    write the same way it always has, never pausing for elicitation."""
    monkeypatch.setattr(wtool, "get_mcp_caller", lambda: _CALLER)
    fp = write_fingerprint(_delete(1))
    gated = WriteBatchItemResult(
        error="needs approval", approval_fingerprint=fp, approval_reasons=["big write"]
    )

    async def _fake_execute_many(writes, **kwargs):
        assert "approval_tokens" not in kwargs or kwargs["approval_tokens"] == {}
        return [gated]

    exec_service = MagicMock()
    exec_service.execute_many = _fake_execute_many

    with patch.object(wtool, "WriteExecutionService", return_value=exec_service):
        monkeypatch.setattr(wtool, "get_mcp_config", lambda: _config(True))
        result = await wtool.run_structured_writes(
            "demo", [_delete(1)], mode="execute", atomic=True, ctx=_ctx()
        )

    assert not isinstance(result, InputRequiredResult)


@pytest.mark.asyncio
async def test_a_batch_with_one_committed_write_never_returns_input_required(monkeypatch):
    """If one write in the batch already committed (its own transaction, non-
    atomic mode) and another is gated, the tool must NOT return
    InputRequiredResult: MRTR retries resubmit the identical `writes`
    argument, so treating this as 'first gated call' would re-run execute_many
    on retry and commit the already-committed write a second time. The
    committed result must survive in the returned batch instead, and the
    gated item stays fail-closed (its existing REST-compatible error) rather
    than pausing for elicitation."""
    monkeypatch.setattr(wtool, "get_mcp_caller", lambda: _CALLER)
    fp = write_fingerprint(_delete(2))
    committed = WriteBatchItemResult(
        operation="delete", table="orders", affected_rows=1, executed=True
    )
    gated = WriteBatchItemResult(
        error="needs approval", approval_fingerprint=fp, approval_reasons=["big write"]
    )

    async def _fake_execute_many(writes, **kwargs):
        return [committed, gated]

    exec_service = MagicMock()
    exec_service.execute_many = _fake_execute_many

    with patch.object(wtool, "WriteExecutionService", return_value=exec_service):
        monkeypatch.setattr(wtool, "get_mcp_config", lambda: _config(True))
        result = await wtool.run_structured_writes(
            "demo", [_delete(1), _delete(2)], mode="execute", ctx=_ctx()
        )

    assert not isinstance(result, InputRequiredResult)
    assert result.results[0].executed is True
    assert result.results[1].approval_fingerprint == fp


# --------------------------------------------------------------------------- #
# The write CONTRACT (TODO.md item 114): the schema must advertise exactly what
# a write accepts. Advertising a field the server refuses invites an agent to
# build a write it will be denied — the "hit a wall, route around the gate"
# failure the engine plan exists to prevent — and spends agent context to do it.
# --------------------------------------------------------------------------- #
_READ_ONLY_DEFINITIONS = (
    "value_subquery",  # item 97/110 — rejected on the write path
    "BinaryOpExpr",  # item 100's Expression union
    "FunctionExpr",
    "CaseExpr",
    "CastExpr",
    "WindowSelectItem",  # item 101
    "WindowSpec",
    "AggregateSelectItem",
    "StructuredQuery",  # the whole read AST, pulled in by value_subquery
)


def _write_tool_schema() -> str:
    import json

    from querygate.mcp.server import create_mcp_server

    tools = create_mcp_server()._tool_manager._tools
    return json.dumps(tools["run_structured_writes"].parameters)


@pytest.mark.parametrize("definition", _READ_ONLY_DEFINITIONS)
def test_write_tool_schema_advertises_no_read_only_definition(definition):
    assert definition not in _write_tool_schema(), (
        f"{definition} is inlined into the write tool's schema but the write path "
        "rejects it — see TODO.md item 114"
    )


def test_write_tool_schema_definitions_are_an_exact_set():
    """An allowlist, not just the denylist above: this catches the NEXT read-only
    node (item 102's date/interval, item 104's set ops) instead of only the nine
    named today, and it catches an accidental re-widening in one assertion."""
    import json

    from querygate.mcp.server import create_mcp_server

    tools = create_mcp_server()._tool_manager._tools
    schema = json.loads(json.dumps(tools["run_structured_writes"].parameters))
    assert set(schema["$defs"]) == {
        "ColumnExpr",  # a col_fn argument
        "LiteralExpr",  # a col_fn argument
        "ScalarFunctionCall",  # Predicate.col_fn, deliberately kept
        "WritePredicate",
        "WriteWhereGroup",
        "InsertStatement",
        "UpdateStatement",
        "DeleteStatement",
        "UpsertStatement",
    }


def test_write_predicate_is_a_strict_narrowing_of_the_read_predicate():
    """The field-level relationship, so drift is caught in BOTH directions: a read
    field that a write should accept cannot be silently missing, and a new
    read-only field forces a decision here rather than being quietly absent."""
    from querygate.query_ast.models import Predicate
    from querygate.write_ast.models import WritePredicate

    write_fields = set(WritePredicate.model_fields)
    read_fields = set(Predicate.model_fields)
    assert write_fields < read_fields, "a write predicate field must exist on the read predicate"
    assert read_fields - write_fields == {
        "expr",
        "value_expr",
        "value_subquery",
        # Item 106. A write's WHERE selects rows to mutate; an EXISTS test over a
        # correlated subquery is a read-shaped question, and admitting it would put
        # a second query's worth of scan behind every UPDATE/DELETE row match. Kept
        # read-only deliberately, which is the decision this assertion exists to
        # force rather than to let pass silently.
        "exists_subquery",
    }, (
        "the read predicate grew or lost a field — decide whether writes accept it, "
        "then update this set (see TODO.md item 114)"
    )


def test_the_write_operator_set_is_not_widened_by_read_only_operators():
    """The other half of item 114's defect, which the field check above cannot see:
    the write tool must not ADVERTISE an operator it rejects.

    Item 106 added `exists`/`not_exists` to the read path. They live on a separate
    `ReadCompareOp` precisely so `CompareOp` — which `WritePredicate.op` uses — does
    not grow them. If a future change puts them on the shared type, the write tool's
    MCP schema starts offering an operator the write validator refuses, which is the
    exact shape item 114 was raised to fix.
    """
    import typing

    from querygate.query_ast.models import CompareOp, EXISTS_OPS, ReadCompareOp
    from querygate.write_ast.models import WritePredicate

    read_ops = set(typing.get_args(ReadCompareOp))
    write_ops = set(typing.get_args(CompareOp))
    assert write_ops < read_ops, "the read operator set must be a strict superset"
    assert read_ops - write_ops == EXISTS_OPS
    assert WritePredicate.model_fields["op"].annotation is CompareOp
    assert not (EXISTS_OPS & write_ops), "an existence test is not a write operator"


def test_write_tool_schema_still_advertises_what_writes_do_accept():
    """The narrowing must not have thrown away the real contract."""
    schema = _write_tool_schema()
    for expected in (
        "WritePredicate",
        "WriteWhereGroup",
        "value_col",
        "col_fn",
        "conflict_columns",
    ):
        assert expected in schema, expected


@pytest.mark.parametrize(
    "payload",
    [
        {"op": "insert", "table": "orders", "rows": [{"id": 1, "status": "new"}]},
        {
            "op": "update",
            "table": "orders",
            "set": {"status": "shipped"},
            "where": {"col": "orders.id", "op": "eq", "value": 1},
        },
        {
            "op": "update",
            "table": "orders",
            "set": {"status": "shipped"},
            "where": {"col": "orders.total_amount", "op": "gt", "value_col": "orders.paid_amount"},
        },
        {
            "op": "delete",
            "table": "orders",
            "where": {
                "and": [
                    {"col": "orders.status", "op": "eq", "value": "draft"},
                    {"or": [{"col": "orders.id", "op": "lt", "value": 10}]},
                    {"not": {"col": "orders.id", "op": "eq", "value": 3}},
                ]
            },
        },
        {
            "op": "delete",
            "table": "orders",
            "where": {
                "col_fn": {"fn": "lower", "args": [{"col": "orders.status"}]},
                "op": "eq",
                "value": "draft",
            },
        },
        {
            "op": "delete",
            "table": "orders",
            "where": {"col": "orders.id", "op": "between", "value": [1, 5]},
        },
        {"op": "delete", "table": "orders", "where": {"col": "orders.status", "op": "is_null"}},
        {
            "op": "delete",
            "table": "orders",
            "where": {"col": "orders.status", "op": "is_not_null"},
        },
        # in/not_in — the commonest write filter of all ("delete these ids").
        {
            "op": "delete",
            "table": "orders",
            "where": {"col": "orders.id", "op": "in", "value": [1, 2, 3]},
        },
        {
            "op": "delete",
            "table": "orders",
            "where": {"col": "orders.status", "op": "not_in", "value": ["sent", "paid"]},
        },
        {"op": "delete", "table": "orders", "where": {"col": "orders.id", "op": "neq", "value": 1}},
        {"op": "delete", "table": "orders", "where": {"col": "orders.id", "op": "lte", "value": 9}},
        {"op": "delete", "table": "orders", "where": {"col": "orders.id", "op": "gte", "value": 2}},
        {
            "op": "delete",
            "table": "orders",
            "where": {"col": "orders.status", "op": "like", "value": "draft%"},
        },
        # A top-level or/not group, and col_fn with a literal argument.
        {
            "op": "delete",
            "table": "orders",
            "where": {"or": [{"col": "orders.id", "op": "eq", "value": 1}]},
        },
        {
            "op": "delete",
            "table": "orders",
            "where": {"not": {"col": "orders.id", "op": "eq", "value": 1}},
        },
        {
            "op": "delete",
            "table": "orders",
            "where": {
                "col_fn": {
                    "fn": "coalesce",
                    "args": [{"col": "orders.status"}, {"literal": "draft"}],
                },
                "op": "eq",
                "value": "draft",
            },
        },
        {
            "op": "upsert",
            "table": "orders",
            "rows": [{"id": 1, "status": "new"}],
            "conflict_columns": ["id"],
            "update_columns": ["status"],
        },
    ],
)
def test_every_previously_valid_write_payload_still_validates(payload):
    """The narrowing is to fields the runtime already rejected, so no payload a
    caller could legitimately send may break. Round-tripped through the real
    discriminated union the transports parse."""
    import pydantic

    from querygate.write_ast.models import WriteStatement

    adapter = pydantic.TypeAdapter(WriteStatement)
    parsed = adapter.validate_python(payload)
    assert parsed.table == "orders"
    # Byte-identical round trip — the property the docs claim, asserted rather
    # than assumed: serialize back out and compare to the caller's own payload.
    assert parsed.model_dump(by_alias=True, exclude_none=True) == payload
    assert adapter.validate_python(parsed.model_dump(by_alias=True, exclude_none=True))


@pytest.mark.parametrize(
    "bad_value_shape",
    [
        {"col": "orders.id", "op": "between", "value": [1]},
        {"col": "orders.id", "op": "in", "value": []},
        {"col": "orders.id", "op": "is_null", "value": 1},
        {"col": "orders.id", "op": "like", "value_col": "orders.status"},
        {"col": "orders.id", "op": "eq"},
        {"col": "orders.id", "op": "eq", "value": 1, "value_col": "orders.status"},
        {"op": "eq", "value": 1},
        {"col": "id", "op": "eq", "value": 1},
    ],
)
def test_write_predicate_enforces_the_read_predicates_value_rules(bad_value_shape):
    """`WritePredicate` validates by building the read `Predicate`, so the
    operator/value rules are the SAME rules, not a second copy that can drift."""
    import pydantic

    from querygate.write_ast.models import DeleteStatement

    with pytest.raises(pydantic.ValidationError):
        DeleteStatement.model_validate(
            {"op": "delete", "table": "orders", "where": bad_value_shape}
        )
