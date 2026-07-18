"""Unit tests for hot-reloading connections.yaml/policy.yaml (querygate/config_reload.py)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from querygate import config_reload as reload_module
from querygate.config_reload import reload_config
from querygate.connections.registry import get_registry
from querygate.execution.concurrency import SEMAPHORES
from querygate.policy.loader import get_policy


def _write(tmp_path: Path, name: str, content: str) -> str:
    path = tmp_path / name
    path.write_text(content)
    return str(path)


_POLICY_YAML = "default:\n  enabled: true\n"


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
    SEMAPHORES["demo"] = object()  # sentinel standing in for a real Semaphore
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

    assert "demo" not in SEMAPHORES


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
