# Changelog

All notable changes to QueryGate are documented here.

## [Unreleased]

### Changed

- **The MCP server now speaks the final `2026-07-28` protocol revision**
  (TODO.md item 128), a full `mcp` SDK v1 → v2 migration. The in-query
  human-approval step-up flow (elicitation) was reworked around the new
  stateless-protocol request shape but keeps the same externally-visible
  behavior: a gated query or write still pauses for human approval and
  resumes once granted. **Upgrade impact:** an MCP client must speak the
  `2026-07-28` (or compatible) protocol revision; the OpenAI-function-calling
  example and any code reading `tool.inputSchema` (now `tool.input_schema`)
  should be checked against the new SDK's attribute names.
- **`GET /metrics` now requires authentication by default** (TODO.md item
  144), gated by a new least-privilege `admin:metrics:read` scope, alongside
  new `querygate_verdicts_total`/`querygate_verdict_duration_seconds`
  metrics for the `verdict()` decision-only endpoint. **Upgrade impact:** an
  existing Prometheus scrape config must present a credential with the new
  scope, or an operator can opt out via `METRICS_REQUIRE_AUTH=false` for a
  deployment whose network reachability is already restricted. The bundled
  Docker Compose and Helm reference deployments were updated to authenticate
  their own scrape.
- **The admin audit browser's pagination `cursor` ceiling was lowered from
  1,000,000 to 5,000** (TODO.md item 140), closing a resource-exhaustion gap
  where a request near the old ceiling could materialize roughly a gigabyte
  of parsed audit records. **Upgrade impact:** an automation paginating past
  page 100 (at the default `limit=50`) against `GET
  /api/v1/admin/ui/audit/events` will now receive `422` instead of a
  page — no real admin session was observed to need more.

- **Capacity/queue rejections now return `429 Too Many Requests` with a
  `Retry-After` header, not `422`** (TODO.md item 35 phase 3). Applies to a
  concurrency-limit timeout, a full admission queue, and the bare
  `ConcurrencyLimitError` case; MCP's equivalent error code moved from
  `VALIDATION` to `RATE_LIMITED`, matching the code quota rejections already
  use. **Upgrade impact:** a client that pattern-matches on HTTP `422` for
  "too many concurrent queries" (or on MCP's `VALIDATION` code for the same
  condition) must switch to `429`/`RATE_LIMITED`. The response BODY's string
  contract (`"too many concurrent..."`, `docs/LOAD_TESTING.md`) and the
  existing `X-QueryGate-Admission-*` headers are unchanged — only the status
  code, error code, and the added `Retry-After` header. No compatibility flag
  is provided; this is a deliberate, one-time migration, matching item 50's
  quota-rejection precedent. Rationale in `docs/PRODUCT_GUIDE.md`'s Decision
  Log (2026-07-28).

- **Postgres sessions are now pinned to UTC** (TODO.md item 102). QueryGate
  issues `SET LOCAL TIME ZONE 'UTC'` alongside the existing per-session lock and
  statement timeouts. Postgres resolves `EXTRACT`, `date_trunc` and every
  `timestamp`/`timestamptz` conversion against the session time zone, which
  QueryGate previously never set — so those answers silently followed whatever
  zone the server happened to be configured for, and the same query could return
  different values on two deployments.
  **Upgrade impact, on a Postgres server whose zone is not UTC:** a
  `timestamptz` column's `date_bucket` values and extracted date fields
  **change** (previously server-local, now UTC); a naive `timestamp` column is
  **unaffected**, because Postgres never consulted the session zone for those;
  and any comparison between the new `now()` and a naive column uses UTC. Write
  filters are affected identically, since the same session guardrails run on the
  write path. Columns are never converted — QueryGate reads a naive `timestamp`
  as stored and guarantees only that its own clock readings and field
  extractions are UTC. A server already running UTC (the default in the shipped
  container) sees no change. Rationale in `docs/PRODUCT_GUIDE.md`'s Decision Log
  (2026-07-26).

- **A date primitive requires a date column** (TODO.md items 102 and 117).
  `extract`, `date_add` and `date_bucket` over a column that is not a date/time
  type are rejected at validation with a typed 4xx. `extract`/`date_add` are new
  in this release; `date_bucket` has shipped for some time, but no caller can be
  broken because none had correct behavior — over an INTEGER column Postgres
  errors, SQL Server returns `1900-01-02` and SQLite returns `-4712-01-05`.
  T-SQL implicitly
  converts an int or a string to a datetime counted from 1900-01-01, so
  `DATEPART(hour, <int or varchar column>)` used to return a value (`0`, and
  `1900-01-03` for a shift) where Postgres errored — the same query, a hard
  failure on one backend and a plausible wrong answer on the other. A string
  column that genuinely holds a timestamp should be cast explicitly
  (`{"cast": {"col": "..."}, "to": "timestamp"}`); the rejection message says
  so. Postgres `interval` columns are unaffected — they remain valid operands.

### Added

