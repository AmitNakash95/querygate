"""MCP tool for governed writes — the only way an agent mutates data over MCP.

Mirror of `run_structured_queries` for the write path (TODO.md item 93): a
typed INSERT/UPDATE/DELETE AST only, never raw DML. `mode='preview'` runs the
non-mutating dry-run (optionally the bounded old→new diff); `mode='execute'`
commits each write in its own transaction, gated by WritePolicy (deny-by-default),
the affected-row cap, and — over MCP — the in-session elicitation approval
channel for a write above the policy's approval threshold.

Note: like the other tool modules, deliberately does NOT use
`from __future__ import annotations` — see mcp/tools/connections.py.
"""

from typing import Annotated, Dict, List, Literal, Optional, Union

from mcp.server.mcpserver import Context
from mcp.types import InputRequiredResult
from pydantic import BaseModel, Field

from querygate.execution.approval import write_fingerprint
from querygate.execution.write_execution import (
    WriteBatchItemResult,
    WriteExecutionService,
)
from querygate.execution.write_preview import WritePreview, WritePreviewService
from querygate.mcp.auth import get_mcp_caller, get_mcp_config
from querygate.mcp.elicitation import (
    build_pending_input_required,
    resolve_approval_tokens_from_retry,
)
from querygate.mcp.exceptions import MCPErrorResult, safe_mcp_tool
from querygate.mcp.server import mcp_server
from querygate.policy.loader import get_policy
from querygate.validation.write_policy_validation import validate_write_batch_size
from querygate.write_ast.models import (
    DeleteStatement,
    InsertStatement,
    UpdateStatement,
    UpsertStatement,
)

_WriteStatement = Annotated[
    Union[InsertStatement, UpdateStatement, DeleteStatement, UpsertStatement],
    Field(discriminator="op"),
]

# TODO.md item 130: `x-mcp-header` annotation, mirrored into
# `Mcp-Param-Connection` for a fronting gateway — see mcp/tools/query.py's
# `_CONNECTION_FIELD` for the full rationale and the join-connection caveat.
_CONNECTION_FIELD = Field(
    description="Connection id from list_connections.",
    json_schema_extra={"x-mcp-header": "Connection"},
)


class WritePreviewBatchResult(BaseModel):
    results: List[WritePreview]


class WriteExecuteBatchResult(BaseModel):
    results: List[WriteBatchItemResult]


@mcp_server.tool(
    description=(
        "Preview or execute one or more governed writes (INSERT/UPDATE/DELETE) against one "
        "connection — always pass writes as a list; results are a list in the same order, one "
        "item per write, and one failing write never drops the others (check each item's "
        "'error'). Raw SQL/DML is NOT accepted: a write is a typed AST "
        "({op: insert|update|delete, table, ...}); an UPDATE/DELETE MUST carry a 'where' (an "
        "unqualified UPDATE/DELETE cannot be expressed). mode='preview' (default) mutates "
        "nothing and reports the affected-row count + parameterized SQL; pass include_diff=true "
        "to also get the bounded old→new row diff (exactly which rows change and how), computed "
        "by running the DML in a rolled-back transaction. mode='execute' commits each write in "
        "its own transaction, but only if writes are enabled by policy for the target table/op "
        "and the affected-row count is within the policy cap. A write above the policy's "
        "approval threshold pauses for a human: if the client supports elicitation and the "
        "operator enabled it, you'll be asked to approve in-session; otherwise it is rejected "
        "with an error to obtain approval out-of-band. Writes are deny-by-default: a read-only "
        "deployment returns a clean policy rejection. Example: "
        '{"op": "update", "table": "orders", "set": {"status": "shipped"}, '
        '"where": {"col": "orders.id", "op": "eq", "value": 42}}'
    )
)
@safe_mcp_tool
async def run_structured_writes(
    connection: Annotated[str, _CONNECTION_FIELD],
    writes: Annotated[
        List[_WriteStatement],
        Field(min_length=1, description="One or more write ASTs to preview or execute, in order."),
    ],
    mode: Annotated[
        Literal["preview", "execute"],
        Field(description="'preview' mutates nothing (dry run); 'execute' commits each write."),
    ] = "preview",
    include_diff: Annotated[
        bool,
        Field(description="In preview mode, also return the bounded old→new row diff."),
    ] = False,
    atomic: Annotated[
        bool,
        Field(
            description=(
                "execute mode only. false (default): each write is its own transaction, one "
                "failure doesn't drop the rest. true: all-or-nothing — every write runs in one "
                "transaction and any failure rolls the whole batch back."
            )
        ),
    ] = False,
    ctx: Context = None,
) -> Union[WritePreviewBatchResult, WriteExecuteBatchResult, InputRequiredResult, MCPErrorResult]:
    caller = get_mcp_caller()
    # Cap the batch before ANY statement is validated, compiled, previewed, or
    # run (item 109) — the read path's `validate_batch_size` equivalent, which
    # the write path was missing entirely.
    validate_write_batch_size(len(writes), get_policy(connection, principal=caller))
    if mode == "preview":
        preview_service = WritePreviewService(connection_id=connection, principal=caller)
        previews = [await preview_service.preview(w, include_diff=include_diff) for w in writes]
        return WritePreviewBatchResult(results=previews)

    service = WriteExecutionService(connection_id=connection, principal=caller, surface="mcp")
    # In-session human approval for a gated write (item 92 machinery, item 93,
    # ported to MRTR by item 128): opt-in and off by default. When
    # unavailable, a gated write stays fail-closed as that item's error.
    # Atomic mode fails closed on a gated write instead (no per-item token
    # channel there — unchanged from before this port).
    config = get_mcp_config()
    approval_tokens: Dict[str, str] = {}
    if ctx is not None and not atomic:
        fingerprints_by_key = {f"w{i}": write_fingerprint(w) for i, w in enumerate(writes)}
        approval_tokens = resolve_approval_tokens_from_retry(
            ctx=ctx,
            fingerprints_by_key=fingerprints_by_key,
            caller=caller,
            config=config,
            connection_id=connection,
        )
    results = await service.execute_many(writes, approval_tokens=approval_tokens, atomic=atomic)
    # Only offer in-session approval when NOTHING in the batch has actually
    # committed yet. Each non-atomic item is its own already-committed
    # transaction; MRTR's retry necessarily resubmits the identical `writes`
    # argument, so once any item has run, returning InputRequiredResult here
    # would re-run (and re-commit) it a second time on retry. A gated item in
    # a partially-executed batch instead stays fail-closed with its existing
    # approval_fingerprint/approval_reasons error, the same as when the
    # channel is disabled.
    if ctx is not None and not atomic and not any(r.executed for r in results):
        pending = [
            (f"w{i}", r.approval_fingerprint, r.approval_reasons or [])
            for i, r in enumerate(results)
            if r.approval_fingerprint is not None
        ]
        input_required = build_pending_input_required(
            items=pending, config=config, caller=caller, connection_id=connection
        )
        if input_required is not None:
            return input_required
    return WriteExecuteBatchResult(results=results)
