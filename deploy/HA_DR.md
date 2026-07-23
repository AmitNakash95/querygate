# High availability & disaster recovery

The reference deployments (`deploy/README.md`) get QueryGate *running*. This
document is the operational contract for running it **highly available across
failure domains** and **recovering it after a loss** — the two questions an
enterprise security/SRE reviewer asks before putting production traffic through
it. It is deliberately honest about which shared state is cross-replica-correct
today and which is not, so you can size policy and topology around the real
behavior rather than an aspirational one.

Scope: the QueryGate tier itself. Your **operational databases** are yours to
run HA/DR for — QueryGate never bundles, proxies-around, or owns their storage;
it only holds a least-privileged read account to them.

Validation status: the HA/DR *mechanisms* below (zero-downtime rolling config
reload, zone spread, PDB, the backup/restore commands, the shared-state
behavior) are rendered and asserted by
`tests/unit/test_helm_ha_deployment.py` against the actual chart, and the
shared-state facts are what the code in `execution/concurrency.py`,
`execution/quota.py`, and `admin/service.py` actually does. A live
multi-zone/multi-region failover *drill* against your own cluster is the one
step only you can run — treat "Failover drill" below as the checklist for it.

---

## 1. Shared-state correctness matrix (read this first)

QueryGate replicas are stateless request handlers, but four kinds of state
behave differently under more than one replica. Getting a cap or a budget
"right" per-replica but wrong in aggregate is the classic multi-instance
footgun, so this is spelled out per-kind:

