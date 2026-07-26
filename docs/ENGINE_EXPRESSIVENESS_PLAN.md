# Expressive Query Engine — Path to 10/10 (Flagship Pillar Plan)

**Status (2026-07-26):** in progress — **Phase 0 (item 99), Phase 1 (item 100) and
Phase 2 (item 101) have shipped.** Phase 3a (item 102, `EXTRACT`/date_part +
relative-date helpers) is next; its Decision Log entry (§8 entry 4 — interval cap
+ timezone semantics) is the first step of that item. Phases 3b–5 (items 103–106)
are unstarted. **The `Expression` substrate items 102–106 all build on is real**
(`query_ast/models.py`'s `Expression` union + `_compile_expression`); reuse it
rather than adding a parallel scalar shape — item 101's `WindowSelectItem.arg`
is the worked example. **Owner:** engine. **Audience:** the
implementing agent (Claude) + reviewers.
**Authority:** this is the *deep spec* the read-engine expressiveness items point
to. Item **content** and `✅ DONE` status live in `TODO.md`; execution **order**
lives in `ROADMAP.md` (this pillar is **Phase 4** as of the 2026-07-25
re-sequence). This document is the reference those items cite — it does not
replace them.

> **Item numbering.** Items 99–106 are already allocated to this plan's phases and
> exist in `TODO.md`. The highest allocated item file-wide is **113**, so a genuinely
> new item takes **114** — never reuse a number in this range. (This line previously
> read "next free number is **99**", which was true only before item 99 was created;
> it is a trap now, since item numbers are permanent and file-global per CLAUDE.md.)

---

## 0. Why this is a flagship pillar, and what "10/10" means

QueryGate's entire product bet is non-goal #1: **no caller-controlled raw SQL,
ever.** The only thing an agent submits is a validated `StructuredQuery` AST. That
bet is the **Structural** pillar of the North Star — and it *only wins if the
structured surface is expressive enough that a fluent SQL author almost never hits
a wall the AST can't express.* If agents routinely hit walls, they route around
the product (dump tables, ask for raw access, give up) — and the safety guarantee
becomes irrelevant because nobody adopts the gate. **Expressiveness is what makes
the safety constraint acceptable.** It is existential, not a nice-to-have.

So the engine must be maximized on four fronts *simultaneously*, and "10/10" means
a specific, testable thing on each:

| Front | 10/10 definition | How we measure it |
| --- | --- | --- |
| **Expressiveness** | A fluent SQL author can express essentially any analytic single-statement query (and multi-step via composition) without reaching for raw SQL. | The canonical regression bar (§5) is 100% green; no "wall" tickets accumulate. |
| **Safety** | No column/policy/mask/credential bypass exists on any code path, including every new primitive. | The visitor-bypass and cap-bypass adversarial suites (§3, §6) are green; `security-invariant-check` clean. |
| **Strictness** | Every input is a typed, schema-checked, policy-capped identifier or literal. No raw string ever reaches SQL. No silent coercion. | `extra="forbid"` everywhere; fuzz suite (item 36) finds no accepted-but-unmodelled input. |
| **Structure** | New capability is added as a *bounded primitive* behind the existing composable interfaces, never as an inline branch or an open-ended grammar. | No new `if dialect ==` at a call site; every variance point is a `DialectAdapter` method; expression grammar is a *closed*, depth-capped union. |

**The tension to hold consciously:** non-goal #7 forbids "an open-ended expression
grammar." A 10/10 on expressiveness is reached by adding **bounded, closed,
capped** primitives — a fixed operator set, a fixed function set, a capped nesting
depth — never by opening the door to arbitrary expressions. Every phase below is
designed to stay on the safe side of that line, and the line itself is recorded in
the Decision Log (§8).

---

## 1. The invariants that bound every change (non-negotiable)

These hold for **every** item in this plan. An item that violates one is not done,
regardless of a green test run.

1. **No raw SQL.** Every AST field is a typed identifier or literal. New nodes
   compile to SQLAlchemy Core constructs (`func.*`, operators, `case()`), never a
   string interpolated into SQL. `literal_column`/`text` are forbidden for
   caller-derived content (the existing `date_bucket` MSSQL adapter uses
   `literal_column` only for *constant* granularity keywords it controls — that is
   the only sanctioned use, and new code must not extend it to caller input).
2. **The visitor is the enforcement chokepoint** (item 96 substrate). Table/column
   allow-deny and the masked-column rule are driven entirely by the canonical
   reference visitor: `iter_column_refs` → `select_item_column_refs` /
   `predicate_column_refs` in `validation/schema_validation.py`, consumed by
   `validation/policy_validation.py::_validate_scope`. **Any new node whose column
   references are not yielded by the visitor is a silent policy-and-mask bypass.**
   This is the single most important rule in this document (see §3).
3. **Caps are summed tree-wide** (item 97). Count-based caps
   (`_enforce_tree_wide_caps`) are enforced on the SUM across every scope in the
   query tree, never per-level. Every new cost-bearing construct must be counted,
   and counted across subqueries/CTEs/set-op arms too.
4. **Reject, don't emulate** (item 74 precedent). Where a dialect genuinely lacks a
   capability, the `DialectAdapter` method raises `QueryValidationError` pointing at
   the primitive the agent can use instead — it never synthesizes structure the AST
   didn't ask for. Mechanical per-dialect translation of the *same* operation is
   fine and belongs on the adapter.
5. **Redaction-safe audit.** Persisted audit events never gain SQL text, predicate
   values, literals, or row data because a query got more expressive. New literals
   (arithmetic constants, frame offsets, cast targets) must not leak into audit.
6. **One pipeline.** Every new capability flows through
   `execution/service.py`'s single path: `policy_validation` → `schema_validation`
   → `sqlalchemy_compiler` → concurrency → engine → audit, in that order. No second
   compile path, no transport-specific shortcut.

---

## 2. The core architectural move: a bounded scalar `Expression` substrate

The engine's expressiveness ceiling reduces to one property of today's AST:

> **There is no recursive scalar-expression type.** "What can be projected or
> compared" is a *flat* set of leaf shapes — a bare `Table.Column` string, a
> one-level `ScalarFunctionCall` (`**No nesting**`), an aggregate over a **bare
> column string** (`col: str`), and a `CASE` whose `then`/`else` is only a
> column-or-literal.

Because of that single boundary, **arithmetic, conditional aggregation, nested
functions, expression-valued CASE, and computed GROUP/ORDER keys are all the same
missing feature.** `SUM(quantity * unit_price)` is impossible because (a) there is
no `*` operator anywhere and (b) an aggregate argument can only be a column name.

**Therefore the centerpiece of this plan (Phase 1 / item 100) is introducing one
bounded, closed, depth-capped `Expression` union** used everywhere a scalar value
is expected. Fix the substrate once and roughly five separate "walls" fall
together. Everything in Phase 1 is that node; later phases (windows, joins, set
ops) reuse it.

The independent second gap is **windowing** — `top_n` is the only `OVER()` surface
and is hard-wired to rank-and-filter-top-N. That is Phase 2.

Everything else is smaller and sequenced behind these two pillars.

---

## 3. Cross-cutting safety framework — the per-primitive Definition of Done

**Every** expressiveness item must satisfy this checklist before it is "done." Copy
it into the item's PR description and check each box with a linked test.

- [ ] **Typed & closed.** New node is a Pydantic model with
      `model_config = ConfigDict(extra="forbid")`. Any union it joins is closed
      (no `Any`-typed escape hatch). Depth/breadth are bounded by a policy cap.
- [ ] **Wired into the canonical visitor.** `select_item_column_refs` /
      `predicate_column_refs` / `iter_column_refs` (and `iter_query_scopes` if the
      node introduces a new scope) yield **every** `Table.Column` the node can
      reference, at the correct `RefPosition`. Add a `RefPosition` member if the
      node creates a genuinely new position.
- [ ] **Allow-deny proven.** Test: a **denied** column buried in the deepest
      position the node allows is rejected by `policy_validation`.
- [ ] **Mask rule proven.** Test: a **masked** column in any non-`SELECT_PROJECTION_BARE`
      position inside the node is rejected (masking may only surface as a bare
      top-level projection).
- [ ] **Schema-resolved.** Every ref resolves against live reflected tables via
      `resolve_column` / `parse_column_ref`; a non-existent column is rejected pre-DB.
- [ ] **Capped, summed tree-wide.** New cost-bearing counts are added to
      `_enforce_tree_wide_caps` (or a per-scope check in `_validate_scope` for
      non-summable structural caps), with a new `Policy.max_*` field where needed.
      Test the cap fires, and fires on the *sum* across a subquery/CTE/arm.
- [ ] **Dialect-honest.** Variance points are `DialectAdapter` methods, one class
      per dialect + registry — never inline `if dialect ==`. A dialect that
      genuinely lacks the capability **rejects** with a message pointing at the
      primitive to use instead; a test asserts the rejection per dialect.
- [ ] **Redaction-safe.** A test asserts no new literal/value/SQL leaks into a
      persisted audit event.
- [ ] **Adversarial cases named & tested.** At minimum: (a) can this field smuggle
      structure the AST didn't ask for? (b) can it create an unbounded/expensive
      path that dodges a cap? (c) can it leak schema or values into audit or the
      error message? Each gets an explicit stopping check + regression test in the
      security suite (`adversarial-probe` / item 36 fuzz corpus).
- [ ] **Composition documented.** If part of the surface is deliberately *not*
      built because the agent can compose it from existing primitives, the recipe
      is written in `docs/PRODUCT_GUIDE.md`, not left implicit.
- [ ] **Guide + Decision Log synced** (`product-guide-sync`), and `black` + the
      relevant test tiers green.

> **The one-line rule to remember:** *new expressiveness is safe iff it is visited,
> capped, dialect-honest, and closed.* Visited stops policy/mask bypass; capped
> stops cost bypass; dialect-honest stops "renders fine, breaks live"; closed keeps
> us on the right side of non-goal #7.

---

## 4. Phased roadmap

Dependency order. Each phase is shippable and testable on its own; later phases
assume earlier ones. Numbers are proposed `TODO.md` items (next free = 99).

```
Phase 0  ── item 99   HAVING-as-WhereNode + searched CASE condition        (cheap, de-risks the pattern)
Phase 1  ── item 100  Bounded scalar Expression substrate                  ★ centerpiece
Phase 2  ── item 101  General window functions (WindowSelectItem)          ★ second pillar
Phase 3a ── item 102  EXTRACT/date_part + relative-date/interval helpers
Phase 3b ── item 103  Non-equi/range join conditions + FULL OUTER / CROSS
Phase 4a ── item 104  Set operations (UNION / INTERSECT / EXCEPT)
Phase 4b ── item 105  CTE / derived table in FROM (non-recursive)
Phase 5  ── item 106  Correlated / EXISTS / scalar subqueries              (highest risk; last)
```

**Sequencing rationale.** Item 99 is a low-risk warm-up that proves the
visitor/cap expansion pattern on machinery that already exists (`WhereNode`).
Item 100 is the highest-value, largest single design and unlocks the most walls, so
it comes first among the big builds — and arithmetic + conditional aggregation are
**one** item, not two, because they share the substrate (splitting builds the hard
part twice). Windowing (101) is independent and self-contained. Dates/joins (102,
103) ride on the substrate. Set-ops/CTE (104, 105) add new scopes but stay
uncorrelated. Correlated/EXISTS (106) is last because it breaks the "uncorrelated"
assumption the entire current subquery layer rests on. Recursive CTE is explicitly
**out of scope** for this plan unless a hard iteration/cost cap is designed and
recorded separately (unbounded recursion is a genuine DoS).

---

### Phase 0 — Item 99: `HAVING` as `WhereNode` + searched `CASE` condition

**Goal.** OR-logic over aggregate conditions (`HAVING SUM(x) > 10 OR COUNT(*) < 3`)
and multi-condition CASE branches (`CASE WHEN a > 0 AND b < 5 THEN …`). Both reuse
already-safe machinery, so this is the proof-of-pattern warm-up.

**AST.** `StructuredQuery.having: List[Predicate]` → `Optional[WhereNode]`.
`CaseWhen.when: Predicate` → `WhereNode`.

**Compiler.** `having` already routes single predicates through `_apply_predicate`;
switch to `_compile_where(query.having, …)` reusing the exact WHERE machinery. CASE
branch conditions already build via `_resolve_predicate_target` + `_apply_predicate`
on a single predicate — call `_compile_where` on the branch's `WhereNode` instead.

**Validation.** `where_depth` and `_iter_where_predicates` already handle
`WhereNode`; point them at `having` and CASE conditions. Bounded by existing
`max_where_depth` / `max_where_predicates` / `max_case_branches` — no new cap.

**Dialect.** None (boolean logic is universal).

**Adversarial / tests.** Denied/masked column inside a `HAVING` OR-group and inside
a CASE `AND`-condition must reject; `max_where_depth` fires on a deep HAVING tree;
a `value_subquery` inside HAVING/CASE stays rejected (the existing
`_validate_subquery_constraints` rule — keep it, extend it to the new positions).

---

### Phase 1 — Item 100: bounded scalar `Expression` substrate ★

**Goal.** Arithmetic (`+ - * /`), nested functions (`lower(trim(x))`,
`coalesce(a, upper(b))`), scalar functions (`cast`, `round`, `floor`, `ceil`,
`abs`, `substring`, `nullif`, `replace`), expression-valued CASE `then`/`else`, and
— the big one — **aggregates over an expression**, which delivers conditional
aggregation (`SUM(CASE WHEN status='paid' THEN amount ELSE 0 END)`) and computed
measures (`SUM(quantity * unit_price)`).

**AST (closed, depth-capped union).**

```
Expression =
    | ColumnExpr(col: "Table.Column")
    | LiteralExpr(literal: scalar)
    | BinaryOpExpr(op: Literal["+","-","*","/"], left: Expression, right: Expression)
    | FunctionExpr(fn: <closed set>, args: List[Expression])   # nesting allowed, depth-capped
    | CaseExpr(when: List[(WhereNode, Expression)], else: Optional[Expression])

# Aggregates gain an expression argument:
AggregateSelectItem.arg: Optional[Expression]   # coexists with the legacy col:str for one release, then col becomes sugar for ColumnExpr
```

Keep the union **closed** (fixed operator set, fixed function set). `FunctionExpr.fn`
is an enum, not a free string. Reuse `WhereNode` for `CaseExpr` conditions (built on
Phase 0).

**Compiler.** Add a single `_compile_expression(expr, tables, alias_map, ctx)`
recursive function that mirrors `_compile_where`'s shape: `ColumnExpr` →
`_column`; `LiteralExpr` → bound literal; `BinaryOpExpr` → SQLAlchemy operator
(`left + right`, guarded divide — see caps); `FunctionExpr` → `func.*` (universal)
or `DialectAdapter` (per-dialect fns); `CaseExpr` → `sa.case`. Aggregates call
`_compile_expression` on `arg` instead of `_column`. `_build_select_columns`,
`_resolve_predicate_target`, group_by, and order_by all route computed values
through this one function.

**Validation.** Extend `select_item_column_refs` / `predicate_column_refs` to
recurse through `Expression` and yield every `ColumnExpr.col` at the right
`RefPosition` (`SELECT_NESTED` inside a projection, `WHERE`/`HAVING`/`GROUP_BY`/
`ORDER_BY` elsewhere). **This is the make-or-break safety step** — an unvisited
`ColumnExpr` deep in a `BinaryOpExpr` is a policy/mask bypass.

**Caps (new).** `Policy.max_expression_depth` (default ~5) and
`Policy.max_expression_nodes` (per query, summed tree-wide via
`_enforce_tree_wide_caps`). Division renders through a guarded form
(`left / NULLIF(right, 0)`) or documents dialect-native divide-by-zero behavior —
**decide in the open and record it** (§8). Depth/node caps stop a pathological
deeply-nested expression from becoming a CPU/plan DoS.

**Dialect.** Arithmetic and `coalesce/lower/upper/trim/concat/cast/round/floor/
ceil/abs/substring/nullif/replace` are dialect-universal → stay off the adapter
(the adapter docstring already says dialect-universal functions don't belong on it).
`length` (`LEN` vs `LENGTH`) and any fn with real per-dialect divergence go **on**
`DialectAdapter` as new methods, rejecting where genuinely absent.

**Adversarial / tests.**
- Denied/masked column nested arbitrarily deep in `BinaryOpExpr`/`FunctionExpr`/
  `CaseExpr` → rejected (the headline test).
- `max_expression_depth` / `max_expression_nodes` fire, including on the SUM across
  a subquery.
- Division-by-zero is guarded (or documented) — assert behavior, not a 500.
- Type-mismatch (`'a' * 2`) surfaces as a clean typed `QueryValidationError`, mapped
  like the write path already maps constraint errors — never a raw driver error or a
  schema-leaking message.
- Audit event for a query with arithmetic literals carries no literal values.

**Migration note.** Keep the legacy `col: str` aggregate form working during
transition (treat it as sugar for `ColumnExpr`), so existing queries and tests don't
break; converge on `Expression` in a follow-up cleanup.

---

### Phase 2 — Item 101: general window functions (`WindowSelectItem`) ★

**Goal.** `SUM/AVG/MIN/MAX/COUNT OVER`, `ROW_NUMBER/RANK/DENSE_RANK`,
`LAG/LEAD/NTILE/FIRST_VALUE/LAST_VALUE`, with `PARTITION BY`, `ORDER BY`, and frames
(`ROWS/RANGE … PRECEDING/FOLLOWING/CURRENT ROW`). Unlocks running totals, moving
averages, percent-of-total (with Phase 1), and gap/island analysis.

**AST.** New `WindowSelectItem` select-item type:

```
WindowSelectItem(
    fn: <closed window-fn set>,
    arg: Optional[Expression],           # e.g. SUM(amount) — reuses Phase 1
    partition_by: List["Table.Column" | alias],
    order_by: List[OrderBySpec],
    frame: Optional[WindowFrame],        # rows|range, start, end
    alias: str,                          # required (like CaseSelectItem)
)
```

Keep `top_n` as-is — it also *filters* to the top N; `WindowSelectItem` only
*projects* a window value. Do not merge them.

**Compiler.** `expr = fn(arg).over(partition_by=…, order_by=adapter.order_by_terms(…),
rows=/range=…)`. Reuse `_apply_top_n`'s existing subquery-materialization insight:
most dialects (incl. MSSQL) won't let `OVER()` reference a same-statement SELECT
alias, so window inputs must reference real columns/derived-table columns, not
peer aliases — factor that rule out of `_apply_top_n` and share it.

**Validation.** Wire `partition_by` / `order_by` / `arg` refs into the visitor.
New cap `Policy.max_window_specs` (per query, summed tree-wide) + a frame-bound
sanity cap so `UNBOUNDED PRECEDING … UNBOUNDED FOLLOWING` is a deliberate choice,
not an accident.

**Dialect.** Core window fns + `ROWS`/`RANGE` frames are supported on both
Postgres and MSSQL → mostly off-adapter. But keep the reject-don't-emulate door
open: if a specific frame or fn form is genuinely absent on a dialect, the adapter
rejects rather than emulating. Verify each fn/frame renders *and runs* on both real
backends (the `stddev`/`percentile_cont` "renders fine, breaks live" trap — items
75/82 — applies here; assert against real Postgres and real MSSQL, not just SQL text).

**Adversarial / tests.** Window over a masked column in `arg`/`order_by`/
`partition_by` → rejected; `max_window_specs` fires; frame-cap fires; per-dialect
run tests on real backends.

---

### Phase 3a — Item 102: `EXTRACT`/date_part + relative-date/interval helpers

**Goal.** `EXTRACT(dow/hour/year …)`, and relative-date filtering
(`created_at > now() - interval '7 days'`) without the caller hand-computing a
timestamp literal. Very high everyday value for agents doing "last N days."

**AST.** Extend `FunctionExpr` (Phase 1) with `extract(part, expr)` and a bounded
interval/`current_date`/`now` construct. Interval magnitude is a **capped literal**
(e.g. `max_interval_days`), not free — an unbounded interval is a cost lever.

**Dialect.** `EXTRACT` (Postgres) vs `DATEPART` (MSSQL); `now()`/`CURRENT_TIMESTAMP`
vs `SYSUTCDATETIME`; `col - interval` vs `DATEADD` → all `DialectAdapter` methods.

**Composition note.** Document that relative-date filtering is *already* achievable
today by the agent computing the cutoff and passing a literal — this item is native
convenience, not a hard unblock. Prioritize accordingly.

**Adversarial / tests.** Interval cap fires; per-dialect render+run; no timezone
ambiguity leaks (pick UTC semantics and document).

---

### Phase 3b — Item 103: non-equi/range joins + FULL OUTER / CROSS

**Goal.** Range/temporal joins (`ON price BETWEEN band.lo AND band.hi`,
`ON a.date < b.date`), `FULL OUTER JOIN`, and `CROSS JOIN`.

**AST.** Generalize `JoinSpec` from equality-pairs (`on` / `extra_on`) to an
optional `condition: WhereNode` (reusing Phase 0/1 machinery), keeping the equality
`on` form as the common-case sugar. Add `"full"` and `"cross"` to `JoinType`
(`cross` takes no condition).

**Compiler.** `stmt.join(right, _compile_where(join.condition, …), full=…,
isouter=…)`; `CROSS` → `stmt.join(right, sa.true())` / `select_from` cartesian.

**Caps.** `CROSS` is a cartesian cost lever — gate it behind a policy flag
(`allow_cross_join`, default off) and/or a stricter row cap; count it in the join
cap. Non-equi conditions count as join predicates.

**Dialect.** FULL OUTER and non-equi joins are universal across PG/MSSQL. No
adapter work expected; verify on real backends.

**Adversarial / tests.** Denied/masked column in a non-equi `condition`; cross-join
gated by policy; join cap still summed tree-wide.

---

### Phase 4a — Item 104: set operations (UNION / INTERSECT / EXCEPT)

**Goal.** Server-side `UNION [ALL]`, `INTERSECT`, `EXCEPT`.

**AST.** New top-level shape wrapping N `StructuredQuery` arms + op + `all: bool`.
Arms must have matching select arity/types. This is a new **scope container** —
extend `iter_query_scopes` so each arm is validated as its own scope (mirror item
97's scoping exactly).

**Caps.** `Policy.max_set_op_arms`; all existing caps summed across arms via the
tree-wide enforcer. Each arm gets full policy/schema validation and its own
mandatory-row-filter / min-group injection.

**Dialect.** Universal. `EXCEPT`/`INTERSECT` naming is identical on PG/MSSQL.

**Adversarial / tests.** Denied table/column in any arm → rejected; caps summed
across arms; mandatory row filters + k-anon apply to every arm (a set op must not
be a channel to dodge a per-table filter).

**Composition note.** `UNION ALL` is partly composable via multiple round-trips +
client merge; server-side dedup/`INTERSECT`/`EXCEPT` are the real unblock.

---

### Phase 4b — Item 105: CTE / derived table in FROM (non-recursive)

**Goal.** A subquery as a FROM/JOIN source and named `WITH` blocks, enabling
multi-stage single-statement analysis (aggregate-then-join, dedup-then-rank).

**AST.** Allow `from`/`JoinSpec.table` to be a named subquery (a `StructuredQuery`
+ alias) in addition to a physical table name. New scope container → extend
`iter_query_scopes`, `effective_name_map`, and the reflected-tables plumbing
(`subquery_tables` map already exists for item 97 — generalize it).

**Caps.** `Policy.max_cte_count` + reuse `max_subquery_depth` for nesting. Summed
tree-wide.

**Adversarial / tests.** A CTE must not become a channel to reference a denied
table, dodge a mandatory row filter, or surface a masked column as a non-projection
input to the outer query. Test each.

**Recursive CTE is OUT of scope here** — unbounded recursion is a genuine DoS.
Only revisit with a hard iteration cap in a separately-recorded decision.

---

### Phase 5 — Item 106: correlated / EXISTS / scalar subqueries (highest risk, last)

**Goal.** `EXISTS`/`NOT EXISTS`, correlated subqueries, and scalar subqueries
(`= (SELECT …)`, subquery in SELECT/HAVING).

**Why last.** The entire current subquery layer is deliberately **uncorrelated**
(`_compile_in_subquery` compiles the subquery against its *own* reflected tables, an
independent scope). Correlation means a subquery references an *outer* table — a new
resolution scope neither the visitor nor the `subquery_tables` map models today.
This is the biggest safety-surface addition in the plan.

**Design constraints (must all hold).**
- Correlation is limited to a **declared, capped** set of outer column refs — not
  "any outer column," so the visitor can enforce policy/masking on the correlated
  refs against the *outer* scope's name map.
- `max_subquery_depth` and all count caps stay summed tree-wide.
- Scalar-subquery arity is enforced (exactly one row, one column) at validation
  time where possible, else the cap/limit strategy is documented.
- Composition note: scalar-aggregate-comparison (`spend > overall average`) is often
  achievable **today** via two round-trips (compute the aggregate, pass as a
  literal) — document that recipe; this item is for the single-statement case.

**Adversarial / tests.** Correlated ref to a denied/masked outer column → rejected;
correlation cannot escape the outer scope's mandatory row filters; depth/count caps
fire across the correlated tree.

---

## 5. The regression bar — canonical analyst queries

This is the **acceptance suite** for "a fluent SQL author doesn't feel restricted."
Encode each as an end-to-end test (real Postgres where the feature is
Postgres-expressible; assert per-dialect rejection where a dialect genuinely lacks
it). The bar grows as new walls are discovered — a new "I couldn't express X"
report becomes a new row here first, then an item.

| # | Query | Works today? | Unblocked by |
| --- | --- | --- | --- |
| 1 | `SUM(quantity*unit_price)` where paid | ✅ **100** | 100 |
| 2 | Per-region `SUM(CASE WHEN status='paid' THEN amount ELSE 0 END)` | ✅ **100** | 100 |
| 3 | 7-day moving average of daily orders | ❌ (2 queries) | 105 (**not** 101 — see below) |
| 4 | Each customer's most-recent order | ✅ | `top_n` (n=1) |
| 5 | Running cumulative total | ✅ **101** | 101 |
| 6 | Cohort retention via CTE | ❌ (multi-query) | 105 |
| 7 | Top category per region **by revenue** | ✅ **100** | 100 |
| 8 | UNION of high-value + dormant segments | ❌ (client merge) | 104 |
| 9 | Median order value per region | ✅ PG / ⛔ MSSQL | `percentile_cont` |
| 10 | Customers with no orders (anti-join) | ✅ | LEFT JOIN + `is_null` |
| 11 | Customers spending > overall average | ❌ (two round-trips) | 106 (or compose) |
| 12 | Orders in last 7 days | 🟡 (literal today) | 102 |
| 13 | Case-insensitive name search | ✅ | `lower(col) like …` |
| 14 | Rank products with ties (WITH TIES) | ✅ | `top_n fn=rank` |
| 15 | Each order's % of total (`amount / SUM(amount) OVER ()`) | 🟡 both halves exist; not in one expression | a window-as-`Expression` item (see below) |
| 16 | Price-band join (`ON price BETWEEN lo AND hi`) | ❌ | 103 |

Baseline at plan time: **5/16 fully expressible, 2 cleanly composable.** After
items 99 + 100: **8/16** — rows 1, 2 and 7 went green, each covered end-to-end in
`tests/integration/test_expression_end_to_end.py` and again on real Postgres in
`tests/integration/test_postgres_expression_substrate.py`. After item 101:
**9/16** — row 5 (running cumulative total) went green, covered in
`tests/integration/test_window_end_to_end.py` and on real Postgres *and* real
MSSQL in `tests/integration/test_cross_dialect_differential.py`. The remaining ❌
set is 3, 6, 8, 11, 16 — derived-table/set-ops/correlated/non-equi, which Phases
3b–5 finish.

**Two corrections item 101's build forced on this table, recorded per this
section's own rule rather than left as an aspiration:**

- **Row 3 is unblocked by item 105, not 101.** A 7-day moving average of *daily*
  order counts is a window over **aggregated** rows, and item 101's window is
  computed over the query's row scope — a window over grouped values needs the
  aggregation materialized as a derived table (105). Item 101 rejects the
  `group_by`+window combination outright rather than growing a second bespoke
  materialization path beside `_apply_top_n`'s (2026-07-26 Decision Log). A moving
  average over *row-level* values, which 101 does unblock, is covered instead.
- **Row 15 needs a new item, and it is deliberately not created here.** Both
  halves now exist (arithmetic from 100, `OVER` from 101) but a window is a
  select-item **projection**, not an `Expression` operand, so
  `amount / SUM(amount) OVER ()` is two projected columns plus client-side
  division. Making `WindowSelectItem` an `Expression` member would introduce a
  union member that is legal in some positions and illegal in others (never in
  `WHERE`, never inside an aggregate, never as a group key) — breaking the
  "legal everywhere a scalar is expected" property that keeps the substrate
  reviewable in one place. Whether that trade is worth a bounded
  projection-only exception is a maintainer call, so it is a recorded wall here,
  not a silently-added feature.

**One wall found during item 100's build, recorded here per this section's own
rule** ("a new 'I couldn't express X' report becomes a new row here first"): a
computed **GROUP BY / ORDER BY key** is expressible but only *indirectly* — project
the expression with an alias and reference the alias, the same route `date_bucket`
has always used. That was judged sufficient rather than widening `group_by`/
`order_by` to accept an inline `Expression`: the alias route already works, costs
the caller one extra select item, and keeps those two fields a flat list of names
that every cap and walker treats uniformly. Revisit only if real usage shows the
extra projection is a genuine obstacle.

---

## 6. Testing & validation strategy

Every item ships across the tiers it touches (CLAUDE.md's definition of done):

- **Unit** (`pytest -m unit`) — AST validation (accept/reject shapes), compiler
  rendering, cap enforcement, visitor ref-enumeration. New AST node ⇒ a
  `select_item_column_refs`/`iter_column_refs` test that asserts every nested ref is
  yielded (the anti-bypass test).
- **Integration** — end-to-end through the one pipeline against SQLite (internal
  compiler/execution path) for logic, and the real-Postgres suite
  (`make test-postgres-live`) for anything with dialect-specific rendering.
- **Real-DB matrix** — window fns, date/interval, set ops, non-equi joins,
  percentile-style features: assert they **run** on real Postgres *and* real MSSQL
  (or reject cleanly on MSSQL), not just that SQL text compiles. This is the
  standing guard against "renders fine, breaks live" (items 75/82).
- **Security / adversarial** (`make test-security`, `adversarial-probe`, item 36
  fuzz) — for **every** new node: denied-column-buried-deep, masked-column-in-nested-
  position, cap-bypass-via-subquery/arm/CTE, no-value-leak-into-audit. These are not
  optional; they are the reason the expressiveness expansion is allowed at all.
- **Gate** — `security-invariant-check` before each commit; `release-gate` before a
  release; `product-guide-sync` after each item that changes a customer-facing
  capability (all of them do).

**Coverage principle:** the adversarial suite must grow at least as fast as the
capability surface. A phase that adds expressiveness without adding a
visitor-bypass + cap-bypass test for its new node is not done.

---

## 7. Scoring rubric — how we know we hit 10/10

Re-rate honestly (1–10, no rounding up) after each phase, per CLAUDE.md's
self-review bar:

- **Expressiveness** — % of the canonical bar (§5) green + zero standing "wall"
  reports. Baseline ≈ 5.5. Target 10 after Phase 5 (single-statement) with
  documented composition recipes covering the rest.
- **Safety** — every new node has a passing visitor-bypass + mask-bypass +
  cap-bypass test; `security-invariant-check` and the fuzz corpus clean. Baseline
  ≈ 9. Target 10 = no known bypass on any path, held under the growing surface.
- **Strictness** — `extra="forbid"` everywhere; unions closed; fuzz finds no
  accepted-but-unmodelled input; every literal typed and capped. Target 10.
- **Structure** — zero inline `if dialect ==` at call sites; every variance point a
  `DialectAdapter` method; expression grammar closed + depth-capped; non-goal #7
  line recorded and respected. Target 10.

A phase that raises expressiveness while *lowering* any of the other three is a
regression, not progress — hold all four.

---

## 8. Decision Log entries required before code (record in `docs/PRODUCT_GUIDE.md`)

These are the deliberate-tradeoff calls that CLAUDE.md requires be recorded in the
open **before** implementation, not discovered after:

1. ✅ **RECORDED 2026-07-25** — **Bounded `Expression` substrate vs. non-goal #7.**
   Record *why* a closed, depth-capped expression tree (fixed operators, fixed
   function set, `max_expression_depth`/`max_expression_nodes`) is **not** the
   "open-ended expression grammar" non-goal #7 forbids — and what the hard boundary
   is (no arbitrary UDFs, no free function strings, no uncapped nesting).
   *Outcome: five stated boundaries; see the `docs/PRODUCT_GUIDE.md` Decision Log
   entry dated 2026-07-25.*
2. ✅ **RECORDED 2026-07-25** — **Division semantics.** Guarded divide
   (`NULLIF(denominator,0)` → NULL) vs. dialect-native error. Pick one, state why.
   *Outcome: **guarded** — `left / NULLIF(right, 0)`. Postgres raises
   unconditionally; MSSQL's behavior depends on `ARITHABORT`/`ANSI_WARNINGS`, which
   our `MSSQLSessionDialectAdapter` does not set — and that adapter DOES set
   `XACT_ABORT ON`, under which an unguarded divide-by-zero aborts the whole
   transaction, not just the row. Guarding makes it deterministic on both.*
3. ✅ **RECORDED 2026-07-26** — **Window frame bounds.** The default frame and the
   cap on unbounded frames. *Outcome: **no default frame is synthesized** (omitting
   `frame` emits no `ROWS`/`RANGE` clause, so the dialect's SQL-standard default
   applies, identical on PG/MSSQL), and unbounded frame ends are **not** separately
   gated — `UNBOUNDED PRECEDING … CURRENT ROW` is both the running-total idiom and
   SQL's own default, and an unbounded-both-ends frame is the same whole-partition
   scan as no frame at all, so gating it would be theater. The caps land on the
   genuinely unbounded magnitudes instead: `max_window_frame_offset` (frame and
   `lag`/`lead` distances) and `max_window_specs` (summed tree-wide). Three further
   bounds ride along: no window with `group_by`/aggregates, no aggregate window
   under `min_group_size`, and a numeric `RANGE` offset rejected on MSSQL. See the
   `docs/PRODUCT_GUIDE.md` Decision Log entry dated 2026-07-26.*
4. **Interval/relative-date cap and timezone semantics** (UTC vs server-local).
5. **CROSS JOIN gating** (policy flag default-off + row-cap rationale).
6. **Recursive CTE exclusion** — record that it is deliberately out of scope pending
   a hard iteration cap.
7. **Correlated-subquery scope model** — the declared, capped correlation-ref rule
   and how policy/masking is enforced against the outer scope.

Each phase's PR updates the relevant Decision Log entry and the matching
`PRODUCT_GUIDE.md` capability section, and refreshes the §5 regression table.

---

## 9. What NOT to do (guardrails against the easy mistakes)

- **Do not** add a new select-item/predicate node without wiring it into the
  canonical visitor — that is the classic silent policy/mask bypass.
- **Do not** enforce a new cap per-scope when it should be summed tree-wide — that
  reopens the item-97 "N here + N in a subquery" loophole.
- **Do not** emulate a missing per-dialect capability to force parity — reject and
  point at the primitive (item 74 posture).
- **Do not** reach for `literal_column`/`text` with caller-derived content — ever.
- **Do not** split arithmetic and conditional aggregation into two items — they are
  one substrate.
- **Do not** open the expression grammar (free function strings, uncapped depth,
  arbitrary UDFs) — closed and capped only.
- **Do not** let the adversarial suite lag the capability surface — a new node
  without its bypass tests is not done.
```
