"""Stable authorization scope names shared by REST, MCP, and guide services."""

ADMIN_RELOAD_CONFIG_SCOPE = "admin:reload-config"
ADMIN_CONFIG_READ_SCOPE = "admin:config:read"
ADMIN_CONFIG_WRITE_SCOPE = "admin:config:write"

# Catalog governance (TODO.md item 32B-1) — least-privilege, split by
# operation rather than one broad "catalog admin" scope. export/delete are
# 32B-2 scope and intentionally have no constant here yet.
CATALOG_GENERATE_SCOPE = "catalog:generate"
CATALOG_REVIEW_SCOPE = "catalog:review"
CATALOG_EDIT_SCOPE = "catalog:edit"
CATALOG_APPROVE_SCOPE = "catalog:approve"
CATALOG_REJECT_SCOPE = "catalog:reject"
CATALOG_PUBLISH_SCOPE = "catalog:publish"
CATALOG_ROLLBACK_SCOPE = "catalog:rollback"
