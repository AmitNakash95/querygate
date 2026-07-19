# QueryGate threat model

**Status:** first-party security model for QueryGate 0.1.x
**Last reviewed:** 2026-07-19
**Scope:** the REST and MCP request paths, authentication, policy/schema
validation, query compilation and execution, Redis concurrency coordination,
configuration reload, the config-governance version store, secret
resolution, versioned semantic-catalog provenance/schema fingerprints,
policy-first catalog retrieval, and audit/log outputs in this repository.

This document explains what QueryGate is designed to defend, which controls
exist in code, and which risks remain with the operator. It is not an external
penetration test, compliance certification, or guarantee that a deployment is
secure regardless of its configuration.

## 1. Security objective

QueryGate permits an authenticated AI agent or application to perform bounded,
read-only, structured database queries without receiving a raw-SQL capability.
The gateway must preserve these properties even when the caller is malicious,
prompt-injected, confused about its permissions, or repeatedly sends unusual
but schema-valid AST combinations:

1. A caller can only discover and query connections, tables, and columns
   allowed by the policy resolved for that principal.
2. Predicate values remain bound data and cannot become executable SQL.
3. Tenant/row restrictions cannot be omitted by the caller.
4. Responses, public errors, schemas, and persisted audit events do not expose
   credentials or unintended database contents.
5. Query concurrency, duration, row count, batch size, and output bytes are
   bounded according to policy.
6. Security-relevant decisions remain attributable through audit events and
   correlation identifiers.

## 2. System and trust boundaries

```text
Untrusted agent/client
        |
        | HTTPS: REST or MCP
        v
Reverse proxy / load balancer  [operator boundary]
        |
        v
QueryGate authentication      -----> JWKS identity provider
        |
        v
Principal-aware policy + schema validation
        |
        v
SQLAlchemy compiler (SELECT only, bound values)
        |
        +-----> Redis concurrency coordinator
        |
        v
Customer database (read-only account recommended)
        |
        +-----> structured result caps
        +-----> stdout logs / persisted audit JSONL

Config load/reload (operator-triggered)
        |
        +-----> secrets/resolvers.py (env, optionally Vault KV v2)

Admin caller (admin:config:read / admin:config:write)
        |
        v
admin/service.py — validate/preview/stage/apply/rollback
        |
        +-----> admin/store.py (versioned connections/policy/catalog snapshots)
        +-----> config_reload.reload_config() (same swap as /admin/reload-config)
        +-----> audit trail (config.governance events)

Authenticated agent catalog search
        |
        +-----> resolve principal policy (connection/table/column)
        +-----> filter catalog entries + relationship endpoints
        +-----> tokenize/rank/count/budget the already-authorized view only
```

The agent/client, all request fields, natural-language intent, JWTs, API keys
presented by callers, and database row values are untrusted input. QueryGate's
process, policy/configuration files, deployment secrets, reverse proxy, JWKS
configuration, Redis, and audit destination are controlled by the operator.
Database servers are trusted to enforce their own permissions but are not
trusted to return client-safe error messages.

## 3. Assets

- Database credentials and connection topology.
- Secret-backend credentials (e.g. a Vault token) and resolved connection
  secrets, from load through use.
- Database schema, row data, aggregates, and sensitive column existence.
- Curated schema-catalog metadata (descriptions, relationship hints,
  sensitivity labels) layered on top of reflected schema.
- Semantic-catalog provenance, verification state, schema fingerprints/diffs,
  and the confidentiality of policy-hidden entries during retrieval.
- Principal identity, claims, scopes, and per-principal policy decisions.
- Availability of QueryGate and the databases behind it.
- Configuration integrity, particularly connection and policy changes.
- Config-governance version history — every staged/applied/rolled-back
  connections/policy/catalog snapshot and its actor attribution.
- Audit-event integrity, availability, and correlation metadata.
- Product-guide integrity and version freshness; caller-scoped guide context
  must not become an oracle for deployment configuration or hidden resources.

