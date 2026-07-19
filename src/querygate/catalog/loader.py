"""Loads an optional curated schema-catalog overlay from YAML.

Unlike connections.yaml/policy.yaml, a catalog file is opt-in — a deployment
with `CATALOG_FILE` unset still works identically, `describe_table` simply
returns `catalog: null` for every table/column.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any, Iterator, Optional

import yaml

from querygate.catalog.models import (
    CatalogDraftProposal,
    CatalogGenerationRecord,
    SchemaCatalog,
    TableCatalogEntry,
)
from querygate.catalog.schema_memory import ObservedSchemaSnapshot


def _stable_entry_id(*parts: str) -> str:
    identity = "\x1f".join(part.casefold() for part in parts)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return f"urn:querygate:catalog:{digest}"


def _bind_provenance(
    raw_entry: dict, *, entry_id: str, catalog_version: int, schema_fingerprint: str
) -> None:
    provenance = raw_entry.setdefault("provenance", {})
    provenance.setdefault("entry_id", entry_id)
    provenance.setdefault("catalog_version", catalog_version)
    provenance.setdefault("schema_fingerprint", schema_fingerprint)


def _normalize_catalog(raw: dict) -> dict:
    """Upgrade legacy catalog content without mutating the caller's object."""

    normalized = copy.deepcopy(raw)
    version = normalized.setdefault("version", 2)
    connections = normalized.get("connections", {}) or {}
    snapshots = normalized.get("schema_snapshots", {}) or {}
    for connection_id, connection in connections.items():
        snapshot = snapshots.get(connection_id) or {}
        schema_fingerprint = (
            ObservedSchemaSnapshot.model_validate(snapshot).fingerprint if snapshot else "untracked"
        )
        for table_name, table in (connection.get("tables", {}) or {}).items():
            _bind_provenance(
                table,
                entry_id=_stable_entry_id(connection_id, "table", table_name),
                catalog_version=version,
                schema_fingerprint=schema_fingerprint,
            )
            for column_name, column in (table.get("columns", {}) or {}).items():
                _bind_provenance(
                    column,
                    entry_id=_stable_entry_id(
                        connection_id, "table", table_name, "column", column_name
                    ),
                    catalog_version=version,
                    schema_fingerprint=schema_fingerprint,
                )
            for relationship in table.get("relationships", []) or []:
                _bind_provenance(
                    relationship,
                    entry_id=_stable_entry_id(
                        connection_id,
                        "table",
                        table_name,
                        "relationship",
                        relationship.get("column", ""),
                        relationship.get("to_table", ""),
                        relationship.get("to_column", ""),
                    ),
                    catalog_version=version,
                    schema_fingerprint=schema_fingerprint,
                )
    return normalized


class CatalogStore:
    def __init__(self, catalog: SchemaCatalog) -> None:
        self._catalog = catalog

    @classmethod
    def empty(cls) -> "CatalogStore":
        return cls(SchemaCatalog())

    @classmethod
    def from_file(cls, path: str) -> "CatalogStore":
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"Catalog file not found: {path}")
        raw = yaml.safe_load(file_path.read_text()) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "CatalogStore":
        return cls(SchemaCatalog.model_validate(_normalize_catalog(raw)))

    def get_table(self, connection_id: str, table_name: str) -> Optional[TableCatalogEntry]:
        connection_catalog = self._catalog.connections.get(connection_id)
        if connection_catalog is None:
            return None
        return connection_catalog.table(table_name)

    def iter_tables(self, connection_id: str) -> Iterator[tuple[str, TableCatalogEntry]]:
        connection_catalog = self._catalog.connections.get(connection_id)
        if connection_catalog is None:
            return
        yield from connection_catalog.tables.items()

    def get_schema_snapshot(self, connection_id: str) -> Optional[ObservedSchemaSnapshot]:
        return self._catalog.schema_snapshots.get(connection_id)

    def iter_draft_proposals(
        self, connection_id: Optional[str] = None
    ) -> Iterator[CatalogDraftProposal]:
        for proposal in self._catalog.draft_proposals:
            if connection_id is None or proposal.target.connection_id == connection_id:
                yield proposal

    def get_generation_record(self, generation_id: str) -> Optional[CatalogGenerationRecord]:
        for record in self._catalog.generation_records:
            if record.generation_id == generation_id:
                return record
        return None

    def to_dict(self) -> dict[str, Any]:
        """Return the complete privileged durable record, never an API projection."""

        # ``round_trip`` omits computed fields such as server-derived
        # precedence, so a serialized catalog cannot feed them back as input.
        return self._catalog.model_dump(mode="json", exclude_none=True, round_trip=True)

    def replace(self, raw: dict[str, Any]) -> "CatalogStore":
        """Build a validated replacement using this store's catalog version."""

        replacement = copy.deepcopy(raw)
        replacement.setdefault("version", self.version)
        return CatalogStore.from_dict(replacement)

    @property
    def version(self) -> int:
        return self._catalog.version

    def connection_ids(self) -> list[str]:
        """Connection ids referenced in the catalog file — used to cross-check
        against the connections file's real ids (see querygate/cli.py's
        validate-config command), mirroring
        PolicyStore.override_connection_ids.
        """
        return sorted(
            set(self._catalog.connections.keys())
            | set(self._catalog.schema_snapshots.keys())
            | {proposal.target.connection_id for proposal in self._catalog.draft_proposals}
            | {record.connection_id for record in self._catalog.generation_records}
        )


_store: Optional[CatalogStore] = None


def get_catalog_store() -> CatalogStore:
    global _store
    if _store is None:
        from querygate.core.config import config

        _store = (
            CatalogStore.from_file(config.catalog_file)
            if config.catalog_file
            else CatalogStore.empty()
        )
    return _store


def set_catalog_store(store: CatalogStore) -> None:
    """Override the process-wide catalog store — used by tests and programmatic setup."""
    global _store
    _store = store
