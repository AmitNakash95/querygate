"""Drift guard for landing/sandbox.html's embedded scenario fixtures.

The sandbox is a static, browser-only page (TODO.md item 34) with its own
JS reimplementation of policy validation so it can run with no backend. This
test extracts the same JSON fixtures the page embeds and replays them through
the *real* StructuredQuery/Policy/validate_policy/clamp_limit — so a change
to real policy semantics that the sandbox's copy would silently misrepresent
fails here instead of only being caught by eyeballing the page.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from querygate.compiler.sqlalchemy_compiler import clamp_limit
from querygate.core.exceptions import PolicyViolationError
from querygate.policy.models import Policy
from querygate.query_ast.models import StructuredQuery
from querygate.validation.policy_validation import validate_policy

SANDBOX_PATH = Path(__file__).parent.parent.parent / "landing" / "sandbox.html"

# Expected behavior per scenario id — the source of truth this test checks
# the page's fixtures against. Adding a scenario to the page without adding
# an entry here is a deliberate failure, not a silent gap.
EXPECTED = {
    "raw-sql": {"kind": "contract_reject"},
    "safe-aggregate": {"kind": "pass"},
    "denied-column": {"kind": "policy_violation", "match": "Column 'customers.email'"},
    "denied-table": {"kind": "policy_violation", "match": "Table 'employees'"},
    "clamped-limit": {"kind": "pass", "clamp_from": 5000, "clamp_to": 200},
    "mandatory-filter": {"kind": "pass"},
}


def _load_fixtures() -> dict:
    html = SANDBOX_PATH.read_text()
    match = re.search(
        r'<script id="scenario-fixtures" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match, "sandbox.html must embed a #scenario-fixtures JSON script block"
    return json.loads(match.group(1))


@pytest.fixture(scope="module")
def fixtures() -> dict:
    return _load_fixtures()


@pytest.fixture(scope="module")
def base_policy(fixtures: dict) -> Policy:
    base = fixtures["basePolicy"]
    return Policy(
        enabled=base["enabled"],
        allowed_tables=base["allowed_tables"],
        denied_columns=base["denied_columns"],
        max_select_columns=base["max_select_columns"],
        max_joins=base["max_joins"],
        max_limit=base["max_limit"],
        max_limit_aggregate=base["max_limit_aggregate"],
        default_limit=base["default_limit"],
    )


def test_scenario_ids_match_expected_coverage(fixtures: dict):
    page_ids = {s["id"] for s in fixtures["scenarios"]}
    assert page_ids == set(EXPECTED), (
        "landing/sandbox.html's scenarios changed without updating this test's "
        "EXPECTED map — add/remove the matching entry so drift stays caught."
    )


def test_raw_sql_scenario_is_not_a_valid_structured_query(fixtures: dict):
    scenario = next(s for s in fixtures["scenarios"] if s["id"] == "raw-sql")
    assert scenario.get("rejectAtContract") is True
    attempt = dict(scenario["rawAttempt"])
    attempt.pop("connection", None)
    with pytest.raises(Exception, match="sql"):
        StructuredQuery.model_validate(attempt)


@pytest.mark.parametrize("scenario_id", [s for s in EXPECTED if EXPECTED[s]["kind"] != "contract_reject"])
def test_scenario_matches_real_policy_engine(fixtures: dict, base_policy: Policy, scenario_id: str):
    expected = EXPECTED[scenario_id]
    scenario = next(s for s in fixtures["scenarios"] if s["id"] == scenario_id)
    query = StructuredQuery.model_validate(scenario["query"]["query"])

    if expected["kind"] == "policy_violation":
        with pytest.raises(PolicyViolationError, match=re.escape(expected["match"])):
            validate_policy(query, base_policy, connection_id="demo")
        return

    validate_policy(query, base_policy, connection_id="demo")  # must not raise

    if "clamp_from" in expected:
        assert query.limit == expected["clamp_from"]
        is_aggregate = any(not isinstance(item, str) and hasattr(item, "fn") for item in query.select)
        clamped = clamp_limit(query.limit, base_policy, is_aggregate=is_aggregate)
        assert clamped == expected["clamp_to"]


def test_reporting_service_principal_override_matches_policy_yaml_pattern(fixtures: dict):
    override = fixtures["principals"]["reporting-service"]["*"]
    assert override["max_limit"] == 20
    scoped_policy = Policy(max_limit=override["max_limit"])
    assert scoped_policy.max_limit == 20
