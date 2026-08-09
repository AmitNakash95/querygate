"""Unit tests for the file-driven connection registry."""

from __future__ import annotations

import pydantic
import pytest

from querygate.connections.registry import ConnectionRegistry
from querygate.secrets.resolvers import EnvSecretResolver, SecretResolverRegistry


def test_env_var_interpolation(monkeypatch):
    monkeypatch.setenv("TEST_DB_URL", "postgresql+asyncpg://user:pass@host/db")
    registry = ConnectionRegistry.from_entries(
        [{"id": "demo", "dialect": "postgresql", "connection_string": "${TEST_DB_URL}"}]
    )
    assert registry.get("demo").connection_string == "postgresql+asyncpg://user:pass@host/db"


def test_dotenv_interpolation_matches_quickstart(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DOTENV_DB_URL", raising=False)
    (tmp_path / ".env").write_text(
        "DOTENV_DB_URL=postgresql+asyncpg://dotenv-user:dotenv-pass@host/db\n"
    )
    connections_file = tmp_path / "connections.yaml"
    connections_file.write_text("""
connections:
  - id: demo
    dialect: postgresql
    connection_string: ${DOTENV_DB_URL}
""")

    registry = ConnectionRegistry.from_file(str(connections_file))

    assert (
        registry.get("demo").connection_string
        == "postgresql+asyncpg://dotenv-user:dotenv-pass@host/db"
    )


def test_process_environment_overrides_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("PRECEDENCE_DB_URL=postgresql+asyncpg://dotenv/db\n")
    monkeypatch.setenv("PRECEDENCE_DB_URL", "postgresql+asyncpg://process/db")

    registry = ConnectionRegistry.from_entries(
        [
            {
                "id": "demo",
                "dialect": "postgresql",
                "connection_string": "${PRECEDENCE_DB_URL}",
            }
        ]
    )

    assert registry.get("demo").connection_string == "postgresql+asyncpg://process/db"


def test_missing_env_var_raises(monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    with pytest.raises(ValueError, match="MISSING_VAR"):
        ConnectionRegistry.from_entries(
            [{"id": "demo", "dialect": "postgresql", "connection_string": "${MISSING_VAR}"}]
        )


def test_duplicate_connection_id_rejected(monkeypatch):
    monkeypatch.setenv("A", "postgresql+asyncpg://a")
    monkeypatch.setenv("B", "postgresql+asyncpg://b")
    with pytest.raises(ValueError, match="Duplicate"):
        ConnectionRegistry.from_entries(
            [
                {"id": "demo", "dialect": "postgresql", "connection_string": "${A}"},
                {"id": "demo", "dialect": "postgresql", "connection_string": "${B}"},
            ]
        )


def test_unknown_connection_raises_keyerror(monkeypatch):
    monkeypatch.setenv("A", "postgresql+asyncpg://a")
    registry = ConnectionRegistry.from_entries(
        [{"id": "demo", "dialect": "postgresql", "connection_string": "${A}"}]
    )
    with pytest.raises(KeyError):
        registry.get("nonexistent")


def test_list_public_excludes_connection_string(monkeypatch):
    monkeypatch.setenv("A", "postgresql+asyncpg://user:supersecret@host/db")
    registry = ConnectionRegistry.from_entries(
        [
            {
                "id": "demo",
                "dialect": "postgresql",
                "connection_string": "${A}",
                "description": "demo db",
            }
        ]
    )
    public = registry.list_public()
    assert len(public) == 1
    assert public[0].id == "demo"
    assert not hasattr(public[0], "connection_string")
    dumped = public[0].model_dump()
    assert "connection_string" not in dumped
    assert "supersecret" not in str(dumped)


class _FakeResolver:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def resolve(self, reference: str) -> str:
        return self._values[reference]


def test_connection_string_resolved_through_custom_resolver_registry():
    registry = SecretResolverRegistry(
        {
            "env": EnvSecretResolver({}),
            "vault": _FakeResolver({"demo#url": "postgresql+asyncpg://vault-resolved/db"}),
        }
    )
    connections = ConnectionRegistry.from_entries(
        [{"id": "demo", "dialect": "postgresql", "connection_string": "${vault:demo#url}"}],
        resolver_registry=registry,
    )
    assert connections.get("demo").connection_string == "postgresql+asyncpg://vault-resolved/db"


def test_from_entries_rejects_a_dialect_mismatch_only_visible_after_interpolation(monkeypatch):
    """TODO.md item 158: the dialect-match validator on `ConnectionProfile`
    only matters in production if it actually runs on the RESOLVED
    connection string, on the one path (`ConnectionRegistry.from_entries`)
    that ever builds a `ConnectionProfile` bound for a real engine. This
    pins that claim at the registry level, not just against the model in
    isolation: `${MYSQL_URL}` resolves to a mysql backend, but the entry
    declares `dialect: postgresql` — this can only be caught by validating
    AFTER `registry.interpolate()` runs, exactly the order `from_entries`
    uses. If a future change ever built profiles via `model_construct()`
    (skipping validation) or reordered interpolate-then-validate, this is
    the test that would catch it.
    """
    monkeypatch.setenv("MYSQL_URL", "mysql+asyncmy://user:pass@host:3306/db")
    with pytest.raises(pydantic.ValidationError, match="mysql"):
        ConnectionRegistry.from_entries(
            [{"id": "demo", "dialect": "postgresql", "connection_string": "${MYSQL_URL}"}]
        )


def test_unregistered_scheme_is_reported_clearly():
    registry = SecretResolverRegistry({"env": EnvSecretResolver({})})
    with pytest.raises(ValueError, match="No secret resolver registered for scheme 'vault'"):
        ConnectionRegistry.from_entries(
            [{"id": "demo", "dialect": "postgresql", "connection_string": "${vault:demo#url}"}],
            resolver_registry=registry,
        )


class TestJoinGroupHostValidation:
    """TODO.md item 170 (maintainer-approved 2026-08-09: validate, don't just
    document). `join_group` was previously gated only by string equality —
    nothing checked the two connections it names are actually the same
    physical server instance, which is what a cross-connection join's
    reflection through the PRIMARY connection's own engine assumes."""

    def test_a_join_group_spanning_different_hosts_is_rejected(self, monkeypatch):
        monkeypatch.setenv("PRIMARY_URL", "postgresql+asyncpg://user:pass@host-a/primary_db")
        monkeypatch.setenv("OTHER_URL", "postgresql+asyncpg://user:pass@host-b/other_db")
        with pytest.raises(ValueError, match="spans different hosts"):
            ConnectionRegistry.from_entries(
                [
                    {
                        "id": "primary",
                        "dialect": "postgresql",
                        "connection_string": "${PRIMARY_URL}",
                        "join_group": "shared",
                    },
                    {
                        "id": "other",
                        "dialect": "postgresql",
                        "connection_string": "${OTHER_URL}",
                        "join_group": "shared",
                    },
                ]
            )

    def test_the_error_names_the_connection_ids_and_hosts_but_never_the_connection_string(
        self, monkeypatch
    ):
        """The rejection message must be safe to surface verbatim in a CLI/
        REST error (`cli.py`'s `_describe_load_error` passes an unrecognized
        `ValueError` straight through as `f"{file}: {exc}"`, no redaction) —
        so it must never embed the raw connection string, which can carry a
        literal (non-`${...}`) credential in a config-governance draft."""
        monkeypatch.setenv(
            "PRIMARY_URL", "postgresql+asyncpg://sensitive_user:sensitive_pass@host-a/primary_db"
        )
        monkeypatch.setenv(
            "OTHER_URL", "postgresql+asyncpg://sensitive_user:sensitive_pass@host-b/other_db"
        )
        with pytest.raises(ValueError) as exc_info:
            ConnectionRegistry.from_entries(
                [
                    {
                        "id": "primary",
                        "dialect": "postgresql",
                        "connection_string": "${PRIMARY_URL}",
                        "join_group": "shared",
                    },
                    {
                        "id": "other",
                        "dialect": "postgresql",
                        "connection_string": "${OTHER_URL}",
                        "join_group": "shared",
                    },
                ]
            )
        message = str(exc_info.value)
        assert "'primary' -> host-a:" in message
        assert "'other' -> host-b:" in message
        assert "sensitive_user" not in message
        assert "sensitive_pass" not in message

    def test_an_unencoded_at_sign_in_the_password_never_leaks_into_the_error(self, monkeypatch):
        """security-invariant-reviewer, 2026-08-09 (QG170-3): SQLAlchemy's
        URL parser splits userinfo from the rest at the FIRST '@', so an
        un-encoded '@' in a password (routine — operators frequently don't
        percent-encode) bleeds its tail into `url.host` itself. Verified
        directly: `postgresql+asyncpg://svc:P@ssw0rd@host/db` parses with
        `host == 'ssw0rd@host'`. Without stripping to the LAST '@',
        `connection_host_port` would return that password fragment as the
        'host', and it would then appear in this rejection message."""
        monkeypatch.setenv("PRIMARY_URL", "postgresql+asyncpg://svc:P@ssw0rd2026@host-a/primary_db")
        monkeypatch.setenv("OTHER_URL", "postgresql+asyncpg://svc:P@ssw0rd2026@host-b/other_db")
        with pytest.raises(ValueError) as exc_info:
            ConnectionRegistry.from_entries(
                [
                    {
                        "id": "primary",
                        "dialect": "postgresql",
                        "connection_string": "${PRIMARY_URL}",
                        "join_group": "shared",
                    },
                    {
                        "id": "other",
                        "dialect": "postgresql",
                        "connection_string": "${OTHER_URL}",
                        "join_group": "shared",
                    },
                ]
            )
        message = str(exc_info.value)
        assert "ssw0rd2026" not in message
        assert "'primary' -> host-a:" in message
        assert "'other' -> host-b:" in message

    def test_same_host_different_passwords_with_at_signs_load_without_error(self, monkeypatch):
        """Companion to the above: two connections genuinely on the same
        host, whose (unencoded) passwords happen to differ, must not be
        falsely rejected because of the '@'-in-password parsing quirk."""
        monkeypatch.setenv("PRIMARY_URL", "postgresql+asyncpg://svc:P@ssOne@same-host/primary_db")
        monkeypatch.setenv("OTHER_URL", "postgresql+asyncpg://svc:P@ssTwo@same-host/other_db")
        registry = ConnectionRegistry.from_entries(
            [
                {
                    "id": "primary",
                    "dialect": "postgresql",
                    "connection_string": "${PRIMARY_URL}",
                    "join_group": "shared",
                },
                {
                    "id": "other",
                    "dialect": "postgresql",
                    "connection_string": "${OTHER_URL}",
                    "join_group": "shared",
                },
            ]
        )
        assert registry.all_ids() == ["other", "primary"]

    def test_a_join_group_on_one_host_with_different_ports_is_rejected(self, monkeypatch):
        monkeypatch.setenv("PRIMARY_URL", "postgresql+asyncpg://user:pass@host:5432/primary_db")
        monkeypatch.setenv("OTHER_URL", "postgresql+asyncpg://user:pass@host:5433/other_db")
        with pytest.raises(ValueError, match="spans different hosts"):
            ConnectionRegistry.from_entries(
                [
                    {
                        "id": "primary",
                        "dialect": "postgresql",
                        "connection_string": "${PRIMARY_URL}",
                        "join_group": "shared",
                    },
                    {
                        "id": "other",
                        "dialect": "postgresql",
                        "connection_string": "${OTHER_URL}",
                        "join_group": "shared",
                    },
                ]
            )

    def test_a_join_group_on_the_same_host_with_one_port_omitted_is_allowed(self, monkeypatch):
        """security-invariant-reviewer, 2026-08-09 (QG170-4 /
        architecture-boundary-reviewer's 170-B, independently found): two
        databases on ONE Postgres server — the canonical join_group use
        case — written the two ordinary ways (one with an explicit
        `:5432`, one relying on the dialect default) must not be rejected
        as if they were different instances. Host and port are compared as
        separate sets, so an omitted port never collides with an explicit
        one for the SAME host."""
        monkeypatch.setenv("PRIMARY_URL", "postgresql+asyncpg://user:pass@host/primary_db")
        monkeypatch.setenv("OTHER_URL", "postgresql+asyncpg://user:pass@host:5432/other_db")
        registry = ConnectionRegistry.from_entries(
            [
                {
                    "id": "primary",
                    "dialect": "postgresql",
                    "connection_string": "${PRIMARY_URL}",
                    "join_group": "shared",
                },
                {
                    "id": "other",
                    "dialect": "postgresql",
                    "connection_string": "${OTHER_URL}",
                    "join_group": "shared",
                },
            ]
        )
        assert registry.all_ids() == ["other", "primary"]

    def test_a_join_group_on_the_same_host_with_different_case_is_allowed(self, monkeypatch):
        """Hostnames are case-insensitive (DNS); a spelling difference alone
        must not trip the check."""
        monkeypatch.setenv("PRIMARY_URL", "postgresql+asyncpg://user:pass@Host/primary_db")
        monkeypatch.setenv("OTHER_URL", "postgresql+asyncpg://user:pass@host/other_db")
        registry = ConnectionRegistry.from_entries(
            [
                {
                    "id": "primary",
                    "dialect": "postgresql",
                    "connection_string": "${PRIMARY_URL}",
                    "join_group": "shared",
                },
                {
                    "id": "other",
                    "dialect": "postgresql",
                    "connection_string": "${OTHER_URL}",
                    "join_group": "shared",
                },
            ]
        )
        assert registry.all_ids() == ["other", "primary"]

    def test_connections_with_no_shared_join_group_are_unaffected_by_host_mismatch(
        self, monkeypatch
    ):
        """Two connections on different hosts are entirely normal when they
        don't share a join_group (the default — no cross-connection join is
        even possible between them); only shared membership triggers the
        check."""
        monkeypatch.setenv("PRIMARY_URL", "postgresql+asyncpg://user:pass@host-a/primary_db")
        monkeypatch.setenv("OTHER_URL", "postgresql+asyncpg://user:pass@host-b/other_db")
        registry = ConnectionRegistry.from_entries(
            [
                {"id": "primary", "dialect": "postgresql", "connection_string": "${PRIMARY_URL}"},
                {"id": "other", "dialect": "postgresql", "connection_string": "${OTHER_URL}"},
            ]
        )
        assert registry.all_ids() == ["other", "primary"]

    def test_an_unparseable_connection_string_in_the_group_does_not_trip_the_check(
        self, monkeypatch
    ):
        """A resolved connection string that still can't be parsed as a URL
        at all (e.g. a config-governance dry-run validating a draft whose
        secret resolves to a non-URL placeholder value) gives no basis to
        assert a host mismatch — same posture as item 158's dialect-match
        validator for this exact case, not a bypass of it. (An actually-
        unresolved `${VAR}` reference raises eagerly inside `interpolate`
        itself, per `test_missing_env_var_raises` above — this exercises the
        distinct case where interpolation succeeds but the RESULT isn't a
        parseable URL.)"""
        monkeypatch.setenv("PRIMARY_URL", "postgresql+asyncpg://user:pass@host/primary_db")
        monkeypatch.setenv("OTHER_URL", "not-a-real-url-at-all")
        registry = ConnectionRegistry.from_entries(
            [
                {
                    "id": "primary",
                    "dialect": "postgresql",
                    "connection_string": "${PRIMARY_URL}",
                    "join_group": "shared",
                },
                {
                    "id": "other",
                    "dialect": "postgresql",
                    "connection_string": "${OTHER_URL}",
                    "join_group": "shared",
                },
            ]
        )
        assert registry.all_ids() == ["other", "primary"]

    def test_constructing_the_registry_directly_bypasses_the_check(self):
        """This is a config-load-boundary-only check by design: enforced at
        `from_entries`/`from_file`, not `ConnectionRegistry.__init__` (a
        distinct, batch-scoped check with no field-validator precedent to
        lean on — corrected 2026-08-09 after `claim-reviewer` found the
        original docstring here mischaracterized item 158's own scope as the
        same pattern; item 158 is a `field_validator` that runs on EVERY
        `ConnectionProfile` construction, including a direct one). A raw
        dict of already-validated profiles (test fixtures, `catalog/
        adaptive_learning_benchmark.py`) is unaffected, so tests that
        deliberately construct cross-host fixtures for an unrelated purpose
        (e.g. a not-connectable secondary dialect) don't need to also satisfy
        this check."""
        from querygate.connections.models import ConnectionProfile

        registry = ConnectionRegistry(
            {
                "primary": ConnectionProfile(
                    id="primary",
                    dialect="postgresql",
                    connection_string="postgresql+asyncpg://user:pass@host-a/primary_db",
                    join_group="shared",
                ),
                "other": ConnectionProfile(
                    id="other",
                    dialect="postgresql",
                    connection_string="postgresql+asyncpg://user:pass@host-b/other_db",
                    join_group="shared",
                ),
            }
        )
        assert registry.all_ids() == ["other", "primary"]

    def test_a_host_packed_into_a_query_parameter_is_still_compared(self, monkeypatch):
        """security-invariant-reviewer, 2026-08-09 (QG170-2): a driver that
        packs the real host into a query parameter instead of the URL's own
        host component (`postgresql+asyncpg://user:pass@/db?host=...`, the
        unix-socket/PgBouncer idiom) still carries a real, comparable host —
        `url.host` alone being empty must not silently opt this member out
        of the check. Verified directly this parses with `url.host is None`
        but `url.query["host"]` set."""
        monkeypatch.setenv("PRIMARY_URL", "postgresql+asyncpg://user:pass@/primary_db?host=host-a")
        monkeypatch.setenv("OTHER_URL", "postgresql+asyncpg://user:pass@host-b/other_db")
        with pytest.raises(ValueError, match="spans different hosts"):
            ConnectionRegistry.from_entries(
                [
                    {
                        "id": "primary",
                        "dialect": "postgresql",
                        "connection_string": "${PRIMARY_URL}",
                        "join_group": "shared",
                    },
                    {
                        "id": "other",
                        "dialect": "postgresql",
                        "connection_string": "${OTHER_URL}",
                        "join_group": "shared",
                    },
                ]
            )

    def test_a_disabled_connection_on_a_different_host_does_not_trip_the_check(self, monkeypatch):
        """security-invariant-reviewer, 2026-08-09 (QG170-5): a disabled
        connection can never actually be resolved
        (`connections/visibility.py`), so it can never be a join's
        secondary — comparing its host would only produce a false-positive
        rejection of an otherwise-valid config, e.g. an operator disabling a
        connection while re-pointing it at a new host."""
        monkeypatch.setenv("PRIMARY_URL", "postgresql+asyncpg://user:pass@host-a/primary_db")
        monkeypatch.setenv("OTHER_URL", "postgresql+asyncpg://user:pass@host-b/other_db")
        registry = ConnectionRegistry.from_entries(
            [
                {
                    "id": "primary",
                    "dialect": "postgresql",
                    "connection_string": "${PRIMARY_URL}",
                    "join_group": "shared",
                },
                {
                    "id": "other",
                    "dialect": "postgresql",
                    "connection_string": "${OTHER_URL}",
                    "join_group": "shared",
                    "enabled": False,
                },
            ]
        )
        assert registry.all_ids() == ["other", "primary"]

    def test_a_three_member_group_with_one_odd_host_out_is_rejected(self, monkeypatch):
        """test-contract-reviewer, 2026-08-09 (F2): the existing rejection
        tests only ever use a clean 2-way split; this pins that the
        `set`-cardinality check genuinely generalizes past 2 members rather
        than accidentally hardcoding a pairwise comparison (e.g. a mutation
        like `len(host_names) > 2` would still pass every 2-member test but
        would NOT catch this case)."""
        monkeypatch.setenv("A_URL", "postgresql+asyncpg://user:pass@host/a_db")
        monkeypatch.setenv("B_URL", "postgresql+asyncpg://user:pass@host/b_db")
        monkeypatch.setenv("C_URL", "postgresql+asyncpg://user:pass@host-odd/c_db")
        with pytest.raises(ValueError, match="spans different hosts"):
            ConnectionRegistry.from_entries(
                [
                    {
                        "id": "a",
                        "dialect": "postgresql",
                        "connection_string": "${A_URL}",
                        "join_group": "shared",
                    },
                    {
                        "id": "b",
                        "dialect": "postgresql",
                        "connection_string": "${B_URL}",
                        "join_group": "shared",
                    },
                    {
                        "id": "c",
                        "dialect": "postgresql",
                        "connection_string": "${C_URL}",
                        "join_group": "shared",
                    },
                ]
            )
