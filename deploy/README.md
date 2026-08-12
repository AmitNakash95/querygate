# QueryGate production deployment reference

Two verified reference deployments — pick the one matching your platform.
Both assume you already have a real database; QueryGate never bundles one.

| | Docker Compose | Helm (Kubernetes) |
|---|---|---|
| Where | `docker-compose/` | `helm/querygate/` |
| Good for | A single host, or a small fixed set of hosts | Any Kubernetes cluster |
| Scaling | Manual (`docker compose up --scale`, watch Redis config) | `replicaCount` / `autoscaling.enabled` |
| Secrets | `.env.production` (gitignored, not `.env.production.example`) | `secrets.existingSecretName` (recommended) or `secrets.create` (quick start) |
| Metrics | Optional bundled `prometheus` service (`--profile monitoring`) or point your own Prometheus at `prometheus.yml`'s scrape config | `metrics.serviceMonitor.enabled` (Prometheus Operator) or scrape the Service directly |

Both were verified against a real deployment before being committed, not just
rendered: the Compose stack was brought up against a real Postgres and
queried end-to-end (including the audit JSONL sink and the optional
Prometheus profile actually scraping `/metrics`), and the Helm chart was
installed into a real `kind` cluster — 2 replicas, real Postgres and Redis,
a real structured query, and the non-root `securityContext` confirmed
against the actual image user (`docker run --rm <image> id`, not guessed).

## Quickstart

### Docker Compose

```bash
cd docker-compose
cp .env.production.example .env.production   # fill in real values
# Adapt config/connections.yaml and config/policy.yaml to your database
docker compose -f docker-compose.prod.yml --env-file .env.production up -d
curl http://localhost:8000/health
```

### Helm

```bash
cd helm
helm install querygate ./querygate \
  --set image.repository=your-registry/querygate \
  --set image.tag=0.1.0 \
  --set secrets.existingSecretName=querygate-secrets \
  -f my-values.yaml   # your connections.yaml/policy.yaml content, redis.url, etc.
kubectl rollout status deployment/querygate-querygate
```

See `helm/querygate/values.yaml` for every knob (it's the source of truth,
kept in sync with the templates by construction — this README doesn't
duplicate it) and `helm/querygate/templates/NOTES.txt` for what `helm
install` prints afterward.

## Both assume

- **You bring the database.** Neither stack provisions Postgres/MSSQL —
  point `connections.yaml` at a real, already-running database with a
  least-privileged, read-only account (see `docs/THREAT_MODEL.md`'s
  deployment requirements).
- **You bring TLS.** Neither stack terminates TLS. Put a reverse proxy or
  your platform's ingress/load balancer in front and terminate there —
  QueryGate should never see plaintext traffic from an untrusted network.
- **`connections.yaml`/`policy.yaml`/`catalog.yaml` are safe to commit as
  rendered here** — connection strings are always `${VAR}`/`${vault:...}`
  references, never literal secrets (see README.md's "Example connection
  config"). Only the resolved values (API keys, database credentials, a
  Vault token) are secret, and both stacks keep those in a separate,
  gitignored/externally-managed location.
- **Redis is required once you run more than one QueryGate instance.**
  `CONCURRENCY_BACKEND=redis` makes `Policy.max_concurrency` actually mean
  something across replicas; without it, each instance enforces the cap
  independently and the real ceiling silently multiplies by (replicas × worker processes).
- **`/metrics`, `/health`, and the `/admin/*` routes are unauthenticated or
  privileged-only respectively** — restrict network access to them at the
  proxy/ingress/firewall layer, not just at the application layer. See
  `docs/THREAT_MODEL.md`'s deployment requirements.

See `runbook.md` for config reloads, the config-governance API, secret
rotation, and rollback procedures once something is running, and `HA_DR.md`
for the highly-available / multi-zone deployment (`helm/querygate/values-ha.yaml`),
the shared-state correctness matrix (concurrency vs. quota vs. config vs.
audit), zero-downtime rolling config reloads, and backup/restore + RTO/RPO.
