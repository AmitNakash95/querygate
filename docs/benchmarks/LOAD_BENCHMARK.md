# QueryGate load benchmark

**A reproducible measurement of throughput and latency under concurrent
load, comparing QueryGate's full REST pipeline to raw SQL at the same
concurrency — and whether adding worker processes actually fixes the
ceiling — over a real socket against a real server.**

The sibling of [`PERFORMANCE_BENCHMARK.md`](PERFORMANCE_BENCHMARK.md), which
answers "how much does one request cost." This answers the second half of the
same question a design partner asks: *does that cost hold up under
concurrent traffic, and does adding capacity actually fix it?*

> No headline number is quoted here on purpose. Unlike single-request
> latency, throughput-under-concurrency is measuring how much CPU-bound work
> QueryGate can push through in a given window — which means the number you
> get depends on what else is competing for CPU on the machine you run it
> on. Run it yourself with `make load-benchmark` on an otherwise-idle
> machine and read the report it prints; don't quote a number from this
> document or from a prior run.

## How to run it

```bash
make compose-up                                            # start the local demo Postgres
make load-benchmark                                         # human-readable report
make load-benchmark ARGS="--json"                            # machine-readable report
make load-benchmark ARGS="--workers 1 2 4 8 --concurrency 1 10 30"  # custom sweep
```

```bash
# ...or call the CLI directly:
poetry run querygate-load-benchmark run
poetry run querygate-load-benchmark run --json
```

**Every run also writes a fresh, publish-ready results snapshot** to
[`LOAD_BENCHMARK_RESULTS.md`](LOAD_BENCHMARK_RESULTS.md), overwritten each
time — data only (the tables above, generated timestamp, host CPU
count/OS), never hand-edited, never interpreted; it links back to this
document for methodology rather than duplicating it. Pass
`--markdown-out <path>` to write it somewhere else, or `--no-markdown` to
skip. This means the moment you run the benchmark, you have a shareable
artifact ready to hand to a design partner or drop into a review — no
manual write-up step.

The runner is `querygate.load_benchmark`; the CLI is
`querygate.load_benchmark_cli`; the harness is regression-locked by
`tests/unit/test_load_benchmark.py` (pure, no DB, no subprocess) and
`tests/integration/test_load_benchmark_live.py` (real Postgres + real
`uvicorn` subprocesses, run via `make test-postgres-live`, and — since it
carries no `load` marker — also part of CI's `postgres-live` job on every
push/PR). Exit code is always `0` on a completed run — informational, not
pass/fail, same posture as the single-request benchmark.

## Not the same tool as `make test-load`/`make test-soak`

[`docs/LOAD_TESTING.md`](../LOAD_TESTING.md)'s harness proves *correctness*
of the concurrency/timeout guardrails — that a configured cap of N never lets
more than N queries run at once, that a caller's wait deadline is honored,
that a stuck query actually gets cancelled — using a small, deliberately
tight cap (2) so rejection/queueing behavior is directly observable.

This benchmark *measures performance*: for a policy cap raised high enough to
never bind, how do latency and achieved throughput behave as concurrency and
worker count both vary, compared to raw SQL under the same concurrency.
Different question, complementary tool.

## Why this drives a real socket, not `ASGITransport`

An earlier version of this benchmark reused `performance_benchmark.py`'s
in-process `ASGITransport` trick (correct for measuring one request's cost,
where it avoids real network variance). That's the wrong choice for a
*concurrency* benchmark specifically: it puts the test client and the server
being measured in the same Python process, on the same event loop. At high
concurrency, the client's own coroutine bookkeeping competes with QueryGate's
CPU-bound request handling for the same CPU — a confound no real deployment
has, since a real client is always a separate process from the server.
Verified directly: the same concurrency sweep, run first in-process and then
over a real socket against a real single-worker server, showed the same
*shape* (throughput rises, peaks, then falls off) but a dramatically
different *magnitude* — the in-process version showed REST throughput
collapsing to roughly a tenth of the raw-SQL baseline at the highest
concurrency tested; the real-socket version showed a much gentler dip from
its own peak. Same underlying single-process ceiling, but the in-process
harness's self-competition made it look far worse than a real deployment
would ever experience. This version fixes that by always measuring the real
app over a real socket, and adds a `--workers` sweep so "does adding workers
fix it" is a measured answer, not an assertion.

## A result is only as clean as the machine it ran on

This is the sharpest methodology lesson from building this benchmark, so it
gets its own section rather than a footnote. Throughput-under-concurrency is
fundamentally a measurement of how much CPU-bound work (request parsing,
policy validation, AST→SQL compilation) a process can push through in a
fixed window. That means **any other CPU-hungry process on the same
machine directly steals from the number you measure** — a second test suite
running in another terminal, a background build, another benchmark, a busy
CI runner shared with other jobs. Confirmed directly while building this
tool: a concurrency sweep that cleanly showed workers helping (a ~2.3–2.5x
throughput increase going from 1 worker to 4) was rerun minutes later on the
same machine, with several unrelated heavy processes now also running, and
showed no improvement from added workers at all — same code, same database,
same query, different result, because the CPU was no longer QueryGate's to
use.

**What this means practically:** run this benchmark on a machine that isn't
also running something else CPU-heavy, and treat a single run as one sample,
not a certified number. If a run looks flat or surprising, check what else
is competing for CPU before concluding anything about QueryGate.

## What it measures

Reuses `performance_benchmark.py`'s seeded table and exact scenario
definitions (point lookup, filtered scan, aggregation — round-robined across
the sweep, so the number reflects a mixed workload, not one easy case), and
two tiers: `baseline` (raw SQL, this process) and `rest` (a real,
separately-spawned `querygate.api.app:app` server, over a real socket). No
`pipeline` (no-HTTP) tier here — that tier exists in the single-request
benchmark to attribute overhead to a layer; it has no meaning once the REST
tier is a separate process.

