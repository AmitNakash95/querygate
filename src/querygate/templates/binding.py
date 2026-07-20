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
import math
from typing import Any, Dict, Mapping, Optional

from querygate.core.exceptions import QueryValidationError
from querygate.query_ast.models import StructuredQuery
from querygate.templates.models import QueryTemplate, TemplateParameter


def _coerce_scalar(param: TemplateParameter, value: Any) -> Any:
    """Type-check + constraint-check a single scalar against the slot."""
    if param.type == "boolean":
        if not isinstance(value, bool):
            raise QueryValidationError(f"parameter {param.name!r} must be a boolean")
        return value
    # bool is a subclass of int — exclude it from numeric/string types explicitly.
    if isinstance(value, bool):
        raise QueryValidationError(f"parameter {param.name!r} must be of type {param.type}")
    if param.type == "string":
        if not isinstance(value, str):
            raise QueryValidationError(f"parameter {param.name!r} must be a string")
        if param.max_length is not None and len(value) > param.max_length:
            raise QueryValidationError(
                f"parameter {param.name!r} exceeds max length {param.max_length}"
            )
    elif param.type == "integer":
        if not isinstance(value, int):
            raise QueryValidationError(f"parameter {param.name!r} must be an integer")
    elif param.type == "number":
        if not isinstance(value, (int, float)):
            raise QueryValidationError(f"parameter {param.name!r} must be a number")
        if not math.isfinite(value):
            raise QueryValidationError(f"parameter {param.name!r} must be finite")
    if param.type in ("integer", "number"):
        if param.min is not None and value < param.min:
            raise QueryValidationError(f"parameter {param.name!r} is below min {param.min}")
        if param.max is not None and value > param.max:
            raise QueryValidationError(f"parameter {param.name!r} is above max {param.max}")
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


def validate_template_structure(template: QueryTemplate) -> Optional[str]:
    """Deploy-time check that a template's skeleton forms a structurally valid
    `StructuredQuery` once its placeholders are filled — so `querygate-validate-
    config` catches a malformed template (a bad field, a wrong-shaped predicate,
    a placeholder in a position its declared type can't satisfy) before deploy,
    not only at first invocation. Substitutes a type-appropriate dummy for each
    parameter and validates the result. Returns an error string, or None if the
    skeleton is structurally sound. This is a *structural* check only — schema
    existence and policy remain runtime concerns, evaluated on the real bound
    query like any other.
    """
    dummies: Dict[str, Any] = {}
    for param in template.parameters:
        scalar = _DUMMY_SCALAR[param.type]
        dummies[param.name] = [scalar] if param.is_list else scalar
    try:
        bound = _substitute(copy.deepcopy(template.query), dummies)
        StructuredQuery.model_validate(bound)
    except Exception as exc:
        return f"template {template.id!r} is not a structurally valid query: {exc}"
    return None
