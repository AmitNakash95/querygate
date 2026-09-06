"""Configuration models for identity providers and claim→scope mapping.

Mirrors the security split `connections/models.py` established and
`tests/unit/test_credential_redaction.py` enforces: `IdentityProviderProfile`
holds the OIDC client secret and never leaves this process, while
`PublicIdentityProvider` — the only shape any REST response or UI ever sees —
has **no field capable of carrying a credential at all**. The separation is
structural, not a redaction step that could be forgotten.

Every value that is ever interpolated into a URL (`tenant`, `domain`) or
compared against a claim is validated here, at load, so a malformed
identity.yaml fails loudly at startup/hot-reload rather than at a user's first
login attempt.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Literal, Optional, Tuple
from urllib.parse import urlsplit

import pydantic as pyd

from querygate.core.scopes import ALL_SCOPES, ROLE_BUNDLES
from querygate.identity.presets import get_preset

ProviderKind = Literal["oidc", "local", "dev"]

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# `tenant` and `domain` are substituted into an issuer URL template, so they are
# restricted to characters that cannot introduce a new path segment, authority,
# scheme, query, or fragment. This is what stops a preset template from being
# turned into a redirect to an attacker-chosen issuer.
_TENANT_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_DOMAIN_PATTERN = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
# AWS region slugs and the like: letters, digits, hyphens only.
_REGION_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_SCOPE_TOKEN_PATTERN = re.compile(r"^[\x21\x23-\x5B\x5D-\x7E]+$")  # RFC 6749 scope-token
_SUBJECT_PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,32}$")

_ROLE_BUNDLE_NAMES: Tuple[str, ...] = tuple(bundle.name for bundle in ROLE_BUNDLES)
_ROLE_BUNDLE_SCOPES: Dict[str, Tuple[str, ...]] = {
    bundle.name: tuple(bundle.scopes) for bundle in ROLE_BUNDLES
}


def _validate_issuer(issuer: str) -> str:
    """Reject anything that isn't a bare https origin+path issuer identifier.

    An issuer identifier is the trust anchor for the whole flow: discovery is
    fetched from it and every ID token's `iss` is compared against it. A value
    carrying a query, a fragment, or userinfo is either a misconfiguration or an
    attempt to smuggle a second destination past the operator's eye, so it is
    refused rather than normalized.
    """
    parts = urlsplit(issuer)
    if parts.scheme not in ("https", "http"):
        raise ValueError(f"Identity provider issuer must be an https URL: {issuer!r}")
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError(
            f"Identity provider issuer must use https (http is allowed only for localhost): {issuer!r}"
        )
    if not parts.hostname:
        raise ValueError(f"Identity provider issuer has no host: {issuer!r}")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError(
            f"Identity provider issuer must not carry a query, fragment, or userinfo: {issuer!r}"
        )
    return issuer


class DevPersonaConfig(pyd.BaseModel):
    """One clickable identity offered by the development provider.

    Only meaningful for a `kind: dev` provider, which cannot exist outside a
    local deployment (`identity/dev_idp.py`). `claims` is merged into the ID
    token verbatim so a persona can carry whatever shape the real IdP will —
    including a nested one like Keycloak's `realm_access.roles`.
    """

    sub: str = pyd.Field(min_length=1, max_length=128)
    name: str = pyd.Field(default="", max_length=128)
    email: str = pyd.Field(default="", max_length=320)
    groups: List[str] = pyd.Field(default_factory=list, max_length=256)
    claims: Dict[str, object] = pyd.Field(default_factory=dict)
    describes: str = pyd.Field(default="", max_length=256)

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.field_validator("sub")
    @classmethod
    def _check_sub(cls, value: str) -> str:
        if not _SUBJECT_PREFIX_PATTERN.match(value):
            raise ValueError(
                f"Dev persona `sub` must be 1-32 chars of [A-Za-z0-9._:-]; got {value!r}."
            )
        return value


class IdentityProviderProfile(pyd.BaseModel):
    """One configured way for a human to sign in. **Carries the client secret.**

    Never returned from REST/MCP — `to_public()` produces the only shape that
    leaves the process.
    """

    id: str
    display_name: str = ""
    kind: ProviderKind = "oidc"
    enabled: bool = True

    # --- OIDC ---------------------------------------------------------------
    preset: str = "generic"
    issuer: str = ""
    tenant: str = ""
    domain: str = ""
    region: str = ""
    client_id: str = ""
    # Empty means a public client: the authorization-code flow still runs, with
    # PKCE (always S256, never optional) as the sole proof of possession.
    client_secret: str = ""
    # Extra OIDC scopes on top of the preset's. `openid` is always requested.
    scopes: List[str] = pyd.Field(default_factory=list)
    subject_claim: str = "sub"
    # Prepended to the IdP's subject claim to form `Principal.subject`.
    # Empty by default so a human's browser session and their agent's bearer
    # JWT resolve to the SAME identity — one policy entry, one audit subject.
    # Required once two OIDC providers are enabled, where opaque subject ids
    # from different issuers could otherwise collide (see IdentityConfigStore).
    subject_prefix: str = ""
    groups_claim: str = ""
    roles_claim: str = ""
    email_claim: str = ""
    name_claim: str = ""
    id_token_algorithms: List[str] = pyd.Field(default_factory=lambda: ["RS256", "ES256"])
    # Restrict sign-in to verified emails in these domains (Google Workspace's
    # `hd` problem, and a cheap second gate for any IdP). Empty = no restriction.
    allowed_email_domains: List[str] = pyd.Field(default_factory=list)
    leeway_seconds: float = pyd.Field(default=60, ge=0, le=300)
    # `kind: dev` only. Empty means the personas are derived from the mapping
    # rules, so a working mapping needs no persona configuration at all.
    users: List[DevPersonaConfig] = pyd.Field(default_factory=list)

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not _ID_PATTERN.match(value):
            raise ValueError(
                "Identity provider id must be lowercase alphanumeric with - or _ "
                f"(1-64 chars); got {value!r}."
            )
        return value

    @pyd.field_validator("subject_prefix")
    @classmethod
    def _check_subject_prefix(cls, value: str) -> str:
        if value and not _SUBJECT_PREFIX_PATTERN.match(value):
            raise ValueError(
                "Identity provider `subject_prefix` must be 1-32 chars of "
                f"[A-Za-z0-9._:-]; got {value!r}."
            )
        return value

    @pyd.field_validator("tenant")
    @classmethod
    def _check_tenant(cls, value: str) -> str:
        if value and not _TENANT_PATTERN.match(value):
            raise ValueError(f"Identity provider `tenant` has unsafe characters: {value!r}")
        return value

    @pyd.field_validator("domain")
    @classmethod
    def _check_domain(cls, value: str) -> str:
        if value and not _DOMAIN_PATTERN.match(value):
            raise ValueError(f"Identity provider `domain` has unsafe characters: {value!r}")
        return value

    @pyd.field_validator("region")
    @classmethod
    def _check_region(cls, value: str) -> str:
        if value and not _REGION_PATTERN.match(value):
            raise ValueError(f"Identity provider `region` has unsafe characters: {value!r}")
        return value

    @pyd.field_validator("scopes")
    @classmethod
    def _check_scopes(cls, value: List[str]) -> List[str]:
        for scope in value:
            if not _SCOPE_TOKEN_PATTERN.match(scope):
                raise ValueError(f"Invalid OIDC scope token: {scope!r}")
        return value

    @pyd.field_validator("allowed_email_domains")
    @classmethod
    def _check_email_domains(cls, value: List[str]) -> List[str]:
        for domain in value:
            if not _DOMAIN_PATTERN.match(domain):
                raise ValueError(f"Invalid allowed_email_domains entry: {domain!r}")
        return [domain.lower() for domain in value]

    @pyd.model_validator(mode="after")
    def _resolve_preset(self) -> "IdentityProviderProfile":
        if self.kind != "dev" and self.users:
            raise ValueError(
                f"Identity provider {self.id!r} sets `users:`, which only a kind=dev "
                "provider may do. Local accounts live in the users file, and an OIDC "
                "provider's users live in the IdP."
            )
        if self.kind == "local":
            if self.issuer or self.client_id or self.client_secret:
                raise ValueError(
                    f"Identity provider {self.id!r} is kind=local; it must not set "
                    "issuer/client_id/client_secret."
                )
            if not self.display_name:
                object.__setattr__(self, "display_name", "QueryGate accounts")
            return self
        if self.kind == "dev":
            # The development provider is a public client by construction — it
            # exists to avoid credential setup, so requiring one would defeat it.
            if self.client_secret:
                raise ValueError(
                    f"Identity provider {self.id!r} is kind=dev; it is a public client "
                    "and must not be given a client_secret."
                )
            if not self.display_name:
                object.__setattr__(self, "display_name", "Local development sign-in")

        preset = get_preset(self.preset)
        if not self.display_name:
            object.__setattr__(self, "display_name", preset.display_name)
        for claim_field, preset_value in (
            ("groups_claim", preset.groups_claim),
            ("roles_claim", preset.roles_claim),
            ("email_claim", preset.email_claim),
            ("name_claim", preset.name_claim),
        ):
            if not getattr(self, claim_field):
                object.__setattr__(self, claim_field, preset_value)

        if not self.issuer:
            if preset.issuer_template is None:
                raise ValueError(
                    f"Identity provider {self.id!r} uses preset {self.preset!r}, which has no "
                    "issuer template — set `issuer` explicitly."
                )
            missing = [field for field in preset.required_fields if not getattr(self, field)]
            if missing:
                raise ValueError(
                    f"Identity provider {self.id!r} (preset {self.preset!r}) requires "
                    f"{', '.join(missing)}. {preset.setup_note}"
                )
            object.__setattr__(
                self,
                "issuer",
                preset.issuer_template.format(
                    tenant=self.tenant, domain=self.domain, region=self.region
                ),
            )
        object.__setattr__(self, "issuer", _validate_issuer(self.issuer))

        if not self.client_id:
            raise ValueError(f"Identity provider {self.id!r} requires a client_id.")
        return self

    @property
    def authorization_scopes(self) -> List[str]:
        """The OIDC scopes requested at authorization time, `openid` first."""
        preset = get_preset(self.preset) if self.kind == "oidc" else None
        requested = ["openid"]
        for scope in list(preset.default_scopes if preset else ()) + list(self.scopes):
            if scope not in requested:
                requested.append(scope)
        return requested

    def to_public(self) -> "PublicIdentityProvider":
        return PublicIdentityProvider(
            id=self.id,
            display_name=self.display_name,
            kind=self.kind,
            enabled=self.enabled,
        )


class PublicIdentityProvider(pyd.BaseModel):
    """The only identity-provider shape that ever leaves REST/MCP.

    Structurally incapable of carrying a secret: there is no client_secret,
    issuer, tenant, or claim-name field on this model. A caller learns *that*
    a sign-in button exists and what to label it — nothing about how QueryGate
    trusts it. `tests/security/test_sso_boundary.py` asserts this against the
    live OpenAPI schema, not by convention.
    """

    id: str
    display_name: str
    kind: ProviderKind
    enabled: bool = True

    model_config = pyd.ConfigDict(extra="forbid")


class ClaimRule(pyd.BaseModel):
    """One deny-by-default grant: "this claim value earns these scopes".

    A rule grants; nothing here can revoke, and no rule is implied. A person
    who matches no rule authenticates successfully with **zero** scopes — they
    are a known human with no authority, which is the safe end state.
    """

    provider: str = "*"
    claim: str
    equals: Optional[str] = None
    any_of: List[str] = pyd.Field(default_factory=list)
    grant_scopes: List[str] = pyd.Field(default_factory=list)
    grant_roles: List[str] = pyd.Field(default_factory=list)
    description: str = ""

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.field_validator("grant_scopes")
    @classmethod
    def _known_scopes(cls, value: List[str]) -> List[str]:
        unknown = [scope for scope in value if scope not in ALL_SCOPES]
        if unknown:
            raise ValueError(
                f"Unknown QueryGate scope(s) in grant_scopes: {', '.join(sorted(unknown))}. "
                "See docs/SCOPE_CATALOG.md for the full vocabulary."
            )
        return value

    @pyd.field_validator("grant_roles")
    @classmethod
    def _known_roles(cls, value: List[str]) -> List[str]:
        unknown = [role for role in value if role not in _ROLE_BUNDLE_SCOPES]
        if unknown:
            raise ValueError(
                f"Unknown role bundle(s) in grant_roles: {', '.join(sorted(unknown))}. "
                f"Known bundles: {', '.join(_ROLE_BUNDLE_NAMES)}."
            )
        return value

    @pyd.model_validator(mode="after")
    def _check_match(self) -> "ClaimRule":
        if not self.claim:
            raise ValueError("A claim rule needs a `claim`.")
        if (self.equals is None) == (not self.any_of):
            raise ValueError(
                f"Claim rule on {self.claim!r} must set exactly one of `equals` or `any_of`."
            )
        if not self.grant_scopes and not self.grant_roles:
            raise ValueError(
                f"Claim rule on {self.claim!r} grants nothing — set grant_scopes or grant_roles."
            )
        return self

    @property
    def match_values(self) -> Tuple[str, ...]:
        return (self.equals,) if self.equals is not None else tuple(self.any_of)

    @property
    def granted_scopes(self) -> frozenset[str]:
        scopes = set(self.grant_scopes)
        for role in self.grant_roles:
            scopes.update(_ROLE_BUNDLE_SCOPES[role])
        return frozenset(scopes)


class SsoSettings(pyd.BaseModel):
    """Deployment-wide SSO settings from identity.yaml's `sso:` block."""

    # Public origin a browser reaches QueryGate on. The OIDC `redirect_uri` is
    # derived from it rather than from the inbound request, so a forged Host
    # header cannot redirect an authorization code somewhere else.
    base_url: str = ""
    session_ttl_seconds: float = pyd.Field(default=28800, ge=60, le=2592000)
    session_idle_timeout_seconds: float = pyd.Field(default=3600, ge=60, le=2592000)
    login_flow_ttl_seconds: float = pyd.Field(default=600, ge=30, le=3600)
    # Where a browser lands after a successful login. Must be a site-relative
    # path; an absolute URL here would be an open redirect.
    default_landing_path: str = "/admin/"

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        if not value:
            return value
        parts = urlsplit(value)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError(f"sso.base_url must be an absolute http(s) URL: {value!r}")
        if parts.query or parts.fragment:
            raise ValueError(f"sso.base_url must not carry a query or fragment: {value!r}")
        return value.rstrip("/")

    @pyd.field_validator("default_landing_path")
    @classmethod
    def _check_landing(cls, value: str) -> str:
        if not is_safe_relative_path(value):
            raise ValueError(f"sso.default_landing_path must be a site-relative path: {value!r}")
        return value


