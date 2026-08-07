"""Dialect-specific engine URL building, connect args, and session guardrails.

Formalized as a `SessionDialectAdapter` interface (TODO.md item 57): one concrete
adapter class per dialect, dispatched through the `_SESSION_ADAPTERS` registry —
the engine/session-layer sibling of `compiler/dialect_adapters.py`'s (sync)
`DialectAdapter`. The two are kept as **separate** abstract bases on purpose: this
one is async (it executes `SET ...` on a live session and hooks pool events),
that one is sync (it builds SQL expressions); merging async session execution and
sync SQL-building into one interface would be awkward. Adding a dialect means
implementing both adapters and registering them — never adding an
`if dialect == ...` branch at a call site.

The module-level `build_engine_url` / `build_connect_args` /
`register_query_timeout` / `apply_session_guardrails` functions are preserved as
thin dispatchers so existing callers (`connections/engine.py`) are unchanged;
all the dialect-specific behavior now lives in the adapter classes below.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from querygate.connections.models import ConnectionProfile, DatabaseDialect
from querygate.core.config import config as app_config


def _raw_pyodbc_connection(dbapi_connection):
    """Unwrap SQLAlchemy's async adapter to the real pyodbc connection.

    Confirmed against a live server: `engine.sync_engine`'s `connect` event
    hands us `AsyncAdapt_aioodbc_connection` (aioodbc), not a raw pyodbc
    connection — it has no `.timeout` attribute of its own. The actual
    pyodbc connection is nested at `._connection._conn` (mirroring how
    SQLAlchemy's own aioodbc adapter reaches it for its `autocommit`
    setter — see sqlalchemy.connectors.aioodbc). Falls back to the object
    itself for a hypothetical sync pyodbc engine, where it would already be
    the raw connection.
    """
    inner = getattr(dbapi_connection, "_connection", None)
    return getattr(inner, "_conn", None) or inner or dbapi_connection


class SessionDialectAdapter(ABC):
    """Per-dialect engine/session behavior: how to build the engine URL and
    connect args, register a query-execution timeout, and apply per-session
    guardrails. One concrete class per dialect, dispatched via
    `get_session_adapter` — no inline `if dialect == ...` branching."""

    @abstractmethod
    def build_engine_url(self, profile: ConnectionProfile) -> str: ...

    @abstractmethod
    def build_connect_args(self, profile: ConnectionProfile, timeout_seconds: int) -> dict: ...

    @abstractmethod
    def register_query_timeout(self, engine: AsyncEngine, timeout_seconds: int) -> None: ...

    @abstractmethod
    async def apply_session_guardrails(
        self,
        session: AsyncSession,
        *,
        lock_timeout_seconds: int,
        statement_timeout_seconds: int,
    ) -> None: ...

    @abstractmethod
    async def capture_session_identifier(self, session: AsyncSession) -> str:
        """A value identifying this live session's server-side process/backend
        (Postgres backend PID, MSSQL `@@SPID`), captured once right after
        connect so a LATER, separate connection can target it for real
        dialect-level cancellation (TODO.md item 35 phase 3). Not a security
        boundary itself — `cancel_session` below is gated by
        `Policy.allow_query_cancellation`, not by this method existing."""
        ...

    @abstractmethod
    async def cancel_session(self, engine: AsyncEngine, identifier: str) -> None:
        """Cancel the query running on the session `identifier` names, from a
        NEW connection on `engine` — never the session being cancelled itself
        (it's presumably blocked executing the query this call is meant to
        stop). Each dialect gets the primitive it actually has, mechanically
        translated, not a synthesized "graceful cancel" a dialect doesn't
        offer out-of-band (2026-07-28 Decision Log): Postgres's
        `pg_cancel_backend` interrupts just the query and leaves the pooled
        connection alive; MSSQL's `KILL` is the only out-of-band primitive
        T-SQL exposes for this and terminates the whole session."""
        ...

    def list_live_tables_extra_filter_sql(self) -> str:
        """An extra SQL condition (starting with " AND ...", or "") appended
        to `schema/reflection.py`'s `list_live_tables()` INFORMATION_SCHEMA
        query, beyond the shared system-schema exclusion every dialect needs.

        Postgres's and MSSQL's `INFORMATION_SCHEMA.TABLES` is already scoped
        to the connected database — excluding the system schema names is
        enough. MySQL's is not: it is a server-wide view spanning every
        database the connecting user has any privilege on, so without this,
        a MySQL connection whose user can see more than its own database
        would leak other databases' table names into `list_tables()` (a
        schema-shape disclosure, not just a system-table one). Default is a
        no-op; MySQLSessionAdapter overrides it."""
        return ""

    def is_connectable(self) -> bool:
        """Whether `connections/engine.py`'s `init_engine` may proceed to
        `create_async_engine` for this dialect at all (TODO.md item 19
        phases 2/3). Default `True` — every dialect with a real async
        SQLAlchemy driver (Postgres/MSSQL/MySQL) is connectable and never
        overrides this. `SnowflakeSessionAdapter` and `BigQuerySessionAdapter`
        both override it to `False`: neither `snowflake-sqlalchemy`'s nor
        `sqlalchemy_bigquery`'s DBAPI has an async driver, so attempting
        `create_async_engine` for either fails inside SQLAlchemy itself (or,
        for BigQuery specifically, even earlier — see
        `BigQuerySessionAdapter`'s own docstring) with a confusing
        library-internal error rather than a QueryGate-owned one.

        This is a capability check on the REGISTERED interface, not an
        inline `if profile.dialect == ...` at the `init_engine` call site
        (2026-08-06 `architecture-boundary-reviewer` finding on this same
        item): a future dialect with a similar "registered but not yet
        connectable" gap overrides this one method instead of `init_engine`
        growing a second bespoke dialect comparison beside the first.
        """
        return True


class PostgresSessionAdapter(SessionDialectAdapter):
    def build_engine_url(self, profile: ConnectionProfile) -> str:
        return profile.connection_string

    def build_connect_args(self, profile: ConnectionProfile, timeout_seconds: int) -> dict:
        return {}

    def register_query_timeout(self, engine: AsyncEngine, timeout_seconds: int) -> None:
        # Postgres bounds execution via `SET LOCAL statement_timeout` in
        # apply_session_guardrails — no per-connection attribute to register.
        return None

    async def apply_session_guardrails(
        self,
        session: AsyncSession,
        *,
        lock_timeout_seconds: int,
        statement_timeout_seconds: int,
    ) -> None:
        # The interpolated values are Pydantic-validated ints from Policy
        # (timeout_seconds), never caller input, and `SET LOCAL` takes no bind
        # parameters — string interpolation is required here, not a SQL injection
        # vector. Semgrep's avoid-sqlalchemy-text rule is suppressed inline
        # per-site with that justification (reviewed 2026-07-22, TODO item 89).
        # fmt: off
        await session.execute(sa.text(f"SET LOCAL lock_timeout = '{lock_timeout_seconds}s'"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        await session.execute(sa.text(f"SET LOCAL statement_timeout = '{statement_timeout_seconds}s'"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        # fmt: on
        # UTC, deliberately (TODO.md item 102, 2026-07-26 Decision Log). Postgres
        # resolves `EXTRACT`, `date_trunc` and every timestamp<->timestamptz
        # conversion against the SESSION TimeZone, so without this pin the value
        # QueryGate returns for `extract(hour from a timestamptz)` or a
        # `date_bucket` depends on the server's configured zone rather than on
        # the query — the same input yielding different answers on two
        # deployments, invisible to any test that only inspects generated SQL.
        # MSSQL has no session time zone to set (its adapter reads the clock via
        # SYSUTCDATETIME instead) and SQLite's 'now' is already UTC, so this is
        # what makes all three dialects agree. Fixed literal, no interpolation.
        await session.execute(sa.text("SET LOCAL TIME ZONE 'UTC'"))

    async def capture_session_identifier(self, session: AsyncSession) -> str:
        result = await session.execute(sa.text("SELECT pg_backend_pid()"))
        return str(result.scalar_one())

    async def cancel_session(self, engine: AsyncEngine, identifier: str) -> None:
        # A bind parameter, unlike apply_session_guardrails's SET LOCAL
        # (which takes none) — pg_cancel_backend is an ordinary function call.
        # A NEW connection: `identifier`'s own session is presumably blocked
        # executing the query this call means to interrupt.
        async with engine.connect() as conn:
            await conn.execute(sa.text("SELECT pg_cancel_backend(:pid)"), {"pid": int(identifier)})


class MSSQLSessionAdapter(SessionDialectAdapter):
    def build_engine_url(self, profile: ConnectionProfile) -> str:
        cert = "&TrustServerCertificate=Yes" if app_config.db_trust_server_certificate else ""
        return f"{profile.connection_string}?driver={app_config.odbc_driver}{cert}"

    def build_connect_args(self, profile: ConnectionProfile, timeout_seconds: int) -> dict:
        # This is pyodbc's *login* timeout (SQL_ATTR_LOGIN_TIMEOUT) — despite
        # its name, `timeout=` in pyodbc.connect() does NOT bound query
        # execution (confirmed against a live server: a WAITFOR DELAY well
        # past this value ran to completion uncancelled — TODO.md item 3).
        # Real query-execution timeout is register_query_timeout below, which
        # has to be set as a post-connect attribute, not a connect kwarg. Kept
        # here anyway — bounding how long a connection attempt itself can hang
        # is still a real, separate guardrail worth having.
        return {"timeout": timeout_seconds}

    def register_query_timeout(self, engine: AsyncEngine, timeout_seconds: int) -> None:
        """Bind pyodbc's actual query-execution timeout (SQL_ATTR_QUERY_TIMEOUT).

        Must be set as an attribute on the raw DBAPI connection after it's
        opened — pyodbc has no connect-time keyword for it, unlike login timeout
        (see build_connect_args). Hooks SQLAlchemy's pool `connect` event so
        every physical connection this engine ever opens (not just the first)
        gets it applied, since the pool can silently create new connections
        later (e.g. after one is recycled or dropped).
        """

        @event.listens_for(engine.sync_engine, "connect")
        def _set_query_timeout(dbapi_connection, connection_record) -> None:
            _raw_pyodbc_connection(dbapi_connection).timeout = timeout_seconds

    async def apply_session_guardrails(
        self,
        session: AsyncSession,
        *,
        lock_timeout_seconds: int,
        statement_timeout_seconds: int,
    ) -> None:
        # See PostgresSessionAdapter for the interpolation/nosemgrep rationale.
        # fmt: off
        # LOCK_TIMEOUT is in milliseconds.
        await session.execute(sa.text(f"SET LOCK_TIMEOUT {lock_timeout_seconds * 1000}"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        await session.execute(sa.text("SET XACT_ABORT ON"))
        await session.execute(sa.text("SET DEADLOCK_PRIORITY LOW"))
        # fmt: on

    async def capture_session_identifier(self, session: AsyncSession) -> str:
        result = await session.execute(sa.text("SELECT @@SPID"))
        return str(result.scalar_one())

    async def cancel_session(self, engine: AsyncEngine, identifier: str) -> None:
        # KILL takes a literal SPID, not a bind parameter — T-SQL has no
        # parameterized form. `identifier` is a driver-returned integer this
        # adapter captured itself (never caller input), and `int(...)` below
        # both validates that and is what makes the subsequent interpolation
        # safe — the same "not a SQL injection vector" justification
        # apply_session_guardrails's SET LOCK_TIMEOUT already documents.
        spid = int(identifier)
        async with engine.connect() as conn:
            # fmt: off
            await conn.execute(sa.text(f"KILL {spid}"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            # fmt: on


class MySQLSessionAdapter(SessionDialectAdapter):
    def build_engine_url(self, profile: ConnectionProfile) -> str:
        return profile.connection_string

    def build_connect_args(self, profile: ConnectionProfile, timeout_seconds: int) -> dict:
        # asyncmy/PyMySQL-family connect() accepts connect_timeout (seconds) —
        # the connection-attempt timeout, the same login-timeout role MSSQL's
        # adapter documents for pyodbc's `timeout` kwarg. Real query-execution
        # timeout is a session-level SET in apply_session_guardrails below,
        # like Postgres — MySQL has no post-connect attribute to register the
        # way pyodbc's SQL_ATTR_QUERY_TIMEOUT needs (register_query_timeout
        # is a no-op here for the same reason it is for Postgres).
        return {"connect_timeout": timeout_seconds}

    def register_query_timeout(self, engine: AsyncEngine, timeout_seconds: int) -> None:
        return None

    async def apply_session_guardrails(
        self,
        session: AsyncSession,
        *,
        lock_timeout_seconds: int,
        statement_timeout_seconds: int,
    ) -> None:
        # See PostgresSessionAdapter for the interpolation/nosemgrep rationale
        # — both values are Pydantic-validated ints from Policy, never caller
        # input, and neither SET takes a bind parameter.
        # fmt: off
        await session.execute(sa.text(f"SET SESSION innodb_lock_wait_timeout = {lock_timeout_seconds}"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        # MAX_EXECUTION_TIME is milliseconds and — a genuine MySQL limitation,
        # not a QueryGate gap — only bounds SELECT statements; MySQL has no
        # session-level statement timeout that also covers INSERT/UPDATE/
        # DELETE the way Postgres's statement_timeout or MSSQL's LOCK_TIMEOUT
        # (paired with query cancellation) do. Documented in
        # docs/THREAT_MODEL.md (QG-38) rather than silently assumed
        # equivalent.
        await session.execute(sa.text(f"SET SESSION MAX_EXECUTION_TIME = {int(statement_timeout_seconds * 1000)}"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        # fmt: on
        # UTC, deliberately — the same reasoning as Postgres's TIME ZONE pin.
        # MySQL's TIMESTAMP columns (unlike DATETIME) convert to/from the
        # session time_zone on every read/write, so without this pin a
        # date_bucket/EXTRACT over a TIMESTAMP column would depend on the
        # server's configured zone rather than the query. Fixed literal, no
        # interpolation.
        await session.execute(sa.text("SET SESSION time_zone = '+00:00'"))

    async def capture_session_identifier(self, session: AsyncSession) -> str:
        result = await session.execute(sa.text("SELECT CONNECTION_ID()"))
        return str(result.scalar_one())

    async def cancel_session(self, engine: AsyncEngine, identifier: str) -> None:
        # KILL QUERY interrupts only the running statement and leaves the
        # target connection alive — the same softer semantics as Postgres's
        # pg_cancel_backend, and the reason it's used here rather than plain
        # KILL (MySQL's whole-session-terminating form, the MSSQL-equivalent
        # primitive). `identifier` is a driver-returned integer this adapter
        # captured itself (never caller input); `int(...)` both validates
        # that and is what makes the interpolation below safe, since MySQL's
        # KILL takes a literal connection id, not a bind parameter.
        conn_id = int(identifier)
        async with engine.connect() as conn:
            # fmt: off
            await conn.execute(sa.text(f"KILL QUERY {conn_id}"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            # fmt: on

    def list_live_tables_extra_filter_sql(self) -> str:
        # Unlike Postgres/MSSQL, MySQL's INFORMATION_SCHEMA.TABLES is
        # server-wide, spanning every database the connecting user has any
        # privilege on — confirmed against MySQL's own documented semantics
        # (TODO.md item 19/128 audit). Restrict to the connected database so
        # a user with cross-database privilege doesn't leak other databases'
        # table names into list_tables().
        return " AND TABLE_SCHEMA = DATABASE()"


class SnowflakeSessionAdapter(SessionDialectAdapter):
    """TODO.md item 19 phase 2 — implemented for real (correct per Snowflake's
    public docs) but **never actually exercised**: `connections/engine.py`'s
    `init_engine` refuses to open a Snowflake connection before any of these
    methods can run, because `snowflake-sqlalchemy`'s DBAPI has no async
    driver and `create_async_engine` requires one (confirmed directly:
    constructing one raises `sqlalchemy.exc.InvalidRequestError: The asyncio
    extension requires an async driver to be used. The loaded 'snowflake' is
    not async.`). This class exists so the URL/connect-arg building is real
    and ready, and so the session-guardrail SQL this dialect will need is
    written down and reviewable now rather than invented later — not because
    it has been proven against a live session. See the Snowflake
    live-verification follow-up item in TODO.md.
    """

    def is_connectable(self) -> bool:
        # The one override of the base class's `True` default — this is
        # exactly what makes `connections/engine.py`'s `init_engine` refuse a
        # Snowflake profile before `create_async_engine`, via the registered
        # interface rather than a bespoke dialect comparison at the call
        # site. See the base method's docstring for the full rationale.
        return False

    def build_engine_url(self, profile: ConnectionProfile) -> str:
        # Passthrough, like Postgres/MySQL: the operator's YAML already
        # supplies a complete `snowflake://user:pass@account/db/schema?...`
        # URL (see examples/connections.example.yaml) — there is no
        # ODBC-driver-name construct to append the way MSSQL's adapter has.
        return profile.connection_string

    def build_connect_args(self, profile: ConnectionProfile, timeout_seconds: int) -> dict:
        # snowflake-connector-python accepts BOTH a connection-attempt
        # timeout (`login_timeout`, the same login-timeout role MSSQL's
        # pyodbc `timeout` kwarg and MySQL's `connect_timeout` play) and a
        # genuine per-request timeout (`network_timeout`, which also bounds
        # query execution) as plain connect() keyword arguments — unlike
        # pyodbc, there is no separate post-connect attribute to register,
        # which is why register_query_timeout below is a no-op here.
        return {"login_timeout": timeout_seconds, "network_timeout": timeout_seconds}

    def register_query_timeout(self, engine: AsyncEngine, timeout_seconds: int) -> None:
        # No post-connect attribute to set — network_timeout above already
        # covers the query-execution-timeout role register_query_timeout
        # exists for on MSSQL's pyodbc driver.
        return None

    async def apply_session_guardrails(
        self,
        session: AsyncSession,
        *,
        lock_timeout_seconds: int,
        statement_timeout_seconds: int,
    ) -> None:
        # ALTER SESSION SET, Snowflake's own session-parameter idiom — both
        # STATEMENT_TIMEOUT_IN_SECONDS and LOCK_TIMEOUT are already
        # documented in whole seconds (unlike Postgres's ms-as-string
        # interval literal or MSSQL's milliseconds), so no unit conversion
        # is needed here. See PostgresSessionAdapter for the interpolation/
        # nosemgrep rationale — both values are Pydantic-validated ints from
        # Policy, never caller input, and ALTER SESSION SET takes no bind
        # parameter.
        # fmt: off
        await session.execute(sa.text(f"ALTER SESSION SET LOCK_TIMEOUT = {lock_timeout_seconds}"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        await session.execute(sa.text(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {statement_timeout_seconds}"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        # fmt: on
        # No time zone pin: `SnowflakeDialectAdapter.current_timestamp` uses
        # SYSDATE(), which is UTC by construction (like MSSQL's
        # SYSUTCDATETIME()) rather than depending on a session TIMEZONE
        # parameter the way Postgres's now() does.

    async def capture_session_identifier(self, session: AsyncSession) -> str:
        result = await session.execute(sa.text("SELECT CURRENT_SESSION()"))
        return str(result.scalar_one())

    async def cancel_session(self, engine: AsyncEngine, identifier: str) -> None:
        # SYSTEM$CANCEL_ALL_QUERIES(session_id) cancels every query currently
        # running in that session and leaves the session itself alive — the
        # same "soft cancel" semantics as Postgres's pg_cancel_backend/
        # MySQL's KILL QUERY, not the whole-session-terminating SYSTEM$
        # ABORT_SESSION (the KILL/MSSQL-equivalent primitive). A bind
        # parameter, like Postgres's pg_cancel_backend — Snowflake's
        # SYSTEM$ functions are ordinary function calls, not a literal-only
        # DDL-style statement the way MSSQL's KILL is.
        async with engine.connect() as conn:
            await conn.execute(
                sa.text("SELECT SYSTEM$CANCEL_ALL_QUERIES(:session_id)"),
                {"session_id": identifier},
            )

    def list_live_tables_extra_filter_sql(self) -> str:
        # Deliberately inherits the base class's "" no-op default (2026-08-06
        # security-invariant-reviewer finding — recorded explicitly here
        # rather than left as a silent inheritance, per QG-39's own lesson:
        # this exact default was silently WRONG once, for MySQL). Snowflake's
        # INFORMATION_SCHEMA is scoped per-database like Postgres's/MSSQL's,
        # not server-wide like MySQL's, so no extra restriction should be
        # needed — but this is a documentation-derived claim, unverified
        # against a live account (like everything else on this adapter), and
        # `schema/reflection.py`'s exclusion list (`information_schema`,
        # `pg_catalog`, ...) is lowercase while Snowflake's own default
        # identifier casing is uppercase, so if this method is ever reached
        # (today it cannot be — see the class docstring), the exclusion may
        # silently no-op rather than silently over- or under-restrict.
        # Verify both claims against a real account before item 157 lifts
        # `init_engine`'s guard, rather than trusting this comment.
        return ""


class BigQuerySessionAdapter(SessionDialectAdapter):
    """TODO.md item 19 phase 3 — like `SnowflakeSessionAdapter`, implemented
    for real where BigQuery has a real equivalent, but **never actually
    exercised**: `connections/engine.py`'s `init_engine` refuses to open a
    BigQuery connection before any of these methods can run, because
    `sqlalchemy_bigquery`'s DBAPI has no async driver — the same gap
    Snowflake's has. Confirming that the SIMPLE way Snowflake's was
    confirmed (constructing an async engine and reading the resulting
    `InvalidRequestError`) is not possible with no GCP credentials
    configured: BigQuery's dialect resolves real Google credentials and
    builds a live client at engine-construction time, and fails there
    FIRST — confirmed directly: with no credentials configured, the same
    attempt raises `google.auth.exceptions.DefaultCredentialsError` before
    SQLAlchemy's own async-driver check ever runs. (With a syntactically
    valid, if fake, credentials file supplied to get past that step, the
    identical `InvalidRequestError` Snowflake's driver raises — "The asyncio
    extension requires an async driver to be used. The loaded 'bigquery' is
    not async." — was also confirmed for BigQuery's driver; see
    `connections/models.py`'s `DatabaseDialect` docstring for the full
    sequencing.) See `is_connectable`'s override below.

    BigQuery is architecturally further from a session-oriented RDBMS than
    Snowflake is, in a way that goes beyond the missing async driver: it has
    NO session-scoped SQL statement at all. A BigQuery "connection" issues
    independent, stateless query jobs against Google's REST API — there is
    no SET/ALTER SESSION equivalent the way Postgres/MSSQL/MySQL/Snowflake
    all have one, and no default per-connection session identifier a later
    connection could target for cancellation (BigQuery's multi-statement
    SESSION feature is opt-in per query job via `create_session=True`/
    `connection_properties`, which this connect-time code never requests).
    `apply_session_guardrails` is therefore a genuine no-op, not a stub —
    there is nothing to SET — and `capture_session_identifier`/
    `cancel_session` raise rather than fabricate a plausible-looking
    identifier for a session that was never created (CLAUDE.md's engine
    philosophy: reject a genuine capability gap, don't emulate one). This
    means query-cancellation support (`Policy.allow_query_cancellation`)
    could not work for BigQuery even if the async-driver gap were somehow
    resolved — real additional scope the live-verification follow-up item in
    TODO.md now also tracks, since BigQuery's actual timeout/cancellation
    primitives are per-QUERY-JOB (`QueryJobConfig.job_timeout_ms`, `jobs.
    cancel`), a materially different mechanism than every other adapter's
    session-level one.
    """

    def is_connectable(self) -> bool:
        # The one override of the base class's `True` default, for the same
        # reason SnowflakeSessionAdapter's is — see the class docstring and
        # `is_connectable`'s own docstring on the base class.
        return False

    def build_engine_url(self, profile: ConnectionProfile) -> str:
        # Passthrough, like Postgres/MySQL/Snowflake: the operator's YAML
        # already supplies a complete `bigquery://project/dataset?...` URL
        # (see examples/connections.example.yaml).
        return profile.connection_string

    def build_connect_args(self, profile: ConnectionProfile, timeout_seconds: int) -> dict:
        # Deliberately empty, not merely unimplemented: confirmed by reading
        # the installed `sqlalchemy_bigquery.base.BigQueryDialect.
        # create_connect_args(self, url)` source directly — it builds the
        # `google.cloud.bigquery.Client` entirely from the URL's own query
        # parameters (credentials_path, location, arraysize, ...) and never
        # consults a `connect_args` dict at all, so `timeout_seconds` has
        # nowhere to go at this layer. BigQuery's real timeout model is
        # per-query-job (`QueryJobConfig.job_timeout_ms`), not per-connection
        # — a materially different mechanism from every other adapter's
        # connect-time (Snowflake's `login_timeout`) or post-connect
        # (MSSQL's pyodbc `timeout`) timeout. See the class docstring.
        return {}

    def register_query_timeout(self, engine: AsyncEngine, timeout_seconds: int) -> None:
        # No post-connect attribute to set, for the same reason
        # build_connect_args above returns {} — see that method's comment.
        return None

    async def apply_session_guardrails(
        self,
        session: AsyncSession,
        *,
        lock_timeout_seconds: int,
        statement_timeout_seconds: int,
    ) -> None:
        # A genuine no-op, not a stub: BigQuery has no SET/ALTER SESSION
        # statement at all to send — see the class docstring.
        return None

    async def capture_session_identifier(self, session: AsyncSession) -> str:
        # Reject, don't fabricate: BigQuery creates no default per-connection
        # session to capture an identifier for — see the class docstring.
        # Unreachable in production either way: connections/engine.py's
        # init_engine refuses to open a BigQuery connection at all before a
        # session is ever created.
        raise NotImplementedError(
            "BigQuery has no default per-connection session to capture an "
            "identifier for (see BigQuerySessionAdapter's docstring) — and this "
            "method is unreachable in production, since init_engine refuses to "
            "open a BigQuery connection before a session exists."
        )

    async def cancel_session(self, engine: AsyncEngine, identifier: str) -> None:
        # Same reasoning as capture_session_identifier above: there is no
        # session-level cancellation primitive to target with a captured
        # identifier that was never captured.
        raise NotImplementedError(
            "BigQuery has no session-level cancellation primitive to target (see "
            "BigQuerySessionAdapter's docstring) — and this method is unreachable "
            "in production for the same reason capture_session_identifier is."
        )

    def list_live_tables_extra_filter_sql(self) -> str:
        # BigQuery's INFORMATION_SCHEMA.TABLES is dataset-scoped (queried as
        # `<project>.<dataset>.INFORMATION_SCHEMA.TABLES`) — the same
        # per-database scoping reasoning as Postgres's/MSSQL's, not MySQL's
        # server-wide view. Deliberately inherits the base class's ""
        # no-op default, recorded explicitly here rather than left as a
        # silent inheritance (2026-08-06 security-invariant-reviewer finding
        # on this exact default for Snowflake, applied here too) — this is a
        # documentation-derived claim, unverified against a live project
        # like everything else on this adapter, and unreachable today (see
        # the class docstring) regardless.
        return ""


_SESSION_ADAPTERS: Dict[DatabaseDialect, SessionDialectAdapter] = {
    DatabaseDialect.POSTGRESQL: PostgresSessionAdapter(),
    DatabaseDialect.MSSQL: MSSQLSessionAdapter(),
    DatabaseDialect.MYSQL: MySQLSessionAdapter(),
    DatabaseDialect.SNOWFLAKE: SnowflakeSessionAdapter(),
    DatabaseDialect.BIGQUERY: BigQuerySessionAdapter(),
}


def get_session_adapter(dialect: DatabaseDialect) -> SessionDialectAdapter:
    adapter = _SESSION_ADAPTERS.get(dialect)
    if adapter is None:
        raise ValueError(f"Unsupported dialect: {dialect}")
    return adapter


# --- Thin dispatchers (preserve the module's public API for existing callers) ---


def build_engine_url(profile: ConnectionProfile) -> str:
    return get_session_adapter(profile.dialect).build_engine_url(profile)


def build_connect_args(profile: ConnectionProfile, timeout_seconds: int) -> dict:
    return get_session_adapter(profile.dialect).build_connect_args(profile, timeout_seconds)


def register_query_timeout(
    engine: AsyncEngine, dialect: DatabaseDialect, timeout_seconds: int
) -> None:
    get_session_adapter(dialect).register_query_timeout(engine, timeout_seconds)


async def apply_session_guardrails(
    session: AsyncSession,
    dialect: DatabaseDialect,
    *,
    lock_timeout_seconds: int,
    statement_timeout_seconds: int,
) -> None:
    await get_session_adapter(dialect).apply_session_guardrails(
        session,
        lock_timeout_seconds=lock_timeout_seconds,
        statement_timeout_seconds=statement_timeout_seconds,
    )


async def capture_session_identifier(session: AsyncSession, dialect: DatabaseDialect) -> str:
    return await get_session_adapter(dialect).capture_session_identifier(session)


async def cancel_session(engine: AsyncEngine, dialect: DatabaseDialect, identifier: str) -> None:
    await get_session_adapter(dialect).cancel_session(engine, identifier)


def list_live_tables_extra_filter_sql(dialect: DatabaseDialect) -> str:
    return get_session_adapter(dialect).list_live_tables_extra_filter_sql()


def not_connectable_explanation(dialect: DatabaseDialect) -> str:
    """The shared clause behind every `is_connectable() == False` rejection —
    `connections/engine.py`'s `init_engine` (the PRIMARY connection guard)
    and `validation/schema_validation.py`'s `resolve_query_table_connections`
    (the SECONDARY/joined-connection guard, TODO.md item 163) each build
    their own `ConfigValidationError` with different context-specific framing
    (which connection/table triggered it), but both need the identical
    "cannot yet open a live connection for this dialect, here's why" body —
    factored out once so the two call sites can't quietly drift apart
    (2026-08-07 `architecture-boundary-reviewer` finding on item 163's own
    audit). Callers prepend their own context and a colon/dash before this.
    """
    return (
        f"QueryGate cannot yet open a live connection for dialect {dialect!r}: its "
        "SessionDialectAdapter is registered but not connectable (see "
        "SessionDialectAdapter.is_connectable's docstring for why — Snowflake's "
        "and BigQuery's drivers both have no async SQLAlchemy engine support, and "
        "this codebase's execution pipeline requires one). Its compiler/session "
        "adapters exist for rendering-level development and testing only "
        "(TODO.md item 19) — see its live-verification follow-up item."
    )
