# QueryGate customer technical guide

This guide is for security, data-platform, infrastructure, and AI engineering
teams evaluating or operating QueryGate. It complements the main
[repository README](README.md): the main README provides the project overview
and local quickstart, while this document focuses on the customer-visible
product boundary, technical controls, integration model, and production
responsibilities.

## What QueryGate is

QueryGate is a self-hosted access gateway that lets AI agents and applications
read from PostgreSQL and Microsoft SQL Server through MCP or REST without giving
them a raw-SQL tool.

Callers submit a constrained JSON query object called a `StructuredQuery`.
QueryGate checks that object against the authenticated caller's policy and the
database's reflected schema, compiles it into parameterized SQL, executes it
within configured resource limits, and returns a bounded result. Every
deployment starts read-only; an operator can separately opt in to a governed
write path (typed INSERT/UPDATE/DELETE — never a raw-DML string) for specific
tables, described below.

QueryGate is not a general database proxy and is not a replacement for database
permissions. It is an additional policy and execution boundary designed for
agent-driven data access, where the caller may be mistaken, compromised, or
prompt-injected.

### At a glance

| Area | Product behavior |
|---|---|
| Deployment | Runs inside infrastructure controlled by the customer |
| Supported databases | PostgreSQL and Microsoft SQL Server |
| Client interfaces | MCP over Streamable HTTP and versioned REST endpoints |
| Query input | Validated `StructuredQuery` JSON; no raw-SQL input |
| Data operations | Read-only by default; opt-in, deny-by-default governed writes (typed INSERT/UPDATE/DELETE — previewed with a diff and approval-gated) when a table/operation is explicitly enabled. No DDL or stored-procedure passthrough in either mode. |
| Identity | Static bearer keys or JWKS-verified JWTs |
| Authorization | Default, connection, and principal-specific policy layers |
| Tenant isolation | Mandatory row filters, including values derived from authenticated JWT claims |
| Secrets | Environment references or HashiCorp Vault KV v2 references |
| Load protection | Query-shape limits, row and byte caps, timeouts, and per-connection concurrency limits |
| Operations | Health endpoint, Prometheus metrics, structured logs, and optional persisted JSONL audit events |

## Where it sits

```text
AI agent or application
        |
        | HTTPS + bearer identity
        | StructuredQuery JSON
        v
Reverse proxy / ingress
        |
        v
+------------------------------------------------------+
| QueryGate                                            |
|                                                      |
| authenticate -> authorize -> validate live schema    |
|              -> compile SELECT -> enforce limits     |
+------------------------------------------------------+
        |                         |
        | parameterized read     +--> metrics, logs, audit events
        v
Customer-managed PostgreSQL or Microsoft SQL Server
```

QueryGate does not call an LLM or require a model provider. It returns permitted
rows to the authenticated REST or MCP client that made the request. If that
client sends results to an external model, that separate client and network
path remain part of the customer's data-governance boundary.

Depending on configuration, QueryGate may also communicate with a JWKS
endpoint for token verification, HashiCorp Vault for database secrets, and
Redis for distributed concurrency coordination.

## How a request is handled

Every REST and MCP query follows the same enforcement path:

1. **Authenticate the caller.** A bearer credential resolves to a principal,
   scopes, and—when JWT is used—verified claims.
2. **Resolve visibility and policy.** Hidden connections behave like unknown
   connections. The caller receives only the connections, tables, columns, and
   catalog metadata allowed by the resolved policy.
3. **Validate the query shape.** Unknown fields and unsupported operations are
   rejected. There is no `sql` field or alternate raw-query endpoint.
4. **Validate every identifier.** Table and column references used in selects,
   joins, filters, grouping, ranking, and sorting are checked against policy and
   the live reflected schema.
5. **Apply mandatory restrictions.** Policy-defined row filters are added by
   the service and cannot be omitted or replaced by the caller.
6. **Compile a read-only statement.** QueryGate builds a SQLAlchemy `SELECT`;
   predicate values are passed as bound parameters rather than executable SQL.
7. **Protect the database.** The query runs under a timeout and a
   per-connection concurrency limit. Multi-instance deployments can coordinate
   that limit through Redis.
8. **Bound the response.** Server-side row and serialized-response byte limits
   are applied. The response reports whether it was truncated.
9. **Record the outcome.** The response includes an `X-Request-ID`, and the
   query attempt contributes structured operational and audit telemetry.

