"""File-driven connection profile registry.

Connection strings are resolved via ${ENV_VAR} interpolation, so the
connections file itself never needs to contain a secret — only the name of
the environment variable that holds it. This replaces the old hardcoded
`DataBase` enum with a dynamic, deployment-specific registry (goal: "support
dynamic database connection profiles instead of hardcoded enum members").
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

import yaml

from querygate.connections.models import ConnectionProfile, PublicConnectionInfo

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate_env(value: str) -> str:
    def _replace(match: "re.Match[str]") -> str:
        var_name = match.group(1)
        resolved = os.environ.get(var_name)
        if resolved is None:
            raise ValueError(
                f"Environment variable {var_name!r} referenced in the connections file is not set"
            )
        return resolved

    return _ENV_VAR_PATTERN.sub(_replace, value)


class ConnectionRegistry:
    """In-memory registry of connection profiles, loaded once from a YAML file."""

    def __init__(self, profiles: dict[str, ConnectionProfile]) -> None:
        self._profiles = profiles

    @classmethod
    def from_file(cls, path: str) -> "ConnectionRegistry":
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"Connections file not found: {path}")
        raw = yaml.safe_load(file_path.read_text()) or {}
        return cls.from_entries(raw.get("connections", []))

    @classmethod
    def from_entries(cls, entries: list[dict]) -> "ConnectionRegistry":
        profiles: dict[str, ConnectionProfile] = {}
        for raw_entry in entries:
            entry = dict(raw_entry)
            entry["connection_string"] = _interpolate_env(entry["connection_string"])
            profile = ConnectionProfile.model_validate(entry)
            if profile.id in profiles:
                raise ValueError(f"Duplicate connection id in connections file: {profile.id!r}")
            profiles[profile.id] = profile
        return cls(profiles)

    def get(self, connection_id: str) -> ConnectionProfile:
        profile = self._profiles.get(connection_id)
        if profile is None:
            raise KeyError(f"Unknown connection: {connection_id!r}")
        return profile

    def list_public(self) -> list[PublicConnectionInfo]:
        return sorted(
            (PublicConnectionInfo.from_profile(p) for p in self._profiles.values()),
            key=lambda info: info.id,
        )

    def all_ids(self) -> list[str]:
        return sorted(self._profiles.keys())


_registry: Optional[ConnectionRegistry] = None


def get_registry() -> ConnectionRegistry:
    global _registry
    if _registry is None:
        from querygate.core.config import config

        _registry = ConnectionRegistry.from_file(config.connections_file)
    return _registry


def set_registry(registry: ConnectionRegistry) -> None:
    """Override the process-wide registry — used by tests and programmatic setup."""
    global _registry
    _registry = registry
