"""The OIDC authorization-code flow: PKCE, discovery trust, ID-token verification.

These are the tests that matter most in `identity/`: everything here is a check
that, if it silently stopped happening, would still look like a working login.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from querygate.identity.discovery import (
    DiscoveryError,
    ProviderMetadata,
    discovery_url,
    parse_metadata,
)
from querygate.identity.models import IdentityProviderProfile
from querygate.identity.oidc import (
    ALLOWED_ID_TOKEN_ALGORITHMS,
    SsoLoginError,
    allowed_algorithms,
    build_authorization_request,
    complete_authorization,
    pkce_pair,
    principal_subject,
    redirect_uri_for,
    verify_id_token,
)

pytestmark = pytest.mark.unit

_ISSUER = "https://login.microsoftonline.com/tenant-1/v2.0"
_CLIENT_ID = "client-abc"
_BASE_URL = "https://querygate.example.com"

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_PEM = (
    _PRIVATE_KEY.public_key()
    .public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    .decode()
)
_PRIVATE_PEM = _PRIVATE_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()


class _StubJwkClient:
    """Stands in for `PyJWKClient` so no network call is made."""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def get_signing_key_from_jwt(self, _token):
        class _Key:
            key = _PUBLIC_PEM

        return _Key()


@pytest.fixture(autouse=True)
def _stub_jwks(monkeypatch):
    monkeypatch.setattr(jwt, "PyJWKClient", _StubJwkClient)


def _provider(**overrides) -> IdentityProviderProfile:
    fields: Dict[str, Any] = {
        "id": "entra",
        "preset": "entra_id",
        "tenant": "tenant-1",
        "client_id": _CLIENT_ID,
        "client_secret": "shh",
    }
    fields.update(overrides)
    return IdentityProviderProfile(**fields)


def _metadata(**overrides) -> ProviderMetadata:
    fields: Dict[str, Any] = {
        "issuer": _ISSUER,
        "authorization_endpoint": "https://idp.example.com/authorize",
        "token_endpoint": "https://idp.example.com/token",
        "jwks_uri": "https://idp.example.com/jwks",
        "end_session_endpoint": None,
        "id_token_signing_alg_values_supported": ("RS256",),
        "code_challenge_methods_supported": ("S256",),
        "token_endpoint_auth_methods_supported": ("client_secret_post",),
    }
    fields.update(overrides)
    return ProviderMetadata(**fields)


def _id_token(*, algorithm: str = "RS256", key: str | None = None, **claim_overrides) -> str:
    now = int(time.time())
    claims: Dict[str, Any] = {
        "iss": _ISSUER,
        "aud": _CLIENT_ID,
        "sub": "user-1",
        "iat": now,
        "exp": now + 300,
        "nonce": "the-nonce",
        "email": "alice@example.com",
        "name": "Alice",
        "groups": ["group-1"],
    }
    claims.update(claim_overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key or _PRIVATE_PEM, algorithm=algorithm)


def _forge_hs256_with(secret: str) -> str:
    """Hand-build an HS256 token keyed on `secret`.

    PyJWT refuses to *encode* HMAC with a PEM public key, so the attack has to
    be assembled byte-by-byte — which is exactly what an attacker would do. The
    token below is a well-formed HS256 JWT whose MAC key is the IdP's public
    key, i.e. a value QueryGate itself would hand to `jwt.decode` as the
    verification key if an HMAC algorithm were ever accepted.
    """
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {
        "iss": _ISSUER,
        "aud": _CLIENT_ID,
        "sub": "attacker",
        "iat": now,
        "exp": now + 300,
        "nonce": "the-nonce",
    }

    def _segment(payload: dict) -> bytes:
        return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(
            b"="
        )

    signing_input = _segment(header) + b"." + _segment(claims)
    signature = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    ).rstrip(b"=")
    return (signing_input + b"." + signature).decode()


class TestPkce:
    def test_challenge_is_the_s256_of_the_verifier(self):
        verifier, challenge = pkce_pair()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .decode()
            .rstrip("=")
        )
        assert challenge == expected

    def test_verifier_length_is_within_rfc_7636_bounds(self):
        verifier, _ = pkce_pair()
        assert 43 <= len(verifier) <= 128

    def test_each_pair_is_fresh(self):
        assert pkce_pair()[0] != pkce_pair()[0]


class TestAuthorizationRequest:
    def test_url_carries_state_nonce_and_s256_challenge(self):
        url, flow = build_authorization_request(
            provider=_provider(),
            metadata=_metadata(),
            base_url=_BASE_URL,
            return_to="/admin/",
            ttl_seconds=600,
        )
        params = parse_qs(urlsplit(url).query)
        assert params["response_type"] == ["code"]
        assert params["code_challenge_method"] == ["S256"]
        assert params["state"] == [flow.state]
        assert params["nonce"] == [flow.nonce]
        assert params["client_id"] == [_CLIENT_ID]
        assert "openid" in params["scope"][0]

    def test_redirect_uri_comes_from_configuration_not_the_request(self):
        url, flow = build_authorization_request(
            provider=_provider(),
            metadata=_metadata(),
            base_url=_BASE_URL,
            return_to="/admin/",
            ttl_seconds=600,
        )
        expected = f"{_BASE_URL}/api/v1/auth/sso/callback"
        assert flow.redirect_uri == expected == redirect_uri_for(_BASE_URL)
        assert parse_qs(urlsplit(url).query)["redirect_uri"] == [expected]

    def test_the_secret_never_appears_in_the_authorization_url(self):
        url, _ = build_authorization_request(
            provider=_provider(client_secret="super-secret-value"),
            metadata=_metadata(),
            base_url=_BASE_URL,
            return_to="/admin/",
            ttl_seconds=600,
        )
        assert "super-secret-value" not in url

    def test_an_idp_without_s256_is_refused_rather_than_downgraded(self):
        with pytest.raises(SsoLoginError) as exc:
            build_authorization_request(
                provider=_provider(),
                metadata=_metadata(code_challenge_methods_supported=("plain",)),
                base_url=_BASE_URL,
                return_to="/admin/",
                ttl_seconds=600,
            )
        assert exc.value.category == "pkce_unsupported"

    def test_flow_expires(self):
        _, flow = build_authorization_request(
            provider=_provider(),
            metadata=_metadata(),
            base_url=_BASE_URL,
            return_to="/admin/",
            ttl_seconds=600,
        )
        assert not flow.is_expired()
        assert flow.is_expired(datetime.now(timezone.utc) + timedelta(seconds=601))


class TestDiscovery:
    def test_well_known_url_tolerates_a_trailing_slash(self):
        assert discovery_url("https://a/") == discovery_url("https://a")

    def test_issuer_must_match_exactly(self):
        with pytest.raises(DiscoveryError, match="issuer mismatch"):
            parse_metadata(_ISSUER, {"issuer": _ISSUER + "/"})

    def test_non_https_endpoints_are_refused(self):
        document = {
            "issuer": _ISSUER,
            "authorization_endpoint": "http://evil.example.com/authorize",
            "token_endpoint": "https://idp.example.com/token",
            "jwks_uri": "https://idp.example.com/jwks",
        }
        with pytest.raises(DiscoveryError, match="non-https authorization_endpoint"):
            parse_metadata(_ISSUER, document)

    def test_a_missing_endpoint_is_an_error_not_a_default(self):
        with pytest.raises(DiscoveryError, match="missing token_endpoint"):
            parse_metadata(
                _ISSUER,
                {
                    "issuer": _ISSUER,
                    "authorization_endpoint": "https://idp.example.com/authorize",
                    "jwks_uri": "https://idp.example.com/jwks",
                },
            )

    def test_localhost_http_is_allowed_for_a_dev_idp(self):
        metadata = parse_metadata(
            "http://localhost:8080/realms/qg",
            {
                "issuer": "http://localhost:8080/realms/qg",
                "authorization_endpoint": "http://localhost:8080/authorize",
                "token_endpoint": "http://localhost:8080/token",
                "jwks_uri": "http://localhost:8080/jwks",
            },
        )
        assert metadata.token_endpoint.startswith("http://localhost")


class TestIdTokenVerification:
    def test_a_valid_token_is_accepted(self):
        claims = verify_id_token(
            id_token=_id_token(),
            provider=_provider(),
            metadata=_metadata(),
            nonce="the-nonce",
        )
        assert claims["sub"] == "user-1"

    @pytest.mark.parametrize(
        "override",
        [
            {"iss": "https://attacker.example.com"},
            {"aud": "some-other-client"},
            {"exp": int(time.time()) - 10},
        ],
        ids=["wrong-issuer", "wrong-audience", "expired"],
    )
    def test_core_claim_checks_reject(self, override):
        with pytest.raises(SsoLoginError) as exc:
            verify_id_token(
                id_token=_id_token(**override),
                provider=_provider(leeway_seconds=0),
                metadata=_metadata(),
                nonce="the-nonce",
            )
        assert exc.value.category == "id_token_invalid"

    def test_nonce_mismatch_is_rejected(self):
        with pytest.raises(SsoLoginError) as exc:
            verify_id_token(
                id_token=_id_token(nonce="a-different-nonce"),
                provider=_provider(),
                metadata=_metadata(),
                nonce="the-nonce",
            )
        assert exc.value.category == "nonce_mismatch"

    def test_a_token_with_no_nonce_at_all_is_rejected(self):
        with pytest.raises(SsoLoginError) as exc:
            verify_id_token(
                id_token=_id_token(nonce=None),
                provider=_provider(),
                metadata=_metadata(),
                nonce="the-nonce",
            )
        assert exc.value.category == "nonce_mismatch"

    def test_azp_for_another_application_is_rejected(self):
        with pytest.raises(SsoLoginError) as exc:
            verify_id_token(
                id_token=_id_token(azp="a-different-client"),
                provider=_provider(),
                metadata=_metadata(),
                nonce="the-nonce",
            )
        assert exc.value.category == "azp_mismatch"

    def test_algorithm_confusion_with_the_public_key_as_an_hmac_secret_is_rejected(self):
        """The classic forgery: sign HS256 with the IdP's *public* key.

        If HMAC families were ever accepted, this token would verify, because
        the verification key handed to `jwt.decode` is that same public PEM.
        """
        forged = _forge_hs256_with(_PUBLIC_PEM)
        with pytest.raises(SsoLoginError) as exc:
            verify_id_token(
                id_token=forged,
                provider=_provider(id_token_algorithms=["RS256", "HS256"]),
                metadata=_metadata(id_token_signing_alg_values_supported=("RS256", "HS256")),
                nonce="the-nonce",
            )
        assert exc.value.category == "id_token_invalid"

    def test_no_hmac_or_none_algorithm_is_ever_acceptable(self):
        assert not any(alg.startswith("HS") or alg == "none" for alg in ALLOWED_ID_TOKEN_ALGORITHMS)

    def test_an_empty_algorithm_intersection_is_an_error_not_a_fallback(self):
        provider = _provider(id_token_algorithms=["RS256"])
        metadata = _metadata(id_token_signing_alg_values_supported=("ES512",))
        assert allowed_algorithms(provider, metadata) == []
        with pytest.raises(SsoLoginError) as exc:
            verify_id_token(
                id_token=_id_token(), provider=provider, metadata=metadata, nonce="the-nonce"
            )
        assert exc.value.category == "no_acceptable_algorithm"

    def test_the_rejection_message_never_contains_the_token(self):
        token = _id_token(iss="https://attacker.example.com")
        with pytest.raises(SsoLoginError) as exc:
            verify_id_token(
                id_token=token, provider=_provider(), metadata=_metadata(), nonce="the-nonce"
            )
        assert token not in str(exc.value)

    def test_subject_prefix_namespaces_the_principal(self):
        provider = _provider(subject_prefix="entra:")
        claims = verify_id_token(
            id_token=_id_token(), provider=provider, metadata=_metadata(), nonce="the-nonce"
        )
        assert principal_subject(provider, claims) == "entra:user-1"
        assert principal_subject(_provider(), claims) == "user-1"


class TestCallbackCompletion:
    async def test_state_mismatch_is_refused_before_any_code_exchange(self):
        _, flow = build_authorization_request(
            provider=_provider(),
            metadata=_metadata(),
            base_url=_BASE_URL,
            return_to="/admin/",
            ttl_seconds=600,
        )

        class _ExplodingClient:
            async def post(self, *_a, **_k):  # pragma: no cover - must not run
                raise AssertionError("the code must not be redeemed on a state mismatch")

        with pytest.raises(SsoLoginError) as exc:
            await complete_authorization(
                provider=_provider(),
                metadata=_metadata(),
                flow=flow,
                code="auth-code",
                state="not-the-state",
                client=_ExplodingClient(),
            )
        assert exc.value.category == "state_mismatch"

    async def test_a_flow_started_for_another_provider_is_refused(self):
        _, flow = build_authorization_request(
            provider=_provider(id="entra"),
            metadata=_metadata(),
            base_url=_BASE_URL,
            return_to="/admin/",
            ttl_seconds=600,
        )
        other = _provider(id="okta", preset="okta", domain="x.okta.com", tenant="")
        with pytest.raises(SsoLoginError) as exc:
            await complete_authorization(
                provider=other,
                metadata=_metadata(),
                flow=flow,
                code="auth-code",
                state=flow.state,
            )
        assert exc.value.category == "provider_mismatch"

    async def test_a_token_endpoint_error_body_is_not_surfaced(self):
        _, flow = build_authorization_request(
            provider=_provider(),
            metadata=_metadata(),
            base_url=_BASE_URL,
            return_to="/admin/",
            ttl_seconds=600,
        )

        class _ErrorResponse:
            status_code = 400
            content = b'{"error":"invalid_grant","code":"the-authorization-code"}'

            def json(self):  # pragma: no cover - not reached
                return {}

        class _Client:
            async def post(self, *_a, **_k):
                return _ErrorResponse()

        with pytest.raises(SsoLoginError) as exc:
            await complete_authorization(
                provider=_provider(),
                metadata=_metadata(),
                flow=flow,
                code="the-authorization-code",
                state=flow.state,
                client=_Client(),
            )
        assert exc.value.category == "token_exchange_failed"
        assert "the-authorization-code" not in str(exc.value)

    async def test_a_successful_exchange_returns_verified_claims(self):
        _, flow = build_authorization_request(
            provider=_provider(),
            metadata=_metadata(),
            base_url=_BASE_URL,
            return_to="/admin/",
            ttl_seconds=600,
        )
        token = _id_token(nonce=flow.nonce)
        sent: Dict[str, Any] = {}

        class _Response:
            status_code = 200
            content = b"{}"

            def json(self):
                return {"id_token": token, "access_token": "at", "token_type": "Bearer"}

        class _Client:
            async def post(self, url, data=None, auth=None, timeout=None):
                sent.update({"url": url, "data": data, "auth": auth})
                return _Response()

        claims = await complete_authorization(
            provider=_provider(),
            metadata=_metadata(),
            flow=flow,
            code="auth-code",
            state=flow.state,
            client=_Client(),
        )
        assert claims["sub"] == "user-1"
        # PKCE verifier is always sent, and the redirect URI is echoed back.
        assert sent["data"]["code_verifier"] == flow.code_verifier
        assert sent["data"]["redirect_uri"] == flow.redirect_uri
        assert sent["data"]["client_secret"] == "shh"

    async def test_basic_auth_is_used_when_the_idp_advertises_it(self):
        metadata = _metadata(token_endpoint_auth_methods_supported=("client_secret_basic",))
        _, flow = build_authorization_request(
            provider=_provider(),
            metadata=metadata,
            base_url=_BASE_URL,
            return_to="/admin/",
            ttl_seconds=600,
        )
        token = _id_token(nonce=flow.nonce)
        sent: Dict[str, Any] = {}

        class _Response:
            status_code = 200
            content = b"{}"

            def json(self):
                return {"id_token": token}

        class _Client:
            async def post(self, url, data=None, auth=None, timeout=None):
                sent.update({"data": data, "auth": auth})
                return _Response()

        await complete_authorization(
            provider=_provider(),
            metadata=metadata,
            flow=flow,
            code="auth-code",
            state=flow.state,
            client=_Client(),
        )
        assert sent["auth"] == (_CLIENT_ID, "shh")
        assert "client_secret" not in sent["data"]

    async def test_an_email_outside_the_allowed_domains_is_refused(self):
        provider = _provider(allowed_email_domains=["example.com"])
        _, flow = build_authorization_request(
            provider=provider,
            metadata=_metadata(),
            base_url=_BASE_URL,
            return_to="/admin/",
            ttl_seconds=600,
        )
        token = _id_token(nonce=flow.nonce, email="mallory@evil.example")

        class _Response:
            status_code = 200
            content = b"{}"

            def json(self):
                return {"id_token": token}

        class _Client:
            async def post(self, *_a, **_k):
                return _Response()

        with pytest.raises(SsoLoginError) as exc:
            await complete_authorization(
                provider=provider,
                metadata=_metadata(),
                flow=flow,
                code="auth-code",
                state=flow.state,
                client=_Client(),
            )
        assert exc.value.category == "email_domain_not_allowed"
