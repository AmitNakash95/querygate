"""Unit tests for connections/models.py — `ConnectionProfile`'s validators.

Covers TODO.md item 158: `dialect` must agree with the backend actually
named in `connection_string`'s URL scheme (e.g. a `dialect: postgresql`
profile pointed at a `mysql+asyncmy://...` string is rejected), across all
four supported dialects, the partially-templated-credential case, that a
wholly-templated or unparseable connection_string is left alone rather than
rejected (there's no backend to compare against — and this is the real
shape `examples/connections.example.yaml`/`help/service.py`'s redacted
config view use), and — critically — that a rejection never echoes the
connection string (which carries the credential) back in the raised error,
since pydantic's default `ValueError` handling embeds whatever the
validator is scoped to into `input_value`.
"""

from __future__ import annotations

import pydantic
import pytest

from querygate.connections.models import ConnectionProfile, DatabaseDialect

_VALID_PAIRS = [
    (DatabaseDialect.POSTGRESQL, "postgresql+asyncpg://user:pass@host:5432/db"),
    (DatabaseDialect.MSSQL, "mssql+aioodbc://user:pass@host:1433/db"),
    (DatabaseDialect.MYSQL, "mysql+asyncmy://user:pass@host:3306/db"),
    (DatabaseDialect.MYSQL, "mysql+aiomysql://user:pass@host:3306/db"),
    (DatabaseDialect.SNOWFLAKE, "snowflake://user:pass@account/db/schema?warehouse=wh"),
]


@pytest.mark.parametrize("dialect, connection_string", _VALID_PAIRS)
def test_matching_dialect_and_connection_string_backend_is_accepted(dialect, connection_string):
    profile = ConnectionProfile(id="demo", dialect=dialect, connection_string=connection_string)
    assert profile.dialect == dialect


_MISMATCHED_PAIRS = [
    (DatabaseDialect.POSTGRESQL, "mysql+asyncmy://user:pass@host:3306/db", "mysql"),
    (DatabaseDialect.MSSQL, "postgresql+asyncpg://user:pass@host:5432/db", "postgresql"),
    (DatabaseDialect.MYSQL, "mssql+aioodbc://user:pass@host:1433/db", "mssql"),
    (
        DatabaseDialect.POSTGRESQL,
        "snowflake://user:pass@account/db/schema?warehouse=wh",
        "snowflake",
    ),
    (DatabaseDialect.SNOWFLAKE, "postgresql+asyncpg://user:pass@host:5432/db", "postgresql"),
]


@pytest.mark.parametrize("declared_dialect, connection_string, actual_backend", _MISMATCHED_PAIRS)
def test_mismatched_dialect_and_connection_string_backend_is_rejected(
    declared_dialect, connection_string, actual_backend
):
    with pytest.raises(pydantic.ValidationError) as exc_info:
        ConnectionProfile(id="demo", dialect=declared_dialect, connection_string=connection_string)
    message = str(exc_info.value)
    assert str(declared_dialect) in message
    assert actual_backend in message
    # The message text we author (`errors()[0]["msg"]`) should read as a
    # plain dialect name (e.g. 'postgresql'), not pydantic's default
    # StrEnum repr (e.g. <DatabaseDialect.POSTGRESQL: 'postgresql'>), which
    # is confusing for an admin reading a config rejection. Checked against
    # just our own message text, not `str(exc_info.value)` as a whole —
    # pydantic itself always appends an `input_value=<DatabaseDialect...>`
    # debugging tail to the full rendered error regardless of what our
    # message says, so asserting against the whole string would be
    # asserting something outside this validator's control.
    assert "DatabaseDialect." not in exc_info.value.errors()[0]["msg"]


def test_mismatch_error_never_echoes_the_connection_string():
    """The connection string carries the real credential — a rejection must
    never surface it, in the message text or in pydantic's own `input_value`/
    `.errors()` machinery (a whole-model validator raising `ValueError` would
    otherwise dump the entire input dict, credential included — see the
    validator's own docstring in connections/models.py for the mechanism).
    `api/routes.py`'s `reload_config_endpoint` stringifies any exception
    straight into an HTTP 400 `detail`, so this isn't a hypothetical.

    Asserts the POSITIVE shape too (not just "the marker isn't there"),
    since a substring-only check would still pass if a future regression
    leaked a *different* piece of the connection string (e.g. the host or
    database name) instead of the password token: pydantic's `input` for a
    `field_validator("dialect")` rejection must be exactly the declared
    dialect string and nothing else, and the whole `connection_string`
    value must be absent from the error's full repr, not just the marker.
    """
    secret_marker = "s3cr3t_credential_marker"
    connection_string = f"mysql+asyncmy://user:{secret_marker}@host:3306/db"
    with pytest.raises(pydantic.ValidationError) as exc_info:
        ConnectionProfile(
            id="demo", dialect=DatabaseDialect.POSTGRESQL, connection_string=connection_string
        )
    exc = exc_info.value
    assert secret_marker not in str(exc)
    errors = exc.errors()
    assert len(errors) == 1
    error = errors[0]
    assert error["loc"] == ("dialect",)
    assert error["input"] == "postgresql"
    assert connection_string not in repr(errors)
    assert secret_marker not in repr(errors)


