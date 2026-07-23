"""Unit tests for connections/dialects.py — dialect-specific engine URL/
connect-arg building and the MSSQL query-timeout guardrail.

`register_query_timeout`'s pyodbc-connection-unwrapping logic
(`_raw_pyodbc_connection`) was added after a real, live-server-only-
detectable bug: pyodbc's `timeout=` connect() kwarg sets the *login*
timeout, not query-execution timeout, and the raw pyodbc connection is
nested inside SQLAlchemy's async aioodbc adapter, not the object handed to
a `connect` pool event directly — see TODO.md item 3 and
tests/integration/test_mssql_live.py's live proof this actually cancels a
running query.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from querygate.connections.dialects import (
    MSSQLSessionAdapter,
    PostgresSessionAdapter,
    SessionDialectAdapter,
    _raw_pyodbc_connection,
    build_connect_args,
    build_engine_url,
    get_session_adapter,
    register_query_timeout,
)
from querygate.connections.models import ConnectionProfile, DatabaseDialect


def test_session_adapter_registry_dispatches_per_dialect():
    """Item 57: one concrete SessionDialectAdapter per dialect, dispatched via
    the registry — never inline `if dialect == ...` branching. Every supported
    dialect resolves to its own adapter, and an unsupported one is rejected."""
    pg = get_session_adapter(DatabaseDialect.POSTGRESQL)
    ms = get_session_adapter(DatabaseDialect.MSSQL)
    assert isinstance(pg, PostgresSessionAdapter)
    assert isinstance(ms, MSSQLSessionAdapter)
    # Both implement the full interface (no abstract methods left unimplemented).
    assert issubclass(PostgresSessionAdapter, SessionDialectAdapter)
    assert issubclass(MSSQLSessionAdapter, SessionDialectAdapter)
    with pytest.raises(ValueError, match="Unsupported dialect"):
        get_session_adapter("oracle")  # type: ignore[arg-type]


def test_database_dialect_equals_its_plain_string_value():
    """`DatabaseDialect` is a `StrEnum` specifically so every existing
    `dialect == "postgresql"`-shaped comparison, YAML-loaded plain string,
    and log line keeps working unchanged — this pins that behavior down
    (a hand-rolled `class X(str, Enum)` would NOT get this for free in
    Python 3.11: only `StrEnum`'s `__str__`/`__format__` resolve to the
    plain value; a plain `str, Enum` mixin prints "DatabaseDialect.X").
    """
    assert DatabaseDialect.POSTGRESQL == "postgresql"
    assert DatabaseDialect.MSSQL == "mssql"
    assert str(DatabaseDialect.POSTGRESQL) == "postgresql"
    assert f"{DatabaseDialect.MSSQL}" == "mssql"


def test_connection_profile_accepts_a_plain_yaml_string_for_dialect():
    profile = ConnectionProfile(
        id="demo", dialect="postgresql", connection_string="postgresql+asyncpg://u:p@h/db"
    )
    assert profile.dialect == DatabaseDialect.POSTGRESQL


def test_connection_profile_rejects_an_unsupported_dialect():
    with pytest.raises(Exception):
        ConnectionProfile(id="demo", dialect="oracle", connection_string="x://u:p@h/db")


def _mssql_profile() -> ConnectionProfile:
    return ConnectionProfile(
        id="demo", dialect="mssql", connection_string="mssql+aioodbc://user:pass@host/db"
    )


def test_build_engine_url_postgres_is_passthrough():
    profile = ConnectionProfile(
        id="demo", dialect="postgresql", connection_string="postgresql+asyncpg://user:pass@host/db"
    )
    assert build_engine_url(profile) == profile.connection_string


def test_build_engine_url_mssql_appends_driver():
    url = build_engine_url(_mssql_profile())
    assert url.startswith(_mssql_profile().connection_string + "?driver=")
    assert "TrustServerCertificate" not in url


def test_build_connect_args_mssql_sets_login_timeout():
    assert build_connect_args(_mssql_profile(), timeout_seconds=30) == {"timeout": 30}


def test_build_connect_args_postgres_is_empty():
    profile = ConnectionProfile(
        id="demo", dialect="postgresql", connection_string="postgresql+asyncpg://user:pass@host/db"
    )
    assert build_connect_args(profile, timeout_seconds=30) == {}


def test_raw_pyodbc_connection_unwraps_aioodbc_adapter():
    """The shape confirmed live: dbapi_connection._connection._conn is the
    real pyodbc connection for the async aioodbc adapter.
    """
    raw_pyodbc_conn = MagicMock(name="raw_pyodbc_connection")
    inner_aioodbc_conn = MagicMock(name="aioodbc_connection", _conn=raw_pyodbc_conn)
    adapter = MagicMock(name="sqlalchemy_async_adapter", _connection=inner_aioodbc_conn)

    assert _raw_pyodbc_connection(adapter) is raw_pyodbc_conn


def test_raw_pyodbc_connection_falls_back_when_no_inner_conn_attr():
    inner = MagicMock(spec=[])  # no ._conn — some other adapter shape
    adapter = MagicMock(_connection=inner)
    assert _raw_pyodbc_connection(adapter) is inner


def test_raw_pyodbc_connection_falls_back_to_self_for_sync_pyodbc_shape():
    # A hypothetical sync pyodbc connection has neither ._connection nor
    # ._conn — it already IS the raw connection.
    bare = MagicMock(spec=[])
    assert _raw_pyodbc_connection(bare) is bare


def _connect_listener_count(engine) -> int:
    # `event.listens_for(engine.sync_engine, "connect")` actually targets
    # the underlying Pool, not the Engine itself — Engine has no "connect"
    # event of its own. Counted this way (not by actually connecting) so
    # this test never risks triggering a real connection attempt: earlier
    # iterations of this test connected to prove the listener fired, and a
    # listener that raises mid-connect can send SQLAlchemy's pool into a
    # connect-retry loop that hangs indefinitely rather than propagating —
    # confirmed the hard way while developing this test.
    return len(list(engine.sync_engine.pool.dispatch.connect))


def test_register_query_timeout_is_noop_for_non_mssql_dialect():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    before = _connect_listener_count(engine)
    register_query_timeout(engine, "postgresql", timeout_seconds=5)
    assert _connect_listener_count(engine) == before
    asyncio.run(engine.dispose())


def test_register_query_timeout_wires_a_listener_for_mssql():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    before = _connect_listener_count(engine)
    register_query_timeout(engine, "mssql", timeout_seconds=5)
    assert _connect_listener_count(engine) == before + 1
    asyncio.run(engine.dispose())
