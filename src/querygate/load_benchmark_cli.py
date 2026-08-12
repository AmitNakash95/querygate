"""`querygate-load-benchmark` — measure QueryGate's throughput/latency under
concurrent load, against a real Postgres instance (default: the local demo
database from ``make compose-up``), driving the real app over a real socket
with a worker-count sweep.

Complements ``querygate-performance-benchmark`` (single-request cost) — see
``querygate.load_benchmark`` for methodology and
``docs/business/LOAD_BENCHMARK.md`` for the published write-up. Not the same
tool as ``make test-load``/``make test-soak`` (guardrail *correctness* under
load) — see the module docstring for the distinction.

Informational, not pass/fail — exit code is always 0 on a completed run
(non-zero only on an error, e.g. no reachable database).
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# See performance_benchmark_cli.py — must happen before any `querygate...`
# import, since querygate.core.logging binds LOG_LEVEL at import time.
os.environ.setdefault("LOG_LEVEL", "WARNING")
logging.getLogger("httpx").setLevel(logging.WARNING)

import asyncio  # noqa: E402

import querygate.load_benchmark as load_benchmark  # noqa: E402
from querygate.load_benchmark import (  # noqa: E402
    DEFAULT_CONCURRENCY_LEVELS,
    DEFAULT_REQUESTS_PER_SLOT,
    DEFAULT_WORKER_COUNTS,
    LoadBenchmarkReport,
    run_load_benchmark,
)
from querygate.performance_benchmark import DEFAULT_CONNECTION_STRING  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MARKDOWN_PATH = _REPO_ROOT / "docs" / "business" / "LOAD_BENCHMARK_RESULTS.md"


def _row(*cols: str) -> str:
    widths = (12, 10, 16, 12, 12, 16, 12, 12, 10)
    return "".join(col.ljust(w) for col, w in zip(cols, widths))


def _print_report(report: LoadBenchmarkReport) -> None:
    print("QueryGate load benchmark (throughput/latency under concurrency, real socket)")
    print(
        f"  seeded rows: {report.seeded_rows}, requests/slot: {report.requests_per_slot}, "
        f"dialect: {report.dialect}, policy max_concurrency: {report.max_concurrency}"
    )
    for sweep in report.sweeps:
        print("=" * 126)
        print(f"workers = {sweep.workers}")
        print("-" * 126)
        print(
            _row(
                "concurrency",
                "requests",
                "baseline req/s",
                "mean ms",
                "p95 ms",
                "rest req/s",
                "mean ms",
                "p95 ms",
                "errors",
            )
        )
        print("-" * 126)
        for level in sweep.levels:
            print(
                _row(
                    str(level.concurrency),
                    str(level.requests),
                    f"{level.baseline.throughput_rps:.1f}",
                    f"{level.baseline.latency.mean_ms:.2f}",
                    f"{level.baseline.latency.p95_ms:.2f}",
                    f"{level.rest.throughput_rps:.1f}",
                    f"{level.rest.latency.mean_ms:.2f}",
                    f"{level.rest.latency.p95_ms:.2f}",
                    f"{level.baseline.errors}/{level.rest.errors}",
                )
            )
            print(
                f"{'':<12}overhead at this concurrency: +{level.rest_overhead_ms:.2f}ms mean, "
                f"REST sustains {level.throughput_ratio * 100:.0f}% of baseline throughput"
            )
    print("=" * 126)
    print(
        "All figures are wall-clock, over a real socket against a real uvicorn server\n"
        "(errors column: baseline/rest failed-request counts). "
        "See docs/business/LOAD_BENCHMARK.md for methodology."
    )


# Below this ratio of (max-worker-count peak throughput) / (min-worker-count
# peak throughput), a run gets an explicit caution banner rather than
# silently publishing a flat-looking sweep. Chosen well below the ~2.3-2.5x
# this benchmark measures on an idle machine (see LOAD_BENCHMARK.md's "only
# as clean as the machine it ran on" section) — this only needs to catch the
# "no visible benefit at all" case a CPU-contended run produces, not flag
# ordinary run-to-run variance.
_FLAT_SCALING_RATIO_THRESHOLD = 1.3


def _scaling_caution(report: LoadBenchmarkReport) -> Optional[str]:
    """A one-paragraph caution banner when this run's own data doesn't show
    added workers helping — i.e. exactly the CPU-contention symptom
    documented in LOAD_BENCHMARK.md's "only as clean as the machine it ran
    on" section. Returns None when there's nothing to flag (a single-worker
    sweep has no comparison to make; a sweep showing real scaling needs no
    caution)."""
    if len(report.sweeps) < 2:
        return None
    lowest = report.sweeps[0]
    highest = report.sweeps[-1]
    if not lowest.levels or not highest.levels:
        return None
    # Each sweep runs the identical, sorted concurrency-level list (see
    # run_load_benchmark), so the last entry is always the peak level.
    lowest_peak_rps = lowest.levels[-1].rest.throughput_rps
    highest_peak_rps = highest.levels[-1].rest.throughput_rps
    if lowest_peak_rps <= 0:
        return None
    ratio = highest_peak_rps / lowest_peak_rps
    if ratio >= _FLAT_SCALING_RATIO_THRESHOLD:
        return None
    return (
        f"> **This run shows little to no benefit from added workers** "
        f"(REST throughput at peak concurrency went from {lowest_peak_rps:.1f} req/s "
        f"at {lowest.workers} worker(s) to {highest_peak_rps:.1f} req/s at "
        f"{highest.workers} worker(s) — a {ratio:.2f}x change). That is the same "
        "symptom this benchmark's own build process hit from unrelated CPU "
        "contention — see [LOAD_BENCHMARK.md](LOAD_BENCHMARK.md)'s \"A result is "
        'only as clean as the machine it ran on" section before concluding '
        "anything about QueryGate from this run."
    )


def _render_markdown(report: LoadBenchmarkReport) -> str:
    """A fresh, publish-ready results snapshot for THIS run only — data, not
    analysis. Interpretation (why the shape looks the way it does, the
    single-worker-ceiling explanation, the "only as clean as the machine it
    ran on" caveat) lives in the hand-maintained ``LOAD_BENCHMARK.md``, which
    this file links to rather than duplicates, so a regenerated results file
    can never silently overwrite curated methodology prose. The one exception
    is `_scaling_caution`: a data-derived (not hand-written) flag when THIS
    run's own numbers show the flat-scaling symptom that section warns about,
    so a design partner reading a bad-machine run doesn't miss the warning
    that lives one file away."""
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "# QueryGate load benchmark — latest results",
        "",
        "**Auto-generated by `make load-benchmark` — do not hand-edit; it is",
        "overwritten on every run.** Data only, no interpretation — see",
        "[LOAD_BENCHMARK.md](LOAD_BENCHMARK.md) for methodology, and read its",
        '"A result is only as clean as the machine it ran on" section before',
        "quoting any number below: throughput-under-concurrency is only as",
        "trustworthy as the CPU is uncontended, so treat this as one sample",
        "from the machine and moment noted here, not a certified figure.",
        "",
    ]
    caution = _scaling_caution(report)
    if caution is not None:
        lines.append(caution)
        lines.append("")
    lines.append(f"- **Generated:** {generated_at}")
    lines += [
        # CPU count/OS only, deliberately no hostname — enough context for a
        # reader to judge trustworthiness (a 2-core CI runner vs. a beefy
        # workstation) without a machine-identifying detail in a
        # publish-ready file.
        f"- **Host context:** {os.cpu_count()} logical CPUs, {platform.system()} {platform.machine()}",
        f"- **Seeded rows:** {report.seeded_rows} · **Requests/slot:** {report.requests_per_slot} "
        f"· **Dialect:** {report.dialect} · **Policy max_concurrency:** {report.max_concurrency}",
        "",
    ]
    for sweep in report.sweeps:
        lines.append(f"## workers = {sweep.workers}")
        lines.append("")
        lines.append(
            "| Concurrency | Requests | Baseline req/s | Baseline mean ms | Baseline p95 ms "
            "| REST req/s | REST mean ms | REST p95 ms | Errors (baseline/rest) "
            "| REST overhead (ms) | REST throughput ratio |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for level in sweep.levels:
            lines.append(
                f"| {level.concurrency} | {level.requests} "
                f"| {level.baseline.throughput_rps:.1f} | {level.baseline.latency.mean_ms:.2f} "
                f"| {level.baseline.latency.p95_ms:.2f} "
                f"| {level.rest.throughput_rps:.1f} | {level.rest.latency.mean_ms:.2f} "
                f"| {level.rest.latency.p95_ms:.2f} "
                f"| {level.baseline.errors}/{level.rest.errors} "
                f"| +{level.rest_overhead_ms:.2f} | {level.throughput_ratio * 100:.0f}% |"
            )
        lines.append("")
    lines.append(
        "All figures are wall-clock, over a real socket against a real `uvicorn` server. "
        "Regenerate with `make load-benchmark`."
    )
    return "\n".join(lines) + "\n"


def _cmd_run(args: argparse.Namespace) -> int:
    report = asyncio.run(
        run_load_benchmark(
            connection_string=args.connection_string,
            rows=args.rows,
            concurrency_levels=args.concurrency,
            worker_counts=args.workers,
            requests_per_slot=args.requests_per_slot,
        )
    )

    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        _print_report(report)

    if args.markdown_out is not None:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.write_text(_render_markdown(report))
        print(f"\nWrote {args.markdown_out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="querygate-load-benchmark",
        description=(
            "Measure QueryGate's throughput and latency under concurrent load, over a "
            "real socket against a real server, across a worker-count sweep."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run the benchmark and print a report.")
    run_p.add_argument(
        "--connection-string",
        default=DEFAULT_CONNECTION_STRING,
        # Never interpolate DEFAULT_CONNECTION_STRING into help text: if an
        # operator has exported QUERYGATE_DEMO_URL with a real credential to
        # point this at a non-demo database, `--help` would print it to
        # stdout / a CI log (2026-08-11 security-invariant-reviewer finding).
        help=(
            "Postgres connection string (default: the local demo DB from "
            "`make compose-up`, or $QUERYGATE_DEMO_URL if set)."
        ),
    )
    run_p.add_argument(
        "--rows", type=int, default=5000, help="Rows to seed in the benchmark table."
    )
    run_p.add_argument(
        "--concurrency",
        type=int,
        nargs="+",
        default=list(DEFAULT_CONCURRENCY_LEVELS),
        help=f"Concurrency levels to sweep (default: {list(DEFAULT_CONCURRENCY_LEVELS)}).",
    )
    run_p.add_argument(
        "--workers",
        type=int,
        nargs="+",
        default=list(DEFAULT_WORKER_COUNTS),
        help=(
            f"uvicorn worker-process counts to sweep (default: {list(DEFAULT_WORKER_COUNTS)}). "
            "The full concurrency sweep runs once per worker count, against a freshly "
            "started server."
        ),
    )
    run_p.add_argument(
        "--requests-per-slot",
        type=int,
        default=DEFAULT_REQUESTS_PER_SLOT,
        help=(
            "Requests fired per concurrency slot at each level (total requests = "
            f"concurrency * this, default {DEFAULT_REQUESTS_PER_SLOT})."
        ),
    )
    run_p.add_argument("--json", action="store_true", help="Emit the machine-readable report.")
    run_p.add_argument(
        "--markdown-out",
        type=Path,
        default=DEFAULT_MARKDOWN_PATH,
        help=(
            "Write a fresh, publish-ready Markdown results snapshot here on every run "
            f"(default: {DEFAULT_MARKDOWN_PATH.relative_to(_REPO_ROOT)}, overwritten each "
            "time). Pass --no-markdown to skip."
        ),
    )
    run_p.add_argument(
        "--no-markdown",
        dest="markdown_out",
        action="store_const",
        const=None,
        help="Skip writing the Markdown results snapshot.",
    )
    run_p.set_defaults(func=_cmd_run)

    return parser


def _handle_termination_signal(signum: int, frame) -> None:
    """SIGTERM (a CI job timeout, `kill <pid>`, an orchestrator stopping this
    process) does not raise a Python exception by default — the try/finally
    in `run_load_benchmark` that stops a spawned server never runs, leaving
    an anonymous-auth uvicorn orphaned indefinitely (2026-08-11
    security-invariant-reviewer finding). Best-effort stop every server this
    process has spawned, then restore the default disposition and re-raise
    so the process still terminates the normal way — this closes the common
    "CI killed the benchmark" case; a SIGKILL (which no process can catch)
    is a residual gap disclosed in LOAD_BENCHMARK.md rather than something
    this handler can close."""
    load_benchmark._stop_all_active_servers()
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGTERM, _handle_termination_signal)
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
