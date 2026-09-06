# Install QueryGate

One command. It generates its own admin credential, seeds a **deny-by-default**
policy, and starts refusing everything until you say what an agent may see.

> **What "deny-by-default" means here.** A fresh install reaches **no table and
> no column**. That is deliberate (TODO.md item 220): before it, enabling a
> connection made every table on it readable up to the numeric caps, which is
> the opposite of what a governance product should do on first run. You will add
> the first table by hand, once, and that is the point.

---

## Docker

```bash
docker run -d --name querygate \
  -p 8000:8000 \
  -v querygate-var:/app/var \
  ghcr.io/<your-org>/querygate:latest
```

> **Connecting to Microsoft SQL Server?** The default image deliberately ships
> **no** Microsoft ODBC driver — it is proprietary, and Apache-2.0 carries no
> clause binding whoever pulls a public image to Microsoft's terms. Build the
> opt-in variant yourself, which is also what puts the person accepting
> `ACCEPT_EULA=Y` and the person installing the driver back together:
>
> ```bash
> docker build --target production-mssql -t querygate:mssql .
> ```
>
> Postgres and MySQL need nothing extra. See `docs/CONTAINER_IMAGE_LICENCES.md`.

Then read the admin key it generated:

```bash
docker exec querygate cat /app/var/admin-api-key
```

**Copy it now.** It is written once, mode `0600`, and never regenerated — a
restart, a rebuild and a scaled-out replica all reuse it. There is no writable
secret backend on a fresh install (`env:` is the only registered resolver and is
not writable from a request), so this file is the honest delivery mechanism
rather than a vault we do not have.

The volume is not optional. Without it the admin key, the entitlement cache and
the clock mark live in the container's writable layer and vanish on the next
`docker run` — which looks like "my key stopped working".

## Docker Compose

```yaml
services:
  querygate:
    image: ghcr.io/<your-org>/querygate:latest
    ports: ["8000:8000"]
    volumes: ["querygate-var:/app/var"]
    restart: unless-stopped
volumes:
  querygate-var:
```

## Kubernetes

```yaml
env:
  - name: DATABASE_URL_MYDB
    valueFrom:
      secretKeyRef: { name: querygate-db, key: url }
volumeMounts:
  - { name: var, mountPath: /app/var }
volumes:
  - name: var
    persistentVolumeClaim: { claimName: querygate-var }
```

A **`ReadWriteOnce` PVC per replica**, or a `ReadWriteMany` volume shared by all
of them. Both work: first boot is create-if-absent, so replicas racing on a
shared volume converge on one key rather than overwriting each other.

---

## What the image does *not* do

Worth stating, because the previous behaviour was the reverse and someone may
remember it:

- **No anonymous access.** The image sets `HARDENED_IMAGE=1`, which removes the
  local-development authentication bypass. Before that, `docker run …` produced a
  deployment where every request resolved to an anonymous principal, with the
  OpenAPI schema public and FastAPI's debug page on.
- **Setting `ENVIRONMENT=development` does not bring it back.** Log level and
  security posture are separate switches on purpose.
- **No OpenAPI schema and no `/docs`** are served from the image, whatever the
  environment.

---

## Add your first connection

Connection strings are **environment-variable references, never literals**:

```bash
docker run -d --name querygate \
  -p 8000:8000 -v querygate-var:/app/var \
  -e DATABASE_URL_MYDB='postgresql+asyncpg://readonly@db.internal/app' \
  ghcr.io/<your-org>/querygate:latest
```

Then in `/app/var/connections.yaml`:

```yaml
connections:
  mydb:
    dialect: postgresql
    connection_string: ${DATABASE_URL_MYDB}   # a reference, not a DSN
    enabled: true
```

A literal DSN here is rejected at the boundary and never persisted. The reason is
specific: the config-version store returns this file verbatim under a **read**
scope, so a literal would launder a credential through an untyped string that
`test_credential_redaction.py` cannot see.

**Use a read-only database role.** QueryGate governs what a query may be; it does
not replace the database's own permissions, and the two together are the posture.

## Name what an agent may see

In `/app/var/policy.yaml`:

```yaml
connections:
  mydb:
    require_explicit_allowlist: true
    allowed_tables: [customers, orders]
    allowed_columns:
      customers: [id, name, created_at]    # note: NOT email, NOT password_hash
      orders: [id, customer_id, total, placed_at]
```

Anything not named is denied. That includes columns: a table in
`allowed_tables` with no `allowed_columns` entry exposes nothing.

## Check it

```bash
curl -H "Authorization: Bearer $(docker exec querygate cat /app/var/admin-api-key)" \
     -X POST http://localhost:8000/api/v1/mydb/query \
     -H 'Content-Type: application/json' \
     -d '{"from_table":"customers","select":["customers.id","customers.name"],"limit":5}'
```

A query naming `customers.email` returns a policy rejection, not rows. That is
the install working, not failing.

---

## Subscription

Off by default; QueryGate runs unmetered until you turn it on. To activate:

```bash
-e SUBSCRIPTION_ENABLED=true \
-e SUBSCRIPTION_ENDPOINT=https://licence.yourdomain.com/v1/entitlement \
-e SUBSCRIPTION_ORG_ID=org_… \
-e SUBSCRIPTION_DEPLOYMENT_ID=dep_… \
-e SUBSCRIPTION_DEPLOYMENT_KEY=qgdk_… \
-e SUBSCRIPTION_ROOT_PUBLIC_KEY=… \
-e SUBSCRIPTION_KEY_MANIFEST_FILE=/app/var/key-manifest.json \
-e SUBSCRIPTION_CACHE_FILE=/app/var/entitlement.json \
-e SUBSCRIPTION_CLOCK_MARK_FILE=/app/var/clock-mark
```

`querygate-license status` shows where it stands. Two things worth knowing before
you enable it:

- **A failed licence check never blocks a query.** Only the paid term *plus* a
  grace window elapsing does, and roughly 45 days of visible warnings precede it.
- **New deployments are issued in `observe` mode**, so the gate runs and counts
  but refuses nothing until the control plane says otherwise.

### Renewal warnings

You will not be surprised by an expiry. When a subscription is set not to renew —
cancelled, or with a payment that failed — and under 30 days remain, three things
happen:

- a **persistent banner** appears in both `/admin` and `/access`, with the day
  count and the exact date, escalating under 7 days. Anyone with any credential
  sees it, including analysts with no admin scope: they are the ones whose
  queries stop.
- `querygate-license status` carries the same countdown, from the same function,
  so the number in a support ticket is the number the operator saw.
- `/health` always carries `"subscription"`, which moves from `"ok"` to
  `"renewal_due"` and then `"expired"`; `querygate_subscription_signal{state=...}`
  carries the same three values.

**Those last two carry no date and no day count, on purpose.** `/health` is
unauthenticated for readiness probes and `/metrics` may be configured
unauthenticated, and a countdown on either would tell anyone who can reach the
port when this gateway stops serving.

An expired subscription is **not** reported as unhealthy. The process is fine and
is refusing on a billing decision; a 503 would make Kubernetes restart the pod in
a loop.

The renewal email comes from the licence service, not from your deployment —
QueryGate has no SMTP client and no address to send to, and adding one would put
an outbound mail path inside your network.

See [the licensing FAQ](the licensing FAQ) for what is transmitted (four fields)
and what never is.
