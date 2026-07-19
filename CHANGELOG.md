# Changelog

All notable changes to QueryGate are documented here.

## [Unreleased]

### Added

- Admin connection-operations status API, phase 1 (TODO.md item 43).
  `GET /api/v1/admin/connections` (`api/admin_connections_routes.py`) returns a
  credential-free, per-connection operational status built from the same
  `HealthMonitor` snapshot `/health` already maintains, gated by a new
  least-privilege `admin:connections:read` scope. Each entry reports dialect,
  enabled, a derived status (healthy/degraded/disabled/unknown), last-checked,
  last-success (persisted across a later failure), latency, schema-reflected
  state, and a stable redacted `failure_category`
  (authentication/unreachable/timeout/error). `HealthMonitor` now tracks
  last-success/latency and classifies failures **by exception type, never
  message**, so a raw driver error embedding a host/username/password never
  leaks through the API — it stays in stdout logs only, matching `/health`'s
  existing non-disclosure of database topology. Documented as QG-21 in
  `docs/THREAT_MODEL.md`. Phase 2 (a rate-limited "test now" probe action and
  the browser workspace that renders this view) is not started.
- Semantic access diff for config changes, phase 1 (TODO.md item 40).
  `POST /api/v1/admin/config/diff` (`admin/access_diff.py`,
  `admin.service.diff_candidate_access`) returns a server-derived,
  authorization-aware diff of *resolved* access — not a line diff of YAML —
  between the active config-governance version and a caller-supplied candidate
  (unset documents inherit from active). Phase 1's `connection_baseline`
  scope resolves the default and per-connection policy layers with no principal
  applied and reports typed `SemanticAccessChange` items for connection
  visibility, every guardrail cap, table/column access, mandatory-filter
  requirements, and join groups, each classified tightening/loosening/neutral
  with a direction-count summary and loosening-first ordering. Both snapshots
  load through item 39's isolated candidate-context path, so the live
  registry/policy/catalog and concurrent requests are provably untouched, and
  nothing is persisted. Like `/simulate`, it requires **both**
  `admin:config:read` and `admin:config:write` (it echoes resolved policy
  detail while resolving caller-supplied config/secret references), and its
  output structurally excludes static mandatory-filter values, resolved
  secrets, connection strings, predicate values, and raw YAML. When the
  per-principal override layer changes, an allow-list toggles between
  restricted and unrestricted, or the change list is truncated,
  `analysis_incomplete` is set with a reason rather than silently
  under-reporting. Documented as QG-20 in `docs/THREAT_MODEL.md`. Phase 2
  (per-configured-principal resolution — "reporting-agent gains `orders.total`"
  — which then feeds item 41's blast-radius analysis) is not started.
- Browser admin control plane (TODO item 31) at `/admin/`, served by the
  QueryGate process with no separate frontend runtime. It adds policy-filtered
  schema review, a layered visual policy designer and active-policy
  test-as-principal simulation, raw YAML editing with active/draft line diffs,
  dry-run validation, immutable staging, typed-confirmation activation and
  rollback, plus filtered/paginated browsing of the redaction-safe JSONL audit
  stream. All config mutations reuse `/api/v1/admin/config/*`; the CLI/YAML
  infrastructure-as-code path remains unchanged. New support APIs are gated by
  the existing `admin:config:read`/`admin:config:write` split, mandatory-filter
  values stay redacted, audit reads are memory-bounded, and the browser shell is
  protected by a same-origin-only Content Security Policy and no-store HTML.
- Governed semantic memory phase 32B-2, completing item 32B: connection-
  scoped export/import (`governance.export_connection`/`import_connection`,
  gated by one bidirectional `catalog:export` scope) serving both data
  portability and disaster recovery — import is a full destructive replace
  of the target connection's governed content, with version and generation
  ids (file-global, not per-connection) re-numbered/de-duplicated against
  the target catalog so importing can never collide with or corrupt an
  unrelated connection's history. Retention/deletion
  (`delete_proposal`/`bulk_delete_proposals`/`delete_version_record`, gated
  by `catalog:delete`) prunes only terminal-state records — a rejected
  proposal, or a published-then-rolled-back proposal together with its
  now-reverted publish record and the rollback record that reverted it
  (cascaded together to avoid a dangling reference), or a standalone
  rollback record — never a proposal whose publish is still live. New REST
  endpoints (`GET/POST .../export`, `.../import`, `DELETE .../proposals/
  {id}`, `POST .../proposals/bulk-delete`, `DELETE .../versions/{id}`) and
  CLI subcommands (`export`, `import`, `delete-proposal`, `delete-version`)
  complete the 32B governance surface — all nine originally-scoped scopes
  (`catalog:generate/review/edit/approve/reject/publish/rollback/export/
  delete`) now exist. 32C (adaptive usage learning) has not started.
- Governed semantic memory phase 32B-1: a deny-by-default review/publish/
  rollback workflow for the quarantined inferred draft proposals 32A-2
  generates. New `querygate/catalog/governance.py` implements an
  actor-attributed proposal state machine (pending → approved/rejected,
  approved → published), field-level edits while pending, bounded atomic
  bulk approve/reject (max 50), and a test-as-principal publish preview that
  never mutates the catalog. Publishing merges an approved proposal into a
  real table/column/relationship entry using the 32A-1 precedence gate: an
  existing verified field that would change is always a reviewable conflict
  and blocks the entire publish, never silently overwritten, and a draft's
  content model structurally cannot carry sensitivity, sampling, policy, or
  mandatory-filter fields, so publishing can never touch any of those. Every
  publish creates a durable, catalog-file-scoped version-history record
  (metadata-only diffs on the list view, full before/after on request);
  rollback reactivates a prior state and refuses if the entry has changed
  since publish or a table-creation rollback would collaterally remove
  columns/relationships added since. New REST surface under
  `/api/v1/admin/catalog/{connection}/...` and `querygate-semantic-memory`
  CLI subcommands (`list-proposals`, `show-proposal`, `edit-proposal`,
  `approve-proposal`, `reject-proposal`, `publish-proposal`,
  `preview-publish`, `list-versions`, `show-version`, `rollback-version`)
  give both a privileged workflow without an admin UI, gated by seven new
  least-privilege scopes (`catalog:generate/review/edit/approve/reject/
  publish/rollback`). Every generation and state transition emits a
  redaction-safe `catalog.governance` audit event (stable ids/actor/outcome
  only, never draft text or raw YAML). All governance mutations go through
  the same `CatalogFileRepository` lock and atomic write 32A already uses —
  there is no second catalog file or mutation path. Export/import,
  backup/restore, retention/deletion, and their `catalog:export`/
  `catalog:delete` scopes are phase 32B-2, not yet started; 32C's adaptive
  usage-learning loop has not begun.
- Governed semantic memory phase 32A-1, extending the existing schema catalog
  to format version 2 with deterministic stable IDs and durable provenance on
  every table, column, and relationship. Provenance carries explicit source/
  evidence, status, confidence, catalog/schema version, freshness, actors, and
  server-derived precedence; legacy version-1 catalogs load as manually
  verified content. A row-free schema fingerprint/diff engine stores raw
  database comments only as hashes. New REST
  (`GET /api/v1/{connection}/catalog/search`) and MCP (`search_catalog`)
  retrieval is deterministic, result/byte bounded, and filters table/column/
  relationship candidates by principal policy before tokenization, ranking,
  counting, or traversal. This storage/retrieval foundation makes no model
  call and cannot alter policy, mandatory filters, sensitivity labels, or
  query execution.
- Governed semantic memory phase 32A-2, completing phase 32A with a provider
  contract restricted to safe-default `disabled` and offline `manual` modes;
  strict fingerprint-bound structured imports become deterministic,
  idempotent, inferred draft proposals in a separate privileged review queue
  and never overwrite or enter agent-visible verified content. An opt-in,
  table-bounded background monitor persists row-free schema refreshes through
  an atomic cross-process-locked catalog update, marks only changed/removed
  targets and dependent relationships/proposals stale, rebinds unaffected
  active entries, and fails independently of query execution while logging no
  raw driver error. The new `querygate-semantic-memory` CLI supports refresh,
  manual draft import, and an offline versioned benchmark. Benchmark release
  thresholds are fixed in source (selection/relationship/stale recall,
  discovery-call reduction, and zero policy violations); the packaged corpus
  currently scores 1.0/1.0/1.0/0.75/0 and fails closed on regression. There is
  still no hosted/live provider or automatic publication path; those remain
  later governed phases.
- Queue-depth pressure controls and cross-replica admission state for capacity waiting
  (TODO.md item 35 phase 2). `Policy.max_queue_depth`/`max_queue_depth_per_principal`
  (both optional, unset/unlimited by default) cap how many callers may be *waiting* for a
  concurrency slot at once — separate from `max_concurrency`, which caps how many may
  *run* — so an unbounded `queue_mode=wait` pile-up can't itself become a
  resource-exhaustion vector. A caller past either cap is rejected immediately
  (`queue_wait_ms: 0`) with a distinct `admission_state` of `queue_full`, told apart from a
  genuine wait-timeout's `capacity_timeout` in REST headers, MCP fields, metrics
  (`querygate_queue_wait_seconds`/`querygate_queries_rejected_total` gain a `queue_full`
  bucket), and the audit event. When `concurrency_backend: redis` is selected, both the cap
  and the `querygate_queue_depth` gauge are enforced/computed against the same Redis every
  replica shares (`RedisConcurrencyLimiter.enter_queue`/`leave_queue`, mirroring its
  existing `acquire`/`release` sorted-set-plus-lease design), closing phase 1's
  documented single-process-only gap for Redis-backed deployments.
- Supply-chain SBOM and dependency vulnerability audit (`scripts/generate_sbom.py`,
  TODO.md item 30 phase 1), run as the final step of `make release-check` and available
  standalone via `make sbom`. Builds a throwaway virtual environment from exactly
  `poetry.lock`'s `main` dependency group (not an unpinned resolve), generates a
  CycloneDX 1.6 SBOM and a `pip-audit` vulnerability report scoped to that locked set, and
  writes SHA-256 checksums for the wheel, sdist, and SBOM to `dist/SHA256SUMS`. Any known
  vulnerability without a reviewed entry in `security/dependency-audit-allowlist.json`
  fails the release (deny-by-default) — publishing to a registry and cryptographic
  signing remain phase 2, deferred until this project has a real publishing pipeline.

### Known issues

- The dependency audit above currently allowlists 13 known advisories across `click`,
  `idna`, `mcp`, `python-dotenv`, and `starlette` at their `poetry.lock`-pinned versions —
  see `security/dependency-audit-allowlist.json` for the specific, code-verified reason
  each doesn't reach a real QueryGate code path, and TODO.md item 30 phase 2 for the
  tracked remediation. Notably, `mcp`'s DNS-rebinding advisory (PYSEC-2026-1617) is already
  mitigated independently at the application layer — `mcp/server.py` enables
  `TransportSecuritySettings(enable_dns_rebinding_protection=True, ...)` regardless of the
  SDK's own default, and this is covered by `make test-security`.

- A repeatable real-PostgreSQL load/soak harness for execution guardrails. Concurrent REST
  bursts are checked against PostgreSQL's observed active-query count, covering strict
  `max_concurrency` enforcement, overflow rejection, queued completion, and statement
  timeout cancellation under load (`make test-load` / `make test-soak`).
- Optional curated schema-catalog overlay (`querygate/catalog/`, `CATALOG_FILE`): business
  descriptions, aliases, relationship hints, sensitivity labels, default aggregation
  preference, and an `allow_samples` flag, curated per connection/table/column and merged
  into `describe_table` (MCP and REST) as a `catalog` object. Filtered by the same
  per-principal policy as everything else — denied columns and relationship hints toward
  denied tables never appear. Hot-reloadable via the existing config-reload endpoint and
  validated by `querygate-validate-config --catalog-file`.
- Pluggable secret resolution for connection strings (`querygate/secrets/`): `${VAR}`
  keeps resolving from the environment unchanged, and a new `${vault:path#field}` syntax
  resolves a HashiCorp Vault KV v2 secret (`VAULT_ENABLED`/`VAULT_ADDR`/`VAULT_TOKEN`/
  `VAULT_KV_MOUNT`/`VAULT_NAMESPACE`). A `SecretResolver` interface keeps future backends
  (AWS/GCP Secrets Manager) additive — no change to how connections are loaded, reloaded,
  or validated. Every `${...}` reference re-resolves on each config load/hot reload, so a
  rotated secret takes effect without a restart. Resolver errors never echo the configured
  token or the backend's own response text.
- Config-governance API (`querygate/admin/`, `/api/v1/admin/config/*`): validate, stage,
  apply, and roll back versioned connections/policy/catalog snapshots over REST, separate
  from and layered on top of the existing `/admin/reload-config` file-reload endpoint.
  Every version is attributed to the calling principal, gated behind new
  `admin:config:read`/`admin:config:write` scopes, and recorded in the same audit sink as
  query execution via a new `ConfigChangeEvent`. Applying re-validates a version
  immediately beforehand, so a version that stops validating between staging and applying
  is rejected, not silently activated. Rollback reuses the same apply endpoint against an
  older version id — history is never rewritten.
- Production deployment reference stacks under `deploy/`: a Docker Compose file and a Helm
  chart (app + Redis, config/secrets mounting, Prometheus scrape config, liveness/readiness
  probes, `runbook.md` for reloads/rotation/rollback). Both verified against a real
  deployment — a real Postgres for Compose, a real `kind` cluster for Helm — which caught
  and fixed a real bug: a fresh named Docker volume is root-owned by default and the
  production image runs as non-root, so the audit JSONL sink failed to write until a
  one-shot init container (Compose) / `fsGroup` (Helm, automatic) fixed volume ownership.

## [0.1.0] — 2026-07-18

Initial self-hosted release candidate.

### Added

- Structured, schema-validated database reads over REST and MCP with no raw-SQL input.
- Postgres and MSSQL adapters, live schema discovery, policy enforcement, query timeouts,
  response-size limits, and in-process or Redis-backed concurrency controls.
- Per-principal visibility and policy overrides with API-key and JWKS/JWT authentication.
- Readiness monitoring, Prometheus metrics, configuration validation/hot reload, and a
  redaction-safe persisted audit-event sink.
- Adversarial security regressions and live Postgres/MSSQL integration coverage.

### Release notes

- The supported release surfaces are the Python package and self-hosted container.
- SQLite remains an internal testing/fixture aid, not a supported connection dialect.
- Artifact signing, SBOM publication, and public registry automation remain tracked as
  post-0.1.0 distribution work.
