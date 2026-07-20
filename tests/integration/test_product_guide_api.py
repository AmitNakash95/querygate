"""REST integration coverage for public guidance and scoped live context."""

from __future__ import annotations

from unittest.mock import patch

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
    assert [item["connection"] for item in body["connection_access"]] == ["demo"]
    assert body["connection_access"][0]["guardrails"]["max_joins"] > 0
    assert body["connection_access"][0]["mandatory_filters"] == []


@pytest.mark.asyncio
async def test_my_access_reports_per_principal_guardrails_and_claim_readiness():
    """TODO.md item 45: two principals with different per-principal policy
    overrides must see different effective guardrails and mandatory-filter
    claim readiness through the same `GET /help/my-access` endpoint the new
    non-admin `/access/` portal calls — never a filter/claim value. Uses two
    JWTs with distinct `sub` claims, matching the existing per-principal
    verification pattern (see `test_jwt_enabled_in_local_dev_still_rejects_invalid_tokens`
    in `tests/integration/test_rest_api.py`) since a single API-key list maps
    to one shared subject.
    """
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    from querygate.policy.loader import PolicyStore, set_policy_store
    from querygate.policy.models import MandatoryRowFilter, Policy

    set_policy_store(
        PolicyStore(
            default=Policy(
                max_joins=5,
                mandatory_row_filters=[
                    MandatoryRowFilter(
                        table="customers", column="tenant_id", from_claim="tenant_id"
                    ),
                ],
            ),
            overrides={},
            principal_overrides={"narrow-agent": {"demo": {"max_joins": 1}}},
        )
    )

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    app = create_app(
        _settings(
            jwt_enabled=True,
            jwt_jwks_url="https://idp.example.com/.well-known/jwks.json",
            jwt_issuer="https://idp.example.com/",
            jwt_audience="querygate",
        )
    )

    def _token(subject: str) -> str:
        return jwt.encode(
            {"sub": subject, "iss": "https://idp.example.com/", "aud": "querygate"},
            private_key,
            algorithm="RS256",
        )

    with patch(
        "jwt.PyJWKClient.get_signing_key_from_jwt",
        lambda self, tok: type("K", (), {"key": public_key})(),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            broad = await client.get(
                "/api/v1/help/my-access",
                headers={"Authorization": f"Bearer {_token('broad-agent')}"},
            )
            narrow = await client.get(
                "/api/v1/help/my-access",
                headers={"Authorization": f"Bearer {_token('narrow-agent')}"},
            )

    assert broad.status_code == 200
    assert narrow.status_code == 200
    broad_detail = broad.json()["connection_access"][0]
    narrow_detail = narrow.json()["connection_access"][0]
    assert broad_detail["guardrails"]["max_joins"] == 5
    assert narrow_detail["guardrails"]["max_joins"] == 1
    [broad_filter] = broad_detail["mandatory_filters"]
    [narrow_filter] = narrow_detail["mandatory_filters"]
    assert broad_filter["claim"] == "tenant_id"
    assert broad_filter["ready"] is False
    assert narrow_filter["ready"] is False


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
