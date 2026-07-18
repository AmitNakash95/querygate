"""Unit tests for the config-governance orchestration (querygate/admin/service.py)."""

from __future__ import annotations

import json

import pytest

from querygate.admin import service as governance
from querygate.admin.models import ConfigVersionStatus
from querygate.admin.store import ConfigVersionStore, set_config_version_store
from querygate.audit.sinks import JsonlAuditSink, reset_audit_sink, set_audit_sink
from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.exceptions import ConfigValidationError

_VALID_CONNECTIONS = """
connections:
  - id: fresh
    dialect: postgresql
    connection_string: ${TEST_ADMIN_URL}
"""
_VALID_POLICY = "default:\n  enabled: true\n"


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


def _principal(**scopes_kwargs) -> Principal:
    return Principal(subject="agent-a", scopes=frozenset({"admin:config:write"}))


def test_get_current_bootstraps_when_empty(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))

    current = governance.get_current(cfg)

    assert current.id == "1"
    assert current.status == ConfigVersionStatus.ACTIVE


def test_validate_reports_errors_for_broken_yaml(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))

    errors = governance.validate(
        cfg,
        connections_yaml="connections:\n  - id: demo\n    dialect: postgresql\n",  # missing connection_string
        policy_yaml=None,
        catalog_yaml=None,
    )

    assert errors


def test_validate_valid_candidate_has_no_errors(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))

    errors = governance.validate(cfg, connections_yaml=None, policy_yaml=None, catalog_yaml=None)

    assert errors == []


def test_stage_creates_staged_version_without_activating(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))
    principal = _principal()

    version = governance.stage(
        cfg,
        principal,
        connections_yaml=None,
        policy_yaml="default:\n  enabled: true\n  max_joins: 2\n",
        catalog_yaml=None,
        description="tighten max_joins",
    )

    assert version.status == ConfigVersionStatus.STAGED
    assert version.created_by == "agent-a"
    assert "max_joins: 2" in version.policy_yaml
    assert version.connections_yaml == _VALID_CONNECTIONS  # inherited, unset in the request
    assert governance.get_current(cfg).id == "1"  # active version untouched


def test_stage_rejects_invalid_candidate_without_persisting(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))
    principal = _principal()

    with pytest.raises(ConfigValidationError):
        governance.stage(
            cfg,
            principal,
            connections_yaml="not: [valid, connections, shape",
            policy_yaml=None,
            catalog_yaml=None,
            description=None,
        )

    assert [v.id for v in governance.list_versions(cfg)] == ["1"]  # only the bootstrap version


@pytest.mark.asyncio
async def test_apply_activates_staged_version_and_reloads(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))
    principal = _principal()
    staged = governance.stage(
        cfg,
        principal,
        connections_yaml=None,
        policy_yaml="default:\n  enabled: true\n  max_joins: 2\n",
        catalog_yaml=None,
        description=None,
    )

    updated, reload_result = await governance.apply(cfg, principal, staged.id)

    assert updated.status == ConfigVersionStatus.ACTIVE
    assert reload_result.connection_ids == ["fresh"]
    assert governance.get_current(cfg).id == staged.id


@pytest.mark.asyncio
async def test_apply_unknown_version_raises_not_found(tmp_path, monkeypatch):
    from querygate.core.exceptions import NotFoundError

    cfg = _cfg(tmp_path, monkeypatch)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))
    principal = _principal()

    with pytest.raises(NotFoundError):
        await governance.apply(cfg, principal, "99")


@pytest.mark.asyncio
async def test_apply_records_rollback_action_for_a_previously_active_version(tmp_path, monkeypatch):
    audit_path = tmp_path / "audit.jsonl"
    set_audit_sink(JsonlAuditSink(str(audit_path)))
    cfg = _cfg(tmp_path, monkeypatch)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))
    principal = _principal()
    staged = governance.stage(
        cfg,
        principal,
        connections_yaml=None,
        policy_yaml="default:\n  enabled: true\n  max_joins: 2\n",
        catalog_yaml=None,
        description=None,
    )
    await governance.apply(cfg, principal, staged.id)  # v2 becomes active

    await governance.apply(cfg, principal, "1")  # roll back to v1

    events = [json.loads(line) for line in audit_path.read_text().splitlines()]
    actions = [e["action"] for e in events if e["event_type"] == "config.governance"]
    assert actions == ["stage", "apply", "rollback"]


@pytest.mark.asyncio
async def test_apply_rejects_a_version_that_no_longer_validates(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))
    principal = _principal()
    staged = governance.stage(
        cfg,
        principal,
        connections_yaml=None,
        policy_yaml="default:\n  enabled: true\n  max_joins: 2\n",
        catalog_yaml=None,
        description=None,
    )

    monkeypatch.delenv("TEST_ADMIN_URL", raising=False)  # the env var the version depends on

    with pytest.raises(ConfigValidationError):
        await governance.apply(cfg, principal, staged.id)
    assert governance.get_current(cfg).id == "1"  # active version unchanged
