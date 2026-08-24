"""Unit tests for demo/baseline_mcp/server.py's startup guard and the
shared demo/_dsn_guard.py module it (and demo/db/seed.py) call into.

DEMO-ONLY. Deliberately lives under demo/, not tests/, and is NOT part of
the product's own test suite: pyproject.toml's `[tool.pytest.ini_options]`
sets `testpaths = ["tests"]`, so neither a bare `pytest` nor `pytest -m unit`
run from the repo root collects this file at all. It only runs when invoked
directly, e.g.:

    poetry run pytest demo/baseline_mcp/test_guard.py -v

Covers every branch of check_startup_guard/validate_dsn: the
QUERYGATE_DEMO_UNSAFE_BASELINE opt-in, non-local host, wrong port, wrong
database name, wrong role, non-Postgres scheme, multi-host DSNs (including
the S1 bypass -- a comma placed before the DSN's *last* '@', which a
naive "split on the last '@'" parser resolves differently from asyncpg's
own "split on the FIRST '@'" parser), the DSN-parse-failure branch, the
success case, the call-time re-check in `_dsn()`, and `main()`'s
fail-closed SystemExit. `test_guard_matches_asyncpg_resolution` is a
differential test: it feeds a range of candidate DSNs to asyncpg's own
private parser and asserts that whenever this guard says a DSN is allowed,
asyncpg agrees on where it actually points -- the property that would have
caught both the original multi-host bypass and the S1 regression of it,
rather than pinning one specific string.
"""

import ast
import asyncio
import inspect
import sys
import textwrap
from pathlib import Path

import pytest

# No __init__.py in this directory and it is outside pyproject's
# `pythonpath = ["."]` package root, so make the sibling module importable
# when this file is run directly (pytest's default "prepend" import mode
# already does this, but the explicit insert keeps `python -m pytest
# demo/baseline_mcp/test_guard.py` and direct `python test_guard.py`
# invocations working too).
sys.path.insert(0, str(Path(__file__).resolve().parent))
# demo/ itself is also not a package (see demo/_dsn_guard.py's docstring) --
# insert it directly too, rather than relying on importing `server` (which
# does this same insert as a side effect) to happen first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# demo/db/ holds seed.py, a third consumer of demo/_dsn_guard.py -- insert
# it too so this file can exercise seed.py's own guard *wiring* (F2 below),
# not just demo/_dsn_guard.py's validate_dsn in isolation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "db"))

import server  # noqa: E402
import seed as seed_module  # noqa: E402
from _dsn_guard import (  # noqa: E402
    ALLOWED_HOSTS,
    ALLOWED_PORT,
    REQUIRED_DB_NAME,
    validate_dsn,
)
from server import (  # noqa: E402
    DEFAULT_DSN,
    DSN_ENV_VAR,
    REQUIRED_DB_USER,
    UNSAFE_BASELINE_ENV_VAR,
    UNSAFE_BASELINE_REQUIRED_VALUE,
    check_startup_guard,
    resolve_dsn,
)

VALID_ENV = {
    UNSAFE_BASELINE_ENV_VAR: UNSAFE_BASELINE_REQUIRED_VALUE,
    DSN_ENV_VAR: DEFAULT_DSN,
}


def test_guard_passes_with_all_checks_satisfied():
    assert check_startup_guard(VALID_ENV) is None


def test_guard_rejects_missing_unsafe_baseline_flag():
    env = dict(VALID_ENV)
    del env[UNSAFE_BASELINE_ENV_VAR]
    reason = check_startup_guard(env)
    assert reason is not None
    assert UNSAFE_BASELINE_ENV_VAR in reason


def test_guard_rejects_wrong_unsafe_baseline_value():
    env = dict(VALID_ENV)
    env[UNSAFE_BASELINE_ENV_VAR] = "yes"
    reason = check_startup_guard(env)
    assert reason is not None
    assert UNSAFE_BASELINE_ENV_VAR in reason


def test_guard_rejects_non_local_host():
    env = dict(VALID_ENV)
    env[DSN_ENV_VAR] = (
        "postgresql://agent_ro:agent_ro_demo_pw_only@db.example.com:5544/" "querygate_demo_pitch"
    )
    reason = check_startup_guard(env)
    assert reason is not None
    assert "localhost/127.0.0.1" in reason


def test_guard_rejects_non_local_host_even_when_it_looks_local_in_the_path():
    # A sneaky DSN that mentions localhost only in the path/db name, not the
    # actual host, must still be rejected on the real host.
    env = dict(VALID_ENV)
    env[DSN_ENV_VAR] = "postgresql://agent_ro:pw@evil.example.com:5544/querygate_demo_pitch"
    reason = check_startup_guard(env)
    assert reason is not None
    assert "evil.example.com" in reason


