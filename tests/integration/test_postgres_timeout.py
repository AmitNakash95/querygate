"""Proves `Policy.timeout_seconds` actually cancels a running query against
a real Postgres — not just fails to start one. See TODO.md item 3: the
guardrail is `SET LOCAL statement_timeout` (connections/dialects.py), which
has never been exercised against a genuinely slow, already-executing query.

Needs a real Postgres — run `make compose-up` first, then
`make test-postgres-live` (or `poetry run pytest -m postgres_live`).
Excluded from the default `pytest` run (see pyproject.toml's
`addopts`/`markers`).
"""

from __future__ import annotations

import time

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from querygate.connections.engine import reset_engines, session_scope
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy

pytestmark = [pytest.mark.integration, pytest.mark.real_db, pytest.mark.postgres_live]

_CONNECTION_STRING = "postgresql+asyncpg://querygate:querygate@localhost:5433/querygate_demo"


def _use_policy(timeout_seconds: int) -> None:
    set_registry(
        ConnectionRegistry(
            {
                "demo": ConnectionProfile(
                    id="demo", dialect="postgresql", connection_string=_CONNECTION_STRING
                )
            }
        )
    )
    set_policy_store(PolicyStore(default=Policy(timeout_seconds=timeout_seconds), overrides={}))
    reset_engines()


@pytest.mark.asyncio
async def test_slow_query_is_cancelled_within_configured_timeout():
    """The core claim under test: a query that would run for 10s against a
    2s statement_timeout is aborted at roughly 2s, not left running for the
    full 10s and not merely rejected before it starts.
    """
    _use_policy(timeout_seconds=2)

    start = time.monotonic()
    with pytest.raises(DBAPIError, match="statement timeout"):
        async with session_scope("demo") as session:
            await session.execute(sa.text("SELECT pg_sleep(10)"))
    elapsed = time.monotonic() - start

    assert (
        elapsed < 5
    ), f"expected cancellation near 2s, took {elapsed:.1f}s (close to the full 10s?)"
    assert elapsed > 1, f"cancelled suspiciously fast ({elapsed:.1f}s) for a 2s timeout"


@pytest.mark.asyncio
async def test_fast_query_succeeds_under_the_same_timeout():
    """The guardrail must not be killing everything indiscriminately."""
    _use_policy(timeout_seconds=2)

    async with session_scope("demo") as session:
        result = await session.execute(sa.text("SELECT 1"))
        assert result.scalar() == 1


@pytest.mark.asyncio
async def test_timeout_value_is_actually_respected_not_hardcoded():
    """A 3s sleep must fail under a 1s timeout and succeed under a 6s one —
    proves the cancellation point tracks Policy.timeout_seconds, not some
    fixed internal value.
    """
    _use_policy(timeout_seconds=1)
    with pytest.raises(DBAPIError, match="statement timeout"):
        async with session_scope("demo") as session:
            await session.execute(sa.text("SELECT pg_sleep(3)"))

    _use_policy(timeout_seconds=6)
    async with session_scope("demo") as session:
        await session.execute(sa.text("SELECT pg_sleep(3)"))  # must not raise
