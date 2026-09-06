"""Per-IdP defaults so an operator configures three fields, not twelve.

Every mainstream identity provider speaks OpenID Connect, so QueryGate needs
exactly one protocol implementation (`identity/oidc.py`). What actually differs
between them is *data*: the issuer URL's shape, which claim carries group
membership, which carries app roles, and which extra scope must be requested to
make those claims appear at all. That is what a `ProviderPreset` holds.

This is deliberately the "right-weight" form of the composable-interface rule
(CLAUDE.md): a preset carries no behavior — no per-provider branch, no
subclass, no override hook — only values the one shared OIDC flow reads. A
provider entry may override any preset field, and `generic` requires the
operator to supply `issuer` explicitly, so an IdP with no preset here is fully
supported on day one rather than blocked on a code change.

Issuer templates interpolate exactly three operator-supplied values —
`{tenant}`, `{domain}`, and `{region}` — each validated as hostname/slug-safe in
`models.py` before it is ever substituted. Three is the deliberate ceiling: an
IdP whose issuer needs a fourth moving part is configured with an explicit
`issuer` under the `generic` preset, which is a first-class path, not a
fallback. Growing a bespoke field per vendor would trade a small amount of
operator typing for an unbounded model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# Claim paths below are dotted paths read by `identity/claims.py`.


@dataclass(frozen=True)
class ProviderPreset:
    """Defaults for one identity provider product."""

    id: str
    display_name: str
    # `{tenant}`/`{domain}` are filled from the provider entry. None means the
    # operator must supply `issuer` directly.
    issuer_template: Optional[str]
    # Provider-entry fields that must be set for `issuer_template` to resolve.
    required_fields: Tuple[str, ...]
    # OIDC scopes requested at authorization time. `openid` is always added.
    default_scopes: Tuple[str, ...]
    groups_claim: str
    roles_claim: str
    email_claim: str
    name_claim: str
    # Operator-facing note surfaced in docs/errors — the one non-obvious step
    # this IdP needs before its group/role claims actually arrive in a token.
    setup_note: str


_PRESETS: Dict[str, ProviderPreset] = {
    "entra_id": ProviderPreset(
        id="entra_id",
        display_name="Microsoft Entra ID",
        issuer_template="https://login.microsoftonline.com/{tenant}/v2.0",
        required_fields=("tenant",),
        default_scopes=("profile", "email"),
        groups_claim="groups",
        roles_claim="roles",
        email_claim="email",
        name_claim="name",
        setup_note=(
            "In the app registration, set Token configuration → Add groups claim "
            "(Security groups) to emit `groups` as object IDs, and define App roles "
            "to emit `roles`. Map the object IDs in identity.yaml, not display names."
        ),
    ),
    "okta": ProviderPreset(
        id="okta",
        display_name="Okta",
        issuer_template="https://{domain}",
        required_fields=("domain",),
        default_scopes=("profile", "email", "groups"),
        groups_claim="groups",
        roles_claim="groups",
        email_claim="email",
        name_claim="name",
        setup_note=(
            "Add a `groups` claim to the ID token in the authorization server's "
            "Claims tab. For a custom authorization server set `issuer` explicitly "
            "to https://<domain>/oauth2/<server-id> instead of using `domain`."
        ),
    ),
    "auth0": ProviderPreset(
        id="auth0",
        display_name="Auth0",
        issuer_template="https://{domain}/",
        required_fields=("domain",),
        default_scopes=("profile", "email"),
        groups_claim="groups",
        roles_claim="roles",
        email_claim="email",
        name_claim="name",
        setup_note=(
            "Auth0 does not emit groups/roles by default. Add a Login Action that "
            "sets a custom claim, then point `groups_claim` at its full namespaced "
            "name (e.g. https://querygate/groups)."
        ),
    ),
    "google": ProviderPreset(
        id="google",
        display_name="Google Workspace",
        issuer_template="https://accounts.google.com",
        required_fields=(),
        default_scopes=("profile", "email"),
        groups_claim="groups",
        roles_claim="groups",
        email_claim="email",
        name_claim="name",
        setup_note=(
            "Google ID tokens carry no group membership. Restrict sign-in with a "
            "`hd` (hosted domain) rule and map authority per-user by the `email` "
            "claim, or front Google with an IdP that does emit groups."
        ),
    ),
    "keycloak": ProviderPreset(
        id="keycloak",
        display_name="Keycloak",
        issuer_template="https://{domain}/realms/{tenant}",
        required_fields=("domain", "tenant"),
        default_scopes=("profile", "email", "roles"),
        groups_claim="groups",
        roles_claim="realm_access.roles",
        email_claim="email",
        name_claim="name",
        setup_note=(
            "Add a `groups` client scope mapper (Group Membership, full path off) "
            "to emit `groups`. Realm roles already arrive nested at "
            "realm_access.roles, which the default roles_claim reads."
        ),
    ),
    "authentik": ProviderPreset(
        id="authentik",
        display_name="authentik",
        issuer_template="https://{domain}/application/o/{tenant}/",
        required_fields=("domain", "tenant"),
        default_scopes=("profile", "email"),
        groups_claim="groups",
        roles_claim="groups",
        email_claim="email",
        name_claim="name",
        setup_note="`tenant` is the application slug. Add the `groups` scope mapping to the provider.",
    ),
    "ping_identity": ProviderPreset(
        id="ping_identity",
        display_name="Ping Identity (PingOne)",
        issuer_template="https://auth.pingone.com/{tenant}/as",
        required_fields=("tenant",),
        default_scopes=("profile", "email"),
        groups_claim="groups",
        roles_claim="groups",
        email_claim="email",
        name_claim="name",
        setup_note=(
            "`tenant` is the PingOne environment ID. Add a group-membership "
            "attribute mapping to the application so `groups` is populated."
        ),
    ),
    "onelogin": ProviderPreset(
        id="onelogin",
        display_name="OneLogin",
        issuer_template="https://{domain}/oidc/2",
        required_fields=("domain",),
        default_scopes=("profile", "email", "groups"),
        groups_claim="groups",
        roles_claim="groups",
        email_claim="email",
        name_claim="name",
        setup_note="Enable the `groups` scope on the OIDC app and set Groups to the roles attribute.",
    ),
    "jumpcloud": ProviderPreset(
        id="jumpcloud",
        display_name="JumpCloud",
        issuer_template="https://oauth.id.jumpcloud.com/",
        required_fields=(),
        default_scopes=("profile", "email"),
        groups_claim="groups",
        roles_claim="groups",
        email_claim="email",
        name_claim="name",
        setup_note="Attach user groups to the SSO application so the `groups` claim is emitted.",
    ),
    "gitlab": ProviderPreset(
        id="gitlab",
        display_name="GitLab",
        issuer_template="https://{domain}",
        required_fields=("domain",),
        default_scopes=("profile", "email"),
        groups_claim="groups_direct",
        roles_claim="groups_direct",
        email_claim="email",
        name_claim="name",
        setup_note="Use gitlab.com as `domain` for SaaS. Group paths arrive in `groups_direct`.",
    ),
    "cognito": ProviderPreset(
        id="cognito",
        display_name="Amazon Cognito",
        issuer_template="https://cognito-idp.{region}.amazonaws.com/{tenant}",
        required_fields=("region", "tenant"),
        default_scopes=("profile", "email"),
        groups_claim="cognito:groups",
        roles_claim="cognito:groups",
        email_claim="email",
        name_claim="name",
        setup_note=(
            "`region` is the pool's AWS region (e.g. eu-west-1) and `tenant` is the "
            "user pool id (e.g. eu-west-1_AbCdEf123). Cognito emits group membership "
            "as `cognito:groups`, which the default groups_claim already reads."
        ),
    ),
    "adfs": ProviderPreset(
        id="adfs",
        display_name="Microsoft AD FS",
        issuer_template="https://{domain}/adfs",
        required_fields=("domain",),
        default_scopes=("profile", "email"),
        groups_claim="groups",
        roles_claim="roles",
        email_claim="email",
        name_claim="name",
        setup_note=(
            "Register QueryGate as a Server application + Web API in the AD FS "
            "application group, and add an issuance transform rule emitting group "
            "membership as a `groups` claim."
        ),
    ),
    "pingfederate": ProviderPreset(
        id="pingfederate",
        display_name="PingFederate",
        issuer_template="https://{domain}",
        required_fields=("domain",),
        default_scopes=("profile", "email"),
        groups_claim="groups",
        roles_claim="groups",
        email_claim="email",
        name_claim="name",
        setup_note=(
            "Distinct from the cloud `ping_identity` (PingOne) preset. Add a group "
            "attribute to the OIDC policy's contract so `groups` is populated."
        ),
    ),
    "zitadel": ProviderPreset(
        id="zitadel",
        display_name="ZITADEL",
        issuer_template="https://{domain}",
        required_fields=("domain",),
        default_scopes=("profile", "email", "urn:zitadel:iam:org:project:roles"),
        groups_claim="urn:zitadel:iam:org:project:roles",
        roles_claim="urn:zitadel:iam:org:project:roles",
        email_claim="email",
        name_claim="name",
        setup_note=(
            "The roles scope above must be requested for roles to appear at all. "
            "ZITADEL emits them as an object keyed by role name, so match on the "
            "role key with a dotted claim path if you need a specific grant."
        ),
    ),
    "authelia": ProviderPreset(
        id="authelia",
        display_name="Authelia",
        issuer_template="https://{domain}",
        required_fields=("domain",),
        default_scopes=("profile", "email", "groups"),
        groups_claim="groups",
        roles_claim="groups",
        email_claim="email",
        name_claim="name",
        setup_note="Grant the `groups` scope to the client in Authelia's OIDC configuration.",
    ),
    "cloudflare_access": ProviderPreset(
        id="cloudflare_access",
        display_name="Cloudflare Access",
        issuer_template="https://{domain}.cloudflareaccess.com",
        required_fields=("domain",),
        default_scopes=("profile", "email", "groups"),
        groups_claim="groups",
        roles_claim="groups",
        email_claim="email",
        name_claim="name",
        setup_note=(
            "`domain` is your Zero Trust team name. Add the `groups` scope to the "
            "SaaS/OIDC application so identity-provider groups are forwarded."
        ),
    ),
    "workos": ProviderPreset(
        id="workos",
        display_name="WorkOS AuthKit",
        issuer_template="https://{domain}",
        required_fields=("domain",),
        default_scopes=("profile", "email"),
        groups_claim="groups",
        roles_claim="role",
        email_claim="email",
        name_claim="name",
        setup_note=(
            "`domain` is your AuthKit domain (e.g. example.authkit.app). WorkOS "
            "emits a single organisation role in `role`; use `equals` rather than "
            "`any_of` unless you have configured a multi-valued claim."
        ),
    ),
    "fusionauth": ProviderPreset(
        id="fusionauth",
        display_name="FusionAuth",
        issuer_template="https://{domain}",
        required_fields=("domain",),
        default_scopes=("profile", "email"),
        groups_claim="groups",
        roles_claim="roles",
        email_claim="email",
        name_claim="name",
        setup_note=(
            "Enable the `roles` and `groups` claims on the application's JWT "
            "populate lambda; FusionAuth omits both by default."
        ),
    ),
    "salesforce": ProviderPreset(
        id="salesforce",
        display_name="Salesforce",
        issuer_template="https://{domain}",
        required_fields=("domain",),
        default_scopes=("profile", "email"),
        groups_claim="groups",
        roles_claim="groups",
        email_claim="email",
        name_claim="name",
        setup_note=(
            "`domain` is login.salesforce.com, test.salesforce.com, or your My "
            "Domain host. Salesforce emits no groups by default — add a custom "
            "attribute to the connected app, or map authority per-user by email."
        ),
    ),
    "generic": ProviderPreset(
        id="generic",
        display_name="OpenID Connect",
        issuer_template=None,
        required_fields=(),
        default_scopes=("profile", "email"),
        groups_claim="groups",
        roles_claim="roles",
        email_claim="email",
        name_claim="name",
        setup_note=(
            "Set `issuer` to the IdP's exact issuer identifier; discovery does the "
            "rest. This is the supported path for every IdP without a preset above — "
            "including ones whose issuer needs more than tenant/domain/region, such "
            "as Azure AD B2C, Duo SSO, Curity, and Ory Hydra."
        ),
    ),
}


def get_preset(preset_id: str) -> ProviderPreset:
    try:
        return _PRESETS[preset_id]
    except KeyError:
        raise ValueError(
            f"Unknown identity provider preset {preset_id!r}. "
            f"Known presets: {', '.join(sorted(_PRESETS))}."
        ) from None


def preset_ids() -> Tuple[str, ...]:
    return tuple(sorted(_PRESETS))


def all_presets() -> Tuple[ProviderPreset, ...]:
    return tuple(_PRESETS[key] for key in sorted(_PRESETS))