def test_guard_rejects_wrong_database_name():
    env = dict(VALID_ENV)
    env[DSN_ENV_VAR] = (
        "postgresql://agent_ro:agent_ro_demo_pw_only@127.0.0.1:5544/" "production_customers"
    )
    reason = check_startup_guard(env)
    assert reason is not None
    assert "querygate_demo_pitch" in reason


def test_guard_accepts_localhost_spelled_as_localhost_not_only_127_0_0_1():
    env = dict(VALID_ENV)
    env[DSN_ENV_VAR] = (
        "postgresql://agent_ro:agent_ro_demo_pw_only@localhost:5544/" "querygate_demo_pitch"
    )
    assert check_startup_guard(env) is None


def test_guard_uses_default_dsn_when_env_var_unset():
    env = {UNSAFE_BASELINE_ENV_VAR: UNSAFE_BASELINE_REQUIRED_VALUE}
    assert check_startup_guard(env) is None


def test_guard_rejects_comma_separated_host_list_even_when_the_first_host_is_local():
    # libpq/asyncpg DSNs support multiple comma-separated hosts with
    # automatic failover. This DSN's netloc has no '@' before the comma, so
    # both a "last @" and a "first @" parser agree it names two hosts --
    # this pins the ordinary case, distinct from the S1 regression test
    # below which pins the case where they disagree.
    env = dict(VALID_ENV)
    env[DSN_ENV_VAR] = (
        "postgresql://agent_ro:pw@localhost:5544,evil.example.com:5432/querygate_demo_pitch"
    )
    reason = check_startup_guard(env)
    assert reason is not None
    assert "more than one host" in reason


def test_guard_rejects_comma_separated_host_list_127_variant():
    env = dict(VALID_ENV)
    env[DSN_ENV_VAR] = (
        "postgresql://agent_ro:pw@127.0.0.1:5544,evil.example.com:5432/querygate_demo_pitch"
    )
    reason = check_startup_guard(env)
    assert reason is not None
    assert "more than one host" in reason


def test_guard_rejects_the_confirmed_s1_bypass_literal_reviewer_example():
    # THE confirmed bypass, in the exact form a security review demonstrated
    # against the real installed asyncpg. The old guard used
    # `netloc.rpartition("@")[2]` (the LAST '@') to find the hostspec, which
    # resolves to "localhost" here -- looking like a single safe host. But
    # asyncpg's own parser (asyncpg.connect_utils
    # ._parse_connect_dsn_and_args) partitions on the FIRST '@', so it sees
    # the hostspec as "evil.example.com,x@localhost" and splits that on ','
    # into TWO hosts, dialing evil.example.com first:
    #
    #   >>> asyncpg.connect_utils._parse_connect_dsn_and_args(dsn=..., ...)
    #   ([('evil.example.com', 5432), ('x@localhost', 5432)], ...)
    #
    # This DSN has no explicit port, so the S3 port check alone is already
    # enough to reject it. test_split_dsn_netloc_partitions_on_first_at_not
    # _last and test_dsn_hostspec_entries_from_first_at_split_yields_two
    # _hosts below are the ones that isolate the multi-host-derivation fix
    # specifically. This test just pins the literal reported string.
    env = dict(VALID_ENV)
    env[DSN_ENV_VAR] = "postgresql://u@evil.example.com,x@localhost/querygate_demo_pitch"
    reason = check_startup_guard(env)
    assert reason is not None


def test_guard_rejects_the_s1_bypass_with_matching_ports_on_both_hosts():
    # Same bypass shape as the literal reviewer example above, but with an
    # explicit, otherwise-valid port (5544) on BOTH hostspec entries, so the
    # S3 port check alone cannot explain a rejection here the way it can for
    # the unported literal example. Confirmed against the real installed
    # asyncpg that it still dials two hosts even with matching ports:
    #
    #   >>> asyncpg.connect_utils._parse_connect_dsn_and_args(dsn=..., ...)
    #   ([('evil.example.com', 5544), ('x@localhost', 5544)], ...)
    #
    # (Not a fully isolated unit test of the multi-host derivation alone --
    # the S3 user check also independently rejects this particular string,
    # since a naive "last @" split corrupts the parsed user too. See
    # test_split_dsn_netloc_partitions_on_first_at_not_last and
    # test_dsn_hostspec_entries_from_first_at_split_yields_two_hosts below
    # for the surgical, single-cause pin on the multi-host derivation
    # itself.)
    env = dict(VALID_ENV)
    env[DSN_ENV_VAR] = "postgresql://u@evil.example.com:5544,x@localhost:5544/querygate_demo_pitch"
    reason = check_startup_guard(env)
    assert reason is not None


