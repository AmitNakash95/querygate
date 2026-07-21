# QueryGate threat model

**Status:** first-party security model for QueryGate 0.1.x
**Last reviewed:** 2026-07-20
**Scope:** the REST and MCP request paths, authentication, policy/schema
validation, query compilation and execution, Redis concurrency coordination,
configuration reload, the config-governance version store and semantic
access diff, secret
resolution, versioned semantic-catalog provenance/schema fingerprints,
manual-only draft quarantine, automatic schema refresh, policy-first catalog
retrieval, the catalog-governance review/publish/rollback/export/import/
deletion workflow, and audit/log outputs in this repository.

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
admin/service.py — validate/preview/simulate/diff/blast-radius/stage/apply/rollback
        |
        +-----> admin/store.py (versioned connections/policy/catalog snapshots)
        +-----> config_reload.reload_config() (same swap as /admin/reload-config)
        +-----> audit trail (config.governance events)

Authenticated agent catalog search
        |
        +-----> resolve principal policy (connection/table/column)
        +-----> filter catalog entries + relationship endpoints
        +-----> tokenize/rank/count/budget the already-authorized view only

Catalog-governance reviewer (catalog:review/edit/approve/reject/publish/rollback)
        |
        v
catalog/governance.py — edit/approve/reject/publish/rollback state machine
        |
        +-----> catalog/repository.py (same CatalogFileRepository lock 32A uses)
        +-----> audit trail (catalog.governance events)

Operator semantic-memory pipeline (disabled by default)
        |
        +-----> bounded row-free schema reflection -> hashed snapshot/diff
        +-----> atomic catalog refresh -> selective stale marking
        +-----> strict offline manual batch -> quarantined inferred drafts
        +-----> no model network adapter and no automatic publication
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
  quarantined draft proposals/generation records, and the confidentiality of
  policy-hidden entries during retrieval.
