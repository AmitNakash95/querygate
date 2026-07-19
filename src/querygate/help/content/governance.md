---
id: configuration.governance
title: Validate, preview, stage, apply, and roll back configuration
summary: Configuration changes use the existing scoped governance workflow with validation, immutable versions, attribution, audit events, and rollback.
tags: [configuration, governance, validate, preview, diff, stage, apply, rollback, audit]
next_actions:
  - Validate and preview the candidate before staging it.
  - Apply only the intended staged version and retain its previous active version for rollback.
---
The governance API manages complete immutable snapshots of connections,
policy, and optional catalog configuration. A candidate may replace one file
while inheriting the others from the active version. Validation uses the same
models, secret resolvers, and cross-file checks as normal startup and reload.

Callers need `admin:config:write` to validate, preview, stage, apply, or roll
back. A preview is non-persistent and content-free: a caller with read and
write scope receives a document-level changed/unchanged comparison, while a
write-only caller receives submitted/inherited so the endpoint cannot become
an equality oracle. A semantic access diff goes further than the document-level
preview: it reports the candidate's *resolved* access changes — typed
tightening/loosening/neutral changes to connection visibility, guardrail caps,
table/column access, mandatory-filter requirements, and join groups at the
connection baseline — rather than a YAML line diff. Like policy simulation, the
diff requires both `admin:config:read` and `admin:config:write` (it echoes
resolved policy detail while resolving caller-supplied config/secret
references) and never includes static filter values, secrets, predicate values,
or raw YAML. Secret values and references are never included. Staging
validates again, records the actor, and creates a new version without
activating it. Applying validates once more, reloads the version through the
normal configuration path, changes the active pointer, and emits an audit
event. Applying an older inactive version is a rollback.

`admin:config:read` permits version metadata and the guide's redacted effective
configuration summary. It does not permit mutation, and write scope does not
implicitly reveal configuration history.
