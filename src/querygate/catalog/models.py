"""Curated schema-catalog overlay — descriptions, aliases, relationship hints,
and sensitivity metadata that admins layer on top of raw schema reflection.

This is display-only metadata surfaced to agents via `describe_table` (MCP and
REST). It has no effect on query validation, compilation, or execution —
`Policy` and `validation/` remain the only enforcement points. A catalog entry
can describe a table/column that a given principal's policy denies; it is the
caller's job (`catalog.visible_relationships` below, plus the existing
column-level policy filter in `execution/service.py`) to keep curated
metadata from disclosing more than schema/policy discovery already allows.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Optional

import pydantic as pyd

if TYPE_CHECKING:
    from querygate.policy.models import Policy


class SensitivityClass(str, Enum):
    NONE = "none"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    PII = "pii"


class RelationshipHint(pyd.BaseModel):
    """A curated, human-asserted relationship — not derived from a live
    foreign key and never enforced. Purely descriptive guidance for an agent
    choosing a join.
    """

    to_table: str
    column: str
    to_column: str
    description: Optional[str] = None

    model_config = pyd.ConfigDict(extra="forbid")


class ColumnCatalogEntry(pyd.BaseModel):
    description: Optional[str] = None
    aliases: list[str] = pyd.Field(default_factory=list)
    sensitivity: SensitivityClass = SensitivityClass.NONE
    allow_samples: bool = False

    model_config = pyd.ConfigDict(extra="forbid")


class TableCatalogEntry(pyd.BaseModel):
    description: Optional[str] = None
    aliases: list[str] = pyd.Field(default_factory=list)
    sensitivity: SensitivityClass = SensitivityClass.NONE
    default_aggregation: Optional[str] = None
    allow_samples: bool = False
    relationships: list[RelationshipHint] = pyd.Field(default_factory=list)
    columns: dict[str, ColumnCatalogEntry] = pyd.Field(default_factory=dict)

    model_config = pyd.ConfigDict(extra="forbid")

    def column(self, column_name: str) -> Optional[ColumnCatalogEntry]:
        """Case-insensitive lookup — column casing in catalog.yaml isn't
        guaranteed to match live reflection any more than it is for policy.yaml
        (see policy/models.py's `_ci_lookup`).
        """
        target = column_name.lower()
        for key, value in self.columns.items():
            if key.lower() == target:
                return value
        return None


class ConnectionCatalog(pyd.BaseModel):
    tables: dict[str, TableCatalogEntry] = pyd.Field(default_factory=dict)

    model_config = pyd.ConfigDict(extra="forbid")

    def table(self, table_name: str) -> Optional[TableCatalogEntry]:
        target = table_name.lower()
        for key, value in self.tables.items():
            if key.lower() == target:
                return value
        return None


class SchemaCatalog(pyd.BaseModel):
    version: int = 1
    connections: dict[str, ConnectionCatalog] = pyd.Field(default_factory=dict)

    model_config = pyd.ConfigDict(extra="forbid")


def visible_relationships(entry: TableCatalogEntry, policy: "Policy") -> list[RelationshipHint]:
    """Drop relationship hints pointing at a table the resolved policy denies.

    An admin's curated relationship shouldn't let a principal-restricted
    caller infer the existence of a table they can't otherwise see or query —
    the same non-enumeration property `connections/visibility.py` and
    `validation/policy_validation.py` already hold for connections and
    tables/columns reachable through the query AST itself.
    """
    return [r for r in entry.relationships if policy.table_allowed(r.to_table)]
