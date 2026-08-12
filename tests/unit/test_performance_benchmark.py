"""Tests for the performance benchmark's non-DB-dependent surface.

The benchmark itself needs a real Postgres instance (see
``tests/integration/test_performance_benchmark_live.py``), but the scenario
definitions, latency statistics, and CLI wiring are pure and worth locking in
here so a bad edit is caught without `make compose-up`.
"""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

import querygate.load_benchmark as load_benchmark_module
import querygate.performance_benchmark as performance_benchmark_module
from querygate.performance_benchmark import (
    SCENARIOS,
    DEFAULT_CONNECTION_STRING,
    LatencyStats,
    PerformanceBenchmarkReport,
    ScenarioResult,
    run_performance_benchmark,
)
from querygate.performance_benchmark_cli import (
    DEFAULT_MARKDOWN_PATH,
    _cmd_run,
    _render_markdown,
    build_parser,
)
from querygate.query_ast.models import StructuredQuery

pytestmark = pytest.mark.unit


def test_latency_stats_from_samples_computes_percentiles():
    # 21 ordered samples so p50/p95/p99 land on three DIFFERENT indices —
    # too small a fixture (e.g. 5 elements) can't actually distinguish a
    # p95/p99 fraction-swap bug, since both round to the same last index
    # (2026-08-11 test-contract-reviewer finding).
    values = [float(i) for i in range(1, 22)]  # 1.0 .. 21.0
    stats = LatencyStats.from_samples(values)
    assert stats.samples == 21
    assert stats.mean_ms == pytest.approx(11.0)
    assert stats.min_ms == 1.0
    assert stats.max_ms == 21.0
    # index = round(fraction * (n - 1)); n=21 -> n-1=20.
    assert stats.p50_ms == values[round(0.50 * 20)] == 11.0
    assert stats.p95_ms == values[round(0.95 * 20)] == 20.0
    assert stats.p99_ms == values[round(0.99 * 20)] == 21.0
    assert stats.p50_ms != stats.p95_ms != stats.p99_ms


def test_scenario_overhead_is_rest_minus_baseline():
    result = ScenarioResult(
        name="x",
        description="d",
        iterations=1,
        baseline=LatencyStats.from_samples([10.0]),
        pipeline=LatencyStats.from_samples([12.0]),
        rest=LatencyStats.from_samples([15.0]),
    )
    assert result.pipeline_overhead_ms == pytest.approx(2.0)
    assert result.rest_overhead_ms == pytest.approx(5.0)
    assert result.rest_overhead_pct == pytest.approx(50.0)


def test_scenario_overhead_pct_handles_zero_baseline():
    result = ScenarioResult(
        name="x",
        description="d",
        iterations=1,
        baseline=LatencyStats.from_samples([0.0]),
        pipeline=LatencyStats.from_samples([1.0]),
        rest=LatencyStats.from_samples([1.0]),
    )
    assert result.rest_overhead_pct == 0.0


def test_report_mean_overhead_across_scenarios():
    def _result(baseline_ms: float, rest_ms: float) -> ScenarioResult:
        return ScenarioResult(
            name="x",
            description="d",
            iterations=1,
            baseline=LatencyStats.from_samples([baseline_ms]),
            pipeline=LatencyStats.from_samples([baseline_ms]),
            rest=LatencyStats.from_samples([rest_ms]),
        )

    report = PerformanceBenchmarkReport(
        seeded_rows=1,
        iterations_per_scenario=1,
        warmup_per_scenario=0,
        scenarios=[_result(10.0, 12.0), _result(10.0, 16.0)],
    )
    assert report.mean_rest_overhead_ms == pytest.approx(4.0)


def test_report_mean_overhead_with_no_scenarios_is_zero():
    report = PerformanceBenchmarkReport(
        seeded_rows=1, iterations_per_scenario=1, warmup_per_scenario=0, scenarios=[]
    )
    assert report.mean_rest_overhead_ms == 0.0
    assert report.mean_pipeline_overhead_ms == 0.0


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
@pytest.mark.parametrize("seed", [0, 1, 4999])
def test_every_scenario_structured_query_validates(scenario, seed):
    """Every scenario's AST payload must itself be a valid StructuredQuery —
    a regression here would mean the benchmark silently measures a rejected
    request instead of a real one."""
    values = scenario.values(seed)
    payload = scenario.structured_query("querygate_perf_bench_test", values)
    query = StructuredQuery.model_validate(payload)
    assert query.from_table == "querygate_perf_bench_test"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_every_scenario_raw_sql_references_the_table(scenario):
    values = scenario.values(0)
    stmt, _params = scenario.raw_sql("querygate_perf_bench_test", values)
    assert "querygate_perf_bench_test" in str(stmt)


