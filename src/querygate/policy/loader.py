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


def _merge_bases(
    connection_id: str, *, default: Policy, overrides: dict[str, Policy]
) -> list[Policy]:
    """Every base `PolicyStore.get` could merge this principal override onto.

    A connection-specific entry has exactly one: that connection's own
    `connections:` override, or the default when it has none — which is
    `overrides.get(connection_id, default)`, the same expression `get()` uses. A
    `"*"` entry applies to every connection, so it must satisfy every base,
    including the bare default for connections with no override entry at all.
    """
    if connection_id != "*":
        return [overrides.get(connection_id, default)]
    return [default, *overrides.values()]


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
                # Fail fast at load time, against EVERY merge base this override
                # can actually land on at request time — not just the default
                # (TODO.md item 188).
                #
                # Validating against `default_raw` alone was harmless while
                # `Policy` had no cross-field constraint: a typo'd field name or
                # a wrong type fails identically whatever the base is. Item 179
                # added `_disclosure_budget_needs_a_k_floor`, the first validator
                # that can fail on a COMBINATION of two individually-valid
                # layers, and with it this shortcut became a real defect:
                # `default:` sets `min_group_size: 5`, `connections.foo:` sets it
                # back to `null`, `principals.alice.foo:` sets
                # `max_shape_repeats_per_window` — every layer valid, the file
                # loads clean, `validate-config` passes, and then the first query
                # alice issues on `foo` raises inside
                # `StructuredQueryService._get_policy()`, which `mask_unexpected`
                # turns into a generic 500. One principal fully offline, on a
                # config the CLI accepted, with an error nobody can act on.
                #
                # So mirror `get()` exactly: it merges the override onto the
                # RESOLVED base (`base.model_dump()`), and the base is this
                # connection's own override when it has one. A `"*"` entry can
                # land on any of them, so it is checked against all.
                for base in _merge_bases(connection_id, default=default, overrides=overrides):
                    Policy.model_validate({**base.model_dump(), **entry})
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

    def principal_override_map(self) -> PrincipalOverrides:
        """A copy of the per-principal override map (subject -> connection ->
        partial policy dict).

        The semantic-access diff (admin/service.py) compares this between the
        active and candidate snapshots so it can honestly flag that the
        per-principal layer changed — connection-baseline diffing resolves the
        default/connection layers only, so a change that lives purely in a
        principal override would otherwise be reported as "no change."
        Returned as a deep-ish copy so callers can't mutate the store's state.
        """
        return {
            subject: {conn: dict(entry) for conn, entry in per_connection.items()}
            for subject, per_connection in self._principal_overrides.items()
        }


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
