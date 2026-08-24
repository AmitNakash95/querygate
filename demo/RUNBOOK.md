# Partner demo — runbook

Everything you need on stage. Read the "Say this first" box, then run the six
scenarios in order. Total runtime ~8 minutes, or ~4 if you skip to the three
starred beats.

---

## Pre-flight

**Tonight, once:**

```bash
make pitch-up          # ~40s cold, seeds the database if needed
open http://127.0.0.1:8900
```

Click through all six scenarios in both gate positions once, so nothing is a
surprise tomorrow. Then leave it running, or `make pitch-stop` and bring it
back up in the morning (the data survives).

**Five minutes before the meeting:**

```bash
make pitch-status      # five lines: healthy / up / up / up / 0 agent backends
make pitch-selfcheck   # asserts the demo's actual claims, not just liveness
make pitch-deck-check  # asserts the demo deck's own claims (policy values, sourced
                       # figures, no retired numbers) and the deck server's boundary

```

`pitch-status` tells you the services are alive. **`pitch-selfcheck` is the one
that matters** — it runs the real scenarios and asserts that gate OFF genuinely
leaks, that gate ON genuinely refuses citing a rule, that a refusal genuinely
leaves the database at a zero `pg_stat_statements` delta, and that a legitimate
query still succeeds. If it prints anything red, do not present until it is
resolved. If anything says DOWN, `make pitch-up` again — it is idempotent and
starts only what is missing.

**Panic button, memorise this one:** `make pitch-kill` frees the database
instantly, no matter what is running.

---

## Say this first — the setup that makes the whole demo work

> "There is one database here. One read-only user on the agent's path — the
> same role, both halves. Its permissions do not change at any point in this
> demo. The only thing that changes is whether the agent's request goes through
> QueryGate."

Say it before you touch anything. Without it, a partner assumes you tightened
database permissions between the two halves, and the entire demo collapses into
"you turned the padlock on." With it, everything that follows is about
**what the agent is allowed to ask for**, which is your actual product.

If you want to prove the read-only claim on the spot:

```bash
make pitch-prove-readonly
```

It connects as `agent_ro` — the role both MCP servers use — reads 250,000 rows
to show the connection works, then tries INSERT, UPDATE, DELETE and CREATE
TABLE and shows all four refused by the database itself. Takes three seconds.

(Do **not** use `make pitch-db-psql` for this — that opens a shell as the
database *owner*, where a write succeeds. It exists for your own inspection.)

---

## Act 1 — the gate is OFF (this is how it works today)

Toggle reads **QUERYGATE OFF**. The page is in its alarmed register.

The agent is connected to an ordinary Postgres MCP server — an `execute_sql`
tool. Worth naming out loud: *this is not a strawman we built to lose.* Google's
MCP Toolbox ships exactly this in its prebuilt Postgres configuration, and it is
the pattern most "connect an agent to your database" integrations reach for.