## 4. Attacker capabilities

The primary attacker is a remote caller that can invoke REST or MCP, possibly
with a valid low-privilege credential. This includes an otherwise legitimate
agent whose prompt or tool context has been injected. The attacker may:

- submit arbitrary JSON values and every valid combination of query-AST fields;
- enumerate identifiers and compare allowed results or error behavior;
- replay requests and run them concurrently;
- possess a valid token for another tenant, environment, issuer, or audience;
- attempt to induce database, driver, timeout, Redis, or audit failures; and
- access a developer's browser while a local MCP server is reachable.

An attacker with write access to QueryGate's policy files, environment,
container, database credentials, reverse proxy, or host is outside the remote
caller model. Those are privileged operator boundaries and require normal host,
CI/CD, and secrets-management controls.

## 5. Threats and controls

| ID | Threat | Implemented controls | Verification |
|---|---|---|---|
| QG-01 | Raw SQL or SQL injection | No raw-SQL field or endpoint; Pydantic forbids extra AST fields; identifiers resolve to reflected SQLAlchemy objects; predicate values use binds | `test_raw_sql_rejected_on_every_query_endpoint`; security test `test_predicate_payload_is_bound_data_not_executable_sql` |
| QG-02 | Table/column policy bypass through filters, joins, grouping, having, ordering, or ranking | Policy gathers every qualified reference before reflection; deny wins; matching is case-insensitive | Security parametrization `test_denied_column_cannot_be_used_for_inference` and denied-table smuggling test |
| QG-03 | Implicit/undeclared table injection | Every referenced table must be the `from` table or an explicit join; each join must reference the joined table and connect to the existing graph | Security test `test_undeclared_table_reference_is_rejected_before_reflection`; schema-validation join tests |
| QG-04 | Schema discovery leaks hidden resources | Connection listing, direct access, table listing, and column description resolve the caller's policy; hidden connections return the same not-found shape as unknown ones | `test_connection_visibility.py`; REST/MCP principal-scoping integration tests |
| QG-13 | Curated schema-catalog metadata discloses a hidden table/column | Catalog entries are display-only and never bypass policy; a denied column is excluded from `describe_table` before catalog lookup happens; a relationship hint pointing at a policy-denied table is dropped from the response | `test_catalog_relationship_hint_cannot_disclose_a_denied_table`; catalog unit/integration tests |
| QG-05 | Cross-principal or cross-tenant confusion | Immutable request-local `Principal`; per-principal policy resolution; claim-derived mandatory row filters; MCP caller stored in a reset `ContextVar` | Security principal-isolation and aggregate row-filter tests; policy-loader and MCP tests |
| QG-06 | Authentication spoofing or token confusion | Constant-time API-key comparison; JWT signature, algorithm, issuer, audience, expiry, required subject, and configured JWKS verification; anonymous bypass only in local/development when no real authenticator is configured | `test_auth.py`, `test_jwt_auth.py`, REST/MCP authentication integration tests |
| QG-07 | Credential, row, predicate, or backend-detail leakage | Public connection DTO has no credential field; unexpected REST/MCP/batch errors are generic; persisted audit schema excludes SQL, params, intent, exception text, and rows; explain/audit SQL is parameterized by default | `test_credential_redaction.py`, `test_audit.py`, security public-error tests |
| QG-08 | Oversized or abusive requests/results | AST depth/width/join/top-N/batch caps; server-side row limits; hard serialized-row byte ceiling, including a single oversized row; database timeout; per-connection concurrency, including a caller-selected `queue_mode`/`wait_timeout_seconds` that can only shorten the operator's `concurrency_wait_seconds` ceiling, never lengthen it; `max_queue_depth`/`max_queue_depth_per_principal` bounding how many callers may wait at once (cross-replica when `concurrency_backend: redis`), so an unbounded waiting queue can't itself become a resource-exhaustion vector; optional Postgres pre-execution `EXPLAIN`-based row/cost estimate rejection before a likely full scan or join explosion runs | Policy/service tests, real timeout tests, security oversized-row test, `test_postgres_cost_estimation.py`, `test_caller_cannot_extend_the_operators_concurrency_wait_ceiling`, `test_unbounded_waiting_queue_is_capped_not_a_dos_vector`, `test_max_queue_depth_per_principal_prevents_one_caller_starving_another` |
| QG-09 | Cross-connection access | Both connections must be visible to the principal and share the resolved `join_group`; the join uses the primary engine and declared physical database mapping | Cross-connection schema tests, including hidden-connection denial |
| QG-10 | Unauthorized configuration changes | `/admin/reload-config` requires `admin:reload-config`; the config-governance API (`/admin/config/*`) separately requires `admin:config:write` for validate/preview/stage/apply/rollback and `admin:config:read` for history/inspection; every path fully validates new content before atomic registry/policy replacement | REST reload scope tests, config-reload tests, `test_config_governance_write_endpoints_require_write_scope`, `test_config_governance_read_endpoints_require_read_scope` |
| QG-11 | Browser-driven DNS rebinding against local MCP | MCP validates `Host` and, when present, `Origin`; protection is enabled by default with loopback hosts allowlisted | Security test `test_mcp_rejects_unapproved_host_header` |
| QG-12 | Audit data becomes a new exfiltration channel | Narrow versioned event schema, explicit normalization without values, mode `0600`, append-only application writes, optional `fsync` | `test_audit.py` |
| QG-14 | Secret-backend failure or misconfiguration discloses a Vault token or backend response text | `SecretResolver.resolve` errors carry only the reference being looked up and the exception type, never the configured token or the backend's own error/response text; a resolved secret value is never returned by any REST/MCP response, matching the existing credential-redaction guarantee | `test_vault_resolver_error_never_leaks_token_or_backend_response_text`, `test_vault_resolved_secret_never_appears_in_connection_listing_or_errors` |
| QG-15 | A config-governance version that stops validating (e.g. an env var/Vault path it depends on disappears between staging and applying) gets silently activated anyway | `apply` re-validates a version's content immediately before activating it, regardless of whether it validated when staged; a failed re-validation leaves the active version and pointer untouched and is recorded as a rejected audit event | `test_apply_rejects_a_version_that_no_longer_validates`, `test_invalid_staged_version_is_rejected_not_silently_applied` |
| QG-16 | Product-guide search, diagnostics, or caching disclose another principal's connections, policies, secret references, or hidden schema identifiers | Static search indexes only packaged public topics; live access context is authorized and assembled separately for each request; no live context cache exists; admin inspection requires `admin:config:read` and reconstructs an allowlisted redacted projection rather than filtering raw YAML afterward | `test_product_guide_security.py`, `test_redacted_configuration_excludes_secrets_policy_names_and_other_principals`, REST/MCP guide integration tests |
| QG-17 | Semantic catalog poisoning, stale guidance, or search/ranking/count/relationship traversal discloses a policy-hidden table or column | Every table/column/relationship entry has stable provenance, explicit status/confidence/schema freshness, and server-derived precedence; rejected/archived content is excluded and stale/draft content is labeled; verified entries cannot be overwritten by lower-precedence proposals and semantic merging cannot change sensitivity; principal policy filters candidates and both relationship columns before tokenization, ranking, counting, or byte budgeting, and removes free-form fields that echo an exact hidden identifier; public citations hash evidence references and omit actor/model identities; schema snapshots contain structure and comment hashes, never row values or raw comments | `test_catalog_retrieval.py`, `test_schema_memory.py`, `test_precedence_never_allows_inferred_overwrite_of_verified_or_sensitivity`, `test_catalog_search_filters_before_ranking_counts_and_relationship_traversal`, REST/MCP catalog-search integration tests |

