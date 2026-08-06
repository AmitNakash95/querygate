"""Column-value masking (TODO.md item 49).

Covers the policy primitive (`ColumnMask`/`Policy.column_mask`), per-principal
resolution, the reject-non-projection-use validation posture, compiler output,
per-dialect rendering, a real SQLite end-to-end execution, and the audit
`masked_columns` trail.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mssql, postgresql

import querygate.execution.service as svc
from querygate.compiler.dialect_adapters import get_dialect_adapter
from querygate.compiler.sqlalchemy_compiler import (
    applied_column_masks,
    compile_structured_query,
)
from querygate.audit.sinks import JsonlAuditSink, reset_audit_sink, set_audit_sink
from querygate.core.auth import Principal
from querygate.core.exceptions import PolicyViolationError, QueryValidationError
from querygate.core.logging import ContextLogger
from querygate.core.logging import context_logger
from querygate.policy.loader import PolicyStore
from querygate.policy.models import ColumnMask, ColumnMaskKind, Policy
from querygate.query_ast.models import (
    AggregateSelectItem,
    CaseSelectItem,
    CaseWhen,
    ColArg,
    JoinSpec,
    OrderBySpec,
    Predicate,
    ScalarFunctionSelectItem,
    StructuredQuery,
    WhereGroup,
)
from querygate.validation.policy_validation import validate_policy

pytestmark = pytest.mark.unit


def _make_tables() -> Dict[str, sa.Table]:
    metadata = sa.MetaData()
    customers = sa.Table(
        "customers",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(100)),
        sa.Column("phone", sa.String(20)),
        sa.Column("salary", sa.Numeric(10, 2)),
    )
    orders = sa.Table(
        "orders",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("customer_id", sa.Integer),
    )
    return {"customers": customers, "orders": orders}


def _mask_policy() -> Policy:
    return Policy(
        column_masks={
            "customers": [
                ColumnMask(column="phone", kind=ColumnMaskKind.LAST, length=4),
                ColumnMask(column="salary", kind=ColumnMaskKind.BUCKET, bucket_size=10000),
            ]
        }
    )


# --------------------------------------------------------------------------- #
# Policy model
# --------------------------------------------------------------------------- #
class TestColumnMaskModel:
    def test_last_requires_positive_length(self):
        with pytest.raises(ValueError):
            ColumnMask(column="phone", kind="last")
        with pytest.raises(ValueError):
            ColumnMask(column="phone", kind="last", length=0)

    def test_bucket_requires_positive_size(self):
        with pytest.raises(ValueError):
            ColumnMask(column="salary", kind="bucket")
        with pytest.raises(ValueError):
            ColumnMask(column="salary", kind="bucket", bucket_size=0)

    def test_hash_and_null_forbid_params(self):
        for kind in ("hash", "null"):
            with pytest.raises(ValueError):
                ColumnMask(column="phone", kind=kind, length=4)
            with pytest.raises(ValueError):
                ColumnMask(column="phone", kind=kind, bucket_size=10)
        # ...but are valid with no params.
        assert ColumnMask(column="phone", kind="hash").kind is ColumnMaskKind.HASH

    def test_last_forbids_bucket_size_and_vice_versa(self):
        with pytest.raises(ValueError):
            ColumnMask(column="phone", kind="last", length=4, bucket_size=10)
        with pytest.raises(ValueError):
            ColumnMask(column="salary", kind="bucket", bucket_size=10, length=4)


class TestColumnMaskResolution:
    def test_lookup_is_case_insensitive_on_table_and_column(self):
        policy = _mask_policy()
        assert policy.column_mask("CUSTOMERS", "PHONE") is not None
        assert policy.column_mask("customers", "phone").kind is ColumnMaskKind.LAST

    def test_unmasked_column_returns_none(self):
        assert _mask_policy().column_mask("customers", "name") is None
        assert _mask_policy().column_mask("orders", "id") is None

    def test_table_specific_wins_over_wildcard(self):
        policy = Policy(
            column_masks={
                "*": [ColumnMask(column="phone", kind="null")],
                "customers": [ColumnMask(column="phone", kind="last", length=2)],
            }
        )
        assert policy.column_mask("customers", "phone").kind is ColumnMaskKind.LAST
        # A table with no specific entry falls back to the wildcard.
        assert policy.column_mask("orders", "phone").kind is ColumnMaskKind.NULL


class TestPerPrincipalResolution:
    def test_principal_override_replaces_masks(self):
        store = PolicyStore.from_dict(
            {
                "default": {},
                "connections": {
                    "demo": {"column_masks": {"customers": [{"column": "phone", "kind": "null"}]}}
                },
                # A trusted principal clears masking on this connection.
                "principals": {"trusted": {"demo": {"column_masks": {}}}},
            }
        )
        anon = store.get("demo")
        assert anon.column_mask("customers", "phone") is not None

        trusted = store.get("demo", principal=Principal(subject="trusted", auth_method="api_key"))
        assert trusted.column_mask("customers", "phone") is None


# --------------------------------------------------------------------------- #
# Validation — masked column only allowed as a bare projection
# --------------------------------------------------------------------------- #
class TestValidationRejectsNonProjectionUse:
    def test_bare_projection_is_allowed(self):
        query = StructuredQuery(from_table="customers", select=["customers.phone"])
        validate_policy(query, _mask_policy(), connection_id="demo")  # no raise

    def test_rejected_in_where(self):
        query = StructuredQuery(
            from_table="customers",
            select=["customers.id"],
            where=Predicate(col="customers.phone", op="eq", value="555-1234"),
        )
        with pytest.raises(PolicyViolationError, match="masked"):
            validate_policy(query, _mask_policy(), connection_id="demo")

    def test_rejected_in_order_by(self):
        query = StructuredQuery(
            from_table="customers",
            select=["customers.id"],
            order_by=[OrderBySpec(col="customers.salary", dir="desc")],
        )
        with pytest.raises(PolicyViolationError, match="masked"):
            validate_policy(query, _mask_policy(), connection_id="demo")

    def test_rejected_in_group_by(self):
        query = StructuredQuery(
            from_table="customers",
            select=["customers.phone"],
            group_by=["customers.phone"],
        )
        with pytest.raises(PolicyViolationError, match="masked"):
            validate_policy(query, _mask_policy(), connection_id="demo")

    def test_rejected_in_join_key(self):
        query = StructuredQuery(
            from_table="customers",
            select=["customers.id"],
            joins=[JoinSpec(table="orders", on=["customers.phone", "orders.customer_id"])],
        )
        with pytest.raises(PolicyViolationError, match="masked"):
            validate_policy(query, _mask_policy(), connection_id="demo")

    def test_rejected_when_nested_in_scalar_function(self):
        query = StructuredQuery(
            from_table="customers",
            select=[ScalarFunctionSelectItem(fn="lower", args=[ColArg(col="customers.phone")])],
        )
        with pytest.raises(PolicyViolationError, match="masked"):
            validate_policy(query, _mask_policy(), connection_id="demo")

    def test_rejected_when_nested_in_aggregate(self):
        query = StructuredQuery(
            from_table="customers",
            select=[AggregateSelectItem(fn="max", col="customers.salary")],
        )
        with pytest.raises(PolicyViolationError, match="masked"):
            validate_policy(query, _mask_policy(), connection_id="demo")

    def test_rejected_when_nested_in_case(self):
        query = StructuredQuery(
            from_table="customers",
            select=[
                CaseSelectItem(
                    when=[
                        CaseWhen(
                            when=Predicate(col="customers.phone", op="is_not_null"),
                            then=ColArg(col="customers.name"),
                        )
                    ],
                    alias="flag",
                )
            ],
        )
        with pytest.raises(PolicyViolationError, match="masked"):
            validate_policy(query, _mask_policy(), connection_id="demo")


# --------------------------------------------------------------------------- #
# Compiler
# --------------------------------------------------------------------------- #
class TestCompilerMasking:
    def test_masked_projection_preserves_output_name(self):
        tables = _make_tables()
        query = StructuredQuery(from_table="customers", select=["customers.id", "customers.phone"])
        stmt, _ = compile_structured_query(query, tables, _mask_policy(), dialect="postgresql")
        assert [c.name for c in stmt.selected_columns] == ["id", "phone"]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "right(" in compiled.lower()
        assert " AS phone" in compiled

    def test_applied_column_masks_lists_masked_output_names(self):
        query = StructuredQuery(
            from_table="customers",
            select=["customers.id", "customers.phone", "customers.salary"],
        )
        assert applied_column_masks(query, _mask_policy()) == ["phone", "salary"]

    def test_applied_column_masks_empty_without_masks(self):
        query = StructuredQuery(from_table="customers", select=["customers.phone"])
        assert applied_column_masks(query, Policy()) == []


# --------------------------------------------------------------------------- #
# Cross-connection join masking (TODO.md item 156)
# --------------------------------------------------------------------------- #
class TestCrossConnectionMasking:
    """`orders` on the primary connection, `customers` joined in from
    connection `other` (`JoinSpec.connection`) — the same shape item 155 fixed
    for the catalog sensitivity-label trigger. Here the mask itself, not the
    approval trigger, must resolve against the joined table's own connection's
    Policy too."""

    def _tables(self) -> Dict[str, sa.Table]:
        return _make_tables()  # already has both "customers" and "orders"

    def _query(self) -> StructuredQuery:
        return StructuredQuery(
            from_table="orders",
            select=["orders.id", "customers.phone"],
            joins=[
                JoinSpec(
                    table="customers",
                    on=["orders.customer_id", "customers.id"],
                    connection="other",
                )
            ],
        )

    def _resolver(self, other_policy: Policy):
        def resolve(connection_id, principal=None):
            return None, other_policy

        return resolve

    def test_mask_configured_only_on_joined_connection_is_applied(self):
        query = self._query()
        scope_connections = {id(query): {"customers": "other"}}
        other_policy = Policy(column_masks={"customers": [ColumnMask(column="phone", kind="null")]})
        stmt, _ = compile_structured_query(
            query,
            self._tables(),
            Policy(),  # primary policy has NO opinion on `customers.phone`
            dialect="postgresql",
            connection_id="primary",
            scope_connections=scope_connections,
            connection_resolver=self._resolver(other_policy),
        )
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "NULL AS phone" in compiled

    def test_mask_configured_only_on_joined_connection_is_never_applied_without_the_map(self):
        """Pins the pre-156 bug: omitting `connection_id`/`scope_connections`
        (every call site before this item) resolves every table's mask
        against the primary policy alone, so a joined-only mask is silently
        never applied — `customers.phone` comes back raw."""
        query = self._query()
        other_policy = Policy(column_masks={"customers": [ColumnMask(column="phone", kind="null")]})
        stmt, _ = compile_structured_query(query, self._tables(), Policy(), dialect="postgresql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "NULL AS phone" not in compiled
        # Distinguishes raw from masked for real (a masked bare projection
        # renders as `NULL AS phone` with no `customers.phone` reference left
        # anywhere in the statement — the previous version of this assertion
        # accepted `"phone" in [c.name for c in stmt.selected_columns]`, which
        # is true for BOTH the raw and the masked rendering since the output
        # column is still named "phone" either way, so it caught nothing a
        # mutation could actually fail).
        assert "customers.phone" in compiled

    def test_mask_configured_only_on_primary_connection_still_applies(self):
        """Mutation guard against a REPLACE-not-union mistake: the joined
        connection has no opinion at all, only the primary policy masks
        `customers.phone` — must still be applied exactly as before item 156."""
        query = self._query()
        scope_connections = {id(query): {"customers": "other"}}
        stmt, _ = compile_structured_query(
            query,
            self._tables(),
            Policy(column_masks={"customers": [ColumnMask(column="phone", kind="null")]}),
            dialect="postgresql",
            connection_id="primary",
            scope_connections=scope_connections,
            connection_resolver=self._resolver(Policy()),
        )
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "NULL AS phone" in compiled

    def test_when_both_connections_mask_the_same_column_differently_the_primary_wins(self):
        """security-invariant-reviewer, 2026-08-06, on this same item: an
        earlier version of `resolve_table_policies` ordered the JOINED
        connection's Policy first, which is a genuine security regression in
        exactly this one case (both connections configure a DIFFERENT mask on
        the same column) — the joined connection's mask would win even when
        it is WEAKER than the primary's, which is strictly worse protection
        than pre-item-156 querying ever gave (the primary's mask was the only
        one that ever applied). The primary connection's own mask must win
        when both have an opinion — matching the pre-156 default exactly —
        and only fall through to the joined connection's mask when the
        primary has none (the scenario the tests above already cover)."""
        query = self._query()
        scope_connections = {id(query): {"customers": "other"}}
        primary_policy = Policy(
            column_masks={"customers": [ColumnMask(column="phone", kind="null")]}
        )
        other_policy = Policy(
            column_masks={"customers": [ColumnMask(column="phone", kind="last", length=4)]}
        )
        stmt, _ = compile_structured_query(
            query,
            self._tables(),
            primary_policy,
            dialect="postgresql",
            connection_id="primary",
            scope_connections=scope_connections,
            connection_resolver=self._resolver(other_policy),
        )
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "NULL AS phone" in compiled
        assert "right(" not in compiled.lower()

    def test_applied_column_masks_reports_the_joined_connection_only_mask(self):
        query = self._query()
        scope_connections = {id(query): {"customers": "other"}}
        other_policy = Policy(column_masks={"customers": [ColumnMask(column="phone", kind="null")]})
        assert applied_column_masks(
            query,
            Policy(),
            connection_id="primary",
            scope_connections=scope_connections,
            connection_resolver=self._resolver(other_policy),
        ) == ["phone"]
        # Omitting the map reproduces the pre-156 shape: nothing reported.
        assert applied_column_masks(query, Policy()) == []

    def test_single_connection_masking_unaffected_by_the_new_parameters(self):
        """Regression: a plain single-connection query must mask identically
        whether or not `connection_id`/`scope_connections` are supplied."""
        query = StructuredQuery(from_table="customers", select=["customers.id", "customers.phone"])
        tables = self._tables()
        stmt_old, _ = compile_structured_query(query, tables, _mask_policy(), dialect="postgresql")
        stmt_new, _ = compile_structured_query(
            query,
            tables,
            _mask_policy(),
            dialect="postgresql",
            connection_id="demo",
            scope_connections={id(query): {"customers": "demo"}},
        )
        compiled_old = str(stmt_old.compile(compile_kwargs={"literal_binds": True}))
        compiled_new = str(stmt_new.compile(compile_kwargs={"literal_binds": True}))
        assert compiled_old == compiled_new


# --------------------------------------------------------------------------- #
# Dialect rendering
# --------------------------------------------------------------------------- #
class TestDialectRendering:
    def _col(self):
        return sa.table("customers", sa.column("phone"), sa.column("salary")).c

    @pytest.mark.parametrize("dialect_obj", [postgresql.dialect(), mssql.dialect()])
    def test_null_and_bucket_render_universally(self, dialect_obj):
        cols = self._col()
        name = dialect_obj.name
        null_expr = get_dialect_adapter(name).column_mask(
            cols.phone, ColumnMask(column="phone", kind="null")
        )
        assert "NULL" in str(sa.select(null_expr).compile(dialect=dialect_obj))
        bucket_expr = get_dialect_adapter(name).column_mask(
            cols.salary, ColumnMask(column="salary", kind="bucket", bucket_size=1000)
        )
        assert "floor" in str(sa.select(bucket_expr).compile(dialect=dialect_obj)).lower()

    def test_postgres_hash_and_last_use_pg_idiom(self):
        cols = self._col()
        adapter = get_dialect_adapter("postgresql")
        hash_sql = str(
            sa.select(
                adapter.column_mask(cols.phone, ColumnMask(column="phone", kind="hash"))
            ).compile(dialect=postgresql.dialect())
        )
        assert "md5" in hash_sql.lower()
        last_sql = str(
            sa.select(
                adapter.column_mask(cols.phone, ColumnMask(column="phone", kind="last", length=4))
            ).compile(dialect=postgresql.dialect())
        )
        assert "right(" in last_sql.lower()

    def test_mssql_hash_and_last_use_tsql_idiom(self):
        cols = self._col()
        adapter = get_dialect_adapter("mssql")
        hash_sql = str(
            sa.select(
                adapter.column_mask(cols.phone, ColumnMask(column="phone", kind="hash"))
            ).compile(dialect=mssql.dialect())
        )
        assert "HASHBYTES" in hash_sql
        last_sql = str(
            sa.select(
                adapter.column_mask(cols.phone, ColumnMask(column="phone", kind="last", length=4))
            ).compile(dialect=mssql.dialect())
        )
        assert "RIGHT(" in last_sql.upper()

    def test_sqlite_hash_rejects(self):
        cols = self._col()
        with pytest.raises(QueryValidationError, match="hash"):
            get_dialect_adapter("sqlite").column_mask(
                cols.phone, ColumnMask(column="phone", kind="hash")
            )


# --------------------------------------------------------------------------- #
# End-to-end against a real SQLite database
# --------------------------------------------------------------------------- #
class TestSQLiteEndToEnd:
    def _seed(self):
        engine = sa.create_engine("sqlite://")
        metadata = sa.MetaData()
        customers = sa.Table(
            "customers",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("phone", sa.String),
            sa.Column("salary", sa.Float),
        )
        metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(
                customers.insert(), [{"id": 1, "phone": "555-987-6789", "salary": 84000.0}]
            )
        return engine, {"customers": customers}

    def _run(self, engine, tables, policy):
        query = StructuredQuery(
            from_table="customers",
            select=["customers.id", "customers.phone", "customers.salary"],
        )
        validate_policy(query, policy, connection_id="demo")
        stmt, _ = compile_structured_query(query, tables, policy, dialect="sqlite")
        with engine.connect() as conn:
            return dict(conn.execute(stmt).mappings().first())

    def test_last_and_bucket_mask_the_returned_value(self):
        engine, tables = self._seed()
        row = self._run(engine, tables, _mask_policy())
        assert row == {"id": 1, "phone": "6789", "salary": 80000.0}
        # The raw value never comes back.
        assert "555-987-6789" not in json.dumps(row)

    def test_null_mask_returns_null(self):
        engine, tables = self._seed()
        policy = Policy(column_masks={"customers": [ColumnMask(column="phone", kind="null")]})
        row = self._run(engine, tables, policy)
        assert row["phone"] is None


# --------------------------------------------------------------------------- #
# Audit trail
# --------------------------------------------------------------------------- #
@pytest.fixture
def _isolated_sink():
    reset_audit_sink()
    yield
    reset_audit_sink()


@pytest.mark.asyncio
async def test_audit_event_records_masked_columns(tmp_path, _isolated_sink):
    path = tmp_path / "events.jsonl"
    set_audit_sink(JsonlAuditSink(str(path)))
    table = sa.Table(
        "customers",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("phone", sa.String(20)),
    )
    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = [{"id": 1, "phone": "SECRET_PHONE_VALUE"}]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    policy = Policy(column_masks={"customers": [ColumnMask(column="phone", kind="last", length=4)]})
    token = context_logger.set(ContextLogger(request_id="req-mask"))
    try:
        with (
            patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
            patch.object(svc, "session_scope", _scope),
            patch.object(svc.StructuredQueryService, "_get_policy", lambda self: policy),
        ):
            service = svc.StructuredQueryService(connection_id="demo", surface="rest")
            await service.execute(
                StructuredQuery(from_table="customers", select=["customers.id", "customers.phone"])
            )
    finally:
        context_logger.reset(token)

    raw = path.read_text()
    event = json.loads(raw)
    assert event["masked_columns"] == ["phone"]
    # Redaction-safe: the pre-mask row value never appears in the event. The
    # sentinel is uppercase + underscore so it can never coincide with the
    # lowercase-hex uuid4 event_id/admission_id (a bare-digit sentinel like "555"
    # flakily collides with those UUIDs).
    assert "SECRET_PHONE_VALUE" not in raw
