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
    """Structurally separate evidence classes from TODO item 32.

    ``MANUAL`` (TODO item 84) is a proposal-only class: a human-authored draft
    stays quarantined under it until published, at which point the publish step
    mints a fresh ``VERIFIED`` provenance for the merged entry. It therefore
    never appears on a published, agent-visible catalog entry — only on a
    pending/approved draft proposal.
    """

    OBSERVED = "observed"
    INFERRED = "inferred"
    VERIFIED = "verified"
    LEARNED = "learned"
    MANUAL = "manual"


class CatalogEntryStatus(StrEnum):
    DRAFT = "draft"
    VERIFIED = "verified"
    REJECTED = "rejected"
    STALE = "stale"
    ARCHIVED = "archived"


class CatalogDraftObjectType(StrEnum):
    TABLE = "table"
    COLUMN = "column"
    RELATIONSHIP = "relationship"


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
    # A human-authored manual proposal is trusted at the verified tier — it
    # publishes as a verified entry. It never actually participates in a
    # replacement_decision merge as a candidate (publish presents a verified
    # probe, see governance._plan_publish), so this only backs the computed
    # `precedence` field on the quarantined draft's provenance.
    KnowledgeSourceClass.MANUAL: CatalogPrecedence.VERIFIED,
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
    """Return the deterministic gate a governed publication merge must use.

    Generated proposals are stored separately and never call this gate on
    their own.  A later review/publish workflow must use it rather than
    inventing ad-hoc rules that can overwrite verified fields. Sensitivity
    labels are immutable through this gate regardless of source precedence;
    changing one remains an explicit manually governed catalog edit.
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


class CatalogDraftTarget(pyd.BaseModel):
    """Exact schema object a quarantined proposal describes."""

    connection_id: Annotated[str, pyd.StringConstraints(min_length=1, max_length=100)]
    object_type: CatalogDraftObjectType
    table: Annotated[str, pyd.StringConstraints(min_length=1, max_length=128)]
    column: Optional[Annotated[str, pyd.StringConstraints(min_length=1, max_length=128)]] = None
    to_table: Optional[Annotated[str, pyd.StringConstraints(min_length=1, max_length=128)]] = None
    to_column: Optional[Annotated[str, pyd.StringConstraints(min_length=1, max_length=128)]] = None

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)

    @pyd.model_validator(mode="after")
    def _validate_target_shape(self) -> "CatalogDraftTarget":
        if self.object_type == CatalogDraftObjectType.TABLE:
            if any((self.column, self.to_table, self.to_column)):
                raise ValueError("table draft targets cannot include column or relationship fields")
        elif self.object_type == CatalogDraftObjectType.COLUMN:
            if self.column is None or self.to_table is not None or self.to_column is not None:
                raise ValueError("column draft targets require only column")
        elif self.column is None or self.to_table is None or self.to_column is None:
            raise ValueError("relationship draft targets require column, to_table, and to_column")
        return self


_DraftText = Annotated[str, pyd.StringConstraints(strip_whitespace=True, min_length=1)]


class CatalogDraftContent(pyd.BaseModel):
    """Semantic fields a provider may propose; enforcement fields are absent."""

    description: Optional[Annotated[_DraftText, pyd.StringConstraints(max_length=2000)]] = None
    aliases: list[Annotated[_DraftText, pyd.StringConstraints(max_length=100)]] = pyd.Field(
        default_factory=list, max_length=20
    )
    default_aggregation: Optional[Annotated[_DraftText, pyd.StringConstraints(max_length=500)]] = (
        None
    )

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)

    @pyd.model_validator(mode="after")
    def _not_empty(self) -> "CatalogDraftContent":
        if self.description is None and not self.aliases and self.default_aggregation is None:
            raise ValueError("catalog draft content must propose at least one semantic field")
        if len({alias.casefold() for alias in self.aliases}) != len(self.aliases):
            raise ValueError("catalog draft aliases must be unique case-insensitively")
        return self


class ProposalReviewStatus(StrEnum):
    """Deny-by-default review state for a quarantined draft proposal.

    ``PENDING`` is the only state a freshly generated proposal may start in.
    Every other state is reached only through an explicit, validated
    transition in ``catalog/governance.py`` — there is no path that lets a
    draft publish itself.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHED = "published"


