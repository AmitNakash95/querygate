"""Integration tests for the server-side encrypted draft store API (TODO.md
item 47, phase 2): POST/GET/DELETE /api/v1/admin/config/drafts[/{id}].
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from querygate.api.app import create_app
from querygate.core.config import AppConfig

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"
_ADMIN_KEY = "draft-store-admin-key"


def _write_source_files(tmp_path):
    connections_file = tmp_path / "connections.yaml"
    connections_file.write_text("""
connections:
  - id: draft-demo
    dialect: postgresql
    connection_string: ${DRAFT_TEST_DB_URL}
    known_tables: [foo]
""")
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("default:\n  enabled: true\n")
    return str(connections_file), str(policy_file)


def _settings(connections_file, policy_file, tmp_path, *, enabled=True, **overrides) -> AppConfig:
    values = dict(
        environment="localhost",
        mcp_enabled=False,
        audit_sink_backend="none",
        connections_file=connections_file,
        policy_file=policy_file,
        api_keys=[_ADMIN_KEY],
        api_key_scopes=["admin:config:write", "admin:config:read"],
        draft_store_dir=str(tmp_path / "drafts"),
        draft_store_encryption_key="a-real-secret" if enabled else "",
    )
    values.update(overrides)
    return AppConfig(**values)


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DRAFT_TEST_DB_URL", "postgresql+asyncpg://user:pass@localhost/x")
    connections_file, policy_file = _write_source_files(tmp_path)
    return create_app(_settings(connections_file, policy_file, tmp_path))


@pytest.fixture
def disabled_app(tmp_path, monkeypatch):
    monkeypatch.setenv("DRAFT_TEST_DB_URL", "postgresql+asyncpg://user:pass@localhost/x")
    connections_file, policy_file = _write_source_files(tmp_path)
    return create_app(_settings(connections_file, policy_file, tmp_path, enabled=False))


async def _export_bundle(app, description="a saved draft"):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/admin/config/export",
            json={
                "policy_yaml": "default:\n  enabled: true\n  max_joins: 4\n",
                "description": description,
            },
            headers=_auth(_ADMIN_KEY),
        )
    assert resp.status_code == 200
    return resp.json()


@pytest.mark.asyncio
async def test_save_requires_authentication(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/admin/config/drafts", json={"created_at": "2026-01-01T00:00:00Z"}
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_requires_authentication(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get("/api/v1/admin/config/drafts")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_read_only_scope_is_sufficient_to_list(tmp_path, monkeypatch):
    """The single-scope contract's *sufficient* direction, complementing
    test_adversarial_security.py's *necessary* direction (a write-only caller
    is rejected): a caller with ONLY admin:config:read must be able to list —
    it must not also require write scope."""
    monkeypatch.setenv("DRAFT_TEST_DB_URL", "postgresql+asyncpg://user:pass@localhost/x")
    connections_file, policy_file = _write_source_files(tmp_path)
    app = create_app(
        _settings(
            connections_file,
            policy_file,
            tmp_path,
            api_key_scopes=["admin:config:read"],
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get("/api/v1/admin/config/drafts", headers=_auth(_ADMIN_KEY))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_write_only_scope_is_sufficient_for_save_load_delete(tmp_path, monkeypatch):
    """The complementary sufficient direction for save/load/delete: a caller
    with ONLY admin:config:write (no read) must be able to do all three —
    listing is the one operation that needs read scope, not these."""
    monkeypatch.setenv("DRAFT_TEST_DB_URL", "postgresql+asyncpg://user:pass@localhost/x")
    connections_file, policy_file = _write_source_files(tmp_path)
    app = create_app(
        _settings(
            connections_file,
            policy_file,
            tmp_path,
            api_key_scopes=["admin:config:write"],
        )
    )
    bundle = await _export_bundle(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        saved = await client.post(
            "/api/v1/admin/config/drafts", json=bundle, headers=_auth(_ADMIN_KEY)
        )
        assert saved.status_code == 201
        draft_id = saved.json()["id"]
        loaded = await client.get(
            f"/api/v1/admin/config/drafts/{draft_id}", headers=_auth(_ADMIN_KEY)
        )
        assert loaded.status_code == 200
        deleted = await client.delete(
            f"/api/v1/admin/config/drafts/{draft_id}", headers=_auth(_ADMIN_KEY)
        )
        assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_save_list_load_delete_round_trip(app):
    bundle = await _export_bundle(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        saved = await client.post(
            "/api/v1/admin/config/drafts", json=bundle, headers=_auth(_ADMIN_KEY)
        )
        assert saved.status_code == 201
        draft_id = saved.json()["id"]

        listing = await client.get("/api/v1/admin/config/drafts", headers=_auth(_ADMIN_KEY))
        assert listing.status_code == 200
        assert [d["id"] for d in listing.json()] == [draft_id]

        loaded = await client.get(
            f"/api/v1/admin/config/drafts/{draft_id}", headers=_auth(_ADMIN_KEY)
        )
        assert loaded.status_code == 200
        assert loaded.json()["documents"] == bundle["documents"]

        deleted = await client.delete(
            f"/api/v1/admin/config/drafts/{draft_id}", headers=_auth(_ADMIN_KEY)
        )
        assert deleted.status_code == 204

        gone = await client.get(
            f"/api/v1/admin/config/drafts/{draft_id}", headers=_auth(_ADMIN_KEY)
        )
        assert gone.status_code == 404

        empty_listing = await client.get("/api/v1/admin/config/drafts", headers=_auth(_ADMIN_KEY))
        assert empty_listing.json() == []


@pytest.mark.asyncio
async def test_load_unknown_draft_is_404(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get(
            "/api/v1/admin/config/drafts/does-not-exist", headers=_auth(_ADMIN_KEY)
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_unknown_draft_is_404(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.delete(
            "/api/v1/admin/config/drafts/does-not-exist", headers=_auth(_ADMIN_KEY)
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_endpoints_report_503_when_store_disabled(disabled_app):
    async with AsyncClient(transport=ASGITransport(app=disabled_app), base_url=_BASE_URL) as client:
        save_resp = await client.post(
            "/api/v1/admin/config/drafts",
            json={"created_at": "2026-01-01T00:00:00Z", "documents": {}},
            headers=_auth(_ADMIN_KEY),
        )
        list_resp = await client.get("/api/v1/admin/config/drafts", headers=_auth(_ADMIN_KEY))
        load_resp = await client.get(
            "/api/v1/admin/config/drafts/anything", headers=_auth(_ADMIN_KEY)
        )
        delete_resp = await client.delete(
            "/api/v1/admin/config/drafts/anything", headers=_auth(_ADMIN_KEY)
        )
    assert save_resp.status_code == 503
    assert list_resp.status_code == 503
    assert load_resp.status_code == 503
    assert delete_resp.status_code == 503


@pytest.mark.asyncio
async def test_max_drafts_per_principal_cap_is_enforced_over_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("DRAFT_TEST_DB_URL", "postgresql+asyncpg://user:pass@localhost/x")
    connections_file, policy_file = _write_source_files(tmp_path)
    app = create_app(
        _settings(connections_file, policy_file, tmp_path, draft_store_max_drafts_per_principal=1)
    )
    bundle = await _export_bundle(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        first = await client.post(
            "/api/v1/admin/config/drafts", json=bundle, headers=_auth(_ADMIN_KEY)
        )
        assert first.status_code == 201
        second = await client.post(
            "/api/v1/admin/config/drafts", json=bundle, headers=_auth(_ADMIN_KEY)
        )
    assert second.status_code == 422


@pytest.mark.asyncio
async def test_saved_draft_never_leaks_another_principals_draft(tmp_path, monkeypatch):
    """The security-critical property, proven end-to-end with two JWTs
    (distinct `sub` claims), mirroring item 45's isolation proof: neither
    principal can list, load, or delete the other's saved draft."""
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    monkeypatch.setenv("DRAFT_TEST_DB_URL", "postgresql+asyncpg://user:pass@localhost/x")
    connections_file, policy_file = _write_source_files(tmp_path)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    def _token(subject: str) -> str:
        return jwt.encode(
            {
                "sub": subject,
                "iss": "https://idp.example.com/",
                "aud": "querygate",
                "scope": "admin:config:write admin:config:read",
            },
            private_key,
            algorithm="RS256",
        )

    app = create_app(
        AppConfig(
            environment="localhost",
            mcp_enabled=False,
            audit_sink_backend="none",
            connections_file=connections_file,
            policy_file=policy_file,
            jwt_enabled=True,
            jwt_jwks_url="https://idp.example.com/.well-known/jwks.json",
            jwt_issuer="https://idp.example.com/",
            jwt_audience="querygate",
            draft_store_dir=str(tmp_path / "drafts"),
            draft_store_encryption_key="a-real-secret",
        )
    )
    with patch(
        "jwt.PyJWKClient.get_signing_key_from_jwt",
        lambda self, tok: type("K", (), {"key": public_key})(),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            broad_headers = {"Authorization": f"Bearer {_token('broad-agent')}"}
            narrow_headers = {"Authorization": f"Bearer {_token('narrow-agent')}"}

            export_resp = await client.post(
                "/api/v1/admin/config/export",
                json={"policy_yaml": "default:\n  enabled: true\n", "description": "broad's draft"},
                headers=broad_headers,
            )
            bundle = export_resp.json()
            saved = await client.post(
                "/api/v1/admin/config/drafts", json=bundle, headers=broad_headers
            )
            draft_id = saved.json()["id"]

            # narrow-agent's own list must never include broad-agent's draft.
            narrow_list = await client.get("/api/v1/admin/config/drafts", headers=narrow_headers)
            assert narrow_list.json() == []

            # narrow-agent cannot load or delete broad-agent's draft by id.
            narrow_load = await client.get(
                f"/api/v1/admin/config/drafts/{draft_id}", headers=narrow_headers
            )
            narrow_delete = await client.delete(
                f"/api/v1/admin/config/drafts/{draft_id}", headers=narrow_headers
            )
            assert narrow_load.status_code == 404
            assert narrow_delete.status_code == 404
            assert "broad's draft" not in narrow_load.text

            # broad-agent's own access is unaffected.
            broad_load = await client.get(
                f"/api/v1/admin/config/drafts/{draft_id}", headers=broad_headers
            )
            assert broad_load.status_code == 200


@pytest.mark.asyncio
async def test_static_api_keys_share_one_identity_so_drafts_are_not_isolated_between_them(
    tmp_path, monkeypatch
):
    """Documents a known, pre-existing limitation surfaced by `auditors`
    review, not a defect in this feature: `DraftStore` isolates by
    `principal.subject`, which is exactly right under JWT auth (each caller's
    own `sub`, proven above) but collapses to ONE shared subject
    (`AppConfig.api_key_subject`) for every key in `AppConfig.api_keys` —
    `ApiKeyAuthenticator` has no concept of a distinct identity per
    configured key. So two different admins each holding their own API key
    see and can delete EACH OTHER's saved drafts, because the auth layer
    hands both of them the identical `principal.subject`. This is the same
    granularity every other "my own X" surface in this codebase already has
    (e.g. item 45's `/help/my-recent-denials`) — not a gap unique to drafts —
    so the fix, if wanted, is a richer API-key identity model, not a
    draft-store change. A deployment that needs per-admin draft isolation
    must use JWT auth (see the test above) rather than shared API keys.
    """
    monkeypatch.setenv("DRAFT_TEST_DB_URL", "postgresql+asyncpg://user:pass@localhost/x")
    connections_file, policy_file = _write_source_files(tmp_path)
    key_a, key_b = "admin-key-a", "admin-key-b"
    app = create_app(
        AppConfig(
            environment="localhost",
            mcp_enabled=False,
            audit_sink_backend="none",
            connections_file=connections_file,
            policy_file=policy_file,
            api_keys=[key_a, key_b],
            api_key_scopes=["admin:config:write", "admin:config:read"],
            draft_store_dir=str(tmp_path / "drafts"),
            draft_store_encryption_key="a-real-secret",
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        export_resp = await client.post(
            "/api/v1/admin/config/export",
            json={"policy_yaml": "default:\n  enabled: true\n  max_joins: 4\n"},
            headers=_auth(key_a),
        )
        bundle = export_resp.json()
        saved = await client.post("/api/v1/admin/config/drafts", json=bundle, headers=_auth(key_a))
        draft_id = saved.json()["id"]

        # key_b sees key_a's draft in its own list and can load/delete it —
        # both keys resolve to the same principal.subject.
        listing = await client.get("/api/v1/admin/config/drafts", headers=_auth(key_b))
        assert [d["id"] for d in listing.json()] == [draft_id]
        loaded = await client.get(f"/api/v1/admin/config/drafts/{draft_id}", headers=_auth(key_b))
        assert loaded.status_code == 200


@pytest.mark.asyncio
async def test_save_and_load_are_recorded_in_the_audit_stream_without_document_content(
    tmp_path, monkeypatch
):
    # configure_audit_sink normally runs inside create_app()'s lifespan
    # handler, which plain ASGITransport(app=app) never triggers — so this
    # test wires the same JSONL sink directly, matching what the real
    # lifespan would configure from this cfg.
    from querygate.audit.sinks import configure_audit_sink

    monkeypatch.setenv("DRAFT_TEST_DB_URL", "postgresql+asyncpg://user:pass@localhost/x")
    connections_file, policy_file = _write_source_files(tmp_path)
    audit_path = tmp_path / "audit.jsonl"
    configure_audit_sink(backend="jsonl", jsonl_path=str(audit_path))
    app = create_app(
        AppConfig(
            environment="localhost",
            mcp_enabled=False,
            connections_file=connections_file,
            policy_file=policy_file,
            api_keys=[_ADMIN_KEY],
            api_key_scopes=["admin:config:write", "admin:config:read"],
            audit_sink_backend="jsonl",
            audit_jsonl_path=str(audit_path),
            draft_store_dir=str(tmp_path / "drafts"),
            draft_store_encryption_key="a-real-secret",
        )
    )
    bundle = await _export_bundle(app, description="a secret rollout plan")
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        saved = await client.post(
            "/api/v1/admin/config/drafts", json=bundle, headers=_auth(_ADMIN_KEY)
        )
        draft_id = saved.json()["id"]
        await client.get(f"/api/v1/admin/config/drafts/{draft_id}", headers=_auth(_ADMIN_KEY))

    lines = [json.loads(line) for line in Path(audit_path).read_text().splitlines() if line]
    draft_events = [e for e in lines if e.get("action") in ("save_draft", "load_draft")]
    actions = {e["action"] for e in draft_events}
    assert actions == {"save_draft", "load_draft"}
    for event in draft_events:
        assert event["draft_id"] == draft_id
        assert event["outcome"] == "success"

    # Redaction: the persisted audit trail never carries the bundle's actual
    # document content — only the opaque draft id, matching export/import's
    # existing posture (which also legitimately records `description`, the
    # same free-text field ConfigChangeEvent already carries for those
    # actions; it is not document content and is not excluded).
    blob = Path(audit_path).read_text()
    assert "max_joins" not in blob
    assert '"policy":' not in blob
    assert '"documents":' not in blob
