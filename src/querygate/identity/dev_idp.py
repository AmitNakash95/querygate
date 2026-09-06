"""A local, throwaway OpenID Connect provider — so the real flow runs offline.

Wiring an IdP is the slowest part of evaluating SSO: register an application,
get a client secret, add a redirect URI, configure a groups claim, wait for a
tenant admin. That cost falls on the person who least benefits from paying it —
a developer changing a policy screen, or someone evaluating QueryGate for the
first hour.

So QueryGate can serve its own. `dev_idp.py` publishes a real discovery
document, a real JWKS, a real authorization endpoint, and a real token endpoint
at `<base_url>/dev-idp`, signs real RS256 ID tokens with a key generated at
process start, and **verifies the PKCE challenge** on exchange. Nothing about
`identity/oidc.py` changes or is bypassed: the browser takes the same redirect,
the callback runs the same `state`/`nonce`/signature/audience checks, and a
session is established through the same code path. The only thing that is fake
is *who vouches for the human* — this provider vouches for anyone who clicks a
button.

That is, stated plainly, an authentication bypass, and it is treated as one:

* it refuses to exist unless `AppConfig.is_local` **and** `DEV_IDP_ENABLED`;
* `ENVIRONMENT=production` refuses to start with it enabled
  (`core/config.py`'s validator), so it cannot be shipped by accident;
* its signing key is generated per process and never written anywhere, so a
  token it minted is worthless the moment the process restarts;
* it announces itself loudly in the log at startup and on every page it serves.

It grants nothing on its own. A dev persona still earns scopes only through
`identity/mapping.py`, deny-by-default, exactly as an Entra group would — which
is what makes it useful for *checking a mapping* rather than merely for getting
past a login screen.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from urllib.parse import urlencode

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from querygate.core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from querygate.core.config import AppConfig
    from querygate.identity.config_store import IdentityConfigStore

# The path the dev provider is served from, relative to `sso.base_url`.
MOUNT_PATH = "/dev-idp"
# Fixed, and not a secret: this provider is a public client and its whole
# security model is "only reachable on a local deployment that opted in".
DEV_CLIENT_ID = "querygate-dev-idp"
AUTHORIZATION_CODE_TTL_SECONDS = 120.0
ID_TOKEN_TTL_SECONDS = 300
MAX_PENDING_CODES = 256
# A persona whose group set matches no mapping rule, so the deny-by-default
# behaviour is one click away rather than something you have to construct.
UNMAPPED_PERSONA_SUB = "dev-unmapped"


@dataclass(frozen=True)
class DevPersona:
    """One clickable identity. `claims` is merged into the ID token verbatim."""

    sub: str
    name: str
    email: str
    claims: Dict[str, Any] = field(default_factory=dict)
    describes: str = ""


@dataclass
class _PendingCode:
    persona: DevPersona
    code_challenge: str
    nonce: str
    redirect_uri: str
    issued_at: float


class DevIdentityProvider:
    """Signs ID tokens for a fixed set of make-believe people."""

    def __init__(self, *, issuer: str, personas: List[DevPersona]) -> None:
        self._issuer = issuer.rstrip("/")
        self._personas = personas
        # Generated per process, held only in memory, never written to disk.
        # A restart invalidates every token this provider ever minted, which is
        # the property that makes a leaked dev token uninteresting.
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._kid = secrets.token_hex(8)
        self._codes: Dict[str, _PendingCode] = {}

    @property
    def issuer(self) -> str:
        return self._issuer

    @property
    def personas(self) -> List[DevPersona]:
        return list(self._personas)

    def persona(self, sub: str) -> Optional[DevPersona]:
        return next((p for p in self._personas if p.sub == sub), None)

    # --- OIDC surface ----------------------------------------------------

    def discovery_document(self) -> Dict[str, Any]:
        return {
            "issuer": self._issuer,
            "authorization_endpoint": f"{self._issuer}/authorize",
            "token_endpoint": f"{self._issuer}/token",
            "jwks_uri": f"{self._issuer}/jwks.json",
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["openid", "profile", "email", "groups"],
            # Not part of the OIDC spec — a marker so nobody mistakes this
            # document for a real identity provider's.
            "querygate_development_provider": True,
        }

    def jwks(self) -> Dict[str, Any]:
        numbers = self._key.public_key().public_numbers()

        def _b64(value: int) -> str:
            raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
            return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": self._kid,
                    "n": _b64(numbers.n),
                    "e": _b64(numbers.e),
                }
            ]
        }

    def issue_code(
        self, *, persona: DevPersona, code_challenge: str, nonce: str, redirect_uri: str
    ) -> str:
        self._prune_codes()
        if len(self._codes) >= MAX_PENDING_CODES:
            self._codes.clear()
        code = secrets.token_urlsafe(24)
        self._codes[code] = _PendingCode(
            persona=persona,
            code_challenge=code_challenge,
            nonce=nonce,
            redirect_uri=redirect_uri,
            issued_at=time.monotonic(),
        )
        return code

    def redeem_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> str:
        """Verify PKCE and the redirect URI, then mint an ID token. Single use."""
        pending = self._codes.pop(code, None)
        if pending is None or time.monotonic() - pending.issued_at > AUTHORIZATION_CODE_TTL_SECONDS:
            raise DevIdpError("invalid_grant", "Unknown or expired authorization code.")
        if pending.redirect_uri != redirect_uri:
            raise DevIdpError(
                "invalid_grant", "redirect_uri does not match the authorization request."
            )
        digest = hashlib.sha256((code_verifier or "").encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        # Checked for real. A dev provider that skipped PKCE would let the flow
        # "work" locally while masking a client that never sent a verifier —
        # exactly the class of bug this provider exists to surface early.
        if not hmac.compare_digest(expected, pending.code_challenge):
            raise DevIdpError("invalid_grant", "PKCE verification failed.")
        return self._id_token(pending.persona, pending.nonce)

    def _id_token(self, persona: DevPersona, nonce: str) -> str:
        now = datetime.now(timezone.utc)
        claims: Dict[str, Any] = {
            "iss": self._issuer,
            "aud": DEV_CLIENT_ID,
            "azp": DEV_CLIENT_ID,
            "sub": persona.sub,
            "name": persona.name,
            "email": persona.email,
            "email_verified": True,
            "nonce": nonce,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=ID_TOKEN_TTL_SECONDS)).timestamp()),
            # Present in every token this provider mints, so an audit reader can
            # tell a development sign-in from a real one at a glance.
            "querygate_dev_identity": True,
        }
        claims.update(persona.claims)
        pem = self._key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": self._kid})

    def _prune_codes(self) -> None:
        now = time.monotonic()
        for code in [
            c for c, p in self._codes.items() if now - p.issued_at > AUTHORIZATION_CODE_TTL_SECONDS
        ]:
            self._codes.pop(code, None)


class DevIdpError(Exception):
    def __init__(self, error: str, message: str) -> None:
        super().__init__(message)
        self.error = error
        self.message = message


def nest_claim(path: str, value: Any) -> Dict[str, Any]:
    """Expand a dotted claim path into the nested object an IdP would emit.

    `identity/claims.py` reads `realm_access.roles` by walking two levels of
    nesting, because that is the shape Keycloak actually sends. A persona built
    with a *flat* key literally named "realm_access.roles" would therefore match
    nothing — the mapping rule that inspired the persona would not fire on the
    persona derived from it, and the dev provider would quietly mis-model every
    nested-claim IdP. Found by the end-to-end flow test, which is precisely the
    kind of defect only a whole round trip surfaces.
    """
    segments = [segment for segment in path.split(".") if segment]
    if not segments:
        return {}
    nested: Any = value
    for segment in reversed(segments[1:]):
        nested = {segment: nested}
    return {segments[0]: nested}


def derive_personas(store: "IdentityConfigStore", provider_id: str) -> List[DevPersona]:
    """Build one persona per mapping rule that could apply to this provider.

    Zero-configuration is the point: an operator who has written a mapping
    already described the populations they care about, so the dev provider
    offers exactly those — "sign in as somebody in the platform group" — plus
    one persona that matches nothing, so deny-by-default is one click away
    instead of something you have to construct by hand.

    An explicit `users:` list in identity.yaml overrides this entirely.
    """
    personas: List[DevPersona] = []
    seen: set[tuple[str, str]] = set()
    for rule in store.mapping.rules:
        if rule.provider not in (provider_id, "*"):
            continue
        for value in rule.match_values:
            key = (rule.claim, value)
            if key in seen:
                continue
            seen.add(key)
            granted = sorted(rule.granted_scopes)
            index = len(personas) + 1
            personas.append(
                DevPersona(
                    sub=f"dev-{index}",
                    name=rule.description or f"{rule.claim}={value}",
                    email=f"dev-{index}@localhost",
                    claims=nest_claim(rule.claim, [value]),
                    describes=(
                        f"{rule.claim} = {value} → "
                        + (", ".join(granted) if granted else "no scopes")
                    ),
                )
            )
    personas.append(
        DevPersona(
            sub=UNMAPPED_PERSONA_SUB,
            name="Unmapped person",
            email="unmapped@localhost",
            claims={"groups": ["dev-no-such-group"]},
            describes="matches no mapping rule → signs in with zero scopes",
        )
    )
    return personas


def build_dev_idp_router(cfg: "AppConfig", provider_id: str, issuer: str) -> APIRouter:
    """Mount the development provider. Refuses to build outside a local deployment."""
    if not cfg.dev_idp_enabled:
        raise RuntimeError("The development identity provider is not enabled.")
    if not cfg.is_local:
        # Belt and braces with the config validator: this is the check that
        # holds even if someone constructs a router directly.
        raise RuntimeError(
            "The development identity provider may only run when ENVIRONMENT is local."
        )

    get_logger().warning(
        "identity.dev_idp.enabled",
        issuer=issuer,
        detail=(
            "QueryGate is serving its own development identity provider. It vouches "
            "for anyone who clicks a button. Never enable this outside local development."
        ),
    )

    router = APIRouter(prefix=MOUNT_PATH, tags=["development-identity-provider"])

    def _provider(request: Request) -> DevIdentityProvider:
        provider = getattr(request.app.state, "dev_idp", None)
        if provider is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The development identity provider is not initialised.",
            )
        return provider

    @router.get("/.well-known/openid-configuration")
    async def discovery(request: Request) -> JSONResponse:
        return JSONResponse(_provider(request).discovery_document())

    @router.get("/jwks.json")
    async def jwks(request: Request) -> JSONResponse:
        return JSONResponse(_provider(request).jwks())

    @router.get("/authorize", response_class=HTMLResponse)
    async def authorize(
        request: Request,
        client_id: str = Query(default=""),
        redirect_uri: str = Query(default="", max_length=2048),
        state: str = Query(default="", max_length=512),
        nonce: str = Query(default="", max_length=512),
        code_challenge: str = Query(default="", max_length=256),
        code_challenge_method: str = Query(default=""),
        persona: str = Query(default="", max_length=128),
    ):
        provider = _provider(request)
        if client_id != DEV_CLIENT_ID:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown client.")
        if code_challenge_method != "S256" or not code_challenge:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This provider requires PKCE with S256.",
            )
        expected_redirect = _expected_redirect_uri(request)
        if redirect_uri != expected_redirect:
            # The dev provider knows exactly one client and exactly one redirect
            # URI. Refusing anything else keeps it from being turned into an
            # open redirector by a page on some other local port.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="redirect_uri is not registered with the development provider.",
            )

        if persona:
            chosen = provider.persona(persona)
            if chosen is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown persona."
                )
            code = provider.issue_code(
                persona=chosen,
                code_challenge=code_challenge,
                nonce=nonce,
                redirect_uri=redirect_uri,
            )
            query = urlencode({"code": code, "state": state})
            return RedirectResponse(
                url=f"{redirect_uri}?{query}", status_code=status.HTTP_302_FOUND
            )

        return HTMLResponse(_picker_page(request, provider, provider_id))

    @router.post("/token")
    async def token(
        request: Request,
        grant_type: str = Form(default=""),
        code: str = Form(default=""),
        code_verifier: str = Form(default=""),
        redirect_uri: str = Form(default=""),
        client_id: str = Form(default=""),
    ) -> JSONResponse:
        provider = _provider(request)
        if grant_type != "authorization_code":
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
        if client_id != DEV_CLIENT_ID:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        try:
            id_token = provider.redeem_code(
                code=code, code_verifier=code_verifier, redirect_uri=redirect_uri
            )
        except DevIdpError as exc:
            return JSONResponse(
                {"error": exc.error, "error_description": exc.message}, status_code=400
            )
        return JSONResponse(
            {
                "token_type": "Bearer",  # nosec B105 - a token type, not a secret
                "expires_in": ID_TOKEN_TTL_SECONDS,
                "id_token": id_token,
                # No access token: QueryGate reads identity from the ID token
                # and never calls a userinfo endpoint, so minting one would be
                # a credential with no purpose.
            }
        )

    return router


def _expected_redirect_uri(request: Request) -> str:
    from querygate.identity.config_store import get_identity_store
    from querygate.identity.oidc import redirect_uri_for

    base = get_identity_store().settings.base_url or str(request.base_url).rstrip("/")
    return redirect_uri_for(base)


# The markup is a module constant rather than a literal at the interpolation
# site: the only values substituted into it are `html.escape`d at the call
# below, so caller text can reach a text node but never the structure. Hoisting
# it also keeps the template and its escaping visible together instead of buried
# inside a multi-line expression.
_PERSONA_ROW = (
    '<li><a class="persona" href="?{params}">'
    "<strong>{name}</strong><span>{describes}</span>"
    "<code>{sub}</code></a></li>"
)


def _picker_page(request: Request, provider: DevIdentityProvider, provider_id: str) -> str:
    """The persona chooser. Every interpolated value is escaped.

    Values here come from identity.yaml (an operator's own file) rather than
    from a caller, but the mapping-derived personas carry claim values verbatim
    and this page is rendered into a browser — so it escapes anyway, on the
    same principle `ui-a11y-reviewer` applies to operator-provided text.
    """
    query = dict(request.query_params)
    rows = []
    for persona in provider.personas:
        params = urlencode({**query, "persona": persona.sub})
        # `fmt: off` keeps the suppression comment on the line semgrep anchors
        # the match to — black would otherwise reflow this call and move it.
        # fmt: off
        rows.append(
            # Reviewed, not blanket-suppressed. `_PERSONA_ROW` is a module
            # constant, and every value substituted into it is html.escape()d
            # right here — so operator-provided text reaches a text node and can
            # never reach the markup structure. QueryGate carries no template
            # engine and will not add one for a single development-only page.
            # The suppression must sit on the line immediately above the call,
            # because that is the line semgrep anchors the multi-line match to.
            # nosemgrep: python.django.security.injection.raw-html-format.raw-html-format
            _PERSONA_ROW.format(
                params=html.escape(params, quote=True),
                name=html.escape(persona.name),
                describes=html.escape(persona.describes or "no mapping description"),
                sub=html.escape(persona.sub),
            )
        )
        # fmt: on
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QueryGate development sign-in</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin:0; padding:2.5rem 1.25rem; font:15px/1.55 ui-sans-serif,system-ui,sans-serif;
         background:#0f1115; color:#e7e9ee; }}
  main {{ max-width: 44rem; margin:0 auto; }}
  .warn {{ border:1px solid #b8860b; background:rgba(184,134,11,.14); border-radius:8px;
          padding:.9rem 1rem; margin-bottom:1.75rem; }}
  h1 {{ font-size:1.5rem; margin:.25rem 0 .5rem; }}
  p.sub {{ color:#a2a8b6; margin-top:0; }}
  ul {{ list-style:none; padding:0; margin:1.5rem 0 0; display:grid; gap:.6rem; }}
  a.persona {{ display:grid; gap:.2rem; padding:.9rem 1rem; border:1px solid #2a2f3a;
              border-radius:8px; text-decoration:none; color:inherit; background:#161a22; }}
  a.persona:hover, a.persona:focus-visible {{ border-color:#5b8def; outline:none; }}
  a.persona span {{ color:#a2a8b6; font-size:13px; }}
  a.persona code {{ color:#7d8494; font-size:12px; }}
</style></head>
<body><main>
  <div class="warn"><strong>Development identity provider.</strong> This is QueryGate
  signing its own tokens so the sign-in flow runs with no external IdP. It vouches for
  anyone who clicks. It refuses to start unless the environment is local, and
  <code>ENVIRONMENT=production</code> will not boot with it enabled.</div>
  <h1>Choose who to sign in as</h1>
  <p class="sub">Each persona carries the claims a real IdP would assert. What they can
  actually do is still decided by your <code>identity.yaml</code> mapping — signing in
  here grants nothing on its own.</p>
  <ul>{''.join(rows)}</ul>
  <p class="sub" style="margin-top:1.75rem">Provider id: <code>{html.escape(provider_id)}</code>
  &middot; issuer <code>{html.escape(provider.issuer)}</code></p>
</main></body></html>"""
