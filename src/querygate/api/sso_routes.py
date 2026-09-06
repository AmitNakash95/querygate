"""The human sign-in surface: OIDC login, local login, sessions, device grants.

Three groups of endpoints live here, and the boundary between them is the point
of the module:

* **Unauthenticated** — the provider list, the OIDC redirect and callback, the
  local password login, and the two RFC 8628 device-grant endpoints a CLI polls.
  These are reachable without a credential *because obtaining one is what they
  are for*; each is bounded, audited, and returns a deliberately uninformative
  failure.
* **Session-authenticated** — who am I, sign out, approve a device grant, list
  and revoke my own tokens. These require the session cookie **and** a matching
  CSRF token.
* **Scope-gated administration** (`api/admin_identity_routes.py`) — local
  accounts, provider inspection, mapping simulation, revoking another person's
  access. Those go through the normal `get_principal` dependency, so an API key,
  a JWT, a browser session, or a device token can all reach them if it carries
  the scope: identity administration is not a browser-only privilege.

Nothing here returns a client secret, a password verifier, a TOTP secret, an
authorization code, an ID token, or another person's claim set. The only place
a caller receives a credential is the response that mints one for them — the
session cookie, and the device grant's access token — each returned exactly
once and never readable back.
"""

from __future__ import annotations

import hmac
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

import pydantic as pyd
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from querygate.audit.logger import audit_authentication
from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.logging import get_logger
from querygate.identity import device as device_grants
from querygate.identity.claims import claim_scalar
from querygate.identity.config_store import IdentityConfigStore, get_identity_store
from querygate.identity.discovery import DiscoveryError, get_discovery_cache
from querygate.identity.local_auth import (
    LocalLoginError,
    LockedOutError,
    authenticate_local_user,
)
from querygate.identity.local_store import (
    LocalUserFileRepository,
    LocalUserUpdate,
    PublicLocalUser,
    get_local_user_store,
    utc_now,
)
from querygate.identity.models import PublicIdentityProvider, is_safe_relative_path
from querygate.identity.oidc import (
    SsoLoginError,
    build_authorization_request,
    complete_authorization,
    principal_subject,
)
from querygate.identity.passwords import PasswordError, check_password_policy, hash_password
from querygate.identity.sessions import (
    SESSION_TOKEN_PREFIX,
    SsoSession,
    get_login_flow_store,
    get_session_store,
    new_opaque_token,
    sanitize_claims,
    token_digest,
)

# Cookie holding the id of an in-flight login. Separate from the session cookie
# and deleted the moment the callback runs: it binds the IdP round trip to
# *this browser*, which the `state` parameter alone cannot do.
LOGIN_FLOW_COOKIE = "qg_login"


class ProvidersResponse(pyd.BaseModel):
    providers: List[PublicIdentityProvider] = pyd.Field(default_factory=list)
    local_provider_id: Optional[str] = None
    device_grant_enabled: bool = False

    model_config = pyd.ConfigDict(extra="forbid")


class SessionResponse(pyd.BaseModel):
    """Who the browser is signed in as. Carries the CSRF token, never the session token."""

    authenticated: bool
    subject: str = ""
    display_name: str = ""
    email: str = ""
    provider_id: str = ""
    auth_method: str = ""
    scopes: List[str] = pyd.Field(default_factory=list)
    csrf_token: str = ""
    expires_at: Optional[datetime] = None

    model_config = pyd.ConfigDict(extra="forbid")


class LocalLoginRequest(pyd.BaseModel):
    username: str = pyd.Field(min_length=1, max_length=128)
    password: str = pyd.Field(min_length=1, max_length=1024)
    totp_code: str = pyd.Field(default="", max_length=16)

    model_config = pyd.ConfigDict(extra="forbid")


class PasswordChangeRequest(pyd.BaseModel):
    current_password: str = pyd.Field(min_length=1, max_length=1024)
    new_password: str = pyd.Field(min_length=1, max_length=1024)

    model_config = pyd.ConfigDict(extra="forbid")


class DeviceCodeRequest(pyd.BaseModel):
    client_name: str = pyd.Field(default="unnamed client", max_length=64)
    scopes: List[str] = pyd.Field(default_factory=list, max_length=64)

    model_config = pyd.ConfigDict(extra="forbid")


class DeviceCodeResponse(pyd.BaseModel):
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int

    model_config = pyd.ConfigDict(extra="forbid")


