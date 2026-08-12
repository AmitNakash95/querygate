# Stored-procedure catalog — scoping & design proposal (item 18)

**Status (2026-08-11): proposal only, not started, not approved for
implementation.** This document is the design/scoping deliverable requested in
place of building item 18 directly — item 18 is XL (1–3+ weeks), a genuinely
new subsystem, and CLAUDE.md's own working agreement requires confirming scope
before starting something this size. Nothing in this document has been built;
no code changed to produce it. **Item numbering below (178–18x) is proposed,
not allocated** — no new `### N` heading, checkbox, or phase claim exists in
TODO.md/ROADMAP.md; only a pointer paragraph in item 18's existing TODO.md
body links to this document. If this plan is approved, allocating the
numbers and claiming the first phase in ROADMAP.md is the first real step of
implementation, not something this document does on its own.

**Owner:** engine/security (a stored-procedure call is closer in risk profile
to a governed write than a read — see §3). **Audience:** whoever scopes or
implements item 18 next, and any reviewer evaluating whether the proposed
design holds to CLAUDE.md's non-negotiables before a line of code is written.

---

## 0. Why this exists, and why it's scoped separately from everything else

The original QueryGate prototype had a stored-procedure pass-through
(`execute_stored_procedure`) that was deliberately **not** ported into the
current product. Exposing arbitrary stored procedures is a different, harder
safety problem than structured SELECT (or even governed INSERT/UPDATE/DELETE)
queries:

- A procedure can have **arbitrary side effects** — not just the row-level
  mutation a write AST bounds, but DDL, multi-table cascades, external calls
  (linked servers, `xp_cmdshell`-class extensions, UDFs that hit the network),
  or `COMMIT`s buried mid-body on some dialects.
