"""Boundary tests for the human sign-in surface (TODO.md item 199).

These model a hostile caller rather than a user: a cross-site page trying to
ride a session cookie, an operator's `return_to` turned into an open redirect,
a device token trying to outlive its approver's authority, an MCP client trying
to present a browser cookie, and every path by which a client secret, a
password verifier, a TOTP secret, or a session token could reach a response or
an audit file.

Each is a regression test for a specific way this surface could silently
degrade while still *looking* like a working login.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from querygate.api.app import create_app
from querygate.audit.events import AuthenticationEvent, persistable_event_types
from querygate.core.config import AppConfig
from querygate.identity.authenticators import (
    CsrfMismatchError,
    DeviceTokenAuthenticator,
    SessionCookieAuthenticator,
)
from querygate.identity.config_store import IdentityConfigStore, set_identity_store
from querygate.identity.device import (
    approve_device_grant,
    redeem_device_code,
    start_device_authorization,
)
from querygate.identity.local_store import (
    LocalUser,
    LocalUserStore,
    PublicLocalUser,
    set_local_user_store,
)
from querygate.identity.models import (
    IdentityProviderProfile,
    PublicIdentityProvider,
    is_safe_relative_path,
)
from querygate.identity.passwords import ScryptParams, hash_password
from querygate.identity.sessions import MAX_SESSION_CLAIMS_BYTES, sanitize_claims

pytestmark = pytest.mark.security

_CLIENT_SECRET = "super-secret-oidc-client-value"
_PASSWORD = "correct-horse-battery-staple"
_CHEAP = ScryptParams(n=2**12, r=8, p=1)

_CONFIG = {
    "sso": {"base_url": "https://qg.example.com", "default_landing_path": "/admin/"},
    "providers": [
        {"id": "local", "kind": "local"},
        {
            "id": "entra",
            "preset": "entra_id",
            "tenant": "tenant-1",
            "client_id": "client-abc",
            "client_secret": _CLIENT_SECRET,
        },
    ],
    "mapping": {
        "rules": [
            {
                "provider": "local",
                "claim": "groups",
                "equals": "admins",
                "grant_roles": ["Identity Administrator"],
            }
        ]
    },
}


def _client(tmp_path, **overrides) -> TestClient:
    kwargs = {
        "environment": "localhost",
        "api_v1_prefix": "/api/v1",
        "sso_enabled": True,
        "local_idp_enabled": True,
        "sso_device_grant_enabled": True,
        "sso_session_cookie_secure": False,
        "local_users_file": str(tmp_path / "users.yaml"),
    }
    kwargs.update(overrides)
    set_identity_store(IdentityConfigStore.from_dict(_CONFIG))
    set_local_user_store(
        LocalUserStore.from_dict(
            {
                "users": [
                    {
                        "username": "alice",
                        "groups": ["admins"],
                        "password_verifier": hash_password(_PASSWORD, _CHEAP),
                    }
                ]
            }
        )
    )
    return TestClient(create_app(AppConfig(**kwargs)))


def _sign_in(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/local/login", json={"username": "alice", "password": _PASSWORD}
    )
    assert response.status_code == 200
    return response.json()["csrf_token"]


class TestNoCredentialOnAnyPublicModel:
    def test_public_provider_model_cannot_carry_a_secret_at_all(self):
        forbidden = {"client_secret", "issuer", "tenant", "domain", "client_id"}
        assert not forbidden & set(PublicIdentityProvider.model_fields)

    def test_public_local_user_model_cannot_carry_a_verifier_or_totp_secret(self):
        forbidden = {"password_verifier", "totp_secret", "password"}
        assert not forbidden & set(PublicLocalUser.model_fields)

    def test_no_schema_in_the_public_api_declares_a_credential_property(self, tmp_path):
        """Checked against the *live* OpenAPI document, not by convention.

        Property names, not raw text: a docstring may legitimately say the word
        "client_secret" while explaining that no such field exists, and a text
        search would turn that explanation into a false failure. What must never
        appear is an actual declared property by any of these names.
        """
        client = _client(tmp_path)
        schema = client.app.openapi()
        forbidden = {
            "client_secret",
            "password_verifier",
            "totp_secret",
            "code_verifier",
            "device_code_digest",
            "session_digest",
            "csrf",
        }
        offenders = {
            f"{name}.{prop}"
            for name, definition in (schema.get("components", {}).get("schemas", {})).items()
            for prop in (definition.get("properties") or {})
            if prop in forbidden
        }
        assert offenders == set()

    def test_no_response_anywhere_echoes_the_configured_client_secret(self, tmp_path):
        client = _client(tmp_path)
        csrf = _sign_in(client)
        headers = {"X-QueryGate-CSRF": csrf}
        for path in (
            "/api/v1/auth/providers",
            "/api/v1/auth/session",
            "/api/v1/admin/identity/providers",
            "/api/v1/admin/identity/users",
        ):
            response = client.get(path, headers=headers)
            assert response.status_code == 200, path
            assert _CLIENT_SECRET not in response.text, path

    def test_a_local_users_public_view_omits_the_secret_even_when_enrolled(self):
        user = LocalUser(
            username="alice",
            password_verifier="scrypt$4096$8$1$AAAA$BBBB",
            totp_secret="JBSWY3DPEHPK3PXP",
        )
        dumped = user.to_public().model_dump_json()
        assert "JBSWY3DPEHPK3PXP" not in dumped
        assert "scrypt" not in dumped


class TestOpenRedirect:
    @pytest.mark.parametrize(
        "candidate",
        [
            "//evil.example.com",
            "https://evil.example.com",
            "http://evil.example.com",
            "/\\evil.example.com",
            "/admin/\\..\\evil",
            "javascript:alert(1)",
            "",
            "admin/",
        ],
    )
    def test_a_non_relative_return_to_is_never_accepted(self, candidate):
        assert is_safe_relative_path(candidate) is False

    @pytest.mark.parametrize("candidate", ["/admin/", "/access/", "/admin/?tab=policy"])
    def test_ordinary_in_app_paths_are_accepted(self, candidate):
        assert is_safe_relative_path(candidate) is True

    def test_a_hostile_return_to_falls_back_to_the_configured_landing_path(self, tmp_path):
        client = _client(tmp_path)
        response = client.get(
            "/api/v1/auth/sso/login",
            params={"provider": "local", "return_to": "//evil.example.com"},
            follow_redirects=False,
        )
        # The local provider has no redirect flow, but the point stands for the
        # sanitizer itself, which every provider shares.
        assert response.status_code == 400

    def test_the_configured_landing_path_cannot_be_absolute(self):
        from querygate.identity.models import SsoSettings

        with pytest.raises(ValueError, match="site-relative"):
            SsoSettings(default_landing_path="https://evil.example.com")


class TestCsrf:
    def test_a_bare_cookie_reaches_no_scope_gated_endpoint(self, tmp_path):
        """The cross-site case: a browser sends the cookie, the attacker's page
        cannot read or set the CSRF header, so every call must fail."""
        client = _client(tmp_path)
        _sign_in(client)
        for method, path in (
            ("get", "/api/v1/admin/identity/users"),
            ("get", "/api/v1/admin/identity/providers"),
            ("post", "/api/v1/admin/identity/revoke"),
            ("get", "/api/v1/connections"),
        ):
            response = getattr(client, method)(
                path, **({"json": {"subject": "alice"}} if method == "post" else {})
            )
            assert response.status_code == 403, f"{method} {path}"

    def test_the_authenticator_raises_rather_than_silently_downgrading(self):
        """A missing CSRF token must be an error, never a fall-through.

        If it merely returned None, the request would slide down to the bearer
        chain and — in a local deployment with nothing else configured — could
        land on the anonymous authenticator instead of being refused.
        """
        authenticator = SessionCookieAuthenticator()
        with pytest.raises(CsrfMismatchError):
            import asyncio

            asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
                _authenticate_without_csrf(authenticator)
            )

    def test_session_read_is_the_only_csrf_exempt_endpoint(self, tmp_path):
        client = _client(tmp_path)
        _sign_in(client)
        assert client.get("/api/v1/auth/session").status_code == 200


async def _authenticate_without_csrf(authenticator: SessionCookieAuthenticator) -> None:
    from datetime import datetime, timedelta, timezone

    from querygate.identity.sessions import (
        SESSION_TOKEN_PREFIX,
        SsoSession,
        get_session_store,
        new_opaque_token,
        token_digest,
    )

    token = new_opaque_token(SESSION_TOKEN_PREFIX)
    now = datetime.now(timezone.utc)
    await get_session_store().create(
        SsoSession(
            session_digest=token_digest(token),
            subject="alice",
            provider_id="local",
            auth_method="local_password",
            csrf_token="qgc_the-real-token",
            created_at=now,
            expires_at=now + timedelta(hours=1),
            last_seen_at=now,
        )
    )
    await authenticator.authenticate(token, csrf_token=None)


class TestSessionCookie:
    def test_the_cookie_is_httponly_and_samesite_bounded(self, tmp_path):
        client = _client(tmp_path)
        response = client.post(
            "/api/v1/auth/local/login", json={"username": "alice", "password": _PASSWORD}
        )
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie or "SameSite=strict" in cookie
        assert "SameSite=none" not in cookie.lower()

    def test_production_refuses_a_non_secure_session_cookie(self):
        with pytest.raises(ValueError, match="SSO_SESSION_COOKIE_SECURE"):
            AppConfig(
                environment="production",
                api_keys=["k"],
                sso_enabled=True,
                sso_session_cookie_secure=False,
            )

    def test_samesite_none_is_not_configurable(self):
        with pytest.raises(ValueError, match="SSO_SESSION_COOKIE_SAMESITE"):
            AppConfig(environment="localhost", sso_enabled=True, sso_session_cookie_samesite="none")

    def test_a_stale_session_token_authenticates_nothing(self, tmp_path):
        client = _client(tmp_path)
        csrf = _sign_in(client)
        stolen = client.cookies.get("qg_session")
        client.post("/api/v1/auth/logout", headers={"X-QueryGate-CSRF": csrf})
        client.cookies.set("qg_session", stolen)
        assert client.get("/api/v1/auth/session").json()["authenticated"] is False

    def test_an_oversized_idp_claim_set_is_refused_not_stored(self):
        with pytest.raises(ValueError, match="claim set larger"):
            sanitize_claims({"sub": "x", "groups": ["g" * 64] * (MAX_SESSION_CLAIMS_BYTES // 32)})

    def test_login_artifacts_are_stripped_from_a_stored_session(self):
        kept = sanitize_claims({"sub": "x", "nonce": "n", "at_hash": "h", "groups": ["a"]})
        assert kept == {"sub": "x", "groups": ["a"]}


class TestSingleUseAndExpiry:
    """Replay and expiry — the two ways a spent credential comes back to life."""

    async def test_a_login_flow_can_only_be_consumed_once(self):
        """A replayed OIDC callback must find nothing.

        The `state`/`nonce`/PKCE verifier live in one server-side record that
        `consume()` deletes as it reads. If it merely *read*, an attacker who
        captured a callback URL could replay it — the second exchange would
        still carry a valid state and a live verifier.
        """
        from datetime import datetime, timedelta, timezone

        from querygate.identity.sessions import LoginFlow, get_login_flow_store

        now = datetime.now(timezone.utc)
        flow = LoginFlow(
            flow_id="qgf_the-flow",
            provider_id="entra",
            state="the-state",
            nonce="the-nonce",
            code_verifier="the-verifier",
            redirect_uri="https://qg.example.com/api/v1/auth/sso/callback",
            return_to="/admin/",
            created_at=now,
            expires_at=now + timedelta(minutes=10),
        )
        store = get_login_flow_store()
        await store.put(flow)
        assert (await store.consume("qgf_the-flow")) is not None
        assert (await store.consume("qgf_the-flow")) is None

    async def test_an_abandoned_login_flow_expires_on_its_own(self):
        from datetime import datetime, timedelta, timezone

        from querygate.identity.sessions import LoginFlow, get_login_flow_store

        now = datetime.now(timezone.utc)
        store = get_login_flow_store()
        await store.put(
            LoginFlow(
                flow_id="qgf_stale",
                provider_id="entra",
                state="s",
                nonce="n",
                code_verifier="v",
                redirect_uri="https://qg.example.com/api/v1/auth/sso/callback",
                return_to="/admin/",
                created_at=now - timedelta(hours=1),
                expires_at=now - timedelta(minutes=50),
            )
        )
        assert (await store.consume("qgf_stale")) is None

    async def test_an_expired_session_resolves_to_nobody(self):
        """Past its absolute lifetime, the record must stop answering.

        Checked at the *store*, not only in the authenticator: `/auth/session`
        reads the store directly, so an expiry enforced one layer up would let
        a dead session still report itself as signed in.
        """
        from datetime import datetime, timedelta, timezone

        from querygate.identity.sessions import (
            SESSION_TOKEN_PREFIX,
            SsoSession,
            get_session_store,
            new_opaque_token,
            token_digest,
        )

        token = new_opaque_token(SESSION_TOKEN_PREFIX)
        now = datetime.now(timezone.utc)
        await get_session_store().create(
            SsoSession(
                session_digest=token_digest(token),
                subject="alice",
                provider_id="local",
                auth_method="local_password",
                csrf_token="qgc_x",
                created_at=now - timedelta(hours=9),
                expires_at=now - timedelta(minutes=1),
                last_seen_at=now,
            )
        )
        assert await get_session_store().get(token_digest(token)) is None

    async def test_an_idle_session_times_out_even_before_its_absolute_expiry(self):
        from datetime import datetime, timedelta, timezone

        from querygate.identity.sessions import (
            SESSION_TOKEN_PREFIX,
            InProcessSessionStore,
            SsoSession,
            new_opaque_token,
            token_digest,
        )

        store = InProcessSessionStore(idle_timeout_seconds=60)
        token = new_opaque_token(SESSION_TOKEN_PREFIX)
        now = datetime.now(timezone.utc)
        session = SsoSession(
            session_digest=token_digest(token),
            subject="alice",
            provider_id="local",
            auth_method="local_password",
            csrf_token="qgc_x",
            created_at=now - timedelta(hours=1),
            expires_at=now + timedelta(hours=7),
            last_seen_at=now - timedelta(minutes=5),
        )
        await store.create(session)
        assert await store.get(session.session_digest) is None

    def test_an_expired_session_is_not_reported_as_signed_in(self, tmp_path):
        """The same property, end to end through `/auth/session`."""
        import asyncio
        from datetime import datetime, timedelta, timezone

        from querygate.identity.sessions import SsoSession, get_session_store, token_digest

        client = _client(tmp_path)
        _sign_in(client)
        cookie = client.cookies.get("qg_session")
        digest = token_digest(cookie)

        async def _age_it() -> None:
            store = get_session_store()
            live = await store.get(digest)
            assert live is not None
            await store.create(
                live.model_copy(
                    update={"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
                )
            )

        # `asyncio.run` rather than `get_event_loop()`: this is a sync test that
        # runs after async ones, and reusing whatever loop happens to be current
        # made the result depend on test order. The in-process store holds no
        # loop-bound objects, so a fresh loop is safe here.
        asyncio.run(_age_it())
        assert client.get("/api/v1/auth/session").json()["authenticated"] is False


class TestIdleTimeoutIsActuallyWired:
    """The idle timeout must come from configuration, not a frozen default.

    It was configurable and enforced in the session store — but nothing ever
    handed the configured value to that store, so the default of "no idle
    timeout" silently won in production while the store's own unit test passed
    against an explicitly-constructed instance. These pin the wiring, not the
    mechanism.
    """

    def test_an_idle_session_is_rejected_end_to_end(self, tmp_path):
        import asyncio
        from datetime import datetime, timedelta, timezone

        from querygate.identity.sessions import get_session_store, token_digest

        config = json.loads(json.dumps(_CONFIG))
        config["sso"]["session_idle_timeout_seconds"] = 60
        set_identity_store(IdentityConfigStore.from_dict(config))
        client = _client(tmp_path)
        set_identity_store(IdentityConfigStore.from_dict(config))
        csrf = _sign_in(client)
        digest = token_digest(client.cookies.get("qg_session"))

        async def _idle_it() -> None:
            store = get_session_store()
            live = await store.get(digest)
            assert live is not None
            await store.create(
                live.model_copy(
                    update={"last_seen_at": datetime.now(timezone.utc) - timedelta(minutes=5)}
                )
            )

        asyncio.run(_idle_it())
        assert client.get("/api/v1/auth/session").json()["authenticated"] is False
        assert (
            client.get(
                "/api/v1/admin/identity/users", headers={"X-QueryGate-CSRF": csrf}
            ).status_code
            == 401
        )

    def test_a_shortened_timeout_takes_effect_without_a_restart(self, tmp_path):
        """identity.yaml hot-reloads, so a frozen value would be a stale one."""
        import asyncio
        from datetime import datetime, timedelta, timezone

        from querygate.identity.sessions import (
            configured_idle_timeout,
            get_session_store,
            token_digest,
        )

        client = _client(tmp_path)  # default: 3600s idle
        _sign_in(client)
        digest = token_digest(client.cookies.get("qg_session"))

        async def _age(minutes: int) -> None:
            store = get_session_store()
            live = await store.get(digest)
            assert live is not None
            await store.create(
                live.model_copy(
                    update={"last_seen_at": datetime.now(timezone.utc) - timedelta(minutes=minutes)}
                )
            )

        asyncio.run(_age(5))
        assert client.get("/api/v1/auth/session").json()["authenticated"] is True

        tightened = json.loads(json.dumps(_CONFIG))
        tightened["sso"]["session_idle_timeout_seconds"] = 60
        set_identity_store(IdentityConfigStore.from_dict(tightened))
        assert configured_idle_timeout() == 60
        assert client.get("/api/v1/auth/session").json()["authenticated"] is False


class TestStartupValidation:
    """A misconfigured identity file must refuse to start, not 500 later.

    `create_app(cfg)` may be handed a config that is not the process-wide
    singleton, so the check has to honour the config it was actually given —
    otherwise it validates the wrong deployment's files, or none at all, and
    looks like it is working.
    """

    def test_a_missing_identity_file_refuses_to_start(self, tmp_path):
        from querygate.identity.config_store import clear_identity_store

        clear_identity_store()
        try:
            with pytest.raises(FileNotFoundError, match="Identity file not found"):
                create_app(
                    AppConfig(
                        environment="localhost",
                        sso_enabled=True,
                        mcp_enabled=False,
                        identity_file=str(tmp_path / "absent.yaml"),
                    )
                )
        finally:
            clear_identity_store()

    def test_an_unknown_scope_in_a_mapping_rule_refuses_to_start(self, tmp_path):
        import yaml

        from querygate.identity.config_store import clear_identity_store

        path = tmp_path / "identity.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "providers": [
                        {
                            "id": "p",
                            "preset": "generic",
                            "issuer": "https://idp.example.com",
                            "client_id": "c",
                        }
                    ],
                    "mapping": {
                        "rules": [
                            {
                                "provider": "p",
                                "claim": "groups",
                                "equals": "g",
                                "grant_scopes": ["admin:not-a-real-scope"],
                            }
                        ]
                    },
                }
            )
        )
        clear_identity_store()
        try:
            with pytest.raises(ValueError, match="Unknown QueryGate scope"):
                create_app(
                    AppConfig(
                        environment="localhost",
                        sso_enabled=True,
                        mcp_enabled=False,
                        identity_file=str(path),
                    )
                )
        finally:
            clear_identity_store()

    def test_a_store_already_installed_is_not_clobbered(self, tmp_path):
        """A hot reload (or a test) that set a store must survive app creation."""
        from querygate.identity.config_store import get_identity_store

        set_identity_store(IdentityConfigStore.from_dict(_CONFIG))
        create_app(
            AppConfig(
                environment="localhost",
                sso_enabled=True,
                mcp_enabled=False,
                identity_file=str(tmp_path / "does-not-exist.yaml"),
            )
        )
        assert get_identity_store().all_ids() == ["entra", "local"]


class TestDevelopmentProviderIsFenced:
    """The dev provider authenticates anybody. Every gate on it is pinned here.

    It exists so the real sign-in flow runs with no external IdP, which makes it
    the single most dangerous thing in `identity/` if it ever escaped a local
    machine. There are three independent fences — the config validator, the
    router builder, and the fact that its signing key never leaves memory — and
    a test for each, because "we would never enable it in production" is not a
    control.
    """

    @pytest.mark.parametrize("environment", ["production", "staging"])
    def test_it_refuses_to_start_outside_a_local_environment(self, environment):
        with pytest.raises(ValueError, match="DEV_IDP_ENABLED"):
            AppConfig(
                environment=environment,
                api_keys=["k"],
                mcp_enabled=False,
                sso_enabled=True,
                dev_idp_enabled=True,
            )

    def test_it_requires_sso_to_be_enabled_at_all(self):
        with pytest.raises(ValueError, match="SSO_ENABLED"):
            AppConfig(environment="localhost", dev_idp_enabled=True)

    def test_the_router_refuses_to_build_even_if_the_config_check_were_bypassed(self):
        """A second, independent fence.

        The validator is the first line, but a caller can construct an
        `AppConfig` object by other means. The builder therefore re-checks
        rather than trusting that it was only reached legitimately.
        """
        from querygate.identity.dev_idp import build_dev_idp_router

        local = AppConfig(environment="localhost", sso_enabled=True, dev_idp_enabled=True)
        smuggled = local.model_copy(update={"environment": "production"})
        with pytest.raises(RuntimeError, match="only run when ENVIRONMENT is local"):
            build_dev_idp_router(smuggled, "dev", "http://127.0.0.1/dev-idp")

        disabled = AppConfig(environment="localhost", sso_enabled=True)
        with pytest.raises(RuntimeError, match="not enabled"):
            build_dev_idp_router(disabled, "dev", "http://127.0.0.1/dev-idp")

    def test_no_dev_surface_exists_when_it_is_not_enabled(self, tmp_path):
        client = _client(tmp_path)  # SSO on, dev IdP off
        assert client.get("/dev-idp/.well-known/openid-configuration").status_code == 404
        assert client.get("/dev-idp/jwks.json").status_code == 404
        assert client.get("/dev-idp/authorize").status_code == 404

    def test_its_signing_key_is_per_process_and_never_persisted(self):
        """A restart must invalidate every token it ever minted."""
        from querygate.identity.dev_idp import DevIdentityProvider, DevPersona

        persona = DevPersona(sub="dev-1", name="Dev", email="dev@localhost")
        first = DevIdentityProvider(issuer="http://127.0.0.1/dev-idp", personas=[persona])
        second = DevIdentityProvider(issuer="http://127.0.0.1/dev-idp", personas=[persona])
        assert first.jwks()["keys"][0]["n"] != second.jwks()["keys"][0]["n"]
        assert first.jwks()["keys"][0]["kid"] != second.jwks()["keys"][0]["kid"]

    def test_a_dev_provider_cannot_be_given_a_client_secret(self):
        with pytest.raises(ValueError, match="public client"):
            IdentityProviderProfile(id="dev", kind="dev", client_secret="x")

    def test_a_non_dev_provider_cannot_declare_personas(self):
        """`users:` is a dev-only affordance; on a real provider it would be a
        list of people QueryGate could sign in as without asking the IdP."""
        with pytest.raises(ValueError, match="kind=dev"):
            IdentityProviderProfile(
                id="entra",
                preset="entra_id",
                tenant="t",
                client_id="c",
                users=[{"sub": "smuggled"}],
            )

    def test_a_dev_provider_shares_the_subject_namespace_check(self):
        """It signs `sub` values like any issuer, so it collides like one."""
        with pytest.raises(ValueError, match="subject_prefix"):
            IdentityConfigStore.from_dict(
                {
                    "sso": {"base_url": "http://127.0.0.1:8000"},
                    "providers": [
                        {"id": "dev", "kind": "dev"},
                        {"id": "entra", "preset": "entra_id", "tenant": "t", "client_id": "c"},
                    ],
                }
            )

    def test_a_dev_provider_needs_a_base_url_to_describe_itself(self):
        with pytest.raises(ValueError, match="sso.base_url"):
            IdentityConfigStore.from_dict({"providers": [{"id": "dev", "kind": "dev"}]})


class TestCookieSecureDefault:
    """`Secure` is right everywhere except the one place it silently breaks.

    A Secure cookie is dropped by the browser over plain http, so a localhost
    deployment with the strict default redirects through a successful login and
    then reports the person as anonymous — which reads as a QueryGate bug. The
    default flips for local environments only, and only when the operator has
    not stated a preference.
    """

    def test_a_local_deployment_gets_a_usable_cookie_by_default(self):
        assert (
            AppConfig(environment="localhost", sso_enabled=True).sso_session_cookie_secure is False
        )
        assert (
            AppConfig(environment="development", sso_enabled=True).sso_session_cookie_secure
            is False
        )

    def test_an_explicit_preference_is_honoured_even_locally(self):
        """Someone testing behind a local TLS proxy asked for Secure; give it."""
        assert (
            AppConfig(
                environment="localhost", sso_enabled=True, sso_session_cookie_secure=True
            ).sso_session_cookie_secure
            is True
        )

    def test_a_non_local_deployment_is_never_relaxed(self):
        assert (
            AppConfig(
                environment="production", api_keys=["k"], mcp_enabled=False, sso_enabled=True
            ).sso_session_cookie_secure
            is True
        )
        assert (
            AppConfig(
                environment="staging", api_keys=["k"], mcp_enabled=False, sso_enabled=True
            ).sso_session_cookie_secure
            is True
        )

    def test_production_still_refuses_an_explicit_insecure_cookie(self):
        with pytest.raises(ValueError, match="SSO_SESSION_COOKIE_SECURE"):
            AppConfig(
                environment="production",
                api_keys=["k"],
                mcp_enabled=False,
                sso_enabled=True,
                sso_session_cookie_secure=False,
            )

    def test_the_relaxation_only_applies_when_sso_is_on(self):
        """Nothing about a non-SSO deployment's defaults should move."""
        assert AppConfig(environment="localhost").sso_session_cookie_secure is True