def test_split_dsn_netloc_partitions_on_first_at_not_last():
    # Surgical pin on the exact S1 root cause, independent of every other
    # check in validate_dsn: asyncpg splits a DSN's netloc into
    # (userinfo, hostspec) on the FIRST '@'. `netloc.rpartition("@")` (the
    # bug) would instead give auth="u@evil.example.com,x",
    # hostspec="localhost" here -- a completely different, wrong split.
    from _dsn_guard import _split_dsn_netloc

    auth, hostspec = _split_dsn_netloc("u@evil.example.com,x@localhost")
    assert auth == "u"
    assert hostspec == "evil.example.com,x@localhost"


def test_dsn_hostspec_entries_from_first_at_split_yields_two_hosts():
    # Continuing the pin above: the correctly-derived hostspec, split on
    # ',', must yield the two entries asyncpg itself dials (it dials
    # "evil.example.com" FIRST).
    from _dsn_guard import _dsn_hostspec_entries

    entries = _dsn_hostspec_entries("evil.example.com,x@localhost")
    assert entries == ["evil.example.com", "x@localhost"]


def test_resolve_hostspec_entry_treats_a_leading_slash_as_a_unix_socket_not_a_tcp_host():
    # F3 follow-up: a hostspec entry starting with '/' can never actually
    # arise from a DSN's netloc (URL grammar terminates netloc at the
    # first unescaped '/', so this branch is unreachable via any real
    # `dsn=...` string -- asyncpg itself only reaches its own equivalent
    # branch via the `?host=/path` query-string route, which this guard
    # rejects outright before ever inspecting a hostspec; see D3). Pin the
    # branch directly at the unit level instead, so it is genuinely
    # exercised rather than silently untested: if ever reached, it must
    # resolve to "not a TCP host" (None, None), never accidentally match
    # something in ALLOWED_HOSTS.
    from _dsn_guard import _resolve_hostspec_entry

    assert _resolve_hostspec_entry("/var/run/postgresql") == (None, None)
    assert _resolve_hostspec_entry("/var/run/postgresql:5544") == (None, None)


def test_guard_rejects_non_postgres_scheme():
    env = dict(VALID_ENV)
    env[DSN_ENV_VAR] = "mysql://agent_ro:pw@127.0.0.1:5544/querygate_demo_pitch"
    reason = check_startup_guard(env)
    assert reason is not None
    assert "mysql" in reason


def test_guard_accepts_sqlalchemy_style_scheme_used_by_demo_config_env_demo():
    # PITCH_AGENT_RO_DSN is shared with demo/config/env.demo, which sets it
    # to the SQLAlchemy 'postgresql+asyncpg://' form for QueryGate's own
    # connection loader. This server must accept the same env var value.
    env = dict(VALID_ENV)
    env[DSN_ENV_VAR] = (
        "postgresql+asyncpg://agent_ro:agent_ro_demo_pw_only@127.0.0.1:5544/querygate_demo_pitch"
    )
    assert check_startup_guard(env) is None
    # And resolve_dsn() must hand asyncpg the normalized, driver-suffix-free
    # scheme, not the raw SQLAlchemy one.
    assert resolve_dsn(env).startswith("postgresql://")


# --- S3: port and role checks -----------------------------------------------


def test_guard_rejects_wrong_port():
    # An SSH local forward (`ssh -L 5599:prod-db:5432 bastion`) makes a
    # genuinely remote production database answer on 127.0.0.1 -- on a port
    # other than the pitch-demo database's own 5544.
    env = dict(VALID_ENV)
    env[DSN_ENV_VAR] = "postgresql://agent_ro:pw@127.0.0.1:5599/querygate_demo_pitch"
    reason = check_startup_guard(env)
    assert reason is not None
    assert "5599" in reason
    assert str(ALLOWED_PORT) in reason


def test_guard_rejects_dsn_with_no_explicit_port():
    # No explicit port falls back to Postgres's ambient default/PGPORT, not
    # a value this check can verify -- reject rather than assume safe.
    env = dict(VALID_ENV)
    env[DSN_ENV_VAR] = "postgresql://agent_ro:pw@127.0.0.1/querygate_demo_pitch"
    reason = check_startup_guard(env)
    assert reason is not None


