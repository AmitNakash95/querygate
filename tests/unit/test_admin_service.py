"""Unit tests for the config-governance orchestration (querygate/admin/service.py)."""

from __future__ import annotations

import json

import pytest

from querygate.admin import service as governance
from querygate.admin.models import (
    CandidatePolicySimulationRequest,
    ConfigSemanticDiffRequest,
    ConfigVersionStatus,
)
from querygate.admin.store import (
    ConfigVersionStore,
    get_config_version_store,
    set_config_version_store,
)
from querygate.audit.sinks import JsonlAuditSink, reset_audit_sink, set_audit_sink
from querygate.connections.registry import get_registry
from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.exceptions import ConfigValidationError
from querygate.policy.loader import get_policy_store

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


def test_preview_validates_and_reports_only_document_level_changes(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))
    principal = _principal()

    result = governance.preview(
        cfg,
        principal,
        connections_yaml=None,
        policy_yaml="default:\n  enabled: true\n  max_joins: 2\n",
        catalog_yaml=None,
    )

    assert result.valid is True
    assert result.ready_to_stage is True
    assert {item.document: item.change for item in result.documents} == {
        "connections": "inherited",
        "policy": "submitted",
        "catalog": "inherited",
        "templates": "inherited",
    }
    serialized = result.model_dump_json()
    assert "max_joins" not in serialized
    assert "TEST_ADMIN_URL" not in serialized
    assert "postgresql" not in serialized


@pytest.mark.security
def test_write_only_preview_is_not_an_active_config_equality_oracle(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))
    principal = _principal()

    exact = governance.preview(
        cfg,
        principal,
        connections_yaml=None,
        policy_yaml=_VALID_POLICY,
        catalog_yaml=None,
    )
    different = governance.preview(
        cfg,
        principal,
        connections_yaml=None,
        policy_yaml="default:\n  enabled: true\n  max_joins: 2\n",
        catalog_yaml=None,
    )

    assert exact.documents == different.documents
    assert exact.documents[1].change == "submitted"


def test_preview_is_audited_without_candidate_content(tmp_path, monkeypatch):
    audit_path = tmp_path / "preview-audit.jsonl"
    set_audit_sink(JsonlAuditSink(str(audit_path)))
    cfg = _cfg(tmp_path, monkeypatch)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))

    governance.preview(
        cfg,
        _principal(),
        connections_yaml=None,
        policy_yaml="default:\n  enabled: true\n  max_joins: 2\n",
        catalog_yaml=None,
    )

    raw = audit_path.read_text()
    event = json.loads(raw)
    assert event["action"] == "preview"
    assert event["principal_id"] == "agent-a"
    assert "max_joins" not in raw
    assert "TEST_ADMIN_URL" not in raw


def test_candidate_simulation_uses_isolated_context_and_redacts_values(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    resolved_secret_marker = "resolved-connection-secret"
    monkeypatch.setenv(
        "TEST_ADMIN_URL",
        f"postgresql+asyncpg://user:{resolved_secret_marker}@localhost/x",
    )
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))
    live_registry = get_registry()
    live_policy_store = get_policy_store()
    static_marker = "static-filter-super-secret"
    claim_marker = "claim-super-secret"
    predicate_marker = "query-predicate-super-secret"
    candidate_policy = f"""
default:
  enabled: false
principals:
  reporting-agent:
    fresh:
      enabled: true
      allowed_tables: [orders]
      denied_columns:
        orders: [customer_email]
      max_limit: 17
      mandatory_row_filters:
        - table: orders
          column: tenant_id
          from_claim: tenant_id
        - table: orders
          column: region
          value: {static_marker}
"""

    result = governance.simulate_candidate_policy(
        cfg,
        Principal(
            subject="admin-a",
            scopes=frozenset({"admin:config:read", "admin:config:write"}),
        ),
        CandidatePolicySimulationRequest(
            policy_yaml=candidate_policy,
            principal="reporting-agent",
            claims={"tenant_id": claim_marker},
            connection="fresh",
            table="orders",
            columns=["id"],
            query={
                "from": "orders",
                "select": ["orders.id"],
                "where": {"col": "orders.status", "op": "eq", "value": predicate_marker},
            },
        ),
    )

    assert result.decision == "allow"
    assert result.query_allowed is True
    assert result.guardrails is not None
    assert result.guardrails.max_limit == 17
    assert [item.model_dump() for item in result.mandatory_filters] == [
        {
            "table": "orders",
            "column": "tenant_id",
            "source": "claim",
            "claim": "tenant_id",
            "ready": True,
        },
        {
            "table": "orders",
            "column": "region",
            "source": "configured_literal",
            "claim": None,
            "ready": True,
        },
    ]
    serialized = result.model_dump_json()
    assert static_marker not in serialized
    assert claim_marker not in serialized
    assert predicate_marker not in serialized
    assert resolved_secret_marker not in serialized
    assert "TEST_ADMIN_URL" not in serialized
    assert get_config_version_store().list_versions() == []
    assert get_registry() is live_registry
    assert get_policy_store() is live_policy_store