def _collect_where_literal_values(node: Optional[Dict[str, Any]]) -> List[Any]:
    """Recursively pull every predicate `value` out of a StructuredQuery
    `where` node, unwrapping `and`/`or` groups. `None` (no where clause)
    yields no values."""
    if node is None:
        return []
    collected: List[Any] = []
    if "value" in node:
        collected.append(node["value"])
    for combinator in ("and", "or"):
        for child in node.get(combinator, []):
            collected.extend(_collect_where_literal_values(child))
    return collected


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
@pytest.mark.parametrize("seed", [0, 7, 4999])
def test_raw_sql_and_structured_query_time_the_identical_values(scenario, seed):
    """The benchmark's entire premise is that `baseline` and
    `pipeline`/`rest` measure the SAME logical query per iteration. Earlier
    versions had `raw_sql()`/`structured_query()` each independently
    recompute their literals from `seed` — nothing caught the two silently
    drifting apart (2026-08-11 test-contract-reviewer finding: neither this
    file's prior tests nor the real-Postgres integration test read the
    other method's output). `values()` is now the single source of truth
    both consume via `for_seed()`; this locks that contract down instead of
    just trusting the refactor."""
    values = scenario.values(seed)
    (_stmt, raw_params), payload = scenario.for_seed("querygate_perf_bench_test", seed)
    assert raw_params == values
    where_values = _collect_where_literal_values(payload.get("where"))
    assert sorted(where_values, key=str) == sorted(values.values(), key=str)


def test_scenario_names_are_unique():
    names = [s.name for s in SCENARIOS]
    assert len(names) == len(set(names))


@pytest.mark.asyncio
async def test_non_postgres_connection_string_is_rejected_before_any_engine_work():
    """The `create_async_engine` exemption for this module in
    `test_connections_engine.py` is only true because a non-Postgres
    connection string can never reach `create_async_engine` here — this is
    the enforcement that makes that claim real rather than aspirational
    (2026-08-11 security-invariant-reviewer finding). Needs no database: the
    rejection happens at URL-parsing time, before either engine is built."""
    with pytest.raises(ValueError, match="Postgres"):
        await run_performance_benchmark(connection_string="snowflake://user:pass@acct/db")


@pytest.mark.asyncio
async def test_excessive_rows_is_rejected_before_any_engine_work():
    """Sibling guard to load_benchmark.py's identical `_MAX_ROWS` check
    (2026-08-11 security-invariant-reviewer finding: this module had no
    upper bound on `rows` at all — `--rows 100000000` would materialize a
    100M-element list in memory before ever touching the database)."""
    with (
        patch("querygate.performance_benchmark.create_async_engine") as mock_engine,
        pytest.raises(ValueError, match="at most"),
    ):
        await run_performance_benchmark(rows=10_000_000)
    mock_engine.assert_not_called()


@pytest.mark.asyncio
async def test_non_positive_rows_is_rejected_before_any_engine_work():
    with (
        patch("querygate.performance_benchmark.create_async_engine") as mock_engine,
        pytest.raises(ValueError, match="at least"),
    ):
        await run_performance_benchmark(rows=0)
    mock_engine.assert_not_called()


def test_cli_run_parser_defaults():
    parser = build_parser()
    args = parser.parse_args(["run"])
    assert args.rows == 5000
    assert args.iterations == 30
    assert args.warmup == 5
    assert args.json is False
    assert args.max_overhead_ms is None


def test_help_text_never_prints_the_default_connection_string():
    """See the identical test in test_load_benchmark.py — same fix, same
    reason, both CLIs (2026-08-11 security-invariant-reviewer finding)."""
    assert DEFAULT_CONNECTION_STRING not in build_parser().format_help()


def test_cli_run_parser_overrides():
    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--rows",
            "100",
            "--iterations",
            "3",
            "--warmup",
            "1",
            "--json",
            "--max-overhead-ms",
            "50",
        ]
    )
    assert args.rows == 100
    assert args.iterations == 3
    assert args.warmup == 1
    assert args.json is True
    assert args.max_overhead_ms == 50.0


def test_cli_markdown_out_defaults_to_the_docs_path():
    parser = build_parser()
    args = parser.parse_args(["run"])
    assert args.markdown_out == DEFAULT_MARKDOWN_PATH


def test_cli_no_markdown_disables_the_default():
    parser = build_parser()
    args = parser.parse_args(["run", "--no-markdown"])
    assert args.markdown_out is None


