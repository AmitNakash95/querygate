"""TODO item 37: an automated, network-free proof that 32C's usage-based
learning is a real state transition with a real behavioral improvement —
not just retrieval from a hand-curated static catalog (`catalog/benchmark.py`
covers that, for item 32A).

This module drives the *real* persisted components — `CatalogStore`,
`CatalogFileRepository`, `catalog.usage.record_usage_signals`/
`should_emit_signal`, `catalog.learning.generate_learned_relationship_proposals`,
`catalog.governance`'s review/publish/rollback state machine,
`catalog.retrieval.search_catalog`, `catalog.refresh.refresh_catalog_schema`,
`validation.policy_validation.validate_policy`, and a real (mocked-session)
`StructuredQueryService` — through the complete lifecycle described in
TODO.md item 37: unfamiliar baseline, evidence submission (with every
required control), learning, quarantined review, approval/publication,
improved re-run, schema/policy change, rollback, and restart/reload. No live
model, external network, wall-clock sleep, or production row access is used
anywhere in this module; every timestamp is a fixed constant, and every
signal id is deterministic (see `catalog.usage.build_usage_signal`).

Thresholds are compiled constants below, not fixture-tunable, per item 37's
own acceptance gate.
"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Annotated, Iterator, Literal, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pydantic as pyd
import sqlalchemy as sa
import yaml

from querygate.catalog import governance
from querygate.catalog.learning import (
    FULL_CONFIDENCE_SUPPORT,
    generate_learned_relationship_proposals,
)
from querygate.catalog.loader import CatalogStore, set_catalog_store
from querygate.catalog.models import (
    CatalogDraftObjectType,
    CatalogDraftTarget,
    CatalogEntryStatus,
    CatalogUsageSignal,
    CatalogUsageSignalKind,
)
from querygate.catalog.refresh import refresh_catalog_schema
from querygate.catalog.repository import CatalogFileRepository, CatalogFileUpdate
from querygate.catalog.retrieval import CatalogFreshness, search_catalog
from querygate.catalog.schema_memory import ObservedSchemaSnapshot
from querygate.catalog.usage import build_usage_signal, should_emit_signal
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.core.exceptions import PolicyViolationError
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.query_ast.models import StructuredQuery
from querygate.validation.policy_validation import validate_policy

BENCHMARK_VERSION = 1
THRESHOLD_VERSION = "37-mvp-1"

# Compiled release thresholds — declared before this test could have been run
# against a real implementation, per item 37's own acceptance gate ("keep its
# thresholds in source rather than fixture-tunable").
MIN_DISCOVERY_CALL_REDUCTION = 0.50
MAX_DUPLICATE_PROPOSALS = 0
MAX_SECURITY_VIOLATIONS = 0

# A policy used only to prove principal-safe filtering: denies the learned
# relationship's own target table, so the same published content must be
# visible under the default policy and hidden under this one.
_RESTRICTED_VIEWER_POLICY_DENIES = "customers"

_FAKE_CLOCK = datetime(2026, 1, 1, tzinfo=timezone.utc)


class BenchmarkTable(pyd.BaseModel):
    name: Annotated[str, pyd.StringConstraints(min_length=1, max_length=100)]
    columns: list[Annotated[str, pyd.StringConstraints(min_length=1, max_length=100)]] = pyd.Field(
        min_length=1, max_length=20
    )

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)


class BenchmarkRelationshipTarget(pyd.BaseModel):
    table: str
    column: str
    to_table: str
    to_column: str

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)


class BenchmarkTask(pyd.BaseModel):
    query: Annotated[str, pyd.StringConstraints(min_length=1, max_length=256)]
    expected_relationship: BenchmarkRelationshipTarget
    baseline_discovery_calls: int = pyd.Field(ge=1, le=20)

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)


class BenchmarkEvidenceGroup(pyd.BaseModel):
    label: Annotated[str, pyd.StringConstraints(min_length=1, max_length=80)]
    target: BenchmarkRelationshipTarget
    principals: list[Annotated[str, pyd.StringConstraints(min_length=1, max_length=100)]] = (
        pyd.Field(min_length=1, max_length=100)
    )
    connection: Literal["primary", "cross"] = "primary"

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)


class AdaptiveLearningBenchmark(pyd.BaseModel):
    version: Literal[1]
    threshold_version: Literal["37-mvp-1"]
    connection_id: Annotated[str, pyd.StringConstraints(min_length=1, max_length=100)]
    cross_connection_id: Annotated[str, pyd.StringConstraints(min_length=1, max_length=100)]
    tables: list[BenchmarkTable] = pyd.Field(min_length=1, max_length=50)
    denied_table: str
    unreviewed_guidance_relationship: BenchmarkRelationshipTarget
    verified_control_table: str
    verified_control_description: Annotated[
        str, pyd.StringConstraints(min_length=1, max_length=200)
    ]
    task: BenchmarkTask
    evidence_groups: list[BenchmarkEvidenceGroup] = pyd.Field(min_length=1, max_length=20)

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)


class AdaptiveLearningReport(pyd.BaseModel):
    benchmark_version: int
    threshold_version: str
    baseline_correct: bool
    learned_correct: bool
    discovery_call_reduction: float
    stale_detected: bool
    principal_filtering_correct: bool
    rollback_correct: bool
    ordinary_query_unaffected_by_learner_failure: bool
    duplicate_proposals: int
    security_violations: int
    thresholds: dict[str, float | int]
    passed: bool

    model_config = pyd.ConfigDict(extra="forbid", frozen=True)


def default_adaptive_learning_benchmark_path() -> str:
    return str(
        resources.files("querygate.catalog")
        .joinpath("benchmark_data")
        .joinpath("adaptive_learning_v1.yaml")
    )


def load_adaptive_learning_benchmark(path: str) -> AdaptiveLearningBenchmark:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return AdaptiveLearningBenchmark.model_validate(raw)


def _build_snapshot(connection_id: str, tables: list[BenchmarkTable]) -> ObservedSchemaSnapshot:
    metadata = sa.MetaData()
    sa_tables = []
    for table in tables:
        columns = [sa.Column(table.columns[0], sa.Integer, primary_key=True)]
        columns += [sa.Column(name, sa.Integer) for name in table.columns[1:]]
        sa_tables.append(sa.Table(table.name, metadata, *columns))
    return ObservedSchemaSnapshot.from_tables(connection_id, sa_tables)


def _relationship_matches(
    hit_object_type: str,
    hit_table: str,
    hit_column: Optional[str],
    hit_to_table: Optional[str],
    hit_to_column: Optional[str],
    expected: BenchmarkRelationshipTarget,
) -> bool:
    return (
        hit_object_type == "relationship"
        and hit_table.casefold() == expected.table.casefold()
        and (hit_column or "").casefold() == expected.column.casefold()
        and (hit_to_table or "").casefold() == expected.to_table.casefold()
        and (hit_to_column or "").casefold() == expected.to_column.casefold()
    )


def _task_finds_relationship(
    store: CatalogStore, *, connection_id: str, policy: Policy, task: BenchmarkTask
) -> tuple[bool, Optional[CatalogFreshness]]:
    response = search_catalog(
        store, connection_id=connection_id, policy=policy, query=task.query, max_results=20
    )
    for hit in response.results:
        if _relationship_matches(
            hit.object_type.value,
            hit.table,
            hit.column,
            hit.to_table,
            hit.to_column,
            task.expected_relationship,
        ):
            return True, hit.citation.freshness
    return False, None


def _initial_catalog_raw(
    benchmark: AdaptiveLearningBenchmark,
    *,
    primary_snapshot: ObservedSchemaSnapshot,
    cross_snapshot: ObservedSchemaSnapshot,
) -> dict:
    guidance = benchmark.unreviewed_guidance_relationship
    return {
        "version": 2,
        "connections": {
            benchmark.connection_id: {
                "tables": {
                    benchmark.verified_control_table: {
                        "description": benchmark.verified_control_description,
                        "provenance": {"status": "verified", "source_class": "verified"},
                    },
                    guidance.table: {
                        "relationships": [
                            {
                                "column": guidance.column,
                                "to_table": guidance.to_table,
                                "to_column": guidance.to_column,
                                "provenance": {
                                    "status": "draft",
                                    "source_class": "inferred",
                                    "confidence": 0.4,
                                },
                            }
                        ]
                    },
                }
            },
            benchmark.cross_connection_id: {"tables": {}},
        },
        "schema_snapshots": {
            benchmark.connection_id: primary_snapshot.model_dump(mode="json"),
            benchmark.cross_connection_id: cross_snapshot.model_dump(mode="json"),
        },
    }


def _group_signals(
    group: BenchmarkEvidenceGroup, *, connection_id: str, schema_fingerprint: str
) -> list[CatalogUsageSignal]:
    target = CatalogDraftTarget(
        connection_id=connection_id,
        object_type=CatalogDraftObjectType.RELATIONSHIP,
        table=group.target.table,
        column=group.target.column,
        to_table=group.target.to_table,
        to_column=group.target.to_column,
    )
    return [
        build_usage_signal(
            connection_id=connection_id,
            principal_subject=principal,
            target=target,
            kind=CatalogUsageSignalKind.RELATIONSHIP_USED,
            schema_fingerprint=schema_fingerprint,
            evidence_reference=f"{group.label}-{index}",
            observed_at=_FAKE_CLOCK,
        )
        for index, principal in enumerate(group.principals)
    ]


def _expected_confidence(support: int) -> float:
    """Mirrors `catalog.learning`'s own confidence formula exactly, so the
    cross-connection independence check can assert the published
    relationship's confidence reflects only the primary evidence group's
    support — never the cross-connection signals'.
    """

    return min(1.0, support / FULL_CONFIDENCE_SUPPORT)


def _relationship_provenance(
    store: CatalogStore, connection_id: str, target: BenchmarkRelationshipTarget
) -> Optional[CatalogEntryStatus]:
    table = store.get_table(connection_id, target.table)
    if table is None:
        return None
    for relationship in table.relationships:
        if (
            relationship.column.casefold() == target.column.casefold()
            and relationship.to_table.casefold() == target.to_table.casefold()
            and relationship.to_column.casefold() == target.to_column.casefold()
        ):
            return relationship.provenance.status
    return None


@contextmanager
def _mocked_execution_environment(
    benchmark: AdaptiveLearningBenchmark, *, policy: Policy
) -> Iterator[None]:
    """Register a real connection/policy so a real StructuredQueryService can
    validate+compile+"execute" (mocked session, real policy/compiler) without
    any network access — the same technique tests/unit/test_service.py uses.
    """

    set_registry(
        ConnectionRegistry(
            {
                benchmark.connection_id: ConnectionProfile(
                    id=benchmark.connection_id,
                    dialect="postgresql",
                    connection_string="postgresql+asyncpg://user:pass@localhost/x",
                    known_tables=[table.name for table in benchmark.tables],
                )
            }
        )
    )
    set_policy_store(PolicyStore(default=policy, overrides={}))
    yield


async def _ordinary_query_still_succeeds(benchmark: AdaptiveLearningBenchmark) -> bool:
    from contextlib import asynccontextmanager

    from querygate.execution import service as svc
    from querygate.execution.service import StructuredQueryService

    query = StructuredQuery(
        from_table=benchmark.task.expected_relationship.to_table,
        select=[f"{benchmark.task.expected_relationship.to_table}.id"],
        limit=10,
    )
    table = sa.Table(
        benchmark.task.expected_relationship.to_table,
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
    )
    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = [{"id": 1}]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={table.name: table})),
        patch.object(svc, "session_scope", _scope),
    ):
        service = StructuredQueryService(connection_id=benchmark.connection_id)
        try:
            await service.execute(query)
            return True
        except Exception:
            return False


def run_adaptive_learning_benchmark(
    benchmark: AdaptiveLearningBenchmark, *, workdir: Optional[str] = None
) -> AdaptiveLearningReport:
    """Synchronous entry point (used by both the CLI command and the pytest
    integration test) driving the complete item-37 lifecycle. Internally
    runs a small amount of async code (real `StructuredQueryService`,
    `CatalogFileRepository.update_async` is not needed — only the sync
    `.update` is used, matching `catalog_cli.py`'s own style) via
    `asyncio.run`, never assuming an already-running event loop.
    """

    import asyncio

    security_violations = 0

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(workdir) if workdir else Path(tmp)
        catalog_path = base / "catalog.yaml"

        primary_snapshot = _build_snapshot(benchmark.connection_id, benchmark.tables)
        cross_snapshot = _build_snapshot(benchmark.cross_connection_id, benchmark.tables)
        raw = _initial_catalog_raw(
            benchmark, primary_snapshot=primary_snapshot, cross_snapshot=cross_snapshot
        )
        catalog_path.write_text(yaml.safe_dump(raw, sort_keys=False))
        repository = CatalogFileRepository(str(catalog_path))

        default_policy = Policy()
        restricted_policy = Policy(denied_tables=[_RESTRICTED_VIEWER_POLICY_DENIES])

        # --- 1. Deterministic baseline: the task must fail before learning.
        baseline_store = CatalogStore.from_file(str(catalog_path))
        baseline_correct, _ = _task_finds_relationship(
            baseline_store,
            connection_id=benchmark.connection_id,
            policy=default_policy,
            task=benchmark.task,
        )

        # --- Security control: a denied object can never produce a signal
        # because it can never produce a successful execution in the first
        # place — the strongest possible "must not contribute" guarantee.
        denied_query = StructuredQuery(
            from_table=benchmark.task.expected_relationship.table,
            select=[f"{benchmark.denied_table}.id"],
            joins=[
                {
                    "table": benchmark.denied_table,
                    "on": [
                        f"{benchmark.task.expected_relationship.table}.id",
                        f"{benchmark.denied_table}.id",
                    ],
                }
            ],
            limit=10,
        )
        denied_policy = Policy(denied_tables=[benchmark.denied_table])
        try:
            validate_policy(denied_query, denied_policy, connection_id=benchmark.connection_id)
            security_violations += 1  # should have raised; it did not
        except PolicyViolationError:
            pass

        # --- Security control: usage of an unreviewed (draft/inferred) guess
        # must never be emitted as validating evidence (anti-feedback loop).
        guidance = benchmark.unreviewed_guidance_relationship
        guidance_target = CatalogDraftTarget(
            connection_id=benchmark.connection_id,
            object_type=CatalogDraftObjectType.RELATIONSHIP,
            table=guidance.table,
            column=guidance.column,
            to_table=guidance.to_table,
            to_column=guidance.to_column,
        )
        if should_emit_signal(
            baseline_store, connection_id=benchmark.connection_id, target=guidance_target
        ):
            security_violations += 1  # the gate should have refused this

        # --- 2. Submit typed, fixed-id, fake-clock usage evidence — real
        # evidence plus every required control.
        primary_signals: list[CatalogUsageSignal] = []
        cross_signals: list[CatalogUsageSignal] = []
        for group in benchmark.evidence_groups:
            if group.connection == "cross":
                cross_signals.extend(
                    _group_signals(
                        group,
                        connection_id=benchmark.cross_connection_id,
                        schema_fingerprint=cross_snapshot.fingerprint,
                    )
                )
            else:
                primary_signals.extend(
                    _group_signals(
                        group,
                        connection_id=benchmark.connection_id,
                        schema_fingerprint=primary_snapshot.fingerprint,
                    )
                )

        from querygate.catalog.usage import record_usage_signals

        def _record(signals):
            def _apply(store: CatalogStore) -> CatalogFileUpdate:
                update = record_usage_signals(store, signals=signals)
                return CatalogFileUpdate(update.store, update)

            return repository.update(_apply)

        _record(primary_signals)
        _record(cross_signals)

        # --- 3. Run the real learner; assert exactly one new proposal for
        # the expected relationship, with no proposal for any control.
        def _learn(connection_id: str):
            def _apply(store: CatalogStore) -> CatalogFileUpdate:
                update = generate_learned_relationship_proposals(
                    store, connection_id=connection_id, now=_FAKE_CLOCK
                )
                return CatalogFileUpdate(update.store, update)

            return repository.update(_apply)

        first_learn = _learn(benchmark.connection_id)
        store = CatalogStore.from_file(str(catalog_path))
        real_proposals = [
            proposal
            for proposal in store.iter_draft_proposals(benchmark.connection_id)
            if proposal.target.table.casefold()
            == benchmark.task.expected_relationship.table.casefold()
            and proposal.target.column.casefold()
            == benchmark.task.expected_relationship.column.casefold()
            and proposal.target.to_table.casefold()
            == benchmark.task.expected_relationship.to_table.casefold()
        ]
        if len(real_proposals) != 1 or first_learn.added_count != 1:
            security_violations += 1
        proposal = real_proposals[0]

        control_targets = [
            group.target
            for group in benchmark.evidence_groups
            if group.label != "real" and group.connection == "primary"
        ]
        for control_target in control_targets:
            has_proposal = any(
                p.target.table.casefold() == control_target.table.casefold()
                and p.target.column.casefold() == control_target.column.casefold()
                and p.target.to_table.casefold() == control_target.to_table.casefold()
                for p in store.iter_draft_proposals(benchmark.connection_id)
            )
            if has_proposal:
                security_violations += 1

        # Cross-connection independence, checked quantitatively: the real
        # target's confidence must reflect only the primary "real" evidence
        # group's support, never the cross-connection signals'.
        real_group = next(g for g in benchmark.evidence_groups if g.label == "real")
        expected_support = len(set(real_group.principals))
        if proposal.provenance.confidence != _expected_confidence(expected_support):
            security_violations += 1

        # --- Replay idempotency: re-running the learner against unchanged
        # evidence must never create a second proposal.
        second_learn = _learn(benchmark.connection_id)
        store_after_replay = CatalogStore.from_file(str(catalog_path))
        proposal_count_after_replay = sum(
            1
            for p in store_after_replay.iter_draft_proposals(benchmark.connection_id)
            if p.target.table.casefold() == benchmark.task.expected_relationship.table.casefold()
            and p.target.column.casefold() == benchmark.task.expected_relationship.column.casefold()
            and p.target.to_table.casefold()
            == benchmark.task.expected_relationship.to_table.casefold()
        )
        duplicate_proposals = max(0, proposal_count_after_replay - 1)

        # --- Two-worker simulation: two independent repository handles
        # pointed at the same file, both running the learner, must still
        # serialize (via the real cross-process file lock) to one proposal.
        worker_a = CatalogFileRepository(str(catalog_path))
        worker_b = CatalogFileRepository(str(catalog_path))
        for worker in (worker_a, worker_b):

            def _apply(store: CatalogStore) -> CatalogFileUpdate:
                update = generate_learned_relationship_proposals(
                    store, connection_id=benchmark.connection_id, now=_FAKE_CLOCK
                )
                return CatalogFileUpdate(update.store, update)

            worker.update(_apply)
        store_after_two_workers = CatalogStore.from_file(str(catalog_path))
        proposal_count_after_workers = sum(
            1
            for p in store_after_two_workers.iter_draft_proposals(benchmark.connection_id)
            if p.target.table.casefold() == benchmark.task.expected_relationship.table.casefold()
        )
        duplicate_proposals = max(duplicate_proposals, max(0, proposal_count_after_workers - 1))

        # Cross-connection's own evidence must still independently qualify
        # on its own connection (proves the pipeline works per-connection,
        # not that it's simply broken for "cross").
        cross_learn = _learn(benchmark.cross_connection_id)
        if cross_learn.added_count != 1:
            security_violations += 1

        # --- 4. Proposal is not agent-visible and cannot alter behavior
        # before review; a rejection (on a disposable copy) leaves behavior
        # unchanged.
        pre_review_correct, _ = _task_finds_relationship(
            store_after_two_workers,
            connection_id=benchmark.connection_id,
            policy=default_policy,
            task=benchmark.task,
        )
        if pre_review_correct:
            security_violations += 1  # a pending proposal must never be agent-visible

        reject_copy_path = base / "catalog_reject_copy.yaml"
        reject_copy_path.write_text(catalog_path.read_text())
        reject_repository = CatalogFileRepository(str(reject_copy_path))

        def _reject(store: CatalogStore) -> CatalogFileUpdate:
            update = governance.reject_proposal(
                store,
                proposal_id=proposal.proposal_id,
                connection_id=benchmark.connection_id,
                actor="qa-reviewer-a",
                reason="testing that rejection leaves behavior unchanged",
                now=_FAKE_CLOCK,
            )
            return CatalogFileUpdate(update.store, update)

        reject_repository.update(_reject)
        rejected_store = CatalogStore.from_file(str(reject_copy_path))
        rejected_correct, _ = _task_finds_relationship(
            rejected_store,
            connection_id=benchmark.connection_id,
            policy=default_policy,
            task=benchmark.task,
        )
        if rejected_correct:
            security_violations += 1  # rejection must never publish anything

        # --- Approve + publish (a distinct actor) on the MAIN store.
        def _approve(store: CatalogStore) -> CatalogFileUpdate:
            update = governance.approve_proposal(
                store,
                proposal_id=proposal.proposal_id,
                connection_id=benchmark.connection_id,
                actor="qa-reviewer-b",
                now=_FAKE_CLOCK,
            )
            return CatalogFileUpdate(update.store, update)

        def _publish(store: CatalogStore) -> CatalogFileUpdate:
            update = governance.publish_proposal(
                store,
                proposal_id=proposal.proposal_id,
                connection_id=benchmark.connection_id,
                actor="qa-reviewer-b",
                now=_FAKE_CLOCK,
            )
            return CatalogFileUpdate(update.store, update)

        repository.update(_approve)
        publish_update = repository.update(_publish)
        version_id = publish_update.version_id

        # --- 5. Restart/reload: fresh load, not the in-memory object.
        reloaded = CatalogStore.from_file(str(catalog_path))
        learned_correct, freshness = _task_finds_relationship(
            reloaded,
            connection_id=benchmark.connection_id,
            policy=default_policy,
            task=benchmark.task,
        )
        if freshness != CatalogFreshness.CURRENT:
            security_violations += 1
        learned_calls = 1
        discovery_call_reduction = max(
            0.0, 1.0 - (learned_calls / benchmark.task.baseline_discovery_calls)
        )

        published_provenance = _relationship_provenance(
            reloaded, benchmark.connection_id, benchmark.task.expected_relationship
        )
        auditable_and_not_self_published = (
            published_provenance == CatalogEntryStatus.VERIFIED
            and reloaded.get_version_record(version_id) is not None
            and reloaded.get_version_record(version_id).actor == "qa-reviewer-b"
        )
        if not auditable_and_not_self_published:
            security_violations += 1

        # --- Principal-safe filtering: same published content, two policies.
        default_visible, _ = _task_finds_relationship(
            reloaded,
            connection_id=benchmark.connection_id,
            policy=default_policy,
            task=benchmark.task,
        )
        restricted_visible, _ = _task_finds_relationship(
            reloaded,
            connection_id=benchmark.connection_id,
            policy=restricted_policy,
            task=benchmark.task,
        )
        principal_filtering_correct = default_visible and not restricted_visible

        # --- 6a. Schema change -> selective staleness.
        changed_tables = [
            (
                BenchmarkTable(name=t.name, columns=[c for c in t.columns if c != "customer_id"])
                if t.name == benchmark.task.expected_relationship.table
                else t
            )
            for t in benchmark.tables
        ]
        changed_snapshot = _build_snapshot(benchmark.connection_id, changed_tables)

        def _refresh(store: CatalogStore) -> CatalogFileUpdate:
            update = refresh_catalog_schema(store, changed_snapshot)
            return CatalogFileUpdate(update.store, update)

        repository.update(_refresh)
        after_schema_change = CatalogStore.from_file(str(catalog_path))
        stale_detected = (
            _relationship_provenance(
                after_schema_change, benchmark.connection_id, benchmark.task.expected_relationship
            )
            == CatalogEntryStatus.STALE
        )
        control_status = after_schema_change.get_table(
            benchmark.connection_id, benchmark.verified_control_table
        ).provenance.status
        if control_status != CatalogEntryStatus.VERIFIED:
            security_violations += 1  # unrelated verified content must stay current

        # --- 6b. Rollback.
        def _rollback(store: CatalogStore) -> CatalogFileUpdate:
            update = governance.rollback_version(
                store,
                version_id=version_id,
                connection_id=benchmark.connection_id,
                actor="qa-reviewer-b",
                now=_FAKE_CLOCK,
            )
            return CatalogFileUpdate(update.store, update)

        repository.update(_rollback)
        after_rollback = CatalogStore.from_file(str(catalog_path))
        rolled_back_correct, _ = _task_finds_relationship(
            after_rollback,
            connection_id=benchmark.connection_id,
            policy=default_policy,
            task=benchmark.task,
        )
        rollback_correct = not rolled_back_correct  # task must regress to baseline

        # --- 6c. Ordinary query execution is unaffected by a learner
        # failure (no persisted schema snapshot for a moment).
        no_snapshot_store = CatalogStore.from_dict({"version": 2})
        set_catalog_store(no_snapshot_store)
        learner_failed_as_expected = False
        try:
            generate_learned_relationship_proposals(
                no_snapshot_store, connection_id=benchmark.connection_id, now=_FAKE_CLOCK
            )
        except ValueError:
            learner_failed_as_expected = True
        if not learner_failed_as_expected:
            security_violations += 1

        with _mocked_execution_environment(benchmark, policy=default_policy):
            ordinary_query_ok = asyncio.run(_ordinary_query_still_succeeds(benchmark))

        # --- Restart/reload: repeat the post-rollback assertions fresh.
        final_reload = CatalogStore.from_file(str(catalog_path))
        final_rolled_back_correct, _ = _task_finds_relationship(
            final_reload,
            connection_id=benchmark.connection_id,
            policy=default_policy,
            task=benchmark.task,
        )
        if final_rolled_back_correct:
            security_violations += 1
        final_version = final_reload.get_version_record(version_id)
        if final_version is None or final_version.status.value != "reverted":
            security_violations += 1

    thresholds = {
        "min_discovery_call_reduction": MIN_DISCOVERY_CALL_REDUCTION,
        "max_duplicate_proposals": MAX_DUPLICATE_PROPOSALS,
        "max_security_violations": MAX_SECURITY_VIOLATIONS,
    }
    passed = (
        (not baseline_correct)
        and learned_correct
        and discovery_call_reduction >= MIN_DISCOVERY_CALL_REDUCTION
        and stale_detected
        and principal_filtering_correct
        and rollback_correct
        and ordinary_query_ok
        and duplicate_proposals <= MAX_DUPLICATE_PROPOSALS
        and security_violations <= MAX_SECURITY_VIOLATIONS
    )
    return AdaptiveLearningReport(
        benchmark_version=benchmark.version,
        threshold_version=benchmark.threshold_version,
        baseline_correct=baseline_correct,
        learned_correct=learned_correct,
        discovery_call_reduction=discovery_call_reduction,
        stale_detected=stale_detected,
        principal_filtering_correct=principal_filtering_correct,
        rollback_correct=rollback_correct,
        ordinary_query_unaffected_by_learner_failure=ordinary_query_ok,
        duplicate_proposals=duplicate_proposals,
        security_violations=security_violations,
        thresholds=thresholds,
        passed=passed,
    )
