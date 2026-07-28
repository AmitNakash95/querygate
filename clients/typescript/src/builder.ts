/**
 * Typed, fluent builder for QueryGate's `StructuredQuery` AST (TODO.md item 51,
 * phase 2a — the TypeScript sibling of `src/querygate/client/builder.py`).
 *
 * This mirrors the Python builder's public surface and porting the same
 * illegal-shape checks it runs at `build()` time, so a caller gets the same
 * early, clear rejection here that the server's Pydantic models would give —
 * NEVER a server-side check this builder skips. Unlike the Python builder,
 * this cannot literally re-instantiate the server's models (they're Python),
 * so equivalence here means: (a) TypeScript's own type system rejects a
 * whole class of mistakes the Python builder can only catch at runtime
 * (wrong field shape, an un-wrapped bare argument, a Predicate passed to
 * `.select()`) at COMPILE time instead, and (b) the handful of genuinely
 * cross-field business rules and single-field bounds Pydantic's
 * `model_validator`s/`Field(...)` constraints enforce (self-join aliasing,
 * window function arity, aggregate/distinct combinations, set-op arm shape,
 * a percentile fraction's [0,1] range, a non-empty order-by/arms list …) are
 * reproduced here as explicit runtime checks with the same rejection
 * message — NOT an exhaustive port of every such Pydantic constraint, only
 * the ones a caller could plausibly hit through this builder's own public
 * API. Whatever this builder emits is still fully policy/schema/
 * guardrail-checked by `StructuredQueryService` before a row is touched —
 * this adds no trust of its own.
 *
 * `build()`/`toDict()`/`toJSON()` emit the COMPLETE shape (every declared
 * field present) rather than eliding None/default values the way the Python
 * builder's `to_dict()` does — see `types.ts`'s module doc for why. This
 * stays fully wire-compatible; the payload is marginally larger, never wrong.
 *
 * No CTE/subquery builder support yet (items 105/106) — the same phase-1 gap
 * the Python builder still has; a real follow-up, not silently dropped.
 */

import type {
  AggregateFn,
  AggregateSelectItem,
  ArrayAggSelectItem,
  BinaryOp,
  BinaryOpExpr,
  CaseExpr,
  CaseSelectItem,
  CaseWhen,
  CastExpr,
  CastType,
  ColumnExpr,
  CompareOp,
  DateAddExpr,
  DateBucketSelectItem,
  DateGranularity,
  DatePart,
  Expression,
  ExpressionSelectItem,
  ExprFn,
  ExtractExpr,
  IntervalUnit,
  JoinSpec,
  JoinType,
  LiteralExpr,
  NowExpr,
  OrderBySpec,
  PercentileContSelectItem,
  Predicate,
  RankFn,
  Scalar,
  ScalarFn,
  ScalarFunctionArg,
  ScalarFunctionCall,
  ScalarFunctionSelectItem,
  SelectItem,
  SetOpKind,
  SetOpSpec,
  SortDir,
  StringAggSelectItem,
  StructuredQuery,
  TopNSpec,
  WhereGroup,
  WhereNode,
  WindowBound,
  WindowBoundKind,
  WindowCall,
  WindowFn,
  WindowFrame,
  WindowFrameMode,
  WindowSelectItem,
  WindowSpec,
} from "./types";

// --------------------------------------------------------------------------
// Value wrappers
// --------------------------------------------------------------------------

/** An explicit literal value, disambiguating it from a column reference in
 * scalar-function arguments and CASE branches. Prefer `lit(...)`. */
export class LiteralValue {
  constructor(public readonly value: Scalar) {}
}

/** Wrap a literal scalar for use as a scalar-function argument, CASE result,
 * or expression-function argument, e.g. `fn("coalesce", col("a"), lit("n/a"))`. */
export function lit(value: Scalar): LiteralValue {
  return new LiteralValue(value);
}

type PredicateTarget = Pick<Predicate, "col" | "col_fn" | "expr">;

const EMPTY_TARGET: PredicateTarget = { col: null, col_fn: null, expr: null };

/** Shared comparison surface for a column or a scalar-function-of-a-column
 * or a built expression; every method returns a fully-built `Predicate`. */
export abstract class Comparable {
  protected abstract target(): PredicateTarget;

  private predicate(
    op: ReadableOp,
    overrides: Partial<
      Pick<Predicate, "value" | "value_col" | "value_expr">
    > = {},
  ): Predicate {
    return {
      ...EMPTY_TARGET,
      ...this.target(),
      op,
      value: null,
      value_col: null,
      value_expr: null,
      value_subquery: null,
      exists_subquery: null,
      ...overrides,
    };
  }

  private cmp(op: CompareOp, other: Operand): Predicate {
    if (other instanceof Column) {
      return this.predicate(op, { value_col: other.name });
    }
    if (other instanceof Expr) {
      return this.predicate(op, { value_expr: other.node });
    }
    return this.predicate(op, { value: unwrap(other) });
  }

  eq(other: Operand): Predicate {
    return this.cmp("eq", other);
  }
  neq(other: Operand): Predicate {
    return this.cmp("neq", other);
  }
  lt(other: Operand): Predicate {
    return this.cmp("lt", other);
  }
  lte(other: Operand): Predicate {
    return this.cmp("lte", other);
  }
  gt(other: Operand): Predicate {
    return this.cmp("gt", other);
  }
  gte(other: Operand): Predicate {
    return this.cmp("gte", other);
  }

