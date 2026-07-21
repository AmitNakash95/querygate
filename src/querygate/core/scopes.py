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

# Admin observability (TODO.md item 44) — read-only aggregated operational
# trends (query volume, rejection categories, queue/concurrency pressure,
# cost-estimation health). Its own scope, distinct from the config and
# connection scopes: reading trend aggregates is a different privilege from
# reading/changing config or probing a connection. The overview is built from
# already-public, low-cardinality metric labels — never a query, value, or
# principal — but a per-connection breakdown is still admin-gated per item 44.
ADMIN_OBSERVABILITY_READ_SCOPE = "admin:observability:read"

# Catalog governance (TODO.md item 32B) — least-privilege, split by
# operation rather than one broad "catalog admin" scope.
CATALOG_GENERATE_SCOPE = "catalog:generate"
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
