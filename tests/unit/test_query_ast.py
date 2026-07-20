"""Unit tests for the StructuredQuery AST."""

from __future__ import annotations

import pytest

from querygate.query_ast.models import (
    AggregateSelectItem,
    JoinSpec,
    OrderBySpec,
    Predicate,
    StructuredQuery,
    WhereGroup,
)
from querygate.validation.schema_validation import parse_column_ref


class TestStructuredQueryModels:
    def test_parse_from_alias_and_aggregate_as(self):
        q = StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": ["orders.id", {"fn": "count", "col": "*", "as": "item_count"}],
                "limit": 10,
            }
        )
        assert q.from_table == "orders"
        assert isinstance(q.select[1], AggregateSelectItem)
        assert q.select[1].alias == "item_count"

    def test_where_requires_and_or(self):
        with pytest.raises(ValueError):
            WhereGroup.model_validate({})

    def test_between_requires_two_values(self):
        with pytest.raises(ValueError, match="between"):
            Predicate(col="orders.id", op="between", value=[1])

    def test_is_null_rejects_value(self):
        with pytest.raises(ValueError, match="must not include"):
            Predicate(col="orders.status", op="is_null", value="x")

    def test_in_requires_non_empty_list(self):
        with pytest.raises(ValueError, match="non-empty list"):
            Predicate(col="orders.status", op="in", value=[])

    def test_order_by_rejects_misnamed_direction_key(self):
        for variant in [
            {"col": "x", "direction": "desc"},
            {"col": "x", "sort": "desc"},
            {"col": "x", "desc": True},
        ]:
            with pytest.raises(ValueError):
                OrderBySpec.model_validate(variant)

    def test_order_by_rejects_bad_dir_value(self):
        with pytest.raises(ValueError):
            OrderBySpec(col="x", dir="DESC")

    def test_structured_query_rejects_misnamed_order_by_key(self):
        with pytest.raises(ValueError):
            StructuredQuery.model_validate(
                {
                    "from": "orders",
                    "select": ["orders.id"],
                    "sort": [{"col": "orders.id", "dir": "desc"}],
                }
            )

    def test_no_raw_sql_field_exists(self):
        """There is no `sql` field anywhere on the AST — extra="forbid" rejects
        any attempt to smuggle a raw SQL string past the schema.
        """
        with pytest.raises(ValueError):
            StructuredQuery.model_validate(
                {"from": "orders", "select": ["orders.id"], "sql": "SELECT * FROM orders"}
            )

    def test_join_uses_connection_not_database(self):
        q = StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": ["orders.id"],
                "joins": [
                    {
                        "table": "customers",
                        "on": ["orders.customer_id", "customers.id"],
                        "connection": "other_connection",
                    }
                ],
            }
        )
        assert q.joins[0].connection == "other_connection"

    def test_distinct_rejects_count_star(self):
        with pytest.raises(ValueError, match="count\\(\\*\\)"):
            AggregateSelectItem(fn="count", col="*", distinct=True)

    def test_distinct_on_real_column_allowed(self):
        item = AggregateSelectItem(fn="count", col="orders.status", distinct=True)
        assert item.distinct is True

    def test_query_level_distinct_defaults_false(self):
        q = StructuredQuery(from_table="orders", select=["orders.id"])
        assert q.distinct is False

    def test_self_join_without_alias_on_either_side_rejected(self):
        with pytest.raises(ValueError, match="self-join"):
            StructuredQuery(
                from_table="employees",
                select=["employees.id"],
                joins=[JoinSpec(table="employees", on=["employees.manager_id", "employees.id"])],
            )

    def test_self_join_with_one_side_unaliased_still_rejected(self):
        with pytest.raises(ValueError, match="self-join"):
            StructuredQuery(
                from_table="employees",
                select=["employees.id"],
                joins=[JoinSpec(table="employees", alias="m", on=["employees.manager_id", "m.id"])],
            )

    def test_self_join_with_both_sides_aliased_accepted(self):
        q = StructuredQuery(
            from_table="employees",
            from_alias="e",
            select=["e.id", "m.name"],
            joins=[JoinSpec(table="employees", alias="m", on=["e.manager_id", "m.id"])],
        )
        assert q.from_alias == "e"
        assert q.joins[0].alias == "m"

    def test_duplicate_effective_name_rejected(self):
        with pytest.raises(ValueError, match="Duplicate table/alias"):
            StructuredQuery(
                from_table="orders",
                from_alias="o",
                select=["o.id"],
                joins=[JoinSpec(table="customers", alias="o", on=["o.customer_id", "o.id"])],
            )

    def test_plain_join_without_alias_still_works(self):
        """Ordinary (non-self) joins remain unaffected — no alias required."""
        q = StructuredQuery(
            from_table="orders",
            select=["orders.id", "customers.name"],
            joins=[JoinSpec(table="customers", on=["orders.customer_id", "customers.id"])],
        )
        assert q.joins[0].alias is None

    def test_entity_id_field_no_longer_exists(self):
        """entity_id was a customer-specific "scope to Company.ID" concept —
        it's been replaced by policy-level mandatory_row_filters and must not
        exist on the AST.
        """
        with pytest.raises(ValueError):
            StructuredQuery.model_validate(
                {"from": "orders", "select": ["orders.id"], "entity_id": 42}
            )


class TestParseColumnRef:
    def test_valid(self):
        assert parse_column_ref("orders.status") == ("orders", "status")

    def test_bare_name_rejected(self):
        with pytest.raises(ValueError, match="Table.Column"):
            parse_column_ref("status")
