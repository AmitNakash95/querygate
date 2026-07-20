"""Unit tests for TODO.md item 46's policy templates/safe-start presets."""

from __future__ import annotations

import pytest
import yaml

from querygate.admin.templates import list_templates, render_template
from querygate.core.exceptions import ConfigValidationError


def test_list_templates_returns_five_fixed_reviewed_presets():
    templates = list_templates()

    assert {t.id for t in templates} == {
        "deny-by-default",
        "reporting-only",
        "customer-support",
        "tenant-isolated",
        "bounded-analytics",
    }
    for template in templates:
        assert template.description
        for param in template.parameters:
            assert param.description


def test_render_unknown_template_is_rejected():
    with pytest.raises(ConfigValidationError, match="Unknown policy template"):
        render_template("does-not-exist", {}, None)


def test_render_missing_required_parameter_is_rejected():
    with pytest.raises(ConfigValidationError, match="allowed_tables"):
        render_template("reporting-only", {"connection": "demo"}, None)


def test_deny_by_default_sets_top_level_default_disabled():
    result = render_template("deny-by-default", {}, None)

    document = yaml.safe_load(result.policy_yaml)
    assert document["default"]["enabled"] is False
    assert result.rules


def test_reporting_only_renders_expected_connection_patch():
    result = render_template(
        "reporting-only",
        {"connection": "demo", "allowed_tables": ["orders", "customers"], "max_limit": 20},
        None,
    )

    section = yaml.safe_load(result.policy_yaml)["connections"]["demo"]
    assert section["allowed_tables"] == ["orders", "customers"]
    assert section["max_joins"] == 2
    assert section["max_where_depth"] == 2
    assert section["max_limit"] == 20
    assert section["max_limit_aggregate"] == 200


def test_template_never_loosens_an_existing_stricter_cap():
    existing = yaml.safe_dump({"connections": {"demo": {"max_joins": 1}}})

    result = render_template(
        "reporting-only",
        {"connection": "demo", "allowed_tables": ["orders"], "max_limit": 20},
        existing,
    )

    section = yaml.safe_load(result.policy_yaml)["connections"]["demo"]
    assert section["max_joins"] == 1
    assert any("kept the existing, stricter max_joins=1" in rule for rule in result.rules)


def test_template_never_widens_an_existing_allow_list():
    existing = yaml.safe_dump({"connections": {"demo": {"allowed_tables": ["orders"]}}})

    result = render_template(
        "reporting-only",
        {"connection": "demo", "allowed_tables": ["orders", "customers"], "max_limit": 20},
        existing,
    )

    section = yaml.safe_load(result.policy_yaml)["connections"]["demo"]
    assert section["allowed_tables"] == ["orders"]
    assert any("narrowed to orders" in rule for rule in result.rules)


def test_template_adds_a_new_allow_list_when_none_existed():
    result = render_template(
        "reporting-only",
        {"connection": "demo", "allowed_tables": ["orders", "customers"], "max_limit": 20},
        None,
    )

    section = yaml.safe_load(result.policy_yaml)["connections"]["demo"]
    assert section["allowed_tables"] == ["orders", "customers"]


def test_customer_support_unions_denied_columns_without_dropping_existing_ones():
    existing = yaml.safe_dump({"connections": {"demo": {"denied_columns": {"customers": ["ssn"]}}}})

    result = render_template(
        "customer-support",
        {
            "connection": "demo",
            "allowed_tables": ["customers"],
            "pii_table": "customers",
            "pii_columns": ["email"],
        },
        existing,
    )

    section = yaml.safe_load(result.policy_yaml)["connections"]["demo"]
    assert sorted(section["denied_columns"]["customers"]) == ["email", "ssn"]


def test_tenant_isolated_appends_filter_without_removing_an_existing_one():
    existing = yaml.safe_dump(
        {
            "connections": {
                "demo": {
                    "mandatory_row_filters": [
                        {"table": "orders", "column": "region", "value": "us"}
                    ]
                }
            }
        }
    )

    result = render_template(
        "tenant-isolated",
        {
            "connection": "demo",
            "table": "orders",
            "tenant_column": "tenant_id",
            "tenant_claim": "tenant_id",
        },
        existing,
    )

    filters = yaml.safe_load(result.policy_yaml)["connections"]["demo"]["mandatory_row_filters"]
    assert {"table": "orders", "column": "region", "value": "us"} in filters
    assert {"table": "orders", "column": "tenant_id", "from_claim": "tenant_id"} in filters
    assert len(filters) == 2


def test_tenant_isolated_reapplication_is_idempotent():
    params = {
        "connection": "demo",
        "table": "orders",
        "tenant_column": "tenant_id",
        "tenant_claim": "tenant_id",
    }
    first = render_template("tenant-isolated", params, None)
    second = render_template("tenant-isolated", params, first.policy_yaml)

    filters = yaml.safe_load(second.policy_yaml)["connections"]["demo"]["mandatory_row_filters"]
    assert len(filters) == 1


def test_bounded_analytics_respects_custom_parameters():
    result = render_template(
        "bounded-analytics",
        {
            "connection": "analytics",
            "max_joins": 4,
            "max_limit_aggregate": 750,
            "timeout_seconds": 20,
        },
        None,
    )

    section = yaml.safe_load(result.policy_yaml)["connections"]["analytics"]
    assert section["max_joins"] == 4
    assert section["max_limit_aggregate"] == 750
    assert section["timeout_seconds"] == 20


def test_template_never_embeds_connection_strings_or_secrets():
    for template_id, params in [
        ("deny-by-default", {}),
        ("reporting-only", {"connection": "demo", "allowed_tables": ["orders"]}),
        ("bounded-analytics", {"connection": "demo"}),
    ]:
        result = render_template(template_id, params, None)
        assert "connection_string" not in result.policy_yaml
        assert "${" not in result.policy_yaml
