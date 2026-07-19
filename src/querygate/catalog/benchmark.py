"""Versioned deterministic semantic-memory benchmark and fixed MVP thresholds."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional

import pydantic as pyd
import yaml

from querygate.catalog.loader import CatalogStore
from querygate.catalog.retrieval import CatalogSearchHit, search_catalog
from querygate.policy.models import Policy

BENCHMARK_VERSION = 1
THRESHOLD_VERSION = "32a-2-mvp-1"

# Declared in source rather than fixture data so an evaluation corpus cannot
# lower the bar after its results are known.
MIN_EXPECTED_HIT_RECALL = 0.85
MIN_RELATIONSHIP_RECALL = 0.80
MIN_STALE_DETECTION_RATE = 1.0
MIN_DISCOVERY_CALL_REDUCTION = 0.50
MAX_POLICY_VIOLATIONS = 0


class BenchmarkExpectedHit(pyd.BaseModel):
    object_type: Literal["table", "column", "relationship"]
    table: str
    column: Optional[str] = None
    to_table: Optional[str] = None
    to_column: Optional[str] = None
    freshness: Optional[Literal["current", "stale", "untracked"]] = None

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)


class SemanticMemoryBenchmarkCase(pyd.BaseModel):
    case_id: str
    query: str
    policy: Policy = pyd.Field(default_factory=Policy)
    expected_hits: list[BenchmarkExpectedHit] = pyd.Field(min_length=1, max_length=20)
    forbidden_identifiers: list[str] = pyd.Field(default_factory=list, max_length=20)
    baseline_discovery_calls: int = pyd.Field(ge=1, le=10)

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)


class SemanticMemoryBenchmark(pyd.BaseModel):
    version: Literal[1]
    threshold_version: Literal["32a-2-mvp-1"]
    connection_id: str = "benchmark"
    catalog: dict[str, Any]
    cases: list[SemanticMemoryBenchmarkCase] = pyd.Field(min_length=1, max_length=100)

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)


class BenchmarkCaseResult(pyd.BaseModel):
    case_id: str
    expected_count: int
    matched_count: int
    policy_violations: int

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)


class SemanticMemoryBenchmarkReport(pyd.BaseModel):
    benchmark_version: int
    threshold_version: str
    case_count: int
    expected_hit_recall: float
    relationship_recall: float
    stale_detection_rate: float
    discovery_call_reduction: float
    policy_violations: int
    thresholds: dict[str, float | int]
    passed: bool
    cases: list[BenchmarkCaseResult]

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)


def load_benchmark(path: str) -> SemanticMemoryBenchmark:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    benchmark = SemanticMemoryBenchmark.model_validate(raw)
    # Validate the embedded catalog independently before any case runs.
    CatalogStore.from_dict(benchmark.catalog)
    return benchmark


def _hit_matches(expected: BenchmarkExpectedHit, hit: CatalogSearchHit) -> bool:
    return (
        hit.object_type.value == expected.object_type
        and hit.table.casefold() == expected.table.casefold()
        and (hit.column or "").casefold() == (expected.column or "").casefold()
        and (hit.to_table or "").casefold() == (expected.to_table or "").casefold()
        and (hit.to_column or "").casefold() == (expected.to_column or "").casefold()
        and (expected.freshness is None or hit.citation.freshness.value == expected.freshness)
    )


def _policy_violations(hits: list[CatalogSearchHit], identifiers: list[str]) -> int:
    serialized = json.dumps(
        [hit.model_dump(mode="json") for hit in hits], sort_keys=True
    ).casefold()
    return sum(identifier.casefold() in serialized for identifier in identifiers)


def evaluate_benchmark(
    benchmark: SemanticMemoryBenchmark,
) -> SemanticMemoryBenchmarkReport:
    store = CatalogStore.from_dict(benchmark.catalog)
    case_results: list[BenchmarkCaseResult] = []
    expected_total = 0
    matched_total = 0
    relationship_total = 0
    relationship_matched = 0
    stale_total = 0
    stale_matched = 0
    policy_violations = 0
    baseline_calls = 0

    for case in benchmark.cases:
        response = search_catalog(
            store,
            connection_id=benchmark.connection_id,
            policy=case.policy,
            query=case.query,
            max_results=20,
        )
        matched = 0
        for expected in case.expected_hits:
            was_matched = any(_hit_matches(expected, hit) for hit in response.results)
            expected_total += 1
            matched_total += int(was_matched)
            matched += int(was_matched)
            if expected.object_type == "relationship":
                relationship_total += 1
                relationship_matched += int(was_matched)
            if expected.freshness == "stale":
                stale_total += 1
                stale_matched += int(was_matched)
        violations = _policy_violations(response.results, case.forbidden_identifiers)
        policy_violations += violations
        baseline_calls += case.baseline_discovery_calls
        case_results.append(
            BenchmarkCaseResult(
                case_id=case.case_id,
                expected_count=len(case.expected_hits),
                matched_count=matched,
                policy_violations=violations,
            )
        )

    expected_hit_recall = matched_total / expected_total
    relationship_recall = relationship_matched / relationship_total if relationship_total else 1.0
    stale_detection_rate = stale_matched / stale_total if stale_total else 1.0
    semantic_calls = len(benchmark.cases)
    discovery_call_reduction = max(0.0, 1.0 - (semantic_calls / baseline_calls))
    thresholds: dict[str, float | int] = {
        "min_expected_hit_recall": MIN_EXPECTED_HIT_RECALL,
        "min_relationship_recall": MIN_RELATIONSHIP_RECALL,
        "min_stale_detection_rate": MIN_STALE_DETECTION_RATE,
        "min_discovery_call_reduction": MIN_DISCOVERY_CALL_REDUCTION,
        "max_policy_violations": MAX_POLICY_VIOLATIONS,
    }
    passed = (
        expected_hit_recall >= MIN_EXPECTED_HIT_RECALL
        and relationship_recall >= MIN_RELATIONSHIP_RECALL
        and stale_detection_rate >= MIN_STALE_DETECTION_RATE
        and discovery_call_reduction >= MIN_DISCOVERY_CALL_REDUCTION
        and policy_violations <= MAX_POLICY_VIOLATIONS
    )
    return SemanticMemoryBenchmarkReport(
        benchmark_version=benchmark.version,
        threshold_version=benchmark.threshold_version,
        case_count=len(benchmark.cases),
        expected_hit_recall=expected_hit_recall,
        relationship_recall=relationship_recall,
        stale_detection_rate=stale_detection_rate,
        discovery_call_reduction=discovery_call_reduction,
        policy_violations=policy_violations,
        thresholds=thresholds,
        passed=passed,
        cases=case_results,
    )
