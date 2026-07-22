"""Integration coverage for TODO item 31's browser control plane."""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from querygate.api.app import create_app
from querygate.audit.events import AuditEvent, ConfigChangeEvent, ConnectionProbeEvent
from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import MandatoryRowFilter, Policy

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"
_ADMIN_KEY = "ui-admin-key"


def _write_source_files(tmp_path):
    connections_file = tmp_path / "connections.yaml"
    connections_file.write_text("""
connections:
  - id: demo
    dialect: postgresql
    connection_string: ${ADMIN_UI_DB_URL}
    known_tables: [customers, orders]
""")
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("default:\n  enabled: true\n")
    return str(connections_file), str(policy_file)


def _settings(tmp_path, monkeypatch, *, scopes=None, audit_path=None):
    monkeypatch.setenv("ADMIN_UI_DB_URL", "postgresql+asyncpg://user:pass@localhost/demo")
    connections_file, policy_file = _write_source_files(tmp_path)
    return AppConfig(
        environment="localhost",
        mcp_enabled=False,
        audit_sink_backend="jsonl" if audit_path else "none",
        audit_jsonl_path=str(audit_path or tmp_path / "unused.jsonl"),
        connections_file=connections_file,
        policy_file=policy_file,
        api_keys=[_ADMIN_KEY],
        api_key_scopes=(
            scopes if scopes is not None else ["admin:config:read", "admin:config:write"]
        ),
    )


def _auth():
    return {"Authorization": f"Bearer {_ADMIN_KEY}"}


@pytest.mark.asyncio
async def test_admin_spa_is_served_with_browser_security_headers(tmp_path, monkeypatch):
    app = create_app(_settings(tmp_path, monkeypatch))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        response = await client.get("/admin/")
        script = await client.get("/admin/app.js")
        styles = await client.get("/admin/app.css")
        logo = await client.get("/admin/logo-wordmark.svg")
        favicon = await client.get("/admin/favicon.svg")

    assert response.status_code == 200
    assert "QueryGate Control Plane" in response.text
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    # The JS/CSS assets must be no-store too, not just the HTML shell — otherwise
    # a browser serves a stale control plane after a UI update.
    assert script.headers["cache-control"] == "no-store"
    assert styles.status_code == 200
    assert styles.headers["cache-control"] == "no-store"
    assert script.status_code == 200
    assert "policy/test" in script.text
    assert "/admin/config/simulate" in script.text
    # TODO.md item 85: domain-separated nav. Catalog is a top-level domain in
    # the sidebar; its inner tabs (Review proposals / Versions & rollback /
    # Curate) are rendered from the navModel in app.js, and the Catalog domain
    # is signalled as self-contained (its own governance, not the shared release).
    assert 'data-domain="catalog"' in response.text
    assert "Review proposals" in script.text
    assert "Versions & rollback" in script.text
    assert "Self-contained governance" in script.text
    assert "/admin/catalog/" in script.text
    assert "catalog:review" in script.text
    assert logo.status_code == 200
    assert "QueryGate</tspan>" in logo.text
    assert ">;</tspan>" in logo.text
    assert favicon.status_code == 200
    assert ">;</text>" in favicon.text
    # TODO.md item 46: safe-start policy templates panel.
    assert "Safe-start templates" in response.text
    assert "/admin/config/templates/render" in script.text
    # TODO.md item 48: read-only query-templates browse panel. Item 85 moved
    # the nav label into the Templates domain's inner tab (rendered from app.js).
    assert 'data-domain="templates"' in response.text
    assert "Query templates" in script.text
    assert "/query-templates" in script.text
    # TODO.md item 48 phase 2: templates.yaml is a governed config-editor document.
    assert 'data-document="templates"' in response.text
    assert '"templates"' in script.text
    # On-demand live-schema check for query templates.
    assert 'id="check-schema"' in response.text
    assert "/admin/config/check-template-schema" in script.text
    # TODO.md item 44 phase 2: read-only observability panel + honest snapshot wording.
    # Surfaced as its own domain in the item-85 two-level nav model.
    assert 'data-domain="observability"' in response.text
    assert "admin:observability:read" in response.text
    assert "not durable history" in response.text
    assert "/admin/observability/overview" in script.text
    # TODO.md item 47: portable change-set export/import + policy-only local recovery.
    assert 'id="export-change-set"' in response.text
    assert 'id="import-change-set"' in response.text
    assert "/admin/config/export" in script.text
    assert "/admin/config/import" in script.text


