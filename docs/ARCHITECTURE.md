# Architecture — the module map

The request-pipeline diagram lives in
[the README](../README.md#architecture-at-a-glance); this page is the
package-by-package map that used to sit under it.

For the plain-language explanation of *why* the architecture is shaped this
way — the decisions, the tradeoffs, and the Decision Log — read
[`PRODUCT_GUIDE.md`](PRODUCT_GUIDE.md).

## Packages

- **`connections/`** — `ConnectionProfile` registry loaded from YAML,
  `${...}`-interpolated connection strings, per-dialect engine/session
  lifecycle (`dialects.py` is the only place Postgres/MSSQL/MySQL-specific
  SQL lives).
- **`secrets/`** — pluggable `${scheme:reference}` secret resolution
  (`SecretResolver` protocol): the built-in `env` backend plus an optional
  `vault` backend, selectable per-reference without touching how
  connections are loaded or reloaded.
- **`schema/`** — table/column reflection with a per-connection metadata
  cache (reflect once, reuse).
- **`query_ast/`** — the `StructuredQuery` Pydantic model. This is the
  entire agent-facing input surface.
- **`policy/`** — `Policy` model + YAML loader (default + per-connection
  overrides).
- **`catalog/`** — versioned semantic-catalog overlay (descriptions, aliases,
  relationship hints, sensitivity labels, durable provenance, schema
  fingerprints/diffs, and compact retrieval) merged into `describe_table`
  and filtered before search/ranking by the same policy as everything else.
  `governance.py` implements the deny-by-default draft review/edit/approve/
  reject/publish/rollback state machine, publish-time conflict detection
  against verified content, durable version history, connection-scoped
  export/import (backup/restore), and terminal-state-only retention/
  deletion — all through the same `CatalogFileRepository` lock as schema
  refresh and draft generation.
- **`templates/`** — admin-defined, named, parameterized `StructuredQuery`
  skeletons (item 48) loaded from an optional `TEMPLATES_FILE`. `bind_template`
  type-checks the caller's parameters, substitutes them, and hands the
  resulting `StructuredQuery` to the same `StructuredQueryService` an ad-hoc
  query uses — a template inherits every policy/schema/guardrail check and
  cannot exceed policy or contain raw SQL.
- **`validation/`** — schema-truth checks (does this table/column exist?)
  and policy checks (is it allowed? within caps?) — deliberately separate
  modules, run in that order, both before compilation.
- **`compiler/`** — turns a validated AST + policy into a SQLAlchemy Core
  `Select`, including dialect-aware date bucketing and top-N ranking.
- **`execution/`** — concurrency guardrail + the `StructuredQueryService`
  that ties validation → compilation → execution → result shaping together.
- **`audit/`** — structured stdout auditing plus a versioned, redaction-safe,
  append-only JSONL event sink, shared by both query execution and
  config-governance events.
- **`admin/`** — config-governance version store and orchestration: staging,
  applying, and rolling back connections/policy/catalog versions on top of
  `config_reload.py`'s existing swap mechanism.
- **`admin_ui/`** — same-origin browser control plane for schema review, visual
  policy design/simulation, config diffs, version activation/rollback, and
  redaction-safe audit browsing; all mutations reuse `admin/`'s API workflow.
- **`help/`** — packaged, versioned technical guide; deterministic offline
  search; config-field reference; caller-scoped access explanation; and
  redacted admin configuration summaries shared by REST and MCP.
- **`api/`** and **`mcp/`** — thin transport layers over the same
  `StructuredQueryService`; neither has its own query logic.
