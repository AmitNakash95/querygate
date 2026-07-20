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

## What is QueryGate

QueryGate is an **agent-safe database access gateway**: it lets you connect
an AI agent to a real Postgres or MSSQL database, but the agent can never
submit raw SQL — there is no `sql` field, tool, or endpoint anywhere in the
codebase that would accept one. Every request goes in as a validated,
structured JSON object instead, and QueryGate runs it inside your own
infrastructure, next to your database, so credentials and data never leave
your network.

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
controlled read access to a production Postgres or SQL Server database —
typically in B2B software, fintech, healthcare, or any engineering
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
100000` without ever selecting `salary`) and infer its value indirectly. The
module's own helper, `_iter_column_refs`, walks every clause of the query
specifically so a denied column can't be smuggled in through a side door.
(`tests/unit/test_policy_validation.py::test_denied_column_rejected_when_only_used_in_where`
locks this behavior in.)

**Why it runs first:** it's the cheapest possible check — pure in-memory
comparison against config, no network call, no database round trip — so an
over-cap or policy-violating query is rejected in microseconds, before
QueryGate spends any effort or any database connection on it.

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

**Dialect-specific logic lives in one place.** QueryGate supports Postgres
and MSSQL (SQLite is used only internally, for tests/examples — see
`connections/models.py`'s `DatabaseDialect`). Almost the entire compiler is
dialect-agnostic SQLAlchemy Core, except for one thing that genuinely differs
per database: bucketing a date column into a day/week/month/quarter/year
("date_trunc"-style grouping). That logic is isolated to a single function,
`_date_bucket_expr`:

- **Postgres** has a native `date_trunc()` function that handles every
  granularity directly.
- **MSSQL** has no equivalent, so it's built from `DATEADD`/`DATEDIFF`
  (the standard MSSQL truncation idiom).
- **SQLite** (test/example path only) uses `strftime()` string formatting.

Keeping this in one function means adding a third real dialect later is a
change to one place, not a hunt through the whole compiler for
Postgres-flavored assumptions.

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
currently either `NullAuditSink` (discard) or `JsonlAuditSink`, which appends
one JSON object per line to a file with restrictive permissions (`0o600`),
designed so log rotation (renaming the file) works without needing to signal
the running process.

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

## Security Model

The [Core Request Pipeline](#the-core-request-pipeline) section explains what
happens to a query on its way through QueryGate — policy checks, schema
checks, compilation, concurrency control, session guardrails, audit logging.
This section is about something different: the structural guarantees that
hold regardless of the pipeline, because they're baked into QueryGate's data
model rather than into a stage that runs and could be skipped, misconfigured,
or worked around.

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

Why check the live schema instead of just trusting the model split above?
Because a schema is a *derived* artifact — FastAPI and FastMCP both generate
it automatically from whatever models are wired into a route or tool
signature. It's possible to define `PublicConnectionInfo` correctly and
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
  current.json                      — {"version_id": "<id>"} pointer
```

Every version is a complete, immutable snapshot of all three documents
together (never a partial diff), written with the same atomic
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

### Why policy is per-connection, not global

**File:** `src/querygate/policy/models.py`

A `Policy` bundles everything that bounds a query against one connection:
table/column allow and deny lists, per-query complexity caps (`max_joins`,
`max_select_columns`, `max_where_depth`, `max_group_by`, `max_top_n` and
`max_partition_by` for windowed queries, `max_limit`/`max_limit_aggregate`
for row counts, `max_response_bytes` for response size), execution
guardrails (`timeout_seconds`, `max_concurrency`, queue-depth caps),
`mandatory_row_filters` (a filter always AND-ed into every query touching a
table — e.g. tenant scoping, see below), and `join_group` (which other
connections this one may be joined with in a single query).

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
clause.

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
  to gate sensitive operations like config reload or catalog governance (see
  `src/querygate/core/scopes.py`).
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
  `StructuredQuery`.
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

### MCP transport

**Files:** `src/querygate/mcp/server.py`, `src/querygate/mcp/auth.py`,
`src/querygate/mcp/tools/*.py`, `src/querygate/mcp/exceptions.py`

**MCP** ("Model Context Protocol") is a standard protocol that lets an AI
agent discover a set of callable "tools" from a server — each with a name,
a description, and a typed input/output schema — and call them the same way
regardless of which server or which agent framework is on either end.
Instead of a human hitting REST endpoints with `curl`, an AI agent (Claude,
or any other MCP-capable client) connects to QueryGate's MCP server, lists
its available tools, and calls them directly as part of its own reasoning.

`mcp/server.py` builds one shared `FastMCP` instance (`mcp_server`) and, on
startup, calls `discover_and_register_tools()` (`mcp/tools/__init__.py`),
which imports every non-underscore-prefixed module under `mcp/tools/` —
so registering a new tool is just adding a file there, decorated with
`@mcp_server.tool(...)`. That server is exposed as its own ASGI app
(`streamable_http_app()`), wrapped in `MCPAuthMiddleware`, and mounted into
the main FastAPI app at `cfg.mcp_mount_path` (default `/mcp`) by
`setup_mcp()`.

