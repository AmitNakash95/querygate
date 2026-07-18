"""Unit tests for pluggable secret resolution (querygate/secrets/resolvers.py)."""

from __future__ import annotations

import hvac.exceptions
import pytest

from querygate.core.config import AppConfig
from querygate.secrets.resolvers import (
    EnvSecretResolver,
    SecretResolverRegistry,
    VaultSecretResolver,
    build_secret_resolver_registry,
)


class _FakeKvV2:
    def __init__(self, secrets: dict[str, dict], *, raises: Exception | None = None) -> None:
        self._secrets = secrets
        self._raises = raises

    def read_secret_version(self, *, path: str, mount_point: str, raise_on_deleted_version=None):
        if self._raises is not None:
            raise self._raises
        if path not in self._secrets:
            raise hvac.exceptions.InvalidPath(f"no secret at {path!r}")
        return {"data": {"data": self._secrets[path]}}


class _FakeSecretsEngines:
    def __init__(self, kv_v2: _FakeKvV2) -> None:
        self.kv = type("_Kv", (), {"v2": kv_v2})()


class _FakeVaultClient:
    def __init__(self, secrets: dict[str, dict], *, raises: Exception | None = None) -> None:
        self.secrets = _FakeSecretsEngines(_FakeKvV2(secrets, raises=raises))


# --- EnvSecretResolver -------------------------------------------------


def test_env_resolver_resolves_from_provided_mapping():
    resolver = EnvSecretResolver({"MY_VAR": "resolved-value"})
    assert resolver.resolve("MY_VAR") == "resolved-value"


def test_env_resolver_raises_on_missing_var():
    resolver = EnvSecretResolver({})
    with pytest.raises(ValueError, match="MY_VAR"):
        resolver.resolve("MY_VAR")


def test_env_resolver_falls_back_to_process_environment(monkeypatch):
    monkeypatch.setenv("TEST_SECRETS_ENV_VAR", "from-os-environ")
    resolver = EnvSecretResolver()
    assert resolver.resolve("TEST_SECRETS_ENV_VAR") == "from-os-environ"


# --- VaultSecretResolver -------------------------------------------------


def test_vault_resolver_resolves_field_from_secret():
    client = _FakeVaultClient({"querygate/demo": {"connection_string": "postgresql://real"}})
    resolver = VaultSecretResolver(url="http://vault", token="t", client=client)
    assert resolver.resolve("querygate/demo#connection_string") == "postgresql://real"


def test_vault_resolver_uses_configured_mount_point():
    client = _FakeVaultClient({"demo": {"field": "value"}})
    resolver = VaultSecretResolver(
        url="http://vault", token="t", mount_point="custom-mount", client=client
    )
    assert resolver.resolve("demo#field") == "value"


def test_vault_resolver_rejects_reference_without_field():
    resolver = VaultSecretResolver(url="http://vault", token="t", client=_FakeVaultClient({}))
    with pytest.raises(ValueError, match="expected 'path#field'"):
        resolver.resolve("querygate/demo")


def test_vault_resolver_raises_on_missing_field():
    client = _FakeVaultClient({"querygate/demo": {"other_field": "x"}})
    resolver = VaultSecretResolver(url="http://vault", token="t", client=client)
    with pytest.raises(ValueError, match="no field 'connection_string'"):
        resolver.resolve("querygate/demo#connection_string")


def test_vault_resolver_wraps_vault_error_without_leaking_details():
    client = _FakeVaultClient({}, raises=hvac.exceptions.Forbidden("permission denied for token X"))
    resolver = VaultSecretResolver(url="http://vault", token="super-secret-token", client=client)
    with pytest.raises(ValueError) as exc_info:
        resolver.resolve("querygate/demo#connection_string")
    message = str(exc_info.value)
    assert "Forbidden" in message
    assert "super-secret-token" not in message
    assert "permission denied for token X" not in message


def test_vault_resolver_construction_does_not_require_network(monkeypatch):
    # hvac.Client() itself performs no network I/O — only an actual API call
    # (read_secret_version) does. Building the resolver against an
    # unreachable address must not raise.
    VaultSecretResolver(url="http://127.0.0.1:1", token="t")


# --- SecretResolverRegistry ----------------------------------------------


def test_registry_dispatches_bare_identifier_to_env_scheme():
    registry = SecretResolverRegistry({"env": EnvSecretResolver({"DB_URL": "postgres://x"})})
    assert registry.interpolate("${DB_URL}") == "postgres://x"


def test_registry_dispatches_scheme_prefixed_reference():
    client = _FakeVaultClient({"querygate/demo": {"connection_string": "postgres://vault"}})
    registry = SecretResolverRegistry(
        {
            "env": EnvSecretResolver({}),
            "vault": VaultSecretResolver(url="http://vault", token="t", client=client),
        }
    )
    assert registry.interpolate("${vault:querygate/demo#connection_string}") == "postgres://vault"


def test_registry_supports_multiple_references_in_one_value():
    registry = SecretResolverRegistry({"env": EnvSecretResolver({"HOST": "db", "PORT": "5432"})})
    assert registry.interpolate("postgresql://${HOST}:${PORT}/x") == "postgresql://db:5432/x"


def test_registry_raises_for_unregistered_scheme():
    registry = SecretResolverRegistry({"env": EnvSecretResolver({})})
    with pytest.raises(ValueError, match="No secret resolver registered for scheme 'vault'"):
        registry.interpolate("${vault:secret/path#field}")


def test_registry_raises_for_malformed_reference():
    registry = SecretResolverRegistry({"env": EnvSecretResolver({})})
    with pytest.raises(ValueError, match="Malformed secret reference"):
        registry.interpolate("${not a valid reference}")


# --- build_secret_resolver_registry --------------------------------------


def test_build_registry_registers_only_env_by_default():
    registry = build_secret_resolver_registry(AppConfig(environment="localhost", api_keys=["k"]))
    with pytest.raises(ValueError, match="No secret resolver registered for scheme 'vault'"):
        registry.interpolate("${vault:secret/path#field}")


def test_build_registry_registers_vault_when_enabled():
    cfg = AppConfig(
        environment="localhost",
        api_keys=["k"],
        vault_enabled=True,
        vault_addr="http://127.0.0.1:1",
        vault_token="t",
    )
    registry = build_secret_resolver_registry(cfg)
    # No real Vault reachable — dispatch succeeds up to the point of actually
    # calling out, proving "vault" is registered rather than raising the
    # "no resolver registered" error a missing registration would produce.
    with pytest.raises(ValueError, match="could not be read"):
        registry.interpolate("${vault:secret/path#field}")


def test_vault_enabled_without_addr_is_rejected():
    with pytest.raises(ValueError, match="VAULT_ADDR"):
        AppConfig(environment="localhost", api_keys=["k"], vault_enabled=True, vault_token="t")


def test_vault_enabled_without_token_is_rejected():
    with pytest.raises(ValueError, match="VAULT_TOKEN"):
        AppConfig(
            environment="localhost",
            api_keys=["k"],
            vault_enabled=True,
            vault_addr="http://127.0.0.1:8200",
        )