REST and MCP are transport layers over this same behavior; choosing one does
not change query authorization or execution semantics.

## The structured query model

A caller can express useful analytical reads without constructing SQL. For
example, this query returns the five customers with the highest completed-order
spend:

```json
{
  "from": "orders",
  "select": [
    "orders.customer_id",
    {"fn": "sum", "col": "orders.total_amount", "as": "total_spent"}
  ],
  "where": {
    "col": "orders.status",
    "op": "eq",
    "value": "completed"
  },
  "group_by": ["orders.customer_id"],
  "order_by": [{"col": "total_spent", "dir": "desc"}],
  "limit": 5,
  "intent": "highest-spending customers for completed orders"
}
```

The supported model includes:

- column selection and aliases;
- `count`, `sum`, `avg`, `min`, and `max` aggregates;
- nested `and` / `or` filters with a fixed operator set;
- inner, left, full-outer and (policy-gated) cross joins — on an equality pair or
  a general condition, so range and temporal joins are expressible;
- grouping, aggregate filters, sorting, limits, and offsets;
- day, week, month, quarter, and year date buckets;
- overall or per-partition top-N ranking; and
- bounded batches of independent structured queries.

The `intent` field is optional context for operational diagnostics. It does not
grant access, change query semantics, or bypass validation.

Optionally, an administrator can publish **curated query templates** — named,
parameterized structured queries — that agents invoke by name (`GET/POST
/api/v1/query-templates`, or the `list_query_templates`/`run_query_template`
MCP tools, and a read-only panel in the admin console). This narrows the
effective surface to a finite, reviewed set of query shapes. A template is a
stored structured query, not raw SQL; its parameters are typed and validated,
and the bound query passes through the same policy, schema, and guardrail checks
as any other — a template can never exceed policy.

The explain operation runs validation and compilation without executing the
query. By default, its SQL representation uses placeholders and redacts
parameter values. Literal rendering should be enabled only when the resulting
operator logs and explain responses are acceptable for the deployment's data
classification.

For complete payload examples, see the [REST examples](examples/rest_calls.md)
and [MCP examples](examples/mcp_calls.md).

## Schema discovery and business context

QueryGate reflects schema metadata from the live database so a caller cannot
invent a table or column and have it trusted as an identifier. The discovery
surface includes:

- visible connections, without credentials or connection strings;
- visible tables for a selected connection; and
- visible columns, types, and nullability for a selected table.

An optional curated catalog can add business descriptions, aliases,
relationship hints, sensitivity labels, and default aggregation guidance.
Catalog metadata is descriptive only: it does not change query validation or
authorization. It is filtered through the same caller policy, so it cannot
reveal a denied table, column, or relationship target.

This separation is intentional:

- the live schema is the source of truth for what technically exists;
- policy decides what the caller may discover and query; and
- the catalog helps an agent understand permitted data in business terms.

## Policy and tenant isolation

Policy is resolved in three layers: a deployment default, an optional
per-connection override, and an optional per-principal override. A
connection-specific principal rule takes precedence over that principal's
wildcard rule.

A policy can control:

- whether a connection is visible to a caller;
- allowed and denied tables and columns, with deny rules taking precedence;
- join count, selected-column count, filter depth, grouping width, and batch
  size;
- different row caps for ordinary and aggregate queries;
- maximum serialized response size;
- top-N and partition width;
- execution timeout, concurrency, and queue wait time;
- same-engine cross-connection join groups; and
- mandatory row filters.

A production-oriented policy can start with no visible connections and enable
only specific principal/connection pairs:

```yaml
default:
  enabled: false
  max_joins: 2
  max_select_columns: 12
  max_limit: 100
  max_limit_aggregate: 500
  timeout_seconds: 20
  max_concurrency: 6

connections:
  analytics:
    allowed_tables: [customers, orders, order_items]
    denied_columns:
      customers: [email, phone]

principals:
  reporting-agent:
    analytics:
      enabled: true
      mandatory_row_filters:
        - table: orders
          column: tenant_id
          from_claim: tenant_id
```

In this example, `reporting-agent` can discover `analytics`, but every query
that touches `orders` is scoped to the verified `tenant_id` claim. If that
claim is absent, the query is rejected. The agent does not supply or control
the enforced tenant value.

