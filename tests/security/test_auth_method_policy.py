"""Per-surface credential-type policy (TODO.md item 200).

Authentication says who a caller is. This says whether that *kind* of proof is
acceptable on the surface they reached. The property that matters most is the
default: enabling SSO must actually **close** the console to shared secrets,
not merely offer a nicer door beside them.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from querygate.api.app import create_app
from querygate.core.auth import Principal
from querygate.core.auth_policy import (
    ANONYMOUS_METHOD,
    API_KEY_METHOD,
    CALLER_AUTH_METHODS,
    CONSOLE_SURFACE,
    DEVICE_TOKEN_METHOD,
    JWT_METHOD,
    MCP_SURFACE,
    REST_SURFACE,
    SHARED_SECRET_METHODS,
    SSO_SESSION_METHOD,
    AuthMethodNotPermitted,
    AuthMethodPolicy,
    default_methods,
    surface_for_path,
    validate_methods,
)
from querygate.core.config import AppConfig
from querygate.identity.config_store import IdentityConfigStore, set_identity_store
from querygate.identity.local_store import LocalUserStore, set_local_user_store
from querygate.identity.passwords import ScryptParams, hash_password

pytestmark = pytest.mark.security

_IDENTITY = {
    "sso": {"base_url": "http://127.0.0.1"},
    "providers": [{"id": "dev", "kind": "dev"}],
    "mapping": {
        "rules": [
            {
                "provider": "dev",
                "claim": "groups",
                "equals": "platform",
                "grant_roles": ["Config Governor"],
            }
        ]
    },
}


class TestVocabulary:
    def test_the_method_set_matches_what_authenticators_actually_produce(self):
        """A closed vocabulary is only useful if it is the real one.

        Each of these is the `auth_method` some authenticator stamps onto a
        Principal; if one were missing, a configuration naming it would be
        rejected as a typo, and if one were spurious, an operator could allow a
        credential type that cannot exist.
        """
        from querygate.core.auth import AnonymousAuthenticator, ApiKeyAuthenticator
        from querygate.identity.authenticators import (
            DEVICE_TOKEN_AUTH_METHOD,
            SESSION_AUTH_METHOD,
        )

        produced = {
            ApiKeyAuthenticator(["k"], "svc").authenticate("k").auth_method,
            AnonymousAuthenticator().authenticate(None).auth_method,
            SESSION_AUTH_METHOD,
            DEVICE_TOKEN_AUTH_METHOD,
            JWT_METHOD,  # core/jwt_auth.py stamps this literal
        }
        assert produced == CALLER_AUTH_METHODS

    def test_an_unknown_method_name_is_refused_at_load(self):
        with pytest.raises(ValueError, match="unknown authentication method"):
            validate_methods(["sso_sesion"], field="CONSOLE_AUTH_METHODS")

    def test_the_shared_secret_set_is_exactly_the_non_human_ones(self):
        assert SHARED_SECRET_METHODS == {API_KEY_METHOD, ANONYMOUS_METHOD}


class TestDefaults:
    def test_enabling_sso_closes_the_console_to_shared_secrets(self):
        """The whole point of the control."""
        allowed = default_methods(CONSOLE_SURFACE, sso_enabled=True)
        assert API_KEY_METHOD not in allowed
        assert ANONYMOUS_METHOD not in allowed
        assert SSO_SESSION_METHOD in allowed and JWT_METHOD in allowed

    def test_a_deployment_without_sso_is_untouched(self):
        assert default_methods(CONSOLE_SURFACE, sso_enabled=False) == CALLER_AUTH_METHODS

    def test_the_agent_surfaces_stay_permissive_by_default(self):
        """Agents and services legitimately authenticate with tokens."""
        assert default_methods(REST_SURFACE, sso_enabled=True) == CALLER_AUTH_METHODS
        assert default_methods(MCP_SURFACE, sso_enabled=True) == CALLER_AUTH_METHODS

    def test_an_explicit_list_overrides_the_default_in_both_directions(self):
        tightened = AuthMethodPolicy.build(
            [SSO_SESSION_METHOD], surface=REST_SURFACE, sso_enabled=True
        )
        assert tightened.allowed == {SSO_SESSION_METHOD}
        relaxed = AuthMethodPolicy.build(
            [API_KEY_METHOD], surface=CONSOLE_SURFACE, sso_enabled=True
        )
        assert relaxed.allowed == {API_KEY_METHOD}

    def test_a_resolved_allowlist_is_never_empty(self):
        """There is no way to lock every credential out of a surface.

        Asserted as a property rather than guarded in config: an empty
        configured list falls back to the surface default, and a non-empty one
        is non-empty by construction — so a "would accept nothing" check would
        be a security check that never fires.
        """
        for surface in (CONSOLE_SURFACE, REST_SURFACE, MCP_SURFACE):
            for sso in (True, False):
                for configured in ([], [" "], ["", "  "]):
                    policy = AuthMethodPolicy.build(
                        validate_methods(configured, field="X"),
                        surface=surface,
                        sso_enabled=sso,
                    )
                    assert policy.allowed, (surface, sso, configured)


class TestSurfaceBoundary:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("/api/v1/admin/config/versions", CONSOLE_SURFACE),
            ("/api/v1/admin/identity/users", CONSOLE_SURFACE),
            ("/api/v1/admin/catalog/proposals", CONSOLE_SURFACE),
            ("/api/v1/connections", REST_SURFACE),
            ("/api/v1/query/execute", REST_SURFACE),
            ("/api/v1/help/my-access", REST_SURFACE),
            ("/api/v1/auth/session", REST_SURFACE),
        ],
    )
    def test_the_console_is_the_admin_path_prefix(self, path, expected):
        assert surface_for_path(path, "/api/v1") == expected

    def test_every_admin_router_mounts_under_the_console_prefix(self):
        """The boundary is only real if no admin router escapes it.

        `surface_for_path` decides by prefix, so an admin router mounted
        somewhere else would silently fall under the *REST* policy — the
        permissive one. Each builder is invoked and every route it produces is
        checked, so adding a seventh admin router without the prefix fails here
        rather than quietly widening what a static key can reach.
        """
        from querygate.api.admin_config_routes import build_admin_config_router
        from querygate.api.admin_connections_routes import build_admin_connections_router
        from querygate.api.admin_identity_routes import build_admin_identity_router
        from querygate.api.admin_observability_routes import build_admin_observability_router
        from querygate.api.admin_ui_routes import build_admin_ui_router
        from querygate.api.catalog_governance_routes import build_catalog_governance_router

        cfg = AppConfig(
            environment="localhost", api_v1_prefix="/api/v1", sso_enabled=True, mcp_enabled=False
        )
        builders = (
            build_admin_config_router,
            build_admin_connections_router,
            build_admin_identity_router,
            build_admin_observability_router,
            build_admin_ui_router,
            build_catalog_governance_router,
        )
        checked = 0
        for build in builders:
            router = build(lambda: None, cfg, prefix="/api/v1")
            paths = [route.path for route in router.routes]
            assert paths, f"{build.__name__} produced no routes"
            for path in paths:
                assert (
                    surface_for_path(path, "/api/v1") == CONSOLE_SURFACE
                ), f"{build.__name__} mounts {path}, which the REST policy would govern"
                checked += 1
        assert checked > 20, f"only {checked} admin routes checked — expected the full surface"


class TestEnforcement:
    def _principal(self, method: str) -> Principal:
        return Principal(subject="someone", auth_method=method)

    def test_a_permitted_method_passes(self):
        policy = AuthMethodPolicy({SSO_SESSION_METHOD}, CONSOLE_SURFACE)
        policy.check(self._principal(SSO_SESSION_METHOD))

    def test_a_refused_method_raises_with_a_useful_message(self):
        policy = AuthMethodPolicy({SSO_SESSION_METHOD, JWT_METHOD}, CONSOLE_SURFACE)
        with pytest.raises(AuthMethodNotPermitted) as exc:
            policy.check(self._principal(API_KEY_METHOD))
        assert exc.value.method == API_KEY_METHOD
        # Names what IS accepted, so an operator can act on it.
        assert "jwt" in exc.value.message and "sso_session" in exc.value.message

    def test_the_message_carries_no_credential(self):
        policy = AuthMethodPolicy({SSO_SESSION_METHOD}, CONSOLE_SURFACE)
        with pytest.raises(AuthMethodNotPermitted) as exc:
            policy.check(Principal(subject="alice", auth_method=API_KEY_METHOD))
        assert "alice" not in exc.value.message


class TestOverHttp:
    def _client(self, **overrides) -> TestClient:
        set_identity_store(IdentityConfigStore.from_dict(_IDENTITY))
        kwargs = {
            "environment": "localhost",
            "api_v1_prefix": "/api/v1",
            "sso_enabled": True,
            "api_keys": ["a-static-key"],
            "api_key_scopes": ["admin:config:read", "admin:config:write"],
            "mcp_enabled": False,
        }
        kwargs.update(overrides)
        return TestClient(create_app(AppConfig(**kwargs)))

    def test_a_static_key_is_refused_on_the_console_once_sso_is_on(self):
        client = self._client()
        response = client.get(
            "/api/v1/admin/config/versions", headers={"Authorization": "Bearer a-static-key"}
        )
        assert response.status_code == 403
        assert "not accepted on the admin control plane" in response.json()["detail"]

    def test_the_same_key_still_works_on_the_rest_api(self):
        """Tightening the console must not break service-to-service callers."""
        client = self._client()
        response = client.get(
            "/api/v1/connections", headers={"Authorization": "Bearer a-static-key"}
        )
        assert response.status_code == 200

    def test_an_operator_can_opt_back_in_explicitly(self):
        client = self._client(console_auth_methods=[API_KEY_METHOD, SSO_SESSION_METHOD])
        response = client.get(
            "/api/v1/admin/config/versions", headers={"Authorization": "Bearer a-static-key"}
        )
        assert response.status_code != 403

    def test_a_deployment_without_sso_keeps_its_console_key(self):
        """No silent breakage for anyone who has not adopted SSO."""
        client = self._client(sso_enabled=False)
        response = client.get(
            "/api/v1/admin/config/versions", headers={"Authorization": "Bearer a-static-key"}
        )
        assert response.status_code == 200

    def test_the_rest_api_can_be_tightened_too(self):
        client = self._client(rest_auth_methods=[SSO_SESSION_METHOD])
        response = client.get(
            "/api/v1/connections", headers={"Authorization": "Bearer a-static-key"}
        )
        assert response.status_code == 403

    def test_a_refusal_is_403_not_401(self):
        """401 would invite a retry that can never succeed."""
        client = self._client()
        assert (
            client.get(
                "/api/v1/admin/config/versions",
                headers={"Authorization": "Bearer a-static-key"},
            ).status_code
            == 403
        )
        # A genuinely absent credential is still 401.
        assert client.get("/api/v1/admin/config/versions").status_code == 401


class TestEveryReturnPathIsGoverned:
    """The check must sit on *all* of them, not just the last one.

    `api/auth.py` resolves a principal through up to three paths — session
    cookie, device token, then the synchronous bearer chain — each with its own
    early return. A policy enforced on only the final one would be silently
    bypassable by exactly the credential types SSO introduced, which is the
    opposite of what this control is for. Mutation testing found the session
    path unguarded; these pin both.
    """

    def _client(self, tmp_path, **overrides) -> TestClient:
        users = tmp_path / "users.yaml"
        users.write_text("users: []\n")
        set_identity_store(
            IdentityConfigStore.from_dict(
                {
                    "sso": {"base_url": "http://127.0.0.1"},
                    "providers": [{"id": "local", "kind": "local"}],
                    "mapping": {
                        "rules": [
                            {
                                "provider": "local",
                                "claim": "groups",
                                "equals": "admins",
                                "grant_roles": ["Config Governor"],
                            }
                        ]
                    },
                }
            )
        )
        set_local_user_store(
            LocalUserStore.from_dict(
                {
                    "users": [
                        {
                            "username": "alice",
                            "groups": ["admins"],
                            "password_verifier": hash_password(
                                "correct-horse-battery-staple", ScryptParams(n=2**12, r=8, p=1)
                            ),
                        }
                    ]
                }
            )
        )
        kwargs = {
            "environment": "localhost",
            "api_v1_prefix": "/api/v1",
            "sso_enabled": True,
            "local_idp_enabled": True,
            "sso_session_cookie_secure": False,
            "local_users_file": str(users),
            "mcp_enabled": False,
        }
        kwargs.update(overrides)
        return TestClient(create_app(AppConfig(**kwargs)))

    def _sign_in(self, client: TestClient) -> str:
        response = client.post(
            "/api/v1/auth/local/login",
            json={"username": "alice", "password": "correct-horse-battery-staple"},
        )
        assert response.status_code == 200, response.text
        return response.json()["csrf_token"]

    def test_a_session_is_refused_when_the_policy_excludes_it(self, tmp_path):
        client = self._client(tmp_path, console_auth_methods=[JWT_METHOD])
        csrf = self._sign_in(client)
        response = client.get("/api/v1/admin/config/versions", headers={"X-QueryGate-CSRF": csrf})
        assert response.status_code == 403
        assert "not accepted on the admin control plane" in response.json()["detail"]

    def test_the_same_session_works_when_the_policy_allows_it(self, tmp_path):
        client = self._client(tmp_path, console_auth_methods=[SSO_SESSION_METHOD])
        csrf = self._sign_in(client)
        response = client.get("/api/v1/admin/config/versions", headers={"X-QueryGate-CSRF": csrf})
        assert response.status_code == 200

    def test_a_device_token_is_refused_when_the_policy_excludes_it(self, tmp_path):
        client = self._client(
            tmp_path,
            sso_device_grant_enabled=True,
            rest_auth_methods=[SSO_SESSION_METHOD],
        )
        csrf = self._sign_in(client)
        started = client.post(
            "/api/v1/auth/device/code", json={"client_name": "cli", "scopes": []}
        ).json()
        client.post(
            "/api/v1/auth/device/approve",
            json={"user_code": started["user_code"]},
            headers={"X-QueryGate-CSRF": csrf},
        )
        issued = client.post(
            "/api/v1/auth/device/token", json={"device_code": started["device_code"]}
        ).json()

        fresh = TestClient(client.app)
        response = fresh.get(
            "/api/v1/connections",
            headers={"Authorization": f"Bearer {issued['access_token']}"},
        )
        assert response.status_code == 403


class TestMcpSurface:
    def _middleware(self, **overrides):
        from querygate.mcp.auth import MCPAuthMiddleware

        kwargs = {
            "environment": "localhost",
            "mcp_enabled": True,
            "mcp_api_keys": ["mcp-key"],
            "sso_enabled": True,
        }
        kwargs.update(overrides)
        return MCPAuthMiddleware(app=None, settings=AppConfig(**kwargs))

    def test_mcp_stays_permissive_by_default(self):
        assert self._middleware()._method_policy.permits(API_KEY_METHOD)

    def test_mcp_can_be_restricted_to_idp_issued_credentials(self):
        policy = self._middleware(mcp_auth_methods=[JWT_METHOD, DEVICE_TOKEN_METHOD])._method_policy
        assert policy.permits(JWT_METHOD)
        assert policy.permits(DEVICE_TOKEN_METHOD)
        assert not policy.permits(API_KEY_METHOD)
        assert not policy.permits(ANONYMOUS_METHOD)

    def test_an_unknown_mcp_method_is_refused_at_load(self):
        with pytest.raises(ValueError, match="unknown authentication method"):
            AppConfig(environment="localhost", mcp_auth_methods=["bearer"])
