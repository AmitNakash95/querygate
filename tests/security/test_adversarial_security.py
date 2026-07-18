"""Adversarial regression tests for QueryGate's security boundaries.

These tests model hostile or confused callers rather than normal product
usage. They intentionally combine valid AST fields in ways that could bypass
policy, pull undeclared tables into a statement, leak backend errors, or evade
response limits if validation order regresses.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient

import hvac.exceptions

from querygate.api.app import create_app
from querygate.catalog.loader import CatalogStore, set_catalog_store
from querygate.compiler.sqlalchemy_compiler import compile_structured_query
from querygate.connections.registry import get_registry
from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.exceptions import (
    PUBLIC_INTERNAL_ERROR,
    PolicyViolationError,
    QueryValidationError,
)
from querygate.execution import service as svc
from querygate.execution.service import StructuredQueryService, _cap_response_bytes
from querygate.mcp.exceptions import _error_code_from_exception
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import MandatoryRowFilter, Policy
from querygate.secrets.resolvers import VaultSecretResolver
from querygate.query_ast.models import (
    AggregateSelectItem,
    JoinSpec,
    OrderBySpec,
    Predicate,
    StructuredQuery,
    TopNSpec,
)
from querygate.validation import schema_validation as sv
from querygate.validation.policy_validation import validate_policy

pytestmark = pytest.mark.security


def _customers_table() -> sa.Table:
    return sa.Table(
        "customers",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(200)),
        sa.Column("tenant_id", sa.String(50)),
    )


@pytest.mark.parametrize(
    "query",
    [
        StructuredQuery(
            from_table="customers",
            select=["customers.id"],
            where=Predicate(col="customers.email", op="eq", value="target@example.com"),
        ),
        StructuredQuery(
            from_table="customers",
            select=["customers.id"],
            group_by=["customers.email"],
        ),
        StructuredQuery(
            from_table="customers",
            select=[AggregateSelectItem(fn="count", col="*", alias="n")],
            having=[Predicate(col="customers.email", op="eq", value="target@example.com")],
        ),
        StructuredQuery(
            from_table="customers",
            select=["customers.id"],
            order_by=[OrderBySpec(col="customers.email", dir="asc")],
        ),
        StructuredQuery(
            from_table="customers",
            select=["customers.id"],
            top_n=TopNSpec(
                partition_by=["customers.email"],
                order_by=[OrderBySpec(col="customers.id", dir="asc")],
                n=1,
            ),
        ),
        StructuredQuery(
            from_table="customers",
            select=["customers.id"],
            top_n=TopNSpec(
                order_by=[OrderBySpec(col="customers.email", dir="asc")],
                n=1,
            ),
        ),
        StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[
                JoinSpec(
                    table="customers",
                    on=["orders.customer_id", "customers.email"],
                )
            ],
        ),
    ],
    ids=["where", "group_by", "having", "order_by", "partition_by", "top_n", "join"],
)
def test_denied_column_cannot_be_used_for_inference(query: StructuredQuery):
    policy = Policy(denied_columns={"customers": ["email"]})

    with pytest.raises(PolicyViolationError, match="not accessible"):
        validate_policy(query, policy, connection_id="demo")


def test_denied_table_cannot_be_smuggled_through_a_filter():
    query = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        where=Predicate(col="employees.ssn", op="eq", value="000-00-0000"),
    )

    with pytest.raises(PolicyViolationError, match="Table 'employees'.*not accessible"):
        validate_policy(query, Policy(denied_tables=["employees"]), connection_id="demo")


@pytest.mark.asyncio
async def test_undeclared_table_reference_is_rejected_before_reflection(monkeypatch):
    load_table = AsyncMock(side_effect=AssertionError("must reject before schema reflection"))
    monkeypatch.setattr(sv, "_load_table", load_table)
    query = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        where=Predicate(col="orders.status", op="eq", value="completed"),
    )

    with pytest.raises(ValueError, match="undeclared.*orders"):
        await sv.validate_schema(query, connection_id="demo")
    load_table.assert_not_awaited()


def test_predicate_payload_is_bound_data_not_executable_sql():
    attack = "x' OR 1=1; DROP TABLE customers; --"
    query = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        where=Predicate(col="customers.email", op="eq", value=attack),
    )
    stmt, _limit = compile_structured_query(
        query,
        {"customers": _customers_table()},
        Policy(),
        dialect="postgresql",
    )
    compiled = stmt.compile()

    assert attack not in str(compiled)
    assert attack in compiled.params.values()


def test_principal_policy_cannot_bleed_between_callers():
    store = PolicyStore.from_dict(
        {
            "default": {},
            "principals": {
                "restricted-agent": {"demo": {"denied_columns": {"customers": ["email"]}}}
            },
        }
    )
    query = StructuredQuery(from_table="customers", select=["customers.email"])

    with pytest.raises(PolicyViolationError):
        validate_policy(
            query,
            store.get("demo", principal=Principal(subject="restricted-agent")),
            connection_id="demo",
        )
    validate_policy(
        query,
        store.get("demo", principal=Principal(subject="standard-agent")),
        connection_id="demo",
    )


@pytest.mark.asyncio
async def test_catalog_relationship_hint_cannot_disclose_a_denied_table():
    """A curated catalog relationship (querygate/catalog/) is admin-authored
    display metadata, not a policy decision — it must not let a
    principal-restricted caller learn that a table they cannot see or query
    exists, the same non-enumeration guarantee connection/table/column
    discovery already holds.
    """
    set_catalog_store(
        CatalogStore.from_dict(
            {
                "connections": {
                    "demo": {
                        "tables": {
                            "orders": {
                                "relationships": [
                                    {
                                        "to_table": "customers",
                                        "column": "customer_id",
                                        "to_column": "id",
                                        "description": "The customer who placed this order.",
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        )
    )
    set_policy_store(
        PolicyStore.from_dict(
            {
                "default": {},
                "principals": {"restricted-agent": {"demo": {"denied_tables": ["customers"]}}},
            }
        )
    )
    orders_table = sa.Table("orders", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True))
    service = StructuredQueryService(
        connection_id="demo", principal=Principal(subject="restricted-agent")
    )

    with (
        patch.object(svc, "get_engine", return_value=MagicMock()),
        patch.object(svc, "get_table_schema", AsyncMock(return_value=orders_table)),
    ):
        description = await service.describe_table("orders")

    assert description.catalog.relationships == []
    assert "customers" not in json.dumps(description.model_dump())


def test_vault_resolver_error_never_leaks_token_or_backend_response_text():
    """A Vault failure (bad token, revoked lease, network blip) must surface
    as a clear operator-facing failure without ever echoing the configured
    Vault token or Vault's own response text — Vault error bodies can
    contain request/path details a deployment wouldn't want captured in a
    log line or pasted into a support ticket.
    """
    vault_token = "hvs.CAESISUPERSECRETTOKENVALUE"
    fake_client = MagicMock()
    fake_client.secrets.kv.v2.read_secret_version.side_effect = hvac.exceptions.Forbidden(
        f"permission denied for token {vault_token} on path querygate/prod-db"
    )
    resolver = VaultSecretResolver(
        url="http://vault.internal", token=vault_token, client=fake_client
    )

    with pytest.raises(ValueError) as exc_info:
        resolver.resolve("querygate/prod-db#connection_string")

    message = str(exc_info.value)
    assert vault_token not in message
    assert "permission denied" not in message


@pytest.mark.asyncio
async def test_vault_resolved_secret_never_appears_in_connection_listing_or_errors(tmp_path):
    """The resolved value of a `${vault:...}`-backed connection string is as
    sensitive as an env-resolved one — REST connection listing and error
    responses must never echo it, matching the existing guarantee
    `test_credential_redaction.py` asserts for env-backed connections.
    """
    marker = "vault-resolved-super-secret-password-marker"
    connections_file = tmp_path / "connections.yaml"
    connections_file.write_text(
        """
