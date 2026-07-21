"""Stable authorization scope names shared by REST, MCP, and guide services."""

ADMIN_RELOAD_CONFIG_SCOPE = "admin:reload-config"
ADMIN_CONFIG_READ_SCOPE = "admin:config:read"
ADMIN_CONFIG_WRITE_SCOPE = "admin:config:write"

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
