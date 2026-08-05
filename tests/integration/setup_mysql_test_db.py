"""One-time setup for tests/integration/test_mysql_live.py — creates and
seeds the real MySQL database that suite needs, using QueryGate's own
`examples/demo_db/schema.py` (the same seed data SQLite/Postgres/MSSQL use).

Usage: python tests/integration/setup_mysql_test_db.py
Reads the same QUERYGATE_TEST_MYSQL_* env vars as test_mysql_live.py (see
its module docstring for defaults — these match the throwaway local docker
setup used during development, not real credentials).

Run this once per fresh MySQL server before `pytest -m mysql_live`; safe to
re-run (CREATE DATABASE/TABLE calls are guarded, INSERTs are not — drop the
database first if you need a clean reseed).
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

# Default 127.0.0.1, NOT "localhost": see setup_mssql_test_db.py's identical
# comment — same macOS IPv6-resolution/docker-compose-publishes-IPv4-only
# reasoning applies to asyncmy's TCP connect.
_HOST = os.environ.get("QUERYGATE_TEST_MYSQL_HOST", "127.0.0.1")
_PORT = os.environ.get("QUERYGATE_TEST_MYSQL_PORT", "13306")
_ROOT_PASSWORD = os.environ.get("QUERYGATE_TEST_MYSQL_ROOT_PASSWORD", "QueryGate_Test_Pw1")

_ROOT_URL = f"mysql+asyncmy://root:{_ROOT_PASSWORD}@{_HOST}:{_PORT}/"


def _db_url(db_name: str) -> str:
    return f"mysql+asyncmy://root:{_ROOT_PASSWORD}@{_HOST}:{_PORT}/{db_name}"


async def _ensure_database(db_name: str) -> None:
    engine = create_async_engine(_ROOT_URL, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        await conn.execute(sa.text(f"CREATE DATABASE IF NOT EXISTS {db_name}"))
        print(f"Ensured database {db_name}")
    await engine.dispose()


async def main() -> None:
    await _ensure_database("querygate_demo")

    demo_engine = create_async_engine(_db_url("querygate_demo"))
    async with demo_engine.connect() as conn:
        exists = (
            await conn.execute(
                sa.text(
                    "SELECT 1 FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_SCHEMA = 'querygate_demo' AND TABLE_NAME = 'customers'"
                )
            )
        ).scalar()
    if not exists:
        await create_and_seed_async(demo_engine)
        print("Seeded querygate_demo with the standard demo schema")
    else:
        print("querygate_demo already seeded")
    await demo_engine.dispose()

    print("MySQL test database ready.")


if __name__ == "__main__":
    asyncio.run(main())
