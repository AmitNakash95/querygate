"""Redis-backed observed-shape store (TODO.md item 195 phase 2).

The two properties this backend exists to provide, and which the in-process
store structurally cannot:

1. **Shared** — one discovery window across every replica and worker, so the
   list an operator narrows a connection from is complete. A per-replica list
   is not merely smaller, it is *wrong*: narrowing from it breaks every shape
   the other replicas saw.
2. **Durable** — the window survives an application restart, so a rolling
   deploy mid-collection does not silently reset it.

Plus the two failure postures that keep it safe to enable in production: it
fails **open** (recording is layered on a query that already succeeded, so a
Redis outage must never surface as a query error), and every multi-key script
is **single-slot** so it does not `CROSSSLOT` on Redis Cluster — the exact
defect `TODO.md` item 192 records for the disclosure budget's script.
"""

from __future__ import annotations

import pytest
from redis.exceptions import RedisError

from querygate.admin.observed_shapes import (
    ObservedShapeCounters,
    SkeletonizationError,
    observed_shape_store,
)
from querygate.admin.redis_observed_shapes import (
    _ALL_KEYS,
    _COUNTERS_KEY,
    _COUNTS_KEY,
    _FIRST_SEEN_KEY,
    _INDEX_KEY,
    _RECORD_SCRIPT,
    _RECORDS_KEY,
    RedisObservedShapeStore,
    _member,
)
from querygate.query_ast.models import Predicate, StructuredQuery

pytestmark = pytest.mark.unit

fakeredis = pytest.importorskip("fakeredis")


def _query(**overrides) -> StructuredQuery:
    base = dict(
        from_table="orders",
        select=["orders.id"],
        where=Predicate(col="orders.status", op="eq", value="completed"),
        limit=5,
    )
    base.update(overrides)
    return StructuredQuery(**base)


@pytest.fixture
def redis_client():
    return fakeredis.aioredis.FakeRedis()


@pytest.fixture
def store(redis_client):
    return RedisObservedShapeStore(redis_client, max_entries=5, enabled=True)


# ---------------------------------------------------------------------------
# Shared: the property the in-process store cannot provide
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_replicas_share_one_discovery_window(redis_client):
    """The reason this backend exists. Two stores over one Redis are two
    replicas; a shape recorded by either must be visible to both, and the same
    shape seen by both must be ONE entry with the counts summed — not two
    partial views an operator has to reconcile by hand.
    """
    replica_a = RedisObservedShapeStore(redis_client, enabled=True)
    replica_b = RedisObservedShapeStore(redis_client, enabled=True)

    await replica_a.record(_query(), connection_id="demo", principal_id="agent")
    await replica_b.record(_query(), connection_id="demo", principal_id="agent")
    await replica_b.record(_query(), connection_id="demo", principal_id="agent")

    from_a = await replica_a.list_shapes()
    from_b = await replica_b.list_shapes()
    assert len(from_a) == 1 and len(from_b) == 1
    assert from_a[0].shape_hash == from_b[0].shape_hash
    assert from_a[0].occurrences == 3


@pytest.mark.asyncio
async def test_the_window_survives_a_restart(redis_client):
    """A new store object over the same Redis is the post-restart process. The
    in-process store discards everything here (`configure_observed_shape_store`
    replaces it at app start); this one must not.
    """
    before = RedisObservedShapeStore(redis_client, enabled=True)
    await before.record(_query(), connection_id="demo", principal_id="agent")

    after_restart = RedisObservedShapeStore(redis_client, enabled=True)
    assert len(await after_restart.list_shapes()) == 1


@pytest.mark.asyncio
async def test_it_reports_a_shared_durable_scope(store):
    """An operator reading a narrowing decision off the list has to know which
    guarantee they have. `scope` is read from the store, never hardcoded.
    """
    assert store.scope == "shared-durable"


@pytest.mark.asyncio
async def test_the_report_carries_the_active_backend_s_scope(store):
    """The store knowing its own scope is useless if the *report* — the thing an
    operator actually reads before flipping `templates_only` — hardcodes the
    volatile default. Pinned separately because a hardcoded value here would
    understate a real guarantee, and (with the backends swapped) overstate one.
    """
    from querygate.admin.observed_shapes import build_observed_shape_report

    await store.record(_query(), connection_id="demo", principal_id="agent")
    report = await build_observed_shape_report(store=store)
    assert report.scope == "shared-durable"
    assert report.enabled is True
    assert len(report.shapes) == 1


# ---------------------------------------------------------------------------
# Same semantics as the in-process store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recording_is_off_unless_enabled(redis_client):
    disabled = RedisObservedShapeStore(redis_client, enabled=False)
    assert await disabled.record(_query(), connection_id="demo", principal_id="agent") is None
    assert await disabled.list_shapes() == []


