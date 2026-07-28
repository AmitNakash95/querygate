/**
 * Wire-shape types for QueryGate's `StructuredQuery` AST (TODO.md item 51,
 * phase 2a). These interfaces mirror `src/querygate/query_ast/models.py`
 * field-for-field, using the exact wire JSON key names (the server's Pydantic
 * `serialization_alias`, e.g. `as`/`else`/`and`/`or`/`not`/`all`/`from`) so a
 * value built here is byte-for-byte what a hand-authored REST/MCP JSON body
 * would need.
 *
 * Every field the Python models declare is always present here (using `null`
 * for an unset optional, and the Python-declared default for a defaulted
 * field) rather than mimicking Pydantic's `exclude_none`/`exclude_defaults`
 * trimming. This sidesteps a real ambiguity a generic "strip null values"
 * pass would hit: `LiteralExpr.literal` can legitimately BE `null` (a SQL
 * NULL literal via `lit(null)`), so "key absent" and "value is null" must
 * stay distinguishable. The server's Pydantic models parse an explicit
 * default/null identically to an omitted field (`extra="forbid"` only
 * rejects UNDECLARED keys), so this stays fully wire-compatible — the
 * payload is marginally larger than the Python builder's trimmed
 * `to_dict()`, never incorrect.
 *
 * `CteSpec`/`StructuredQuery.correlate`/`StructuredQuery.ctes` (items 105/106)
 * have no builder support here, matching the same gap in the phase-1 Python
 * builder (see `src/querygate/client/builder.py`) — both stay a follow-up.
 */

export type CompareOp =
  | "eq"
  | "neq"
  | "lt"
  | "lte"
  | "gt"
  | "gte"
  | "in"
  | "not_in"
  | "like"
  | "between"
  | "is_null"
  | "is_not_null";

/** `Predicate.op`'s wire type — `CompareOp` plus the two subquery-existence
 * tests (item 106). No builder helper produces `exists`/`not_exists` yet. */
export type ReadCompareOp = CompareOp | "exists" | "not_exists";

export type AggregateFn = "count" | "sum" | "avg" | "min" | "max" | "stddev" | "variance";
export type JoinType = "inner" | "left" | "full" | "cross";
export type SortDir = "asc" | "desc";
export type RankFn = "row_number" | "rank" | "dense_rank";
export type DateGranularity = "day" | "week" | "month" | "quarter" | "year";
export type SetOpKind = "union" | "intersect" | "except";
export type ScalarFn = "coalesce" | "lower" | "upper" | "trim" | "concat";
export type BinaryOp = "+" | "-" | "*" | "/";
export type ExprFn =
  | "coalesce"
  | "lower"
  | "upper"
  | "trim"
  | "concat"
  | "abs"
  | "ceil"
  | "floor"
  | "round"
  | "length"
  | "nullif"
  | "replace"
  | "substring";
export type CastType = "text" | "integer" | "numeric" | "boolean" | "date" | "timestamp";
export type DatePart =
  | "year"
  | "quarter"
  | "month"
  | "week"
  | "day"
  | "dayofweek"
  | "dayofyear"
  | "hour"
  | "minute"
  | "second";
export type IntervalUnit = "year" | "month" | "week" | "day" | "hour" | "minute" | "second";
export type WindowFn =
  | "sum"
  | "avg"
  | "min"
  | "max"
  | "count"
  | "row_number"
  | "rank"
  | "dense_rank"
  | "ntile"
  | "lag"
  | "lead"
  | "first_value"
  | "last_value";
export type WindowBoundKind =
  | "unbounded_preceding"
  | "preceding"
  | "current_row"
  | "following"
  | "unbounded_following";
export type WindowFrameMode = "rows" | "range";

/** A literal scalar value (string/number/bool/null) — binds as a parameter,
 * never interpolated. */
export type Scalar = string | number | boolean | null;

// --------------------------------------------------------------------------
// The bounded scalar Expression substrate (item 100) and its leaves.
// --------------------------------------------------------------------------

export interface ColumnExpr {
  col: string;
}

export interface LiteralExpr {
  literal: Scalar;
}

export type ScalarFunctionArg = ColumnExpr | LiteralExpr;

export interface ScalarFunctionCall {
  fn: ScalarFn;
  args: ScalarFunctionArg[];
}

export interface ScalarFunctionSelectItem extends ScalarFunctionCall {
  as: string | null;
}

export interface BinaryOpExpr {
  op: BinaryOp;
  left: Expression;
  right: Expression;
}

export interface FunctionExpr {
  fn: ExprFn;
  args: Expression[];
}

export interface CastExpr {
  cast: Expression;
  to: CastType;
}

export interface ExtractExpr {
  extract: Expression;
  part: DatePart;
}

export interface NowExpr {
  now: "timestamp" | "date";
}