## 6. Error and data-disclosure policy

Policy, schema, not-found, and concurrency errors are actionable client errors
and may identify the caller-supplied table or column that was rejected.
Unexpected query/database exceptions are never returned verbatim through REST,
MCP, or batch results. Only explicitly typed client-validation failures are
actionable; a coincidental `ValueError` from a driver is still masked. Callers
otherwise receive `An unexpected error occurred.` and can provide the response
correlation id to an operator.

Operator stdout logs can contain stack traces, redacted SQL, intent text, and
database exception details. They are more sensitive than persisted audit JSONL
and must be access-controlled and retained accordingly. Policies should leave
`log_query_literals=false` unless literal SQL in operator logs is explicitly
acceptable.

## 7. Deployment requirements

The code controls above assume a correctly operated deployment. `deploy/`
(Docker Compose and Helm references, both verified against a real
deployment — see `deploy/README.md`) applies every requirement below by
default: non-root container user, no bundled database, Redis required once
more than one replica runs, and secrets kept out of version-controlled
config. Treat deviations from it as deliberate, reviewed decisions, not
defaults.

- Terminate TLS at a trusted proxy or at the service boundary; do not expose
  plaintext QueryGate traffic across an untrusted network.
- Set `ENVIRONMENT=production` and configure REST/MCP API keys or JWT. Never
  use the local anonymous mode on a shared host or network.