def test_guard_rejects_wrong_role():
    # A DSN for pitch_owner (which owns every table) would silently falsify
    # the demo's central claim that this server and QueryGate's own MCP
    # server connect with identical, genuinely read-only privileges.
    env = dict(VALID_ENV)
    env[DSN_ENV_VAR] = (
        "postgresql://pitch_owner:pitch_owner_demo_pw_only@127.0.0.1:5544/querygate_demo_pitch"
    )
    reason = check_startup_guard(env)
    assert reason is not None
    assert "pitch_owner" in reason
    assert REQUIRED_DB_USER in reason


def test_guard_rejects_missing_user():
    env = dict(VALID_ENV)
    env[DSN_ENV_VAR] = "postgresql://127.0.0.1:5544/querygate_demo_pitch"
    reason = check_startup_guard(env)
    assert reason is not None
    assert REQUIRED_DB_USER in reason


# --- S4: the previously-unreproducible "leaks the password" premise --------


def test_guard_rejects_unparseable_dsn_with_the_exact_reason():
    # A prior version of this test asserted only that the password did not
    # appear in the reason string, on the premise that CPython's
    # urlparse/urlsplit can embed a DSN's password in a ValueError message
    # for some malformed inputs (e.g. an invalid IPv6 host literal). A
    # security review probed this directly against the installed CPython
    # (3.11.9) with a range of malformed DSNs, including this exact one,
    # and found every reachable ValueError names only the malformed
    # host/bracket fragment -- never the userinfo/password. The premise
    # could not be reproduced, so a test that only checks the password's
    # absence would also pass if the redaction were removed entirely (i.e.
    # it wasn't actually testing the redaction). Assert the exact reason
    # string instead, so a reworded or regressed message fails this test.
    env = dict(VALID_ENV)
    env[DSN_ENV_VAR] = "postgresql://agent_ro:supersecretpw@[::1:5544/querygate_demo_pitch"
    reason = check_startup_guard(env)
    assert reason == (
        f"refusing to proceed: the DSN in {DSN_ENV_VAR} could not be parsed as a URL."
    )


def test_reason_never_contains_any_part_of_a_password_containing_an_at_sign():
    # D1 (2026-08-23 security review): the ValueError-message channel
    # above turned out to be unreproducible, but a REAL leak channel was
    # found and confirmed against the real installed asyncpg: if a DSN's
    # password itself contains a literal '@', `_split_dsn_netloc` (which
    # correctly partitions on the FIRST '@', per S1) puts the REMAINDER of
    # the password into `hostspec`. Before the fix, that remainder then
    # got echoed verbatim into the "not localhost/127.0.0.1" (or "more
    # than one host") rejection reason -- printed to stderr, straight into
    # `make pitch-up` output, potentially onto a projector, in exactly the
    # stale/mistyped-DSN-pointing-at-a-real-database scenario S2 exists to
    # catch. asyncpg still resolves this DSN to a single, otherwise valid
    # host (confirmed: `[('127.0.0.1', 5544)]`), so the ONLY reason this
    # is rejected is the extra '@' -- exercise that specifically.
    secret = "sup3rSecretProdPw"
    reason = validate_dsn(
        f"postgresql://agent_ro:A@{secret}@127.0.0.1:5544/querygate_demo_pitch",
        required_user="agent_ro",
        dsn_env_var=DSN_ENV_VAR,
    )
    assert reason is not None
    assert secret not in reason
    assert "A@" not in reason


def test_reason_never_contains_a_password_fragment_from_a_multi_host_at_sign():
    # Same leak channel, exercised through the multi-host code path (a
    # comma AND an extra '@') instead of the single-host path above --
    # confirms the fix closes both interpolation sites (the multi-host
    # message used to echo `hostspec!r` directly).
    secret = "prod.internal"
    reason = validate_dsn(
        f"postgresql://pitch_owner:A@b,c@{secret}:5432/appdb",
        required_user="pitch_owner",
        dsn_env_var="QUERYGATE_PITCH_DB_DSN",
    )
    assert reason is not None
    assert secret not in reason


# --- D2: the S1 fix must not be revertible with a green suite --------------


