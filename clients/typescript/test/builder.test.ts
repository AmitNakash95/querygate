/**
 * Coverage tests for the TypeScript client builder (TODO.md item 51, phase 2).
 *
 * This is a hand-maintained analogue of the Python builder's introspective
 * "drift guard" tests (`tests/unit/test_client_builder.py`), not a literal
 * port: TypeScript's union types are erased at compile time, so there is no
 * `typing.get_args`-equivalent that can automatically fail when the AST
 * gains a new member. Each test below instead exercises every CURRENTLY
 * KNOWN shape by name — a human adding a new AST member must remember to
 * extend this file, the same way they must remember to add a builder
 * helper in the first place. The one AUTOMATIC cross-language regression
 * guard is `kitchenSink.test.ts`, which pins both builders to a single
 * checked-in fixture.
 */
import assert from "node:assert/strict";
import test from "node:test";

import {
  Query,
  agg,
  and_,
  arrayAgg,
  asc,
  caseExpr,
  caseSelect,
  cast,
  col,
  colFn,
  dateAdd,
  dateBucket,
  desc,
  expr,
  exprFn,
  exprSelect,
  extract,
  fn,
  fnSelect,
  frame,
  lit,
  not_,
  now,
  or_,
  percentileCont,
  stringAgg,
  when,
  window,
  windowExpr,
  type SelectItem,
} from "../src";

// --------------------------------------------------------------------------
// Fidelity
// --------------------------------------------------------------------------

test("builder matches a hand-written wire object", () => {
  const built = Query.from("orders")
    .join("customers", ["orders.customer_id", "customers.id"])
    .select("customers.name", agg.sum("orders.total_amount", { as: "total_spend" }))
    .where(col("customers.country").eq("GB"))
    .groupBy("customers.name")
    .orderBy("total_spend", { desc: true })
    .limit(10)
    .build();

  assert.equal(built.from, "orders");
  assert.equal(built.select.length, 2);
  assert.deepEqual(built.select[1], { fn: "sum", col: "orders.total_amount", arg: null, distinct: false, as: "total_spend" });
  assert.deepEqual(built.where, {
    col: "customers.country",
    expr: null,
    col_fn: null,
    op: "eq",
    value: "GB",
    value_col: null,
    value_expr: null,
    value_subquery: null,
    exists_subquery: null,
  });
  assert.equal(built.limit, 10);
  assert.deepEqual(built.order_by, [{ col: "total_spend", dir: "desc", nulls: null }]);
});

test("multiple where calls are AND-combined", () => {
  const built = Query.from("t").select("t.c").where(col("t.a").eq(1)).where(col("t.b").eq(2)).build();
  assert.ok(built.where && "and" in built.where && built.where.and?.length === 2);
});

test("a single where predicate is not wrapped in a group", () => {
  const built = Query.from("t").select("t.c").where(col("t.a").eq(1)).build();
  assert.equal((built.where as { op?: string }).op, "eq");
});

test("column-to-column comparison uses value_col", () => {
  const p = col("a").gt(col("b"));
  assert.deepEqual(p, {
    col: "a",
    expr: null,
    col_fn: null,
    op: "gt",
    value: null,
    value_col: "b",
    value_expr: null,
    value_subquery: null,
    exists_subquery: null,
  });
});

test("boolean groups and not_ nest correctly", () => {
  const node = and_(col("a").eq(1), or_(col("b").eq(2), col("c").eq(3)), not_(col("d").isNull()));
  assert.ok(node.and);
  assert.equal(node.and!.length, 3);
  assert.ok("or" in node.and![1]! && (node.and![1] as { or: unknown }).or);
  assert.ok("not" in node.and![2]! && (node.and![2] as { not: unknown }).not);
});

test("scalar fn predicate and projection", () => {
  const predicate = fn("lower", col("customers.name")).eq("ada");
  assert.deepEqual(predicate.col_fn, { fn: "lower", args: [{ col: "customers.name" }] });
  const projection = fnSelect("coalesce", [col("c.nickname"), lit("n/a")], { as: "display_name" });
  assert.equal(projection.fn, "coalesce");
  assert.equal(projection.as, "display_name");
});

