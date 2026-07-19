"""Schema reflection + metadata cache — the source of truth for identifiers.

An agent never gets to assert a table/column exists; every reference is
checked here against the live reflected schema (or rejected as unknown).
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

import sqlalchemy as sa

from querygate.core.exceptions import QueryValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from querygate.core.logging import get_logger

VALID_TABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_METADATA_LOCKS: dict[str, asyncio.Lock] = {}


def sanitize_table_name(table_name: str) -> str:
    if not VALID_TABLE_NAME.match(table_name):
        raise QueryValidationError(f"Invalid table name: {table_name}")
    return table_name


async def list_live_tables(connection_id: str) -> list[str]:
    """Enumerate real base tables via INFORMATION_SCHEMA — used when a
    connection has no `known_tables` seed list, so list_tables() still
    returns a real catalog before anything has been reflected yet.

    Excludes system catalog schemas: Postgres's own `information_schema.tables`
    (unlike MSSQL's) lists `pg_catalog`/`information_schema` system tables
    (`pg_type`, `pg_aggregate`, ...) alongside real ones when queried without a
    schema filter, which would otherwise leak internal database structure into
    `list_tables()` for any connection without an explicit `known_tables` seed,
    and made schema-refresh scanning (`catalog/refresh.py`) fail outright by
    trying to reflect them under the wrong schema.
    """
    from querygate.connections.engine import session_scope

    async with session_scope(connection_id) as session:
        result = await session.execute(
            sa.text(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_TYPE = 'BASE TABLE' "
                "AND TABLE_SCHEMA NOT IN ('pg_catalog', 'information_schema', 'sys')"
            )
        )
        return [row[0] for row in result.all()]


def _lock_for(connection_id: str) -> asyncio.Lock:
    if connection_id not in _METADATA_LOCKS:
        _METADATA_LOCKS[connection_id] = asyncio.Lock()
    return _METADATA_LOCKS[connection_id]


async def get_table_schema(
    table_name: str,
    connection_id: str,
    engine: AsyncEngine,
    *,
    schema: Optional[str] = None,
) -> sa.Table:
    """Return a SQLAlchemy Table, reflecting + caching it on first use.

    `schema` lets a cross-connection join qualify a table with another
    connection's physical database name (same-instance cross-database joins,
    e.g. two MSSQL databases on one server) — see
    validation/schema_validation.py's join_group check. When unset, both the
    default schema and "dbo" (MSSQL's default schema) are tried, harmlessly
    no-op on dialects without one.
    """
    from querygate.connections.engine import get_metadata

    table_name = sanitize_table_name(table_name)
    metadata = get_metadata(connection_id)
    log = get_logger()

    schemas_to_try = [schema] if schema is not None else [None, "dbo"]

    def _cache_lookup() -> Optional[sa.Table]:
        for candidate in schemas_to_try:
            key = f"{candidate}.{table_name}" if candidate else table_name
            cached = metadata.tables.get(key)
            if cached is not None:
                return cached
        return None

    table = _cache_lookup()
    if table is not None:
        return table

    async with _lock_for(connection_id):
        table = _cache_lookup()
        if table is not None:
            return table

        log.debug("schema.reflect", table=table_name, connection=connection_id)
        reflected_key = None

        # Each schema attempt needs its own connection — a NoSuchTableError
        # kills the current transaction and makes it unusable for retries.
        for candidate in schemas_to_try:
            key = f"{candidate}.{table_name}" if candidate else table_name
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(
                        lambda sync_conn, _s=candidate: sa.Table(
                            table_name, metadata, autoload_with=sync_conn, schema=_s
                        )
                    )
                reflected_key = key
                break
            except Exception:
                if key in metadata.tables:
                    metadata.remove(metadata.tables[key])

        if reflected_key is None:
            raise sa.exc.NoSuchTableError(table_name)

        return metadata.tables[reflected_key]
