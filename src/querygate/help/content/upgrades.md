---
id: operations.upgrades
title: Upgrade QueryGate safely
summary: Validate configuration against the target release, review release notes, stage changes, verify health, and preserve a rollback path.
tags: [upgrade, release, migration, rollback, version, validation]
next_actions:
  - Validate current configuration with the target QueryGate version before deployment.
---
Read the target release notes and configuration changes before upgrading. Run
the target version's `querygate-validate-config` against connections, policy,
and catalog files in CI. Build or obtain the immutable release artifact and
verify its version before changing production traffic.

Preserve the current image and active configuration version. Deploy the new
version to a non-production environment, verify `/health`, metrics scraping,
authentication, caller visibility, schema discovery, a bounded query, audit
persistence, and configuration reload. Then roll out gradually using the
deployment's normal health checks.

If validation, visibility, execution, or observability changes unexpectedly,
restore the prior image and apply the previously active configuration version.
Guide citations always identify the installed version whose behavior they
describe.
