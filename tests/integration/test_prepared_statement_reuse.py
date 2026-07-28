"""Pins TODO.md item 94's finding: server-side prepared-statement/plan reuse
for repeated identical-shape queries (the query-template execution path,
item 48) already happens today with QueryGate's existing engine setup — no
config change was needed on either dialect.

Postgres: SQLAlchemy's asyncpg dialect defaults `prepared_statement_cache_size`
to 100 per pooled DBAPI connection and skips re-preparing when the compiled SQL
text repeats (`AsyncAdapt_asyncpg_connection._prepare`,
sqlalchemy/dialects/postgresql/asyncpg.py). MSSQL: SQL Server's own plan cache
reuses a single compiled plan across executions of the same parameterized text
sent via pyodbc/aioodbc, independent of any QueryGate setting.

Both hold *because* QueryGate already compiles every query with bound
parameters and never inlines predicate values (see `execution/service.py`) —
repeated calls to the same template send byte-identical SQL text and differ
only in bound values, which is exactly what the Postgres cache keys on. The
Postgres test below pins that causal claim directly (an inlined-literal
mutation defeats it, verified). This test exercises QueryGate's real
`connections.engine` path (not a bespoke script) so a future SQLAlchemy/driver
upgrade that silently disables reuse (e.g. a default flipping to
`prepared_statement_cache_size=0`) fails a test instead of only being caught
by production profiling.

Note on the MSSQL side — the two dialects are **not symmetric** here, measured
directly, not assumed: SQL Server's own "simple parameterization" auto-
parameterizes even a literal-inlined trivial `SELECT` of this exact shape, so
an inlined-literal mutation does *not* reproduce a param-binding regression
the way it does for Postgres — a QueryGate regression that stopped binding
parameters specifically on MSSQL would NOT be caught by the MSSQL test below.
What it does pin is the shape-vs-reuse contract itself (same shape -> one
reused plan, different shape -> a second distinct plan), which is still real
and is what a query-template *regression in QueryGate's compiled SQL shape*
could break.

Needs a real Postgres (`make compose-up`) and MSSQL (`make compose-up-mssql`).
Excluded from the default `pytest` run.
"""

from __future__ import annotations

import os
import uuid

import pytest
import sqlalchemy as sa

from querygate.connections.engine import reset_engines, session_scope
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy

_POSTGRES_URL = "postgresql+asyncpg://querygate:querygate@localhost:5433/querygate_demo"

# 127.0.0.1, not "localhost": see test_mssql_cost_estimation.py — macOS
# resolves "localhost" to ::1 first, but docker-compose only publishes IPv4.
_MSSQL_HOST = os.environ.get("QUERYGATE_TEST_MSSQL_HOST", "127.0.0.1")
_MSSQL_PORT = os.environ.get("QUERYGATE_TEST_MSSQL_PORT", "14330")
_MSSQL_PW = os.environ.get("QUERYGATE_TEST_MSSQL_SA_PASSWORD", "QueryGate_Test_Pw1!")
_MSSQL_DRIVER = os.environ.get("QUERYGATE_TEST_MSSQL_ODBC_DRIVER", "ODBC Driver 18 for SQL Server")
_MSSQL_URL = f"mssql+aioodbc://sa:{_MSSQL_PW}@{_MSSQL_HOST}:{_MSSQL_PORT}/master"


def _use_postgres() -> None:
    set_registry(
        ConnectionRegistry(
            {
                "demo": ConnectionProfile(
                    id="demo", dialect="postgresql", connection_string=_POSTGRES_URL
                )
            }
        )
    )
    set_policy_store(PolicyStore(default=Policy(), overrides={}))
    reset_engines()


def _use_mssql() -> None:
    from querygate.core.config import config as shared_config

    shared_config.odbc_driver = _MSSQL_DRIVER.replace(" ", "+")
    shared_config.db_trust_server_certificate = True
    set_registry(
        ConnectionRegistry(
            {"ms": ConnectionProfile(id="ms", dialect="mssql", connection_string=_MSSQL_URL)}
        )
    )
    set_policy_store(PolicyStore(default=Policy(), overrides={}))
    reset_engines()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.real_db