  in_(values: Scalar[]): Predicate {
    if (values.length === 0) {
      throw new Error("Operator 'in' requires a non-empty list");
    }
    return this.predicate("in", { value: values });
  }

  notIn(values: Scalar[]): Predicate {
    if (values.length === 0) {
      throw new Error("Operator 'not_in' requires a non-empty list");
    }
    return this.predicate("not_in", { value: values });
  }

  like(pattern: string): Predicate {
    return this.predicate("like", { value: pattern });
  }

  between(low: Scalar, high: Scalar): Predicate {
    return this.predicate("between", { value: [low, high] });
  }

  isNull(): Predicate {
    return this.predicate("is_null");
  }

  isNotNull(): Predicate {
    return this.predicate("is_not_null");
  }
}

type ReadableOp = CompareOp;

/** A `Table.Column` (or `Alias.Column`) reference. Build with `col`. Arithmetic
 * methods lift it into an `Expr` (item 100). */
export class Column extends Comparable {
  constructor(public readonly name: string) {
    super();
  }

  protected target(): PredicateTarget {
    return { ...EMPTY_TARGET, col: this.name };
  }

  add(other: Operand): Expr {
    return binary("+", this, other);
  }
  sub(other: Operand): Expr {
    return binary("-", this, other);
  }
  mul(other: Operand): Expr {
    return binary("*", this, other);
  }
  div(other: Operand): Expr {
    return binary("/", this, other);
  }
}

/** A `Table.Column` reference for use in predicates, select items, or as a
 * scalar-function argument. */
export function col(name: string): Column {
  return new Column(name);
}

/** A whitelisted scalar function applied to a column, usable as a *predicate*
 * target (`fn("lower", col("customers.name")).eq("ada")`). Build with `fn`. */
export class FnColumn extends Comparable {
  constructor(public readonly call: ScalarFunctionCall) {
    super();
  }

  protected target(): PredicateTarget {
    return { ...EMPTY_TARGET, col_fn: this.call };
  }
}

/** A built scalar `Expression`. Arithmetic methods compose it further and the
 * comparison methods target the computed value, exactly like `Column`. */
export class Expr extends Comparable {
  constructor(public readonly node: Expression) {
    super();
  }

  protected target(): PredicateTarget {
    return { ...EMPTY_TARGET, expr: this.node };
  }

  add(other: Operand): Expr {
    return binary("+", this, other);
  }
  sub(other: Operand): Expr {
    return binary("-", this, other);
  }
  mul(other: Operand): Expr {
    return binary("*", this, other);
  }
  div(other: Operand): Expr {
    return binary("/", this, other);
  }
}

/** Anything usable as an arithmetic operand or comparison right-hand side. */
export type Operand = Column | Expr | LiteralValue | Scalar;

/** A column name or an already-built `Column`. */
export type ColumnLike = string | Column;

function unwrap(value: LiteralValue | Scalar | Column): Scalar {
  if (value instanceof LiteralValue) {
    return value.value;
  }
  if (value instanceof Column) {
    throw new TypeError(
      "compare against a column with the comparison method directly " +
        "(e.g. col('a').eq(col('b'))); do not wrap it",
    );
  }
  return value;
}

function colName(value: ColumnLike): string {
  if (value instanceof Column) {
    return value.name;
  }
  return value;
}

// --------------------------------------------------------------------------
// Scalar functions (predicate target / select-item spellings)
// --------------------------------------------------------------------------

/** A value already explicitly wrapped as a column or literal, for scalar
 * function arguments and CASE results (ambiguity rule: a bare value there
 * could plausibly be either). TypeScript enforces this at compile time —
 * a bare string/number argument is a type error, not a runtime one. */
export type Wrapped = Column | LiteralValue;

function toArg(value: Wrapped): ScalarFunctionArg {
  if (value instanceof Column) {
    return { col: value.name };
  }
  return { literal: value.value };
}

const _SCALAR_FN_ONE_ARG = new Set<ScalarFn>(["lower", "upper", "trim"]);

function scalarCall(name: ScalarFn, args: Wrapped[]): ScalarFunctionCall {
  if (_SCALAR_FN_ONE_ARG.has(name)) {
    if (args.length !== 1 || !(args[0] instanceof Column)) {
      throw new Error(`${name} takes exactly one column argument`);
    }
  } else if (args.length < 2) {
    throw new Error(`${name} requires at least 2 arguments`);
  }
  return { fn: name, args: args.map(toArg) };
}

/** A scalar function applied to column(s)/literal(s), usable as a *predicate*
 * target. `lower`/`upper`/`trim` take one `col(...)`; `coalesce`/`concat` take
 * 2+ `col(...)`/`lit(...)` args. */
export function fn(name: ScalarFn, ...args: Wrapped[]): FnColumn {
  return new FnColumn(scalarCall(name, args));
}

/** Alias of `fn` — a scalar function used as a predicate target. */
export function colFn(name: ScalarFn, ...args: Wrapped[]): FnColumn {
  return fn(name, ...args);
}

