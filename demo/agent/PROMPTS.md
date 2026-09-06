# Live-agent backup path — exact prompts to type

Use this when a partner asks *"is this actually real, or is it a slideshow?"*
It is the same demo, driven by a real Claude session over real MCP, with the
model choosing its own tool calls.

**Setup (do this before the meeting, not during it):**

```bash
make pitch-up          # database + both servers
cd demo/agent && claude
```

Claude Code reads `mcp.claude-code.json` from the directory you launch it in.
Confirm both servers are connected with `/mcp` before you present — you should
see `unsafe-postgres` and `querygate` listed. If you prefer Claude Desktop,
merge `claude_desktop_config.snippet.json` instead and warm the npx cache once
while you still have network.

Both servers are pointed at the **same database** as the **same read-only
database user**. Say that out loud before you start — it is the whole argument.

---

## Beat 1 — the agent reads what it should never see

> Using only the **unsafe-postgres** tools, list every employee with their
> social security number and salary.

The model writes its own SQL and gets the rows. Let the SSNs sit on screen for
a beat before you say anything.

Follow-up worth doing, because it lands harder than the first:

> That database user is read-only. Explain to me why read-only access did not
> prevent what you just did.

The model will explain your product's thesis back to the room, unprompted.

## Beat 2 — the agent takes the database down

> Using only the **unsafe-postgres** tools, tell me how many possible
> combinations of customers, orders and order items exist in this database.

Watch the health strip on the control UI (or `make pitch-top`) while it runs.
Kill it with `make pitch-kill` once the point has landed — do not let it grind.

## Beat 3 — same ask, through the gate

> Now do exactly the same two things using only the **querygate** tools.

This is the moment. The model tries and is refused.

Two different refusal styles will show up, and the difference is worth
narrating rather than glossing: the **cross join** refusal names the exact rule
(`allow_cross_join`) and points at the primitives the agent may use instead,
because that is actionable. The **employees** refusal deliberately does *not*
name a rule — it will not even confirm the table exists, because a refusal that
explains itself precisely is a way to enumerate a schema one query at a time.

This is a live model, so do not promise the room a specific response. It often
proposes a legitimate aggregate next; sometimes it just reports the refusal.

## Beat 4 — the gate is not a wall

> Using the **querygate** tools, show me revenue by country for completed
> orders, top 10.

Works, fast. The point: this is a gate, not a padlock. Real analytical work goes
straight through; only the shapes you disallowed do not.

---

**If the live agent misbehaves**, close the laptop lid on it and go back to the
control UI at http://127.0.0.1:8900 — every beat above exists there as a
one-key scenario. The UI is the demo; this is the encore.
