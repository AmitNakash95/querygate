"""File-driven connection profile registry.

Connection strings are resolved via ${...} interpolation (see
`querygate/secrets/resolvers.py`), so the connections file itself never
needs to contain a secret — only a reference to where the real value lives.
This replaces the old hardcoded `DataBase` enum with a dynamic,
deployment-specific registry (goal: "support dynamic database connection
profiles instead of hardcoded enum members").
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from querygate.connections.models import ConnectionProfile, PublicConnectionInfo
from querygate.secrets.resolvers import EnvSecretResolver, SecretResolverRegistry


def _default_resolver_registry() -> SecretResolverRegistry:
    return SecretResolverRegistry({"env": EnvSecretResolver()})


class ConnectionRegistry:
    """In-memory registry of connection profiles, loaded once from a YAML file."""

    def __init__(self, profiles: dict[str, ConnectionProfile]) -> None:
        self._profiles = profiles

    @classmethod
    def from_file(
        cls, path: str, resolver_registry: Optional[SecretResolverRegistry] = None
    ) -> "ConnectionRegistry":
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"Connections file not found: {path}")
        raw = yaml.safe_load(file_path.read_text()) or {}
        return cls.from_entries(raw.get("connections", []), resolver_registry=resolver_registry)

    @classmethod
    def from_entries(
        cls,
        entries: list[dict],
        resolver_registry: Optional[SecretResolverRegistry] = None,
    ) -> "ConnectionRegistry":
        registry = resolver_registry or _default_resolver_registry()
        profiles: dict[str, ConnectionProfile] = {}
        for raw_entry in entries:
            entry = dict(raw_entry)
            entry["connection_string"] = registry.interpolate(entry["connection_string"])
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
        from querygate.secrets.resolvers import build_secret_resolver_registry

        _registry = ConnectionRegistry.from_file(
            config.connections_file, resolver_registry=build_secret_resolver_registry(config)
        )
    return _registry


def set_registry(registry: ConnectionRegistry) -> None:
    """Override the process-wide registry — used by tests and programmatic setup."""
    global _registry
    _registry = registry
