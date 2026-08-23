"""Stable authorization scope names shared by REST, MCP, and guide services.

This module is the **single source of truth** for QueryGate's authorization
scope vocabulary. Beyond the raw constants, it publishes a structured
`SCOPE_CATALOG` (every scope, the action it gates, its category) and
`ROLE_BUNDLES` (recommended groupings an operator pastes into their IdP). Both
the RFC 9728 protected-resource metadata (`mcp/oauth_metadata.py`) and the
generated operator reference (`docs/SCOPE_CATALOG.md`, produced by
`querygate-scope-catalog`) derive from these, so the wire metadata, the docs,
and the enforced constants can never drift apart (TODO.md item 95). Adding a
scope means adding its constant AND a `ScopeInfo` row here — a test
(`tests/unit/test_scope_catalog.py`) fails if a constant is missing from the
catalog, so the "forgotten scope" gap can't reopen.
"""

from __future__ import annotations

from typing import NamedTuple, Tuple

ADMIN_RELOAD_CONFIG_SCOPE = "admin:reload-config"
ADMIN_CONFIG_READ_SCOPE = "admin:config:read"
ADMIN_CONFIG_WRITE_SCOPE = "admin:config:write"
# Four-eyes config approval (TODO.md item 42) — reviewing/approving a staged
# config version is a distinct privilege from authoring one (admin:config:write),
# so a deployment can require author != approver by giving them to different
# principals. Enforced server-side, never only in the UI.
ADMIN_CONFIG_APPROVE_SCOPE = "admin:config:approve"

# Admin connection-operations (TODO.md item 43) — read-only operational health
# of configured connections. Separate from the config scopes: seeing whether a
# connection is reachable is a distinct privilege from reading/changing what
# QueryGate connects to.
ADMIN_CONNECTIONS_READ_SCOPE = "admin:connections:read"
# The "test now" probe (item 43 phase 2) triggers a real, immediate
# connection attempt against a customer database on demand — a distinct,
# audited action from passively reading the last cached status, so it gets
# its own least-privilege scope rather than being implied by the read scope.
ADMIN_CONNECTIONS_TEST_SCOPE = "admin:connections:test"

# Admin observability (TODO.md item 44) — read-only aggregated operational
# trends (query volume, rejection categories, queue/concurrency pressure,
# cost-estimation health). Its own scope, distinct from the config and
# connection scopes: reading trend aggregates is a different privilege from
# reading/changing config or probing a connection. The overview is built from
# already-public, low-cardinality metric labels — never a query, value, or
# principal — but a per-connection breakdown is still admin-gated per item 44.
ADMIN_OBSERVABILITY_READ_SCOPE = "admin:observability:read"

# Managed search over the WORM (compliance-grade, S3 Object Lock) audit
# archive (TODO.md item 134 phase 2, audit/worm_search.py). Deliberately its
# OWN scope rather than reuse of ADMIN_OBSERVABILITY_READ_SCOPE: the WORM
# archive is the durable, long-retention compliance copy — potentially
# covering years a locally-rotated file no longer holds — so a principal that
# can read the in-process anomaly/observability aggregates should not
# automatically be able to search that archive too. A deployment that wants
# both grants both scopes explicitly (see the Compliance Auditor role bundle
# below); holding one never implies the other.
ADMIN_AUDIT_WORM_SEARCH_SCOPE = "admin:audit:worm-search"

# Raw Prometheus scrape endpoint (TODO.md item 144). Its own scope, distinct
# from admin:observability:read: the scraper (Prometheus, a sidecar) is a
# different consumer than an admin-UI operator reading aggregated trends, and
# /metrics exposes lower-level, higher-cardinality-adjacent signal (per-reason
# rejection counters) that a dedicated least-privilege credential should gate
# rather than folding into the observability-dashboard scope. Enforced only
# when AppConfig.metrics_require_auth is true (the default; see
# docs/THREAT_MODEL.md QG-36).
ADMIN_METRICS_READ_SCOPE = "admin:metrics:read"

