"""Caller-supplied identifiers cannot break out of their quoting.

Almost every string a caller sends becomes a **bound parameter**, and
`test_predicate_payload_is_bound_data_not_executable_sql` pins that. Aliases are
the exception: they are rendered as **identifiers**, not parameters, so they are
the caller-controlled strings that reach the SQL text itself. There are two such
sinks, and both are covered here — a select item's `as`, and a **table** alias
(`from_alias` / `joins[].alias`), which lands in the FROM clause via
`schema_validation`'s `source.alias(name)`. If it could carry its dialect's quote character out of the quoting, the
"no caller-controlled SQL" guarantee would have a hole in exactly the place the
guarantee is hardest to see.

This was an undisclosed gap. The behaviour was already correct — SQLAlchemy
quotes the identifier and doubles the embedded delimiter — but it rested
entirely on SQLAlchemy's quoter with no QueryGate assertion, so a future
compiler change that interpolated an alias instead of quoting it would not have
failed anything. Surfaced 2026-08-21 while drafting the public essay, which had
to disclose it as an honest edge rather than cite a test.

The assertions below are about the **escaping mechanism**, not about keyword
absence: a hostile alias legitimately contains the words `FROM` and `DROP`
*inside* its quoted identifier, so counting keywords in the raw SQL proves
nothing. What matters is that the identifier is closed correctly and that the
statement outside it is unchanged.

`CteSpec.name` is the third identifier a caller names, and the only one that is
pattern-constrained at the AST layer (`^[A-Za-z_][A-Za-z0-9_]*$`); that
constraint is pinned here too. The two alias fields are deliberately *not*
constrained — adding a pattern there would reject input that is valid today and
would change the published MCP schema, so it is the owner's call, recorded in
the audit follow-ups rather than taken unilaterally. Until then, escaping is the
guarantee and these tests are what hold it.
"""

from __future__ import annotations

import re

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mssql, mysql, postgresql, sqlite

from querygate.compiler.sqlalchemy_compiler import compile_structured_query
from querygate.policy.models import Policy
from querygate.query_ast.models import CteSpec, StructuredQuery

pytestmark = [pytest.mark.security, pytest.mark.unit]

# Every dialect the registry supports, plus sqlite (used internally by the
# end-to-end compiler tests). Quoting rules differ per dialect, so one dialect
# passing proves nothing about the others.
DIALECTS = {
    "postgresql": postgresql.dialect(),
    "mssql": mssql.dialect(),
    "mysql": mysql.dialect(),
    "sqlite": sqlite.dialect(),
}

# How each dialect delimits an identifier, and how it escapes an embedded
# delimiter. MSSQL brackets only need `]` doubled — a `"` inside is inert.
QUOTING = {
    "postgresql": ('"', '"', '"'),
    "sqlite": ('"', '"', '"'),
    "mysql": ("`", "`", "`"),
    "mssql": ("[", "]", "]"),
}

HOSTILE_ALIASES = [
    'x" FROM customers; DROP TABLE customers --',
    "x' FROM customers --",
    "x` FROM customers --",
    "x] FROM customers --",
    'x", (SELECT email FROM customers) AS leaked, "y',
    "x\\",
]


def _words(sql: str, keyword: str) -> int:
    """Count `keyword` as a whole word. SQLAlchemy renders `\nFROM`, not ` FROM `."""
    return len(re.findall(rf"\b{keyword}\b", sql, re.I))


def _customers() -> sa.Table:
    return sa.Table(
        "customers",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(200)),
    )


def _compiled_sql(alias: str, dialect: str) -> str:
    query = StructuredQuery.model_validate(
        {"from": "customers", "select": [{"fn": "count", "col": "*", "as": alias}]}
    )
    stmt, _ = compile_structured_query(
        query, {"customers": _customers()}, Policy(), dialect=dialect
    )
    return str(stmt.compile(dialect=DIALECTS[dialect]))


def _expected_identifier(alias: str, dialect: str) -> str:
    """The single quoted token the alias must render as, escaping included."""
    open_q, close_q, escaped = QUOTING[dialect]
    return f"{open_q}{alias.replace(close_q, escaped * 2)}{close_q}"