- Keep MCP DNS-rebinding protection enabled. Add the exact `Host` header used
  by the production proxy to `MCP_ALLOWED_HOSTS`; add browser origins only when
  they are intentionally supported.
- Give each database connection the least-privileged, read-only account
  QueryGate needs. Database grants are the final defense if application policy
  is misconfigured.
- Keep connection files and environment/secrets readable only by the service
  identity. Do not place production connection strings in source control.
- When using `${vault:...}` references, scope `VAULT_TOKEN`'s Vault policy to
  only the secret paths QueryGate needs (read-only), and rotate it through
  the deployment's normal secret-rotation process — QueryGate re-resolves
  every `${...}` reference on each config load/reload, so a rotated token or
  secret value takes effect on the next reload without a restart.
- Use private, authenticated, TLS-protected Redis in multi-instance setups.
  Consider `CONCURRENCY_REDIS_FAIL_OPEN=false` when database protection is more
  important than availability during a Redis outage.
- Protect stdout logs and audit storage. Send audit events to retained or WORM
  storage when regulation requires immutability, and alert on
  `audit.sink.write_failed`.
- Restrict `/metrics`, `/health`, and admin routes at the network/proxy layer as
  appropriate for the environment.
- Grant `admin:config:write` only to identities that should be able to change
  what QueryGate connects to and enforces — treat it as equivalent in
  sensitivity to `admin:reload-config`. Grant `admin:config:read` more
  broadly if config-change visibility (not the ability to change it) is
  useful for an on-call/audit role; both are read-only into the same version
  history otherwise available only via the persisted audit trail.
- Back up `AppConfig.config_governance_dir` (default `var/config_versions/`)
  like any other durable state — it is QueryGate's own version history, not
  reconstructable from `connections.yaml`/`policy.yaml` alone once history
  has diverged from what's currently on disk.

## 8. Residual risks and explicit non-goals

- **Authorized inference:** QueryGate blocks use of denied columns, but a caller
  authorized for an aggregate can still infer facts from permitted counts and
  narrow filters. Minimum-group-size, differential-privacy, and query-history
  controls are not implemented.
- **Pre-execution cost:** limits and timeouts are primarily reactive, but an
  optional Postgres `EXPLAIN`-based check (`Policy.max_estimated_rows`/
  `max_estimated_cost`, unset/disabled by default) can reject a likely full
  scan or join explosion before it executes (TODO item 26 phase 1). MSSQL has
  no equivalent yet (item 26 phase 2), and the check is fail-open: an EXPLAIN
  failure degrades to "not enforced for this query" rather than blocking it,
  so it is a complement to the reactive guardrails, not a replacement. The
  fail-open path is observable, not silent —
  `querygate_cost_estimation_unavailable_total{reason}` — and
  `Policy.cost_estimation_mode: observe` lets an operator measure what a
  threshold would reject against real traffic before switching a connection
  to `enforce`, since Postgres's planner-cost units aren't portable across
  schemas/hardware.
