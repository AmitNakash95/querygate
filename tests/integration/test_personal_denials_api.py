"""Integration tests for the caller's own recent-denials API (TODO.md item 45,
phase 2): GET /api/v1/help/my-recent-denials — requires only authentication
(no admin scope, matching /help/my-access), honest "disabled" when no
persisted sink, a real denial surfaced from a JSONL file, and — the
security-critical property — one principal never sees another principal's
denial."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from querygate.api.app import create_app
from querygate.audit.events import AuditEvent
from querygate.core.config import AppConfig

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"


def _settings(**overrides) -> AppConfig:
    values = {
        "environment": "localhost",
        "api_keys": [],
        "api_key_subject": "test-client",
        "api_key_scopes": [],
        "jwt_enabled": False,
        "mcp_enabled": False,
        "audit_sink_backend": "none",
    }
    values.update(overrides)
    return AppConfig(_env_file=None, **values)


def _event(*, at, principal, connection="demo", error_category="policy") -> AuditEvent:
    return AuditEvent(
        occurred_at=at,
        principal_id=principal,
        connection_id=connection,
        policy_decision="denied",
        outcome="rejected",
        query_shape={"from": "customers", "select": [{"kind": "column", "column": "ssn"}]},
        error_category=error_category,
        duration_ms=2,
    )


def _write_events(path, events):
    with open(path, "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(event.model_dump_json(exclude_none=True) + "\n")


@pytest.mark.asyncio
async def test_my_recent_denials_requires_authentication():
    app = create_app(_settings(api_keys=["required-key"]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get("/api/v1/help/my-recent-denials")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_my_recent_denials_disabled_without_a_persisted_sink():
    app = create_app(_settings(api_keys=["reader-key"], api_key_subject="reader"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get(
            "/api/v1/help/my-recent-denials", headers={"Authorization": "Bearer reader-key"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "disabled"
    assert body["denials"] == []


@pytest.mark.asyncio
async def test_my_recent_denials_surfaces_the_callers_own_denial(tmp_path):
    path = tmp_path / "audit.jsonl"
    now = datetime.now(timezone.utc)
    _write_events(
        path,
        [_event(at=now - timedelta(seconds=60), principal="reader", connection="demo")],
    )
    app = create_app(
        _settings(
            api_keys=["reader-key"],
            api_key_subject="reader",
            audit_sink_backend="jsonl",
            audit_jsonl_path=str(path),
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get(
            "/api/v1/help/my-recent-denials", headers={"Authorization": "Bearer reader-key"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "jsonl"
    assert len(body["denials"]) == 1
    assert body["denials"][0]["connection"] == "demo"
    assert body["denials"][0]["reason"] == "policy"

    # Redaction: never the query shape or a predicate value, even for the
    # caller's own event.
    blob = resp.text
    assert "ssn" not in blob
    assert "query_shape" not in blob
    assert "customers" not in blob


@pytest.mark.asyncio
async def test_my_recent_denials_reads_hash_chained_ledger(tmp_path):
    """TODO.md item 136: the tamper-evident backend used to be refused at this
    surface's gate entirely (`source="disabled"`), even though the reader
    already understood the chain envelope — a capability the config alone
    should not have hidden."""
    from querygate.audit.ledger import GENESIS_PREV_HASH, make_record

    path = tmp_path / "audit.jsonl"
    now = datetime.now(timezone.utc)
    event = _event(at=now - timedelta(seconds=60), principal="reader", connection="demo")
    record = make_record(0, GENESIS_PREV_HASH, event.model_dump(mode="json", exclude_none=True))
    path.write_text(record.model_dump_json() + "\n")
    app = create_app(
        _settings(
            api_keys=["reader-key"],
            api_key_subject="reader",
            audit_sink_backend="jsonl_chained",
            audit_jsonl_path=str(path),
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get(
            "/api/v1/help/my-recent-denials", headers={"Authorization": "Bearer reader-key"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "jsonl_chained"
    assert len(body["denials"]) == 1
    assert body["denials"][0]["connection"] == "demo"


@pytest.mark.asyncio
async def test_my_recent_denials_never_leaks_another_principals_denial(tmp_path):
    """The security-critical property: two principals (distinguished by JWT
    `sub`, since a single API-key list maps to one shared subject — same
    pattern as `test_my_access_reports_per_principal_guardrails_and_claim_readiness`
    in test_product_guide_api.py) must never see each other's denials through
    this endpoint."""
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    def _token(subject: str) -> str:
        return jwt.encode(
            {"sub": subject, "iss": "https://idp.example.com/", "aud": "querygate"},
            private_key,
            algorithm="RS256",
        )

    now = datetime.now(timezone.utc)
    path = tmp_path / "audit.jsonl"
    _write_events(
        path,
        [
            _event(at=now - timedelta(seconds=60), principal="broad-agent", connection="c-broad"),
            _event(at=now - timedelta(seconds=30), principal="narrow-agent", connection="c-narrow"),
        ],
    )
    app = create_app(
        _settings(
            jwt_enabled=True,
            jwt_jwks_url="https://idp.example.com/.well-known/jwks.json",
            jwt_issuer="https://idp.example.com/",
            jwt_audience="querygate",
            audit_sink_backend="jsonl",
            audit_jsonl_path=str(path),
        )
    )
    with patch(
        "jwt.PyJWKClient.get_signing_key_from_jwt",
        lambda self, tok: type("K", (), {"key": public_key})(),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            broad = await client.get(
                "/api/v1/help/my-recent-denials",
                headers={"Authorization": f"Bearer {_token('broad-agent')}"},
            )
            narrow = await client.get(
                "/api/v1/help/my-recent-denials",
                headers={"Authorization": f"Bearer {_token('narrow-agent')}"},
            )

    assert broad.status_code == 200
    assert narrow.status_code == 200
    broad_body = broad.json()
    narrow_body = narrow.json()

    assert [d["connection"] for d in broad_body["denials"]] == ["c-broad"]
    assert [d["connection"] for d in narrow_body["denials"]] == ["c-narrow"]

    # Belt and suspenders: the other principal's connection id never appears
    # anywhere in either response body.
    assert "c-narrow" not in broad.text
    assert "narrow-agent" not in broad.text
    assert "c-broad" not in narrow.text
    assert "broad-agent" not in narrow.text


@pytest.mark.asyncio
async def test_my_recent_denials_respects_configured_limit(tmp_path):
    path = tmp_path / "audit.jsonl"
    now = datetime.now(timezone.utc)
    _write_events(
        path,
        [_event(at=now - timedelta(seconds=i), principal="reader") for i in range(10)],
    )
    app = create_app(
        _settings(
            api_keys=["reader-key"],
            api_key_subject="reader",
            audit_sink_backend="jsonl",
            audit_jsonl_path=str(path),
            personal_denials_limit=2,
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get(
            "/api/v1/help/my-recent-denials", headers={"Authorization": "Bearer reader-key"}
        )
    assert resp.status_code == 200
    assert len(resp.json()["denials"]) == 2


@pytest.mark.asyncio
async def test_my_recent_denials_respects_configured_max_lines_read(tmp_path):
    # TODO.md item 138 (security-review follow-up): personal_denials_max_lines_read
    # must be independently operator-configurable and actually reach the
    # reader, not silently stuck at AnomalyThresholds' own class default —
    # this is the one surface reachable with authentication only, no admin
    # scope, so an operator needs to be able to tighten it on its own.
    path = tmp_path / "audit.jsonl"
    now = datetime.now(timezone.utc)
    _write_events(
        path,
        [_event(at=now - timedelta(seconds=1000), principal="reader")]
        + [_event(at=now - timedelta(seconds=i), principal="someone-else") for i in range(50)],
    )
    app = create_app(
        _settings(
            api_keys=["reader-key"],
            api_key_subject="reader",
            audit_sink_backend="jsonl",
            audit_jsonl_path=str(path),
            personal_denials_max_lines_read=5,
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get(
            "/api/v1/help/my-recent-denials", headers={"Authorization": "Bearer reader-key"}
        )
    assert resp.status_code == 200
    body = resp.json()
    # The caller's own denial sits behind 50 newer events belonging to
    # another principal; a line-read cap of 5 stops well before reaching it.
    assert body["denials"] == []
    assert body["truncated"] is True


@pytest.mark.asyncio
async def test_my_recent_denials_respects_configured_max_consecutive_out_of_window(tmp_path):
    # TODO.md item 141 (security-review follow-up, docs/THREAT_MODEL.md
    # QG-43): personal_denials_max_consecutive_out_of_window must be
    # independently operator-configurable and actually reach the reader
    # through this route, the same requirement item 138 established for
    # max_lines_read above (`tests/unit/test_personal_denials.py`'s
    # `test_report_respects_configured_max_consecutive_out_of_window` pins
    # the same behavior one layer down, directly against
    # `build_recent_denials_report`; this test pins the REST route's own
    # config wiring into that function, which nothing else here exercises).
    # Unlike max_lines_read, crossing this cap does NOT set `truncated=True`
    # — TODO.md item 171 tracks giving this surface its own disclosure.
    path = tmp_path / "audit.jsonl"
    now = datetime.now(timezone.utc)
    # Physically first (oldest write position) = the reader's own, in-window
    # denial; physically after it = a run of out-of-window events from
    # another principal. A tail-first scan reads the out-of-window run
    # first — with the cap set low enough to cross it, the scan stops
    # before ever reaching the reader's own denial.
    _write_events(
        path,
        [_event(at=now - timedelta(seconds=100), principal="reader")]
        + [_event(at=now - timedelta(seconds=50_000), principal="someone-else") for _ in range(3)],
    )
    app = create_app(
        _settings(
            api_keys=["reader-key"],
            api_key_subject="reader",
            audit_sink_backend="jsonl",
            audit_jsonl_path=str(path),
            personal_denials_lookback_seconds=3600.0,
            personal_denials_max_consecutive_out_of_window=3,
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get(
            "/api/v1/help/my-recent-denials", headers={"Authorization": "Bearer reader-key"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["denials"] == []
    assert body["truncated"] is False