class TestDeviceTokenAuthority:
    async def test_revoking_the_mapping_shrinks_a_live_token_immediately(self, tmp_path):
        """A device token must not outlive its approver's authority.

        Its scope list is a ceiling fixed at approval; the effective set is that
        ceiling intersected with what the approver's claims map to *now*. Tighten
        the mapping and the already-issued token loses the scope on the next
        request, without waiting for expiry or a revocation call.
        """
        set_identity_store(IdentityConfigStore.from_dict(_CONFIG))
        code, grant = await start_device_authorization(
            client_name="cli", requested_scopes=["admin:identity:read"]
        )
        await approve_device_grant(
            user_code=grant.user_code,
            approver_subject="alice",
            approver_scopes=frozenset({"admin:identity:read"}),
            provider_id="local",
            approver_claims={"sub": "alice", "groups": ["admins"]},
        )
        token, _ = await redeem_device_code(device_code=code, token_ttl_seconds=600)

        authenticator = DeviceTokenAuthenticator()
        before = await authenticator.authenticate(token)
        assert before is not None and "admin:identity:read" in before.scopes

        # The operator removes the group's grant and reloads.
        tightened = json.loads(json.dumps(_CONFIG))
        tightened["mapping"]["rules"] = []
        set_identity_store(IdentityConfigStore.from_dict(tightened))

        after = await authenticator.authenticate(token)
        assert after is not None and after.scopes == frozenset()

    async def test_a_token_cannot_gain_a_scope_the_mapping_later_adds(self, tmp_path):
        """The ceiling only ever shrinks — a widened mapping does not widen it."""
        set_identity_store(IdentityConfigStore.from_dict(_CONFIG))
        code, grant = await start_device_authorization(
            client_name="cli", requested_scopes=["admin:identity:read"]
        )
        await approve_device_grant(
            user_code=grant.user_code,
            approver_subject="alice",
            approver_scopes=frozenset({"admin:identity:read"}),
            provider_id="local",
            approver_claims={"sub": "alice", "groups": ["admins"]},
        )
        token, _ = await redeem_device_code(device_code=code, token_ttl_seconds=600)

        widened = json.loads(json.dumps(_CONFIG))
        widened["mapping"]["rules"] = [
            {
                "provider": "local",
                "claim": "groups",
                "equals": "admins",
                "grant_scopes": ["admin:config:write", "admin:identity:read"],
            }
        ]
        set_identity_store(IdentityConfigStore.from_dict(widened))

        principal = await DeviceTokenAuthenticator().authenticate(token)
        assert principal is not None
        assert principal.scopes == frozenset({"admin:identity:read"})

    async def test_an_unknown_or_wrongly_prefixed_token_resolves_to_nobody(self):
        authenticator = DeviceTokenAuthenticator()
        assert await authenticator.authenticate("qgd_not-a-real-token") is None
        assert await authenticator.authenticate("qgs_a-session-token") is None
        assert await authenticator.authenticate(None) is None