class ProposalReviewAction(StrEnum):
    EDITED = "edited"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHED = "published"
    ROLLED_BACK = "rolled_back"


class ProposalReviewEvent(pyd.BaseModel):
    """One durable, actor-attributed transition in a proposal's review history."""

    action: ProposalReviewAction
    actor: Annotated[str, pyd.StringConstraints(min_length=1, max_length=160)]
    occurred_at: pyd.AwareDatetime
    reason: Optional[Annotated[str, pyd.StringConstraints(min_length=1, max_length=500)]] = None

    model_config = pyd.ConfigDict(extra="forbid")


class CatalogDraftProposal(pyd.BaseModel):
    """A durable, non-agent-visible proposal working through 32B review.

    ``review_status``/``review_history`` are orthogonal to ``provenance``:
    ``provenance.status`` tracks the underlying schema-freshness lifecycle
    (draft/stale), while ``review_status`` tracks the human governance
    workflow (pending/approved/rejected/published). A proposal is never
    agent-visible or indexed regardless of either status.
    """

    proposal_id: Annotated[str, pyd.StringConstraints(min_length=1, max_length=160)]
    generation_id: Annotated[str, pyd.StringConstraints(min_length=1, max_length=120)]
    target: CatalogDraftTarget
    content: CatalogDraftContent
    provenance: CatalogEntryProvenance
    review_status: ProposalReviewStatus = ProposalReviewStatus.PENDING
    review_history: list[ProposalReviewEvent] = pyd.Field(default_factory=list, max_length=50)
    published_entry_id: Optional[
        Annotated[str, pyd.StringConstraints(min_length=1, max_length=160)]
    ] = None
    published_version_id: Optional[
        Annotated[str, pyd.StringConstraints(min_length=1, max_length=40)]
    ] = None

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _quarantine_inferred_content(self) -> "CatalogDraftProposal":
        if self.provenance.entry_id != self.proposal_id:
            raise ValueError("draft proposal_id must equal its provenance entry_id")
        if self.provenance.source_class not in {
            KnowledgeSourceClass.INFERRED,
            KnowledgeSourceClass.LEARNED,
            KnowledgeSourceClass.MANUAL,
        }:
            raise ValueError(
                "catalog proposals must remain inferred, learned, or manual until published"
            )
        if self.provenance.status not in {
            CatalogEntryStatus.DRAFT,
            CatalogEntryStatus.STALE,
        }:
            raise ValueError("generated catalog proposals must remain draft or stale")
        if self.target.object_type != CatalogDraftObjectType.TABLE and (
            self.content.default_aggregation is not None
        ):
            raise ValueError("default_aggregation may only be proposed for a table")
        if self.target.object_type == CatalogDraftObjectType.RELATIONSHIP and self.content.aliases:
            raise ValueError("relationship proposals cannot define aliases")
        return self

    @pyd.model_validator(mode="after")
    def _review_state_shape(self) -> "CatalogDraftProposal":
        if self.review_status == ProposalReviewStatus.PUBLISHED and (
            self.published_entry_id is None or self.published_version_id is None
        ):
            raise ValueError("a published proposal requires published_entry_id and version_id")
        if self.review_status != ProposalReviewStatus.PUBLISHED and (
            self.published_entry_id is not None or self.published_version_id is not None
        ):
            raise ValueError("only a published proposal may carry published_entry_id/version_id")
        if self.review_status == ProposalReviewStatus.REJECTED and not any(
            event.action == ProposalReviewAction.REJECTED and event.reason
            for event in self.review_history
        ):
            raise ValueError("a rejected proposal requires a rejection reason in review_history")
        return self


class CatalogVersionAction(StrEnum):
    PUBLISH = "publish"
    ROLLBACK = "rollback"


class CatalogVersionStatus(StrEnum):
    ACTIVE = "active"
    REVERTED = "reverted"