| State | Cross-replica behavior | How to make it correct |
|---|---|---|
| **In-flight concurrency cap** (`Policy.max_concurrency`, item 9) | **Correct & shared** when `CONCURRENCY_BACKEND=redis` — all replicas count against one Redis-backed limiter. With the in-process backend each replica enforces the cap independently, so the real ceiling silently multiplies by replica count. | Set `CONCURRENCY_BACKEND=redis` and a real `CONCURRENCY_REDIS_URL` whenever `replicaCount > 1`. The HA overlay does this. |
| **Per-principal rate / byte quota** (item 50) | **Shared & correct** when `CONCURRENCY_BACKEND=redis` — the `RedisQuotaLimiter` (item 50 phase 2) enforces a principal's request/byte rolling-window budget against the true cross-replica window via one Redis, so the budget is a single fleet-wide cap. With the **in-process** backend the window is per-replica, so the effective quota multiplies by replica count (up to N×). | Set `CONCURRENCY_BACKEND=redis` (the same setting that makes the concurrency cap shared) whenever `replicaCount > 1`; the HA overlay does this. Only the in-process fallback (single-replica) is per-replica. |
| **Config-governance version store** (Path B API, item 17/42) | **Per-pod** by default (each replica's own `CONFIG_GOVERNANCE_DIR`). A governance `apply` reloads **only the replica that served the request**; there is no cross-replica reload broadcast. | Prefer the GitOps/ConfigMap path (§2) for multi-replica config changes. If you need the governance *API* in an HA deployment, set `configGovernance.enabled=true` (an RWX PVC shared by all replicas) **and** roll the fleet after an `apply` so every replica reloads — or fan the `apply`/`reload-config` call out to each pod. |
| **Persisted audit ledger** (JSONL / `jsonl_chained`, item 91) | **Per-replica file.** Each pod writes its own file; the hash-chain integrity is *per file*, not fleet-global. Every event is also on the pod's stdout regardless. | Treat **stdout + your log aggregator** as the unified, durable audit stream. Persist per-pod JSONL (a PVC) only if you also want the local chained copy; verify each file's chain independently with `querygate-audit verify`. |

The one-line takeaway: **concurrency and per-principal quota are both true
shared caps under `CONCURRENCY_BACKEND=redis`; config and audit are per-replica
unless you deliberately share them.**

---

## 2. Zero-downtime config reload across replicas

There are two config-change paths (full command detail in `runbook.md`). Under
more than one replica they differ in an important way:

**Path A — GitOps / ConfigMap (recommended for multi-replica).** Edit the
`config.connectionsYaml` / `policyYaml` / `catalogYaml` values and
`helm upgrade`. The chart stamps a `checksum/config` annotation derived from the
rendered ConfigMap onto the pod template, so a changed config **rolls the
deployment automatically**. Combined with `updateStrategy.maxUnavailable: 0`,
Kubernetes brings a new, *ready* replica up before retiring an old one — the new
config propagates to **every** replica, one at a time, behind the readiness
gate, with capacity never dropping below `replicaCount`. This is the
multi-replica-correct, zero-downtime path.

```bash
# after editing values (or -f my-values.yaml)
helm upgrade querygate ./querygate -f values.yaml -f values-ha.yaml -f my-values.yaml
kubectl rollout status deployment/querygate-querygate   # blocks until the roll completes
```

Requires a surge headroom of at least one replica (`replicaCount >= 2`, or
`autoscaling.minReplicas >= 2`) — with a single replica, `maxUnavailable: 0`
cannot make progress and there is nothing to be HA about anyway.

**Path B — governance API.** `POST /admin/config/versions/{id}/apply` reloads
in-process on the **one** replica that receives the call (see the matrix above).
It is correct for single-replica staging, or for validated/versioned/rollback
workflows where you then roll the fleet to propagate. Do not assume a single
`apply` has taken effect fleet-wide.

**Neither path drops a request.** Readiness probes hold traffic off a pod until
it has reloaded and reflected healthy; the PDB keeps voluntary disruptions from
ever taking the fleet below `minAvailable`.

---

## 3. Multi-zone / multi-region topology

**Multi-zone (single cluster) — supported by the chart.** The HA overlay
(`values-ha.yaml`) sets:

- `autoscaling.minReplicas: 3` — a quorum survives a single pod or zone loss.
- `podDisruptionBudget` — node drains / cluster upgrades can't breach
  `minAvailable`.
- `topologySpreadConstraints` on `topology.kubernetes.io/zone` and
  `kubernetes.io/hostname` (soft `ScheduleAnyway`) — the scheduler spreads
  replicas across zones and nodes instead of stacking them. Switch to
  `DoNotSchedule` only if running degraded is preferable to co-location.

If you set a chart `nameOverride`, update the overlay's `labelSelector`
(`app.kubernetes.io/name: <your-name>`) to match — the spread is keyed on that
label.

The shared Redis (concurrency) is then a dependency to make HA in its own right:
a single Redis pod is a single point of failure for the concurrency cap. Point
`redis.url` at a managed or replicated Redis, not one pod.

**Multi-region (active/active) — deployment pattern, not a single chart
install.** QueryGate carries no cross-region consensus of its own, so run it as
**independent per-region deployments** that share nothing region-crossing except
what you deliberately replicate:

- Each region: its own QueryGate deployment + its own regional Redis.
  Concurrency caps are then per-region (acceptable — a region is the failure
  boundary). Route users to their region (geo DNS / global LB).
- Config: one Git repo of `connections.yaml`/`policy.yaml`/`catalog.yaml` is the
  single source of truth; each region's CD applies it via Path A. This keeps
  policy identical across regions without any runtime coupling.
- Databases: point each region at your database's regional
  read replica/endpoint. QueryGate does not manage that replication.
- Audit: ship every region's stdout to one central log store for a global,
  tamper-evident (per-file-chained) trail.

Active/passive is the same setup with the passive region scaled to zero (or
`minReplicas: 1`) and promoted by repointing the global LB — recovery time is
bounded by DNS/LB propagation plus a scale-up, typically minutes.

---

## 4. Backup & restore (disaster recovery)

QueryGate has **no app-owned database** — its entire durable footprint is a
handful of files plus rebuildable caches. That makes DR simple, but only if you
know exactly what to back up.

| Artifact | Where | Backup | RPO driver |
|---|---|---|---|
| **Connections / policy / catalog / templates** | Git repo (Path A) *or* the ConfigMap | Git is the backup — every change is a commit. | Last commit. Effectively zero if you commit before applying. |
| **Config-governance version history** (only if `configGovernance.enabled`) | RWX PVC at `CONFIG_GOVERNANCE_DIR` (`var/config_versions/`) | Snapshot the PVC, or `tar` the directory on a schedule. | Snapshot interval. |
| **Persisted audit ledger** (only if `AUDIT_SINK_BACKEND=jsonl*` + `persistence.enabled`) | Per-pod PVC at `AUDIT_JSONL_PATH` | The durable copy is your **log aggregator** (every event is on stdout). Back up per-pod PVCs too only if the local chained file itself is a compliance artifact. | Log-shipping lag (seconds) via stdout; snapshot interval for the PVC copy. |
| **Redis concurrency state** | Redis | **Do not back up** — it is ephemeral, self-heals on restart, and holds no source-of-truth data. | n/a |
| **Secrets** (API keys, DB creds, Vault token) | Your secrets manager / External Secrets / Sealed Secrets | Backed up by that system, not QueryGate. | That system's RPO. |

**Backup commands (governance PVC + audit copy):**

```bash
# config-governance history (if enabled) — copy out of any one pod (RWX = same content on all)
POD=$(kubectl get pod -l app.kubernetes.io/name=querygate -o jsonpath='{.items[0].metadata.name}')
kubectl exec "$POD" -- tar czf - -C /app/var config_versions > config-governance-$(date +%F).tgz

# audit ledger local copy (only if you persist it and it's a compliance artifact)
kubectl exec "$POD" -- tar czf - -C /app/var audit > audit-$(date +%F).tgz
```

**Restore / rebuild procedure (RTO target: minutes):**

1. **Recreate the platform** — cluster/namespace, StorageClasses, secrets
   (from your secrets manager), and Redis (empty is fine; it self-heals).
2. **Redeploy QueryGate** from a known image digest + the Git config:
   `helm upgrade --install querygate ./querygate -f values.yaml -f values-ha.yaml -f my-values.yaml`.
   Pin `image.tag`/digest to a verified release (`make verify-release`, item
   30/89) — do not restore by re-pulling `latest`.
3. **Restore governance history** (if used): untar the backup into the
   provisioned RWX PVC before the pods start (or into a pod, then restart).
   Skip entirely if you use Path A/GitOps — the config *is* in Git.
4. **Restore/verify audit**: the authoritative trail is already in your log
   store. If you restore per-pod JSONL copies, run
   `querygate-audit verify <file>` on each to confirm the hash chain is intact
   before treating it as evidence.
5. **Validate**: `kubectl rollout status`, then `curl /health` (200 = all
   connections healthy) and one real structured query end-to-end.

**Recovery objectives (reference, tune to your SLOs):**

- **RTO ≈ minutes.** Stateless tier + declarative deploy + GitOps config. The
  bound is image pull + pod readiness + (optional) PVC restore, not a database
  replay.
- **RPO ≈ zero for config** (Git commit) and **≈ log-shipping lag for audit**
  (stdout streamed continuously). The governance PVC's RPO is its snapshot
  interval — another reason to prefer GitOps for anything you can't afford to
  lose between snapshots.

---

## 5. Failover drill (the step only you can run)

Run this against your own multi-zone cluster to validate the above end-to-end
before a pilot depends on it:

1. **Rolling upgrade under load.** Drive steady query traffic, then
   `helm upgrade` with a changed `policyYaml`. Confirm zero 5xx during the roll
   (`maxUnavailable: 0` + readiness), and that the new policy is in effect on
   *every* replica afterward (`GET /connections` from several pods, or after
   several requests).
2. **Zone loss.** Cordon/drain a zone's nodes. Confirm the PDB holds
   `minAvailable`, surviving replicas keep serving, and the concurrency cap
   still totals correctly (Redis-shared) rather than multiplying.
3. **Redis loss.** Kill Redis. Confirm behavior matches your chosen posture
   (fail-closed vs. degraded) and that recovery is automatic when Redis returns.
   The quota limiter fails open on a Redis error (a request is admitted rather
   than blocked), so confirm that matches your posture too.
4. **Full-region loss (multi-region only).** Repoint the global LB to the second
   region; confirm RTO against your target and that audit from both regions
   lands in the central store.
5. **Restore from backup.** In a scratch namespace, execute §4's restore from
   your backups and confirm a real query succeeds — proving the backups are
   actually restorable, not just present.

Record the measured RTO/RPO from this drill in your own runbook; the numbers in
§4 are the design targets, your cluster's measured numbers are the contract.