- **The WORM audit archive tier now runs against any S3-API-compatible object
  store, not AWS S3 alone** — `AUDIT_WORM_S3_ENDPOINT_URL` (empty by default,
  meaning AWS S3 resolved by region exactly as before) points both the flush
  monitor and the managed search at the same endpoint, so a self-hosted or
  air-gapped deployment can have the immutable archive copy and not only the
  local hash-chained ledger. The store **must** implement S3 Object Lock:
  retention is still sent as Object Lock headers, so a store that ignores them
  would accept the writes and produce ordinary deletable objects — QueryGate
  logs an `audit.worm.custom_endpoint` warning at startup whenever the override
  is set with the WORM backend enabled. Google Cloud Storage and Azure Blob are
  **not** reachable this way; their immutability models are their own APIs
  rather than S3 Object Lock. QueryGate runs no live test against any
  third-party store — verify COMPLIANCE-mode retention against yours before
  relying on the archive. **Upgrade impact:** none. Leaving the variable unset
  preserves existing AWS behaviour exactly.
- **A deny-by-default third-party licence gate over every locked Python package**
  (`make license-check`, `scripts/check_licenses.py`), emitting
  `docs/THIRD_PARTY_LICENSES.md` — the standard answer to the "list your third-party
  components and their licences" question on a vendor security questionnaire. It runs in
  the default test suite and in `make release-check`, and `make sbom` now copies the
  report into `dist/` under `SHA256SUMS`, so it reaches a consumer with the release.
  Strong copyleft (GPL/AGPL) fails in either dependency group and cannot be waived; weak
  copyleft (MPL/LGPL) needs an individually recorded exception in
  `security/copyleft-license-allowlist.json`; and a licence string the gate does not
  recognise fails rather than being guessed. Each exception carries a machine-checked
  `facts` block verified against `poetry.lock` and the source tree on every run, so a
  waiver cannot outlive its own premises. Regenerate with `make license-report`.
  **Open finding:** `certifi` (MPL-2.0) is in the redistributed set; its review — like all
  five recorded — is still a draft awaiting confirmation, which the gate prints as a
  `NOTICE:` on every run. The inventory covers Python packages only; the container image's
  Debian and `msodbcsql18` layers are not assessed (TODO.md item 196).

