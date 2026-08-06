"""Integration tests for the REST API surface."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from querygate.api.app import create_app
from querygate.catalog.loader import CatalogStore, set_catalog_store
from querygate.catalog.retrieval import CatalogCitation
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, get_registry, set_registry
from querygate.core.config import AppConfig
from querygate.core.exceptions import (
    CapacityTimeoutError,
    ConfigValidationError,
    QueryValidationError,
    QueueFullError,
)
from querygate.execution.service import (
    BatchQueryItemResult,
    ColumnCatalogInfo,
    ColumnInfo,
    RelationshipCatalogInfo,
    StructuredQueryResult,
    TableCatalogInfo,
    TableDescription,
)
from querygate.policy.loader import PolicyStore, set_policy_store

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"
_SERVICE = "querygate.api.routes.StructuredQueryService"


def _catalog_citation() -> CatalogCitation:
    return CatalogCitation(
        entry_id="urn:querygate:catalog:test",
        source_class="verified",
        source_evidence=[
            {
                "kind": "manual",
                "reference_fingerprint": "sha256:" + ("0" * 64),
            }
        ],
        status="verified",
        confidence=1.0,
        precedence=400,
        catalog_version=2,
        schema_fingerprint="untracked",
        freshness="untracked",
    )


def _settings(**overrides) -> AppConfig:
    base = dict(
        environment="localhost",
        mcp_enabled=False,
        audit_sink_backend="none",
    )
    base.update(overrides)
    return AppConfig(**base)


@pytest.fixture
def app():
    return create_app(_settings())


@pytest.mark.asyncio
async def test_list_connections(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get("/api/v1/connections")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["id"] == "demo"
    assert "connection_string" not in body[0]


@pytest.mark.asyncio
async def test_list_connections_and_direct_access_are_principal_scoped(app):
    set_registry(
        ConnectionRegistry(
            {
                connection_id: ConnectionProfile(
                    id=connection_id,
                    dialect="postgresql",
                    connection_string=f"postgresql+asyncpg://user:pass@localhost/{connection_id}",
                    known_tables=["customers"],
                )
                for connection_id in ("demo", "internal_finance")
            }
        )
    )
    # Local development resolves to subject "anonymous-dev". A disabled
    # default plus explicit grants is the strict deny-by-default setup.
    set_policy_store(
        PolicyStore.from_dict(
            {
                "default": {"enabled": False},
                "principals": {"anonymous-dev": {"demo": {"enabled": True}}},
            }
        )
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        listed = await client.get("/api/v1/connections")
        hidden_schema = await client.get("/api/v1/internal_finance/tables")

    assert [connection["id"] for connection in listed.json()] == ["demo"]
    assert hidden_schema.status_code == 404
    assert hidden_schema.json()["detail"] == "Unknown connection: 'internal_finance'"


@pytest.mark.asyncio
async def test_query_success(app):
    mock_result = StructuredQueryResult(
        rows=[{"id": 1, "name": "Ada"}], row_count=1, truncated=False, limit=50, offset=0
    )
    with patch(f"{_SERVICE}.execute", new_callable=AsyncMock, return_value=mock_result):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(
                "/api/v1/demo/query",
                json={
                    "from": "customers",
                    "select": ["customers.id", "customers.name"],
                    "limit": 50,
                },
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["row_count"] == 1
    assert body["rows"][0]["name"] == "Ada"


@pytest.mark.asyncio
async def test_query_success_includes_admission_headers(app):
    mock_result = StructuredQueryResult(
        rows=[{"id": 1, "name": "Ada"}],
        row_count=1,
        truncated=False,
        limit=50,
        offset=0,
        admission_id="admission-123",
        queue_wait_ms=7,
    )
    with patch(f"{_SERVICE}.execute", new_callable=AsyncMock, return_value=mock_result):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(
                "/api/v1/demo/query",
                json={"from": "customers", "select": ["customers.id"], "limit": 50},
            )
    assert resp.status_code == 200
    assert resp.headers["X-QueryGate-Admission-Id"] == "admission-123"
    assert resp.headers["X-QueryGate-Admission-State"] == "completed"
    assert resp.headers["X-QueryGate-Queue-Wait-Ms"] == "7"
    body = resp.json()
    assert body["admission_id"] == "admission-123"
    assert body["queue_wait_ms"] == 7


@pytest.mark.asyncio
async def test_query_capacity_timeout_is_429_with_admission_headers(app):
    """TODO.md item 35 phase 3 (2026-07-28): capacity/queue rejections
    migrated from 422 to 429 + Retry-After, matching item 50's quota
    precedent — a deliberate, documented breaking change (CHANGELOG.md), not
    an accident. The response BODY string contract (docs/LOAD_TESTING.md)
    still holds; only the status code and the added Retry-After header
    changed."""
    exc = CapacityTimeoutError(
        "too many concurrent 'demo' queries in flight, try again shortly",
        admission_id="admission-456",
        queue_wait_ms=42,
        retry_after_seconds=10,
    )
    with patch(f"{_SERVICE}.execute", new_callable=AsyncMock, side_effect=exc):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(
                "/api/v1/demo/query",
                json={"from": "customers", "select": ["customers.id"], "limit": 50},
                params={"queue_mode": "fail_fast"},
            )
    assert resp.status_code == 429
    # Existing body contract (docs/LOAD_TESTING.md) must not change.
    assert "too many concurrent" in resp.json()["detail"]
    assert resp.headers["X-QueryGate-Admission-Id"] == "admission-456"
    assert resp.headers["X-QueryGate-Admission-State"] == "capacity_timeout"
    assert resp.headers["X-QueryGate-Queue-Wait-Ms"] == "42"
    assert resp.headers["Retry-After"] == "10"


@pytest.mark.asyncio
async def test_query_queue_full_is_429_with_queue_full_admission_state(app):
    exc = QueueFullError(
        "connection 'demo' queue is already at its configured depth, try again shortly",
        admission_id="admission-789",
        retry_after_seconds=10,
    )
    with patch(f"{_SERVICE}.execute", new_callable=AsyncMock, side_effect=exc):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(
                "/api/v1/demo/query",
                json={"from": "customers", "select": ["customers.id"], "limit": 50},
            )
    assert resp.status_code == 429
    # Existing body contract (docs/LOAD_TESTING.md) must not change.
    assert "queue" in resp.json()["detail"]
    assert resp.headers["X-QueryGate-Admission-Id"] == "admission-789"
    assert resp.headers["Retry-After"] == "10"
    assert resp.headers["X-QueryGate-Admission-State"] == "queue_full"
    assert resp.headers["X-QueryGate-Queue-Wait-Ms"] == "0"


@pytest.mark.asyncio
async def test_query_passes_queue_mode_and_wait_timeout_to_service(app):
    mock_result = StructuredQueryResult(rows=[], row_count=0, truncated=False, limit=50, offset=0)
    with patch(
        f"{_SERVICE}.execute", new_callable=AsyncMock, return_value=mock_result
    ) as mock_execute:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(
                "/api/v1/demo/query",
                json={"from": "customers", "select": ["customers.id"], "limit": 50},
                params={"queue_mode": "wait", "wait_timeout_seconds": "2.5"},
            )
    assert resp.status_code == 200
    _query_arg, kwargs = mock_execute.call_args
    assert kwargs["queue_mode"] == "wait"
    assert kwargs["wait_timeout_seconds"] == 2.5


@pytest.mark.asyncio
async def test_query_rejects_invalid_queue_mode(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/demo/query",
            json={"from": "customers", "select": ["customers.id"], "limit": 50},
            params={"queue_mode": "not_a_real_mode"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_query_unknown_connection_is_404(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/nonexistent/query", json={"from": "customers", "select": ["customers.id"]}
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_query_validation_error_is_422(app):
    with patch(
        f"{_SERVICE}.execute",
        new_callable=AsyncMock,
        side_effect=QueryValidationError("Column 'x' not found"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(
                "/api/v1/demo/query", json={"from": "customers", "select": ["customers.x"]}
            )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_query_against_a_not_yet_connectable_dialect_is_422_not_500(app):
    """TODO.md item 19 phase 2 — 2026-08-06 `security-invariant-reviewer`
    finding: `connections/engine.py`'s Snowflake guard used to raise a bare
    `ValueError`, which is NOT in `api/_errors.py`'s `_ACTIONABLE` tuple, so
    `mask_unexpected()` was silently converting the guard's explained
    rejection into an opaque REST 500 — defeating the guard's whole point.
    It now raises `ConfigValidationError`, which has its own dedicated
    `@app.exception_handler` mapping to 422 with the message intact
    (`api/_errors.py`). This proves that mapping holds through the real
    ASGI app for the exact exception `init_engine`'s guard raises, not just
    that the exception TYPE is correct in isolation
    (`tests/unit/test_connections_engine.py` proves that half)."""
    with patch(
        f"{_SERVICE}.execute",
        new_callable=AsyncMock,
        side_effect=ConfigValidationError(
            "Connection 'demo' is dialect 'snowflake', which QueryGate cannot yet open a "
            "live connection for"
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(
                "/api/v1/demo/query", json={"from": "customers", "select": ["customers.id"]}
            )
    assert resp.status_code == 422
    assert "cannot yet open a live connection" in resp.text
    from querygate.core.exceptions import PUBLIC_INTERNAL_ERROR

    assert PUBLIC_INTERNAL_ERROR not in resp.text


@pytest.mark.asyncio
async def test_query_verdict_allowed(app):
    from querygate.execution.service import VerdictResult

    with patch(
        f"{_SERVICE}.verdict",
        new_callable=AsyncMock,
        return_value=VerdictResult(allowed=True),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(
                "/api/v1/demo/query/verdict",
                json={"from": "customers", "select": ["customers.id"], "limit": 5},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["allowed"] is True
    assert body["reason"] is None
    assert body["plan"] is None


@pytest.mark.asyncio
async def test_query_verdict_denied(app):
    from querygate.execution.service import VerdictResult

    with patch(
        f"{_SERVICE}.verdict",
        new_callable=AsyncMock,
        return_value=VerdictResult(
            allowed=False,
            reason="not-available-to-you",
            message="This query is not available to you under your effective policy.",
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(
                "/api/v1/demo/query/verdict",
                json={"from": "customers", "select": ["customers.id"], "limit": 5},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["allowed"] is False
    assert body["reason"] == "not-available-to-you"
    assert body["plan"] is None


@pytest.mark.asyncio
async def test_query_verdict_unknown_connection_is_404(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/nonexistent/query/verdict",
            json={"from": "customers", "select": ["customers.id"]},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_no_raw_sql_field_accepted(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post("/api/v1/demo/query", json={"sql": "SELECT * FROM customers"})
    # StructuredQuery has no `sql` field and forbids unknown fields — this
    # never reaches the database, it fails FastAPI's own body validation.
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_tables(app):
    with patch(
        f"{_SERVICE}.list_tables", new_callable=AsyncMock, return_value=["customers", "orders"]
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.get("/api/v1/demo/tables")
    assert resp.status_code == 200
    assert resp.json()["tables"] == ["customers", "orders"]


@pytest.mark.asyncio
async def test_describe_table(app):
    desc = TableDescription(
        name="customers", columns=[ColumnInfo(name="id", type="INTEGER", nullable=False)]
    )
    with patch(f"{_SERVICE}.describe_table", new_callable=AsyncMock, return_value=desc):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.get("/api/v1/demo/tables/customers")
    assert resp.status_code == 200
    assert resp.json()["name"] == "customers"


@pytest.mark.asyncio
async def test_describe_table_includes_catalog_metadata_when_configured(app):
    desc = TableDescription(
        name="customers",
        columns=[
            ColumnInfo(
                name="email",
                type="VARCHAR",
                nullable=True,
                catalog=ColumnCatalogInfo(
                    description="Customer email",
                    sensitivity="pii",
                    provenance=_catalog_citation(),
                ),
            )
        ],
        catalog=TableCatalogInfo(
            description="One row per customer.",
            relationships=[
                RelationshipCatalogInfo(
                    to_table="orders",
                    column="id",
                    to_column="customer_id",
                    provenance=_catalog_citation(),
                )
            ],
            provenance=_catalog_citation(),
        ),
    )
    with patch(f"{_SERVICE}.describe_table", new_callable=AsyncMock, return_value=desc):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.get("/api/v1/demo/tables/customers")
    assert resp.status_code == 200
    body = resp.json()
    assert body["catalog"]["description"] == "One row per customer."
    assert body["catalog"]["relationships"][0]["to_table"] == "orders"
    assert body["columns"][0]["catalog"]["sensitivity"] == "pii"


@pytest.mark.asyncio
async def test_describe_table_catalog_is_null_when_not_configured(app):
    desc = TableDescription(
        name="customers", columns=[ColumnInfo(name="id", type="INTEGER", nullable=False)]
    )
    with patch(f"{_SERVICE}.describe_table", new_callable=AsyncMock, return_value=desc):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.get("/api/v1/demo/tables/customers")
    assert resp.status_code == 200
    body = resp.json()
    assert body["catalog"] is None
    assert body["columns"][0]["catalog"] is None


@pytest.mark.asyncio
async def test_catalog_search_returns_policy_filtered_provenance_without_database_access(app):
    set_catalog_store(
        CatalogStore.from_dict(
            {
                "version": 2,
                "connections": {
                    "demo": {
                        "tables": {
                            "orders": {
                                "description": "Customer purchases and revenue.",
                                "aliases": ["sales"],
                            },
                            "internal_finance": {
                                "description": "Restricted profit planning.",
                            },
                        }
                    }
                },
            }
        )
    )
    set_policy_store(PolicyStore.from_dict({"default": {"denied_tables": ["internal_finance"]}}))

    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        visible = await client.get(
            "/api/v1/demo/catalog/search",
            params={"q": "sales revenue", "limit": 3, "verbose_provenance": True},
        )
        hidden = await client.get("/api/v1/demo/catalog/search", params={"q": "profit planning"})

    assert visible.status_code == 200
    assert visible.json()["results"][0]["table"] == "orders"
    citation = visible.json()["results"][0]["citation"]
    assert citation["entry_id"].startswith("urn:querygate:catalog:")
    assert citation["status"] == "verified"
    assert citation["freshness"] == "untracked"
    assert hidden.status_code == 200
    assert hidden.json()["result_count"] == 0
    assert hidden.json()["results"] == []


@pytest.mark.asyncio
async def test_batch_query(app):
    results = [
        BatchQueryItemResult(rows=[{"id": 1}], row_count=1, truncated=False, limit=5, offset=0)
    ]
    with patch(f"{_SERVICE}.execute_many", new_callable=AsyncMock, return_value=results):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(
                "/api/v1/demo/query/batch",
                json={"queries": [{"from": "customers", "select": ["customers.id"], "limit": 5}]},
            )
    assert resp.status_code == 200
    assert resp.json()["results"][0]["row_count"] == 1


@pytest.mark.asyncio
async def test_batch_query_passes_queue_mode_and_wait_timeout_to_service(app):
    results = [
        BatchQueryItemResult(rows=[{"id": 1}], row_count=1, truncated=False, limit=5, offset=0)
    ]
    with patch(
        f"{_SERVICE}.execute_many", new_callable=AsyncMock, return_value=results
    ) as mock_execute_many:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(
                "/api/v1/demo/query/batch",
                json={"queries": [{"from": "customers", "select": ["customers.id"], "limit": 5}]},
                params={"queue_mode": "fail_fast"},
            )
    assert resp.status_code == 200
    _queries_arg, kwargs = mock_execute_many.call_args
    assert kwargs["queue_mode"] == "fail_fast"
    assert kwargs["wait_timeout_seconds"] is None


@pytest.mark.asyncio
async def test_batch_query_rejects_async_queue_mode(app):
    """Audit fix (item 35 phase 3 re-audit): only POST .../query implements the
    202/poll/cancel async admission lifecycle. Without this rejection,
    queue_mode=async silently downgrades to synchronous execution here — no
    202, no admission_id, no signal the caller's requested mode wasn't
    honored (resolve_wait_seconds treats async identically to wait for the
    wait-ceiling calculation, so nothing else would have caught this)."""
    with patch(f"{_SERVICE}.execute_many", new_callable=AsyncMock) as mock_execute_many:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(
                "/api/v1/demo/query/batch",
                json={"queries": [{"from": "customers", "select": ["customers.id"], "limit": 5}]},
                params={"queue_mode": "async"},
            )
    assert resp.status_code == 422
    assert "only supported on POST /{connection}/query" in resp.json()["detail"]
    mock_execute_many.assert_not_called()


@pytest.mark.asyncio
async def test_batch_query_passes_approval_tokens_to_service(app):
    """The batch route threads per-query approval grants (item 92) through to
    execute_many, so an approval-gated query can run inside a batch."""
    results = [
        BatchQueryItemResult(rows=[{"id": 1}], row_count=1, truncated=False, limit=5, offset=0)
    ]
    with patch(
        f"{_SERVICE}.execute_many", new_callable=AsyncMock, return_value=results
    ) as mock_execute_many:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(
                "/api/v1/demo/query/batch",
                json={
                    "queries": [{"from": "customers", "select": ["customers.id"], "limit": 5}],
                    "approval_tokens": {"deadbeef": "signed-token"},
                },
            )
    assert resp.status_code == 200
    _queries_arg, kwargs = mock_execute_many.call_args
    assert kwargs["approval_tokens"] == {"deadbeef": "signed-token"}


@pytest.mark.asyncio
async def test_batch_query_uses_per_principal_max_batch_size(app):
    """Regression: batch-size validation used to read only the connection
    policy, so a tighter principal override could be bypassed before
    execute_many() ran.
    """
    set_policy_store(
        PolicyStore.from_dict(
            {
                "default": {"max_batch_size": 10},
                "principals": {"anonymous-dev": {"demo": {"max_batch_size": 1}}},
            }
        )
    )
    with patch(f"{_SERVICE}.execute_many", new_callable=AsyncMock) as execute_many:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(
                "/api/v1/demo/query/batch",
                json={
                    "queries": [
                        {"from": "customers", "select": ["customers.id"], "limit": 5},
                        {"from": "orders", "select": ["orders.id"], "limit": 5},
                    ]
                },
            )
    assert resp.status_code == 422
    assert "max of 1" in resp.json()["detail"]
    execute_many.assert_not_called()


@pytest.mark.asyncio
async def test_auth_required_outside_local_dev():
    settings = _settings(environment="staging", api_keys=["secret-key"])
    staged_app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=staged_app), base_url=_BASE_URL) as client:
        unauthorized = await client.get("/api/v1/connections")
        assert unauthorized.status_code == 401

        authorized = await client.get(
            "/api/v1/connections", headers={"Authorization": "Bearer secret-key"}
        )
        assert authorized.status_code == 200


@pytest.mark.asyncio
async def test_jwt_bearer_token_accepted_alongside_api_key():
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    settings = _settings(
        environment="staging",
        api_keys=["secret-key"],
        jwt_enabled=True,
        jwt_jwks_url="https://idp.example.com/.well-known/jwks.json",
        jwt_issuer="https://idp.example.com/",
        jwt_audience="querygate",
    )
    jwt_app = create_app(settings)

    token = jwt.encode(
        {"sub": "human-user", "iss": "https://idp.example.com/", "aud": "querygate"},
        private_key,
        algorithm="RS256",
    )

    with patch(
        "jwt.PyJWKClient.get_signing_key_from_jwt",
        lambda self, tok: type("K", (), {"key": public_key})(),
    ):
        async with AsyncClient(transport=ASGITransport(app=jwt_app), base_url=_BASE_URL) as client:
            jwt_resp = await client.get(
                "/api/v1/connections", headers={"Authorization": f"Bearer {token}"}
            )
            api_key_resp = await client.get(
                "/api/v1/connections", headers={"Authorization": "Bearer secret-key"}
            )
            rejected_resp = await client.get(
                "/api/v1/connections", headers={"Authorization": "Bearer garbage"}
            )
    assert jwt_resp.status_code == 200
    assert api_key_resp.status_code == 200
    assert rejected_resp.status_code == 401


@pytest.mark.asyncio
async def test_jwt_enabled_in_local_dev_still_rejects_invalid_tokens():
    """Regression test: enabling JWT auth in a local/dev environment (the
    default) with no api_keys configured must not silently fall back to the
    anonymous-dev bypass for an invalid/missing token — that would make
    configuring JWT auth in dev a no-op. Caught by live testing during
    development; see core/auth.py's AnonymousAuthenticator docstring.
    """
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    settings = _settings(  # environment defaults to "localhost" (is_local)
        jwt_enabled=True,
        jwt_jwks_url="https://idp.example.com/.well-known/jwks.json",
        jwt_issuer="https://idp.example.com/",
        jwt_audience="querygate",
    )
    jwt_app = create_app(settings)

    token_a = jwt.encode(
        {"sub": "agent-a", "iss": "https://idp.example.com/", "aud": "querygate"},
        private_key,
        algorithm="RS256",
    )

    with patch(
        "jwt.PyJWKClient.get_signing_key_from_jwt",
        lambda self, tok: type("K", (), {"key": public_key})(),
    ):
        async with AsyncClient(transport=ASGITransport(app=jwt_app), base_url=_BASE_URL) as client:
            no_token = await client.get("/api/v1/connections")
            garbage = await client.get(
                "/api/v1/connections", headers={"Authorization": "Bearer garbage"}
            )
            valid = await client.get(
                "/api/v1/connections", headers={"Authorization": f"Bearer {token_a}"}
            )
    assert no_token.status_code == 401
    assert garbage.status_code == 401
    assert valid.status_code == 200


@pytest.mark.asyncio
async def test_metrics_endpoint_requires_auth_by_default():
    """TODO.md item 144 / docs/THREAT_MODEL.md QG-36: metrics_require_auth
    defaults to true, so an unauthenticated caller — including the local/dev
    anonymous bypass, which grants no scopes — must not reach /metrics."""
    app = create_app(_settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get("/metrics")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_metrics_endpoint_requires_admin_metrics_read_scope():
    app = create_app(_settings(api_keys=["secret-key"], api_key_scopes=["admin:connections:read"]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        wrong_scope = await client.get("/metrics", headers={"Authorization": "Bearer secret-key"})
    assert wrong_scope.status_code == 403

    app = create_app(_settings(api_keys=["secret-key"], api_key_scopes=["admin:metrics:read"]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get("/metrics", headers={"Authorization": "Bearer secret-key"})
    assert resp.status_code == 200
    assert "querygate_queries_total" in resp.text
    assert "querygate_concurrency_in_use" in resp.text


@pytest.mark.asyncio
async def test_metrics_endpoint_reachable_unauthenticated_when_disabled():
    """metrics_require_auth=False is an explicit operator opt-out (e.g. a
    same-pod sidecar scrape path already restricts reachability)."""
    app = create_app(_settings(metrics_require_auth=False))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "querygate_queries_total" in resp.text


@pytest.mark.asyncio
async def test_health_endpoint(app):
    # /health reads app.state.health_monitor, populated by the app's
    # lifespan — ASGITransport doesn't trigger startup/shutdown on its own
    # (see test_mcp_server.py for the same pattern). health.ping is patched
    # so lifespan startup doesn't attempt a real network connection.
    with patch("querygate.health._ping", new_callable=AsyncMock):
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client,
        ):
            resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "querygate"
    assert body["status"] == "ok"
    assert body["connections"] == {"healthy": 1, "unhealthy": 0, "unknown": 0}
    assert "demo" not in resp.text


@pytest.mark.asyncio
async def test_health_endpoint_reports_degraded_when_connection_unreachable(app):
    with patch(
        "querygate.health._ping", new_callable=AsyncMock, side_effect=ConnectionError("refused")
    ):
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client,
        ):
            resp = await client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["connections"] == {"healthy": 0, "unhealthy": 1, "unknown": 0}
    assert "demo" not in resp.text
    assert "refused" not in resp.text


@pytest.mark.asyncio
async def test_worm_audit_backend_wires_the_flush_monitor_end_to_end(tmp_path):
    """TODO.md item 134: the piece the unit tests (test_audit_worm_sink.py)
    can't cover — that app.py's lifespan actually starts a WormFlushMonitor
    reaching the SAME buffer singleton configure_audit_sink()'s
    CompositeAuditSink writes into, not two independently-constructed
    buffers that happen to share a class."""
    import boto3
    from moto import mock_aws

    from querygate.audit.events import AuditEvent
    from querygate.audit.sinks import get_audit_sink

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="qg-worm-it-test", ObjectLockEnabledForBucket=True)

        settings = _settings(
            audit_sink_backend="jsonl_chained_s3_worm",
            audit_jsonl_path=str(tmp_path / "chain.jsonl"),
            audit_worm_s3_bucket="qg-worm-it-test",
            audit_worm_s3_region="us-east-1",
        )
        app = create_app(settings)
        with patch("querygate.health._ping", new_callable=AsyncMock):
            async with app.router.lifespan_context(app):
                assert app.state.worm_flush_monitor is not None
                assert app.state.worm_flush_monitor.is_running

                get_audit_sink().emit(
                    AuditEvent(
                        connection_id="demo",
                        policy_decision="allowed",
                        outcome="success",
                        query_shape={"from": "customers"},
                        duration_ms=1,
                    )
                )
                # Deterministic, rather than waiting out the real interval.
                await app.state.worm_flush_monitor.flush_once()

        listing = s3.list_objects_v2(Bucket="qg-worm-it-test")
        assert listing["KeyCount"] == 1
        body = s3.get_object(Bucket="qg-worm-it-test", Key=listing["Contents"][0]["Key"])[
            "Body"
        ].read()
        assert b'"connection_id":"demo"' in body

        # Local hash-chained ledger still got the same event — WORM composes,
        # it doesn't replace.
        assert (tmp_path / "chain.jsonl").exists()
        assert "demo" in (tmp_path / "chain.jsonl").read_text()


def _write_reload_config_files(tmp_path):
    connections_file = tmp_path / "connections.yaml"
    connections_file.write_text("""
