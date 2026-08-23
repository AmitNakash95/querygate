"""End-to-end HTTP behaviour of the sign-in surface."""

from __future__ import annotations

import time
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from querygate.api.app import create_app
from querygate.core.config import AppConfig
from querygate.identity.config_store import IdentityConfigStore, set_identity_store
from querygate.identity.local_store import LocalUserStore, set_local_user_store
from querygate.identity.passwords import ScryptParams, hash_password
from querygate.identity.totp import new_totp_secret, totp_code

pytestmark = pytest.mark.unit

_PASSWORD = "correct-horse-battery-staple"
_CHEAP = ScryptParams(n=2**12, r=8, p=1)


def _identity_config(**mapping_overrides) -> dict:
    return {
        "sso": {"base_url": "https://qg.example.com", "default_landing_path": "/admin/"},
        "providers": [
            {"id": "local", "kind": "local"},
            {
                "id": "entra",
                "preset": "entra_id",
                "tenant": "tenant-1",
                "client_id": "client-abc",
                "client_secret": "super-secret-value",
            },
        ],
        "mapping": {
            "rules": mapping_overrides.get(
                "rules",
                [
                    {
                        "provider": "local",
                        "claim": "groups",
                        "equals": "admins",
                        "grant_roles": ["Identity Administrator"],
                    }
                ],
            )
        },
    }


def _make_client(tmp_path, *, groups=("admins",), totp_secret="", **config_overrides) -> TestClient:
    users_file = tmp_path / "users.yaml"
    settings_kwargs = {
        "environment": "localhost",
        "api_v1_prefix": "/api/v1",
        "sso_enabled": True,
        "local_idp_enabled": True,
        "sso_device_grant_enabled": True,
        "sso_session_cookie_secure": False,
        "local_users_file": str(users_file),
    }
    settings_kwargs.update(config_overrides)
    settings = AppConfig(**settings_kwargs)
    set_identity_store(IdentityConfigStore.from_dict(_identity_config()))
    set_local_user_store(
        LocalUserStore.from_dict(
            {
                "users": [
                    {
                        "username": "alice",
                        "display_name": "Alice",
                        "email": "alice@example.com",
                        "groups": list(groups),
                        "password_verifier": hash_password(_PASSWORD, _CHEAP),
                        "totp_secret": totp_secret,
                    }
                ]
            }
        )
    )
    users_file.write_text("users: []\n")
    return TestClient(create_app(settings))