# Read the recorded observed query shapes and render a template draft from one
# (TODO.md item 195, admin/observed_shapes.py). Its OWN scope rather than reuse
# of ADMIN_OBSERVABILITY_READ_SCOPE: an observed shape is a far more specific
# object than an aggregate trend — it names the exact tables, columns, joins
# and predicates one principal queries — so it is closer to reading that
# principal's query catalogue than to reading a dashboard. Holding the
# observability scope should not imply it. It stays a *read* scope because
# nothing on this surface mutates: a drafted template is returned for review,
# never installed.
ADMIN_SHAPES_READ_SCOPE = "admin:shapes:read"

# Catalog governance (TODO.md item 32B) — least-privilege, split by
# operation rather than one broad "catalog admin" scope.
CATALOG_GENERATE_SCOPE = "catalog:generate"
# Human authoring of a curated catalog entry (TODO.md item 84) — distinct
# from review so a deployment *can* keep authoring and approving as different
# principals, but separation of duties is enforced by scope, not identity: a
# principal that also holds catalog:review may approve/publish its own manual
# proposal. A manual proposal stays quarantined until published, exactly like
# a generated one.
CATALOG_AUTHOR_SCOPE = "catalog:author"
CATALOG_REVIEW_SCOPE = "catalog:review"
CATALOG_EDIT_SCOPE = "catalog:edit"
CATALOG_APPROVE_SCOPE = "catalog:approve"
CATALOG_REJECT_SCOPE = "catalog:reject"
CATALOG_PUBLISH_SCOPE = "catalog:publish"
CATALOG_ROLLBACK_SCOPE = "catalog:rollback"
# 32B-2: bidirectional data-portability (export + import/restore) and
# retention/deletion, each its own least-privilege scope.
CATALOG_EXPORT_SCOPE = "catalog:export"
CATALOG_DELETE_SCOPE = "catalog:delete"

# In-query human-in-the-loop approval (TODO.md item 92) — a holder can grant an
# approval token for a specific query that tripped the pre-execution approval
# gate. Deliberately separate from the query-execution path so the querying
# agent cannot approve its own expensive/sensitive read; a human/approver holds
# this.
QUERY_APPROVE_SCOPE = "query:approve"

# Cancel another principal's in-flight async query (TODO.md item 35 phase 3).
# Cancelling YOUR OWN query needs no scope at all — the same posture as
# submitting one in the first place, which this resource never scopes either
# (data access is governed by policy.yaml keyed by `sub`, not a scope). This
# scope only gates the cross-principal case: an operator stepping in to stop
# someone else's stuck query.
QUERY_CANCEL_SCOPE = "query:cancel"

# Human identity administration (TODO.md item 199, querygate/identity/). Its OWN
# pair of scopes rather than reuse of admin:config:*: identity.yaml decides who
# can sign in and what their groups earn them, and users.yaml holds password
# verifiers — an operator trusted to stage a connection change is not
# automatically trusted to mint an account that can approve one. The read scope
# never exposes a verifier, a TOTP secret, or a client secret (those fields do
# not exist on the public models at all); it lists accounts, providers, active
# sessions, and issued tokens.
ADMIN_IDENTITY_READ_SCOPE = "admin:identity:read"
# Create/disable/delete a local account, rotate a password or second factor,
# revoke a person's sessions and issued device tokens.
ADMIN_IDENTITY_WRITE_SCOPE = "admin:identity:write"


class ScopeInfo(NamedTuple):
    """One authorization scope: the wire string, the action it gates, and the
    category it belongs to (for grouping in metadata and docs)."""

    scope: str
    category: str
    gates: str


class RoleBundle(NamedTuple):
    """A recommended grouping of scopes into an IdP role. Purely advisory
    guidance — QueryGate enforces individual scopes, not roles — but it is the
    copy-paste source an operator pastes into Okta/Entra/Auth0/Keycloak so
    end-user provisioning reduces to role assignment."""

    name: str
    purpose: str
    scopes: Tuple[str, ...]


