"""Redis-backed, cross-replica, restart-surviving observed-shape store
(TODO.md item 195 phase 2).

The distributed sibling of `observed_shapes.InProcessObservedShapeStore`, and
the reason it exists is the one limitation that made the in-process store
awkward in a real deployment rather than merely bounded:

- **Per-replica.** A discovery window run against a 3-replica deployment saw
  roughly a third of the traffic in each process, so no single reading of
  "which shapes does this agent use?" was complete. An operator who narrowed a
  connection to `templates_only` from one replica's list would break every
  shape the other two happened to see. An incomplete discovery list is not a
  smaller version of the right answer — it is the wrong answer.
- **Volatile.** The store lived only in memory and was re-created at every
  application start, so a rolling deploy or a crash silently discarded the
  window mid-collection. `scope: "process-local-volatile"` disclosed this, but
  disclosure is not a fix.

With this backend both go away: one shared window across every replica and
worker, surviving restarts, reported as `scope: "shared-durable"`.

## Key layout, and why the mutable parts are separate keys

    qg:shapes:{qgshapes}:records   HASH  member -> IMMUTABLE body JSON
    qg:shapes:{qgshapes}:counts    HASH  member -> occurrences
    qg:shapes:{qgshapes}:first     HASH  member -> first_seen (ISO)
    qg:shapes:{qgshapes}:idx       ZSET  member scored by last_seen (LRU + last_seen)
    qg:shapes:{qgshapes}:counters  HASH  evicted / failures / max_entries

`member` is `"<connection>|<principal>|<shape_hash>"`, and every key carries the
same `{qgshapes}` hash tag, so all five hash to one Redis Cluster slot and the
record script can declare all five as `KEYS` — which is the point.

**The script never parses the record body, and that is the whole reason for
this split.** The first version stored one JSON blob per shape and used Lua
`cjson.decode`/`cjson.encode` to bump `occurrences` and preserve `first_seen`.
That round-trip **silently corrupts the skeleton on real Redis**: Lua has one
table type, so an empty JSON array re-encodes as an empty *object*, and every
`joins: []`, `group_by: []`, `order_by: []`, `correlate: []`, `ctes: []` came
back as `{}`. Measured — every Redis-sourced draft failed
`validate_template_structure` with five `Input should be a valid list` errors,
so the entire promote-a-template workflow was broken on the durable backend and
only on the durable backend (fakeredis's Lua bridge preserves the distinction,
so the whole test suite passed). Found by `security-invariant-reviewer` on the
final tree; it is the mirror image of the `ZPOPMIN` gotcha — same
fakeredis-vs-real-Redis Lua divergence class, with fakeredis the forgiving one
this time.

The fix is structural rather than a careful encoder: the body is **written
once and never decoded** (it is fully determined by `shape_hash`, so re-writing
it is idempotent), and every mutable field lives in its own Redis structure
that Lua can update with an integer/string primitive — `HINCRBY` for the count,
`HSETNX` for `first_seen`, the index's own ZSET score for `last_seen`. There is
no `cjson` in the script at all, so this class of bug cannot recur here.

**This is the second design.** The first used a *per-(connection, principal)*
record hash and reached it from Lua via `redis.call` on a key built inside the
script rather than declared in `KEYS`, specifically to dodge `CROSSSLOT`. That
is exactly backwards: touching an undeclared key from a script is what Redis
Cluster forbids, so the "clever" version would have failed on Cluster *and*
silently lost every eviction. `test_the_store_is_bounded_and_counts_evictions`
caught it (10 entries retained against a bound of 3, zero evictions counted)
before it shipped. `TODO.md` item 192 records the same defect class in the
disclosure budget's script, so it is a repeat mistake in this repo, not a novel
one — hence the explicit test asserting every declared key shares the tag.

Single-slot means no sharding across the cluster for these keys. That is the
right trade here: the store is bounded to `max_entries` (hundreds), written
once per distinct query shape rather than once per query, and read only by an
operator. Correctness under Cluster beats horizontal spread for an
operator-facing discovery surface.

Client-side filtering reads `connection_id`/`principal_id` off each decoded
record rather than parsing them back out of the member string — so a principal
id containing a `|` (a JWT subject can) cannot shift the parse.

## Fail-open, always

Recording a shape is layered on a query that has *already succeeded* and whose
rows are in the caller's hand. A Redis outage must degrade to "the discovery
window is missing entries", never to an error on a query that worked. Every
method catches `RedisError`, logs once, and returns empty. This is deliberately
the opposite posture from `execution/redis_disclosure_budget.py` (a security
control, so it fails closed) and matches `catalog/usage.py`'s signal buffer,
which is the closer analogue: best-effort operator telemetry.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from redis.exceptions import RedisError

from querygate.admin.observed_shapes import (
    ObservedShape,
    ObservedShapeCounters,
    SkeletonizationError,
    shape_hash,
    skeletonize,
)
from querygate.core.logging import get_logger
from querygate.query_ast.models import StructuredQuery

# One hash tag across every key -> one Cluster slot -> every key the record
# script touches can be a declared KEY. See the module docstring.
_RECORDS_KEY = "qg:shapes:{qgshapes}:records"
_COUNTS_KEY = "qg:shapes:{qgshapes}:counts"
_FIRST_SEEN_KEY = "qg:shapes:{qgshapes}:first"
_INDEX_KEY = "qg:shapes:{qgshapes}:idx"
_COUNTERS_KEY = "qg:shapes:{qgshapes}:counters"
_ALL_KEYS = (_RECORDS_KEY, _COUNTS_KEY, _FIRST_SEEN_KEY, _INDEX_KEY, _COUNTERS_KEY)

# KEYS: 1=records 2=counts 3=first_seen 4=index(zset) 5=counters. All five carry
# the {qgshapes} tag, so they share one Cluster slot and are all declared.
# ARGV: 1=member 2=body_json 3=now 4=first_seen_iso 5=max_entries 6=ttl_seconds
# Returns {occurrences, evicted_this_call}
#
# Deliberately NO cjson: the body is written verbatim and never parsed here (see
# the module docstring — a Lua JSON round-trip turns every empty array into an
# empty object on real Redis and corrupts the skeleton).
_RECORD_SCRIPT = """
local member = ARGV[1]
local body = ARGV[2]
local now = tonumber(ARGV[3])
local first_seen = ARGV[4]
local max_entries = tonumber(ARGV[5])
local ttl = tonumber(ARGV[6])