test("case with else and literal/column branches", () => {
  const item = caseSelect(
    [when(col("o.status").eq("paid"), col("o.amount")), when(col("o.status").eq("void"), lit(0))],
    { else: lit(-1), as: "k" },
  );
  assert.equal(item.as, "k");
  assert.deepEqual(item.else, { literal: -1 });
  assert.equal(item.when.length, 2);
});

test("searched having with an or_ group builds", () => {
  const built = Query.from("t")
    .select("t.k", agg.count("*", { as: "n" }))
    .groupBy("t.k")
    .having(or_(col("n").gt(1), col("n").eq(0)))
    .build();
  assert.ok(built.having && "or" in built.having);
});

test("top_n with partition and ordering helpers", () => {
  const built = Query.from("t")
    .select("t.a")
    .topN(3, { orderBy: [desc("t.score")], partitionBy: ["t.group"] })
    .build();
  assert.deepEqual(built.top_n, {
    partition_by: ["t.group"],
    order_by: [{ col: "t.score", dir: "desc", nulls: null }],
    n: 3,
    fn: "row_number",
  });
});

test("topN requires at least one orderBy entry", () => {
  assert.throws(
    () => Query.from("t").select("t.a").topN(3, { orderBy: [] }).build(),
    /at least one orderBy/,
  );
});

test("union/intersect/except require at least one arm", () => {
  assert.throws(() => Query.from("a").select("a.x").union([]).build(), /at least one arm/);
  assert.throws(() => Query.from("a").select("a.x").intersect([]).build(), /at least one arm/);
  assert.throws(() => Query.from("a").select("a.x").except([]).build(), /at least one arm/);
});

test("self-join needs alias and from_alias is expressible", () => {
  const built = Query.from("employees", { alias: "e" })
    .select("e.name")
    .join("employees", ["e.manager_id", "m.id"], { alias: "m" })
    .build();
  assert.equal(built.from_alias, "e");
  assert.equal(built.joins[0]!.alias, "m");
});

test("distinct and offset and composite join", () => {
  const built = Query.from("a")
    .distinct()
    .select("a.x")
    .join("b", ["a.k1", "b.k1"], { extraOn: [["a.k2", "b.k2"]] })
    .offset(7)
    .build();
  assert.equal(built.distinct, true);
  assert.equal(built.offset, 7);
  assert.deepEqual(built.joins[0]!.extra_on, [["a.k2", "b.k2"]]);
});

// --------------------------------------------------------------------------
// Illegal shapes
// --------------------------------------------------------------------------

test("between builds a two-element value list", () => {
  const p = col("a").between(1, 10);
  assert.deepEqual(p.value, [1, 10]);
});

test("in_/notIn reject an empty list", () => {
  assert.throws(() => col("a").in_([]));
  assert.throws(() => col("a").notIn([]));
});

test("self-join missing an alias raises", () => {
  assert.throws(
    () => Query.from("employees").select("employees.name").join("employees", ["a", "b"]).build(),
    /self-join/,
  );
});

test("percentile fraction out of range raises on both bounds", () => {
  assert.throws(() => percentileCont("t.c", 1.5));
  assert.throws(() => percentileCont("t.c", -0.5));
});

test("scalar fn arity is enforced", () => {
  assert.throws(() => fn("lower", col("a"), col("b")));
  assert.throws(() => fn("coalesce", col("a")));
});

test("scalar fn argument type is enforced (a literal where lower/upper/trim need a column)", () => {
  assert.throws(() => fn("lower", lit("x")), /exactly one column argument/);
});

test("expr fn arity is enforced", () => {
  assert.throws(() => exprFn("abs", col("a"), col("b")));
  assert.throws(() => exprFn("substring", col("a"), lit(1)));
});

test("count(*) with distinct is rejected the same way the server would, however '*' is spelled", () => {
  assert.throws(() => agg.count("*", { distinct: true }));
  assert.throws(() => agg.count(col("*"), { distinct: true }));
});

