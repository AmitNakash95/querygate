"""The built-in local identity provider: verifiers, TOTP, lockout, user file."""

from __future__ import annotations

import stat
import time
from pathlib import Path

import pytest
import yaml

from querygate.identity.local_auth import (
    GENERIC_FAILURE_MESSAGE,
    LocalLoginError,
    LockedOutError,
    authenticate_local_user,
    dummy_verifier,
)
from querygate.identity.local_store import (
    LocalUser,
    LocalUserFileRepository,
    LocalUserStore,
    LocalUserUpdate,
    PublicLocalUser,
)
from querygate.identity.passwords import (
    MIN_PASSWORD_LENGTH,
    PasswordError,
    ScryptParams,
    check_password_policy,
    hash_password,
    needs_rehash,
    parse_verifier,
    verify_password,
)
from querygate.identity.totp import new_totp_secret, totp_code, verify_totp

pytestmark = pytest.mark.unit

_PASSWORD = "correct-horse-battery-staple"
# scrypt at production parameters costs ~90ms per derivation; tests that only
# care about the surrounding logic use the cheapest legal cost instead.
_CHEAP = ScryptParams(n=2**12, r=8, p=1)


def _store_with(**overrides) -> LocalUserStore:
    entry = {
        "username": "alice",
        "password_verifier": hash_password(_PASSWORD, _CHEAP),
        "groups": ["admins"],
    }
    entry.update(overrides)
    return LocalUserStore.from_dict({"users": [entry]})


class TestPasswordVerifiers:
    def test_round_trip(self):
        verifier = hash_password(_PASSWORD, _CHEAP)
        assert verify_password(_PASSWORD, verifier)
        assert not verify_password(_PASSWORD + "x", verifier)

    def test_verifier_never_contains_the_password(self):
        assert _PASSWORD not in hash_password(_PASSWORD, _CHEAP)

    def test_salt_makes_two_hashes_of_one_password_differ(self):
        assert hash_password(_PASSWORD, _CHEAP) != hash_password(_PASSWORD, _CHEAP)

    def test_malformed_verifier_fails_closed_rather_than_raising(self):
        for junk in ("", "not-a-verifier", "scrypt$1$2$3", "scrypt$x$8$1$AA==$AA=="):
            assert verify_password(_PASSWORD, junk) is False

    def test_cost_parameters_are_bounded(self):
        with pytest.raises(PasswordError):
            ScryptParams(n=3, r=8, p=1).validate()  # not a power of two
        with pytest.raises(PasswordError):
            ScryptParams(n=2**21, r=8, p=1).validate()  # above MAX_N

    def test_weaker_stored_parameters_are_flagged_for_rehash(self):
        assert needs_rehash(hash_password(_PASSWORD, _CHEAP))
        assert not needs_rehash(hash_password(_PASSWORD))

    def test_verifier_records_its_own_parameters(self):
        params, salt, digest = parse_verifier(hash_password(_PASSWORD, _CHEAP))
        assert (params.n, params.r, params.p) == (2**12, 8, 1)
        assert len(salt) == 16 and len(digest) == 32

    def test_policy_rejects_short_and_username_passwords(self):
        with pytest.raises(PasswordError, match=str(MIN_PASSWORD_LENGTH)):
            check_password_policy("short", username="alice")
        long_username = "alice-the-administrator"
        with pytest.raises(PasswordError, match="username"):
            check_password_policy(long_username.upper(), username=long_username)
        check_password_policy(_PASSWORD, username="alice")


