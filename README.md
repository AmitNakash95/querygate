<p align="center">
  <img src="landing/assets/logo-wordmark.svg" alt="QueryGate" width="520">
</p>

**QueryGate is an agent-safe database access gateway.** It lets you expose a
Postgres, MSSQL, or MySQL database to AI agents over MCP and REST — without
ever letting them run raw SQL.

> **QueryGate runs inside your infrastructure.** It dynamically discovers
> your schema, exposes policy-controlled MCP and REST tools, limits query
> complexity and database load, and keeps credentials and data inside your
> network.

Agents submit a structured, schema-checked query plan (a JSON AST), not a SQL
string. QueryGate validates every table and column against the live
reflected schema, enforces a per-connection policy (allow/deny lists,
complexity caps, row limits, timeouts), compiles the plan to parameterized
SQL through SQLAlchemy Core, executes it under a concurrency guardrail, and
returns a bounded result set. There is no code path — REST or MCP — that
accepts a SQL string.

## Project maturity — read this before you evaluate

QueryGate is a **pre-1.0 project with no commercial track record**. Everything
below is verifiable from this repository; none of it is buried further down.

- **No paying customers, and no reference deployments.** The project's own
  definition of success is one paid design partner whose security team signs
  off; that has not happened yet.
- **No independent security audit, penetration test, or compliance
  certification.** The threat model, the adversarial regression suite
  (`make test-security`) and the security benchmark are all **first-party**.
  An external audit is a known, open, funded-by-nobody item (`TODO.md` item
  53). Treat this repository's security claims as *testable*, not as
  *attested* — and please do test them.
- **One tagged version, `0.1.0`, and nothing published yet.** The signed,
  provenance-attested release pipeline (cosign keyless + SLSA) is built and
  CI-exercised, but **no release has yet been cut through it**, and there is
  no PyPI upload step at all. You install by cloning, not by `pip install
  querygate`. See [`docs/RELEASING.md`](docs/RELEASING.md).
- **No SLA and no commercial support.** See [`SUPPORT.md`](SUPPORT.md) for
  what that means in practice.
- **Self-hosted only.** There is no hosted QueryGate, and running one on
  someone else's behalf is the one thing the intended licence will not permit.
