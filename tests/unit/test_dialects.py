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
    MySQLSessionAdapter,
    PostgresSessionAdapter,
    SessionDialectAdapter,
    SnowflakeSessionAdapter,
    _raw_pyodbc_connection,
    build_connect_args,
    build_engine_url,
    get_session_adapter,
    list_live_tables_extra_filter_sql,
    register_query_timeout,
)
from querygate.connections.models import ConnectionProfile, DatabaseDialect


def test_session_adapter_registry_dispatches_per_dialect():
    """Item 57: one concrete SessionDialectAdapter per dialect, dispatched via
    the registry — never inline `if dialect == ...` branching. Every supported
    dialect resolves to its own adapter, and an unsupported one is rejected."""
    pg = get_session_adapter(DatabaseDialect.POSTGRESQL)
    ms = get_session_adapter(DatabaseDialect.MSSQL)
    sf = get_session_adapter(DatabaseDialect.SNOWFLAKE)
    assert isinstance(pg, PostgresSessionAdapter)
    assert isinstance(ms, MSSQLSessionAdapter)
    assert isinstance(sf, SnowflakeSessionAdapter)
    # Both implement the full interface (no abstract methods left unimplemented).
    assert issubclass(PostgresSessionAdapter, SessionDialectAdapter)
    assert issubclass(MSSQLSessionAdapter, SessionDialectAdapter)
    assert issubclass(SnowflakeSessionAdapter, SessionDialectAdapter)
    with pytest.raises(ValueError, match="Unsupported dialect"):
        get_session_adapter("oracle")  # type: ignore[arg-type]


def test_is_connectable_is_true_by_default_and_false_only_for_snowflake():
    """TODO.md item 19 phase 2 — 2026-08-06 `architecture-boundary-reviewer`
    finding: `connections/engine.py`'s guard must dispatch through this
    registered capability method, never an inline `if dialect ==
    DatabaseDialect.SNOWFLAKE` comparison at the `init_engine` call site. Pin
    the method itself here, independent of `init_engine`'s own guard test in
    `tests/unit/test_connections_engine.py`."""
    assert get_session_adapter(DatabaseDialect.POSTGRESQL).is_connectable() is True
    assert get_session_adapter(DatabaseDialect.MSSQL).is_connectable() is True
    assert get_session_adapter(DatabaseDialect.MYSQL).is_connectable() is True
    assert get_session_adapter(DatabaseDialect.SNOWFLAKE).is_connectable() is False


def test_list_live_tables_extra_filter_only_restricts_mysql():
    """Postgres's/MSSQL's INFORMATION_SCHEMA.TABLES is already scoped to the
    connected database — no extra filter needed. MySQL's is server-wide, so
    it must add a `TABLE_SCHEMA = DATABASE()` restriction, or a connection
    whose user can see more than its own database would leak other
    databases' table names into list_tables() (schema/reflection.py)."""
    assert list_live_tables_extra_filter_sql(DatabaseDialect.POSTGRESQL) == ""
    assert list_live_tables_extra_filter_sql(DatabaseDialect.MSSQL) == ""
    assert isinstance(get_session_adapter(DatabaseDialect.MYSQL), MySQLSessionAdapter)
    assert "DATABASE()" in list_live_tables_extra_filter_sql(DatabaseDialect.MYSQL)
    # Snowflake's per-database (not server-wide) INFORMATION_SCHEMA scoping
    # is a documentation-derived, unverified claim (2026-08-06
    # security-invariant-reviewer finding) — pinned here as an explicit,
    # reviewable choice, not a silent inherited default. See
    # `SnowflakeSessionAdapter.list_live_tables_extra_filter_sql`'s own
    # docstring for the caveat this must be reverified against a live
    # account before item 157 lifts `init_engine`'s connect guard.
    assert list_live_tables_extra_filter_sql(DatabaseDialect.SNOWFLAKE) == ""


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


