"""The complete OIDC redirect flow, driven end to end with no external IdP.

Every other SSO test exercises a piece: the PKCE pair, the ID-token checks, the
session store, the routes. This one drives what a person actually does —
`/auth/sso/login` → the provider's authorize endpoint → the callback → a live
session → a scope-gated call — through QueryGate's *real* OIDC implementation,
with `identity/dev_idp.py` standing in for the identity provider.

That is the gap this closes. The redirect path could previously only be
verified in parts, because verifying it whole meant registering an application
with a real IdP. The development provider signs real RS256 tokens against a
real JWKS and verifies the PKCE challenge on exchange, so nothing on
QueryGate's side is stubbed or bypassed: only *who vouches for the human* is
make-believe.

It runs against a real uvicorn server rather than `TestClient`, deliberately.
QueryGate talks to the development provider the way it talks to any other one —
outbound HTTP for the discovery document, the token exchange, and the JWKS
fetch. An in-process short-circuit would make the test pass while proving
nothing about the code path a real deployment takes.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Iterator
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import uvicorn

from querygate.api.app import create_app
from querygate.core.config import AppConfig
from querygate.identity.config_store import IdentityConfigStore, set_identity_store

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


PORT = _free_port()
# Loopback, not a hostname: the issuer validator only tolerates plain http for a
# genuine loopback host, and that rule is not one to relax for the development
# provider — a dev issuer on a non-loopback origin is exactly the mistake worth
# failing on.
BASE_URL = f"http://127.0.0.1:{PORT}"
API = f"{BASE_URL}/api/v1"

_CONFIG = {
    "sso": {"base_url": BASE_URL, "default_landing_path": "/admin/"},
    "providers": [{"id": "dev", "kind": "dev"}],
    "mapping": {
        "rules": [
            {
                "provider": "dev",
                "claim": "groups",
                "equals": "platform",
                "grant_roles": ["Identity Administrator", "Operator"],
                "description": "Platform on-call",
            },
            {
                "provider": "dev",
                "claim": "realm_access.roles",
                "any_of": ["qg-approver"],
                "grant_roles": ["Query Approver"],
                "description": "Query approver (nested claim)",
            },
        ]
    },
}


@pytest.fixture(scope="module")
def server() -> Iterator[None]:
    set_identity_store(IdentityConfigStore.from_dict(_CONFIG))
    app = create_app(
        AppConfig(
            environment="localhost",
            api_v1_prefix="/api/v1",
            sso_enabled=True,
            dev_idp_enabled=True,
            sso_session_cookie_secure=False,
            mcp_enabled=False,
            health_check_interval_seconds=3600,
        )
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    running = uvicorn.Server(config)
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not running.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert running.started, "the test server did not start"
    try:
        yield
    finally:
        running.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def client(server: None) -> Iterator[httpx.Client]:
    # `tests/conftest.py`'s autouse reset empties the identity store before every
    # test, and the server thread reads it per request — so re-install it here.
    set_identity_store(IdentityConfigStore.from_dict(_CONFIG))
    with httpx.Client(base_url=BASE_URL, follow_redirects=False, timeout=15) as http:
        yield http


def _start_login(client: httpx.Client, return_to: str = "/admin/") -> str:
    response = client.get(
        "/api/v1/auth/sso/login", params={"provider": "dev", "return_to": return_to}
    )
    assert response.status_code == 302, response.text
    return response.headers["location"]


def _sign_in_as(client: httpx.Client, persona: str, return_to: str = "/admin/") -> str:
    """Run the whole flow and return the final redirect location."""
    authorize_url = _start_login(client, return_to)
    picked = client.get(f"{authorize_url}&persona={persona}")
    assert picked.status_code == 302, picked.text
    callback = client.get(picked.headers["location"])
    assert callback.status_code == 302, callback.text
    return callback.headers["location"]


class TestProviderSurface:
    def test_discovery_is_a_real_document_for_its_own_issuer(self, client: httpx.Client):
        document = client.get("/dev-idp/.well-known/openid-configuration").json()
        assert document["issuer"] == f"{BASE_URL}/dev-idp"
        assert document["code_challenge_methods_supported"] == ["S256"]
        assert document["id_token_signing_alg_values_supported"] == ["RS256"]
        # Marked, so nobody mistakes this for a real identity provider.
        assert document["querygate_development_provider"] is True

    def test_jwks_publishes_an_rsa_signing_key(self, client: httpx.Client):
        key = client.get("/dev-idp/jwks.json").json()["keys"][0]
        assert key["kty"] == "RSA" and key["alg"] == "RS256" and key["use"] == "sig"

    def test_the_picker_warns_that_it_authenticates_anyone(self, client: httpx.Client):
        page = client.get(_start_login(client)).text
        assert "Development identity provider." in page
        assert "vouches for" in page

    def test_personas_are_derived_from_the_mapping_with_no_configuration(
        self, client: httpx.Client
    ):
        page = client.get(_start_login(client)).text
        assert "Platform on-call" in page
        assert "Query approver (nested claim)" in page
        # And one that matches nothing, so deny-by-default is one click away.
        assert "Unmapped person" in page


class TestFullRedirectFlow:
    def test_a_person_signs_in_and_holds_exactly_their_mapped_scopes(self, client: httpx.Client):
        assert _sign_in_as(client, "dev-1") == "/admin/"
        session = client.get("/api/v1/auth/session").json()
        assert session["authenticated"] is True
        assert session["subject"] == "dev-1"
        assert session["provider_id"] == "dev"
        assert "admin:identity:write" in session["scopes"]
        assert "admin:reload-config" in session["scopes"]
        # Not granted by the rule that matched.
        assert "query:approve" not in session["scopes"]

    def test_the_session_authenticates_a_scope_gated_call(self, client: httpx.Client):
        _sign_in_as(client, "dev-1")
        csrf = client.get("/api/v1/auth/session").json()["csrf_token"]
        response = client.get(
            "/api/v1/admin/identity/providers", headers={"X-QueryGate-CSRF": csrf}
        )
        assert response.status_code == 200

    def test_a_nested_claim_rule_matches_through_the_real_token(self, client: httpx.Client):
        """Keycloak-shaped `realm_access.roles` has to survive the round trip.

        The persona's claims go into a signed ID token, come back through
        verification, are stored on the session, and are read by a dotted claim
        path — so this pins the whole chain, not just `claim_values`.
        """
        _sign_in_as(client, "dev-2")
        session = client.get("/api/v1/auth/session").json()
        assert session["scopes"] == ["query:approve"]

    def test_deny_by_default_survives_the_round_trip(self, client: httpx.Client):
        _sign_in_as(client, "dev-unmapped")
        session = client.get("/api/v1/auth/session").json()
        assert session["authenticated"] is True
        assert session["scopes"] == []
        response = client.get(
            "/api/v1/admin/identity/providers",
            headers={"X-QueryGate-CSRF": session["csrf_token"]},
        )
        assert response.status_code == 403

    def test_return_to_is_honoured_when_it_is_site_relative(self, client: httpx.Client):
        assert _sign_in_as(client, "dev-1", return_to="/access/") == "/access/"

    def test_a_hostile_return_to_falls_back_to_the_landing_path(self, client: httpx.Client):
        assert _sign_in_as(client, "dev-1", return_to="//evil.example.com") == "/admin/"


class TestFlowHardening:
    def test_replaying_the_callback_is_refused(self, client: httpx.Client):
        authorize_url = _start_login(client)
        picked = client.get(f"{authorize_url}&persona=dev-1")
        callback_url = picked.headers["location"]
        assert client.get(callback_url).headers["location"] == "/admin/"
        replayed = client.get(callback_url)
        assert "sso_error=" in replayed.headers["location"]

    def test_a_callback_whose_state_was_tampered_with_is_refused(self, client: httpx.Client):
        authorize_url = _start_login(client)
        picked = client.get(f"{authorize_url}&persona=dev-1")
        parts = urlsplit(picked.headers["location"])
        query = parse_qs(parts.query)
        tampered = f"{parts.path}?code={query['code'][0]}&state=not-the-state"
        response = client.get(tampered)
        assert response.headers["location"] == "/admin/?sso_error=state_mismatch"

    def test_a_replayed_code_exchange_is_refused_over_http(self, client: httpx.Client):
        """End-to-end shape of the replay. See the unit test for the precise one.

        This exercise cannot supply the *correct* PKCE verifier — QueryGate keeps
        it server-side — so a failure here could be either single-use or PKCE.
        `tests/unit/test_dev_idp.py` pins single-use on its own, with the real
        verifier, so the two together say which check is doing the work.
        """
        authorize_url = _start_login(client)
        picked = client.get(f"{authorize_url}&persona=dev-1")
        code = parse_qs(urlsplit(picked.headers["location"]).query)["code"][0]
        client.get(picked.headers["location"])
        second = client.post(
            "/dev-idp/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": "irrelevant",
                "redirect_uri": f"{BASE_URL}/api/v1/auth/sso/callback",
                "client_id": "querygate-dev-idp",
            },
        )
        assert second.status_code == 400 and second.json()["error"] == "invalid_grant"

    def test_the_provider_verifies_pkce_for_real(self, client: httpx.Client):
        """A dev provider that skipped PKCE would hide a client that never sent
        a verifier — the exact class of bug it exists to surface early."""
        authorize_url = _start_login(client)
        picked = client.get(f"{authorize_url}&persona=dev-1")
        code = parse_qs(urlsplit(picked.headers["location"]).query)["code"][0]
        response = client.post(
            "/dev-idp/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": "a-verifier-that-does-not-match-the-challenge",
                "redirect_uri": f"{BASE_URL}/api/v1/auth/sso/callback",
                "client_id": "querygate-dev-idp",
            },
        )
        assert response.status_code == 400
        assert response.json()["error_description"] == "PKCE verification failed."

    def test_the_provider_refuses_an_unregistered_redirect_uri(self, client: httpx.Client):
        authorize_url = _start_login(client)
        parts = urlsplit(authorize_url)
        query = parse_qs(parts.query)
        query["redirect_uri"] = ["http://evil.example.com/steal"]
        rebuilt = parts.path + "?" + "&".join(f"{k}={v[0]}" for k, v in query.items())
        response = client.get(rebuilt)
        assert response.status_code == 400

    def test_the_provider_refuses_a_request_without_pkce(self, client: httpx.Client):
        authorize_url = _start_login(client)
        parts = urlsplit(authorize_url)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        query.pop("code_challenge")
        query["code_challenge_method"] = "plain"
        rebuilt = parts.path + "?" + "&".join(f"{k}={v}" for k, v in query.items())
        assert client.get(rebuilt).status_code == 400

    def test_an_unknown_persona_is_refused(self, client: httpx.Client):
        authorize_url = _start_login(client)
        response = client.get(f"{authorize_url}&persona=dev-nonexistent")
        assert response.status_code == 400
