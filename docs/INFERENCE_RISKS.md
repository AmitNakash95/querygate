# Inference and transitive-exposure risks

A design note (TODO.md item 55) enumerating **inference attacks** against the
`StructuredQuery` AST — attempts to learn a *denied* value without ever
directly selecting the denied column — and, for each shape, stating explicitly
whether QueryGate closes it or accepts it as a documented residual risk. It
complements `docs/THREAT_MODEL.md`; the adversarial regression cases live in
`tests/security/test_adversarial_security.py`.

## The core guarantee, and its exact boundary

Column allow/deny is enforced on **every column reference in every clause**,
not just the `select` projection. `validation/policy_validation.py`'s
`_iter_column_refs` walks the whole AST and checks each reference against the
resolved `Policy`; `validation/schema_validation.py`'s
`select_item_column_refs` / `predicate_column_refs` are the shared leaf
harvesters. So a denied column cannot be reached by *referencing* it anywhere.

What this guarantee does **not** cover: information a caller can reconstruct
using **only permitted references** — through a permitted column that is
semantically related to a denied one, or by observing aggregate/row-count
*results*. Identifier-level allow/deny cannot see semantics or result values,
so those are a different class of risk, handled below.

## Closed: direct reference in any clause (Class A)

Every AST position that can carry a column reference is harvested and policy-
checked. If any were missed, a denied value would leak indirectly (e.g.
`ORDER BY salary` then reading the row order, or `CASE WHEN salary > 100000`
then reading the flag). The parametrized
`test_denied_column_cannot_be_used_for_inference` proves each rejects a denied
column:

| AST position | Harvested by | Test id |
|---|---|---|
| `where` predicate | `predicate_column_refs` | `where` |
| `group_by` | `_iter_column_refs` | `group_by` |
| `having` predicate | `predicate_column_refs` | `having` |
| `order_by` | `_iter_column_refs` | `order_by` |
| `top_n.partition_by` | `_iter_column_refs` | `partition_by` |
| `top_n.order_by` | `_iter_column_refs` | `top_n` |
| join `on` key | `_iter_column_refs` | `join` |
| join `extra_on` composite key (item 76) | `_iter_column_refs` | `composite_join_extra_on` |
| join `condition` predicate tree (item 103) | `iter_column_refs` → `predicate_column_refs` | `join_condition_range_bound`, `join_condition_arithmetic_bound`, and the `join_condition` entry in the buried-expression matrix |
| scalar function arg (items 71/77) | `select_item_column_refs` | `scalar_fn_select` |
| `CASE WHEN` condition (item 72) | `select_item_column_refs` → `predicate_column_refs` | `case_when_condition` |
| `CASE ... THEN` value | `select_item_column_refs` | `case_then_value` |
| `CASE ... ELSE` value | `select_item_column_refs` | `case_else_value` |
| aggregate `col` | `select_item_column_refs` | `aggregate_col` |
| `percentile_cont` col (item 82) | `select_item_column_refs` | `percentile_cont` |
| `string_agg` col (item 80) | `select_item_column_refs` | `string_agg` |
| predicate `col_fn` arg (item 77) | `predicate_column_refs` | `predicate_col_fn` |
| predicate `value_col` (column-to-column) | `predicate_column_refs` | `predicate_value_col` |

The harvest is exhaustive by construction, not just by enumeration: a scalar
function's arguments are `ColArg | LiteralArg` with **no nested-function
variant** (`query_ast/models.py`, `ScalarFunctionCall`), and `CASE`
`then`/`else` are that same union — so there is no deeper expression tree a
column could hide inside. Any new AST node that can carry a column reference
**must** extend `select_item_column_refs` / `predicate_column_refs` and gain a
case here; that is the single place this guarantee is maintained.

## Residual: not closable by identifier allow/deny (Class B)

These use only permitted references. They are **accepted residual risks** in
v1, documented rather than silently ignored, each with a demonstrating test
asserting the current (allowed) behavior so the boundary is explicit and would
flip the day a closing feature lands.

### R1 — Derived or correlated permitted columns

A permitted column that is a coarsened form of a denied one (`salary_band`
permitted while `salary` is denied), or statistically correlated with it, still
leaks information about the denied value. Allow/deny works on identifiers, not
semantics, so it cannot detect the relationship.

