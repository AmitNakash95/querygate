"""One-time setup for tests/integration/test_mssql_live.py — creates and
seeds the two real MSSQL databases that suite needs, using QueryGate's own
`examples/demo_db/schema.py` (the same seed data SQLite/Postgres use) plus
two small test-specific tables that don't belong in the general demo
dataset (cross-database join + geography-column reflection are specific to
proving item 2's claims, not general demo content).

Usage: python tests/integration/setup_mssql_test_db.py
Reads the same QUERYGATE_TEST_MSSQL_* env vars as test_mssql_live.py (see
its module docstring for defaults — these match the throwaway local docker
setup used during development, not real credentials).

Run this once per fresh MSSQL server before `pytest -m mssql_live`; safe to
re-run (CREATE DATABASE/TABLE calls are guarded, INSERTs are not — drop the
databases first if you need a clean reseed).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from examples.demo_db.schema import create_and_seed_async  # noqa: E402

_HOST = os.environ.get("QUERYGATE_TEST_MSSQL_HOST", "localhost")
_PORT = os.environ.get("QUERYGATE_TEST_MSSQL_PORT", "14330")
_SA_PASSWORD = os.environ.get("QUERYGATE_TEST_MSSQL_SA_PASSWORD", "QueryGate_Test_Pw1!")
_ODBC_DRIVER = os.environ.get("QUERYGATE_TEST_MSSQL_ODBC_DRIVER", "ODBC Driver 18 for SQL Server")
_DRIVER_QS = _ODBC_DRIVER.replace(" ", "+")

_MASTER_URL = (
    f"mssql+aioodbc://sa:{_SA_PASSWORD}@{_HOST}:{_PORT}/master"
    f"?driver={_DRIVER_QS}&TrustServerCertificate=Yes"
)


def _db_url(db_name: str) -> str:
    return (
        f"mssql+aioodbc://sa:{_SA_PASSWORD}@{_HOST}:{_PORT}/{db_name}"
        f"?driver={_DRIVER_QS}&TrustServerCertificate=Yes"
    )


async def _ensure_database(db_name: str) -> None:
    engine = create_async_engine(_MASTER_URL, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        exists = (
            await conn.execute(
                sa.text("SELECT 1 FROM sys.databases WHERE name = :n"), {"n": db_name}
            )
        ).scalar()
        if not exists:
            await conn.execute(sa.text(f"CREATE DATABASE {db_name}"))
            print(f"Created database {db_name}")
    await engine.dispose()


async def main() -> None:
    await _ensure_database("querygate_demo")
    await _ensure_database("querygate_reporting")

    demo_engine = create_async_engine(_db_url("querygate_demo"))
    await create_and_seed_async(demo_engine)
    print("Seeded querygate_demo with the standard demo schema")

    async with demo_engine.begin() as conn:
        exists = (
            await conn.execute(sa.text("SELECT 1 FROM sys.tables WHERE name = 'store_locations'"))
        ).scalar()
        if not exists:
            await conn.execute(
                sa.text(
                    "CREATE TABLE store_locations (id INT PRIMARY KEY, name VARCHAR(100) NOT NULL, geo GEOGRAPHY NULL)"
                )
            )
            await conn.execute(
                sa.text(
                    "INSERT INTO store_locations (id, name, geo) VALUES "
                    "(1, 'HQ', geography::Point(47.6062, -122.3321, 4326))"
                )
            )
            print("Created + seeded store_locations (geography column)")
    await demo_engine.dispose()

    reporting_engine = create_async_engine(_db_url("querygate_reporting"))
    async with reporting_engine.begin() as conn:
        exists = (
            await conn.execute(sa.text("SELECT 1 FROM sys.tables WHERE name = 'customer_regions'"))
        ).scalar()
        if not exists:
            await conn.execute(
                sa.text(
                    "CREATE TABLE customer_regions (customer_id INT PRIMARY KEY, region VARCHAR(50) NOT NULL)"
                )
            )
            await conn.execute(
                sa.text(
                    "INSERT INTO customer_regions (customer_id, region) VALUES "
                    "(1, 'EMEA'), (2, 'AMER'), (3, 'EMEA'), (4, 'AMER')"
                )
            )
            print("Created + seeded customer_regions (cross-database join test)")
    await reporting_engine.dispose()

    print("MSSQL test databases ready.")


if __name__ == "__main__":
    asyncio.run(main())
