"""Unit tests for the `querygate-validate-config` CLI."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from querygate.cli import load_config_context, validate_config
from querygate.secrets.resolvers import EnvSecretResolver, SecretResolverRegistry

# Deliberately short, and matching `test_rest_api.py`'s own `_LIVE_PASSWORD`
# exactly (item 165): pydantic truncates a long `ValidationError.input_value`
# dict repr to a small head/tail window, and confirmed directly (before
# writing these tests) that only this specific short id + short password
# shape survives that truncation -- a longer id or a few extra password
# characters pushes the password out of the surviving tail and the
# un-fixed code would pass these tests for the wrong reason. Every test
# below reuses this exact id/password shape for that reason.
_LIVE_PASSWORD = "S3cretPw"

CONNECTIONS_YAML = """
connections:
  - id: demo
    dialect: postgresql
    connection_string: ${TEST_DEMO_DB_URL}
    known_tables: [customers, orders]
"""

POLICY_YAML = """
default:
  enabled: true

connections:
  demo:
    max_joins: 2
"""


def _write(path: Path, name: str, content: str) -> str:
    file_path = path / name
    file_path.write_text(content)
    return str(file_path)


def test_valid_config_has_no_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DEMO_DB_URL", "postgresql+asyncpg://user:pass@localhost/demo")
    connections_file = _write(tmp_path, "connections.yaml", CONNECTIONS_YAML)
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)

    errors = validate_config(connections_file, policy_file)
    assert errors == []


def test_missing_connections_file_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DEMO_DB_URL", "postgresql+asyncpg://user:pass@localhost/demo")
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)

    errors = validate_config(str(tmp_path / "does_not_exist.yaml"), policy_file)
    assert len(errors) == 1
    assert "does_not_exist.yaml" in errors[0]


def test_unresolved_env_var_is_reported(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_DEMO_DB_URL", raising=False)
    connections_file = _write(tmp_path, "connections.yaml", CONNECTIONS_YAML)
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)

    errors = validate_config(connections_file, policy_file)
    assert len(errors) == 1
    assert "TEST_DEMO_DB_URL" in errors[0]


def test_policy_referencing_unknown_connection_id_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DEMO_DB_URL", "postgresql+asyncpg://user:pass@localhost/demo")
    connections_file = _write(tmp_path, "connections.yaml", CONNECTIONS_YAML)
    policy_file = _write(
        tmp_path,
        "policy.yaml",
        """
default:
  enabled: true

connections:
  typo_connection:
    max_joins: 2
""",
    )

    errors = validate_config(connections_file, policy_file)
    assert len(errors) == 1
    assert "typo_connection" in errors[0]
    assert "demo" in errors[0]


def test_invalid_policy_field_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DEMO_DB_URL", "postgresql+asyncpg://user:pass@localhost/demo")
    connections_file = _write(tmp_path, "connections.yaml", CONNECTIONS_YAML)
    policy_file = _write(
        tmp_path,
        "policy.yaml",
        """
default:
  enabled: true
  max_joins: "not-a-number"
""",
    )

    errors = validate_config(connections_file, policy_file)
    assert len(errors) == 1
    assert policy_file in errors[0]


def test_both_files_broken_reports_both_errors(tmp_path):
    errors = validate_config(
        str(tmp_path / "missing_connections.yaml"), str(tmp_path / "missing_policy.yaml")
    )
    assert len(errors) == 2


def test_catalog_file_is_optional(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DEMO_DB_URL", "postgresql+asyncpg://user:pass@localhost/demo")
    connections_file = _write(tmp_path, "connections.yaml", CONNECTIONS_YAML)
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)

    errors = validate_config(connections_file, policy_file, catalog_file=None)
    assert errors == []


def test_valid_catalog_file_has_no_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DEMO_DB_URL", "postgresql+asyncpg://user:pass@localhost/demo")
    connections_file = _write(tmp_path, "connections.yaml", CONNECTIONS_YAML)
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)
    catalog_file = _write(
        tmp_path,
        "catalog.yaml",
        """
connections:
  demo:
    tables:
      customers:
        description: "One row per customer."
