"""End-to-end proof that the cumulative disclosure budget (TODO.md item 179)
gates execution against a real (SQLite) database.

The headline test is `test_a_salary_differencing_probe_is_cut_off`: it runs the
actual attack the item exists to bound — a sequence of individually-legal,
individually-k-anonymous aggregates whose predicate differs only by a sliding
constant — and proves QueryGate stops answering partway through. Everything
else here pins the surrounding contract: that the budget is checked in the real
request path, that a refused attempt never reaches the database, that ordinary
non-aggregate reads and `explain` are untouched, and that a query paused for
approval isn't charged twice.

SQLite stands in for Postgres/MSSQL, same as `test_sqlite_end_to_end.py`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from examples.demo_db.schema import create_and_seed_async
from querygate.core.auth import Principal
from querygate.core.exceptions import DisclosureBudgetExceededError
from querygate.execution.service import StructuredQueryService
from querygate.metrics import DISCLOSURE_BUDGET_REJECTIONS_TOTAL
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.query_ast.models import StructuredQuery

pytestmark = pytest.mark.integration


def _probe(salary_floor: int) -> StructuredQuery:
    """One step of a salary-differencing probe: how many employees per
    department earn more than `salary_floor`. Individually legal, individually
    k-anonymous — the disclosure is in the *sequence*, as the floor walks
    upward and each answer differs from the last by the people in between.
    """
    return StructuredQuery.model_validate(
        {
            "from": "employees",
            "select": [{"fn": "count", "col": "employees.id", "as": "n"}],
            "group_by": ["employees.department"],
            "where": {"col": "employees.salary", "op": "gt", "value": salary_floor},
        }
    )


_PLAIN_READ = {
    "from": "employees",
    "select": ["employees.id", "employees.department"],
    "limit": 5,
}


@pytest_asyncio.fixture
async def sqlite_engine(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    await create_and_seed_async(engine)

    import querygate.execution.service as svc_module
    import querygate.validation.schema_validation as sv_module

    @asynccontextmanager
    async def _session_scope(connection_id, policy=None, *, session_identifier_sink=None):
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    monkeypatch.setattr(svc_module, "get_engine", lambda connection_id: engine)
    monkeypatch.setattr(svc_module, "session_scope", _session_scope)
    monkeypatch.setattr(sv_module, "get_engine", lambda connection_id: engine)
    yield engine
    await engine.dispose()


def _set_policy(**overrides):
    base = {"min_group_size": 2, "disclosure_budget_window_seconds": 3600}
    base.update(overrides)
    set_policy_store(PolicyStore(default=Policy(**base), overrides={}))


def _service(subject="agent-1"):
    return StructuredQueryService(
        "demo", principal=Principal(subject=subject, auth_method="api_key")
    )


@pytest.mark.asyncio
async def test_a_salary_differencing_probe_is_cut_off(sqlite_engine):
    """The attack item 179 exists to bound, run end to end.

    Each probe passes policy validation, passes the k-anonymity floor, compiles,
    and would return a real answer. What stops the sequence is the cumulative
    budget — and it stops it even though every probe carries a DIFFERENT literal,
    which is precisely why the budget keys on the literal-free shape.
    """
    _set_policy(max_shape_repeats_per_window=4)
    service = _service()

    # Guard against this test passing for the WRONG reason: every probe must
    # genuinely be an individually-legal, individually-k-anonymous query. If the
    # k-floor weren't actually in force, the "sequence of legal queries" premise
    # would be false and the test would prove something much weaker.
    explained = await service.explain(_probe(50_000))
    assert "having count(*) >=" in " ".join(explained.sql.lower().split())

    # The first four probes are answered normally — with REAL rows, which is
    # the premise the headline claim rests on. `row_count >= 0` would be a
    # tautology and would still pass if a tighter k-floor suppressed every
    # group, degrading the claim to "we cut off a probe that returned nothing".
    for salary_floor in (50_000, 60_000, 70_000, 80_000):
        result = await service.execute(_probe(salary_floor))
        assert result.row_count > 0

    # The fifth is refused — the budget recognised four re-runs of one shape.
    with pytest.raises(DisclosureBudgetExceededError) as excinfo:
        await service.execute(_probe(90_000))
    assert excinfo.value.quota_kind == "disclosure_shape"
    # Close to the configured window, not merely >= 1 (which `max(1, ...)`
    # guarantees unconditionally).
    assert 3500 <= excinfo.value.retry_after_seconds <= 3600


@pytest.mark.asyncio
async def test_a_shape_varying_prober_is_caught_by_the_per_table_cap(sqlite_engine):
    """Varying the shape defeats the per-shape cap by design, so the blunt
    per-table cap is what has to catch it."""
    _set_policy(max_aggregate_queries_per_window=3)
    service = _service()

    for group_column in ("employees.department", "employees.name", "employees.email"):
        await service.execute(
            StructuredQuery.model_validate(
                {
                    "from": "employees",
                    "select": [{"fn": "count", "col": "employees.id", "as": "n"}],
                    "group_by": [group_column],
                }
            )
        )

    with pytest.raises(DisclosureBudgetExceededError) as excinfo:
        await service.execute(
            StructuredQuery.model_validate(
                {
                    "from": "employees",
                    "select": [{"fn": "count", "col": "employees.id", "as": "n"}],
                    "group_by": ["employees.hired_at"],
                }
            )
        )
    assert excinfo.value.quota_kind == "disclosure_table"


@pytest.mark.asyncio
async def test_ordinary_non_aggregate_reads_are_untouched(sqlite_engine):
    """The budget must not become a general rate limit — that is the quota's
    job. A plain row read discloses nothing the k-floor was protecting."""
    _set_policy(max_shape_repeats_per_window=1, max_aggregate_queries_per_window=1)
    service = _service()
    for _ in range(6):
        result = await service.execute(StructuredQuery.model_validate(_PLAIN_READ))
        assert result.row_count > 0


@pytest.mark.asyncio
async def test_a_budget_without_a_k_anonymity_floor_cannot_even_be_configured(sqlite_engine):
    """The documented coupling, now enforced at load time rather than silently:
    a budget with no k-floor to defend would enforce nothing, so `Policy`
    refuses it outright instead of accepting a control that does nothing."""
    with pytest.raises(ValueError, match="min_group_size"):
        _set_policy(min_group_size=None, max_shape_repeats_per_window=1)


@pytest.mark.asyncio
async def test_budget_is_isolated_per_principal(sqlite_engine):
    _set_policy(max_shape_repeats_per_window=1)
    await _service("agent-1").execute(_probe(50_000))
    # A different principal has its own budget.
    await _service("agent-2").execute(_probe(60_000))
    with pytest.raises(DisclosureBudgetExceededError):
        await _service("agent-1").execute(_probe(70_000))


@pytest.mark.asyncio
async def test_each_purpose_has_its_own_budget_end_to_end(sqlite_engine):
    """Item 145's declared purpose becomes a *cumulative* bound, not just a
    per-query narrowing."""
    _set_policy(
        max_shape_repeats_per_window=1,
        allowed_purposes=["fraud_review", "billing"],
    )
    service = _service()

    def _with_purpose(purpose, salary_floor):
        body = _probe(salary_floor).model_dump(by_alias=True, exclude_none=True)
        body["purpose"] = purpose
        return StructuredQuery.model_validate(body)

    await service.execute(_with_purpose("fraud_review", 50_000))
    await service.execute(_with_purpose("billing", 60_000))
    with pytest.raises(DisclosureBudgetExceededError):
        await service.execute(_with_purpose("fraud_review", 70_000))


@pytest.mark.asyncio
async def test_a_refused_query_never_reaches_the_database(sqlite_engine, monkeypatch):
    _set_policy(max_shape_repeats_per_window=1)
    service = _service()
    await service.execute(_probe(50_000))

    executed = []
    original = AsyncSession.execute

    async def _spy(self, stmt, *args, **kwargs):
        executed.append(stmt)
        return await original(self, stmt, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", _spy)
    with pytest.raises(DisclosureBudgetExceededError):
        await service.execute(_probe(60_000))
    assert executed == []


@pytest.mark.asyncio
async def test_explain_neither_charges_nor_is_charged(sqlite_engine):
    """`explain` compiles a preview and returns no rows, so it discloses
    nothing for a disclosure budget to meter — it must neither be refused by a
    spent budget nor spend one itself. (This is why item 179 needed no answer to
    the 'should explain be quota-gated' question: for THIS guardrail the answer
    follows from what the surface actually reveals.)
    """
    _set_policy(max_shape_repeats_per_window=1)
    service = _service()

    # Explaining many times spends nothing...
    for salary_floor in (50_000, 60_000, 70_000):
        await service.explain(_probe(salary_floor))
    # ...so the single execute budget is still fully available.
    await service.execute(_probe(80_000))
    # ...and now that it is spent, explain still works.
    await service.explain(_probe(90_000))


@pytest.mark.asyncio
async def test_rejection_increments_the_dedicated_metric(sqlite_engine):
    _set_policy(max_shape_repeats_per_window=1)
    service = _service()
    labels = DISCLOSURE_BUDGET_REJECTIONS_TOTAL.labels(
        connection="demo", budget_kind="disclosure_shape"
    )
    before = labels._value.get()

    await service.execute(_probe(50_000))
    with pytest.raises(DisclosureBudgetExceededError):
        await service.execute(_probe(60_000))

    assert labels._value.get() == before + 1


@pytest.mark.asyncio
async def test_rejection_message_never_names_the_table(sqlite_engine):
    """Which table is near its budget is itself a disclosure channel."""
    _set_policy(max_shape_repeats_per_window=1)
    service = _service()
    await service.execute(_probe(50_000))
    with pytest.raises(DisclosureBudgetExceededError) as excinfo:
        await service.execute(_probe(60_000))
    assert "employees" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_verdict_neither_charges_nor_is_charged(sqlite_engine):
    """The `verdict` half of "only execute spends budget". Asserted in the
    product guide, the threat model and the policy example, so it needs to be
    asserted here too — and it is genuinely non-obvious, because `verdict` DOES
    consume the ordinary per-principal quota (item 133)."""
    _set_policy(max_shape_repeats_per_window=1)
    service = _service()
    for salary_floor in (50_000, 60_000, 70_000):
        await service.verdict(_probe(salary_floor))
    await service.execute(_probe(80_000))
    await service.verdict(_probe(90_000))


@pytest.mark.asyncio
async def test_both_caps_configured_together_each_still_bite(sqlite_engine):
    """Neither cap may be swallowed when both are set — the documented
    recommended posture. With the two `if`s collapsed to an `if/elif`, the
    targeted per-shape cap would silently go dead behind the blunt one."""
    _set_policy(max_shape_repeats_per_window=2, max_aggregate_queries_per_window=10)
    service = _service()
    for salary_floor in (50_000, 60_000):
        await service.execute(_probe(salary_floor))
    with pytest.raises(DisclosureBudgetExceededError) as excinfo:
        await service.execute(_probe(70_000))
    assert excinfo.value.quota_kind == "disclosure_shape"


@pytest.mark.asyncio
async def test_the_blunt_cap_still_bites_when_the_shape_cap_is_generous(sqlite_engine):
    """The mirror of the test above, so neither ordering hides the other."""
    _set_policy(max_shape_repeats_per_window=100, max_aggregate_queries_per_window=2)
    service = _service()
    for group_column in ("employees.department", "employees.name"):
        await service.execute(
            StructuredQuery.model_validate(
                {
                    "from": "employees",
                    "select": [{"fn": "count", "col": "employees.id", "as": "n"}],
                    "group_by": [group_column],
                }
            )
        )
    with pytest.raises(DisclosureBudgetExceededError) as excinfo:
        await service.execute(
            StructuredQuery.model_validate(
                {
                    "from": "employees",
                    "select": [{"fn": "count", "col": "employees.id", "as": "n"}],
                    "group_by": ["employees.email"],
                }
            )
        )
    assert excinfo.value.quota_kind == "disclosure_table"


@pytest.mark.asyncio
async def test_two_different_shapes_on_one_table_do_not_share_a_shape_budget(sqlite_engine):
    """A false-positive guard. If every shape on a table collapsed into one
    bucket, an analyst running genuinely different aggregates would be refused
    after `max_shape_repeats_per_window` — a denial of service with a green
    suite, since every other test re-runs one shape."""
    _set_policy(max_shape_repeats_per_window=1)
    service = _service()
    for group_column in ("employees.department", "employees.name", "employees.email"):
        await service.execute(
            StructuredQuery.model_validate(
                {
                    "from": "employees",
                    "select": [{"fn": "count", "col": "employees.id", "as": "n"}],
                    "group_by": [group_column],
                }
            )
        )


@pytest.mark.asyncio
async def test_a_cte_wrapped_probe_is_cut_off_too(sqlite_engine):
    """The bypass found in review, end to end: a NON-aggregating cte around the
    table, aggregated over in the outer scope. The k-floor still applies and the
    query still returns real answers, so if the budget charged nothing here the
    caller could difference indefinitely."""
    _set_policy(max_shape_repeats_per_window=2)
    service = _service()

    def _wrapped(salary_floor: int) -> StructuredQuery:
        return StructuredQuery.model_validate(
            {
                "ctes": [
                    {
                        "name": "src",
                        "query": {
                            "from": "employees",
                            "select": ["employees.department", "employees.salary"],
                            "where": {
                                "col": "employees.salary",
                                "op": "gt",
                                "value": salary_floor,
                            },
                        },
                    }
                ],
                "from": "src",
                "select": [{"fn": "count", "col": "src.salary", "as": "n"}],
                "group_by": ["src.department"],
            }
        )

    for salary_floor in (50_000, 60_000):
        result = await service.execute(_wrapped(salary_floor))
        assert result.row_count > 0
    with pytest.raises(DisclosureBudgetExceededError):
        await service.execute(_wrapped(70_000))


@pytest.mark.asyncio
async def test_an_approval_pause_does_not_double_charge_the_budget(sqlite_engine, monkeypatch):
    """The stated justification for enforcing AFTER the approval gate, which
    until now was asserted only by a comment. A query that pauses for approval
    must spend nothing; its retry must spend exactly one unit — not zero (which
    would make approved queries unbudgeted) and not two (which would halve the
    effective cap for every approval-gated query).
    """
    import querygate.execution.service as svc_module
    from querygate.catalog.loader import CatalogStore, set_catalog_store
    from querygate.core.exceptions import ApprovalRequiredError
    from querygate.execution.approval import issue_approval_token, query_fingerprint

    _APPROVAL_KEY = "test-hmac-key"
    monkeypatch.setattr(svc_module.app_config, "approval_token_hmac_key", _APPROVAL_KEY)

    set_catalog_store(
        CatalogStore.from_dict(
            {
                "connections": {
                    "demo": {
                        "tables": {
                            "employees": {
                                "provenance": {"created_by": "admin"},
                                "columns": {"salary": {"sensitivity": "pii"}},
                            }
                        }
                    }
                }
            }
        )
    )
    _set_policy(max_shape_repeats_per_window=1, approval_sensitivities=["pii"])
    service = _service()
    probe = _probe(50_000)

    # First attempt pauses for a human — and must NOT have spent the one unit.
    with pytest.raises(ApprovalRequiredError):
        await service.execute(probe)

    # Retry with a token: succeeds, spending exactly one unit...
    token = issue_approval_token(
        fingerprint=query_fingerprint(probe),
        approver_subject="human-1",
        key=_APPROVAL_KEY,
        connection_id="demo",
        principal_subject="agent-1",
    )
    result = await service.execute(probe, approval_token=token)
    assert result.row_count > 0

    # ...so the budget is now exhausted, proving the retry spent one and the
    # paused attempt spent zero.
    with pytest.raises(DisclosureBudgetExceededError):
        await service.execute(probe, approval_token=token)


@pytest.mark.asyncio
async def test_the_quota_counter_is_not_polluted_by_a_disclosure_rejection(sqlite_engine):
    """The two counters answer different operator questions, so a disclosure
    refusal must not also increment the quota breakdown."""
    from querygate.metrics import QUERY_QUOTA_REJECTIONS_TOTAL

    _set_policy(max_shape_repeats_per_window=1)
    service = _service()
    quota_labels = QUERY_QUOTA_REJECTIONS_TOTAL.labels(
        connection="demo", quota_kind="disclosure_shape"
    )
    before = quota_labels._value.get()

    await service.execute(_probe(50_000))
    with pytest.raises(DisclosureBudgetExceededError):
        await service.execute(_probe(60_000))

    assert quota_labels._value.get() == before
