"""The built-in local identity provider's user file, and its one writer.

`users.yaml` is QueryGate's own identity store: the fallback for a deployment
that has no external IdP at all — air-gapped installs, a break-glass account
that still works when the IdP is down, a design partner who wants to evaluate
before wiring SSO. It is deliberately **not** a general user-management
product; it holds exactly what is needed to authenticate a person and hand
their group memberships to `IdentityMappingStore`.

Two rules make it safe to have at all:

* **One mutation path.** Every write goes through `LocalUserFileRepository`,
  which takes an adjacent file lock, re-reads the file under that lock, writes
  atomically (temp file + fsync + `os.replace`), and swaps the process-wide
  store — the same discipline `catalog/repository.py` uses, for the same
  reason. There is no second users file, no shadow copy in the config-version
  store, and no in-place edit.
* **The file only ever holds verifiers.** `password_verifier` is an scrypt
  output (`identity/passwords.py`) and there is no field anywhere that can hold
  a plaintext password. `PublicLocalUser` — the only shape REST returns —
  carries neither the verifier nor the TOTP secret, structurally.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TextIO, TypeVar

import pydantic as pyd
import yaml

# Usernames are compared case-insensitively (people type them by hand) but
# stored as written. The character set excludes anything that could be read as
# a path segment or a claim-path separator.
_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$")
_GROUP_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._@:/-]{0,127}$")
MAX_GROUPS_PER_USER = 256
MAX_USERS = 10000

T = TypeVar("T")


class LocalUser(pyd.BaseModel):
    """One local account. **Holds the password verifier and TOTP secret.**"""

    username: str
    display_name: str = ""
    email: str = ""
    groups: List[str] = pyd.Field(default_factory=list)
    password_verifier: str = ""
    totp_secret: str = ""
    disabled: bool = False
    must_change_password: bool = False
    created_at: Optional[datetime] = None
    password_updated_at: Optional[datetime] = None

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.field_validator("username")
    @classmethod
    def _check_username(cls, value: str) -> str:
        if not _USERNAME_PATTERN.match(value):
            raise ValueError(
                "Local username must be 1-128 chars of letters, digits, and . _ @ + - ; "
                f"got {value!r}."
            )
        return value

    @pyd.field_validator("groups")
    @classmethod
    def _check_groups(cls, value: List[str]) -> List[str]:
        if len(value) > MAX_GROUPS_PER_USER:
            raise ValueError(f"A local user may have at most {MAX_GROUPS_PER_USER} groups.")
        for group in value:
            if not _GROUP_PATTERN.match(group):
                raise ValueError(f"Invalid local group name: {group!r}")
        return value

    @property
    def key(self) -> str:
        return self.username.lower()

    @property
    def mfa_enabled(self) -> bool:
        return bool(self.totp_secret)

    def claims(self) -> Dict[str, Any]:
        """The claim set the local provider asserts about this person.

        Shaped like an OIDC ID token's payload on purpose: `IdentityMappingStore`
        then maps a local `groups` entry exactly the way it maps an Entra group
        GUID, with no local-provider special case anywhere in the mapping layer.
        """
        return {
            "sub": self.username,
            "preferred_username": self.username,
            "name": self.display_name or self.username,
            "email": self.email,
            "groups": list(self.groups),
            "amr": ["pwd", "otp"] if self.mfa_enabled else ["pwd"],
        }

    def to_public(self) -> "PublicLocalUser":
        return PublicLocalUser(
            username=self.username,
            display_name=self.display_name,
            email=self.email,
            groups=list(self.groups),
            disabled=self.disabled,
            mfa_enabled=self.mfa_enabled,
            must_change_password=self.must_change_password,
            created_at=self.created_at,
            password_updated_at=self.password_updated_at,
        )


class PublicLocalUser(pyd.BaseModel):
    """The only local-account shape that leaves REST.

    No `password_verifier` and no `totp_secret` field exists on this model, so
    neither can be leaked by forgetting to exclude it — the same structural
    split `PublicConnectionInfo` uses for connection strings.
    """

    username: str
    display_name: str = ""
    email: str = ""
    groups: List[str] = pyd.Field(default_factory=list)
    disabled: bool = False
    mfa_enabled: bool = False
    must_change_password: bool = False
    created_at: Optional[datetime] = None
    password_updated_at: Optional[datetime] = None

    model_config = pyd.ConfigDict(extra="forbid")


class LocalUserStore:
    """In-memory view of users.yaml."""

    def __init__(self, users: Dict[str, LocalUser]) -> None:
        if len(users) > MAX_USERS:
            raise ValueError(f"users file holds more than {MAX_USERS} accounts.")
        self._users = users

    @classmethod
    def empty(cls) -> "LocalUserStore":
        return cls({})

    @classmethod
    def from_file(cls, path: str) -> "LocalUserStore":
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"Local users file not found: {path}")
        return cls.from_dict(yaml.safe_load(file_path.read_text()) or {})

    @classmethod
    def from_dict(cls, raw: dict) -> "LocalUserStore":
        users: Dict[str, LocalUser] = {}
        for entry in raw.get("users") or []:
            user = LocalUser.model_validate(entry or {})
            if user.key in users:
                raise ValueError(f"Duplicate local username (case-insensitive): {user.username!r}")
            users[user.key] = user
        return cls(users)

    def to_dict(self) -> dict:
        return {
            "users": [
                user.model_dump(mode="json", exclude_none=True)
                for user in sorted(self._users.values(), key=lambda u: u.key)
            ]
        }

    def get(self, username: str) -> Optional[LocalUser]:
        return self._users.get((username or "").strip().lower())

    def list_public(self) -> List[PublicLocalUser]:
        return [user.to_public() for user in sorted(self._users.values(), key=lambda u: u.key)]

    def all_usernames(self) -> List[str]:
        return sorted(user.username for user in self._users.values())

    def with_user(self, user: LocalUser) -> "LocalUserStore":
        updated = dict(self._users)
        updated[user.key] = user
        return LocalUserStore(updated)

    def without_user(self, username: str) -> "LocalUserStore":
        updated = dict(self._users)
        updated.pop((username or "").strip().lower(), None)
        return LocalUserStore(updated)

    def __len__(self) -> int:
        return len(self._users)


class LocalUserFileRepository:
    """The single mutation path for users.yaml.

    Mirrors `catalog/repository.CatalogFileRepository`: an adjacent `.lock`
    file serializes concurrent writers (including a second QueryGate process
    sharing the volume), the file is re-read *inside* the lock so a
    read-modify-write can never clobber a concurrent change, and the replace is
    atomic and fsynced. The file is created 0600 and its mode preserved on
    every write, so a password verifier never widens its permissions.
    """

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def update(self, updater: Callable[[LocalUserStore], "LocalUserUpdate[T]"]) -> T:
        lock_file = self._acquire_lock()
        try:
            current = (
                LocalUserStore.from_file(str(self.path))
                if self.path.exists()
                else LocalUserStore.empty()
            )
            update = updater(current)
            if update.store.to_dict() != current.to_dict():
                self._write_atomic(update.store)
            set_local_user_store(update.store)
            return update.result
        finally:
            lock_file.close()

    def _acquire_lock(self) -> TextIO:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            lock_file = os.fdopen(lock_fd, "r+")
            self._lock(lock_file)
            return lock_file
        except Exception:
            try:
                os.close(lock_fd)
            except OSError:
                pass
            raise

    @staticmethod
    def _lock(lock_file: TextIO) -> None:
        if os.name == "nt":  # pragma: no cover - platform specific
            import msvcrt

            if os.fstat(lock_file.fileno()).st_size == 0:
                lock_file.write("0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            return
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

    def _write_atomic(self, store: LocalUserStore) -> None:
        payload = yaml.safe_dump(
            store.to_dict(), sort_keys=False, allow_unicode=True, default_flow_style=False
        )
        mode = stat.S_IMODE(self.path.stat().st_mode) if self.path.exists() else 0o600
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_fd, temp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp"
        )
        try:
            os.fchmod(temp_fd, mode)
            with os.fdopen(temp_fd, "w", encoding="utf-8") as temp_file:
                temp_file.write(payload)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_name, self.path)
            if os.name != "nt":
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise


class LocalUserUpdate:
    """The (new store, caller result) pair a repository updater returns."""

    def __init__(self, store: LocalUserStore, result: Any = None) -> None:
        self.store = store
        self.result = result


_local_store: Optional[LocalUserStore] = None


def get_local_user_store() -> LocalUserStore:
    global _local_store
    if _local_store is None:
        from querygate.core.config import config

        if not config.local_idp_enabled:
            _local_store = LocalUserStore.empty()
        elif Path(config.local_users_file).exists():
            _local_store = LocalUserStore.from_file(config.local_users_file)
        else:
            _local_store = LocalUserStore.empty()
    return _local_store


def set_local_user_store(store: LocalUserStore) -> None:
    global _local_store
    _local_store = store


def clear_local_user_store() -> None:
    global _local_store
    _local_store = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
