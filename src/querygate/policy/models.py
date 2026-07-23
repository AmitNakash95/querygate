"""Per-connection policy — the enforcement surface between an agent and real SQL.

A `Policy` is resolved per connection (see policy/loader.py) and checked by
validation/policy_validation.py before a query is ever compiled or executed.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Optional

import pydantic as pyd

from querygate.catalog.models import SensitivityClass
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


class ColumnMaskKind(StrEnum):
    """How a `ColumnMask` transforms a column's value in the compiled Select.

    HASH  — a deterministic one-way hash (real value never leaves the DB).
    NULL  — always NULL (the value is fully suppressed but the column stays
            selectable, unlike a denied column which can't be referenced).
    LAST  — reveal only the trailing `length` characters (e.g. last-4 of a
            card number); everything else is dropped by the DB.
    BUCKET — round a numeric value down to a multiple of `bucket_size`
            (e.g. a salary bucketed to the nearest 10_000).
    """

    HASH = "hash"
    NULL = "null"
    LAST = "last"
    BUCKET = "bucket"


class ColumnMask(pyd.BaseModel):
    """A per-column value transform applied in the compiled Select so the
    database itself never returns the raw value to a masked caller (TODO.md
    item 49). Unlike allow/deny — which is binary — a mask grants *partial*
    visibility of a column.

    A masked column may only appear as a bare SELECT projection item. Using it
    in a filter/join/order/group position, or nested inside a function/CASE/
    aggregate, is rejected by policy_validation (an unmasked reference there
    would leak the real value via inference), not silently masked-in-place.
    """

    column: str
    kind: ColumnMaskKind
    # LAST: number of trailing characters to reveal. BUCKET: numeric bucket
    # width. HASH/NULL take no parameters.
    length: Optional[int] = None
    bucket_size: Optional[float] = None

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _check_params(self) -> "ColumnMask":
        if self.kind is ColumnMaskKind.LAST:
            if self.length is None or self.length <= 0:
                raise ValueError(f"column_mask kind 'last' on {self.column!r} needs length > 0")
            if self.bucket_size is not None:
                raise ValueError(f"column_mask kind 'last' on {self.column!r} forbids bucket_size")
        elif self.kind is ColumnMaskKind.BUCKET:
            if self.bucket_size is None or self.bucket_size <= 0:
                raise ValueError(
                    f"column_mask kind 'bucket' on {self.column!r} needs bucket_size > 0"
                )
            if self.length is not None:
                raise ValueError(f"column_mask kind 'bucket' on {self.column!r} forbids length")
        else:  # HASH / NULL take no parameters
            if self.length is not None or self.bucket_size is not None:
                raise ValueError(
                    f"column_mask kind {self.kind.value!r} on {self.column!r} takes no parameters"
                )
        return self


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


class WritePolicy(pyd.BaseModel):
    """Governed-writes policy (TODO.md item 93). Deny-by-default: writes are OFF
    unless `enabled` is true AND the target table is in `allowed_tables` AND the
    operation is in `allowed_operations`. In Phase 1 nothing executes regardless
    — the write pipeline only ever previews (compiles, runs in a transaction,
    diffs, rolls back)."""

    enabled: bool = False
    allowed_tables: list[str] = pyd.Field(default_factory=list)
    # Which operations are permitted at all (globally); an empty list means none.
    allowed_operations: list[Literal["insert", "update", "delete"]] = pyd.Field(
        default_factory=list
    )
    # Columns that may never be written, keyed by table ("*" = every table) —
    # the write analogue of denied_columns (e.g. id, created_at, tenant_id).
    denied_write_columns: dict[str, list[str]] = pyd.Field(default_factory=dict)
    # Hard cap on how many rows a single previewed/executed write may affect.
    max_affected_rows: int = pyd.Field(default=100, ge=1)
    # In-query approval trigger for writes (item 93 phase 2, reuses item 92): a
    # write whose affected-row count exceeds this pauses for a human sign-off
    # before it commits (REST 428 -> query:approve token -> resubmit). None =
    # no approval gate on writes; 0 = require approval for *every* write; N =
    # require it only for writes touching more than N rows. A softer gate below
    # the hard `max_affected_rows` reject, exactly like the read gate.
    require_approval_over_rows: Optional[int] = pyd.Field(default=None, ge=0)

    model_config = pyd.ConfigDict(extra="forbid")

    def table_writable(self, table_name: str) -> bool:
        if not self.enabled:
            return False
        name = table_name.lower()
        return any(t.lower() == name for t in self.allowed_tables)

    def operation_allowed(self, op: str) -> bool:
        return self.enabled and op in self.allowed_operations

    def write_column_allowed(self, table_name: str, column_name: str) -> bool:
        col = column_name.lower()
        for key in (table_name.lower(), "*"):
            for denied in self.denied_write_columns.get(key, []):
                if denied.lower() == col:
                    return False
        # Also honor the read denied_columns for the *_KEY convention? Kept
        # separate deliberately: a column can be readable but not writable and
        # vice versa; write policy is its own axis.
        return True


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

    # Governed writes (TODO.md item 93), deny-by-default and preview-only in
    # Phase 1. A read-only deployment leaves this at its default (writes off).
    write: WritePolicy = pyd.Field(default_factory=WritePolicy)

    # Per-column value masks (TODO.md item 49) — keyed by table name with "*"
    # applying to every table, the same convention as allowed/denied_columns.
    # A mask grants partial visibility of a column that is otherwise permitted;
    # deny always wins, so a denied column is unreachable before a mask is ever
    # consulted (no load-time cross-check needed).
    column_masks: dict[str, list[ColumnMask]] = pyd.Field(default_factory=dict)

    # Query complexity caps.
    max_joins: int = pyd.Field(default=5)
    max_select_columns: int = pyd.Field(default=30)
    max_where_depth: int = pyd.Field(default=5)
    max_group_by: int = pyd.Field(default=10)
    # Bounds caller-authored subquery nesting (Predicate.value_subquery, an
    # `IN (subquery)`; TODO.md item 97). Default 1 = at most one level of nesting.
    # 0 disables nested subqueries entirely. All the count caps above
    # (max_joins/max_select_columns/max_group_by/max_where_predicates/top_n) are
    # additionally enforced SUMMED across the whole query tree, so nesting can
    # never multiply the effective cap.
    max_subquery_depth: int = pyd.Field(default=1, ge=0)
    max_limit: int = pyd.Field(default=100)
    max_limit_aggregate: int = pyd.Field(default=1000)
    default_limit: int = pyd.Field(default=50)
    max_top_n: int = pyd.Field(default=50)
    max_partition_by: int = pyd.Field(default=5)
    max_batch_size: int = pyd.Field(default=10)

    # Resource-exhaustion guardrails on WHERE/HAVING shape that the other caps
    # above don't cover: max_where_depth bounds *nesting*, not the total
    # number of predicate leaves, so a single-level `or` with thousands of
    # terms passes depth checks while still compiling into a huge boolean
    # expression. max_in_list_size separately bounds one `in`/`not_in`
    # predicate's value list, since that's an unbounded-size field with no
    # other cap touching it.
    max_where_predicates: int = pyd.Field(default=100)
    max_in_list_size: int = pyd.Field(default=1000)

    # Caps the number of WHEN branches in any single CaseSelectItem — the
    # same "structural size" guardrail philosophy as the caps above, applied
    # to CASE expressions once those became expressible in SELECT.
    max_case_branches: int = pyd.Field(default=10)

    # k-anonymity guardrail (TODO.md item 88): the minimum number of underlying
    # rows any aggregate result group must be backed by. When set, the compiler
    # injects `HAVING count(*) >= min_group_size` into every aggregate query
    # (grouped or single-group), suppressing any group small enough for a caller
    # to single out an individual by aggregating over a razor-thin filter — the
    # aggregate analog of a mandatory row filter, and non-removable the same way.
    # None (the default) disables it; the floor is 2, since k=1 is no protection.
    # It bounds single-query singling-out, not multi-query differencing — see
    # docs/INFERENCE_RISKS.md (R3).
    min_group_size: Optional[int] = pyd.Field(default=None, ge=2)

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

    # Queue-depth pressure controls (TODO.md item 35 phase 2). Unset/None
    # (both defaults) means unlimited — identical to pre-phase-2 behavior.
    # max_concurrency bounds *running* queries; these bound *waiting* ones,
    # so a `queue_mode=wait` caller pile-up can't itself become a resource-
    # exhaustion vector. A caller whose admission would exceed either cap is
    # rejected immediately (before it starts waiting at all), the same way a
    # `queue_mode=fail_fast` caller is, but with a distinct `queue_full`
    # admission_state so it can be told apart from a genuine wait-timeout.
    max_queue_depth: Optional[int] = pyd.Field(default=None, ge=0)
    # Same idea, scoped to one principal on one connection — stops a single
    # noisy caller from exhausting the whole connection's queue budget even
    # when max_queue_depth still has headroom.
    max_queue_depth_per_principal: Optional[int] = pyd.Field(default=None, ge=0)

    # Per-principal request/byte quota over a rolling window (TODO.md item 50).
    # max_concurrency bounds *in-flight* queries; these bound the *rate* over
    # time, so a caller that never exceeds its concurrency limit still can't
    # fire unbounded sequential queries and exhaust DB capacity or a cost
    # budget. Unset (None, the default for both caps) means the quota is
    # disabled — existing deployments behave identically. The quota is scoped
    # per principal per connection and is only enforced for an authenticated
    # caller (an anonymous/unattributable request can't be rate-limited per
    # principal, so it's skipped). Enforcement is in-process — correct for a
    # single instance; a Redis-backed cross-replica quota is item 50 phase 2.
    # `max_response_bytes_per_window` counts a query's response size *after* it
    # runs, so the request that crosses the byte ceiling still completes and the
    # next one is refused (rolling total already at/over the cap).
    max_requests_per_window: Optional[int] = pyd.Field(default=None, ge=1)
    max_response_bytes_per_window: Optional[int] = pyd.Field(default=None, ge=1)
    quota_window_seconds: int = pyd.Field(default=60, ge=1)

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

    # In-query human-in-the-loop approval gate (execution/approval.py, TODO.md
    # item 92 phase 1). Unset (None, the default for both) means the gate is
    # off — existing deployments behave identically. When set, a query whose
    # pre-execution estimate exceeds the threshold is paused with
    # ApprovalRequiredError unless the caller supplies a valid approval token
    # (issued by a `query:approve` holder). These are a *softer* gate than
    # max_estimated_rows/max_estimated_cost above: set them LOWER than the hard
    # reject caps to mean "ask a human" rather than "refuse". Postgres-only in
    # phase 1 (same estimate source as cost estimation); the catalog
    # sensitivity-label trigger and MCP elicitation channel are phase 2.
    # Enabling the gate requires AppConfig.approval_token_hmac_key to be set,
    # otherwise no approval could ever be granted (fail-closed).
    approval_max_estimated_rows: Optional[int] = pyd.Field(default=None)
    approval_max_estimated_cost: Optional[float] = pyd.Field(default=None)
    # Sensitivity-label trigger (item 92 phase 2): a query that references a
    # column (or its table) carrying one of these catalog sensitivity labels
    # requires approval regardless of its estimated size — the "what it would
    # touch" half of the gate. Empty (default) means off. Dialect-agnostic and
    # needs no cost estimate, so it works on MSSQL too. Reads only the static
    # catalog label (metadata), never a row value — the catalog stays
    # descriptive, never a query/row-value path.
    approval_sensitivities: list[SensitivityClass] = pyd.Field(default_factory=list)

    # Audit/explain SQL rendering. Default is safe-by-default: SQL text uses
    # bind placeholders and parameter values are redacted, so a WHERE-clause
    # literal (an email, an SSN) never ends up verbatim in the audit log or
    # in run_structured_queries(mode="explain")'s response. Set true only
    # for deployments that intentionally want full literal SQL for debugging.
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

    @property
    def approval_cost_gate_enabled(self) -> bool:
        return (
            self.approval_max_estimated_rows is not None
            or self.approval_max_estimated_cost is not None
        )

    @property
    def approval_gate_enabled(self) -> bool:
        return self.approval_cost_gate_enabled or bool(self.approval_sensitivities)

    @property
    def estimate_needed(self) -> bool:
        """Whether `execute()` must obtain the pre-execution estimate — true when
        the hard cost gate or the cost-based approval gate is configured (the
        sensitivity-label approval trigger needs no estimate), so the estimate is
        computed once and fed to both."""
        return self.cost_estimation_enabled or self.approval_cost_gate_enabled

    @property
    def query_quota_enabled(self) -> bool:
        return (
            self.max_requests_per_window is not None
            or self.max_response_bytes_per_window is not None
        )

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

    def column_mask(self, table_name: str, column_name: str) -> Optional[ColumnMask]:
        """The mask configured for a column, or None if it's unmasked. A
        table-specific entry wins over the "*" wildcard list; within a list the
        first case-insensitive match on `column` wins. Same case-insensitive
        table lookup as `column_allowed` (see `_ci_lookup`).
        """
        col = column_name.lower()
        for masks in (
            self._ci_lookup(self.column_masks, table_name),
            self._ci_lookup(self.column_masks, "*"),
        ):
            if masks is None:
                continue
            for mask in masks:
                if mask.column.lower() == col:
                    return mask
        return None
