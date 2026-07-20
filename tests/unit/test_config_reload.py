"""Unit tests for hot-reloading connections.yaml/policy.yaml (querygate/config_reload.py)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from querygate import config_reload as reload_module
from querygate.catalog.loader import get_catalog_store
from querygate.catalog.repository import catalog_process_lock
from querygate.config_reload import reload_config
from querygate.connections.registry import get_registry
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
