<!-- GENERATED FILE — do not edit by hand.
     Regenerate with: make scope-catalog  (or: poetry run querygate-scope-catalog)
     Source of truth: src/querygate/core/scopes.py -->

# QueryGate authorization scope catalog

QueryGate does not own identities — you bring your IdP (Okta, Entra, Auth0,
Keycloak, …) and QueryGate verifies its JWTs (item 10) as an OAuth resource
server (item 90). For that to work, your authorization server needs QueryGate's
scope vocabulary. This page is that vocabulary, plus recommended role bundles to
paste into your IdP. It is generated from `core/scopes.py`, so it always matches
what the running server actually enforces.

Two things stay out of the IdP by design:

- **Data-access grants** (which tables/columns a principal may read) live in
  `policy.yaml`, keyed by `sub`/claim — never as scopes. An "Analyst" therefore
  carries *no* scope and is governed entirely by data policy.
- The same vocabulary is machine-discoverable at the RFC 9728 endpoint
  `/.well-known/oauth-protected-resource/mcp` (`scopes_supported`) when the MCP
  OAuth resource server is enabled, so an IdP can import it without any typing.

## Scopes

Every scope QueryGate understands, grouped by area. Each gates exactly one class
of privileged action; unlisted actions (running a structured query) need no
scope, only a valid identity.

### Admin · Config

| Scope | Gates |
|---|---|
| `admin:reload-config` | Hot-reload connections/policy/catalog from disk |
| `admin:config:read` | Read config-governance versions and history |
| `admin:config:write` | Stage/apply/roll back config-governance versions |
| `admin:config:approve` | Approve/reject a staged config version (four-eyes; not the author) |

### Admin · Connections

| Scope | Gates |
|---|---|
| `admin:connections:read` | Read per-connection operational health/status |
| `admin:connections:test` | Trigger an immediate live connection probe |

### Admin · Observability

| Scope | Gates |
|---|---|
| `admin:observability:read` | Read aggregated query/rejection/pressure trends |
| `admin:metrics:read` | Scrape the raw Prometheus /metrics endpoint |
| `admin:audit:worm-search` | Search the durable WORM (S3 Object Lock) compliance audit archive |

### Catalog governance

| Scope | Gates |
|---|---|
| `catalog:generate` | Generate draft catalog entries for review |
| `catalog:author` | Author a curated catalog entry (quarantined until published) |
| `catalog:review` | Review pending catalog proposals |
| `catalog:edit` | Edit a pending catalog proposal |
| `catalog:approve` | Approve a catalog proposal |
| `catalog:reject` | Reject a catalog proposal |
| `catalog:publish` | Publish an approved catalog proposal live |
| `catalog:rollback` | Roll back the published catalog to an earlier version |
| `catalog:export` | Export the catalog (backup / data portability) |
| `catalog:delete` | Delete catalog content (retention/deletion) |

### Query approval

| Scope | Gates |
|---|---|
| `query:approve` | Grant an approval token for a query that tripped the human-in-the-loop gate |

### Query cancellation

| Scope | Gates |
|---|---|
| `query:cancel` | Cancel another principal's in-flight async query (self-cancellation needs no scope) |

## Recommended role bundles

Advisory groupings — QueryGate enforces individual scopes, not roles. Register each as a role in your IdP and grant it the listed scopes; provisioning a user is then a role assignment.

### Analyst

Runs structured queries. Carries no authorization scope; table/column access is resolved from policy.yaml by identity (`sub`/claim).

- _(no scopes — governed by data policy only)_

### Operator

Day-2 operations: reload config, check connection health, read trends and scrape Prometheus metrics.

- `admin:reload-config`
- `admin:connections:read`
- `admin:connections:test`
- `admin:observability:read`
- `admin:metrics:read`

### Config Governor

Authors governed config changes (stage/apply/rollback) and reloads.

- `admin:config:read`
- `admin:config:write`
- `admin:reload-config`

### Config Approver

Reviews and approves/rejects staged config changes (four-eyes). Kept separate from the authoring role so an author cannot approve their own change when require_config_approvals is enabled.

- `admin:config:read`
- `admin:config:approve`

### Catalog Author

Proposes catalog content for review (kept quarantined until published).

- `catalog:generate`
- `catalog:author`

### Catalog Admin

Governs the catalog review queue and what is published live.

- `catalog:review`
- `catalog:edit`
- `catalog:approve`
- `catalog:reject`
- `catalog:publish`
- `catalog:rollback`

### Catalog Data Steward

Backup/restore and retention of catalog content.

- `catalog:export`
- `catalog:delete`

### Query Approver

Reviews and approves individual queries that trip the in-query human-in-the-loop gate (sensitive/expensive reads). Kept separate from the querying role so an agent cannot approve its own read.

- `query:approve`

### Query Operator

Steps in to cancel another principal's stuck or runaway async query. A caller can always cancel their own query without this scope.

- `query:cancel`

### Compliance Auditor

Searches the durable WORM audit archive for a security/compliance review. Kept separate from the Operator bundle: this reaches a long-retention copy an Operator's day-2 observability access does not need.

- `admin:audit:worm-search`
