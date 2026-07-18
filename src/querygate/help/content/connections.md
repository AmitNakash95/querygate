---
id: configuration.connections
title: Configure database connections and secrets
summary: Define named PostgreSQL or MSSQL profiles while keeping connection credentials outside the configuration response surface.
tags: [configuration, connections.yaml, postgresql, mssql, credentials, environment, vault, secrets]
next_actions:
  - Explain the connection.connection_string field for the supported secret-reference behavior.
  - Validate the connections and policy files together before reload or staging.
---
`connections.yaml` contains a `connections` list. Every entry needs a unique
`id`, a supported `dialect` (`postgresql` or `mssql`), and a SQLAlchemy
`connection_string`. Optional fields include `enabled`, `description`,
`known_tables`, and `join_group`.

Use `${ENVIRONMENT_VARIABLE}` for environment-backed credentials or the
configured external-secret reference syntax. Resolved credentials belong only
inside the private runtime connection registry: public REST and MCP responses
use a credential-free connection projection. Disabling a profile hides it even
when policy would otherwise allow it.

`known_tables` is a discovery seed, not an authorization rule. `join_group`
permits explicitly configured cross-connection joins only when the effective
policies also agree. Access control belongs in `policy.yaml`, and the two files
must be validated together so policy cannot reference an unknown connection.