def test_guard_rejects_s1_shape_where_only_the_multi_host_derivation_saves_it():
    # D2 (2026-08-23 security review): every existing S1-shape test above
    # is independently rejected by some OTHER check too (the port-required
    # check, or the D1 fix above), so none of them alone proves the
    # multi-host DERIVATION is what's doing the work at the validate_dsn
    # level -- only the two low-level pins on _split_dsn_netloc/
    # _dsn_hostspec_entries do. This DSN was specifically chosen so that
    # EVERY other field parses to a valid value under the OLD, buggy
    # `rpartition("@")` derivation too (user still resolves to "agent_ro"
    # because usernames don't contain '@'/':' in any of these DSNs; host
    # resolves to "localhost"; port 5544; db name valid) -- confirmed
    # in-process that reverting _split_dsn_netloc to rpartition semantics
    # makes this exact DSN ALLOWED. Whichever specific check ends up
    # catching it under the CURRENT (fixed) code -- this repo's other D1
    # fix means it's now the "more than one '@'" check rather than "more
    # than one host" -- is fine; what this test pins is that it is
    # rejected AT ALL, which flips to allowed the moment the underlying
    # first-'@' derivation regresses.
    env = dict(VALID_ENV)
    env[DSN_ENV_VAR] = (
        "postgresql://agent_ro:pw@evil.example.com:5544,x@localhost:5544/querygate_demo_pitch"
    )
    reason = check_startup_guard(env)
    assert reason is not None


# --- D3: the DSN's query string must not be a silent bypass channel --------


def test_guard_rejects_a_dsn_carrying_a_query_string():
    # D3 (2026-08-23 security review): asyncpg honours host/port/dbname/
    # database/user/password/passfile/sslmode/options from a DSN's query
    # string; some are only used when the corresponding netloc/path value
    # is absent, but `options` (and any other unrecognised key) is
    # forwarded into the Postgres session UNCONDITIONALLY, regardless of
    # what the netloc says. Confirmed against the real installed asyncpg
    # that an otherwise fully valid DSN with
    # `?options=-c%20default_transaction_read_only%3Doff` reaches the
    # server able to flip the exact session-level read-only setting
    # (`ALTER ROLE agent_ro SET default_transaction_read_only = on`,
    # demo/db/02_roles.sql) this demo's "same genuinely read-only agent_ro
    # role" claim rests on. This guard validates only the netloc and
    # path, so any query string at all is rejected outright.
    env = dict(VALID_ENV)
    env[DSN_ENV_VAR] = DEFAULT_DSN + "?options=-c%20default_transaction_read_only%3Doff"
    reason = check_startup_guard(env)
    assert reason is not None
    assert "query" in reason.lower()


# --- D4: the database-name derivation must mirror asyncpg exactly ----------


def test_guard_rejects_a_database_name_with_a_double_leading_slash():
    # D4 (2026-08-23 security review): asyncpg strips exactly ONE leading
    # '/' from the path (then unquotes); `.lstrip("/")` used to strip
    # EVERY leading '/', so a path of "//querygate_demo_pitch" resolved to
    # the allowed db name "querygate_demo_pitch" under the old code, while
    # asyncpg itself requests a database literally named
    # "/querygate_demo_pitch" (confirmed: `database='/querygate_demo_pitch'`).
    # That specific database does not exist, so the old bug failed closed
    # in practice -- but the guard's own claim to validate the database
    # asyncpg will actually use was false for this shape.
    env = dict(VALID_ENV)
    env[DSN_ENV_VAR] = (
        "postgresql://agent_ro:agent_ro_demo_pw_only@127.0.0.1:5544//querygate_demo_pitch"
    )
    reason = check_startup_guard(env)
    assert reason is not None
    assert "querygate_demo_pitch" in reason


# --- Call-time re-check: `_dsn()` and `main()` ------------------------------


