"""RedisDisclosureBudgetLimiter against fakeredis (TODO.md item 179).

Proves the cross-replica budget's semantics hold via the Lua script — window
expiry, per-key isolation, the all-or-nothing multi-key charge — and (standing
in for cross-replica) that two limiter instances sharing one Redis enforce a
single shared budget rather than one budget each. Also pins the deliberate
fail-CLOSED posture, which diverges from `redis_quota.py`/`redis_concurrency.py`.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest
from redis.exceptions import RedisError

from querygate.core.exceptions import DisclosureBudgetExceededError
from querygate.execution.disclosure_budget import KIND_SHAPE, KIND_TABLE
from querygate.execution.redis_disclosure_budget import (
    _RESERVE_SCRIPT,
    RedisDisclosureBudgetLimiter,
    _redis_key,
)

pytestmark = pytest.mark.unit

KEY_SHAPE = ("demo", "agent", "", "employees", "shape-a")
KEY_TABLE = ("demo", "agent", "", "employees", "")


@pytest.fixture
def redis_client():
    return fakeredis.aioredis.FakeRedis()


@pytest.mark.asyncio
async def test_admits_up_to_the_cap_then_rejects_with_retry_after(redis_client):
    lim = RedisDisclosureBudgetLimiter(redis_client)
    for _ in range(2):
        await lim.reserve([(KEY_SHAPE, 2, KIND_SHAPE, 1)], window_seconds=600, now=100.0)
    with pytest.raises(DisclosureBudgetExceededError) as excinfo:
        await lim.reserve([(KEY_SHAPE, 2, KIND_SHAPE, 1)], window_seconds=600, now=100.0)
    assert excinfo.value.quota_kind == KIND_SHAPE
    assert excinfo.value.retry_after_seconds == 600


@pytest.mark.asyncio
async def test_window_rolls_forward(redis_client):
    lim = RedisDisclosureBudgetLimiter(redis_client)
    await lim.reserve([(KEY_SHAPE, 1, KIND_SHAPE, 1)], window_seconds=600, now=100.0)
    with pytest.raises(DisclosureBudgetExceededError):
        await lim.reserve([(KEY_SHAPE, 1, KIND_SHAPE, 1)], window_seconds=600, now=200.0)
    await lim.reserve([(KEY_SHAPE, 1, KIND_SHAPE, 1)], window_seconds=600, now=701.0)


@pytest.mark.asyncio
async def test_distinct_keys_are_independent(redis_client):
    lim = RedisDisclosureBudgetLimiter(redis_client)
    await lim.reserve([(KEY_SHAPE, 1, KIND_SHAPE, 1)], window_seconds=600, now=100.0)
    await lim.reserve([(KEY_TABLE, 1, KIND_TABLE, 1)], window_seconds=600, now=100.0)


@pytest.mark.asyncio
async def test_the_tripped_key_determines_the_reported_kind(redis_client):
    """The second key in the charge list is the exhausted one, so the rejection
    must report *its* kind — not the first one's."""
    lim = RedisDisclosureBudgetLimiter(redis_client)
    await lim.reserve([(KEY_TABLE, 1, KIND_TABLE, 1)], window_seconds=600, now=100.0)
    with pytest.raises(DisclosureBudgetExceededError) as excinfo:
        await lim.reserve(
            [(KEY_SHAPE, 5, KIND_SHAPE, 1), (KEY_TABLE, 1, KIND_TABLE, 1)],
            window_seconds=600,
            now=100.0,
        )
    assert excinfo.value.quota_kind == KIND_TABLE


@pytest.mark.asyncio
async def test_a_refused_charge_spends_nothing_on_its_other_keys(redis_client):
    lim = RedisDisclosureBudgetLimiter(redis_client)
    await lim.reserve([(KEY_TABLE, 1, KIND_TABLE, 1)], window_seconds=600, now=100.0)
    for _ in range(5):
        with pytest.raises(DisclosureBudgetExceededError):
            await lim.reserve(
                [(KEY_SHAPE, 3, KIND_SHAPE, 1), (KEY_TABLE, 1, KIND_TABLE, 1)],
                window_seconds=600,
                now=100.0,
            )
    # KEY_SHAPE was never charged by any of those refusals.
    for _ in range(3):
        await lim.reserve([(KEY_SHAPE, 3, KIND_SHAPE, 1)], window_seconds=600, now=100.0)


