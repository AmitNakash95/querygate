# QueryGate threat model

**Status:** first-party security model for QueryGate 0.1.x
**Last reviewed:** 2026-07-18
**Scope:** the REST and MCP request paths, authentication, policy/schema
validation, query compilation and execution, Redis concurrency coordination,
configuration reload, and audit/log outputs in this repository.

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
```

The agent/client, all request fields, natural-language intent, JWTs, API keys
presented by callers, and database row values are untrusted input. QueryGate's
process, policy/configuration files, deployment secrets, reverse proxy, JWKS
configuration, Redis, and audit destination are controlled by the operator.
Database servers are trusted to enforce their own permissions but are not
trusted to return client-safe error messages.

## 3. Assets

- Database credentials and connection topology.
- Database schema, row data, aggregates, and sensitive column existence.
- Principal identity, claims, scopes, and per-principal policy decisions.
- Availability of QueryGate and the databases behind it.
- Configuration integrity, particularly connection and policy changes.
- Audit-event integrity, availability, and correlation metadata.

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
| QG-05 | Cross-principal or cross-tenant confusion | Immutable request-local `Principal`; per-principal policy resolution; claim-derived mandatory row filters; MCP caller stored in a reset `ContextVar` | Security principal-isolation and aggregate row-filter tests; policy-loader and MCP tests |
| QG-06 | Authentication spoofing or token confusion | Constant-time API-key comparison; JWT signature, algorithm, issuer, audience, expiry, required subject, and configured JWKS verification; anonymous bypass only in local/development when no real authenticator is configured | `test_auth.py`, `test_jwt_auth.py`, REST/MCP authentication integration tests |
| QG-07 | Credential, row, predicate, or backend-detail leakage | Public connection DTO has no credential field; unexpected REST/MCP/batch errors are generic; persisted audit schema excludes SQL, params, intent, exception text, and rows; explain/audit SQL is parameterized by default | `test_credential_redaction.py`, `test_audit.py`, security public-error tests |
| QG-08 | Oversized or abusive requests/results | AST depth/width/join/top-N/batch caps; server-side row limits; hard serialized-row byte ceiling, including a single oversized row; database timeout; per-connection concurrency | Policy/service tests, real timeout tests, security oversized-row test |
| QG-09 | Cross-connection access | Both connections must be visible to the principal and share the resolved `join_group`; the join uses the primary engine and declared physical database mapping | Cross-connection schema tests, including hidden-connection denial |
| QG-10 | Unauthorized configuration changes | Reload endpoint requires `admin:reload-config`; new files are fully validated before atomic registry/policy replacement | REST reload scope tests and config-reload tests |
| QG-11 | Browser-driven DNS rebinding against local MCP | MCP validates `Host` and, when present, `Origin`; protection is enabled by default with loopback hosts allowlisted | Security test `test_mcp_rejects_unapproved_host_header` |
| QG-12 | Audit data becomes a new exfiltration channel | Narrow versioned event schema, explicit normalization without values, mode `0600`, append-only application writes, optional `fsync` | `test_audit.py` |

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

The code controls above assume a correctly operated deployment:

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
- Use private, authenticated, TLS-protected Redis in multi-instance setups.
  Consider `CONCURRENCY_REDIS_FAIL_OPEN=false` when database protection is more
  important than availability during a Redis outage.
- Protect stdout logs and audit storage. Send audit events to retained or WORM
  storage when regulation requires immutability, and alert on
  `audit.sink.write_failed`.
- Restrict `/metrics`, `/health`, and admin routes at the network/proxy layer as
  appropriate for the environment.

## 8. Residual risks and explicit non-goals

- **Authorized inference:** QueryGate blocks use of denied columns, but a caller
  authorized for an aggregate can still infer facts from permitted counts and
  narrow filters. Minimum-group-size, differential-privacy, and query-history
  controls are not implemented.
- **Pre-execution cost:** limits and timeouts are primarily reactive. QueryGate
  does not yet estimate a plan and reject likely full scans or join explosions
  before execution (TODO item 26).
- **Large values in process memory:** the output byte cap prevents an oversized
  row from leaving QueryGate, but the database driver must first receive that
  row. Database-side statement limits and denial of large/blob columns remain
  important.
- **Configuration governance:** YAML reload has validation and an admin scope,
  but no approval workflow, version history, rollback ledger, or separation of
  duties (TODO item 25).
- **Secrets lifecycle:** connection secrets currently resolve from environment
  variables; external secret-manager rotation is TODO item 13.
- **Static-key identity:** every key in one configured API-key list shares one
  subject and scopes. Use JWT for per-human/per-agent identity and expiry.
- **Concurrency fail-open:** Redis-backed concurrency can intentionally fail
  open. This improves availability but temporarily removes the distributed cap.
- **Audit durability:** JSONL is not WORM storage, has no built-in retention or
  search, and sink failures do not fail an already-executed database query.
- **Operator compromise:** a host/config administrator can change policy,
  secrets, logs, or the running process; QueryGate does not defend against a
  fully compromised control plane.
- **Supply chain and independent review:** signed artifacts/SBOM are TODO item
  30. No independent penetration test or formal certification has been
  performed.

## 9. Review triggers

Review and version this threat model whenever QueryGate adds a write path, a
new database dialect, stored procedures, a new authentication mechanism,
browser-facing UI, external secret/audit backend, query-cost engine, or a new
admin/config mutation surface. A release should also rerun `make test-security`
and the full default suite.