def test_dsn_raises_when_the_unsafe_baseline_flag_is_dropped(monkeypatch):
    # _dsn() re-runs the startup guard against the REAL process environment
    # at call time (not a value captured once at import/process-start), so
    # it must fail closed the moment the environment stops satisfying the
    # guard -- this is the enforcement that actually protects every tool
    # call, not just process startup.
    monkeypatch.setenv(DSN_ENV_VAR, DEFAULT_DSN)
    monkeypatch.delenv(UNSAFE_BASELINE_ENV_VAR, raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        server._dsn()
    assert UNSAFE_BASELINE_ENV_VAR in str(excinfo.value)


def test_dsn_raises_when_the_dsn_env_var_points_at_a_non_local_host(monkeypatch):
    monkeypatch.setenv(UNSAFE_BASELINE_ENV_VAR, UNSAFE_BASELINE_REQUIRED_VALUE)
    monkeypatch.setenv(DSN_ENV_VAR, "postgresql://agent_ro:pw@evil.example.com:5544/db")
    with pytest.raises(RuntimeError):
        server._dsn()


def test_dsn_returns_the_resolved_dsn_when_the_environment_is_valid(monkeypatch):
    monkeypatch.setenv(UNSAFE_BASELINE_ENV_VAR, UNSAFE_BASELINE_REQUIRED_VALUE)
    monkeypatch.setenv(DSN_ENV_VAR, DEFAULT_DSN)
    assert server._dsn() == DEFAULT_DSN


def test_main_exits_nonzero_without_starting_the_server_when_the_guard_fails(monkeypatch):
    monkeypatch.delenv(UNSAFE_BASELINE_ENV_VAR, raising=False)
    monkeypatch.setenv(DSN_ENV_VAR, DEFAULT_DSN)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("mcp_server.run() must not be reached when the startup guard fails")

    monkeypatch.setattr(server.mcp_server, "run", _fail_if_called)

    with pytest.raises(SystemExit) as excinfo:
        server.main()
    assert excinfo.value.code == 1


def test_seed_main_exits_nonzero_without_connecting_when_the_guard_fails(monkeypatch):
    # F2: demo/db/seed.py's own guard *wiring* (the
    # `if reason is not None: raise SystemExit(1)` block in its `main()`,
    # immediately before `asyncpg.connect(DSN)`) had no test at all --
    # invert it, delete it, or replace it with `pass`, and no test
    # anywhere goes red, while that block is the ONLY thing standing
    # between a stale/mistyped QUERYGATE_PITCH_DB_DSN and a five-table
    # TRUNCATE. Mirrors test_main_exits_nonzero_without_starting_the_
    # server_when_the_guard_fails above: monkeypatch seed.py's own module-
    # level DSN to something the guard must reject, monkeypatch
    # asyncpg.connect to raise if it is EVER called, and assert
    # SystemExit(1).
    monkeypatch.setattr(
        seed_module,
        "DSN",
        "postgresql://pitch_owner:pw@evil.example.com:5544/querygate_demo_pitch",
    )

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("asyncpg.connect() must not be reached when the seed guard fails")

    monkeypatch.setattr(seed_module.asyncpg, "connect", _fail_if_called)

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(seed_module.main())
    assert excinfo.value.code == 1


def test_seed_main_proceeds_to_connect_when_the_guard_passes(monkeypatch):
    # The mirror image of the test above, so a mutation that makes the
    # guard unconditionally raise SystemExit (as opposed to one that makes
    # it unconditionally NOT raise) would also be caught: with a DSN the
    # guard accepts, main() must reach asyncpg.connect(). Stops main() at
    # that exact point by having the stubbed connect() raise a sentinel
    # exception rather than actually connecting to anything.
    monkeypatch.setattr(seed_module, "DSN", seed_module.DEFAULT_DSN)

    class _StoppedAfterConnectCall(Exception):
        pass

    def _raise_sentinel(*args, **kwargs):
        raise _StoppedAfterConnectCall()

    monkeypatch.setattr(seed_module.asyncpg, "connect", _raise_sentinel)

    with pytest.raises(_StoppedAfterConnectCall):
        asyncio.run(seed_module.main())


def _contains_a_real_call_to(source: str, function_name: str) -> bool:
    """Parse `source` and return True iff it contains an actual
    `ast.Call` node invoking a bare name `function_name` (e.g. `_dsn()`)
    -- not merely the substring anywhere in the source (a comment, a
    docstring, an unrelated string literal). `source` is dedented first
    since `inspect.getsource` on a module-level function already starts at
    column 0, but this keeps the helper correct if ever pointed at a
    nested/indented function too.
    """
    tree = ast.parse(textwrap.dedent(source))
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == function_name
        for node in ast.walk(tree)
    )


def test_each_tool_body_calls_dsn_at_call_time():
    # Swapping `_dsn()` for a direct env read (or a captured module-level
    # constant) in any tool body would silently remove the call-time
    # re-check `_dsn()` provides, while every test above -- which only
    # exercises check_startup_guard/validate_dsn directly -- would stay
    # green. A naive `"_dsn()" in source` substring check has its own gap:
    # a rewritten body that reads the environment directly but leaves a
    # decoy comment/string mentioning "_dsn()" would still pass it. Parse
    # the real AST instead and require an actual `ast.Call` to a bare name
    # `_dsn`.
    for tool in (server.execute_sql, server.list_tables, server.describe_table):
        source = inspect.getsource(tool)
        assert _contains_a_real_call_to(
            source, "_dsn"
        ), f"{tool.__name__} no longer contains a real call to _dsn()"