def test_candidate_simulation_denies_query_and_missing_mandatory_claim(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))
    candidate_policy = """
default:
  enabled: true
  max_select_columns: 1
  mandatory_row_filters:
    - table: orders
      column: tenant_id
      from_claim: tenant_id
"""

    result = governance.simulate_candidate_policy(
        cfg,
        _principal(),
        CandidatePolicySimulationRequest(
            policy_yaml=candidate_policy,
            principal="reporting-agent",
            connection="fresh",
            table="orders",
            query={"from": "orders", "select": ["orders.id", "orders.total"]},
        ),
    )

    assert result.decision == "deny"
    assert result.query_allowed is False
    assert {reason.code for reason in result.reasons} == {
        "query_policy_denied",
        "mandatory_claim_missing",
    }
    assert result.mandatory_filters[0].ready is False


@pytest.mark.security
def test_candidate_simulation_does_not_reveal_filters_for_denied_table(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))
    hidden_column = "hidden_tenant_column"
    candidate_policy = f"""
default:
  enabled: true
  denied_tables: [payroll]
  mandatory_row_filters:
    - table: payroll
      column: {hidden_column}
      value: do-not-return
"""

    result = governance.simulate_candidate_policy(
        cfg,
        _principal(),
        CandidatePolicySimulationRequest(
            policy_yaml=candidate_policy,
            principal="reporting-agent",
            connection="fresh",
            table="payroll",
        ),
    )

    assert result.decision == "deny"
    assert result.mandatory_filters == []
    assert hidden_column not in result.model_dump_json()


@pytest.mark.security
def test_candidate_simulation_masks_invalid_candidate_content(tmp_path, monkeypatch):
    audit_path = tmp_path / "simulation-audit.jsonl"
    set_audit_sink(JsonlAuditSink(str(audit_path)))
    cfg = _cfg(tmp_path, monkeypatch)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))
    marker = "static-filter-value-must-not-leak"

    with pytest.raises(ConfigValidationError) as exc_info:
        governance.simulate_candidate_policy(
            cfg,
            _principal(),
            CandidatePolicySimulationRequest(
                policy_yaml=f"""
default:
  enabled: true
  mandatory_row_filters:
    - table: orders
      column: tenant_id
      value: {marker}
      unsupported_field: true
""",
                principal="reporting-agent",
                connection="fresh",
                table="orders",
            ),
        )

    assert marker not in str(exc_info.value)
    assert marker not in audit_path.read_text()
    event = json.loads(audit_path.read_text())
    assert event["action"] == "simulate"
    assert event["outcome"] == "rejected"
    assert get_config_version_store().list_versions() == []


def _diff_actor() -> Principal:
    return Principal(
        subject="admin-a", scopes=frozenset({"admin:config:read", "admin:config:write"})
    )


def test_diff_reports_guardrail_change_without_mutating_live_state(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))
    live_registry = get_registry()
    live_policy_store = get_policy_store()

    result = governance.diff_candidate_access(
        cfg,
        _diff_actor(),
        # Active default max_joins is 5; the candidate tightens it to 2.
        ConfigSemanticDiffRequest(policy_yaml="default:\n  enabled: true\n  max_joins: 2\n"),
    )

    guardrails = [c for c in result.changes if c.category == "guardrail"]
    assert any(c.object == "max_joins" and c.direction == "tightening" for c in guardrails)
    assert result.evaluation_scope == "connection_baseline"
    # The isolated diff must not touch the live singletons or governance store.
    assert get_registry() is live_registry
    assert get_policy_store() is live_policy_store
    assert get_config_version_store().list_versions() == []


def test_diff_with_no_candidate_changes_is_empty(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))

    result = governance.diff_candidate_access(cfg, _diff_actor(), ConfigSemanticDiffRequest())

    assert result.changes == []
    assert result.summary.total == 0


