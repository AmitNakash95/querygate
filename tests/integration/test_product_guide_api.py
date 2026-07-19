"""REST integration coverage for public guidance and scoped live context."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from querygate.api.app import create_app
from querygate.core.config import AppConfig

pytestmark = pytest.mark.integration


def _settings(**overrides) -> AppConfig:
    """Build auth-isolated settings that never inherit a developer's .env.

    These tests assert exact scope boundaries, so allowing API_KEY_SCOPES or
    JWT settings from the checkout's local environment would change the
    principal under test rather than exercise the fixture declared here.
    """
    values = {
        "environment": "localhost",
        "api_keys": [],
        "api_key_subject": "test-client",
        "api_key_scopes": [],
        "jwt_enabled": False,
        "mcp_enabled": False,
    }
    values.update(overrides)
    return AppConfig(_env_file=None, **values)


@pytest.mark.asyncio
async def test_static_guide_is_available_without_credentials_when_api_auth_is_configured():
    app = create_app(_settings(api_keys=["required-key"]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.get("/api/v1/help/search", params={"q": "configure policy"})

    assert response.status_code == 200
    assert response.json()["results"][0]["topic_id"] == "configuration.policy"


@pytest.mark.asyncio
async def test_my_access_requires_auth_and_returns_only_caller_capabilities():
    app = create_app(
        _settings(
            api_keys=["reader-key"],
            api_key_subject="reader",
            api_key_scopes=["admin:config:read"],
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        unauthorized = await client.get("/api/v1/help/my-access")
        authorized = await client.get(
            "/api/v1/help/my-access", headers={"Authorization": "Bearer reader-key"}
        )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    body = authorized.json()
    assert body["principal"] == "reader"
    assert body["capabilities"]["read_configuration"] is True
    assert body["capabilities"]["change_configuration"] is False
    assert [item["id"] for item in body["visible_connections"]] == ["demo"]


@pytest.mark.asyncio
async def test_configuration_summary_is_scope_gated_and_never_returns_raw_yaml():
    without_scope = create_app(_settings(api_keys=["plain-key"]))
    with_scope = create_app(
        _settings(
            api_keys=["reader-key"],
            api_key_scopes=["admin:config:read"],
        )
    )
    async with AsyncClient(
        transport=ASGITransport(app=without_scope), base_url="http://localhost"
    ) as client:
        forbidden = await client.get(
            "/api/v1/help/configuration", headers={"Authorization": "Bearer plain-key"}
        )
    async with AsyncClient(
        transport=ASGITransport(app=with_scope), base_url="http://localhost"
    ) as client:
        allowed = await client.get(
            "/api/v1/help/configuration", headers={"Authorization": "Bearer reader-key"}
        )

    assert forbidden.status_code == 403
    assert allowed.status_code == 200
    serialized = allowed.text
    assert "connections_yaml" not in serialized
    assert "policy_yaml" not in serialized
    assert "catalog_yaml" not in serialized
    assert "connection_string" not in serialized