Be precise here rather than sweeping: some newer entrants have moved off raw SQL
(Microsoft's Data API Builder, Cube). Claiming "everyone ships `execute_sql`" is
rebuttable by anyone who knows those two, and you do not need the overclaim —
the incumbent default doing it is enough.

### ★ Beat 1 — it reads what it must never read  → scenario `2` (`pii_table`)

The agent asks for every employee's SSN and salary. It gets them.

Let the SSNs sit on screen. Do not narrate over them. Then:

> "Read-only access did nothing here. Read-only protects your data from being
> *changed*. It has never protected it from being *read*. Confidentiality and
> integrity are different problems, and the industry has been shipping a
> solution to the wrong one."

### Beat 2 — table-level thinking is not enough → scenario `3` (`pii_column`)

Customer emails and phone numbers, from a table the agent is *supposed* to
have. The point: "just don't expose that table" is not a real answer, because
the sensitive columns live inside the tables you need.

### Beat 3 — and it can walk out with everything → scenario `4` (`bulk_export`)

The entire customer table. 250,000 rows.

> "No individual query here is malicious. That is the problem — there is nothing
> to detect. This is a legitimate query that a legitimate agent runs for a
> legitimate reason, and it is also a complete customer database walking out of
> the building."

### ★ Beat 4 — it takes the database down → scenario `5` (`overload`)

Watch the health strip at the top. This is a live measurement of what an
ordinary application query costs, sampled continuously.

The agent fans out 20 concurrent queries — which is exactly what agents do when
they retry, or loop over a list, or decide to be thorough.

| | median response time of a normal app query |
|---|---|
| before | **~10 ms** |
| during | **~100-300 ms**, sustained for the whole window |
| after | ~10 ms |

**Roughly 10-30× slower**, with 27 database backends pinned. Every other
application on that database is now crawling. It self-heals after 15 seconds —
the backend terminates the queries the way your on-call engineer would have to
at 2am.

> **Quote the number on the screen, not one from memory.** The multiplier moves
> with how warm the database's cache is. Cold, we measured a single run at
> 606 ms (34x); warm and repeatedly re-run, it settles around 100-300 ms
> (10-30x). It is always a large, obvious, sustained jump — but say what the
> strip is showing, not a figure you memorised. If you want the starkest
> version, the first run after `make pitch-stop && make pitch-up` hits hardest.

> "Still a read-only user. Read-only never protected availability either."

---

## Act 2 — the config (this is the whole product)

Open the config panel. `policy.demo.yaml`, 51 lines.

Read four lines aloud, no more:

```yaml
allowed_tables: [customers, orders, order_items, products]   # employees is not here
denied_columns:  { customers: [email] }
max_limit: 100
allow_cross_join: false
```

> "That is the entire rulebook. It is a file, it lives in your repository, it
> goes through code review, and it is the same file whether you have one agent
> or fifty."

The one point that lands hardest with a technical partner:

> "Notice what is *not* in this file: a list of bad queries to look for. We are
> not pattern-matching SQL strings and hoping we thought of everything. The
> agent cannot send SQL at all — it sends a structured request, and anything
> outside this list is refused before your database is ever contacted. There is
> no SQL string for it to smuggle something past us in."

A precision worth getting right if a technical partner is watching the payload
drawer: a denied request *can* be constructed — you will see it sitting in the
Request pane. What cannot happen is it reaching your database. Say "refused",
not "impossible to express"; the screen will back the first and contradict the
second.

---

## Act 3 — the gate is ON (same database, same user, same questions)

Flip the toggle. The page changes register. Then re-run **the same scenarios**,
in the same order — the fact that you are not typing anything different is the
argument.

### Beat 1 again → `2`

**BLOCKED.** And look at the two numbers:

- **under 3 ms** — the refusal (measured median 2.6 ms)
- **queries executed against your database: 0**

> "It did not run the query and check the results. It did not run the query at
> all. Your database never heard about this request. That number is read live
> from Postgres's own statistics — it is not something we print, it is something
> we measured."

This is the single most important moment in the demo. Give it room.

### Beats 2, 3 → `3`, `4`

Column denied. Export capped at 100 rows. Note the second half of scenario `3`:
`phone` comes back **masked** — the agent gets `0182`, never the real number,
and the masking happens inside the query, so the real value never leaves the
database at all.

### ★ Beat 4 again → `5`

The same 20-way fan-out. All 20 refused in single-digit milliseconds. The health
strip stays **flat**.

> "The policy stopped this twice over — the shape of the query isn't allowed, and
> even if it were, the concurrency cap would have bounded it."

### Beat 5 — the closing number → scenario `6` (`legit`)

A real analytical question: revenue by country, top 10. It runs. Both paths, 15
times each, measured live in front of them.

> "This is a gate, not a padlock. Real analytical work goes straight through.
> You are paying a few milliseconds of validation on a request that takes about
> eighty end to end — and in exchange: the HR table is unreachable, the denied
> column never leaves the database, the cross join is refused outright, and no
> single request can pull more than a hundred rows."

**Read the overhead off the screen — do not quote a number from memory.** It is
a difference between two live medians and it genuinely moves: we have measured
the same scenario at +5.9 ms and at +14.9 ms on the same machine. Single-digit
to mid-teens milliseconds is the honest characterisation, and the screen will
show the real one.

---

## Act 4 — the receipts (do this one if there is a security person in the room)

Everything in Act 3 wrote a record. This act is where "we govern it" becomes
"and here is the proof, which you can check without trusting us."

### Beat 1 — what one record contains

Open the audit panel on the last blocked scenario. Read out three things:

- **who** — the principal, the auth method, the surface it came in on
- **what** — the operation, the connection, and the *shape* of the query
- **the verdict** — `denied`, and which rule decided it

Then the part that matters most:

> "Notice what is *not* in this record. No SQL text. No filter values. No rows.
> No credentials. It proves what was asked and what we decided — without the
> audit log becoming a second copy of the data it was protecting. A log that
> quietly accumulates your customers' data is a liability, not a control."

### ★ Beat 2 — with the gate off, there is nothing at all

Flip to gate OFF, re-run scenario `2`, and look at the same panel.

**Zero records.** The ordinary Postgres MCP server has no audit surface — it ran
a query against your database and left no trace of who asked or why.

> "This is the part people miss. The choice isn't between good logs and better
> logs. Postgres can tell you a query ran. It cannot tell you which human, on
> which agent, under which policy, was refused — because nobody asked it to."

### ★ Beat 3 — attack the ledger, live

Drop to a terminal:

```bash
make pitch-prove-audit
```

It runs in about ten seconds, on a copy, and shows three attacks being caught:

| attack | what happens |
|---|---|
| **edit** a record — turn a refusal into an approval | `BROKEN at seq 15: record hash does not match its contents`, exit 1 |
| **delete** a record | `BROKEN: sequence gap: expected 15, found 16`, exit 1 |
| **truncate** the tail to hide the last few actions | passes on its own — then fails against the anchored head hash |

**Do not skip the third one, and do not gloss it.** It passes the first check on
purpose: a truncated chain is still an internally valid chain. That is the
honest limit of a hash chain, and the tool says so out loud before showing you
that an externally anchored head hash catches it.

> "I'm showing you the limit deliberately. Anyone who tells you their audit log
> is tamper-*proof* is selling you something. This is tamper-*evident*: you
> cannot change it without the change being visible to someone who checks."

That sentence buys more credibility with a security reviewer than any feature
on the list.

### Beat 4 — a receipt they can take away

The last section prints a single `querygate.audit.receipt` — a self-contained,
HMAC-SHA256 JSON object for one event.

> "Hand that to your auditor. They can verify that specific decision happened,
> exactly as recorded, without being given the ledger, the database, or access
> to anything else."

Non-zero exit codes throughout, so this is a CI gate, not a dashboard:

```bash
poetry run querygate-audit verify <ledger> --hmac-key-env AUDIT_LEDGER_HMAC_KEY
```

**If asked about the HMAC key:** without one, the chain is plain SHA256 — an
attacker who can rewrite the file can recompute every subsequent hash and hand
you a perfectly valid chain. The key makes records unforgeable to anyone who
doesn't hold it, which is why in a real deployment it lives somewhere the
database operator cannot read. The demo's key is a throwaway literal in
`env.demo`, and you should say so.

---

## The hard questions, and honest answers

**"Why is that database only 2 CPUs? Isn't that rigged?"**
Yes, it's capped, deliberately, and it's the *more* realistic setting. Production
Postgres is sized to its workload — 2 to 8 vCPU is normal. A dev laptop with 18
idle cores is the unrealistic thing. We measured it both ways; the rationale is
written into the compose file. Offer to show them.

**"Couldn't the agent just... ask for the employees table anyway?"**
It has no way to name it. `list_tables` doesn't return it (run scenario `1` to
show this). Even naming it explicitly is refused before the database is
consulted. Its reality is the allow-list.

