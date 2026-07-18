"""Unit tests for core.auth — Principal identity and API-key authentication."""

from __future__ import annotations

from querygate.core.auth import (
    AnonymousAuthenticator,
    ApiKeyAuthenticator,
    CompositeAuthenticator,
    Principal,
    extract_bearer_token,
)


def test_principal_defaults_to_empty_scopes_and_claims():
    principal = Principal(subject="svc")
    assert principal.scopes == frozenset()
    assert principal.claims == {}
    assert principal.auth_method == "unknown"


def test_principal_is_frozen():
    principal = Principal(subject="svc")
    try:
        principal.subject = "other"  # type: ignore[misc]
        assert False, "Principal must be immutable"
    except AttributeError:
        pass


def test_api_key_authenticator_attaches_configured_scopes():
    auth = ApiKeyAuthenticator(api_keys=["secret"], subject="svc", scopes=["read:orders"])
    principal = auth.authenticate("secret")
    assert principal is not None
    assert principal.subject == "svc"
    assert principal.scopes == frozenset({"read:orders"})
    assert principal.auth_method == "api_key"


def test_api_key_authenticator_rejects_wrong_token():
    auth = ApiKeyAuthenticator(api_keys=["secret"], subject="svc", scopes=["read:orders"])
    assert auth.authenticate("wrong") is None


def test_api_key_authenticator_returns_none_when_no_keys_configured():
    """No keys configured means "nothing to check here", not "accept
    anything" — accepting anything would make it impossible for a
    CompositeAuthenticator to ever reach a later authenticator (e.g. JWT)
    in the chain. See AnonymousAuthenticator for the actual dev-bypass.
    """
    auth = ApiKeyAuthenticator(api_keys=[], subject="svc")
    assert auth.authenticate(None) is None
    assert auth.authenticate("anything") is None


def test_anonymous_authenticator_always_succeeds():
    auth = AnonymousAuthenticator()
    principal = auth.authenticate(None)
    assert principal is not None
    assert principal.subject == "anonymous-dev"
    assert principal.scopes == frozenset()
    assert principal.auth_method == "anonymous"

    assert auth.authenticate("some-token").subject == "anonymous-dev"


def test_composite_authenticator_tries_real_credentials_before_anonymous_fallback():
    """The bug this guards against: if AnonymousAuthenticator (or an empty
    ApiKeyAuthenticator that accepted anything) ran first, a real JWT would
    never be checked — every request would resolve to "anonymous-dev"
    regardless of what token was actually sent.
    """
    api_key_auth = ApiKeyAuthenticator(api_keys=["secret-key"], subject="svc")
    composite = CompositeAuthenticator([api_key_auth, AnonymousAuthenticator()])

    matched = composite.authenticate("secret-key")
    assert matched.subject == "svc"

    fallback = composite.authenticate("not-the-configured-key")
    assert fallback.subject == "anonymous-dev"


def test_extract_bearer_token():
    assert extract_bearer_token("Bearer abc123") == "abc123"
    assert extract_bearer_token("abc123") is None
    assert extract_bearer_token(None) is None
