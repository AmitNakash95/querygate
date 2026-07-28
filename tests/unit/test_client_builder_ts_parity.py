"""Cross-language parity between the Python and TypeScript client builders
(TODO.md item 51, phase 2a).

The TypeScript builder (`clients/typescript/`) cannot reuse the Python
Pydantic models, so it has its own sync-test strategy rather than Python's
introspective drift guards (`test_client_builder.py`'s `typing.get_args`
checks have no TypeScript runtime equivalent — TS unions are erased at
compile time). This is the other half of that strategy: a checked-in
fixture (`tests/fixtures/client_builder_kitchen_sink.json`) that both
languages' builders must reproduce for the SAME three logical queries
(a wide non-aggregate/aggregate/CASE/join mix, a window-function query, and a
set-operation query). This test pins the fixture to the real Pydantic
models — if it goes stale, this fails on the Python side; the TypeScript
side (`clients/typescript/test/kitchenSink.test.ts`) is the other half of the
same guard.

Deliberately a FULL, unexcluded `model_dump` (not `to_dict()`'s trimmed
form): the TypeScript builder emits every declared field explicitly rather
than eliding None/default values (see `clients/typescript/src/types.ts`'s
module doc for why), so comparing the untrimmed shape is what lets both
sides agree without either language special-casing default elision.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from querygate.client import (
    Query,
    agg,
    and_,
    asc,
    case,
    col,
    date_bucket,
    desc,
    expr_select,
    fn_select,
    frame,
    lit,
    not_,
    or_,
    when,
    window,
    window_expr,
)

pytestmark = pytest.mark.unit

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "client_builder_kitchen_sink.json"


def _build_core() -> dict:
    q = (
        Query.from_("employees", alias="e")
        .distinct()
        .select(
            "e.name",
            agg.count("*", distinct=False, as_="n"),
            case(
                when(
                    and_(
                        col("e.active") == True,  # noqa: E712
                        or_(col("e.dept") == "eng", col("e.dept") == "ops"),
                    ),
                    lit("core"),
                ),
                when(not_(col("e.active") == True), lit("inactive")),  # noqa: E712
                else_=lit("other"),
                as_="bucket",
            ),
            expr_select(col("e.salary") * 1.1, as_="salary_with_bonus"),
            date_bucket("e.hired_at", "month", as_="hired_month"),
            fn_select("coalesce", col("e.nickname"), lit("n/a"), as_="display_name"),
        )
        .join("employees", on=("e.manager_id", "m.id"), alias="m")
        .join(
            "departments",
            alias="d",
            condition=[
                col("d.min_headcount") <= col("e.dept_size"),
                col("d.max_headcount") >= col("e.dept_size"),
            ],
        )
        .join("audit_log", type="cross", alias="al")
        .where(
            col("e.name").like("A%"),
            col("e.salary").between(50000, 200000),
            col("e.dept").in_(["eng", "ops", "sales"]),
            col("e.terminated_at").is_null(),
        )
        .group_by("e.name", "e.dept")
        .having(or_(col("n") > 1, col("n") == 0))
        .order_by("n", desc=True, nulls="last")
        .order_by("e.name")
        .limit(25)
        .offset(5)
        .top_n(3, order_by=[desc("n")], partition_by=["e.dept"])
        .intent("kitchen sink parity fixture — item 51 phase 2")
    )
    return q.build().model_dump(by_alias=True)


def _build_window() -> dict:
    q = (
        Query.from_("orders")
        .select(
            "orders.id",
            window(
                "sum",
                col("orders.amount"),
                as_="running_total",
                partition_by=["orders.customer_id"],
                order_by=[asc("orders.created_at")],
                frame=frame("rows", None, 0),
            ),
            window(
                "row_number",
                as_="rn",
                partition_by=["orders.customer_id"],
                order_by=[col("orders.created_at")],
            ),
            window("ntile", as_="quartile", order_by=[col("orders.amount")], buckets=4),
            window(
                "lag",
                col("orders.amount"),
                as_="prev_amount",
                order_by=[col("orders.created_at")],
                offset=1,
            ),
            expr_select(
                col("orders.amount")
                / window_expr("sum", col("orders.amount"), partition_by=["orders.customer_id"]),
                as_="share_of_customer_total",
            ),
        )
        .where(col("orders.status") == "paid")
    )
    return q.build().model_dump(by_alias=True)


def _build_set_op() -> dict:
    q = (
        Query.from_("orders_2024")
        .select("orders_2024.customer_id", "orders_2024.amount")
        .where(col("orders_2024.amount") > 0)
        .union(
            Query.from_("orders_2025").select("orders_2025.customer_id", "orders_2025.amount"),
            all=True,
        )
        .order_by("customer_id")
        .limit(100)
    )
    return q.build().model_dump(by_alias=True)


@pytest.fixture(scope="module")
def fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def test_core_scenario_matches_fixture(fixture: dict) -> None:
    assert _build_core() == fixture["core"]


def test_window_scenario_matches_fixture(fixture: dict) -> None:
    assert _build_window() == fixture["window"]


def test_set_op_scenario_matches_fixture(fixture: dict) -> None:
    assert _build_set_op() == fixture["set_op"]