connections:
  - id: reload-demo
    dialect: postgresql
    connection_string: ${TEST_RELOAD_DB_URL}
    known_tables: [foo]
""")
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("""
default:
  enabled: true
""")
    return str(connections_file), str(policy_file)


@pytest.mark.asyncio
async def test_reload_config_forbidden_without_scope(tmp_path):
    connections_file, policy_file = _write_reload_config_files(tmp_path)
    settings = _settings(
        api_keys=["secret-key"], connections_file=connections_file, policy_file=policy_file
    )
    reload_app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=reload_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/admin/reload-config", headers={"Authorization": "Bearer secret-key"}
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_reload_config_succeeds_with_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_RELOAD_DB_URL", "postgresql+asyncpg://user:pass@localhost/demo")
    connections_file, policy_file = _write_reload_config_files(tmp_path)
    settings = _settings(
        api_keys=["secret-key"],
        api_key_scopes=["admin:reload-config"],
        connections_file=connections_file,
        policy_file=policy_file,
    )
    reload_app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=reload_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/admin/reload-config", headers={"Authorization": "Bearer secret-key"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["connection_ids"] == ["reload-demo"]

    # The registry actually swapped — /api/v1/connections now sees it too.
    async with AsyncClient(transport=ASGITransport(app=reload_app), base_url=_BASE_URL) as client:
        listed = await client.get(
            "/api/v1/connections", headers={"Authorization": "Bearer secret-key"}
        )
    assert [c["id"] for c in listed.json()] == ["reload-demo"]


@pytest.mark.asyncio
async def test_reload_config_swaps_in_catalog_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_RELOAD_DB_URL", "postgresql+asyncpg://user:pass@localhost/demo")
    connections_file, policy_file = _write_reload_config_files(tmp_path)
    catalog_file = tmp_path / "catalog.yaml"
    catalog_file.write_text("""