# --------------------------------------------------------------------------- #
# Session guardrails — what each adapter actually issues (TODO.md item 102).
#
# These exist because the UTC pin was, until this test, provable ONLY by
# `make test-postgres-live` against a deliberately non-UTC server. Deleting the
# line passed the default suite, `-m unit`, `-m integration` and
# `make test-security` — while `docs/PRODUCT_GUIDE.md` promises every date
# answer is UTC, and the already-shipped `date_bucket` depends on it. A
# customer-facing guarantee needs a guard in the tier that always runs.
# --------------------------------------------------------------------------- #
class _RecordingSession:
    """Captures the SQL text of every `execute` without a database."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, statement):
        self.statements.append(str(statement))
        return None


@pytest.mark.asyncio
async def test_postgres_session_guardrails_pin_the_timezone_to_utc():
    session = _RecordingSession()
    await PostgresSessionAdapter().apply_session_guardrails(
        session, lock_timeout_seconds=3, statement_timeout_seconds=7
    )
    assert session.statements == [
        "SET LOCAL lock_timeout = '3s'",
        "SET LOCAL statement_timeout = '7s'",
        "SET LOCAL TIME ZONE 'UTC'",
    ]


@pytest.mark.asyncio
async def test_postgres_timezone_pin_is_transaction_scoped():
    """`SET LOCAL`, not `SET`: the pin must not outlive the transaction, or a
    pooled connection would carry it to unrelated work (and it would be unsafe
    under transaction-level connection pooling)."""
    session = _RecordingSession()
    await PostgresSessionAdapter().apply_session_guardrails(
        session, lock_timeout_seconds=1, statement_timeout_seconds=1
    )
    timezone_statements = [s for s in session.statements if "TIME ZONE" in s]
    assert timezone_statements == ["SET LOCAL TIME ZONE 'UTC'"]
    assert all(s.startswith("SET LOCAL") for s in session.statements)


@pytest.mark.asyncio
async def test_mssql_session_guardrails_set_no_timezone():
    """T-SQL has no session time zone to pin — MSSQL carries the UTC guarantee
    in `SYSUTCDATETIME()` at compile time instead. Issuing a timezone statement
    here would be a silent no-op at best and an error at worst."""
    session = _RecordingSession()
    await MSSQLSessionAdapter().apply_session_guardrails(
        session, lock_timeout_seconds=3, statement_timeout_seconds=7
    )
    assert session.statements == [
        "SET LOCK_TIMEOUT 3000",
        "SET XACT_ABORT ON",
        "SET DEADLOCK_PRIORITY LOW",
    ]
    assert not any("TIME ZONE" in s for s in session.statements)


# --------------------------------------------------------------------------- #
# Session-identifier capture / cancellation (TODO.md item 35 phase 3).
# --------------------------------------------------------------------------- #
class _ScalarSession(_RecordingSession):
    """`_RecordingSession` plus a configurable scalar result, for
    `capture_session_identifier`'s `result.scalar_one()` read."""

    def __init__(self, scalar_value) -> None:
        super().__init__()
        self._scalar_value = scalar_value

    async def execute(self, statement):
        await super().execute(statement)
        return _ScalarResult(self._scalar_value)


class _ScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one(self):
        return self._value


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, object]] = []

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None


class _RecordingEngine:
    def __init__(self) -> None:
        self.connection = _RecordingConnection()

    def connect(self):
        return self.connection


@pytest.mark.asyncio
async def test_postgres_captures_the_backend_pid():
    session = _ScalarSession(4242)
    identifier = await PostgresSessionAdapter().capture_session_identifier(session)
    assert identifier == "4242"
    assert session.statements == ["SELECT pg_backend_pid()"]


@pytest.mark.asyncio
async def test_postgres_cancel_session_uses_a_new_connection_and_a_bind_parameter():
    engine = _RecordingEngine()
    await PostgresSessionAdapter().cancel_session(engine, "4242")
    [(statement, params)] = engine.connection.statements
    assert statement == "SELECT pg_cancel_backend(:pid)"
    assert params == {"pid": 4242}


@pytest.mark.asyncio
async def test_mssql_captures_the_spid():
    session = _ScalarSession(55)
    identifier = await MSSQLSessionAdapter().capture_session_identifier(session)
    assert identifier == "55"
    assert session.statements == ["SELECT @@SPID"]


