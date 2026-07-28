"""End-to-end REST test of the `queue_mode=async` lifecycle (TODO.md item 35
phase 3): `202` + `admission_id`, polling `GET .../query/{admission_id}` to a
terminal state, and cancellation — against a real (SQLite) database exactly
like `test_sqlite_end_to_end.py`. The dialect-level DB cancel itself (Postgres
`pg_cancel_backend`/MSSQL `KILL`) is proven separately and mocked in
`tests/unit/test_async_execution.py`/`tests/unit/test_dialects.py`; this test
covers the REST-layer plumbing — admission, polling, ownership, and the two
cancel paths (`queued`, already-terminal) that need no live engine connection.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from examples.demo_db.schema import ORDERS_DATA, create_and_seed_async
from querygate.api.app import create_app
from querygate.core.config import AppConfig

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"


async def _poll_until_terminal(client: AsyncClient, status_url: str, *, timeout_s: float = 5.0):
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        resp = await client.get(status_url)
        assert resp.status_code == 200
        body = resp.json()
        if body["state"] in ("completed", "failed", "cancelled"):
            return body
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"never reached a terminal state: {body}")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_async_query_returns_202_then_completes(sqlite_app):
    completed = [o for o in ORDERS_DATA if o["status"] == "completed"]
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/demo/query?queue_mode=async",
            json={
                "from": "orders",
                "select": ["orders.id", "orders.status"],
                "where": {"col": "orders.status", "op": "eq", "value": "completed"},
                "limit": len(completed),
            },
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["admission_id"]
        assert body["status_url"] == f"/api/v1/demo/query/{body['admission_id']}"

        final = await _poll_until_terminal(client, body["status_url"])
        assert final["state"] == "completed"
        assert final["result"]["row_count"] == len(completed)
        assert all(row["status"] == "completed" for row in final["result"]["rows"])


@pytest.mark.asyncio
async def test_getting_status_of_an_unknown_admission_id_is_404(sqlite_app):
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.get("/api/v1/demo/query/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cancelling_an_already_completed_query_is_an_idempotent_no_op(sqlite_app):
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/demo/query?queue_mode=async",
            json={"from": "orders", "select": ["orders.id"], "limit": 1},
        )
        admission_id = resp.json()["admission_id"]
        await _poll_until_terminal(client, resp.json()["status_url"])

        cancel_resp = await client.post(f"/api/v1/demo/query/{admission_id}/cancel")
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["state"] == "completed"


@pytest.mark.asyncio
async def test_cancelling_a_query_admission_from_a_different_connection_is_404(sqlite_app):
    """`analytics` and `demo` are two different registered connections
    (see conftest's default registry) — an admission_id that exists but
    belongs to `demo` must not resolve under a different connection's path."""
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/demo/query?queue_mode=async",
            json={"from": "orders", "select": ["orders.id"], "limit": 1},
        )
        admission_id = resp.json()["admission_id"]
        await _poll_until_terminal(client, resp.json()["status_url"])

        wrong_connection_resp = await client.get(
            f"/api/v1/nonexistent-connection/query/{admission_id}"
        )
    assert wrong_connection_resp.status_code == 404


@pytest.mark.asyncio
async def test_an_unexpected_cancel_session_failure_is_masked_not_leaked(sqlite_app, monkeypatch):
    """A real Postgres/MSSQL permission error (e.g. an operator enabling
    `allow_query_cancellation` without actually granting `pg_signal_backend`/
    `ALTER ANY CONNECTION` yet) must not leak driver text past the REST
    boundary — the same `mask_unexpected()` guarantee every other DB-touching
    route in this file already gets."""
    from querygate.execution.async_execution import AsyncExecutionRecord, async_execution_store
    from querygate.policy.loader import PolicyStore, set_policy_store
    from querygate.policy.models import Policy

    set_policy_store(PolicyStore(default=Policy(allow_query_cancellation=True), overrides={}))

    record = AsyncExecutionRecord(
        admission_id="running-record-1",
        connection_id="demo",
        principal_subject="anonymous-dev",  # sqlite_app's default (unauthenticated) caller
        state="running",
        session_identifier="4242",
    )
    async_execution_store().put(record)

    async def _boom(engine, dialect, identifier):
        raise RuntimeError("FATAL: permission denied for function pg_cancel_backend")

    monkeypatch.setattr("querygate.execution.async_execution.cancel_session", _boom)

    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post("/api/v1/demo/query/running-record-1/cancel")

    assert resp.status_code == 500
    assert "permission denied" not in resp.text
    assert "pg_cancel_backend" not in resp.text


