"""Unit coverage for the offline, versioned QueryGate product guide."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from querygate import __version__
from querygate.catalog.models import SchemaCatalog
from querygate.connections.models import ConnectionProfile
from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.exceptions import AuthorizationError
from querygate.help.service import GuideService, get_guide_service
from querygate.policy.models import Policy


def test_packaged_corpus_matches_installed_version_and_searches_deterministically():
    service = get_guide_service()

    first = service.search("configure policy limits")
    second = service.search("configure policy limits")

    assert service.version == __version__
    assert first == second
    assert first.results[0].topic_id == "configuration.policy"
    assert all(result.citation.version == __version__ for result in first.results)


def test_static_search_does_not_index_live_deployment_identifiers():
    response = get_guide_service().search("tenant_internal_ledger_xyz")

    assert response.results == []


def test_static_guide_remains_available_when_live_deployment_context_is_unavailable():
    with patch(
        "querygate.help.service.list_visible_connections",
        side_effect=RuntimeError("all databases unavailable"),
    ):
        result = get_guide_service().search("install QueryGate")

    assert result.results[0].topic_id == "setup.first-run"


@pytest.mark.parametrize(
    ("question", "expected_topic"),
    [
        ("install and run for the first time", "setup.first-run"),
        ("configure a database connection and secret", "configuration.connections"),
        ("why is my table not found or denied", "troubleshooting.errors"),
        ("monitor health metrics and audit logs", "operations.observability"),
        ("upgrade and rollback safely", "operations.upgrades"),
        ("how does QueryGate validate structured queries", "product.how-it-works"),
    ],
)
def test_representative_guide_tasks_rank_the_authoritative_topic_first(question, expected_topic):
    result = get_guide_service().search(question, limit=3)

    assert result.results[0].topic_id == expected_topic


@pytest.mark.parametrize(
    ("model_name", "model_type"),
    [
        ("app", AppConfig),
        ("connection", ConnectionProfile),
        ("policy", Policy),
        ("catalog", SchemaCatalog),
    ],
)
def test_every_current_config_model_field_has_a_generated_explanation(model_name, model_type):
    service = GuideService()

    for field_name in model_type.model_fields:
        result = service.explain_config_field(model_name, field_name)
        assert result.field == field_name
        assert result.description
        assert result.citation.version == __version__


def test_setup_checklist_and_error_explanation_are_source_attributed():
    service = get_guide_service()

    checklist = service.setup_checklist("production")
    error = service.explain_error("NOT_FOUND")

    assert len(checklist.steps) >= 6
    assert all(step.citation.topic_id for step in checklist.steps)
    assert "does not exist or is not visible" in error.explanation
    assert error.citation.topic_id == "troubleshooting.errors"


def test_access_summary_contains_only_caller_scopes_and_visible_connections():
    principal = Principal(
        subject="guide-reader",
        scopes=frozenset({"admin:config:read"}),
        auth_method="jwt",
    )

    result = get_guide_service().access_summary(principal)

    assert result.principal == "guide-reader"
    assert result.scopes == ["admin:config:read"]
    assert result.capabilities.read_configuration is True
    assert result.capabilities.change_configuration is False
    assert [connection.id for connection in result.visible_connections] == ["demo"]


@pytest.mark.parametrize(
    ("scopes", "read", "write", "reload"),
    [
        (set(), False, False, False),
        ({"admin:reload-config"}, False, False, True),
        ({"admin:config:read"}, True, False, False),
        ({"admin:config:write"}, False, True, False),
    ],
)
def test_access_capability_matrix_for_ordinary_operator_reader_and_writer(
    scopes, read, write, reload
):
    result = get_guide_service().access_summary(
        Principal(subject="role-under-test", scopes=frozenset(scopes))
    )

    assert result.capabilities.read_configuration is read
    assert result.capabilities.change_configuration is write
    assert result.capabilities.reload_configuration is reload


def _redaction_cfg(tmp_path) -> AppConfig:
    connections_file = tmp_path / "connections.yaml"
    connections_file.write_text(
        """
connections:
  - id: finance
    dialect: postgresql
    connection_string: postgresql+asyncpg://secret-user:secret-pass@secret-host/private
    description: description-secret-value
    known_tables: [internal_ledger]
"""
    )
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        """
default:
  denied_tables: [internal_ledger]
  mandatory_row_filters:
    - table: customers
      column: tenant_id
      value: secret-tenant-value
principals:
  hidden-principal:
    finance:
      enabled: false
"""
    )
    catalog_file = tmp_path / "catalog.yaml"
    catalog_file.write_text(
        """
version: 1
connections:
  finance:
    tables:
      internal_ledger:
        description: secret business meaning
        columns:
          private_amount:
            sensitivity: confidential
"""
    )
    return AppConfig(
        environment="localhost",
        connections_file=str(connections_file),
        policy_file=str(policy_file),
        catalog_file=str(catalog_file),
    )


def test_redacted_configuration_requires_read_scope(tmp_path):
    cfg = _redaction_cfg(tmp_path)

    with pytest.raises(AuthorizationError):
        get_guide_service().redacted_configuration(
            cfg, Principal(subject="writer", scopes=frozenset({"admin:config:write"}))
        )


def test_redacted_configuration_excludes_secrets_policy_names_and_other_principals(tmp_path):
    cfg = _redaction_cfg(tmp_path)
    principal = Principal(subject="config-reader", scopes=frozenset({"admin:config:read"}))

    result = get_guide_service().redacted_configuration(cfg, principal)
    serialized = json.dumps(result.model_dump(mode="json"))

    assert result.connections[0].credential_source == "configured"
    assert result.connections[0].known_table_count == 1
    assert result.policy.principal_override_count == 1
    assert result.catalog.table_count == 1
    assert result.version.created_by == "system"
    for forbidden in (
        "secret-user",
        "secret-pass",
        "secret-host",
        "description-secret-value",
        "internal_ledger",
        "secret-tenant-value",
        "hidden-principal",
        "private_amount",
        "secret business meaning",
    ):
        assert forbidden not in serialized
