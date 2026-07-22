"""`querygate-security-benchmark` — run the adversarial boundary benchmark.

TODO.md item 58 (phase 1). Runs the fixed, versioned attack corpus
(`benchmarks/security_boundary_v1.yaml` by default) against QueryGate's real
request-pipeline guardrails and prints catch rate, the structurally-modeled
raw-SQL-passthrough baseline, and per-query guardrail latency.

Subcommands:

- ``run`` — execute the corpus and print a human-readable report (or ``--json``
  for the machine-readable `SecurityBenchmarkReport`). Exit code is 0 iff the
  run is clean (no regressions and every attack caught), so it can gate a
  release or a periodic integrity job.
- ``list`` — list the corpus cases without running them.

The benchmark is offline and deterministic (no DB, no network, no LLM); see
``docs/business/SECURITY_BENCHMARK.md`` for methodology and comparison scope.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from querygate.security_benchmark import (
    DEFAULT_CORPUS,
    load_corpus,
    run_security_benchmark,
)


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _cmd_run(args: argparse.Namespace) -> int:
    corpus = load_corpus(args.corpus)
    report = run_security_benchmark(corpus)

    if args.json:
        print(report.model_dump_json(indent=2))
        return 0 if report.ok else 1

    print(f"QueryGate adversarial security benchmark — {report.corpus_id} v{report.corpus_version}")
    print("=" * 72)
    for case in report.cases:
        if case.should_block:
            mark = "PASS" if case.querygate_matched else "FAIL"
            verdict = "blocked" if case.querygate_blocked else "NOT BLOCKED"
        else:
            mark = "residual" if case.querygate_matched else "FAIL"
            verdict = "allowed by design"
        print(f"  [{mark:>8}] {case.id:<38} {verdict}")
        if args.verbose or not case.querygate_matched:
            print(f"             {case.detail}")

    print("-" * 72)
    print(f"Attack cases:            {report.attack_count}")
    print(
        f"QueryGate catch rate:    {report.querygate_caught}/{report.attack_count} "
        f"({_pct(report.querygate_catch_rate)})"
    )
    print(
        f"Raw-SQL baseline*:       {report.baseline_caught}/{report.attack_count} "
        f"({_pct(report.baseline_catch_rate)})"
    )
    print(f"Documented residuals:    {report.residual_count} (allowed by design, disclosed)")
    print(f"Regressions (mismatch):  {report.querygate_mismatches}")
    print(
        f"Guardrail latency:       mean {report.mean_guardrail_latency_ms:.3f} ms, "
        f"p95 {report.p95_guardrail_latency_ms:.3f} ms (before any DB round-trip)"
    )
    print("-" * 72)
    print(
        "* Structural model of a gateway that forwards a model-generated SQL string\n"
        "  with no AST contract — not a live competitor run. See\n"
        "  docs/business/SECURITY_BENCHMARK.md for methodology and scope."
    )
    if report.ok:
        print("\nRESULT: clean — every attack caught, no regressions.")
        return 0
    print("\nRESULT: FAILED — see the FAIL lines above.")
    return 1


def _cmd_list(args: argparse.Namespace) -> int:
    corpus = load_corpus(args.corpus)
    print(f"{corpus.corpus_id} v{corpus.version}: {len(corpus.cases)} case(s)")
    for case in corpus.cases:
        kind = "attack" if case.should_block else "residual"
        print(f"  {case.id:<38} [{kind}] ({case.category}) {case.title}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="querygate-security-benchmark",
        description="Run QueryGate's reproducible adversarial security benchmark.",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=DEFAULT_CORPUS,
        help=f"Path to the attack corpus YAML (default: {DEFAULT_CORPUS}).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run the corpus and print a report.")
    run_p.add_argument("--json", action="store_true", help="Emit the machine-readable report.")
    run_p.add_argument(
        "-v", "--verbose", action="store_true", help="Print each case's detail line."
    )
    run_p.set_defaults(func=_cmd_run)

    list_p = sub.add_parser("list", help="List corpus cases without running them.")
    list_p.set_defaults(func=_cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
