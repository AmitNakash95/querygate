"""Postgres EXPLAIN-based pre-execution query-cost estimation (TODO.md item 26).

Row limits, timeouts, and concurrency caps (the rest of `execution/`) are all
*reactive*: they bound a query only once it's already running. This module
adds a *proactive* check — plan the compiled query with Postgres's own query
planner, without ever running it, and reject it before it touches real data
if the planner's own row/cost estimate is past a configured threshold.

Scope for this first pass is deliberately Postgres-only. MSSQL's equivalent
(`SET SHOWPLAN_XML ON`) can't be composed the way Postgres's inline
`EXPLAIN (FORMAT JSON) <query>` is here: SHOWPLAN mode must be the only
statement in its batch — no other statement, including the query it's
meant to plan, can run in the same batch once it's set — so getting an
estimated MSSQL plan needs its own dedicated connection/session lifecycle,
not a one-line prefix on the already-compiled statement. Tracked as
follow-up work in TODO.md item 26; a policy with `max_estimated_rows`/
`max_estimated_cost` set on an MSSQL connection is accepted but has no
effect there.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from querygate.core.exceptions import CostEstimateExceededError
from querygate.core.logging import get_logger
from querygate.policy.models import Policy


@dataclass(frozen=True)
class QueryCostEstimate:
    estimated_rows: Optional[int]
    estimated_total_cost: Optional[float]


async def estimate_postgres_query_cost(
    session: AsyncSession, stmt: sa.Select
) -> Optional[QueryCostEstimate]:
    """Plan `stmt` with Postgres's `EXPLAIN (FORMAT JSON)` and return the
    root plan node's estimated row count / total cost.

    Best-effort and fail-open by design: this is a proactive *addition* to
    query safety, not the sole guardrail, so an EXPLAIN quirk (an unusual
    construct that fails to compile with literal binds, an unexpected plan
    shape) degrades to "not enforced for this query" rather than blocking a
    query the existing reactive guardrails would otherwise have handled
    safely. Never raises.
    """
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
        get_logger().bind(func="estimate_postgres_query_cost").warning(
            "cost_estimation.compile_failed", error=f"{type(exc).__name__}: {exc}"
        )
        return None

    try:
        result = await session.execute(sa.text(f"EXPLAIN (FORMAT JSON) {compiled}"))
        raw_plan = result.scalar()
    except Exception as exc:
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


def enforce_cost_estimate(estimate: QueryCostEstimate, policy: Policy) -> None:
    """Raise `CostEstimateExceededError` with a message that tells the agent
    what to do next, not just that it was rejected — matches TODO item 26's
    "clear denial messages that help the agent narrow the query safely".
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
    if violations:
        raise CostEstimateExceededError(
            "Query rejected by pre-execution cost estimate: "
            + "; ".join(violations)
            + ". Narrow the query with additional filters, a smaller limit/top_n, "
            "or a more selective time range and try again."
        )