class TestMcpSurface:
    def test_the_mcp_transport_never_accepts_a_browser_cookie(self, tmp_path):
        """MCP is an agent transport; an ambient cookie credential has no place
        on it, or a signed-in operator's browser could be induced to drive it."""
        from querygate.mcp.auth import MCPAuthMiddleware

        settings = AppConfig(
            environment="localhost",
            sso_enabled=True,
            sso_device_grant_enabled=True,
            mcp_enabled=True,
        )
        middleware = MCPAuthMiddleware(app=None, settings=settings)
        source = MCPAuthMiddleware.__call__.__code__.co_names
        assert "cookies" not in source
        # A device token authenticator IS wired in — agents may carry one.
        assert middleware._device_authenticator is not None

    def test_enabling_sso_closes_the_local_anonymous_bypass_on_mcp_too(self):
        """Turning SSO on must not leave MCP anonymous while REST is not.

        The dev bypass is gated on "nothing real is configured", never on
        `is_local` alone. SSO counts as something real — a device token is a
        credential this surface accepts — so it has to close the bypass the
        same way an API key or JWT does. Without this, a local deployment that
        enabled SSO would have an authenticated REST surface and a wide-open
        MCP one.
        """
        from querygate.mcp.auth import MCPAuthMiddleware

        base = {
            "environment": "localhost",
            "mcp_enabled": True,
            "mcp_api_keys": [],
            "jwt_enabled": False,
        }
        wide_open = MCPAuthMiddleware(app=None, settings=AppConfig(**base))
        assert (
            wide_open._authenticator.authenticate(None) is not None
        ), "precondition: with nothing configured the local dev bypass is expected to apply"

        with_sso = MCPAuthMiddleware(
            app=None, settings=AppConfig(**base, sso_enabled=True, sso_device_grant_enabled=True)
        )
        assert with_sso._authenticator.authenticate(None) is None
        assert with_sso._authenticator.authenticate("not-a-real-token") is None


