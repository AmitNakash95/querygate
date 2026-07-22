"""Unit tests for config change-set bundle export/import (item 47).

Covers the service layer directly (querygate/admin/service.py): the bundle
carries only submitted deltas, stamps a base fingerprint, and import detects
stale bases, rejects invalid/oversized content, and stays a validation-only
step that never persists or stages.
"""

from __future__ import annotations

import pytest

from querygate.admin import service as governance
from querygate.admin.models import ConfigChangeSetBundle
from querygate.admin.store import (
    ConfigVersionStore,
    get_config_version_store,
    set_config_version_store,
)
from querygate.audit.sinks import reset_audit_sink
from querygate.core.auth import Principal
from querygate.core.config import AppConfig

_VALID_CONNECTIONS = """
connections:
  - id: fresh
    dialect: postgresql
    connection_string: ${TEST_ADMIN_URL}
"""
_VALID_POLICY = "default:\n  enabled: true\n"
_VALID_POLICY_V2 = "default:\n  enabled: true\n  max_joins: 3\n"


@pytest.fixture(autouse=True)
def _isolated_sink():
    reset_audit_sink()
    yield
    reset_audit_sink()


def _cfg(tmp_path, monkeypatch, **overrides) -> AppConfig:
    monkeypatch.setenv("TEST_ADMIN_URL", "postgresql+asyncpg://user:pass@localhost/x")
    connections_file = tmp_path / "connections.yaml"
    connections_file.write_text(_VALID_CONNECTIONS)
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(_VALID_POLICY)
    defaults = dict(
        environment="localhost",
        api_keys=["k"],
        connections_file=str(connections_file),
        policy_file=str(policy_file),
    )
    defaults.update(overrides)
    return AppConfig(**defaults)


def _writer() -> Principal:
    return Principal(subject="admin-a", scopes=frozenset({"admin:config:write"}))


def _setup(tmp_path, monkeypatch, **overrides):
    cfg = _cfg(tmp_path, monkeypatch, **overrides)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))
    return cfg


# --- export -----------------------------------------------------------------


def test_export_includes_only_submitted_documents(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch)

    bundle = governance.export_change_set(
        cfg,
        _writer(),
        connections_yaml=None,
        policy_yaml=_VALID_POLICY_V2,
        catalog_yaml=None,
        templates_yaml=None,
        description="tighten joins",
    )

    assert set(bundle.documents) == {"policy"}
    assert bundle.documents["policy"] == _VALID_POLICY_V2
    assert bundle.description == "tighten joins"
    assert bundle.base_version_id == "1"
    assert bundle.base_fingerprint
    assert bundle.contains_connections is False


def test_export_does_not_disclose_inherited_active_content(tmp_path, monkeypatch):
    """An unset document is never resolved into the exported bundle, so export
    can't be used to read the active connections/policy content."""
    cfg = _setup(tmp_path, monkeypatch)

    bundle = governance.export_change_set(
        cfg,
        _writer(),
        connections_yaml=None,
        policy_yaml=_VALID_POLICY_V2,
        catalog_yaml=None,
        templates_yaml=None,
        description=None,
    )

    assert "connections" not in bundle.documents
    # The literal connection-string content of the active version must not leak.
    assert "postgresql+asyncpg" not in bundle.model_dump_json()


def test_export_bundle_with_connections_sets_flag(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch)

    bundle = governance.export_change_set(
        cfg,
        _writer(),
        connections_yaml=_VALID_CONNECTIONS,
        policy_yaml=None,
        catalog_yaml=None,
        templates_yaml=None,
        description=None,
    )

    assert bundle.contains_connections is True


# --- import -----------------------------------------------------------------


def test_import_roundtrip_valid_not_stale(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch)
    bundle = governance.export_change_set(
        cfg,
        _writer(),
        connections_yaml=None,
        policy_yaml=_VALID_POLICY_V2,
        catalog_yaml=None,
        templates_yaml=None,
        description="tighten joins",
    )

    result = governance.import_change_set(cfg, _writer(), bundle)

    assert result.valid is True
    assert result.errors == []
    assert result.stale_base is False
    assert result.ready_to_stage is True
    signal = {d.document: d.change for d in result.documents}
    assert signal["policy"] == "submitted"
    assert signal["connections"] == "inherited"


