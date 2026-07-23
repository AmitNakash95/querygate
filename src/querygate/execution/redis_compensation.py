"""Redis-backed cross-replica compensation store (TODO.md item 93 phase 3b).

The durable sibling of `compensation.InMemoryCompensationStore`: a
`compensation_id` minted on one replica is resolvable by every replica, so
`POST /write/undo` works under the multi-replica HA deployment (item 56) instead
of only on the pod that served the write — closing the self-review's #1 gap.

Design mirrors `redis_quota.py`/`redis_concurrency.py`: one Redis key per record
(`qg:comp:{id}`) holding the JSON-serialized `CompensationRecord`, with a
key-expiry TTL (the record's own TTL) and single-use via delete-on-consume.

**Sensitivity, stated honestly.** A pre-image necessarily holds real row *values*
(you cannot restore what you redact), so this store puts those values in Redis.
Secure that Redis exactly as you would the concurrency/quota one — network
isolation + auth — and note the TTL bounds exposure. Field-level
encryption-at-rest of the pre-image is a further hardening, not yet implemented;
the redaction-safe audit stream still carries only the `compensation_id` + counts.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import time
from decimal import Decimal
from typing import Optional

from querygate.execution.compensation import CompensationRecord


def _json_default(obj):
    if isinstance(obj, (dt.datetime, dt.date, dt.time)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return str(obj)
    return str(obj)


def _key(compensation_id: str) -> str:
    return f"qg:comp:{compensation_id}"


class RedisCompensationStore:
    """Cross-replica compensation store satisfying the async `CompensationStore`
    shape. Pre-image values round-trip through JSON as strings (datetime/Decimal);
    the write compiler's `_coerce_write_value` restores their Python types on undo."""

    def __init__(self, redis_client) -> None:
        self._redis = redis_client

    async def put(self, record: CompensationRecord) -> None:
        ttl = max(1, int(record.expires_at - time.time()))
        payload = json.dumps(dataclasses.asdict(record), default=_json_default)
        await self._redis.set(_key(record.compensation_id), payload, ex=ttl)

    async def get(self, compensation_id: str) -> Optional[CompensationRecord]:
        raw = await self._redis.get(_key(compensation_id))
        if raw is None:
            return None
        data = json.loads(raw)
        record = CompensationRecord(**data)
        # Belt-and-suspenders alongside the key TTL.
        if record.consumed or record.expires_at < time.time():
            return None
        return record

    async def consume(self, compensation_id: str) -> None:
        # Single-use: the record is gone after one undo, so it can't be replayed.
        await self._redis.delete(_key(compensation_id))
