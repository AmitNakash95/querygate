"""Unit tests for ConfigVersionStore (querygate/admin/store.py)."""

from __future__ import annotations

import pytest

from querygate.admin.models import ConfigVersionStatus
from querygate.admin.store import ConfigVersionStore
from querygate.core.exceptions import NotFoundError


def _store(tmp_path) -> ConfigVersionStore:
    return ConfigVersionStore(str(tmp_path / "governance"))


def test_bootstrap_creates_active_version_1(tmp_path):
    store = _store(tmp_path)
    version = store.bootstrap_if_empty(
        connections_yaml="connections: []\n", policy_yaml="default: {}\n", catalog_yaml=None
    )
    assert version.id == "1"
    assert version.status == ConfigVersionStatus.ACTIVE
    assert version.created_by == "system:bootstrap"
    assert store.get_active_version_id() == "1"


def test_bootstrap_is_idempotent(tmp_path):
    store = _store(tmp_path)
    first = store.bootstrap_if_empty(
        connections_yaml="connections: []\n", policy_yaml="default: {}\n", catalog_yaml=None
    )
    second = store.bootstrap_if_empty(
        connections_yaml="connections: [changed]\n", policy_yaml="default: {}\n", catalog_yaml=None
    )
    assert first.id == second.id == "1"
    assert second.connections_yaml == "connections: []\n"  # not overwritten by the second call


def test_templates_yaml_snapshot_round_trips_and_appears_in_file_paths(tmp_path):
    store = _store(tmp_path)
    store.bootstrap_if_empty(
        connections_yaml="connections: []\n", policy_yaml="default: {}\n", catalog_yaml=None
    )
    staged = store.create_staged_version(
        connections_yaml="connections: []\n",
        policy_yaml="default: {}\n",
        catalog_yaml=None,
        templates_yaml="templates: []\n",
        description="add templates document",
        actor="agent-a",
    )
    # Re-read from disk (not the in-memory return) to prove it persisted.
    reloaded = store.get_version(staged.id)
    assert reloaded.templates_yaml == "templates: []\n"
    paths = store.file_paths(staged.id)
    assert paths.templates is not None and paths.templates.endswith("templates.yaml")


def test_version_without_templates_has_none_and_no_templates_path(tmp_path):
    store = _store(tmp_path)
    version = store.bootstrap_if_empty(
        connections_yaml="connections: []\n", policy_yaml="default: {}\n", catalog_yaml=None
    )
    assert version.templates_yaml is None
    assert store.file_paths(version.id).templates is None


def test_create_staged_version_does_not_change_active_version(tmp_path):
    store = _store(tmp_path)
    store.bootstrap_if_empty(
        connections_yaml="connections: []\n", policy_yaml="default: {}\n", catalog_yaml=None
    )
    staged = store.create_staged_version(
        connections_yaml="connections: []\n",
        policy_yaml="default:\n  max_joins: 2\n",
        catalog_yaml=None,
        description="tighten caps",
        actor="agent-a",
    )
    assert staged.id == "2"
    assert staged.status == ConfigVersionStatus.STAGED
    assert staged.created_by == "agent-a"
    assert staged.description == "tighten caps"
    assert store.get_active_version_id() == "1"


def test_mark_active_activates_and_demotes_previous(tmp_path):
    store = _store(tmp_path)
    store.bootstrap_if_empty(
        connections_yaml="connections: []\n", policy_yaml="default: {}\n", catalog_yaml=None
    )
    staged = store.create_staged_version(
        connections_yaml="connections: []\n",
        policy_yaml="default:\n  max_joins: 2\n",
        catalog_yaml=None,
        description=None,
        actor="agent-a",
    )

    updated = store.mark_active(staged.id, actor="agent-a")

    assert updated.status == ConfigVersionStatus.ACTIVE
    assert updated.applied_by == "agent-a"
    assert updated.applied_at is not None
    assert updated.previous_active_version_id == "1"
    assert store.get_active_version_id() == staged.id
    assert store.get_version("1").status == ConfigVersionStatus.INACTIVE


def test_rollback_reactivates_an_older_version(tmp_path):
    store = _store(tmp_path)
    store.bootstrap_if_empty(
        connections_yaml="connections: []\n", policy_yaml="default: {}\n", catalog_yaml=None
    )
    staged = store.create_staged_version(
        connections_yaml="connections: []\n",
        policy_yaml="default:\n  max_joins: 2\n",
        catalog_yaml=None,
        description=None,
        actor="agent-a",
    )
    store.mark_active(staged.id, actor="agent-a")

    rolled_back = store.mark_active("1", actor="agent-b")

    assert rolled_back.id == "1"
    assert rolled_back.status == ConfigVersionStatus.ACTIVE
    assert rolled_back.previous_active_version_id == staged.id
    assert store.get_version(staged.id).status == ConfigVersionStatus.INACTIVE
    assert store.get_active_version_id() == "1"


def test_get_version_raises_not_found(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(NotFoundError, match="99"):
        store.get_version("99")


def test_file_paths_raises_not_found(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(NotFoundError):
        store.file_paths("99")


def test_file_paths_omits_catalog_when_none(tmp_path):
    store = _store(tmp_path)
    store.bootstrap_if_empty(
        connections_yaml="connections: []\n", policy_yaml="default: {}\n", catalog_yaml=None
    )
    paths = store.file_paths("1")
    assert paths.catalog is None
    assert paths.connections.endswith("connections.yaml")
    assert paths.policy.endswith("policy.yaml")


def test_file_paths_includes_catalog_when_present(tmp_path):
    store = _store(tmp_path)
    store.bootstrap_if_empty(
        connections_yaml="connections: []\n",
        policy_yaml="default: {}\n",
        catalog_yaml="connections: {}\n",
    )
    paths = store.file_paths("1")
    assert paths.catalog is not None
    assert paths.catalog.endswith("catalog.yaml")


def test_list_versions_sorted_by_id(tmp_path):
    store = _store(tmp_path)
    store.bootstrap_if_empty(
        connections_yaml="connections: []\n", policy_yaml="default: {}\n", catalog_yaml=None
    )
    for i in range(3):
        store.create_staged_version(
            connections_yaml="connections: []\n",
            policy_yaml="default: {}\n",
            catalog_yaml=None,
            description=f"v{i}",
            actor="agent-a",
        )
    versions = store.list_versions()
    assert [v.id for v in versions] == ["1", "2", "3", "4"]


def test_get_active_version_returns_none_when_never_bootstrapped(tmp_path):
    store = _store(tmp_path)
    assert store.get_active_version() is None
    assert store.get_active_version_id() is None
