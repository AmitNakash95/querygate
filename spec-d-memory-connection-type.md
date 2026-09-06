# Spec D (v2) — Memory Stores as a QueryGate Connection Type

**Supersedes:** `spec-d-memory-governance-spike.md`
**Positioning:** a connection type inside QueryGate. Not a module, not a sub-brand, not MemGate.

**The sentence this feature buys you:**
> QueryGate governs what your agents can reach — databases *and* memory stores — with one policy grammar and one audit log.

That last clause is the actual product. An audit trail that covers Postgres but silently misses the memory store is close to useless to a security reviewer, because memory is exactly where data crosses between agents.

---

## The key finding: this is a READ problem

Spec 1's `plan → approve → apply` model does **not** transfer here. Three reasons:

1. **Memory writes are implicit.** Agents write memory as a side effect of doing their job, not as a deliberate action. There is no moment where a human wants to approve "the agent remembered something."
2. **Frequency is wrong.** A memory write happens many times per session. Approval workflow would be unusable within minutes.
3. **The damage is on read.** A leaked memory hurts when the *wrong agent retrieves it*, not when it was written.

So memory governance reuses QueryGate's **read** path, not the new write path. That is good news for effort and it means this feature does not depend on Spec 1 shipping first.

Writes still need governance, but of a simpler kind: **mandatory tagging** (below), not approval.

---

## The hard part: semantic retrieval breaks predicate-based policy

This is the one genuinely novel engineering problem, and it needs to be right.

A SQL query states what it wants: `WHERE tenant_id = 'A'`. You can inspect the predicate and decide.

A memory query does not. It's a vector search — it returns whatever is *semantically nearest*, and nearness has no respect for tenancy. There is no predicate to inspect.

**Two possible enforcement points:**

| Approach | How | Verdict |
|---|---|---|
| **Post-filter** — run the search, drop results the principal may not see | Trivial to build | **Wrong. Do not do this.** |
| **Pre-filter** — push a metadata filter into the store so forbidden entries are never candidates | Requires store support | **Correct.** |

Post-filtering leaks through ranking side channels. If tenant A's search returns 3 results instead of 10, the *absence* is information — and worse, relevance scores shift depending on what was excluded. You leak the existence and rough shape of other tenants' data without ever returning a row. This is the memory equivalent of a blind SQL injection oracle.

**Design rule: if a memory store cannot push a metadata filter into the search itself, QueryGate refuses to govern it.** Say this in the docs as a feature, not a limitation. It's an honest statement that you enforce properly or not at all.

> **Verify before building:** I have not confirmed the current metadata-filtering API surface of Mem0 or Zep. Check this first — it determines which store you support and whether the pre-filter approach is viable at all. If neither supports it cleanly, this feature does not work and you should stop here.

---

## Mandatory tagging on write

Pre-filtering only works if every entry carries the metadata to filter on. So the write path has one job: **refuse untagged writes.**

Every memory entry written through QueryGate gets stamped:

```json
{
  "qg_tenant": "acme-corp",
  "qg_human": "amit@example.com",
  "qg_agent": "support-triage-agent",
  "qg_session": "sess_01J...",
  "qg_written_at": "2026-08-20T09:14:22Z",
  "qg_policy_version": "v14"
}
```

The agent cannot set these. QueryGate derives them from the authenticated principal, exactly as it does for database connections.

**Pre-existing untagged entries are the migration problem.** Options, in order of preference: (a) refuse to serve untagged entries at all, (b) treat untagged as a reserved `legacy` tenant readable only by explicitly-allowed principals. Never default untagged to "visible."

---

## Configuration

`connections.yaml` — memory is just another entry:

```yaml
connections:
  prod-postgres:
    type: postgres
    dsn: ${PG_DSN}

  agent-memory:
    type: mem0            # or zep
    endpoint: ${MEM0_URL}
    tenant_field: qg_tenant
```

`policy.yaml` — same grammar, memory-aware match keys:

```yaml
connections:
  agent-memory:
    memory_policy:
      enforcement: pre_filter      # only supported value; explicit on purpose
      require_tags: true
      untagged_entries: reject     # reject | legacy_tenant

      rules:
        - name: tenant-hard-isolation
          match: { cross_tenant: true }
          outcome: reject

        - name: triage-owns-its-namespace
          match:
            agent: support-triage-agent
            namespace: "tickets/*"
          outcome: allow
          operations: [read, write]

        - name: analytics-reads-only
          match: { agent: analytics-agent }
          outcome: allow
          operations: [read]

      default_outcome: reject
```

First match wins. `default_outcome: reject` mandatory. Hot-reload via the existing staged workflow.

---

## MCP tool surface

Mirrors the database tools so the agent experience is consistent:

| Tool | Purpose |
|---|---|
| `memory_search` | Semantic search, pre-filtered by policy |
| `memory_write` | Write with mandatory principal tags |
| `memory_forget` | Delete by id or filter — policy-gated |
| `describe_memory_policy` | What this agent may reach, so it can self-limit |

`memory_forget` exists mainly for erasure requests (below), not for agent convenience. Consider defaulting it to `reject` in policy.

---

## Evidence log

Reuses Spec 1's hash-chained log. Same file, same chain, same `verify-log` command — **this is the entire point of doing it inside QueryGate.**

Memory events append alongside database events:

```json
{ "event": "memory.search.filtered", "connection": "agent-memory",
  "principal": {...}, "payload": { "namespace": "tickets/*",
  "candidates_excluded_by_policy": 47, "matched_rule": "tenant-hard-isolation" } }
```

Log the *metadata* of what was excluded, never the excluded content — same reasoning as Spec 1's no-row-contents rule. A log full of other tenants' memories is a liability.

**GDPR erasure is a real secondary sales angle.** Memory stores accumulate personal data indefinitely with no natural deletion point. Being able to answer "show me every memory entry attributable to this person, and prove it was deleted" is a genuine capability. Worth mentioning in sales; not worth building a compliance module around yet.

---

## Explicitly out of scope

Do not let these creep in:

- **Prompt-injection / poisoned-memory defence.** Memory is a control channel and poisoning is a real threat — but it's a *content* problem, not an *access* problem. Different discipline, crowded field. Not yours.
- **Memory provenance and lineage graphs.** Interesting, unproven demand.
- **Vector/embedding-level policy.** Namespace and tenant granularity is enough.
- **Memory quality, dedup, summarisation.** That's the memory store's job.
- **A second store backend** until the first has a paying user.

---

## Validation — reduced, not skipped

Framing this as a feature lowers the bar but does not remove it. You still need to know it will be used.

**Cheapest possible test, before writing code:** add it to `describe_memory_policy`-shaped conversation with your existing QueryGate customers. One question:

> Do your agents share a memory store, and do you know today which agent can read what in it?

- **3+ of your existing customers say "yes, and no"** → build it. Real demand from people who already pay you.
- **Most don't use a shared memory store** → shelve it. Revisit in 6 months. Nothing lost.
- **They use one but don't care who reads what** → shelve it. That's the honest signal that the pain is theoretical.

This replaces the 5 external interviews from v1. The external-buyer question only matters if this were a standalone product — as a feature, the only opinion that counts is your existing customers'.

**Still worth pre-committing:** write down which bucket you land in, with quotes. The point of deciding the threshold now is that it stops you talking yourself into it later.

---

## Sequencing

1. **Ship write-governance first** (Spec 1). It sells to your existing base immediately and stretches QueryGate from "read" to "read + write."
2. **Verify metadata-filter support** in Mem0/Zep. This is a half-day check that can kill the feature outright.
3. **Ask the one validation question** during normal customer conversations. Zero incremental cost.
4. **Build**, if 1–3 all pass. Estimate: smaller than write-governance, since it reuses the read path and the evidence log.

**Do not reposition QueryGate's marketing around "agent-reachable stores" until this ships and has a user.** You have 15–20 customers who know exactly what QueryGate means. That clarity is an asset — broaden the story after the capability is real, not before.

---

## Honest uncertainty

- No evidence anyone will pay *extra* for this. As a bundled feature that's acceptable; as a pricing line item it is unvalidated.
- The Mem0/Zep filtering capability is assumed, not verified. Check it first.
- The pre-filter/post-filter analysis is my reasoning about how vector search leaks, not a citation. It's sound as far as I can tell, but worth a second opinion before it becomes a load-bearing security claim in your docs.
