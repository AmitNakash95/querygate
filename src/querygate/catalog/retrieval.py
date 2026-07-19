"""Compact deterministic retrieval over the policy-visible catalog view.

Authorization is applied while candidates are assembled, before tokenization,
ranking, result counts, or byte budgeting.  Hidden entries therefore never
enter an index or affect a caller's ordering/statistics.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Iterable, Optional

import pydantic as pyd

from querygate.catalog.loader import CatalogStore
from querygate.catalog.models import (
    CatalogEntryProvenance,
    CatalogEntryStatus,
    ColumnCatalogEntry,
    RelationshipHint,
    SensitivityClass,
    TableCatalogEntry,
    agent_visible,
    visible_relationships,
)
from querygate.policy.models import Policy

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


class CatalogObjectType(StrEnum):
    TABLE = "table"
    COLUMN = "column"
    RELATIONSHIP = "relationship"


class CatalogFreshness(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    UNTRACKED = "untracked"


class CatalogCitation(pyd.BaseModel):
    entry_id: str
    source_class: str
    source_evidence: list["CatalogEvidenceCitation"]
    status: CatalogEntryStatus
    confidence: Optional[float]
    precedence: int
    catalog_version: int
    schema_fingerprint: str
    freshness: CatalogFreshness

    model_config = pyd.ConfigDict(extra="forbid")


class CatalogEvidenceCitation(pyd.BaseModel):
    """Public evidence citation: kind plus a stable digest, never the raw pointer."""

    kind: str
    reference_fingerprint: str

    model_config = pyd.ConfigDict(extra="forbid")


class CatalogSearchHit(pyd.BaseModel):
    object_type: CatalogObjectType
    table: str
    column: Optional[str] = None
    to_table: Optional[str] = None
    to_column: Optional[str] = None
    description: Optional[str] = None
    aliases: list[str] = pyd.Field(default_factory=list)
    sensitivity: Optional[SensitivityClass] = None
    score: int = pyd.Field(ge=1)
    citation: CatalogCitation

    model_config = pyd.ConfigDict(extra="forbid")


class CatalogSearchResponse(pyd.BaseModel):
    query: str
    results: list[CatalogSearchHit]
    result_count: int = pyd.Field(ge=0)
    truncated: bool = False
    max_results: int = pyd.Field(ge=1)
    max_response_bytes: int = pyd.Field(ge=1)

    model_config = pyd.ConfigDict(extra="forbid")


def _tokens(value: str) -> set[str]:
    return {match.group(0).casefold() for match in _TOKEN_RE.finditer(value)}


def _normalized_identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _score(query_tokens: set[str], *, identifiers: Iterable[str], text: Iterable[str]) -> int:
    identifier_tokens: set[str] = set()
    for value in identifiers:
        identifier_tokens.update(_tokens(value))
    text_tokens: set[str] = set()
    for value in text:
        text_tokens.update(_tokens(value))
    return 5 * len(query_tokens & identifier_tokens) + len(query_tokens & text_tokens)


def policy_safe_catalog_text(
    value: Optional[str], hidden_identifier_tokens: set[str]
) -> Optional[str]:
    if value is None:
        return None
    normalized = "_" + _normalized_identifier(value) + "_"
    echoes_hidden_identifier = any(
        f"_{_normalized_identifier(identifier)}_" in normalized
        for identifier in hidden_identifier_tokens
    )
    if _tokens(value) & hidden_identifier_tokens or echoes_hidden_identifier:
        return None
    return value


def policy_safe_catalog_aliases(
    values: Iterable[str], hidden_identifier_tokens: set[str]
) -> list[str]:
    return [
        value
        for value in values
        if policy_safe_catalog_text(value, hidden_identifier_tokens) is not None
    ]


def policy_hidden_identifier_tokens(
    store: CatalogStore, *, connection_id: str, policy: Policy
) -> set[str]:
    """Exact hidden identifiers that free-form visible metadata must not echo."""

    catalog_tables = list(store.iter_tables(connection_id))
    visible_column_names = {
        column_name.casefold()
        for table_name, table_entry in catalog_tables
        if policy.table_allowed(table_name)
        for column_name in table_entry.columns
        if policy.column_allowed(table_name, column_name)
    }
    hidden_table_names = {table_name.casefold() for table_name in policy.denied_tables} | {
        table_name.casefold()
        for table_name, _table_entry in catalog_tables
        if not policy.table_allowed(table_name)
    }
    explicitly_denied_column_names = {
        column_name.casefold()
        for column_names in policy.denied_columns.values()
        for column_name in column_names
    }
    potentially_hidden_column_names = explicitly_denied_column_names | {
        column_name.casefold()
        for table_name, table_entry in catalog_tables
        for column_name in table_entry.columns
        if not policy.table_allowed(table_name)
        or not policy.column_allowed(table_name, column_name)
    }
    return hidden_table_names | (potentially_hidden_column_names - visible_column_names)


def _freshness(
    provenance: CatalogEntryProvenance, current_schema_fingerprint: Optional[str]
) -> CatalogFreshness:
    if provenance.status == CatalogEntryStatus.STALE:
        return CatalogFreshness.STALE
    if provenance.schema_fingerprint == "untracked" or current_schema_fingerprint is None:
        return CatalogFreshness.UNTRACKED
    if provenance.schema_fingerprint != current_schema_fingerprint:
        return CatalogFreshness.STALE
    return CatalogFreshness.CURRENT


def catalog_citation(
    provenance: CatalogEntryProvenance, current_schema_fingerprint: Optional[str]
) -> CatalogCitation:
    return CatalogCitation(
        entry_id=provenance.entry_id,
        source_class=provenance.source_class.value,
        source_evidence=[
            CatalogEvidenceCitation(
                kind=evidence.kind,
                reference_fingerprint=(
                    "sha256:" + hashlib.sha256(evidence.reference.encode("utf-8")).hexdigest()
                ),
            )
            for evidence in provenance.source_evidence
        ],
        status=provenance.status,
        confidence=provenance.confidence,
        precedence=provenance.precedence,
        catalog_version=provenance.catalog_version,
        schema_fingerprint=provenance.schema_fingerprint,
        freshness=_freshness(provenance, current_schema_fingerprint),
    )


def _searchable_status(provenance: CatalogEntryProvenance) -> bool:
    return agent_visible(provenance)


def _table_hit(
    query_tokens: set[str],
    table_name: str,
    entry: TableCatalogEntry,
    current_schema_fingerprint: Optional[str],
    hidden_identifier_tokens: set[str],
) -> Optional[CatalogSearchHit]:
    if not _searchable_status(entry.provenance):
        return None
    aliases = policy_safe_catalog_aliases(entry.aliases, hidden_identifier_tokens)
    description = policy_safe_catalog_text(entry.description, hidden_identifier_tokens)
    default_aggregation = policy_safe_catalog_text(
        entry.default_aggregation, hidden_identifier_tokens
    )
    score = _score(
        query_tokens,
        identifiers=[table_name, *aliases],
        text=[description or "", default_aggregation or ""],
    )
    if score == 0:
        return None
    return CatalogSearchHit(
        object_type=CatalogObjectType.TABLE,
        table=table_name,
        description=description,
        aliases=aliases,
        sensitivity=entry.sensitivity,
        score=score,
        citation=catalog_citation(entry.provenance, current_schema_fingerprint),
    )


def _column_hit(
    query_tokens: set[str],
    table_name: str,
    column_name: str,
    entry: ColumnCatalogEntry,
    current_schema_fingerprint: Optional[str],
    hidden_identifier_tokens: set[str],
) -> Optional[CatalogSearchHit]:
    if not _searchable_status(entry.provenance):
        return None
    aliases = policy_safe_catalog_aliases(entry.aliases, hidden_identifier_tokens)
    description = policy_safe_catalog_text(entry.description, hidden_identifier_tokens)
    score = _score(
        query_tokens,
        identifiers=[table_name, column_name, f"{table_name}.{column_name}", *aliases],
        text=[description or ""],
    )
    if score == 0:
        return None
    return CatalogSearchHit(
        object_type=CatalogObjectType.COLUMN,
        table=table_name,
        column=column_name,
        description=description,
        aliases=aliases,
        sensitivity=entry.sensitivity,
        score=score,
        citation=catalog_citation(entry.provenance, current_schema_fingerprint),
    )


def _relationship_hit(
    query_tokens: set[str],
    table_name: str,
    relationship: RelationshipHint,
    current_schema_fingerprint: Optional[str],
    hidden_identifier_tokens: set[str],
) -> Optional[CatalogSearchHit]:
    if not _searchable_status(relationship.provenance):
        return None
    description = policy_safe_catalog_text(relationship.description, hidden_identifier_tokens)
    score = _score(
        query_tokens,
        identifiers=[
            table_name,
            relationship.column,
            relationship.to_table,
            relationship.to_column,
        ],
        text=[description or "", "join relationship"],
    )
    if score == 0:
        return None
    return CatalogSearchHit(
        object_type=CatalogObjectType.RELATIONSHIP,
        table=table_name,
        column=relationship.column,
        to_table=relationship.to_table,
        to_column=relationship.to_column,
        description=description,
        score=score,
        citation=catalog_citation(relationship.provenance, current_schema_fingerprint),
    )


def search_catalog(
    store: CatalogStore,
    *,
    connection_id: str,
    policy: Policy,
    query: str,
    max_results: int = 5,
    max_response_bytes: int = 16_384,
) -> CatalogSearchResponse:
    """Search only the already-authorized view of one connection's catalog."""

    query = query.strip()
    if not query:
        raise ValueError("catalog search query must not be empty")
    if len(query) > 256:
        raise ValueError("catalog search query must be 256 characters or fewer")
    if not 1 <= max_results <= 20:
        raise ValueError("catalog search max_results must be between 1 and 20")
    if max_response_bytes < 512:
        raise ValueError("catalog search max_response_bytes must be at least 512")

    query_tokens = _tokens(query)
    if not query_tokens:
        raise ValueError("catalog search query must contain a searchable word")
    empty_response = CatalogSearchResponse(
        query=query,
        results=[],
        result_count=0,
        max_results=max_results,
        max_response_bytes=max_response_bytes,
    )
    envelope_bytes = len(json.dumps(empty_response.model_dump(mode="json")).encode("utf-8"))
    if envelope_bytes > max_response_bytes:
        raise ValueError("catalog search response byte budget is too small for its envelope")
    snapshot = store.get_schema_snapshot(connection_id)
    current_fingerprint = snapshot.fingerprint if snapshot is not None else None
    candidates: list[CatalogSearchHit] = []

    # Free-form descriptions/aliases on an otherwise-visible entry must not
    # smuggle exact hidden identifiers into search. Determine those identifiers
    # under policy before any candidate text is scored. A column name that is
    # visible elsewhere is not itself secret; qualified relationships remain
    # protected by visible_relationships below.
    catalog_tables = list(store.iter_tables(connection_id))
    hidden_identifier_tokens = policy_hidden_identifier_tokens(
        store, connection_id=connection_id, policy=policy
    )

    # Security boundary: policy checks happen before candidate text is
    # tokenized/scored. Hidden objects cannot affect ranking or counts.
    for table_name, table_entry in catalog_tables:
        if not policy.table_allowed(table_name):
            continue
        table_hit = _table_hit(
            query_tokens,
            table_name,
            table_entry,
            current_fingerprint,
            hidden_identifier_tokens,
        )
        if table_hit is not None:
            candidates.append(table_hit)
        for column_name, column_entry in table_entry.columns.items():
            if not policy.column_allowed(table_name, column_name):
                continue
            column_hit = _column_hit(
                query_tokens,
                table_name,
                column_name,
                column_entry,
                current_fingerprint,
                hidden_identifier_tokens,
            )
            if column_hit is not None:
                candidates.append(column_hit)
        for relationship in visible_relationships(table_entry, policy, from_table=table_name):
            relationship_hit = _relationship_hit(
                query_tokens,
                table_name,
                relationship,
                current_fingerprint,
                hidden_identifier_tokens,
            )
            if relationship_hit is not None:
                candidates.append(relationship_hit)

    candidates.sort(
        key=lambda hit: (
            -hit.score,
            hit.object_type.value,
            hit.table.casefold(),
            (hit.column or "").casefold(),
            (hit.to_table or "").casefold(),
        )
    )
    selected: list[CatalogSearchHit] = []
    truncated = len(candidates) > max_results
    for hit in candidates[:max_results]:
        proposed = [*selected, hit]
        # Measure the complete public response, not only the results list.
        # ``truncated=True`` is the worst-case serialization by one byte.
        candidate_response = CatalogSearchResponse(
            query=query,
            results=proposed,
            result_count=len(proposed),
            truncated=True,
            max_results=max_results,
            max_response_bytes=max_response_bytes,
        )
        response_bytes = len(json.dumps(candidate_response.model_dump(mode="json")).encode("utf-8"))
        if response_bytes > max_response_bytes:
            truncated = True
            break
        selected.append(hit)

    return CatalogSearchResponse(
        query=query,
        results=selected,
        result_count=len(selected),
        truncated=truncated,
        max_results=max_results,
        max_response_bytes=max_response_bytes,
    )
