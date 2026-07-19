"""Per-connection policy — the enforcement surface between an agent and real SQL.

A `Policy` is resolved per connection (see policy/loader.py) and checked by
validation/policy_validation.py before a query is ever compiled or executed.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Optional

import pydantic as pyd

from querygate.core.exceptions import PolicyViolationError

if TYPE_CHECKING:
    from querygate.core.auth import Principal


class CostEstimationMode(StrEnum):
    """How `Policy.max_estimated_rows`/`max_estimated_cost` are applied once
    cost estimation is enabled (see `Policy.cost_estimation_enabled`).

    ENFORCE (default) rejects a query whose Postgres EXPLAIN estimate
    exceeds the configured threshold — the original TODO.md item 26 phase 1
    behavior, unchanged. OBSERVE records what *would* have been rejected
    (a `cost_estimation.observed_would_reject` log line plus
    `querygate_cost_estimation_would_reject_total`) without blocking the
    query — use it to calibrate thresholds against real traffic before
    switching a connection over to ENFORCE, since Postgres's planner-cost
    units aren't portable across schemas/hardware and a threshold copied
    from documentation is a guess, not a measurement.
    """

    ENFORCE = "enforce"
    OBSERVE = "observe"


class MandatoryRowFilter(pyd.BaseModel):
    """An equality filter always AND-ed into every query that touches
    `table` — a generic, policy-declared replacement for hardcoding a
    "scope every query to this tenant" rule into the query AST itself.

    The filtered value is either a static `value` (the original design —
    every caller on this connection gets the same filter) or `from_claim`,
    resolved from the authenticated principal's claims at query time — the
    shape most multi-tenant deployments actually need ("scope every query
    to tenant_id = <claim from the caller's JWT>"), which a fixed `value`
    can't express since it's identical for every caller on the connection.
    """

    table: str
    column: str
    value: Optional[object] = None
    from_claim: Optional[str] = None

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _require_one_value_source(self) -> "MandatoryRowFilter":
        if self.from_claim is None and self.value is None:
            raise ValueError(
                f"mandatory_row_filter on {self.table}.{self.column} needs either "
                "'value' or 'from_claim'"
            )
        return self

    def resolve(self, principal: "Optional[Principal]") -> object:
        if self.from_claim is None:
            return self.value
        if principal is None or self.from_claim not in principal.claims:
            raise PolicyViolationError(
                f"mandatory_row_filter on {self.table}.{self.column} requires claim "
                f"{self.from_claim!r}, which the authenticated principal doesn't have"
            )
        return principal.claims[self.from_claim]


class Policy(pyd.BaseModel):
    # Access and discovery switch. A resolved false value hides the
    # connection from REST/MCP listings and makes direct access behave as if
    # the connection id does not exist.
    enabled: bool = True

    # Table/column allow-deny. An empty allow-list means "no restriction";
    # deny always wins over allow. Column lists are keyed by table name, with
    # "*" applying to every table.
    allowed_tables: list[str] = pyd.Field(default_factory=list)
    denied_tables: list[str] = pyd.Field(default_factory=list)
    allowed_columns: dict[str, list[str]] = pyd.Field(default_factory=dict)
    denied_columns: dict[str, list[str]] = pyd.Field(default_factory=dict)

    # Query complexity caps.
    max_joins: int = pyd.Field(default=5)
    max_select_columns: int = pyd.Field(default=30)
    max_where_depth: int = pyd.Field(default=5)
    max_group_by: int = pyd.Field(default=10)
    max_limit: int = pyd.Field(default=100)
    max_limit_aggregate: int = pyd.Field(default=1000)
    default_limit: int = pyd.Field(default=50)
    max_top_n: int = pyd.Field(default=50)
    max_partition_by: int = pyd.Field(default=5)
    max_batch_size: int = pyd.Field(default=10)

    # `max_limit`/`max_limit_aggregate` cap row *count*; this caps response
    # *size* — a wide TEXT/JSONB/BLOB column selected across many rows is a
    # policy-compliant query that can still blow up the response body. Rows
    # are truncated (not the request rejected) once the running total would
    # exceed this, mirroring the row-count truncation UX.
    max_response_bytes: int = pyd.Field(default=10_000_000)

    # Execution guardrails.
    timeout_seconds: int = pyd.Field(default=30)
    max_concurrency: int = pyd.Field(default=8)
    concurrency_wait_seconds: float = pyd.Field(default=10)

    # Pre-execution cost estimation (execution/cost_estimation.py, TODO.md
    # item 26 phase 1). Unset (None, the default for both) means disabled —
    # existing deployments behave identically. When set, `execute()` asks
    # Postgres to plan (never run) the compiled query via
    # `EXPLAIN (FORMAT JSON)` before it actually executes, and rejects the
    # query if the planner's row-count/cost estimate exceeds the configured
    # threshold. Postgres only for this first pass — MSSQL's estimated-plan
    # equivalent needs its own connection lifecycle (see
    # execution/cost_estimation.py's module docstring) and silently has no
    # effect here, so an MSSQL connection with these set behaves the same as
    # one without them. `max_estimated_cost` is in Postgres's own arbitrary
    # planner-cost units (not seconds or bytes) — treat it as a relative
    # complexity signal to tune per deployment/hardware, not a portable
    # absolute number. `cost_estimation_mode` decides whether an over-threshold
    # estimate actually rejects the query (ENFORCE, the default) or is only
    # observed (OBSERVE) — see CostEstimationMode's docstring.
    max_estimated_rows: Optional[int] = pyd.Field(default=None)
    max_estimated_cost: Optional[float] = pyd.Field(default=None)
    cost_estimation_mode: CostEstimationMode = pyd.Field(default=CostEstimationMode.ENFORCE)

    # Audit/explain SQL rendering. Default is safe-by-default: SQL text uses
    # bind placeholders and parameter values are redacted, so a WHERE-clause
    # literal (an email, an SSN) never ends up verbatim in the audit log or
    # in explain_structured_query's response. Set true only for deployments
    # that intentionally want full literal SQL for debugging.
    log_query_literals: bool = pyd.Field(default=False)

    # Cross-connection joins: two connections may be joined in one query only
    # when they resolve to the same join_group (defaults to the connection's
    # own id, i.e. no cross-connection joins unless explicitly configured).
    join_group: Optional[str] = None

    mandatory_row_filters: list[MandatoryRowFilter] = pyd.Field(default_factory=list)

    model_config = pyd.ConfigDict(extra="forbid")

    @property
    def cost_estimation_enabled(self) -> bool:
        return self.max_estimated_rows is not None or self.max_estimated_cost is not None

    def table_allowed(self, table_name: str) -> bool:
        name = table_name.lower()
        if any(t.lower() == name for t in self.denied_tables):
            return False
        if self.allowed_tables:
            return any(t.lower() == name for t in self.allowed_tables)
        return True

    @staticmethod
    def _ci_lookup(mapping: dict[str, list[str]], table_name: str) -> Optional[list[str]]:
        """Case-insensitive dict lookup — table-name casing in policy.yaml
        isn't guaranteed to match what schema reflection returns (dialects
        differ: Postgres lowercases unquoted identifiers, MSSQL usually
        preserves case), so an exact-string dict lookup here can silently
        fail to match a configured rule.
        """
        target = table_name.lower()
        for key, value in mapping.items():
            if key.lower() == target:
                return value
        return None

    def column_allowed(self, table_name: str, column_name: str) -> bool:
        col = column_name.lower()
        denied = (self._ci_lookup(self.denied_columns, table_name) or []) + (
            self._ci_lookup(self.denied_columns, "*") or []
        )
        if any(c.lower() == col for c in denied):
            return False
        allowed_specific = self._ci_lookup(self.allowed_columns, table_name)
        allowed_wildcard = self._ci_lookup(self.allowed_columns, "*")
        allowed = allowed_specific if allowed_specific is not None else allowed_wildcard
        if allowed is not None:
            return any(c.lower() == col for c in allowed)
        return True