For each **worker count** in the sweep (default `1, 2, 4` — real
`uvicorn --workers N` processes, a fresh server per worker count, its own
throwaway `connections.yaml`/`policy.yaml`), the full **concurrency level**
sweep (default `1, 5, 15, 30`) runs against that server: `count = concurrency
× requests-per-slot` (default 10) requests fired with at most `concurrency`
in flight at once. Per (worker count, concurrency level) pair:

- **Achieved throughput** — `successful_requests / wall_clock_seconds` for
  that whole batch. Deliberately not `count / wall_clock_seconds`: failures
  return fast (a rejection, a refused connection), so counting them in the
  numerator would inflate throughput exactly when a run is least
  trustworthy. This is what "requests/second" means under concurrency, not
  the reciprocal of one request's latency.
- **Latency distribution** — mean/p50/p95/p99 across the batch's
  *successful* requests.
- **Error count** — a batch tolerates per-request failures (a non-2xx
  response, a connection error) rather than aborting, the same posture any
  real load-testing tool (`wrk`, `hey`, `ab`) takes; a clean run against a
  correctly-sized server should show zero.

Both engines' connection pools and the policy's concurrency cap are sized to
comfortably exceed the sweep being run — a rejected/queued/connection-starved
request here would be a benchmark-setup bug, not a measurement (an earlier
version of the pool-sizing formula under-sized each worker's own pool below
the concurrency being thrown at it, which silently capped every worker
count at the same ceiling and made added workers look like they weren't
helping — fixed by sizing generously per worker instead of dividing a fixed
budget across them).

## The spawned server is unauthenticated for the run's duration

Each server this benchmark spawns is bound to `127.0.0.1` only — never
reachable from another host — but for the seconds-to-minutes it runs it has
**no API key, no JWT, `ENVIRONMENT=localhost`** (so the production
auth-required check never fires), against the **real** connection string you
passed in, reading and writing the benchmark's own throwaway seeded table.
On a single-user workstation this is no different from any other local dev
server. On a **shared or multi-tenant machine**, any other local user or
process can reach `http://127.0.0.1:<port>` for that window and issue
queries as an anonymous, fully-privileged caller against that connection —
the same posture as running `uvicorn querygate.api.app:app` by hand with no
`API_KEYS` set, not a gap specific to this tool. `_start_server` also
explicitly neutralizes a curated set of settings a caller's own shell/CI
environment (or the repo's own `.env`, which `AppConfig` reads by default)
might otherwise leak into the spawned process — auth (`JWT_ENABLED`,
`MCP_ENABLED`, `METRICS_REQUIRE_AUTH`), catalog/semantic-memory
(`CATALOG_FILE`, `TEMPLATES_FILE`, the three `SEMANTIC_MEMORY_*` flags),
secrets (`VAULT_ENABLED`, `VAULT_TOKEN`, `CREDENTIAL_LEASE_REFRESH_ENABLED`),
and routing (`API_V1_PREFIX`, `CONCURRENCY_BACKEND`) — see its source for the
exact list. This closes every security-relevant setting identified so far,
but it is a curated list, not a mechanical guarantee against every field
`AppConfig` exposes; a future `AppConfig` field with its own security
implication needs an explicit decision here, the same way each field above
got one, rather than being assumed safe by omission.
**Don't run this benchmark's throwaway server against a connection string
that reaches a real production database**, and be aware of who else has
local access to the machine while it runs.

This window is bounded by the run completing OR by a SIGTERM (the CLI
installs a handler that stops every spawned server before the process
actually exits, closing the common case of a CI job timeout or `kill <pid>`)
— but not by a SIGKILL, which no process can catch or clean up after. If
this benchmark's process is killed with `kill -9` (or an OOM killer, or a
hard container stop), the spawned server keeps running, still anonymous,
still holding pooled connections to your database, until something else
stops it. Recover with `pkill -f "uvicorn querygate.api.app:app"` (adjust the
pattern if you're running multiple unrelated uvicorn processes) if a run
ever gets killed this way and you're unsure whether the server is still up.

## What this does *not* claim

- **It is not a certified capacity number.** See "A result is only as clean
  as the machine it ran on" above — run it yourself, on a quiet machine, and
  treat the output as one sample.
- **It does not model a realistic traffic pattern.** Concurrency levels are
  fixed, sustained, and synthetic (uniform round-robin across three query
  shapes) — real agent traffic is bursty and shaped differently. Use this
  for relative comparison (baseline vs. REST, worker count vs. worker count),
  not as a capacity-planning number taken literally.
- **It does not model the concurrency *guardrail*.** The policy cap is
  deliberately raised above every level tested; `make test-load`/
  `make test-soak` cover cap-rejection/queueing behavior.
- **It does not measure a governed production connection's full cost.**
  Audit persistence is set to `none` (no real sink I/O), the REST tier runs
  as an anonymous caller (no API key/JWT, so per-principal quota accounting
  is not exercised), and the synthetic policy carries no column masks,
  mandatory row filters, or k-anonymity checks. A real, governed production
  connection with those features enabled will show somewhat higher
  per-request cost.

## Relationship to the rest of the performance/reliability posture

Three complementary artifacts now cover the full performance story a
security/ops review asks for:
[`PERFORMANCE_BENCHMARK.md`](PERFORMANCE_BENCHMARK.md) (per-request cost),
this document (throughput/latency under concurrent load, across a
worker-count sweep), and [`docs/LOAD_TESTING.md`](../LOAD_TESTING.md)
(concurrency/timeout guardrail *correctness*, plus the Redis-backed
multi-replica concurrency story).
