/**
 * The TypeScript half of the cross-language parity guard (see
 * `tests/unit/test_client_builder_ts_parity.py`'s module doc for the full
 * rationale): both language builders must reproduce the SAME checked-in
 * fixture for three logical queries. `readFileSync` reads the fixture
 * straight from the shared `tests/fixtures/` directory rather than a local
 * copy, so there is exactly one file to keep in sync.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

import {
  Query,
  agg,
  and_,
  asc,
  caseSelect,
  col,
  dateBucket,
  desc,
  exprSelect,
  fnSelect,
  frame,
  lit,
  not_,
  or_,
  when,
  window,
  windowExpr,
} from "../src";

const fixturePath = join(
  __dirname,
  "..",
  "..",
  "..",
  "..",
  "tests",
  "fixtures",
  "client_builder_kitchen_sink.json",
);
const fixture = JSON.parse(readFileSync(fixturePath, "utf-8"));

function buildCore() {
  return Query.from("employees", { alias: "e" })
    .distinct()
    .select(
      "e.name",
      agg.count("*", { distinct: false, as: "n" }),
      caseSelect(
        [
          when(
            and_(col("e.active").eq(true), or_(col("e.dept").eq("eng"), col("e.dept").eq("ops"))),
            lit("core"),
          ),
          when(not_(col("e.active").eq(true)), lit("inactive")),
        ],
        { else: lit("other"), as: "bucket" },
      ),
      exprSelect(col("e.salary").mul(1.1), { as: "salary_with_bonus" }),
      dateBucket("e.hired_at", "month", { as: "hired_month" }),
      fnSelect("coalesce", [col("e.nickname"), lit("n/a")], { as: "display_name" }),
    )
    .join("employees", ["e.manager_id", "m.id"], { alias: "m" })
    .join("departments", undefined, {
      alias: "d",
      condition: [col("d.min_headcount").lte(col("e.dept_size")), col("d.max_headcount").gte(col("e.dept_size"))],
    })
    .join("audit_log", undefined, { type: "cross", alias: "al" })
    .where(
      col("e.name").like("A%"),
      col("e.salary").between(50000, 200000),
      col("e.dept").in_(["eng", "ops", "sales"]),
      col("e.terminated_at").isNull(),
    )
    .groupBy("e.name", "e.dept")
    .having(or_(col("n").gt(1), col("n").eq(0)))
    .orderBy("n", { desc: true, nulls: "last" })
    .orderBy("e.name")
    .limit(25)
    .offset(5)
    .topN(3, { orderBy: [desc("n")], partitionBy: ["e.dept"] })
    .intent("kitchen sink parity fixture — item 51 phase 2")
    .build();
}

function buildWindow() {
  return Query.from("orders")
    .select(
      "orders.id",
      window("sum", col("orders.amount"), {
        as: "running_total",
        partitionBy: ["orders.customer_id"],
        orderBy: [asc("orders.created_at")],
        frame: frame("rows", null, 0),
      }),
      window("row_number", undefined, {
        as: "rn",
        partitionBy: ["orders.customer_id"],
        orderBy: [col("orders.created_at")],
      }),
      window("ntile", undefined, { as: "quartile", orderBy: [col("orders.amount")], buckets: 4 }),
      window("lag", col("orders.amount"), {
        as: "prev_amount",
        orderBy: [col("orders.created_at")],
        offset: 1,
      }),
      exprSelect(
        col("orders.amount").div(
          windowExpr("sum", col("orders.amount"), { partitionBy: ["orders.customer_id"] }),
        ),
        { as: "share_of_customer_total" },
      ),
    )
    .where(col("orders.status").eq("paid"))
    .build();
}

function buildSetOp() {
  return Query.from("orders_2024")
    .select("orders_2024.customer_id", "orders_2024.amount")
    .where(col("orders_2024.amount").gt(0))
    .union([Query.from("orders_2025").select("orders_2025.customer_id", "orders_2025.amount")], {
      all: true,
    })
    .orderBy("customer_id")
    .limit(100)
    .build();
}

test("core scenario matches the shared kitchen-sink fixture", () => {
  assert.deepEqual(buildCore(), fixture.core);
});

test("window scenario matches the shared kitchen-sink fixture", () => {
  assert.deepEqual(buildWindow(), fixture.window);
});

test("set_op scenario matches the shared kitchen-sink fixture", () => {
  assert.deepEqual(buildSetOp(), fixture.set_op);
});