def test_import_detects_stale_base(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch)
    # Bundle composed against the bootstrap base (v1).
    bundle = governance.export_change_set(
        cfg,
        _writer(),
        connections_yaml=None,
        policy_yaml=_VALID_POLICY_V2,
        catalog_yaml=None,
        templates_yaml=None,
        description="from v1",
    )

    # Someone else moves the active base to a v2 with a different policy.
    store = get_config_version_store()
    v2 = store.create_staged_version(
        connections_yaml=_VALID_CONNECTIONS,
        policy_yaml="default:\n  enabled: true\n  max_select_columns: 5\n",
        catalog_yaml=None,
        templates_yaml=None,
        description="drift",
        actor="admin-b",
    )
    store.mark_active(v2.id, actor="admin-b")

    result = governance.import_change_set(cfg, _writer(), bundle)

    assert result.stale_base is True
    assert result.base_conflict_documents == ["policy"]
    assert any("changed since" in w for w in result.warnings)
    # It's still valid content — stale-base is a warning, not a hard error.
    assert result.valid is True


def test_import_rejects_invalid_content(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch)
    bundle = ConfigChangeSetBundle(
        base_version_id="1",
        base_fingerprint=None,
        created_at=__import__("datetime").datetime.now(),
        documents={"policy": "default: [this is not a mapping]"},
    )

    result = governance.import_change_set(cfg, _writer(), bundle)

    assert result.valid is False
    assert result.errors
    assert result.ready_to_stage is False


def test_import_rejects_oversized_bundle(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch, config_bundle_max_bytes=50)
    bundle = ConfigChangeSetBundle(
        base_version_id="1",
        base_fingerprint=None,
        created_at=__import__("datetime").datetime.now(),
        documents={"policy": _VALID_POLICY + "# padding comment\n" * 20},
    )

    result = governance.import_change_set(cfg, _writer(), bundle)

    assert result.valid is False
    assert any("too large" in e for e in result.errors)


def test_import_without_fingerprint_warns_cannot_check_drift(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch)
    governance.export_change_set(  # bootstrap the store
        cfg,
        _writer(),
        connections_yaml=None,
        policy_yaml=None,
        catalog_yaml=None,
        templates_yaml=None,
        description=None,
    )
    bundle = ConfigChangeSetBundle(
        base_version_id=None,
        base_fingerprint=None,
        created_at=__import__("datetime").datetime.now(),
        documents={"policy": _VALID_POLICY_V2},
    )

    result = governance.import_change_set(cfg, _writer(), bundle)

    assert result.stale_base is False
    assert any("no base fingerprint" in w for w in result.warnings)


def test_import_flags_connections_document(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch)
    bundle = governance.export_change_set(
        cfg,
        _writer(),
        connections_yaml=_VALID_CONNECTIONS,
        policy_yaml=None,
        catalog_yaml=None,
        templates_yaml=None,
        description=None,
    )

    result = governance.import_change_set(cfg, _writer(), bundle)

    assert result.contains_connections is True
    assert any("connections document" in w for w in result.warnings)


def test_import_empty_delta_is_not_ready_to_stage(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch)
    governance.export_change_set(  # bootstrap
        cfg,
        _writer(),
        connections_yaml=None,
        policy_yaml=None,
        catalog_yaml=None,
        templates_yaml=None,
        description=None,
    )
    bundle = governance.export_change_set(
        cfg,
        _writer(),
        connections_yaml=None,
        policy_yaml=None,
        catalog_yaml=None,
        templates_yaml=None,
        description=None,
    )
    assert bundle.documents == {}

    result = governance.import_change_set(cfg, _writer(), bundle)

    assert result.valid is True
    assert result.ready_to_stage is False


def test_import_never_persists_a_version(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch)
    bundle = governance.export_change_set(
        cfg,
        _writer(),
        connections_yaml=None,
        policy_yaml=_VALID_POLICY_V2,
        catalog_yaml=None,
        templates_yaml=None,
        description=None,
    )
    store = get_config_version_store()
    before = {v.id for v in store.list_versions()}

    governance.import_change_set(cfg, _writer(), bundle)

    after = {v.id for v in store.list_versions()}
    assert before == after  # import stages nothing