connections:
  reload-demo:
    tables:
      foo:
        description: "Reloaded catalog entry."
""")
    settings = _settings(
        api_keys=["secret-key"],
        api_key_scopes=["admin:reload-config"],
        connections_file=connections_file,
        policy_file=policy_file,
        catalog_file=str(catalog_file),
    )
    reload_app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=reload_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/admin/reload-config", headers={"Authorization": "Bearer secret-key"}
        )
    assert resp.status_code == 200
    assert resp.json()["catalog_connection_ids"] == ["reload-demo"]


@pytest.mark.asyncio
async def test_reload_config_resolves_vault_backed_connection_string(tmp_path):
    connections_file = tmp_path / "connections.yaml"
    connections_file.write_text("""
connections:
  - id: vault-demo
    dialect: postgresql
    connection_string: ${vault:querygate/demo#connection_string}
    known_tables: [foo]
""")
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("default:\n  enabled: true\n")

    settings = _settings(
        api_keys=["secret-key"],
        api_key_scopes=["admin:reload-config"],
        connections_file=str(connections_file),
        policy_file=str(policy_file),
        vault_enabled=True,
        vault_addr="http://vault.internal:8200",
        vault_token="test-vault-token",
    )

    fake_client = MagicMock()
    fake_client.secrets.kv.v2.read_secret_version.return_value = {
        "data": {"data": {"connection_string": "postgresql+asyncpg://vault-resolved/db"}}
    }
    with patch("hvac.Client", return_value=fake_client) as mock_hvac_client:
        reload_app = create_app(settings)
        async with AsyncClient(
            transport=ASGITransport(app=reload_app), base_url=_BASE_URL
        ) as client:
            resp = await client.post(
                "/api/v1/admin/reload-config", headers={"Authorization": "Bearer secret-key"}
            )

    assert resp.status_code == 200
    assert resp.json()["connection_ids"] == ["vault-demo"]
    # The connection string actually resolved through Vault, not just that
    # reload reported success — and hvac.Client was built with the
    # deployment's own token/address, not left at some default.
    assert (
        get_registry().get("vault-demo").connection_string
        == "postgresql+asyncpg://vault-resolved/db"
    )
    mock_hvac_client.assert_called_once_with(
        url="http://vault.internal:8200", token="test-vault-token", namespace=None, timeout=10.0
    )
