"""Password verifiers for the built-in local identity provider.

Uses `hashlib.scrypt` — a memory-hard KDF in the standard library — rather than
adding an argon2 binding. That is a deliberate supply-chain choice: the local
IdP is a fallback for deployments that have *no* external IdP, often air-gapped
ones, and it should not be the reason QueryGate grows a new native dependency.
scrypt at the default parameters below is a sound 2026 choice for an
interactive login (OWASP's minimum is N=2^17/r=8/p=1 for scrypt used alone;
QueryGate's default N=2^16 with r=8 sits alongside per-account lockout in
`local_auth.py`, which is what actually bounds online guessing).

The stored verifier is self-describing — `scrypt$N$r$p$salt$hash` — so raising
the cost parameters later re-hashes on next login instead of invalidating every
existing password. Nothing here ever logs, returns, or compares a password
except in constant time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Tuple

SCHEME = "scrypt"
DEFAULT_N = 2**16
DEFAULT_R = 8
DEFAULT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32
# Refuse to *evaluate* a verifier costlier than this. A users.yaml is operator
# controlled, but a mistyped N would otherwise turn one login into an OOM.
MAX_N = 2**20
# An unbounded password is a hashing-cost amplifier; 1024 bytes is far past any
# real passphrase.
MAX_PASSWORD_BYTES = 1024
MIN_PASSWORD_LENGTH = 12


class PasswordError(ValueError):
    """A password or verifier was rejected. Message never contains either."""


@dataclass(frozen=True)
class ScryptParams:
    n: int = DEFAULT_N
    r: int = DEFAULT_R
    p: int = DEFAULT_P

    def validate(self) -> "ScryptParams":
        if self.n < 2**12 or self.n > MAX_N or (self.n & (self.n - 1)) != 0:
            raise PasswordError("scrypt N must be a power of two between 2^12 and 2^20.")
        if not 1 <= self.r <= 32 or not 1 <= self.p <= 16:
            raise PasswordError("scrypt r must be 1-32 and p must be 1-16.")
        return self

    @property
    def maxmem(self) -> int:
        # OpenSSL's default 32 MiB ceiling is below what N=2^16/r=8 needs, and
        # exceeding it raises instead of degrading — so state the budget.
        return 256 * self.n * self.r * self.p


def _derive(password: str, salt: bytes, params: ScryptParams) -> bytes:
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise PasswordError("Password is too long.")
    return hashlib.scrypt(
        encoded,
        salt=salt,
        n=params.n,
        r=params.r,
        p=params.p,
        maxmem=params.maxmem,
        dklen=KEY_BYTES,
    )


def hash_password(password: str, params: ScryptParams | None = None) -> str:
    """Produce a self-describing verifier for `password`."""
    resolved = (params or ScryptParams()).validate()
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _derive(password, salt, resolved)
    return "${}${}${}${}${}${}".format(
        SCHEME,
        resolved.n,
        resolved.r,
        resolved.p,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    ).lstrip("$")


def parse_verifier(verifier: str) -> Tuple[ScryptParams, bytes, bytes]:
    parts = verifier.split("$")
    if len(parts) != 6 or parts[0] != SCHEME:
        raise PasswordError("Stored password verifier is not in the expected scrypt format.")
    try:
        params = ScryptParams(n=int(parts[1]), r=int(parts[2]), p=int(parts[3])).validate()
        salt = base64.b64decode(parts[4], validate=True)
        digest = base64.b64decode(parts[5], validate=True)
    except (ValueError, PasswordError) as exc:
        raise PasswordError("Stored password verifier is malformed.") from exc
    if not salt or not digest:
        raise PasswordError("Stored password verifier is malformed.")
    return params, salt, digest


def verify_password(password: str, verifier: str) -> bool:
    """Constant-time check of `password` against a stored verifier."""
    try:
        params, salt, expected = parse_verifier(verifier)
        candidate = _derive(password, salt, params)
    except PasswordError:
        return False
    return hmac.compare_digest(candidate, expected)


def needs_rehash(verifier: str, params: ScryptParams | None = None) -> bool:
    """True when a verifier was made with weaker parameters than current policy."""
    target = (params or ScryptParams()).validate()
    try:
        stored, _, _ = parse_verifier(verifier)
    except PasswordError:
        return True
    return (stored.n, stored.r, stored.p) < (target.n, target.r, target.p)


def check_password_policy(
    password: str, *, username: str, min_length: int = MIN_PASSWORD_LENGTH
) -> None:
    """Reject passwords a local account must not be created with.

    Deliberately minimal: length, byte ceiling, and "not the username". A long
    denylist belongs in the customer's IdP — the local provider exists for
    deployments that have none, and pretending otherwise would be theatre.
    """
    if len(password) < min_length:
        raise PasswordError(f"Password must be at least {min_length} characters.")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise PasswordError("Password is too long.")
    if password.strip().lower() == username.strip().lower():
        raise PasswordError("Password must not be the username.")