class DeviceTokenRequest(pyd.BaseModel):
    device_code: str = pyd.Field(min_length=1, max_length=256)

    model_config = pyd.ConfigDict(extra="forbid")


class DeviceTokenResponse(pyd.BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    scope: str = ""

    model_config = pyd.ConfigDict(extra="forbid")


class UserCodeRequest(pyd.BaseModel):
    user_code: str = pyd.Field(min_length=1, max_length=32)

    model_config = pyd.ConfigDict(extra="forbid")


def require_sso(cfg: AppConfig) -> IdentityConfigStore:
    if not cfg.sso_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="SSO is not enabled on this deployment."
        )
    return get_identity_store()


def base_url_for(store: IdentityConfigStore, request: Request) -> str:
    """The public origin used to build the redirect URI.

    Prefers the configured `sso.base_url`. Falling back to the request's own
    base URL happens **only** when nothing is configured, because that value
    derives from the Host header — fine for a local developer, not something to
    depend on in production.
    """
    return store.settings.base_url or str(request.base_url).rstrip("/")


def _safe_return_to(candidate: str, store: IdentityConfigStore) -> str:
    if candidate and is_safe_relative_path(candidate):
        return candidate
    return store.settings.default_landing_path


def _set_session_cookie(response: Response, cfg: AppConfig, token: str, max_age: int) -> None:
    response.set_cookie(
        key=cfg.sso_session_cookie_name,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=cfg.sso_session_cookie_secure,
        samesite=cfg.sso_session_cookie_samesite.lower(),
        path="/",
    )


def _clear_session_cookie(response: Response, cfg: AppConfig) -> None:
    response.delete_cookie(
        key=cfg.sso_session_cookie_name,
        path="/",
        httponly=True,
        secure=cfg.sso_session_cookie_secure,
        samesite=cfg.sso_session_cookie_samesite.lower(),
    )


async def establish_session(
    *,
    store: IdentityConfigStore,
    provider_id: str,
    subject: str,
    claims: Dict[str, Any],
    auth_method: str,
    display_name: str,
    email: str,
) -> tuple[str, SsoSession]:
    now = datetime.now(timezone.utc)
    token = new_opaque_token(SESSION_TOKEN_PREFIX)
    session = SsoSession(
        session_digest=token_digest(token),
        subject=subject,
        provider_id=provider_id,
        auth_method=auth_method,
        csrf_token=new_opaque_token("qgc_"),
        display_name=display_name,
        email=email,
        claims=sanitize_claims(claims),
        created_at=now,
        expires_at=now + timedelta(seconds=store.settings.session_ttl_seconds),
        last_seen_at=now,
    )
    await get_session_store().create(session)
    return token, session


async def current_session_of(request: Request, cfg: AppConfig) -> Optional[SsoSession]:
    token = request.cookies.get(cfg.sso_session_cookie_name)
    if not token:
        return None
    return await get_session_store().get(token_digest(token))


def _require_session(session: Optional[SsoSession]) -> SsoSession:
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed in.")
    return session


def _require_csrf(request: Request, cfg: AppConfig, session: SsoSession) -> None:
    presented = request.headers.get(cfg.sso_csrf_header) or ""
    if not presented or not hmac.compare_digest(presented, session.csrf_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing or invalid CSRF token. Reload the page and try again.",
        )


async def _authenticated_session(request: Request, cfg: AppConfig) -> SsoSession:
    session = _require_session(await current_session_of(request, cfg))
    _require_csrf(request, cfg, session)
    return session


def session_response(session: SsoSession, scopes: frozenset[str]) -> SessionResponse:
    return SessionResponse(
        authenticated=True,
        subject=session.subject,
        display_name=session.display_name,
        email=session.email,
        provider_id=session.provider_id,
        auth_method=session.auth_method,
        scopes=sorted(scopes),
        csrf_token=session.csrf_token,
        expires_at=session.expires_at,
    )


def update_local_user(cfg: AppConfig, username: str, mutate) -> PublicLocalUser:
    """Apply `mutate` to one account through the single locked writer."""

    def _updater(store):
        user = store.get(username)
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown account.")
        updated = mutate(user)
        return LocalUserUpdate(store.with_user(updated), updated.to_public())

    return LocalUserFileRepository(cfg.local_users_file).update(_updater)


