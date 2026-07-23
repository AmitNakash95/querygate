"""In-query human-in-the-loop approval gate (TODO.md item 92, phase 1).

Covers the security-critical core (token issue/verify — forge, replay across a
different query, expiry, wrong key, malformed all fail closed), the trigger
logic, the service gate seam (`_enforce_approval_gate` raises without a token,
admits with a valid one, no-ops when disabled/within threshold), and the REST
flow (428 with fingerprint+reasons -> scope-gated approve -> re-submit passes).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient

from querygate.api.app import create_app
from querygate.core.config import AppConfig
from querygate.core.exceptions import ApprovalRequiredError
from querygate.execution import service as svc
from querygate.catalog.loader import CatalogStore, set_catalog_store
from querygate.catalog.models import SensitivityClass
from querygate.execution.approval import (
    approval_required_reasons,
    issue_approval_token,
    query_fingerprint,
    sensitivity_approval_reasons,
    verify_approval_token,
)
from querygate.execution.cost_estimation import QueryCostEstimate
from querygate.execution.service import StructuredQueryService
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.query_ast.models import StructuredQuery

_KEY = "test-approval-hmac-key"


# --------------------------------------------------------------------------- #
# Token security (the part that must be exactly right)                          #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_token_round_trips_for_the_same_fingerprint():
    token = issue_approval_token(fingerprint="fp1", approver_subject="approver", key=_KEY)
    assert verify_approval_token(token, fingerprint="fp1", key=_KEY) is True


@pytest.mark.unit
def test_token_is_rejected_for_a_different_query_fingerprint():
    # Replay against a DIFFERENT query must fail — an approval authorizes one
    # specific query, not a class of them.
    token = issue_approval_token(fingerprint="fp1", approver_subject="a", key=_KEY)
    assert verify_approval_token(token, fingerprint="fp2", key=_KEY) is False


@pytest.mark.unit
def test_token_is_rejected_under_a_different_key():
    token = issue_approval_token(fingerprint="fp1", approver_subject="a", key=_KEY)
    assert verify_approval_token(token, fingerprint="fp1", key="other-key") is False


@pytest.mark.unit
def test_forged_signature_is_rejected():
    token = issue_approval_token(fingerprint="fp1", approver_subject="a", key=_KEY)
    payload, _sig = token.rsplit(".", 1)
    forged = f"{payload}.{'0' * 64}"
    assert verify_approval_token(forged, fingerprint="fp1", key=_KEY) is False


@pytest.mark.unit
def test_expired_token_is_rejected():
    token = issue_approval_token(fingerprint="fp1", approver_subject="a", key=_KEY, ttl_seconds=-1)
    assert verify_approval_token(token, fingerprint="fp1", key=_KEY) is False


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["", "not-a-token", "a.b.c", "@@@.###"])
def test_malformed_tokens_fail_closed(bad):
    assert verify_approval_token(bad, fingerprint="fp1", key=_KEY) is False


@pytest.mark.unit
def test_verification_without_a_key_fails_closed():
    token = issue_approval_token(fingerprint="fp1", approver_subject="a", key=_KEY)
    assert verify_approval_token(token, fingerprint="fp1", key="") is False


@pytest.mark.unit
def test_issuing_without_a_key_raises():
    with pytest.raises(ValueError):
        issue_approval_token(fingerprint="fp1", approver_subject="a", key="")


@pytest.mark.unit
def test_fingerprint_is_stable_and_value_sensitive():
    q1 = StructuredQuery(from_table="orders", select=["orders.id"], limit=10)
    q2 = StructuredQuery(from_table="orders", select=["orders.id"], limit=10)
    q3 = StructuredQuery(from_table="orders", select=["orders.id"], limit=11)
    assert query_fingerprint(q1) == query_fingerprint(q2)
    assert query_fingerprint(q1) != query_fingerprint(q3)


# --------------------------------------------------------------------------- #
# Trigger logic                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_reasons_trigger_only_past_the_threshold():
    policy = Policy(approval_max_estimated_rows=1000)
    under = QueryCostEstimate(estimated_rows=1000, estimated_total_cost=None)
    over = QueryCostEstimate(estimated_rows=1001, estimated_total_cost=None)
    assert approval_required_reasons(under, policy) == []
    assert len(approval_required_reasons(over, policy)) == 1


@pytest.mark.unit
def test_no_reasons_when_gate_disabled():
    policy = Policy()  # neither approval threshold set
    est = QueryCostEstimate(estimated_rows=10_000_000, estimated_total_cost=9e9)
    assert approval_required_reasons(est, policy) == []
    assert policy.approval_gate_enabled is False


# --------------------------------------------------------------------------- #
# Sensitivity-label trigger (phase 2)                                          #
# --------------------------------------------------------------------------- #


def _catalog_with_pii():
    set_catalog_store(
        CatalogStore.from_dict(
            {
                "connections": {
                    "demo": {
                        "tables": {
                            "customers": {
                                "provenance": {"created_by": "admin"},
                                "columns": {
                                    "email": {"sensitivity": "pii"},
                                    "id": {"sensitivity": "none"},
                                },
                            },
                            "audit_log": {
                                "sensitivity": "confidential",
                                "provenance": {"created_by": "admin"},
                                "columns": {"note": {}},
                            },
                        }
                    }
                }
            }
        )
    )


@pytest.mark.unit
def test_sensitivity_reasons_trigger_on_a_labelled_column():
    _catalog_with_pii()
    policy = Policy(approval_sensitivities=[SensitivityClass.PII])
    q = StructuredQuery(from_table="customers", select=["customers.email"])
    reasons = sensitivity_approval_reasons(q, policy, "demo")
    assert len(reasons) == 1 and "customers.email" in reasons[0]


@pytest.mark.unit
def test_sensitivity_reasons_empty_for_non_sensitive_columns():
    _catalog_with_pii()
    policy = Policy(approval_sensitivities=[SensitivityClass.PII])
    q = StructuredQuery(from_table="customers", select=["customers.id"])
    assert sensitivity_approval_reasons(q, policy, "demo") == []


@pytest.mark.unit
def test_sensitivity_reasons_empty_when_trigger_unconfigured():
    _catalog_with_pii()
    q = StructuredQuery(from_table="customers", select=["customers.email"])
    assert sensitivity_approval_reasons(q, Policy(), "demo") == []


@pytest.mark.unit
def test_table_level_sensitivity_applies_to_unlabelled_columns():
    _catalog_with_pii()
    policy = Policy(approval_sensitivities=[SensitivityClass.CONFIDENTIAL])
    q = StructuredQuery(from_table="audit_log", select=["audit_log.note"])
    reasons = sensitivity_approval_reasons(q, policy, "demo")
    assert len(reasons) == 1 and "audit_log.note" in reasons[0]


@pytest.mark.unit
def test_sensitivity_trigger_fires_in_a_where_clause_too(monkeypatch):
    # A denied-value inference vector: filtering ON a sensitive column, not
    # selecting it, must also trip the gate (item 96 visitor covers WHERE).
    _catalog_with_pii()
    monkeypatch.setattr(svc.app_config, "approval_token_hmac_key", _KEY)
    from querygate.query_ast.models import Predicate

    policy = Policy(approval_sensitivities=[SensitivityClass.PII])
    q = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        where=Predicate(col="customers.email", op="eq", value="x@y.z"),
    )
    with pytest.raises(ApprovalRequiredError):
        _gate_service()._enforce_approval_gate(None, policy, q, None)


@pytest.mark.unit
def test_sensitivity_gate_admits_with_a_valid_token(monkeypatch):
    _catalog_with_pii()
    monkeypatch.setattr(svc.app_config, "approval_token_hmac_key", _KEY)
    policy = Policy(approval_sensitivities=[SensitivityClass.PII])
    q = StructuredQuery(from_table="customers", select=["customers.email"])
    token = issue_approval_token(fingerprint=query_fingerprint(q), approver_subject="a", key=_KEY)
    _gate_service()._enforce_approval_gate(None, policy, q, token)  # no raise


# --------------------------------------------------------------------------- #
# Service gate seam                                                             #
# --------------------------------------------------------------------------- #


def _gate_service() -> StructuredQueryService:
    return StructuredQueryService(connection_id="demo")


@pytest.mark.unit
def test_gate_raises_without_a_token(monkeypatch):
    monkeypatch.setattr(svc.app_config, "approval_token_hmac_key", _KEY)
    policy = Policy(approval_max_estimated_rows=100)
    query = StructuredQuery(from_table="orders", select=["orders.id"])
    estimate = QueryCostEstimate(estimated_rows=500, estimated_total_cost=None)
    with pytest.raises(ApprovalRequiredError) as ei:
        _gate_service()._enforce_approval_gate(estimate, policy, query, None)
    assert ei.value.fingerprint == query_fingerprint(query)
    assert ei.value.reasons


@pytest.mark.unit
def test_gate_admits_with_a_valid_token(monkeypatch):
    monkeypatch.setattr(svc.app_config, "approval_token_hmac_key", _KEY)
    policy = Policy(approval_max_estimated_rows=100)
    query = StructuredQuery(from_table="orders", select=["orders.id"])
    estimate = QueryCostEstimate(estimated_rows=500, estimated_total_cost=None)
    token = issue_approval_token(
        fingerprint=query_fingerprint(query), approver_subject="approver", key=_KEY
    )
    # Should not raise.
    _gate_service()._enforce_approval_gate(estimate, policy, query, token)


@pytest.mark.unit
def test_gate_noop_when_within_threshold(monkeypatch):
    monkeypatch.setattr(svc.app_config, "approval_token_hmac_key", _KEY)
    policy = Policy(approval_max_estimated_rows=100)
    query = StructuredQuery(from_table="orders", select=["orders.id"])
    estimate = QueryCostEstimate(estimated_rows=50, estimated_total_cost=None)
    _gate_service()._enforce_approval_gate(estimate, policy, query, None)  # no raise


@pytest.mark.unit
def test_gate_admits_a_token_for_a_different_query_never(monkeypatch):
    monkeypatch.setattr(svc.app_config, "approval_token_hmac_key", _KEY)
    policy = Policy(approval_max_estimated_rows=100)
    approved = StructuredQuery(from_table="orders", select=["orders.id"])
    other = StructuredQuery(from_table="orders", select=["orders.total"])
    estimate = QueryCostEstimate(estimated_rows=500, estimated_total_cost=None)
    token = issue_approval_token(
        fingerprint=query_fingerprint(approved), approver_subject="a", key=_KEY
    )
    with pytest.raises(ApprovalRequiredError):
        _gate_service()._enforce_approval_gate(estimate, policy, other, token)


# --------------------------------------------------------------------------- #
# Full execute() gate + REST flow                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_execute_pauses_then_admits_after_approval(monkeypatch):
    monkeypatch.setattr(svc.app_config, "approval_token_hmac_key", _KEY)
    set_policy_store(PolicyStore(default=Policy(approval_max_estimated_rows=100), overrides={}))
    table = sa.Table("orders", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True))
    query = StructuredQuery(from_table="orders", select=["orders.id"], limit=10)

    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = [{"id": 1}]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    estimate = QueryCostEstimate(estimated_rows=500, estimated_total_cost=None)
    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"orders": table})),
        patch.object(svc, "session_scope", _scope),
        patch.object(svc, "estimate_postgres_query_cost", AsyncMock(return_value=estimate)),
    ):
        service = StructuredQueryService(connection_id="demo")
        with pytest.raises(ApprovalRequiredError):
            await service.execute(query)

        token = issue_approval_token(
            fingerprint=query_fingerprint(query), approver_subject="a", key=_KEY
        )
        result = await service.execute(query, approval_token=token)
    assert result.row_count == 1


@pytest.mark.asyncio
async def test_execute_many_admits_only_the_query_its_token_matches(monkeypatch):
    # A batch where every query trips the gate: only the query whose fingerprint
    # is in approval_tokens runs; the others stay fail-closed as per-item errors.
    monkeypatch.setattr(svc.app_config, "approval_token_hmac_key", _KEY)
    set_policy_store(PolicyStore(default=Policy(approval_max_estimated_rows=100), overrides={}))
    table = sa.Table("orders", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True))
    approved = StructuredQuery(from_table="orders", select=["orders.id"], limit=10)
    other = StructuredQuery(from_table="orders", select=["orders.id"], limit=20)

    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = [{"id": 1}]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    estimate = QueryCostEstimate(estimated_rows=500, estimated_total_cost=None)
    token = issue_approval_token(
        fingerprint=query_fingerprint(approved), approver_subject="a", key=_KEY
    )
    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"orders": table})),
        patch.object(svc, "session_scope", _scope),
        patch.object(svc, "estimate_postgres_query_cost", AsyncMock(return_value=estimate)),
    ):
        service = StructuredQueryService(connection_id="demo")
        results = await service.execute_many(
            [approved, other],
            approval_tokens={query_fingerprint(approved): token},
        )
    assert results[0].row_count == 1 and results[0].error is None
    # The token is bound to `approved`'s fingerprint, so `other` — with a
    # different fingerprint and no token of its own — stays gated.
    assert results[1].row_count is None and results[1].error is not None


@pytest.mark.asyncio
async def test_execute_many_rejects_a_token_replayed_onto_another_query(monkeypatch):
    # A token minted for one query must not admit a different query in the batch,
    # even though the map lookup would never hand it over — assert the per-query
    # fingerprint binding in execute() is the real guard, not the map key.
    monkeypatch.setattr(svc.app_config, "approval_token_hmac_key", _KEY)
    set_policy_store(PolicyStore(default=Policy(approval_max_estimated_rows=100), overrides={}))
    table = sa.Table("orders", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True))
    approved = StructuredQuery(from_table="orders", select=["orders.id"], limit=10)
    other = StructuredQuery(from_table="orders", select=["orders.id"], limit=20)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield AsyncMock()

    estimate = QueryCostEstimate(estimated_rows=500, estimated_total_cost=None)
    token = issue_approval_token(
        fingerprint=query_fingerprint(approved), approver_subject="a", key=_KEY
    )
    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"orders": table})),
        patch.object(svc, "session_scope", _scope),
        patch.object(svc, "estimate_postgres_query_cost", AsyncMock(return_value=estimate)),
    ):
        service = StructuredQueryService(connection_id="demo")
        # Deliberately hand `other`'s fingerprint the approved query's token.
        results = await service.execute_many(
            [other], approval_tokens={query_fingerprint(other): token}
        )
    assert results[0].error is not None


_KEY_APIKEY = "approver-key"


def _rest_app(scopes) -> AppConfig:
    return AppConfig(
        environment="localhost",
        mcp_enabled=False,
        audit_sink_backend="none",
        api_keys=[_KEY_APIKEY],
        api_key_scopes=list(scopes),
    )


@pytest.mark.asyncio
async def test_approve_endpoint_requires_scope(monkeypatch):
    monkeypatch.setattr(svc.app_config, "approval_token_hmac_key", _KEY)
    app = create_app(_rest_app(scopes=()))  # no query:approve scope
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        resp = await client.post(
            "/api/v1/demo/query/approve",
            headers={"Authorization": f"Bearer {_KEY_APIKEY}"},
            json={"from_table": "orders", "select": ["orders.id"]},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_approve_endpoint_503_when_key_unset(monkeypatch):
    monkeypatch.setattr(svc.app_config, "approval_token_hmac_key", "")
    app = create_app(_rest_app(scopes=("query:approve",)))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        resp = await client.post(
            "/api/v1/demo/query/approve",
            headers={"Authorization": f"Bearer {_KEY_APIKEY}"},
            json={"from_table": "orders", "select": ["orders.id"]},
        )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_approve_endpoint_issues_verifiable_token(monkeypatch):
    monkeypatch.setattr(svc.app_config, "approval_token_hmac_key", _KEY)
    app = create_app(_rest_app(scopes=("query:approve",)))
    transport = ASGITransport(app=app)
    query = {"from_table": "orders", "select": ["orders.id"]}
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        resp = await client.post(
            "/api/v1/demo/query/approve",
            headers={"Authorization": f"Bearer {_KEY_APIKEY}"},
            json=query,
        )
    assert resp.status_code == 200
    body = resp.json()
    expected_fp = query_fingerprint(StructuredQuery(**query))
    assert body["fingerprint"] == expected_fp
    assert verify_approval_token(body["approval_token"], fingerprint=expected_fp, key=_KEY)
