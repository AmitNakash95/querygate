"""Unit tests for the querygate-config four-eyes CLI (TODO.md item 42 phase 2).

The CLI is a thin authenticated HTTP client over /admin/config/*; these tests
mock the transport so no server is needed, and assert it builds the right
requests (path + bearer + body), renders approval status, and surfaces the
server's redaction-safe errors (403 no-scope, 409 author-conflict) rather than
inventing its own.
"""

from __future__ import annotations

import json

import httpx
import pytest

from querygate import config_cli

pytestmark = pytest.mark.unit

_ENV = {"QUERYGATE_URL": "https://gw.example", "QUERYGATE_TOKEN": "reviewer-token"}


def _run(argv, handler, monkeypatch, capsys):
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    transport = httpx.MockTransport(handler)
    real_client = config_cli.httpx.Client

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(config_cli.httpx, "Client", _client)
    rc = config_cli.main(argv)
    return rc, capsys.readouterr()


def test_versions_lists_status_and_approval_summary(monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/admin/config/versions"
        assert request.headers["Authorization"] == "Bearer reviewer-token"
        return httpx.Response(
            200,
            json=[
                {"id": "1", "status": "active", "created_by": "sys"},
                {
                    "id": "2",
                    "status": "staged",
                    "created_by": "author-a",
                    "description": "widen reporting",
                    "approvals": [
                        {"approver": "rev-b", "decision": "approve"},
                        {"approver": "rev-c", "decision": "reject"},
                    ],
                },
            ],
        )

    rc, out = _run(["versions"], handler, monkeypatch, capsys)
    assert rc == 0
    assert "active" in out.out and "staged" in out.out
    assert "approved by rev-b" in out.out
    assert "rejected by rev-c" in out.out
    assert "widen reporting" in out.out


def test_approve_posts_to_the_right_endpoint_with_note(monkeypatch, capsys):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "7",
                "status": "staged",
                "approvals": [{"approver": "rev-b", "decision": "approve"}],
            },
        )

    rc, out = _run(["approve", "7", "--note", "lgtm"], handler, monkeypatch, capsys)
    assert rc == 0
    assert seen["path"] == "/api/v1/admin/config/versions/7/approve"
    assert seen["body"] == {"note": "lgtm"}
    assert "approved by rev-b" in out.out


def test_reject_uses_reject_endpoint(monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/reject")
        return httpx.Response(200, json={"id": "7", "status": "staged", "approvals": []})

    rc, _out = _run(["reject", "7"], handler, monkeypatch, capsys)
    assert rc == 0


def test_author_conflict_409_is_surfaced(monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "author cannot approve their own change"})

    rc, out = _run(["approve", "7"], handler, monkeypatch, capsys)
    assert rc == 1
    assert "409" in out.err and "author cannot approve" in out.err


def test_missing_scope_403_is_surfaced(monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "Missing required scope: admin:config:approve"})

    rc, out = _run(["approve", "7"], handler, monkeypatch, capsys)
    assert rc == 1
    assert "403" in out.err


def test_missing_url_or_token_is_a_clean_error(monkeypatch, capsys):
    monkeypatch.delenv("QUERYGATE_URL", raising=False)
    monkeypatch.delenv("QUERYGATE_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        config_cli.main(["versions"])
