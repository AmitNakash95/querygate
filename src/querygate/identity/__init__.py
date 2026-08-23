"""Human identity: SSO login, session management, and claim→scope mapping.

This package is the *human* half of authentication. `core/auth.py` stays the
single `Authenticator` boundary for bearer credentials presented by agents and
services; everything here resolves a **person** — through an external OIDC
identity provider, or through QueryGate's own built-in local identity provider
— into exactly the same `Principal` those transports already consume.

Nothing in here can widen what a caller may do: a login produces a `Principal`
whose scopes come solely from `mapping.IdentityMappingStore` (deny-by-default,
validated against `core/scopes.ALL_SCOPES` at load), and every downstream
policy/schema/compile check runs unchanged. No module here ever returns a
client secret, a password hash, a TOTP secret, a raw session id, or an ID
token to a caller.
"""
