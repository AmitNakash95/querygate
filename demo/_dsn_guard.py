"""Shared DSN validation for demo/ components that connect to the local
pitch-demo Postgres database (demo/SPEC.md's "Hard safety constraints").

This module exists because a security review found the DSN-safety check
duplicated (or, worse, entirely absent) across the demo's several
DB-touching components, and one of the duplicates disagreed with asyncpg
about how to parse a DSN in a way that was an outright bypass (see "S1"
below). SPEC.md declares the "never a non-local database, never a database
other than querygate_demo_pitch" constraint non-negotiable; a constraint
enforced in only one of several places that need it is not actually
enforced. `validate_dsn` below is the ONE place this logic lives; every
demo component that opens a real `asyncpg` connection must call it first.

--- S1: the guard and asyncpg must agree on which '@' splits the netloc ---

A previous version of this check (still, at time of writing, duplicated
inline in demo/baseline_mcp/server.py) used
`parsed.netloc.rpartition("@")[2]` -- the LAST '@' -- to find the host part
of the DSN. asyncpg's own parser
(`asyncpg.connect_utils._parse_connect_dsn_and_args`) instead does:

    if '@' in parsed.netloc:
        dsn_auth, _, dsn_hostspec = parsed.netloc.partition('@')
    else:
        dsn_hostspec = parsed.netloc
        dsn_auth = ''

i.e. the FIRST '@'. `dsn_hostspec` is then split on ',' into one or more
hosts (`_parse_hostlist`). Those two disagree whenever a DSN's userinfo
portion of a *second* host contains its own '@'. Confirmed against the real
installed asyncpg:

    dsn = "postgresql://u@evil.example.com,x@localhost/querygate_demo_pitch"
    # old guard's rpartition("@")[2] -> "localhost" (parsed.hostname agrees)
    #   -> looks like ONE safe host to the old guard.
    # asyncpg's own resolution (asyncpg.connect_utils
    #   ._parse_connect_dsn_and_args) ->
    #   [('evil.example.com', 5432), ('x@localhost', 5432)]
    #   -> asyncpg dials evil.example.com FIRST.

`_dsn_hostspec_entries`/`_resolve_hostspec_entry` below deliberately mirror
asyncpg's own derivation (FIRST '@', then split on ',') instead of
reinventing a check that merely happens to look right. The differential
test in demo/baseline_mcp/test_guard.py cross-checks this module's verdict
against asyncpg's actual private parser on a range of DSNs (not just this
one string) so a *future* regression in this mirroring is caught the same
way this one was.

--- S3: host alone is not enough; port and role matter too ---

Validating only host + database name leaves two further ways for this
constraint to be satisfied on paper while not meaning what SPEC.md intends:

  - A local SSH port-forward (`ssh -L 5599:prod-db:5432 bastion`) makes a
    genuinely remote production database answer on 127.0.0.1 -- the host
    check alone cannot tell that apart from the real local pitch-demo
    Postgres. Requiring the exact port (5544) the pitch-demo Postgres binds
    to raises the bar from "any local port" to "the forward has to
    specifically collide with our port", which is not something an
    accidental/stale env var will do.
  - A DSN naming a role other than the one a given component is supposed to
    connect as can silently defeat what the role itself was FOR. In
    particular the demo's central claim -- that the baseline (gate OFF) and
    QueryGate (gate ON) MCP servers connect with *identical, genuinely
    read-only* database privileges, so the only difference in outcome is
    the policy gate -- is falsified the moment either server is handed a
    DSN for `pitch_owner` (which owns every table) instead of `agent_ro`.
    `required_user` makes each caller assert the specific role it is
    supposed to be using.

Residual limitation, stated plainly: an attacker who deliberately forwards
a *local* port 5544 to a remote database (`ssh -L 5544:prod-db:5432 ...`)
still passes every check here -- no DSN-string-level guard can distinguish
"the real local Postgres on 5544" from "something else answering on
127.0.0.1:5544". The port check defeats the *accidental*/stale-env-var case
this SPEC constraint is chiefly worried about, not a deliberate,
sophisticated local adversary with shell access to the presenter's machine.

`demo/` is deliberately NOT a Python package (no top-level `__init__.py`),
so this module cannot be imported the normal package-relative way. Each
consumer (demo/baseline_mcp/server.py, demo/db/seed.py) inserts the repo's
demo/ directory onto `sys.path` before importing it -- see the comment at
each import site.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple
from urllib.parse import unquote, urlparse

REQUIRED_DB_NAME = "querygate_demo_pitch"
ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1"})
ALLOWED_PORT = 5544
ALLOWED_SCHEMES = frozenset({"postgresql", "postgres"})

# Matches asyncpg.connect_utils._parse_hostlist's own IPv6 hostspec pattern:
# `[addr]` optionally followed by `:port`.
_IPV6_HOSTSPEC_RE = re.compile(r"\[([^\]]+)\](?::([0-9]*))?$")


def _split_dsn_netloc(netloc: str) -> Tuple[str, str]:
    """Split a DSN's netloc into (auth, hostspec), on the FIRST '@' --
    exactly what asyncpg.connect_utils._parse_connect_dsn_and_args does.
    See this module's docstring, "S1", for why the first/last distinction
    is the whole bug.
    """
    if "@" in netloc:
        auth, _, hostspec = netloc.partition("@")
    else:
        auth, hostspec = "", netloc
    return auth, hostspec


def _dsn_hostspec_entries(hostspec: str) -> List[str]:
    """Split a hostspec into its comma-separated entries, the way
    asyncpg.connect_utils._parse_hostlist does."""
    return hostspec.split(",") if hostspec else [""]


def _resolve_hostspec_entry(entry: str) -> Tuple[Optional[str], Optional[int]]:
    """Return (host, port) for one hostspec entry the way
    asyncpg.connect_utils._parse_hostlist resolves it.

    Returns (None, None) for a unix-socket path (leading '/') or a
    malformed IPv6 bracket -- neither can ever be a value in ALLOWED_HOSTS,
    so callers can treat both the same as "does not match". A present but
    non-numeric port yields port=-1 (never equal to ALLOWED_PORT) rather
    than raising, so a malformed port is a normal rejection, not a crash.
    """
    if not entry:
        return None, None
    if entry[0] == "/":
        return None, None  # unix-socket path, not a TCP host
    if entry[0] == "[":
        m = _IPV6_HOSTSPEC_RE.match(entry)
        if not m:
            return None, None
        host = unquote(m.group(1))
        port_str = m.group(2) or ""
    else:
        host, _, port_str = entry.partition(":")
        host = unquote(host)
    if not port_str:
        return host, None
    try:
        return host, int(port_str)
    except ValueError:
        return host, -1  # unparseable port -> never matches ALLOWED_PORT


def validate_dsn(dsn: str, *, required_user: str, dsn_env_var: str) -> Optional[str]:
    """Return None if `dsn` satisfies every demo/SPEC.md hard safety
    constraint for connecting to the local pitch-demo database, else a
    specific, human-readable, redaction-safe reason naming exactly which
    check failed (never echoes the DSN's password).

    `dsn_env_var` names the offending env var in the returned message only.
    `required_user` is the exact database role this caller must connect as
    -- callers differ (the MCP servers' read path uses `agent_ro`; the seed
    script legitimately needs `pitch_owner`'s write/TRUNCATE privileges), so
    it is a parameter, not a module constant. See "S3" in this module's
    docstring for why the role must be checked at all.
    """
    try:
        parsed = urlparse(dsn)
    except ValueError:
        # Deliberately do not interpolate the original exception. A prior
        # version of this comment claimed CPython's urlsplit/urlparse can
        # embed the DSN's password in a ValueError message for some
        # malformed inputs (e.g. an invalid IPv6 host literal); a security
        # review probed this directly against the installed CPython and
        # found every reachable ValueError message here names only the
        # malformed host/bracket fragment, never the userinfo/password. The
        # claim could not be reproduced. This still declines to interpolate
        # the exception, on the general principle that a change to a
        # dependency's error-formatting is not something to rely on staying
        # password-free forever, and because there is no cost to being
        # cautious here -- but the reason is "we don't need to know exactly
        # what broke to refuse to start", not "this specific exception is
        # known to leak the password".
        return f"refusing to proceed: the DSN in {dsn_env_var} could not be parsed as a URL."

    if parsed.scheme not in ALLOWED_SCHEMES:
        return (
            f"refusing to proceed: DSN scheme {parsed.scheme!r} (parsed from "
            f"{dsn_env_var}) is not 'postgresql'/'postgres'."
        )

    # D3 (2026-08-23 security review): asyncpg honours host/port/dbname/
    # database/user/password/passfile/sslmode/options from a DSN's QUERY
    # STRING, each only when the corresponding netloc/path value is absent
    # -- except `options` (and any other unrecognised key), which asyncpg
    # forwards into the session unconditionally as a Postgres startup
    # option regardless of what the netloc says. Confirmed:
    # `?options=-c%20default_transaction_read_only%3Doff` on an otherwise
    # fully valid DSN reaches the server and can flip the very read-only
    # guarantee this demo's "same agent_ro role, same privileges" claim
    # rests on. This module validates only the netloc and path, so any DSN
    # carrying a query string is rejected outright rather than taxonomizing
    # which query keys are safe. Deliberately does not interpolate
    # `parsed.query` -- it can contain `password=`.
    if parsed.query:
        return (
            f"refusing to proceed: the DSN in {dsn_env_var} carries query "
            "parameters. asyncpg honours host/port/dbname/user/password/"
            "options from a DSN's query string (some unconditionally, "
            "regardless of the netloc); this guard validates only the "
            "netloc and path, so a DSN with a query string is rejected "
            "rather than assumed safe."
        )

    auth, hostspec = _split_dsn_netloc(parsed.netloc)

    # D1 (2026-08-23 security review): if the netloc has a SECOND '@' (i.e.
    # `hostspec`, everything after the first '@', still contains one), that
    # second '@' most often means the password itself contains a literal
    # '@' -- asyncpg partitions on the FIRST '@' only, so the remainder of
    # the password becomes part of what asyncpg treats as the host list.
    # Every check below this point may need to echo `hostspec`/the derived
    # `host` into its rejection reason for a legitimate, redaction-safe
    # DSN; once this check has passed, `hostspec` is guaranteed to contain
    # no part of the password (the password lives entirely in `auth`,
    # before the single '@', and `auth` is never echoed anywhere below
    # except its username portion). Reject here, before any further use of
    # `hostspec`, and interpolate nothing DSN-derived -- confirmed against
    # the real installed asyncpg that a DSN shaped exactly like this
    # (`postgresql://u:A@secretpw@127.0.0.1:5544/querygate_demo_pitch`)
    # previously made the "not localhost/127.0.0.1" rejection below echo
    # `secretpw` verbatim to stderr.
    if "@" in hostspec:
        return (
            f"refusing to proceed: the DSN in {dsn_env_var} has more than "
            "one '@' in its netloc. asyncpg splits userinfo from the host "
            "list on the FIRST '@' only, so anything after it -- including "
            "a literal '@' inside the password -- becomes part of the host "
            "list; rejected without quoting any part of the DSN, since the "
            "text after the first '@' can be part of the password."
        )

    entries = _dsn_hostspec_entries(hostspec)
    if len(entries) != 1:
        return (
            f"refusing to proceed: DSN in {dsn_env_var} declares more than "
            f"one host ({hostspec!r}) -- asyncpg splits a DSN's netloc into "
            "userinfo/host-list on the FIRST '@' (not the last) and then "
            "splits the host list on ',', so a comma placed before the "
            "DSN's final '@' still yields more than one host to asyncpg "
            "even when it looks like a single host with an embedded '@' to "
            "a naive parser. A multi-host DSN can have asyncpg dial a "
            "non-local host even when another entry is localhost/127.0.0.1 "
            "(demo/SPEC.md hard safety constraint: this must never point "
            "at a non-local database)."
        )

    host, port = _resolve_hostspec_entry(entries[0])
    host_norm = host.lower() if host is not None else None
    if host_norm not in ALLOWED_HOSTS:
        return (
            f"refusing to proceed: DSN host {host!r} (parsed from "
            f"{dsn_env_var}) is not localhost/127.0.0.1 (demo/SPEC.md hard "
            "safety constraint: this must never point at a non-local "
            "database)."
        )

    # Defense in depth: urllib's own .hostname (a completely independent
    # code path from _resolve_hostspec_entry above) must also land in
    # ALLOWED_HOSTS. This does not re-derive the asyncpg-faithful host --
    # only urllib's -- so it cannot mask a bug in the asyncpg-mirroring
    # logic above; it exists to catch a bug in *this* module's own parsing,
    # not to replace the asyncpg-faithful check with a weaker one.
    try:
        urllib_hostname = parsed.hostname
    except ValueError:
        urllib_hostname = None
    if urllib_hostname not in ALLOWED_HOSTS:
        return (
            f"refusing to proceed: DSN host {urllib_hostname!r} (parsed "
            f"from {dsn_env_var} via urllib) is not localhost/127.0.0.1 "
            "(demo/SPEC.md hard safety constraint: this must never point "
            "at a non-local database)."
        )

    if port is None:
        return (
            f"refusing to proceed: DSN in {dsn_env_var} does not specify an "
            f"explicit port on its host -- must be exactly {ALLOWED_PORT} "
            "(the pitch-demo database's port). A DSN with no explicit port "
            "falls back to Postgres's ambient default/PGPORT rather than a "
            "port this check can verify, so it is rejected rather than "
            "assumed safe."
        )
    if port != ALLOWED_PORT:
        return (
            f"refusing to proceed: DSN port {port!r} (parsed from "
            f"{dsn_env_var}) is not {ALLOWED_PORT} (demo/SPEC.md hard "
            "safety constraint: this must connect only to the pitch-demo "
            f"database on port {ALLOWED_PORT}; e.g. a local SSH forward "
            f"such as `ssh -L {ALLOWED_PORT}:prod-db:5432 ...` could "
            "otherwise make a genuinely remote database satisfy the host "
            "check above)."
        )

    # D4 (2026-08-23 security review): mirror asyncpg's own derivation
    # exactly -- it strips exactly ONE leading '/' from the path, then
    # unquotes (asyncpg.connect_utils: `if dsn_database.startswith('/'):
    # dsn_database = dsn_database[1:]`, then
    # `urllib.parse.unquote(dsn_database)`). `.lstrip("/")` strips EVERY
    # leading '/', which disagreed with asyncpg for a DSN with two leading
    # slashes (`.../.../ /querygate_demo_pitch` after urlparse's netloc):
    # this guard would ALLOW it as `querygate_demo_pitch`, while asyncpg
    # would request a database literally named `/querygate_demo_pitch`
    # (which does not exist, so it fails closed today -- but the guard's
    # own guarantee that it validates the database asyncpg will actually
    # use was false for that shape).
    raw_path = parsed.path or ""
    db_name = unquote(raw_path[1:] if raw_path.startswith("/") else raw_path)
    if db_name != REQUIRED_DB_NAME:
        return (
            f"refusing to proceed: DSN database name {db_name!r} (parsed "
            f"from {dsn_env_var}) is not exactly {REQUIRED_DB_NAME!r} "
            "(demo/SPEC.md hard safety constraint: this must never point "
            "at anything but the throwaway pitch database)."
        )

    dsn_user = unquote(auth.partition(":")[0]) if auth else ""
    if dsn_user != required_user:
        return (
            f"refusing to proceed: DSN user {dsn_user!r} (parsed from "
            f"{dsn_env_var}) is not {required_user!r} -- this component "
            f"must connect as {required_user!r} specifically (a DSN for a "
            "different role, e.g. the table-owning pitch_owner instead of "
            "the genuinely read-only agent_ro, would silently falsify what "
            "the demo claims about database privileges)."
        )

    return None
