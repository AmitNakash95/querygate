# What is free, what is paid, and what will never be either

This document exists so nobody has to guess, and so a future version of this
project cannot quietly change the answer without the change being visible in
this file's history.

## Everything in this repository is free, forever

QueryGate — the gateway itself — is licensed under Apache-2.0. That covers the
whole enforcement product:

- the structured query and write AST, and every validator behind them;
- **the policy engine**, including query-shape caps, table/column deny-by-default,
  and min-group-size;
- **the audit ledger**, including the hash-chained tamper-evident mode, portable
  receipts, and `querygate-audit verify`;
- per-human attribution and delegated identity (RFC 8693 actor→policy);
- the compiler and every dialect adapter;
- governed writes, preview/diff, and approval;
- the admin and access consoles;
- the MCP server and the REST API.

**The policy engine and the audit ledger will never be tier-gated.** Selling
security as an upsell on a security product is a bad trade for everyone: it
makes the free tier the insecure tier, which is precisely the outcome this
product exists to prevent. If you ever see those two behind a paywall, this
paragraph is the commitment that was broken.

There is no licence key, no activation step, no entitlement check, and **no
outbound call to us from anywhere in the shipped source** — no telemetry, no
update check, no usage reporting. That is enforced by a test, not by a promise:
`tests/security/test_no_phone_home.py` fails if any module in `src/querygate/`
gains one.

## What is planned as a paid service

Nothing below exists yet. Each is listed so the funding model is legible from
day one, not so it can be sold to you today.

### QueryGate Notary — third-party anchoring of the audit chain head

The hash-chained ledger is tamper-evident **relative to an externally anchored
head**. That limitation is documented honestly in `docs/THREAT_MODEL.md` and
`docs/SECURITY_POSTURE.md`, and it is real: unkeyed, an operator with write
access to the ledger file can rewrite history and re-derive a consistent chain
from genesis.

Notary closes that gap by attesting to the chain head, at a point in time,
independently of the operator who could otherwise rewrite it.

**Why this is paid rather than open:** you cannot meaningfully self-host it. An
anchor you run yourself is your own infrastructure attesting to your own logs,
which proves nothing to a third party. Third-partyness *is* the product. That
is a structural argument, not a packaging decision — the same reason the
gateway's safety comes from having no raw-SQL path rather than from scanning
strings.

When it exists, it will receive **chain head hashes only** — no queries, no
rows, no schema, no credentials — and it will be asynchronous and never in the
request path. An audit subsystem that depends on a vendor's uptime is a
security-review finding, and we would rather not ship one.

### Fleet management

Policy bundles distributed to many self-hosted deployments, and cross-fleet
audit aggregation. A single deployment does not need this and never will; an
estate of thirty does. Nothing is removed from the open product to create the
need.

### Support, indemnification, escrow, and security-review assistance

Commercial agreements, not code. The software is Apache-2.0 either way.

## What QueryGate will never do

These are product identity, not a roadmap gap, and each is tempting precisely
because some competitor does it:

- **No `execute_sql` or raw-SQL mode.** Not as a flag, not as a power-user
  escape hatch, not "just for admins". The whole safety argument is that no
  caller-controlled SQL string exists to attack. A contribution adding one will
  be declined regardless of quality — see `CONTRIBUTING.md`.
- **No execution of model-generated code.**
- **No stored-procedure or arbitrary-procedural-SQL path.**
- **No mandatory semantic-modelling step** — point it at your live schema.
- **No warehouse or query engine of our own.**
- **No second query interface** (including GraphQL). A second path to the
  database is a second path around the enforcement point.
- **No hosted query execution.** The commercial side may anchor hashes, issue
  entitlements, and distribute policy. It must never execute a query, hold a
  database credential, or receive a row. This is the one item on this list the
  business model itself creates pressure to break, which is exactly why it is
  written down.

## Questions this is meant to pre-empt

**"Is this open-core with the good parts held back?"** No. The enforcement
point — the entire reason to run QueryGate — is Apache-2.0 and complete. The
paid items are a service that cannot be self-hosted without defeating its own
purpose, and fleet tooling a single deployment does not need.

**"Will you relicense once you have adoption?"** The Apache-2.0 grant on every
released version is irrevocable; nothing can retroactively close what has
shipped. If a future version were ever relicensed, the last Apache-2.0 release
would remain forkable by anyone, and this paragraph would be the record of what
was promised.

**"What happens if the project is abandoned?"** You keep running it. There is no
licence server to go dark, no activation to expire, and no call home to fail.
That is a property of the architecture, not a policy we maintain.
