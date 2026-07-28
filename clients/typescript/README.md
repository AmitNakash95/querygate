# @querygate/query-builder (TypeScript)

Typed, fluent builder for QueryGate's `StructuredQuery` AST — the TypeScript
sibling of `src/querygate/client/builder.py` (TODO.md item 51, phase 2a).

This is a pure client-side authoring convenience. It constructs the exact
wire JSON a hand-written REST/MCP `StructuredQuery` body needs, with editor
autocomplete and compile-time type checking in place of raw dicts. It adds
**no** trust of its own: whatever it emits is still fully policy/schema/
guardrail-checked by `StructuredQueryService` before a single row is
touched — the server enforces the same rules regardless of what built this
JSON.

**In-tree only, not yet published.** This package is not on npm — see
TODO.md item 51 (the standalone, dependency-light distribution is coupled to
item 30 phase 2's still-unresolved package-registry choice). Use it by
building from source in this repository.

## Quick start

```bash
cd clients/typescript
npm install
npm run build
```

```ts
import { Query, agg, col, desc } from "./dist/src";

const body = Query.from("orders")
  .join("customers", ["orders.customer_id", "customers.id"])
  .select("customers.name", agg.sum("orders.total_amount", { as: "total_spend" }))
  .where(col("customers.country").eq("GB"))
  .groupBy("customers.name")
  .orderBy("total_spend", { desc: true })
  .limit(10)
  .build(); // -> the StructuredQuery wire JSON to POST to /api/v1/<connection>/query
```

See `examples/client_sdk_typescript.ts` (mirrors `examples/client_sdk_python.py`)
for four complete queries — aggregate/join/filter, a searched CASE with a
scalar function, per-partition top-N, and a window-function running total —
and `test/builder.test.ts`/`test/kitchenSink.test.ts` for the full surface.

## API shape

Every builder call returns the exact field names the server's Pydantic
models expect (`from`, `as`, `else`, `and`/`or`/`not`, `all`, snake_case
elsewhere) — `build()`/`toDict()`/`toJSON()` never need translation before
they hit the wire. The naming is a **structural and behavioral** mirror of
the Python builder, not name-for-name: every multi-word Python name is
renamed snake_case→camelCase per TS convention (`group_by`→`groupBy`,
`order_by`→`orderBy`, `top_n`→`topN`, `col_fn`→`colFn`, `date_bucket`→
`dateBucket`, `string_agg`→`stringAgg`, `array_agg`→`arrayAgg`,
`percentile_cont`→`percentileCont`, `fn_select`→`fnSelect`, `expr_fn`→
`exprFn`, `expr_select`→`exprSelect`, `date_add`→`dateAdd`, `case_expr`→
`caseExpr`, `not_in`→`notIn`, `is_null`/`is_not_null`→`isNull`/`isNotNull`),
on top of the smaller set of renames JavaScript's own grammar forces:

| Python                      | TypeScript                    | Why                                   |
| ---------------------------- | ------------------------------ | -------------------------------------- |
| `col("a") == 1`               | `col("a").eq(1)`                | no operator overloading in JS/TS       |
| `case(...)`                   | `caseSelect(...)`               | `case` is a reserved JS keyword        |
| `Query.from_(...)`            | `Query.from(...)`               | `from` is not reserved in JS            |
| `.except_(...)`               | `.except(...)`                  | `except` is not reserved in JS          |

Everything with a single-word Python name — `col`, `lit`, `fn`, `agg.*`,
`when`, `asc`/`desc`, `expr`/`cast`/`extract`/`now`, `window`/`windowExpr`/
`frame` — keeps that same name; everything else follows the camelCase
renaming above.

**Illegal shapes are rejected at `build()` time**, the same as the Python
builder — self-join aliasing, aggregate `distinct` combinations, window
function arity/frame/order-by rules, set-operation arm shape, percentile
range, scalar-function arity. Because TypeScript is statically typed, a
whole additional class of mistakes the Python builder can only catch at
*runtime* (an un-wrapped bare argument to a scalar function, a `Predicate`
passed to `.select()`) is instead a *compile* error here.

**Every field is always present** in `build()`'s output (`null` for an unset
optional, the declared default for a defaulted one) rather than eliding
`None`/default values the way the Python builder's `to_dict()` does — see
`src/types.ts`'s module doc for why. This stays fully wire-compatible; the
payload is marginally larger, never wrong.

**No CTE/subquery builder support yet** (items 105/106) — the same phase-1
gap the Python builder still has. A real follow-up, not silently dropped.

## Testing

```bash
npm test    # tsc -p tsconfig.json && node --test dist/test/*.test.js
```

Two things are proven:

1. **Coverage** (`test/builder.test.ts`) — a hand-maintained analogue of the
   Python builder's introspective drift guards. TypeScript unions are erased
   at compile time, so there is no automatic "the AST grew a member" trip
   wire the way Python's `typing.get_args` gives; each currently-known shape
   is instead exercised by name.
2. **Cross-language parity** (`test/kitchenSink.test.ts`) — both this builder
   and the Python builder must reproduce the exact same checked-in fixture
   (`tests/fixtures/client_builder_kitchen_sink.json`) for three representative
   queries. The Python side is pinned by
   `tests/unit/test_client_builder_ts_parity.py`. Together these are the
   automatic regression guard for this language pair.