@pytest.mark.asyncio
async def test_a_different_principal_cannot_view_or_cancel_someone_elses_admission(monkeypatch):
    """The one real cross-principal test in this file: `sqlite_app`'s default
    static-auth setup maps every caller to the SAME anonymous-dev principal,
    which would make an ownership check pass trivially even if deleted
    outright (confirmed by mutation-testing the check during development —
    the other tests in this file did NOT catch it being removed). Two JWTs
    with distinct `sub` claims, the same pattern
    `test_product_guide_api.py`'s per-principal test already established,
    since a single API-key list maps to one shared subject.
    """
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    await create_and_seed_async(engine)

    import querygate.execution.service as svc_module
    import querygate.validation.schema_validation as sv_module
    from contextlib import asynccontextmanager
    from sqlalchemy.ext.asyncio import AsyncSession

    @asynccontextmanager
    async def _session_scope(connection_id, policy=None, *, session_identifier_sink=None):
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    monkeypatch.setattr(svc_module, "get_engine", lambda connection_id: engine)
    monkeypatch.setattr(svc_module, "session_scope", _session_scope)
    monkeypatch.setattr(sv_module, "get_engine", lambda connection_id: engine)

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    app = create_app(
        AppConfig(
            _env_file=None,
            environment="localhost",
            api_keys=[],
            jwt_enabled=True,
            jwt_jwks_url="https://idp.example.com/.well-known/jwks.json",
            jwt_issuer="https://idp.example.com/",
            jwt_audience="querygate",
            mcp_enabled=False,
            audit_sink_backend="none",
        )
    )

    def _token(subject: str) -> str:
        return jwt.encode(
            {"sub": subject, "iss": "https://idp.example.com/", "aud": "querygate"},
            private_key,
            algorithm="RS256",
        )

    from unittest.mock import patch

    with patch(
        "jwt.PyJWKClient.get_signing_key_from_jwt",
        lambda self, tok: type("K", (), {"key": public_key})(),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            submit_resp = await client.post(
                "/api/v1/demo/query?queue_mode=async",
                json={"from": "orders", "select": ["orders.id"], "limit": 1},
                headers={"Authorization": f"Bearer {_token('agent-a')}"},
            )
            assert submit_resp.status_code == 202
            admission_id = submit_resp.json()["admission_id"]
            status_url = submit_resp.json()["status_url"]

            other_status_resp = await client.get(
                status_url, headers={"Authorization": f"Bearer {_token('agent-b')}"}
            )
            other_cancel_resp = await client.post(
                f"/api/v1/demo/query/{admission_id}/cancel",
                headers={"Authorization": f"Bearer {_token('agent-b')}"},
            )
            own_status_resp = await client.get(
                status_url, headers={"Authorization": f"Bearer {_token('agent-a')}"}
            )

    assert other_status_resp.status_code == 404
    assert other_cancel_resp.status_code == 404
    assert own_status_resp.status_code == 200


@pytest.mark.asyncio
async def test_a_principal_with_query_cancel_scope_can_view_and_cancel_someone_elses_admission(
    monkeypatch,
):
    """Audit fix: the sibling test above only proves a cross-principal caller
    WITHOUT query:cancel is denied — it never proves the authorized positive
    path (a caller WITH the scope actually succeeds), so a future regression
    that denied everyone regardless of scope would pass that test too. Same
    two-distinct-JWT setup, but agent-b's token carries the query:cancel
    scope this time.
    """
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    await create_and_seed_async(engine)

    import querygate.execution.service as svc_module
    import querygate.validation.schema_validation as sv_module
    from contextlib import asynccontextmanager
    from sqlalchemy.ext.asyncio import AsyncSession

    @asynccontextmanager
    async def _session_scope(connection_id, policy=None, *, session_identifier_sink=None):
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    monkeypatch.setattr(svc_module, "get_engine", lambda connection_id: engine)
    monkeypatch.setattr(svc_module, "session_scope", _session_scope)
    monkeypatch.setattr(sv_module, "get_engine", lambda connection_id: engine)

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    app = create_app(
        AppConfig(
            _env_file=None,
            environment="localhost",
            api_keys=[],
            jwt_enabled=True,
            jwt_jwks_url="https://idp.example.com/.well-known/jwks.json",
            jwt_issuer="https://idp.example.com/",
            jwt_audience="querygate",
            mcp_enabled=False,
            audit_sink_backend="none",
        )
    )

    def _token(subject: str, *, scope: str = "") -> str:
        payload = {"sub": subject, "iss": "https://idp.example.com/", "aud": "querygate"}
        if scope:
            payload["scope"] = scope
        return jwt.encode(payload, private_key, algorithm="RS256")

    from unittest.mock import patch

    with patch(
        "jwt.PyJWKClient.get_signing_key_from_jwt",
        lambda self, tok: type("K", (), {"key": public_key})(),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            submit_resp = await client.post(
                "/api/v1/demo/query?queue_mode=async",
                json={"from": "orders", "select": ["orders.id"], "limit": 1},
                headers={"Authorization": f"Bearer {_token('agent-a')}"},
            )
            assert submit_resp.status_code == 202
            admission_id = submit_resp.json()["admission_id"]
            status_url = submit_resp.json()["status_url"]

            # Poll to a terminal state with the owner's own token first — a
            # terminal-record cancel is an idempotent no-op regardless of the
            # connection's allow_query_cancellation policy (unlike a RUNNING
            # query, which that separate gate can 403), so this isolates
            # exactly the property under test: does an authorized
            # (query:cancel-scoped) non-owner get 200 where an unscoped one
            # got 404, with no confound from the unrelated DB-permission gate.
            deadline = asyncio.get_event_loop().time() + 5.0
            while True:
                poll_resp = await client.get(
                    status_url, headers={"Authorization": f"Bearer {_token('agent-a')}"}
                )
                if poll_resp.json()["state"] in ("completed", "failed", "cancelled"):
                    break
                if asyncio.get_event_loop().time() > deadline:
                    raise AssertionError(f"never reached a terminal state: {poll_resp.json()}")
                await asyncio.sleep(0.01)

            other_status_resp = await client.get(
                status_url,
                headers={"Authorization": f"Bearer {_token('agent-b', scope='query:cancel')}"},
            )
            other_cancel_resp = await client.post(
                f"/api/v1/demo/query/{admission_id}/cancel",
                headers={"Authorization": f"Bearer {_token('agent-b', scope='query:cancel')}"},
            )

    assert other_status_resp.status_code == 200
    assert other_cancel_resp.status_code == 200
