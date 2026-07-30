"""Integration coverage for TODO item 45's non-admin "my access" portal."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from querygate.api.app import create_app
from querygate.core.config import AppConfig

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"


def _settings(**overrides) -> AppConfig:
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
async def test_access_ui_is_served_with_browser_security_headers_and_no_admin_controls():
    app = create_app(_settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        response = await client.get("/access/")
        script = await client.get("/access/app.js")

    assert response.status_code == 200
    assert "QueryGate My Access" in response.text
    # TODO.md item 45 phase 2: safe explanations of the caller's own recent
    # denials, over the same self-service (no admin scope) surface.
    assert "Recent denials" in response.text
    assert "/help/my-recent-denials" in script.text
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert script.status_code == 200

    # This is a scoped, read-only portal (TODO.md item 45) — it must never
    # surface admin-only navigation, raw YAML editing, version history, or
    # audit browsing, even by accident of reusing the admin stylesheet/shell.
    for admin_only_text in (
        "Policy designer",
        "Change set",
        "Versions",
        "Audit trail",
        "Catalog review",
        "Connection health",
        "admin:config:write",
        "/admin/config/",
        "/admin/catalog/",
    ):
        assert admin_only_text not in response.text
        assert admin_only_text not in script.text

    # The caller's bearer token is in-memory only — never written to any web
    # storage, so an XSS cannot exfiltrate it. No "remember" affordance exists.
    assert "sessionStorage" not in script.text
    assert "localStorage" not in script.text
    assert "querygate_access_token" not in script.text
    assert 'id="remember-token"' not in response.text


@pytest.mark.asyncio
async def test_access_ui_shell_is_served_without_authentication():
    """Like `/admin/`, the static shell itself is public; every API call it
    makes from the browser still requires the caller's own bearer token."""
    app = create_app(_settings(api_keys=["configured-key"]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        response = await client.get("/access/")

    assert response.status_code == 200
