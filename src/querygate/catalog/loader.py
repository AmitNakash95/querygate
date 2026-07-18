"""Loads an optional curated schema-catalog overlay from YAML.

Unlike connections.yaml/policy.yaml, a catalog file is opt-in — a deployment
with `CATALOG_FILE` unset still works identically, `describe_table` simply
returns `catalog: null` for every table/column.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from querygate.catalog.models import SchemaCatalog, TableCatalogEntry


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
        return cls(SchemaCatalog.model_validate(raw))

    def get_table(self, connection_id: str, table_name: str) -> Optional[TableCatalogEntry]:
        connection_catalog = self._catalog.connections.get(connection_id)
        if connection_catalog is None:
            return None
        return connection_catalog.table(table_name)

    def connection_ids(self) -> list[str]:
        """Connection ids referenced in the catalog file — used to cross-check
        against the connections file's real ids (see querygate/cli.py's
        validate-config command), mirroring
        PolicyStore.override_connection_ids.
        """
        return sorted(self._catalog.connections.keys())


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