""",
    )

    errors = validate_config(connections_file, policy_file, catalog_file=catalog_file)
    assert errors == []


def test_missing_catalog_file_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DEMO_DB_URL", "postgresql+asyncpg://user:pass@localhost/demo")
    connections_file = _write(tmp_path, "connections.yaml", CONNECTIONS_YAML)
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)

    errors = validate_config(
        connections_file, policy_file, catalog_file=str(tmp_path / "missing_catalog.yaml")
    )
    assert len(errors) == 1
    assert "missing_catalog.yaml" in errors[0]


def test_catalog_referencing_unknown_connection_id_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DEMO_DB_URL", "postgresql+asyncpg://user:pass@localhost/demo")
    connections_file = _write(tmp_path, "connections.yaml", CONNECTIONS_YAML)
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)
    catalog_file = _write(
        tmp_path,
        "catalog.yaml",
        """
connections:
  typo_connection:
    tables: {}
""",
    )

    errors = validate_config(connections_file, policy_file, catalog_file=catalog_file)
    assert len(errors) == 1
    assert "typo_connection" in errors[0]
    assert "demo" in errors[0]


def test_invalid_catalog_field_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DEMO_DB_URL", "postgresql+asyncpg://user:pass@localhost/demo")
    connections_file = _write(tmp_path, "connections.yaml", CONNECTIONS_YAML)
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)
    catalog_file = _write(
        tmp_path,
        "catalog.yaml",
        """
connections:
  demo:
    tables:
      customers:
        not_a_real_field: 1
""",
    )

    errors = validate_config(connections_file, policy_file, catalog_file=catalog_file)
    assert len(errors) == 1
    assert catalog_file in errors[0]


def test_validate_config_resolves_connection_strings_through_custom_resolver_registry(tmp_path):
    connections_file = _write(
        tmp_path,
        "connections.yaml",
        """
