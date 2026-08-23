"""The cross-replica SSO stores, and the traps they had to be shaped around.

Two of these tests are source-level assertions rather than behavioural ones,
deliberately. CLAUDE.md records that neither fakeredis nor a single-node Redis
can detect the two failure modes that matter most here — a `cjson` round trip
mangling empty arrays, and a Lua script touching an undeclared key — so a
behavioural test would pass on a defect that only appears in production, on real
Redis, in a Cluster. The guard has to be that the code does not do those things
at all.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from redis.exceptions import RedisError

from querygate.identity import redis_sessions
from querygate.identity.config_store import IdentityConfigStore, set_identity_store
from querygate.identity.device import DeviceGrant, IssuedToken
from querygate.identity.redis_sessions import (
    RedisDeviceGrantStore,
    RedisIssuedTokenStore,
    RedisLoginFlowStore,
    RedisSessionStore,
)
from querygate.identity.sessions import LoginFlow, SsoSession

pytestmark = pytest.mark.unit

fakeredis = pytest.importorskip("fakeredis")


@pytest.fixture
def redis_client():
    return fakeredis.aioredis.FakeRedis()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _session(**overrides) -> SsoSession:
    fields = {
        "session_digest": "d" * 64,
        "subject": "alice",
        "provider_id": "entra",
        "auth_method": "sso_session",
        "csrf_token": "qgc_token",
        "display_name": "Alice",
        "email": "alice@example.com",
        # Arrays and nesting on purpose: this is the exact shape a cjson round
        # trip would corrupt, so every test here carries it through the store.
        "claims": {
            "sub": "alice",
            "groups": ["platform", "readers"],
            "realm_access": {"roles": ["qg-admin"]},
            "empty_list": [],
            "empty_object": {},
        },
        "created_at": _now(),
        "expires_at": _now() + timedelta(hours=8),
        "last_seen_at": _now(),
    }
    fields.update(overrides)
    return SsoSession(**fields)


@pytest.fixture(autouse=True)
def _identity_config():
    set_identity_store(
        IdentityConfigStore.from_dict(
            {
                "sso": {"session_idle_timeout_seconds": 3600},
                "providers": [
                    {"id": "entra", "preset": "entra_id", "tenant": "t", "client_id": "c"}
                ],
            }
        )
    )


class TestSessionStore:
    async def test_round_trip_preserves_arrays_and_nesting_exactly(self, redis_client):
        store = RedisSessionStore(redis_client)
        original = _session()
        await store.create(original)
        loaded = await store.get(original.session_digest)
        assert loaded is not None
        # The specific corruption a Lua `cjson` round trip causes: an empty
        # array comes back as an empty object. Asserted by type, not equality,
        # because `[] == {}` is False in Python but the mistake is easy to make
        # in a way that still validates.
        assert loaded.claims["empty_list"] == []
        assert isinstance(loaded.claims["empty_list"], list)
        assert isinstance(loaded.claims["empty_object"], dict)
        assert loaded.claims["groups"] == ["platform", "readers"]
        assert loaded.claims["realm_access"]["roles"] == ["qg-admin"]
        assert loaded.subject == "alice" and loaded.csrf_token == "qgc_token"

    async def test_an_unknown_digest_resolves_to_nobody(self, redis_client):
        assert await RedisSessionStore(redis_client).get("f" * 64) is None

    async def test_touch_updates_the_clock_without_rewriting_the_document(self, redis_client):
        store = RedisSessionStore(redis_client)
        session = _session()
        await store.create(session)
        moved = _now() + timedelta(minutes=5)
        await store.touch(session.session_digest, moved)
        loaded = await store.get(session.session_digest)
        assert loaded is not None
        assert abs((loaded.last_seen_at - moved).total_seconds()) < 1
        # The claim set survived a clock update untouched — the property that
        # would break if `touch` read, decoded, and rewrote the document.
        assert loaded.claims["empty_list"] == []
        assert loaded.claims["groups"] == ["platform", "readers"]

    async def test_delete_removes_the_session_and_its_index_entry(self, redis_client):
        store = RedisSessionStore(redis_client)
        session = _session()
        await store.create(session)
        await store.delete(session.session_digest)
        assert await store.get(session.session_digest) is None
        assert await store.delete_for_subject("alice") == 0

    async def test_revoking_a_subject_removes_every_session_they_hold(self, redis_client):
        store = RedisSessionStore(redis_client)
        for index in range(3):
            await store.create(_session(session_digest=str(index) * 64))
        assert await store.delete_for_subject("alice") == 3
        for index in range(3):
            assert await store.get(str(index) * 64) is None

    async def test_revoking_one_person_leaves_another_alone(self, redis_client):
        store = RedisSessionStore(redis_client)
        await store.create(_session(session_digest="a" * 64, subject="alice"))
        await store.create(_session(session_digest="b" * 64, subject="bob"))
        assert await store.delete_for_subject("alice") == 1
        assert (await store.get("b" * 64)) is not None

    async def test_an_expired_session_is_not_returned(self, redis_client):
        store = RedisSessionStore(redis_client)
        session = _session(expires_at=_now() - timedelta(seconds=1))
        # Written directly: `create` would compute a TTL of zero and Redis would
        # reject it, which would hide the check under test.
        await redis_client.set(
            redis_sessions.SESSION_KEY.format(digest=session.session_digest),
            session.model_dump_json(),
        )
        assert await store.get(session.session_digest) is None

    async def test_an_idle_session_is_not_returned(self, redis_client):
        store = RedisSessionStore(redis_client)
        session = _session()
        await store.create(session)
        await store.touch(session.session_digest, _now() - timedelta(hours=2))
        assert await store.get(session.session_digest) is None

    async def test_a_corrupt_document_resolves_to_nobody_rather_than_raising(self, redis_client):
        store = RedisSessionStore(redis_client)
        await redis_client.set(redis_sessions.SESSION_KEY.format(digest="c" * 64), "not json")
        assert await store.get("c" * 64) is None
        await redis_client.set(
            redis_sessions.SESSION_KEY.format(digest="c" * 64), '{"subject": "alice"}'
        )
        assert await store.get("c" * 64) is None


class TestLoginFlowStore:
    def _flow(self, **overrides) -> LoginFlow:
        fields = {
            "flow_id": "qgf_one",
            "provider_id": "entra",
            "state": "the-state",
            "nonce": "the-nonce",
            "code_verifier": "the-verifier",
            "redirect_uri": "https://qg.example.com/api/v1/auth/sso/callback",
            "return_to": "/admin/",
            "created_at": _now(),
            "expires_at": _now() + timedelta(minutes=10),
        }
        fields.update(overrides)
        return LoginFlow(**fields)

    async def test_a_flow_is_consumed_exactly_once(self, redis_client):
        store = RedisLoginFlowStore(redis_client)
        await store.put(self._flow())
        first = await store.consume("qgf_one")
        assert first is not None and first.code_verifier == "the-verifier"
        # The property that makes a replayed callback inert across replicas.
        assert await store.consume("qgf_one") is None

    async def test_an_unknown_flow_is_none(self, redis_client):
        assert await RedisLoginFlowStore(redis_client).consume("qgf_nope") is None


class TestDeviceStores:
    def _grant(self, **overrides) -> DeviceGrant:
        fields = {
            "device_code_digest": "g" * 64,
            "user_code": "CDFG-3467",
            "client_name": "querygate-cli",
            "requested_scopes": [],
            "created_at": _now(),
            "expires_at": _now() + timedelta(minutes=10),
        }
        fields.update(overrides)
        return DeviceGrant(**fields)

    async def test_a_grant_is_reachable_by_both_of_its_keys(self, redis_client):
        store = RedisDeviceGrantStore(redis_client)
        await store.put(self._grant())
        assert (await store.by_device_code("g" * 64)) is not None
        assert (await store.by_user_code("CDFG-3467")) is not None

    async def test_an_empty_requested_scope_list_survives_the_round_trip(self, redis_client):
        """`requested_scopes: []` means "act as the approver" — turning it into
        an object would fail validation and break the whole grant."""
        store = RedisDeviceGrantStore(redis_client)
        await store.put(self._grant(requested_scopes=[]))
        loaded = await store.by_device_code("g" * 64)
        assert loaded is not None and loaded.requested_scopes == []
        assert isinstance(loaded.requested_scopes, list)

    async def test_deleting_a_grant_clears_its_user_code_alias(self, redis_client):
        store = RedisDeviceGrantStore(redis_client)
        await store.put(self._grant())
        await store.delete("g" * 64)
        assert await store.by_user_code("CDFG-3467") is None

    async def test_issued_tokens_round_trip_and_revoke_by_subject(self, redis_client):
        store = RedisIssuedTokenStore(redis_client)
        for index in range(2):
            await store.put(
                IssuedToken(
                    token_digest=str(index) * 64,
                    subject="alice",
                    provider_id="entra",
                    client_name="cli",
                    scopes=["admin:metrics:read"],
                    approver_claims={"groups": ["platform"], "empty": []},
                    created_at=_now(),
                    expires_at=_now() + timedelta(hours=1),
                )
            )
        listed = await store.list_for_subject("alice")
        assert len(listed) == 2
        assert listed[0].approver_claims["empty"] == []
        assert await store.delete_for_subject("alice") == 2
        assert await store.list_for_subject("alice") == []


class TestRedisUsageIsShapedAroundTheKnownTraps:
    """Source-level guards for two defects no fakeredis test can catch.

    Checked over the parsed AST rather than the raw text: the module's own
    docstring *names* `cjson` and `KEYS` while explaining why it avoids them, so
    a substring search would fail on the explanation. What must be absent is a
    call, not a mention.
    """

    def _calls(self) -> set[str]:
        tree = ast.parse(Path(inspect.getfile(redis_sessions)).read_text())
        return {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }

    def test_the_module_never_executes_lua(self):
        forbidden = {"eval", "evalsha", "eval_ro", "register_script", "script_load"}
        offenders = forbidden & self._calls()
        assert not offenders, (
            f"{sorted(offenders)} is called in redis_sessions.py. This module is Lua-free "
            "by design: a scripted implementation would hit the cjson empty-array trap "
            "(item 195 phase 2) or the undeclared-KEYS Cluster trap (item 192), and "
            "neither is detectable under fakeredis."
        )

    def test_every_key_template_carries_exactly_one_hash_tag(self):
        templates = [
            value
            for name, value in vars(redis_sessions).items()
            if name.endswith("_KEY") and isinstance(value, str)
        ]
        assert templates, "no key templates found — this guard would be vacuous"
        for template in templates:
            assert template.count("{{") == 1 and template.count("}}") == 1, template

    def test_no_multi_key_command_is_ever_issued(self):
        """A `DEL a b` across two records is a CROSSSLOT error on a Cluster.

        Every deletion here passes exactly one key, which is why revoking a
        subject loops instead of batching.
        """
        assert not ({"mget", "mset", "msetnx"} & self._calls())
        tree = ast.parse(Path(inspect.getfile(redis_sessions)).read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("delete", "unlink")
            ):
                assert len(node.args) == 1, "a delete must name exactly one key"
                assert not any(isinstance(arg, ast.Starred) for arg in node.args)

    def test_the_store_fails_closed_when_redis_is_unreachable(self):
        """An authentication store that fails open is not one.

        Unlike the concurrency limiter, which may deliberately fail open, every
        read here answers "not signed in" on a Redis error rather than falling
        back to anything permissive.
        """

        class _Broken:
            async def get(self, *_a, **_k):
                raise RedisError("connection refused")

            async def smembers(self, *_a, **_k):
                raise RedisError("connection refused")

            async def getdel(self, *_a, **_k):
                raise RedisError("connection refused")

        async def _exercise() -> None:
            broken = _Broken()
            assert await RedisSessionStore(broken).get("d" * 64) is None
            assert await RedisSessionStore(broken).delete_for_subject("alice") == 0
            assert await RedisLoginFlowStore(broken).consume("qgf_x") is None
            assert await RedisDeviceGrantStore(broken).by_device_code("g" * 64) is None
            assert await RedisIssuedTokenStore(broken).get("t" * 64) is None
            assert await RedisIssuedTokenStore(broken).delete_for_subject("alice") == 0

        asyncio.run(_exercise())
