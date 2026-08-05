"""Validated policy templates and safe-start presets (TODO.md item 46).

Each template is a small, fixed, code-reviewed Python definition — never
inferred from live schema/table names, never carrying credentials or tenant
values — that renders a `policy.yaml` patch from a caller's typed parameters
and merges it into the caller's local draft. The merge is monotonically
restrictive: a template can only add a new restriction or tighten one
already present in the draft, never loosen or remove one. This makes it safe
to apply a template on top of policy an administrator has already
customized, rather than only being safe against a blank starting document.

This module only ever manipulates the `policy.yaml` document text supplied
by the caller — it never touches the live registry/policy singletons, the
config-version store, or any secret. Rendering is a pure function; the
existing `/validate`, item 39's `/simulate`, and item 40's `/diff` endpoints
still run against the result exactly like any hand-edited draft before it is
staged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

import yaml

from querygate.admin.models import (
    PolicyTemplateRenderResult,
    PolicyTemplateSummary,
    TemplateParameter,
)
from querygate.core.exceptions import ConfigValidationError

# Numeric guardrail fields a template may set, where a lower value is
# strictly more restrictive. Only the fields the shipped templates below
# actually emit — not every `Policy` guardrail — so the merge rule stays
# exactly as broad as what can currently be produced.
_CAP_FIELDS = (
    "max_joins",
    "max_where_depth",
    "max_limit",
    "max_limit_aggregate",
    "timeout_seconds",
)

_BuildResult = tuple[
    str, dict, list[str]
]  # (scope, patch, rules) — scope is "default" or a connection id


def _require_str(params: dict, name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigValidationError(
            f"Template parameter {name!r} is required and must be a non-empty string."
        )
    return value.strip()


def _require_str_list(params: dict, name: str) -> list[str]:
    value = params.get(name)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise ConfigValidationError(
            f"Template parameter {name!r} is required and must be a non-empty list of strings."
        )
    return [item.strip() for item in value]


def _optional_int(params: dict, name: str, default: int) -> int:
    value = params.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigValidationError(f"Template parameter {name!r} must be an integer.")
    return value


def _build_deny_by_default(params: dict) -> _BuildResult:
    return (
        "default",
        {"enabled": False},
        ["Every connection is denied unless explicitly enabled per connection or principal."],
    )


def _build_reporting_only(params: dict) -> _BuildResult:
    connection = _require_str(params, "connection")
    allowed_tables = _require_str_list(params, "allowed_tables")
    max_limit = _optional_int(params, "max_limit", 100)
    max_limit_aggregate = min(max_limit * 10, 1000)
    patch = {
        "allowed_tables": allowed_tables,
        "max_joins": 2,
        "max_where_depth": 2,
        "max_limit": max_limit,
        "max_limit_aggregate": max_limit_aggregate,
    }
    rules = [
        "Joins capped at 2, where-clause depth capped at 2 — narrow reporting shapes only.",
        f"Row selects capped at {max_limit} rows; aggregates capped at {max_limit_aggregate} rows.",
    ]
    return connection, patch, rules


def _build_customer_support(params: dict) -> _BuildResult:
    connection = _require_str(params, "connection")
    allowed_tables = _require_str_list(params, "allowed_tables")
    pii_table = _require_str(params, "pii_table")
    pii_columns = _require_str_list(params, "pii_columns")
    patch = {
        "allowed_tables": allowed_tables,
        "denied_columns": {pii_table: pii_columns},
        "max_joins": 2,
        "max_limit": 50,
    }
    rules = [
        f"Column(s) {', '.join(pii_columns)} on {pii_table!r} stay denied.",
        "Joins capped at 2; row selects capped at 50 rows.",
    ]
    return connection, patch, rules


def _build_tenant_isolated(params: dict) -> _BuildResult:
    connection = _require_str(params, "connection")
    table = _require_str(params, "table")
    tenant_column = _require_str(params, "tenant_column")
    tenant_claim = _require_str(params, "tenant_claim")
    patch = {
        "mandatory_row_filters": [
            {"table": table, "column": tenant_column, "from_claim": tenant_claim}
        ]
    }
    rules = [
        f"Connection {connection!r}: every query against {table!r} is scoped to "
        f"{table}.{tenant_column} = the caller's {tenant_claim!r} claim.",
        "A caller without that claim is rejected rather than served an unfiltered result.",
    ]
    return connection, patch, rules


def _build_bounded_analytics(params: dict) -> _BuildResult:
    connection = _require_str(params, "connection")
    max_joins = _optional_int(params, "max_joins", 3)
    max_limit_aggregate = _optional_int(params, "max_limit_aggregate", 500)
    timeout_seconds = _optional_int(params, "timeout_seconds", 15)
    patch = {
        "max_joins": max_joins,
        "max_limit_aggregate": max_limit_aggregate,
        "timeout_seconds": timeout_seconds,
    }
    rules = [
        f"Connection {connection!r}: joins capped at {max_joins}, aggregate row selects capped at "
        f"{max_limit_aggregate}, query timeout capped at {timeout_seconds}s."
    ]
    return connection, patch, rules


@dataclass(frozen=True)
class _TemplateDef:
    id: str
    name: str
    description: str
    parameters: list[TemplateParameter]
    build: Callable[[dict], _BuildResult]


_CONNECTION_PARAM = TemplateParameter(
    name="connection",
    label="Connection",
    type="string",
    description="Connection id this template applies to.",
)

_TEMPLATES: dict[str, _TemplateDef] = {
    t.id: t
    for t in [
        _TemplateDef(
            id="deny-by-default",
            name="Deny by default",
            description=(
                "Hides every connection until explicitly enabled per connection or principal — "
                "the safest starting point for a new deployment."
            ),
            parameters=[],
            build=_build_deny_by_default,
        ),
        _TemplateDef(
            id="reporting-only",
            name="Reporting only",
            description=(
                "Narrow, read-only reporting access to an explicit table list with tight "
                "join/depth/row caps."
            ),
            parameters=[
                _CONNECTION_PARAM,
                TemplateParameter(
                    name="allowed_tables",
                    label="Allowed tables",
                    type="string_list",
                    description="Only these tables become queryable.",
                ),
                TemplateParameter(
                    name="max_limit",
                    label="Max rows",
                    type="integer",
                    required=False,
                    default=100,
                    description="Row cap for non-aggregate selects.",
                ),
            ],
            build=_build_reporting_only,
        ),
        _TemplateDef(
            id="customer-support",
            name="Customer support",
            description="Table-scoped access with named PII columns denied and tight row/join caps.",
            parameters=[
                _CONNECTION_PARAM,
                TemplateParameter(
                    name="allowed_tables",
                    label="Allowed tables",
                    type="string_list",
                    description="Only these tables become queryable.",
                ),
                TemplateParameter(
                    name="pii_table",
                    label="Table with PII columns",
                    type="string",
                    description="Table the denied columns below belong to.",
                ),
                TemplateParameter(
                    name="pii_columns",
                    label="Denied columns",
                    type="string_list",
                    description="Columns on that table to keep denied.",
                ),
            ],
            build=_build_customer_support,
        ),
        _TemplateDef(
            id="tenant-isolated",
            name="Tenant isolated",
            description=(
                "Scopes every query against one table to the caller's own tenant claim via a "
                "mandatory row filter."
            ),
            parameters=[
                _CONNECTION_PARAM,
                TemplateParameter(
                    name="table",
                    label="Table",
                    type="string",
                    description="Table to scope by tenant.",
                ),
                TemplateParameter(
                    name="tenant_column",
                    label="Tenant column",
                    type="string",
                    description="Column holding the tenant identifier.",
                ),
                TemplateParameter(
                    name="tenant_claim",
                    label="Tenant claim",
                    type="string",
                    description="Authenticated principal claim providing the tenant value.",
                ),
            ],
            build=_build_tenant_isolated,
        ),
        _TemplateDef(
            id="bounded-analytics",
            name="Bounded analytics",
            description=(
                "Keeps an existing analytics connection's shape but tightens join/aggregate/"
                "timeout caps."
            ),
            parameters=[
                _CONNECTION_PARAM,
                TemplateParameter(
                    name="max_joins",
                    label="Max joins",
                    type="integer",
                    required=False,
                    default=3,
                    description="Join cap.",
                ),
                TemplateParameter(
                    name="max_limit_aggregate",
                    label="Max aggregate rows",
                    type="integer",
                    required=False,
                    default=500,
                    description="Aggregate row cap.",
                ),
                TemplateParameter(
                    name="timeout_seconds",
                    label="Timeout (s)",
                    type="integer",
                    required=False,
                    default=15,
                    description="Query timeout cap.",
                ),
            ],
            build=_build_bounded_analytics,
        ),
    ]
}


def list_templates() -> list[PolicyTemplateSummary]:
    return [
        PolicyTemplateSummary(
            id=t.id, name=t.name, description=t.description, parameters=t.parameters
        )
        for t in _TEMPLATES.values()
    ]


def _mapping(value: object) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _merge_cap_fields(existing: dict, patch: dict, rules_out: list[str], label: str) -> None:
    for field_name in _CAP_FIELDS:
        if field_name not in patch:
            continue
        template_value = patch[field_name]
        current_value = existing.get(field_name)
        if current_value is not None:
            merged = min(current_value, template_value)
            if merged != template_value:
                rules_out.append(
                    f"{label}: kept the existing, stricter {field_name}={merged} instead of the "
                    f"template's {template_value}."
                )
        else:
            merged = template_value
        existing[field_name] = merged


def _merge_allowed_tables(existing: dict, patch: dict, rules_out: list[str], label: str) -> None:
    if "allowed_tables" not in patch:
        return
    current = existing.get("allowed_tables") or []
    template_value = patch["allowed_tables"]
    # An empty existing list means "no restriction" — adopting the template's
    # list only ever *adds* a restriction. A non-empty existing list is
    # already a restriction, so the template can only narrow it further.
    #
    # The intersection is case-insensitive (TODO.md item 149's bug class,
    # found by `security-invariant-reviewer`): an existing entry "Orders" must
    # still match the template's "orders" the same way `Policy.table_allowed`
    # already treats them as the same table. A plain `in` check here missed
    # that match, silently intersecting down to an EMPTY list — which
    # `table_allowed` reads as "no restriction at all", the exact opposite of
    # this module's own "monotonically restrictive" invariant.
    template_folded = {table.casefold() for table in template_value}
    merged = (
        list(template_value)
        if not current
        else [table for table in current if table.casefold() in template_folded]
    )
    existing["allowed_tables"] = merged
    rules_out.append(
        f"{label}: queryable tables narrowed to {', '.join(merged) if merged else '(none — empty intersection with the existing allow-list)'}."
    )


def _merge_denied_columns(existing: dict, patch: dict) -> None:
    if "denied_columns" not in patch:
        return
    current = _mapping(existing.get("denied_columns"))
    # Resolve the patch's table key against an existing key that differs only
    # in casing (same bug class as above): a plain `current.get(table, [])`
    # would create a SECOND key instead of merging into the existing one, and
    # `Policy._ci_lookup`'s first-match-wins read would then only ever see
    # one of the two — silently dropping the other side's denied columns.
    canonical = {table.casefold(): table for table in current}
    for table, columns in patch["denied_columns"].items():
        key = canonical.setdefault(table.casefold(), table)
        merged_columns = list(current.get(key, []))
        for column in columns:
            if column not in merged_columns:
                merged_columns.append(column)
        current[key] = merged_columns
    existing["denied_columns"] = current


def _merge_mandatory_row_filters(existing: dict, patch: dict) -> None:
    if "mandatory_row_filters" not in patch:
        return
    current = list(existing.get("mandatory_row_filters") or [])
    existing_keys = {(f.get("table"), f.get("column")) for f in current if isinstance(f, dict)}
    for row_filter in patch["mandatory_row_filters"]:
        key = (row_filter.get("table"), row_filter.get("column"))
        if key not in existing_keys:
            current.append(row_filter)
            existing_keys.add(key)
    existing["mandatory_row_filters"] = current


def render_template(
    template_id: str, params: Optional[dict[str, Any]], policy_yaml: Optional[str]
) -> PolicyTemplateRenderResult:
    template = _TEMPLATES.get(template_id)
    if template is None:
        raise ConfigValidationError(f"Unknown policy template: {template_id!r}")

    scope, patch, rules = template.build(dict(params or {}))

    document = _mapping(yaml.safe_load(policy_yaml)) if policy_yaml and policy_yaml.strip() else {}

    if scope == "default":
        section = _mapping(document.get("default"))
        if patch.get("enabled") is False:
            section["enabled"] = False
        document["default"] = section
    else:
        connections = {
            key: _mapping(value) for key, value in _mapping(document.get("connections")).items()
        }
        section = connections.get(scope, {})
        label = f"Connection {scope!r}"
        _merge_cap_fields(section, patch, rules, label)
        _merge_allowed_tables(section, patch, rules, label)
        _merge_denied_columns(section, patch)
        _merge_mandatory_row_filters(section, patch)
        if patch.get("enabled") is False:
            section["enabled"] = False
        connections[scope] = section
        document["connections"] = connections

    rendered = yaml.safe_dump(
        document, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    return PolicyTemplateRenderResult(policy_yaml=rendered, rules=rules, warnings=[])