class CatalogVersionChange(pyd.BaseModel):
    """A single entry's before/after content — business metadata only, never
    row data, credentials, or query literals, since draft content itself
    structurally cannot carry those fields (see ``CatalogDraftContent``).
    """

    target: CatalogDraftTarget
    change: Literal["created", "updated", "removed"]
    before: Optional[CatalogDraftContent] = None
    after: Optional[CatalogDraftContent] = None

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _shape_matches_change_kind(self) -> "CatalogVersionChange":
        # "updated" deliberately has no before/after shape constraint beyond
        # what "created"/"removed" forbid below: the entity itself keeps
        # existing, but any individual touched field may have had no prior
        # value (before=None) or may be cleared entirely (after=None) —
        # both are legitimate field-level states, not entity lifecycle
        # changes.
        if self.change == "created" and (self.before is not None or self.after is None):
            raise ValueError("a 'created' change requires 'after' and no 'before'")
        if self.change == "removed" and (self.before is None or self.after is not None):
            raise ValueError("a 'removed' change requires 'before' and no 'after'")
        return self


class CatalogVersionRecord(pyd.BaseModel):
    """Durable, append-only history entry for a publish or rollback action.

    Stored inside the same catalog file/lock as everything else in this
    module — there is no separate catalog-governance database or file.
    """

    version_id: Annotated[str, pyd.StringConstraints(min_length=1, max_length=40)]
    action: CatalogVersionAction
    status: CatalogVersionStatus = CatalogVersionStatus.ACTIVE
    actor: Annotated[str, pyd.StringConstraints(min_length=1, max_length=160)]
    occurred_at: pyd.AwareDatetime
    proposal_id: Optional[Annotated[str, pyd.StringConstraints(min_length=1, max_length=160)]] = (
        None
    )
    rolled_back_version_id: Optional[
        Annotated[str, pyd.StringConstraints(min_length=1, max_length=40)]
    ] = None
    changes: list[CatalogVersionChange] = pyd.Field(min_length=1, max_length=1)

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _action_shape(self) -> "CatalogVersionRecord":
        if self.action == CatalogVersionAction.PUBLISH and self.proposal_id is None:
            raise ValueError("a publish version record requires proposal_id")
        if self.action == CatalogVersionAction.ROLLBACK and self.rolled_back_version_id is None:
            raise ValueError("a rollback version record requires rolled_back_version_id")
        return self


class CatalogGenerationRecord(pyd.BaseModel):
    """Idempotency/provenance record without provider payload or draft text."""

    generation_id: Annotated[str, pyd.StringConstraints(min_length=1, max_length=120)]
    connection_id: Annotated[str, pyd.StringConstraints(min_length=1, max_length=100)]
    provider_mode: Literal["manual", "usage-learner"] = "manual"
    provider_id: Annotated[str, pyd.StringConstraints(min_length=1, max_length=160)]
    prompt_template_version: Annotated[str, pyd.StringConstraints(min_length=1, max_length=80)]
    schema_fingerprint: Annotated[str, pyd.StringConstraints(min_length=1, max_length=80)]
    input_fingerprint: Annotated[str, pyd.StringConstraints(min_length=71, max_length=71)]
    proposal_ids: list[Annotated[str, pyd.StringConstraints(min_length=1, max_length=160)]] = (
        pyd.Field(default_factory=list, max_length=200)
    )
    created_by: Optional[Annotated[str, pyd.StringConstraints(min_length=1, max_length=160)]] = None
    created_at: Optional[pyd.AwareDatetime] = None

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.field_validator("input_fingerprint")
    @classmethod
    def _sha256_fingerprint(cls, value: str) -> str:
        if not value.startswith("sha256:") or any(
            character not in "0123456789abcdef" for character in value[7:]
        ):
            raise ValueError("generation input_fingerprint must be a lowercase SHA-256 digest")
        return value

    @pyd.model_validator(mode="after")
    def _proposal_ids_are_unique(self) -> "CatalogGenerationRecord":
        if len(self.proposal_ids) != len(set(self.proposal_ids)):
            raise ValueError("generation proposal ids must be unique")
        return self


class CatalogUsageSignalKind(StrEnum):
    """TODO item 32C's typed usage-signal contract. Only ``RELATIONSHIP_USED``
    currently feeds the learner (``catalog/learning.py``) — the other two
    kinds are recorded/observable now so the signal contract does not need
    to change if a later phase teaches the learner to act on them too.
    """

    TABLE_USED = "table_used"
    COLUMN_USED = "column_used"
    RELATIONSHIP_USED = "relationship_used"


