"""Unit tests for the StructuredQuery AST."""

from __future__ import annotations

import pytest

from querygate.query_ast.models import (
    AggregateSelectItem,
    ArrayAggSelectItem,
    CaseSelectItem,
    JoinSpec,
    OrderBySpec,
    Predicate,
    ScalarFunctionCall,
    ScalarFunctionSelectItem,
    StringAggSelectItem,
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

    def test_distinct_rejected_with_stddev(self):
        with pytest.raises(ValueError, match="distinct is not valid with stddev"):
            AggregateSelectItem(fn="stddev", col="orders.total_amount", distinct=True)

    def test_distinct_rejected_with_variance(self):
        with pytest.raises(ValueError, match="distinct is not valid with variance"):
            AggregateSelectItem(fn="variance", col="orders.total_amount", distinct=True)

    def test_stddev_without_distinct_accepted(self):
        item = AggregateSelectItem(fn="stddev", col="orders.total_amount")
        assert item.fn == "stddev"

    def test_query_level_distinct_defaults_false(self):
        q = StructuredQuery(from_table="orders", select=["orders.id"])
        assert q.distinct is False

    def test_string_agg_select_item_valid(self):
        item = StringAggSelectItem(col="customers.email", delimiter=", ", alias="emails")
        assert item.col == "customers.email"
        assert item.delimiter == ", "
        assert item.alias == "emails"

    def test_string_agg_select_item_alias_optional(self):
        item = StringAggSelectItem(col="customers.email", delimiter=", ")
        assert item.alias is None

    def test_string_agg_rejects_star(self):
        with pytest.raises(ValueError, match="requires a real column"):
            StringAggSelectItem(col="*", delimiter=", ")

    def test_array_agg_select_item_valid(self):
        item = ArrayAggSelectItem(col="orders.status", alias="statuses")
        assert item.col == "orders.status"
        assert item.alias == "statuses"

    def test_array_agg_select_item_alias_optional(self):
        item = ArrayAggSelectItem(col="orders.status")
        assert item.alias is None

    def test_array_agg_rejects_star(self):
        with pytest.raises(ValueError, match="requires a real column"):
            ArrayAggSelectItem(col="*")

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

    def test_not_group_accepted(self):
        g = WhereGroup(not_terms=Predicate(col="orders.status", op="eq", value="x"))
        assert g.not_terms is not None
        assert g.and_terms is None
        assert g.or_terms is None

    def test_not_plus_and_both_set_rejected(self):
        with pytest.raises(ValueError, match="exactly one"):
            WhereGroup(
                and_terms=[Predicate(col="orders.status", op="eq", value="x")],
                not_terms=Predicate(col="orders.id", op="eq", value=1),
            )

    def test_where_group_with_none_set_rejected(self):
        with pytest.raises(ValueError, match="exactly one"):
            WhereGroup()

    def test_value_col_accepted_for_comparison_ops(self):
        p = Predicate(col="orders.price", op="gt", value_col="orders.cost")
        assert p.value_col == "orders.cost"
        assert p.value is None

    def test_value_col_and_value_both_set_rejected(self):
        with pytest.raises(ValueError, match="both"):
            Predicate(col="orders.price", op="gt", value=5, value_col="orders.cost")

    def test_value_col_rejected_for_in_op(self):
        with pytest.raises(ValueError, match="value_col is not valid"):
            Predicate(col="orders.status", op="in", value_col="orders.other_status")

    def test_value_col_rejected_for_like_op(self):
        with pytest.raises(ValueError, match="value_col is not valid"):
            Predicate(col="orders.name", op="like", value_col="orders.other_name")

    def test_value_col_rejected_with_is_null(self):
        with pytest.raises(ValueError, match="must not include"):
            Predicate(col="orders.status", op="is_null", value_col="orders.other")

    def test_neither_value_nor_value_col_rejected(self):
        with pytest.raises(ValueError, match="requires a value"):
            Predicate(col="orders.status", op="eq")

    def test_lower_requires_exactly_one_column_arg(self):
        with pytest.raises(ValueError, match="exactly one column argument"):
            ScalarFunctionSelectItem(fn="lower", args=[{"literal": "x"}])

    def test_lower_rejects_extra_args(self):
        with pytest.raises(ValueError, match="exactly one column argument"):
            ScalarFunctionSelectItem(
                fn="lower", args=[{"col": "orders.status"}, {"col": "orders.id"}]
            )

    def test_coalesce_requires_at_least_two_args(self):
        with pytest.raises(ValueError, match="at least 2 arguments"):
            ScalarFunctionSelectItem(fn="coalesce", args=[{"col": "orders.discount"}])

    def test_coalesce_with_two_args_accepted(self):
        item = ScalarFunctionSelectItem(
            fn="coalesce", args=[{"col": "orders.discount"}, {"literal": 0}]
        )
        assert len(item.args) == 2

    def test_case_alias_required(self):
        with pytest.raises(ValueError):
            CaseSelectItem.model_validate(
                {
                    "when": [
                        {
                            "when": {"col": "orders.status", "op": "eq", "value": "x"},
                            "then": {"literal": "y"},
                        }
                    ]
                }
            )

    def test_case_accepts_else_and_alias(self):
        item = CaseSelectItem(
            when=[
                {
                    "when": {"col": "orders.status", "op": "eq", "value": "x"},
                    "then": {"literal": "y"},
                }
            ],
            else_={"literal": "z"},
            alias="label",
        )
        assert item.alias == "label"
        assert item.else_.literal == "z"

    def test_select_item_discriminates_between_scalar_function_and_case(self):
        q = StructuredQuery(
            from_table="orders",
            select=[
                {"fn": "upper", "args": [{"col": "orders.status"}], "as": "s"},
                {
                    "when": [
                        {
                            "when": {"col": "orders.id", "op": "gt", "value": 0},
                            "then": {"literal": "pos"},
                        }
                    ],
                    "as": "label",
                },
            ],
        )
        assert isinstance(q.select[0], ScalarFunctionSelectItem)
        assert isinstance(q.select[1], CaseSelectItem)

    def test_predicate_col_fn_accepted(self):
        p = Predicate(
            col_fn={"fn": "lower", "args": [{"col": "customers.email"}]}, op="eq", value="a@b.com"
        )
        assert p.col is None
        assert p.col_fn.fn == "lower"

    def test_predicate_col_and_col_fn_both_set_rejected(self):
        with pytest.raises(ValueError, match="exactly one of 'col' or 'col_fn'"):
            Predicate(
                col="customers.email",
                col_fn={"fn": "lower", "args": [{"col": "customers.email"}]},
                op="eq",
                value="x",
            )

    def test_predicate_neither_col_nor_col_fn_rejected(self):
        with pytest.raises(ValueError, match="exactly one of 'col' or 'col_fn'"):
            Predicate(op="eq", value="x")

    def test_predicate_col_fn_works_with_in_op(self):
        p = Predicate(
            col_fn={"fn": "lower", "args": [{"col": "orders.status"}]},
            op="in",
            value=["completed", "pending"],
        )
        assert p.col_fn is not None

    def test_scalar_function_call_rejects_nested_function(self):
        with pytest.raises(ValueError):
            ScalarFunctionCall.model_validate(
                {"fn": "coalesce", "args": [{"fn": "lower", "args": [{"col": "a.b"}]}]}
            )

    def test_case_when_predicate_supports_col_fn(self):
        item = CaseSelectItem(
            when=[
                {
                    "when": {
                        "col_fn": {"fn": "lower", "args": [{"col": "orders.status"}]},
                        "op": "eq",
                        "value": "active",
                    },
                    "then": {"literal": "Active"},
                }
            ],
            alias="label",
        )
        assert item.when[0].when.col_fn.fn == "lower"

    def test_extra_on_accepts_valid_pairs(self):
        j = JoinSpec(
            table="order_items",
            on=["orders.tenant_id", "order_items.tenant_id"],
            extra_on=[["orders.id", "order_items.order_id"]],
        )
        assert j.extra_on == [["orders.id", "order_items.order_id"]]

    def test_extra_on_rejects_pair_not_length_two(self):
        with pytest.raises(ValueError, match="two-element"):
            JoinSpec(
                table="order_items",
                on=["orders.tenant_id", "order_items.tenant_id"],
                extra_on=[["orders.id"]],
            )

    def test_extra_on_defaults_empty(self):
        j = JoinSpec(table="customers", on=["orders.customer_id", "customers.id"])
        assert j.extra_on == []

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
