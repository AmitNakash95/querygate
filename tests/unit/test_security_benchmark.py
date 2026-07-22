"""Tests for the reproducible adversarial security benchmark (TODO.md item 58).

These lock in the two properties that make the benchmark publishable: it drives
the *real* guardrails (so a 100% catch rate is meaningful, not stubbed) and it
is honest (residuals disclosed, baseline not inflated, and the no-raw-SQL check
actually detects a regression).
"""

from __future__ import annotations

import pytest

from querygate import security_benchmark as sb
from querygate.security_benchmark import (
    BenchmarkOutcome,
    SecurityBenchmarkCase,
    load_corpus,
    run_security_benchmark,
)
from querygate.security_benchmark_cli import main as cli_main

pytestmark = pytest.mark.unit


def test_default_corpus_loads_with_unique_ids():
    corpus = load_corpus(sb.DEFAULT_CORPUS)
    assert corpus.corpus_id == "security_boundary_v1"
    assert len(corpus.cases) >= 14
    ids = [c.id for c in corpus.cases]
    assert len(ids) == len(set(ids))


def test_duplicate_case_ids_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        sb.SecurityBenchmarkCorpus(
            version=1,
            corpus_id="dup",
            description="x",
            cases=[
                _minimal_no_raw_sql_case("a"),
                _minimal_no_raw_sql_case("a"),
            ],
        )


def _minimal_no_raw_sql_case(case_id: str) -> SecurityBenchmarkCase:
    return SecurityBenchmarkCase(
        id=case_id,
        category="structural_invariant",
        title="t",
        check="no_raw_sql_field",
        querygate=BenchmarkOutcome(blocks=True),
        raw_sql_baseline=BenchmarkOutcome(blocks=False),
    )


def test_full_benchmark_is_clean_and_drives_real_guardrails():
    report = run_security_benchmark()
    # Every declared attack is actually caught by the real pipeline.
    assert report.querygate_caught == report.attack_count
    assert report.querygate_catch_rate == 1.0
    assert report.querygate_mismatches == 0
    assert report.ok is True
    # The structural raw-SQL baseline catches none of the structural attacks.
    assert report.baseline_caught == 0
    assert report.baseline_catch_rate == 0.0
    # Residuals are present and disclosed, never counted as attacks.
    assert report.residual_count >= 2


def test_benchmark_verdicts_are_deterministic():
    a = run_security_benchmark()
    b = run_security_benchmark()
    verdicts_a = {c.id: (c.querygate_blocked, c.querygate_matched) for c in a.cases}
    verdicts_b = {c.id: (c.querygate_blocked, c.querygate_matched) for c in b.cases}
    assert verdicts_a == verdicts_b


def test_every_attack_case_declares_a_vulnerable_baseline():
    # The comparison must not be inflated: an attack QueryGate is credited for
    # catching must be one the naive baseline genuinely does not block.
    corpus = load_corpus(sb.DEFAULT_CORPUS)
    for case in corpus.cases:
        if case.should_block:
            assert case.querygate.blocks is True, case.id
            assert case.raw_sql_baseline.blocks is False, case.id


def test_bound_parameter_check_neutralizes_injection():
    report = run_security_benchmark()
    sqli = next(c for c in report.cases if c.id == "sqli-predicate-eq-value")
    assert sqli.querygate_blocked is True
    assert "bound parameter" in sqli.detail


def test_no_raw_sql_field_check_detects_an_escape_hatch(monkeypatch):
    # Simulate a regression that adds a raw-SQL field to the request AST; the
    # structural check must flip to NOT blocked.
    from querygate.query_ast.models import StructuredQuery

    real_schema = StructuredQuery.model_json_schema()

    def _leaky_schema(*args, **kwargs):
        schema = dict(real_schema)
        schema["properties"] = {**schema.get("properties", {}), "raw_sql": {"type": "string"}}
        return schema

    monkeypatch.setattr(StructuredQuery, "model_json_schema", _leaky_schema)
    case = _minimal_no_raw_sql_case("leak")
    blocked, detail = sb._check_no_raw_sql_field(case)
    assert blocked is False
    assert "raw_sql" in detail


def test_cli_run_exits_zero_on_clean_run(capsys):
    rc = cli_main(["run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "100.0%" in out
    assert "clean" in out.lower()


def test_cli_json_output_is_machine_readable(capsys):
    rc = cli_main(["run", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"querygate_catch_rate"' in out