@pytest.mark.asyncio
async def test_template_authoring_parse_render_round_trip_and_validation(tmp_path, monkeypatch):
    """TODO.md item 87: the Templates domain composes a validated QueryTemplate
    into the draft templates.yaml via parse/render, the same pattern as the
    policy designer. A malformed slot is rejected as a clean 422 by the shared
    model authority, never silently accepted."""
    app = create_app(_settings(tmp_path, monkeypatch))
    templates_yaml = """
templates:
  - id: recent_orders
    connection: demo
    description: Recent orders
    parameters:
      - name: since
        type: string
        required: true
    query:
      from: orders
      select: [orders.id]
      where:
        col: orders.created_at
        op: gte
        value: {param: since}
"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        parsed = await client.post(
            "/api/v1/admin/ui/templates/parse",
            json={"templates_yaml": templates_yaml},
            headers=_auth(),
        )
        document = parsed.json()["document"]
        # Add a second template through the structured document, then render.
        document["templates"].append(
            {
                "id": "customer_count",
                "connection": "demo",
                "parameters": [],
                "query": {"from": "customers", "select": [{"fn": "count", "as": "n"}]},
            }
        )
        rendered = await client.post(
            "/api/v1/admin/ui/templates/render",
            json={"document": document},
            headers=_auth(),
        )
        # A slot whose numeric bound is set on a string type must be rejected.
        bad = await client.post(
            "/api/v1/admin/ui/templates/render",
            json={
                "document": {
                    "templates": [
                        {
                            "id": "bad_slot",
                            "connection": "demo",
                            "parameters": [{"name": "x", "type": "string", "min": 3}],
                            "query": {"from": "customers", "select": ["customers.id"]},
                        }
                    ]
                }
            },
            headers=_auth(),
        )

    assert parsed.status_code == 200
    assert [t["id"] for t in document["templates"]] == ["recent_orders", "customer_count"]
    assert rendered.status_code == 200
    assert "id: customer_count" in rendered.json()["templates_yaml"]
    assert "id: recent_orders" in rendered.json()["templates_yaml"]
    assert bad.status_code == 422
    assert "min/max only valid for numeric types" in bad.json()["detail"]


@pytest.mark.asyncio
async def test_template_authoring_render_requires_write_scope(tmp_path, monkeypatch):
    """Composing into the draft is a write action; parse is read-or-write."""
    app = create_app(_settings(tmp_path, monkeypatch, scopes=["admin:config:read"]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        parse = await client.post(
            "/api/v1/admin/ui/templates/parse",
            json={"templates_yaml": "templates: []"},
            headers=_auth(),
        )
        render = await client.post(
            "/api/v1/admin/ui/templates/render",
            json={"document": {"templates": []}},
            headers=_auth(),
        )
    assert parse.status_code == 200
    assert render.status_code == 403


@pytest.mark.asyncio
async def test_policy_designer_parse_and_render_round_trip(tmp_path, monkeypatch):
    app = create_app(_settings(tmp_path, monkeypatch))
    policy_yaml = """
default:
  enabled: true
  max_limit: 50
connections:
  demo:
    denied_tables: [employees]
"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        parsed = await client.post(
            "/api/v1/admin/ui/policy/parse",
            json={"policy_yaml": policy_yaml},
            headers=_auth(),
        )
        document = parsed.json()["document"]
        document["connections"]["demo"]["max_limit"] = 25
        rendered = await client.post(
            "/api/v1/admin/ui/policy/render",
            json={"document": document},
            headers=_auth(),
        )

    assert parsed.status_code == 200
    assert document["connections"]["demo"]["denied_tables"] == ["employees"]
    assert rendered.status_code == 200
    assert "max_limit: 25" in rendered.json()["policy_yaml"]