def test_cli_markdown_out_overrides_the_path():
    parser = build_parser()
    args = parser.parse_args(["run", "--markdown-out", "/tmp/custom.md"])
    assert args.markdown_out == Path("/tmp/custom.md")


def _sample_report() -> PerformanceBenchmarkReport:
    def _result(name: str) -> ScenarioResult:
        return ScenarioResult(
            name=name,
            description="a scenario",
            iterations=1,
            baseline=LatencyStats.from_samples([2.0]),
            pipeline=LatencyStats.from_samples([3.0]),
            rest=LatencyStats.from_samples([5.0]),
        )

    return PerformanceBenchmarkReport(
        dialect="postgresql",
        seeded_rows=100,
        iterations_per_scenario=1,
        warmup_per_scenario=0,
        scenarios=[_result("point_lookup")],
    )


def test_render_markdown_contains_the_report_data_and_not_hand_edit_banner():
    markdown = _render_markdown(_sample_report())
    assert "do not hand-edit" in markdown
    assert "point_lookup" in markdown
    assert "Host context:" in markdown


def test_render_markdown_never_claims_to_be_analysis():
    """The auto-generated file is data only — it must point to the
    hand-maintained methodology doc rather than assert its own
    interpretation of the numbers."""
    markdown = _render_markdown(_sample_report())
    assert "PERFORMANCE_BENCHMARK.md" in markdown


def _run_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        connection_string="postgresql+asyncpg://x/y",
        rows=100,
        iterations=1,
        warmup=0,
        json=False,
        markdown_out=None,
        max_overhead_ms=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_cmd_run_actually_writes_the_markdown_file_to_disk(tmp_path):
    """`--markdown-out`'s CLI wiring, not just `_render_markdown`'s content,
    must be exercised: a bug in `_cmd_run` itself (wrong attribute name, path
    never created, write skipped) would pass every `_render_markdown`-only
    test above while never actually producing the file a user asked for."""
    out_path = tmp_path / "nested" / "results.md"

    async def _fake_run_performance_benchmark(**kwargs):
        return _sample_report()

    with patch(
        "querygate.performance_benchmark_cli.run_performance_benchmark",
        _fake_run_performance_benchmark,
    ):
        exit_code = _cmd_run(_run_args(markdown_out=out_path))

    assert exit_code == 0
    assert out_path.exists()
    assert "do not hand-edit" in out_path.read_text()


def test_cmd_run_fails_when_a_scenario_exceeds_max_overhead_ms(capsys):
    """PERFORMANCE_BENCHMARK.md documents `--max-overhead-ms` as the one
    exception to "exit code is always 0" — the code path
    (`performance_benchmark_cli.py`'s `_cmd_run`) had no test proving it
    actually returns 1 when a scenario exceeds the threshold (2026-08-11
    claim-reviewer finding). `_sample_report()`'s one scenario has
    rest_overhead_ms == 5.0 - 2.0 == 3.0ms."""

    async def _fake_run_performance_benchmark(**kwargs):
        return _sample_report()

    with patch(
        "querygate.performance_benchmark_cli.run_performance_benchmark",
        _fake_run_performance_benchmark,
    ):
        exit_code = _cmd_run(_run_args(max_overhead_ms=1.0))

    assert exit_code == 1
    assert "RESULT: FAILED" in capsys.readouterr().out


def test_cmd_run_passes_when_no_scenario_exceeds_max_overhead_ms():
    async def _fake_run_performance_benchmark(**kwargs):
        return _sample_report()

    with patch(
        "querygate.performance_benchmark_cli.run_performance_benchmark",
        _fake_run_performance_benchmark,
    ):
        exit_code = _cmd_run(_run_args(max_overhead_ms=10.0))

    assert exit_code == 0


def test_cmd_run_skips_the_markdown_file_when_disabled(tmp_path):
    out_path = tmp_path / "results.md"

    async def _fake_run_performance_benchmark(**kwargs):
        return _sample_report()

    with patch(
        "querygate.performance_benchmark_cli.run_performance_benchmark",
        _fake_run_performance_benchmark,
    ):
        exit_code = _cmd_run(_run_args(markdown_out=None))

    assert exit_code == 0
    assert not out_path.exists()


