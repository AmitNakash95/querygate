"""Which *kinds* of credential each surface accepts (TODO.md item 200).

Authentication answers "who is this". This module answers a different question
the codebase previously could not express at all: **"is this an acceptable way
to prove it, here?"**

Before this, every configured credential scheme worked everywhere. Enabling SSO
added a nicer door beside the static API key rather than closing it, so an
operator who had done the whole IdP integration still had a shared secret that
opened the admin console — and every action taken with it was attributable to a
config entry rather than to a person. That is precisely the property the Proof
pillar claims (`CLAUDE.md`’s "North Star" section), so leaving it to convention was
the wrong call.

The control is a per-surface allowlist over `Principal.auth_method`:

* **console** — everything under `<api_v1_prefix>/admin`, the governed control
  plane the admin UI drives;
* **rest** — the rest of the REST API, where agents and services live;
* **mcp** — the MCP transport.

The default is the interesting part, and it is deliberately *not* "allow
everything": **turning SSO on closes the console to shared secrets.** An
operator who has integrated an identity provider has already said humans are
identified by that provider; continuing to honour a static key on the console
would contradict the thing they just configured. A deployment that genuinely
needs it says so explicitly, which makes it a reviewable decision rather than
an ambient default. Deployments with SSO off are untouched.

Enforcement is a single check on the **resolved** principal, not a matter of
which authenticators got built. That ordering is deliberate: it means a
credential scheme added later is governed by this policy automatically instead
of silently inheriting access to every surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from querygate.core.auth import Principal

# Every `auth_method` a Principal can carry out of the authentication chain.
# This is the whole vocabulary, and it is closed: a new authenticator must add
# its method here, which is also what makes a typo in configuration a loud
# error instead of a silently-empty allowlist.
API_KEY_METHOD = "api_key"
JWT_METHOD = "jwt"
ANONYMOUS_METHOD = "anonymous"
SSO_SESSION_METHOD = "sso_session"
DEVICE_TOKEN_METHOD = "sso_device_token"  # nosec B105 - a method label, not a token

CALLER_AUTH_METHODS: frozenset[str] = frozenset(
    {API_KEY_METHOD, JWT_METHOD, ANONYMOUS_METHOD, SSO_SESSION_METHOD, DEVICE_TOKEN_METHOD}
)

# Credentials that identify a *deployment* rather than a *person*: a static key
# shared by everyone who has it, and the local dev bypass that identifies
# nobody at all. Neither can answer "which human did this", which is the whole
# question the console exists to have an answer to.
SHARED_SECRET_METHODS: frozenset[str] = frozenset({API_KEY_METHOD, ANONYMOUS_METHOD})

CONSOLE_SURFACE = "console"
REST_SURFACE = "rest"
MCP_SURFACE = "mcp"

_SURFACE_LABELS = {
    CONSOLE_SURFACE: "the admin control plane",
    REST_SURFACE: "the REST API",
    MCP_SURFACE: "the MCP surface",
}


class AuthMethodNotPermitted(Exception):
    """The caller authenticated, but not in a way this surface accepts.

    Distinct from a failed authentication on purpose, and callers map it to
    **403, not 401**: retrying with the same credential will never work, and a
    401 would invite exactly that.
    """

    def __init__(self, method: str, surface: str, allowed: frozenset[str]) -> None:
        self.method = method
        self.surface = surface
        self.allowed = allowed
        super().__init__(self.message)

    @property
    def message(self) -> str:
        # Names what IS accepted rather than what was refused. An operator
        # debugging this needs the target, and the accepted set is not a
        # secret — it is deployment policy the person is entitled to know.
        label = _SURFACE_LABELS.get(self.surface, self.surface)
        accepted = ", ".join(sorted(self.allowed)) or "(nothing — check configuration)"
        return f"This credential type is not accepted on {label}. " f"Accepted here: {accepted}."


def validate_methods(configured: Iterable[str], *, field: str) -> list[str]:
    """Reject an unknown method name at config load, not at request time.

    Same posture as an unknown scope in a claim→scope rule: a typo that
    silently narrowed an allowlist would look like a working deployment right
    up until the moment somebody could not sign in.
    """
    methods = [str(method).strip() for method in configured if str(method).strip()]
    unknown = sorted(set(methods) - CALLER_AUTH_METHODS)
    if unknown:
        raise ValueError(
            f"{field} names unknown authentication method(s): {', '.join(unknown)}. "
            f"Valid methods: {', '.join(sorted(CALLER_AUTH_METHODS))}."
        )
    return methods


def default_methods(surface: str, *, sso_enabled: bool) -> frozenset[str]:
    """What a surface accepts when the operator has not said.

    The console drops shared secrets the moment SSO is configured — see the
    module docstring for why that is a default rather than a suggestion. Every
    other surface stays permissive, because agents and services legitimately
    authenticate with tokens rather than sessions.
    """
    if surface == CONSOLE_SURFACE and sso_enabled:
        return CALLER_AUTH_METHODS - SHARED_SECRET_METHODS
    return CALLER_AUTH_METHODS


def resolve_allowed(
    configured: Iterable[str], *, surface: str, sso_enabled: bool
) -> frozenset[str]:
    methods = frozenset(configured)
    if not methods:
        return default_methods(surface, sso_enabled=sso_enabled)
    return methods


class AuthMethodPolicy:
    """The allowlist for one surface, checked against a resolved principal."""

    def __init__(self, allowed: frozenset[str], surface: str) -> None:
        self._allowed = allowed
        self._surface = surface

    @property
    def allowed(self) -> frozenset[str]:
        return self._allowed

    @property
    def surface(self) -> str:
        return self._surface

    def permits(self, method: str) -> bool:
        return method in self._allowed

    def check(self, principal: "Principal") -> None:
        """Raise `AuthMethodNotPermitted` if this credential type is refused here."""
        if principal.auth_method not in self._allowed:
            raise AuthMethodNotPermitted(principal.auth_method, self._surface, self._allowed)

    @classmethod
    def build(
        cls, configured: Iterable[str], *, surface: str, sso_enabled: bool
    ) -> "AuthMethodPolicy":
        return cls(resolve_allowed(configured, surface=surface, sso_enabled=sso_enabled), surface)


def surface_for_path(path: str, api_v1_prefix: str) -> str:
    """Which surface a REST request belongs to.

    The console is a path prefix rather than a separate transport, because that
    is what it actually is: the admin UI is a browser client of
    `<api_v1_prefix>/admin/*`. Every admin router mounts under that prefix
    (`test_auth_method_policy.py` asserts it), so the boundary is checkable
    rather than a naming convention someone can drift out of.
    """
    admin_prefix = f"{api_v1_prefix.rstrip('/')}/admin"
    return CONSOLE_SURFACE if path.startswith(admin_prefix) else REST_SURFACE


def describe(policy: Optional[AuthMethodPolicy]) -> str:
    """One-line summary for the startup log."""
    if policy is None:
        return "unrestricted"
    return ",".join(sorted(policy.allowed))
