"""Administering who can sign in: local accounts, providers, and revocation.

Scope-gated rather than session-gated, deliberately. `admin:identity:read` and
`admin:identity:write` are ordinary QueryGate scopes, so these endpoints are
reachable by whatever credential carries them — a static API key in a
provisioning script, an IdP-issued JWT, a browser session, or a device token.
Identity administration is not a browser-only privilege, and making it one
would have meant an operator could not automate account provisioning.

Three properties this surface holds to:

* **Never read a secret back.** A password verifier and a TOTP secret can be
  *set* here and never retrieved; `PublicLocalUser` has no field for either.
  A TOTP secret is shown exactly once, in the response that creates it.
* **Every change is audited** through `audit_authentication`, naming the
  administrator and the affected account — never the new password.
* **No self-lockout by accident.** An administrator cannot delete or disable
  their own account through this surface; that has to be a deliberate act by
  someone else, or a config-file edit.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

import pydantic as pyd
from fastapi import APIRouter, Depends, HTTPException, status

from querygate.api._errors import require_scope
from querygate.audit.logger import audit_authentication
from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.scopes import ADMIN_IDENTITY_READ_SCOPE, ADMIN_IDENTITY_WRITE_SCOPE
from querygate.identity.config_store import get_identity_store
from querygate.identity.device import get_issued_token_store
from querygate.identity.local_store import (
    LocalUser,
    LocalUserFileRepository,
    LocalUserUpdate,
    PublicLocalUser,
    get_local_user_store,
    utc_now,
)
from querygate.identity.passwords import PasswordError, check_password_policy, hash_password
from querygate.identity.sessions import get_session_store
from querygate.identity.totp import new_totp_secret, provisioning_uri


class AdminProviderInfo(pyd.BaseModel):
    """An operator's view of a configured provider.

    Carries the issuer, the client id, and the claim names — everything needed
    to answer "why did nobody get any scopes" — and, structurally, no
    `client_secret` field. The client id is public by construction: it travels
    in the authorization URL through the user's own browser. Whether a secret
    is configured at all is reported as the boolean `confidential_client`.
    """

    id: str
    display_name: str
    kind: str
    enabled: bool
    preset: str = ""
    issuer: str = ""
    client_id: str = ""
    groups_claim: str = ""
    roles_claim: str = ""
    subject_prefix: str = ""
    confidential_client: bool = False

    model_config = pyd.ConfigDict(extra="forbid")


class CreateLocalUserRequest(pyd.BaseModel):
    username: str = pyd.Field(min_length=1, max_length=128)
    password: str = pyd.Field(min_length=1, max_length=1024)
    display_name: str = pyd.Field(default="", max_length=256)
    email: str = pyd.Field(default="", max_length=320)
    groups: List[str] = pyd.Field(default_factory=list, max_length=256)
    must_change_password: bool = True

    model_config = pyd.ConfigDict(extra="forbid")


class UpdateLocalUserRequest(pyd.BaseModel):
    display_name: Optional[str] = pyd.Field(default=None, max_length=256)
    email: Optional[str] = pyd.Field(default=None, max_length=320)
    groups: Optional[List[str]] = pyd.Field(default=None, max_length=256)
    disabled: Optional[bool] = None
    password: Optional[str] = pyd.Field(default=None, max_length=1024)
    must_change_password: Optional[bool] = None
    # Only `false` is meaningful: it removes an enrolled factor. Enrolling one
    # goes through POST /{username}/mfa, which is the sole path that can produce
    # a secret — and it returns it exactly once.
    mfa_enabled: Optional[bool] = None

    model_config = pyd.ConfigDict(extra="forbid")


class MfaEnrolmentResponse(pyd.BaseModel):
    """Returned once, at enrolment. The secret is never readable again."""

    username: str
    provisioning_uri: str
    secret: str

    model_config = pyd.ConfigDict(extra="forbid")


class ExplainMappingRequest(pyd.BaseModel):
    provider_id: str = pyd.Field(min_length=1, max_length=64)
    claims: Dict[str, Any] = pyd.Field(default_factory=dict)

    model_config = pyd.ConfigDict(extra="forbid")


class ExplainMappingResponse(pyd.BaseModel):
    provider_id: str
    granted_scopes: List[str] = pyd.Field(default_factory=list)
    matched_rules: List[Dict[str, Any]] = pyd.Field(default_factory=list)

    model_config = pyd.ConfigDict(extra="forbid")


class RevokeRequest(pyd.BaseModel):
    subject: str = pyd.Field(min_length=1, max_length=256)

    model_config = pyd.ConfigDict(extra="forbid")


class RevokeResponse(pyd.BaseModel):
    subject: str
    sessions_revoked: int
    tokens_revoked: int

    model_config = pyd.ConfigDict(extra="forbid")


def _require_sso(cfg: AppConfig) -> None:
    if not cfg.sso_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="SSO is not enabled on this deployment."
        )


def _require_local_idp(cfg: AppConfig) -> None:
    _require_sso(cfg)
    if not cfg.local_idp_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The local identity provider is not enabled on this deployment.",
        )


def _local_subject_of(principal: Principal) -> str:
    """The local username this principal corresponds to, if any."""
    return str(principal.claims.get("preferred_username") or "")


def build_admin_identity_router(
    get_principal: Callable[..., Principal], cfg: AppConfig, prefix: str = "/api/v1"
) -> APIRouter:
    router = APIRouter(prefix=f"{prefix}/admin/identity", tags=["admin-identity"])

    @router.get("/providers", response_model=List[AdminProviderInfo])
    async def list_providers(
        principal: Principal = Depends(get_principal),
    ) -> List[AdminProviderInfo]:
        _require_sso(cfg)
        require_scope(principal, ADMIN_IDENTITY_READ_SCOPE)
        store = get_identity_store()
        return [
            AdminProviderInfo(
                id=profile.id,
                display_name=profile.display_name,
                kind=profile.kind,
                enabled=profile.enabled,
                preset=profile.preset if profile.kind == "oidc" else "",
                issuer=profile.issuer,
                client_id=profile.client_id,
                groups_claim=profile.groups_claim,
                roles_claim=profile.roles_claim,
                subject_prefix=profile.subject_prefix,
                confidential_client=bool(profile.client_secret),
            )
            for profile in (store.get(pid) for pid in store.all_ids())
        ]

    @router.post("/simulate", response_model=ExplainMappingResponse)
    async def simulate_mapping(
        payload: ExplainMappingRequest, principal: Principal = Depends(get_principal)
    ) -> ExplainMappingResponse:
        """Answer "what would these claims earn?" without anyone signing in.

        The counterpart to the policy simulator the admin UI already has: an
        operator pastes the claim set their IdP emits and sees exactly which
        rules fire and which scopes result, instead of discovering after
        rollout that everyone signs in with no authority.
        """
        _require_sso(cfg)
        require_scope(principal, ADMIN_IDENTITY_READ_SCOPE)
        mapping = get_identity_store().mapping
        return ExplainMappingResponse(
            provider_id=payload.provider_id,
            granted_scopes=sorted(mapping.scopes_for(payload.provider_id, payload.claims)),
            matched_rules=mapping.explain(payload.provider_id, payload.claims),
        )

    # ------------------------------------------------------- local accounts

    @router.get("/users", response_model=List[PublicLocalUser])
    async def list_users(principal: Principal = Depends(get_principal)) -> List[PublicLocalUser]:
        _require_local_idp(cfg)
        require_scope(principal, ADMIN_IDENTITY_READ_SCOPE)
        return get_local_user_store().list_public()

    @router.post("/users", response_model=PublicLocalUser, status_code=status.HTTP_201_CREATED)
    async def create_user(
        payload: CreateLocalUserRequest, principal: Principal = Depends(get_principal)
    ) -> PublicLocalUser:
        _require_local_idp(cfg)
        require_scope(principal, ADMIN_IDENTITY_WRITE_SCOPE)
        try:
            check_password_policy(payload.password, username=payload.username)
        except PasswordError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        try:
            new_user = LocalUser(
                username=payload.username,
                display_name=payload.display_name,
                email=payload.email,
                groups=payload.groups,
                password_verifier=hash_password(payload.password),
                must_change_password=payload.must_change_password,
                created_at=utc_now(),
                password_updated_at=utc_now(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

        def _updater(store):
            if store.get(new_user.username) is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="That account already exists."
                )
            return LocalUserUpdate(store.with_user(new_user), new_user.to_public())

        created = LocalUserFileRepository(cfg.local_users_file).update(_updater)
        audit_authentication(
            action="local_user.create",
            outcome="success",
            principal=principal.subject,
            principal_scopes=sorted(principal.scopes),
            auth_method=principal.auth_method,
            target_principal=created.username,
        )
        return created

    @router.patch("/users/{username}", response_model=PublicLocalUser)
    async def update_user(
        username: str,
        payload: UpdateLocalUserRequest,
        principal: Principal = Depends(get_principal),
    ) -> PublicLocalUser:
        _require_local_idp(cfg)
        require_scope(principal, ADMIN_IDENTITY_WRITE_SCOPE)
        if payload.disabled and _local_subject_of(principal).lower() == username.lower():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot disable your own account.",
            )
        if payload.password is not None:
            try:
                check_password_policy(payload.password, username=username)
            except PasswordError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        if payload.mfa_enabled:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Enrol a second factor with POST /users/{username}/mfa.",
            )

        def _updater(store):
            user = store.get(username)
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Unknown account."
                )
            changes: Dict[str, Any] = {}
            if payload.display_name is not None:
                changes["display_name"] = payload.display_name
            if payload.email is not None:
                changes["email"] = payload.email
            if payload.groups is not None:
                changes["groups"] = payload.groups
            if payload.disabled is not None:
                changes["disabled"] = payload.disabled
            if payload.must_change_password is not None:
                changes["must_change_password"] = payload.must_change_password
            if payload.password is not None:
                changes["password_verifier"] = hash_password(payload.password)
                changes["password_updated_at"] = utc_now()
                changes["must_change_password"] = (
                    payload.must_change_password
                    if payload.must_change_password is not None
                    else True
                )
            if payload.mfa_enabled is False:
                # nosec B105: clearing the enrolled factor, not setting a secret.
                changes["totp_secret"] = ""  # nosec B105
            try:
                updated = user.model_copy(update=changes)
                LocalUser.model_validate(updated.model_dump())
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
            return LocalUserUpdate(store.with_user(updated), updated.to_public())

        result = LocalUserFileRepository(cfg.local_users_file).update(_updater)
        # A group change, a disable, or a password rotation must not leave a
        # live session running with the old authority — the session's claims
        # were captured at sign-in, so they are re-issued only on next login.
        revoked_sessions = await get_session_store().delete_for_subject(result.username)
        audit_authentication(
            action="local_user.update",
            outcome="success",
            principal=principal.subject,
            principal_scopes=sorted(principal.scopes),
            auth_method=principal.auth_method,
            target_principal=result.username,
        )
        if revoked_sessions:
            audit_authentication(
                action="session.revoke",
                outcome="success",
                principal=principal.subject,
                auth_method=principal.auth_method,
                target_principal=result.username,
            )
        return result

    @router.delete("/users/{username}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_user(username: str, principal: Principal = Depends(get_principal)):
        _require_local_idp(cfg)
        require_scope(principal, ADMIN_IDENTITY_WRITE_SCOPE)
        if _local_subject_of(principal).lower() == username.lower():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot delete your own account.",
            )

        def _updater(store):
            user = store.get(username)
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Unknown account."
                )
            return LocalUserUpdate(store.without_user(username), user.username)

        removed = LocalUserFileRepository(cfg.local_users_file).update(_updater)
        await get_session_store().delete_for_subject(removed)
        await get_issued_token_store().delete_for_subject(removed)
        audit_authentication(
            action="local_user.delete",
            outcome="success",
            principal=principal.subject,
            principal_scopes=sorted(principal.scopes),
            auth_method=principal.auth_method,
            target_principal=removed,
        )
        return None

    @router.post("/users/{username}/mfa", response_model=MfaEnrolmentResponse)
    async def enrol_mfa(
        username: str, principal: Principal = Depends(get_principal)
    ) -> MfaEnrolmentResponse:
        """Generate a TOTP secret for an account and return it exactly once."""
        _require_local_idp(cfg)
        require_scope(principal, ADMIN_IDENTITY_WRITE_SCOPE)
        secret = new_totp_secret()

        def _updater(store):
            user = store.get(username)
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Unknown account."
                )
            updated = user.model_copy(update={"totp_secret": secret})
            return LocalUserUpdate(store.with_user(updated), updated.username)

        resolved = LocalUserFileRepository(cfg.local_users_file).update(_updater)
        audit_authentication(
            action="local_user.update",
            outcome="success",
            principal=principal.subject,
            principal_scopes=sorted(principal.scopes),
            auth_method=principal.auth_method,
            target_principal=resolved,
            mfa_used=None,
        )
        return MfaEnrolmentResponse(
            username=resolved,
            provisioning_uri=provisioning_uri(secret, account=resolved),
            secret=secret,
        )

    # ------------------------------------------------------------ revocation

    @router.post("/revoke", response_model=RevokeResponse)
    async def revoke_access(
        payload: RevokeRequest, principal: Principal = Depends(get_principal)
    ) -> RevokeResponse:
        """Immediately end one person's sessions and issued device tokens.

        The break-glass control: it works regardless of which provider the
        person signed in through, and it does not wait for a token to expire.
        """
        _require_sso(cfg)
        require_scope(principal, ADMIN_IDENTITY_WRITE_SCOPE)
        sessions = await get_session_store().delete_for_subject(payload.subject)
        tokens = await get_issued_token_store().delete_for_subject(payload.subject)
        audit_authentication(
            action="session.revoke",
            outcome="success",
            principal=principal.subject,
            principal_scopes=sorted(principal.scopes),
            auth_method=principal.auth_method,
            target_principal=payload.subject,
        )
        return RevokeResponse(
            subject=payload.subject, sessions_revoked=sessions, tokens_revoked=tokens
        )

    return router
