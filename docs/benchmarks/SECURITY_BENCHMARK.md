# QueryGate adversarial security benchmark

**A reproducible, versioned measurement of how QueryGate's structural
guardrails behave against a corpus of boundary attacks — and how a naive
raw-SQL-forwarding gateway behaves against the same attacks.**

This document turns the adversarial "five-minute demo" from
the go-to-market plan (internal) — historically performed live by a
salesperson — into a numbers-on-the-page artifact anyone can regenerate. It is
the evidence behind the claim that structurally forbidding raw SQL is a
categorically different posture from filtering or trusting a model-generated
SQL string.

> TL;DR of the current corpus (`security_boundary_v1`): QueryGate blocks
> **16/16 (100%)** of the structural boundary attacks; the modeled raw-SQL
> passthrough baseline blocks **0/16 (0%)**; **2** documented inference
> residuals are disclosed (not counted as catches); guardrail overhead is
> **sub-millisecond per query, before any database round-trip.** Regenerate the
> exact current numbers with the command below — never quote these from memory.

## How to run it

```bash
make security-benchmark                                # human-readable report
make security-benchmark ARGS="--json"                  # machine-readable report
make security-benchmark ARGS=list                      # list the corpus cases

# ...or call the CLI directly:
poetry run querygate-security-benchmark run
poetry run querygate-security-benchmark run --json
poetry run querygate-security-benchmark list
```

Exit code is `0` iff the run is clean (every attack caught, no regressions), so
the same command gates CI and a periodic integrity job. The corpus lives in
[`benchmarks/security_boundary_v1.yaml`](../../benchmarks/security_boundary_v1.yaml);
the runner is `querygate.security_benchmark`; the harness is regression-locked
by `tests/unit/test_security_benchmark.py`.

## Methodology (why the numbers are trustworthy)

1. **It drives the real guardrails, not a mock.** Every case is run through the
   genuine production code paths — policy validation
   (`validation/policy_validation.py`) and the SQLAlchemy compiler's parameter
   binding (`compiler/sqlalchemy_compiler.py`). A 100% catch rate means the
   shipping enforcement caught the attack, not that a test double returned
   "blocked."
2. **It is offline and deterministic.** The three guardrails this benchmark
   exercises — the AST-only structural shape, policy allow/deny checked over
   *every* column reference in the query (not just `SELECT`), and compiler
   parameter binding — all run *before any database touch*. So the benchmark
   needs no database, no network, and no LLM, and its pass/fail is fully
   reproducible by a third party. (Only the latency figures vary run to run and
   are reported as informational.)
3. **It is honest by construction.** Documented inference residuals
   ([`docs/INFERENCE_RISKS.md`](../INFERENCE_RISKS.md)) are carried in the
   corpus and reported in their own line — the benchmark discloses what
   QueryGate does *not* block rather than cherry-picking only its wins. The
   `test_every_attack_case_declares_a_vulnerable_baseline` test prevents the
   comparison from being inflated.

## The attack corpus (`security_boundary_v1`)

| Category | What it probes | Example case |
| --- | --- | --- |
| `sql_injection` | Injection payloads in predicate values are carried as **bound parameters**, absent from the compiled SQL text — across dialects (Postgres, MSSQL) | `sqli-predicate-eq-value` |
| `policy_denied_table` | A denied table cannot be smuggled in through `WHERE`, `ORDER BY`, or any non-`SELECT` clause | `denied-table-in-where` |
| `policy_denied_column` | A denied column cannot be referenced anywhere — `SELECT`, `WHERE` (even when never projected), `GROUP BY`, `HAVING`, `ORDER BY`, or a window partition | `denied-column-in-where-not-selected` |
| `complexity_cap` | Query-shape budgets (max joins, max select columns, …) reject over-broad queries | `joins-exceed-cap` |
| `structural_invariant` | **No field anywhere in the request AST accepts a raw SQL string** — a structural regression lock | `no-raw-sql-escape-hatch` |
| `documented_residual` | Inference risks column policy cannot close (semantic correlation, single-row aggregates) — **allowed by design, disclosed** | `residual-single-row-aggregate` |

The corpus is versioned. A change to the guardrails that would alter a verdict
is a deliberate, reviewed edit (new `corpus_id` version), never silent drift.

## The raw-SQL passthrough baseline — what it is and is not

The baseline column is a **structural model**, declared per case, of a gateway
whose interface is a model-generated SQL string forwarded to the database with
no AST contract. Its verdicts are grounded in a structural fact, not an
assumption: such a gateway has

- **no per-query table/column policy** — it cannot check identifiers it never
  parses into a validated shape, so denied-table/denied-column smuggling
  through any clause lands; and
- **no parameter-binding contract** — a string-concatenating passthrough
  executes an injected statement rather than binding it as data.

This is **not** a live run of any specific competitor. It is the honest lower
bound for "just let the agent send SQL." The point of the comparison is
categorical: the attacks QueryGate stops are ones that a raw-SQL interface has
no structural mechanism to stop, no matter how the SQL was generated or
reviewed.

## Google MCP Toolbox comparison (capability-level, not a live run)

[Google's MCP Toolbox for Databases](https://github.com/googleapis/mcp-toolbox)
is the closest widely-known comparator, so a fair, factual comparison matters.
Based on Toolbox's **documented design** (not a benchmark run against it):

- Toolbox exposes database access as developer-authored *tools*, commonly
  parameterized SQL statements. Where a tool is a fixed parameterized statement,
  its parameters are bound — so the SQL-injection class is **not** where
  QueryGate differentiates against a well-authored Toolbox tool.
- The differentiation is **structural governance of agent-composed queries**:
  QueryGate's caller submits a validated `StructuredQuery` AST and *cannot*
  submit raw SQL at all, and every query is checked against per-principal
  table/column policy and query-shape caps over every clause — by construction,
  before compilation. Toolbox's model centers on the tools a developer decides
  to publish; it does not impose an AST-only contract or a per-query
  column-policy check across every clause on arbitrary agent-composed queries.

We deliberately do **not** publish head-to-head catch-rate numbers against
Toolbox in phase 1, because a fair live comparison requires a comparably
configured Toolbox deployment and would otherwise risk misrepresenting a
documented competitor capability — which this project forbids. That live
comparison is scoped as phase 2 below.

## Scope and phases

- **Phase 1 (this document).** The versioned attack corpus, the runner, the CLI,
  QueryGate's measured catch rate, the structurally-modeled raw-SQL baseline, and
  the capability-level Toolbox comparison. Fully offline, deterministic, and
  reproducible.
- **Phase 2 (not started — needs external infrastructure).** A *live* baseline:
  execute the same corpus against (a) a real LLM composing raw SQL against a
  seeded database and (b) a comparably configured Google MCP Toolbox deployment,
  and publish measured catch-rate and latency numbers for both. This needs a
  model-provider decision and a GCP/Toolbox environment, so it is intentionally
  out of phase 1's offline scope. See TODO.md item 58.

## Relationship to the rest of the security posture

This benchmark is the **publishable subset** of QueryGate's adversarial QA. The
full guarantee set — including error-masking, credential redaction, catalog
disclosure, concurrency/DoS, and config-governance authorization — is
regression-locked in [`tests/security/`](../../tests/security/) (TODO.md item
28) and described in [`docs/THREAT_MODEL.md`](../THREAT_MODEL.md). The benchmark
exists to make the structural core of that posture *legible and reproducible to
an outside reviewer*, not to replace the full suite.
