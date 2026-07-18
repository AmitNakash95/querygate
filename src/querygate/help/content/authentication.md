---
id: authentication.scopes
title: Authentication, principals, scopes, and effective access
summary: API keys or JWTs resolve a principal; scopes authorize administrative capabilities while policy controls database visibility and query access.
tags: [authentication, api-key, jwt, oauth, principal, scopes, claims, permissions, access]
next_actions:
  - Call describe_my_querygate_access to inspect your own scopes and visible connections.
  - Grant only the minimum administrative scope needed for an operation.
---
REST and MCP accept configured API keys and can also validate JWT bearer
tokens through a JWKS endpoint. A successful authenticator produces a
principal with a subject, scopes, claims, and authentication method. Anonymous
access is a local-development convenience only when no real authenticator is
configured.

Scopes grant administrative capabilities. `admin:config:read` permits a
redacted configuration summary, `admin:config:write` permits validation and
the governed stage/apply workflow, and `admin:reload-config` permits reloading
the deployment's configured files. Write does not imply read.

Database access is resolved separately through per-principal policy. A caller
may have no administrative scopes and still query visible connections, or may
hold a configuration scope without seeing every database resource. Hidden and
unknown resources deliberately produce the same external behavior.
