---
name: dialect-primitive
description: >-
  Add or extend dialect-specific SQL rendering the right way: implement a
  DialectAdapter method per dialect, translate mechanically where dialects differ,
  and REJECT (don't emulate) where a dialect genuinely lacks a capability. Use when
  adding a new AST feature that renders differently per dialect, adding a dialect,
  or touching compiler/dialect_adapters.py. Enforces the "expose primitives, don't
  spoon-feed the agent" doctrine.
---

# dialect-primitive — DialectAdapter work, done to doctrine

Governs [compiler/dialect_adapters.py](../../../src/querygate/compiler/dialect_adapters.py)
and its call sites. The engine exposes primitives as expressive as raw SQL,
bounded by schema/policy/dialect — it does **not** pre-solve query construction
for the agent.

## The three-way decision (from CLAUDE.md "Engine philosophy")

For a per-dialect rendering difference, classify it:

1. **Mechanical translation of the same AST operation into each dialect's idiom
   → DO IT.** That's what a compiler is for: `date_trunc` vs `DATEADD/DATEDIFF`,
   `STDEV`/`VAR` naming, `STRING_AGG` vs `group_concat`. Every `DialectAdapter`
   method is this shape.
2. **Dialect genuinely lacks the capability → REJECT, don't emulate.** The
   adapter method on that dialect raises `QueryValidationError` explaining the
   gap (e.g. `array_agg` on T-SQL — no array type exists). Do not invent an
   emulation.
3. **Synthesizing query structure the AST never asked for → NEVER,** to paper
   over a missing keyword. Precedent (item 74): `MSSQLDialectAdapter.order_by_terms`
   used to inject an extra CASE sort column for `nulls`; it now **rejects** `nulls`
   on MSSQL and points the agent at primitives it already has (`CaseSelectItem`
   for the bucket + multiple `OrderBySpec` entries for the tie-break).

When in doubt between 1 and 2/3: is the difference *how the same operation is
spelled* (→ translate) or *whether the operation exists at all* (→ reject)?

## Mechanics

- One concrete adapter class per dialect: `PostgresDialectAdapter`,
  `MSSQLDialectAdapter`, `SQLiteDialectAdapter` (SQLite is internal test/example
  only — not a registry dialect).
- Add the abstract method to `DialectAdapter` with a docstring describing the
  single AST concept it renders. Nothing dialect-universal belongs on the
  interface (count/sum/avg/min/max, coalesce/lower/upper/trim/concat stay in the
  dialect-agnostic compiler).
- Implement it in **every** adapter (reject where unsupported, per rule 2).
- Register in `_ADAPTERS`; dispatch via `get_dialect_adapter(dialect)`. **Never**
  an inline `if dialect == ...` at a call site in `sqlalchemy_compiler.py`.
- Rejections raise `QueryValidationError` with a message that names the gap and
  points at the primitives the agent can compose instead.

## Deliberate-exception clause

If a case seems to genuinely warrant the engine doing *more* than mechanical
translation (synthesizing structure for the agent), that is **not** a default —
it's a design discussion to have explicitly with the user, or record as a
reasoned Decision Log entry in `docs/PRODUCT_GUIDE.md` (see the
`product-guide-sync` skill), before implementing. Reject-and-point is the default.

## Tests

**A rendering assertion is not evidence.** This is the single most important
rule here, and it is written from a real failure (item 100, 2026-07-25): a
`CAST(x AS text)` mapping shipped with a *wrong justification* — "T-SQL's TEXT
is deprecated and not comparable with `=`" — because it was backed only by an
assertion on generated SQL text. A **connected** SQL Server renders that
spelling as `VARCHAR(max)`, which does not error at all; it silently mangles
non-ASCII (`'δ-λ'` → `'d-?'`). The rendering test passed happily against the
broken spelling. Compare with items 75/82, where SQLAlchemy compiled
T-SQL-invalid `within_group()` SQL with no complaint: **the compiler will render
whatever you ask for, so SQL text cannot distinguish correct from
merely-correct-looking.**

Two traps specifically:

- `sa.dialects.mssql.dialect()` constructed standalone is **not** the dialect a
  live connection uses. A connected dialect sets `deprecate_large_types` (and
  other server-version flags) after interrogating the server, so type names and
  some constructs render differently in a test than in production. If you
  compile with an unconnected dialect, you are testing a fiction.
- Silent wrongness beats loud wrongness for danger. A syntax error surfaces
  immediately; a codepage/collation/rounding difference returns *data*.

So, required:

- **Execute against every real dialect that implements it, and assert a VALUE.**
  `tests/integration/test_postgres_expression_substrate.py` and
  `test_mssql_expression_substrate.py` are the pattern: they are parameterized
  over the compiler's exported `DIALECT_ROUTED_EXPR_FNS` / `CAST_TARGETS`, and a
  guard test asserts the live cases cover exactly those sets — so a new
  per-dialect primitive **cannot** be added without real-database coverage on
  both backends. Extend those sets and the suites will demand the cases.
- **Mutation-check it.** Revert your rendering to the other dialect's spelling
  and confirm the live test fails with the real server error. If it still
  passes, the test proves nothing — that is exactly how the `CAST` mistake was
  caught. Record the observed error in the test docstring.
- Rendering assertions are still fine as a *fast* unit-level check of the
  translation, but label them as such; they are never the evidence for a claim
  about a dialect's behavior.
- Assert the unsupported-on-dialect path raises `QueryValidationError` with a
  message that mentions the primitive alternative (not a silent fallback).

Run them with `make test-postgres-live` and `make test-mssql-live` — the latter
starts and seeds both databases for you (the cross-dialect differential suite
needs Postgres up as well).