_OBJECT_TYPE_FOR_SIGNAL_KIND = {
    CatalogUsageSignalKind.TABLE_USED: CatalogDraftObjectType.TABLE,
    CatalogUsageSignalKind.COLUMN_USED: CatalogDraftObjectType.COLUMN,
    CatalogUsageSignalKind.RELATIONSHIP_USED: CatalogDraftObjectType.RELATIONSHIP,
}


class CatalogUsageSignal(pyd.BaseModel):
    """A single redaction-safe, typed observation that one successfully
    executed, already policy-validated query used a table/column/
    relationship — the only kind of evidence TODO item 32C's learner may
    act on. Structurally cannot carry row values, query literals,
    natural-language history, or a raw principal identity:
    ``principal_partition`` is always a stable hash (see
    ``catalog.usage.hash_principal_partition``), never the caller's real
    subject, so a durable catalog file never stores caller identity.
    """

    signal_id: Annotated[str, pyd.StringConstraints(min_length=1, max_length=160)]
    connection_id: Annotated[str, pyd.StringConstraints(min_length=1, max_length=100)]
    principal_partition: Annotated[str, pyd.StringConstraints(min_length=1, max_length=80)]
    target: CatalogDraftTarget
    kind: CatalogUsageSignalKind
    schema_fingerprint: Annotated[str, pyd.StringConstraints(min_length=1, max_length=80)]
    observed_at: pyd.AwareDatetime
    evidence: CatalogEvidence

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)

    @pyd.field_validator("principal_partition")
    @classmethod
    def _hashed_partition(cls, value: str) -> str:
        if not value.startswith("sha256:") or any(
            character not in "0123456789abcdef" for character in value[7:]
        ):
            raise ValueError("usage signal principal_partition must be a lowercase SHA-256 digest")
        return value

    @pyd.model_validator(mode="after")
    def _target_matches_kind(self) -> "CatalogUsageSignal":
        if self.target.connection_id != self.connection_id:
            raise ValueError("usage signal target must match its own connection_id")
        if self.target.object_type != _OBJECT_TYPE_FOR_SIGNAL_KIND[self.kind]:
            raise ValueError(
                f"usage signal kind {self.kind.value!r} requires a matching target object_type"
            )
        if self.evidence.kind != CatalogEvidenceKind.USAGE:
            raise ValueError("usage signal evidence must use the 'usage' evidence kind")
        return self


class CatalogExportBundle(pyd.BaseModel):
    """A connection-scoped, self-contained snapshot for export/backup and
    import/restore (TODO item 32B-2). Contains exactly the same kinds of
    content already privileged behind ``catalog:review``/``catalog:export``
    (published entries, quarantined draft proposals with review history,
    generation records, version history, and the schema snapshot) — never
    row values, credentials, or anything not already part of the catalog
    file's own privileged record.
    """

    export_version: Literal[1] = 1
    connection_id: Annotated[str, pyd.StringConstraints(min_length=1, max_length=100)]
    exported_at: pyd.AwareDatetime
    catalog_version: int = pyd.Field(ge=1)
    tables: dict[str, TableCatalogEntry] = pyd.Field(default_factory=dict)
    schema_snapshot: Optional[ObservedSchemaSnapshot] = None
    draft_proposals: list[CatalogDraftProposal] = pyd.Field(default_factory=list)
    generation_records: list[CatalogGenerationRecord] = pyd.Field(default_factory=list)
    version_history: list[CatalogVersionRecord] = pyd.Field(default_factory=list)

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _content_matches_connection(self) -> "CatalogExportBundle":
        if (
            self.schema_snapshot is not None
            and self.schema_snapshot.connection_id != self.connection_id
        ):
            raise ValueError("export schema_snapshot must match the bundle's connection_id")
        if any(p.target.connection_id != self.connection_id for p in self.draft_proposals):
            raise ValueError("export draft_proposals must all target the bundle's connection_id")
        if any(r.connection_id != self.connection_id for r in self.generation_records):
            raise ValueError(
                "export generation_records must all belong to the bundle's connection_id"
            )
        if any(
            record.changes[0].target.connection_id != self.connection_id
            for record in self.version_history
        ):
            raise ValueError("export version_history must all target the bundle's connection_id")
        return self