A denied column cannot be selected, filtered, joined, grouped, ranked, or
sorted. This closes common indirect paths around a projection-only deny rule.
As with any analytical system, however, a caller may still infer information
from aggregates it is legitimately authorized to request. QueryGate does not
provide differential privacy or query-history-based inference controls.

## Authentication and authorization

REST and MCP accept bearer authentication. Deployments can use:

- static keys, suitable for service identities and controlled pilots; or
- JWTs verified through a configured JWKS endpoint, including signature,
  algorithm, issuer, audience, expiry, subject, and scope validation.

JWT is the better fit when callers need distinct identities, expiry, or
claim-derived row isolation. Static keys configured for one surface share the
configured subject and scopes for that surface; they are not a substitute for
per-user identity.

Administrative actions use explicit scopes:

- `admin:reload-config` reloads customer-managed configuration files;
- `admin:config:read` inspects versioned configuration history;
- `admin:config:write` validates, stages, applies, and rolls back versioned
  configuration; and
- `admin:connections:read` returns credential-free per-connection operational
  health (which connection is reachable, when it last succeeded, and a redacted
  failure category — never a connection string or raw driver error); and
- `admin:connections:test` (independent of the read scope above) triggers an
  immediate, rate-limited re-check of one connection on demand; and
- `admin:observability:read` returns an aggregated operational overview
  (query volume, rejection categories, queue/concurrency pressure, and the
  cost-estimation fail-open rate, globally and per connection) as an honest
  current-process snapshot — never a query, value, or principal — rendered as
  the control plane's "Observability" panel.

These privileges should be separated from ordinary query identities. In
production, authentication is mandatory. Anonymous access is limited to a
local/development configuration with no real authenticator configured.

## Connections and secret handling

Connection profiles are configuration, not client-visible API objects. Public
connection responses contain a connection identifier, dialect, status, and an
optional description; they do not contain connection strings.

Connection strings should be referenced rather than written directly into the
configuration file:

```yaml
connections:
  - id: analytics
    dialect: postgresql
    connection_string: ${QUERYGATE_ANALYTICS_DB_URL}
    description: "Customer analytics database"
    enabled: true
```

For deployments using HashiCorp Vault KV v2, the reference can instead identify
a secret path and field:

```yaml
connection_string: ${vault:querygate/analytics#connection_string}
```

Secret references are resolved when configuration is loaded or reloaded.
Environment-backed secret rotation requires the running process to receive the
new environment value, which normally means a restart or redeployment.
Vault-backed database secrets can be picked up on the next reload without a
process restart. Access to the Vault token and permitted secret paths remains
an operator responsibility.

## REST and MCP integration

The principal REST operations are:

| Operation | Endpoint |
|---|---|
| List visible connections | `GET /api/v1/connections` |
| List visible tables | `GET /api/v1/{connection}/tables` |
| Describe a visible table | `GET /api/v1/{connection}/tables/{table}` |
| Explain without execution | `POST /api/v1/{connection}/query/explain` |
| Execute one structured query | `POST /api/v1/{connection}/query` |
| Execute a bounded batch | `POST /api/v1/{connection}/query/batch` |

When MCP is enabled, the equivalent agent tools include connection and schema
discovery, explain, single-query execution, and batch execution. QueryGate uses
Streamable HTTP and can be connected directly to an MCP-compatible client.

QueryGate also includes an offline product guide exposed through REST and MCP.
It can search packaged guidance, return setup checklists, explain supported
configuration fields and error codes, describe the current caller's access,
and provide an authorized redacted configuration summary. Static guide search
does not query a model provider or include live deployment context.

## Deployment and availability

QueryGate is distributed as a self-hosted application/container. The repository
includes reference deployments for [Docker Compose and Kubernetes with
Helm](deploy/README.md).

A production deployment should provide:

- an existing PostgreSQL or Microsoft SQL Server database;
- a least-privileged, read-only database account for each connection;
- TLS termination at a trusted reverse proxy, ingress, or service boundary;
- API-key or JWT authentication for every exposed interface;
- protected configuration and secret storage;
- persistent or externally collected audit storage when required; and
- network restrictions around health, metrics, and administrative routes.

