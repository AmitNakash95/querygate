---
id: interfaces.mcp-rest
title: Use QueryGate over MCP and REST
summary: MCP and REST expose the same connection discovery, schema discovery, explain, execution, and batch-query capabilities.
tags: [mcp, rest, api, tools, structured-query, list-connections, describe-table]
next_actions:
  - Begin with list_connections, list_tables, and describe_table in that order.
---
The safest workflow is discovery before execution. List connections visible to
the authenticated principal, list tables for one returned connection, and
describe a candidate table to obtain allowed columns and optional catalog
metadata. Then submit a `StructuredQuery` to the explain operation before
executing it when the shape is unfamiliar.

MCP exposes purpose-built tools and is mounted at the configured MCP path.
REST exposes equivalent operations below `/api/v1`. Both surfaces authenticate
the caller and call the same underlying service; neither accepts raw SQL.

Use batch execution only for a bounded set of independent structured queries.
If a concurrency limit is returned, retry once after a delay rather than in a
tight loop. Treat identifiers returned by discovery as authoritative for the
current caller and deployment.
