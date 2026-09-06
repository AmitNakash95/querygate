# The `io.github.agitmit/structured-query-ast` MCP extension

**Status:** internal half shipped (TODO.md item 131); external publication
decision-gated — see [Publication status](#publication-status) below.

**Extension identifier:** `io.github.agitmit/structured-query-ast`
**Version:** `1.0.0`
**Framework:** [SEP-2133](https://modelcontextprotocol.io/community/sep/sep-2133) —
the `2026-07-28` MCP protocol revision's formal, reverse-DNS-namespaced
extensions mechanism (the same mechanism `io.modelcontextprotocol/tasks`
moved into once it left experimental core).
**Schema file:** [`structured_query_ast.schema.json`](structured_query_ast.schema.json)
(generated — see [Regenerating the schema](#regenerating-the-schema))

## What this is

QueryGate's request boundary accepts exactly one shape of query: a validated
`StructuredQuery` JSON AST for reads, or one of `InsertStatement` /
`UpdateStatement` / `DeleteStatement` / `UpsertStatement` for governed writes
— never a raw-SQL string. This extension **declares that AST as a
citable, versioned JSON-Schema contract**, advertised under
`ServerCapabilities.extensions["io.github.agitmit/structured-query-ast"]` on
any QueryGate MCP server, so a security reviewer, a gateway author, or
another tool vendor can point at a stable artifact instead of reverse-
engineering the shape from QueryGate's tool schemas or product docs.

It exists because "structured, not raw SQL" is QueryGate's Structural pillar
(`CLAUDE.md`’s "North Star" section) and its durable moat is the *contract*, not
the implementation — see strategic play P2 in
an internal competitive analysis §7. A named extension is
citable in a security review and gives other implementers something concrete
to target, while every actual enforcement decision (policy, schema
validation, compilation, execution, audit) stays exactly where it already is:
inside QueryGate's one request pipeline.

## What is declared

The extension's per-extension settings, advertised at
`capabilities.extensions["io.github.agitmit/structured-query-ast"]` on
`initialize`, carry:

| Key | Meaning |
| --- | --- |
| `version` | The extension/contract's own semver, independent of QueryGate's package version. |
| `kind` | Always `"contract"` — see [Non-goals](#non-goals-hard-boundary) below. |
| `spec` | Repo-relative path to this document. |
| `schema` | Repo-relative path to the generated JSON-Schema file. |

The schema file itself (`structured_query_ast.schema.json`) has two
top-level members:

- **`read`** — the full JSON Schema for `StructuredQuery`
  (`querygate.query_ast.models.StructuredQuery.model_json_schema()`),
  covering every read primitive QueryGate's engine exposes: joins, the
  predicate/`WhereGroup` boolean tree, aggregates, window functions, CASE
  expressions, scalar functions, date-bucketing, set operations, CTEs, and
  top-N — the same AST documented in `docs/ENGINE_EXPRESSIVENESS_PLAN.md`.
- **`write`** — the JSON Schema for the write AST's discriminated union
  (`Union[InsertStatement, UpdateStatement, DeleteStatement, UpsertStatement]`
  from `querygate.write_ast.models`, generated via
  `pydantic.TypeAdapter(...).json_schema()` — the identical union
  `src/querygate/api/routes.py` and `src/querygate/mcp/tools/write.py`
  already type their request bodies with), covering governed inserts,
  filtered updates/deletes (via the narrowed `WritePredicate`/
  `WriteWhereGroup` filter tree), and upserts.

Both are emitted directly from the Pydantic models that are QueryGate's own
source of truth for these shapes — never hand-written. See
[Regenerating the schema](#regenerating-the-schema).

## Non-goals (hard boundary)

**This extension declares a contract. It never introduces a JSON-RPC method
that accepts, previews, or executes a query.**

Concretely:

- There is no `structured-query-ast/*` request method registered anywhere on
  the MCP server, and this extension never wraps or short-circuits
  `tools/call` itself. The only way to submit a query or write against a
  QueryGate deployment is `tools/call` against the existing
  `run_structured_queries` / `run_structured_writes` tools — the one request
  pipeline documented in `CLAUDE.md` ("The one request pipeline"). This
  extension's `Extension` subclass (`querygate.mcp.extensions
  .StructuredQueryAstExtension`) overrides only `settings()`; it does not
  override `methods()`, `tools()`, `resources()`, or `intercept_tool_call()`
  (the SDK's fourth contribution point — the one that can answer a
  `tools/call` without ever reaching the real handler), so it contributes
  nothing callable and intercepts nothing — a structural guarantee a
  conformance test enforces directly against a running server
  (`tests/unit/test_mcp_extensions.py::test_no_jsonrpc_method_registered_under_the_extension_namespace`
  and `::test_extension_does_not_intercept_tool_calls`), not just a claim in
  this paragraph.
- A namespaced `structured-query-ast/execute`-shaped method (mirroring how
  `io.modelcontextprotocol/tasks` *does* define request methods like
  `tasks/get`) would be a second query-execution path beside `tools/call` and
  the single `StructuredQueryService` pipeline — the identical rejection
  class as the GraphQL decision recorded in `docs/PRODUCT_GUIDE.md`'s
  Decision Log (2026-07-22) and as the permanent absence of any
  `execute_sql` field, endpoint, or tool (CLAUDE.md non-negotiable #1). The
  precedent this extension is modeled on (`io.modelcontextprotocol/tasks`)
  is explicitly the wrong shape to imitate here for that reason.
- Nothing about this extension changes how a query is validated, compiled,
  executed, or audited. It is purely descriptive metadata plus a published
  schema file — the MCP-extension-framework equivalent of a `.proto` or
  OpenAPI file, not a new capability.

## Publication status

**Shipped now (this item's "code half"):** the namespace is reserved in this
repository, the spec is written, the schema is generated from the live
Pydantic models (not hand-authored), the extension is declared from the real
`MCPServer` instance's capabilities, and a conformance test guards all three
plus the hard boundary above.

**Explicitly not done, and gated on a maintainer decision, not an agent's:**
actually publishing this as an external, adopted standard — registering the
namespace with any outside body, announcing it, or committing to
cross-version compatibility guarantees for third parties — is a standing,
outward-facing commitment. No network call, external PR, or registration
with any standards body was made implementing this item. See TODO.md item
131 for the full scope split.

## Regenerating the schema

The schema file is generated, never hand-edited. After any change to
`query_ast/models.py` or `write_ast/models.py` that changes the AST's shape,
regenerate it:

```bash
make mcp-extension-schema
# or directly:
poetry run python scripts/generate_mcp_extension_schema.py
```

`tests/unit/test_mcp_extensions.py::test_generated_schema_matches_published_file`
fails CI if the committed file drifts from a fresh `model_json_schema()` /
`TypeAdapter(...).json_schema()` call — the same fresh-vs-committed
drift-guard idea `tests/unit/test_credential_redaction.py` uses for the
credential invariant and `tests/unit/test_client_builder.py` uses for the
TypeScript client builder. Unlike those two, which compare against a live
schema in-process, this test generates the schema in a fresh subprocess (see
`_generate_schema_in_fresh_interpreter`'s own docstring for why).