def is_safe_relative_path(value: str) -> str | bool:
    """True when `value` can only ever navigate within this origin.

    Used for both the configured landing path and a login request's
    `return_to`. A protocol-relative `//evil.example` and a backslash variant
    both parse as *absolute* in a browser while looking relative to a naive
    `startswith("/")` check, so both are rejected explicitly — this function is
    the open-redirect guard for the whole SSO surface.

    The explicit prefix checks and the `urlsplit` check below deliberately
    **overlap**: browsers and `urlsplit` do not agree on every malformed input,
    and this is not a place to depend on one parser's edge cases. Mutation
    testing confirms the consequence — removing either layer alone leaves the
    rule enforced by the other, so neither line is individually pinned by a
    test. That is the intended redundancy, not dead code; removing "the one
    that never fires" would leave the guard resting on a single parser.
    """
    if not value or not value.startswith("/"):
        return False
    if value.startswith("//") or value.startswith("/\\"):
        return False
    if "\\" in value or "\n" in value or "\r" in value:
        return False
    parts = urlsplit(value)
    return not parts.scheme and not parts.netloc


def role_bundle_scopes(name: str) -> Tuple[str, ...]:
    return _ROLE_BUNDLE_SCOPES.get(name, ())


def known_role_bundles() -> Tuple[str, ...]:
    return _ROLE_BUNDLE_NAMES


def _unused(_: Any) -> None:  # pragma: no cover - typing anchor
    return None