- **Decision: closed by policy, not by the engine.** The mitigation is a
  configuration decision — if a column is a derived/bucketed form of a
  sensitive one, deny it too (or don't model it). QueryGate cannot infer which
  permitted columns are proxies for denied ones.
- Demonstrated by `test_derived_or_correlated_permitted_column_is_a_documented_residual`.

### R2 — Correlation in the underlying data

Two permitted columns, or a permitted column and externally-known facts, can
jointly narrow a denied attribute purely through the data's own distribution.
This is a property of the database contents, below QueryGate's layer.

- **Decision: accepted residual, out of scope for an access gateway.** No
  query-shape rule can close a correlation that exists in the stored data;
  addressing it belongs to data modeling / differential privacy at the source,
  not an identifier-level policy engine.

### R3 — Aggregate differencing / minimum group size

An aggregate over a highly selective *permitted* predicate can narrow a group
to a single row (a query-set-size / singling-out inference), and repeated
aggregates with and without a condition can isolate one individual's
contribution (multi-query differencing).

- **Single-query singling-out: closed by `Policy.min_group_size` (TODO.md item
  88), including across joins since item 118.** When set, the
  compiler injects `HAVING count(*) >= k` into every
  aggregate query, so any result group backed by fewer than *k* underlying rows
  is suppressed — `count(*) WHERE id = X` returns nothing rather than revealing
  a single individual. It is the aggregate analog of a mandatory row filter:
  policy-driven, injected, non-removable, and applied only to aggregate queries.
  When it is **unset** (the default), this remains an accepted residual — the
  guardrail is opt-in per connection. Behavior demonstrated both ways:
  `test_aggregate_has_no_minimum_group_size_documented_residual` (unset → the
  thin aggregate is allowed) and
  `test_min_group_size_closes_the_single_row_aggregate_singling_out` /
  `test_sqlite_end_to_end.py`'s suppression tests (set → suppressed).
  **Window functions cannot route around it (TODO.md item 101).** An aggregate
  window (`COUNT(*) OVER ()`, `AVG(x) OVER (PARTITION BY …)`) computes an
  aggregate without producing a result *group* for the `HAVING` floor to filter,
  and `COUNT(*) OVER ()` needs no projected column at all — so a below-*k* count
  would be readable even where every column of the table is denied. While
  `min_group_size` is set, aggregate window functions are therefore **rejected**
  at policy validation; ranking and offset windows (`row_number`/`rank`/
  `dense_rank`/`ntile`/`lag`/`lead`/`first_value`/`last_value`) stay allowed
  because they only surface values the caller may already project bare. Asserted
  by `test_window_aggregate_cannot_dodge_the_k_anonymity_floor`.
- **A fan-out JOIN cannot defeat the floor (TODO.md item 118, closed
  2026-07-27).** The floor counts **joined** rows, so a join matching many
  right-hand rows per left-hand row would multiply a group's count past *k*.
  Measured before the fix with `k=5`: with no join the singleton was suppressed;
  with `JOIN big ON person.tenant = big.tenant` — an **equality** join, so the gap
  predated item 103's non-equi form and could not be fixed by rejecting
  inequalities — it was returned. Such a join is now **refused** on an aggregate
  query rather than answered, the same fail-closed posture item 101 took for
  aggregate windows under this floor. The refusal is scoped by reflected
  uniqueness metadata, not blanket: a join whose equality pins a set of the target
  table's columns covering a primary key or unique constraint matches at most one
  row, cannot inflate a count, and is allowed — so the ordinary
  join-to-a-dimension-on-its-key shape is unaffected. Range joins, cross joins,
  joins on a non-unique column and non-conjunctive conditions are refused.
  Asserted by `test_k_anonymity_floor_cannot_be_defeated_by_a_fan_out_join`, which
  was inverted from the test that originally pinned the leak.
- **Multi-query differencing: bounded since item 179, still residual.**
  Isolating an individual by subtracting two *independently* compliant
  aggregates (each ≥ *k*) is not closed by a per-query group-size floor, and
  **closing** it properly still needs query-set auditing or differential
  privacy — both deliberately out of scope.

  What changed is that the sequence is now *bounded* rather than unlimited.
  `Policy.max_shape_repeats_per_window` / `max_aggregate_queries_per_window`
  (`execution/disclosure_budget.py`) cap, per (principal, connection, declared
  purpose, table) over a rolling window, how many times one aggregate query
  shape may be re-run and how many aggregate queries may touch one table at
  all. Both are off by default and both apply only where `min_group_size` is
  also set. The key property: because the recorded query shape is predicate-literal-free
  (`audit/events.normalize_query_shape`), a differencing probe walking a
  constant is **one** shape re-sent N times, so counting re-runs is what
  detects it.

  **Read the limits honestly.** A caller *inside* its budget still differences
  successfully — this raises the cost of the attack and caps the disclosure per
  window, it does not prevent it. And because the shape is predicate-literal-free, the
  budget cannot tell a probe from an innocent repeat of the same query, so it
  is deliberately conservative and will also count benign repetition. No
  threshold is recommended: we have not calibrated these against real traffic.
  Asserted by `tests/unit/test_disclosure_budget.py` and
  `tests/integration/test_disclosure_budget_e2e.py`, whose headline test runs an
  actual salary-differencing probe and proves it is cut off partway through.

### R4 — Existence and row-count probing

Any query interface that returns rows or counts lets a caller confirm whether
rows matching a permitted predicate exist. This is inherent to permitting reads
at all.

- **Decision: accepted residual, mitigated in depth, not eliminated.** Existing
  controls shrink the surface without pretending to remove it: mandatory row
  filters (item 6) bound every query to the caller's own partition, column
  masking (item 49) removes raw sensitive values from results, per-principal
  quotas (item 50) rate-limit the probing needed for a differencing attack, the
  cumulative disclosure budget (item 179) bounds how many times one aggregate
  shape may be re-run against a *k*-floored table, and the audit trail (item 23)
  makes a probing pattern observable after the fact.

## Summary

Class A (direct reference in any clause) is **closed and regression-locked** —
the exhaustive test above fails if any future AST node reintroduces an
unharvested reference. Class B (semantic correlation, derived columns,
aggregate differencing, existence probing) is **not closable by identifier
allow/deny**; R1 is closed by policy configuration, R3's single-query
singling-out is closed by the opt-in `Policy.min_group_size` guardrail (item 88),
including across joins since item 118 — with multi-query differencing **bounded
but not closed** by the opt-in disclosure budget (item 179), and R2/R4 are accepted
residuals mitigated in depth by mandatory filters, masking, quotas, and audit.

*Wording note (item 104, 2026-07-27).* "Multi-query differencing" is now slightly
imprecise: a set operation lets a caller express `A EXCEPT B` in a **single**
statement, so the differencing is one request, one audit event and one quota unit
rather than several. The **guarantee is unchanged** — every arm is independently
policy-checked and independently floored by `min_group_size`, so a set operation
reveals nothing two separate round-trips did not already reveal, which is why it
was not treated as a new risk class. What changed is only that this residual is
now cheaper to exercise and *more* visible in the audit trail, not less.
