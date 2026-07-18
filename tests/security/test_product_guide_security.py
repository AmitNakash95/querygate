"""Adversarial boundaries for product guidance and deployment diagnostics."""

from __future__ import annotations

import json

import pytest

from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.help.service import get_guide_service
from querygate.policy.loader import PolicyStore, set_policy_store

pytestmark = pytest.mark.security


def test_live_connection_and_principal_names_never_enter_static_search_results():
    set_registry(
        ConnectionRegistry(
            {
                "tenant_internal_finance_xyz": ConnectionProfile(
                    id="tenant_internal_finance_xyz",
                    dialect="postgresql",
                    connection_string="postgresql+asyncpg://private-host/db",
                )
            }
        )
    )
    set_policy_store(
        PolicyStore.from_dict(
            {
                "default": {"enabled": False},
                "principals": {
                    "hidden_subject_xyz": {"tenant_internal_finance_xyz": {"enabled": True}}
                },
            }
        )
    )

    service = get_guide_service()

    assert service.search("tenant_internal_finance_xyz").results == []
    assert service.search("hidden_subject_xyz").results == []


def test_access_context_is_recomputed_per_principal_without_cross_caller_cache_leakage():
    set_registry(
        ConnectionRegistry(
            {
                name: ConnectionProfile(
                    id=name,
                    dialect="postgresql",
                    connection_string=f"postgresql+asyncpg://private-host/{name}",
                )
                for name in ("alpha_only", "beta_only")
            }
        )
    )
    set_policy_store(
        PolicyStore.from_dict(
            {
                "default": {"enabled": False},
                "principals": {
                    "alpha": {"alpha_only": {"enabled": True}},
                    "beta": {"beta_only": {"enabled": True}},
                },
            }
        )
    )
    service = get_guide_service()

    alpha = service.access_summary(Principal(subject="alpha"))
    beta = service.access_summary(Principal(subject="beta"))
    alpha_again = service.access_summary(Principal(subject="alpha"))

    assert [item.id for item in alpha.visible_connections] == ["alpha_only"]
    assert [item.id for item in beta.visible_connections] == ["beta_only"]
    assert alpha_again == alpha
    assert "beta_only" not in alpha.model_dump_json()
    assert "alpha_only" not in beta.model_dump_json()


def test_redacted_admin_summary_hides_env_and_vault_references_and_policy_literals(tmp_path):
    connections = tmp_path / "connections.yaml"
    connections.write_text(
        """
connections:
  - id: env-backed
    dialect: postgresql
    connection_string: ${HIGHLY_SECRET_ENV_NAME}
  - id: vault-backed
    dialect: postgresql
    connection_string: ${vault:secret/querygate/hidden-path#password}
"""
    )
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        """
default:
  mandatory_row_filters:
    - table: hidden_tenant_table
      column: hidden_tenant_column
      from_claim: highly_sensitive_claim_name
principals:
  another-hidden-principal:
    env-backed:
      enabled: false
"""
    )
    cfg = AppConfig(
        environment="localhost",
        connections_file=str(connections),
        policy_file=str(policy),
    )
    reader = Principal(subject="reader", scopes=frozenset({"admin:config:read"}))

    serialized = json.dumps(
        get_guide_service().redacted_configuration(cfg, reader).model_dump(mode="json")
    )

    assert '"credential_source": "environment"' in serialized
    assert '"credential_source": "external_secret"' in serialized
    for forbidden in (
        "HIGHLY_SECRET_ENV_NAME",
        "secret/querygate/hidden-path",
        "password",
        "hidden_tenant_table",
        "hidden_tenant_column",
        "highly_sensitive_claim_name",
        "another-hidden-principal",
    ):
        assert forbidden not in serialized
