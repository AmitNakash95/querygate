"""Unit tests for execution/cost_estimation.py — the Postgres EXPLAIN-based
pre-execution cost gate (TODO.md item 26 phase 1).

Real Postgres coverage (a genuinely large scan actually gets rejected) lives
in tests/integration/test_postgres_cost_estimation.py; these tests exercise
plan parsing and enforcement in isolation with a mocked AsyncSession.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlalchemy as sa

from querygate.core.exceptions import CostEstimateExceededError
from querygate.execution.cost_estimation import (
    QueryCostEstimate,
    cost_estimate_violations,
    enforce_cost_estimate,
    estimate_postgres_query_cost,
    format_cost_estimate_violation_message,
)
from querygate.metrics import REGISTRY
from querygate.policy.models import Policy

_CONNECTION_ID = "demo"


def _sample(name: str, labels: dict) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _attempts_count() -> float:
    return _sample("querygate_cost_estimation_attempts_total", {"connection": _CONNECTION_ID})


def _unavailable_count(reason: str) -> float:
    return _sample(
        "querygate_cost_estimation_unavailable_total",
        {"connection": _CONNECTION_ID, "reason": reason},
    )


async def _estimate(session, stmt):
    return await estimate_postgres_query_cost(session, stmt, connection_id=_CONNECTION_ID)


def _stmt() -> sa.Select:
    table = sa.Table(
        "customers",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(50)),
    )
    return sa.select(table.c.id, table.c.name)


def _session_returning(raw_plan) -> AsyncMock:
    mock_result = MagicMock()
    mock_result.scalar.return_value = raw_plan
    session = AsyncMock()
    session.execute = AsyncMock(return_value=mock_result)
    return session


_PLAN_JSON_TEXT = (
    '[{"Plan": {"Node Type": "Seq Scan", "Plan Rows": 250000, "Total Cost": 54321.5}}]'
)
_PLAN_PYTHON = [{"Plan": {"Node Type": "Seq Scan", "Plan Rows": 42, "Total Cost": 1.23}}]


class TestEstimatePostgresQueryCost:
    @pytest.mark.asyncio
    async def test_parses_json_text_plan(self):
        session = _session_returning(_PLAN_JSON_TEXT)
        estimate = await _estimate(session, _stmt())
        assert estimate == QueryCostEstimate(estimated_rows=250000, estimated_total_cost=54321.5)
        sql_arg = session.execute.call_args[0][0]
        assert "EXPLAIN (FORMAT JSON)" in str(sql_arg)

    @pytest.mark.asyncio
    async def test_parses_driver_predecoded_plan(self):
        """Some drivers may hand back an already-decoded list/dict rather
        than a raw JSON string — must not assume `raw_plan` is always str.
        """
        session = _session_returning(_PLAN_PYTHON)
        estimate = await _estimate(session, _stmt())
        assert estimate == QueryCostEstimate(estimated_rows=42, estimated_total_cost=1.23)

    @pytest.mark.asyncio
    async def test_every_call_increments_attempts_metric(self):
        before = _attempts_count()
        await _estimate(_session_returning(_PLAN_JSON_TEXT), _stmt())
        assert _attempts_count() == before + 1

    @pytest.mark.asyncio
    async def test_returns_none_when_explain_execution_fails(self):
        """Fail-open: a DB-level EXPLAIN error must not raise out of the
        estimator — the caller falls back to "not enforced for this query".
        """
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=RuntimeError("boom"))
        before = _unavailable_count("explain_failed")
        estimate = await _estimate(session, _stmt())
        assert estimate is None
        assert _unavailable_count("explain_failed") == before + 1

    @pytest.mark.asyncio
    async def test_returns_none_when_plan_json_is_malformed(self):
        session = _session_returning("not valid json")
        before = _unavailable_count("plan_parse_failed")
        estimate = await _estimate(session, _stmt())
        assert estimate is None
        assert _unavailable_count("plan_parse_failed") == before + 1

    @pytest.mark.asyncio
    async def test_returns_none_when_plan_shape_is_unexpected(self):
        session = _session_returning([{"NotAPlan": {}}])
        before = _unavailable_count("plan_parse_failed")
        estimate = await _estimate(session, _stmt())
        assert estimate is None
        assert _unavailable_count("plan_parse_failed") == before + 1

    @pytest.mark.asyncio
    async def test_returns_none_when_statement_cannot_render_literal_binds(self):
        broken_stmt = MagicMock()
        broken_stmt.compile.side_effect = Exception("array binds can't render as literals")
        session = _session_returning(_PLAN_JSON_TEXT)
        before = _unavailable_count("compile_failed")
        estimate = await _estimate(session, broken_stmt)
        assert estimate is None
        assert _unavailable_count("compile_failed") == before + 1
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_plan_fields_yield_none_estimate_values(self):
        session = _session_returning([{"Plan": {"Node Type": "Seq Scan"}}])
        estimate = await _estimate(session, _stmt())
        assert estimate == QueryCostEstimate(estimated_rows=None, estimated_total_cost=None)


class TestEnforceCostEstimate:
    def test_no_violation_within_thresholds(self):
        policy = Policy(max_estimated_rows=1000, max_estimated_cost=5000)
        estimate = QueryCostEstimate(estimated_rows=500, estimated_total_cost=1000)
        enforce_cost_estimate(estimate, policy)  # must not raise

    def test_no_violation_when_thresholds_unset(self):
        policy = Policy()
        estimate = QueryCostEstimate(estimated_rows=10_000_000, estimated_total_cost=99_999_999)
        enforce_cost_estimate(estimate, policy)  # must not raise

    def test_rejects_over_row_threshold(self):
        policy = Policy(max_estimated_rows=1000)
        estimate = QueryCostEstimate(estimated_rows=5000, estimated_total_cost=None)
        with pytest.raises(CostEstimateExceededError, match="estimated rows"):
            enforce_cost_estimate(estimate, policy)

    def test_rejects_over_cost_threshold(self):
        policy = Policy(max_estimated_cost=100)
        estimate = QueryCostEstimate(estimated_rows=None, estimated_total_cost=999)
        with pytest.raises(CostEstimateExceededError, match="planner cost"):
            enforce_cost_estimate(estimate, policy)

    def test_message_mentions_both_violations_when_both_exceeded(self):
        policy = Policy(max_estimated_rows=10, max_estimated_cost=10)
        estimate = QueryCostEstimate(estimated_rows=999, estimated_total_cost=999)
        with pytest.raises(CostEstimateExceededError) as exc_info:
            enforce_cost_estimate(estimate, policy)
        message = str(exc_info.value)
        assert "estimated rows" in message
        assert "planner cost" in message

    def test_missing_estimate_value_does_not_trip_its_own_threshold(self):
        """A threshold with no corresponding estimate value (EXPLAIN didn't
        report it) must not be treated as a violation.
        """
        policy = Policy(max_estimated_rows=10, max_estimated_cost=10)
        estimate = QueryCostEstimate(estimated_rows=None, estimated_total_cost=None)
        enforce_cost_estimate(estimate, policy)  # must not raise


class TestCostEstimateViolations:
    """cost_estimate_violations() is the shared building block behind both
    enforce_cost_estimate() (ENFORCE mode: raises) and execute()'s OBSERVE
    mode (records without raising) — the two must never disagree.
    """

    def test_returns_empty_list_within_thresholds(self):
        policy = Policy(max_estimated_rows=1000)
        estimate = QueryCostEstimate(estimated_rows=500, estimated_total_cost=None)
        assert cost_estimate_violations(estimate, policy) == []

    def test_returns_one_entry_per_exceeded_threshold(self):
        policy = Policy(max_estimated_rows=10, max_estimated_cost=10)
        estimate = QueryCostEstimate(estimated_rows=999, estimated_total_cost=999)
        assert len(cost_estimate_violations(estimate, policy)) == 2

    def test_format_message_mentions_every_violation_and_a_next_step(self):
        message = format_cost_estimate_violation_message(["a", "b"])
        assert "a" in message and "b" in message
        assert "Narrow the query" in message