/** A scalar function as a SELECT projection, e.g.
 * `fnSelect("coalesce", [col("customers.name"), lit("unknown")], {as: "display_name"})`. */
export function fnSelect(
  name: ScalarFn,
  args: Wrapped[],
  options: { as?: string } = {},
): ScalarFunctionSelectItem {
  const call = scalarCall(name, args);
  return { fn: call.fn, args: call.args, as: options.as ?? null };
}

// --------------------------------------------------------------------------
// Bounded scalar expressions (item 100)
// --------------------------------------------------------------------------

function isExpressionNode(value: unknown): value is Expression {
  return (
    typeof value === "object" &&
    value !== null &&
    ("col" in value ||
      "literal" in value ||
      "op" in value ||
      "fn" in value ||
      "cast" in value ||
      "when" in value ||
      "extract" in value ||
      "now" in value ||
      "date_add" in value)
  );
}

/** Normalize anything usable as a scalar expression operand. A bare scalar is
 * accepted here (unlike `toArg`) because an arithmetic operand is
 * unambiguous — a column must be written `col(...)`, so `col("a.qty").mul(2)`
 * can only mean a literal. */
function toExpression(value: Operand | Expression): Expression {
  if (value instanceof Expr) {
    return value.node;
  }
  if (value instanceof Column) {
    return { col: value.name };
  }
  if (value instanceof LiteralValue) {
    return { literal: value.value };
  }
  if (
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean" ||
    value === null
  ) {
    return { literal: value };
  }
  if (value instanceof FnColumn) {
    throw new TypeError(
      "fn()/colFn() build a scalar function as a predicate target, not an " +
        "expression operand — use exprFn(...) instead",
    );
  }
  if (isExpressionNode(value)) {
    return value;
  }
  throw new TypeError(
    "cannot use this value as a scalar expression — use col(...), lit(...), " +
      "a bare scalar, or another expression",
  );
}

function binary(op: BinaryOp, left: Operand, right: Operand): Expr {
  return new Expr({ op, left: toExpression(left), right: toExpression(right) });
}

/** Lift a `col(...)`/`lit(...)`/bare scalar into an `Expr` so the arithmetic
 * methods are available. A `Column` already supports them directly. */
export function expr(value: Operand): Expr {
  return new Expr(toExpression(value));
}

const _EXPR_FN_ARITY: Record<ExprFn, [number, number | null]> = {
  coalesce: [2, null],
  concat: [2, null],
  lower: [1, 1],
  upper: [1, 1],
  trim: [1, 1],
  abs: [1, 1],
  ceil: [1, 1],
  floor: [1, 1],
  length: [1, 1],
  round: [1, 2],
  nullif: [2, 2],
  replace: [3, 3],
  substring: [3, 3],
};

/** A whitelisted scalar function over expressions, WITH nesting —
 * `exprFn("lower", exprFn("trim", col("c.name")))`. */
export function exprFn(name: ExprFn, ...args: (Wrapped | Expr)[]): Expr {
  const [low, high] = _EXPR_FN_ARITY[name];
  const count = args.length;
  if (count < low || (high !== null && count > high)) {
    const expected = low === high ? `exactly ${low}` : high === null ? `at least ${low}` : `${low}-${high}`;
    throw new Error(`${name} takes ${expected} argument(s), got ${count}`);
  }
  return new Expr({ fn: name, args: args.map((a) => toExpression(a)) });
}

/** `CAST(value AS type)` over the closed target-type set. */
export function cast(value: Wrapped | Expr, to: CastType): Expr {
  return new Expr({ cast: toExpression(value), to });
}

/** `EXTRACT(part FROM value)` as an integer, evaluated in UTC on every
 * dialect. `dayofweek` is 0=Sunday..6=Saturday, `week` is the ISO-8601 week. */
export function extract(part: DatePart, value: Wrapped | Expr): Expr {
  return new Expr({ extract: toExpression(value), part });
}

/** The current UTC time — `"timestamp"` for the clock reading, `"date"` for
 * midnight UTC today. */
export function now(kind: "timestamp" | "date" = "timestamp"): Expr {
  return new Expr({ now: kind });
}

/** Shift a date/timestamp by `amount` whole `unit`s; negative goes back, so
 * `dateAdd(now(), "day", -7)` is "7 days ago". Bounded by
 * `Policy.max_interval_days`. */
export function dateAdd(value: Wrapped | Expr, unit: IntervalUnit, amount: number): Expr {
  return new Expr({ date_add: toExpression(value), unit, amount });
}

/** An expression-valued `CASE` — usable INSIDE an aggregate or arithmetic,
 * which is what makes conditional aggregation expressible. */
export function caseExpr(whens: CaseWhen[], options: { else?: Wrapped | Expr } = {}): Expr {
  if (whens.length === 0) {
    throw new Error("caseExpr requires at least one when(...) branch");
  }
  return new Expr({
    when: whens,
    else: options.else !== undefined ? toExpression(options.else) : null,
  });
}

/** Project a computed expression, e.g.
 * `exprSelect(col("oi.qty").mul(col("oi.price")), {as: "line_total"})`. */
