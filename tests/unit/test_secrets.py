"""Unit tests for pluggable secret resolution (querygate/secrets/resolvers.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import hvac.exceptions
import pytest

from querygate.core.config import AppConfig
from querygate.secrets.resolvers import (
    EnvSecretResolver,
    LeasedSecretResolver,
    SecretResolverRegistry,
    VaultSecretResolver,
    build_secret_resolver_registry,
    iter_secret_references,
)


class _FakeKvV2:
    def __init__(
        self,
        secrets: dict[str, dict],
        *,
        raises: Exception | None = None,
        lease_duration: int = 0,
    ) -> None:
        self._secrets = secrets
        self._raises = raises
        self._lease_duration = lease_duration

    def read_secret_version(self, *, path: str, mount_point: str, raise_on_deleted_version=None):
        if self._raises is not None:
            raise self._raises
        if path not in self._secrets:
            raise hvac.exceptions.InvalidPath(f"no secret at {path!r}")
        return {
            "data": {"data": self._secrets[path]},
            "lease_duration": self._lease_duration,
        }


class _FakeSecretsEngines:
    def __init__(self, kv_v2: _FakeKvV2) -> None:
        self.kv = type("_Kv", (), {"v2": kv_v2})()


class _FakeVaultClient:
    def __init__(
        self,
        secrets: dict[str, dict],
        *,
        raises: Exception | None = None,
        lease_duration: int = 0,
    ) -> None:
        self.secrets = _FakeSecretsEngines(
            _FakeKvV2(secrets, raises=raises, lease_duration=lease_duration)
        )


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


def test_credential_lease_refresh_without_vault_is_rejected():
    with pytest.raises(ValueError, match="VAULT_ENABLED"):
        AppConfig(
            environment="localhost",
            api_keys=["k"],
            credential_lease_refresh_enabled=True,
        )


def test_credential_lease_refresh_with_vault_is_accepted():
    cfg = AppConfig(
        environment="localhost",
        api_keys=["k"],
        credential_lease_refresh_enabled=True,
        vault_enabled=True,
        vault_addr="http://127.0.0.1:8200",
        vault_token="t",
    )
    assert cfg.credential_lease_refresh_enabled is True


def test_credential_lease_check_interval_must_be_smaller_than_the_margin():
    # Polling less often than the margin means a lease can come due and
    # expire between two polls without the monitor ever seeing it inside
    # the margin window -- the whole feature would silently do nothing.
    with pytest.raises(ValueError, match="CREDENTIAL_LEASE_CHECK_INTERVAL_SECONDS"):
        AppConfig(
            environment="localhost",
            api_keys=["k"],
            credential_lease_refresh_enabled=True,
            vault_enabled=True,
            vault_addr="http://127.0.0.1:8200",
            vault_token="t",
            credential_lease_check_interval_seconds=600,
            credential_lease_refresh_margin_seconds=300,
        )


def test_credential_lease_check_interval_smaller_than_margin_is_accepted():
    cfg = AppConfig(
        environment="localhost",
        api_keys=["k"],
        credential_lease_refresh_enabled=True,
        vault_enabled=True,
        vault_addr="http://127.0.0.1:8200",
        vault_token="t",
        credential_lease_check_interval_seconds=60,
        credential_lease_refresh_margin_seconds=300,
    )
    assert cfg.credential_lease_check_interval_seconds == 60


# --- LeasedSecretResolver / VaultSecretResolver.lease_expiry -------------


def test_vault_lease_expiry_reports_no_expiry_for_a_static_secret():
    # KV v2's own response carries lease_duration: 0 for a static secret --
    # this must be reported as "no lease" (None), not a synthesized TTL.
    client = _FakeVaultClient({"querygate/demo": {"connection_string": "postgresql://real"}})
    resolver = VaultSecretResolver(url="http://vault", token="t", client=client)
    expires_at = resolver.lease_expiry("querygate/demo#connection_string")
    assert expires_at is None


def test_vault_lease_expiry_reports_a_real_expiry():
    client = _FakeVaultClient(
        {"querygate/demo": {"connection_string": "postgresql://real"}}, lease_duration=120
    )
    resolver = VaultSecretResolver(url="http://vault", token="t", client=client)
    before = datetime.now(timezone.utc)
    expires_at = resolver.lease_expiry("querygate/demo#connection_string")
    assert expires_at is not None
    assert before + timedelta(seconds=119) < expires_at <= before + timedelta(seconds=121)


def test_vault_lease_expiry_never_returns_the_resolved_value():
    # lease_expiry deliberately has no return-value slot for the secret --
    # a probe must not be the thing that hands out (or even fetches) the
    # value it's checking the expiry of. Assert the method signature/return
    # shape rather than assuming: it must be a bare Optional[datetime], not
    # a tuple a caller could misuse to also grab the value.
    client = _FakeVaultClient(
        {"querygate/demo": {"connection_string": "postgresql://real"}}, lease_duration=120
    )
    resolver = VaultSecretResolver(url="http://vault", token="t", client=client)
    result = resolver.lease_expiry("querygate/demo#connection_string")
    assert isinstance(result, datetime)


def test_vault_lease_expiry_caps_an_absurd_lease_duration():
    # A lease_duration large enough would otherwise raise OverflowError from
    # `datetime + timedelta(seconds=huge)`, which is not a ValueError and
    # would escape SecretResolverRegistry.soonest_lease_expiry's
    # deliberately narrow `except ValueError` (see its docstring) and abort
    # the whole scan. The value is capped instead, so this never raises.
    client = _FakeVaultClient(
        {"querygate/demo": {"connection_string": "postgresql://real"}},
        lease_duration=10**20,
    )
    resolver = VaultSecretResolver(url="http://vault", token="t", client=client)
    expires_at = resolver.lease_expiry("querygate/demo#connection_string")
    assert expires_at is not None
    assert expires_at < datetime.now(timezone.utc) + timedelta(days=401)


def test_vault_lease_expiry_raises_on_missing_field():
    client = _FakeVaultClient({"querygate/demo": {"other_field": "x"}})
    resolver = VaultSecretResolver(url="http://vault", token="t", client=client)
    with pytest.raises(ValueError, match="no field 'connection_string'"):
        resolver.lease_expiry("querygate/demo#connection_string")


def test_vault_lease_expiry_wraps_vault_error_without_leaking_details():
    client = _FakeVaultClient({}, raises=hvac.exceptions.Forbidden("permission denied for token X"))
    resolver = VaultSecretResolver(url="http://vault", token="super-secret-token", client=client)
    with pytest.raises(ValueError) as exc_info:
        resolver.lease_expiry("querygate/demo#connection_string")
    message = str(exc_info.value)
    assert "Forbidden" in message
    assert "super-secret-token" not in message


def test_env_resolver_is_not_a_leased_secret_resolver():
    # EnvSecretResolver deliberately has no lease_expiry -- isinstance
    # against the runtime_checkable Protocol is how SecretResolverRegistry
    # probes for the optional capability without SecretResolver itself
    # growing a lease-related method every backend must implement.
    assert not isinstance(EnvSecretResolver({}), LeasedSecretResolver)


def test_vault_resolver_is_a_leased_secret_resolver():
    assert isinstance(VaultSecretResolver(url="http://v", token="t"), LeasedSecretResolver)


# --- iter_secret_references ------------------------------------------------


def test_iter_secret_references_splits_env_and_scheme_prefixed_references():
    refs = list(
        iter_secret_references(
            "postgresql://${DB_USER}@host/db?token=${vault:querygate/demo#password}"
        )
    )
    assert refs == [("env", "DB_USER"), ("vault", "querygate/demo#password")]


def test_iter_secret_references_skips_a_reference_it_cannot_split():
    # A body with no scheme separator and not a bare identifier (whitespace)
    # is exactly what interpolate() itself would reject as malformed -- this
    # helper is a best-effort scan for the lease monitor, not a validator,
    # so it silently skips rather than raising.
    refs = list(iter_secret_references("${not a valid reference}"))
    assert refs == []


# --- SecretResolverRegistry.soonest_lease_expiry ---------------------------


def test_soonest_lease_expiry_is_none_when_no_leased_resolver_is_registered():
    registry = SecretResolverRegistry({"env": EnvSecretResolver({"DB_URL": "postgres://x"})})
    assert registry.soonest_lease_expiry("${DB_URL}") is None


def test_soonest_lease_expiry_is_none_for_a_leased_resolver_with_no_active_lease():
    client = _FakeVaultClient({"demo": {"field": "value"}})  # lease_duration=0
    registry = SecretResolverRegistry(
        {
            "env": EnvSecretResolver({}),
            "vault": VaultSecretResolver(url="http://v", token="t", client=client),
        }
    )
    assert registry.soonest_lease_expiry("${vault:demo#field}") is None


def test_soonest_lease_expiry_reports_the_earliest_of_several_leased_references():
    soon_client = _FakeVaultClient({"soon": {"field": "a"}}, lease_duration=60)
    late_client = _FakeVaultClient({"late": {"field": "b"}}, lease_duration=3600)
    registry = SecretResolverRegistry(
        {
            "soon": VaultSecretResolver(url="http://v", token="t", client=soon_client),
            "late": VaultSecretResolver(url="http://v", token="t", client=late_client),
        }
    )
    text = "${soon:soon#field} and ${late:late#field}"
    expiry = registry.soonest_lease_expiry(text)
    assert expiry is not None
    assert expiry <= datetime.now(timezone.utc) + timedelta(seconds=61)


def test_soonest_lease_expiry_skips_a_reference_whose_probe_fails():
    # One reference erroring (a rotated-out path, a transient outage) must
    # not hide a different reference's real, soon expiry.
    failing_client = _FakeVaultClient({}, raises=hvac.exceptions.Forbidden("denied"))
    leased_client = _FakeVaultClient({"demo": {"field": "value"}}, lease_duration=60)
    registry = SecretResolverRegistry(
        {
            "failing": VaultSecretResolver(url="http://v", token="t", client=failing_client),
            "vault": VaultSecretResolver(url="http://v", token="t", client=leased_client),
        }
    )
    text = "${failing:missing#field} and ${vault:demo#field}"
    expiry = registry.soonest_lease_expiry(text)
    assert expiry is not None
    assert expiry <= datetime.now(timezone.utc) + timedelta(seconds=61)


class _BuggyLeasedResolver:
    """A LeasedSecretResolver that violates its own documented contract by
    raising something other than ValueError -- used to prove
    soonest_lease_expiry's `except ValueError` narrowing is deliberate, not
    accidentally too broad: a real bug in a resolver implementation must
    surface (to the monitor's own iteration-level catch-all), not be
    silently treated as "no lease" the way an expected ValueError is.
    """

    def resolve(self, reference: str) -> str:
        raise AssertionError("not exercised")

    def lease_expiry(self, reference: str) -> None:
        raise RuntimeError("not the documented failure mode")


def test_soonest_lease_expiry_does_not_swallow_a_non_value_error_from_a_resolver():
    registry = SecretResolverRegistry({"buggy": _BuggyLeasedResolver()})
    with pytest.raises(RuntimeError, match="not the documented failure mode"):
        registry.soonest_lease_expiry("${buggy:demo#field}")


def test_soonest_lease_expiry_never_calls_lease_expiry_on_a_non_leased_resolver():
    # The isinstance(resolver, LeasedSecretResolver) guard is what makes
    # this safe for EnvSecretResolver (which has no lease_expiry at all) --
    # assert directly that a non-implementing resolver is never probed,
    # rather than only inferring it from the absence of an error.
    calls: list[str] = []

    class _TrackingEnvResolver(EnvSecretResolver):
        def resolve(self, reference: str) -> str:
            calls.append(reference)
            return super().resolve(reference)

    registry = SecretResolverRegistry({"env": _TrackingEnvResolver({"DB_URL": "postgres://x"})})
    assert registry.soonest_lease_expiry("${DB_URL}") is None
    assert calls == []