-- HSETNX, not HSET: the body is written exactly ONCE per member and never
-- touched again. Everything in it is determined by the shape hash except the
-- embedded timestamps, which readers ignore in favour of the authoritative side
-- structures below. Write-once also means a repeat sighting costs no bandwidth.
redis.call('HSETNX', KEYS[1], member, body)
local occurrences = redis.call('HINCRBY', KEYS[2], member, 1)
redis.call('HSETNX', KEYS[3], member, first_seen)
redis.call('ZADD', KEYS[4], now, member)

-- Evict least-recently-seen while over the bound. The index and all three
-- per-member structures must drop the victim TOGETHER: leaving the body behind
-- would make the bound cosmetic (list_shapes reads the records hash) and let it
-- grow without limit anyway.
-- ZRANGE+ZREM rather than ZPOPMIN, deliberately: ZPOPMIN's Lua reply shape is
-- NOT portable. Real Redis returns a flat {member, score}; fakeredis returns a
-- nested table, so `reply[1]` is a table and the HDEL raises "Lua redis lib
-- command arguments must be strings or integers" mid-script, which a fail-open
-- wrapper swallows — so the bound looks cosmetic and every eviction is lost.
-- ZRANGE and ZREM have stable flat replies on every implementation.
local evicted = 0
while redis.call('ZCARD', KEYS[4]) > max_entries do
  local oldest = redis.call('ZRANGE', KEYS[4], 0, 0)
  local victim = oldest[1]
  if not victim then break end
  redis.call('ZREM', KEYS[4], victim)
  redis.call('HDEL', KEYS[1], victim)
  redis.call('HDEL', KEYS[2], victim)
  redis.call('HDEL', KEYS[3], victim)
  evicted = evicted + 1
end
if evicted > 0 then
  redis.call('HINCRBY', KEYS[5], 'evicted', evicted)