@pytest.mark.asyncio
async def test_the_same_shape_with_different_values_is_one_entry(store):
    for value in ("completed", "pending", "cancelled"):
        await store.record(
            _query(where=Predicate(col="orders.status", op="eq", value=value)),
            connection_id="demo",
            principal_id="agent",
        )
    shapes = await store.list_shapes()
    assert len(shapes) == 1 and shapes[0].occurrences == 3


@pytest.mark.asyncio
async def test_one_shape_run_by_two_principals_stays_separable(store):
    await store.record(_query(), connection_id="demo", principal_id="agent-a")
    await store.record(_query(), connection_id="demo", principal_id="agent-b")
    assert len(await store.list_shapes()) == 2
    assert len(await store.list_shapes(principal_id="agent-a")) == 1
    assert len(await store.list_shapes(connection_id="demo")) == 2


@pytest.mark.asyncio
async def test_a_recorded_shape_holds_no_values(store):
    await store.record(
        _query(where=Predicate(col="orders.status", op="eq", value="SSN-123-45-6789")),
        connection_id="demo",
        principal_id="agent",
    )
    shapes = await store.list_shapes()
    # The redaction guarantee is enforced by the shared `skeletonize`, so it
    # cannot diverge between backends — asserted here anyway, because "the
    # other backend leaks" is exactly the kind of gap a second implementation
    # introduces.
    assert "SSN-123-45-6789" not in str(shapes[0].model_dump())
    assert shapes[0].skeleton["where"]["value"] == {"param": "status"}


@pytest.mark.asyncio
async def test_get_disambiguates_by_connection_and_principal(store):
    await store.record(_query(), connection_id="demo", principal_id="agent-a")
    shapes = await store.list_shapes()
    digest = shapes[0].shape_hash
    assert (await store.get(digest, principal_id="agent-a")) is not None
    assert (await store.get(digest, principal_id="nobody")) is None


@pytest.mark.asyncio
async def test_shapes_are_ranked_most_used_first(store):
    await store.record(_query(group_by=["orders.status"]), connection_id="demo", principal_id="a")
    for _ in range(3):
        await store.record(_query(), connection_id="demo", principal_id="a")
    assert (await store.list_shapes())[0].occurrences == 3


# ---------------------------------------------------------------------------
# Bounds and honesty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_store_is_bounded_and_counts_evictions(redis_client):
    """Shapes are caller-authored, so an unbounded store is a memory leak an
    adversarial caller controls — in Redis that is somebody else's memory, which
    makes the bound more important here, not less.
    """
    store = RedisObservedShapeStore(redis_client, max_entries=3, enabled=True)
    for index in range(10):
        await store.record(
            _query(select=[f"orders.c{index}"]), connection_id="demo", principal_id="agent"
        )
    assert len(await store.list_shapes()) <= 3
    counters = await store.counters()
    assert counters.evicted_total > 0
    assert counters.max_entries == 3


@pytest.mark.asyncio
async def test_an_evicted_entry_is_removed_from_the_record_hash_too(redis_client):
    """The index and the per-pair record hash must be evicted together. If the
    hash kept the body, `list_shapes` would keep returning a shape the bound
    had already dropped and the store would grow without limit anyway — the
    bound would be cosmetic.
    """
    store = RedisObservedShapeStore(redis_client, max_entries=2, enabled=True)
    for index in range(6):
        await store.record(
            _query(select=[f"orders.c{index}"]), connection_id="demo", principal_id="agent"
        )
    stored = await redis_client.hgetall(_RECORDS_KEY)
    assert len(stored) <= 2
    assert len(await store.list_shapes()) <= 2


@pytest.mark.asyncio
async def test_a_shape_that_cannot_be_skeletonized_is_counted(store):
    with pytest.raises(SkeletonizationError):
        await store.record(
            _query(where=Predicate(col="orders.meta", op="eq", value={"nested": 1})),
            connection_id="demo",
            principal_id="agent",
        )
    assert (await store.counters()).skeletonization_failures == 1


@pytest.mark.asyncio
async def test_keys_carry_a_refreshed_ttl(redis_client):
    """A discovery window is bounded activity; a Redis key is not. Without a
    TTL an operator who enables recording and forgets keeps the keys forever.
    """
    store = RedisObservedShapeStore(redis_client, ttl_seconds=600, enabled=True)
    await store.record(_query(), connection_id="demo", principal_id="agent")
    assert 0 < await redis_client.ttl(_RECORDS_KEY) <= 600
    assert 0 < await redis_client.ttl(_INDEX_KEY) <= 600


# ---------------------------------------------------------------------------
# Failure posture
# ---------------------------------------------------------------------------