export function exprSelect(value: Operand | Expression, options: { as: string }): ExpressionSelectItem {
  return { expr: toExpression(value), as: options.as };
}

// --------------------------------------------------------------------------
// Aggregate / date-bucket / ordered-set select items
// --------------------------------------------------------------------------

// A non-enumerable marker identifying the four aggregate-family select-item
// shapes (AggregateSelectItem/StringAggSelectItem/ArrayAggSelectItem/
// PercentileContSelectItem — `_AGGREGATE_SELECT_ITEM_TYPES` in
// query_ast/models.py) for the window/aggregate exclusivity check below.
// Deliberately NOT duck-typed off wire keys (e.g. "distinct"/"delimiter"/
// "fraction" in item): only `AggregateSelectItem` carries `distinct`, so a
// key-sniffing check silently missed `stringAgg`/`arrayAgg`/`percentileCont`
// entirely. A symbol key is invisible to `JSON.stringify`/`Object.keys`, so
// it never reaches the wire — this is metadata for THIS builder only.
const AGGREGATE_FAMILY = Symbol("aggregateFamilySelectItem");

function tagAggregate<T extends object>(item: T): T {
  Object.defineProperty(item, AGGREGATE_FAMILY, { value: true, enumerable: false });
  return item;
}

function isAggregateFamily(item: unknown): boolean {
  return (
    typeof item === "object" &&
    item !== null &&
    (item as Record<symbol, unknown>)[AGGREGATE_FAMILY] === true
  );
}

// stddev/variance never expose a `distinct` option below (matching the
// Python builder's own agg.stddev/agg.variance, which hardcode distinct=False
// the same way) — this check exists only for a caller constructing an
// AggregateSelectItem object literal directly, not reachable through the
// public builder API, mirroring `AggregateSelectItem._validate_shape`'s
// equivalent Pydantic rule for that direct-construction case.
const _NO_DISTINCT_AGG_FNS = new Set<AggregateFn>(["stddev", "variance"]);

function makeAggregate(
  fn_: AggregateFn,
  column: ColumnLike | Expr,
  distinct: boolean,
  as_?: string,
): AggregateSelectItem {
  if (column instanceof Expr) {
    if (distinct && _NO_DISTINCT_AGG_FNS.has(fn_)) {
      throw new Error(
        `distinct is not valid with ${fn_} — MSSQL's STDEV/VAR don't accept DISTINCT`,
      );
    }
    if (as_ === undefined) {
      throw new Error("an aggregate over a computed expression requires an explicit 'as' alias");
    }
    return tagAggregate({ fn: fn_, col: null, arg: column.node, distinct, as: as_ });
  }
  // Resolve to the actual wire `col` value BEFORE checking distinct+star, so
  // `agg.count(col("*"), {distinct: true})` is rejected exactly like
  // `agg.count("*", {distinct: true})` — checking the caller's syntactic form
  // instead of the resolved value let a `col("*")`-wrapped argument bypass
  // this check entirely while still resolving to the same illegal `col: "*"`
  // shape below.
  const resolvedCol = colName(column);
  const isStar = resolvedCol === "*";
  if (distinct && isStar) {
    throw new Error("distinct is not valid with count(*) — give a real column");
  }
  if (distinct && _NO_DISTINCT_AGG_FNS.has(fn_)) {
    throw new Error(
      `distinct is not valid with ${fn_} — MSSQL's STDEV/VAR don't accept DISTINCT`,
    );
  }
  return tagAggregate({ fn: fn_, col: resolvedCol, arg: null, distinct, as: as_ ?? null });
}

/** Namespace of aggregate select-item factories, e.g. `agg.sum(...)`. */
export const agg = {
  count(column: ColumnLike | Expr = "*", options: { distinct?: boolean; as?: string } = {}): AggregateSelectItem {
    return makeAggregate("count", column, options.distinct ?? false, options.as);
  },
  sum(column: ColumnLike | Expr, options: { distinct?: boolean; as?: string } = {}): AggregateSelectItem {
    return makeAggregate("sum", column, options.distinct ?? false, options.as);
  },
  avg(column: ColumnLike | Expr, options: { distinct?: boolean; as?: string } = {}): AggregateSelectItem {
    return makeAggregate("avg", column, options.distinct ?? false, options.as);
  },
  min(column: ColumnLike | Expr, options: { distinct?: boolean; as?: string } = {}): AggregateSelectItem {
    return makeAggregate("min", column, options.distinct ?? false, options.as);
  },
  max(column: ColumnLike | Expr, options: { distinct?: boolean; as?: string } = {}): AggregateSelectItem {
    return makeAggregate("max", column, options.distinct ?? false, options.as);
  },
  stddev(column: ColumnLike | Expr, options: { as?: string } = {}): AggregateSelectItem {
    return makeAggregate("stddev", column, false, options.as);
  },
  variance(column: ColumnLike | Expr, options: { as?: string } = {}): AggregateSelectItem {
    return makeAggregate("variance", column, false, options.as);
  },
};

/** A date-truncation projection, e.g. `dateBucket("orders.created_at", "month")`. */
export function dateBucket(
  column: ColumnLike,
  granularity: DateGranularity,
  options: { as?: string } = {},
): DateBucketSelectItem {
  return { col: colName(column), granularity, as: options.as ?? null };
}

