"""File-backed store of query templates + a process-wide singleton.

Mirrors `catalog/loader.py`'s `CatalogStore`: loaded from a YAML file
(`TEMPLATES_FILE`), swapped atomically on config reload, and never a second
mutation path. Templates are declarative configuration in phase 1 — as safe as
`policy.yaml` (and a template can't exceed policy); the governed
create/edit/approve/publish workflow is phase 2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import yaml

from querygate.templates.models import QueryTemplate, QueryTemplateFile


class TemplateStore:
    """In-memory registry of query templates keyed by id (case-insensitively)."""

    def __init__(self, templates: Optional[List[QueryTemplate]] = None) -> None:
        self._by_id: Dict[str, QueryTemplate] = {}
        for template in templates or []:
            self._by_id[template.id.casefold()] = template

    @classmethod
    def empty(cls) -> "TemplateStore":
        return cls([])

    @classmethod
    def from_file(cls, path: str) -> "TemplateStore":
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"Templates file not found: {path}")
        raw = yaml.safe_load(file_path.read_text()) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "TemplateStore":
        return cls(QueryTemplateFile.model_validate(raw).templates)

    def get(self, template_id: str) -> Optional[QueryTemplate]:
        return self._by_id.get(template_id.casefold())

    def list(self) -> List[QueryTemplate]:
        return [self._by_id[key] for key in sorted(self._by_id)]

    def template_ids(self) -> List[str]:
        return sorted(t.id for t in self._by_id.values())

    def connection_ids(self) -> List[str]:
        """Every connection id any template targets — used by config validation
        to cross-check against the connections file's real ids.
        """
        return sorted({t.connection for t in self._by_id.values()})


_store: Optional[TemplateStore] = None


def get_template_store() -> TemplateStore:
    global _store
    if _store is None:
        from querygate.core.config import config

        _store = (
            TemplateStore.from_file(config.template_file)
            if config.template_file
            else TemplateStore.empty()
        )
    return _store


def set_template_store(store: TemplateStore) -> None:
    """Override the process-wide template store — used by tests and reload."""
    global _store
    _store = store
