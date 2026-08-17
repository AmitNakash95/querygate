"""`querygate-shapes` — the operator CLI over item 195's admin API.

The first version of this CLI read the in-process store directly and therefore
*always* reported "no query shapes recorded yet" against a real deployment, and
had no tests at all to reveal it (`claim-reviewer`, 2026-08-17). These tests
exist so that class of defect fails loudly: the CLI is exercised against a
stubbed HTTP layer, and the "disabled" vs "enabled but empty" distinction — the
one an operator would act on wrongly — is pinned explicitly.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
import yaml

from querygate import observed_shapes_cli as cli

pytestmark = pytest.mark.unit


def _stub(monkeypatch, payload: Dict[str, Any], captured: dict | None = None):
    def fake_request(args, path, params):
        if captured is not None:
            captured["path"] = path
            captured["params"] = params
        return payload

    monkeypatch.setattr(cli, "_request", fake_request)


_SHAPE = {
    "shape_hash": "abc123",
    "connection_id": "demo",
    "principal_id": "agent",
    "skeleton": {"from_table": "orders", "select": ["orders.id"]},
    "parameters": [{"name": "status", "type": "string", "required": True, "is_list": False}],
    "occurrences": 7,
    "first_seen": "2026-08-17T00:00:00Z",
    "last_seen": "2026-08-17T01:00:00Z",
}


def test_disabled_is_reported_distinctly_from_empty(monkeypatch, capsys):
    """The failure this CLI shipped with. "Recording is off" and "recording is
    on and your agent ran nothing" lead an operator to opposite actions, so
    they must never render the same.
    """
    _stub(monkeypatch, {"enabled": False, "shapes": [], "max_entries": 500, "evicted_total": 0})
    assert cli.main(["--token", "t", "list"]) == 0
    disabled = capsys.readouterr().err

    _stub(monkeypatch, {"enabled": True, "shapes": [], "max_entries": 500, "evicted_total": 0})
    assert cli.main(["--token", "t", "list"]) == 0
    empty = capsys.readouterr().err

    assert "DISABLED" in disabled
    assert "DISABLED" not in empty
    assert "no query shapes" in empty.lower()
    # The scope line must appear in BOTH — on an empty window, knowing whether
    # you are looking at the whole fleet or one replica matters most.
    assert "scope=" in disabled and "scope=" in empty


def test_list_renders_recorded_shapes(monkeypatch, capsys):
    _stub(
        monkeypatch,
        {
            "enabled": True,
            "shapes": [_SHAPE],
            "max_entries": 500,
            "evicted_total": 0,
            "skeletonization_failures": 0,
            "scope": "process-local-volatile",
        },
    )
    assert cli.main(["--token", "t", "list"]) == 0
    out = capsys.readouterr()
    assert "abc123" in out.out and "orders" in out.out and "7" in out.out
    assert "process-local-volatile" in out.err


def test_an_incomplete_list_is_flagged_rather_than_shown_as_complete(monkeypatch, capsys):
    """Eviction and skeletonization failure both mean "this list is missing
    shapes". Narrowing a connection on a silently-truncated list is exactly how
    an agent breaks after cutover, so both warn.
    """
    _stub(
        monkeypatch,
        {
            "enabled": True,
            "shapes": [_SHAPE],
            "max_entries": 1,
            "evicted_total": 12,
            "skeletonization_failures": 3,
            "scope": "process-local-volatile",
        },
    )
    cli.main(["--token", "t", "list"])
    err = capsys.readouterr().err
    assert "evicted" in err.lower() and "incomplete" in err.lower()
    assert "could not be reduced" in err


def test_draft_emits_reviewable_yaml_and_says_it_is_not_installed(monkeypatch, capsys):
    captured: dict = {}
    _stub(
        monkeypatch,
        {
            "id": "orders_by_status",
            "connection": "demo",
            "description": None,
            "parameters": [
                {"name": "status", "type": "string", "required": True, "is_list": False}
            ],
            "query": {"from_table": "orders", "select": ["orders.id"]},
        },
        captured,
    )
    assert cli.main(["--token", "t", "draft", "abc123", "--template-id", "orders_by_status"]) == 0
    out = capsys.readouterr()
    document = yaml.safe_load(out.out)
    assert document["templates"][0]["id"] == "orders_by_status"
    # `description: None` must not survive into the YAML — the templates file
    # model forbids nothing here, but a null description is noise in a draft a
    # human is about to edit.
    assert "description" not in document["templates"][0]
    assert "NOT" in out.err and "installed" in out.err
    assert captured["params"]["template_id"] == "orders_by_status"


def test_draft_can_disambiguate_by_connection_and_principal(monkeypatch):
    """A shape hash is not unique — the same shape run by two principals is two
    records — so the CLI must be able to say which one it means.
    """
    captured: dict = {}
    _stub(monkeypatch, {"id": "t", "connection": "demo", "parameters": [], "query": {}}, captured)
    cli.main(
        [
            "--token",
            "t",
            "draft",
            "abc123",
            "--template-id",
            "t",
            "--connection",
            "demo",
            "--principal",
            "agent",
        ]
    )
    assert captured["params"]["connection_id"] == "demo"
    assert captured["params"]["principal_id"] == "agent"


def test_an_unreachable_backend_is_not_reported_as_an_idle_agent(monkeypatch, capsys):
    """The third way an empty list happens. Fail-open means an unreachable Redis
    renders exactly like "recording is on and nothing ran", and an operator who
    narrows from that list narrows to nothing.
    """
    _stub(
        monkeypatch,
        {
            "enabled": True,
            "shapes": [],
            "max_entries": 500,
            "evicted_total": 0,
            "skeletonization_failures": 0,
            "scope": "shared-durable",
            "backend_healthy": False,
        },
    )
    assert cli.main(["--token", "t", "list"]) == 0
    err = capsys.readouterr().err
    assert "UNREACHABLE" in err
    assert "not because nothing ran" in err.lower()
    assert "Do not narrow" in err


def test_the_scope_line_explains_which_guarantee_you_have(monkeypatch, capsys):
    for scope, expected in (
        ("shared-durable", "shared across replicas"),
        ("process-local-volatile", "discarded on restart"),
    ):
        _stub(
            monkeypatch,
            {
                "enabled": True,
                "shapes": [_SHAPE],
                "max_entries": 500,
                "evicted_total": 0,
                "skeletonization_failures": 0,
                "scope": scope,
            },
        )
        cli.main(["--token", "t", "list"])
        assert expected in capsys.readouterr().err


def test_a_missing_credential_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("QUERYGATE_ADMIN_TOKEN", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["list"])
    assert "admin:shapes:read" in str(excinfo.value)