/** Concatenate a column's grouped values into one delimited string. */
export function stringAgg(
  column: ColumnLike,
  delimiter: string,
  options: { as?: string } = {},
): StringAggSelectItem {
  const name = colName(column);
  if (name === "*") {
    throw new Error("string_agg requires a real column, not '*'");
  }
  return tagAggregate({ col: name, delimiter, as: options.as ?? null });
}

/** Collect a column's grouped values into an array (Postgres only server-side). */
export function arrayAgg(column: ColumnLike, options: { as?: string } = {}): ArrayAggSelectItem {
  const name = colName(column);
  if (name === "*") {
    throw new Error("array_agg requires a real column, not '*'");
  }
  return tagAggregate({ col: name, as: options.as ?? null });
}

/** A continuous-interpolation percentile, e.g.
 * `percentileCont("orders.total_amount", 0.5, {as: "median"})`. */
export function percentileCont(
  column: ColumnLike,
  fraction: number,
  options: { as?: string } = {},
): PercentileContSelectItem {
  const name = colName(column);
  if (name === "*") {
    throw new Error("percentile_cont requires a real column, not '*'");
  }
  if (fraction < 0.0 || fraction > 1.0) {
    throw new Error("percentile_cont fraction must be between 0.0 and 1.0");
  }
  return tagAggregate({ col: name, fraction, as: options.as ?? null });
}

// --------------------------------------------------------------------------
// Window functions (item 101 projection, item 125 operand)
// --------------------------------------------------------------------------

const _WINDOW_AGGREGATE_FNS = new Set<WindowFn>(["sum", "avg", "min", "max", "count"]);
const _WINDOW_NO_ARG_FNS = new Set<WindowFn>(["row_number", "rank", "dense_rank", "ntile"]);
const _WINDOW_ORDERED_FNS = new Set<WindowFn>([
  ..._WINDOW_NO_ARG_FNS,
  "lag",
  "lead",
  "first_value",
  "last_value",
]);
const _WINDOW_FRAMEABLE_FNS = new Set<WindowFn>([..._WINDOW_AGGREGATE_FNS, "first_value", "last_value"]);
const _WINDOW_OFFSET_FNS = new Set<WindowFn>(["lag", "lead"]);

function toWindowBound(
  value: null | number | WindowBoundKind | WindowBound,
  isStart: boolean,
): WindowBound {
  if (value === null) {
    return { bound: isStart ? "unbounded_preceding" : "unbounded_following", offset: null };
  }
  if (typeof value === "object") {
    return value;
  }
  if (typeof value === "string") {
    return { bound: value, offset: null };
  }
  if (value === 0) {
    return { bound: "current_row", offset: null };
  }
  return { bound: value < 0 ? "preceding" : "following", offset: Math.abs(value) };
}

function validateWindowBound(bound: WindowBound): void {
  const needsOffset = bound.bound === "preceding" || bound.bound === "following";
  if (needsOffset && bound.offset === null) {
    throw new Error(`window frame bound '${bound.bound}' requires an offset`);
  }
  if (!needsOffset && bound.offset !== null) {
    throw new Error(`window frame bound '${bound.bound}' takes no offset`);
  }
}

function boundPosition(bound: WindowBound): number {
  if (bound.bound === "unbounded_preceding") return -Infinity;
  if (bound.bound === "unbounded_following") return Infinity;
  if (bound.bound === "current_row") return 0;
  const offset = bound.offset ?? 0;
  return bound.bound === "preceding" ? -offset : offset;
}

/** A window frame, e.g. the last 7 rows: `frame("rows", -6, 0)`. `null` means
 * unbounded (in that end's direction). */
export function frame(
  mode: WindowFrameMode,
  start: null | number | WindowBoundKind | WindowBound,
  end: null | number | WindowBoundKind | WindowBound,
): WindowFrame {
  const startBound = toWindowBound(start, true);
  const endBound = toWindowBound(end, false);
  validateWindowBound(startBound);
  validateWindowBound(endBound);
  if (startBound.bound === "unbounded_following") {
    throw new Error("a window frame cannot start at 'unbounded_following'");
  }
  if (endBound.bound === "unbounded_preceding") {
    throw new Error("a window frame cannot end at 'unbounded_preceding'");
  }
  if (boundPosition(startBound) > boundPosition(endBound)) {
    throw new Error("a window frame's start must not come after its end");
  }
  return { mode, start: startBound, end: endBound };
}

interface WindowOptions {
  partitionBy?: ColumnLike[];
  orderBy?: (ColumnLike | OrderBySpec)[];
  frame?: WindowFrame;
  offset?: number;
  buckets?: number;
}

function windowSpec(options: WindowOptions): WindowSpec {
  return {
    partition_by: (options.partitionBy ?? []).map(colName),
    order_by: (options.orderBy ?? []).map(toOrderBy),
    frame: options.frame ?? null,
  };
}

