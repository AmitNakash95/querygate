"""Versioned semantic-catalog models and policy-safe relationship filtering.

The catalog remains descriptive only: it cannot grant access, alter a policy,
or affect query compilation/execution.  Version 2 adds durable provenance to
the item-27 catalog without making older ``version: 1`` files invalid.  The
loader binds deterministic entry ids and catalog versions for legacy content.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Annotated, Literal, Optional

import pydantic as pyd

from querygate.catalog.schema_memory import ObservedSchemaSnapshot

if TYPE_CHECKING:
    from querygate.policy.models import Policy


class SensitivityClass(StrEnum):
    NONE = "none"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    PII = "pii"


class KnowledgeSourceClass(StrEnum):
    """Structurally separate evidence classes from TODO item 32."""

    OBSERVED = "observed"
    INFERRED = "inferred"
    VERIFIED = "verified"
    LEARNED = "learned"


class CatalogEntryStatus(StrEnum):
    DRAFT = "draft"
    VERIFIED = "verified"
    REJECTED = "rejected"
    STALE = "stale"
    ARCHIVED = "archived"


class CatalogEvidenceKind(StrEnum):
    MANUAL = "manual"
    SCHEMA = "schema"
    MODEL = "model"
    USAGE = "usage"
    IMPORT = "import"


class CatalogPrecedence(IntEnum):
    """Non-configurable replacement precedence.

    Human-verified knowledge wins over deterministic observed metadata when
    the two describe the same semantic field; observed schema remains the
    source of truth for actual identifiers/types in the schema subsystem.
    Inferred and learned content stay below both and therefore cannot replace
    a verified value during regeneration.
    """

    LEARNED = 100
    INFERRED = 200
    OBSERVED = 300
    VERIFIED = 400


_PRECEDENCE = {
    KnowledgeSourceClass.LEARNED: CatalogPrecedence.LEARNED,
    KnowledgeSourceClass.INFERRED: CatalogPrecedence.INFERRED,
    KnowledgeSourceClass.OBSERVED: CatalogPrecedence.OBSERVED,
    KnowledgeSourceClass.VERIFIED: CatalogPrecedence.VERIFIED,
}


class CatalogEvidence(pyd.BaseModel):
    """A redaction-safe evidence pointer, never the evidence payload itself."""

    kind: CatalogEvidenceKind
    reference: Annotated[str, pyd.StringConstraints(min_length=1, max_length=256)]

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.field_validator("reference")
    @classmethod
    def _single_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("catalog evidence pointers must be single-line metadata")
        return value

    @pyd.field_validator("reference")
    @classmethod
    def _redaction_safe_reference(cls, value: str) -> str:
        if not all(character.isalnum() or character in "._:/-" for character in value):
            raise ValueError(
                "catalog evidence references may contain only letters, numbers, '.', '_', "
                "':', '/', and '-'"
            )
        return value


class CatalogEntryProvenance(pyd.BaseModel):
    """Durable provenance shared by table, column, and relationship entries."""

    entry_id: Annotated[str, pyd.StringConstraints(min_length=1, max_length=160)] = (
        "urn:querygate:catalog:unbound"
    )
    catalog_version: int = pyd.Field(default=2, ge=1)
    source_class: KnowledgeSourceClass = KnowledgeSourceClass.VERIFIED
    source_evidence: list[CatalogEvidence] = pyd.Field(
        default_factory=lambda: [CatalogEvidence(kind="manual", reference="catalog.yaml")],
        min_length=1,
        max_length=20,
    )
    confidence: Optional[float] = pyd.Field(default=1.0, ge=0.0, le=1.0)
    status: CatalogEntryStatus = CatalogEntryStatus.VERIFIED
    schema_fingerprint: Annotated[str, pyd.StringConstraints(min_length=1, max_length=80)] = (
        "untracked"
    )
    created_by: Optional[Annotated[str, pyd.StringConstraints(min_length=1, max_length=160)]] = None
    approved_by: Optional[Annotated[str, pyd.StringConstraints(min_length=1, max_length=160)]] = (
        None
    )
    created_at: Optional[pyd.AwareDatetime] = None
    approved_at: Optional[pyd.AwareDatetime] = None
    model_id: Optional[Annotated[str, pyd.StringConstraints(min_length=1, max_length=160)]] = None
    prompt_template_version: Optional[
        Annotated[str, pyd.StringConstraints(min_length=1, max_length=80)]
    ] = None

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _validate_state(self) -> "CatalogEntryProvenance":
        if self.status == CatalogEntryStatus.VERIFIED and self.source_class not in {
            KnowledgeSourceClass.VERIFIED,
            KnowledgeSourceClass.OBSERVED,
        }:
            raise ValueError("inferred or learned catalog content cannot be verified at load time")
        if (
            self.source_class
            in {
                KnowledgeSourceClass.INFERRED,
                KnowledgeSourceClass.LEARNED,
            }
            and self.confidence is None
        ):
            raise ValueError("inferred or learned catalog content requires confidence")
        if self.source_class != KnowledgeSourceClass.INFERRED and (
            self.model_id is not None or self.prompt_template_version is not None
        ):
            raise ValueError("model metadata is only valid for inferred catalog content")
        return self

    @pyd.computed_field
    @property
    def precedence(self) -> int:
        """Server-derived; catalog YAML cannot choose or raise precedence."""

        return int(_PRECEDENCE[self.source_class])


class ReplacementDecision(pyd.BaseModel):
    allowed: bool
    reason: str

    model_config = pyd.ConfigDict(extra="forbid")


def replacement_decision(
    existing: CatalogEntryProvenance,
    candidate: CatalogEntryProvenance,
    *,
    field_name: str,
    value_changes: bool = True,
) -> ReplacementDecision:
    """Return the deterministic gate a future regeneration merge must use.

    This phase has no generator and performs no automatic merge.  Defining
    and testing the gate now prevents the later provider work from inventing
    ad-hoc rules that can overwrite verified fields.  Sensitivity labels are
    immutable through this gate regardless of source precedence; changing one
    remains an explicit manually governed catalog edit.
    """

    if not value_changes:
        return ReplacementDecision(allowed=True, reason="value is unchanged")
    if field_name == "sensitivity":
        return ReplacementDecision(
            allowed=False,
            reason="sensitivity labels are never changed by semantic-memory merging",
        )
    if existing.status == CatalogEntryStatus.VERIFIED and (
        candidate.source_class != KnowledgeSourceClass.VERIFIED
        or candidate.status != CatalogEntryStatus.VERIFIED
    ):
        return ReplacementDecision(
            allowed=False,
            reason="a draft or non-verified source cannot overwrite human-verified knowledge",
        )
    if candidate.status != CatalogEntryStatus.VERIFIED:
        return ReplacementDecision(
            allowed=False,
            reason=f"candidate status {candidate.status.value!r} is not publishable",
        )
    if candidate.precedence < existing.precedence:
        return ReplacementDecision(
            allowed=False,
            reason="candidate source has lower precedence than existing knowledge",
        )
    return ReplacementDecision(
        allowed=True,
        reason="candidate meets the documented status and precedence rules",
    )


def agent_visible(provenance: CatalogEntryProvenance) -> bool:
    """Rejected/archived review history never reaches agent context."""

    return provenance.status not in {
        CatalogEntryStatus.REJECTED,
        CatalogEntryStatus.ARCHIVED,
    }


class RelationshipHint(pyd.BaseModel):
    """A descriptive join hint, never an enforced or executable relationship."""

    to_table: str
    column: str
    to_column: str
    description: Optional[str] = None
    provenance: CatalogEntryProvenance = pyd.Field(default_factory=CatalogEntryProvenance)

    model_config = pyd.ConfigDict(extra="forbid")


class ColumnCatalogEntry(pyd.BaseModel):
    description: Optional[str] = None
    aliases: list[str] = pyd.Field(default_factory=list)
    sensitivity: SensitivityClass = SensitivityClass.NONE
    allow_samples: bool = False
    provenance: CatalogEntryProvenance = pyd.Field(default_factory=CatalogEntryProvenance)

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _protect_sensitivity_and_sampling(self) -> "ColumnCatalogEntry":
        if self.sensitivity != SensitivityClass.NONE and (
            self.provenance.source_class != KnowledgeSourceClass.VERIFIED
        ):
            raise ValueError("only verified knowledge may set a sensitivity label")
        if self.allow_samples and self.provenance.source_class != KnowledgeSourceClass.VERIFIED:
            raise ValueError("only verified knowledge may enable samples")
        return self


class TableCatalogEntry(pyd.BaseModel):
    description: Optional[str] = None
    aliases: list[str] = pyd.Field(default_factory=list)
    sensitivity: SensitivityClass = SensitivityClass.NONE
    default_aggregation: Optional[str] = None
    allow_samples: bool = False
    relationships: list[RelationshipHint] = pyd.Field(default_factory=list)
    columns: dict[str, ColumnCatalogEntry] = pyd.Field(default_factory=dict)
    provenance: CatalogEntryProvenance = pyd.Field(default_factory=CatalogEntryProvenance)

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _relationship_identities_are_unique(self) -> "TableCatalogEntry":
        identities = [
            (
                relationship.column.casefold(),
                relationship.to_table.casefold(),
                relationship.to_column.casefold(),
            )
            for relationship in self.relationships
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate catalog relationship identity")
        column_names = [name.casefold() for name in self.columns]
        if len(column_names) != len(set(column_names)):
            raise ValueError("catalog column names must be unique case-insensitively")
        if self.sensitivity != SensitivityClass.NONE and (
            self.provenance.source_class != KnowledgeSourceClass.VERIFIED
        ):
            raise ValueError("only verified knowledge may set a sensitivity label")
        if self.allow_samples and self.provenance.source_class != KnowledgeSourceClass.VERIFIED:
            raise ValueError("only verified knowledge may enable samples")
        return self

    def column(self, column_name: str) -> Optional[ColumnCatalogEntry]:
        target = column_name.lower()
        for key, value in self.columns.items():
            if key.lower() == target:
                return value
        return None


class ConnectionCatalog(pyd.BaseModel):
    tables: dict[str, TableCatalogEntry] = pyd.Field(default_factory=dict)

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _table_names_are_unique(self) -> "ConnectionCatalog":
        table_names = [name.casefold() for name in self.tables]
        if len(table_names) != len(set(table_names)):
            raise ValueError("catalog table names must be unique case-insensitively")
        return self

    def table(self, table_name: str) -> Optional[TableCatalogEntry]:
        target = table_name.lower()
        for key, value in self.tables.items():
            if key.lower() == target:
                return value
        return None


class SchemaCatalog(pyd.BaseModel):
    # Version 1 remains accepted.  CatalogStore upgrades its entries in
    # memory with deterministic provenance; new files should use version 2.
    version: Literal[1, 2] = 2
    connections: dict[str, ConnectionCatalog] = pyd.Field(default_factory=dict)
    schema_snapshots: dict[str, ObservedSchemaSnapshot] = pyd.Field(default_factory=dict)

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _snapshot_keys_match_connections(self) -> "SchemaCatalog":
        if self.version == 1 and self.schema_snapshots:
            raise ValueError("schema_snapshots require catalog version 2")
        for connection_id, snapshot in self.schema_snapshots.items():
            if connection_id != snapshot.connection_id:
                raise ValueError(
                    f"schema_snapshots key {connection_id!r} does not match snapshot "
                    f"connection_id {snapshot.connection_id!r}"
                )
        entry_ids: list[str] = []
        for connection in self.connections.values():
            for table in connection.tables.values():
                entry_ids.append(table.provenance.entry_id)
                entry_ids.extend(column.provenance.entry_id for column in table.columns.values())
                entry_ids.extend(
                    relationship.provenance.entry_id for relationship in table.relationships
                )
        bound_ids = [
            entry_id for entry_id in entry_ids if entry_id != "urn:querygate:catalog:unbound"
        ]
        if len(bound_ids) != len(set(bound_ids)):
            raise ValueError("catalog entry ids must be unique")
        return self


def visible_relationships(
    entry: TableCatalogEntry, policy: "Policy", *, from_table: Optional[str] = None
) -> list[RelationshipHint]:
    """Filter relationship targets and both join columns before disclosure."""

    visible: list[RelationshipHint] = []
    for relationship in entry.relationships:
        if not agent_visible(relationship.provenance):
            continue
        if not policy.table_allowed(relationship.to_table):
            continue
        if from_table is not None and not policy.column_allowed(from_table, relationship.column):
            continue
        if not policy.column_allowed(relationship.to_table, relationship.to_column):
            continue
        visible.append(relationship)
    return visible
