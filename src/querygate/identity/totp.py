"""RFC 6238 TOTP for the built-in local identity provider.

Second factor for local accounts, implemented against the stdlib (`hmac`,
`hashlib`, `base64`) rather than a TOTP package — the algorithm is thirty lines
and the local IdP's whole reason to exist is deployments that cannot pull in
more surface. Interoperable with any authenticator app: SHA-1, 6 digits, 30s
step, which is what the `otpauth://` URI defaults to.

Replay is the failure mode people forget: a code stays valid for its whole step
(plus the drift window), so an observed code can be reused inside that window.
`consumed_counter` is returned alongside the verification result so
`local_auth.py` can record it and refuse any counter it has already accepted
for that account.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

DIGITS = 6
STEP_SECONDS = 30
# One step either side: tolerates ~30s of clock drift, which is the standard
# recommendation. Widening this multiplies the codes valid at any instant.
DEFAULT_DRIFT_STEPS = 1
SECRET_BYTES = 20  # 160 bits, the RFC 4226 recommendation for HMAC-SHA1


class TotpError(ValueError):
    """A TOTP secret or code was malformed."""


@dataclass(frozen=True)
class TotpResult:
    valid: bool
    # The time-step counter the accepted code belongs to. None when invalid.
    consumed_counter: Optional[int] = None


def new_totp_secret() -> str:
    """A fresh base32 secret, in the form an authenticator app expects."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def _decode_secret(secret: str) -> bytes:
    cleaned = secret.strip().replace(" ", "").upper()
    padding = "=" * (-len(cleaned) % 8)
    try:
        raw = base64.b32decode(cleaned + padding, casefold=True)
    except (ValueError, TypeError) as exc:
        raise TotpError("TOTP secret is not valid base32.") from exc
    if len(raw) < 10:
        raise TotpError("TOTP secret is too short.")
    return raw


def totp_code(secret: str, counter: int) -> str:
    key = _decode_secret(secret)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10**DIGITS)).zfill(DIGITS)


def verify_totp(
    secret: str,
    code: str,
    *,
    at: Optional[float] = None,
    drift_steps: int = DEFAULT_DRIFT_STEPS,
    last_consumed_counter: Optional[int] = None,
) -> TotpResult:
    """Check `code`, refusing any counter already consumed by this account."""
    candidate = (code or "").strip().replace(" ", "")
    if len(candidate) != DIGITS or not candidate.isdigit():
        return TotpResult(valid=False)
    now = time.time() if at is None else at
    current = int(now // STEP_SECONDS)
    for offset in range(-drift_steps, drift_steps + 1):
        counter = current + offset
        if counter < 0:
            continue
        if last_consumed_counter is not None and counter <= last_consumed_counter:
            # Already used (or older than) a code this account has spent —
            # refuse the replay even though the HMAC would still match.
            continue
        if hmac.compare_digest(totp_code(secret, counter), candidate):
            return TotpResult(valid=True, consumed_counter=counter)
    return TotpResult(valid=False)


def provisioning_uri(secret: str, *, account: str, issuer: str = "QueryGate") -> str:
    """The `otpauth://` URI an authenticator app scans.

    Contains the shared secret, so it is shown once at enrolment, never stored,
    never logged, and never returned by a read endpoint.
    """
    label = quote(f"{issuer}:{account}", safe="")
    query = f"secret={quote(secret)}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
    return f"otpauth://totp/{label}?{query}"
