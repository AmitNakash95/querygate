# QueryGate Product Guide

**Audience:** human developers (new hires, contributors, and anyone
prepping to talk about the product — including for marketing/sales
conversations).

**Purpose:** explain, in plain language, what QueryGate is, how it works,
why it was built this way, and what terms/tools show up in the codebase.
Read this instead of reverse-engineering the architecture from source.

**Style rules for anyone writing in this doc:**
- Simple, plain English. Explain jargon the first time it's used.
- Prefer "why" over "what" — the code already shows what; this doc should
  carry the reasoning a reader can't get from `git log` alone.
- Short sections, scannable headers, no walls of text.
- Every claim should be traceable to real code/docs (link file paths), not
  vibes.

---

## Maintenance protocol (read this if you're an agent)

This document is **living** and must stay in sync with the product.

**After finishing any non-trivial task in this repo**, ask: *did this task
introduce or change something a human would need to know to understand the
product's architecture, a technical decision, a new term/tool, or a
customer-facing capability?*

- If **yes** — add or update the relevant section below (and the
  [Decision Log](#decision-log) if it was a deliberate tradeoff), in the
  same style as the rest of the doc. Keep edits small and targeted.
- If **no** (pure bug fix, refactor with no behavioral/architectural
  change, test-only change) — skip it. Don't pad this doc.

Do not do a large speculative rewrite as a side effect of an unrelated
task. Small, incremental, accurate updates only.

---

## Table of contents

0. [TL;DR — read this before a demo](#tldr--read-this-before-a-demo) — *status: populated*
1. [What is QueryGate](#what-is-querygate) — *status: populated*
2. [The Core Request Pipeline](#the-core-request-pipeline) — *status: populated*
3. [Security Model](#security-model) — *status: populated*
4. [Connections, Policy & Configuration](#connections-policy--configuration) — *status: populated*
5. [Catalog / Semantic Layer](#catalog--semantic-layer) — *status: populated*
6. [Auth & Transports (REST + MCP)](#auth--transports-rest--mcp) — *status: populated*
7. [Testing, Release & Operations](#testing-release--operations) — *status: populated*
8. [Glossary of Terms](#glossary-of-terms) — *status: populated*
9. [FAQ (for marketing/positioning conversations)](#faq-for-marketingpositioning-conversations) — *status: populated*
10. [Decision Log](#decision-log) — *status: populated*

---

## TL;DR — read this before a demo

**Read time: about 3 minutes.** This section exists so you can walk into a
demo or a hallway conversation and sound confident without having read the
rest of this document. Everything here is explained in plain words on
purpose — no jargon without a definition next to it. If a question goes
deeper than this section, the [FAQ](#faq-for-marketingpositioning-conversations)
below has a longer, more precise answer for almost anything you'll get asked.

### The 10-second pitch

**QueryGate lets an AI agent query your company's real database, but the
agent can never send it a raw command — only a pre-checked, structured
request. That one design choice is why it's safe.**

Think of the difference between handing someone a blank check versus a form
with fixed fields (amount, payee, date) that a teller checks before it's
ever cashed. Most "AI talks to your database" tools hand the AI a blank
check — a text box where it writes SQL (the language databases understand)
and hope it behaves. QueryGate never gives it that text box. The AI can only
fill out the form.

### The problem, in one paragraph

If you let an AI agent write its own SQL, you're trusting a system that
sometimes makes things up, can be tricked by hidden instructions in the data
it reads ("prompt injection"), and might phrase a legitimate-sounding
request that happens to touch a table it should never see. A read-only
database account doesn't fix this — it can still read *every* table it has
access to and run something slow enough to hurt the database for everyone
else. QueryGate closes that gap structurally: since the agent can never
write SQL in the first place, there's no SQL string for a mistake or an
attack to hide inside.

### How it works, in four steps

1. **The agent sends a request as data, not code** — a JSON object naming
   which table, which columns, which filters. It looks like a form, not a
   sentence.
2. **QueryGate checks the request** against two things before touching the
   database: does this table/column actually exist, and is this caller
   *allowed* to see it (an admin-configured allow/deny list, per database,
   sometimes per person).
3. **Only if both checks pass**, QueryGate turns the request into real,
   safe SQL and runs it — with a timeout and a cap on how many other queries
   can run at once, so nothing can accidentally overload the database.
4. **Every attempt is logged** — who asked, what they asked for, whether it
   was allowed — without ever writing down the sensitive values themselves,
   so the log is safe to store and safe to show an auditor.

### What makes it hard to copy (the three-part story)

Say these three points together — they're the whole pitch, and no single
competitor does all three:

1. **Structural safety.** Not "we scan the SQL for bad words" — there is no
   SQL for the agent to write, so there's nothing to scan. This also lets us
   enforce *shape* rules a scanner can't reach — "no more than 3 joins," "no
   filters that would return under 5 rows" (protects against fishing for one
   person's data one narrow query at a time).
2. **It runs on your real, live database.** Not a copy, not a warehouse we
   host, not a semantic layer that needs a modeling project first. It
   connects to the operational database you already run, self-hosted inside
   your own infrastructure — data and credentials never leave your network.
3. **Proof, not just prevention.** Every access is tied to the actual human
   behind it (not just "an API key"), and the audit log is tamper-evident —
   there's a cryptographic way to prove afterward that nobody edited it.
   That's the thing a security reviewer actually needs to sign off.

### What it explicitly does *not* do (say this with confidence, don't dodge)

These are deliberate choices, not gaps we haven't gotten to — if asked, say
"that's on purpose" and move on:
- No mode that lets an agent run a raw SQL string, ever.
- No running AI-generated code of any kind.
- No stored procedures / arbitrary procedural SQL.
- No mandatory upfront modeling step before you can start querying — it
  works against the schema you already have.
- We don't host a database or a data warehouse ourselves.

### Quick answers to the questions you'll actually get asked

- **"What databases does it support?"** Postgres, SQL Server, and MySQL
  today, tested against real servers. Snowflake and BigQuery can already
  generate correct queries but aren't wired up for live connections yet —
  say "in progress," not "supported."
- **"Isn't this just a firewall that blocks bad SQL?"** No — a firewall
  still has to accept a SQL string and guess whether it's dangerous.
  QueryGate never accepts a SQL string, so there's nothing to guess about.
- **"Can the AI ever break out and run something arbitrary?"** No — there is
  no field or tool anywhere in the product that accepts raw SQL. It's not a
  setting you could even turn on.
- **"Does our data ever leave our network?"** No — QueryGate runs inside
  your own infrastructure, next to your database.
- **"Can we change the rules without downtime?"** Yes — policy changes
  reload live, with a versioned history and one-click rollback.
- **"How do you know the security claims actually hold?"** Every one of
  them is backed by an automated test that runs on every code change,
  including a dedicated suite whose whole job is to try to break the
  access rules. Not a one-time audit — continuous enforcement.
- **"How much time does QueryGate add to a query?"** Low single-digit
  milliseconds in a representative run — regenerate it yourself with
  `make performance-benchmark` for your own hardware, never quote a number
  from memory. It compiles your query into standard SQL rather than
  generating different query structure, though it does apply session-level
  guardrails (a statement/lock timeout) Postgres then executes under.

If you get a question this section doesn't cover, it's almost certainly in
the [FAQ](#faq-for-marketingpositioning-conversations) further down — that
section is written for exactly this purpose, just in more depth.

## What is QueryGate

QueryGate is an **agent-safe database access gateway**: it lets you connect
an AI agent to a real database — Postgres, SQL Server (MSSQL), or MySQL
today, all verified against real servers, with Snowflake and BigQuery
rendering-only for now (see the FAQ's "What databases does it support?") —
but the agent can never submit raw SQL. There is no `sql` field, tool, or
endpoint anywhere in the codebase that would accept one. Every request goes
in as a validated, structured JSON object instead, and QueryGate runs it
inside your own infrastructure, next to your database, so credentials and
data never leave your network.

### The problem it solves

The obvious way to let an AI agent answer questions from a database is to
give it a tool like `run_sql(query: str)` and let the model write the SQL
itself. That means trusting a probabilistic text generator to never write
`DROP TABLE`, never wander into a table it shouldn't see, never run an
unbounded join that takes the database down, and never follow an instruction
smuggled in from a document it happened to read (a **prompt injection** —
malicious text hidden in data the agent processes, worded to look like an
instruction).

The usual half-measures don't hold up:

- A **read-only database user** still lets an agent read every table you
  didn't mean to expose, and run something expensive enough to degrade the
  database for everyone else.
- **Prompt-level instructions** ("only query the `orders` table") are
  guidance, not enforcement — nothing stops the next prompt, the next model
  version, or an injected instruction from ignoring them.
- A **query timeout** limits how long a query runs, not what it's allowed to
  touch — it doesn't stop a query from reaching a table or column it should
  never have reached at all.

(For the full adversarial breakdown — what QueryGate specifically defends
against and what stays the operator's responsibility — see
`docs/THREAT_MODEL.md`; that's covered in depth in the Security Model
section of this guide.)

### Who it's for

Teams building an internal agent, copilot, or automated workflow that needs
controlled read access to a production database — typically in B2B software,
fintech, healthcare, or any engineering
organization where "let the model write SQL against prod" won't pass a
security review. The people in the room are usually a platform/AI engineer
building the integration, and a security or database owner who has to sign
off on it.

### The guarantee that makes it safe

Instead of a SQL string, a caller can only ever submit a `StructuredQuery` —
a fixed-shape JSON object (fields like `from`, `select`, `where`, `joins`,
`group_by`, `limit`; see `src/querygate/query_ast/models.py`) that rejects
any field it doesn't recognize. Because the input shape is fixed and known in
advance, QueryGate can check a query *before* it becomes SQL — validating
every table and column it touches against both a live copy of the real
schema and an explicit allow/deny policy — rather than trying to spot danger
inside an arbitrary string. Only after a query passes those checks does
QueryGate compile it into parameterized SQL and run it, under a timeout and
a concurrency limit, returning a capped set of rows. This applies identically
however the agent connects — over MCP (the standard AI agents use to call
tools) or over REST.

### The one-sentence version

If you had to repeat this back in a sales conversation: *QueryGate lets an
AI agent query your database, but the only thing it can ever send is a
pre-validated request object, not a SQL string — so there's no way for the
agent, a bad prompt, or an injected instruction to make it run something you
didn't explicitly allow.*

## The Core Request Pipeline

Every way a caller talks to QueryGate — a REST route in `src/querygate/api/routes.py`
or an MCP tool in `src/querygate/mcp/tools/*.py` — is a thin wrapper around one
class: `StructuredQueryService` (`src/querygate/execution/service.py`). There is
no other path to a database. Whether a request comes in over HTTP or over MCP
(Model Context Protocol, the standard AI agents use to call tools), it ends up
calling the same `execute()` or `explain()` method, which runs the same six
stages in the same order.

The input to all of this is always a `StructuredQuery` — never raw SQL text.
A `StructuredQuery` is a JSON object (an **AST**, short for "Abstract Syntax
Tree" — a structured, tree-shaped description of a query, as opposed to a flat
string of SQL) with fields like `from_table`, `select`, `joins`, `where`,
`group_by`, `limit`. See `src/querygate/query_ast/models.py`. Because a caller
can only ever submit this shape, QueryGate can inspect and reject a query
*before* it becomes SQL, instead of trying to detect danger inside a string
of arbitrary text.

The six stages, in the order every request passes through them:

### 1. Policy validation — the caps and allow/deny check

**File:** `src/querygate/validation/policy_validation.py`

This is the very first thing that runs, and it never touches a database. It
checks the incoming query's *shape* against the `Policy` configured for that
connection (`src/querygate/policy/models.py`):

- Is the connection even enabled?
- Does the query stay under configured caps — max `select` columns, max
  `joins`, max `where` nesting depth, max `group_by` columns, max `top_n`
  partition columns, max `top_n.n`?
- Does every table the query touches appear on that connection's allow list
  (and not on its deny list)?
- Does every *column* the query references — not just the ones in `select`,
  but also join keys, `where` predicates, `group_by`, `having`, `order_by`,
  and `top_n` — pass the same allow/deny check?

That last point is deliberate and easy to get wrong: a naive implementation
might only check columns that appear in `select`, letting a caller filter or
sort on a column they're not allowed to *see* (e.g. `where Employee.salary >
100000` without ever selecting `salary`) and infer its value indirectly.
`iter_column_refs` — the single canonical reference visitor (item 96) — walks
every clause of the query specifically so a denied column can't be smuggled in
through a side door.
(`tests/unit/test_policy_validation.py::test_denied_column_rejected_when_only_used_in_where`
locks this behavior in.)

#### The one-walk rule, in plain terms

This is worth understanding on its own, because it is the quiet reason the
allow/deny guarantee actually holds — and it is the part of the design that is
hardest for a competitor to copy.

**The problem.** "Don't let them see the `salary` column" sounds like one rule.
It isn't. A SQL query can mention a column in a *lot* of places: the select
list, a join key, an extra join condition, a `WHERE` predicate, `GROUP BY`,
`HAVING`, `ORDER BY`, a window's `PARTITION BY`, inside a `CASE`, inside
arithmetic, as an argument to a function or an aggregate, inside a date
expression — and then again inside every subquery, every CTE, and every arm of
a `UNION`. That's more than a dozen distinct positions.

The naive way to enforce a column rule is to check each of those places
wherever you happen to need it. That works right up until someone adds a new
AST feature and updates *eleven* of the twelve checks. The twelfth is now a
silent bypass, and no test fails, because the feature works perfectly — it just
isn't policed. This is the single most likely way a gateway like this gets
quietly broken over time.

**What QueryGate does instead.** There is exactly **one** function that knows
where columns can appear: `iter_column_refs`. It yields every real
`Table.Column` reference in a query, tagged with *which position* it came from.
Policy validation (allow/deny and masking), schema validation, and
`referenced_tables` all consume that same walk rather than each maintaining
their own. Add a new AST feature and you teach the walk once — every consumer
is enforced by construction, not by remembering.

Its sibling, `iter_query_scopes`, does the same job one level up: it yields the
outer query *plus* every CTE body, every set-operation arm, and every nested
subquery as an **independent** validation scope, each resolving against its own
tables. So a denied column is not reachable from inside a subquery either, and
complexity caps are summed across the whole tree rather than per level — which
is what stops nesting from being used as a cap multiplier.

**The detail that shows the idea is real.** Positions are not interchangeable,
and one distinction carries a security guarantee: a *bare* column in the select
list is the **only** place a masked column may appear. Ask for `salary * 2`
instead of `salary` and that reference is classified as *nested*, not bare — and
`policy_validation.py` then **rejects the query outright** rather than masking in
place, because an unmasked reference inside arithmetic (or a filter, join,
ordering, grouping, or function argument) would leak the real value by
inference. This is the same reject-don't-emulate posture the engine takes
elsewhere: refuse the shape rather than half-satisfy it.

Note the failure mode this closes. An implementation that applies masks by
matching a *column name* sees `salary * 2`, finds no bare `salary` to match,
and returns the true number. Here the classification happens in the shared walk,
so the enforcement can't be skipped by wrapping the column in something.
`tests/unit/test_reference_visitor.py` pins the classification ("a column inside
a computed projection must NOT claim that position, or masking silently stops
applying to arithmetic") alongside a kitchen-sink query exercising every known
position; `policy_validation.py` applies the rule per scope, so a masked column
can't hide inside a subquery either.

**It is not a performance feature — don't sell it as one.** Walking the AST once
instead of a dozen times is a rounding error next to the query itself. Policy
validation is fast for a different reason: it is a pure in-memory comparison
against config with no network call and no database round trip, so a violating
query dies before a connection is touched (see `docs/business/PERFORMANCE_BENCHMARK.md`
for the measured end-to-end overhead, and regenerate it on your own hardware
rather than quoting a number). The one-walk rule buys **correctness that
survives the next ten AST features**, which is a different and more valuable
thing than speed.

**What it does and does not prove.** It makes a whole class of bug unlikely —
the one where a new feature is added and eleven of twelve enforcement points get
updated. It is not a proof of correctness: the guarantee still rests on that one
visitor being right, exercised by the suite rather than formally verified, and
QueryGate has had no third-party penetration test. Claim *drift resistance*,
not *proven safe*. `docs/SECURITY_POSTURE.md` ("External attestations") states
this posture in the form a security reviewer will ask for.

**Why it matters commercially.** "No raw SQL" is the headline, but competitors
are converging on that claim. *Enforcement completeness* — the guarantee that a
denied column is denied in all dozen-plus places at once, including inside
subqueries — is the harder thing to reproduce, because it is an architectural
decision made early, not a feature that can be bolted on. See
`sales/comparison.html` for how to demo it.

**Why it runs first:** it's the cheapest possible check — pure in-memory
comparison against config, no network call, no database round trip — so an
over-cap or policy-violating query is rejected in microseconds, before
QueryGate spends any effort or any database connection on it.

**Column masking, not just allow/deny.** Allow/deny is binary — a caller
either sees a column's real value or can't reference it at all. A `column_mask`
policy entry (keyed by table like the allow/deny lists) is the middle ground:
the column stays selectable, but the database returns a *masked* value —
`hash` (deterministic one-way hash), `null`, `last` (reveal only the trailing N
characters, e.g. last-4 of a card), or `bucket` (round a number down to a
bucket width). The transform is applied **inside the compiled query** (a
SQLAlchemy `func` wrapper — see stage 3), so the raw value never leaves the
database for a masked caller; it is not a redaction of the response after the
fact. Masks resolve per principal exactly like every other policy field, so
one caller can see raw values and another sees them masked on the same
connection. A masked column may appear **only as a bare `select` item** —
using it in a `where`/`join`/`order_by`/`group_by` position, or nested inside a
function/CASE/aggregate, is rejected, for the same inference reason denied
columns are checked in every clause (see the Decision Log entry on masking
scope). The audit event records which output columns were masked (never the
pre-mask value), so an operator can tell "masked" from "denied" access apart in
the same stream.

### 2. Schema validation — does this actually exist?

**Files:** `src/querygate/schema/reflection.py`,
`src/querygate/validation/schema_validation.py`

Passing policy only proves a query is *shaped* correctly and *allowed* in
principle — it doesn't prove `Employee.salary` is a real table and column.
This stage reflects the real database schema and checks every table/column
reference in the query against it. **Reflection** here means SQLAlchemy
connecting to the database and reading its actual schema (table and column
names, types) — QueryGate never trusts a caller's claim that a table exists;
it always checks against a live copy of the real schema (cached after the
first lookup per connection, in `schema/reflection.py`'s `get_table_schema`).

This stage does two other things worth calling out:

- **Cross-connection joins.** A join can name a different `connection` than
  the query's primary one — for example, joining two databases on the same
  physical MSSQL server. `resolve_query_table_connections` in
  `schema_validation.py` only allows this when both connections share the
  same configured `join_group` in policy; otherwise it rejects the query with
  a message telling the caller to run separate queries per connection and
  combine the results themselves, rather than silently querying across a
  trust boundary.
- **The join graph must actually connect.** `_validate_join_graph` requires
  each declared join to link exactly one new table into the query, using a
  column that's actually part of the join condition — this catches a join
  clause that doesn't connect to anything the rest of the query touches.

**Why it runs after policy, but before compiling SQL:** reflecting a schema
means talking to the database (at least once, then cached), so it's more
expensive than the pure-config check in stage 1 — no reason to pay that cost
for a query that was already going to be rejected on caps or table/column
policy. But it still has to happen before compiling any SQL, because the
compiler (stage 3) assumes every identifier it's given is real; it doesn't
re-check.

### 3. Compilation — AST + Policy becomes real SQL

**File:** `src/querygate/compiler/sqlalchemy_compiler.py`

This is where the validated `StructuredQuery` finally becomes SQL — but
QueryGate never builds a SQL string directly. It builds a **SQLAlchemy Core
`Select`**: SQLAlchemy is a Python library for talking to databases, and
"Core" is its lower-level query-building layer (as opposed to its ORM/object
layer) — you assemble a query out of typed Python objects (tables, columns,
conditions), and SQLAlchemy renders the final SQL text and keeps user-supplied
values as separate bound parameters, not string-interpolated into the query.
That's what makes SQL injection structurally impossible here: there's never a
step where a value gets concatenated into a SQL string.

The compiler also applies the resolved `Policy` itself at this stage —
`_apply_mandatory_row_filters` adds any policy-configured `WHERE` conditions
a caller can't remove (e.g. tenant scoping), and `clamp_limit` caps the
result size even if the caller asked for something larger.

**Dialect-specific logic lives behind one interface.**
(`compiler/dialect_adapters.py`, TODO.md item 73, a compiler-scoped slice
of item 57's "pluggable dialect adapter" plan.) QueryGate supports Postgres,
MSSQL, and MySQL in production, live-verified (item 19 phase 1), plus
Snowflake and BigQuery at a rendering/compilation-only level that is **not**
live-verified (item 19 phases 2/3 — see the "What databases does it
support?" FAQ entry above and the 2026-08-06/2026-08-07 Decision Log entries
for the honest caveat; SQLite is used only internally, for tests/examples —
see `connections/models.py`'s `DatabaseDialect`). Almost the entire compiler
is dialect-agnostic SQLAlchemy Core; the things that genuinely differ per
database are each one method on a small `DialectAdapter` interface, with
one concrete adapter class per dialect (`PostgresDialectAdapter`,
`MSSQLDialectAdapter`, `MySQLDialectAdapter`, `SnowflakeDialectAdapter`,
`BigQueryDialectAdapter`, `SQLiteDialectAdapter`) rather than an `if dialect
== ...` branch scattered at each call site. Today that's date bucketing
(`date_bucket`) — a day/week/month/quarter/year truncation: Postgres has a
native `date_trunc()` that handles every granularity directly, MSSQL has
no equivalent so it's built from `DATEADD`/`DATEDIFF` (the standard MSSQL
truncation idiom), SQLite (test/example path only) uses `strftime()`
string formatting. The interface also defines `order_by_terms`, `stat_fn`,
`string_agg`, `array_agg`, `percentile_cont`, `scalar_function`, and
`window_frame` for the dialect-sensitive features (NULLS FIRST/LAST ordering,
`stddev`/`variance` aggregates, the `string_agg` aggregate, the `array_agg`
aggregate, the `percentile_cont` aggregate, the handful of item-100 scalar
functions that genuinely diverge, and the item-101 window frame grammar — see
the corresponding TODO.md items). `string_agg` is the one
case where the internal SQLite adapter does real work instead of raising:
SQLite's `group_concat(expr, sep)` happens to share the exact `(expr,
separator)` shape as Postgres's `string_agg`/MSSQL's `STRING_AGG`, unlike
stddev/variance (SQLite has no such functions at all), so it's a genuine
mapping rather than a stub — the only adapter method with a real SQLite
implementation today. `array_agg` goes the other way: Postgres gets a real
`array_agg(...)` implementation, but both MSSQL (no array/collection type
in T-SQL at all) and SQLite (`json_group_array()` returns a JSON string,
not a real array) raise `QueryValidationError` rather than faking one —
the first time a real, supported registry dialect (MSSQL), not just the
internal-only SQLite path, rejects a capability outright. `percentile_cont`
rejects on MSSQL too, but for a genuinely different reason worth
distinguishing from `array_agg`'s: Postgres's `percentile_cont(fraction)
WITHIN GROUP (ORDER BY ...)` is a true `GROUP BY`-compatible aggregate,
while T-SQL's `PERCENTILE_CONT` exists only as an analytic (window)
function requiring an `OVER (...)` clause — not a missing type, but an
incompatible structural form. Confirmed directly that this needed a real
adapter-level rejection rather than being left to fail at runtime: SQLAlchemy's
`within_group()` construct silently compiles identical SQL text against
both the Postgres and MSSQL dialect compilers with no dialect-level guard
of its own.

Adding a real third dialect (item 19) means implementing one new
`DialectAdapter` subclass, not a hunt through the compiler for
Postgres/MSSQL-flavored assumptions — that containment is the whole point
of the abstraction, not just where today's two dialects happen to differ.

### Computed expressions: arithmetic, conditional aggregation, nested functions

**Files:** `src/querygate/query_ast/models.py` (the `Expression` union),
`src/querygate/compiler/sqlalchemy_compiler.py` (`_compile_expression`).
**Item:** TODO.md 100 — the first big build of the
[Expressive Query Engine plan](ENGINE_EXPRESSIVENESS_PLAN.md).

Until item 100, "what can be projected or compared" was a **flat** set of
shapes: a bare `Table.Column`, a one-level function call with no nesting, an
aggregate over a bare column *name*, and a `CASE` whose result was a
column-or-literal. One boundary — no recursive scalar type — is why
`SUM(quantity * unit_price)` was impossible (no `*` operator existed anywhere,
*and* an aggregate argument could only be a column name), and why conditional
aggregation, nested functions, expression-valued `CASE`, and computed group
keys were all missing at once. They were the same gap wearing five hats.

There is now one recursive **`Expression`** used everywhere a scalar value is
expected:

| Node | Shape | Example |
| --- | --- | --- |
| `ColumnExpr` | `{"col": "Order.Total"}` | a column |
| `LiteralExpr` | `{"literal": 5}` | a bound literal |
| `BinaryOpExpr` | `{"op": "*", "left": …, "right": …}` | `quantity * unit_price` |
| `FunctionExpr` | `{"fn": "lower", "args": [ … ]}` | `lower(trim(name))` |
| `CastExpr` | `{"cast": …, "to": "numeric"}` | `CAST(x AS NUMERIC)` |
| `CaseExpr` | `{"when": [ … ], "else": …}` | conditional aggregation |
| `ExtractExpr` | `{"extract": …, "part": "hour"}` | one field of a timestamp (item 102) |
| `NowExpr` | `{"now": "timestamp"}` | the current UTC instant (item 102) |
| `DateAddExpr` | `{"date_add": …, "unit": "day", "amount": -7}` | shift a timestamp (item 102) |

The last three are covered in
[Dates and relative time](#dates-and-relative-time-the-last-30-days-without-doing-the-arithmetic).

It appears in four positions: a new `ExpressionSelectItem` projection
(`{"expr": …, "as": "line_total"}`), an aggregate's `arg`, and **both** sides
of a `Predicate` (`expr` / `value_expr`). So
`SUM(CASE WHEN status='paid' THEN amount ELSE 0 END)`,
`WHERE quantity * unit_price > 100`, and `lower(trim(name))` are all
expressible now. A **computed GROUP BY key** needs no new field: project the
expression with an alias and group by that alias — the same route
`date_bucket` has always used, rather than a second inline grammar for group
keys.

**This is deliberately not an open-ended expression grammar** (non-goal #7).
The boundary is recorded in the [Decision Log](#decision-log) and is five
things: the operator set is fixed (`+ - * /`); `FunctionExpr.fn` is an **enum**,
so no caller can name a UDF or stored procedure; nesting is capped
(`max_expression_depth`, plus `max_expression_nodes` summed **tree-wide** across
subqueries); every leaf is still a typed identifier or bound literal, so no
string is ever interpolated into SQL; and the item-96 canonical visitor recurses
through every node, so a denied or masked column buried in a `BinaryOpExpr` is
rejected exactly as it would be at the top level. That last one is the load-
bearing property — `tests/security/test_adversarial_security.py` asserts it for
a denied *and* a masked column in every position an expression can occupy,
including the subtle one (a column inside a `CASE`'s *condition*, which is a
predicate tree rather than an expression node). There is exactly **one**
recursion over the union (`iter_expression_parts`); the ref walk, the caps, and
the CASE rules are filters over it, and it raises on an unrecognized node rather
than silently skipping it — so a future member cannot open a hole by being
taught to some walks and not others.

**Division renders guarded** — `left / NULLIF(right, 0)`, so a zero denominator
yields NULL identically on every dialect instead of Postgres's hard error and
MSSQL's `XACT_ABORT`-driven whole-transaction abort. See the Decision Log for
why consistency won over dialect fidelity here.

**Dialect handling** follows the existing rule: `coalesce`/`lower`/`upper`/
`trim`/`concat`/`abs`/`floor`/`nullif`/`replace` are identical everywhere and
stay off the adapter; the four that genuinely differ — `ceil` (T-SQL spells it
`CEILING`), `length` (`LEN`), `round` (T-SQL requires the length argument;
Postgres has no `round(double precision, integer)` so the adapter casts to
`NUMERIC`), and `substring` — are one `DialectAdapter.scalar_function` method.
`substring` requires exactly three arguments at the AST layer *because* T-SQL
has no two-argument form: allowing it would render fine on Postgres and break
against a live SQL Server. Every one of these is verified by *executing* against
both real backends and asserting values — deliberately not by asserting SQL
text, which cannot distinguish a correct rendering from one that merely looks
correct. (`to: "text"` is the case that proves the point: it maps to
`NVARCHAR(max)` on MSSQL, and the wrong spelling does not error at all — it
silently mangles non-ASCII, which only a round-trip assertion catches.)

**Writes are unchanged.** A computed predicate in a write's `WHERE` is rejected
at write validation, the same posture item 110 established for subqueries —
the write path re-derives that WHERE for its affected-row count, its diff, and
its row cap, so widening it is a separately-scoped decision, not a side effect
of widening reads.

### Window functions: running totals, moving averages, rank in place

**Files:** `src/querygate/query_ast/models.py` (`WindowSelectItem`),
`src/querygate/compiler/sqlalchemy_compiler.py` (`_compile_window`),
`src/querygate/compiler/dialect_adapters.py` (`window_frame`).
**Item:** TODO.md 101 — the second pillar of the
[Expressive Query Engine plan](ENGINE_EXPRESSIVENESS_PLAN.md).

Until item 101, `top_n` was the **only** `OVER()` surface QueryGate had, and it
did exactly one thing: rank rows within partitions and *keep the top N*. So
"show me each order with a running total", "the 7-order trailing average", "each
customer's orders numbered 1..n", and "how much did this order change from the
previous one" were all unreachable — not because the database couldn't do it, but
because the AST had nowhere to say it.

A `WindowSelectItem` projects a window value without collapsing or filtering
rows:

```json
{"fn": "sum",
 "arg": {"col": "orders.total_amount"},
 "over": {"order_by": [{"col": "orders.id"}],
          "frame": {"mode": "rows",
                    "start": {"bound": "unbounded_preceding"},
                    "end": {"bound": "current_row"}}},
 "as": "running_total"}
```

| Part | What it is |
| --- | --- |
| `fn` | `sum`/`avg`/`min`/`max`/`count`, `row_number`/`rank`/`dense_rank`/`ntile`, `lag`/`lead`/`first_value`/`last_value` |
| `arg` | The value to read — any item-100 `Expression`, so `SUM(qty * price) OVER (…)` works. Omitted for the ranking functions and for `COUNT(*) OVER` |
| `over` | The `OVER (…)` clause: `partition_by`, `order_by`, `frame`. **Required** — `{}` means `OVER ()` |
| `offset` / `buckets` | `lag`/`lead` row distance; `ntile` bucket count |
| `as` | Required output name |

`over` being required is not ceremony: `{"fn": "sum", "arg": …}` is the plain
aggregate and `{"fn": "sum", "arg": …, "over": {…}}` is the window, and the two
shapes are otherwise identical — the `over` key is what makes the select-item
union unambiguous instead of guessing.

**What it deliberately does not do**, each recorded in the
[Decision Log](#decision-log):

- **No frame is invented.** Omit `frame` and no `ROWS`/`RANGE` clause is emitted,
  so you get the dialect's SQL-standard default (identical on Postgres and
  MSSQL). A **numeric `RANGE` offset is rejected on MSSQL** — T-SQL's `RANGE`
  takes only unbounded/current-row bounds — pointing you at `mode: "rows"`,
  rather than being quietly rewritten into `ROWS`, which treats ties differently.
- **A window can't be combined with `group_by` or aggregate select items.** A
  window over *aggregated* rows needs the aggregate materialized as a derived
  table (item 105); until then, use the two-query recipe below.
- **A window isn't an expression operand**, so `amount / SUM(amount) OVER ()` is
  two projected columns plus client-side division, not one expression — recipe
  below.
- **Aggregate windows are refused when `min_group_size` (k-anonymity) is set** —
  a window aggregate has no result group for the floor to filter, so it would be
  a way around it. Ranking and offset windows stay available.

**The two composition recipes**, written out rather than left implicit — this is
the "expose primitives, don't spoon-feed the agent" doctrine in practice: both
are things a human SQL author would compose from what's already here.

*1 — a moving average over **daily** (aggregated) values.* Two round trips: first
the daily aggregate, then the window over what came back. The agent already holds
the intermediate result, so the second query is over its own data, not the
database's:

```json
{"from": "orders",
 "select": [{"col": "orders.created_at", "granularity": "day", "as": "day"},
            {"fn": "count", "col": "*", "as": "orders_that_day"}],
 "group_by": ["day"], "order_by": [{"col": "day"}]}
```

…then compute the 7-point rolling mean over `orders_that_day` client-side (or, if
the smoothing must happen in the database, wait for item 105's derived table,
which lets the aggregate become the `FROM` of a windowed query in one statement).

*2 — each row's percent of total.* Project the row value and the window total as
two columns and divide in the caller — here each line item's share of its order,
which is also the two engine pillars composing (an item-100 expression inside an
item-101 window):

```json
{"from": "order_items",
 "select": ["order_items.id",
            "order_items.order_id",
            {"expr": {"op": "*",
                      "left": {"col": "order_items.quantity"},
                      "right": {"col": "order_items.unit_price"}},
             "as": "line_total"},
            {"fn": "sum",
             "arg": {"op": "*",
                     "left": {"col": "order_items.quantity"},
                     "right": {"col": "order_items.unit_price"}},
             "over": {"partition_by": ["order_items.order_id"]},
             "as": "order_total"}],
 "order_by": [{"col": "order_items.id"}]}
```

Every row carries its order's total, so `line_total / order_total` is one
division in the agent; drop `partition_by` for a share of the whole result set.
(Both recipes are executed end-to-end by
`tests/integration/test_window_end_to_end.py`, and both stay inside the shipped
demo policy — note that a *masked* column such as the demo's `orders.total_amount`
cannot be a window argument at all, since that is a non-projection use.) What
QueryGate deliberately does *not* do is let the division happen inside the AST by
making a window an expression operand — see the [Decision Log](#decision-log) for
why that trade is a maintainer call rather than a convenience we take.

**Bounds:** `max_window_specs` (how many windows, summed across the query and
its subqueries; `0` turns the feature off per connection) and
`max_window_frame_offset` (how far a frame or a `lag`/`lead` may reach). **Both
defaults were chosen against a measurement, not a guess** —
`tests/integration/test_postgres_window_cost.py` re-runs it on the live-Postgres
suite against the 25,000-row table the large-domain stress suite seeds (it skips
with the seeding command when that data is absent). Five windows costs ~15x a
plain scan and the marginal cost per window rises past that, which is where the
cap sits — the rise is the argument for capping at all; 5 itself is a judgement
inside the measured range.

The frame cap is the load-bearing one. For an aggregate the engine can't compute
with inverse transitions, the work is O(rows × frame) — 12 ms at 10 preceding,
574 ms at 1,000, **3.4 s at 10,000** over 25k rows. Three qualifications that
matter for tuning it:

- **Which aggregates rescan** is narrower than "`MIN`/`MAX`": Postgres has an
  inverse transition for `SUM`/`AVG` over integer/numeric/money/interval only —
  over `real`/`double precision` they rescan like `MIN`/`MAX`, so a **float**
  metric column pays the full cost.
- **Those are unlimited-scan numbers — and the row limit does not save you.** A
  top-level read is LIMIT-clamped (default 50, max 100), which bounds the rescan
  *only* while the query's `order_by` is already satisfied by the window's own
  ordering. Order by anything else and the plan becomes `Limit → Sort →
  WindowAgg`, and that blocking sort runs the window over every row anyway:
  measured **0.7 ms aligned vs 583 ms with an unrelated `ORDER BY`**, both at
  `LIMIT 50`. So the full cost is reachable at the top level under default
  policy — and always inside an `IN (subquery)`, where the LIMIT is stripped.
- **Postgres does not price the frame at all**, reporting an identical `EXPLAIN`
  cost for a 10-row and a 10,000-row frame, so the cost-estimation gate is
  structurally blind to it there and the cap is the only guardrail that sees it,
  with `timeout_seconds` as the backstop. MSSQL's estimator is unmeasured for
  this.

`PARTITION BY` columns count against the same `max_partition_by` budget `top_n`
uses. Every column a window touches — in `arg`, `partition_by`, **and**
`order_by` — goes through the item-96 canonical visitor, so a denied column is
rejected and a masked one can't be ordered or partitioned by (which would leak
its value by inference).

**Proven on both real backends, not on rendered SQL:** every one of the thirteen
window functions and each frame form executes against a live Postgres *and* a
live SQL Server in `tests/integration/test_cross_dialect_differential.py`, with
the returned rows asserted equal — and a test enforces that a new window function
cannot ship without such a case. That is the items 75/82 lesson applied: `RANGE 2
PRECEDING` compiles cleanly for the MSSQL dialect and then fails on the server.

### Dates and relative time: "the last 30 days" without doing the arithmetic

**Files:** `src/querygate/query_ast/models.py` (`ExtractExpr`, `NowExpr`,
`DateAddExpr`), `src/querygate/compiler/dialect_adapters.py` (`extract_part`,
`current_timestamp`, `date_add`), `src/querygate/connections/dialects.py` (the
UTC session pin).
**Item:** TODO.md 102 — Phase 3a of the
[Expressive Query Engine plan](ENGINE_EXPRESSIVENESS_PLAN.md).

Three nodes join item 100's `Expression` union, so each is legal anywhere a
scalar is — a projection, an aggregate argument, either side of a predicate,
inside a `CASE`:

| Node | Wire form | What it is |
| --- | --- | --- |
| `extract` | `{"extract": {"col": "orders.created_at"}, "part": "hour"}` | One integer field of a date/timestamp |
| `now` | `{"now": "timestamp"}` or `{"now": "date"}` | The current UTC instant, or midnight UTC today |
| `date_add` | `{"date_add": {"now": "timestamp"}, "unit": "day", "amount": -7}` | Shift a date/timestamp; negative goes back |

`part` is one of `year`, `quarter`, `month`, `week`, `day`, `dayofweek`,
`dayofyear`, `hour`, `minute`, `second`. `unit` is the seven you can actually
shift by — `year`, `month`, `week`, `day`, `hour`, `minute`, `second` — the three
others (`quarter`, `dayofweek`, `dayofyear`) being *derived* fields you can read
but not add. Both are closed enums: there is no free-string date format, no
time-zone name a caller can pass, and no way to name a function.

So "orders in the last 7 days" stops being something the caller computes:

```json
{"from": "orders",
 "select": [{"fn": "count", "col": "orders.id", "as": "n"}],
 "where": {"col": "orders.created_at", "op": "gte",
           "value_expr": {"date_add": {"now": "timestamp"},
                          "unit": "day", "amount": -7}}}
```

**Honest framing of what this unlocks.** Relative-date filtering was *always*
expressible — the agent computed the cutoff and passed a timestamp literal. This
is native convenience, not a removed wall, and it was prioritized accordingly
(row 12 of the plan's regression bar was 🟡, not ❌). What it genuinely fixes is
correctness rather than reach: a caller-computed cutoff is computed in the
*caller's* clock, and every agent had to get the timezone right on its own.

**Everything here is UTC, and the Postgres session is pinned to make that true.**
This is the part worth reading even if you never use these nodes. Postgres
resolves `EXTRACT`, `date_trunc` and every `timestamp`↔`timestamptz` conversion
against the session `TimeZone`; QueryGate never set one, so those answers
followed whatever zone the server happened to be configured for. Sessions now
issue `SET LOCAL TIME ZONE 'UTC'` alongside the existing lock and statement
timeouts. MSSQL has no session zone, so its adapter reads the clock with
`SYSUTCDATETIME()`; SQLite's `'now'` is already UTC.

**This changes existing behavior, and exactly who is affected is worth being
precise about** (it was measured, not assumed):

- A **`timestamptz`** column on a Postgres server whose zone is not UTC: its
  `date_bucket` values and extracted fields **change**. Previously they followed
  the server's zone; now they are UTC.
- A naive **`timestamp`** column: **nothing changes.** Postgres never consulted
  the session zone for those, so `EXTRACT(hour …)` and `date_trunc` returned the
  stored wall-clock value before and still do.
- Any comparison between `now()` and a naive `timestamp` column: **changes on
  every non-UTC deployment**, because that conversion always went through the
  session zone. This is the case the pin exists for.

Write filters are affected identically — the session guardrails run on the write
path too, so a write's `WHERE` selects rows under the same UTC semantics, and
preview and execute stay mutually consistent. Columns are never converted:
QueryGate cannot know what a naive `timestamp` column means, so it reads it as
stored. See the [Decision Log](#decision-log).

**Two parts mean the same thing on every dialect, by definition rather than by
luck.** `dayofweek` is `0`=Sunday..`6`=Saturday, and `week` is the ISO-8601 week
number. T-SQL's natural spellings disagree with both — `DATEPART(weekday)` is
1-based *and* shifts with the server's `SET DATEFIRST`, and its plain `week` is a
different count from ISO — so the MSSQL adapter renders the DATEFIRST-independent
idiom and `iso_week`. Where a dialect genuinely lacks the capability it still
**rejects** rather than approximating: the internal SQLite path has no ISO-week
function, so `extract(week)` raises and points at `date_bucket`'s `week`
granularity.

**A date primitive requires a date column.** `extract`, `date_add` **and**
`date_bucket` over a column that is not a date/time type are all rejected before
the database is touched (items 102 and 117; the third was added once measurement
showed no caller could have correct behavior to lose). That is not
pedantry: with an INTEGER operand, Postgres *errors* while SQL Server silently
returns `0` (and `1900-01-03` for a shift), because T-SQL implicitly converts an
int to a datetime counted from 1900-01-01 — the same query, a hard failure on one
backend and a plausible wrong answer on the other. Only a **bare column** operand
is checked, since that is the only case with a known type; an explicit
`{"cast": …, "to": "timestamp"}` is the documented way through, and the rejection
message says so.

**Bounds:** `max_interval_days` caps how far one `date_add` may shift (default
3,660 = 10 x 366, i.e. ten years counted the way the cap counts a year; `0` allows
only a no-op shift). It is computed from the amount
with **upper-bound** unit lengths — a year counts as 366 days, a month as 31 — so
a bigger unit can't launder a bigger reach past the cap. It is deliberately *not*
a row-count guardrail: a caller who wants everything omits the filter, which
`max_limit` and the mandatory row filters bound. What it prevents is a
caller-triggerable server error — a 10,000-year *lookback* overflows T-SQL's
datetime range and makes Postgres raise "timestamp out of range" — and an
unbounded lookback masquerading as a filter. The nodes
also count toward `max_expression_depth`/`max_expression_nodes` like any other
expression, and every column inside them goes through the item-96 canonical
visitor, so a denied column can't hide in an `extract` and a masked one can't be
shifted by a `date_add`.

To **group by** an extracted field, project it with an alias and group on the
alias — the same route `date_bucket` has always used:

```json
{"from": "orders",
 "select": [{"expr": {"extract": {"col": "orders.created_at"}, "part": "month"},
             "as": "mth"},
            {"fn": "count", "col": "orders.id", "as": "n"}],
 "group_by": ["mth"]}
```

**Proven on both real backends:** every date part and every interval unit
executes against a live Postgres *and* a live SQL Server in
`tests/integration/test_cross_dialect_differential.py`, with values compared to
ground truth computed in Python — not to each other, so two identically-wrong
adapters cannot agree. The corpus deliberately includes sharper
discriminators than the demo data: a year boundary (where T-SQL's non-ISO `week`
returns 53 against the ISO week's 1) and fractional seconds (where an unfloored
Postgres `EXTRACT(second …)` returns 60 — a value no clock produces). The SQL Server runs on a **non-UTC clock** —
`TZ` is set on the compose service and on both CI service blocks — so
`SYSUTCDATETIME()` versus `GETDATE()` is a real assertion rather than a vacuous
one, and the test *skips loudly* rather than passing if it ever finds itself
against a UTC-clocked server. A test makes a live case mandatory for each part, and
lives in the unit tier so it fires outside the two-database job. The UTC pin has its own live proof in
`tests/integration/test_postgres_date_primitives.py`, which sets the server's
zone to UTC−09:30 and asserts the extracted hour is still UTC.

#### Joins: equality sugar, a general `ON` condition, and the four join types

A join is written one of two ways (item 103). `on` is the equality sugar and the
common case — `"on": ["orders.customer_id", "customers.id"]`, with `extra_on`
adding further ANDed pairs for a composite key. `condition` is the general form:
the **same predicate tree** `where` uses, which is what makes a range, temporal or
inequality join expressible at all.

```json
{"from": "products", "from_alias": "p",
 "select": ["p.name", "band.label"],
 "joins": [{"table": "price_bands", "alias": "band",
            "condition": {"and": [
               {"col": "p.price", "op": "gte", "value_col": "band.lo"},
               {"col": "p.price", "op": "lte", "value_col": "band.hi"}]}}]}
```

The two forms are **mutually exclusive** — they are two spellings of one clause,
so accepting both would leave their precedence for the compiler to invent.

`type` is `inner` (default), `left`, `full` or `cross`. A `cross` join takes no
condition at all and is **off by default**: `Policy.allow_cross_join` must be
turned on for the connection. It is the only join whose cost is the *product* of
its inputs, and the only one exempt from the rule below — before item 103 a
cartesian product was structurally inexpressible, so it now has to be asked for
explicitly. It still counts against `max_joins`.

**Which tables a condition may name.** It must reference the table being joined,
must connect to something already in the query graph, and must not
forward-reference a table joined *later*. A condition naming a third,
already-joined table is fine (`JOIN c ON c.x = a.x AND c.y = b.y`) — `extra_on`'s
"same two tables" restriction is a property of that sugar, not of a join.

**The `ON` clause is capped exactly like `WHERE`.** It is the fourth position a
predicate tree can occupy (after `where`, `having` and a searched-`CASE`
condition), and it inherits `max_where_depth`, `max_where_predicates` (summed
tree-wide), `max_in_list_size` and the expression caps — plus the denied-column,
masked-column and date-operand rules, which apply by construction because the
condition's refs flow through the same canonical visitor everything else uses.
`IN (subquery)` is **rejected** in a join condition, as it is in `HAVING` and
`CASE`.

**One cross-dialect caveat worth knowing.** An outer join can produce NULLs in the
column you `order_by`, and Postgres sorts those NULLs **last** on ASC while SQL
Server sorts them **first** — same rows, different order. This predates item 103
(a plain `LEFT JOIN` behaves identically) and is not papered over, because the
only in-engine fix is `OrderBySpec.nulls`, which QueryGate deliberately rejects on
MSSQL rather than emulating (item 74). Order by a non-nullable column when you
need a deterministic order across both backends.

**Proven on both real backends:** every join type, the range-join shape and the
cross join's cartesian count all execute against a live Postgres *and* a live SQL
Server in `tests/integration/test_cross_dialect_differential.py`, and a test makes
a live both-dialects case mandatory for any future join type.

### Set operations: UNION, INTERSECT and EXCEPT in one statement

**Shipped by TODO.md item 104.** Combining result sets server-side — "customers
who are high-value **or** dormant", "IDs in both lists", "in this list but not
that one" — is a `set_op` on the query:

```json
{"from": "customers", "select": ["customers.name"],
 "where": {"col": "customers.lifetime_value", "op": "gt", "value": 10000},
 "set_op": {"op": "union", "arms": [
     {"from": "customers", "select": ["customers.name"],
      "where": {"col": "customers.last_order_at", "op": "lt", "value": "2025-01-01"}}]},
 "order_by": [{"col": "name"}], "limit": 100}
```

**The query that carries `set_op` is the first arm.** Its `from`/`joins`/`where`/
`group_by`/`having` describe arm 1; its `order_by`/`limit`/`offset` apply to the
*combined* result. That mirrors SQL exactly — `SELECT … WHERE … UNION SELECT …
ORDER BY … LIMIT …` binds the `WHERE` to one arm and the `ORDER BY`/`LIMIT` to the
statement — so an arm may not set `order_by`, `limit`, `offset` or `top_n`, and a
`top_n` cannot be combined with a set operation at all. Every arm must project the
same number of columns with **compatible types** at each position, and arms don't
nest: a set operation is one flat `arms` list.

The type rule exists because the backends disagree: an integer column unioned with
a text cast of it is a hard error on Postgres and *succeeds* on SQL Server, which
converts one side and returns rows. QueryGate refuses it on both. The check is
deliberately coarse — integer and numeric are compatible, so are date and
timestamp — and a value whose type isn't statically knowable (arithmetic, a
function call, a `CASE`) is left to the database rather than guessed at.

`op` is `union`, `intersect` or `except`; `all: true` keeps duplicates. `UNION ALL`
works everywhere, but **`INTERSECT ALL` and `EXCEPT ALL` are Postgres-only** —
T-SQL has neither, so QueryGate rejects them there with a typed error rather than
silently dropping the flag and returning fewer rows than you asked for.

**Every arm is a full, independently governed scope.** This is the part that
matters for safety rather than reach: each arm is validated on its own — its own
table/column allow-deny, its own masked-column rule, its own schema resolution
against its own tables — and each arm is *compiled* on its own, so it carries its
own mandatory row filters and its own `min_group_size` floor. **A set operation is
not a channel to dodge a per-table filter.** An arm also cannot reference another
arm's table: arms are siblings, not a correlated scope.

**Bounds:** `max_set_op_arms` (default 3, counting arm 1, summed across the query
and its subqueries; `0` turns the feature off per connection). Every *other* cap —
`max_joins`, `max_select_columns`, `max_where_predicates`, `max_expression_nodes` —
is summed across the arms, so arms share one budget rather than each getting their
own. The higher `max_limit_aggregate` ceiling applies only when **every** arm
aggregates; one raw-row arm means the response contains raw rows and gets
`max_limit`.

**Proven on both real backends:** all three operators, `UNION ALL`'s duplicate
retention, per-arm aggregation, the enforced row limit, and a mandatory row filter
reaching *every* arm all execute against a live Postgres *and* a live SQL Server in
`tests/integration/test_cross_dialect_differential.py`. (The `INTERSECT ALL` /
`EXCEPT ALL` split is proven there too, but asymmetrically and deliberately: the
Postgres half executes, while the MSSQL half is a client-side rejection raised
before any SQL is sent — the `array_agg` posture, where the dialect gap is a
documented fact rather than something we make a server prove.) That last one is
there for a specific reason: SQLAlchemy's MSSQL dialect **silently drops** a
`LIMIT` applied to a compound `SELECT`, which would have made a policy guardrail a
no-op on one dialect only, so QueryGate wraps the compound in a derived table
before limiting it.

### Multi-stage analysis in one statement: named `WITH` blocks (`ctes`)

Some questions need two stages: aggregate first, then join the result; or define a
cohort, then measure it. Before item 105 that meant two round-trips and stitching
the results together in the agent. Now the first stage is a **named block** the
rest of the query refers to by name:

```json
{
  "ctes": [
    {"name": "totals",
     "query": {"from": "orders",
               "select": ["orders.customer_id",
                          {"fn": "sum", "col": "orders.total_amount", "as": "total"}],
               "group_by": ["orders.customer_id"]}}
  ],
  "from": "customers",
  "joins": [{"table": "totals", "on": ["customers.id", "totals.customer_id"]}],
  "select": ["customers.name", "totals.total"]
}
```

A block is referenced exactly like a table — put its `name` in `from` or a join's
`table`. Because it is referenced by name rather than inlined, **one block can feed
the FROM clause and several joins** without being written, or planned, more than
once. Only the top-level query declares blocks (not a `set_op` arm, an
`IN (subquery)`, or another block), and a block may reference only an **earlier**
block — which is also why a **recursive CTE cannot be expressed**: it isn't
forbidden by a special rule, it simply has no way to name itself. That is
deliberate; unbounded recursion is a real denial-of-service surface, and admitting
it would need a hard iteration cap designed and recorded first.

**A block is a full scope, not a shortcut past anything.** Its tables get the same
allow/deny, the same mandatory row filters, the same column masking and the same
`min_group_size` floor as any other query, because every block compiles through the
identical code path a top-level query does. Two consequences are worth stating
outright:

- **A masked column may not be projected by a block.** Inside the block it looks
  like a bare projection, but the block's rows are an *input* to another scope,
  where the value could be filtered, joined or ordered on — every position the
  masking rule exists to keep an unmasked value out of.
- **Joining onto a block is refused while `min_group_size` is set.** A computed
  stage carries no uniqueness guarantee, so it may match many rows per key and
  inflate the count the k-anonymity floor checks. Refusing is the same posture item
  118 took for any other fan-out join: when the floor cannot be enforced correctly,
  the query fails rather than returning an answer the policy believes is protected.

**Bounds:** `max_cte_count` (default 3; `0` turns the feature off per connection)
and `max_subquery_depth`, charged along the **reference chain** — two independent
blocks each cost 1, while a block reading another block costs 2, so the default
denies a chain until an operator raises the cap. Every other cap already counts
block bodies, so blocks share one budget rather than each getting a fresh one.

**A block is deliberately *not* row-capped.** `max_rows` bounds the response, and a
block's rows are intermediate work feeding a join or an aggregate — clamping it
would silently truncate the population a total is computed over, which is a wrong
answer rather than a refusal. An explicit `limit` inside a block is honoured (and
clamped), because that is the caller asking for "the top 100". Stated plainly, as
the cross-join gate had to be: what bounds a block is `timeout_seconds`,
`max_response_bytes`, the concurrency limiter and `max_cte_count` — not a row cap.

**Naming:** a block may not be named after a table this connection's policy has a
rule for. A block name wins name resolution, so calling one `orders` would make
every rule the operator wrote about the *table* `orders` unreachable in that query.
A block's own projections must also have **distinct output names** — its columns
are referred to by name, so `select: ["orders.id", "customers.id"]` would leave
`block.id` meaningless; alias one of them with `as` and the meaning is explicit.

**A block stays single-connection**, the same restriction a nested `IN (subquery)`
has carried since item 97: a block's own joins may not reach a second connection,
even one in the same `join_group`. Run a query per connection and combine the
results. (The outer query's joins are unaffected — cross-connection joins there
work exactly as before.)

**Proven on both real backends:** the aggregate-then-join shape, a two-stage chain,
the absence of the row cap (a grand total through a block under a row cap of 1 must
still equal the direct total), and a 7-day moving average over daily order counts
all execute against a live Postgres *and* a live SQL Server in
`tests/integration/test_cross_dialect_differential.py`.

### Asking about rows that relate to other rows: EXISTS and scalar subqueries

Two shapes shipped with item 106, and between them they close the last expressible
gap in the read engine.

**`EXISTS` / `NOT EXISTS`** tests whether a related row exists, without joining:

```json
{"from": "customers", "select": ["customers.name"],
 "where": {"op": "not_exists",
           "exists_subquery": {"from": "orders", "select": ["orders.id"],
                               "correlate": ["customers.id"],
                               "where": {"col": "orders.customer_id", "op": "eq",
                                         "value_col": "customers.id"}}}}
```

Note `correlate`. **QueryGate does not give a subquery ambient access to the
enclosing query's columns the way SQL does.** Every outer column a subquery may read
must be named in its own `correlate` list, and each named column is then checked
against the *enclosing* query's policy — table allow-deny, column allow-deny, and the
masked-column rule. An outer column that is *not* declared is rejected, exactly as it
was before this feature existed. Correlation is therefore opt-in per subquery, and
the safe default is unchanged. It reaches exactly one level, to the immediately
enclosing query. `Policy.max_correlated_refs` (default 2) caps how many columns may
be pulled in; `0` disables correlation entirely.

**A scalar subquery** compares a value against an aggregate computed by another
query — "orders above the overall average", in one statement instead of two:

```json
{"from": "orders", "select": ["orders.id"],
 "where": {"col": "orders.total_amount", "op": "gt",
           "value_subquery": {"from": "orders",
                              "select": [{"fn": "avg", "col": "orders.total_amount",
                                          "as": "a"}]}}}
```

A scalar subquery **must be an aggregate with no `group_by`**. That is not a
restriction for its own sake: a comparison needs exactly one row, and this shape
guarantees it before the query reaches the database, on every backend. The
alternative — quietly taking the first row of many — would return a wrong answer
with no error, which this project treats as worse than a refusal. For a multi-row
value set, use `in`/`not_in`, which is unchanged.

Scalar subqueries also work in `HAVING` (comparing one aggregate to another).
`EXISTS` does not: it asks a per-row question, which has no meaning after grouping.
Neither is permitted in a join condition or a `CASE` condition, where it would reach
the compiler without having been checked as a scope of its own.

**Cost, stated plainly:** a correlated subquery is re-evaluated per candidate outer
row. `max_correlated_refs` bounds the *surface* — how many outer columns a nested
scope can see, which is the part that must not grow silently — not the work. What
bounds the work is `timeout_seconds`, the concurrency limiter, and the (opt-in)
query-cost gate.

**What bounds a write's `WHERE`.** Two things beyond the write policy itself, both
easy to miss because they live on the *read* side of `Policy`: the filter's columns
are checked against read allow/deny **and** the masked-column rule (a masked column
cannot be used to target a mutation at all), and since item 116 the filter's *shape*
is bounded by `max_where_depth`, `max_where_predicates` and `max_in_list_size` — the
same caps and the same numbers a read gets, because a write's `WHERE` reads rows in
order to select them. So a `DELETE` filtering on a 5,000-element `IN` list needs a
policy change, exactly as the equivalent `SELECT` would. Since item 114 the filter
is also its own narrowed type (`WritePredicate`/`WriteWhereGroup`): no computed
expressions and no subqueries, refused by the schema rather than at runtime.

### 4. Concurrency control — don't overwhelm the database

**File:** `src/querygate/execution/concurrency.py`

Before actually running the compiled query, QueryGate makes the caller wait
for a "slot." Every read against a given connection — a structured query, one
item in a batch, even a schema lookup — draws from the same per-connection
concurrency budget, so nothing bypasses the limit by going through a
different code path.

The default mechanism is a **semaphore** — a classic concurrency primitive
that only lets N callers hold a resource at once; anyone past that count
waits (up to a configurable timeout) or is rejected. QueryGate's default is
an in-process `asyncio.Semaphore` per connection id, which is correct for a
single running instance but — as the module's own docstring flags — would
silently multiply if QueryGate is deployed behind a load balancer with
several instances (each instance would enforce its own separate limit,
adding up to more total concurrency than intended). For that case, a
Redis-backed distributed limiter (`redis_concurrency.py`) can be configured
instead, sharing one real limit across every instance.

This stage also enforces `max_queue_depth` / `max_queue_depth_per_principal`
— caps on how many callers may be *waiting* for a slot, not just how many can
run at once — so an unbounded wait queue can't itself become a way to exhaust
server resources.

**Why it runs after compiling, right before execution:** there's no point
queuing for a database slot before QueryGate even knows the query is valid
SQL worth running.

A related but distinct control (`execution/quota.py`) sits *earlier*, before
the caller even queues: a **per-principal rate/byte quota** over a rolling
time window. Concurrency caps bound how many queries run *at once*; the quota
bounds how many (and how large a total response) one authenticated caller may
run *over time* — so an agent that stays under its concurrency limit still
can't fire unbounded sequential queries and run up a database's load or a
customer's cost budget. It's checked before queuing so a throttled caller
never even consumes a slot, and a rejection comes back as a distinct 429 /
`RATE_LIMITED` with a retry hint, not a generic denial. See the Decision Log
entry on rate limits for the deliberate scope choices (in-process phase 1,
counts admitted attempts, skips anonymous callers).

### 5. Session lifecycle and dialect guardrails

**Files:** `src/querygate/connections/engine.py`,
`src/querygate/connections/dialects.py`

With a concurrency slot secured, QueryGate opens an actual database session
and runs the query. Two things happen here that are easy to take for
granted but matter a lot in practice:

- **Session lifecycle.** `engine.py` keeps one SQLAlchemy engine (connection
  pool) per connection id, created lazily on first use, and every query runs
  inside `session_scope()` — a context manager that begins a transaction,
  guarantees the session is rolled back on any error, and always closes the
  session afterward. No caller code has to remember to clean up a connection.
- **Dialect-specific safety settings**, applied at the start of every
  session, isolated to `dialects.py`'s `apply_session_guardrails` — the same
  "keep dialect logic in one place" principle as the compiler's date
  bucketing:
  - **Postgres:** `SET LOCAL lock_timeout` and `SET LOCAL statement_timeout`
    — if this query gets stuck waiting on a lock, or just runs too long, the
    database itself cuts it off rather than tying up a connection
    indefinitely.
  - **MSSQL:** `SET LOCK_TIMEOUT` (milliseconds) plus `SET XACT_ABORT ON`
    (abort the whole transaction on error, rather than leaving it in a
    half-committed state) and `SET DEADLOCK_PRIORITY LOW` (if MSSQL has to
    pick a victim to kill in a deadlock, prefer killing this query over
    someone else's).

Both settings exist to prevent one slow or stuck agent-issued query from
degrading the shared database for every other caller.

### 6. Audit logging — every attempt, always

**Files:** `src/querygate/audit/logger.py`, `src/querygate/audit/sinks.py`

Whether the query succeeded or was rejected at any earlier stage, QueryGate
logs the attempt. `audit_query()` builds a versioned `AuditEvent`
(`audit/events.py`) and writes it through a pluggable **sink** (`sinks.py`) —
currently `NullAuditSink` (discard), `JsonlAuditSink`, which appends one JSON
object per line to a file with restrictive permissions (`0o600`), designed so
log rotation (renaming the file) works without needing to signal the running
process, or `HashChainedAuditSink` (the tamper-evident ledger described below).

What makes this event safe to retain and share is what it deliberately
*excludes*. Per the model's own docstring, an `AuditEvent` has no field for
SQL text, predicate/parameter values, result rows, connection strings, or
free-form exception text — because any of those can carry customer data. What
it *does* keep is a normalized `query_shape` (which tables/columns/operators
were involved, with literal values stripped out), timing, row count, byte
size, and the outcome — enough to investigate "what happened" without the
audit trail itself becoming something that leaks the data QueryGate exists to
protect. A separate write failure (e.g. the sink's disk is full) is logged
loudly as its own error but never turns an already-successful query into a
failed response to the caller — the database work already happened; the
audit write is best-effort on top of it.

**Delegated identity — "Agent A, on behalf of User Z."** When an agent calls
QueryGate *on behalf of* a human — the two-identity model the industry is
standardizing (RFC 8693 token exchange; the MCP `2026-07-28` authorization
revision) — the audit event records **both** identities: `principal_id` is the
human on whose behalf the action ran, and `actor_id` (plus the full
`delegation_chain` for multi-hop delegation) is the agent that ran it. This is
the artifact an auditor actually asks for: not "some service read this table"
but "*this named human's* access was exercised *by this agent* under *this
human's* policy." Crucially, the identity carried into policy enforcement is the
**human's** — QueryGate applies the human's per-principal policy and
`mandatory_row_filters`, so an agent can never see more than the person it acts
for. Both fields are identities, never credentials, so the redaction guarantee
above is unchanged. See the [Decision Log](#decision-log) for why the human maps
to `subject` (and why that made policy enforcement free).

**Tamper-evident ledger + per-query receipts (opt-in).** The delegated-identity
audit answers *who did what*; a tamper-evident ledger answers *and the record
proving it hasn't been edited*. Set `AUDIT_SINK_BACKEND=jsonl_chained`
(`audit/ledger.py`, `HashChainedAuditSink`) and every persisted event is wrapped
in a **hash-chain envelope** — a sequence number and the hash of the previous
record — so any later edit, deletion, reordering, or insertion breaks the chain
and is caught by the verify-only tool `querygate-audit verify`. Two honest
integrity levels: set `AUDIT_LEDGER_HMAC_KEY` and the chain is **HMAC-SHA256** —
unforgeable by anyone with write access to the file but not the key; leave it
unset and it is **SHA-256** — still detects corruption/reordering/mid-file
deletion, with full tamper-evidence then resting on externally anchoring the head
hash (`verify --expected-head`, which also catches records dropped from the end).
The chain adds *only* the sequence number and hashes — the embedded event body is
byte-for-byte the same redaction-safe event, so the guarantee above is untouched.
`querygate-audit receipt <event_id>` emits a portable, self-contained
**compliance receipt** proving one query's position in the chain without handing
over the whole ledger — the "prove to your auditor exactly what this agent did,
on whose behalf, under which policy, and that the record is intact" artifact. One
logical writer owns the chain head, so it assumes a single replica (or a
per-replica ledger file); see the [Decision Log](#decision-log) for why chaining
lives at the sink/envelope layer rather than on the event model.

**Compliance-grade WORM retention + managed search (opt-in).** The local
hash-chained ledger proves nobody edited what was kept — it doesn't promise
the file itself survives log rotation, disk loss, or years of retention.
Set `AUDIT_SINK_BACKEND=jsonl_chained_s3_worm` and the same redaction-safe
events are *additionally* archived to S3 Object Lock (`audit/worm_sink.py`),
genuinely undeletable for the configured window under `COMPLIANCE` mode —
this composes with, never replaces, the local chain above, so both
questions ("was it edited?" and "can you still produce it?") stay answered.
`GET /api/v1/admin/observability/worm-search`
(`admin:audit:worm-search` scope — deliberately its own, not implied by
`admin:observability:read`) is the QueryGate-native way to search that
archive: a required, capped time range plus `event_type`/`connection_id`/
`principal_id` filters and cursor-based pagination, so "every query against
`pii_customers` in the last 18 months" is answerable even once the local
file has long since rotated that window out. Every bound (window width,
objects scanned, wall-clock timeout, page size) is enforced server-side —
an over-wide or missing range is rejected outright, a bound hit mid-scan
degrades to a truncated, resumable page rather than an unbounded scan — except
a day listing over `max_objects_scanned`, whose cursor does not advance (item 184).

**MCP as an OAuth 2.0 resource server (opt-in).** For deployments that put the
MCP surface behind a real authorization server, QueryGate can run it as a
conformant OAuth 2.0 *resource server* per the MCP `2026-07-28` spec (off by
default; turn it on with `MCP_OAUTH_RESOURCE_SERVER_ENABLED` once JWT auth is
configured). Three things then hold. First, it **publishes discovery metadata**
(RFC 9728) at `/.well-known/oauth-protected-resource/<mcp path>`, so a client
that gets challenged can find which authorization server issues tokens for this
resource. Second, it **enforces audience binding** (RFC 8707): a token is only
accepted if it was minted *for this resource* (its `aud` names QueryGate's
configured resource identifier), which stops a token issued for some other API
from being replayed against QueryGate — the "confused deputy" attack the spec
exists to close. Third, when a caller shows up with no token, a token bound to
the wrong audience, or a token missing a required scope, QueryGate answers with a
standards-compliant `WWW-Authenticate` challenge (`invalid_token` /
`insufficient_scope`) that points back at the metadata — the signal a
well-behaved client uses to go obtain or *step up* to the right token. Static API
keys remain a valid, separately-configured trust for service-to-service callers
and are not subject to audience binding, but still pass the same scope gate.

### Why this ordering matters

The six stages are ordered from cheapest-and-safest to most-expensive: pure
config comparison (1) → a schema check that costs one cached database round
trip (2) → in-memory query building with no I/O (3) → waiting for a
concurrency slot (4) → an actual database session with real work and locking
(5) → a log write (6, alongside the response). A query that's going to be
rejected — because it's over a cap, touches a denied table, or references a
column that doesn't exist — is rejected before it ever competes for a
concurrency slot or opens a database connection. This keeps rejected traffic
cheap for QueryGate to handle and keeps the concurrency budget in stage 4
reserved for queries that are actually going to run. The one stage that
always happens regardless of where a request failed is the last one: audit
logging wraps the whole pipeline so every attempt — allowed or denied — is
recorded.

### Query templates — a curated entry point, not a second pipeline

Query templates (TODO.md item 48) let an admin publish named, parameterized
`StructuredQuery` skeletons — e.g. `orders_for_customer` with a typed
`customer_id` parameter — that an agent can invoke by id (`GET
/api/v1/query-templates`, `POST /api/v1/query-templates/{id}/run`, or the
`list_query_templates`/`run_query_template` MCP tools) instead of assembling
the whole AST itself. This is deliberately *not* a bypass: a template is a
stored `StructuredQuery`, never raw SQL, and binding one first validates the
supplied parameters against their declared types/bounds, substitutes them as
bound values (not string-spliced SQL), then hands the resulting
`StructuredQuery` to the exact same `StructuredQueryService.execute()`
described above. Every stage — policy caps, table/column allow-deny, schema
existence, compilation, concurrency admission, session guardrails, audit —
runs unchanged. Templated execution is therefore a strict *superset* of
enforcement: it adds parameter validation on top of the normal pipeline and
never removes a check. The audit event records `operation:
"run_query_template"` with the `template_id` and the parameter *names* bound
(`template_param_shape`), never their values. Templates are file-configured
(`TEMPLATES_FILE`) and hot-reloadable like connections/policy/catalog, and a
template referencing a denied table or an over-cap shape is rejected the same
way an ad-hoc query would be — at deploy-time validation and again at run
time.

**What the dry-run validates (and what it deliberately doesn't).** Staging a
template through the config plane runs a fast, database-free check: the YAML
shape, the query skeleton's *structural* validity (dummy-substitute each
`{param}` and validate the result as a real `StructuredQuery`), the target
connection's existence, and each parameter *slot's self-consistency* — a slot
whose `allowed_values` or `default` contradict its declared `type`/bounds (e.g.
`type: integer` with string `allowed_values`, which would deploy but never bind)
is rejected at dry-run, not left to fail confusingly at invocation. What the
dry-run does **not** do is touch the live database, so it does not verify that a
referenced *column or table actually exists* — that stays a run-time check (the
bound query hits live schema validation), consistent with how `explain` and
cost-estimation never open a session. The admin dry-run panel says so, and
surfaces validation failures attributed to their document (`templates.yaml: …`)
in plain language rather than raw validator output.

For column/table existence there is a separate, **explicit on-demand check**
(`POST /admin/config/check-template-schema`, the admin UI's "Check templates
vs. schema" button): it reflects each template's target connection from the
*currently-live* registry, binds the skeleton with dummy values, and runs the
same `validate_schema` the real pipeline uses, reporting per template `ok` /
`issues` (a missing column/table, named) / `connection_unavailable` /
`unreachable`. It is deliberately **best-effort** — a database that can't be
reached yields `unreachable`, never a hard failure — so the fast, offline
dry-run stays decoupled from database availability while authors still get
pre-stage schema feedback on demand. Like `simulate`/`diff` it requires both
config scopes (it reveals live schema detail while resolving caller-supplied
template content).

**Authoring a template is a governed change (item 48 phase 2).** `templates.yaml`
is a fourth governed document in the config-versioning plane (see [the admin
surface](#the-admin-surface-config-as-versioned-history-not-a-live-edited-file)),
right alongside connections/policy/catalog. An admin submits a template change
through the same `/admin/config/*` validate → preview → stage → apply →
rollback flow (and the admin UI's `templates.yaml` editor tab): it is validated
with the rest of the config, staged as an immutable version, and becomes live
only on a separate, separately-authorized apply — never a self-publishing
direct mutation. A version snapshots its own `templates.yaml`, so a rollback
restores the exact template set that was live before. This gives the "these are
the twelve things this agent may ask, and here's who signed off on the change"
story without any new governance machinery — templates simply joined the plane
that already governs every other config document.

**A guided form composes the template, not just raw YAML (item 87).** Authoring
a template used to mean hand-typing it into the `templates.yaml` change-set
`<textarea>` — knowing the exact key names, the parameter-slot schema, and all
the type/bounds/`allowed_values` self-consistency rules. The Templates domain
now carries a structured form (`admin_ui/`): template id, connection,
description, a repeatable parameter-slot builder (name/type/required/list plus
numeric bounds, string length, and allowed values), and the `StructuredQuery`
skeleton. On submit it composes a template, merges it by id into the current
`templates.yaml` draft through two support endpoints
(`POST /admin/ui/templates/parse` and `/templates/render`), and the result
flows through the *same* validate → stage → apply → rollback as before — no new
store, no per-domain publish. The schema authority is the very same
`QueryTemplateFile`/`TemplateParameter` model the loader and dry-run use, so a
bad slot (e.g. a numeric bound on a string type, or a duplicate id) is rejected
with a clear 422 *at compose time* rather than surfacing later at stage. The
form opens on demand from a "+ New template" button, and each template already
in the browse list is clickable to expand a read-only view of its query
skeleton — read from the admin's own `templates.yaml` (the agent-facing
`/query-templates` projection deliberately omits the AST). Raw `templates.yaml`
editing stays available in the change-set editor as the power-user escape
hatch — the form is additive. This mirrors the visual policy
designer, which already composed a validated `Policy` layer into the draft the
same way; item 87 brought that "form instead of YAML" ergonomics to templates,
the one config document that still lacked it.

### Narrowing: from the general AST to a reviewed template set (item 195)

The expressive AST and the curated templates above are two ends of one
spectrum, and item 195 is the path between them.

**`Policy.templates_only`** (off by default) narrows a principal/connection to
templates: an ad-hoc `StructuredQuery` is refused on execute, explain, batch
*and* verdict, and only a template invocation passes. The exemption is the
**server-set** `template_id` — the template route/tool constructs the service
with it, and nothing reads it from a request body, so a caller cannot assert
its own way out. The refusal is raised *before* the structural caps, policy
validation and schema validation, so the refusal is **uniform** — it does not
depend on which rule the query happened to trip, and a narrowed caller cannot
distinguish "you may not do this at all" from "you exceeded max_cte_count".
(This is consistency and least-surprise, not confidentiality: cap values are
already readable by any authenticated caller via `/help/my-access` and MCP
`describe_my_querygate_access`, and schema discovery stays open — so do not
pitch the ordering as hiding the policy.) It governs the READ surface only;
governed writes stay gated independently and deny-by-default by `WritePolicy`,
and schema discovery stays available.

**The observed-shape recorder** answers the question that makes narrowing
practical: *which* templates does this agent actually need? Opt-in
(`OBSERVED_SHAPES_ENABLED`, default false), it records the redaction-safe
**skeleton** of each allowed query — the AST with every caller-supplied *value*
replaced by a typed parameter slot and `intent` dropped. "Value" is the
enumerated set in `_LITERAL_KEYS`: predicate values, expression literals, a
`string_agg` delimiter, date-arithmetic amounts, window offsets/buckets,
percentile fractions, and limit/offset/top-N. What remains is structure —
identifiers, operators, flags, and caller-authored **aliases**, which the
persisted audit event also keeps. So the honest claim is parity with the audit
event (no predicate value, no row, no `intent`, no credential), not "no free
text": an alias is caller-authored text in both surfaces. That is the same shape a
`QueryTemplate` stores, so a recorded shape promotes directly into a draft that
binds back into a real query. Two properties make it safe to run in production:
the walk is generic over the dumped AST rather than an enumeration of node
types (with a reflection test that fails the day a new literal-bearing field
appears), and `skeletonize` refuses at runtime to return a skeleton still
carrying a value. The store is bounded — shapes are caller-authored, so an
unbounded one would be a memory leak an adversarial caller controls.

**Promotion is read-only, and has a UI.** The observability domain of the admin
console carries an **Observed Shapes** panel — recorded shapes most-used first,
with a "Draft template…" dialog that renders the `QueryTemplate` for review.
There is deliberately **no install action anywhere in it**: adding a template
stays a governed config change, so nothing an agent was seen doing can install
its own template. The panel also surfaces both incompleteness counters as a
visible warning, and renders "recording is disabled" differently from
"recording is on and nothing ran" — confusing those two is how an operator
narrows a connection to an empty template set.

`GET /api/v1/admin/observability/observed-shapes`
and its `/{shape_hash}/template-draft` sibling (scope `admin:shapes:read`,
deliberately *not* `admin:observability:read` — a shape names one principal's
exact tables, columns and predicates), plus `querygate-shapes list|draft` at
the CLI, return a draft for review. Neither installs it; adding a template
stays a deliberate edit to `templates.yaml` or a governed config version.

**The operating story this enables**, and the one to use in a security
conversation: run the connection open in staging with recording on; promote the
shapes the agent actually used into templates; flip `templates_only` on; the
agent's entire reachable database surface is now a finite, reviewed, diffable
list — with an audit trail proving nothing else ever ran. Several limits to state
rather than gloss. **Scope depends on the backend, and the report says which
you have.** With `CONCURRENCY_BACKEND=redis` the store is
`shared-durable`: one discovery window across every replica and worker,
surviving restarts. Without it the store is `process-local-volatile` — a
multi-replica deployment sees only what its own process handled, and a restart
or rolling deploy resets the window entirely, so run the window inside one
process lifetime and collect per replica. The distinction matters more than it
looks: narrowing a connection off a per-replica list breaks every shape the
other replicas saw, so an incomplete list is not a smaller version of the right
answer. The list can be
**incomplete in two ways, both counted and surfaced**: `evicted_total` when the
bound is hit, and `skeletonization_failures` when a query the AST accepts has a
value no template slot can express (a dict value, a mixed-type `IN` list) and
is therefore skipped. A third emptiness cause is disclosed separately because it
looks identical to an idle agent: the Redis backend fails **open**, so an
unreachable store returns an empty list — `backend_healthy: false` says so, and
both the CLI and the panel refuse to let that read as "nothing ran". On the
shared backend the bound that actually binds is whichever replica last wrote, so
a fleet whose replicas disagree on `OBSERVED_SHAPES_MAX_ENTRIES` is held to the
smallest value; the report shows that stored bound, not the reading process's
config — narrowing on a list carrying either number without
raising the bound or handling the failures will break the queries it omitted.
And a rejected ad-hoc query still consumes
quota, since quota is reserved earlier in `execute()` than validation runs.

### Governed Writes (the write pipeline)

Governed writes (TODO.md item 93) extend the same spine to **mutations** —
INSERT / UPDATE / DELETE / UPSERT — without ever crossing the core invariant: a
write is a **typed AST**, never a raw-DML string. (UPSERT compiles to native
`ON CONFLICT DO UPDATE` on Postgres/SQLite and is rejected on MSSQL, which has no
such clause — reject-not-emulate.) `write_ast/` mirrors `query_ast/`
(`InsertStatement`/`UpdateStatement`/`DeleteStatement`, discriminated on `op`),
an UPDATE/DELETE **structurally requires** a WHERE (an unqualified one cannot be
expressed), and set-values and predicates reuse the read AST — so an injected
value can only ever land in a bound parameter. A write flows through the write
siblings of every read stage: `validate_write_policy` → `validate_write_schema` →
`compile_write` (SQLAlchemy Core `insert()/update()/delete()`) → execute → audit.

- **WritePolicy is deny-by-default.** Writes are off unless enabled, and then only
  for allowed tables/operations, with a `max_affected_rows` cap and per-column
  write allow/deny (separate from read allow/deny). A default deployment cannot
  write at all.
- **Preview, and the old→new diff.** `POST /{connection}/write/preview` validates,
  compiles, and reports the affected-row count + parameterized SQL, all without
  mutating. `?include_diff=true` adds the killer feature: the bounded, old→new row
  diff of exactly what would change — computed by running the DML in a
  **rolled-back** transaction. The diff is the one write surface that returns row
  values, so it is bounded (`max_diff_rows`), masking-aware (a read-masked column
  is redacted), and never audited.
- **Gated execution.** `POST /{connection}/write/execute` commits in a single
  transaction: it counts matched rows *in that transaction*, aborts before
  mutating if over the cap (and re-checks the statement's own rowcount so a race
  can't over-write), runs the item-92 approval gate on the row count (REST token
  or MCP elicitation), and rolls the whole thing back on any error — never a
  partial write. A constraint/type violation surfaces as a clean typed 4xx, not a
  500 or a raw-driver leak.
- **No undo — by design.** QueryGate deliberately does not snapshot rows to offer
  a rollback: a second copy of the customer's data outside its source of truth is
  exactly the footprint an operational-database gateway should not add, and it
  would place unredacted row values in a second store. The safety model is
  *prevention*, not reversal — the diff preview and the approval gate put a human
  in front of the exact change before it commits. See the Decision Log entry that
  records removing the earlier compensation/undo mechanism.
- **Both transports, no raw field on either.** REST routes above, plus the MCP
  `run_structured_writes` tool (`mode=preview|execute`, batch, `include_diff`,
  in-session elicitation approval). Audit is redaction-safe, dual-identity (item
  90), tamper-evident (item 91): op/table/affected-count only, never a value.

The claim is deliberately **governed** writes — *bounded, previewed, approved,
attributed* — never "safe autonomous writes." What's guaranteed by
construction (no raw DML, every target policy-checked, no unqualified
UPDATE/DELETE, bounded rows) eliminates the catastrophic-shape class; the residual
("did the agent intend *this* change") is what preview + approval make
reviewable and attributable. See the Decision Log for the approval authorization
model and the honest bounded limits.

## Security Model

The [Core Request Pipeline](#the-core-request-pipeline) section explains what
happens to a query on its way through QueryGate — policy checks, schema
checks, compilation, concurrency control, session guardrails, audit logging.
This section is about something different: the structural guarantees that
hold regardless of the pipeline, because they're baked into QueryGate's data
model rather than into a stage that runs and could be skipped, misconfigured,
or worked around.

> **The pitch, in one breath (for a buyer conversation).** The core guarantee
> is *structural*: there's no raw-SQL field anywhere — a caller can only submit
> a validated JSON AST, so injection and denylist arms-races don't apply.
> Credentials never sit on any returned model, and that's asserted against the
> live API schema, not by convention. And none of it is "trust us": every
> guarantee is backed by a deny-by-default CI gate (static analysis, dependency
> audit, SBOM, image and secret scanning, OpenAPI fuzzing, and a 480-test
> adversarial suite), and reviewers get a reproducible packet where each claim
> names the command that reproduces it. The published container image is signed
> (cosign keyless) and carries SLSA build provenance, both consumer-verifiable.
> We're also upfront about the edges — no third-party pentest yet. The whole
> subject is three things: **structural guarantees, continuous and reproducible
> proof, and honesty about the gaps.**

### 1. There is no raw-SQL input, structurally

Every caller — REST or MCP — can only ever submit one shape of thing: a
`StructuredQuery` (`src/querygate/query_ast/models.py`). It's a Pydantic
model, not a string. It has typed fields like `from_table`, `select`,
`joins`, `where`, `group_by`, `having`, `order_by`, `limit`, `top_n` — and
every one of those models sets `extra="forbid"`, so a caller can't smuggle
in an extra field Pydantic doesn't know about. There is no `sql` field, no
`raw_query` field, no "escape hatch" endpoint anywhere in the codebase that
accepts a string and runs it. The model's own docstring says it plainly:
*"This is the ONLY shape an agent can submit — there is no raw-SQL entry
point anywhere in QueryGate."*

This is a different security strategy than the common alternative: accept a
SQL string and try to make it safe (parsing it, denylisting keywords,
running it through a SQL firewall, sanitizing inputs). That approach has a
structural weakness — the set of things you have to detect is unbounded. SQL
has comments, alternate encodings, vendor-specific syntax extensions, and
endless ways to express the same intent; every new dialect quirk or parser
edge case is a new way for a filter to miss something, and the codebase has
to keep growing a denylist to keep up.

QueryGate sidesteps that arms race entirely by never accepting the string in
the first place. A `StructuredQuery` can't contain a semicolon-separated
second statement, a comment that hides part of a clause, or an
encoding trick, because none of those concepts exist in a JSON object with a
fixed set of typed fields — there's no "SQL text" for such a thing to hide
inside. Instead of asking "does this string look dangerous?", QueryGate asks
"is this AST shape one my validators already understand?" — a strictly
smaller, closed question. (Policy validation and schema validation, covered
in the pipeline section, are what answer that question for each field;
this section is about why there's nothing else to check in the first place.)

The `intent` field is worth calling out specifically: it's the one place a
caller can attach a free-text string (a natural-language summary of what
they're trying to do, for audit/debugging). It's explicitly documented as
"logged... never returned to the caller" and — like every other field — it
plays no role in compilation; the compiler never reads it to build SQL. Free
text can travel through QueryGate for logging purposes, but it can never
become part of a query.

### 2. Credential redaction is a tested invariant, not a habit

QueryGate splits what it knows about a connection into two separate models,
in `src/querygate/connections/models.py`:

- **`ConnectionProfile`** — the real thing, used internally. Its
  `connection_string` field carries the actual database credential, already
  resolved from a `${...}` reference in the connections YAML file (see
  secrets handling below).
- **`PublicConnectionInfo`** — the only thing ever returned to a caller over
  REST or MCP. It has exactly four fields: `id`, `dialect`, `enabled`,
  `description`.

The important detail is *how* `PublicConnectionInfo` is credential-free: it's
not that some code path remembers to strip the connection string before
sending a response. The field doesn't exist on the model at all — there's no
way to construct a `PublicConnectionInfo` with a credential in it, because
there's nowhere on the class to put one. A future contributor who wants to
add a new field to a connection-listing response has to consciously add it
to this narrow model; they can't accidentally forward a field from
`ConnectionProfile` they didn't think about, because the two models aren't
related by inheritance — `PublicConnectionInfo.from_profile()` builds one
from the other field-by-field.

`tests/unit/test_credential_redaction.py` backs this with tests that check
the *actual, live* interfaces a caller sees, not just the model definition:

- `test_public_connection_info_has_no_connection_string_field` — checks the
  Pydantic model fields directly.
- `test_connection_profile_validation_error_does_not_leak_secret` — makes
  sure a validation error on a malformed connection profile doesn't echo the
  connection string back in the error message.
- `test_openapi_schema_never_mentions_connection_string` — builds the real
  FastAPI app and inspects its generated OpenAPI schema (the JSON document
  that describes every REST endpoint and response shape) for the string
  `"connection_string"`.
- `test_mcp_tool_schemas_never_mention_connection_string` — does the
  equivalent check against every registered MCP tool's parameter and output
  schema.

`ConnectionProfile` also validates that the declared `dialect` agrees with
the backend actually named in `connection_string`'s URL scheme (TODO.md item
158) — see the 2026-08-07 Decision Log entry for why that validator is
specifically a `field_validator("dialect")`, not a whole-model validator or
a validator scoped to `connection_string` itself: either of the latter two
leaks the credential straight into `pydantic.ValidationError.input_value`.

Why check the live schema instead of just trusting the model split above?
Because a schema is a *derived* artifact — FastAPI and `MCPServer` both
generate it automatically from whatever models are wired into a route or
tool signature. It's possible to define `PublicConnectionInfo` correctly and
still, months later, accidentally wire a new endpoint's response type to
`ConnectionProfile` instead (a typo, a copy-pasted signature, a "just for
now" shortcut). A test that only checks the model definition wouldn't catch
that — the model would still be defined correctly, just not used everywhere
it should be. Asserting against the actual generated schema catches the
"used it in the wrong place" mistake, not just the "defined it wrong"
mistake — which is the class of bug that "we remember not to leak it" can't
protect against, because remembering doesn't scale as new endpoints get
added.

### 3. Secrets: resolved at load time, never stored as YAML text

Connection strings never live in `connections.yaml` as plain text. The file
holds a *reference* — `${QUERYGATE_DEMO_DB_URL}`, or, if HashiCorp Vault is
configured, `${vault:querygate/demo-db#connection_string}` — and
`src/querygate/secrets/resolvers.py` resolves it into the real value when
the connection registry loads (`connections/registry.py`, on both initial
load and authorized hot reload).

Two resolvers exist today, dispatched by the `scheme:` prefix in the
reference (a bare name with no scheme is always treated as an environment
variable, so existing `${VAR}`-style connection files keep working
unchanged):

- **`EnvSecretResolver`** — reads a process environment variable, falling
  back to a local `.env` file if the variable isn't set in the real
  environment (matching how most 12-factor deployments already expect
  config to work).
- **`VaultSecretResolver`** — reads a key from a HashiCorp Vault KV v2
  secret (`path#field` syntax), authenticated with a static Vault token.

Both resolvers follow the same rule as `PublicConnectionInfo` above: errors
are informative without being a leak. If a Vault lookup fails, the raised
error names the secret *path* that was being looked up and the exception
*type* — never Vault's own response text or the token used to authenticate,
because either of those could itself embed something sensitive (a URL with
credentials in it, for instance). `SecretResolverRegistry` is deliberately a
thin, pluggable dispatch table (one `resolve(reference) -> str` method per
backend) so adding a third backend later (AWS or GCP Secrets Manager, say)
means writing one new class, not touching the registry/interpolation call
site.

Because secrets are re-resolved from their reference on every config load —
not cached forever after the first read — rotating a credential (a new
Vault token, an updated environment variable) takes effect on the next
config reload, without restarting QueryGate.

That reload was, until item 135, always operator-pull: something had to call
`POST /admin/reload-config` (or apply a config-governance version) after a
credential rotated. For a short-TTL dynamic secret — the actual value
proposition of Vault's dynamic secrets engines — that gap between "rotated"
and "someone reloads" is a real outage window. `CredentialLeaseMonitor`
(`src/querygate/config_reload.py`) closes it with a separate, optional
`LeasedSecretResolver` protocol (`lease_expiry(reference) ->
Optional[datetime]`) that a resolver backend implements only if it actually
has a lease to report — `SecretResolver` itself is deliberately *not*
widened with this, the same "compose, don't widen" shape
`core/auth.py`'s `CompositeAuthenticator` already uses. `lease_expiry`
deliberately never returns the resolved value itself: for a backend where
"reading is issuing" (a Vault *dynamic* secrets engine, as opposed to the
static KV v2 secrets this module implements today), fetching the value just
to check its expiry would mint and immediately orphan a fresh privileged
credential on every poll — an early draft of this item did exactly that,
caught and fixed by a post-ship security review before it shipped.

A background task (disabled by default; `CREDENTIAL_LEASE_REFRESH_ENABLED=true`)
polls, on each iteration and offloaded via `asyncio.to_thread` (probing a
leased resolver can be a real, slow network call — running it inline would
stall the whole process, not just the poll), every currently-live
connection's `connection_string` field for a `${scheme:reference}` whose
resolver implements `LeasedSecretResolver`, and — when the soonest reported
expiry falls inside `CREDENTIAL_LEASE_REFRESH_MARGIN_SECONDS` and hasn't
already been acted on (hysteresis — otherwise a lease shorter than the
margin, the normal case for a real dynamic credential, would trigger a
reload on every single poll forever) — calls the exact same
`reload_config()` the operator-triggered path already uses, so the same
registry/policy swap and the same `_dispose_stale_engines` in-flight-safe
disposal apply either way, and records an `audit_config_change` event so an
automatic trigger is attributable like any other config change. "Currently
live" is governance-aware: it prefers an active config-governance version's
own files over the plain `connections.yaml`/`policy.yaml` paths, since
governance's `apply()`/rollback never write an approved version's content
back to those plain files — a monitor that ignored this would, the moment a
lease came due, silently revert a four-eyes-approved policy tightening back
to stale disk content. `VaultSecretResolver` implements the protocol by
honestly reporting Vault's own `lease_duration` response field (rather than
inventing a TTL) — since the shipped Vault integration reads KV v2 (static
secrets) only, that value is typically `0` (no lease) today, so in practice
the monitor currently activates once a registered resolver reads a path
that genuinely carries a lease. A resolver with nothing to report (env, or a
static Vault secret) is simply skipped every poll and stays reachable only
through the existing `/admin/reload-config` path — this is strictly
additive.

### 4. The threat model: what QueryGate defends against

The full reasoning lives in `docs/THREAT_MODEL.md`; here's the plain-language
version.

**The adversary QueryGate is built for is an authenticated but untrusted
caller** — most concretely, an AI agent that holds a real, valid credential
but might be malicious, confused, or *prompt-injected* (tricked by text it
read somewhere into trying to do something its operator never intended). The
threat model explicitly assumes this caller will try things like: sending
every technically-valid combination of query-AST fields to see what's
allowed, probing to see whether an error message reveals something about
data it can't otherwise see, replaying or flooding requests, or holding a
token that's valid but for the wrong tenant or environment. QueryGate has to
keep its guarantees intact against all of that, not just against a
well-behaved client.

**What it promises, in plain terms:**
- A caller can only ever see and query what its policy allows — nothing
  discoverable "leaks" a hidden table or column, even indirectly (through a
  `where` filter, an error message, or a denied column's absence looking
  different from a nonexistent one).
- A predicate's *value* (like a specific customer ID in a filter) can never
  turn into part of the SQL statement itself — it stays a bound parameter.
- Row-level restrictions an operator configured (like "only this tenant's
  rows") can't be removed or bypassed by anything a caller sends.
- Nothing that comes back to a caller — a response, an error, a schema, a
  stored audit record — contains a credential or an unintended slice of the
  database.
- Resource usage (how many queries run at once, how long they can run, how
  much data comes back) stays within policy-configured bounds regardless of
  what a caller asks for.
- Every security-relevant decision (allowed or denied) is traceable after
  the fact through an audit event.

**What's explicitly out of scope:** the threat model draws a clear line at
who controls the *deployment* itself. Someone with write access to
QueryGate's policy files, its host, its container, the database credentials,
or the reverse proxy in front of it is not the adversary this document
defends against — that's a privileged operator boundary, protected by normal
infrastructure controls (access control on the host, CI/CD, secrets
management), not by anything QueryGate's request-handling code does. The
document is also explicit that it isn't a substitute for an external
penetration test or compliance certification, and it names concrete residual
risks it doesn't claim to solve — for example, a caller authorized to see an
aggregate (like a `count`) can still sometimes infer something about
individual rows from a narrow-enough filter, since QueryGate protects
*column and table access*, not statistical inference in general.

### 5. The admin control plane has no token to steal

The `/admin/` and `/access/` browser surfaces are deliberately built so that
the most valuable thing a browser-based attack could go after — the operator's
bearer token — is **never in browser storage to begin with**. The token lives
only in an in-memory variable for the lifetime of the tab; there is no "remember
me", no `localStorage`, no `sessionStorage`, no cookie. Closing or reloading the
tab discards it and requires re-authentication.

Why this is a structural strength, not just a setting: the standard way an XSS
(cross-site scripting) bug turns into an account takeover is by reading a
session token out of `localStorage`/`sessionStorage` and exfiltrating it. That
entire class of attack is **removed by construction** here — there is no stored
token for injected script to read, so even a hypothetical XSS could not steal
the admin's credential from storage. This is the same "make the bad outcome
impossible by design rather than guarded against at runtime" posture as the
no-raw-SQL-field and no-credential-on-a-public-model guarantees elsewhere in
this document.

To be precise about the boundary (this product does not overclaim): this
removes *token theft from browser storage*, which is the highest-value XSS
outcome — it does not by itself make the page immune to XSS. Two other controls
reduce that surface: a strict Content-Security-Policy on every control-plane
response (`default-src 'self'; script-src 'self'`, no inline/external scripts,
`object-src 'none'`, `base-uri 'none'`, `frame-ancestors 'none'`) blocks the
common script-injection execution vectors, and the single-page app escapes
untrusted content before rendering it. The only other thing the control plane
persists in the browser is an in-progress **policy** draft in `localStorage`
purely so a tab crash doesn't lose edits (item 47) — never `connections.yaml`,
a credential, a secret reference, or a token. All of this is regression-locked:
`test_admin_ui.py::test_local_draft_recovery_is_policy_only_never_credentials`
asserts the shipped script contains no `sessionStorage` use and no token
storage key, and that the one `localStorage` write only ever persists the
policy document.

### 6. How we prove it: the CI-gated assurance program

Sections 1–5 are what QueryGate is *designed* to guarantee. This section is
about how a buyer can know those guarantees actually hold — because for a
security product, a claim is only worth what its evidence is. QueryGate's
posture is deliberately **"prove, don't assert"**: every guarantee on this
page is backed by an open-source, industry-standard check that runs in
continuous integration on every change, and each check is **deny-by-default**
— a regression that weakened it would fail the build, not slip through. The
full, reproducible write-up (each row names the command a reviewer can run in
a source checkout to see the same result CI does) lives in
[`docs/SECURITY_POSTURE.md`](SECURITY_POSTURE.md); this is the plain-language
summary.

The gates fall into three groups:

- **The access boundary itself.** The adversarial security suite (480 tests,
  `make test-security`) encodes specific known bypass classes as regressions —
  denied-column inference, undeclared-table smuggling, predicate-as-SQL,
  schema-discovery leaks, policy-cap breaches, audit no-leak. On top of that,
  **Schemathesis** (`make test-dast`) property-fuzzes the live OpenAPI schema
  with malformed and boundary payloads and gates on two rules: no fuzzed
  request may cause a server error, and every schema-violating input *must* be
  rejected — the latest run passed with zero server errors and zero accepted
  malformed payloads. Credential isolation (section 2) is asserted against the
  live OpenAPI and MCP schemas, so it catches "wired the wrong model into a new
  endpoint," not just "defined the model wrong."
- **Supply-chain hygiene** — the questions a security reviewer always asks.
  **Bandit + Semgrep OSS** run static analysis over the source (SAST);
  **pip-audit** checks the *exact* shipped dependency set against the CVE
  database; a **CycloneDX SBOM** is generated per release; **Trivy** scans the
  shipped container image for OS/library CVEs, embedded secrets, and
  misconfiguration; and **gitleaks** scans the working tree *and the full git
  history*, so we can affirmatively prove no credential was ever committed.
  Every one of these is deny-by-default: any finding fails the build. Where the
  dependency and image scans found real CVEs, they were **fixed by upgrading to
  patched versions, not accepted** — the Trivy exception list (`.trivyignore`)
  is **empty**, and the pip-audit allowlist (`security/dependency-audit-
  allowlist.json`) holds exactly **one** reviewed entry (`PYSEC-2026-286`,
  asyncmy — a SQL-injection CVE in a codepath SQLAlchemy's `mysql+asyncmy`
  dialect never reaches, guarded by a regression test), with every other
  previously-known CVE in the shipped set remediated by upgrade rather than
  accepted. The only other recorded exceptions are dev-only secret-scan
  placeholders (`.gitleaks.toml`) and a few reviewed, inline-justified SAST
  suppressions (`# nosec` / `# nosemgrep`) — never silent.
- **Reliability under real load.** Security guarantees must hold under
  concurrency, not just in isolation — so `make test-load`/`make test-soak` run
  the guardrails against a real Postgres and assert caps are never breached and
  no connection or slot leaks.

**Being honest about attestations matters more than badge-count**, and a buyer
will respect the precision. QueryGate ships as a private, closed-source
container image, so the OpenSSF Best Practices *badge* — awarded only to
public FLOSS repositories — cannot be earned today; instead we keep a genuine
**self-assessment** against its criteria that attaches to a security
questionnaire. **Signed artifact distribution is now implemented** — the release
workflow signs the published image with **Sigstore/cosign** (keyless) and
attaches a **SLSA build-provenance** attestation, both bound to the image digest
and verifiable by a consumer with `cosign verify` / `gh attestation verify` (see
`docs/RELEASING.md`); this was the external attestation that genuinely fits a
self-hosted image product. What we still do *not* claim is an independent
penetration test or a formal certification. Stating the real gaps plainly is
itself part of the posture.

### The published adversarial benchmark — proof a reviewer can rerun

The assurance gates above run inside CI; a prospect's security reviewer often
wants to run the proof themselves and see a *number*. That is what the
**adversarial security benchmark** (`querygate-security-benchmark run`) is: a
fixed, versioned attack corpus
([`benchmarks/security_boundary_v1.yaml`](../benchmarks/security_boundary_v1.yaml))
run against the real request-pipeline guardrails, entirely offline (no
database, no network, no LLM), so the result is deterministic and reproducible
in a source checkout. It reports QueryGate's catch rate next to a
structurally-modeled *raw-SQL-passthrough baseline* — a gateway whose interface
is a model-generated SQL string forwarded with no AST contract — over SQL
injection, denied-table/denied-column smuggling across every clause, complexity
caps, and the no-raw-SQL structural invariant. It also **discloses the
documented inference residuals** it does *not* block (it never counts those as
catches), which is the point: an honest, rerunnable benchmark is more
persuasive than an asserted one. The published methodology, results, and the
factual (capability-level, non-live) Google MCP Toolbox comparison live in
[`docs/business/SECURITY_BENCHMARK.md`](business/SECURITY_BENCHMARK.md). A
*live* LLM/Toolbox head-to-head is a scoped phase-2 follow-up that needs
external infrastructure; see the [Decision Log](#decision-log) for why the
baseline is a declared structural model rather than a live competitor run.

### SQL injection, in one line

Bound parameters make SQL injection impossible by construction — this is
covered in the [Core Request Pipeline's compilation stage](#3-compilation--ast--policy-becomes-real-sql);
the threat-model table's own entry for it (`QG-01`) ties it back to the same
two guarantees covered above: no raw-SQL field exists to inject into, and
every value that reaches the database travels as a separate bound parameter,
never concatenated into SQL text.

## Connections, Policy & Configuration

QueryGate's behavior — which databases it can reach, and what an agent is
allowed to do against each one — comes entirely from two YAML files on disk:
a connections file and a policy file, plus an optional third one for the
catalog (covered in its own section). This section explains how those files
become live, in-memory config; how a change gets applied without downtime;
and how the admin surface manages that process as history rather than as ad
hoc file edits.

### File-configured, not code-configured

**Files:** `src/querygate/connections/registry.py`,
`src/querygate/policy/loader.py`, `src/querygate/catalog/loader.py`

There is no app-owned configuration database. "Configuration database"
means a database QueryGate itself reads/writes to store its own settings —
QueryGate doesn't have one. Every connection profile and every policy rule
lives in a YAML file (`CONNECTIONS_FILE`, `POLICY_FILE`, optionally
`CATALOG_FILE` — see `examples/connections.example.yaml`,
`examples/policy.example.yaml`). Practically, this means:

- Config review is a code/file review. A change to who can query what shows
  up as a diff on a YAML file, in your existing version control, not as a
  row changed in some admin database you'd need a separate audit trail for.
- There's nothing extra to run or back up. No migrations, no schema for the
  config store itself, no second database to keep available just so
  QueryGate can start.
- Getting the current config back to a known state is "check out an old file
  version," the same recovery story as any other file in the repo.

Each of the three loaders (`ConnectionRegistry`, `PolicyStore`,
`CatalogStore`) follows the same shape: a `from_file()` classmethod parses
the YAML into validated Pydantic models once, and the result is cached as a
process-wide singleton (a module-level global, e.g. `registry.py`'s
`_registry`) behind a `get_registry()` / `get_policy_store()` /
`get_catalog_store()` accessor. The first request to touch any of these
triggers the load; every request after that reuses the same in-memory
object — no YAML parsing happens on the hot path.

**Connection strings are never written in plaintext in the file.** A
connections file entry has a `connection_string` field, but it holds a
reference like `${QUERYGATE_DEMO_DB_URL}` or `${vault:path#field}`, not the
literal string. `secrets/resolvers.py`'s `SecretResolverRegistry`
interpolates that reference at load time — from an environment variable by
default, or from HashiCorp Vault if `VAULT_ENABLED=true` — so the
connections file itself is safe to commit to source control even though it
fully determines which databases QueryGate can reach.

### The atomic hot-reload

**File:** `src/querygate/config_reload.py`

Tightening a policy in response to an incident, or adding a connection,
doesn't require restarting the process. `reload_config()` re-reads all three
files from disk, builds fresh `ConnectionRegistry` / `PolicyStore` /
`CatalogStore` instances, and swaps them in for the singletons described
above.

The swap is safe for a request that's already in flight. Python's `global`
reassignment (`set_registry(new_registry)`, and similarly for the policy and
catalog stores) is a single reference write, which is atomic under Python's
GIL (Global Interpreter Lock — Python's mechanism that only lets one thread
execute Python bytecode at a time, which happens to make a single variable
assignment indivisible). A request that already read the old registry at the
start of the pipeline keeps using that same old object to completion; it
never sees a state where, say, the new registry is live but the old policy
store still is. There's no lock a query has to wait on, and no risk of a
half-swapped config where one store reflects the new file and another still
reflects the old one.

A few details worth knowing if you're relying on reload in production:

- **Stale connections get disposed, unchanged ones don't.** If a
  connection's `connection_string` or `dialect` changed (or the connection
  was removed entirely), `config_reload.py` calls
  `connections/engine.py`'s `dispose_engine()` so the next query against
  that connection lazily opens a fresh pool with the new settings. A
  connection whose config didn't change keeps its existing pool — no
  unnecessary reconnect churn on every reload.
- **Concurrency limits reset unconditionally.** Every connection's semaphore
  (`execution/concurrency.py`'s `SEMAPHORES`) is cleared on every reload,
  even for connections whose caps didn't change, because the semaphore is
  the one piece of `Policy` baked into cached runtime state. This is a
  deliberate, accepted tradeoff: reloads are rare, admin-triggered events,
  not hot-path operations, so a brief window where observed concurrency can
  drift slightly past the old or new limit is fine in exchange for not
  having to diff semaphore state.
- **Reload is itself serialized against catalog refresh.** `reload_config()`
  takes the same `catalog_process_lock()` that guards in-process catalog
  refresh (`catalog/repository.py`), so a full config swap can't interleave
  with a concurrent catalog-only refresh.
- **It's an authorized operation, not a public one.** The REST route,
  `POST /admin/reload-config` (`api/routes.py`), requires the
  `admin:reload-config` scope (`core/scopes.py`) on the caller's principal.

### What a config file actually looks like

A **connection** entry (`examples/connections.example.yaml`) names a
database and where to find it — nothing about what's allowed to be queried
against it lives here, that's policy's job:

```yaml
connections:
  - id: demo
    dialect: postgresql
    connection_string: ${QUERYGATE_DEMO_DB_URL}
    description: "Example demo database"
    enabled: true
    known_tables: [customers, orders, order_items, products, employees]
```

`known_tables` is an optional seed list so table discovery has something to
show before QueryGate has ever reflected the live schema; it falls back to a
real schema query if left empty. `join_group` (omitted above, so it defaults
to the connection's own id) is what lets two *different* connections be
joined in a single query — see policy below for the enforcement side of
that.

A **policy** entry (`examples/policy.example.yaml`) is where access actually
gets restricted:

```yaml
default:
  enabled: true
  max_joins: 4
  max_select_columns: 20
  max_limit: 100
  # ... every other cap

connections:
  demo:
    allowed_tables: [customers, orders, order_items, products]
    denied_columns:
      customers: [email]
    max_limit: 200
    mandatory_row_filters: []
```

`default` is the baseline applied to any connection with no entry of its own
under `connections`; a connection-specific entry is merged on top of
`default`, so you only write down what differs (`demo` above overrides
`max_limit` and adds table/column restrictions, but inherits every other cap
— `max_joins: 4`, etc. — unchanged). Notice `employees` is a real,
known table (per `known_tables` above) that's deliberately left off
`allowed_tables` here — it's reachable at the connection level but invisible
at the policy level, the realistic shape of "wall off the HR table with SSNs
and salaries from the agent."

Policy also supports an optional third layer, `principals` — a further
override keyed by the authenticated caller's identity (an API key's
configured subject, or a JWT's `sub` claim), merged on top of whichever of
`default`/`connections` applies. Without it, every caller of a connection
gets identical access; with it, one agent identity can get a tighter
`max_limit` or an extra denied column than another caller hitting the same
connection. `policy/loader.py`'s `PolicyStore.get()` resolves all three
layers at request time, and a connection-specific principal override wins
over a `"*"` (every-connection) one for the same principal.

For a **delegated** (on-behalf-of) request, the identity this layer keys off is
deliberately the **human's**, not the agent's: an agent acting for a human
carries the human as `Principal.subject` (from the token's `sub`) and itself as
`Principal.actor` (from the RFC 8693 `act` claim), so the `principals` override
that applies is the *human's*. An agent therefore inherits exactly the human's
access ceiling — it can never widen it — and both identities are recorded in the
audit event. See the audit section's "Delegated identity" note and the
[Decision Log](#decision-log) for why this required no change to policy
resolution.

### The admin surface: config as versioned history, not a live-edited file

**Files:** `src/querygate/admin/store.py`, `src/querygate/admin/service.py`,
`src/querygate/admin_ui/`

Editing `connections.yaml`/`policy.yaml` directly on a server and running
reload works, but it leaves no record of who changed what, no way to
preview a change before it's live, and no easy rollback. The admin API and
its UI (`admin_ui/` — a small static single-page app for reviewing policy,
staging changes, and inspecting audit history) exist to make config changes
a governed, inspectable workflow instead of an untracked file edit.

`admin/store.py`'s `ConfigVersionStore` is the durable history behind that
workflow. It's still file-based — matching the same "no app-owned database"
posture as the rest of config — but laid out as an append-only version log
rather than a single mutable file:

```
config_governance_dir/
  versions/<id>/manifest.json       — status, timestamps, actor, description
  versions/<id>/connections.yaml
  versions/<id>/policy.yaml
  versions/<id>/catalog.yaml        — only if this version has one
  versions/<id>/templates.yaml      — only if this version has query templates
  current.json                      — {"version_id": "<id>"} pointer
```

Every version is a complete, immutable snapshot of all its documents
together (connections + policy, plus catalog and query templates when the
deployment uses them — never a partial diff), written with the same atomic
write-then-rename pattern used elsewhere in the codebase
(`_atomic_write`: write to a `.tmp` file, then `os.replace()`). A version is
`staged` (created but not yet live), `active` (the one currently in effect),
or `inactive` (was active once, superseded). "Rollback" is not a special
operation with its own code path — it's `mark_active()` called on a version
that's already `inactive`; `admin/service.py`'s `apply()` just chooses
whether to log the action as `apply` or `rollback` based on the version's
status beforehand. History is never rewritten: rolling back doesn't delete
or edit anything, it just moves the `current.json` pointer, so every version
that ever existed stays inspectable.

`admin/service.py` layers `validate` → `preview` → `stage` → `apply` on top
of this store, and deliberately reuses existing primitives rather than
building a parallel path: applying a version is exactly `reload_config()`
pointed at that version's files, and validating a candidate is exactly the
same `validate_config()` the `querygate` CLI already uses, run against a
scratch temp directory so a candidate can be checked without ever touching
the live files or singletons. `simulate_candidate_policy()` and
`diff_candidate_access()` go a step further — they let an operator ask "what
would this uncommitted change do to principal X's access?" against an
isolated, tempdir-loaded copy of the candidate config, without staging or
applying anything.

**What `ConfigVersionStore` explicitly does not touch.** It stores a
`catalog.yaml` snapshot alongside each version purely as a point-in-time
copy for diffing and rollback context — it is not where live catalog
content is read from or written to. The catalog has its own separate
governed store and file lock, `catalog/governance.py` /
`CatalogFileRepository`, which is what `catalog/loader.py`'s refresh and
draft-proposal generation actually read and write. This boundary is
deliberate: `ConfigVersionStore` snapshotting its own copy of
`catalog.yaml` into `var/config_versions/` is convenient for showing "what
changed" in a version's preview, but if governance mutations (approve,
publish, rollback) were routed through it instead of through
`CatalogFileRepository`'s own lock, the two copies would silently diverge —
the config-version snapshot wouldn't reflect what `CATALOG_FILE` refresh had
actually written, and vice versa. So there are two versioning
mechanisms by design, each owning a different concern: `ConfigVersionStore`
owns "what did connections/policy/catalog look like at each admin-reviewed
checkpoint," and `CatalogFileRepository` owns "what is the current, actively
mutated catalog content." Neither is a shortcut for the other.

Access to all of this is scoped narrowly (`core/scopes.py`):
`admin:config:read` to see current/staged content and diffs,
`admin:config:write` to stage/validate/apply, and the separate
`admin:reload-config` scope specifically for the low-level "just re-read the
files from disk right now" REST endpoint that bypasses the versioned
workflow entirely (useful if you edited the files directly and want the
running process to pick them up).

**Moving a reviewed change between environments, and recovering a lost draft
(item 47).** `POST /admin/config/export` packages the documents an admin is
currently editing into a portable *change-set bundle* — a plain downloadable
JSON file carrying only the submitted document deltas plus the id and a
content *fingerprint* of the base version they were composed against (never
the base content itself, so export can't be used to read the active
connections/policy). `POST /admin/config/import` takes such a bundle back,
re-validates it through the exact same loaders `/validate` uses, and reports
whether the target's active version has *drifted* from the bundle's
fingerprint (`stale_base`, with the specific documents that moved) — but it
never stages or persists anything on its own. Staging still goes through the
unchanged `/versions` endpoint, so a bundle is a transport format, not an
ungoverned second config store. Both are `admin:config:write`-scoped and the
upload is size-capped (`AppConfig.config_bundle_max_bytes`). In the browser,
the control plane additionally auto-saves an in-progress **policy** draft to
`localStorage` so a tab crash doesn't lose it — and *only* the policy
document: `connections.yaml` (which can carry a literal credential), secret
references, and bearer tokens are never written to browser storage;
full-config recovery is the explicitly downloaded bundle instead.

A related but separate admin surface, `api/admin_connections_routes.py`,
answers a different question: not "what is QueryGate configured to connect
to," but "is that connection actually reachable right now." `GET
/api/v1/admin/connections` (`admin:connections:read`) exposes the same
cached per-connection health `HealthMonitor` already computes in the
background for `/health`'s aggregate readiness signal — dialect, enabled
state, derived status, last check/success, latency, and a stable redacted
`failure_category` (never a connection string or raw driver error). `POST
.../connections/{id}/test` (its own `admin:connections:test` scope,
independent of the read scope) triggers an immediate out-of-band re-check —
useful right after rotating a credential — reusing that exact ping path so
the result and its non-disclosure guarantee are identical; a per-connection
cooldown (`AppConfig.admin_connection_test_cooldown_seconds`) keeps a caller
from turning it into a way to hammer the target database with connection
attempts. Both read and probe scopes are deliberately independent of each
other and of the config scopes above — reachability is a different privilege
from configuration.

The browser control plane's "Connection health" tab is a thin rendering
layer over exactly this API — no new endpoint or backend logic, same pattern
as the catalog workspace being a rendering layer over item 32B's REST
routes. Each row's "Test now" button is disabled client-side (with an
explanatory `title`) when the caller lacks `admin:connections:test` or the
connection is deployment-disabled, mirroring how the catalog workspace
already gates its own action buttons per scope.

**How the control plane is organized (item 85).** The single-page app groups
its views into seven domains in the sidebar — **Overview**, **Connections**
(schema review + connection health), **Policy** (the visual designer, its
safe-start presets, and policy simulation), **Catalog** (Curate + Review
proposals + Versions & rollback), **Templates** (query templates),
**Releases** (change set + versions), and **Audit** (the redaction-safe audit
trail) — with a secondary tab bar inside any domain that has more than one
view. This is a pure nav/layout
shell: routing is still a flat `showView(<view>)` over a `viewMeta` map, now
resolved through a domain→tab model, and every per-view render function and
`hasScope`/`canRead`/`canWrite` gate is reused unchanged. The URL hash still
names the leaf view (`/admin/#catalog-versions`), so every view stays
deep-linkable and selects its owning domain and sub-panel on load. The
`catalog:author`-gated **Curate** panel is a tab inside the Catalog domain
that appears only when the principal holds that scope — the same gate as
before, expressed against the tab model instead of a standalone nav slot.

Crucially, the grouping makes an honest structural fact visible rather than
papering over it. Each domain's tab bar carries a one-line release signal:
Connections, Policy, and Templates say their edits **stage into the shared
release** — because `ConfigVersionStore` bundles policy + connections +
catalog.yaml + templates into one atomic version, so their *authoring*
surfaces feed a single cross-domain change set that is staged and applied
together in the **Releases** domain. **Catalog is the sole exception**: it
carries its own governance versioning (`CatalogFileRepository`, described
above) independent of `ConfigVersionStore`, so its tab bar reads
*self-contained* and the whole author → review → publish → rollback loop lives
inside the Catalog domain. The UI surfaces this split deliberately so an
operator can see which edits flow into a shared release and which publish on
their own — see the [Decision Log](#decision-log).

### Why policy is per-connection, not global

**File:** `src/querygate/policy/models.py`

A `Policy` bundles everything that bounds a query against one connection:
table/column allow and deny lists, per-query complexity caps (`max_joins`,
`max_select_columns`, `max_where_depth`, `max_where_predicates` and
`max_in_list_size` for WHERE/HAVING/CASE-condition/join-condition predicate
shape — all four of those positions are the same `WhereNode` tree and are bounded
identically (items 99, 103) — `max_expression_depth` and `max_expression_nodes` for the scalar
expression substrate (item 100; see
[Computed expressions](#computed-expressions-arithmetic-conditional-aggregation-nested-functions)),
`max_group_by`, `max_top_n` and
`max_partition_by` for ranked/windowed queries, `max_window_specs` and
`max_window_frame_offset` for window functions (item 101; see
[Window functions](#window-functions-running-totals-moving-averages-rank-in-place)),
`max_interval_days` for how far a relative-date shift may reach (item 102; see
[Dates and relative time](#dates-and-relative-time-the-last-30-days-without-doing-the-arithmetic)),
`allow_cross_join` for the one join type whose cost is the product of its inputs
(item 103, default off; see
[Joins](#joins-equality-sugar-a-general-on-condition-and-the-four-join-types)),
`max_set_op_arms` for how many SELECTs one `UNION`/`INTERSECT`/`EXCEPT` may
combine (item 104, default 3, `0` disables; see
[Set operations](#set-operations-union-intersect-and-except-in-one-statement)),
`max_limit`/`max_limit_aggregate`
for row counts, `max_response_bytes` for response size), execution
guardrails (`timeout_seconds`, `max_concurrency`, queue-depth caps),
`mandatory_row_filters` (a filter always AND-ed into every query touching a
table — e.g. tenant scoping, see below), and `join_group` (which other
connections this one may be joined with in a single query).

**Every one of those caps is reported by the same four surfaces**, derived from
`Policy` itself rather than hand-listed (item 115): the semantic access diff
(`/admin/config/diff`, which classifies a staged change as tightening or
loosening), the effective-guardrails view, the help API's policy summary, and
the admin UI's policy panel. Each of the four used to keep its own list and all
four had rotted — nine caps were invisible to at least one, so a change that
loosened, say, `max_expression_nodes` was shown to a reviewer as *no guardrail
change*. Adding a cap to `Policy` now reports it everywhere by default, and a
test fails if a surface grows a private list again or if a new cap's direction
is ambiguous (a larger `min_group_size` is more restrictive, not less — the
kind of thing a naive list gets backwards).

It's resolved *per connection* (`policy/loader.py`'s `PolicyStore`,
optionally narrowed further per authenticated principal) rather than being
one global setting, because different databases behind the same QueryGate
deployment realistically need different rules. A reporting warehouse might
tolerate a `max_limit` of 1000 and heavier joins; a database with a raw
`employees` table containing salaries needs its sensitive columns denied
outright. Making `Policy` global would force every connection to the
strictest common denominator, or require unrelated hacks to carve out
exceptions per table. Keyed by connection id instead, each database gets
exactly the boundary appropriate to what it contains, and the merge order
(`default` → per-connection override → optional per-principal override,
all in `policy/loader.py`) means most deployments only need to write down
what's *different* about a given connection or caller, not restate every
cap from scratch.

`join_group` is the one field that ties policy back to the connection
layer: two connections can only be joined together in a single structured
query if their resolved `Policy.join_group` values match (checked in
`validation/schema_validation.py`, part of the Core Request Pipeline
described earlier in this guide) — a policy-level assertion of "these two
databases are close enough / trusted enough to combine," not something a
caller can request their way around by naming both connections in a join
clause. Being in the same `join_group` only settles *whether* a join is
allowed, not *whose* `Policy` governs the joined-in table once it is: a
joined table's column masks, mandatory row filters, and table/column
deny-list are enforced from BOTH its own connection's `Policy` and the
primary connection's, never one exclusively (TODO.md item 156) — the same
per-connection resolution the catalog sensitivity-label approval trigger
uses (item 155).

### Purpose-bound access: a declared reason that narrows, never widens

**File:** `src/querygate/policy/models.py`, `validation/policy_validation.py`
(item 145, feature F7). `StructuredQuery.purpose` is a closed-set token — not
free text like `intent` — a caller declares alongside a query (e.g.
`"fraud_review"`). If `Policy.allowed_purposes` is empty (the default), a
connection hasn't opted into purpose-gating and a declared purpose is
accepted but inert, the same "empty allow-list = unrestricted" convention
`allowed_tables` uses. Once `allowed_purposes` is non-empty, *every read
query* on that connection must declare a purpose from the set, or it's
rejected before any DB touch — the same posture as an unresolvable claim.
Read-only: the gate runs in `policy_validation.py`, which the write pipeline
never calls, so governed writes (item 93) are unaffected by this setting.

A valid purpose narrows the effective policy via `Policy.purpose_policies`, a
map from purpose token to a `PurposePolicyDelta`: additional
`denied_tables`/`denied_columns`/`mandatory_row_filters`/`column_masks`
unioned onto the connection's base `Policy`. There is deliberately no "allow"
field on a delta — a purpose can only take away access the base policy
already granted, never grant more, which makes "narrows never widens" true
by construction rather than by convention. `validate_policy` returns this
effective policy, and `execution/service.py` reuses it for compilation too
(not just validation), so a purpose-added mandatory filter or column mask
actually reaches the SQL — `explain()` and `verdict()` share the same
choke point and get the same treatment. The declared purpose is persisted to
the audit event (unlike `intent`, which is deliberately excluded): it's a
fixed token from an operator-configured allow-list, not caller-authored
prose, so recording it doesn't touch the redaction guarantee.

### Cumulative disclosure budget: bounding what many legal queries add up to

**File:** `src/querygate/execution/disclosure_budget.py` (+ its cross-replica
sibling `redis_disclosure_budget.py`), item 179.

Every guardrail in QueryGate that judges *disclosure* judges **one query**. (The quota and the concurrency limiter do span requests — but they count requests and bytes, not what those requests reveal.) The k-anonymity floor
(`min_group_size`, above) is the clearest case: it injects
`HAVING count(*) >= k`, so no single aggregate returns a group backed by fewer
than *k* rows. But a caller who asks fifty *individually* legal, individually
k-compliant questions can still reconstruct the row the floor was hiding —
`salary > 50000`, `> 60000`, `> 70000`, and the differences between the answers
are the people in between. Item 88 said this was out of scope; this is the piece
that bounds it.

**The key insight is that repetition, not variety, is the signal.** The query
shape QueryGate records (`audit/events.normalize_query_shape`) keeps operators,
column names, `group_by` keys and join structure but strips predicate
literals — the same redaction-safe projection that makes the audit event safe
to persist. So a differencing probe walking a constant is **one shape re-sent N
times**, while genuine exploration is N *different* shapes. A design that
counted distinct shapes would have missed the attack completely; this one counts
re-runs.

Two caps, each `None` (off) by default, each applying per **(principal,
connection, declared purpose, table)** over a rolling window:

- `max_shape_repeats_per_window` — how many times one query shape may be re-run
  against one table. Intended as the targeted probe cap, but **currently
  evadable** (item 186) — the fingerprint does not canonicalize a referenced
  select alias, a cte rename, a nested-scope alias, or list order, so a prober
  can mint a fresh bucket per probe. Do not set this cap alone until that lands.
- `max_aggregate_queries_per_window` — how many aggregate queries may touch one
  table however the shape varies. The blunt backstop that catches a prober who
  varies its shape to dodge the first cap.

Design decisions worth knowing:

- **Purpose is in the key**, so each declared purpose carries its own
  independent budget. That is what turns item 145's purpose from a per-query
  narrowing into a *cumulative* bound. It deliberately does **not** put a cap on
  `PurposePolicyDelta`: every field `for_purpose` narrows today is a list or
  table-keyed dict it unions, and `validate_structural_caps`' safety argument explicitly breaks if
  the delta ever gains a subtractive field. Keying the window by purpose gets
  the same outcome without touching that argument.
- **Principal, not actor.** Under item 90's delegation (RFC 8693) the principal
  is the *human* whose policy applied and the actor is the agent, so keying on
  the actor would budget an entire agent fleet as one identity and let one agent
  deny service to every other. Note the honest limit: with a **non-delegated**
  shared service credential the principal *is* the fleet, and the budget is
  shared by everything using that credential — delegated identity is what makes
  per-human budgeting real.
- **Only `execute` spends budget.** `explain` and `verdict` return no rows, so
  they disclose nothing for a disclosure budget to meter — they neither spend a
  budget nor are refused by a spent one.
- **Both caps require `min_group_size`.** With no k-floor a caller can read the
  rows directly, so bounding aggregate differencing would cost availability and
  protect nothing.
- **The Redis backend fails closed**, unlike the quota limiter (which fails
  open unconditionally) and the concurrency limiter (which fails open by
  default, via `concurrency_redis_fail_open`). Those are anti-abuse controls where admitting traffic is the
  right degradation; this one defends a privacy guarantee, and failing open
  would silently suspend it while the operator's config still said it was
  enforced.

**What this is not.** It is a *bound*, not a closure. A caller inside its budget
still differences successfully. Because the shape is predicate-literal-free the budget
cannot distinguish a probe from an innocent repeat, so it is deliberately
conservative and will also count benign repetition. And there is **no
recommended threshold** — we have not calibrated these against real traffic, so
an operator switching this on is choosing a number we have not validated for
them. Closing multi-query differencing properly needs query-set auditing or
differential privacy, both still out of scope (`docs/INFERENCE_RISKS.md` R3).

## Catalog / Semantic Layer

### The problem: knowing a schema isn't the same as understanding it

Every connection already supports **reflection** — SQLAlchemy connecting to
the real database and reading its actual table/column names and types (see
`src/querygate/schema/reflection.py`, covered in the Core Request Pipeline
section). Reflection answers "what exists." It cannot answer:

- Is `orders.status` a business-meaningful lifecycle field, or an internal
  code an agent shouldn't reason about directly?
- Which foreign key is the *right* one to join `orders` to `customers` on,
  when there are three plausible candidates?
- Is `customers.email` safe to select, or does it carry personal data that
  should stay hidden even from an otherwise-permitted caller?
- Has anything about this schema changed since an agent last looked at it?

Left alone, an AI agent answers these questions by trial and error: list
tables, describe several candidates, guess at a join, run a query, see if
the shape looks right. That's slow (many discovery round trips before the
first real query) and risky (a technically valid but semantically wrong
query — joining on the wrong key, aggregating the wrong column — can look
successful while being silently incorrect).

The **catalog** (`src/querygate/catalog/`) is QueryGate's answer: a curated,
versioned layer of business metadata — descriptions, aliases, relationship
hints, sensitivity labels, preferred aggregations — that sits *on top of*
live reflection. It never changes what a query is allowed to do. As the
module's own docstring puts it (`catalog/models.py`):

> The catalog remains descriptive only: it cannot grant access, alter a
> policy, or affect query compilation/execution.

An agent calling `describe_table` or `search_catalog` gets both layers
merged: the real, reflected schema, annotated with whatever curated
knowledge exists for it — filtered by the caller's own policy, so catalog
metadata can never disclose more than ordinary schema discovery already
would (`tests/unit/test_catalog_relationship_hint_cannot_disclose_a_denied_table`
locks this in for relationship hints specifically).

This is opt-in end to end. A deployment that never sets `CATALOG_FILE`
behaves identically to one without this module at all — `describe_table`
just returns `catalog: null` for every table and column
(`catalog/loader.py`'s module docstring).

### Four knowledge classes, and a strict pecking order

Not all catalog knowledge is equally trustworthy. `catalog/models.py`
defines a `KnowledgeSourceClass` with four tiers, ordered by a
non-configurable `CatalogPrecedence`:

| Source class | Precedence | Where it comes from |
|---|---|---|
| `verified` | 400 (highest) | A human wrote or approved it |
| `observed` | 300 | Deterministic schema facts (used by the legacy-upgrade path) |
| `inferred` | 200 | A generated suggestion (currently: manually-imported structured batches) |
| `learned` | 100 (lowest) | A pattern the system noticed from real query usage |

The rule that makes this safe, `replacement_decision`
(`catalog/models.py`), is deliberately simple and hard to route around: a
draft or non-verified source can **never** overwrite a field that's already
`verified`, and a `sensitivity` label (like marking a column `pii`) can
*never* be changed by any automatic merge, full stop — regardless of
precedence. Changing a sensitivity label is always a manual, explicit
catalog edit. This one function is the single gate every governed publish
goes through (`catalog/governance.py` calls it directly rather than
inventing its own merge logic), so there's exactly one place "can this
overwrite that" is decided.

### Provenance: every piece of catalog knowledge carries its own paper trail

Every table, column, relationship, and draft proposal carries a
`CatalogEntryProvenance` (`catalog/models.py`) — think of it as a permanent
label answering "where did this claim come from, and how much should I
trust it": which knowledge class produced it, a list of evidence pointers
(`CatalogEvidence` — a *kind* like `manual`/`schema`/`model`/`usage`/
`import` plus a short redaction-safe reference string, never the actual
evidence payload), a confidence score, a lifecycle `status`
(`draft`/`verified`/`rejected`/`stale`/`archived`), who created and
approved it and when, and a `schema_fingerprint` (see below) recording
which version of the real schema this claim was made against.

This matters because catalog entries aren't static documentation — they're
claims that can go stale, get contested, or need a paper trail for "who
said this table means X, and when." An agent's `search_catalog` result
comes back with a `CatalogCitation` (`catalog/retrieval.py`) for every hit:
source class, status, confidence, precedence, and freshness — so the
consuming agent (or a human debugging its behavior) can see not just *what*
the catalog claims, but *how sure QueryGate is* and *whether it might be
outdated*.

### Schema-only fingerprints and diffs: knowing when the world changed

`catalog/schema_memory.py` builds an `ObservedSchemaSnapshot` — a
deterministic, canonical description of a connection's real schema (table
names, column names/types/nullability/primary keys, foreign keys, indexes)
with one important redaction: comments are stored only as a SHA-256 hash
(`comment_fingerprint`), never as raw text, specifically because raw
database comments are attacker- or admin-controlled free text that
shouldn't be replayed into an agent's context ("prompt-injectable," in the
module's own words). The whole snapshot reduces to one `fingerprint` — a
SHA-256 hash of the canonical, sorted JSON payload — so "has this schema
changed at all" is a single string comparison.

When it *has* changed, `diff_schema_snapshots` produces a structured
`SchemaDiff`: which tables/columns were added or removed, which columns
changed type/nullability, which foreign keys or indexes changed. It also
proposes — never asserts — likely renames: if a removed column and an added
column have an identical structural signature (same type, nullability,
etc.), that's reported as a `PossibleRename`, a hint for a human reviewer,
not an automatic fact.

`catalog/refresh.py`'s `refresh_catalog_schema` uses that diff to decide,
entry by entry, whether a piece of catalog knowledge is still trustworthy.
Only the specific tables/columns/relationships the diff actually touched
are marked `stale`; everything else keeps its existing status and just gets
rebound to the new fingerprint. This selective invalidation matters — a
naive "any schema change staleness everything" policy would throw away
good, human-verified knowledge every time an unrelated table changed
anywhere in the database. A `stale` entry isn't deleted; it's flagged so
callers (and the governance workflow — a stale proposal can't be approved
or published, see below) know not to trust it blindly until it's
reviewed. Refresh runs opt-in, either on demand (`querygate-semantic-memory
refresh`) or via a background `CatalogRefreshMonitor`
(`SEMANTIC_MEMORY_REFRESH_ENABLED`, off by default) — and never touches row
data; it only ever reflects structure.

### Draft proposals: quarantined by construction

`catalog/generation.py` and `catalog/providers.py` let new catalog content
get produced — today only from a `manual` provider (an operator imports a
strictly-structured batch of suggestions offline; there's no live model
call, no HTTP client, no hosted provider adapter in this codebase yet —
`providers.py`'s own docstring is explicit that neither shipped
implementation has "a network primitive"). Whatever produces a suggestion,
the output always lands as a `CatalogDraftProposal`
(`catalog/models.py`), never as a direct edit to a real table/column entry.

A proposal is structurally quarantined, not quarantined by convention:

- Its `content` field (`CatalogDraftContent`) can only ever carry
  `description`, `aliases`, and `default_aggregation` — there is no
  `sensitivity`, `allow_samples`, policy, or mandatory-filter field on that
  model *at all*. A reviewer approving a proposal cannot accidentally
  change data access, no matter what they click "approve" on.
- Its `source_class` is validated to be `inferred` or `learned`, never
  `verified` — a generated proposal cannot claim to already be trusted.
- `search_catalog` and `describe_table` only ever read from the real,
  published table/column/relationship entries — proposals live in a
  completely separate list (`draft_proposals`) that neither of those
  agent-facing lookups touches, regardless of a proposal's review status.

**Why quarantine matters:** without it, "generate suggestions about my
schema" and "change what agents see" would be the same action — meaning a
bad automatic suggestion (or a compromised/malfunctioning generation
process) could immediately start feeding wrong information to every agent
querying that connection, with no human in the loop. Quarantine means a
suggestion has to be reviewed and explicitly promoted before it can affect
anything an agent sees.

### 32B: the governed review workflow

`catalog/governance.py` (its module docstring is the best one-paragraph
summary in the codebase for this) implements the human workflow that turns
a quarantined proposal into real, agent-visible catalog content — and it's
built entirely on 32A's existing `CatalogStore` and
`CatalogFileRepository`, so there's no second catalog file, database, or
mutation path to keep in sync.

**The state machine.** Every proposal starts `pending` and can only move
through one of these explicit, validated transitions:

```
pending ──edit──> pending            (content changes, resets nothing else)
pending ──approve──> approved
pending ──reject──> rejected         (requires a non-empty reason)
approved ──reject──> rejected        (requires a non-empty reason)
approved ──publish──> published      (merges into real catalog content)
published ──rollback──> (version reverted; proposal stays published, marked rolled_back in history)
```

Every transition is attributed to an `actor` and timestamped, and gets
appended to the proposal's own `review_history` — so "who approved this,
and when" is always answerable. Invalid transitions (approving twice,
rejecting something already published, editing after approval) raise a
`CatalogGovernanceError` and change nothing. There is no code path — by
construction, not just by policy — that lets a proposal publish itself;
`publish_proposal` hard-requires `review_status == APPROVED`, which can only
be set by a separate `approve_proposal` call.

**Publishing is where the precedence gate and the "no silent overwrite"
rule actually run.** `publish_proposal` re-checks that the proposal isn't
`stale` relative to the current schema fingerprint, then calls
`replacement_decision` per touched field. On top of that gate,
`governance.py` adds one more rule of its own: if a field already carries
verified, human-approved content and the proposal's value would actually
change it, that's *always* a reviewable conflict — publishing is refused
outright, listing exactly which fields conflict, rather than silently
overwriting someone's prior verification (see the `had_prior_value`/
`existing_provenance.status == VERIFIED` check in `_plan_publish`). The
only sanctioned ways to change already-verified content are an explicit
rollback, or hand-editing `catalog.yaml` directly.

**Every publish is a durable, reversible event.** Publishing writes a
`CatalogVersionRecord` into an append-only `version_history` (capped at
2000 entries) — actor, timestamp, the proposal that produced it, and a
before/after content diff. `rollback_version` reverses one: it restores the
prior field values (or removes an entry the publish had created), and it's
safety-checked — refusing if the entry has changed again since that publish
(so a rollback can't silently discard someone else's newer edit) or if
rolling back a table's *creation* would collaterally strip columns a later,
separate publish added to that same table.

**Preview before you commit.** `preview_publish` is a read-only,
test-as-principal dry run: given a specific principal's policy, it reports
whether that principal could even *see* the target object and whether
publishing would hit a conflict — without writing anything. It reuses the
exact same visibility check `describe_table` uses, so "would this be
visible" is answered consistently everywhere.

**Backup, restore, and cleanup (32B-2).** `export_connection`/
`import_connection` give one connection's entire governed history
(published entries, quarantined proposals with their review history,
generation records, version history, and the schema snapshot) as a single
self-contained bundle — used both for migrating a connection's catalog
between environments and for disaster-recovery restore. Because
`version_id`s and `generation_id`s are file-global (not per-connection),
import always remaps them against the target file's current content, so
importing one connection's export can never collide with or corrupt
another connection's history — the CLAUDE.md note about this ("do not
simplify that away") reflects a real bug class this remapping specifically
prevents. `delete_proposal`/`delete_version_record` only ever remove
terminal-state records (a rejected proposal, or a published-and-rolled-back
proposal together with the rollback that reverted it) — anything still
live (`pending`, `approved`, or `published` with the publish still active)
must be rejected or rolled back first. That ordering means deletion can
never erase the only record of *why* the current catalog looks the way it
does.

All nine governance actions are exposed over REST
(`api/catalog_governance_routes.py`, under
`/api/v1/admin/catalog/{connection}/...`) and the `querygate-semantic-memory`
CLI, each gated by its own least-privilege scope (`catalog:generate`,
`catalog:review`, `catalog:edit`, `catalog:approve`, `catalog:reject`,
`catalog:publish`, `catalog:rollback`, `catalog:export`, `catalog:delete`)
— none implied by any other, so (for example) a principal who can review
proposals can't also silently approve or publish them. Every action emits
a redaction-safe `catalog.governance` audit event: who, what action, which
proposal/version, outcome — never the draft text itself.

**All of this is also a browser workflow, not just REST/CLI (item 38).** The
admin UI's Catalog domain gives a reviewer the full loop without touching the
API directly: a filtered proposal queue with side-by-side proposed-versus-
published comparison, edit/approve/reject/publish with a publish-conflict
preview, connection-scoped version history with rollback, bulk approve/
reject/delete for working through a queue at once, one-click export/import
(backup/restore) of a connection's governed history, browser-triggered
`generate-drafts`/`learn` runs, a per-proposal review-history trail, and a
usage-signals browsing tab over the 32C evidence the learner draws on. It is
a thin client over the same nine governance routes above — no separate
mutation path, no relaxed scope.

### Human-authored catalog entries (item 84)

Generated and usage-learned proposals aren't the only way curated content
gets in. A human can author a catalog entry directly — describe a table,
column, or relationship in a guided form (the admin UI's **Curate** panel:
pick a connection → object type → table → optional column, then fill in a
description, aliases, or a table's default aggregation, each with inline
field help) instead of hand-typing raw `catalog.yaml`. The key decision is
*where that authored content goes*: it is composed into the same validated
`CatalogDraftContent`/`CatalogDraftTarget` models and routed through the
**catalog governance queue** (`create_manual_proposal` →
`CatalogFileRepository`), **not** the change-set/`ConfigVersionStore`
`catalog.yaml` tab. So a hand-authored entry gets the identical staged →
reviewed → published → rolled-back safety and actor-attributed audit that
agent-generated proposals already get, and there is still exactly one
catalog file and one mutation path.

A manual proposal carries a distinct `source_class = manual`. That is a
proposal-only class: it stays **quarantined** (never agent-visible, never
indexed) until a reviewer publishes it, at which point the normal
`publish_proposal` merge mints a `verified` entry — it is *publishable as
verified*, never auto-trusted. Authoring is gated on its own
`catalog:author` scope, separate from `catalog:review`. Separation of duties
is deliberately **scope-based, not identity-based**: a deployment that wants
strict separation grants `catalog:author` and `catalog:review` to different
principals, but a principal holding both may approve and publish its own
manual proposal — there is no author≠approver identity check. Actor
attribution (`created_by`/`approved_by`) is still recorded on every step for
audit; it's the *gate* that's scope-driven, not the paper trail.

### Search and retrieval: what an agent actually sees

`catalog/retrieval.py`'s `search_catalog` is the read path agents actually
call (`GET /api/v1/{connection}/catalog/search`, and the MCP `search_catalog`
tool). It's deliberately simple — deterministic lexical token matching, not
an embedding/vector search — bounded to 1–20 results and a 16 KiB response
by default, over only the connection's *published* catalog content
(proposals are never candidates here, regardless of review status).

The security-critical detail: **policy is applied before anything else.**
`search_catalog` filters out any table/column/relationship the caller's
policy denies *before* that content is tokenized, scored, counted, or
budgeted — so a hidden object can't affect ranking, result counts, or even
whether the response happens to be truncated. It goes one step further:
`policy_hidden_identifier_tokens`/`policy_safe_catalog_text` also strip a
hidden table or column's exact identifier out of an otherwise-visible
entry's free-form description or aliases, so a curator can't accidentally
leak a denied identifier by mentioning it in prose on a visible neighbor.
Every hit returns its `CatalogCitation` (source, status, confidence,
freshness) so an agent — or a human reading a transcript — can judge how
much to trust it, and `stale`/`draft` results are never presented without
that state being visible.

### Why the catalog can never become a query or row-value path

This boundary shows up repeatedly across the module, on purpose:

- Catalog entries hold *descriptions of* data (a table's purpose, a
  column's meaning), never row values. `allow_samples` is captured as a
  metadata flag, but nothing in this codebase generates or returns actual
  sample values from it.
- Schema snapshots are structural only — table/column/type/nullability/
  key/index shape — never a `SELECT` against the database; comments are
  hashed rather than stored verbatim for the same reason.
- Usage signals (see 32C, below) record which table/column/relationship a
  query touched, never the predicate values or result rows.
- `search_catalog` and `describe_table` are read-only lookups against the
  catalog file; they never open a database session or route through
  `execution/service.py`'s `execute()`/`explain()`.

**Why this matters:** if the catalog could execute queries or search row
values, it would become a second, less-audited path into the same
databases the entire rest of QueryGate exists to gate — undermining the
"only a validated `StructuredQuery` AST reaches a database" guarantee the
whole product is built on (see the Core Request Pipeline section). Keeping
the catalog strictly descriptive means a catalog bug, a bad proposal, or
even a compromised generation process can produce *wrong metadata*, never
*unauthorized data access* — the two failure classes have completely
different blast radii, and QueryGate keeps them structurally separate.

### The semantic-memory benchmark: `querygate-semantic-memory evaluate`

`catalog/benchmark.py` defines a fixed, versioned, offline evaluation —
`poetry run querygate-semantic-memory evaluate` (wired up in
`src/querygate/catalog_cli.py`) runs it against the packaged
`semantic_memory_v1.yaml` corpus. It measures whether `search_catalog`
actually does its job well, not whether the catalog data model is merely
valid:

- **Expected-hit recall** — for each benchmark case (a natural-language-ish
  query against a small embedded demo catalog), did the search surface the
  table/column/relationship a human would expect?
- **Relationship recall** — the same, specifically for join-relationship
  hits, since picking the right join is one of the riskiest things an
  under-informed agent can get wrong.
- **Stale-detection rate** — when an entry is marked `stale`, does the
  search result actually say so (`citation.freshness`), rather than
  presenting outdated guidance as current?
- **Discovery-call reduction** — each benchmark case also records how many
  raw discovery calls (`list_tables`/`describe_table` round trips) a naive
  agent would need to reach the same answer without semantic search; the
  benchmark checks that one `search_catalog` call gets there in meaningfully
  fewer calls.
- **Policy violations** — a hard `0` ceiling: the benchmark also runs cases
  under a restrictive policy and asserts that a denied identifier never
  appears anywhere in the response, even inside a description string.

The pass/fail thresholds (`MIN_EXPECTED_HIT_RECALL = 0.85`,
`MIN_RELATIONSHIP_RECALL = 0.80`, `MIN_STALE_DETECTION_RATE = 1.0`,
`MIN_DISCOVERY_CALL_REDUCTION = 0.50`, `MAX_POLICY_VIOLATIONS = 0`) are
compiled constants in source, not something a benchmark fixture could
quietly relax after the fact — the module comment is explicit that this is
deliberate, so a future change to the corpus can't lower the bar after
seeing what the current code scores.

### Usage-based learning (32C) — shipped, and deliberately narrow

TODO.md item 32 describes a fourth knowledge tier beyond curated
(`verified`) and generated (`inferred`) content: `learned` — patterns
noticed from how agents actually use a connection, most concretely "these
two tables are frequently joined this way." This tier is implemented
(`catalog/usage.py`, `catalog/learning.py`,
`catalog/adaptive_learning_benchmark.py`), reusing the exact same 32B review
machinery described above — a learned proposal is an ordinary
`CatalogDraftProposal` with `source_class=learned`, goes through the
identical `pending → approved → published` state machine, and can no more
publish itself than a manually-imported one can. It's built under a
deliberately narrow set of guardrails worth calling out explicitly, since
they're the constraints that keep this tier safe:

- **Typed, redaction-safe signals only.** A `CatalogUsageSignal` can only
  record that one already-successful, already policy-validated query
  touched a specific table/column/relationship — never a predicate value,
  row, or raw principal identity (`principal_partition` is always a hashed
  partition key, `hash_principal_partition`, never the caller's real
  subject).
- **No live LLM calls.** The learner is pure pattern-matching over
  accumulated signals (support thresholds, a decay window, a conflict rule
  for competing join targets) — there's no model inference step in this
  path.
- **No autonomous publication.** Every learned proposal is quarantined
  exactly like a manually-generated one and requires an explicit human
  approve/publish through 32B before it can affect anything an agent sees.
- **An explicit anti-feedback-loop rule.** `should_emit_signal` only
  records a usage signal when the object's *current* catalog knowledge is
  either absent or already `verified` — never when an agent only chose that
  table/relationship because it followed the system's own unreviewed
  draft/stale guess. Without this, the learner could "confirm" its own
  unpublished suggestions just because an agent happened to act on them.
- **Off by default.** Both signal recording
  (`SEMANTIC_MEMORY_USAGE_SIGNALS_ENABLED`) and the background learning job
  (`SEMANTIC_MEMORY_LEARNING_ENABLED`) require an explicit opt-in plus a
  configured `CATALOG_FILE`.

This is a genuinely constrained feature by design: it can only ever
*propose* a join hint for human review, never rewrite what a table means,
never touch sensitivity or access, and never learn from its own unverified
guesses.

> **Note on this section's accuracy vs. `CLAUDE.md`:** at the time this
> section was written, `CLAUDE.md` still described 32C as "not started."
> `TODO.md` (item 32: "32C ✅") and the files above show it shipped. This
> guide reflects the verified code/TODO.md state; `CLAUDE.md` has been
> corrected to match (see the [Decision Log](#decision-log)).

## Auth & Transports (REST + MCP)

QueryGate can be reached two ways — a REST API (`src/querygate/api/`) and an
MCP server (`src/querygate/mcp/`), the protocol AI agents use to discover and
call tools (one or two sentences on that below). Both sit on top of the same
`StructuredQueryService` (see [The Core Request Pipeline](#the-core-request-pipeline))
and, just as importantly, the same authentication code
(`src/querygate/core/auth.py`). Neither transport has its own copy of
"check the bearer token" logic — they both call into one shared boundary.
That matters for a simple reason: authentication is exactly the kind of code
where a subtle bug (a comparison that isn't constant-time, an empty-key list
that accidentally matches everything, an ordering bug that lets an invalid
JWT fall through to "anonymous") is dangerous and easy to introduce a second
copy of. Fixing it once, in one file, means it's fixed for every way into
QueryGate — not fixed in REST and silently still broken in MCP.

### Principals, tokens, and the shared auth boundary

**File:** `src/querygate/core/auth.py`

Every authenticated caller — human, service, or AI agent — is represented as
a `Principal`:

- `subject` — who the caller is (a string identifier).
- `scopes` — a set of permission strings (e.g. `admin:reload-config`), used
  to gate sensitive operations like config reload or catalog governance.
  `src/querygate/core/scopes.py` is the single source of truth for the whole
  vocabulary: beyond the constants it carries a structured `SCOPE_CATALOG` and
  recommended `ROLE_BUNDLES`, from which both the RFC 9728 `scopes_supported`
  metadata and the human reference `docs/SCOPE_CATALOG.md` (via
  `make scope-catalog`, drift-tested) are generated — so an IdP can wire up
  QueryGate's scopes and roles without reverse-engineering source (item 95).
- `claims` — the raw claims from a token, if the auth method produced any
  (empty for a static API key).
- `auth_method` — which scheme produced this principal (`"api_key"`,
  `"jwt"`, or `"anonymous"`), useful for audit/debugging.

`Authenticator` is a narrow one-method protocol (`authenticate(bearer_token)
-> Optional[Principal]`) — deliberately small so a new auth scheme can be
added later without touching either transport's code, since both only ever
depend on this interface. Three implementations exist today, and they're
meant to be chained:

- **`ApiKeyAuthenticator`** — checks a bearer token against a configured list
  of static keys, using `hmac.compare_digest` (a constant-time comparison —
  it takes the same amount of time whether the first character matches or
  the whole string does, so a caller can't guess a valid key one character
  at a time by measuring response time). If no keys are configured at all,
  it returns "no match" rather than treating an empty list as "accept
  anything" — a deliberate choice, because "accept anything" would silently
  swallow every token before a real `JwtAuthenticator` further down the
  chain ever got a look at it.
- **`AnonymousAuthenticator`** — matches unconditionally, so QueryGate can
  run unauthenticated for local development. It must be placed *last* in a
  chain (see below) and is gated out of production entirely.
- **`CompositeAuthenticator`** — tries a list of authenticators in order and
  returns the first match. Order is what makes "accept an API key *or* a
  JWT, but never fall back to anonymous if either is configured" work
  correctly: real credential schemes go first, `AnonymousAuthenticator` goes
  last. Both `api/auth.py` and `mcp/auth.py` build this same chain — API key
  first, then JWT if enabled, then anonymous only when the deployment is
  local *and* neither an API key nor JWT is configured (`AppConfig.is_local`
  alone is not the gate — see `build_authenticator` in `api/auth.py`).

### A second scheme: verifying a JWT against a JWKS endpoint

**File:** `src/querygate/core/jwt_auth.py`

A static API key is simple but coarse: every caller sharing a key looks
identical, there's no expiry, and there's no way to plug in an existing
company identity provider (Okta, Auth0, Cognito, an internal SSO system).
`JwtAuthenticator` solves that by accepting a **JWT** — "JSON Web Token," a
compact, digitally signed string that encodes a set of claims (a small JSON
object: who this is, when it expires, what it's allowed to do) and a
signature over that content. Anyone holding the token can read the claims,
but only someone holding the matching private key could have produced a
valid signature — so a server that trusts that key can verify the token
wasn't forged or altered, without needing a database lookup or a call back
to the issuer for every single request.

That still leaves one question: *which* public key should verify a given
token? That's what a **JWKS** — "JSON Web Key Set" — answers. It's a small,
publicly-hosted JSON document (published by the identity provider at a
well-known URL) listing the public keys currently in use for signing. A JWT
carries a key id in its header, so a verifier fetches the JWKS, finds the
key with the matching id, and uses it to check the signature. The identity
provider can rotate its signing key over time — publish a new key in the
JWKS, start signing new tokens with it — without QueryGate needing a
redeploy, because it always fetches (and caches) whatever the JWKS
currently says.

Concretely, `JwtAuthenticator.authenticate`:

1. Uses `jwt.PyJWKClient` (from `PyJWT`) to fetch the signing key for this
   specific token from `jwks_url`, caching keys in-process (default 5-minute
   lifespan) so the JWKS endpoint is only hit on a cache miss, not on every
   request.
2. Calls `jwt.decode()` with that key, the configured `algorithms` (default
   `["RS256"]`), and optional `issuer`/`audience` checks — this verifies the
   signature *and* rejects an expired, not-yet-valid, wrong-issuer, or
   wrong-audience token in the same step.
3. Maps claims onto a `Principal`: `subject_claim` (default `"sub"`) becomes
   `Principal.subject`, `scopes_claim` (default `"scope"`, accepting either
   a space-delimited string per the OAuth2 convention or a JSON list) becomes
   `Principal.scopes`, and the full claim set is kept as `Principal.claims`.
4. On any verification failure, it logs *why* verification failed but never
   logs the token itself, and returns `None` (not a match) rather than
   raising — so it composes cleanly inside `CompositeAuthenticator`.

`authenticate` is a plain synchronous method (matching the `Authenticator`
protocol), even though the JWKS fetch on a cache miss is a blocking network
call — an accepted tradeoff, since it happens once per cache lifespan
rather than once per request, and keeping the interface synchronous avoids
making both transports' auth code async just for this one rare case (see
the module's own docstring).

`build_jwt_authenticator(cfg)` is the one function both `api/auth.py` and
`mcp/auth.py` call to build this authenticator from `AppConfig` — one
`jwks_url`/`issuer`/`audience`/algorithm configuration, shared by both REST
and MCP, so an operator configures their identity provider once
(`AppConfig`'s `jwt_*` fields in `core/config.py`) and it applies to every
way into QueryGate.

### REST transport

**Files:** `src/querygate/api/auth.py`, `src/querygate/api/routes.py`,
`src/querygate/api/app.py`

`api/auth.py` wires the shared auth boundary into FastAPI as a dependency.
`build_principal_dependency(cfg)` returns a function that reads the
`Authorization` header, extracts a bearer token, runs it through the same
`CompositeAuthenticator` chain described above, and raises
`HTTPException(401)` if nothing matched. Every route that needs a caller
identity declares `principal: Principal = Depends(get_principal)` and
FastAPI runs that check before the route body executes.

`api/routes.py` (mounted under `/api/v1` by default) is where the actual
data-access surface lives — the REST equivalent of the MCP tools described
below:

- `GET /connections` — list connections visible to this principal.
- `GET /{connection}/tables` — list tables on a connection.
- `GET /{connection}/tables/{table}` — describe one table's columns.
- `GET /{connection}/catalog/search` — search the semantic catalog (see
  [Catalog / Semantic Layer](#catalog--semantic-layer)).
- `POST /{connection}/query/explain` — compile a `StructuredQuery` to SQL
  without running it.
- `POST /{connection}/query` — validate, compile, and execute one
  `StructuredQuery`. `queue_mode=async` (TODO.md item 35 phase 3) returns
  `202` with an `admission_id`/`status_url` instead of blocking.
- `GET /{connection}/query/{admission_id}` — poll a `queue_mode=async`
  execution's state/result. 404s uniformly for an unknown id, a different
  connection's id, or (absent the `query:cancel` scope) a different
  principal's id.
- `POST /{connection}/query/{admission_id}/cancel` — request cancellation of
  a `queue_mode=async` execution; free while still queued, gated on
  `Policy.allow_query_cancellation` for a running query (see
  [Agent-visible capacity waiting](../README.md#agent-visible-capacity-waiting)
  in the README for the full contract).
- `POST /{connection}/query/batch` — the same, for a list of queries against
  one connection in a single call.
- `POST /admin/reload-config` — hot-reload connections/policy/catalog from
  disk; gated on the caller holding the `admin:reload-config` scope, checked
  directly against `principal.scopes` before the reload runs.

Separate routers (assembled in `api/app.py`) cover admin config governance,
connection health, catalog governance, the admin UI, and the help/guide
endpoints (see below) — all built the same way, taking the same
`get_principal` dependency, so they inherit this auth boundary rather than
reimplementing it.

**What FastAPI/OpenAPI buys here:** every request/response body is a
Pydantic model (`StructuredQuery`, `PublicConnectionInfo`, `TableDescription`,
etc.), so FastAPI validates incoming JSON against that shape automatically
and rejects malformed requests before a route body ever runs — one more
layer before a request even reaches [policy validation](#the-core-request-pipeline).
FastAPI also generates an OpenAPI schema and an interactive docs UI for
free from those same models — but `api/app.py` only exposes
`openapi_url`/`docs_url`/`redoc_url` when `cfg.is_local` is true, so a
production deployment doesn't hand out a machine-readable map of its own API
surface to anyone who asks. `tests/unit/test_credential_redaction.py`
specifically checks that this generated schema never exposes a credential
field, since `PublicConnectionInfo` is the only connection shape ever
returned over REST (see [Security Model](#security-model)).

### The typed Python query builder — authoring, not a new pipeline

**Files:** `src/querygate/client/builder.py`, `src/querygate/client/__init__.py`,
`examples/client_sdk_python.py`

A caller composes a `StructuredQuery` as JSON. That JSON is easy to get
subtly wrong by hand — a mistyped key, the wrong operator spelling, a
forgotten self-join alias — and today the only feedback is a `422` after a
network round-trip. `querygate.client` is a fluent, typed builder that makes
that authoring ergonomic:

```python
from querygate.client import Query, agg, col, desc

body = (
    Query.from_("orders")
    .join("customers", on=("orders.customer_id", "customers.id"))
    .select("customers.name", agg.count("*", as_="order_count"))
    .where(col("customers.country") == "GB")
    .group_by("customers.name")
    .order_by("order_count", desc=True)
    .to_dict()   # the exact wire JSON to POST to /api/v1/<connection>/query
)
```

The important design property: **the builder is not a second way to talk to
the database, and adds no validation of its own.** It constructs the *same*
`query_ast` Pydantic models the server validates, then serializes them. Two
consequences fall out of that for free:

- An illegal shape (a `count(*)` with `distinct`, a self-join missing an
  alias, a percentile fraction outside `[0,1]`) raises *client-side* with the
  identical error the server would return — because it *is* the server's
  validator running early.
- Whatever the builder emits is still fully policy-, schema-, and
  guardrail-checked by `StructuredQueryService` before a single row is read.
  A masked or denied column composed by the builder still gets a policy
  `422`. The builder cannot bypass or weaken any guardrail; it only makes the
  JSON pleasant to write.

Because it front-ends the real AST, it can't drift: `tests/unit/test_client_builder.py`
includes drift guards that fail if the `StructuredQuery` AST grows a field, a
`SelectItem` variant, or a comparison/aggregate/scalar function the builder
can't express. It ships inside the `querygate` package for now; a standalone
dependency-light distribution is still planned (TODO item 51 phase 2's other
half, coupled to item 30 phase 2's registry choice).

**The TypeScript sibling has shipped** (item 51 phase 2a, `clients/typescript/`):
a structural/behavioral mirror of the same fluent surface (`Query.from(...)
.select(...).where(col("a").eq(1))...build()`), producing the identical wire
JSON — but not name-for-name: every multi-word Python name is renamed
snake_case→camelCase per TS convention, beyond the handful JavaScript's own
grammar forces (no operator overloading, `case`/`in` are reserved words); see
`clients/typescript/README.md`'s naming table for the full picture. It is
in-tree only — not published to npm, for the same registry-choice reason the
Python side isn't standalone yet — and has its own two-part sync-test strategy
since it cannot import the Python Pydantic models: a hand-maintained coverage
suite (`clients/typescript/test/builder.test.ts`, the closest TS analogue of
Python's introspective drift guards, which have no runtime equivalent once
TypeScript's unions are erased at compile time) and a cross-language kitchen-sink
fixture (`tests/fixtures/client_builder_kitchen_sink.json`) that both builders
must reproduce for the same three representative queries, pinned on the Python
side by `tests/unit/test_client_builder_ts_parity.py` and on the TypeScript side
by `clients/typescript/test/kitchenSink.test.ts` — both wired into CI (a
`typescript-client` GitHub Actions job runs `npm ci && npm test`; the existing
`pytest` job already picks up the Python-side parity test with no changes
needed). Verified end-to-end against a real running server and real Postgres
during development (`docker compose up -d` + `uvicorn querygate.api.app:app`;
a manual check, not an automated/CI-enforced one — the same posture already
recorded for phase 1's `examples/client_sdk_python.py`): both a join/aggregate
query and a window-function running-total query built by the TS SDK returned
real rows over HTTP 200.

### MCP transport

**Files:** `src/querygate/mcp/server.py`, `src/querygate/mcp/auth.py`,
`src/querygate/mcp/transport_guard.py`, `src/querygate/mcp/tools/*.py`,
`src/querygate/mcp/exceptions.py`

**MCP** ("Model Context Protocol") is a standard protocol that lets an AI
agent discover a set of callable "tools" from a server — each with a name,
a description, and a typed input/output schema — and call them the same way
regardless of which server or which agent framework is on either end.
Instead of a human hitting REST endpoints with `curl`, an AI agent (Claude,
or any other MCP-capable client) connects to QueryGate's MCP server, lists
its available tools, and calls them directly as part of its own reasoning.

Because MCP is a shared standard, QueryGate works with any framework that
speaks it — there's nothing QueryGate-specific to install on the agent side.
The repo ships runnable, framework-idiomatic quick-starts under `examples/`
for the Claude Agent SDK, LangChain / LangGraph, LlamaIndex, and OpenAI's
Chat Completions function-calling (the OpenAI one bridges MCP tool schemas
into OpenAI function schemas, since that API doesn't speak MCP natively).
Each registers QueryGate as a Streamable-HTTP MCP server and lets the
framework's agent discover and call the tools itself. The examples deliberately
add no maintained QueryGate SDK layer of their own — they're thin references,
so there's no extra library to keep in sync with each framework's churn. A
model-free test (`tests/integration/test_integration_examples.py`) keeps them
honest by asserting every tool each example wires up is still a tool the MCP
server actually registers.

`mcp/server.py` builds one shared `MCPServer` instance (`mcp_server` — `mcp`
SDK v2, formerly `FastMCP`; TODO.md item 128's `2026-07-28` protocol
conformance) and, on startup, calls `discover_and_register_tools()`
(`mcp/tools/__init__.py`), which imports every non-underscore-prefixed
module under `mcp/tools/` — so registering a new tool is just adding a file
there, decorated with `@mcp_server.tool(...)`. That server is exposed as its
own ASGI app (`streamable_http_app()`), wrapped in `MCPAuthMiddleware`, and
mounted into the main FastAPI app at `cfg.mcp_mount_path` (default `/mcp`)
by `setup_mcp()`.

**Auth works differently here than in REST**, because MCP tool functions
aren't FastAPI route handlers with a `Depends()` mechanism — they're plain
async functions `MCPServer` calls directly. So `mcp/auth.py` authenticates at
the ASGI layer instead: `MCPAuthMiddleware` builds the same
`CompositeAuthenticator` chain (API key → JWT → anonymous-if-local) used by
REST, and on every incoming request extracts the bearer token, authenticates
it, and — if it succeeds — stashes the resulting `Principal` (and the
`AppConfig`) in a `contextvars.ContextVar` before calling into the MCP app;
if it fails, it short-circuits with a 401 and never calls the wrapped app at
all. Individual tool functions then call `get_mcp_caller()` /
`get_mcp_config()` to read that context — this is how a tool like
`run_structured_queries` knows *who* is calling without the caller having
to pass identity as a tool argument (which an agent could tamper with).
`run_structured_queries` also reports MCP's standard progress notifications
(`Context.report_progress`, TODO.md item 35 phase 3) to a client that
advertises support — "waiting for a concurrency slot" then "admitted,
executing" per query — a no-op for a client that doesn't, so this is purely
additive.

A second, outer ASGI layer sits *around* the auth wrapper:
`transport_guard.py`'s `MCPRequestGuardMiddleware` (TODO item 86). The
upstream MCP Streamable-HTTP transport parses the request body with its own
`json.loads`, which raises `RecursionError` on a body nested past the JSON
parser's recursion guard — surfacing as a *handled but HTTP 500* JSON-RPC
internal error, unlike REST's clean 400 for the same input. The guard closes
that asymmetry by rejecting an oversized body (`413`) or an over-deep body
(`400`, via a cheap O(n) structural-depth scan that skips string contents)
*before* the transport ever parses it — so malformed input is a clean client
error on the MCP surface too, never a 5xx. Both thresholds are configurable
(`mcp_max_request_bytes` / `mcp_max_request_depth`) with defaults far above
any legitimate batch, so normal traffic is untouched.

The same middleware also closes a gateway confused-deputy gap (TODO item
127). The MCP `2026-07-28` spec mirrors `method`/`params.name`/`params.uri`
into `Mcp-Method`/`Mcp-Name` HTTP headers so a fronting gateway can route and
authorize without parsing the body — and requires the server to reject a
request where a present header disagrees with the body (`400` +
`-32020 HeaderMismatch`), because otherwise a gateway authorizing on the
header while QueryGate executes the body is a confused deputy (e.g. a
gateway permits `Mcp-Name: list_tables` for a low-privilege caller while the
body actually invokes `run_structured_writes`). QueryGate now speaks the
final `2026-07-28` protocol revision (item 128, `mcp` SDK v2) that defines
these headers; the gate still validates **if present**, not required — a
direct MCP client has no reason to send transport-routing headers meant for
a fronting gateway, so requiring them from every caller would be a
conformance requirement the spec doesn't make — but it fails closed the
moment any caller (gateway or otherwise) does send them, run strictly after
the depth scan above (so a hostile deep body can't reach this check's own
`json.loads` first). Base64
"sentinel"-encoded header values (`=?base64?...?=`, used when a name isn't
safely ASCII) are decoded before comparison; a header wearing the sentinel's
markers that doesn't actually decode is rejected as malformed rather than
compared as literal text. `Mcp-Name` is checked against `params.uri` for a
`resources/*` method and `params.name` otherwise (a body carrying both is
rejected as ambiguous rather than guessed at), and a routing header sent more
than once — which has no single source of truth for an intermediary to agree
with QueryGate about — is rejected outright rather than resolved by first
match.

**Gateway-native per-connection routing (TODO item 130).** Every tool's
`connection` parameter carries the `2026-07-28` spec's `x-mcp-header`
JSON-schema annotation (`inputSchema.properties.connection["x-mcp-header"] =
"Connection"`), which a conforming client mirrors into an
`Mcp-Param-Connection` HTTP header on the Streamable HTTP transport (the
value lives at `params.arguments.connection` in the JSON-RPC body, since a
`tools/call` request nests every tool argument under `arguments`). `id` is
already public — `PublicConnectionInfo` returns it, and the spec only warns
against annotating *sensitive* parameters — so a customer's existing gateway
can enforce "this agent identity may only reach the `analytics` connection"
at the edge, cheaply and natively, entirely without parsing the JSON-RPC
body. `mcp/transport_guard.py`'s `MCPRequestGuardMiddleware` — the same
guard item 127 built for `Mcp-Method`/`Mcp-Name` — is extended to this
header too: a present `Mcp-Param-Connection` that disagrees with
`params.arguments.connection` is rejected as `-32020 HeaderMismatch`,
because the `2026-07-28` spec's "Server Validation" MUST-reject rule is
written generically for any header a server mirrors via `x-mcp-header`, not
only `Mcp-Method`/`Mcp-Name`.

**This is still a routing hint, never an authoritative access decision.**
The header/body agreement check stops a caller from lying to the gateway
about which connection its own request targets; it does not, and cannot,
make the gateway's decision correct or complete. Visibility/routing only —
the actual authorization boundary is each tool's own call-time scope and
policy check (`resolve_visible_connection`, `connections/visibility.py`);
this annotation must never become a substitute for that check, the same
posture `mcp/server.py`'s `_SCOPE_GATED_TOOLS` already documents for scoped
tool listing. It is also deliberately **non-exhaustive** in two ways an
operator writing an edge rule needs to know:

- A read's top-level `connection` is not the only connection a request can
  touch — every `JoinSpec` carries its own optional `connection` for a
  same-instance cross-database join (`query_ast/models.py`), resolved at
  schema-validation time against the `join_group` policy rule. No header
  exists for a `JoinSpec`'s connection at all (only the top-level `connection`
  argument is annotated), so a request headed `Mcp-Param-Connection:
  analytics` may still legitimately join `crm` — the header/body agreement
  check above only proves the header didn't lie about the *entry-point*
  connection, not that the query touches nothing else. `join_group` policy is
  what actually bounds cross-connection reach.
- `list_connections`, `list_query_templates`, and `run_query_template` take
  no `connection` argument at all (the last resolves it from the stored
  template server-side), so no `Mcp-Param-Connection` header is ever emitted
  for them. A gateway rule keyed on this header must decide explicitly how to
  treat a header-less `tools/call` for one of these three tools — deny by
  default, or gate on `Mcp-Name` (the tool name) instead — rather than assume
  every request carries the header.

Scope is deliberately narrow: only `connection` is annotated, never a
query/write AST field — mirroring query internals into headers would leak
query semantics to intermediaries and invert the confidentiality posture.

The actual tools, one module per concern:

- `mcp/tools/connections.py` — `list_connections`.
- `mcp/tools/schema.py` — table listing/description tools.
- `mcp/tools/query.py` — `run_structured_queries`, one tool for execute,
  dry-run (`mode="explain"`), and single-or-batch (`queries` is always a
  list) — the MCP equivalent of the REST query endpoints (which stay
  split into `/query`, `/query/explain`, `/query/batch`; OpenAPI can
  share one `$ref`'d schema across endpoints where raw per-tool MCP
  schemas can't, so REST didn't have the duplication problem this merge
  fixes — see TODO.md item 61), calling the same `StructuredQueryService`
  with `surface="mcp"` (REST passes `surface="rest"`) purely so downstream
  logging/audit can tell which transport a request came from — the
  validation/compile/execute pipeline itself doesn't branch on it.
- `mcp/tools/write.py` — `run_structured_writes`, the governed-writes
  equivalent of the read tool above (preview/execute/atomic).
- `mcp/tools/help.py` — the guide/diagnostics tools (see below).
- `mcp/tools/templates.py` — query-template listing/invocation tools.

**The `io.github.agitmit/structured-query-ast` extension (`mcp/extensions.py`,
TODO.md item 131, internal half).** The `2026-07-28` revision's SEP-2133
extensions framework lets a server advertise a reverse-DNS-namespaced,
versioned capability under `ServerCapabilities.extensions`. `mcp/server.py`
passes a `StructuredQueryAstExtension` instance to `MCPServer(extensions=...)`,
which declares the read `StructuredQuery` AST and the write
`Insert`/`Update`/`Delete`/`Upsert` union as a citable JSON-Schema contract —
generated from the live Pydantic models (never hand-written) into
`docs/mcp_extensions/structured_query_ast.schema.json`, regenerated with
`make mcp-extension-schema`. It is purely descriptive: the extension
contributes no tool, resource, or JSON-RPC method, so `tools/call` against
the tools above remains the only way to submit a query or write — see the
Decision Log and `docs/mcp_extensions/structured_query_ast.md`'s "Non-goals"
section for why a namespaced method here would be the same rejection class
as `execute_sql`. External publication of the namespace is a separate,
maintainer-gated decision not made as part of this.

**In-query human approval over MCP (`mcp/elicitation.py`, items 92/93/128).**
When a gated read or write trips the approval gate, `run_structured_queries`/
`run_structured_writes` don't block waiting for a human — the `2026-07-28`
protocol removed server-initiated mid-call requests entirely (Multi
Round-Trip Requests, MRTR). Instead the tool returns `InputRequiredResult`
(one `input_request` per gated item in the batch, keyed `"q0"`/`"w0"` etc.
by position) and the client retries the *identical* tool call carrying
`input_responses`/`request_state`. Both phases reuse
`execution/approval.py`'s existing HMAC-signed token verbatim — no new
crypto: a *pending* token (bound to the item's fingerprint, short TTL)
travels in `request_state`; on retry, the tool re-derives each item's
fingerprint from the *resubmitted* AST and compares it against the pending
token's before ever looking at `input_responses` — the mutation-verified
enforcement point that stops a caller from obtaining approval for a cheap
query and replaying the granted state against an expensive one in the same
batch slot. Only once that comparison passes and the human's response says
`approve: true` does a real grant get minted and handed to
`execute_many`/write `execute_many`'s existing `approval_tokens` map (the
same parameter REST's out-of-band token flow already used). Off by default
(`AppConfig.mcp_elicitation_approval_enabled`) — an elicitation response
carries no authenticated approver identity, so treating it as an approval
is an explicit operator decision that the client's human is trusted.

Every tool is wrapped in `@safe_mcp_tool` (`mcp/exceptions.py`). MCP tool
calls don't have HTTP status codes to raise on failure — a tool has to
*return* something — so `safe_mcp_tool` catches every exception the wrapped
function raises, maps it to a small `MCPErrorResult` (an error code like
`NOT_FOUND`, `FORBIDDEN`, `VALIDATION`, or `INTERNAL`, plus a safe message),
logs it, and returns that instead of raising. This mirrors the REST layer's
`HTTPException` mapping (`api/routes.py`'s `try`/`except` blocks) — same
outcomes, different shape, because that's what each transport's protocol
expects.

**A real gotcha worth knowing if you touch these files:** every tool
module that declares a typed parameter needing resolution — `mcp/tools/
connections.py`, `schema.py`, `query.py`, `write.py`, `help.py`,
`templates.py` — deliberately does **not** start with `from __future__
import annotations`, even though most of the codebase does. Here's why.
With that import, Python stores a function's type annotations as plain
strings instead of live objects (e.g. the annotation `StructuredQuery`
becomes the string `"StructuredQuery"`, to be resolved later, lazily).
`MCPServer` needs to resolve those annotations into real types to build
each tool's input schema, and it does that resolution against the
*wrapping* function's `__globals__` — because `safe_mcp_tool` uses
`functools.wraps`, the object `MCPServer` actually inspects is the wrapper
defined in `mcp/exceptions.py`, not the original tool function defined in,
say, `query.py`. So when `MCPServer` tries to look up the string
`"StructuredQuery"` in that wrapper's global namespace, it's looking in
`mcp/exceptions.py`'s namespace — which never imported `StructuredQuery` —
and resolution fails at server-startup registration time, not at call
time, which makes it a confusing failure to debug if you don't know this is
why. Keeping annotations as real (unstringified) objects in these files
sidesteps the problem entirely, because then there's nothing to resolve. If
you add a new tool module with type annotations that need resolving, either
skip `from __future__ import annotations` there too, or confirm `MCPServer`
can still resolve them.

### The `help/` module — a queryable product guide

**Files:** `src/querygate/help/service.py`, `help/corpus.py`, `help/models.py`

`help/` is not part of the query-execution path at all — it's a small,
self-contained "ask QueryGate about itself" service, exposed over both
transports (`api/help_routes.py` for REST, `mcp/tools/help.py` for MCP) the
same thin-wrapper way as everything else. `GuideService` answers questions
like:

- `search` / `topic` — full-text search over a packaged, versioned corpus of
  guide content, and fetch one topic by id.
- `setup_checklist` — a profile-specific (`local`/`container`/`production`)
  ordered checklist for standing QueryGate up correctly.
- `explain_config_field` — look up what a specific config field
  (`AppConfig`, `ConnectionProfile`, `Policy`, `SchemaCatalog`) means,
  redacting sensitive fields like `api_keys` or `connection_string` rather
  than ever echoing a real value.
- `explain_error` — turn an error code (`VALIDATION`, `POLICY_LIMIT_EXCEEDED`,
  `CONCURRENCY_LIMIT`, etc.) into a plain-language explanation and safe next
  actions — genuinely useful for an agent that just got a rejected query and
  needs to decide what to do next, not just a human reading logs.
- `access_summary` — given a `Principal`, report back what that specific
  caller can actually see and do (their scopes, their visible connections,
  whether they can read/write/reload config) — this is scope-aware, so it
  reuses the exact same `Principal` both transports already produced.
- `redacted_configuration` — a redacted view of the live configuration,
  gated on the `admin:config:read` scope.

The guide content is versioned and checked against the installed
`querygate.__version__` at startup (`get_guide_service` raises if they
don't match) — so the guidance a caller gets back can never quietly drift
out of sync with the code actually running. Search/topic lookups are
intentionally left unauthenticated in REST (`help_routes.py`'s own
comment: "Static product guidance is intentionally public... never touches
deployment-specific state"), while anything that reflects real
configuration or a specific caller's access goes through the same
`Principal`/scope checks as every other privileged endpoint.

> **Naming note:** this in-product `help/` service is sometimes called the
> "product guide" *inside the codebase* (e.g.
> `tests/security/test_product_guide_security.py`) — that's QueryGate's own
> runtime, queryable self-documentation feature, a completely different
> thing from *this* file (`docs/PRODUCT_GUIDE.md`), which is a static,
> human-maintained document. Don't confuse the two when searching the repo.

## Testing, Release & Operations

QueryGate sits between AI agents and real production databases. A bug in the
request pipeline isn't just "the wrong number comes back" — it can mean an
unauthorized query got through, or a shared database got overloaded by a
misbehaving agent. That's why this project puts unusual weight on two kinds
of testing most projects treat as optional: **adversarial security testing**
(does the boundary actually hold when someone tries to break it, not just
when they use it correctly?) and **load/soak testing against a real
database** (do the concurrency and timeout guardrails hold under real
concurrent traffic, not just in a mocked unit test?). The rest of this
section walks through the test suite, the release gates, and what runs in
CI.

### The test suite: three markers, three purposes

Pytest markers are configured in `pyproject.toml` under
`[tool.pytest.ini_options]`. The default run (`poetry run pytest`, or
`make test`) excludes anything marked `real_db` (`addopts = ["-m", "not
real_db"]`), so a contributor can run the full default suite with no
database, no Docker, and no network access.

- **`unit`** (`tests/unit/`, 46 files) — the bulk of the suite. Pure
  in-memory tests: policy validation, schema validation, the compiler, auth,
  the catalog, etc. No real database — see the "Testing gotchas" in
  `CLAUDE.md` for the exact patching seams (e.g. patch
  `schema_validation._load_table` rather than mocking SQLAlchemy internals).
- **`integration`** (`tests/integration/`, 19 files) — exercises more of the
  pipeline together. `test_sqlite_end_to_end.py` is the interesting case
  here: it runs the compiler and executor against a real in-memory SQLite
  engine, so it's a genuine end-to-end test of query execution, even though
  SQLite is never a supported registry dialect for real deployments (Postgres
  and MSSQL only — see `connections/models.py`'s `DatabaseDialect`).
- **`real_db`** — anything needing an actual running database server. This
  is a superset marker: `postgres_live` and `mssql_live` tests are also
  tagged `real_db`, so `make test-real-db` (`pytest -m real_db`) runs both.
  These are excluded from the default suite specifically so `poetry run
  pytest` never requires Docker or a live database.

  `make test-postgres-live` and `make test-mssql-live` are one-command paths:
  each starts the databases it needs (the MSSQL one is behind a compose
  `mssql` profile so a normal `docker compose up` doesn't pull a ~2GB image
  nobody asked for, and it also brings up Postgres because the cross-dialect
  differential suite is marked *both*), seeds them, and runs the suite.

  **Why the real-database tier is not optional for dialect work.** A test that
  asserts *generated SQL text* cannot tell a correct rendering from one that
  merely looks correct — SQLAlchemy renders whatever it is asked to. Item 82
  found it compiling T-SQL-invalid `within_group()` SQL without complaint, and
  item 100 found something subtler: a cast spelling that produced no error at
  all on a real server and instead silently corrupted non-ASCII data. So every
  per-dialect primitive is *executed* against both backends with its value
  asserted, and the two expression suites are parameterized over the compiler's
  own exported set of dialect-routed functions and cast targets — adding one
  without live coverage on both dialects fails a guard test by construction.

### `make test-security` — adversarial boundary suite

**File:** `tests/security/` (`pytest -m security`)

This is a small (3 files) but deliberately hostile suite. Its own docstring
says it best: these tests "model hostile or confused callers rather than
normal product usage." Where a unit test asks "does policy validation work
for a normal query," a security test asks "can a caller combine valid AST
fields in a way that bypasses policy, pulls an undeclared table into the
statement, leaks a backend error message, or evades the response-size
limit?" — the same category of side-door the Core Request Pipeline section
describes for denied columns hidden in `where` clauses. `test_product_guide_
security.py` covers a related but different boundary: making sure live
connection ids and principal names never leak into static help/search
results, and that per-caller results aren't cached across different
principals. This suite exists because for a product whose only job is
*refusing* dangerous or disallowed queries, "does the happy path work" is
necessary but nowhere near sufficient — the tests that matter most are the
ones that try to defeat the boundary on purpose.

### `make test-postgres-live`, `make test-load`, `make test-soak` — real-database guardrail tests

**Files:** `tests/integration/test_postgres_load_guardrails.py`,
`test_postgres_timeout.py`, `test_postgres_schema_discovery.py`,
`test_postgres_cost_estimation.py`; harness described in
`docs/LOAD_TESTING.md`.

- **`make test-postgres-live`** (`pytest -m postgres_live`) runs everything
  that needs a real Postgres server but isn't specifically a concurrency
  load test — schema reflection against a real database, cost estimation,
  and the timeout guardrail (`SET LOCAL statement_timeout` actually cutting
  off a slow query, not just being sent as a string).
- **`make test-load`** (`pytest -m load`, `LOAD_ROUNDS` default 3) is the
  concurrency guardrail proof described in `docs/LOAD_TESTING.md`. It sends
  concurrent structured queries through the real REST application (over
  HTTPX's in-process ASGI transport — no socket, no reverse proxy, but every
  other stage of the pipeline runs for real) and polls Postgres's own
  `pg_stat_activity` table while they execute. That last point matters: it
  measures how many queries are *actually active at the database*, not just
  how many client tasks were launched — a much stronger proof that the
  concurrency-control stage of the request pipeline (stage 4, the semaphore
  described earlier in this guide) is doing its job. Each round fires an
  eight-request burst against a policy cap of two, in both a
  short-wait mode (exactly two succeed, six get a `422` "too many
  concurrent..." rejection) and a long-wait mode (all six queue and
  eventually succeed in waves) — and checks that Postgres never reports more
  than two active probe queries at once. It also proves the caller-facing
  admission controls: `queue_mode=fail_fast` never waits, a caller's own
  shorter `wait_timeout_seconds` is honored over the operator's longer
  ceiling, and successful responses carry the documented
  `X-QueryGate-Admission-*` headers.
- **`make test-soak`** (`SOAK_ROUNDS`, default 100) reruns the exact same
  machine-checked load scenarios many times in a row, to catch flakiness or
  slow resource leaks that a 3-round run wouldn't surface.

The harness creates two throwaway views and one function
(prefixed `querygate_load_`) in the demo database and drops them in fixture
teardown — no production-shaped data involved. It's also scoped to the
default single-process `asyncio.Semaphore` backend; the Redis-backed
distributed limiter (used for multi-replica deployments) has its own
separate tests for cross-instance atomicity and fail-open/fail-closed
behavior.

### Performance benchmark: how much does QueryGate add to a request?

`poetry run querygate-performance-benchmark run` (`make performance-benchmark`,
needs `make compose-up` first) answers the question a design partner asks
before routing real traffic through the gateway: how much latency does this
add, and does it slow down database access? Full methodology in
`docs/business/PERFORMANCE_BENCHMARK.md`; the runner is
`querygate.performance_benchmark`.

It times the same query at three tiers — raw SQL direct against Postgres
(`baseline`), the real `StructuredQueryService` pipeline with no HTTP
(`pipeline`), and a full REST round trip through the real FastAPI app
(`rest`) — across a point lookup, a filtered scan, and a `GROUP BY`
aggregation, against a throwaway table the benchmark seeds and drops itself.
`rest - baseline` is the number a customer means by "overhead"; `pipeline -
baseline` isolates the guardrail/compile cost from HTTP/JSON transport, so a
slow number can be attributed to the right layer instead of guessed at. It's
informational (distributions, not a pass/fail gate) — unlike the security
benchmark, it needs a real database, so it can't run in the default
`pytest -m unit` suite; its own integration test does run automatically in
CI's `postgres-live` job on every push/PR (checking the harness works end to
end, not asserting a specific latency number), while the published figures in
`docs/business/PERFORMANCE_BENCHMARK.md` come from a manual, regenerate-it-
yourself run.

### Load benchmark: does per-request cost hold up under concurrent traffic?

`poetry run querygate-load-benchmark run` (`make load-benchmark`, needs
`make compose-up` first) is the performance benchmark's sibling — the second
half of the same design-partner question ("does this add latency, *and does
it slow down under real traffic, and does adding capacity fix it*?"). Full
methodology in `docs/business/LOAD_BENCHMARK.md`; the runner is
`querygate.load_benchmark`.

It reuses the performance benchmark's seeded table and scenarios, and sweeps
**two dimensions**: worker-process count (default `1, 2, 4`) and concurrency
level (default `1, 5, 15, 30`). For each worker count, a real
`querygate.api.app:app` is launched via real `uvicorn --workers N`, bound to
a real localhost socket — not the in-process `ASGITransport` trick the
single-request benchmark uses — and the full concurrency sweep runs against
it, measuring achieved throughput/latency for `baseline` (raw SQL) vs. `rest`
(a real HTTP round trip) at each combination. It is **not** the same tool as
`make test-load`/`make test-soak` (`docs/LOAD_TESTING.md`), which prove the
concurrency cap's *rejection/queueing* behavior with a deliberately tight
cap — this benchmark raises the cap so it never binds, to measure
performance instead.

**Why a real socket, not `ASGITransport`:** an earlier version reused the
single-request benchmark's in-process transport, which puts the test client
and the measured server in the same process/event loop — at high concurrency
the client's own bookkeeping competes with QueryGate's CPU-bound work for
the same CPU, a confound no real deployment has. Verified directly: the same
sweep run in-process vs. over a real socket showed the same *shape*
(throughput rises, peaks, then falls off — a real single-process CPU
ceiling) but a much worse *magnitude* in-process. Fixed by always driving a
real server process now, with the worker count as a measured sweep dimension
instead of an assumption.

**A result is only as clean as the machine it's measured on** — this is
CPU-bound-throughput measurement, so anything else competing for CPU on the
same host directly steals from the number (confirmed directly while building
this: a clean run showing workers helping was rerun minutes later on the
same machine with unrelated heavy processes now also running, and showed no
improvement at all — same code, different result). Run it on an otherwise
idle machine and treat one run as one sample, not a certified number.

**Every run of either benchmark also writes a fresh, publish-ready Markdown
results snapshot** (`docs/business/LOAD_BENCHMARK_RESULTS.md` /
`PERFORMANCE_BENCHMARK_RESULTS.md`, overwritten each time, `--markdown-out`
to redirect or `--no-markdown` to skip) — data only, no interpretation, so
there's always a shareable artifact ready the moment a run finishes, without
a manual write-up step.

### Semantic-memory benchmark

`poetry run querygate-semantic-memory evaluate` (`make
semantic-memory-evaluate`) runs a fixed, offline benchmark against the
catalog/semantic-memory layer with pass/fail thresholds — a regression gate
to make sure catalog search quality doesn't silently degrade. It's part of
`make release-check` (see below). The catalog system itself is covered in
depth in the Catalog / Semantic Layer section of this guide.

### Formatting and CI

Code style is enforced with **Black** (an opinionated, zero-config Python
formatter — it removes style bikeshedding by only allowing one output
format):

```bash
poetry run black --check src/ tests/ examples/ scripts/   # make format-check — fails if anything is unformatted
poetry run black src/ tests/ examples/ scripts/          # make format — rewrites files in place
```

CI is defined in `.github/workflows/ci.yml` and runs four jobs on every pull
request and every push to `main`:

1. **`test`** — installs dependencies, runs `black --check`, runs the full
   default pytest suite with coverage, validates the bundled example
   connection/policy YAML through the real CLI path, runs the release
   metadata and package-build checks (`scripts/check_release.py`,
   `poetry build`, `scripts/check_release_artifacts.py`), generates the SBOM
   and runs the dependency vulnerability audit
   (`scripts/generate_sbom.py`), validates the production Docker Compose and
   Helm chart configs (`helm lint` + `helm template` with every optional
   feature turned on), and checks they render.
2. **`postgres-live`** — spins up a real Postgres 15.1 service container and
   runs `pytest -m postgres_live` (the timeout and load-guardrail tests)
   against it.
3. **`mssql-live`** — spins up a real MSSQL 2022 service container,
   installs the ODBC driver, sets up the MSSQL test databases, and runs
   `pytest -m mssql_live`.
4. **`docker`** — depends on the `test` job passing, then runs `make
   release-smoke`: builds the production image and executes a real
   structured query against Postgres end to end.

In short: nothing merges to `main` without the full default test suite,
formatting, package/SBOM checks, and both live-database suites passing
independently — and nothing is *released* without also passing the
concurrency/timeout load gate and semantic-memory benchmark (next section).

### Releasing: what has to pass before a shipped release

**File:** `docs/RELEASING.md`

A release is cut with two gates, run in order:

**1. `make release-check`** — the deterministic source/package gate:

- verifies the lock file and release metadata (version numbers in
  `pyproject.toml` and `src/querygate/__init__.py` must match, checked by
  `scripts/check_release.py`, which also rejects generated or legacy
  product files from being packaged);
- runs `make format-check`;
- runs the full default test suite (`make test`);
- validates the bundled example configuration through the installed CLI
  path;
- runs the fixed-threshold offline semantic-memory benchmark and the
  adaptive-learning lifecycle proof;
- builds both the wheel and source distribution into `dist/`;
- inspects the built package for anything that shouldn't ship (generated
  files, secrets, archived docs, test-only files) via
  `scripts/check_release_artifacts.py`;
- generates a **CycloneDX 1.6 SBOM** (Software Bill of Materials — a
  standard, machine-readable manifest of every dependency an artifact
  ships, used for supply-chain auditing) from a throwaway environment built
  from the *locked* dependency set (not whatever an unpinned `pip install`
  would resolve today), and runs `pip-audit` against that same environment.
  Any known vulnerability that isn't a specifically reviewed, justified
  entry in `security/dependency-audit-allowlist.json` **fails the release**
  — deny-by-default, not silently ignored;
- gates the **third-party licence inventory** (`make license-check`,
  `scripts/check_licenses.py`). `docs/THIRD_PARTY_LICENSES.md` records the
  licence of every Python package in `poetry.lock`, split by whether
  QueryGate redistributes it (the `main` group — what the published
  container image contains, and what a `pip install` resolves transitively
  from PyPI, subject to environment markers and pip's own resolution)
  or merely builds and tests with it. Deny-by-default three times over:
  strong copyleft (GPL, AGPL) is blocking in either group and can't be
  waived at all; weak copyleft (MPL, LGPL) needs a specifically reviewed
  entry in `security/copyleft-license-allowlist.json`; and a licence string
  the gate doesn't *recognise* is a failure rather than a guess, so a new
  copyleft dependency can't slip in on a fuzzy match. The same check runs
  in the unit suite, so a `poetry add`/`poetry update` that pulls in a
  copyleft dependency fails at test time, not at release time. Each reviewed
  exception carries a machine-checked `facts` block (which packages require
  it; whether QueryGate declares it directly; whether any module under
  `src/querygate/` imports it) verified against
  the lockfile and the source tree every run, so a waiver can't outlive its
  own premises — but a green gate is still not a settled legal question, and
  every unconfirmed review prints a `NOTICE:` line on each run. `make sbom`
  copies the report into `dist/` under `SHA256SUMS`, so it reaches a consumer
  with the release rather than separately;
- writes `dist/SHA256SUMS` so a downloaded artifact set can be checksummed
  against what CI actually produced.

**2. `make release-smoke`** — the container/infrastructure gate: spins up
an isolated Docker Compose project, waits for Postgres and Redis to report
healthy, builds the production image, starts QueryGate, checks readiness
and connection discovery, and executes one real structured query against
the seeded demo Postgres database — proving the packaged image actually
works end to end, not just that the source tests pass. Its containers and
volumes are torn down on exit either way.

Only after both gates pass does a maintainer tag the release
(`git tag -a v0.1.0 ...`); tags stay local until someone explicitly pushes
them. Rollback policy is simple and deliberate: an already-published tag is
never moved — a bad release gets a *new*, higher version number that goes
through every gate again, not a rewritten tag.

Pushing that tag triggers the release workflow, which builds the image,
Trivy-scans it (deny-by-default, pre-publish), pushes it to **GHCR**, and then
**signs it with Sigstore/cosign** (keyless) and attaches a **SLSA
build-provenance** attestation — both bound to the image digest and verifiable
by a consumer (`cosign verify` / `gh attestation verify`; see
`docs/RELEASING.md`). The SBOM, dependency audit, and checksum steps above are
the phase-1 supply-chain work and run on every `make release-check`;
`make verify-release` checks a downloaded bundle's integrity offline against
`dist/SHA256SUMS`. What remains a maintainer step (`TODO.md` item 30/89 phase 2)
is cutting the *first* signed release (a deliberate tag push, never automatic)
and choosing a Python package-index — the image is the signed distribution
channel today.

### Why so much of this is "adversarial" and "under real load"

Most software can treat "the tests pass" as "the feature works." QueryGate's
entire product is a *refusal* boundary in front of other people's
production databases — its job is to say no to the right things as reliably
as it says yes to the right things, while many different agents share the
same database connection at once. That changes what "well tested" has to
mean:

- A wrong answer in most apps is a bug. A validation gap here can mean a
  caller reads data they were never granted access to — so the security
  suite specifically tries to defeat the boundary (hidden columns in
  `where` clauses, cross-connection joins, leaked internal names) rather
  than just confirming normal requests work.
- A performance bug in most apps is a slow page. An unenforced concurrency
  cap here means one agent (or one bug) can saturate a shared production
  database that other systems depend on — so the load and soak tests
  measure actual active queries at the database itself
  (`pg_stat_activity`), under real concurrent traffic, repeated up to 100
  times, rather than trusting that a mocked semaphore test is representative
  of real behavior.

Testing "does this work" is necessary. For a gateway sitting in front of
real databases, testing "does this correctly refuse to work, under
adversarial input and under real concurrent load" is the harder and more
important bar — and it's the one this test suite is built around.

### Running it highly available (HA / DR)

The Helm chart (`deploy/helm/querygate/`) ships a production HA path, documented
end-to-end in `deploy/HA_DR.md`. Three things make a multi-replica deployment
correct rather than merely running:

- **Zero-downtime rollouts and all-replica config reload.** The deployment uses
  `updateStrategy.maxUnavailable: 0` and stamps a `checksum/config` annotation on
  the pod template, so a `helm upgrade` that changes connections/policy/catalog
  rolls *every* replica one at a time behind the readiness gate — capacity never
  drops, and no replica is left on stale config. This is the multi-replica
  answer to the fact that the governance API's in-process reload only affects
  the single replica that served the request.
- **Honest shared-state boundaries.** The in-flight concurrency cap and the
  per-principal rate/byte *quota* are both true fleet-wide budgets under
  `CONCURRENCY_BACKEND=redis` (the one setting installs the Redis-backed
  concurrency limiter *and* the `RedisQuotaLimiter`, item 50 phase 2). On the
  in-process default each replica enforces its own window, so under N replicas
  both effectively multiply by N. `deploy/HA_DR.md`'s shared-state matrix states
  the per-kind behavior plainly rather than implying a budget QueryGate doesn't
  enforce — the config-governance version store and the persisted audit ledger
  remain genuinely per-replica.
- **DR without an app database.** QueryGate owns no configuration database — its
  durable footprint is config (GitOps-backed), an optional governance PVC, and
  the audit stream (stdout → your log store). Recovery is a redeploy from a
  pinned image digest plus the Git config, so the DR runbook targets an RTO of
  minutes and an RPO of ~zero for config.

Chart HA invariants (the strategy, the change-sensitive checksum, the overlay's
PDB/zone-spread/Redis, the RWX governance PVC) are asserted against a real
`helm template` render in `tests/unit/test_helm_ha_deployment.py`, so the
guarantees above can't silently drift out of the manifests.

## Glossary of Terms

Alphabetical. Each term links back to the section that covers it in depth.

- **AST (Abstract Syntax Tree)** — a structured, tree-shaped description of
  something, as opposed to a flat string. QueryGate's `StructuredQuery` is an
  AST: a JSON object with typed fields, never a SQL string. See
  [What is QueryGate](#what-is-querygate).
- **Audit sink** — where an [audit event](#the-core-request-pipeline) gets
  written (`NullAuditSink` discards it, `JsonlAuditSink` appends it to a
  file). See stage 6 of the [Core Request Pipeline](#the-core-request-pipeline).
- **Bound parameter** — a value passed to a database separately from the SQL
  text itself, instead of being concatenated into the query string. This is
  what makes SQL injection structurally impossible in QueryGate's compiled
  queries. See [Security Model](#security-model).
- **Catalog** — QueryGate's optional, versioned layer of curated business
  metadata (descriptions, aliases, join hints, sensitivity labels) that sits
  on top of raw schema reflection. Purely descriptive — it can never affect
  what a query is allowed to do. See [Catalog / Semantic Layer](#catalog--semantic-layer).
- **Dialect** — which database flavor a connection speaks (`postgresql` or
  `mssql` in production; SQLite internally for tests only). Dialect-specific
  logic (date bucketing, session guardrails) is isolated to single functions
  rather than scattered through the codebase. See [Core Request Pipeline](#the-core-request-pipeline).
- **Draft proposal** — a suggested piece of catalog content (from a manual
  import or the usage-based learner) that is quarantined from real,
  agent-visible catalog entries until a human reviews and publishes it. See
  [Catalog / Semantic Layer](#catalog--semantic-layer).
- **GIL (Global Interpreter Lock)** — Python's mechanism that only lets one
  thread execute Python bytecode at a time. It's what makes a single
  variable reassignment (like swapping in a freshly reloaded config
  registry) atomic/indivisible. See [Connections, Policy & Configuration](#connections-policy--configuration).
- **Hot reload** — re-reading the connections/policy/catalog YAML files from
  disk and swapping them into the running process, without a restart. See
  [Connections, Policy & Configuration](#connections-policy--configuration).
- **JWKS (JSON Web Key Set)** — a small JSON document, published by an
  identity provider, listing the public keys currently used to sign JWTs.
  Lets QueryGate verify tokens without a hardcoded key, and survive key
  rotation without a redeploy. See [Auth & Transports](#auth--transports-rest--mcp).
- **JWT (JSON Web Token)** — a compact, digitally signed token that encodes
  claims (who this is, what they're allowed to do, when it expires). One of
  two supported ways to authenticate to QueryGate (the other is a static API
  key). See [Auth & Transports](#auth--transports-rest--mcp).
- **Knowledge source class** — the catalog's four-tier trust ranking for a
  piece of metadata: `verified` (human-approved) > `observed` (deterministic
  schema fact) > `inferred` (generated suggestion) > `learned` (noticed from
  usage patterns). A lower tier can never silently overwrite a higher one.
  See [Catalog / Semantic Layer](#catalog--semantic-layer).
- **Load benchmark** — `querygate-load-benchmark` / `make load-benchmark`: the
  performance benchmark's sibling, measuring throughput and latency across a
  concurrency sweep (raw SQL vs. full QueryGate REST) against a real
  database — distinct from `make test-load`'s concurrency-cap *correctness*
  check. See [Testing, Release & Operations](#testing-release--operations).
- **MCP (Model Context Protocol)** — the standard protocol AI agents use to
  discover and call a server's tools. One of QueryGate's two transports
  (the other is REST). See [Auth & Transports](#auth--transports-rest--mcp).
- **Performance benchmark** — `querygate-performance-benchmark` /
  `make performance-benchmark`: measures how much latency QueryGate adds to a
  query against a real database, split into raw-SQL baseline, pipeline
  (no HTTP), and full REST tiers. See
  [Testing, Release & Operations](#testing-release--operations).
- **Policy** — the per-connection (optionally per-principal) configuration
  that bounds a query: table/column allow-deny lists, complexity caps (max
  joins, max `where` depth, etc.), mandatory row filters, and which other
  connections it can be joined with. See [Connections, Policy & Configuration](#connections-policy--configuration).
- **Principal** — the representation of an authenticated caller (subject,
  scopes, claims, which auth method produced it), shared identically by both
  REST and MCP. See [Auth & Transports](#auth--transports-rest--mcp).
- **Prompt injection** — malicious or manipulative text hidden in data an AI
  agent processes, worded to look like an instruction, trying to make the
  agent do something its operator never intended. Part of why QueryGate
  can't rely on "the model was told to behave." See [What is QueryGate](#what-is-querygate).
- **Provenance** — the paper trail attached to every catalog entry: which
  knowledge source produced it, evidence pointers, confidence, status, who
  approved it and when. See [Catalog / Semantic Layer](#catalog--semantic-layer).
- **Query builder** — `querygate.client`, a typed, fluent Python API for
  constructing a [`StructuredQuery`](#the-core-request-pipeline) with method
  calls and autocomplete instead of raw JSON. A pure client-side authoring
  convenience: it builds the same models the server validates, so it adds no
  trust and cannot bypass any guardrail. See [Auth & Transports](#auth--transports-rest--mcp).
- **Quarantine** (catalog) — the structural separation between draft
  proposals and real, published catalog entries — a proposal literally
  cannot carry access-affecting fields, and agent-facing lookups never read
  from the proposal list. See [Catalog / Semantic Layer](#catalog--semantic-layer).
- **Reflection** — SQLAlchemy connecting to the real database and reading
  its actual schema (table/column names and types), rather than trusting a
  caller's claim about what exists. See [Core Request Pipeline](#the-core-request-pipeline).
- **SBOM (Software Bill of Materials)** — a standard, machine-readable
  manifest of every dependency a shipped artifact contains, used for
  supply-chain auditing. QueryGate generates one (CycloneDX format) on every
  release. See [Testing, Release & Operations](#testing-release--operations).
- **Schema fingerprint** — a single hash summarizing a connection's entire
  structural schema, so "has anything changed" is one string comparison
  instead of a full re-diff. See [Catalog / Semantic Layer](#catalog--semantic-layer).
- **Scope** — a permission string on a `Principal` (e.g.
  `admin:reload-config`, `catalog:publish`) gating one specific sensitive
  operation. Scopes are deliberately narrow and non-overlapping. See
  [Auth & Transports](#auth--transports-rest--mcp).
- **Semaphore** — a concurrency primitive that only lets N callers hold a
  resource at once; QueryGate uses one per connection to cap simultaneous
  database queries. See [Core Request Pipeline](#the-core-request-pipeline).
- **Session guardrails** — dialect-specific safety settings applied at the
  start of every database session (lock/statement timeouts on Postgres,
  lock timeout + deadlock priority on MSSQL) so one stuck query can't
  degrade the database for everyone else. See [Core Request Pipeline](#the-core-request-pipeline).
- **SQLAlchemy Core** — the lower-level, non-ORM query-building layer of the
  SQLAlchemy Python library. QueryGate compiles every `StructuredQuery` into
  a Core `Select` built from typed Python objects, never a hand-built SQL
  string. See [Core Request Pipeline](#the-core-request-pipeline).
- **Staleness** (catalog) — the state a catalog entry is flagged with when
  the real schema changes underneath it. A stale entry isn't deleted, just
  marked untrustworthy until reviewed; it can't be approved or published
  while stale. See [Catalog / Semantic Layer](#catalog--semantic-layer).
- **StructuredQuery** — the one and only shape a caller can submit to run a
  query: a validated JSON object, never a raw SQL string. See
  [What is QueryGate](#what-is-querygate) and [Security Model](#security-model).
- **Vault (HashiCorp Vault)** — an external secrets-management system
  QueryGate can optionally resolve database credentials from at config-load
  time, instead of a plain environment variable. See
  [Connections, Policy & Configuration](#connections-policy--configuration)
  and [Security Model](#security-model).

## FAQ (for marketing/positioning conversations)

**"Isn't this just a SQL firewall / query sanitizer?"**
No — that's a meaningfully different (and weaker) design. A SQL firewall
accepts a SQL string and tries to detect danger inside it (denylists,
parsing, pattern matching), which is an unbounded arms race against SQL's
own expressiveness (comments, encodings, dialect quirks). QueryGate never
accepts a SQL string in the first place — the only input shape that exists
is a validated `StructuredQuery` JSON object. See
[What is QueryGate](#what-is-querygate) and [Security Model](#security-model).

**"Can the AI agent ever run arbitrary SQL, even in theory?"**
No. There is no field, tool, or endpoint anywhere in the codebase that
accepts a raw SQL string — every Pydantic model in the query AST rejects
unknown fields outright. See [Security Model](#security-model).

**"How is this different from just giving the agent a read-only database
user?"**
A read-only user still lets an agent read every table it can technically
reach, run something expensive enough to degrade the database, and infer
denied values indirectly through clever filtering. QueryGate additionally
enforces per-table/column allow-deny policy, per-query complexity caps,
concurrency limits, and timeouts — and blocks column access even when it's
only used in a `where` clause, not just in `select`. See
[What is QueryGate](#what-is-querygate) and stage 1 of the
[Core Request Pipeline](#the-core-request-pipeline).

**"What databases does it support?"**
Postgres, MSSQL, and MySQL in production, verified against real servers
(item 19 phase 1). Snowflake (item 19 phase 2) and BigQuery (item 19 phase 3)
each have a real `DialectAdapter` (compiling its output against a real,
installed `snowflake.sqlalchemy`/`sqlalchemy_bigquery` dialect object) and a
`SessionDialectAdapter` (tested against recording fakes that assert the
exact SQL it builds, not the real dialect compiler — its statements are
built directly with `sa.text(...)`), but neither is production-ready the
same way as the other three: there is no Snowflake or BigQuery instance to test against in
this project's environment, and neither `snowflake-sqlalchemy`'s nor
`sqlalchemy-bigquery`'s driver has async SQLAlchemy engine support, so
`connections/engine.py` deliberately refuses to open a live connection to
either today rather than connecting unverified — see the Decision Log
entries below and TODO.md item 19's live-verification follow-up items.
SQLite is used only internally for tests and examples — it's never a
supported registry dialect for a real deployment.
See [Core Request Pipeline](#the-core-request-pipeline).

**"How does an agent connect — does it need a special client?"**
Two transports, both backed by the exact same validation/execution pipeline
and the same authentication code: a standard REST API, and an MCP (Model
Context Protocol) server for MCP-capable AI agent frameworks. See
[Auth & Transports](#auth--transports-rest--mcp).

**"Where does it run — is our data sent anywhere?"**
QueryGate is self-hosted: it runs inside your own infrastructure, next to
your database, with reference deployment stacks for Docker Compose and
Helm/Kubernetes (see `README.md`'s "Production deployment" section and
`deploy/`). Credentials and query results don't leave your network as part
of QueryGate's own design.

**"Can it enforce different rules for different databases, or different
callers?"**
Yes. Policy (caps, allow/deny lists, mandatory row filters) is resolved
per connection, and can be further narrowed per authenticated caller
(`principals` in the policy file). See
[Connections, Policy & Configuration](#connections-policy--configuration).

**"What happens when a policy needs to change urgently — do we have to
restart?"**
No. Connections, policy, and catalog config all support an atomic hot
reload from disk — safe for in-flight requests, no downtime. There's also a
governed admin workflow (versioned config history, preview-before-apply,
one-action rollback) on top of that for auditable changes. See
[Connections, Policy & Configuration](#connections-policy--configuration).

**"Does the 'catalog' / semantic layer let an agent search actual row
data?"**
No — this is an explicit, structural boundary. The catalog only ever holds
*descriptions of* tables and columns (purpose, aliases, relationship hints,
sensitivity labels), never row values, and its search/read paths never open
a database session. See [Catalog / Semantic Layer](#catalog--semantic-layer).

**"Is there a human review step before AI-suggested metadata becomes
'official'?"**
Yes, for every source of catalog content, including the usage-based
learning tier. A suggestion is quarantined as a draft proposal — structurally
unable to carry access-affecting fields — until a human explicitly
approves and publishes it through a versioned, rollback-capable governance
workflow. See [Catalog / Semantic Layer](#catalog--semantic-layer).

**"What's out of scope — what does QueryGate *not* protect against?"**
Compromise of the deployment itself (the host, the container, the policy
files, the database credentials, the reverse proxy in front of it) is a
privileged-operator boundary handled by normal infrastructure controls, not
by QueryGate's request-handling code. QueryGate also isn't a substitute for
an external penetration test, and it protects column/table access, not
general statistical inference (a caller authorized to see an aggregate can
sometimes still infer something about individual rows from a narrow enough
filter). See [Security Model](#security-model).

**"How rigorously is all of this actually tested?"**
Beyond a standard unit/integration suite, there's a dedicated adversarial
security suite that specifically tries to defeat the access boundary
(hidden columns, cross-connection joins, leaked internal names), and
load/soak tests that measure actual concurrent query counts at a real
Postgres database (via `pg_stat_activity`), not just a mocked concurrency
limiter. See [Testing, Release & Operations](#testing-release--operations).

**"How do we know the security guarantees actually hold — do you scan for
vulnerabilities, have an SBOM, check dependencies?"**
Yes to all three, and it's continuously enforced rather than a point-in-time
report. Every guarantee is backed by an open-source, deny-by-default check
that runs in CI on every change: static analysis (Bandit + Semgrep), a
dependency-CVE audit of the exact shipped set (pip-audit), a CycloneDX SBOM
per release, container-image scanning (Trivy), full-history secret scanning
(gitleaks), and OpenAPI fuzzing (Schemathesis) — plus two first-party
deny-by-default gates: the adversarial regression suite and a third-party
licence inventory over every locked Python package
(`docs/THIRD_PARTY_LICENSES.md`, `make license-check`). A regression that weakened any of them fails the build.
For a reviewer under NDA, `docs/SECURITY_POSTURE.md` is a reproducible packet
— every claim names the command that reproduces it. The published image is
signed (Sigstore/cosign keyless) with a SLSA build-provenance attestation,
verifiable via `cosign verify` / `gh attestation verify`. We're also precise
about what we *don't* claim: no independent penetration test or formal
certification. See [Security Model](#security-model), section 6.

## Decision Log

Chronological list of notable technical/architectural decisions and the
reasoning behind them, newest first. Added to incrementally as work happens
— see the maintenance protocol above.

- **2026-08-21 — The dependency-licence gate records copyleft findings rather
  than blocking on them, and scopes itself to Python packages.** Ahead of the
  planned source-available licence flip, `make license-check`
  (`scripts/check_licenses.py`) now gates every package in `poetry.lock`
  deny-by-default and emits `docs/THIRD_PARTY_LICENSES.md`. Three choices in it
  are deliberate and non-obvious. **First, the authority is the lockfile, not
  the ambient virtualenv** — for existence *and* for version. Every off-the-shelf
  licence scanner reports whatever happens to be installed; this repo's own
  `.venv` carried a stale `sniffio` that such a tool would have reported as a
  QueryGate dependency, and reading a licence from a release the report does not
  name would hide a relicensing that happened in exactly that bump. This mirrors
  the rule `scripts/generate_sbom.py` already states for the SBOM. **Second,
  strong copyleft (GPL/AGPL) is unwaivable in either group, while weak copyleft
  (MPL/LGPL) passes on a recorded review** — and a recorded review that is still
  a `draft` *passes the gate* while printing a `NOTICE:` on every run. The
  alternative, failing the build until a lawyer answers, would redden every
  unrelated CI run for weeks; the cost is that a green gate is not evidence a
  licence question is closed, which is why the report leads with a count of
  unconfirmed records and why the status row in `docs/SECURITY_POSTURE.md` is
  amber rather than green. Promoting an unconfirmed redistributed finding to a
  hard failure remains available and is the owner's call. What *is* mechanised
  is the factual half: each record carries a `facts` block (its requirers,
  whether QueryGate declares it directly, and whether any `src/querygate/`
  module imports it) checked against the lockfile
  and source tree on every run, so the unverifiable part is confined to the
  legal reading and cannot quietly expand to cover stale evidence. The five
  packages whose licence cannot be read from a local install are re-read from
  PyPI nightly (`--verify-overrides`), so no record rests permanently on a
  hand-typed snapshot; that mode needs network, so it stays out of the
  per-commit gate deliberately. **Third, the inventory
  covers Python packages only.** The published container image also layers a
  Debian userland and Microsoft's `msodbcsql18` under `ACCEPT_EULA=Y`; that is
  stated as an explicit scope limit in the report rather than silently implied
  to be covered, and clearing it is tracked as TODO.md item 196 — needed before
  the first paid pilot's security review, not before the flip. Related: the redistribution
  boundary itself is the image, not the wheel — a wheel declares dependencies
  and contains none of them, which is the premise the one open MPL-2.0 question
  (`certifi`) rests on.
- **2026-08-17 — Expressiveness is for discovery; narrowness is for
  production. QueryGate ships the bridge between them (item 195).** A standing
  tension had gone unresolved in the docs: the read AST is deliberately
  SQL-complete for SELECT (items 99–106), which is a liability in a security
  review — "if the agent can express any SELECT, what did removing the string
  buy me?" — while item 48's curated templates are safe but require the
  operator to *guess the needed shape list up front*, the same thing that makes
  hand-authored tool catalogues wrong. **Decision:** do not narrow the AST, and
  do not leave templates as an unreachable ideal. Ship the path between them:
  `Policy.templates_only` narrows a principal to reviewed templates, and an
  opt-in, redaction-safe recorder answers *which* templates by observing real
  traffic. The pitch becomes "run open in staging, promote the shapes your
  agent actually used, flip to templates-only, and show the audit proving
  nothing else ran" — a story a connector library (which needs the tool list
  authored first) and a semantic layer (which needs a modeling step first)
  structurally cannot tell. **Two constraints kept it on-thesis:** the recorder
  stores a *skeleton* (every literal replaced by a typed parameter slot,
  `intent` dropped), so it inherits the persisted-audit redaction guarantee
  rather than becoming a new place values accumulate; and it **never
  auto-installs** a template — a draft is returned for human review, the same
  quarantined-draft posture catalog governance takes. An auto-promoting
  recorder would have been a policy engine editing its own policy from traffic,
  which the catalog rules already forbid for learned content and which would
  have made the narrowing worthless as evidence. **Rejected alternative:**
  restricting the AST itself (fewer primitives, so the general surface is
  "safe enough" without narrowing) — that trades a real capability every agent
  needs for a guarantee `templates_only` provides exactly, and it would have
  walked back the flagship expressiveness pillar to solve a positioning
  problem. See TODO.md item 195 and
  [`docs/INFERENCE_RISKS.md`](INFERENCE_RISKS.md).

- **2026-08-12 — Two filed items were renumbered 179→184 and 180→185 when two
  concurrent branches were merged; the sole deviation from "item numbers are
  permanent".** Item 177 (WORM integrity counters) and item 179 (cumulative
  disclosure budget) were built in separate worktrees cut from the same base
  commit and neither was merged before the other finished. Each allocated the
  next free numbers against the TODO.md it could see, so **both** filed an item
  179 and an item 180, for four different pieces of work. CLAUDE.md's rule that
  numbers are permanent and never reused cannot hold when two allocations of the
  same number already exist — one pair had to move. **Decision:** the disclosure
  budget keeps 179 (and its deferred approval-escalation follow-up keeps 180),
  because it is shipped code whose number is referenced from roughly thirty
  files including `execution/disclosure_budget.py`, `policy/models.py`,
  `deploy/HA_DR.md`, `docs/INFERENCE_RISKS.md` and its own tests; the item-177
  branch's two were **filed-only** — no code implements them — so moving them
  cost two `metrics.py` comment lines and three archive cross-references. The
  WORM non-advancing-cursor defect is now **item 184** and the unreachable
  `rejected` metric label is now **item 185**; both carry a permanent note of
  their prior number in their TODO.md body, and item 177's archive entry points
  at the new numbers. **The real lesson is the process one:** a worktree that
  files new items allocates numbers against a base that another worktree cannot
  see, so long-lived unmerged branches make collisions structural rather than
  unlucky. Merge (or at least reserve numbers centrally) before filing follow-up
  items from a branch. `docs/STORED_PROCEDURE_CATALOG_PLAN.md`'s four
  *proposed* phase numbers (178–181) were the same trap already latent in the
  tree — they now say "numbered when prioritized" instead of naming a number
  nothing had reserved.
- **2026-08-11 — "Governing intent" resolved as a cumulative disclosure budget,
  and NL-intent enforcement rejected outright (TODO.md item 179).** Asked
  whether QueryGate could govern an agent's *intent* as well as its query, we
  split the word three ways and answered each separately. (1) **Declared
  purpose as a closed-set token** — already shipped as item 145. (2) **The
  natural-language ask** (`StructuredQuery.intent`, free text) — **deliberately
  not governable, and recorded here so it isn't revisited under pressure.**
  Enforcing it means inspecting a string and judging its meaning, which
  contradicts the North Star's "by construction, never by inspecting a string",
  turns a deterministic gate probabilistic, opens a prompt-injection surface
  inside the control plane, and is self-attested by the exact party being
  governed — an agent that would exfiltrate will also write "routine
  reporting". Adding it would need a NORTH_STAR decision, not just this log.
  (3) **Intent as read off the query shape, accumulated over time** — built,
  as item 179.

  **Two things were corrected during the work and are worth preserving.**
  First, the item's own original premise was backwards: it implied budgeting
  *distinct* query shapes. Because `normalize_query_shape` strips predicate
  literals, a differencing probe (`salary > 50000`, `> 60000`, …) is ONE shape
  re-sent N times — so counting distinct shapes would have left the guardrail
  silently inert against the exact attack it exists for. Repetition, not
  variety, is the signal. Second, the "should `explain`/`verdict` spend budget"
  question that blocked the item resolved itself once framed correctly: they
  return no rows, so they disclose nothing a *disclosure* budget could meter.
  No change to `explain`'s deliberate non-quota-gating was needed.

  **Decisions:** two independent caps rather than one (`max_shape_repeats_per_window`
  for the targeted probe, `max_aggregate_queries_per_window` as the blunt
  backstop against a shape-varying prober); key on
  (principal, connection, purpose, table) — purpose in the *key* rather than a
  cap on `PurposePolicyDelta`, which would have been the first subtractive
  field on that delta and would have broken `validate_structural_caps`'
  documented monotonicity argument; principal not actor, since the actor is the
  agent and a fleet shares it; reject on exhaustion rather than escalating into
  item 92's approval gate (deferred as item 180, to be decided against real
  usage data rather than guessed at, and because "click here to buy unlimited
  disclosure" is a real failure mode); and both caps inert unless
  `min_group_size` is also set, since with no k-floor there is nothing to
  difference around. The Redis backend **fails closed**, diverging from the
  quota/concurrency limiters — those are anti-abuse controls where admitting
  traffic is the right degradation, whereas failing open here would silently
  suspend a privacy guarantee the operator's config still claimed.

  **Stated as a bound, not a closure, everywhere it is documented.** A caller
  inside its budget still differences successfully, the literal-free shape
  cannot distinguish a probe from an innocent repeat, and no threshold is
  recommended because none has been calibrated against real traffic.
  `docs/INFERENCE_RISKS.md` R3 and `THREAT_MODEL.md` QG-29 were updated to say
  "bounded since item 179, still residual" rather than "closed".

- **2026-08-12 — A hash-verified WORM record whose `seq` is type-confused is
  REJECTED as a chain break, not accepted at its coerced value (TODO.md item
  178).** `verify_envelope_hash` validates a line through
  `LedgerRecord.model_validate` before computing the digest, and pydantic's lax
  coercion accepts `"3"`, `3.0` and `true` for an `int` field — so the digest is
  computed over the *coerced* value and a crafted line can be perfectly
  self-consistent while still holding a non-integer raw position. Reading that
  raw value back and doing `seq == prev_seq + 1` on it failed in two different
  ways, and only one of them was the crash: a **string** made `prev_seq + 1`
  raise `TypeError` out of the scan on any cursor-resumed page, masked as a
  generic HTTP 500 (availability), while a **float or boolean** raised nothing
  and was silently accepted as a valid link — `1 == 0.0 + 1` and
  `1 == False + 1` are both true — so an attacker-retyped chain position passed
  verification and the record was returned as genuine (integrity, the more
  serious of the two).
  **Decision:** narrow the type at the reader (`_chain_seq`) and treat a
  non-integer as an ordinary chain break — counted in `unverified` and
  `chain_breaks`, that segment's scan stopped — rather than reading the coerced
  `LedgerRecord.seq`, which would have silently normalised a value an attacker
  deliberately retyped. This is **deliberately stricter than
  `audit/ledger.py`'s `verify_chain`**, which still accepts the coerced value:
  the S3 archive is writable by anyone holding `s3:PutObject` (necessarily
  including QueryGate's own role) while the local ledger file is not, and
  `verify_chain` returns a typed failure rather than raising, so it has no
  crash to avoid. The asymmetry is intentional and recorded at both ends. It
  cannot false-positive: the archival writer emits `seq` through
  `LedgerRecord.model_dump_json()`, now asserted per line by a test. **What
  this deliberately does NOT claim:** the reader still has three unhandled-
  exception paths for other crafted-or-corrupt line shapes (item 194), one
  reachable by ordinary corruption — so "a bad line is always counted, never
  raised" is a goal, not yet a guarantee.

- **2026-08-11 — The WORM archive's two integrity signals are now Prometheus
  counters, so a broken hash chain is alertable rather than merely
  inspectable (TODO.md item 177).** Item 172 added
  `WormSearchResult.chain_breaks` — the strongest tamper/omission signal this
  surface produces, stronger than an ordinary `unverified` hash mismatch
  because the record's own hash still verified — but it was only ever visible
  to whoever happened to run an ad-hoc
  `GET /api/v1/admin/observability/worm-search` over the right window.
  Nobody was paged. Since the Proof pillar is a product claim, that made
  "detected" mean "detectable on demand" rather than "monitored": an operator
  on the normal dashboards/alerts posture would never learn a chain broke.
  **Decision:** `querygate_audit_worm_search_chain_breaks_total` and
  `querygate_audit_worm_search_unverified_total` in `metrics.py`, incremented
  in `audit/worm_search.py`'s `_finalize` alongside the existing
  `AUDIT_WORM_SEARCH_REQUESTS_TOTAL`/`..._OBJECTS_SCANNED_TOTAL` pattern.
  Both are **unlabelled**, matching item 126's
  `PERSONAL_DENIALS_RATE_LIMITED_TOTAL` precedent — a `connection`/
  `principal` label here would be caller-chosen cardinality and a weak
  activity oracle over the audit archive itself, on a surface whose whole
  point is that reading it is privileged. `unverified_total` deliberately
  includes each chain break's own breaking line, mirroring the result model's
  field relationship exactly, so `unverified_total - chain_breaks_total` is
  the count actually likely to be a key rotation — the same split the
  response `note` already makes in prose.
  **Two decisions beyond the item's literal scope, both deliberate:**
  (1) the counters also fire on the **mid-scan S3 failure** path, not only
  the served path — a chain break found just before S3 died is a real
  discovery, and letting the exception swallow it would reproduce the exact
  silent-signal failure this item exists to close; the item's own text said
  only "in `_finalize`", which would have left that hole. (2) An idempotence
  guard drafted for the two call sites was **removed** rather than shipped:
  mutation testing confirmed it was unreachable (every `_finalize` call site
  is a `return`, and `_finalize` cannot raise after counting) and therefore
  untestable — the same call item 114 made on its fifth hand-rolled predicate
  enumerator. The reasoning is recorded in a comment at the site, and the
  invariant it rested on is now pinned *behaviourally* by
  `test_a_result_that_fails_to_build_counts_the_signals_once_not_twice`, which
  makes the result-model construction raise and asserts the signals are counted
  once, not twice. (A first attempt guarded this by string-matching `_finalize`
  call sites in the module source; the audit's second pass showed that proxy
  false-passed on the one edit that genuinely double-counts — moving the
  counting call above the model construction — and false-failed on a docstring
  mentioning `_finalize(`, so it was replaced. The behavioural test also covers
  the WS-7 "build the response first, count only after construction succeeds"
  ordering, comment-only since item 134 phase 2.)
  **Scope corrected by this item's own `auditors` gate, before commit.**
  Reviewers independently flagged that the first draft's framing —
  "detected" becomes "monitored" — overstated what shipped: nothing in
  QueryGate scans the archive on a schedule (`search_worm_archive` has exactly
  one production caller, the scope-gated REST route), so the counters only
  advance while a search actually runs. A segment tampered inside a window
  nobody searches still produces no signal. What this item delivers is
  precisely that a finding becomes *reachable by alerting* rather than only
  readable in a response body; genuine monitoring additionally requires the
  operator to schedule a periodic search, which README/THREAT_MODEL now say.
  A false sense of coverage would have been worse than the gap it replaced, so
  the claim was narrowed on every surface rather than the code widened. The
  same gate also established that the counters count findings **per scan**,
  not distinct segments (a WORM object is immutable, so a real break is
  permanent and re-counted by every later search that reaches it) — hence the
  guidance to alert on the first non-zero increase, not on a magnitude. A
  scheduled verifier, if wanted, is a new item rather than a doc edit.
  See [Security Model](#security-model).
- **2026-08-10 — `validate_schema` reuses the per-request connection-resolver
  snapshot instead of re-deriving cross-connection resolution a second time
  (TODO.md item 173).** Item 160 already built one fixed snapshot per
  request (`_validate_and_compile`'s `connection_resolver`, via
  `_snapshot_connection_resolver`) and threaded it into `validate_policy`
  and `compile_structured_query` — but `validate_schema`'s own internal
  `resolve_query_table_connections` walk had no way to receive it, so it
  always fell through to a fresh live `resolve_visible_connection` lookup
  for every non-primary connection a query's joins touch. Not a correctness
  bug (each resolution was independently correct, and TOCTOU-safe within
  its own call), just the same connection/Policy resolution work redone a
  second time in one request — surfaced 2026-08-09 by
  `security-invariant-reviewer` auditing item 160's own commit as a
  performance follow-up, deliberately not folded into that item. **Decision
  (recorded per this item's own scoping requirement):** thread the existing
  snapshot through rather than add a second, parallel memoization
  mechanism (a per-call dict cache keyed by `(connection_id, id(principal))`
  was the item's other named option) — the snapshot already exists, is
  already proven correct and TOCTOU-safe by item 160's own tests, and a
  second cache would be a second solution to the same problem the codebase
  already solved once. **Fix:** `validate_schema` gained an optional
  `connection_resolver` parameter, threaded to both of its own internal
  `resolve_query_table_connections` calls; `execution/service.py`'s
  `_validate_and_compile` now passes its already-built `connection_resolver`
  into its `validate_schema` call, so both the CTE-body and outer/nested-scope
  resolution walks reuse the snapshot. `None` (every other caller —
  `admin/service.py`'s template-binding validator, and every existing test)
  falls back to the exact pre-173 live-resolution behavior; strictly
  additive. `tests/unit/test_schema_validation.py`'s
  `test_connection_resolver_snapshot_is_reused_not_rederived` patches the
  live `resolve_visible_connection` to raise if called at all, proving both
  the primary and joined connection resolve through the supplied snapshot
  exclusively — mutation-verified (reverting the threading makes the test
  fail on the patched-to-raise live lookup).
- **2026-08-10 — The WORM archive reader now verifies a segment's internal
  chain linkage, not just each record's own hash (TODO.md item 172).**
  Item 154 made `audit/worm_search.py` verify each `LedgerRecord`'s own hash
  before returning it, closing the gap where a fabricated, schema-valid
  segment could be planted and returned indistinguishably from a genuine
  one — but it never checked that a segment's records form a genuine,
  complete chain. Against even a KEYED archive (where forging new content
  is infeasible without the HMAC key), a principal with `s3:PutObject` on
  the archive prefix could still upload an object containing an arbitrary
  SUBSET of a genuine segment's records (drop the first, or one from the
  middle) — each surviving record still verified individually, so the
  omission produced no signal. Surfaced 2026-08-09 by
  `security-invariant-reviewer`/`test-contract-reviewer` auditing item 154's
  own commit. **Decision (this item's own first step, per its TODO.md
  scoping):** implement linkage-break detection now; defer the harder,
  separate problem — a genuine segment copied WHOLESALE to a second S3 key,
  undetectable by any linkage check since a full copy is itself a valid
  chain — to a future item, since closing it needs binding a segment to its
  own object key (a write-format change with the same "explicit decision
  before implementation" shape item 154's own two decisions had), not
  something to default into alongside this item's narrower, well-specified
  fix. **Fix:** `search_worm_archive`'s per-object line loop now tracks
  `prev_verified_hash`/`prev_seq` across consumed lines, scoped to one
  object (each WORM segment restarts its own chain at
  `seq=0`/`GENESIS_PREV_HASH` — a per-segment, not cross-segment, design).
  The first line actually CONSUMED when starting fresh at an object
  (`consume_from == 0`) must be the genuine genesis; every subsequent
  consumed line must continue `seq`/`prev_hash` from the one before it. On
  a break, the breaking line is counted `unverified` (still envelope-shaped
  and self-consistent — just not linked) plus a new, distinct
  `WormSearchResult.chain_breaks` field (a stronger signal than an ordinary
  hash mismatch, which is more often a key rotation than tampering), and the
  scan stops consuming that object entirely — deliberately routed around the
  existing per-object line-cap truncation path, since a cursor resuming into
  an already-broken chain would just re-encounter the identical break.

  **Hardened same-day by this item's own mandatory `security-invariant-reviewer`
  gate**, which found the first cut's resumed-page handling genuinely
  weaker than claimed: a cursor resuming mid-object (`consume_from > 0`,
  which the SERVER itself issues at every ordinary page boundary, not just
  a hand-crafted cursor) unconditionally accepted the incoming link of the
  first record consumed on that page — the Decision Log's own draft
  reasoning claimed this "mirrors `audit/ledger.py`'s `verify_chain`
  carve-out for a rotated ledger file," which does not hold: `verify_chain`'s
  carve-out exists because a rotated file's true predecessor lives in a
  DIFFERENT file the verifier doesn't have, whereas here the whole object
  (predecessor line included) was already fetched into memory. **Fix:**
  before the line loop, when `consume_from > 0`, seed `prev_verified_hash`/
  `prev_seq` from the nearest preceding non-blank line that itself verifies
  — so the first record consumed on ANY page, resumed or not, has its
  incoming link checked like every other record; the accept-as-given
  carve-out now applies only in the one case it's actually unavoidable (the
  predecessor line itself didn't verify). Also found and fixed: a broken
  chain silently suppressed every remaining line in that object with no
  disclosure of HOW MANY — added `WormSearchResult.
  lines_skipped_after_chain_break`, summed across every broken object in
  the scan and named in the response `note`; and the pre-existing
  key-rotation sentence was miscounting a chain-break line (whose hash DID
  verify) as a hash-mismatch candidate — the note now separates
  `hash_mismatches = unverified - chain_breaks` for that sentence and gives
  the chain-break sentence its own, explicitly-not-a-key-mismatch wording.
  Also documented, not fixed (a genuinely separate residual, not swept into
  this item): records dropped from the TAIL (not interior) of a segment
  leave a self-contained valid prefix chain with nothing to fail a
  continuity check against — unlike `verify_chain`'s `expected_head`
  parameter, no per-segment head anchor exists outside the object to
  compare against; pinned by a dedicated test asserting the current
  non-detection so a future fix updates it deliberately rather than it
  silently starting to pass.

  **A second review pass on this same-day hardening caught one more
  issue in the fix itself: it introduced a crash.** The resumed-page
  seeding loop indexed `lines[back]` from `consume_from - 1` with no upper
  bound against the object's actual current length — but `consume_from` is
  a cursor's own `line` field, which this module's cursor contract already
  treats as untrustworthy (a hand-edited cursor, or one whose target
  object was legitimately replaced by a shorter body since issuance,
  reaches the same unbounded index). Clamped to
  `min(consume_from, len(lines))` so an out-of-range offset degrades
  (nothing to seed from) instead of raising `IndexError`. See
  [Security Model](#security-model).
- **2026-08-10 — The audit windowed early-exit (item 141) now discloses
  itself on the report, instead of looking identical to a genuinely complete
  scan (TODO.md item 171).** Item 141 added `max_consecutive_out_of_window`
  as a heuristic early-exit to the three tail-first audit readers
  (`admin/anomaly.py`, `admin/config_trends.py`, `help/personal_denials.py`,
  the last reusing the first's reader) — once enough consecutive
  matching-type lines are all before the report's window start, the scan
  stops on the assumption physical write order tracks `occurred_at` order,
  an assumption a merged/restored/multi-writer audit file can violate (see
  `docs/THREAT_MODEL.md` QG-43). Surfaced 2026-08-08 by three of the four
  `auditors` reviewers auditing item 141's own commit: the stop didn't set
  `truncated`, so a caller-facing consumer had no way to distinguish "no
  more matches exist" from "the scan gave up early on a heuristic." **Fix:**
  `AuditEventSource.load_query_events`/`ChangeEventSource.load_change_events`
  widened from a 3-tuple `(events, malformed, truncated)` to a 4-tuple
  `(events, malformed, truncated, scan_ended_on_out_of_window_run)` —
  mechanical but wide, touching both `Jsonl*EventSource` implementations,
  both `build_*_report` functions, and ~20 call sites across
  `tests/unit/test_anomaly.py`, `tests/unit/test_config_trends.py`, and
  `tests/unit/test_personal_denials.py`, exactly as item 141's own
  deferral scoped it. `AnomalyReport`, `ConfigCatalogChangeTrend`, and
  `RecentDenialsReport` each gained the matching
  `scan_ended_on_out_of_window_run: bool` field, deliberately independent of
  `truncated` (a resource-bound stop and a heuristic stop are different
  facts, and either can fire without the other — both directions are
  regression-tested). See
  [Security Model](#security-model).
- **2026-08-10 — A cross-connection join's secondary-connection schema
  qualifier now dispatches through the registered dialect, instead of a
  hardcoded MSSQL idiom (TODO.md item 174).** `validation/schema_validation.py`'s
  `_load_table` reflects a joined table on a SECONDARY connection through the
  PRIMARY connection's own engine (see item 163/170's entries below for why),
  and used to qualify it with an unconditional `f"{db_name}.dbo"` — T-SQL's
  three-part naming, hardcoded regardless of the secondary's actual dialect.
  Surfaced 2026-08-09 by `security-invariant-reviewer` auditing item 166's own
  commit: literal CLAUDE.md non-negotiable #6 territory (dialect differences
  behind a Protocol + one class per variant + registry, never an inline
  assumption at a call site) — and there wasn't even an `if dialect == ...`
  branch, just the bare MSSQL idiom applied unconditionally. A cross-connection
  join whose secondary is Postgres or MySQL reflected under a schema qualifier
  neither dialect understands, failing closed with a masked `NoSuchTableError`
  that named neither the real cause nor the dialect — a robustness/clarity
  gap, not a policy bypass (no data ever reached the caller), but it meant
  only an MSSQL secondary had ever actually worked in this configuration,
  silently. **Fix:** `connections/dialects.py`'s `SessionDialectAdapter` gained
  `cross_database_schema_qualifier(db_name) -> Optional[str]` (default `None`
  — Postgres has no true cross-database reference without an extension, MySQL's
  is a bare `<db>.<table>` with no schema segment, so guessing at either would
  be exactly the "synthesize structure the caller never asked for" the engine
  philosophy rules out); `MSSQLSessionAdapter` overrides it with the real
  `<db>.dbo.<table>` naming. `_load_table` now dispatches through
  `get_session_adapter(...)` and raises `QueryValidationError` naming the
  dialect when the adapter returns `None`, the same reject-don't-emulate
  posture item 74 set for MSSQL's missing `NULLS FIRST/LAST`, rather than
  guessing at a convention that varies per dialect. **`QueryValidationError`
  chosen over `ConfigValidationError`, reviewed and confirmed reasonable but
  a nit (2026-08-10 `architecture-boundary-reviewer`):** `resolve_query_table_
  connections`'s sibling checks (`is_connectable`, host-mismatch) use
  `ConfigValidationError` for a deployment-wide fact true of every query
  against that secondary; this one instead follows the item-74/`array_agg`
  "dialect lacks a requested capability" precedent from
  `compiler/dialect_adapters.py`. Both types currently map to the same HTTP
  422 and no caller distinguishes them, so this is a documented judgment
  call, not left ambiguous. See
  [Why policy is per-connection, not global](#why-policy-is-per-connection-not-global).
- **2026-08-10 — `GET /help/my-recent-denials` now carries a per-principal
  cooldown (TODO.md item 126).** This is the only self-service (no
  `admin:observability:read`) surface that triggers `admin/anomaly.py`'s
  `JsonlAuditEventSource`'s O(file-size) scan of the persisted audit JSONL —
  every other reader of that source requires the admin scope. Surfaced
  2026-07-30 as consistent with (not worse than) the rest of the codebase's
  no-REST-rate-limiting posture, and deliberately left open pending a
  throttling-design decision rather than fixed unprompted. **Decision:** add
  `AppConfig.personal_denials_cooldown_seconds` (default 5s, `0` disables) and
  a `PersonalDenialsCooldown` tracker (`help/personal_denials.py`) — a plain
  closure-scoped `Dict[principal_id, float]`, mirroring item 43's
  `HealthMonitor._last_manual_test` cooldown shape but keyed by principal
  instead of connection. A second call within the window gets `429` with
  `Retry-After`. Kept as one router-closure instance (not a module-global
  singleton) so each app build — including every test's own `create_app()` —
  starts with a clean cooldown, the same reasoning `tests/conftest.py`'s
  `in_process_limiter().clear()` gotcha documents for other in-process state,
  without needing a new autouse-fixture reset.

  **Hardened same-day by this item's own mandatory `security-invariant-reviewer`
  gate**, which found the first cut real but under-disclosed and under-tested:
  the cooldown is genuinely per-WORKER-PROCESS (undisclosed — `num_of_workers
  > 1`/multiple replicas multiply the effective ceiling, the same limitation
  `execution/quota.py`'s in-process limiter already documents for the
  identical shape) and its bucket is `principal.subject`, which every
  statically configured API key shares one of (docs/THREAT_MODEL.md QG-33
  already documents the identical coupling for this endpoint's *data*
  isolation — extended to note it now also covers the *rate limit*). Neither
  is a bypass — both only make the limit stronger or more coarse, never
  weaker — but both needed writing down, not just holding informally. Also
  found and fixed: the cooldown map grew one permanent entry per distinct
  principal ever seen with no eviction (`record_request` now prunes every
  entry whose own cooldown has already elapsed on each call — behavior-
  preserving, since an expired entry is semantically identical to an absent
  one); and the 429 path was invisible to observability (added
  `querygate_personal_denials_rate_limited_total`, a single unlabeled
  counter — deliberately a metric, not a persisted audit event, since an
  audit event per 429 would let a caller inflate the very file this endpoint
  scans, a self-amplifying feedback loop). `tests/unit/test_personal_denials.py`
  now exercises `PersonalDenialsCooldown` directly (first-call-allowed,
  second-blocked, per-principal isolation, zero disables, pruning), and
  `test_personal_denials_api.py` gained a shared-API-key-shares-one-bucket
  regression test pinning the documented QG-33 coupling.
- **2026-08-09 — a `join_group` spanning different physical hosts is now
  rejected, both at config-load time and (the load-bearing layer) at request
  time (TODO.md item 170).** Cross-connection joins reflect the joined table
  through the PRIMARY connection's own engine, under a same-instance
  cross-database schema qualifier — an assumption that two connections
  sharing a `join_group` are the same physical server instance, never
  genuinely separate hosts, that nothing previously checked. **Decision:
  validate, don't just document.** The first draft (`ConnectionRegistry.
  from_entries` comparing `ConnectionProfile.join_group` members' `(host,
  port)`) was found incomplete by this item's own `auditors` gate
  (security-invariant-reviewer and architecture-boundary-reviewer,
  independently): the join_group actually consulted at request time is
  `policy.join_group or profile.effective_join_group()`, and a
  `Policy.join_group` override (default, per-connection, or per-principal)
  can unite two connections invisibly to a config-load-only check, since
  `Policy` lives in a separate file. `validation/schema_validation.py`'s
  `resolve_query_table_connections` — the one place that sees the
  fully-resolved, possibly-per-principal value — now carries the same host
  check too; the config-load-time layer stays as a fast, common-case
  backstop, not the sole enforcement point. Both layers share one function,
  `connections/models.py`'s `connection_host_port`, which never returns the
  full connection string. The same audit pass also found and fixed two
  narrower gaps in that function: an un-encoded `@` in a password used to
  bleed into the parsed "host" (SQLAlchemy splits userinfo at the FIRST `@`,
  so the rejection message could carry a password fragment — now stripped to
  the LAST `@`), and a driver that packs the real host into a `?host=` query
  parameter instead of the URL's own host component used to silently escape
  the comparison — now falls back to the query parameter. Full write-up,
  including the corrected (and previously-wrong) comparison to item 158's
  own scoping, is in `docs/TODO_ARCHIVE.md` (item 170). Mutation-verified,
  each enforcement point independently.

- **2026-08-09 — a cross-connection self-join now reflects each alias
  against its own declared connection, instead of both silently collapsing
  onto whichever alias reflected first (TODO.md item 166).** A self-join
  (the same physical table name declared twice) where the join names a
  DIFFERENT `connection` than the primary is a shape `StructuredQuery`
  already permits — `_validate_table_aliases` only requires an alias per
  repeated name, not that both occurrences share a connection. But
  `_reflect_and_validate_scope`'s `physical_tables` reflection memo
  (`validation/schema_validation.py`) was keyed by the casefolded physical
  name alone, so both aliases shared one memo slot: whichever alias's name
  won the `needed` set's iteration order reflected the table once against
  its own connection, and the other alias silently reused that same
  `sa.Table` object — compiling and executing against the wrong connection
  despite its own declared one. **Not merely a coin flip** (found by this
  item's own `security-invariant-reviewer` audit pass): the caller chooses
  both alias strings and can observe from the returned data which direction
  the race went, so it was adaptively steerable; the exploitable direction
  is the PRIMARY alias losing its own slot, since it then silently read
  data reflected under the SECONDARY connection's schema while its masks/
  filters/deny-list still resolved against only the PRIMARY's Policy — see
  the full write-up in `docs/TODO_ARCHIVE.md` (item 166) for the other
  direction's weaker, over-enforcing failure mode. **Decision: fix the memo
  key, don't reject the shape** — key `physical_tables` by `(table_cx,
  physical_key)` instead of `physical_key` alone, so two aliases only share
  a reflection when they actually resolve to the same connection
  (byte-identical for every single-connection query, including a
  same-connection self-join). Chosen over rejecting cross-connection
  self-joins outright because the AST already allows the shape and it has a
  legitimate use (the same table name existing on two different databases)
  — narrowing what a caller can express because one code path handled it
  incorrectly runs against the "expose primitives, don't spoon-feed"
  posture the engine otherwise holds. One disclosed side effect: a
  cross-connection self-join against a non-MSSQL secondary now fails
  deterministically instead of racing between a masked failure and a silent
  wrong-connection success, since the secondary alias's reflection always
  runs into the pre-existing MSSQL-only `.dbo` schema-qualifier gap (tracked
  separately as TODO.md item 174, not introduced or closed by this item).
  Mutation-verified: reverting the key change makes the regression test fail
  with `assert 1 == 2` (only one reflection recorded instead of two); the
  test also asserts each alias binds to its OWN connection's reflected
  `sa.Table` (not just that both `_load_table` calls happened), closing a
  gap the same audit pass found in the first version of the test.

- **2026-08-09 — `validate_policy`/`compile_structured_query`/
  `applied_column_masks` now self-derive `scope_connections` from a given
  `connection_resolver`, closing item 160's finding 3 footgun (TODO.md item
  160, fully closed).** Every `scope_connections`/`connection_resolver`
  parameter these three functions gained under item 156 defaulted to `None`
  — meaning a caller that passed a `connection_resolver` (able to resolve
  cross-connection joins) but forgot the explicit `scope_connections` map
  silently fell back to primary-only enforcement, with no error or warning.
  This is the exact shape that let `admin/service.py`'s
  `simulate_candidate_policy` drift onto the weak path before item 156
  caught it, and item 160's own audit-response pass (2026-08-07) deliberately
  left the "should we self-derive" question open as a maintainer decision
  rather than defaulting into it under time pressure. **Decision: yes,
  self-derive**, made only after auditing every call site that exists today:
  `execution/service.py` and `admin/service.py` (the two production callers)
  and every internal recursive compiler call (subquery, EXISTS, CTE) already
  pass `scope_connections` and `connection_resolver` together, via item 160
  finding 1's snapshot; the three benchmark-harness callers
  (`security_benchmark.py` x2, `catalog/adaptive_learning_benchmark.py`) pass
  neither. So the fix is a no-op for every caller in the codebase today — it
  only protects a future one from repeating the exact mistake item 156 fixed.
  Implementation calls the existing `resolve_scope_connections`
  (`validation/schema_validation.py`) when `scope_connections is None and
  connection_resolver is not None` (plus `connection_id is not None` for the
  two compiler functions, which allow an unset connection_id).
  In `validate_policy`, this runs AFTER `validate_structural_caps`,
  preserving finding 2's cheap-bound-first ordering, since
  `resolve_scope_connections` touches the connection registry/policy store.
  Mutation-verified in all three functions.

- **2026-08-09 — WORM archive segments are now enveloped and per-segment
  hash-chained, closing `docs/THREAT_MODEL.md` QG-40's residual (TODO.md
  item 154).** The local hash-chained sink (item 91) wraps every event in a
  `LedgerRecord` a reader can verify; the WORM sink (item 134) wrote the bare
  event body instead, so a principal with `s3:PutObject` on the archive
  prefix — necessarily including QueryGate's own AWS role — could plant a
  fabricated, schema-valid segment the search reader would return
  indistinguishably from a genuine one. Two decisions were needed before
  building, made explicitly rather than assumed: **(1) per-segment chain,
  not cross-segment** — each flushed batch gets its own chain restarting at
  `GENESIS_PREV_HASH`/seq 0, rather than one continuous chain across every
  segment ever written. This fully answers the actual threat (was this
  segment altered after being written) without `WormFlushMonitor` needing to
  persist/recover chain state across restarts or coordinate a single writer
  across replicas — the same complexity a cross-segment design would add
  that item 141 already flagged as disproportionate for a comparable case.
  The trade-off is real and stated, not hidden: a per-segment chain cannot
  prove no segment was ever silently withheld from a result, only that a
  *returned* segment wasn't altered — a different, lower-severity residual
  left to the existing S3-listing/Object-Lock posture. **(2) No
  legacy-segment tolerance** — the reader now requires an envelope
  unconditionally (a bare line is `malformed`, not accepted as a weaker
  historical shape), because this feature has no production deployment
  predating the fix; the "support both shapes indefinitely" alternative
  would have added permanent reader complexity for a migration case that
  doesn't exist. Implementation: `audit/worm_sink.py`'s `_build_segment_body`
  chains `make_record` calls per drained batch; `audit/worm_search.py`
  verifies each record's hash before unwrapping, using the same
  `AUDIT_LEDGER_HMAC_KEY` the local chain/reader use.
  **Corrected the same day, by this item's own `auditors` gate**: the first
  version of this entry and of `docs/THREAT_MODEL.md`'s QG-40 row claimed
  the gap was closed unconditionally. It is not — SHA-256 (the default,
  unkeyed chain) is a public function, so a principal with `s3:PutObject`
  can still forge a passing segment when `AUDIT_LEDGER_HMAC_KEY` is unset;
  only a KEYED (HMAC) chain makes forgery infeasible, the same distinction
  `audit/ledger.py`'s docstring already draws for the local ledger — except
  the WORM chain has no externally-anchored head hash to fall back on for
  the unkeyed case, since each segment restarts at genesis. `app.py` now
  logs a startup warning when WORM is configured without a key. Two further
  residuals recorded rather than glossed over: the reader verifies each
  RECORD's hash but never the chain's LINKAGE within a segment, so a
  genuine segment can be duplicated to a second key (returned twice) or
  have interior records dropped without detection (TODO.md item 172,
  deliberately out of this item's scope); and rotating the HMAC key makes
  every previously-archived segment fail verification on the next search —
  now surfaced as a distinct `unverified` count (not lumped into
  `malformed`) so the response discloses a likely key mismatch rather than
  returning a silently-empty, "nothing happened"-looking result. See
  [Audit logging](#6-audit-logging--every-attempt-always).

- **2026-08-08 — Accepted a windowed early-exit for the tail-first audit
  readers (`admin/anomaly.py`, `admin/config_trends.py`), after first closing
  the dominant source of the ordering risk it depends on (TODO.md item
  141).** Both readers scan the audit log tail-first (newest line first) and,
  before this item, always read until a hard line/byte cap fired — correct,
  but wasteful on a long-running deployment's history once the caller's
  requested time window has clearly been left behind. The item as originally
  scoped proposed trading a *bounded chance of silently missing a handful of
  borderline events* for that speed, because `occurred_at` was stamped at
  event **construction** time, before an arbitrary amount of intervening work
  (structured logging, and any future code added between construction and
  the write) — so under concurrent load, two events' physical write order
  and their `occurred_at` order were not strictly guaranteed to agree.
  Investigating whether that risk could be closed outright (not just bounded)
  found the real fix: `audit/logger.py` now re-stamps `occurred_at` inside a
  new `_persist` helper, immediately before calling the sink's `emit()` —
  removing the dominant, effectively-unbounded-under-load source of drift
  (the gap between construction and the write). A fully unconditional
  version of this — moving the stamp into the sink's own `emit()` under its
  write lock, so *no* caller could ever see a different value — was
  considered and rejected: the audit test suite (and any future
  backfill/import tooling) legitimately calls `sink.emit()` directly with a
  synthetic, controlled `occurred_at` to seed historical fixtures, and
  forcing the sink to always overwrite it would silently break that
  capability. `_persist` stamps `occurred_at` **before** calling `emit()`, not
  inside the sink's write lock, so the residual is bounded by however long a
  competing writer holds that lock (`os.open`/`os.write`/optional `os.fsync`),
  not sub-microsecond jitter as an earlier draft of this entry and the
  in-code comments claimed — corrected 2026-08-08 by the `auditors` gate on
  this same item, independently caught by three of its four reviewers. It is
  negligible in practice today only because every production `audit_*` caller
  runs synchronously on the single asyncio event loop with no `await` between
  event construction and `_persist`, not because of anything the tolerance
  itself guarantees. **Decision:** keep a `max_consecutive_out_of_window`
  tolerance (default 5,000, counted only across the reader's own matching
  event type) as defense-in-depth against that residual, rather than claiming
  a mathematical guarantee the multi-process case (an operator setting
  `num_of_workers > 1` in one pod/replica, sharing one `AUDIT_JSONL_PATH`
  across independent OS processes with no cross-process lock) can't actually
  back. That multi-worker/single-ledger-file configuration is a pre-existing,
  undocumented gap (not introduced or worsened by this item), recorded as
  `docs/THREAT_MODEL.md` QG-43 alongside the early-exit's own residual (a
  merged/restored/multi-writer audit file can make this heuristic return a
  confidently-wrong, undisclosed `truncated=False`); the operator-facing
  tuning knob for the tolerance and the fuller fix (a disclosure field
  threaded through both reports plus `help/personal_denials.py`, which
  independently inherits the same gap) are tracked as TODO.md item 171,
  deliberately not folded into this item's already-committed scope. See
  [Audit logging](#6-audit-logging--every-attempt-always) and
  [the Core Request Pipeline](#the-core-request-pipeline).

- **2026-08-07 — A correlated subquery's `correlate` reference now resolves
  through the same shared, case-insensitive table lookup everywhere, closing
  a real cross-tenant `EXISTS`/scalar-subquery bypass (TODO.md item 169).**
  This is a sibling of the item 167 bug just below: when a query joins a
  table under one alias (say `"C"`) but also references it elsewhere with
  different letter-casing (`"c"`), schema validation can end up holding two
  separate Python objects for what should be one alias. Item 167 closed the
  consumer of that duplication inside the compiler's mandatory-row-filter
  step; this item found and closed a second, independent consumer: a child
  `EXISTS`/scalar subquery's `correlate` field, which tells the compiler
  which outer-query table the subquery should stay tied to. Reproducing the
  exact shape (a joined `customers` table with a `mandatory_row_filter` on
  `tenant_id`, and a child `EXISTS` correlating against the differently-cased
  spelling) compiled to a subquery that silently dropped the correlation
  entirely and scanned `customers` as an independent, unconditioned join —
  a genuine cross-tenant boolean oracle a caller could use to test row
  existence outside their own tenant's mandatory filter, not a hypothetical.
  **Fix:** the same case-insensitive `_table_by_name` helper the compiler's
  FROM/JOIN construction already used was relocated into
  `validation/schema_validation.py` (as `table_by_name`/
  `table_by_name_or_none`) so both the compiler and the schema validator
  resolve every alias through the identical function over the identical
  dict, instead of two independently-written lookups that could silently
  drift apart. While verifying the fix, a second, related bug in the same
  code region was found and closed in the same change: a child subquery's
  own body referencing a correlated table with different case than its
  `correlate` declaration used raised an unhandled `KeyError` instead of a
  clean validation error (a crash, not a bypass, but the same root
  duplication). The deeper structural option — preventing the duplicate
  alias objects from ever being created in the first place — was
  considered and deliberately not taken, for the same reason item 167 didn't
  take it: it touches a function every query in the engine runs through, and
  the narrower fix (routing every consumer through one shared lookup) fully
  closes this class of bug for both known consumers today. The residual risk
  is a future third consumer that indexes the table dict directly instead of
  through the shared helper — worth remembering if you ever add a new place
  that reads schema validation's resolved table map. See
  [The Core Request Pipeline](#the-core-request-pipeline) and
  [3. Compilation](#3-compilation--ast--policy-becomes-real-sql).

- **2026-08-07 — Extended item 165's structural credential-safety fix to the
  `querygate-validate-config` CLI and the config-governance dry-run path,
  closing the one gap item 165 itself flagged as a follow-up (TODO.md item
  168).** Item 165 (below) stopped the REST `/admin/reload-config` endpoint
  from ever stringifying a raw pydantic/YAML exception into an HTTP error
  body. But `cli.py`'s `load_config_context` — shared by the CLI and by the
  admin config-governance "validate"/"preview" dry-run endpoints — still
  built its own error messages the old, unsafe way, relying on a downstream
  regex scrub (`_humanize_validation_errors`) that only ever ran on the HTTP
  path, not on the CLI's own terminal/CI-log output. A malformed
  `connections.yaml` carrying a real credential could still leak it straight
  into an operator's terminal or CI log. **Fix:** the two structural helpers
  item 165 introduced (`safe_pydantic_error_lines`, `safe_yaml_error_detail`)
  moved to the neutral `core/exceptions.py` (both `cli.py` and
  `admin/service.py` needed to import them, and `admin/service.py` already
  imports from `cli.py`, so a shared home avoided a circular import), and
  `load_config_context` now routes every one of its four file loads through
  them instead of `f"{file}: {exc}"`. See
  [2. Credential redaction is a tested invariant, not a habit](#2-credential-redaction-is-a-tested-invariant-not-a-habit).

- **2026-08-07 — Closed the `is_connectable()` gap for the SECONDARY
  connection in a cross-connection join (TODO.md item 163).** Some dialects
  (Snowflake, BigQuery) support policy/rendering but can't actually be
  connected to yet (`connections/engine.py` deliberately refuses to open a
  live connection for them — see the BigQuery entry just below). That guard
  was only ever checked for a query's *primary* connection. A join whose
  *secondary* connection was one of these not-yet-connectable dialects
  wasn't rejected up front the way the same attempt on a primary connection
  already was — it instead failed later, deep in reflection, as an opaque,
  masked `NoSuchTableError` (a raw 500, not a clean validation error). No
  data ever leaked and no check was skipped (the join's `join_group`
  membership and the joined connection's own policy were both still
  enforced) — this was purely a confusing-error problem, not a security
  bypass, but a bad one: an operator debugging a rejected join got no signal
  about why. **Fix:** the same `is_connectable()` check `init_engine` already
  runs for a primary connection now also runs for a join's secondary
  connection, raising the identical, clearly-worded rejection. See
  [Why policy is per-connection, not global](#why-policy-is-per-connection-not-global).

- **2026-08-07 — Made `column_mask`'s HASH branch an explicit, exhaustive
  check on all five `DialectAdapter`s, instead of an implicit "falls through
  to HASH" default (TODO.md item 164).** Every dialect adapter's column-mask
  renderer already treats an unrecognized date-part or interval unit as a
  hard rejection (a deliberate exhaustiveness habit, so a future enum member
  is a forced decision rather than a silent guess) — but the same discipline
  wasn't applied to `ColumnMaskKind`: an unrecognized mask kind silently
  rendered as the strongest mask (HASH) instead of raising. Not a live
  disclosure risk today (`ColumnMaskKind` is a closed, stable four-member
  enum, and "fails toward the strongest mask" never under-masks a column) —
  but a gap worth closing on principle before a future fifth mask kind makes
  it a real one. **Fix:** all five adapters
  (Postgres/MSSQL/MySQL/Snowflake/BigQuery) now check for `HASH` explicitly
  and raise a typed `QueryValidationError` for anything else, matching the
  same pattern already used for date/interval maps.

- **2026-08-07 — A case-different column reference to a joined alias could
  leave a phantom second copy of that table, which a mandatory row filter
  then turned into either an unconditioned cross join (duplicated rows) or,
  under a different internal ordering, a silent bypass of the filter itself
  (TODO.md item 167).** If a query joins a table under one alias spelling
  (say `orders AS "O"`) but a column elsewhere in the query refers to it with
  different letter-casing (`"o.id"`), schema validation's internal bookkeeping
  ends up with two distinct table objects for what should be a single join
  occurrence — one for `"O"`, one for the phantom `"o"`. The compiler's
  mandatory-row-filter step used to walk *every* such name it found, not just
  the ones the query's actual `FROM`/`JOIN` clause declared. Compiling the
  exact shape confirmed real, measurable damage: the filter's `WHERE` clause
  ended up applied to both the real alias and the phantom one, and because the
  phantom alias was never part of the statement's own `FROM`/`JOIN`, SQL
  silently added it as an unconditioned cartesian join — multiplying every
  output row. A first, narrower fix (deduping by name alone) was caught by
  review as *still* wrong under the opposite internal ordering: it could bind
  the filter to the phantom object while the alias actually used in the query
  went completely unfiltered — a real mandatory-row-filter bypass, worse than
  the row-duplication bug it was meant to fix. **Fix, in two parts, both
  required:** the mandatory-filter step now walks only the table names the
  query itself declared in `FROM`/`JOIN` (never the extra casing variants),
  and resolves each one through the same case-insensitive lookup the
  `FROM`/`JOIN` clause construction already uses — guaranteeing the filter
  always binds to the exact object that ends up in the compiled statement,
  regardless of internal ordering. (Item 169, above, found and closed a
  second, independent consumer of this same underlying duplication.) See
  [3. Compilation](#3-compilation--ast--policy-becomes-real-sql).

- **2026-08-07 — `/admin/reload-config`'s generic error handler could leak a
  live credential in its HTTP 400 response body; fixed with a structural
  guarantee, not a string scrub (TODO.md item 165).** The endpoint's
  exception handling built its error message from Python's default
  stringification of a validation failure — and pydantic's default rendering
  of certain error kinds includes the *entire offending input*, connection
  string and password included, truncated but often not enough to hide a
  short host/password. A syntactically broken `connections.yaml` could leak
  the same way through a YAML parser error, which embeds the literal
  offending source line. Reachable by anyone holding the config-reload admin
  scope. **Fix:** two dedicated exception handlers, ahead of the generic
  catch-all, that build the error text from only the safe, structured parts
  of the failure (field name and message — never the raw input value), so
  the credential is never materialized into a string in the first place
  rather than being scrubbed out after the fact. Both new regression tests
  were confirmed to fail with the real credential visible when reverted to
  the old behavior, then the fix restored — the standard mutation-verify
  discipline for a new enforcement point. See
  [2. Credential redaction is a tested invariant, not a habit](#2-credential-redaction-is-a-tested-invariant-not-a-habit).

- **2026-08-07 — `ConnectionProfile.dialect` is now validated against
  `connection_string`'s actual backend, and the fix itself is a case study
  in why a whole-model Pydantic validator is the wrong tool near a
  credential field (TODO.md item 158).** Nothing previously checked that
  the declared `dialect` enum agreed with the backend named in
  `connection_string`'s URL scheme — a `dialect: postgresql` profile
  pointed at a real `mysql+asyncmy://...` string was accepted, and every
  place that branches on the *declared* dialect rather than the live engine
  would act on the wrong assumption. `connections/engine.py`'s
  `session_scope` applies session guardrails (`SET LOCAL statement_timeout`,
  etc.) keyed on the declared dialect, so a misdeclared profile's guardrail
  SQL silently doesn't apply (`create_async_engine` still resolves the
  actual driver from the URL regardless of what's declared, so the wire
  protocol itself is always correct — but the guardrail is not). More
  concretely security-relevant: `schema/reflection.py`'s
  `list_live_tables()` selects its extra `INFORMATION_SCHEMA.TABLES` filter
  via `SessionDialectAdapter.list_live_tables_extra_filter_sql()`, keyed on
  the same declared dialect — MySQL's adapter appends `AND TABLE_SCHEMA =
  DATABASE()` specifically because MySQL's `INFORMATION_SCHEMA.TABLES` is
  server-wide (spans every database the connecting user can see), unlike
  Postgres/MSSQL. A profile misdeclared `postgresql` over a real MySQL
  backend would skip that filter (Postgres's adapter contributes none),
  letting `list_tables()` leak *other* databases' table names to a caller
  whose connection was only supposed to see one. Item 158 closes that
  specific cross-database leak, not just the guardrail-text mismatch. It
  also meant item 19 phase 2's Snowflake `is_connectable()` guard, which
  keys on the declared dialect, couldn't be trusted to catch a misdeclared
  Snowflake connection.

  **Fix:** a `field_validator("dialect")` on `ConnectionProfile`
  (`connections/models.py`) that parses `connection_string` with
  `sa.engine.url.make_url(...).get_backend_name()` and rejects a mismatch,
  naming both the declared dialect and the actual backend found.

  **Why a field validator on `dialect`, not a whole-model validator or a
  field validator on `connection_string` — the credential-leak catch.**
  `ConnectionProfile`'s own docstring says its `connection_string` must
  never end up "embedded in a validation error." A first draft used
  `@model_validator(mode="after")`, and a direct check against a live
  `pydantic.ValidationError` showed that when a model-level validator
  raises `ValueError`, pydantic populates the error's `input_value` — in
  both `str(ValidationError)` and `.errors()[0]["input"]` — with the
  **entire raw input dict**, `connection_string` included. `api/routes.py`'s
  `reload_config_endpoint` does `except Exception as exc: raise
  HTTPException(..., detail=str(exc))`, so this would have handed a live
  credential straight back in an HTTP 400 body the first time someone
  reloaded a mismatched config. Scoping to `field_validator
  ("connection_string")` instead doesn't fix it either — that field's own
  raw value *is* the credential, so `input_value` is the leak. The actual
  fix: reorder `ConnectionProfile`'s fields so `connection_string` is
  declared before `dialect`, and scope the validator to
  `field_validator("dialect")`, reading `connection_string` back out via
  `info.data` (already-validated, since it's declared earlier). `dialect`'s
  own value (e.g. `"postgresql"`) is all that ever lands in `input_value`
  on rejection. If you touch this validator, keep it scoped to a field
  whose own raw value is never the credential — don't "simplify" it back
  into a model-level check.

  **The "scheme is always literal" assumption in the item's own write-up
  turned out to be false**, caught by running the full suite, not asserted
  from the spec. Two integration tests failed:
  `test_mcp_server.py::test_mcp_configuration_inspection_uses_request_application_config`
  and `test_product_guide_api.py::test_configuration_summary_is_scope_gated_and_never_returns_raw_yaml`.
  Both go through `help/service.py`'s `_redacted_connection`, which
  deliberately builds a `ConnectionProfile` straight from a stored config
  version's raw, never-interpolated YAML — it only wants typed
  `dialect`/`id`/`known_tables`/`join_group` for an admin summary and never
  resolves the real secret. `examples/connections.example.yaml` (the
  default `connections_file`) confirmed the *whole* `connection_string`
  value, not just its credentials, is commonly one `${VAR}`-shaped
  reference with no literal scheme present at all. The validator now skips
  the check (doesn't reject) when `make_url` can't parse a backend out of
  the string at all — there's nothing to compare against, and it's not a
  live bypass: `ConnectionRegistry.from_entries`, the one path that
  actually opens a connection, always interpolates `connection_string`
  before constructing `ConnectionProfile`, so the real check still runs
  against the fully-resolved URL by the time a connection is opened.

  **Two more real gaps surfaced by the `auditors` review of this same
  change, both fixed before landing.** `make_url` doesn't only raise
  `sa.exc.ArgumentError` on an unparseable string — a *templated port*
  (`...@${DB_HOST}:${DB_PORT}/...`) raises a plain `ValueError` while
  coercing the port to an int, which the original `except
  sa.exc.ArgumentError:` missed, so a legitimately-templated profile could
  have been wrongly rejected; fixed by widening the except clause to catch
  both. And `_BACKEND_NAME_TO_DIALECT` (hand-maintained on purpose, so an
  unsupported backend gets a clear rejection instead of a bare enum error)
  had nothing forcing it to stay a superset of `DatabaseDialect`, so a
  future fifth dialect added without a matching entry would silently
  mis-reject every profile for it; fixed with a module-level `assert` that
  fails at import time instead. A separate reviewer also found the
  "interpolate-then-validate" claim two paragraphs up was asserted in prose
  but never pinned by a test that actually goes through
  `ConnectionRegistry.from_entries` — every test up to that point exercised
  `ConnectionProfile` directly with already-resolved literals. Fixed with
  `test_from_entries_rejects_a_dialect_mismatch_only_visible_after_interpolation`
  in `tests/unit/test_connections_registry.py`.

- **2026-08-07 — BigQuery ships as a rendering-level-only phase (TODO.md item
  19 phase 3), following the Snowflake precedent's exact shape;
  `connections/engine.py` refuses to actually open a BigQuery connection.**
  `DatabaseDialect` gained a `bigquery` member with a real
  `BigQueryDialectAdapter` (`compiler/dialect_adapters.py`) and
  `BigQuerySessionAdapter` (`connections/dialects.py`), covering the same
  primitive surface the other four dialects do — native `NULLS FIRST/LAST`,
  sample-statistic `STDDEV`/`VARIANCE` (BigQuery's docs: both are documented
  aliases of the `_SAMP` forms, matching Postgres/MSSQL/Snowflake, unlike
  MySQL's population-default bare names), `STRING_AGG`, a genuine `ARRAY_AGG`
  (BigQuery has a real native `ARRAY<T>` type), full ROWS/RANGE window frames
  with numeric offsets, and distinct-only `INTERSECT`/`EXCEPT` (rejected with
  `ALL`, the MSSQL/MySQL/Snowflake posture; `UNION`/`UNION ALL` are fully
  supported — confirmed by compiling against the installed dialect object
  that a bare `sa.union(...)` already renders `UNION DISTINCT`, not a bare
  `UNION`, so the shared set-operation helper needed no BigQuery-specific
  handling for that part). `PERCENTILE_CONT` is rejected, not emulated:
  BigQuery's own syntax requires `OVER(...)` as part of the grammar (a
  navigation/window function, not a `GROUP BY`-compatible aggregate) — the
  same genuine gap as MSSQL's, documented as such in Google's docs rather
  than assumed. Upsert is rejected, not emulated, for a reason stronger than
  Snowflake's: BigQuery's only upsert idiom is `MERGE`, a multi-clause
  statement with no single-target-constraint model the way
  `conflict_columns`/`update_columns` express one, AND (confirmed against
  Google's docs) BigQuery's `PRIMARY KEY`/`UNIQUE` constraints, even when
  declared, are explicitly documented as **NOT ENFORCED** (query-optimizer
  hints only) — so there is no database-enforced uniqueness for
  `conflict_columns` to even name a real target against, a structurally
  wider gap than Snowflake's.

  **The one genuinely new mechanism this dialect needed, with no precedent
  in the other four adapters.** BigQuery, uniquely, has no single
  polymorphic date-truncate/date-add function — it has THREE strictly-typed
  ones (`DATE_TRUNC`/`DATETIME_TRUNC`/`TIMESTAMP_TRUNC`,
  `DATE_ADD`/`DATETIME_ADD`/`TIMESTAMP_ADD`), each accepting only its own
  operand type and each with a DIFFERENT unit/date_part vocabulary (confirmed
  against Google's docs, not assumed): `DATE_ADD` supports only
  day/week/month/quarter/year (a `DATE` has no time-of-day); `TIMESTAMP_ADD`
  supports only sub-day units, day/hour/minute/second (a `TIMESTAMP` is a
  timezone-independent absolute instant, and BigQuery does not allow
  calendar-relative arithmetic directly on one); `DATETIME_ADD` supports the
  full range (a civil calendar value with both a date and a time-of-day).
  `BigQueryDialectAdapter.date_bucket`/`date_add` dispatch on the operand's
  SQLAlchemy Core `.type` for this reason (`_bq_temporal_kind`), and reject —
  rather than guess — when that type cannot be determined. This surfaced a
  genuinely new, honest product fact during this item's own integration into
  `test_date_primitives.py`'s shared cross-dialect suite (the same suite
  Snowflake's phase 2 did NOT get folded into, an architecture-boundary-
  reviewer finding on that item this one does not repeat):
  `date_add(now(), 'year', ...)` — a composition `NowExpr`'s own docstring
  recommends — does NOT render on BigQuery, because `now()` renders as a
  `TIMESTAMP` and `TIMESTAMP_ADD` genuinely lacks `week`/`month`/`year`. An
  agent that needs calendar-unit arithmetic relative to "now" on BigQuery
  must compose it from primitives QueryGate already exposes (cast the
  `now()` expression, or reference a `DATETIME`-typed column directly)
  rather than the engine silently picking a different function on the
  caller's behalf — the same "reject and point at primitives, don't
  synthesize" posture item 74 established.

  **The deliberate exception to "add a dialect, register it, done": engine
  wiring stops short of a live connection, for TWO independent reasons.**
  First, the one Snowflake already established: `sqlalchemy_bigquery`'s
  DBAPI has no async SQLAlchemy engine support — the same gap Snowflake's
  driver has. Confirming it the SIMPLE way Snowflake's was confirmed (just
  constructing an async engine and reading the resulting
  `InvalidRequestError`) is not possible with no GCP credentials configured
  in this project's environment, because of the second, genuinely-new-to-
  BigQuery reason: `sqlalchemy_bigquery.BigQueryDialect.create_connect_args`
  builds a real `google.cloud.bigquery.Client` (resolving Google
  credentials) at ENGINE-CONSTRUCTION time, not connection time, and fails
  there FIRST — confirmed directly by reading the installed package's
  source and by observation: with no credentials configured, constructing
  SQLAlchemy's async engine factory against a `bigquery://` URL raises
  `google.auth.exceptions.DefaultCredentialsError` immediately inside
  `create_connect_args`, before SQLAlchemy's own async-driver check ever
  gets a chance to run. (With a syntactically valid, if fake, credentials
  file supplied to get past that step, the identical `InvalidRequestError`
  Snowflake's driver raises — "The asyncio extension requires an async
  driver to be used. The loaded 'bigquery' is not async." — was also
  confirmed for BigQuery's driver, but that is not the error either dialect
  actually presents in this project's normal, credential-less environment.)
  `connections/engine.py`'s `init_engine`
  dispatches the refusal through `SessionDialectAdapter.is_connectable()`
  (the same registered-capability seam Snowflake's phase established, per
  its own 2026-08-06 architecture-boundary-reviewer finding — reused here,
  not reinvented) and raises a clear, client-actionable
  `ConfigValidationError` (422) naming the gap, pre-empting BOTH failure
  modes before either can surface as a confusing library-internal error.
  This also means none of `BigQuerySessionAdapter`'s methods are reachable
  in production — and unlike Snowflake, BigQuery genuinely has no session-
  scoped SQL statement at all to send even if it were reachable (no SET/
  ALTER SESSION equivalent — a BigQuery "connection" issues independent,
  stateless query jobs against Google's REST API), so
  `apply_session_guardrails` is a real no-op rather than a rendered
  statement, and `capture_session_identifier`/`cancel_session` raise rather
  than fabricate a plausible-looking session identifier that was never
  created — a genuine additional architecture gap beyond the missing async
  driver, which the BigQuery live-verification follow-up item now also
  tracks (BigQuery's real timeout/cancellation primitives are per-query-job,
  `QueryJobConfig.job_timeout_ms`/`jobs.cancel`, a materially different
  mechanism than every other adapter's session-level one).

  **Nothing here is live-verified, the identical posture to Snowflake's
  phase, not MySQL's item-19-phase-1 precedent.** There is no BigQuery
  project or GCP credentials available in this project's environment,
  unlike Postgres/MySQL/MSSQL which run in Docker — every BigQuery claim in
  this change is backed by Google's public SQL reference docs and checked by
  compiling against a real, installed `sqlalchemy_bigquery` dialect object
  (`BigQueryDialectAdapter`, `tests/unit/test_dialect_adapters.py`'s
  `TestBigQuery*` classes) or against recording fakes asserting exact SQL
  text/params (`BigQuerySessionAdapter`, `tests/unit/test_dialects.py`'s
  BigQuery session-adapter section) — the same two-different-ways split
  Snowflake's entry above documents, for the same reason. Neither path is
  ever run against a live server. Treat every BigQuery behavior claim in
  this codebase as "renders/compiles as documented", not "confirmed correct
  against a real project", until the live-verification follow-up item ships.
  Item 19, which originally scoped "MySQL, Snowflake, BigQuery, …", is now
  marked done: all three explicitly-named dialects have shipped (one fully
  live-verified, two rendering-only and explicitly flagged as such); a new
  open-ended item now separately tracks "further dialects beyond these
  three" so the item doesn't stay open indefinitely on the strength of its
  own trailing "…".

- **2026-08-07 — TODO.md item 160 (item 156's own follow-up audit): two of
  its four findings fixed, one left OPEN as an explicit maintainer decision,
  one confirmed as already-accurate scope documentation. Item 160 is
  therefore `✅ DONE (findings 1, 2, 4 addressed; finding 3 remains an open
  design decision)`, not archived — see TODO.md's "partially-done items stay
  inline" rule.**

  **Finding 1 (fixed) — audit-vs-compiled-SQL Policy snapshot consistency.**
  `resolve_table_policies`'s default resolver used to re-read the joined
  connection's Policy from the live `PolicyStore` at every call site
  (`validate_policy`, `compile_structured_query`, and again at `execute()`'s
  post-execution `applied_column_masks` audit call) instead of resolving it
  once and threading the same snapshot through, the way the primary Policy
  already did via `_get_policy()`. `execution/service.py` gained
  `StructuredQueryService._snapshot_connection_resolver`: given
  `_validate_and_compile`'s already-computed `scope_connections` map, it
  resolves each DISTINCT non-primary connection's Policy exactly once and
  returns a `ConnectionResolver` closure over that fixed dict, threaded into
  `validate_policy`, `compile_structured_query`, and the `applied_column_masks`
  audit call — all three now agree with each other and with what was
  actually compiled, even if an authorized `/admin/reload-config` changes the
  joined connection's Policy while the query is executing. Mutation-verified:
  dropping the audit call's `connection_resolver=connection_resolver` makes
  `test_audit_masked_columns_reflects_the_snapshot_compiled_against_not_a_late_reload`
  (`tests/unit/test_service.py`) fail exactly as expected — the audit event's
  `masked_columns` reports the post-reload (unmasked) view instead of the
  compiled one.

  **Finding 2 (fixed, via extraction rather than relocation) — cap-ordering
  restored.** `_validate_and_compile` used to compute `early_scope_connections`
  (one `PolicyStore.get()` per cross-connection scope) BEFORE `validate_policy`
  ran at all, ahead of the cheap `max_cte_count`/`max_subquery_depth` checks
  the item's own docstring documents as deliberately cheap-bound-first. Fixed
  by extracting those two checks into a new `validate_structural_caps(query,
  policy)` in `validation/policy_validation.py`, called directly from
  `_validate_and_compile` BEFORE `resolve_scope_connections` runs; `validate_
  policy` also calls it internally afterward (unchanged behavior for every
  OTHER caller). The item offered two options — move the map computation
  inside `validate_policy` itself, or extract-and-call-early — and this
  chose extraction deliberately over literally moving `resolve_scope_
  connections`'s call inside `validate_policy`: doing that would have meant
  `validate_policy` unconditionally self-deriving `scope_connections`
  whenever a caller passes `connection_resolver`/`principal` but no explicit
  map, which is finding 3's exact open question — extraction achieves the
  same cap-before-lookup ordering without deciding it. Mutation-verified:
  reordering the extracted call to run AFTER `resolve_scope_connections`
  again makes `test_structural_caps_reject_before_any_per_scope_policy_lookup`
  (`tests/unit/test_service.py`) fail — the test asserts, via a patched
  `resolve_scope_connections` spy, that it is never called at all when the
  cheap cap alone is enough to reject.

  **Same-day correction (`security-invariant-reviewer`, 2026-08-07): the
  redundant second structural-caps check is NOT guaranteed to agree with the
  first, and an earlier version of this entry (and of `validate_
  structural_caps`'s own docstring) claimed otherwise.** `max_cte_count`/
  `max_subquery_depth` themselves genuinely never move under purpose
  narrowing, but `validate_structural_caps` also runs `_validate_cte_
  constraints`'s cte-name-shadowing and masked-cte-projection rules, which
  DO read `denied_tables`/`denied_columns`/`mandatory_row_filters`/
  `column_masks` — fields `Policy.for_purpose` narrows. Since `_validate_
  and_compile`'s pre-check runs against the UN-narrowed Policy and `validate_
  policy`'s own internal call runs AFTER narrowing, a purpose delta that adds
  a new denied-table/mask entry can make the pre-check pass a query the
  narrowed internal call correctly rejects. This is safe, not a bypass,
  ONLY because `Policy.for_purpose` is additive-only (verified: every field
  it touches is unioned/concatenated onto the base `Policy`, never replaced
  or removed) — the un-narrowed pre-check's governed-name set is therefore
  always a SUBSET of the narrowed one's, so the two calls can under-reject
  relative to each other but never over-reject, and `validate_policy`'s own
  call (the actual authority, run unconditionally on every code path) never
  misses an enforcement because of it. Pinned by a new test,
  `test_structural_caps_pre_check_can_under_reject_a_purpose_narrowed_cte_
  shadow_but_validate_policy_still_catches_it`
  (`tests/unit/test_policy_validation.py`), which constructs exactly that
  disagreement (a cte named after a table only a purpose delta denies) and
  asserts the pre-check alone passes while `validate_policy` still rejects.
  If `PurposePolicyDelta` ever gains a subtractive field this monotonicity
  argument breaks, and the redundant internal call in `validate_policy` must
  never be removed on the assumption the pre-check makes it redundant.

  **Second same-day correction (`security-invariant-reviewer`, 2026-08-07):
  `_snapshot_connection_resolver`'s closure silently discarded its own
  `principal` argument.** An earlier version always answered from the
  snapshot resolved against `self._principal`, regardless of what `principal`
  the `ConnectionResolver` contract's caller actually passed in — not
  reachable through today's production call graph (every in-pipeline caller
  passes `self._principal`), but this REPLACED a default resolver that DID
  honor its `principal` argument, so a future caller threading a different
  principal (item 90's delegated/on-behalf-of resolution, an admin dry-run
  reusing the service, a per-scope principal) would have silently received a
  stranger's cached per-principal Policy override with no error. Fixed: the
  closure now serves the snapshot only when the caller's `principal` is
  identical to `self._principal`; any other principal falls through to a
  live `resolve_visible_connection` resolved against that actual principal.
  Pinned by `test_snapshot_connection_resolver_honors_a_different_principal_
  argument` (`tests/unit/test_service.py`), mutation-verified against the
  prior (unconditional-cache) version.

  **Finding 3 (left OPEN — a maintainer decision, not resolved here).**
  Every `scope_connections`/`connection_resolver` parameter still defaults to
  `None`, so a future call site that passes a `principal`/`connection_
  resolver` but forgets `scope_connections` silently gets the weaker
  pre-156 (primary-only) behavior rather than an error — the same shape that
  let `admin/service.py`'s `simulate_candidate_policy` drift onto the weak
  path before item 156 caught it. The open question — should `validate_
  policy` self-derive the map in that situation, closing the footgun
  structurally — was deliberately NOT decided under this item's own
  time-boxed audit-response pass: it is a real design change to the shared
  validation-layer contract (every caller of `validate_policy`, including
  `admin/service.py`'s dry-run simulator and `security_benchmark.py`, would
  need to be re-examined against a new default), not a mechanical fix, and
  CLAUDE.md's working agreement reserves exactly this kind of judgment call
  for an explicit decision rather than a default reached for under time
  pressure. Recorded here so it stays visible rather than silently dropped
  when item 160 is eventually closed; whoever makes the call should record
  the outcome as its own Decision Log entry.

  **Finding 4 (confirmed, no code change) — purpose narrowing/`min_group_
  size`/row limits were never claimed to be cross-connection-resolved.**
  `resolve_table_policies` returns the joined connection's Policy un-narrowed
  by the query's declared `purpose`, and `min_group_size`/`max_limit`/
  `default_limit` are read from the primary Policy only. Checked against both
  `docs/THREAT_MODEL.md` QG-09 and this log's own item-156 entry above — both
  already say "column masks, mandatory row filters, and the table/column
  deny-list", never "all policy enforcement" — so no doc drift exists to fix.
  Left as a documented residual and a candidate for its own future TODO.md
  item if per-connection purpose/k-anonymity/limit resolution is ever
  prioritized.
- **2026-08-06 — `connection` gets the MCP `x-mcp-header` annotation;
  query/write AST fields deliberately do not (TODO.md item 130).** The
  `2026-07-28` spec lets a server mark a primitive tool parameter so
  conforming clients mirror it into an `Mcp-Param-{Name}` HTTP header, so a
  fronting gateway can route/authorize on it without parsing the body.
  `connection` is the one parameter every tool shares that is (a) already
  public (`PublicConnectionInfo` returns it) and (b) meaningful to a gateway's
  own routing decision ("this identity may reach connection X"), so it's
  annotated `x-mcp-header: "Connection"` in `mcp/tools/{query,schema,
  write}.py`. Query/write AST content is deliberately never annotated —
  mirroring AST internals into headers would leak query *semantics* (which
  tables, columns, predicates a caller is touching) to every intermediary on
  the path, inverting the confidentiality posture QueryGate exists to
  enforce; the spec itself only clears primitive, already-non-sensitive
  parameters for this treatment. The header is documented everywhere it
  appears (this section, README.md, the field's own docstring in each tool
  module) as a **routing hint, never an authoritative access decision** —
  visibility/routing only, same posture as `_SCOPE_GATED_TOOLS` — and as
  **non-exhaustive**: it mirrors `params.arguments.connection` alone, so it
  says nothing about a `JoinSpec`'s own optional `connection` field for a
  same-instance cross-database join (no header exists for that field at all),
  which is bounded by `join_group` policy instead, not by anything a gateway
  can see on the wire; and three tools (`list_connections`,
  `list_query_templates`, `run_query_template`) take no `connection` argument
  at all, so no header is ever emitted for them.

  **A same-day `auditors` pass caught, and this item's own fix closed, a real
  gap**: the annotation makes `connection` a spec-defined mirrored header for
  the first time, and the `2026-07-28` spec's "Server Validation" MUST-reject
  rule is written generically for *any* such header — not scoped to
  `Mcp-Method`/`Mcp-Name`, the two item 127 originally implemented it for.
  Shipping the annotation without extending that check would have told
  clients (and any gateway trusting them) to rely on `Mcp-Param-Connection`
  while QueryGate silently accepted a body whose actual `connection` argument
  disagreed — the exact confused-deputy shape item 127 exists to prevent, now
  reopened for the one header item 130 itself introduces. Fixed in the same
  commit: `mcp/transport_guard.py`'s `_header_body_mismatch` (item 127's
  guard) now also validates `Mcp-Param-Connection` against
  `params.arguments.connection`, validate-if-present like the other two
  headers, with its own regression tests in
  `tests/security/test_malformed_input_fuzzing.py`. This ships independently
  of item 128 (the `2026-07-28` transport/SDK migration): both the annotation
  and the header check are inert/dormant under the protocol revision
  QueryGate's own server currently negotiates, forward-compatible with that
  migration rather than blocked on it.


- **2026-08-06 — column masks, mandatory row filters, and the table/column
  deny-list now consult a cross-connection join's table's OWN connection's
  `Policy` too, not just the primary connection's (TODO.md item 156).**
  Surfaced by the `security-invariant-reviewer` audit of item 155: that item
  fixed the catalog sensitivity-label *approval trigger* to resolve a joined
  table against its own connection's catalog, but `validation/
  policy_validation.py`'s `validate_policy` and `compiler/
  sqlalchemy_compiler.py`'s `compile_structured_query` still applied a single
  `Policy` — the primary connection's — to every table in the query,
  including one reached through a cross-connection join (`JoinSpec.connection`,
  gated by policy's `join_group` rule). So a column masked under connection
  B's own `Policy` was NOT masked when read via a query whose primary
  connection was A joined to B, unless A's `Policy` happened to declare the
  identical mask for B's table — the same gap for a mandatory row filter or a
  deny-list entry.

  **Fix: a reflection-free sibling of item 155's per-scope connection map,
  threaded through both enforcement paths, unioned rather than replaced.**
  `validation/schema_validation.py` gained `resolve_scope_connections` — the
  same per-scope walk `validate_schema` performs, computing
  `resolve_query_table_connections` per scope, but touching no database at
  all (only the in-memory connection registry/policy store). This matters
  because `policy_validation.py`'s own module docstring requires it to run
  BEFORE schema validation ever reflects anything, so the per-table
  connection map policy validation needs has to be computable before schema
  validation runs, not reused from its output — `execution/service.py`'s
  `_validate_and_compile` now computes this map first and passes it into
  `validate_policy`, preserving that ordering, then separately reuses
  `validate_schema`'s own (already-computed, for item 155) map when calling
  the compiler. A second new shared primitive, `resolve_table_policies`,
  returns `[policy]` alone when a table resolves to the primary connection
  (byte-identical to the pre-156 shape) or `[policy, other_policy]` — the
  PRIMARY connection first, the table's own connection second — when they
  differ; NEVER a strict replacement of one for the other, applying item
  155's own follow-up lesson directly rather than re-learning it. `validate_
  policy`'s table/column allow-deny and masked-column-position checks, and
  the compiler's mask/mandatory-row-filter application (including every
  set-operation arm independently, since an arm — unlike a cte body or a
  nested subquery — is allowed to cross-connection join on its own), compose
  these candidates with the right direction for each rule — and the three
  rules do NOT all compose the same way: `all()` across candidates for an
  allow check (denied under either policy denies the query); a first-match
  walk, PRIMARY-first, for a mask (governed under either applies, but when
  BOTH connections mask the same column differently the primary's own mask
  wins — a `security-invariant-reviewer` pass on this same item, 2026-08-06,
  caught that an earlier version ordered the joined connection first, which
  is a real weakening: the joined connection's mask would win even when it
  is objectively less protective than the primary's own, which is worse than
  the pre-156 default of only ever consulting the primary); and every
  matching filter on every candidate, applied as a UNION (not a pick-one),
  for a mandatory row filter — a joined-only filter and a primary-only
  filter on different tables both apply simultaneously, and if both
  connections happen to filter the SAME table they both apply as an AND, not
  a choice between them. `_validate_cte_constraints`/`_validate_subquery_
  constraints`'s own separate masked-projection checks were deliberately
  left untouched — a cte body or nested subquery is structurally forced to
  stay single-connection (`_reject_cross_connection_nesting`), so their
  table's resolved connection can never actually differ from the primary.

  **Mutation-verified.** Flipping the allow-check helpers from `all()` to
  `any()` fails exactly the "enforced from X connection" tests (the
  permissive direction lets a denied table/column through). Changing
  `resolve_table_policies` to `[other_policy]` — a strict replacement, the
  exact mistake item 155 itself had to correct — fails exactly the "still
  enforced from the primary connection" tests, at both the policy-validation
  and compiler layers. Collapsing the mask lookup to only check the first
  candidate passed every test that existed at the time — a real gap this
  item's own mutation pass caught and closed with a new test
  (`test_cross_connection_join_masked_column_position_rule_still_enforced_
  from_primary_connection`) before landing, exactly the self-review
  discipline the working agreement asks for. Breaking the compiler's
  per-table connection lookup, and dropping the early map from `_validate_
  and_compile`'s call to `validate_policy`, each fail their respective
  targeted tests too.

  **Second pass, same day: a full four-reviewer `auditors` run surfaced four
  confirmed defects, each fixed before this item shipped.** (1) The mask
  ordering above was corrected same-day from an initial joined-first
  ordering — see the paragraph above, already updated to describe the fixed
  (primary-first) behavior; the joined-first version was a real regression
  in the one untested case where both connections mask the same column
  differently. (2) `admin/service.py`'s `simulate_candidate_policy` (the
  `/admin/config/simulate` policy-preview endpoint) still called
  `validate_policy` without the new map — a NEW divergence this item itself
  created (production became correct, the simulator did not), fixed by
  threading the same map through with the simulator's own isolated candidate
  resolver, plus extending its mandatory-filter readiness report to walk a
  joined connection's own filters too. (3) `_validate_correlation`'s own
  item-156 change shipped with no direct test coverage; added (a correlated
  reference into a cross-connection-joined outer table, mutation-verified).
  (4) A weak assertion in one regression-pin test was tightened. Full
  write-up with mutation-verification detail for all four:
  `docs/TODO_ARCHIVE.md` (item 156). Full unit (2225) and security (472)
  suites green after this pass.

  **Known residual, not closed by this item:** the two cte/subquery masked-
  projection checks noted above stay primary-policy-only; if the structural
  single-connection restriction on those containers is ever relaxed, they
  would need the same treatment this item gave `_validate_scope`. Two
  smaller follow-ups filed as their own TODO.md items rather than fixed
  under this item's own time-boxed audit-response pass: item 159 (a
  pre-existing, unrelated case-sensitivity bug in schema reflection's
  connection lookup, found by the same audit pass) and item 160 (four lower-
  severity connection-resolution edge cases in this item's own design — an
  audit/compile Policy-snapshot race under a concurrent reload, a cap-
  ordering inversion, a fail-open-by-default parameter shape, and a purpose/
  k-anonymity/limit scope clarification).

- **2026-08-06 — Snowflake ships as a rendering-level-only phase 1 (TODO.md
  item 19 phase 2); `connections/engine.py` refuses to actually open a
  Snowflake connection.** `DatabaseDialect` gained a `snowflake` member with a
  real `SnowflakeDialectAdapter` (`compiler/dialect_adapters.py`) and
  `SnowflakeSessionAdapter` (`connections/dialects.py`), covering the same
  primitive surface the other three dialects do — date bucketing/EXTRACT
  (`DATE_TRUNC`, with `week` computed explicitly via `DAYOFWEEKISO` rather
  than trusting the `WEEK_START`-session-dependent `DATE_TRUNC('week', ...)`),
  native `NULLS FIRST/LAST` (unlike MSSQL/MySQL, Snowflake has it), sample-
  statistic `STDDEV`/`VARIANCE` (matching Postgres, unlike MySQL's population-
  default bare names), `LISTAGG`, a genuine `ARRAY_AGG` (real native ARRAY
  type), `PERCENTILE_CONT ... WITHIN GROUP` as a plain aggregate (Snowflake's
  `OVER(...)` is optional, unlike MSSQL's analytic-only restriction), full
  ROWS/RANGE window frames, and distinct-only `INTERSECT`/`EXCEPT` (rejected
  with `ALL`, the MSSQL/MySQL posture). Upsert is rejected, not emulated:
  Snowflake's idiom is `MERGE`, a multi-clause statement with no single-
  target-constraint model the way `conflict_columns`/`update_columns`
  express one — synthesizing a `MERGE` from those two fields would be the
  engine inventing statement structure the AST never asked for, the same
  reject-don't-emulate posture as MySQL's `ON DUPLICATE KEY UPDATE` gap.

  **The deliberate exception to "add a dialect, register it, done": engine
  wiring stops short of a live connection.** `snowflake-sqlalchemy`'s DBAPI
  has no async SQLAlchemy engine support (confirmed directly:
  `create_async_engine("snowflake://...")` raises `sqlalchemy.exc.
  InvalidRequestError: The asyncio extension requires an async driver to be
  used. The loaded 'snowflake' is not async.`), and this codebase's entire
  session/execution pipeline is built on `AsyncSession`/`AsyncEngine`
  throughout (`execution/service.py`, `schema/reflection.py`,
  `execution/cost_estimation.py`, ...) — there is no small patch for that.
  Rather than let a Snowflake `ConnectionProfile` reach `create_async_engine`
  and fail with a confusing library-internal error, `connections/engine.py`'s
  `init_engine` checks for `DatabaseDialect.SNOWFLAKE` first and raises a
  clear, explained `ValueError` naming the gap. This also means none of
  `SnowflakeSessionAdapter`'s session-guardrail/cancel methods are reachable
  in production yet — they're implemented for real, against Snowflake's
  public SQL reference (`ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS`/
  `LOCK_TIMEOUT`, `SYSTEM$CANCEL_ALL_QUERIES`), so the shape is reviewed and
  ready, not invented later, but unverified against a live session.

  **Nothing here is live-verified, unlike MySQL's item-19-phase-1 precedent.**
  There is no Snowflake instance available in this project's environment (a
  proprietary cloud service, unlike Postgres/MySQL/MSSQL, which run in
  Docker) and no real account credentials — every Snowflake claim in this
  change is backed by Snowflake's public SQL documentation, and the two
  adapters are tested two different ways (2026-08-06 `claim-reviewer`
  finding — an earlier draft of this entry described both as tested "against
  a real, installed `snowflake.sqlalchemy` dialect object," which is true
  only for one of them): `SnowflakeDialectAdapter`'s output is compiled
  against that real, installed dialect object
  (`tests/unit/test_dialect_adapters.py`'s `TestSnowflake*` classes);
  `SnowflakeSessionAdapter` is tested against recording fakes that assert
  the exact SQL text/params it builds (`tests/unit/test_dialects.py`'s
  Snowflake session-adapter section), not compiled through any dialect
  object, since its statements are built directly with `sa.text(...)`
  rather than as SQLAlchemy Core expressions. Neither path is ever run
  against a live server. Treat every Snowflake behavior claim in this
  codebase as "renders/compiles as documented", not "confirmed correct
  against a real account", until the live-verification follow-up item
  ships. BigQuery shipped the same way one day later — see the 2026-08-07
  Decision Log entry above.

- **2026-08-06 — the catalog sensitivity-label approval trigger consults both
  a joined table's own connection AND the query's top-level connection, not
  either one exclusively (TODO.md item 155).** Surfaced by the
  `security-invariant-reviewer` audit of item 151: `execution/approval.py`'s
  `sensitivity_approval_reasons` looked up every column's table via
  `store.get_table(connection_id, physical)` using a single top-level
  `connection_id`, even for a table reached through a cross-connection join
  (`JoinSpec.connection`, gated by policy's `join_group` rule). A query
  joining connection `analytics`'s `orders` to connection `crm`'s `customers`,
  where `customers.email` is labelled `pii` only in `crm`'s catalog, never
  tripped the gate: the lookup searched `analytics`'s catalog for a
  `customers` entry, found none, and silently treated the joined column as
  unlabelled. Pre-existing and separate in scope from item 151 (which stops an
  already-minted token from being redeemed against the wrong connection; this
  stops the trigger itself from ever asking for one).

  **Fix, shipped in two passes on the same day.** The first pass threaded
  `validation/schema_validation.py`'s `resolve_query_table_connections` output
  — the same per-scope table-to-connection map schema validation already
  computes to decide which connection's schema to reflect a joined table
  against — out of `validate_schema` (a new optional `scope_connections`
  keyword, populated the same way the existing `scope_tables` out-param is)
  and into `sensitivity_approval_reasons` (a new optional parameter, keyed by
  `id(scope)`, each scope's map case-folded onto its effective table names),
  and simply **replaced** the top-level `connection_id` lookup with the
  table's resolved connection. A same-day `security-invariant-reviewer` pass
  caught that this replacement reopens a mirror-image gap: connection catalogs
  are curated independently (item 32's per-connection scoping —
  `import_connection` exists specifically because labels do not propagate
  automatically), so an operator may have labelled a table sensitive under
  only the connection they curated *first*; once a query reaches that same
  physical table through a *different*, cross-joined connection, a strict
  replacement stops finding the label the same way the original bug did, just
  on the other side. The lookup now consults **both** connections when they
  differ and triggers on either hit — over-triggering on an unrelated
  same-named table in the other catalog is the direction this module's own
  fail-closed posture ("denies rather than admits on any ambiguity") already
  commits to; under-triggering on a genuinely sensitive column is not.
  Single-connection queries are unaffected either way: the outer FROM table is
  always present in a real map (seeded with its own connection by
  `resolve_query_table_connections`), a cte reference never reaches this
  lookup at all (skipped earlier by the `cte_names` check), and a call site
  with no map (every pre-155 caller) collapses the two candidate connections
  into one.

  **Mutation-verified, both passes.** Reverting the `approval.py` lookup to
  ignore the map entirely, and separately reverting `schema_validation.py`'s
  main-loop map population to an empty dict, each independently fail exactly
  the same four map-consuming tests (`test_cross_connection_join_trips_
  sensitivity_gate_when_scope_connections_threaded`, `test_cross_connection_
  join_trips_the_gate_through_an_aliased_mixed_case_ref`, `test_enforce_
  approval_gate_uses_the_real_cross_connection_map`, `test_execute_trips_the_
  gate_for_a_cross_connection_join_end_to_end`) — the aliased/end-to-end tests
  were added after an initial (narrower) mutation pass undercounted this, so
  the count here is the one that was actually re-verified, not assumed.
  Reverting the union check back to a strict replacement fails exactly
  `test_cross_connection_join_still_trips_when_the_label_lives_only_on_the_
  primary_side` (the one test built specifically to require both connections
  be consulted). Swapping the lookup key from the alias/table token to the
  physical name, and separately dropping either `.casefold()` call at the
  `scope_connections` population sites, each fail exactly `test_cross_
  connection_join_trips_the_gate_through_an_aliased_mixed_case_ref` — every
  other cross-connection test uses an unaliased, all-lowercase table name, so
  only that one test can tell the two conventions apart.

  **Third pass, same day: the reason string names which connection actually
  matched.** An `architecture-boundary-reviewer` pass on the union-check fix
  above pointed out that "references pii-labelled column customers.email"
  reads as if it describes the physical table the query actually reads, even
  when the union check found the label on the OTHER candidate — an approver
  reading the reason (or an auditor reading the persisted `approval.required`/
  `approval.granted` log line) could reasonably draw the wrong conclusion
  about which database the sensitive data lives in. The reason string now
  appends `(connection '<id>')` naming the connection whose catalog entry
  actually produced the hit, but only when there were genuinely two different
  candidates to choose between — a single-connection query's reason text is
  byte-identical to before, so no pre-155 caller's reason-string handling
  changes. Mutation-verified: dropping the suffix unconditionally fails
  exactly `test_cross_connection_join_still_trips_when_the_label_lives_only_
  on_the_primary_side`'s added assertion, and no other test.

  **Known residual, not closed by this item:** the map lookup is keyed by
  `id(scope)` on the assumption that the exact same `StructuredQuery` object
  flows unmutated from `_validate_and_compile` (where `scope_connections` is
  built) to `_enforce_approval_gate` (where it's consumed) within one
  `execute()` call — true today, verified by reading the call chain — but a
  future change that re-parses or deep-copies the AST between those two points
  would make every lookup miss silently, collapsing back to the pre-155
  single-connection-only behavior rather than raising. Low severity (no
  current call site does this) and left unfixed for now rather than adding a
  speculative guard against a change that hasn't happened; worth revisiting if
  `execute()`'s internal shape changes.

- **2026-08-06 — the approval gate's token binds to connection + principal via
  additive claims, not by folding either into the fingerprint hash; MCP's own
  `RequestStateSecurity(bind_principal=...)` is deliberately not wired
  (TODO.md item 151).** Surfaced by the `security-invariant-reviewer` audit of
  items 19/128: `execution/approval.py`'s `query_fingerprint`/`write_fingerprint`
  hash the AST alone, so a token approved for query `Q` on connection `staging`
  verified unchanged for the byte-identical `Q` on `prod` — realistic, since
  `prod` normally carries the catalog `sensitivity: pii` labels or row volumes
  that actually trip the gate, while `staging` never does. Separately, neither
  the REST token nor the MCP MRTR pending state (`mcp/elicitation.py`) was
  bound to a principal at all, so a `request_state` handed from one agent
  session to another was honored for the second principal's identical retry.
  Four decisions:

  **(1) `issue_approval_token`/`verify_approval_token` (`execution/approval.py`)
  gained two new claims, `"cx"` (connection id) and `"sub_bind"` (bound
  principal subject) — required keyword arguments (no default; pass `None`
  explicitly to opt out), additive alongside the existing `fp`/`sub`/`exp` —
  not absorbed into the fingerprint hash.** This keeps every already-issued
  token's shape and any doc describing "the fingerprint is a hash of the AST"
  stable; the fingerprint computation itself is untouched. Verification only
  enforces a claim the token actually carries: a token minted with `cx`/
  `sub_bind` is rejected unless the caller supplies a matching value (including
  when the caller omits it), fail-closed the same way a fingerprint mismatch
  is; a token minted without them (the pre-item-151 shape, still exercised by
  low-level fingerprint-only tests, both explicitly passed as `None`) verifies
  exactly as before. `"sub_bind"` binds to `Principal.subject` — the calling
  principal's own identity, not a session id and not an agent-delegation
  `actor`; two delegated sessions acting for the same human subject are not
  distinguished, a deliberate, narrower scope than session-binding. `"sub_bind"`
  is a field distinct from the pre-existing `"sub"` claim (the *approver's*
  subject, recorded for audit only, unchanged) — reusing `"sub"` for
  enforcement would have collided with MCP's real-grant minting, which already
  writes an audit-distinctive `f"mcp-elicitation:{caller.subject}"` into
  `"sub"` and would break an equality check against the raw subject. The
  parameters were made required (not optional-with-`None`-default) after a
  same-day `security-invariant-reviewer` follow-up pass on this same item
  pointed out that an optional default is exactly the shape a *future* call
  site could silently omit, reopening the unbound-token gap this item exists
  to close — a `TypeError` on omission forces a deliberate choice instead.

  **(2) The connection binding is always the query/write's own top-level
  `connection_id`, never a cross-connection join's own `connection`
  (`JoinSpec.connection`)** — item 130 (not yet implemented on this branch)
  proposes a `Mcp-Param-Connection` gateway header that would need the same
  scoping for its own reasons; this decision reaches that conclusion
  independently, on its own merits, rather than by citing item 130 as
  precedent. `StructuredQueryService`/
  `WriteExecutionService` already carry `self._connection_id` as the top-level
  connection, so no new parameter threading was needed at that layer; `_enforce_approval_gate`/
  `_enforce_write_approval_gate` just pass it (and `self._principal_subject`)
  into `verify_approval_token`. REST's `approve_query`/`approve_write`
  (`api/routes.py`) mint with `connection_id=<path param>`,
  `principal_subject=<the approver's own subject>` — meaning, after this
  change, **the same principal that calls `POST /{connection}/query/approve`
  must be the one that redeems the resulting token** via
  `POST /{connection}/query`; a token handed to a different principal to
  redeem is now rejected. This is a deliberate tightening of the REST
  out-of-band flow: `/query/approve` has no way to know "the original
  requester's" identity (only the query AST and the approver's own
  credentials), so the only principal a REST-issued token can be soundly
  bound to is the one that actually reviewed and approved it — the approver.
  MCP's flow binds to a different, well-defined identity instead: the
  *original calling* principal whose tool call tripped the pending
  elicitation (`build_pending_input_required`'s new `caller`/`connection_id`
  params), verified again when `resolve_approval_tokens_from_retry` resolves
  the retry and mints the real grant — both ends of one MRTR round trip
  necessarily share that same principal, so this closes the session-handoff
  gap without changing legitimate usage.

  **(3) MCP's own `RequestStateSecurity(bind_principal=...)`
  (`mcp/server/request_state.py`, shipped in the v2 SDK) is deliberately NOT
  wired into `mcp/server.py`'s `MCPServer` construction.** It reads
  `mcp.server.auth.middleware.auth_context.get_access_token()` — the SDK's own
  auth layer — but QueryGate authenticates independently in its own
  `MCPAuthMiddleware` on top of `core/auth.py` and never populates that
  context, so wiring the SDK mechanism would be dead code that appears to
  enforce something it structurally cannot see. The claim-based check in (1)/(2),
  sitting below the transport layer where QueryGate's own `Principal` is
  actually available, is the correct enforcement point instead.

  **(4) A same-day mandatory review found the shipped `cx`/`sub_bind` binding
  closed the *connection/principal* replay but left two adjacent gaps in the
  same token format — both closed with two more claims, additive again, not
  a further redesign.** A pending MRTR elicitation token
  (`build_pending_input_required`) carries a genuine `fp`/`cx`/`sub_bind` —
  it is only ever an integrity-protected "this was asked about" marker, but
  nothing stopped it being presented directly as a real grant. Fixed with a
  `"k"` claim (`kind="grant"` default; the elicitation pending-mint site is
  the only caller that passes `"pending"`; `verify_approval_token`'s
  `expected_kind` parameter, default `"grant"`, is checked unconditionally —
  not opt-in like `cx`/`sub_bind` — since a *missing* kind claim must fail
  the same as a wrong one). Separately, an unconditional `"v"` (format
  version) claim closes a rolling-deploy race: a token minted by a pod
  running a pre-item-151 build (predating `cx`/`sub_bind`/`k`/`v` entirely)
  must not be honored by a newer pod as "carries no binding claims, therefore
  unbound and permissive" — `"v"` is checked first and rejects outright.

- **2026-08-06 — the StructuredQuery AST is published as a namespaced MCP
  extension declaring a contract, never a method, and the namespace is
  `io.github.agitmit/structured-query-ast` rather than a not-yet-owned domain
  (TODO.md item 131, internal half).** The `2026-07-28` protocol revision's
  SEP-2133 extensions framework gives strategic play P2 ("open the contract",
  `docs/business/MARKET_DOMINATION_ANALYSIS.md` §7) a standards-blessed
  vehicle: the read `StructuredQuery` AST and the write
  `Insert`/`Update`/`Delete`/`Upsert` union can be declared as a named,
  versioned artifact instead of just a product-specific JSON schema no one
  outside QueryGate can cite. Four decisions:

  **(1) Namespace: `io.github.agitmit/structured-query-ast`, not a branded
  domain.** SEP-2133 requires a reverse-DNS-shaped prefix
  (`mcp.shared.extension.validate_extension_identifier`); QueryGate does not
  own a registered domain, and inventing one to *look* standards-grade would
  misrepresent what's actually been reserved. `io.github.<owner>` mirrors the
  real, verifiable location of this project (github.com/AGitmit/QueryGate) —
  citable today, not aspirational.

  **(2) The extension declares a contract, never a JSON-RPC method or a
  `tools/call` interceptor — the identical rejection class as the GraphQL
  decision below and as the permanent absence of `execute_sql`.** The
  precedent this item's own scan cites, `io.modelcontextprotocol/tasks`,
  *does* define request methods (`tasks/get`, etc.) — which makes it exactly
  the wrong shape to imitate here: a namespaced method, or an interceptor
  that answers a `tools/call` without reaching the real handler, would each
  be a second query-execution path beside `tools/call` and the single
  `StructuredQueryService` pipeline. `mcp/extensions.py`'s
  `StructuredQueryAstExtension` overrides only `Extension.settings()` —
  never `methods()`, `tools()`, `resources()`, or `intercept_tool_call()`
  (the SDK's fourth contribution point) — so there is structurally nothing
  for `MCPServer._apply_extension`/`_install_extension_interceptor` to
  register or wrap beyond the settings dict.
  `tests/unit/test_mcp_extensions.py::test_no_jsonrpc_method_registered_under_the_extension_namespace`
  and `::test_extension_does_not_intercept_tool_calls` enforce this against a
  real server's registered request handlers and installed extensions, not
  just the module docstring's say-so — the latter was added after a
  security review flagged that the first three tests covered three of the
  SDK's four contribution points but not the interceptor, the one kind that
  actually touches execution.

  **(3) The published schema is generated from the live Pydantic AST models,
  never hand-written — with the drift-guard test running the generator in a
  fresh subprocess, not in-process.** `clients/typescript/src/types.ts` is
  already a hand-mirrored second copy of the AST carrying known drift; a
  hand-authored spec would be a third, and drift in a *published* standard is
  a compatibility break, not just a local bug. `generate_structured_query_ast_schema()`
  (`mcp/extensions.py`) calls `model_json_schema()`/`TypeAdapter(...).json_schema()`
  directly on `query_ast.models.StructuredQuery` and the write union. Building
  it surfaced a pydantic non-determinism worth recording, though not
  independently root-caused against an upstream pydantic issue: `StructuredQuery`'s
  recursive AST cycle (`CteSpec.query`, `SetOpSpec.arms`, `Predicate.value_subquery`,
  ... all forward-referencing `StructuredQuery` itself — items 97/99/100/103/104/105)
  has *multiple* fields that resolve to the same `$ref` target, and repeated
  full-suite runs (2,100+ tests, many constructing their own
  schemas/`TypeAdapter`s over the same model graph) during development showed
  pydantic's JSON-schema generator occasionally misattaching a `$ref`'s
  sibling `description` between two such occurrences — never reproduced in an
  isolated interpreter across the same number of repeated runs. Rather than
  chase that internal further, the fix matches how the schema is actually
  produced in practice: `scripts/generate_mcp_extension_schema.py` always
  runs as a standalone process (`make mcp-extension-schema`), so the
  conformance test's drift guard now generates in a fresh subprocess too
  (`test_mcp_extensions.py::_generate_schema_in_fresh_interpreter`), matching
  that real invocation instead of trusting a shared process's ambient
  pydantic schema-cache state. `generate_structured_query_ast_schema()`
  itself force-rebuilds nothing — that would have made a general-purpose
  library function mutate live, process-global pydantic state on every call,
  a hazard flagged by the same security review, one feature (e.g. serving
  the schema from a route) away from racing real request validation.
  `query_ast/models.py`'s own recursive-cycle rebuild block was extracted
  into a reusable, named `rebuild_recursive_ast_cycle()` function (previously
  inline statements) precisely so the one place a *forced* rebuild is
  actually needed — decision (4) below — doesn't have to hand-duplicate the
  cycle's member list.

  **(4) Tool registration also force-rebuilds the same recursive cycle, and
  this one runs in the real server process, deliberately.** MCP tool schemas
  (`run_structured_queries`'s `List[StructuredQuery]` argument,
  `run_structured_writes`'s write-AST union) are built from the identical
  model graph, by `Tool.from_function` at registration time inside
  `discover_and_register_tools()` — and `create_app()`/`create_mcp_server()`
  "legitimately runs more than once in the same process" (every test in this
  suite; any production hot-reload/multi-instantiation path — see
  `mcp/server.py`'s own `_install_scoped_tool_listing` docstring), which is
  exactly the shared-process condition that produces the symptom in (3). Left
  unguarded, the real `tools/list` schema served to MCP clients could
  silently lose a description the same way. `discover_and_register_tools()`
  (`mcp/tools/__init__.py`) now calls `rebuild_recursive_ast_cycle(force=True)`
  once, before importing any tool module — at registration time only, never
  per query — closing that gap without reintroducing the process-global
  hazard decision (3) deliberately avoided in the schema generator itself.

  **Explicitly not done, gated on the maintainer:** actually publishing this
  as an adopted external standard — registering the namespace with any
  outside body, announcing it, or committing to cross-version compatibility
  for third parties. See `docs/mcp_extensions/structured_query_ast.md`'s
  "Publication status" section and TODO.md item 131.

- **2026-08-06 — proactive lease-driven credential re-resolution composes a
  new optional protocol rather than widening `SecretResolver`, and a
  same-day post-ship security review found one HIGH-severity gap before it
  shipped (TODO.md item 135).** Item 13 already made a rotated
  `${vault:...}` value take effect on the next `reload_config()` without a
  restart; the residual gap was that the refresh was operator-pull only, so
  a short-TTL dynamic credential could expire into failures between reloads
  with nobody scheduling a reload. Three decisions:

  **(1) `LeasedSecretResolver` (`secrets/resolvers.py`) is a separate,
  `@runtime_checkable` Protocol with one method
  (`lease_expiry(reference) -> Optional[datetime]`), probed via
  `isinstance` — not an addition to `SecretResolver` itself.**
  `SecretResolver`'s module docstring is explicit that its one-method shape
  is deliberate; adding lease/TTL methods there would force every backend
  (starting with `EnvSecretResolver`, which is genuinely leaseless — it
  re-reads the environment on every `resolve()` instead) to answer a
  question that doesn't apply to it. `CredentialLeaseMonitor`
  (`config_reload.py`) composes the capability instead, mirroring
  `core/auth.py`'s `CompositeAuthenticator` precedent for this repo's
  "Protocol + one class per variant + registry, composition over widening"
  rule. `lease_expiry` deliberately returns no value at all (an earlier
  draft, `resolve_with_lease(reference) -> (value, expires_at)`, did) — for
  a backend where "reading is issuing" (a Vault *dynamic* secrets engine,
  this feature's actual motivation), fetching the value on every poll just
  to check its expiry would mint and immediately orphan a fresh privileged
  credential, a real design flaw the same review caught before shipping.

  **(2) `VaultSecretResolver.lease_expiry` reports Vault's own
  `lease_duration` response field verbatim (capped at a sane bound), never a
  synthesized TTL.** An earlier draft of item 135 considered forcing every
  Vault reference to report *some* expiry; that would have been exactly the
  kind of invented structure this repo's engine-philosophy doctrine already
  rejects for dialect adapters (see the MSSQL `nulls` precedent above),
  applied here to secrets instead of SQL. The shipped Vault integration
  reads KV v2 (static secrets) only, whose `lease_duration` is genuinely
  `0` — so `lease_expiry` reports `None` for those today, and the monitor
  correctly never fires for them. The trigger machinery is real and tested;
  it activates automatically, with no further change, the moment a
  registered resolver reads a path that actually carries a lease (a
  dynamic-secrets-engine resolver was explicitly out of this item's scope).

  **(3) The monitor resolves its source files through the same
  governance-aware precedence `admin/service.py`'s `apply()` already uses,
  not the plain `AppConfig` paths — found by the mandatory post-ship
  `security-invariant-reviewer` audit, not self-review.** Config governance
  (`admin/service.py`'s `apply()`/rollback) makes a staged, four-eyes-approved
  version's own on-disk files the live truth and never writes that content
  back to `cfg.connections_file` et al. The first cut of `CredentialLeaseMonitor`
  always reloaded from the plain `cfg.*` paths — correct for the
  operator-triggered `/admin/reload-config` endpoint, which behaves the same
  way, but wrong for an *unattended* trigger: the moment a lease came due in
  any deployment using config governance, it would have silently reverted
  the live registry/policy back to stale disk content, discarding an
  approved policy tightening with zero audit trail. Fixed before shipping by
  preferring the active governed version's files
  (`ConfigVersionStore.file_paths`) when one exists, and by recording an
  `audit_config_change(action="lease_refresh", ...)` event on every
  automatic trigger so it's attributable like any other config change.

  The same review also found (and this item fixed pre-ship): unbounded
  synchronous Vault I/O on the event loop (now `asyncio.to_thread` plus an
  explicit 10s client timeout), no hysteresis (a lease shorter than the
  refresh margin — the normal case for a real dynamic credential — would
  have retriggered on every single poll forever), a resolver registry
  pinned for the monitor's whole lifetime (a rotated `VAULT_TOKEN` would
  never be picked up), and scanning the whole raw connections file instead
  of just the `connection_string` field a reload actually resolves.
  Mutation-verified: every one of these guards, plus the `isinstance` probe
  and the monitor's per-iteration catch-all, was broken in turn and made a
  specific test fail for that reason before being reverted. The
  `isinstance` guard mutation initially passed silently (a surrounding
  broad `except Exception` swallowed the resulting `AttributeError`) — a
  genuine gap this process caught, closed by narrowing that except to
  `ValueError` (matching `lease_expiry`'s documented contract).

- **2026-08-06 — item 128 conformed the MCP surface to the final
  `2026-07-28` protocol revision**, upgrading `mcp` 1.28.1 → 2.0.0 (the
  Python SDK's own major rework for this revision). Scoped by reading the
  installed v2 source directly, not the migration guide's prose alone —
  several load-bearing details (below) only became clear that way, and each
  was verified against the real SDK before being relied on.

  **The security-critical boundary was already insulated.** `mcp/auth.py`
  (RFC 8707 audience binding, RFC 6750 challenges) and
  `mcp/oauth_metadata.py` (RFC 9728 metadata) import nothing from the `mcp`
  package at all — QueryGate's MCP surface is a resource server only, and
  those two files are pure Starlette/FastAPI built on `core.auth`. Same for
  `mcp/transport_guard.py` (item 127's header-mismatch guard) and
  `mcp/exceptions.py` (`safe_mcp_tool`). The actual code-change footprint
  was three files: `mcp/server.py`, `mcp/tools/query.py`, `mcp/tools/write.py`,
  plus a full rewrite of `mcp/elicitation.py` (below).

  **`FastMCP` → `MCPServer`.** Transport settings (`streamable_http_path`,
  `stateless_http`, `transport_security`) moved from the constructor to
  `streamable_http_app()`. The v1 tool-listing filter
  (`server._mcp_server.list_tools()(scoped_list_tools)`, a decorator-based
  registration on the private low-level `Server`) no longer has an
  equivalent — `_mcp_server` is gone. Verified directly (a throwaway script
  against the real installed SDK, not assumed from the migration guide):
  `MCPServer.list_tools` is a plain overridable `async def` method, and a
  bare instance-attribute assignment (`server.list_tools = scoped_list_tools`)
  is observed by the dispatcher's own `_handle_list_tools` too, so that's
  the direct v2-idiomatic replacement — no decorator needed.

  **The approval/elicitation port (items 92/93) was the one genuinely novel
  design piece**, because `Context.elicit()` (the v1 blocking mid-call
  round-trip) no longer exists in a form usable under the new Multi
  Round-Trip Requests (MRTR) model — a server may no longer send a
  JSON-RPC request mid-call at all. The SDK ships a sophisticated `Resolve`/
  `Elicit` dependency-injection mechanism that handles MRTR pause/resume
  automatically, but it fills *tool parameters* from a resolver run
  *before* the tool body — QueryGate's approval trigger is decided
  *mid-pipeline* (a cost estimate or sensitivity-label check inside
  `execute()`), not from the raw arguments, so wiring it through that
  mechanism would have meant either duplicating pipeline logic or teaching
  `execution/service.py` about MRTR — both rejected. Verified directly that
  a plain tool function CAN return `InputRequiredResult` on its own
  (`mcp/server/mcpserver/tools/base.py` explicitly supports this path,
  separate from `Resolve`), and that `Context.input_responses`/
  `Context.request_state` (populated from `CallToolRequestParams` on a
  retry) are readable from an ordinary `ctx: Context` tool parameter — so
  the hand-rolled, tool-layer port the item's own write-up anticipated is
  still the right shape in v2, just needed the exact mechanism confirmed
  rather than assumed.

  `mcp/elicitation.py`'s new two-phase design reuses
  `execution/approval.py`'s existing HMAC token signing verbatim (no new
  crypto): a *pending* token bound to each gated item's fingerprint travels
  in `request_state`; on retry, the freshly re-derived fingerprint of the
  *resubmitted* item is compared against it before `input_responses` is
  even consulted (the mutation-verified enforcement point — confirmed a
  naive implementation that skips this comparison lets a caller obtain
  approval for one query and replay the granted state against a different
  one in the same batch slot). `StructuredQueryService.execute_many`
  already had a pre-supplied `approval_tokens` map (the REST out-of-band
  flow's mechanism); `WriteExecutionService.execute_many` did not and
  gained one, mirroring the read side exactly — both services also gained
  typed `approval_fingerprint`/`approval_reasons` fields on their batch
  item results (previously only visible in a free-text `error` string),
  so the MCP layer can build `InputRequiredResult` without parsing
  messages, and any other batch caller gets the same structured signal.

  **Item 127's header-mismatch guard needed no code change** — it already
  validated `Mcp-Method`/`Mcp-Name` "if present, not required" specifically
  *because* the new headers weren't mandatory before 2026-07-28 shipped;
  that posture is unchanged now that QueryGate speaks the revision that
  defines them; only the doc explaining *why* needed updating.

  **Scope, stated honestly:** no dual-path (old-protocol blocking `ctx.elicit()`
  alongside the new MRTR path) was built — a client that negotiates an
  older protocol revision loses the in-session elicitation channel and
  falls back to the existing out-of-band REST token flow, the same
  fail-closed default already documented for "client can't elicit." Given
  the whole feature is opt-in and off by default, this was judged a
  reasonable simplification rather than doubling the approval-flow surface
  to support a deprecated transport.

- **2026-08-06 — item 134 phase 1 added compliance-grade WORM audit
  retention**, composing (never replacing) the item-91 local hash-chained
  ledger with an additional S3 Object Lock archival copy —
  `AuditSinkBackend.JSONL_CHAINED_S3_WORM`. Four judgment calls the item's
  own write-up flagged as needing an explicit decision, made and recorded
  here: **(1) backend** — S3 Object Lock only for phase 1 (the most common
  self-hosted/cloud pairing), with the `AuditSink` Protocol kept narrow so a
  future Azure immutable-blob variant is an additive class + registry entry,
  not a redesign; **(2) fail-open, not fail-closed** — a WORM archival
  failure never blocks or fails the query that triggered the underlying
  event (the local chain sink already captured it for tamper-evidence), but
  the failed batch is re-queued for retry rather than silently dropped, and
  a dedicated metric (`querygate_audit_worm_flush_failures_total`) exists
  for an operator to alert on — only a *sustained* outage past the buffer's
  bound drops the oldest events, visibly; **(3) object granularity is one
  batch ("segment") per flush, never one object per event** — S3 Object
  Lock's retain-until timestamp is set per PUT, so per-event objects would
  each expire at a slightly different moment as they age out, leaving the
  archive's shape incoherent; segments that were written (and will
  therefore retire) together keep it coherent instead; **(4) compose, don't
  replace** — `audit/sinks.py`'s single global `_sink` would have *lost*
  the local chain's tamper-evidence if WORM simply replaced it, the exact
  opposite of "buyers ask for both by name" (per the item's own framing);
  `CompositeAuditSink` (the `CompositeAuthenticator` shape) fans one event
  out to both, with a failure in one sink never suppressing the other
  (proven by mutation: a naive un-guarded loop that stops at the first
  raised exception fails the test suite).

  **Off the request path, by construction, not by convention:** `emit()` —
  the `AuditSink` Protocol method `audit/logger.py` calls synchronously from
  inside `async def execute()` — only ever appends to an in-process buffer
  (`InProcessWormEventBuffer`, mirroring `catalog/usage.py`'s buffered-signal
  precedent exactly, down to the module-level-singleton enqueue/drain split
  with zero explicit wiring between the two sides). All real network I/O
  (the S3 `PUT`, run via `asyncio.to_thread` since boto3 has no native async
  client) lives in a separate `WormFlushMonitor` background task with its
  own `start()`/`stop()` lifecycle, wired into `app.py`'s lifespan exactly
  like `CatalogUsageLearningMonitor`/`CatalogRefreshMonitor` — proven end to
  end (not just at the unit level) by `test_worm_audit_backend_wires_the_flush_monitor_end_to_end`,
  which mutation-verified that a `WormFlushMonitor.start()` that's never
  called fails the test's `is_running` assertion.

  **`configure_audit_sink` converted to a real registry** (`audit/sinks.py`'s
  `_SINK_FACTORIES`, mirroring `secrets/resolvers.py`'s
  `build_secret_resolver_registry`), replacing the inline `if backend ==
  "none"/"jsonl"/"jsonl_chained"` chain the item's own write-up correctly
  flagged as the exact shape non-negotiable #6 forbids. Landing item 136's
  capability-lookup pattern first (already shipped) is what made this safe:
  the new backend just needed one addition each to
  `AuditSinkBackend.is_locally_readable()` and a new sibling
  `wraps_events_in_a_hash_chain_envelope()` (replacing the four scattered
  `== AuditSinkBackend.JSONL_CHAINED` call sites `help_routes.py`/
  `admin_observability_routes.py`/`admin_ui_routes.py` had), rather than
  four call sites each needing to learn about a new backend individually —
  proven by the same exhaustiveness-test pattern item 136 established
  (`test_audit_sink_backend_envelope_wrapping_is_exhaustively_classified`
  fails until every enum member is deliberately classified).

  **Redaction safety (non-negotiable #3) holds by construction, not by a
  second implementation that could drift**: the WORM sink never builds its
  own event body — it serializes the exact same `PersistableEvent` the local
  sinks already write, proven byte-identical in
  `test_flushed_event_body_is_byte_identical_to_the_local_sink`.

  **Scope, stated honestly (as of phase 1):** phase 2 (managed search over
  the WORM archive) is not built — the archive is retrievable directly from
  S3 today, not through a QueryGate query surface. Tested against a real,
  faithful S3 Object Lock emulation (`moto`'s `mock_aws`, confirmed
  separately to accept the same `ObjectLockMode`/`ObjectLockRetainUntilDate`
  parameters a real bucket does) rather than a live AWS account, since no
  real AWS credentials are available in this environment — this is a
  materially different verification bar than item 19's live MySQL server,
  and is named here rather than left implicit.

- **2026-08-06 — item 134 phase 2 added managed search over the WORM
  archive** (`audit/worm_search.py`), closing the gap phase 1 left open
  above. `GET /api/v1/admin/observability/worm-search` answers the
  compliance-review question phase 1's archive alone couldn't: "every query
  against `pii_customers` in the last 18 months" — a window the *local*
  hash-chained file (`admin/anomaly.py`'s reader and friends) has typically
  long since rotated out, but the S3 archive still holds. Three judgment
  calls made and recorded here: **(1) its own scope, not
  `admin:observability:read`** — `ADMIN_AUDIT_WORM_SEARCH_SCOPE`
  (`admin:audit:worm-search`) gates it, deliberately not implied by the
  existing observability-read scope: the WORM archive is the durable,
  potentially multi-year compliance copy, so a principal that can read
  today's in-process trend aggregates should not automatically be able to
  search years of retained history — proven by
  `test_the_observability_read_scope_alone_is_not_sufficient`
  (`tests/integration/test_worm_search_api.py`), not just asserted in the
  scope's own comment; **(2) bounded, not unbounded, by construction** —
  `start_time`/`end_time` are required on every request (no "search
  everything" mode) and capped at `AUDIT_WORM_SEARCH_MAX_WINDOW_DAYS`
  (default 730 days — wide enough for the 18-month scenario the feature
  exists for, still a real enforced ceiling), and one request's actual S3
  work is separately bounded by `AUDIT_WORM_SEARCH_MAX_OBJECTS_SCANNED` and
  `AUDIT_WORM_SEARCH_REQUEST_TIMEOUT_SECONDS` — a bound hit mid-scan
  degrades to a truncated, resumable page — except a day listing over
`max_objects_scanned`, whose cursor repeats that day (item 184) — (an opaque cursor encoding
  `day`/`key`/`line` plus a fingerprint of the request's own filters, so
  replaying a cursor against different filters is rejected rather than
  silently returning a mismatched page) instead of continuing an
  expensive/slow scan; **(3) exploits the archive's own key structure
  instead of a full-bucket scan** — `WormFlushMonitor`'s segment keys
  (`audit/worm_sink.py`'s `_segment_key`) are `<prefix>/YYYY/MM/DD/<ts>.jsonl`,
  timestamp-first, so the search issues one `ListObjectsV2` per calendar day
  in the (flush-interval-padded) requested window rather than one unbounded
  listing over the whole prefix — the padding only widens which day
  directories get listed, since every individual event is still filtered
  against the caller's exact window by its own `occurred_at`, so it cannot
  leak an out-of-window event into a result.

  **Redaction safety holds the same way phase 1's did — by construction, not
  a second implementation that could drift**: this module never constructs
  its own event body, it validates each archived line against the identical
  `extra="forbid"` `PersistableEvent` union (`AuditEvent`/`ConfigChangeEvent`/
  `CatalogGovernanceEvent`/`ConnectionProbeEvent`) the local sinks already
  write and `admin_ui_routes.py`'s own local reader already reuses for the
  identical reason — one union deciding which event shapes exist, never two
  that could drift (the module's first draft hand-duplicated this union;
  `architecture-boundary-reviewer`'s 2026-08-06 pass caught it and it was
  switched to direct reuse the same day). A returned event's TOP-LEVEL shape
  can never carry more than a local audit-browser read already could; the one
  field `extra="forbid"` cannot reach — `query_shape`, a plain
  `Dict[str, Any]` — is separately screened for the same denylisted content
  nested at any depth. Proven, not just asserted, by
  `tests/security/test_worm_search_redaction.py`: a legitimate event with a
  predicate referencing a sensitive value never surfaces that value (only
  its structural shape does, matching `audit/events.py`'s existing
  `_predicate_shape` posture); an S3 object tampered to carry a forbidden
  `sql`/`row_data`/`connection_string` field alongside otherwise-valid fields
  is rejected outright (counted as `malformed`, zero events returned) rather
  than the extra field being silently dropped and the rest let through,
  which would have been a different, still-bad failure mode; and the same
  forgery nested two levels inside `query_shape` is rejected identically.

  **Deliberately REST-only, not also an MCP tool**: no sibling read on
  `admin_observability_routes.py` (overview/anomalies/config-changes/
  history) has an MCP counterpart either, so adding one here would have
  introduced a new REST/MCP asymmetry among admin observability reads
  rather than following an existing pattern — left REST-only to stay
  consistent with the surface as it already exists, not out of an oversight.

  **Hardened by the same-day `security-invariant-reviewer` pass** beyond the
  redaction/query_shape point above: a per-object size probe (`_MAX_OBJECT_
  BYTES`, checked via a lightweight HEAD before the body is ever read into
  memory) guards against a corrupted or adversarially oversized archive
  object; the per-object line-count cap now TRUNCATES (discloses) rather than
  silently drops content past it; `_list_day_keys`'s own S3 pagination is
  bounded by the same deadline and by `MaxKeys`, so neither an unusually
  large day directory nor a single oversized listing page can escape the
  request's bounds; `WormSearchBounds` rejects a misconfigured `default_limit`
  above `max_limit` at construction rather than silently exceeding the
  documented page-size ceiling; and the route wraps its S3 call in
  `mask_unexpected()` so a raw backend failure (bucket/endpoint/driver text)
  can never leak to the client, matching every other REST route's posture.
  **Superseded 2026-08-09 (item 154): the enveloping/hash-chaining gap named
  below is closed for a keyed archive** — see that item's own Decision Log
  entry above (search "WORM archive segments are now enveloped") and
  `docs/THREAT_MODEL.md` QG-40 for the current, full mitigation/residual
  statement (including what's still genuinely open: the unkeyed default,
  intra-segment chain linkage, and key rotation). Original note, kept for
  history: unlike the local hash-chained sink, WORM segments were written
  unenveloped, so this reader could confirm a line matched a known
  redaction-safe SHAPE but not that QueryGate itself wrote it — a principal
  holding `s3:PutObject` on the archive prefix could plant a fabricated,
  schema-valid segment this reader would return indistinguishably from a
  genuine one.

- **2026-08-06 — item 19 phase 1 added MySQL as a third registry dialect**,
  purely additive per the item-57 adapter architecture: `MySQLDialectAdapter`
  (`compiler/dialect_adapters.py`) and `MySQLSessionAdapter`
  (`connections/dialects.py`), registered in their respective registries — no
  existing dispatch site touched. Two genuine capability gaps, decided the
  same reject-don't-emulate way as MSSQL's: (1) MySQL's bare
  `STDDEV()`/`VARIANCE()` are the *population* statistic, a different number
  from Postgres's/MSSQL's sample-statistic bare names — mapped to
  `STDDEV_SAMP`/`VAR_SAMP` instead to preserve the cross-dialect semantic
  contract, verified live; (2) MySQL's `ON DUPLICATE KEY UPDATE` fires on a
  collision with *any* unique/PK constraint, with no way to name a specific
  target the way `conflict_columns` declares one, so upsert is rejected with
  its own accurate message (the generic "no ON CONFLICT clause" text would
  have been factually wrong for MySQL, which does have an upsert idiom — just
  not one that can honor a specific conflict target). Live testing against a
  real MySQL 8.4 server (`tests/integration/test_mysql_live.py`,
  `make test-mysql-live`) surfaced one real pre-existing gap unrelated to the
  adapter itself: `schema/reflection.py`'s `list_live_tables()` excluded
  Postgres's/MSSQL's system schemas but not MySQL's own two (`mysql`,
  `performance_schema`), which would have leaked internal server tables into
  `list_tables()` for any MySQL connection without an explicit `known_tables`
  seed — fixed and covered by a regression test. **Scope, stated honestly:**
  covered here are `dialect_adapters.py`/`dialects.py` unit tests (including
  the item-102 exhaustive per-dialect date/time-primitive suite), a live
  REST-to-real-server integration suite, and CI wiring
  (`.github/workflows/ci.yml`'s `mysql-live` job, `docker-compose.yml`'s
  `mysql` profile). *Not* extended in this pass: the several OTHER
  exhaustive per-dialect unit-test files (`test_compiler.py`, `test_cte.py`,
  `test_nonequi_joins.py`, `test_set_operations.py`, `test_column_masking.py`)
  still parametrize only `postgresql`/`mssql`/`sqlite` — a real, open gap for
  a future pass, not silently claimed as covered. `execution/cost_estimation.py`
  has no MySQL estimator either (falls back to the existing documented
  "any other dialect proceeds under the reactive guardrails" behavior,
  unchanged). TODO.md item 19 stays open for the remaining dialects
  (Snowflake, BigQuery, ...) it was always scoped to cover.

- **2026-08-05 — item 150 closed the one gap item 149 deliberately deferred:
  `mandatory_row_filters` (tenant-scoping) compared an AST-resolved table name
  against an operator-configured `Policy` value using `.lower()`, while every
  `Policy` method item 149 fixed now uses `.casefold()`.** The comparison's
  AST-side input comes from `validation/schema_validation.py`'s
  `effective_name_map`/`declared_cte_names`/`cte_source_names`, a subsystem
  shared by policy validation, schema validation, `execution/approval.py`,
  `execution/service.py`, and `compiler/sqlalchemy_compiler.py` — internally
  self-consistent (every site agreed with every other), which is exactly why
  it never showed up as a live bug until a `.casefold()`-side value
  (`MandatoryRowFilter.table`) crossed into it. Fixed with a mechanical,
  uniform sweep: every one of the subsystem's ~64 `.lower()` call sites (read
  individually first, to confirm each is genuinely an identifier comparison
  and not something unrelated) switched to `.casefold()` together, since
  `.casefold()` is defined to equal `.lower()` for pure-ASCII input — the
  overwhelming majority of real schemas — so the sweep preserves existing
  behavior exactly while closing the Unicode-casing gap everywhere at once.
  Confirmed by the full pre-existing unit suite (2012 tests) passing unchanged
  after the sweep, with zero test needing an update for the ASCII case. Added
  a regression test with a real physical table (`"STRASSE"` — the only
  spelling reflectable at all, since `schema/reflection.py`'s
  `VALID_TABLE_NAME` rejects any non-ASCII table name at load time, which is
  exactly why this mismatch can only originate on the policy-configured side)
  and a `mandatory_row_filters` entry configured `table="straße"`; mutation-
  verified by reverting the specific comparison to `.lower()` and confirming
  the new test failed for the exact reported reason.
  **This item's own mandatory two-reviewer self-review then found a real
  regression the sweep itself introduced:** `query_ast/models.py`'s
  `_validate_table_aliases`/`_validate_cte_names` — the AST-layer uniqueness
  checks `effective_name_map` relies on as its precondition — were still
  `.lower()`-based, so the AST could accept two aliases as distinct (under
  `.lower()`) that `effective_name_map` (now `.casefold()`) then silently
  collapsed into one key, dropping a table from the query graph with no
  error. Confirmed directly, fixed with the same substitution, verified the
  reproduction is now correctly rejected. Two further leftover `.lower()`
  sites in the same bug class were also found and fixed: `admin_ui_routes.py`'s
  policy simulator (a simulated `allowed=True` verdict that omitted a
  mandatory filter real execution would enforce) and
  `write_schema_validation.py`'s WHERE-ref check (provably unreachable in
  production — both sides are already ASCII-restricted by
  `sanitize_table_name` before that comparison runs — fixed for consistency,
  deliberately left untested since a test would require also bypassing that
  restriction). Full unit (2014), integration (346), and security (463)
  suites green on the final tree.

- **2026-08-05 — items 148/149's own mandatory security reviews each found a
  real defect one level deeper than the item's own scope, fixed same-day; one
  (item 149's) was a fail-open human-approval-gate bypass.** Item 148 (diff
  `Policy.column_masks`) shipped a `_diff_masks` implementation whose first
  draft flattened the masks dict directly; both `security-invariant-reviewer`
  and `architecture-boundary-reviewer` independently found it misclassified a
  real loosening as a false tightening whenever a table-specific mask
  shadowed a `"*"` wildcard entry — fixed by resolving through
  `Policy.column_mask()` itself instead of reimplementing its precedence
  rules (verified directly: `Policy.column_mask` evaluated against the
  reproduction before any fix landed). The same review also found
  `blast_radius.py`'s risk-priority table missing `column_mask` **and** the
  pre-existing `purpose_access` (item 145) — both silently fell to the
  lowest-priority bucket — fixed, plus an exhaustiveness test
  (`typing.get_args(SemanticChangeCategory)` against the priority dict) so no
  future category can repeat the gap silently.
  Item 149 (standardize `Policy`'s case-insensitive lookups on `.casefold()`,
  not `.lower()`) grew from a single-method fix into four sites once its own
  `security-invariant-reviewer` pass ran: `WritePolicy.write_column_allowed`
  looked up `denied_write_columns` via a literal `.get(table_name.lower(), [])`
  — a plain exact-key dict lookup with **no** case-insensitive fallback at
  all — so a deny-list key configured with any casing other than all-lowercase
  never matched, regardless of the write statement's own table casing (write
  deny-by-default silently inert; confirmed with a direct Python
  reproduction before fixing). `catalog/models.py`'s `TableCatalogEntry.column`/
  `ConnectionCatalog.table` used `.lower()` while this class's own uniqueness
  validators already used `.casefold()`, so a catalog entry the module treats
  as one unique table/column could fail to resolve — and an unresolved entry
  means its sensitivity label silently never reaches the human-approval gate
  (item 92 phase 2): a genuine fail-open bypass on a Unicode-casing edge case,
  confirmed empirically before fixing. `admin/templates.py`'s policy-template
  merge (item 46) matched table names with **no** case-folding at all — the
  most reachable of the four, since it fails on any casing mismatch, not just
  the exotic Unicode window — and could silently widen an existing allow-list
  to empty (which `Policy.table_allowed` reads as *unrestricted*), directly
  inverting that module's own stated "monotonically restrictive" guarantee.
  All four were fixed with regression tests and mutation-verified (each
  enforcement line broken deliberately, confirmed to fail for the reported
  reason, restored). A fifth, related instance —
  `compiler/sqlalchemy_compiler.py`'s `mandatory_row_filters` (tenant-scoping)
  matching against `validation/schema_validation.py`'s `.lower()`-consistent
  AST name-resolution subsystem — was found but deliberately **not** fixed in
  the same change: it roots in a shared subsystem with several call sites
  (`effective_name_map`/`declared_cte_names`/`cte_source_names`), not a
  one-line fix, and was recorded as TODO.md item 150 for its own dedicated,
  reviewed unit of work rather than a same-session patch under time pressure.
  Full unit (2012), integration (345, excluding `real_db`), and security (463)
  suites green on the final tree.

- **2026-08-05 — mandatory post-session audit of items 137/139/140/145/146/147
  found 6 real, confirmed defects across security-invariant, architecture, and
  test-contract review; all fixed same-day, each with a regression test that
  fails without the fix (mutation-verified).** Auditors ran in parallel
  (`security-invariant-reviewer`, `architecture-boundary-reviewer`,
  `test-contract-reviewer`, `claim-reviewer`; `ui-a11y-reviewer` N/A — no UI
  files touched) over the full `main..HEAD` range. Findings and fixes, most
  severe first:
  1. **Declared `purpose` was persisted to the audit event even when
     `Policy.allowed_purposes` is empty (the default)** — reopening the exact
     free-text-in-audit-log channel `intent`'s exclusion exists to close, just
     under a different field name, on every connection that hasn't opted into
     purpose-gating. Fixed: `execution/service.py`'s 5 `audit_query(...)`
     call sites now gate `purpose` on `policy.allowed_purposes` being
     non-empty (`purpose=(query.purpose if policy is not None and
     policy.allowed_purposes else None)`); a pre-try `policy: Optional[Policy]
     = None` guard avoids `UnboundLocalError` in the two outer `except`
     blocks where `self._get_policy()` itself may have failed.
  2. **`Policy.for_purpose`'s `denied_columns`/`column_masks` merge used
     literal (case-sensitive) dict keys**, so a case-mismatched table name
     between the base policy and a purpose delta (e.g. `"customers"` vs.
     `"Customers"`) could make `_ci_lookup`'s first-match-wins read see only
     ONE side's entries — for `column_masks` this silently **unmasked** a
     column the base policy protected (a real "narrows never widens"
     violation); for `denied_columns` a delta's own added deny silently
     failed to apply. Fixed with a new `Policy._merge_table_keyed` helper
     that resolves table names to one canonical key across both sides before
     merging, the same case-insensitive resolution `_ci_lookup` already uses
     for reads.
  3. **`admin/access_diff.py` never diffed `allowed_purposes`/
     `purpose_policies` at all** — an operator could delete a connection's
     entire purpose gate in a candidate config version and the semantic
     access diff would report no change, the exact "loosening reported as
     no change" governance blind spot item 40 exists to prevent. Fixed: a new
     `_diff_purposes` function (mirroring `_diff_mandatory_filters`'s shape)
     and a new `SemanticChangeCategory` member, `"purpose_access"`. Investigating
     this surfaced a second, **pre-existing** instance of the same gap —
     `column_masks` has never been diffed by `access_diff.py` at all, despite
     an inline comment claiming otherwise since item 49 — filed as TODO.md
     item 148 rather than fixed here (out of this session's scope; it
     predates every item shipped today).
  4. **`admin/service.py`'s candidate-policy simulation discarded
     `validate_policy`'s purpose-narrowed return value**, so a purpose
     delta's own `mandatory_row_filters` entry never appeared in the
     simulation's readiness report — an operator could be told a
     purpose-declaring principal was fully `ready` when a missing claim
     would actually refuse at real execution time. Fixed with the identical
     one-line reassignment `execution/service.py` already uses.
  5. **A bare (non-enveloped) forged line on a `jsonl_chained` backend
     bypassed verification entirely** — `verify_envelope_hash` correctly
     returns `None` (not `False`) for a line with no envelope shape at all,
     since that's exactly what a legitimate plain-`jsonl` line looks like,
     but item 137's own stated goal was that a forged event must never
     display as clean, and an attacker forging a bare line rather than a
     malformed envelope defeated that. Fixed: a new `require_envelope: bool`
     flag on both `JsonlAuditEventSource`/`JsonlChangeEventSource` and a local
     in `_audit_page`, set `True` only when `cfg.audit_sink_backend ==
     AuditSinkBackend.JSONL_CHAINED`; when true, `verified is None` is ALSO
     counted malformed, since every line on that backend must be enveloped.
  6. **The persisted `masked_columns` audit field was computed from the
     un-narrowed policy**, understating which columns a purpose-added mask
     actually protected (the mask itself was correctly APPLIED to the
     compiled SQL either way — this was an audit-fidelity gap, not an access
     bypass). Fixed: `_validate_and_compile`'s return type widened to
     include the effective `Policy` (`Tuple[sa.Select, int, dict, str,
     Policy]`), and `execute()` rebinds its own outer `policy` to it, so
     `applied_column_masks(query, policy)` at the audit call site sees the
     same narrowing the compiler used.

  Additionally: two wording clarifications (item 145's "every query" language
  in `docs/PRODUCT_GUIDE.md`/`examples/policy.example.yaml` narrowed to
  "every READ query", since the gate never reaches governed writes; item
  146's "non-sensitive columns" language changed to "not labeled sensitive in
  the catalog", since a connection with no catalog labels at all would
  otherwise read as a stronger guarantee than it is); seven test-contract
  gaps closed (a TS `.purpose()` test, a Python `.purpose()` value assertion,
  a tightened `_read_last_line` bound-test match string, direct
  `verify_envelope_hash` unit tests including its `ValidationError` branch, a
  trust-page version-drift assertion, six "accepted at exactly the cap"
  companion tests for item 139's AST size caps, and a same-table-different-
  column `column_masks` merge test); and two stale-claim corrections
  (`docs/SECURITY_POSTURE.md`'s adversarial-suite test count corrected from a
  pre-existing stale 310 to the actual 463, propagated into a regenerated
  `docs/TRUST_EVIDENCE.md`; `docs/business/PRODUCT_SCORECARD.md`/
  `MARKET_DOMINATION_ANALYSIS.md` marked stale where they described items
  137/145 as open gaps that shipped the same day, without attempting a full
  re-score — that's `product-scorecard`'s job, not a side effect of an audit
  response). Full unit (1994), integration (345, excluding `real_db`),
  security (463), and TypeScript (43) suites pass on the final tree; every
  fix above was independently mutation-verified (revert the fix, confirm the
  new test fails for the stated reason, restore).

- **2026-08-05 — the quickstart is a standalone `querygate-quickstart` CLI, not
  a `querygate quickstart` subcommand (TODO.md item 146).** The item's own
  prose showed the invocation `querygate quickstart <connection>`, but
  `querygate` (`[project.scripts]` → `querygate.run:main`) is the uvicorn
  server entrypoint with no subcommand dispatcher — every other CLI in this
  repo (`querygate-config`, `querygate-semantic-memory`, `querygate-audit`,
  `querygate-security-benchmark`, `querygate-scope-catalog`,
  `querygate-validate-config`) is its own top-level `querygate-*` script, so
  a new subcommand dispatcher on `querygate` itself would be a second CLI
  pattern for no benefit. `querygate-quickstart` matches the established
  convention (and the item's own parenthetical: "mirroring the existing
  `querygate-config`/`querygate-semantic-memory` CLI shape") — a thin,
  authenticated `httpx` client over already-shipped read-only REST routes
  (`GET /{connection}/tables`, `GET /{connection}/tables/{table}`), adding no
  new server-side authority. Sensitivity filtering reuses
  `TableDescription.columns[].catalog.sensitivity` (item 32) directly — no
  second sensitivity check invented, and no `catalog/search` round trip
  needed since `describe_table` already carries it per column. Verified live
  against the real demo Postgres (`docker compose up` + the packaged
  `examples/` config): all three generated `curl` commands and the printed
  Python SDK snippets execute and return real rows, not just plausible-
  looking text. Mutation-verified: short-circuiting the sensitivity check to
  always return "safe" made the sensitive-column regression test fail for
  the expected reason (a PII column appeared in a proposed query).

- **2026-08-05 — the procurement evidence page is a checked-in generated doc,
  not a live REST route (TODO.md item 147).** The item offered two shapes: "a
  served page (candidate: a `/trust` REST route... or a static generator
  invoked at release time)". A live route was the first instinct, but
  `pyproject.toml`'s `packages` list and the `Dockerfile`'s `COPY` lines both
  confirm `docs/` is **not** part of the installed package or the container
  image — only `src/querygate` and `examples/` are. A runtime handler trying
  to read `docs/SECURITY_POSTURE.md`/`docs/COMPLIANCE_MAPPING.md` etc. would
  work in a dev checkout and 404/fail in the actually-shipped product, which
  is exactly the kind of thing that must be reliable for a security-review
  artifact. `scripts/generate_trust_page.py` (mirroring `scripts/
  generate_sbom.py`'s shape) instead composes `docs/SECURITY_POSTURE.md`,
  `docs/COMPLIANCE_MAPPING.md`, `docs/business/SECURITY_BENCHMARK.md`,
  `SECURITY.md`'s disclosure section, and the live
  `security/dependency-audit-allowlist.json` status into one generated,
  git-committed `docs/TRUST_EVIDENCE.md` (`make trust-page`) — composes the
  real docs **verbatim**, not a lossy summary, so no new evidence or claim is
  introduced. A drift guard (`tests/unit/test_trust_page.py::
  test_committed_doc_matches_generator`) fails if the checked-in file and the
  generator disagree, so "always current" is enforced the same way item 95's
  `docs/SCOPE_CATALOG.md` guard already is, not left as a habit to remember.
  Deliberately no HTML/CSS page: the item's own scope explicitly rules out
  this becoming a marketing page (that's `pitch-sync`/`GO_TO_MARKET.md`'s
  job) — a generated Markdown doc is the evidentiary companion, not a
  landing-page replacement for `landing/security.html`.

- **2026-08-05 — purpose-bound access: declared, closed-set, narrows-only
  (TODO.md item 145, feature F7).** `StructuredQuery.intent` was free text,
  logged but never enforced; Immuta's purpose-based access — where a caller
  must declare *why* it needs data and that purpose narrows what it can see —
  was flagged as a real, unmatched gap in the 2026-08-05 competitive scan.
  Four design choices, each recorded because a flipped default would have
  been a real security regression:
  1. **A new field, `StructuredQuery.purpose`, not a repurposed `intent`.**
     `intent` is documented free text and deliberately excluded from the
     persisted audit event (`audit/events.py`'s own docstring: "no SQL,
     parameter, intent... fields"); conflating the two would have put
     arbitrary caller-authored prose into a policy decision. `purpose` is a
     closed-set token instead, checked against `Policy.allowed_purposes`.
  2. **`allowed_purposes` empty means the connection hasn't opted into
     purpose-gating at all — a declared purpose is accepted but inert.**
     The same "empty allow-list = unrestricted" convention every other
     `Policy` list already uses (`allowed_tables`, `allowed_columns`).
     **Once non-empty, EVERY READ query on that connection must declare a
     purpose from the set** — not just queries touching some enumerated
     "purpose-gated" subset of tables/columns. The alternative (per-table
     purpose-gating) would need a second concept (which tables require a
     purpose) the item's own scope didn't ask for and that adds real
     complexity for a security feature; connection-wide is simpler, harder
     to misconfigure into a false sense of security, and matches how every
     other Policy allow-list already reads (a list-level switch, not a
     row/column-level one). **Scoped to reads only** (matching `intent`'s own
     read-AST-only precedent): the gate runs in `validation/
     policy_validation.py`, which the write pipeline never calls, and the
     write AST has no `purpose` field — a purpose-gated connection's
     governed writes (item 93) are entirely unaffected by this setting.
     Extending the gate to writes is real additional scope, not attempted
     here (found and scoped correctly by `security-invariant-reviewer`,
     2026-08-05; tracked as a candidate follow-up, not built under review
     pressure).
  3. **`PurposePolicyDelta` has no "allow" field — only additional
     `denied_tables`/`denied_columns`/`mandatory_row_filters`/`column_masks`,
     unioned onto the base `Policy` (`Policy.for_purpose`).** This makes
     "narrows never widens" true by construction rather than by convention:
     there is no field a misconfigured delta could set to grant more than
     the base policy already allows. For `column_masks` specifically, the
     delta's own masks are checked FIRST (so a purpose-specific stricter
     mask on an already-masked column actually takes effect), but a
     base-level deny/mandatory-filter can never be removed by any delta,
     empty or not — the field literally isn't there to remove it with.
  4. **`validate_policy` now returns the (possibly purpose-narrowed)
     effective `Policy`, and `execution/service.py`'s `_validate_and_compile`
     rebinds its local `policy` to that return value**, rather than
     resolving the purpose gate in `policy_validation.py` while the
     compiler keeps reading the un-narrowed policy from `_get_policy()`.
     Column masking and mandatory-row-filters are *compiled in*
     (`compiler/sqlalchemy_compiler.py` reads `policy.column_mask`/
     `policy.mandatory_row_filters` directly from whatever `Policy` object
     it's handed) — resolving the purpose only inside `policy_validation.py`
     without this one-line propagation fix would have made a purpose delta's
     mandatory filter pass *validation* but never reach the SQL, silently
     doing nothing. `explain()` and `verdict()` share the same
     `_validate_and_compile` choke point, so both inherit the fix for free.
     The purpose token is also persisted to the audit event (`AuditEvent
     .purpose`) — unlike `intent`, it's a fixed allow-listed token, not
     caller-authored prose, so this doesn't reopen the redaction guarantee.
  New `Policy` fields (`allowed_purposes`, `purpose_policies`) are excluded
  from `GUARDRAIL_FIELDS` (item 115's drift guard) with a stated reason —
  structural access rules, not scalar caps, the same treatment
  `allowed_tables`/`mandatory_row_filters` already get.
  **Mutation-verified:** reverting the `denied_tables` union to
  `delta.denied_tables` alone (dropping the base policy's own list) made both
  the `Policy.for_purpose` unit test and the `validate_policy` integration
  test fail on exactly the widened-access assertion; dropping
  `_validate_and_compile`'s policy reassignment made the compiled-SQL test
  fail (the purpose delta's mandatory filter silently never reached the
  SQL); disabling the missing-purpose check made both the unit and
  `service.execute` tests fail with the wrong (fallback) error message. All
  three reverted; full unit (1950), integration (344, excluding `real_db`),
  and security (463) suites pass on the final tree. The Python
  (`client/builder.py`) and TypeScript (`clients/typescript/src/builder.ts`)
  client SDKs both got a matching `.purpose(...)` builder method and the
  shared kitchen-sink parity fixture was updated, keeping item 51's
  cross-language parity guard green.

- **2026-08-05 — `_audit_page`'s `cursor` ceiling lowered from 1,000,000 to
  5,000, not redesigned (TODO.md item 140).** `GET /api/v1/admin/ui/audit/events`
  retains up to `cursor + limit` fully-parsed event dicts before slicing the
  response page, so a `cursor` near the old ceiling let one admin-scoped
  request allocate on the order of a gigabyte. The item offered two options:
  lower the ceiling, or redesign pagination to an opaque cursor keyed to file
  position (avoiding the need to re-derive `cursor` matches from the start on
  every page). Chose the lower ceiling — it closes the actual resource-
  exhaustion gap with a one-line change and no API/response-shape
  change, versus a cursor-shape redesign that's real scope growth for a
  marginal further improvement once the ceiling itself is no longer six
  figures. 5,000 pages of the default `limit=50` is far past any real "load
  more" admin session, while bounding worst-case retained dicts per request
  to ~5,100 (cursor + limit), several orders of magnitude below the old
  bound. A cursor-shape redesign remains available later if 5,000 pages ever
  proves insufficient for a real deployment's audit-browsing needs.
  Mutation-verified: restoring the old ceiling made the new regression test
  (asserting `cursor=5001` returns `422`) fail for the expected reason.

- **2026-08-05 — hard structural size caps on the read AST's list fields, and
  `audit/sinks.py`'s startup tail-read folded into the item-138 bounded
  reader (TODO.md item 139).** `execution/service.py`'s `normalize_query_shape`
  runs at the top of `execute()`/`explain()`/`verdict()`, before
  `validate_policy` — so a syntactically valid but pathologically large
  `StructuredQuery` (tens of thousands of `select`/`joins`/`group_by`/
  `order_by`/`correlate`/`ctes`/`set_op.arms` entries) got its full shape
  serialized into one audit-log line before policy's own
  `max_select_columns`/`max_joins`/`max_group_by` caps ever had a chance to
  reject the query. Following the item-100–106 precedent (this Decision Log
  entry is the item's own first step), the decision is a **hard, non-operator-
  tunable `max_length`** on each of `StructuredQuery.select` (1000),
  `.joins` (200), `.group_by` (500), `.order_by` (500), `.correlate` (50),
  `.ctes` (50), and `SetOpSpec.arms` (50) in `query_ast/models.py` — enforced
  by Pydantic at request-parsing time, before any application code
  (including `normalize_query_shape`) ever sees the payload, so a caller
  never reaches the audit-log-amplification path at all. These are
  deliberately **not** the same knob as the existing operator-tunable
  `Policy.max_select_columns`/`max_joins`/`max_group_by` (checked later in
  `validation/policy_validation.py`, defaults 30/5/10): the AST cap is a hard
  ceiling generous enough that no realistic operator-raised policy cap would
  ever hit it (10–100x headroom over each field's Policy default, or over a
  sane maximum for fields with no Policy cap at all, like `order_by`), sized
  only to bound the worst case rather than to enforce a product limit.
  `correlate`/`ctes`/`SetOpSpec.arms` were added beyond the item's own three
  named fields (`select`/`joins`/`group_by`/`order_by`) because reading
  `normalize_query_shape` end to end (per CLAUDE.md's "measure the shape the
  product actually emits" discipline) showed both are walked into the audit
  shape exactly the same way, including recursively through nested CTE
  bodies and set-op arms — `intent` was checked too and confirmed **already**
  excluded from the audit event by design, so it needed no change.
  Separately, `audit/sinks.py`'s `_read_last_line` (used once at process
  startup to resume a `jsonl_chained` ledger's head) had the identical
  unbounded-expanding-read shape item 138 fixed for every other audit
  reader — `chunk = handle.read(size - pos)` re-reads a growing span for a
  trailing region with no newline, worst case the whole file, once per boot.
  It's now a thin wrapper over `audit.file_reader.iter_lines_reverse` (the
  same bounded primitive the four read-only surfaces use), reusing the
  composable read-path rather than carrying a second hand-rolled tail
  scanner — a bound-exceeded read now raises the same "refuse to silently
  fork the chain" `ValueError` `_recover_head` already raises for an
  unparseable last line, rather than being misread as "file empty, start at
  genesis" (which would have restarted the sequence at 0 over real prior
  content). Mutation-verified: raising each cap by 100x (and separately,
  short-circuiting `_read_last_line` to always return `None`) made the
  corresponding new regression test fail for the expected reason; both
  reverted and the full unit + integration + security suites pass on the
  final tree.

- **2026-08-05 — audit read surfaces both disclose the actual backend and
  verify chain-envelope self-consistency (TODO.md item 137).** Item 136 made
  the four durable read-only surfaces (`admin/anomaly.py`'s anomaly report,
  `admin/config_trends.py`'s change-trend report, `/help/my-recent-denials`,
  the admin UI `_audit_page` audit browser) accept `AUDIT_SINK_BACKEND=
  jsonl_chained`, but they all still reported `source="jsonl"` regardless of
  backend, and none of them checked whether a chain envelope's own `hash`
  actually matched its contents — chain *linkage* was verify-only
  (`querygate-audit verify`), so a forged record with a plausible `seq`/
  `prev_hash` and an arbitrary `hash` would display as a genuine event.
  Following the same precedent as items 100–106 (the Decision Log entry is
  the item's own first step, not an external gate), the decision is **both**
  options the item's TODO body offered, not one:
  1. **Disclosure**: `source` on all four response models widens from
     `Literal["jsonl", "disabled", ...]` to include `"jsonl_chained"`, and
     each route now passes through the actually configured
     `cfg.audit_sink_backend.value` instead of a hardcoded `"jsonl"` literal.
  2. **Real per-record verification**: a new `audit/ledger.py` primitive,
     `verify_envelope_hash(raw, key=...)`, recomputes `compute_record_hash`
     over `{seq, prev_hash, event}` and compares in constant time against the
     record's own `hash`. All three readers (`JsonlAuditEventSource`,
     `JsonlChangeEventSource`, `_audit_page`) now call it before
     `unwrap_envelope`, counting a self-inconsistent envelope as `malformed`
     rather than displaying it — mirroring how item 136 itself consolidated
     the envelope unwrap into one shared primitive. `resolve_ledger_key`
     (also new, and now shared with `audit/sinks.py`'s write path) converts
     the same `cfg.audit_ledger_hmac_key` string every route already reads.
  **Stated honestly, not oversold**: this is self-consistency only, not full
  chain-linkage verification — a windowed/reverse-order scan never walks the
  whole file from genesis, so it cannot by itself prove no record was
  *dropped*. It is real per-record forgery *detection* for the concrete
  attack the item's report describes (`{"hash": "anything"}` with an
  arbitrary `event` body), and — as `audit/ledger.py`'s own module docstring
  already states for the write path — is only forgery-*infeasible* (not just
  detectable) when `AUDIT_LEDGER_HMAC_KEY` is set; an unkeyed chain still
  only detects tampering relative to a trusted external anchor. Full-file
  chain-linkage verification remains `querygate-audit verify`'s job.
  Regression: a genuine record followed by a hand-forged one (correct
  `prev_hash`/`seq`, `hash: "anything"`) is counted `malformed` and excluded
  from every one of the four surfaces, at both the unit (reader) and
  integration (HTTP) level; mutation-verified — reverting the hash comparison
  to an unconditional pass makes exactly those new tests fail.

- **2026-07-28 — item 35 phase 3's four design-gated questions are resolved,
  maintainer-approved before build (TODO.md item 35).** Phases 1-2 shipped
  agent-visible admission (caller-tunable wait/fail-fast, `admission_id`/
  `queue_wait_ms`, Redis cross-replica queue-depth caps); phase 3 was left
  `[ ]` in ROADMAP.md specifically because its remaining pieces each needed a
  protocol/product decision independent of phase 1-2's storage/admission-control
  work. Four decisions, each with a rejected alternative:

  **(1) MCP progress notifications reuse FastMCP's built-in
  `report_progress`**, not a bespoke QueryGate wire format. MCP's standard
  `notifications/progress` message is keyed on a client-supplied
  `progressToken`; `Context.report_progress()` already no-ops when no token is
  present, so calling it periodically during `concurrency_slot()`'s wait is
  purely additive and opt-in — no new protocol surface, no schema to keep in
  sync with a client SDK.

  **(2) The REST async lifecycle is `202` + `GET .../query/{admission_id}` +
  `POST .../query/{admission_id}/cancel`**, not a streaming (SSE) endpoint. A
  polling lifecycle reuses the `admission_id` phase 1 already returns on every
  response, needs no long-lived connection handling in the ASGI server, and
  matches the REST-idiomatic "long-running job" shape (a resource with a
  status) rather than introducing a second response protocol (`text/event-stream`)
  alongside JSON. Opt-in via a new `queue_mode=async`; the default synchronous
  path is unchanged.

  **(3) Cancellation is real dialect-level cancellation (Postgres
  `pg_cancel_backend`, MSSQL `KILL <spid>`), gated on a new deny-by-default
  `Policy.allow_query_cancellation` flag** — not queue-only cancellation, and
  not a silently-attempted best-effort call. Both dialects' out-of-band
  cancellation primitives need a real permission grant beyond what a typical
  reporting connection has (Postgres: superuser or `pg_signal_backend` role
  membership; MSSQL: `ALTER ANY CONNECTION` server permission or sysadmin) —
  discovered during design, not assumed. Rather than attempt the cancel and
  degrade gracefully on a permission error (which would let an operator believe
  cancellation "mostly works" until the one time it silently doesn't), a cancel
  request is rejected outright, before any DB call, unless the connection's
  policy explicitly sets `allow_query_cancellation: true` — the same
  deny-by-default posture as `allow_cross_join` (item 103). The operator sets
  the flag only after granting the DB-level permission themselves; the
  required grant is documented at the flag's own definition and in deploy docs,
  not discovered by a runtime error. Postgres uses `pg_cancel_backend` (interrupts
  the running query, the pooled connection stays alive and is reused normally)
  over `pg_terminate_backend` (kills the whole backend) — QueryGate pools
  connections, so terminating one would force the pool to detect and recycle a
  dead connection for what should be a single query's cancellation. MSSQL has
  no equivalent gentler primitive at the T-SQL level (unlike a driver's own
  `Command.Cancel()`, there is no out-of-band "cancel just the query" statement
  a second connection can issue) — `KILL <spid>` terminates the whole session,
  a real cross-dialect asymmetry recorded here rather than papered over: each
  dialect gets the primitive it actually has, mechanically translated, not a
  synthesized "graceful cancel" MSSQL doesn't offer out-of-band.

  **(4) Capacity/queue rejections migrate fully from REST `422` to `429` +
  `Retry-After`**, matching item 50's quota-rejection precedent, with no
  compatibility flag to keep emitting `422`. `docs/LOAD_TESTING.md`'s existing
  `422` string-contract commitment is superseded for this rejection class (not
  for policy/schema validation errors, which stay `422`); recorded as a
  breaking change in `CHANGELOG.md`, not silently changed.

- **2026-07-28 — the TypeScript client builder emits the complete
  `StructuredQuery` shape rather than eliding defaults, and its cross-language
  parity guard is a checked-in fixture rather than shared introspection
  (TODO.md item 51 phase 2a).** `clients/typescript/` mirrors the Python
  builder's fluent surface structurally and behaviorally (not name-for-name —
  every multi-word name is renamed snake_case→camelCase per TS convention, on
  top of the handful JavaScript's grammar forces), in-tree only (same registry
  gate as the Python standalone distribution). Two deliberate choices:

  **(1) `build()`/`toDict()` always emit every declared field** (`null` for an
  unset optional, the Python-declared default for a defaulted one) instead of
  reproducing Pydantic's `exclude_none`/`exclude_defaults` trimming. A generic
  "strip falsy/default values" pass was tried first and rejected: it cannot
  distinguish "field never set" from "the value genuinely IS that default" —
  concretely, `WindowFn` includes `"row_number"`, so a global key→default table
  keyed on `fn` would have silently stripped a legitimate
  `ROW_NUMBER() OVER (...)` window's `fn` field the same way it strips
  `TopNSpec.fn`'s default. Per-node-type-aware trimming would avoid that, but
  the effort bought nothing: the server's Pydantic models parse an explicit
  default/null identically to an omitted field (`extra="forbid"` only rejects
  UNDECLARED keys), verified live end-to-end (a join/aggregate query and a
  window running-total query built by the TS SDK both returned real rows over
  HTTP 200 against a real demo Postgres). The untrimmed payload is marginally
  larger, never wrong — and it is what makes choice (2) below tractable.

  **(2) The cross-language sync-test strategy is a checked-in JSON fixture**
  (`tests/fixtures/client_builder_kitchen_sink.json`), not shared runtime
  introspection. Python's own drift guards (`test_client_builder.py`) walk
  `typing.get_args(m.SelectItem)` etc. at test time — a real "the AST grew a
  member with no builder support" trip wire — but TypeScript unions are erased
  at compile time, so there is no equivalent to call from Node. Instead, both
  builders construct three representative queries (a wide join/aggregate/CASE
  query, a window-function query, a set-operation query) and must reproduce
  the identical fixture: pinned on the Python side by
  `tests/unit/test_client_builder_ts_parity.py` (using a FULL, unexcluded
  `model_dump` — the one that agrees with choice (1) above) and on the
  TypeScript side by `clients/typescript/test/kitchenSink.test.ts`. This is
  weaker than Python's automatic new-member detection (a human must still
  remember to extend the fixture and both builders when the AST grows), but it
  is a real, automatic drift guard for every shape the fixture already covers,
  and `clients/typescript/test/builder.test.ts` is the explicit,
  hand-maintained coverage list for everything else — its module doc states
  this asymmetry rather than implying a guarantee TypeScript cannot give.

  Also decided in the same pass: the TS builder has **no CTE/subquery support**
  (items 105/106), matching a pre-existing, previously-undocumented gap in the
  Python builder itself (items 105/106 shipped after item 51 phase 1, and phase
  1 was never revisited) — `correlate`/`ctes` are typed on `StructuredQuery` and
  always serialize as `[]`. Left as a real follow-up on both languages, not
  silently dropped on just the new one.

  **Two real bugs found and fixed before this landed, not silently left
  broken** (a 4-reviewer `auditors` pass — security/architecture/test-contract/
  claim — run before commit, per CLAUDE.md's completion gate): **(1)**
  `validateWindowScope()`'s window/aggregate-exclusivity check duck-typed the
  wire shape (`"fn" in item && ("distinct" in item || "delimiter" in item ||
  "fraction" in item)`) to detect an aggregate sibling — but only
  `AggregateSelectItem` carries an `fn` key at all, so `stringAgg`/`arrayAgg`/
  `percentileCont` siblings silently passed the exclusivity check the server
  still correctly rejects. Fixed with a non-enumerable `Symbol`-keyed marker
  (`AGGREGATE_FAMILY`) tagged onto all four aggregate-family return values —
  invisible to `JSON.stringify`, so it never reaches the wire — rather than
  more wire-shape guessing. **(2)** `agg.count(col("*"), {distinct: true})`
  bypassed the "no DISTINCT with count(*)" check `agg.count("*", {distinct:
  true})` correctly rejected, because `isStar` was computed from the
  argument's *syntactic* form (a bare string literally `"*"`) before
  resolution, while the final `col` value was still resolved via `colName()`
  to the same illegal `"*"` — fixed by resolving the column name once, before
  the check, matching the pattern `stringAgg`/`arrayAgg`/`percentileCont`
  already used correctly. Neither was a security defect (`StructuredQueryService`
  remains the sole enforcement authority and rejected both shapes correctly on
  arrival) but both broke the module's own "same rejection message,
  client-side" promise for those specific inputs — each is now pinned by a
  regression test, mutation-verified by reverting the fix and confirming the
  new test fails for that exact reason. The audit's other actionable finding —
  the TypeScript test suite had no CI enforcement — is closed by a new
  `typescript-client` job in `.github/workflows/ci.yml`.

- **2026-07-27 — a window function becomes a legal `Expression` operand in
  PROJECTIONS ONLY, enforced by one positional rule rather than by a parallel
  expression union (TODO.md item 125).** Item 101 shipped `OVER` and item 100
  shipped arithmetic, but a window was a select-item *projection*, never an
  operand — so `amount / SUM(amount) OVER ()` was two projected columns plus
  client-side division, and row 15 of the canonical regression bar
  (`docs/ENGINE_EXPRESSIVENESS_PLAN.md` §5) stayed red at **15/16**. Item 101
  deliberately recorded this as a *wall awaiting a maintainer call* rather than
  adding it silently, because the fix has a genuine cost. **The maintainer
  approved it on 2026-07-27**; this entry records the shape and the rejected
  alternative.

  The cost is real: the item-100 substrate's reviewability rests on "any
  `Expression` is legal wherever a scalar is expected", and a window is legal in
  a projection but never in `WHERE`, never inside an aggregate, never as a group
  key, never inside another window. Adding `WindowExpr` to the closed union
  therefore breaks that property *in the type*.

  Two ways to restore it. A **parallel projection-only expression union** makes
  misuse structurally inexpressible rather than merely forbidden — the posture
  item 105 chose for recursive CTEs, and the stronger guarantee. It was
  **rejected** because it buys that guarantee by duplicating the entire recursive
  tree across every walker, cap, compiler path and audit shape; the failure mode
  of a duplicated walk is one copy silently missing a rule, which is the exact
  class of bug items 96, 103 and 104 each had to fix. Trading one reviewable rule
  for two divergent trees is a worse maintainability position than the wall it
  removes.

  So the decision is the **single fail-closed positional rule**, sited at the one
  position-aware expression walk: a window may appear only inside a select-item
  expression tree, never nested in another window's `arg`, never inside an
  aggregate argument, with item 101's existing `group_by`/aggregate
  incompatibility carrying over. This keeps the substrate's *review* in one place
  even though the *type* no longer encodes it — which was item 101's actual
  stated concern. Precedent exists: `EXISTS` is likewise legal in exactly one
  position and rejected elsewhere with a typed error. The rule is fail-closed —
  a position that does not explicitly permit a window rejects it — so a future
  expression position added without thinking about windows refuses them rather
  than admitting them.

- **2026-07-27 — correlation is DECLARED and capped, never implicit; a scalar
  subquery must be a single-group aggregate; and `EXISTS` rides on `Predicate`
  rather than becoming a third `WhereNode` member (TODO.md item 106).** This is
  the plan's §8 entry 8 and the last item of the flagship engine pillar. It is
  also the only item that deliberately *removes* an invariant the rest of the
  engine was built on, so each part of the model is stated with what it buys.

  **1. Correlation is a declared list, not ambient scope.** `StructuredQuery`
  gains `correlate: ["Customers.Id", …]` — the outer columns a nested subquery is
  permitted to see. SQL makes every enclosing column implicitly visible; QueryGate
  does not, because the enforcement chokepoint is the canonical visitor
  (`iter_column_refs`), and a ref that resolves against a scope the visitor was
  not looking at is exactly the silent policy-and-mask bypass the plan's invariant
  2 names as the single most important rule in the document. With a declaration,
  every correlated ref has a **known position** the visitor yields, so each one is
  checked against the *enclosing* scope's name map for table allow/deny, column
  allow/deny and the masked-column rule before it is usable. Undeclared outer refs
  keep failing exactly as they do today — `_reflect_and_validate_scope` rejects an
  undeclared table — so the pre-106 behavior is the default and correlation is
  opt-in per subquery. Capped by `Policy.max_correlated_refs`, summed tree-wide.

  **Correlation reaches one level, to the immediately enclosing scope.** Not "any
  ancestor": a scope stack is a second resolution order to keep correct forever,
  and every query shape this item exists to serve (`EXISTS`, per-row aggregate,
  anti-join) correlates to its parent. Deeper correlation is expressible by
  restructuring, and admitting it later is additive.

  **2. A scalar subquery must be an aggregate with no `group_by`.** A subquery
  feeding a scalar comparison (`amount > (SELECT AVG(…))`) must return exactly one
  row, and the three ways to get that are not equal. Injecting `LIMIT 1` picks an
  arbitrary row and returns a *wrong answer with no error* — the failure class
  items 102/117 established this project treats as worse than a rejection. Letting
  the database raise leaves the contract dialect-dependent and post-execution.
  Requiring the single-group aggregate shape makes "exactly one row" true **by
  construction, pre-database, identically on every backend** — and it costs
  nothing real, because that shape is what the use case is: the regression bar's
  row 11 ("customers spending above the overall average") is an `AVG` over no
  grouping, and a correlated per-row `COUNT(*)` is too. A caller needing a
  non-aggregate single value uses `in` with a one-row filter, or two round-trips.

  **3. `EXISTS`/`NOT EXISTS` is an operator on `Predicate`, not a new `WhereNode`
  member.** A third union member would force every `isinstance(node, Predicate)`
  site — `iter_where_predicates`, `_compile_where`, `predicate_column_refs`,
  `where_depth`, and both write validators — to learn a new shape, whose failure
  mode is a consumer silently handling only the members it knows. That is the same
  argument items 104 and 105 turned on, now for the third time, so it is settled
  precedent rather than a fresh judgement. `EXISTS` instead behaves like the
  `is_null` operators that already take no value: the op carries the meaning and
  `exists_subquery` carries the operand.

  **4. A scalar subquery in a SELECT projection is deliberately NOT built** — the
  one part of this item's original write-up that did not ship, recorded here rather
  than left as a silent omission. It is the position with the worst cost profile
  (evaluated once per output row) and the widest blast radius (a new `SelectItem`
  member touching output naming, the ref-position taxonomy, set-op arity and
  `top_n`), and — decisively — it is the one shape an agent can already compose
  from primitives QueryGate exposes. "Each customer with their order count" is a
  cte that aggregates plus a LEFT JOIN:

  ```json
  {"ctes": [{"name": "counts", "query": {
       "from": "orders", "select": ["orders.customer_id",
                                    {"fn": "count", "col": "*", "as": "n"}],
       "group_by": ["orders.customer_id"]}}],
   "from": "customers",
   "joins": [{"table": "counts", "type": "left",
              "on": ["customers.id", "counts.customer_id"]}],
   "select": ["customers.name", "counts.n"]}
  ```

  That is the item-74 posture applied to a position rather than a dialect: expose
  the primitives and point at the composition, rather than grow a second way to
  express the same result. Item 105 is what makes it a real recipe — before ctes
  it would have been two round-trips, and this deferral would not have been
  honest. Revisit only if a caller reports a shape the cte+join form cannot reach.

  **The new ops go on a READ-only operator type.** `CompareOp` is shared with the
  write AST (`write_ast/models.py`), so widening it would advertise `exists` in
  the write tool's MCP schema while the write path rejects it — precisely the
  defect item 114 was raised to fix. The read `Predicate` takes a superset type;
  the write path keeps the narrow one.

- **2026-07-27 — a derived table is spelled as a named `WITH` block (`ctes`), not
  as a subquery inlined into `from`/`JoinSpec.table` (TODO.md item 105, which
  absorbs item 97 phase 2).** `ENGINE_EXPRESSIVENESS_PLAN.md` §4 Phase 4b
  specified "allow `from`/`JoinSpec.table` to be a named subquery (a
  `StructuredQuery` + alias) in addition to a physical table name" — i.e. turn two
  `str` fields into unions. That is the *same* shape item 104 rejected one day
  earlier, for the same reason, and it lost again here. It is the second time this
  plan's up-front sketch has been beaten by a shape found during the build; the
  plan text has been corrected rather than left to contradict the code.

  **Why the union loses, stated as the failure it produces.** The risk is not a
  caller — it is an *unaware consumer*. With `from_table: Union[str, DerivedTable]`,
  every existing site that reads `query.from_table` receives an object where it
  expected a string: `referenced_tables` puts a non-string into a set of table
  names, `normalize_query_shape` records it as the table that was read,
  `policy.table_allowed(...)` is handed a model. Some of those raise and some
  return a wrong answer, and Pydantic cannot flag any of it, because the field is
  legitimately both types. With `ctes`, `from_table` stays a `str` that names
  *something*, and a consumer that has never heard of a CTE treats `"daily"` as a
  table name — at which point table allow-deny and schema reflection **reject it**,
  because no such table exists. The unaware consumer fails closed. That property is
  the whole argument; efficiency and expressiveness agree with it but did not
  decide it.

  Three consequences ride along, each decided rather than inherited:
  1. **One declaration site, and CTE names are a flat namespace.** Only the
     top-level query carries `ctes`; a set-op arm, a `value_subquery` and a CTE's
     own query may not. A CTE may reference an *earlier* CTE (declaration order is
     therefore topological order), never a later one and never itself — the same
     no-forward-reference posture item 103 gave join conditions, and the rule that
     keeps **recursive CTE out of scope** without needing a separate check.
     `max_subquery_depth` is charged along the reference *chain*, so a CTE reading a
     CTE costs 2 and the default cap of 1 denies it until an operator raises it.
  2. **A CTE name may not collide with any physical table named anywhere in the
     query tree.** SQL would let the CTE shadow the table. Shadowing is not itself a
     policy bypass — a CTE's body is a full scope, so its tables get the complete
     allow-deny, mandatory-filter and mask treatment at the source — but it makes
     the audit trail ambiguous about which `Orders` was read, and ambiguity in the
     Proof pillar is a defect. Rejected at the AST layer, before any DB touch.
  3. **A CTE does not carry the `max_rows` clamp.** This follows item 97's
     `_compile_in_subquery`, which strips the limit for the same reason: `max_rows`
     bounds the *response*, and a CTE is intermediate work feeding a join or an
     aggregate. Clamping it would silently truncate the input to a total — a wrong
     answer, which is the failure class items 102 and 117 established this project
     treats as worse than a rejection. An *explicit* caller `limit` inside a CTE is
     honored, because that is the caller expressing "the top 100", not a guardrail.
     Said plainly, as item 103's cross-join entry had to be: what actually bounds a
     CTE is `timeout_seconds`, `max_response_bytes`, the concurrency limiter and the
     new `max_cte_count` — not a row cap.

  **Two things this entry got wrong before the build corrected them, recorded
  because the corrections are the useful part.** (a) The name-collision rule was
  first written as "a CTE name may not collide with any physical table named
  anywhere in the query tree," justified on audit legibility. That phrasing is
  **circular and silently never fires** — the reference to the block *is* such a
  use, so the check excluded the very name it was testing. Writing its test is what
  exposed that. The rule is now scoped to names the **policy has a rule for**,
  which is both checkable and tied to real enforcement: measured, a block named
  after a mandatory-filtered table would have had that filter applied to the
  *block's output*, filtering the wrong rows if it happened to project a column of
  that name and raising a compiler-internal error if it did not. (b) The claim that
  a CTE "inherits every guardrail because it compiles through the same path" was
  true but incomplete — the k-anonymity fan-out check reads *reflected uniqueness
  metadata*, which a CTE has none of, and the function reading it crashed rather
  than answering. That turned out to be a pre-existing defect reachable with no CTE
  at all (item 122); it is now the fail-closed answer the floor requires.

- **2026-07-27 — set operations attach to the query as `set_op`, with the
  carrying query as arm 1, rather than becoming a second top-level query type
  (TODO.md item 104).** The plan called for "a new top-level shape wrapping N
  `StructuredQuery` arms." It did not ship that way, and the reason is the one
  this repo keeps relearning: a second top-level type would have made every
  signature in the pipeline a `Union[StructuredQuery, SetOperationQuery]` — every
  REST route, MCP tool, template, audit call, approval call, cost estimate — or
  made `from`/`select` optional on every query in the codebase. The failure mode
  of that change is a consumer that quietly handles only one member, which is
  precisely the class of bug the item-103 audit found and the item-102 build hit
  five times. Hanging an optional `set_op` off `StructuredQuery` keeps one
  top-level type, so every existing consumer keeps working and the ones that must
  now see *every arm* are found by one question — "does this walk
  `iter_query_scopes`?" — instead of by a type checker that Python does not run.
  Four things follow from that choice, each decided rather than inherited:
  1. **The carrying query is arm 1, and its `order_by`/`limit`/`offset` bound the
     whole statement while its `where`/`group_by`/`having` bound arm 1.** That
     reads asymmetric, and it is — but it is SQL's own asymmetry, not one invented
     here: `SELECT … WHERE … UNION SELECT … ORDER BY … LIMIT …` distributes exactly
     that way. An arm that sets any of the four is rejected at the AST layer, with
     one typed message, instead of producing three different dialect errors (MSSQL
     refuses `ORDER BY` in a compound arm outright).
  2. **An arm is a scope at the SAME depth as its carrier, not one deeper.**
     `max_subquery_depth` bounds caller-authored *nesting*; an arm is a sibling
     SELECT. Making arms depth+1 would have charged a two-arm union against a
     budget it has nothing to do with — and, worse, would have made an arm look
     like an `IN (subquery)` to the two rules that branch on `depth > 0`.
  3. **The compound is wrapped in a derived table before `LIMIT` is applied.**
     Measured, not assumed: SQLAlchemy's MSSQL dialect **silently drops**
     `.limit()` on a `CompoundSelect` — no `TOP`, no `FETCH`, no error — while
     Postgres renders `LIMIT` normally. `clamp_limit` is a policy guardrail, so
     that is an unbounded response on one dialect only. Wrapping makes the limit a
     limit on a plain `SELECT`, which both dialects render correctly, and gives
     `ORDER BY` real derived-table columns instead of a bare output name. This is
     the engine enforcing *its own* cap, not synthesizing structure the caller
     didn't ask for, so it is not the item-74 line.
  4. **`INTERSECT ALL` / `EXCEPT ALL` are rejected on MSSQL and SQLite rather than
     silently de-duplicated.** T-SQL has no `ALL` form of either. Dropping the flag
     would return *fewer* rows than asked for with no error anywhere; SQLAlchemy
     renders the invalid keyword for every dialect with no guard of its own
     (verified by execution — it is a live syntax error, not a compile error). So
     it is a `DialectAdapter` method that raises, the array_agg posture.

  One pre-existing hole was closed on the way, and it is worth naming because it
  was not an item-104 regression: `sensitivity_approval_reasons` — the item-92
  approval trigger — walked only the outer query, so a catalog-labelled sensitive
  column reached from inside an `IN (subquery)` never tripped the human-approval
  gate. That has been true since item 97. It now walks `iter_query_scopes`, which
  fixes subqueries and set-op arms together. The catalog usage signals (32C) had
  the same single-scope assumption, with fidelity rather than safety consequences,
  and were fixed the same way. Note the scope precisely: the **audit shape** was
  taught about set-op arms only — a nested `value_subquery` contributed nothing
  to `normalize_query_shape`, which was tracked separately as TODO.md item 120
  rather than folded in here. *(Item 120 closed that gap on 2026-07-27: a
  predicate's `value_subquery` now recurses through the same
  `normalize_query_shape` authority, so a nested scope is named in the event.)*

- **2026-07-27 — the k-anonymity floor now refuses a join it cannot correctly
  bound, instead of answering as if it had (TODO.md item 118).** `min_group_size`
  compiles to `HAVING count(*) >= k`, which counts **joined** rows. A join matching
  many right-hand rows per left-hand row multiplied a group's count, lifting a
  single-row group above the floor — so a guarantee stated in QG-29, R3, the README
  and the customer-facing security page did not hold across a join. Measured with
  `k=5`: no join suppressed the singleton, `JOIN big ON person.tenant = big.tenant`
  returned it. **That is an equality join**, shipped long before item 103's
  `condition` form, which is why the fix could not be scoped to non-equi joins and
  why the gap is recorded as pre-existing rather than an item-103 regression — the
  item-103 audit surfaced it, it did not cause it.
  Two calls shape the fix:
  1. **Refuse, don't silently exempt.** The same posture item 101 took for
     aggregate windows under this floor: when the floor cannot be enforced
     correctly, the query fails closed with a typed `PolicyViolationError` rather
     than returning an answer the policy believes is protected. The alternative —
     switching the floor to `count(DISTINCT <pk>)` — was rejected because the
     "individual" a group must be backed by *k* of is genuinely ambiguous once a
     join is involved (distinct customers? distinct orders?), and picking one
     silently would trade a visible refusal for an invisible wrong answer.
  2. **Scope the refusal by reflected uniqueness, not by join type.** A blanket
     "no joins while the floor is on" would be simple and fail-closed, but it costs
     the most expressiveness for exactly the deployments that turn the floor on. A
     join provably cannot fan out when the equalities it pins cover a primary key,
     unique constraint or unique index of the joined table — the ordinary
     join-to-a-dimension-on-its-key shape — and those stay allowed. Refused:
     non-unique columns, range conditions, cross joins, and an equality that sits
     inside an `OR` (it holds on only one branch, so it pins nothing). **Narrowing
     never fans out**, so `pk = x AND price BETWEEN lo AND hi` is allowed — an
     added conjunct can only remove rows. **Partial coverage of a composite unique
     key does not count**: `UNIQUE (product_id, region)` needs both columns pinned.
     Direction is fail-closed throughout — an unreflected constraint costs a
     rejection, a missed fan-out would cost the guarantee.

- **2026-07-27 — a join's `ON` clause becomes a full predicate tree, and CROSS
  JOIN is the one join type that is deny-by-default (TODO.md item 103;
  maintainer-ratified before build, as ENGINE_EXPRESSIVENESS_PLAN.md §8 entry 5
  requires).** Before it, `JoinSpec` could only express equality pairs (`on` plus
  `extra_on`), so a price-band join (`ON price BETWEEN lo AND hi`), a temporal
  join (`ON event.at >= window.start`) and any FULL OUTER join were simply
  inexpressible — §5 regression-bar row 16. `JoinSpec.condition` is the same
  `WhereNode` the WHERE clause uses, and `JoinType` gains `full` and `cross`. The
  four calls that shape what it is:
  1. **CROSS JOIN is gated by `Policy.allow_cross_join`, default off, and the gate
     is a *flag*, not a bespoke row cap.** A cross join is the only join whose
     cost is the **product** of its inputs rather than bounded by a key — and,
     more to the point, the only one the join-graph rule exempts from having to
     connect to anything. Before item 103 an accidental cartesian product was
     structurally *inexpressible*, because every join had to name a table already
     in the graph; `cross` is the first way to ask for one, so it must be asked
     for. Cross joins still count against `max_joins`.
     **What actually bounds one, stated by measurement rather than by
     assumption** (an earlier draft of this entry claimed the effective `LIMIT`
     and the item-26 cost gate bounded it "on both dialects" — both halves were
     checked and neither carries that weight): the always-on bound is
     `timeout_seconds` (default 30 — Postgres `SET LOCAL statement_timeout`,
     MSSQL the driver timeout), together with `max_response_bytes` and the
     concurrency limiter. `LIMIT` bounds **rows returned, not work done** — this
     item's own live test issues `count(*)` over a cross join, a statement that
     carries the LIMIT and still makes the server materialize the whole product,
     and the same is true of any `GROUP BY`/`ORDER BY`/`DISTINCT` over one. The
     item-26 cost gate is the right pre-execution bound but is **opt-in and off by
     default**: `max_estimated_rows`/`max_estimated_cost` both default to `None`,
     so `cost_estimation_enabled` is `False` and no `EXPLAIN`/`SHOWPLAN_XML` runs
     at all; it can also be set to `OBSERVE` (never blocks) and is fail-open on an
     unavailable estimate. That gap is precisely *why* the flag is deny-by-default
     rather than a cap — and an operator enabling `allow_cross_join` should set
     `max_estimated_rows` alongside it.
  2. **`on` and `condition` are mutually exclusive, and `cross` takes neither.**
     They are two spellings of one clause, so accepting both would leave their
     precedence — ANDed? overriding? — for the compiler to invent. `extra_on`
     stays tied to `on` (a `condition` expresses composite keys directly). A
     `cross` join carrying a condition is rejected rather than having it silently
     dropped, which would answer a different question than the caller asked.
  3. **A `condition` may reference any table ALREADY joined, not just the joined
     pair.** `extra_on`'s "same two tables" restriction is a property of that
     sugar, not of a join: `JOIN c ON c.x = a.x AND c.y = b.y` is ordinary SQL.
     The connectivity rule therefore generalizes rather than being special-cased
     — the condition must name the table being joined, must connect to something
     already known, and must **not forward-reference a table joined later**,
     which SQLAlchemy would render as a broken or implicitly-cartesian FROM
     clause rather than the join that was written. That third check is new and is
     only reachable through `condition`; the `on` path keeps its original message.
  4. **The ON clause is held to every cap the WHERE clause has.** A join condition
     is the fourth position a predicate tree can occupy (after WHERE, HAVING and a
     searched-CASE `when`), so `max_where_depth`, `max_where_predicates` (summed
     tree-wide, per item 97), `max_in_list_size`, `max_expression_depth`/
     `max_expression_nodes` and the `date_add` interval cap all reach it — wired
     in at the two shared walks (`iter_column_refs` gains a `JOIN_CONDITION`
     position; `iter_scope_expressions` gains join-condition expressions) rather
     than as parallel checks. That single wiring is what makes the denied-column,
     masked-column (item 49) and date-operand (item 117) rules apply inside an ON
     clause by construction. `IN (subquery)` is **rejected** there, matching
     HAVING and CASE: `iter_query_scopes` descends WHERE/HAVING only, so a
     subquery in a join condition would never be validated as its own scope.

  **A pre-existing divergence this surfaced, deliberately left standing.** An
  outer join can NULL out the column a query orders by, and Postgres sorts those
  NULLs **last** on ASC while SQL Server sorts them **first** — so the same AST
  returns the same row *set* in a different *order*. Measured on both live
  servers, this predates item 103: a plain LEFT JOIN diverges identically, and
  item 103 only makes it reachable from a second join type. It is **not** fixed
  here, because the only in-engine fix is `OrderBySpec.nulls`, which item 74
  deliberately *rejects* on MSSQL rather than emulating with a synthesized
  CASE sort column. Changing that posture is a maintainer decision, not a side
  effect of this item; it is pinned by
  `test_outer_join_null_ordering_diverges_and_predates_item_103` so it is
  recorded rather than rediscovered.

- **2026-07-26 — every date/time answer QueryGate gives is UTC, and the Postgres
  session is pinned to make that true rather than claimed (TODO.md item 102).**
  Item 102 adds `extract`, `now` and `date_add`, and the plan required the
  timezone semantics (UTC vs server-local) to be settled before code. The
  finding that decided it: **Postgres resolves `EXTRACT`, `date_trunc` and every
  `timestamp`↔`timestamptz` conversion against the session `TimeZone`**, which
  QueryGate never set. So `extract(hour from a timestamptz)` — and the
  already-shipped `date_bucket` — silently returned whatever the server's
  configured zone implied. The same query, the same data, two deployments, two
  different answers, with nothing in the SQL text to show it.
  **The decision: UTC everywhere, enforced per dialect where each dialect
  actually decides it.** `PostgresSessionAdapter.apply_session_guardrails` now
  issues `SET LOCAL TIME ZONE 'UTC'` beside the existing lock/statement timeouts;
  MSSQL has no session zone to pin, so its adapter reads the clock with
  `SYSUTCDATETIME()` rather than `GETDATE()`/`SYSDATETIME()`; SQLite's `'now'` is
  already UTC. Under the pin, `now()` compares correctly against both `timestamptz`
  and naive `timestamp` columns — neither is correct without it.
  **This is a behavior change, and the blast radius was measured rather than
  assumed** — an earlier draft of this entry over-claimed it. On a Postgres server
  whose zone is not UTC: **`timestamptz`** columns see different `date_bucket` and
  `EXTRACT` values than before (previously server-local, now UTC); naive
  **`timestamp`** columns see **no change at all**, because Postgres never
  consulted the session zone for those; and any comparison between `now()` and a
  naive column changes on every such deployment, since that conversion always did.
  That last case is the one the pin exists for. **Write filters are affected
  identically** — `apply_session_guardrails` runs on the write path too, so a
  write's `WHERE` selects rows under the same UTC semantics, and preview and
  execute stay mutually consistent. The previous behavior was not a different
  valid choice; it was an unstated dependency on server configuration. Columns are
  never converted: QueryGate reads a naive `timestamp` as stored and guarantees
  only that *its own* clock readings and field extractions are UTC.
  Proven live in `tests/integration/test_postgres_date_primitives.py`, which sets
  the role's default zone to `Pacific/Marquesas` (UTC−09:30, a half-hour offset so
  no plausible off-by-N bug can imitate it) and asserts a row stored at 12:00 UTC
  still extracts hour 12. Removing the pin makes it 2.
  **Two bounds added after this item's audit, each measured on a live server
  before being written down.** (1) `DateAddExpr.amount` is capped at signed 32-bit
  independently of `max_interval_days`, because the two bound different things and
  only this one tracks the dialect's limit: `DATEADD(second, 2147483647, …)`
  succeeds on SQL Server 2022 and `…, 2147483648` raises "Arithmetic overflow
  error converting expression to data type int" — reachable once a deployment
  raises `max_interval_days` above 24,855. (2) `extract`/`date_add` over a
  **non-temporal column** is rejected at schema validation. With an INTEGER
  operand, Postgres *errors* while MSSQL silently returns `0` (and `1900-01-03`
  for a shift), because T-SQL implicitly converts an int to a datetime counted
  from 1900-01-01 — the same AST, a hard failure on one backend and a plausible
  wrong answer on the other, which is the items 75/82 class. Only a **bare
  column** operand is checked, since that is the only case with a reflected type;
  Postgres `interval` columns are allowed (`EXTRACT(hour FROM interval_col)` is
  real Postgres, measured), and the cast hint is offered only for string columns
  because casting an integer reproduces the divergence. **Extended to
  `date_bucket` as item 117**, once measurement settled the question that had made
  it look risky: over an INTEGER column Postgres errors, MSSQL returns
  `1900-01-02`, and the internal SQLite path returns `-4712-01-05` — three
  backends, three different wrong answers, so no caller had correct behavior to
  lose and the change is a bug fix rather than a breaking one. All three date
  primitives now go through one shared operand walk, with a coverage test that
  fails if a fourth is added without being wired in.

  **Two further calls recorded with it.** (1) **`max_interval_days` bounds a
  `date_add`'s magnitude** — computed from the amount with *upper-bound* unit
  lengths (a year counts as 366 days, a month as 31) so a larger unit cannot
  launder a bigger reach past the cap. Be precise about what it is: **not** a
  row-count guardrail (a caller who wants everything just omits the filter, which
  `max_limit` and the mandatory row filters bound). It prevents a caller-triggerable
  *server-side error* — a 10,000-year **lookback** overflows T-SQL's datetime range
  and makes Postgres raise "timestamp out of range" — and keeps a relative-date
  filter an honestly-bounded lookback. (Direction matters, and was measured rather
  than reasoned: 10,000 years *forward* is fine on Postgres, landing in 12026.)
  Default 3,660 days = 10 x 366, i.e. ten years counted exactly the way the cap
  counts a year — at 3,653 the cap rejected `{"unit": "year", "amount": -10}`,
  the most natural spelling of the window it advertised.
  (2) **Two parts have a QueryGate-defined value, not a passed-through keyword**:
  `dayofweek` is 0=Sunday..6=Saturday and `week` is the ISO-8601 week, on every
  dialect. T-SQL's natural spellings of both disagree — `DATEPART(weekday)` is
  1-based *and* moves with the server's `SET DATEFIRST`, and plain `week` is a
  different count from ISO — so the MSSQL adapter renders the DATEFIRST-independent
  idiom and `iso_week`. This is mechanical translation of a defined primitive (the
  `date_bucket` category), not synthesized structure. Where a dialect genuinely
  lacks the capability it still **rejects**: the internal SQLite path has no
  ISO-week function, so `extract(week)` raises and points at `date_bucket`'s
  `week` granularity — the item-74 posture, unchanged.

- **2026-07-26 — a write's WHERE is bounded by the READ shape caps, not new
  write-specific ones (TODO.md item 116).** `validate_write_policy` enforced none of
  `max_where_depth`, `max_where_predicates` or `max_in_list_size` — they lived only
  in the read validator, which the write path never calls. So a
  `delete … where id in [<huge list>]` was rendered in full client-side *before*
  `max_affected_rows` was consulted (measured: 100,000 values → a 689 KB statement
  in ~25 ms; past Postgres's 32,767-parameter limit the driver refuses it instead
  of QueryGate refusing it cleanly), while a read of the same shape was already
  capped at 1,000. Surfaced by item 114's audit, where three of four
  reviewers found it independently.
  **The decision** the item asked to be made explicitly rather than defaulted: the
  write path uses the **same `Policy` fields as reads**. A write's WHERE *reads*
  rows in order to select them — the identical reasoning that already applies the
  read allow/deny and masked-column rules to a write filter — so splitting them
  would mean two numbers for one cost and a second place to forget one. Each of the
  three rules is now a single function both paths reach — verified by spying on each
  one and asserting the read *and* write validators route through it — while the
  scope counted over still differs by design: reads sum predicate counts tree-wide
  across subqueries per item 97, a write filter is one tree.
  **Accepted cost:** a write that legitimately needs >1,000 `in` values, >100
  predicates or >5 nesting levels now needs a policy change — the same conversation
  a read of that shape has always required.
- **2026-07-26 — a write's WHERE gets its own narrowed predicate types instead of
  reusing the read `Predicate` (TODO.md item 114).** `run_structured_writes` was
  the largest MCP tool schema in the product — larger than the *read* tool —
  because an `UPDATE`/`DELETE` filter reused the read `Predicate` verbatim,
  inlining `expr`/`value_expr` (item 100's whole `Expression` union),
  `value_subquery` (item 110) and, through that, the entire read
  `StructuredQuery` definition, windows included. **Every one of those is
  rejected by write validation at runtime.**
  **The schema is the agent's contract**, and advertising a field the server
  refuses invites the agent to build a write it will be denied — the
  "hit a wall, route around the gate" failure the engine plan exists to prevent —
  while spending that context on every session. So `WritePredicate` /
  `WriteWhereGroup` now carry exactly what a write accepts (`col`, `col_fn`,
  `op`, `value`, `value_col`, boolean groups).
  **They are a narrowing, not a parallel grammar** — the distinction that keeps
  this from re-opening the item-96/111 duplication: `to_read_where` converts at
  the validation boundary, so the canonical predicate walk and the one compiler
  are still the read implementation, and `WritePredicate` validates by
  *constructing* a read `Predicate`, so the **operator/value** rules are literally
  the same rules rather than a second copy that can drift. Two rules are
  deliberately write-only and stricter: `col` and `value_col` must be dotted
  `Table.Column` refs, where the read side defers dottedness (a HAVING clause may
  legitimately name a select alias). For `col` that closed a real latent gap — an
  undotted ref was *skipped* by the reference walk, so it bypassed allow/deny and
  masking entirely before failing in the compiler; for `value_col` it only moves an
  existing rejection earlier. Error messages are re-raised in the write
  vocabulary, because the read predicate's own text offers `value_expr`/
  `value_subquery` as alternatives — the removed fields — which would move this
  item's defect from the schema into the error channel. Read nodes pass through
  the conversion unchanged, which is what keeps the runtime rejections reachable
  as defence in depth; anything else fails closed, since the permissive version
  would have silently dropped the terms of a future combinator.
  **The MCP context budget went down, not up:** 128,551 → 104,042 chars (**-19%**;
  the baseline is item 115's tree, which had itself added 1,851 chars over item
  101's 126,700 without a budget note), and the write tool 38,964 → 14,455
  (-63%). `_MAX_TOTAL_CHARS` drops 132,000 → 110,000: back below item 100's
  123,000, though still ~2,000 above the 108,000 ceiling that predated item 100 —
  the earlier claim that it was *lower* than pre-item-100 was simply wrong, and
  the measured *total* being lower is the accurate version of it. Along the way,
  `write_preview.py`'s subquery rejection — a fifth hand-rolled predicate
  enumerator that item 111's four-validator consolidation had missed — was found
  to be **unreachable** (every call site ran after the validator that already
  rejects the same thing) and untested, so it was deleted rather than kept as
  decorative defence in depth.
  **Accepted cost:** `col_fn` is deliberately kept — it works on the write path
  today, and removing it would break currently-valid payloads. Python-level
  construction of a write statement now uses `WritePredicate`; wire payloads are
  field-for-field identical, pinned by a round-trip test over 18
  previously-valid payloads covering every comparison operator.
- **2026-07-26 — the four "which caps are in force" surfaces are derived from
  `Policy` instead of hand-listed, and a cap's *direction* must now be stated
  (TODO.md item 115).** Four places answered that question — the semantic access
  diff (item 40), the effective-guardrails view (item 45), the help API's policy
  summary, and the admin UI's policy panel — each with its own typed-out field
  list and nothing checking any of them against `Policy`. All four had rotted:
  nine caps from items 68–72, 88, 97, 100 and 101 were missing from at least one,
  and the shortest list (the admin UI's) omitted even `max_top_n`. The concrete
  harm is not cosmetic: `/admin/config/diff` exists to make a staged policy
  change legible *before* it ships, so a cap it cannot see is reported to the
  approving reviewer as **"no guardrail change"** — a governance surface telling a
  human the opposite of the truth, which is worse than having no diff.
  **The fix inverts the failure mode.** `GUARDRAIL_FIELDS` is derived from
  `Policy.model_fields` minus a small, reasoned exclusion set (structural
  allow/deny rules, which the diff already itemizes properly; the nested
  `WritePolicy`; and `approval_sensitivities`, a *list* with no scalar
  permissiveness, which now gets its own dedicated change instead of being
  invisible). So a new cap is reported everywhere **by default**, and forgetting
  is loud rather than silent: a non-scalar field that nobody excluded fails a
  test, a surface that re-grows a private list fails a test, and a stale
  exclusion fails a test. `EffectiveGuardrails` is *generated* from those fields,
  so the response model cannot drift from the enforcement model in either
  membership or type.
  **The second-order trap, closed too:** deriving the field set fixes "a new cap
  is invisible" but leaves "a new cap is diffed **backwards**". Direction is not
  derivable in general — a larger `min_group_size` suppresses *more* groups, and
  the same request budget over a longer `quota_window_seconds` is a *lower* rate,
  so both are tightening while every `max_*` cap loosens as it grows. A `max_*`
  name states its own direction; every other guardrail must be listed as
  direction-reviewed, and a test fails until it is. That test caught two fields
  on its first run (`approval_max_estimated_rows`/`_cost`) whose direction had
  never actually been considered.
  **Accepted cost:** `EffectiveGuardrails` widens from 19 fields to 34 — additive
  for consumers, but it is a public response shape, which is why this was split
  out of item 101 rather than ridden along with it. The naming convention
  (`max_*` = ceiling) is now load-bearing for the direction exemption, and is
  itself asserted rather than assumed.
- **2026-07-26 — general window functions enter the read AST as a *projection*
  with four deliberate bounds (TODO.md item 101; maintainer-ratified before
  build, as ENGINE_EXPRESSIVENESS_PLAN.md §8 entry 3 requires).** Before it,
  `top_n` was the only `OVER()` surface in the product and it was hard-wired to
  rank-and-filter-top-N, so a running total, a moving average, a rank projected
  *alongside* the rows, and lag/lead gap analysis were all inexpressible.
  `WindowSelectItem` adds them. The four calls that shape what it is — each one a
  place a looser design would have produced an AST that renders on Postgres and
  fails on a live SQL Server, or that silently answers a different question than
  the caller asked:
  1. **No default frame is synthesized.** Omitting `frame` emits no `ROWS`/`RANGE`
     clause at all, so the dialect's own SQL-standard default applies (with
     `order_by`: everything up to the current row's peers; without it: the whole
     partition) — identical on Postgres and MSSQL. Inventing a frame the AST
     didn't ask for is precisely the item-74 line. **A numeric `RANGE` offset is
     rejected on MSSQL** (T-SQL's `RANGE` accepts only unbounded/current-row
     bounds) pointing at `ROWS`, rather than being rewritten to `ROWS`, which has
     different tie semantics. Verified live: Postgres runs `RANGE 2 PRECEDING`,
     SQL Server refuses it, and SQLAlchemy compiles it happily for both — so the
     adapter is the only thing standing between that AST and a live failure.
  2. **Unbounded frame ends are NOT separately gated, and the cap is on
     *offsets* instead.** `UNBOUNDED PRECEDING … CURRENT ROW` *is* the
     running-total idiom and also SQL's own default frame, and an
     unbounded-both-ends frame is the same whole-partition scan as omitting the
     frame — so a flag forbidding it would be theater while the no-frame form
     stays legal. What is genuinely unbounded is the caller-supplied *magnitude*
     in `N PRECEDING`/`N FOLLOWING` and in a `lag`/`lead` offset, so those get
     `Policy.max_window_frame_offset` (default 1000), and the count of windows
     gets `Policy.max_window_specs` (default 5, summed tree-wide per item 97;
     0 disables windows). Window `PARTITION BY` shares `max_partition_by` with
     `top_n` — same cost, one budget.
  3. **A window cannot be combined with `group_by` or aggregate select items.** A
     window over *aggregated* values needs the aggregation materialized as a
     derived table first, which is item 105's job; the alternative was to grow a
     second bespoke materialization path beside `_apply_top_n`'s and to let a
     window's refs name select aliases (which `ColumnExpr` forbids, since no
     dialect lets an `OVER` clause reference a peer alias). Rejected at the AST
     layer with a message pointing at composition. **Accepted cost:** a 7-day
     moving average of *daily* order counts (§5 regression-bar row 3) is still
     two queries, and the plan's claim that item 101 alone unblocks that row was
     optimistic — §5 now records the correction rather than the aspiration.
  4. **An aggregate window is rejected when `Policy.min_group_size` is set.** The
     item-88 k-anonymity floor is enforced as `HAVING count(*) >= k` on grouped
     results; a window aggregate produces no group to filter, and `COUNT(*) OVER
     ()` would report a below-floor count that the aggregate path suppresses —
     with no column projected at all, so even a table whose every column is
     denied would leak its size. No in-statement construct can re-impose the
     floor without `QUALIFY`/a derived table, so windows fail closed while the
     floor is on. Ranking/offset windows (`row_number`/`rank`/`dense_rank`/
     `ntile`/`lag`/`lead`/`first_value`/`last_value`) stay allowed: they only
     surface values the caller may already project bare. **Accepted cost:** a
     k-anonymity deployment gets no running totals; the floor's guarantee wins
     over the convenience, and `min_group_size` is off by default.
  **Also decided: a window is a select item, not an `Expression` operand.** So
  `amount / SUM(amount) OVER ()` (§5 row 15) is *composable* — project both
  columns and divide client-side — but not expressible in one scalar expression.
  Making `WindowSelectItem` a union member would give `Expression` a member that
  is legal in some positions and illegal in others (never in `WHERE`, never
  inside an aggregate, never as a group key), breaking the "legal everywhere a
  scalar is expected" property that makes item 100's substrate reviewable in one
  place. Recorded in §5 as a discovered wall, for the maintainer to weigh as its
  own item rather than smuggled in here.
- **2026-07-25 — the roadmap is re-sequenced so the Expressive Query Engine
  (items 99–106) becomes ROADMAP.md Phase 4, ahead of adoption/breadth.** The
  engine pillar was previously a subsection at the *bottom* of "Phase 4 — Adoption
  & breadth", sequenced behind the client SDK (51), DX polish (35), a perf check
  (94), and the stored-procedure catalog (18). **Why that was wrong:** the engine
  *is* the North Star's **Structural** pillar, not polish downstream of it. The
  "no caller-controlled raw SQL, ever" bet (non-goal #1) only holds if the
  structured surface is expressive enough that a fluent SQL author rarely hits a
  wall — an agent that hits one routes around the gate (dumps tables, asks for raw
  access, gives up), at which point the safety guarantee protects nothing because
  nobody adopts it. Expressiveness is what makes the safety constraint acceptable.
  Adoption/breadth is also *literally downstream*: every engine item widens the AST
  the SDK must mirror and each new dialect adapter must render, so building those
  first buys rework. Adoption & breadth became Phase 5, catalog/observability Phase
  6. **Two selector fixes shipped with it** so the automated `roadmap-next` walk is
  deterministic rather than re-derived from prose each session: items 58 phase 2
  and 30·89 phase 2 sit in Phase 0/1 *ahead* of the engine but appeared in neither
  gating list (their blockers — external LLM/Toolbox infrastructure, and the
  maintainer's own signed-release tag push — were described only inline), so both
  are now listed as externally blocked; and the pick algorithm now states
  explicitly that **a required Decision Log entry is not a skip condition** — it is
  the item's own first step. Without that, a cautious agent could skip all seven
  engine items (each of which says "requires a Decision Log entry before build")
  and fall through to Phase 5, the exact failure the re-sequence corrects.
  **Accepted cost:** the client SDK's TypeScript half and new dialects wait; a
  design partner integrating today uses the shipped in-tree Python builder and the
  two supported dialects.
- **2026-07-25 — a closed, depth-capped scalar `Expression` union entered the
  read AST (TODO.md item 100), and it is deliberately *not* the "open-ended
  expression grammar" non-goal #7 forbids.** *(Decision recorded ahead of
  implementation, as ENGINE_EXPRESSIVENESS_PLAN.md §8 requires; **item 100 has
  since SHIPPED and holds to all five boundaries below** — see
  [Computed expressions](#computed-expressions-arithmetic-conditional-aggregation-nested-functions)
  for the shipped capability, and the two follow-on entries below for the
  decisions the build itself forced.)* Before it, "what can be projected or
  compared" was a flat set of leaf
  shapes: a bare `Table.Column` string, a one-level `ScalarFunctionCall` with no
  nesting, an aggregate over a bare column string, and a `CASE` whose `then`/`else`
  is a column-or-literal. That single boundary is why arithmetic, conditional
  aggregation, nested functions, expression-valued `CASE`, and computed
  GROUP/ORDER keys are all *the same* missing feature — `SUM(quantity *
  unit_price)` fails both because no `*` operator exists anywhere and because an
  aggregate argument can only be a column name. Item 100 replaces that flat set
  with one recursive `Expression` union (`ColumnExpr` | `LiteralExpr` |
  `BinaryOpExpr` | `FunctionExpr` | `CaseExpr`) used everywhere a scalar value is
  expected, and gives aggregates an `Expression` argument.
  **The hard boundary that keeps this bounded rather than open-ended**, stated so a
  future change can be measured against it:
  1. **The operator set is fixed and finite** — `+ - * /` only. Not a parser, not
     an operator table a caller can extend.
  2. **`FunctionExpr.fn` is an enum, never a free string.** A caller cannot name a
     UDF, a stored procedure, or any function the enum does not list. Adding a
     function is a code change with a dialect decision, not caller input.
  3. **Nesting is capped, not unbounded** — new `Policy.max_expression_depth`
     (default 5) and `Policy.max_expression_nodes`, the latter summed **tree-wide**
     via `_enforce_tree_wide_caps` exactly as item 97 requires, so nesting an
     expression inside a subquery cannot multiply the budget.
  4. **Every leaf is still a typed identifier or literal.** `ColumnExpr.col` is
     resolved and policy-checked like any other column reference; `LiteralExpr`
     binds as a parameter. No string is ever interpolated into SQL, so non-goal #1
     is untouched.
  5. **The canonical visitor recurses through every `Expression` node.** An
     unvisited `ColumnExpr` buried in a `BinaryOpExpr` would be a silent policy and
     masking bypass, so `select_item_column_refs` / `predicate_column_refs` recurse
     and yield each `ColumnExpr.col` at its correct `RefPosition`. This is the
     make-or-break safety step, and the headline adversarial test is a
     denied/masked column nested arbitrarily deep in
     `BinaryOpExpr`/`FunctionExpr`/`CaseExpr` being rejected.
  What non-goal #7 forbids is a grammar whose *shape* the caller authors freely — an
  expression string, an arbitrary function name, unbounded nesting. What this adds
  is a closed algebra over identifiers a caller is *already* allowed to reference:
  finite operators, an enumerated function set, a capped tree. The difference is
  that the set of legal programs here is enumerable and every one is
  policy-checked; an open grammar's is not. **Accepted cost:** the AST surface an
  agent must learn grows meaningfully, and every later engine item (101–106)
  inherits `Expression` as a dependency — a bug in the substrate is a bug
  everywhere. That is the deliberate trade: one node reviewed hard, once, instead
  of five separate special cases each with its own visitor wiring to forget.
- **2026-07-25 — division renders guarded: `left / NULLIF(right, 0)`, so
  divide-by-zero yields NULL rather than an error** (item 100; ratified by the
  maintainer). The alternative — dialect-native behavior — is **not reliably
  specified for us**: Postgres raises `division_by_zero` unconditionally, while
  MSSQL's behavior is contingent on `ARITHABORT`/`ANSI_WARNINGS`, which
  `connections/dialects.py`'s `MSSQLSessionDialectAdapter` does **not** set (it
  sets only `LOCK_TIMEOUT`, `XACT_ABORT ON`, and `DEADLOCK_PRIORITY LOW`) — so the
  outcome would be inherited from whatever the driver/connection negotiates rather
  than from anything QueryGate specifies. Worse, that same adapter sets
  **`XACT_ABORT ON`**, under which a run-time error aborts the entire transaction:
  an unguarded divide-by-zero would take down the whole request rather than yield a
  row. So "do nothing" means one AST whose behavior varies by backend *and* by
  driver configuration — exactly what the cross-dialect differential execution
  suite (item 36 phase 2b) exists to prevent. Guarding makes it deterministic
  everywhere. It also turns an ordinary analytic query — a ratio over a group that
  happens to have a zero denominator — into a failed request rather than a NULL
  cell, which is what a SQL author writing this by hand would reach for anyway.
  **Accepted cost:** a caller cannot distinguish "denominator was zero" from
  "operand was NULL" without an explicit `CASE`, and we diverge from Postgres's
  native behavior — a deliberate consistency-over-fidelity call, documented in the
  capability section rather than left for a user to discover. This is **not** the
  engine synthesizing structure the AST didn't ask for (the item-74 line): it is
  one fixed, documented rendering of the `/` operator the caller *did* ask for,
  identical on every dialect — the same category as `date_bucket` compiling to
  `date_trunc` on Postgres and `DATEADD`/`DATEDIFF` on MSSQL.
- **2026-07-25 — `AggregateSelectItem.col: str` is kept permanently as sugar for
  `ColumnExpr`, a ratified exception to the item-99 "prefer the clean break"
  precedent** (item 100). The 2026-07-24 item-99 entry set the precedent for items
  100–106: prefer a clean breaking change while the AST has no external consumers,
  and add a shim only once a real caller would break. That precedent and
  ENGINE_EXPRESSIVENESS_PLAN.md §4 Phase 1's migration note (keep `col` working as
  sugar) genuinely conflict, so the conflict was surfaced and decided in the open
  rather than resolved silently by whichever document was read last. **Decision:
  keep `col`.** Unlike `having` — which no caller outside this repository emitted —
  `col` *does* have consumers: `examples/`, the docs, and the Python client
  builder's `agg.*` helpers all produce it. And `SUM(col="Orders.Amount")` is the
  overwhelmingly common case; forcing `arg={"col": "Orders.Amount"}` on every
  aggregate is ceremony for no safety gain. **Why this is a narrower exception than
  the `having` shim would have been:** `col` is normalized to `ColumnExpr` by a
  `mode="before"` validator, so exactly one shape reaches the compiler, the caps,
  and the canonical visitor. It is a *spelling*, not a second structural shape and
  not a second code path — the property that made rejecting the `having` shim
  worthwhile (one way to express one thing, at the enforcement layer) is preserved.
  **Accepted cost:** two spellings exist at the wire/schema level, so the MCP tool
  schema and docs must show which is canonical, and a future reader must know the
  normalization exists to reason about `col`.
- **2026-07-25 — `ColArg`/`LiteralArg` were collapsed into item 100's
  `ColumnExpr`/`LiteralExpr` rather than kept as sibling types.** The two pairs had
  byte-identical wire shapes (`{"col": …}` / `{"literal": …}`), so building the
  `Expression` union alongside them would have left **two** column-leaf types that
  every walker, cap, and policy check must treat identically forever — precisely
  the duplicated-walk failure class items 96 and 111 were built to remove,
  reintroduced at the leaf. `ColArg`/`LiteralArg` are now aliases of the same
  classes, so `ScalarFunctionArg` is a subset of `Expression` and existing payloads
  and Python call sites keep working verbatim. **Two deliberate tightenings ride
  along:** a column leaf must now be a dotted `Table.Column` at the AST layer
  (previously a bare name was accepted and rejected later by `parse_column_ref`),
  and a literal is typed `str|int|float|bool|None` instead of `Any` (a dict or list
  literal is now rejected up front rather than failing opaquely at bind time).
  **Accepted cost:** both tightenings are technically breaking for inputs that
  could only ever have failed further down; the earlier, typed rejection is the
  point. The `ColArg`/`LiteralArg` names are kept because they read better at a
  scalar-function call site.
- **2026-07-25 — item 100's `Expression` is a READ-engine capability; a computed
  predicate in a write's `WHERE` is rejected at write validation.** `expr` and
  `value_expr` would compile fine on the write path (it reuses the read
  `Predicate` and `_compile_where`), which is exactly why leaving them unhandled
  was the risk: the write path independently re-derives that same WHERE three
  times — for the affected-row `COUNT(*)`, for the item-108 diff, and for the
  post-mutation row-cap re-check — and none of those has been reviewed against an
  expression's semantics. `validate_write_policy` therefore rejects it with a clean
  typed error, the same reject-at-the-validation-layer posture item 110
  established for `value_subquery`, rather than relying on it happening to work.
  **Accepted cost:** `UPDATE … WHERE price * qty > 100` is not expressible; scope
  the target rows with literal or column predicates, or compute the set with a read
  first. Widening writes is a separately-scoped future item — the engine plan is
  explicitly scoped to the read query engine, and quietly extending the write
  contract as a side effect of a read change is exactly the kind of drift the
  governed-writes decision record exists to prevent.
- **2026-07-24 — `having` and a CASE branch's `when` become full `WhereNode`s
  (TODO.md item 99), a deliberate breaking wire-format change.** `having` was
  `List[Predicate]` (implicitly AND-combined, no nesting) and `CaseWhen.when` was a
  single `Predicate`; both are now the same `WhereNode` union `where` already used
  (a `Predicate` **or** an `and`/`or`/`not` group). So `HAVING SUM(x) > 10 OR
  COUNT(*) < 3` and a searched `CASE WHEN a > 0 AND b < 5 THEN …` are expressible
  instead of forcing the caller to pre-filter in `where` or give up.
  **Why this is safe rather than new surface:** it *removes* a special case rather
  than adding a grammar. Both positions now compile through the one existing
  `_compile_where`, are walked by the one canonical reference visitor (item 96) via
  `_where_column_refs`, and are bounded by the caps that already existed —
  `max_where_depth` now also fires on a deep HAVING or CASE-condition tree, and
  `max_where_predicates` now counts HAVING predicates across the tree (not a flat
  list length) plus a new tree-wide `case condition predicate count` budget so a
  wide boolean CASE condition can't dodge the cap. No new policy field, no new
  dialect code (boolean logic is universal), and `value_subquery` stays WHERE-only
  (item 97) — now rejected even when buried inside a HAVING/CASE boolean group.
  Closed a latent gap on the way: `max_in_list_size` now also applies to an
  `in`/`not_in` inside a CASE condition, which the old single-predicate walk missed.
  **Accepted cost:** the wire shape changed — `"having": [{…}]` becomes
  `"having": {…}` (or `{"and": [ … ]}` for several conditions), and the audit
  event's `having` is now a nested shape emitted only when set, matching `where`.
  The Python client builder's `.having(...)` ergonomics are unchanged (multiple
  predicates/calls still AND-combine, exactly like `.where()`).
  **A backward-compatibility shim was considered and deliberately declined
  (maintainer-ratified 2026-07-24).** A `mode="before"` validator could have
  accepted the legacy list and folded it (`[X]` → `X`, `[X, Y]` →
  `{"and": [X, Y]}`), and that is the pattern item 100 plans for its legacy
  `col: str` aggregate form. It was rejected here because **no caller outside
  this repository sends `having`**: no `examples/`, doc, or landing-page JSON
  used it; the Python builder is unaffected; and MCP clients re-read the tool
  schema each session, so a fresh agent emits the new shape automatically. With
  no real migration burden to absorb, a permanent second accepted shape would
  buy nothing and cost the property that makes `extra="forbid"` meaningful —
  exactly one way to express exactly one thing. The break also fails **loudly**
  (a typed 422 at Pydantic validation), never silently, so a stale caller gets a
  clear error rather than wrong data. Precedent for future engine items
  (100–106): prefer the clean break while the AST has no external consumers;
  add a deprecating shim only once a real caller would be broken by it. This is the Phase 0
  warm-up of the Expressive Query Engine pillar
  ([docs/ENGINE_EXPRESSIVENESS_PLAN.md](ENGINE_EXPRESSIVENESS_PLAN.md)), proving the
  visitor/cap-expansion pattern on machinery that already existed before the larger
  items (100+) build on it.
- **2026-07-23 — REMOVED: the governed-write undo/compensation mechanism (item
  93). Reversibility is no longer a QueryGate capability; governed writes keep the
  preview → approve → attribute → audit governance tier.** This supersedes the
  earlier phase-3a/3b reversibility entries below (bounded reversibility, undo
  semantics, the Redis-backed compensation store, RETURNING capture) — retained
  as history, but they no longer describe shipping behavior. **Why removed.** Undo
  was the only part of the write feature that forced QueryGate to keep a second
  copy of the customer's *actual row values* outside their database (an
  in-process/Redis compensation store). That (a) placed unredacted sensitive data
  in a second store and expanded the compliance/attack surface — squarely against
  the North Star's least-privilege, data-never-leaves posture — and (b) opened a
  run of correctness landmines a security product cannot ship casually
  (capture/undo isolation races, blind-clobber of concurrent writes, store
  durability). Its marginal value is low once preview + approval exist: a human
  has already seen and approved the exact diff, so a rollback net is a
  nice-to-have, not the point. Removing it **reclaims the core security
  proposition — QueryGate never persists your data outside its source of truth**
  — while keeping the differentiator: a bounded, previewed, human-approved,
  attributed, audited write with no raw DML. **Removed:**
  `execution/compensation.py` + `execution/redis_compensation.py`,
  `WritePolicy.compensation_enabled`/`max_compensation_rows`/`compensation_ttl_seconds`,
  `POST /{connection}/write/undo`, the MCP `undo_structured_write` tool, and the
  `compensation_id` field on write results. **Unchanged:** preview + diff, gated
  execution, the approval gate, dual-identity + tamper-evident audit,
  deny-by-default, the affected-row cap. If durable reversibility is ever
  revisited, the correct path is the customer's *own* database history (temporal
  tables / CDC), never a QueryGate-owned copy — a fresh decision, not a revival of
  this mechanism.
- **2026-07-23 — Governed Writes Phase 3b: upserts (INSERT ON CONFLICT DO UPDATE)
  ship for Postgres/SQLite and are *rejected* on MSSQL — reject-not-emulate, no
  synthesized MERGE (item 93).** A new `UpsertStatement` (insert rows, but on a
  unique/PK conflict on `conflict_columns`, update `update_columns`) compiles to
  the native construct via a per-dialect compiler **registry**
  (`compiler/write_compiler.py` `_UPSERT_COMPILERS` — no inline `if dialect ==`,
  per the composable-interface rule). MSSQL has no `ON CONFLICT`; rather than
  emulate a `MERGE` the caller never expressed, it is rejected with a clear error
  pointing at the primitives (a separate governed update-then-insert) — the same
  item-74 posture as `array_agg` and MSSQL `NULLS`. Still no raw DML: the conflict
  target and updated columns are validated identifiers, and the values are bound
  parameters. Proven on real Postgres (insert-then-update-on-conflict) and MSSQL
  (rejection).
- **2026-07-23 — Governed Writes Phase 3b: reversibility's documented limits are
  closed — serial-PK inserts are undoable (RETURNING), undo works under HA (a
  Redis-backed compensation store), and an UPDATE undo refuses on drift instead
  of silently clobbering (item 93).** **(3) Optimistic concurrency.** An UPDATE
  undo reads each affected row's current changed-column values and **refuses**
  (422) if any differs from what the write set — or the row no longer exists — so
  a concurrent change since the write is never silently overwritten; the pre-3b
  behavior clobbered the snapshot value. The phase-3a self-review left
  two honest limitations; phase 3b removes them. **(1) RETURNING capture.** An
  INSERT that omits a single-column PK (serial/identity — the common case) now
  executes with `RETURNING pk` in the same transaction, so its generated keys are
  captured and the insert is undoable (was `compensation_id=null`). Supplied-PK
  inserts are unchanged. **(2) `RedisCompensationStore`.** The `CompensationStore`
  is now async + pluggable (mirroring the concurrency/quota limiters); when
  `CONCURRENCY_BACKEND=redis`, `create_app` installs a Redis-backed store, so a
  `compensation_id` minted on one replica is resolvable on every replica and undo
  works under the multi-replica HA deployment (item 56) — closing the self-review
  #1 gap. The pre-image round-trips through Redis as JSON (datetime/Decimal become
  strings; the compiler's `_coerce_write_value` restores their Python types on
  undo — verified end-to-end). Sensitivity is stated honestly: the Redis store
  holds real pre-image values, so it is secured like the concurrency/quota Redis
  (network isolation + auth; TTL bounds exposure; field-level encryption-at-rest
  is a further hardening); the redaction-safe audit still carries only the id +
  counts.
- **2026-07-23 — Governed Writes undo semantics (item 93 phase 3a hardening):
  undo is atomic, restores only the changed columns, and is authorized by the
  compensation id rather than re-running the approval/op gates.** A self-review
  of the first undo cut surfaced real gaps; the resolutions are deliberate, not
  incidental. **(1) Undo is atomic all-or-nothing.** All inverse statements run
  in ONE transaction (`_apply_undo_atomically`), committing once — a failure
  reverses nothing and does not consume the compensation record, so undo upholds
  the same "no partial write" guarantee the forward path does. (The first cut ran
  each inverse through its own `execute()`/commit, which could half-reverse a
  multi-row UPDATE and orphan the record.) **(2) Undo restores only the columns
  the original write changed**, captured as `changed_columns` (the UPDATE's `set`
  keys) — so an undo's blast radius never exceeds the change it reverses, and a
  concurrent change to an untouched column is preserved. It still restores the
  *snapshotted* value of those columns (a concurrent change to a changed column
  since is overwritten — a documented bounded limit; optimistic-concurrency
  detection is a later refinement). **(3) Undo is authorized by the
  compensation id**, which is single-use, TTL'd, connection-scoped, and only ever
  returned to whoever executed the original governed write. Undo therefore does
  NOT re-trigger the approval gate (the forward write was already approved; undo
  restores the lower-risk prior state — otherwise exactly the high-impact writes
  you most want to reverse would be un-undoable) and does NOT re-check the
  inverse op against `allowed_operations` (undoing a DELETE is an INSERT;
  requiring INSERT to be separately enabled would make reversibility unusable).
  It still enforces deny-by-default (writes enabled + table writable), the
  affected-row cap, schema truth, and full audit (`undo_structured_write`).
  **Known bounded limits, documented for callers, not hidden:** the compensation
  store is process-local, so undo needs single-replica or session affinity
  (durable cross-replica store is phase 3b — see `deploy/HA_DR.md`); and an
  INSERT with a server-generated PK returns `compensation_id=null` (not
  undoable without RETURNING capture — supply the PK to make it reversible).
- **2026-07-23 — Governed Writes Phase 3a: bounded reversibility (undo) stores
  pre-images in a QueryGate-owned compensation store, NOT an in-DB shadow table,
  and undoes by re-applying through the governed write pipeline (item 93).** The
  "reversible" pillar. Two storage designs were on the table; the choice is the
  gate this phase required. **(1) Rejected: an in-DB shadow table.** Snapshotting
  pre-images into a table in the customer's operational database would keep undo
  transactionally atomic with the write, but it demands DDL + write privilege on
  the operational DB *beyond* the governed-write target tables — a large,
  invasive expansion of QueryGate's footprint that contradicts the North Star
  (least-privilege, we don't own or mutate your storage's shape). **(2) Chosen: a
  QueryGate-owned, bounded, TTL'd compensation store.** Before a gated mutation
  commits, QueryGate captures a bounded pre-image of the affected rows (UPDATE/
  DELETE) or the inserted keys (INSERT) into its *own* store (a `CompensationStore`
  protocol, in-memory default, mirroring the audit-sink pattern), and returns a
  `compensation_id`. Undo (`POST /write/undo`, scope-gated) reads that record and
  re-applies the inverse **as an ordinary governed write** — a DELETE-undo
  re-INSERTs the captured rows, an INSERT-undo DELETEs by key, an UPDATE-undo
  restores the old values by primary key — so the undo is itself validated,
  capped, and audited; no new privilege or bypass path exists. **Honest limits,
  documented, not hidden:** bounded reversibility only — it cannot unwind
  cascading triggers/FK actions or side effects, and a downstream consumer may
  already have read the changed value; a snapshot is capped by policy
  (rows/bytes) and expires (TTL). **Redaction posture:** a pre-image necessarily
  holds real row *values* (you cannot restore what you redact), so the
  compensation store is a distinct, access-controlled, short-lived store — the
  redaction-safe **audit** stream still carries only the `compensation_id` +
  counts, never the values, preserving that invariant.
- **2026-07-23 — Governed Writes Phase 2b: the dry-run preview gains the bounded
  old→new row *diff* — the one place a preview deliberately shows values, kept
  safe by being bounded, masking-aware, and never audited (item 93).** The
  headline governed-writes feature: `POST /write/preview?include_diff=true`
  returns exactly which rows an INSERT/UPDATE/DELETE would change and how
  (`before`/`after` per row), computed by running the DML inside a transaction
  and **rolling it back** — so even the diff mutates nothing. This is the only
  QueryGate surface that returns row values, which is the point (a human approves
  *this specific change*), so three guards make it safe and were chosen
  deliberately: it is **bounded** by `WritePolicy.max_diff_rows` (a preview can
  never dump a table — a larger affected set comes back `truncated`); it is
  **masking-aware** (a column the read policy masks is redacted to `***MASKED***`
  in the diff, so the value-bearing preview can't become a masking bypass); and
  it is **transient to the caller only** — the redaction-safe audit event still
  carries counts/shapes, never these values. UPDATE old→new is read back by
  single-column primary key after the in-txn DML (real committed shape,
  DB-side effects included), falling back to applying the SET in Python for a
  composite/absent PK. Opt-in per request (`include_diff`), so the default
  preview stays a cheap count.
- **2026-07-23 — Governed Writes Phase 2a: gated write *execution* is enabled
  (maintainer-approved), still deny-by-default and with no raw DML — a write
  commits only when in-policy, capped inside its own transaction, atomic, and
  audited (item 93).** Phase 1 shipped the write contract + dry-run preview with
  execution disabled; the read-only line is now crossed for *execution*, on
  explicit maintainer approval, for single-table INSERT/UPDATE/DELETE. The whole
  point is that crossing it changes the safety *surface* as little as possible —
  a write reuses the exact validate → policy → schema → compile spine reads use,
  so the guarantees are structural, not bolted on. What Phase 2a guarantees, and
  proves in tests: **(1) Deny-by-default.** `WritePolicy.enabled` is false out of
  the box and a write needs its table in `allowed_tables` and its op in
  `allowed_operations`; a default deployment cannot write at all. **(2) No raw
  DML, ever.** Execution runs the *same* validated `write_ast` → `compile_write`
  Core statement the preview compiles — there is no raw-SQL field or string path,
  and an injected value can only ever populate a bound parameter. **(3) The
  affected-row cap is enforced *inside the transaction*.** The service counts
  matched rows in the same transaction that will mutate them and aborts (rolls
  back) before mutating if the count exceeds `max_affected_rows` — so a
  concurrent insert can't push a write over its cap between preview and execute.
  **(4) One transaction, no partial write.** Each write runs in a single explicit
  transaction; any error (validation, cap, DB, deadlock) rolls the whole thing
  back and surfaces a clean typed error — never a half-applied mutation.
  **(5) The approval gate extends to writes.** Reusing item 92's machinery, a
  write whose affected-row count crosses `WritePolicy.require_approval_over_rows`
  pauses for a human: REST returns `428` with the write's fingerprint, and a
  `query:approve`-scoped grant issues a fingerprint-bound token to resubmit —
  the agent can't approve its own write. **(6) Redaction-safe, dual-identity,
  tamper-evident audit.** The write reuses `audit_query` (operation
  `execute_structured_write`) so it inherits per-human attribution (item 90) and
  the hash-chained ledger (item 91); the event carries the op, table, affected
  count, and *parameterized* SQL — never a SET value, predicate literal, or row.
  **Deliberately deferred to 2b/3** (each its own slice, so this one stays
  reviewable): the row-level old→new diff preview, the MCP `run_structured_writes`
  execute tool, the write concurrency load gate + `release-smoke` write round-trip,
  compensation/undo (bounded reversibility), upserts/multi-row batch, and MSSQL
  execution parity. The claim remains *governed* writes — bounded, previewed,
  approved, attributed — never "safe autonomous writes."
- **2026-07-23 — The MCP elicitation approval channel is opt-in and off by
  default: a client-human's in-session elicitation response counts as an
  approval only when the operator explicitly enables it (item 92).** Completing
  the in-query approval gate, the interactive MCP channel was built so a gated
  query can be approved *within the querying session* via `Context.elicit`
  instead of the out-of-band REST `query:approve` token round-trip. The
  security question this settled: an elicitation response carries **no
  authenticated approver identity** — it is a form answer from whoever operates
  the client — so treating it as an approval is a deliberate trust decision, not
  a default. Three choices were made in the open. **(1) Off by default
  (`MCP_ELICITATION_APPROVAL_ENABLED=false`).** REST enforces separation of
  duties structurally (`query:approve` is a distinct scope, so an agent can't
  approve its own read); the elicitation channel trades that structural
  separation for a human-in-the-loop one, so an operator must opt in. Left off,
  a gated MCP query stays fail-closed and the only approval path is the REST
  token flow. **(2) Separation of duties is preserved by the medium, not a
  scope.** The querying agent physically cannot satisfy its own gate here —
  only a *human* answering the client's elicitation prompt can — so the agent
  can't self-approve even though the approval happens in its own session. That
  is the whole point of elicitation as the HITL primitive. Deployments where the
  client's human is *not* a trusted approver leave the channel off. **(3) The
  minted token is bound and attributed.** On approval the server mints the same
  fingerprint-bound, short-lived HMAC token the REST flow issues (so it can't be
  reused for another query), with `approver_subject` recorded as
  `mcp-elicitation:<caller>` to mark in the audit trail that approval came from
  an interactive session, not a scoped `query:approve` grant. The channel plugs
  into `execute_many` through a narrow injected `ApprovalResolver` callback, so
  the execution service never imports MCP and the batch/error logic stays in one
  place. Still opt-in, read-only, AST-only — a default deployment is unchanged.
- **2026-07-23 — Governed Writes Phase 1 (contract + dry-run preview, execution
  DISABLED) is approved and built; the read-only line is crossed for *preview
  only* (item 93; maintainer-approved).** The decision to cross read-only was
  made explicitly for Phase 1's zero-write-risk surface. What ships: a
  `write_ast/` contract (`InsertStatement`/`UpdateStatement`/`DeleteStatement`,
  a discriminated union dispatched via a type registry — **no raw-DML field of
  any kind**; SET-values and predicates reuse the *existing* read AST surface —
  whitelisted scalar functions + the `Predicate` filter tree), a `WritePolicy`
  (opt-in `writes_enabled`, per-table allowed operations, allowed write
  columns, `max_affected_rows`), write policy + schema validation mirroring the
  read validators, a write compiler, and a **dry-run diff engine** that runs the
  compiled write inside a transaction, computes a bounded before/after diff, and
  **ROLLS BACK** — no code path can commit. Two structural guarantees carry the
  safety story: (1) `UPDATE`/`DELETE` **require** a WHERE clause (an unqualified
  mutation is impossible by construction, not by lint), and (2) the affected-row
  count is capped by policy. The claim is deliberately bounded — "governed
  writes: bounded, previewed, approved, attributed, reversible", never "safe
  autonomous writes" or a "100%". **What is NOT in Phase 1:** any execution/
  commit path (Phase 2, gated on items 90/91/92), compensation/undo (Phase 3),
  upserts/batch/MSSQL-parity (Phase 3). The preview is immediately useful on its
  own as a "what would this change?" planner and validates the whole
  AST/policy/compiler design before any real mutation.
- **2026-07-23 — Reversed: `connections/dialects.py`'s inline dialect branching
  is now a `SessionDialectAdapter`, a SEPARATE async abstract base from the sync
  compiler `DialectAdapter` (item 57; maintainer-approved).** A prior decision
  kept `connections/dialects.py`'s `if dialect == ...` branching as a deliberate
  "lighter-weight" exception to the composable-interface doctrine. With dialect
  breadth (item 19) now a priority, that exception is reversed: engine-URL/
  connect-args/query-timeout/session-guardrail behavior is formalized as one
  concrete `SessionDialectAdapter` per dialect, dispatched via a registry, so
  adding a dialect is "implement + register", not "find every inline branch".
  **The deliberate part of the reversal is keeping it a *separate* ABC from the
  compiler's `DialectAdapter`, not merging them:** the session adapter is *async*
  (it runs `SET ...` on a live session and hooks pool `connect` events) while the
  compiler adapter is *sync* (it builds SQL expressions) — one interface spanning
  both execution models would be awkward, so the pattern is reused (per-dialect
  class + registry) but the two layers stay distinct. Behavior-preserving (the
  module functions are kept as thin dispatchers; proven by the unchanged
  `test_dialects.py` plus a new registry test). The cost-estimation hook the
  item also mentions was Postgres-only at this date, and was the one documented
  inline-branch exception. *(Superseded: item 26 phase 2 shipped MSSQL
  `SHOWPLAN_XML` estimation, and `_estimate_cost` is now a per-dialect dispatch,
  so the exception is resolved — CLAUDE.md records it as such.)*
- **2026-07-23 — Bounded nested subqueries are added as a recursive AST node with
  caps enforced TREE-WIDE, not per-level, and only the uncorrelated/single-
  connection/depth-capped subset (item 97; maintainer-approved).** The AST gains
  caller-authored nesting for the first time — a `Predicate.value_subquery` (an
  `IN (subquery)` value set that is itself a full validated `StructuredQuery`),
  phase 1. This is a deliberate capability expansion approved by the maintainer,
  and it crosses **no** North Star non-goal: a subquery is still a fully
  validated AST, never a raw-SQL string. Three decisions make it safe. **(1)
  Caps sum tree-wide.** Every count-based cap (`max_select_columns`, `max_joins`,
  `max_group_by`, `max_where_predicates`, in-list size, `top_n`) is enforced on
  the **sum across the whole query tree**, and `max_where_depth` per query, so
  nesting can never be used as a cap-multiplier bypass (the exact attack this
  node introduces). A new `max_subquery_depth` (default 1) bounds nesting itself.
  **(2) The canonical visitor (item 96) descends into subqueries**, so column
  allow/deny and the masked-column rule apply to a subquery's base-table
  references automatically — a denied/masked column can't hide one level down.
  Each subquery is an **independent scope**: its column refs resolve to its own
  from/join tables (a virtual relation), never the outer's, which is *also* what
  makes it structurally uncorrelated. **(3) Reject, don't emulate** (the item-74
  precedent): a **correlated** subquery (inner references an outer row), a
  **cross-connection** subquery, and an **over-depth** subquery are each rejected
  with a `QueryValidationError` pointing at the primitive to use instead (joins;
  a separate per-connection query; a shallower shape) rather than being
  half-supported. Phase 1 ships `IN (subquery)`; `FROM (subquery)` (a derived
  table the outer selects from) is phase 2, because it additionally needs the
  outer query to resolve against the inner's *output* aliases without reaching
  past them into inner base tables. Full local adversarial coverage
  (`tests/security/`) proves the caps and allow/deny hold through nesting;
  MSSQL SQL-rendering parity (mechanical, not a security surface) is CI-validated.
- **2026-07-23 — Four-eyes config approval is enforced server-side, author≠approver
  is structural, and rollback is exempt from the gate (item 42 phase 1).** Adding
  separation of duties to the config plane, three decisions were made. **(1)
  Enforcement lives in the store + `apply`, never only the UI.** The store refuses
  to record a version author's own review and refuses a review of a non-staged
  version; `apply` refuses a staged version's first activation until it has the
  configured number of valid approvals. Simulating four-eyes in the browser while
  the server still permits self-approval (the anti-pattern item 42 explicitly
  names) is impossible because the browser isn't in the enforcement path. **(2)
  N approvals means N *distinct* reviewers, bound to content.** A reviewer's
  latest decision supersedes their own earlier one (no stacking), and each
  approval carries the version's content fingerprint so it can never count for
  different content. **(3) Rollback is exempt.** The gate applies only to a staged
  version's *first* activation — reactivating a previously-active version (DR /
  rollback) is never blocked on re-approval, because it was approved when first
  applied and blocking recovery on a quorum would be unsafe. Backward-compatible
  by default: `require_config_approvals` defaults to 0 (single-administrator mode)
  and manifests written before the `approvals` field load unchanged. The
  CLI/admin-UI review flows are phase 2; the authorization core is done.
- **2026-07-23 — The in-query approval gate uses a stateless, fingerprint-bound
  HMAC token and a scope separate from execution; phase 1 triggers only on the
  cost estimate (item 92).** Building the human-in-the-loop gate, three
  decisions were made deliberately. **(1) Stateless token, not an approval
  store.** An approval is an HMAC-SHA256 signature over `{query fingerprint,
  approver, expiry}` — no server-side pending-approval table. It cannot be
  forged (keyed HMAC), cannot be replayed against a *different* query (the
  fingerprint is a SHA-256 of the whole AST, so a one-character change
  invalidates it), and cannot be replayed forever (short expiry). Verification is
  fail-closed: a missing key, forged signature, expired, mismatched, or malformed
  token all deny. This avoids adding statefulness to a security product and keeps
  the gate horizontally-scalable with no shared approval state. **(2) `query:approve`
  is a distinct scope from querying.** The agent that runs the query must not be
  able to approve its own sensitive/expensive read, so approval is a separate
  scope (a "Query Approver" role) — separation of duties by scope, the same
  posture as catalog governance. **(3) Two triggers, one decision.** The gate
  fires on either the cost/row estimate (Postgres, reusing the estimate the
  pipeline already computes) **or** a catalog sensitivity label — a query
  referencing a `pii`/`confidential`/`internal`-labelled column (found via the
  item-96 canonical visitor, so a sensitive column in *any* clause counts, and
  resolving only the static descriptive label, never a row value). Both fold into
  one set of reasons so a single approval token covers whatever tripped it, and
  it rejects with `428 Precondition Required` + fingerprint/reasons. The
  sensitivity trigger is dialect-agnostic (works on MSSQL, no estimate needed).
  The same fingerprint→token flow extends to **batch** (`POST /query/batch`
  carries a `fingerprint → token` map): a batch can run an approval-gated query
  while an unapproved one in the same batch stays fail-closed as that item's
  error, and each token is still bound to its own query's fingerprint so it
  can't be replayed onto another query in the batch. The remaining phase-2 piece
  is the interactive MCP elicitation channel. The whole gate is opt-in per policy
  and off by default, so it changes nothing for an existing deployment,
  preserving the read-only, AST-only invariant (it only *adds* a pre-execution
  pause).
- **2026-07-23 — RFC 9728 `scopes_supported` advertises the full scope
  vocabulary, kept distinct from the `mcp_required_scopes` access gate; role
  bundles are advisory and generated, never enforced or hand-maintained (item
  95).** Making "bring your IdP" turnkey required publishing QueryGate's scope
  vocabulary. Two boundaries were drawn deliberately. **(1) Discovery ≠ gate.**
  `scopes_supported` now lists the entire vocabulary an IdP might mint tokens
  for (`core/scopes.py`'s `ALL_SCOPES`, unioned with any custom required scope),
  while `mcp_required_scopes` stays exactly as-is as the enforced MCP access gate
  — conflating the two (the prior behavior published only the gate) would have
  told IdPs a token needs *only* the MCP scope, hiding every admin/catalog scope
  they must also be able to issue. **(2) Roles are guidance, data-access is
  not a scope.** `ROLE_BUNDLES` (Analyst/Operator/Config Governor/Catalog
  Author/Catalog Admin/Catalog Data Steward) are advisory groupings QueryGate
  never enforces — it enforces individual scopes — and an Analyst deliberately
  carries *no* scope, because which tables/columns a principal may read stays in
  `policy.yaml` keyed by `sub`/claim, never in the IdP. Both the wire metadata
  and `docs/SCOPE_CATALOG.md` are generated from `core/scopes.py` with a drift
  test (`test_scope_catalog.py`), so a new scope constant that isn't catalogued
  fails CI rather than silently going undiscoverable. No auth-model change, no
  QG-owned identity store.
- **2026-07-23 — The multi-replica config-reload path is GitOps + a rolling
  restart, not a cross-replica broadcast; per-principal quota stays honestly
  per-replica (TODO.md item 56).** Building the HA/DR story, two boundaries were
  decided in the open rather than papered over. **(1) Config propagation.** The
  governance API's `apply()` reloads only the in-process registries of the single
  replica that served the request; there is no reload fan-out. Rather than build
  a cross-replica broadcast (a new distributed-coordination surface in a security
  product), the chart makes `helm upgrade` the multi-replica path: a
  `checksum/config` pod annotation rolls every replica behind the readiness gate
  with `maxUnavailable: 0`. The governance API remains correct for single-replica
  or staging; the optional RWX `configGovernance` PVC shares *history* across
  replicas but still requires a roll to propagate an apply — documented, not
  hidden. **(2) Quota honesty.** *(Superseded — item 50 phase 2 has since shipped;
  retained as the record of the decision made while it had not. Current state:
  the quota is a true fleet-wide budget under `CONCURRENCY_BACKEND=redis`, per
  `deploy/HA_DR.md`'s matrix.)* At the time, the in-flight concurrency cap was
  fleet-wide via Redis, but per-principal rate/byte quotas (item 50) were still
  per-replica. We chose to state
  the N× multiplication plainly in `deploy/HA_DR.md`'s shared-state matrix and
  size guidance around it, rather than imply a cross-fleet budget the code did
  not yet enforce. The rejected alternative — quietly shipping the HA overlay and
  letting operators assume quotas were global — was declined because a security
  product's operational claims have to match what the code does. Chart invariants
  are asserted against a real `helm template` render
  (`tests/unit/test_helm_ha_deployment.py`) so these guarantees can't drift.
- **2026-07-23 — Signed delivery attests the container image (the thing we
  actually publish), not the Python package; publishing stays a manual tag push
  (TODO.md item 30/89 phase 2).** Completing the signed-delivery gate, two
  choices were recorded. **(1) Attest what is published.** The release workflow
  signs the GHCR image with cosign keyless (Sigstore) and attaches a SLSA
  build-provenance attestation, both bound to the immutable digest — because the
  container image is how QueryGate is distributed. The Python wheel/sdist are
  *not* signed/published: no package index (PyPI/private) is chosen, so signing
  them would attest an artifact nobody pulls. They are covered by
  `dist/SHA256SUMS` + `make verify-release` (offline integrity) until an index is
  chosen. This resolves item 30's earlier "signing against nothing is theater"
  concern — there is now a real published artifact (the image) to sign. **(2)
  Publishing is never automatic.** The signing/provenance runs only on a
  maintainer's deliberate `v*` tag push, never on a `main` commit, so a release
  is always an intentional act. The rejected alternative — auto-publish on merge
  — was declined because a security product's release must be a deliberate,
  gated decision, not a side effect of merging. The consumer verifies with
  `cosign verify` / `gh attestation verify` (commands in `docs/RELEASING.md`).
- **2026-07-23 — The published adversarial benchmark's raw-SQL baseline is a
  declared structural model, not a live competitor run (TODO.md item 58,
  phase 1).** Turning the internal adversarial suite into a *publishable*
  comparison forced a choice about what to measure the raw-SQL baseline against.
  **Decision:** phase 1 declares, per corpus case, whether a naive gateway that
  forwards a model-generated SQL string with no AST contract would block the
  attack — grounded in a structural fact (such a gateway has no per-query
  table/column policy and no parameter-binding contract), so it is factual and
  reproducible without running any competitor. The runner drives QueryGate's
  *real* guardrails (policy validation + compiler parameter binding + the
  no-raw-SQL AST introspection) offline and deterministically, and the corpus
  discloses the documented inference residuals it does *not* block rather than
  counting only wins. **Rejected (for phase 1):** a live head-to-head that
  executes the corpus against a real LLM composing raw SQL and a comparably
  configured Google MCP Toolbox deployment. It was deferred, not declined —
  a fair live run needs a model-provider decision and a GCP/Toolbox environment,
  and publishing head-to-head numbers off a misconfigured competitor setup would
  risk misrepresenting a documented capability, which this project forbids. So
  the live baseline is scoped as phase 2; the Toolbox comparison in phase 1 is
  capability-level, drawn from Toolbox's documented design. See
  `docs/business/SECURITY_BENCHMARK.md`.
- **2026-07-22 — The tamper-evident audit ledger chains at the sink/envelope
  layer, not on the event model (TODO.md item 91, F5).** Building the
  hash-chained ledger, the choice was where the `prev_hash`/sequence/`hash` live.
  **Decision:** they live on a chain *envelope* (`audit/ledger.py`'s
  `LedgerRecord`, written by `HashChainedAuditSink`) that wraps the unmodified
  event body — `{seq, prev_hash, event, hash}` — never as new fields on
  `AuditEvent` (or the other three event models). **Why:** (1) *Redaction
  invariant stays trivially true.* The embedded `event` is byte-for-byte the same
  `model_dump(exclude_none=True)` the plain JSONL sink already writes; the chain
  adds only a sequence number and hashes, so there is no new place for customer
  data to leak and the QG-12 guarantee needs no re-proof. (2) *One chain over all
  four event types.* Query, config-governance, catalog-governance, and probe
  events already share one sink; chaining at the sink covers the whole trail with
  one head, whereas per-model fields would fragment it. (3) *No circular hashing.*
  A hash field on the model would have to hash the model excluding itself;
  enveloping avoids that entirely. The one cost — a second reader shape — is paid
  by a three-line unwrap in the item-59 anomaly reader (`admin/anomaly.py`), which
  now transparently reads both bare and enveloped lines. Integrity is honestly
  tiered: **keyed** (`AUDIT_LEDGER_HMAC_KEY`) → HMAC-SHA256, unforgeable without
  the key; **unkeyed** → SHA-256, tamper-evident only against an externally
  anchored head (`verify --expected-head`). Chaining is verify-only (nothing in
  the request path reads it) and assumes a single logical writer owns the head —
  documented as a single-replica / per-replica-file constraint rather than
  engineered into a distributed-consensus ledger, which would be scope no design
  partner has asked for. Per-query **receipts** (`querygate-audit receipt`) fall
  out for free: a receipt is just one `LedgerRecord` re-verifiable on its own.

- **2026-07-22 — A GraphQL query interface is a permanent non-goal.** Prompted by
  the "GraphQL is more flexible than REST — would it broaden QueryGate from an AI
  gateway into a universal data-access guard?" question. Rejected on two grounds.
  (1) **It adds no expressiveness.** The REST and MCP transports already carry the
  *full* `StructuredQuery` AST; the expressiveness ceiling is `AST + policy`, not
  the transport envelope. GraphQL-over-the-same-AST is no more capable than
  REST-over-the-same-AST — a different envelope for identical semantics. A *real*
  GraphQL engine (per-field resolvers hitting the database) is strictly worse: it
  is a second query-execution path *beside* the one pipeline, bypassing policy →
  schema → compile → concurrency → audit, which violates the "no other path to a
  database" invariant — the same class of rejection as `execute_sql` and
  generated-code execution. (2) **Brand collision.** "GraphQL over your database"
  is already a category (Hasura engine, PostGraphile, Supabase auto-APIs) — and it
  is a *developer-productivity* category, not a security one. Adopting it makes
  buyers evaluate QueryGate on flexibility (where it should not compete) instead
  of on the query-semantic guarantee (its moat). The legitimate instinct behind
  the question — *be the enforcement point guarding all access within a client's
  architecture* — is already served on-thesis by the **P4 verdict endpoint**
  (`NORTH_STAR.md` leverage move #1: any front door, gateway, or app calls
  QueryGate for the query-semantic verdict it cannot compute itself) and by the
  **sole-credential-holder deployment** (below), neither of which requires a new
  query language or broadens the AI-agent wedge. Reaching non-AI *apps* is a
  later adoption vector via the typed client SDK (TODO.md item 51) emitting the
  same AST — an adoption lever after the design-partner proof, never a
  repositioning of the North Star.
- **2026-07-22 — Analytics performance is served by DB-side materialized views +
  query templates, NOT by an in-product cache or stored-procedure execution.**
  Prompted by the recurring "stored procedures run faster — why not do that?"
  question. Executing stored procedures is a permanent non-goal: an SP is a
  stored blob of raw procedural SQL, so invoking one reopens the exact
  raw-SQL/arbitrary-code path QueryGate exists to remove. The performance concern
  is answered without it, along two lines. (1) The heavy "pre-compute the work"
  win belongs in the customer's database: a DBA builds a **materialized view**
  (plus indexes/partitioning), and QueryGate reads it as an **ordinary table
  today — zero product change** — so the expensive aggregation runs on the DBA's
  refresh schedule, not per request; a policy + query template then govern access
  to the pre-aggregated table. The database owns physical optimization; QueryGate
  owns the safe boundary. (2) The one durable SP benefit that isn't SP-exclusive
  — cached execution plans (skip re-parse/re-optimize on repeated calls) — is a
  **prepared/parameterized-statement** benefit; we already compile with bound
  parameters and templates are fixed-shape, so we likely already capture much of
  it, and verifying/tuning it is tracked as the measure-first TODO.md item 94.
  We deliberately do **not** build an in-product result cache or pre-aggregation
  cache (that would make QueryGate a stale-data caching engine and duplicate what
  the DB already does well). See `docs/business/COMPETITOR_CUBE.md` for the
  companion competitive framing.
- **2026-07-22 — Delegated agent identity maps the *human* to `Principal.subject`
  and the *agent* to a new `Principal.actor` chain (TODO.md item 90, phase 1).**
  The two-identity, on-behalf-of model (RFC 8693 token exchange; the MCP
  `2026-07-28` authorization revision) needs QueryGate to carry both the agent
  and the human into enforcement and audit. The design choice that made this
  cheap and correct: because per-principal policy resolution already keys off
  `Principal.subject` (`policy/loader.PolicyStore.get`), we map the **human** —
  the one whose policy and `mandatory_row_filters` must bind — to `subject`, and
  add the agent as a separate `actor` (a nested `Actor` chain built from the
  verified JWT's RFC 8693 `act` claim). The result: the human's policy applies
  with **zero change to the policy layer**, an agent can never exceed the access
  of the person it acts for, and the audit event records both (`principal_id` =
  human, `actor_id`/`delegation_chain` = agent) while staying redaction-safe
  (identities, never credentials). This is deliberately the *attribution* slice
  (phase 1); full MCP OAuth resource-server conformance — RFC 9728 protected-
  resource metadata, mandatory RFC 8707 audience validation, `insufficient_scope`
  step-up — is phase 2, scoped separately in TODO.md item 90 so the moat (which
  competitors' warehouse-coupled gateways can't reach on Postgres/MSSQL) ships
  without waiting on transport-spec work.
- **2026-07-22 — Governed Writes (TODO.md item 93) is designed and specced but
  DECISION-PENDING; it must not be implemented until a maintainer explicitly
  approves crossing the read-only line.** *This entry records the design intent
  and the framing so the eventual go/no-go decision has a durable anchor; it
  will be updated to "approved" (with date and any scope changes) or "declined"
  when the decision is made.* The market's biggest unsolved problem is safe
  agent writes — everyone retreated to read-only-by-default because LLM-authored
  DML is unsolved (Neon read→write hijack, Supabase removed the write channel,
  AWS ships a "best-effort, bypassable" write blocklist). QueryGate's read-only
  posture is the launchpad, not a weakness: the same AST-validation spine that
  makes reads safe makes a `StructuredWrite` contract (typed insert/update/
  delete, **no raw DML field anywhere**) possible, carried through the same
  validate → policy → schema → compile → preview → approve → execute → audit
  pipeline. **The deliberate framing, and the line we will not cross:** the
  product claims **governed writes — bounded, previewed, approved, attributed,
  reversible** (no raw DML, no unqualified UPDATE/DELETE ever, bounded affected-
  row count, only allowed ops on allowed targets — which eliminates the
  *catastrophic-shape* class of write by construction), and it explicitly does
  **not** claim "safe autonomous" or "provably correct" writes. No structural
  layer can make a well-formed, in-policy write with the *wrong values* correct;
  that residual is made *reviewable and reversible* (dry-run diff preview,
  mandatory approval on the diff for sensitive/large writes, dual-identity
  audit, bounded compensation/undo), never claimed away — we will never repeat
  PromptQL's unbackable "100%". Reversibility is explicitly **bounded** (a
  pre-image snapshot cannot unwind cascading triggers/FK actions or
  already-consumed reads). **Open decisions to resolve before Phase 1 starts:**
  (a) confirm crossing read-only now vs. later; (b) compensation storage
  location + retention (in-DB shadow table vs. sink-backed pre-image), which must
  stay redaction-safe; (c) whether Phase 1's preview tool (execution disabled)
  ships publicly on its own as a "dry-run planner" ahead of any execution; (d)
  REST approval-token vs. MCP-elicitation parity expectations. **Invariants
  preserved regardless:** no caller-controlled raw SQL/DML ever reaches a
  database (a write is a validated structure, exactly as a read is); audit stays
  redaction-safe (counts/shapes/hashes, never values/rows); the catalog stays
  descriptive; all dialect variance goes through `DialectAdapter`
  (reject-not-emulate). Phasing keeps risk gated: Phase 1 (contract + dry-run
  preview, execution **disabled**) carries zero write risk and depends on nothing
  beyond today's code; Phase 2 (gated execution) depends on items 90+91+92;
  Phase 3 (compensation/undo + upserts + batch + MSSQL parity) depends on
  Phase 2.
- **2026-07-22 — Security validation is enforced by open-source CI gates and
  surfaced as a reproducible, buyer-facing posture rather than marketing claims
  (TODO.md item 89).** Added SAST (Bandit + Semgrep OSS), full-history secret
  scanning (gitleaks), container image scanning of the shipped image (Trivy), and
  OpenAPI fuzzing (Schemathesis), each **deny-by-default** and mirroring the
  established pip-audit/SBOM allowlist pattern, plus `SECURITY.md` and a
  customer-facing `docs/SECURITY_POSTURE.md`. Three deliberate choices. **(1)
  Prove, don't assert.** Every posture claim points at a command a reviewer can
  run and a gate that fails CI on regression — a security product's claims must
  be checkable, not conventional. **(2) Deny-by-default everywhere — fix, don't
  accept.** Each scanner fails the build on any finding. The real CVEs the
  dependency and image scans surfaced were remediated by upgrade, leaving the
  pip-audit allowlist and `.trivyignore` **empty**; the only recorded exceptions
  are dev-only secret-scan placeholders (`.gitleaks.toml`) and a few reviewed,
  inline-justified SAST suppressions (`# nosec` / `# nosemgrep`), never silent.
  **(3) Honest about badges.** The OpenSSF Best Practices badge is FLOSS/public-
  repo-only, so a private product cannot be *awarded* it; rather than display a
  badge we can't earn, we keep an honest criteria self-assessment (all
  Quality/Security/Analysis criteria Met by real gates) and identify signed
  delivery (Sigstore/cosign + SLSA provenance) as the genuinely-earnable external
  attestation for a self-hosted image — the one real gap, tracked as item 89
  phase 2. The scanning tooling itself adds no runtime dependency (all dev/CI-only)
  and the security invariants are untouched. Where the new gates surfaced real
  findings they were **fixed, not accepted**: the container/dependency scans caught
  known CVEs in the shipped set, so those dependencies were upgraded to fixed
  versions (fastapi/starlette 1.x, mcp 1.28, python-dotenv, click, idna — the
  work item 30 phase 2 had deferred) and build/install tooling (pip/setuptools/
  wheel) was stripped from the runtime image, leaving **both the pip-audit
  allowlist and the Trivy exception list empty**. Schemathesis is deliberately
  kept *out* of the dependency lock (run from its pinned Docker image) because its
  transitive pins conflict with both those runtime fixes and the project's test
  stack — a dev testing tool must never constrain the shipped graph. DAST is a
  standalone hermetic script (no DB, no fixtures); the recursive query endpoints
  are excluded from generic schema fuzzing (Schemathesis #947) because
  `test_malformed_input_fuzzing.py` already fuzzes that exact surface more deeply.
- **2026-07-21 — The `min_group_size` k-anonymity guardrail suppresses
  small aggregate groups by injecting `HAVING count(*) >= k`, rather than
  rejecting the query or restricting the AST (TODO.md item 88).** It closes
  the single-query singling-out form of item 55's R3 residual (a caller
  aggregating over a razor-thin filter to isolate one individual). Two
  deliberate choices. **(1) Suppress, don't reject.** QueryGate cannot know a
  group's actual size without running the query, so a static "reject
  aggregates that *could* be small" rule would reject nearly every aggregate;
  instead the compiler injects a `HAVING` floor so under-*k* groups are
  dropped by the database and compliant ones still return — the exact
  precedent set by mandatory row filters (policy-driven structure injected
  into the compiled statement, non-removable), not the "engine synthesizing
  query structure the agent didn't ask for" anti-pattern, since this is a
  policy guardrail, not a convenience. **(2) Close only what a per-query floor
  honestly can.** It stops single-query singling-out but *not* multi-query
  differencing (subtracting two independently-≥*k* aggregates), which needs
  query-set auditing or differential privacy. Rather than overclaim
  "k-anonymity," the guardrail's scope is stated exactly, and multi-query
  differencing stays a documented residual in `docs/INFERENCE_RISKS.md` (R3).
- **2026-07-21 — The admin observability overview reports an honest
  single-process snapshot rather than pretending to be a durable
  time-series (TODO.md item 44, phase 1).** `GET
  /api/v1/admin/observability/overview` aggregates the in-process
  Prometheus registry into operational trends (rejection categories, queue
  pressure, cost-estimate fail-open rate) globally and per connection.
  QueryGate does **not** own a durable metrics store, so instead of
  silently presenting cumulative-since-process-start counters as if they
  were history, the response is explicitly labeled
  (`source="process_snapshot"`, `durable=false`, `since`, and a `note` that
  the numbers are per-replica under the default backends). **Why accepted:**
  an observability surface that quietly implies more durability/coverage
  than it has is actively misleading to the operator making a capacity or
  policy decision from it — worse than a smaller, truthful one. The
  aggregates are also built only from already-public, low-cardinality
  metric labels (connection ids, fixed reason/outcome buckets), never a
  query, value, or principal, so the surface stays inside the same
  redaction invariant as `metrics.py`/`audit/logger.py` (QG-28). The `/admin/`
  control plane renders it as a read-only cards panel over the same scoped
  endpoint, with the snapshot's honesty note shown in the UI. Consistent with
  that honesty, the panel shows current-value **cards**, not time-window trend
  charts — a point-in-time snapshot has no stored history to plot, so real
  charts wait on the deferred external metrics backend. The phase-1 boundary is
  deliberately "ship the honest cards, not a dishonest trend line."
- **2026-07-22 — Behavioral anomaly surfacing over the audit stream is a
  read-only signal for a human, deliberately *not* an autonomous throttle
  (TODO.md item 59, phase 1).** `GET /api/v1/admin/observability/anomalies`
  (`admin:observability:read`) reads item 23's persisted audit stream and, per
  principal, compares a recent window against that caller's own preceding
  baseline to flag volume spikes, rejection-rate jumps, and newly-touched
  connections — including among queries policy *allowed*. **Why read-only,
  not enforcement:** the tempting next step is to auto-throttle or auto-block a
  spiking caller, but that would cross the same hard line 32C's usage learning
  already respects — no signal derived from observed traffic may feed back into
  enforcement without a human in the loop, or the gateway becomes a system that
  silently rewrites its own access decisions from noisy behavioral data. So the
  module has *no write path at all*: it emits a report an admin reads, never a
  policy change. **Why per-principal-baseline, not a global threshold:** a fixed
  "N queries/minute is suspicious" bar is wrong for every deployment and every
  caller; comparing each principal to its own recent history surfaces a genuine
  behavioral change without the operator hand-tuning a number per service
  account. The report is bounded (one-pass streamed read, capped principals) and
  carries only the same ids/counts already in the audit event — no SQL, value,
  table, or column (QG-28). It reuses item 44's observability router and scope,
  and is surfaced as a read-only "Behavioral anomalies" panel in the `/admin/`
  Observability view (one row per principal-signal, with a color-coded kind
  badge and a plain-language detail), rendered from the same scoped fetch as
  the overview.
- **2026-07-29 — The config/catalog-change trend card reads the durable audit
  stream, not the process-snapshot metrics registry (TODO.md item 44, phase 2
  slice).** `GET /api/v1/admin/observability/config-changes`
  (`admin:observability:read`) answers the trend question item 44 phase 1
  deliberately deferred: "is config-governance or catalog-governance change
  *volume* rising, and by which action?" **Why a different read path than
  phase 1:** those events (`ConfigChangeEvent`/`CatalogGovernanceEvent`) live in
  the persisted audit stream, not the Prometheus registry, so `admin/
  config_trends.py` follows item 59's `AuditEventSource`-protocol shape
  (`ChangeEventSource`/`JsonlChangeEventSource`) instead of extending
  `admin/observability.py`'s registry aggregation. **Why a real trend, not
  another honest snapshot:** phase 1's metrics-registry counters reset on
  process restart, which is why that surface had to label itself
  `process_snapshot`/`durable=false`; the audit JSONL file is already durable
  history, so a genuine recent-vs-baseline rate comparison (the same two-window
  shape item 59 uses per-principal, applied fleet-wide to change-event volume)
  is honest here without any new persistence subsystem. Bounded by a
  configurable scan cap, honestly reports `source="disabled"` without the JSONL
  sink enabled, and carries only action names/outcomes/counts — never version
  content, proposal text, or raw YAML (QG-28). Rendered as a "Change velocity"
  subsection under the same `/admin/` Observability view, reusing the existing
  `admin:observability:read` scope rather than adding a new one. Time-window
  trend *charts* and querying an operator-configured external metrics backend
  remain deferred — those still need a store QueryGate does not own; this slice
  needed none.
- **2026-07-30 — Safe explanations of a caller's own recent denials reuse item
  59's audit reader rather than a third file-parsing implementation, and are
  self-service (no admin scope) by construction (TODO.md item 45, phase 2).**
  `GET /api/v1/help/my-recent-denials` (`help/personal_denials.py`) answers the
  question item 45 phase 1 explicitly deferred: "why was my recent query
  rejected?" **Why it reuses `admin/anomaly.py`'s `JsonlAuditEventSource`:**
  the underlying I/O — stream the audit JSONL file, unwrap the hash-chained
  ledger envelope, tolerate malformed lines — is identical to item 59's, since
  both read the same `query.execution` event stream; only the window shape
  differs (a single lookback here, vs. item 59's recent-vs-baseline
  comparison), so a nominal `baseline_window_seconds` reuses the tested reader
  instead of duplicating chain-envelope-unwrapping logic a third time (item
  44 phase 2's `config_trends.py` was the second instance, justified there
  because it reads a genuinely different event pair). **Why no admin scope:**
  unlike every other audit-stream reader in the codebase (item 59, item 44
  phase 2), this is reached from `/help/my-recent-denials` with the same
  posture as `/help/my-access` — authentication only — because the response
  is filtered to the caller's own `principal_id` before any data is returned,
  the same identity `execution/service.py` already scopes delegated policy
  resolution by, so there is no cross-principal disclosure to gate. Verified
  end-to-end with two JWTs (distinct `sub` claims): each principal sees only
  their own denial, proven both by exact response contents and by asserting
  the other principal's connection id/subject never appears anywhere in either
  response body (`tests/integration/test_personal_denials_api.py`). Each
  denial carries only a stable `reason` label (drawn from
  `AuditEvent.error_category`) and one fixed explanation per category — never
  `query_shape`, another principal's activity, or a table/column identifier.
  **A residual caught by `auditors` review before shipping:** the report's
  scan-diagnostic fields (renamed `own_denials_found`, alongside `truncated`)
  must themselves stay caller-scoped rather than reporting the underlying
  reader's fleet-wide event count — safe on item 59's admin-scoped sibling,
  but this endpoint carries no scope requirement at all, so a raw scan-wide
  number would have leaked cross-principal audit volume to any authenticated
  caller; fixed before commit, with a regression test proving the count never
  reflects other principals' activity. Surfaced as a "Recent denials" panel on
  the existing `/access/` portal.
- **2026-07-30 — The server-side draft store persists the exact same bundle
  phase 1 downloads, encrypted at rest, rather than inventing a second
  document shape (TODO.md item 47, phase 2).** `admin/draft_store.py`'s
  `DraftStore` (behind `POST/GET/GET-by-id/DELETE /admin/config/drafts[/{id}]`)
  answers what phase 1 explicitly deferred: recovery that survives a lost
  download and works across devices, without the browser ever holding the
  file. **Why the same `ConfigChangeSetBundle`, not a new model:** a stored
  draft and a downloaded one are the same artifact under two delivery
  mechanisms — keeping one shape means loading a draft still hands the caller
  back through the unchanged validate/stage/apply flow, exactly like
  importing a downloaded bundle, with no second config-mutation path to keep
  in sync. **Why encryption, not just access control:** a bundle can
  legitimately carry a `connections` document with a literal credential —
  the same reason phase 1 never writes it to browser `localStorage` — so the
  server-side store encrypts the bundle at rest (Fernet, keyed by a SHA-256
  derivation of `AppConfig.draft_store_encryption_key`, matching the plain-
  string-secret ergonomics `audit_ledger_hmac_key`/`approval_token_hmac_key`
  already established rather than demanding a pre-formatted key from the
  operator) instead of relying on scope checks alone. An empty key disables
  the subsystem outright (a clean `503`), never a silent plaintext fallback.
  **Why ownership is enforced at the storage layer, not just the route:**
  `DraftStore.load`/`.delete` raise the identical `NotFoundError` for "doesn't
  exist," "expired," and "exists but belongs to someone else" — proven
  end-to-end with two JWT principals, not just asserted — so the endpoint
  structurally cannot become a draft-id enumeration oracle even if a future
  route-layer check were ever loosened. **A residual `auditors` review
  surfaced and this pass documents rather than leaving silently untested:**
  isolation is only as fine-grained as the deployment's identity model — this
  codebase's static `api_keys` all share one `api_key_subject`, so two admins
  each holding their own key are NOT isolated from each other's drafts (the
  same limitation item 45's `/help/my-recent-denials` already carries); real
  per-admin isolation needs JWT auth, proven by a dedicated regression test
  rather than left as an implicit assumption. **Why per-principal retention
  bounds, not a cron job:** every save/list call opportunistically prunes
  expired drafts first, and `draft_store_max_drafts_per_principal` (default
  20) rejects an over-cap save rather than silently evicting an older draft
  — deterministic behavior an admin can reason about, with no background
  sweeper to operate. Surfaced as a "Saved drafts" panel beside the existing
  export/import buttons in the admin UI.
- **2026-07-30 — Real trend charts read an operator-configured external
  metrics backend instead of QueryGate building a time-series store of its own
  (TODO.md item 44, phase 2 remainder — now fully `✅ DONE`).** `GET
  /api/v1/admin/observability/history` (`admin/metrics_history.py`) closes the
  two gaps the 2026-07-29 change-velocity slice explicitly deferred: real
  time-window trend charts, and querying an external metrics backend. **Why an
  external backend, not QueryGate's own store:** the North Star's non-goals
  rule out QueryGate owning a warehouse/time-series store of its own, and
  standing one up just to chart five gauges would be a disproportionate,
  hard-to-operate addition for a read-only convenience panel — an operator
  already running Prometheus against this deployment's `/metrics` is a much
  smaller lift, and it's the same shape as this repo's other "reuse what the
  operator already has" calls (the JSONL audit sink, `docs/RELEASING.md`'s
  registry choice). **Why a Protocol, not a hardcoded Prometheus client:**
  `MetricsHistorySource` is a narrow read-only seam with one concrete
  implementation today (`PrometheusMetricsHistorySource`) registered by a
  `MetricsHistoryBackend` config enum — the same composable-interface
  doctrine as `SecretResolver`/`DialectAdapter`/`AuditSink` — so a future
  backend (e.g. a hosted TSDB) is one more class, never a branch at the route.
  **Why five fixed named series, not an arbitrary-PromQL passthrough:** the
  dashboard answers a small, known set of operational questions (query rate,
  rejection rate, avg duration, queue depth, concurrency utilization); letting
  a caller submit its own PromQL would be a second uncontrolled query surface
  in a codebase whose entire premise is that the *only* surface is a validated
  AST — so every query sent to the backend is one of five fixed templates
  QueryGate composes itself, parameterized only by the admin-configured
  window/step, never by request input. **Why honest degradation, not a 5xx:**
  an unreachable or misconfigured backend is caught and surfaced as
  `backend_error` on a 200 response (`source="prometheus"`, empty series)
  rather than failing the whole dashboard request — the same "report the gap
  honestly, don't crash" posture phase 1's `process_snapshot` labeling and
  phase 2's `source="disabled"` already established. **Why the step is
  widened, never the window:** `metrics_history_max_points_per_series` bounds
  the request by construction the same way `config_trends.py`'s scan cap
  does — an operator misconfiguring a wide window with a tiny step gets a
  coarser chart, never an unbounded backend query or a silently truncated
  time range. Rendered as a "Trend charts" subsection with one dependency-free
  inline-SVG sparkline per series (no charting library, consistent with the
  rest of the vanilla-JS control plane) under the same
  `admin:observability:read` scope as every other Observability read.
- **2026-07-21 — Structured template authoring feeds the shared release, and
  keeps the query skeleton as validated JSON rather than a visual AST builder
  (TODO.md item 87).** Three choices. **(1) It composes into the change-set
  draft, not a governance queue.** Unlike catalog (item 84), query templates
  have no independent governance versioning — they are a config document in the
  bundled `ConfigVersionStore` release. So the form's output merges into the
  draft `templates.yaml` and flows through the shared validate → stage → apply →
  rollback, consistent with item 85's Releases domain; routing it through a
  per-domain publish would have been the wrong versioning plane. **(2) The
  parameter-slot builder is structured, the query skeleton stays JSON.** The
  error-prone, schema-heavy part of a template is the parameter slots (type +
  required/default + numeric/length bounds + `allowed_values`, all with
  self-consistency rules), so that got a guided row builder; the
  `StructuredQuery` skeleton is entered as validated JSON rather than a full
  visual AST builder, which would be a large separate surface and is already
  served by the typed query-builder SDK and raw editing. The whole template is
  validated against the same `QueryTemplateFile` model the loader uses, so
  compose-time errors match stage-time ones. **(3) Policy needed no new work.**
  The visual policy designer already composed a validated `Policy` layer into
  the draft — item 87's "form instead of YAML" goal was already met for policy,
  so the deliverable was templates, the one document that still lacked it. Raw
  YAML editing stays as the escape hatch for every document.
- **2026-07-21 — The domain-separated admin UI surfaces the shared-release vs.
  self-contained-catalog split rather than hiding it (TODO.md item 85).** The
  control-plane nav was regrouped from nine-plus flat views into seven domains
  (Overview / Connections / Policy / Catalog / Templates / Releases / Audit). The
  tempting "clean" grouping would give each domain its own self-contained
  authoring-and-activation loop, but that would misrepresent how config
  actually versions: `ConfigVersionStore` stages policy + connections +
  catalog.yaml + templates as **one** bundled atomic version, so those
  domains' authoring surfaces inherently feed a single cross-domain change set
  that lives in a shared **Releases** domain — they cannot each own their own
  activation. Catalog is the deliberate exception: it has its own governance
  versioning (`CatalogFileRepository`), independent of `ConfigVersionStore`, so
  its whole author → review → publish → rollback loop *is* self-contained in
  the Catalog domain. Rather than paper over this asymmetry (e.g. faking a
  per-domain "apply" everywhere, or burying the change set), each domain's tab
  bar carries a one-line release signal — *"edits stage into the shared
  release"* for Connections/Policy/Templates vs. *"self-contained governance"*
  for Catalog — so an operator can see which edits flow into a shared,
  co-applied release and which publish on their own. Two secondary choices:
  the refactor is nav/layout only (routing stays a flat `showView` over a
  `viewMeta` map, now resolved through a domain→tab model; every render
  function and scope gate is reused unchanged, and the leaf-view URL hash keeps
  every view deep-linkable), and `app.js` was **not** split into per-domain
  modules — the change never touches the view-render logic, so a module split
  would add risk without reducing it.
- **2026-07-21 — Human-authored catalog entries route through the catalog
  governance queue (a new `manual` proposal source), with scope-based — not
  identity-based — separation of duties (TODO.md item 84).** Two deliberate
  choices. **(1) Route authored content through catalog governance
  (`CatalogFileRepository`), not the change-set store.** `catalog.yaml` already
  carries human-curated content, but the only prior way to author it by hand
  was raw YAML in the change-set `<textarea>`, which flows through
  `ConfigVersionStore` — whose snapshot copy CLAUDE.md explicitly warns
  diverges from the live `CATALOG_FILE` catalog governance writes to. The
  Curate form instead composes a validated `CatalogDraftContent` and creates a
  `source_class = manual` proposal through the same
  quarantine → review → publish → rollback path generated proposals use, so it
  gets the same staged safety and actor-attributed audit and there is still one
  catalog file and one mutation path. The rejected alternative (author straight
  into the change-set `catalog.yaml`) was declined precisely because it uses the
  wrong versioning plane. `manual` is a proposal-only knowledge class — it stays
  quarantined until publish resolves it to a `verified` entry; it is
  publishable-as-verified, never auto-trusted or auto-indexed, and the draft
  content model still structurally has no sensitivity/sampling field, so a
  manual body can't smuggle one in. **(2) Separation of duties is scope-based,
  not identity-based.** Authoring is gated on a distinct `catalog:author` scope,
  but a principal that *also* holds `catalog:review` may approve and publish its
  own manual proposal — there is no author≠approver check. The rejected
  alternative was a hard-coded identity gate forbidding self-approval; it was
  declined because it can't express the real deployment spectrum (a solo
  operator vs. enforced four-eyes) — granting `catalog:author` and
  `catalog:review` to different principals is what enforces four-eyes, and the
  scopes are the knob. `created_by`/`approved_by` are still recorded for audit;
  only the gate is scope-driven.
- **2026-07-21 — Query-template validation is layered: offline slot/structural
  checks in the always-on dry-run, live column/table existence as a separate
  best-effort on-demand action (TODO.md item 83).** Two deliberate choices.
  **(1)** Parameter-slot self-consistency (`allowed_values`/`default` must match
  the declared `type`/bounds) is validated at load/dry-run time in the model
  itself, sharing one `scalar_type_error` primitive with runtime binding so the
  two can't disagree — a slot that could never bind (e.g. `type: integer` with
  string `allowed_values`) is caught before it ships, not left to fail at
  invocation. **(2)** Column/table existence is *not* folded into the dry-run,
  which is intentionally offline (no DB session, same posture as
  `explain`/cost-estimation). Folding it in would couple every config
  validation to database availability — a slow or down database would block
  staging otherwise-valid config. Instead it's a distinct, opt-in
  `check-template-schema` action that reflects the currently-live connections
  and returns best-effort per-template results (`ok`/`issues`/
  `connection_unavailable`/`unreachable`); an unreachable database is reported,
  never a hard failure. The rejected alternative — always-on live schema
  validation in the dry-run — was declined for that coupling, even though it
  would be marginally more convenient, because keeping the fast path offline is
  worth more than saving one button click.
- **2026-07-21 — Query templates are governed through the config-versioning
  plane (item 25), not 32B's catalog-proposal state machine (TODO.md item 48
  phase 2).** Item 48's original sketch said route template authoring "through
  32B's proposal state machine." On implementation the better fit was item
  25's config-versioning plane: `templates.yaml` became a fourth governed
  document beside connections/policy/catalog, carried through the same
  `ConfigVersionStore` snapshots and `/admin/config/*` validate → preview →
  stage → apply → rollback flow (plus a `templates.yaml` admin-UI editor tab).
  The rejected alternative (**Option A**) was a per-template proposal model
  with individual approve/publish and separation of duties, mirroring
  `catalog/governance.py`. Two reasons it lost: **(1)** query templates are a
  configuration *document* loaded by `config_reload` alongside the other three
  — not catalog *entries*, which carry sensitivity/confidence/provenance/
  relationship-target fields the 32B model is built around; forcing a config
  document through catalog-entry machinery would have been a second, awkward
  mutation path, exactly what this repo's standing rule forbids. **(2)** The
  config-versioning plane already *is* "governed create/edit/publish/rollback"
  for config documents — whole-document staging, validation, re-validation on
  apply (QG-15), atomic reload, rollback to a prior snapshot, redaction-safe
  audit, and no self-publish (staging is not activation). Templates joined it
  as one more document with zero new governance code. The cost is that
  governance is whole-`templates.yaml`, not per-template; a finer-grained
  per-template sign-off workflow remains a possible future addition, not a gap
  this phase left broken. A version snapshots its own `templates.yaml`, and a
  pre-phase-2 version with no snapshot falls back to the deployment's static
  `template_file` on apply rather than clobbering it to empty.
- **2026-07-21 — The typed Python query builder front-ends the real AST
  rather than re-implementing it, and ships in-tree before a standalone
  distribution (TODO.md item 51 phase 1).** `querygate.client` builds the
  same `query_ast` Pydantic models `StructuredQueryService` validates, then
  serializes them to wire JSON. Two deliberate choices, each with a rejected
  alternative: **(1) Reuse the server's models, don't build a parallel typed
  schema.** The obvious "SDK" shape is a decoupled client with its own
  validation, but that would *duplicate* server-side validation (a CLAUDE.md
  invariant forbids exactly that) and could silently drift from the AST. By
  constructing the real models, `build()` raises the server's own error
  early, the builder can never accept a shape the server rejects (or vice
  versa), and drift is structurally impossible — enforced by explicit
  drift-guard tests that fail if the AST grows a field/variant/operator the
  builder can't express. The cost is that the builder currently imports from
  the `querygate` package; acceptable because that import path
  (`query_ast/models.py`, `querygate/__init__.py`) is pydantic-only and
  stays light. **(2) Ship in-tree (`from querygate.client import Query`) now;
  defer the standalone dependency-light distribution.** A separate
  `querygate-client` package (PyPI/npm) that installs without the server's
  full dependency closure is the eventual goal, but it is coupled to item 30
  phase 2: no package registry has been chosen or configured, and publishing
  requires explicit maintainer approval. Building a standalone dist with
  nowhere to publish it — and a second copy of the models to keep in sync —
  would be premature; the in-tree module is the shipped, importable, tested
  surface until a registry exists. TypeScript was deferred as phase 2 at the
  time of this entry — a genuine second-language implementation with its own
  sync-test strategy, not more of the Python work — and has since shipped
  in-tree the same way (see the 2026-07-28 entry above); only the standalone
  distribution for either language still waits on item 30 phase 2.
- **2026-07-21 — Per-principal quota is enforced before queuing, counts every
  admitted attempt, skips anonymous callers, and ships in-process first
  (TODO.md item 50 phase 1).** A rolling-window cap on request count and
  response bytes (`execution/quota.py`), resolved per principal per connection
  through the existing policy merge. Four deliberate scope choices, each with a
  rejected alternative: **(1) Checked before the concurrency slot / DB session,
  not alongside execution** — a throttled caller shouldn't get to consume a
  slot or a connection just to be rejected; putting the gate first keeps
  rejected traffic cheap, matching where allow/deny already sits. **(2) The
  reservation counts as one request the moment it's admitted, and a query that
  then fails downstream is *not* refunded** — a quota is an anti-abuse / cost
  control, so an admitted attempt is spent whatever its outcome; refunding
  failures would let a caller probe invalid queries for free at machine speed.
  Reserving atomically at admission (prune-check-record in one step) also
  closes a check-then-act race where many concurrent in-flight callers each
  pass a check before any records. **(3) Only authenticated callers are
  throttled** — an anonymous/unattributable request has no principal to key a
  per-principal window on; applying the quota to a shared anonymous bucket
  would let one caller's traffic throttle an unrelated one, so it's skipped
  (a deployment that wants anonymous callers rate-limited should require auth,
  which is a separate control). **(4) In-process window now, Redis cross-replica
  later** — the in-process quota is a complete guardrail for a single instance
  and the exact same phasing the concurrency limiter itself used (item 9 /
  item 35 phase 2); under a load balancer the window is per-replica until the
  `RedisQuotaLimiter` (phase 2) lands behind the same `QuotaLimiter` Protocol.
  The rejection is a distinct 429 + `Retry-After` (REST) / `RATE_LIMITED`
  (MCP), not a generic denial, so an agent can tell "slow down / budget spent,
  retry later" apart from a structural error it should not retry unchanged.
  `explain` is not quota-gated — it compiles a preview without executing, the
  same reason it skips cost-estimation.
- **2026-07-20 — A masked column may appear only as a bare `select`
  projection; any other use is rejected, not silently masked-in-place
  (TODO.md item 49).** Column value masking (`hash`/`null`/`last`/`bucket`,
  applied in the compiled `Select` so the raw value never leaves the
  database) gives partial visibility of an otherwise-permitted column. The
  open question was what to do when a masked column is referenced outside the
  projection — in a `where`/`join`/`order_by`/`group_by` position, or nested
  inside a function/CASE/aggregate. Three options: (A) reject such a query;
  (B) mask the column *everywhere it appears*, so `where ssn = 'x'` becomes
  `where mask(ssn) = 'x'`; (C) mask only the projection and let filters/sorts
  use the raw column (the Immuta/Privacera default). **Why (A) accepted:**
  option C leaves an inference exfiltration channel — a caller who can't see
  `ssn` but can filter `where ssn = '123-45-6789'` and observe whether a row
  comes back has read the value one guess at a time, which an untrusted agent
  can probe at machine speed (the same side-channel the stage-1 allow/deny
  check already walks every clause to close for *denied* columns). Option B
  closes the channel but silently rewrites what the query means — the agent
  asks to filter on the real value and gets a filter on the masked value with
  no way to tell, producing wrong results rather than an error. Rejecting is
  the only posture that closes the channel without lying about semantics, and
  it matches the "expose primitives, don't spoon-feed the agent" philosophy
  (reject and name the gap, as `array_agg` and item 74's MSSQL nulls do)
  rather than the engine silently transforming a request the caller didn't
  make. Starting strict is extensible: a deliberate masked-comparison opt-in
  (e.g. filter on last-4 of a card) can be added later as its own recorded
  decision; starting permissive and tightening later would break callers.
- **2026-07-20 — Query templates bind through the unchanged
  `execute()` pipeline as a strict enforcement superset, rather than a
  separate templated-execution path (TODO.md item 48).** `bind_template`
  validates the supplied parameters against their declared types/bounds,
  substitutes them as bound values, and produces an ordinary
  `StructuredQuery` that goes through the exact same
  `StructuredQueryService.execute()` — policy caps, allow-deny, schema
  existence, compilation, concurrency, audit — as an ad-hoc query. **Why
  accepted:** the safety-critical property is that no invocation shape can
  ever see *fewer* checks than an ad-hoc query. Reusing `execute()`
  verbatim makes that structural (templates can only *add* parameter
  validation on top), instead of asking a reviewer to prove a parallel
  code path re-implemented every guardrail identically. Templates are
  file-configured (`TEMPLATES_FILE`) and hot-reloadable like the other
  config stores, and deliberately expose no query skeleton to agents —
  only the id, description, and typed parameter signature — so a template
  is a curation/ergonomics layer, never a new trust boundary. Governed
  edit/approve/publish of templates through the admin control plane is a
  recorded follow-up, not part of item 48.
- **2026-07-20 — `array_agg` rejects outright on MSSQL and SQLite instead
  of emulating an array (TODO.md item 81).** Postgres's `DialectAdapter.
  array_agg` is a real implementation (`array_agg(...)`, a native
  Postgres primitive). MSSQL has no array/collection type in T-SQL at
  all, so `MSSQLDialectAdapter.array_agg` raises `QueryValidationError`
  naming that gap — the first time a real, supported registry dialect,
  not just the internal-only SQLite path, has had a `DialectAdapter`
  method reject a capability outright. SQLite's adapter also raises,
  deliberately *not* mirroring `string_agg`'s SQLite mapping
  (`group_concat`): SQLite's `json_group_array()` returns a JSON-encoded
  string, not a real array value, so mapping it would be the exact
  forced-parity emulation CLAUDE.md's "Engine philosophy: expose
  primitives, don't spoon-feed the agent" section rules out — there is no
  lucky shape match here the way `group_concat(expr, sep)` happened to
  match `string_agg`/`STRING_AGG`'s `(expr, separator)` signature.
  **Why accepted:** the alternative (e.g. faking an array via
  `STRING_AGG`-as-CSV or a JSON-aggregation trick) would mean the engine
  silently deciding what an agent's `array_agg` call "really meant" on a
  dialect that has no such concept — precisely the kind of
  unrequested-structure synthesis the engine-philosophy section was
  written to rule out. A calling agent that needs array-like behavior on
  MSSQL can compose its own workaround with primitives QueryGate already
  exposes (e.g. `string_agg` plus client-side splitting), the same way a
  human SQL author would.
- **2026-07-20 — SQLite's `DialectAdapter.string_agg` maps to a real
  function (`group_concat`) instead of raising, unlike every other
  internal-only-dialect adapter method (TODO.md item 80).** Every other
  `DialectAdapter` method that has no real SQLite equivalent (`stat_fn`,
  for `stddev`/`variance`) raises `QueryValidationError` rather than
  faking one, since SQLite is explicitly the internal test/example
  dialect, not a supported registry one. `string_agg` is the exception
  because SQLite's `group_concat(expr, sep)` genuinely has the identical
  `(expr, separator)` shape as Postgres's `string_agg`/MSSQL's
  `STRING_AGG` — not a coincidence worth ignoring. **Why accepted:** it
  turns this item's SQLite coverage from a rendering-only assertion into a
  real end-to-end execution test (seed data, run the query, check the
  actual concatenated output), which every other dialect-sensitive item in
  this arc except stddev/variance has had. Concatenation order is
  implementation-defined on every dialect without an `ORDER BY`-in-call
  (deliberately out of v1 scope), so the test compares the *set* of
  concatenated values, not an exact ordered string.
- **2026-07-20 — Fixed a pre-existing crash bug in `audit/events.py`
  discovered while implementing item 80, not introduced by it.**
  `_select_shape`/`_predicate_shape` build the redaction-safe structural
  summary persisted for every query attempt, called unconditionally and
  unguarded at the very top of `StructuredQueryService.execute()` — before
  policy validation, schema validation, or compilation even run. They only
  recognized `str`/`AggregateSelectItem`/`DateBucketSelectItem` and raised
  `TypeError` for anything else, meaning any real query selecting a
  `ScalarFunctionSelectItem` or `CaseSelectItem` (items 71/72, already
  shipped) crashed `execute()` entirely — not merely a logging gap. No
  test exercised that path, so it went unnoticed. Fixed in the same commit
  as `StringAggSelectItem`'s own shape (which needed the identical fix to
  avoid repeating the bug for the new type), reusing the existing
  `select_item_column_refs`/`predicate_column_refs` collectors from
  `validation/schema_validation.py` as the shared source of truth for
  which columns a select item or predicate touches, rather than
  re-deriving that logic a third time in the audit module.
- **2026-07-20 — Superseded: item 72's SELECT-only scalar-function
  restriction, below, no longer holds (TODO.md item 77).** The "materially
  bigger change" item 72 deferred turned out to be worth doing: rather than
  making `Predicate.col` itself a `Union[str, expression]`, the fix was
  `Predicate.col_fn: Optional[ScalarFunctionCall]` — a field sibling to
  `col`, mutually exclusive with it, reusing the exact same whitelisted
  fn/args shape `ScalarFunctionSelectItem` already had (factored into a
  shared `ScalarFunctionCall` base once two different places needed it).
  `WHERE lower(Customer.Email) = 'x'` and `HAVING coalesce(discount, 0) >
  5` are both expressible now. The narrowness that made the original
  SELECT-only version safe is preserved deliberately: no function nesting
  (`col_fn`'s args are still only a column ref or a literal, never another
  function call), and `col_fn`'s columns are always real `Table.Column`
  refs, never a select-item alias. See TODO.md item 77 for the shipped
  design; the entry below is kept as an accurate record of the tradeoff as
  it stood on 2026-07-20, not deleted.
- **2026-07-20 — Whitelisted scalar functions/CASE (TODO.md item 72) are
  SELECT-projection only, not usable as a WHERE/HAVING predicate target.**
  Extending `Predicate.col` to accept a function-wrapped expression instead
  of a bare `Table.Column` would thread a new type through every
  `parse_column_ref` call site across policy validation, schema validation,
  and the compiler — a materially bigger change than adding `coalesce`/
  `lower`/`upper`/`trim`/`concat`/CASE as SELECT items, which only needed
  new branches in the existing select-item dispatch. **Accepted cost:** a
  request like "filter where lower(status) = 'active'" still can't be
  expressed directly — the caller filters on the raw column instead.
  **Why accepted anyway:** the SELECT-only version closes the far more
  common gap (projecting a normalized/conditional value) at a fraction of
  the risk, and keeps the WHERE/HAVING grammar exactly as narrow and
  auditable as it was before. Extending function calls into predicates is
  left as an explicit, separately-scoped future item if real usage shows
  it's needed. See [The Core Request Pipeline](#the-core-request-pipeline).
- **2026-07-20 — Merged `execute_structured_query`/`explain_structured_query`/
  `execute_structured_queries` into one `run_structured_queries` MCP tool
  (TODO.md item 61), a deliberate breaking rename.** Each MCP tool's JSON
  Schema is independently self-contained — the MCP `tools/list` protocol has
  no mechanism for one tool to reference another's schema, unlike OpenAPI's
  `$ref`'d component schemas, which is why REST kept its three separate
  `/query`, `/query/explain`, `/query/batch` endpoints without this
  problem. That meant `StructuredQuery`'s full nested schema was carried
  three times, byte-for-byte identical, across the three MCP tools — real,
  measured duplication (`tests/unit/test_mcp_token_budget.py`), not a
  theoretical one. Collapsing to one tool (`queries` always a list, `mode:
  "execute"|"explain"`) cut it to one copy. **Accepted cost:** any
  caller/doc/example referencing the three old tool names by name breaks —
  updated everywhere found in this repository (examples, this guide, the
  landing sandbox), but an external integration holding the old names would
  need to update too. **Why accepted anyway:** the alternative (leaving the
  duplication in place) cost roughly 3x whatever `StructuredQuery`'s schema
  size is on every single MCP session's fixed overhead, for every caller,
  forever — a recurring tax rather than a one-time migration cost. See
  [MCP transport](#mcp-transport).
- **2026-07-20 — The "test now" connection probe got its own scope,
  separate from read.** `admin:connections:read` (item 43 phase 1) is
  passive — it only reads `HealthMonitor`'s cached snapshot. The new "test
  now" action (phase 2a) actively opens a real connection to a customer
  database on demand, which is a materially different privilege (and a
  materially different cost to the target database), so it required its own
  `admin:connections:test` scope plus a per-connection cooldown rather than
  being folded into the existing read scope. See
  [The admin surface](#the-admin-surface-config-as-versioned-history-not-a-live-edited-file).
- **2026-07-20 — Corrected a stale claim in `CLAUDE.md` about 32C.**
  `CLAUDE.md` stated adaptive usage learning (32C) "has not started."
  While researching the [Catalog / Semantic Layer](#catalog--semantic-layer)
  section, the code (`catalog/usage.py`, `catalog/learning.py`,
  `catalog/adaptive_learning_benchmark.py`) and `TODO.md` (item 32: "32C
  ✅") both showed it had in fact shipped, fully guardrailed. `CLAUDE.md`
  was updated to match. **Why this matters:** an agent working from the
  stale claim could have mistakenly treated 32C as greenfield work rather
  than an existing, narrowly-scoped feature to extend carefully within its
  documented guardrails.
- **StructuredQuery AST as the only input shape, from the start.** Rather
  than accepting SQL text and trying to sanitize it, QueryGate closes off
  the raw-SQL attack surface entirely — no `sql` field exists anywhere in
  the codebase. This is the foundational decision the rest of the security
  model builds on. See [Security Model](#security-model).
- **`ConnectionProfile`/`PublicConnectionInfo` as two unrelated models, not
  one model with a "hide this field" convention.** Credential redaction is
  enforced by the public model structurally lacking a credential field,
  and is tested against the live OpenAPI/MCP schema, not just the model
  definition — specifically to catch a future "wired the wrong model to a
  new endpoint" mistake that a model-only test would miss. See
  [Security Model](#security-model).
- **Policy is per-connection (optionally per-principal), not global.**
  Different databases behind one QueryGate deployment need different rules;
  a global policy would force every connection to the strictest common
  denominator. See [Connections, Policy & Configuration](#connections-policy--configuration).
- **Catalog content is quarantined by construction, not by review
  discipline.** A draft proposal's data model has no field capable of
  affecting access (no `sensitivity`, no policy field), so even skipping
  review can't turn a bad suggestion into a security issue — only into
  metadata that still has to be explicitly published to matter at all. See
  [Catalog / Semantic Layer](#catalog--semantic-layer).
- **32C usage-based learning ships with an explicit anti-feedback-loop
  rule.** A usage signal is only recorded when the object's current catalog
  knowledge is absent or already verified — never when an agent only acted
  on the system's own unreviewed guess — so the learner can't "confirm"
  its own unpublished suggestions. See
  [Catalog / Semantic Layer](#catalog--semantic-layer).
- **Cryptographic release signing (Sigstore/cosign) — since implemented
  (2026-07-23).** *Superseded:* this was deferred while no publishing pipeline
  existed. Once GHCR became the real published artifact, the release workflow
  gained cosign keyless signing + a SLSA build-provenance attestation; see the
  2026-07-23 signed-delivery entry above and [Testing, Release &
  Operations](#testing-release--operations). The SBOM, dependency audit, and
  checksum steps remain the phase-1 supply-chain work (`TODO.md` item 30).
- **MCP OAuth audience binding is enforced in the MCP resource-server layer,
  not globally in the JWT verifier.** The JWT authenticator is shared by the
  REST and MCP surfaces; making it *globally* require one audience would couple
  the two resources and force REST tokens to name the MCP resource. Instead the
  MCP middleware validates audience *after* verification, reading the decoded
  `aud` claim and requiring it to include the MCP resource identifier — so each
  surface owns its own RFC 8707 binding and enabling MCP OAuth doesn't silently
  change REST auth. Static API keys carry no `aud` and are treated as an
  explicitly-configured out-of-band trust: they skip audience binding but still
  pass the required-scope gate. The whole mode is opt-in
  (`MCP_OAUTH_RESOURCE_SERVER_ENABLED`) so existing deployments are unaffected.
  See [The Core Request Pipeline](#the-core-request-pipeline) (`TODO.md` item 90 phase 2).
- **2026-08-01 — Opting into the tamper-evident audit backend no longer
  silently disables four read surfaces (`TODO.md` item 136).** `/help/my-recent-
  denials`, the anomaly report, the config/catalog change-trend report, and the
  admin UI audit browser each gated on `audit_sink_backend == AuditSinkBackend.
  JSONL`, so `AUDIT_SINK_BACKEND=jsonl_chained` — the posture item 91 shipped and
  a regulated buyer would actually enable — refused all four, even though three
  of the four readers already transparently unwrapped the chain envelope and
  could serve them. **Two fixes, not one.** (1) The four equality/inequality
  gates are now one capability lookup, `AuditSinkBackend.is_locally_readable()`
  — `{JSONL, JSONL_CHAINED}` today — so a future backend (item 134) declares
  readability once instead of every call site repeating the check (and the bug)
  a third time. (2) The admin UI audit browser's `_audit_page` had **no**
  envelope unwrap at all (unlike the other three), so a gate-only fix would have
  turned it from honestly `source="disabled"` into silently empty with a rising
  `malformed` count — it now calls the same unwrap the other three use. That
  unwrap itself was duplicated inline in two readers (`admin/anomaly.py`,
  `admin/config_trends.py`); a 2026-07-30 Decision Log entry above reasoned
  explicitly about not tripling it, so this fix also extracts it once as
  `audit.ledger.unwrap_envelope()` and points all three readers at it — the
  concern that entry raised is now moot rather than deferred. The extraction is
  not a pure move: the two inline copies it replaced matched on only two of
  `LedgerRecord`'s four keys (`event` + `hash`); the shared function requires
  all four (`seq`/`prev_hash`/`event`/`hash`), a hardening caught by the
  `security-invariant-reviewer` audit of this change, closing a latent path for
  a plain event body carrying its own same-named fields to be misread as a
  chain envelope. That same audit found two further gaps this fix deliberately
  left open rather than folding in — the four surfaces still neither verify the
  chain nor disclose which backend actually produced a `source="jsonl"`
  response (`TODO.md` item 137, needs a maintainer decision on posture/cost),
  and the underlying line-by-line file scan is unbounded by lines read, a
  pre-existing gap for the default `jsonl` backend that this change also makes
  reachable under `jsonl_chained` (`TODO.md` item 138). See [Auth &
  Transports](#auth--transports) and [The `help/` module](#the-help-module--a-queryable-product-guide).
- **2026-08-01 — Every reader of the persisted audit stream now scans
  tail-first, bounded by lines read, not just lines retained (`TODO.md`
  item 138).** Before this, `admin/anomaly.py`, `admin/config_trends.py`, and
  the admin UI audit browser (`api/admin_ui_routes.py`'s `_audit_page`) all
  streamed the JSONL file forward from the beginning and relied on a bounded
  in-memory deque to keep only what mattered — correct, but unbounded in the
  amount of *work* done: a large audit history meant parsing and validating
  every line on every request, regardless of how much was actually needed.
  **Why not just cap lines read from the start, the simplest fix:** a forward
  scan capped at N lines reads the *oldest* N lines first (the sink only
  appends), so on a file larger than the cap it would silently never reach
  the recent window or newest page a caller actually asked for — worse than
  no bound at all, since the exact security/observability signal these
  surfaces exist for would go quietly blind at high volume, the moment it
  matters most. **The fix instead reads backward:** a new shared primitive,
  `audit.file_reader.iter_lines_reverse()`, reads the file in fixed-size
  chunks from the end, splitting on the raw newline byte (safe regardless of
  chunk boundaries, since `0x0A` never appears inside a multi-byte UTF-8
  sequence). Because the sink only appends, the physically newest lines are
  always what every one of these readers wants most, so a hard cap on lines
  *read* (`AnomalyThresholds.max_lines_read`, `ChangeTrendThresholds.max_lines_read`,
  `AppConfig.audit_page_max_lines_read`, `personal_denials_max_lines_read`) can
  bound worst-case work without ever trading away correctness. `admin/anomaly.py`'s,
  `admin/config_trends.py`'s, and `_audit_page`'s bounded deques were all
  removed entirely — reading tail-first, the first `max_events_scanned` (or,
  for `_audit_page`, `cursor + limit`) matches encountered *are* the newest N
  by construction, so there's nothing left for a deque to evict. `_audit_page`
  gained a new `truncated` field on its response (`AuditEventPage`) for the
  same reason the other two reports already had one: once a cap can fire, "no
  more matches found" and "stopped looking before finding out" are different
  claims, and conflating them would be a silent regression from the
  honest-disabled posture this audit surface has kept since item 31.
  `truncated` is deliberately conservative — it can report `True` even when
  every real match was already found (the scan simply kept going,
  sight-unseen, until the line cap fired on trailing non-matching lines) —
  rather than risk ever reporting `False` when data could plausibly be missing.

  **Hardened same-day by a second `security-invariant-reviewer` pass, before
  this item shipped.** The first cut bounded only *line count*, defaulted to
  2,000,000 everywhere, and left `anomaly_max_lines_read`/
  `change_trend_max_lines_read`/`personal_denials_max_lines_read` unwired from
  `AppConfig` (the thresholds objects had the field; the routers never read it
  from config, so it was stuck at the class default) — an `architecture-boundary-reviewer`
  finding, fixed by threading all three. The bigger issue: `iter_lines_reverse`
  itself was algorithmically unsound for a *single undelimited run* — the
  carry-forward buffer (`chunk + carry`) is fully re-copied every chunk, so a
  region with no newline at all costs O(run_length²), not O(run_length), and
  nothing bounded run length, since the line-count cap only increments once a
  line is actually yielded. Measured: at the shipped 2,000,000-line default,
  a realistic ~800-byte audit line put per-request cost at ~1.6 GB read and
  10-40s of blocking work on `/help/my-recent-denials` — authenticated-only,
  no admin scope, no rate limit at the time — which is a bound in the formal sense and
  not one in the practical sense. Fixed with two new independent bounds
  inside `iter_lines_reverse` itself, both raising a typed
  `AuditFileReadBounded` rather than returning silently (so every caller's
  `truncated` stays accurate — a silent stop would have looked identical to
  reaching the start of the file): `max_line_bytes` (default 1 MiB) aborts an
  undelimited run before it can grow past a fixed, cheap size; `max_total_bytes`
  (default 256 MiB) bounds total bytes read regardless of how many lines that
  spans, closing a companion gap where a file padded with an enormous number
  of *blank* lines was never counted against the line cap at all (blank lines
  are filtered inside the generator, before a caller's own counter ever sees
  them). The three `max_lines_read` defaults were also lowered from 2,000,000
  to 200,000 (50,000 for `personal_denials_max_lines_read`, defaulted tighter
  than the admin-scoped surfaces since it's the one reachable with
  authentication only) — now a real backstop underneath the byte-level bounds,
  not the sole line of defense. A fourth, narrower finding — a short read
  during in-place file truncation (`logrotate copytruncate`, not the
  rename-and-recreate rotation `JsonlAuditSink` already tolerates) could splice
  non-adjacent byte ranges into a fabricated line while still reporting
  `truncated=False` — is closed the same way: a short read from `handle.read`
  now raises `AuditFileReadBounded` rather than being silently concatenated.

  **Deliberately not folded into this item**, filed as follow-ups instead of
  fixed inline, since each is either a distinct subsystem or a real
  correctness/performance tradeoff needing its own scoping: bounding audit-line
  size at the *source* (the read-query AST's unbounded `select`/`join`/`group_by`
  lists, and `audit/sinks.py`'s own `_read_last_line` startup-path reader,
  which shares the pre-fix unbounded-growth shape) — `TODO.md` item 139;
  `_audit_page`'s pagination can still materialize up to `cursor + limit` ≈
  1,000,100 dicts given the existing `cursor` query-param ceiling — `TODO.md`
  item 140; and converting the line-count cap into a practically-tight,
  window-based early exit (stop after N consecutive out-of-window matches,
  tail-first-native) — a real further tightening, but one that trades a small,
  bounded ordering-tolerance assumption for speed, which is a call for the
  maintainer, not a default to reach for under review pressure — `TODO.md`
  item 141.
  See [The `help/` module](#the-help-module--a-queryable-product-guide).
- **2026-08-01 — A caller-facing verdict endpoint reuses the read pipeline's
  shared validation seam, and deliberately answers less than `explain` does
  (`TODO.md` item 133, the P4 leverage-move play).** `POST .../query/verdict`
  and MCP `run_structured_queries(mode="verdict")` add a new
  `StructuredQueryService.verdict()` method that calls the same
  `_validate_and_compile` seam `execute`/`explain` already share (non-negotiable
  #4 — one path, not a second evaluator) and answers only "would this be
  allowed", for a caller (an MCP gateway, proxy, or CI check) that needs a
  yes/no rather than debug detail. This is response-shape hygiene, not a
  privilege boundary: the same principal that can call `verdict` can call
  `explain`/`query` with the same credential and get the real message —
  QueryGate has no scope today that distinguishes "may see a verdict" from
  "may see debug detail" (a verdict-only-caller confinement would be a new
  scope, an explicit product decision, not something this item adds). Several
  deliberate design choices, all because a verdict-shaped endpoint is a
  discovery oracle unless designed against it (`docs/THREAT_MODEL.md`
  QG-19/QG-24 already establish this class for the admin simulation and "my
  access" surfaces; this entry adds QG-34):
  - **A denial always reports the same generic `reason="not-available-to-you"`,
    never the real exception or which validator raised it.** `validate_policy`
    runs strictly before `validate_schema`, so distinguishing "on your policy's
    deny list" from "doesn't exist in the schema" — even just the *category*,
    without the identifier — would let a caller enumerate identifiers and
    reconstruct both the schema and the policy boundary one query at a time.
    The collapse has to hold for every exception `_validate_and_compile` can
    raise for a shape reason, and patching it type-by-type already missed
    twice: the original except clause caught only
    `(PolicyViolationError, QueryValidationError)`; a first audit pass found
    `NotFoundError` (a join's own `connection` field naming a connection the
    caller can't see) and `sa.exc.NoSuchTableError` (a wholly non-existent
    table, which reflection raises directly rather than through
    `QueryValidationError`) missing, the second escaping as an uncaught 500
    that was itself a distinguishable fourth outcome; a second audit pass on
    that same fix then found the four-type allow-list itself was still
    reachable-but-missed by a plain `ValueError` from an unresolved
    `${ENV_VAR}` connection-string secret. The except clause is now a
    deliberate catch-all (`except Exception`), not an allow-list — safe
    because `_get_policy`/`enforce_query_quota`/`concurrency_slot` all run
    strictly before this inner block, so nothing reaching it is a
    quota/concurrency system failure, only a genuine query-shape rejection.
    `help/personal_denials.py`'s `_is_own_denial` excludes
    `operation="query_verdict"` events entirely, rather than the "different
    surface, different posture" argument this entry originally made for
    leaving that channel open: on reflection, the caller reading a verdict
    endpoint is the same caller who can immediately read `/help/my-recent-
    denials` afterward, so "retrospective, about the caller's own
    already-submitted queries" doesn't hold as a distinction — the caller's
    own already-submitted *verdict probe* is exactly the query in question.
    Every other operation's category is still surfaced there unchanged.
  - **`explain` is deliberately left untouched.** `explain_many`'s per-item
    `error` field already echoes the real validation message via
    `public_error_message` (unchanged, pre-existing behavior) — an
    authenticated caller using `explain` already needs identifier-level detail
    to debug their own query, a different posture from `verdict`'s "may I"
    question. Whether to also tighten `explain` was explicitly named as this
    item's own open question (not a copy of QG-19/QG-24's oracle, since
    `explain` requires the same authentication `verdict` does — the concern
    would be about debug-detail richness, not privilege); tightening it here
    would have been unrelated scope creep, so it is recorded as considered and
    rejected, not silently skipped.
  - The compiled plan (SQL text + touched-table list) is omitted by default —
    `Policy.verdict_include_plan`, off by default — since which tables end up
    referenced is itself data-dependent enough to be a discovery channel; an
    operator opts a connection in deliberately, the same posture
    `log_query_literals` already uses for a different disclosure axis.
  - Unlike `explain` (deliberately free — a pure, always-cheap compile
    preview with no DB round trip), `verdict` calls `enforce_query_quota`
    (consuming one unit **when the connection's policy configures a quota
    window** — off by default, same as every other read) and emits a
    redaction-safe `operation="query_verdict"` audit event on every outcome,
    including a quota/concurrency rejection: this item's own audit found the
    first cut called `enforce_query_quota` outside any try/except that led to
    `audit_query`, so a quota-throttled probe left no trace — fixed by
    wrapping quota reservation, connection resolution, and the concurrency
    admission in an outer handler that audits then re-raises, mirroring
    `execute`'s outer handler. `verdict` also now passes the same
    `principal_subject`/`max_queue_depth`/`max_queue_depth_per_principal`
    caps to `concurrency_slot` that `execute` does, closing an unbounded-queue
    gap the first cut left open — the same gap was then also found still
    open in `explain()` (a pre-existing issue, not introduced by this item,
    but standing out once `verdict` was fixed and `explain` wasn't), and
    fixed there too in the same pass for consistency. A quota/concurrency
    failure still propagates as a real error rather than being reported as
    `allowed=False` — it's "the endpoint couldn't answer right now", not a
    verdict about the query's shape; the outer handler that audits this
    failure classifies a `NotFoundError` (e.g. an MCP caller naming an
    unregistered connection, since MCP's `_service()` doesn't pre-resolve
    connection visibility the way REST's does) as `error_category="not_found"`,
    matching `execute`'s own outer handler, rather than falling through to
    the generic `"db_error"` label. The plan-compile-and-success-audit block
    also moved inside the outer `try` (compiling the plan text doesn't need
    the concurrency slot, only to happen before the `except`), so a
    `_compile_to_text` failure — or a failure in the audit call itself — is
    still audited and reported as a real error, instead of 500ing silently
    with the quota unit already spent and zero trace left behind.
  - **Scope is policy-and-schema shape only** — `verdict` stops at
    `_validate_and_compile` and does not evaluate the approval gate (item 92)
    or the cost-estimation gate, both of which `execute` still enforces. A
    query reported `allowed: true` can still be paused for human approval or
    refused by a cost cap when actually run; a caller treating `verdict` as a
    full execution simulation would be wrong to. Implementing either gate
    here would need a DB-free cost/sensitivity evaluation path of its own —
    out of scope for this item, left for a follow-up if a design partner
    needs it.
  See [The Core Request Pipeline](#the-core-request-pipeline) and
  [`docs/THREAT_MODEL.md`](THREAT_MODEL.md) QG-34.
