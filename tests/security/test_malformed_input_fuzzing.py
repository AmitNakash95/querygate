"""Malformed-input boundary fuzzing for the REST and MCP query surfaces
(TODO.md item 36 phase 2a).

QueryGate's entire safety argument rests on a single claim: the *only* thing a
caller can submit is a `StructuredQuery` AST that is fully validated before any
database is touched. That claim is only as strong as the validator's behavior
on input the caller *shouldn't* be able to submit. Existing suites prove the
happy path and specific attack shapes (`test_adversarial_security.py`) and
every policy-cap boundary (`test_policy_boundaries.py`, item 36 phase 1). This
file sweeps the *input-parsing/schema* boundary itself with a broad corpus of
malformed-but-plausible JSON and asserts, for both REST and MCP:

* it is rejected cleanly with a client error — never a 5xx / unhandled crash
  (the "rejects cleanly rather than 500ing" acceptance criterion), and
* the rejection body never leaks a server internal (traceback, file path,
  driver/SQLAlchemy text, or a connection-string credential) — QG-07, and
* the malformed input never reaches query execution — the core "no bad input
  reaches the database" invariant (QG-01): the `StructuredQueryService`
  execute/explain/batch methods are patched and asserted un-called.

These are all schema-invalid shapes rejected at the FastAPI/Pydantic (REST) or
FastMCP-argument (MCP) validation layer, *before* the service is ever reached —
so the assertions hold with no live database. Policy-cap boundary rejection
(over-depth, over-size, over-batch) is deliberately *not* re-tested here; that
is item 36 phase 1's `test_policy_boundaries.py`.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from querygate.api.app import create_app
from querygate.core.config import AppConfig
from querygate.execution.service import StructuredQueryResult

pytestmark = pytest.mark.security

_BASE_URL = "http://localhost"
_SERVICE = "querygate.execution.service.StructuredQueryService"

# Substrings that must NEVER appear in a client-facing error body: a leaked
# stack trace, an internal file path, a database-driver/ORM identifier, a
# raw missing-table error, or the demo connection string's own credentials
# (`postgresql+asyncpg://user:pass@localhost/demo`, seeded by tests/conftest.py).
# The default FastAPI/Pydantic 422 body legitimately echoes the caller's own
# input plus an `errors.pydantic.dev` doc URL and validator names — none of
# which are server secrets — so those are intentionally NOT in this set.
_SENSITIVE_TOKENS = (
    "Traceback (most recent call last)",
    "site-packages",
    "/Users/",
    'File "',
    "asyncpg",
    "psycopg",
    "sqlalchemy",
    "NoSuchTableError",
    "postgresql+asyncpg",
    "user:pass",
)


def _assert_no_internal_leak(text: str) -> None:
    for token in _SENSITIVE_TOKENS:
        assert token not in text, f"error body leaked internal token {token!r}: {text[:400]!r}"


def _settings(**overrides) -> AppConfig:
    base = dict(environment="localhost", mcp_enabled=False, audit_sink_backend="none")
    base.update(overrides)
    return AppConfig(**base)


def _mcp_settings(**overrides) -> AppConfig:
    base = dict(
        environment="localhost",
        mcp_enabled=True,
        mcp_api_keys=[],
        mcp_mount_path="/mcp",
        audit_sink_backend="none",
    )
    base.update(overrides)
    return AppConfig(**base)


# --- Malformed query corpus (schema-invalid; must be rejected before execution) ---
#
# Each entry is a would-be `StructuredQuery` body that Pydantic must reject at
# the validation boundary. `id`s double as the parametrize test ids.
_MALFORMED_QUERIES: dict[str, object] = {
    "body_is_array": [1, 2, 3],
    "body_is_string": "customers",
    "body_is_number": 42,
    "body_is_bool": True,
    "select_is_int": {"from": "customers", "select": 5},
    "select_is_string": {"from": "customers", "select": "customers.id"},
    "select_item_is_bad_type": {"from": "customers", "select": [12345]},
    "from_missing": {"select": ["customers.id"]},
    "from_is_null": {"from": None, "select": ["customers.id"]},
    "from_is_list": {"from": ["customers"], "select": ["customers.id"]},
    "limit_is_string": {"from": "customers", "select": ["customers.id"], "limit": "lots"},
    "limit_is_negative": {"from": "customers", "select": ["customers.id"], "limit": -1},
    "joins_is_dict": {
        "from": "customers",
        "select": ["customers.id"],
        "joins": {"table": "orders"},
    },
    "join_missing_on": {
        "from": "customers",
        "select": ["customers.id"],
        "joins": [{"table": "orders"}],
    },
    "bad_where_op": {
        "from": "customers",
        "select": ["customers.id"],
        "where": {"col": "customers.id", "op": "; DROP TABLE customers", "value": 1},
    },
    "where_group_has_both_and_col": {
        "from": "customers",
        "select": ["customers.id"],
        "where": {"and": [], "col": "customers.id", "op": "eq", "value": 1},
    },
    "order_by_bad_dir": {
        "from": "customers",
        "select": ["customers.id"],
        "order_by": [{"col": "customers.id", "dir": "sideways"}],
    },
    "aggregate_bad_fn": {
        "from": "customers",
        "select": [{"fn": "obliterate", "col": "customers.id", "as": "n"}],
    },
    "extra_unknown_field": {
        "from": "customers",
        "select": ["customers.id"],
        "totally_unknown": True,
    },
    # Window functions (item 101): the new select-item surface at the transport
    # boundary. Each of these is schema-invalid and must be refused before the
    # service is reached, not coerced into some nearby valid window.
    "window_bad_fn": {
        "from": "customers",
        "select": [{"fn": "obliterate", "over": {}, "as": "w"}],
    },
    "window_missing_alias": {
        "from": "customers",
        "select": [{"fn": "sum", "arg": {"col": "customers.id"}, "over": {}}],
    },
    "window_over_is_a_string": {
        "from": "customers",
        "select": [{"fn": "sum", "arg": {"col": "customers.id"}, "over": "everything", "as": "w"}],
    },
    "window_frame_bad_bound": {
        "from": "customers",
        "select": [
            {
                "fn": "sum",
                "arg": {"col": "customers.id"},
                "over": {
                    "order_by": [{"col": "customers.id"}],
                    "frame": {
                        "mode": "rows",
                        "start": {"bound": "sideways"},
                        "end": {"bound": "current_row"},
                    },
                },
                "as": "w",
            }
        ],
    },
    "window_frame_offset_is_a_string": {
        "from": "customers",
        "select": [
            {
                "fn": "sum",
                "arg": {"col": "customers.id"},
                "over": {
                    "order_by": [{"col": "customers.id"}],
                    "frame": {
                        "mode": "rows",
                        "start": {"bound": "preceding", "offset": "lots"},
                        "end": {"bound": "current_row"},
                    },
                },
                "as": "w",
            }
        ],
    },
    "window_ntile_without_buckets": {
        "from": "customers",
        "select": [{"fn": "ntile", "over": {"order_by": [{"col": "customers.id"}]}, "as": "w"}],
    },
}

# The no-raw-SQL invariant (QG-01) stated as its own corpus: every one of these
# smuggled fields must be rejected as an unpermitted extra field, never quietly
# ignored and never forwarded anywhere near execution.
_RAW_SQL_SMUGGLE_FIELDS = ("sql", "raw_sql", "query", "statement", "raw")


def _query_with_extra_field(field: str) -> dict:
    return {"from": "customers", "select": ["customers.id"], field: "SELECT * FROM customers"}


# Raw request bodies (not round-tripped through json.dumps on the client, so we
# can exercise shapes a normal encoder can't build): a body deep enough to trip
# the JSON parser's own recursion guard, syntactically invalid JSON, and a
# non-finite numeric literal.
def _deep_where_raw(depth: int) -> str:
    inner = '{"col":"customers.id","op":"eq","value":1}'
    return '{"and":[' * depth + inner + "]}" * depth


def _deep_query_raw(depth: int) -> str:
    return '{"from":"customers","select":["customers.id"],"where":' + _deep_where_raw(depth) + "}"


_RAW_MALFORMED_BODIES: dict[str, str] = {
    "deeply_nested_where_5000": _deep_query_raw(5000),
    "deeply_nested_where_50000": _deep_query_raw(50000),
    "invalid_json_truncated": '{"from":"customers","select":[',
    "invalid_json_trailing_comma": '{"from":"customers","select":["customers.id"],}',
    "non_finite_number": '{"from":"customers","select":["customers.id"],"limit":NaN}',
    "empty_body": "",
}


def _patched_service() -> tuple:
    """Patch every StructuredQueryService execution entrypoint with an
    AsyncMock, so a test can assert malformed input never reached any of them.
    """
    execute = patch(f"{_SERVICE}.execute", new_callable=AsyncMock)
    explain = patch(f"{_SERVICE}.explain", new_callable=AsyncMock)
    execute_many = patch(f"{_SERVICE}.execute_many", new_callable=AsyncMock)
    explain_many = patch(f"{_SERVICE}.explain_many", new_callable=AsyncMock)
    return execute, explain, execute_many, explain_many


# ---------------------------------------------------------------------------
# REST boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", list(_MALFORMED_QUERIES))
@pytest.mark.parametrize("path", ["query", "query/explain", "query/approve"])
async def test_rest_malformed_query_rejected_cleanly(case_id: str, path: str):
    payload = _MALFORMED_QUERIES[case_id]
    app = create_app(_settings())
    execute, explain, execute_many, explain_many = _patched_service()
    with execute as m_execute, explain as m_explain, execute_many, explain_many:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(f"/api/v1/demo/{path}", json=payload)

    # Clean client error, never a 5xx crash.
    assert 400 <= resp.status_code < 500, f"{case_id}/{path}: got {resp.status_code}"
    # Rejected at validation — execution never happened.
    m_execute.assert_not_awaited()
    m_explain.assert_not_awaited()
    # Parseable JSON body, no server-internal leak.
    resp.json()
    _assert_no_internal_leak(resp.text)


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", list(_MALFORMED_QUERIES))
async def test_rest_malformed_batch_rejected_cleanly(case_id: str):
    payload = {"queries": [_MALFORMED_QUERIES[case_id]]}
    app = create_app(_settings())
    execute, explain, execute_many, explain_many = _patched_service()
    with execute, explain, execute_many as m_many, explain_many:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post("/api/v1/demo/query/batch", json=payload)

    assert 400 <= resp.status_code < 500, f"{case_id}: got {resp.status_code}"
    m_many.assert_not_awaited()
    resp.json()
    _assert_no_internal_leak(resp.text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"queries": "not-a-list"},
        {"queries": {}},
        {"queries": [1, 2, 3]},
        {},  # missing `queries`
        {"queries": []},  # empty batch (min_length)
        [1, 2, 3],  # body is not an object at all
    ],
)
async def test_rest_malformed_batch_envelope_rejected_cleanly(payload: object):
    app = create_app(_settings())
    execute, explain, execute_many, explain_many = _patched_service()
    with execute, explain, execute_many as m_many, explain_many:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post("/api/v1/demo/query/batch", json=payload)

    assert 400 <= resp.status_code < 500
    m_many.assert_not_awaited()
    _assert_no_internal_leak(resp.text)


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", list(_RAW_MALFORMED_BODIES))
@pytest.mark.parametrize("path", ["query", "query/explain", "query/batch", "query/approve"])
async def test_rest_raw_malformed_body_rejected_cleanly(case_id: str, path: str):
    body = _RAW_MALFORMED_BODIES[case_id]
    app = create_app(_settings())
    execute, explain, execute_many, explain_many = _patched_service()
    with execute as m_execute, explain as m_explain, execute_many as m_many, explain_many:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(
                f"/api/v1/demo/{path}",
                content=body.encode(),
                headers={"content-type": "application/json"},
            )

    # A body that can't even be parsed (deep recursion, invalid JSON, NaN, empty)
    # must still fail as a client error, never crash the server.
    assert 400 <= resp.status_code < 500, f"{case_id}/{path}: got {resp.status_code}"
    m_execute.assert_not_awaited()
    m_explain.assert_not_awaited()
    m_many.assert_not_awaited()
    _assert_no_internal_leak(resp.text)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", _RAW_SQL_SMUGGLE_FIELDS)
@pytest.mark.parametrize("path", ["query", "query/explain", "query/approve"])
async def test_rest_raw_sql_field_is_rejected_not_ignored(field: str, path: str):
    """QG-01: there is no raw-SQL field. A smuggled `sql`/`query`/... field is
    rejected as an unpermitted extra field, never silently dropped, and never
    reaches execution."""
    app = create_app(_settings())
    execute, explain, execute_many, explain_many = _patched_service()
    with execute as m_execute, explain as m_explain, execute_many, explain_many:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(f"/api/v1/demo/{path}", json=_query_with_extra_field(field))

    assert resp.status_code == 422
    m_execute.assert_not_awaited()
    m_explain.assert_not_awaited()
    _assert_no_internal_leak(resp.text)


@pytest.mark.asyncio
@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("path", ["query", "query/explain", "query/batch"])
async def test_rest_non_finite_number_is_a_clean_422_not_a_500(literal: str, path: str):
    """Regression for TODO.md item 36 phase 2a: `NaN`/`Infinity` (accepted by
    Python's json parser, not valid JSON) in a numeric field used to make the
    validation-error response fail to encode (Starlette renders with
    `allow_nan=False`) and surface as a 500 leaking a traceback under debug.
    `_errors._request_validation` now scrubs non-finite floats, so it is a
    clean 422. Adversarially confirmed to 500 before that handler existed."""
    inner = f'{{"from":"customers","select":["customers.id"],"limit":{literal}}}'
    body = f'{{"queries":[{inner}]}}' if path == "query/batch" else inner
    app = create_app(_settings())
    execute, explain, execute_many, explain_many = _patched_service()
    with execute as m_execute, explain as m_explain, execute_many as m_many, explain_many:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(
                f"/api/v1/demo/{path}",
                content=body.encode(),
                headers={"content-type": "application/json"},
            )

    assert resp.status_code == 422, f"{literal}/{path}: got {resp.status_code}"
    m_execute.assert_not_awaited()
    m_explain.assert_not_awaited()
    m_many.assert_not_awaited()
    resp.json()  # body encodes cleanly
    _assert_no_internal_leak(resp.text)


@pytest.mark.asyncio
async def test_rest_large_but_valid_literal_is_accepted_not_spuriously_rejected():
    """The mirror of the corpus above: an *extreme but structurally valid*
    input (a very large string literal) must be accepted at the input boundary
    and forwarded to execution — proving these tests reject malformed shapes,
    not merely large ones, and that no over-eager size guard rejects legitimate
    values. Execution itself is mocked (no live DB in this suite)."""
    mock_result = StructuredQueryResult(
        rows=[{"id": 1}], row_count=1, truncated=False, limit=50, offset=0
    )
    body = {
        "from": "customers",
        "select": ["customers.id"],
        "where": {"col": "customers.name", "op": "eq", "value": "A" * 1_000_000},
        "limit": 50,
    }
    app = create_app(_settings())
    with patch(f"{_SERVICE}.execute", new_callable=AsyncMock, return_value=mock_result) as m:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post("/api/v1/demo/query", json=body)

    assert resp.status_code == 200
    m.assert_awaited_once()


# ---------------------------------------------------------------------------
# MCP boundary
# ---------------------------------------------------------------------------

_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _reset_mcp_session_manager() -> None:
    from querygate.mcp.server import mcp_server

    mcp_server._session_manager = None  # type: ignore[assignment]


def _parse_mcp_sse(resp) -> dict:
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError(f"no SSE data line in MCP response: {resp.text[:400]!r}")


def _mcp_call(name: str, arguments: object) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def _assert_mcp_error_without_leak(payload: dict, resp_text: str) -> None:
    """A malformed MCP tool call must surface as a client-visible tool error,
    never a server-internal one. FastMCP argument-validation failures come back
    as `result.isError == true` with a text message; a failure inside the tool
    body comes back as a structured `MCPErrorResult` with a non-INTERNAL
    error_code. Either is acceptable; a leaked internal or an INTERNAL code is
    not."""
    assert "error" not in payload, f"unexpected JSON-RPC transport error: {payload!r}"
    result = payload["result"]
    structured = result.get("structuredContent", {}).get("result")
    is_error = result.get("isError") is True
    if structured is not None:
        assert structured.get("success") is False
        assert structured.get("error_code") != "INTERNAL", structured
    else:
        assert is_error, f"expected an error result, got {result!r}"
    _assert_no_internal_leak(resp_text)


# The MCP tool takes `queries: list[StructuredQuery]`; malformed arguments are
# rejected by FastMCP before the tool body runs. Corpus mirrors the REST one,
# wrapped in the tool's argument envelope.
_MCP_MALFORMED_ARGS: dict[str, object] = {
    "queries_not_a_list": {"connection": "demo", "queries": {"from": "customers"}},
    "queries_empty": {"connection": "demo", "queries": []},
    "queries_is_string": {"connection": "demo", "queries": "customers"},
    "connection_missing": {"queries": [{"from": "customers", "select": ["customers.id"]}]},
    "connection_wrong_type": {"connection": 5, "queries": [{"from": "customers"}]},
    "query_missing_from": {"connection": "demo", "queries": [{"select": ["customers.id"]}]},
    "query_select_wrong_type": {
        "connection": "demo",
        "queries": [{"from": "customers", "select": 7}],
    },
    "query_bad_where_op": {
        "connection": "demo",
        "queries": [
            {
                "from": "customers",
                "select": ["customers.id"],
                "where": {"col": "customers.id", "op": "DROP", "value": 1},
            }
        ],
    },
    "query_extra_unknown_field": {
        "connection": "demo",
        "queries": [{"from": "customers", "select": ["customers.id"], "totally_unknown": True}],
    },
    "bad_mode": {
        "connection": "demo",
        "mode": "delete",
        "queries": [{"from": "customers", "select": ["customers.id"]}],
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", list(_MCP_MALFORMED_ARGS))
async def test_mcp_malformed_query_arguments_rejected_cleanly(case_id: str):
    _reset_mcp_session_manager()
    arguments = _MCP_MALFORMED_ARGS[case_id]
    app = create_app(_mcp_settings())
    execute, explain, execute_many, explain_many = _patched_service()
    with execute, explain, execute_many as m_many, explain_many as m_explain_many:
        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app), base_url=_BASE_URL, follow_redirects=True
            ) as client,
        ):
            resp = await client.post(
                "/mcp/", json=_mcp_call("run_structured_queries", arguments), headers=_MCP_HEADERS
            )

    assert resp.status_code == 200  # MCP errors ride inside a 200 JSON-RPC envelope
    payload = _parse_mcp_sse(resp)
    _assert_mcp_error_without_leak(payload, resp.text)
    # Malformed arguments never reached execution.
    m_many.assert_not_awaited()
    m_explain_many.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", _RAW_SQL_SMUGGLE_FIELDS)
async def test_mcp_raw_sql_field_is_rejected_not_ignored(field: str):
    _reset_mcp_session_manager()
    arguments = {"connection": "demo", "queries": [_query_with_extra_field(field)]}
    app = create_app(_mcp_settings())
    execute, explain, execute_many, explain_many = _patched_service()
    with execute, explain, execute_many as m_many, explain_many:
        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app), base_url=_BASE_URL, follow_redirects=True
            ) as client,
        ):
            resp = await client.post(
                "/mcp/", json=_mcp_call("run_structured_queries", arguments), headers=_MCP_HEADERS
            )

    assert resp.status_code == 200
    payload = _parse_mcp_sse(resp)
    _assert_mcp_error_without_leak(payload, resp.text)
    m_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_deeply_nested_argument_body_is_rejected_as_a_clean_4xx():
    """A tools/call whose arguments contain a JSON body deep enough to trip the
    parser's recursion guard must be rejected as a clean client error, never
    reach execution, and leak nothing.

    TODO.md item 86 closed the asymmetry this test used to document: the REST
    surface has always rejected such a body with a clean 400, but the MCP
    Streamable-HTTP transport's own `json.loads(body)` raised `RecursionError`
    and surfaced it as a handled — but HTTP 500 — JSON-RPC internal error. The
    `MCPRequestGuardMiddleware` now rejects an over-deep body with a clean 400
    *before* the transport parses it, so the MCP surface matches REST's
    "malformed input is a client error, never a 5xx" posture. Built as a raw
    string so a normal JSON encoder's own recursion limit isn't what's under
    test."""
    _reset_mcp_session_manager()
    deep_query = _deep_query_raw(50000)
    raw_request = (
        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
        '{"name":"run_structured_queries","arguments":'
        '{"connection":"demo","queries":[' + deep_query + "]}}}"
    )
    app = create_app(_mcp_settings())
    execute, explain, execute_many, explain_many = _patched_service()
    with execute, explain, execute_many as m_many, explain_many:
        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app), base_url=_BASE_URL, follow_redirects=True
            ) as client,
        ):
            resp = await client.post("/mcp/", content=raw_request.encode(), headers=_MCP_HEADERS)

    # Clean client error (not a 5xx), leak-free, and never dispatched to the
    # service.
    assert 400 <= resp.status_code < 500, f"got {resp.status_code}"
    payload = json.loads(resp.text)
    assert "error" in payload and payload["error"].get("code") is not None, payload
    _assert_no_internal_leak(resp.text)
    m_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_oversized_body_is_rejected_as_413_before_execution():
    """A request body past the configured byte cap is a clean 413 rejected by
    the transport guard (TODO.md item 86) before auth, before the transport
    parses it, and before any tool runs — leaking nothing. The cap is lowered
    here so the test body stays small; the shipped default is far above any
    legitimate batch."""
    _reset_mcp_session_manager()
    big_arguments = {"connection": "demo", "queries": [{"from": "customers", "select": ["x"]}]}
    raw_request = json.dumps(_mcp_call("run_structured_queries", big_arguments))
    raw_request += " " * 4096  # pad past the lowered cap without changing shape
    app = create_app(_mcp_settings(mcp_max_request_bytes=1024))
    execute, explain, execute_many, explain_many = _patched_service()
    with execute, explain, execute_many as m_many, explain_many:
        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app), base_url=_BASE_URL, follow_redirects=True
            ) as client,
        ):
            resp = await client.post("/mcp/", content=raw_request.encode(), headers=_MCP_HEADERS)

    assert resp.status_code == 413, f"got {resp.status_code}: {resp.text[:200]!r}"
    _assert_no_internal_leak(resp.text)
    m_many.assert_not_awaited()


# --------------------------------------------------------------------------- #
# The WRITE endpoints' deep-nesting boundary (gap found by item 116's audit: the
# corpus above only ever pointed at the read routes, so a write filter's own
# recursion depth was never fuzzed — and a write's WHERE is a different type since
# item 114, with its own recursive group).
# --------------------------------------------------------------------------- #
def _deep_write_where_raw(depth: int) -> str:
    inner = '{"col":"orders.id","op":"eq","value":1}'
    return '{"and":[' * depth + inner + "]}" * depth


_DEEP_WRITE_BODIES: dict[str, str] = {
    "write_deeply_nested_where_1000": (
        '{"op":"delete","table":"orders","where":' + _deep_write_where_raw(1000) + "}"
    ),
    "write_deeply_nested_where_50000": (
        '{"op":"delete","table":"orders","where":' + _deep_write_where_raw(50000) + "}"
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", list(_DEEP_WRITE_BODIES))
@pytest.mark.parametrize("path", ["write/preview", "write/execute"])
async def test_rest_deeply_nested_write_filter_is_rejected_cleanly(case_id: str, path: str):
    """A clean 4xx, never a 5xx or a RecursionError, and nothing reaches the write
    services. Depth is bounded by `max_where_depth` once parsing succeeds (item 116)
    and by the parser's own recursion guard before that; either way the caller gets a
    client error and no internals."""
    from unittest.mock import AsyncMock, patch

    body = _DEEP_WRITE_BODIES[case_id]
    app = create_app(_settings())
    preview = patch(
        "querygate.execution.write_preview.WritePreviewService.preview", new_callable=AsyncMock
    )
    execute = patch(
        "querygate.execution.write_execution.WriteExecutionService.execute", new_callable=AsyncMock
    )
    with preview as m_preview, execute as m_execute:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(
                f"/api/v1/demo/{path}", content=body, headers={"Content-Type": "application/json"}
            )
    assert 400 <= resp.status_code < 500, f"{resp.status_code}: {resp.text[:200]}"
    _assert_no_internal_leak(resp.text)
    assert not m_preview.called and not m_execute.called