**"Isn't `read-only` just a session setting you could turn off?"**
You will only get this from someone genuinely good, and the answer is: partly
yes, and we show it. `ALTER ROLE ... SET default_transaction_read_only = on` is
a session *default* — anyone holding the connection string can override it with
`?options=-c default_transaction_read_only=off`. `make pitch-prove-readonly`
deliberately does exactly that in front of them, and the writes still fail with
**"permission denied for table products"**, because `agent_ro` was never granted
INSERT/UPDATE/DELETE on anything. Read-only here is a privilege, not a setting.

That said, be honest about the shape of the argument: this is why the demo's
point is *not* "read-only is broken." Read-only is doing its job perfectly. It
simply has nothing to say about who may read what, or how expensive a read may
be — which is the entire gap QueryGate fills.

**"Couldn't the agent just loop and page through everything anyway?"**
For the denied column, no — `email` never comes back at all. For the masked
ones, no — it only ever sees the masked form. For the rest: correct, per-query
caps alone do not stop a patient loop. QueryGate ships a per-principal
disclosure budget and a query quota for exactly that; this demo's policy
deliberately leaves them off to keep the file at 51 readable lines. Good
question to get — it means they understand what a row cap is and isn't.

**"Why do we need this if we already have a database firewall?"**
Because a firewall reads the SQL and decides whether to permit it, and we never
hand it a SQL string to read. That's the whole answer; the rest is detail.

