# QueryGate partner demo

A live, four-act demonstration: an AI agent runs wild against a read-only
database; a 51-line policy file stops the same agent cold, with the refusals
never reaching the database at all; and every decision lands in a hash-chained
audit ledger you can attack on stage and watch detect the attack.

## Start here

```bash
make pitch-up          # brings up everything, seeds if needed
make pitch-selfcheck   # asserts the demo's claims are actually true right now
open http://127.0.0.1:8900
```

Then read **[RUNBOOK.md](RUNBOOK.md)** — the talk track, the numbers, the hard
questions and how to answer them honestly, and what to do if something breaks.

Panic button: `make pitch-kill`. Health: `make pitch-status`.

On-stage proofs, both about ten seconds:
- `make pitch-prove-readonly` — the agent's role really is read-only, at both
  the session-flag and the GRANT layer.
- `make pitch-prove-audit` — the audit ledger really is tamper-evident: edit,
  delete and truncate are each attacked live and each caught.

## What's in here

| Path | What it is |
|---|---|
| [RUNBOOK.md](RUNBOOK.md) | **The thing you hold while presenting.** |
| [SPEC.md](SPEC.md) | The build contract, plus amendments recording what measurement disproved. |
| [db/](db/) | Dedicated Postgres on :5544 — 2M rows, three roles, `agent_ro` genuinely read-only. |
| [baseline_mcp/](baseline_mcp/) | The counter-example: a naive `execute_sql` MCP server on :8811. |
| [config/](config/) | QueryGate's demo connection + **policy.demo.yaml** (Act 2), and the measured evidence in `VERIFIED.md`. |
| [control/](control/) | The control UI and its backend on :8900. |
| [agent/](agent/) | Live-agent encore — real Claude session wired to both servers. |
| [slides/](slides/) | One-page answers to the questions that come up in the room (today: why not a database firewall). |

## The three numbers

| | measured |
|---|---|
| Policy refusal | **under 3 ms** (median 2.6 ms) |
| Audit records a gate-OFF action leaves behind | **0** — no trace of who asked, or why |
| Queries your database ran during a refusal | **0** (read from `pg_stat_statements`, not asserted) |
| App-query slowdown while an ungated agent fans out 20 queries | **~10-30×** (~10 ms → 100-300 ms, 27 backends pinned) |

## Ground rules this demo holds itself to

- **Nothing is simulated.** Every scenario makes a real MCP `tools/call` to a
  real server. If a component is down the UI shows an error, never a plausible
  fake success. The *requests* are scripted rather than model-chosen — see
  [agent/](agent/) to run the same beats with a live model driving.
- **Same database, same read-only user, both halves.** The only variable is
  whether the request goes through QueryGate.
- **All PII is synthetic by construction** — reserved-invalid SSNs (900-999),
  `@example.invalid` emails, `555-01xx` phones. A screenshot can never be
  mistaken for a real leak.
- **The audit ledger is tamper-evident, not tamper-proof**, and the demo says
  so out loud. Editing or deleting a record breaks the chain at a named
  sequence number; truncating the tail passes an isolated check and is caught
  only against an externally anchored head hash. That limit is demonstrated
  deliberately rather than hidden.
- **`baseline_mcp/` is not part of the product.** It is deliberately unsafe,
  lives outside the `querygate` package, refuses to start without an explicit
  unsafe-mode env var, and will only connect to the throwaway demo database.
  It never ships in the wheel, sdist, or container image.
