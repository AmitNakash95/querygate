"""File-backed durable store for config-governance versions.

Layout under `AppConfig.config_governance_dir`:

    versions/<id>/manifest.json       — ConfigVersion, minus the YAML content
    versions/<id>/connections.yaml
    versions/<id>/policy.yaml
    versions/<id>/catalog.yaml        — only when the version has a catalog
    versions/<id>/templates.yaml      — only when the version has query templates
    current.json                      — {"version_id": "<id>"} pointer

No database dependency — matches this project's existing "file-configured"
posture (connections.yaml/policy.yaml/catalog.yaml) rather than introducing
an app-owned database for what is, at this stage, a low-volume, admin-
triggered history.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional

from querygate.admin.models import ConfigVersion, ConfigVersionStatus
from querygate.core.exceptions import NotFoundError


class ConfigVersionFilePaths(NamedTuple):
    connections: str
    policy: str
    catalog: Optional[str]
    templates: Optional[str]


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content)
    os.replace(tmp_path, path)


class ConfigVersionStore:
    def __init__(self, root: str) -> None:
        self._root = Path(root)
        self._versions_dir = self._root / "versions"
        self._current_path = self._root / "current.json"
        self._lock = threading.Lock()

    def _version_dir(self, version_id: str) -> Path:
        return self._versions_dir / version_id

    def _manifest_path(self, version_id: str) -> Path:
        return self._version_dir(version_id) / "manifest.json"

    def _read_manifest(self, version_id: str) -> ConfigVersion:
        path = self._manifest_path(version_id)
        if not path.exists():
            raise NotFoundError(f"Unknown config version: {version_id!r}")
        manifest = json.loads(path.read_text())
        version_dir = self._version_dir(version_id)
        manifest["connections_yaml"] = (version_dir / "connections.yaml").read_text()
        manifest["policy_yaml"] = (version_dir / "policy.yaml").read_text()
        catalog_path = version_dir / "catalog.yaml"
        manifest["catalog_yaml"] = catalog_path.read_text() if catalog_path.exists() else None
        templates_path = version_dir / "templates.yaml"
        manifest["templates_yaml"] = templates_path.read_text() if templates_path.exists() else None
        return ConfigVersion.model_validate(manifest)

    def _write_manifest(self, version: ConfigVersion) -> None:
        # Content lives in its own files (see file_paths), not duplicated
        # into manifest.json — keeps the manifest small and the YAML the
        # single source of truth for what a version actually contains.
        manifest = version.model_dump(
            mode="json",
            exclude={"connections_yaml", "policy_yaml", "catalog_yaml", "templates_yaml"},
        )
        _atomic_write(self._manifest_path(version.id), json.dumps(manifest, indent=2))

    def _write_content(
        self,
        version_id: str,
        connections_yaml: str,
        policy_yaml: str,
        catalog_yaml: Optional[str],
        templates_yaml: Optional[str],
    ) -> None:
        version_dir = self._version_dir(version_id)
        version_dir.mkdir(parents=True, exist_ok=True)
        (version_dir / "connections.yaml").write_text(connections_yaml)
        (version_dir / "policy.yaml").write_text(policy_yaml)
        if catalog_yaml is not None:
            (version_dir / "catalog.yaml").write_text(catalog_yaml)
        if templates_yaml is not None:
            (version_dir / "templates.yaml").write_text(templates_yaml)

    def _next_id(self) -> str:
        if not self._versions_dir.exists():
            return "1"
        existing = [
            int(p.name) for p in self._versions_dir.iterdir() if p.is_dir() and p.name.isdigit()
        ]
        return str((max(existing) if existing else 0) + 1)

    def file_paths(self, version_id: str) -> ConfigVersionFilePaths:
        """Real on-disk paths for a version — consumed directly by
        `config_reload.reload_config()` and `cli.validate_config()`, which
        already work against file paths, so applying/validating a version
        needs no separate codepath from the file-based flow item 5 built.
        """
        version_dir = self._version_dir(version_id)
        if not version_dir.exists():
            raise NotFoundError(f"Unknown config version: {version_id!r}")
        catalog_path = version_dir / "catalog.yaml"
        templates_path = version_dir / "templates.yaml"
        return ConfigVersionFilePaths(
            connections=str(version_dir / "connections.yaml"),
            policy=str(version_dir / "policy.yaml"),
            catalog=str(catalog_path) if catalog_path.exists() else None,
            templates=str(templates_path) if templates_path.exists() else None,
        )

    def get_version(self, version_id: str) -> ConfigVersion:
        return self._read_manifest(version_id)

    def list_versions(self) -> list[ConfigVersion]:
        if not self._versions_dir.exists():
            return []
        ids = [p.name for p in self._versions_dir.iterdir() if p.is_dir()]
        versions = [self._read_manifest(version_id) for version_id in ids]
        return sorted(versions, key=lambda v: int(v.id))

    def get_active_version_id(self) -> Optional[str]:
        if not self._current_path.exists():
            return None
        return json.loads(self._current_path.read_text())["version_id"]

    def get_active_version(self) -> Optional[ConfigVersion]:
        version_id = self.get_active_version_id()
        return self._read_manifest(version_id) if version_id is not None else None

    def bootstrap_if_empty(
        self,
        *,
        connections_yaml: str,
        policy_yaml: str,
        catalog_yaml: Optional[str],
        templates_yaml: Optional[str] = None,
    ) -> ConfigVersion:
        """Seed version "1" from whatever files the deployment started with,
        the first time the governance API is used — so "current active
        version" always means something, even before any admin action.
        """
        with self._lock:
            active = self.get_active_version()
            if active is not None:
                return active
            now = datetime.now(timezone.utc)
            version_id = self._next_id()
            self._write_content(
                version_id, connections_yaml, policy_yaml, catalog_yaml, templates_yaml
            )
            version = ConfigVersion(
                id=version_id,
                status=ConfigVersionStatus.ACTIVE,
                created_at=now,
                created_by="system:bootstrap",
                description="Bootstrapped from the deployment's configured files.",
                connections_yaml=connections_yaml,
                policy_yaml=policy_yaml,
                catalog_yaml=catalog_yaml,
                templates_yaml=templates_yaml,
                applied_at=now,
                applied_by="system:bootstrap",
            )
            self._write_manifest(version)
            _atomic_write(self._current_path, json.dumps({"version_id": version_id}))
            return version

    def create_staged_version(
        self,
        *,
        connections_yaml: str,
        policy_yaml: str,
        catalog_yaml: Optional[str],
        templates_yaml: Optional[str] = None,
        description: Optional[str],
        actor: str,
    ) -> ConfigVersion:
        with self._lock:
            version_id = self._next_id()
            self._write_content(
                version_id, connections_yaml, policy_yaml, catalog_yaml, templates_yaml
            )
            version = ConfigVersion(
                id=version_id,
                status=ConfigVersionStatus.STAGED,
                created_at=datetime.now(timezone.utc),
                created_by=actor,
                description=description,
                connections_yaml=connections_yaml,
                policy_yaml=policy_yaml,
                catalog_yaml=catalog_yaml,
                templates_yaml=templates_yaml,
            )
            self._write_manifest(version)
            return version

    def mark_active(self, version_id: str, *, actor: str) -> ConfigVersion:
        """Make `version_id` the active version — serves both "apply" (a
        staged version's first activation) and "rollback" (reactivating a
        version that was active before) identically; the caller determines
        which word applies by checking the version's status beforehand.
        """
        with self._lock:
            version = self._read_manifest(version_id)  # raises NotFoundError
            previous_id = self.get_active_version_id()
            if previous_id is not None and previous_id != version_id:
                previous = self._read_manifest(previous_id)
                updated_previous = previous.model_copy(
                    update={"status": ConfigVersionStatus.INACTIVE}
                )
                self._write_manifest(updated_previous)
            now = datetime.now(timezone.utc)
            updated = version.model_copy(
                update={
                    "status": ConfigVersionStatus.ACTIVE,
                    "applied_at": now,
                    "applied_by": actor,
                    "previous_active_version_id": previous_id,
                }
            )
            self._write_manifest(updated)
            _atomic_write(self._current_path, json.dumps({"version_id": version_id}))
            return updated


_store: Optional[ConfigVersionStore] = None


def get_config_version_store() -> ConfigVersionStore:
    global _store
    if _store is None:
        from querygate.core.config import config

        _store = ConfigVersionStore(config.config_governance_dir)
    return _store


def set_config_version_store(store: ConfigVersionStore) -> None:
    """Override the process-wide store — used by tests and programmatic setup."""
    global _store
    _store = store