The in-process concurrency limiter is correct for a single QueryGate process.
When multiple workers or replicas can query the same database connection, use
Redis-backed concurrency so `max_concurrency` remains a deployment-wide cap
rather than a per-process cap. Redis behavior during an outage is configurable:
the application default is availability-oriented fail-open. Configure
`CONCURRENCY_REDIS_FAIL_OPEN=false` when protecting the database is more
important than query availability.

Cross-connection joins are intentionally narrow. Both connections must be
visible to the caller, explicitly share a join group, and be reachable through
one physical database engine—for example, two databases on the same SQL Server
instance. QueryGate is not a federated query engine for joins across unrelated
database servers.

## Observability and audit

QueryGate exposes:

- `GET /health` for aggregate readiness status without connection names or
  driver errors;
- `GET /metrics` in Prometheus format for query outcomes, rejection reasons,
  successful-query duration, and concurrency utilization;
- structured stdout logs for operational troubleshooting; and
- optional append-only JSONL audit events for queries and configuration
  governance actions.

Persisted query audit events include correlation ID, interface, principal,
authentication method, connection, normalized query shape, policy decision,
outcome, timing, row/byte counts, truncation, and a fixed error category. They
exclude returned rows, predicate values, SQL parameters, natural-language
intent, connection strings, and free-form exception text.

Operational stdout logs are more sensitive than persisted audit events: they
may contain diagnostic exception details, intent text, and SQL rendered
according to policy. Protect and retain them accordingly.

The built-in JSONL sink is rotation-friendly but is not a WORM archive, SIEM,
retention service, or search interface. A sink-write failure is reported in
operational logs and does not retroactively fail a database query that already
succeeded. Customers with audit-durability requirements should collect and
monitor the audit stream externally.

## Configuration lifecycle

Customers can manage connections, policy, and the optional catalog in either of
two ways:

1. **Infrastructure as code:** update the mounted configuration files and call
   the scoped reload endpoint.
2. **Governance API:** validate a candidate, preview which documents were
   submitted, stage a version, apply it, and reapply an older version to roll
   back.

Configuration is validated before activation. A staged version is validated
again when applied, so a version whose secret reference or other dependency is
no longer valid is rejected without replacing the active configuration.
Governance actions are attributed to the principal and included in the audit
trail without copying raw YAML into the audit event.

The built-in governance API provides version history and rollback, but it does
not currently provide a second-approver workflow, scheduled activation, or an
administrative user interface. Teams that require four-eyes approval should
enforce it in their GitOps or change-management process.

See the [operational runbook](deploy/runbook.md) for reload, rollback, secret
rotation, health, metrics, and audit procedures.

## Security assumptions and explicit boundaries

QueryGate adds controls at the application boundary, but a secure deployment
still depends on customer configuration and infrastructure:

- Database credentials should stay least-privileged regardless of the
  application-layer policy: read-only for connections with no `WritePolicy`
  enabled, and scoped to only the tables/operations governed writes actually
  need otherwise. Least privilege at the database is the final line of
  defense if QueryGate's own policy were ever misconfigured.
- TLS, firewalling, identity-provider security, host hardening, and Redis/Vault
  security remain customer responsibilities.
- Policy prevents use of denied identifiers; it does not prevent inference
  from data and aggregates the caller is allowed to access.
- Row and response caps bound egress, but a large database value may enter
  process memory before the response-size limit can omit it. Deny large object
  columns unless they are required.
- JSONL audit persistence does not provide immutability or guaranteed delivery.
- A host or configuration administrator is inside the trusted control plane and
  can change the service, policy, or secrets.
- The project provides a first-party threat model; it does not claim an
  independent penetration test or formal compliance certification.

For the full control and residual-risk analysis, read the
[QueryGate threat model](docs/THREAT_MODEL.md).

## Recommended evaluation path

1. Select one read-only reporting use case and one non-production database.
2. Create a dedicated database account with only the required grants.
3. Start with `enabled: false`, then allow only the pilot identity, connection,
   tables, and columns.
4. Add claim-derived mandatory row filters if the use case is tenant-scoped.
5. Set conservative row, byte, timeout, batch, and concurrency limits.
6. Connect through REST first to verify discovery, explain, execution, rejected
   access, and truncation behavior.
7. Connect the same policy boundary to the intended MCP client or agent.
8. Forward metrics and audit events to the customer's monitoring stack.
9. Review the threat model and deployment runbook before production approval.

The result should be a deliberately small, observable data surface that can be
expanded connection by connection as the customer's access policy matures.
