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
from typing import Optional, Tuple

import yaml

from querygate.connections.models import (
    ConnectionProfile,
    PublicConnectionInfo,
    connection_host_port,
)
from querygate.secrets.resolvers import EnvSecretResolver, SecretResolverRegistry


def _default_resolver_registry() -> SecretResolverRegistry:
    return SecretResolverRegistry({"env": EnvSecretResolver()})


def _validate_join_group_hosts(profiles: dict[str, ConnectionProfile]) -> None:
    """TODO.md item 170 (maintainer-approved 2026-08-09: validate, don't just
    document). `join_group` membership was previously gated only by string
    equality (`validation/schema_validation.py`'s `resolve_query_table_
    connections`) — nothing checked that connections sharing a `join_group`
    are actually the same physical server instance. A cross-connection join
    reflects the joined table through the PRIMARY connection's own engine,
    under a same-instance cross-database schema qualifier
    (`connections/engine.py`'s `physical_db_name`) — so two connections
    placed in one `join_group` that actually point at different hosts were
    never rejected here; instead the query would either silently read an
    unrelated same-named database on the primary's host, or fail with a
    masked `NoSuchTableError` naming neither the real cause nor the mismatch.

    Checked once here, at the file-driven config-load/hot-reload boundary —
    the same boundary item 158's dialect-vs-connection_string check runs at —
    not inside `ConnectionRegistry.__init__` itself, so a raw
    dict-of-profiles built directly (tests, `catalog/
    adaptive_learning_benchmark.py`) is unaffected. This is deliberately
    NOT the same scoping precedent item 158's own validator sets (that one
    is a `field_validator` and therefore runs on EVERY `ConnectionProfile`
    construction, including a direct one — corrected 2026-08-09 after
    `claim-reviewer` found the original wording here mischaracterized that);
    it's a distinct, batch-scoped check with no real precedent to lean on,
    accepted as config-load-boundary-only on its own terms.

    THIS CHECK ALONE DOES NOT CLOSE THE GAP (security-invariant-reviewer /
    architecture-boundary-reviewer, 2026-08-09, independently found): the
    join_group actually consulted at request time
    (`resolve_query_table_connections`) is `Policy.join_group or profile.
    effective_join_group()` — a `Policy.join_group` (set in `policy.yaml`,
    possibly per-principal) can unite two connections whose OWN `join_group`
    fields disagree or are unset, entirely invisibly to this function, since
    `Policy` lives in a separate file this function never sees. This check
    is a fast, config-load-time backstop for the common case (and the only
    place a per-connection-level misconfiguration is caught before any query
    ever runs); `resolve_query_table_connections` itself carries the
    load-bearing version of the same check, since it's the only point that
    has visibility into the actual resolved (and possibly per-principal)
    join_group."""
    groups: dict[str, list[ConnectionProfile]] = {}
    for profile in profiles.values():
        if not profile.enabled:
            # A disabled connection can never be resolved
            # (`connections/visibility.py`), so it can never actually be a
            # join's secondary — comparing its host would only produce a
            # false-positive rejection of an otherwise-valid config
            # (security-invariant-reviewer, 2026-08-09, QG170-5).
            continue
        groups.setdefault(profile.effective_join_group(), []).append(profile)
    for group_name, members in groups.items():
        if len(members) < 2:
            continue
        hosts: dict[str, Tuple[str, Optional[int]]] = {}
        for member in members:
            host_port = connection_host_port(member)
            if host_port is not None:
                hosts[member.id] = host_port
        # Host and port are compared as separate sets, not as a single
        # (host, port) tuple: an omitted port (defaults to the dialect's own
        # default) must not be treated as a mismatch against an explicit one
        # for the SAME host (security-invariant-reviewer, 2026-08-09,
        # QG170-4) — two databases on one Postgres server, written the two
        # ordinary ways, is the canonical join_group use case.
        host_names = {host for host, _ in hosts.values()}
        ports = {port for _, port in hosts.values() if port is not None}
        if len(host_names) > 1 or len(ports) > 1:
            detail = ", ".join(
                f"{connection_id!r} -> {host}:{port}"
                for connection_id, (host, port) in sorted(hosts.items())
            )
            raise ValueError(
                f"join_group {group_name!r} spans different hosts ({detail}) — "
                "connections sharing a join_group must be the same physical server "
                "instance, since a cross-connection join is reflected through the "
                "primary connection's own engine and would otherwise silently read "
                "the wrong database. Split them into separate join_groups if they "
                "are genuinely different hosts."
            )


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
        _validate_join_group_hosts(profiles)
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
