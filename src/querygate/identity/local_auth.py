"""Authenticating a local account: lockout, password check, second factor.

Everything that makes a password login survive contact with the internet lives
here rather than in the route, so the REST surface, a future CLI `login`, and
tests all go through one implementation.

* **Per-account lockout.** scrypt makes offline cracking expensive; only a
  lockout makes *online* guessing expensive. After `lockout_threshold` failures
  the account stops answering for `lockout_seconds`, and a successful login
  clears the counter.
* **No user enumeration.** A missing account, a disabled account, a wrong
  password, and a wrong TOTP code all produce the same message and the same
  work: an absent user is checked against a fixed dummy verifier so the
  response time does not reveal whether the username exists.
* **A bound on hashing concurrency.** scrypt is memory-hard *by design*, which
  means unbounded parallel logins are a self-inflicted resource exhaustion. A
  semaphore caps how many verifications run at once; excess attempts wait.
* **TOTP replay refusal.** An accepted time-step counter is recorded per
  account and never accepted again, closing the window where an observed code
  stays valid.

Attempt state (failure counts, lockouts, consumed TOTP counters) is deliberately
*not* written back into users.yaml — that file holds durable credentials and
should not be rewritten on every failed password attempt. It lives in a small
bounded store with the same in-process/shared split the concurrency limiter and
quota store use.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Dict, NoReturn, Optional, Protocol

from querygate.identity.local_store import LocalUser, LocalUserStore
from querygate.identity.passwords import hash_password, verify_password
from querygate.identity.totp import verify_totp

# A verifier for a password nobody holds, derived once at import. Checking an
# unknown username against it costs the same as checking a real one.
_DUMMY_VERIFIER = hash_password("querygate-nonexistent-account-placeholder")
# Concurrent scrypt derivations. Each holds ~64 MiB at the default parameters,
# so this is a memory bound as much as a CPU one.
DEFAULT_MAX_CONCURRENT_VERIFICATIONS = 4
# Bound on tracked accounts, so failed logins for random usernames cannot grow
# the attempt store without limit.
MAX_TRACKED_ACCOUNTS = 50000

# The single message every local-login failure returns. Never says which part
# was wrong.
GENERIC_FAILURE_MESSAGE = "Sign-in failed. Check your username, password, and code."


class LocalLoginError(Exception):
    """A local sign-in attempt failed. `category` is safe to audit."""

    def __init__(self, category: str, message: str = GENERIC_FAILURE_MESSAGE) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


class LockedOutError(LocalLoginError):
    def __init__(self, retry_after_seconds: float) -> None:
        super().__init__(
            "locked_out",
            "Too many failed sign-in attempts. Try again later.",
        )
        self.retry_after_seconds = retry_after_seconds


@dataclass
class _AttemptRecord:
    failures: int = 0
    locked_until: float = 0.0
    last_totp_counter: Optional[int] = None
    updated_at: float = 0.0


class LoginAttemptStore(Protocol):
    """Transient per-account login state.

    Async from the start (CLAUDE.md's store gotcha) so a Redis-backed
    cross-replica variant can be added without converting any call site: in a
    multi-replica deployment an in-process lockout counts failures per replica,
    which multiplies the real threshold by the replica count.
    """

    async def lockout_remaining(self, key: str) -> float: ...

    async def record_failure(self, key: str, threshold: int, lockout_seconds: float) -> float: ...

    async def record_success(self, key: str) -> None: ...

    async def consume_totp_counter(self, key: str, counter: int) -> bool: ...

    async def last_totp_counter(self, key: str) -> Optional[int]: ...


class InProcessLoginAttemptStore:
    """Single-replica attempt tracking."""

    def __init__(self, max_entries: int = MAX_TRACKED_ACCOUNTS) -> None:
        self._records: Dict[str, _AttemptRecord] = {}
        self._max_entries = max_entries

    def _record(self, key: str) -> _AttemptRecord:
        record = self._records.get(key)
        if record is None:
            if len(self._records) >= self._max_entries:
                oldest = min(self._records.items(), key=lambda item: item[1].updated_at)[0]
                self._records.pop(oldest, None)
            record = _AttemptRecord()
            self._records[key] = record
        record.updated_at = time.monotonic()
        return record

    async def lockout_remaining(self, key: str) -> float:
        record = self._records.get(key)
        if record is None:
            return 0.0
        return max(0.0, record.locked_until - time.monotonic())

    async def record_failure(self, key: str, threshold: int, lockout_seconds: float) -> float:
        record = self._record(key)
        record.failures += 1
        if threshold > 0 and record.failures >= threshold:
            record.locked_until = time.monotonic() + lockout_seconds
            record.failures = 0
            return lockout_seconds
        return 0.0

    async def record_success(self, key: str) -> None:
        record = self._record(key)
        record.failures = 0
        record.locked_until = 0.0

    async def consume_totp_counter(self, key: str, counter: int) -> bool:
        record = self._record(key)
        if record.last_totp_counter is not None and counter <= record.last_totp_counter:
            return False
        record.last_totp_counter = counter
        return True

    async def last_totp_counter(self, key: str) -> Optional[int]:
        record = self._records.get(key)
        return record.last_totp_counter if record else None

    def clear(self) -> None:
        self._records.clear()


_in_process_attempts = InProcessLoginAttemptStore()
_attempt_store: Optional[LoginAttemptStore] = None
_verification_semaphore: Optional[asyncio.Semaphore] = None
_semaphore_limit = DEFAULT_MAX_CONCURRENT_VERIFICATIONS


def get_login_attempt_store() -> LoginAttemptStore:
    return _attempt_store or _in_process_attempts


def set_login_attempt_store(store: Optional[LoginAttemptStore]) -> None:
    global _attempt_store
    _attempt_store = store


def clear_login_attempt_state() -> None:
    """Reset transient login state — used by the test fixture, and on shutdown."""
    global _verification_semaphore
    _in_process_attempts.clear()
    _verification_semaphore = None


def _semaphore() -> asyncio.Semaphore:
    # Bound to the running loop, like `execution/concurrency.py`'s — recreated
    # per loop by `clear_login_attempt_state()` in the test fixture.
    global _verification_semaphore
    if _verification_semaphore is None:
        _verification_semaphore = asyncio.Semaphore(_semaphore_limit)
    return _verification_semaphore


@dataclass(frozen=True)
class LocalLoginResult:
    user: LocalUser
    used_mfa: bool


async def authenticate_local_user(
    *,
    store: LocalUserStore,
    username: str,
    password: str,
    totp_code: Optional[str] = None,
    lockout_threshold: int = 5,
    lockout_seconds: float = 900.0,
    require_mfa: bool = False,
) -> LocalLoginResult:
    """Verify a local account's password (and second factor, if enrolled)."""
    key = (username or "").strip().lower()
    if not key:
        raise LocalLoginError("missing_username")

    attempts = get_login_attempt_store()
    remaining = await attempts.lockout_remaining(key)
    if remaining > 0:
        raise LockedOutError(remaining)

    user = store.get(key)
    verifier = user.password_verifier if user and user.password_verifier else _DUMMY_VERIFIER

    async with _semaphore():
        password_ok = await asyncio.to_thread(verify_password, password or "", verifier)

    # Evaluate every reason for failure the same way, so neither the response
    # nor the timing distinguishes "no such user" from "wrong password".
    if user is None or user.disabled or not user.password_verifier or not password_ok:
        await _fail(attempts, key, lockout_threshold, lockout_seconds, "invalid_credentials")
        # Unreachable: `_fail` is typed NoReturn and always raises. Written as a
        # real branch rather than an `assert user is not None`, because an
        # assert is stripped under `python -O` — and the thing it would be
        # guarding here is whether an unauthenticated caller proceeds.
        raise LocalLoginError("invalid_credentials")

    used_mfa = False
    if user.mfa_enabled:
        if not totp_code:
            await _fail(attempts, key, lockout_threshold, lockout_seconds, "mfa_required")
        # Verification and replay are two separate questions, answered in that
        # order and by two different components: `verify_totp` says the code is
        # cryptographically right for some step in the drift window, and the
        # attempt store says whether this account has already spent that step.
        # Keeping them apart is what lets a replay be *audited as a replay*
        # rather than as a generic bad code — and it is the only ordering under
        # which two concurrent logins presenting the same code cannot both win.
        result = verify_totp(user.totp_secret, totp_code or "")
        if not result.valid or result.consumed_counter is None:
            await _fail(attempts, key, lockout_threshold, lockout_seconds, "invalid_mfa_code")
        if not await attempts.consume_totp_counter(key, result.consumed_counter):
            await _fail(attempts, key, lockout_threshold, lockout_seconds, "replayed_mfa_code")
        used_mfa = True
    elif require_mfa:
        # An operator can require a second factor deployment-wide; an account
        # that has not enrolled one is refused rather than silently exempted.
        await _fail(attempts, key, lockout_threshold, lockout_seconds, "mfa_not_enrolled")

    await attempts.record_success(key)
    return LocalLoginResult(user=user, used_mfa=used_mfa)


async def _fail(
    attempts: LoginAttemptStore,
    key: str,
    threshold: int,
    lockout_seconds: float,
    category: str,
) -> NoReturn:
    locked_for = await attempts.record_failure(key, threshold, lockout_seconds)
    if locked_for > 0:
        raise LockedOutError(locked_for)
    raise LocalLoginError(category)


def dummy_verifier() -> str:
    """Exposed for the test that asserts an unknown user is still hashed."""
    return _DUMMY_VERIFIER


def verification_concurrency() -> int:
    return _semaphore_limit


def set_verification_concurrency(limit: int) -> None:
    global _semaphore_limit, _verification_semaphore
    _semaphore_limit = max(1, limit)
    _verification_semaphore = None


def totp_key_for(username: str) -> str:
    return (username or "").strip().lower()
