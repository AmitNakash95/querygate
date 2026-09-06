<p align="center">
  <img src="src/querygate/admin_ui/logo-wordmark.svg" alt="QueryGate" width="440">
</p>

<p align="center">
  <strong>Give AI agents real access to your database. Without giving them SQL.</strong>
</p>

<p align="center">
  <a href="https://github.com/AmitNakash95/querygate/actions/workflows/ci.yml"><img src="https://github.com/AmitNakash95/querygate/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://github.com/AmitNakash95/querygate/actions/workflows/codeql.yml"><img src="https://github.com/AmitNakash95/querygate/actions/workflows/codeql.yml/badge.svg?branch=main" alt="CodeQL"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/licence-Apache--2.0-blue.svg" alt="Apache-2.0"></a>
  <img src="https://img.shields.io/badge/python-3.11-blue.svg" alt="Python 3.11">
  <img src="https://img.shields.io/badge/tests-4.6k-brightgreen.svg" alt="4.6k tests">
  <img src="https://img.shields.io/badge/adversarial%20suite-704-brightgreen.svg" alt="704 adversarial tests">
  <img src="https://img.shields.io/badge/coverage-92%25-brightgreen.svg" alt="92% coverage">
</p>

---

An AI agent that can query your production database is enormously useful and
enormously dangerous. The usual answer is to hand it a SQL string and hope —
then bolt on a scanner that tries to recognise the bad ones.

**QueryGate removes the string.** An agent submits a structured query plan — a
JSON AST — describing *what it wants*. QueryGate validates every table and
column against your live schema, enforces policy, compiles it to parameterised
SQL itself, and runs it under guardrails. There is no code path, REST or MCP,
that accepts SQL text. Injection isn't blocked; it has nowhere to live.

```jsonc
// This is what an agent sends. There is no other way in.
{
  "from_table": "orders",
  "select": ["orders.id", "orders.total_amount"],
  "where": { "col": "orders.status", "op": "eq", "value": "shipped" },
  "limit": 100
}
```

## Why QueryGate, and not the alternatives

"No raw SQL" is the foundation, not the pitch. It's what the foundation makes
*possible* that other designs structurally cannot do — because once a product
accepts a SQL string, it can only inspect text.

**Policy governs the *shape* of a query, not just table access.**
Cap joins, subquery depth, GROUP BY width, returned rows, and execution time.
Set a **minimum group size** so an aggregate can't be sliced down to identify one
person. You cannot enforce "at most 3 joins and never fewer than 5 rows per
group" on a string you didn't build.

**Columns can be denied *or masked* — the real value never leaves the database.**
`email` invisible. `phone` returned as last-4. `national_id` one-way hashed.
Deny and mask are different rules, and both are enforced during compilation.

**The *human* behind the agent reaches policy and audit.**
Delegated identity (RFC 8693) carries the real person through the agent into the
applied policy *and* into both identities on every audit record. Not "an agent
did this" — "Dana's agent did this, under Dana's permissions".

**The audit trail can't become a second data leak.**
Persisted events never contain SQL, predicate values, rows, exceptions, or
credentials — by construction, not by redaction pass. Optionally hash-chained
and tamper-evident, with portable per-query receipts you can verify offline.

**Writes are previewed, not hoped over.**
See the exact diff and blast radius *before* execution, behind an approval gate.
No stored procedures, no arbitrary DML strings.

**Expensive queries are refused before they touch data.**
Cost is estimated up front (Postgres `EXPLAIN`, MSSQL `SHOWPLAN`), so a runaway
plan is rejected rather than discovered by your on-call.

**It points at your live operational database.**
No warehouse to buy, no ETL, no mandatory semantic-modelling step. Postgres,
MSSQL and MySQL, each verified against a real server in CI.

**It never calls home.**
No telemetry, no licence check, no update ping. That's enforced by
[`test_no_phone_home.py`](tests/security/test_no_phone_home.py), which fails if
any module gains one — not promised in a privacy policy.

## Quick start

**1. Run it,** passing your database URL as an environment variable:

```bash
docker run -d --name querygate \
  -p 8000:8000 -v querygate-var:/app/var \
  -e DATABASE_URL_MYDB='postgresql+asyncpg://readonly@db.internal/app' \
  ghcr.io/amitnakash95/querygate:latest
```