class _BrokenRedis:
    """Every operation raises, including `register_script`'s returned callable."""

    def register_script(self, _script):
        async def _raise(*_args, **_kwargs):
            raise RedisError("down")

        return _raise

    async def hgetall(self, *_args, **_kwargs):
        raise RedisError("down")

    async def zrange(self, *_args, **_kwargs):
        raise RedisError("down")

    async def hincrby(self, *_args, **_kwargs):
        raise RedisError("down")

    async def expire(self, *_args, **_kwargs):
        raise RedisError("down")

    async def delete(self, *_args, **_kwargs):
        raise RedisError("down")


@pytest.mark.asyncio
async def test_a_redis_outage_never_raises_into_the_caller():
    """Recording happens *after* a query has already succeeded and its rows are
    in hand. Failing open is not a convenience here — failing closed would turn
    a Redis blip into an error on a query that worked, which is strictly worse
    than an incomplete discovery list. This is the opposite posture from the
    disclosure budget (a security control, fails closed) and matches the
    catalog usage-signal buffer.
    """
    store = RedisObservedShapeStore(_BrokenRedis(), enabled=True)
    assert await store.record(_query(), connection_id="demo", principal_id="agent") is None
    assert await store.list_shapes() == []
    assert await store.get("whatever") is None
    counters = await store.counters()
    assert isinstance(counters, ObservedShapeCounters)
    assert counters.evicted_total == 0
    await store.clear()  # must not raise either


# ---------------------------------------------------------------------------
# Redis Cluster safety (TODO.md item 192's defect class)
# ---------------------------------------------------------------------------


def test_every_key_the_script_touches_shares_one_hash_slot():
    """Item 192 is exactly this bug in the disclosure budget's script: a Lua
    script whose keys land in different Cluster hash slots fails `CROSSSLOT`,
    turning a working control into an outage on the traffic it touches.

    All three keys carry the same `{qgshapes}` tag, so they hash to one slot and
    every key the script touches can be a *declared* KEY. The first design
    dodged CROSSSLOT by building a per-pair key inside Lua and reaching it with
    `redis.call` — which is worse, because touching an undeclared key is what
    Cluster actually forbids. It also silently lost every eviction; the bound
    test below is what caught it.
    """
    for key in _ALL_KEYS:
        assert "{qgshapes}" in key
    for index in range(1, len(_ALL_KEYS) + 1):
        assert f"KEYS[{index}]" in _RECORD_SCRIPT
    assert f"KEYS[{len(_ALL_KEYS) + 1}]" not in _RECORD_SCRIPT
    assert "qg:shapes" not in _RECORD_SCRIPT, (
        "the script must not build a key name itself — every key it touches has "
        "to be declared in KEYS, or it breaks on Redis Cluster. Note neither "
        "fakeredis nor a single-node Redis can detect this, which is why the "
        "guard is this source-level assertion (see TODO.md item 192)."
    )


def test_the_script_never_parses_the_record_body():
    """A Lua `cjson` round-trip turns every empty JSON array into an empty
    *object* on real Redis (Lua has one table type), so `joins: []`,
    `group_by: []`, `order_by: []`, `correlate: []` and `ctes: []` all came back
    as `{}` and every Redis-sourced draft failed
    `validate_template_structure` with five "Input should be a valid list"
    errors. fakeredis's Lua bridge preserves the distinction, so the entire test
    suite passed while the durable backend was broken
    (`security-invariant-reviewer`, 2026-08-17) — the mirror image of the
    ZPOPMIN trap, with fakeredis the forgiving one this time.

    A source-level assertion because the divergence is invisible under
    fakeredis: the body must be written verbatim and never decoded, with every
    mutable field held in its own Redis structure that Lua updates with an
    integer/string primitive.
    """
    assert "cjson" not in _RECORD_SCRIPT
    assert "HSETNX" in _RECORD_SCRIPT  # body written once, never rewritten
    assert "HINCRBY" in _RECORD_SCRIPT  # occurrences, not a parsed JSON field


def test_the_ttl_is_refreshed_on_every_key_unconditionally():
    """The counters key used to be EXPIREd only inside `if evicted > 0`, so
    traffic that kept refreshing the records but stopped evicting outlived its
    own warning counters — an incomplete list started reporting itself as
    complete and the UI's "this list is incomplete" warning vanished.
    """
    assert "for index = 1, 5 do" in _RECORD_SCRIPT
    # The EXPIRE must not be nested under the eviction branch.
    eviction_branch = _RECORD_SCRIPT.split("if evicted > 0 then")[1].split("end")[0]
    assert "EXPIRE" not in eviction_branch