State their side fairly or you lose the room. A modern SQL firewall is not just
a denylist guessing at danger — these products run allow-list modes as well as
denylist ones, DAM products can write policy naming a table or a column, and
app-user tracking exists. Concede all three. (If you want to name Oracle SQL
Firewall's training-period allow-list specifically, confirm the mechanism first
— we have no primary-sourced competitor brief for this category yet, so don't
assert a named vendor's internals from this document.) The argument that
survives a prospect who actually runs one:

- **Novelty.** An allow-list assumes traffic that repeats. An agent composes SQL
  it has never sent before on almost every request — so they block legitimate
  work or run permissive. This is the strongest point; lead with it.
- **Shape, not statement.** They allow, deny, alert or substitute a statement.
  We bound joins, nesting depth, row caps, in-query masking and timeout, and
  check every column reference — including one that only appears in a `WHERE`.
- **Identity in the decision.** The database user they see is real — but it is
  usually a pooled app credential, and app-user attribution above it is inferred
  and lands in a log. Ours is carried by delegation into the policy that gets
  applied.

Two things not to say. Do **not** claim we block "earlier" — a good firewall
also stops a statement before it executes; the difference is *what gets judged*.
And do not ask them to remove it: a firewall covers the DBA, app, ETL and
stolen-credential paths, and QueryGate is in none of those.

There's a one-page slide for this — `demo/slides/why-not-a-database-firewall.html`.
Its footnote hedges one row and one thing that isn't a row; hedge both the same
way: per-human attribution is shipped but this demo runs on a static key, and
audit persistence is an operator setting rather than a product default.

**"Where does this run — is it SaaS, or in our infrastructure?"**
Today it is self-hosted: QueryGate runs inside your network, holds the database
credential there, and result rows never leave your infrastructure. That is the
only deployment that exists right now. We are moving to a managed service, and
the self-hosted option stays available through that transition — so a buyer
whose security posture requires it is not stranded.

Say this plainly rather than letting them infer permanence. Two reasons: the
slide deck used to promise data stayed local "permanently" and that is no longer
the plan, and a security-led buyer will build their approval around wherever the
credential lives. Better they hear the roadmap from you now than discover it at
renewal.

If they push on what changes under SaaS: the structural guarantee does not — the
agent still cannot submit SQL, and policy is still enforced by construction. What
changes is where the enforcement point runs and who holds the credential, and
that is exactly the part we would work through with a design partner.

**"What if someone gets the database credentials directly?"**
Then they have the database, and QueryGate is not in that path. Be straight
about this — it governs the *agent* path, which is the path you are about to
open to a non-deterministic system. It is not a replacement for network
controls, credential hygiene, or your existing DBA practice.

**"Is this actually running, or is it a video?"**
`demo/agent/PROMPTS.md` — drop into a real Claude session wired to both servers
and let the model drive. Same database, same two endpoints, model picks its own
tool calls. That's the encore.

**"What does it cost us in latency?"**
Scenario `6`, measured in the room — it runs both paths fifteen times each and
shows you both medians. Expect single-digit to mid-teens milliseconds of
overhead on an ~80 ms request. Don't quote a number from memory; run it and
read it. (The ~2.6 ms figure from Act 3 is a *refusal* latency — a different
measurement. Don't mix them up in the room.)

---

## If something breaks

| Symptom | Do this |
|---|---|
| Database feels stuck | `make pitch-kill` |
| A service says DOWN | `make pitch-up` (idempotent, starts only what's missing) |
| UI shows an error banner | It's telling you the truth — a backend is down. `make pitch-status` |
| Everything is confused | `make pitch-down && make pitch-up` (data survives) |
| Data looks wrong | `make pitch-db-reset` (full reseed, about a minute) |

The UI never fabricates a result. If a backend is unreachable it shows an error
rather than a plausible-looking success — so if you see numbers, they are real.

---

## Afterwards

```bash
make pitch-stop        # stops everything, keeps the data for next time
```

---

## What this demo does not claim

Worth knowing so you don't over-promise in the room:

- It does not prove QueryGate stops someone with direct database credentials.
- **The six scenarios are scripted requests, not model output.** The transport,
  the database, the policy enforcement and every measurement are real — a real
  MCP `tools/call` over HTTP every time. What is ours rather than a model's is
  the *choice* of request. `demo/agent/PROMPTS.md` runs the same beats with a
  live model picking its own tool calls; use that if someone asks.
- `max_limit` bounds a single request, not a campaign. A patient caller paging
  100 rows at a time still accumulates data. The disclosure budget and quota
  that address that are shipped but deliberately not enabled in this policy.
- The "0 queries" result is specific to **policy-level** refusals — which is
  every scenario here. A query that passes policy but references a column that
  doesn't exist is caught slightly later, after schema reflection.
- All PII shown is synthetic by construction: SSNs in the reserved-invalid
  900-999 range, `@example.invalid` emails, `555-01xx` phones. If someone
  screenshots your demo, nothing real is in it.