test("aggregate over a computed expression requires an alias", () => {
  assert.throws(() => agg.sum(expr(col("a").mul(col("b")))));
  assert.doesNotThrow(() => agg.sum(expr(col("a").mul(col("b"))), { as: "total" }));
});

test("cross join takes neither on nor condition", () => {
  assert.throws(
    () => Query.from("a").select("a.x").join("b", ["a.k", "b.k"], { type: "cross" }).build(),
    /cross/,
  );
});

test("join with neither on nor condition raises", () => {
  assert.throws(() => Query.from("a").select("a.x").join("b").build(), /exactly one of/);
});

test("join with both on and condition raises", () => {
  assert.throws(
    () =>
      Query.from("a")
        .select("a.x")
        .join("b", ["a.k", "b.k"], { condition: [col("a.k").eq(col("b.k"))] })
        .build(),
    /exactly one of/,
  );
});

test("extraOn cannot be used together with condition", () => {
  assert.throws(
    () =>
      Query.from("a")
        .select("a.x")
        .join("b", undefined, {
          condition: [col("a.k").eq(col("b.k"))],
          extraOn: [["a.k2", "b.k2"]],
        })
        .build(),
    /extraOn/,
  );
});

test("two different tables sharing the same alias raises a duplicate-alias error, not a self-join error", () => {
  assert.throws(
    () =>
      Query.from("a", { alias: "x" })
        .select("a.c")
        .join("b", ["a.k", "b.k"], { alias: "x" })
        .build(),
    /Duplicate table\/alias/,
  );
});

test("window function arity/order-by rules are enforced", () => {
  assert.throws(() => window("row_number", col("a"), { as: "rn" }), /takes no 'arg'/);
  assert.throws(() => window("sum", undefined, { as: "s" }), /requires an 'arg'/);
  assert.throws(() => window("rank", undefined, { as: "r" }), /requires over.orderBy/);
  assert.doesNotThrow(() => window("rank", undefined, { as: "r", orderBy: [col("a")] }));
});

test("a window function cannot combine with group_by", () => {
  assert.throws(
    () =>
      Query.from("t")
        .select("t.k", window("sum", col("t.x"), { as: "rn" }))
        .groupBy("t.k")
        .build(),
    /cannot be combined with group_by or aggregate select items/,
  );
});

test("a window function cannot combine with any aggregate-family select item (not just plain aggregates)", () => {
  // A window with a well-formed frame of its own (no orderBy needed for `sum`
  // without a frame), so a throw here can only come from the exclusivity
  // check under test, never from window()'s own unrelated arity rules.
  const windowItem = () => window("sum", col("t.x"), { as: "rn" });
  const withWindow = (sibling: Exclude<SelectItem, string>) =>
    Query.from("t").select(windowItem(), sibling).build();
  const rejects = /cannot be combined with group_by or aggregate select items/;
  assert.throws(() => withWindow(agg.sum("t.a", { as: "s" })), rejects);
  assert.throws(() => withWindow(stringAgg("t.b", ", ")), rejects);
  assert.throws(() => withWindow(arrayAgg("t.c")), rejects);
  assert.throws(() => withWindow(percentileCont("t.d", 0.5)), rejects);
  // Control: the same window alongside a NON-aggregate sibling must NOT throw.
  assert.doesNotThrow(() => withWindow(dateBucket("t.e", "month")));
});

test("a set_op arm may not carry its own order_by/limit", () => {
  assert.throws(() =>
    Query.from("a")
      .select("a.x")
      .union([Query.from("b").select("b.x").limit(1)])
      .build(),
  );
});

test("top_n cannot combine with set_op", () => {
  assert.throws(() =>
    Query.from("a")
      .select("a.x")
      .topN(1, { orderBy: [asc("a.x")] })
      .union([Query.from("b").select("b.x")])
      .build(),
  );
});

// --------------------------------------------------------------------------
// Reachability (hand-maintained coverage — see module doc)
// --------------------------------------------------------------------------