**2. Take the admin key** it generated on first boot — written once, mode 0600:

```bash
docker exec querygate cat /app/var/admin-api-key
```

**3. Name the connection** in `/app/var/connections.yaml`. Connection strings are
environment-variable *references*, never literals — a literal DSN is rejected at
the boundary and never persisted:

```yaml
connections:
  mydb:
    dialect: postgresql
    connection_string: ${DATABASE_URL_MYDB}
    enabled: true
```

**4. Say what agents may see** in `/app/var/policy.yaml`. A fresh install reaches
**nothing** until you do — deny-by-default is the shipped posture, not an option
you have to find:

```yaml
connections:
  mydb:
    require_explicit_allowlist: true
    allowed_tables: [customers, orders]
    allowed_columns:
      customers: [id, name, created_at]   # note: NOT email, NOT password_hash
      orders: [id, customer_id, total, placed_at]
```

Anything unnamed is denied — including columns. A table in `allowed_tables` with
no `allowed_columns` entry exposes nothing.

**5. Query it:**

```bash
curl -X POST http://localhost:8000/api/v1/mydb/query \
  -H "Authorization: Bearer $(docker exec querygate cat /app/var/admin-api-key)" \
  -H 'Content-Type: application/json' \
  -d '{"from_table":"customers","select":["customers.id","customers.name"],"limit":5}'
```

> **Use a read-only database role.** QueryGate governs what a query may *be*; it
> does not replace your database's own permissions. The two together are the
> posture.

Connecting to **MSSQL**? Build the opt-in variant — the default image ships no
proprietary driver:
`docker build --target production-mssql -t querygate:mssql .`

Full walkthrough: **[docs/INSTALL.md](docs/INSTALL.md)** · Running it for real:
**[deploy/](deploy/)**

## What it deliberately does not do

These are permanent design choices. They're why the guarantees above hold, and
they will not be added:

- **No raw-SQL mode** — not as a flag, an admin escape hatch, or a "power user" tool
- **No execution of model-generated code**
- **No stored-procedure or arbitrary-procedural-SQL path**
- **No second query interface** (GraphQL included) — a second path is a path *around* the gate
- **No mandatory semantic-modelling step** — it reads your live schema
- **No warehouse or query engine of its own**

Honest about the rest, too: **[docs/LIMITATIONS.md](docs/LIMITATIONS.md)** lists
what's missing, unproven, or weaker than you might assume — including that no
independent penetration test has been performed.

## Documentation

| I want to… | Read |
|---|---|
| Understand the design | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · [docs/PRODUCT_GUIDE.md](docs/PRODUCT_GUIDE.md) |
| Review it as a security engineer | [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) · [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md) · [docs/INFERENCE_RISKS.md](docs/INFERENCE_RISKS.md) |
| See every capability | [docs/FEATURE_REFERENCE.md](docs/FEATURE_REFERENCE.md) |
| Know what's missing | [docs/LIMITATIONS.md](docs/LIMITATIONS.md) |
| Deploy it | [docs/INSTALL.md](docs/INSTALL.md) · [deploy/](deploy/) |
| Check the benchmarks | [docs/benchmarks/](docs/benchmarks/) |
| Report a vulnerability | [SECURITY.md](SECURITY.md) |

## Contributing

Contributions are welcome — read **[CONTRIBUTING.md](CONTRIBUTING.md)** first.
It includes an honest list of what *won't* be accepted (a raw-SQL path leads it),
so you don't write code that can't be merged.

There is **no CLA**. Apache-2.0 already grants the patent rights a CLA would ask
for, and licenses your contribution on the project's own terms. Opening a pull
request is the whole agreement.

The working agreement this project holds itself to — including mutation-testing
every enforcement point and rating its own work honestly — is in
**[CLAUDE.md](CLAUDE.md)**.

## Licence

**[Apache-2.0](LICENSE).** Free to use, modify and redistribute, including
commercially and inside closed-source products.

The policy engine and the audit ledger **will never be tier-gated** — selling
security as an upsell on a security product makes the free tier the insecure
tier. **[COMMERCIAL.md](COMMERCIAL.md)** states what is free forever, what is
planned as a paid service, and why.