connections:
  - id: vault-demo
    dialect: postgresql
    connection_string: ${vault:querygate/demo#connection_string}
"""
    )
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("default:\n  enabled: true\n")

    settings = AppConfig(
        environment="localhost",
        mcp_enabled=False,
        audit_sink_backend="none",
        connections_file=str(connections_file),
        policy_file=str(policy_file),
        api_keys=["admin-key"],
        api_key_scopes=["admin:reload-config"],
        vault_enabled=True,
        vault_addr="http://vault.internal:8200",
        vault_token="test-vault-token",
    )
    fake_client = MagicMock()
    fake_client.secrets.kv.v2.read_secret_version.return_value = {
        "data": {"data": {"connection_string": f"postgresql+asyncpg://{marker}@host/db"}}
    }
    with patch("hvac.Client", return_value=fake_client):
        app = create_app(settings)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            # Load the vault-backed connection via the real admin reload
            # path, exactly as an operator would — nothing in this test
            # reaches into internal state to seed the registry directly.
            reload_resp = await client.post(
                "/api/v1/admin/reload-config", headers={"Authorization": "Bearer admin-key"}
            )
            assert reload_resp.status_code == 200
            list_resp = await client.get(
                "/api/v1/connections", headers={"Authorization": "Bearer admin-key"}
            )
            missing_table_resp = await client.get(
                "/api/v1/vault-demo/tables/does-not-exist",
                headers={"Authorization": "Bearer admin-key"},
            )

    assert marker not in reload_resp.text
    assert marker not in list_resp.text
    assert marker not in missing_table_resp.text
    assert get_registry().get("vault-demo").connection_string == (
        f"postgresql+asyncpg://{marker}@host/db"
    )


def test_aggregate_queries_retain_principal_mandatory_row_filter():
    query = StructuredQuery(
        from_table="customers",
        select=[AggregateSelectItem(fn="count", col="*", alias="n")],
    )
    policy = Policy(
        mandatory_row_filters=[
            MandatoryRowFilter(table="customers", column="tenant_id", from_claim="tenant_id")
        ]
    )
    table = _customers_table()

    statements = []
    for tenant in ("tenant-a", "tenant-b"):
        stmt, _limit = compile_structured_query(
            query,
            {"customers": table},
            policy,
            dialect="postgresql",
            principal=Principal(subject=tenant, claims={"tenant_id": tenant}),
        )
        statements.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))

    assert "tenant-a" in statements[0] and "tenant-b" not in statements[0]
    assert "tenant-b" in statements[1] and "tenant-a" not in statements[1]


def test_single_oversized_row_cannot_bypass_response_cap():
    rows = [{"secret_blob": "x" * 100_000}]

    kept, truncated = _cap_response_bytes(rows, max_bytes=1024)

    assert kept == []
    assert truncated is True
    assert len(json.dumps(kept).encode("utf-8")) <= 1024


@pytest.mark.asyncio
async def test_rest_masks_unexpected_database_error_details():
    app = create_app(
        AppConfig(
            environment="localhost",
            mcp_enabled=False,
            audit_sink_backend="none",
        )
    )
    secret = "password=do-not-return; SQL=SELECT private_data"
    with patch(
        "querygate.api.routes.StructuredQueryService.execute",
        new_callable=AsyncMock,
        side_effect=ValueError(secret),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            response = await client.post(
                "/api/v1/demo/query",
                json={"from": "customers", "select": ["customers.id"]},
            )

    assert response.status_code == 500
    assert response.json()["detail"] == PUBLIC_INTERNAL_ERROR
    assert secret not in response.text


@pytest.mark.asyncio
async def test_batch_and_mcp_mask_unexpected_error_details():
    query = StructuredQuery(from_table="customers", select=["customers.id"])
    service = StructuredQueryService(connection_id="demo")
    secret = "db.internal:5432 password=do-not-return"
    with patch.object(service, "execute", AsyncMock(side_effect=RuntimeError(secret))):
        result = (await service.execute_many([query]))[0]

    code, message = _error_code_from_exception(RuntimeError(secret))
    assert result.error == PUBLIC_INTERNAL_ERROR
    assert code == "INTERNAL"
    assert message == PUBLIC_INTERNAL_ERROR
    assert secret not in f"{result.error} {message}"


def test_only_explicit_query_validation_errors_are_public():
    secret = "driver ValueError containing password=do-not-return"

    internal_code, internal_message = _error_code_from_exception(ValueError(secret))
    validation_code, validation_message = _error_code_from_exception(
        QueryValidationError("unknown column supplied by caller")
    )

    assert (internal_code, internal_message) == ("INTERNAL", PUBLIC_INTERNAL_ERROR)
    assert (validation_code, validation_message) == (
        "VALIDATION",
        "unknown column supplied by caller",
    )


@pytest.mark.asyncio
async def test_mcp_rejects_unapproved_host_header():
    from querygate.mcp.server import mcp_server

    mcp_server._session_manager = None  # type: ignore[assignment]
    app = create_app(
        AppConfig(
            environment="localhost",
            mcp_enabled=True,
            mcp_api_keys=[],
            audit_sink_backend="none",
        )
    )
    headers = {
        "Host": "attacker.example",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client,
    ):
        response = await client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers=headers,
        )

    assert response.status_code == 421