@pytest.mark.asyncio
async def test_two_replicas_share_one_budget(redis_client):
    """The whole point of the Redis backend: without it an N-replica deployment
    silently gives a prober N× the probes the operator configured."""
    replica_a = RedisDisclosureBudgetLimiter(redis_client)
    replica_b = RedisDisclosureBudgetLimiter(redis_client)
    await replica_a.reserve([(KEY_SHAPE, 2, KIND_SHAPE, 1)], window_seconds=600, now=100.0)
    await replica_b.reserve([(KEY_SHAPE, 2, KIND_SHAPE, 1)], window_seconds=600, now=100.0)
    with pytest.raises(DisclosureBudgetExceededError):
        await replica_a.reserve([(KEY_SHAPE, 2, KIND_SHAPE, 1)], window_seconds=600, now=100.0)


@pytest.mark.asyncio
async def test_empty_charge_list_is_a_noop(redis_client):
    await RedisDisclosureBudgetLimiter(redis_client).reserve([], window_seconds=600)


@pytest.mark.asyncio
async def test_unreachable_redis_fails_closed(redis_client):
    """Deliberately diverges from the quota/concurrency limiters, which fail
    OPEN. This is a privacy control defending a k-anonymity floor: failing open
    would silently suspend that defense while the operator's config still says
    it is enforced.
    """

    class _Boom:
        async def __call__(self, *args, **kwargs):
            raise RedisError("connection refused")

    lim = RedisDisclosureBudgetLimiter(redis_client)
    lim._reserve = _Boom()
    with pytest.raises(DisclosureBudgetExceededError) as excinfo:
        await lim.reserve([(KEY_SHAPE, 5, KIND_SHAPE, 1)], window_seconds=600, now=100.0)
    # The message must say the guardrail could not be evaluated, NOT that a
    # budget was exhausted — an operator reading logs must not be misled into
    # tuning a threshold that was never consulted.
    assert "could not be evaluated" in str(excinfo.value)


@pytest.mark.asyncio
async def test_every_key_gets_a_window_length_ttl(redis_client):
    """The Redis backend's answer to unbounded key growth. Without the EXPIRE a
    production Redis accumulates one permanent zset per
    (connection, principal, purpose, table, shape) forever."""
    lim = RedisDisclosureBudgetLimiter(redis_client)
    await lim.reserve([(KEY_SHAPE, 5, KIND_SHAPE, 1)], window_seconds=600, now=100.0)
    assert await redis_client.ttl(_redis_key(KEY_SHAPE)) > 0


@pytest.mark.asyncio
async def test_keys_differing_only_in_separator_placement_do_not_collide(redis_client):
    """`purpose` is free text and an IdP `sub` may contain ':', so a raw
    f-string join would let two distinct logical keys flatten to one Redis key —
    and the two implementations of this Protocol would then disagree about key
    identity (the in-process one keys on a tuple and cannot collide)."""
    a = ("demo", "agent:x", "", "employees", "fp")
    b = ("demo", "agent", "x", "employees", "fp")
    assert _redis_key(a) != _redis_key(b)
    lim = RedisDisclosureBudgetLimiter(redis_client)
    await lim.reserve([(a, 1, KIND_SHAPE, 1)], window_seconds=600, now=100.0)
    # b has its own budget; a collision would refuse this.
    await lim.reserve([(b, 1, KIND_SHAPE, 1)], window_seconds=600, now=100.0)


@pytest.mark.asyncio
async def test_a_weighted_charge_costs_its_full_weight(redis_client):
    """Weight parity with the in-process limiter: one statement covering several
    aggregating scopes must cost several units here too, or the Redis backend
    would enforce a weaker bound than the default one."""
    lim = RedisDisclosureBudgetLimiter(redis_client)
    await lim.reserve([(KEY_SHAPE, 3, KIND_SHAPE, 2)], window_seconds=600, now=100.0)
    with pytest.raises(DisclosureBudgetExceededError):
        await lim.reserve([(KEY_SHAPE, 3, KIND_SHAPE, 2)], window_seconds=600, now=100.0)
    await lim.reserve([(KEY_SHAPE, 3, KIND_SHAPE, 1)], window_seconds=600, now=100.0)


