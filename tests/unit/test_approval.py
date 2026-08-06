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
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.core.auth import Principal
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
    write_fingerprint,
)
from querygate.execution.cost_estimation import QueryCostEstimate
from querygate.execution.service import StructuredQueryService
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.query_ast.models import JoinSpec, StructuredQuery
from querygate.validation.schema_validation import validate_schema
from querygate.write_ast.models import DeleteStatement

_KEY = "test-approval-hmac-key"


# --------------------------------------------------------------------------- #
# Token security (the part that must be exactly right)                          #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_token_round_trips_for_the_same_fingerprint():
    token = issue_approval_token(
        fingerprint="fp1",
        approver_subject="approver",
        key=_KEY,
        connection_id=None,
        principal_subject=None,
    )
    assert (
        verify_approval_token(
            token, fingerprint="fp1", key=_KEY, connection_id=None, principal_subject=None
        )
        is True
    )


@pytest.mark.unit
def test_token_is_rejected_for_a_different_query_fingerprint():
    # Replay against a DIFFERENT query must fail — an approval authorizes one
    # specific query, not a class of them.
    token = issue_approval_token(
        fingerprint="fp1",
        approver_subject="a",
        key=_KEY,
        connection_id=None,
        principal_subject=None,
    )
    assert (
        verify_approval_token(
            token, fingerprint="fp2", key=_KEY, connection_id=None, principal_subject=None
        )
        is False
    )


@pytest.mark.unit
def test_token_is_rejected_under_a_different_key():
    token = issue_approval_token(
        fingerprint="fp1",
        approver_subject="a",
        key=_KEY,
        connection_id=None,
        principal_subject=None,
    )
    assert (
        verify_approval_token(
            token, fingerprint="fp1", key="other-key", connection_id=None, principal_subject=None
        )
        is False
    )


@pytest.mark.unit
def test_forged_signature_is_rejected():
    token = issue_approval_token(
        fingerprint="fp1",
        approver_subject="a",
        key=_KEY,
        connection_id=None,
        principal_subject=None,
    )
    payload, _sig = token.rsplit(".", 1)
    forged = f"{payload}.{'0' * 64}"
    assert (
        verify_approval_token(
            forged, fingerprint="fp1", key=_KEY, connection_id=None, principal_subject=None
        )
        is False
    )


@pytest.mark.unit
def test_expired_token_is_rejected():
    token = issue_approval_token(
        fingerprint="fp1",
        approver_subject="a",
        key=_KEY,
        ttl_seconds=-1,
        connection_id=None,
        principal_subject=None,
    )
    assert (
        verify_approval_token(
            token, fingerprint="fp1", key=_KEY, connection_id=None, principal_subject=None
        )
        is False
    )


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["", "not-a-token", "a.b.c", "@@@.###"])
def test_malformed_tokens_fail_closed(bad):
    assert (
        verify_approval_token(
            bad, fingerprint="fp1", key=_KEY, connection_id=None, principal_subject=None
        )
        is False
    )


@pytest.mark.unit
def test_verification_without_a_key_fails_closed():
    token = issue_approval_token(
        fingerprint="fp1",
        approver_subject="a",
        key=_KEY,
        connection_id=None,
        principal_subject=None,
    )
    assert (
        verify_approval_token(
            token, fingerprint="fp1", key="", connection_id=None, principal_subject=None
        )
        is False
    )


@pytest.mark.unit
def test_issuing_without_a_key_raises():
    with pytest.raises(ValueError):
        issue_approval_token(
            fingerprint="fp1",
            approver_subject="a",
            key="",
            connection_id=None,
            principal_subject=None,
        )


# --------------------------------------------------------------------------- #
# Token kind + format version (TODO.md item 151, F1/F2)                        #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_pending_kind_token_is_rejected_where_a_grant_is_expected():
    # A pending elicitation token carries a genuine fingerprint/expiry, but is
    # never itself redeemable — only a "grant"-kind token verifies where the
    # gate expects one (the default `expected_kind`).
    token = issue_approval_token(
        fingerprint="fp1",
        approver_subject="a",
        key=_KEY,
        connection_id=None,
        principal_subject=None,
        kind="pending",
    )
    assert (
        verify_approval_token(
            token, fingerprint="fp1", key=_KEY, connection_id=None, principal_subject=None
        )
        is False
    )
    assert (
        verify_approval_token(
            token,
            fingerprint="fp1",
            key=_KEY,
            connection_id=None,
            principal_subject=None,
            expected_kind="pending",
        )
        is True
    )


