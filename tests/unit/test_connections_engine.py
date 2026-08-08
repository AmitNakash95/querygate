"""Unit tests for `connections/engine.py`'s `init_engine` — specifically the
Snowflake (TODO.md item 19 phase 2) and BigQuery (item 19 phase 3) guards.

`create_async_engine` is lazy (it does not actually open a network connection
at construction time), so `init_engine` is safely unit-testable for the three
live-verified dialects too: this just proves the guard fires ONLY for
Snowflake/BigQuery, before `create_async_engine` is ever reached, rather than
letting one of those profiles fall through to SQLAlchemy's own (correct, but
confusing out of context) `InvalidRequestError` — or, for BigQuery
specifically, its OWN even-earlier `DefaultCredentialsError` from Google's
auth library (confirmed directly: `sqlalchemy_bigquery`'s
`create_connect_args` builds a real `google.cloud.bigquery.Client` at engine-
construction time, so a bare `create_async_engine("bigquery://...")` with no
credentials configured fails there before SQLAlchemy's own async-driver check
even runs).
"""

from __future__ import annotations

import pathlib

import pytest

from querygate.connections.engine import init_engine, reset_engines
from querygate.connections.registry import set_registry
from querygate.core.exceptions import ConfigValidationError

from tests.conftest import make_demo_registry

pytestmark = pytest.mark.unit


def test_snowflake_profile_is_rejected_before_create_async_engine():
    set_registry(
        make_demo_registry(
            dialect="snowflake",
            connection_string="snowflake://user:pass@myaccount/mydb/myschema?warehouse=wh",
        )
    )
    # ConfigValidationError, not a bare ValueError (2026-08-06
    # security-invariant-reviewer finding): a bare ValueError is not in
    # api/_errors.py's _ACTIONABLE tuple, so it would be masked to an opaque
    # REST 500 / MCP INTERNAL rather than the explained, client-actionable
    # rejection this guard exists to give.
    with pytest.raises(ConfigValidationError, match="cannot yet open a live connection"):
        init_engine("demo")


def test_bigquery_profile_is_rejected_before_create_async_engine():
    set_registry(
        make_demo_registry(
            dialect="bigquery",
            connection_string="bigquery://my-project/my_dataset",
        )
    )
    # Same actionable-error requirement as the Snowflake test above — and,
    # for BigQuery specifically, this also proves the guard fires BEFORE
    # `create_connect_args`'s own credential resolution ever runs (this test
    # has no GOOGLE_APPLICATION_CREDENTIALS configured, so an unguarded call
    # would fail with google.auth.exceptions.DefaultCredentialsError instead
    # — a confusing, un-actionable error from a different library, not this
    # one's own ConfigValidationError).
    with pytest.raises(ConfigValidationError, match="cannot yet open a live connection"):
        init_engine("demo")


@pytest.mark.parametrize(
    "dialect,connection_string",
    [
        ("postgresql", "postgresql+asyncpg://user:pass@localhost/demo"),
        ("mysql", "mysql+asyncmy://user:pass@localhost/demo"),
        ("mssql", "mssql+aioodbc://user:pass@localhost/demo"),
    ],
)
def test_the_other_three_dialects_are_unaffected_by_the_snowflake_guard(dialect, connection_string):
    """Regression guard for the guard itself: it must not accidentally widen
    to reject a real, working dialect."""
    set_registry(make_demo_registry(dialect=dialect, connection_string=connection_string))
    try:
        engine = init_engine("demo")
        assert engine is not None
    finally:
        reset_engines()


def test_create_async_engine_has_exactly_one_production_call_site():
    """TODO.md item 19 phase 2 — 2026-08-06 `test-contract-reviewer` finding:
    the claim that `init_engine`'s `is_connectable()` guard is the SOLE gate
    a Snowflake (or any not-yet-connectable) profile could reach
    `create_async_engine` through is provable only as long as no second
    production call site is ever added that bypasses it. A future change
    (e.g. a dialect-specific cost estimator opening its own engine, or a
    shortcut added to `session_scope`) that constructs an `AsyncEngine`
    without going through `connections/engine.py`'s `init_engine` would
    silently reopen the exact "no async driver" failure this guard exists to
    prevent, with nothing here going red. Grepping the shipped source is the
    same single-source-of-truth technique `scripts/check_worklist.py`/
    `test_worklist_consistency.py` already use elsewhere in this repo.
    """
    src_root = pathlib.Path(__file__).resolve().parents[2] / "src" / "querygate"
    call_sites = []
    for path in src_root.rglob("*.py"):
        text = path.read_text()
        if "create_async_engine(" in text:
            call_sites.append(str(path.relative_to(src_root)))
    assert call_sites == ["connections/engine.py"], (
        f"create_async_engine(...) now appears in {call_sites!r}, not just "
        "connections/engine.py — a new call site bypasses init_engine's "
        "is_connectable() guard (and its policy/timeout wiring)."
    )


def test_no_production_module_imports_the_never_connectable_dialect_drivers():
    """2026-08-07 security-invariant-reviewer finding: the pyproject.toml
    comments for `snowflake-sqlalchemy`/`sqlalchemy-bigquery` (both dev-only
    dependencies) both claim "no module under src/querygate/ imports"
    either package — a real security-relevant claim (it's the whole
    justification for NOT shipping their TLS/crypto/gRPC/auth stacks in the
    release container/package). That claim was convention-only, unlike the
    `create_async_engine` single-call-site claim just above, which
    `test_create_async_engine_has_exactly_one_production_call_site` already
    machine-checks. This is that same grep-based technique applied to the
    two driver packages themselves, so the claim can't silently drift.
    """
    src_root = pathlib.Path(__file__).resolve().parents[2] / "src" / "querygate"
    forbidden = ("snowflake.sqlalchemy", "snowflake.connector", "sqlalchemy_bigquery")
    offenders = []
    for path in src_root.rglob("*.py"):
        text = path.read_text()
        for name in forbidden:
            if f"import {name}" in text or f"from {name}" in text:
                offenders.append((str(path.relative_to(src_root)), name))
    assert offenders == [], (
        f"production module(s) import a never-connectable dialect driver: {offenders!r} — "
        "this defeats the whole point of keeping snowflake-sqlalchemy/sqlalchemy-bigquery "
        "dev-only (see their pyproject.toml comments)."
    )