- **Large values in process memory:** the output byte cap prevents an oversized
  row from leaving QueryGate, but the database driver must first receive that
  row. Database-side statement limits and denial of large/blob columns remain
  important.
- **Configuration governance has version history and rollback, but no
  approval workflow yet:** the `/admin/config/*` API validates, versions,
  previews document-level changes, attributes, and audits every change, and a single `admin:config:write`
  caller can stage and apply a version in one session with no second-
  approver/four-eyes requirement, scheduled apply, or detailed semantic diff.
  The preview is deliberately content-free; a write-only caller sees only
  submitted/inherited rather than an equality result, so write scope cannot be
  used as read scope. No admin UI (TODO item 31).
- **Secrets lifecycle:** connection secrets resolve from either the
  environment or, optionally, HashiCorp Vault (token auth only — no
  AppRole/Kubernetes auth yet). `VAULT_TOKEN` itself is still a static,
  env-configured credential with no built-in rotation of its own; rotating
  it is an operator responsibility, same as any other credential. Other
  secret-manager backends (AWS/GCP Secrets Manager) remain unimplemented,
  though the `SecretResolver` interface is designed to add them without a
  breaking change.
- **Static-key identity:** every key in one configured API-key list shares one
  subject and scopes. Use JWT for per-human/per-agent identity and expiry.
- **Concurrency fail-open:** Redis-backed concurrency can intentionally fail
  open. This improves availability but temporarily removes the distributed cap.
- **Capacity waiting still has no cancellation or async contract (TODO item
  35 phase 3):** a caller may request `queue_mode=wait` up to the operator's
  own `concurrency_wait_seconds` ceiling (never longer — see QG-08), and
  `max_queue_depth`/`max_queue_depth_per_principal` now bound how many
  callers may wait at once (cross-replica when `concurrency_backend: redis`
  is selected — see QG-08), but there is still no MCP progress notification,
  REST `202`-plus-cancel contract, or way for a caller to actually cancel a
  query it's already waiting on/running. The in-process (non-Redis)
  `querygate_queue_depth` gauge remains single-process visibility only, like
  `querygate_concurrency_in_use`.
- **Audit durability:** JSONL is not WORM storage, has no built-in retention or
  search, and sink failures do not fail an already-executed database query.
- **Semantic memory is only phase 32A-1:** catalog provenance, row-free schema
  fingerprints/diffs, and deterministic policy-first retrieval are present,
  but there is no automatic refresh/invalidation job, provider execution,
  generated-draft flow, benchmark report, approval workflow, embedding index,
  or usage-learning loop yet. Schema freshness is explicitly `untracked`
  unless a version-2 snapshot is persisted, and a fingerprint mismatch is
  returned as `stale`; neither state changes access or blocks ordinary schema/
  query operations. Database comments are represented only by SHA-256 hashes
  in snapshots, limiting prompt-injection persistence in this phase.
- **Operator compromise:** a host/config administrator can change policy,
  secrets, logs, or the running process; QueryGate does not defend against a
  fully compromised control plane.
- **Supply chain and independent review:** signed artifacts/SBOM are TODO item
  30. No independent penetration test or formal certification has been
  performed.

## 9. Review triggers

Review and version this threat model whenever QueryGate adds a write path, a
new database dialect, stored procedures, a new authentication mechanism,
browser-facing UI, external secret/audit backend, query-cost engine, a new
admin/config mutation surface, or a change to the schema-catalog data model
(e.g. model-generated catalog content, item 32), product-guide retrieval, or
deployment-diagnostic context assembly. A release should also rerun
`make test-security` and the full default suite.
