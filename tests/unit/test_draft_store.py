"""Unit tests for the server-side encrypted-at-rest draft store (TODO.md item
47, phase 2): `admin/draft_store.py`'s `DraftStore` directly, and the
audit-wrapped service functions in `admin/service.py`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from querygate.admin import service as governance
from querygate.admin.draft_store import DraftStore, get_draft_store
from querygate.admin.models import ConfigChangeSetBundle
from querygate.audit.sinks import reset_audit_sink
from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.exceptions import ConfigValidationError, NotFoundError, ServiceDisabledError

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolated_sink():
    reset_audit_sink()
    yield
    reset_audit_sink()


def _bundle(*, connections: bool = False, description="a draft") -> ConfigChangeSetBundle:
    documents = {"policy": "default:\n  enabled: true\n"}
    if connections:
        documents["connections"] = "connections: []\n"
    return ConfigChangeSetBundle(
        base_version_id="v1",
        base_fingerprint="abc123",
        created_at=_NOW,
        description=description,
        documents=documents,
    )


def _store(tmp_path, key="secret-key") -> DraftStore:
    return DraftStore(str(tmp_path / "drafts"), key)


# --- DraftStore: round trip / encryption ------------------------------------


def test_save_and_load_round_trip(tmp_path):
    store = _store(tmp_path)
    summary = store.save(
        principal_id="user-a",
        bundle=_bundle(),
        description="a draft",
        retention_seconds=3600.0,
        max_drafts=10,
        now=_NOW,
    )
    loaded = store.load(summary.id, principal_id="user-a", now=_NOW)
    assert loaded.documents == _bundle().documents
    assert loaded.description == "a draft"


def test_save_reports_contains_connections(tmp_path):
    store = _store(tmp_path)
    summary = store.save(
        principal_id="user-a",
        bundle=_bundle(connections=True),
        description=None,
        retention_seconds=3600.0,
        max_drafts=10,
        now=_NOW,
    )
    assert summary.contains_connections is True


def test_bundle_content_is_not_stored_as_plaintext_on_disk(tmp_path):
    store = _store(tmp_path)
    summary = store.save(
        principal_id="user-a",
        bundle=_bundle(description="super secret rollout plan"),
        description="super secret rollout plan",
        retention_seconds=3600.0,
        max_drafts=10,
        now=_NOW,
    )
    raw = (tmp_path / "drafts" / summary.id / "bundle.enc").read_bytes()
    assert b"super secret rollout plan" not in raw
    assert b"policy" not in raw
    assert b"enabled" not in raw


def test_manifest_carries_only_metadata_never_documents(tmp_path):
    store = _store(tmp_path)
    summary = store.save(
        principal_id="user-a",
        bundle=_bundle(),
        description="a draft",
        retention_seconds=3600.0,
        max_drafts=10,
        now=_NOW,
    )
    manifest_text = (tmp_path / "drafts" / summary.id / "manifest.json").read_text()
    assert "documents" not in manifest_text
    assert "enabled" not in manifest_text


def test_manifest_missing_a_required_key_is_treated_as_not_found(tmp_path):
    """Regression: a manifest that's valid JSON but missing a required key
    (a partial write, or manual tampering) must fail closed as "not found"
    rather than raising an unhandled KeyError from a bare `raw["..."]` index."""
    store = _store(tmp_path)
    summary = store.save(
        principal_id="user-a",
        bundle=_bundle(),
        description="mine",
        retention_seconds=3600.0,
        max_drafts=10,
        now=_NOW,
    )
    manifest_path = tmp_path / "drafts" / summary.id / "manifest.json"
    raw = json.loads(manifest_path.read_text())
    del raw["expires_at"]
    manifest_path.write_text(json.dumps(raw))

    assert store.list_for_principal("user-a", now=_NOW) == []
    with pytest.raises(NotFoundError):
        store.load(summary.id, principal_id="user-a", now=_NOW)


# --- Cross-principal isolation (the security-critical property) ------------


def test_list_for_principal_only_returns_own_drafts(tmp_path):
    store = _store(tmp_path)
    store.save(
        principal_id="user-a",
        bundle=_bundle(),
        description="mine",
        retention_seconds=3600.0,
        max_drafts=10,
        now=_NOW,
    )
    store.save(
        principal_id="user-b",
        bundle=_bundle(),
        description="not mine",
        retention_seconds=3600.0,
        max_drafts=10,
        now=_NOW,
    )
    mine = store.list_for_principal("user-a", now=_NOW)
    assert len(mine) == 1
    assert mine[0].description == "mine"


def test_load_raises_not_found_for_another_principals_draft(tmp_path):
    store = _store(tmp_path)
    summary = store.save(
        principal_id="user-a",
        bundle=_bundle(),
        description="mine",
        retention_seconds=3600.0,
        max_drafts=10,
        now=_NOW,
    )
    with pytest.raises(NotFoundError):
        store.load(summary.id, principal_id="user-b", now=_NOW)


def test_load_raises_not_found_when_bundle_file_is_missing(tmp_path):
    """Regression: a manifest with no bundle file (only reachable via a crash
    between save()'s two writes) must fail closed as NotFoundError, not crash
    with an unhandled OSError."""
    store = _store(tmp_path)
    summary = store.save(
        principal_id="user-a",
        bundle=_bundle(),
        description="mine",
        retention_seconds=3600.0,
        max_drafts=10,
        now=_NOW,
    )
    (tmp_path / "drafts" / summary.id / "bundle.enc").unlink()
    with pytest.raises(NotFoundError):
        store.load(summary.id, principal_id="user-a", now=_NOW)


def test_delete_raises_not_found_for_another_principals_draft(tmp_path):
    store = _store(tmp_path)
    summary = store.save(
        principal_id="user-a",
        bundle=_bundle(),
        description="mine",
        retention_seconds=3600.0,
        max_drafts=10,
        now=_NOW,
    )
    with pytest.raises(NotFoundError):
        store.delete(summary.id, principal_id="user-b", now=_NOW)
    # And it must still be there, untouched, for its real owner.
    assert store.load(summary.id, principal_id="user-a", now=_NOW) is not None


def test_load_raises_not_found_for_unknown_draft_id(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(NotFoundError):
        store.load("does-not-exist", principal_id="user-a", now=_NOW)


# --- Deletion ----------------------------------------------------------------


def test_delete_removes_the_draft(tmp_path):
    store = _store(tmp_path)
    summary = store.save(
        principal_id="user-a",
        bundle=_bundle(),
        description="mine",
        retention_seconds=3600.0,
        max_drafts=10,
        now=_NOW,
    )
    store.delete(summary.id, principal_id="user-a", now=_NOW)
    assert store.list_for_principal("user-a", now=_NOW) == []
    with pytest.raises(NotFoundError):
        store.load(summary.id, principal_id="user-a", now=_NOW)


# --- Retention / expiry -------------------------------------------------------


def test_expired_draft_is_not_listed_or_loadable(tmp_path):
    store = _store(tmp_path)
    summary = store.save(
        principal_id="user-a",
        bundle=_bundle(),
        description="mine",
        retention_seconds=1.0,
        max_drafts=10,
        now=_NOW,
    )
    later = _NOW + timedelta(seconds=2)
    assert store.list_for_principal("user-a", now=later) == []
    with pytest.raises(NotFoundError):
        store.load(summary.id, principal_id="user-a", now=later)


def test_expired_draft_is_pruned_from_disk_on_next_operation(tmp_path):
    store = _store(tmp_path)
    summary = store.save(
        principal_id="user-a",
        bundle=_bundle(),
        description="mine",
        retention_seconds=1.0,
        max_drafts=10,
        now=_NOW,
    )
    later = _NOW + timedelta(seconds=2)
    store.list_for_principal("user-a", now=later)  # triggers prune
    assert not (tmp_path / "drafts" / summary.id).exists()


def test_non_expired_draft_survives_a_prune_pass(tmp_path):
    store = _store(tmp_path)
    summary = store.save(
        principal_id="user-a",
        bundle=_bundle(),
        description="mine",
        retention_seconds=3600.0,
        max_drafts=10,
        now=_NOW,
    )
    soon = _NOW + timedelta(seconds=5)
    assert len(store.list_for_principal("user-a", now=soon)) == 1
    assert store.load(summary.id, principal_id="user-a", now=soon) is not None


# --- Per-principal cap --------------------------------------------------------


def test_max_drafts_per_principal_cap_rejects_extra_save(tmp_path):
    store = _store(tmp_path)
    for _ in range(3):
        store.save(
            principal_id="user-a",
            bundle=_bundle(),
            description="mine",
            retention_seconds=3600.0,
            max_drafts=3,
            now=_NOW,
        )
    with pytest.raises(ConfigValidationError):
        store.save(
            principal_id="user-a",
            bundle=_bundle(),
            description="one too many",
            retention_seconds=3600.0,
            max_drafts=3,
            now=_NOW,
        )


def test_cap_is_per_principal_not_global(tmp_path):
    store = _store(tmp_path)
    for _ in range(3):
        store.save(
            principal_id="user-a",
            bundle=_bundle(),
            description="mine",
            retention_seconds=3600.0,
            max_drafts=3,
            now=_NOW,
        )
    # A different principal is unaffected by user-a's cap.
    summary = store.save(
        principal_id="user-b",
        bundle=_bundle(),
        description="also mine",
        retention_seconds=3600.0,
        max_drafts=3,
        now=_NOW,
    )
    assert summary is not None


# --- Encryption key handling ---------------------------------------------------


def test_wrong_encryption_key_cannot_decrypt_another_stores_draft(tmp_path):
    store_a = _store(tmp_path, key="key-one")
    summary = store_a.save(
        principal_id="user-a",
        bundle=_bundle(),
        description="mine",
        retention_seconds=3600.0,
        max_drafts=10,
        now=_NOW,
    )
    store_b = DraftStore(str(tmp_path / "drafts"), "key-two")
    with pytest.raises(NotFoundError):
        store_b.load(summary.id, principal_id="user-a", now=_NOW)


def test_get_draft_store_returns_none_when_key_is_empty():
    cfg = AppConfig(environment="localhost", draft_store_encryption_key="")
    assert get_draft_store(cfg) is None


def test_get_draft_store_returns_instance_when_key_is_set(tmp_path):
    cfg = AppConfig(
        environment="localhost",
        draft_store_encryption_key="a-real-secret",
        draft_store_dir=str(tmp_path / "drafts"),
    )
    store = get_draft_store(cfg)
    assert isinstance(store, DraftStore)


# --- Service layer (admin/service.py) ----------------------------------------


def _writer() -> Principal:
    return Principal(subject="admin-a", scopes=frozenset({"admin:config:write"}))


def _cfg(tmp_path, **overrides) -> AppConfig:
    defaults = dict(
        environment="localhost",
        api_keys=["k"],
        draft_store_encryption_key="a-real-secret",
        draft_store_dir=str(tmp_path / "drafts"),
    )
    defaults.update(overrides)
    return AppConfig(**defaults)


def test_service_save_disabled_raises_service_disabled_error(tmp_path):
    cfg = _cfg(tmp_path, draft_store_encryption_key="")
    with pytest.raises(ServiceDisabledError):
        governance.save_draft(cfg, _writer(), _bundle())


def test_service_load_disabled_raises_service_disabled_error(tmp_path):
    cfg = _cfg(tmp_path, draft_store_encryption_key="")
    with pytest.raises(ServiceDisabledError):
        governance.load_draft(cfg, _writer(), "any-id")


def test_service_save_then_load_round_trip(tmp_path):
    cfg = _cfg(tmp_path)
    summary = governance.save_draft(cfg, _writer(), _bundle())
    loaded = governance.load_draft(cfg, _writer(), summary.id)
    assert loaded.documents == _bundle().documents


def test_service_list_only_shows_the_callers_own(tmp_path):
    cfg = _cfg(tmp_path)
    other = Principal(subject="admin-b", scopes=frozenset({"admin:config:write"}))
    governance.save_draft(cfg, _writer(), _bundle(description="a-owns-this"))
    governance.save_draft(cfg, other, _bundle(description="b-owns-this"))
    mine = governance.list_my_drafts(cfg, _writer())
    assert [d.description for d in mine] == ["a-owns-this"]


def test_service_delete_then_load_raises_not_found(tmp_path):
    cfg = _cfg(tmp_path)
    summary = governance.save_draft(cfg, _writer(), _bundle())
    governance.delete_draft(cfg, _writer(), summary.id)
    with pytest.raises(NotFoundError):
        governance.load_draft(cfg, _writer(), summary.id)


def test_service_max_drafts_cap_surfaces_as_config_validation_error(tmp_path):
    cfg = _cfg(tmp_path, draft_store_max_drafts_per_principal=1)
    governance.save_draft(cfg, _writer(), _bundle())
    with pytest.raises(ConfigValidationError):
        governance.save_draft(cfg, _writer(), _bundle())
