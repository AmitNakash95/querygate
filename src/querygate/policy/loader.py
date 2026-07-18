"""Loads per-connection (and, optionally, per-principal) policy from a YAML file.

The file has a `default:` section (used for any connection without its own
entry), a `connections:` map of per-connection overrides merged on top of
the default, and an optional `principals:` map of per-principal overrides
merged on top of whichever of those two applies — every authenticated
caller of a connection otherwise gets identical table/column/row access.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import yaml

from querygate.core.auth import Principal
from querygate.policy.models import Policy

# subject -> (connection_id or "*") -> partial Policy override dict
PrincipalOverrides = Dict[str, Dict[str, dict]]


class PolicyStore:
    def __init__(
        self,
        default: Policy,
        overrides: dict[str, Policy],
        principal_overrides: Optional[PrincipalOverrides] = None,
    ) -> None:
        self._default = default
        self._overrides = overrides
        self._principal_overrides = principal_overrides or {}

    @classmethod
    def from_file(cls, path: str) -> "PolicyStore":
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"Policy file not found: {path}")
        raw = yaml.safe_load(file_path.read_text()) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "PolicyStore":
        default_raw = raw.get("default", {}) or {}
        default = Policy.model_validate(default_raw)
        overrides: dict[str, Policy] = {}
        for connection_id, entry in (raw.get("connections", {}) or {}).items():
            merged = {**default_raw, **(entry or {})}
            overrides[connection_id] = Policy.model_validate(merged)

        principal_overrides: PrincipalOverrides = {}
        for subject, per_connection in (raw.get("principals", {}) or {}).items():
            resolved: Dict[str, dict] = {}
            for connection_id, entry in (per_connection or {}).items():
                entry = entry or {}
                # Fail fast on a typo'd field name or wrong type here, at load
                # time, rather than the first time a matching principal
                # queries — merged against default_raw as a validity check
                # only; the real merge base at request time may instead be
                # this connection's own `connections:` override (see get()).
                Policy.model_validate({**default_raw, **entry})
                resolved[connection_id] = entry
            principal_overrides[subject] = resolved

        return cls(default=default, overrides=overrides, principal_overrides=principal_overrides)

    def get(self, connection_id: str, principal: Optional[Principal] = None) -> Policy:
        base = self._overrides.get(connection_id, self._default)
        if principal is None:
            return base
        by_connection = self._principal_overrides.get(principal.subject)
        if not by_connection:
            return base
        # A connection-specific entry wins over a "*" (every connection)
        # entry for the same principal.
        override = by_connection.get(connection_id, by_connection.get("*"))
        if not override:
            return base
        return Policy.model_validate({**base.model_dump(), **override})

    def override_connection_ids(self) -> list[str]:
        """Connection ids with a `connections:` override entry in the policy
        file — used to cross-check against the connections file's real ids
        (see querygate/cli.py's validate-config command).
        """
        return sorted(self._overrides.keys())


_store: Optional[PolicyStore] = None


def get_policy_store() -> PolicyStore:
    global _store
    if _store is None:
        from querygate.core.config import config

        _store = PolicyStore.from_file(config.policy_file)
    return _store


def set_policy_store(store: PolicyStore) -> None:
    """Override the process-wide policy store — used by tests and programmatic setup."""
    global _store
    _store = store


def get_policy(connection_id: str, principal: Optional[Principal] = None) -> Policy:
    return get_policy_store().get(connection_id, principal=principal)