def _sign_in(client: TestClient, code: Optional[str] = None) -> str:
    """Sign in as alice and return the CSRF token."""
    payload = {"username": "alice", "password": _PASSWORD}
    if code:
        payload["totp_code"] = code
    response = client.post("/api/v1/auth/local/login", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


class TestProviderDiscovery:
    def test_the_sign_in_surface_does_not_exist_when_sso_is_off(self, tmp_path):
        client = TestClient(create_app(AppConfig(environment="localhost")))
        assert client.get("/api/v1/auth/providers").status_code == 404

    def test_providers_are_listed_without_a_credential(self, tmp_path):
        client = _make_client(tmp_path)
        body = client.get("/api/v1/auth/providers").json()
        assert {p["id"] for p in body["providers"]} == {"local", "entra"}
        assert body["local_provider_id"] == "local"
        assert body["device_grant_enabled"] is True

    def test_the_public_provider_list_exposes_nothing_but_a_label(self, tmp_path):
        client = _make_client(tmp_path)
        serialized = client.get("/api/v1/auth/providers").text
        assert "super-secret-value" not in serialized
        assert "login.microsoftonline.com" not in serialized
        assert "client-abc" not in serialized

    def test_a_disabled_provider_is_not_offered(self, tmp_path):
        client = _make_client(tmp_path)
        config = _identity_config()
        config["providers"][1]["enabled"] = False
        set_identity_store(IdentityConfigStore.from_dict(config))
        body = client.get("/api/v1/auth/providers").json()
        assert {p["id"] for p in body["providers"]} == {"local"}


class TestLocalLogin:
    def test_successful_login_sets_an_httponly_session_cookie(self, tmp_path):
        client = _make_client(tmp_path)
        response = client.post(
            "/api/v1/auth/local/login", json={"username": "alice", "password": _PASSWORD}
        )
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
        body = response.json()
        assert body["subject"] == "alice"
        assert body["scopes"] == ["admin:identity:read", "admin:identity:write"]

    def test_the_session_token_is_never_in_the_response_body(self, tmp_path):
        client = _make_client(tmp_path)
        response = client.post(
            "/api/v1/auth/local/login", json={"username": "alice", "password": _PASSWORD}
        )
        token = client.cookies.get("qg_session")
        assert token and token not in response.text

    def test_a_user_matching_no_mapping_rule_signs_in_with_no_authority(self, tmp_path):
        client = _make_client(tmp_path, groups=("contractors",))
        body = client.post(
            "/api/v1/auth/local/login", json={"username": "alice", "password": _PASSWORD}
        ).json()
        assert body["authenticated"] is True
        assert body["scopes"] == []

    def test_a_wrong_password_is_401_with_a_generic_message(self, tmp_path):
        client = _make_client(tmp_path)
        wrong = client.post(
            "/api/v1/auth/local/login", json={"username": "alice", "password": "nope"}
        )
        unknown = client.post(
            "/api/v1/auth/local/login", json={"username": "nobody", "password": "nope"}
        )
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json()["detail"] == unknown.json()["detail"]

    def test_repeated_failures_lock_the_account_with_a_retry_after(self, tmp_path):
        client = _make_client(tmp_path, local_login_lockout_threshold=2)
        for _ in range(2):
            client.post("/api/v1/auth/local/login", json={"username": "alice", "password": "no"})
        response = client.post(
            "/api/v1/auth/local/login", json={"username": "alice", "password": _PASSWORD}
        )
        assert response.status_code == 429
        assert int(response.headers["retry-after"]) > 0

    def test_an_enrolled_second_factor_is_required(self, tmp_path):
        secret = new_totp_secret()
        client = _make_client(tmp_path, totp_secret=secret)
        assert (
            client.post(
                "/api/v1/auth/local/login", json={"username": "alice", "password": _PASSWORD}
            ).status_code
            == 401
        )
        code = totp_code(secret, int(time.time() // 30))
        assert (
            client.post(
                "/api/v1/auth/local/login",
                json={"username": "alice", "password": _PASSWORD, "totp_code": code},
            ).status_code
            == 200
        )


class TestSessionAndCsrf:
    def test_session_endpoint_reports_anonymous_before_sign_in(self, tmp_path):
        client = _make_client(tmp_path)
        assert client.get("/api/v1/auth/session").json() == {
            "authenticated": False,
            "subject": "",
            "display_name": "",
            "email": "",
            "provider_id": "",
            "auth_method": "",
            "scopes": [],
            "csrf_token": "",
            "expires_at": None,
        }

    def test_session_endpoint_issues_the_csrf_token_after_sign_in(self, tmp_path):
        client = _make_client(tmp_path)
        csrf = _sign_in(client)
        body = client.get("/api/v1/auth/session").json()
        assert body["authenticated"] is True and body["csrf_token"] == csrf

    def test_a_cookie_without_the_csrf_header_is_not_a_credential(self, tmp_path):
        client = _make_client(tmp_path)
        _sign_in(client)
        # The cookie alone must not authenticate a scope-gated API call — this
        # is what makes a cross-site request from another origin inert.
        assert client.get("/api/v1/admin/identity/users").status_code == 403

    def test_the_csrf_header_makes_the_same_call_succeed(self, tmp_path):
        client = _make_client(tmp_path)
        csrf = _sign_in(client)
        response = client.get("/api/v1/admin/identity/users", headers={"X-QueryGate-CSRF": csrf})
        assert response.status_code == 200

    def test_a_forged_csrf_token_is_refused(self, tmp_path):
        client = _make_client(tmp_path)
        _sign_in(client)
        response = client.get(
            "/api/v1/admin/identity/users", headers={"X-QueryGate-CSRF": "qgc_not-the-token"}
        )
        assert response.status_code == 403

    def test_logout_invalidates_the_session_server_side(self, tmp_path):
        client = _make_client(tmp_path)
        csrf = _sign_in(client)
        assert (
            client.post("/api/v1/auth/logout", headers={"X-QueryGate-CSRF": csrf}).status_code
            == 200
        )
        assert client.get("/api/v1/auth/session").json()["authenticated"] is False

    def test_logout_without_the_csrf_header_is_refused(self, tmp_path):
        client = _make_client(tmp_path)
        _sign_in(client)
        assert client.post("/api/v1/auth/logout").status_code == 403
        assert client.get("/api/v1/auth/session").json()["authenticated"] is True


class TestRedirectLogin:
    def test_an_unknown_provider_is_404(self, tmp_path):
        client = _make_client(tmp_path)
        response = client.get(
            "/api/v1/auth/sso/login", params={"provider": "nope"}, follow_redirects=False
        )
        assert response.status_code == 404

    def test_the_local_provider_has_no_redirect_flow(self, tmp_path):
        client = _make_client(tmp_path)
        response = client.get(
            "/api/v1/auth/sso/login", params={"provider": "local"}, follow_redirects=False
        )
        assert response.status_code == 400

    def test_a_callback_with_no_login_flow_lands_back_on_the_app(self, tmp_path):
        client = _make_client(tmp_path)
        response = client.get(
            "/api/v1/auth/sso/callback", params={"code": "x", "state": "y"}, follow_redirects=False
        )
        assert response.status_code == 302
        assert response.headers["location"] == "/admin/?sso_error=no_login_flow"


class TestDeviceGrantOverHttp:
    def test_full_flow_from_code_to_token(self, tmp_path):
        client = _make_client(tmp_path)
        started = client.post(
            "/api/v1/auth/device/code",
            json={"client_name": "querygate-cli", "scopes": ["admin:identity:read"]},
        ).json()
        assert started["verification_uri"].endswith("/admin/")
        assert started["interval"] >= 1

        # Pending until a human approves.
        pending = client.post(
            "/api/v1/auth/device/token", json={"device_code": started["device_code"]}
        )
        assert pending.status_code == 400
        assert pending.json()["detail"]["error"] == "authorization_pending"

        csrf = _sign_in(client)
        approved = client.post(
            "/api/v1/auth/device/approve",
            json={"user_code": started["user_code"].lower()},
            headers={"X-QueryGate-CSRF": csrf},
        )
        assert approved.status_code == 200 and approved.json()["status"] == "approved"

        issued = client.post(
            "/api/v1/auth/device/token", json={"device_code": started["device_code"]}
        ).json()
        assert issued["token_type"] == "Bearer"
        assert issued["scope"] == "admin:identity:read"

        # The token authenticates a *bearer* call as the approving human.
        fresh = TestClient(client.app)
        listed = fresh.get(
            "/api/v1/admin/identity/users",
            headers={"Authorization": f"Bearer {issued['access_token']}"},
        )
        assert listed.status_code == 200

    def test_approving_requires_a_signed_in_human(self, tmp_path):
        client = _make_client(tmp_path)
        started = client.post("/api/v1/auth/device/code", json={"client_name": "cli"}).json()
        assert (
            client.post(
                "/api/v1/auth/device/approve", json={"user_code": started["user_code"]}
            ).status_code
            == 401
        )

    def test_a_token_never_exceeds_the_approvers_scopes(self, tmp_path):
        client = _make_client(tmp_path, groups=("contractors",))  # maps to no scopes
        started = client.post(
            "/api/v1/auth/device/code",
            json={"client_name": "cli", "scopes": ["admin:identity:write"]},
        ).json()
        csrf = _sign_in(client)
        client.post(
            "/api/v1/auth/device/approve",
            json={"user_code": started["user_code"]},
            headers={"X-QueryGate-CSRF": csrf},
        )
        issued = client.post(
            "/api/v1/auth/device/token", json={"device_code": started["device_code"]}
        ).json()
        assert issued["scope"] == ""

    def test_the_device_grant_is_absent_when_not_enabled(self, tmp_path):
        client = _make_client(tmp_path, sso_device_grant_enabled=False)
        assert (
            client.post("/api/v1/auth/device/code", json={"client_name": "cli"}).status_code == 404
        )


class TestAdminIdentitySurface:
    def _admin(self, tmp_path):
        client = _make_client(tmp_path)
        return client, {"X-QueryGate-CSRF": _sign_in(client)}

    def test_creating_and_listing_an_account(self, tmp_path):
        client, headers = self._admin(tmp_path)
        created = client.post(
            "/api/v1/admin/identity/users",
            json={"username": "bob", "password": "another-long-password", "groups": ["readers"]},
            headers=headers,
        )
        assert created.status_code == 201
        assert "password_verifier" not in created.text
        listed = client.get("/api/v1/admin/identity/users", headers=headers).json()
        assert [u["username"] for u in listed] == ["bob"]
        assert listed[0]["must_change_password"] is True

    def test_a_weak_password_is_refused(self, tmp_path):
        client, headers = self._admin(tmp_path)
        response = client.post(
            "/api/v1/admin/identity/users",
            json={"username": "bob", "password": "short"},
            headers=headers,
        )
        assert response.status_code == 400

    def test_a_duplicate_account_is_a_conflict(self, tmp_path):
        client, headers = self._admin(tmp_path)
        body = {"username": "bob", "password": "another-long-password"}
        client.post("/api/v1/admin/identity/users", json=body, headers=headers)
        again = client.post("/api/v1/admin/identity/users", json=body, headers=headers)
        assert again.status_code == 409

    def test_mfa_enrolment_returns_the_secret_once_and_never_again(self, tmp_path):
        client, headers = self._admin(tmp_path)
        client.post(
            "/api/v1/admin/identity/users",
            json={"username": "bob", "password": "another-long-password"},
            headers=headers,
        )
        enrolled = client.post("/api/v1/admin/identity/users/bob/mfa", headers=headers).json()
        assert enrolled["secret"] and enrolled["provisioning_uri"].startswith("otpauth://")
        listed = client.get("/api/v1/admin/identity/users", headers=headers).text
        assert enrolled["secret"] not in listed
        assert '"mfa_enabled":true' in listed.replace(" ", "")

    def test_an_administrator_cannot_delete_or_disable_themselves(self, tmp_path):
        client, headers = self._admin(tmp_path)
        assert (
            client.delete("/api/v1/admin/identity/users/alice", headers=headers).status_code == 400
        )
        assert (
            client.patch(
                "/api/v1/admin/identity/users/alice", json={"disabled": True}, headers=headers
            ).status_code
            == 400
        )

    def test_the_provider_view_shows_configuration_but_never_the_secret(self, tmp_path):
        client, headers = self._admin(tmp_path)
        response = client.get("/api/v1/admin/identity/providers", headers=headers)
        assert response.status_code == 200
        assert "super-secret-value" not in response.text
        entra = next(p for p in response.json() if p["id"] == "entra")
        assert entra["issuer"] == "https://login.microsoftonline.com/tenant-1/v2.0"
        assert entra["client_id"] == "client-abc"
        assert entra["confidential_client"] is True

    def test_the_mapping_simulator_explains_a_claim_set(self, tmp_path):
        client, headers = self._admin(tmp_path)
        response = client.post(
            "/api/v1/admin/identity/simulate",
            json={"provider_id": "local", "claims": {"groups": ["admins"]}},
            headers=headers,
        ).json()
        assert response["granted_scopes"] == ["admin:identity:read", "admin:identity:write"]
        assert response["matched_rules"][0]["matched_value"] == "admins"

    def test_revocation_ends_a_persons_session_immediately(self, tmp_path):
        client, headers = self._admin(tmp_path)
        assert (
            client.post(
                "/api/v1/admin/identity/revoke", json={"subject": "alice"}, headers=headers
            ).json()["sessions_revoked"]
            == 1
        )
        assert client.get("/api/v1/auth/session").json()["authenticated"] is False

    def test_the_admin_surface_needs_the_scope(self, tmp_path):
        client = _make_client(tmp_path, groups=("contractors",))
        csrf = _sign_in(client)
        response = client.get("/api/v1/admin/identity/users", headers={"X-QueryGate-CSRF": csrf})
        assert response.status_code == 403
