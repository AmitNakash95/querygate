"""Pre-execution query-cost estimation (TODO.md item 26).

Row limits, timeouts, and concurrency caps (the rest of `execution/`) are all
*reactive*: they bound a query only once it's already running. This module
adds a *proactive* check — plan the compiled query with the database's own query
planner, without ever running it, and reject it before it touches real data
if the planner's own row/cost estimate is past a configured threshold.

Both supported dialects are covered. **Postgres** (phase 1) plans inline in the
already-open session with `EXPLAIN (FORMAT JSON) <query>`. **MSSQL** (phase 2)
can't be composed that way — `SET SHOWPLAN_XML ON` must be the only statement in
its batch and applies to the whole connection until turned off — so
`estimate_mssql_query_cost` uses a **dedicated connection** (SET ON, plan the
query without running it, SET OFF) and parses the estimated rows/subtree cost out
of the SHOWPLAN XML. A policy with `max_estimated_rows`/`max_estimated_cost` now
enforces on either dialect (any *other* dialect has no estimator and proceeds
under the reactive guardrails).

Fail-open by design (see `estimate_postgres_query_cost`), and observable by
design too: every path that can't produce an estimate increments
`querygate_cost_estimation_unavailable_total{reason}` so an operator can
alert on this guardrail silently going dark, and `Policy.cost_estimation_mode`
(`CostEstimationMode.OBSERVE`) lets a deployment measure what a threshold
*would* reject against real traffic (`querygate_cost_estimation_would_reject_total`)
before switching a connection over to actually enforcing it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

import defusedxml.ElementTree as ET

import sqlalchemy as sa
from sqlalchemy.dialects import mssql, postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from querygate.core.exceptions import CostEstimateExceededError
from querygate.core.logging import get_logger
from querygate.metrics import COST_ESTIMATION_ATTEMPTS_TOTAL, COST_ESTIMATION_UNAVAILABLE_TOTAL
from querygate.policy.models import Policy


@dataclass(frozen=True)
class QueryCostEstimate:
    estimated_rows: Optional[int]
    estimated_total_cost: Optional[float]


async def estimate_postgres_query_cost(
    session: AsyncSession, stmt: sa.Select, *, connection_id: str
) -> Optional[QueryCostEstimate]:
    """Plan `stmt` with Postgres's `EXPLAIN (FORMAT JSON)` and return the
    root plan node's estimated row count / total cost.

    Best-effort and fail-open by design: this is a proactive *addition* to
    query safety, not the sole guardrail, so an EXPLAIN quirk (an unusual
    construct that fails to compile with literal binds, an unexpected plan
    shape) degrades to "not enforced for this query" rather than blocking a
    query the existing reactive guardrails would otherwise have handled
    safely. Never raises. Every failure path also increments
    `querygate_cost_estimation_unavailable_total{reason=...}` — fail-open
    must stay observable, not just quietly logged, or an operator has no way
    to notice this guardrail stopped protecting a connection.
    """
    COST_ESTIMATION_ATTEMPTS_TOTAL.labels(connection=connection_id).inc()

    try:
        # literal_binds inlines every bound value directly into the SQL text
        # instead of using driver bind parameters. EXPLAIN never executes
        # the statement — it only plans it — so there's no data-exposure
        # concern in doing this purely to get one self-contained string to
        # prefix with EXPLAIN; the same fallback-on-failure shape as
        # execution/service.py's _compile_to_text (some param types, e.g.
        # arrays, can't render as literals).
        compiled = stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    except Exception as exc:
        COST_ESTIMATION_UNAVAILABLE_TOTAL.labels(
            connection=connection_id, reason="compile_failed"
        ).inc()
        get_logger().bind(func="estimate_postgres_query_cost").warning(
            "cost_estimation.compile_failed", error=f"{type(exc).__name__}: {exc}"
        )
        return None

    try:
        # `compiled` is QueryGate's own SQLAlchemy-compiled statement (from the
        # already policy+schema-validated AST), rendered only to prefix EXPLAIN,
        # which plans but never executes it — not caller-supplied SQL. Semgrep's
        # avoid-sqlalchemy-text rule is a false positive here (reviewed 2026-07-22).
        # `# fmt: off` keeps the `# nosemgrep` on the `sa.text(...)` match line.
        explain_sql = f"EXPLAIN (FORMAT JSON) {compiled}"
        # fmt: off
        result = await session.execute(sa.text(explain_sql))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        # fmt: on
        raw_plan = result.scalar()
    except Exception as exc:
        COST_ESTIMATION_UNAVAILABLE_TOTAL.labels(
            connection=connection_id, reason="explain_failed"
        ).inc()
        get_logger().bind(func="estimate_postgres_query_cost").warning(
            "cost_estimation.explain_failed", error=f"{type(exc).__name__}: {exc}"
        )
        return None

    try:
        plan_document = json.loads(raw_plan) if isinstance(raw_plan, str) else raw_plan
        root_plan = plan_document[0]["Plan"]
        estimated_rows = root_plan.get("Plan Rows")
        estimated_total_cost = root_plan.get("Total Cost")
    except (TypeError, KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
        COST_ESTIMATION_UNAVAILABLE_TOTAL.labels(
            connection=connection_id, reason="plan_parse_failed"
        ).inc()
        get_logger().bind(func="estimate_postgres_query_cost").warning(
            "cost_estimation.plan_parse_failed", error=f"{type(exc).__name__}: {exc}"
        )
        return None

    return QueryCostEstimate(
        estimated_rows=int(estimated_rows) if estimated_rows is not None else None,
        estimated_total_cost=(
            float(estimated_total_cost) if estimated_total_cost is not None else None
        ),
    )


def _parse_showplan_xml(raw: str) -> tuple[Optional[int], Optional[float]]:
    """Pull the root statement's estimated rows/cost out of an MSSQL SHOWPLAN_XML
    document — the first element carrying the `StatementSubTreeCost`/
    `StatementEstRows` attributes (namespace-agnostic)."""
    root = ET.fromstring(raw)
    for element in root.iter():
        if "StatementSubTreeCost" in element.attrib or "StatementEstRows" in element.attrib:
            rows = element.attrib.get("StatementEstRows")
            cost = element.attrib.get("StatementSubTreeCost")
            return (
                int(float(rows)) if rows is not None else None,
                float(cost) if cost is not None else None,
            )
    return (None, None)


async def estimate_mssql_query_cost(
    engine: AsyncEngine, stmt: sa.Select, *, connection_id: str
) -> Optional[QueryCostEstimate]:
    """Plan `stmt` with SQL Server's `SET SHOWPLAN_XML ON` and return the root
    statement's estimated rows / subtree cost.

    Unlike Postgres's inline `EXPLAIN (FORMAT JSON) <query>`, SHOWPLAN mode must
    be the only thing in its batch and applies to the whole connection until
    turned off — so this uses a **dedicated connection** (SET ON, plan the query
    without running it, SET OFF), never the session running the real query.
    Same fail-open, observable-by-design contract as the Postgres estimator:
    never raises, and every unavailable path increments the unavailable counter."""
    COST_ESTIMATION_ATTEMPTS_TOTAL.labels(connection=connection_id).inc()

    try:
        compiled = stmt.compile(dialect=mssql.dialect(), compile_kwargs={"literal_binds": True})
    except Exception as exc:
        COST_ESTIMATION_UNAVAILABLE_TOTAL.labels(
            connection=connection_id, reason="compile_failed"
        ).inc()
        get_logger().bind(func="estimate_mssql_query_cost").warning(
            "cost_estimation.compile_failed", error=f"{type(exc).__name__}: {exc}"
        )
        return None

    try:
        async with engine.connect() as conn:
            # SHOWPLAN_XML ON is its own batch; the next statement is *planned*,
            # not executed, and returns the plan XML; then turn it off.
            await conn.exec_driver_sql("SET SHOWPLAN_XML ON")
            try:
                result = await conn.exec_driver_sql(str(compiled))
                raw_plan = result.scalar()
            finally:
                await conn.exec_driver_sql("SET SHOWPLAN_XML OFF")
    except Exception as exc:
        COST_ESTIMATION_UNAVAILABLE_TOTAL.labels(
            connection=connection_id, reason="explain_failed"
        ).inc()
        get_logger().bind(func="estimate_mssql_query_cost").warning(
            "cost_estimation.explain_failed", error=f"{type(exc).__name__}: {exc}"
        )
        return None

    try:
        estimated_rows, estimated_total_cost = _parse_showplan_xml(raw_plan)
    except Exception as exc:
        COST_ESTIMATION_UNAVAILABLE_TOTAL.labels(
            connection=connection_id, reason="plan_parse_failed"
        ).inc()
        get_logger().bind(func="estimate_mssql_query_cost").warning(
            "cost_estimation.plan_parse_failed", error=f"{type(exc).__name__}: {exc}"
        )
        return None

    return QueryCostEstimate(
        estimated_rows=estimated_rows, estimated_total_cost=estimated_total_cost
    )


def cost_estimate_violations(estimate: QueryCostEstimate, policy: Policy) -> list[str]:
    """Human-readable descriptions of every threshold `estimate` exceeds, or
    an empty list if it's within policy. Shared by `enforce_cost_estimate`
    (ENFORCE mode: raises) and execute()'s OBSERVE-mode logging (records
    what would have been rejected without blocking the query) so the two
    modes can never disagree about what counts as a violation.
    """
    violations: list[str] = []
    if (
        policy.max_estimated_rows is not None
        and estimate.estimated_rows is not None
        and estimate.estimated_rows > policy.max_estimated_rows
    ):
        violations.append(
            f"estimated rows ({estimate.estimated_rows}) exceeds max_estimated_rows "
            f"({policy.max_estimated_rows})"
        )
    if (
        policy.max_estimated_cost is not None
        and estimate.estimated_total_cost is not None
        and estimate.estimated_total_cost > policy.max_estimated_cost
    ):
        violations.append(
            f"estimated planner cost ({estimate.estimated_total_cost:.0f}) exceeds "
            f"max_estimated_cost ({policy.max_estimated_cost:.0f})"
        )
    return violations


def format_cost_estimate_violation_message(violations: list[str]) -> str:
    return (
        "Query rejected by pre-execution cost estimate: "
        + "; ".join(violations)
        + ". Narrow the query with additional filters, a smaller limit/top_n, "
        "or a more selective time range and try again."
    )


def enforce_cost_estimate(estimate: QueryCostEstimate, policy: Policy) -> None:
    """Raise `CostEstimateExceededError` with a message that tells the agent
    what to do next, not just that it was rejected — matches TODO item 26's
    "clear denial messages that help the agent narrow the query safely".
    Only called under `CostEstimationMode.ENFORCE` — see execute().
    """
    violations = cost_estimate_violations(estimate, policy)
    if violations:
        raise CostEstimateExceededError(format_cost_estimate_violation_message(violations))