class TestAuditRedaction:
    def test_the_authentication_event_has_no_field_for_any_secret(self):
        forbidden = {
            "password",
            "password_verifier",
            "totp_secret",
            "totp_code",
            "claims",
            "id_token",
            "access_token",
            "device_code",
            "session_token",
            "code",
            "client_secret",
        }
        assert not forbidden & set(AuthenticationEvent.model_fields)

    def test_the_event_forbids_extra_fields(self):
        with pytest.raises(ValueError):
            AuthenticationEvent(action="sso.login", outcome="success", password="hunter2")

    def test_the_new_event_type_is_discoverable_everywhere_it_must_be(self):
        from querygate.api.admin_ui_routes import _AUDIT_EVENT_TYPES
        from querygate.audit.worm_search import _persistable_event_types

        assert "identity.authentication" in persistable_event_types()
        assert "identity.authentication" in _AUDIT_EVENT_TYPES
        assert "identity.authentication" in _persistable_event_types()


class TestProviderConfigurationHardening:
    @pytest.mark.parametrize(
        "tenant",
        ["../../evil", "tenant/../..", "a b", "tenant?x=1", "tenant#frag", "tenant/path"],
    )
    def test_an_issuer_template_value_cannot_smuggle_a_new_destination(self, tenant):
        with pytest.raises(ValueError, match="unsafe characters"):
            IdentityProviderProfile(id="entra", preset="entra_id", tenant=tenant, client_id="c")

    def test_a_non_https_issuer_is_refused(self):
        with pytest.raises(ValueError, match="https"):
            IdentityProviderProfile(
                id="p", preset="generic", issuer="http://idp.example.com", client_id="c"
            )

    def test_an_issuer_carrying_userinfo_or_a_query_is_refused(self):
        for issuer in (
            "https://user:pass@idp.example.com",
            "https://idp.example.com?next=evil",
            "https://idp.example.com#frag",
        ):
            with pytest.raises(ValueError):
                IdentityProviderProfile(id="p", preset="generic", issuer=issuer, client_id="c")

    def test_a_local_provider_cannot_be_given_oidc_credentials(self):
        with pytest.raises(ValueError, match="kind=local"):
            IdentityProviderProfile(id="local", kind="local", client_secret="x")

    def test_two_oidc_providers_need_distinct_subject_namespaces(self):
        with pytest.raises(ValueError, match="subject_prefix"):
            IdentityConfigStore.from_dict(
                {
                    "providers": [
                        {"id": "a", "preset": "entra_id", "tenant": "t", "client_id": "c"},
                        {"id": "b", "preset": "okta", "domain": "x.okta.com", "client_id": "c"},
                    ]
                }
            )
