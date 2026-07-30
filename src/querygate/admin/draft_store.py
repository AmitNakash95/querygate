"""Server-side encrypted-at-rest config draft store (TODO.md item 47, phase 2).

Phase 1 shipped a portable *change-set bundle* (`ConfigChangeSetBundle`,
`admin/models.py`) that an admin downloads/uploads to move a reviewed change
between environments or recover a lost draft. This module adds the heavier
alternative that item's own scope named alongside the file: a server-side
store for the exact same bundle, so recovery survives a lost download and
works across devices without the admin's browser ever holding the file.

Layout under `AppConfig.draft_store_dir`:

    <draft_id>/manifest.json   — plaintext DraftSummary fields (id, principal_id,
                                 description, created_at, expires_at,
                                 contains_connections) — never the bundle content
    <draft_id>/bundle.enc      — the bundle's JSON, Fernet-encrypted

**Why the manifest stays plaintext but the bundle is encrypted.** A bundle may
legitimately carry a `connections` document with a literal credential (the
same reason phase 1 never writes it to browser `localStorage`) — that content
is the only thing this store needs to protect. The manifest carries nothing
more sensitive than `ConfigVersion`'s own manifest already does, so listing a
caller's own drafts never requires decrypting anything.

**No new config-mutation path.** Loading a draft returns the same
`ConfigChangeSetBundle` the phase-1 download does — the caller still imports
it through the unchanged validate/stage/apply flow. This store only persists
and retrieves that bundle; it has no path to becoming an active version on
its own.

**Ownership, not just authentication.** Every read/delete is scoped to the
principal that saved the draft — a `NotFoundError` covers "doesn't exist",
"expired", and "exists but isn't yours" uniformly, so this can never become a
draft-id enumeration or existence oracle across principals (the same posture
item 48's curated-template lookup and item 45's `/help/my-recent-denials`
already established).

**Ownership isolation is only as fine-grained as the deployment's identity
model — surfaced by `auditors` review, not a defect here.** This module
correctly isolates by `principal.subject`, matching item 45's pattern exactly.
Under JWT auth, that is a real per-caller identity (each token's own `sub`).
Under this codebase's static `AppConfig.api_keys`, it is NOT: every configured
key authenticates to the same single `AppConfig.api_key_subject`, so two
different admins each holding their own API key see and can delete each
other's saved drafts — proven deliberately (not just left untested) by
`test_static_api_keys_share_one_identity_so_drafts_are_not_isolated_between_them`
in `tests/integration/test_draft_store_api.py`. A deployment that needs
per-admin draft isolation must use JWT auth; this is a property of the
identity layer, not something a draft-store change can fix on its own.

**Retention, not a cron job.** Every mutating/listing call opportunistically
prunes expired drafts before doing its own work — bounded, low-volume admin
usage never needs a background sweeper. `max_drafts_per_principal` bounds
storage per admin deterministically; a caller over the cap must delete an old
draft before saving a new one, rather than one silently evicting another.

**No process-global state — but the lock is still module-level, not
per-instance.** Unlike `ConfigVersionStore`, `DraftStore` holds no in-memory
index — every operation reads/writes the filesystem directly, so
`get_draft_store(cfg)` can construct a fresh instance per call with no
staleness risk and nothing for a test-reset fixture to track. This is only
safe because `_LOCK` (below) is a *module-level* lock every instance shares,
mirroring how `ConfigVersionStore`'s singleton exists specifically so every
caller shares one lock *instance* — not because it caches anything either.
Moving `_LOCK` onto `self` to "match" would silently drop mutual exclusion
across the fresh-instance-per-call construction and reintroduce a torn-write
race the current single-request test suite would not catch.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from cryptography.fernet import Fernet, InvalidToken

from querygate.admin.models import ConfigChangeSetBundle, DraftSummary
from querygate.core.config import AppConfig
from querygate.core.exceptions import ConfigValidationError, NotFoundError

_MANIFEST_NAME = "manifest.json"
_BUNDLE_NAME = "bundle.enc"

# One process-wide lock is enough: draft operations are admin-triggered and
# low-frequency, and correctness (no torn writes, no double-prune races)
# matters far more than concurrency here — the same tradeoff
# `ConfigVersionStore` already makes.
_LOCK = threading.Lock()


def _derive_fernet(encryption_key: str) -> Fernet:
    """Any operator-supplied secret string becomes a valid Fernet key via a
    fixed SHA-256 derivation — matching this codebase's existing posture of
    plain-string secrets (`audit_ledger_hmac_key`, `approval_token_hmac_key`)
    rather than requiring a pre-formatted base64 key from the operator."""
    digest = hashlib.sha256(encryption_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_bytes(content)
    tmp_path.replace(path)


class DraftStore:
    def __init__(self, root: str, encryption_key: str) -> None:
        self._root = Path(root)
        self._fernet = _derive_fernet(encryption_key)

    def _draft_dir(self, draft_id: str) -> Path:
        return self._root / draft_id

    def _manifest_path(self, draft_id: str) -> Path:
        return self._draft_dir(draft_id) / _MANIFEST_NAME

    def _bundle_path(self, draft_id: str) -> Path:
        return self._draft_dir(draft_id) / _BUNDLE_NAME

    def _all_draft_ids(self) -> List[str]:
        if not self._root.exists():
            return []
        return [
            child.name
            for child in self._root.iterdir()
            if child.is_dir() and (child / _MANIFEST_NAME).exists()
        ]

    _REQUIRED_MANIFEST_KEYS = frozenset(
        {"id", "principal_id", "created_at", "expires_at", "contains_connections"}
    )

    def _read_manifest_raw(self, draft_id: str) -> Optional[dict]:
        """Best-effort read: a missing, corrupted, or partially-written
        manifest (a crash between the two atomic writes `save()` makes, or
        manual tampering) is treated as "not found" rather than raising —
        the same tolerance `ConfigVersionStore`/`JsonlAuditEventSource`
        already apply to malformed persisted state."""
        path = self._manifest_path(draft_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(raw, dict) or not self._REQUIRED_MANIFEST_KEYS.issubset(raw):
            return None
        return raw

    def _prune_expired(self, *, now: datetime) -> None:
        for draft_id in self._all_draft_ids():
            raw = self._read_manifest_raw(draft_id)
            if raw is None:
                continue
            expires_at = datetime.fromisoformat(raw["expires_at"])
            if expires_at <= now:
                shutil.rmtree(self._draft_dir(draft_id), ignore_errors=True)

    def _owned_manifest(self, draft_id: str, *, principal_id: str, now: datetime) -> dict:
        """Return the raw manifest only if it exists, hasn't expired, and
        belongs to `principal_id` — otherwise NotFoundError, uniformly, so a
        caller can never distinguish "not yours" from "doesn't exist"."""
        raw = self._read_manifest_raw(draft_id)
        if raw is None:
            raise NotFoundError(f"Unknown draft: {draft_id!r}")
        expires_at = datetime.fromisoformat(raw["expires_at"])
        if expires_at <= now or raw["principal_id"] != principal_id:
            raise NotFoundError(f"Unknown draft: {draft_id!r}")
        return raw

    def _summary(self, raw: dict) -> DraftSummary:
        return DraftSummary(
            id=raw["id"],
            description=raw.get("description"),
            created_at=datetime.fromisoformat(raw["created_at"]),
            expires_at=datetime.fromisoformat(raw["expires_at"]),
            contains_connections=raw["contains_connections"],
        )

    def save(
        self,
        *,
        principal_id: str,
        bundle: ConfigChangeSetBundle,
        description: Optional[str],
        retention_seconds: float,
        max_drafts: int,
        now: Optional[datetime] = None,
    ) -> DraftSummary:
        now = now or datetime.now(timezone.utc)
        with _LOCK:
            self._prune_expired(now=now)
            existing = sum(
                1
                for draft_id in self._all_draft_ids()
                if (raw := self._read_manifest_raw(draft_id)) is not None
                and raw["principal_id"] == principal_id
            )
            if existing >= max_drafts:
                raise ConfigValidationError(
                    f"you already have {existing} saved drafts (the limit is "
                    f"{max_drafts}) — delete one before saving another"
                )

            draft_id = uuid.uuid4().hex
            manifest = {
                "id": draft_id,
                "principal_id": principal_id,
                "description": description,
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=retention_seconds)).isoformat(),
                "contains_connections": bundle.contains_connections,
            }
            encrypted = self._fernet.encrypt(bundle.model_dump_json().encode("utf-8"))
            # Manifest first: if a crash lands between these two writes, the
            # orphan is a manifest with no bundle file — still visible to
            # `_all_draft_ids()` and self-healing (it expires and gets pruned
            # normally). The other order would leave an invisible bundle.enc
            # with no manifest, which `_all_draft_ids()` can never see and
            # `_prune_expired` can therefore never clean up.
            _atomic_write_bytes(
                self._manifest_path(draft_id), json.dumps(manifest, indent=2).encode("utf-8")
            )
            _atomic_write_bytes(self._bundle_path(draft_id), encrypted)
            return self._summary(manifest)

    def list_for_principal(
        self, principal_id: str, *, now: Optional[datetime] = None
    ) -> List[DraftSummary]:
        now = now or datetime.now(timezone.utc)
        with _LOCK:
            self._prune_expired(now=now)
            summaries = []
            for draft_id in self._all_draft_ids():
                raw = self._read_manifest_raw(draft_id)
                if raw is not None and raw["principal_id"] == principal_id:
                    summaries.append(self._summary(raw))
            summaries.sort(key=lambda s: s.created_at, reverse=True)
            return summaries

    def load(
        self, draft_id: str, *, principal_id: str, now: Optional[datetime] = None
    ) -> ConfigChangeSetBundle:
        now = now or datetime.now(timezone.utc)
        with _LOCK:
            self._owned_manifest(draft_id, principal_id=principal_id, now=now)
            try:
                encrypted = self._bundle_path(draft_id).read_bytes()
            except OSError as exc:
                # A manifest with no bundle file: only reachable via a crash
                # between the two writes in save() (manifest is written
                # first, precisely so this case is possible instead of a
                # permanently invisible orphan — see save()'s comment).
                raise NotFoundError(f"Unknown draft: {draft_id!r}") from exc
            try:
                decrypted = self._fernet.decrypt(encrypted)
            except InvalidToken as exc:
                # Only reachable if the key changed since the draft was saved,
                # or the file was corrupted — never a caller-controlled path.
                raise NotFoundError(f"Unknown draft: {draft_id!r}") from exc
            return ConfigChangeSetBundle.model_validate_json(decrypted)

    def delete(self, draft_id: str, *, principal_id: str, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        with _LOCK:
            self._owned_manifest(draft_id, principal_id=principal_id, now=now)
            shutil.rmtree(self._draft_dir(draft_id), ignore_errors=True)


def get_draft_store(cfg: AppConfig) -> Optional[DraftStore]:
    """`None` means the subsystem is disabled (no encryption key configured) —
    the route layer reports this honestly as a 503 rather than ever writing
    plaintext. No process-global instance: construction is cheap (no I/O), and
    every operation reads/writes `cfg.draft_store_dir` directly, so a fresh
    instance per call has no staleness risk."""
    if not cfg.draft_store_encryption_key:
        return None
    return DraftStore(cfg.draft_store_dir, cfg.draft_store_encryption_key)
