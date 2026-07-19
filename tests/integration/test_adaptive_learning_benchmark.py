"""TODO item 37: automated end-to-end proof of adaptive usage learning.

These tests drive `querygate.catalog.adaptive_learning_benchmark` (the same
runner behind `make adaptive-learning-test`) against the packaged
`adaptive_learning_v1.yaml` fixture. The happy-path test proves the real
lifecycle passes; the adversarial tests each break exactly one real
invariant and prove the checker actually notices — a checker that always
passes would not be an automated proof of anything.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from querygate.catalog import adaptive_learning_benchmark as alb
from querygate.catalog.adaptive_learning_benchmark import (
    default_adaptive_learning_benchmark_path,
    load_adaptive_learning_benchmark,
    run_adaptive_learning_benchmark,
)
from querygate.catalog import learning as learning_mod
from querygate.execution.service import StructuredQueryService

pytestmark = pytest.mark.integration


@pytest.fixture
def benchmark():
    return load_adaptive_learning_benchmark(default_adaptive_learning_benchmark_path())


def test_packaged_fixture_loads_and_validates():
    benchmark = load_adaptive_learning_benchmark(default_adaptive_learning_benchmark_path())
    assert benchmark.version == 1
    assert benchmark.threshold_version == "37-mvp-1"
    assert len(benchmark.evidence_groups) >= 5


def test_full_lifecycle_passes(benchmark, tmp_path):
    report = run_adaptive_learning_benchmark(benchmark, workdir=str(tmp_path))

    # The fixture was not simply pre-seeded with the answer: the task must
    # demonstrably fail before learning and succeed only afterward.
    assert report.baseline_correct is False
    assert report.learned_correct is True
    assert report.discovery_call_reduction >= alb.MIN_DISCOVERY_CALL_REDUCTION
    assert report.stale_detected is True
    assert report.principal_filtering_correct is True
    assert report.rollback_correct is True
    assert report.ordinary_query_unaffected_by_learner_failure is True
    assert report.duplicate_proposals == 0
    assert report.security_violations == 0
    assert report.passed is True


def test_report_never_carries_row_values_credentials_or_raw_errors(benchmark, tmp_path):
    """Redaction-safety of the report itself: only booleans/counts/floats
    and stable version identifiers, matching every other catalog surface's
    posture.
    """

    report = run_adaptive_learning_benchmark(benchmark, workdir=str(tmp_path))
    serialized = report.model_dump_json()
    for forbidden in ("password", "connection_string", "asyncpg", "Traceback"):
        assert forbidden not in serialized


# --- Adversarial: each test breaks exactly one real invariant and proves the
# checker notices, per item 37's own acceptance gate ("fail on any learned-
# content auto-publication or policy disclosure").


def test_checker_catches_a_broken_anti_feedback_loop_gate(benchmark, tmp_path):
    with patch.object(alb, "should_emit_signal", return_value=True):
        report = run_adaptive_learning_benchmark(benchmark, workdir=str(tmp_path))
    assert report.security_violations > 0
    assert report.passed is False


def test_checker_catches_a_broken_rollback(benchmark, tmp_path):
    def _noop_rollback(store, *, version_id, connection_id, actor, now=None):
        class _Update:
            pass

        update = _Update()
        update.store = store  # deliberately does not revert anything
        update.outcome = "rolled_back"
        update.version_id = "x"
        update.rolled_back_version_id = version_id
        return update

    with patch.object(alb.governance, "rollback_version", side_effect=_noop_rollback):
        report = run_adaptive_learning_benchmark(benchmark, workdir=str(tmp_path))
    assert report.rollback_correct is False
    assert report.passed is False


def test_checker_catches_a_disabled_conflict_rule(benchmark, tmp_path):
    with patch.object(learning_mod, "CONFLICT_MARGIN", 0):
        report = run_adaptive_learning_benchmark(benchmark, workdir=str(tmp_path))
    assert report.security_violations > 0
    assert report.passed is False


def test_checker_catches_a_disabled_support_threshold(benchmark, tmp_path):
    with (
        patch.object(learning_mod, "MIN_DISTINCT_PRINCIPALS", 1),
        patch.object(learning_mod, "MIN_CONFIDENCE", 0.0),
    ):
        report = run_adaptive_learning_benchmark(benchmark, workdir=str(tmp_path))
    assert report.security_violations > 0
    assert report.passed is False


def test_checker_catches_a_denied_table_that_is_silently_allowed(benchmark, tmp_path):
    with patch.object(alb, "validate_policy", return_value=None):
        report = run_adaptive_learning_benchmark(benchmark, workdir=str(tmp_path))
    assert report.security_violations > 0
    assert report.passed is False


def test_checker_catches_cross_connection_evidence_leakage(benchmark, tmp_path):
    original = alb._group_signals

    def _leaky(group, *, connection_id, schema_fingerprint):
        # Force every evidence group onto the primary connection regardless
        # of the fixture's own "cross" routing.
        return original(
            group, connection_id=benchmark.connection_id, schema_fingerprint=schema_fingerprint
        )

    with patch.object(alb, "_group_signals", side_effect=_leaky):
        report = run_adaptive_learning_benchmark(benchmark, workdir=str(tmp_path))
    assert report.security_violations > 0
    assert report.passed is False


def test_checker_catches_ordinary_query_execution_regressions(benchmark, tmp_path):
    with patch.object(
        StructuredQueryService, "execute", AsyncMock(side_effect=RuntimeError("simulated outage"))
    ):
        report = run_adaptive_learning_benchmark(benchmark, workdir=str(tmp_path))
    assert report.ordinary_query_unaffected_by_learner_failure is False
    assert report.passed is False


def test_replay_hits_the_deterministic_generation_id_fast_path_directly(benchmark, tmp_path):
    """The full lifecycle's `duplicate_proposals == 0` could in principle be
    enforced entirely by the "already has an open proposal" dedup guard
    rather than the generation-id idempotency contract itself. Prove the
    faster, more specific mechanism actually fires: a second learner call
    against byte-identical evidence must resolve to `"idempotent"`, not
    merely fail to duplicate via the other safety net.
    """

    import yaml

    from querygate.catalog.loader import CatalogStore
    from querygate.catalog.repository import CatalogFileRepository, CatalogFileUpdate
    from querygate.catalog.usage import record_usage_signals

    catalog_path = tmp_path / "catalog.yaml"
    primary_snapshot = alb._build_snapshot(benchmark.connection_id, benchmark.tables)
    cross_snapshot = alb._build_snapshot(benchmark.cross_connection_id, benchmark.tables)
    raw = alb._initial_catalog_raw(
        benchmark, primary_snapshot=primary_snapshot, cross_snapshot=cross_snapshot
    )
    catalog_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    repository = CatalogFileRepository(str(catalog_path))

    signals = []
    for group in benchmark.evidence_groups:
        if group.connection == "primary":
            signals.extend(
                alb._group_signals(
                    group,
                    connection_id=benchmark.connection_id,
                    schema_fingerprint=primary_snapshot.fingerprint,
                )
            )

    def _record(store: CatalogStore) -> CatalogFileUpdate:
        update = record_usage_signals(store, signals=signals)
        return CatalogFileUpdate(update.store, update)

    repository.update(_record)

    def _learn(store: CatalogStore) -> CatalogFileUpdate:
        update = learning_mod.generate_learned_relationship_proposals(
            store, connection_id=benchmark.connection_id, now=alb._FAKE_CLOCK
        )
        return CatalogFileUpdate(update.store, update)

    first = repository.update(_learn)
    second = repository.update(_learn)

    assert first.outcome == "generated"
    assert first.added_count == 1
    assert second.outcome == "idempotent"
    assert second.added_count == 0
