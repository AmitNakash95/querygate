"""Observed query shapes — the discovery half of the narrowing path (TODO.md
item 195).

**Why this exists.** QueryGate's read AST is deliberately expressive, so a
connection opened to the general `StructuredQuery` surface can express far more
query shapes than any one agent actually needs. `Policy.templates_only` (the
enforcement half of item 195) can narrow that surface to a finite, reviewed set
of curated templates — but an operator can only flip that switch once they know
*which* shapes their agent genuinely uses. Guessing the list up front is the
thing that makes hand-authored tool catalogues wrong; this module records the
answer from real traffic instead.

**The redaction posture is the load-bearing part.** A recorded entry is a
*skeleton*: the query AST with every literal-bearing position replaced by a
typed parameter slot, and the natural-language `intent` dropped. It therefore
carries the same redaction guarantee the persisted audit event carries — no
predicate value, no row, no credential, no free text — while being strictly
*more* useful than `audit.events.normalize_query_shape`, because a skeleton can
be promoted directly into a `QueryTemplate` (whose stored `query` is exactly
this shape: a `StructuredQuery` dict with `{"param": name}` placeholders).

**Deny-by-default over the dumped AST, not an enumeration of node types.**
`skeletonize` walks the *dumped* query dict generically and rewrites every
occurrence of a literal-bearing key at any depth, rather than enumerating the
AST node classes that can carry one. That is deliberate: an enumeration is the
exact drift failure mode `CLAUDE.md`'s item-96 note warns about (a new node
type that nobody remembers to add to the walk). `_LITERAL_KEYS` is the single
place this guarantee is maintained, and
`test_observed_shapes.py::test_literal_bearing_ast_fields_are_all_known`
reflects over the AST models to fail the day a new literal-bearing field
appears without being added here.

**This module never publishes anything.** It records, it ranks, and it emits a
*draft* template for a human to review and commit to the templates file — the
same quarantined-draft posture `catalog/governance.py` takes. There is no path
from observed traffic to an installed template that does not pass through a
person.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, Tuple

import pydantic as pyd

from querygate.query_ast.models import StructuredQuery
from querygate.templates.models import ParameterType, QueryTemplate, TemplateParameter

# Every field in the read AST that carries a caller-supplied *value* rather
# than an identifier, an enum or a flag. Each becomes a typed parameter slot.
#
# The rule, so a future field is classified rather than guessed: **a bare
# scalar a caller chooses the CONTENT of is a value; a bare scalar a caller
# chooses from a fixed vocabulary (a table/column/alias name, an operator, a
# boolean switch) is structure.** `audit/events.normalize_query_shape` draws
# the same line, and this set is deliberately a superset of what that function
# retains — `delimiter`, `amount`, window `offset` and `buckets` are values the
# audit event omits entirely, so keeping them here would have made the record
# strictly leakier than the audit event it claims parity with.
#
# `limit`/`offset`/`n` are values the audit event *does* keep. They are still
# parameterized here, for a second reason: an unparameterized number is part of
# `shape_hash`, so one logical shape run at limit 10 and limit 100 would occupy
# two entries — fragmenting the operator's view, and letting a caller flush a
# bounded store by walking a number.
#
# Regression-locked by `test_literal_bearing_ast_fields_are_all_known`, which
# reflects over every read-AST model and fails on ANY bare-scalar field that is
# in neither this set, `_DROPPED_KEYS`, nor the explicitly-justified
# `_STRUCTURAL_SCALAR_KEYS` below.
_LITERAL_KEYS = frozenset(
    {
        "value",  # Predicate comparison value
        "literal",  # LiteralExpr
        "delimiter",  # StringAggSelectItem — unbounded caller free text
        "amount",  # DateAddExpr — "shifted by 90 days" is a value
        "fraction",  # PercentileContSelectItem
        "buckets",  # WindowCall/ntile
        "offset",  # WindowBound/WindowCall frame offset, and StructuredQuery.offset
        "limit",  # StructuredQuery
        "n",  # TopNSpec
    }
)

# Bare scalars that are structure, not values — each one is an identifier, a
# name the caller assigns, or a switch, and each is retained by
# `normalize_query_shape` too. Enumerated explicitly (rather than left as the
# default) so the reflection lock can fail on anything unclassified.
#
# `alias`, `from_alias` and `name` are caller-*authored* strings, so they are
# free text in the strict sense. They are kept because the shape is unreadable
# without them (an aliased select item's whole identity is its alias) and
# because the persisted audit event keeps them too — parity, not an exemption
# invented here. State this rather than claim "no free text".
_STRUCTURAL_SCALAR_KEYS = frozenset(
    {
        "col",  # Table.Column reference
        "value_col",  # column-to-column comparison target
        "from_table",
        "from_alias",
        "table",  # JoinSpec target
        "connection",  # cross-connection join target
        "name",  # CteSpec name
        "alias",  # caller-assigned output name
        "group_by",
        "partition_by",
        "correlate",
        "on",  # JoinSpec equality key pair — column refs
        "extra_on",  # JoinSpec composite key pairs — column refs
        "distinct",
        "all_",  # UNION vs UNION ALL
        "purpose",  # operator-configured declared purpose (allowed_purposes)
    }
)

# Dropped outright rather than parameterized: `intent` is free-form
# natural-language text, which persisted audit events deliberately exclude
# (CLAUDE.md non-negotiable 3). A skeleton holds none either.
_DROPPED_KEYS = frozenset({"intent"})

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NON_IDENTIFIER = re.compile(r"[^A-Za-z0-9_]+")


class SkeletonizationError(ValueError):
    """A query could not be reduced to a redaction-safe skeleton."""


def _param_type_of(value: Any) -> Tuple[ParameterType, bool]:
    """Map a caller-supplied literal to a template parameter type.

    Returns `(type, is_list)`. Raises rather than guessing for a value shape a
    template slot cannot express — a draft that cannot be bound is worse than
    no draft, because an operator would install it and discover the failure at
    agent run time.
    """
    if isinstance(value, list):
        if not value:
            raise SkeletonizationError("cannot infer a parameter type from an empty list")
        element_types = {_param_type_of(item)[0] for item in value}
        if len(element_types) != 1:
            raise SkeletonizationError(
                "cannot infer a parameter type from a mixed-type list: " f"{sorted(element_types)}"
            )
        return element_types.pop(), True
    # bool before int — bool is an int subclass in Python.
    if isinstance(value, bool):
        return "boolean", False
    if isinstance(value, int):
        return "integer", False
    if isinstance(value, float):
        return "number", False
    if isinstance(value, str):
        return "string", False
    raise SkeletonizationError(
        f"cannot infer a parameter type from a value of type {type(value).__name__}"
    )


def _base_name(hint: Optional[str]) -> str:
    """Turn a column reference (`orders.status`) into a parameter-name stem
    (`status`). Falls back to a generic stem when there is no usable hint, so a
    predicate on an expression still yields a legal identifier.
    """
    if not hint:
        return "param"
    stem = hint.rsplit(".", maxsplit=1)[-1]
    stem = _NON_IDENTIFIER.sub("_", stem).strip("_")
    if not stem or not _IDENTIFIER.match(stem):
        return "param"
    return stem


class _ParameterAllocator:
    """Allocates stable, collision-free parameter names in walk order.

    Deterministic by construction: the same query dumped twice walks its keys
    in the same order and therefore allocates the same names, which is what
    makes `shape_hash` stable across occurrences of one shape.
    """

    def __init__(self) -> None:
        self._used: Dict[str, int] = {}
        self.parameters: List[TemplateParameter] = []

    def allocate(self, hint: Optional[str], value: Any) -> str:
        param_type, is_list = _param_type_of(value)
        stem = _base_name(hint)
        seen = self._used.get(stem, 0)
        self._used[stem] = seen + 1
        name = stem if seen == 0 else f"{stem}_{seen + 1}"
        self.parameters.append(
            TemplateParameter(
                name=name,
                type=param_type,
                required=True,
                is_list=is_list,
                description=(f"Observed value at {hint}." if hint else "Observed value."),
            )
        )
        return name


def _walk(node: Any, allocator: _ParameterAllocator, *, column_hint: Optional[str] = None) -> Any:
    """Recursively rewrite `node`, replacing every literal-bearing key with a
    `{"param": name}` placeholder and dropping every free-text key.

    `column_hint` carries the nearest enclosing column reference down one level
    so a predicate's value slot can be named after the column it filters — a
    naming nicety only; it never affects what is stripped.
    """
    if isinstance(node, dict):
        # A predicate dict names its own column; use it to name that dict's
        # value slot rather than the enclosing scope's.
        local_hint = node.get("col") if isinstance(node.get("col"), str) else column_hint
        out: Dict[str, Any] = {}
        for key, value in node.items():
            if key in _DROPPED_KEYS:
                continue
            if key in _LITERAL_KEYS:
                # `value`/`literal` sit next to the column they filter, so the
                # column makes the better slot name; every other value key IS
                # its own best name (`limit`, `amount`, `delimiter`).
                hint = local_hint if key in ("value", "literal") else key
                if value is None:
                    # `is_null`/`is_not_null` carry no value at all — there is
                    # nothing to parameterize, and emitting a required slot
                    # nobody can fill would make the draft unbindable.
                    continue
                out[key] = {"param": allocator.allocate(hint, value)}
                continue
            out[key] = _walk(value, allocator, column_hint=local_hint)
        return out
    if isinstance(node, list):
        return [_walk(item, allocator, column_hint=column_hint) for item in node]
    return node


def _assert_no_literal_survived(skeleton: Any) -> None:
    """Fail closed if any literal-bearing key still holds a concrete value.

    This is the runtime half of the redaction guarantee: `_walk` is supposed to
    have replaced every one, and this refuses to hand back a skeleton where it
    did not, rather than storing a value and trusting a test to have caught it.
    """
    if isinstance(skeleton, dict):
        for key, value in skeleton.items():
            if key in _DROPPED_KEYS:
                raise SkeletonizationError(f"free-text key {key!r} survived skeletonization")
            if key in _LITERAL_KEYS:
                if not (isinstance(value, dict) and set(value) == {"param"}):
                    raise SkeletonizationError(
                        f"literal-bearing key {key!r} survived skeletonization"
                    )
                continue
            _assert_no_literal_survived(value)
    elif isinstance(skeleton, list):
        for item in skeleton:
            _assert_no_literal_survived(item)


def skeletonize(query: StructuredQuery) -> Tuple[Dict[str, Any], List[TemplateParameter]]:
    """Reduce `query` to a redaction-safe, promotable skeleton.

    Returns the `StructuredQuery` dict with `{"param": name}` in every literal
    position — exactly the shape `QueryTemplate.query` stores — plus the typed
    parameter slots those placeholders refer to.
    """
    dumped = query.model_dump(mode="json", exclude_none=True)
    allocator = _ParameterAllocator()
    skeleton = _walk(dumped, allocator)
    _assert_no_literal_survived(skeleton)
    return skeleton, allocator.parameters


def shape_hash(skeleton: Dict[str, Any]) -> str:
    """Stable identity for a skeleton: sha256 over its canonical JSON form.

    Deliberately computed over the *skeleton*, not the raw query, so two
    invocations of one shape with different filter values collapse to one
    entry — which is the whole point of recording shapes rather than queries.
    """
    canonical = json.dumps(skeleton, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


class ObservedShape(pyd.BaseModel):
    """One distinct query shape a principal has successfully run.

    Every field here is either structural (identifiers, operators, counts) or a
    parameter *name* — never a value. Safe to return over an admin API and safe
    to write to disk.
    """

    shape_hash: str
    connection_id: str
    principal_id: Optional[str] = None
    skeleton: Dict[str, Any]
    parameters: List[TemplateParameter] = pyd.Field(default_factory=list)
    occurrences: int = 1
    first_seen: datetime
    last_seen: datetime

    model_config = pyd.ConfigDict(extra="forbid")

    def to_template_draft(
        self, template_id: str, *, description: Optional[str] = None
    ) -> QueryTemplate:
        """Render this shape as a `QueryTemplate` an operator can review.

        Returned, never written. Installing a template stays a deliberate edit
        to the templates file (or a governed config version), so the review
        gate cannot be skipped by calling this.
        """
        return QueryTemplate(
            id=template_id,
            connection=self.connection_id,
            description=(
                description
                or f"Drafted from an observed query shape ({self.occurrences} occurrences)."
            ),
            parameters=list(self.parameters),
            query=self.skeleton,
        )


class ObservedShapeStore(Protocol):
    """Records and reads back observed shapes.

    A Protocol rather than a concrete class because the in-process
    implementation below is per-process — the same limitation
    `execution/concurrency.py` and `execution/quota.py` carry before their
    Redis siblings — so a durable/shared backend can be registered later
    without touching any call site.
    """

    def record(
        self,
        query: StructuredQuery,
        *,
        connection_id: str,
        principal_id: Optional[str],
    ) -> Optional[ObservedShape]: ...

    def list_shapes(
        self, *, connection_id: Optional[str] = None, principal_id: Optional[str] = None
    ) -> List[ObservedShape]: ...

    def get(self, shape_hash: str) -> Optional[ObservedShape]: ...

    def clear(self) -> None: ...


class InProcessObservedShapeStore:
    """Bounded, thread-safe, per-process shape recorder.

    **Bounded on purpose.** An unbounded dict keyed by query shape is a memory
    leak an adversarial caller controls: shapes are caller-authored, so a
    caller emitting one novel shape per request would grow it without limit.
    `max_entries` evicts the least-recently-seen shape when full, and
    `evicted_total` reports how many were dropped so the ceiling is visible in
    the API response rather than silently truncating the operator's view.
    """

    def __init__(self, max_entries: int = 500, *, enabled: bool = False) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._shapes: "OrderedDict[str, ObservedShape]" = OrderedDict()
        self.evicted_total = 0
        # A query the AST accepts but no template slot can express (a dict
        # value, a mixed-type IN list) is skipped rather than recorded, and the
        # recording hook swallows the error so a succeeded query is unaffected.
        # Counted and surfaced for the same reason `evicted_total` is: an
        # operator about to narrow a connection must be able to see that the
        # discovery record is incomplete, rather than promote N templates and
        # break the N+1st query.
        self.skeletonization_failures = 0
        # Owned by the store rather than read from a config singleton at each
        # call site, so the recording hook and the admin report can never
        # disagree about whether recording is on (a 2026-08-17 review found
        # exactly that split: an app-scoped AppConfig reported enabled=true
        # while the module-global config kept the recorder silent).
        self.enabled = enabled

    @property
    def max_entries(self) -> int:
        return self._max_entries

    def record(
        self,
        query: StructuredQuery,
        *,
        connection_id: str,
        principal_id: Optional[str],
    ) -> Optional[ObservedShape]:
        if not self.enabled:
            return None
        try:
            skeleton, parameters = skeletonize(query)
        except SkeletonizationError:
            with self._lock:
                self.skeletonization_failures += 1
            raise
        digest = shape_hash(skeleton)
        now = datetime.now(timezone.utc)
        # One shape is recorded per (shape, connection, principal): the same
        # SQL shape run by two principals is two rows, because the promotion
        # decision ("may THIS agent be narrowed to these N templates?") is
        # per-principal.
        key = f"{digest}:{connection_id}:{principal_id or ''}"
        with self._lock:
            existing = self._shapes.get(key)
            if existing is not None:
                existing.occurrences += 1
                existing.last_seen = now
                self._shapes.move_to_end(key)
                return existing
            entry = ObservedShape(
                shape_hash=digest,
                connection_id=connection_id,
                principal_id=principal_id,
                skeleton=skeleton,
                parameters=parameters,
                first_seen=now,
                last_seen=now,
            )
            self._shapes[key] = entry
            while len(self._shapes) > self._max_entries:
                self._shapes.popitem(last=False)
                self.evicted_total += 1
            return entry

    def list_shapes(
        self, *, connection_id: Optional[str] = None, principal_id: Optional[str] = None
    ) -> List[ObservedShape]:
        with self._lock:
            entries = list(self._shapes.values())
        if connection_id is not None:
            entries = [e for e in entries if e.connection_id == connection_id]
        if principal_id is not None:
            entries = [e for e in entries if e.principal_id == principal_id]
        # Most-used first: the promotion decision starts with the shapes that
        # carry the most traffic, not the most recent one.
        return sorted(entries, key=lambda e: (-e.occurrences, e.first_seen))

    def get(
        self,
        shape_hash_value: str,
        *,
        connection_id: Optional[str] = None,
        principal_id: Optional[str] = None,
    ) -> Optional[ObservedShape]:
        """One recorded shape. The optional filters matter because a hash is
        NOT unique on its own — the same shape run by two principals is two
        entries by design, so an unfiltered lookup returns whichever was
        recorded first and would draft a template naming the wrong principal's
        connection.
        """
        with self._lock:
            for entry in self._shapes.values():
                if entry.shape_hash != shape_hash_value:
                    continue
                if connection_id is not None and entry.connection_id != connection_id:
                    continue
                if principal_id is not None and entry.principal_id != principal_id:
                    continue
                return entry
        return None

    def clear(self) -> None:
        with self._lock:
            self._shapes.clear()
            self.evicted_total = 0
            self.skeletonization_failures = 0


_in_process_store = InProcessObservedShapeStore()


def observed_shape_store() -> InProcessObservedShapeStore:
    """The persistent in-process store — used by the recording hook, the admin
    read API, the CLI, and by `tests/conftest.py` to reset state between tests
    (the same discipline `in_process_limiter()` documents).
    """
    return _in_process_store


def configure_observed_shape_store(max_entries: int, *, enabled: bool) -> None:
    """Re-create the store with the operator's ceiling and on/off switch.
    Called once at application start; replacing rather than mutating keeps both
    immutable for the store's lifetime.

    Note this **discards** anything already recorded — the store is in-memory
    and non-durable, so an application restart (or a rolling deploy) resets the
    discovery window. Stated in `ObservedShapeReport.scope` and in the
    operator docs rather than left for someone to discover mid-cutover.
    """
    global _in_process_store
    _in_process_store = InProcessObservedShapeStore(max_entries=max_entries, enabled=enabled)


class ObservedShapeReport(pyd.BaseModel):
    """Admin-API projection: the recorded shapes plus honest bounds."""

    enabled: bool
    shapes: List[ObservedShape] = pyd.Field(default_factory=list)
    max_entries: int
    evicted_total: int
    skeletonization_failures: int = 0
    # Stated rather than glossed: the in-process store is per serving process,
    # so a multi-replica or multi-worker deployment sees only the shapes that
    # this process handled. An operator narrowing a connection to templates
    # must collect from every replica, or run the discovery window against a
    # single-replica deployment.
    scope: str = "process-local-volatile"

    model_config = pyd.ConfigDict(extra="forbid")


def build_observed_shape_report(
    *,
    connection_id: Optional[str] = None,
    principal_id: Optional[str] = None,
    store: Optional[InProcessObservedShapeStore] = None,
) -> ObservedShapeReport:
    """`enabled` is read from the store, not from a caller-supplied flag, so the
    report cannot claim recording is on while the recorder is silent.
    """
    active = store if store is not None else observed_shape_store()
    return ObservedShapeReport(
        enabled=active.enabled,
        shapes=(
            active.list_shapes(connection_id=connection_id, principal_id=principal_id)
            if active.enabled
            else []
        ),
        max_entries=active.max_entries,
        evicted_total=active.evicted_total,
        skeletonization_failures=active.skeletonization_failures,
    )
