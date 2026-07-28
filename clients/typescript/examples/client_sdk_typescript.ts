/**
 * Author QueryGate structured queries with the typed TypeScript builder
 * (TODO.md item 51, phase 2a) — the TypeScript sibling of
 * `examples/client_sdk_python.py`. Same four example queries, same shape.
 *
 * No server or network is needed to *build* a query — everything through
 * `.toDict()` runs fully offline and is what `test/kitchenSink.test.ts` and
 * `test/builder.test.ts` verify. Sending one needs a running QueryGate (see
 * README.md: `poetry run uvicorn querygate.api.app:app`, default demo
 * connection `demo`); the optional `--send` path uses Node's built-in
 * `fetch` and is the only part that needs the server.
 *
 * Run (from `clients/typescript/`):
 *   npm run build && node dist/examples/client_sdk_typescript.js
 *   node dist/examples/client_sdk_typescript.js --send
 */
import {
  Query,
  agg,
  and_,
  asc,
  caseSelect,
  col,
  desc,
  fnSelect,
  frame,
  lit,
  when,
  window,
  type StructuredQuery,
} from "../src";

// NOTE: the demo policy (examples/policy.example.yaml) masks
// orders.total_amount so it may only appear as a bare SELECT projection, and
// denies customers.email. These examples deliberately stay within that
// policy so `--send` is a clean happy path — but the point of QueryGate is
// that the *server* enforces this regardless of what the builder emits.

function ordersPerCustomer(): StructuredQuery {
  return Query.from("orders")
    .join("customers", ["orders.customer_id", "customers.id"])
    .select("customers.name", agg.count("*", { as: "order_count" }))
    .where(col("customers.country").eq("GB"))
    .where(col("orders.status").eq("completed"))
    .groupBy("customers.name")
    .orderBy("order_count", { desc: true })
    .limit(5)
    .intent("GB customers ranked by completed-order count")
    .build();
}

function orderItemValueBuckets(): StructuredQuery {
  return Query.from("order_items")
    .select(
      "order_items.id",
      fnSelect("upper", [col("order_items.product_name")], { as: "product_upper" }),
      caseSelect(
        [
          when(col("order_items.quantity").gte(100), lit("bulk")),
          when(col("order_items.quantity").gte(10), lit("wholesale")),
        ],
        { else: lit("retail"), as: "tier" },
      ),
    )
    .where(
      and_(col("order_items.unit_price").gt(0), col("order_items.quantity").isNotNull()),
    )
    .orderBy("order_items.quantity", { desc: true })
    .limit(20)
    .build();
}

function topLineItemsPerOrder(): StructuredQuery {
  return Query.from("order_items")
    .select("order_items.order_id", agg.avg("order_items.unit_price", { as: "avg_price" }))
    .groupBy("order_items.order_id")
    .topN(3, { orderBy: [desc("avg_price")] })
    .build();
}

function runningOrderValue(): StructuredQuery {
  return Query.from("order_items")
    .select(
      "order_items.order_id",
      "order_items.id",
      window("sum", col("order_items.quantity").mul(col("order_items.unit_price")), {
        as: "running_order_value",
        partitionBy: ["order_items.order_id"],
        orderBy: [asc("order_items.id")],
        frame: frame("rows", null, 0),
      }),
    )
    .orderBy("order_items.id")
    .build();
}

const EXAMPLES: Record<string, () => StructuredQuery> = {
  ordersPerCustomer,
  orderItemValueBuckets,
  topLineItemsPerOrder,
  runningOrderValue,
};

async function send(connection: string, body: StructuredQuery): Promise<void> {
  const token = process.env["QUERYGATE_API_KEY"];
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  const url = `http://localhost:8000/api/v1/${connection}/query`;
  const resp = await fetch(url, { method: "POST", headers, body: JSON.stringify(body) });
  console.log(`  -> HTTP ${resp.status}`);
  const text = await resp.text();
  console.log(" ", text.slice(0, 800));
}

async function main(): Promise<void> {
  const shouldSend = process.argv.includes("--send");
  for (const [name, factory] of Object.entries(EXAMPLES)) {
    const body = factory();
    console.log(`\n# ${name}`);
    console.log(JSON.stringify(body, null, 2));
    if (shouldSend) {
      await send("demo", body);
    }
  }
}

main().catch((err: unknown) => {
  console.error(err);
  process.exitCode = 1;
});