- **Two Prometheus counters put the WORM archive's chain-integrity findings on
  `/metrics`** (TODO.md item 177):
  `querygate_audit_worm_search_chain_breaks_total` and
  `querygate_audit_worm_search_unverified_total`. The managed WORM search
  already detected a broken segment hash chain, but only ever reported it
  inside the response body of an ad-hoc search. Alert on
  `chain_breaks_total` — unlike `unverified_total`, it is not explained by a
  rotated `AUDIT_LEDGER_HMAC_KEY`. Both are unlabelled (no caller-chosen
  cardinality, no per-principal activity oracle over the archive) and are
  also recorded when a scan fails against S3 partway through, so an S3
  failure doesn't discard a break the scan had already found. **Two limits
  are documented rather than papered over:** QueryGate does not scan the
  archive on a schedule, so these only advance while a search runs (pair them
  with a cron'd search — see `README.md`); and they count findings per scan,
  not distinct segments, so alert on the first non-zero increase rather than
  on a magnitude. Purely additive.
- **MySQL 8.4+ as a supported connection dialect** (TODO.md item 19 phase 1),
  alongside the existing Postgres and MSSQL support — verified against a real
  MySQL server, not just rendering-only tests. Purely additive; no existing
  dialect's behavior changes. Two capability gaps are rejected rather than
  silently emulated: MySQL's bare `STDDEV`/`VARIANCE` are population
  statistics, so QueryGate maps them to `STDDEV_SAMP`/`VAR_SAMP` to match
  Postgres's/MSSQL's sample-statistic semantics instead; an upsert's
  `ON DUPLICATE KEY UPDATE` can't target a specific conflict-column set the
  way Postgres's `ON CONFLICT` can, so that shape is rejected with an
  explanatory error rather than silently ignoring the requested columns.
  Snowflake/BigQuery and a MySQL query-cost estimator remain open follow-on
  work.
- **Snowflake as a connection dialect — rendering/compilation only, NOT
  live-verified** (TODO.md item 19 phase 2). A real `DialectAdapter`/
  `SessionDialectAdapter` cover the same primitive surface Postgres/MSSQL/
  MySQL do (date bucketing, native `NULLS FIRST/LAST`, statistical
  aggregates, `LISTAGG`, a genuine `ARRAY_AGG`, `PERCENTILE_CONT`, window
  frames, set operations), backed by Snowflake's public SQL docs and checked
  against a real installed `snowflake.sqlalchemy` dialect object — but there
  is no Snowflake instance or account available to this project to actually
  connect to, and `snowflake-sqlalchemy`'s driver has no async SQLAlchemy
  engine support, so QueryGate deliberately refuses to open a live Snowflake
  connection today (a clear, explained error, not a silent failure or a
  confusing library-internal one). **Do not treat this the way MySQL's
  live-tested phase 1 is treated** — see TODO.md item 19 and its item 157
  live-verification follow-up before relying on it for a real deployment.
  Upsert (`MERGE`) is rejected rather than emulated, the same
  reject-don't-emulate posture as MySQL's `ON DUPLICATE KEY UPDATE` gap.
- **BigQuery as a connection dialect — rendering/compilation only, NOT
  live-verified** (TODO.md item 19 phase 3, the same posture as Snowflake's
  phase above). A real `DialectAdapter`/`SessionDialectAdapter` cover the
  same primitive surface the other four dialects do, backed by Google's
  public SQL docs and checked against a real installed `sqlalchemy_bigquery`
  dialect object — but there is no BigQuery project or GCP credentials
  available to this project, and `sqlalchemy-bigquery`'s driver has no async
  SQLAlchemy engine support either (plus a second, BigQuery-specific gap:
  its dialect resolves real Google credentials and builds a live client at
  engine-construction time), so QueryGate deliberately refuses to open a
  live BigQuery connection today. **Do not treat this the way MySQL's
  live-tested phase 1 is treated** — see TODO.md item 19 and its BigQuery
  live-verification follow-up item before relying on it for a real
  deployment. Upsert (`MERGE`) and `PERCENTILE_CONT` as a `GROUP BY`
  aggregate are both rejected rather than emulated, the same
  reject-don't-emulate posture as Snowflake's/MSSQL's respective gaps —
  BigQuery's `date_bucket`/`date_add` also, uniquely among the five
  dialects, dispatch on whether the operand is a DATE/DATETIME/TIMESTAMP,
  since BigQuery has three separate, differently-capable functions for each
  rather than one polymorphic function the way every other dialect here
  does. With MySQL, Snowflake, and BigQuery now all shipped, TODO.md item 19
  is marked done; a new item tracks any further dialect beyond these three.
- **Compliance-grade WORM (write-once-read-many) audit archival, with
  managed search over it** (TODO.md item 134, both phases). A new opt-in
  audit sink backend (`AUDIT_SINK_BACKEND=jsonl_chained_s3_worm`) composes
  the existing local hash-chained audit ledger with S3 Object Lock storage,
  batching events into retention-protected segments in the background with
  zero query-path latency impact — a flush failure never blocks or fails the
  triggering query, and a sustained outage drops only the oldest buffered
  events, visibly, via a dedicated metric an operator can alert on. A new
  `GET /api/v1/admin/observability/worm-search` endpoint (gated by its own
  `admin:audit:worm-search` scope, not implied by general observability
  read access) lets an authorized operator search the archive directly —
  bounded by a mandatory time window (default cap 730 days) and per-request
  scan limits, with a resumable cursor for a truncated page — including a
  day listing over the object budget (TODO item 184). **Upgrade
  impact:** none for a deployment that doesn't set
  `AUDIT_SINK_BACKEND=jsonl_chained_s3_worm`; a deployment that does should
  read the fail-open buffering caveat above and monitor
  `querygate_audit_worm_flush_failures_total`/
  `querygate_audit_worm_buffer_dropped_total`.
- **Purpose-bound access: a declared query purpose can now narrow what a
  caller sees, not just get logged** (TODO.md item 145). A new
  `StructuredQuery.purpose` field, checked against a connection's
  `Policy.allowed_purposes` allow-list, can apply an additional
  `Policy.purpose_policies` narrowing (extra denied tables/columns,
  mandatory row filters, or column masks) on top of the caller's base
  policy — a purpose can only narrow access, never widen it. Both the
  Python and TypeScript client SDKs gained a matching `.purpose(...)`
  builder method. Unset by default; a connection with no
  `allowed_purposes` configured is unaffected.
- **A `querygate-quickstart` CLI for a first governed query in minutes**
  (TODO.md item 146). Points it at a connection and it finds a table with
  non-sensitive columns, then prints a ready-to-run `curl` command, MCP
  tool-call JSON, and Python SDK snippet for a plain select, a filtered
  select, and a group-by aggregate — no server-side changes, no new
  authority.
- **A generated, checked-in procurement evidence page**
  (`docs/TRUST_EVIDENCE.md`, TODO.md item 147) assembling the SBOM status,
  compliance-control mapping, adversarial benchmark results, and
  threat-model coverage into one document a prospect's security team can be
  pointed at, regenerated via `make trust-page` so it can't silently drift
  from the sources it cites.
- **Automatic, TTL/lease-driven credential re-resolution** (TODO.md item
  135) for a Vault-backed connection secret with a reportable lease — no
  operator-triggered reload required. Opt-in via
  `CREDENTIAL_LEASE_REFRESH_ENABLED=true` (requires `VAULT_ENABLED=true`);
  a deployment that doesn't enable it is unaffected. The currently-shipped
  Vault KV v2 integration reports no lease, so the trigger is inert today
  and activates automatically, with no further change, against a future
  dynamic-secrets resolver.

- Agent-visible progress, an asynchronous REST execution lifecycle, and real
  query cancellation (TODO.md item 35 phase 3). MCP's `run_structured_queries`
  now reports two progress notifications per query when the calling client
  supports them (MCP's standard `notifications/progress`, via
  `Context.report_progress`) — "waiting for a concurrency slot" and "admitted,
  executing." A new `queue_mode=async` on `POST .../query` returns `202`
  immediately with an `admission_id`/`status_url` instead of blocking;
  `GET .../query/{admission_id}` polls the execution's state
  (`queued`/`running`/`completed`/`failed`/`cancelled`), and
  `POST .../query/{admission_id}/cancel` requests cancellation — free while
  still queued, and, for a running query, real dialect-level cancellation
  (Postgres `pg_cancel_backend`, MSSQL `KILL`) gated on a new deny-by-default
  `Policy.allow_query_cancellation` flag the operator sets only after
  granting the required DB-level permission (Postgres: `pg_signal_backend`
  role membership; MSSQL: `ALTER ANY CONNECTION`). Cancelling your own query
  needs no scope; cancelling another principal's needs the new
  `query:cancel` scope. In-process only for this pass (a Redis-backed
  cross-replica async-execution store is a documented follow-up, mirroring
  admission/quota's own phasing).

- Admin UI catalog-governance workspace (TODO.md item 38). A new Catalog
  domain in the `/admin/` control plane exposes item 32B's proposal
  review→approve/reject→publish→rollback loop as a browser workflow: a
  filtered proposal queue, side-by-side proposed-versus-published
  comparison, edit/approve/reject/publish actions gated per their existing
  least-privilege scopes, a publish-conflict preview, and connection-scoped
  catalog version history with rollback. Phase 2 adds bulk approve/reject/
  delete (checkbox selection across the queue), one-click export/import
  (backup/restore) of a connection's governed catalog history, browser
  triggers for `generate-drafts`/`learn`, a per-proposal review-history
  trail (a new `review_history` field on the single-proposal REST fetch
  only, never the bulk list), and a usage-signals browsing tab over the
  32C evidence the learner draws on. Every action is a thin wrapper over
  the existing governance REST routes — no new mutation path, no relaxed
  scope.

- Date and relative-time query primitives (TODO.md item 102). Three new members
  of the structured-query expression substrate — `{"extract": <expr>, "part":
  …}` for one integer field of a timestamp, `{"now": "timestamp"|"date"}` for
  the current UTC instant, and `{"date_add": <expr>, "unit": …, "amount": …}` to
  shift one — so an agent can express "orders in the last 30 days" without
  computing a cutoff timestamp itself. Usable anywhere a scalar belongs
  (projection, aggregate argument, either side of a predicate, inside a `CASE`).
  `dayofweek` is 0=Sunday..6=Saturday and `week` is the ISO-8601 week on every
  backend, normalized per dialect rather than passed through. Bounded by a new
  `Policy.max_interval_days` guardrail (default 3,660 ≈ 10 years) computed with
  upper-bound unit lengths so a larger unit cannot launder a longer reach.

- Admin observability overview API, phase 1 (TODO.md item 44). A new
  `admin:observability:read`-scoped `GET /api/v1/admin/observability/overview`
  (`api/admin_observability_routes.py`, `admin/observability.py`) returns a
  typed, redaction-safe `ObservabilityOverview` aggregated from the existing
  in-process Prometheus registry — query volume, success/rejection categories,
  average duration, queue depth and wait-by-outcome, concurrency
  in-use/max/utilization, per-principal quota rejections by kind, and
  cost-estimation attempts/unavailable/would-reject with a derived
  `fail_open_rate` — both globally and per connection, so an operator can see
  *which reason rejects the most*, *whether queue pressure is rising*, and
  *whether the cost-estimate gate is silently failing open* without parsing
  logs or scraping raw `/metrics`. It is explicitly an honest current-process
  snapshot (`source="process_snapshot"`, `durable=false`, `since`, and a `note`
  that counters are cumulative-since-start and per-replica under the default
  backends) — it never implies durable history. The overview is built only from
  already-public, low-cardinality metric labels (connection ids and fixed
  reason/outcome/quota buckets), never a query, value, principal, table, or
  column; its own least-privilege scope gates the whole response including the
  per-connection breakdown. Documented as QG-28 in `docs/THREAT_MODEL.md`. The
  `/admin/` control plane renders it as a read-only "Observability" panel —
  overview cards + a per-connection table + a banner echoing the snapshot's
  honesty note. Time-window trend charts (the phase-1 endpoint is a point-in-
  time snapshot with no stored history to plot), a config/catalog-change trend
  card (those events live in the audit stream, not the metrics registry), and
  querying an operator-configured external metrics backend for durable
  cross-replica history are phase 2.
- Admin-defined query templates, phase 1 (TODO.md item 48). A new
  `querygate/templates/` module + optional `TEMPLATES_FILE` let an admin
  pre-define named, parameterized `StructuredQuery` skeletons (typed parameter
  slots: type/required/default/min/max/max_length/allowed_values/is_list) that
  agents invoke *by name* instead of composing an arbitrary query — shrinking
  the effective surface to a finite, reviewed set of query shapes. At
  invocation the caller's parameters are type/constraint-checked, bound into the
  skeleton, and the result validated as a real `StructuredQuery` and run through
  the unchanged `StructuredQueryService`, so a bound template inherits every
  policy cap, allow/deny list, mandatory row filter, and guardrail an ad-hoc
  query has — a parameter can never smuggle SQL (a template is a stored AST, not
  a SQL string) or exceed policy. New surface: `GET /api/v1/query-templates` +
  `POST /api/v1/query-templates/{id}/run` (REST) and `list_query_templates` /
  `run_query_template` (MCP), plus a read-only "Query templates" browse panel in
  the `/admin/` control plane, all filtered per-principal by target-connection
  visibility (an unknown template and one on a hidden connection return the same
  non-enumerating 404). Invocations audit distinctly
  (`operation="run_query_template"`, template id, and parameter *names* — never
  values). Hot-reloadable via `POST /admin/reload-config` and validated by
  `querygate-validate-config --template-file`, which structurally validates each
  template's query skeleton (catching a malformed template at deploy, not only
  at first invocation) in addition to cross-checking its target connection.
  Documented as QG-26 in
  `docs/THREAT_MODEL.md`. Phase 2 (the governed create/edit/approve/publish/
  rollback workflow through item 32B's state machine) is not started.
- Policy-change blast-radius analysis, phase 1 (TODO.md item 41).
  `POST /api/v1/admin/config/blast-radius` (`admin/blast_radius.py`,
  `admin.service.compute_blast_radius`) aggregates item 40's semantic access
  diff across the connection baseline and every principal explicitly
  configured in `policy.yaml`'s `principals:` section, so a reviewer can tell
  a targeted access expansion from a fleet-wide one before staging a
  candidate. `admin/access_diff.compute_access_diff` gained an optional
  `principal` argument reused for this, rather than a parallel resolution
  path. Findings are ranked by risk — a removed mandatory row filter above a
  newly visible connection/table/column, above a loosened guardrail — and
  each is tagged `baseline` (affects every principal without an override) or
  `principal` (affects only that caller). Bounded work on every axis
  (at most 100 configured principals individually evaluated, a capped
  per-principal change list, a capped 25-entry `highest_risk` list), each
  with an honest `analysis_incomplete` reason rather than silent
  under-reporting. Shares `/diff`'s isolated-context loading, redaction
  posture, `admin:config:read` + `admin:config:write` scope requirement, and
  audit trail (new `"blast_radius"` action). Documented as QG-22 in
  `docs/THREAT_MODEL.md`. Phase 2 (async/paginated evaluation for deployments
  with more configured principals than the bounded pass can cover in one
  request) is not started.
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
  `docs/THREAT_MODEL.md`.
- Admin connection "test now" probe, phase 2a (TODO.md item 43).
  `POST /api/v1/admin/connections/{id}/test` triggers an immediate,
  out-of-band re-check of one connection, gated by its own
  `admin:connections:test` scope (deliberately independent of
  `admin:connections:read` — passive status visibility does not imply the
  ability to trigger a live probe). Reuses `HealthMonitor`'s exact
  ping/classification path and updates the shared cached status, so a
  following `GET` reflects the manual probe too. Rate-limited to one manual
  probe per connection per `admin_connection_test_cooldown_seconds` (new
  `AppConfig` field, default 10s) — a request inside that window gets `429`
  with `Retry-After` rather than opening another real connection to the
  target database. An unknown connection is `404`; a deployment-disabled one
  is `409`. Every probe attempt is recorded as a new redaction-safe
  `connection.probe` audit event (`ConnectionProbeEvent`,
  `audit_connection_probe`) — connection id, actor, probe result/failure
  category, never a raw driver error or connection string. Documented as
  QG-23 in `docs/THREAT_MODEL.md`.
- Admin connection health browser workspace, phase 2b (TODO.md item 43) —
  completing the item. A new "Connection health" tab in the existing admin
  control plane (`admin_ui/index.html`, `admin_ui/app.js`) renders phase 1's
  status list (dialect, status chip, last checked/success, latency,
  schema-reflected state, failure category) and gives each connection its
  own "Test now" button calling phase 2a's endpoint, updating that row in
  place from the response. No new backend endpoint or logic — a rendering
  layer over the already-tested REST surface, gated client-side on
  `admin:connections:test` (disabled with an explanatory `title` otherwise)
  and the connection's `enabled` flag. Verified against a real running app
  and real Postgres with a headless-browser driver, which surfaced and fixed
  two real pre-existing gaps: `admin_ui/app.js`'s audit-event renderer only
  special-cased two event types and silently mislabeled everything else
  (including the new `connection.probe` type, and latently
  `catalog.governance` too) as `Catalog ${action}`; and
  `api/admin_ui_routes.py`'s audit `event_type` filter allowlist didn't
  include `"connection.probe"`, so filtering the audit browser down to just
  probe events would have 422'd. Both fixed generically.
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
  writes SHA-256 checksums for the wheel, sdist, SBOM, and third-party licence inventory
  to `dist/SHA256SUMS`. Any known
  vulnerability without a reviewed entry in `security/dependency-audit-allowlist.json`
  fails the release (deny-by-default) — publishing to a registry and cryptographic
  signing remain phase 2, deferred until this project has a real publishing pipeline.
- **Two reproducible benchmarks answering the performance question every design
  partner asks before routing real traffic through the gateway**:
  `querygate-performance-benchmark`/`make performance-benchmark` measures per-request
  latency overhead (raw SQL vs. the guardrail pipeline vs. a full REST round trip,
  in-process over `ASGITransport`), and `querygate-load-benchmark`/`make load-benchmark`
  measures throughput/latency under concurrent load across a worker-count sweep, driving
  the real app over a real socket against a real, separately-spawned `uvicorn` process
  (not `ASGITransport` — a confound specific to concurrency measurement, see
  `docs/business/LOAD_BENCHMARK.md`). Both are informational (not pass/fail; the
  single-request tool's `--max-overhead-ms` is the one exception), need a real Postgres,
  and write a fresh, publish-ready `--markdown-out` results snapshot on every run
  (`docs/business/PERFORMANCE_BENCHMARK_RESULTS.md` / `LOAD_BENCHMARK_RESULTS.md`). See
  `docs/business/PERFORMANCE_BENCHMARK.md`/`LOAD_BENCHMARK.md` for full methodology.

- **Cumulative disclosure budget (optional, off by default)** — bounds the
  multi-query differencing that `min_group_size`'s k-anonymity floor alone does
  not: a prober re-runs one aggregate shape with a sliding constant and
  subtracts the answers. Two new `Policy` caps,
  `max_shape_repeats_per_window` (how many times one query *shape* may be
  re-run) and `max_aggregate_queries_per_window` (how many aggregate queries may
  touch one table at all), applied per principal, connection, declared purpose
  and table over a rolling `disclosure_budget_window_seconds`. Because the
  recorded query shape carries no predicate literals, a differencing probe is
  one shape re-sent N times — so re-runs are the signal. Exhausting a budget
  refuses the query with the existing REST 429 + `Retry-After` / MCP
  `RATE_LIMITED` contract, and is counted by a new
  `querygate_disclosure_budget_rejections_total{connection,budget_kind}`.
  Both caps require `min_group_size` on the same policy (a budget with no floor
  to defend is now rejected at load time rather than silently enforcing
  nothing), and only `execute` spends budget — `explain`/`verdict` return no
  rows. With `CONCURRENCY_BACKEND=redis` the window is a shared cross-replica
  budget; unlike the quota and concurrency limiters, that backend **fails
  closed**. **This bounds multi-query differencing, it does not close it** — a
  caller inside its budget still differences successfully, and no threshold is
  recommended because none has been calibrated against real traffic. **Set the
  per-table cap:** `max_shape_repeats_per_window` is currently evadable (item
  186) because the shape fingerprint does not canonicalize a referenced select
  alias, a cte rename, a nested-scope alias, or list order;
  `max_aggregate_queries_per_window` is unaffected. See
  `docs/INFERENCE_RISKS.md` R3.

### Fixed

- A crafted WORM-archive line whose chain position (`seq`) was
  type-confused rather than corrupt — a JSON string, float, or boolean that
  still carried a self-consistent hash, because the envelope is validated
  through a model that coerces those back to an integer before the digest is
  checked — defeated the archive's chain-linkage verification in one of two
  ways, depending on the type (TODO.md item 178). A **string** made
  `GET /api/v1/admin/observability/worm-search` fail with a generic HTTP 500
  instead of counting it, on any page resumed with a cursor (which the server
  itself issues at every ordinary page boundary). A **float or boolean** did
  not crash at all — worse, it was silently ACCEPTED as a valid link
  (`1 == 0.0 + 1`), so a retyped chain position passed verification and the
  crafted record was returned as a genuine event. Such a line is now reported
  like any other forgery class:
  counted in both `unverified` and `chain_breaks`, with that segment's scan
  stopping there. No genuine segment can be affected — QueryGate's own
  archival writer emits `seq` through a typed model, now asserted per line
  by `test_a_flushed_segment_writes_every_seq_as_a_bare_integer` — so this
  changes nothing for an untampered archive.
- A deployment running the tamper-evident hash-chained audit backend
  (`AUDIT_SINK_BACKEND=jsonl_chained`) lost four observability read
  surfaces — the personal denial-history view, the anomaly report, the
  config/catalog change-trend report, and the admin UI's audit browser —
  which each accepted only the plain backend (TODO.md item 136). Choosing
  the stronger audit posture silently cost those features; all four now
  read either backend.
- The admin config-diff tool (`POST /api/v1/admin/config/diff` and the
  blast-radius analysis built on it) never diffed column-masking policy
  changes at all, despite an inline comment claiming it did (TODO.md item
  148) — a mask added or removed between two policy versions was invisible
  to the reviewer workflow both are meant to protect. Fixed and covered by
  a regression test asserting a masked-then-unmasked column is now reported
  as a loosening.

- Query-template REST run endpoint returned HTTP 500 on every call (a merge
  regression, never in a release). `POST /api/v1/query-templates/{id}/run`
  (TODO.md item 48) was authored with its own inline `try/except` error
  mapping. It landed on `main` alongside the REST error-mapping
  centralization refactor, which moved every domain-exception → HTTP-status
  mapping into app-level handlers (`api/_errors.py`), renamed the admission
  header helper `_admission_headers` → `admission_headers`, and dropped
  routes.py's now-unused `CapacityTimeoutError` import. The merge converted
  the sibling `execute_query` route to the new minimal style but left
  `run_query_template` on the old one, so its `except CapacityTimeoutError`
  clause and `_admission_headers(...)` calls referenced names no longer in
  the module — raising `NameError` on *every* code path (success, capacity
  timeout, and even parameter-validation failures that should return 422).
  Fixed by aligning `run_query_template` to the same pattern as
  `execute_query`: bind + execute inside `with mask_unexpected():` and let
  the centralized handlers map the same exceptions to the same statuses and
  admission headers as before — identical externally-visible behavior, one
  mapping source instead of a stale duplicate. The five
  `test_query_template_api.py` integration cases now exercise the real route
  again. (The MCP tools and the `GET /query-templates` list endpoint were
  unaffected; only the REST run route regressed.)

### Security

- **The cumulative disclosure budget's per-shape cap was evadable, and the
  refusal handed over the evasion strategy** (TODO.md items 186 and 187). A
  prober could mint a fresh shape bucket per probe — and so never trip
  `max_shape_repeats_per_window` at any configured value — by walking a select
  alias and its `ORDER BY` reference, renaming a CTE, renaming a table alias
  inside a nested scope, or reordering a list. Measured at **20 distinct
  fingerprints for 20 probes**; it now measures 1. `shape_fingerprint` collapses
  every bare reference to a caller-authored name to one token, resolves table
  aliases from every scope rather than only the outermost, sorts every list, and
  ignores sort direction. Separately, the refusal message named which cap
  tripped, its configured value and the window length — which directly answered
  "will varying my shape help?" — and is now byte-identical for both caps;
  `quota_kind` remains on the exception for metrics and the operator breakdown.
  **Upgrade impact:** shape fingerprints changed, so an in-flight rolling window
  resets once on deploy. If you set only `max_aggregate_queries_per_window` on
  the previous advice that the per-shape cap was not load-bearing, both caps are
  now worth setting.
- **The disclosure budget's Redis script failed `CROSSSLOT` on Redis Cluster**
  (TODO.md item 192), turning a deliberately fail-closed privacy control into a
  hard outage on exactly the aggregate queries it was enabled to protect. It is
  the only limiter that passes several KEYS to one Lua script; those keys now
  carry a per-connection hash tag so they land in one slot. Neither `fakeredis`
  nor a single-node Redis can observe this, so the guard is a source-level
  assertion. **Upgrade impact:** the Redis key format changed; existing budget
  keys expire on their own window-length TTL.
- **A principal policy override could pass `validate-config` and then 500 every
  query for that principal** (TODO.md item 188). `policy/loader.py` validated a
  per-principal override against the `default:` layer only, which was harmless
  until `Policy` gained its first cross-layer validator: a default that sets
  `min_group_size`, a connection override that removes it, and a principal
  override that sets a disclosure cap each validated alone, loaded clean, and
  then raised at request time as a generic 500. Overrides are now validated
  against every merge base `PolicyStore.get` can actually resolve, so the
  failure lands at load time with an actionable message.

- **A denied-write-column configured with any capitalization other than
  all-lowercase (e.g. `{"Orders": [...]}`) was silently never enforced,
  regardless of the write statement's own table casing** (TODO.md item
  149) — the write-column deny list was inert for any table key that
  wasn't already lowercase. Found and fixed alongside two related
  case-folding disagreements: `Policy`'s table/column allow-deny lookups
  used `.lower()` in one place and `.casefold()` in another (differing on
  Unicode identifiers), and the semantic catalog's table/column resolution
  disagreed the same way — which could make a sensitivity label silently
  fail to reach the in-query human-approval gate for an affected identifier.
  A follow-up sweep (TODO.md item 150) extended the same `.casefold()`
  consistency fix through the compiler's `mandatory_row_filters` matching
  and the AST's own alias/CTE-name uniqueness validators, closing the
  identical disagreement in the tenant row-scoping path.
- **An approval token minted for a sensitive/expensive query on one
  connection verified unchanged for the byte-identical query on a
  different connection** (TODO.md item 151), since the token was bound
  only to a hash of the query itself. A `query:approve` holder who
  approved what they believed was a lower-sensitivity connection had, in
  fact, approved the same query shape everywhere it might be submitted.
  Approval tokens (both the REST approval endpoints and the MCP in-query
  approval flow) are now additionally bound to the specific connection and
  the specific principal that requested execution — a token can no longer
  be redeemed against a different connection or handed off to a different
  principal. A previously-issued (unbound) token keeps verifying exactly
  as before.
- **A cross-connection join's joined-in table was governed only by the
  primary connection's catalog labels and `Policy` — never its own
  connection's** (TODO.md items 155 and 156). A `StructuredQuery` joining
  connection A (primary) to connection B (`JoinSpec.connection`, gated by
  policy's `join_group` rule) resolved a joined table's catalog
  `sensitivity: pii` label, column masks, mandatory row filters, and
  table/column deny-list entirely against A — so a rule an operator
  configured only on B's own catalog/Policy never took effect for a query
  reaching that table through A. Both are now resolved against BOTH
  connections when they differ (never a replacement of one for the other —
  an operator's rule on the primary connection keeps applying exactly as
  before). No caller-visible API change; this is a pure tightening of
  existing enforcement, not a new capability.
- Audit read surfaces (the admin UI audit browser, the anomaly report, the
  config/catalog change-trend report, personal denial history) now verify
  each record's hash-chain envelope and disclose which audit backend
  actually produced the response, rather than silently trusting an
  envelope's shape and reporting the same `source` value regardless of
  backend (TODO.md item 137). A forged or tampered record is now excluded
  from the response and counted as malformed instead of being displayed.
- Hardened the audit-log read paths against resource exhaustion (TODO.md
  items 138–139): the three per-request audit readers now scan a bounded
  number of bytes/lines from the end of the file backward (the direction
  that actually serves what a caller wants — the newest window) instead of
  an unbounded forward scan of the whole file, and the read/write query
  AST's previously-unbounded `select`/`joins`/`group_by`/`order_by`/
  `correlate`/`ctes`/set-operation-arm lists now carry a hard, generous
  (10–100x a typical policy cap) size ceiling so a pre-rejection audit
  event can never itself become the oversized-line problem being guarded
  against.
- Upgraded `cryptography` to 50.0.0, resolving a Bleichenbacher
  padding-oracle advisory (`CVE-2026-69247`) in its PKCS7 decrypt
  functions (TODO.md item 143). QueryGate's own code never called the
  affected functions, but the dependency-audit gate now passes with zero
  allowlisted vulnerabilities for this package rather than a
  reviewed-but-present one.
- MCP's `tools/list` caching metadata (added by the `2026-07-28` protocol
  revision) is explicitly marked non-shared-cacheable, since QueryGate's
  visible tool set varies by the calling principal's scopes (TODO.md item
  129) — a shared MCP-aware intermediary caching a `"public"`-scoped
  listing could otherwise serve one principal's tool visibility to
  another.

- Inference/transitive-exposure adversarial test suite + design note (TODO.md
  item 55). Extends item 28's adversarial suite with a new attack *category*:
  reconstructing a *denied* value without ever selecting the denied column.
  `test_denied_column_cannot_be_used_for_inference` is now exhaustive across
  every AST position that can carry a column reference — scalar-function args,
  `CASE` when/then/else, aggregate/`percentile_cont`/`string_agg` columns,
  predicate `col_fn` and `value_col`, and composite join `extra_on` keys, on
  top of the existing where/group_by/having/order_by/top_n/join cases —
  proving the policy column walk (`validation/policy_validation.py`) harvests
  and rejects a denied column in all of them, with no new enforcement code
  required (the harvest was already complete; this locks it against
  regression). New `docs/INFERENCE_RISKS.md` enumerates the attack shapes and,
  for each, states whether QueryGate closes it (Class A — direct reference in
  any clause) or accepts it as a documented residual risk (Class B — derived/
  correlated permitted columns, underlying-data correlation, aggregate
  differencing with no minimum group size, existence probing), each Class-B
  residual carrying a demonstrating test asserting the current allowed-by-design
  behavior so the boundary is explicit and regression-locked rather than
  silently unaddressed.
- Minimum aggregation group size / k-anonymity guardrail (TODO.md item 88),
  closing the direct form of item 55's R3 residual. New `Policy.min_group_size`
  cap (`policy/models.py`, floor 2, `None`/default disables): when set, the
  compiler (`compiler/sqlalchemy_compiler.py`) injects `HAVING count(*) >= k`
  into every aggregate query — grouped or single-implicit-group — so any result
  group backed by fewer than *k* underlying rows is suppressed. A caller can no
  longer aggregate over a razor-thin filter to single out an individual
  (`count(*) WHERE id = X` returns nothing when fewer than *k* rows match). It
  is the aggregate analog of a mandatory row filter — policy-driven, injected,
  non-removable, and applied only to aggregate queries (plain row reads stay
  governed by mandatory row filters). It closes single-query singling-out, not
  multi-query differencing (which needs query-set auditing or differential
  privacy — deliberately out of scope, documented as still-residual in
  `docs/INFERENCE_RISKS.md` R3). Proven by compiler unit tests, real end-to-end
  suppression against SQLite, a security test tying it to R3, and policy-model
  validation.

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
