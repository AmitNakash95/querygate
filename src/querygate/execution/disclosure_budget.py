"""Cumulative disclosure budget over a rolling window (TODO.md item 179).

`Policy.min_group_size` (item 88) bounds what a *single* aggregate query may
reveal: the compiler injects `HAVING count(*) >= k`, so no one query returns a
group backed by fewer than *k* rows. It says nothing about what a caller
reconstructs from *many* individually-legal queries — fifty aggregates whose
predicates differ only by a sliding constant still isolate the row the k-floor
exists to hide. Item 88's own write-up says so ("multi-query differencing stays
honestly out of scope"), `docs/INFERENCE_RISKS.md` carries it as residual R3,
and `docs/THREAT_MODEL.md` QG-29 lists it among the residuals identifier
allow/deny structurally cannot close. This module bounds it.

**Repetition is the signal, not variety.** The shape this module counts comes
from `audit/events.normalize_query_shape`, which records operators, column
identifiers, `group_by` keys and join structure but strips predicate literals —
the same redaction-safe projection the persisted audit event relies on, so
budgeting reopens nothing (non-negotiable #3). The consequence is the design's
central fact: a differencing probe (`… WHERE age > 40`, `> 41`, `> 42`, …) is
ONE normalized shape re-sent N times, while a genuine exploration is N
different shapes. So the budget counts **re-runs of a shape**, and a design
that counted *distinct* shapes would have missed the attack completely.

Two caps, both `None` (disabled) by default, either one tripping refuses:

* `Policy.max_shape_repeats_per_window` — how many times one
  (table, normalized-shape) pair may be re-run. The targeted probe cap.
* `Policy.max_aggregate_queries_per_window` — how many aggregate queries may
  touch one table however the shape varies. The blunt backstop, and what makes
  the shape cap non-trivial to evade: `normalize_query_shape` deliberately
  retains a few caller-supplied numbers (`requested_limit`, `offset`,
  `top_n.n`, a percentile `fraction`), so without this second cap a caller
  could mint a fresh shape bucket per probe just by walking `limit`.
  `shape_fingerprint` below strips those same numbers before hashing, which
  closes the evasion at the shape layer too — the two are belt and braces.

**Scope of the key: `(connection_id, principal, purpose, table)`.** Principal,
not actor: under item 90 the principal IS the human whose policy applied (the
Proof-pillar subject); keying on the actor would budget an entire agent fleet
as one identity and let one agent deny service to every other. Purpose is in
the key so each declared purpose (item 145) carries its own independent budget
— which is what makes a purpose bound *cumulative* disclosure rather than only
narrowing one query at a time. Note this deliberately does NOT put a cap on
`PurposePolicyDelta`: every field `Policy.for_purpose` narrows today is a
list/dict it unions, and `validation/policy_validation.validate_structural_caps`
documents that its monotonicity argument breaks if the delta ever gains a
subtractive field. Keying the window by purpose gets the same product outcome
without touching that argument. A per-purpose *cap* remains available later as
a deliberate, separately-reviewed change.

**Applies only where there is a k-floor to defend.** Both caps are inert unless
the effective policy also sets `min_group_size` and the scope actually
aggregates. Without a k-floor the caller can read the rows directly (subject to
the ordinary column/row policy), so bounding aggregate *differencing* would cost
availability and buy nothing.

Enforcement shape mirrors `execution/quota.py` and `execution/concurrency.py`:
a narrow Protocol with an in-process default and a Redis-backed cross-replica
sibling (`execution/redis_disclosure_budget.py`), dispatched through the active
instance. Unlike the quota there is no reserve/record split — a disclosure
charge is fully known before execution and carries no post-hoc byte weight.

**Charges are all-or-nothing.** One query can charge several keys (a per-table
counter and a per-shape counter, for each table it aggregates over). `reserve`
checks every cap first and only then records any of them, so a query refused by
its third key has not silently spent budget on its first two.

**This is a bound, not a closure.** A caller inside its budget still
differences successfully, and because the shape carries no predicate literals the budget
cannot distinguish a probe from an innocent repeat of the same query — it is
deliberately conservative and will count benign repetition. Closing R3 properly
needs query-set auditing or differential privacy, neither of which this is. Do
not describe it as closing multi-query differencing.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter
from typing import Any, Dict, FrozenSet, List, Optional, Protocol, Sequence, Tuple

from querygate.audit.events import normalize_query_shape
from querygate.core.exceptions import DisclosureBudgetExceededError
from querygate.policy.models import Policy
from querygate.query_ast.models import StructuredQuery, is_aggregate_scope
from querygate.validation.schema_validation import (
    effective_name_map,
    iter_query_scopes,
)

# (connection_id, principal_subject, purpose, table, shape_fingerprint).
# `purpose` is "" when the query declared none; `shape_fingerprint` is "" for
# the per-table counter, which deliberately ignores shape.
DisclosureBudgetKey = Tuple[str, str, str, str, str]

# One charge: which window, its cap, which cap it is (for the rejection and the
# metric), and how many units this query costs against it. The weight is what
# stops a single statement buying several probe answers — see
# `budgeted_occurrences`.
Charge = Tuple[DisclosureBudgetKey, int, str, int]

# Kinds reported on the rejection and the metrics counter. A fixed, closed set
# — never caller-derived — so the label cardinality stays bounded.
KIND_SHAPE = "disclosure_shape"
KIND_TABLE = "disclosure_table"

# Numeric fields `normalize_query_shape` legitimately retains (they are
# structure, not predicate literals — see its own docstring), but which a
# caller can vary freely without changing what the query *discloses*. Dropped
# before fingerprinting so walking `limit` cannot mint a fresh shape bucket per
# probe. Collapsing two genuinely-different shapes into one bucket is the safe
# direction: it makes the cap trip sooner, never later.
_VOLATILE_SHAPE_KEYS = frozenset({"requested_limit", "offset", "n", "fraction", "alias"})


def _canonicalize(node: Any, aliases: Dict[str, str]) -> Any:
    """Recursively drop `_VOLATILE_SHAPE_KEYS` and canonicalize identifiers.

    Two normalizations, both of which exist to stop a prober minting a fresh
    shape bucket for a query that is semantically identical:

    * **Case.** `normalize_query_shape` records identifiers with the caller's
      own casing, but every other identifier comparison in the policy layer is
      `.casefold()`-insensitive (items 149/150), and the database resolves
      `Employees.Id` and `employees.id` to the same column. Folding every
      string keeps the fingerprint consistent with that. Safe to apply
      indiscriminately because the shape carries no predicate literals by construction —
      there is no user data in it to corrupt, only identifiers, operators and
      structural keywords.
    * **Aliases.** A column reference carries the *effective* name
      (`emp.salary`), so renaming an alias would otherwise change the shape
      without changing the query. Any dotted reference whose prefix is a
      declared effective name is rewritten to the physical table
      (`employees.salary`).

    A self-join collapses (`emp.id` and `mgr.id` both become `employees.id`),
    merging two distinct shapes into one counter. That is the conservative
    direction — the cap trips sooner, never later.
    """
    if isinstance(node, dict):
        return {
            key: _canonicalize(value, aliases)
            for key, value in node.items()
            if key not in _VOLATILE_SHAPE_KEYS
        }
    if isinstance(node, list):
        return [_canonicalize(item, aliases) for item in node]
    if isinstance(node, str):
        folded = node.casefold()
        prefix, sep, rest = folded.partition(".")
        if sep and prefix in aliases:
            return f"{aliases[prefix]}{sep}{rest}"
        return folded
    return node


def shape_fingerprint(query: StructuredQuery) -> str:
    """A stable, redaction-safe fingerprint of one scope's query *shape*.

    Built from `normalize_query_shape` — so it contains identifiers, operators
    and structure but never a predicate literal, a row, or a credential — with
    the volatile numeric fields stripped and identifiers canonicalized (see
    `_canonicalize`), then serialized deterministically and hashed. The digest
    is truncated to 32 hex chars: this is a bucketing key for a rolling window,
    not a security boundary, and a collision merges two shapes into one counter
    (again, the conservative direction).
    """
    aliases = {
        effective.casefold(): physical.casefold()
        for effective, physical in effective_name_map(query).items()
    }
    canonical = json.dumps(
        _canonicalize(normalize_query_shape(query), aliases),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


class _WindowEntry:
    __slots__ = ("ts",)

    def __init__(self, ts: float) -> None:
        self.ts = ts


class DisclosureBudgetLimiter(Protocol):
    """The one operation `enforce_disclosure_budget` needs. Implemented by
    `InProcessDisclosureBudgetLimiter` (single-process) and
    `RedisDisclosureBudgetLimiter` (`execution/redis_disclosure_budget.py`,
    cross-replica), dispatched behind this interface exactly like the quota and
    concurrency limiters. Async so the Redis backend can await its client.
    """

    async def reserve(
        self,
        charges: Sequence[Charge],
        *,
        window_seconds: int,
        now: Optional[float] = None,
    ) -> None:
        """Charge `weight` units against every `(key, limit, kind, weight)` or
        raise `DisclosureBudgetExceededError`, charging nothing at all if any
        cap would be exceeded."""
        ...


class InProcessDisclosureBudgetLimiter:
    """Single-process rolling-window limiter — the default, correct only for one
    instance. Keeps a sliding log of per-key timestamps; each `reserve()` prunes
    entries older than `window_seconds` first, so the window genuinely rolls
    rather than resetting on a fixed boundary (same shape as
    `quota.InProcessQuotaLimiter`).
    """

    def __init__(self) -> None:
        self._windows: Dict[DisclosureBudgetKey, List[_WindowEntry]] = {}
        self._last_sweep = 0.0

    def clear(self) -> None:
        """Reset all in-process budget state. Called once per test by the
        conftest autouse fixture, for the same reason the concurrency and quota
        limiters are: window state seeded by one test must not leak into the
        next."""
        self._windows.clear()
        # `_last_sweep` is state too, and `reserve()` accepts an explicit `now`:
        # a test that sweeps at a large `now` would otherwise leave the clock
        # ahead, so a later test passing a smaller `now` could never satisfy
        # `now - _last_sweep > window_seconds` and its sweep would silently not
        # run. No production impact (`time.monotonic()` never goes backwards) —
        # this keeps the docstring's "all state" promise literally true.
        self._last_sweep = 0.0

    def _prune(self, key: DisclosureBudgetKey, cutoff: float) -> List[_WindowEntry]:
        entries = [e for e in self._windows.get(key, ()) if e.ts > cutoff]
        if entries:
            self._windows[key] = entries
        else:
            self._windows.pop(key, None)
        return entries

    def _sweep(self, cutoff: float) -> None:
        """Drop every key whose newest entry has aged out.

        `_prune` only ever touches keys named in the current charge list, so
        without this a key charged once and never charged again would live for
        the process's lifetime. That matters here in a way it does not for
        `quota.py`, whose key is just (connection, principal) and is therefore
        bounded by the principal roster: this key also carries the query's shape
        fingerprint, so its cardinality grows with the number of *distinct
        queries* a caller has ever run. The Redis backend gets this for free
        from its per-key TTL; the in-process default needs to do it explicitly.
        """
        stale = [
            key for key, entries in self._windows.items() if not entries or entries[-1].ts <= cutoff
        ]
        for key in stale:
            del self._windows[key]

    async def reserve(
        self,
        charges: Sequence[Charge],
        *,
        window_seconds: int,
        now: Optional[float] = None,
    ) -> None:
        now = time.monotonic() if now is None else now
        cutoff = now - window_seconds
        # Amortized: sweeping is O(tracked keys), so it runs at most once per
        # window rather than on every request.
        if now - self._last_sweep > window_seconds:
            self._sweep(cutoff)
            self._last_sweep = now
        # Phase 1: prune and check EVERY cap before charging any of them, so a
        # query refused on its last key hasn't spent budget on its earlier ones.
        pruned: List[Tuple[DisclosureBudgetKey, List[_WindowEntry], int]] = []
        for key, limit, kind, weight in charges:
            entries = self._prune(key, cutoff)
            if len(entries) + weight > limit:
                oldest = entries[0].ts if entries else now
                retry_after = max(1, math.ceil(window_seconds - (now - oldest)))
                raise DisclosureBudgetExceededError(
                    rejection_message(kind, limit, window_seconds, retry_after),
                    quota_kind=kind,
                    retry_after_seconds=retry_after,
                )
            pruned.append((key, entries, weight))
        # Phase 2: no cap tripped, so charge them all. No await between the two
        # phases, so nothing can interleave and invalidate the checks above.
        for key, entries, weight in pruned:
            entries.extend(_WindowEntry(now) for _ in range(weight))
            self._windows[key] = entries


def rejection_message(kind: str, limit: int, window_seconds: int, retry_after: int) -> str:
    """The caller-visible refusal text.

    Names the cap and the window but deliberately NOT the table or the query
    shape: which table is near its disclosure budget is itself a disclosure
    channel, and echoing the shape back would confirm to a prober exactly which
    of its variants the server considers identical.
    """
    if kind == KIND_SHAPE:
        return (
            f"disclosure budget exceeded: this query shape may be re-run at most "
            f"{limit} times per {window_seconds}s window against the same table; "
            f"retry in ~{retry_after}s"
        )
    return (
        f"disclosure budget exceeded: at most {limit} aggregate queries per "
        f"{window_seconds}s window against the same table; retry in ~{retry_after}s"
    )


_in_process_limiter = InProcessDisclosureBudgetLimiter()
_active_limiter: DisclosureBudgetLimiter = _in_process_limiter


def in_process_disclosure_budget_limiter() -> InProcessDisclosureBudgetLimiter:
    """The persistent in-process limiter — used by tests to seed/reset window
    state and by the conftest autouse fixture to clear it, regardless of which
    backend is active (mirrors `quota.in_process_quota_limiter()`)."""
    return _in_process_limiter


def init_redis_disclosure_budget_limiter(limiter: DisclosureBudgetLimiter) -> None:
    """Install the Redis-backed cross-replica limiter as active. Called from
    `create_app` when the Redis backend is selected, mirroring
    `quota.init_redis_quota_limiter`."""
    global _active_limiter
    _active_limiter = limiter


def clear_redis_disclosure_budget_limiter() -> None:
    """Revert to the in-process limiter (test teardown / shutdown)."""
    global _active_limiter
    _active_limiter = _in_process_limiter


def _cte_base_tables(query: StructuredQuery) -> Dict[str, FrozenSet[str]]:
    """Case-folded cte name -> the physical tables its body ultimately reads.

    A cte is not a physical table, so an aggregating scope that reads one must
    be charged against whatever real tables sit behind it. Resolution is
    transitive: a cte reading an earlier cte inherits that cte's base tables.
    Declaration order is sufficient for the walk because
    `_validate_cte_constraints` guarantees a cte may only reference an EARLIER
    one (no forward or self reference), so every name is resolved before it can
    be referenced.
    """
    resolved: Dict[str, FrozenSet[str]] = {}
    for spec in query.ctes:
        tables: set[str] = set()
        for _depth, scope in iter_query_scopes(spec.query):
            for table in [scope.from_table, *(join.table for join in scope.joins)]:
                key = table.casefold()
                tables |= resolved.get(key, frozenset({key}))
        resolved[spec.name.casefold()] = frozenset(tables)
    return resolved


def budgeted_occurrences(query: StructuredQuery) -> Counter:
    """`(physical table, shape fingerprint) -> how many units this query costs`.

    Walks `iter_query_scopes` — the single canonical scope walker policy and
    schema validation already use (item 96 doctrine) — so a cte body, a
    set-operation arm and a nested `IN (subquery)` are each considered on their
    own terms rather than only the outer SELECT. A scope that does not aggregate
    contributes nothing: the k-floor this budget defends only applies to
    aggregates.

    **Every aggregating scope costs its own unit.** An earlier draft charged a
    table at most once per query, which let one statement buy several probe
    answers: a `UNION ALL` of three aggregates over the same table with three
    different constants returns three answers, and `max_set_op_arms` (3 by
    default) would have multiplied the operator's configured bound accordingly.
    Counting occurrences keeps "N units per window" meaning N probe answers.

    **A cte name is resolved to its base tables, not skipped.** Skipping it was
    a complete bypass: wrap the table in a *non-aggregating* cte
    (`WITH src AS (SELECT dept, salary FROM employees WHERE salary > X)`) and
    aggregate over `src` in the outer scope. The cte body contributes nothing
    (it does not aggregate) and the outer scope's only table was the cte name —
    so the whole query charged nothing, while the compiler still applied the
    `min_group_size` floor to the outer scope and returned a real answer. The
    caller could then difference indefinitely. `_cte_base_tables` resolves
    `src` back to `employees`, charged with the *aggregating* scope's
    fingerprint.
    """
    occurrences: Counter = Counter()
    cte_bases = _cte_base_tables(query)
    for _depth, scope in iter_query_scopes(query):
        if not is_aggregate_scope(scope):
            continue
        fingerprint = shape_fingerprint(scope)
        for table in [scope.from_table, *(join.table for join in scope.joins)]:
            key = table.casefold()
            for physical in sorted(cte_bases.get(key, frozenset({key}))):
                occurrences[(physical, fingerprint)] += 1
    return occurrences


def resolve_disclosure_budget(policy: Policy) -> Optional[Tuple[Optional[int], Optional[int], int]]:
    """`(max_shape_repeats, max_aggregate_queries, window_seconds)` if this
    policy's disclosure budget can apply, else `None`.

    Returns `None` when neither cap is set, and ALSO when `min_group_size` is
    unset: with no k-anonymity floor there is no aggregate-only disclosure to
    difference around (the caller could read the rows directly), so the budget
    would cost availability and protect nothing.

    `Policy` now rejects that combination outright at load time
    (`_disclosure_budget_needs_a_k_floor`), so the second condition is
    unreachable from a validated policy. It is kept as a defence in depth
    because a `Policy` can also be constructed in-process (tests, the admin
    simulation path), and the failure mode it guards — a budget silently not
    applying while the config claims it does — is exactly the one worth being
    redundant about.
    """
    if not policy.disclosure_budget_enabled or policy.min_group_size is None:
        return None
    return (
        policy.max_shape_repeats_per_window,
        policy.max_aggregate_queries_per_window,
        policy.disclosure_budget_window_seconds,
    )


async def enforce_disclosure_budget(
    query: StructuredQuery,
    policy: Policy,
    *,
    connection_id: str,
    principal_subject: Optional[str],
) -> None:
    """Charge this query against its principal's cumulative disclosure budget,
    or raise `DisclosureBudgetExceededError`.

    A no-op when the budget is disabled or inapplicable for this policy, when
    there is no authenticated principal to attribute usage to (an
    unattributable caller cannot be budgeted per principal, so the guard is
    skipped rather than applied to a shared anonymous bucket — the same posture
    `enforce_query_quota` takes), or when no scope of the query aggregates.
    """
    budget = resolve_disclosure_budget(policy)
    if budget is None or principal_subject is None:
        return
    max_shape_repeats, max_aggregate_queries, window_seconds = budget
    occurrences = budgeted_occurrences(query)
    if not occurrences:
        return
    # A purpose only partitions the budget when the OPERATOR declared the closed
    # set it comes from. With `allowed_purposes` empty,
    # `validation/policy_validation.resolve_purpose_policy` accepts any string a
    # caller invents (item 145's "empty allow-list = unrestricted" convention),
    # so keying on it unconditionally would let a prober mint an unlimited
    # supply of fresh budgets — `purpose="p1"`, `"p2"`, … — and defeat both caps
    # entirely. This mirrors, exactly, the rule `execution/service.py` already
    # applies before persisting a purpose to the audit event: an un-gated
    # purpose is caller-authored free text and is treated as such.
    purpose = query.purpose if (policy.allowed_purposes and query.purpose) else ""
    charges: List[Charge] = []
    if max_aggregate_queries is not None:
        # The per-table cap ignores shape, so a table's weight is every
        # aggregating occurrence of it across the whole statement.
        per_table: Counter = Counter()
        for (table, _fingerprint), count in occurrences.items():
            per_table[table] += count
        for table, weight in sorted(per_table.items()):
            charges.append(
                (
                    (connection_id, principal_subject, purpose, table, ""),
                    max_aggregate_queries,
                    KIND_TABLE,
                    weight,
                )
            )
    if max_shape_repeats is not None:
        for (table, fingerprint), weight in sorted(occurrences.items()):
            charges.append(
                (
                    (connection_id, principal_subject, purpose, table, fingerprint),
                    max_shape_repeats,
                    KIND_SHAPE,
                    weight,
                )
            )
    if charges:
        await _active_limiter.reserve(charges, window_seconds=window_seconds)
