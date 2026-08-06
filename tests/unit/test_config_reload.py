"""Unit tests for hot-reloading connections.yaml/policy.yaml (querygate/config_reload.py)."""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, Mock, patch

import pytest

from querygate import config_reload as reload_module
from querygate.admin.store import get_config_version_store
from querygate.catalog.loader import get_catalog_store
from querygate.catalog.repository import catalog_process_lock
from querygate.config_reload import CredentialLeaseMonitor, ReloadResult, reload_config
from querygate.connections.registry import get_registry
from querygate.core.config import AppConfig
from querygate.execution.concurrency import in_process_limiter
from querygate.policy.loader import get_policy
from querygate.secrets.resolvers import EnvSecretResolver, SecretResolverRegistry


def _write(tmp_path: Path, name: str, content: str) -> str:
    path = tmp_path / name
    path.write_text(content)
    return str(path)


_POLICY_YAML = "default:\n  enabled: true\n"


@pytest.mark.asyncio
async def test_reload_waits_for_an_in_process_catalog_refresh_transaction(tmp_path):
    connections_file = _write(
        tmp_path,
        "connections.yaml",
        """
connections:
  - id: demo
    dialect: postgresql
    connection_string: postgresql+asyncpg://user:pass@localhost/demo
""",
    )
    policy_file = _write(tmp_path, "policy.yaml", _POLICY_YAML)
    lock = catalog_process_lock()
    await lock.acquire()
    try:
        with patch.object(reload_module, "dispose_engine", new_callable=AsyncMock):
            task = asyncio.create_task(
                reload_config(
                    connections_file=connections_file,
                    policy_file=policy_file,
                )
            )
            await asyncio.sleep(0.01)
            assert not task.done()
    finally:
        lock.release()

    await task


@pytest.mark.asyncio
async def test_reload_swaps_registry_and_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_RELOAD_URL", "postgresql+asyncpg://user:pass@localhost/x")
    connections_file = _write(
        tmp_path,
        "connections.yaml",
        """
connections:
  - id: fresh
    dialect: postgresql
    connection_string: ${TEST_RELOAD_URL}
""",
    )
    policy_file = _write(tmp_path, "policy.yaml", _POLICY_YAML)

    with patch.object(reload_module, "dispose_engine", new_callable=AsyncMock):
        result = await reload_config(connections_file=connections_file, policy_file=policy_file)

    assert result.connection_ids == ["fresh"]
    assert get_registry().all_ids() == ["fresh"]
    assert get_policy("fresh").enabled is True


