"""Integration coverage for TODO item 31's browser control plane."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import querygate.api.app as _app_module
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


def _settings(tmp_path, monkeypatch, *, scopes=None, audit_path=None, audit_backend=None, **extra):
    monkeypatch.setenv("ADMIN_UI_DB_URL", "postgresql+asyncpg://user:pass@localhost/demo")
    connections_file, policy_file = _write_source_files(tmp_path)
    return AppConfig(
        environment="localhost",
        mcp_enabled=False,
        audit_sink_backend=audit_backend or ("jsonl" if audit_path else "none"),
        audit_jsonl_path=str(audit_path or tmp_path / "unused.jsonl"),
        connections_file=connections_file,
        policy_file=policy_file,
        api_keys=[_ADMIN_KEY],
        api_key_scopes=(
            scopes if scopes is not None else ["admin:config:read", "admin:config:write"]
        ),
        **extra,
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
    # TODO.md item 59 phase 2: read-only per-principal anomaly panel in the same
    # observability domain, over the same admin:observability:read endpoint.
    assert 'id="anomaly-table-wrap"' in response.text
    assert "Behavioral anomalies" in response.text
    assert "/admin/observability/anomalies" in script.text
    # TODO.md item 44 phase 2 slice: config/catalog change-velocity trend card,
    # over the persisted audit stream rather than the metrics registry.
    assert 'id="change-trend-table-wrap"' in response.text
    assert "Change velocity" in response.text
    assert "/admin/observability/config-changes" in script.text
    # TODO.md item 44 phase 2 remainder: real time-window trend charts from an
    # operator-configured external metrics backend.
    assert 'id="history-charts"' in response.text
    assert "Trend charts" in response.text
    assert "/admin/observability/history" in script.text
    # TODO.md item 195: the observed-shapes panel — the promotion surface with a
    # human in it. Asserted here rather than only in a JS test because the two
    # properties that make it safe are both *in the markup*: it is gated on its
    # own scope, and it never offers an install action.
    assert 'id="shapes-table-wrap"' in response.text
    assert "Observed query shapes" in response.text
    assert "admin:shapes:read" in response.text
    assert "/admin/observability/observed-shapes" in script.text
    # The draft dialog exists and says plainly that it does not install.
    assert 'id="shape-draft-dialog"' in response.text
    assert "not installed" in response.text
    # No install/apply affordance anywhere in this panel: the only way a template
    # reaches the live store is a governed config change, and a button here would
    # be a second path around that review gate.
    assert "install-template" not in response.text
    assert "/query-templates/install" not in script.text
    # "Recording is off" must never render the same as "your agent ran nothing" —
    # an operator who confuses them narrows a connection to an empty template set.
    assert "DISABLED" in script.text
    # The honest-scope wording for the volatile backend, so a narrowing decision
    # is not taken off a per-replica list without knowing that is what it is.
    assert "survives a restart" in script.text
    assert "discarded on restart" in script.text

    # TODO.md item 47: portable change-set export/import + policy-only local recovery.
    assert 'id="export-change-set"' in response.text
    assert 'id="import-change-set"' in response.text
    assert "/admin/config/export" in script.text
    # TODO.md item 47 phase 2: server-side encrypted-at-rest draft store.
    assert 'id="save-draft-server"' in response.text
    assert "Saved drafts" in response.text
    assert "/admin/config/drafts" in script.text
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
async def test_policy_simulation_mandatory_filter_match_survives_a_casefold_lower_disagreement(
    tmp_path, monkeypatch
):
    # "STRASSE".casefold() == "straße".casefold() but "STRASSE".lower() != "straße".lower()
    # (ß is not in .lower()'s ASCII-only fold). `policy.table_allowed`/`column_allowed`
    # (called a few lines above the mandatory-filter match in `_test_policy`) already
    # casefold, so the mandatory-filter match must too, or this simulator can report a
    # simulated allowed=True verdict with no mandatory filter listed, for exactly the
    # table/filter pair real execution would reject (TODO.md item 150).
    set_policy_store(
        PolicyStore(
            default=Policy(enabled=False),
            overrides={},
            principal_overrides={
                "reporting-agent": {
                    "demo": {
                        "enabled": True,
                        "allowed_tables": ["STRASSE"],
                        "mandatory_row_filters": [
                            MandatoryRowFilter(
                                table="straße", column="tenant_id", from_claim="tenant_id"
                            ).model_dump()
                        ],
                    }
                }
            },
        )
    )
    app = create_app(_settings(tmp_path, monkeypatch))
    request = {
        "principal": "reporting-agent",
        "connection": "demo",
        "table": "STRASSE",
        "columns": [],
        "claims": {},
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        response = await client.post("/api/v1/admin/ui/policy/test", json=request, headers=_auth())

    assert response.status_code == 200
    body = response.json()
    assert body["mandatory_filters"] == [
        {
            "table": "straße",
            "column": "tenant_id",
            "source": "claim:tenant_id",
            "satisfied": False,
        }
    ]


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
    # The ordinary (no-cap-hit) path must report truncated=False, not just
    # default to it — a regression that always set truncated=True on this
    # branch would otherwise slip past every other test in this file, none
    # of which are near audit_page_max_lines_read.
    assert page.json()["truncated"] is False
    assert "params" not in json.dumps(page.json())
    assert "rows" not in json.dumps(page.json())


@pytest.mark.asyncio
async def test_audit_browser_rejects_a_cursor_past_the_lowered_ceiling(tmp_path, monkeypatch):
    """TODO.md item 140: the old cursor ceiling (1_000_000) let one request
    retain on the order of a gigabyte of fully-parsed event dicts before
    slicing the response page. Lowered to 5,000 — far past any real "load
    more" admin session, but no longer six figures."""
    app = create_app(_settings(tmp_path, monkeypatch))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        at_ceiling = await client.get(
            "/api/v1/admin/ui/audit/events?cursor=5000",
            headers=_auth(),
        )
        past_ceiling = await client.get(
            "/api/v1/admin/ui/audit/events?cursor=5001",
            headers=_auth(),
        )

    assert at_ceiling.status_code == 200
    assert past_ceiling.status_code == 422


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
async def test_audit_browser_reads_hash_chained_ledger(tmp_path, monkeypatch):
    """TODO.md item 136: `AUDIT_SINK_BACKEND=jsonl_chained` used to leave this
    surface refused at the gate (`source="disabled"`), and if the gate alone
    were widened it would have gone silently empty instead — `_audit_page` had
    no envelope unwrap, so every real record would count as `malformed`. Both
    the gate and the reader must accept the tamper-evident backend."""
    from querygate.audit.ledger import GENESIS_PREV_HASH, make_record

    audit_path = tmp_path / "audit.jsonl"
    event = AuditEvent(
        event_id="query-1",
        connection_id="demo",
        principal_id="agent-a",
        policy_decision="allowed",
        outcome="success",
        query_shape={"from": "orders"},
        duration_ms=4,
    )
    record = make_record(0, GENESIS_PREV_HASH, event.model_dump(mode="json", exclude_none=True))
    audit_path.write_text(record.model_dump_json() + "\n")
    app = create_app(
        _settings(tmp_path, monkeypatch, audit_path=audit_path, audit_backend="jsonl_chained")
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        page = await client.get(
            "/api/v1/admin/ui/audit/events?event_type=query.execution",
            headers=_auth(),
        )

    assert page.status_code == 200
    assert page.json()["source"] == "jsonl_chained"
    assert page.json()["total"] == 1
    assert page.json()["malformed"] == 0
    assert page.json()["events"][0]["event_id"] == "query-1"


@pytest.mark.asyncio
async def test_audit_browser_rejects_a_forged_chain_record(tmp_path, monkeypatch):
    """TODO.md item 137: a chain envelope whose own hash doesn't match its
    contents (a record appended by an actor with file access, not derived from
    a real emit) must never be displayed as a clean event — chain linkage
    alone wouldn't catch this, since the forgery can still supply a
    plausible-looking `prev_hash`/`seq`."""
    from querygate.audit.ledger import GENESIS_PREV_HASH, LedgerRecord, make_record

    audit_path = tmp_path / "audit.jsonl"
    genuine = AuditEvent(
        event_id="query-1",
        connection_id="demo",
        principal_id="agent-a",
        policy_decision="allowed",
        outcome="success",
        query_shape={"from": "orders"},
        duration_ms=4,
    )
    record = make_record(0, GENESIS_PREV_HASH, genuine.model_dump(mode="json", exclude_none=True))
    forged_event = AuditEvent(
        event_id="query-2",
        connection_id="demo",
        principal_id="agent-a",
        policy_decision="allowed",
        outcome="success",
        query_shape={"from": "secret_table"},
        duration_ms=4,
    )
    forged = LedgerRecord(
        seq=1,
        prev_hash=record.hash,
        event=forged_event.model_dump(mode="json", exclude_none=True),
        hash="anything",
    )
    audit_path.write_text(record.model_dump_json() + "\n" + forged.model_dump_json() + "\n")
    app = create_app(
        _settings(tmp_path, monkeypatch, audit_path=audit_path, audit_backend="jsonl_chained")
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        page = await client.get(
            "/api/v1/admin/ui/audit/events?event_type=query.execution",
            headers=_auth(),
        )

    assert page.status_code == 200
    assert page.json()["source"] == "jsonl_chained"
    assert page.json()["total"] == 1
    assert page.json()["malformed"] == 1
    assert page.json()["events"][0]["event_id"] == "query-1"
    assert all(e["event_id"] != "query-2" for e in page.json()["events"])


@pytest.mark.asyncio
async def test_audit_browser_rejects_a_bare_envelope_less_line_on_a_chained_backend(
    tmp_path, monkeypatch
):
    """TODO.md item 137 regression (found by `security-invariant-reviewer`,
    2026-08-05): `verify_envelope_hash` correctly returns `None` (not
    `False`) for a line with no envelope shape at all, since that's exactly
    what a legitimate plain-`jsonl` line looks like. On a `jsonl_chained`
    backend every persisted line MUST be an envelope, so a bare line is
    itself the forgery/corruption signal and must not pass through
    unverified."""
    from querygate.audit.ledger import GENESIS_PREV_HASH, make_record

    audit_path = tmp_path / "audit.jsonl"
    genuine = AuditEvent(
        event_id="query-1",
        connection_id="demo",
        principal_id="agent-a",
        policy_decision="allowed",
        outcome="success",
        query_shape={"from": "orders"},
        duration_ms=4,
    )
    record = make_record(0, GENESIS_PREV_HASH, genuine.model_dump(mode="json", exclude_none=True))
    bare_event = AuditEvent(
        event_id="query-2",
        connection_id="demo",
        principal_id="agent-a",
        policy_decision="allowed",
        outcome="success",
        query_shape={"from": "secret_table"},
        duration_ms=4,
    )
    audit_path.write_text(
        record.model_dump_json() + "\n" + bare_event.model_dump_json(exclude_none=True) + "\n"
    )
    app = create_app(
        _settings(tmp_path, monkeypatch, audit_path=audit_path, audit_backend="jsonl_chained")
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        page = await client.get(
            "/api/v1/admin/ui/audit/events?event_type=query.execution",
            headers=_auth(),
        )

    assert page.status_code == 200
    assert page.json()["total"] == 1
    assert page.json()["malformed"] == 1
    assert page.json()["events"][0]["event_id"] == "query-1"
    assert all(e["event_id"] != "query-2" for e in page.json()["events"])


@pytest.mark.asyncio
async def test_audit_browser_reports_disabled_without_a_locally_readable_backend(
    tmp_path, monkeypatch
):
    """TODO.md item 136: the same gate line this item fixed
    (`is_locally_readable()`) must still refuse `AuditSinkBackend.NONE`, even
    when a file that would otherwise parse as valid audit events already
    exists at the configured path — proving the gate short-circuits on the
    backend, not merely on an absent/empty file."""
    audit_path = tmp_path / "audit.jsonl"
    event = AuditEvent(
        event_id="query-1",
        connection_id="demo",
        principal_id="agent-a",
        policy_decision="allowed",
        outcome="success",
        query_shape={"from": "orders"},
        duration_ms=4,
    )
    audit_path.write_text(event.model_dump_json(exclude_none=True) + "\n")
    app = create_app(_settings(tmp_path, monkeypatch, audit_path=audit_path, audit_backend="none"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        page = await client.get("/api/v1/admin/ui/audit/events", headers=_auth())

    assert page.status_code == 200
    assert page.json() == {
        "source": "disabled",
        "events": [],
        "total": 0,
        "malformed": 0,
        "next_cursor": None,
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_audit_browser_finds_newest_events_past_a_huge_prefix_of_old_lines(
    tmp_path, monkeypatch
):
    """TODO.md item 138: reading tail-first means a line-read cap far smaller
    than the file still finds the newest matching events completely — a
    forward-and-cap scan from the start of the file would have found none of
    them, since they're the physically last lines written."""
    audit_path = tmp_path / "audit.jsonl"
    with open(audit_path, "w", encoding="utf-8") as handle:
        # A different event_type so the query.execution filter below excludes
        # every one of these, regardless of how many precede the real matches.
        for i in range(2000):
            handle.write(
                ConnectionProbeEvent(
                    event_id=f"ancient-{i}",
                    connection_id="demo",
                    principal_id="agent-a",
                    outcome="success",
                    probe_healthy=True,
                ).model_dump_json(exclude_none=True)
                + "\n"
            )
        for i in range(3):
            handle.write(
                AuditEvent(
                    event_id=f"recent-{i}",
                    connection_id="demo",
                    principal_id="agent-a",
                    policy_decision="allowed",
                    outcome="success",
                    query_shape={"from": "orders"},
                    duration_ms=1,
                ).model_dump_json(exclude_none=True)
                + "\n"
            )
    app = create_app(
        _settings(
            tmp_path,
            monkeypatch,
            audit_path=audit_path,
            audit_backend="jsonl",
            audit_page_max_lines_read=10,
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        page = await client.get(
            "/api/v1/admin/ui/audit/events?event_type=query.execution&limit=10",
            headers=_auth(),
        )

    body = page.json()
    assert page.status_code == 200
    ids = [e["event_id"] for e in body["events"]]
    assert ids == ["recent-2", "recent-1", "recent-0"]
    assert body["truncated"] is True


def test_audit_browser_exposes_exactly_four_filters_in_the_ui():
    """`CUSTOMER_README.md`, `sales/index.html` and the go-to-market analysis
    each tell a customer or a salesperson which filters the admin UI's Audit view
    offers. That claim was wrong once already — an earlier revision said five,
    counting the `action` parameter that the REST endpoint accepts but the UI has
    no control for — so pin the set rather than trusting prose to stay in step.

    Deliberately asserts the *UI's* controls, not the endpoint's signature: the
    endpoint may legitimately accept more than the UI exposes (it does), and it is
    the UI surface the docs describe.
    """
    # Resolved exactly as api/app.py mounts it, so the test cannot pass against a
    # directory the server does not actually serve.
    ui_root = Path(_app_module.__file__).resolve().parent.parent / "admin_ui"
    markup = (ui_root / "index.html").read_text(encoding="utf-8")
    app_js = (ui_root / "app.js").read_text(encoding="utf-8")

    # Slice the filter form and enumerate whatever ids are inside it. A fixed
    # alternation would only pin the four names against *removal* — a fifth
    # control (say `audit-table`) would match nothing and the test would pass
    # while the docs went stale, which is the exact drift this exists to catch.
    form_open = markup.index('<form id="audit-filters"')
    form = markup[markup.index(">", form_open) + 1 : markup.index("</form>", form_open)]
    control_ids = set(re.findall(r'id="([^"]+)"', form))
    assert control_ids == {
        "audit-event-type",
        "audit-outcome",
        "audit-principal",
        "audit-connection",
    }, (
        "the admin UI's audit filter controls changed — update the customer-facing docs "
        "that enumerate them (CUSTOMER_README.md, sales/index.html)"
    )

    # And that the request builder sends exactly those four, so a control could not
    # be added to the markup while silently never reaching the endpoint (or vice versa).
    # Enumerated from the `fields` object literal for the same reason as above.
    fields_start = app_js.index("const fields = {", app_js.index("function auditQuery("))
    fields = app_js[fields_start : app_js.index("};", fields_start)]
    sent = set(re.findall(r"^\s*([A-Za-z_]+):", fields, re.M))
    assert sent == {"event_type", "outcome", "principal_id", "connection_id"}, (
        "the admin UI's audit request builder changed — a filter reaching it that the "
        "docs don't name (or vice versa) makes the four-filter claim wrong"
    )


@pytest.mark.asyncio
async def test_audit_browser_bounds_lines_read_not_just_page_size(tmp_path, monkeypatch):
    audit_path = tmp_path / "audit.jsonl"
    with open(audit_path, "w", encoding="utf-8") as handle:
        for i in range(500):
            handle.write(
                ConnectionProbeEvent(
                    event_id=f"probe-{i}",
                    connection_id="demo",
                    principal_id="admin-a",
                    outcome="success",
                    probe_healthy=True,
                ).model_dump_json(exclude_none=True)
                + "\n"
            )
    app = create_app(
        _settings(
            tmp_path,
            monkeypatch,
            audit_path=audit_path,
            audit_backend="jsonl",
            audit_page_max_lines_read=20,
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        page = await client.get(
            "/api/v1/admin/ui/audit/events?event_type=query.execution",
            headers=_auth(),
        )

    body = page.json()
    assert page.status_code == 200
    assert body["events"] == []  # every line is a connection.probe, never matched
    assert body["total"] == 0
    assert body["truncated"] is True  # stopped at the line cap, not because it finished


@pytest.mark.asyncio
async def test_audit_browser_reports_truncated_when_the_underlying_reader_bails(
    tmp_path, monkeypatch
):
    # audit.file_reader.iter_lines_reverse raises AuditFileReadBounded on an
    # internal safety bound (an oversized undelimited line, here) rather than
    # silently stopping — the route must catch it and return a normal 200
    # with truncated=True, not a 500.
    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_bytes(b"x" * 2_000_000)  # one giant line, no newline anywhere
    app = create_app(_settings(tmp_path, monkeypatch, audit_path=audit_path, audit_backend="jsonl"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        page = await client.get("/api/v1/admin/ui/audit/events", headers=_auth())

    body = page.json()
    assert page.status_code == 200
    assert body["events"] == []
    assert body["truncated"] is True


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


@pytest.mark.asyncio
async def test_catalog_governance_phase2_ui_is_wired_and_scope_gated(tmp_path, monkeypatch):
    """Item 38 phase 2: the catalog workspace surfaces bulk approve/reject/
    delete, export/import (backup/restore), generate-drafts/learn triggers,
    a per-proposal review_history detail view, and a usage-signals browsing
    tab — all thin wrappers over the existing 32B governance routes, which
    already shipped in phase 1 (this phase is frontend-only)."""
    app = create_app(_settings(tmp_path, monkeypatch))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        page = await client.get("/admin/")
        script = (await client.get("/admin/app.js")).text

    # New "Usage signals" nav tab, in the same catalog domain as the existing
    # Review proposals / Versions & rollback tabs.
    assert "Usage signals" in script
    assert '"catalog-usage"' in script
    assert "/usage-signals" in script
    assert 'data-catalog-panel="usage"' in page.text

    # Bulk approve/reject/delete: checkboxes wired to a selection Set and a
    # toolbar calling the existing bulk-* endpoints.
    assert "data-proposal-checkbox" in script
    assert "bulk-approve" in script and "bulk-reject" in script and "bulk-delete" in script
    assert "/proposals/bulk-approve" in script
    assert "/proposals/bulk-reject" in script
    assert "/proposals/bulk-delete" in script
    assert 'id="proposal-bulk-toolbar"' in page.text

    # Export/import (backup/restore) for the catalog itself.
    assert 'id="export-catalog"' in page.text
    assert 'id="import-catalog"' in page.text
    assert "exportCatalog" in script and "importCatalog" in script
    assert "/export" in script and "/import" in script

    # Generate-drafts / learn triggers callable from the browser.
    assert 'id="generate-drafts"' in page.text
    assert 'id="run-learn"' in page.text
    assert "/generate-drafts" in script
    assert "runLearn" in script

    # Per-proposal review_history detail view, populated from the
    # single-proposal fetch (ProposalDetail), never the bulk list.
    assert 'id="review-history-body"' in page.text
    assert "renderReviewHistory" in script
    assert "review_history" in script

    # Scope gating: bulk actions disabled without the matching scope, the same
    # posture as the existing single-proposal approve/reject/publish buttons.
    assert '"#bulk-approve").disabled = !hasScope("catalog:approve")' in script
    assert '"#bulk-reject").disabled = !hasScope("catalog:reject")' in script
    assert '"#bulk-delete").disabled = !hasScope("catalog:delete")' in script
    assert '"#generate-drafts").disabled = !hasScope("catalog:generate")' in script
    assert '"#export-catalog").disabled = !hasScope("catalog:export")' in script

    # Audit fix: each proposal-row checkbox's accessible name distinguishes it
    # from every other row (not a generic "Select proposal" repeated for every
    # row in the queue).
    assert 'aria-label="Select ${escapeHtml(proposalTargetLabel(proposal.target))}"' in script
    assert 'aria-label="Select proposal"' not in script

    # Audit fix: selectProposal() discards a stale response instead of letting
    # a slower, earlier-clicked row's fetch overwrite a faster, later one.
    assert "latestProposalSelectionId" in script
    assert "if (latestProposalSelectionId !== proposalId) return;" in script


@pytest.mark.asyncio
async def test_four_eyes_review_ui_is_wired_and_scope_gated(tmp_path, monkeypatch):
    """Item 42 phase 2: the versions view surfaces four-eyes review — approval
    status per staged version, Approve/Reject buttons gated on the
    admin:config:approve scope (canApprove), calling the /approve and /reject
    endpoints. Server enforcement (item 42 ph1) is unchanged; this only makes the
    flow visible/usable in the browser."""
    app = create_app(_settings(tmp_path, monkeypatch))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        script = (await client.get("/admin/app.js")).text
    # Scope gate: approve buttons are shown only to an admin:config:approve holder.
    assert 'canApprove() { return hasScope("admin:config:approve"); }' in script
    # The buttons and their endpoints are wired.
    assert "data-approve-version" in script and "data-reject-version" in script
    assert "/approve" in script and "/reject" in script
    assert "reviewDecision(" in script
    # Approval status is surfaced per staged version.
    assert "approvalSummary(" in script


@pytest.mark.asyncio
async def test_observed_shapes_panel_loads_for_a_shapes_only_caller(tmp_path, monkeypatch):
    """`admin:shapes:read` and `admin:observability:read` are separate scopes on
    purpose, so a caller holding only the first must still get the shapes panel —
    not one 403 from the aggregate reads blanking the whole view.

    Asserted behaviourally (a real request with only the shapes scope) rather
    than by reading the JS, because "these two loads are independent" is exactly
    the kind of claim that is true in the markup and false in the control flow.
    """
    from querygate.admin.observed_shapes import configure_observed_shape_store, observed_shape_store
    from querygate.query_ast.models import Predicate, StructuredQuery

    configure_observed_shape_store(500, enabled=True)
    await observed_shape_store().record(
        StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            where=Predicate(col="orders.status", op="eq", value="completed"),
            limit=5,
        ),
        connection_id="demo",
        principal_id="agent",
    )

    app = create_app(
        AppConfig(
            environment="localhost",
            mcp_enabled=False,
            observed_shapes_enabled=True,
            api_keys=["shapes-only-key"],
            api_key_scopes=["admin:shapes:read"],
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        headers = {"Authorization": "Bearer shapes-only-key"}
        shapes = await client.get("/api/v1/admin/observability/observed-shapes", headers=headers)
        overview = await client.get("/api/v1/admin/observability/overview", headers=headers)

    # The shapes read succeeds...
    assert shapes.status_code == 200
    assert shapes.json()["enabled"] is True
    assert len(shapes.json()["shapes"]) == 1
    # ...while the aggregate read this caller has no scope for is refused, which
    # is the point: one scope does not imply the other in either direction.
    assert overview.status_code == 403


@pytest.mark.asyncio
async def test_the_panel_distinguishes_disabled_from_unreachable_from_empty(tmp_path, monkeypatch):
    """Three different reasons the shapes list can be empty, three different
    operator actions. The panel must never render them the same — confusing
    "recording is off" or "the backend is down" with "your agent ran nothing" is
    how a connection gets narrowed to an empty template set.
    """
    app = create_app(_settings(tmp_path, monkeypatch))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        script = (await client.get("/admin/app.js")).text
    # disabled
    assert "recording is DISABLED" in script
    # unreachable — must name itself distinctly and warn against narrowing
    assert "UNREACHABLE" in script
    assert "not because nothing ran" in script.lower()
    # enabled-but-empty
    assert "no query shapes have been seen yet" in script
