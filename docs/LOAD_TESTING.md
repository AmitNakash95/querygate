# Load and soak testing

QueryGate has a real-PostgreSQL load harness for the concurrency and query-timeout
guardrails. It sends concurrent structured queries through the REST application and polls
PostgreSQL's `pg_stat_activity` while they execute. This measures work active at the
database boundary, rather than treating the number of client tasks as proof that the cap
worked.

## Quick load gate

Start the local demo infrastructure, then run the bounded CI-equivalent check:

```bash
make compose-up
make test-load
```

The default three rounds take a few seconds. Each round sends an eight-request burst at a
policy cap of two in two modes:

- a short concurrency wait, where exactly two requests succeed and six receive the REST
  concurrency rejection (`422`, `too many concurrent ...`); and
- a long concurrency wait, where six requests queue and all succeed in waves.

In both cases PostgreSQL must report a peak of exactly two active probe queries. A final
burst uses a four-second database query under a one-second policy timeout: two requests
must be cancelled near one second, two must be rejected by the concurrency cap, and no
probe query may remain active afterward.

## Soak gate

Run the same machine-checked scenarios repeatedly:

```bash
make test-soak
```

`SOAK_ROUNDS` defaults to 100 and can be overridden without editing the test:

```bash
make test-soak SOAK_ROUNDS=500
```

For a shorter custom run, `make test-load LOAD_ROUNDS=10` uses the same mechanism. The
pytest command can also be run directly with `QUERYGATE_LOAD_ROUNDS=<n>` and `-m load`.

## What the harness changes

The test creates two views and one function prefixed `querygate_load_` in the demo
database, and removes them in fixture teardown. They contain no production data. QueryGate
uses its normal structured-query validation, reflection, compilation, session guardrails,
and REST error mapping; only the HTTP socket and reverse proxy are bypassed by HTTPX's
in-process ASGI transport.

The automated harness targets the single-process semaphore. Redis cross-instance atomicity,
lease recovery, and fail-open/fail-closed behavior are covered separately by the Redis
limiter tests; production multi-replica deployments must continue to select
`CONCURRENCY_BACKEND=redis`.

## Interpreting failures

- A PostgreSQL peak above `max_concurrency` is a guardrail correctness failure.
- A peak below the cap usually indicates that the database or runner could not start work
  quickly enough; rerun once, then inspect pool saturation and host load.
- Unexpected `422` responses in queued mode mean `concurrency_wait_seconds` was exhausted.
- A timeout scenario near four seconds means the server-side statement timeout did not
  cancel the query. A failure much faster than one second is likely setup, reflection, or
  connection failure rather than successful timeout enforcement.
