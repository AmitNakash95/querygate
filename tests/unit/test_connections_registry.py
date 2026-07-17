"""Unit tests for the file-driven connection registry."""

from __future__ import annotations

import pytest

from querygate.connections.registry import ConnectionRegistry


def test_env_var_interpolation(monkeypatch):
    monkeypatch.setenv("TEST_DB_URL", "postgresql+asyncpg://user:pass@host/db")
    registry = ConnectionRegistry.from_entries(
        [{"id": "demo", "dialect": "postgresql", "connection_string": "${TEST_DB_URL}"}]
    )
    assert registry.get("demo").connection_string == "postgresql+asyncpg://user:pass@host/db"


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
