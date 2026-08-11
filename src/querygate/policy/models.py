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


def _ci_lookup(mapping: dict[str, list], key: str) -> Optional[list]:
    """Case-insensitive dict lookup, shared by `WritePolicy` and `Policy` —
    table-name casing in policy.yaml isn't guaranteed to match what schema
    reflection returns (dialects differ: Postgres lowercases unquoted
    identifiers, MSSQL usually preserves case), so an exact-string dict
    lookup here can silently fail to match a configured rule.

    Uses `.casefold()`, not `.lower()` (TODO.md item 149): a handful of real
    Unicode identifiers disagree between the two (e.g. German `"STRASSE"` vs
    `"straße"` — `.lower()` leaves them distinct, `.casefold()` unifies
    them), and every OTHER case-insensitive table-key comparison in this
    module (`Policy._merge_table_keyed`, and every table-keyed helper in
    `admin/access_diff.py`) already uses `.casefold()`. A previous version of
    this function used `.lower()`, the one holdout — found by
    `security-invariant-reviewer` while reviewing item 148's `_diff_masks`
    fix, fixed here rather than left as a live inconsistency.
    """
    target = key.casefold()
    for k, value in mapping.items():
        if k.casefold() == target:
            return value
    return None


class WritePolicy(pyd.BaseModel):
    """Governed-writes policy (TODO.md item 93). Deny-by-default: writes are OFF
    unless `enabled` is true AND the target table is in `allowed_tables` AND the
    operation is in `allowed_operations`. In Phase 1 nothing executes regardless
    — the write pipeline only ever previews (compiles, runs in a transaction,
    diffs, rolls back)."""

    enabled: bool = False
    allowed_tables: list[str] = pyd.Field(default_factory=list)
    # Which operations are permitted at all (globally); an empty list means none.
    allowed_operations: list[Literal["insert", "update", "delete", "upsert"]] = pyd.Field(
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
    # Row cap on the dry-run diff preview (item 93 phase 2b): a preview shows at
    # most this many old→new row changes; a larger affected set is reported
    # truncated. Bounds the value-bearing preview so it can never dump a table.
    max_diff_rows: int = pyd.Field(default=50, ge=1)
    # How many writes one batch call may carry (TODO.md item 109) — the write
    # sibling of `Policy.max_batch_size`, and deliberately the same default. The
    # write path is batched only over MCP (`run_structured_writes`); without this
    # a caller could submit an unbounded batch of individually-in-cap writes in
    # one call, multiplying lock time and cost per call well past what the read
    # path allows the same principal. `max_affected_rows` bounds ONE statement;
    # this bounds how many statements ride along with it.
    max_batch_size: int = pyd.Field(default=10, ge=1)

    model_config = pyd.ConfigDict(extra="forbid")

    def table_writable(self, table_name: str) -> bool:
        if not self.enabled:
            return False
        target = table_name.casefold()
        return any(t.casefold() == target for t in self.allowed_tables)

    def operation_allowed(self, op: str) -> bool:
        return self.enabled and op in self.allowed_operations

    def write_column_allowed(self, table_name: str, column_name: str) -> bool:
        col = column_name.casefold()
        # `_ci_lookup`, not a literal `.get(table_name.casefold(), [])`: the
        # PREVIOUS version of this method looked up the dict by exact key
        # match on the lowercased table name, so a `denied_write_columns` key
        # configured with any casing other than all-lowercase (e.g. the same
        # "Orders" casing `allowed_tables` legitimately uses elsewhere in the
        # same policy) never matched at all — the deny list was silently
        # inert for that table regardless of what casing the caller passed.
        # Found while fixing item 149's Policy-side casefold/lower skew;
        # this was the more severe sibling bug on the write-deny path.
        denied = (_ci_lookup(self.denied_write_columns, table_name) or []) + (
            _ci_lookup(self.denied_write_columns, "*") or []
        )
        if any(d.casefold() == col for d in denied):
            return False
        # Also honor the read denied_columns for the *_KEY convention? Kept
        # separate deliberately: a column can be readable but not writable and
        # vice versa; write policy is its own axis.
        return True


class PurposePolicyDelta(pyd.BaseModel):
    """A narrowing-only adjustment layered onto the principal's resolved
    `Policy` when a query declares this purpose (TODO.md item 145, feature
    F7 — purpose-bound access). By construction it can only take away access
    the base `Policy` already granted: there is no "allow" field here, only
    additional deny/filter/mask constraints unioned onto the base policy's
    own (`Policy.for_purpose`), so a misconfigured delta cannot grant more
    than the base policy already allows.
    """

    denied_tables: list[str] = pyd.Field(default_factory=list)
    denied_columns: dict[str, list[str]] = pyd.Field(default_factory=dict)
    mandatory_row_filters: list[MandatoryRowFilter] = pyd.Field(default_factory=list)
    column_masks: dict[str, list[ColumnMask]] = pyd.Field(default_factory=dict)

    model_config = pyd.ConfigDict(extra="forbid")


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

    # Purpose-bound access (TODO.md item 145, feature F7): the closed set of
    # purpose tokens a caller may declare (`StructuredQuery.purpose`) on this
    # connection. Empty (the default) means this connection has not opted
    # into purpose-gating at all — the same "empty allow-list = unrestricted"
    # convention as `allowed_tables` — so a declared purpose is accepted but
    # has no effect. Once non-empty, every query on this connection must
    # declare a purpose from this set (enforced in
    # `validation/policy_validation.py`).
    allowed_purposes: list[str] = pyd.Field(default_factory=list)
    # Per-purpose narrowing applied on top of the resolved policy when a query
    # declares that purpose — see `for_purpose`. A purpose with no entry here
    # is still a legitimate declaration (if listed in `allowed_purposes`)
    # that adds no further narrowing beyond the base policy.
    purpose_policies: dict[str, PurposePolicyDelta] = pyd.Field(default_factory=dict)

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
    # CROSS JOIN (TODO.md item 103) — deny by default. Every other join type is
    # required to connect to the query graph, so before item 103 a cartesian
    # product was structurally inexpressible; `cross` is the one join a caller has
    # to deliberately ask for, and the one whose cost is the PRODUCT of its inputs
    # rather than bounded by a key. Cross joins still count against `max_joins`.
    allow_cross_join: bool = pyd.Field(default=False)
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
    # How many SELECTs one set operation (item 104) may combine, counting the
    # query that carries `set_op` as arm 1, and SUMMED tree-wide like every other
    # count cap — so putting a second set operation inside an `IN (subquery)`
    # cannot multiply the budget. 0 (or 1) disables set operations entirely, the
    # same convention `max_window_specs` uses.
    #
    # Default 3 is a judgement, not a measurement, and the honest reason it can be
    # this low is that it is NOT the query's cost bound: every other cap
    # (max_joins, max_select_columns, max_where_predicates, max_expression_nodes)
    # is already summed across the arms, so N arms SHARE one budget rather than
    # each getting their own. What this cap alone bounds is the number of
    # independent scans plus the dedup sort a non-ALL set op adds on top of them.
    # Two arms is the canonical segment-union shape; three leaves room without
    # inviting an eight-way union under a default policy.
    max_set_op_arms: int = pyd.Field(default=3, ge=0)
    # How many named WITH blocks one query may declare (item 105). 0 disables ctes
    # entirely, the same convention `max_set_op_arms`/`max_window_specs` use.
    #
    # NOT summed tree-wide, and that is a property of the shape rather than an
    # exemption: only the ROOT query may declare ctes, so there is no second place
    # for a count to hide and nothing for a sum to add. Every cap that IS summable
    # (max_joins, max_select_columns, max_where_predicates, max_expression_nodes …)
    # already counts cte bodies, because `iter_query_scopes` yields them — so N
    # blocks SHARE one budget rather than each getting a fresh one. What this cap
    # alone bounds is the number of independent materialization stages.
    #
    # Default 3 is a judgement: two blocks is the canonical aggregate-then-join
    # shape and three covers dedup-then-aggregate-then-join, without a default
    # policy inviting a ten-stage pipeline. Note what does NOT bound a block —
    # `max_rows` is deliberately not applied to one (truncating intermediate work
    # is a silently wrong total), so a cte's cost is bounded by `timeout_seconds`,
    # `max_response_bytes` and the concurrency limiter, exactly as item 103's
    # cross-join entry had to state plainly rather than claim a row cap it lacks.
    max_cte_count: int = pyd.Field(default=3, ge=0)
    # How many outer columns a query tree's subqueries may DECLARE as correlated
    # (item 106), summed tree-wide. 0 disables correlation entirely — the same
    # convention max_set_op_arms/max_cte_count use, and the setting that keeps a
    # connection on the pre-106 uncorrelated model.
    #
    # This caps the correlation SURFACE, not its cost, and the distinction is the
    # honest one: a correlated subquery is re-evaluated per candidate outer row, so
    # its cost is driven by the outer scan, which no count here bounds. What bounds
    # it is timeout_seconds, the concurrency limiter, and item 26's cost gate where
    # enabled. What this bounds is how many outer columns a caller can pull into a
    # nested scope's namespace — the policy/masking surface, which is the part that
    # must not grow silently. Default 2 covers the shapes correlation exists for (a
    # single-key EXISTS, a two-column composite key).
    max_correlated_refs: int = pyd.Field(default=2, ge=0)
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

    # Caps the number of WHEN branches in any single CASE — the same
    # "structural size" guardrail philosophy as the caps above, applied to CASE
    # expressions once those became expressible in SELECT. Since item 100 it
    # covers every CaseExpr too, wherever it is nested.
    max_case_branches: int = pyd.Field(default=10)

    # The two caps that keep item 100's scalar `Expression` substrate BOUNDED
    # rather than open-ended — the third of the five non-goal-#7 boundaries
    # recorded in the 2026-07-25 Decision Log. `max_expression_depth` bounds how
    # deeply one tree may nest (a pathological deep nest is a compile/plan CPU
    # lever); `max_expression_nodes` bounds total expression size and is summed
    # TREE-WIDE across the query and every subquery (item 97's rule), so nesting
    # an expression inside a subquery cannot multiply the budget.
    max_expression_depth: int = pyd.Field(default=5, ge=1)
    max_expression_nodes: int = pyd.Field(default=200, ge=1)

    # The two caps that bound item 101's window functions. Both defaults were
    # chosen against a measurement rather than a guess — see
    # tests/integration/test_postgres_window_cost.py, which re-runs it against
    # 25,000 real rows and pins the order-of-magnitude findings (the ratios and
    # the planner's blindness), though not the individual timings quoted below.
    #
    # `max_window_specs` counts `WindowSelectItem`s summed TREE-WIDE (item 97's
    # rule); 0 disables window functions entirely for a connection. Default 5:
    # at 5 windows a query costs ~15x a plain scan of the same table, and the
    # marginal cost per window rises across the measured range (7.3 ms/window at
    # 5, 17.7 at 20). That rise is the shape argument for capping; 5 is a
    # judgement inside the measured range, not a knee three data points could
    # locate.
    #
    # `max_window_frame_offset` bounds the caller-supplied distance in an
    # `N PRECEDING`/`N FOLLOWING` frame bound and in a `lag`/`lead` offset — the
    # only unbounded magnitudes in the window AST. Default 1000, and this is the
    # load-bearing one: for an aggregate the engine cannot compute with inverse
    # transitions the work is O(rows x frame) — 12 ms at 10 preceding, 574 ms at
    # 1,000, 3.4 s at 10,000 over 25k rows, and linear in row count too, so a
    # deployment with much larger tables should lower it.
    #   * Which aggregates rescan is narrower than "MIN/MAX": Postgres provides an
    #     inverse transition for `SUM`/`AVG` over int2/int4/int8/numeric/money/
    #     interval ONLY — over `real`/`double precision` they rescan like
    #     `MIN`/`MAX`, so a float metric column pays the full cost.
    #   * Those timings are for an UNLIMITED scan, and a top-level read IS
    #     LIMIT-clamped (`clamp_limit`, default 50 / max 100) — but the clamp only
    #     bounds the rescan while the query's `order_by` is already satisfied by the
    #     window's own ordering. Order by anything else and the plan becomes
    #     `Limit -> Sort -> WindowAgg`, whose blocking sort runs the window over
    #     every row regardless: measured 0.7 ms aligned vs **583 ms** with an
    #     unrelated `ORDER BY`, both at `LIMIT 50`. So the full cost is reachable at
    #     the top level under default policy — and always inside an `IN (subquery)`,
    #     whose LIMIT is deliberately stripped.
    #   * **Postgres does not price the frame at all** (identical `EXPLAIN` cost
    #     for a 10-row and a 10,000-row frame), so item 26's cost-estimation gate
    #     is structurally blind to it there and cannot substitute for this cap;
    #     `timeout_seconds` is the only backstop behind it. MSSQL's
    #     `SHOWPLAN_XML` estimator has not been measured for this.
    #
    # Unbounded frame ends are NOT separately gated: `UNBOUNDED PRECEDING …
    # CURRENT ROW` is the running-total idiom and also SQL's own default frame,
    # and an unbounded-both-ends frame is semantically the same whole-partition
    # scan as omitting the frame — see the 2026-07-26 Decision Log entry.
    max_window_specs: int = pyd.Field(default=5, ge=0)
    max_window_frame_offset: int = pyd.Field(default=1000, ge=1)

    # The one unbounded magnitude item 102's date primitives introduce: how far
    # a single `date_add` may shift a timestamp. Checked per node against the
    # node's magnitude converted to whole days with UPPER-bound unit lengths
    # (`interval_magnitude_days`), so "31 months" cannot slip past a cap that
    # "944 days" would not.
    #
    # Be precise about what this cap is and is not. It is NOT a row-count
    # guardrail — a caller who wants to read everything simply omits the filter,
    # which `max_limit`/`clamp_limit` and the mandatory row filters bound, not
    # this. What it bounds is the magnitude reaching the database, and the
    # failure it prevents is a real one, and the direction matters: a
    # 10,000-year *lookback* (`DATEADD(year, -10000, …)`) overflows T-SQL's
    # datetime range and makes Postgres raise "timestamp out of range" — a
    # caller-triggerable server-side error rather than a typed rejection.
    # (Measured: forward 10,000 years is fine on Postgres, landing in 12026; it
    # only errors forward at ~292,000 years. MSSQL's datetime2 tops out at 9999,
    # so it errors in both directions.) It also keeps a relative-date filter an
    # honestly-bounded lookback instead of one that silently matches everything
    # ever recorded.
    #
    # Default 3,660 = 10 x 366, i.e. ten years measured the same way the cap
    # itself measures a year. That equality is the point, not a rounding: at
    # 3,653 (10 x 365.3) the cap REJECTED the most natural spelling of a ten-year
    # lookback, `{"unit": "year", "amount": -10}`, which converts to 3,660 days
    # under the upper-bound rule — while the docs described the default as
    # "~10 years". Caught in review; the fix is to make the default agree with
    # the arithmetic rather than to soften the prose.
    #
    # It sits above every realistic analytic window and well below either
    # dialect's overflow point, including when nested date_adds compound it
    # (`max_expression_depth` of 5 admits 4 nested shifts = 40 years).
    #
    # 0 permits only a zero-magnitude (no-op) shift, which is the closest thing
    # to disabling relative-date arithmetic for a connection.
    max_interval_days: int = pyd.Field(default=3660, ge=0)

    # k-anonymity guardrail (TODO.md item 88): the minimum number of underlying
    # rows any aggregate result group must be backed by. When set, the compiler
    # injects `HAVING count(*) >= min_group_size` into every aggregate query
    # (grouped or single-group), suppressing any group small enough for a caller
    # to single out an individual by aggregating over a razor-thin filter — the
    # aggregate analog of a mandatory row filter, and non-removable the same way.
    # None (the default) disables it; the floor is 2, since k=1 is no protection.
    # It bounds single-query singling-out, not multi-query differencing — see
    # docs/INFERENCE_RISKS.md (R3).
    #
    # The floor counts JOINED rows, so a join that matches many right-hand rows per
    # left-hand row would inflate the count and lift a singleton group above k
    # (TODO.md item 118). Such a join is therefore REFUSED on an aggregate query
    # while this is set — precisely: joining onto the target's primary key or a
    # unique column matches at most one row, cannot inflate a count, and is
    # allowed.
    min_group_size: Optional[int] = pyd.Field(default=None, ge=2)

    # Cumulative disclosure budget (TODO.md item 179) — the multi-query
    # counterpart to `min_group_size` directly above, which bounds only what a
    # SINGLE query may reveal. Fifty individually-legal aggregates whose
    # predicates differ by a sliding constant still reconstruct the row the
    # k-floor exists to hide; these two caps bound that reconstruction over a
    # rolling window, per (principal, connection, declared purpose, table).
    #
    # Because the recorded query shape is free of predicate literals by construction
    # (`audit/events.normalize_query_shape` — the same redaction guarantee the
    # audit event relies on), a differencing probe is the SAME shape re-sent
    # with a different constant. Repetition, not variety, is therefore the
    # signal: `max_shape_repeats_per_window` caps how many times one
    # (table, normalized-shape) pair may be re-run, and
    # `max_aggregate_queries_per_window` is the blunter backstop that caps ALL
    # aggregate queries against one table however the shape varies — which is
    # also what catches a caller varying `limit`/`offset` to manufacture a
    # fresh shape bucket.
    #
    # Both default to None (disabled) and BOTH only ever apply on a connection
    # that also sets `min_group_size`: with no k-floor there is nothing to
    # differentiate around, since the caller could read the rows directly.
    # No default threshold is recommended anywhere, in code or docs — the
    # calibration data (audit-stream replay against real traffic) does not
    # exist yet, so an operator enabling this is choosing an unvalidated
    # number and the docs say exactly that.
    max_shape_repeats_per_window: Optional[int] = pyd.Field(default=None, ge=1)
    max_aggregate_queries_per_window: Optional[int] = pyd.Field(default=None, ge=1)
    # Deliberately separate from `quota_window_seconds` (60s): a rate quota
    # bounds burst abuse over seconds, whereas differencing is patient and is
    # meaningful over hours.
    disclosure_budget_window_seconds: int = pyd.Field(default=3600, ge=1)

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

    # Real dialect-level cancellation of a running query (TODO.md item 35
    # phase 3) — deny by default, the same posture as `allow_cross_join`.
    # Cancelling an in-flight query needs a permission grant beyond what a
    # typical reporting connection has: Postgres requires the connection's
    # role to be a superuser or a member of `pg_signal_backend`
    # (`GRANT pg_signal_backend TO <role>;`); MSSQL requires the server-level
    # `ALTER ANY CONNECTION` permission or sysadmin. A cancel request is
    # rejected outright, before any DB call, unless this is explicitly set —
    # the operator sets it only AFTER granting the permission themselves, so a
    # missing grant is a deployment decision made in the open, never a runtime
    # permission error discovered mid-cancellation (2026-07-28 Decision Log).
    allow_query_cancellation: bool = pyd.Field(default=False)

    # Per-principal request/byte quota over a rolling window (TODO.md item 50).
    # max_concurrency bounds *in-flight* queries; these bound the *rate* over
    # time, so a caller that never exceeds its concurrency limit still can't
    # fire unbounded sequential queries and exhaust DB capacity or a cost
    # budget. Unset (None, the default for both caps) means the quota is
    # disabled — existing deployments behave identically. The quota is scoped
    # per principal per connection and is only enforced for an authenticated
    # caller (an anonymous/unattributable request can't be rate-limited per
    # principal, so it's skipped). Enforcement defaults to in-process — correct
    # for a single instance; setting CONCURRENCY_BACKEND=redis installs
    # `execution/redis_quota.py`'s RedisQuotaLimiter (item 50 phase 2, shipped)
    # so the window becomes one shared cross-replica budget.
    # `max_response_bytes_per_window` counts a query's response size *after* it
    # runs, so the request that crosses the byte ceiling still completes and the
    # next one is refused (rolling total already at/over the cap).
    max_requests_per_window: Optional[int] = pyd.Field(default=None, ge=1)
    max_response_bytes_per_window: Optional[int] = pyd.Field(default=None, ge=1)
    quota_window_seconds: int = pyd.Field(default=60, ge=1)

    # Pre-execution cost estimation (execution/cost_estimation.py, TODO.md
    # item 26). Unset (None, the default for both) means disabled —
    # existing deployments behave identically, and **this is the default**: with
    # neither threshold set, `cost_estimation_enabled` is False and no plan is
    # ever requested. When set, `execute()` asks the database to plan (never run)
    # the compiled query before it actually executes, and rejects the query if the
    # planner's row-count/cost estimate exceeds the configured threshold.
    # **Both dialects are supported since item 26 phase 2** — Postgres via inline
    # `EXPLAIN (FORMAT JSON)`, MSSQL via `SET SHOWPLAN_XML ON` on a dedicated
    # connection, dispatched by `StructuredQueryService._estimate_cost`. (An
    # earlier version of this comment said MSSQL "silently has no effect here";
    # that has been false since phase 2 shipped.) A dialect with no estimator
    # returns None and proceeds under the reactive guardrails.
    # `max_estimated_cost` is in Postgres's own arbitrary
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

    # The caller-facing verdict endpoint (TODO.md item 133) reports
    # allowed/denied without executing. Off by default: even parameterized SQL
    # and the touched-table list are data-dependent enough (table cardinality,
    # predicate selectivity via which tables end up referenced) to be a
    # discovery channel for a caller who could not otherwise see this
    # connection's schema. Set true only when that plan visibility is an
    # accepted tradeoff for this connection's callers.
    verdict_include_plan: bool = pyd.Field(default=False)

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

    @pyd.model_validator(mode="after")
    def _disclosure_budget_needs_a_k_floor(self) -> "Policy":
        """Reject a disclosure budget configured without `min_group_size`
        (TODO.md item 179).

        The budget only ever applies to aggregate queries on a connection that
        also sets a k-anonymity floor — with no floor the caller can read the
        rows directly, so bounding aggregate differencing protects nothing. That
        coupling is deliberate, but leaving it *silent* would be the worst kind
        of misconfiguration: the config loads clean, the caps show up in the
        admin effective-guardrails view and in an access diff, and nothing is
        ever enforced. Failing at load time means an operator finds out
        immediately rather than believing a control is live. Both fields are new
        in item 179, so no existing configuration can break on this.
        """
        if self.disclosure_budget_enabled and self.min_group_size is None:
            raise ValueError(
                "max_shape_repeats_per_window/max_aggregate_queries_per_window "
                "(the cumulative disclosure budget) require min_group_size to be "
                "set on the same policy: the budget bounds multi-query "
                "differencing around the k-anonymity floor, and without a floor "
                "there is nothing to difference around. Set min_group_size, or "
                "remove the disclosure-budget caps."
            )
        return self

    @property
    def disclosure_budget_enabled(self) -> bool:
        """True when either cumulative-disclosure cap is configured (TODO.md
        item 179). Deliberately independent of `min_group_size`: the budget
        only ever *applies* to an aggregate query on a connection that also
        sets a k-anonymity floor (see
        `execution/disclosure_budget.resolve_disclosure_budget`), but whether
        an operator configured the caps at all is a separate question from
        whether they bite on a given query.
        """
        return (
            self.max_shape_repeats_per_window is not None
            or self.max_aggregate_queries_per_window is not None
        )

    def table_allowed(self, table_name: str) -> bool:
        target = table_name.casefold()
        if any(t.casefold() == target for t in self.denied_tables):
            return False
        if self.allowed_tables:
            return any(t.casefold() == target for t in self.allowed_tables)
        return True

    def column_allowed(self, table_name: str, column_name: str) -> bool:
        col = column_name.casefold()
        denied = (_ci_lookup(self.denied_columns, table_name) or []) + (
            _ci_lookup(self.denied_columns, "*") or []
        )
        if any(c.casefold() == col for c in denied):
            return False
        allowed_specific = _ci_lookup(self.allowed_columns, table_name)
        allowed_wildcard = _ci_lookup(self.allowed_columns, "*")
        allowed = allowed_specific if allowed_specific is not None else allowed_wildcard
        if allowed is not None:
            return any(c.casefold() == col for c in allowed)
        return True

    def column_mask(self, table_name: str, column_name: str) -> Optional[ColumnMask]:
        """The mask configured for a column, or None if it's unmasked. A
        table-specific entry wins over the "*" wildcard list; within a list the
        first case-insensitive match on `column` wins. Same case-insensitive
        table lookup as `column_allowed` (see `_ci_lookup`).
        """
        col = column_name.casefold()
        for masks in (
            _ci_lookup(self.column_masks, table_name),
            _ci_lookup(self.column_masks, "*"),
        ):
            if masks is None:
                continue
            for mask in masks:
                if mask.column.casefold() == col:
                    return mask
        return None

    @staticmethod
    def _merge_table_keyed(*mappings_in_order: dict, key_preference: "list[dict]") -> dict:
        """Union table-keyed lists (`denied_columns`/`column_masks` shape)
        across mappings, resolving table names the same case-insensitive way
        `_ci_lookup` reads them back — so a base entry keyed `"customers"` and
        a delta entry keyed `"Customers"` land under ONE key instead of two.

        Without this, whichever differently-cased key a plain `dict` merge
        happens to insert wins outright at read time (`_ci_lookup` returns on
        its first case-insensitive match) — silently dropping the OTHER
        side's entries for that table entirely. For `column_masks` that is a
        real widening (a base mask vanishes); for `denied_columns` a delta's
        added deny silently fails to apply. Both are the exact "narrows never
        widens" failure item 145 exists to prevent (found by
        `security-invariant-reviewer`, 2026-08-05).

        `key_preference` is the mapping list (in priority order) whose casing
        wins as the canonical key when the same table appears under two
        different spellings — cosmetic only, since lookups are already
        case-insensitive; it just keeps the merged dict's keys stable.
        `mappings_in_order` is the order values are concatenated within one
        table's list, which IS load-bearing for `column_masks`'s first-
        column-match-wins rule.
        """
        canonical: dict[str, str] = {}
        for mapping in key_preference:
            for table in mapping:
                canonical.setdefault(table.casefold(), table)
        merged: dict[str, list] = {}
        for mapping in mappings_in_order:
            for table, values in mapping.items():
                key = canonical[table.casefold()]
                merged[key] = merged.get(key, []) + list(values)
        return merged

    def for_purpose(self, purpose: Optional[str]) -> "Policy":
        """The effective `Policy` once `purpose` is applied (TODO.md item 145).

        Callers must validate `purpose` against `allowed_purposes` themselves
        (`validation/policy_validation.py.resolve_purpose_policy` does this) —
        this method only applies the narrowing, and narrows only, by
        construction: every field `PurposePolicyDelta` carries is additive to
        a deny-list, filter list, or mask list, never a replacement or an
        "allow" that could grant more than this `Policy` already does.
        `purpose=None`, or a purpose with no configured delta, returns this
        `Policy` unchanged (not a copy) — the common case costs nothing.
        """
        if purpose is None:
            return self
        delta = self.purpose_policies.get(purpose)
        if delta is None:
            return self
        merged_columns = self._merge_table_keyed(
            self.denied_columns,
            delta.denied_columns,
            key_preference=[self.denied_columns, delta.denied_columns],
        )
        # The delta's own masks are concatenated FIRST within each table's
        # list (see `column_mask`'s first-match-wins rule on the COLUMN name)
        # so a purpose that adds a stricter mask to an otherwise-unmasked
        # column actually takes effect; a column the base policy already
        # masks keeps that mask unless the delta names the identical column,
        # in which case the purpose-specific one wins.
        merged_masks = self._merge_table_keyed(
            delta.column_masks,
            self.column_masks,
            key_preference=[self.column_masks, delta.column_masks],
        )
        return self.model_copy(
            update={
                "denied_tables": self.denied_tables + delta.denied_tables,
                "denied_columns": merged_columns,
                "mandatory_row_filters": self.mandatory_row_filters + delta.mandatory_row_filters,
                "column_masks": merged_masks,
            }
        )


# ---------------------------------------------------------------------------
# The guardrail field set (TODO.md item 115) — ONE derivation, four consumers.
#
# Four surfaces answer "which caps are in force": the semantic access diff
# (`admin/access_diff.py`, item 40), the effective-guardrails view
# (`admin/models.py`, items 45/95), the queryable product guide
# (`help/service.py`), and the admin UI's policy panel
# (`api/admin_ui_routes.py`). Each used to hand-list the fields, and all four
# had rotted: nine caps added by items 68-72, 88, 97, 100 and 101 were missing
# from at least one, so a config change that loosened `max_expression_nodes` or
# `max_window_specs` was reported to a reviewer as **no guardrail change**.
#
# Derived, not listed — the same posture as
# `sqlalchemy_compiler.DIALECT_ROUTED_EXPR_FNS`. A new cap on `Policy` is
# reported everywhere by default; a new field that is NOT a cap must be named
# below, with a reason. That inverts the failure mode: forgetting is now
# loud (a non-scalar field trips `test_policy_guardrails.py`) instead of silent.
# ---------------------------------------------------------------------------

# Policy fields that are not scalar caps. Each is excluded for a stated reason,
# NOT because it doesn't matter:
#   * the structural access rules are diffed field-by-field by access_diff
#     (tables, columns, row filters, and — since TODO.md item 148 fixed a
#     pre-existing gap where this claim was false for it — masks too) —
#     reporting them again as opaque scalars would be worse, not better;
#   * `write` is a nested WritePolicy with its own caps; diffing those needs its
#     own change category and is deliberately out of scope here;
#   * `approval_sensitivities` is a list of labels, so it has no scalar
#     permissiveness — access_diff reports it with its own dedicated change.
_NON_GUARDRAIL_POLICY_FIELDS = frozenset(
    {
        "enabled",
        "allowed_tables",
        "denied_tables",
        "allowed_columns",
        "denied_columns",
        "column_masks",
        "mandatory_row_filters",
        "join_group",
        "write",
        "approval_sensitivities",
        # TODO.md item 145: structural access rules, the same reason
        # allowed_tables/denied_columns/mandatory_row_filters are excluded —
        # a list and a dict of narrowing deltas have no scalar permissiveness
        # to compare, and reporting them as opaque scalars would be worse
        # than not reporting them at all. Diffed field-by-field by
        # `admin/access_diff.py::_diff_purposes` instead (found missing by
        # `security-invariant-reviewer`/`architecture-boundary-reviewer`
        # 2026-08-05, then added).
        "allowed_purposes",
        "purpose_policies",
    }
)

# Declaration order, so every consumer reports the same fields in the same
# deterministic order without maintaining an order of its own.
GUARDRAIL_FIELDS: tuple[str, ...] = tuple(
    name for name in Policy.model_fields if name not in _NON_GUARDRAIL_POLICY_FIELDS
)

# Caps where a HIGHER value is MORE restrictive — the opposite of every other
# entry, and the trap a naive "just add the field names" fix falls into.
# `min_group_size`: a larger k suppresses more result groups.
# `quota_window_seconds`: the same request budget spread over a longer window is
# a lower sustained rate.
# `disclosure_budget_window_seconds`: identical reasoning (item 179) — the same
# probe budget spread over a longer window is a lower sustained probe rate, so
# LENGTHENING it is the tighter posture.
INVERTED_GUARDRAIL_FIELDS = frozenset(
    {"min_group_size", "quota_window_seconds", "disclosure_budget_window_seconds"}
)

# Deriving the field set fixes "a new cap is invisible", but a new cap could
# still be diffed in the WRONG DIRECTION — the same silent-wrongness one layer
# down. Direction is guessable from the name for the common case (`max_*` is a
# ceiling: higher = looser), so those need no ceremony. Every guardrail whose
# name does NOT say which way it runs must be listed here, meaning its direction
# was actually considered; `test_policy_guardrails.py` fails until it is. Being
# in this set is not a claim about direction — `INVERTED_GUARDRAIL_FIELDS` above
# is that — only that someone decided.
_DIRECTION_REVIEWED_GUARDRAILS = frozenset(
    {
        "default_limit",  # rows returned when the caller asks for none: higher = looser
        "timeout_seconds",  # longer query budget = looser
        "concurrency_wait_seconds",  # longer admission wait = looser
        "quota_window_seconds",  # INVERTED: same budget over longer = lower rate
        # INVERTED, same reasoning as quota_window_seconds (item 179): the same
        # number of permitted probes spread over a longer window is a lower
        # sustained probe rate, so a longer window is the TIGHTER setting.
        "disclosure_budget_window_seconds",
        "min_group_size",  # INVERTED: a larger k-anonymity floor hides more
        "cost_estimation_mode",  # OBSERVE never blocks; ENFORCE can
        "log_query_literals",  # logging raw literals is the looser posture
        # Permitting the one join whose cost is a cartesian product is looser —
        # the normal direction, but "allow_*" does not read as a ceiling, so it
        # is stated rather than guessed (item 103).
        "allow_cross_join",
        # Same "allow_* does not read as a ceiling" reasoning as allow_cross_join
        # — permitting cancellation is the looser posture (item 35 phase 3).
        "allow_query_cancellation",
        # Approval thresholds, not caps: a HIGHER threshold means fewer queries
        # are stopped for a human, so higher is looser — the normal direction,
        # but stated because "approval_max_*" does not read like a ceiling on
        # what a query may do. (This test caught both of these on its first run,
        # which is the point of it.)
        "approval_max_estimated_rows",
        "approval_max_estimated_cost",
        # Revealing the compiled plan on the verdict endpoint (item 133) is the
        # looser posture — same "log_*"/"allow_*" reasoning as the two entries
        # above this comment: the name doesn't read as a ceiling.
        "verdict_include_plan",
    }
)
