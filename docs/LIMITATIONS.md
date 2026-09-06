# Current limitations — the unabridged list

Being upfront about what is not done yet. The README carries the headline
subset and the [project-maturity statement](../README.md#project-maturity--read-this-before-you-evaluate);
this page is the full text, with nothing trimmed.

- **Cross-connection joins** only make sense when both connections are
  visible through one physical database engine (e.g. two MSSQL databases on
  the same server) — the `join_group` policy mechanism gates *intent*, but
  can't make a genuinely separate database server joinable in one SQL
  statement.
- **No RBAC beyond scopes/per-principal policy overrides** — `Principal`
  carries scopes/claims and `policy.yaml`'s optional `principals:` section
  can vary caps/allow-deny/mandatory-row-filters per caller, but there's no
  role hierarchy: no named roles, no inheritance, no group membership. A
  principal override is edited either by hand in YAML or through the admin
  UI's visual policy designer (`/admin/`, the "Principal" layer), which
  writes the same `principals:` section — the *editor* exists, the *role
  model* does not.
- **No stored-procedure catalog** — deliberately out of scope for this
  version; exposing stored procedures safely needs its own cataloging and
  policy-approval mechanism, not a generic pass-through.
- **Audit retention has an opt-in native WORM path with managed search** —
  `AUDIT_SINK_BACKEND=jsonl_chained_s3_worm` (item 134) archives to S3
  Object Lock (COMPLIANCE mode) alongside the local hash-chained ledger, and
  `GET /api/v1/admin/observability/worm-search` (item 134 phase 2) searches
  that archive directly — bounded time-range, filtered, paginated — but it's
  still opt-in: the default `jsonl_chained` sink is local-file-only, not
  itself WORM, and there's no browser UI over the search endpoint yet, REST
  only.
- **Config-governance's approval workflow is opt-in, not the default** — a
  caller with `admin:config:write` can stage and immediately apply a version
  in one session unless an operator sets `require_config_approvals` above 0
  (item 42), which then requires that many distinct `admin:config:approve`
  holders — never the author themselves — before an apply proceeds; there is
  no scheduled/timed apply either way. `POST /admin/config/diff` reports a
  resolved-access semantic diff
  (typed tightening/loosening/neutral changes, not just a YAML line diff) at
  the connection baseline, and `POST /admin/config/blast-radius` aggregates
  that same diff across every principal explicitly configured in
  `policy.yaml`'s `principals:` section — ranking access-expanding changes so
  a reviewer can tell a targeted change from a fleet-wide one — but it is
  bounded to the principals a deployment actually configured (up to 100) and
  reports `analysis_incomplete` rather than resolving every conceivable
  subject; blast-radius impact analysis beyond that bound (e.g. asynchronous
  or paginated evaluation for very large principal counts) is a later phase.
  The preview reports changed/unchanged only
  with read scope; write-only callers see submitted/inherited so write scope
  cannot become read scope. The browser admin control plane *does* drive this
  end to end — validate, stage, apply, roll back, and, when
  `require_config_approvals` is set, approve/reject a staged version
  (`src/querygate/admin_ui/app.js` calls `POST /admin/config/versions`,
  `.../apply`, and `.../{approve,reject}`), so the governance mutation API is
  **not** REST-only. What has no browser UI is *catalog* bulk operations,
  connection-scoped export/import, and triggering draft generation (TODO item
  38 phase 2), plus the WORM archive search endpoint above.
- **Writes are opt-in and deny-by-default, not absent** — see "Governed
  writes" above. `WritePolicy.enabled` is `false` until an operator turns it
  on per table/operation, so a default deployment is read-only in practice;
  there is still no raw-DML string field on either transport.
- **Pre-execution cost estimation covers Postgres and MSSQL only** —
  `max_estimated_rows`/`max_estimated_cost` (above) are enforced on both
  (TODO item 26 phases 1–2: inline `EXPLAIN` on Postgres, a dedicated
  `SET SHOWPLAN_XML ON` connection on MSSQL). MySQL, Snowflake and BigQuery
  have no estimator, so the check returns nothing there and those connections
  fall back to the reactive guardrails. The check is also fail-open by design:
  an estimation failure degrades to "not enforced for this query".
- **Distributed concurrency enforcement (Redis-backed) is opt-in** — the
  default is an in-process semaphore, correct for a single instance only;
  set `concurrency_backend: redis` for multi-instance deployments.
- **The `queue_mode=async` execution store is in-process only** — a caller
  polling `GET .../query/{admission_id}` must reach the same replica that
  started the query, or gets a `404` (TODO item 35 phase 3); a Redis-backed
  cross-replica variant is a documented follow-up, mirroring how
  distributed concurrency enforcement above and per-principal quota both
  shipped in-process before growing a Redis-backed variant. The in-process
  (non-Redis) `querygate_queue_depth` gauge remains single-process
  visibility only, like `querygate_concurrency_in_use`.
- **Semantic memory enrichment is opt-in and relationship-learning only** —
  item 32's governed adaptive loop is complete, including redaction-safe usage
  signals, confidence/decay/conflict-gated learned relationship proposals,
  background processing, and the existing review/publish/rollback workflow.
  There is still no embedding index or live model provider, and usage alone
  does not generate free-form table or column descriptions. Learned proposals
  remain hidden from agents until explicitly approved and published by an
  authorized reviewer through the workflow above.
- **Security review is first-party** — the repository includes a maintained
  threat model and adversarial regression suite, but has not yet undergone an
  independent penetration test or formal compliance certification.

MSSQL support (including the query-execution-timeout guardrail), MySQL
support (item 19 phase 1), and the Postgres statement-timeout guardrail are
all verified against real servers, not just unit-tested SQL text — see
`tests/integration/test_mssql_live.py`, `tests/integration/test_mysql_live.py`,
and `tests/integration/test_postgres_timeout.py`. **Snowflake support (item 19
phase 2) is compiler/rendering-level only and is NOT live-verified**: the
`DialectAdapter` is unit-tested by compiling its output against a real
`snowflake.sqlalchemy` dialect object; the `SessionDialectAdapter` is
unit-tested against recording fakes that assert the exact SQL text/params it
builds, not against that real dialect object (its statements are built
directly with `sa.text(...)` rather than compiled expressions). Neither is
tested against a live Snowflake instance — there is none available in this
project's environment (a proprietary cloud service, unlike Postgres/MySQL/MSSQL
which run in Docker), and
`snowflake-sqlalchemy`'s driver has no async SQLAlchemy engine support, so
`connections/engine.py` refuses to actually open a Snowflake connection today
— registering one fails with a clear, explained error rather than connecting.
Do not treat Snowflake as production-ready the way the other three dialects
are; see TODO.md item 19's Snowflake live-verification follow-up. **BigQuery
support (item 19 phase 3) is the same compiler/rendering-level-only, NOT
live-verified posture**, checked the same way (its `DialectAdapter` compiles
against a real, installed `sqlalchemy_bigquery` dialect object; its
`SessionDialectAdapter` is unit-tested against recording fakes). BigQuery has
a second, independent reason beyond the missing async driver that
`connections/engine.py` refuses to open a connection for it: `sqlalchemy_
bigquery`'s DBAPI resolves real Google credentials and builds a live client
at engine-construction time, not connection time. Do not treat BigQuery as
production-ready either; see TODO.md item 19's BigQuery live-verification
follow-up. The real-Postgres load/soak
harness also proves the observed database concurrency cap, overflow rejection,
queued completion, timeout cancellation, `queue_mode=fail_fast` never
waiting, and a caller-shortened `wait_timeout_seconds` being honored under
concurrent REST traffic; see [`docs/LOAD_TESTING.md`](LOAD_TESTING.md).
OAuth/JWT is implemented (`core/jwt_auth.py`) alongside static API keys.
Every `Policy` complexity cap (joins, select width, where-depth, group-by,
top-N, partition-by, batch size) is boundary-tested at exactly its configured
limit, and the SQLAlchemy compiler is fuzzed with Hypothesis-generated random
`StructuredQuery` combinations — including that a mandatory row filter
survives every generated shape — rather than only the fixed set of
hand-written cases; see `tests/unit/test_policy_boundaries.py` and
`tests/unit/test_compiler_properties.py`.

For the repeatable source/package and container release gates, see
[`docs/RELEASING.md`](RELEASING.md). Historical extraction notes are
kept outside the product surface under `archive/extraction/`.
