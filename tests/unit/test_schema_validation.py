"""Unit tests for schema-truth validation (table/column existence, join graph,
cross-connection join_group enforcement).
"""

from __future__ import annotations

from typing import Dict
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.core.auth import Principal
from querygate.core.exceptions import ConfigValidationError, NotFoundError
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.compiler.sqlalchemy_compiler import compile_structured_query
from querygate.query_ast.models import (
    AggregateSelectItem,
    JoinSpec,
    OrderBySpec,
    Predicate,
    StructuredQuery,
    TopNSpec,
    WhereGroup,
)
from querygate.validation import schema_validation as sv


def _make_tables() -> Dict[str, sa.Table]:
    metadata = sa.MetaData()
    customers = sa.Table(
        "customers",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(100)),
    )
    orders = sa.Table(
        "orders",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("customer_id", sa.Integer),
        sa.Column("status", sa.String(20)),
    )
    return {"customers": customers, "orders": orders}


def _patch_load_table(monkeypatch, tables: Dict[str, sa.Table]):
    calls = []

    async def fake_load_table(connection_id, table_name, table_connection):
        calls.append((connection_id, table_name, table_connection))
        return tables[table_name]

    monkeypatch.setattr(sv, "_load_table", fake_load_table)
    return calls


@pytest.mark.asyncio
class TestValidateSchema:
    async def test_rejects_unknown_column(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(from_table="customers", select=["customers.missing"], limit=5)
        with pytest.raises(ValueError, match="not found"):
            await sv.validate_schema(query, connection_id="demo")

    async def test_accepts_valid_query(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id", "customers.name"],
            joins=[JoinSpec(table="customers", on=["orders.customer_id", "customers.id"])],
            where=Predicate(col="orders.status", op="eq", value="completed"),
            limit=5,
        )
        loaded = await sv.validate_schema(query, connection_id="demo")
        assert set(loaded) == {"orders", "customers"}

    async def test_join_must_connect_to_graph(self, monkeypatch):
        tables = _make_tables()
        tables["dangling"] = sa.Table(
            "dangling", sa.MetaData(), sa.Column("id", sa.Integer), sa.Column("x", sa.Integer)
        )
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[JoinSpec(table="dangling", on=["customers.id", "dangling.x"])],
            limit=5,
        )
        with pytest.raises(ValueError, match="does not connect"):
            await sv.validate_schema(query, connection_id="demo")

    async def test_self_join_reflects_physical_table_once_and_aliases_both(self, monkeypatch):
        metadata = sa.MetaData()
        employees = sa.Table(
            "employees",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("name", sa.String(100)),
            sa.Column("manager_id", sa.Integer),
        )
        calls = _patch_load_table(monkeypatch, {"employees": employees})
        query = StructuredQuery(
            from_table="employees",
            from_alias="e",
            select=["e.name", "m.name"],
            joins=[
                JoinSpec(
                    table="employees",
                    alias="m",
                    on=["e.manager_id", "m.id"],
                )
            ],
            limit=5,
        )
        tables = await sv.validate_schema(query, connection_id="demo")
        assert set(tables) == {"e", "m"}
        assert tables["e"].name == "e"
        assert tables["m"].name == "m"
        assert tables["e"].element is employees
        assert tables["m"].element is employees
        assert tables["e"] is not tables["m"]
        # the physical table was only reflected once, not once per occurrence
        assert len(calls) == 1

    async def test_value_col_referencing_join_table_reflects_and_resolves(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[JoinSpec(table="customers", on=["orders.customer_id", "customers.id"])],
            where=Predicate(col="orders.status", op="neq", value_col="customers.name"),
            limit=5,
        )
        loaded = await sv.validate_schema(query, connection_id="demo")
        assert set(loaded) == {"orders", "customers"}

    async def test_value_col_unknown_column_rejected(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            where=Predicate(col="orders.status", op="neq", value_col="orders.missing"),
            limit=5,
        )
        with pytest.raises(ValueError, match="not found"):
            await sv.validate_schema(query, connection_id="demo")

    async def test_not_group_column_validated(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            where=WhereGroup(not_terms=Predicate(col="orders.missing", op="eq", value="x")),
            limit=5,
        )
        with pytest.raises(ValueError, match="not found"):
            await sv.validate_schema(query, connection_id="demo")

    async def test_composite_join_extra_on_columns_resolved(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[
                JoinSpec(
                    table="customers",
                    on=["orders.customer_id", "customers.id"],
                    extra_on=[["orders.status", "customers.name"]],
                )
            ],
            limit=5,
        )
        loaded = await sv.validate_schema(query, connection_id="demo")
        assert set(loaded) == {"orders", "customers"}

    async def test_join_condition_columns_are_resolved_against_the_schema(self, monkeypatch):
        """A general join `condition` (item 103) is a WhereNode, so its columns
        must be resolved here like any other ref — not left to fail deeper in the
        compiler with a compiler-internal error."""
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[
                JoinSpec(
                    table="customers",
                    condition=Predicate(
                        col="orders.customer_id", op="gte", value_col="customers.id"
                    ),
                )
            ],
            limit=5,
        )
        loaded = await sv.validate_schema(query, connection_id="demo")
        assert set(loaded) == {"orders", "customers"}

    async def test_join_condition_unknown_column_rejected(self, monkeypatch):
        """The negative half — and the one that pins the check itself, since a
        condition over only real columns passes either way."""
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[
                JoinSpec(
                    table="customers",
                    condition=WhereGroup(
                        and_terms=[
                            Predicate(col="orders.customer_id", op="eq", value_col="customers.id"),
                            Predicate(
                                col="orders.customer_id", op="gte", value_col="customers.missing"
                            ),
                        ]
                    ),
                )
            ],
            limit=5,
        )
        with pytest.raises(ValueError, match="not found"):
            await sv.validate_schema(query, connection_id="demo")

    async def test_composite_join_extra_on_unknown_column_rejected(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[
                JoinSpec(
                    table="customers",
                    on=["orders.customer_id", "customers.id"],
                    extra_on=[["orders.missing", "customers.name"]],
                )
            ],
            limit=5,
        )
        with pytest.raises(ValueError, match="not found"):
            await sv.validate_schema(query, connection_id="demo")

    async def test_composite_join_extra_on_referencing_third_table_rejected(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[
                JoinSpec(
                    table="customers",
                    on=["orders.customer_id", "customers.id"],
                    extra_on=[["orders.id", "orders.status"]],
                )
            ],
            limit=5,
        )
        with pytest.raises(ValueError, match="same two tables"):
            await sv.validate_schema(query, connection_id="demo")

    async def test_predicate_col_fn_column_resolved(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            where=Predicate(
                col_fn={"fn": "lower", "args": [{"col": "orders.status"}]},
                op="eq",
                value="active",
            ),
            limit=5,
        )
        loaded = await sv.validate_schema(query, connection_id="demo")
        assert set(loaded) == {"orders"}

    async def test_predicate_col_fn_unknown_column_rejected(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            where=Predicate(
                col_fn={"fn": "lower", "args": [{"col": "orders.missing"}]},
                op="eq",
                value="active",
            ),
            limit=5,
        )
        with pytest.raises(ValueError, match="not found"):
            await sv.validate_schema(query, connection_id="demo")

    async def test_predicate_col_fn_referencing_join_table_reflects_it(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[JoinSpec(table="customers", on=["orders.customer_id", "customers.id"])],
            where=Predicate(
                col_fn={"fn": "lower", "args": [{"col": "customers.name"}]},
                op="eq",
                value="ada",
            ),
            limit=5,
        )
        loaded = await sv.validate_schema(query, connection_id="demo")
        assert set(loaded) == {"orders", "customers"}

    async def test_having_requires_group_by_or_aggregate(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            having=Predicate(col="orders.status", op="eq", value="completed"),
            limit=5,
        )
        with pytest.raises(ValueError, match="having requires"):
            await sv.validate_schema(query, connection_id="demo")

    async def test_having_allowed_with_string_agg_and_no_group_by(self, monkeypatch):
        """A select-only string_agg (no group_by, no AggregateSelectItem)
        must count as an aggregate for the having-requires-aggregate rule —
        this only passes if StringAggSelectItem is recognized alongside
        AggregateSelectItem in _validate_group_by's has_aggregate check."""
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=[
                {"col": "orders.status", "delimiter": ", ", "as": "statuses"},
            ],
            having=Predicate(col="statuses", op="neq", value=""),
            limit=5,
        )
        result = await sv.validate_schema(query, connection_id="demo")
        assert result is not None

    async def test_having_allowed_with_array_agg_and_no_group_by(self, monkeypatch):
        """A select-only array_agg (no group_by, no AggregateSelectItem)
        must count as an aggregate for the having-requires-aggregate rule —
        this only passes if ArrayAggSelectItem is recognized alongside
        AggregateSelectItem/StringAggSelectItem in _validate_group_by's
        has_aggregate check."""
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=[
                {"col": "orders.status", "as": "statuses"},
            ],
            having=Predicate(col="statuses", op="neq", value=""),
            limit=5,
        )
        result = await sv.validate_schema(query, connection_id="demo")
        assert result is not None

    async def test_having_allowed_with_percentile_cont_and_no_group_by(self, monkeypatch):
        """A select-only percentile_cont (no group_by, no
        AggregateSelectItem) must count as an aggregate for the
        having-requires-aggregate rule — this only passes if
        PercentileContSelectItem is recognized in the shared
        _AGGREGATE_SELECT_ITEM_TYPES tuple _validate_group_by's
        has_aggregate check uses."""
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=[
                {"col": "orders.status", "fraction": 0.5, "as": "median"},
            ],
            having=Predicate(col="median", op="gt", value=0),
            limit=5,
        )
        result = await sv.validate_schema(query, connection_id="demo")
        assert result is not None


