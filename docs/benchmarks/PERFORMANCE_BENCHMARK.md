# QueryGate performance benchmark

**A reproducible measurement of how much latency QueryGate adds to a query,
and where that time goes.**

This is the evidence behind the performance question every design partner
asks before they'll route real traffic through a gateway: *"how much time
does this add, and will it slow down our database access?"* It turns that
question into numbers anyone can regenerate on their own hardware, against a
real Postgres instance — not an estimate.

> TL;DR of a representative run (your hardware will differ — regenerate the
> exact current numbers with the command below, never quote these from
> memory): across three query shapes (point lookup, filtered scan,
> aggregation), QueryGate's full REST round trip added a **mean of ~2.8ms**
> over raw SQL executed directly against the same Postgres instance — of
> which **~1.6ms** is the guardrail pipeline itself (policy validation,
> cached schema validation, compilation, execution — audit persistence is
> off for this run, see "What it measures" below) and the remainder is
> HTTP/JSON transport. Both baseline and QueryGate queries completed in low
> single-digit milliseconds; the overhead is not a perceptible delay for an
> agent workflow. Architecturally, QueryGate does not rewrite the *shape* of
> the query you asked for — it compiles your AST into standard SQL via
> SQLAlchemy Core rather than generating different query structure — but it
> does apply per-session guardrails Postgres itself then executes under
> (`SET LOCAL statement_timeout`/`lock_timeout`), and that cost is part of
> the `pipeline`/`rest` numbers above, not hidden from them. This benchmark
> does not itself compare Postgres query plans between tiers.

## How to run it

```bash
make compose-up                                          # start the local demo Postgres
make performance-benchmark                                # human-readable report
make performance-benchmark ARGS="--json"                  # machine-readable report
make performance-benchmark ARGS="--rows 20000 --iterations 100"  # heavier run

# ...or call the CLI directly:
poetry run querygate-performance-benchmark run
poetry run querygate-performance-benchmark run --json
```

**Every run also writes a fresh, publish-ready results snapshot** to
[`PERFORMANCE_BENCHMARK_RESULTS.md`](PERFORMANCE_BENCHMARK_RESULTS.md),
overwritten each time — data only, never hand-edited; it links back to this
document for methodology rather than duplicating it. Pass
`--markdown-out <path>` to write it elsewhere, or `--no-markdown` to skip.