test("every CompareOp is reachable through the Comparable DSL", () => {
  const c = col("t.c");
  const reachable = new Set(
    [
      c.eq(1),
      c.neq(1),
      c.lt(1),
      c.lte(1),
      c.gt(1),
      c.gte(1),
      c.in_([1]),
      c.notIn([1]),
      c.like("a%"),
      c.between(1, 2),
      c.isNull(),
      c.isNotNull(),
    ].map((p) => p.op),
  );
  const expected = new Set(["eq", "neq", "lt", "lte", "gt", "gte", "in", "not_in", "like", "between", "is_null", "is_not_null"]);
  assert.deepEqual(reachable, expected);
});

test("every AggregateFn is reachable", () => {
  const reachable = new Set(
    [
      agg.count("*"),
      agg.sum("t.c"),
      agg.avg("t.c"),
      agg.min("t.c"),
      agg.max("t.c"),
      agg.stddev("t.c"),
      agg.variance("t.c"),
    ].map((a) => a.fn),
  );
  assert.deepEqual(reachable, new Set(["count", "sum", "avg", "min", "max", "stddev", "variance"]));
});

test("every non-string SelectItem shape is constructible", () => {
  const items = [
    agg.count("*"),
    dateBucket("orders.created_at", "month"),
    stringAgg("customers.email", ", "),
    arrayAgg("order_items.product_name"),
    percentileCont("orders.total_amount", 0.5),
    fnSelect("upper", [col("customers.name")]),
    caseSelect([when(col("orders.id").eq(1), lit("x"))], { as: "k" }),
    exprSelect(col("order_items.quantity").mul(col("order_items.price")), { as: "line" }),
    window("sum", col("orders.total_amount"), {
      as: "running_total",
      orderBy: [asc("orders.created_at")],
      frame: frame("rows", null, 0),
    }),
  ];
  assert.equal(items.length, 9);
});

test("every Expression member is constructible", () => {
  const members = [
    col("a"), // ColumnExpr via toExpression, exercised through expr()
    lit("x"),
    col("a").add(1), // BinaryOpExpr
    exprFn("lower", col("a")), // FunctionExpr
    cast(col("a"), "text"), // CastExpr
    caseExpr([when(col("a").eq(1), lit("y"))]), // CaseExpr
    extract("hour", col("a")), // ExtractExpr
    now("date"), // NowExpr
    dateAdd(now(), "day", -7), // DateAddExpr
    windowExpr("sum", col("a")), // WindowExpr
  ];
  assert.equal(members.length, 10);
});

test("date helpers wire their arguments to the right fields", () => {
  const e = dateAdd(now("timestamp"), "day", -7);
  assert.deepEqual(e.node, { date_add: { now: "timestamp" }, unit: "day", amount: -7 });
  const x = extract("dayofweek", col("o.created_at"));
  assert.deepEqual(x.node, { extract: { col: "o.created_at" }, part: "dayofweek" });
});

test("colFn is an alias of fn", () => {
  assert.equal(colFn("upper", col("a")).call.fn, fn("upper", col("a")).call.fn);
});

test("union/intersect/except are all expressible with order_by/limit on the carrying query", () => {
  const arm = () => Query.from("b").select("b.x");
  const u = Query.from("a").select("a.x").union([arm()], { all: true }).orderBy("x").limit(5).build();
  assert.equal(u.set_op!.op, "union");
  assert.equal(u.set_op!.all, true);
  const i = Query.from("a").select("a.x").intersect([arm()]).build();
  assert.equal(i.set_op!.op, "intersect");
  const e = Query.from("a").select("a.x").except([arm()]).build();
  assert.equal(e.set_op!.op, "except");
});

test("purpose builder serializes the declared value", () => {
  // TODO.md item 146 test-contract gap (found by `test-contract-reviewer`,
  // 2026-08-05): the kitchen-sink parity fixture only ever leaves `purpose`
  // unset (null), so a bug dropping `.purpose()`'s value would go
  // undetected anywhere in this suite.
  const built = Query.from("orders").select("orders.id").purpose("fraud_review").build();
  assert.equal(built.purpose, "fraud_review");
});