# The complete scope vocabulary this resource understands, in a stable,
# operator-friendly order. THIS is what RFC 9728 `scopes_supported` advertises
# (distinct from `mcp_required_scopes`, the access gate). Every `*_SCOPE`
# constant above must appear here exactly once — enforced by test.
SCOPE_CATALOG: Tuple[ScopeInfo, ...] = (
    ScopeInfo(
        ADMIN_RELOAD_CONFIG_SCOPE,
        "Admin · Config",
        "Hot-reload connections/policy/catalog from disk",
    ),
    ScopeInfo(
        ADMIN_CONFIG_READ_SCOPE, "Admin · Config", "Read config-governance versions and history"
    ),
    ScopeInfo(
        ADMIN_CONFIG_WRITE_SCOPE,
        "Admin · Config",
        "Stage/apply/roll back config-governance versions",
    ),
    ScopeInfo(
        ADMIN_CONFIG_APPROVE_SCOPE,
        "Admin · Config",
        "Approve/reject a staged config version (four-eyes; not the author)",
    ),
    ScopeInfo(
        ADMIN_CONNECTIONS_READ_SCOPE,
        "Admin · Connections",
        "Read per-connection operational health/status",
    ),
    ScopeInfo(
        ADMIN_CONNECTIONS_TEST_SCOPE,
        "Admin · Connections",
        "Trigger an immediate live connection probe",
    ),
    ScopeInfo(
        ADMIN_OBSERVABILITY_READ_SCOPE,
        "Admin · Observability",
        "Read aggregated query/rejection/pressure trends",
    ),
    ScopeInfo(
        ADMIN_METRICS_READ_SCOPE,
        "Admin · Observability",
        "Scrape the raw Prometheus /metrics endpoint",
    ),
    ScopeInfo(
        ADMIN_AUDIT_WORM_SEARCH_SCOPE,
        "Admin · Observability",
        "Search the durable WORM (S3 Object Lock) compliance audit archive",
    ),
    ScopeInfo(
        ADMIN_SHAPES_READ_SCOPE,
        "Admin · Observability",
        "Read observed query shapes and draft a template from one",
    ),
    ScopeInfo(
        CATALOG_GENERATE_SCOPE, "Catalog governance", "Generate draft catalog entries for review"
    ),
    ScopeInfo(
        CATALOG_AUTHOR_SCOPE,
        "Catalog governance",
        "Author a curated catalog entry (quarantined until published)",
    ),
    ScopeInfo(CATALOG_REVIEW_SCOPE, "Catalog governance", "Review pending catalog proposals"),
    ScopeInfo(CATALOG_EDIT_SCOPE, "Catalog governance", "Edit a pending catalog proposal"),
    ScopeInfo(CATALOG_APPROVE_SCOPE, "Catalog governance", "Approve a catalog proposal"),
    ScopeInfo(CATALOG_REJECT_SCOPE, "Catalog governance", "Reject a catalog proposal"),
    ScopeInfo(
        CATALOG_PUBLISH_SCOPE, "Catalog governance", "Publish an approved catalog proposal live"
    ),
    ScopeInfo(
        CATALOG_ROLLBACK_SCOPE,
        "Catalog governance",
        "Roll back the published catalog to an earlier version",
    ),
    ScopeInfo(
        CATALOG_EXPORT_SCOPE, "Catalog governance", "Export the catalog (backup / data portability)"
    ),
    ScopeInfo(
        CATALOG_DELETE_SCOPE, "Catalog governance", "Delete catalog content (retention/deletion)"
    ),
    ScopeInfo(
        QUERY_APPROVE_SCOPE,
        "Query approval",
        "Grant an approval token for a query that tripped the human-in-the-loop gate",
    ),
    ScopeInfo(
        QUERY_CANCEL_SCOPE,
        "Query cancellation",
        "Cancel another principal's in-flight async query (self-cancellation needs no scope)",
    ),
    ScopeInfo(
        ADMIN_IDENTITY_READ_SCOPE,
        "Admin · Identity",
        "List sign-in providers, local accounts, active sessions, and issued device tokens",
    ),
    ScopeInfo(
        ADMIN_IDENTITY_WRITE_SCOPE,
        "Admin · Identity",
        "Manage local accounts and revoke a person's sessions and device tokens",
    ),
)

# Every scope string this resource understands, derived from SCOPE_CATALOG so it
# can never drift from the documented set.
ALL_SCOPES: Tuple[str, ...] = tuple(info.scope for info in SCOPE_CATALOG)

