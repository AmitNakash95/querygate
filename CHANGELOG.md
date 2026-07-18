# Changelog

All notable changes to QueryGate are documented here.

## [Unreleased]

### Added

- A repeatable real-PostgreSQL load/soak harness for execution guardrails. Concurrent REST
  bursts are checked against PostgreSQL's observed active-query count, covering strict
  `max_concurrency` enforcement, overflow rejection, queued completion, and statement
  timeout cancellation under load (`make test-load` / `make test-soak`).
- Optional curated schema-catalog overlay (`querygate/catalog/`, `CATALOG_FILE`): business
  descriptions, aliases, relationship hints, sensitivity labels, default aggregation
  preference, and an `allow_samples` flag, curated per connection/table/column and merged
  into `describe_table` (MCP and REST) as a `catalog` object. Filtered by the same
  per-principal policy as everything else — denied columns and relationship hints toward
  denied tables never appear. Hot-reloadable via the existing config-reload endpoint and
  validated by `querygate-validate-config --catalog-file`.
- Pluggable secret resolution for connection strings (`querygate/secrets/`): `${VAR}`
  keeps resolving from the environment unchanged, and a new `${vault:path#field}` syntax
  resolves a HashiCorp Vault KV v2 secret (`VAULT_ENABLED`/`VAULT_ADDR`/`VAULT_TOKEN`/
  `VAULT_KV_MOUNT`/`VAULT_NAMESPACE`). A `SecretResolver` interface keeps future backends
  (AWS/GCP Secrets Manager) additive — no change to how connections are loaded, reloaded,
  or validated. Every `${...}` reference re-resolves on each config load/hot reload, so a
  rotated secret takes effect without a restart. Resolver errors never echo the configured
  token or the backend's own response text.
- Config-governance API (`querygate/admin/`, `/api/v1/admin/config/*`): validate, stage,
  apply, and roll back versioned connections/policy/catalog snapshots over REST, separate
  from and layered on top of the existing `/admin/reload-config` file-reload endpoint.
  Every version is attributed to the calling principal, gated behind new
  `admin:config:read`/`admin:config:write` scopes, and recorded in the same audit sink as
  query execution via a new `ConfigChangeEvent`. Applying re-validates a version
  immediately beforehand, so a version that stops validating between staging and applying
  is rejected, not silently activated. Rollback reuses the same apply endpoint against an
  older version id — history is never rewritten.
- Production deployment reference stacks under `deploy/`: a Docker Compose file and a Helm
  chart (app + Redis, config/secrets mounting, Prometheus scrape config, liveness/readiness
  probes, `runbook.md` for reloads/rotation/rollback). Both verified against a real
  deployment — a real Postgres for Compose, a real `kind` cluster for Helm — which caught
  and fixed a real bug: a fresh named Docker volume is root-owned by default and the
  production image runs as non-root, so the audit JSONL sink failed to write until a
  one-shot init container (Compose) / `fsGroup` (Helm, automatic) fixed volume ownership.

## [0.1.0] — 2026-07-18

Initial self-hosted release candidate.

### Added

- Structured, schema-validated database reads over REST and MCP with no raw-SQL input.
- Postgres and MSSQL adapters, live schema discovery, policy enforcement, query timeouts,
  response-size limits, and in-process or Redis-backed concurrency controls.
- Per-principal visibility and policy overrides with API-key and JWKS/JWT authentication.
- Readiness monitoring, Prometheus metrics, configuration validation/hot reload, and a
  redaction-safe persisted audit-event sink.
- Adversarial security regressions and live Postgres/MSSQL integration coverage.

### Release notes

- The supported release surfaces are the Python package and self-hosted container.
- SQLite remains an internal testing/fixture aid, not a supported connection dialect.
- Artifact signing, SBOM publication, and public registry automation remain tracked as
  post-0.1.0 distribution work.
