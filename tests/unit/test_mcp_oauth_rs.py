"""Unit tests for MCP OAuth 2.0 resource-server conformance (item 90 phase 2).

Covers the three pieces of the MCP 2026-07-28 authorization posture:
  * RFC 9728 protected-resource metadata (document shape + served route),
  * RFC 8707 audience binding on verified tokens (confused-deputy protection),
  * RFC 6750 `WWW-Authenticate` step-up (401 unauth / 401 invalid_token /
    403 insufficient_scope), all pointing back at the metadata.

The middleware's audience/scope logic is exercised directly against hand-built
`Principal`s so no JWKS/IdP mocking is needed; the unauthenticated path is
driven through a real ASGI round-trip.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.scopes import ALL_SCOPES
from querygate.mcp.auth import MCPAuthMiddleware
from querygate.mcp.oauth_metadata import (
    WELL_KNOWN_PREFIX,
    build_oauth_metadata_router,
    build_protected_resource_metadata,
    protected_resource_metadata_path,
)

_RESOURCE = "https://gateway.example.com/mcp"
_ISSUER = "https://idp.example.com/"


def _rs_config(**overrides) -> AppConfig:
    base = dict(
        environment="localhost",
        mcp_enabled=True,
        mcp_oauth_resource_server_enabled=True,
        mcp_resource_identifier=_RESOURCE,
        mcp_authorization_servers=[_ISSUER],
        jwt_enabled=True,
        jwt_jwks_url="https://idp.example.com/.well-known/jwks.json",
    )
    base.update(overrides)
    return AppConfig(**base)


# --------------------------------------------------------------------------- #
# Config validation                                                            #
# --------------------------------------------------------------------------- #


def test_rs_requires_jwt_enabled():
    with pytest.raises(ValueError, match="JWT_ENABLED must be true"):
        AppConfig(
            environment="localhost",
            mcp_enabled=True,
            mcp_oauth_resource_server_enabled=True,
            mcp_resource_identifier=_RESOURCE,
            mcp_authorization_servers=[_ISSUER],
            jwt_enabled=False,
        )


def test_rs_requires_resource_identifier():
    with pytest.raises(ValueError, match="MCP_RESOURCE_IDENTIFIER"):
        AppConfig(
            environment="localhost",
            mcp_enabled=True,
            mcp_oauth_resource_server_enabled=True,
            mcp_resource_identifier="",
            mcp_authorization_servers=[_ISSUER],
            jwt_enabled=True,
            jwt_jwks_url="https://idp.example.com/.well-known/jwks.json",
        )


def test_rs_requires_authorization_servers():
    with pytest.raises(ValueError, match="MCP_AUTHORIZATION_SERVERS"):
        AppConfig(
            environment="localhost",
            mcp_enabled=True,
            mcp_oauth_resource_server_enabled=True,
            mcp_resource_identifier=_RESOURCE,
            mcp_authorization_servers=[],
            jwt_enabled=True,
            jwt_jwks_url="https://idp.example.com/.well-known/jwks.json",
        )


def test_disabled_rs_needs_no_oauth_fields():
    # The default posture must remain valid with none of the RS fields set.
    cfg = AppConfig(environment="localhost")
    assert cfg.mcp_oauth_resource_server_enabled is False


# --------------------------------------------------------------------------- #
# RFC 9728 metadata document + route                                           #
# --------------------------------------------------------------------------- #


def test_metadata_document_shape():
    cfg = _rs_config(
        mcp_required_scopes=["mcp:query"],
        mcp_resource_documentation="https://docs.example.com/mcp",
    )
    doc = build_protected_resource_metadata(cfg)
    assert doc["resource"] == _RESOURCE
    assert doc["authorization_servers"] == [_ISSUER]
    assert doc["bearer_methods_supported"] == ["header"]
    # scopes_supported advertises the FULL vocabulary (item 95), unioned with any
    # custom required scope, so an IdP can import the whole set.
    assert doc["scopes_supported"] == sorted(set(ALL_SCOPES) | {"mcp:query"})
    assert doc["resource_documentation"] == "https://docs.example.com/mcp"


def test_metadata_publishes_full_vocabulary_even_without_required_scopes():
    # Unlike the old behavior (scopes_supported present only when required scopes
    # were configured), the full catalog is always advertised now.
    doc = build_protected_resource_metadata(_rs_config())
    assert doc["scopes_supported"] == sorted(ALL_SCOPES)
    assert "resource_documentation" not in doc


def test_metadata_path_follows_mount_path():
    cfg = _rs_config(mcp_mount_path="/mcp")
    assert protected_resource_metadata_path(cfg) == "/.well-known/oauth-protected-resource/mcp"


def test_metadata_route_served_unauthenticated():
    cfg = _rs_config()
    app = FastAPI()
    app.include_router(build_oauth_metadata_router(cfg))
    client = TestClient(app)

    suffixed = client.get(protected_resource_metadata_path(cfg))
    assert suffixed.status_code == 200
    assert suffixed.json()["resource"] == _RESOURCE

    # Root alias returns the identical document for clients omitting the suffix.
    root = client.get(WELL_KNOWN_PREFIX)
    assert root.status_code == 200
    assert root.json() == suffixed.json()


# --------------------------------------------------------------------------- #
# Middleware: audience binding + scope step-up                                  #
# --------------------------------------------------------------------------- #


def _middleware(cfg: AppConfig) -> MCPAuthMiddleware:
    async def _downstream(scope, receive, send):  # pragma: no cover - never called on denial
        raise AssertionError("downstream should not run when the request is denied")

    return MCPAuthMiddleware(app=_downstream, settings=cfg)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "headers": [(b"host", b"gateway.example.com")],
            "scheme": "https",
            "server": ("gateway.example.com", 443),
            "query_string": b"",
        }
    )


def test_correct_audience_and_scope_passes():
    mw = _middleware(_rs_config(mcp_required_scopes=["mcp:query"]))
    principal = Principal(
        subject="user-1",
        scopes=frozenset({"mcp:query"}),
        claims={"aud": _RESOURCE},
        auth_method="jwt",
    )
    assert mw._authorize_resource_server(principal, _request()) is None


def test_wrong_audience_is_rejected_with_invalid_token_challenge():
    mw = _middleware(_rs_config())
    principal = Principal(
        subject="user-1",
        claims={"aud": "https://some-other-api.example.com"},
        auth_method="jwt",
    )
    denial = mw._authorize_resource_server(principal, _request())
    assert denial is not None
    assert denial.status_code == 401
    challenge = denial.headers["WWW-Authenticate"]
    assert 'error="invalid_token"' in challenge
    assert 'resource_metadata="https://gateway.example.com/.well-known/' in challenge


def test_missing_audience_claim_is_rejected():
    mw = _middleware(_rs_config())
    principal = Principal(subject="user-1", claims={}, auth_method="jwt")
    denial = mw._authorize_resource_server(principal, _request())
    assert denial is not None
    assert denial.status_code == 401


def test_audience_as_list_is_accepted():
    mw = _middleware(_rs_config())
    principal = Principal(
        subject="user-1",
        claims={"aud": ["https://other.example.com", _RESOURCE]},
        auth_method="jwt",
    )
    assert mw._authorize_resource_server(principal, _request()) is None


def test_insufficient_scope_is_rejected_with_challenge():
    mw = _middleware(_rs_config(mcp_required_scopes=["mcp:query", "mcp:admin"]))
    principal = Principal(
        subject="user-1",
        scopes=frozenset({"mcp:query"}),
        claims={"aud": _RESOURCE},
        auth_method="jwt",
    )
    denial = mw._authorize_resource_server(principal, _request())
    assert denial is not None
    assert denial.status_code == 403
    challenge = denial.headers["WWW-Authenticate"]
    assert 'error="insufficient_scope"' in challenge
    assert 'scope="mcp:admin mcp:query"' in challenge


def test_api_key_skips_audience_but_still_needs_scope():
    mw = _middleware(_rs_config(mcp_required_scopes=["mcp:query"]))
    # A static API-key principal carries no `aud`; audience binding does not
    # apply, but the scope gate still does.
    without_scope = Principal(subject="svc", scopes=frozenset(), auth_method="api_key")
    denial = mw._authorize_resource_server(without_scope, _request())
    assert denial is not None and denial.status_code == 403

    with_scope = Principal(subject="svc", scopes=frozenset({"mcp:query"}), auth_method="api_key")
    assert mw._authorize_resource_server(with_scope, _request()) is None


# --------------------------------------------------------------------------- #
# Middleware: unauthenticated challenge (full ASGI round-trip)                  #
# --------------------------------------------------------------------------- #


async def _run_unauth(cfg: AppConfig) -> tuple[int, dict[str, str]]:
    mw = _middleware(cfg)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"host", b"gateway.example.com")],
        "scheme": "https",
        "server": ("gateway.example.com", 443),
        "query_string": b"",
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    status_holder: dict[str, int] = {}
    headers: dict[str, str] = {}

    async def send(message):
        if message["type"] == "http.response.start":
            status_holder["status"] = message["status"]
            for k, v in message["headers"]:
                headers[k.decode()] = v.decode()

    await mw(scope, receive, send)
    return status_holder["status"], headers


async def test_unauthenticated_request_gets_resource_metadata_challenge():
    status_code, headers = await _run_unauth(_rs_config())
    assert status_code == 401
    assert "www-authenticate" in headers
    assert "resource_metadata=" in headers["www-authenticate"]
    assert "/.well-known/oauth-protected-resource/mcp" in headers["www-authenticate"]


async def test_unauthenticated_request_without_rs_has_no_challenge():
    # RS disabled: production JWT-only config, unauth request → plain 401.
    cfg = AppConfig(
        environment="production",
        mcp_enabled=True,
        jwt_enabled=True,
        jwt_jwks_url="https://idp.example.com/.well-known/jwks.json",
    )
    status_code, headers = await _run_unauth(cfg)
    assert status_code == 401
    assert "www-authenticate" not in headers