class SchemaCatalog(pyd.BaseModel):
    # Version 1 remains accepted.  CatalogStore upgrades its entries in
    # memory with deterministic provenance; new files should use version 2.
    version: Literal[1, 2] = 2
    connections: dict[str, ConnectionCatalog] = pyd.Field(default_factory=dict)
    schema_snapshots: dict[str, ObservedSchemaSnapshot] = pyd.Field(default_factory=dict)
    draft_proposals: list[CatalogDraftProposal] = pyd.Field(default_factory=list)
    generation_records: list[CatalogGenerationRecord] = pyd.Field(default_factory=list)
    version_history: list[CatalogVersionRecord] = pyd.Field(default_factory=list, max_length=2000)
    # TODO item 32C: bounded, pre-decision usage evidence — never exported/
    # imported (see CatalogExportBundle), never a query-execution or row-
    # value path, and pruned FIFO at the cap by catalog.usage.record_usage_signal
    # rather than growing unboundedly.
    usage_signals: list[CatalogUsageSignal] = pyd.Field(default_factory=list, max_length=50_000)

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _snapshot_keys_match_connections(self) -> "SchemaCatalog":
        if self.version == 1 and (
            self.schema_snapshots
            or self.draft_proposals
            or self.generation_records
            or self.usage_signals
        ):
            raise ValueError(
                "schema snapshots and semantic-memory records require catalog version 2"
            )
        signal_ids = [signal.signal_id for signal in self.usage_signals]
        if len(signal_ids) != len(set(signal_ids)):
            raise ValueError("catalog usage signal ids must be unique")
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
        entry_ids.extend(proposal.provenance.entry_id for proposal in self.draft_proposals)
        bound_ids = [
            entry_id for entry_id in entry_ids if entry_id != "urn:querygate:catalog:unbound"
        ]
        if len(bound_ids) != len(set(bound_ids)):
            raise ValueError("catalog entry ids must be unique")
        proposal_ids = [proposal.proposal_id for proposal in self.draft_proposals]
        if len(proposal_ids) != len(set(proposal_ids)):
            raise ValueError("catalog draft proposal ids must be unique")
        generation_ids = [record.generation_id for record in self.generation_records]
        if len(generation_ids) != len(set(generation_ids)):
            raise ValueError("catalog generation ids must be unique")
        proposal_id_set = set(proposal_ids)
        proposals_by_id = {proposal.proposal_id: proposal for proposal in self.draft_proposals}
        for record in self.generation_records:
            if not set(record.proposal_ids).issubset(proposal_id_set):
                raise ValueError("catalog generation records must reference existing proposals")
            if any(
                proposals_by_id[proposal_id].generation_id != record.generation_id
                for proposal_id in record.proposal_ids
            ):
                raise ValueError("catalog generation records cannot claim another run's proposal")
        generation_id_set = set(generation_ids)
        records_by_id = {record.generation_id: record for record in self.generation_records}
        for proposal in self.draft_proposals:
            if proposal.generation_id not in generation_id_set:
                raise ValueError("catalog draft proposals require a generation record")
            if proposal.proposal_id not in records_by_id[proposal.generation_id].proposal_ids:
                raise ValueError("catalog generation record must include each of its proposals")
            if proposal.published_version_id is not None and not any(
                record.version_id == proposal.published_version_id
                for record in self.version_history
            ):
                raise ValueError("a published proposal must reference an existing version record")

        version_ids = [record.version_id for record in self.version_history]
        if len(version_ids) != len(set(version_ids)):
            raise ValueError("catalog version history ids must be unique")
        version_id_set = set(version_ids)
        for record in self.version_history:
            if record.proposal_id is not None and record.proposal_id not in proposal_id_set:
                raise ValueError("a publish version record must reference an existing proposal")
            if (
                record.rolled_back_version_id is not None
                and record.rolled_back_version_id not in version_id_set
            ):
                raise ValueError("a rollback version record must reference an existing version")
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