def test_unsupported_backend_error_also_never_echoes_the_connection_string():
    """Same credential-safety contract as the mismatch case above, but for
    the OTHER raise site in the validator (an SQLAlchemy-known but
    QueryGate-unsupported backend) — both `raise ValueError` sites in
    `_dialect_matches_connection_string` must uphold it independently.
    """
    secret_marker = "s3cr3t_credential_marker"
    connection_string = f"sqlite:///./{secret_marker}.db"
    with pytest.raises(pydantic.ValidationError) as exc_info:
        ConnectionProfile(
            id="demo", dialect=DatabaseDialect.POSTGRESQL, connection_string=connection_string
        )
    errors = exc_info.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("dialect",)
    assert errors[0]["input"] == "postgresql"
    assert secret_marker not in repr(errors)


def test_templated_credential_still_resolves_the_scheme():
    """`connection_string` may still contain an unresolved `${ENV_VAR}`
    placeholder in its credentials/host/db portion if this validator ever
    runs before `secrets/resolvers.py` interpolation — the scheme itself
    (before `://`) is never templated in this codebase's YAML shape, so
    validation must still succeed.
    """
    profile = ConnectionProfile(
        id="demo",
        dialect=DatabaseDialect.POSTGRESQL,
        connection_string="postgresql+asyncpg://${DB_USER}:${DB_PASS}@${DB_HOST}:5432/${DB_NAME}",
    )
    assert profile.dialect == DatabaseDialect.POSTGRESQL


def test_templated_credential_with_mismatched_dialect_is_still_rejected():
    with pytest.raises(pydantic.ValidationError) as exc_info:
        ConnectionProfile(
            id="demo",
            dialect=DatabaseDialect.MSSQL,
            connection_string="postgresql+asyncpg://${DB_USER}:${DB_PASS}@${DB_HOST}:5432/${DB_NAME}",
        )
    message = str(exc_info.value)
    assert "mssql" in message
    assert "postgresql" in message


def test_templated_port_still_resolves_the_scheme():
    """A templated *port* (`...@${DB_HOST}:${DB_PORT}/...`) is the same
    "still templated" shape as a templated user/pass/host, but SQLAlchemy's
    `make_url` fails to parse it differently underneath: it raises a plain
    `ValueError` (`invalid literal for int() with base 10: '${DB_PORT}'`)
    while trying to coerce the port to an int, not `sa.exc.ArgumentError`
    like a wholly-unparseable string. Confirmed directly against `make_url`
    — a first-draft `except sa.exc.ArgumentError:` alone missed this and
    would incorrectly reject a genuinely-matching dialect/connection_string
    pair. Both exception types must be treated the same way: skip, don't
    reject.
    """
    profile = ConnectionProfile(
        id="demo",
        dialect=DatabaseDialect.POSTGRESQL,
        connection_string="postgresql+asyncpg://${DB_USER}:${DB_PASS}@${DB_HOST}:${DB_PORT}/${DB_NAME}",
    )
    assert profile.dialect == DatabaseDialect.POSTGRESQL


def test_wholly_templated_connection_string_is_not_rejected():
    """A `connection_string` that is entirely a single `${ENV_VAR}`
    reference (no literal scheme at all, e.g. `${QUERYGATE_DEMO_DB_URL}`) is
    the actual default shape in `examples/connections.example.yaml`, and
    `help/service.py`'s `_redacted_connection` deliberately constructs a
    `ConnectionProfile` straight from a stored config version's raw,
    never-interpolated YAML for an admin summary — it never resolves the
    real secret. `make_url` can't parse a backend out of that shape at all,
    so this validator can't assert a mismatch and must not reject it: the
    one path that actually opens a connection always interpolates first, so
    that real check still applies to the resolved string when it matters.
    """
    profile = ConnectionProfile(
        id="demo",
        dialect=DatabaseDialect.POSTGRESQL,
        connection_string="${QUERYGATE_DEMO_DB_URL}",
    )
    assert profile.dialect == DatabaseDialect.POSTGRESQL


def test_garbage_non_url_connection_string_is_not_rejected_either():
    """Same reasoning as the wholly-templated case: an unparseable string
    gives this validator no basis to compare backends, so it's left for
    `create_async_engine` to reject at connect time instead (unchanged from
    pre-158 behavior for this specific shape)."""
    profile = ConnectionProfile(
        id="demo", dialect=DatabaseDialect.POSTGRESQL, connection_string="not a url"
    )
    assert profile.dialect == DatabaseDialect.POSTGRESQL


def test_unsupported_backend_is_rejected_even_though_scheme_parses():
    with pytest.raises(pydantic.ValidationError) as exc_info:
        ConnectionProfile(
            id="demo", dialect=DatabaseDialect.POSTGRESQL, connection_string="sqlite:///:memory:"
        )
    message = str(exc_info.value)
    assert "sqlite" in message


def test_backend_name_map_covers_every_declared_dialect():
    """`_BACKEND_NAME_TO_DIALECT` is hand-maintained (deliberately, so an
    unsupported-but-SQLAlchemy-known backend gets this module's own clear
    rejection) rather than derived from `DatabaseDialect` automatically, so
    nothing else forces it to stay a superset of the enum. If a fifth
    dialect is ever added to `DatabaseDialect` without a matching entry
    here, every genuinely-matching profile for that dialect would be
    silently rejected as "unsupported" — this pins the invariant so that
    failure happens loudly (a module-level `assert` on import) instead.
    """
    from querygate.connections.models import _BACKEND_NAME_TO_DIALECT

    assert set(DatabaseDialect) <= set(_BACKEND_NAME_TO_DIALECT.values())