# Recommended IdP role groupings. Advisory only — enforcement is per-scope — but
# this is the turnkey wire-up: register these as roles in your IdP and grant the
# scopes each lists. Data-access grants (which tables/columns a principal may
# read) are NOT here: they stay in policy.yaml keyed by `sub`/claim, so an
# Analyst carries no scope at all and is governed entirely by data policy.
ROLE_BUNDLES: Tuple[RoleBundle, ...] = (
    RoleBundle(
        "Analyst",
        "Runs structured queries. Carries no authorization scope; table/column "
        "access is resolved from policy.yaml by identity (`sub`/claim).",
        (),
    ),
    RoleBundle(
        "Operator",
        "Day-2 operations: reload config, check connection health, read trends "
        "and scrape Prometheus metrics.",
        (
            ADMIN_RELOAD_CONFIG_SCOPE,
            ADMIN_CONNECTIONS_READ_SCOPE,
            ADMIN_CONNECTIONS_TEST_SCOPE,
            ADMIN_OBSERVABILITY_READ_SCOPE,
            ADMIN_METRICS_READ_SCOPE,
        ),
    ),
    RoleBundle(
        "Config Governor",
        "Authors governed config changes (stage/apply/rollback) and reloads.",
        (ADMIN_CONFIG_READ_SCOPE, ADMIN_CONFIG_WRITE_SCOPE, ADMIN_RELOAD_CONFIG_SCOPE),
    ),
    RoleBundle(
        "Config Approver",
        "Reviews and approves/rejects staged config changes (four-eyes). Kept "
        "separate from the authoring role so an author cannot approve their own "
        "change when require_config_approvals is enabled.",
        (ADMIN_CONFIG_READ_SCOPE, ADMIN_CONFIG_APPROVE_SCOPE),
    ),
    RoleBundle(
        "Catalog Author",
        "Proposes catalog content for review (kept quarantined until published).",
        (CATALOG_GENERATE_SCOPE, CATALOG_AUTHOR_SCOPE),
    ),
    RoleBundle(
        "Catalog Admin",
        "Governs the catalog review queue and what is published live.",
        (
            CATALOG_REVIEW_SCOPE,
            CATALOG_EDIT_SCOPE,
            CATALOG_APPROVE_SCOPE,
            CATALOG_REJECT_SCOPE,
            CATALOG_PUBLISH_SCOPE,
            CATALOG_ROLLBACK_SCOPE,
        ),
    ),
    RoleBundle(
        "Catalog Data Steward",
        "Backup/restore and retention of catalog content.",
        (CATALOG_EXPORT_SCOPE, CATALOG_DELETE_SCOPE),
    ),
    RoleBundle(
        "Identity Administrator",
        "Manages who can sign in: local accounts, active sessions, and issued "
        "device tokens. Deliberately separate from the config roles — minting an "
        "account is a different privilege from changing what a connection exposes.",
        (ADMIN_IDENTITY_READ_SCOPE, ADMIN_IDENTITY_WRITE_SCOPE),
    ),
    RoleBundle(
        "Query Approver",
        "Reviews and approves individual queries that trip the in-query "
        "human-in-the-loop gate (sensitive/expensive reads). Kept separate from "
        "the querying role so an agent cannot approve its own read.",
        (QUERY_APPROVE_SCOPE,),
    ),
    RoleBundle(
        "Query Operator",
        "Steps in to cancel another principal's stuck or runaway async query. "
        "A caller can always cancel their own query without this scope.",
        (QUERY_CANCEL_SCOPE,),
    ),
    RoleBundle(
        "Compliance Auditor",
        "Searches the durable WORM audit archive for a security/compliance "
        "review. Kept separate from the Operator bundle: this reaches a "
        "long-retention copy an Operator's day-2 observability access does "
        "not need.",
        (ADMIN_AUDIT_WORM_SEARCH_SCOPE,),
    ),
    RoleBundle(
        "Access Narrower",
        "Reads the observed query shapes a principal actually runs and drafts "
        "curated templates from them, ahead of switching that principal to "
        "templates_only (TODO.md item 195). Kept separate from the Operator "
        "bundle: a shape names one principal's exact tables, columns and "
        "predicates, which day-2 dashboard access does not need. The bundle "
        "grants no ability to install a template — that stays a config change.",
        (ADMIN_SHAPES_READ_SCOPE,),
    ),
)