- **The licence is a draft that is not in force.** Until the banner at the top
  of [`LICENSE`](LICENSE) is removed, QueryGate is proprietary and all rights
  are reserved. See [Licence](#licence).
- **Three dialects are live-verified; two are not.** Postgres, MSSQL and MySQL
  run against real servers in CI. Snowflake and BigQuery are
  compiler/rendering-level only — QueryGate deliberately **refuses to open a
  connection** to either rather than pretending to support them.

What *is* real: the structural guarantee below; live Postgres/MSSQL/MySQL tiers
in CI; a maintained threat model; and a reconciliation between what the docs
claim and what the code does that is itself gated in CI. The adversarial
security suite is 651 tests (`make test-security`); the unit and integration
suites are 3,326 and 428. Every number there is reproducible — `poetry run
pytest -m unit -q`, `-m "integration and not real_db"`, `make test-security` —
and the adversarial count is itself CI-gated against the documents that quote
it, this file included. The full, unabridged list of what is missing or partial
is [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md).

## Why raw SQL for agents is dangerous

Handing an LLM a `run_sql(query: str)` tool means trusting a probabilistic
text generator to never produce `DROP TABLE`, never wander into a table it
shouldn't see, never write an unbounded cross join that takes your database
down, and never leak a credential in a stack trace. Prompt injection makes
this worse — a malicious document an agent reads can suggest SQL for it to
run just as easily as a user can. None of the usual mitigations (asking the
model nicely, read-only DB users, query timeouts alone) are structural
guarantees:

- A read-only DB user still lets an agent read every table you didn't mean
  to expose, run an unindexed 12-way join, or exfiltrate an entire table in
  one `SELECT *`.
- Prompt-level instructions ("only query the `orders` table") are guidance,
  not enforcement — nothing stops the next prompt, the next model, or an
  injected instruction from ignoring them.
- A query timeout limits *duration*, not *scope* — it doesn't stop a query
  from touching a table or column it should never have reached at all.

QueryGate's structural guarantee: **the only thing an agent can submit is a
`StructuredQuery` object.** It's a Pydantic model with a fixed shape — no
`sql` field exists anywhere in the schema, so there's no field to inject
into. Every table/column reference in it is checked against the real,
live-reflected schema and an explicit policy before a single SQL statement
is compiled. A bad query gets a validation error before it ever reaches the
database, not a raw error message from the database itself.

## How QueryGate differs from generic MCP SQL connectors

A lot of "MCP + database" integrations are a thin wrapper around
`cursor.execute(model_generated_sql)`, sometimes with a read-only role and a
row cap tacked on. QueryGate is structurally different:

| | Generic MCP SQL connector | QueryGate |
|---|---|---|
| What the agent submits | A SQL string | A validated AST (`StructuredQuery`) |
| Table/column safety | Whatever the DB role allows | Explicit allow/deny policy, checked before compilation |
| Query shape limits | Usually none | Max joins, where-depth, select width, group-by, top-N — policy-enforced |
| Row limits | Often just `LIMIT` appended, sometimes bypassable | Server-clamped, tiered by query shape (aggregate vs. row select) |
| Multi-database support | One connection string, hardcoded | Dynamic connection registry, credential-isolated from schema/tool responses |
| Concurrency/load control | Rare | Per-connection concurrency semaphore + execution timeout |
| Rate limits / cost budget | DIY | Per-principal rolling-window request & response-byte quotas (429 + `Retry-After`) |
| Multi-tenant scoping | DIY | Policy-level `mandatory_row_filters` |
| Column-level masking | DIY (or none) | Per-principal `column_masks` (hash/null/last-N/bucket), applied in the compiled SQL |
| Audit trail | Rare | Every query logged plus an optional persisted, redaction-safe JSONL event — optionally a tamper-evident hash-chained ledger with per-query receipts |

## Quickstart

Everything below was run end to end from a clean checkout on 2026-08-21; it
takes about five minutes, most of which is `poetry install`.

```bash
poetry install
cp .env.example .env
docker compose up -d          # demo Postgres on :5433 (auto-seeded) + the Redis limiter
```

**Set an API key before you start the server.** `.env.example` ships
`ENVIRONMENT=localhost` and `API_KEYS='[]'`, and with no key configured **the
REST API accepts unauthenticated calls** — convenient for a first look, wrong for
anything else, and it leaves you with no token to give the quickstart command
below.

That anonymous path is gated, not accidental: it is only reachable when the
environment is local *and* no API key *and* no JWT issuer *and* no SSO is
configured (`api/auth.py`'s `build_authenticator`, and its sibling in
`mcp/auth.py`), and `ENVIRONMENT=production` **refuses to start** without
`API_KEYS` or `JWT_ENABLED` (`core/config.py::_validate_production_auth`). You
cannot ship this state by accident — but you should not browse in it either.
Edit `.env`:

```bash
API_KEYS='["local-dev-key"]'   # any string; this is a local demo credential
```

Then start it:

```bash
poetry run python -m querygate.run
```

The example environment selects `CONCURRENCY_BACKEND=redis` and connects to
the Compose service at `redis://localhost:6379/0`, so concurrency limits are
shared across multiple local QueryGate processes. If you intentionally run
without Compose, set `CONCURRENCY_BACKEND=in_process`; that mode is suitable
for a single QueryGate process only.

For development with automatic reload, use `make dev` (or its longer alias,
`make run-dev`). `make run dev` is interpreted by Make as two separate
targets and is not the development-server command.

### Your first governed query

In a second shell, from the repository root:

```bash
export QUERYGATE_URL=http://localhost:8000
export QUERYGATE_TOKEN=local-dev-key      # whatever you put in API_KEYS
poetry run querygate-quickstart demo
```

`querygate-quickstart` (TODO.md item 146) reflects a connection's schema and
prints 3 ready-to-run example queries — a plain select, a filtered select,
and an aggregate — scoped to columns the catalog doesn't mark sensitive, each
with a REST `curl`, an MCP tool-call, and a Python SDK snippet. It composes
existing read-only discovery routes; it never writes anything. Run the `curl` it
prints and you get real rows back:

```bash
curl -s -X POST "$QUERYGATE_URL/api/v1/demo/query" \
  -H "Authorization: Bearer $QUERYGATE_TOKEN" -H "Content-Type: application/json" \
  -d '{"from": "customers", "select": ["customers.id", "customers.name"], "limit": 10}'
```

Now try to send SQL instead, and watch there be no field to put it in:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$QUERYGATE_URL/api/v1/demo/query" \
  -H "Authorization: Bearer $QUERYGATE_TOKEN" -H "Content-Type: application/json" \
  -d '{"sql": "SELECT 1"}'
# 422 — `StructuredQuery` is extra="forbid" and has no SQL-string field
```

Two browser surfaces are also live: [`/admin/`](http://localhost:8000/admin/)
(operator control plane, needs an admin scope to show anything useful) and
[`/access/`](http://localhost:8000/access/) (what a non-admin caller may see).
The offline product guide answers configuration questions without leaving the
process: `curl "$QUERYGATE_URL/api/v1/help/search?q=configure+policy"`.

## Architecture at a glance

```
Agent (MCP) / Client (REST)
        │  StructuredQuery JSON — never SQL
        ▼
┌───────────────────────────────────────────────────────────────┐
│ querygate/execution/service.py  (StructuredQueryService)       │
│                                                                  │
│  1. validation/policy_validation.py  — caps + allow/deny        │
│  2. validation/schema_validation.py  — reflect + verify exists  │
│  3. compiler/sqlalchemy_compiler.py  — AST → SQLAlchemy Select  │
│  4. execution/concurrency.py         — per-connection semaphore │
│  5. connections/engine.py            — session + guardrails     │
│  6. audit/                           — stdout + persisted JSONL  │
│                                         event, timing, outcome    │
└───────────────────────────────────────────────────────────────┘
        │
        ▼
  Real Postgres / MSSQL / MySQL database
```

Every REST route and every MCP tool is a thin wrapper over that one pipeline —
there is no second path to a database. The package-by-package map is
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md); the reasoning behind it is
[`docs/PRODUCT_GUIDE.md`](docs/PRODUCT_GUIDE.md).

## Security model, in seven lines

- **No raw SQL, anywhere.** `StructuredQuery` has no SQL-string field and
  rejects unknown fields (`extra="forbid"`) — there is no field to smuggle SQL
  into, and no endpoint that would accept it if there were.
- **Credentials never leave `connections/`.** Only `PublicConnectionInfo` is
  returned from REST/MCP; `tests/unit/test_credential_redaction.py` asserts
  that against the live OpenAPI and MCP schemas, not by convention.
- **Policy is enforced before compilation**, not as a post-hoc filter.
- **Bring your own identity, for people as well as agents.** Humans sign in
  through your IdP — nineteen named presets (Entra ID, Okta, Auth0, Google Workspace,
  Keycloak, AD FS, Cognito, Cloudflare Access, Ping, OneLogin, JumpCloud,
  authentik, ZITADEL, Authelia, WorkOS, FusionAuth, Salesforce, GitLab, …) plus
  any OIDC issuer by name — or through a built-in local provider when there is
  no IdP to reach. Group membership maps to QueryGate scopes through a
  reviewable file that is **deny-by-default** and re-evaluated on every request,
  so tightening it takes effect immediately rather than at the next logout. A
  terminal gets the same identity with `querygate-login` (RFC 8628), never a
  shared key. See [`docs/SCOPE_CATALOG.md`](docs/SCOPE_CATALOG.md) and
  `examples/identity.example.yaml`.
  *Evaluating? `DEV_IDP_ENABLED=true` makes QueryGate serve its own OIDC
  provider so the whole sign-in flow runs with nothing to register — refused
  outside a local environment.*
- **Every identifier is schema-checked** against the live reflected schema,
  never agent-asserted.
- **Bounded execution** — per-connection concurrency semaphore, policy
  timeout, server-clamped row counts.
- **Redaction-safe audit** — every attempt is logged; the persisted event
  never contains SQL, predicate values, rows, exceptions, or credentials.
- **Adversarially tested, and continuously scanned** — `make test-security`,
  plus deny-by-default SAST, dependency/SBOM, secret, container and OpenAPI
  fuzzing gates in CI.

The unabridged version, with the enforcement point for each,
is [`docs/SECURITY_MODEL.md`](docs/SECURITY_MODEL.md). To report a
vulnerability, see [`SECURITY.md`](SECURITY.md).

## What it deliberately does not do

These are design decisions, not gaps, and they are not up for negotiation
without a recorded decision:

- **No raw-SQL mode**, no `execute_sql` field, endpoint, or MCP tool.
- **No execution of model-generated code.**
- **No stored-procedure pass-through** — exposing stored procedures safely
  needs its own catalog and approval mechanism, which is out of scope here.
- **No mandatory semantic-modelling step** before you can ask a question.
- **No warehouse of our own** — QueryGate governs the operational database you
  already run; your data never moves.

And the honest list of what is *partial* — RBAC depth, cost estimation
coverage, opt-in WORM audit retention, in-process async execution, the two
non-live-verified dialects — is [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md).

## Where to go next

[`docs/README.md`](docs/README.md) is the map of everything in `docs/`. The
short version:

| If you want to… | Read |
|---|---|
| See every capability, with request/response examples | [`docs/FEATURE_REFERENCE.md`](docs/FEATURE_REFERENCE.md) |
| Understand the design and the tradeoffs behind it | [`docs/PRODUCT_GUIDE.md`](docs/PRODUCT_GUIDE.md) |
| Review this as a security engineer | [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md), [`docs/SECURITY_POSTURE.md`](docs/SECURITY_POSTURE.md), [`docs/INFERENCE_RISKS.md`](docs/INFERENCE_RISKS.md) |
| Know exactly what is missing | [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) |
| Deploy it for real | [`deploy/README.md`](deploy/README.md), [`deploy/runbook.md`](deploy/runbook.md) |
| Contribute | [`CONTRIBUTING.md`](CONTRIBUTING.md), [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) |
| Get help, or find out what is supported | [`SUPPORT.md`](SUPPORT.md) |
| Report a vulnerability | [`SECURITY.md`](SECURITY.md) |
| Understand the licence | [`LICENSE`](LICENSE), [`docs/LICENSING_FAQ.md`](docs/LICENSING_FAQ.md) |

Historical extraction notes are kept outside the product surface under
`archive/extraction/`.

<p align="center">
  <img src="landing/assets/favicon.svg" alt="QueryGate app icon" width="64">
</p>

## Licence

QueryGate **will be** licensed under the **Business Source License 1.1**,
converting to Apache-2.0 four years after each release. `LICENSE` currently holds
that text as a **draft that is not yet in force** — until its banner is removed,
QueryGate remains proprietary and all rights are reserved.

The headline of the intended grant: **internal production use is free, forever,
for every version released under it, with no limit** — no user cap, no database
cap, no seat count. The only restriction is providing QueryGate itself to third
parties on a hosted, managed, or embedded basis, with a carve-out for an MSP
running it on a single licensee's behalf.

- [`LICENSE`](LICENSE) — the draft terms
- [`docs/LICENSING_FAQ.md`](docs/LICENSING_FAQ.md) — what it means in practice
- [`docs/THIRD_PARTY_LICENSES.md`](docs/THIRD_PARTY_LICENSES.md) — every dependency's licence

None of this is legal advice, and the terms are not settled until reviewed.
