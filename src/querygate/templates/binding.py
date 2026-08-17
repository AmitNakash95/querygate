"""Bind caller parameters into a query template, producing a StructuredQuery.

The security model is deliberately simple: a template's `query` skeleton is an
untrusted-shaped dict with `{"param": name}` placeholders; binding validates
each supplied value against its typed slot, substitutes it, and then validates
the *result* as a real `StructuredQuery`. Because the bound query is then run
through the unchanged `StructuredQueryService`, it is subject to every policy
cap, allow/deny list, mandatory row filter, schema check, and guardrail an
ad-hoc query is — a parameter can never smuggle SQL (there is no SQL, only a
validated AST) or exceed policy.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Mapping, Optional

from querygate.core.exceptions import QueryValidationError
from querygate.query_ast.models import StructuredQuery
from querygate.templates.models import QueryTemplate, TemplateParameter, scalar_type_error


def _coerce_scalar(param: TemplateParameter, value: Any) -> Any:
    """Type-check + constraint-check a single supplied scalar against the slot.

    The type/bounds check is `scalar_type_error` — the same primitive the slot's
    own load-time self-consistency validation uses — so a value the model would
    reject for a slot and a value binding rejects can never diverge. Enum
    membership is layered on top here.
    """
    reason = scalar_type_error(
        param.type, value, min=param.min, max=param.max, max_length=param.max_length
    )
    if reason is not None:
        raise QueryValidationError(f"parameter {param.name!r} {reason}")
    if param.allowed_values is not None and value not in param.allowed_values:
        raise QueryValidationError(f"parameter {param.name!r} is not an allowed value")
    return value


def validate_parameter_value(param: TemplateParameter, value: Any) -> Any:
    """Validate/normalize a supplied value against a parameter slot."""
    if param.is_list:
        if not isinstance(value, (list, tuple)):
            raise QueryValidationError(f"parameter {param.name!r} must be a list")
        if len(value) == 0:
            raise QueryValidationError(f"parameter {param.name!r} must be a non-empty list")
        return [_coerce_scalar(param, item) for item in value]
    return _coerce_scalar(param, value)


def _resolve_parameters(template: QueryTemplate, supplied: Mapping[str, Any]) -> Dict[str, Any]:
    declared = {p.name for p in template.parameters}
    unknown = set(supplied) - declared
    if unknown:
        raise QueryValidationError(f"unknown parameter(s): {', '.join(sorted(unknown))}")
    resolved: Dict[str, Any] = {}
    for param in template.parameters:
        if param.name in supplied:
            resolved[param.name] = validate_parameter_value(param, supplied[param.name])
        elif param.default is not None:
            resolved[param.name] = validate_parameter_value(param, param.default)
        elif param.required:
            raise QueryValidationError(f"missing required parameter {param.name!r}")
        else:
            raise QueryValidationError(f"parameter {param.name!r} was omitted and has no default")
    return resolved


def _substitute(node: Any, resolved: Mapping[str, Any]) -> Any:
    """Recursively replace {"param": name} placeholders with bound values."""
    if isinstance(node, dict):
        if set(node.keys()) == {"param"} and isinstance(node["param"], str):
            name = node["param"]
            if name not in resolved:
                raise QueryValidationError(f"template references undefined parameter {name!r}")
            return copy.deepcopy(resolved[name])
        return {key: _substitute(val, resolved) for key, val in node.items()}
    if isinstance(node, list):
        return [_substitute(item, resolved) for item in node]
    return node


def bind_template(template: QueryTemplate, supplied: Mapping[str, Any]) -> StructuredQuery:
    """Bind `supplied` parameters into `template`, returning a validated
    `StructuredQuery`. Raises `QueryValidationError` on any bad parameter or if
    the bound result is not a valid query.
    """
    resolved = _resolve_parameters(template, supplied)
    bound = _substitute(copy.deepcopy(template.query), resolved)
    try:
        return StructuredQuery.model_validate(bound)
    except Exception as exc:  # pydantic ValidationError et al.
        raise QueryValidationError(
            f"template {template.id!r} produced an invalid query: {exc}"
        ) from exc


_DUMMY_SCALAR = {"string": "x", "integer": 1, "number": 1.0, "boolean": True}


def dummy_bound_query(template: QueryTemplate) -> StructuredQuery:
    """Bind a type-appropriate dummy for every parameter and return the
    resulting validated `StructuredQuery`. Placeholder values don't affect which
    tables/columns the query references, so this yields a real query suitable
    for both the structural check below and the on-demand live-schema check
    (`admin.service.check_template_schema`) — neither needs real caller input.
    Raises on a structurally invalid skeleton (a bad field, a placeholder in a
    position its type can't satisfy, etc.).
    """
    dummies: Dict[str, Any] = {}
    for param in template.parameters:
        scalar = _DUMMY_SCALAR[param.type]
        # TWO elements, not one, for a list slot. A one-element dummy satisfies
        # `in`/`not_in` ("non-empty list") but fails `between`, which requires
        # exactly `[low, high]` — so any template carrying a BETWEEN predicate
        # was rejected by `querygate config check` even though it was perfectly
        # valid, and the failure came from this dry-run binder rather than from
        # the template. Two satisfies every list-taking operator the AST has
        # (`between` needs exactly 2; `in`/`not_in` need >= 1 with no upper
        # bound), so the arity does not have to be inferred from the operator
        # this slot happens to sit under. Surfaced by `claim-reviewer`
        # 2026-08-17 against item 195's promote-then-check workflow, where a
        # drafted template is checked before it is installed.
        dummies[param.name] = [scalar, scalar] if param.is_list else scalar
    bound = _substitute(copy.deepcopy(template.query), dummies)
    return StructuredQuery.model_validate(bound)


# Positions in the skeleton that name a table, column, alias or CTE — i.e. an
# IDENTIFIER, not a value. A template parameter must never land in one.
#
# The whole premise of a curated template is that the *author* fixes what the
# query touches and the *caller* only supplies values. A placeholder in an
# identifier position inverts that: the caller would choose the join columns, the
# grouping, or the table. Such a template is not an escalation on its own —
# the bound query still passes full policy and schema validation, so a denied
# identifier is still refused — but it silently converts a reviewed template back
# into a general query surface, which is exactly what `templates_only` exists to
# prevent. Better to refuse it at deploy time.
#
# Until the two-element dummy fix (item 195 phase 2) this was *accidentally*
# caught for list slots, because a one-element dummy failed `between`; a
# `claim-enforcing` review pointed out the accident was load-bearing. This makes
# the rule explicit instead.
_IDENTIFIER_POSITIONS = frozenset(
    {
        "from",
        "from_table",
        "from_alias",
        "table",
        "alias",
        "name",
        "col",
        "value_col",
        "on",
        "extra_on",
        "group_by",
        "partition_by",
        "correlate",
        "connection",
    }
)


def _placeholder_in_an_identifier_position(node: Any, path: str = "") -> Optional[str]:
    """The dotted path of the first identifier position holding a placeholder,
    or None. Walks the raw skeleton, so it covers nested subqueries and CTEs by
    construction rather than by enumeration."""
    if isinstance(node, dict):
        if set(node.keys()) == {"param"} and isinstance(node["param"], str):
            return path or "<root>"
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            if key in _IDENTIFIER_POSITIONS:
                found = _placeholder_in_an_identifier_position(value, child)
                if found:
                    return found
            else:
                found = _placeholder_in_an_identifier_position(value, child)
                if found and _path_touches_identifier(found):
                    return found
        return None
    if isinstance(node, list):
        for index, item in enumerate(node):
            found = _placeholder_in_an_identifier_position(item, f"{path}[{index}]")
            if found:
                return found
    return None


def _path_touches_identifier(path: str) -> bool:
    return any(
        segment.split("[")[0] in _IDENTIFIER_POSITIONS for segment in path.split(".") if segment
    )


def validate_template_structure(template: QueryTemplate) -> Optional[str]:
    """Deploy-time check that a template's skeleton forms a structurally valid
    `StructuredQuery` once its placeholders are filled — so `querygate-validate-
    config` catches a malformed template (a bad field, a wrong-shaped predicate,
    a placeholder in a position its declared type can't satisfy) before deploy,
    not only at first invocation. Returns an error string, or None if the
    skeleton is structurally sound. This is a *structural* check only — schema
    existence and policy remain runtime concerns, evaluated on the real bound
    query like any other (the on-demand live-schema check verifies existence
    separately, against the real database).
    """
    offending = _placeholder_in_an_identifier_position(template.query)
    if offending is not None:
        return (
            f"template {template.id!r} puts a parameter in an identifier position "
            f"({offending}). A template parameter may only supply a VALUE — the "
            "template author fixes which tables, columns, aliases and join keys "
            "the query touches, or the template stops being a narrower surface "
            "than an ad-hoc query."
        )
    try:
        dummy_bound_query(template)
    except Exception as exc:
        return f"template {template.id!r} is not a structurally valid query: {exc}"
    return None
