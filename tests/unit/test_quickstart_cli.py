"""Unit tests for the querygate-quickstart CLI (TODO.md item 146).

Same mock-transport strategy as test_config_cli.py: the CLI is a thin
authenticated HTTP client over already-shipped read-only discovery routes, so
these tests mock the transport and assert it (a) picks the first table with
at least two non-sensitive columns, (b) never proposes a sensitive column,
and (c) renders all three snippet kinds for each of the three example
queries.
"""

from __future__ import annotations

import json

import httpx
import pytest

from querygate import quickstart_cli

pytestmark = pytest.mark.unit

_ENV = {"QUERYGATE_URL": "https://gw.example", "QUERYGATE_TOKEN": "caller-token"}


def _run(argv, handler, monkeypatch, capsys):
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    transport = httpx.MockTransport(handler)
    real_client = quickstart_cli.httpx.Client

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(quickstart_cli.httpx, "Client", _client)
    rc = quickstart_cli.main(argv)
    return rc, capsys.readouterr()


def _column(name, *, sensitivity=None):
    col = {"name": name, "type": "text", "nullable": True}
    if sensitivity is not None:
        col["catalog"] = {
            "sensitivity": sensitivity,
            "provenance": {"status": "verified", "precedence": "human"},
        }
    return col


def test_skips_a_table_with_fewer_than_two_safe_columns(monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/demo/tables":
            return httpx.Response(200, json={"tables": ["staff", "orders"]})
        if path == "/api/v1/demo/tables/staff":
            # Every column sensitive (or only one safe) — must be skipped.
            return httpx.Response(
                200,
                json={
                    "name": "staff",
                    "columns": [
                        _column("ssn", sensitivity="pii"),
                        _column("salary", sensitivity="confidential"),
                    ],
                },
            )
        if path == "/api/v1/demo/tables/orders":
            return httpx.Response(
                200,
                json={
                    "name": "orders",
                    "columns": [
                        _column("id"),
                        _column("status"),
                        _column("customer_email", sensitivity="pii"),
                    ],
                },
            )
        raise AssertionError(f"unexpected request: {path}")

    rc, out = _run(["demo"], handler, monkeypatch, capsys)
    assert rc == 0
    assert "found table 'orders'" in out.out
    assert "'id'" in out.out or "id" in out.out
    # The sensitive column must never appear in a proposed query.
    assert "customer_email" not in out.out


def test_proposes_three_query_shapes_with_all_snippet_kinds(monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert request.headers["Authorization"] == "Bearer caller-token"
        if path == "/api/v1/demo/tables":
            return httpx.Response(200, json={"tables": ["orders"]})
        if path == "/api/v1/demo/tables/orders":
            return httpx.Response(
                200,
                json={
                    "name": "orders",
                    "columns": [_column("id"), _column("status")],
                },
            )
        raise AssertionError(f"unexpected request: {path}")

    rc, out = _run(["demo"], handler, monkeypatch, capsys)
    assert rc == 0
    assert out.out.count("StructuredQuery body:") == 3
    assert out.out.count("REST (curl):") == 3
    assert out.out.count("MCP tool call (run_structured_queries):") == 3
    assert out.out.count("Python SDK:") == 3
    assert "1. A plain select" in out.out
    assert "2. A filtered select" in out.out
    assert "3. An aggregate (count per group)" in out.out
    assert "is_not_null" in out.out
    assert "group_by" in out.out
    assert "from querygate.client import Query, col, agg" in out.out


def test_no_qualifying_table_is_a_clean_non_error_message(monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/demo/tables":
            return httpx.Response(200, json={"tables": ["staff"]})
        return httpx.Response(
            200,
            json={
                "name": "staff",
                "columns": [_column("ssn", sensitivity="pii")],
            },
        )

    rc, out = _run(["demo"], handler, monkeypatch, capsys)
    assert rc == 1
    assert "nothing to propose" in out.err


def test_generated_queries_are_valid_json_bodies(monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/demo/tables":
            return httpx.Response(200, json={"tables": ["orders"]})
        return httpx.Response(
            200,
            json={"name": "orders", "columns": [_column("id"), _column("status")]},
        )

    rc, out = _run(["demo"], handler, monkeypatch, capsys)
    assert rc == 0
    # Every "StructuredQuery body:" block is followed by parseable JSON.
    blocks = out.out.split("StructuredQuery body:\n")[1:]
    for block in blocks:
        raw = block.split("\n\nREST (curl):")[0]
        parsed = json.loads(raw)
        assert parsed["from"] == "orders"


def test_missing_url_or_token_is_a_clean_error(monkeypatch):
    monkeypatch.delenv("QUERYGATE_URL", raising=False)
    monkeypatch.delenv("QUERYGATE_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        quickstart_cli.main(["demo"])