@pytest.mark.asyncio
async def test_reload_disposes_engine_for_removed_connection(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_RELOAD_URL", "postgresql+asyncpg://user:pass@localhost/x")
    # conftest's autouse fixture seeds a registry with "demo"; the new file
    # doesn't mention it at all, so it should be disposed as removed.
    connections_file = _write(
        tmp_path,
        "connections.yaml",
        """
connections:
  - id: fresh
    dialect: postgresql
    connection_string: ${TEST_RELOAD_URL}
""",
    )
    policy_file = _write(tmp_path, "policy.yaml", _POLICY_YAML)

    with patch.object(reload_module, "dispose_engine", new_callable=AsyncMock) as mock_dispose:
        result = await reload_config(connections_file=connections_file, policy_file=policy_file)

    mock_dispose.assert_awaited_once_with("demo")
    assert result.disposed_connections == ["demo"]


@pytest.mark.asyncio
async def test_reload_disposes_engine_when_connection_string_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_RELOAD_URL", "postgresql+asyncpg://user:pass@localhost/changed")
    connections_file = _write(
        tmp_path,
        "connections.yaml",
        """
connections:
  - id: demo
    dialect: postgresql
    connection_string: ${TEST_RELOAD_URL}
""",
    )
    policy_file = _write(tmp_path, "policy.yaml", _POLICY_YAML)

    with patch.object(reload_module, "dispose_engine", new_callable=AsyncMock) as mock_dispose:
        result = await reload_config(connections_file=connections_file, policy_file=policy_file)

    mock_dispose.assert_awaited_once_with("demo")
    assert result.disposed_connections == ["demo"]


@pytest.mark.asyncio
async def test_reload_keeps_engine_for_unchanged_connection(tmp_path):
    # conftest's demo registry uses this exact connection string — see
    # tests/conftest.py's make_demo_registry.
    connections_file = _write(
        tmp_path,
        "connections.yaml",
        """
connections:
  - id: demo
    dialect: postgresql
    connection_string: postgresql+asyncpg://user:pass@localhost/demo
""",
    )
    policy_file = _write(tmp_path, "policy.yaml", _POLICY_YAML)

    with patch.object(reload_module, "dispose_engine", new_callable=AsyncMock) as mock_dispose:
        result = await reload_config(connections_file=connections_file, policy_file=policy_file)

    mock_dispose.assert_not_awaited()
    assert result.disposed_connections == []


@pytest.mark.asyncio
async def test_reload_resets_concurrency_semaphores(tmp_path):
    # A real semaphore, captured by identity — proves reload actually forces
    # a fresh one rather than just checking a key was removed.
    before = in_process_limiter().semaphore("demo", 1)
    connections_file = _write(
        tmp_path,
        "connections.yaml",
        """
connections:
  - id: demo
    dialect: postgresql
    connection_string: postgresql+asyncpg://user:pass@localhost/demo
""",
    )
    policy_file = _write(tmp_path, "policy.yaml", _POLICY_YAML)

    with patch.object(reload_module, "dispose_engine", new_callable=AsyncMock):
        await reload_config(connections_file=connections_file, policy_file=policy_file)

    after = in_process_limiter().semaphore("demo", 1)
    assert after is not before


@pytest.mark.asyncio
async def test_reload_reports_policy_connection_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_RELOAD_URL", "postgresql+asyncpg://user:pass@localhost/x")
    connections_file = _write(
        tmp_path,
        "connections.yaml",
        """
connections:
  - id: fresh
    dialect: postgresql
    connection_string: ${TEST_RELOAD_URL}
""",
    )
    policy_file = _write(
        tmp_path,
        "policy.yaml",
        """
default:
  enabled: true

connections:
  fresh:
    max_joins: 2
""",
    )

    with patch.object(reload_module, "dispose_engine", new_callable=AsyncMock):
        result = await reload_config(connections_file=connections_file, policy_file=policy_file)

    assert result.policy_connection_overrides == ["fresh"]


@pytest.mark.asyncio
async def test_reload_with_no_catalog_file_yields_empty_catalog_store(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_RELOAD_URL", "postgresql+asyncpg://user:pass@localhost/x")
    connections_file = _write(
        tmp_path,
        "connections.yaml",
        """
connections:
  - id: fresh
    dialect: postgresql
    connection_string: ${TEST_RELOAD_URL}
""",
    )
    policy_file = _write(tmp_path, "policy.yaml", _POLICY_YAML)

    with patch.object(reload_module, "dispose_engine", new_callable=AsyncMock):
        result = await reload_config(
            connections_file=connections_file, policy_file=policy_file, catalog_file=None
        )

    assert result.catalog_connection_ids == []
    assert get_catalog_store().get_table("fresh", "anything") is None


@pytest.mark.asyncio
async def test_reload_swaps_in_new_catalog_store(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_RELOAD_URL", "postgresql+asyncpg://user:pass@localhost/x")
    connections_file = _write(
        tmp_path,
        "connections.yaml",
        """
connections:
  - id: fresh
    dialect: postgresql
    connection_string: ${TEST_RELOAD_URL}
""",
    )
    policy_file = _write(tmp_path, "policy.yaml", _POLICY_YAML)
    catalog_file = _write(
        tmp_path,
        "catalog.yaml",
        """
connections:
  fresh:
    tables:
      widgets:
        description: "One row per widget."
""",
    )

    with patch.object(reload_module, "dispose_engine", new_callable=AsyncMock):
        result = await reload_config(
            connections_file=connections_file, policy_file=policy_file, catalog_file=catalog_file
        )

    assert result.catalog_connection_ids == ["fresh"]
    entry = get_catalog_store().get_table("fresh", "widgets")
    assert entry is not None
    assert entry.description == "One row per widget."


class _FakeResolver:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def resolve(self, reference: str) -> str:
        return self._values[reference]


@pytest.mark.asyncio
async def test_reload_resolves_connection_strings_through_custom_resolver_registry(tmp_path):
    connections_file = _write(
        tmp_path,
        "connections.yaml",
        """
connections:
  - id: fresh
    dialect: postgresql
    connection_string: ${vault:fresh#url}
""",
    )
    policy_file = _write(tmp_path, "policy.yaml", _POLICY_YAML)
    resolver_registry = SecretResolverRegistry(
        {
            "env": EnvSecretResolver({}),
            "vault": _FakeResolver({"fresh#url": "postgresql+asyncpg://vault-resolved/db"}),
        }
    )

    with patch.object(reload_module, "dispose_engine", new_callable=AsyncMock):
        await reload_config(
            connections_file=connections_file,
            policy_file=policy_file,
            resolver_registry=resolver_registry,
        )

    assert get_registry().get("fresh").connection_string == "postgresql+asyncpg://vault-resolved/db"


@pytest.mark.asyncio
async def test_reload_without_resolver_registry_only_resolves_env_references(tmp_path):
    connections_file = _write(
        tmp_path,
        "connections.yaml",
        """
connections:
  - id: fresh
    dialect: postgresql
    connection_string: ${vault:fresh#url}
""",
    )
    policy_file = _write(tmp_path, "policy.yaml", _POLICY_YAML)

    with patch.object(reload_module, "dispose_engine", new_callable=AsyncMock):
        with pytest.raises(ValueError, match="No secret resolver registered for scheme 'vault'"):
            await reload_config(connections_file=connections_file, policy_file=policy_file)


# --- CredentialLeaseMonitor (TODO.md item 135) ------------------------------


class _StubLeaseRegistry:
    """A minimal stand-in for SecretResolverRegistry that only implements
    the one method CredentialLeaseMonitor actually calls, so these tests
    exercise the monitor's trigger/catch-all logic in isolation from real
    resolver dispatch (already covered by tests/unit/test_secrets.py).
    """

    def __init__(self, expiries) -> None:
        self._expiries = list(expiries)
        self.calls = 0
        self.call_thread_idents: list[int] = []
        self.last_text: Optional[str] = None

    def soonest_lease_expiry(self, text: str):
        self.calls += 1
        self.last_text = text
        self.call_thread_idents.append(threading.get_ident())
        if not self._expiries:
            return None
        value = self._expiries.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class _StubTriggerRegistry:
    """Unlike `_StubLeaseRegistry`, this one also implements `interpolate`
    (delegating to a real env-only `SecretResolverRegistry` -- the tests
    using this stub use plain literal connection strings, no `${...}`
    reference to resolve) so it's safe to pass to a *real* `reload_config()`
    call, not just a mocked one -- needed for tests that assert on the
    actual post-reload registry contents rather than only on whether
    `reload_config` was called.
    """

    def __init__(self, expiry) -> None:
        self._real = SecretResolverRegistry({"env": EnvSecretResolver({})})
        self._expiry = expiry

    def interpolate(self, value: str) -> str:
        return self._real.interpolate(value)

    def soonest_lease_expiry(self, text: str):
        return self._expiry


class _RepeatingLeaseRegistry:
    """Always reports the same fixed expiry -- models a real dynamic
    credential whose lease TTL is shorter than the configured refresh
    margin, the normal case the hysteresis guard (`_last_triggered_expiry`)
    exists for.
    """

    def __init__(self, expiry: datetime) -> None:
        self._expiry = expiry
        self.calls = 0

    def soonest_lease_expiry(self, text: str):
        self.calls += 1
        return self._expiry


def _cfg(tmp_path: Path, connections_yaml: Optional[str] = None, **overrides) -> AppConfig:
    connections_file = _write(
        tmp_path,
        "connections.yaml",
        connections_yaml or """
connections:
  - id: demo
    dialect: postgresql
    connection_string: ${vault:demo#connection_string}
""",
    )
    policy_file = _write(tmp_path, "policy.yaml", _POLICY_YAML)
    kwargs = dict(
        environment="localhost",
        api_keys=["k"],
        connections_file=connections_file,
        policy_file=policy_file,
        vault_enabled=True,
        vault_addr="http://vault.internal:8200",
        vault_token="t",
    )
    kwargs.update(overrides)
    return AppConfig(**kwargs)


def _monitor(tmp_path: Path, resolver_registry_factory, **overrides) -> CredentialLeaseMonitor:
    cfg = overrides.pop("cfg", None) or _cfg(tmp_path)
    kwargs = dict(
        cfg=cfg,
        poll_interval_seconds=0.01,
        refresh_margin_seconds=60,
        resolver_registry_factory=resolver_registry_factory,
    )
    kwargs.update(overrides)
    return CredentialLeaseMonitor(**kwargs)


def test_connection_string_reference_text_ignores_a_reference_outside_connection_string(
    tmp_path,
):
    # QG135-06: a ${...}-shaped reference sitting in a comment or an
    # unrelated field is never resolved by a real reload_config()
    # (interpolate() is only ever called against each entry's
    # connection_string), so probing it for a lease would be reacting to
    # something reload never actually reads.
    connections_file = _write(
        tmp_path,
        "connections.yaml",
        """
# a stray reference in a comment: ${vault:not-a-real-connection#field}
connections:
  - id: demo
    dialect: postgresql
    connection_string: postgresql+asyncpg://user:pass@localhost/demo
    description: another stray one -- ${vault:also-not-real#field}
""",
    )
    text = reload_module._connection_string_reference_text(connections_file)
    assert "not-a-real-connection" not in text
    assert "also-not-real" not in text


@pytest.mark.asyncio
async def test_lease_monitor_does_not_probe_a_reference_outside_connection_string(tmp_path):
    cfg = _cfg(
        tmp_path,
        connections_yaml="""
# a stray reference in a comment, matching a scheme the registry has
# registered, with a lease it WOULD report if it were ever probed:
# ${vault:stray#field}
connections:
  - id: demo
    dialect: postgresql
    connection_string: postgresql+asyncpg://user:pass@localhost/demo
""",
    )
    registry = _StubLeaseRegistry([datetime.now(timezone.utc) + timedelta(seconds=1)])
    monitor = CredentialLeaseMonitor(
        cfg=cfg,
        poll_interval_seconds=0.01,
        refresh_margin_seconds=60,
        resolver_registry_factory=lambda: registry,
    )

    monitor._evaluate()
    assert registry.calls == 1
    assert "stray" not in registry.last_text


@pytest.mark.asyncio
async def test_lease_monitor_triggers_reload_when_expiry_is_within_the_margin(tmp_path):
    registry = _StubLeaseRegistry([datetime.now(timezone.utc) + timedelta(seconds=1)])
    monitor = _monitor(tmp_path, lambda: registry)
    fake_result = ReloadResult(
        connection_ids=["demo"],
        policy_connection_overrides=[],
        catalog_connection_ids=[],
        template_ids=[],
        disposed_connections=["demo"],
    )
    with patch.object(
        reload_module, "reload_config", new=AsyncMock(return_value=fake_result)
    ) as mock_reload:
        await monitor.start()
        await asyncio.sleep(0.05)
        await monitor.stop()

    assert mock_reload.await_count >= 1
    _, kwargs = mock_reload.call_args
    assert kwargs["connections_file"] == monitor._cfg.connections_file
    assert kwargs["resolver_registry"] is registry


@pytest.mark.asyncio
async def test_lease_monitor_records_an_audit_event_when_it_triggers(tmp_path):
    # QG135-01: an automatic change this monitor makes must be attributable,
    # the same way any other config-governance action is -- not just logged
    # via loguru.
    registry = _StubLeaseRegistry([datetime.now(timezone.utc) + timedelta(seconds=1)])
    monitor = _monitor(tmp_path, lambda: registry)
    fake_result = ReloadResult(
        connection_ids=["demo"],
        policy_connection_overrides=[],
        catalog_connection_ids=[],
        template_ids=[],
        disposed_connections=[],
    )
    with (
        patch.object(reload_module, "reload_config", new=AsyncMock(return_value=fake_result)),
        patch("querygate.audit.logger.audit_config_change", new=Mock()) as mock_audit,
    ):
        await monitor.start()
        await asyncio.sleep(0.05)
        await monitor.stop()

    mock_audit.assert_called_once()
    _, kwargs = mock_audit.call_args
    assert kwargs["action"] == "lease_refresh"
    assert kwargs["outcome"] == "success"
    assert kwargs["principal"] == "system:credential-lease-monitor"


@pytest.mark.asyncio
async def test_lease_monitor_does_not_reload_when_no_lease_is_reported(tmp_path):
    # Mirrors an env-only / static-secret deployment: soonest_lease_expiry
    # always reports None, so the monitor must never call reload_config --
    # it stays reachable only through the operator-pull path.
    registry = _StubLeaseRegistry([None, None, None])
    monitor = _monitor(tmp_path, lambda: registry)

    with patch.object(reload_module, "reload_config", new=AsyncMock()) as mock_reload:
        await monitor.start()
        await asyncio.sleep(0.05)
        await monitor.stop()

    mock_reload.assert_not_awaited()
    assert registry.calls >= 1


@pytest.mark.asyncio
async def test_lease_monitor_does_not_reload_on_every_poll_while_a_lease_stays_due(tmp_path):
    # QG135-04: a real dynamic credential's TTL is normally *shorter* than
    # the refresh margin -- without hysteresis, every single poll would see
    # the same still-due expiry and reload again, turning a documented
    # "reloads are rare" tradeoff into a routine one (resetting every
    # connection's concurrency semaphore on every poll).
    fixed_expiry = datetime.now(timezone.utc) + timedelta(seconds=1)
    registry = _RepeatingLeaseRegistry(fixed_expiry)
    monitor = _monitor(tmp_path, lambda: registry, poll_interval_seconds=0.01)
    fake_result = ReloadResult(
        connection_ids=["demo"],
        policy_connection_overrides=[],
        catalog_connection_ids=[],
        template_ids=[],
        disposed_connections=[],
    )

    with patch.object(
        reload_module, "reload_config", new=AsyncMock(return_value=fake_result)
    ) as mock_reload:
        await monitor.start()
        await asyncio.sleep(0.08)  # several poll intervals
        await monitor.stop()

    assert registry.calls >= 3  # the loop really did poll repeatedly
    assert mock_reload.await_count == 1  # but only triggered once


@pytest.mark.asyncio
async def test_lease_monitor_is_unaffected_by_a_non_leased_resolver(tmp_path):
    # A real registry with only the always-registered env resolver -- no
    # LeasedSecretResolver at all. The monitor must probe it without error
    # and simply find nothing to act on.
    cfg = _cfg(
        tmp_path,
        connections_yaml="""
connections:
  - id: demo
    dialect: postgresql
    connection_string: ${DEMO_URL}
""",
        vault_enabled=False,
        vault_addr="",
        vault_token="",
    )
    registry = SecretResolverRegistry({"env": EnvSecretResolver({"DEMO_URL": "postgres://x"})})
    monitor = CredentialLeaseMonitor(
        cfg=cfg,
        poll_interval_seconds=0.01,
        refresh_margin_seconds=60,
        resolver_registry_factory=lambda: registry,
    )

    assert monitor._evaluate().due is False

    with patch.object(reload_module, "reload_config", new=AsyncMock()) as mock_reload:
        await monitor.start()
        await asyncio.sleep(0.03)
        await monitor.stop()

    mock_reload.assert_not_awaited()


@pytest.mark.asyncio
async def test_lease_monitor_builds_a_fresh_resolver_registry_every_poll(tmp_path):
    # QG135-07: a resolver registry pinned once at construction would keep
    # using a stale VAULT_TOKEN forever, silently failing every poll once
    # it rotated -- resolver_registry_factory must be called fresh each
    # iteration, mirroring how /admin/reload-config and config-governance
    # apply() both already rebuild their registry per call.
    built = []

    def factory():
        registry = _StubLeaseRegistry([None])
        built.append(registry)
        return registry

    monitor = _monitor(tmp_path, factory, poll_interval_seconds=0.01)

    with patch.object(reload_module, "reload_config", new=AsyncMock()):
        await monitor.start()
        await asyncio.sleep(0.05)
        await monitor.stop()

    # Several distinct registry instances, not the same one reused --
    # proves the factory is actually re-invoked, not cached at __init__.
    assert len(built) >= 3
    assert len(set(id(r) for r in built)) == len(built)


@pytest.mark.asyncio
async def test_lease_monitor_iteration_failure_does_not_kill_the_background_loop(tmp_path):
    # Mirrors CatalogRefreshMonitor's/CatalogUsageLearningMonitor's catch-all
    # around each iteration: the first poll's probe raises, later polls must
    # still run rather than the background task silently dying.
    registry = _StubLeaseRegistry([RuntimeError("transient vault error"), None, None, None])
    monitor = _monitor(tmp_path, lambda: registry)

    with patch.object(reload_module, "reload_config", new=AsyncMock()) as mock_reload:
        await monitor.start()
        await asyncio.sleep(0.08)
        await monitor.stop()

    # The loop must have survived the first (raising) iteration and polled
    # again -- proving one failure doesn't wedge or kill the background task.
    assert registry.calls >= 2
    mock_reload.assert_not_awaited()


@pytest.mark.asyncio
async def test_lease_monitor_stop_is_idempotent_and_cancels_cleanly(tmp_path):
    registry = _StubLeaseRegistry([None] * 10)
    monitor = _monitor(tmp_path, lambda: registry, poll_interval_seconds=5)

    await monitor.start()
    await monitor.stop()
    await monitor.stop()  # must not raise or hang

    assert monitor._task is None


@pytest.mark.asyncio
async def test_lease_monitor_offloads_probing_to_a_worker_thread(tmp_path):
    # QG135-02: probing a leased resolver can be a real, slow network call
    # (Vault). Run on the event loop directly, it would stall every other
    # concurrent request this process is serving for the duration of the
    # call -- _evaluate() must run via asyncio.to_thread, not inline in _run.
    registry = _StubLeaseRegistry([None])
    monitor = _monitor(tmp_path, lambda: registry, poll_interval_seconds=5)
    main_thread_ident = threading.get_ident()

    with patch.object(reload_module, "reload_config", new=AsyncMock()):
        await monitor.start()
        await asyncio.sleep(0.03)
        await monitor.stop()

    assert registry.call_thread_idents  # the probe actually ran
    assert all(ident != main_thread_ident for ident in registry.call_thread_idents)


@pytest.mark.asyncio
async def test_lease_monitor_prefers_the_active_governed_config_version_over_plain_disk_files(
    tmp_path,
):
    # QG135-01 -- the HIGH-severity post-ship finding: a deployment using
    # config governance (admin/service.py's apply()/rollback()) makes a
    # staged, approved version's own on-disk files the live truth and never
    # writes that content back to AppConfig.connections_file. A monitor that
    # always reloaded from the plain cfg.* paths would silently revert an
    # approved policy tightening back to stale disk content the moment a
    # lease came due. This must not happen: the monitor must resolve the
    # *governed* files when a version is active.
    plain_connections_file = _write(
        tmp_path,
        "plain_connections.yaml",
        """
connections:
  - id: disk-stale
    dialect: postgresql
    connection_string: postgresql+asyncpg://user:pass@localhost/stale
""",
    )
    plain_policy_file = _write(tmp_path, "plain_policy.yaml", _POLICY_YAML)
    cfg = AppConfig(
        environment="localhost",
        api_keys=["k"],
        connections_file=plain_connections_file,
        policy_file=plain_policy_file,
        vault_enabled=True,
        vault_addr="http://vault.internal:8200",
        vault_token="t",
    )

    store = get_config_version_store()
    version = store.bootstrap_if_empty(
        connections_yaml="""
connections:
  - id: governed-approved
    dialect: postgresql
    connection_string: postgresql+asyncpg://user:pass@localhost/governed
""",
        policy_yaml=_POLICY_YAML,
        catalog_yaml=None,
        templates_yaml=None,
    )
    store.mark_active(version.id, actor="tester")

    registry = _StubTriggerRegistry(datetime.now(timezone.utc) + timedelta(seconds=1))
    monitor = CredentialLeaseMonitor(
        cfg=cfg,
        poll_interval_seconds=0.01,
        refresh_margin_seconds=60,
        resolver_registry_factory=lambda: registry,
    )

    with patch.object(reload_module, "dispose_engine", new_callable=AsyncMock):
        await monitor.start()
        await asyncio.sleep(0.05)
        await monitor.stop()

    assert get_registry().all_ids() == ["governed-approved"]
    assert "disk-stale" not in get_registry().all_ids()


@pytest.mark.asyncio
async def test_lease_monitor_falls_back_to_plain_cfg_files_when_governance_never_used(tmp_path):
    # The complementary case to the governed-version test above: when
    # governance's active-version pointer has never been set (the common
    # case -- a deployment that only ever uses plain reload-config), the
    # monitor must behave exactly as before this fix.
    cfg = _cfg(
        tmp_path,
        connections_yaml="""
connections:
  - id: demo
    dialect: postgresql
    connection_string: postgresql+asyncpg://user:pass@localhost/demo
""",
    )
    assert get_config_version_store().get_active_version_id() is None

    registry = _StubTriggerRegistry(datetime.now(timezone.utc) + timedelta(seconds=1))
    monitor = CredentialLeaseMonitor(
        cfg=cfg,
        poll_interval_seconds=0.01,
        refresh_margin_seconds=60,
        resolver_registry_factory=lambda: registry,
    )

    with patch.object(reload_module, "dispose_engine", new_callable=AsyncMock):
        await monitor.start()
        await asyncio.sleep(0.05)
        await monitor.stop()

    assert get_registry().all_ids() == ["demo"]


@pytest.mark.asyncio
async def test_app_lifespan_starts_credential_lease_monitor_when_enabled(tmp_path):
    # Closes a pre-existing gap this item's own security review flagged
    # (shared with the other two background monitors, but worth closing
    # here since it backs a stated security default): the off-by-default
    # gate itself had no direct regression coverage.
    from querygate.api.app import create_app

    # No ${vault:...} reference here -- this test only asserts the monitor
    # is constructed/started, not that a poll iteration completes; a plain
    # literal connection string means even a stray first iteration (the
    # loop's body runs once immediately on start(), before its first
    # interval wait) finds nothing to probe and makes no network call.
    cfg = _cfg(
        tmp_path,
        connections_yaml="""
connections:
  - id: demo
    dialect: postgresql
    connection_string: postgresql+asyncpg://user:pass@localhost/demo
""",
        mcp_enabled=False,
        concurrency_backend="in_process",
    )
    enabled_cfg = cfg.model_copy(
        update={
            "credential_lease_refresh_enabled": True,
            "credential_lease_check_interval_seconds": 60,
            "credential_lease_refresh_margin_seconds": 300,
        }
    )
    app = create_app(enabled_cfg)

    with patch("querygate.health._ping", new_callable=AsyncMock):
        async with app.router.lifespan_context(app):
            monitor = app.state.credential_lease_monitor
            assert isinstance(monitor, CredentialLeaseMonitor)
            assert monitor._task is not None


@pytest.mark.asyncio
async def test_app_lifespan_leaves_credential_lease_monitor_unset_by_default(tmp_path):
    from querygate.api.app import create_app

    cfg = _cfg(tmp_path, mcp_enabled=False, concurrency_backend="in_process")
    assert cfg.credential_lease_refresh_enabled is False
    app = create_app(cfg)

    with patch("querygate.health._ping", new_callable=AsyncMock):
        async with app.router.lifespan_context(app):
            assert app.state.credential_lease_monitor is None
