"""Unit tests for `connections/engine.py`'s `init_engine` — specifically the
Snowflake guard (TODO.md item 19 phase 2).

`create_async_engine` is lazy (it does not actually open a network connection
at construction time), so `init_engine` is safely unit-testable for the three
live-verified dialects too: this just proves the guard fires ONLY for
Snowflake, before `create_async_engine` is ever reached, rather than letting a
Snowflake profile fall through to SQLAlchemy's own (correct, but confusing
out of context) `InvalidRequestError`.
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