def test_contains_a_real_call_to_is_not_fooled_by_a_decoy_comment():
    # Pins the AST-vs-substring distinction directly: a decoy string that
    # merely mentions "_dsn()" textually must NOT satisfy the helper the
    # test above relies on.
    decoy = (
        "async def fake_tool():\n"
        "    # this comment mentions _dsn() but never calls it\n"
        '    dsn = "_dsn()"  # neither does this string literal\n'
        '    conn = await asyncpg.connect(dsn=os.environ["PITCH_AGENT_RO_DSN"])\n'
    )
    assert "_dsn()" in decoy  # the naive substring check WOULD be fooled
    assert not _contains_a_real_call_to(decoy, "_dsn")  # the AST check is not

    real = "async def real_tool():\n" "    conn = await asyncpg.connect(dsn=_dsn())\n"
    assert _contains_a_real_call_to(real, "_dsn")


# --- Differential test generalizing S1 --------------------------------------


def _asyncpg_resolution(dsn: str):
    """Return (addrs, user, database) exactly as asyncpg's own private
    parser resolves `dsn`, or None if asyncpg itself cannot parse it (in
    which case connecting would fail regardless of what this guard says,
    so there is nothing to compare).

    Imports asyncpg.connect_utils lazily and only inside this test helper
    -- this is deliberately probing a private module, not something
    demo/_dsn_guard.py itself should depend on at runtime (see that
    module's docstring: it mirrors asyncpg's algorithm instead of calling
    into asyncpg's private API directly, so this test is the thing keeping
    that mirror honest against the real, installed asyncpg).
    """
    import asyncpg.connect_utils as cu

    try:
        addrs, params = cu._parse_connect_dsn_and_args(
            dsn=dsn,
            host=None,
            port=None,
            user=None,
            password=None,
            passfile=None,
            database=None,
            ssl=None,
            direct_tls=None,
            server_settings=None,
            target_session_attrs=None,
            krbsrvname=None,
            gsslib=None,
        )
    except Exception:
        return None
    return addrs, params.user, params.database


# A mix of legitimate and adversarial DSNs. Includes the original
# rpartition-vs-partition bypass (S1), comma-before-and-after-the-last-'@'
# variants, wrong port/role/db-name/scheme, and the plain valid DSN.
_DIFFERENTIAL_CANDIDATE_DSNS = [
    DEFAULT_DSN,
    "postgresql://agent_ro:agent_ro_demo_pw_only@localhost:5544/querygate_demo_pitch",
    "postgresql://u@evil.example.com,x@localhost/querygate_demo_pitch",
    "postgresql://u@evil.example.com:5544,x@localhost:5544/querygate_demo_pitch",
    "postgresql://agent_ro:pw@localhost:5544,evil.example.com:5432/querygate_demo_pitch",
    "postgresql://agent_ro:pw@evil.example.com:5544,localhost:5544/querygate_demo_pitch",
    # F3 (2026-08-23 security review): the corpus previously had no
    # IPv6-bracketed, unix-socket-path, or percent-encoded host entries,
    # so _resolve_hostspec_entry's IPv6/unix-socket branches and the
    # urllib_hostname defense-in-depth block were never exercised in a
    # diverging case.
    "postgresql://agent_ro:pw@[::1]:5544/querygate_demo_pitch",  # IPv6 loopback
    "postgresql://agent_ro:pw@[2001:db8::1]:5544/querygate_demo_pitch",  # IPv6 non-loopback
    # percent-encoded "127.0.0.1" -- exercises unquote(); real asyncpg
    # resolves this to the plain 127.0.0.1, but this guard's own
    # urllib_hostname defense-in-depth (urllib does NOT percent-decode a
    # hostname) currently rejects it anyway -- a discovered, non-security,
    # fails-closed quirk (over-restriction, not a bypass), not something
    # this pass changes.
    "postgresql://agent_ro:agent_ro_demo_pw_only@%31%32%37%2E0%2E0%2E1:5544/querygate_demo_pitch",
    # unix-socket-path-via-query-string trick: netloc is fully empty, so
    # asyncpg falls through to `?host=` for a literal unix socket path.
    # This guard never inspects the query string at all (D3 below rejects
    # any DSN carrying one outright), so this is caught long before the
    # empty-netloc host derivation would matter.
    "postgresql:///querygate_demo_pitch?host=%2Fvar%2Frun%2Fpostgresql&user=agent_ro",
    "postgresql://agent_ro:pw@127.0.0.1:5599/querygate_demo_pitch",
    "postgresql://pitch_owner:pitch_owner_demo_pw_only@127.0.0.1:5544/querygate_demo_pitch",
    "postgresql://agent_ro:pw@evil.example.com:5544/querygate_demo_pitch",
    "postgresql://agent_ro:pw@127.0.0.1:5544/production_customers",
    "mysql://agent_ro:pw@127.0.0.1:5544/querygate_demo_pitch",
    "postgresql://agent_ro:pw@127.0.0.1:5544/querygate_demo_pitch",
    # D1 (2026-08-23 security review): a password containing a literal '@'
    # used to make this guard's own rejection message echo the password
    # fragment after the first '@'. Confirmed the real asyncpg still
    # resolves this to a single, otherwise-valid host -- the guard must
    # reject it (more than one '@' in the netloc) without echoing any of
    # it; see test_reason_never_contains_any_part_of_a_password_
    # containing_an_at_sign below for the no-leak assertion specifically.
    "postgresql://agent_ro:A@sup3rSecretProdPw@127.0.0.1:5544/querygate_demo_pitch",
    # D3: a query string with `?options=` reaches Postgres as a startup
    # option regardless of the netloc, on an otherwise fully valid DSN.
    "postgresql://agent_ro:agent_ro_demo_pw_only@127.0.0.1:5544/querygate_demo_pitch"
    "?options=-c%20default_transaction_read_only%3Doff",
    # D4: asyncpg strips exactly ONE leading '/' from the path (then
    # unquotes); `.lstrip("/")` used to strip every leading '/', so this
    # guard would have ALLOWED a DSN naming a database asyncpg spells
    # "/querygate_demo_pitch", not "querygate_demo_pitch".
    "postgresql://agent_ro:agent_ro_demo_pw_only@127.0.0.1:5544//querygate_demo_pitch",
]


