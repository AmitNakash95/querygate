"""Unit tests for JwtAuthenticator — signature/claims verification is
exercised against a real RSA keypair and real `jwt.encode`/`jwt.decode`,
but the JWKS *endpoint* itself is mocked (get_signing_key_from_jwt), so no
real network call or IdP is involved.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from querygate.core.auth import CompositeAuthenticator
from querygate.core.jwt_auth import JwtAuthenticator, build_jwt_authenticator

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_KEY = _PRIVATE_KEY.public_key()

_OTHER_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _make_token(claims: dict, *, private_key=None) -> str:
    return jwt.encode(claims, private_key or _PRIVATE_KEY, algorithm="RS256")


def _authenticator(**overrides) -> JwtAuthenticator:
    return JwtAuthenticator(
        jwks_url="https://idp.example.com/.well-known/jwks.json",
        issuer=overrides.pop("issuer", "https://idp.example.com/"),
        audience=overrides.pop("audience", "querygate"),
        **overrides,
    )


@pytest.fixture(autouse=True)
def _mock_jwks():
    # Patches the jwt.PyJWKClient *class* method, not a JwtAuthenticator
    # instance attribute — JwtAuthenticator.__init__ constructs a real
    # PyJWKClient (no network call happens until a key is actually
    # requested), so patching has to intercept that call regardless of
    # which instance makes it.
    with patch.object(
        jwt.PyJWKClient,
        "get_signing_key_from_jwt",
        lambda self, token: SimpleNamespace(key=_PUBLIC_KEY),
    ):
        yield


def test_valid_token_maps_claims_to_principal():
    auth = _authenticator()
    token = _make_token(
        {
            "sub": "user-123",
            "iss": "https://idp.example.com/",
            "aud": "querygate",
            "scope": "read:orders write:orders",
            "tenant_id": "acme",
        }
    )
    principal = auth.authenticate(token)
    assert principal is not None
    assert principal.subject == "user-123"
    assert principal.scopes == frozenset({"read:orders", "write:orders"})
    assert principal.claims["tenant_id"] == "acme"
    assert principal.auth_method == "jwt"


def test_scopes_claim_as_list():
    auth = _authenticator()
    token = _make_token(
        {
            "sub": "user-123",
            "iss": "https://idp.example.com/",
            "aud": "querygate",
            "scope": ["read:orders", "write:orders"],
        }
    )
    principal = auth.authenticate(token)
    assert principal.scopes == frozenset({"read:orders", "write:orders"})


def test_missing_scopes_claim_yields_empty_scopes():
    auth = _authenticator()
    token = _make_token({"sub": "user-123", "iss": "https://idp.example.com/", "aud": "querygate"})
    principal = auth.authenticate(token)
    assert principal.scopes == frozenset()


def test_none_bearer_token_returns_none():
    auth = _authenticator()
    assert auth.authenticate(None) is None


def test_empty_bearer_token_returns_none():
    auth = _authenticator()
    assert auth.authenticate("") is None


def test_garbage_token_returns_none():
    auth = _authenticator()
    assert auth.authenticate("not-a-jwt") is None


def test_wrong_signature_is_rejected():
    auth = _authenticator()
    token = _make_token(
        {"sub": "user-123", "iss": "https://idp.example.com/", "aud": "querygate"},
        private_key=_OTHER_PRIVATE_KEY,
    )
    assert auth.authenticate(token) is None


def test_expired_token_is_rejected():
    auth = _authenticator()
    token = _make_token(
        {
            "sub": "user-123",
            "iss": "https://idp.example.com/",
            "aud": "querygate",
            "exp": int(time.time()) - 60,
        }
    )
    assert auth.authenticate(token) is None


def test_wrong_audience_is_rejected():
    auth = _authenticator()
    token = _make_token(
        {"sub": "user-123", "iss": "https://idp.example.com/", "aud": "some-other-service"}
    )
    assert auth.authenticate(token) is None


def test_wrong_issuer_is_rejected():
    auth = _authenticator()
    token = _make_token({"sub": "user-123", "iss": "https://evil.example.com/", "aud": "querygate"})
    assert auth.authenticate(token) is None


def test_missing_subject_claim_is_rejected():
    auth = _authenticator()
    token = _make_token({"iss": "https://idp.example.com/", "aud": "querygate"})
    assert auth.authenticate(token) is None


def test_custom_subject_and_scopes_claims():
    auth = _authenticator(subject_claim="user_id", scopes_claim="permissions")
    token = _make_token(
        {
            "user_id": "user-456",
            "iss": "https://idp.example.com/",
            "aud": "querygate",
            "permissions": "admin:reload-config",
        }
    )
    principal = auth.authenticate(token)
    assert principal.subject == "user-456"
    assert principal.scopes == frozenset({"admin:reload-config"})


def test_composite_authenticator_falls_back_to_jwt():
    from querygate.core.auth import ApiKeyAuthenticator

    api_key_auth = ApiKeyAuthenticator(api_keys=["secret-key"], subject="svc")
    jwt_auth = _authenticator()
    composite = CompositeAuthenticator([api_key_auth, jwt_auth])

    token = _make_token({"sub": "user-123", "iss": "https://idp.example.com/", "aud": "querygate"})
    principal = composite.authenticate(token)
    assert principal is not None
    assert principal.subject == "user-123"

    api_key_principal = composite.authenticate("secret-key")
    assert api_key_principal is not None
    assert api_key_principal.subject == "svc"

    assert composite.authenticate("neither-a-key-nor-a-jwt") is None


def test_build_jwt_authenticator_returns_none_when_disabled():
    from querygate.core.config import AppConfig

    cfg = AppConfig(jwt_enabled=False)
    assert build_jwt_authenticator(cfg) is None


def test_build_jwt_authenticator_builds_from_config():
    from querygate.core.config import AppConfig

    cfg = AppConfig(
        jwt_enabled=True,
        jwt_jwks_url="https://idp.example.com/.well-known/jwks.json",
        jwt_issuer="https://idp.example.com/",
        jwt_audience="querygate",
        jwt_subject_claim="user_id",
    )
    authenticator = build_jwt_authenticator(cfg)
    assert isinstance(authenticator, JwtAuthenticator)
    assert authenticator._subject_claim == "user_id"