@pytest.mark.asyncio
class TestCrossConnectionJoins:
    def _two_connections(self, group_a="group-a", group_b="group-a"):
        primary = ConnectionProfile(
            id="primary",
            dialect="mssql",
            connection_string="mssql+aioodbc://user:pass@host/primary_db",
            join_group=group_a,
        )
        other = ConnectionProfile(
            id="other",
            dialect="mssql",
            connection_string="mssql+aioodbc://user:pass@host/other_db",
            join_group=group_b,
        )
        set_registry(ConnectionRegistry({"primary": primary, "other": other}))
        set_policy_store(PolicyStore(default=Policy(), overrides={}))

    async def test_same_join_group_allowed(self, monkeypatch):
        self._two_connections(group_a="shared", group_b="shared")
        tables = _make_tables()
        calls = _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id", "customers.name"],
            joins=[
                JoinSpec(
                    table="customers",
                    on=["orders.customer_id", "customers.id"],
                    connection="other",
                )
            ],
            limit=5,
        )
        await sv.validate_schema(query, connection_id="primary")
        # _load_table is always invoked through the primary connection's engine
        # (connection_id); table_connection records which connection's physical
        # database each table actually belongs to.
        engine_connection_ids = {conn for conn, _, _ in calls}
        assert engine_connection_ids == {"primary"}
        table_connection_map = {name: table_conn for _, name, table_conn in calls}
        assert table_connection_map["orders"] == "primary"
        assert table_connection_map["customers"] == "other"

    async def test_different_join_group_rejected(self, monkeypatch):
        self._two_connections(group_a="group-a", group_b="group-b")
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[
                JoinSpec(
                    table="customers",
                    on=["orders.customer_id", "customers.id"],
                    connection="other",
                )
            ],
            limit=5,
        )
        with pytest.raises(ValueError, match="cross-connection"):
            await sv.validate_schema(query, connection_id="primary")

    async def test_policy_join_group_spanning_different_hosts_is_rejected(self, monkeypatch):
        """TODO.md item 170 (security-invariant-reviewer / architecture-
        boundary-reviewer, 2026-08-09, QG170-1/170-A, both independently
        found the same gap): `connections/registry.py`'s config-load-time
        join_group host check only ever sees `ConnectionProfile.join_group`
        — neither connection here sets one (each defaults to its own id, a
        1-member group the config-load check never compares), so that check
        is a no-op for this shape. A `Policy.join_group` override — set
        here at the `default` level, exactly as an operator could in
        policy.yaml — unites the two connections at REQUEST time regardless,
        via `policy.join_group or profile.effective_join_group()`
        (`resolve_query_table_connections`). Without the request-time host
        check, this cross-connection join would be silently allowed and
        `_load_table` would reflect `other`'s table through `primary`'s own
        engine as if they were the same physical server, despite genuinely
        different hosts."""
        primary = ConnectionProfile(
            id="primary",
            dialect="mssql",
            connection_string="mssql+aioodbc://user:pass@host-a/primary_db",
        )
        other = ConnectionProfile(
            id="other",
            dialect="mssql",
            connection_string="mssql+aioodbc://user:pass@host-b/other_db",
        )
        set_registry(ConnectionRegistry({"primary": primary, "other": other}))
        set_policy_store(PolicyStore(default=Policy(join_group="shared"), overrides={}))
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[
                JoinSpec(
                    table="customers",
                    on=["orders.customer_id", "customers.id"],
                    connection="other",
                )
            ],
            limit=5,
        )
        with pytest.raises(ConfigValidationError, match="not the same physical"):
            await sv.validate_schema(query, connection_id="primary")

    async def test_policy_join_group_on_the_same_host_with_one_port_omitted_is_allowed(
        self, monkeypatch
    ):
        """Companion regression: the request-time host check must compare
        host and port separately, the same way `_validate_join_group_hosts`
        does (QG170-4) — a caller comparing `(host, port)` as a single tuple
        would falsely reject two databases on the SAME Postgres server just
        because one connection string omits the (default) port and the
        other states it explicitly."""
        primary = ConnectionProfile(
            id="primary",
            dialect="mssql",
            connection_string="mssql+aioodbc://user:pass@host/primary_db",
        )
        other = ConnectionProfile(
            id="other",
            dialect="mssql",
            connection_string="mssql+aioodbc://user:pass@host:1433/other_db",
        )
        set_registry(ConnectionRegistry({"primary": primary, "other": other}))
        set_policy_store(PolicyStore(default=Policy(join_group="shared"), overrides={}))
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[
                JoinSpec(
                    table="customers",
                    on=["orders.customer_id", "customers.id"],
                    connection="other",
                )
            ],
            limit=5,
        )
        await sv.validate_schema(query, connection_id="primary")

    @pytest.mark.parametrize("secondary_dialect", ["snowflake", "bigquery"])
    async def test_secondary_connection_not_connectable_rejected_at_validation_time(
        self, secondary_dialect
    ):
        """Regression for item 163. A `join_group`-eligible cross-connection join
        whose SECONDARY connection is a registered-but-not-connectable dialect
        (Snowflake/BigQuery — item 19 phases 2/3) used to sail straight past
        `resolve_query_table_connections`'s only other guard (`join_group`
        membership) because `connections/engine.py`'s `init_engine` — the sole
        place `SessionDialectAdapter.is_connectable()` was checked — only ever
        runs for a query's PRIMARY connection. It would then fail later with a
        confusing masked error rather than a clean, early, client-actionable
        rejection: `_load_table` always reflects through the PRIMARY
        connection's own engine (never the secondary's — see its docstring), so
        without this guard the primary engine would attempt to reflect the
        joined table under a schema qualifier borrowed from the secondary's
        connection string and fail with `sqlalchemy.exc.NoSuchTableError` — the
        secondary's own engine/driver is never actually reached either way.

        Parametrized over both not-connectable dialects (not just Snowflake) so
        the claim that BigQuery-as-secondary is rejected the same way is
        actually exercised, not just inferred from `is_connectable()` being
        independently `False` for it.

        `_load_table` is intentionally left UNPATCHED here (unlike the sibling
        tests in this class) — the whole point is that reflection must never be
        reached at all; if the guard regresses, this test would instead
        exercise the masked-`NoSuchTableError` path described above instead of
        raising `ConfigValidationError`.
        """
        primary = ConnectionProfile(
            id="primary",
            dialect="mssql",
            connection_string="mssql+aioodbc://user:pass@host/primary_db",
            join_group="shared",
        )
        other = ConnectionProfile(
            id="other",
            dialect=secondary_dialect,
            connection_string=f"{secondary_dialect}://user:pass@account/db",
            join_group="shared",
        )
        set_registry(ConnectionRegistry({"primary": primary, "other": other}))
        set_policy_store(PolicyStore(default=Policy(), overrides={}))
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[
                JoinSpec(
                    table="customers",
                    on=["orders.customer_id", "customers.id"],
                    connection="other",
                )
            ],
            limit=5,
        )
        with pytest.raises(ConfigValidationError, match="cannot yet open a live connection"):
            await sv.validate_schema(query, connection_id="primary")

    async def test_join_group_uses_per_principal_policy(self, monkeypatch):
        self._two_connections(group_a="shared", group_b="shared")
        set_policy_store(
            PolicyStore.from_dict(
                {
                    "default": {},
                    "principals": {
                        "agent-a": {
                            "primary": {"join_group": "agent-a-primary"},
                            "other": {"join_group": "agent-a-other"},
                        }
                    },
                }
            )
        )
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[
                JoinSpec(
                    table="customers",
                    on=["orders.customer_id", "customers.id"],
                    connection="other",
                )
            ],
            limit=5,
        )

        # Connection-level fallback says both connections share a join_group,
        # but this principal's policy splits them, so the principal-specific
        # view must reject the cross-connection join.
        await sv.validate_schema(query, connection_id="primary")
        with pytest.raises(ValueError, match="cross-connection"):
            await sv.validate_schema(
                query,
                connection_id="primary",
                principal=Principal(subject="agent-a"),
            )

    async def test_cross_connection_join_cannot_reach_principal_hidden_connection(
        self, monkeypatch
    ):
        self._two_connections(group_a="shared", group_b="shared")
        set_policy_store(
            PolicyStore.from_dict(
                {
                    "default": {"enabled": True},
                    "principals": {"agent-a": {"other": {"enabled": False}}},
                }
            )
        )
        _patch_load_table(monkeypatch, _make_tables())
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[
                JoinSpec(
                    table="customers",
                    on=["orders.customer_id", "customers.id"],
                    connection="other",
                )
            ],
            limit=5,
        )

        with pytest.raises(NotFoundError, match="Unknown connection: 'other'"):
            await sv.validate_schema(
                query,
                connection_id="primary",
                principal=Principal(subject="agent-a"),
            )

    async def test_reflection_lookup_case_folds_table_connection_key(self, monkeypatch):
        """Regression for item 159. `_reflect_and_validate_scope`'s `_load_table`
        lookup used to be `table_connection.get(name, connection_id)` —
        case-SENSITIVE — while `table_connection`'s keys preserve a join's own
        DECLARED alias/table casing and `needed` (the set of names to reflect) is
        unioned from that declared spelling AND every column ref's own table
        token. A join declared `alias="O"` but referenced as `"o.id"` put BOTH
        `"O"` and `"o"` into `needed`; whichever spelling happened to win
        Python's hash-randomized `set` iteration order for that table decided
        whether the lookup hit (correct) or missed and silently fell back to the
        PRIMARY connection (wrong) — the same query could reflect against the
        right or wrong connection depending on the process's hash seed.

        Deliberately NOT testing this via the natural `needed`-set race (which
        `test_natural_casing_mismatch_resolves_to_the_joined_connection` below
        also exercises, but whose *pre-fix* failure depends on which of "O"/"o"
        the set happens to yield first). Instead this calls
        `_reflect_and_validate_scope` directly with a hand-supplied
        `table_connection` map whose only relevant key ("O") deliberately does
        NOT match the casing the query itself uses for that join everywhere
        (alias "o", ref "o.id") — so `needed` contains exactly ONE name for the
        joined table ("o"), no race, and the lookup either matches
        case-insensitively (fixed) or misses every single time (bug), with no
        dependency on set ordering or PYTHONHASHSEED at all.
        """
        self._two_connections(group_a="shared", group_b="shared")
        tables = _make_tables()
        calls = _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="customers",
            select=["customers.id", "o.id"],
            joins=[
                JoinSpec(
                    table="orders",
                    alias="o",
                    on=["customers.id", "o.customer_id"],
                    connection="other",
                )
            ],
            limit=5,
        )
        # A hand-supplied map, standing in for `resolve_query_table_connections`'s
        # real output but deliberately keyed with different casing ("O") than the
        # query's own declared alias ("o") — the map's key casing is exactly what
        # the fix must not depend on matching `needed`'s spelling exactly.
        table_connection = {"customers": "primary", "O": "other"}
        await sv._reflect_and_validate_scope(query, "primary", table_connection=table_connection)
        table_connection_map = {name: table_conn for _, name, table_conn in calls}
        assert table_connection_map["orders"] == "other"

    async def test_natural_casing_mismatch_resolves_to_the_joined_connection(self, monkeypatch):
        """The same bug (item 159), exercised through the real pipeline: a join
        declared with alias `"O"` but referenced in the projection as `"o.id"`.
        `resolve_query_table_connections` (unpatched) produces the real,
        declared-casing-keyed map here, so `needed`'s two spellings ("O" from the
        join, "o" from the ref) genuinely race for which one triggers the single
        memoized `_load_table` call — this test does not control that race. What
        it DOES assert holds regardless of the race: after the fix, the lookup is
        case-folded, so BOTH spellings resolve identically and the outcome no
        longer depends on which one wins. This test therefore passes
        deterministically post-fix; the fully order-independent proof of the bug
        itself is the previous test, which sidesteps the race entirely.
        """
        self._two_connections(group_a="shared", group_b="shared")
        tables = _make_tables()
        calls = _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="customers",
            select=["customers.id", "o.id"],
            joins=[
                JoinSpec(
                    table="orders",
                    alias="O",
                    on=["customers.id", "O.customer_id"],
                    connection="other",
                )
            ],
            limit=5,
        )
        await sv.validate_schema(query, connection_id="primary")
        table_connection_map = {name: table_conn for _, name, table_conn in calls}
        assert table_connection_map["orders"] == "other"

    async def test_cross_connection_self_join_reflects_each_alias_against_its_own_connection(
        self, monkeypatch
    ):
        """TODO.md item 166 (maintainer-approved 2026-08-09: fix the reflection
        memo key rather than reject the shape). A self-join — the same physical
        table name declared twice, with the join naming a DIFFERENT `connection`
        than the primary — used to collapse onto ONE connection:
        `_reflect_and_validate_scope`'s `physical_tables` memo was keyed by
        physical table name alone, so whichever alias's reflection ran first won
        the memo slot and the SECOND alias silently reused that same `sa.Table`
        object — compiled and executed against the first alias's connection
        despite its own declared `connection` naming the other one. Unlike the
        item-159 casing race this class also covers, both aliases here share the
        identical casefolded name ("orders"/"orders"), so which alias's
        reflection call WINS the single pre-fix memo slot depends on `needed`'s
        (a `set`) iteration order — but the test's own PASS/FAIL outcome does not:
        `len(calls) == 2` and the two identity/schema assertions below all fail
        deterministically regardless of which alias wins, since exactly one
        `_load_table` call (and one bound schema) is missing either way. Post-fix
        both calls happen and each alias binds to ITS OWN reflection, because the
        memo key includes the resolved connection, not just the name.

        security-invariant-reviewer, 2026-08-09 (SIR-166-1): the first version of
        this test only asserted on `_load_table`'s recorded call arguments, which
        proves both connections were REFLECTED but not that each alias actually
        BINDS to its own reflection rather than to whichever one happened to win
        a downstream lookup — a real, already-shipped bug class in this same
        region (items 167/169). The `.element is`/`.schema` assertions below
        close that gap by giving each connection's `_load_table` call a
        distinguishable table (unlike `_patch_load_table`'s shared fake, which
        returns the same object for both, masking exactly this)."""
        self._two_connections(group_a="shared", group_b="shared")
        primary_orders = sa.Table("orders", sa.MetaData(), sa.Column("id", sa.Integer))
        other_orders = sa.Table(
            "orders", sa.MetaData(), sa.Column("id", sa.Integer), schema="other_db.dbo"
        )
        calls = []

        async def fake_load_table(connection_id, table_name, table_connection):
            calls.append((connection_id, table_name, table_connection))
            return other_orders if table_connection == "other" else primary_orders

        monkeypatch.setattr(sv, "_load_table", fake_load_table)
        query = StructuredQuery(
            from_table="orders",
            from_alias="o1",
            select=["o1.id", "o2.id"],
            joins=[
                JoinSpec(
                    table="orders",
                    alias="o2",
                    on=["o1.id", "o2.id"],
                    connection="other",
                )
            ],
            limit=5,
        )
        resolved = await sv.validate_schema(query, connection_id="primary")
        assert set(resolved) == {"o1", "o2"}
        # Both aliases must be reflected -- one per connection -- not collapsed
        # onto whichever alias's reflection happened to run first.
        assert len(calls) == 2
        table_connections = {table_conn for _, _, table_conn in calls}
        assert table_connections == {"primary", "other"}
        # And each alias must BIND to its own connection's reflection, not just
        # trigger the right `_load_table` call in passing (SIR-166-1).
        assert resolved["o1"].element is primary_orders
        assert resolved["o2"].element is other_orders
        assert resolved["o1"].element.schema is None
        assert resolved["o2"].element.schema == "other_db.dbo"