@pytest.mark.parametrize("dsn", _DIFFERENTIAL_CANDIDATE_DSNS)
def test_guard_matches_asyncpg_resolution(dsn):
    """Differential test generalizing S1: whenever the guard says a DSN is
    allowed, asyncpg's own parser must agree it resolves to exactly the
    allowed host/port/user/database -- not just "the guard's reason string
    doesn't mention this one bad host". This is the test that would have
    caught both the original multi-host bypass and the S1 regression of
    it, and would catch a *third* such regression without needing to know
    its exact shape in advance.
    """
    env = dict(VALID_ENV)
    env[DSN_ENV_VAR] = dsn
    guard_reason = check_startup_guard(env)

    ground_truth = _asyncpg_resolution(resolve_dsn(env))
    if ground_truth is None:
        # asyncpg itself can't parse/resolve this DSN -- connecting would
        # fail regardless, so the guard allowing or rejecting it doesn't by
        # itself create a bypass. Nothing further to check.
        return

    addrs, user, database = ground_truth
    if guard_reason is None:
        assert len(addrs) == 1, f"guard allowed {dsn!r} but asyncpg resolves {len(addrs)} addresses"
        host, port = addrs[0]
        assert host in ALLOWED_HOSTS, (
            f"guard allowed {dsn!r} but asyncpg would dial host {host!r}, "
            f"not in {ALLOWED_HOSTS!r}"
        )
        assert port == ALLOWED_PORT, (
            f"guard allowed {dsn!r} but asyncpg would dial port {port!r}, " f"not {ALLOWED_PORT!r}"
        )
        assert user == REQUIRED_DB_USER, (
            f"guard allowed {dsn!r} but asyncpg would connect as user "
            f"{user!r}, not {REQUIRED_DB_USER!r}"
        )
        assert database == REQUIRED_DB_NAME, (
            f"guard allowed {dsn!r} but asyncpg would use database "
            f"{database!r}, not {REQUIRED_DB_NAME!r}"
        )


# --- Direct demo/_dsn_guard.py coverage (independent of server.py) --------


def test_validate_dsn_directly_rejects_wrong_required_user_for_seed_role():
    # demo/db/seed.py calls validate_dsn with required_user="pitch_owner"
    # (the opposite of server.py's "agent_ro"). Exercise that directly so
    # the shared function's `required_user` parameter itself is covered,
    # not just server.py's fixed use of it.
    reason = validate_dsn(
        "postgresql://agent_ro:agent_ro_demo_pw_only@127.0.0.1:5544/querygate_demo_pitch",
        required_user="pitch_owner",
        dsn_env_var="QUERYGATE_PITCH_DB_DSN",
    )
    assert reason is not None
    assert "pitch_owner" in reason


def test_validate_dsn_directly_accepts_the_seed_scripts_own_default_dsn():
    reason = validate_dsn(
        "postgresql://pitch_owner:pitch_owner_demo_pw_only@127.0.0.1:5544/querygate_demo_pitch",
        required_user="pitch_owner",
        dsn_env_var="QUERYGATE_PITCH_DB_DSN",
    )
    assert reason is None