def test_diff_emits_success_audit_event(tmp_path, monkeypatch):
    audit_path = tmp_path / "diff-audit.jsonl"
    set_audit_sink(JsonlAuditSink(str(audit_path)))
    cfg = _cfg(tmp_path, monkeypatch)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))

    governance.diff_candidate_access(
        cfg,
        _diff_actor(),
        ConfigSemanticDiffRequest(policy_yaml="default:\n  enabled: true\n  max_limit: 5\n"),
    )

    event = json.loads(audit_path.read_text())
    assert event["action"] == "diff"
    assert event["outcome"] == "success"


@pytest.mark.security
def test_diff_masks_invalid_candidate_and_audits_rejection(tmp_path, monkeypatch):
    audit_path = tmp_path / "diff-audit.jsonl"
    set_audit_sink(JsonlAuditSink(str(audit_path)))
    cfg = _cfg(tmp_path, monkeypatch)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))
    marker = "static-filter-value-must-not-leak"

    with pytest.raises(ConfigValidationError) as exc_info:
        governance.diff_candidate_access(
            cfg,
            _diff_actor(),
            ConfigSemanticDiffRequest(
                policy_yaml=f"""
default:
  enabled: true
  mandatory_row_filters:
    - table: orders
      column: tenant_id
      value: {marker}
      unsupported_field: true
"""
            ),
        )

    assert marker not in str(exc_info.value)
    assert marker not in audit_path.read_text()
    event = json.loads(audit_path.read_text())
    assert event["action"] == "diff"
    assert event["outcome"] == "rejected"


def test_blast_radius_reports_baseline_and_configured_principal_impact(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))
    live_registry = get_registry()
    live_policy_store = get_policy_store()

    result = governance.compute_blast_radius(
        cfg,
        _diff_actor(),
        ConfigSemanticDiffRequest(
            policy_yaml=(
                "default:\n"
                "  enabled: true\n"
                "  max_joins: 2\n"
                "principals:\n"
                "  agent-a:\n"
                "    '*':\n"
                "      denied_tables: [orders]\n"
            )
        ),
    )

    assert result.evaluation_scope == "connection_baseline_plus_configured_principals"
    assert any(c.object == "max_joins" for c in result.baseline.changes)
    assert result.principals_configured == 1
    assert result.principals_affected == 1
    (impact,) = result.principal_impacts
    assert impact.principal == "agent-a"
    assert any(c.category == "table_access" for c in impact.changes)
    # A newly-denied table for agent-a is tightening, so it won't be in
    # highest_risk (which only ranks access-expanding changes); the baseline
    # max_joins tightening is likewise excluded.
    assert result.highest_risk == []
    # The isolated diff must not touch the live singletons or governance store.
    assert get_registry() is live_registry
    assert get_policy_store() is live_policy_store
    assert get_config_version_store().list_versions() == []


def test_blast_radius_with_no_candidate_changes_is_empty(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))

    result = governance.compute_blast_radius(cfg, _diff_actor(), ConfigSemanticDiffRequest())

    assert result.baseline.changes == []
    assert result.principal_impacts == []
    assert result.principals_configured == 0


def test_blast_radius_emits_success_audit_event(tmp_path, monkeypatch):
    audit_path = tmp_path / "blast-radius-audit.jsonl"
    set_audit_sink(JsonlAuditSink(str(audit_path)))
    cfg = _cfg(tmp_path, monkeypatch)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))

    governance.compute_blast_radius(
        cfg,
        _diff_actor(),
        ConfigSemanticDiffRequest(policy_yaml="default:\n  enabled: true\n  max_limit: 5\n"),
    )

    event = json.loads(audit_path.read_text())
    assert event["action"] == "blast_radius"
    assert event["outcome"] == "success"


@pytest.mark.security
def test_blast_radius_masks_invalid_candidate_and_audits_rejection(tmp_path, monkeypatch):
    audit_path = tmp_path / "blast-radius-audit.jsonl"
    set_audit_sink(JsonlAuditSink(str(audit_path)))
    cfg = _cfg(tmp_path, monkeypatch)
    set_config_version_store(ConfigVersionStore(str(tmp_path / "gov")))
    marker = "static-filter-value-must-not-leak"

    with pytest.raises(ConfigValidationError) as exc_info:
        governance.compute_blast_radius(
            cfg,
            _diff_actor(),
            ConfigSemanticDiffRequest(
                policy_yaml=f"""
default:
  enabled: true
  mandatory_row_filters:
    - table: orders
      column: tenant_id
      value: {marker}
      unsupported_field: true
"""
            ),
        )

    assert marker not in str(exc_info.value)
    assert marker not in audit_path.read_text()
    event = json.loads(audit_path.read_text())
    assert event["action"] == "blast_radius"
    assert event["outcome"] == "rejected"
    assert get_config_version_store().list_versions() == []


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
