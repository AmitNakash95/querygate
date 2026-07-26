"""MSSQL pre-execution cost-estimation gate (TODO.md item 26 phase 2).

Phase 1 gated on Postgres `EXPLAIN`; this proves the MSSQL equivalent
(`SET SHOWPLAN_XML ON` on a dedicated connection) actually rejects a too-large
query before it runs, against a live SQL Server — the Postgres-only gap
`execution/service.py` documented is now closed.

Needs a real MSSQL — see tests/integration/test_mssql_live.py's docstring for
setup. Run with `poetry run pytest -m mssql_live`. Excluded from the default run.
"""

from __future__ import annotations

import os

import pytest

from querygate.connections.engine import reset_engines
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.core.exceptions import CostEstimateExceededError
from querygate.execution.cost_estimation import estimate_mssql_query_cost
from querygate.execution.service import StructuredQueryService
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.query_ast.models import Predicate, StructuredQuery

pytestmark = [pytest.mark.integration, pytest.mark.real_db, pytest.mark.mssql_live]

# 127.0.0.1, not "localhost": on macOS localhost resolves to ::1 first while
# docker-compose publishes this port on IPv4 only, so "localhost" hangs on IPv6
# and fails with a misleading Login timeout. CI sets the env var explicitly.
_HOST = os.environ.get("QUERYGATE_TEST_MSSQL_HOST", "127.0.0.1")
_PORT = os.environ.get("QUERYGATE_TEST_MSSQL_PORT", "14330")
_PW = os.environ.get("QUERYGATE_TEST_MSSQL_SA_PASSWORD", "QueryGate_Test_Pw1!")
_DRIVER = os.environ.get("QUERYGATE_TEST_MSSQL_ODBC_DRIVER", "ODBC Driver 18 for SQL Server")
_URL = f"mssql+aioodbc://sa:{_PW}@{_HOST}:{_PORT}/querygate_demo"


def _setup(**policy_kwargs) -> None:
    from querygate.core.config import config as shared_config

    shared_config.odbc_driver = _DRIVER.replace(" ", "+")
    shared_config.db_trust_server_certificate = True
    set_registry(
        ConnectionRegistry(
            {
                "ms": ConnectionProfile(
                    id="ms",
                    dialect="mssql",
                    connection_string=_URL,
                    known_tables=["customers", "orders", "order_items"],
                )
            }
        )
    )
    set_policy_store(PolicyStore(default=Policy(**policy_kwargs), overrides={}))
    reset_engines()


@pytest.mark.asyncio
async def test_showplan_estimator_returns_rows_and_cost():
    import sqlalchemy as sa

    from querygate.connections.engine import get_engine

    _setup()
    orders = sa.Table(
        "orders", sa.MetaData(), sa.Column("id", sa.Integer), sa.Column("status", sa.String(20))
    )
    est = await estimate_mssql_query_cost(
        get_engine("ms"), sa.select(orders.c.id).select_from(orders), connection_id="ms"
    )
    assert est is not None
    assert est.estimated_rows == 20  # the seeded orders count
    assert est.estimated_total_cost is not None and est.estimated_total_cost > 0


@pytest.mark.asyncio
async def test_cost_gate_rejects_large_scan_on_mssql():
    # A full-table scan (est ~20 rows) exceeds max_estimated_rows=5 -> rejected
    # BEFORE execution, via the SHOWPLAN_XML plan.
    _setup(max_estimated_rows=5)
    svc = StructuredQueryService(connection_id="ms")
    with pytest.raises(CostEstimateExceededError):
        await svc.execute(StructuredQuery(from_table="orders", select=["orders.id"]))


@pytest.mark.asyncio
async def test_cost_gate_admits_selective_query_on_mssql():
    # A tightly-filtered query (1 row) is within the same threshold -> runs.
    _setup(max_estimated_rows=5)
    svc = StructuredQueryService(connection_id="ms")
    result = await svc.execute(
        StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            where=Predicate(col="orders.id", op="eq", value=1),
        )
    )
    assert result.row_count <= 1