**Auth works differently here than in REST**, because MCP tool functions
aren't FastAPI route handlers with a `Depends()` mechanism — they're plain
async functions FastMCP calls directly. So `mcp/auth.py` authenticates at
the ASGI layer instead: `MCPAuthMiddleware` builds the same
`CompositeAuthenticator` chain (API key → JWT → anonymous-if-local) used by
REST, and on every incoming request extracts the bearer token, authenticates
it, and — if it succeeds — stashes the resulting `Principal` (and the
`AppConfig`) in a `contextvars.ContextVar` before calling into the MCP app;
if it fails, it short-circuits with a 401 and never calls the wrapped app at
all. Individual tool functions then call `get_mcp_caller()` /
`get_mcp_config()` to read that context — this is how a tool like
`execute_structured_query` knows *who* is calling without the caller having
to pass identity as a tool argument (which an agent could tamper with).

The actual tools, one module per concern:

- `mcp/tools/connections.py` — `list_connections`.
- `mcp/tools/schema.py` — table listing/description tools.
- `mcp/tools/query.py` — `execute_structured_query`, `explain_structured_query`,
  and `execute_structured_queries` (batch) — the MCP equivalents of the
  REST query endpoints, calling the same `StructuredQueryService` with
  `surface="mcp"` (REST passes `surface="rest"`) purely so downstream
  logging/audit can tell which transport a request came from — the
  validation/compile/execute pipeline itself doesn't branch on it.
- `mcp/tools/help.py` — the guide/diagnostics tools (see below).

Every tool is wrapped in `@safe_mcp_tool` (`mcp/exceptions.py`). MCP tool
calls don't have HTTP status codes to raise on failure — a tool has to
*return* something — so `safe_mcp_tool` catches every exception the wrapped
function raises, maps it to a small `MCPErrorResult` (an error code like
`NOT_FOUND`, `FORBIDDEN`, `VALIDATION`, or `INTERNAL`, plus a safe message),
logs it, and returns that instead of raising. This mirrors the REST layer's
`HTTPException` mapping (`api/routes.py`'s `try`/`except` blocks) — same
outcomes, different shape, because that's what each transport's protocol
expects.

**A real gotcha worth knowing if you touch these files:** three tool
modules — `mcp/tools/connections.py`, `mcp/tools/schema.py`, and
`mcp/tools/query.py` — deliberately do **not** start with `from __future__
import annotations`, even though most of the codebase does. Here's why.
With that import, Python stores a function's type annotations as plain
strings instead of live objects (e.g. the annotation `StructuredQuery`
becomes the string `"StructuredQuery"`, to be resolved later, lazily).
FastMCP needs to resolve those annotations into real types to build each
tool's input schema, and it does that resolution against the *wrapping*
function's `__globals__` — because `safe_mcp_tool` uses `functools.wraps`,
the object FastMCP actually inspects is the wrapper defined in
`mcp/exceptions.py`, not the original tool function defined in, say,
`query.py`. So when FastMCP tries to look up the string `"StructuredQuery"`
in that wrapper's global namespace, it's looking in `mcp/exceptions.py`'s
namespace — which never imported `StructuredQuery` — and resolution fails
at server-startup registration time, not at call time, which makes it a
confusing failure to debug if you don't know this is why. Keeping
annotations as real (unstringified) objects in these three files sidesteps
the problem entirely, because then there's nothing to resolve. If you add a
new tool module with type annotations that need resolving, either skip
`from __future__ import annotations` there too, or confirm FastMCP can
still resolve them.

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
poetry run black --check src/ tests/ examples/   # make format-check — fails if anything is unformatted
poetry run black src/ tests/ examples/            # make format — rewrites files in place
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

Cryptographic artifact signing (e.g. Sigstore/cosign) is intentionally
deferred — see `docs/RELEASING.md` and `TODO.md` item 30 phase 2 — because
it needs a real publishing pipeline (a container registry / package index)
that doesn't exist yet for this project. The SBOM, dependency audit, and
checksum steps above are the phase-1 supply-chain work and already run on
every `make release-check`.

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
- **MCP (Model Context Protocol)** — the standard protocol AI agents use to
  discover and call a server's tools. One of QueryGate's two transports
  (the other is REST). See [Auth & Transports](#auth--transports-rest--mcp).
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
Postgres and MSSQL in production. SQLite is used only internally for tests
and examples — it's never a supported registry dialect for a real
deployment. See [Core Request Pipeline](#the-core-request-pipeline).

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

## Decision Log

Chronological list of notable technical/architectural decisions and the
reasoning behind them, newest first. Added to incrementally as work happens
— see the maintenance protocol above.

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
- **Cryptographic release signing (Sigstore/cosign) deliberately deferred.**
  Requires a real publishing pipeline (container registry / package index)
  that doesn't exist yet; the SBOM, dependency audit, and checksum steps
  already run on every release are treated as the phase-1 supply-chain
  work (`TODO.md` item 30). See [Testing, Release & Operations](#testing-release--operations).