function validateWindowCall(call: WindowCall): void {
  if (_WINDOW_NO_ARG_FNS.has(call.fn)) {
    if (call.arg !== null) {
      throw new Error(`window function ${call.fn} takes no 'arg'`);
    }
  } else if (call.arg === null && call.fn !== "count") {
    throw new Error(`window function ${call.fn} requires an 'arg'`);
  }
  if (call.offset !== null && !_WINDOW_OFFSET_FNS.has(call.fn)) {
    throw new Error(`'offset' is only valid for lag/lead, not ${call.fn}`);
  }
  if (call.fn === "ntile" && call.buckets === null) {
    throw new Error("window function ntile requires 'buckets'");
  }
  if (call.buckets !== null && call.fn !== "ntile") {
    throw new Error(`'buckets' is only valid for ntile, not ${call.fn}`);
  }
  if (call.over.frame !== null && !_WINDOW_FRAMEABLE_FNS.has(call.fn)) {
    throw new Error(
      `a ROWS/RANGE frame is not valid for window function ${call.fn} — frames apply to ` +
        "sum/avg/min/max/count/first_value/last_value",
    );
  }
  if (call.over.order_by.length === 0 && (_WINDOW_ORDERED_FNS.has(call.fn) || call.over.frame !== null)) {
    const reason = _WINDOW_ORDERED_FNS.has(call.fn) ? call.fn : "a frame";
    throw new Error(`${reason} requires over.orderBy`);
  }
}

function windowCall(
  fnName: WindowFn,
  arg: (Wrapped | Expr) | undefined,
  options: WindowOptions,
): WindowCall {
  const call: WindowCall = {
    fn: fnName,
    over: windowSpec(options),
    arg: arg !== undefined ? toExpression(arg) : null,
    offset: options.offset ?? null,
    buckets: options.buckets ?? null,
  };
  validateWindowCall(call);
  return call;
}

/** A window-function projection (item 101), e.g. a running total:
 * `window("sum", col("orders.amount"), {as: "running_total", orderBy: [asc("orders.created_at")], frame: frame("rows", null, 0)})`.
 * `arg` is omitted for the ranking functions and for `COUNT(*) OVER`. */
export function window(
  fnName: WindowFn,
  arg: (Wrapped | Expr) | undefined,
  options: WindowOptions & { as: string },
): WindowSelectItem {
  return { ...windowCall(fnName, arg, options), as: options.as };
}

/** A window function as an OPERAND rather than a projection (item 125), so it
 * composes with arithmetic. Legal only in a projection — never in
 * where/having, a join condition, a group key, or another window's `arg`. */
export function windowExpr(
  fnName: WindowFn,
  arg?: Wrapped | Expr,
  options: WindowOptions = {},
): Expr {
  return new Expr(windowCall(fnName, arg, options));
}

// --------------------------------------------------------------------------
// CASE
// --------------------------------------------------------------------------

/** One CASE branch: a condition and its result. The condition is a full
 * `WhereNode` — a single `Predicate` or an `and_(...)`/`or_(...)`/`not_(...)`
 * group for a searched CASE (item 99). */
export function when(condition: WhereNode, then: Wrapped | Expr): CaseWhen {
  return { when: condition, then: toExpression(then) };
}

/** `CASE WHEN ... THEN ... [ELSE ...] END` — an `as` alias is required.
 * Named `caseSelect` (not `case`, a reserved JS keyword) to keep this a valid
 * top-level export. */
export function caseSelect(
  whens: CaseWhen[],
  options: { else?: Wrapped | Expr; as: string },
): CaseSelectItem {
  if (whens.length === 0) {
    throw new Error("caseSelect requires at least one when(...) branch");
  }
  return {
    when: whens,
    else: options.else !== undefined ? toExpression(options.else) : null,
    as: options.as,
  };
}

// --------------------------------------------------------------------------
// Boolean groups
// --------------------------------------------------------------------------

/** Combine predicates/groups with AND. */
export function and_(...terms: WhereNode[]): WhereGroup {
  return { and: terms, or: null, not: null };
}

/** Combine predicates/groups with OR. */
export function or_(...terms: WhereNode[]): WhereGroup {
  return { and: null, or: terms, not: null };
}

/** Negate a single predicate or group: NOT term. */
export function not_(term: WhereNode): WhereGroup {
  return { and: null, or: null, not: term };
}

// --------------------------------------------------------------------------
// Ordering
// --------------------------------------------------------------------------

export function asc(column: ColumnLike, options: { nulls?: "first" | "last" } = {}): OrderBySpec {
  return { col: colName(column), dir: "asc", nulls: options.nulls ?? null };
}

export function desc(column: ColumnLike, options: { nulls?: "first" | "last" } = {}): OrderBySpec {
  return { col: colName(column), dir: "desc", nulls: options.nulls ?? null };
}

function toOrderBy(value: ColumnLike | OrderBySpec): OrderBySpec {
  if (typeof value === "object" && "dir" in value) {
    return value;
  }
  return { col: colName(value as ColumnLike), dir: "asc", nulls: null };
}

// --------------------------------------------------------------------------
// Query builder
// --------------------------------------------------------------------------

interface JoinOptions {
  type?: JoinType;
  alias?: string;
  extraOn?: [ColumnLike, ColumnLike][];
  condition?: WhereNode[];
  connection?: string;
}

interface TopNOptions {
  orderBy: (ColumnLike | OrderBySpec)[];
  partitionBy?: ColumnLike[];
  fn?: RankFn;
}

