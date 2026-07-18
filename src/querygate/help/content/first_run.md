---
id: setup.first-run
title: Install and run QueryGate for the first time
summary: Configure connections and policy, validate them, start the service, and verify health before issuing a structured query.
tags: [install, setup, first-run, start, health, docker, poetry]
next_actions:
  - Copy the bundled example configuration files and replace the example connection URL reference.
  - Run querygate-validate-config before starting the service.
---
QueryGate requires Python 3.11 or newer. Install the project dependencies,
copy `examples/connections.example.yaml` and `examples/policy.example.yaml`,
and optionally copy `examples/catalog.example.yaml` when business descriptions
or sensitivity metadata are useful.

Set `CONNECTIONS_FILE`, `POLICY_FILE`, and optionally `CATALOG_FILE` to those
files. Put database credentials in environment variables or the configured
external secret resolver; keep only references in the connections file. Run
`querygate-validate-config` to catch malformed YAML, invalid field values, and
cross-file connection references before startup.

Start QueryGate with the `querygate` command. Check `/health`, then call
`GET /api/v1/connections` or the MCP `list_connections` tool. Continue with
table discovery and `describe_table`; do not guess deployment-specific
connection, table, or column names.