connections:
  - id: demo
    dialect: postgresql
    connection_string: ${vault:demo#url}
""",
    )
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)
    resolver_registry = SecretResolverRegistry(
        {"env": EnvSecretResolver({}), "vault": _FakeResolver({"demo#url": "postgresql://ok"})}
    )

    errors = validate_config(connections_file, policy_file, resolver_registry=resolver_registry)
    assert errors == []


def test_validate_config_reports_unresolvable_vault_reference(tmp_path):
    connections_file = _write(
        tmp_path,
        "connections.yaml",
        """
connections:
  - id: demo
    dialect: postgresql
    connection_string: ${vault:demo#url}
""",
    )
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)
    resolver_registry = SecretResolverRegistry({"env": EnvSecretResolver({})})

    errors = validate_config(connections_file, policy_file, resolver_registry=resolver_registry)
    assert len(errors) == 1
    assert "No secret resolver registered for scheme 'vault'" in errors[0]


class _FakeResolver:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def resolve(self, reference: str) -> str:
        return self._values[reference]


def test_load_config_context_missing_field_error_does_not_leak_credential(tmp_path, monkeypatch):
    """Item 168 regression (gap 1): a malformed connections.yaml entry that
    trips a pydantic `missing`-type error embeds the WHOLE already-
    interpolated entry dict (including the live connection_string) in that
    error's `input_value` (item 165's own finding, reused here at the
    `load_config_context` layer rather than the HTTP layer item 165 already
    covers). Before this fix, `load_config_context`'s generic
    `except Exception as exc: errors.append(f"...: {exc}")` handed that
    straight through -- reachable both from `querygate-validate-config` and
    from `admin/service.py`'s config-governance dry-run endpoints
    (`/admin/config/validate`, `/admin/config/preview`)."""
    monkeypatch.setenv(
        "TEST_CLI_MISSING_FIELD_URL", f"postgresql://user:{_LIVE_PASSWORD}@db:5432/app"
    )
    connections_file = _write(
        tmp_path,
        "connections.yaml",
        """
connections:
  - id: leaky-demo
    connection_string: ${TEST_CLI_MISSING_FIELD_URL}
""",
    )
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)

    _context, errors = load_config_context(connections_file, policy_file)

    assert len(errors) == 1
    assert _LIVE_PASSWORD not in errors[0]
    # Still actionable -- names the missing field.
    assert "dialect" in errors[0]


def test_load_config_context_yaml_syntax_error_does_not_leak_credential(tmp_path):
    """Item 168 regression (gap 1), YAML case: a SYNTACTICALLY broken
    connections.yaml raises `yaml.YAMLError` before pydantic ever runs, and
    PyYAML's own `Mark.__str__()` embeds the literal offending SOURCE LINE.
    A connections.yaml entry is permitted to carry a literal (non-`${...}`)
    credential (config-governance drafts explicitly support this), so an
    unterminated quote on a `connection_string:` line puts the real password
    inside `str(yaml.YAMLError)` -- which reached `load_config_context`'s
    generic `except Exception` handler verbatim before this fix. This is a
    second, independently-found gap: item 168's own write-up only flagged
    the pydantic case here, but `ConnectionRegistry.from_file` parses YAML
    before it interpolates or validates anything, so a broken connections.yaml
    hits this same generic handler with a `yaml.YAMLError`, not a
    `pydantic.ValidationError` -- and reaches it through the same
    REST-exposed dry-run endpoints as the pydantic case above."""
    connections_file = _write(
        tmp_path,
        "connections.yaml",
        f"""
connections:
  - id: leaky-yaml
    dialect: postgresql
    connection_string: "postgresql://user:{_LIVE_PASSWORD}@db:5432/app
""",
    )
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)

    _context, errors = load_config_context(connections_file, policy_file)

    assert len(errors) == 1
    assert _LIVE_PASSWORD not in errors[0]
    # Still actionable -- names roughly where the syntax broke.
    assert "line" in errors[0]


def test_main_stderr_does_not_leak_credential_on_missing_field_error(tmp_path, monkeypatch, capsys):
    """Item 168 regression (gap 2): `main()` (the `querygate-validate-config`
    CLI entry point) prints `validate_config`'s error list straight to
    stderr. Before this fix that error list was built from
    `load_config_context`'s raw, unscrubbed `f"...: {exc}"` string -- a
    Vault- or env-resolved credential in a malformed connections.yaml would
    land in an operator's terminal or CI log. Pins the fix at the actual
    stderr output, not just the returned error list `validate_config` also
    covers."""
    monkeypatch.setenv("TEST_CLI_MAIN_LEAK_URL", f"postgresql://user:{_LIVE_PASSWORD}@db:5432/app")
    connections_file = _write(
        tmp_path,
        "connections.yaml",
        """
connections:
  - id: leaky-cli
    connection_string: ${TEST_CLI_MAIN_LEAK_URL}
""",
    )
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "querygate-validate-config",
            "--connections-file",
            connections_file,
            "--policy-file",
            policy_file,
        ],
    )

    from querygate.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert _LIVE_PASSWORD not in captured.err
    assert _LIVE_PASSWORD not in captured.out
    # Still actionable -- names the missing field.
    assert "dialect" in captured.err


def test_main_subprocess_stderr_does_not_leak_credential_on_missing_field_error(
    tmp_path, monkeypatch
):
    """Same regression as the in-process `main()` test above, but exercised
    as an actual separate `python -m querygate.cli` subprocess -- the literal
    shape a CI job or an operator's terminal would see, ruling out any
    in-process test-harness artifact (captured logging handlers, monkeypatched
    stdio, ...) from masking a real leak. Reuses `leaky-demo` + `_LIVE_PASSWORD`
    unmodified (see the module-level comment) -- a longer id here stops the
    password from surviving pydantic's own truncation, which would make this
    test pass for the wrong reason even against the un-fixed code."""
    connections_file = _write(
        tmp_path,
        "connections.yaml",
        f"""
connections:
  - id: leaky-demo
    connection_string: postgresql://user:{_LIVE_PASSWORD}@db:5432/app
""",
    )
    policy_file = _write(tmp_path, "policy.yaml", POLICY_YAML)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "querygate.cli",
            "--connections-file",
            connections_file,
            "--policy-file",
            policy_file,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 1
    assert _LIVE_PASSWORD not in result.stderr
    assert _LIVE_PASSWORD not in result.stdout
    assert "dialect" in result.stderr