@pytest.mark.asyncio
async def test_policy_simulation_uses_target_principal_and_redacts_filter_value(
    tmp_path, monkeypatch
):
    set_policy_store(
        PolicyStore(
            default=Policy(enabled=False),
            overrides={},
            principal_overrides={
                "reporting-agent": {
                    "demo": {
                        "enabled": True,
                        "allowed_tables": ["orders"],
                        "denied_columns": {"orders": ["customer_email"]},
                        "mandatory_row_filters": [
                            MandatoryRowFilter(
                                table="orders", column="tenant_id", from_claim="tenant_id"
                            ).model_dump()
                        ],
                        "max_limit": 20,
                    }
                }
            },
        )
    )
    app = create_app(_settings(tmp_path, monkeypatch))
    request = {
        "principal": "reporting-agent",
        "connection": "demo",
        "table": "orders",
        "columns": ["id"],
        "claims": {"tenant_id": "acme-secret-value"},
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        allowed = await client.post("/api/v1/admin/ui/policy/test", json=request, headers=_auth())
        denied = await client.post(
            "/api/v1/admin/ui/policy/test",
            json={**request, "columns": ["customer_email"]},
            headers=_auth(),
        )

    assert allowed.status_code == 200
    assert allowed.json()["allowed"] is True
    assert allowed.json()["guardrails"]["max_limit"] == 20
    assert allowed.json()["mandatory_filters"] == [
        {
            "table": "orders",
            "column": "tenant_id",
            "source": "claim:tenant_id",
            "satisfied": True,
        }
    ]
    assert "acme-secret-value" not in allowed.text
    assert denied.status_code == 200
    assert denied.json()["allowed"] is False
    assert denied.json()["columns"] == [{"column": "customer_email", "allowed": False}]


@pytest.mark.asyncio
async def test_audit_browser_is_filtered_newest_first_and_redaction_safe(tmp_path, monkeypatch):
    audit_path = tmp_path / "audit.jsonl"
    events = [
        AuditEvent(
            event_id="query-1",
            connection_id="demo",
            principal_id="agent-a",
            policy_decision="allowed",
            outcome="success",
            query_shape={"from": "orders"},
            duration_ms=4,
        ),
        ConfigChangeEvent(
            event_id="config-1",
            action="stage",
            principal_id="admin-a",
            outcome="success",
        ),
        AuditEvent(
            event_id="query-2",
            connection_id="demo",
            principal_id="agent-b",
            policy_decision="denied",
            outcome="rejected",
            query_shape={"from": "customers"},
            duration_ms=1,
            error_category="policy",
        ),
    ]
    audit_path.write_text(
        "\n".join(event.model_dump_json(exclude_none=True) for event in events) + "\nnot-json\n"
    )
    app = create_app(_settings(tmp_path, monkeypatch, audit_path=audit_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        page = await client.get(
            "/api/v1/admin/ui/audit/events?event_type=query.execution&limit=1",
            headers=_auth(),
        )
        older = await client.get(
            f"/api/v1/admin/ui/audit/events?event_type=query.execution&limit=1&cursor={page.json()['next_cursor']}",
            headers=_auth(),
        )

    assert page.status_code == 200
    assert page.json()["total"] == 2
    assert page.json()["malformed"] == 1
    assert page.json()["events"][0]["event_id"] == "query-2"
    assert older.json()["events"][0]["event_id"] == "query-1"
    assert "params" not in json.dumps(page.json())
    assert "rows" not in json.dumps(page.json())


@pytest.mark.asyncio
async def test_audit_browser_accepts_connection_probe_event_type(tmp_path, monkeypatch):
    audit_path = tmp_path / "audit.jsonl"
    events = [
        AuditEvent(
            event_id="query-1",
            connection_id="demo",
            principal_id="agent-a",
            policy_decision="allowed",
            outcome="success",
            query_shape={"from": "orders"},
            duration_ms=4,
        ),
        ConnectionProbeEvent(
            event_id="probe-1",
            connection_id="demo",
            principal_id="admin-a",
            outcome="success",
            probe_healthy=True,
        ),
    ]
    audit_path.write_text(
        "\n".join(event.model_dump_json(exclude_none=True) for event in events) + "\n"
    )
    app = create_app(_settings(tmp_path, monkeypatch, audit_path=audit_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        page = await client.get(
            "/api/v1/admin/ui/audit/events?event_type=connection.probe",
            headers=_auth(),
        )
        unsupported = await client.get(
            "/api/v1/admin/ui/audit/events?event_type=not.a.real.type",
            headers=_auth(),
        )

    assert page.status_code == 200
    assert page.json()["total"] == 1
    assert page.json()["events"][0]["event_id"] == "probe-1"
    assert page.json()["events"][0]["event_type"] == "connection.probe"
    assert unsupported.status_code == 422


@pytest.mark.asyncio
async def test_admin_support_apis_require_config_scope(tmp_path, monkeypatch):
    app = create_app(_settings(tmp_path, monkeypatch, scopes=[]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        policy_test = await client.post(
            "/api/v1/admin/ui/policy/test",
            json={"principal": "agent", "connection": "demo"},
            headers=_auth(),
        )
        audit = await client.get("/api/v1/admin/ui/audit/events", headers=_auth())
        parse = await client.post(
            "/api/v1/admin/ui/policy/parse",
            json={"policy_yaml": "default: {}"},
            headers=_auth(),
        )
        # item 87: the template-authoring support endpoints are config-scoped too,
        # so a caller with no config scope can never read a template's query
        # skeleton through them (the admin-only "view query" path).
        templates_parse = await client.post(
            "/api/v1/admin/ui/templates/parse",
            json={"templates_yaml": "templates: []"},
            headers=_auth(),
        )
        templates_render = await client.post(
            "/api/v1/admin/ui/templates/render",
            json={"document": {"templates": []}},
            headers=_auth(),
        )

    assert policy_test.status_code == 403
    assert audit.status_code == 403
    assert parse.status_code == 403
    assert templates_parse.status_code == 403
    assert templates_render.status_code == 403


@pytest.mark.asyncio
async def test_local_draft_recovery_is_policy_only_never_credentials(tmp_path, monkeypatch):
    """Item 47's security invariant: browser storage holds only the policy
    document. connections.yaml (which can carry a literal credential), secret
    references, and bearer tokens are never written to localStorage — full-config
    recovery goes through the explicitly downloaded change-set file instead."""
    app = create_app(_settings(tmp_path, monkeypatch))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        script = (await client.get("/admin/app.js")).text

    # Exactly one write to localStorage, and it stores the policy draft only.
    assert script.count("localStorage.setItem(") == 1
    assert 'LOCAL_POLICY_KEY = "querygate.admin.policyDraft"' in script
    # The single setItem call's first argument is the policy key — no other key
    # (i.e. no connections/token key) is ever persisted.
    set_item_index = script.index("localStorage.setItem(")
    set_item_call = script[set_item_index : set_item_index + 80]
    assert "LOCAL_POLICY_KEY" in set_item_call
    # The only localStorage write persists the policy document — the save
    # function never reaches for the connections document (credentials) or a token.
    save_region = _local_storage_region(script)
    assert "draftDocuments.connections" not in save_region
    assert "token" not in save_region.lower()
    assert "state.draftDocuments.policy" in save_region
    # The bearer token is in-memory only — never written to any web storage, so
    # an XSS cannot exfiltrate it from localStorage/sessionStorage. There is no
    # "remember token" affordance, and no code path stores the token.
    assert "sessionStorage" not in script
    assert "querygate_admin_token" not in script


def _local_storage_region(script: str) -> str:
    """The savePolicyDraftLocally function body, for asserting what it persists."""
    start = script.index("function savePolicyDraftLocally")
    end = script.index("function clearLocalPolicyDraft")
    return script[start:end]


@pytest.mark.asyncio
async def test_query_templates_endpoint_is_reachable_and_needs_no_config_scope(
    tmp_path, monkeypatch
):
    """The read-only query-templates panel (item 48) calls GET /query-templates,
    which is filtered by per-connection visibility and needs no config scope —
    so it returns 200 (a list) even for a caller with no scopes at all."""
    app = create_app(_settings(tmp_path, monkeypatch, scopes=[]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        response = await client.get("/api/v1/query-templates", headers=_auth())
    assert response.status_code == 200
    assert isinstance(response.json(), list)