@pytest.mark.unit
def test_grant_kind_token_is_rejected_where_a_pending_marker_is_expected():
    # The reverse direction: a real grant must not satisfy a check that
    # specifically wants a pending marker (defense in depth for the MCP
    # elicitation resolver's own pending-state verification).
    token = issue_approval_token(
        fingerprint="fp1",
        approver_subject="a",
        key=_KEY,
        connection_id=None,
        principal_subject=None,
    )
    assert (
        verify_approval_token(
            token,
            fingerprint="fp1",
            key=_KEY,
            connection_id=None,
            principal_subject=None,
            expected_kind="pending",
        )
        is False
    )


@pytest.mark.unit
def test_token_missing_the_format_version_claim_is_rejected():
    # Simulates a token minted by a pod running a pre-item-151 build mid
    # rolling-deploy: a payload lacking "v" entirely (the pre-item-151 shape
    # for the WHOLE claim, not just cx/sub_bind) must be rejected outright by
    # a pod that understands "v", not silently treated as unbound-and-
    # therefore-permissive.
    import base64
    import hashlib
    import hmac
    import json
    import time

    payload = {"fp": "fp1", "sub": "a", "k": "grant", "exp": int(time.time() + 300)}
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(_KEY.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    encoded = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
    token = f"{encoded}.{signature}"
    assert (
        verify_approval_token(
            token, fingerprint="fp1", key=_KEY, connection_id=None, principal_subject=None
        )
        is False
    )


# --------------------------------------------------------------------------- #
# Connection/principal binding (TODO.md item 151)                              #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_token_bound_to_a_connection_is_rejected_for_a_different_connection():
    # The item's exact motivating scenario: the byte-identical fingerprint,
    # minted for "staging", must not verify against "prod".
    token = issue_approval_token(
        fingerprint="fp1",
        approver_subject="a",
        key=_KEY,
        connection_id="staging",
        principal_subject=None,
    )
    assert (
        verify_approval_token(
            token, fingerprint="fp1", key=_KEY, connection_id="prod", principal_subject=None
        )
        is False
    )
    assert (
        verify_approval_token(
            token, fingerprint="fp1", key=_KEY, connection_id="staging", principal_subject=None
        )
        is True
    )


@pytest.mark.unit
def test_token_bound_to_a_connection_is_rejected_when_connection_omitted_at_verify():
    # Omitting the binding at verification time must not be treated as "no
    # opinion" and pass — a bound token demands a matching value, not silence.
    token = issue_approval_token(
        fingerprint="fp1",
        approver_subject="a",
        key=_KEY,
        connection_id="staging",
        principal_subject=None,
    )
    assert (
        verify_approval_token(
            token, fingerprint="fp1", key=_KEY, connection_id=None, principal_subject=None
        )
        is False
    )


@pytest.mark.unit
def test_token_bound_to_a_principal_is_rejected_for_a_different_principal():
    token = issue_approval_token(
        fingerprint="fp1",
        approver_subject="a",
        key=_KEY,
        principal_subject="alice",
        connection_id=None,
    )
    assert (
        verify_approval_token(
            token, fingerprint="fp1", key=_KEY, principal_subject="bob", connection_id=None
        )
        is False
    )
    assert (
        verify_approval_token(
            token, fingerprint="fp1", key=_KEY, principal_subject="alice", connection_id=None
        )
        is True
    )


@pytest.mark.unit
def test_token_bound_to_a_principal_is_rejected_when_principal_omitted_at_verify():
    token = issue_approval_token(
        fingerprint="fp1",
        approver_subject="a",
        key=_KEY,
        principal_subject="alice",
        connection_id=None,
    )
    assert (
        verify_approval_token(
            token, fingerprint="fp1", key=_KEY, connection_id=None, principal_subject=None
        )
        is False
    )


@pytest.mark.unit
def test_unbound_token_verifies_regardless_of_connection_or_principal_supplied():
    # A token minted without connection_id/principal_subject (the pre-item-151
    # shape) carries no cx/sub_bind claim, so it verifies on fingerprint/expiry
    # alone — unchanged behavior for callers that don't opt into binding.
    token = issue_approval_token(
        fingerprint="fp1",
        approver_subject="a",
        key=_KEY,
        connection_id=None,
        principal_subject=None,
    )
    assert (
        verify_approval_token(
            token, fingerprint="fp1", key=_KEY, connection_id="anything", principal_subject="anyone"
        )
        is True
    )


@pytest.mark.unit
def test_token_bound_to_both_connection_and_principal_requires_both_to_match():
    token = issue_approval_token(
        fingerprint="fp1",
        approver_subject="a",
        key=_KEY,
        connection_id="staging",
        principal_subject="alice",
    )
    # Right connection, wrong principal.
    assert (
        verify_approval_token(
            token,
            fingerprint="fp1",
            key=_KEY,
            connection_id="staging",
            principal_subject="bob",
        )
        is False
    )
    # Right principal, wrong connection.
    assert (
        verify_approval_token(
            token,
            fingerprint="fp1",
            key=_KEY,
            connection_id="prod",
            principal_subject="alice",
        )
        is False
    )
    # Both right.
    assert (
        verify_approval_token(
            token,
            fingerprint="fp1",
            key=_KEY,
            connection_id="staging",
            principal_subject="alice",
        )
        is True
    )


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
    token = issue_approval_token(
        fingerprint=query_fingerprint(q),
        approver_subject="a",
        key=_KEY,
        connection_id=None,
        principal_subject=None,
    )
    _gate_service()._enforce_approval_gate(None, policy, q, token)  # no raise


# --------------------------------------------------------------------------- #
# Cross-connection join sensitivity trigger (TODO.md item 155)                #
# --------------------------------------------------------------------------- #


def _cross_connection_setup(monkeypatch) -> StructuredQuery:
    """Two connections in the same join_group: `orders` lives on `primary`,
    `customers` lives on `other` (joined via `JoinSpec.connection`). Mirrors
    `test_schema_validation.py::TestCrossConnectionJoins._two_connections` —
    this is exactly the shape `resolve_query_table_connections` (schema
    validation, already shipped) enforces the `join_group` rule against.
    `customers.email` is labelled `pii` ONLY in `other`'s catalog; `primary`'s
    catalog has no `customers` entry at all, so a lookup that (incorrectly)
    used `primary` for every table would find nothing."""
    primary = ConnectionProfile(
        id="primary",
        dialect="postgresql",
        connection_string="postgresql+asyncpg://user:pass@host/primary_db",
        join_group="shared",
    )
    other = ConnectionProfile(
        id="other",
        dialect="postgresql",
        connection_string="postgresql+asyncpg://user:pass@host/other_db",
        join_group="shared",
    )
    set_registry(ConnectionRegistry({"primary": primary, "other": other}))
    set_policy_store(PolicyStore(default=Policy(), overrides={}))
    set_catalog_store(
        CatalogStore.from_dict(
            {
                "connections": {
                    "other": {
                        "tables": {
                            "customers": {
                                "provenance": {"created_by": "admin"},
                                "columns": {"email": {"sensitivity": "pii"}},
                            }
                        }
                    }
                }
            }
        )
    )

    tables = {
        "orders": sa.Table(
            "orders",
            sa.MetaData(),
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("customer_id", sa.Integer),
        ),
        "customers": sa.Table(
            "customers",
            sa.MetaData(),
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("email", sa.String(100)),
        ),
    }

    async def fake_load_table(connection_id, table_name, table_connection):
        return tables[table_name]

    monkeypatch.setattr("querygate.validation.schema_validation._load_table", fake_load_table)

    return StructuredQuery(
        from_table="orders",
        select=["orders.id", "customers.email"],
        joins=[
            JoinSpec(
                table="customers",
                on=["orders.customer_id", "customers.id"],
                connection="other",
            )
        ],
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cross_connection_join_trips_sensitivity_gate_when_scope_connections_threaded(
    monkeypatch,
):
    """TODO.md item 155's exact scenario, using the REAL schema-validation
    output (not a hand-built map): `resolve_query_table_connections` resolves
    `customers` to connection `other`, `validate_schema` threads that into
    `scope_connections`, and `sensitivity_approval_reasons` must use it to find
    `customers.email`'s `pii` label in `other`'s catalog rather than looking
    (and finding nothing) in `primary`'s."""
    query = _cross_connection_setup(monkeypatch)
    policy = Policy(approval_sensitivities=[SensitivityClass.PII])

    scope_connections: dict = {}
    await validate_schema(query, connection_id="primary", scope_connections=scope_connections)

    reasons = sensitivity_approval_reasons(query, policy, "primary", scope_connections)
    assert len(reasons) == 1 and "customers.email" in reasons[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cross_connection_join_trips_the_gate_through_an_aliased_mixed_case_ref(
    monkeypatch,
):
    """test-contract-reviewer, 2026-08-06, on this same item: every one of the
    other cross-connection tests joins `customers` with NO alias, so `table`
    (the raw token in a column ref) and `physical` (the reflected table name)
    are identical strings throughout — a mutation swapping the lookup key in
    `sensitivity_approval_reasons` from `table.casefold()` to
    `physical.casefold()` would leave every one of them green, and so would
    dropping the `.casefold()` calls where `scope_connections` is populated in
    `validate_schema`, since every name in those tests is already lowercase.
    This test joins `customers` under alias `Cust` and references it in
    mixed case (`Cust.email`, `cust.id`) — since `resolve_query_table_
    connections`'s map is keyed by alias-or-table (never physical name), only
    the alias-keyed, case-folded lookup path can find the label here."""
    primary = ConnectionProfile(
        id="primary",
        dialect="postgresql",
        connection_string="postgresql+asyncpg://user:pass@host/primary_db",
        join_group="shared",
    )
    other = ConnectionProfile(
        id="other",
        dialect="postgresql",
        connection_string="postgresql+asyncpg://user:pass@host/other_db",
        join_group="shared",
    )
    set_registry(ConnectionRegistry({"primary": primary, "other": other}))
    set_policy_store(PolicyStore(default=Policy(), overrides={}))
    set_catalog_store(
        CatalogStore.from_dict(
            {
                "connections": {
                    "other": {
                        "tables": {
                            "customers": {
                                "provenance": {"created_by": "admin"},
                                "columns": {"email": {"sensitivity": "pii"}},
                            }
                        }
                    }
                }
            }
        )
    )
    tables = {
        "orders": sa.Table(
            "orders",
            sa.MetaData(),
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("customer_id", sa.Integer),
        ),
        "customers": sa.Table(
            "customers",
            sa.MetaData(),
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("email", sa.String(100)),
        ),
    }

    async def fake_load_table(connection_id, table_name, table_connection):
        return tables[table_name]

    monkeypatch.setattr("querygate.validation.schema_validation._load_table", fake_load_table)

    query = StructuredQuery(
        from_table="orders",
        select=["orders.id", "Cust.email"],
        joins=[
            JoinSpec(
                table="customers",
                alias="Cust",
                on=["orders.customer_id", "cust.id"],
                connection="other",
            )
        ],
    )
    policy = Policy(approval_sensitivities=[SensitivityClass.PII])

    scope_connections: dict = {}
    await validate_schema(query, connection_id="primary", scope_connections=scope_connections)

    reasons = sensitivity_approval_reasons(query, policy, "primary", scope_connections)
    assert len(reasons) == 1 and "customers.email" in reasons[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cross_connection_join_still_trips_when_the_label_lives_only_on_the_primary_side(
    monkeypatch,
):
    """security-invariant-reviewer, 2026-08-06, on this same item: the fix must
    not merely REPLACE the top-level connection's catalog with the joined
    connection's — that would silently stop catching a label that lives only
    on the PRIMARY side once a same-named table is joined in from elsewhere.
    Catalogs are curated independently per connection (item 32), so an
    operator may label `customers.email` under `primary` and never get around
    to duplicating that label under `other`. This is the mirror image of
    `_cross_connection_setup` (which labels only `other`): here the label
    lives ONLY on `primary`'s own `customers` catalog entry, and the query
    still joins `customers` from `other` — the gate must still fire by
    consulting `primary` too, not just the table's resolved connection."""
    primary = ConnectionProfile(
        id="primary",
        dialect="postgresql",
        connection_string="postgresql+asyncpg://user:pass@host/primary_db",
        join_group="shared",
    )
    other = ConnectionProfile(
        id="other",
        dialect="postgresql",
        connection_string="postgresql+asyncpg://user:pass@host/other_db",
        join_group="shared",
    )
    set_registry(ConnectionRegistry({"primary": primary, "other": other}))
    set_policy_store(PolicyStore(default=Policy(), overrides={}))
    set_catalog_store(
        CatalogStore.from_dict(
            {
                "connections": {
                    "primary": {
                        "tables": {
                            "customers": {
                                "provenance": {"created_by": "admin"},
                                "columns": {"email": {"sensitivity": "pii"}},
                            }
                        }
                    }
                }
            }
        )
    )
    tables = {
        "orders": sa.Table(
            "orders",
            sa.MetaData(),
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("customer_id", sa.Integer),
        ),
        "customers": sa.Table(
            "customers",
            sa.MetaData(),
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("email", sa.String(100)),
        ),
    }

    async def fake_load_table(connection_id, table_name, table_connection):
        return tables[table_name]

    monkeypatch.setattr("querygate.validation.schema_validation._load_table", fake_load_table)

    query = StructuredQuery(
        from_table="orders",
        select=["orders.id", "customers.email"],
        joins=[
            JoinSpec(
                table="customers",
                on=["orders.customer_id", "customers.id"],
                connection="other",
            )
        ],
    )
    policy = Policy(approval_sensitivities=[SensitivityClass.PII])

    scope_connections: dict = {}
    await validate_schema(query, connection_id="primary", scope_connections=scope_connections)

    reasons = sensitivity_approval_reasons(query, policy, "primary", scope_connections)
    assert len(reasons) == 1 and "customers.email" in reasons[0]
    # architecture-boundary-reviewer, 2026-08-06, on this same item: since the
    # label came from `primary`'s own catalog entry rather than the table's
    # resolved connection (`other`), the reason string must say so — otherwise
    # an approver reading "references pii-labelled column customers.email"
    # could reasonably assume the label describes the physical table the query
    # actually reads (`other`'s), when it names an unrelated same-named entry.
    assert "connection 'primary'" in reasons[0]


@pytest.mark.unit
def test_cross_connection_join_never_trips_the_gate_without_the_map():
    """Pins the exact pre-155 bug as a permanent regression: omitting the
    scope_connections map — the shape of every call site before this item, and
    still the default for any caller that doesn't pass one — resolves every
    table against the query's own top-level connection, so a label that lives
    only on the JOINED connection's catalog is never found and the gate never
    fires."""
    set_catalog_store(
        CatalogStore.from_dict(
            {
                "connections": {
                    "other": {
                        "tables": {
                            "customers": {
                                "provenance": {"created_by": "admin"},
                                "columns": {"email": {"sensitivity": "pii"}},
                            }
                        }
                    }
                }
            }
        )
    )
    policy = Policy(approval_sensitivities=[SensitivityClass.PII])
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id", "customers.email"],
        joins=[
            JoinSpec(
                table="customers",
                on=["orders.customer_id", "customers.id"],
                connection="other",
            )
        ],
    )
    assert sensitivity_approval_reasons(query, policy, "primary") == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enforce_approval_gate_uses_the_real_cross_connection_map(monkeypatch):
    """Service-level wiring, not just the two functions cooperating in
    isolation: `_validate_and_compile`'s real `scope_connections` output must
    reach `_enforce_approval_gate`, which must forward it to
    `sensitivity_approval_reasons`. Mirrors
    `test_sensitivity_trigger_fires_in_a_where_clause_too`'s direct-gate-call
    pattern, using a real cross-connection `validate_schema` map instead of a
    single-connection one — and pins that omitting the map (the old call
    shape) does NOT raise, so the map argument is what changes the outcome."""
    query = _cross_connection_setup(monkeypatch)
    monkeypatch.setattr(svc.app_config, "approval_token_hmac_key", _KEY)
    policy = Policy(approval_sensitivities=[SensitivityClass.PII])

    scope_connections: dict = {}
    await validate_schema(query, connection_id="primary", scope_connections=scope_connections)

    service = StructuredQueryService(connection_id="primary")
    with pytest.raises(ApprovalRequiredError):
        service._enforce_approval_gate(None, policy, query, None, scope_connections)
    service._enforce_approval_gate(None, policy, query, None)  # no map -> no raise


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_trips_the_gate_for_a_cross_connection_join_end_to_end(monkeypatch):
    """The full production path — `execute()` -> `_validate_and_compile()`
    (real `validate_schema`, only `_load_table` patched) -> `_enforce_approval_
    gate()` — must raise for the cross-connection scenario, not just the two
    helper functions when driven by hand. This is the call site the two tests
    above cannot see: a wiring break at `execute()`'s own call to
    `_enforce_approval_gate` (e.g. dropping the `scope_connections` argument
    it now passes) is invisible to any test that calls `_enforce_approval_
    gate` directly, so this one goes through the real public entry point."""
    query = _cross_connection_setup(monkeypatch)
    monkeypatch.setattr(svc.app_config, "approval_token_hmac_key", _KEY)
    set_policy_store(
        PolicyStore(default=Policy(approval_sensitivities=[SensitivityClass.PII]), overrides={})
    )

    mock_engine = MagicMock()
    mock_engine.dialect.name = "postgresql"
    mock_session = AsyncMock()

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    with (
        patch.object(svc, "get_engine", return_value=mock_engine),
        patch.object(svc, "session_scope", _scope),
    ):
        service = StructuredQueryService(connection_id="primary")
        with pytest.raises(ApprovalRequiredError) as ei:
            await service.execute(query)
    assert any("customers.email" in reason for reason in ei.value.reasons)
    # The session was never used to run the statement — the gate stops
    # execution before the query touches the database.
    mock_session.execute.assert_not_awaited()


@pytest.mark.unit
def test_single_connection_sensitivity_gate_unaffected_by_scope_connections_param():
    """No-regression check: a single-connection query's existing behavior is
    unchanged whether `scope_connections` is omitted, `None`, or an explicit
    map that happens not to cover the referenced table — every table falls
    back to the top-level connection_id exactly as before item 155."""
    _catalog_with_pii()
    policy = Policy(approval_sensitivities=[SensitivityClass.PII])
    q = StructuredQuery(from_table="customers", select=["customers.email"])
    baseline = sensitivity_approval_reasons(q, policy, "demo")
    assert len(baseline) == 1 and "customers.email" in baseline[0]
    assert sensitivity_approval_reasons(q, policy, "demo", None) == baseline
    assert sensitivity_approval_reasons(q, policy, "demo", {}) == baseline
    assert sensitivity_approval_reasons(q, policy, "demo", {id(q): {}}) == baseline


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
        fingerprint=query_fingerprint(query),
        approver_subject="approver",
        key=_KEY,
        connection_id=None,
        principal_subject=None,
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
        fingerprint=query_fingerprint(approved),
        approver_subject="a",
        key=_KEY,
        connection_id=None,
        principal_subject=None,
    )
    with pytest.raises(ApprovalRequiredError):
        _gate_service()._enforce_approval_gate(estimate, policy, other, token)


@pytest.mark.unit
def test_gate_rejects_a_token_minted_for_a_different_connection(monkeypatch):
    """TODO.md item 151's exact scenario: a token approved for the
    byte-identical query on connection A must not admit it on connection B —
    e.g. `staging` never trips the gate, `prod` does; a `query:approve` holder
    approving what they believe is the `staging` query must not thereby
    approve it on `prod` too."""
    monkeypatch.setattr(svc.app_config, "approval_token_hmac_key", _KEY)
    policy = Policy(approval_max_estimated_rows=100)
    query = StructuredQuery(from_table="orders", select=["orders.id"])
    estimate = QueryCostEstimate(estimated_rows=500, estimated_total_cost=None)
    token = issue_approval_token(
        fingerprint=query_fingerprint(query),
        approver_subject="approver",
        key=_KEY,
        connection_id="staging",
        principal_subject=None,
    )
    service_on_prod = StructuredQueryService(connection_id="prod")
    with pytest.raises(ApprovalRequiredError):
        service_on_prod._enforce_approval_gate(estimate, policy, query, token)
    # ...but it does admit on the connection it was actually minted for.
    service_on_staging = StructuredQueryService(connection_id="staging")
    service_on_staging._enforce_approval_gate(estimate, policy, query, token)  # no raise


@pytest.mark.unit
def test_gate_rejects_a_token_minted_for_a_different_principal(monkeypatch):
    """The principal-binding sibling of the connection test above: a token
    bound to principal X at issue time must not admit principal Y's identical
    retry, even on the same connection with the same query."""
    monkeypatch.setattr(svc.app_config, "approval_token_hmac_key", _KEY)
    policy = Policy(approval_max_estimated_rows=100)
    query = StructuredQuery(from_table="orders", select=["orders.id"])
    estimate = QueryCostEstimate(estimated_rows=500, estimated_total_cost=None)
    token = issue_approval_token(
        fingerprint=query_fingerprint(query),
        approver_subject="alice",
        key=_KEY,
        connection_id="demo",
        principal_subject="alice",
    )
    service_as_bob = StructuredQueryService(
        connection_id="demo", principal=Principal(subject="bob")
    )
    with pytest.raises(ApprovalRequiredError):
        service_as_bob._enforce_approval_gate(estimate, policy, query, token)
    # ...but it does admit when redeemed by the principal it was bound to.
    service_as_alice = StructuredQueryService(
        connection_id="demo", principal=Principal(subject="alice")
    )
    service_as_alice._enforce_approval_gate(estimate, policy, query, token)  # no raise


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
            fingerprint=query_fingerprint(query),
            approver_subject="a",
            key=_KEY,
            connection_id=None,
            principal_subject=None,
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
        fingerprint=query_fingerprint(approved),
        approver_subject="a",
        key=_KEY,
        connection_id=None,
        principal_subject=None,
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
        fingerprint=query_fingerprint(approved),
        approver_subject="a",
        key=_KEY,
        connection_id=None,
        principal_subject=None,
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
    # The token is bound to this connection and to the approving principal
    # (TODO.md item 151) — the default API-key subject configured by _rest_app.
    assert verify_approval_token(
        body["approval_token"],
        fingerprint=expected_fp,
        key=_KEY,
        connection_id="demo",
        principal_subject="api-key-client",
    )
    # Negative counterparts (test-contract review): a positive check alone
    # can't distinguish "genuinely bound to demo/api-key-client" from "not
    # bound to anything, so it matches whatever I asked for" — an /approve
    # regression that silently drops connection_id/principal_subject from its
    # issue_approval_token call would still pass the assertion above.
    assert not verify_approval_token(
        body["approval_token"],
        fingerprint=expected_fp,
        key=_KEY,
        connection_id="some-other-connection",
        principal_subject="api-key-client",
    )
    assert not verify_approval_token(
        body["approval_token"],
        fingerprint=expected_fp,
        key=_KEY,
        connection_id="demo",
        principal_subject="some-other-principal",
    )


@pytest.mark.asyncio
async def test_approve_write_endpoint_issues_verifiable_token(monkeypatch):
    """Write-side sibling of `test_approve_endpoint_issues_verifiable_token`
    (test-contract review, item 151): `POST /{connection}/write/approve` had
    zero test coverage before this — a regression here (a dropped connection/
    principal binding, a wrong scope check) would not have failed any test."""
    monkeypatch.setattr(svc.app_config, "approval_token_hmac_key", _KEY)
    app = create_app(_rest_app(scopes=("query:approve",)))
    transport = ASGITransport(app=app)
    statement = {
        "op": "delete",
        "table": "orders",
        "where": {"col": "orders.id", "op": "eq", "value": 1},
    }
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        resp = await client.post(
            "/api/v1/demo/write/approve",
            headers={"Authorization": f"Bearer {_KEY_APIKEY}"},
            json=statement,
        )
    assert resp.status_code == 200
    body = resp.json()
    expected_fp = write_fingerprint(DeleteStatement(**statement))
    assert body["fingerprint"] == expected_fp
    assert verify_approval_token(
        body["approval_token"],
        fingerprint=expected_fp,
        key=_KEY,
        connection_id="demo",
        principal_subject="api-key-client",
    )
    assert not verify_approval_token(
        body["approval_token"],
        fingerprint=expected_fp,
        key=_KEY,
        connection_id="some-other-connection",
        principal_subject="api-key-client",
    )
    assert not verify_approval_token(
        body["approval_token"],
        fingerprint=expected_fp,
        key=_KEY,
        connection_id="demo",
        principal_subject="some-other-principal",
    )


@pytest.mark.asyncio
async def test_approve_endpoint_only_the_approving_principal_can_redeem_the_token(monkeypatch):
    """Full REST round trip (test-contract review, item 151): approve as one
    principal, attempt to redeem as a genuinely DIFFERENT principal — must
    stay fail-closed with a fresh 428, even though the token is otherwise
    valid; the SAME principal redeeming it succeeds. Exercises the real
    transport/auth/service wiring end to end (two distinct JWT `sub` claims,
    not two API keys — `ApiKeyAuthenticator` maps every configured key to one
    shared `api_key_subject`, so it can't produce two different principals on
    its own), not just `_enforce_approval_gate` called directly.
    """
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    monkeypatch.setattr(svc.app_config, "approval_token_hmac_key", _KEY)
    set_policy_store(PolicyStore(default=Policy(approval_max_estimated_rows=100), overrides={}))

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    app = create_app(
        AppConfig(
            environment="staging",
            mcp_enabled=False,
            audit_sink_backend="none",
            jwt_enabled=True,
            jwt_jwks_url="https://idp.example.com/.well-known/jwks.json",
            jwt_issuer="https://idp.example.com/",
            jwt_audience="querygate",
        )
    )
    transport = ASGITransport(app=app)

    token_approver = jwt.encode(
        {
            "sub": "principal-approver",
            "iss": "https://idp.example.com/",
            "aud": "querygate",
            "scope": "query:approve",
        },
        private_key,
        algorithm="RS256",
    )
    token_other = jwt.encode(
        {"sub": "principal-different", "iss": "https://idp.example.com/", "aud": "querygate"},
        private_key,
        algorithm="RS256",
    )

    query = {"from_table": "orders", "select": ["orders.id"]}
    table = sa.Table("orders", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True))
    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = [{"id": 1}]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    estimate = QueryCostEstimate(estimated_rows=500, estimated_total_cost=None)
    with (
        patch(
            "jwt.PyJWKClient.get_signing_key_from_jwt",
            lambda self, tok: type("K", (), {"key": public_key})(),
        ),
        patch.object(svc, "validate_schema", AsyncMock(return_value={"orders": table})),
        patch.object(svc, "session_scope", _scope),
        patch.object(svc, "estimate_postgres_query_cost", AsyncMock(return_value=estimate)),
    ):
        async with AsyncClient(transport=transport, base_url="http://localhost") as client:
            approve_resp = await client.post(
                "/api/v1/demo/query/approve",
                headers={"Authorization": f"Bearer {token_approver}"},
                json=query,
            )
            assert approve_resp.status_code == 200
            token = approve_resp.json()["approval_token"]

            # A different principal presenting the SAME token stays fail-closed.
            other_resp = await client.post(
                "/api/v1/demo/query",
                headers={
                    "Authorization": f"Bearer {token_other}",
                    "X-QueryGate-Approval": token,
                },
                json=query,
            )
            assert other_resp.status_code == 428

            # The approving principal redeeming it themselves succeeds.
            same_resp = await client.post(
                "/api/v1/demo/query",
                headers={
                    "Authorization": f"Bearer {token_approver}",
                    "X-QueryGate-Approval": token,
                },
                json=query,
            )
    assert same_resp.status_code == 200
    assert same_resp.json()["row_count"] == 1