/** A fluent builder for a single `StructuredQuery`. Every mutating method
 * returns `this` for chaining. Terminal methods: `build()` (the full,
 * always-valid-by-construction shape), `toDict()`, and `toJSON()`. */
export class Query {
  private fromTable: string;
  private fromAlias: string | undefined;
  private selectItems: SelectItem[] = [];
  private distinctFlag = false;
  private joinSpecs: JoinSpec[] = [];
  private whereNodes: WhereNode[] = [];
  private groupByColumns: string[] = [];
  private havingNodes: WhereNode[] = [];
  private orderBySpecs: OrderBySpec[] = [];
  private limitValue: number | undefined;
  private offsetValue = 0;
  private topNSpec: TopNSpec | undefined;
  private setOpSpec: SetOpSpec | undefined;
  private intentText: string | undefined;

  private constructor(table: string, alias?: string) {
    this.fromTable = table;
    this.fromAlias = alias;
  }

  /** Start a query rooted at `table` (optionally aliased). */
  static from(table: string, options: { alias?: string } = {}): Query {
    return new Query(table, options.alias);
  }

  /** Add one or more projections: bare column names, `col(...)`, or a
   * select-item helper (`agg.*`, `dateBucket`, `caseSelect` …). */
  select(...items: (string | Column | Exclude<SelectItem, string>)[]): Query {
    for (const item of items) {
      if (item instanceof FnColumn) {
        throw new TypeError(
          "fn()/colFn() build a scalar function as a predicate target — for a " +
            "scalar function in select(), use fnSelect(...) instead",
        );
      }
      this.selectItems.push(item instanceof Column ? item.name : item);
    }
    return this;
  }

  /** SELECT DISTINCT across the whole select list. */
  distinct(value = true): Query {
    this.distinctFlag = value;
    return this;
  }

  /** Join `table` on an equality pair `(left, right)`. `extraOn` adds further
   * ANDed pairs for composite keys. Pass `options.condition` instead of `on`
   * for a range/inequality join; a `type: "cross"` join takes neither. */
  join(table: string, on?: [ColumnLike, ColumnLike], options: JoinOptions = {}): Query {
    const type = options.type ?? "inner";
    const extraOn = (options.extraOn ?? []).map(
      ([a, b]) => [colName(a), colName(b)] as [string, string],
    );
    const condition = options.condition ? andCombine(options.condition) : null;

    if (type === "cross") {
      if (on !== undefined || extraOn.length > 0 || condition !== null) {
        throw new Error(
          "a 'cross' join takes no condition — an unconditioned cartesian product is " +
            "the entire point of it; use 'inner' with the condition instead",
        );
      }
    } else if ((on === undefined) === (condition === null)) {
      throw new Error(
        "exactly one of `on` (equality sugar) or `options.condition` (general predicate " +
          "tree) is required on a join",
      );
    } else if (extraOn.length > 0 && on === undefined) {
      throw new Error(
        "`extraOn` is additional equality pairs for `on` and cannot be used with " +
          "`condition` — express the extra pairs inside `condition` instead",
      );
    }

    this.joinSpecs.push({
      table,
      alias: options.alias ?? null,
      type,
      on: on !== undefined ? [colName(on[0]), colName(on[1])] : null,
      extra_on: extraOn,
      condition,
      connection: options.connection ?? null,
    });
    return this;
  }

  /** Add predicate(s)/group(s). Multiple nodes — across one call or several
   * `.where()` calls — are combined with AND. */
  where(...nodes: WhereNode[]): Query {
    this.whereNodes.push(...nodes);
    return this;
  }

  groupBy(...columns: ColumnLike[]): Query {
    this.groupByColumns.push(...columns.map(colName));
    return this;
  }

  /** Add post-aggregation predicate(s)/group(s), AND-combined exactly like
   * `.where()`. Pass `or_(...)`/`and_(...)`/`not_(...)` for boolean logic
   * over aggregate conditions (item 99). */
  having(...nodes: WhereNode[]): Query {
    this.havingNodes.push(...nodes);
    return this;
  }

  orderBy(column: ColumnLike, options: { desc?: boolean; nulls?: "first" | "last" } = {}): Query {
    this.orderBySpecs.push({
      col: colName(column),
      dir: options.desc ? "desc" : "asc",
      nulls: options.nulls ?? null,
    });
    return this;
  }

  limit(n: number): Query {
    this.limitValue = n;
    return this;
  }

  offset(n: number): Query {
    this.offsetValue = n;
    return this;
  }

  /** Keep the top `n` rows per partition (an overall top-N if `partitionBy`
   * is empty). */
  topN(n: number, options: TopNOptions): Query {
    if (options.orderBy.length === 0) {
      throw new Error("topN requires at least one orderBy entry");
    }
    this.topNSpec = {
      partition_by: (options.partitionBy ?? []).map(colName),
      order_by: options.orderBy.map(toOrderBy),
      n,
      fn: options.fn ?? "row_number",
    };
    return this;
  }

  private setOperation(op: SetOpKind, allRows: boolean, arms: Query[]): Query {
    if (arms.length === 0) {
      throw new Error(`${op} requires at least one arm`);
    }
    this.setOpSpec = { op, all: allRows, arms: arms.map((arm) => arm.build()) };
    return this;
  }

