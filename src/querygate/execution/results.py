"""Response and catalog-info models returned by `StructuredQueryService`.

**Why these live here and not in `service.py`.** Item 214's compiler spike
measured that a module defining `pydantic.BaseModel` subclasses cannot be
compiled: Cython turns methods into `cyfunction` objects, which pydantic v2's
metaclass does not recognise as functions, so every method reads as an
un-annotated candidate *field* and class creation raises `PydanticUserError`.

`service.py` is the read enforcement funnel — `_validate_and_compile` is the one
call site covering execute/explain/verdict/batch, and it is where the
subscription gate lands (TODO.md item 211). That is exactly the module worth
compiling, so the twelve response models it used to define moved here, leaving
`service.py` free of `BaseModel` definitions and therefore compilable.

Little is lost by leaving these interpreted: **ten of the twelve are already
published** in the OpenAPI component schemas, so they are a public API contract
rather than protectable logic. The exceptions are `BatchExplainItemResult` and
`BatchVerdictItemResult`, which appear in neither the OpenAPI components nor the
MCP tool schemas — they are internal shapes, and they stay readable. Both are
five trivial optional fields, so the exposure is negligible, but the claim is
"ten of twelve", not "all". `tests/unit/test_execution_results_contract.py`
pins the split.

`service.py` re-exports each name, so existing `from ...service import
StructuredQueryResult` imports keep working unchanged.
"""

from typing import List, Literal, Optional, Union

import pydantic as pyd

from querygate.catalog.models import SensitivityClass
from querygate.catalog.retrieval import CatalogCitation, CompactCatalogCitation


class TableCatalogInfo(pyd.BaseModel):
    """Curated table-level metadata from an optional catalog overlay (see
    `querygate/catalog/`) — display-only, never used for policy enforcement.
    `relationships` is pre-filtered to targets the caller's resolved policy
    allows (see `catalog.models.visible_relationships`).
    """

    description: Optional[str] = None
    aliases: List[str] = pyd.Field(default_factory=list)
    sensitivity: SensitivityClass = SensitivityClass.NONE
    default_aggregation: Optional[str] = None
    allow_samples: bool = False
    relationships: List["RelationshipCatalogInfo"] = pyd.Field(default_factory=list)
    provenance: Union[CatalogCitation, CompactCatalogCitation]


class RelationshipCatalogInfo(pyd.BaseModel):
    to_table: str
    column: str
    to_column: str
    description: Optional[str] = None
    provenance: Union[CatalogCitation, CompactCatalogCitation]


class ColumnCatalogInfo(pyd.BaseModel):
    description: Optional[str] = None
    aliases: List[str] = pyd.Field(default_factory=list)
    sensitivity: SensitivityClass = SensitivityClass.NONE
    allow_samples: bool = False
    provenance: Union[CatalogCitation, CompactCatalogCitation]


class ColumnInfo(pyd.BaseModel):
    name: str
    type: str
    nullable: bool
    description: Optional[str] = None
    catalog: Optional[ColumnCatalogInfo] = None


class TableDescription(pyd.BaseModel):
    name: str
    columns: List[ColumnInfo]
    description: Optional[str] = None
    catalog: Optional[TableCatalogInfo] = None


class StructuredQueryResult(pyd.BaseModel):
    rows: List[dict]
    row_count: int
    truncated: bool
    limit: int
    offset: int
    # Agent-visible admission info (TODO.md item 35 phase 1). Optional so
    # existing direct-construction call sites (tests, `execute_many`'s error
    # path) don't need to supply them.
    admission_id: Optional[str] = None
    queue_wait_ms: Optional[int] = None


class ExplainResult(pyd.BaseModel):
    sql: str
    params: Optional[str] = None
    tables: List[str]
    limit: int


class VerdictPlan(pyd.BaseModel):
    """Only present when `Policy.verdict_include_plan` opts in; omitted by
    default since the compiled SQL and touched-table list are themselves a
    discovery channel for a caller who couldn't otherwise see this schema."""

    sql: str
    tables: List[str]


class VerdictResult(pyd.BaseModel):
    """TODO.md item 133: would `query` be allowed for this principal, without
    executing it. `reason`/`message` are deliberately coarse — see
    `StructuredQueryService.verdict`'s docstring for why a denial never
    distinguishes policy from schema."""

    allowed: bool
    reason: Optional[Literal["not-available-to-you"]] = None
    message: Optional[str] = None
    plan: Optional[VerdictPlan] = None


class BatchVerdictItemResult(pyd.BaseModel):
    allowed: Optional[bool] = None
    reason: Optional[Literal["not-available-to-you"]] = None
    message: Optional[str] = None
    plan: Optional[VerdictPlan] = None
    # A system-level failure (quota exhausted, connection at capacity) rather
    # than a verdict about the query's shape — mirrors BatchExplainItemResult's
    # per-item error tolerance.
    error: Optional[str] = None


class BatchQueryItemResult(pyd.BaseModel):
    rows: Optional[List[dict]] = None
    row_count: Optional[int] = None
    truncated: Optional[bool] = None
    limit: Optional[int] = None
    offset: Optional[int] = None
    admission_id: Optional[str] = None
    admission_state: Optional[str] = None
    queue_wait_ms: Optional[int] = None
    error: Optional[str] = None
    # Populated only when `error` is specifically an ApprovalRequiredError
    # (TODO.md item 92/128) — lets a caller distinguish "this item needs
    # human approval" from any other rejection without parsing `error`'s
    # free-text message, and carries what a caller needs to request it (the
    # MCP MRTR port builds one InputRequiredResult input_request per item
    # that sets these; REST's single-query path already surfaces the same
    # two fields via ApprovalRequiredError's 428 response).
    approval_fingerprint: Optional[str] = None
    approval_reasons: Optional[List[str]] = None


class BatchExplainItemResult(pyd.BaseModel):
    sql: Optional[str] = None
    params: Optional[str] = None
    tables: Optional[List[str]] = None
    limit: Optional[int] = None
    error: Optional[str] = None
