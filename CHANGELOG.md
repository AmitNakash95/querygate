# Changelog

All notable changes to QueryGate are documented here.

## [Unreleased]

### Added

- Optional curated schema-catalog overlay (`querygate/catalog/`, `CATALOG_FILE`): business
  descriptions, aliases, relationship hints, sensitivity labels, default aggregation
  preference, and an `allow_samples` flag, curated per connection/table/column and merged
  into `describe_table` (MCP and REST) as a `catalog` object. Filtered by the same
  per-principal policy as everything else — denied columns and relationship hints toward
  denied tables never appear. Hot-reloadable via the existing config-reload endpoint and
  validated by `querygate-validate-config --catalog-file`.

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