@pytest.mark.asyncio
async def test_mssql_cancel_session_issues_kill_with_the_literal_spid():
    engine = _RecordingEngine()
    await MSSQLSessionAdapter().cancel_session(engine, "55")
    [(statement, params)] = engine.connection.statements
    assert statement == "KILL 55"
    assert params is None


# --------------------------------------------------------------------------- #
# Snowflake (TODO.md item 19 phase 2). Unlike the sections above, none of this
# is verified against a live server — `SnowflakeSessionAdapter`'s methods are
# never actually reached in production yet (`connections/engine.py`'s
# `init_engine` refuses a Snowflake profile before any of them could run; see
# `tests/unit/test_connections_engine.py`). These are rendering/shape
# assertions against the recording fakes, backed by Snowflake's public SQL
# reference docs, proving the adapter builds the SQL it claims to build.
# --------------------------------------------------------------------------- #
def _snowflake_profile() -> ConnectionProfile:
    return ConnectionProfile(
        id="demo",
        dialect="snowflake",
        connection_string="snowflake://user:pass@myaccount/mydb/myschema?warehouse=wh",
    )


def test_build_engine_url_snowflake_is_passthrough():
    profile = _snowflake_profile()
    assert build_engine_url(profile) == profile.connection_string


def test_build_connect_args_snowflake_sets_login_and_network_timeout():
    assert build_connect_args(_snowflake_profile(), timeout_seconds=30) == {
        "login_timeout": 30,
        "network_timeout": 30,
    }


def test_register_query_timeout_is_noop_for_snowflake():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    before = _connect_listener_count(engine)
    register_query_timeout(engine, "snowflake", timeout_seconds=5)
    assert _connect_listener_count(engine) == before
    asyncio.run(engine.dispose())


@pytest.mark.asyncio
async def test_snowflake_session_guardrails_use_alter_session_set():
    from querygate.connections.dialects import SnowflakeSessionAdapter

    session = _RecordingSession()
    await SnowflakeSessionAdapter().apply_session_guardrails(
        session, lock_timeout_seconds=3, statement_timeout_seconds=7
    )
    assert session.statements == [
        "ALTER SESSION SET LOCK_TIMEOUT = 3",
        "ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 7",
    ]
    # Both values are already in whole seconds on Snowflake — no ms/interval
    # conversion the way Postgres's `'Ns'` literal or MSSQL's milliseconds need.
    assert not any("TIME ZONE" in s for s in session.statements)


@pytest.mark.asyncio
async def test_snowflake_captures_the_current_session_id():
    from querygate.connections.dialects import SnowflakeSessionAdapter

    session = _ScalarSession(778899)
    identifier = await SnowflakeSessionAdapter().capture_session_identifier(session)
    assert identifier == "778899"
    assert session.statements == ["SELECT CURRENT_SESSION()"]


@pytest.mark.asyncio
async def test_snowflake_cancel_session_uses_a_new_connection_and_a_bind_parameter():
    from querygate.connections.dialects import SnowflakeSessionAdapter

    engine = _RecordingEngine()
    await SnowflakeSessionAdapter().cancel_session(engine, "778899")
    [(statement, params)] = engine.connection.statements
    assert statement == "SELECT SYSTEM$CANCEL_ALL_QUERIES(:session_id)"
    assert params == {"session_id": "778899"}


@pytest.mark.asyncio
async def test_mssql_cancel_session_rejects_a_non_integer_identifier():
    """`identifier` is always this adapter's own driver-returned SPID in
    practice, but KILL takes a literal — a non-integer value must never reach
    the interpolated statement, so this is enforced defensively rather than
    trusted."""
    engine = _RecordingEngine()
    with pytest.raises(ValueError):
        await MSSQLSessionAdapter().cancel_session(engine, "55; DROP TABLE x")
    # Proves ORDER, not just outcome: int(identifier) must reject before any
    # statement is built/executed — a test that only checked the raise could
    # still pass even if the malformed value reached the interpolated KILL
    # statement first and the rejection came too late to matter.
    assert engine.connection.statements == []