end
-- The bound that actually BINDS is whichever replica last wrote, not whichever
-- replica reads. Recorded here so `counters()` reports the real ceiling instead
-- of the reading process's own config — otherwise a fleet whose replicas
-- disagree on max_entries shows an eviction warning naming a limit that is not
-- the one doing the evicting.
redis.call('HSET', KEYS[5], 'max_entries', max_entries)

-- TTL refreshed on EVERY write, unconditionally and on every key. Refreshing
-- the counters only when an eviction happened let the warning counters expire
-- out from under a still-truncated list, so an incomplete list started
-- reporting itself as complete.
for index = 1, 5 do
  redis.call('EXPIRE', KEYS[index], ttl)
end
return {occurrences, evicted}
"""


def _member(connection_id: str, principal_id: Optional[str], digest: str) -> str:
    return f"{connection_id}|{principal_id or ''}|{digest}"


def _as_text(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _as_int(value, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _lookup(mapping, key):
    """Fetch from a redis-py reply that may be keyed by bytes or by str —
    `decode_responses` is the operator's choice, not ours to assume.
    """
    if not mapping:
        return None
    if key in mapping:
        return mapping[key]
    if isinstance(key, bytes) and key.decode() in mapping:
        return mapping[key.decode()]
    if isinstance(key, str) and key.encode() in mapping:
        return mapping[key.encode()]
    return None


class RedisObservedShapeStore:
    """Cross-replica, restart-surviving observed-shape store.

    Satisfies the async `ObservedShapeStore` protocol. `enabled` is carried
    here (rather than read from a config singleton at each call site) for the
    same reason the in-process store carries it: the recording hook and the
    admin report must never disagree about whether recording is on.
    """

    scope = "shared-durable"

    def __init__(
        self,
        redis_client,
        *,
        max_entries: int = 500,
        ttl_seconds: int = 2_592_000,
        enabled: bool = False,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._redis = redis_client
        self._max_entries = max_entries
        # A discovery window is a bounded activity, but a Redis key is not — a
        # TTL refreshed on every write keeps an operator who enables recording
        # and forgets from holding the keys forever.
        self._ttl_seconds = ttl_seconds
        self.enabled = enabled
        # Fail-open means a Redis outage renders as an EMPTY shape list, which is
        # indistinguishable from "recording is on and your agent ran nothing" —
        # the exact confusion this feature exists to prevent, since an operator
        # who narrows from that list narrows to nothing. So reachability is
        # tracked and surfaced on the report rather than only logged server-side.
        self._backend_healthy = True
        self._record_script = redis_client.register_script(_RECORD_SCRIPT)

    @property
    def max_entries(self) -> int:
        return self._max_entries

    @property
    def backend_healthy(self) -> bool:
        return self._backend_healthy

    def _log_unreachable(self, operation: str, exc: BaseException) -> None:
        self._backend_healthy = False
        get_logger().warning(
            "observed_shapes.redis.unreachable_fail_open",
            operation=operation,
            error=str(exc),
        )

    async def record(
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
            # Counted so an operator can see the discovery list is incomplete
            # (see `ObservedShapeCounters`). Best-effort: if Redis is down the
            # count is lost, which is strictly better than failing the query.
            try:
                await self._redis.hincrby(_COUNTERS_KEY, "failures", 1)
                await self._redis.expire(_COUNTERS_KEY, self._ttl_seconds)
            except RedisError as exc:  # pragma: no cover - defensive
                self._log_unreachable("counters.failures", exc)
            raise
        digest = shape_hash(skeleton)
        now = datetime.now(timezone.utc)
        entry = ObservedShape(
            shape_hash=digest,
            connection_id=connection_id,
            principal_id=principal_id,
            skeleton=skeleton,
            parameters=parameters,
            occurrences=1,
            first_seen=now,
            last_seen=now,
        )
        # The body is stored verbatim and written once (HSETNX). Its embedded
        # occurrences/first_seen/last_seen are the values at first sighting and
        # are IGNORED on read — the authoritative versions live in the counts,
        # first-seen and index structures, which Lua updates with integer/string
        # primitives. Nothing here round-trips through `cjson`; see the module
        # docstring for the corruption that caused.
        body = entry.model_dump_json()
        try:
            result = await self._record_script(
                keys=list(_ALL_KEYS),
                args=[
                    _member(connection_id, principal_id, digest),
                    body,
                    now.timestamp(),
                    now.isoformat(),
                    self._max_entries,
                    self._ttl_seconds,
                ],
            )
        except RedisError as exc:
            self._log_unreachable("record", exc)
            return None
        self._backend_healthy = True
        entry.occurrences = int(result[0])
        return entry

    async def list_shapes(
        self, *, connection_id: Optional[str] = None, principal_id: Optional[str] = None
    ) -> List[ObservedShape]:
        try:
            bodies = await self._redis.hgetall(_RECORDS_KEY)
            counts = await self._redis.hgetall(_COUNTS_KEY)
            firsts = await self._redis.hgetall(_FIRST_SEEN_KEY)
            last_seen = dict(await self._redis.zrange(_INDEX_KEY, 0, -1, withscores=True))
        except RedisError as exc:
            self._log_unreachable("list_shapes", exc)
            return []
        self._backend_healthy = True
        shapes: List[ObservedShape] = []
        for member, value in (bodies or {}).items():
            try:
                shape = ObservedShape.model_validate_json(value)
            except Exception:  # pragma: no cover - a corrupt entry is skipped
                get_logger().warning("observed_shapes.redis.undecodable_entry")
                continue
            shape.occurrences = _as_int(_lookup(counts, member), default=1)
            first_iso = _lookup(firsts, member)
            if first_iso:
                try:
                    shape.first_seen = datetime.fromisoformat(_as_text(first_iso))
                except ValueError:  # pragma: no cover - defensive
                    pass
            score = _lookup(last_seen, member)
            if score is not None:
                shape.last_seen = datetime.fromtimestamp(float(score), tz=timezone.utc)
            shapes.append(shape)
        # Filter on the decoded record's own fields, never by parsing the member
        # string: a principal id can legitimately contain the '|' separator.
        if connection_id is not None:
            shapes = [s for s in shapes if s.connection_id == connection_id]
        if principal_id is not None:
            shapes = [s for s in shapes if s.principal_id == principal_id]
        # Most-used first, matching the in-process store: the promotion decision
        # starts with the shapes carrying the most traffic.
        return sorted(shapes, key=lambda s: (-s.occurrences, s.first_seen))

    async def get(
        self,
        shape_hash_value: str,
        *,
        connection_id: Optional[str] = None,
        principal_id: Optional[str] = None,
    ) -> Optional[ObservedShape]:
        for shape in await self.list_shapes(connection_id=connection_id, principal_id=principal_id):
            if shape.shape_hash == shape_hash_value:
                return shape
        return None

    async def counters(self) -> ObservedShapeCounters:
        try:
            raw = await self._redis.hgetall(_COUNTERS_KEY)
        except RedisError as exc:
            self._log_unreachable("counters", exc)
            raw = {}

        def _count(name: str) -> int:
            # redis-py returns bytes unless decode_responses=True; accept both
            # rather than assuming how the operator constructed the client.
            value = None
            if raw:
                value = raw.get(name.encode()) if name.encode() in raw else raw.get(name)
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0

        # The ceiling that actually binds is the one the last writer used, which
        # in a fleet whose replicas disagree is not this process's own config.
        # Reporting our own would name a limit that is not doing the evicting.
        stored_bound = _count("max_entries")
        return ObservedShapeCounters(
            max_entries=stored_bound or self._max_entries,
            evicted_total=_count("evicted"),
            skeletonization_failures=_count("failures"),
            backend_healthy=self._backend_healthy,
        )

    async def clear(self) -> None:
        """Drop every recorded shape. Used by tests; deliberately not exposed
        over any API — an operator resets a discovery window by letting the TTL
        lapse or by clearing the keys directly, not through a scope-gated
        endpoint that could be used to erase what an agent was seen doing.
        """
        try:
            await self._redis.delete(*_ALL_KEYS)
            self._backend_healthy = True
        except RedisError as exc:  # pragma: no cover - defensive
            self._log_unreachable("clear", exc)