class TestTotp:
    def test_generated_code_verifies(self):
        secret = new_totp_secret()
        counter = int(time.time() // 30)
        result = verify_totp(secret, totp_code(secret, counter))
        assert result.valid and result.consumed_counter == counter

    def test_drift_window_accepts_the_previous_step(self):
        secret = new_totp_secret()
        counter = int(time.time() // 30)
        assert verify_totp(secret, totp_code(secret, counter - 1)).valid

    def test_two_steps_away_is_refused(self):
        secret = new_totp_secret()
        counter = int(time.time() // 30)
        assert not verify_totp(secret, totp_code(secret, counter - 3)).valid

    def test_malformed_codes_are_refused_without_raising(self):
        secret = new_totp_secret()
        for code in ("", "12345", "1234567", "abcdef", "12 34 56"):
            assert not verify_totp(secret, code).valid

    def test_an_already_consumed_counter_is_refused(self):
        secret = new_totp_secret()
        counter = int(time.time() // 30)
        assert not verify_totp(
            secret, totp_code(secret, counter), last_consumed_counter=counter
        ).valid


class TestLocalAuthentication:
    async def test_successful_login(self):
        result = await authenticate_local_user(
            store=_store_with(), username="alice", password=_PASSWORD
        )
        assert result.user.username == "alice" and result.used_mfa is False

    async def test_username_match_is_case_insensitive(self):
        result = await authenticate_local_user(
            store=_store_with(), username="ALICE", password=_PASSWORD
        )
        assert result.user.username == "alice"

    async def test_wrong_password_and_unknown_user_are_indistinguishable(self):
        store = _store_with()
        with pytest.raises(LocalLoginError) as wrong:
            await authenticate_local_user(store=store, username="alice", password="nope")
        with pytest.raises(LocalLoginError) as unknown:
            await authenticate_local_user(store=store, username="nobody", password="nope")
        assert str(wrong.value) == str(unknown.value) == GENERIC_FAILURE_MESSAGE
        assert wrong.value.category == unknown.value.category == "invalid_credentials"

    async def test_an_unknown_user_is_still_hashed_against_a_dummy_verifier(self):
        # The timing-equalizing dummy must be a real verifier no password matches.
        assert verify_password("", dummy_verifier()) is False
        assert dummy_verifier().startswith("scrypt$")

    async def test_disabled_account_cannot_sign_in(self):
        with pytest.raises(LocalLoginError, match=GENERIC_FAILURE_MESSAGE):
            await authenticate_local_user(
                store=_store_with(disabled=True), username="alice", password=_PASSWORD
            )

    async def test_account_with_no_verifier_cannot_sign_in(self):
        with pytest.raises(LocalLoginError):
            await authenticate_local_user(
                store=_store_with(password_verifier=""), username="alice", password=""
            )

    async def test_lockout_after_the_threshold_and_success_does_not_reset_it_early(self):
        store = _store_with()
        for _ in range(2):
            with pytest.raises(LocalLoginError):
                await authenticate_local_user(
                    store=store, username="alice", password="nope", lockout_threshold=3
                )
        with pytest.raises(LockedOutError) as locked:
            await authenticate_local_user(
                store=store, username="alice", password="nope", lockout_threshold=3
            )
        assert locked.value.retry_after_seconds > 0
        # Even the CORRECT password is refused while the account is locked.
        with pytest.raises(LockedOutError):
            await authenticate_local_user(
                store=store, username="alice", password=_PASSWORD, lockout_threshold=3
            )

    async def test_a_success_clears_the_failure_counter(self):
        store = _store_with()
        for _ in range(2):
            with pytest.raises(LocalLoginError):
                await authenticate_local_user(
                    store=store, username="alice", password="nope", lockout_threshold=3
                )
        await authenticate_local_user(store=store, username="alice", password=_PASSWORD)
        # The counter reset, so two more failures still do not lock the account.
        for _ in range(2):
            with pytest.raises(LocalLoginError) as exc:
                await authenticate_local_user(
                    store=store, username="alice", password="nope", lockout_threshold=3
                )
            assert not isinstance(exc.value, LockedOutError)

    async def test_enrolled_account_requires_a_code(self):
        store = _store_with(totp_secret=new_totp_secret())
        with pytest.raises(LocalLoginError) as exc:
            await authenticate_local_user(store=store, username="alice", password=_PASSWORD)
        assert exc.value.category == "mfa_required"

    async def test_enrolled_account_accepts_a_valid_code_once(self):
        secret = new_totp_secret()
        store = _store_with(totp_secret=secret)
        code = totp_code(secret, int(time.time() // 30))
        result = await authenticate_local_user(
            store=store, username="alice", password=_PASSWORD, totp_code=code
        )
        assert result.used_mfa is True
        with pytest.raises(LocalLoginError) as replay:
            await authenticate_local_user(
                store=store, username="alice", password=_PASSWORD, totp_code=code
            )
        assert replay.value.category == "replayed_mfa_code"

    async def test_require_mfa_refuses_an_unenrolled_account(self):
        with pytest.raises(LocalLoginError) as exc:
            await authenticate_local_user(
                store=_store_with(), username="alice", password=_PASSWORD, require_mfa=True
            )
        assert exc.value.category == "mfa_not_enrolled"


class TestUserStore:
    def test_public_view_has_no_field_for_a_verifier_or_totp_secret(self):
        assert "password_verifier" not in PublicLocalUser.model_fields
        assert "totp_secret" not in PublicLocalUser.model_fields

    def test_public_view_reports_mfa_without_exposing_the_secret(self):
        user = LocalUser(username="alice", totp_secret=new_totp_secret())
        public = user.to_public()
        assert public.mfa_enabled is True
        assert user.totp_secret not in public.model_dump_json()

    def test_claims_are_shaped_like_an_oidc_payload(self):
        claims = LocalUser(username="alice", groups=["admins"]).claims()
        assert claims["sub"] == "alice" and claims["groups"] == ["admins"]
        assert claims["amr"] == ["pwd"]

    def test_duplicate_usernames_are_refused_case_insensitively(self):
        with pytest.raises(ValueError, match="Duplicate local username"):
            LocalUserStore.from_dict({"users": [{"username": "alice"}, {"username": "ALICE"}]})

    def test_unsafe_usernames_and_groups_are_refused(self):
        with pytest.raises(ValueError):
            LocalUser(username="../../etc/passwd")
        with pytest.raises(ValueError):
            LocalUser(username="alice", groups=["a\nb"])


class TestUserFileRepository:
    def test_write_is_atomic_and_the_file_stays_owner_only(self, tmp_path: Path):
        path = tmp_path / "users.yaml"
        repo = LocalUserFileRepository(str(path))
        user = LocalUser(username="alice", password_verifier=hash_password(_PASSWORD, _CHEAP))
        repo.update(lambda store: LocalUserUpdate(store.with_user(user), user.username))

        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        written = yaml.safe_load(path.read_text())
        assert [u["username"] for u in written["users"]] == ["alice"]
        assert not list(tmp_path.glob(".users.yaml.*.tmp"))

    def test_the_file_never_contains_the_plaintext_password(self, tmp_path: Path):
        path = tmp_path / "users.yaml"
        repo = LocalUserFileRepository(str(path))
        user = LocalUser(username="alice", password_verifier=hash_password(_PASSWORD, _CHEAP))
        repo.update(lambda store: LocalUserUpdate(store.with_user(user), None))
        assert _PASSWORD not in path.read_text()

    def test_the_update_re_reads_under_the_lock(self, tmp_path: Path):
        """A read-modify-write must see a concurrent writer's change.

        The repository loads the file *inside* the lock, so an update built from
        a stale in-memory store cannot clobber an account added since.
        """
        path = tmp_path / "users.yaml"
        repo = LocalUserFileRepository(str(path))
        first = LocalUser(username="alice")
        repo.update(lambda store: LocalUserUpdate(store.with_user(first), None))
        # Simulate another process having written the file after our snapshot.
        path.write_text(yaml.safe_dump({"users": [{"username": "bob"}]}))

        second = LocalUser(username="carol")
        repo.update(lambda store: LocalUserUpdate(store.with_user(second), None))
        written = {u["username"] for u in yaml.safe_load(path.read_text())["users"]}
        assert written == {"bob", "carol"}

    def test_deleting_an_account_removes_it_from_the_file(self, tmp_path: Path):
        path = tmp_path / "users.yaml"
        repo = LocalUserFileRepository(str(path))
        user = LocalUser(username="alice")
        repo.update(lambda store: LocalUserUpdate(store.with_user(user), None))
        repo.update(lambda store: LocalUserUpdate(store.without_user("ALICE"), None))
        assert yaml.safe_load(path.read_text())["users"] == []
