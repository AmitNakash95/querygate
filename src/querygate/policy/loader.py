"""Loads per-connection policy from a YAML file.

The file has a `default:` section (used for any connection without its own
entry) and a `connections:` map of per-connection overrides merged on top of
the default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from querygate.policy.models import Policy


class PolicyStore:
    def __init__(self, default: Policy, overrides: dict[str, Policy]) -> None:
        self._default = default
        self._overrides = overrides

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
        return cls(default=default, overrides=overrides)

    def get(self, connection_id: str) -> Policy:
        return self._overrides.get(connection_id, self._default)


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


def get_policy(connection_id: str) -> Policy:
    return get_policy_store().get(connection_id)
