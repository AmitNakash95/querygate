"""Validate, compile, and execute structured reads against one connection.

This is the single seam every REST route and MCP tool goes through — there
is no other path to a database from agent/client-facing code.
"""

from __future__ import annotations

import time
from typing import Any, List, Optional, Tuple

import pydantic as pyd
import sqlalchemy as sa

from querygate.audit.logger import audit_query
from querygate.compiler.sqlalchemy_compiler import compile_structured_query
from querygate.connections.engine import get_engine, get_metadata, session_scope
from querygate.connections.registry import get_registry
from querygate.core.exceptions import PolicyViolationError
from querygate.core.logging import log_execution
from querygate.execution.concurrency import concurrency_slot
from querygate.policy.loader import get_policy
from querygate.query_ast.models import StructuredQuery
from querygate.schema.reflection import get_table_schema, list_live_tables, sanitize_table_name
from querygate.validation.policy_validation import validate_policy
from querygate.validation.schema_validation import validate_schema


class ColumnInfo(pyd.BaseModel):
    name: str
    type: str
    nullable: bool
    description: Optional[str] = None


class TableDescription(pyd.BaseModel):
    name: str
    columns: List[ColumnInfo]
    description: Optional[str] = None


class StructuredQueryResult(pyd.BaseModel):
    rows: List[dict]
    row_count: int
    truncated: bool
    limit: int
    offset: int


class ExplainResult(pyd.BaseModel):
    sql: str
    params: Optional[str] = None
    tables: List[str]
    limit: int


class BatchQueryItemResult(pyd.BaseModel):
    rows: Optional[List[dict]] = None
    row_count: Optional[int] = None
    truncated: Optional[bool] = None
    limit: Optional[int] = None
    offset: Optional[int] = None
    error: Optional[str] = None


def _clean_row_values(row: dict) -> dict:
    return {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()}


def _compile_to_text(stmt: Any) -> Tuple[str, Optional[str]]:
    """Render a compiled statement to SQL text, falling back to unbound SQL + params."""
    try:
        compiled = stmt.compile(compile_kwargs={"literal_binds": True})
        return str(compiled), None
    except Exception:
        compiled = stmt.compile()
        params = str(compiled.params) if hasattr(compiled, "params") else None
        return str(compiled), params


class StructuredQueryService:
    """Validate, compile, and execute structured reads against `connection_id`."""

    def __init__(self, connection_id: str, principal: Optional[str] = None) -> None:
        self._connection_id = connection_id
        self._principal = principal

    async def _validate_and_compile(self, query: StructuredQuery) -> Tuple[sa.Select, int, dict]:
        policy = get_policy(self._connection_id)
        validate_policy(query, policy, connection_id=self._connection_id)
        tables = await validate_schema(query, connection_id=self._connection_id)
        # Derived from the live engine, not ConnectionProfile.dialect — the
        # engine's own dialect is what actually executes the compiled SQL,
        # so this can't drift from reality (and lets tests swap in a SQLite
        # engine and get correct SQLite-flavored SQL, not Postgres/MSSQL SQL
        # that happens to fail against it).
        dialect = get_engine(self._connection_id).dialect.name
        stmt, limit = compile_structured_query(query, tables, policy, dialect=dialect)
        return stmt, limit, tables

    @log_execution
    async def execute(self, query: StructuredQuery) -> StructuredQueryResult:
        policy = get_policy(self._connection_id)
        start = time.monotonic()
        try:
            async with concurrency_slot(
                self._connection_id, policy.max_concurrency, policy.concurrency_wait_seconds
            ):
                stmt, limit, _tables = await self._validate_and_compile(query)
                sql, _params = _compile_to_text(stmt)

                async with session_scope(self._connection_id) as session:
                    result = await session.execute(stmt)
                    raw_rows = [dict(r) for r in result.mappings().all()]

                rows = [_clean_row_values(r) for r in raw_rows]
                truncated = len(rows) >= limit
                audit_query(
                    connection_id=self._connection_id,
                    sql=sql,
                    intent=query.intent,
                    row_count=len(rows),
                    duration_ms=int((time.monotonic() - start) * 1000),
                    principal=self._principal,
                )
                return StructuredQueryResult(
                    rows=rows,
                    row_count=len(rows),
                    truncated=truncated,
                    limit=limit,
                    offset=query.offset,
                )
        except Exception as exc:
            audit_query(
                connection_id=self._connection_id,
                sql="",
                intent=query.intent,
                duration_ms=int((time.monotonic() - start) * 1000),
                principal=self._principal,
                rejected=True,
                rejection_reason=str(exc),
            )
            raise

    async def execute_many(self, queries: List[StructuredQuery]) -> List[BatchQueryItemResult]:
        """Run each query independently; one failure doesn't drop the rest of the batch."""
        results: List[BatchQueryItemResult] = []
        for query in queries:
            try:
                result = await self.execute(query)
                results.append(BatchQueryItemResult(**result.model_dump()))
            except Exception as exc:
                results.append(BatchQueryItemResult(error=str(exc)))
        return results

    @log_execution
    async def explain(self, query: StructuredQuery) -> ExplainResult:
        """Validate + compile without executing; returns the SQL that would run."""
        policy = get_policy(self._connection_id)
        async with concurrency_slot(
            self._connection_id, policy.max_concurrency, policy.concurrency_wait_seconds
        ):
            stmt, limit, tables = await self._validate_and_compile(query)
            sql, params = _compile_to_text(stmt)
            return ExplainResult(sql=sql, params=params, tables=sorted(tables), limit=limit)

    @log_execution
    async def list_tables(self) -> List[str]:
        """Known/reflected table names for this connection, filtered by policy.

        Prefers already-reflected metadata + any `known_tables` seed list on
        the connection profile; falls back to a live INFORMATION_SCHEMA query
        when neither is available.
        """
        policy = get_policy(self._connection_id)
        profile = get_registry().get(self._connection_id)
        metadata = get_metadata(self._connection_id)
        names = {t.split(".")[-1] for t in metadata.tables.keys()}
        if profile.known_tables:
            names.update(profile.known_tables)
        else:
            names.update(await list_live_tables(self._connection_id))
        return sorted(name for name in names if policy.table_allowed(name))

    @log_execution
    async def describe_table(self, table_name: str) -> TableDescription:
        policy = get_policy(self._connection_id)
        sanitize_table_name(table_name)
        if not policy.table_allowed(table_name):
            raise PolicyViolationError(
                f"Table {table_name!r} is not accessible under the active policy"
            )
        engine = get_engine(self._connection_id)
        table = await get_table_schema(table_name, self._connection_id, engine)
        columns = [
            ColumnInfo(
                name=col.name,
                type=str(col.type),
                nullable=bool(col.nullable),
                description=col.comment,
            )
            for col in table.columns
            if policy.column_allowed(table_name, col.name)
        ]
        return TableDescription(name=table.name, columns=columns, description=table.comment)