@pytest.mark.asyncio
async def test_retry_after_counts_down_as_the_window_ages(redis_client):
    """Pinned away from the degenerate zero-age point, matching the in-process
    limiter's own test — the Lua arithmetic is a separate implementation of the
    same contract and can drift from it."""
    lim = RedisDisclosureBudgetLimiter(redis_client)
    await lim.reserve([(KEY_SHAPE, 1, KIND_SHAPE, 1)], window_seconds=600, now=100.0)
    with pytest.raises(DisclosureBudgetExceededError) as excinfo:
        await lim.reserve([(KEY_SHAPE, 1, KIND_SHAPE, 1)], window_seconds=600, now=400.0)
    assert excinfo.value.retry_after_seconds == 300


# ---------------------------------------------------------------------------
# Redis Cluster safety (TODO.md item 192)
# ---------------------------------------------------------------------------


def _hash_tag(key: str) -> str:
    """The span Redis Cluster actually hashes: between the first `{` and the
    first `}` after it. An empty span means Redis ignores the tag and hashes the
    whole key — which is the failure this guard exists to catch."""
    open_at = key.find("{")
    if open_at < 0:
        return ""
    close_at = key.find("}", open_at + 1)
    if close_at < 0:
        return ""
    return key[open_at + 1 : close_at]


def test_every_key_one_reserve_call_passes_shares_one_hash_slot():
    """Item 192. This limiter is the only one that hands SEVERAL keys to one Lua
    script, so on Redis Cluster they must hash to one slot or the call fails
    `CROSSSLOT` → `RedisError` → this limiter's fail-CLOSED branch → every
    aggregate query on a k-floored connection is refused.

    `enforce_disclosure_budget` builds every charge in a single call from its own
    `connection_id`, so tagging on the connection is sufficient *and* necessary.
    A source-level assertion because neither `fakeredis` (models no slots) nor a
    single-node Redis (has exactly one) can observe the bug — the same reason
    `test_redis_observed_shapes.py` guards its script this way.
    """
    charged_in_one_call = [
        ("demo", "agent", "", "employees", ""),
        ("demo", "agent", "", "salaries", ""),
        ("demo", "agent", "", "employees", "shape-a"),
        ("demo", "agent", "", "salaries", "shape-b"),
    ]
    tags = {_hash_tag(_redis_key(key)) for key in charged_in_one_call}
    assert tags == {"demo"}, f"keys in one script call span several hash slots: {tags}"


def test_a_different_connection_gets_a_different_tag():
    """The tag must be the *connection*, not a constant: a constant would put
    every deployment's entire budget in one Cluster slot for no reason."""
    assert _hash_tag(_redis_key(("demo", "a", "", "t", ""))) == "demo"
    assert _hash_tag(_redis_key(("other", "a", "", "t", ""))) == "other"


def test_the_hash_tag_is_never_empty():
    """Redis ignores an EMPTY `{}` tag and hashes the whole key instead, which
    silently reinstates the CROSSSLOT bug. Nothing should hand us a blank
    connection id, but the failure mode is invisible, so it is closed by
    construction."""
    assert _hash_tag(_redis_key(("", "a", "", "t", ""))) != ""


def test_a_brace_in_a_connection_id_cannot_open_a_second_tag():
    """Percent-escaping runs inside the braces, so `{`/`}` in an identifier
    become `%7B`/`%7D` and the first `{...}` span stays the one we wrote."""
    key = _redis_key(("we{ird}", "a", "", "t", ""))
    assert _hash_tag(key) == "we%7Bird%7D"
    assert key.count("{") == 1 and key.count("}") == 1


def test_the_script_declares_every_key_it_touches():
    """Reaching a key built inside Lua via `redis.call` is what Cluster forbids —
    a worse version of the same bug, and equally invisible to fakeredis."""
    assert "qg:disclosure" not in _RESERVE_SCRIPT
    assert "KEYS[i]" in _RESERVE_SCRIPT