export interface DateAddExpr {
  date_add: Expression;
  unit: IntervalUnit;
  amount: number;
}

export interface CaseWhen {
  when: WhereNode;
  then: Expression;
}

export interface CaseExpr {
  when: CaseWhen[];
  else: Expression | null;
}

export interface WindowBound {
  bound: WindowBoundKind;
  offset: number | null;
}

export interface WindowFrame {
  mode: WindowFrameMode;
  start: WindowBound;
  end: WindowBound;
}

export interface OrderBySpec {
  col: string;
  dir: SortDir;
  nulls: "first" | "last" | null;
}

export interface WindowSpec {
  partition_by: string[];
  order_by: OrderBySpec[];
  frame: WindowFrame | null;
}

export interface WindowCall {
  fn: WindowFn;
  over: WindowSpec;
  arg: Expression | null;
  offset: number | null;
  buckets: number | null;
}

export interface WindowSelectItem extends WindowCall {
  as: string;
}

// eslint-disable-next-line @typescript-eslint/no-empty-object-type
export interface WindowExpr extends WindowCall {}

/** One closed, depth-capped recursive union used everywhere a scalar value is
 * expected (item 100). `WindowExpr` (item 125) is the one member legal ONLY
 * in a projection — never in where/having/a join condition/a group key/an
 * aggregate argument/another window's arg. */
export type Expression =
  | ColumnExpr
  | LiteralExpr
  | BinaryOpExpr
  | FunctionExpr
  | CastExpr
  | CaseExpr
  | ExtractExpr
  | NowExpr
  | DateAddExpr
  | WindowExpr;

// --------------------------------------------------------------------------
// Select items
// --------------------------------------------------------------------------

export interface AggregateSelectItem {
  fn: AggregateFn;
  col: string | null;
  arg: Expression | null;
  distinct: boolean;
  as: string | null;
}

export interface DateBucketSelectItem {
  col: string;
  granularity: DateGranularity;
  as: string | null;
}

export interface StringAggSelectItem {
  col: string;
  delimiter: string;
  as: string | null;
}

export interface ArrayAggSelectItem {
  col: string;
  as: string | null;
}

export interface PercentileContSelectItem {
  col: string;
  fraction: number;
  as: string | null;
}

export interface CaseSelectItem {
  when: CaseWhen[];
  else: Expression | null;
  as: string;
}

export interface ExpressionSelectItem {
  expr: Expression;
  as: string;
}

export type SelectItem =
  | string
  | AggregateSelectItem
  | DateBucketSelectItem
  | StringAggSelectItem
  | ArrayAggSelectItem
  | PercentileContSelectItem
  | ScalarFunctionSelectItem
  | CaseSelectItem
  | ExpressionSelectItem
  | WindowSelectItem;

// --------------------------------------------------------------------------
// Joins, predicates, boolean groups
// --------------------------------------------------------------------------

export interface JoinSpec {
  table: string;
  alias: string | null;
  type: JoinType;
  on: [string, string] | null;
  extra_on: [string, string][];
  condition: WhereNode | null;
  connection: string | null;
}

export interface Predicate {
  col: string | null;
  expr: Expression | null;
  col_fn: ScalarFunctionCall | null;
  op: ReadCompareOp;
  value: unknown;
  value_col: string | null;
  value_expr: Expression | null;
  value_subquery: StructuredQuery | null;
  exists_subquery: StructuredQuery | null;
}

export interface WhereGroup {
  and: WhereNode[] | null;
  or: WhereNode[] | null;
  not: WhereNode | null;
}

export type WhereNode = Predicate | WhereGroup;

export interface TopNSpec {
  partition_by: string[];
  order_by: OrderBySpec[];
  n: number;
  fn: RankFn;
}

export interface SetOpSpec {
  op: SetOpKind;
  all: boolean;
  arms: StructuredQuery[];
}

/** One named `WITH` block (item 105). Part of the real wire contract, but —
 * like the Python phase-1 builder — has no builder helper here yet; a
 * `StructuredQuery.ctes` this builder produces is always `[]`. */
export interface CteSpec {
  name: string;
  query: StructuredQuery;
}

export interface StructuredQuery {
  from: string;
  from_alias: string | null;
  select: SelectItem[];
  distinct: boolean;
  joins: JoinSpec[];
  where: WhereNode | null;
  group_by: string[];
  having: WhereNode | null;
  order_by: OrderBySpec[];
  limit: number | null;
  offset: number;
  top_n: TopNSpec | null;
  set_op: SetOpSpec | null;
  /** Outer-column refs a subquery may read (items 97/106). Always `[]` here —
   * no builder support for subqueries/correlation yet, matching the same gap
   * in the phase-1 Python builder. */
  correlate: string[];
  /** Named `WITH` blocks (item 105). Always `[]` here — see `CteSpec`. */
  ctes: CteSpec[];
  intent: string | null;
}
