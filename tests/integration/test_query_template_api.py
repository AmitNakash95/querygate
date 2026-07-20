"""Integration tests for the query-template REST surface (item 48), exercising
the real validate → policy → schema → compile → execute pipeline via SQLite."""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from examples.demo_db.schema import ORDERS_DATA
from querygate.audit.sinks import JsonlAuditSink, reset_audit_sink, set_audit_sink
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import MandatoryRowFilter, Policy
from querygate.templates.loader import TemplateStore, set_template_store

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"


def _install_templates() -> None:
    set_template_store(
        TemplateStore.from_dict(
            {
                "templates": [
                    {
                        "id": "orders_by_status",
                        "connection": "demo",
                        "description": "Orders in a given status.",
                        "parameters": [
                            {"name": "status", "type": "string", "required": True},
                            {
                                "name": "limit",
                                "type": "integer",
                                "required": False,
                                "default": 100,
                                "min": 1,
                                "max": 500,
                            },
                        ],
                        "query": {
                            "from": "orders",
                            "select": ["orders.id", "orders.status"],
                            "where": {
                                "col": "orders.status",
                                "op": "eq",
                                "value": {"param": "status"},
                            },
                            "limit": {"param": "limit"},
                        },
                    },
                    # A template whose connection does not exist — must be
                    # invisible and non-invocable (non-enumerating 404).
                    {
                        "id": "hidden_conn_template",
                        "connection": "nonexistent",
                        "query": {"from": "orders", "select": ["orders.id"]},
                    },
                ]
            }
        )
    )


@pytest.mark.asyncio
async def test_list_filters_to_visible_connections(sqlite_app):
    _install_templates()
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.get("/api/v1/query-templates")
    assert resp.status_code == 200
    ids = {t["id"] for t in resp.json()}
    assert "orders_by_status" in ids
    # A template on a connection the caller can't see is not listed.
    assert "hidden_conn_template" not in ids
    # The public projection is the callable signature, not the query skeleton.
    body = {t["id"]: t for t in resp.json()}
    assert "query" not in body["orders_by_status"]
    assert [p["name"] for p in body["orders_by_status"]["parameters"]] == ["status", "limit"]


@pytest.mark.asyncio
async def test_run_executes_through_the_real_pipeline(sqlite_app):
    _install_templates()
    completed = [o for o in ORDERS_DATA if o["status"] == "completed"]
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/query-templates/orders_by_status/run",
            json={"parameters": {"status": "completed"}},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["row_count"] == len(completed)
    assert all(row["status"] == "completed" for row in body["rows"])
    # Admission headers are present, same as an ad-hoc query.
    assert "X-QueryGate-Admission-Id" in resp.headers


@pytest.mark.asyncio
async def test_missing_required_parameter_is_422(sqlite_app):
    _install_templates()
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/query-templates/orders_by_status/run", json={"parameters": {}}
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_out_of_range_parameter_is_422(sqlite_app):
    _install_templates()
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/query-templates/orders_by_status/run",
            json={"parameters": {"status": "completed", "limit": 99999}},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_unknown_and_hidden_templates_are_uniform_404(sqlite_app):
    _install_templates()
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        unknown = await client.post(
            "/api/v1/query-templates/does_not_exist/run", json={"parameters": {}}
        )
        hidden = await client.post(
            "/api/v1/query-templates/hidden_conn_template/run", json={"parameters": {}}
        )
    assert unknown.status_code == 404
    assert hidden.status_code == 404
    # Non-enumerating: a hidden template doesn't reveal its connection name.
    assert "nonexistent" not in hidden.text


@pytest.mark.asyncio
@pytest.mark.security
async def test_mandatory_row_filter_applies_to_a_template(sqlite_app):
    """A template runs through the identical pipeline, so a policy
    mandatory_row_filter (tenant isolation) is AND-ed into the bound query — a
    template selecting all orders returns only the filtered rows, and cannot be
    a way around the filter."""
    set_template_store(
        TemplateStore.from_dict(
            {
                "templates": [
                    {
                        "id": "all_orders",
                        "connection": "demo",
                        "query": {"from": "orders", "select": ["orders.id", "orders.status"]},
                    }
                ]
            }
        )
    )
    # Every query touching orders is scoped to status='completed'.
    set_policy_store(
        PolicyStore(
            default=Policy(
                mandatory_row_filters=[
                    MandatoryRowFilter(table="orders", column="status", value="completed")
                ]
            ),
            overrides={},
        )
    )
    completed = [o for o in ORDERS_DATA if o["status"] == "completed"]
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post("/api/v1/query-templates/all_orders/run", json={"parameters": {}})
    assert resp.status_code == 200
    body = resp.json()
    # The filter narrowed the result even though the template has no WHERE.
    assert body["row_count"] == len(completed)
    assert all(row["status"] == "completed" for row in body["rows"])


@pytest.mark.asyncio
async def test_audit_records_template_id_and_param_names_never_values(sqlite_app, tmp_path):
    _install_templates()
    audit_path = tmp_path / "template-audit.jsonl"
    set_audit_sink(JsonlAuditSink(str(audit_path)))
    try:
        # A distinctive value that matches no real field, so any appearance in
        # the persisted event would be a genuine parameter-value leak.
        marker = "zzz-sensitive-status-marker"
        async with AsyncClient(
            transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL
        ) as client:
            resp = await client.post(
                "/api/v1/query-templates/orders_by_status/run",
                json={"parameters": {"status": marker}},
            )
        assert resp.status_code == 200
        event = json.loads(audit_path.read_text().splitlines()[-1])
        assert event["operation"] == "run_query_template"
        assert event["template_id"] == "orders_by_status"
        assert event["template_param_shape"] == ["status"]
        # The parameter *value* must never appear in the persisted event.
        assert marker not in audit_path.read_text()
    finally:
        reset_audit_sink()