- Catalog-governance review state and version history — every proposal's
  review status/transitions and every publish/rollback's actor attribution
  and durable record.
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
| QG-10 | Unauthorized configuration changes | `/admin/reload-config` requires `admin:reload-config`; the config-governance API (`/admin/config/*`) separately requires `admin:config:write` for validate/preview/stage/apply/rollback and `admin:config:read` for history/inspection; every path fully validates new content before atomic registry/policy replacement. The governed documents are connections, policy, catalog, and — since item 48 phase 2 — query templates (`templates.yaml`): a curated query template is authored only through this staged/validated/re-validated-on-apply/rollbackable path, never a direct mutation endpoint, so it can never publish itself | REST reload scope tests, config-reload tests, `test_config_governance_write_endpoints_require_write_scope`, `test_config_governance_read_endpoints_require_read_scope`, `test_query_template_authored_through_governance_becomes_invocable_then_rolls_back`, `test_staged_template_is_not_live_until_a_separate_apply` |
| QG-11 | Browser-driven DNS rebinding against local MCP | MCP validates `Host` and, when present, `Origin`; protection is enabled by default with loopback hosts allowlisted | Security test `test_mcp_rejects_unapproved_host_header` |
| QG-12 | Audit data becomes a new exfiltration channel | Narrow versioned event schema, explicit normalization without values, mode `0600`, append-only application writes, optional `fsync` | `test_audit.py` |
| QG-14 | Secret-backend failure or misconfiguration discloses a Vault token or backend response text | `SecretResolver.resolve` errors carry only the reference being looked up and the exception type, never the configured token or the backend's own error/response text; a resolved secret value is never returned by any REST/MCP response, matching the existing credential-redaction guarantee | `test_vault_resolver_error_never_leaks_token_or_backend_response_text`, `test_vault_resolved_secret_never_appears_in_connection_listing_or_errors` |
| QG-15 | A config-governance version that stops validating (e.g. an env var/Vault path it depends on disappears between staging and applying) gets silently activated anyway | `apply` re-validates a version's content immediately before activating it, regardless of whether it validated when staged; a failed re-validation leaves the active version and pointer untouched and is recorded as a rejected audit event | `test_apply_rejects_a_version_that_no_longer_validates`, `test_invalid_staged_version_is_rejected_not_silently_applied` |
| QG-16 | Product-guide search, diagnostics, or caching disclose another principal's connections, policies, secret references, or hidden schema identifiers | Static search indexes only packaged public topics; live access context is authorized and assembled separately for each request; no live context cache exists; admin inspection requires `admin:config:read` and reconstructs an allowlisted redacted projection rather than filtering raw YAML afterward | `test_product_guide_security.py`, `test_redacted_configuration_excludes_secrets_policy_names_and_other_principals`, REST/MCP guide integration tests |
| QG-17 | Semantic catalog poisoning, provider output, stale guidance, or search/ranking/count/relationship traversal discloses a policy-hidden table/column or changes enforcement | Every published entry has stable provenance, explicit status/confidence/schema freshness, and server-derived precedence; rejected/archived content is excluded and stale content is labeled; verified entries cannot be overwritten by lower-precedence proposals and merging cannot change sensitivity. Provider mode defaults to disabled and only strict offline manual imports exist: their schema-bound output is stored in a separate inferred-draft queue whose model has no policy/sensitivity/sampling fields and which retrieval never indexes. Row-free refresh atomically stales only affected entries/proposals, rebinds unaffected entries, and logs only failure types. Principal policy filters published candidates and both relationship columns before tokenization, ranking, counting, or byte budgeting, removing free-form fields that echo exact hidden identifiers; public citations hash evidence references and omit actor/model identities; schema snapshots contain structure and comment hashes, never rows or raw comments | `test_catalog_retrieval.py`, `test_schema_memory.py`, `test_catalog_generation.py`, `test_catalog_refresh.py`, `test_semantic_memory_benchmark.py`, `test_manual_provider_output_cannot_publish_itself_or_change_verified_content`, `test_schema_refresh_failure_log_never_copies_raw_driver_error`, REST/MCP catalog-search integration tests |
| QG-18 | A caller with partial catalog-governance privilege publishes without authorized approval, silently overwrites already-verified content, a rollback discards a change made after the one it targets, an import corrupts an unrelated connection's history, or a deletion leaves a dangling reference an unauthorized caller could exploit or that would crash schema validation (TODO items 32B-1/32B-2) | Nine independent least-privilege scopes (`catalog:generate/review/edit/approve/reject/publish/rollback/export/delete`) gate every mutation; none is implied by another. A proposal's `review_status` starts `pending` and can only reach `published` through a separate, actor-attributed `approved` transition — a draft can never publish itself, and repeated/out-of-order/invalid transitions fail with no partial mutation. Publishing an approved proposal onto a table/column/relationship that already carries different, human-verified content for the same field is always rejected as a reviewable conflict, never silently overwritten; a draft's content model has no sensitivity/sampling/policy/mandatory-filter field, so publication cannot touch any of those regardless of what is approved. Rollback is idempotent, refuses if the entry has changed since the targeted publish, and refuses a table-creation rollback while later publishes still depend on it. Import (destructive, connection-scoped) re-numbers/de-duplicates file-global version and generation ids against the target catalog's current content so it can never collide with or corrupt an unrelated connection's history, and only ever replaces the connection named in the request. Deletion only ever removes terminal-state records (rejected, or published-and-rolled-back) and cascades a publish record's paired rollback record together so no dangling `rolled_back_version_id`/`published_version_id` reference can ever be persisted; a proposal whose publish is still live can never be deleted. Every generation and state transition is a redaction-safe `catalog.governance` audit event (ids/actor/outcome only, never draft text or raw YAML) | `test_catalog_governance.py`, `test_catalog_governance_rest.py`, `test_catalog_governance_write_scopes_are_independent`, `test_catalog_governance_review_endpoints_require_review_scope`, `test_catalog_governance_endpoints_reject_unauthenticated_callers`, `test_a_draft_proposal_cannot_publish_itself`, `test_catalog_export_and_delete_scopes_are_independent`, `test_catalog_delete_scope_alone_cannot_export_or_import`, `test_catalog_export_scope_alone_cannot_delete`, `test_import_remaps_version_ids_and_cross_references_to_avoid_collision`, `test_delete_published_proposal_requires_rollback_first` |
| QG-19 | Draft-aware policy simulation (`POST /admin/config/simulate`, TODO item 39) becomes a secret-existence oracle, discloses static row-filter values/claims/query predicates/resolved secrets, leaks hidden policy metadata for a table the target principal can't see, persists a candidate as a real version, or lets concurrent simulations interfere with live traffic or each other | `simulate` requires both `admin:config:read` and `admin:config:write` together (neither alone is sufficient), unlike every other `/admin/config/*` action which needs only one; candidate documents are loaded through the same loaders/cross-validation as `/validate` into a temporary, request-scoped registry/policy/catalog context that is never installed as a process global and is discarded when the request completes — the live registry, policy store, and config-version store are provably untouched, so concurrent production requests and other simulations can't observe or corrupt each other. The response schema (`CandidatePolicySimulation`) structurally excludes resolved secret values, static mandatory-filter values, supplied claim values, query predicate values, and compiled SQL; a table the candidate policy hides from the target principal never contributes its mandatory-filter identifiers to the response, even when the caller explicitly names that table. An invalid candidate fails closed with a generic message pointing at `/validate` for detail, rather than echoing the offending content, and is still recorded as a redaction-safe rejected audit event | `test_candidate_simulation_uses_isolated_context_and_redacts_values`, `test_candidate_simulation_does_not_reveal_filters_for_denied_table`, `test_candidate_simulation_masks_invalid_candidate_content`, `test_candidate_simulation_uses_draft_without_persisting_or_changing_live_policy`, `test_config_governance_write_endpoints_require_write_scope`, `test_config_governance_read_endpoints_require_read_scope` |
| QG-20 | Semantic access diff (`POST /admin/config/diff`, TODO item 40 phase 1) becomes a secret-existence oracle, discloses static mandatory-filter values/resolved secrets/query predicate values/raw YAML, persists the candidate as a real version, or lets a concurrent diff interfere with live traffic | `diff` requires both `admin:config:read` and `admin:config:write` together (neither alone suffices), for the same read-detail-plus-write-resolution reasoning as `simulate`. Both the active and candidate snapshots load through the same isolated, request-scoped registry/policy/catalog context `simulate` uses (`_load_isolated_candidate_context`), never installed as a process global and discarded when the request completes — the live registry, policy store, and config-version store are provably untouched and nothing is persisted. The response schema (`SemanticAccessChange`) structurally carries only non-sensitive resolved values (guardrail numbers, `visible`/`hidden`/`absent`, join-group names, mandatory-filter source kind/claim name); static filter values, resolved secrets, connection strings, predicate values, and raw YAML are never placed in it. An invalid candidate fails closed with the same generic message pointing at `/validate` and is recorded as a redaction-safe rejected `diff` audit event. Per-principal override changes are flagged as `analysis_incomplete` rather than resolved (phase 2) | `test_config_semantic_diff.py`, `test_diff_reports_guardrail_change_without_mutating_live_state`, `test_diff_masks_invalid_candidate_and_audits_rejection`, `test_mandatory_filter_added_is_tightening_removed_is_loosening_without_values`, `test_diff_endpoint_reports_semantic_changes_without_persisting`, `test_config_governance_write_endpoints_require_write_scope`, `test_config_governance_read_endpoints_require_read_scope` |
| QG-21 | Admin connection-status API (`GET /admin/connections`, TODO item 43 phase 1) discloses database topology or a connection string, echoes a raw driver error that embeds a host/port/database/username, or is readable without the intended privilege | The endpoint is gated by a dedicated `admin:connections:read` scope, independent of the config scopes, so operational-health visibility is not implied by any other admin privilege and an anonymous caller is rejected. The response model (`ConnectionStatus`) carries only credential-free fields already exposed by `PublicConnectionInfo` (`connection_id`/`dialect`/`enabled`) plus derived health (`status`/`last_checked`/`last_success`/`latency_ms`/`schema_reflected`); no connection string is present. A failure is reported only as a stable `failure_category` (`authentication`/`unreachable`/`timeout`/`error`) derived from the exception *type*, never its message, so a driver error embedding a host/username/password cannot leak — the raw error remains in stdout logs only, matching `/health`'s existing non-disclosure of topology to its unauthenticated probe | `test_admin_connections_status_requires_its_own_scope`, `test_response_is_sorted_and_never_leaks_raw_error_or_credentials`, `test_classify_failure_never_uses_the_message`, `test_lists_credential_free_per_connection_status` |
| QG-23 | Admin "test now" connection probe (`POST /admin/connections/{id}/test`, TODO item 43 phase 2a) is triggered without the intended privilege, echoes a raw driver error/connection string like QG-21, or is abused as a repeated-connection-attempt resource-exhaustion vector against the target database | Gated by its own `admin:connections:test` scope — independent of `admin:connections:read`, so passive status visibility does not imply the ability to trigger a live probe, and vice versa. The probe reuses `HealthMonitor`'s exact ping/classification path and returns the same credential-free `ConnectionStatus` model as QG-21, so it inherits the identical non-disclosure guarantee. Each connection can be manually probed at most once per `AppConfig.admin_connection_test_cooldown_seconds` (default 10s); a request inside that window is rejected with `429`/`Retry-After` before a second real connection attempt is made, rather than being queued or silently throttled. Every probe attempt — successful or rate-limited — is recorded as a redaction-safe `connection.probe` audit event (connection id, actor, probe result/failure category, never a raw driver error or connection string) | `test_admin_connections_test_now_requires_its_own_scope`, `test_test_now_success_updates_status_and_never_leaks_raw_error`, `test_test_now_is_rate_limited_per_connection`, `test_test_now_persists_audit_event_without_raw_error` |
| QG-22 | Policy-change blast-radius analysis (`POST /admin/config/blast-radius`, TODO item 41) becomes a secret-existence oracle, discloses static filter values/resolved secrets/raw YAML, persists the candidate as a real version, performs unbounded work by fanning a diff out across every conceivable principal, or silently under-reports impact when a bound is reached | `blast-radius` requires both `admin:config:read` and `admin:config:write` together, identical to `diff`'s reasoning — it echoes resolved policy detail while resolving caller-supplied config/secret references. It is built entirely from `diff`'s own primitives: the same isolated, request-scoped registry/policy/catalog context (never installed as a process global, discarded when the request completes) and the same `compute_access_diff` classification logic, called once at the connection baseline and once per principal that has an explicit `principals:` override in either snapshot — a principal with no override is provably identical to the baseline by construction, so it is never separately fanned out. Work is bounded on two axes, each with its own explicit cap: at most 100 configured principals are individually evaluated, and each principal's own change list is capped like `diff`'s; either cap being reached sets `analysis_incomplete` with a specific, human-readable reason rather than silently omitting impact. `highest_risk` ranks only access-*expanding* (loosening) changes — mandatory-filter removal ranked above newly visible connections/tables/columns, ranked above a loosened guardrail — and is itself capped at 25 entries with the same incomplete-reason behavior if truncated. The response schema structurally excludes static filter values, resolved secrets, connection strings, and raw YAML, matching `SemanticAccessDiff`'s existing redaction guarantee at every nesting level (baseline, each principal's own changes, and `highest_risk`). An invalid candidate fails closed with the same generic message pointing at `/validate` and is recorded as a redaction-safe rejected `blast_radius` audit event | `test_blast_radius.py`, `test_blast_radius_reports_baseline_and_configured_principal_impact`, `test_blast_radius_masks_invalid_candidate_and_audits_rejection`, `test_blast_radius_endpoint_finds_targeted_expansion_under_an_overall_tightening`, `test_blast_radius_never_leaks_static_filter_values_or_yaml`, `test_principal_cap_marks_analysis_incomplete_and_bounds_work`, `test_highest_risk_cap_marks_analysis_incomplete`, `test_config_governance_write_endpoints_require_write_scope`, `test_config_governance_read_endpoints_require_read_scope` |
| QG-24 | Non-admin "my access" portal (`/access/`, `GET /help/my-access`'s new `connection_access` field, TODO item 45) discloses another principal's effective guardrails or mandatory-filter claim readiness, leaks a mandatory-filter value/claim value, discloses filter metadata for a table the caller's own policy hides, or the static shell smuggles in admin-only navigation/actions/endpoints | `connection_access` is built from the caller's own already-authenticated `Principal` only — there is no target-principal parameter, unlike `/admin/config/simulate`, so a caller can never request another subject's guardrails. Guardrails and mandatory-filter readiness reuse item 39's exact typed, redaction-safe models (`EffectiveGuardrails`/`MandatoryFilterReadiness`): guardrail numbers and table/column/claim-*name*/source/ready booleans only — never a static filter value or a supplied claim's resolved value. A mandatory filter on a table the caller's own resolved policy denies is excluded before it ever reaches the response, matching QG-19's "don't leak hidden-table filter metadata" reasoning. The `/access/` static shell is served with the same CSP/no-referrer/nosniff/no-store headers as `/admin/`, is public (only the API calls it makes require the caller's own bearer token, identical to `/admin/`'s own posture), and is asserted to never contain admin-only navigation labels, `admin:config:write`, or `/admin/config/`\|`/admin/catalog/` endpoint references | `test_access_summary_reports_effective_guardrails_and_claim_readiness`, `test_access_summary_excludes_mandatory_filter_for_a_table_the_principal_cannot_see`, `test_my_access_reports_per_principal_guardrails_and_claim_readiness`, `test_access_ui_is_served_with_browser_security_headers_and_no_admin_controls`, `test_access_ui_shell_is_served_without_authentication` |
| QG-25 | Policy templates (`GET /admin/config/templates`, `POST /admin/config/templates/render`, TODO item 46) are readable/renderable without the intended privilege, a rendered template embeds a credential/tenant value, or applying a template silently loosens or drops a restriction already present in the caller's draft | `render` requires `admin:config:write` (like `/validate`/`/preview`, since it resolves caller-supplied content meant to be staged); `GET /templates` requires `admin:config:read`, and both reject an unauthenticated caller. Templates are a fixed, code-reviewed Python registry (`admin/templates.py`) — parameters are typed and validated, never inferred from live schema/table names, and no template patch ever contains a connection string, secret reference, or literal tenant value; `render_template` never touches the live registry/policy singletons, the config-version store, or any secret, operating only on the caller-supplied `policy_yaml` text. The merge is monotonically restrictive by construction: numeric guardrail caps take `min(existing, template)`, `allowed_tables` only narrows an existing non-empty allow-list, and `denied_columns`/`mandatory_row_filters` only union onto what is already present — never replacing or removing an existing restriction — with every case where an existing value was kept over the template's own called out by name in the returned `rules` preview rather than silently absorbed. A rendered document is plain `policy_yaml` text with no special privilege: it still must pass the unchanged `/validate` and re-validation-on-apply gates (QG-15) before it can ever be activated | `test_policy_templates.py` (`test_render_missing_required_parameter_is_rejected`, `test_template_never_loosens_an_existing_stricter_cap`, `test_template_never_widens_an_existing_allow_list`, `test_tenant_isolated_reapplication_is_idempotent`, `test_template_never_embeds_connection_strings_or_secrets`), `test_list_templates_requires_read_scope`, `test_render_template_requires_write_scope`, `test_render_template_end_to_end_through_validate_and_stage` |
| QG-26 | A curated query template (`run_query_template` / `POST /query-templates/{id}/run`, TODO item 48) is invoked to exceed the caller's policy, a parameter smuggles a raw identifier/SQL, an invoker enumerates hidden templates/connections, or the audit trail records a parameter value | A template is a stored `StructuredQuery` skeleton, not a SQL string — there is no raw-SQL field anywhere. At invocation the caller's parameters are type/constraint-checked against typed slots and substituted only into value positions, and the *bound result is validated as a real `StructuredQuery` and run through the unchanged `StructuredQueryService`*, so it is subject to the identical policy caps, table/column allow-deny, mandatory row filters, schema checks, and guardrails as an ad-hoc query — a parameter gets no exemption, and a bound query that references a denied column is rejected exactly as an ad-hoc one is. A template is visible/invocable only if its target connection is visible to the principal (item 22); an unknown template and one on a hidden connection return the same non-enumerating not-found, so it can't be used to probe hidden template ids or connection names. Invocations audit distinctly (`operation="run_query_template"`, the template id, and the parameter *names*) but never parameter *values*, which are bound as data and stripped from the query shape like any literal. Templates are declarative, file-configured, hot-reloadable config (as safe as `policy.yaml`, and a template can't exceed policy); as of item 48 phase 2 that config is *governed* — authored only through the staged/validated/rollbackable config-versioning plane (`templates.yaml` is a governed document, see QG-10), never a direct mutation endpoint | `test_query_templates.py`, `test_query_template_api.py` (`test_unknown_and_hidden_templates_are_uniform_404`, `test_audit_records_template_id_and_param_names_never_values`), `test_query_template_cannot_exceed_policy` |
| QG-27 | The on-demand template live-schema check (`POST /admin/config/check-template-schema`, TODO item 83) is triggered without the intended privilege, echoes a raw driver error/connection string, discloses schema of a connection beyond the caller's privilege, or is abused as a repeated-reflection resource-exhaustion vector | Gated by both `admin:config:read` and `admin:config:write` together (like `/simulate` and `/diff`): it reveals live column/table existence (read-like) while resolving caller-supplied template content (write-like), so neither scope alone suffices and an anonymous caller is rejected. It reflects the *currently-live* connections through the same cached `validate_schema` the real query pipeline uses — read-only, never executing the query or returning rows — against a dummy-bound skeleton (placeholder values never change which identifiers are referenced). The per-template result (`TemplateSchemaCheck`) carries only the template id, connection id, a coarse `status`, and the schema-validation message (a missing column/table *name*, which an admin with config access can already see via the schema browser) — never row values, connection strings, or a raw driver error: any database/reflection failure is caught and reported as the generic `unreachable` status, so a connection error embedding host/credentials can't leak. Best-effort by design (an unreachable database is a result, not a `500`), and every invocation is a redaction-safe `check_template_schema` audit event (actor/outcome only) | `test_template_schema_check.py` (`test_unreachable_database_is_best_effort_not_a_failure`, `test_unknown_connection_is_connection_unavailable`, missing-column/table classification), `test_check_template_schema_endpoint_flags_missing_column`, `test_config_governance_write_endpoints_require_write_scope`, `test_config_governance_read_endpoints_require_read_scope` |

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
- Grant `admin:connections:read` to on-call/monitoring identities that need to
  see which configured connection is failing (`GET /admin/connections`). It is
  read-only operational health — credential-free and never a connection string
  or raw driver error — and independent of the config scopes, so it can be
  granted to a role that should observe database reachability without being
  able to read or change configuration.