@pytest.mark.postgres_live
async def test_postgres_repeated_query_shape_reuses_prepared_statement():
    _use_postgres()

    async with session_scope("demo") as session:
        conn = await session.connection()
        raw = await conn.get_raw_connection()
        cache = raw.dbapi_connection._prepared_statement_cache

        # Same SQL text, different bound value each time — exactly what two
        # calls to the same query template with different caller args produce.
        keys_before = set(cache.keys())
        await session.execute(sa.text("SELECT :x AS val").bindparams(x=1))
        new_keys = set(cache.keys()) - keys_before
        assert len(new_keys) == 1, f"expected exactly one new cache entry, got {new_keys}"
        our_key = next(iter(new_keys))
        # `cache[our_key]` is the (prepared_stmt, attributes, cached_timestamp)
        # tuple SQLAlchemy's asyncpg dialect stores (see `_prepare` in
        # sqlalchemy/dialects/postgresql/asyncpg.py). On a genuine cache HIT it
        # returns early without touching the cache at all, so this exact tuple
        # object survives untouched; on a re-prepare (hidden by a same-key
        # dict overwrite that `len(cache)` alone can't see) a brand new tuple
        # is assigned to the same key. Identity, not size, is what actually
        # proves reuse happened.
        entry_after_first = cache[our_key]

        await session.execute(sa.text("SELECT :x AS val").bindparams(x=2))
        assert cache[our_key] is entry_after_first, (
            "the cached entry for the repeated SQL text was replaced by a new "
            "tuple — the statement was re-prepared instead of reused, even "
            "though the cache's total size didn't change"
        )

        # Sanity control: a genuinely different shape must still add a new
        # entry, proving the check above isn't vacuous (e.g. a cache that
        # never grows/changes at all).
        keys_before_third = set(cache.keys())
        await session.execute(sa.text("SELECT :x AS val, 1 AS extra").bindparams(x=3))
        assert len(set(cache.keys()) - keys_before_third) == 1


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.real_db
@pytest.mark.mssql_live
async def test_mssql_repeated_query_shape_reuses_cached_plan():
    _use_mssql()
    # A fresh marker every run: SQL Server's plan cache is server-global and
    # outlives a single pytest process, so a fixed marker string would
    # accumulate stale plans (and inflated usecounts) across repeated runs.
    marker = f"QG_ITEM94_PLAN_REUSE_PROBE_{uuid.uuid4().hex}"

    async with session_scope("ms") as session:
        for value in (1, 2, 3):
            await session.execute(
                sa.text(f"SELECT :x AS val, '{marker}' AS marker").bindparams(x=value)
            )

        # Sanity control: a genuinely different shape must produce a distinct
        # cached plan, proving the query below isn't vacuously true (e.g. a
        # DMV query that always reports "one plan" regardless of what ran).
        await session.execute(
            sa.text(f"SELECT :x AS val, :y AS val2, '{marker}' AS marker").bindparams(x=4, y=5)
        )

        result = await session.execute(sa.text("""
                SELECT cp.usecounts
                FROM sys.dm_exec_cached_plans cp
                CROSS APPLY sys.dm_exec_sql_text(cp.plan_handle) st
                WHERE st.text LIKE :marker
                ORDER BY cp.usecounts DESC
                """).bindparams(marker=f"%{marker}%"))
        rows = result.fetchall()

    assert len(rows) == 2, (
        f"expected two distinct cached plans (one per query shape), found "
        f"{len(rows)} — either shapes aren't being distinguished (vacuous test) "
        f"or the repeated shape isn't reusing a single plan"
    )
    assert rows[0].usecounts >= 3, (
        f"expected the repeated-shape plan to be reused across all 3 executions, "
        f"usecounts={rows[0].usecounts}"
    )