def _public_grant(grant: device_grants.DeviceGrant) -> device_grants.PublicDeviceGrant:
    return device_grants.PublicDeviceGrant(
        user_code=grant.user_code,
        client_name=grant.client_name,
        requested_scopes=list(grant.requested_scopes),
        status=grant.status,
        expires_at=grant.expires_at,
    )


def build_sso_router(
    get_principal: Callable[..., Principal], cfg: AppConfig, prefix: str = "/api/v1"
) -> APIRouter:
    router = APIRouter(prefix=f"{prefix}/auth", tags=["authentication"])

    # ---------------------------------------------------------------- public

    @router.get("/providers", response_model=ProvidersResponse)
    async def list_providers() -> ProvidersResponse:
        """The sign-in options a browser should offer.

        Unauthenticated by necessity — a sign-in page has to render before
        anyone holds a credential — and safe to be: `PublicIdentityProvider`
        exposes only an id, a label, and a kind.
        """
        store = require_sso(cfg)
        local = store.local_provider()
        return ProvidersResponse(
            providers=store.list_public(),
            local_provider_id=local.id if local else None,
            device_grant_enabled=cfg.sso_device_grant_enabled,
        )

    @router.get("/sso/login")
    async def sso_login(
        request: Request,
        provider: str = Query(min_length=1, max_length=64),
        return_to: str = Query(default="", max_length=512),
    ) -> RedirectResponse:
        """Start the authorization-code flow: 302 to the identity provider."""
        store = require_sso(cfg)
        try:
            profile = store.get_enabled(provider)
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Unknown identity provider."
            )
        if profile.kind not in ("oidc", "dev"):
            # `dev` is deliberately included: the development provider exists to
            # exercise the *real* redirect flow, so it must take the same path
            # rather than a shortcut that would prove nothing.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="That provider does not use a redirect-based sign-in.",
            )
        try:
            metadata = await get_discovery_cache().get(profile.issuer)
        except DiscoveryError as exc:
            get_logger().warning(
                "identity.sso.discovery_failed", provider=profile.id, error=str(exc)
            )
            audit_authentication(
                action="sso.login",
                outcome="rejected",
                provider_id=profile.id,
                auth_method="sso_session",
                error_category="discovery_failed",
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The identity provider is unreachable. Try again shortly.",
            )
        try:
            authorize_url, flow = build_authorization_request(
                provider=profile,
                metadata=metadata,
                base_url=base_url_for(store, request),
                return_to=_safe_return_to(return_to, store),
                ttl_seconds=store.settings.login_flow_ttl_seconds,
            )
        except SsoLoginError as exc:
            audit_authentication(
                action="sso.login",
                outcome="rejected",
                provider_id=profile.id,
                auth_method="sso_session",
                error_category=exc.category,
            )
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.message)

        await get_login_flow_store().put(flow)
        response = RedirectResponse(url=authorize_url, status_code=status.HTTP_302_FOUND)
        response.set_cookie(
            key=LOGIN_FLOW_COOKIE,
            value=flow.flow_id,
            max_age=int(store.settings.login_flow_ttl_seconds),
            httponly=True,
            secure=cfg.sso_session_cookie_secure,
            # Must be `lax`, not `strict`: the browser arrives back at the
            # callback from the IdP's origin, and a strict cookie would not be
            # sent on that cross-site navigation — the flow could never complete.
            samesite="lax",
            path="/",
        )
        return response

    @router.get("/sso/callback")
    async def sso_callback(
        request: Request,
        code: str = Query(default="", max_length=4096),
        state: str = Query(default="", max_length=512),
        error: str = Query(default="", max_length=256),
    ) -> RedirectResponse:
        """Complete the flow: verify, establish a session, return to the app."""
        started = time.perf_counter()
        store = require_sso(cfg)
        flow_id = request.cookies.get(LOGIN_FLOW_COOKIE) or ""
        flow = await get_login_flow_store().consume(flow_id) if flow_id else None

        def _fail(category: str, provider_id: str = "") -> RedirectResponse:
            audit_authentication(
                action="sso.login",
                outcome="rejected",
                provider_id=provider_id or None,
                auth_method="sso_session",
                error_category=category,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            failure = RedirectResponse(
                url=f"{store.settings.default_landing_path}?sso_error={category}",
                status_code=status.HTTP_302_FOUND,
            )
            failure.delete_cookie(LOGIN_FLOW_COOKIE, path="/")
            return failure

        if error:
            # The IdP declined. Its own message can echo request content, so
            # only the fact of the denial is recorded.
            return _fail("provider_denied")
        if flow is None:
            return _fail("no_login_flow")
        if not code:
            return _fail("missing_code", flow.provider_id)
        try:
            profile = store.get_enabled(flow.provider_id)
            metadata = await get_discovery_cache().get(profile.issuer)
            claims = await complete_authorization(
                provider=profile, metadata=metadata, flow=flow, code=code, state=state
            )
        except KeyError:
            return _fail("provider_removed", flow.provider_id)
        except DiscoveryError:
            return _fail("discovery_failed", flow.provider_id)
        except SsoLoginError as exc:
            return _fail(exc.category, flow.provider_id)

        subject = principal_subject(profile, claims)
        try:
            token, session = await establish_session(
                store=store,
                provider_id=profile.id,
                subject=subject,
                claims=claims,
                auth_method="oidc",
                display_name=claim_scalar(claims, profile.name_claim) or "",
                email=claim_scalar(claims, profile.email_claim) or "",
            )
        except ValueError:
            return _fail("claims_too_large", flow.provider_id)

        granted = store.mapping.scopes_for(profile.id, session.claims)
        audit_authentication(
            action="sso.login",
            outcome="success",
            principal=subject,
            principal_scopes=sorted(granted),
            provider_id=profile.id,
            auth_method="sso_session",
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        response = RedirectResponse(url=flow.return_to, status_code=status.HTTP_302_FOUND)
        response.delete_cookie(LOGIN_FLOW_COOKIE, path="/")
        _set_session_cookie(response, cfg, token, int(store.settings.session_ttl_seconds))
        return response

    @router.post("/local/login", response_model=SessionResponse)
    async def local_login(payload: LocalLoginRequest, response: Response) -> SessionResponse:
        """Sign in with a QueryGate local account."""
        started = time.perf_counter()
        store = require_sso(cfg)
        provider = store.local_provider()
        if provider is None or not cfg.local_idp_enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Local sign-in is not enabled on this deployment.",
            )
        try:
            result = await authenticate_local_user(
                store=get_local_user_store(),
                username=payload.username,
                password=payload.password,
                totp_code=payload.totp_code or None,
                lockout_threshold=cfg.local_login_lockout_threshold,
                lockout_seconds=cfg.local_login_lockout_seconds,
                require_mfa=cfg.local_require_mfa,
            )
        except LocalLoginError as exc:
            audit_authentication(
                action="local.login",
                outcome="rejected",
                principal=payload.username,
                provider_id=provider.id,
                auth_method="local_password",
                error_category=exc.category,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            locked = isinstance(exc, LockedOutError)
            raise HTTPException(
                status_code=(
                    status.HTTP_429_TOO_MANY_REQUESTS if locked else status.HTTP_401_UNAUTHORIZED
                ),
                detail=exc.message,
                headers=(
                    {"Retry-After": str(int(exc.retry_after_seconds) + 1)} if locked else None
                ),
            )

        user = result.user
        subject = f"{provider.subject_prefix}{user.username}"
        token, session = await establish_session(
            store=store,
            provider_id=provider.id,
            subject=subject,
            claims=user.claims(),
            auth_method="local_password",
            display_name=user.display_name or user.username,
            email=user.email,
        )
        granted = store.mapping.scopes_for(provider.id, session.claims)
        audit_authentication(
            action="local.login",
            outcome="success",
            principal=subject,
            principal_scopes=sorted(granted),
            provider_id=provider.id,
            auth_method="local_password",
            mfa_used=result.used_mfa,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        _set_session_cookie(response, cfg, token, int(store.settings.session_ttl_seconds))
        return session_response(session, granted)

    # ------------------------------------------------------- session-scoped

    @router.get("/session", response_model=SessionResponse)
    async def read_session(request: Request) -> SessionResponse:
        """Who this browser is signed in as, and the CSRF token to echo back.

        Deliberately exempt from the CSRF check — it is the endpoint that
        *issues* that token. Safe because a cross-origin page cannot read the
        response: QueryGate installs no CORS middleware, so a credentialed
        cross-site fetch is blocked from seeing the body.
        """
        store = require_sso(cfg)
        session = await current_session_of(request, cfg)
        if session is None:
            return SessionResponse(authenticated=False)
        return session_response(
            session, store.mapping.scopes_for(session.provider_id, session.claims)
        )

    @router.post("/logout", response_model=SessionResponse)
    async def logout(request: Request, response: Response) -> SessionResponse:
        require_sso(cfg)
        session = await current_session_of(request, cfg)
        if session is not None:
            _require_csrf(request, cfg, session)
            await get_session_store().delete(session.session_digest)
            audit_authentication(
                action="sso.logout",
                outcome="success",
                principal=session.subject,
                provider_id=session.provider_id,
                auth_method=session.auth_method,
            )
        _clear_session_cookie(response, cfg)
        return SessionResponse(authenticated=False)

    @router.post("/local/password", response_model=SessionResponse)
    async def change_own_password(
        request: Request, payload: PasswordChangeRequest
    ) -> SessionResponse:
        """Change your own local password. Requires the current one."""
        store = require_sso(cfg)
        session = await _authenticated_session(request, cfg)
        provider = store.local_provider()
        if provider is None or session.provider_id != provider.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This account is managed by your identity provider.",
            )
        username = str(session.claims.get("preferred_username") or session.subject)
        try:
            # Re-check the password only. The person is already holding a
            # session that the second factor (if enrolled) gated at sign-in;
            # demanding a fresh TOTP code here would block a rotation whenever
            # the previous code has not yet rolled over.
            await authenticate_local_user(
                store=get_local_user_store(),
                username=username,
                password=payload.current_password,
                lockout_threshold=cfg.local_login_lockout_threshold,
                lockout_seconds=cfg.local_login_lockout_seconds,
            )
        except LocalLoginError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=exc.message)
        try:
            check_password_policy(payload.new_password, username=username)
        except PasswordError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

        verifier = hash_password(payload.new_password)
        update_local_user(
            cfg,
            username,
            lambda user: user.model_copy(
                update={
                    "password_verifier": verifier,
                    "must_change_password": False,  # nosec B105 - a flag, not a password
                    "password_updated_at": utc_now(),
                }
            ),
        )
        audit_authentication(
            action="local_user.update",
            outcome="success",
            principal=session.subject,
            target_principal=username,
            provider_id=provider.id,
            auth_method=session.auth_method,
        )
        return session_response(
            session, store.mapping.scopes_for(session.provider_id, session.claims)
        )

    # -------------------------------------------------------- device grants

    def _require_device_grant() -> None:
        if not cfg.sso_device_grant_enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="The device authorization grant is not enabled on this deployment.",
            )

    @router.post("/device/code", response_model=DeviceCodeResponse)
    async def device_code(request: Request, payload: DeviceCodeRequest) -> DeviceCodeResponse:
        """RFC 8628 §3.1 — a browserless tool asks for a code to be approved."""
        store = require_sso(cfg)
        _require_device_grant()
        code, grant = await device_grants.start_device_authorization(
            client_name=payload.client_name,
            requested_scopes=payload.scopes,
            ttl_seconds=cfg.sso_device_code_ttl_seconds,
        )
        audit_authentication(
            action="device.authorize",
            outcome="success",
            client_name=grant.client_name,
            auth_method="sso_device_token",
        )
        verification_uri = f"{base_url_for(store, request)}{store.settings.default_landing_path}"
        return DeviceCodeResponse(
            device_code=code,
            user_code=grant.user_code,
            verification_uri=verification_uri,
            verification_uri_complete=f"{verification_uri}?user_code={grant.user_code}",
            expires_in=int(cfg.sso_device_code_ttl_seconds),
            interval=int(grant.interval_seconds),
        )

    @router.post("/device/token", response_model=DeviceTokenResponse)
    async def device_token(payload: DeviceTokenRequest) -> DeviceTokenResponse:
        """RFC 8628 §3.4 — the tool polls until a human approves or time runs out."""
        require_sso(cfg)
        _require_device_grant()
        try:
            access_token, issued = await device_grants.redeem_device_code(
                device_code=payload.device_code,
                token_ttl_seconds=cfg.sso_device_token_ttl_seconds,
            )
        except device_grants.DeviceGrantError as exc:
            # RFC 8628 uses 400 with an `error` code for every non-success,
            # including the entirely normal "still waiting" case.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": exc.error, "error_description": exc.message},
            )
        audit_authentication(
            action="device.token_issued",
            outcome="success",
            principal=issued.subject,
            principal_scopes=list(issued.scopes),
            provider_id=issued.provider_id,
            client_name=issued.client_name,
            auth_method="sso_device_token",
        )
        return DeviceTokenResponse(
            access_token=access_token,
            expires_in=int(cfg.sso_device_token_ttl_seconds),
            scope=" ".join(issued.scopes),
        )

    @router.get("/device/pending", response_model=device_grants.PublicDeviceGrant)
    async def pending_device_grant(
        request: Request, user_code: str = Query(min_length=1, max_length=32)
    ) -> device_grants.PublicDeviceGrant:
        """What a tool is asking for, shown to the human about to approve it."""
        require_sso(cfg)
        _require_device_grant()
        await _authenticated_session(request, cfg)
        grant = await device_grants.get_device_grant_store().by_user_code(
            device_grants.normalize_user_code(user_code)
        )
        if grant is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="That code is not valid or has expired.",
            )
        return _public_grant(grant)

    @router.post("/device/approve", response_model=device_grants.PublicDeviceGrant)
    async def approve_device(
        request: Request, payload: UserCodeRequest
    ) -> device_grants.PublicDeviceGrant:
        """Approve a pending device grant as yourself, narrowed to your scopes."""
        store = require_sso(cfg)
        _require_device_grant()
        session = await _authenticated_session(request, cfg)
        approver_scopes = store.mapping.scopes_for(session.provider_id, session.claims)
        try:
            grant = await device_grants.approve_device_grant(
                user_code=payload.user_code,
                approver_subject=session.subject,
                approver_scopes=approver_scopes,
                provider_id=session.provider_id,
                approver_claims=dict(session.claims),
            )
        except device_grants.DeviceGrantError as exc:
            audit_authentication(
                action="device.approve",
                outcome="rejected",
                principal=session.subject,
                provider_id=session.provider_id,
                auth_method=session.auth_method,
                error_category=exc.error,
            )
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message)
        audit_authentication(
            action="device.approve",
            outcome="success",
            principal=session.subject,
            principal_scopes=list(grant.granted_scopes),
            provider_id=session.provider_id,
            auth_method=session.auth_method,
            client_name=grant.client_name,
        )
        return _public_grant(grant)

    @router.post("/device/deny", response_model=device_grants.PublicDeviceGrant)
    async def deny_device(
        request: Request, payload: UserCodeRequest
    ) -> device_grants.PublicDeviceGrant:
        require_sso(cfg)
        _require_device_grant()
        session = await _authenticated_session(request, cfg)
        try:
            grant = await device_grants.deny_device_grant(user_code=payload.user_code)
        except device_grants.DeviceGrantError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message)
        audit_authentication(
            action="device.deny",
            outcome="success",
            principal=session.subject,
            provider_id=session.provider_id,
            auth_method=session.auth_method,
            client_name=grant.client_name,
        )
        return _public_grant(grant)

    @router.get("/tokens", response_model=List[device_grants.PublicIssuedToken])
    async def my_tokens(request: Request) -> List[device_grants.PublicIssuedToken]:
        """Tokens issued in your name, so you can see and revoke them."""
        require_sso(cfg)
        session = await _authenticated_session(request, cfg)
        tokens = await device_grants.get_issued_token_store().list_for_subject(session.subject)
        return [
            device_grants.PublicIssuedToken(
                token_digest=token.token_digest,
                subject=token.subject,
                client_name=token.client_name,
                scopes=list(token.scopes),
                created_at=token.created_at,
                expires_at=token.expires_at,
            )
            for token in tokens
        ]

    @router.delete("/tokens/{token_digest}", status_code=status.HTTP_204_NO_CONTENT)
    async def revoke_my_token(request: Request, token_digest: str) -> Response:
        require_sso(cfg)
        session = await _authenticated_session(request, cfg)
        store = device_grants.get_issued_token_store()
        existing = await store.get(token_digest)
        # Revocation is scoped to your own tokens: a digest you do not own gets
        # the same 404 as one that does not exist, so this is not an oracle for
        # whether some other person holds a token.
        if existing is None or existing.subject != session.subject:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown token.")
        await store.delete(token_digest)
        audit_authentication(
            action="session.revoke",
            outcome="success",
            principal=session.subject,
            provider_id=session.provider_id,
            auth_method=session.auth_method,
            client_name=existing.client_name,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