- Grant `admin:connections:test` more narrowly, only to operators who should be
  able to trigger a real, immediate connection attempt against a target
  database on demand (`POST /admin/connections/{id}/test`) — it is a distinct
  privilege from `admin:connections:read`, not implied by it.
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
  approver/four-eyes requirement or scheduled apply. `POST /admin/config/diff`
  (QG-20, TODO item 40 phase 1) adds a resolved-access semantic diff, but only
  at the connection baseline — per-principal resolution and blast-radius
  analysis are later phases. The preview is deliberately content-free; a
  write-only caller sees only submitted/inherited rather than an equality
  result, so write scope cannot be used as read scope. No admin UI (TODO item
  31).
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
- **Semantic memory phases 32A and 32B are complete; 32C is not:**
  provenance, row-free automatic refresh/diffs, selective staleness,
  deterministic policy-first retrieval, disabled/manual-only quarantined
  drafts, the fixed benchmark, a governed review/edit/approve/reject/
  publish/rollback workflow with audit events and durable version history,
  and connection-scoped export/import (backup/restore) and
  retention/deletion are present. There is still no hosted/local model
  adapter, embedding index, or usage-learning loop (32C). An unpublished draft remains a privileged,
  non-agent-visible record regardless of review status; only an explicitly
  published entry becomes agent-visible, and it is then filtered by policy
  like every other catalog entry. Schema freshness is `untracked` until a
  version-2 snapshot is persisted; stale state never changes access or
  blocks ordinary schema/query operations. Database comments remain only
  SHA-256 hashes in snapshots. Catalog version history is bounded to 2000
  entries and terminal-state records (rejected proposals; published-and-
  rolled-back proposals with their paired publish/rollback records) can be
  explicitly deleted (`catalog:delete`), but there is no automated
  retention schedule — pruning is an authorized, manual operator action.
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
