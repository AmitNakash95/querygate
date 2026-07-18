"""Orchestrates config-governance validate/stage/apply for the admin API.

Layered on top of the same primitives item 5 (config_reload) and item 17
(cli.validate_config) already built — applying a version is exactly
`reload_config()` pointed at that version's files, and validating a
candidate is exactly `validate_config()` pointed at a scratch directory.
Neither of those gets a parallel implementation here.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import List, Optional, Tuple

from querygate.admin.models import ConfigVersion, ConfigVersionStatus
from querygate.admin.store import ConfigVersionStore, get_config_version_store
from querygate.audit.logger import audit_config_change
from querygate.cli import validate_config
from querygate.config_reload import ReloadResult, reload_config
from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.exceptions import ConfigValidationError
from querygate.secrets.resolvers import build_secret_resolver_registry


def _bootstrap(cfg: AppConfig, store: ConfigVersionStore) -> ConfigVersion:
    connections_yaml = Path(cfg.connections_file).read_text()
    policy_yaml = Path(cfg.policy_file).read_text()
    catalog_yaml = (
        Path(cfg.catalog_file).read_text()
        if cfg.catalog_file and Path(cfg.catalog_file).exists()
        else None
    )
    return store.bootstrap_if_empty(
        connections_yaml=connections_yaml, policy_yaml=policy_yaml, catalog_yaml=catalog_yaml
    )


def _resolve_candidate(
    cfg: AppConfig,
    store: ConfigVersionStore,
    *,
    connections_yaml: Optional[str],
    policy_yaml: Optional[str],
    catalog_yaml: Optional[str],
) -> Tuple[str, str, Optional[str]]:
    """A field left unset (None) inherits unchanged from the active version.

    There is no dedicated way to explicitly clear a catalog back to "none"
    through this API — pass an empty-but-present catalog document (e.g.
    `"connections: {}"`) if that's genuinely needed; this keeps the request
    shape simple (no separate "unset" sentinel) for what is, in practice, a
    rare edge case.
    """
    active = _bootstrap(cfg, store)
    return (
        connections_yaml if connections_yaml is not None else active.connections_yaml,
        policy_yaml if policy_yaml is not None else active.policy_yaml,
        catalog_yaml if catalog_yaml is not None else active.catalog_yaml,
    )


def validate_candidate_content(
    cfg: AppConfig, connections_yaml: str, policy_yaml: str, catalog_yaml: Optional[str]
) -> List[str]:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        connections_file = tmp_path / "connections.yaml"
        connections_file.write_text(connections_yaml)
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text(policy_yaml)
        catalog_file: Optional[str] = None
        if catalog_yaml is not None:
            catalog_path = tmp_path / "catalog.yaml"
            catalog_path.write_text(catalog_yaml)
            catalog_file = str(catalog_path)
        return validate_config(
            str(connections_file),
            str(policy_file),
            catalog_file,
            resolver_registry=build_secret_resolver_registry(cfg),
        )


def validate(
    cfg: AppConfig,
    *,
    connections_yaml: Optional[str],
    policy_yaml: Optional[str],
    catalog_yaml: Optional[str],
) -> List[str]:
    store = get_config_version_store()
    resolved_connections, resolved_policy, resolved_catalog = _resolve_candidate(
        cfg,
        store,
        connections_yaml=connections_yaml,
        policy_yaml=policy_yaml,
        catalog_yaml=catalog_yaml,
    )
    return validate_candidate_content(cfg, resolved_connections, resolved_policy, resolved_catalog)


def stage(
    cfg: AppConfig,
    principal: Principal,
    *,
    connections_yaml: Optional[str],
    policy_yaml: Optional[str],
    catalog_yaml: Optional[str],
    description: Optional[str],
) -> ConfigVersion:
    store = get_config_version_store()
    resolved_connections, resolved_policy, resolved_catalog = _resolve_candidate(
        cfg,
        store,
        connections_yaml=connections_yaml,
        policy_yaml=policy_yaml,
        catalog_yaml=catalog_yaml,
    )
    errors = validate_candidate_content(
        cfg, resolved_connections, resolved_policy, resolved_catalog
    )
    if errors:
        audit_config_change(
            action="stage",
            outcome="rejected",
            principal=principal.subject,
            principal_scopes=sorted(principal.scopes),
            auth_method=principal.auth_method,
            description=description,
            error_category="validation",
        )
        raise ConfigValidationError("; ".join(errors))

    version = store.create_staged_version(
        connections_yaml=resolved_connections,
        policy_yaml=resolved_policy,
        catalog_yaml=resolved_catalog,
        description=description,
        actor=principal.subject,
    )
    audit_config_change(
        action="stage",
        outcome="success",
        principal=principal.subject,
        principal_scopes=sorted(principal.scopes),
        auth_method=principal.auth_method,
        version_id=version.id,
        description=description,
    )
    return version


async def apply(
    cfg: AppConfig, principal: Principal, version_id: str
) -> Tuple[ConfigVersion, ReloadResult]:
    """Activate `version_id` — this is "apply" for a staged version and
    "rollback" for a version that was previously active (same operation;
    the audit action label is chosen from the version's status just before
    it's mutated).
    """
    store = get_config_version_store()
    version = store.get_version(version_id)  # raises NotFoundError -> 404 at the route
    action = "rollback" if version.status == ConfigVersionStatus.INACTIVE else "apply"
    start = time.monotonic()

    errors = validate_candidate_content(
        cfg, version.connections_yaml, version.policy_yaml, version.catalog_yaml
    )
    if errors:
        audit_config_change(
            action=action,
            outcome="rejected",
            principal=principal.subject,
            principal_scopes=sorted(principal.scopes),
            auth_method=principal.auth_method,
            version_id=version_id,
            error_category="validation",
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        raise ConfigValidationError("; ".join(errors))

    paths = store.file_paths(version_id)
    reload_result = await reload_config(
        connections_file=paths.connections,
        policy_file=paths.policy,
        catalog_file=paths.catalog,
        resolver_registry=build_secret_resolver_registry(cfg),
    )
    updated_version = store.mark_active(version_id, actor=principal.subject)
    audit_config_change(
        action=action,
        outcome="success",
        principal=principal.subject,
        principal_scopes=sorted(principal.scopes),
        auth_method=principal.auth_method,
        version_id=version_id,
        previous_version_id=updated_version.previous_active_version_id,
        duration_ms=int((time.monotonic() - start) * 1000),
    )
    return updated_version, reload_result


def get_current(cfg: AppConfig) -> ConfigVersion:
    store = get_config_version_store()
    return _bootstrap(cfg, store)


def list_versions(cfg: AppConfig) -> List[ConfigVersion]:
    store = get_config_version_store()
    _bootstrap(cfg, store)
    return store.list_versions()


def get_version(version_id: str) -> ConfigVersion:
    return get_config_version_store().get_version(version_id)
