"""Unit tests for the file-driven connection registry."""

from __future__ import annotations

import pydantic
import pytest

from querygate.connections.registry import ConnectionRegistry
from querygate.secrets.resolvers import EnvSecretResolver, SecretResolverRegistry


def test_env_var_interpolation(monkeypatch):
    monkeypatch.setenv("TEST_DB_URL", "postgresql+asyncpg://user:pass@host/db")
    registry = ConnectionRegistry.from_entries(
        [{"id": "demo", "dialect": "postgresql", "connection_string": "${TEST_DB_URL}"}]
    )
    assert registry.get("demo").connection_string == "postgresql+asyncpg://user:pass@host/db"


def test_dotenv_interpolation_matches_quickstart(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DOTENV_DB_URL", raising=False)
    (tmp_path / ".env").write_text(
        "DOTENV_DB_URL=postgresql+asyncpg://dotenv-user:dotenv-pass@host/db\n"
    )
    connections_file = tmp_path / "connections.yaml"
    connections_file.write_text("""
connections:
  - id: demo
    dialect: postgresql
    connection_string: ${DOTENV_DB_URL}
""")

    registry = ConnectionRegistry.from_file(str(connections_file))

    assert (
        registry.get("demo").connection_string
        == "postgresql+asyncpg://dotenv-user:dotenv-pass@host/db"
    )


def test_process_environment_overrides_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("PRECEDENCE_DB_URL=postgresql+asyncpg://dotenv/db\n")
    monkeypatch.setenv("PRECEDENCE_DB_URL", "postgresql+asyncpg://process/db")

    registry = ConnectionRegistry.from_entries(
        [
            {
                "id": "demo",
                "dialect": "postgresql",
                "connection_string": "${PRECEDENCE_DB_URL}",
            }
        ]
    )

    assert registry.get("demo").connection_string == "postgresql+asyncpg://process/db"


def test_missing_env_var_raises(monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    with pytest.raises(ValueError, match="MISSING_VAR"):
        ConnectionRegistry.from_entries(
            [{"id": "demo", "dialect": "postgresql", "connection_string": "${MISSING_VAR}"}]
        )


def test_duplicate_connection_id_rejected(monkeypatch):
    monkeypatch.setenv("A", "postgresql+asyncpg://a")
    monkeypatch.setenv("B", "postgresql+asyncpg://b")
    with pytest.raises(ValueError, match="Duplicate"):
        ConnectionRegistry.from_entries(
            [
                {"id": "demo", "dialect": "postgresql", "connection_string": "${A}"},
                {"id": "demo", "dialect": "postgresql", "connection_string": "${B}"},
            ]
        )


def test_unknown_connection_raises_keyerror(monkeypatch):
    monkeypatch.setenv("A", "postgresql+asyncpg://a")
    registry = ConnectionRegistry.from_entries(
        [{"id": "demo", "dialect": "postgresql", "connection_string": "${A}"}]
    )
    with pytest.raises(KeyError):
        registry.get("nonexistent")


def test_list_public_excludes_connection_string(monkeypatch):
    monkeypatch.setenv("A", "postgresql+asyncpg://user:supersecret@host/db")
    registry = ConnectionRegistry.from_entries(
        [
            {
                "id": "demo",
                "dialect": "postgresql",
                "connection_string": "${A}",
                "description": "demo db",
            }
        ]
    )
    public = registry.list_public()
    assert len(public) == 1
    assert public[0].id == "demo"
    assert not hasattr(public[0], "connection_string")
    dumped = public[0].model_dump()
    assert "connection_string" not in dumped
    assert "supersecret" not in str(dumped)


class _FakeResolver:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def resolve(self, reference: str) -> str:
        return self._values[reference]


def test_connection_string_resolved_through_custom_resolver_registry():
    registry = SecretResolverRegistry(
        {
            "env": EnvSecretResolver({}),
            "vault": _FakeResolver({"demo#url": "postgresql+asyncpg://vault-resolved/db"}),
        }
    )
    connections = ConnectionRegistry.from_entries(
        [{"id": "demo", "dialect": "postgresql", "connection_string": "${vault:demo#url}"}],
        resolver_registry=registry,
    )
    assert connections.get("demo").connection_string == "postgresql+asyncpg://vault-resolved/db"


def test_from_entries_rejects_a_dialect_mismatch_only_visible_after_interpolation(monkeypatch):
    """TODO.md item 158: the dialect-match validator on `ConnectionProfile`
    only matters in production if it actually runs on the RESOLVED
    connection string, on the one path (`ConnectionRegistry.from_entries`)
    that ever builds a `ConnectionProfile` bound for a real engine. This
    pins that claim at the registry level, not just against the model in
    isolation: `${MYSQL_URL}` resolves to a mysql backend, but the entry
    declares `dialect: postgresql` — this can only be caught by validating
    AFTER `registry.interpolate()` runs, exactly the order `from_entries`
    uses. If a future change ever built profiles via `model_construct()`
    (skipping validation) or reordered interpolate-then-validate, this is
    the test that would catch it.
    """
    monkeypatch.setenv("MYSQL_URL", "mysql+asyncmy://user:pass@host:3306/db")
    with pytest.raises(pydantic.ValidationError, match="mysql"):
        ConnectionRegistry.from_entries(
            [{"id": "demo", "dialect": "postgresql", "connection_string": "${MYSQL_URL}"}]
        )


def test_unregistered_scheme_is_reported_clearly():
    registry = SecretResolverRegistry({"env": EnvSecretResolver({})})
    with pytest.raises(ValueError, match="No secret resolver registered for scheme 'vault'"):
        ConnectionRegistry.from_entries(
            [{"id": "demo", "dialect": "postgresql", "connection_string": "${vault:demo#url}"}],
            resolver_registry=registry,
        )