def test_aggregate_default_alias_matches_the_compilers_for_an_unaliased_column_aggregate():
    """`top_n` resolves its refs against the names `_aggregate_alias` returns,
    while the compiler labels the column with its OWN default-alias logic. The
    two must agree, or a valid top_n over an unaliased aggregate is rejected.

    Regression: when item 100 normalized `col` into `arg`, `_aggregate_alias`
    still read `item.col` — now `None` for a column aggregate — and raised
    `TypeError` instead of returning `sum_total_amount`.
    """
    item = AggregateSelectItem(fn="sum", col="orders.total_amount")
    assert item.col is None and item.arg is not None  # normalized to `arg`
    assert sv._aggregate_alias(item) == "sum_total_amount"
    assert sv._aggregate_alias(AggregateSelectItem(fn="count", col="*")) == "count_all"

    # And the compiler agrees, which is the property that actually matters.
    metadata = sa.MetaData()
    orders = sa.Table(
        "orders",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("customer_id", sa.Integer),
        sa.Column("total_amount", sa.Numeric(10, 2)),
    )
    query = StructuredQuery(
        from_table="orders",
        select=["orders.customer_id", item],
        group_by=["orders.customer_id"],
        top_n=TopNSpec(
            partition_by=[], order_by=[OrderBySpec(col="sum_total_amount", dir="desc")], n=1
        ),
    )
    sv._validate_top_n(query, {"orders": orders})  # no raise
    stmt, _ = compile_structured_query(query, {"orders": orders}, Policy())
    assert "sum_total_amount" in str(stmt.compile(compile_kwargs={"literal_binds": True}))


