"""Provider contract for quarantined semantic-memory draft generation.

32A-2 intentionally ships no hosted or local model adapter. ``disabled`` is
the safe default and performs no work; ``manual`` accepts an already-produced,
strictly structured batch. Neither implementation has a network primitive.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Optional, Protocol

import pydantic as pyd

from querygate.catalog.models import CatalogDraftContent, CatalogDraftTarget
from querygate.catalog.schema_memory import ObservedSchemaSnapshot

_SafeId = Annotated[
    str,
    pyd.StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


class SemanticMemoryProviderMode(StrEnum):
    DISABLED = "disabled"
    MANUAL = "manual"


class SemanticGenerationRequest(pyd.BaseModel):
    generation_id: _SafeId
    connection_id: Annotated[str, pyd.StringConstraints(min_length=1, max_length=100)]
    snapshot: ObservedSchemaSnapshot
    purpose: Literal["onboarding", "refresh", "correction"] = "onboarding"
    max_proposals: int = pyd.Field(default=100, ge=1, le=200)

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)

    @pyd.model_validator(mode="after")
    def _snapshot_matches_connection(self) -> "SemanticGenerationRequest":
        if self.snapshot.connection_id != self.connection_id:
            raise ValueError("generation snapshot must match the requested connection")
        return self


class ManualDraftSuggestion(pyd.BaseModel):
    target: CatalogDraftTarget
    content: CatalogDraftContent
    confidence: float = pyd.Field(ge=0.0, le=1.0)

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)


class ManualDraftBatch(pyd.BaseModel):
    """Offline/manual structured output; never an instruction or raw prompt."""

    format_version: Literal[1] = 1
    generation_id: _SafeId
    connection_id: Annotated[str, pyd.StringConstraints(min_length=1, max_length=100)]
    schema_fingerprint: Annotated[str, pyd.StringConstraints(min_length=1, max_length=80)]
    provider_id: _SafeId = "manual-only"
    prompt_template_version: Annotated[
        str, pyd.StringConstraints(strip_whitespace=True, min_length=1, max_length=80)
    ] = "manual-v1"
    created_by: Optional[Annotated[str, pyd.StringConstraints(min_length=1, max_length=160)]] = None
    created_at: Optional[pyd.AwareDatetime] = None
    suggestions: list[ManualDraftSuggestion] = pyd.Field(min_length=1, max_length=200)

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)

    @pyd.model_validator(mode="after")
    def _targets_match_connection(self) -> "ManualDraftBatch":
        if any(
            suggestion.target.connection_id != self.connection_id for suggestion in self.suggestions
        ):
            raise ValueError("manual draft targets must match the batch connection")
        return self


class ProviderGenerationResult(pyd.BaseModel):
    outcome: Literal["disabled", "generated"]
    provider_mode: SemanticMemoryProviderMode
    suggestions: tuple[ManualDraftSuggestion, ...] = ()
    provider_id: Optional[str] = None
    prompt_template_version: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[pyd.AwareDatetime] = None

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)


class SemanticMemoryProvider(Protocol):
    mode: SemanticMemoryProviderMode

    def generate(self, request: SemanticGenerationRequest) -> ProviderGenerationResult:
        """Return structured proposals without publishing or merging them."""
        ...


class DisabledSemanticMemoryProvider:
    mode = SemanticMemoryProviderMode.DISABLED

    def generate(self, request: SemanticGenerationRequest) -> ProviderGenerationResult:
        return ProviderGenerationResult(outcome="disabled", provider_mode=self.mode)


class ManualSemanticMemoryProvider:
    mode = SemanticMemoryProviderMode.MANUAL

    def __init__(self, batch: ManualDraftBatch) -> None:
        self._batch = batch

    def generate(self, request: SemanticGenerationRequest) -> ProviderGenerationResult:
        if self._batch.generation_id != request.generation_id:
            raise ValueError("manual batch generation_id does not match the request")
        if self._batch.connection_id != request.connection_id:
            raise ValueError("manual batch connection does not match the request")
        if self._batch.schema_fingerprint != request.snapshot.fingerprint:
            raise ValueError("manual batch was produced for a different schema fingerprint")
        if len(self._batch.suggestions) > request.max_proposals:
            raise ValueError("manual batch exceeds the request proposal limit")
        return ProviderGenerationResult(
            outcome="generated",
            provider_mode=self.mode,
            suggestions=tuple(self._batch.suggestions),
            provider_id=self._batch.provider_id,
            prompt_template_version=self._batch.prompt_template_version,
            created_by=self._batch.created_by,
            created_at=self._batch.created_at,
        )