- A procedure's **parameter shape is not independently verifiable**. Every
  other primitive in QueryGate — a table, a column, a write target — is
  checked against something SQLAlchemy can *reflect*: `INFORMATION_SCHEMA`,
  a live column list. There is no SQLAlchemy `Procedure`/`Routine` reflection
  object, and no standard, dialect-portable introspection surface for
  procedure parameters that would let QueryGate independently confirm a
  caller's declared shape matches reality the way `get_table_schema` does for
  a table (see §2 for exactly what's missing and why).
- A procedure's **read-only-ness cannot be derived, only declared**.
  Reflecting a table's columns tells you precisely what a SELECT can return.
  Reflecting (if it were even possible) a procedure's signature tells you
  nothing about what the body does. This is a hard structural fact, not an
  engineering gap to close later — see §2.

Because of that last point, this subsystem's shape is different from every
existing QueryGate subsystem in one specific way: **the catalog is the sole
source of truth**, not a policy/reflection layer sitting on top of an
independently-observable database fact. `connections/`, `policy/`, and the
read/write validation pipeline all check a caller's request against something
the database itself can confirm. A stored-procedure catalog entry cannot be
checked against the database this way — an operator's declaration *is* the
authorization boundary, in the absence of anything else to check it against.
That raises the bar on how carefully this subsystem must be designed and
reviewed, and is why this item stays out of the "when prioritized" backlog
until a design is confirmed rather than improvised mid-implementation.

**This document does not resolve every open question below.** Several are
flagged explicitly as decisions for whoever approves this plan to make, not
defaults to reach for under time pressure — consistent with how item 74
(MSSQL `nulls`) and item 172 (WORM segment-duplication binding) were each
deferred to an explicit, recorded decision rather than guessed at.

---

## 1. Non-negotiables this design must hold, restated for this subsystem

Everything below inherits CLAUDE.md's non-negotiables unchanged. Restated in
this subsystem's terms, since a new executable-code surface is exactly the
kind of change those rules exist to constrain:

1. **No caller-controlled raw SQL.** A procedure *call* is not raw SQL — the
   caller supplies a declared procedure name and typed parameter values, never
   a SQL string, never the procedure body. This holds even though the
   procedure's own body is (necessarily) SQL the *operator* wrote, not
   QueryGate — QueryGate never constructs or accepts SQL text from a caller
   for this feature, same as every other surface.
2. **No credential on any returned model.** A `PublicProcedureInfo`-shaped
   projection (mirroring `PublicConnectionInfo`) is the only thing ever
   returned; nothing about connection internals leaks through a procedure
   listing/describe response.
3. **Redaction-safe audit only.** A procedure call's audit event carries the
   procedure name and parameter *names*, never parameter *values* (mirroring
   `execution/write_execution.py`'s `_write_shape`, which records `{op,
   table, columns}` — names, never values) unless a future, explicit decision
   says otherwise for a specific low-sensitivity case.
4. **One database path.** Procedure calls go through the same
   `StructuredQueryService`-adjacent execution layer's connection/engine
   plumbing (`connections/engine.py`), never a second ad hoc DB access path.
5. **One catalog mutation path** — but **not** the existing
   `catalog/CatalogFileRepository`. See §4 for why this needs its own
   sibling store, not a reuse of the item-32 semantic catalog's file/governance
   machinery.
6. **Vary behavior by registered interface, not scattered `if`s.** Any
   dialect-specific procedure-call syntax difference (Postgres `CALL`, MSSQL
   `EXEC`, MySQL `CALL`, whether Snowflake/BigQuery even have a comparable
   primitive) goes behind a registered adapter method, never an inline
   `if dialect == ...`.
7. **Expose primitives; don't spoon-feed the agent.** A caller declares which
   procedure to call and supplies its declared parameters — QueryGate does not
   synthesize a procedure call from a natural-language description, and does
   not attempt to make an undeclared procedure callable by inference.
8. **The non-goals are product identity.** This subsystem is explicitly
   **not** a return to the old prototype's generic `execute_stored_procedure`
   pass-through. Every exposed procedure is individually, explicitly declared
   — there is no mode where an operator points QueryGate at a database and
   every procedure the DB user can see becomes callable. If this design ever
   drifts toward "list every procedure the connection's DB role can execute,
   let the caller invoke any of them," that is a non-goal violation requiring
   its own NORTH_STAR decision, not something to slide into during
   implementation.

---

## 2. The reflection gap, stated precisely (why this can't mirror `schema/reflection.py`)

`schema/reflection.py`'s `get_table_schema` works because SQLAlchemy's
`Table.autoload_with`/`MetaData.reflect` gives a **uniform, dialect-agnostic,
side-effect-free** introspection surface across all five supported dialects —
asking "what does this table look like" is itself a safe read with no
execution risk, and SQLAlchemy normalizes the answer into one `Column`
type/nullable shape regardless of which of the five dialects answered it.

No equivalent exists for procedures, for three separate reasons, not one:

1. **No uniform cross-dialect introspection API for parameter shapes.**
   Postgres (`information_schema.parameters`/`pg_proc.proargtypes`), MSSQL
   (`sys.parameters`/`INFORMATION_SCHEMA.PARAMETERS`), MySQL
   (`information_schema.parameters`), Snowflake, and BigQuery (whose routines
   API is structurally quite different from SQL-standard
   `INFORMATION_SCHEMA`) each expose this differently, and SQLAlchemy has no
   generic `Procedure`/`Routine` reflection object comparable to `sa.Table` to
   lean on. Building this would mean five hand-written, dialect-fragile
   introspection paths with no shared abstraction — a materially bigger and
   more fragile undertaking than table reflection ever was.
2. **Even successful introspection can't establish read-only-ness.**
   Reflecting a parameter list says nothing about what the procedure body
   does — whether it's a pure `SELECT`, does DML, calls other procedures,
   runs DDL, or reaches outside the database via a UDF/extension. There is no
   dialect-portable "is this routine read-only" flag to reflect even if
   parameter reflection existed.
3. **Introspected parameter types aren't necessarily the right validation
   surface anyway** — many dialects' declared parameter types are loose
   (nullable defaults, polymorphic types, data-dependent real behavior), so a
   successful reflection wouldn't give the same enforcement guarantee a
   reflected `sa.Table`'s columns give the read/write AST today.

**Consequence:** the catalog must be the *sole* source of truth, populated by
explicit operator declaration — not a reflection cache with an optional
manual override, the way the item-32 semantic catalog augments (never
replaces) reflected table/column facts. A procedure's parameter schema
(name, SQL type, nullable/required, direction) and its `confirmed_read_only`
flag are both **operator claims**, exactly the same epistemic status as
`WritePolicy.enabled`/`allowed_operations` today — not derived facts, and the
catalog's own internal validation (type-checking a caller's parameter values
against the declared schema) has to do the job schema reflection does for
tables, entirely from declared metadata with nothing independently
verifiable against the live database to check it against.

**This is the one point in the design an operator can get wrong in a way
QueryGate cannot detect:** if an operator mis-declares `confirmed_read_only:
true` for a procedure that in fact mutates data, nothing in this design
catches that — the declaration *is* the trust boundary. §7 names this
explicitly as a residual to disclose (in `docs/THREAT_MODEL.md`), not a gap
this design can close by itself.

---

## 3. Why a procedure call is modeled as write-adjacent, not read-adjacent

A procedure call has no meaningful place in the read pipeline (no `SELECT`
shape, no schema-reflection-checked columns to return) and a much closer risk
profile to the governed write path (`write_ast/`, `execution/write_execution.py`,
`execution/approval.py`) than to a read: side effects, no
independently-verifiable target, deny-by-default gating. The proposed design
reuses that machinery rather than inventing a parallel one:

- **Policy gating mirrors `WritePolicy`** (`policy/models.py:165-233`) —
  `ProcedurePolicy` nested in `Policy`, `enabled: bool = False` by default,
  `allowed_procedures: list[str]`, and (open question, see §7) either a
  per-procedure risk classification or a blanket
  `require_approval_for_non_read_only: bool`.
- **Approval-token machinery is reused verbatim, not reinvented.**
  `execution/approval.py`'s `issue_approval_token`/`verify_approval_token`/
  `_model_fingerprint` are already generic over "any fingerprintable
  pydantic model" — a `procedure_fingerprint(call)` following
  `write_fingerprint`'s exact shape, gated the same way
  `WritePolicy.require_approval_over_rows` gates a write, is the natural fit.
  No new token format, no new HMAC scheme.
- **Audit shape mirrors `_write_shape`** — procedure name + parameter
  *names* only, never values, matching non-negotiable #3 above.

**One deliberate deviation from the write path, flagged as its own decision
in §7, not silently assumed:** the write path's preview/diff mechanism (run
the DML inside a transaction, compute a diff, unconditionally roll back —
`execution/write_preview.py`) is **not safely generalizable to an arbitrary
stored procedure**. A procedure may perform non-transactional I/O, may not be
transaction-safe at all, or may issue an explicit `COMMIT` mid-body on
dialects that allow it (T-SQL procedures commonly do). Assuming
"preview = run it and roll back" for a procedure the way it works for a
bounded INSERT/UPDATE/DELETE would be **incorrect and potentially dangerous**
— it could execute real, non-rollback-able side effects while presenting
itself to the caller as a safe dry run. This is the single biggest structural
difference from the write-path template this plan otherwise leans on
heavily, and needs its own explicit decision before any preview/dry-run
concept is built (§7, decision 3).

---

## 4. Why this is a new sibling subsystem, not an extension of `catalog/`

The existing `catalog/` package (item 32) is a **descriptive overlay**:
business metadata (descriptions, aliases, sensitivity labels, relationship
hints) layered over tables/columns that are *independently discoverable* via
`schema/reflection.py`. It has draft/review/publish governance
(`CatalogFileRepository`, the 32B-1 review gate, `CatalogDraftProposal`)
because its content is human-authored *description* text that benefits from
a propose→approve→publish workflow — but it has no notion of "callable," and
CLAUDE.md's catalog section is explicit that it "must never become a query
execution or row-value search path."

A stored-procedure catalog is the opposite: it is the **sole source of
truth** for what's callable at all (§2), not a description layered on
something else already safely discoverable. Folding it into
`catalog/`/`CatalogFileRepository`/`CatalogDraftProposal` would either (a)
weaken the item-32 invariant that the catalog is never an execution path, or
(b) contort `CatalogDraftObjectType`'s closed enum (`TABLE`/`COLUMN`/
`RELATIONSHIP`) to include something structurally different in kind. Neither
is acceptable — this needs its own top-level package (proposed:
`src/querygate/procedures/`), its own YAML file (proposed:
`procedures.yaml`), and its own registry, following the `connections/`+
`policy/` file-per-subsystem convention instead of `catalog/`'s (which is
justified there specifically by the proposal/review/versioning machinery
this subsystem doesn't need in the same shape — see §7, decision 4, for
whether *any* review gate is warranted here and what it would look like if
so).

---

## 5. Proposed shape (mirrors `connections/` + `policy/`, not `catalog/`)

**Not final — this is the concrete strawman the phases in §6 would build,
subject to whatever §7's decisions change.**

```python
# procedures/models.py (proposed)

class ProcedureParameter(pyd.BaseModel):
    name: str
    sql_type: str            # operator-declared, e.g. "INTEGER", "VARCHAR(50)"
    required: bool = True
    direction: Literal["in", "out", "inout"] = "in"
    description: Optional[str] = None
    model_config = ConfigDict(extra="forbid")

class ProcedureDeclaration(pyd.BaseModel):
    """Privileged: the operator's full declaration. Never returned directly."""
    name: str                        # the caller-facing name (may differ from the DB routine name)
    connection: str
    procedure_name: str              # the actual DB routine name to CALL/EXEC
    schema_name: Optional[str] = None
    parameters: list[ProcedureParameter] = Field(default_factory=list)
    confirmed_read_only: bool = False   # operator CLAIM, never derived — see §2
    description: Optional[str] = None
    enabled: bool = True
    model_config = ConfigDict(extra="forbid")

class PublicProcedureInfo(pyd.BaseModel):
    """Credential-free, connection-internals-free projection — the only
    shape ever returned over REST/MCP. Mirrors PublicConnectionInfo."""
    name: str
    connection: str
    parameters: list[ProcedureParameter]
    confirmed_read_only: bool
    description: Optional[str] = None

    @classmethod
    def from_declaration(cls, decl: ProcedureDeclaration) -> "PublicProcedureInfo": ...
```

```python
# policy/models.py addition (proposed)

class ProcedurePolicy(pyd.BaseModel):
    enabled: bool = False                      # OFF unless explicitly turned on
    allowed_procedures: list[str] = Field(default_factory=list)
    require_approval_for_non_read_only: bool = True
    model_config = ConfigDict(extra="forbid")

class Policy(pyd.BaseModel):
    ...
    procedures: ProcedurePolicy = Field(default_factory=ProcedurePolicy)
```

Registry mirrors `ConnectionRegistry` exactly: `ProcedureRegistry.from_file`/
`from_entries`, module-level singleton (`get_procedure_registry`/
`set_procedure_registry`), wired into `config_reload.py`'s
`_reload_config_unlocked` and `ReloadResult` the same additive way item 13/48/93
each added their own store to that one reload path — **not** a second reload
mechanism.

REST surface (mirrors the write triad in `api/routes.py`):
- `GET /{connection}/procedures` — list, mirrors `list_tables`.
- `GET /{connection}/procedures/{name}` — describe, mirrors `describe_table`.
- `POST /{connection}/procedures/{name}/call` — mirrors `write/execute`.
- `POST /{connection}/procedures/approve` — only if §7 decision 2 lands on
  reusing the write path's approval-token gate; mirrors `write/approve`.

MCP surface (new file, auto-registers via `discover_and_register_tools()`
with zero additional wiring — `mcp/tools/procedures.py`):
`list_procedures`, `describe_procedure` (mirrors `list_tables`/
`describe_table`), `call_stored_procedure` (mirrors `run_structured_writes`'s
batch-list, per-item-error-isolated shape).

Execution: `ProcedureExecutionService.call(...)` mirroring
`WriteExecutionService.execute`'s shape — validate policy (deny-by-default
cascade: connection enabled → `procedures.enabled` → `allowed_procedures` →
per-parameter type/required validation against the declared schema) →
compile the dialect-specific `CALL`/`EXEC` primitive via a registered
adapter method (non-negotiable #6) → execute in one transaction → redaction-safe
audit (name + parameter names only) → typed result.

---

## 6. Proposed phasing (item numbers below are proposed, not allocated)

Highest allocated item number as of this writing is **177**
(`docs/TODO_ARCHIVE.md`/`TODO.md`, verify against the live file before trusting
this line — item numbers have gone stale in a plan doc before, see the engine
plan's own warning about exactly this). Proposed phases, each independently
shippable and independently reviewable, in dependency order:

- **Phase 0 (a follow-up item, numbered when prioritized) — Decisions + model +
  registry, no execution.**
  Resolve §7's open decisions, record them in the PRODUCT_GUIDE Decision Log
  (the same "decide before implementing" discipline item 172's segment-binding
  question and item 74's MSSQL-nulls question were held to), then build
  `procedures/models.py`, `ProcedureRegistry`, YAML loading, `config_reload.py`
  wiring, and `ProcedurePolicy`. No REST/MCP surface yet, no execution path —
  this phase is entirely declarative, reviewable on its own without touching
  the execution/security surface at all.
- **Phase 1 (a follow-up item, numbered when prioritized) — Read-only
  execution only.** Build
  `ProcedureExecutionService` restricted to `confirmed_read_only=True`
  procedures only (`allowed_procedures` gate + parameter validation +
  dialect-adapter `CALL`/`EXEC` compilation + audit), REST + MCP surface,
  full security review. Deliberately excludes non-read-only procedures
  entirely in this phase — ships the lower-risk half first, the same
  incremental-safety posture item 19's phased dialect rollout (MySQL, then
  Snowflake, then BigQuery) used.
- **Phase 2 (a follow-up item, numbered when prioritized) — Non-read-only
  procedures + approval gate.**
  Only once phase 1 has shipped and been reviewed: extend execution to
  `confirmed_read_only=False` procedures, gated behind the approval-token
  machinery from §3. This is the highest-risk phase and should get its own
  dedicated `security-invariant-reviewer` pass plus `adversarial-probe`
  coverage before shipping, not bundled into phase 1's review.
- **Phase 3 (a follow-up item, numbered when prioritized; optional) —
  Preview/dry-run, only if §7
  decision 3 concludes a safe concept exists.** If not, this phase is
  dropped and the design doc updated to say so explicitly, rather than left
  as a silently-abandoned placeholder.

---

## 7. Open decisions — for whoever approves this plan, not defaults to reach for

1. **Per-procedure risk classification vs. a single boolean.** Is
   `confirmed_read_only: bool` (this doc's strawman) sufficient, or does the
   real need call for a graded risk level (e.g. `read_only` /
   `bounded_write` / `unbounded`) with different gating per level? A single
   boolean is simpler and matches `WritePolicy`'s own binary
   enabled/disabled precedent, but may not be expressive enough for a real
   deployment's needs. **Needs a decision**, not a default.
2. **Does the approval gate reuse `execution/approval.py` unmodified, or does
   a procedure call need its own token semantics** (e.g. binding to a
   specific parameter *value*, not just the procedure name + parameter
   *names*, so an approved call can't be replayed with different argument
   values)? `write_fingerprint`'s existing shape fingerprints the whole
   statement including bound values already — likely a non-issue if
   `procedure_fingerprint` follows the identical pattern, but should be
   confirmed against a concrete adversarial scenario before assuming it,
   not asserted here.
3. **Is any preview/dry-run concept safe at all for an arbitrary procedure**
   (§3's flagged deviation)? Candidate answers, none assumed: (a) no preview
   ever, only immediate execution behind approval — simplest, safest; (b)
   preview only for `confirmed_read_only=True` procedures, where "preview"
   just means "render what would be called," since there's genuinely nothing
   to roll back; (c) some dialect-specific transactional dry-run primitive
   exists worth investigating per-dialect. This needs research and a decision
   before phase 3 (if built at all), not an assumption baked into phases 0–2.
4. **Does any review/approval gate belong on the *declaration* itself** (i.e.
   should adding a new procedure to the catalog require a second approver,
   the way config-governance four-eyes approval — item 42, not yet shipped —
   would work for policy changes), or is operator-authored YAML + the
   existing config-governance audit trail sufficient? Given a mis-declared
   `confirmed_read_only` flag is the one thing this design cannot
   independently verify (§2), a stronger case exists here than for an
   ordinary connections.yaml/policy.yaml edit — but adding a bespoke review
   gate for just this one file, before item 42's general four-eyes mechanism
   ships, may be premature machinery. **Needs a decision**, ideally informed
   by whether item 42 is likely to ship before item 18's phase 0.
5. **Multi-statement / result-set-returning procedures.** Some procedures
   return one or more result sets (common in MSSQL/MySQL), not just OUT
   parameters. Does phase 1's execution service need to handle multiple
   result sets, and if so, how are they typed/validated without a reflected
   schema to check them against (§2's core gap)? Not addressed by this
   document — needs its own investigation before phase 1 is scoped in detail.

---

## 8. What this document deliberately does not do

- **No code.** No model, registry, route, or test was written to produce
  this document — per the explicit scope of this request.
- **No item allocation.** No `### 178`–`181` heading, checkbox, or phase claim
  exists in TODO.md/ROADMAP.md — the phase/item numbers in §6 are a proposal
  for whoever approves this plan to allocate, not a claim already made.
- **No decision made on §7.** Each open question is stated with its
  tradeoffs, not resolved — resolving them is explicitly out of scope for a
  design/scoping pass and belongs to whoever approves and begins
  implementation, recorded in the PRODUCT_GUIDE Decision Log at that time.
