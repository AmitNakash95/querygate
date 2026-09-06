"""The OpenID Connect authorization-code flow, with PKCE, for browser sign-in.

One implementation serves every provider in `identity/presets.py`; what varies
between IdPs is data (`ProviderPreset`), never control flow. The flow has two
halves:

* `build_authorization_request` mints the `state`, `nonce`, and PKCE verifier,
  stores them server-side as a single-use `LoginFlow`, and returns the URL to
  send the browser to.
* `complete_authorization` takes the callback's `code`, redeems it at the token
  endpoint, and verifies the ID token before anyone is considered signed in.

Everything that makes this flow safe is enforced here rather than assumed:

* **PKCE S256 always** — never `plain`, never omitted, even for a confidential
  client. An intercepted authorization code is useless without the verifier.
* **`state` bound to a server-side flow record**, consumed on first use, so a
  forged or replayed callback matches nothing.
* **`nonce` compared against the ID token**, in constant time, which is what
  stops an ID token obtained elsewhere from being injected into this session.
* **Asymmetric signature algorithms only.** `none` and every HMAC family are
  refused before verification, so a key-confusion token (signed with the
  public key as an HMAC secret) cannot be presented as a valid ID token.
* **`redirect_uri` derived from configuration**, never from the inbound
  request's Host/X-Forwarded-* headers, so a forged host cannot steer a code.

Nothing here returns a token, a code, or a client secret to a caller: the
success value is a verified claim set, and every failure is a
`SsoLoginError` whose message names the *category*, never the token content.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx
import jwt

from querygate.core.logging import get_logger
from querygate.identity.claims import claim_scalar
from querygate.identity.discovery import ProviderMetadata
from querygate.identity.models import IdentityProviderProfile
from querygate.identity.sessions import LoginFlow, new_opaque_token

# Signature algorithms QueryGate will verify an ID token with. Asymmetric only:
# with an HMAC family, the "verification key" is a shared secret, and the JWKS
# public key would serve as one — the classic algorithm-confusion forgery.
ALLOWED_ID_TOKEN_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"}
)
# RFC 7636 allows 43-128 characters; 64 random bytes lands at 86.
CODE_VERIFIER_BYTES = 64
TOKEN_EXCHANGE_TIMEOUT_SECONDS = 15.0
# An authorization-code response is small. A larger body is not one.
MAX_TOKEN_RESPONSE_BYTES = 512 * 1024
CALLBACK_PATH = "/api/v1/auth/sso/callback"


class SsoLoginError(Exception):
    """A login attempt failed. `category` is safe to log and to audit."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


def pkce_pair() -> Tuple[str, str]:
    """(code_verifier, code_challenge) for PKCE S256."""
    verifier = secrets.token_urlsafe(CODE_VERIFIER_BYTES)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def redirect_uri_for(base_url: str) -> str:
    """The one registered redirect URI, derived from configuration only."""
    return base_url.rstrip("/") + CALLBACK_PATH


