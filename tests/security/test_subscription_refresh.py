"""The refresh loop's two fail-open promises, driven rather than read.

`SubscriptionManager` documents "fail-open per iteration" and "a refresh failure
is never fatal". Both were true of the paths its tests exercised and false of two
that nothing exercised, and each failure is silent in the direction that says
everything is fine:

* A document this build **cannot verify** was written to the cache before
  verification, evicting the last one it could read. The deployment then falls
  back to `evaluate(None, ...)` — grace, observe, no `expires_at` — so item
  216's banner, countdown and `/health` signal all go quiet, while `refresh`
  still reports OK because the *fetch* succeeded.
* An exception that is not an `EntitlementVerificationError` escaped
  `refresh_once`, escaped the timer loop, and ended the task for the process
  lifetime. One bad issuance would freeze every deployment's verdict at once.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from querygate.subscription.manager import SubscriptionManager
from querygate.subscription.models import SCHEMA_VERSION, EntitlementState
from querygate.subscription.state import subscription_state
from querygate.subscription.verify import ENTITLEMENT_DOMAIN, TrustedKey

pytestmark = [pytest.mark.security, pytest.mark.unit]

NOW = datetime.now(timezone.utc)
DEPLOYMENT = "dep_refresh_test"


@pytest.fixture
def signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def trusted(signing_key) -> dict[str, TrustedKey]:
    return {"k1": TrustedKey(key_id="k1", public_key=signing_key.public_key())}


def _envelope(key: Ed25519PrivateKey, **overrides) -> bytes:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "org_id": "org_test",
        "deployment_id": DEPLOYMENT,
        "serial": 5,
        "issued_at": (NOW - timedelta(days=1)).isoformat(),
        "expires_at": (NOW + timedelta(days=10)).isoformat(),
        "grace_expires_at": (NOW + timedelta(days=24)).isoformat(),
        "enforcement": "enforce",
        "renewal_state": "cancelling",
        "plan": "team",
        "max_connections": 10,
        "max_seats": 25,
        "renewal_url": "https://example.invalid/renew",
    }
    payload.update(overrides)
    raw = json.dumps(payload).encode("utf-8")
    signature = key.sign(ENTITLEMENT_DOMAIN + b"." + raw)
    return json.dumps(
        {
            "key_id": "k1",
            "payload": base64.b64encode(raw).decode(),
            "signature": base64.b64encode(signature).decode(),
        }
    ).encode("utf-8")


class RecordingCache:
    """A real cache that remembers what it was asked to store."""

    def __init__(self, initial: bytes | None = None) -> None:
        self._raw = initial
        self.stores: list[bytes] = []

    async def load(self) -> bytes | None:
        return self._raw

    async def store(self, raw: bytes) -> None:
        self.stores.append(raw)
        self._raw = raw


class Source:
    def __init__(self, raw: bytes | Exception) -> None:
        self._raw = raw

    async def fetch(self) -> bytes:
        if isinstance(self._raw, Exception):
            raise self._raw
        return self._raw


def _manager(source, cache, trusted) -> SubscriptionManager:
    return SubscriptionManager(
        source=source, cache=cache, trusted_keys=trusted, deployment_id=DEPLOYMENT
    )


# --- the cache is a record of what verified, not of what arrived ---------------


def test_a_document_that_fails_verification_never_reaches_the_cache(signing_key, trusted):
    """The eviction that would silently remove the countdown.

    A fetched-but-unreadable document (an unknown schema version is the realistic
    case, and the first-ever bump made it reachable) must not overwrite the last
    good envelope. Storing it means the next restart loads garbage, verification
    fails again, and the deployment sits in grace with no expiry date — so no
    banner, no CLI countdown, and `/health` back to `ok` — at exactly the moment
    something is wrong.
    """
    good = _envelope(signing_key)
    cache = RecordingCache(initial=good)
    unreadable = _envelope(signing_key, schema_version=SCHEMA_VERSION + 1)

    asyncio.run(_manager(Source(unreadable), cache, trusted).refresh_once())

    assert cache.stores == [], "an unverifiable document was written to the cache"
    assert cache._raw == good


def test_a_document_that_verifies_is_cached(signing_key, trusted):
    """The positive control: without it, `stores == []` passes for a manager that
    never caches anything at all."""
    cache = RecordingCache()
    raw = _envelope(signing_key)

    asyncio.run(_manager(Source(raw), cache, trusted).refresh_once())

    assert cache.stores == [raw]
    assert subscription_state().status.entitlement is EntitlementState.VALID


def test_the_last_good_verdict_survives_an_unreadable_refresh(signing_key, trusted):
    """End to end, through the published status: a good refresh then a bad one.

    The verdict does fall back to grace (that is `evaluate`'s documented
    fail-open), but the *cache* must still hold the good envelope so the next
    restart recovers rather than compounding.
    """
    cache = RecordingCache()
    good = _envelope(signing_key)
    asyncio.run(_manager(Source(good), cache, trusted).refresh_once())
    assert subscription_state().status.expires_at is not None

    unreadable = _envelope(signing_key, schema_version=SCHEMA_VERSION + 1)
    asyncio.run(_manager(Source(unreadable), cache, trusted).refresh_once())

    assert cache._raw == good
    # And a restart against that cache recovers the countdown.
    asyncio.run(_manager(Source(RuntimeError("offline")), cache, trusted).refresh_once())
    assert subscription_state().status.expires_at is not None


# --- one bad iteration must not end the loop ----------------------------------


def test_a_refresh_that_raises_does_not_end_the_timer(signing_key, trusted, monkeypatch):
    """ "A refresh failure is never fatal" — including a failure that is not the
    one type `_verify` catches.

    `_run` had no `try` around `refresh_once`, so anything other than an
    `EntitlementVerificationError` ended the task silently and froze the verdict
    for the process lifetime. Driven by making `refresh_once` itself raise, which
    is the only way to reach the loop's own handler.
    """
    manager = _manager(Source(_envelope(signing_key)), RecordingCache(), trusted)
    manager._interval = 0.01
    calls: list[int] = []

    async def exploding(*, use_cache_only: bool = False) -> None:
        # `start()` calls this once with use_cache_only=True before the loop;
        # only the loop's own calls need to raise.
        if use_cache_only:
            return
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("something no handler expected")

    monkeypatch.setattr(manager, "refresh_once", exploding)

    async def drive() -> None:
        await manager.start()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(calls) >= 3:
                break
        running = manager._task is not None and not manager._task.done()
        await manager.stop()
        assert running, "the refresh task ended after an unexpected exception"

    asyncio.run(drive())
    assert len(calls) >= 3, calls