@pytest.mark.asyncio
async def test_a_principal_id_containing_the_separator_still_filters_correctly(store):
    """A JWT subject can legitimately contain `|`. Filtering reads the decoded
    record's own fields rather than parsing the member string, so this cannot
    shift the parsed connection or hash.
    """
    await store.record(_query(), connection_id="demo", principal_id="agent|with|pipes")
    assert len(await store.list_shapes(principal_id="agent|with|pipes")) == 1
    assert len(await store.list_shapes(connection_id="demo")) == 1
    assert _member("demo", "agent|with|pipes", "deadbeef").startswith("demo|agent|with|pipes|")


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registering_the_redis_store_switches_the_active_backend(redis_client):
    from querygate.admin.observed_shapes import (
        clear_redis_observed_shape_store,
        configure_observed_shape_store,
        init_redis_observed_shape_store,
    )

    assert observed_shape_store().scope == "process-local-volatile"
    init_redis_observed_shape_store(RedisObservedShapeStore(redis_client, enabled=True))
    assert observed_shape_store().scope == "shared-durable"

    # Re-configuring the in-process ceiling must NOT silently demote a shared
    # deployment back to per-process — that would revert the guarantee an
    # operator is relying on, with no signal.
    configure_observed_shape_store(100, enabled=True)
    assert observed_shape_store().scope == "shared-durable"

    clear_redis_observed_shape_store()
    assert observed_shape_store().scope == "process-local-volatile"


@pytest.mark.asyncio
async def test_the_reported_bound_is_the_one_that_actually_binds(redis_client):
    """In a fleet whose replicas disagree on `OBSERVED_SHAPES_MAX_ENTRIES` — or
    mid-rolling-deploy — the ceiling that binds is whichever replica last wrote,
    not whichever reads. Reporting the reader's own config made the UI say
    "evicted because the store hit its 100-entry bound" about a store whose real
    ceiling was 2, sending the operator to raise a limit that was not the
    problem.
    """
    generous = RedisObservedShapeStore(redis_client, max_entries=100, enabled=True)
    strict = RedisObservedShapeStore(redis_client, max_entries=2, enabled=True)
    for index in range(6):
        await generous.record(
            _query(select=[f"orders.c{index}"]), connection_id="demo", principal_id="agent"
        )
    assert len(await generous.list_shapes()) == 6
    await strict.record(_query(), connection_id="demo", principal_id="agent")
    assert len(await generous.list_shapes()) == 2
    # Read from the generous replica: it must report the ceiling doing the work.
    assert (await generous.counters()).max_entries == 2


@pytest.mark.asyncio
async def test_an_outage_is_reported_as_unhealthy_not_as_an_idle_agent():
    """Fail-open means an outage renders as an EMPTY list — indistinguishable
    from "recording is on and your agent ran nothing", which is precisely the
    confusion that makes an operator narrow a connection to nothing. So
    reachability is surfaced on the report, not only logged server-side.
    """
    from querygate.admin.observed_shapes import build_observed_shape_report

    store = RedisObservedShapeStore(_BrokenRedis(), enabled=True)
    assert store.backend_healthy is True
    assert await store.record(_query(), connection_id="demo", principal_id="agent") is None
    report = await build_observed_shape_report(store=store)
    assert report.enabled is True
    assert report.shapes == []
    assert report.backend_healthy is False


@pytest.mark.asyncio
async def test_an_empty_array_in_the_skeleton_survives_the_round_trip(store):
    """The end-to-end form of the cjson finding: what an operator actually hits
    is "every drafted template is structurally invalid". Asserted on the decoded
    record so it holds for whichever backend is under test.
    """
    from querygate.templates.binding import validate_template_structure

    await store.record(_query(), connection_id="demo", principal_id="agent")
    shape = (await store.list_shapes())[0]
    for key in ("joins", "group_by", "order_by", "correlate", "ctes"):
        assert isinstance(
            shape.skeleton[key], list
        ), f"{key} decoded as {type(shape.skeleton[key])}"
    assert validate_template_structure(shape.to_template_draft("drafted")) is None


@pytest.mark.asyncio
async def test_varying_an_alias_cannot_mint_entries_or_evict_another_principal(redis_client):
    """The residual eviction vector after the limit/offset fix: `alias` is
    caller-authored, so leaving it in the shape hash let a caller mint unbounded
    "distinct" shapes and push another principal's shapes out of a bound that is
    fleet-global on this backend (`security-invariant-reviewer`, 2026-08-17).
    """
    from querygate.query_ast.models import AggregateSelectItem

    store = RedisObservedShapeStore(redis_client, max_entries=3, enabled=True)
    await store.record(_query(), connection_id="demo", principal_id="victim")
    for index in range(40):
        await store.record(
            _query(select=[AggregateSelectItem(fn="sum", col="orders.total", alias=f"a{index}")]),
            connection_id="demo",
            principal_id="attacker",
        )
    # 40 alias variations collapse to ONE shape, so the victim survives.
    assert len(await store.list_shapes(principal_id="attacker")) == 1
    assert len(await store.list_shapes(principal_id="victim")) == 1
    assert (await store.counters()).evicted_total == 0
