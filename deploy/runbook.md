# Operational runbook

Procedures for config reloads, the config-governance API, secret rotation,
and rollback — for both `docker-compose/` and `helm/` deployments. Assumes
`ADMIN_KEY` is a bearer token whose principal has the scopes named in each
section (`admin:reload-config`, or `admin:config:read`/`admin:config:write`
— see README.md's "Config-governance API" section for what each grants).

## Config reload — two paths, pick one per change

**Path A — edit files directly, then reload.** For teams that manage
`connections.yaml`/`policy.yaml`/`catalog.yaml` as infrastructure-as-code
(a Git repo, a ConfigMap update, a Compose volume mount):

1. Edit the file(s) on disk — the Compose stack's `config/` directory, or
   update the Helm release's `config.connectionsYaml`/`policyYaml`/
   `catalogYaml` values and `helm upgrade`.
2. `curl -X POST -H "Authorization: Bearer $ADMIN_KEY" $HOST/api/v1/admin/reload-config`
3. Confirm: `curl -H "Authorization: Bearer $ADMIN_KEY" $HOST/api/v1/connections`
   reflects the change.

This reloads whatever the running process's `CONNECTIONS_FILE`/
`POLICY_FILE`/`CATALOG_FILE` currently point at — a Helm `values` change
needs `helm upgrade` (which recreates pods with the new ConfigMap mounted)
before this reload has anything new to pick up; a Compose `config/` file
edit takes effect on the next reload immediately, no restart needed.

The reference Compose/Helm config mounts are read-only, so semantic-memory
automatic refresh remains disabled there by default. To enable it, place
`CATALOG_FILE` on a writable persistent mount (one shared file plus its
adjacent `.lock` for every replica), then set
`SEMANTIC_MEMORY_REFRESH_ENABLED=true`. The refresh transaction serializes
live scan/diff/write work and rewrites canonical YAML, so YAML comments are
not preserved. Alternatively, keep Git/ConfigMap as the source of truth and
run `querygate-semantic-memory refresh` against a governed writable copy
before applying it through Path A or B.

**Path B — submit through the governance API.** For validated, versioned,
audited, and roll-back-able changes without touching files on disk or
redeploying:

```bash
# 1. Dry-run first — validates without persisting anything
curl -X POST -H "Authorization: Bearer $ADMIN_KEY" $HOST/api/v1/admin/config/validate \
  -d '{"policy_yaml": "..."}'

# 2. Stage it as a new version
curl -X POST -H "Authorization: Bearer $ADMIN_KEY" $HOST/api/v1/admin/config/versions \
  -d '{"policy_yaml": "...", "description": "why this change"}'
# -> {"id": "N", "status": "staged", ...}

# 3. Apply it
curl -X POST -H "Authorization: Bearer $ADMIN_KEY" $HOST/api/v1/admin/config/versions/N/apply
```

Every stage/apply is attributed to the calling principal and recorded in the
audit trail (a `config.governance` event — see README.md's "Config-governance
API" section). Prefer this path when you want history, rollback, or
multiple people reviewing a change before it goes live; prefer Path A for
GitOps workflows where the Git repo is already the source of truth and
history.

## Rollback

**Config rollback (governance API)** — the same `apply` endpoint, targeting
an older version id:

```bash
curl -H "Authorization: Bearer $ADMIN_KEY" $HOST/api/v1/admin/config/versions   # find the id to roll back to
curl -X POST -H "Authorization: Bearer $ADMIN_KEY" $HOST/api/v1/admin/config/versions/<older-id>/apply
```

Re-validates before activating — if the older version no longer validates
(e.g. it depends on an env var that's since been removed), the rollback is
rejected rather than silently applied. History is never rewritten: every
version, including the one you're rolling back *from*, stays inspectable
via `GET /admin/config/versions/{id}`.

**Application rollback:**

- Docker Compose: `QUERYGATE_IMAGE=<previous-tag> docker compose -f docker-compose.prod.yml --env-file .env.production up -d`
- Helm: `helm rollback querygate <REVISION>` (`helm history querygate` to
  find the revision number), or `helm upgrade` with the previous `image.tag`.

Config-file rollback (Path A above) and application rollback are
independent — rolling back the app version doesn't revert a config change
made via the governance API, and vice versa.

## Secret rotation

- **API keys**: update `API_KEYS` (Compose: `.env.production`; Helm: the
  Secret referenced by `secrets.existingSecretName`, or `secrets.env` +
  `helm upgrade` for the quick-start path) and restart/redeploy — API keys
  are read once at process startup, not hot-reloaded.
- **Database credentials referenced by `${VAR}`**: same as above — update
  the env var's value and restart/redeploy. `${VAR}` interpolation itself
  re-resolves on every config reload, but the environment variable's value
  is fixed for the life of the process, so a plain env-var rotation still
  needs a restart to pick up the new value.
- **Database credentials referenced by `${vault:path#field}`**: rotate the
  secret in Vault directly. No restart needed — every `${vault:...}`
  reference re-resolves on the next config reload (Path A or B above), so
  `POST /admin/reload-config` (or a governance-API apply, even of an
  unrelated field) picks up the rotated value.
- **`VAULT_TOKEN` itself**: still a static, env-configured credential with
  no built-in rotation (see `docs/THREAT_MODEL.md`'s residual risks) —
  rotate it through your normal secret-rotation process and restart/redeploy
  QueryGate afterward, same as any other env-configured secret.

## Health, metrics, and audit

- `GET /health` — unauthenticated by design (orchestrator readiness probes
  shouldn't need a credential); returns aggregate healthy/unhealthy counts,
  never connection ids or driver errors. 503 when any connection is
  unhealthy.
- `GET /api/v1/admin/connections` (scope `admin:connections:read`) — when
  `/health` reports something unhealthy, this is the credential-free per-
  connection view that says *which* one: dialect, enabled state,
  healthy/degraded/disabled/unknown status, last check/success, latency,
  schema-reflected state, and a redacted failure category
  (`authentication`/`unreachable`/`timeout`/`error`). It never returns a
  connection string or the raw driver error — that stays in stdout logs.
- `POST /api/v1/admin/connections/{id}/test` (its own scope,
  `admin:connections:test`, independent of the read scope above) — after
  rotating a credential or changing network access, use this instead of
  waiting for the next background health-check interval; it triggers an
  immediate re-check and returns the same credential-free status shape. At
  most one manual probe per connection per
  `admin_connection_test_cooldown_seconds` (default 10s) — a second request
  inside that window gets `429`/`Retry-After` rather than opening another
  real connection to the database.
- `GET /metrics` — unauthenticated Prometheus text format; empty samples
  (only `# HELP`/`# TYPE` lines) until at least one query has run in the
  process's lifetime — that's expected, not a bug.
- Persisted audit JSONL (when `AUDIT_SINK_BACKEND=jsonl`) — Compose: the
  `querygate-audit` named volume; Helm: `/app/var/audit` inside each pod,
  ephemeral by default (`persistence.enabled: false`) since every event is
  also always in pod stdout logs regardless. Set `persistence.enabled: true`
  once you know your cluster's StorageClass if you need the JSONL copy to
  survive pod restarts too.
- A sink write failure (e.g. a permissions problem, disk full) never fails
  the underlying query — it logs `audit.sink.write_failed` to stdout. Alert
  on that log line if persisted audit durability matters to you.
