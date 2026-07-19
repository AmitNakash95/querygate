"""Deterministic semantic-memory MVP benchmark tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from querygate.catalog.benchmark import (
    MAX_POLICY_VIOLATIONS,
    MIN_DISCOVERY_CALL_REDUCTION,
    MIN_EXPECTED_HIT_RECALL,
    MIN_RELATIONSHIP_RECALL,
    MIN_STALE_DETECTION_RATE,
    SemanticMemoryBenchmark,
    evaluate_benchmark,
    load_benchmark,
)
from querygate.catalog_cli import default_benchmark_path

BENCHMARK_PATH = Path(__file__).parents[2] / "benchmarks" / "semantic_memory_v1.yaml"


def test_versioned_semantic_memory_benchmark_meets_predeclared_thresholds():
    report = evaluate_benchmark(load_benchmark(str(BENCHMARK_PATH)))

    assert report.passed
    assert report.case_count == 4
    assert report.thresholds == {
        "min_expected_hit_recall": MIN_EXPECTED_HIT_RECALL,
        "min_relationship_recall": MIN_RELATIONSHIP_RECALL,
        "min_stale_detection_rate": MIN_STALE_DETECTION_RATE,
        "min_discovery_call_reduction": MIN_DISCOVERY_CALL_REDUCTION,
        "max_policy_violations": MAX_POLICY_VIOLATIONS,
    }
    assert report.policy_violations == 0


def test_repository_and_packaged_benchmark_copies_are_semantically_identical():
    repository = load_benchmark(str(BENCHMARK_PATH))
    packaged = load_benchmark(default_benchmark_path())
    assert repository == packaged


def test_benchmark_fixture_cannot_override_release_thresholds():
    raw = load_benchmark(str(BENCHMARK_PATH)).model_dump(mode="json")
    raw["thresholds"] = {"min_expected_hit_recall": 0}

    with pytest.raises(Exception):
        SemanticMemoryBenchmark.model_validate(raw)


def test_benchmark_fails_when_expected_selection_is_wrong():
    benchmark = load_benchmark(str(BENCHMARK_PATH))
    raw = benchmark.model_dump(mode="json")
    raw["cases"][0]["expected_hits"][0]["table"] = "invented_table"

    report = evaluate_benchmark(SemanticMemoryBenchmark.model_validate(raw))

    assert not report.passed
    assert report.expected_hit_recall < MIN_EXPECTED_HIT_RECALL
