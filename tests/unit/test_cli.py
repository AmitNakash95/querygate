"""Unit tests for the `querygate-validate-config` CLI."""

from __future__ import annotations

from pathlib import Path

from querygate.cli import validate_config
from querygate.secrets.resolvers import EnvSecretResolver, SecretResolverRegistry

CONNECTIONS_YAML = """
connections:
  - id: demo
    dialect: postgresql
    connection_string: ${TEST_DEMO_DB_URL}
    known_tables: [customers, orders]
"""

POLICY_YAML = """
default:
  enabled: true

connections:
  demo:
    max_joins: 2
"""


def _write(path: Path, name: str, content: str) -> str:
    file_path = path / name
    file_path.write_text(content)
    return str(file_path)


def test_valid_config_has_no_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DEMO_DB_URL", "postgresql+asyncpg://user:pass@localhost/demo")
    connections_file = _write(tmp_path, "connections.yaml", CONNECTIONS_YAML)
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)

    errors = validate_config(connections_file, policy_file)
    assert errors == []


def test_missing_connections_file_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DEMO_DB_URL", "postgresql+asyncpg://user:pass@localhost/demo")
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)

    errors = validate_config(str(tmp_path / "does_not_exist.yaml"), policy_file)
    assert len(errors) == 1
    assert "does_not_exist.yaml" in errors[0]


def test_unresolved_env_var_is_reported(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_DEMO_DB_URL", raising=False)
    connections_file = _write(tmp_path, "connections.yaml", CONNECTIONS_YAML)
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)

    errors = validate_config(connections_file, policy_file)
    assert len(errors) == 1
    assert "TEST_DEMO_DB_URL" in errors[0]


def test_policy_referencing_unknown_connection_id_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DEMO_DB_URL", "postgresql+asyncpg://user:pass@localhost/demo")
    connections_file = _write(tmp_path, "connections.yaml", CONNECTIONS_YAML)
    policy_file = _write(
        tmp_path,
        "policy.yaml",
        """
default:
  enabled: true

connections:
  typo_connection:
    max_joins: 2
""",
    )

    errors = validate_config(connections_file, policy_file)
    assert len(errors) == 1
    assert "typo_connection" in errors[0]
    assert "demo" in errors[0]


def test_invalid_policy_field_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DEMO_DB_URL", "postgresql+asyncpg://user:pass@localhost/demo")
    connections_file = _write(tmp_path, "connections.yaml", CONNECTIONS_YAML)
    policy_file = _write(
        tmp_path,
        "policy.yaml",
        """
default:
  enabled: true
  max_joins: "not-a-number"
""",
    )

    errors = validate_config(connections_file, policy_file)
    assert len(errors) == 1
    assert policy_file in errors[0]


def test_both_files_broken_reports_both_errors(tmp_path):
    errors = validate_config(
        str(tmp_path / "missing_connections.yaml"), str(tmp_path / "missing_policy.yaml")
    )
    assert len(errors) == 2


def test_catalog_file_is_optional(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DEMO_DB_URL", "postgresql+asyncpg://user:pass@localhost/demo")
    connections_file = _write(tmp_path, "connections.yaml", CONNECTIONS_YAML)
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)

    errors = validate_config(connections_file, policy_file, catalog_file=None)
    assert errors == []


def test_valid_catalog_file_has_no_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DEMO_DB_URL", "postgresql+asyncpg://user:pass@localhost/demo")
    connections_file = _write(tmp_path, "connections.yaml", CONNECTIONS_YAML)
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)
    catalog_file = _write(
        tmp_path,
        "catalog.yaml",
        """
connections:
  demo:
    tables:
      customers:
        description: "One row per customer."
""",
    )

    errors = validate_config(connections_file, policy_file, catalog_file=catalog_file)
    assert errors == []


def test_missing_catalog_file_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DEMO_DB_URL", "postgresql+asyncpg://user:pass@localhost/demo")
    connections_file = _write(tmp_path, "connections.yaml", CONNECTIONS_YAML)
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)

    errors = validate_config(
        connections_file, policy_file, catalog_file=str(tmp_path / "missing_catalog.yaml")
    )
    assert len(errors) == 1
    assert "missing_catalog.yaml" in errors[0]


def test_catalog_referencing_unknown_connection_id_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DEMO_DB_URL", "postgresql+asyncpg://user:pass@localhost/demo")
    connections_file = _write(tmp_path, "connections.yaml", CONNECTIONS_YAML)
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)
    catalog_file = _write(
        tmp_path,
        "catalog.yaml",
        """
connections:
  typo_connection:
    tables: {}
""",
    )

    errors = validate_config(connections_file, policy_file, catalog_file=catalog_file)
    assert len(errors) == 1
    assert "typo_connection" in errors[0]
    assert "demo" in errors[0]


def test_invalid_catalog_field_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DEMO_DB_URL", "postgresql+asyncpg://user:pass@localhost/demo")
    connections_file = _write(tmp_path, "connections.yaml", CONNECTIONS_YAML)
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)
    catalog_file = _write(
        tmp_path,
        "catalog.yaml",
        """
connections:
  demo:
    tables:
      customers:
        not_a_real_field: 1
""",
    )

    errors = validate_config(connections_file, policy_file, catalog_file=catalog_file)
    assert len(errors) == 1
    assert catalog_file in errors[0]


def test_validate_config_resolves_connection_strings_through_custom_resolver_registry(tmp_path):
    connections_file = _write(
        tmp_path,
        "connections.yaml",
        """
connections:
  - id: demo
    dialect: postgresql
    connection_string: ${vault:demo#url}
""",
    )
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)
    resolver_registry = SecretResolverRegistry(
        {"env": EnvSecretResolver({}), "vault": _FakeResolver({"demo#url": "postgresql://ok"})}
    )

    errors = validate_config(connections_file, policy_file, resolver_registry=resolver_registry)
    assert errors == []


def test_validate_config_reports_unresolvable_vault_reference(tmp_path):
    connections_file = _write(
        tmp_path,
        "connections.yaml",
        """
connections:
  - id: demo
    dialect: postgresql
    connection_string: ${vault:demo#url}
""",
    )
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)
    resolver_registry = SecretResolverRegistry({"env": EnvSecretResolver({})})

    errors = validate_config(connections_file, policy_file, resolver_registry=resolver_registry)
    assert len(errors) == 1
    assert "No secret resolver registered for scheme 'vault'" in errors[0]


class _FakeResolver:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def resolve(self, reference: str) -> str:
        return self._values[reference]