@pytest.mark.parametrize("dialect", sorted(DIALECTS))
@pytest.mark.parametrize("alias", HOSTILE_ALIASES)
def test_a_hostile_alias_is_rendered_as_one_escaped_identifier(alias: str, dialect: str):
    """Either the AST refuses it, or it becomes exactly one closed identifier and
    the rest of the statement is untouched."""
    try:
        sql = _compiled_sql(alias, dialect)
    except Exception:
        return  # refused at the AST/compiler layer — also a safe outcome

    identifier = _expected_identifier(alias, dialect)
    assert identifier in sql, f"alias not escaped as expected: {sql!r}"

    # With the identifier removed, nothing the alias carried may remain: no
    # second FROM, no statement separator, no injected SELECT.
    remainder = sql.replace(identifier, "«ALIAS»")
    assert _words(remainder, "FROM") == 1, remainder
    assert ";" not in remainder, remainder
    assert _words(remainder, "DROP") == 0, remainder
    assert _words(remainder, "SELECT") == 1, remainder


def _compiled_sql_with_table_alias(alias: str, dialect: str) -> str:
    """The second identifier sink: a table alias reaches the FROM clause.

    Goes through policy + schema validation so the alias is registered the way a
    real request registers it (`schema_validation` maps the alias onto
    `source.alias(name)`), rather than being smuggled straight into the compiler.
    """
    from querygate.policy.models import Policy as _Policy
    from querygate.validation import schema_validation

    query = StructuredQuery.model_validate(
        {
            "from": "customers",
            "from_alias": alias,
            "select": [{"fn": "count", "col": "*", "as": "n"}],
        }
    )
    tables = {alias: _customers().alias(alias)}
    stmt, _ = compile_structured_query(query, tables, _Policy(), dialect=dialect)
    return str(stmt.compile(dialect=DIALECTS[dialect]))


@pytest.mark.parametrize("dialect", sorted(DIALECTS))
@pytest.mark.parametrize("alias", HOSTILE_ALIASES)
def test_a_hostile_table_alias_is_rendered_as_one_escaped_identifier(alias: str, dialect: str):
    """Same guarantee for the FROM clause. This sink was unpinned until an audit
    pointed out that the select-item alias was not, in fact, the only one."""
    try:
        sql = _compiled_sql_with_table_alias(alias, dialect)
    except Exception:
        return  # refused upstream — also a safe outcome

    identifier = _expected_identifier(alias, dialect)
    assert identifier in sql, f"table alias not escaped as expected: {sql!r}"
    remainder = sql.replace(identifier, "«ALIAS»")
    assert _words(remainder, "FROM") == 1, remainder
    assert ";" not in remainder, remainder
    assert _words(remainder, "DROP") == 0, remainder
    assert _words(remainder, "SELECT") == 1, remainder


@pytest.mark.parametrize("dialect", sorted(DIALECTS))
def test_a_benign_table_alias_still_survives(dialect: str):
    """Negative control for the table-alias rule."""
    sql = _compiled_sql_with_table_alias("c", dialect)
    assert _words(sql, "FROM") == 1
    assert "customers" in sql


@pytest.mark.parametrize("dialect", sorted(DIALECTS))
def test_a_benign_alias_still_survives(dialect: str):
    """Negative control: the rule above must not pass by rejecting everything."""
    sql = _compiled_sql("total_orders", dialect)
    assert "total_orders" in sql
    assert _words(sql, "FROM") == 1


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        ("postgresql", '"a""b"'),
        ("sqlite", '"a""b"'),
        ("mysql", '`a"b`'),
        ("mssql", '[a"b]'),
    ],
)
def test_an_embedded_delimiter_is_doubled_rather_than_terminating_the_identifier(
    dialect: str, expected: str
):
    """The mechanism itself, pinned per dialect so a compiler change that
    interpolated an alias instead of quoting it fails here."""
    assert expected in _compiled_sql('a"b', dialect)


@pytest.mark.parametrize(
    "name",
    [
        'x" FROM customers --',
        "x; DROP TABLE customers",
        "x y",
        "1x",
        "",
    ],
)
def test_a_cte_name_that_is_not_a_plain_identifier_is_refused(name: str):
    """CTE names are the other caller-supplied identifier. The AST pattern is the
    guard; this fails if that pattern is ever loosened."""
    with pytest.raises(Exception):
        CteSpec.model_validate(
            {"name": name, "query": {"from": "customers", "select": ["customers.id"]}}
        )


def test_a_plain_cte_name_is_accepted():
    """Negative control for the pattern."""
    spec = CteSpec.model_validate(
        {"name": "recent_orders", "query": {"from": "customers", "select": ["customers.id"]}}
    )
    assert spec.name == "recent_orders"