@pytest.mark.asyncio
async def test_benchmark_environment_resets_the_reentrancy_guard_when_engine_construction_fails():
    """A previous version set `_benchmark_active = True` before entering any
    try/finally — an exception raised by engine construction itself (a
    malformed connection_string, a driver import error) left the flag stuck
    True forever, making every later call in the same process falsely
    report "already running" instead of surfacing the real error
    (2026-08-11 security-invariant-reviewer finding). Uses the real
    `create_async_engine` for the admin engine (safe: construction is lazy,
    no network I/O) and only fails the second (baseline) construction."""
    call_count = 0
    real_create_async_engine = performance_benchmark_module.create_async_engine

    def _flaky_create_async_engine(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("simulated engine construction failure")
        return real_create_async_engine(*args, **kwargs)

    assert performance_benchmark_module._benchmark_active is False

    with patch(
        "querygate.performance_benchmark.create_async_engine",
        _flaky_create_async_engine,
    ):
        with pytest.raises(RuntimeError, match="simulated engine construction failure"):
            async with performance_benchmark_module._benchmark_environment(
                DEFAULT_CONNECTION_STRING, rows=10
            ):
                pass

    assert performance_benchmark_module._benchmark_active is False

    # A second call must not falsely report "already running" — it must
    # reach and raise the SAME real error again, proving the guard was
    # actually cleared rather than merely not yet observed.
    with patch(
        "querygate.performance_benchmark.create_async_engine",
        _flaky_create_async_engine,
    ):
        call_count = 0
        with pytest.raises(RuntimeError, match="simulated engine construction failure"):
            async with performance_benchmark_module._benchmark_environment(
                DEFAULT_CONNECTION_STRING, rows=10
            ):
                pass


# --------------------------------------------------------------------------- #
# SAST suppression placement
# --------------------------------------------------------------------------- #

# Bandit's own B608 trigger (bandit/plugins/injection_sql.py). Kept as a literal
# rather than imported so this test states the contract it enforces.
_BANDIT_SIMPLE_SQL_RE = re.compile(
    r"(select\s.*from\s|delete\s+from\s|insert\s+into\s.*values[\s(]|update\s.*set\s)",
    re.IGNORECASE | re.DOTALL,
)

_SAST_MARKER_MODULES = [
    Path(performance_benchmark_module.__file__),
    Path(load_benchmark_module.__file__),
]


@pytest.mark.unit
@pytest.mark.parametrize("module_path", _SAST_MARKER_MODULES, ids=lambda p: p.name)
def test_every_sql_string_bandit_would_flag_carries_an_in_range_nosec(module_path: Path) -> None:
    """A `# nosec B608` must sit INSIDE the f-string bandit anchors to.

    Bandit resolves `# nosec` against the *string* node's own line range (its
    parent `JoinedStr`), not the enclosing statement — so a marker on an
    ``sql = (  # nosec B608`` assignment line silently stops suppressing. That
    is not hypothetical: restructuring these modules for semgrep on 2026-08-11
    moved three markers exactly one line out of range, and `bandit -r src/`
    went to exit 1 with nothing in the test suite noticing.

    Semgrep has the opposite convention (anchors to the match's FIRST line),
    which is why the `sa.text(...)` calls are pinned with `# fmt: off`. The two
    tools are not interchangeable; this test guards the bandit half.
    """
    source = module_path.read_text()
    lines = source.splitlines()
    tree = ast.parse(source)

    unguarded: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        text = "".join(part.value for part in node.values if isinstance(part, ast.Constant))
        if not _BANDIT_SIMPLE_SQL_RE.search(text):
            continue
        span = range(node.lineno, (node.end_lineno or node.lineno) + 1)
        if not any("# nosec B608" in lines[n - 1] for n in span):
            unguarded.append(f"{module_path.name}:{node.lineno} -> {text[:60]!r}")

    assert not unguarded, (
        "SQL f-string(s) bandit flags as B608 with no `# nosec B608` inside the "
        "string's own line range — bandit will fail:\n  " + "\n  ".join(unguarded)
    )


@pytest.mark.unit
@pytest.mark.parametrize("module_path", _SAST_MARKER_MODULES, ids=lambda p: p.name)
def test_no_inert_nosec_b608_markers(module_path: Path) -> None:
    """Every `# nosec B608` must actually suppress something.

    B608 matches only select/delete/insert/update, so a marker on DDL (CREATE
    TABLE, CREATE INDEX, ANALYZE, DROP) suppresses nothing while reading as
    "reviewed and accepted" — which is what made the three genuinely-broken
    markers above hard to spot by eye.
    """
    source = module_path.read_text()
    lines = source.splitlines()
    tree = ast.parse(source)

    covered: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        text = "".join(part.value for part in node.values if isinstance(part, ast.Constant))
        if _BANDIT_SIMPLE_SQL_RE.search(text):
            covered.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))

    inert = [
        f"{module_path.name}:{i + 1}"
        for i, line in enumerate(lines)
        if "# nosec B608" in line and (i + 1) not in covered
    ]
    assert not inert, "Inert `# nosec B608` marker(s) suppressing nothing: " + ", ".join(inert)