  /** Combine this query's rows with further queries (UNION; pass
   * `{all: true}` to keep duplicates). This query is the first arm. */
  union(arms: Query[], options: { all?: boolean } = {}): Query {
    return this.setOperation("union", options.all ?? false, arms);
  }

  /** Rows present in this query AND in every arm. */
  intersect(arms: Query[]): Query {
    return this.setOperation("intersect", false, arms);
  }

  /** Rows present in this query but not in any arm. */
  except(arms: Query[]): Query {
    return this.setOperation("except", false, arms);
  }

  /** Attach a natural-language intent (logged with the compiled SQL for
   * audit/debugging; never returned to the caller). */
  intent(text: string): Query {
    this.intentText = text;
    return this;
  }

  private whereNode(): WhereNode | null {
    return andCombine(this.whereNodes);
  }

  private havingNode(): WhereNode | null {
    return andCombine(this.havingNodes);
  }

  private validateAliases(): void {
    const occurrences: [string, string | undefined][] = [
      [this.fromTable, this.fromAlias],
      ...this.joinSpecs.map((j): [string, string | undefined] => [j.table, j.alias ?? undefined]),
    ];
    const physicalCounts = new Map<string, number>();
    for (const [physical] of occurrences) {
      const key = physical.toLowerCase();
      physicalCounts.set(key, (physicalCounts.get(key) ?? 0) + 1);
    }
    const seen = new Set<string>();
    for (const [physical, alias] of occurrences) {
      const effective = (alias ?? physical).toLowerCase();
      if (seen.has(effective)) {
        throw new Error(
          `Duplicate table/alias '${effective}' — every from/join effective name ` +
            "(its alias if given, else its table name) must be unique",
        );
      }
      seen.add(effective);
      if ((physicalCounts.get(physical.toLowerCase()) ?? 0) > 1 && alias === undefined) {
        throw new Error(
          `Table '${physical}' is used more than once in this query (a self-join) — ` +
            "every occurrence must have an explicit alias, including this one",
        );
      }
    }
  }

  private validateWindowScope(): void {
    const hasWindow = this.selectItems.some((item) => typeof item === "object" && "over" in item);
    if (!hasWindow) return;
    const hasAggregate = this.selectItems.some(isAggregateFamily);
    if (this.groupByColumns.length > 0 || hasAggregate) {
      throw new Error(
        "a window function cannot be combined with group_by or aggregate select items — " +
          "a window projects a value per row, so aggregate in one query and window over " +
          "that result in a second query",
      );
    }
  }

  private validateSetOp(): void {
    const spec = this.setOpSpec;
    if (spec === undefined) return;
    if (this.topNSpec !== undefined) {
      throw new Error(
        "top_n cannot be combined with set_op — rank within each arm, or order the " +
          "combined result with order_by + limit",
      );
    }
    for (const arm of spec.arms) {
      if (arm.set_op !== null) {
        throw new Error(
          "a set_op arm may not carry its own set_op — list every arm in one arms " +
            "array instead of nesting them",
        );
      }
      const disallowed = [
        ["order_by", arm.order_by.length > 0] as const,
        ["limit", arm.limit !== null] as const,
        ["offset", arm.offset !== 0] as const,
        ["top_n", arm.top_n !== null] as const,
      ]
        .filter(([, set]) => set)
        .map(([name]) => name);
      if (disallowed.length > 0) {
        throw new Error(
          `a set_op arm may not set [${disallowed.join(", ")}] — order_by/limit/offset/` +
            "top_n apply to the combined result and belong on the query that carries set_op",
        );
      }
      if (arm.select.length !== this.selectItems.length) {
        throw new Error(
          "every set_op arm must project the same number of columns: this query " +
            `projects ${this.selectItems.length}, an arm projects ${arm.select.length}`,
        );
      }
    }
  }

  /** Construct the full `StructuredQuery` shape, running the same
   * cross-field checks the server's Pydantic models would (self-join
   * aliasing, window/aggregate exclusivity, set-op arm shape). */
  build(): StructuredQuery {
    this.validateAliases();
    this.validateWindowScope();
    this.validateSetOp();
    return {
      from: this.fromTable,
      from_alias: this.fromAlias ?? null,
      select: this.selectItems,
      distinct: this.distinctFlag,
      joins: this.joinSpecs,
      where: this.whereNode(),
      group_by: this.groupByColumns,
      having: this.havingNode(),
      order_by: this.orderBySpecs,
      limit: this.limitValue ?? null,
      offset: this.offsetValue,
      top_n: this.topNSpec ?? null,
      set_op: this.setOpSpec ?? null,
      correlate: [],
      ctes: [],
      intent: this.intentText ?? null,
    };
  }

  /** The complete wire JSON object to send as a REST/MCP query body. */
  toDict(): StructuredQuery {
    return this.build();
  }

  /** The wire JSON as a string. */
  toJSON(indent?: number): string {
    return JSON.stringify(this.build(), null, indent);
  }
}

function andCombine(nodes: WhereNode[]): WhereNode | null {
  if (nodes.length === 0) return null;
  if (nodes.length === 1) return nodes[0]!;
  return { and: nodes, or: null, not: null };
}