The runner is `querygate.performance_benchmark`; the CLI is
`querygate.performance_benchmark_cli`; the harness is regression-locked by
`tests/unit/test_performance_benchmark.py` (pure, no DB) and
`tests/integration/test_performance_benchmark_live.py` (real Postgres, run via
`make test-postgres-live`, and — since it carries no `load` marker — also part
of CI's `postgres-live` job on every push/PR). Exit code is `0` on a completed
run unless you pass `--max-overhead-ms` and a scenario exceeds it, or the run
itself errors (e.g. no reachable database) — otherwise it is informational,
not pass/fail (see below).

## What it measures

For three query shapes of differing complexity — a primary-key point lookup,
a filtered-and-sorted scan, and a `GROUP BY` aggregation — the benchmark times
the *same logical query* at three tiers:

| Tier | What it is | What it includes |
| --- | --- | --- |
| `baseline` | Raw SQL via a plain SQLAlchemy async engine | Nothing but the database round trip — this is what an agent with a bare connection string sees |
| `pipeline` | `StructuredQueryService.execute()` called directly, no HTTP | Policy validation, cached schema validation, AST→SQL compilation, session guardrails, execution, the audit call site (persistence explicitly disabled for this run — see below) |
| `rest` | A full `POST /{connection}/query` round trip through the real FastAPI app, via an in-process ASGI transport | Everything `pipeline` does, plus request routing, the auth dependency, and JSON (de)serialization |

The benchmark explicitly disables audit *persistence* for the run (an
in-process `NullAuditSink`, not the operator's configured backend) so results
are reproducible regardless of what audit sink the machine running it happens
to have configured — the `pipeline`/`rest` numbers include the cost of
*reaching* the audit call site, not the I/O cost of a real sink. A deployment
with a persisted backend (`jsonl`, `jsonl_chained`, `jsonl_chained_s3_worm`)
adds file/S3 I/O on top of these numbers.

`rest_ms - baseline_ms` is "how much QueryGate adds to a request" the way a
customer means the question. `pipeline_ms - baseline_ms` isolates the
guardrail/compile cost from HTTP/JSON transport cost, so a slow number (if one
ever shows up) can be attributed to the right layer instead of guessed at.

Each run seeds its own throwaway table (`querygate_perf_bench_<random>`, 5,000
rows by default — override with `--rows`) so results never depend on whatever
happens to be loaded into the shared demo database, and drops the table when
it finishes (including on error). 5 warmup iterations per scenario are
discarded before the 30 measured ones (both configurable), and each iteration
draws a fresh random row/predicate value so no single cached plan or row
flatters one tier over another.

## Methodology (why the numbers are trustworthy)

1. **It drives the real pipeline, not a mock.** Every timed call goes through
   the genuine production code paths — the same `StructuredQueryService` and
   `create_app` the REST API and MCP transport are thin wrappers over (see
   `CLAUDE.md`'s "one request pipeline"). A reported pipeline overhead is what
   shipped code actually costs, not an estimate of it.
2. **It needs a real database, deliberately.** Unlike the security benchmark
   (`docs/benchmarks/SECURITY_BENCHMARK.md`), which is offline by design because
   every guardrail it exercises runs before any database touch, latency
   *is* the thing under measurement here, and a mocked database would make
   the numbers meaningless. Unlike the security benchmark, this means it
   can't run in the default `pytest -m unit` suite — but its integration test
   *does* run automatically in CI's `postgres-live` job on every push/PR
   (it carries no `load` marker, so it lands in the same "every postgres_live
   test except the load-marked ones" bucket as most of that job, not the
   schedule-only `test-soak` tier). What CI checks there is that the harness
   itself works end to end against a real database, not any specific latency
   number — this document's published figures are still a manual
   `make performance-benchmark` run, regenerated for a specific report.
3. **It measures three tiers so overhead is attributable.** A single
   "QueryGate is Xms slower" number invites the question "slower where?" —
   reporting `baseline`/`pipeline`/`rest` separately answers it: how much is
   the guardrail logic itself, and how much is HTTP/JSON transport that any
   REST API pays regardless of what's behind it.
4. **It is honest about scope.** The `rest` tier uses httpx's ASGI transport
   in-process — real FastAPI routing, dependency injection, and
   (de)serialization, but no real TCP socket or TLS handshake. A production
   deployment adds real network latency on top of these numbers, the same way
   it would for a direct database connection; this benchmark isolates
   *QueryGate's own* contribution, not the network's.
5. **It reports distributions, not a cherry-picked number.** Mean, p50, p95,
   and p99 are all reported per tier per scenario, so a customer evaluating
   tail latency isn't stuck with only a mean.

## What this does *not* claim

- **It does not measure query execution cost inside the target database, and
  does not compare Postgres query plans between tiers.** QueryGate validates
  and compiles a query before Postgres ever sees it, and compiles the same
  AST shape you submitted into standard SQL rather than generating different
  query structure — it does apply session-level guardrails (`SET LOCAL
  statement_timeout`/`lock_timeout`) the raw-SQL baseline doesn't set, and
  that setup cost is included in the `pipeline`/`rest` numbers, not hidden.
  The `baseline` vs. `pipeline`/`rest` gap is QueryGate's own request-handling
  cost (validation, compilation, the session guardrail, HTTP); this benchmark
  does not itself capture or diff `EXPLAIN` output to verify plan equivalence.
- **It does not model concurrency or load.** This benchmark is single-request
  latency. For behavior under concurrent load (admission queuing, the
  configured concurrency cap, timeout cancellation), see
  [`docs/LOAD_TESTING.md`](../LOAD_TESTING.md) and `make test-load`/
  `make test-soak` — a different, complementary question from "how much does
  one request cost."
- **It is not a cross-competitor comparison.** There is no independent
  baseline gateway run here (contrast the security benchmark's modeled
  raw-SQL-passthrough baseline) — the comparison is QueryGate vs. no gateway
  at all, which is the number a design partner asks for first.
- **Absolute numbers are hardware- and network-dependent.** The published
  TL;DR is one representative run; the point of publishing the command is
  that a reviewer regenerates the number on their own infrastructure rather
  than trusting a number quoted from memory.

## Relationship to the rest of the performance/reliability posture

This benchmark answers "how much does one request cost." The concurrency,
timeout, and queueing guarantees under *concurrent* load are covered
separately by [`docs/LOAD_TESTING.md`](../LOAD_TESTING.md) (`make test-load` /
`make test-soak`), which proves the configured caps hold at the database
boundary rather than inferring them from client task counts. Together they
cover both halves of the performance story a security/ops review asks for:
per-request cost, and behavior under load.