def build_authorization_request(
    *,
    provider: IdentityProviderProfile,
    metadata: ProviderMetadata,
    base_url: str,
    return_to: str,
    ttl_seconds: float,
) -> Tuple[str, LoginFlow]:
    """Build the IdP authorization URL and the server-side flow record."""
    if metadata.code_challenge_methods_supported and (
        "S256" not in metadata.code_challenge_methods_supported
    ):
        # Refuse rather than downgrade to `plain`: an IdP that cannot do S256 in
        # 2026 is misconfigured, and silently weakening the flow is exactly the
        # kind of accommodation that turns into a finding.
        raise SsoLoginError(
            "pkce_unsupported",
            f"Identity provider {provider.id!r} does not advertise PKCE S256 support.",
        )
    verifier, challenge = pkce_pair()
    now = datetime.now(timezone.utc)
    flow = LoginFlow(
        flow_id=new_opaque_token("qgf_"),
        provider_id=provider.id,
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        code_verifier=verifier,
        redirect_uri=redirect_uri_for(base_url),
        return_to=return_to,
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    params = {
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": flow.redirect_uri,
        "scope": " ".join(provider.authorization_scopes),
        "state": flow.state,
        "nonce": flow.nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{metadata.authorization_endpoint}?{urlencode(params)}", flow


async def complete_authorization(
    *,
    provider: IdentityProviderProfile,
    metadata: ProviderMetadata,
    flow: LoginFlow,
    code: str,
    state: str,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Redeem `code` and return the ID token's verified claims."""
    if not hmac.compare_digest(state, flow.state):
        raise SsoLoginError("state_mismatch", "The sign-in request could not be verified.")
    if flow.provider_id != provider.id:
        raise SsoLoginError("provider_mismatch", "The sign-in request could not be verified.")
    token_response = await _redeem_code(
        provider=provider, metadata=metadata, flow=flow, code=code, client=client
    )
    id_token = token_response.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        raise SsoLoginError(
            "missing_id_token",
            "The identity provider did not return an ID token; check that the `openid` "
            "scope is granted to this application.",
        )
    claims = await asyncio.to_thread(
        verify_id_token,
        id_token=id_token,
        provider=provider,
        metadata=metadata,
        nonce=flow.nonce,
    )
    _enforce_email_domain(provider, claims)
    return claims


async def _redeem_code(
    *,
    provider: IdentityProviderProfile,
    metadata: ProviderMetadata,
    flow: LoginFlow,
    code: str,
    client: Optional[httpx.AsyncClient],
) -> Dict[str, Any]:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": flow.redirect_uri,
        "client_id": provider.client_id,
        "code_verifier": flow.code_verifier,
    }
    auth: Optional[Tuple[str, str]] = None
    if provider.client_secret:
        if "client_secret_basic" in metadata.token_endpoint_auth_methods_supported:
            auth = (provider.client_id, provider.client_secret)
        else:
            form["client_secret"] = provider.client_secret

    try:
        if client is not None:
            response = await client.post(
                metadata.token_endpoint,
                data=form,
                auth=auth,
                timeout=TOKEN_EXCHANGE_TIMEOUT_SECONDS,
            )
        else:
            async with httpx.AsyncClient(
                timeout=TOKEN_EXCHANGE_TIMEOUT_SECONDS, follow_redirects=False
            ) as http_client:
                response = await http_client.post(metadata.token_endpoint, data=form, auth=auth)
    except httpx.HTTPError as exc:
        raise SsoLoginError(
            "token_endpoint_unreachable",
            f"Could not reach the identity provider's token endpoint ({type(exc).__name__}).",
        ) from exc

    if response.status_code != 200:
        # The IdP's error body can echo the submitted parameters, so only the
        # status code is surfaced or logged — never the response text.
        get_logger().warning(
            "identity.sso.token_exchange_failed",
            provider=provider.id,
            status_code=response.status_code,
        )
        raise SsoLoginError(
            "token_exchange_failed",
            "The identity provider rejected the sign-in (authorization code exchange failed).",
        )
    if len(response.content) > MAX_TOKEN_RESPONSE_BYTES:
        raise SsoLoginError(
            "token_response_too_large", "The identity provider's response was too large."
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise SsoLoginError(
            "token_response_invalid", "The identity provider returned a malformed response."
        ) from exc
    if not isinstance(payload, dict):
        raise SsoLoginError(
            "token_response_invalid", "The identity provider returned a malformed response."
        )
    return payload


def allowed_algorithms(provider: IdentityProviderProfile, metadata: ProviderMetadata) -> List[str]:
    """Signature algorithms acceptable for this provider's ID tokens.

    The intersection of what the operator configured, what the IdP advertises,
    and `ALLOWED_ID_TOKEN_ALGORITHMS`. An empty intersection is an error, never
    a fallback to "whatever the token says" — that fallback is the vulnerability.
    """
    configured = [alg for alg in provider.id_token_algorithms if alg in ALLOWED_ID_TOKEN_ALGORITHMS]
    advertised = set(metadata.id_token_signing_alg_values_supported)
    if advertised:
        configured = [alg for alg in configured if alg in advertised]
    return configured


def verify_id_token(
    *,
    id_token: str,
    provider: IdentityProviderProfile,
    metadata: ProviderMetadata,
    nonce: Optional[str],
) -> Dict[str, Any]:
    """Verify signature, issuer, audience, expiry, and nonce. Blocking."""
    algorithms = allowed_algorithms(provider, metadata)
    if not algorithms:
        raise SsoLoginError(
            "no_acceptable_algorithm",
            f"Identity provider {provider.id!r} advertises no ID-token signing algorithm "
            "QueryGate accepts (asymmetric signatures only).",
        )
    try:
        jwk_client = jwt.PyJWKClient(metadata.jwks_uri, cache_keys=True)
        signing_key = jwk_client.get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=algorithms,
            audience=provider.client_id,
            issuer=provider.issuer,
            leeway=provider.leeway_seconds,
            options={"require": ["exp", "iat", "iss", "aud", provider.subject_claim]},
        )
    except jwt.PyJWTError as exc:
        # Never log the token; only why verification failed.
        get_logger().warning("identity.sso.id_token_rejected", provider=provider.id, error=str(exc))
        raise SsoLoginError(
            "id_token_invalid", "The identity provider's token failed verification."
        )

    token_nonce = claims.get("nonce")
    if nonce is not None:
        if not isinstance(token_nonce, str) or not hmac.compare_digest(token_nonce, nonce):
            raise SsoLoginError("nonce_mismatch", "The sign-in could not be verified (nonce).")
    authorized_party = claims.get("azp")
    if isinstance(authorized_party, str) and authorized_party != provider.client_id:
        raise SsoLoginError("azp_mismatch", "The token was issued for a different application.")
    subject = claim_scalar(claims, provider.subject_claim)
    if not subject:
        raise SsoLoginError(
            "missing_subject",
            f"The identity provider's token has no usable {provider.subject_claim!r} claim.",
        )
    return claims


def _enforce_email_domain(provider: IdentityProviderProfile, claims: Dict[str, Any]) -> None:
    if not provider.allowed_email_domains:
        return
    email = (claim_scalar(claims, provider.email_claim) or "").lower()
    verified = claims.get("email_verified")
    if verified is False:
        raise SsoLoginError(
            "email_unverified", "Your identity provider reports this email as unverified."
        )
    domain = email.rpartition("@")[2]
    if not domain or domain not in provider.allowed_email_domains:
        raise SsoLoginError(
            "email_domain_not_allowed",
            "This account's email domain is not permitted to sign in to QueryGate.",
        )


def principal_subject(provider: IdentityProviderProfile, claims: Dict[str, Any]) -> str:
    subject = claim_scalar(claims, provider.subject_claim) or ""
    return f"{provider.subject_prefix}{subject}"