@pytest.mark.asyncio
class TestWindowSchemaValidation:
    """item 101 — every ref a window carries must resolve against a real
    reflected column before the query reaches a database."""

    @staticmethod
    def _query(**over) -> StructuredQuery:
        return StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": [
                    "orders.id",
                    {"fn": "sum", "arg": {"col": "orders.id"}, "over": over, "as": "w"},
                ],
            }
        )

    async def test_accepts_a_window_over_real_columns(self, monkeypatch):
        _patch_load_table(monkeypatch, _make_tables())
        await sv.validate_schema(
            self._query(partition_by=["orders.status"], order_by=[{"col": "orders.id"}]),
            connection_id="demo",
        )

    @pytest.mark.parametrize(
        "over",
        [
            {"partition_by": ["orders.missing"]},
            {"order_by": [{"col": "orders.missing"}]},
        ],
    )
    async def test_rejects_an_unknown_column_in_the_over_clause(self, monkeypatch, over):
        _patch_load_table(monkeypatch, _make_tables())
        with pytest.raises(ValueError, match="not found"):
            await sv.validate_schema(self._query(**over), connection_id="demo")

    async def test_rejects_an_undeclared_table_reached_only_through_a_window(self, monkeypatch):
        """A window's PARTITION BY cannot smuggle in a table the query never
        joined — the ref flows through the canonical visitor, so it hits the same
        undeclared-table check as any other reference."""
        _patch_load_table(monkeypatch, _make_tables())
        with pytest.raises(ValueError, match="undeclared"):
            await sv.validate_schema(
                self._query(partition_by=["customers.name"]), connection_id="demo"
            )
